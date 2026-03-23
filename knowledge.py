# knowledge.py
"""Knowledge-as-a-Service: acquire, ingest, query knowledge packs."""

import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from identity import sign_bytes, verify_bytes

logger = logging.getLogger("thrall.knowledge")

# ── Chunking ──

def chunk_markdown(text: str, source_file: str, domain: str,
                   max_chunk: int = 1500, min_chunk: int = 100) -> List[dict]:
    """Split markdown into chunks by ## headers, then by paragraphs if too large."""
    sections = re.split(r'(?=^## )', text, flags=re.MULTILINE)
    chunks = []
    current_section = ""

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Extract section title for context prefix
        title_match = re.match(r'^##\s+(.+)', section)
        title = title_match.group(1).strip() if title_match else ""

        if len(section) <= max_chunk:
            # Check if too small to stand alone
            if len(section) < min_chunk and chunks:
                # Merge with previous chunk
                chunks[-1]["chunk_text"] += "\n\n" + section
                continue
            current_section = section
        else:
            # Split on paragraph boundaries
            paragraphs = section.split("\n\n")
            current_section = ""
            for para in paragraphs:
                if len(current_section) + len(para) + 2 > max_chunk and current_section:
                    chunks.append(_make_chunk(
                        current_section, source_file, domain, title, len(chunks)))
                    current_section = para
                else:
                    current_section = (current_section + "\n\n" + para).strip()
            # Don't append yet — fall through to append below

        if current_section:
            chunks.append(_make_chunk(
                current_section, source_file, domain, title, len(chunks)))
            current_section = ""

    # Handle remaining text
    if current_section:
        if len(current_section) < min_chunk and chunks:
            chunks[-1]["chunk_text"] += "\n\n" + current_section
        else:
            chunks.append(_make_chunk(
                current_section, source_file, domain, "", len(chunks)))

    return chunks


def _make_chunk(text: str, source_file: str, domain: str,
                section: str, index: int) -> dict:
    prefix = f"[domain: {domain} | file: {source_file}"
    if section:
        prefix += f" | section: {section}"
    prefix += "]"
    return {
        "chunk_text": f"{prefix}\n{text}",
        "source_file": source_file,
        "chunk_index": index,
    }


# ── KnowledgeManager ──

class KnowledgeManager:
    """Manages knowledge pack ingestion, storage, and query."""

    def __init__(self, db, backend=None, plugin_dir: str = "",
                 config: Optional[Dict[str, Any]] = None):
        self._db = db
        self._backend = backend
        self._plugin_dir = plugin_dir
        cfg = config or {}
        self._trust_level = cfg.get("trust_level", "none")
        self._storage_dir = os.path.join(
            plugin_dir, cfg.get("storage_dir", "knowledge"))
        self._max_domains = int(cfg.get("max_domains", 50))
        self._max_chunks = int(cfg.get("max_chunks_per_domain", 500))
        self._max_pack_bytes = int(cfg.get("max_pack_bytes", 5_242_880))
        self._embedding_source = cfg.get("embedding_source", "l1")
        self._overrides = cfg.get("overrides", {})
        self._executor = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="knowledge")
        self._vec_available = False
        self._vec_dim = None
        self._detect_vec()

    def _detect_vec(self):
        """Check if sqlite-vec is available."""
        try:
            import sqlite_vec
            sqlite_vec.load(self._db._conn)
            self._vec_available = True
            logger.info("KNOWLEDGE sqlite-vec available")
        except Exception:
            self._vec_available = False
            logger.info("KNOWLEDGE sqlite-vec unavailable — FTS5 fallback")

    def _get_trust_level(self, domain: str) -> str:
        """Get trust level for a domain (per-domain override or global)."""
        override = self._overrides.get(domain, {})
        return override.get("trust_level", self._trust_level)

    # ── Validation ──

    def _validate_pack(self, pack: dict) -> None:
        """Validate pack structure, size, hash integrity, and signature."""
        # Required fields
        for key in ("domain", "version", "description", "files", "metadata"):
            if key not in pack:
                raise ValueError(f"Missing required field: {key}")
        meta = pack["metadata"]
        for key in ("author", "author_pubkey", "sha256", "signature"):
            if key not in meta:
                raise ValueError(f"Missing metadata field: {key}")

        # Size check
        content_size = sum(len(v.encode()) for v in pack["files"].values())
        for v in pack.get("recipe", {}).values():
            content_size += len(v.encode())
        if content_size > self._max_pack_bytes:
            raise ValueError(
                f"Pack size {content_size} exceeds max_pack_bytes "
                f"{self._max_pack_bytes}")

        # SHA256 integrity
        canonical = json.dumps(
            {"files": pack["files"], "recipe": pack.get("recipe", {})},
            sort_keys=True, ensure_ascii=False)
        computed = hashlib.sha256(canonical.encode()).hexdigest()
        if computed != meta["sha256"]:
            raise ValueError(
                f"SHA256 integrity check failed: computed={computed[:16]}... "
                f"claimed={meta['sha256'][:16]}...")

        # Ed25519 signature
        try:
            verify_bytes(
                meta["author_pubkey"],
                computed.encode(),
                bytes.fromhex(meta["signature"]))
        except Exception as e:
            raise ValueError(f"Ed25519 signature verification failed: {e}")

    # ── Ingestion ──

    def ingest(self, pack: dict, sender_node_id: str = "") -> dict:
        """Validate and ingest a knowledge pack. Non-blocking.

        Returns status dict immediately. Background thread handles
        chunking, embedding, and indexing.
        """
        self._validate_pack(pack)
        domain = pack["domain"]
        version = pack["version"]

        # Transport check (advisory)
        if sender_node_id and pack["metadata"]["author"] != sender_node_id:
            logger.info(
                f"KNOWLEDGE_TRANSPORT_MISMATCH domain={domain} "
                f"author={pack['metadata']['author'][:16]} "
                f"sender={sender_node_id[:16]}")

        # Idempotency checks
        existing = self._db._conn.execute(
            "SELECT version, ingestion_status FROM thrall_knowledge_meta "
            "WHERE domain = ?", (domain,)).fetchone()
        if existing:
            if existing["ingestion_status"] == "ingesting":
                return {"status": "ingesting", "domain": domain}
            # Simple semver tuple comparison (no external dependency)
            def _ver_tuple(v):
                try:
                    return tuple(int(x) for x in v.split("."))
                except (ValueError, AttributeError):
                    return (0,)
            ev = _ver_tuple(existing["version"])
            nv = _ver_tuple(version)
            if ev == nv:
                return {"status": "exists", "domain": domain}
            if ev > nv:
                return {"status": "newer_exists", "domain": domain,
                        "current_version": existing["version"]}

        # Domain count check
        count = self._db._conn.execute(
            "SELECT COUNT(*) FROM thrall_knowledge_meta").fetchone()[0]
        if count >= self._max_domains and not existing:
            raise ValueError(
                f"Max domains ({self._max_domains}) reached. "
                f"Remove a domain before adding new ones.")

        # Save raw files immediately
        domain_dir = os.path.join(self._storage_dir, domain)
        os.makedirs(domain_dir, exist_ok=True)
        for fname, content in pack["files"].items():
            with open(os.path.join(domain_dir, fname), "w",
                      encoding="utf-8") as f:
                f.write(content)

        # Save recipes to disk (loaded later based on trust level)
        if pack.get("recipe"):
            recipe_dir = os.path.join(domain_dir, "recipes")
            os.makedirs(recipe_dir, exist_ok=True)
            for fname, content in pack["recipe"].items():
                with open(os.path.join(recipe_dir, fname), "w",
                          encoding="utf-8") as f:
                    f.write(content)

        # Insert/update metadata
        self._db._conn.execute("""
            INSERT OR REPLACE INTO thrall_knowledge_meta
                (domain, version, description, author_node, author_pubkey,
                 acquired_at, file_count, chunk_count, ingestion_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'ingesting')
        """, (domain, version, pack["description"],
              pack["metadata"]["author"], pack["metadata"]["author_pubkey"],
              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              len(pack["files"])))
        self._db._conn.commit()

        # Background ingestion
        self._executor.submit(self._ingest_background, pack)

        return {"status": "accepted", "domain": domain,
                "files": len(pack["files"])}

    def _ingest_background(self, pack: dict):
        """Background thread: chunk, embed, index."""
        domain = pack["domain"]
        t0 = time.time()
        try:
            # Delete old chunks if upgrading
            self._db._conn.execute(
                "DELETE FROM thrall_knowledge WHERE domain = ?", (domain,))

            # Chunk all files
            all_chunks = []
            for fname, content in pack["files"].items():
                chunks = chunk_markdown(content, fname, domain)
                all_chunks.extend(chunks)

            # Enforce chunk limit
            if len(all_chunks) > self._max_chunks:
                all_chunks = all_chunks[:self._max_chunks]
                logger.warning(
                    f"KNOWLEDGE_TRUNCATED domain={domain} "
                    f"chunks={len(all_chunks)} max={self._max_chunks}")

            # Embed and insert
            for i, chunk in enumerate(all_chunks):
                embedding = None
                if self._backend and self._vec_available:
                    try:
                        embedding = self._backend.embed(chunk["chunk_text"])
                    except Exception as e:
                        logger.warning(f"Embed failed for chunk {i}: {e}")

                # Insert chunk
                blob = None
                if embedding:
                    blob = struct.pack(f'{len(embedding)}f', *embedding)
                cur = self._db._conn.execute("""
                    INSERT OR REPLACE INTO thrall_knowledge
                        (domain, source_file, chunk_index, chunk_text,
                         embedding, version, acquired_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (domain, chunk["source_file"], chunk["chunk_index"],
                      chunk["chunk_text"], blob, pack["version"],
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

                # Insert into vec table if available
                if embedding and self._vec_available:
                    try:
                        if self._vec_dim is None:
                            self._vec_dim = len(embedding)
                            self._db._conn.execute(f"""
                                CREATE VIRTUAL TABLE IF NOT EXISTS
                                thrall_knowledge_vec USING vec0(
                                    id INTEGER PRIMARY KEY,
                                    embedding float[{self._vec_dim}]
                                )
                            """)
                        self._db._conn.execute(
                            "INSERT INTO thrall_knowledge_vec(id, embedding) "
                            "VALUES (?, ?)",
                            (cur.lastrowid, blob))
                    except Exception as e:
                        logger.warning(f"Vec insert failed: {e}")

                # Insert into FTS5
                try:
                    self._db._conn.execute(
                        "INSERT INTO thrall_knowledge_fts"
                        "(rowid, chunk_text, domain, source_file) "
                        "VALUES (?, ?, ?, ?)",
                        (cur.lastrowid, chunk["chunk_text"],
                         domain, chunk["source_file"]))
                except Exception as e:
                    logger.warning(f"FTS5 insert failed: {e}")

            self._db._conn.commit()

            # Update metadata
            wall_ms = int((time.time() - t0) * 1000)
            model_name = self._backend.model_name if self._backend else "none"
            self._db._conn.execute("""
                UPDATE thrall_knowledge_meta
                SET chunk_count = ?, ingestion_status = 'ready',
                    embedding_model = ?
                WHERE domain = ?
            """, (len(all_chunks), model_name, domain))
            self._db._conn.commit()

            logger.info(
                f"KNOWLEDGE_INGESTED domain={domain} "
                f"chunks={len(all_chunks)} version={pack['version']} "
                f"wall_ms={wall_ms}")

            # Load recipes if trust allows
            self._maybe_load_recipes(pack)

        except Exception as e:
            logger.error(f"KNOWLEDGE_INGEST_FAILED domain={domain}: {e}")
            self._db._conn.execute("""
                UPDATE thrall_knowledge_meta
                SET ingestion_status = 'failed'
                WHERE domain = ?
            """, (domain,))
            self._db._conn.commit()

    def _maybe_load_recipes(self, pack: dict):
        """Load recipes from pack if trust level allows."""
        domain = pack["domain"]
        trust = self._get_trust_level(domain)
        if trust == "none" or not pack.get("recipe"):
            return
        # Recipe loading delegated to loader.py (Task 7)
        # Placeholder: recipes are on disk, loader picks them up on reload

    # ── Query ──

    def query_knowledge(self, domain: str, query_text: str,
                        top_k: int = 5) -> str:
        """Similarity search (or FTS5 fallback) over a knowledge domain."""
        # Check domain status
        meta = self._db._conn.execute(
            "SELECT ingestion_status, embedding_model FROM "
            "thrall_knowledge_meta WHERE domain = ?",
            (domain,)).fetchone()
        if not meta:
            return ""
        if meta["ingestion_status"] in ("pending", "ingesting"):
            logger.debug(f"KNOWLEDGE_PENDING domain={domain}")
            return ""
        if meta["ingestion_status"] == "stale":
            # Fall through to FTS5
            pass

        # Stale check — model mismatch triggers re-embed
        if (meta["ingestion_status"] == "ready" and self._backend
                and meta["embedding_model"]
                and meta["embedding_model"] != (
                    self._backend.model_name if self._backend else "")):
            logger.warning(
                f"KNOWLEDGE_EMBEDDING_MISMATCH domain={domain} "
                f"model_stored={meta['embedding_model']} "
                f"model_active={self._backend.model_name}")
            self._db._conn.execute(
                "UPDATE thrall_knowledge_meta SET ingestion_status='stale' "
                "WHERE domain=?", (domain,))
            self._db._conn.commit()
            # Trigger background re-embed (future: self._executor.submit(...))

        # Vector search
        if self._vec_available and meta["ingestion_status"] == "ready":
            try:
                vec_list = self._backend.embed(query_text)
                query_vec = struct.pack(f'{len(vec_list)}f', *vec_list)
                rows = self._db._conn.execute("""
                    SELECT k.chunk_text, k.source_file
                    FROM thrall_knowledge k
                    JOIN thrall_knowledge_vec v ON k.id = v.id
                    WHERE k.domain = ?
                    ORDER BY vec_distance_cosine(v.embedding, ?)
                    LIMIT ?
                """, (domain, query_vec, top_k)).fetchall()
                return self._format_chunks(rows)
            except Exception as e:
                logger.warning(f"Vec query failed, falling back to FTS5: {e}")

        # FTS5 fallback
        keywords = self._extract_keywords(query_text)
        if not keywords:
            # No keywords — return first N chunks by index
            rows = self._db._conn.execute("""
                SELECT chunk_text, source_file FROM thrall_knowledge
                WHERE domain = ? ORDER BY chunk_index LIMIT ?
            """, (domain, top_k)).fetchall()
        else:
            try:
                rows = self._db._conn.execute("""
                    SELECT k.chunk_text, k.source_file
                    FROM thrall_knowledge_fts f
                    JOIN thrall_knowledge k ON f.rowid = k.id
                    WHERE f.domain = ? AND f.chunk_text MATCH ?
                    LIMIT ?
                """, (domain, keywords, top_k)).fetchall()
            except Exception:
                rows = self._db._conn.execute("""
                    SELECT chunk_text, source_file FROM thrall_knowledge
                    WHERE domain = ? ORDER BY chunk_index LIMIT ?
                """, (domain, top_k)).fetchall()

        return self._format_chunks(rows)

    def _format_chunks(self, rows) -> str:
        """Format query results as text for prompt injection."""
        if not rows:
            return ""
        parts = []
        for row in rows:
            parts.append(row["chunk_text"] if isinstance(row, dict)
                         else row[0])
        return "\n\n---\n\n".join(parts)

    def _extract_keywords(self, text: str) -> str:
        """Extract keywords from text for FTS5 MATCH query."""
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
        # Remove common stop words
        stops = {"the", "and", "for", "are", "but", "not", "you",
                 "all", "can", "had", "her", "was", "one", "our",
                 "out", "has", "have", "this", "that", "with", "from"}
        keywords = [w for w in words if w not in stops][:10]
        return " OR ".join(keywords) if keywords else ""

    # ── Cleanup ──

    def remove_domain(self, domain: str) -> dict:
        """Remove all data for a knowledge domain."""
        # Collect IDs BEFORE deleting from content table
        ids = [r[0] for r in self._db._conn.execute(
            "SELECT id FROM thrall_knowledge WHERE domain = ?",
            (domain,)).fetchall()]

        # Delete from vec table first (needs IDs from content table)
        if self._vec_available and ids:
            try:
                placeholders = ",".join("?" * len(ids))
                self._db._conn.execute(
                    f"DELETE FROM thrall_knowledge_vec "
                    f"WHERE id IN ({placeholders})", ids)
            except Exception:
                pass

        # Delete from FTS5 (uses rowid matching content table IDs)
        if ids:
            try:
                placeholders = ",".join("?" * len(ids))
                self._db._conn.execute(
                    f"DELETE FROM thrall_knowledge_fts "
                    f"WHERE rowid IN ({placeholders})", ids)
            except Exception:
                pass

        # Now safe to delete from content table
        self._db._conn.execute(
            "DELETE FROM thrall_knowledge WHERE domain = ?", (domain,))
        self._db._conn.execute(
            "DELETE FROM thrall_knowledge_meta WHERE domain = ?", (domain,))
        self._db._conn.commit()

        # Remove files
        domain_dir = os.path.join(self._storage_dir, domain)
        if os.path.isdir(domain_dir):
            import shutil
            shutil.rmtree(domain_dir, ignore_errors=True)

        logger.info(f"KNOWLEDGE_REMOVED domain={domain}")
        return {"status": "removed", "domain": domain}

    def list_domains(self) -> list:
        """List all knowledge domains with status."""
        rows = self._db._conn.execute(
            "SELECT domain, version, description, chunk_count, "
            "ingestion_status, acquired_at FROM thrall_knowledge_meta"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_domain_status(self, domain: str) -> Optional[dict]:
        """Get status for a single domain."""
        row = self._db._conn.execute(
            "SELECT * FROM thrall_knowledge_meta WHERE domain = ?",
            (domain,)).fetchone()
        return dict(row) if row else None

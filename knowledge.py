# knowledge.py
"""Knowledge-as-a-Service: acquire, ingest, query knowledge packs.

Retrieval modes:
- fts:    FTS5 keyword match only (no embeddings needed)
- vec:    Vector similarity only (requires embed model)
- hybrid: FTS + VEC merged via Reciprocal Rank Fusion
- rerank: FTS broad recall, reranked by embedding cosine similarity
- auto:   hybrid if vec available, else fts
"""

import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
import urllib.request
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

        title_match = re.match(r'^##\s+(.+)', section)
        title = title_match.group(1).strip() if title_match else ""

        if len(section) <= max_chunk:
            if len(section) < min_chunk and chunks:
                chunks[-1]["chunk_text"] += "\n\n" + section
                continue
            current_section = section
        else:
            paragraphs = section.split("\n\n")
            current_section = ""
            for para in paragraphs:
                if len(current_section) + len(para) + 2 > max_chunk and current_section:
                    chunks.append(_make_chunk(
                        current_section, source_file, domain, title, len(chunks)))
                    current_section = para
                else:
                    current_section = (current_section + "\n\n" + para).strip()

        if current_section:
            chunks.append(_make_chunk(
                current_section, source_file, domain, title, len(chunks)))
            current_section = ""

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
        "section": section,
        "chunk_index": index,
    }


def _sanitize_filename(name: str) -> str:
    """Strip path separators and parent references from filenames."""
    base = os.path.basename(name)
    base = base.replace("..", "").replace("\x00", "")
    if not base:
        base = "unnamed"
    return base


# ── OllamaEmbedder ──

class OllamaEmbedder:
    """Lightweight embedding client for Ollama's /api/embed endpoint."""

    def __init__(self, url: str, model: str, timeout: int = 60):
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._dim = None

    @property
    def model_name(self) -> str:
        return self._model

    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        payload = json.dumps({"model": self._model, "input": texts}).encode()
        req = urllib.request.Request(
            f"{self._url}/api/embed", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            resp = json.loads(
                urllib.request.urlopen(req, timeout=self._timeout).read())
            embeddings = resp.get("embeddings", [])
            if embeddings and self._dim is None:
                self._dim = len(embeddings[0])
            return embeddings
        except Exception as e:
            raise RuntimeError(f"Ollama embed failed: {e}") from e

    @property
    def dim(self) -> Optional[int]:
        return self._dim


# ── KnowledgeManager ──

class KnowledgeManager:
    """Manages knowledge pack ingestion, storage, and retrieval."""

    def __init__(self, db, backend=None, plugin_dir: str = "",
                 config: Optional[Dict[str, Any]] = None):
        self._db = db
        self._backend = backend
        self._plugin_dir = plugin_dir
        self._lock = threading.Lock()
        cfg = config or {}
        self._trust_level = cfg.get("trust_level", "none")
        self._storage_dir = os.path.join(
            plugin_dir, cfg.get("storage_dir", "knowledge"))
        self._max_domains = int(cfg.get("max_domains", 50))
        self._max_chunks = int(cfg.get("max_chunks_per_domain", 500))
        self._max_pack_bytes = int(cfg.get("max_pack_bytes", 5_242_880))
        self._retrieval_mode = cfg.get("retrieval_mode", "auto")
        self._rrf_k = int(cfg.get("rrf_k", 60))
        self._rerank_candidates = int(cfg.get("rerank_candidates", 20))
        self._wing = cfg.get("wing", "")
        self._overrides = cfg.get("overrides", {})
        self._executor = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="knowledge")
        # Embedder
        embed_model = cfg.get("embed_model", "")
        embed_url = cfg.get("embed_url", "")
        if embed_model:
            if not embed_url and backend:
                embed_url = getattr(backend, '_url', 'http://127.0.0.1:11434')
            self._embedder = OllamaEmbedder(
                embed_url or "http://127.0.0.1:11434", embed_model)
        else:
            self._embedder = None

        # sqlite-vec detection
        self._vec_available = False
        self._vec_dim = None
        self._detect_vec()

    def _detect_vec(self):
        try:
            import sqlite_vec
            sqlite_vec.load(self._db._conn)
            self._vec_available = True
            logger.info("KNOWLEDGE sqlite-vec available")
        except Exception:
            self._vec_available = False
            logger.info("KNOWLEDGE sqlite-vec unavailable — FTS5 fallback")

    def _resolve_mode(self, domain: str = "") -> str:
        """Resolve retrieval mode with per-domain override."""
        override = self._overrides.get(domain, {})
        mode = override.get("retrieval_mode", self._retrieval_mode)
        if mode == "auto":
            return "hybrid" if (self._vec_available and self._embedder) else "fts"
        return mode

    def _get_trust_level(self, domain: str) -> str:
        override = self._overrides.get(domain, {})
        return override.get("trust_level", self._trust_level)

    # ── Validation ──

    def _validate_pack(self, pack: dict) -> None:
        for key in ("domain", "version", "description", "files", "metadata"):
            if key not in pack:
                raise ValueError(f"Missing required field: {key}")
        meta = pack["metadata"]
        for key in ("author", "author_pubkey", "sha256", "signature"):
            if key not in meta:
                raise ValueError(f"Missing metadata field: {key}")

        content_size = sum(len(v.encode()) for v in pack["files"].values())
        for v in pack.get("recipe", {}).values():
            content_size += len(v.encode())
        if pack.get("chunks"):
            content_size += sum(len(c.get("text", "").encode())
                                for c in pack["chunks"])
        if content_size > self._max_pack_bytes:
            raise ValueError(
                f"Pack size {content_size} exceeds max {self._max_pack_bytes}")

        hash_content = {
            "files": pack["files"],
            "recipe": pack.get("recipe", {}),
        }
        if "chunks" in pack:
            # Pre-vectorized: include chunk texts (not embeddings) in hash
            # Embeddings are model-dependent and may be regenerated;
            # the text content is the authoritative signed payload
            hash_content["chunk_texts"] = [
                c["text"] for c in pack["chunks"]]
        canonical = json.dumps(hash_content, sort_keys=True,
                               ensure_ascii=False)
        computed = hashlib.sha256(canonical.encode()).hexdigest()
        if computed != meta["sha256"]:
            raise ValueError(
                f"SHA256 mismatch: computed={computed[:16]} "
                f"claimed={meta['sha256'][:16]}")

        try:
            verify_bytes(
                meta["author_pubkey"],
                computed.encode(),
                bytes.fromhex(meta["signature"]))
        except Exception as e:
            raise ValueError(f"Ed25519 signature failed: {e}")

    # ── Ingestion ──

    def ingest(self, pack: dict, sender_node_id: str = "") -> dict:
        self._validate_pack(pack)
        domain = pack["domain"]
        version = pack["version"]
        wing = self._wing

        if sender_node_id and pack["metadata"]["author"] != sender_node_id:
            logger.info(
                f"KNOWLEDGE_TRANSPORT_MISMATCH domain={domain} "
                f"author={pack['metadata']['author'][:16]} "
                f"sender={sender_node_id[:16]}")

        with self._lock:
            existing = self._db._conn.execute(
                "SELECT version, ingestion_status FROM thrall_knowledge_meta "
                "WHERE domain = ? AND wing = ?", (domain, wing)).fetchone()
            if existing:
                if existing["ingestion_status"] == "ingesting":
                    return {"status": "ingesting", "domain": domain}
                def _ver(v):
                    try: return tuple(int(x) for x in v.split("."))
                    except: return (0,)
                if _ver(existing["version"]) == _ver(version):
                    return {"status": "exists", "domain": domain}
                if _ver(existing["version"]) > _ver(version):
                    return {"status": "newer_exists", "domain": domain,
                            "current_version": existing["version"]}

            count = self._db._conn.execute(
                "SELECT COUNT(*) FROM thrall_knowledge_meta WHERE wing = ?",
                (wing,)).fetchone()[0]
            if count >= self._max_domains and not existing:
                raise ValueError(f"Max domains ({self._max_domains}) reached.")

            # Save raw files with sanitized names
            domain_dir = os.path.join(self._storage_dir,
                                      _sanitize_filename(domain))
            os.makedirs(domain_dir, exist_ok=True)
            for fname, content in pack["files"].items():
                safe_name = _sanitize_filename(fname)
                with open(os.path.join(domain_dir, safe_name), "w",
                          encoding="utf-8") as f:
                    f.write(content)

            if pack.get("recipe"):
                recipe_dir = os.path.join(domain_dir, "recipes")
                os.makedirs(recipe_dir, exist_ok=True)
                for fname, content in pack["recipe"].items():
                    safe_name = _sanitize_filename(fname)
                    with open(os.path.join(recipe_dir, safe_name), "w",
                              encoding="utf-8") as f:
                        f.write(content)

            self._db._conn.execute("""
                INSERT OR REPLACE INTO thrall_knowledge_meta
                    (domain, wing, version, description, author_node, author_pubkey,
                     acquired_at, file_count, chunk_count, ingestion_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'ingesting')
            """, (domain, wing, version, pack["description"],
                  pack["metadata"]["author"], pack["metadata"]["author_pubkey"],
                  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  len(pack["files"])))
            self._db._conn.commit()

        self._executor.submit(self._ingest_background, pack)

        return {"status": "accepted", "domain": domain,
                "files": len(pack["files"])}

    def _ingest_background(self, pack: dict):
        domain = pack["domain"]
        wing = self._wing
        t0 = time.time()
        with self._lock:
            try:
                # Begin explicit transaction for atomicity
                self._db._conn.execute("BEGIN IMMEDIATE")

                self._db._conn.execute(
                    "DELETE FROM thrall_knowledge WHERE domain = ? AND wing = ?",
                    (domain, wing))

                # Pre-vectorized packs: chunks + embeddings already computed
                # by the curator. Consumer skips chunking and embedding.
                pre_vectorized = pack.get("chunks")

                if pre_vectorized:
                    all_chunks = []
                    embeddings = []
                    has_embeddings = False
                    embed_model = pack.get("embed_model", "")
                    for pc in pre_vectorized:
                        all_chunks.append({
                            "chunk_text": pc["text"],
                            "source_file": pc.get("source_file", ""),
                            "section": pc.get("section", ""),
                            "chunk_index": len(all_chunks),
                        })
                        emb = pc.get("embedding")
                        embeddings.append(emb)
                        if emb:
                            has_embeddings = True
                    logger.info(
                        f"KNOWLEDGE_PREVEC domain={domain} "
                        f"chunks={len(all_chunks)} "
                        f"has_embeddings={has_embeddings} "
                        f"embed_model={embed_model}")
                else:
                    # Standard path: chunk from files, embed locally
                    all_chunks = []
                    for fname, content in pack["files"].items():
                        chunks = chunk_markdown(
                            content, _sanitize_filename(fname), domain)
                        all_chunks.extend(chunks)

                    # Batch embed if embedder available
                    embeddings = [None] * len(all_chunks)
                    has_embeddings = False
                    if self._embedder and self._vec_available:
                        try:
                            texts = [c["chunk_text"] for c in all_chunks]
                            for b_start in range(0, len(texts), 20):
                                batch = texts[b_start:b_start + 20]
                                embs = self._embedder.embed_batch(batch)
                                for j, emb in enumerate(embs):
                                    embeddings[b_start + j] = emb
                                    has_embeddings = True
                        except Exception as e:
                            logger.warning(f"Batch embed failed: {e}")

                if len(all_chunks) > self._max_chunks:
                    all_chunks = all_chunks[:self._max_chunks]
                    logger.warning(
                        f"KNOWLEDGE_TRUNCATED domain={domain} "
                        f"chunks={len(all_chunks)} max={self._max_chunks}")

                for i, chunk in enumerate(all_chunks):
                    blob = None
                    if embeddings[i]:
                        blob = struct.pack(
                            f'{len(embeddings[i])}f', *embeddings[i])

                    cur = self._db._conn.execute("""
                        INSERT INTO thrall_knowledge
                            (domain, wing, source_file, chunk_index,
                             chunk_text, section, embedding, version,
                             acquired_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (domain, wing, chunk["source_file"],
                          chunk["chunk_index"], chunk["chunk_text"],
                          chunk.get("section", ""), blob, pack["version"],
                          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

                    if embeddings[i] and self._vec_available:
                        try:
                            if self._vec_dim is None:
                                self._vec_dim = len(embeddings[i])
                                self._db._conn.execute(f"""
                                    CREATE VIRTUAL TABLE IF NOT EXISTS
                                    thrall_knowledge_vec USING vec0(
                                        id INTEGER PRIMARY KEY,
                                        embedding float[{self._vec_dim}]
                                    )
                                """)
                            self._db._conn.execute(
                                "INSERT INTO thrall_knowledge_vec"
                                "(id, embedding) VALUES (?, ?)",
                                (cur.lastrowid, blob))
                        except Exception as e:
                            logger.warning(f"Vec insert failed: {e}")

                    try:
                        self._db._conn.execute(
                            "INSERT INTO thrall_knowledge_fts"
                            "(rowid, chunk_text, domain, source_file) "
                            "VALUES (?, ?, ?, ?)",
                            (cur.lastrowid, chunk["chunk_text"],
                             domain, chunk["source_file"]))
                    except Exception as e:
                        logger.warning(f"FTS5 insert failed: {e}")

                # Determine effective retrieval mode based on what we stored
                if has_embeddings:
                    effective_mode = self._resolve_mode(domain)
                else:
                    effective_mode = "fts"

                wall_ms = int((time.time() - t0) * 1000)
                model_name = (self._embedder.model_name
                              if self._embedder else "none")
                self._db._conn.execute("""
                    UPDATE thrall_knowledge_meta
                    SET chunk_count = ?, ingestion_status = 'ready',
                        embedding_model = ?, retrieval_mode = ?
                    WHERE domain = ? AND wing = ?
                """, (len(all_chunks), model_name,
                      effective_mode, domain, wing))

                self._db._conn.commit()

                logger.info(
                    f"KNOWLEDGE_INGESTED domain={domain} wing={wing} "
                    f"chunks={len(all_chunks)} version={pack['version']} "
                    f"mode={effective_mode} wall_ms={wall_ms}")

                self._maybe_load_recipes(pack)

            except Exception as e:
                logger.error(
                    f"KNOWLEDGE_INGEST_FAILED domain={domain}: {e}")
                try:
                    self._db._conn.rollback()
                except Exception:
                    pass
                # Mark as failed without committing partial data
                try:
                    self._db._conn.execute("""
                        UPDATE thrall_knowledge_meta
                        SET ingestion_status = 'failed'
                        WHERE domain = ? AND wing = ?
                    """, (domain, wing))
                    self._db._conn.commit()
                except Exception:
                    pass

    def _maybe_load_recipes(self, pack: dict):
        domain = pack["domain"]
        trust = self._get_trust_level(domain)
        if trust == "none" or not pack.get("recipe"):
            return

    # ── Query: mode dispatch ──

    def query_knowledge(self, domain: str, query_text: str,
                        top_k: int = 5, source_file: str = None,
                        section: str = None, wing: str = None) -> str:
        """Retrieve relevant chunks using the configured retrieval mode.

        Supports structural scoping: wing, domain, source_file, section.
        """
        w = wing or self._wing
        meta = self._db._conn.execute(
            "SELECT ingestion_status, embedding_model FROM "
            "thrall_knowledge_meta WHERE domain = ? AND wing = ?",
            (domain, w)).fetchone()
        if not meta:
            return ""
        if meta["ingestion_status"] in ("pending", "ingesting", "failed"):
            return ""

        # Build scope filter (always uses k. alias for thrall_knowledge)
        scope_where = "k.domain = ?"
        scope_params: list = [domain]
        if w:
            scope_where += " AND k.wing = ?"
            scope_params.append(w)
        if source_file:
            scope_where += " AND k.source_file = ?"
            scope_params.append(source_file)
        if section:
            scope_where += " AND k.section = ?"
            scope_params.append(section)

        mode = self._resolve_mode(domain)
        if mode == "fts":
            return self._query_fts(scope_where, scope_params,
                                   query_text, top_k)
        elif mode == "vec":
            return self._query_vec(scope_where, scope_params,
                                   query_text, top_k)
        elif mode == "hybrid":
            return self._query_rrf(scope_where, scope_params,
                                   query_text, top_k)
        elif mode == "rerank":
            return self._query_rerank(scope_where, scope_params,
                                      query_text, top_k)
        else:
            return self._query_fts(scope_where, scope_params,
                                   query_text, top_k)

    # ── FTS5 retrieval ──

    def _query_fts(self, scope_where, scope_params, query_text, top_k):
        keywords = self._extract_keywords(query_text)
        if not keywords:
            rows = self._db._conn.execute(f"""
                SELECT k.id, k.chunk_text, k.source_file
                FROM thrall_knowledge k
                WHERE {scope_where} ORDER BY k.chunk_index LIMIT ?
            """, scope_params + [top_k]).fetchall()
        else:
            try:
                # FTS5 JOIN: filter on k. columns, MATCH on f.chunk_text
                rows = self._db._conn.execute(f"""
                    SELECT k.id, k.chunk_text, k.source_file
                    FROM thrall_knowledge_fts f
                    JOIN thrall_knowledge k ON f.rowid = k.id
                    WHERE {scope_where} AND f.chunk_text MATCH ?
                    LIMIT ?
                """, scope_params + [keywords, top_k]).fetchall()
            except Exception:
                rows = self._db._conn.execute(f"""
                    SELECT k.id, k.chunk_text, k.source_file
                    FROM thrall_knowledge k
                    WHERE {scope_where} ORDER BY k.chunk_index LIMIT ?
                """, scope_params + [top_k]).fetchall()
        return self._format_chunks(rows)

    def _query_fts_ranked(self, scope_where, scope_params,
                          query_text, top_k):
        """Return ordered list of (id, chunk_text) from FTS5."""
        keywords = self._extract_keywords(query_text)
        if not keywords:
            return []
        try:
            rows = self._db._conn.execute(f"""
                SELECT k.id, k.chunk_text
                FROM thrall_knowledge_fts f
                JOIN thrall_knowledge k ON f.rowid = k.id
                WHERE {scope_where} AND f.chunk_text MATCH ?
                ORDER BY f.rank LIMIT ?
            """, scope_params + [keywords, top_k]).fetchall()
            return rows
        except Exception:
            return []

    # ── VEC retrieval ──

    def _query_vec(self, scope_where, scope_params, query_text, top_k):
        if not self._embedder or not self._vec_available:
            return self._query_fts(scope_where, scope_params,
                                   query_text, top_k)
        try:
            vec_list = self._embedder.embed(query_text)
            query_vec = struct.pack(f'{len(vec_list)}f', *vec_list)
            rows = self._db._conn.execute(f"""
                SELECT k.id, k.chunk_text, k.source_file
                FROM thrall_knowledge k
                JOIN thrall_knowledge_vec v ON k.id = v.id
                WHERE {scope_where}
                ORDER BY vec_distance_cosine(v.embedding, ?)
                LIMIT ?
            """, scope_params + [query_vec, top_k]).fetchall()
            if not rows:
                return self._query_fts(scope_where, scope_params,
                                       query_text, top_k)
            return self._format_chunks(rows)
        except Exception as e:
            logger.warning(f"Vec query failed, falling back to FTS5: {e}")
            return self._query_fts(scope_where, scope_params,
                                   query_text, top_k)

    def _query_vec_ranked(self, scope_where, scope_params,
                          query_text, top_k):
        """Return ordered list of (id, chunk_text) from VEC."""
        if not self._embedder or not self._vec_available:
            return []
        try:
            vec_list = self._embedder.embed(query_text)
            query_vec = struct.pack(f'{len(vec_list)}f', *vec_list)
            rows = self._db._conn.execute(f"""
                SELECT k.id, k.chunk_text
                FROM thrall_knowledge k
                JOIN thrall_knowledge_vec v ON k.id = v.id
                WHERE {scope_where}
                ORDER BY vec_distance_cosine(v.embedding, ?)
                LIMIT ?
            """, scope_params + [query_vec, top_k]).fetchall()
            return rows
        except Exception:
            return []

    # ── RRF hybrid retrieval ──

    def _query_rrf(self, scope_where, scope_params, query_text, top_k):
        """Reciprocal Rank Fusion: merge FTS + VEC ranked lists."""
        broad_k = top_k * 4
        fts_rows = self._query_fts_ranked(
            scope_where, scope_params, query_text, broad_k)
        vec_rows = self._query_vec_ranked(
            scope_where, scope_params, query_text, broad_k)

        if not fts_rows and not vec_rows:
            return ""
        if not vec_rows:
            return self._format_chunks(fts_rows[:top_k])
        if not fts_rows:
            return self._format_chunks(vec_rows[:top_k])

        # RRF scoring (1-based rank per standard formula)
        k = self._rrf_k
        rrf_scores = {}
        chunk_map = {}
        for rank, row in enumerate(fts_rows, 1):
            rid = row[0] if isinstance(row, (tuple, list)) else row["id"]
            rrf_scores[rid] = rrf_scores.get(rid, 0) + 1.0 / (k + rank)
            chunk_map[rid] = row
        for rank, row in enumerate(vec_rows, 1):
            rid = row[0] if isinstance(row, (tuple, list)) else row["id"]
            rrf_scores[rid] = rrf_scores.get(rid, 0) + 1.0 / (k + rank)
            chunk_map[rid] = row

        ranked = sorted(rrf_scores.items(), key=lambda x: -x[1])[:top_k]
        result_rows = [chunk_map[rid] for rid, _ in ranked]
        return self._format_chunks(result_rows)

    # ── Rerank retrieval ──

    def _query_rerank(self, scope_where, scope_params, query_text, top_k):
        """FTS broad recall, reranked by embedding cosine similarity."""
        n_cand = self._rerank_candidates
        fts_rows = self._query_fts_ranked(
            scope_where, scope_params, query_text, n_cand)

        if not fts_rows or not self._embedder:
            return self._format_chunks(fts_rows[:top_k])

        try:
            query_vec = self._embedder.embed(query_text)
        except Exception:
            return self._format_chunks(fts_rows[:top_k])

        scored = []
        for row in fts_rows:
            rid = row[0] if isinstance(row, (tuple, list)) else row["id"]
            stored = self._get_stored_embedding(rid)
            if stored:
                sim = self._cosine_sim(query_vec, stored)
                scored.append((row, sim))
            else:
                scored.append((row, 0.0))

        scored.sort(key=lambda x: -x[1])
        return self._format_chunks([r for r, _ in scored[:top_k]])

    def _get_stored_embedding(self, chunk_id: int) -> Optional[List[float]]:
        row = self._db._conn.execute(
            "SELECT embedding FROM thrall_knowledge WHERE id = ?",
            (chunk_id,)).fetchone()
        if not row or not row[0]:
            return None
        blob = row[0]
        n = len(blob) // 4
        return list(struct.unpack(f'{n}f', blob))

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a < 1e-8 or norm_b < 1e-8:
            return 0.0
        return dot / (norm_a * norm_b)

    # ── Helpers ──

    def _format_chunks(self, rows) -> str:
        if not rows:
            return ""
        parts = []
        for row in rows:
            text = row[1] if isinstance(row, (tuple, list)) else row["chunk_text"]
            parts.append(text)
        return "\n\n---\n\n".join(parts)

    def _extract_keywords(self, text: str) -> str:
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
        stops = {"the", "and", "for", "are", "but", "not", "you",
                 "all", "can", "had", "her", "was", "one", "our",
                 "out", "has", "have", "this", "that", "with", "from",
                 "what", "which", "when", "where", "how", "does"}
        keywords = [w for w in words if w not in stops][:10]
        return " OR ".join(keywords) if keywords else ""

    # ── Cleanup ──

    def remove_domain(self, domain: str, wing: str = None) -> dict:
        w = wing or self._wing
        ids = [r[0] for r in self._db._conn.execute(
            "SELECT id FROM thrall_knowledge WHERE domain = ? AND wing = ?",
            (domain, w)).fetchall()]

        with self._lock:
            if self._vec_available and ids:
                try:
                    ph = ",".join("?" * len(ids))
                    self._db._conn.execute(
                        f"DELETE FROM thrall_knowledge_vec "
                        f"WHERE id IN ({ph})", ids)
                except Exception:
                    pass

            if ids:
                try:
                    ph = ",".join("?" * len(ids))
                    self._db._conn.execute(
                        f"DELETE FROM thrall_knowledge_fts "
                        f"WHERE rowid IN ({ph})", ids)
                except Exception:
                    pass

            self._db._conn.execute(
                "DELETE FROM thrall_knowledge "
                "WHERE domain = ? AND wing = ?", (domain, w))
            self._db._conn.execute(
                "DELETE FROM thrall_knowledge_meta "
                "WHERE domain = ? AND wing = ?", (domain, w))
            self._db._conn.commit()

        # Only remove files if no other wing uses this domain
        remaining = self._db._conn.execute(
            "SELECT COUNT(*) FROM thrall_knowledge WHERE domain = ?",
            (domain,)).fetchone()[0]
        if remaining == 0:
            domain_dir = os.path.join(self._storage_dir,
                                      _sanitize_filename(domain))
            if os.path.isdir(domain_dir):
                import shutil
                shutil.rmtree(domain_dir, ignore_errors=True)

        logger.info(f"KNOWLEDGE_REMOVED domain={domain} wing={w}")
        return {"status": "removed", "domain": domain}

    def list_domains(self) -> list:
        rows = self._db._conn.execute(
            "SELECT domain, wing, version, description, chunk_count, "
            "ingestion_status, retrieval_mode, acquired_at "
            "FROM thrall_knowledge_meta"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_domain_status(self, domain: str) -> Optional[dict]:
        row = self._db._conn.execute(
            "SELECT * FROM thrall_knowledge_meta WHERE domain = ? AND wing = ?",
            (domain, self._wing)).fetchone()
        return dict(row) if row else None

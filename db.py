"""Thrall Switchboard — Database layer.

Three tables:
- thrall_journal: every pipeline execution (audit + training + dryrun)
- thrall_context: async workflow state (session continuations, flags)
- thrall_recipes: runtime cache of loaded recipe configs
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ThrallDB:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self):
        c = self._conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS thrall_journal (
                id              INTEGER PRIMARY KEY,
                timestamp       REAL NOT NULL,
                pipeline        TEXT NOT NULL,
                session_id      TEXT,
                envelope_json   TEXT NOT NULL,
                filter_json     TEXT,
                eval_type       TEXT,
                eval_result     TEXT,
                action_name     TEXT,
                action_trace    TEXT,
                context_written TEXT,
                wall_ms         INTEGER,
                mode            TEXT DEFAULT 'automated',
                reviewed        INTEGER DEFAULT 0,
                correction      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_journal_pipeline ON thrall_journal(pipeline);
            CREATE INDEX IF NOT EXISTS idx_journal_ts ON thrall_journal(timestamp);
            CREATE INDEX IF NOT EXISTS idx_journal_review ON thrall_journal(reviewed);

            CREATE TABLE IF NOT EXISTS thrall_context (
                session_id  TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT,
                created_at  REAL NOT NULL,
                expires_at  REAL,
                PRIMARY KEY (session_id, key)
            );
            CREATE INDEX IF NOT EXISTS idx_context_expires ON thrall_context(expires_at);

            CREATE TABLE IF NOT EXISTS thrall_recipes (
                name        TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                source_file TEXT,
                loaded_at   REAL NOT NULL,
                mode        TEXT DEFAULT 'automated'
            );

            CREATE TABLE IF NOT EXISTS thrall_compilation (
                id          INTEGER PRIMARY KEY,
                buffer_name TEXT NOT NULL,
                entry_json  TEXT NOT NULL,
                pipeline    TEXT NOT NULL,
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_comp_buffer ON thrall_compilation(buffer_name);

            CREATE TABLE IF NOT EXISTS thrall_wallet_spend (
                id          INTEGER PRIMARY KEY,
                timestamp   REAL NOT NULL,
                amount      REAL NOT NULL,
                reference   TEXT DEFAULT '',
                peer_pk     TEXT DEFAULT '',
                description TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_ts ON thrall_wallet_spend(timestamp);
        """)

        # Structured memory for decision tracking (v3.8)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS thrall_memory (
                id          INTEGER PRIMARY KEY,
                timestamp   REAL NOT NULL,
                skill       TEXT NOT NULL DEFAULT '',
                node_id     TEXT NOT NULL DEFAULT '',
                outcome     TEXT NOT NULL DEFAULT '',
                mail_id     TEXT DEFAULT '',
                amount      REAL DEFAULT 0.0,
                reasoning   TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '',
                dryrun      INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_memory_skill ON thrall_memory(skill);
            CREATE INDEX IF NOT EXISTS idx_memory_node ON thrall_memory(node_id);
            CREATE INDEX IF NOT EXISTS idx_memory_ts ON thrall_memory(timestamp);
        """)

        # Circuit breaker state (v3.9 T2)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS thrall_circuit_breaker (
                peer_id     TEXT NOT NULL,
                operation   TEXT NOT NULL,
                consecutive_failures INTEGER DEFAULT 0,
                last_failure_at     REAL,
                backoff_until       REAL,
                backoff_level       INTEGER DEFAULT 0,
                PRIMARY KEY (peer_id, operation)
            );
        """)

        # Migration: add from_node column for fast rate-limit / cache queries
        try:
            c.execute("ALTER TABLE thrall_journal ADD COLUMN from_node TEXT DEFAULT ''")
            c.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        c.commit()

        # Knowledge-as-a-Service (v3.10) + wing scoping (v3.11.1)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS thrall_knowledge (
                id          INTEGER PRIMARY KEY,
                domain      TEXT NOT NULL,
                wing        TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text  TEXT NOT NULL,
                section     TEXT NOT NULL DEFAULT '',
                embedding   BLOB,
                version     TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                UNIQUE(wing, domain, source_file, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_domain
                ON thrall_knowledge(domain);
            CREATE INDEX IF NOT EXISTS idx_knowledge_wing
                ON thrall_knowledge(wing);
            CREATE INDEX IF NOT EXISTS idx_knowledge_wing_domain
                ON thrall_knowledge(wing, domain);

            CREATE TABLE IF NOT EXISTS thrall_knowledge_meta (
                domain           TEXT NOT NULL,
                wing             TEXT NOT NULL DEFAULT '',
                version          TEXT NOT NULL,
                description      TEXT,
                author_node      TEXT,
                author_pubkey    TEXT,
                trust_level      TEXT DEFAULT 'none',
                acquired_at      TEXT NOT NULL,
                file_count       INTEGER,
                chunk_count      INTEGER,
                ingestion_status TEXT DEFAULT 'pending',
                embedding_model  TEXT DEFAULT '',
                retrieval_mode   TEXT DEFAULT '',
                PRIMARY KEY (wing, domain)
            );
        """)

        # FTS5 index — always created
        try:
            c.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS thrall_knowledge_fts
                USING fts5(
                    chunk_text, domain, source_file,
                    content='thrall_knowledge', content_rowid='id'
                );
            """)
        except Exception as e:
            logger.warning(f"FTS5 index creation failed: {e}")

        # Migration: add columns to existing knowledge tables (idempotent)
        for tbl, col, default in [
            ("thrall_knowledge", "wing", "''"),
            ("thrall_knowledge", "section", "''"),
            ("thrall_knowledge_meta", "wing", "''"),
            ("thrall_knowledge_meta", "retrieval_mode", "''"),
        ]:
            try:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {default}")
                c.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    # ── Journal ──

    def write_journal(self, pipeline: str, envelope: dict, filter_result: dict,
                      eval_type: str, eval_result: str, action_name: str,
                      action_trace: str, context_written: dict,
                      wall_ms: int, mode: str, session_id: str = None) -> int:
        from_node = envelope.get("from_node", "")[:16]
        cur = self._conn.execute("""
            INSERT INTO thrall_journal
                (timestamp, pipeline, session_id, envelope_json, filter_json,
                 eval_type, eval_result, action_name, action_trace,
                 context_written, wall_ms, mode, from_node)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(), pipeline, session_id,
            json.dumps(envelope), json.dumps(filter_result),
            eval_type, eval_result, action_name, action_trace,
            json.dumps(context_written) if context_written else None,
            wall_ms, mode, from_node,
        ))
        self._conn.commit()
        return cur.lastrowid

    # ── Rate-limit & LLM cache queries ──

    def count_recent_from_sender(self, from_node_prefix: str, pipeline: str,
                                  window_seconds: float) -> int:
        """Count journal entries from a sender within a time window."""
        cutoff = time.time() - window_seconds
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM thrall_journal "
            "WHERE from_node = ? AND pipeline = ? AND timestamp > ?",
            (from_node_prefix[:16], pipeline, cutoff),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_cached_eval(self, from_node_prefix: str, pipeline: str,
                        ttl_seconds: float) -> Optional[dict]:
        """Find the most recent LLM eval result for a sender within TTL.

        Returns {"action": "...", "reason": "..."} or None.
        """
        cutoff = time.time() - ttl_seconds
        row = self._conn.execute(
            "SELECT action_name, eval_result, timestamp FROM thrall_journal "
            "WHERE from_node = ? AND pipeline = ? AND eval_type = 'llm' "
            "AND timestamp > ? ORDER BY id DESC LIMIT 1",
            (from_node_prefix[:16], pipeline, cutoff),
        ).fetchone()
        if not row:
            return None
        return {
            "action": row["action_name"],
            "eval_result": row["eval_result"],
            "timestamp": row["timestamp"],
        }

    def query_journal(self, pipeline: str = None, reviewed: int = None,
                      limit: int = 50, since: float = None) -> List[dict]:
        sql = "SELECT * FROM thrall_journal WHERE 1=1"
        params = []
        if pipeline:
            sql += " AND pipeline = ?"
            params.append(pipeline)
        if reviewed is not None:
            sql += " AND reviewed = ?"
            params.append(reviewed)
        if since:
            sql += " AND timestamp > ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def mark_reviewed(self, journal_id: int, reviewed: int, correction: str = None):
        self._conn.execute(
            "UPDATE thrall_journal SET reviewed=?, correction=? WHERE id=?",
            (reviewed, correction, journal_id))
        self._conn.commit()

    # ── Context ──

    def set_context(self, session_id: str, key: str, value: str,
                    ttl_seconds: float = None):
        expires = time.time() + ttl_seconds if ttl_seconds else None
        self._conn.execute("""
            INSERT OR REPLACE INTO thrall_context (session_id, key, value, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, key, value, time.time(), expires))
        self._conn.commit()

    def get_context(self, session_id: str, key: str = None) -> dict:
        if key:
            row = self._conn.execute(
                "SELECT value FROM thrall_context WHERE session_id=? AND key=? AND (expires_at IS NULL OR expires_at > ?)",
                (session_id, key, time.time())).fetchone()
            return {"value": row["value"]} if row else {}
        rows = self._conn.execute(
            "SELECT key, value FROM thrall_context WHERE session_id=? AND (expires_at IS NULL OR expires_at > ?)",
            (session_id, time.time())).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def clear_context(self, session_id: str, key: str = None):
        if key:
            self._conn.execute(
                "DELETE FROM thrall_context WHERE session_id=? AND key=?",
                (session_id, key))
        else:
            self._conn.execute(
                "DELETE FROM thrall_context WHERE session_id=?", (session_id,))
        self._conn.commit()

    def cleanup_expired_context(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM thrall_context WHERE expires_at IS NOT NULL AND expires_at < ?",
            (time.time(),))
        self._conn.commit()
        return cur.rowcount

    # ── Recipes ──

    def upsert_recipe(self, name: str, config: dict, source_file: str = None,
                      mode: str = "automated"):
        self._conn.execute("""
            INSERT OR REPLACE INTO thrall_recipes (name, config_json, source_file, loaded_at, mode)
            VALUES (?, ?, ?, ?, ?)
        """, (name, json.dumps(config), source_file, time.time(), mode))
        self._conn.commit()

    def get_recipe(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM thrall_recipes WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d["config_json"])
        return d

    def prune_recipes(self, keep_names: List[str]) -> int:
        """Delete DB rows for recipes not in keep_names. Returns count pruned."""
        if not keep_names:
            return 0
        placeholders = ",".join("?" for _ in keep_names)
        cur = self._conn.execute(
            f"DELETE FROM thrall_recipes WHERE name NOT IN ({placeholders})",
            keep_names)
        self._conn.commit()
        return cur.rowcount

    def get_all_recipes(self) -> List[dict]:
        rows = self._conn.execute("SELECT * FROM thrall_recipes").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d["config_json"])
            result.append(d)
        return result

    # ── Compilation buffer ──

    def add_to_buffer(self, buffer_name: str, entry: dict, pipeline: str) -> int:
        cur = self._conn.execute("""
            INSERT INTO thrall_compilation (buffer_name, entry_json, pipeline, created_at)
            VALUES (?, ?, ?, ?)
        """, (buffer_name, json.dumps(entry), pipeline, time.time()))
        self._conn.commit()
        return cur.lastrowid

    def get_buffer(self, buffer_name: str) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM thrall_compilation WHERE buffer_name=? ORDER BY created_at",
            (buffer_name,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["entry"] = json.loads(d["entry_json"])
            result.append(d)
        return result

    def buffer_count(self, buffer_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM thrall_compilation WHERE buffer_name=?",
            (buffer_name,)).fetchone()
        return row["cnt"]

    def flush_buffer(self, buffer_name: str) -> List[dict]:
        entries = self.get_buffer(buffer_name)
        self._conn.execute(
            "DELETE FROM thrall_compilation WHERE buffer_name=?", (buffer_name,))
        self._conn.commit()
        return entries

    # ── Wallet ──

    def get_daily_spend(self, day_start: float) -> float:
        """Sum all wallet spending since day_start (midnight UTC)."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM thrall_wallet_spend "
            "WHERE timestamp >= ?", (day_start,)
        ).fetchone()
        return row["total"] if row else 0.0

    def record_wallet_spend(self, amount: float, reference: str = "",
                            peer_pk: str = "", description: str = ""):
        """Record a spending event."""
        self._conn.execute("""
            INSERT INTO thrall_wallet_spend (timestamp, amount, reference, peer_pk, description)
            VALUES (?, ?, ?, ?, ?)
        """, (time.time(), amount, reference, peer_pk, description))
        self._conn.commit()

    def get_wallet_history(self, since: float = None, limit: int = 50) -> List[dict]:
        """Return wallet spending records since timestamp (default: last 24h)."""
        if since is None:
            since = time.time() - 86400
        rows = self._conn.execute(
            "SELECT * FROM thrall_wallet_spend WHERE timestamp >= ? "
            "ORDER BY id DESC LIMIT ?", (since, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Structured Memory ──

    def record_memory(self, skill: str, node_id: str, outcome: str,
                      mail_id: str = "", amount: float = 0.0,
                      reasoning: str = "", metadata: dict = None,
                      dryrun: bool = False) -> int:
        """Record a decision outcome in structured memory."""
        meta_json = json.dumps(metadata) if metadata else ""
        cur = self._conn.execute("""
            INSERT INTO thrall_memory
                (timestamp, skill, node_id, outcome, mail_id, amount,
                 reasoning, metadata_json, dryrun)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(), skill, node_id[:16], outcome,
            mail_id, amount, reasoning[:500], meta_json,
            1 if dryrun else 0,
        ))
        self._conn.commit()
        return cur.lastrowid

    def query_memory(self, skill: str = None, node_id: str = None,
                     outcome: str = None, limit: int = 10,
                     since: float = None,
                     include_dryrun: bool = False) -> List[dict]:
        """Query structured memory with flexible filters."""
        sql = "SELECT * FROM thrall_memory WHERE 1=1"
        params: list = []
        if not include_dryrun:
            sql += " AND dryrun = 0"
        if skill:
            sql += " AND skill = ?"
            params.append(skill)
        if node_id:
            sql += " AND node_id = ?"
            params.append(node_id[:16])
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        if since:
            sql += " AND timestamp > ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("metadata_json"):
                try:
                    d["metadata"] = json.loads(d["metadata_json"])
                except (json.JSONDecodeError, ValueError):
                    d["metadata"] = {}
            else:
                d["metadata"] = {}
            result.append(d)
        return result

    # ── Circuit Breaker ──

    def get_circuit(self, peer_id: str, operation: str) -> Optional[dict]:
        """Get circuit breaker state for a peer+operation."""
        row = self._conn.execute(
            "SELECT * FROM thrall_circuit_breaker WHERE peer_id=? AND operation=?",
            (peer_id[:16], operation)).fetchone()
        return dict(row) if row else None

    def upsert_circuit(self, peer_id: str, operation: str,
                       consecutive_failures: int, last_failure_at: float,
                       backoff_until: float, backoff_level: int):
        """Insert or update circuit breaker state."""
        self._conn.execute("""
            INSERT OR REPLACE INTO thrall_circuit_breaker
                (peer_id, operation, consecutive_failures, last_failure_at,
                 backoff_until, backoff_level)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (peer_id[:16], operation, consecutive_failures,
              last_failure_at, backoff_until, backoff_level))
        self._conn.commit()

    def reset_circuit(self, peer_id: str, operation: str):
        """Reset circuit to closed (success resets failures)."""
        self._conn.execute(
            "DELETE FROM thrall_circuit_breaker WHERE peer_id=? AND operation=?",
            (peer_id[:16], operation))
        self._conn.commit()

    def get_all_circuits(self) -> List[dict]:
        """Get all circuit breaker entries (diagnostics)."""
        rows = self._conn.execute(
            "SELECT * FROM thrall_circuit_breaker ORDER BY backoff_until DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ──

    def get_stats(self, since: float, pipeline: str = None) -> list:
        """Aggregate pipeline stats for the agent.

        Returns list of dicts with action_name, eval_type, count,
        avg_wall_ms, llm_ms_total — grouped by action+eval_type.
        """
        where = "WHERE timestamp > ?"
        params: list = [since]
        if pipeline:
            where += " AND pipeline = ?"
            params.append(pipeline)

        rows = self._conn.execute(f"""
            SELECT
                action_name,
                eval_type,
                COUNT(*) as count,
                CAST(AVG(wall_ms) AS INTEGER) as avg_wall_ms,
                SUM(CASE WHEN eval_type = 'llm' THEN wall_ms ELSE 0 END) as llm_ms_total
            FROM thrall_journal
            {where}
            GROUP BY action_name, eval_type
            ORDER BY count DESC
        """, params).fetchall()
        return [dict(r) for r in rows]

    def get_totals(self, since: float) -> dict:
        """Quick totals for stats summary line."""
        row = self._conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN eval_type='llm' THEN 1 ELSE 0 END) as llm_calls, "
            "SUM(CASE WHEN eval_type='cache' THEN 1 ELSE 0 END) as cache_hits, "
            "SUM(CASE WHEN eval_type='bypass' THEN 1 ELSE 0 END) as bypasses, "
            "SUM(CASE WHEN eval_type='hotwire' THEN 1 ELSE 0 END) as hotwire, "
            "SUM(CASE WHEN eval_type='rate_limit' THEN 1 ELSE 0 END) as rate_limited "
            "FROM thrall_journal WHERE timestamp > ?", (since,)
        ).fetchone()
        return dict(row) if row else {}

    # ── Pruning ──

    def prune_journal(self, max_age_seconds: int = 259200) -> int:
        """Delete journal entries older than max_age. Default 3 days."""
        cutoff = time.time() - max_age_seconds
        cur = self._conn.execute(
            "DELETE FROM thrall_journal WHERE timestamp < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def prune_memory(self, max_age_seconds: int = 604800) -> int:
        """Delete memory entries older than max_age. Default 7 days."""
        cutoff = time.time() - max_age_seconds
        cur = self._conn.execute(
            "DELETE FROM thrall_memory WHERE timestamp < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def prune_wallet_spend(self, max_age_seconds: int = 604800) -> int:
        """Delete wallet spend entries older than max_age. Default 7 days."""
        cutoff = time.time() - max_age_seconds
        cur = self._conn.execute(
            "DELETE FROM thrall_wallet_spend WHERE timestamp < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def prune_compilation(self, max_age_seconds: int = 86400) -> int:
        """Delete compilation buffer entries older than max_age. Default 1 day."""
        cutoff = time.time() - max_age_seconds
        cur = self._conn.execute(
            "DELETE FROM thrall_compilation WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def close(self):
        self._conn.close()

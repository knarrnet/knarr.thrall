"""Thrall Switchboard — Structured memory + Living Memory Pillars.

Two layers:
1. **Structured memory** (thrall_memory table) — classified decision records
   for targeted recall. Query by skill, node_id, outcome.
2. **Living memory pillars** (rag/90-93-memory-*.md) — agent-maintained RAG
   documents that accumulate operational wisdom across sessions. Survives
   context resets, provides strategic continuity.

Pillar domains:
- operations (90): what worked/failed, operational patterns
- peers (91): CRM — peer behavior, reliability, preferences
- strategy (92): strategic decisions, rationale, outcomes
- felag (93): group agreements, policies, shared context
"""

import logging
import os
import time
from typing import Dict, List, Optional

from db import ThrallDB

logger = logging.getLogger("thrall.memory")

# ── Living Memory Pillar domains ─────────────────────────────────────

MEMORY_DOMAINS = {
    "operations": "90-memory-operations.md",
    "peers": "91-memory-peers.md",
    "strategy": "92-memory-strategy.md",
    "felag": "93-memory-felag.md",
}


class ThrallMemory:
    """Structured decision memory for thrall recipes."""

    def __init__(self, db: ThrallDB):
        self._db = db

    def record(self, skill: str, node_id: str, outcome: str,
               mail_id: str = "", amount: float = 0.0,
               reasoning: str = "", metadata: dict = None,
               dryrun: bool = False) -> int:
        """Record a decision outcome.

        Returns the row ID of the inserted record.
        """
        row_id = self._db.record_memory(
            skill=skill, node_id=node_id, outcome=outcome,
            mail_id=mail_id, amount=amount, reasoning=reasoning,
            metadata=metadata, dryrun=dryrun)
        logger.debug(f"MEMORY_RECORD skill={skill} peer={node_id[:16]} "
                     f"outcome={outcome} amount={amount:.1f} "
                     f"{'[dryrun]' if dryrun else ''}")
        return row_id

    def query(self, node_id: str = None, skill: str = None,
              outcome: str = None, limit: int = 10,
              since: float = None, include_dryrun: bool = False) -> List[dict]:
        """Query memory with flexible filters."""
        return self._db.query_memory(
            node_id=node_id, skill=skill, outcome=outcome,
            limit=limit, since=since, include_dryrun=include_dryrun)

    def get_peer_summary(self, node_id: str, skill: str = None,
                         days: int = 7) -> dict:
        """Aggregated peer interaction summary."""
        since = time.time() - (days * 86400)
        records = self.query(node_id=node_id, skill=skill,
                             since=since, limit=100)

        if not records:
            return {"total": 0, "outcomes": {}}

        outcomes: Dict[str, int] = {}
        total_amount = 0.0
        for r in records:
            o = r["outcome"]
            outcomes[o] = outcomes.get(o, 0) + 1
            total_amount += r.get("amount", 0)

        return {
            "total": len(records),
            "outcomes": outcomes,
            "total_amount": round(total_amount, 2),
            "last_interaction": records[0]["timestamp"],
            "skills": list(set(r["skill"] for r in records)),
        }

    def format_for_prompt(self, node_id: str = None, skill: str = None,
                          limit: int = 5) -> str:
        """Format memory as text for LLM prompt injection."""
        records = self.query(node_id=node_id, skill=skill, limit=limit)
        if not records:
            return "No prior interactions recorded."

        lines = [f"Recent interactions ({len(records)} records):"]
        for r in records:
            ts = time.strftime("%m-%d %H:%M", time.gmtime(r["timestamp"]))
            peer = r["node_id"][:12] if r["node_id"] else "?"
            lines.append(
                f"  {ts} | {r['skill']} | peer={peer} | "
                f"outcome={r['outcome']} | amount={r.get('amount', 0):.1f}"
            )
            if r.get("reasoning"):
                lines.append(f"    reason: {r['reasoning'][:80]}")
        return "\n".join(lines)


# ── Circuit Breaker ──────────────────────────────────────────────────


class CircuitBreaker:
    """Per-peer, per-operation circuit breaker with exponential backoff.

    States: CLOSED (normal) → OPEN (skip peer) → HALF_OPEN (probe).
    Persists to SQLite via ThrallDB.

    Config defaults:
        failure_threshold = 3
        backoff_seconds = 3600   (1h, doubles each level)
        max_backoff_seconds = 86400  (24h cap)
    """

    def __init__(self, db: ThrallDB, config: dict = None):
        self._db = db
        cfg = config or {}
        self._threshold = int(cfg.get("failure_threshold", 3))
        self._base_backoff = float(cfg.get("backoff_seconds", 3600))
        self._max_backoff = float(cfg.get("max_backoff_seconds", 86400))

    def is_open(self, peer_id: str, operation: str) -> bool:
        """True if circuit is open (peer should be skipped).

        Returns False (allow) if: no record, below threshold, or backoff expired
        (half-open — allows one probe attempt).
        """
        row = self._db.get_circuit(peer_id, operation)
        if not row:
            return False
        if row["consecutive_failures"] < self._threshold:
            return False
        # Circuit is open — check if backoff has expired (half-open)
        if row["backoff_until"] and time.time() >= row["backoff_until"]:
            return False  # half-open: allow one probe
        return True

    def record_failure(self, peer_id: str, operation: str) -> None:
        """Increment failure count. Opens circuit at threshold."""
        row = self._db.get_circuit(peer_id, operation)
        now = time.time()

        if row:
            failures = row["consecutive_failures"] + 1
            level = row.get("backoff_level", 0)
        else:
            failures = 1
            level = 0

        # Calculate backoff if at or above threshold
        backoff_until = 0.0
        if failures >= self._threshold:
            backoff_secs = min(
                self._base_backoff * (2 ** level),
                self._max_backoff)
            backoff_until = now + backoff_secs
            level += 1
            logger.info(
                f"CIRCUIT_OPEN peer={peer_id[:16]} op={operation} "
                f"failures={failures} backoff={backoff_secs:.0f}s "
                f"until={time.strftime('%H:%M', time.gmtime(backoff_until))}")

        self._db.upsert_circuit(
            peer_id, operation, failures, now, backoff_until, level)

    def record_success(self, peer_id: str, operation: str) -> None:
        """Reset failure count. Closes circuit."""
        row = self._db.get_circuit(peer_id, operation)
        if row:
            if row["consecutive_failures"] >= self._threshold:
                logger.info(
                    f"CIRCUIT_CLOSED peer={peer_id[:16]} op={operation} "
                    f"(was open with {row['consecutive_failures']} failures)")
            self._db.reset_circuit(peer_id, operation)

    def get_status(self, peer_id: str, operation: str) -> dict:
        """Return circuit state for diagnostics."""
        row = self._db.get_circuit(peer_id, operation)
        if not row:
            return {"state": "closed", "failures": 0, "backoff_until": None}

        now = time.time()
        failures = row["consecutive_failures"]
        if failures < self._threshold:
            state = "closed"
        elif row["backoff_until"] and now >= row["backoff_until"]:
            state = "half_open"
        else:
            state = "open"

        return {
            "state": state,
            "failures": failures,
            "backoff_until": row["backoff_until"],
            "backoff_level": row.get("backoff_level", 0),
        }


# ── Living Memory Pillar Writer ──────────────────────────────────────

class MemoryWriter:
    """Append-only writer for living memory RAG pillars.

    Each domain maps to a markdown file in rag/. Entries are timestamped
    and appended. The file is created with a header if missing.
    """

    def __init__(self, rag_dir: str):
        self._rag_dir = rag_dir
        os.makedirs(rag_dir, exist_ok=True)

    def _pillar_path(self, domain: str) -> str:
        filename = MEMORY_DOMAINS.get(domain)
        if not filename:
            raise ValueError(f"Unknown memory domain: {domain}. "
                             f"Valid: {list(MEMORY_DOMAINS.keys())}")
        return os.path.join(self._rag_dir, filename)

    def append(self, domain: str, entry: str, timestamp: float = None):
        """Append a timestamped entry to a memory pillar.

        Creates the file with a header if it doesn't exist.
        """
        path = self._pillar_path(domain)
        ts = timestamp or time.time()
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))

        # Create file with header if missing
        if not os.path.exists(path):
            header = self._make_header(domain)
            with open(path, "w", encoding="utf-8") as f:
                f.write(header)

        # Append entry with timestamp
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## {ts_str}\n{entry}\n")

        logger.info(f"MEMORY_PILLAR {domain}: appended entry ({len(entry)} chars)")

    def read(self, domain: str) -> str:
        """Read full pillar content. Returns empty string if file missing."""
        path = self._pillar_path(domain)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def line_count(self, domain: str) -> int:
        """Count lines in pillar. Returns 0 if file missing."""
        path = self._pillar_path(domain)
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)

    def _make_header(self, domain: str) -> str:
        """Generate initial header for a new pillar file."""
        titles = {
            "operations": "Operational Memory",
            "peers": "Peer Knowledge",
            "strategy": "Strategic Memory",
            "felag": "Félag Memory",
        }
        title = titles.get(domain, domain.title())
        return f"# {title}\n\n"

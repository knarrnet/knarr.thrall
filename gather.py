"""Thrall Switchboard — Context gatherer stage.

Pipeline stage between [filter] and [evaluate]. Two gather modes:

1. **Catalog fields** (v3.7) — Recipe declares `gather = ["field1", "field2"]`.
   Engine looks up fields in the catalog, deduplicates by source, fetches once
   per source, extracts and formats individual fields. Supports glob syntax
   (e.g. `gather = ["economy.*"]`).

2. **Legacy [[gather]] blocks** — Recipe declares `[[gather]]` sections with
   explicit source/endpoint. Still supported for backward compatibility.

Sources (catalog):
    status   — GET /api/status (peer_count, skill_count, uptime, node_version)
    economy  — GET /api/economy (net_position, worst_position, settlement_candidates, ...)
    wallet   — internal wallet.get_status() (daily_spend, budget_remaining, budget_ceiling)
    journal  — internal db query (recent_actions, action_counts)
    peers    — GET /api/peers (peer_list, skill_inventory, peer_skill_gaps)
    probe    — computed, no API call (probe_entropy, probe_unique_peers)

Sources (legacy):
    cockpit   — HTTP GET to cockpit API
    punchhole — Direct in-process read from punchhole-backend private cache
    memory    — Query thrall structured memory
    static    — Literal values
    peers     — GET /api/peers with optional filter/fields/limit
    receipts  — ctx.query_receipts() with optional peer/since/limit
"""

import asyncio
import fnmatch
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("thrall.gather")

# ── Catalog ──────────────────────────────────────────────────────────────

_catalog: Optional[Dict[str, dict]] = None


def load_catalog(plugin_dir: str) -> Dict[str, dict]:
    """Load gather-field-catalog.toml and return {field_name: metadata}."""
    global _catalog
    if _catalog is not None:
        return _catalog

    path = os.path.join(plugin_dir, "gather-field-catalog.toml")
    if not os.path.exists(path):
        logger.warning(f"CATALOG not found: {path}")
        _catalog = {}
        return _catalog

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    fields = raw.get("fields", {})
    # Validate: every field must have 'cost'
    valid = {}
    for name, meta in fields.items():
        if "cost" not in meta:
            logger.warning(f"CATALOG field '{name}' missing 'cost', skipping")
            continue
        valid[name] = meta

    _catalog = valid
    logger.info(f"CATALOG loaded: {len(valid)} fields from {path}")
    return _catalog


def _expand_field_names(names: List[str], catalog: Dict[str, dict]) -> List[str]:
    """Expand glob patterns (e.g. 'economy.*') against catalog field names.

    Fields are matched by their cost attribute for source-glob patterns,
    or by fnmatch against field names for wildcard patterns.
    """
    expanded = []
    for name in names:
        if "*" in name or "?" in name:
            # Try matching against field names first
            matched = [f for f in catalog if fnmatch.fnmatch(f, name)]
            # Also try source-based glob: "economy.*" matches all fields with cost="economy"
            if not matched and "." in name:
                source_prefix = name.split(".")[0]
                matched = [f for f in catalog
                           if catalog[f].get("cost") == source_prefix]
            if not matched:
                logger.warning(f"GATHER glob '{name}' matched 0 fields")
            expanded.extend(matched)
        else:
            expanded.append(name)
    # Deduplicate preserving order
    seen = set()
    result = []
    for f in expanded:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


# ── Source fetchers ──────────────────────────────────────────────────────

def _fetch_source_status(commerce) -> dict:
    """GET /api/status — returns raw JSON. Uses _get_sync (called from thread)."""
    if not commerce:
        return {"error": "no commerce module"}
    return commerce._get_sync("/api/status") or {}


def _fetch_source_economy(commerce) -> dict:
    """GET /api/economy — returns raw JSON. Uses _get_sync (called from thread)."""
    if not commerce:
        return {"error": "no commerce module"}
    return commerce._get_sync("/api/economy") or {}


def _fetch_source_wallet(wallet) -> dict:
    """Internal wallet.get_status()."""
    if not wallet:
        return {"error": "no wallet module"}
    return wallet.get_status()


def _fetch_source_peers(commerce) -> dict:
    """GET /api/peers — returns raw JSON. Uses _get_sync (called from thread)."""
    if not commerce:
        return {"error": "no commerce module"}
    return commerce._get_sync("/api/peers") or {}


def _fetch_source_journal(db, hours: int = 24) -> dict:
    """Query thrall_journal for recent actions + action counts."""
    if not db:
        return {"error": "no db"}
    cutoff = time.time() - (hours * 3600)

    # Recent actions (last 5)
    rows = db.query_journal(limit=5, since=cutoff)
    recent = []
    for r in rows:
        recent.append({
            "timestamp": r.get("timestamp", 0),
            "recipe": r.get("pipeline", ""),
            "action": r.get("action_name", ""),
            "outcome": r.get("eval_type", ""),
        })

    # Action counts by type
    stats = db.get_stats(since=cutoff)
    counts = {}
    for s in stats:
        action = s.get("action_name", "unknown")
        counts[action] = counts.get(action, 0) + s.get("count", 0)

    return {"recent_actions": recent, "action_counts": counts}


def _fetch_source_memory(db, hours: int = 72) -> dict:
    """Query thrall_memory table for settlement and skill call history."""
    if not db:
        return {"error": "no db"}
    cutoff = time.time() - (hours * 3600)

    settlements = db.query_memory(skill="settlement-review", limit=10, since=cutoff)
    return {"recent_settlements": settlements}


def _fetch_source_probe(db, hours: int = 24) -> dict:
    """Computed probe fields — no API call."""
    if not db:
        return {"probe_entropy": 0.0, "probe_unique_peers": 0}
    cutoff = time.time() - (hours * 3600)
    rows = db.query_journal(limit=500, since=cutoff)

    # Action distribution for entropy
    action_counts: Dict[str, int] = {}
    unique_peers: set = set()
    for r in rows:
        action = r.get("action_name", "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1
        from_node = r.get("from_node", "")
        if from_node:
            unique_peers.add(from_node[:16])

    entropy = _shannon_entropy(action_counts)
    return {
        "probe_entropy": round(entropy, 3),
        "probe_unique_peers": len(unique_peers),
    }


def _shannon_entropy(counts: Dict[str, int]) -> float:
    """Shannon entropy of a count distribution."""
    total = sum(counts.values())
    if total == 0:
        return 0.0
    entropy = 0.0
    for c in counts.values():
        if c > 0:
            p = c / total
            entropy -= p * math.log2(p)
    return entropy


# ── Field extraction and formatting ─────────────────────────────────────

def _extract_field(field_name: str, field_meta: dict,
                   source_data: dict) -> str:
    """Extract and format a single field from its source response.

    Returns a formatted string suitable for LLM context.
    """
    fmt = field_meta.get("format", "scalar")
    field_type = field_meta.get("type", "text")
    source = field_meta.get("cost", "")

    # Probe fields are direct keys in the source dict
    if source == "none":
        val = source_data.get(field_name)
        if val is not None:
            return _format_value(val, fmt, field_type)
        return "unavailable"

    # Journal fields
    if source == "journal":
        if field_name == "recent_actions":
            actions = source_data.get("recent_actions", [])
            if not actions:
                return "no recent actions"
            lines = []
            for a in actions:
                lines.append(f"  {a.get('recipe', '?')}: {a.get('action', '?')} ({a.get('outcome', '?')})")
            return "\n".join(lines)
        elif field_name == "action_counts":
            counts = source_data.get("action_counts", {})
            if not counts:
                return "no actions in last 24h"
            lines = [f"  {k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])]
            return "\n".join(lines)
        return str(source_data.get(field_name, "unavailable"))

    # Wallet fields — direct keys from get_status()
    if source == "wallet":
        key_map = {
            "daily_spend": "daily_spent",
            "budget_remaining": "remaining",
            "budget_ceiling": "ceiling",
        }
        key = key_map.get(field_name, field_name)
        val = source_data.get(key)
        if val is not None:
            return _format_value(val, fmt, field_type)
        return "unavailable"

    # Status fields — direct keys
    if source == "status":
        key_map = {
            "peer_count": "peer_count",
            "skill_count": "skill_count",
            "uptime": "uptime",
            "node_version": "version",
        }
        key = key_map.get(field_name, field_name)
        val = source_data.get(key)
        if val is not None:
            return _format_value(val, fmt, field_type)
        return "unavailable"

    # Economy fields — need specific extraction logic
    if source == "economy":
        return _extract_economy_field(field_name, field_meta, source_data)

    # Peers fields
    if source == "peers":
        return _extract_peers_field(field_name, field_meta, source_data)

    # Memory fields
    if source == "memory":
        return _extract_memory_field(field_name, field_meta, source_data)

    return "unavailable"


def _extract_economy_field(field_name: str, field_meta: dict,
                           data: dict) -> str:
    """Extract economy fields from /api/economy response."""
    if isinstance(data, dict) and "error" in data:
        return f"error: {data['error']}"

    # The economy API returns various structures depending on knarr version.
    # Common patterns: {positions: [...], summary: {...}}
    positions = data.get("positions", data.get("ledger", []))
    if isinstance(positions, dict):
        positions = list(positions.values()) if positions else []

    if field_name == "net_position":
        total = sum(float(p.get("balance", 0)) for p in positions) if positions else 0
        return f"{total:.2f}"

    elif field_name == "worst_position":
        if not positions:
            return "no positions"
        worst = min(positions, key=lambda p: float(p.get("balance", 0)))
        pk = worst.get("peer_public_key", worst.get("peer_id", "?"))[:16]
        bal = float(worst.get("balance", 0))
        return f"{pk}: {bal:.2f}"

    elif field_name == "settlement_candidates":
        # Peers where utilization is high (>80%)
        candidates = []
        for p in positions:
            bal = float(p.get("balance", 0))
            # Approximate utilization (negative balance = we owe them)
            if bal < -5.0:  # rough threshold
                pk = p.get("peer_public_key", p.get("peer_id", "?"))[:16]
                candidates.append(f"  {pk}: balance={bal:.2f}")
        return "\n".join(candidates) if candidates else "no settlement candidates"

    elif field_name == "credit_floor_peers":
        floor_peers = []
        for p in positions:
            bal = float(p.get("balance", 0))
            if bal < -10.0:  # near hard limit
                pk = p.get("peer_public_key", p.get("peer_id", "?"))[:16]
                floor_peers.append(f"  {pk}: balance={bal:.2f}")
        return "\n".join(floor_peers) if floor_peers else "no peers at credit floor"

    elif field_name == "bilateral_summary":
        if not positions:
            return "no bilateral positions"
        lines = []
        for p in positions:
            pk = p.get("peer_public_key", p.get("peer_id", "?"))[:16]
            bal = float(p.get("balance", 0))
            prov = p.get("tasks_provided", p.get("calls_provided", 0))
            cons = p.get("tasks_consumed", p.get("calls_consumed", 0))
            lines.append(f"  {pk}: bal={bal:.2f} prov={prov} cons={cons}")
        return "\n".join(lines)

    elif field_name in ("top_skills_by_revenue", "top_skills_by_cost"):
        # These need receipt/skill economics data which may not be in /api/economy
        return "unavailable (requires skill economics data)"

    return "unavailable"


def _extract_peers_field(field_name: str, field_meta: dict,
                         data: dict) -> str:
    """Extract peers fields from /api/peers response."""
    if isinstance(data, dict) and "error" in data:
        return f"error: {data['error']}"

    peers = data if isinstance(data, list) else data.get("peers", [])

    if field_name == "peer_list":
        if not peers:
            return "no connected peers"
        lines = []
        for p in peers:
            pk = p.get("node_id", p.get("peer_id", "?"))[:16]
            host = p.get("host", "?")
            skills = p.get("skill_count", 0)
            lines.append(f"  {pk}: {host} skills={skills}")
        return "\n".join(lines)

    elif field_name == "skill_inventory":
        lines = []
        for p in peers:
            pk = p.get("node_id", p.get("peer_id", "?"))[:16]
            skills = p.get("skills", [])
            if skills:
                for s in skills[:5]:  # cap at 5 per peer
                    name = s if isinstance(s, str) else s.get("name", "?")
                    lines.append(f"  {pk}: {name}")
        return "\n".join(lines) if lines else "no skills discovered"

    elif field_name == "peer_skill_gaps":
        # Would require comparing our skills vs network skills — simplified
        return "unavailable (requires skill comparison)"

    return "unavailable"


def _extract_memory_field(field_name: str, field_meta: dict,
                           data: dict) -> str:
    """Extract memory fields from thrall_memory query results."""
    if isinstance(data, dict) and "error" in data:
        return f"error: {data['error']}"

    if field_name == "recent_settlements":
        entries = data.get("recent_settlements", [])
        if not entries:
            return "no recent settlements"
        lines = []
        for e in entries:
            ts = time.strftime("%m-%d %H:%M", time.gmtime(e.get("timestamp", 0)))
            peer = e.get("node_id", "?")[:16]
            outcome = e.get("outcome", "?")
            amount = e.get("amount", 0)
            reason = e.get("reasoning", "")[:60]
            lines.append(f"  {ts} {peer}: {outcome} {amount:.1f}cr — {reason}")
        return "\n".join(lines)

    return "unavailable"


def _format_value(val: Any, fmt: str, field_type: str) -> str:
    """Format a value according to catalog format spec."""
    if val is None:
        return "unavailable"

    if "rendered as hours" in fmt:
        try:
            return f"{float(val) / 3600:.1f}h"
        except (ValueError, TypeError):
            return str(val)

    if "2 decimal" in fmt:
        try:
            return f"{float(val):.2f}"
        except (ValueError, TypeError):
            return str(val)

    if "3 decimal" in fmt:
        try:
            return f"{float(val):.3f}"
        except (ValueError, TypeError):
            return str(val)

    return str(val)


# ── Main gatherer class ─────────────────────────────────────────────────

class ContextGatherer:
    """Fetches contextual data for recipe evaluation.

    Two modes:
    - gather_fields(["field1", "field2"]) — catalog-based (v3.7)
    - gather(recipe, envelope) — legacy [[gather]] blocks
    """

    def __init__(self, commerce=None, memory=None, db=None,
                 wallet=None, plugin_dir=None, ctx=None):
        self._commerce = commerce
        self._memory = memory
        self._db = db
        self._wallet = wallet
        self._plugin_dir = plugin_dir
        self._catalog = None
        self._ctx = ctx

    @property
    def catalog(self) -> Dict[str, dict]:
        if self._catalog is None:
            if self._plugin_dir:
                self._catalog = load_catalog(self._plugin_dir)
            else:
                self._catalog = {}
        return self._catalog

    def set_commerce(self, commerce):
        self._commerce = commerce

    def set_memory(self, memory):
        self._memory = memory

    def set_wallet(self, wallet):
        self._wallet = wallet

    def set_db(self, db):
        self._db = db

    def set_ctx(self, ctx):
        self._ctx = ctx

    # ── Catalog-based gather (v3.7) ─────────────────────────────────

    def gather_fields(self, field_names: List[str]) -> Dict[str, str]:
        """Fetch fields by catalog name. Deduplicates by source.

        Returns {field_name: formatted_string}.
        """
        cat = self.catalog
        if not cat:
            logger.warning("GATHER_FIELDS no catalog loaded")
            return {}

        # Expand globs
        expanded = _expand_field_names(field_names, cat)
        if not expanded:
            return {}

        # Group by source (cost attribute) for dedup
        by_source: Dict[str, List[str]] = {}
        for name in expanded:
            meta = cat.get(name)
            if not meta:
                logger.warning(f"GATHER_FIELDS unknown field: {name}")
                continue
            source = meta["cost"]
            by_source.setdefault(source, []).append(name)

        # Fetch each source once.
        # I/O sources (status, economy, peers) call commerce._get_sync() which
        # blocks. gather_fields() runs on the asyncio event loop; a blocking HTTP
        # call here deadlocks because the cockpit handler (in the same loop) can
        # never process the request. Fix: run I/O sources in a thread pool so the
        # event loop stays free to handle the cockpit response.
        import concurrent.futures as _cf
        _IO_SOURCES = {"status", "economy", "peers"}
        source_data: Dict[str, dict] = {}
        t0 = time.time()

        io_sources = {s: f for s, f in by_source.items() if s in _IO_SOURCES}
        sync_sources = {s: f for s, f in by_source.items() if s not in _IO_SOURCES}

        # Run I/O-bound sources in parallel threads
        if io_sources:
            with _cf.ThreadPoolExecutor(max_workers=len(io_sources)) as _ex:
                _futs = {}
                for source in io_sources:
                    if source == "status":
                        _futs[source] = _ex.submit(_fetch_source_status, self._commerce)
                    elif source == "economy":
                        _futs[source] = _ex.submit(_fetch_source_economy, self._commerce)
                    elif source == "peers":
                        _futs[source] = _ex.submit(_fetch_source_peers, self._commerce)
                for source, fut in _futs.items():
                    try:
                        source_data[source] = fut.result(timeout=12)
                    except Exception as e:
                        logger.warning(f"GATHER_FIELDS source {source} failed: {e}")
                        source_data[source] = {"error": str(e)}

        # Run non-I/O sources synchronously
        for source, fields in sync_sources.items():
            try:
                if source == "wallet":
                    source_data[source] = _fetch_source_wallet(self._wallet)
                elif source == "journal":
                    source_data[source] = _fetch_source_journal(self._db)
                elif source == "memory":
                    source_data[source] = _fetch_source_memory(self._db)
                elif source == "none":
                    source_data[source] = _fetch_source_probe(self._db)
                else:
                    logger.warning(f"GATHER_FIELDS unknown source: {source}")
                    source_data[source] = {"error": f"unknown source: {source}"}
            except Exception as e:
                logger.warning(f"GATHER_FIELDS source {source} failed: {e}")
                source_data[source] = {"error": str(e)}

        # Extract individual fields
        results = {}
        for name in expanded:
            meta = cat.get(name)
            if not meta:
                continue
            source = meta["cost"]
            data = source_data.get(source, {})
            try:
                results[name] = _extract_field(name, meta, data)
            except Exception as e:
                logger.warning(f"GATHER_FIELDS extract {name} failed: {e}")
                results[name] = f"error: {e}"

        wall_ms = int((time.time() - t0) * 1000)
        sources_hit = len(source_data)
        logger.debug(
            f"GATHER_FIELDS {len(results)} fields from {sources_hit} sources, "
            f"{wall_ms}ms")
        return results

    # ── Legacy [[gather]] blocks ────────────────────────────────────

    async def gather(self, recipe: dict, envelope) -> Dict[str, Any]:
        """Run all [[gather]] sections and return results dict.

        Results are keyed by the gather name. Each value is the fetched
        data (dict, list, or string depending on source).

        HTTP-based sources (cockpit, commerce) run in threads to avoid
        blocking the event loop during sync urllib calls.
        """
        gather_configs = recipe.get("gather", [])
        if not gather_configs:
            return {}

        results = {}
        t0 = time.time()

        # Sources that do sync HTTP / blocking I/O and must run in a thread
        _IO_SOURCES = {"cockpit", "commerce", "http", "peers", "receipts"}

        for cfg in gather_configs:
            name = cfg.get("name", "")
            source = cfg.get("source", "")
            if not name or not source:
                continue

            try:
                if source in _IO_SOURCES:
                    value = await asyncio.to_thread(self._fetch_one, cfg, envelope)
                else:
                    value = self._fetch_one(cfg, envelope)
                results[name] = value
                preview = str(value)[:100] if value else "empty"
                logger.debug(f"GATHER {name} ({source}): {preview}")
            except Exception as e:
                logger.warning(f"GATHER {name} failed: {e}")
                results[name] = {"error": str(e)}

        wall_ms = int((time.time() - t0) * 1000)
        if results:
            logger.debug(f"GATHER total: {len(results)} sources, {wall_ms}ms")
        return results

    def _fetch_one(self, cfg: dict, envelope) -> Any:
        """Fetch data from a single source."""
        source = cfg["source"]
        resolved = _resolve_templates(cfg, envelope)

        if source == "cockpit":
            return self._fetch_cockpit(resolved)
        elif source == "punchhole":
            object_key = resolved.get("object", "")
            if not object_key:
                raise ValueError("punchhole source requires 'object' field")
            return self._gather_punchhole(object_key)
        elif source == "memory":
            return self._fetch_memory(resolved)
        elif source == "static":
            return resolved.get("value", "")
        elif source == "peers":
            return self._fetch_peers(resolved)
        elif source == "receipts":
            return self._fetch_receipts(resolved)
        else:
            raise ValueError(f"Unknown gather source: {source}")

    def _fetch_peers(self, cfg: dict) -> List[dict]:
        """Fetch peer list from GET /api/peers with optional filtering.

        Params (from resolved cfg):
            filter  — "connected" (default), "stale", or "all"
            fields  — list of field names to project (optional)
            limit   — integer cap on results (optional)

        Runs inside asyncio.to_thread() — uses sync commerce HTTP.
        """
        if not self._commerce:
            logger.debug("PEERS no commerce module — returning []")
            return []

        raw = self._commerce._get_sync("/api/peers") or {}
        peers: List[dict] = raw if isinstance(raw, list) else raw.get("peers", [])

        # Apply filter
        peer_filter = cfg.get("filter", "connected")
        if peer_filter == "connected":
            peers = [p for p in peers if p.get("connected", False)]
        elif peer_filter == "stale":
            peers = [p for p in peers if not p.get("connected", False)]
        # "all" — no filter

        # Project fields if requested
        fields = cfg.get("fields")
        if fields and isinstance(fields, list):
            peers = [{k: p[k] for k in fields if k in p} for p in peers]

        # Cap at limit
        limit = cfg.get("limit")
        if limit is not None:
            try:
                peers = peers[:int(limit)]
            except (ValueError, TypeError):
                pass

        logger.debug(f"PEERS fetched {len(peers)} peers (filter={peer_filter})")
        return peers

    def _fetch_receipts(self, cfg: dict) -> List[dict]:
        """Fetch receipts via ctx.query_receipts() with optional filtering.

        Params (from resolved cfg):
            peer   — node_id prefix/full to filter by (optional)
            since  — time window string: "Nh" for N hours, "Nm" for N minutes (optional)
            limit  — integer cap on results (optional)

        Requires ctx.query_receipts() — degrades gracefully to [] if unavailable.
        Runs inside asyncio.to_thread() — query_receipts is assumed synchronous.
        """
        if self._ctx is None:
            logger.debug("RECEIPTS no ctx — returning []")
            return []

        query_receipts = getattr(self._ctx, "query_receipts", None)
        if query_receipts is None:
            logger.debug("RECEIPTS ctx.query_receipts not available — returning []")
            return []

        # Parse since → cutoff timestamp
        since_str = cfg.get("since", "")
        cutoff: Optional[float] = None
        if since_str:
            m = re.match(r"^(\d+(?:\.\d+)?)\s*([hm])$", since_str.strip())
            if m:
                amount = float(m.group(1))
                unit = m.group(2)
                seconds = amount * 3600 if unit == "h" else amount * 60
                cutoff = time.time() - seconds
            else:
                logger.warning(f"RECEIPTS unrecognised since format: {since_str!r}")

        try:
            receipts: List[dict] = query_receipts() or []
        except Exception as e:
            logger.warning(f"RECEIPTS query_receipts() failed: {e}")
            return []

        # Apply since filter
        if cutoff is not None:
            receipts = [r for r in receipts
                        if float(r.get("timestamp", 0)) >= cutoff]

        # Apply peer filter
        peer = cfg.get("peer", "")
        if peer:
            receipts = [r for r in receipts
                        if r.get("peer_id", r.get("node_id", "")).startswith(peer)
                        or r.get("from_node", "").startswith(peer)]

        # Cap at limit
        limit = cfg.get("limit")
        if limit is not None:
            try:
                receipts = receipts[:int(limit)]
            except (ValueError, TypeError):
                pass

        logger.debug(f"RECEIPTS fetched {len(receipts)} receipts "
                     f"(peer={peer or 'any'}, since={since_str or 'all'})")
        return receipts

    def _gather_punchhole(self, object_key: str) -> dict:
        """Read a private cache object directly from the punchhole backend.

        Calls get_private_object(key) on the punchhole-backend plugin instance
        via PluginContext.get_plugin(). This is a direct in-process memory read
        — no HTTP, no serialization, sub-millisecond latency.

        Gracefully degrades to {} when punchhole-backend is not loaded.

        Returns the data payload from the signed cache object. Private objects
        are stored as {"data": {...}, ...} — we unwrap and return data.
        """
        if self._ctx is None:
            logger.debug("PUNCHHOLE no ctx — skipping (ctx not wired)")
            return {}
        get_plugin = getattr(self._ctx, "get_plugin", None)
        if get_plugin is None:
            logger.debug("PUNCHHOLE ctx.get_plugin not available — skipping")
            return {}
        backend = get_plugin("punchhole-backend")
        if backend is None:
            logger.debug(f"PUNCHHOLE backend not loaded — returning empty for {object_key}")
            return {}
        try:
            obj = backend.get_private_object(object_key)
            if obj is None:
                logger.debug(f"PUNCHHOLE {object_key} not in cache — returning empty")
                return {}
            # Signed cache objects wrap payload in a "data" key; fall back to
            # returning the whole object if there is no "data" key.
            return obj.get("data", obj)
        except Exception as e:
            logger.warning(f"PUNCHHOLE get_private_object({object_key}) failed: {e}")
            return {}

    def _fetch_cockpit(self, cfg: dict) -> Any:
        """Fetch data from cockpit API."""
        if not self._commerce:
            return {"error": "no commerce module"}

        endpoint = cfg.get("endpoint", "")
        if not endpoint:
            return {"error": "no endpoint specified"}

        # _fetch_cockpit runs inside asyncio.to_thread() — use sync HTTP
        if endpoint == "/api/economy":
            return self._commerce._get_sync("/api/economy")
        elif endpoint == "/api/ledger":
            result = self._commerce._get_sync("/api/ledger")
            return result if isinstance(result, list) else []
        elif endpoint.startswith("/api/receipts/"):
            ref = endpoint.split("/")[-1]
            return self._commerce._get_sync(f"/api/receipts/{ref}")
        else:
            return self._commerce._get_sync(endpoint)

    def _fetch_memory(self, cfg: dict) -> Any:
        """Query thrall structured memory."""
        if not self._memory:
            return {"error": "no memory module"}

        query = cfg.get("query", {})

        if cfg.get("format") == "prompt":
            return self._memory.format_for_prompt(
                node_id=query.get("node_id") or None,
                skill=query.get("skill") or None,
                limit=int(query.get("limit", "5")))

        if cfg.get("format") == "summary":
            node_id = query.get("node_id")
            if not node_id:
                return {"error": "summary requires node_id"}
            return self._memory.get_peer_summary(
                node_id=node_id,
                skill=query.get("skill") or None,
                days=int(query.get("days", "7")))

        return self._memory.query(
            node_id=query.get("node_id") or None,
            skill=query.get("skill") or None,
            outcome=query.get("outcome") or None,
            limit=int(query.get("limit", "10")),
            include_dryrun=query.get("include_dryrun") == "true")


def _resolve_templates(cfg: dict, envelope) -> dict:
    """Substitute {{field}} placeholders with envelope values."""
    resolved = {}
    for k, v in cfg.items():
        if isinstance(v, str) and "{{" in v:
            resolved[k] = re.sub(
                r"\{\{(.+?)\}\}",
                lambda m: envelope.get(m.group(1).strip(), m.group(0)),
                v)
        elif isinstance(v, dict):
            resolved[k] = _resolve_templates(v, envelope)
        else:
            resolved[k] = v
    return resolved

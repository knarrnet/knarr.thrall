"""settlement-check-lite — Autonomous bilateral position checker.

Queries the cockpit ledger, finds positions above soft_threshold,
builds signed netting proposals using thrall's delegated identity,
and submits settlement requests via mail.

Triggered by the settlement-check recipe (on_tick, hotwire, hourly).
No LLM inference — pure computation.

T3: Per-peer cooldown — each peer gets its own last-checked timestamp
in thrall_settlement_cooldown (thrall.db). A single tick can check all
peers that are past their cooldown; no starvation at 100+ peers.

Input:
  - threshold: utilization threshold (default: 0.8)
  - cooldown_seconds: per-peer cooldown override (default: 3600)
  - cockpit_url: override cockpit URL (default from plugin.toml)

Output:
  - status: ok/no_positions/disabled/ceiling_hit/error
  - positions_checked: number of ledger entries examined
  - settlements_proposed: number of proposals sent
  - total_amount: sum of proposed settlement amounts
  - skipped_cooldown: peers skipped due to per-peer cooldown
  - wall_ms: total execution time
"""

import json
import logging
import os
import sqlite3
import sys
import time

NODE = None

logger = logging.getLogger("thrall.settlement_check")


def set_node(node):
    global NODE
    NODE = node


def _get_plugin_dir():
    """Resolve thrall plugin directory from skill file location.

    Skills live at <provider>/skills/ but thrall config is at
    <provider>/plugins/06-thrall/. Resolve relative to this file.
    """
    provider_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(provider_root, "plugins", "06-thrall")


def _get_config():
    """Read thrall config from plugin.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    plugin_toml = os.path.join(_get_plugin_dir(), "plugin.toml")
    with open(plugin_toml, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("config", {})


# ── Per-peer cooldown helpers ──

def _open_cooldown_db(db_path: str):
    """Open thrall.db and ensure thrall_settlement_cooldown table exists."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thrall_settlement_cooldown (
            peer_id         TEXT PRIMARY KEY,
            last_checked_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _load_cooldowns(conn) -> dict:
    """Return {peer_id: last_checked_at} for all rows."""
    rows = conn.execute(
        "SELECT peer_id, last_checked_at FROM thrall_settlement_cooldown"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _set_cooldown(conn, peer_id: str, now: float):
    """Upsert last_checked_at for a peer."""
    conn.execute("""
        INSERT OR REPLACE INTO thrall_settlement_cooldown (peer_id, last_checked_at)
        VALUES (?, ?)
    """, (peer_id, now))
    conn.commit()


async def handle(input_data: dict) -> dict:
    t0 = time.time()
    threshold = float(input_data.get("threshold", "0.8"))

    # Load config
    try:
        config = _get_config()
    except Exception as e:
        return {"status": "error", "result_summary": f"Config load failed: {e}",
                "wall_ms": str(int((time.time() - t0) * 1000))}

    thrall_cfg = config.get("thrall", {})
    identity_cfg = thrall_cfg.get("identity", {})
    wallet_cfg = thrall_cfg.get("wallet", {})

    # Per-peer cooldown: prefer input override, then recipe filter config, then 3600s
    # (The recipe passes cooldown_seconds=3600 in [filter]; we mirror that default here.)
    cooldown_seconds = float(input_data.get(
        "cooldown_seconds",
        thrall_cfg.get("settlement_check_cooldown", 3600)
    ))

    # Check if identity is enabled
    if not identity_cfg.get("enabled", False):
        return {"status": "disabled",
                "result_summary": "Thrall identity not enabled",
                "wall_ms": str(int((time.time() - t0) * 1000))}

    # Import thrall modules
    plugin_dir = _get_plugin_dir()
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    from identity import ThrallIdentity
    from wallet import ThrallWallet
    from commerce import ThrallCommerce
    from db import ThrallDB
    from memory import CircuitBreaker

    # Initialize components
    db_path = os.path.join(plugin_dir, "thrall.db")
    db = ThrallDB(db_path)
    node_id = NODE.node_info.node_id if NODE else ""
    identity = ThrallIdentity(plugin_dir, identity_cfg, node_id=node_id)
    wallet = ThrallWallet(db, wallet_cfg)
    cockpit_url = thrall_cfg.get("cockpit_url", "http://127.0.0.1:8080")
    cockpit_token = thrall_cfg.get("cockpit_token", "")

    if not identity.enabled:
        return {"status": "disabled",
                "result_summary": "Thrall identity failed to initialize",
                "wall_ms": str(int((time.time() - t0) * 1000))}

    # Policy defaults (from knarr.toml, fallback to sensible defaults)
    policy = config.get("policy", {})
    commerce = ThrallCommerce(
        cockpit_url=cockpit_url,
        cockpit_token=cockpit_token,
        node_id=node_id,
        default_policy=policy,
    )
    breaker_cfg = thrall_cfg.get("circuit_breaker", {})
    breaker = CircuitBreaker(db, breaker_cfg)

    # ── Per-peer cooldown: open DB connection (graceful fallback on failure) ──
    cooldown_conn = None
    cooldown_map: dict = {}
    try:
        cooldown_conn = _open_cooldown_db(db_path)
        cooldown_map = _load_cooldowns(cooldown_conn)
        logger.debug(
            f"Loaded per-peer cooldowns: {len(cooldown_map)} entries, "
            f"cooldown_seconds={cooldown_seconds:.0f}"
        )
    except Exception as e:
        logger.warning(f"Per-peer cooldown DB unavailable — checking all peers: {e}")

    # Check positions
    ledger = await commerce.query_ledger()
    positions_checked = len(ledger)
    positions = await commerce.check_positions(threshold)

    if not positions:
        if cooldown_conn:
            cooldown_conn.close()
        return {
            "status": "no_positions",
            "positions_checked": str(positions_checked),
            "settlements_proposed": "0",
            "total_amount": "0",
            "skipped_cooldown": "0",
            "result_summary": f"No positions above {threshold * 100:.0f}% threshold",
            "wall_ms": str(int((time.time() - t0) * 1000)),
        }

    # Process each over-threshold position
    proposed = 0
    total_amount = 0.0
    skipped_ceiling = 0
    skipped_circuit = 0
    skipped_cooldown = 0
    now = time.time()

    for pos in positions:
        amount = pos["settle_amount"]
        peer_pk = pos["peer_public_key"]

        # ── Per-peer cooldown check ──
        # peer_id key: use full peer_public_key for maximum uniqueness.
        # Falls back to checking all peers if cooldown_conn is unavailable.
        if cooldown_conn is not None:
            last_checked = cooldown_map.get(peer_pk)
            if last_checked is not None and (now - last_checked) < cooldown_seconds:
                remaining = int(cooldown_seconds - (now - last_checked))
                logger.debug(
                    f"PEER_COOLDOWN skip peer={peer_pk[:16]} "
                    f"remaining={remaining}s"
                )
                skipped_cooldown += 1
                continue

        # Check circuit breaker — skip peers with open circuits
        if breaker.is_open(peer_pk, "settlement"):
            skipped_circuit += 1
            continue

        # Check wallet ceiling
        if not wallet.can_spend(amount):
            skipped_ceiling += 1
            continue

        # Build and sign netting document
        doc = commerce.build_netting_doc(
            peer_pk=pos["peer_public_key"],
            settle_amount=amount,
            current_balance=pos["balance"],
            target_balance=pos["target_balance"],
            identity=identity,
        )

        # Submit via mail (need send_mail function)
        # For now, log the proposal — actual submission requires send_mail_fn
        # which is wired through the ActionExecutor in handler.py
        if NODE and hasattr(NODE, "send_mail"):
            body = {
                "type": "knarr/commerce/settle_request",
                "proposal": doc,
            }
            try:
                await NODE.send_mail(
                    peer_pk, "knarr/commerce/settle_request", body)
                wallet.record_spend(amount, f"netting:{peer_pk[:16]}",
                                    peer_pk,
                                    f"Auto-settlement at {pos['utilization_pct']}% utilization")
                breaker.record_success(peer_pk, "settlement")
                # Update per-peer cooldown on successful proposal
                if cooldown_conn is not None:
                    try:
                        _set_cooldown(cooldown_conn, peer_pk, now)
                        cooldown_map[peer_pk] = now
                    except Exception as e:
                        logger.warning(f"Failed to persist cooldown for {peer_pk[:16]}: {e}")
                proposed += 1
                total_amount += amount
            except Exception as e:
                breaker.record_failure(peer_pk, "settlement")
        else:
            # No NODE — record the proposal anyway for observability
            wallet.record_spend(amount, f"netting:{peer_pk[:16]}",
                                peer_pk,
                                f"Proposal built (no send_mail), {pos['utilization_pct']}% util")
            # Update per-peer cooldown even in no-NODE path
            if cooldown_conn is not None:
                try:
                    _set_cooldown(cooldown_conn, peer_pk, now)
                    cooldown_map[peer_pk] = now
                except Exception as e:
                    logger.warning(f"Failed to persist cooldown for {peer_pk[:16]}: {e}")
            proposed += 1
            total_amount += amount

    if cooldown_conn is not None:
        cooldown_conn.close()

    status = "ok"
    if proposed == 0 and skipped_ceiling > 0:
        status = "ceiling_hit"

    summary_parts = [f"checked={positions_checked}", f"proposed={proposed}"]
    if skipped_cooldown:
        summary_parts.append(f"cooldown_skip={skipped_cooldown}")
    if skipped_circuit:
        summary_parts.append(f"circuit_open={skipped_circuit}")
    if skipped_ceiling:
        summary_parts.append(f"ceiling_blocked={skipped_ceiling}")

    return {
        "status": status,
        "positions_checked": str(positions_checked),
        "settlements_proposed": str(proposed),
        "total_amount": f"{total_amount:.1f}",
        "skipped_cooldown": str(skipped_cooldown),
        "skipped_ceiling": str(skipped_ceiling),
        "skipped_circuit": str(skipped_circuit),
        "result_summary": ", ".join(summary_parts),
        "wall_ms": str(int((time.time() - t0) * 1000)),
    }

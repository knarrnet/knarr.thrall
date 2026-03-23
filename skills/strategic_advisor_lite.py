"""strategic-advisor-lite — Strategic guidance for swarm nodes.

Assembles an operational snapshot from punchhole gather data and calls
peer-analysis-lite to produce prioritised guidance on peer selection,
skill provisioning, and budget allocation. Writes the returned strategy
to living memory pillar 92-memory-strategy.md.

Two call modes:

1. Snapshot mode (called by strategic-advisor recipe on_tick):
   Input keys: economy, positions, peers, skills — each a JSON string
   returned from punchhole gather. Builds prompt, calls peer-analysis-lite
   via cockpit, writes strategy to memory pillar, returns strategy dict.

2. Reset-cooldown mode (called by strategic-advisor-refresh recipe):
   Input key: action = "reset_cooldown"
   Clears the thrall cooldown key for the main recipe so it fires on
   the next tick after a significant state-change event.

Output (snapshot mode):
  strategy:
    priorities           — ordered list of recommended focus areas
    peers_to_cultivate   — peer IDs worth investing credits with
    skills_to_provision  — skills worth adding to this node
    budget_allocation    — suggested spend distribution as a dict
  result_summary: short human-readable summary
  wall_ms: execution time in milliseconds

Output (reset_cooldown mode):
  status: ok | error
  message: confirmation string
  wall_ms: execution time
"""

import json
import logging
import os
import sys
import time

NODE = None

logger = logging.getLogger("thrall.strategic_advisor_lite")


def set_node(node):
    global NODE
    NODE = node


def _get_plugin_dir() -> str:
    """Resolve thrall plugin directory from skill file location."""
    provider_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(provider_root, "plugins", "06-thrall")


def _get_config() -> dict:
    """Read thrall config from plugin.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    plugin_toml = os.path.join(_get_plugin_dir(), "plugin.toml")
    with open(plugin_toml, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("config", {})


def _parse_snapshot_field(raw: str) -> dict:
    """Parse a punchhole gather field. Returns {} on empty or parse failure."""
    if not raw or raw.strip() in ("", "{}", "null"):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _build_snapshot_prompt(economy: dict, positions: dict,
                            peers: dict, skills: dict) -> str:
    """Format operational snapshot as a structured text prompt."""
    lines = [
        "# Operational Snapshot",
        "",
        "## Economy",
    ]

    if economy:
        # Surface key economy signals
        net_pos = economy.get("net_position", economy.get("summary", {}).get("net_position", "n/a"))
        lines.append(f"Net position: {net_pos}")
        ledger = economy.get("positions", economy.get("ledger", []))
        if isinstance(ledger, list) and ledger:
            lines.append(f"Active bilateral positions: {len(ledger)}")
            for p in ledger[:5]:
                pk = p.get("peer_public_key", p.get("peer_id", "?"))[:16]
                bal = p.get("balance", 0)
                prov = p.get("tasks_provided", 0)
                cons = p.get("tasks_consumed", 0)
                lines.append(f"  {pk}: bal={bal:.2f} prov={prov} cons={cons}")
    else:
        lines.append("(punchhole not loaded — no economy data)")

    lines += ["", "## Positions"]
    if positions:
        util = positions.get("utilization_pct", positions.get("utilization", "n/a"))
        lines.append(f"Utilization: {util}")
        cands = positions.get("settlement_candidates", [])
        if cands:
            lines.append(f"Settlement candidates: {len(cands)}")
    else:
        lines.append("(no positions data)")

    lines += ["", "## Peers"]
    if peers:
        peer_list = peers if isinstance(peers, list) else peers.get("peers", [])
        lines.append(f"Connected peers: {len(peer_list)}")
        for p in peer_list[:8]:
            pk = p.get("node_id", p.get("peer_id", "?"))[:16]
            host = p.get("host", "?")
            n_skills = p.get("skill_count", len(p.get("skills", [])))
            lines.append(f"  {pk}: {host} skills={n_skills}")
    else:
        lines.append("(no peers data)")

    lines += ["", "## Available Skills"]
    if skills:
        skill_list = skills if isinstance(skills, list) else skills.get("skills", [])
        lines.append(f"Network skills: {len(skill_list)}")
        for s in skill_list[:10]:
            name = s if isinstance(s, str) else s.get("name", "?")
            price = "" if isinstance(s, str) else f" price={s.get('price', '?')}"
            lines.append(f"  {name}{price}")
    else:
        lines.append("(no skills inventory)")

    lines += [
        "",
        "## Request",
        "Based on the snapshot above, provide strategic guidance for this swarm node.",
        "Return a JSON object with keys:",
        "  priorities: list of 3-5 recommended focus areas (strings)",
        "  peers_to_cultivate: list of peer IDs (first 16 chars) worth investing in",
        "  skills_to_provision: list of skill names worth adding to this node",
        "  budget_allocation: dict of category -> suggested % of daily budget",
        "",
        "Be concise. Each entry should be actionable.",
    ]

    return "\n".join(lines)


def _cockpit_call_skill(cockpit_url: str, cockpit_token: str,
                        skill_name: str, skill_input: dict,
                        timeout_s: int = 120) -> dict:
    """Synchronous cockpit skill call with async job polling."""
    import ssl
    from urllib.request import Request, urlopen
    from urllib.error import URLError

    ssl_ctx = None
    if cockpit_url.startswith("https"):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    payload = json.dumps({
        "skill": skill_name,
        "input": skill_input,
        "timeout": timeout_s,
    }).encode()

    req = Request(
        f"{cockpit_url}/api/execute",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cockpit_token}",
        },
    )
    try:
        resp = urlopen(req, timeout=timeout_s + 10, context=ssl_ctx)
        data = json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "error": f"cockpit submit failed: {e}"}

    job_id = data.get("job_id")
    if not job_id:
        return data.get("result", data)

    # Poll for result
    deadline = time.time() + timeout_s
    poll_count = 0
    while time.time() < deadline:
        poll_req = Request(
            f"{cockpit_url}/api/jobs/{job_id}",
            headers={"Authorization": f"Bearer {cockpit_token}"},
        )
        try:
            poll_resp = urlopen(poll_req, timeout=10, context=ssl_ctx)
            poll_data = json.loads(poll_resp.read())
            status = poll_data.get("status", "")
            if status == "completed":
                return poll_data.get("result", poll_data.get("output_data", {}))
            if status == "failed":
                return {"status": "error", "error": poll_data.get("error", "job failed")}
        except URLError:
            pass
        poll_count += 1
        if poll_count <= 3:
            time.sleep(0.5)
        elif poll_count <= 8:
            time.sleep(1)
        else:
            time.sleep(2)

    return {"status": "error", "error": f"job {job_id} timed out after {timeout_s}s"}


def _parse_strategy_from_result(result: dict) -> dict:
    """Extract strategy dict from peer-analysis-lite result."""
    # peer-analysis-lite returns analysis in various keys
    for key in ("strategy", "analysis", "result", "output"):
        val = result.get(key)
        if isinstance(val, dict):
            return val

    # Try to parse JSON from a string field
    for key in ("strategy", "analysis", "result", "content", "text"):
        val = result.get(key)
        if isinstance(val, str) and val.strip().startswith("{"):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass

    # Build minimal strategy from whatever keys are present
    strategy = {}
    for k in ("priorities", "peers_to_cultivate", "skills_to_provision", "budget_allocation"):
        if k in result:
            strategy[k] = result[k]
    return strategy if strategy else {}


def _write_strategy_to_pillar(plugin_dir: str, strategy: dict,
                               snapshot_summary: str) -> None:
    """Append strategy to living memory pillar 92-memory-strategy.md."""
    rag_dir = os.path.join(plugin_dir, "rag")
    os.makedirs(rag_dir, exist_ok=True)
    pillar_path = os.path.join(rag_dir, "92-memory-strategy.md")

    ts_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime())

    # Create file with header if missing
    if not os.path.exists(pillar_path):
        with open(pillar_path, "w", encoding="utf-8") as f:
            f.write("# Strategic Memory\n\n")

    priorities = strategy.get("priorities", [])
    peers = strategy.get("peers_to_cultivate", [])
    skills = strategy.get("skills_to_provision", [])
    budget = strategy.get("budget_allocation", {})

    entry_lines = [f"## {ts_str}"]
    entry_lines.append(f"*Source: strategic-advisor-lite from punchhole snapshot*")
    entry_lines.append("")

    if priorities:
        entry_lines.append("**Priorities:**")
        for p in priorities:
            entry_lines.append(f"- {p}")
        entry_lines.append("")

    if peers:
        entry_lines.append(f"**Peers to cultivate:** {', '.join(str(p) for p in peers)}")
        entry_lines.append("")

    if skills:
        entry_lines.append(f"**Skills to provision:** {', '.join(str(s) for s in skills)}")
        entry_lines.append("")

    if budget:
        entry_lines.append("**Budget allocation:**")
        for cat, pct in budget.items():
            entry_lines.append(f"- {cat}: {pct}")
        entry_lines.append("")

    if snapshot_summary:
        entry_lines.append(f"*Snapshot: {snapshot_summary}*")

    with open(pillar_path, "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(entry_lines) + "\n")

    logger.info(f"STRATEGY_PILLAR: wrote strategy entry ({len(str(strategy))} chars)")


async def handle(input_data: dict) -> dict:
    t0 = time.time()

    # ── Mode 1: reset_cooldown ────────────────────────────────────────
    if input_data.get("action") == "reset_cooldown":
        plugin_dir = _get_plugin_dir()
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        try:
            from db import ThrallDB
            db_path = os.path.join(plugin_dir, "thrall.db")
            db = ThrallDB(db_path)
            db.clear_context("cooldown:strategic-advisor", "last_fired")
            logger.info("STRATEGIC_ADVISOR: cooldown cleared by refresh trigger")
            return {
                "status": "ok",
                "message": "strategic-advisor cooldown cleared",
                "wall_ms": str(int((time.time() - t0) * 1000)),
            }
        except Exception as e:
            logger.warning(f"STRATEGIC_ADVISOR reset_cooldown failed: {e}")
            return {
                "status": "error",
                "error": f"cooldown reset failed: {e}",
                "wall_ms": str(int((time.time() - t0) * 1000)),
            }

    # ── Mode 2: snapshot → strategy ──────────────────────────────────

    # Parse punchhole gather fields (JSON strings from envelope template)
    economy   = _parse_snapshot_field(input_data.get("economy", ""))
    positions = _parse_snapshot_field(input_data.get("positions", ""))
    peers     = _parse_snapshot_field(input_data.get("peers", ""))
    skills    = _parse_snapshot_field(input_data.get("skills", ""))

    # If all fields are empty, punchhole is not loaded — skip gracefully
    if not any([economy, positions, peers, skills]):
        return {
            "strategy": {},
            "skipped": "no_snapshot",
            "result_summary": "punchhole not loaded — no snapshot data",
            "wall_ms": str(int((time.time() - t0) * 1000)),
        }

    # Build snapshot summary for logging and pillar entry
    n_peers = (len(peers) if isinstance(peers, list)
               else len(peers.get("peers", [])) if isinstance(peers, dict) else 0)
    n_skills = (len(skills) if isinstance(skills, list)
                else len(skills.get("skills", [])) if isinstance(skills, dict) else 0)
    snapshot_summary = f"peers={n_peers} skills={n_skills}"

    # Load config for cockpit credentials
    try:
        config = _get_config()
    except Exception as e:
        return {
            "strategy": {},
            "status": "error",
            "result_summary": f"config load failed: {e}",
            "wall_ms": str(int((time.time() - t0) * 1000)),
        }

    thrall_cfg = config.get("thrall", {})
    cockpit_url = thrall_cfg.get("cockpit_url", "http://127.0.0.1:8080")
    cockpit_token = thrall_cfg.get("cockpit_token", "")

    plugin_dir = _get_plugin_dir()
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    # Try vault for cockpit token
    if not cockpit_token and NODE and hasattr(NODE, "vault_get"):
        try:
            cockpit_token = NODE.vault_get("cockpit_token") or ""
        except Exception:
            pass

    if not cockpit_token:
        return {
            "strategy": {},
            "status": "error",
            "result_summary": "no cockpit token — cannot call peer-analysis-lite",
            "wall_ms": str(int((time.time() - t0) * 1000)),
        }

    # Build prompt and call peer-analysis-lite
    prompt = _build_snapshot_prompt(economy, positions, peers, skills)

    import asyncio
    result = await asyncio.to_thread(
        _cockpit_call_skill,
        cockpit_url, cockpit_token,
        "peer-analysis-lite",
        {"query": prompt, "context": "strategic_advisor"},
        120,
    )

    if isinstance(result, dict) and result.get("status") == "error":
        logger.warning(f"STRATEGIC_ADVISOR peer-analysis-lite error: {result.get('error')}")
        return {
            "strategy": {},
            "status": "error",
            "result_summary": f"peer-analysis-lite failed: {result.get('error', 'unknown')}",
            "wall_ms": str(int((time.time() - t0) * 1000)),
        }

    # Parse strategy from result
    strategy = _parse_strategy_from_result(result)

    # Ensure expected keys are present (fill with empty defaults)
    strategy.setdefault("priorities", [])
    strategy.setdefault("peers_to_cultivate", [])
    strategy.setdefault("skills_to_provision", [])
    strategy.setdefault("budget_allocation", {})

    # Write to living memory pillar 92-memory-strategy.md
    try:
        _write_strategy_to_pillar(plugin_dir, strategy, snapshot_summary)
    except Exception as e:
        logger.warning(f"STRATEGIC_ADVISOR pillar write failed: {e}")

    wall_ms = int((time.time() - t0) * 1000)
    n_priorities = len(strategy.get("priorities", []))
    logger.info(
        f"STRATEGIC_ADVISOR: strategy written ({n_priorities} priorities, "
        f"{snapshot_summary}) wall={wall_ms}ms")

    return {
        "strategy": strategy,
        "result_summary": f"strategy: {n_priorities} priorities, {snapshot_summary}",
        "wall_ms": str(wall_ms),
    }

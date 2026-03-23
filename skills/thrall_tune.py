"""thrall-tune-lite — Agent tuning interface for thrall pipeline.

The agent calls this via call_skill to observe, correct, and tune
thrall's decisions. Private, zero-cost.

Actions:
  stats        — pipeline performance summary (counts, LLM hit rate, wall_ms)
  journal      — recent decisions for review
  correct      — mark a decision as wrong + record what it should have been
  get_recipe   — read a recipe's current config
  set_threshold — adjust a recipe parameter without file editing
  set_prompt   — update a prompt template
  wallet       — wallet spending summary and recent history

All actions use the same thrall.db (WAL mode handles concurrent readers).
"""

import json
import os
import sys
import time

# Add thrall plugin dir to path for imports
_THRALL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "06-thrall",
)
if _THRALL_DIR not in sys.path:
    sys.path.insert(0, _THRALL_DIR)

NODE = None
_db = None


def set_node(node):
    global NODE
    NODE = node


def _get_db():
    global _db
    if _db is not None:
        return _db
    from db import ThrallDB
    db_path = os.path.join(_THRALL_DIR, "thrall.db")
    _db = ThrallDB(db_path)
    return _db


async def handle(input_data: dict) -> dict:
    action = str(input_data.get("action", "")).strip()
    if not action:
        return {"status": "error", "error": "action is required (stats|journal|correct|get_recipe|set_threshold|set_prompt|wallet)"}

    try:
        db = _get_db()
    except Exception as e:
        return {"status": "error", "error": f"DB init failed: {e}"}

    if action == "stats":
        return _do_stats(db, input_data)
    elif action == "journal":
        return _do_journal(db, input_data)
    elif action == "correct":
        return _do_correct(db, input_data)
    elif action == "get_recipe":
        return _do_get_recipe(db, input_data)
    elif action == "set_threshold":
        return _do_set_threshold(db, input_data)
    elif action == "set_prompt":
        return _do_set_prompt(input_data)
    elif action == "wallet":
        return _do_wallet(db, input_data)
    else:
        return {"status": "error", "error": f"unknown action: {action}"}


def _do_stats(db, input_data: dict) -> dict:
    """Pipeline performance summary."""
    hours = float(input_data.get("hours", "24"))
    pipeline = input_data.get("pipeline", "").strip() or None
    since = time.time() - (hours * 3600)

    breakdown = db.get_stats(since, pipeline)
    totals = db.get_totals(since)

    total = totals.get("total", 0)
    llm_calls = totals.get("llm_calls", 0)
    cache_hits = totals.get("cache_hits", 0)
    bypasses = totals.get("bypasses", 0)
    hotwire = totals.get("hotwire", 0)
    rate_limited = totals.get("rate_limited", 0)

    evaluable = total - bypasses
    llm_hit_rate = f"{llm_calls}/{evaluable}" if evaluable > 0 else "0/0"
    cache_rate = f"{cache_hits}/{evaluable}" if evaluable > 0 else "0/0"

    # Backend info
    backend_type = "unknown"
    backend_model = ""
    try:
        toml_path = os.path.join(_THRALL_DIR, "plugin.toml")
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with open(toml_path, "rb") as f:
            cfg = tomllib.load(f)
        thrall_cfg = cfg.get("config", {}).get("thrall", {})
        backend_type = thrall_cfg.get("backend", "local")
        if backend_type == "ollama":
            backend_model = thrall_cfg.get("ollama", {}).get("model", "")
        elif backend_type == "openai":
            backend_model = thrall_cfg.get("openai", {}).get("model", "")
        elif backend_type == "local":
            backend_model = "gemma3:1b"
    except Exception:
        pass

    backend_label = f"{backend_type}"
    if backend_model:
        backend_label += f"/{backend_model}"

    summary = (
        f"{total} events in {hours}h | "
        f"backend: {backend_label} | "
        f"LLM: {llm_hit_rate} | cache: {cache_rate} | "
        f"bypass: {bypasses} | hotwire: {hotwire} | rate_limited: {rate_limited}"
    )

    return {
        "status": "ok",
        "summary": summary,
        "breakdown": json.dumps(breakdown, indent=2),
        "totals": json.dumps(totals),
        "backend": backend_label,
        "hours": str(hours),
    }


def _do_journal(db, input_data: dict) -> dict:
    """Recent decisions for review."""
    pipeline = input_data.get("pipeline", "").strip() or None
    limit = int(input_data.get("limit", "20"))
    reviewed = input_data.get("reviewed", "").strip()
    reviewed_val = int(reviewed) if reviewed != "" else None

    entries = db.query_journal(pipeline=pipeline, reviewed=reviewed_val, limit=limit)

    formatted = []
    for e in entries:
        formatted.append({
            "id": e["id"],
            "pipeline": e["pipeline"],
            "eval_type": e["eval_type"],
            "action": e["action_name"],
            "wall_ms": e["wall_ms"],
            "reviewed": e["reviewed"],
            "correction": e["correction"],
            "from_node": (e.get("from_node") or "")[:16],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.gmtime(e["timestamp"])),
        })

    return {
        "status": "ok",
        "entries": json.dumps(formatted, indent=2),
        "count": str(len(formatted)),
    }


def _do_correct(db, input_data: dict) -> dict:
    """Mark a decision as wrong + record correction."""
    journal_id = input_data.get("journal_id", "").strip()
    correction = input_data.get("correction", "").strip()

    if not journal_id:
        return {"status": "error", "error": "journal_id is required"}
    if not correction:
        return {"status": "error", "error": "correction is required (e.g. 'should_have_been:wake reason:urgent misclassified')"}

    try:
        jid = int(journal_id)
    except ValueError:
        return {"status": "error", "error": f"journal_id must be integer, got: {journal_id}"}

    entry = None
    for e in db._conn.execute(
        "SELECT id, action_name, eval_type FROM thrall_journal WHERE id=?", (jid,)
    ).fetchall():
        entry = dict(e)

    if not entry:
        return {"status": "error", "error": f"journal entry {jid} not found"}

    db.mark_reviewed(jid, 1, correction)
    return {
        "status": "ok",
        "message": f"Journal #{jid} marked reviewed with correction",
        "original_action": entry.get("action_name", "?"),
        "correction": correction,
    }


def _do_get_recipe(db, input_data: dict) -> dict:
    """Read a recipe's current config."""
    name = input_data.get("name", "").strip()
    if not name:
        all_recipes = db.get_all_recipes()
        names = [r["name"] for r in all_recipes]
        return {
            "status": "ok",
            "recipes": json.dumps(names),
            "count": str(len(names)),
        }

    recipe = db.get_recipe(name)
    if not recipe:
        return {"status": "error", "error": f"recipe '{name}' not found"}

    return {
        "status": "ok",
        "name": name,
        "config": json.dumps(recipe["config"], indent=2),
        "source_file": recipe.get("source_file", ""),
        "mode": recipe.get("mode", "automated"),
    }


def _do_set_threshold(db, input_data: dict) -> dict:
    """Adjust a recipe parameter without file editing.

    path is dot-separated: "filter.cache_ttl", "evaluate.fallback_action",
    "actions.compile.summon_threshold"
    """
    recipe_name = input_data.get("recipe", "").strip()
    path = input_data.get("path", "").strip()
    value = input_data.get("value", "").strip()

    if not recipe_name or not path or not value:
        return {"status": "error", "error": "recipe, path, and value are all required"}

    recipe = db.get_recipe(recipe_name)
    if not recipe:
        return {"status": "error", "error": f"recipe '{recipe_name}' not found"}

    config = recipe["config"]
    parts = path.split(".")

    node = config
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]

    leaf = parts[-1]
    old_value = node.get(leaf, "(unset)")

    try:
        typed_value = int(value)
    except ValueError:
        try:
            typed_value = float(value)
        except ValueError:
            if value.lower() in ("true", "false"):
                typed_value = value.lower() == "true"
            else:
                typed_value = value

    node[leaf] = typed_value

    db.upsert_recipe(recipe_name, config,
                     source_file=recipe.get("source_file"),
                     mode=recipe.get("mode", "automated"))

    source_file = recipe.get("source_file", "")
    toml_updated = False
    if source_file and os.path.exists(source_file):
        try:
            _update_toml_file(source_file, parts, typed_value)
            toml_updated = True
        except Exception:
            pass

    _touch_reload()

    return {
        "status": "ok",
        "recipe": recipe_name,
        "path": path,
        "old_value": str(old_value),
        "new_value": str(typed_value),
        "toml_updated": str(toml_updated),
    }


def _do_set_prompt(input_data: dict) -> dict:
    """Update a prompt template."""
    name = input_data.get("name", "").strip()
    content = input_data.get("content", "").strip()

    if not name or not content:
        return {"status": "error", "error": "name and content are required"}

    prompts_dir = os.path.join(_THRALL_DIR, "prompts")
    os.makedirs(prompts_dir, exist_ok=True)
    prompt_path = os.path.join(prompts_dir, f"{name}.toml")

    old_version = "0"
    if os.path.exists(prompt_path):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with open(prompt_path, "rb") as f:
            old = tomllib.load(f)
            old_version = str(old.get("version", "0"))

    try:
        new_version = str(float(old_version) + 0.1)
    except ValueError:
        new_version = "1.0"

    toml_content = f'name = "{name}"\nversion = "{new_version}"\n\ncontent = """{content}"""\n'

    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(toml_content)

    _touch_reload()

    return {
        "status": "ok",
        "name": name,
        "old_version": old_version,
        "new_version": new_version,
        "path": prompt_path,
    }


def _do_wallet(db, input_data: dict) -> dict:
    """Wallet spending summary and recent history."""
    hours = float(input_data.get("hours", "24"))
    since = time.time() - (hours * 3600)

    daily_spend = db.get_daily_spend()
    history = db.get_wallet_history(since=since, limit=20)

    formatted = []
    for h in history:
        formatted.append({
            "amount": h["amount"],
            "peer": (h.get("peer_pk") or "")[:16],
            "reference": h.get("reference", ""),
            "description": h.get("description", ""),
            "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.gmtime(h["timestamp"])),
        })

    ceiling = 50.0
    try:
        toml_path = os.path.join(_THRALL_DIR, "plugin.toml")
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with open(toml_path, "rb") as f:
            cfg = tomllib.load(f)
        ceiling = cfg.get("config", {}).get("wallet", {}).get("daily_ceiling", 50.0)
    except Exception:
        pass

    utilization = (daily_spend / ceiling * 100) if ceiling > 0 else 0

    summary = (
        f"Today: {daily_spend:.1f}/{ceiling:.1f} credits "
        f"({utilization:.0f}% utilization) | "
        f"{len(history)} transactions in {hours}h"
    )

    return {
        "status": "ok",
        "summary": summary,
        "daily_spend": str(daily_spend),
        "ceiling": str(ceiling),
        "utilization_pct": f"{utilization:.1f}",
        "history": json.dumps(formatted, indent=2),
        "transaction_count": str(len(history)),
    }


def _touch_reload():
    """Touch the sentinel file to trigger recipe/prompt reload."""
    reload_path = os.path.join(_THRALL_DIR, "thrall.reload")
    with open(reload_path, "w") as f:
        f.write(str(time.time()))


def _update_toml_file(filepath: str, path_parts: list, value):
    """Best-effort TOML file update. Reads, modifies, writes back."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    with open(filepath, "rb") as f:
        config = tomllib.load(f)

    node = config
    for part in path_parts[:-1]:
        if part not in node:
            node[part] = {}
        node = node[part]
    node[path_parts[-1]] = value

    try:
        import tomli_w
        with open(filepath, "wb") as f:
            tomli_w.dump(config, f)
    except ImportError:
        pass

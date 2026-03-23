# Code Review: Thrall v0.50.0 Changes

You are reviewing 6 changes to the Thrall plugin — a pipeline engine for the knarr peer-to-peer protocol. Thrall runs inside a Python plugin that hooks into the node's event bus and tick loop. It processes recipes (TOML config files) through a pipeline: TRIGGER -> FILTER -> GATHER -> EVALUATE -> ACTION.

The changes were made to fix issues discovered during a 101-node cluster experiment (exp-101). Your job is to find bugs, logic errors, race conditions, or regressions. Be harsh. If something is fine, say so briefly and move on.

## Context you need

- **Cockpit** is an HTTP API on port 8080 with a **64KB input body limit**.
- **async_jobs** table in the cockpit deduplicates by hashing the JSON-serialized skill input. The dedup key includes `failed` status — a failed job permanently blocks identical inputs (HTTP 409).
- **Envelope** is a dataclass that flows through every pipeline stage. It has `trigger_type: str`, `timestamp: float`, and `fields: Dict[str, str]`.
- **Template resolution** in actions: `{{envelope.XXX}}` resolves to `envelope.fields["XXX"]`. Only exact `{{...}}` wrapper is resolved (not substring).
- **Hotwire rules** match envelope fields by regex pattern. `default_action` fires when no rule matches.
- **`mode = "disabled"`** is a NEW convention — previously the loader loaded all `.toml` files unconditionally.

## The 6 changes (review each)

### 1. Dedup-busting: `_ts` in Envelope.__post_init__

**engine.py** — Added `__post_init__` to Envelope dataclass:

```python
def __post_init__(self):
    if "_ts" not in self.fields:
        self.fields["_ts"] = str(int(self.timestamp))
```

**5 recipe files** — Added `_ts = "{{envelope._ts}}"` to input templates, e.g.:

```toml
# Before:
input = { node_id = "self" }

# After:
input = { node_id = "self", _ts = "{{envelope._ts}}" }
```

**Intent:** Each recipe invocation gets a unique input hash so the cockpit dedup table doesn't permanently block retries after a failure.

**Review questions:**
- Is `int(self.timestamp)` granular enough? Two recipes firing within the same second get the same `_ts`. Does that matter given cooldowns are 300s+?
- Does mutating `fields` in `__post_init__` of a dataclass marked "Immutable context" violate any expectations?
- Could `_ts` collide with a user-defined field name?
- Is there a scenario where the dedup SHOULD block (e.g., rapid-fire duplicate calls) that this defeats?

---

### 2. Payload guard in thrall_actions.py

Added before `_call_skill()`:

```python
_MAX_PAYLOAD_BYTES = 60_000
payload_json = json.dumps(skill_input)
if len(payload_json.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
    logger.warning(
        f"PAYLOAD_GUARD {skill}: {len(payload_json)}B > {_MAX_PAYLOAD_BYTES}B, truncating")
    for k in sorted(skill_input, key=lambda k: len(str(skill_input[k])), reverse=True):
        v = skill_input[k]
        if isinstance(v, str) and len(v) > 2000:
            skill_input[k] = v[:2000] + f"... [truncated from {len(v)} chars]"
            payload_json = json.dumps(skill_input)
            if len(payload_json.encode("utf-8")) <= _MAX_PAYLOAD_BYTES:
                break
```

**Review questions:**
- `len(payload_json)` vs `len(payload_json.encode("utf-8"))` — the first check uses char count, the guard condition uses byte count. Is this inconsistent? (Actually both use `.encode("utf-8")` — re-check)
- The truncation loop re-serializes JSON on every iteration. With many large fields this is O(n * serialization_cost). Is that a problem at 60KB?
- If ALL fields are <2000 chars but their sum exceeds 60KB, the guard does nothing and the 413 still fires. Is that a real scenario?
- The `_MAX_PAYLOAD_BYTES` is a local variable inside the method, recomputed on every call. Should it be a class or module constant?
- `json.dumps(skill_input)` may not match the exact serialization the cockpit uses. Could there be a size discrepancy?

---

### 3. Settlement schema alignment (CR-04) in handler.py

Added to `on_inbound_settlement()`:

```python
debt_component = proposal.get("debt_component")
target_balance_component = proposal.get("target_balance_component")
has_components = debt_component is not None and target_balance_component is not None
if has_components:
    debt_component = float(debt_component)
    target_balance_component = float(target_balance_component)
    components_valid = abs((debt_component + target_balance_component) - amount) < 0.01
else:
    components_valid = True
```

And in `inbound-settlement.toml`:

```toml
[[evaluate.rules]]
field = "components_valid"
pattern = "false"
action = "reject"
```

**Review questions:**
- `float()` conversion could raise `ValueError` if the wire body contains a non-numeric string. Is there a try/except?
- The 0.01 tolerance — is this appropriate for credit values that could be very small or very large?
- If an attacker sends `debt_component = 0, target_balance_component = 0` with `amount = 0`, `components_valid` is `true` but the settlement is meaningless. Does this matter?
- The hotwire rule uses string pattern matching: `pattern = "false"`. If `components_valid` were somehow `"False"` (Python bool stringified differently), the rule wouldn't match. Is `"true"/"false"` guaranteed lowercase?

---

### 4. Payment-finalized recipe annotation

Just a comment block added. No code review needed unless you spot factual errors in the comment.

---

### 5. `mode = "disabled"` in loader.py

```python
mode = config.get("mode", "automated")
if mode == "disabled":
    logger.debug(f"Recipe skipped (disabled): {name}")
    continue
```

And `memory-update.toml`:
```toml
mode = "disabled"   # memory-update-lite not deployed in exp clusters
```

**Review questions:**
- The `continue` skips `db.upsert_recipe()` but also skips `loaded_names.append(name)`. The prune logic at the bottom removes DB rows for names NOT in `loaded_names`. So if a recipe was previously loaded and is now disabled, does it get pruned from the DB? Is that the intended behavior?
- If a recipe is disabled, should it still appear in any status/diagnostic output?

---

## What I want from you

1. For each change: PASS, CONCERN, or BUG. If CONCERN or BUG, explain precisely what could go wrong and suggest a fix.
2. One overall assessment: are these changes safe to deploy to a 101-node cluster?
3. Anything I missed — interactions between the changes, edge cases, etc.

Keep it technical. No praise, no fluff.

# knarr-thrall: Operator Guide

> **Version**: 3.7.0 (Catalog Gather + Settlement Identity + On-chain Execution)
> **Model**: gemma3:1b (L1, CPU) + ollama/openai (L2, GPU/cloud)
> **Dependencies**: `llama-cpp-python` (L1 inference), `pynacl` (identity/signing), standard knarr plugin system
> **Requires**: knarr >= 0.40.0 (tested up to v0.50.0)

---

## 1. File Layout

```
plugins/06-thrall/
├── plugin.toml          # Plugin config — trust tiers, model path, compilation settings
├── handler.py           # PluginHooks subclass — on_mail_received + on_tick + on_event
├── engine.py            # Pipeline engine — TRIGGER → FILTER → GATHER → EVALUATE → ACTION
├── evaluate.py          # LLM orchestrator — L1/L2 cascade, cost tracker, prompt rendering
├── backends.py          # LocalBackend, OllamaBackend, OpenAIBackend + factory
├── thrall_actions.py    # Action executor — log, compile, wake, act, reply, settle, memory
├── gather.py            # Context gatherer — catalog-based field gather + legacy cockpit/memory
├── gather-field-catalog.toml  # 21 fields, 6 sources, format specs
├── memory.py            # Structured decision memory (tagged records, filtered queries)
├── db.py                # ThrallDB (SQLite: journal, buffers, context, cache, memory, wallet)
├── loader.py            # Recipe/prompt TOML loader (auto-discovers *.toml)
├── identity.py          # Delegated Ed25519 keypair for settlement signing
├── wallet.py            # Scoped daily spending ceiling
├── commerce.py          # Cockpit API wrappers for economy/ledger queries
├── solana_tx.py         # Solana SPL Token-2022 transaction builder
├── recipes/             # 28 recipe TOML files (pipeline definitions)
├── prompts/             # 7 prompt TOML files (LLM prompt templates)
├── skills/              # Thrall-managed skill handlers
├── rag/                 # RAG documents for LLM context injection
├── artifacts/           # Briefing files written by compilation flush (agent reads these)
└── thrall.db            # Runtime SQLite DB (created automatically)
```

**Key rule**: knarr's PluginLoader looks for exactly `plugin.toml`. Rename to `plugin.toml.disabled` to disable the plugin without deleting files.

---

## 2. plugin.toml — Full Configuration

```toml
name = "knarr-thrall"
version = "3.0.0"
handler = "handler:ThrallPlugin"

[config]
enabled = true
debug = true                                    # verbose logging
dry_run = true                                  # pipeline runs fully, briefings tagged dry_run
ignore_msg_types = ["ack", "delivery", "system"] # message types that skip thrall entirely

[config.thrall]
enabled = true

# Model — absolute path to gemma3:1b GGUF file
model_path = "/path/to/gemma3-1b.gguf"

# Inference tuning
n_threads = 4          # CPU threads for inference (match your vCPU count)
n_ctx = 1024           # context window (1024 is enough for triage)
max_tokens = 128       # max output tokens from LLM
queue_timeout = 5.0    # seconds to wait for LLM slot before fallback action

# Cockpit API — for skill execution via the `act` action
cockpit_url = "http://127.0.0.1:8080"           # HTTP preferred over HTTPS (avoids IPv6 timeout)
cockpit_token = "your-cockpit-bearer-token"      # same token as in node cockpit config

# Trust tiers — node ID prefixes (16 chars minimum)
[config.thrall.trust_tiers]
team = ["de2a6068a517c9d7", "840d544087cbacb1"]  # team nodes bypass LLM entirely
known = ["d9196be699447a12"]                      # known nodes get lower wake threshold

[config.compilation]
interval_seconds = 3600    # timer-based compilation flush (seconds)
buffer = "mail-digest"     # which buffer the timer flushes
```

### Option Reference

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `true` | Master switch — disables all pipeline processing |
| `debug` | `false` | Extra debug logging |
| `dry_run` | `false` | Pipeline runs fully, but briefings carry `dry_run: true` flag. The agent writes a report instead of acting. |
| `model_path` | (none) | **Required**. Absolute path to GGUF model file. |
| `n_threads` | `4` | CPU threads for inference. Match your vCPU count. |
| `n_ctx` | `1024` | LLM context window. 1024 is enough for mail triage. |
| `max_tokens` | `128` | Max LLM output tokens. Keep small for classification. |
| `queue_timeout` | `5.0` | Seconds to wait for the LLM inference slot. If busy, uses fallback action. |
| `cockpit_url` | `http://127.0.0.1:8080` | Local cockpit URL for skill execution. Use 127.0.0.1, not localhost (Python urllib IPv6 timeout issue on Windows). |
| `cockpit_token` | (none) | Bearer token for cockpit API authentication. Can also be loaded from vault. |
| `trust_tiers.team` | `[]` | Node ID prefixes for team tier (bypass LLM, instant wake). |
| `trust_tiers.known` | `[]` | Node ID prefixes for known tier (lower threshold for wake). |
| `compilation.interval_seconds` | `3600` | Timer flush interval for compilation buffers. |
| `compilation.buffer` | `mail-digest` | Which buffer the timer flush targets. |

---

## 3. Model Setup

Thrall uses **gemma3:1b** via `llama-cpp-python`. No ollama, no GPU, pure CPU inference.

### Getting the model

```bash
# Option A: Download from ollama registry, extract GGUF
ollama pull gemma3:1b
# GGUF blob is at: ~/.ollama/models/blobs/sha256-7cd4618c...
# Copy it to your preferred location

# Option B: Download GGUF directly from HuggingFace
# Search for "gemma-3-1b-it" GGUF — any Q4_K_M quantization works
```

The model file is approximately **778 MB**.

### Where it lives

Place the GGUF anywhere accessible by the node process. Set the absolute path in `plugin.toml`:

```toml
model_path = "/models/gemma3-1b.gguf"
```

**Docker**: Mount as a read-only volume:
```yaml
volumes:
  - ./models/gemma3-1b.gguf:/app/models/gemma3-1b.gguf:ro
```

### What happens if it's missing

- If `model_path` is not set or the file doesn't exist: **the LLM evaluator fails to load**. Recipes using `type = "llm"` in their evaluate stage will fall back to their `fallback_action` (typically `compile`).
- Recipes using `type = "hotwire"` (static rules, no LLM) still work fine.
- Trust tier bypass (team nodes) still works — it never calls the LLM.

### Performance expectations

| Environment | Hot inference | Cold load (first call) | RAM |
|-------------|-------------|----------------------|-----|
| Desktop (4+ cores) | 500ms–2.5s | ~10s | ~1.2 GB |
| Docker (2 vCPU) | 600ms | ~5s | ~1.2 GB |
| Hetzner CX23 (2 vCPU, 4GB) | 1.3–2.4s | ~10s | ~1.2 GB |

The model loads lazily on first LLM call. Subsequent calls reuse the loaded model (singleton).

### Install llama-cpp-python

```bash
pip install llama-cpp-python

# For CPU with OpenBLAS acceleration (recommended):
CMAKE_ARGS="-DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS" pip install llama-cpp-python

# Docker: also install libgomp1 at runtime, pkg-config at build time
```

---

## 4. Trust Tier Configuration

Trust tiers control how aggressively thrall filters inbound messages.

```toml
[config.thrall.trust_tiers]
team = ["de2a6068a517c9d7", "840d544087cbacb1"]
known = ["d9196be699447a12"]
```

Each entry is a **node ID prefix** (minimum 16 characters). Thrall matches the sender's node ID against these prefixes.

### Tier behavior

| Tier | LLM call? | Default action | Latency |
|------|-----------|---------------|---------|
| **team** | No — bypassed | `wake` (instant summon) | 0ms |
| **known** | Yes (or cached) | Per LLM classification | 500ms–2.5s |
| **unknown** | Yes | Per LLM classification (prefer compile) | 500ms–2.5s |

- **team**: Full trust. Messages skip the LLM entirely and go straight to the `bypass_action` (default: `wake`). Use for your own nodes and trusted partners.
- **known**: Partial trust. Messages go through LLM classification but with a lower bar for `wake`. Use for peers you've interacted with.
- **unknown**: Zero trust. All messages get LLM classification. The triage prompt instructs the model to prefer `compile` over `wake` for unknown senders.

### Adding nodes

Get the node ID from the peer table (first 16+ chars):
```sql
SELECT SUBSTR(node_id, 1, 16), host, port FROM peers;
```

Add the prefix to the appropriate tier in plugin.toml and restart the provider.

---

## 5. Recipe Examples

Recipes are TOML files in the `recipes/` directory. Each recipe defines a pipeline:

**TRIGGER** → **FILTER** → **EVALUATE** → **ACTION**

### Example 1: Mail Triage (LLM classification)

```toml
# recipes/mail-triage.toml
# Classifies inbound text/offer mail using the LLM.
# Team nodes bypass LLM. Known/unknown get classified.

mode = "automated"

[trigger]
type = "on_mail"
msg_types = ["text", "offer"]        # only match these message types

[filter]
trust_bypass = true                   # team tier skips LLM
bypass_action = "wake"                # team mail always wakes agent

rate_limit_window = 60                # per-sender rate limit
rate_limit_max = 5                    # max 5 messages per sender per 60s
rate_limit_action = "compile"         # rate-limited → buffer instead of wake

cache_ttl = 120                       # reuse LLM decision for same sender within 2 min

[evaluate]
type = "llm"                          # uses the LLM for classification
prompt = "triage"                     # references prompts/triage.toml
model = "gemma3-1b"
fallback_action = "compile"           # if LLM fails or busy → buffer

[actions.compile]
buffer = "mail-digest"
summon_threshold = 10                 # 10+ messages → flush and summon agent
summon_keywords = ["BUG", "DOWN", "URGENT", "CRITICAL", "SECURITY"]

[actions.wake]
# Direct summon — agent wakes immediately with full message context

[actions.reply]
template = "Message received. Processing."
```

### Example 2: Cluster Health Probe (skill execution)

```toml
# recipes/cluster-probe.toml
# Calls a canary skill on tick to verify the execution pipeline works.
# Cooldown-throttled to once per 5 minutes.

mode = "automated"

[trigger]
type = "on_tick"

[filter]
cooldown_key = "cluster-probe"
cooldown_seconds = 300                # at most once per 5 minutes

[evaluate]
type = "hotwire"                      # no LLM — static rules only
default_action = "act"                # always execute the skill

[actions.act]
skill = "knarr-mail"                  # skill to call via cockpit API
input = { action = "poll", status_filter = "read", limit = "1" }
error_buffer = "probe-errors"         # compile errors for review
```

### Example 3: Health Check (peer monitoring)

```toml
# recipes/health-check.toml
# Monitors peer count on tick. Fires when network looks thin.

mode = "automated"

[trigger]
type = "on_tick"

[filter]
cooldown_key = "health-check"
cooldown_seconds = 300

[evaluate]
type = "hotwire"
default_action = "log"                # default: just log, no action

[[evaluate.rules]]                    # hotwire rule: 0 or 1 peers = isolated
field = "peer_count"
pattern = "^[0-1]$"
action = "compile"

[actions.compile]
buffer = "health-alerts"
summon_threshold = 3                  # 3 isolation events → summon agent
```

### Recipe reference

**Trigger types**: `on_mail`, `on_tick`, `on_event`

**Filter options**:
- `trust_bypass` — team tier skips LLM (bool)
- `bypass_action` — action for bypassed messages (default: `wake`)
- `cooldown_key` / `cooldown_seconds` — per-recipe cooldown
- `rate_limit_window` / `rate_limit_max` / `rate_limit_action` — per-sender rate limit
- `cache_ttl` — reuse LLM decisions for same sender

**Evaluate types**:
- `llm` — LLM classification (requires model). Uses `prompt`, `model`, `fallback_action`.
- `hotwire` — static pattern matching (no LLM). Uses `rules` array and `default_action`.

**Hotwire rules**: `field` (envelope field name), `pattern` (regex), `action` (pipeline action).

**Actions**: `log`/`drop`, `compile`, `wake`/`summon`, `act`, `reply`, `settle`, `memory`

**Envelope fields available** (on_mail): `from_node`, `to_node`, `msg_type`, `body_text`, `body_json`, `session_id`
**Envelope fields available** (on_tick): `peer_count`, `tick`

---

## 6. Prompt Templates

Prompts live in `prompts/` as TOML files. They define the LLM system prompt for classification.

```toml
# prompts/triage.toml
name = "triage"
version = "3.0"
model = "gemma3-1b"

content = """You classify inbound P2P network messages. Reply with exactly one JSON object.

Valid actions: drop, compile, wake.
- drop: spam, noise, acknowledgments
- compile: routine messages, status updates, non-urgent
- wake: urgent questions, collaboration requests, bug reports

Sender trust tier: {{envelope.msg_type}} from {{filter.tier}} node.

Output format: {"action":"drop"|"compile"|"wake","reason":"brief explanation"}

Now classify this message:
{{envelope.body_text}}"""
```

### Template variables

| Variable | Description |
|----------|-------------|
| `{{envelope.from_node}}` | Sender node ID |
| `{{envelope.to_node}}` | Recipient node ID |
| `{{envelope.msg_type}}` | Message type (text, offer, etc.) |
| `{{envelope.body_text}}` | Message body as text |
| `{{envelope.session_id}}` | Session ID if present |
| `{{filter.tier}}` | Resolved trust tier (team/known/unknown) |
| `{{filter.decision}}` | Filter stage decision |
| `{{context.*}}` | Session context from DB |

---

## 7. Verification

After deploying, verify thrall is working:

### Check plugin loaded

```bash
# In the provider log, look for:
grep "knarr-thrall" logs/serve.log | tail -5
# Should show: "Loaded plugin: knarr-thrall v3.7.0"
# And: "Thrall switchboard ready: {'recipes': 28, 'prompts': 7}"
```

### Send a test mail

From another node (or self):
```bash
# Send a test message to the thrall node
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"skill":"knarr-mail","input":{"action":"send","to":"<node_id>","body":"Hello, testing thrall triage"}}' \
  http://127.0.0.1:8080/api/execute
```

Check the log for pipeline execution:
```bash
grep "PIPELINE mail-triage" logs/serve.log | tail -5
# Should show: "PIPELINE mail-triage: filter=bypass eval=bypass->wake action=summon wall=30ms"
# (for team sender) or similar with eval=llm for unknown senders
```

### Check the journal

```python
import sqlite3
db = sqlite3.connect("plugins/06-thrall/thrall.db")
db.row_factory = sqlite3.Row
for r in db.execute("SELECT pipeline, eval_type, action_name, wall_ms FROM thrall_journal ORDER BY timestamp DESC LIMIT 10"):
    print(f"{r['pipeline']:20s} eval={r['eval_type']:10s} action={r['action_name']:12s} wall={r['wall_ms']}ms")
```

### Use thrall-inject for testing

If the `thrall-inject` skill is registered:
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"skill":"thrall-inject","input":{"recipe":"mail-triage","body":"BUG: skill timeout","from_node":"unknown_test_node"}}' \
  http://127.0.0.1:8080/api/execute
```

This runs the pipeline without real mail, using `mode=manual` (logs but doesn't execute actions).

---

## 8. Troubleshooting

### Model fails to load

```
ERROR thrall.evaluate: Failed to load model: ...
```

- Verify `model_path` points to a valid GGUF file
- Ensure `llama-cpp-python` is installed: `pip show llama-cpp-python`
- Check file permissions — the node process must be able to read the model file
- The model loads lazily on first LLM call. If no mail arrives, you won't see load errors until one does.

### Recipes not loading

```
WARNING thrall.loader: Recipes directory not found
```

- Ensure `recipes/` dir exists inside the plugin directory
- Recipe files must end in `.toml`
- Check TOML syntax: `python -c "import tomllib; tomllib.load(open('recipe.toml','rb'))"`

### Cooldown not working (probe fires every tick)

The cooldown context must be written after a successful pipeline run. If the journal shows the recipe running but the cooldown never takes effect, check that `engine.py` writes the cooldown context after the action stage.

### LLM queue timeout

```
WARNING thrall.evaluate: LLM_QUEUE_FULL: slot busy >5.0s
```

Only one inference runs at a time (Semaphore(1)). If a new request arrives while one is running, it waits up to `queue_timeout` seconds. If the slot is still busy, the fallback action is used (typically `compile`). This is by design — prevents thread-pool starvation.

Increase `queue_timeout` if you want longer waits, or accept compile as the overflow behavior.

### No cockpit token

```
WARNING knarr.plugin.knarr-thrall: call_skill: no cockpit token
```

Add `cockpit_token` to `[config.thrall]` in plugin.toml. The token must match your cockpit bearer token. Alternatively, store it in the vault and thrall will load it via `ctx.vault_get("cockpit_token")`.

### Sentinel reload

To reload recipes and prompts without restarting:

```bash
touch plugins/06-thrall/thrall.reload
```

Thrall checks this file on tick (~60s). Only recipes and prompts reload — **handler.py, engine.py, actions.py, and plugin.toml changes require a full restart.**

After code changes, always clear bytecode cache before restart:
```bash
rm -rf plugins/06-thrall/__pycache__
```

### `gemma3` multimodal content format

The gemma3 chat template requires multimodal content format:
```python
messages=[
    {"role": "system", "content": [{"type": "text", "text": "..."}]},
    {"role": "user", "content": [{"type": "text", "text": "..."}]},
]
```

Using plain string `content` will fail with gemma3 models. This is handled by the evaluator automatically.

### Python urllib localhost slowness (Windows)

Python's `urllib` resolves `localhost` via IPv6 first, which can add ~2s per request on Windows. Always use `127.0.0.1` in `cockpit_url`, not `localhost`.

---

## 9. Architecture Summary

```
Inbound mail / tick event
        │
        ▼
   ┌─────────┐
   │ TRIGGER  │  Match recipes by trigger type (on_mail / on_tick)
   └────┬─────┘
        │
        ▼
   ┌─────────┐
   │ FILTER   │  Trust bypass, rate limit, cooldown, LLM cache
   └────┬─────┘
        │
        ▼
   ┌──────────┐
   │ EVALUATE  │  LLM classification (gemma3:1b) or hotwire (static rules)
   └────┬──────┘
        │
        ▼
   ┌─────────┐
   │ ACTION   │  log / compile / summon / act / reply
   └────┬─────┘
        │
        ▼
   ┌─────────┐
   │ JOURNAL  │  Every execution recorded in thrall.db
   └─────────┘
```

**Concurrency model**: One LLM inference at a time (asyncio.Semaphore). Overflow → fallback action (compile). The pipeline engine itself is async and non-blocking.

**Wake modes**:
- **respond**: Fire-and-forget briefing via system mail. Agent gets full message context and can reply in one shot.
- **process**: Orientation brief + artifact file. Agent reads the briefing artifact, then acts.

**Compilation**: Messages can be buffered into named compilation buffers. Three flush triggers: timer (hourly), threshold (N+ messages), keyword (BUG/DOWN/URGENT). Flush writes a markdown briefing to `artifacts/` and summons the agent.

---

## 10. thrall-tune-lite — Runtime Tuning Skill

The `thrall-tune-lite` skill provides a runtime API for agents (or operators via cockpit) to observe, correct, and tune thrall's pipeline decisions without restarting or editing files.

**Registration** (in `knarr.toml`):
```toml
[skills.thrall-tune-lite]
handler = "skills/thrall_tune.py:handle"
price = 0
visibility = "private"
```

### Actions

All actions use `{"action": "<name>", ...}` as input.

| Action | Description | Key params |
|--------|-------------|------------|
| `stats` | Pipeline performance summary | `hours` (default 24), `pipeline` (filter) |
| `journal` | Recent pipeline decisions | `limit` (default 20), `pipeline`, `reviewed` (0/1) |
| `correct` | Mark a decision as wrong | `journal_id`, `correction` (free text) |
| `get_recipe` | Read recipe config (or list all) | `name` (omit to list all) |
| `set_threshold` | Adjust a recipe parameter at runtime | `recipe`, `path` (dot-separated), `value` |
| `set_prompt` | Update a prompt template | `name`, `content` |
| `wallet` | Wallet spending summary | `hours` (default 24) |

### Examples

**Check pipeline stats** (last 24h):
```json
{"action": "stats", "hours": "24"}
```
Returns: event count, LLM hit rate, cache rate, bypass/hotwire/rate-limited counts, backend info.

**Review unreviewed decisions**:
```json
{"action": "journal", "reviewed": "0", "limit": "10"}
```

**Correct a misclassification**:
```json
{"action": "correct", "journal_id": "42", "correction": "should_have_been:wake reason:urgent security alert misclassified as routine"}
```

**Adjust a recipe threshold at runtime** (no restart needed):
```json
{"action": "set_threshold", "recipe": "mail-triage", "path": "actions.compile.summon_threshold", "value": "5"}
```
This updates both the DB (runtime source of truth) and the TOML source file (if `tomli_w` is available). Touches `thrall.reload` to trigger hot-reload.

**Check wallet spending**:
```json
{"action": "wallet", "hours": "12"}
```
Returns: daily spend, ceiling, utilization %, recent transaction history.

### Architecture notes

- Uses the same `thrall.db` as the running pipeline (WAL mode = safe concurrent reads)
- `set_threshold` and `set_prompt` touch the `thrall.reload` sentinel to trigger hot-reload
- The skill resolves `_THRALL_DIR` relative to the skill file location (`../plugins/06-thrall/`)
- Private, zero-cost — intended for agent self-tuning loops, not external consumers

---

## 11. Recipe Pruning

When recipes are loaded from disk, the loader now prunes stale DB entries for recipes that no longer have a corresponding TOML file. This prevents ghost recipes from accumulating after renames or deletions.

The pruning happens automatically on every reload (startup + sentinel touch). No operator action needed.

---

*Written by Viggo (provider node), running thrall v3.7.0 live since 2026-02-28. Updated 2026-03-23.*

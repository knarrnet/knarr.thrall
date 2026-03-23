# knarr-thrall: Autonomous Switchboard Plugin

**Version**: 3.7.0
**Requires**: knarr >= 0.40.0 (tested up to v0.50.0), PyNaCl, base58 (optional)

## What is thrall?

Thrall is a **plugin** that gives your knarr node autonomous intelligence. It intercepts inbound messages, classifies them through a two-stage LLM cascade, and executes actions based on configurable recipes — all without waking your agent or burning API credits on noise.

**Core pipeline:**
```
Inbound -> TRIGGER -> FILTER -> GATHER -> EVALUATE -> ACTION -> Journal/Memory
            on_mail    trust     cockpit    L1->L2     drop/log
            on_event   tiers     memory     cascade    compile
            on_tick    rate      static     hotwire    wake/summon
                       limit                           act (skill)
                       cache                           reply
                       cooldown                        settle
```

## Architecture

### Two-stage LLM Cascade

- **L1** (gemma3:1b, CPU, llama-cpp-python, ~2s): Binary drop/escalate. Runs outside the L2 semaphore. Drops ~50% of traffic.
- **L2** (qwen3:32b, GPU, ollama, ~5s): Full classification with context. Gated by `Semaphore(1)`.
- L1 saves L2 for substantive mail only.

### Three Backends

Swappable via `plugin.toml [config.thrall]`:

| Backend | Engine | Cost | Use case |
|---------|--------|------|----------|
| `local` | llama-cpp-python (CPU) | Zero | Default, VPS, low traffic |
| `ollama` | HTTP to local/LAN ollama | Zero | GPU available, higher quality |
| `openai` | Any OpenAI-compatible API | Metered | Cloud models, highest quality |

Hot-swap via sentinel reload (touch `thrall.reload`).

### Recipe Engine

Recipes are TOML files in `recipes/` that define autonomous behaviors:

```toml
mode = "automated"

[trigger]
type = "on_tick"              # on_mail | on_event | on_tick

[filter]
cooldown_key = "health-check"
cooldown_seconds = 300

[evaluate]
type = "hotwire"              # llm | hotwire (pattern match, no LLM)
default_action = "act"

[actions.act]
skill = "health-check-lite"
input = {}
error_buffer = "health-errors"
```

### Context Gather Stage (v3.4.0 + v3.7.0 catalog)

Pipeline stage between FILTER and EVALUATE. Two modes:

**Catalog fields (v3.7.0)** — declare field names, engine deduplicates by source:

```toml
# 3 fields, 2 API calls (economy + wallet). Source dedup automatic.
gather = ["settlement_candidates", "net_position", "daily_spend"]
```

21 fields across 6 sources: `status`, `economy`, `wallet`, `journal`, `peers`, `probe`.
Glob syntax supported: `gather = ["economy.*"]` expands to all economy fields.
Catalog defined in `gather-field-catalog.toml`.

**Legacy [[gather]] blocks** — for recipes needing envelope templating (e.g. `{{peer_pk}}`):

```toml
[[gather]]
name = "positions"
source = "cockpit"
endpoint = "/api/positions"

[[gather]]
name = "peer_history"
source = "memory"
query = { node_id = "{{peer_pk}}", limit = "5" }
```

Both modes coexist. Engine auto-detects: list of strings → catalog, list of dicts → legacy.

### Structured Memory (v3.4.0)

Classified decision memory replacing unstructured journals. Every record tagged with skill, node_id, outcome for targeted recall.

- Query: "Last 5 settlements with peer X", "Rejection rate for skill Y"
- Dryrun isolation: `dryrun=1` records excluded from operational queries
- Two consumers: recipes (via gather stage) and agent plugin (via DB)

### Knowledge-as-a-Service (v3.10.0)

Agents can acquire domain knowledge from the network via skill calls. Knowledge packs contain markdown files + optional recipes, signed with Ed25519.

**Acquire:** Call a knowledge skill (e.g., `knowledge-casino-lite`). Pack is validated (SHA256 + signature), files saved locally, chunks embedded into sqlite-vec (FTS5 fallback).

**Query:** Recipes declare `gather = ["knowledge.casino"]`. During gather stage, top-K relevant chunks are retrieved via similarity search and injected into the prompt.

**Trust levels:**
- `none` (default) — files saved, no recipes loaded
- `trusted` — recipes loaded with allowlist validation (only `log` + `act`, hotwire eval, >=300s cooldown)
- `unsafe` — all recipe actions allowed

**Config:**
```toml
[config.thrall.knowledge]
trust_level = "none"
max_domains = 50
max_pack_bytes = 5242880
embedding_source = "l1"
```

### Included Recipes (28)

| Recipe | Trigger | Eval | Purpose |
|--------|---------|------|---------|
| `mail-triage` | on_mail | llm | LLM classification of inbound mail |
| `mail-digest` | on_tick | hotwire | Buffered compilation for batch processing |
| `health-check` | on_tick | hotwire | Periodic skill chain canary |
| `cluster-probe` | on_tick | hotwire | Execution pipeline probe |
| `cluster-monitor` | on_tick | hotwire | Cluster health monitoring |
| `credit-warning` | on_event | hotwire | Credit limit warnings |
| `security-alert` | on_event | hotwire | Security event monitoring |
| `task-failures` | on_event | hotwire | Task failure monitoring |
| `queue-backpressure` | on_tick | hotwire | Queue congestion detection |
| `selftest` | on_tick | hotwire | 7-point self-test |
| `swarm-probe` | on_tick | hotwire+gather | Swarm probe with economic context (catalog gather v3.7) |
| `settlement-check` | on_tick | hotwire+gather | Autonomous netting proposals (v3.3, catalog gather v3.7) |
| `strategic-advisor` | on_tick | hotwire+gather | Strategic advisor refresh with economic context |
| `strategic-advisor-refresh` | on_tick | hotwire | Periodic advisor data refresh |
| `memory-update` | on_tick | hotwire | Structured memory maintenance |
| `cashout-trigger` | on_event | hotwire | On-chain cashout trigger |
| `chat` | on_mail | llm | Conversational chat |
| `concierge-intake` | on_mail | llm | Service intake classification |
| `concierge-faq` | on_mail | llm | FAQ auto-response |
| `concierge-expert` | on_mail | llm | Expert routing with gather |
| `settlement-review` | on_mail | llm+gather | Inbound settlement review with memory |
| `inbound-settlement` | on_mail | hotwire | Settle_request message handler |
| `wm-review` | on_event | hotwire | WM quarantine document review (v3.5) |
| `payment-received` | on_event | hotwire | BCW payment included in block (v3.5) |
| `payment-finalized` | on_event | hotwire | BCW payment reached finality (v3.5) |
| `payment-receipt-notify` | on_event | hotwire | Payment receipt notification (v3.5) |
| `settlement-execute` | on_event | hotwire | On-chain settlement execution (v3.5) |
| `punchhole-test` | on_tick | hotwire | Network punchhole connectivity test |

### Included Prompts (7)

| Prompt | Used by | Purpose |
|--------|---------|---------|
| `triage` | mail-triage | L2 full classification |
| `triage-l1` | cascade L1 | Binary drop/escalate |
| `chat` | chat recipe | Conversational responses |
| `concierge-intake` | concierge | Service intake |
| `concierge-faq` | concierge | FAQ matching |
| `concierge-expert` | concierge | Expert analysis |
| `greeter` | greeter demo | Peer assessment [DEMO] |

## Settlement Identity (v3.3.0)

Thrall can autonomously sign netting settlement proposals using its own delegated Ed25519 keypair, separate from the node's identity.

### Components

| File | Purpose |
|------|---------|
| `identity.py` | Delegated Ed25519 keypair — generated on first init, config-gated, revocable |
| `wallet.py` | Scoped daily spending ceiling — prevents runaway autonomous spending |
| `commerce.py` | Cockpit API wrappers — queries ledger, economy, receipts via HTTP |
| `skills/settlement_check.py` | Skill handler — finds over-threshold positions, signs netting proposals |
| `recipes/settlement-check.toml` | Hourly on_tick recipe, hotwire eval, no LLM |

### How it works

1. Every hour, the settlement-check recipe fires
2. Queries bilateral ledger positions via cockpit API
3. Finds positions above the soft threshold (default 80% utilization)
4. For each: checks wallet ceiling, builds a signed netting proposal
5. Submits via knarr-mail as `knarr/commerce/settle_request`

### Configuration

```toml
[config.thrall.identity]
enabled = true
keyfile = "thrall_identity.key"   # auto-generated 32-byte Ed25519 seed

[config.thrall.wallet]
ceiling = 50.0                    # max credits per day (resets at midnight UTC)
```

### Revocation

Delete `thrall_identity.key` and restart. Thrall logs `IDENTITY_DISABLED` and settlement-check returns `status: "disabled"`.

## Quick Start

### 1. Add plugin files

```
your-node/
  plugins/
    06-thrall/
      handler.py, engine.py, evaluate.py, backends.py,
      thrall_actions.py, gather.py, memory.py, db.py,
      loader.py, identity.py, wallet.py, commerce.py,
      __init__.py, plugin.toml
      recipes/           # 18 recipe TOML files
      prompts/           # 7 prompt TOML files
```

### 2. Get model files

**L1 (required):** gemma3:1b (~778 MB GGUF)
```bash
ollama pull gemma3:1b
# Copy the GGUF blob to your models directory
```

**L2 (optional, for cascade):** qwen3:32b via ollama
```bash
ollama pull qwen3:32b
```

### 3. Install dependencies

```bash
# For local backend (CPU inference)
pip install llama-cpp-python

# For identity/signing
pip install pynacl
```

### 4. Configure plugin.toml

Edit `plugin.toml` — at minimum set:
- `cockpit_token` — your cockpit Bearer token
- `[config.thrall.local] model_path` — path to gemma3:1b GGUF
- `[config.thrall.trust_tiers]` — your team and known peer node ID prefixes

### 5. Register settlement skill (optional)

In your `knarr.toml`:
```toml
[skills.settlement-check-lite]
description = "Autonomous settlement position check"
handler = "skills/settlement_check.py:handle"
input_schema = {threshold = "string"}
output_schema = {status = "string", positions_checked = "string", settlements_proposed = "string", total_amount = "string", wall_ms = "string"}
price = 0
tags = ["settlement", "thrall", "internal"]
visibility = "public"
allowed_nodes = ["your-node-id-prefix"]
```

### 6. Restart node

```bash
knarr serve --config knarr.toml
```

Expected log output:
```
INFO thrall.loader: Recipe loaded: mail-triage (mode=automated)
INFO thrall.loader: Recipe loaded: settlement-check (mode=automated)
INFO thrall.engine: Loaded 22 recipes
INFO thrall.identity: IDENTITY_LOADED public_key=b84d1bc2...
INFO thrall.wallet: WALLET_INIT ceiling=50.0 daily_spent=0.0 remaining=50.0
INFO thrall.gather: CATALOG loaded: 21 fields from .../gather-field-catalog.toml
INFO knarr.dht.plugins: Loaded plugin: knarr-thrall v3.7.0
```

## Trust Tiers

```toml
[config.thrall.trust_tiers]
team = ["ad8d21d81a497993"]    # your own nodes — instant pass, 0ms
known = ["d9196be699447a12"]   # trusted peers — LLM classifies, lower bar
# unknown: everyone else      — LLM classifies, higher bar
```

## File Reference

### Core

| File | Purpose |
|------|---------|
| `handler.py` | Plugin entry, hooks, bus consumer, subsystem init, pipeline orchestration |
| `engine.py` | Pipeline engine (TRIGGER->FILTER->GATHER->EVALUATE->ACTION) |
| `evaluate.py` | LLM orchestrator, L1/L2 cascade, cost tracker, prompt rendering |
| `backends.py` | LocalBackend, OllamaBackend, OpenAIBackend + factory |
| `thrall_actions.py` | Action executor (log, compile, wake, act, reply, settle, memory) |
| `gather.py` | Context gatherer — catalog-based field gather (v3.7) + legacy cockpit/memory/static |
| `gather-field-catalog.toml` | Field catalog: 21 fields, 6 sources, format specs |
| `memory.py` | Structured decision memory (tagged records, filtered queries) |
| `db.py` | ThrallDB (SQLite: journal, buffers, context, cache, memory, wallet) |
| `loader.py` | Recipe/prompt TOML loader (auto-discovers *.toml) |

### Settlement Identity (v3.3.0) + On-chain Execution (v3.5.0)

| File | Purpose |
|------|---------|
| `identity.py` | Delegated Ed25519 keypair, eddsa-jcs-2022 signing, Solana address derivation, revocable |
| `wallet.py` | Scoped daily ceiling, can_spend/record_spend |
| `commerce.py` | Cockpit API wrappers, position check, netting doc builder, WM quarantine ops, Solana devnet RPC |
| `solana_tx.py` | Solana transaction builder for SPL Token-2022 transfers |
| `skills/settlement_check.py` | Autonomous netting position check skill |
| `skills/settlement_execute.py` | On-chain settlement execution skill (v3.5) |
| `skills/strategic_advisor_lite.py` | Strategic advisor skill with economic context |
| `skills/thrall_tune.py` | Runtime tuning API (stats, journal, correct, set_threshold) |

### RAG Documents

| File | Purpose |
|------|---------|
| `rag/90-memory-operations.md` | Memory operation reference for LLM context |
| `rag/91-memory-peers.md` | Peer memory patterns for LLM context |

## Database

Thrall uses its own `thrall.db` (SQLite, in the plugin directory):

| Table | Purpose |
|-------|---------|
| `thrall_journal` | All classification decisions + actions |
| `thrall_buffers` | Compilation buffers for batch processing |
| `thrall_context` | Conversation context per sender |
| `thrall_cache` | Response cache (dedup) |
| `thrall_memory` | Structured decision records (v3.4.0) |
| `thrall_wallet` | Settlement spending ledger (v3.3.0) |

## Dry Run Mode

Set `dry_run = true` in plugin.toml to observe thrall's judgment without consequences. Actions are logged to `artifacts/dryrun-{timestamp}.md` instead of being executed.

## Performance

| Metric | L1 (CPU) | L2 (GPU) | Hotwire |
|--------|----------|----------|---------|
| Per-message | ~2s | ~5s | <1ms |
| Team bypass | 0ms | 0ms | 0ms |
| RAM | ~1.2GB | 26MB (client) | 0 |

L1 drops ~50% of traffic before L2. Hotwire recipes (on_tick, on_event) skip the LLM entirely.

## Design Principles

- **Plugin, not agent**: Thrall goes through PluginContext API only. Every action has a manual equivalent. Thrall is optional intelligence.
- **Recipes over code**: New behaviors are TOML files, not Python code. Drop a recipe in `recipes/`, touch `thrall.reload`.
- **No LLM in the settlement path**: Settlement-check uses hotwire evaluation — zero inference cost.
- **Delegated identity**: Thrall signs with its own Ed25519 key, not the node key. Revocable by deleting the keyfile.
- **Scoped spending**: Wallet ceiling prevents runaway autonomous spending.

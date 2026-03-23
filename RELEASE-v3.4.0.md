# knarr-thrall v3.4.0 Release Notes

**Date:** 2026-03-04
**From:** Viggo (Provider Node, de2a6068)
**Status:** Production (live on Viggo provider node)

---

## Summary

Major release. Three new subsystems (structured memory, context gathering, settlement commerce) plus concierge recipes and swarm probe integration. File count: 13 Python modules (+6), 18 recipes (+8), 7 prompts (+4).

---

## New Subsystems

### Structured Memory (`memory.py`) — NEW

Classified decision memory, replacing unstructured journal entries. Every record is tagged with skill, node_id, mail_id, and outcome for targeted recall.

- **Query patterns**: "Last 5 settlements with peer X", "Rejection rate for peer Y", "All outcomes for skill Z in last 24h"
- **Dryrun isolation**: Records marked `dryrun=1` are excluded from operational queries — swarm experiments get clean data separation
- **Two consumers**: Thrall recipes (via gather stage) and agent plugin (via DB direct)
- **DB table**: `thrall_memory` with indexed columns for fast filtered queries

### Context Gather Stage (`gather.py`) — NEW

New pipeline stage inserted between `[filter]` and `[evaluate]`. Fetches contextual data before the LLM makes a decision — the "event-based research" pattern.

Three data sources:
- **cockpit** — HTTP GET to cockpit API (economy, ledger, receipts, positions)
- **memory** — Query thrall structured memory (peer history, outcomes)
- **static** — Literal values (template defaults, thresholds)

Recipe config:
```toml
[[gather]]
name = "positions"
source = "cockpit"
endpoint = "/api/positions"
threshold = "0.6"

[[gather]]
name = "peer_history"
source = "memory"
format = "prompt"
query = { node_id = "{{peer_pk}}", skill = "settlement-review", limit = "5" }
```

Results injected into envelope as `gather.{name}` fields, available to evaluate and action stages.

### Settlement Commerce (`identity.py`, `wallet.py`, `commerce.py`) — NEW

Autonomous settlement capability for thrall, operating under operator-defined guardrails.

**Delegated Identity** (`identity.py`):
- Own Ed25519 keypair, separate from node identity
- Signs with `did:knarr:{node_id}#thrall-1` verification method (distinct from `#key-1`)
- Aligned with v0.35.0+ eddsa-jcs-2022 signing format
- Keyfile stored in plugin directory, config-gated
- Revocable by deleting keyfile

**Scoped Wallet** (`wallet.py`):
- Daily spending ceiling (default 50.0 credits)
- Resets at midnight UTC
- Every settlement proposal deducted from ceiling, even before execution
- Prevents runaway autonomous spending

**Commerce Wrappers** (`commerce.py`):
- `query_ledger()` — bilateral positions via cockpit API
- `get_economy()` — aggregated economy summary
- `query_receipt(ref)` — receipt lookup (PluginContext or HTTP fallback)
- `check_positions(threshold)` — find over-utilized positions
- `build_netting_doc()` — signed netting proposal (eddsa-jcs-2022)
- `submit_settlement()` — send settle_request mail to peer

---

## New Recipes (+8)

| Recipe | Trigger | Evaluation | Purpose |
|--------|---------|------------|---------|
| `concierge-faq.toml` | on_mail | LLM | Protocol FAQ — answers common questions about knarr |
| `concierge-intake.toml` | on_mail | LLM | Service intake — classifies what the caller needs |
| `concierge-expert.toml` | on_mail | LLM | Deep expertise — detailed answers with context gather |
| `settlement-check.toml` | on_tick (1h) | hotwire | Periodic position scan, proposes settlements |
| `settlement-review.toml` | on_mail | LLM + gather | Reviews inbound settlement proposals with memory |
| `inbound-settlement.toml` | on_mail | hotwire | Handles incoming settle_request messages |
| `swarm-probe.toml` | on_tick | hotwire | Autonomous skill probing for swarm experiments |
| `chat.toml` | on_mail | LLM | General conversational chat recipe |

### Concierge (3 recipes)

Three-tier protocol support bundle:
1. **FAQ** — fast answers to common questions (credit system, mail, skills)
2. **Intake** — classifies the request type and routes to appropriate handler
3. **Expert** — detailed contextual answers using gather stage for live data

### Settlement (3 recipes)

Autonomous settlement lifecycle:
1. **Check** — hourly scan of bilateral positions, proposes settlements above threshold
2. **Review** — LLM-evaluated review of inbound settlement proposals, with peer history from memory
3. **Inbound** — hotwire handler for settle_request messages

---

## New Prompts (+4)

| Prompt | Used By | Description |
|--------|---------|-------------|
| `concierge-faq.toml` | concierge-faq recipe | Protocol knowledge base, formatted answer |
| `concierge-intake.toml` | concierge-intake recipe | Service classification prompt |
| `concierge-expert.toml` | concierge-expert recipe | Deep analysis with gathered context |
| `chat.toml` | chat recipe | General-purpose conversational prompt |

---

## Core Module Changes

### `handler.py` — Major refactor
- Initialization of new subsystems (ThrallMemory, ThrallIdentity, ThrallWallet, ThrallCommerce)
- Context gather integration in pipeline
- Settlement-aware mail processing
- Memory recording after every decision
- Ganglion integration for wake/escalation decisions

### `engine.py` — Gather stage integration
- New `[[gather]]` pipeline stage between filter and evaluate
- Template variable expansion in gather configs (`{{field}}` syntax)
- Envelope enrichment with gathered data before LLM evaluation

### `evaluate.py` — Memory-aware evaluation
- Gathered context injected into LLM prompts
- Memory-based few-shot examples from past decisions
- Cost tracking improvements

### `backends.py` — Stability improvements
- OpenAI backend reliability fixes
- Timeout handling improvements across all backends
- Better error classification (transient vs permanent)

### `db.py` — New tables
- `thrall_memory` — structured decision records with indexes
- `thrall_wallet` — daily spend tracking for scoped wallet
- Migration support for schema evolution

### `loader.py` — Extended recipe format
- `[[gather]]` section parsing
- Concierge recipe type support
- Settlement recipe type support

### `thrall_actions.py` (renamed from `actions.py`)
- Settlement action: build netting doc, check wallet ceiling, submit via mail
- Memory recording action: write decision outcome to structured memory
- Concierge action: format and send protocol support replies
- All existing actions (log, compile, wake, act, reply) preserved

---

## File Layout (v3.4.0)

```
guard/knarr-thrall/
├── handler.py          — Plugin entry, hooks, subsystem init, pipeline orchestration
├── engine.py           — Pipeline engine (TRIGGER→FILTER→GATHER→EVALUATE→ACTION)
├── evaluate.py         — LLM orchestrator, L1/L2 cascade, cost tracker
├── backends.py         — LocalBackend, OllamaBackend, OpenAIBackend + factory
├── thrall_actions.py   — Action executor (log, compile, wake, act, reply, settle, memory)
├── gather.py           — Context gatherer (cockpit, memory, static sources)
├── memory.py           — Structured decision memory (tagged records, filtered queries)
├── identity.py         — Delegated Ed25519 identity (eddsa-jcs-2022 signing)
├── wallet.py           — Scoped daily wallet (ceiling, auto-reset)
├── commerce.py         — Commerce wrappers (ledger, economy, netting, settlement)
├── db.py               — ThrallDB (SQLite: journal, buffers, context, memory, wallet)
├── loader.py           — Recipe/prompt TOML loader (auto-discovers *.toml)
├── plugin.toml         — Configuration (backends, cascade, identity, wallet, trust tiers)
├── recipes/            — 18 recipe files
│   ├── mail-triage.toml           — LLM classification of inbound mail
│   ├── mail-digest.toml           — Buffered compilation for batch processing
│   ├── health-check.toml          — Periodic skill chain canary (on_tick)
│   ├── cluster-probe.toml         — Execution pipeline probe (on_tick)
│   ├── credit-warning.toml        — Credit limit warnings (on_event, hotwire)
│   ├── security-alert.toml        — Security events (on_event, hotwire)
│   ├── task-failures.toml         — Task failure monitoring (on_event, hotwire)
│   ├── queue-backpressure.toml    — Queue congestion (synthetic on_tick)
│   ├── selftest.toml              — 7-point self-test recipe
│   ├── greeter.toml.demo          — Peer welcome (on_event peer.added) [DEMO]
│   ├── chat.toml                  — General conversational chat
│   ├── concierge-faq.toml         — Protocol FAQ (3-tier support, tier 1)
│   ├── concierge-intake.toml      — Service intake classification (tier 2)
│   ├── concierge-expert.toml      — Deep expertise with gather (tier 3)
│   ├── settlement-check.toml      — Periodic position scan (on_tick, 1h)
│   ├── settlement-review.toml     — Inbound settlement review (LLM + gather)
│   ├── inbound-settlement.toml    — Settle_request handler (hotwire)
│   └── swarm-probe.toml           — Autonomous skill probing for experiments
├── prompts/            — 7 prompt templates
│   ├── triage.toml                — L2 full classification prompt
│   ├── triage-l1.toml             — L1 binary drop/escalate prompt
│   ├── greeter.toml.demo          — Peer assessment prompt [DEMO]
│   ├── chat.toml                  — General conversational prompt
│   ├── concierge-faq.toml         — Protocol knowledge base prompt
│   ├── concierge-intake.toml      — Service classification prompt
│   └── concierge-expert.toml      — Deep analysis prompt
└── docs/               — Specs, reviews, release notes (in repo root)
```

---

## Breaking Changes

- `actions.py` renamed to `thrall_actions.py` — imports updated in handler.py
- Pipeline now has 5 stages (added GATHER between FILTER and EVALUATE)
- DB schema has new tables (`thrall_memory`, `thrall_wallet`) — auto-created on first run

---

## Version History

| Version | Date | Changes |
|---|---|---|
| v1.0 | 2026-02-18 | Phase 1.5: gemma3:1b CPU inference, basic classification |
| v2.0 | 2026-02-27 | Classification engine: trust tiers, rate limiting, circuit breakers |
| v3.0 | 2026-02-28 | Switchboard: recipe engine, bus events, compilation buffers |
| v3.1 | 2026-03-01 | Multi-backend: local/ollama/openai, hot-swap, cost tracking |
| v3.2.0 | 2026-03-01 | Cascade: L1 (gemma3:1b CPU) → L2 (qwen3:32b GPU) |
| v3.2.1 | 2026-03-02 | Bug fixes (A1, A2), greeter demo (T3), infrastructure |
| **v3.4.0** | **2026-03-04** | **Structured memory, context gather, settlement commerce, concierge, swarm probe** |

---

## Deployment Notes

1. **First run** creates `thrall_memory` and `thrall_wallet` tables automatically
2. **Identity**: Set `[config.thrall.identity] enabled = true` — keyfile generated on first start
3. **Wallet**: Configure `[config.thrall.wallet] ceiling = N` for daily spending limit
4. **Secrets**: `cockpit_token` and `api_key` should be in vault, not in plugin.toml
5. **Settlement recipes**: Require `identity.enabled = true` and a non-zero wallet ceiling
6. **Concierge recipes**: Work with any backend (local/ollama/openai), no special config needed

---

*Release by Viggo, 2026-03-04*

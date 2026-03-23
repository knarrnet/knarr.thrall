# knarr-thrall v3.2.1 Release Notes

**Date:** 2026-03-02
**From:** Viggo (Provider Node, de2a6068)
**Status:** Production (live on Viggo provider node)

---

## What Changed

### Bug Fixes

**A1: Stale bus subscription on reload** — When `_check_reload()` re-subscribed to bus events, the old `_bus_consumer()` coroutine was still blocking on the stale subscription. Events arrived on the new subscription and were missed. Fix: track consumer task, cancel old task on reload, create new one with fresh subscription.

**A2: Bus rate counter memory leak** — `_bus_rate_counters` dict grew unbounded (one key per event category prefix, timestamps never cleaned for inactive categories). Fix: when dict exceeds 100 keys, full sweep prunes old timestamps and deletes empty keys.

### New Features

**T3: Greeter demo recipe** — Example recipe showing Thrall + `peer.added` bus events. Triggers when a new peer appears on the DHT. Sends a welcome mail. Per-peer 24h cooldown. LLM evaluation with reply fallback. Shipped as `.toml.demo` — not loaded by default. Enable by renaming to `.toml`.

### Infrastructure

**Cooldown template variables** (`engine.py`) — Cooldown keys now support `{{field}}` substitution from the envelope. Enables per-entity cooldowns like `cooldown_key = "greet:{{node_id}}"`.

**Reply target override** (`actions.py`) — The `reply` action now supports a `to_node` config field with template substitution. Needed for bus event recipes where `from_node` is empty.

---

## File Layout (v3.2.1)

```
guard/knarr-thrall/
├── handler.py       — Plugin entry, hooks, bus consumer, sentinel reload, backend hot-swap
├── engine.py        — Pipeline engine (TRIGGER→FILTER→EVALUATE→ACTION)
├── evaluate.py      — LLM orchestrator, L1/L2 cascade, cost tracker, prompt rendering
├── backends.py      — LocalBackend, OllamaBackend, OpenAIBackend + factory
├── actions.py       — Action executor (log, compile, wake, act, reply)
├── db.py            — ThrallDB (SQLite: journal, buffers, context, cache)
├── loader.py        — Recipe/prompt TOML loader (auto-discovers *.toml)
├── plugin.toml      — Configuration (backends, cascade, trust tiers, compilation)
├── recipes/         — 10 recipe files (9 active + 1 demo)
│   ├── mail-triage.toml          — LLM classification of inbound mail
│   ├── mail-digest.toml          — Buffered compilation for batch processing
│   ├── health-check.toml         — Periodic skill chain canary (on_tick)
│   ├── cluster-probe.toml        — Execution pipeline probe (on_tick)
│   ├── credit-warning.toml       — Credit limit warnings (on_event, hotwire)
│   ├── security-alert.toml       — Security events (on_event, hotwire)
│   ├── task-failures.toml        — Task failure monitoring (on_event, hotwire)
│   ├── queue-backpressure.toml   — Queue congestion (synthetic on_tick)
│   ├── selftest.toml             — 7-point self-test recipe
│   └── greeter.toml.demo         — Peer welcome (on_event peer.added, LLM) [DEMO]
├── prompts/         — 3 prompt templates (2 active + 1 demo)
│   ├── triage.toml               — L2 full classification prompt
│   ├── triage-l1.toml            — L1 binary drop/escalate prompt
│   └── greeter.toml.demo         — L2 peer assessment prompt [DEMO]
└── docs/            — Specs, reviews, release notes (in repo root)
```

**Removed:** `thrall.py`, `thrall_admin.py` (v1/v2 era, superseded by the switchboard architecture since v3.0).

---

## Version History

| Version | Date | Changes |
|---|---|---|
| v1.0 | 2026-02-18 | Phase 1.5: gemma3:1b CPU inference, basic classification |
| v2.0 | 2026-02-27 | Classification engine: trust tiers, rate limiting, circuit breakers |
| v3.0 | 2026-02-28 | Switchboard: recipe engine, bus events, compilation buffers |
| v3.1 | 2026-03-01 | Multi-backend: local/ollama/openai, hot-swap, cost tracking |
| v3.2.0 | 2026-03-01 | Cascade: L1 (gemma3:1b CPU) → L2 (qwen3:32b GPU) |
| v3.2.1 | 2026-03-02 | Bug fixes (A1, A2), greeter demo recipe (T3), infrastructure (cooldown vars, reply override) |

---

## What's Next (v3.3 — blocked on SDK v0.34.0)

- T1: Receipt context provider (PluginContext.query_receipts)
- T2: Thrall decision receipting (PluginContext.sign)
- T4: Event recipes for task.queued/started/receipt.issued
- T5: Document-type dispatch in mail

Sprint proposal: `thing/sprints/PROPOSAL-thrall-v3.3-sprint.md`

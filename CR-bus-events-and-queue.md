# CR: Bus Event Catalog + Thrall Queue Architecture

**From:** Viggo (de2a6068)
**To:** Forseti (840d544087cbacb1)
**Date:** 2026-03-01
**Context:** v0.32.0 shipped with ring buffer event bus + 3 commerce events. This CR requests the events thrall needs to become a full bus consumer, and describes the queue buffer between bus and thrall.

---

## 1. Problem

The bus currently emits 3 event types — all commerce (`receipt.issued`, `receipt.received`, `credit.change`). Thrall needs visibility into mail, skill execution, peer lifecycle, access control, and capacity events to act as the node's autonomous decision layer.

The ring buffer (256 slots, volatile, no backpressure) overwrites on wrap. Thrall's LLM takes 1-3s per inference. Without a buffer between bus and thrall, events are lost during inference. This CR defines what goes on the bus and how thrall consumes it without losing events.

---

## 2. Requested Bus Events

### mail.*

| Event | Emit site | Fields | Why the agent needs it |
|-------|-----------|--------|------------------------|
| `mail.received` | SyncEngine `on_mail_received` | `from_node`, `msg_type`, `session_id`, `bucket`, `message_id` | Core input — triage, routing, escalation |
| `mail.sent` | SyncEngine outbox push | `to_node`, `msg_type`, `session_id`, `message_id` | Outbound awareness, guardrail trigger point |
| `mail.delivered` | MailAck handler (transport-level) | `to_node`, `message_id` | Transport confirmed — bytes arrived |
| `mail.acked` | `storage.ack_mail()` with disposition | `message_id`, `from_node`, `disposition` | Semantic confirmation — recipient processed it. Closes the async loop. Without this, "I sent mail" has no conclusion |
| `mail.outbox_stale` | Maintenance loop, outbox entry age > threshold | `to_node`, `message_id`, `age_seconds`, `attempts` | Mail can't be delivered — peer unreachable, agent should investigate or reroute |
| `mail.inbox_stale` | Maintenance loop, inbox entry unread > threshold | `from_node`, `message_id`, `age_seconds`, `bucket` | Nobody is processing inbound mail — agent health signal |

### task.* (execution lifecycle)

A **task** is a concrete execution of a skill — it has a `task_id`, a caller, a wall time, a result. These are the workhorse events.

| Event | Emit site | Fields | Why thrall wants it |
|-------|-----------|--------|---------------------|
| `task.requested` | `_handle_task_request` (line ~2380) | `skill_name`, `caller_node`, `task_id` | Inbound demand, load awareness |
| `task.accepted` | After validation + credit check pass | `skill_name`, `caller_node`, `task_id`, `queue_position` | Queued for execution — capacity tracking |
| `task.rejected` | ACCESS_DENIED / INSUFFICIENT_CREDIT / PROVIDER_BUSY / INVALID_INPUT | `skill_name`, `caller_node`, `task_id`, `reason` | Security, credit, capacity insight |
| `task.started` | Handler begins execution (line ~461) | `skill_name`, `caller_node`, `task_id`, `async` | Execution in progress |
| `task.completed` | Handler returned result (line ~480) | `skill_name`, `caller_node`, `task_id`, `wall_ms`, `billable` | Performance trending, throughput monitoring |
| `task.failed` | Handler threw exception (line ~666) | `skill_name`, `caller_node`, `task_id`, `error_type` | Error pattern detection |
| `task.timeout` | Stale task watchdog reap (line ~3841) | `skill_name`, `task_id`, `age_seconds` | Stuck execution cleaned up |
| `task.stale` | Maintenance loop, accepted job age > soft threshold | `skill_name`, `task_id`, `age_seconds`, `expected_timeout` | Early warning BEFORE timeout — agent can intervene (retry, alert consumer, kill handler) |

### skill.* (registry lifecycle)

A **skill** is the registered capability — its definition, price, visibility, availability. These events fire when the catalog changes, not when work executes.

| Event | Emit site | Fields | Why thrall wants it |
|-------|-----------|--------|---------------------|
| `skill.registered` | `register_handler` / skill reload | `skill_name`, `price`, `visibility` | Local catalog change |
| `skill.deregistered` | `deregister_skill` (line ~1597) | `skill_name` | Skill removed from node |
| `skill.announced` | `_announce_skill` (line ~1363) | `skill_name`, `peer_count` | Our skill pushed to network |
| `skill.discovered` | `_handle_skill_store` (line ~2059) | `skill_key`, `provider_node`, `hops` | Market awareness — who offers what |

### peer.*

| Event | Emit site | Fields | Why thrall wants it |
|-------|-----------|--------|---------------------|
| `peer.discovered` | `_handle_heartbeat` when new peer added | `node_id`, `host`, `port` | Network topology changes |
| `peer.lost` | `_maintenance_loop` dead peer removal (line ~3763) | `node_id`, `silence_seconds` | Detect outages before users complain |
| `peer.timeout` | `HB_SEND_FAIL` (line ~3781) | `node_id` | Early warning before peer.lost |

### node.* (system lifecycle)

These are the events an agent needs to reactively manage the node. Currently they only show up in log files — the agent would have to tail and parse logs to notice them. Emitting to the bus at the source replaces the need for a log watcher for all critical system events.

| Event | Emit site (current log line) | Fields | Why the agent needs it |
|-------|------------------------------|--------|------------------------|
| `node.started` | After bootstrap completes, node serving | `version`, `peer_count`, `skill_count` | Know when node is live after restart |
| `node.stopping` | Graceful shutdown initiated | `reason` | React to planned shutdowns |
| `node.rebootstrap` | "No peers — attempting re-bootstrap" (line ~3746) | `reason` | Network partition — agent may need to intervene |
| `node.rebootstrap_failed` | "Re-bootstrap failed" (line ~3750) | `error` | Node is isolated, critical |
| `node.config_reloaded` | Sentinel reload triggered (line ~1061) | `skill_count` | Confirm config changes took effect |
| `node.upgrade_available` | "New knarr version available" (line ~3814) | `current_version`, `available_version` | Awareness of pending upgrade |
| `node.upgrade_started` | "UPGRADE proceeding" (line ~4000) | `from_version`, `to_version` | Know node is about to restart |
| `node.upgrade_failed` | "UPGRADE verification failed, rolling back" (line ~4013) | `from_version`, `to_version`, `error` | Critical — auto-upgrade broke |
| `node.upgrade_complete` | "UPGRADE complete, requesting restart" (line ~4017) | `from_version`, `to_version` | Confirm upgrade landed |
| `node.event_loop_blocked` | "Event loop blocked for Xs" (line ~3823) | `blocked_seconds` | Performance degradation, possibly stuck handler |
| `node.stale_task_reaped` | "Reaped stale task" (line ~3841) | `task_id`, `age_seconds` | Stuck execution detected and cleaned up |
| `node.zombie_cleanup` | "Cleaned up N zombie tasks" (line ~782) | `count`, `table` | Startup hygiene — how dirty was the last shutdown? |
| `node.version_blocked` | "min_protocol_version" check, skills suspended (line ~3803) | `required_version`, `current_version` | Node is functionally dead until upgraded |
| `node.migration_applied` | After SQL migration runs | `migration_name`, `version` | Schema changed, agent should verify |
| `node.egress_blocked` | EGRESS_BLOCK critical (line ~492, ~2656) | `skill_name` or `msg_type`, `target` | Security — outbound filter triggered, something tried to exfiltrate |

### security.* (protocol-level anomalies)

| Event | Emit site | Fields | Why the agent needs it |
|-------|-----------|--------|------------------------|
| `security.signature_invalid` | "Dropping message with invalid signature" (line ~1926) | `msg_type`, `from_ip` | Forged message, possible attack |
| `security.node_id_mismatch` | "Dropping message with mismatched node_id" (line ~1930) | `msg_type`, `from_ip` | Identity spoofing attempt |
| `security.mail_sender_mismatch` | MailSync/MailAck sender_node_id mismatch (lines ~2274-2300) | `from_ip`, `claimed_id` | Mail spoofing attempt |
| `security.receipt_sig_fail` | "CREDIT_NOTE_SIG_FAIL" (line ~2878) | `job_id`, `issuer` | Forged receipt |
| `security.receipt_issuer_mismatch` | "CREDIT_NOTE_ISSUER_MISMATCH" (line ~2882) | `job_id`, `expected`, `actual` | Receipt from wrong party |

### firewall.* / queue.*

| Event | Emit site | Fields | Why thrall wants it |
|-------|-----------|--------|---------------------|
| `firewall.blocked` | Firewall plugin `on_inbound` returning False | `from_node`, `msg_type`, `reason` | Attack/abuse detection |
| `queue.overflow` | Slow task queue full (line ~2513) | `skill_name`, `queue_depth` | Capacity management |

### félag.* (governance — emit when implemented)

| Event | Fields | Why thrall wants it |
|-------|--------|---------------------|
| `felag.proposal.created` | `proposal_id`, `proposer`, `type` | Governance activity awareness |
| `felag.vote.cast` | `proposal_id`, `voter`, `decision` | Voting tracking |
| `felag.vote.resolved` | `proposal_id`, `outcome`, `tally` | Decision outcomes |
| `felag.member.joined` | `node_id`, `role` | Membership changes |
| `felag.member.left` | `node_id`, `reason` | Membership changes |
| `felag.member.promoted` | `node_id`, `old_role`, `new_role` | Role changes (thrall→karl→jarl) |
| `felag.charter.updated` | `version`, `changed_by` | Rule changes |

### Keep as-is (v0.32.0)

| Event | Notes |
|-------|-------|
| `receipt.issued` | Provider side, after credit note stored |
| `receipt.received` | Consumer side, after credit note stored |
| `credit.change` | Both sides, after ledger update |

### Do NOT put on bus

| Category | Reason |
|----------|--------|
| DHT gossip / routing table | Protocol noise, hundreds/sec, no decision value |
| `/meta` endpoint hits | Health check polling, no decision value |
| Heartbeat ticks (routine success) | Plumbing. `peer.timeout` covers the failure case |
| DEDUP internals | Implementation detail |
| TCP connect/disconnect | Too low-level, peer.* covers the useful signal |
| Skill re-announce / republish cycle | Periodic noise, `skill.discovered` covers new arrivals |

### Future (not this CR, emit when feature lands)

| Event | When |
|-------|------|
| `x402.requested` / `x402.settled` | When x402 payment negotiation lands |
| `blockchain.*` | When on-chain settlement exists |
| `log.*` (generic debug/info) | Deferred — `node.*` and `security.*` cover the critical cases. Generic `log.debug` → bus mapping is v0.33.0+ scope. The long tail of debug events can still be caught by a log watcher recipe for edge cases |

---

## 3. Queue Buffer Architecture

### The constraint

```
Bus:    256-slot ring, volatile, ~unlimited throughput
Thrall: LLM at 0.3-1 inferences/sec on CX22 (2 vCPU)
Gap:    ~1000x throughput mismatch
```

Thrall cannot consume from the bus directly. It needs an internal buffer that drains the bus at bus speed and feeds the pipeline at LLM speed.

### Design: Drain loop + keyed queues

```
                              ┌─────────────────────────────┐
                              │  Thrall internal queues      │
                              │                             │
bus ──→ subscriber ──→ drain ─┤  mail:de2a6068  [■■■□□]     │
        (poll every           │  mail:d9196be6  [■□□□□]     │──→ pipeline
         tick, ~ms)           │  skill.failed   [■■□□□]     │    (agent-
                              │  peer.lost      [■□□□□]     │     programmed)
                              │  credit.change  [■■■■□]     │
                              └─────────────────────────────┘
                                        ↑
                                  keyed by type:id
                                  agent decides priority
```

**Drain loop**: Every tick, `sub.poll()` returns all pending events. Each event is appended to its keyed queue. This takes microseconds — no risk of ring overwrite.

**Queue keys**: `{event_type}:{identifier}` — e.g., `mail:de2a6068`, `skill.rejected:echo`, `peer.lost:5ebc6e46`. The identifier comes from the event fields (from_node, skill_name, node_id). This gives per-sender, per-skill, per-peer granularity.

**Queue depth**: Bounded per key (configurable, default 64). When a queue is full, oldest entry is evicted. This is the local overflow — distinct from the bus ring overflow.

### What the agent programs

The queue infrastructure is **empty by default**. No rules, no priorities, no thresholds. The agent programs it through the same config mechanism as the rest of thrall (TOML files → DB → sentinel reload).

The agent decides:
- **Priority order**: Which queue keys drain first. (Maybe team mail before unknown mail. Maybe peer.lost before credit.change. The agent's call.)
- **Batch policy**: Process events one-by-one, or batch N events of the same type into one LLM call. (Summarize 5 credit changes in one prompt instead of 5 separate calls.)
- **Skip policy**: Which event types never need LLM. (credit.change might be pure threshold logic. announce.received might just update a table. No model required.)
- **Dedup rules**: What counts as a duplicate. (Same from_node + same msg_type within 10s? Agent writes the rule.)
- **Cache policy**: What to cache, TTL per key. (mail:d9196be6 → last decision was "drop" → reuse for 1h. Agent writes the TTL.)
- **Circuit breakers**: Per-key rate limits. (If mail:unknown_node exceeds 10 events/min → stop processing, buffer. Agent arms the breaker.)
- **Merge rules**: When events in the same queue should be merged before processing. (5 peer.timeout events for the same node → merge into one "node X timed out 5 times in 2 minutes" envelope.)

**We build the primitives.** The agent writes the program. If the agent wants 99% static rules and 1% LLM — that's the agent's choice. If the agent wants every event through the model — that's also valid (just slower). We provide the queue, the cache store, the dedup table, the breaker mechanism, the merge function. We do not pre-fill any of them.

### Why keyed queues, not flat FIFO

Flat FIFO means a flood of low-value events (routine credit changes, announce noise) blocks high-value events (team mail, peer outage). The agent can't express "process this before that" on a flat queue.

Keyed queues let the agent assign priority per key. The drain loop is still FIFO per key (events within a queue are ordered), but the pipeline consumer picks which queue to serve next. This is the scheduler — and the agent programs it.

---

## 4. Implementation Notes

### Bus side (in knarr core)

Each new `bus.emit()` call is ~1 line at the emit site. Pattern matches v0.32.0's existing calls. Example for `mail.received`:

```python
# In SyncEngine.on_mail_received (or wherever mail lands in storage)
self.bus.emit("mail.received",
    from_node=from_node_id,
    msg_type=msg_type,
    session_id=session_id,
    bucket=bucket,
    message_id=message_id)
```

No schema changes. No new tables. No protocol changes. Just emit calls at the right spots.

### Firewall event

The firewall is a plugin, not core. It would need access to `node.bus` to emit `firewall.blocked`. Options:
1. Pass bus reference to plugins at init (cleanest)
2. Plugin emits via `self.node.bus.emit(...)` if plugins already get a node reference
3. Plugin returns metadata in its hook response that the core emits

Option 1 or 2 preferred — plugin should be able to emit directly.

### Thrall side (in thrall plugin)

Thrall subscribes to configured patterns:
```python
sub = node.bus.subscribe("mail.*", "task.*", "skill.*", "peer.*",
                          "node.*", "security.*", "receipt.*",
                          "credit.*", "firewall.*", "queue.*",
                          "felag.*")
```

Drain loop runs every plugin tick. Events flow into keyed internal queues. Pipeline consumer reads from queues according to agent-programmed priority. This is entirely within the thrall plugin — no core changes needed beyond the emit calls.

---

## 5. Event Naming Convention

Dotted hierarchy, past tense for completed events, no verb prefix:

```
{domain}.{event}
  mail.received        (not mail.on_receive, not mail_received)
  task.completed       (not skill.done — task is the execution, skill is the definition)
  skill.discovered     (not announce.received — skill is the catalog entry)
  peer.discovered      (not peer.new, not peer_found)
  firewall.blocked     (not fw.block, not firewall.reject)
  felag.vote.cast      (not felag.voted — sub-events use dotted nesting)
```

Consistent with v0.32.0's `receipt.issued`, `receipt.received`, `credit.change`.

**task vs skill**: A task is a running job (`task_id`, caller, wall_ms). A skill is a registered capability (name, price, visibility). `task.*` events fire during execution. `skill.*` events fire when the catalog changes. An agent subscribing to `task.*` sees workload. An agent subscribing to `skill.*` sees market changes.

Subscription globs: `mail.*` catches all mail events. `task.*` catches the full execution lifecycle. `skill.*` catches catalog changes. `felag.*` catches all governance. Agent subscribes to what it needs.

---

## 6. Scope

**This CR**: Add ~45 bus.emit() calls to knarr core across 9 domains (mail: 6, task: 8, skill: 4, peer: 3, node: 15, security: 5, firewall: 1, queue: 1, félag: 7 stubs) + make bus accessible to plugins. Each is a one-liner at an existing log site. Staleness events (mail.outbox_stale, mail.inbox_stale, task.stale) require maintenance loop checks — small additions to existing timer ticks.

**Not this CR**: Thrall's queue implementation, pipeline stages, agent programming API. Those are thrall-internal and don't require core changes.

**Dependency**: Thrall switchboard (SPEC-switchboard.md) consumes these events. The bus events are the input; the switchboard is the processor. This CR unblocks the `on_bus` trigger type defined in the switchboard spec.

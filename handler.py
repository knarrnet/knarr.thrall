"""Thrall Switchboard — Main plugin handler.

Replaces plugins/06-responder. Hooks into on_mail_received and on_tick.
Runs recipes against events, compiles digests, summons agent when needed.

v3.0: Configurable pipeline engine. TOML recipes. Compilation buffers.

NOTE: Uses absolute imports (not relative) because knarr's PluginLoader
imports handler.py via spec_from_file_location with the plugin directory
temporarily on sys.path. Relative imports would fail.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth
from knarr.core.models import NodeInfo

from db import ThrallDB
from evaluate import Evaluator
from backends import create_backend
from thrall_actions import ActionExecutor
from engine import PipelineEngine, Envelope
from loader import load_all
from identity import ThrallIdentity
from wallet import ThrallWallet
from commerce import ThrallCommerce
from memory import ThrallMemory, MemoryWriter
from gather import ContextGatherer

logger = logging.getLogger("thrall")


def _setup_file_logging(plugin_dir: str, debug: bool):
    """Wire thrall.log as the agent-readable trace.

    RotatingFileHandler: 1MB x 3 backups. The agent reads this to observe
    thrall's decisions and tune recipes/prompts/thresholds.
    """
    log_path = os.path.join(plugin_dir, "thrall.log")
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    # Attach to the root "thrall" logger — all children inherit
    root = logging.getLogger("thrall")
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)


class ThrallPlugin(PluginHooks):
    """Knarr plugin — the switchboard.

    Pipeline: TRIGGER → FILTER → EVALUATE → ACTION
    Every inbound event flows through matching recipes.
    """

    def __init__(self, ctx: PluginContext, config: Dict[str, Any]):
        self._ctx = ctx
        self._config = config
        self._log = ctx.log
        self._plugin_dir = str(ctx.plugin_dir)
        self._enabled = config.get("enabled", True)
        self._debug = config.get("debug", False)
        self._dry_run = config.get("dry_run", False)
        self._tick_count = 0
        self._processing = False

        if not self._enabled:
            self._log.info("Thrall switchboard disabled")
            return

        # File logging — the agent's observability layer
        _setup_file_logging(self._plugin_dir, self._debug)

        # Initialize DB
        db_path = os.path.join(self._plugin_dir, "thrall.db")
        self.db = ThrallDB(db_path)

        # Initialize evaluator (swappable LLM backend)
        thrall_cfg = config.get("thrall", {})
        self._current_backend_name = thrall_cfg.get("backend", "local")

        # Migrate flat model_path to local sub-config for backwards compat
        if "model_path" in thrall_cfg and "local" not in thrall_cfg:
            thrall_cfg["local"] = {
                "model_path": thrall_cfg["model_path"],
                "n_threads": thrall_cfg.get("n_threads", 4),
                "n_ctx": thrall_cfg.get("n_ctx", 1024),
                "max_tokens": thrall_cfg.get("max_tokens", 128),
            }
        if "backend" not in thrall_cfg:
            thrall_cfg["backend"] = "local"

        backend = create_backend(thrall_cfg, vault_get=ctx.vault_get)
        cost_budget = thrall_cfg.get("openai", {}).get("cost_budget_daily", 0.0)

        # Cascade L1 prefilter — optional fast binary filter
        cascade_cfg = thrall_cfg.get("cascade", {})
        l1_backend = None
        self._cascade_enabled = cascade_cfg.get("enabled", False)
        self._cascade_l1_type = cascade_cfg.get("l1_backend", "")
        if self._cascade_enabled and self._cascade_l1_type:
            try:
                l1_cfg = dict(thrall_cfg)
                l1_cfg["backend"] = self._cascade_l1_type
                l1_backend = create_backend(l1_cfg, vault_get=ctx.vault_get)
                self._log.info(f"Cascade L1: {l1_backend.name}/{l1_backend.model_name}")
            except Exception as e:
                self._log.warning(f"Cascade L1 init failed: {e} — running without cascade")

        self.evaluator = Evaluator(
            backend=backend,
            queue_timeout=thrall_cfg.get("queue_timeout", 5.0),
            cost_budget_daily=float(cost_budget),
            l1_backend=l1_backend,
            l1_prompt=cascade_cfg.get("l1_prompt", "triage-l1"),
        )

        # Initialize living memory pillar writer (before ActionExecutor which needs it)
        rag_dir = thrall_cfg.get("rag_dir", "")
        if not rag_dir:
            candidate = os.path.join(self._plugin_dir, "rag")
            if os.path.isdir(candidate):
                rag_dir = candidate
            elif os.path.isdir("/app/rag"):
                rag_dir = "/app/rag"
            else:
                rag_dir = candidate  # fallback, MemoryWriter creates it
        self._memory_writer = MemoryWriter(rag_dir)

        # Initialize action executor (commerce wired after identity init below)
        priority_kw = thrall_cfg.get("priority_keywords", None)
        max_payload = int(thrall_cfg.get("max_payload_bytes", 60_000))
        self.actions = ActionExecutor(
            db=self.db,
            send_mail_fn=self._send_mail,
            call_skill_fn=self._call_skill,
            summon_fn=self._summon_agent,
            plugin_dir=self._plugin_dir,
            priority_keywords=priority_kw,
            memory_writer=self._memory_writer,
            max_payload_bytes=max_payload,
        )

        # Initialize structured memory and wire to action executor
        self.memory = ThrallMemory(self.db)
        self.actions._structured_memory = self.memory

        # Knowledge-as-a-Service (v3.10)
        knowledge_cfg = thrall_cfg.get("knowledge", {})
        self.knowledge_manager = None
        if knowledge_cfg.get("trust_level", "none") not in ("disabled", "none"):
            from knowledge import KnowledgeManager
            self.knowledge_manager = KnowledgeManager(
                db=self.db,
                backend=backend,
                plugin_dir=self._plugin_dir,
                config=knowledge_cfg,
            )

        # Initialize context gatherer (pre-prompt data fetching)
        self.gatherer = ContextGatherer(
            db=self.db, plugin_dir=self._plugin_dir)

        # Initialize engine (with gatherer for [[gather]] recipe stages)
        # T6: ctx + memory wired for pipeline error bus events and structured records
        self.engine = PipelineEngine(
            db=self.db,
            evaluator=self.evaluator,
            action_executor=self.actions,
            gatherer=self.gatherer,
            ctx=ctx,
            memory=self.memory,
        )

        # Set trust tiers
        trust_tiers = thrall_cfg.get("trust_tiers", {})
        self.engine.set_trust_tiers(trust_tiers)

        # Load recipes and prompts
        summary = load_all(self._plugin_dir, self.db, self.evaluator)
        self.engine.load_recipes()
        dry_label = " [DRY_RUN MODE]" if self._dry_run else ""
        self._log.info(f"Thrall switchboard ready{dry_label}: {summary}")

        # Cockpit for skill calls
        # Use 127.0.0.1, not localhost — Python urllib on Windows tries IPv6
        # first which adds ~2s per request due to connection timeout fallback
        self._cockpit_url = thrall_cfg.get("cockpit_url", "http://127.0.0.1:8080")
        self._cockpit_call_timeout = int(thrall_cfg.get("cockpit_call_timeout", 30))
        self._poll_max_wait = int(thrall_cfg.get("poll_max_wait", 60))
        self._poll_initial_interval = float(thrall_cfg.get("poll_initial_interval", 2.0))
        self._cockpit_token = ""
        if ctx.vault_get:
            try:
                self._cockpit_token = ctx.vault_get("cockpit_token") or ""
            except Exception:
                pass
        if not self._cockpit_token:
            self._cockpit_token = thrall_cfg.get("cockpit_token", "")

        # Settlement identity — delegated keypair + scoped wallet + commerce
        identity_cfg = thrall_cfg.get("identity", {})
        wallet_cfg = thrall_cfg.get("wallet", {})
        self.identity = ThrallIdentity(self._plugin_dir, identity_cfg,
                                        node_id=ctx.node_id)
        self.wallet = ThrallWallet(self.db, wallet_cfg) if self.identity.enabled else None
        self.commerce = ThrallCommerce(
            cockpit_url=self._cockpit_url,
            cockpit_token=self._cockpit_token,
            node_id=ctx.node_id,
            query_receipts_fn=getattr(ctx, "query_receipts", None),
        ) if self.identity.enabled else None

        # Wire commerce, wallet, memory, ctx into action executor and gatherer
        if self.commerce:
            self.actions._commerce = self.commerce
            self.gatherer.set_commerce(self.commerce)
        if self.wallet:
            self.gatherer.set_wallet(self.wallet)
        self.gatherer.set_memory(self.memory)
        # Wire PluginContext so punchhole gather source can call get_plugin()
        self.gatherer.set_ctx(ctx)
        if self.knowledge_manager:
            self.gatherer.set_knowledge_manager(self.knowledge_manager)

        # Compilation config
        compile_cfg = config.get("compilation", {})
        self._compile_interval = compile_cfg.get("interval_seconds", 3600)
        self._compile_buffer = compile_cfg.get("buffer", "mail-digest")

        # Sentinel reload tracking
        self._last_reload = time.time()

        # Bus event subscription (v0.32.0+ graceful degradation)
        self._bus_sub = None
        self._bus_events_processed = 0
        self._bus_events_dropped = 0
        self._bus_rate_counters: Dict[str, List[float]] = {}  # pattern -> timestamps
        self._bus_consumer_task = None
        if getattr(self._ctx, "subscribe_events", None):
            patterns = self._collect_event_patterns()
            if patterns:
                try:
                    self._bus_sub = self._ctx.subscribe_events(*patterns)
                    self._bus_consumer_task = asyncio.get_event_loop().create_task(
                        self._bus_consumer())
                    self._log.info(f"Thrall subscribed to bus: {patterns}")
                except Exception as e:
                    self._log.warning(f"Bus subscription failed: {e}")
        else:
            self._log.info("Bus API not available (pre-v0.32.0) — bus events disabled")

    # ── Plugin hooks ──

    async def on_mail_received(self, msg_type: str, from_node: str,
                                to_node: str, body: Any,
                                session_id: Optional[str] = None) -> None:
        """Called by knarr core on every inbound message.

        Runs matching recipes. Does not return a value — suppression is
        handled by the action (drop/compile silently consume the message;
        wake sends a system mail that the agent plugin picks up).
        """
        if not self._enabled:
            return

        # Skip system/ack/delivery types
        ignore_types = self._config.get("ignore_msg_types",
                                         ["ack", "delivery", "system"])
        if msg_type in ignore_types:
            return

        # Drop messages with no sender and no body — these are protocol
        # artifacts (heartbeat echoes, empty event notifications) that have
        # no content to act on. Letting them through creates empty wakes
        # that flood the inbox with meaningless thrall_digest system mails.
        body_text = _extract_body_text(body)
        if not from_node and not body_text.strip():
            if self._debug:
                self._log.debug("Dropping empty message (no sender, no body)")
            return
        envelope = Envelope(
            trigger_type="on_mail",
            timestamp=time.time(),
            fields={
                "from_node": from_node or "",
                "to_node": to_node or "",
                "msg_type": msg_type or "text",
                "body_text": body_text,
                "body_json": json.dumps(body) if isinstance(body, dict) else str(body),
                "session_id": session_id or "",
            },
        )

        # Match recipes
        matched = self.engine.match_recipes("on_mail", envelope)
        if not matched:
            if self._debug:
                self._log.debug(f"No recipe matched for mail from {from_node[:16]}")
            return

        # Run all matching recipes
        # dry_run does NOT skip actions — thrall runs the full pipeline.
        # The dry_run flag is carried in the briefing so the AGENT session
        # knows to write a report instead of acting. thrall-inject uses
        # mode_override="manual" separately for pure pipeline testing.
        for recipe_name in matched:
            try:
                result = await self.engine.run(recipe_name, envelope)
                dry_tag = " [DRY_RUN]" if self._dry_run else ""
                self._log.info(
                    f"PIPELINE {recipe_name}{dry_tag}: "
                    f"filter={result.filter_result.decision} "
                    f"eval={result.eval_result.eval_type}->{result.eval_result.action} "
                    f"action={result.action_result.name} "
                    f"wall={result.wall_ms}ms"
                )
            except Exception as e:
                self._log.error(f"Pipeline {recipe_name} failed: {e}")

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        """Called on every node tick (~10s). Handles timers and cleanup."""
        if not self._enabled:
            return
        if self._processing:
            return

        self._tick_count += 1
        # Run every 6th tick (~60s)
        if self._tick_count % 6 != 0:
            return

        self._processing = True
        try:
            # Check compilation timer flush
            await self.actions.check_timer_flush(
                self._compile_buffer, self._compile_interval)

            # Run on_tick recipes (health checks, etc.)
            envelope = Envelope(
                trigger_type="on_tick",
                timestamp=time.time(),
                fields={
                    "peer_count": str(len(peers)),
                    "tick": str(self._tick_count),
                },
            )
            matched = self.engine.match_recipes("on_tick", envelope)
            for recipe_name in matched:
                try:
                    result = await self.engine.run(recipe_name, envelope)
                    if result.action_result.name not in ("skip", "log", "manual_skip"):
                        self._log.info(
                            f"PIPELINE {recipe_name}: "
                            f"eval={result.eval_result.eval_type}->{result.eval_result.action} "
                            f"action={result.action_result.name} "
                            f"wall={result.wall_ms}ms"
                        )
                except Exception as e:
                    self._log.error(f"Tick pipeline {recipe_name} failed: {e}")

            # Check LLM queue backpressure (synthetic bus event)
            bp_envelope = self._check_queue_backpressure()
            if bp_envelope:
                bp_matched = self.engine.match_recipes("on_event", bp_envelope)
                for recipe_name in bp_matched:
                    try:
                        result = await self.engine.run(recipe_name, bp_envelope)
                        if result.action_result.name not in ("skip", "log", "manual_skip"):
                            self._log.info(
                                f"PIPELINE {recipe_name} [backpressure]: "
                                f"depth={bp_envelope.get('queue_depth')} "
                                f"drop_rate={bp_envelope.get('bus_drop_rate')}% "
                                f"action={result.action_result.name}")
                    except Exception as e:
                        self._log.error(f"Backpressure pipeline {recipe_name} failed: {e}")

            # Cleanup expired context (every ~5 min)
            if self._tick_count % 30 == 0:
                expired = self.db.cleanup_expired_context()
                if expired > 0:
                    self._log.info(f"Cleaned {expired} expired context entries")

            # Data pruning (every ~30 min)
            if self._tick_count % 180 == 0:
                pruned = 0
                pruned += self.db.prune_journal()
                pruned += self.db.prune_memory()
                pruned += self.db.prune_wallet_spend()
                pruned += self.db.prune_compilation()
                if pruned > 0:
                    self._log.info(f"THRALL_PRUNE deleted {pruned} stale rows")
                # Compact memory pillar files (100 KB cap per file)
                freed = self._memory_writer.compact_all()
                if freed > 0:
                    self._log.info(f"THRALL_PILLAR_COMPACT freed {freed} bytes")

            # Check for sentinel reload
            await self._check_reload()

        except Exception as e:
            self._log.error(f"Tick failed: {e}")
        finally:
            self._processing = False

    async def on_settlement_review(self, prepared_tx: dict) -> Optional[dict]:
        """v0.36.0 PluginHook: review a settlement_prepared document.

        Called by the node when a settlement is prepared and needs authority
        review. Returns a countersigned document (approve) or None (reject).

        Routes through settlement-review recipe for hotwire/LLM decision.
        Records outcome in structured memory.
        """
        if not self._enabled or not self.identity or not self.identity.enabled:
            return None

        peer_pk = prepared_tx.get("counterparty", "")
        proposal = prepared_tx.get("proposal", {})
        positions = prepared_tx.get("positions", {})
        amount = float(proposal.get("amount", 0))
        utilization = float(positions.get("utilization_pct", 0))

        # Pre-compute decision flags for hotwire rules
        wallet_ok = self.wallet.can_spend(amount) if self.wallet else False
        util_high = utilization > 80

        envelope = Envelope(
            trigger_type="on_settlement_review",
            timestamp=time.time(),
            fields={
                "peer_pk": peer_pk,
                "amount": str(amount),
                "utilization_pct": str(utilization),
                "wallet_ok": "true" if wallet_ok else "false",
                "util_high": "true" if util_high else "false",
                "affordable_and_high_util": "true" if (wallet_ok and util_high) else "false",
                "prepared_tx_json": json.dumps(prepared_tx),
                "document_type": "settlement_prepared",
            },
        )

        matched = self.engine.match_recipes("on_settlement_review", envelope)
        if not matched:
            self._log.warning("No recipe matched on_settlement_review")
            return None

        for recipe_name in matched:
            try:
                result = await self.engine.run(recipe_name, envelope)
                action = result.eval_result.action
                reason = result.eval_result.reason

                if action in ("approve", "act"):
                    # Countersign with thrall identity
                    signed = self.identity.sign_document(prepared_tx)
                    if self.wallet:
                        self.wallet.record_spend(amount, f"settlement:{peer_pk[:16]}", peer_pk)
                    self.memory.record(
                        skill="settlement-review", node_id=peer_pk,
                        outcome="approved", amount=amount, reasoning=reason)
                    # Living memory hook: record settlement in peers pillar
                    if hasattr(self, '_memory_writer') and self._memory_writer:
                        self._memory_writer.append(
                            "peers",
                            f"Peer {peer_pk[:16]}: settled {amount:.1f}cr at "
                            f"{utilization:.0f}% util — approved ({reason[:60]})")
                    self._log.info(f"SETTLEMENT_REVIEW approved: peer={peer_pk[:16]} "
                                   f"amount={amount:.1f} reason={reason}")
                    return signed
                else:
                    self.memory.record(
                        skill="settlement-review", node_id=peer_pk,
                        outcome="rejected", amount=amount, reasoning=reason)
                    # Living memory hook: record rejection in peers pillar
                    if hasattr(self, '_memory_writer') and self._memory_writer:
                        self._memory_writer.append(
                            "peers",
                            f"Peer {peer_pk[:16]}: settlement rejected "
                            f"({amount:.1f}cr) — {reason[:80]}")
                    self._log.info(f"SETTLEMENT_REVIEW rejected: peer={peer_pk[:16]} "
                                   f"amount={amount:.1f} reason={reason}")
                    return None
            except Exception as e:
                self._log.error(f"Settlement review failed: {e}")
                return None

        return None

    async def on_inbound_settlement(self, settle_request: dict,
                                     sender_pk: str) -> bool:
        """v0.36.0 PluginHook: evaluate an inbound settle_request.

        Called by the node when a remote peer sends a settlement request.
        Returns True (accept) or False (reject).

        Routes through inbound-settlement recipe for hotwire/LLM decision.
        """
        if not self._enabled:
            return False

        proposal = settle_request.get("proposal", {})
        try:
            amount = float(proposal.get("amount", 0))
        except (ValueError, TypeError):
            self._log.warning(f"INBOUND_SETTLEMENT malformed amount from {sender_pk[:16]}")
            return False

        # CR-04 SETTLE-SCHEMA: extract component fields if present
        debt_component = proposal.get("debt_component")
        target_balance_component = proposal.get("target_balance_component")
        has_components = debt_component is not None and target_balance_component is not None
        components_valid = True  # backward compat default
        if has_components:
            try:
                debt_component = float(debt_component)
                target_balance_component = float(target_balance_component)
                components_valid = abs((debt_component + target_balance_component) - amount) < 0.01
            except (ValueError, TypeError):
                self._log.warning(
                    f"INBOUND_SETTLEMENT malformed components from {sender_pk[:16]}")
                components_valid = False

        envelope = Envelope(
            trigger_type="on_inbound_settlement",
            timestamp=time.time(),
            fields={
                "peer_pk": sender_pk,
                "amount": str(amount),
                "settle_request_json": json.dumps(settle_request),
                "document_type": "settle_request",
                "has_components": "true" if has_components else "false",
                "debt_component": str(debt_component) if has_components else "",
                "target_balance_component": str(target_balance_component) if has_components else "",
                "components_valid": "true" if components_valid else "false",
            },
        )

        matched = self.engine.match_recipes("on_inbound_settlement", envelope)
        if not matched:
            self._log.info("No recipe for on_inbound_settlement, defaulting to accept")
            return True

        for recipe_name in matched:
            try:
                result = await self.engine.run(recipe_name, envelope)
                action = result.eval_result.action
                reason = result.eval_result.reason
                accepted = action in ("accept", "approve", "act")

                self.memory.record(
                    skill="inbound-settlement", node_id=sender_pk,
                    outcome="accepted" if accepted else "rejected",
                    amount=amount, reasoning=reason)

                self._log.info(f"INBOUND_SETTLEMENT {'accepted' if accepted else 'rejected'}: "
                               f"peer={sender_pk[:16]} amount={amount:.1f} reason={reason}")
                return accepted
            except Exception as e:
                self._log.error(f"Inbound settlement eval failed: {e}")
                return False

        return False

    async def on_shutdown(self) -> None:
        """Graceful shutdown."""
        if hasattr(self, "db"):
            self.db.close()
        self._log.info("Thrall switchboard shut down")

    # ── Bus event consumer ──

    def _collect_event_patterns(self) -> list:
        """Extract fnmatch patterns from on_event recipes."""
        patterns = set()
        for name, config in self.engine._recipes.items():
            trigger = config.get("trigger", {})
            if trigger.get("type") == "on_event":
                pat = trigger.get("event_pattern", "")
                if pat:
                    patterns.add(pat)
        return list(patterns) or ["*"]

    def _bus_rate_check(self, event_name: str, window: float = 60.0,
                        max_per_window: int = 30) -> bool:
        """Per-event-type rate limiter. Returns True if allowed, False if dropped.

        Prevents bus event floods from overwhelming the pipeline. This is
        separate from recipe-level cooldowns — this is a hard gate BEFORE
        the pipeline even runs.
        """
        now = time.time()
        cutoff = now - window

        # A2 fix: evict stale categories when dict grows past cap.
        # Full sweep only triggers at 100+ keys to avoid per-call overhead.
        if len(self._bus_rate_counters) > 100:
            self._bus_rate_counters = {
                k: [t for t in ts if t > cutoff]
                for k, ts in self._bus_rate_counters.items()
                if any(t > cutoff for t in ts)
            }

        # Use the first dotted segment as the rate-limit key
        # e.g. "credit.limit_warning" -> "credit"
        key = event_name.split(".")[0] if "." in event_name else event_name

        if key not in self._bus_rate_counters:
            self._bus_rate_counters[key] = []

        # Prune old timestamps for this key
        self._bus_rate_counters[key] = [t for t in self._bus_rate_counters[key]
                                        if t > cutoff]
        timestamps = self._bus_rate_counters[key]

        if len(timestamps) >= max_per_window:
            self._bus_events_dropped += 1
            return False

        timestamps.append(now)
        return True

    async def _bus_consumer(self):
        """Consume bus events and route through pipeline.

        All bus recipes should use hotwire evaluation (no LLM) to avoid
        overwhelming the single inference slot. The rate limiter above
        provides a hard cap per event category.
        """
        while self._enabled:
            try:
                event = await self._bus_sub.next()
                event_name = event.get("event", event.get("event_type", event.get("type", "unknown")))

                # Hard rate-limit gate — drop before pipeline
                if not self._bus_rate_check(event_name):
                    if self._debug:
                        self._log.debug(f"BUS drop (rate): {event_name}")
                    continue

                self._bus_events_processed += 1

                envelope = Envelope(
                    trigger_type="on_event",
                    timestamp=time.time(),
                    fields={
                        "event_name": event_name,
                        **{k: str(v) for k, v in event.items()
                           if k not in ("event", "event_type", "type")},
                    },
                )

                matched = self.engine.match_recipes("on_event", envelope)
                for recipe_name in matched:
                    try:
                        result = await self.engine.run(recipe_name, envelope)
                        if result.action_result.name not in ("skip", "log", "manual_skip"):
                            self._log.info(
                                f"PIPELINE {recipe_name} [bus:{event_name}]: "
                                f"eval={result.eval_result.eval_type}->{result.eval_result.action} "
                                f"action={result.action_result.name} wall={result.wall_ms}ms")
                    except Exception as e:
                        self._log.error(f"Bus pipeline {recipe_name} failed: {e}")
            except Exception as e:
                self._log.error(f"Bus consumer error: {e}")
                await asyncio.sleep(1)

    # ── Queue backpressure check (runs on tick) ──

    def _check_queue_backpressure(self) -> Optional[Envelope]:
        """Check if the LLM inference queue is backed up.

        Returns an Envelope for the backpressure recipe if pressure detected,
        None otherwise. This is a synthetic event — no bus needed.
        """
        queue_depth = getattr(self.evaluator, "_queue_depth", 0)

        # Also check bus drop ratio as a pressure signal
        total = self._bus_events_processed + self._bus_events_dropped
        drop_rate = (self._bus_events_dropped / total * 100) if total > 0 else 0

        if queue_depth == 0 and drop_rate < 20:
            return None

        return Envelope(
            trigger_type="on_event",
            timestamp=time.time(),
            fields={
                "event_name": "thrall.backpressure",
                "queue_depth": str(queue_depth),
                "bus_processed": str(self._bus_events_processed),
                "bus_dropped": str(self._bus_events_dropped),
                "bus_drop_rate": f"{drop_rate:.0f}",
            },
        )

    # ── Sentinel reload ──

    async def _check_reload(self):
        """Check for recipe/prompt changes and reload if needed."""
        reload_file = os.path.join(self._plugin_dir, "thrall.reload")
        if os.path.exists(reload_file):
            try:
                mtime = os.path.getmtime(reload_file)
                if mtime > self._last_reload:
                    # Reload recipes + prompts
                    summary = load_all(self._plugin_dir, self.db, self.evaluator)
                    self.engine.load_recipes()
                    self._last_reload = time.time()
                    self._log.info(f"Recipes reloaded: {summary}")

                    # Check if backend config changed — hot-swap if needed
                    try:
                        import tomllib
                    except ImportError:
                        import tomli as tomllib
                    toml_path = os.path.join(self._plugin_dir, "plugin.toml")
                    if os.path.exists(toml_path):
                        with open(toml_path, "rb") as f:
                            new_config = tomllib.load(f)
                        new_thrall_cfg = new_config.get("config", {}).get("thrall", {})
                        new_backend_name = new_thrall_cfg.get("backend", "local")
                        if new_backend_name != self._current_backend_name:
                            new_backend = create_backend(
                                new_thrall_cfg, vault_get=self._ctx.vault_get)
                            self.evaluator.set_backend(new_backend)
                            self._current_backend_name = new_backend_name
                            self._log.info(f"Backend swapped to: {new_backend_name}")

                        # Check cascade L1 config changes
                        new_cascade = new_thrall_cfg.get("cascade", {})
                        new_cascade_enabled = new_cascade.get("enabled", False)
                        new_cascade_l1_type = new_cascade.get("l1_backend", "")
                        if new_cascade_enabled != self._cascade_enabled or \
                           new_cascade_l1_type != self._cascade_l1_type:
                            if new_cascade_enabled and new_cascade_l1_type:
                                try:
                                    l1_cfg = dict(new_thrall_cfg)
                                    l1_cfg["backend"] = new_cascade_l1_type
                                    new_l1 = create_backend(
                                        l1_cfg, vault_get=self._ctx.vault_get)
                                    self.evaluator.set_l1_backend(new_l1)
                                    self._log.info(f"Cascade L1 swapped: {new_l1.name}/{new_l1.model_name}")
                                except Exception as e:
                                    self._log.warning(f"Cascade L1 reload failed: {e}")
                            else:
                                self.evaluator.set_l1_backend(None)
                                self._log.info("Cascade L1 disabled via reload")
                            self._cascade_enabled = new_cascade_enabled
                            self._cascade_l1_type = new_cascade_l1_type

                    # Re-subscribe to bus if patterns changed (A1 fix:
                    # cancel old consumer task so it doesn't block on
                    # the stale subscription object)
                    if self._bus_sub and getattr(self._ctx, "subscribe_events", None):
                        new_patterns = self._collect_event_patterns()
                        try:
                            self._bus_sub = self._ctx.subscribe_events(*new_patterns)
                            if self._bus_consumer_task:
                                self._bus_consumer_task.cancel()
                            self._bus_consumer_task = asyncio.get_event_loop().create_task(
                                self._bus_consumer())
                            self._log.info(f"Bus re-subscribed: {new_patterns}")
                        except Exception as e:
                            self._log.warning(f"Bus re-subscribe failed: {e}")
            except Exception as e:
                self._log.error(f"Reload failed: {e}")

    # ── Action callbacks ──

    async def _send_mail(self, to_node: str, msg_type: str,
                         body: dict, session_id: str):
        """Send mail via PluginContext."""
        if self._ctx.send_mail:
            await self._ctx.send_mail(to_node, msg_type, body, session_id)
        else:
            self._log.warning(f"send_mail not available (would send to {to_node[:16]})")

    async def _call_skill(self, skill_name: str, input_data: dict) -> dict:
        """Call a skill via cockpit API. Async submit + poll for result."""
        if not self._cockpit_token:
            self._log.warning(f"call_skill: no cockpit token (would call {skill_name})")
            return {"status": "error", "message": "no cockpit token configured"}

        try:
            result = await asyncio.to_thread(
                self._cockpit_execute, skill_name, input_data)
            self._log.info(f"SKILL_CALL {skill_name}: {str(result)[:120]}")
            return result
        except Exception as e:
            self._log.error(f"SKILL_CALL {skill_name} failed: {e}")
            return {"status": "error", "message": str(e)}

    def _cockpit_execute(self, skill_name: str, skill_input: dict) -> dict:
        """Synchronous cockpit skill execution (runs in thread)."""
        # Build SSL context only for HTTPS URLs
        ssl_ctx = None
        if self._cockpit_url.startswith("https"):
            import ssl
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        payload = json.dumps({
            "skill": skill_name,
            "input": skill_input,
            "timeout": self._cockpit_call_timeout,
        }).encode()

        req = Request(
            f"{self._cockpit_url}/api/execute",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._cockpit_token}",
            },
        )
        resp = urlopen(req, timeout=self._cockpit_call_timeout + 5, context=ssl_ctx)
        data = json.loads(resp.read())
        job_id = data.get("job_id")
        if job_id:
            return self._poll_job(job_id, ssl_ctx)
        return data.get("result", data)

    def _poll_job(self, job_id: str, ssl_ctx) -> dict:
        """Poll cockpit for async job result.

        Configurable via plugin.toml [config.thrall]:
          poll_max_wait          — max seconds to poll (default 60)
          poll_initial_interval  — starting interval in seconds (default 2.0)

        Backoff: starts at poll_initial_interval, doubles each poll, caps at 5s.
        """
        max_wait = self._poll_max_wait
        interval = self._poll_initial_interval
        deadline = time.time() + max_wait
        while time.time() < deadline:
            req = Request(
                f"{self._cockpit_url}/api/jobs/{job_id}",
                headers={"Authorization": f"Bearer {self._cockpit_token}"},
            )
            try:
                resp = urlopen(req, timeout=10, context=ssl_ctx)
                data = json.loads(resp.read())
                status = data.get("status", "")
                if status == "completed":
                    return data.get("result", data.get("output_data", {}))
                if status == "failed":
                    return {"status": "error", "error": data.get("error", "job failed")}
            except URLError:
                pass
            time.sleep(interval)
            interval = min(interval * 1.5, 5.0)  # backoff, cap at 5s
        return {"status": "error", "error": f"job {job_id} timed out after {max_wait}s"}

    async def _summon_agent(self, briefing: dict, buffer_name: str,
                            trigger: str, entries: list):
        """Wake the agent with a structured briefing.

        Sends a system mail to self that the agent plugin picks up.
        This is the ONLY path that spins up a Claude session.

        briefing is a rich dict with mode ("respond" or "process"),
        full message context, and artifact paths.
        """
        # Inject dry_run into briefing so the agent session knows
        if self._dry_run:
            briefing["dry_run"] = True
            briefing["dry_run_instructions"] = (
                "DRY RUN MODE. Do NOT execute any actions (no mail, no skill calls). "
                "Instead, write a report to plugins/06-thrall/artifacts/"
                "dryrun-{timestamp}.md describing what you WOULD have done, "
                "including the reply you would have sent. "
                "This lets us observe thrall's judgment without consequences."
            )

        body = {
            "type": "thrall_digest",
            "wake_agent": True,
            "trigger": trigger,
            "buffer": buffer_name,
            "entry_count": len(entries),
            "briefing": briefing,
        }

        mode = briefing.get("mode", "respond")

        # Send mail to self — agent plugin's on_mail_received will pick it up
        if self._ctx.send_mail:
            own_node = self._ctx.node_id
            await self._ctx.send_mail(own_node, "system", body,
                                       f"thrall:{buffer_name}")
            self._log.info(f"SUMMON [{mode}]: agent woken via system mail "
                          f"(trigger={trigger}, entries={len(entries)})")
        else:
            self._log.warning(f"Cannot summon agent — no send_mail in context")
            self._log.info(f"SUMMON (dry) [{mode}]: {json.dumps(briefing)[:300]}")


def _extract_body_text(body: Any) -> str:
    """Extract readable text from mail body (various formats)."""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        for key in ("content", "text", "message", "summary"):
            if key in body:
                val = body[key]
                return val if isinstance(val, str) else json.dumps(val)
        return json.dumps(body)
    return str(body)

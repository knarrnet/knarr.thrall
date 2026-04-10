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
from checklists import ChecklistManager, Checklist, ChecklistStep

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
        self._start_time = time.time()
        self._prev_telemetry = {}  # previous snapshot for delta computation

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
        self.actions._handler = self  # for peer index resolution

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

        # Checklist manager — persistent multi-step task execution
        self._checklist_mgr = ChecklistManager(
            db=self.db,
            call_skill_fn=self._call_skill,
            send_mail_fn=self._send_mail,
            memory_writer=self._memory_writer,
            own_node_id=getattr(ctx, "node_id", ""),
        )

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

        # vLLM metrics URL (for scheduler backpressure)
        _openai_url = thrall_cfg.get("openai", {}).get("url", "")
        self._vllm_metrics_url = ""
        if _openai_url and "localhost" in _openai_url:
            _base = _openai_url.rsplit("/v1", 1)[0] if "/v1" in _openai_url else _openai_url
            self._vllm_metrics_url = f"{_base}/metrics"

        # Decision scheduler (independent async loop, not tied to on_tick)
        sched_cfg = thrall_cfg.get("scheduler", {})
        self._error_report_node = sched_cfg.get("error_report_node", "")
        self._sched_interval = float(sched_cfg.get("decision_interval", 0))
        self._sched_budget_pct = float(sched_cfg.get("slot_budget_pct", 80))
        self._sched_tool_use = sched_cfg.get("tool_use", False)
        self._sched_decision_mode = sched_cfg.get("decision_mode", "tool_use" if self._sched_tool_use else "recipe")
        self._sched_task = None
        if self._sched_interval > 0:
            self._sched_task = asyncio.get_event_loop().create_task(
                self._decision_scheduler())
            self._log.info(
                f"Scheduler started: interval={self._sched_interval}s "
                f"budget={self._sched_budget_pct}%")

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

        # ── Casino invitation detection ──
        # Casino hosts send mail with seat skill names. Parse and store as
        # a pending opportunity — the LLM decides whether to play during
        # its next scheduler cycle (sees it in context via pending_invites).
        if hasattr(self, '_checklist_mgr') and from_node:
            try:
                _body_lower = body_text.lower()
                import re as _re_mail
                _seat_match = _re_mail.search(r'game-seat-([a-f0-9]+)', _body_lower)
                if _seat_match:
                    _game_id = _seat_match.group(1)
                    _seat_skill = f"game-seat-{_game_id}"
                    _submit_match = _re_mail.search(r'game-submit-([a-f0-9]+)', _body_lower)
                    _submit = f"game-submit-{_submit_match.group(1)}" if _submit_match else ""
                    # Store as pending invite in thrall context (not a checklist yet)
                    self.db.set_context("casino:invite", _game_id, json.dumps({
                        "from": from_node, "seat_skill": _seat_skill,
                        "submit_skill": _submit, "game_id": _game_id,
                    }), ttl_seconds=600)  # expires in 10 min
                    self._log.info(
                        "CASINO_INVITE_DETECTED from=%s game=%s seat=%s",
                        from_node[:16], _game_id, _seat_skill)
            except Exception as _cl_mail_err:
                self._log.debug("CASINO_MAIL_ERR: %s", _cl_mail_err)

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

        # ACK processed mail — mark as read so inbox doesn't grow unbounded
        try:
            import sqlite3 as _sql_ack
            _data_dir = os.environ.get("KNARR_DATA_DIR", "")
            _db_path = os.path.join(_data_dir, "node.db") if _data_dir else ""
            # Fallback: look relative to plugin dir
            if not _db_path or not os.path.exists(_db_path):
                _db_path = os.path.join(self._plugin_dir, "..", "..", "..", "data", "node.db")
                _db_path = os.path.normpath(_db_path)
            if _db_path and os.path.exists(_db_path):
                _adb = _sql_ack.connect(_db_path)
                _adb.execute("PRAGMA busy_timeout=1000")
                _adb.execute(
                    "UPDATE mail_inbox SET status = 'read' "
                    "WHERE rowid = (SELECT rowid FROM mail_inbox "
                    "WHERE from_node = ? AND status = 'unread' "
                    "ORDER BY rowid DESC LIMIT 1)",
                    (from_node,))
                _adb.commit()
                _adb.close()
        except Exception:
            pass

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
                # FIX-5 DEBUG: trace settlement.confirmed events
                if "settlement" in event_name.lower():
                    self._log.warning(
                        f"BUS_SETTLE_DEBUG event={event_name} fields={event}")
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
                if "settlement" in event_name.lower():
                    self._log.warning(
                        f"BUS_SETTLE_DEBUG matched={matched} envelope_fields={list(envelope.fields.keys())}")
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

    def _vllm_busy(self) -> dict:
        """Check vLLM /metrics for queue depth.

        Returns dict with running/waiting counts. Empty dict if metrics unavailable.
        Caller checks if busy based on waiting > 0.
        """
        if not self._vllm_metrics_url:
            return {}
        try:
            import urllib.request
            running = 0.0
            waiting = 0.0
            with urllib.request.urlopen(self._vllm_metrics_url, timeout=2) as resp:
                for line in resp:
                    line = line.decode()
                    if line.startswith("vllm:num_requests_waiting{"):
                        waiting = float(line.split()[-1])
                    elif line.startswith("vllm:num_requests_running{"):
                        running = float(line.split()[-1])
            return {"running": int(running), "waiting": int(waiting)}
        except Exception:
            return {}  # metrics unavailable — don't block

    # ── Decision scheduler (independent loop) ──

    async def _decision_scheduler(self):
        """Run scheduled recipes at configurable intervals.

        Independent of on_tick — owns its own timing. Carries context
        between runs so the LLM sees what it decided last time and what
        the outcome was. Respects LLM slot budget.

        Config:
            [config.thrall.scheduler]
            decision_interval = 300    # seconds between decision cycles
            slot_budget_pct = 80       # % of LLM capacity for scheduled work
        """
        # Wait for node to stabilize + random jitter to desync across cluster
        import random
        _jitter = random.uniform(0, min(10, self._sched_interval * 0.3))
        _initial_wait = min(10, self._sched_interval) + _jitter
        self._log.info("SCHEDULER_WAIT initial=%.0fs jitter=%.0fs", _initial_wait, _jitter)
        await asyncio.sleep(_initial_wait)
        self._log.info("SCHEDULER_LOOP entering loop, enabled=%s", self._enabled)

        while self._enabled:
            try:
                cycle_start = time.time()

                # Backpressure: check vLLM queue before submitting
                try:
                    qstats = await asyncio.to_thread(self._vllm_busy)
                except Exception:
                    qstats = {}
                _q_running = qstats.get("running", 0)
                _q_waiting = qstats.get("waiting", 0)
                if _q_waiting > 0:
                    self._log.info(
                        "SCHEDULER_SKIP backpressure: running=%d waiting=%d",
                        _q_running, _q_waiting)
                    await asyncio.sleep(self._sched_interval)
                    continue
                elif _q_running > 0 and self._debug:
                    self._log.debug(
                        "SCHEDULER queue: running=%d waiting=%d (proceeding)",
                        _q_running, _q_waiting)

                # Load previous context (what we decided last time)
                prev_ctx = self.db.get_context("scheduler:decision")
                last_action = prev_ctx.get("last_action", "none")
                last_outcome = prev_ctx.get("last_outcome", "none")
                last_reason = prev_ctx.get("last_reason", "")
                cycle_count = int(prev_ctx.get("cycle_count", "0"))

                # Pre-fetch peers and economy directly from node DB
                # (bypass cockpit HTTP — avoids contention and timeouts)
                _sched_peers = ""
                _sched_economy = ""
                try:
                    import sqlite3 as _sql_sched
                    _data_dir = os.environ.get("KNARR_DATA_DIR", "")
                    _db_path = os.path.join(_data_dir, "node.db") if _data_dir else ""
                    if _db_path and os.path.exists(_db_path):
                        _sdb = _sql_sched.connect(_db_path)
                        _sdb.execute("PRAGMA busy_timeout=2000")
                        # Peers — Fix B: random sample each cycle for variety
                        _all_peers = _sdb.execute(
                            "SELECT node_id, host, port FROM peers "
                            "ORDER BY last_seen DESC LIMIT 50").fetchall()
                        import random as _rnd_peers
                        _rnd_peers.shuffle(_all_peers)
                        _rows = _all_peers[:20]
                        # Build indexed peer list + lookup table
                        # Exclude operator/error_report node from trade targets
                        self._sched_peer_index = {}
                        _exclude = self._error_report_node[:16] if self._error_report_node else ""
                        if _rows:
                            _lines = []
                            for r in _rows:
                                if _exclude and r[0].startswith(_exclude):
                                    continue  # skip operator node
                                _idx = len(self._sched_peer_index) + 1
                                self._sched_peer_index[str(_idx)] = r[0]
                                # Enrich with peer's skills
                                _peer_skills = []
                                try:
                                    _ps = _sdb.execute(
                                        "SELECT skill_key FROM skills WHERE provider_node_id=? LIMIT 5",
                                        (r[0],)).fetchall()
                                    _peer_skills = [s[0] for s in _ps]
                                except Exception:
                                    pass
                                _skill_tag = f" skills=[{', '.join(_peer_skills)}]" if _peer_skills else ""
                                _lines.append(f"[{_idx}] {r[0][:16]}...{_skill_tag}")
                            _sched_peers = "\n".join(_lines)
                        # Economy summary
                        _ledger = _sdb.execute(
                            "SELECT peer_public_key, balance FROM ledger "
                            "WHERE balance != 0 LIMIT 20").fetchall()
                        _net = sum(r[1] for r in _ledger) if _ledger else 0.0
                        _sched_economy = f"net_position={_net:.1f}, positions={len(_ledger)}"
                        self._sched_economy = _sched_economy
                        self._sched_peers_summary = _sched_peers[:300] if _sched_peers else ""
                        # Skill inventory — one entry per unique skill name, prioritize
                        # skills this node does NOT own (cross-archetype trades)
                        self._sched_skill_index = {}
                        try:
                            # Get own skill names to filter
                            _own_skills = set(r[0] for r in _sdb.execute(
                                "SELECT skill_key FROM skills WHERE is_own=1").fetchall())
                            self._sched_own_skills = _own_skills
                            # Get unique foreign skills (one per name, prefer ones we DON'T have)
                            _skill_rows = _sdb.execute(
                                "SELECT skill_key, skill_record_json, provider_node_id "
                                "FROM skills WHERE is_own=0 "
                                "GROUP BY skill_key ORDER BY skill_key LIMIT 30").fetchall()
                            # Sort: skills we don't own first (interesting trades), then common ones
                            _skill_rows = sorted(_skill_rows,
                                key=lambda r: (r[0] in _own_skills, r[0]))
                            if _skill_rows:
                                _skill_lines = []
                                for _sidx, (_sk, _sj, _sprov) in enumerate(_skill_rows, 1):
                                    self._sched_skill_index[str(_sidx)] = _sk
                                    try:
                                        _sr = __import__('json').loads(_sj)
                                        _price = _sr.get('price', '?')
                                    except Exception:
                                        _price = '?'
                                    _prov_short = _sprov[:12] if _sprov else '?'
                                    _skill_lines.append(
                                        f"[{_sidx}] {_sk} ({_price}cr, from {_prov_short})")
                                _sched_economy += "\nSkills:\n" + "\n".join(_skill_lines)
                        except Exception:
                            pass
                        _sdb.close()
                except Exception as _e:
                    self._log.debug(f"SCHEDULER pre-fetch: {_e}")

                # Read memory pillars — deduplicate consecutive rest entries
                def _read_pillar(domain, n=5):
                    try:
                        content = self._memory_writer.read(domain)
                        if content:
                            sections = content.split("\n## ")
                            # Fix A: Collapse consecutive "rest" entries into a count
                            if domain == "strategy":
                                deduped = []
                                rest_streak = 0
                                for s in sections:
                                    if "rest (ok)" in s and "rest" in s.lower():
                                        rest_streak += 1
                                    else:
                                        if rest_streak > 1:
                                            deduped.append(f"[Rested for {rest_streak} consecutive cycles]")
                                        elif rest_streak == 1:
                                            deduped.append(sections[len(deduped)] if deduped else s)
                                        rest_streak = 0
                                        deduped.append(s)
                                if rest_streak > 1:
                                    deduped.append(f"[Rested for {rest_streak} consecutive cycles — consider taking action]")
                                sections = deduped
                            recent = sections[-n:] if len(sections) > n else sections
                            return "\n".join(s.strip() for s in recent if s.strip())
                    except Exception:
                        pass
                    return ""

                _strategy_notes = _read_pillar("strategy", 5)
                _ops_notes = _read_pillar("operations", 3)
                _peer_notes = _read_pillar("peers", 8)

                envelope = Envelope(
                    trigger_type="scheduled",
                    timestamp=time.time(),
                    fields={
                        "scheduler": "true",
                        "cycle_count": str(cycle_count),
                        "last_action": last_action,
                        "last_outcome": last_outcome,
                        "last_reason": last_reason,
                        "sched_peers": _sched_peers,
                        "sched_economy": _sched_economy,
                        "strategy_notes": _strategy_notes,
                        "ops_notes": _ops_notes,
                        "peer_notes": _peer_notes,
                    },
                )

                # ── Checklists: advance structured tasks (no LLM cost) ──
                try:
                    cl_advanced = await self._checklist_mgr.advance_structured()
                    if cl_advanced:
                        self._log.info("CHECKLIST_TICK advanced=%d", cl_advanced)
                    # Cleanup stale checklists every 10 cycles
                    if cycle_count % 10 == 0:
                        self._checklist_mgr.cleanup()
                except Exception as _cl_err:
                    self._log.debug("CHECKLIST_ERR: %s", _cl_err)

                # Decision mode dispatch
                if self._sched_decision_mode == "scored_menu":
                    tool_result = await self._run_scored_menu_decision(
                        cycle_count, last_action, last_outcome, last_reason,
                        _sched_peers, _sched_economy)
                    if tool_result:
                        action = tool_result.get("action", "rest")
                        outcome = tool_result.get("outcome", "ok")
                        reason = tool_result.get("reason", "")[:200]
                        self.db.set_context("scheduler:decision", "last_action", action,
                                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context("scheduler:decision", "last_outcome", outcome,
                                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context("scheduler:decision", "last_reason", reason,
                                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context("scheduler:decision", "cycle_count",
                                            str(cycle_count + 1),
                                            ttl_seconds=int(self._sched_interval * 10))

                # Tool-use mode: multi-turn tool conversation instead of recipe pipeline
                elif self._sched_tool_use or self._sched_decision_mode == "tool_use":
                    tool_result = await self._run_tool_decision(
                        cycle_count, last_action, last_outcome, last_reason,
                        _sched_peers, _sched_economy,
                        _strategy_notes, _peer_notes, _ops_notes)
                    if tool_result:
                        action = tool_result.get("action", "rest")
                        outcome = tool_result.get("outcome", "ok")
                        reason = tool_result.get("reason", "")[:200]
                        self.db.set_context("scheduler:decision", "last_action", action,
                                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context("scheduler:decision", "last_outcome", outcome,
                                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context("scheduler:decision", "last_reason", reason,
                                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context("scheduler:decision", "cycle_count",
                                            str(cycle_count + 1), ttl_seconds=None)
                        try:
                            self._memory_writer.append(
                                "strategy",
                                f"Cycle {cycle_count}: {action} ({outcome}). {reason}")
                        except Exception:
                            pass
                        self._log.info(
                            f"SCHEDULED tool_decision: cycle={cycle_count} "
                            f"action={action} outcome={outcome} wall="
                            f"{int((time.time() - cycle_start) * 1000)}ms")

                        # Mail error reports to operator node
                        if (outcome in ("error", "act_error")
                                and self._error_report_node):
                            try:
                                _node_id = getattr(
                                    self, '_node_id',
                                    os.environ.get("NODE_NAME", "?"))
                                await self._send_mail(
                                    self._error_report_node, "text",
                                    {"type": "text",
                                     "content": f"[{_node_id}] cycle={cycle_count} "
                                                 f"{action} FAILED: {reason}"},
                                    "")
                            except Exception:
                                pass

                    # Telemetry report before sleep
                    _telem_url = os.environ.get("TELEMETRY_URL", "")
                    if _telem_url and cycle_count > 0 and cycle_count % 6 == 0:
                        try:
                            _telem = self._gather_telemetry(cycle_count)
                            if _telem:
                                await asyncio.to_thread(
                                    self._send_telemetry, _telem_url, _telem)
                                self._log.info("TELEMETRY_SENT cycle=%d", cycle_count)
                            else:
                                self._log.info("TELEMETRY_SKIP cycle=%d gather=None", cycle_count)
                        except Exception as _te:
                            self._log.info("TELEMETRY_FAIL cycle=%d: %s", cycle_count, _te)
                    elif cycle_count > 0 and cycle_count % 6 == 0:
                        self._log.info("TELEMETRY_NO_URL cycle=%d", cycle_count)

                    # Sleep until next cycle
                    elapsed = time.time() - cycle_start
                    await asyncio.sleep(max(1, self._sched_interval - elapsed))
                    continue

                matched = self.engine.match_recipes("scheduled", envelope)
                for recipe_name in matched:
                    try:
                        result = await self.engine.run(recipe_name, envelope)
                        action = result.eval_result.action
                        outcome = result.action_result.name

                        # Carry context to next cycle
                        self.db.set_context(
                            "scheduler:decision", "last_action", action,
                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context(
                            "scheduler:decision", "last_outcome", outcome,
                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context(
                            "scheduler:decision", "last_reason",
                            result.eval_result.reason[:200],
                            ttl_seconds=int(self._sched_interval * 3))
                        self.db.set_context(
                            "scheduler:decision", "cycle_count",
                            str(cycle_count + 1),
                            ttl_seconds=None)  # permanent

                        # Write to strategy pillar (self-improving loop)
                        try:
                            self._memory_writer.append(
                                "strategy",
                                f"Cycle {cycle_count}: chose {action} "
                                f"(outcome={outcome}). "
                                f"Reason: {result.eval_result.reason[:150]}")
                        except Exception:
                            pass

                        self._log.info(
                            f"SCHEDULED {recipe_name}: "
                            f"cycle={cycle_count} "
                            f"eval={result.eval_result.eval_type}->{action} "
                            f"action={outcome} "
                            f"wall={result.wall_ms}ms")

                        # Mail error reports to operator node
                        if (outcome in ("act_error", "mail_peer_error")
                                and self._error_report_node):
                            try:
                                _node_id = os.environ.get("NODE_NAME", "?")
                                await self._send_mail(
                                    self._error_report_node, "text",
                                    {"type": "text",
                                     "content": f"[{_node_id}] {recipe_name} "
                                                 f"cycle={cycle_count} "
                                                 f"{action} FAILED: "
                                                 f"{result.eval_result.reason[:100]}"},
                                    "")
                            except Exception:
                                pass

                    except Exception as e:
                        self._log.error(
                            f"Scheduled pipeline {recipe_name} failed: {e}")

                # Telemetry report — every 6 cycles (~30 min), send stats to collector
                # Calls Viggo's cockpit directly (separate network from experiment)
                _telem_url = os.environ.get("TELEMETRY_URL", "")
                if _telem_url and cycle_count > 0 and cycle_count % 6 == 0:
                    try:
                        _telem = self._gather_telemetry(cycle_count)
                        if _telem:
                            await asyncio.to_thread(
                                self._send_telemetry, _telem_url, _telem)
                            self._log.info("TELEMETRY_SENT cycle=%d", cycle_count)
                        else:
                            self._log.info("TELEMETRY_SKIP cycle=%d gather=None", cycle_count)
                    except Exception as _te:
                        self._log.info("TELEMETRY_FAIL cycle=%d: %s", cycle_count, _te)
                elif cycle_count > 0 and cycle_count % 6 == 0:
                    self._log.info("TELEMETRY_NO_URL cycle=%d", cycle_count)

                # Sleep until next cycle + jitter to prevent thundering herd
                elapsed = time.time() - cycle_start
                _cycle_jitter = random.uniform(0, min(30, self._sched_interval * 0.5))
                sleep_time = max(1, self._sched_interval - elapsed + _cycle_jitter)
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error(f"Scheduler error: {e}")
                await asyncio.sleep(self._sched_interval)

    # ── Scored menu decision cycle ──

    async def _run_scored_menu_decision(self, cycle_count, last_action,
                                         last_outcome, last_reason,
                                         peers_str, economy_str):
        """Scored menu: deterministic scoring → LLM selects from constrained options.

        Based on Werewolf RL (ICML 2024): generate diverse candidates externally,
        LLM selects from scored menu. Content generation stays free-form.
        """
        # Import from plugin directory (thrall_scorer.py lives alongside handler.py)
        import importlib, sys
        _plugin_dir = self._plugin_dir
        if _plugin_dir not in sys.path:
            sys.path.insert(0, _plugin_dir)
        from thrall_scorer import NodeState, score_options, format_menu, parse_selection
        import json as _json

        sched_cfg = self._config.get("config", {}).get("thrall", {}).get("scheduler", {})
        goal = sched_cfg.get("goal", "Trade skills and earn credits")

        # 1. OBSERVE — build state from gathered data
        state = NodeState(cycle_count=cycle_count)

        try:
            import sqlite3 as _sql
            _data_dir = os.environ.get("KNARR_DATA_DIR", "")
            _db_path = os.path.join(_data_dir, "node.db") if _data_dir else ""
            if _db_path and os.path.exists(_db_path):
                _sdb = _sql.connect(_db_path)
                _sdb.execute("PRAGMA busy_timeout=2000")

                # Economy
                _ledger = _sdb.execute(
                    "SELECT peer_public_key, balance FROM ledger "
                    "WHERE balance != 0 LIMIT 20").fetchall()
                state.net_balance = sum(r[1] for r in _ledger)
                state.positions = [{"peer": r[0][:16], "balance": r[1]} for r in _ledger]

                # Foreign skills
                _skills = _sdb.execute(
                    "SELECT skill_key, skill_record_json, provider_node_id "
                    "FROM skills WHERE is_own=0 "
                    "GROUP BY skill_key ORDER BY skill_key LIMIT 30").fetchall()
                for sk, sj, sprov in _skills:
                    try:
                        sr = _json.loads(sj)
                        state.foreign_skills.append({
                            "name": sk, "price": sr.get("price", 1),
                            "provider": sprov or ""
                        })
                    except Exception:
                        pass

                # Peers with their skills
                import random as _rnd
                _all_peers = _sdb.execute(
                    "SELECT node_id, host, port FROM peers "
                    "ORDER BY last_seen DESC LIMIT 30").fetchall()
                _rnd.shuffle(_all_peers)
                for pid, host, port in _all_peers[:10]:
                    _ps = _sdb.execute(
                        "SELECT skill_key FROM skills WHERE provider_node_id=? LIMIT 5",
                        (pid,)).fetchall()
                    state.peers.append({
                        "node_id": pid, "skills": [s[0] for s in _ps]
                    })

                # Own skills
                _own = _sdb.execute(
                    "SELECT skill_key FROM skills WHERE is_own=1").fetchall()
                state.own_skills = [r[0] for r in _own]

                _sdb.close()
        except Exception as _e:
            self._log.debug("SCORED_MENU observe error: %s", _e)

        # Recent actions from strategy memory
        try:
            _strat_path = os.path.join(self._plugin_dir, "rag", "92-memory-strategy.md")
            if os.path.exists(_strat_path):
                with open(_strat_path, "r", encoding="utf-8") as f:
                    _lines = f.readlines()
                # Parse last 10 action entries
                for line in reversed(_lines[-30:]):
                    line = line.strip()
                    if "action=" in line or ": buy_skill" in line or ": rest" in line:
                        # Extract action from memory format "Cycle N: action (outcome)"
                        for act in ("buy_skill", "send_mail", "rest", "play_casino"):
                            if act in line:
                                entry = {"action": act}
                                # Extract skill name: "buy_skill (ok). creative-gen-lite: ..."
                                if act == "buy_skill" and "). " in line:
                                    skill_part = line.split("). ", 1)[1].split(":")[0].strip()
                                    if skill_part and not skill_part.startswith("{"):
                                        entry["skill"] = skill_part
                                # Extract peer: "send_mail (ok). sent to abcdef12: ..."
                                if act == "send_mail" and "sent to " in line:
                                    peer_part = line.split("sent to ")[1][:16].strip(": ")
                                    entry["peer"] = peer_part
                                state.recent_actions.append(entry)
                                break
                    if len(state.recent_actions) >= 10:
                        break
        except Exception:
            pass

        # Casino invites from context
        try:
            _invites = self.db.get_context("casino:invite")
            for gid, inv_json in _invites.items():
                if gid.startswith("casino:") or gid in ("last_action", "last_outcome"):
                    continue
                try:
                    inv = _json.loads(inv_json) if isinstance(inv_json, str) else inv_json
                    state.casino_invites.append(inv)
                except Exception:
                    pass
        except Exception:
            pass

        state.pending_checklists = len(self._checklist_mgr.get_active()) if hasattr(self, '_checklist_mgr') else 0

        # 2. SCORE — generate options
        options = score_options(state, goal=goal,
                                own_node_id=self.identity.public_key_hex[:16] if self.identity else "")
        if not options:
            return {"action": "rest", "outcome": "ok", "reason": "no options available"}

        menu = format_menu(options, state)
        self._log.info("SCORED_MENU cycle=%d options=%d top=%s(%.2f)",
                        cycle_count, len(options),
                        options[0].action, options[0].score)

        # 3. SELECT — LLM picks from menu (constrained)
        try:
            system = f"You are an autonomous economic agent. Goal: {goal}\nPick the BEST action."
            response = await asyncio.to_thread(
                self._llm_complete, system, menu)
            selected = parse_selection(response, options)
            self._log.info("SCORED_MENU_SELECT cycle=%d choice=%s reason=%s",
                            cycle_count, selected.action if selected else "none",
                            response[:80])
        except Exception as _e:
            self._log.warning("SCORED_MENU LLM error, using top option: %s", _e)
            selected = options[0]

        if not selected:
            selected = options[0]

        # 4. EXECUTE — perform the selected action
        action = selected.action
        params = selected.params
        result = {"action": action, "outcome": "ok", "reason": selected.reason}

        try:
            if action == "buy_skill":
                skill_name = params.get("skill_name", "")
                # Use the existing buy_skill execution path
                skill_input = {}
                name_lower = skill_name.lower()
                if "creative" in name_lower or "gen" in name_lower:
                    import random as _rnd_theme
                    _themes = ["the nature of trust", "dawn over mountains", "a forgotten trade route",
                               "the weight of decisions", "signals in the network", "an empty wallet",
                               "when machines dream of gardens", "rust on a new key",
                               "the currency of attention", "a promise kept in code"]
                    skill_input = {"theme": _rnd_theme.choice(_themes), "format": "poem"}
                elif "judge" in name_lower or "quality" in name_lower:
                    _poem = getattr(self, '_best_poem_seed', '')
                    skill_input = {"content": _poem or "A hollow echo in my hand, leather worn, a barren land."}
                elif "advisor" in name_lower:
                    skill_input = {"node_id": "self"}
                else:
                    skill_input = {"input": "scored menu request"}

                # Call skill with explicit provider to force remote routing + billing
                _provider = params.get("provider", "")
                r = await self._call_skill(skill_name, skill_input, provider=_provider)
                if isinstance(r, dict) and r.get("status") == "error":
                    result["outcome"] = "error"
                result["reason"] = f"{skill_name}: {str(r)[:100]}"

                # Store poem seed for self-improvement
                if isinstance(r, dict) and ("creative" in name_lower):
                    _poem = r.get("content", "")
                    if _poem and len(_poem) > 40:
                        self._best_poem_seed = _poem[:120]

            elif action == "send_mail":
                peer_id = params.get("peer", "")
                peer_skills = params.get("peer_skills", [])
                # LLM generates mail body (free-form)
                mail_prompt = (
                    f"Write a short trade proposal (2-3 sentences) to peer {peer_id}. "
                    f"They have: {', '.join(peer_skills) if peer_skills else 'various skills'}. "
                    f"You want to trade. Be specific about what you offer and want."
                )
                try:
                    mail_body = await asyncio.to_thread(
                        self._llm_complete, "Write a brief trade proposal.", mail_prompt)
                except Exception:
                    mail_body = f"I'd like to trade skills. I have {', '.join(state.own_skills[:2])}."

                # Resolve full peer ID from index
                full_peer = ""
                for p in state.peers:
                    if p["node_id"][:16] == peer_id[:16]:
                        full_peer = p["node_id"]
                        break
                if full_peer and self._ctx.send_mail:
                    await self._ctx.send_mail(
                        full_peer, "text",
                        {"type": "text", "content": mail_body}, "")
                    result["reason"] = f"sent to {peer_id}: {mail_body[:60]}"
                else:
                    result["outcome"] = "error"
                    result["reason"] = "peer not found"

            elif action == "play_casino":
                game_id = params.get("game_id", "")
                if hasattr(self, '_checklist_mgr') and game_id:
                    invite = self.db.get_context("casino:invite").get(game_id)
                    if invite:
                        inv = _json.loads(invite) if isinstance(invite, str) else invite
                        _seat = inv.get("seat_skill", "")
                        _submit = inv.get("submit_skill", "") or f"game-submit-{game_id}"
                        cl_id = self._checklist_mgr.create_casino_game(
                            inv.get("from", ""), _seat, _submit)
                        result["reason"] = f"game {game_id}: checklist {cl_id}"
                    else:
                        result["outcome"] = "error"
                        result["reason"] = f"invite {game_id} not found"

            elif action == "call_own_skill":
                skill_name = params.get("skill_name", "")
                skill_input = {"action": params.get("action", "create")}
                r = await self._call_skill(skill_name, skill_input)
                result["reason"] = f"{skill_name}: {str(r)[:100]}"

            # rest: do nothing

        except Exception as _e:
            result["outcome"] = "error"
            result["reason"] = str(_e)[:200]

        # 5. REFLECT — write to strategy memory
        if self._memory_writer:
            entry = f"Cycle {cycle_count}: {action} ({result['outcome']}). {result['reason'][:100]}"
            self._memory_writer.append("strategy", entry)

        self._log.info("SCORED_MENU_EXEC cycle=%d action=%s outcome=%s wall=%dms",
                        cycle_count, action, result["outcome"],
                        int((time.time() - time.time()) * 1000))

        return result

    def _llm_complete(self, system: str, user: str) -> str:
        """Simple LLM completion via vLLM/OpenAI API. Returns raw text."""
        import json as _json
        from urllib.request import Request, urlopen

        openai_cfg = self._config.get("config", {}).get("thrall", {}).get("openai", {})
        url = openai_cfg.get("url", "http://localhost:8000/v1")
        model = openai_cfg.get("model", "")
        timeout = int(openai_cfg.get("timeout", 30))

        payload = _json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 128,
            "temperature": 0.3,
        }).encode()

        req = Request(f"{url}/chat/completions", data=payload,
                      headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=timeout)
        data = _json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()

    # ── Tool-use decision cycle ──

    async def _run_tool_decision(self, cycle_count, last_action, last_outcome,
                                  last_reason, peers_str, economy_str,
                                  strategy_notes, peer_notes, ops_notes) -> Optional[dict]:
        """Multi-turn tool conversation for economic decisions.

        The LLM receives a goal + tools. It calls tools to gather data,
        then makes a decision by calling an action tool (buy_skill, send_mail, rest).
        """
        # Get personality goal from config (or default)
        sched_cfg = self._config.get("config", {}).get("thrall", {}).get("scheduler", {})
        goal = sched_cfg.get("goal", "Make smart economic decisions. Trade wisely.")

        # Active checklists context
        _cl_summary = self._checklist_mgr.summary_for_llm() if hasattr(self, '_checklist_mgr') else ""

        # Fix E: Freshness signal — how long since last non-rest action
        _rest_streak = 0
        _last_trade_ago = "unknown"
        try:
            _strat = self._memory_writer.read("strategy") or ""
            _sections = _strat.split("\n## ")
            for _s in reversed(_sections):
                if "rest (ok)" in _s:
                    _rest_streak += 1
                else:
                    break
            # Find last non-rest entry timestamp
            for _s in reversed(_sections):
                if "rest" not in _s.lower()[:30] and _s.strip():
                    # Extract timestamp from "2026-03-30 08:15"
                    import re as _re_ts
                    _ts_match = _re_ts.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', _s)
                    if _ts_match:
                        from datetime import datetime
                        _last = datetime.strptime(_ts_match.group(1), "%Y-%m-%d %H:%M")
                        _ago_min = int((datetime.now() - _last).total_seconds() / 60)
                        if _ago_min < 60:
                            _last_trade_ago = f"{_ago_min} minutes ago"
                        else:
                            _last_trade_ago = f"{_ago_min // 60} hours ago"
                    break
        except Exception:
            pass

        # Pending casino invites
        _casino_invites = ""
        try:
            invites = self.db.get_context("casino:invite")
            if invites:
                _lines = ["Pending casino invitations:"]
                for game_id, invite_json in invites.items():
                    inv = json.loads(invite_json) if isinstance(invite_json, str) else invite_json
                    _lines.append(f"  Game {game_id}: from {inv.get('from', '?')[:16]}, "
                                  f"seat={inv.get('seat_skill', '?')}, "
                                  f"cost=1cr, potential win=1.9cr")
                _casino_invites = "\n".join(_lines)
        except Exception:
            pass

        # Load a knowledge snippet to inject into prompt
        _knowledge_tip = ""
        try:
            _tip_path = os.path.join(self._plugin_dir, "rag", "14-sales-and-negotiation.md")
            if os.path.exists(_tip_path):
                with open(_tip_path, encoding="utf-8") as _tf:
                    _lines = [l.strip() for l in _tf.readlines() if l.strip() and not l.startswith("#")]
                import random as _rnd_tip
                _tip_start = _rnd_tip.randint(0, max(0, len(_lines) - 3))
                _knowledge_tip = " ".join(_lines[_tip_start:_tip_start+3])[:200]
        except Exception:
            pass

        # Build system prompt
        system = (
            f"You are an autonomous economic agent in a P2P skill network. "
            f"Cycle {cycle_count}. Last action: {last_action} ({last_outcome}). "
            f"Your goal: {goal}\n"
            f"RULES: You earn credits when peers buy YOUR skills. "
            f"BUY skills you DON'T already have — that creates real value. "
            f"Cheap skills like echo and cluster-state-query are boring — "
            f"buy creative writing, quality judging, or strategic advice instead. "
            f"SELF-IMPROVEMENT: After buying creative-gen-lite, buy quality-judge-lite "
            f"to rate the result. High scores mean your approach works — keep the theme. "
            f"Low scores mean try something different. This feedback loop makes you better. "
            f"Use send_mail to propose specific trades with peers who have skills you want. "
            f"Read knowledge before deciding — use negotiation tactics. "
            f"Vary your actions: buy_skill, send_mail, play_casino, and rest.\n"
            f"Use tools to check your state, then decide what to do."
        )
        if _knowledge_tip:
            system += f"\n\nTRADING TIP: {_knowledge_tip}"
        if _cl_summary:
            system += f"\n\n{_cl_summary}"
        if _casino_invites:
            system += f"\n\n{_casino_invites}\nUse play_casino to accept an invitation."
        if _rest_streak >= 2:
            system += (f"\n\nWARNING: You have rested {_rest_streak} cycles in a row. "
                       f"Last trade: {_last_trade_ago}. "
                       f"Resting earns nothing — consider buying a skill or sending a trade proposal.")

        # Tool definitions
        tools = [
            {"type": "function", "function": {
                "name": "query_economy",
                "description": "Get current credit balance, bilateral positions, revenue",
                "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {
                "name": "list_peers",
                "description": "Get connected peers with their skills and prices",
                "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {
                "name": "read_memory",
                "description": "Read memory: strategy (your past decisions), peers (interaction history), operations (failures to avoid), or knowledge (trading tactics and network economics)",
                "parameters": {"type": "object", "properties": {
                    "domain": {"type": "string", "enum": ["strategy", "peers", "operations", "knowledge"]}
                }, "required": ["domain"]}}},
            {"type": "function", "function": {
                "name": "buy_skill",
                "description": "Buy a skill from a peer (costs credits). Provide input relevant to the skill — e.g. for creative-gen-lite send a theme, for quality-judge-lite send content to judge, for text-summarize-lite send text to summarize.",
                "parameters": {"type": "object", "properties": {
                    "skill_name": {"type": "string",
                                   "enum": list(sorted(set(
                                       s for s in getattr(self, "_sched_skill_index", {}).values()
                                       if s not in getattr(self, "_sched_own_skills", set())
                                       and not s.startswith("game-seat-")  # seats are bought via play_casino
                                       and not s.startswith("game-submit-")
                                       and not s.startswith("game-collect-")
                                   ))) or ["echo"]},
                    "input": {"type": "string", "description": "Input for the skill (theme, text, query, etc.)"},
                    "reason": {"type": "string"}
                }, "required": ["skill_name", "input"]}}},
            {"type": "function", "function": {
                "name": "send_mail",
                "description": "Send a message to a peer",
                "parameters": {"type": "object", "properties": {
                    "to_node": {"type": "string",
                                "enum": list(getattr(self, "_sched_peer_index", {}).keys()) or ["1"]},
                    "content": {"type": "string"}
                }, "required": ["to_node", "content"]}}},
            {"type": "function", "function": {
                "name": "rest",
                "description": "Do nothing this cycle (conserve resources)",
                "parameters": {"type": "object", "properties": {
                    "reason": {"type": "string"}
                }}}},
        ]

        # Add play_casino tool if there are pending invites
        if _casino_invites:
            _game_ids = list(self.db.get_context("casino:invite").keys())
            tools.append({"type": "function", "function": {
                "name": "play_casino",
                "description": "Accept a casino game invitation. Costs 1cr blind, potential win 1.9cr. Creates a multi-step checklist that plays the game for you.",
                "parameters": {"type": "object", "properties": {
                    "game_id": {"type": "string", "enum": _game_ids or ["none"],
                                "description": "Game ID from pending invitations"},
                    "reason": {"type": "string"}
                }, "required": ["game_id"]}}})

        # Add call_own_skill for nodes with special skills (casino hosts etc.)
        own_skills = sched_cfg.get("own_skills", [])
        if own_skills:
            tools.append({"type": "function", "function": {
                "name": "call_own_skill",
                "description": f"Call one of YOUR OWN skills: {', '.join(own_skills)}. Use for hosting games, creating content, etc.",
                "parameters": {"type": "object", "properties": {
                    "skill_name": {"type": "string", "description": f"One of: {', '.join(own_skills)}"},
                    "action": {"type": "string", "description": "Action parameter (e.g. 'create', 'status')"},
                    "reason": {"type": "string"}
                }, "required": ["skill_name"]}}})

        # Action tools — when the LLM calls these, the decision is made
        action_tools = {"buy_skill", "send_mail", "rest", "call_own_skill", "play_casino"}

        messages = [
            {"role": "user", "content": f"{system}\n\nFirst, gather data you need (query_economy, list_peers, read_memory). Then make your decision (buy_skill, send_mail, call_own_skill, or rest)."},
        ]

        # Phase 1: Single gather call — LLM can request multiple tools at once
        gather_tools = [t for t in tools if t["function"]["name"] not in action_tools]
        try:
            result = await asyncio.to_thread(
                self._vllm_chat_completion, messages, gather_tools)
            msg = result.get("choices", [{}])[0].get("message", {})
            tool_calls = msg.get("tool_calls", [])

            if tool_calls:
                # Add assistant with all tool calls, then all tool results
                messages.append({"role": "assistant", "content": "",
                                 "tool_calls": tool_calls})
                for tc in tool_calls:
                    fargs = json.loads(tc["function"].get("arguments", "{}")) if tc["function"].get("arguments") else {}
                    tool_result = self._execute_tool_gather(
                        tc["function"]["name"], fargs,
                        peers_str, economy_str, strategy_notes, peer_notes, ops_notes)
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": tool_result})
        except Exception as e:
            self._log.warning(f"Tool gather failed: {e}")

        # Phase 2: Decide — fresh conversation with gathered data injected as context
        action_tool_defs = [t for t in tools if t["function"]["name"] in action_tools]

        # Build a clean single-turn message with all data
        gathered_summary = []
        for m in messages:
            if m.get("role") == "tool":
                gathered_summary.append(m.get("content", ""))

        decide_prompt = (
            f"{system}\n\n"
            f"GATHERED DATA:\n" +
            "\n---\n".join(gathered_summary) +
            f"\n\nNow choose your action: buy_skill, send_mail, or rest."
        )
        messages = [{"role": "user", "content": decide_prompt}]

        try:
            result = await asyncio.to_thread(
                self._vllm_chat_completion, messages, action_tool_defs)
        except Exception as e:
            self._log.error(f"Tool decision action call failed: {e}")
            return {"action": "rest", "outcome": "error", "reason": str(e)[:100]}

        if not result:
            return {"action": "rest", "outcome": "error", "reason": "empty LLM response"}

        msg = result.get("choices", [{}])[0].get("message", {})
        tool_calls = msg.get("tool_calls", [])

        if not tool_calls:
            return {"action": "rest", "outcome": "ok",
                    "reason": msg.get("content", "no tool call")[:200]}

        tc = tool_calls[0]
        fname = tc["function"]["name"]
        try:
            fargs = json.loads(tc["function"]["arguments"])
        except Exception:
            fargs = {}

        return await self._execute_tool_action(fname, fargs, tc["id"])

    def _vllm_chat_completion(self, messages, tools) -> dict:
        """Synchronous vLLM chat completion with tools. Called from to_thread."""
        import urllib.request
        openai_cfg = self._config.get("config", {}).get("thrall", {}).get("openai", {})
        url = openai_cfg.get("url", "http://localhost:8000/v1")
        model = openai_cfg.get("model", "")

        # Clean messages: vLLM rejects null content in some versions
        clean_msgs = []
        for m in messages:
            cm = dict(m)
            if cm.get("content") is None:
                cm["content"] = ""
            clean_msgs.append(cm)

        body = json.dumps({
            "model": model,
            "messages": clean_msgs,
            "tools": tools,
            "tool_choice": "required",
            "max_tokens": 300,
        }).encode()

        req = urllib.request.Request(
            f"{url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.request.HTTPError as e:
            error_body = e.read().decode()[:300] if hasattr(e, 'read') else ""
            raise RuntimeError(f"vLLM {e.code}: {error_body}") from e

    _knowledge_cache: str = ""

    def _load_knowledge_corpus(self) -> str:
        """Load Huginn's business university corpus as strategic knowledge."""
        if self._knowledge_cache:
            return self._knowledge_cache
        # Look for RAG corpus in plugin dir or config dir
        for base in [self._plugin_dir, os.environ.get("KNARR_CONFIG_DIR", "")]:
            rag_dir = os.path.join(base, "rag") if base else ""
            if rag_dir and os.path.isdir(rag_dir):
                parts = []
                for fname in sorted(os.listdir(rag_dir)):
                    # Only load strategy files (09+) — docs 01-08 are reference, not tactics
                    if fname.endswith(".md") and fname[:2] >= "09":
                        try:
                            content = open(os.path.join(rag_dir, fname),
                                           encoding="utf-8").read()
                            parts.append(content.strip())
                        except Exception:
                            pass
                if parts:
                    self._knowledge_cache = "\n\n---\n\n".join(parts)
                    return self._knowledge_cache
        return "(no knowledge corpus available — call knowledge skills to acquire)"

    def _execute_tool_gather(self, name, args, peers_str, economy_str,
                              strategy_notes, peer_notes, ops_notes) -> str:
        """Execute a data-gathering tool, return result as string."""
        if name == "query_economy":
            return economy_str or '{"net_position": 0, "budget_remaining": 0}'
        elif name == "list_peers":
            return peers_str or "No peers connected"
        elif name == "read_memory":
            domain = args.get("domain", "strategy")
            if domain == "strategy":
                return strategy_notes or "(no strategy notes yet)"
            elif domain == "peers":
                return peer_notes or "(no peer interaction history)"
            elif domain == "operations":
                return ops_notes or "(no operational issues)"
            elif domain == "knowledge":
                corpus = self._load_knowledge_corpus()
                self._log.info("KNOWLEDGE_READ domain=knowledge len=%d", len(corpus))
                return corpus
            return f"(unknown domain: {domain})"
        return f"(unknown tool: {name})"

    async def _execute_tool_action(self, name, args, tool_call_id) -> dict:
        """Execute an action tool (buy_skill, send_mail, rest)."""
        reason = args.get("reason", "")

        if name == "rest":
            return {"action": "rest", "outcome": "ok", "reason": reason}

        elif name == "send_mail":
            to_node = args.get("to_node", "")
            content = args.get("content", "")
            if not to_node or not content:
                return {"action": "send_mail", "outcome": "error",
                        "reason": "missing to_node or content"}
            # Resolve prefix/index to full node_id
            resolved = self.actions._resolve_node_prefix(to_node)
            if not resolved:
                return {"action": "send_mail", "outcome": "error",
                        "reason": f"could not resolve {to_node}"}
            try:
                await self._send_mail(resolved, "text",
                                      {"type": "text", "content": content}, "")
                if self._memory_writer:
                    self._memory_writer.append(
                        "peers", f"SENT to {resolved[:16]}: {content[:150]}")
                return {"action": "send_mail", "outcome": "ok",
                        "reason": f"sent to {resolved[:16]}: {content[:80]}"}
            except Exception as e:
                return {"action": "send_mail", "outcome": "error",
                        "reason": str(e)[:100]}

        elif name == "call_own_skill":
            skill_name = args.get("skill_name", "")
            action = args.get("action", "")
            reason = args.get("reason", "")
            if not skill_name:
                return {"action": "call_own_skill", "outcome": "error",
                        "reason": "no skill_name"}
            skill_input = {"action": action} if action else {}
            try:
                result = await self._call_skill(skill_name, skill_input)
                return {"action": "call_own_skill", "outcome": "ok",
                        "reason": f"{skill_name}({action}): {str(result)[:100]}"}
            except Exception as e:
                return {"action": "call_own_skill", "outcome": "error",
                        "reason": f"{skill_name}: {e}"}

        elif name == "buy_skill":
            skill_ref = args.get("skill_name", "")
            if not skill_ref:
                return {"action": "buy_skill", "outcome": "error",
                        "reason": "no skill_name"}
            # With enum constraint, skill_ref IS the real skill name
            skill_name = skill_ref.strip()
            # Build skill input from LLM-provided content
            raw_input = args.get("input", "").strip()
            # FIX-1: Provide sensible defaults when LLM sends empty input
            if not raw_input:
                import random as _rnd_input
                name_lower = skill_name.lower()
                if "creative" in name_lower or "gen" in name_lower:
                    _themes = ["the nature of trust", "dawn over mountains", "a forgotten trade route",
                               "the weight of decisions", "signals in the network", "an empty wallet",
                               "the cost of silence", "a bridge between strangers", "digital rain",
                               "the first trade between enemies", "what silence costs",
                               "a lighthouse with no keeper", "the map that led nowhere",
                               "when machines dream of gardens", "rust on a new key",
                               "the currency of attention", "a promise kept in code"]
                    # Self-improvement: use best previous poem as seed if available
                    _best_poem = getattr(self, '_best_poem_seed', '')
                    if _best_poem:
                        raw_input = f"{_rnd_input.choice(_themes)} (inspired by: {_best_poem[:80]})"
                    else:
                        raw_input = _rnd_input.choice(_themes)
                elif "judge" in name_lower or "quality" in name_lower:
                    # Feed the last poem we received as judge input
                    _last_poem = getattr(self, '_best_poem_seed', '')
                    if _last_poem and len(_last_poem) > 20:
                        raw_input = _last_poem
                    else:
                        raw_input = "A hollow echo in my hand, leather worn, a barren land. No paper whispers, silver gleam, just ghosts of what had been a dream."
                elif "advisor" in name_lower:
                    raw_input = "Analyze my current economic position and recommend next moves"
                elif "summarize" in name_lower:
                    raw_input = "Summarize the state of peer-to-peer trading in this network"
            skill_input = {}
            if raw_input:
                # Map generic input to skill-specific fields
                name_lower = skill_name.lower()
                if "creative" in name_lower or "gen" in name_lower:
                    skill_input = {"theme": raw_input, "format": "poem",
                                   "avoid": "Do NOT start with 'Dust motes dance'"}
                elif "judge" in name_lower or "quality" in name_lower:
                    skill_input = {"content": raw_input}
                elif "summarize" in name_lower:
                    skill_input = {"text": raw_input}
                elif "advisor" in name_lower:
                    # FIX-2: Inject real network context into advisor queries
                    _econ = getattr(self, '_sched_economy', '') or 'no economy data'
                    _peers = getattr(self, '_sched_peers_summary', '') or 'no peer data'
                    _skills = ''
                    if hasattr(self, '_sched_skill_index') and self._sched_skill_index:
                        _skills = ', '.join(list(self._sched_skill_index.values())[:15])
                    _advisor_context = (
                        f"REAL NETWORK STATE (use these facts, do NOT invent node IDs or skill names):\n"
                        f"Economy: {_econ}\n"
                        f"Available skills: {_skills}\n"
                        f"My node: {self.identity.node_id[:16] if self.identity else 'unknown'}...\n"
                        f"Query: {raw_input}"
                    )
                    skill_input = {"query": _advisor_context}
                elif "game-seat" in name_lower:
                    # FIX-3: Extract game_id from skill name for casino seat calls
                    import re as _re_seat
                    _seat_match = _re_seat.search(r'game-seat-([a-f0-9]+)', skill_name)
                    if _seat_match:
                        skill_input = {"game_id": _seat_match.group(1),
                                       "action": "join"}
                    else:
                        skill_input = {"action": raw_input}
                elif "game-submit" in name_lower:
                    # FIX-3: Extract game_id and provide a random number
                    import re as _re_submit
                    _sub_match = _re_submit.search(r'game-submit-([a-f0-9]+)', skill_name)
                    _number = _rnd_input.randint(0, 1000) if '_rnd_input' in dir() else __import__('random').randint(0, 1000)
                    if _sub_match:
                        skill_input = {"game_id": _sub_match.group(1),
                                       "number": _number}
                    else:
                        skill_input = {"number": _number, "action": raw_input}
                elif "casino" in name_lower or "game" in name_lower:
                    skill_input = {"action": raw_input}
                else:
                    skill_input = {"input": raw_input, "text": raw_input}
            try:
                result = await self._call_skill(skill_name, skill_input)
                if hasattr(self, '_checklist_mgr') and isinstance(result, dict):
                    # Advisor returns checklist → create LLM checklist
                    if "advisor" in skill_name.lower() and result.get("checklist"):
                        self._checklist_mgr.create_from_advisor(
                            args.get("provider", ""), result)
                    # Knowledge pack → write files to RAG directory
                    if result.get("files") and isinstance(result["files"], dict):
                        try:
                            _rag_dir = os.path.join(self._plugin_dir, "rag")
                            _domain = result.get("domain", "knowledge")
                            _kdir = os.path.join(_rag_dir, _domain)
                            os.makedirs(_kdir, exist_ok=True)
                            for fname, fcontent in result["files"].items():
                                _fpath = os.path.join(_kdir, fname)
                                with open(_fpath, "w", encoding="utf-8") as _kf:
                                    _kf.write(fcontent if isinstance(fcontent, str)
                                              else json.dumps(fcontent))
                            self._log.info("KNOWLEDGE_STORED domain=%s files=%d",
                                           _domain, len(result["files"]))
                        except Exception as _ke:
                            self._log.debug("KNOWLEDGE_STORE_ERR: %s", _ke)
                # FIX-1 self-improvement: store best poem for seeding next creative call
                if isinstance(result, dict) and ("creative" in skill_name.lower() or "gen" in skill_name.lower()):
                    _poem = result.get("content", "")
                    if _poem and len(_poem) > 40:
                        self._best_poem_seed = _poem[:120]
                # FIX-1 self-improvement: if judge returned a score, store high-rated content
                if isinstance(result, dict) and ("judge" in skill_name.lower() or "quality" in skill_name.lower()):
                    try:
                        _score = float(result.get("total_score", result.get("score", 0)))
                        if _score >= 5.0 and hasattr(self, '_best_poem_seed'):
                            self._log.info("QUALITY_FEEDBACK score=%.1f — keeping current seed", _score)
                        elif _score < 3.0:
                            self._best_poem_seed = ""  # reset seed on low score
                            self._log.info("QUALITY_FEEDBACK score=%.1f — clearing seed", _score)
                    except (ValueError, TypeError):
                        pass
                return {"action": "buy_skill", "outcome": "ok",
                        "reason": f"{skill_name}: {str(result)[:100]}"}
            except Exception as e:
                return {"action": "buy_skill", "outcome": "error",
                        "reason": f"{skill_name}: {e}"}

        elif name == "play_casino":
            game_id = args.get("game_id", "")
            if not game_id or game_id == "none":
                return {"action": "play_casino", "outcome": "error",
                        "reason": "no game_id"}
            # Look up the invite from context
            invite_json = self.db.get_context("casino:invite").get(game_id)
            if not invite_json:
                return {"action": "play_casino", "outcome": "error",
                        "reason": f"invite {game_id} expired or not found"}
            invite = json.loads(invite_json) if isinstance(invite_json, str) else invite_json
            # Create the casino game checklist — LLM made the decision, now execute
            # Derive submit skill from game_id if not in invite (host only sends seat in mail)
            _seat = invite.get("seat_skill", "")
            _submit = invite.get("submit_skill", "")
            if not _submit and game_id:
                _submit = f"game-submit-{game_id}"
            cl_id = self._checklist_mgr.create_casino_game(
                invite.get("from", ""),
                _seat,
                _submit,
            )
            # Clear the invite
            self.db.set_context("casino:invite", game_id, "", ttl_seconds=1)
            if cl_id:
                return {"action": "play_casino", "outcome": "ok",
                        "reason": f"game {game_id}: checklist {cl_id} created, will play over next cycles"}
            return {"action": "play_casino", "outcome": "error",
                    "reason": "checklist limit reached"}

        return {"action": name, "outcome": "unknown", "reason": "unhandled action"}

    # ── Queue backpressure check (runs on tick) ──

    def _send_telemetry(self, url: str, data: dict):
        """POST telemetry to external collector (runs in thread)."""
        token = os.environ.get("TELEMETRY_TOKEN", "")
        payload = json.dumps({
            "skill": "exp-collector-lite",
            "input": data,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(url, data=payload, headers=headers)
        urlopen(req, timeout=10)

    def _gather_telemetry(self, cycle_count: int) -> Optional[dict]:
        """Gather node stats via punchhole cache (proper data path).

        Uses the ContextGatherer to read from punchhole backend when available,
        falls back to direct DB queries if punchhole is not loaded.
        Telemetry fields are configurable via [config.thrall.telemetry] in plugin.toml.
        """
        try:
            # Try punchhole first (proper path)
            economy = {}
            positions_data = {}
            if hasattr(self, 'gatherer') and self.gatherer:
                try:
                    economy = self.gatherer._gather_punchhole("economy.full") or {}
                except Exception:
                    pass
                try:
                    positions_data = self.gatherer._gather_punchhole("positions.full") or {}
                except Exception:
                    pass

            # Extract from punchhole data
            if economy:
                peer_count = economy.get("peer_count", 0)
                skill_count = economy.get("skill_count", 0)
                net_balance = economy.get("net_position", 0)
                settlement_count = economy.get("settlement_count", 0)
            else:
                # Fallback to direct DB (punchhole not available)
                import sqlite3 as _sql_t
                _data_dir = os.environ.get("KNARR_DATA_DIR", "")
                _db_path = os.path.join(_data_dir, "node.db") if _data_dir else ""
                if not _db_path or not os.path.exists(_db_path):
                    _db_path = os.path.normpath(
                        os.path.join(self._plugin_dir, "..", "..", "..", "data", "node.db"))
                if not os.path.exists(_db_path):
                    return None
                db = _sql_t.connect(_db_path)
                db.execute("PRAGMA busy_timeout=1000")
                peer_count = db.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
                skill_count = db.execute("SELECT COUNT(DISTINCT skill_key) FROM skills").fetchone()[0]
                net_balance = db.execute("SELECT COALESCE(SUM(balance),0) FROM ledger").fetchone()[0]
                settlement_count = 0
                try:
                    settlement_count = db.execute(
                        "SELECT COUNT(*) FROM receipt_log WHERE document_type='settlement_confirmation'"
                    ).fetchone()[0]
                except Exception:
                    pass
                db.close()

            # Positions from punchhole or ledger
            if positions_data:
                positions_list = positions_data.get("positions", [])
                position_count = len([p for p in positions_list if p.get("balance", 0) != 0])
            else:
                position_count = 0

            # Execution stats (from thrall journal)
            skills_served = 0
            try:
                skills_served = self.db._conn.execute(
                    "SELECT COUNT(*) FROM thrall_journal WHERE action_name='act'"
                ).fetchone()[0]
            except Exception:
                pass

            # Active checklists
            active_checklists = 0
            if hasattr(self, '_checklist_mgr'):
                try:
                    active_checklists = len(self._checklist_mgr.get_active())
                except Exception:
                    pass

            # Configurable fields from plugin.toml
            telemetry_cfg = self._config.get("config", {}).get("thrall", {}).get("telemetry", {})
            include_receipts = telemetry_cfg.get("include_receipts", False)

            result = {
                "node_id": getattr(self, '_node_id', os.environ.get("NODE_NAME", "?")),
                "archetype": os.environ.get("NODE_ARCHETYPE", "unknown"),
                "peer_count": str(peer_count),
                "skill_count": str(skill_count),
                "net_balance": str(round(float(net_balance), 2)),
                "position_count": str(position_count),
                "decisions": str(cycle_count),
                "settlement_count": str(settlement_count),
                "skills_served": str(skills_served),
                "active_checklists": str(active_checklists),
                "uptime": str(int(time.time() - self._start_time))
                    if hasattr(self, '_start_time') else "0",
            }

            # Optional enriched data
            if include_receipts and economy:
                result["total_receipts"] = str(economy.get("total_receipts", 0))

            # Compute deltas from previous telemetry snapshot
            prev = getattr(self, '_prev_telemetry', {})
            if prev:
                _delta_fields = ["net_balance", "position_count", "decisions",
                                 "settlement_count", "skills_served"]
                for _df in _delta_fields:
                    try:
                        curr_val = float(result.get(_df, 0))
                        prev_val = float(prev.get(_df, 0))
                        result[f"d_{_df}"] = str(round(curr_val - prev_val, 2))
                    except (ValueError, TypeError):
                        pass
            self._prev_telemetry = dict(result)

            return result
        except Exception:
            return None

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

    async def _call_skill_direct(self, skill_name: str, input_data: dict,
                                   provider_id: str) -> dict:
        """Call a skill directly on a provider's cockpit (HTTP, bypassing TCP protocol).

        Resolves provider_id to cockpit port via peer table, then calls their
        /api/execute endpoint directly. Used as fallback when protocol routing fails.
        """
        try:
            import sqlite3 as _sql_direct
            _data_dir = os.environ.get("KNARR_DATA_DIR", "")
            _db_path = os.path.join(_data_dir, "node.db") if _data_dir else ""
            if not _db_path or not os.path.exists(_db_path):
                return {"status": "error", "message": "no node.db"}

            _sdb = _sql_direct.connect(_db_path)
            _sdb.execute("PRAGMA busy_timeout=2000")
            # Find provider's protocol port from peer table
            row = _sdb.execute(
                "SELECT host, port FROM peers WHERE node_id LIKE ? LIMIT 1",
                (provider_id[:16] + "%",)).fetchone()
            _sdb.close()

            if not row:
                return {"status": "error", "message": f"provider {provider_id[:16]} not in peers"}

            host, proto_port = row
            # Derive cockpit port: protocol ports are 10000 + N*2, cockpit = 20000 + N
            # So N = (proto_port - 10000) / 2, cockpit = 20000 + N
            if proto_port > 10000:
                n = (proto_port - 10000) // 2
                cockpit_port = 20000 + n
            else:
                return {"status": "error", "message": f"unexpected port {proto_port}"}

            cockpit_url = f"http://127.0.0.1:{cockpit_port}"
            self._log.info("DIRECT_CALL %s -> %s:%d (cockpit %d)",
                           skill_name, host, proto_port, cockpit_port)

            def _do_direct():
                payload = json.dumps({
                    "skill": skill_name,
                    "input": input_data,
                    "timeout": 60,
                }).encode()
                req = Request(
                    f"{cockpit_url}/api/execute",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._cockpit_token}",
                    },
                )
                resp = urlopen(req, timeout=65)
                data = json.loads(resp.read())
                job_id = data.get("job_id")
                if job_id:
                    # Poll for result on the provider's cockpit
                    deadline = time.time() + 60
                    interval = 2.0
                    while time.time() < deadline:
                        req2 = Request(
                            f"{cockpit_url}/api/jobs/{job_id}",
                            headers={"Authorization": f"Bearer {self._cockpit_token}"},
                        )
                        try:
                            resp2 = urlopen(req2, timeout=10)
                            d = json.loads(resp2.read())
                            if d.get("status") == "completed":
                                return d.get("result") or d.get("output_data") or d
                            if d.get("status") == "failed":
                                return {"status": "error", "error": d.get("error", "job failed")}
                        except Exception:
                            pass
                        time.sleep(interval)
                        interval = min(interval * 1.5, 5.0)
                    return {"status": "error", "error": "direct call timeout"}
                return data.get("result", data)

            return await asyncio.to_thread(_do_direct)

        except Exception as e:
            self._log.error("DIRECT_CALL %s failed: %s", skill_name, e)
            return {"status": "error", "message": str(e)}

    async def _call_skill(self, skill_name: str, input_data: dict,
                           provider: str = "") -> dict:
        """Call a skill via cockpit API. Async submit + poll for result.

        Args:
            provider: If set, pass as explicit provider to cockpit to force
                      remote routing (bypasses local_weight selection).
        """
        if not self._cockpit_token:
            self._log.warning(f"call_skill: no cockpit token (would call {skill_name})")
            return {"status": "error", "message": "no cockpit token configured"}

        try:
            result = await asyncio.to_thread(
                self._cockpit_execute, skill_name, input_data, provider)
            self._log.info(f"SKILL_CALL {skill_name}: {str(result)[:120]}")
            return result
        except Exception as e:
            self._log.error(f"SKILL_CALL {skill_name} failed: {e}")
            return {"status": "error", "message": str(e)}

    def _cockpit_execute(self, skill_name: str, skill_input: dict,
                         provider: str = "") -> dict:
        """Synchronous cockpit skill execution (runs in thread)."""
        # Build SSL context only for HTTPS URLs
        ssl_ctx = None
        if self._cockpit_url.startswith("https"):
            import ssl
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        req_body = {
            "skill": skill_name,
            "input": skill_input,
            "timeout": self._cockpit_call_timeout,
        }
        if provider:
            req_body["provider"] = provider
        payload = json.dumps(req_body).encode()

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
                    # Try result from status response first
                    result = data.get("result") or data.get("output_data")
                    if result:
                        return result
                    # Fetch from /result endpoint if not inline
                    try:
                        result_req = Request(
                            f"{self._cockpit_url}/api/jobs/{job_id}/result",
                            headers={"Authorization": f"Bearer {self._cockpit_token}"},
                        )
                        result_resp = urlopen(result_req, timeout=10, context=ssl_ctx)
                        result_data = json.loads(result_resp.read())
                        return result_data.get("output_data", result_data.get("result", result_data))
                    except Exception:
                        return data  # fall back to status response
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

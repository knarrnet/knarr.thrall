"""Thrall Switchboard — Pipeline execution engine.

Runs: TRIGGER → FILTER → GATHER → EVALUATE → ACTION
Each stage is a pure function that takes an envelope and recipe config.
The engine orchestrates the flow and writes to the journal.
"""

import asyncio
import fnmatch
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from db import ThrallDB

logger = logging.getLogger("thrall.engine")


@dataclass
class Envelope:
    """Immutable context that flows through every pipeline stage."""
    trigger_type: str
    timestamp: float
    fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        # Inject _ts into fields so recipes can use {{envelope._ts}} for
        # dedup-busting: cockpit async_jobs hashes input including _ts,
        # preventing permanent 409 blocks after a failed invocation.
        if "_ts" not in self.fields:
            self.fields["_ts"] = str(int(self.timestamp))

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key, default)


@dataclass
class FilterResult:
    decision: str = "pass"  # "pass", "skip", "cache_hit", "bypass"
    reason: str = ""
    tier: str = "unknown"
    context: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"decision": self.decision, "reason": self.reason,
                "tier": self.tier, "context": self.context}


@dataclass
class EvalResult:
    eval_type: str = "skip"  # "llm", "hotwire", "cache", "skip"
    action: str = "log"
    reason: str = ""
    raw: str = ""
    wall_ms: int = 0

    def to_dict(self) -> dict:
        return {"eval_type": self.eval_type, "action": self.action,
                "reason": self.reason, "wall_ms": self.wall_ms}


@dataclass
class ActionResult:
    name: str = "log"
    trace: str = ""
    context_written: Dict[str, str] = field(default_factory=dict)


@dataclass
class PipelineResult:
    pipeline: str
    envelope: Envelope
    filter_result: FilterResult
    eval_result: EvalResult
    action_result: ActionResult
    wall_ms: int = 0
    mode: str = "automated"


class PipelineEngine:
    """Executes recipes against envelopes. Stateless except for DB writes."""

    def __init__(self, db: ThrallDB, evaluator: Any = None,
                 action_executor: Any = None, gatherer: Any = None,
                 ctx: Any = None, memory: Any = None):
        self.db = db
        self._evaluator = evaluator
        self._action_executor = action_executor
        self._gatherer = gatherer  # ContextGatherer for pre-prompt data
        self._ctx = ctx            # PluginContext — for bus emit_event (T6)
        self._memory = memory      # ThrallMemory — for structured error records (T6)
        self._recipes: Dict[str, dict] = {}
        self._trust_tiers: Dict[str, str] = {}  # node_prefix → tier

    def load_recipes(self):
        """Load all recipes from DB into memory."""
        self._recipes.clear()
        for r in self.db.get_all_recipes():
            self._recipes[r["name"]] = r["config"]
        logger.info(f"Loaded {len(self._recipes)} recipes")

    def set_trust_tiers(self, tiers: Dict[str, List[str]]):
        """Set trust tier mappings: {"team": ["de2a...", ...], "known": [...]}"""
        self._trust_tiers.clear()
        for tier, prefixes in tiers.items():
            for prefix in prefixes:
                self._trust_tiers[prefix[:16]] = tier

    def resolve_tier(self, node_id: str) -> str:
        prefix = node_id[:16]
        for known_prefix, tier in self._trust_tiers.items():
            if prefix.startswith(known_prefix[:len(prefix)]) or known_prefix.startswith(prefix):
                return tier
        return "unknown"

    def match_recipes(self, trigger_type: str, envelope: Envelope) -> List[str]:
        """Find all recipes whose trigger matches this event."""
        matched = []
        for name, config in self._recipes.items():
            if config.get("mode") == "disabled":
                continue
            trigger = config.get("trigger", {})
            if trigger.get("type") != trigger_type:
                continue

            # on_event: match event_pattern (fnmatch glob) against event_name
            if trigger_type == "on_event":
                event_pattern = trigger.get("event_pattern", "")
                event_name = envelope.get("event_name", "")
                if event_pattern and not fnmatch.fnmatch(event_name, event_pattern):
                    continue

            # Optional msg_type filter for on_mail triggers
            msg_types = trigger.get("msg_types", [])
            if msg_types and envelope.get("msg_type") not in msg_types:
                continue

            # Optional match pattern on any field
            match_field = trigger.get("match_field")
            match_pattern = trigger.get("match_pattern")
            if match_field and match_pattern:
                value = envelope.get(match_field, "")
                if not re.search(match_pattern, value, re.IGNORECASE):
                    continue

            matched.append(name)
        return matched

    async def run(self, recipe_name: str, envelope: Envelope,
                  mode_override: str = None) -> PipelineResult:
        """Execute a single recipe against an envelope."""
        start = time.time()
        recipe = self._recipes.get(recipe_name)
        if not recipe:
            logger.warning(f"Recipe not found: {recipe_name}")
            return PipelineResult(
                pipeline=recipe_name, envelope=envelope,
                filter_result=FilterResult("skip", "recipe not found"),
                eval_result=EvalResult(), action_result=ActionResult(),
            )

        mode = mode_override or recipe.get("mode", "automated")

        # ── FILTER ──
        filter_result = self._run_filter(recipe, envelope, recipe_name)

        if filter_result.decision == "skip":
            result = PipelineResult(
                pipeline=recipe_name, envelope=envelope,
                filter_result=filter_result,
                eval_result=EvalResult("skip", "log", filter_result.reason),
                action_result=ActionResult("skip", f"filtered: {filter_result.reason}"),
                wall_ms=int((time.time() - start) * 1000), mode=mode,
            )
            self._journal(result)
            return result

        # ── GATHER ──
        if self._gatherer and recipe.get("gather"):
            try:
                gather_cfg = recipe["gather"]
                if isinstance(gather_cfg, list) and gather_cfg and isinstance(gather_cfg[0], str):
                    # v3.7 catalog fields: gather = ["field1", "field2"]
                    gathered = await asyncio.to_thread(self._gatherer.gather_fields, gather_cfg)
                else:
                    # Legacy [[gather]] blocks
                    gathered = await self._gatherer.gather(recipe, envelope)
                for k, v in gathered.items():
                    if isinstance(v, str):
                        envelope.fields[f"gather.{k}"] = v
                    else:
                        envelope.fields[f"gather.{k}"] = json.dumps(v)
            except Exception as e:
                logger.warning(f"[{recipe_name}] GATHER failed: {e}")
                # T6: Emit bus event on gather-stage failure
                try:
                    if self._ctx and getattr(self._ctx, "emit_event", None):
                        self._ctx.emit_event("thrall.pipeline.failed", {
                            "recipe": recipe_name,
                            "stage": "gather",
                            "error": str(e),
                            "peer": envelope.fields.get("from_node", ""),
                        })
                except Exception:
                    pass  # bus emit must never crash the engine
                # T6: Record in structured memory for T2 circuit breaker
                # T2 queries: SELECT * FROM thrall_memory WHERE skill='thrall-pipeline'
                #             AND outcome='error' AND node_id=<peer_id>
                try:
                    if self._memory:
                        self._memory.record(
                            skill="thrall-pipeline",
                            outcome="error",
                            node_id=envelope.fields.get("from_node", ""),
                            metadata={"recipe": recipe_name, "stage": "gather",
                                      "error": str(e)},
                        )
                except Exception:
                    pass

        # ── EVALUATE ──
        from_node = envelope.get("from_node", "")
        tag = f"[{recipe_name}:{from_node[:8]}]"

        if filter_result.decision == "bypass":
            bypass_action = recipe.get("filter", {}).get("bypass_action", "wake")
            eval_result = EvalResult("bypass", bypass_action,
                                     f"trust tier {filter_result.tier}", "", 0)
            logger.debug(f"{tag} EVAL bypass → action={bypass_action}")
        elif filter_result.decision == "rate_limited":
            rate_action = recipe.get("filter", {}).get("rate_limit_action", "compile")
            eval_result = EvalResult("rate_limit", rate_action,
                                     filter_result.reason, "", 0)
            logger.debug(f"{tag} EVAL rate_limited → action={rate_action}")
        elif filter_result.decision == "cache_hit":
            cached_action = filter_result.context.get("cached_action", "log")
            eval_result = EvalResult("cache", cached_action, "cached result", "", 0)
            logger.debug(f"{tag} EVAL cache_hit → action={cached_action}")
        else:
            logger.debug(f"{tag} EVAL running LLM classify")
            eval_result = await self._run_evaluate(recipe, envelope, filter_result)
            logger.debug(f"{tag} EVAL {eval_result.eval_type} → action={eval_result.action} "
                         f"reason={eval_result.reason} wall={eval_result.wall_ms}ms")

        # ── ACTION ──
        if mode == "manual":
            action_result = ActionResult("manual_skip",
                                         f"would execute: {eval_result.action}")
            logger.debug(f"{tag} ACTION manual_skip (would={eval_result.action})")
        else:
            action_result = await self._run_action(
                recipe, envelope, eval_result, filter_result)
            logger.debug(f"{tag} ACTION {action_result.name}: {action_result.trace[:120]}")

        # ── POST-ACTION: update cooldown ──
        cooldown_key = recipe.get("filter", {}).get("cooldown_key")
        if cooldown_key:
            cooldown_key = re.sub(
                r"\{\{(.+?)\}\}",
                lambda m: envelope.get(m.group(1).strip(), m.group(0)),
                cooldown_key)
        if cooldown_key and action_result.name not in ("skip", "manual_skip"):
            cooldown_seconds = recipe.get("filter", {}).get("cooldown_seconds", 0)
            self.db.set_context(
                f"cooldown:{cooldown_key}", "last_fired",
                str(time.time()), ttl_seconds=cooldown_seconds or None)

        wall_ms = int((time.time() - start) * 1000)
        result = PipelineResult(
            pipeline=recipe_name, envelope=envelope,
            filter_result=filter_result, eval_result=eval_result,
            action_result=action_result, wall_ms=wall_ms, mode=mode,
        )
        self._journal(result)
        return result

    # ── Filter stage ──

    def _run_filter(self, recipe: dict, envelope: Envelope,
                    recipe_name: str = "") -> FilterResult:
        filter_cfg = recipe.get("filter", {})
        result = FilterResult()

        from_node = envelope.get("from_node", "")
        tag = f"[{recipe_name}:{from_node[:8]}]"

        # Trust tier resolution
        result.tier = self.resolve_tier(from_node)
        logger.debug(f"{tag} FILTER trust_tier={result.tier}")

        # Blocked tier — drop before any evaluation
        if result.tier == "blocked":
            result.decision = "skip"
            result.reason = "blocked by trust tier"
            logger.debug(f"{tag} FILTER → skip (blocked)")
            return result

        # Trust bypass — team nodes skip LLM
        if filter_cfg.get("trust_bypass") and result.tier == "team":
            result.decision = "bypass"
            result.reason = "team node"
            logger.debug(f"{tag} FILTER → bypass (team)")
            return result

        # Cooldown check — persistence flag
        cooldown_key = filter_cfg.get("cooldown_key")
        cooldown_seconds = filter_cfg.get("cooldown_seconds", 0)
        # Substitute envelope fields in cooldown key (e.g. "greet:{{from_node}}")
        if cooldown_key:
            cooldown_key = re.sub(
                r"\{\{(.+?)\}\}",
                lambda m: envelope.get(m.group(1).strip(), m.group(0)),
                cooldown_key)
        if cooldown_key and cooldown_seconds > 0:
            ctx = self.db.get_context(f"cooldown:{cooldown_key}")
            if ctx.get("last_fired"):
                elapsed = time.time() - float(ctx["last_fired"])
                if elapsed < cooldown_seconds:
                    result.decision = "skip"
                    result.reason = f"cooldown ({int(cooldown_seconds - elapsed)}s remaining)"
                    logger.debug(f"{tag} FILTER → skip (cooldown {int(cooldown_seconds - elapsed)}s)")
                    return result
                logger.debug(f"{tag} FILTER cooldown expired ({int(elapsed)}s ago)")
            else:
                logger.debug(f"{tag} FILTER cooldown clear (never fired)")

        # Per-sender rate limit
        rate_window = filter_cfg.get("rate_limit_window", 0)
        rate_max = filter_cfg.get("rate_limit_max", 0)
        if rate_window > 0 and rate_max > 0 and from_node:
            count = self.db.count_recent_from_sender(
                from_node[:16], recipe_name, rate_window)
            logger.debug(f"{tag} FILTER rate_limit {count}/{rate_max} in {rate_window}s")
            if count >= rate_max:
                result.decision = "rate_limited"
                result.reason = f"rate limited ({count}/{rate_max} in {rate_window}s)"
                return result

        # LLM result cache — reuse recent decision for same sender
        cache_ttl = filter_cfg.get("cache_ttl", 0)
        if cache_ttl > 0 and from_node:
            cached = self.db.get_cached_eval(
                from_node[:16], recipe_name, cache_ttl)
            if cached:
                age = int(time.time() - cached['timestamp'])
                result.decision = "cache_hit"
                result.reason = f"cached from {age}s ago"
                result.context["cached_action"] = cached["action"]
                logger.debug(f"{tag} FILTER → cache_hit (action={cached['action']}, age={age}s)")
                return result
            logger.debug(f"{tag} FILTER cache miss (ttl={cache_ttl}s)")

        # Context stitch — inject session state into filter result
        session_id = envelope.get("session_id")
        if session_id:
            result.context = self.db.get_context(session_id)

        logger.debug(f"{tag} FILTER → pass (proceeding to evaluate)")
        return result

    # ── Evaluate stage ──

    async def _run_evaluate(self, recipe: dict, envelope: Envelope,
                            filter_result: FilterResult) -> EvalResult:
        eval_cfg = recipe.get("evaluate", {})
        eval_type = eval_cfg.get("type", "hotwire")

        if eval_type == "hotwire":
            return self._run_hotwire(eval_cfg, envelope, filter_result)
        elif eval_type == "llm":
            return await self._run_llm(eval_cfg, envelope, filter_result)
        else:
            return EvalResult("skip", "log", f"unknown eval type: {eval_type}")

    def _run_hotwire(self, eval_cfg: dict, envelope: Envelope,
                     filter_result: FilterResult) -> EvalResult:
        """Static extraction — no LLM, just pattern matching."""
        rules = eval_cfg.get("rules", [])
        for rule in rules:
            field_name = rule.get("field", "")
            pattern = rule.get("pattern", "")
            action = rule.get("action", "log")
            value = envelope.get(field_name, "")
            if pattern and re.search(pattern, value, re.IGNORECASE):
                return EvalResult("hotwire", action,
                                  f"matched {field_name}=~/{pattern}/", "", 0)

        # Default action if no rules match
        default = eval_cfg.get("default_action", "log")
        return EvalResult("hotwire", default, "no rules matched", "", 0)

    async def _run_llm(self, eval_cfg: dict, envelope: Envelope,
                       filter_result: FilterResult) -> EvalResult:
        """LLM classification — call the evaluator."""
        if not self._evaluator:
            fallback = eval_cfg.get("fallback_action", "log")
            return EvalResult("skip", fallback, "no evaluator configured")

        prompt_name = eval_cfg.get("prompt", "default")
        model_name = eval_cfg.get("model", "gemma3-1b")
        fallback = eval_cfg.get("fallback_action", "compile")

        start = time.time()
        try:
            result = await self._evaluator.evaluate(
                prompt_name=prompt_name,
                model_name=model_name,
                envelope=envelope,
                filter_result=filter_result,
                fallback_action=fallback,
            )
            wall_ms = int((time.time() - start) * 1000)
            return EvalResult(
                eval_type="llm",
                action=result.get("action", "log"),
                reason=result.get("reason", ""),
                raw=json.dumps(result),
                wall_ms=wall_ms,
            )
        except Exception as e:
            wall_ms = int((time.time() - start) * 1000)
            fallback = eval_cfg.get("fallback_action", "log")
            logger.error(f"LLM eval failed: {e}")
            # T6: Emit bus event on evaluate-stage failure
            try:
                if self._ctx and getattr(self._ctx, "emit_event", None):
                    self._ctx.emit_event("thrall.pipeline.failed", {
                        "recipe": prompt_name,
                        "stage": "evaluate",
                        "error": str(e),
                        "peer": envelope.fields.get("from_node", ""),
                    })
            except Exception:
                pass  # bus emit must never crash the engine
            # T6: Record in structured memory for T2 circuit breaker
            # T2 queries: SELECT * FROM thrall_memory WHERE skill='thrall-pipeline'
            #             AND outcome='error' AND node_id=<peer_id>
            try:
                if self._memory:
                    self._memory.record(
                        skill="thrall-pipeline",
                        outcome="error",
                        node_id=envelope.fields.get("from_node", ""),
                        metadata={"recipe": prompt_name, "stage": "evaluate",
                                  "error": str(e)},
                    )
            except Exception:
                pass
            return EvalResult("llm_error", fallback, str(e), "", wall_ms)

    # ── Action stage ──

    async def _run_action(self, recipe: dict, envelope: Envelope,
                          eval_result: EvalResult,
                          filter_result: FilterResult) -> ActionResult:
        """Dispatch to action executor."""
        if not self._action_executor:
            return ActionResult(eval_result.action, "no executor configured")

        try:
            return await self._action_executor.execute(
                recipe=recipe,
                envelope=envelope,
                eval_result=eval_result,
                filter_result=filter_result,
            )
        except Exception as e:
            logger.error(f"Action failed: {e}")
            # T6: Emit bus event on action-stage failure
            try:
                if self._ctx and getattr(self._ctx, "emit_event", None):
                    self._ctx.emit_event("thrall.pipeline.failed", {
                        "recipe": recipe.get("name", "unknown"),
                        "stage": "action",
                        "error": str(e),
                        "peer": envelope.fields.get("from_node", ""),
                    })
            except Exception:
                pass  # bus emit must never crash the engine
            # T6: Record in structured memory for T2 circuit breaker
            # T2 queries: SELECT * FROM thrall_memory WHERE skill='thrall-pipeline'
            #             AND outcome='error' AND node_id=<peer_id>
            try:
                if self._memory:
                    self._memory.record(
                        skill="thrall-pipeline",
                        outcome="error",
                        node_id=envelope.fields.get("from_node", ""),
                        metadata={"recipe": recipe.get("name", "unknown"),
                                  "stage": "action", "error": str(e)},
                    )
            except Exception:
                pass
            return ActionResult(eval_result.action, f"error: {e}")

    # ── Journal ──

    def _journal(self, result: PipelineResult):
        """Write pipeline execution to journal."""
        try:
            self.db.write_journal(
                pipeline=result.pipeline,
                envelope=result.envelope.fields,
                filter_result=result.filter_result.to_dict(),
                eval_type=result.eval_result.eval_type,
                eval_result=result.eval_result.raw or json.dumps(result.eval_result.to_dict()),
                action_name=result.action_result.name,
                action_trace=result.action_result.trace,
                context_written=result.action_result.context_written,
                wall_ms=result.wall_ms,
                mode=result.mode,
                session_id=result.envelope.get("session_id"),
            )
        except Exception as e:
            logger.error(f"Journal write failed: {e}")

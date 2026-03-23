"""Thrall Switchboard — Evaluate stage.

Orchestrates LLM classification via a swappable backend (local, ollama, openai).
Prompts loaded from recipes/prompts/ directory.

Uses an asyncio.Semaphore(1) as an inference slot — only one inference at
a time, regardless of backend. Overflow requests that can't get the slot
within queue_timeout get the fallback action immediately.

Cost tracking: for paid backends (openai), tracks daily spend and enforces
a configurable budget. Budget exhaustion triggers hotwire fallback — the
agent sees this in health/journal, pipeline never blocks.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Optional

from backends import ThrallBackend

logger = logging.getLogger("thrall.evaluate")


class CostTracker:
    """Daily cost tracker for paid backends. Resets at midnight UTC."""

    def __init__(self, daily_budget: float = 0.0):
        self.daily_budget = daily_budget  # 0 = unlimited
        self._spent_today = 0.0
        self._reset_day = self._utc_day()

    @staticmethod
    def _utc_day() -> int:
        return int(time.time() // 86400)

    def _maybe_reset(self):
        today = self._utc_day()
        if today != self._reset_day:
            self._spent_today = 0.0
            self._reset_day = today

    def add(self, prompt_tokens: int, completion_tokens: int,
            cost_per_1m_input: float = 0.50, cost_per_1m_output: float = 3.00):
        """Record token usage and add to daily spend."""
        self._maybe_reset()
        cost = (prompt_tokens * cost_per_1m_input / 1_000_000 +
                completion_tokens * cost_per_1m_output / 1_000_000)
        self._spent_today += cost
        return cost

    def is_over_budget(self) -> bool:
        self._maybe_reset()
        if self.daily_budget <= 0:
            return False
        return self._spent_today >= self.daily_budget

    @property
    def spent_today(self) -> float:
        self._maybe_reset()
        return self._spent_today

    def status(self) -> dict:
        self._maybe_reset()
        return {
            "spent_today": f"${self._spent_today:.4f}",
            "daily_budget": f"${self.daily_budget:.2f}" if self.daily_budget > 0 else "unlimited",
            "over_budget": self.is_over_budget(),
        }


class Evaluator:
    """LLM evaluation orchestrator. Delegates inference to a ThrallBackend.

    Concurrency model:
    - asyncio.Semaphore(1) gates access (one inference at a time)
    - Backend handles its own blocking→async wrapping
    - queue_timeout: how long a request waits for the slot (default 5s)

    Cascade mode (optional):
    - L1 backend runs a fast binary filter (drop/escalate)
    - If L1 drops, short-circuit — no L2 call
    - If L1 escalates, L2 runs the full classification with L1 context
    - L1 failures are non-fatal: fall through to L2
    """

    def __init__(self, backend: ThrallBackend, queue_timeout: float = 5.0,
                 cost_budget_daily: float = 0.0,
                 l1_backend: Optional[ThrallBackend] = None,
                 l1_prompt: str = "triage-l1"):
        self._backend = backend
        self._queue_timeout = queue_timeout
        self._slot = asyncio.Semaphore(1)   # inference concurrency gate
        self._prompts: Dict[str, str] = {}
        self._queue_depth = 0  # tracks waiting coroutines (for logging)
        self._cost = CostTracker(daily_budget=cost_budget_daily)
        # Cascade L1 prefilter
        self._l1_backend = l1_backend
        self._l1_prompt_name = l1_prompt
        self._l1_drops = 0     # counter: messages L1 filtered out
        self._l1_passes = 0    # counter: messages L1 escalated to L2
        self._l1_errors = 0    # counter: L1 failures (fell through to L2)

    def set_backend(self, backend: ThrallBackend):
        """Hot-swap primary (L2) backend (sentinel reload)."""
        old_name = self._backend.name
        self._backend = backend
        logger.info(f"Backend swapped: {old_name} -> {backend.name}")

    def set_l1_backend(self, backend: Optional[ThrallBackend]):
        """Hot-swap or disable cascade L1 backend."""
        old = self._l1_backend
        self._l1_backend = backend
        if backend:
            logger.info(f"L1 cascade: {old.name if old else 'none'} -> {backend.name}/{backend.model_name}")
        else:
            logger.info(f"L1 cascade disabled (was: {old.name if old else 'none'})")

    def load_prompt(self, name: str, content: str):
        self._prompts[name] = content
        logger.info(f"Loaded prompt: {name} ({len(content)} chars)")

    async def evaluate(self, prompt_name: str, model_name: str,
                       envelope: Any, filter_result: Any,
                       fallback_action: str = "compile") -> dict:
        """Run LLM classification. Returns {"action": "...", "reason": "..."}.

        If cascade L1 is configured:
        1. L1 runs first (fast, cheap) — binary drop/escalate
        2. If L1 drops, return immediately — no L2 call
        3. If L1 escalates, L2 runs with L1 context injected

        If the inference slot is busy for longer than queue_timeout,
        returns the fallback action immediately (no LLM call).
        """
        # ── CASCADE L1 PREFILTER ──
        # Runs before the main slot. L1 drops save L2 entirely.
        l1_context = None
        if self._l1_backend and self._l1_backend.is_available():
            l1_context = await self._run_l1(envelope, filter_result)
            if l1_context and l1_context.get("action") == "drop":
                # L1 says drop — done, no L2 needed
                self._l1_drops += 1
                logger.info(f"CASCADE L1 drop: {l1_context.get('reason', '?')} "
                            f"(drops={self._l1_drops}, passes={self._l1_passes})")
                return l1_context
            if l1_context:
                self._l1_passes += 1
                logger.debug(f"CASCADE L1 escalate: {l1_context.get('reason', '?')} → L2")

        # ── L2 (PRIMARY) EVALUATION ──
        # Cost budget check (paid backends only)
        if self._cost.is_over_budget():
            budget = self._cost.status()
            logger.warning(f"COST_BUDGET_EXHAUSTED: {budget['spent_today']}/{budget['daily_budget']}")
            return {"action": fallback_action,
                    "reason": f"daily cost budget exhausted ({budget['spent_today']}/{budget['daily_budget']})"}

        # Backend availability check
        if not self._backend.is_available():
            logger.warning(f"BACKEND_UNAVAILABLE: {self._backend.name}")
            return {"action": fallback_action,
                    "reason": f"backend '{self._backend.name}' not available"}

        prompt_template = self._prompts.get(prompt_name)
        if not prompt_template:
            return {"action": "log", "reason": f"prompt '{prompt_name}' not found"}

        # Render prompt with envelope fields and filter context
        rendered = self._render_prompt(prompt_template, envelope, filter_result,
                                       l1_context=l1_context)
        logger.debug(f"LLM prompt={prompt_name} rendered={len(rendered)} chars "
                     f"tier={getattr(filter_result, 'tier', '?')} "
                     f"backend={self._backend.name}"
                     f"{' (L1→L2)' if l1_context else ''}")

        # Try to acquire the inference slot
        self._queue_depth += 1
        logger.debug(f"LLM queue_depth={self._queue_depth} waiting for slot")
        try:
            await asyncio.wait_for(
                self._slot.acquire(), timeout=self._queue_timeout)
        except asyncio.TimeoutError:
            self._queue_depth -= 1
            logger.warning(
                f"LLM_QUEUE_FULL: slot busy >{self._queue_timeout}s, "
                f"depth={self._queue_depth}, using fallback={fallback_action}")
            return {"action": fallback_action,
                    "reason": f"LLM busy (queue wait >{self._queue_timeout}s)"}

        try:
            result_text = await self._backend.infer(
                rendered, "Classify and respond with JSON only.")
            logger.debug(f"LLM raw_response [{self._backend.name}]: {result_text[:200]}")

            # Track cost for openai backend
            if self._backend.name == "openai":
                backend = self._backend
                cost = self._cost.add(
                    getattr(backend, "last_prompt_tokens", 0),
                    getattr(backend, "last_completion_tokens", 0),
                )
                if cost > 0:
                    logger.debug(f"Cost: ${cost:.6f} (today: {self._cost.status()['spent_today']})")

        except Exception as e:
            logger.error(f"Backend inference failed [{self._backend.name}]: {e}")
            result_text = json.dumps({"action": fallback_action,
                                      "reason": f"inference error: {e}"})
        finally:
            self._slot.release()
            self._queue_depth -= 1

        # Parse JSON result
        parsed = self._parse_result(result_text)
        # Tag cascade provenance
        if l1_context:
            parsed["cascade"] = "l1>l2"
            parsed["l1_reason"] = l1_context.get("reason", "")
        logger.debug(f"LLM parsed: action={parsed.get('action')} reason={parsed.get('reason', '')[:80]}")
        return parsed

    async def _run_l1(self, envelope: Any, filter_result: Any) -> Optional[dict]:
        """Run cascade L1 prefilter. Returns parsed result or None on failure.

        L1 is fire-and-forget: errors are logged and fall through to L2.
        No semaphore — L1 uses a different backend (typically CPU) and should
        be fast enough (~2s) to not need queuing.
        """
        l1_prompt_template = self._prompts.get(self._l1_prompt_name)
        if not l1_prompt_template:
            logger.debug(f"L1 prompt '{self._l1_prompt_name}' not loaded, skipping cascade")
            return None

        rendered = self._render_prompt(l1_prompt_template, envelope, filter_result)
        start = time.time()
        try:
            result_text = await self._l1_backend.infer(
                rendered, "Classify and respond with JSON only.")
            wall_ms = int((time.time() - start) * 1000)
            logger.debug(f"L1 raw [{self._l1_backend.name}] ({wall_ms}ms): {result_text[:200]}")

            parsed = self._parse_result(result_text)
            parsed["_l1_wall_ms"] = wall_ms
            parsed["_l1_backend"] = self._l1_backend.name

            # L1 only returns drop or escalate — normalize anything else to escalate
            action = parsed.get("action", "escalate")
            if action != "drop":
                parsed["action"] = "escalate"

            return parsed
        except Exception as e:
            wall_ms = int((time.time() - start) * 1000)
            self._l1_errors += 1
            logger.warning(f"L1 inference failed ({wall_ms}ms): {e} — falling through to L2")
            return None

    def get_status(self) -> dict:
        """Backend status for health/stats reporting."""
        status = {
            "type": self._backend.name,
            "available": self._backend.is_available(),
            "model": self._backend.model_name,
            "queue_depth": self._queue_depth,
        }
        if self._backend.name == "openai":
            status.update(self._cost.status())
        # Cascade L1 status
        if self._l1_backend:
            total = self._l1_drops + self._l1_passes + self._l1_errors
            status["cascade"] = {
                "l1_backend": self._l1_backend.name,
                "l1_model": self._l1_backend.model_name,
                "l1_available": self._l1_backend.is_available(),
                "l1_drops": self._l1_drops,
                "l1_passes": self._l1_passes,
                "l1_errors": self._l1_errors,
                "l1_drop_rate": f"{self._l1_drops / total * 100:.0f}%" if total > 0 else "n/a",
            }
        return status

    def _render_prompt(self, template: str, envelope: Any,
                       filter_result: Any,
                       l1_context: Optional[dict] = None) -> str:
        """Replace {{envelope.*}}, {{filter.*}}, {{context.*}}, {{cascade.*}} placeholders."""
        def replacer(match):
            path = match.group(1).strip()
            if path.startswith("envelope."):
                return envelope.get(path[9:], "")
            elif path.startswith("filter."):
                key = path[7:]
                return getattr(filter_result, key, "")
            elif path.startswith("context."):
                key = path[8:]
                return filter_result.context.get(key, "")
            elif path.startswith("cascade."):
                key = path[8:]
                if l1_context:
                    return str(l1_context.get(key, ""))
                return ""
            return match.group(0)

        return re.sub(r"\{\{(.+?)\}\}", replacer, template)

    def _parse_result(self, text: str) -> dict:
        """Extract JSON from LLM output. Tolerant of markdown fences."""
        text = text.strip()

        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines).strip()

        # Try direct JSON parse
        try:
            result = json.loads(text)
            if isinstance(result, dict) and "action" in result:
                return result
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in the text
        match = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]*"[^{}]*\}', text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning(f"Could not parse LLM output: {text[:100]}")
        return {"action": "log", "reason": f"unparseable: {text[:80]}"}

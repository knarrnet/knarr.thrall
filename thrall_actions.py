"""Thrall Switchboard — Action executor.

Five core actions:
- log/drop : write it down, do nothing (99% of mail)
- compile  : add to batch buffer, check flush triggers
- wake     : summon the agent (expensive, rationed)
- act      : execute a skill directly (thrall handles end-to-end)
- reply    : send an auto-reply

Wake has two modes:
- respond : fire-and-forget, self-contained briefing, agent can reply in one shot
- process : orientation brief + reading list, agent reads artifacts then acts
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from db import ThrallDB
from engine import ActionResult

logger = logging.getLogger("thrall.actions")

# Default priority keywords for briefing flagging.
# Override via plugin.toml [config.thrall] priority_keywords or recipe summon_keywords.
PRIORITY_KEYWORDS_DEFAULT = ["URGENT", "CRITICAL", "OUTAGE", "INCIDENT"]

# Artifacts dir for process-mode briefings
_ARTIFACTS_DIR = None


def _get_artifacts_dir(plugin_dir: str) -> str:
    """Ensure artifacts directory exists."""
    global _ARTIFACTS_DIR
    if _ARTIFACTS_DIR is None:
        _ARTIFACTS_DIR = os.path.join(plugin_dir, "artifacts")
        os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
    return _ARTIFACTS_DIR


class ActionExecutor:
    """Dispatches actions from pipeline eval results."""

    def __init__(self, db: ThrallDB, send_mail_fn: Callable = None,
                 call_skill_fn: Callable = None, summon_fn: Callable = None,
                 plugin_dir: str = "", commerce=None,
                 priority_keywords: List[str] = None,
                 memory_writer=None,
                 structured_memory=None,
                 circuit_breaker=None,
                 max_payload_bytes: int = 60_000):
        self.db = db
        self._send_mail = send_mail_fn
        self._call_skill = call_skill_fn
        self._summon = summon_fn
        self._plugin_dir = plugin_dir
        self._commerce = commerce
        self._priority_keywords = priority_keywords or PRIORITY_KEYWORDS_DEFAULT
        self._memory_writer = memory_writer
        self._structured_memory = structured_memory
        self._circuit_breaker = circuit_breaker
        self._max_payload_bytes = max_payload_bytes
        # Consecutive skill failure tracking for memory hooks
        self._skill_fail_streak: Dict[str, int] = {}  # skill_name -> consecutive failures
        # Compilation flush state
        self._buffer_timers: Dict[str, float] = {}  # buffer_name -> last_flush_time

    async def execute(self, recipe: dict, envelope: Any,
                      eval_result: Any, filter_result: Any) -> Any:
        """Route to the appropriate action handler."""


        action = eval_result.action
        actions_cfg = recipe.get("actions", {}).get(action, {})
        from_node = envelope.get("from_node", "")
        logger.debug(f"[{from_node[:8]}] ACTION dispatch={action} "
                     f"eval_type={eval_result.eval_type} reason={eval_result.reason[:80]}")

        if action == "drop" or action == "log":
            return self._do_log(action, eval_result, envelope)

        elif action == "compile":
            return await self._do_compile(recipe, envelope, eval_result, actions_cfg)

        elif action == "wake" or action == "summon":
            return await self._do_summon(recipe, envelope, eval_result,
                                         filter_result, actions_cfg)

        elif action == "act":
            return await self._do_act(envelope, eval_result, actions_cfg)

        elif action == "reply":
            return await self._do_reply(envelope, eval_result, actions_cfg)

        elif action == "wm_approve":
            return await self._do_wm_approve(envelope, eval_result, actions_cfg)

        elif action == "wm_reject":
            return await self._do_wm_reject(envelope, eval_result, actions_cfg)

        elif action == "execute_settlement":
            return await self._do_execute_settlement(envelope, eval_result, actions_cfg)

        else:
            # Unknown action — treat as log
            return ActionResult(action, f"unknown action '{action}', logged")

    def _do_log(self, action: str, eval_result: Any, envelope: Any) -> Any:

        msg = f"{action}: {eval_result.reason} (from={envelope.get('from_node', '?')[:16]})"
        logger.info(f"THRALL {msg}")
        return ActionResult(action, msg)

    async def _do_compile(self, recipe: dict, envelope: Any,
                          eval_result: Any, actions_cfg: dict) -> Any:
        """Add to compilation buffer. Check if flush triggers fire."""


        buffer_name = actions_cfg.get("buffer", recipe.get("compile", {}).get("buffer", "default"))
        body_text = envelope.get("body_text", "")

        # Build the compilation entry — full body, not truncated
        entry = {
            "from_node": envelope.get("from_node", ""),
            "msg_type": envelope.get("msg_type", ""),
            "body": body_text,
            "body_preview": _truncate(body_text, 200),
            "session_id": envelope.get("session_id", ""),
            "eval_action": eval_result.action,
            "eval_reason": eval_result.reason,
            "timestamp": time.time(),
        }

        self.db.add_to_buffer(buffer_name, entry, recipe.get("_name", "unknown"))
        count = self.db.buffer_count(buffer_name)
        threshold = actions_cfg.get("summon_threshold", 0)
        keywords = actions_cfg.get("summon_keywords", [])
        logger.debug(f"COMPILE buffer={buffer_name} count={count} "
                     f"threshold={threshold} keywords={keywords}")

        # Check keyword trigger — immediate summon (word-boundary match)
        if keywords:
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', body_text, re.IGNORECASE):
                    logger.info(f"THRALL keyword trigger: '{kw}' in mail -> summon")
                    await self._flush_and_summon(
                        buffer_name, f"keyword:{kw}")
                    return ActionResult("compile+summon",
                                        f"keyword '{kw}' triggered immediate summon, {count} entries")

        # Check threshold trigger
        if threshold > 0 and count >= threshold:
            logger.info(f"THRALL threshold trigger: {count} >= {threshold} -> summon")
            await self._flush_and_summon(
                buffer_name, f"threshold:{count}")
            return ActionResult("compile+summon",
                                f"threshold {count}>={threshold} triggered summon")

        return ActionResult("compile",
                            f"added to {buffer_name} (count={count})")

    async def check_timer_flush(self, buffer_name: str, interval_seconds: float):
        """Called from on_tick to check if a time-based flush is due."""
        count = self.db.buffer_count(buffer_name)
        if count == 0:
            return

        last_flush = self._buffer_timers.get(buffer_name, 0)
        elapsed = time.time() - last_flush
        if elapsed >= interval_seconds:
            logger.info(f"THRALL timer flush: buffer={buffer_name} "
                        f"elapsed={int(elapsed)}s count={count}")
            await self._flush_and_summon(
                buffer_name, f"timer:{int(elapsed)}s")

    # ── Wake mode: respond (fire-and-forget) ──

    async def _do_summon(self, recipe: dict, envelope: Any,
                         eval_result: Any, filter_result: Any,
                         actions_cfg: dict) -> Any:
        """Immediate summon — self-contained briefing for single-shot response."""


        # Build rich briefing — everything the agent needs to respond
        briefing = {
            "mode": "respond",
            "task": "Reply to this inbound message.",
            "sender": {
                "node_id": envelope.get("from_node", ""),
                "tier": filter_result.tier,
            },
            "message": {
                "type": envelope.get("msg_type", "text"),
                "body": envelope.get("body_text", ""),
                "session_id": envelope.get("session_id", ""),
            },
            "classification": {
                "action": eval_result.action,
                "reason": eval_result.reason,
                "eval_type": eval_result.eval_type,
                "wall_ms": eval_result.wall_ms,
            },
            "reply_to": envelope.get("from_node", ""),
        }

        if self._summon:
            try:
                logger.debug(f"SUMMON respond: sender={envelope.get('from_node', '')[:16]} "
                             f"tier={filter_result.tier} eval={eval_result.eval_type}")
                await self._summon(briefing, "direct", "immediate", [])
            except Exception as e:
                logger.error(f"Summon failed: {e}")
                return ActionResult("summon", f"error: {e}")

        return ActionResult("summon",
                            f"agent woken (respond): {eval_result.reason}")

    # ── Wake mode: process (orientation + reading list) ──

    async def _flush_and_summon(self, buffer_name: str, trigger: str):
        """Flush the buffer and summon the agent with a briefing artifact."""
        entries = self.db.flush_buffer(buffer_name)
        self._buffer_timers[buffer_name] = time.time()

        if not entries:
            return

        # Write full briefing to artifact file
        artifact_path = self._write_briefing_artifact(
            buffer_name, trigger, entries)

        # Build orientation brief
        priority_items = []
        senders = set()
        for e in entries:
            entry = e["entry"]
            senders.add(entry.get("from_node", "?")[:16])
            # Flag priority items (word-boundary match, configurable keywords)
            body = entry.get("body", entry.get("body_preview", ""))
            if any(re.search(r'\b' + re.escape(kw) + r'\b', body, re.IGNORECASE)
                   for kw in self._priority_keywords):
                priority_items.append({
                    "from": entry.get("from_node", "?")[:16],
                    "preview": _truncate(body, 120),
                })

        briefing = {
            "mode": "process",
            "task": f"Review {len(entries)} collected items and act on anything that needs attention.",
            "trigger": trigger,
            "stats": {
                "item_count": len(entries),
                "unique_senders": len(senders),
                "senders": list(senders),
                "time_span_s": int(entries[-1]["created_at"] - entries[0]["created_at"])
                               if len(entries) > 1 else 0,
            },
            "priority_items": priority_items,
            "artifact": artifact_path,
            "instructions": (
                f"Full briefing saved to: {artifact_path}\n"
                "Read the artifact for complete message bodies and classification details.\n"
                "Items are sorted chronologically. Priority items flagged above."
            ),
        }

        if self._summon:
            try:
                await self._summon(briefing, buffer_name, trigger, entries)
            except Exception as e:
                logger.error(f"Summon failed: {e}")
        else:
            logger.warning(f"THRALL summon requested but no summon_fn: {trigger}")

    def _write_briefing_artifact(self, buffer_name: str, trigger: str,
                                  entries: list) -> str:
        """Write a markdown briefing file for the agent to read."""
        artifacts_dir = _get_artifacts_dir(self._plugin_dir)
        ts = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{ts}-{buffer_name}.md"
        filepath = os.path.join(artifacts_dir, filename)

        lines = [
            f"# Thrall Briefing: {buffer_name}",
            f"",
            f"Trigger: {trigger}",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
            f"Items: {len(entries)}",
            f"",
            f"---",
            f"",
        ]

        for i, e in enumerate(entries, 1):
            entry = e["entry"]
            from_node = entry.get("from_node", "unknown")
            msg_type = entry.get("msg_type", "text")
            body = entry.get("body", entry.get("body_preview", "(no body)"))
            reason = entry.get("eval_reason", "")
            action = entry.get("eval_action", "")
            ts_str = time.strftime(
                "%H:%M:%S",
                time.gmtime(entry.get("timestamp", e.get("created_at", 0))))

            lines.append(f"## Item {i} [{ts_str}]")
            lines.append(f"")
            lines.append(f"- **From**: `{from_node[:16]}`")
            lines.append(f"- **Type**: {msg_type}")
            lines.append(f"- **Classification**: {action} ({reason})")
            lines.append(f"")
            lines.append(f"### Body")
            lines.append(f"")
            lines.append(body)
            lines.append(f"")
            lines.append(f"---")
            lines.append(f"")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"THRALL artifact written: {filepath} ({len(entries)} items)")
        return filepath

    async def _do_act(self, envelope: Any, eval_result: Any,
                      actions_cfg: dict) -> Any:
        """Execute a skill directly — thrall handles end-to-end."""


        skill = actions_cfg.get("skill")
        if not skill:
            return ActionResult("act", "no skill configured")

        if not self._call_skill:
            return ActionResult("act", f"would call {skill} (no executor)")

        # Circuit breaker check — skip if circuit is open for this peer+skill
        peer_id = envelope.get("from_node", "") or envelope.get("peer_node_id", "")
        if self._circuit_breaker and peer_id and self._circuit_breaker.is_open(peer_id, skill):
            logger.info(f"CIRCUIT_SKIP peer={peer_id[:16]} skill={skill}")
            return ActionResult("act_skipped",
                                f"circuit open for peer={peer_id[:16]} skill={skill}")

        # Build skill input from config template + envelope
        input_template = actions_cfg.get("input", {})
        skill_input = {}
        for k, v in input_template.items():
            if isinstance(v, str) and v.startswith("{{") and v.endswith("}}"):
                field_path = v[2:-2].strip()
                if field_path.startswith("envelope."):
                    skill_input[k] = envelope.get(field_path[9:], "")
                elif field_path.startswith("eval."):
                    skill_input[k] = getattr(eval_result, field_path[5:], "")
                else:
                    skill_input[k] = v
            else:
                skill_input[k] = v

        # Payload guard: cockpit has an input body limit (default 64KB, HTTP 413).
        # Truncate oversized string values to stay under the cap.
        cap = self._max_payload_bytes
        payload_size = len(json.dumps(skill_input).encode("utf-8"))
        if payload_size > cap:
            logger.warning(
                f"PAYLOAD_GUARD {skill}: {payload_size}B > {cap}B, truncating")
            for k in sorted(skill_input, key=lambda k: len(str(skill_input[k])), reverse=True):
                v = skill_input[k]
                if isinstance(v, str) and len(v) > 2000:
                    skill_input[k] = v[:2000] + f"... [truncated from {len(v)} chars]"
                    payload_size = len(json.dumps(skill_input).encode("utf-8"))
                    if payload_size <= cap:
                        break
            # Hard stop: if still oversized after all truncation, do not call skill
            if payload_size > cap:
                logger.error(
                    f"PAYLOAD_GUARD {skill}: still {payload_size}B after truncation, aborting")
                return ActionResult("act_error",
                                    f"payload {payload_size}B exceeds {cap}B limit")

        try:
            result = await self._call_skill(skill, skill_input)

            # Detect error results from skill execution
            is_error = False
            error_msg = ""
            if isinstance(result, dict):
                if result.get("status") == "error":
                    is_error = True
                    error_msg = result.get("error", result.get("message", "unknown error"))
                elif result.get("error"):
                    is_error = True
                    error_msg = str(result["error"])

            if is_error:
                logger.warning(f"SKILL_ERROR {skill}: {error_msg}")
                # Circuit breaker: record failure for this peer+skill
                if self._circuit_breaker and peer_id:
                    self._circuit_breaker.record_failure(peer_id, skill)
                # Track consecutive failures for memory hook
                self._skill_fail_streak[skill] = self._skill_fail_streak.get(skill, 0) + 1
                if self._memory_writer and self._skill_fail_streak[skill] >= 3:
                    peer = envelope.get("from_node", "?")[:16]
                    self._memory_writer.append(
                        "operations",
                        f"Skill `{skill}` from peer {peer}: unreliable, "
                        f"{self._skill_fail_streak[skill]} consecutive failures. "
                        f"Last error: {_truncate(error_msg, 100)}")
                    self._skill_fail_streak[skill] = 0  # reset after recording
                # Compile errors into error buffer for awareness
                error_buffer = actions_cfg.get("error_buffer")
                if error_buffer:
                    entry = {
                        "skill": skill,
                        "input": str(skill_input)[:200],
                        "error": error_msg,
                        "timestamp": time.time(),
                    }
                    self.db.add_to_buffer(error_buffer, entry, f"act:{skill}")
                if self._structured_memory and skill not in ("swarm-probe-lite", "knarr-static"):
                    err_peer = (skill_input.get("peer_node_id", "")
                                or envelope.get("from_node", "")
                                or envelope.get("peer_node_id", ""))
                    self._structured_memory.record(
                        skill=skill, node_id=err_peer or "self",
                        outcome="error",
                        reasoning=_truncate(error_msg, 200))
                return ActionResult("act_error",
                                    f"skill={skill} error={_truncate(error_msg, 200)}")

            # Success — reset failure streak and circuit breaker
            self._skill_fail_streak.pop(skill, None)
            if self._circuit_breaker and peer_id:
                self._circuit_breaker.record_success(peer_id, skill)
            # Resolve peer: skill_input > envelope > result
            peer = (skill_input.get("peer_node_id", "")
                    or envelope.get("from_node", "")
                    or envelope.get("peer_node_id", "")
                    or (result.get("node_id", "") if isinstance(result, dict) else ""))
            peer_display = peer[:16] if peer else "self"
            if self._memory_writer and skill not in ("swarm-probe-lite", "knarr-mail", "knarr-static"):
                self._memory_writer.append(
                    "peers",
                    f"Peer {peer_display}: `{skill}` succeeded. "
                    f"Result: {_truncate(str(result), 80)}")
            if self._structured_memory and skill not in ("swarm-probe-lite", "knarr-static"):
                # Build useful reasoning: eval reason + result summary
                reason_parts = []
                if eval_result and getattr(eval_result, "reason", ""):
                    reason_parts.append(eval_result.reason)
                if isinstance(result, dict):
                    summary = result.get("result_summary", "") or result.get("status", "")
                    if summary:
                        reason_parts.append(str(summary))
                reason = " | ".join(reason_parts) if reason_parts else _truncate(str(result), 200)
                # Extract amount from skill_input if available
                amount = 0.0
                try:
                    amount = float(skill_input.get("settle_amount", 0))
                except (ValueError, TypeError):
                    pass
                self._structured_memory.record(
                    skill=skill, node_id=peer or "self",
                    outcome="success", amount=amount,
                    reasoning=_truncate(reason, 500))
            return ActionResult("act", f"skill={skill} result={_truncate(str(result), 200)}")
        except Exception as e:
            logger.error(f"Skill call failed: {skill}: {e}")
            return ActionResult("act_error", f"skill={skill} exception: {e}")

    async def _do_reply(self, envelope: Any, eval_result: Any,
                        actions_cfg: dict) -> Any:
        """Send an auto-reply."""


        if not self._send_mail:
            return ActionResult("reply", "no send_mail_fn configured")

        # Allow recipe to override target (e.g. greeter replying to
        # a peer from a bus event where from_node is empty)
        to_node = actions_cfg.get("to_node", "") or envelope.get("from_node", "")
        # Substitute envelope fields in to_node (e.g. "{{node_id}}")
        if "{{" in to_node:
            import re
            to_node = re.sub(
                r"\{\{(.+?)\}\}",
                lambda m: envelope.get(
                    m.group(1).replace("envelope.", "").strip(), m.group(0)),
                to_node)
        session_id = envelope.get("session_id", "")
        template = actions_cfg.get("template", eval_result.reason)

        try:
            await self._send_mail(to_node, "text",
                                  {"type": "text", "content": template},
                                  session_id)
            return ActionResult("reply", f"replied to {to_node[:16]}")
        except Exception as e:
            return ActionResult("reply", f"error: {e}")


    async def _do_wm_approve(self, envelope: Any, eval_result: Any,
                              actions_cfg: dict) -> Any:
        """Approve a document held in WM quarantine."""


        doc_id = envelope.get("document_id", "")
        if not doc_id:
            return ActionResult("wm_approve", "no document_id in envelope")

        if not self._call_skill:
            return ActionResult("wm_approve", f"would approve {doc_id} (no executor)")

        try:
            from commerce import ThrallCommerce
            commerce = getattr(self, "_commerce", None)
            if not commerce:
                # Fall back to cockpit API directly
                result = await self._call_skill("knarr-mail", {
                    "action": "wm_approve", "document_id": doc_id})
            else:
                result = await commerce.approve_quarantine(doc_id)
            logger.info(f"WM_APPROVE doc={doc_id[:16]} result={result}")
            return ActionResult("wm_approve", f"approved {doc_id[:16]}")
        except Exception as e:
            logger.error(f"WM_APPROVE failed: {e}")
            return ActionResult("wm_approve", f"error: {e}")

    async def _do_wm_reject(self, envelope: Any, eval_result: Any,
                             actions_cfg: dict) -> Any:
        """Reject a document held in WM quarantine."""


        doc_id = envelope.get("document_id", "")
        reason = actions_cfg.get("reason", eval_result.reason)
        if not doc_id:
            return ActionResult("wm_reject", "no document_id in envelope")

        if not self._call_skill:
            return ActionResult("wm_reject", f"would reject {doc_id} (no executor)")

        try:
            from commerce import ThrallCommerce
            commerce = getattr(self, "_commerce", None)
            if not commerce:
                result = await self._call_skill("knarr-mail", {
                    "action": "wm_reject", "document_id": doc_id, "reason": reason})
            else:
                result = await commerce.reject_quarantine(doc_id, reason)
            logger.info(f"WM_REJECT doc={doc_id[:16]} reason={reason[:80]}")
            return ActionResult("wm_reject", f"rejected {doc_id[:16]}: {reason[:80]}")
        except Exception as e:
            logger.error(f"WM_REJECT failed: {e}")
            return ActionResult("wm_reject", f"error: {e}")

    async def _do_execute_settlement(self, envelope: Any, eval_result: Any,
                                      actions_cfg: dict) -> Any:
        """Execute an on-chain settlement transfer (Solana devnet)."""


        skill = actions_cfg.get("skill", "settlement-execute-lite")
        if not self._call_skill:
            return ActionResult("execute_settlement", f"would call {skill} (no executor)")

        # Build skill input from envelope fields
        skill_input = {
            "peer_node_id": envelope.get("peer_node_id", ""),
            "peer_public_key": envelope.get("peer_public_key", ""),
            "settle_amount": envelope.get("settle_amount", "0"),
        }
        # Merge any explicit input from recipe config
        for k, v in actions_cfg.get("input", {}).items():
            if isinstance(v, str) and v.startswith("{{") and v.endswith("}}"):
                field = v[2:-2].strip()
                skill_input[k] = envelope.get(field, "")
            else:
                skill_input[k] = v

        try:
            result = await self._call_skill(skill, skill_input)
            status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            logger.info(f"EXECUTE_SETTLEMENT {skill}: status={status}")
            # Living memory hook: record settlement execution
            if self._memory_writer:
                peer = skill_input.get("peer_node_id", "?")[:16]
                amount = skill_input.get("settle_amount", "?")
                tx_hash = result.get("tx_hash", "") if isinstance(result, dict) else ""
                domain = "strategy" if status == "ok" else "operations"
                entry = f"On-chain settlement to peer {peer}: {amount}cr, status={status}"
                if tx_hash:
                    entry += f", tx={tx_hash[:16]}"
                self._memory_writer.append(domain, entry)
            return ActionResult("execute_settlement",
                                f"skill={skill} status={status} "
                                f"result={_truncate(str(result), 200)}")
        except Exception as e:
            logger.error(f"EXECUTE_SETTLEMENT {skill} failed: {e}")
            return ActionResult("execute_settlement", f"error: {e}")


def _truncate(s: str, maxlen: int) -> str:
    return s[:maxlen] + "..." if len(s) > maxlen else s

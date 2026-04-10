"""Thrall Checklists — persistent multi-step task execution.

Checklists track sequential actions across scheduler cycles. Two types:
  - structured: pre-defined steps, executed mechanically (no LLM)
  - llm: high-level goals, each step goes through LLM for specific action

Each checklist is scoped to a peer_node_id (or own node for self-tasks).
Multiple checklists can run in parallel, prioritized by priority field.

Schema: thrall_checklists in ThrallDB.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("thrall.checklists")


@dataclass
class ChecklistStep:
    step: int
    action: str          # buy_skill, call_own_skill, send_mail, poll_result, store_result, noop, llm_decide, evaluate
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending, active, completed, failed, skipped
    result: Optional[Any] = None
    error: Optional[str] = None
    execute_at: Optional[float] = None  # timestamp — skip until this time
    completed_at: Optional[float] = None
    condition: Optional[Dict[str, Any]] = None  # pre-condition for this step


@dataclass
class Checklist:
    id: str
    peer_node_id: str
    checklist_type: str   # structured, llm
    name: str
    steps: List[ChecklistStep]
    current_step: int = 0
    status: str = "active"  # active, paused, completed, failed, expired
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: Optional[float] = None
    source: str = "self"   # self, advisor, mail, recipe, auto
    priority: int = 5      # 1=highest, 10=lowest
    condition_json: Optional[Dict[str, Any]] = None  # pre-conditions for activation


class ChecklistManager:
    """Manages checklist lifecycle and step execution."""

    # Limits
    MAX_PER_PEER = 1
    MAX_SELF = 3
    MAX_TOTAL = 10
    MAX_RETRIES = 3

    def __init__(self, db, call_skill_fn: Callable = None,
                 send_mail_fn: Callable = None,
                 memory_writer=None,
                 own_node_id: str = ""):
        self._db = db
        self._call_skill = call_skill_fn
        self._send_mail = send_mail_fn
        self._memory_writer = memory_writer
        self._own_node_id = own_node_id
        self._ensure_table()

    def _ensure_table(self):
        self._db._conn.executescript("""
            CREATE TABLE IF NOT EXISTS thrall_checklists (
                id TEXT PRIMARY KEY,
                peer_node_id TEXT NOT NULL,
                checklist_type TEXT NOT NULL,
                name TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                current_step INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL,
                expires_at REAL,
                source TEXT DEFAULT 'self',
                priority INTEGER DEFAULT 5,
                condition_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_checklists_status
                ON thrall_checklists(status);
            CREATE INDEX IF NOT EXISTS idx_checklists_peer
                ON thrall_checklists(peer_node_id);
        """)

    # ── CRUD ──

    def create(self, checklist: Checklist) -> str:
        """Insert a new checklist. Returns the ID."""
        if not checklist.id:
            checklist.id = str(uuid.uuid4())[:12]
        if not checklist.created_at:
            checklist.created_at = time.time()
        checklist.updated_at = checklist.created_at

        # Enforce limits
        active = self.get_active()
        if len(active) >= self.MAX_TOTAL:
            logger.warning("CHECKLIST_LIMIT total=%d max=%d", len(active), self.MAX_TOTAL)
            return ""
        peer_count = sum(1 for c in active if c.peer_node_id == checklist.peer_node_id)
        if checklist.peer_node_id == self._own_node_id:
            if peer_count >= self.MAX_SELF:
                logger.debug("CHECKLIST_LIMIT_SELF count=%d", peer_count)
                return ""
        elif peer_count >= self.MAX_PER_PEER:
            logger.debug("CHECKLIST_LIMIT_PEER peer=%s", checklist.peer_node_id[:16])
            return ""

        steps_json = json.dumps([_step_to_dict(s) for s in checklist.steps])
        self._db._conn.execute(
            "INSERT OR REPLACE INTO thrall_checklists "
            "(id, peer_node_id, checklist_type, name, steps_json, current_step, "
            "status, created_at, updated_at, expires_at, source, priority, condition_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (checklist.id, checklist.peer_node_id, checklist.checklist_type,
             checklist.name, steps_json, checklist.current_step,
             checklist.status, checklist.created_at, checklist.updated_at,
             checklist.expires_at, checklist.source, checklist.priority,
             json.dumps(checklist.condition_json) if checklist.condition_json else None))
        self._db._conn.commit()
        logger.info("CHECKLIST_CREATE id=%s name=%s type=%s steps=%d peer=%s",
                     checklist.id, checklist.name, checklist.checklist_type,
                     len(checklist.steps), checklist.peer_node_id[:16])
        return checklist.id

    def get_active(self) -> List[Checklist]:
        """Return all active checklists, ordered by priority."""
        rows = self._db._conn.execute(
            "SELECT * FROM thrall_checklists WHERE status IN ('active', 'paused') "
            "ORDER BY priority ASC, created_at ASC"
        ).fetchall()
        return [_row_to_checklist(r) for r in rows]

    def get_by_id(self, checklist_id: str) -> Optional[Checklist]:
        r = self._db._conn.execute(
            "SELECT * FROM thrall_checklists WHERE id = ?", (checklist_id,)
        ).fetchone()
        return _row_to_checklist(r) if r else None

    def update(self, checklist: Checklist):
        """Persist checklist state."""
        checklist.updated_at = time.time()
        steps_json = json.dumps([_step_to_dict(s) for s in checklist.steps])
        self._db._conn.execute(
            "UPDATE thrall_checklists SET steps_json=?, current_step=?, "
            "status=?, updated_at=? WHERE id=?",
            (steps_json, checklist.current_step, checklist.status,
             checklist.updated_at, checklist.id))
        self._db._conn.commit()

    def cleanup(self):
        """Remove expired and old completed checklists."""
        now = time.time()
        # Expire active checklists past TTL
        expired = self._db._conn.execute(
            "UPDATE thrall_checklists SET status='expired' "
            "WHERE status='active' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,)).rowcount
        # Purge completed/expired older than 1 hour
        purged = self._db._conn.execute(
            "DELETE FROM thrall_checklists "
            "WHERE status IN ('completed', 'expired') AND updated_at < ?",
            (now - 3600,)).rowcount
        # Purge failed older than 24 hours
        purged += self._db._conn.execute(
            "DELETE FROM thrall_checklists "
            "WHERE status = 'failed' AND updated_at < ?",
            (now - 86400,)).rowcount
        self._db._conn.commit()
        if expired or purged:
            logger.info("CHECKLIST_CLEANUP expired=%d purged=%d", expired, purged)

    # ── Execution ──

    async def advance_structured(self) -> int:
        """Advance all active structured checklists. Returns count advanced."""
        advanced = 0
        now = time.time()

        for cl in self.get_active():
            if cl.checklist_type != "structured":
                continue
            if cl.status == "paused":
                # Check condition
                if cl.condition_json and not self._check_condition(cl.condition_json):
                    continue
                cl.status = "active"

            if cl.current_step >= len(cl.steps):
                cl.status = "completed"
                self.update(cl)
                logger.info("CHECKLIST_DONE id=%s name=%s", cl.id, cl.name)
                continue

            step = cl.steps[cl.current_step]

            # Check execute_at (grace period)
            if step.execute_at and step.execute_at > now:
                continue

            # Check step condition
            if step.condition:
                prev_result = cl.steps[cl.current_step - 1].result if cl.current_step > 0 else None
                if not self._check_step_condition(step.condition, prev_result):
                    step.status = "skipped"
                    step.completed_at = now
                    cl.current_step += 1
                    self.update(cl)
                    advanced += 1
                    continue

            # Execute
            step.status = "active"
            try:
                result = await self._execute_step(step, cl)
                step.result = result
                step.status = "completed"
                step.completed_at = now
                cl.current_step += 1
                advanced += 1
                logger.info("CHECKLIST_STEP id=%s step=%d/%d action=%s",
                            cl.id, cl.current_step, len(cl.steps), step.action)
            except Exception as e:
                step.error = str(e)[:200]
                retries = step.params.get("_retries", 0)
                if retries < self.MAX_RETRIES:
                    step.params["_retries"] = retries + 1
                    step.status = "pending"
                    step.execute_at = now + 10  # retry after 10s
                    logger.warning("CHECKLIST_RETRY id=%s step=%d retry=%d: %s",
                                   cl.id, cl.current_step, retries + 1, e)
                else:
                    step.status = "failed"
                    cl.status = "failed"
                    logger.error("CHECKLIST_FAIL id=%s step=%d: %s", cl.id, cl.current_step, e)

            self.update(cl)

        return advanced

    async def _execute_step(self, step: ChecklistStep, cl: Checklist) -> Any:
        """Execute a single checklist step."""
        action = step.action
        params = step.params

        if action == "noop":
            wait = params.get("wait_seconds", 0)
            if wait and cl.current_step + 1 < len(cl.steps):
                cl.steps[cl.current_step + 1].execute_at = time.time() + wait
            return {"status": "ok"}

        elif action == "buy_skill":
            if not self._call_skill:
                return {"status": "error", "error": "no call_skill fn"}
            skill = self._resolve_template(params.get("skill_name", ""), cl)
            input_data = self._resolve_template(params.get("input", ""), cl)
            # Build skill input based on skill type (same logic as handler buy_skill)
            skill_input = {}
            if input_data:
                name_lower = skill.lower()
                if "creative" in name_lower or "gen" in name_lower:
                    skill_input = {"theme": input_data, "format": "poem"}
                elif "judge" in name_lower or "quality" in name_lower:
                    skill_input = {"content": input_data}
                elif "game-seat" in name_lower:
                    import re as _re_cl
                    _m = _re_cl.search(r'game-seat-([a-f0-9]+)', skill)
                    skill_input = {"game_id": _m.group(1), "action": "join"} if _m else {"action": input_data}
                elif "game-submit" in name_lower:
                    import re as _re_cl2
                    _m = _re_cl2.search(r'game-submit-([a-f0-9]+)', skill)
                    import random as _rnd_cl
                    skill_input = {"game_id": _m.group(1), "number": _rnd_cl.randint(0, 1000)} if _m else {"number": _rnd_cl.randint(0, 1000)}
                elif "casino" in name_lower or "game" in name_lower or "collect" in name_lower:
                    skill_input = {"action": input_data}
                elif "advisor" in name_lower:
                    skill_input = {"query": input_data}
                else:
                    skill_input = {"input": input_data, "text": input_data}
            result = await self._call_skill(skill, skill_input)
            return result

        elif action == "call_own_skill":
            if not self._call_skill:
                return {"status": "error", "error": "no call_skill fn"}
            skill = params.get("skill_name", "")
            result = await self._call_skill(skill, params.get("skill_input", {}))
            return result

        elif action == "send_mail":
            if not self._send_mail:
                return {"status": "error", "error": "no send_mail fn"}
            to_node = self._resolve_template(params.get("to", cl.peer_node_id), cl)
            content = self._resolve_template(params.get("content", ""), cl)
            await self._send_mail(to_node, "text", {"type": "text", "content": content}, "")
            return {"status": "sent"}

        elif action == "poll_result":
            # Poll cockpit for async job result
            job_id = self._resolve_template(params.get("job_id", ""), cl)
            if not job_id:
                return {"status": "error", "error": "no job_id"}
            # The result should already be in async_jobs by now
            # Return whatever the job status is
            return {"status": "polled", "job_id": job_id}

        elif action == "store_result":
            # Write previous step's result to memory pillar
            if self._memory_writer and cl.current_step > 0:
                prev = cl.steps[cl.current_step - 1]
                domain = params.get("domain", "strategy")
                fmt = params.get("format", "Checklist '{name}': {result}")
                entry = fmt.replace("{name}", cl.name)
                entry = entry.replace("{result}", str(prev.result)[:200] if prev.result else "no result")
                self._memory_writer.append(domain, entry)
            return {"status": "stored"}

        else:
            logger.warning("CHECKLIST_UNKNOWN_ACTION action=%s", action)
            return {"status": "unknown_action"}

    def _resolve_template(self, value: str, cl: Checklist) -> str:
        """Replace template variables in step params."""
        if not isinstance(value, str):
            return value
        value = value.replace("{{own_node_id}}", self._own_node_id)
        value = value.replace("{{peer_node_id}}", cl.peer_node_id)
        # Replace {{prev_result.*}} with previous step's result
        if cl.current_step > 0:
            prev = cl.steps[cl.current_step - 1]
            if isinstance(prev.result, dict):
                for k, v in prev.result.items():
                    value = value.replace("{{prev_result." + k + "}}", str(v))
            value = value.replace("{{prev_result}}", str(prev.result)[:100] if prev.result else "")
        # Random number for casino
        if "{{random_number}}" in value:
            import random
            value = value.replace("{{random_number}}", str(random.randint(0, 1000)))
        return value

    def _check_condition(self, condition: dict) -> bool:
        """Check if a checklist pre-condition is met."""
        ctype = condition.get("type", "")
        params = condition.get("params", {})

        if ctype == "time_after":
            return time.time() >= params.get("timestamp", 0)
        elif ctype == "checklist_completed":
            other = self.get_by_id(params.get("checklist_id", ""))
            return other is not None and other.status == "completed"
        # Future: balance_above, mail_received, skill_available, etc.
        return True  # unknown conditions pass by default

    def _check_step_condition(self, condition: dict, prev_result: Any) -> bool:
        """Check a per-step condition (e.g., only collect if winner)."""
        if not condition or not prev_result:
            return True
        if isinstance(prev_result, dict):
            for key, expected in condition.items():
                actual = str(prev_result.get(key.split(".")[-1], ""))
                expected_resolved = expected.replace("{{own_node_id}}", self._own_node_id)
                if actual != expected_resolved:
                    return False
        return True

    # ── Templates ──

    def create_result_pickup(self, job_id: str, skill_name: str,
                              peer_node_id: str) -> str:
        """Auto-create a result pickup checklist for a buy_skill call."""
        cl = Checklist(
            id="", peer_node_id=peer_node_id,
            checklist_type="structured",
            name=f"pickup:{skill_name}",
            steps=[
                ChecklistStep(step=0, action="noop",
                              params={"wait_seconds": 10}),
                ChecklistStep(step=1, action="poll_result",
                              params={"job_id": job_id}),
                ChecklistStep(step=2, action="store_result",
                              params={"domain": "strategy",
                                      "format": "Bought {name}: {result}"}),
            ],
            source="auto", priority=2,
            expires_at=time.time() + 120,
        )
        return self.create(cl)

    def create_casino_game(self, host_node_id: str,
                            seat_skill: str, submit_skill: str = "",
                            collect_skill: str = "") -> str:
        """Create a casino game checklist from a mail invitation."""
        steps = [
            ChecklistStep(step=0, action="buy_skill",
                          params={"skill_name": seat_skill, "input": "seat"}),
        ]
        if submit_skill:
            # Use game_id from seat result (not skill name — seat skills are reused across games)
            steps.append(ChecklistStep(
                step=1, action="buy_skill",
                params={"skill_name": "game-submit-{{prev_result.game_id}}",
                        "input": "{{random_number}}"},
                execute_at=time.time() + 60))  # wait for submit skill registration
        if collect_skill:
            steps.append(ChecklistStep(
                step=2, action="buy_skill",
                params={"skill_name": collect_skill, "input": "collect"},
                execute_at=time.time() + 60,
                condition={"prev_result.winner": "{{own_node_id}}"}))

        cl = Checklist(
            id="", peer_node_id=host_node_id,
            checklist_type="structured",
            name=f"casino:{seat_skill}",
            steps=steps,
            source="mail", priority=3,
            expires_at=time.time() + 600,
        )
        return self.create(cl)

    def create_from_advisor(self, advisor_node_id: str,
                             strategy: dict) -> str:
        """Create checklist from advisor response."""
        advisor_steps = strategy.get("checklist", [])
        if not advisor_steps:
            return ""
        steps = []
        for i, s in enumerate(advisor_steps):
            steps.append(ChecklistStep(
                step=i,
                action=s.get("action", "llm_decide"),
                params=s.get("params", {}),
            ))
        cl = Checklist(
            id="", peer_node_id=advisor_node_id,
            checklist_type="llm",
            name=f"advisor-strategy",
            steps=steps,
            source="advisor", priority=5,
            expires_at=time.time() + 3600,
        )
        return self.create(cl)

    def summary_for_llm(self) -> str:
        """Format active checklists for LLM context."""
        active = self.get_active()
        if not active:
            return ""
        lines = ["Active checklists:"]
        for cl in active[:5]:
            step_info = f"step {cl.current_step + 1}/{len(cl.steps)}"
            if cl.current_step < len(cl.steps):
                step = cl.steps[cl.current_step]
                step_info += f" ({step.action})"
                if step.execute_at and step.execute_at > time.time():
                    wait = int(step.execute_at - time.time())
                    step_info += f" wait {wait}s"
            lines.append(f"  [{cl.priority}] \"{cl.name}\" — {step_info}")
        return "\n".join(lines)


# ── Serialization helpers ──

def _step_to_dict(s: ChecklistStep) -> dict:
    d = {"step": s.step, "action": s.action, "params": s.params, "status": s.status}
    if s.result is not None:
        d["result"] = s.result
    if s.error:
        d["error"] = s.error
    if s.execute_at:
        d["execute_at"] = s.execute_at
    if s.completed_at:
        d["completed_at"] = s.completed_at
    if s.condition:
        d["condition"] = s.condition
    return d


def _dict_to_step(d: dict) -> ChecklistStep:
    return ChecklistStep(
        step=d.get("step", 0),
        action=d.get("action", "noop"),
        params=d.get("params", {}),
        status=d.get("status", "pending"),
        result=d.get("result"),
        error=d.get("error"),
        execute_at=d.get("execute_at"),
        completed_at=d.get("completed_at"),
        condition=d.get("condition"),
    )


def _row_to_checklist(row) -> Checklist:
    cols = ["id", "peer_node_id", "checklist_type", "name", "steps_json",
            "current_step", "status", "created_at", "updated_at",
            "expires_at", "source", "priority", "condition_json"]
    if hasattr(row, "keys"):
        d = dict(row)
    else:
        d = dict(zip(cols, row))
    steps = [_dict_to_step(s) for s in json.loads(d.get("steps_json", "[]"))]
    cond = json.loads(d["condition_json"]) if d.get("condition_json") else None
    return Checklist(
        id=d["id"], peer_node_id=d["peer_node_id"],
        checklist_type=d["checklist_type"], name=d["name"],
        steps=steps, current_step=d.get("current_step", 0),
        status=d.get("status", "active"),
        created_at=d.get("created_at", 0),
        updated_at=d.get("updated_at", 0),
        expires_at=d.get("expires_at"),
        source=d.get("source", "self"),
        priority=d.get("priority", 5),
        condition_json=cond,
    )

"""thrall_scorer.py — Deterministic action scoring for the scored menu decision mode.

Generates ranked action candidates based on gathered node state.
No LLM involved — pure heuristic scoring. The LLM only SELECTS from the menu.

Based on: Werewolf RL (ICML 2024) — generate diverse candidates, score externally.
"""
import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ActionOption:
    """A scored action candidate for the LLM to select from."""
    action: str              # buy_skill, send_mail, play_casino, call_own_skill, rest
    score: float             # 0.0 to 1.0
    reason: str              # human-readable explanation for the LLM
    params: Dict[str, Any] = field(default_factory=dict)  # action-specific parameters


@dataclass
class NodeState:
    """Gathered state for scoring decisions."""
    net_balance: float = 0.0
    positions: List[Dict] = field(default_factory=list)
    peers: List[Dict] = field(default_factory=list)
    foreign_skills: List[Dict] = field(default_factory=list)
    own_skills: List[str] = field(default_factory=list)
    recent_actions: List[Dict] = field(default_factory=list)   # last N actions from strategy pillar
    casino_invites: List[Dict] = field(default_factory=list)
    pending_checklists: int = 0
    unread_mail: int = 0
    cycle_count: int = 0
    knowledge_domains: List[str] = field(default_factory=list)  # ingested knowledge packs


def score_options(state: NodeState, goal: str = "",
                  own_node_id: str = "",
                  max_options: int = 5) -> List[ActionOption]:
    """Generate and score action candidates from current state.

    Returns sorted list (highest score first), capped at max_options.
    """
    options: List[ActionOption] = []
    now = time.time()

    # Track what we did recently to encourage variety
    recent_skills = set()
    recent_peers = set()
    recent_actions_set = set()
    consecutive_rest = 0
    for a in state.recent_actions[-10:]:
        act = a.get("action", "")
        recent_actions_set.add(act)
        if act == "buy_skill":
            recent_skills.add(a.get("skill", ""))
        if act == "send_mail":
            recent_peers.add(a.get("peer", ""))
        if act == "rest":
            consecutive_rest += 1
        else:
            consecutive_rest = 0

    # ── BUY SKILL ──
    for skill in state.foreign_skills:
        name = skill.get("name", "")
        price = float(skill.get("price", 1))
        provider = skill.get("provider", "")

        # Skip skills we already own (buying own skill = local call, no credit)
        if name in state.own_skills:
            continue
        # Skip if we can't afford it
        if state.net_balance < -price:
            continue
        # Skip game-seat skills (handled by play_casino)
        if name.startswith("game-seat-") or name.startswith("game-submit-"):
            continue
        # Skip boring system skills
        if name in ("echo", "cluster-state-query", "knarr-mail", "knarr-static",
                     "knarr-faucet", "welcome-airdrop-lite"):
            continue

        score = 0.3  # base

        # Variety bonus: haven't bought this recently
        if name not in recent_skills:
            score += 0.15

        # Strategic value by skill type
        if "judge" in name or "quality" in name:
            score += 0.2  # quality feedback is high value
            # Extra bonus if we recently bought creative output
            if "creative" in " ".join(recent_skills):
                score += 0.15  # self-improvement loop
        elif "advisor" in name:
            score += 0.15  # strategic insight
        elif "creative" in name or "gen" in name:
            score += 0.1   # content production
        elif "knowledge" in name:
            score += 0.2   # knowledge acquisition is high value

        # Affordability: healthier balance = more willing to spend
        if state.net_balance > 10:
            score += 0.1
        elif state.net_balance < -5:
            score -= 0.2

        # Anti-stagnation: if we've been resting, boost buy
        if consecutive_rest >= 2:
            score += 0.2

        score = max(0.0, min(1.0, score))
        options.append(ActionOption(
            action="buy_skill",
            score=score,
            reason=f"Buy {name} from {provider} ({price}cr)",
            params={"skill_name": name, "provider": provider, "price": price},
        ))

    # ── SEND MAIL ──
    for peer in state.peers:
        peer_id = peer.get("node_id", "")[:16]
        peer_skills = peer.get("skills", [])

        # Skip if we recently mailed this peer
        if peer_id in recent_peers:
            continue

        score = 0.25  # base

        # Bonus if peer has skills we want
        wants = set()
        for s in peer_skills:
            if s not in state.own_skills and s not in recent_skills:
                wants.add(s)
        if wants:
            score += 0.2
            reason = f"Trade proposal to {peer_id} (has {', '.join(list(wants)[:2])})"
        else:
            reason = f"Introduce yourself to {peer_id}"

        # Bonus if we have unread mail (should respond)
        if state.unread_mail > 0:
            score += 0.15

        score = max(0.0, min(1.0, score))
        options.append(ActionOption(
            action="send_mail",
            score=score,
            reason=reason,
            params={"peer": peer_id, "peer_skills": list(wants)[:3]},
        ))

    # ── PLAY CASINO ──
    for invite in state.casino_invites:
        game_id = invite.get("game_id", "")
        score = 0.35  # moderate — gambling is risky
        if state.net_balance > 5:
            score += 0.1  # can afford to gamble

        options.append(ActionOption(
            action="play_casino",
            score=score,
            reason=f"Join game {game_id} (1cr bet, ~1.9cr potential win)",
            params={"game_id": game_id},
        ))

    # ── CALL OWN SKILL (for casino hosts etc.) ──
    if "number-game-host-lite" in state.own_skills:
        # Check if we should create a new game
        score = 0.4
        options.append(ActionOption(
            action="call_own_skill",
            score=score,
            reason="Create a new casino game (earn 5% rake)",
            params={"skill_name": "number-game-host-lite", "action": "create"},
        ))

    # ── REST ──
    rest_score = 0.05  # always lowest
    if state.net_balance < -8:
        rest_score = 0.3  # conserve when broke
    if state.pending_checklists > 2:
        rest_score = 0.2  # too many pending tasks

    options.append(ActionOption(
        action="rest",
        score=rest_score,
        reason="No urgent opportunities — conserve resources",
        params={},
    ))

    # Sort by score descending, deduplicate by action+skill, cap at max_options
    options.sort(key=lambda o: -o.score)
    seen = set()
    deduped = []
    for o in options:
        key = f"{o.action}:{o.params.get('skill_name', o.params.get('peer', ''))}"
        if key not in seen:
            seen.add(key)
            deduped.append(o)
        if len(deduped) >= max_options:
            break

    return deduped


def format_menu(options: List[ActionOption], state: NodeState) -> str:
    """Format scored options as a text menu for the LLM."""
    letters = "ABCDEFGHIJ"
    lines = [
        f"Cycle {state.cycle_count}. Balance: {state.net_balance:.1f}cr. "
        f"Positions: {len(state.positions)}. Pending: {state.pending_checklists}.",
        "",
        "Choose the best action:",
        "",
    ]
    for i, opt in enumerate(options):
        letter = letters[i] if i < len(letters) else str(i)
        lines.append(f"{letter}. [{opt.score:.2f}] {opt.reason}")

    lines.append("")
    lines.append("Reply with ONLY the letter and a one-sentence reason.")
    lines.append('Example: {"choice": "A", "reason": "Quality feedback improves my creative output"}')
    return "\n".join(lines)


def parse_selection(response: str, options: List[ActionOption]) -> Optional[ActionOption]:
    """Parse LLM selection response. Returns selected option or None."""
    import json as _json
    import re as _re

    # Try JSON parse first
    try:
        d = _json.loads(response)
        choice = d.get("choice", "").strip().upper()
    except (_json.JSONDecodeError, AttributeError):
        # Fallback: extract letter from text
        match = _re.search(r'["\']?([A-J])["\']?', response.strip()[:10])
        choice = match.group(1) if match else ""

    letters = "ABCDEFGHIJ"
    if choice and choice in letters:
        idx = letters.index(choice)
        if idx < len(options):
            return options[idx]

    # Fallback: return highest-scored option
    return options[0] if options else None

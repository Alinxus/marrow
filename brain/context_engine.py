"""Context selection for deep reasoning and decision quality."""

from __future__ import annotations

import logging
from typing import Any

from brain.digital_twin import get_active_workspace_summary
from brain.operator_profile import infer_operator_profile
from brain.world_model import get_world_context
from brain.knowledgebase import build_context as build_knowledge_context
from storage import db, state_store

log = logging.getLogger(__name__)


def _compact(text: str, limit: int = 400) -> str:
    return " ".join((text or "").split())[:limit]


def _screen_context(window_seconds: int = 12 * 60, limit: int = 8) -> str:
    shots = db.get_recent_screenshots(window_seconds, limit=limit)
    if not shots:
        return ""
    lines = ["[Relevant screen context]"]
    seen = set()
    for shot in shots[:limit]:
        app = (shot.get("app_name") or "").strip()
        title = (shot.get("window_title") or "").strip()
        focused = _compact(shot.get("focused_context") or "", 180)
        summary = _compact(shot.get("ocr_text") or "", 280)
        key = f"{app}|{title}|{summary[:80]}"
        if key in seen:
            continue
        seen.add(key)
        entry = " | ".join(part for part in [app, title, focused, summary] if part)
        if entry:
            lines.append(f"- {entry[:520]}")
    return "\n".join(lines[: limit + 1])


def _observation_context(limit: int = 8) -> str:
    obs = db.get_observations(limit=40)
    if not obs:
        return ""
    lines = ["[Relevant observations]"]
    for row in obs[:limit]:
        typ = (row.get("type") or "fact").strip()
        content = _compact(row.get("content") or "", 200)
        if content:
            lines.append(f"- ({typ}) {content}")
    return "\n".join(lines)


def _conversation_context(limit: int = 10) -> str:
    rows = list(reversed(db.get_recent_conversations(limit=limit)))
    if not rows:
        return ""
    lines = ["[Recent conversation]"]
    for row in rows[-limit:]:
        role = (row.get("role") or "user").strip()
        content = _compact(row.get("content") or "", 220)
        if content:
            lines.append(f"- {role}: {content}")
    return "\n".join(lines)


def _operator_context() -> str:
    profile = infer_operator_profile()
    signals = profile.get("adaptation_signals") or {}
    return "\n".join(
        [
            "[Operator profile]",
            f"initiative_style: {profile.get('initiative_style', 'balanced')}",
            f"initiative_tolerance: {profile.get('initiative_tolerance', 3)}",
            f"teaching_depth: {profile.get('teaching_depth', 'balanced')}",
            f"challenge_preference: {profile.get('challenge_preference', 'balanced')}",
            f"engagement_score: {signals.get('engagement_score', 0.5)}",
        ]
    )


def _scratchpad_context(session_id: str) -> tuple[dict[str, Any], str]:
    session = state_store.get_scratchpad_session(session_id)
    lines = [
        "[Scratchpad]",
        f"Title: {_compact(session.get('problem_title') or '', 160)}",
        f"Summary: {_compact(session.get('problem_summary') or '', 260)}",
        f"Project: {_compact(session.get('project_brief') or '', 260)}",
    ]
    for key in (
        "goals",
        "constraints",
        "assumptions",
        "unknowns",
        "blockers",
        "open_questions",
        "next_steps",
        "design_decisions",
    ):
        items = session.get(key) or []
        if isinstance(items, list) and items:
            vals = "; ".join(_compact(str(x), 90) for x in items[:4])
            lines.append(f"{key}: {vals}")
    return session, "\n".join(lines)


async def build_reasoning_context(
    user_text: str,
    context_hint: str = "",
    session_id: str = "default",
) -> dict[str, Any]:
    """Assemble selected context blocks for deep reasoning."""
    session, scratchpad = _scratchpad_context(session_id)
    blocks = []
    if context_hint:
        blocks.append("[Runtime context]\n" + context_hint[:1500])
    if scratchpad:
        blocks.append(scratchpad[:1500])
    twin = get_active_workspace_summary()
    if twin:
        blocks.append(twin[:1200])
    try:
        world = get_world_context()
    except Exception:
        world = ""
    if world:
        blocks.append(world[:1200])
    operator = _operator_context()
    if operator:
        blocks.append(operator[:800])
    screen = _screen_context()
    if screen:
        blocks.append(screen[:2200])
    convo = _conversation_context()
    if convo:
        blocks.append(convo[:1800])
    obs = _observation_context()
    if obs:
        blocks.append(obs[:1500])

    knowledge = ""
    try:
        knowledge = await build_knowledge_context(user_text[:200], session_id=session_id)
    except Exception as exc:
        log.debug(f"Context engine knowledge fetch failed: {exc}")
    if knowledge:
        blocks.append("[Knowledgebase]\n" + knowledge[:2200])

    return {
        "session": session,
        "assembled_context": "\n\n".join(blocks),
        "context_blocks": blocks,
        "screen_context": screen,
        "conversation_context": convo,
        "observation_context": obs,
        "memory_context": knowledge,
        "context_meta": {
            "blocks": len(blocks),
            "chars": sum(len(b) for b in blocks),
        },
    }

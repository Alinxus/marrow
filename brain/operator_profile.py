"""Adaptive operator profile for initiative, teaching, and execution style."""

from __future__ import annotations

import time
from typing import Any

from storage import db, state_store


def _count_recent(rows: list[dict], seconds: int) -> int:
    cutoff = time.time() - max(1, seconds)
    return sum(1 for row in rows if float(row.get("ts", 0) or 0) >= cutoff)


def _latest_ts(rows: list[dict]) -> float:
    return max((float(row.get("ts", 0) or 0) for row in rows), default=0.0)


def infer_operator_profile() -> dict[str, Any]:
    """Blend configured defaults with recent engagement signals."""
    profile = state_store.get_operator_profile().copy()

    interruptions = db.get_recent_interruptions(45 * 60)
    actions = db.get_recent_actions(40)
    conversations = db.get_recent_conversations(40)

    recent_interruptions = len(interruptions)
    recent_actions = _count_recent(actions, 20 * 60)
    recent_conversations = _count_recent(conversations, 20 * 60)
    last_interrupt_ts = _latest_ts(interruptions)
    last_user_ts = _latest_ts([row for row in conversations if (row.get("role") or "") == "user"])
    responded_after_interrupt = bool(last_interrupt_ts and last_user_ts and last_user_ts >= last_interrupt_ts)

    engagement = 0.45
    engagement += min(0.25, recent_actions * 0.06)
    engagement += min(0.25, recent_conversations * 0.05)
    if responded_after_interrupt:
        engagement += 0.15
    if recent_interruptions >= 4 and not responded_after_interrupt:
        engagement -= 0.18
    engagement = max(0.05, min(0.98, engagement))

    style = str(profile.get("initiative_style", "balanced") or "balanced").lower()
    tolerance = int(profile.get("initiative_tolerance", 3) or 3)
    if engagement >= 0.74:
        tolerance = min(5, tolerance + 1)
        if style == "quiet":
            style = "balanced"
    elif engagement <= 0.3:
        tolerance = max(1, tolerance - 1)
        if style == "aggressive":
            style = "balanced"

    adapted = profile | {
        "initiative_style": style,
        "initiative_tolerance": tolerance,
        "adaptation_signals": {
            "engagement_score": round(engagement, 2),
            "recent_interruptions": recent_interruptions,
            "recent_actions": recent_actions,
            "recent_conversations": recent_conversations,
            "responded_after_interrupt": responded_after_interrupt,
        },
        "updated_at": time.time(),
    }
    state_store.update_operator_profile(adapted)
    return adapted

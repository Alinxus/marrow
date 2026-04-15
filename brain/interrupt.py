"""
Interrupt Decision Engine.

Given a candidate message from the reasoning loop, decides whether to speak.

Scoring factors (in order applied):
  1. Urgency 5 bypass  — emergency, always speaks regardless of anything
  2. Meeting detection — active video call → soft-mute unless urgency ≥ 4
  3. Flow state        — deep work detected → raise cooldown threshold
  4. Cooldown          — time since last interruption
  5. Dedup             — word-overlap similarity against recent interruptions
  6. Minimum urgency   — urgency < 2 never speaks

Omi had FloatingBarNotification with rich metadata (sourceApp, windowTitle,
reasoning, screenshot). We carry equivalent richness in InterruptCandidate
and log it so the future UI layer can intercept it.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from storage import db

log = logging.getLogger(__name__)


@dataclass
class InterruptCandidate:
    message: str
    reasoning: str
    urgency: int              # 1-5
    source_app: str = ""      # what app triggered this
    context_snippet: str = "" # brief snippet of what was seen
    act: Optional[dict] = None  # {"task": ..., "context": ...} if action needed


class InterruptDecisionEngine:
    def __init__(self):
        self._last_spoken_at: float = 0.0

    def should_speak(self, candidate: InterruptCandidate) -> bool:
        now = time.time()

        # 1. Urgency 5 — unconditional (emergency / time-critical)
        if candidate.urgency >= 5:
            log.info("Urgency 5 — bypassing all checks")
            return True

        # 2. Meeting detection — active video call
        if _in_meeting():
            if candidate.urgency < 4:
                log.debug("Interrupt suppressed: active meeting (urgency < 4)")
                return False
            log.info("In meeting but urgency ≥ 4 — allowing")

        # 3. Flow state — deep focus
        if _in_flow_state():
            # In flow, raise the effective cooldown and minimum urgency
            if candidate.urgency < 3:
                log.debug("Interrupt suppressed: flow state (urgency < 3)")
                return False

        # 4. Cooldown
        seconds_since_last = now - self._last_spoken_at
        required_cooldown = config.INTERRUPT_COOLDOWN

        if candidate.urgency >= 4:
            required_cooldown = required_cooldown // 2   # high urgency: half cooldown
        elif _in_flow_state():
            required_cooldown = int(required_cooldown * 1.5)  # in flow: 1.5x cooldown

        if seconds_since_last < required_cooldown:
            remaining = int(required_cooldown - seconds_since_last)
            log.debug(f"Interrupt suppressed: cooldown ({remaining}s remaining)")
            return False

        # 5. Dedup — don't repeat something said in the last 10 minutes
        recent = db.get_recent_interruptions(window_seconds=600)
        for past in recent:
            if _is_similar(candidate.message, past["message"]):
                log.debug("Interrupt suppressed: too similar to recent message")
                return False

        # 6. Minimum urgency
        if candidate.urgency < 2:
            log.debug(f"Interrupt suppressed: urgency too low ({candidate.urgency})")
            return False

        return True

    def record_spoken(self, candidate: InterruptCandidate) -> None:
        self._last_spoken_at = time.time()
        db.insert_interruption(
            ts=self._last_spoken_at,
            message=candidate.message,
            reasoning=candidate.reasoning,
            urgency=candidate.urgency,
        )
        log.info(
            f"[{candidate.urgency}/5] Spoke: {candidate.message[:80]}"
            + (f" | app={candidate.source_app}" if candidate.source_app else "")
        )


# ─── State detectors ───────────────────────────────────────────────────────────

def _in_meeting() -> bool:
    """Check if a meeting app has been active recently."""
    recent_apps = db.get_recent_apps(window_seconds=120)
    return any(app in config.MEETING_APPS for app in recent_apps)


def _in_flow_state() -> bool:
    """
    Detect deep focus: a code editor / terminal has been the active window
    for the last 5 minutes without switching much.
    Simple heuristic: if a flow app is in recent screenshots and we haven't
    seen a meeting app, call it flow.
    """
    recent_apps = db.get_recent_apps(window_seconds=300)
    has_flow_app = any(app in config.FLOW_STATE_APPS for app in recent_apps)
    has_meeting = any(app in config.MEETING_APPS for app in recent_apps)
    return has_flow_app and not has_meeting


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _is_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """
    Word-overlap Jaccard similarity. Threshold 0.6 catches rephrased duplicates
    while allowing genuinely different messages through.
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return False
    intersection = len(words_a & words_b)
    shorter = min(len(words_a), len(words_b))
    return (intersection / shorter) >= threshold

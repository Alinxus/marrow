"""Buffered proactive mentor pipeline (ported pattern from Omi backend)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from collections import OrderedDict

import config
from brain.llm import get_client
from storage import db

log = logging.getLogger(__name__)


@dataclass
class BufferedMessage:
    text: str
    ts: float
    is_user: bool = True


class MessageBuffer:
    def __init__(self) -> None:
        self.messages: list[BufferedMessage] = []
        self.last_activity_ts: float = 0.0
        self.last_analysis_count: int = 0
        self.seen_keys: set[str] = set()
        self.silence_detected: bool = False
        self.words_after_silence: int = 0

    def ingest(self, transcripts: list[dict]) -> None:
        now = time.time()
        if (
            self.last_activity_ts
            and now - self.last_activity_ts > config.MENTOR_SILENCE_RESET_SECONDS
        ):
            self.messages.clear()
            self.last_analysis_count = 0
            self.seen_keys.clear()
            self.silence_detected = True
            self.words_after_silence = 0

        for t in sorted(transcripts, key=lambda r: float(r.get("ts", 0))):
            text = (t.get("text") or "").strip()
            if not text or len(text) < config.MENTOR_MIN_TRANSCRIPT_CHARS:
                continue
            ts = float(t.get("ts", 0) or now)
            key = f"{int(ts * 10)}:{text[:80].lower()}"
            if key in self.seen_keys:
                continue
            self.seen_keys.add(key)

            if self.silence_detected:
                self.words_after_silence += len(text.split())
                if self.words_after_silence >= config.MENTOR_MIN_WORDS_AFTER_SILENCE:
                    self.silence_detected = False

            if self.messages and abs(self.messages[-1].ts - ts) < 2.0:
                self.messages[-1].text += " " + text
                self.messages[-1].ts = ts
            else:
                self.messages.append(BufferedMessage(text=text, ts=ts, is_user=True))

        if len(self.messages) > config.MENTOR_MAX_BUFFER_MESSAGES:
            excess = len(self.messages) - config.MENTOR_MAX_BUFFER_MESSAGES
            self.messages = self.messages[excess:]
            self.last_analysis_count = max(0, self.last_analysis_count - excess)

        self.last_activity_ts = now

    def ready_for_analysis(self) -> bool:
        if self.silence_detected:
            return False
        new_count = len(self.messages) - self.last_analysis_count
        return new_count >= config.MENTOR_MIN_NEW_SEGMENTS_FOR_ANALYSIS

    def mark_analyzed(self) -> None:
        self.last_analysis_count = len(self.messages)


class SessionBufferManager:
    def __init__(self, max_sessions: int = 24):
        self._buffers: OrderedDict[str, MessageBuffer] = OrderedDict()
        self._max_sessions = max_sessions

    def get(self, session_id: str) -> MessageBuffer:
        sid = (session_id or "default")[:120]
        if sid in self._buffers:
            self._buffers.move_to_end(sid)
            return self._buffers[sid]
        buf = MessageBuffer()
        self._buffers[sid] = buf
        if len(self._buffers) > self._max_sessions:
            self._buffers.popitem(last=False)
        return buf

    def sessions(self) -> int:
        return len(self._buffers)


_session_buffers = SessionBufferManager()

_stats: dict[str, int | float] = {
    "runs": 0,
    "buffer_not_ready": 0,
    "rate_limited": 0,
    "daily_capped": 0,
    "gate_rejected": 0,
    "generate_rejected": 0,
    "critic_rejected": 0,
    "sent": 0,
    "last_sent_ts": 0.0,
}


FREQUENCY_TO_BASE_THRESHOLD = {
    1: 0.92,
    2: 0.85,
    3: 0.78,
    4: 0.70,
    5: 0.60,
}


def _parse_json_object(text: str) -> dict:
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return {}


def _format_conversation(messages: list[BufferedMessage]) -> str:
    lines = []
    for m in messages[-30:]:
        speaker = "User" if m.is_user else "Other"
        lines.append(f"[{speaker}] {m.text[:280]}")
    return "\n".join(lines) if lines else "No conversation in progress."


def _format_user_facts() -> str:
    obs = db.get_observations(limit=50)
    if not obs:
        return "No stored facts yet."
    lines = []
    for o in obs[:20]:
        lines.append(f"- ({o.get('type', 'fact')}) {(o.get('content') or '')[:180]}")
    return "\n".join(lines)


def _format_recent_notifications() -> str:
    rows = db.get_recent_interruptions(window_seconds=24 * 3600)
    if not rows:
        return "No recent proactive notifications."
    return "\n".join(f"- {(r.get('message') or '')[:120]}" for r in rows[:20])


async def _llm_json(prompt: str, max_tokens: int = 220) -> dict:
    llm = get_client()
    resp = await llm.create(
        messages=[{"role": "user", "content": prompt}],
        model_type="scoring",
        max_tokens=max_tokens,
    )
    return _parse_json_object(resp.text)


async def maybe_generate_mentor_signal() -> tuple[str, int] | None:
    return await maybe_generate_mentor_signal_for_session("default")


async def maybe_generate_mentor_signal_for_session(
    session_id: str,
) -> tuple[str, int] | None:
    _stats["runs"] = int(_stats["runs"]) + 1
    if not config.MENTOR_PROACTIVE_ENABLED:
        return None

    lane = "mentor_proactive"

    transcripts = db.get_recent_context(config.MENTOR_CONTEXT_WINDOW_SECONDS).get(
        "transcripts", []
    )
    buffer = _session_buffers.get(session_id)
    buffer.ingest(transcripts)
    if not buffer.ready_for_analysis():
        _stats["buffer_not_ready"] = int(_stats["buffer_not_ready"]) + 1
        db.insert_proactive_decision(
            lane=lane,
            stage="buffer",
            status="skip",
            reason="not_ready",
            payload=f"session={session_id[:60]}",
        )
        return None

    buffer.mark_analyzed()

    # Rate limit + daily cap
    last_age = db.get_last_interruption_age_seconds()
    if last_age is not None and last_age < config.MENTOR_RATE_LIMIT_SECONDS:
        _stats["rate_limited"] = int(_stats["rate_limited"]) + 1
        db.insert_proactive_decision(
            lane=lane,
            stage="rate_limit",
            status="skip",
            reason="cooldown",
            score=last_age,
        )
        return None
    if (
        db.count_interruptions_since(time.time() - 86400)
        >= config.MENTOR_MAX_DAILY_NOTIFICATIONS
    ):
        _stats["daily_capped"] = int(_stats["daily_capped"]) + 1
        db.insert_proactive_decision(
            lane=lane,
            stage="rate_limit",
            status="skip",
            reason="daily_cap",
        )
        return None

    threshold = FREQUENCY_TO_BASE_THRESHOLD.get(config.PROACTIVE_FREQUENCY, 0.78)
    user_facts = _format_user_facts()
    convo = _format_conversation(buffer.messages)
    recent_notifications = _format_recent_notifications()

    gate_prompt = f"""Decide if the current live conversation is worth interrupting.
Return strict JSON: {{"is_relevant": true|false, "relevance_score": 0.0-1.0, "reasoning": "short"}}

Rules:
- Default false.
- True only for specific, actionable, non-obvious guidance that changes next action now.
- Reject repetitive topics similar to recent notifications.

User facts:
{user_facts}

Current conversation:
{convo}

Recent notifications:
{recent_notifications}
"""
    t0 = time.time()
    gate = await _llm_json(gate_prompt, max_tokens=160)
    gate_ms = (time.time() - t0) * 1000.0
    is_relevant = bool(gate.get("is_relevant", False))
    score = float(gate.get("relevance_score", 0.0) or 0.0)
    if not is_relevant or score < threshold:
        _stats["gate_rejected"] = int(_stats["gate_rejected"]) + 1
        db.insert_proactive_decision(
            lane=lane,
            stage="gate",
            status="reject",
            score=score,
            latency_ms=gate_ms,
            reason=(gate.get("reasoning") or "")[:200],
        )
        return None

    db.insert_proactive_decision(
        lane=lane,
        stage="gate",
        status="pass",
        score=score,
        latency_ms=gate_ms,
        reason=(gate.get("reasoning") or "")[:200],
    )

    gen_prompt = f"""Generate ONE proactive notification from this conversation.
Return strict JSON: {{"notification_text":"<max 140 chars>", "confidence": 0.0-1.0, "reasoning":"short"}}

Must be concrete, actionable, and specific to this exact conversation.
No generic coaching. No filler.

Gate reasoning:
{(gate.get("reasoning") or "")[:220]}

User facts:
{user_facts}

Current conversation:
{convo}
"""
    t1 = time.time()
    draft = await _llm_json(gen_prompt, max_tokens=200)
    gen_ms = (time.time() - t1) * 1000.0
    text = str(draft.get("notification_text", "")).strip()
    confidence = float(draft.get("confidence", 0.0) or 0.0)
    if not text or confidence < threshold:
        _stats["generate_rejected"] = int(_stats["generate_rejected"]) + 1
        db.insert_proactive_decision(
            lane=lane,
            stage="generate",
            status="reject",
            score=confidence,
            latency_ms=gen_ms,
            reason=(draft.get("reasoning") or "")[:200],
        )
        return None

    db.insert_proactive_decision(
        lane=lane,
        stage="generate",
        status="pass",
        score=confidence,
        latency_ms=gen_ms,
        reason=(draft.get("reasoning") or "")[:200],
        payload=text[:220],
    )

    critic_prompt = f"""Final critic. Should this interruption be sent now?
Return strict JSON: {{"approved": true|false, "why":"short"}}

Candidate: {text}
Reasoning: {(draft.get("reasoning") or "")[:240]}
Conversation:
{convo}

Reject if obvious, repetitive, or not actionable now.
"""
    t2 = time.time()
    verdict = await _llm_json(critic_prompt, max_tokens=120)
    critic_ms = (time.time() - t2) * 1000.0
    if not bool(verdict.get("approved", False)):
        _stats["critic_rejected"] = int(_stats["critic_rejected"]) + 1
        db.insert_proactive_decision(
            lane=lane,
            stage="critic",
            status="reject",
            latency_ms=critic_ms,
            reason=(verdict.get("why") or "")[:200],
            payload=text[:220],
        )
        return None

    db.insert_proactive_decision(
        lane=lane,
        stage="critic",
        status="pass",
        latency_ms=critic_ms,
        reason=(verdict.get("why") or "")[:200],
        payload=text[:220],
    )

    urgency = 4 if confidence >= 0.88 else 3
    _stats["sent"] = int(_stats["sent"]) + 1
    _stats["last_sent_ts"] = time.time()
    db.insert_proactive_decision(
        lane=lane,
        stage="send",
        status="sent",
        score=confidence,
        payload=text[:220],
    )
    return text[:220], urgency


def get_mentor_proactive_stats() -> dict:
    default_buf = _session_buffers.get("default")
    return {
        "runs": int(_stats["runs"]),
        "buffer_not_ready": int(_stats["buffer_not_ready"]),
        "rate_limited": int(_stats["rate_limited"]),
        "daily_capped": int(_stats["daily_capped"]),
        "gate_rejected": int(_stats["gate_rejected"]),
        "generate_rejected": int(_stats["generate_rejected"]),
        "critic_rejected": int(_stats["critic_rejected"]),
        "sent": int(_stats["sent"]),
        "last_sent_ts": float(_stats["last_sent_ts"]),
        "buffer_messages": len(default_buf.messages),
        "silence_detected": bool(default_buf.silence_detected),
        "sessions": _session_buffers.sessions(),
    }

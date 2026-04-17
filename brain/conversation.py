"""Fast conversational mode for low-latency back-and-forth dialogue."""

from __future__ import annotations

import asyncio
import logging
import re
import time

import config

log = logging.getLogger(__name__)

_active_until: float = 0.0
_history: list[dict] = []
_lock = asyncio.Lock()

_EXIT_PATTERNS = [
    "thanks",
    "thank you",
    "that's all",
    "that is all",
    "done",
    "stop listening",
    "goodbye",
    "bye",
]


def _now() -> float:
    return time.time()


def _trim_history() -> None:
    max_turns = max(2, int(config.CONVERSATION_MAX_TURNS))
    # each turn is two messages (user+assistant)
    keep_msgs = max_turns * 2
    if len(_history) > keep_msgs:
        del _history[: len(_history) - keep_msgs]


def activate_session() -> None:
    global _active_until
    _active_until = _now() + max(10, int(config.CONVERSATION_MODE_TIMEOUT_SECONDS))


def touch_session() -> None:
    if is_active():
        activate_session()


def end_session() -> None:
    global _active_until
    _active_until = 0.0


def is_active() -> bool:
    return _now() < _active_until


def remaining_seconds() -> int:
    if not is_active():
        return 0
    return max(0, int(_active_until - _now()))


def extract_wake_query(text: str) -> str:
    """Extract query after wake phrase, e.g. 'hey marrow open spotify'."""
    t = (text or "").strip()
    low = t.lower()
    for ww in config.WAKE_WORDS:
        ww_low = ww.lower()
        if low == ww_low:
            return ""
        if low.startswith(ww_low + " "):
            return t[len(ww) :].strip()
    return t


def _is_exit_utterance(text: str) -> bool:
    low = (text or "").lower().strip()
    return any(p in low for p in _EXIT_PATTERNS)


async def handle_turn(user_text: str, context_hint: str = "") -> str:
    """Fast conversational turn; keeps short session memory."""
    from brain.llm import get_client

    user_text = (user_text or "").strip()
    if not user_text:
        return ""

    if _is_exit_utterance(user_text):
        end_session()
        return "Got it. I'll stay quiet until you call me again."

    activate_session()

    system = (
        "You are Marrow in live conversation mode."
        "Be brief, natural, and helpful."
        "Prefer 1-2 short sentences unless user asked for detail."
        "If an action is requested, say you'll do it and keep response concise."
        "No internal jargon."
    )

    async with _lock:
        llm = get_client()
        msgs = list(_history)
        if context_hint:
            msgs.append(
                {
                    "role": "user",
                    "content": f"[Current context]\n{context_hint[:550]}\n\nUser: {user_text}",
                }
            )
        else:
            msgs.append({"role": "user", "content": user_text})

        response = await llm.create(
            messages=msgs,
            system=system,
            max_tokens=config.CONVERSATION_MAX_TOKENS,
            model_type="scoring",
        )
        text = (response.text or "").strip()

        if not text:
            text = "I'm here. What do you want me to do?"

        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": text})
        _trim_history()
        return text

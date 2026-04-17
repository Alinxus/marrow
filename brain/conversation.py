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
_recent_refs: dict[str, str] = {
    "tab": "",
    "window": "",
    "app": "",
    "file": "",
    "person": "",
}
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
    return ""


def note_reference(kind: str, value: str) -> None:
    kind = (kind or "").strip().lower()
    value = (value or "").strip()
    if kind in _recent_refs and value:
        _recent_refs[kind] = value


def _refresh_references_from_context(context_hint: str) -> None:
    lines = [line.strip() for line in (context_hint or "").splitlines() if line.strip()]
    for line in lines[:8]:
        if line.startswith("[") and line.endswith("]"):
            note_reference("app", line.strip("[]"))
        elif "." in line and any(
            ext in line.lower() for ext in (".py", ".ts", ".js", ".md", ".txt", ".json")
        ):
            note_reference("file", line)
        elif "http" in line or "www." in line:
            note_reference("tab", line)
        elif not _recent_refs.get("window"):
            note_reference("window", line[:120])


def _resolve_followup_references(text: str) -> str:
    updated = text
    replacements = {
        "that tab": _recent_refs.get("tab") or _recent_refs.get("window"),
        "that window": _recent_refs.get("window") or _recent_refs.get("app"),
        "that app": _recent_refs.get("app"),
        "that file": _recent_refs.get("file") or _recent_refs.get("window"),
        "same person": _recent_refs.get("person"),
    }
    for needle, replacement in replacements.items():
        if replacement and needle in updated.lower():
            updated = re.sub(
                re.escape(needle), replacement, updated, flags=re.IGNORECASE
            )
    return updated


def _is_exit_utterance(text: str) -> bool:
    low = (text or "").lower().strip()
    return any(p in low for p in _EXIT_PATTERNS)


def _is_affirmative(text: str) -> bool:
    low = (text or "").lower().strip()
    return low in {
        "yes",
        "yep",
        "yeah",
        "sure",
        "ok",
        "okay",
        "do it",
        "go ahead",
        "proceed",
    }


def _last_assistant_question() -> str:
    for row in reversed(_history):
        if row.get("role") == "assistant":
            content = (row.get("content") or "").strip()
            if "?" in content:
                return content[:260]
    return ""


def _style_instruction() -> str:
    style = (
        getattr(config, "CONVERSATION_RESPONSE_STYLE", "balanced") or "balanced"
    ).lower()
    if style == "short":
        return "Keep every answer to one short sentence unless the user explicitly asks for detail."
    if style == "detailed":
        return "Give complete but crisp answers in 2-4 sentences with concrete next steps when useful."
    return "Keep it natural and concise, usually 1-3 sentences, with specifics over generic filler."


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
    _refresh_references_from_context(context_hint)
    user_text = _resolve_followup_references(user_text)

    if _is_affirmative(user_text):
        last_q = _last_assistant_question()
        if last_q:
            user_text = (
                "The user confirmed YES to your previous question. "
                "Do not repeat the question. Proceed with the implied next step now. "
                f"Previous question: {last_q}"
            )

    system = " ".join(
        [
            "You are Marrow in live conversation mode.",
            "Be natural, fast, and context-aware.",
            _style_instruction(),
            "Answer directly first, then ask at most one clarifying question only when blocked.",
            "Do not repeat the user's question back to them.",
            "Do not repeat your own previous question after the user says yes.",
            "Use concrete details from current context and prior observed history when relevant.",
            "Never claim you are not watching the screen unless the context explicitly says screen data is stale.",
            "No internal jargon.",
        ]
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

"""High-signal context extraction from screenshots.

Adds two classes of durable signals:
1) Contact interaction events (outgoing/incoming email/chat)
2) High-risk claim events from media feeds (misinfo-sensitive topics)
"""

import re

from storage import db

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TO_NAME_RE = re.compile(r"\bto\s*:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})")

_MAIL_APPS = {"outlook", "thunderbird", "mail", "spark", "superhuman"}
_CHAT_APPS = {"slack", "discord", "teams", "telegram", "whatsapp", "signal"}
_MEETING_APPS = {"zoom", "teams", "meet", "webex", "discord", "slack"}
_MEDIA_APPS = {
    "chrome",
    "msedge",
    "firefox",
    "brave",
    "safari",
    "youtube",
    "x",
    "reddit",
}

_TOPIC_FACTS = {
    "epstein": "Jeffrey Epstein is officially reported deceased (2019).",
    "jeffrey epstein": "Jeffrey Epstein is officially reported deceased (2019).",
}

_CLAIM_PATTERNS = [
    re.compile(r"\b(is|was)\s+alive\b", re.IGNORECASE),
    re.compile(r"\bfake\b", re.IGNORECASE),
    re.compile(r"\bhoax\b", re.IGNORECASE),
    re.compile(r"\bcover\s?up\b", re.IGNORECASE),
    re.compile(r"\bnot\s+dead\b", re.IGNORECASE),
]

_MEETING_PRESENCE_PATTERNS = [
    re.compile(r"\b(participant|participants)\b", re.IGNORECASE),
    re.compile(r"\bjoined\b", re.IGNORECASE),
    re.compile(r"\bwaiting room\b", re.IGNORECASE),
    re.compile(r"\bwas added\b", re.IGNORECASE),
    re.compile(r"\bconnected\b", re.IGNORECASE),
]


def _extract_contact(text: str) -> str:
    m = _EMAIL_RE.search(text)
    if m:
        return m.group(0).lower()
    n = _TO_NAME_RE.search(text)
    if n:
        return n.group(1).strip().lower()
    return ""


def _channel_for_app(app_name: str, title: str) -> str:
    app = (app_name or "").lower()
    t = (title or "").lower()
    if app in _MAIL_APPS or "gmail" in t or "outlook" in t or "inbox" in t:
        return "email"
    if app in _CHAT_APPS:
        return "chat"
    return ""


def _record_contact_signal(ts: float, app_name: str, title: str, ocr_text: str) -> None:
    if not ocr_text:
        return

    channel = _channel_for_app(app_name, title)
    if not channel:
        return

    text = ocr_text.lower()
    contact = _extract_contact(ocr_text)
    if not contact:
        return

    direction = "outgoing"
    action = "draft"
    confidence = 0.55

    if any(k in text for k in ["message sent", "sent", "your message has been sent"]):
        action = "sent"
        confidence = 0.86
    elif "reply" in text or "re:" in text:
        action = "reply"
        confidence = 0.72
    elif any(k in text for k in ["from:", "new message from", "unread"]):
        direction = "incoming"
        action = "received"
        confidence = 0.75

    db.insert_contact_interaction(
        ts=ts,
        contact=contact,
        channel=channel,
        direction=direction,
        action=action,
        source_app=(app_name or "").lower(),
        evidence=ocr_text[:500],
        confidence=confidence,
    )


def _record_claim_signal(ts: float, app_name: str, title: str, ocr_text: str) -> None:
    if not ocr_text:
        return
    app = (app_name or "").lower()
    if app not in _MEDIA_APPS and "youtube" not in (title or "").lower():
        return

    text = ocr_text.lower()
    topic = ""
    for t in _TOPIC_FACTS.keys():
        if t in text:
            topic = t
            break
    if not topic:
        return

    if not any(p.search(text) for p in _CLAIM_PATTERNS):
        return

    claim = ocr_text[:220]
    verdict = f"Likely false or misleading. {_TOPIC_FACTS[topic]}"
    db.insert_claim_event(
        ts=ts,
        topic=topic,
        claim=claim,
        verdict=verdict,
        source_app=app,
        evidence=ocr_text[:550],
        confidence=0.82,
    )


def _record_meeting_presence_signal(
    ts: float, app_name: str, title: str, ocr_text: str, focused_context: str
) -> None:
    if not ocr_text and not focused_context:
        return

    app = (app_name or "").lower()
    combined = f"{title}\n{focused_context}\n{ocr_text}".lower()
    if app not in _MEETING_APPS and not any(
        k in combined for k in ["zoom", "meeting", "call"]
    ):
        return

    if not any(p.search(combined) for p in _MEETING_PRESENCE_PATTERNS):
        return

    evidence = (f"{title} | {focused_context} | {ocr_text}")[:500]
    # Store as durable observation so world model can reference it.
    db.insert_observation(
        "meeting_presence_signal",
        f"Possible additional participant/presence change detected in active call. Evidence: {evidence}",
        source="screen",
    )


def process_screen_signals(
    ts: float,
    app_name: str,
    title: str,
    ocr_text: str,
    focused_context: str = "",
) -> None:
    """Extract high-signal context events from the latest screenshot OCR."""
    _record_contact_signal(ts, app_name, title, ocr_text)
    _record_claim_signal(ts, app_name, title, ocr_text)
    _record_meeting_presence_signal(ts, app_name, title, ocr_text, focused_context)


def build_high_signal_context() -> str:
    """Build compact latent context block for reasoning loop."""
    lines = []

    pressure = db.get_contact_pressure_signals(window_days=14, limit=5)
    if pressure:
        lines.append("=== LONG-HORIZON CONTEXT ===")
        for r in pressure:
            outgoing = int(r.get("outgoing") or 0)
            incoming = int(r.get("incoming") or 0)
            if outgoing >= 3 and outgoing > incoming:
                lines.append(
                    f"- Interaction pattern: {r['contact']} has {outgoing} outgoing vs {incoming} incoming in 14 days."
                )

    claims = db.get_recent_claim_events(window_hours=24, limit=5)
    if claims:
        if not lines:
            lines.append("=== LONG-HORIZON CONTEXT ===")
        for c in claims:
            lines.append(
                f"- Potentially misleading media claim observed about {c['topic']}: {c['claim'][:120]}"
            )
            if c.get("verdict"):
                lines.append(f"  background fact: {c['verdict'][:120]}")

    meeting_presence = db.get_observations_by_type("meeting_presence_signal", limit=4)
    if meeting_presence:
        if not lines:
            lines.append("=== LONG-HORIZON CONTEXT ===")
        for m in meeting_presence[:3]:
            lines.append(f"- Live meeting context shift: {m['content'][:160]}")

    return "\n".join(lines)

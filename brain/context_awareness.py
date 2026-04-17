"""High-signal context extraction from screenshots and transcripts.

Three classes of durable signals:
1) Contact interaction events — outgoing/incoming email/chat, plus apology/pressure detection
2) Claim events — any factual claim on screen or in audio (queued for web verification)
3) Meeting presence signals — participants joining/leaving video calls

Unlike the old version which hardcoded specific topics (e.g. "epstein"),
claim detection now covers ANY verifiable factual assertion via the
claim_verifier pipeline.
"""

import re
import time
import asyncio
import hashlib

from storage import db

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TO_NAME_RE = re.compile(r"\bto\s*:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})")

_MAIL_APPS = {"outlook", "thunderbird", "mail", "spark", "superhuman"}
_CHAT_APPS = {"slack", "discord", "teams", "telegram", "whatsapp", "signal"}
_MEETING_APPS = {
    "zoom",
    "teams",
    "meet",
    "webex",
    "discord",
    "slack",
    "facetime",
    "skype",
}
_MEDIA_APPS = {
    "chrome",
    "msedge",
    "firefox",
    "brave",
    "safari",
    "youtube",
    "vlc",
    "mpv",
    "iina",
    "x",
    "reddit",
    "twitter",
}

# Apology / concession patterns — detect user writing an apology to someone
_APOLOGY_PATTERNS = [
    re.compile(
        r"\b(sorry|apologize|apologies|forgive me|my fault|i was wrong)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(sincerely apologize|deeply sorry|truly sorry)\b", re.IGNORECASE),
]

# Claim-like language in media/audio — any strong factual assertion
_STRONG_CLAIM_PATTERNS = [
    re.compile(
        r"\b(is|was|are|were)\s+(alive|dead|fake|real|innocent|guilty)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(they\s+lied|cover[\s-]?up|government\s+hid|secret\s+deal)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(never\s+happened|didn[''']t\s+happen|was\s+fabricated)\b", re.IGNORECASE
    ),
    re.compile(r"\b(proven|confirmed|leaked|revealed|exposed)\s+that\b", re.IGNORECASE),
    re.compile(
        r"\b(the\s+truth\s+is|the\s+real\s+story|what\s+they\s+don[''']t\s+tell\s+you)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcauses?\s+(cancer|autism|death|disease)\b", re.IGNORECASE),
    re.compile(r"\b(did|didn[''']t)\s+kill\b", re.IGNORECASE),
    re.compile(r"\bstill\s+alive\b", re.IGNORECASE),
    re.compile(r"\bwas\s+murdered\b", re.IGNORECASE),
]

_MEETING_PRESENCE_PATTERNS = [
    re.compile(r"\b(participant|participants)\b", re.IGNORECASE),
    re.compile(r"\bjoined\b", re.IGNORECASE),
    re.compile(r"\bwaiting room\b", re.IGNORECASE),
    re.compile(r"\bwas added\b", re.IGNORECASE),
    re.compile(r"\bconnected\b", re.IGNORECASE),
    re.compile(r"\d+\s+participants?\b", re.IGNORECASE),
    re.compile(r"\bsomeone\s+(joined|entered|appeared)\b", re.IGNORECASE),
]

# Vision model OCR phrases that indicate a person is visible in a video call frame
# These come from the LLM vision description, not raw text on screen
_VIDEO_FACE_PATTERNS = [
    re.compile(
        r"\b(person|man|woman|individual)\s+(visible|appearing|shown|seen|in\s+(the\s+)?video)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bvideo\s+(tile|feed|frame|call|thumbnail)\s+(showing|with|of)\s+(a\s+)?(person|man|woman|face)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(face|head|upper\s+body)\s+visible\s+in\b", re.IGNORECASE),
    re.compile(
        r"\b(another|second|third)\s+(person|participant|individual)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(background|behind)\s+.{0,30}\s+(person|man|woman|someone)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(someone|a\s+person)\s+(is\s+)?(in\s+the\s+)?(background|visible|frame|camera)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d\s+people\s+(visible|in\s+the\s+call|on\s+screen|on\s+camera)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(camera|webcam)\s+shows?\s+(a\s+)?(person|man|woman|face|someone)\b",
        re.IGNORECASE,
    ),
]

# Dedup: track last observation timestamps to avoid spamming
_last_face_signal_ts: float = 0.0
_FACE_DEDUP_SECS = 120  # only record a new face observation every 2 min

# Task/deadline/intent extraction (strong actionable context)
_TASK_PATTERNS = [
    re.compile(
        r"\b(todo|to do|next step|action item|follow up|follow-up)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(ship|deploy|merge|fix|refactor|implement|review|reply|send|schedule)\b",
        re.IGNORECASE,
    ),
]
_DEADLINE_PATTERNS = [
    re.compile(
        r"\b(due\s+today|due\s+tomorrow|deadline|by\s+\d{1,2}(:\d{2})?\s*(am|pm)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(eod|end\s+of\s+day|this\s+week|by\s+friday|urgent|asap)\b", re.IGNORECASE
    ),
]
_DECISION_PATTERNS = [
    re.compile(
        r"\b(should\s+we|should\s+i|decide|decision|pick\s+one|option\s+a|option\s+b|trade[-\s]?off)\b",
        re.IGNORECASE,
    ),
]

_last_memory_mirror_ts: dict[str, float] = {}
_MIRROR_DEDUP_SECS = 180


def _extract_contact(text: str) -> str:
    m = _EMAIL_RE.search(text)
    if m:
        return m.group(0).lower()
    n = _TO_NAME_RE.search(text)
    if n:
        return n.group(1).strip().lower()
    return ""


def _mirror_to_retaindb(obs_type: str, content: str, source: str) -> None:
    """Fire-and-forget mirror of high-signal context into RetainDB extraction lane."""
    try:
        key = hashlib.md5(f"{obs_type}:{content[:180]}".encode("utf-8")).hexdigest()
        now = time.time()
        last = _last_memory_mirror_ts.get(key, 0.0)
        if last and now - last < _MIRROR_DEDUP_SECS:
            return
        _last_memory_mirror_ts[key] = now

        from actions.memory import memory_record_observation

        async def _push() -> None:
            try:
                await memory_record_observation(
                    content, obs_type=obs_type, source=source
                )
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                asyncio.create_task(_push())
        except RuntimeError:
            pass
    except Exception:
        pass


def _record_observation_with_memory(
    obs_type: str, content: str, source: str = "screen"
) -> None:
    inserted = db.insert_observation(obs_type, content, source=source)
    if inserted:
        _mirror_to_retaindb(obs_type, content, source)


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

    # Detect apology drafts — flag as observation so reasoning can notice
    if direction == "outgoing" and action in ("draft", "reply"):
        if any(p.search(ocr_text) for p in _APOLOGY_PATTERNS):
            _record_observation_with_memory(
                "apology_draft",
                f"User appears to be drafting an apology to {contact} via {channel}.",
                source="screen",
            )


def _record_claim_signal(
    ts: float,
    app_name: str,
    title: str,
    ocr_text: str,
    transcript_text: str = "",
) -> None:
    """
    Detect any strong factual claim in screen OCR or audio transcripts.
    Queues it for web verification via claim_verifier pipeline.
    No hardcoded topics — covers anything.
    """
    app = (app_name or "").lower()
    title_lower = (title or "").lower()

    # Claims in audio are high-value (user is hearing a claim)
    audio_claim = bool(transcript_text) and any(
        p.search(transcript_text) for p in _STRONG_CLAIM_PATTERNS
    )

    # Claims on screen are only signal if in media/browser context
    screen_claim = (
        bool(ocr_text)
        and (app in _MEDIA_APPS or "youtube" in title_lower or "watch" in title_lower)
        and any(p.search(ocr_text) for p in _STRONG_CLAIM_PATTERNS)
    )

    if not audio_claim and not screen_claim:
        return

    claim_text = transcript_text if audio_claim else ocr_text
    source = "audio" if audio_claim else "screen"

    # Queue for async verification (non-blocking)
    try:
        import asyncio
        from brain.claim_verifier import detect_claims_from_context

        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        if loop and loop.is_running():
            asyncio.create_task(
                detect_claims_from_context(
                    ocr_text=ocr_text if source == "screen" else "",
                    transcript_text=transcript_text if source == "audio" else "",
                    source=source,
                )
            )
        else:
            # Store raw signal for next reasoning cycle to pick up
            _record_observation_with_memory(
                "claim_signal_pending",
                f"Possible claim in {source}: {claim_text[:200]}",
                source=source,
            )
    except Exception:
        pass


def _record_meeting_presence_signal(
    ts: float, app_name: str, title: str, ocr_text: str, focused_context: str
) -> None:
    global _last_face_signal_ts

    if not ocr_text and not focused_context:
        return

    app = (app_name or "").lower()
    combined = f"{title}\n{focused_context}\n{ocr_text}".lower()
    in_call_app = app in _MEETING_APPS or any(
        k in combined
        for k in ["zoom", "meeting", "call", "facetime", "webex", "google meet"]
    )

    # Standard participant text patterns
    if in_call_app and any(p.search(combined) for p in _MEETING_PRESENCE_PATTERNS):
        evidence = f"{title} | {focused_context} | {ocr_text}"[:500]
        _record_observation_with_memory(
            "meeting_presence_signal",
            f"Possible additional participant/presence change detected in active call. Evidence: {evidence}",
            source="screen",
        )

    # Vision-model face detection: look for person-in-video phrases in the LLM OCR output
    # The vision model describes what it sees, so these patterns appear in ocr_text
    if in_call_app and ocr_text and (ts - _last_face_signal_ts) > _FACE_DEDUP_SECS:
        face_matches = [p for p in _VIDEO_FACE_PATTERNS if p.search(ocr_text)]
        if len(face_matches) >= 1:
            _last_face_signal_ts = ts
            # Extract a short excerpt for context
            excerpt = ocr_text[:300].replace("\n", " ")
            _record_observation_with_memory(
                "video_call_face_detected",
                (
                    f"Vision model detected a person visible in video call frame "
                    f"(app: {app_name}, title: {title[:60]}). "
                    f"Description: {excerpt}"
                ),
                source="screen",
            )


def _record_intent_and_deadline_signals(
    ts: float,
    app_name: str,
    title: str,
    ocr_text: str,
    transcript_text: str = "",
) -> None:
    combined = f"{title}\n{ocr_text}\n{transcript_text}".strip()
    if not combined:
        return

    if any(p.search(combined) for p in _TASK_PATTERNS):
        _record_observation_with_memory(
            "task_signal",
            f"Likely actionable task detected in {app_name}: {combined[:220]}",
            source="screen",
        )

    if any(p.search(combined) for p in _DEADLINE_PATTERNS):
        _record_observation_with_memory(
            "deadline_signal",
            f"Possible deadline/urgency signal in {app_name}: {combined[:220]}",
            source="screen",
        )

    if any(p.search(combined) for p in _DECISION_PATTERNS):
        _record_observation_with_memory(
            "decision_signal",
            f"Possible decision point detected in {app_name}: {combined[:220]}",
            source="screen",
        )


def process_screen_signals(
    ts: float,
    app_name: str,
    title: str,
    ocr_text: str,
    focused_context: str = "",
    transcript_text: str = "",
) -> None:
    """Extract high-signal context events from the latest screenshot OCR and audio transcript."""
    _record_contact_signal(ts, app_name, title, ocr_text)
    _record_claim_signal(ts, app_name, title, ocr_text, transcript_text)
    _record_meeting_presence_signal(ts, app_name, title, ocr_text, focused_context)
    _record_intent_and_deadline_signals(ts, app_name, title, ocr_text, transcript_text)


def build_high_signal_context() -> str:
    """Build compact latent context block for reasoning loop."""
    lines = []

    # Communication pressure — repeated outgoing with no response
    pressure = db.get_contact_pressure_signals(window_days=14, limit=5)
    if pressure:
        lines.append("=== LONG-HORIZON CONTEXT ===")
        for r in pressure:
            outgoing = int(r.get("outgoing") or 0)
            incoming = int(r.get("incoming") or 0)
            if outgoing >= 2 and outgoing > incoming:
                ratio = f"{outgoing} sent, {incoming} received"
                lines.append(
                    f"- Communication pressure: {r['contact']} — {ratio} in 14 days (no reply)."
                )

    # Apology drafts — user writing apology to someone who ignored them
    apology_obs = db.get_observations_by_type("apology_draft", limit=3)
    if apology_obs:
        if not lines:
            lines.append("=== LONG-HORIZON CONTEXT ===")
        for a in apology_obs:
            lines.append(f"- {a['content']}")

    # Verified claim results — web-checked facts
    claims = db.get_recent_claim_events(window_hours=2, limit=5)
    if claims:
        if not lines:
            lines.append("=== LONG-HORIZON CONTEXT ===")
        for c in claims:
            lines.append(
                f'- Claim detected [{c.get("source_app", "media")}]: "{c["claim"][:100]}"'
            )
            if c.get("verdict"):
                lines.append(f"  Verdict: {c['verdict'][:200]}")

    # Meeting presence shifts
    meeting_presence = db.get_observations_by_type("meeting_presence_signal", limit=3)
    if meeting_presence:
        if not lines:
            lines.append("=== LONG-HORIZON CONTEXT ===")
        for m in meeting_presence[:2]:
            lines.append(f"- Live meeting context shift: {m['content'][:160]}")

    # Video call face detection — person visible in a video call frame
    face_obs = db.get_observations_by_type("video_call_face_detected", limit=2)
    if face_obs:
        if not lines:
            lines.append("=== LONG-HORIZON CONTEXT ===")
        for f in face_obs[:2]:
            lines.append(f"- {f['content'][:220]}")

    return "\n".join(lines)

"""
Proactive intelligence loop — the Jarvis heartbeat.

Runs every 60 seconds independent of screen changes. Does the things a
pure-reactive system misses:

1. Calendar proximity — "You have a standup in 9 minutes."
2. Focus state tracking — detects flow, elevates interrupt threshold,
   debriefs on exit: "You focused for 47 minutes. 3 Slacks from Alex."
3. Distraction detection — "You've been on Twitter for 21 minutes. Deadline today."
4. Deadline proximity — surfaces known deadlines from world model as they approach
5. Time-of-day nudges — end-of-day wrap-up, lunch break detection

Unlike the reactive reasoning loop (triggered by screen deltas), this loop
is purely time-driven. It emits:
  - Direct bridge toasts for time-critical alerts (calendar, focus exit)
  - DB observations for the reasoning loop to incorporate into context
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import config
from storage import db

log = logging.getLogger(__name__)

# ─── App taxonomy ──────────────────────────────────────────────────────────────

_PRODUCTIVE_APPS = {
    # Editors / IDEs
    "code",
    "cursor",
    "vim",
    "nvim",
    "emacs",
    "pycharm",
    "intellij",
    "webstorm",
    "goland",
    "rider",
    "clion",
    "rubymine",
    "datagrip",
    "xcode",
    "androidstudio",
    "eclipse",
    "netbeans",
    "sublime_text",
    "notepad++",
    "brackets",
    "zed",
    # Writing
    "word",
    "pages",
    "notion",
    "obsidian",
    "logseq",
    "typora",
    "scrivener",
    "bear",
    "ulysses",
    # Design
    "figma",
    "sketch",
    "photoshop",
    "illustrator",
    "affinity",
    "lightroom",
    "premiere",
    "davinci resolve",
    "final cut pro",
    # Terminal / shell
    "terminal",
    "iterm2",
    "iterm",
    "wt",
    "powershell",
    "alacritty",
    "kitty",
    "hyper",
    "warp",
    # Spreadsheets / docs
    "excel",
    "numbers",
    "sheets",
    "google docs",
    "airtable",
}

_DISTRACTION_APPS = {
    "x",
    "twitter",
    "reddit",
    "youtube",
    "netflix",
    "tiktok",
    "hulu",
    "instagram",
    "facebook",
    "twitch",
    "snapchat",
    "chess",
    "steam",
    "epic games",
    "roblox",
}

# ─── State ────────────────────────────────────────────────────────────────────

_state = {
    # Focus / flow
    "in_flow": False,
    "flow_app": "",
    "flow_start_ts": 0.0,
    "last_focus_debrief_ts": 0.0,
    # Distraction tracking
    "distraction_warned": {},  # app_name → last warn timestamp
    # Calendar
    "last_calendar_fetch_ts": 0.0,
    "alerted_events": set(),  # "title|start" strings we already alerted on
    "cached_events": [],  # list of dicts from last fetch
    # End-of-day
    "eod_triggered_today": False,
    "eod_date": "",
    # Ambient delivery controls
    "last_spoken_ts": 0.0,
    "last_signal_by_key": {},  # key -> ts
    "last_ambient_pulse_ts": 0.0,
    "last_presence_ping_ts": 0.0,
    "last_kind_emit_ts": {},  # kind -> ts
    "last_live_event_key": "",
    "last_live_event_ts": 0.0,
    "health_state": "active",  # active|degraded|recovering
    "consecutive_errors": 0,
}

# Thresholds
FLOW_THRESHOLD_SECS = 22 * 60  # 22 min continuous in productive app = flow
FLOW_DEBRIEF_MIN_SEC = 15 * 60  # only debrief if flow lasted ≥15 min
DISTRACT_WARN_SECS = 15 * 60  # 15 min in distraction app = warn
DISTRACT_COOLDOWN = 3600  # re-warn at most once per hour per app
CALENDAR_REFETCH = 300  # re-fetch calendar every 5 min
CALENDAR_ALERT_MIN = 12  # minutes before event to alert
EOD_HOUR = 17  # 5 PM end-of-day nudge
LOOP_INTERVAL = 60  # run checks every 60s
DEFAULT_SIGNAL_DEDUP = 420  # suppress similar proactive signals for 7 minutes
AMBIENT_PULSE_SECONDS = 180
PRESENCE_PING_SECONDS = 240

_KIND_COOLDOWNS = {
    "mentor_proactive": 120,
    "live_work_mentor": 150,
    "presence_ping": 240,
    "ambient_pulse": 180,
    "calendar": 180,
    "focus_debrief": 60,
    "distraction": 900,
    "screen_stale": 180,
}


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _is_productive(app_name: str) -> bool:
    if not app_name:
        return False
    app = app_name.lower()
    return any(p in app for p in _PRODUCTIVE_APPS)


def _is_distraction(app_name: str, window_title: str = "") -> bool:
    if not app_name:
        return False
    combined = (app_name + " " + window_title).lower()
    return any(d in combined for d in _DISTRACTION_APPS)


def _get_current_app_run() -> tuple[str, int]:
    """
    Returns (current_app_name, consecutive_seconds_in_that_app).
    Scans recent screenshots newest-first until app changes.
    """
    try:
        shots = db.get_recent_screenshots(3600, limit=1200)  # last hour
        if not shots:
            return "", 0

        shots_sorted = sorted(shots, key=lambda s: s.get("ts", 0), reverse=True)
        current_app = (shots_sorted[0].get("app_name") or "").lower()
        if not current_app:
            return "", 0

        earliest_ts = shots_sorted[0].get("ts", 0)
        for s in shots_sorted:
            if (s.get("app_name") or "").lower() != current_app:
                break
            earliest_ts = s.get("ts", 0)

        duration = int(shots_sorted[0].get("ts", 0) - earliest_ts)
        return current_app, duration

    except Exception as e:
        log.debug(f"App run calc error: {e}")
        return "", 0


def _get_window_title() -> str:
    """Most recent window title from DB."""
    try:
        ctx = db.get_recent_context(120)
        shots = ctx.get("screenshots", [])
        if shots:
            return shots[0].get("window_title", "")
    except Exception:
        pass
    return ""


def _get_recent_work_snapshots(window_seconds: int = 12 * 60, limit: int = 80) -> list[dict]:
    try:
        return db.get_recent_screenshots(window_seconds, limit=limit)
    except Exception:
        return []


def _detect_stuckness(shots: list[dict]) -> dict:
    """
    Heuristic stuck detector:
    - same productive app for a while
    - low unique screen changes
    - repeated error/debug/problem markers
    """
    if not shots:
        return {"is_stuck": False, "confidence": 0.0, "reason": "no_shots"}

    ordered = sorted(shots, key=lambda s: float(s.get("ts", 0) or 0), reverse=True)
    latest = ordered[0]
    current_app = (latest.get("app_name") or "").lower().strip()
    if not current_app or not _is_productive(current_app):
        return {"is_stuck": False, "confidence": 0.0, "reason": "not_productive"}

    contiguous: list[dict] = []
    for shot in ordered:
        app = (shot.get("app_name") or "").lower().strip()
        if app != current_app:
            break
        contiguous.append(shot)

    if len(contiguous) < 4:
        return {"is_stuck": False, "confidence": 0.0, "reason": "too_short"}

    newest_ts = float(contiguous[0].get("ts", 0) or 0)
    oldest_ts = float(contiguous[-1].get("ts", 0) or 0)
    duration = max(0, int(newest_ts - oldest_ts))
    hashes = {
        (shot.get("content_hash") or "").strip()
        for shot in contiguous
        if (shot.get("content_hash") or "").strip()
    }
    unique_ratio = (len(hashes) / max(1, len(contiguous))) if contiguous else 1.0
    combined = "\n".join(
        " ".join(
            (
                str(shot.get("window_title") or ""),
                str(shot.get("focused_context") or ""),
                str(shot.get("ocr_text") or ""),
            )
        ).lower()
        for shot in contiguous[:8]
    )
    error_markers = (
        "error",
        "traceback",
        "exception",
        "failed",
        "undefined",
        "nan",
        "todo",
        "fixme",
        "stuck",
        "blocked",
        "warning",
        "failing",
        "assert",
    )
    marker_hits = sum(1 for m in error_markers if m in combined)

    is_stuck = duration >= 7 * 60 and (unique_ratio <= 0.45 or marker_hits >= 2)
    confidence = 0.0
    if is_stuck:
        confidence = min(0.95, 0.45 + (duration / 1800.0) + max(0, 0.15 * marker_hits))
    return {
        "is_stuck": is_stuck,
        "confidence": round(confidence, 2),
        "reason": f"duration={duration}s unique_ratio={unique_ratio:.2f} marker_hits={marker_hits}",
        "duration_seconds": duration,
        "unique_ratio": unique_ratio,
        "marker_hits": marker_hits,
        "app": current_app,
    }


def _mentor_style_instruction() -> str:
    style = str(getattr(config, "LIVE_WORK_MENTOR_STYLE", "balanced") or "balanced").lower()
    tolerance = max(1, min(5, int(getattr(config, "LIVE_WORK_MENTOR_TOLERANCE", 3) or 3)))
    tone = {
        "quiet": "Speak rarely. Only interrupt for high-signal guidance.",
        "balanced": "Speak when there is a concrete edge, not for narration.",
        "aggressive": "Be assertive and proactive when you see leverage.",
    }.get(style, "Speak when there is a concrete edge, not for narration.")
    tol_text = {
        1: "User tolerance is very low: interruptions must be rare and extremely high-value.",
        2: "User tolerance is low: keep interruptions selective.",
        3: "User tolerance is moderate: allow useful guidance, but avoid spam.",
        4: "User tolerance is high: interrupt when you can materially help.",
        5: "User tolerance is very high: bias toward frequent high-signal coaching.",
    }[tolerance]
    return f"{tone} {tol_text}"


def _emit_toast(title: str, body: str, urgency: int = 3) -> None:
    """Thread-safe toast emission."""
    try:
        from ui.bridge import get_bridge

        get_bridge().toast_requested.emit(title, body, urgency)
    except Exception:
        pass


def _emit_overlay(kind: str, title: str, body: str, confidence: float = 0.78) -> None:
    try:
        from ui.bridge import get_bridge

        get_bridge().overlay_update.emit(
            json.dumps(
                {
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "state": "proactive",
                    "current_action": body[:120],
                    "confidence": confidence,
                    "next_step": "",
                }
            )
        )
    except Exception:
        pass


def _signal_key(kind: str, body: str) -> str:
    compact = re.sub(r"\s+", " ", (body or "").lower()).strip()
    return f"{kind}:{compact[:90]}"


def _should_emit_signal(kind: str, body: str, urgency: int) -> bool:
    now = time.time()

    # Per-lane cooldowns (Omi-style practical anti-spam by category)
    lane_cd = int(_KIND_COOLDOWNS.get(kind, 0))
    if lane_cd > 0:
        last_kind = float(_state["last_kind_emit_ts"].get(kind, 0.0))
        if last_kind and now - last_kind < lane_cd:
            return False

    key = _signal_key(kind, body)
    dedup_seconds = int(
        getattr(config, "PROACTIVE_SIGNAL_DEDUP_SECONDS", DEFAULT_SIGNAL_DEDUP)
    )
    if urgency >= 5:
        dedup_seconds = min(dedup_seconds, 120)
    prev = float(_state["last_signal_by_key"].get(key, 0.0))
    if prev and now - prev < dedup_seconds:
        return False
    _state["last_signal_by_key"][key] = now
    if len(_state["last_signal_by_key"]) > 240:
        items = sorted(
            _state["last_signal_by_key"].items(), key=lambda kv: kv[1], reverse=True
        )
        _state["last_signal_by_key"] = dict(items[:180])

    _state["last_kind_emit_ts"][kind] = now
    if len(_state["last_kind_emit_ts"]) > 100:
        pairs = sorted(
            _state["last_kind_emit_ts"].items(), key=lambda kv: kv[1], reverse=True
        )
        _state["last_kind_emit_ts"] = dict(pairs[:60])
    return True


def _in_meeting_now() -> bool:
    recent_apps = db.get_recent_apps(window_seconds=120)
    hard_meeting_apps = {"zoom", "teams", "meet", "webex", "whereby"}
    if any(app in hard_meeting_apps for app in recent_apps):
        return True

    # Slack/Discord/etc should only count as meetings when call/huddle-like titles are present.
    try:
        ctx = db.get_recent_context(120)
        titles = " ".join(
            (s.get("window_title") or "").lower() for s in ctx.get("screenshots", [])
        )
        meeting_words = (
            "meeting",
            "huddle",
            "call",
            "joining",
            "zoom",
            "google meet",
            "teams",
        )
        soft_apps = {"slack", "discord", "loom"}
        if any(app in soft_apps for app in recent_apps) and any(
            w in titles for w in meeting_words
        ):
            return True
    except Exception:
        pass
    return False


def _in_flow_now() -> bool:
    recent_apps = db.get_recent_apps(window_seconds=300)
    has_flow_app = any(app in config.FLOW_STATE_APPS for app in recent_apps)
    has_meeting = any(app in config.MEETING_APPS for app in recent_apps)
    return has_flow_app and not has_meeting


def _user_actively_speaking() -> bool:
    try:
        ctx = db.get_recent_context(4)
        transcripts = ctx.get("transcripts", [])
        chars = sum(len((t.get("text") or "").strip()) for t in transcripts)
        newest_ts = max((float(t.get("ts") or 0.0) for t in transcripts), default=0.0)
        if chars < 45:
            return False
        if newest_ts and (time.time() - newest_ts) > 4.5:
            return False
        return True
    except Exception:
        return False


def _can_speak_now(urgency: int) -> bool:
    if not config.PROACTIVE_SPEECH_ENABLED:
        return False
    if not getattr(config, "VOICE_ENABLED", True):
        return False
    if urgency < int(getattr(config, "PROACTIVE_SPEECH_MIN_URGENCY", 4)):
        return False
    if _user_actively_speaking() and urgency < 5:
        return False
    if _in_meeting_now() and urgency < 5:
        return False
    if _in_flow_now() and urgency < 4:
        return False

    min_gap = int(getattr(config, "PROACTIVE_SPEECH_MIN_GAP_SECONDS", 60))
    now = time.time()
    if now - float(_state["last_spoken_ts"]) < min_gap and urgency < 5:
        return False
    _state["last_spoken_ts"] = now
    return True


async def _surface_signal(
    body: str,
    urgency: int = 3,
    *,
    title: str = "",
    speak_now: bool = False,
    kind: str = "proactive",
) -> None:
    title = title or _marrow_name()
    if not _should_emit_signal(kind, body, urgency):
        log.debug(f"Proactive signal deduped: kind={kind} urgency={urgency}")
        return

    # Omi-like ambient ladder: overlay pulse -> toast -> speech
    _emit_overlay(kind, title, body, confidence=0.78)
    audio_unavailable = (not getattr(config, "VOICE_ENABLED", True)) or (
        not getattr(config, "PROACTIVE_SPEECH_ENABLED", True)
    )
    toast_threshold = max(1, int(getattr(config, "PROACTIVE_TOAST_MIN_URGENCY", 1)))
    force_toast = bool(
        getattr(config, "PROACTIVE_FORCE_TOAST_WHEN_AUDIO_UNAVAILABLE", True)
    )
    if urgency >= toast_threshold or (audio_unavailable and force_toast):
        _emit_toast(title, body, urgency)

    auto_min = int(getattr(config, "PROACTIVE_AUTO_SPEAK_MIN_URGENCY", 2))
    should_speak = speak_now or urgency >= auto_min

    if should_speak and _can_speak_now(urgency):
        try:
            from voice.speak import speak

            await speak(body)
        except Exception as e:
            log.debug(f"Proactive speech failed: {e}")


def _marrow_name() -> str:
    return getattr(config, "MARROW_NAME", "Marrow")


def _build_live_guidance(app: str, title: str, ocr_text: str = "") -> str:
    app_l = (app or "").lower()
    title_l = (title or "").lower()
    text_l = (ocr_text or "").lower()

    if any(
        x in app_l
        for x in ("code", "cursor", "pycharm", "intellij", "terminal", "powershell")
    ):
        return (
            "You're live in build mode. Start with one concrete next step, run it immediately, "
            "then I can verify the result and queue the follow-up."
        )

    if any(
        x in app_l for x in ("slack", "discord", "teams", "mail", "outlook", "gmail")
    ):
        return (
            "You're in comms. I recommend clearing the highest-impact reply first, "
            "then I'll draft the next two responses to keep momentum."
        )

    if any(x in app_l for x in ("chrome", "edge", "brave", "firefox", "safari")):
        if any(
            x in title_l + " " + text_l
            for x in ("youtube", "reddit", "x.com", "twitter")
        ):
            return (
                "I can feel drift risk here. Give me the target outcome and I'll steer this session "
                "to a concrete result instead of passive browsing."
            )
        return (
            "You're in research mode. Tell me the decision you need to make, "
            "and I'll extract only decision-grade facts and contradictions."
        )

    if any(
        x in app_l for x in ("excel", "sheets", "numbers", "notion", "obsidian", "word")
    ):
        return (
            "You're in planning/doc mode. I can help you structure the next three actions "
            "and turn this into an executable checklist."
        )

    return "I'm live with full context. Give me your immediate objective and I'll drive the next step now."


def _contains_strong_error_signal(app_name: str, combined: str) -> bool:
    app_l = (app_name or "").lower()
    dev_app = any(
        x in app_l for x in ("code", "cursor", "pycharm", "intellij", "terminal", "powershell")
    )
    strong_patterns = (
        "traceback",
        "exception:",
        "syntaxerror",
        "typeerror",
        "module not found",
        "build failed",
        "test failed",
        "compilation failed",
        "command failed",
        "npm err!",
        "pytest",
        "failed with exit code",
    )
    if any(token in combined for token in strong_patterns):
        return True
    if dev_app and "error:" in combined:
        return True
    return False


def _contains_strong_task_signal(combined: str) -> bool:
    task_hits = sum(
        1
        for token in (
            "todo",
            "action item",
            "next step",
            "follow up",
            "follow-up",
            "reply by",
            "needs response",
            "assign to",
        )
        if token in combined
    )
    return task_hits >= 2


def _contains_strong_decision_signal(combined: str) -> bool:
    decision_hits = sum(
        1
        for token in (
            "should we",
            "should i",
            "what do you think",
            "which one",
            "pick one",
            "trade-off",
            "tradeoff",
            "option a",
            "option b",
            "pros and cons",
            "decide",
            "decision",
        )
        if token in combined
    )
    return decision_hits >= 1


async def _generate_grounded_advice(
    app_name: str,
    window_title: str,
    focused_context: str,
    ocr_text: str,
) -> tuple[str, int] | None:
    """Small advisory lane for decision/help moments that deserve an opinion, not just detection."""
    from brain.llm import get_client

    llm = get_client()
    if llm.provider == "none":
        return None

    prompt = f"""You are Marrow, a proactive laptop companion with good judgment.

Current app: {app_name}
Window title: {window_title}
Focused context: {focused_context[:220]}
Screen summary: {ocr_text[:700]}

Write one short grounded proactive message only if this is a real decision, ambiguity, trade-off, next-step, or stalled-momentum moment.
Have a real opinion when warranted. Sound like a sharp friend helping in real time.
Do not narrate the screen. Do not ask generic questions. Do not mention AI or observations.

Return strict JSON only:
{{"speak": true|false, "message": "one or two sentences", "urgency": 2|3|4}}
"""
    try:
        response = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            model_type="scoring",
        )
        raw = (response.text or "").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        data = json.loads(raw[start:end])
        if not bool(data.get("speak")):
            return None
        message = (data.get("message") or "").strip()
        if not message:
            return None
        urgency = max(2, min(4, int(data.get("urgency", 3))))
        return message, urgency
    except Exception as e:
        log.debug(f"Grounded advice generation failed: {e}")
        return None


async def handle_live_screen_event(
    app_name: str,
    window_title: str,
    focused_context: str = "",
    ocr_text: str = "",
) -> None:
    """Grounded proactive lane driven by fresh screen changes, not timer heartbeats."""
    app = (app_name or "").strip()
    title = (window_title or "").strip()
    focused = (focused_context or "").strip()
    ocr = (ocr_text or "").strip()
    combined = "\n".join(x for x in [title, focused, ocr] if x).lower()

    if not combined:
        return
    if _in_meeting_now() or _user_actively_speaking():
        return

    msg = ""
    urgency = 3
    kind = "live_screen"

    if _contains_strong_error_signal(app, combined):
        target = focused or title or app or "the current screen"
        msg = f"That looks like a real failure in {target[:90]}. I'd fix that before doing anything else."
        urgency = 4
        kind = "live_error"
    elif any(
        token in combined
        for token in ("deadline", "due today", "due tomorrow", "urgent", "asap", "eod")
    ):
        target = title or app or "what you're looking at"
        msg = f"{target[:90]} has real urgency on it. Best move is to turn it into one concrete next action right now."
        urgency = 4
        kind = "live_deadline"
    elif _contains_strong_task_signal(combined):
        target = title or app or "this view"
        msg = f"The next movable thing is in {target[:90]}. I'd do that now instead of leaving it hanging."
        urgency = 3
        kind = "live_task"
    elif _contains_strong_decision_signal(combined):
        target = title or app or "this screen"
        msg = f"You're at a real choice in {target[:90]}. My bias is toward the more reversible option unless the upside of the heavier path is obvious."
        urgency = 3
        kind = "live_decision"

    if not msg:
        advice = await _generate_grounded_advice(app, title, focused, ocr)
        if advice:
            msg, urgency = advice
            kind = "live_advice"
        else:
            return

    event_key = f"{kind}:{app.lower()}:{title[:80].lower()}:{focused[:80].lower()}"
    now = time.time()
    if (
        event_key == _state.get("last_live_event_key")
        and (now - float(_state.get("last_live_event_ts", 0.0))) < 180
    ):
        return

    _state["last_live_event_key"] = event_key
    _state["last_live_event_ts"] = now
    await _surface_signal(msg, urgency=urgency, kind=kind)


async def emit_live_kickoff() -> None:
    """Deterministic startup proactive guidance right after Marrow is live."""
    if not getattr(config, "LIVE_KICKOFF_ENABLED", True):
        return

    # Wait for capture loops to produce first context sample.
    deadline = time.time() + 25
    latest = None
    while time.time() < deadline:
        ctx = db.get_recent_context(120)
        shots = ctx.get("screenshots", [])
        if shots:
            latest = shots[0]
            break
        await asyncio.sleep(1.0)

    if not latest:
        await _surface_signal(
            "I'm live and ready. If you give me the goal, I'll start executing immediately.",
            urgency=4,
            speak_now=True,
            kind="live_kickoff_no_context",
        )
        return

    app = (latest.get("app_name") or "your current app").strip()
    title = (latest.get("window_title") or "").strip()
    ocr = (latest.get("ocr_text") or "").strip()

    guidance = _build_live_guidance(app, title, ocr)
    msg = f"I'm live and tracking {app}"
    if title:
        msg += f" on '{title[:52]}'"
    msg += f". {guidance}"

    await _surface_signal(msg, urgency=4, speak_now=True, kind="live_kickoff")


# ─── Focus state ──────────────────────────────────────────────────────────────


async def _check_focus_state() -> None:
    """
    Detect flow state (prolonged productive focus).
    When entering flow: store start time, lower interrupt priority.
    When exiting flow: emit debrief toast and DB observation.
    """
    current_app, duration_secs = _get_current_app_run()
    currently_productive = _is_productive(current_app)
    was_in_flow = _state["in_flow"]

    if currently_productive and duration_secs >= FLOW_THRESHOLD_SECS:
        if not was_in_flow:
            # Entering flow state
            _state["in_flow"] = True
            _state["flow_app"] = current_app
            _state["flow_start_ts"] = time.time() - duration_secs
            log.info(f"Flow state entered: {current_app} ({duration_secs // 60}m)")
            await _surface_signal(
                f"You're in a good focus run on {current_app}. I'll keep interruptions light unless important.",
                urgency=2,
                kind="focus_start",
            )
            db.insert_observation(
                "focus_state",
                f"User entered flow state in {current_app}. Interrupt threshold raised.",
                source="proactive",
            )

    elif was_in_flow and not currently_productive:
        # Exiting flow state
        _state["in_flow"] = False
        flow_duration = int(time.time() - _state["flow_start_ts"])
        flow_mins = flow_duration // 60

        if flow_duration >= FLOW_DEBRIEF_MIN_SEC:
            await _emit_focus_debrief(flow_mins, _state["flow_app"])
            _state["last_focus_debrief_ts"] = time.time()

        _state["flow_app"] = ""
        _state["flow_start_ts"] = 0.0
        db.insert_observation(
            "focus_state",
            f"User exited flow state. Was focused for {flow_mins} minutes.",
            source="proactive",
        )


async def _emit_focus_debrief(flow_mins: int, app: str) -> None:
    """
    After a focus session ends, brief the user on what happened while they worked.
    Direct toast + reasoning observation.
    """
    try:
        ctx = db.get_recent_context(flow_mins * 60 + 300)

        # Count new items
        transcripts = ctx.get("transcripts", [])
        new_audio = len(
            [t for t in transcripts if "marrow" not in (t.get("text") or "").lower()]
        )

        # Check for pending calendar events
        events_soon = [
            e
            for e in _state["cached_events"]
            if _minutes_until_event(e) is not None and 0 < _minutes_until_event(e) < 60
        ]

        lines = [f"You focused for {flow_mins} minutes."]
        if events_soon:
            for ev in events_soon[:2]:
                mins = _minutes_until_event(ev)
                lines.append(f"Meeting in {mins} min: {ev.get('title', 'Untitled')}")

        if new_audio > 3:
            lines.append(f"{new_audio} audio events captured while you worked.")

        body = " ".join(lines)
        await _surface_signal(body, urgency=4, speak_now=True, kind="focus_debrief")

        db.insert_observation(
            "focus_debrief",
            f"Focus session ended ({flow_mins}min, {app}). Brief: {body}",
            source="proactive",
        )
    except Exception as e:
        log.debug(f"Focus debrief error: {e}")


# ─── Distraction detection ────────────────────────────────────────────────────


async def _check_distraction() -> None:
    """
    If user has been in a distraction app for > DISTRACT_WARN_SECS, warn them.
    Only warns once per DISTRACT_COOLDOWN per app.
    Cross-references with deadline proximity from world model for urgency.
    """
    current_app, duration_secs = _get_current_app_run()
    title = _get_window_title()

    if not _is_distraction(current_app, title):
        return
    if duration_secs < DISTRACT_WARN_SECS:
        return

    last_warn = _state["distraction_warned"].get(current_app, 0)
    if time.time() - last_warn < DISTRACT_COOLDOWN:
        return

    _state["distraction_warned"][current_app] = time.time()
    mins = duration_secs // 60

    # Check for deadline proximity in world model
    deadline_context = _get_deadline_context()
    suffix = f" {deadline_context}" if deadline_context else ""

    msg = f"You've been on {current_app} for {mins} minutes.{suffix}"
    await _surface_signal(msg, urgency=3, kind="distraction")

    db.insert_observation(
        "distraction_signal",
        msg,
        source="proactive",
    )
    log.info(f"Distraction warn: {current_app} for {mins}m")


def _get_deadline_context() -> str:
    """Check world model for upcoming deadlines."""
    try:
        wm = db.get_world_model_entries(limit=50)
        today = datetime.now().date()
        for entry in wm:
            content = (entry.get("content") or "").lower()
            if "deadline" in content or "due" in content or "submit" in content:
                # Any deadline mention is surfaced
                return f"You have a deadline: {entry['content'][:80]}"
    except Exception:
        pass
    return ""


# ─── Calendar proximity ───────────────────────────────────────────────────────


def _minutes_until_event(event: dict) -> Optional[int]:
    """Parse event start time, return minutes until it starts. None if unparseable."""
    start_str = event.get("start") or event.get("time") or event.get("date") or ""
    if not start_str:
        return None
    try:
        # Try common formats
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%H:%M",
            "%I:%M %p",
        ):
            try:
                if fmt in ("%H:%M", "%I:%M %p"):
                    now = datetime.now()
                    t = datetime.strptime(start_str.strip(), fmt)
                    target = now.replace(hour=t.hour, minute=t.minute, second=0)
                    if target < now:
                        target += timedelta(days=1)
                else:
                    target = datetime.strptime(start_str.strip(), fmt)
                delta = int((target - datetime.now()).total_seconds() / 60)
                return delta
            except ValueError:
                continue
    except Exception:
        pass
    return None


async def _check_calendar() -> None:
    """
    Fetch calendar events and alert when one is within CALENDAR_ALERT_MIN minutes.
    Fetches every CALENDAR_REFETCH seconds, alerts are deduped by event identity.
    """
    now = time.time()
    if now - _state["last_calendar_fetch_ts"] < CALENDAR_REFETCH:
        # Use cache
        events = _state["cached_events"]
    else:
        events = await _fetch_calendar_events()
        _state["cached_events"] = events
        _state["last_calendar_fetch_ts"] = now

    for event in events:
        title = event.get("title") or event.get("summary") or "Meeting"
        start = event.get("start") or event.get("time") or ""
        event_key = f"{title}|{start}"

        if event_key in _state["alerted_events"]:
            continue

        mins = _minutes_until_event(event)
        if mins is None:
            continue

        if 0 < mins <= CALENDAR_ALERT_MIN:
            _state["alerted_events"].add(event_key)
            location = event.get("location") or event.get("url") or ""
            location_str = f" — {location[:50]}" if location else ""
            msg = f"In {mins} minute{'s' if mins != 1 else ''}: {title}{location_str}"
            await _surface_signal(
                msg,
                urgency=5,
                speak_now=True,
                kind="calendar_alert",
            )
            db.insert_observation(
                "calendar_alert",
                f"Upcoming event: {title} in {mins}min. {location_str}",
                source="proactive",
            )
            log.info(f"Calendar alert: {title} in {mins}m")

        elif mins < 0 and abs(mins) < 5:
            # Event just started — if not alerted, do it now
            event_key_now = f"{title}|{start}|started"
            if event_key_now not in _state["alerted_events"]:
                _state["alerted_events"].add(event_key_now)
                await _surface_signal(
                    f"Starting now: {title}",
                    urgency=5,
                    speak_now=True,
                    kind="calendar_start",
                )


async def _fetch_calendar_events() -> list[dict]:
    """Fetch today's calendar events. Runs the executor tool in a thread."""
    try:
        import platform

        loop = asyncio.get_event_loop()

        def _sync_fetch():
            try:
                from actions.executor import _get_calendar

                raw = _get_calendar(days=1)
                return _parse_calendar_text(raw)
            except Exception as e:
                log.debug(f"Calendar fetch error: {e}")
                return []

        return await loop.run_in_executor(None, _sync_fetch)
    except Exception as e:
        log.debug(f"Calendar async fetch error: {e}")
        return []


def _parse_calendar_text(raw: str) -> list[dict]:
    """
    Parse the text output from _get_calendar() into structured event dicts.
    The output format varies by platform (osascript / outlook / google).
    We do best-effort extraction.
    """
    if not raw or raw.startswith("["):
        return []

    events = []
    lines = raw.splitlines()
    current: dict = {}

    time_re = re.compile(
        r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)"
        r"|\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})\b",
        re.IGNORECASE,
    )
    skip_keywords = {"no events", "no upcoming", "error", "nothing scheduled"}

    for line in lines:
        line = line.strip()
        if not line:
            if current:
                events.append(current)
                current = {}
            continue
        if any(k in line.lower() for k in skip_keywords):
            continue

        # Look for time
        tm = time_re.search(line)
        if tm:
            current["start"] = (tm.group(1) or tm.group(2) or "").strip()
            # Title is the rest of the line before/after time
            title_part = line[: tm.start()].strip(" -:•") or line[tm.end() :].strip(
                " -:•"
            )
            if title_part:
                current["title"] = title_part[:80]
        elif not current.get("title") and len(line) > 3:
            current["title"] = line[:80]
        elif current.get("title") and "location" not in current and len(line) < 80:
            current["location"] = line

    if current:
        events.append(current)

    # Filter: only events that have a title
    return [e for e in events if e.get("title")]


# ─── End of day ──────────────────────────────────────────────────────────────


async def _check_end_of_day() -> None:
    """At EOD_HOUR, trigger a day-wrap summary if not done today."""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    if (
        now.hour != EOD_HOUR
        or _state["eod_triggered_today"]
        or _state["eod_date"] == today_str
    ):
        return

    _state["eod_triggered_today"] = True
    _state["eod_date"] = today_str

    log.info("End-of-day summary triggered")
    db.insert_observation(
        "end_of_day",
        f"End of working day ({today_str}). Reasoning loop should surface day summary.",
        source="proactive",
    )
    await _surface_signal(
        "The workday is winding down. I can help you wrap up open loops and summarize what changed.",
        urgency=4,
        speak_now=True,
        kind="end_of_day",
    )

    # Clear alerted events for tomorrow
    _state["alerted_events"].clear()


# ─── Context export for reasoning loop ───────────────────────────────────────


def get_proactive_context() -> str:
    """
    Returns a concise context block for the reasoning loop.
    Called from reasoning.py's context builder.
    """
    lines = []

    # Focus state
    if _state["in_flow"]:
        flow_mins = int((time.time() - _state["flow_start_ts"]) / 60)
        lines.append(
            f"[FOCUS] User is in flow state — {_state['flow_app']} for {flow_mins} min. "
            f"Raise interrupt bar unless urgency ≥ 4."
        )

    # Upcoming calendar events (next 60 min)
    upcoming = []
    for ev in _state["cached_events"]:
        mins = _minutes_until_event(ev)
        if mins is not None and 0 < mins <= 60:
            upcoming.append(f"{ev.get('title', 'Meeting')} in {mins}min")
    if upcoming:
        lines.append("[CALENDAR] Upcoming: " + " | ".join(upcoming[:3]))

    # Recent observations
    obs_types = [
        ("distraction_signal", "DISTRACTION"),
        ("calendar_alert", "CALENDAR ALERT"),
        ("focus_debrief", "POST-FOCUS"),
        ("end_of_day", "END OF DAY"),
    ]
    for obs_type, label in obs_types:
        obs = db.get_observations_by_type(obs_type, limit=1)
        if obs:
            ts = obs[0].get("ts", 0)
            if time.time() - ts < 600:  # only surface if fresh (<10 min)
                lines.append(f"[{label}] {obs[0]['content'][:140]}")

    if not lines:
        return ""

    return "=== PROACTIVE CONTEXT ===\n" + "\n".join(lines)


async def _check_ambient_pulse() -> None:
    """Periodic lightweight spoken check-in to avoid silent behavior."""
    if not getattr(config, "PROACTIVE_AMBIENT_PULSE_ENABLED", False):
        return
    now = time.time()
    if now - float(_state["last_ambient_pulse_ts"]) < AMBIENT_PULSE_SECONDS:
        return

    age = db.get_last_screenshot_age_seconds()
    if age is None or age > 120:
        await _surface_signal(
            "I don't have fresh screen context right now. I may be less proactive until screen capture resumes.",
            urgency=4,
            kind="screen_stale",
            speak_now=True,
        )
        _state["last_ambient_pulse_ts"] = now
        return

    ctx = db.get_recent_context(12 * 60)
    shots = ctx.get("screenshots", [])
    if not shots:
        return

    latest = shots[0]
    app = (latest.get("app_name") or "your current app").strip()
    title = (latest.get("window_title") or "").strip()

    obs = db.get_observations(limit=10)
    hint = ""
    for row in obs:
        t = (row.get("type") or "").lower()
        if t in {"deadline", "calendar_alert", "task", "focus_debrief", "claim_fact"}:
            hint = (row.get("content") or "").strip()[:110]
            break

    body = f"Quick check-in: you're in {app}"
    if title:
        body += f" on '{title[:52]}'"
    if hint:
        body += f". Noticed: {hint}"

    await _surface_signal(body, urgency=2, kind="ambient_pulse")
    _state["last_ambient_pulse_ts"] = now


async def _check_presence_ping() -> None:
    """Deterministic non-LLM presence ping if Marrow has been too quiet."""
    if not getattr(config, "PROACTIVE_PRESENCE_PING_ENABLED", False):
        return
    now = time.time()
    if now - float(_state["last_presence_ping_ts"]) < PRESENCE_PING_SECONDS:
        return

    # Need fresh visual context.
    age = db.get_last_screenshot_age_seconds()
    if age is None or age > 90:
        return

    # If we've already spoken recently, skip.
    last_interrupt_age = db.get_last_interruption_age_seconds()
    if last_interrupt_age is not None and last_interrupt_age < PRESENCE_PING_SECONDS:
        return

    if _in_meeting_now() or _user_actively_speaking():
        return

    ctx = db.get_recent_context(120)
    shots = ctx.get("screenshots", [])
    if not shots:
        return
    latest = shots[0]
    app = (latest.get("app_name") or "your current app").strip()
    title = (latest.get("window_title") or "").strip()

    msg = f"I'm with you and tracking {app}"
    if title:
        msg += f" on '{title[:52]}'"
    msg += ". If you want, I can take the next step now."

    # Force spoken ping (still goes through dedupe + gap logic).
    await _surface_signal(msg, urgency=4, speak_now=True, kind="presence_ping")
    _state["last_presence_ping_ts"] = now


async def _check_mentor_proactive() -> None:
    """Buffered mentor-style proactive lane (gate -> generate -> critic)."""
    if not getattr(config, "MENTOR_PROACTIVE_ENABLED", True):
        return
    try:
        from brain.mentor_proactive import maybe_generate_mentor_signal_for_session

        session_id = "default"
        try:
            ctx = db.get_recent_context(120)
            shots = ctx.get("screenshots", [])
            if shots:
                app = (shots[0].get("app_name") or "unknown").lower().strip()
                title = (shots[0].get("window_title") or "").lower().strip()
                session_id = f"{app}|{title[:80]}"
        except Exception:
            pass

        signal = await maybe_generate_mentor_signal_for_session(session_id)
        if not signal:
            return
        message, urgency = signal
        await _surface_signal(
            message,
            urgency=max(2, min(5, int(urgency))),
            speak_now=True,
            kind="mentor_proactive",
        )
    except Exception as e:
        log.debug(f"Mentor proactive check failed: {e}")


async def _check_live_work_mentor() -> None:
    """Screen-aware proactive coaching for focused work moments."""
    if not getattr(config, "LIVE_WORK_MENTOR_ENABLED", True):
        return
    if _in_meeting_now() or _user_actively_speaking():
        return

    now = time.time()
    if (
        now - float(_state.get("last_kind_emit_ts", {}).get("live_work_mentor", 0.0))
        < int(getattr(config, "LIVE_WORK_MENTOR_MIN_GAP_SECONDS", 150))
    ):
        return

    try:
        from brain.deep_reasoning import get_scratchpad_summary
        from brain.llm import get_client
    except Exception:
        return

    recent_shots = _get_recent_work_snapshots(12 * 60, limit=80)
    stuck = _detect_stuckness(recent_shots)

    ctx = db.get_recent_context(180)
    shots = ctx.get("screenshots", [])
    if not shots:
        return
    latest = shots[0]
    app = (latest.get("app_name") or "").strip()
    title = (latest.get("window_title") or "").strip()
    focused = (latest.get("focused_context") or "").strip()
    ocr = (latest.get("ocr_text") or "").strip()
    combined = "\n".join(x for x in [title, focused, ocr] if x).lower()

    if not app or not _is_productive(app):
        return

    work_markers = (
        "error",
        "traceback",
        "todo",
        "fixme",
        "function",
        "class",
        "api",
        "equation",
        "design",
        "architecture",
        "simulation",
        "experiment",
        "constraint",
        "assumption",
        "debug",
        "compile",
        "test",
    )
    if not any(marker in combined for marker in work_markers) and not stuck.get("is_stuck"):
        return

    llm = get_client()
    if llm.provider == "none":
        return

    scratchpad = ""
    try:
        scratchpad = get_scratchpad_summary(getattr(config, "DEEP_REASONING_SESSION_ID", "default"))
    except Exception:
        pass

    prompt = f"""You are Marrow, trying to feel like the closest thing to Jarvis that fits in a computer.

Decide whether to proactively interrupt with one sharp work-mentor nudge.

Return strict JSON only:
{{"speak": true|false, "mode": "teaching"|"execution"|"challenge", "message": "", "urgency": 2|3|4, "reason": ""}}

Only speak if there is a specific high-value intervention right now:
- a better next step
- a likely mistake or blind spot
- an assumption to challenge
- a tiny teaching insight that helps the user move faster while learning
- a verification/simulation/research step they should do before going further

Do not speak for generic encouragement, narration, or obvious commentary.
Keep the message to 1-2 sentences max.
Pick mode:
- teaching: explain a concept or mental model that directly unlocks progress now
- execution: tell them the best next concrete step
- challenge: push on a risky assumption, missing verification, or flawed direction

{_mentor_style_instruction()}

Current app: {app}
Window title: {title[:160]}
Focused context: {focused[:260]}
Screen summary: {ocr[:1200]}

Stuckness signal:
is_stuck={stuck.get("is_stuck")} confidence={stuck.get("confidence")} details={stuck.get("reason")}

Current scratchpad:
{scratchpad[:1800] if scratchpad else "None"}
"""
    try:
        response = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=140,
            model_type="scoring",
        )
        raw = (response.text or "").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return
        data = json.loads(raw[start:end])
        if not bool(data.get("speak")):
            return
        mode = str(data.get("mode", "") or "").strip().lower()
        if mode not in {"teaching", "execution", "challenge"}:
            mode = "execution" if stuck.get("is_stuck") else "teaching"
        message = (data.get("message") or "").strip()
        if not message:
            return
        urgency = max(2, min(4, int(data.get("urgency", 3))))
        prefix = {
            "teaching": "Quick concept: ",
            "execution": "Best next step: ",
            "challenge": "Challenge: ",
        }.get(mode, "")
        if prefix and not message.lower().startswith(prefix.lower()):
            message = prefix + message
        await _surface_signal(
            message,
            urgency=urgency,
            kind="live_work_mentor",
        )
    except Exception as e:
        log.debug(f"Live work mentor check failed: {e}")


# ─── Main loop ────────────────────────────────────────────────────────────────


async def proactive_loop() -> None:
    """
    The Jarvis heartbeat. Runs every 60 seconds.
    Performs time-driven checks that the reactive screen loop can't do.
    """
    log.info("Proactive intelligence loop started")

    # Stagger startup to avoid collision with startup sequence
    await asyncio.sleep(
        max(3, int(getattr(config, "PROACTIVE_STARTUP_DELAY_SECONDS", 8)))
    )

    sleep_seconds = LOOP_INTERVAL

    while True:
        try:
            await _check_focus_state()
            await _check_distraction()
            await _check_calendar()
            await _check_end_of_day()
            if getattr(config, "PROACTIVE_AMBIENT_PULSE_ENABLED", False):
                await _check_ambient_pulse()
            if getattr(config, "PROACTIVE_PRESENCE_PING_ENABLED", False):
                await _check_presence_ping()
            if getattr(config, "MENTOR_PROACTIVE_ENABLED", False):
                await _check_mentor_proactive()
            if getattr(config, "LIVE_WORK_MENTOR_ENABLED", True):
                await _check_live_work_mentor()
            _state["consecutive_errors"] = 0
            if _state["health_state"] != "active":
                _state["health_state"] = "recovering"
            sleep_seconds = LOOP_INTERVAL
        except Exception as e:
            log.debug(f"Proactive loop tick error: {e}")
            _state["consecutive_errors"] = int(_state.get("consecutive_errors", 0)) + 1
            if _state["consecutive_errors"] >= 3:
                _state["health_state"] = "degraded"
            # Exponential backoff to prevent hot error loops
            sleep_seconds = min(
                int(getattr(config, "PROACTIVE_BACKOFF_MAX_SECONDS", 300)),
                max(LOOP_INTERVAL, sleep_seconds * 2),
            )

        await asyncio.sleep(max(5, int(sleep_seconds)))


def get_proactive_health() -> dict:
    return {
        "state": _state.get("health_state", "active"),
        "consecutive_errors": int(_state.get("consecutive_errors", 0) or 0),
        "last_spoken_ts": float(_state.get("last_spoken_ts", 0.0) or 0.0),
        "last_presence_ping_ts": float(_state.get("last_presence_ping_ts", 0.0) or 0.0),
        "last_ambient_pulse_ts": float(_state.get("last_ambient_pulse_ts", 0.0) or 0.0),
    }

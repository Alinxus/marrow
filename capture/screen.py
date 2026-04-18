"""
Screen capture loop.

Every SCREENSHOT_INTERVAL seconds:
  1. Get active window (app name + title) via Windows API
  2. Skip if same window + same content hash as last capture (dedup)
  3. Grab screenshot with mss
  4. Send to Claude vision (haiku — fast + cheap) for semantic OCR
  5. Store in DB + optionally save image to disk

Design notes:
  - Uses VISION_MODEL (haiku) not REASONING_MODEL (sonnet) — screenshots
    are high-frequency, vision is the bottleneck, haiku is plenty smart for OCR
  - Content hash dedup: skip API call if screen hasn't changed
  - Images saved to ~/.marrow/screenshots/ for potential future retrieval
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import platform
import subprocess
import time
from datetime import datetime
from pathlib import Path

import mss
import mss.tools
from PIL import Image

import config
from storage import db

log = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path.home() / ".marrow" / "screenshots"

# Cache last capture for dedup
_last_app: str = ""
_last_title: str = ""
_last_focused: str = ""
_last_hash: str = ""
_last_vision_ts: float = 0.0
_last_persist_ts: float = 0.0
_mac_screen_perm_warned: bool = False


_OSASCRIPT_WINDOW = (
    'tell application "System Events"\n'
    "  set fp to first process whose frontmost is true\n"
    "  set appN to name of fp\n"
    '  set winT to ""\n'
    "  try\n"
    "    set winT to name of front window of fp\n"
    "  end try\n"
    '  return appN & "|" & winT\n'
    "end tell"
)

_OSASCRIPT_CHROME_URL = (
    'tell application "Google Chrome"\n'
    '  if not (exists front window) then return ""\n'
    "  try\n"
    "    return URL of active tab of front window\n"
    "  on error\n"
    '    return ""\n'
    "  end try\n"
    "end tell"
)

_OSASCRIPT_SAFARI_URL = (
    'tell application "Safari"\n'
    '  if not (exists front document) then return ""\n'
    "  try\n"
    "    return URL of front document\n"
    "  on error\n"
    '    return ""\n'
    "  end try\n"
    "end tell"
)


def _get_browser_url_mac(app_name: str) -> str:
    try:
        script = None
        if "chrome" in app_name:
            script = _OSASCRIPT_CHROME_URL
        elif "safari" in app_name:
            script = _OSASCRIPT_SAFARI_URL
        if not script:
            return ""

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return (result.stdout or "").strip()
    except Exception:
        pass
    return ""


def _get_active_window_mac() -> tuple[str, str, str]:
    """Get foreground app + window title on macOS via osascript."""
    try:
        result = subprocess.run(
            ["osascript", "-e", _OSASCRIPT_WINDOW],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            parts = out.split("|", 1)
            app_name = parts[0].strip().lower() if parts else "unknown"
            window_title = parts[1].strip() if len(parts) > 1 else "unknown"
            focused = ""
            url = _get_browser_url_mac(app_name)
            if url:
                focused = f"[URL] {url}"
            return app_name, window_title, focused
    except Exception as e:
        log.debug(f"macOS window detection failed: {e}")
    return "unknown", "unknown", ""


def _get_active_window() -> tuple[str, str, str]:
    """Returns (app_name, window_title, focused_context) for the foreground window."""
    if platform.system() == "Darwin":
        return _get_active_window_mac()

    try:
        import uiautomation as auto
        import psutil

        # Get foreground window
        win = auto.GetForegroundControl()
        if not win:
            return "unknown", "unknown", ""

        window_title = win.Name or "unknown"

        # Get app via process ID (cleaner than raw exe path)
        pid = win.ProcessId
        try:
            proc = psutil.Process(pid)
            app_name = proc.name().replace(".exe", "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            app_name = "unknown"

        # Get focused element context
        focused = auto.GetFocusedControl()
        focused_context = ""
        if focused and focused.Name:
            ctrl_type = focused.ControlTypeName or "Element"
            focused_context = f"[Focused: {ctrl_type}] {focused.Name}"

        # Browser URL extraction (Windows accessibility hint)
        if app_name in ("chrome", "msedge", "brave", "firefox"):
            try:
                root = win
                edit = root.EditControl(foundIndex=1)
                if edit and edit.Name:
                    candidate = edit.Name.strip()
                    if candidate.startswith("http") or "." in candidate:
                        focused_context = (
                            (focused_context + "\n") if focused_context else ""
                        ) + f"[URL] {candidate[:240]}"
            except Exception:
                pass

        return app_name, window_title, focused_context

    except Exception as e:
        log.debug(f"Active window detection failed: {e}")
        return "unknown", "unknown", ""


def _screenshot_to_b64(
    img: Image.Image,
    max_size: int = None,
    jpeg_quality: int = None,
) -> tuple[str, str]:
    """Resize + compress to base64 JPEG. Returns (b64, content_hash)."""
    if max_size is None:
        max_size = config.SCREEN_VISION_MAX_SIZE
    if jpeg_quality is None:
        jpeg_quality = config.SCREEN_VISION_JPEG_QUALITY

    ratio = min(max_size / img.width, max_size / img.height, 1.0)
    if ratio < 1.0:
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS
        )
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality)
    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode()
    content_hash = hashlib.md5(raw).hexdigest()
    return b64, content_hash


def _save_screenshot(img: Image.Image, ts: float) -> str:
    """Save screenshot to disk. Returns path string."""
    if not config.SCREENSHOT_SAVE_TO_DISK:
        return ""
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        dt = datetime.utcfromtimestamp(ts)
        fname = dt.strftime("%Y%m%d_%H%M%S") + ".jpg"
        path = SCREENSHOTS_DIR / fname
        img.convert("RGB").save(path, format="JPEG", quality=60)
        return str(path)
    except Exception as e:
        log.debug(f"Screenshot save failed: {e}")
        return ""


_VISION_PROMPT = (
    "Describe this screenshot precisely for an AI that needs to understand what the user is doing.\n\n"
    "Include ALL of the following you can see:\n"
    "- App and exact window/tab title\n"
    "- What the user is actively working on (document name, code file, email subject, URL)\n"
    "- Any visible text that matters: code, error messages, names, emails, chat messages, task lists\n"
    "- Any notifications, alerts, popups\n"
    "- If multiple windows: what's in focus vs background\n\n"
    "Transcribe as much relevant visible text as possible, especially headings, errors, decisions, options, tasks, and names.\n"
    "If this is code or technical output, include filenames, commands, stack traces, and failing lines when visible.\n"
    "If this is writing, planning, or chat, include the concrete choices, requests, and unresolved questions.\n\n"
    "Be specific and dense. No filler phrases. Plain text."
)


def _local_visual_summary(
    app_name: str, window_title: str, focused_context: str
) -> str:
    """No-LLM fallback summary so capture still produces usable context."""
    parts = [
        f"App: {app_name or 'unknown'}",
        f"Window: {window_title or 'unknown'}",
    ]
    if focused_context:
        parts.append(f"Focus: {focused_context[:240]}")
    parts.append("Vision model unavailable; using local window metadata only.")
    return "\n".join(parts)


def _extract_text_with_local_ocr(img: Image.Image) -> str:
    if not getattr(config, "SCREEN_OCR_ENABLED", True):
        return ""
    try:
        import pytesseract

        gray = img.convert("L")
        text = pytesseract.image_to_string(gray) or ""
        compact = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
        return compact[: int(getattr(config, "SCREEN_OCR_MAX_CHARS", 1800))]
    except Exception:
        return ""


def _build_screen_payload(
    app_name: str,
    window_title: str,
    focused_context: str,
    fused_summary: str,
    raw_ocr_text: str,
    vision_text: str,
) -> str:
    payload = {
        "metadata": {
            "app_name": app_name or "",
            "window_title": window_title or "",
            "focused_context": focused_context or "",
            "url": "",
        },
        "vision_summary": vision_text or "",
        "ocr_raw_text": raw_ocr_text or "",
        "fused_summary": fused_summary or "",
    }
    if focused_context and "[URL]" in focused_context:
        try:
            payload["metadata"]["url"] = focused_context.split("[URL]", 1)[1].strip()
        except Exception:
            pass
    return json.dumps(payload)


def _emit_perception_snapshot(
    app_name: str,
    window_title: str,
    focused_context: str,
    ocr_text: str,
    raw_ocr_text: str = "",
    vision_text: str = "",
    *,
    source: str,
) -> None:
    try:
        from ui.bridge import get_bridge

        payload = {
            "app": app_name or "",
            "title": window_title or "",
            "focused_context": (focused_context or "")[:240],
            "summary": (ocr_text or "")[:900],
            "ocr_raw_text": (raw_ocr_text or "")[:900],
            "vision_text": (vision_text or "")[:900],
            "source": source,
        }
        get_bridge().perception_update.emit(json.dumps(payload))
    except Exception:
        pass


async def _extract_text_with_vision(
    b64_image: str,
    app_name: str = "",
    window_title: str = "",
    focused_context: str = "",
) -> str:
    """
    Semantic screen OCR via the vision-capable model.
    Falls back gracefully if vision unavailable.
    """
    try:
        from brain.llm import get_client

        llm = get_client()

        if llm.provider == "none":
            return _local_visual_summary(app_name, window_title, focused_context)

        if llm.provider == "anthropic":
            client = llm.get_raw_anthropic()
            msg = await client.messages.create(
                model=config.VISION_MODEL,
                max_tokens=config.VISION_MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64_image,
                                },
                            },
                            {"type": "text", "text": _VISION_PROMPT},
                        ],
                    }
                ],
            )
            return msg.content[0].text.strip()

        elif llm.provider in ("openai", "ollama"):
            resp = await llm.create(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64_image}"
                                },
                            },
                            {"type": "text", "text": _VISION_PROMPT},
                        ],
                    }
                ],
                model_type="vision",
                max_completion_tokens=config.VISION_MAX_TOKENS,
            )
            text = resp.text.strip()
            return text or _local_visual_summary(
                app_name, window_title, focused_context
            )

    except Exception as e:
        log.warning(f"Vision extraction failed: {e}")
    return _local_visual_summary(app_name, window_title, focused_context)


async def screen_capture_loop() -> None:
    """Main screen capture loop. Runs forever. No external client needed."""
    global \
        _last_app, \
        _last_title, \
        _last_focused, \
        _last_hash, \
        _last_vision_ts, \
        _last_persist_ts, \
        _mac_screen_perm_warned

    log.info("Screen capture loop started")
    try:
        db.upsert_runtime_component("screen_capture", "starting", "loop boot")
    except Exception:
        pass

    with mss.mss() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

        while True:
            try:
                ts = time.time()
                app_name, window_title, focused_context = _get_active_window()

                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                b64, content_hash = _screenshot_to_b64(img)

                # Dedup — only on content hash + app/title (not focused_context,
                # which changes on every mouse hover and causes noise)
                if (
                    app_name == _last_app
                    and window_title == _last_title
                    and content_hash == _last_hash
                ):
                    heartbeat_every = max(20, config.SCREENSHOT_INTERVAL * 6)
                    if (ts - _last_persist_ts) >= heartbeat_every:
                        keepalive_text = (
                            _local_visual_summary(
                                app_name, window_title, focused_context
                            )
                            + "\nScreen unchanged (keepalive capture)."
                        )
                        db.insert_screenshot(
                            ts=ts,
                            app_name=app_name,
                            window_title=window_title,
                            focused_context=focused_context,
                            ocr_text=keepalive_text,
                            ocr_raw_text="",
                            vision_text="",
                            screen_payload_json=_build_screen_payload(
                                app_name,
                                window_title,
                                focused_context,
                                keepalive_text,
                                "",
                                "",
                            ),
                            image_path="",
                            content_hash=content_hash,
                        )
                        _last_persist_ts = ts
                        log.debug("Screen unchanged — keepalive persisted")
                    else:
                        log.debug("Screen unchanged — skipping")
                    await asyncio.sleep(config.SCREENSHOT_INTERVAL)
                    continue

                _last_app = app_name
                _last_title = window_title
                _last_focused = focused_context
                _last_hash = content_hash

                # Emit focus change to UI
                try:
                    from brain.conversation import note_reference
                    from brain.digital_twin import note_focus_change
                    from ui.bridge import get_bridge

                    note_reference("app", app_name)
                    note_reference("window", window_title)
                    note_focus_change(app_name, window_title)
                    get_bridge().focus_changed.emit(app_name, window_title)
                except Exception:
                    pass

                image_path = _save_screenshot(img, ts)
                now = time.time()
                use_vision = (now - _last_vision_ts) >= max(
                    config.SCREENSHOT_INTERVAL, config.SCREEN_VISION_INTERVAL_SECONDS
                )

                if use_vision:
                    vision_text = await _extract_text_with_vision(
                        b64,
                        app_name=app_name,
                        window_title=window_title,
                        focused_context=focused_context,
                    )
                    _last_vision_ts = now
                else:
                    vision_text = _local_visual_summary(
                        app_name, window_title, focused_context
                    )
                raw_ocr_text = _extract_text_with_local_ocr(img)
                fused_parts = []
                if vision_text:
                    fused_parts.append(f"Vision summary:\n{vision_text}")
                if raw_ocr_text:
                    fused_parts.append(f"Raw OCR:\n{raw_ocr_text}")
                if not fused_parts:
                    fused_parts.append(
                        _local_visual_summary(app_name, window_title, focused_context)
                    )
                ocr_text = "\n\n".join(fused_parts)
                screen_payload_json = _build_screen_payload(
                    app_name,
                    window_title,
                    focused_context,
                    ocr_text,
                    raw_ocr_text,
                    vision_text,
                )
                _emit_perception_snapshot(
                    app_name,
                    window_title,
                    focused_context,
                    ocr_text,
                    raw_ocr_text=raw_ocr_text,
                    vision_text=vision_text,
                    source="vision" if use_vision else "local",
                )

                db.insert_screenshot(
                    ts=ts,
                    app_name=app_name,
                    window_title=window_title,
                    focused_context=focused_context,
                    ocr_text=ocr_text,
                    ocr_raw_text=raw_ocr_text,
                    vision_text=vision_text,
                    screen_payload_json=screen_payload_json,
                    image_path=image_path,
                    content_hash=content_hash,
                )
                _last_persist_ts = ts
                try:
                    db.upsert_runtime_component(
                        "screen_capture",
                        "active",
                        f"app={app_name[:40]} title={window_title[:80]}",
                    )
                except Exception:
                    pass

                # High-signal context extraction (contact pressure + claim events)
                try:
                    from brain.context_awareness import process_screen_signals
                    from brain.proactive import handle_live_screen_event

                    # Pull the most recent audio transcript to feed claim detection
                    recent_transcripts = db.get_recent_context(30).get(
                        "transcripts", []
                    )
                    recent_audio = " ".join(
                        t.get("text", "") for t in recent_transcripts[-3:]
                    )

                    process_screen_signals(
                        ts,
                        app_name,
                        window_title,
                        ocr_text,
                        focused_context=focused_context,
                        transcript_text=recent_audio,
                    )
                    asyncio.create_task(
                        handle_live_screen_event(
                            app_name,
                            window_title,
                            focused_context=focused_context,
                            ocr_text=ocr_text,
                        )
                    )
                except Exception as e:
                    log.debug(f"Signal extraction skipped: {e}")

                log.debug(f"Screen captured: [{app_name}] {window_title[:60]}")

            except Exception as e:
                log.error(f"Screen capture error: {e}")
                try:
                    db.upsert_runtime_component("screen_capture", "error", str(e)[:220])
                except Exception:
                    pass
                msg = str(e).lower()
                if platform.system() == "Darwin" and not _mac_screen_perm_warned:
                    if (
                        "permission" in msg
                        or "not authorized" in msg
                        or "cgwindow" in msg
                        or "display" in msg
                    ):
                        _mac_screen_perm_warned = True
                        log.warning(
                            "macOS screen permission likely missing. Enable Screen Recording for Terminal/Python in System Settings > Privacy & Security > Screen Recording, then restart Marrow."
                        )
                        try:
                            from ui.bridge import get_bridge

                            get_bridge().toast_requested.emit(
                                "Marrow",
                                "Enable Screen Recording permission for Terminal/Python (System Settings > Privacy & Security).",
                                2,
                            )
                        except Exception:
                            pass

            await asyncio.sleep(config.SCREENSHOT_INTERVAL)

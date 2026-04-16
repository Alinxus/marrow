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
import logging
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


def _get_active_window() -> tuple[str, str, str]:
    """Returns (app_name, window_title, focused_context) for the foreground window."""
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
                max_tokens=700,
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
                max_completion_tokens=700,
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
    global _last_app, _last_title, _last_focused, _last_hash

    log.info("Screen capture loop started")

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
                    log.debug("Screen unchanged — skipping")
                    await asyncio.sleep(config.SCREENSHOT_INTERVAL)
                    continue

                _last_app = app_name
                _last_title = window_title
                _last_focused = focused_context
                _last_hash = content_hash

                # Emit focus change to UI
                try:
                    from ui.bridge import get_bridge

                    get_bridge().focus_changed.emit(app_name, window_title)
                except Exception:
                    pass

                image_path = _save_screenshot(img, ts)
                ocr_text = await _extract_text_with_vision(
                    b64,
                    app_name=app_name,
                    window_title=window_title,
                    focused_context=focused_context,
                )

                db.insert_screenshot(
                    ts=ts,
                    app_name=app_name,
                    window_title=window_title,
                    focused_context=focused_context,
                    ocr_text=ocr_text,
                    image_path=image_path,
                    content_hash=content_hash,
                )

                # High-signal context extraction (contact pressure + claim events)
                try:
                    from brain.context_awareness import process_screen_signals

                    process_screen_signals(
                        ts,
                        app_name,
                        window_title,
                        ocr_text,
                        focused_context=focused_context,
                    )
                except Exception as e:
                    log.debug(f"Signal extraction skipped: {e}")

                log.debug(f"Screen captured: [{app_name}] {window_title[:60]}")

            except Exception as e:
                log.error(f"Screen capture error: {e}")

            await asyncio.sleep(config.SCREENSHOT_INTERVAL)

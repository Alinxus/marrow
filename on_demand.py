"""
On-demand activation - multiple ways to wake Marrow.

1. Hotkey: Ctrl+Shift+M (configurable)
2. Wake word: "Marrow" or "Hey Marrow" (when mic always-on)
3. System tray click
4. CLI: marrow ask "..."

All modes flow into the same on_activation coroutine.

Thread safety: hotkey and wake-word fire from threads that have no event loop.
We store the main event loop and use run_coroutine_threadsafe to schedule
coroutines on it — the only correct way to cross the thread/async boundary.
"""

import asyncio
import logging
import platform
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import config

log = logging.getLogger(__name__)

_activation_callback: Optional[Callable] = None
_hotkey_thread: Optional[threading.Thread] = None
_main_loop: Optional[asyncio.AbstractEventLoop] = None  # set by set_main_loop()


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Must be called from main() after the event loop is running."""
    global _main_loop
    _main_loop = loop


def activate_marrow(reason: str = "hotkey") -> None:
    """
    Activate Marrow for on-demand interaction.
    Safe to call from any thread — schedules on the main event loop.
    """
    log.info(f"Marrow activated via {reason}")

    if _activation_callback is None:
        log.warning("No activation callback set")
        return

    if _main_loop is None:
        log.warning("No main event loop set — on-demand activation won't work")
        return

    asyncio.run_coroutine_threadsafe(_activation_callback(reason), _main_loop)


# ─── Hotkey listener (Windows) ─────────────────────────────────────────────────


def _hotkey_listener_windows() -> None:
    """Listen for hotkey using Windows RegisterHotKey (no admin required)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        MOD_CONTROL = 0x0002
        MOD_SHIFT = 0x0004
        VK_M = 0x4D
        hotkey_id = 9000

        user32.UnregisterHotKey(None, hotkey_id)

        if user32.RegisterHotKey(None, hotkey_id, MOD_CONTROL | MOD_SHIFT, VK_M):
            log.info(f"Hotkey registered: Ctrl+Shift+M")
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:
                    break
                if ret == -1:
                    break
                if msg.message == 0x0312 and msg.wParam == hotkey_id:  # WM_HOTKEY
                    activate_marrow("hotkey")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        else:
            log.warning("Failed to register Ctrl+Shift+M hotkey — trying fallback")
            _hotkey_listener_fallback()

    except Exception as e:
        log.warning(f"Windows hotkey setup failed: {e}")
        _hotkey_listener_fallback()


def _hotkey_listener_fallback() -> None:
    """Fallback hotkey using the `keyboard` package."""
    try:
        import keyboard

        hotkey = config.ON_DEMAND_HOTKEY
        keyboard.add_hotkey(hotkey, lambda: activate_marrow("hotkey"))
        log.info(f"Hotkey listener active (keyboard package): {hotkey}")
        keyboard.wait()  # blocks forever
    except ImportError:
        log.warning(
            "keyboard package not installed — hotkey disabled. Run: pip install keyboard (or set HOTKEY_ENABLED=0)."
        )
    except Exception as e:
        log.warning(
            f"Hotkey fallback failed: {e}. On macOS, grant Accessibility permission for Terminal/Python, or set HOTKEY_ENABLED=0."
        )


def _start_hotkey_listener() -> None:
    global _hotkey_thread
    if not config.HOTKEY_ENABLED:
        log.info("Hotkey disabled by config")
        return
    if platform.system() == "Windows":
        target = _hotkey_listener_windows
    else:
        target = _hotkey_listener_fallback
        log.info("Using fallback hotkey listener on non-Windows platform")

    _hotkey_thread = threading.Thread(target=target, daemon=True)
    _hotkey_thread.start()


# ─── Wake word ─────────────────────────────────────────────────────────────────


def check_wake_word(text: str) -> bool:
    text_lower = text.lower().strip()
    for wake_word in config.WAKE_WORDS:
        if text_lower == wake_word or text_lower.startswith(wake_word + " "):
            log.info(f"Wake word detected: {wake_word}")
            return True
    return False


# ─── CLI ───────────────────────────────────────────────────────────────────────


async def handle_cli_query(query: str) -> str:
    from actions import executor
    from storage import db
    from brain.world_model import get_world_context

    log.info(f"CLI query: {query}")
    context = "[ON-DEMAND MODE - User asked directly]\n"
    recent = db.get_recent_context(60)
    context += f"Recent: {len(recent.get('screenshots', []))} screens, {len(recent.get('transcripts', []))} transcripts\n"
    context += get_world_context()

    return await executor.execute_action(query, context=context)


# ─── Init ──────────────────────────────────────────────────────────────────────


def set_activation_callback(callback: Callable) -> None:
    global _activation_callback
    _activation_callback = callback


def init_on_demand() -> None:
    """Initialize all on-demand activation methods."""
    threading.Thread(target=_start_hotkey_listener, daemon=True).start()
    log.info("On-demand activation initialized (hotkey + wake word)")


# ─── CLI entry point ───────────────────────────────────────────────────────────


def cli_main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] != "ask":
        print('Usage: marrow ask "your question here"')
        sys.exit(1)
    query = sys.argv[2]
    result = asyncio.run(handle_cli_query(query))
    print(result)


if __name__ == "__main__":
    cli_main()

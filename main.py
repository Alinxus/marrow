"""
Marrow — ambient intelligence.

Threading model:
  Main thread  → PyQt6 event loop (UI MUST be on main thread on Windows)
  Daemon thread → asyncio event loop (all async work: capture, reasoning, TTS)

Communication:
  asyncio → Qt : emit Qt signals via bridge (thread-safe by Qt design)
  Qt → asyncio : run_coroutine_threadsafe(coro, _asyncio_loop)

UI architecture:
  MarrowOrb       — tiny always-on-top pulsing orb (56×56) in corner
  MarrowDashboard — full panel, opens/closes when orb is clicked
  ToastManager    — slide-in text notifications (substitute for voice)
  ApprovalDialog  — dangerous-action confirmation popup
  SettingsPanel   — settings editor
"""

import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

import config
from storage import db
from capture.screen import screen_capture_loop
from capture.audio import AudioCaptureService, set_wake_word_callback
from brain.reasoning import (
    reasoning_loop,
    _run_reasoning,
    _handle_result,
    _build_context_summary,
    _build_deep_world_context,
    _build_semantic_memory_context,
)
from brain.interrupt import InterruptDecisionEngine
from on_demand import (
    init_on_demand,
    set_activation_callback,
    set_main_loop as od_set_loop,
)
from actions.approval import set_confirm_callback
from actions.scheduler import init_scheduler, shutdown_scheduler


# ─── Logging ──────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    log_dir = Path.home() / ".marrow"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "marrow.log", encoding="utf-8"),
        ],
    )


log = logging.getLogger("marrow")

# ─── Global asyncio loop reference ────────────────────────────────────────────

_asyncio_loop: Optional[asyncio.AbstractEventLoop] = None
_shutdown_event: Optional[asyncio.Event] = None


def _request_shutdown(*_) -> None:
    """Thread-safe shutdown from any context."""
    if _asyncio_loop and _shutdown_event:
        _asyncio_loop.call_soon_threadsafe(_shutdown_event.set)
    else:
        sys.exit(0)


# ─── Asyncio backend ───────────────────────────────────────────────────────────


async def _main_async() -> None:
    global _shutdown_event

    _shutdown_event = asyncio.Event()

    log.info(f"Starting {config.MARROW_NAME} backend…")

    db.init_db()
    db.prune_old_data(days=7)
    init_scheduler()

    audio_service = AudioCaptureService()
    interrupt_engine = InterruptDecisionEngine()

    # ── Bridge ────────────────────────────────────────────────────────────
    try:
        from ui.bridge import get_bridge

        _bridge = get_bridge()
    except Exception:
        _bridge = None

    def _emit(signal_name: str, *args):
        if _bridge:
            try:
                getattr(_bridge, signal_name).emit(*args)
            except Exception:
                pass

    # ── Approval wiring ───────────────────────────────────────────────────
    async def _request_approval_async(request) -> bool:
        if _bridge:
            return await _bridge.request_approval(
                request.description, request.command[:120]
            )
        log.warning(f"[APPROVAL REQUIRED — no UI] {request.description}: blocked")
        return False

    def _approval_callback(request):
        if _asyncio_loop:
            future = asyncio.run_coroutine_threadsafe(
                _request_approval_async(request), _asyncio_loop
            )
            try:
                return future.result(timeout=35)
            except Exception:
                return False
        return False

    set_confirm_callback(_approval_callback)

    # ── On-demand activation ──────────────────────────────────────────────
    async def on_activation(reason: str) -> None:
        log.info(f"On-demand activation: {reason}")
        _emit("state_changed", "thinking")
        try:
            context = db.get_recent_context(config.CONTEXT_WINDOW_SECONDS)
            ctx_str = _build_context_summary(context)
            deep_world = _build_deep_world_context()
            memory_ctx = await _build_semantic_memory_context(ctx_str)
            full_ctx = "\n\n".join(filter(None, [deep_world, memory_ctx, ctx_str]))
            result = await _run_reasoning(full_ctx)
            if result:
                await _handle_result(result, ctx_str, interrupt_engine)
        except Exception as e:
            log.error(f"On-demand activation error: {e}")
            _emit("state_changed", "error")
        else:
            _emit("state_changed", "idle")

    od_set_loop(_asyncio_loop)
    set_activation_callback(on_activation)
    set_wake_word_callback(on_activation)
    audio_service.set_loop(_asyncio_loop)
    if _bridge:
        _bridge.set_loop(_asyncio_loop)
        _bridge.set_activation_callback(on_activation)
    init_on_demand()

    # ── Patch speak() → bridge signals + toast ────────────────────────────
    try:
        import voice.speak as _speak_mod

        _orig_speak = _speak_mod.speak

        async def _patched_speak(text: str) -> None:
            _emit("state_changed", "speaking")
            # Show toast (text fallback when voice is off or supplemental)
            _emit("toast_requested", config.MARROW_NAME, text[:220], 4)
            await _orig_speak(text)
            _emit("state_changed", "idle")

        _speak_mod.speak = _patched_speak
    except Exception:
        pass

    # ── Patch interrupt engine → bridge message_spoken ────────────────────
    try:
        _orig_record = interrupt_engine.record_spoken

        def _patched_record(candidate):
            _orig_record(candidate)
            _emit("message_spoken", candidate.message, candidate.urgency)

        interrupt_engine.record_spoken = _patched_record
    except Exception:
        pass

    # ── Periodic stats ────────────────────────────────────────────────────
    async def _stats_loop() -> None:
        import json

        while not _shutdown_event.is_set():
            try:
                ctx = db.get_recent_context(3600)
                stats = {
                    "screenshots": len(ctx.get("screenshots", [])),
                    "speaks": len(db.get_recent_actions(limit=200)),
                    "actions": sum(
                        1 for a in db.get_recent_actions(limit=200) if a.get("success")
                    ),
                }
                _emit("stats_updated", json.dumps(stats))
            except Exception:
                pass
            await asyncio.sleep(30)

    # ── Log startup info ──────────────────────────────────────────────────
    try:
        from brain.llm import get_client

        _llm = get_client()
        log.info(f"  Provider  : {_llm.provider} (configured={config.LLM_PROVIDER})")
        log.info(f"  Reasoning : {_llm.model_for('reasoning')}")
    except Exception:
        log.info(f"  Provider  : {config.LLM_PROVIDER}")
        log.info("  Reasoning : unknown")
    log.info(f"  Whisper   : {config.WHISPER_MODEL}")
    log.info(f"  Voice     : {'ElevenLabs' if config.VOICE_ENABLED else 'SAPI/off'}")
    log.info(
        f"  Hotkey    : {config.ON_DEMAND_HOTKEY if config.HOTKEY_ENABLED else 'off'}"
    )
    log.info("Backend running.")

    # ── Signals ───────────────────────────────────────────────────────────
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_shutdown)
        except (OSError, ValueError):
            pass

    # ── Supervised task runner ────────────────────────────────────────────
    async def _supervised(name: str, coro_factory, *args) -> None:
        while not _shutdown_event.is_set():
            try:
                log.info(f"Starting {name}")
                await coro_factory(*args)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"{name} crashed: {e} — restarting in 5s", exc_info=True)
                await asyncio.sleep(5)
        log.info(f"{name} stopped.")

    # ── Wiki: load on startup ─────────────────────────────────────────────
    try:
        from brain.wiki import get_wiki, wiki_update_loop

        get_wiki()  # loads from disk
    except Exception as e:
        log.warning(f"Wiki init failed: {e}")

    # ── AGI: init on startup ──────────────────────────────────────────────
    try:
        from brain.agi import get_agi, agi_loop

        get_agi()  # init singleton
    except Exception as e:
        log.warning(f"AGI init failed: {e}")

    # ── Launch all tasks ──────────────────────────────────────────────────
    tasks = [
        asyncio.create_task(_supervised("screen_capture", screen_capture_loop)),
        asyncio.create_task(_supervised("reasoning", reasoning_loop, interrupt_engine)),
        asyncio.create_task(_supervised("wiki_update", wiki_update_loop)),
        asyncio.create_task(_supervised("agi", agi_loop)),
        asyncio.create_task(_stats_loop()),
        asyncio.create_task(_shutdown_event.wait()),
    ]

    if config.AUDIO_ENABLED:
        tasks.append(
            asyncio.create_task(_supervised("audio_capture", audio_service.run))
        )
    else:
        log.info("Audio capture disabled (AUDIO_ENABLED=0)")

    # Startup welcome — fires 15s in, once per day.
    # NOT added to the monitored tasks list — it completes by design and must
    # not trigger the FIRST_COMPLETED shutdown.
    try:
        from brain.startup import run_startup_sequence

        asyncio.create_task(run_startup_sequence(interrupt_engine))
    except Exception as e:
        log.warning(f"Startup sequence init failed: {e}")

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if config.AUDIO_ENABLED:
        audio_service.stop()
    shutdown_scheduler()
    log.info(f"{config.MARROW_NAME} backend stopped.")


async def _execute_user_task(text: str) -> None:
    """
    Execute a task typed by the user in the dashboard.
    Runs on the asyncio loop, called from Qt thread via run_coroutine_threadsafe.
    """
    from actions.executor import execute_action
    from ui.bridge import get_bridge

    bridge = get_bridge()
    bridge.state_changed.emit("acting")
    try:
        result = await execute_action(text)
        out = (result or "Done.").strip()
        bridge.task_response.emit(out)
        bridge.toast_requested.emit(config.MARROW_NAME, out[:200], 4)
    except Exception as e:
        err = f"Error: {e}"
        log.error(f"User task failed: {e}")
        bridge.task_response.emit(err)
    finally:
        bridge.state_changed.emit("idle")


def _run_asyncio_backend() -> None:
    """Daemon thread that owns the asyncio event loop."""
    global _asyncio_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _asyncio_loop = loop
    try:
        loop.run_until_complete(_main_async())
    except Exception as e:
        log.error(f"Asyncio backend error: {e}", exc_info=True)
    finally:
        loop.close()


# ─── Qt frontend ──────────────────────────────────────────────────────────────


def _build_tray(app, orb):
    """Optional system tray icon."""
    if not config.TRAY_ENABLED:
        return None
    try:
        from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush
        from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

        pix = QPixmap(32, 32)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(16, 16, 20, 255)))
        p.setPen(QColor(210, 210, 225, 160))
        p.drawEllipse(2, 2, 28, 28)
        p.setBrush(QBrush(QColor(210, 210, 225, 220)))
        p.setPen(QColor(0, 0, 0, 0))
        p.drawEllipse(11, 11, 10, 10)
        p.end()

        icon = QSystemTrayIcon(QIcon(pix), app)
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background: rgba(14,14,18,248);
                color: rgba(220,220,230,255);
                border: 1px solid rgba(255,255,255,20);
                border-radius: 8px; padding: 4px; font-size: 9pt;
            }
            QMenu::item { padding: 6px 20px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(96,165,250,80); }
        """)
        menu.addAction("Toggle Dashboard", lambda: orb.dashboard_toggle.emit())
        menu.addSeparator()
        menu.addAction("Quit", lambda: (_request_shutdown(), app.quit()))
        icon.setContextMenu(menu)
        icon.activated.connect(
            lambda r: (
                orb.dashboard_toggle.emit()
                if r == QSystemTrayIcon.ActivationReason.Trigger
                else None
            )
        )
        icon.setToolTip(f"{config.MARROW_NAME} — ambient intelligence")
        icon.show()
        return icon
    except Exception as e:
        log.warning(f"Tray init failed: {e}")
        return None


def _run_qt() -> None:
    """Build and run the Qt application on the main thread."""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt, QTimer

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Marrow")

    # ── Orb (always-there presence) ───────────────────────────────────────
    from ui.orb import MarrowOrb

    orb = MarrowOrb()
    orb.connect_bridge()
    orb.show()

    # ── Dashboard (opens on orb click) ────────────────────────────────────
    from ui.dashboard import MarrowDashboard

    dashboard = MarrowDashboard()

    def _toggle_dashboard():
        if dashboard.isVisible():
            dashboard.hide()
        else:
            dashboard.open_near(orb.geometry())

    orb.dashboard_toggle.connect(_toggle_dashboard)

    # ── Settings panel ────────────────────────────────────────────────────
    settings_panel = [None]  # lazy singleton

    def _show_settings():
        try:
            from ui.settings_panel import MarrowSettingsPanel

            if settings_panel[0] is None:
                settings_panel[0] = MarrowSettingsPanel()
            settings_panel[0].show()
            settings_panel[0].raise_()
        except Exception as e:
            log.warning(f"Settings panel error: {e}")

    orb.settings_requested.connect(_show_settings)
    dashboard.settings_requested.connect(_show_settings)

    # ── Quit ─────────────────────────────────────────────────────────────
    def _quit():
        _request_shutdown()
        app.quit()

    orb.quit_requested.connect(_quit)

    # ── Text task: connect from Qt main thread (required for signal safety) ──
    try:
        from ui.bridge import get_bridge as _get_bridge

        def _on_text_task_qt(text: str):
            if _asyncio_loop:
                asyncio.run_coroutine_threadsafe(
                    _execute_user_task(text), _asyncio_loop
                )

        _get_bridge().text_task_submitted.connect(_on_text_task_qt)
    except Exception as e:
        log.warning(f"Text task wire failed: {e}")

    # ── Approval dialog ───────────────────────────────────────────────────
    try:
        from ui.bridge import get_bridge
        from ui.approval_dialog import show_approval_dialog

        get_bridge().approval_requested.connect(show_approval_dialog)
    except Exception as e:
        log.warning(f"Approval bridge wire failed: {e}")

    # ── Toast notifications ────────────────────────────────────────────────
    try:
        from ui.bridge import get_bridge
        from ui.toast import get_toast_manager

        toast_mgr = get_toast_manager()

        def _on_toast(title: str, body: str, urgency: int):
            toast_mgr.show(title, body, urgency)

        get_bridge().toast_requested.connect(_on_toast)

        # Also wire message_spoken → toast (shows what Marrow said visually)
        def _on_message_spoken(text: str, urgency: int):
            toast_mgr.show(config.MARROW_NAME, text, urgency)

        get_bridge().message_spoken.connect(_on_message_spoken)

    except Exception as e:
        log.warning(f"Toast wire failed: {e}")

    # ── Backend shutdown → app quit ───────────────────────────────────────
    def _check_backend():
        if _asyncio_loop and _asyncio_loop.is_closed():
            app.quit()

    checker = QTimer()
    checker.timeout.connect(_check_backend)
    checker.start(1000)

    # ── Tray ─────────────────────────────────────────────────────────────
    _build_tray(app, orb)

    sys.exit(app.exec())


# ─── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    _setup_logging()

    # Start asyncio backend in a daemon thread first
    backend = threading.Thread(
        target=_run_asyncio_backend,
        name="marrow-asyncio",
        daemon=True,
    )
    backend.start()

    # Small delay to let the loop start and assign _asyncio_loop
    import time

    time.sleep(0.15)

    # Qt runs on the main thread (required on Windows)
    _run_qt()


if __name__ == "__main__":
    main()

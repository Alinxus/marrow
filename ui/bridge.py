"""
Qt ↔ asyncio bridge.

The asyncio event loop runs in a daemon thread. Qt runs in the main thread.
Qt signals are thread-safe, so asyncio code emits signals to update the UI.

For the reverse (UI → asyncio), we schedule coroutines using
asyncio.run_coroutine_threadsafe(coro, loop) after storing the loop reference.

Approval flow uses threading.Event so asyncio can await a modal Qt dialog
without blocking the event loop thread.

Usage (asyncio side):
    bridge = get_bridge()
    bridge.state_changed.emit("thinking")
    bridge.message_spoken.emit("You have a meeting at 3pm", 2)

Usage (Qt side):
    bridge = get_bridge()
    bridge.state_changed.connect(panel.on_state_changed)
"""

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class MarrowBridge(QObject):
    """
    Thread-safe event bus between asyncio backend and Qt frontend.

    All signals are emitted from the asyncio thread and processed safely
    by Qt's signal/slot mechanism on the main thread.
    """

    # ── Backend → UI signals ──────────────────────────────────────────────
    # Marrow's internal state: "idle" | "thinking" | "speaking" | "acting" | "error"
    state_changed = Signal(str)

    # A message was spoken: (text, urgency 1-5)
    message_spoken = Signal(str, int)

    # The screen focus changed: (app_name, window_title)
    focus_changed = Signal(str, str)

    # Reasoning trace update (full JSON dict as string for thread safety)
    reasoning_update = Signal(str)

    # World model: list of (type, content) pairs as JSON string
    world_model_updated = Signal(str)

    # Stats: JSON string with counts
    stats_updated = Signal(str)

    # An approval request arrived: (description, command, callback_id)
    # The UI shows a dialog and calls respond_to_approval(callback_id, bool)
    approval_requested = Signal(str, str, str)

    # Notification (title, body)
    notify = Signal(str, str)

    # Show a toast card: (title, body, urgency 1-5)
    toast_requested = Signal(str, str, int)

    # Audio transcript heard: (text,)
    transcript_heard = Signal(str)

    # Mic status: True = actively listening, False = off/unavailable
    mic_active = Signal(bool)

    # Task result from executor: (result_text,)
    task_response = Signal(str)

    # Claim verified: JSON string {claim, verdict, explanation, sources, confidence}
    claim_verified = Signal(str)

    # Mission runtime update: JSON string
    mission_update = Signal(str)

    # Agent / swarm update: JSON string
    agent_update = Signal(str)

    # Overlay payload update: JSON string
    overlay_update = Signal(str)

    # Verification payload update: JSON string
    verification_update = Signal(str)

    # Audio diagnostics / live status
    audio_debug = Signal(str)

    # Rich screen/perception snapshot payload: JSON string
    perception_update = Signal(str)

    # Deep reasoning workbench payload: JSON string
    deep_reasoning_update = Signal(str)

    # ── UI → Backend signals ──────────────────────────────────────────────
    # User pressed "Ask Marrow" button — triggers on-demand activation
    ask_requested = Signal()

    # User submitted a text task from the dashboard input
    text_task_submitted = Signal(str)

    def __init__(self):
        super().__init__()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._activation_callback: Optional[Callable] = None
        # pending approval callbacks: id → threading.Event + result holder
        self._pending_approvals: dict[str, tuple[threading.Event, list]] = {}

    # ── Loop reference (set from asyncio thread before first use) ─────────

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_activation_callback(self, callback: Callable) -> None:
        """Register the async on_activation(reason) coroutine."""
        self._activation_callback = callback

    # ── UI → asyncio dispatch ─────────────────────────────────────────────

    def trigger_activation(self, reason: str = "ui_button") -> None:
        """Called from Qt thread — schedules on_activation on the asyncio loop."""
        if self._loop and self._activation_callback:
            asyncio.run_coroutine_threadsafe(
                self._activation_callback(reason), self._loop
            )
        else:
            log.warning("trigger_activation: no loop or callback set")

    # ── Approval flow ─────────────────────────────────────────────────────

    async def request_approval(
        self, description: str, command: str, timeout: float = 30.0
    ) -> bool:
        """
        Called from asyncio thread. Emits approval_requested signal (Qt shows dialog),
        then waits (in thread executor, not blocking event loop) for user response.
        """
        import uuid as _uuid
        callback_id = str(_uuid.uuid4())
        event = threading.Event()
        result: list[bool] = [False]
        self._pending_approvals[callback_id] = (event, result)

        self.approval_requested.emit(description, command, callback_id)

        loop = asyncio.get_running_loop()
        approved = await loop.run_in_executor(
            None, lambda: event.wait(timeout)
        )

        del self._pending_approvals[callback_id]
        return result[0] if approved else False

    def respond_to_approval(self, callback_id: str, approved: bool) -> None:
        """Called from Qt thread when user clicks Yes/No in approval dialog."""
        if callback_id in self._pending_approvals:
            event, result = self._pending_approvals[callback_id]
            result[0] = approved
            event.set()


# ─── Module-level singleton ────────────────────────────────────────────────────

_bridge: Optional[MarrowBridge] = None


def get_bridge() -> MarrowBridge:
    global _bridge
    if _bridge is None:
        _bridge = MarrowBridge()
    return _bridge

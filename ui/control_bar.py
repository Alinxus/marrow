"""Unified floating control bar (Omi-style) for Marrow.

Compact always-on-top bar with hover expansion, embedded proactive cards,
and slide-down conversation panel.
"""

import json
import logging
from typing import Optional

from PyQt6.QtCore import (
    QPoint,
    QRectF,
    Qt,
    QPropertyAnimation,
    QEasingCurve,
    QTimer,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.dashboard import ChatSection, MiniOrb, MicDot, _lbl

log = logging.getLogger(__name__)


class ProactiveCard(QWidget):
    """Embedded notification card under the control bar."""

    def __init__(
        self, text: str, urgency: int, open_callback, dismiss_callback, parent=None
    ):
        super().__init__(parent)
        self._text = text
        self._urgency = max(1, min(5, urgency))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        icon = QLabel("🔔")
        icon.setStyleSheet("font-size: 12px;")
        lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        body_col = QVBoxLayout()
        body_col.setContentsMargins(0, 0, 0, 0)
        body_col.setSpacing(2)
        body_col.addWidget(
            _lbl("Proactive Insight", QColor(37, 99, 235), 8, bold=True)
        )
        txt = _lbl(
            text[:130] + ("…" if len(text) > 130 else ""), QColor(40, 40, 58), 8
        )
        txt.setWordWrap(True)
        body_col.addWidget(txt)
        body_col.addWidget(_lbl(f"urgency {self._urgency}", QColor(110, 110, 130), 7))
        lay.addLayout(body_col, 1)

        open_btn = QPushButton("Open")
        open_btn.setFixedSize(48, 22)
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setStyleSheet(
            "QPushButton { background: rgba(37,99,235,18); color: rgba(37,99,235,220);"
            " border: 1px solid rgba(37,99,235,60); border-radius: 11px; font-size: 8pt; font-weight: 600; }"
            "QPushButton:hover { background: rgba(37,99,235,32); }"
        )
        open_btn.clicked.connect(open_callback)
        lay.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignTop)

        x_btn = QPushButton("×")
        x_btn.setFixedSize(18, 18)
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(120,120,140,180); border: none; }"
            "QPushButton:hover { color: rgba(12,12,22,220); }"
        )
        x_btn.clicked.connect(dismiss_callback)
        lay.addWidget(x_btn, 0, Qt.AlignmentFlag.AlignTop)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setBrush(QColor(245, 247, 255, 210))
        p.setPen(QPen(QColor(255, 255, 255, 190), 1.0))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        p.setPen(QPen(QColor(0, 0, 0, 12), 0.5))
        p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), 9, 9)


class MarrowControlBar(QWidget):
    """Single floating bar with hover-expand, notifications, and chat panel."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._drag_pos: Optional[QPoint] = None
        self._expanded = False
        self._pinned = False
        self._body_height = 0
        self._last_proactive_text = ""
        self._state = "idle"
        self._busy_phase = 0
        self._settings_panel = None

        self.setFixedWidth(380)
        self._build_ui()
        self._connect_bridge()
        self._position_default()

        self._anim = QPropertyAnimation(self, b"bodyHeight", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(350)
        self._busy_timer.timeout.connect(self._tick_busy)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._top = QWidget()
        self._top.setFixedHeight(52)
        top_l = QHBoxLayout(self._top)
        top_l.setContentsMargins(12, 0, 10, 0)
        top_l.setSpacing(7)

        self._orb = MiniOrb(10)
        top_l.addWidget(self._orb)
        top_l.addWidget(_lbl("MARROW", QColor(12, 12, 22), 10, bold=True))

        self._focus_lbl = _lbl("idle", QColor(90, 90, 108), 8)
        top_l.addWidget(self._focus_lbl)

        self._state_lbl = _lbl("idle", QColor(120, 120, 140), 8)
        top_l.addWidget(self._state_lbl)
        top_l.addStretch()

        self._mic = MicDot()
        top_l.addWidget(self._mic)

        ask_btn = QPushButton("Ask")
        ask_btn.setFixedSize(44, 24)
        ask_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ask_btn.setStyleSheet(
            "QPushButton { background: rgba(37,99,235,210); color: white; border: none; border-radius: 12px; font-size: 8pt; font-weight: 600; }"
            "QPushButton:hover { background: rgba(37,99,235,245); }"
        )
        ask_btn.clicked.connect(self._ask)
        top_l.addWidget(ask_btn)

        pin_btn = QPushButton("▾")
        pin_btn.setFixedSize(20, 20)
        pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pin_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(100,100,120,190); border: none; }"
            "QPushButton:hover { color: rgba(12,12,22,220); }"
        )
        pin_btn.clicked.connect(self.toggle_expand)
        top_l.addWidget(pin_btn)

        set_btn = QPushButton("⚙")
        set_btn.setFixedSize(20, 20)
        set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(100,100,120,190); border: none; }"
            "QPushButton:hover { color: rgba(12,12,22,220); }"
        )
        set_btn.clicked.connect(self.open_settings)
        top_l.addWidget(set_btn)

        root.addWidget(self._top)

        self._body = QWidget()
        self._body.setFixedHeight(0)
        body_l = QVBoxLayout(self._body)
        body_l.setContentsMargins(10, 8, 10, 10)
        body_l.setSpacing(8)

        self._notif_wrap = QScrollArea()
        self._notif_wrap.setWidgetResizable(True)
        self._notif_wrap.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._notif_wrap.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._notif_wrap.setMaximumHeight(130)
        self._notif_wrap.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 3px; background: transparent; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,30); border-radius: 2px; }"
        )
        self._notif_box = QWidget()
        self._notif_l = QVBoxLayout(self._notif_box)
        self._notif_l.setContentsMargins(0, 0, 0, 0)
        self._notif_l.setSpacing(6)
        self._notif_wrap.setWidget(self._notif_box)
        body_l.addWidget(self._notif_wrap)

        self._mission_lbl = _lbl("", QColor(50, 60, 90), 8, bold=True)
        self._mission_lbl.setWordWrap(True)
        self._mission_lbl.setVisible(False)
        body_l.addWidget(self._mission_lbl)

        self._perception_lbl = _lbl("", QColor(70, 70, 98), 8)
        self._perception_lbl.setWordWrap(True)
        self._perception_lbl.setVisible(False)
        body_l.addWidget(self._perception_lbl)

        self._agent_lbl = _lbl("", QColor(88, 80, 40), 8)
        self._agent_lbl.setWordWrap(True)
        self._agent_lbl.setVisible(False)
        body_l.addWidget(self._agent_lbl)

        self._audio_lbl = _lbl("", QColor(60, 90, 60), 8)
        self._audio_lbl.setWordWrap(True)
        self._audio_lbl.setVisible(False)
        body_l.addWidget(self._audio_lbl)

        self._chat = ChatSection()
        self._chat.task_submitted.connect(self._on_task_submitted)
        body_l.addWidget(self._chat)

        root.addWidget(self._body)

    def _connect_bridge(self):
        try:
            from ui.bridge import get_bridge

            b = get_bridge()
            b.state_changed.connect(self._on_state)
            b.focus_changed.connect(self._on_focus)
            b.mic_active.connect(self._mic.set_active)
            b.message_spoken.connect(self._on_message_spoken)
            b.transcript_heard.connect(self._on_transcript_heard)
            b.task_response.connect(self._on_task_response)
            b.mission_update.connect(self._on_mission_update)
            b.agent_update.connect(self._on_agent_update)
            b.audio_debug.connect(self._on_audio_debug)
            b.perception_update.connect(self._on_perception_update)
        except Exception as e:
            log.warning(f"Control bar bridge failed: {e}")

    def _position_default(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 18, screen.bottom() - 72)

    def _on_state(self, state: str):
        self._state = state
        self._orb.set_state(state)
        self._state_lbl.setText(state)

        is_busy = state in ("thinking", "acting")
        if is_busy and not self._busy_timer.isActive():
            self._busy_phase = 0
            self._busy_timer.start()
        if not is_busy and self._busy_timer.isActive():
            self._busy_timer.stop()

        self._chat._input.setEnabled(not is_busy)
        if state == "acting":
            self._chat._input.setPlaceholderText("Executing action…")
        elif state == "thinking":
            self._chat._input.setPlaceholderText("Thinking…")
        else:
            self._chat._input.setPlaceholderText("Ask or tell Marrow anything…")

    def _tick_busy(self):
        if self._state not in ("thinking", "acting"):
            return
        self._busy_phase = (self._busy_phase + 1) % 4
        dots = "." * self._busy_phase
        self._state_lbl.setText(f"{self._state}{dots}")

    def _on_focus(self, app: str, title: str):
        txt = app or "idle"
        if title:
            txt += f" · {title[:26]}"
        self._focus_lbl.setText(txt)

    def _on_message_spoken(self, text: str, urgency: int):
        self._last_proactive_text = text
        self._add_proactive_card(text, urgency)
        self._expand(transient=True)

    def _add_proactive_card(self, text: str, urgency: int):
        def _open():
            self.open_with_notification_context(text)

        card = ProactiveCard(text, urgency, _open, lambda: self._remove_card(card))
        self._notif_l.insertWidget(0, card)
        while self._notif_l.count() > 3:
            item = self._notif_l.itemAt(self._notif_l.count() - 1)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

    def _remove_card(self, card: QWidget):
        card.setParent(None)
        card.deleteLater()

    def _on_task_submitted(self, text: str):
        self._chat.set_thinking()
        try:
            from ui.bridge import get_bridge

            get_bridge().text_task_submitted.emit(text)
        except Exception as e:
            log.warning(f"Task dispatch failed: {e}")

    def _on_task_response(self, result: str):
        self._chat.replace_last_marrow(result or "Done.")

    def _on_mission_update(self, payload_json: str):
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        step = payload.get("step") or {}
        text = f"{payload.get('state', 'idle')}: {step.get('title') or payload.get('goal', '')}"
        self._mission_lbl.setText(text[:120])
        self._mission_lbl.setVisible(True)
        if payload.get("state") in {"planning", "executing", "verifying"}:
            self._expand(transient=True)

    def _on_agent_update(self, payload_json: str):
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        role = payload.get("role")
        status = payload.get("status")
        if role and status:
            self._agent_lbl.setText(f"{role}: {status}")
            self._agent_lbl.setVisible(True)

    def _on_audio_debug(self, message: str):
        self._audio_lbl.setText(message[:120])
        self._audio_lbl.setVisible(True)

    def _on_transcript_heard(self, text: str):
        heard = f'heard: "{text[:80]}"'
        self._audio_lbl.setText(heard)
        self._audio_lbl.setVisible(True)

    def _on_perception_update(self, payload_json: str):
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        focused = (payload.get("focused_context") or "").strip()
        summary = " ".join(((payload.get("summary") or "").split()))
        source = payload.get("source") or ""
        parts = []
        if focused:
            parts.append(focused[:80])
        if summary:
            parts.append(summary[:140])
        if source:
            parts.append(source)
        text = " · ".join(parts[:3])
        if text:
            self._perception_lbl.setText(text)
            self._perception_lbl.setVisible(True)

    def _ask(self):
        try:
            from ui.bridge import get_bridge

            get_bridge().trigger_activation("ui_control_bar")
        except Exception as e:
            log.warning(f"Ask trigger failed: {e}")

    def open_settings(self):
        try:
            from ui.settings_panel import MarrowSettingsPanel

            # Keep strong reference so window is not garbage-collected on macOS.
            if self._settings_panel is None:
                self._settings_panel = MarrowSettingsPanel(self)
            self._settings_panel.show()
            self._settings_panel.raise_()
            self._settings_panel.activateWindow()
        except Exception as e:
            log.warning(f"Settings open failed: {e}")

    def open_with_notification_context(self, text: Optional[str] = None):
        msg = text or self._last_proactive_text
        if msg:
            self._chat.add_message("marrow", msg)
            self._chat._input.setPlaceholderText("Ask a follow-up about this insight…")
        self._expand()
        self.show()
        self.raise_()
        self._chat._input.setFocus()

    def toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def toggle_expand(self):
        self._pinned = not self._pinned
        if self._expanded:
            self._collapse(force=True)
        else:
            self._expand()

    def _expand(self, transient: bool = False):
        if self._expanded:
            return
        self._expanded = True
        self._anim.stop()
        self._anim.setStartValue(self._body_height)
        self._anim.setEndValue(300)
        self._anim.start()
        if transient and not self._pinned:
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(6000, lambda: self._collapse(force=False))

    def _collapse(self, force: bool = False):
        if not self._expanded:
            return
        if self._pinned and not force:
            return
        self._expanded = False
        self._anim.stop()
        self._anim.setStartValue(self._body_height)
        self._anim.setEndValue(0)
        self._anim.start()

    def getBodyHeight(self):
        return self._body_height

    def setBodyHeight(self, h):
        self._body_height = int(max(0, h))
        self._body.setFixedHeight(self._body_height)
        self.setFixedHeight(52 + self._body_height)

    bodyHeight = pyqtProperty(int, fget=getBodyHeight, fset=setBodyHeight)

    def enterEvent(self, _):
        self._expand()

    def leaveEvent(self, _):
        self._collapse(force=False)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), 16, 16)

        # Soft shadow
        sp = QPainterPath()
        sp.addRoundedRect(r.adjusted(-1, 4, 1, 5), 16, 16)
        p.fillPath(sp, QColor(0, 0, 0, 18))

        # White glass fill
        p.fillPath(path, QColor(255, 255, 255, 218))

        # Top shimmer
        from PyQt6.QtGui import QLinearGradient
        shimmer = QLinearGradient(0, 0, 0, 30)
        shimmer.setColorAt(0.0, QColor(255, 255, 255, 90))
        shimmer.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, shimmer)

        # Borders
        p.setPen(QPen(QColor(255, 255, 255, 200), 1.2))
        p.drawPath(path)
        p.setPen(QPen(QColor(0, 0, 0, 14), 0.7))
        inner = QPainterPath()
        inner.addRoundedRect(r.adjusted(2.5, 2.5, -2.5, -2.5), 15, 15)
        p.drawPath(inner)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.pos().y() <= 52:
            child = self.childAt(event.pos())
            if isinstance(child, (QPushButton, QLineEdit, QScrollArea)):
                self._drag_pos = None
                return super().mousePressEvent(event)
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            return
        return super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            return
        return super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        return super().mouseReleaseEvent(event)

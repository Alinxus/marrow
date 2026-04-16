"""Unified floating control bar (Omi-style) for Marrow.

Compact always-on-top bar with hover expansion, embedded proactive cards,
and slide-down conversation panel.
"""

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
            _lbl("Proactive Insight", QColor(232, 232, 238), 8, bold=True)
        )
        txt = _lbl(
            text[:130] + ("…" if len(text) > 130 else ""), QColor(175, 175, 185), 8
        )
        txt.setWordWrap(True)
        body_col.addWidget(txt)
        body_col.addWidget(_lbl(f"urgency {self._urgency}", QColor(110, 110, 122), 7))
        lay.addLayout(body_col, 1)

        open_btn = QPushButton("Open")
        open_btn.setFixedSize(48, 22)
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,16); color: rgba(235,235,240,240);"
            " border: 1px solid rgba(255,255,255,24); border-radius: 11px; font-size: 8pt; }"
            "QPushButton:hover { background: rgba(255,255,255,26); }"
        )
        open_btn.clicked.connect(open_callback)
        lay.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignTop)

        x_btn = QPushButton("×")
        x_btn.setFixedSize(18, 18)
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(150,150,160,200); border: none; }"
            "QPushButton:hover { color: rgba(235,235,245,255); }"
        )
        x_btn.clicked.connect(dismiss_callback)
        lay.addWidget(x_btn, 0, Qt.AlignmentFlag.AlignTop)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setBrush(QColor(20, 20, 28, 214))
        p.setPen(QPen(QColor(255, 255, 255, 24), 1.0))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)


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
        top_l.addWidget(_lbl("MARROW", QColor(236, 236, 242), 10, bold=True))

        self._focus_lbl = _lbl("idle", QColor(125, 125, 136), 8)
        top_l.addWidget(self._focus_lbl)

        self._state_lbl = _lbl("idle", QColor(160, 160, 172), 8)
        top_l.addWidget(self._state_lbl)
        top_l.addStretch()

        self._mic = MicDot()
        top_l.addWidget(self._mic)

        ask_btn = QPushButton("Ask")
        ask_btn.setFixedSize(44, 24)
        ask_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ask_btn.setStyleSheet(
            "QPushButton { background: rgba(96,165,250,180); color: white; border: none; border-radius: 12px; font-size: 8pt; }"
            "QPushButton:hover { background: rgba(96,165,250,220); }"
        )
        ask_btn.clicked.connect(self._ask)
        top_l.addWidget(ask_btn)

        pin_btn = QPushButton("▾")
        pin_btn.setFixedSize(20, 20)
        pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pin_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(170,170,180,220); border: none; }"
        )
        pin_btn.clicked.connect(self.toggle_expand)
        top_l.addWidget(pin_btn)

        set_btn = QPushButton("⚙")
        set_btn.setFixedSize(20, 20)
        set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(170,170,180,220); border: none; }"
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
            b.task_response.connect(self._on_task_response)
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
        path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        p.fillPath(path, QColor(12, 12, 16, 246))
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.drawPath(path)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.pos().y() <= 52:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, _):
        self._drag_pos = None

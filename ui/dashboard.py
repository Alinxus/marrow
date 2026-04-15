"""
Marrow Dashboard — the full information panel.

Opens/closes when the orb is clicked.

Sections:
  Header     — status orb, name, mic indicator, close
  Watching   — what app/window Marrow sees
  Last msg   — what Marrow last said, when
  World      — what Marrow has learned about you
  ─────────────────────────────────────
  Chat       — conversation history (what you typed, what Marrow did)
  Input bar  — type a task or question, hit Enter or click Send

The chat/input section is the text-based alternative to speaking.
Type anything: "check my emails", "what's on my calendar", "open Spotify",
"summarize that PDF", "remind me at 3pm", etc.
"""

import json
import logging
import time
from typing import Optional

from PyQt6.QtCore import (
    QPoint, QPointF, QRectF, QRect, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter,
    QPainterPath, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# ─── Palette ──────────────────────────────────────────────────────────────────

BG          = QColor(10, 10, 14, 248)
BG_SECTION  = QColor(18, 18, 24, 180)
BG_INPUT    = QColor(22, 22, 28, 220)
BORDER      = QColor(255, 255, 255, 22)
TEXT_PRI    = QColor(240, 240, 245)
TEXT_SEC    = QColor(160, 160, 170)
TEXT_DIM    = QColor(80,  80,  90)
TEXT_USER   = QColor(180, 180, 190)
TEXT_MARROW = QColor(235, 235, 242)   # white — Marrow's text
ACCENT      = QColor(220, 220, 232)   # silver-white accent
ACCENT_G    = QColor(74,  222, 128)
ACCENT_A    = QColor(251, 191, 36)
MIC_ON      = QColor(74,  222, 128)
MIC_OFF     = QColor(72,  72,  82)

DASH_W      = 360
DASH_H      = 620
RADIUS      = 14
MAX_HISTORY = 20    # max chat exchanges to keep in memory


def _color_state(state: str) -> QColor:
    return {
        "idle":     QColor(90, 90, 100),
        "thinking": ACCENT_A,
        "speaking": ACCENT,
        "acting":   ACCENT_G,
        "error":    QColor(248, 113, 113),
    }.get(state, QColor(90, 90, 100))


def _lbl(text: str, color: QColor = TEXT_SEC, pt: int = 9,
         bold: bool = False) -> QLabel:
    w = QLabel(text)
    f = QFont()
    f.setPointSize(pt)
    f.setBold(bold)
    w.setFont(f)
    w.setStyleSheet(
        f"color: rgba({color.red()},{color.green()},{color.blue()},{color.alpha()});"
        " background: transparent;"
    )
    return w


def _sep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: rgba(255,255,255,10);")
    line.setFixedHeight(1)
    return line


def _section_label(text: str) -> QLabel:
    lbl = _lbl(text.upper(), TEXT_DIM, 7, bold=True)
    lbl.setContentsMargins(0, 6, 0, 2)
    return lbl


# ─── Animated status dot ─────────────────────────────────────────────────────

class MiniOrb(QWidget):
    def __init__(self, size: int = 10, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._size  = size
        self._state = "idle"
        self._glow  = 0.5
        self._t     = 0.0
        t = QTimer(self)
        t.setInterval(40)
        t.timeout.connect(self._tick)
        t.start()

    def set_state(self, s: str):
        self._state = s
        self._t = 0.0

    def _tick(self):
        speeds = {"idle": 0.4, "thinking": 2.5, "speaking": 3.5, "acting": 2.0, "error": 6.0}
        self._t += speeds.get(self._state, 1.0) * 0.04
        import math
        self._glow = (math.sin(self._t) + 1.0) / 2.0
        self.update()

    def paintEvent(self, _):
        c = _color_state(self._state)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._size
        dot = QColor(c)
        dot.setAlpha(160 + int(80 * self._glow))
        p.setBrush(dot)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, s - 2, s - 2)


# ─── Mic indicator dot ────────────────────────────────────────────────────────

class MicDot(QWidget):
    """Small dot: green = listening, dim = off."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(8, 8)
        self._on = False
        self._t  = 0.0
        self._glow = 0.5
        t = QTimer(self)
        t.setInterval(50)
        t.timeout.connect(self._tick)
        t.start()

    def set_active(self, on: bool):
        self._on = on
        self.update()

    def _tick(self):
        import math
        self._t   += 0.1 if self._on else 0.03
        self._glow = (math.sin(self._t) + 1.0) / 2.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(MIC_ON if self._on else MIC_OFF)
        c.setAlpha(180 + int(60 * self._glow) if self._on else 80)
        p.setBrush(c)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, 6, 6)


# ─── Chat bubble ─────────────────────────────────────────────────────────────

class ChatBubble(QWidget):
    """Single message in the conversation (user or Marrow)."""

    def __init__(self, text: str, role: str, parent=None):
        super().__init__(parent)
        self.role = role          # "user" or "marrow"
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(1)

        is_user = role == "user"

        who = _lbl("You" if is_user else "Marrow",
                   TEXT_USER if is_user else TEXT_MARROW,
                   pt=7, bold=True)
        lay.addWidget(who)

        self._body = QLabel(text)
        f = QFont()
        f.setPointSize(9)
        self._body.setFont(f)
        col = TEXT_SEC if is_user else TEXT_PRI
        self._body.setStyleSheet(
            f"color: rgba({col.red()},{col.green()},{col.blue()},220);"
            " background: transparent;"
        )
        self._body.setWordWrap(True)
        self._body.setMaximumWidth(DASH_W - 44)
        lay.addWidget(self._body)

    def set_text(self, text: str):
        self._body.setText(text)


# ─── Chat section ─────────────────────────────────────────────────────────────

class ChatSection(QWidget):
    """Conversation history + text input."""

    task_submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._history: list[tuple[str, str]] = []   # (role, text)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # ── History scroll area ───────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setMinimumHeight(120)
        self._scroll.setMaximumHeight(180)
        self._scroll.setStyleSheet("""
            QScrollArea { background: rgba(16,16,22,160); border: 1px solid rgba(255,255,255,10);
                          border-radius: 8px; }
            QScrollBar:vertical { background: transparent; width: 3px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,25); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        self._chat_widget = QWidget()
        self._chat_widget.setStyleSheet("background: transparent;")
        self._chat_lay = QVBoxLayout(self._chat_widget)
        self._chat_lay.setContentsMargins(10, 8, 10, 8)
        self._chat_lay.setSpacing(6)
        self._chat_lay.addStretch()

        self._empty_lbl = _lbl(
            "Type a task below — or say \"Hey Marrow\"",
            TEXT_DIM, 8
        )
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chat_lay.addWidget(self._empty_lbl)

        self._scroll.setWidget(self._chat_widget)
        outer.addWidget(self._scroll)

        # ── Input row ─────────────────────────────────────────────────────
        input_row = QHBoxLayout()
        input_row.setSpacing(6)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Ask or tell Marrow anything…")
        self._input.setFixedHeight(34)
        self._input.setStyleSheet("""
            QLineEdit {
                background: rgba(22, 22, 30, 200);
                color: rgba(235, 235, 242, 255);
                border: 1px solid rgba(255,255,255,18);
                border-radius: 8px;
                padding: 0 10px;
                font-size: 9pt;
            }
            QLineEdit:focus {
                border: 1px solid rgba(220,220,232,90);
                background: rgba(28, 28, 38, 240);
            }
        """)
        self._input.returnPressed.connect(self._submit)
        input_row.addWidget(self._input)

        send_btn = QPushButton("↑")
        send_btn.setFixedSize(34, 34)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet("""
            QPushButton {
                background: rgba(220,220,232,30);
                color: rgba(220,220,232,255);
                border: 1px solid rgba(220,220,232,60);
                border-radius: 8px; font-size: 14px; font-weight: bold;
            }
            QPushButton:hover { background: rgba(220,220,232,55); }
            QPushButton:pressed { background: rgba(220,220,232,80); }
        """)
        send_btn.clicked.connect(self._submit)
        input_row.addWidget(send_btn)

        outer.addLayout(input_row)

    def _submit(self):
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.add_message("user", text)
        self.task_submitted.emit(text)

    def add_message(self, role: str, text: str):
        """Add a bubble to the conversation. thread-safe via Qt main thread."""
        # Remove empty state label
        if self._empty_lbl.parent():
            self._empty_lbl.setParent(None)

        # Remove stretch so new items go at the end
        # (we always add at bottom, stretch was at top)
        bubble = ChatBubble(text, role)
        self._chat_lay.addWidget(bubble)

        self._history.append((role, text))

        # Trim
        if len(self._history) > MAX_HISTORY:
            oldest = self._history.pop(0)
            # Remove the oldest bubble widget
            item = self._chat_lay.itemAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        # Scroll to bottom
        QTimer.singleShot(
            50,
            lambda: self._scroll.verticalScrollBar().setValue(
                self._scroll.verticalScrollBar().maximum()
            ),
        )

    def set_thinking(self):
        """Add a "thinking…" placeholder that gets replaced by response."""
        self.add_message("marrow", "…")

    def replace_last_marrow(self, text: str):
        """Replace the last Marrow bubble (thinking '…' → real response)."""
        for i in range(self._chat_lay.count() - 1, -1, -1):
            item = self._chat_lay.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if isinstance(w, ChatBubble) and w.role == "marrow":
                w.set_text(text)
                if self._history:
                    self._history[-1] = ("marrow", text)
                return
        # No placeholder found — just add
        self.add_message("marrow", text)


# ─── Info sections ────────────────────────────────────────────────────────────

class WatchingSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(_section_label("Watching"))
        self._app   = _lbl("—", TEXT_PRI, 10, bold=True)
        self._title = _lbl("", TEXT_SEC, 9)
        self._title.setWordWrap(True)
        lay.addWidget(self._app)
        lay.addWidget(self._title)

    def update_focus(self, app: str, title: str):
        self._app.setText(app or "—")
        self._title.setText(title[:60] if title else "")


class MessageSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(_section_label("Last Proactive Message"))
        self._msg  = _lbl("Nothing yet", TEXT_SEC, 9)
        self._msg.setWordWrap(True)
        self._meta = _lbl("", TEXT_DIM, 8)
        lay.addWidget(self._msg)
        lay.addWidget(self._meta)
        self._ts = 0.0

    def update_message(self, text: str, urgency: int = 3):
        self._ts = time.time()
        self._msg.setText(text[:180] if text else "")
        self._meta.setText(f"just now · urgency {urgency}")

    def tick(self):
        if not self._ts:
            return
        diff = int(time.time() - self._ts)
        self._meta.setText(f"{diff}s ago" if diff < 60 else f"{diff // 60}m ago")


class StatsSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(_section_label("Activity"))
        self._line = _lbl("—", TEXT_SEC, 9)
        lay.addWidget(self._line)

    def update_stats(self, json_str: str):
        try:
            d = json.loads(json_str)
            screens = d.get("screenshots", 0)
            actions = d.get("actions", 0)
            speaks  = d.get("speaks", 0)
            self._line.setText(
                f"{screens} screens captured · {speaks} messages · {actions} actions"
            )
        except Exception:
            pass


# ─── Main dashboard ───────────────────────────────────────────────────────────

class MarrowDashboard(QWidget):
    """The full information panel — opened/closed by the orb."""

    settings_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(DASH_W, DASH_H)

        self._state    = "idle"
        self._drag_pos: Optional[QPoint] = None
        self._pending_response = False

        self._build()
        self._connect_bridge()

        self._clock = QTimer(self)
        self._clock.setInterval(10_000)
        self._clock.timeout.connect(self._tick_meta)
        self._clock.start()

    # ── Build layout ──────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        self._orb = MiniOrb(10)
        hdr.addWidget(self._orb)

        title = _lbl("MARROW", TEXT_PRI, 10, bold=True)
        hdr.addWidget(title)
        hdr.addStretch()

        # Mic indicator
        self._mic_dot  = MicDot()
        self._mic_lbl  = _lbl("mic off", TEXT_DIM, 7)
        hdr.addWidget(self._mic_dot)
        hdr.addSpacing(3)
        hdr.addWidget(self._mic_lbl)
        hdr.addSpacing(6)

        self._state_lbl = _lbl("idle", TEXT_DIM, 8)
        hdr.addWidget(self._state_lbl)

        set_btn = QPushButton("⚙")
        set_btn.setFixedSize(20, 20)
        set_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_btn.setStyleSheet("""
            QPushButton { background: transparent; color: rgba(100,100,110,180);
                          border: none; font-size: 11px; }
            QPushButton:hover { color: rgba(200,200,210,255); }
        """)
        set_btn.clicked.connect(self.settings_requested.emit)
        hdr.addWidget(set_btn)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: rgba(100,100,110,180);
                          border: none; font-size: 14px; }
            QPushButton:hover { color: rgba(220,220,230,255); }
        """)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)

        root.addLayout(hdr)
        root.addSpacing(8)
        root.addWidget(_sep())
        root.addSpacing(6)

        # ── Info sections (in a scroll area) ──────────────────────────────
        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        info_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        info_scroll.setFixedHeight(220)
        info_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 3px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,25); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        info_widget = QWidget()
        info_widget.setStyleSheet("background: transparent;")
        info_lay = QVBoxLayout(info_widget)
        info_lay.setContentsMargins(0, 0, 4, 0)
        info_lay.setSpacing(6)

        self._watching = WatchingSection()
        info_lay.addWidget(self._watching)
        info_lay.addWidget(_sep())

        self._message = MessageSection()
        info_lay.addWidget(self._message)
        info_lay.addWidget(_sep())

        self._stats = StatsSection()
        info_lay.addWidget(self._stats)
        info_lay.addStretch()

        info_scroll.setWidget(info_widget)
        root.addWidget(info_scroll)

        root.addSpacing(8)
        root.addWidget(_sep())
        root.addSpacing(6)

        # ── Chat section ──────────────────────────────────────────────────
        chat_hdr = QHBoxLayout()
        chat_hdr.setSpacing(6)
        chat_hdr.addWidget(_section_label("Conversation"))
        chat_hdr.addStretch()
        self._listen_lbl = _lbl("", TEXT_DIM, 7)
        chat_hdr.addWidget(self._listen_lbl)
        root.addLayout(chat_hdr)
        root.addSpacing(4)

        self._chat = ChatSection()
        self._chat.task_submitted.connect(self._on_task_submitted)
        root.addWidget(self._chat)

    # ── Bridge connections ────────────────────────────────────────────────

    def _connect_bridge(self):
        try:
            from ui.bridge import get_bridge
            b = get_bridge()
            b.state_changed.connect(self._on_state)
            b.focus_changed.connect(self._watching.update_focus)
            b.message_spoken.connect(self._on_marrow_spoke)
            b.stats_updated.connect(self._stats.update_stats)
            b.mic_active.connect(self._on_mic_active)
            b.transcript_heard.connect(self._on_transcript)
            b.task_response.connect(self._on_task_response)
        except Exception as e:
            log.warning(f"Dashboard bridge connect: {e}")

    def _on_state(self, state: str):
        self._state = state
        self._orb.set_state(state)
        self._state_lbl.setText(state)
        if state == "thinking" and self._pending_response:
            pass  # already showing "…"

    def _on_marrow_spoke(self, text: str, urgency: int):
        """Marrow proactively said something — show in message section."""
        self._message.update_message(text, urgency)

    def _on_mic_active(self, active: bool):
        self._mic_dot.set_active(active)
        self._mic_lbl.setText("listening" if active else "mic off")

    def _on_transcript(self, text: str):
        """Audio capture heard something — show as heard transcript."""
        self._listen_lbl.setText(f'heard: "{text[:40]}"')
        QTimer.singleShot(6000, lambda: self._listen_lbl.setText(""))

    def _on_task_submitted(self, text: str):
        """User typed something — send to executor via bridge."""
        self._chat.set_thinking()
        self._pending_response = True
        try:
            from ui.bridge import get_bridge
            get_bridge().text_task_submitted.emit(text)
        except Exception as e:
            log.warning(f"Task submit error: {e}")

    def _on_task_response(self, result: str):
        """Executor returned — update the chat with the result."""
        self._pending_response = False
        self._chat.replace_last_marrow(result or "Done.")

    def _tick_meta(self):
        self._message.tick()

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())

        # Shadow
        shadow = QRadialGradient(
            QPointF(r.center().x(), r.bottom()), r.width() * 0.8
        )
        shadow.setColorAt(0.0, QColor(0, 0, 0, 55))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(shadow)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(r.adjusted(6, 6, -6, -6), RADIUS, RADIUS)

        # Glass background
        p.setBrush(BG)
        p.drawRoundedRect(r, RADIUS, RADIUS)

        # Top shimmer
        shimmer = QLinearGradient(0, 0, 0, 80)
        shimmer.setColorAt(0.0, QColor(255, 255, 255, 9))
        shimmer.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(shimmer)
        clip = QPainterPath()
        clip.addRoundedRect(r, RADIUS, RADIUS)
        p.setClipPath(clip)
        p.drawRect(r)
        p.setClipping(False)

        # Border
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(BORDER, 1))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

    # ── Drag ─────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        # Only drag from non-interactive area (header region approx)
        if event.button() == Qt.MouseButton.LeftButton and event.pos().y() < 36:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, _):
        self._drag_pos = None

    # ── Positioning ───────────────────────────────────────────────────────

    def open_near(self, orb_geom: QRect):
        screen = QApplication.primaryScreen().availableGeometry()
        x = orb_geom.left() - DASH_W - 12
        y = orb_geom.bottom() - DASH_H
        x = max(screen.left() + 6, min(x, screen.right() - DASH_W - 6))
        y = max(screen.top()  + 6, min(y, screen.bottom() - DASH_H - 6))
        self.move(x, y)
        self.show()
        self.raise_()
        # Focus the input field so user can type immediately
        self._chat._input.setFocus()

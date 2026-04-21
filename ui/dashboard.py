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
    QPoint,
    QPointF,
    QRectF,
    QRect,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# ─── Palette (white frosted glass) ───────────────────────────────────────────

BG = QColor(255, 255, 255, 218)
BG_SECTION = QColor(245, 247, 255, 180)
BG_INPUT = QColor(240, 242, 255, 220)
BORDER = QColor(255, 255, 255, 200)
BORDER_DARK = QColor(0, 0, 0, 14)
TEXT_PRI = QColor(12, 12, 22)
TEXT_SEC = QColor(70, 70, 88)
TEXT_DIM = QColor(140, 140, 158)
TEXT_USER = QColor(30, 40, 90)        # user chat: dark blue
TEXT_MARROW = QColor(15, 15, 25)      # Marrow's text: near-black
ACCENT = QColor(37, 99, 235)          # blue accent
ACCENT_G = QColor(22, 163, 74)        # green
ACCENT_A = QColor(234, 155, 10)       # amber
MIC_ON = QColor(22, 163, 74)
MIC_OFF = QColor(180, 180, 195)

DASH_W = 360
DASH_H = 620
RADIUS = 14
MAX_HISTORY = 20  # max chat exchanges to keep in memory


def _color_state(state: str) -> QColor:
    return {
        "idle": QColor(90, 90, 100),
        "thinking": ACCENT_A,
        "speaking": ACCENT,
        "acting": ACCENT_G,
        "error": QColor(248, 113, 113),
    }.get(state, QColor(90, 90, 100))


def _lbl(
    text: str, color: QColor = TEXT_SEC, pt: int = 9, bold: bool = False
) -> QLabel:
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
        self._size = size
        self._state = "idle"
        self._glow = 0.5
        self._t = 0.0
        t = QTimer(self)
        t.setInterval(40)
        t.timeout.connect(self._tick)
        t.start()

    def set_state(self, s: str):
        self._state = s
        self._t = 0.0

    def _tick(self):
        speeds = {
            "idle": 0.4,
            "thinking": 2.5,
            "speaking": 3.5,
            "acting": 2.0,
            "error": 6.0,
        }
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
        self._t = 0.0
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

        self._t += 0.1 if self._on else 0.03
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
        self.role = role  # "user" or "marrow"
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(1)

        is_user = role == "user"

        who = _lbl(
            "You" if is_user else "Marrow",
            TEXT_USER if is_user else TEXT_MARROW,
            pt=7,
            bold=True,
        )
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


class NotificationCard(QWidget):
    """Omi-style inline proactive card with open/dismiss affordances."""

    open_requested = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self._urgency = 3
        self._drag_start: Optional[QPoint] = None
        self._origin_pos: Optional[QPoint] = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(10)

        icon = QLabel("🔔")
        icon.setStyleSheet("font-size: 14px; background: transparent;")
        lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(3)

        self._title = _lbl("Proactive Insight", TEXT_PRI, 9, bold=True)
        text_col.addWidget(self._title)

        self._body = _lbl("", TEXT_SEC, 9)
        self._body.setWordWrap(True)
        text_col.addWidget(self._body)

        self._meta = _lbl("", TEXT_DIM, 8)
        text_col.addWidget(self._meta)

        self._cue = _lbl("", QColor(110, 110, 124), 7)
        self._cue.setWordWrap(True)
        text_col.addWidget(self._cue)
        lay.addLayout(text_col, 1)

        self._open_btn = QPushButton("Open")
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.setFixedSize(52, 24)
        self._open_btn.setStyleSheet("""
            QPushButton {
                background: rgba(37,99,235,18);
                color: rgba(37,99,235,230);
                border: 1px solid rgba(37,99,235,60);
                border-radius: 12px;
                font-size: 8pt;
                font-weight: 600;
            }
            QPushButton:hover { background: rgba(37,99,235,32); }
        """)
        self._open_btn.clicked.connect(self.open_requested.emit)
        lay.addWidget(self._open_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._dismiss_btn = QPushButton("×")
        self._dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dismiss_btn.setFixedSize(20, 20)
        self._dismiss_btn.setStyleSheet("""
            QPushButton { background: transparent; color: rgba(120,120,140,180); border: none; }
            QPushButton:hover { color: rgba(12,12,22,220); }
        """)
        self._dismiss_btn.clicked.connect(self._dismiss)
        lay.addWidget(self._dismiss_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._last_ts = 0.0

    def _dismiss(self):
        self.setVisible(False)
        self.dismissed.emit()

    def show_message(self, text: str, urgency: int, cue: str = ""):
        self._urgency = max(1, min(5, urgency))
        self._last_ts = time.time()
        trimmed = text[:170] + "…" if len(text) > 170 else text
        self._body.setText(trimmed)
        self._meta.setText(f"just now · urgency {self._urgency}")
        self._cue.setText(f"why now: {cue}" if cue else "")
        self.setVisible(True)
        self.update()

    def tick(self):
        if not self.isVisible() or not self._last_ts:
            return
        diff = int(time.time() - self._last_ts)
        ago = f"{diff}s ago" if diff < 60 else f"{diff // 60}m ago"
        self._meta.setText(f"{ago} · urgency {self._urgency}")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())

        urgency_color = {
            1: QColor(248, 113, 113),
            2: QColor(251, 146, 60),
            3: QColor(251, 191, 36),
            4: QColor(96, 165, 250),
            5: QColor(74, 222, 128),
        }.get(self._urgency, QColor(96, 165, 250))

        p.setBrush(QColor(248, 249, 255, 210))
        p.setPen(QPen(QColor(255, 255, 255, 190), 1.0))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

        p.setBrush(urgency_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(8, 10, 3, max(14, self.height() - 20)), 2, 2)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint()
            self._origin_pos = self.pos()

    def mouseMoveEvent(self, event):
        if self._drag_start is None or self._origin_pos is None:
            return
        delta = event.globalPosition().toPoint() - self._drag_start
        self.move(self._origin_pos + QPoint(delta.x(), 0))

    def mouseReleaseEvent(self, event):
        if self._drag_start is None or self._origin_pos is None:
            return
        dx = self.pos().x() - self._origin_pos.x()
        if abs(dx) > 90:
            self._dismiss()
        else:
            self.move(self._origin_pos)
        self._drag_start = None
        self._origin_pos = None


# ─── Chat section ─────────────────────────────────────────────────────────────


class ChatSection(QWidget):
    """Conversation history + text input."""

    task_submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._history: list[tuple[str, str]] = []  # (role, text)

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
            QScrollArea { background: rgba(240,242,255,120); border: 1px solid rgba(0,0,0,10);
                          border-radius: 8px; }
            QScrollBar:vertical { background: transparent; width: 4px; }
            QScrollBar::handle:vertical { background: rgba(37,99,235,50); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        self._chat_widget = QWidget()
        self._chat_widget.setStyleSheet("background: transparent;")
        self._chat_lay = QVBoxLayout(self._chat_widget)
        self._chat_lay.setContentsMargins(10, 8, 10, 8)
        self._chat_lay.setSpacing(6)
        self._chat_lay.addStretch()

        self._empty_lbl = _lbl('Type a task below — or say "Hey Marrow"', TEXT_DIM, 8)
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
                background: rgba(248, 250, 255, 220);
                color: rgba(12, 12, 22, 240);
                border: 1px solid rgba(0,0,0,14);
                border-radius: 8px;
                padding: 0 10px;
                font-size: 9pt;
            }
            QLineEdit:focus {
                border: 1px solid rgba(37,99,235,120);
                background: rgba(255, 255, 255, 255);
            }
        """)
        self._input.returnPressed.connect(self._submit)
        input_row.addWidget(self._input)

        send_btn = QPushButton("↑")
        send_btn.setFixedSize(34, 34)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet("""
            QPushButton {
                background: rgba(37,99,235,200);
                color: white;
                border: none;
                border-radius: 8px; font-size: 14px; font-weight: bold;
            }
            QPushButton:hover { background: rgba(37,99,235,240); }
            QPushButton:pressed { background: rgba(29,78,216,240); }
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
        self._app = _lbl("—", TEXT_PRI, 10, bold=True)
        self._title = _lbl("", TEXT_SEC, 9)
        self._title.setWordWrap(True)
        self._summary = _lbl("", TEXT_DIM, 8)
        self._summary.setWordWrap(True)
        lay.addWidget(self._app)
        lay.addWidget(self._title)
        lay.addWidget(self._summary)

    def update_focus(self, app: str, title: str):
        self._app.setText(app or "—")
        self._title.setText(title[:60] if title else "")

    def cue(self) -> str:
        app = self._app.text().strip()
        title = self._title.text().strip()
        if app and title:
            return f"{app} · {title[:44]}"
        return app or title

    def update_snapshot(self, payload_json: str):
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        summary = (payload.get("summary") or "").strip()
        focused = (payload.get("focused_context") or "").strip()
        source = (payload.get("source") or "").strip()
        parts = []
        if focused:
            parts.append(focused[:90])
        if summary:
            compact = " ".join(summary.split())
            parts.append(compact[:160])
        if source:
            parts.append(f"source: {source}")
        self._summary.setText(" · ".join(parts[:3]))


class MessageSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(_section_label("Last Proactive Message"))
        self._msg = _lbl("Nothing yet", TEXT_SEC, 9)
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
            speaks = d.get("speaks", 0)
            self._line.setText(
                f"{screens} screens captured · {speaks} messages · {actions} actions"
            )
        except Exception:
            pass


class WorkbenchSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(_section_label("Reasoning Workbench"))
        self._title = _lbl("No active deep-reasoning problem", TEXT_PRI, 9, bold=True)
        self._title.setWordWrap(True)
        self._summary = _lbl("", TEXT_SEC, 8)
        self._summary.setWordWrap(True)
        self._state = _lbl("", TEXT_DIM, 8)
        self._state.setWordWrap(True)
        lay.addWidget(self._title)
        lay.addWidget(self._summary)
        lay.addWidget(self._state)

    def update_payload(self, payload_json: str):
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        title = (payload.get("problem_title") or "").strip()
        summary = (payload.get("problem_summary") or payload.get("project_brief") or "").strip()
        stage = (payload.get("stage") or "").strip()
        status = (payload.get("status") or "").strip()
        assumptions = payload.get("assumptions") or []
        blockers = payload.get("blockers") or []
        next_steps = payload.get("next_steps") or []
        verification = payload.get("verification_status") or {}

        self._title.setText(title[:140] if title else "Deep reasoning active")
        parts = []
        if summary:
            parts.append(summary[:180])
        if assumptions:
            parts.append("assume: " + "; ".join(str(x)[:50] for x in assumptions[:2]))
        if blockers:
            parts.append("blockers: " + "; ".join(str(x)[:50] for x in blockers[:2]))
        if next_steps:
            parts.append("next: " + "; ".join(str(x)[:50] for x in next_steps[:2]))
        self._summary.setText(" · ".join(parts[:3]))

        verify_line = verification.get("status", "")
        issues = verification.get("issues") or []
        line = f"{stage} · {status}" if stage or status else ""
        if verify_line:
            line = f"{line} · verify {verify_line}" if line else f"verify {verify_line}"
        if issues:
            line = f"{line} · issue: {str(issues[0])[:70]}" if line else str(issues[0])[:70]
        self._state.setText(line)


# ─── Main dashboard ───────────────────────────────────────────────────────────


class MarrowDashboard(QWidget):
    """The full information panel — opened/closed by the orb."""

    settings_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(DASH_W, DASH_H)

        self._state = "idle"
        self._drag_pos: Optional[QPoint] = None
        self._pending_response = False
        self._last_proactive_text = ""

        self._build()
        self._connect_bridge()

        self._clock = QTimer(self)
        self._clock.setInterval(10_000)
        self._clock.timeout.connect(self._tick_meta)
        self._clock.start()

        # Fast loader animation for acting/thinking states
        self._busy_phase = 0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(350)
        self._busy_timer.timeout.connect(self._tick_busy)

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
        self._mic_dot = MicDot()
        self._mic_lbl = _lbl("mic off", TEXT_DIM, 7)
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

        self._notif = NotificationCard()
        self._notif.open_requested.connect(self._open_notification_context)
        self._notif.dismissed.connect(lambda: None)
        root.addWidget(self._notif)
        root.addSpacing(6)

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

        self._workbench = WorkbenchSection()
        info_lay.addWidget(self._workbench)
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
            b.perception_update.connect(self._watching.update_snapshot)
            b.message_spoken.connect(self._on_marrow_spoke)
            b.stats_updated.connect(self._stats.update_stats)
            b.deep_reasoning_update.connect(self._workbench.update_payload)
            b.mic_active.connect(self._on_mic_active)
            b.transcript_heard.connect(self._on_transcript)
            b.task_response.connect(self._on_task_response)
            b.mission_update.connect(self._on_mission_update)
            b.audio_debug.connect(self._on_audio_debug)
        except Exception as e:
            log.warning(f"Dashboard bridge connect: {e}")

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

        if state == "acting":
            self._listen_lbl.setText("executing action…")
            self._chat._input.setEnabled(False)
        elif state == "thinking":
            self._listen_lbl.setText("thinking…")
            self._chat._input.setEnabled(False)
        else:
            self._chat._input.setEnabled(True)
            if self._listen_lbl.text() in ("executing action…", "thinking…"):
                self._listen_lbl.setText("")
        if state == "thinking" and self._pending_response:
            pass  # already showing "…"

    def _tick_busy(self):
        if self._state not in ("thinking", "acting"):
            return
        self._busy_phase = (self._busy_phase + 1) % 4
        dots = "." * self._busy_phase
        if self._state == "acting":
            self._state_lbl.setText(f"acting{dots}")
            self._listen_lbl.setText(f"executing action{dots}")
        else:
            self._state_lbl.setText(f"thinking{dots}")

    def _on_marrow_spoke(self, text: str, urgency: int):
        """Marrow proactively said something — show in message section."""
        self._message.update_message(text, urgency)
        self._notif.show_message(text, urgency, cue=self._watching.cue())
        self._last_proactive_text = text

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

    def _on_mission_update(self, payload_json: str):
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        step = payload.get("step") or {}
        label = step.get("title") or payload.get("goal", "")
        self._listen_lbl.setText(f"mission {payload.get('state', 'idle')}: {label[:42]}")

    def _on_audio_debug(self, message: str):
        self._listen_lbl.setText(message[:64])

    def _tick_meta(self):
        self._message.tick()
        self._notif.tick()

    def _open_notification_context(self):
        """Bring proactive card context into chat continuity."""
        text = getattr(self, "_last_proactive_text", "")
        if not text:
            return
        self._chat.add_message("marrow", text)
        self._chat._input.setFocus()
        self._chat._input.setPlaceholderText("Ask a follow-up about this insight…")

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())

        # Drop shadow (soft, layered)
        for off, alpha in ((6, 6), (4, 10), (2, 16)):
            sp = QPainterPath()
            sp.addRoundedRect(r.adjusted(-1, off, 1, off + 2), RADIUS, RADIUS)
            p.fillPath(sp, QColor(0, 0, 0, alpha))

        # Glass background
        path = QPainterPath()
        path.addRoundedRect(r, RADIUS, RADIUS)
        p.fillPath(path, BG)

        # Top shimmer
        shimmer = QLinearGradient(0, 0, 0, 50)
        shimmer.setColorAt(0.0, QColor(255, 255, 255, 100))
        shimmer.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, shimmer)

        # Outer bright border
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(BORDER, 1.2))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

        # Inner dark ring
        p.setPen(QPen(BORDER_DARK, 0.7))
        p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), RADIUS - 1, RADIUS - 1)

    # ── Drag ─────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        # Only drag from non-interactive area (header region approx)
        if event.button() == Qt.MouseButton.LeftButton and event.pos().y() < 36:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

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
        y = max(screen.top() + 6, min(y, screen.bottom() - DASH_H - 6))
        self.move(x, y)
        self.show()
        self.raise_()
        # Focus the input field so user can type immediately
        self._chat._input.setFocus()

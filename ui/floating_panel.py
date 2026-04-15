"""
Marrow floating panel — the always-on-top desktop presence.

Design:
  - Frameless, always-on-top, no taskbar entry (Qt.Tool)
  - Docked to the right edge of the screen by default
  - Two states: collapsed (slim pill) and expanded (full panel)
  - Draggable by clicking anywhere on the header
  - Semi-transparent dark glass background
  - Status orb that pulses based on Marrow's state:
      idle     → dim white, slow breathe
      thinking → amber pulse
      speaking → blue pulse
      acting   → green pulse
      error    → red flash

Layout (expanded, 280×480px):
  ┌─────────────────────────────┐
  │  ◉  MARROW           [─][×] │  ← header / drag handle
  ├─────────────────────────────┤
  │  WATCHING                   │
  │  cursor · main.py           │
  ├─────────────────────────────┤
  │  LAST MESSAGE               │
  │  "You have a meeting at 3"  │
  │  2m ago · urgency 2         │
  ├─────────────────────────────┤
  │  WORLD MODEL                │
  │  John · meeting · project   │
  ├─────────────────────────────┤
  │  ACTIVITY                   │
  │  42 screens · 3 messages    │
  │  1 action completed         │
  ├─────────────────────────────┤
  │  [▶ Ask Marrow] [⚙]         │
  └─────────────────────────────┘
"""

import json
import logging
import time
from typing import Optional

from PyQt6.QtCore import (
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt,
    QTimer, pyqtProperty, pyqtSignal, QPointF,
)
from PyQt6.QtGui import (
    QColor, QCursor, QFont, QFontDatabase, QLinearGradient,
    QPainter, QPainterPath, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QSpacerItem, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# ─── Palette ──────────────────────────────────────────────────────────────────

BG_COLOR        = QColor(12, 12, 14, 230)
BG_LIGHTER      = QColor(20, 20, 24, 200)
BORDER_COLOR    = QColor(255, 255, 255, 18)
TEXT_PRIMARY    = QColor(230, 230, 235)
TEXT_SECONDARY  = QColor(140, 140, 150)
TEXT_DIM        = QColor(80, 80, 90)
ACCENT_IDLE     = QColor(100, 100, 110)
ACCENT_THINK    = QColor(251, 191, 36)     # amber
ACCENT_SPEAK    = QColor(96, 165, 250)     # blue
ACCENT_ACT      = QColor(74, 222, 128)     # green
ACCENT_ERROR    = QColor(248, 113, 113)    # red
PANEL_W         = 280
PANEL_RADIUS    = 14


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _color_for_state(state: str) -> QColor:
    return {
        "idle":     ACCENT_IDLE,
        "thinking": ACCENT_THINK,
        "speaking": ACCENT_SPEAK,
        "acting":   ACCENT_ACT,
        "error":    ACCENT_ERROR,
    }.get(state, ACCENT_IDLE)


def _label(text: str, color: QColor = TEXT_SECONDARY, size: int = 10,
           bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    font = QFont()
    font.setPointSize(size)
    font.setBold(bold)
    lbl.setFont(font)
    lbl.setStyleSheet(f"color: rgba({color.red()},{color.green()},{color.blue()},{color.alpha()});")
    return lbl


def _sep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: rgba(255,255,255,12);")
    line.setFixedHeight(1)
    return line


# ─── Status Orb ───────────────────────────────────────────────────────────────

class StatusOrb(QWidget):
    """Animated circular orb that reflects Marrow's state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._state = "idle"
        self._glow = 0.0

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(40)          # 25 fps
        self._t = 0.0

    def set_state(self, state: str) -> None:
        self._state = state
        self._t = 0.0

    def _tick(self) -> None:
        speeds = {
            "idle": 0.5, "thinking": 2.5, "speaking": 3.0,
            "acting": 2.0, "error": 5.0,
        }
        self._t += speeds.get(self._state, 1.0) * 0.04
        import math
        self._glow = (math.sin(self._t) + 1.0) / 2.0
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        color = _color_for_state(self._state)
        alpha_base = 160 + int(80 * self._glow)
        glow_r = 5 + int(3 * self._glow)

        # Outer glow
        grad = QRadialGradient(7, 7, glow_r + 4)
        glow_color = QColor(color)
        glow_color.setAlpha(int(60 * self._glow))
        grad.setColorAt(0, glow_color)
        grad.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, 14, 14)

        # Core dot
        dot_color = QColor(color)
        dot_color.setAlpha(alpha_base)
        p.setBrush(dot_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(3, 3, 8, 8)


# ─── Section widgets ──────────────────────────────────────────────────────────

class FocusSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(3)

        self._title = _label("WATCHING", TEXT_DIM, 8, bold=True)
        lay.addWidget(self._title)

        self._app = _label("—", TEXT_PRIMARY, 11, bold=True)
        lay.addWidget(self._app)

        self._window = _label("", TEXT_SECONDARY, 9)
        self._window.setWordWrap(True)
        lay.addWidget(self._window)

    def update_focus(self, app: str, title: str) -> None:
        self._app.setText(app or "—")
        short_title = title[:50] + "…" if len(title) > 50 else title
        self._window.setText(short_title)


class MessageSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(3)

        self._title = _label("LAST MESSAGE", TEXT_DIM, 8, bold=True)
        lay.addWidget(self._title)

        self._msg = _label("Nothing yet.", TEXT_PRIMARY, 10)
        self._msg.setWordWrap(True)
        lay.addWidget(self._msg)

        self._meta = _label("", TEXT_DIM, 8)
        lay.addWidget(self._meta)

        self._last_time: float = 0
        self._last_urgency: int = 3

    def update_message(self, text: str, urgency: int) -> None:
        self._last_time = time.time()
        self._last_urgency = urgency

        short = text[:140] + "…" if len(text) > 140 else text
        self._msg.setText(f'"{short}"')

        u_color = {1: "#f87171", 2: "#fb923c", 3: "#60a5fa",
                   4: "#4ade80", 5: "#9ca3af"}.get(urgency, "#9ca3af")
        self._meta.setText(f'<span style="color:{u_color}">urgency {urgency}</span>')
        self._meta.setTextFormat(Qt.TextFormat.RichText)

    def refresh_time(self) -> None:
        if self._last_time:
            delta = int(time.time() - self._last_time)
            if delta < 60:
                t = f"{delta}s ago"
            elif delta < 3600:
                t = f"{delta // 60}m ago"
            else:
                t = f"{delta // 3600}h ago"
            u_color = {1: "#f87171", 2: "#fb923c", 3: "#60a5fa",
                       4: "#4ade80", 5: "#9ca3af"}.get(self._last_urgency, "#9ca3af")
            self._meta.setText(
                f'{t} · <span style="color:{u_color}">urgency {self._last_urgency}</span>'
            )
            self._meta.setTextFormat(Qt.TextFormat.RichText)


class WorldSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(3)

        self._title = _label("WORLD MODEL", TEXT_DIM, 8, bold=True)
        lay.addWidget(self._title)

        self._entities = _label("—", TEXT_SECONDARY, 9)
        self._entities.setWordWrap(True)
        lay.addWidget(self._entities)

    def update_world(self, data: list) -> None:
        if not data:
            self._entities.setText("—")
            return
        names = [item.get("content", "")[:30] for item in data[:8]]
        self._entities.setText("  ·  ".join(names))


class ActivitySection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)

        self._title = _label("ACTIVITY", TEXT_DIM, 8, bold=True)
        lay.addWidget(self._title)

        self._screens = _label("screens: 0", TEXT_SECONDARY, 9)
        self._speaks = _label("messages: 0", TEXT_SECONDARY, 9)
        self._actions = _label("actions: 0", TEXT_SECONDARY, 9)
        for w in (self._screens, self._speaks, self._actions):
            lay.addWidget(w)

    def update_stats(self, stats: dict) -> None:
        self._screens.setText(f"screens: {stats.get('screenshots', 0)}")
        self._speaks.setText(f"messages: {stats.get('speaks', 0)}")
        self._actions.setText(f"actions: {stats.get('actions', 0)}")


# ─── Main floating panel ──────────────────────────────────────────────────────

class MarrowFloatingPanel(QWidget):
    """
    Always-on-top floating panel.

    Draggable, collapsible, semi-transparent. Docked to the right edge
    of the primary screen on first launch.
    """

    def __init__(self):
        super().__init__()
        self._collapsed = False
        self._drag_pos: Optional[QPoint] = None
        self._state = "idle"

        self._build_window_flags()
        self._build_ui()
        self._position_default()
        self._connect_bridge()

        # Periodic 1-minute refresh for "X ago" timestamps
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(30_000)

    # ── Window setup ──────────────────────────────────────────────────────

    def _build_window_flags(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool                  # no taskbar entry
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Marrow")

    def _position_default(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.right() - PANEL_W - 16
        y = screen.top() + (screen.height() - 480) // 2
        self.move(x, max(y, screen.top() + 8))

    # ── Build UI ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setFixedWidth(PANEL_W)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Container that we paint on
        self._container = QWidget(self)
        self._container.setObjectName("container")
        outer.addWidget(self._container)

        root = QVBoxLayout(self._container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        root.addWidget(self._build_header())

        # Collapsible body
        self._body = QWidget()
        body_lay = QVBoxLayout(self._body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        self._focus_sec = FocusSection()
        self._msg_sec = MessageSection()
        self._world_sec = WorldSection()
        self._activity_sec = ActivitySection()

        for w in (self._focus_sec, _sep(), self._msg_sec, _sep(),
                  self._world_sec, _sep(), self._activity_sec):
            body_lay.addWidget(w)

        body_lay.addWidget(_sep())
        body_lay.addWidget(self._build_footer())

        root.addWidget(self._body)
        self._update_size()

    def _build_header(self) -> QWidget:
        hdr = QWidget()
        hdr.setFixedHeight(40)
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(12, 0, 10, 0)
        lay.setSpacing(6)

        self._orb = StatusOrb()
        lay.addWidget(self._orb)

        title = _label("MARROW", TEXT_PRIMARY, 11, bold=True)
        title.setStyleSheet(
            "color: rgba(230,230,235,255); letter-spacing: 2px;"
        )
        lay.addWidget(title)
        lay.addStretch()

        # Collapse / expand toggle
        self._collapse_btn = self._icon_btn("–", tooltip="Collapse")
        self._collapse_btn.clicked.connect(self._toggle_collapse)
        lay.addWidget(self._collapse_btn)

        # Settings
        self._settings_btn = self._icon_btn("⚙", tooltip="Settings")
        self._settings_btn.clicked.connect(self._open_settings)
        lay.addWidget(self._settings_btn)

        return hdr

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setFixedHeight(52)
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        self._ask_btn = QPushButton("▶  Ask Marrow")
        self._ask_btn.setFixedHeight(34)
        self._ask_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._ask_btn.setStyleSheet("""
            QPushButton {
                background: rgba(96,165,250,180);
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 11px;
                font-weight: bold;
                padding: 0 14px;
            }
            QPushButton:hover  { background: rgba(96,165,250,220); }
            QPushButton:pressed { background: rgba(59,130,246,220); }
        """)
        self._ask_btn.clicked.connect(self._on_ask)
        lay.addWidget(self._ask_btn)

        lay.addStretch()
        return footer

    @staticmethod
    def _icon_btn(icon: str, tooltip: str = "") -> QPushButton:
        btn = QPushButton(icon)
        btn.setFixedSize(24, 24)
        btn.setToolTip(tooltip)
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: rgba(140,140,150,200);
                border: none;
                font-size: 13px;
            }
            QPushButton:hover { color: rgba(230,230,235,255); }
        """)
        return btn

    # ── Bridge connection ──────────────────────────────────────────────────

    def _connect_bridge(self) -> None:
        try:
            from ui.bridge import get_bridge
            bridge = get_bridge()
            bridge.state_changed.connect(self._on_state_changed)
            bridge.message_spoken.connect(self._on_message_spoken)
            bridge.focus_changed.connect(self._on_focus_changed)
            bridge.world_model_updated.connect(self._on_world_model)
            bridge.stats_updated.connect(self._on_stats_updated)
            bridge.ask_requested.connect(self._on_ask)
        except Exception as e:
            log.warning(f"Bridge connect failed: {e}")

    # ── Bridge slots ──────────────────────────────────────────────────────

    def _on_state_changed(self, state: str) -> None:
        self._state = state
        self._orb.set_state(state)

    def _on_message_spoken(self, text: str, urgency: int) -> None:
        self._msg_sec.update_message(text, urgency)
        # Briefly flash the panel if collapsed
        if self._collapsed:
            self._flash()

    def _on_focus_changed(self, app: str, title: str) -> None:
        self._focus_sec.update_focus(app, title)

    def _on_world_model(self, json_str: str) -> None:
        try:
            data = json.loads(json_str)
            self._world_sec.update_world(data)
        except Exception:
            pass

    def _on_stats_updated(self, json_str: str) -> None:
        try:
            stats = json.loads(json_str)
            self._activity_sec.update_stats(stats)
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────────────────

    def _on_ask(self) -> None:
        try:
            from ui.bridge import get_bridge
            get_bridge().trigger_activation("ui_ask_button")
        except Exception as e:
            log.warning(f"Ask trigger failed: {e}")

    def _open_settings(self) -> None:
        try:
            from ui.settings_panel import MarrowSettingsPanel
            dlg = MarrowSettingsPanel(self)
            dlg.show()
        except Exception as e:
            log.warning(f"Settings open failed: {e}")

    # ── Collapse / expand ─────────────────────────────────────────────────

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self._body.setVisible(not self._collapsed)
        self._collapse_btn.setText("□" if self._collapsed else "–")
        self._update_size()

    def _update_size(self) -> None:
        if self._collapsed:
            self.setFixedHeight(40)
        else:
            self._container.adjustSize()
            self.setFixedHeight(self._container.sizeHint().height())

    def _flash(self) -> None:
        """Briefly highlight the orb when a message arrives while collapsed."""
        self._orb.set_state("speaking")
        QTimer.singleShot(2000, lambda: self._orb.set_state(self._state))

    def _tick(self) -> None:
        self._msg_sec.refresh_time()

    # ── Painting — glass background ────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)

        # Fill
        p.fillPath(path, BG_COLOR)

        # Border
        p.setPen(QPen(BORDER_COLOR, 1.0))
        p.drawPath(path)

        # Top highlight (glass shimmer)
        shimmer = QLinearGradient(0, 0, 0, 20)
        shimmer.setColorAt(0, QColor(255, 255, 255, 18))
        shimmer.setColorAt(1, QColor(255, 255, 255, 0))
        top_path = QPainterPath()
        top_rect = QRectF(0.5, 0.5, self.width() - 1, 20)
        top_path.addRoundedRect(top_rect, PANEL_RADIUS, PANEL_RADIUS)
        p.fillPath(top_path, shimmer)

    # ── Drag to move ──────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            # Clamp to screen bounds
            screen = QApplication.primaryScreen().availableGeometry()
            new_pos.setX(max(screen.left(), min(new_pos.x(), screen.right() - self.width())))
            new_pos.setY(max(screen.top(), min(new_pos.y(), screen.bottom() - self.height())))
            self.move(new_pos)

    def mouseReleaseEvent(self, _event):
        self._drag_pos = None

    # ── Context menu ─────────────────────────────────────────────────────

    def contextMenuEvent(self, event):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: rgba(20,20,24,240);
                color: rgba(230,230,235,255);
                border: 1px solid rgba(255,255,255,20);
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item { padding: 6px 20px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(96,165,250,120); }
        """)
        menu.addAction("⚙  Settings", self._open_settings)
        menu.addSeparator()
        menu.addAction("✕  Hide panel", self.hide)
        menu.addAction("✕  Quit Marrow", QApplication.instance().quit)
        menu.exec(event.globalPos())

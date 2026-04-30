"""
Marrow status orb — the always-there desktop presence.

A small (64×64) animated circle that lives at the corner of your screen.
It's the permanent visual anchor: always visible, never in the way.

States → animation:
  idle     → slow dim breathe (gray)
  thinking → amber pulse, faster
  speaking → blue ripple
  acting   → green spark
  error    → red flash

Interactions:
  Left click  → open/close dashboard
  Drag        → reposition anywhere on screen
  Right click → context menu (Dashboard, Settings, Quit)
"""

import logging
import math
from typing import Optional

from PySide6.QtCore import (
    QPoint, QPointF, QRectF, QSize, Qt, QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QFont, QPainter, QPainterPath,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QMenu, QWidget,
)

log = logging.getLogger(__name__)

# Orb geometry
ORB_TOTAL    = 64     # widget size (includes glow padding)
ORB_CORE     = 38    # solid circle diameter
ORB_CENTER   = ORB_TOTAL // 2
MARGIN       = 20    # from screen edge when initially positioned

# State colors
COLORS = {
    "idle":     QColor(200, 200, 215),   # soft white
    "thinking": QColor(251, 191, 36),    # amber
    "speaking": QColor(220, 220, 235),   # bright white pulse
    "acting":   QColor(74,  222, 128),   # green
    "error":    QColor(248, 113, 113),   # red
}
BG_INNER = QColor(14, 14, 18, 235)


class MarrowOrb(QWidget):
    """
    The tiny always-on-top orb.
    Emits dashboard_toggle when left-clicked (not after drag).
    """

    dashboard_toggle = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(ORB_TOTAL, ORB_TOTAL)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._state    = "idle"
        self._t        = 0.0        # animation time
        self._glow     = 0.5        # 0–1
        self._drag_pos: Optional[QPoint] = None
        self._dragged  = False
        self._trace_lines: list[str] = []
        self._focus_hint = ""

        self._anim = QTimer(self)
        self._anim.setInterval(33)  # ~30fps
        self._anim.timeout.connect(self._tick)
        self._anim.start()

        # Position: bottom-right corner
        self._snap_to_corner()

    # ── Animation ─────────────────────────────────────────────────────────

    def _tick(self):
        speeds = {
            "idle": 0.5, "thinking": 2.5,
            "speaking": 3.5, "acting": 2.0, "error": 6.0,
        }
        self._t += speeds.get(self._state, 1.0) * 0.033
        self._glow = (math.sin(self._t) + 1.0) / 2.0
        self.update()

    def set_state(self, state: str):
        if state != self._state:
            self._state = state
            self._t = 0.0
            self._refresh_tooltip()

    def set_audio_trace(self, message: str):
        msg = (message or "").strip()
        if not msg:
            return
        if self._trace_lines and self._trace_lines[-1] == msg:
            return
        self._trace_lines.append(msg[:120])
        self._trace_lines = self._trace_lines[-4:]
        self._refresh_tooltip()

    def set_focus_hint(self, app_name: str, window_title: str):
        app_name = (app_name or "").strip()
        window_title = (window_title or "").strip()
        self._focus_hint = " - ".join([p for p in (app_name, window_title[:60]) if p])
        self._refresh_tooltip()

    def _refresh_tooltip(self):
        lines = [f"Marrow: {self._state}"]
        if self._focus_hint:
            lines.append(f"Focus: {self._focus_hint}")
        if self._trace_lines:
            lines.append("Trace:")
            lines.extend(self._trace_lines[-3:])
        self.setToolTip("\n".join(lines))

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        color  = COLORS.get(self._state, COLORS["idle"])
        cx, cy = ORB_CENTER, ORB_CENTER
        cr     = ORB_CORE / 2

        # Outer glow ring (large, soft)
        glow_r = cr + 8 + int(6 * self._glow)
        glow_alpha = int(40 * self._glow) if self._state != "idle" else int(15 * self._glow)
        outer = QRadialGradient(QPointF(cx, cy), glow_r)
        gc = QColor(color)
        gc.setAlpha(glow_alpha)
        outer.setColorAt(0.0, gc)
        outer.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(outer)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(
            int(cx - glow_r), int(cy - glow_r),
            int(glow_r * 2), int(glow_r * 2),
        )

        # Dark inner shell
        p.setBrush(BG_INNER)
        p.drawEllipse(
            int(cx - cr - 2), int(cy - cr - 2),
            int(cr + 2) * 2, int(cr + 2) * 2,
        )

        # Core — radial gradient from color center to dark edge
        core_grad = QRadialGradient(QPointF(cx - 4, cy - 4), cr)
        bright = QColor(color)
        bright.setAlpha(220 + int(35 * self._glow))
        dim = QColor(color)
        dim.setAlpha(90)
        core_grad.setColorAt(0.0, bright)
        core_grad.setColorAt(1.0, dim)
        p.setBrush(core_grad)
        p.drawEllipse(
            int(cx - cr), int(cy - cr),
            int(cr * 2), int(cr * 2),
        )

        # Specular highlight
        spec = QRadialGradient(QPointF(cx - 6, cy - 7), cr * 0.45)
        spec.setColorAt(0.0, QColor(255, 255, 255, 55))
        spec.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(spec)
        p.drawEllipse(
            int(cx - cr), int(cy - cr),
            int(cr * 2), int(cr * 2),
        )

    # ── Interactions ──────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            self.move(new_pos)
            self._dragged = True

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._dragged:
                self.dashboard_toggle.emit()
            self._drag_pos = None
            self._dragged = False

    def _show_menu(self, pos: QPoint):
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background: rgba(14, 14, 18, 250);
                color: rgba(225, 225, 235, 255);
                border: 1px solid rgba(255,255,255,18);
                border-radius: 8px;
                padding: 4px;
                font-size: 9pt;
            }
            QMenu::item { padding: 6px 18px 6px 12px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(96,165,250,80); }
            QMenu::separator { height: 1px; background: rgba(255,255,255,12); margin: 3px 6px; }
        """)
        menu.addAction("Open Dashboard", lambda: self.dashboard_toggle.emit())
        menu.addAction("Settings",       lambda: self.settings_requested.emit())
        menu.addSeparator()
        menu.addAction("Quit Marrow",    lambda: self.quit_requested.emit())
        menu.exec(pos)

    # ── Positioning ───────────────────────────────────────────────────────

    def _snap_to_corner(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            screen.right()  - ORB_TOTAL - MARGIN,
            screen.bottom() - ORB_TOTAL - MARGIN,
        )

    # ── Bridge wiring ─────────────────────────────────────────────────────

    def connect_bridge(self):
        try:
            from ui.bridge import get_bridge
            bridge = get_bridge()
            bridge.state_changed.connect(self.set_state)
            bridge.audio_debug.connect(self.set_audio_trace)
            bridge.transcript_heard.connect(
                lambda text: self.set_audio_trace(f"heard: {text[:80]}")
            )
            bridge.focus_changed.connect(self.set_focus_hint)
        except Exception as e:
            log.warning(f"Orb bridge connect failed: {e}")

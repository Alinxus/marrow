"""
Toast notification system — white frosted-glass slide-in cards.

Toasts appear at bottom-right, stack upward, auto-dismiss with progress bar.

Usage:
    from ui.toast import get_toast_manager
    get_toast_manager().show("Marrow", "You have 3 unread emails", urgency=2)
"""

import logging
from typing import Optional

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QPoint, QPropertyAnimation,
    QRectF, QTimer, Qt,
)
from PySide6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

TOAST_W      = 340
TOAST_MARGIN = 20
TOAST_GAP    = 8
MAX_TOASTS   = 4

# Urgency → left-border accent color
URGENCY_COLORS = {
    1: QColor(239, 68,  68),    # red   — critical
    2: QColor(249, 115, 22),    # orange
    3: QColor(234, 179,  8),    # yellow
    4: QColor(59,  130, 246),   # blue
    5: QColor(148, 163, 184),   # slate — low
}
DISMISS_MS = {1: 12000, 2: 10000, 3: 8000, 4: 7000, 5: 6000}

# White glass palette
BG_GLASS     = QColor(255, 255, 255, 218)
BG_INNER     = QColor(248, 249, 255, 200)
BORDER_LIGHT = QColor(255, 255, 255, 210)
BORDER_DARK  = QColor(0,   0,   0,   18)
TEXT_PRI     = QColor(12,  12,  22)
TEXT_SEC     = QColor(80,  80,  98)


class ToastCard(QWidget):
    """One frosted-glass notification card."""

    from PySide6.QtCore import Signal
    closed = Signal(object)

    def __init__(self, title: str, body: str, urgency: int = 3,
                 action_label: str = "", action_callback=None, parent=None):
        super().__init__(parent, Qt.WindowType.Tool |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.X11BypassWindowManagerHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._urgency    = max(1, min(5, urgency))
        self._accent     = URGENCY_COLORS.get(self._urgency, URGENCY_COLORS[4])
        self._dismiss_ms = DISMISS_MS.get(self._urgency, 8000)
        self._remaining  = self._dismiss_ms
        self._progress   = 1.0

        self._build_layout(title, body, action_label, action_callback)
        self._start_timers()

    def _build_layout(self, title, body, action_label, action_callback):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)  # glow padding

        card = QWidget()
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(16, 12, 12, 12)
        card_lay.setSpacing(4)

        # Header row
        hdr = QHBoxLayout()
        hdr.setSpacing(8)

        t_lbl = QLabel(title)
        tf = QFont()
        tf.setPointSize(9)
        tf.setBold(True)
        t_lbl.setFont(tf)
        ar, ag, ab = self._accent.red(), self._accent.green(), self._accent.blue()
        t_lbl.setStyleSheet(f"color: rgb({ar},{ag},{ab}); background: transparent;")
        hdr.addWidget(t_lbl)
        hdr.addStretch()

        close_btn = QPushButton("×")
        close_btn.setFixedSize(18, 18)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: rgba(100,100,118,180);
                border: none;
                font-size: 14px;
                padding: 0;
            }
            QPushButton:hover { color: rgba(20,20,35,220); }
        """)
        close_btn.clicked.connect(self._dismiss)
        hdr.addWidget(close_btn)
        card_lay.addLayout(hdr)

        # Body text
        body_lbl = QLabel(body)
        bf = QFont()
        bf.setPointSize(9)
        body_lbl.setFont(bf)
        body_lbl.setStyleSheet("color: rgba(12,12,22,210); background: transparent;")
        body_lbl.setWordWrap(True)
        body_lbl.setMaximumWidth(TOAST_W - 52)
        card_lay.addWidget(body_lbl)

        # Action button (optional)
        if action_label and action_callback:
            btn = QPushButton(action_label)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba({ar},{ag},{ab},20);
                    color: rgb({ar},{ag},{ab});
                    border: 1px solid rgba({ar},{ag},{ab},80);
                    border-radius: 5px;
                    font-size: 9px;
                    font-weight: bold;
                    padding: 0 10px;
                }}
                QPushButton:hover {{
                    background: rgba({ar},{ag},{ab},35);
                }}
            """)
            def _do():
                if action_callback:
                    action_callback()
                self._dismiss()
            btn.clicked.connect(_do)
            card_lay.addWidget(btn)

        outer.addWidget(card)
        self.setFixedWidth(TOAST_W)
        self.adjustSize()

    def _start_timers(self):
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(50)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

    def _tick(self):
        self._remaining -= 50
        if self._remaining <= 0:
            self._dismiss()
            return
        self._progress = max(0.0, self._remaining / self._dismiss_ms)
        self.update()

    def _dismiss(self):
        self._tick_timer.stop()
        self.closed.emit(self)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Outer rect (card area minus glow padding)
        r = self.rect().adjusted(7, 7, -7, -7)
        rf = QRectF(r)

        # Drop shadow (multi-layer soft shadow)
        for offset, alpha in ((4, 8), (2, 14), (1, 20)):
            shadow_path = QPainterPath()
            shadow_path.addRoundedRect(
                QRectF(r.adjusted(-1, offset, 1, offset + 1)), 13, 13
            )
            p.fillPath(shadow_path, QColor(0, 0, 0, alpha))

        # Glass fill
        p.setBrush(BG_GLASS)
        p.setPen(Qt.PenStyle.NoPen)
        path = QPainterPath()
        path.addRoundedRect(rf, 12, 12)
        p.fillPath(path, BG_GLASS)

        # Inner subtle gradient (brighter at top)
        grad = QLinearGradient(0, r.top(), 0, r.bottom())
        grad.setColorAt(0.0, QColor(255, 255, 255, 80))
        grad.setColorAt(0.4, QColor(255, 255, 255, 0))
        p.fillPath(path, grad)

        # Border: bright outer edge
        p.setPen(QPen(BORDER_LIGHT, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rf.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

        # Border: thin dark inner shadow ring
        p.setPen(QPen(BORDER_DARK, 0.8))
        p.drawRoundedRect(rf.adjusted(1, 1, -1, -1), 11, 11)

        # Accent bar (left side, 3px)
        accent_rect = QRectF(r.left(), r.top() + 18, 3, r.height() - 36)
        p.setBrush(self._accent)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(accent_rect, 1.5, 1.5)

        # Progress bar (bottom)
        if self._progress > 0:
            bar_h = 3
            bar_w = int((r.width() - 24) * self._progress)
            bar_y = r.bottom() - bar_h - 7
            bar_x = r.left() + 12
            prog_c = QColor(self._accent)
            prog_c.setAlpha(140)
            p.setBrush(prog_c)
            p.drawRoundedRect(QRectF(bar_x, bar_y, max(bar_w, 0), bar_h), 1.5, 1.5)


# ─── Toast manager ────────────────────────────────────────────────────────────

class ToastManager:
    def __init__(self):
        self._toasts: list[ToastCard] = []
        self._pending_pos = QTimer()
        self._pending_pos.setSingleShot(True)
        self._pending_pos.setInterval(16)
        self._pending_pos.timeout.connect(self._reposition_all)

    def show(self, title: str, body: str, urgency: int = 3,
             action_label: str = "", action_callback=None,
             replace: bool = False) -> None:
        if replace or len(self._toasts) >= MAX_TOASTS:
            for card in list(self._toasts):
                card.closed.emit(card)
            self._toasts.clear()

        # Keep toast body concise — long AI observations get truncated
        if len(body) > 180:
            body = body[:177] + "…"

        card = ToastCard(title, body, urgency, action_label, action_callback)
        card.closed.connect(self._on_closed)
        self._toasts.append(card)

        screen = QApplication.primaryScreen().availableGeometry()
        card.move(screen.right() + TOAST_W + 20, screen.bottom())
        card.show()
        self._reposition_all(animate_last=True)

    def _on_closed(self, card: ToastCard):
        if card in self._toasts:
            self._toasts.remove(card)
        card.hide()
        card.deleteLater()
        self._pending_pos.start()

    def _reposition_all(self, animate_last: bool = False):
        screen = QApplication.primaryScreen().availableGeometry()
        y = screen.bottom() - TOAST_MARGIN

        for i, card in enumerate(reversed(self._toasts)):
            card.adjustSize()
            h = card.height()
            ty = y - h
            tx = screen.right() - TOAST_W - TOAST_MARGIN
            is_newest = (i == 0)

            anim = QPropertyAnimation(card, b"pos", card)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            if is_newest and animate_last:
                anim.setDuration(300)
                anim.setStartValue(QPoint(screen.right() + 10, ty))
            else:
                anim.setDuration(200)
                anim.setStartValue(card.pos())
            anim.setEndValue(QPoint(tx, ty))
            anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

            y -= h + TOAST_GAP

    def dismiss_all(self):
        for card in list(self._toasts):
            card.closed.emit(card)


_manager: Optional[ToastManager] = None


def get_toast_manager() -> ToastManager:
    global _manager
    if _manager is None:
        _manager = ToastManager()
    return _manager

"""
Toast notification system — beautiful slide-in message cards.

Used when voice is off, for structured info, or when user prefers visual.
Toasts appear at bottom-right, stack upward, auto-dismiss with progress bar.

Usage:
    from ui.toast import get_toast_manager
    get_toast_manager().show("Marrow", "You have 3 unread emails", urgency=2)
"""

import logging
import math
from typing import Optional

from PyQt6.QtCore import (
    QAbstractAnimation, QEasingCurve, QPoint, QPropertyAnimation,
    QRect, QRectF, QSize, QTimer, Qt, pyqtProperty, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QBrush,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

TOAST_W      = 330
TOAST_MARGIN = 18     # from screen edge
TOAST_GAP    = 8
MAX_TOASTS   = 4

# Urgency 1–5 → left-border color
URGENCY_COLORS = {
    1: QColor(248, 113, 113),   # red   — critical
    2: QColor(251, 146, 60),    # orange
    3: QColor(251, 191, 36),    # amber
    4: QColor(96,  165, 250),   # blue
    5: QColor(100, 100, 110),   # gray  — low
}

# Auto-dismiss: urgency 1 stays 12s, urgency 5 stays 6s
DISMISS_MS = {1: 12000, 2: 10000, 3: 8000, 4: 7000, 5: 6000}

BG_COLOR     = QColor(10, 10, 14, 248)
TEXT_PRI     = QColor(232, 232, 238)
TEXT_SEC     = QColor(148, 148, 158)
BORDER_COL   = QColor(255, 255, 255, 20)


# ─── Single toast card ────────────────────────────────────────────────────────

class ToastCard(QWidget):
    """One notification card. Emits closed() when it should be removed."""

    closed = pyqtSignal(object)   # self

    def __init__(self, title: str, body: str, urgency: int = 3,
                 action_label: str = "", action_callback=None, parent=None):
        super().__init__(parent, Qt.WindowType.Tool |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.X11BypassWindowManagerHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._urgency = max(1, min(5, urgency))
        self._accent  = URGENCY_COLORS.get(self._urgency, QColor(100, 100, 110))
        self._dismiss_ms  = DISMISS_MS.get(self._urgency, 8000)
        self._remaining   = self._dismiss_ms
        self._progress    = 1.0     # 1.0 → 0.0

        self._build_layout(title, body, action_label, action_callback)
        self._start_timers()

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_layout(self, title, body, action_label, action_callback):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)   # space for glow shadow

        card = QWidget()
        card.setObjectName("card")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(16, 12, 12, 12)
        card_lay.setSpacing(4)

        # Header row
        hdr = QHBoxLayout()
        hdr.setSpacing(8)

        t_lbl = QLabel(title)
        f = QFont()
        f.setPointSize(9)
        f.setBold(True)
        t_lbl.setFont(f)
        t_lbl.setStyleSheet(f"color: rgba({self._accent.red()},{self._accent.green()},{self._accent.blue()},255);")
        hdr.addWidget(t_lbl)
        hdr.addStretch()

        close_btn = QPushButton("×")
        close_btn.setFixedSize(18, 18)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: rgba(150,150,160,200);
                          border: none; font-size: 14px; padding: 0; }
            QPushButton:hover { color: rgba(220,220,230,255); }
        """)
        close_btn.clicked.connect(self._dismiss)
        hdr.addWidget(close_btn)
        card_lay.addLayout(hdr)

        # Body
        body_lbl = QLabel(body)
        bf = QFont()
        bf.setPointSize(9)
        body_lbl.setFont(bf)
        body_lbl.setStyleSheet("color: rgba(230,230,236,220);")
        body_lbl.setWordWrap(True)
        body_lbl.setMaximumWidth(TOAST_W - 48)
        card_lay.addWidget(body_lbl)

        # Action button (optional)
        if action_label and action_callback:
            btn = QPushButton(action_label)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba({self._accent.red()},{self._accent.green()},{self._accent.blue()},40);
                    color: rgba({self._accent.red()},{self._accent.green()},{self._accent.blue()},255);
                    border: 1px solid rgba({self._accent.red()},{self._accent.green()},{self._accent.blue()},80);
                    border-radius: 4px; font-size: 9px; padding: 0 10px;
                }}
                QPushButton:hover {{
                    background: rgba({self._accent.red()},{self._accent.green()},{self._accent.blue()},70);
                }}
            """)
            def _do_action():
                if action_callback:
                    action_callback()
                self._dismiss()
            btn.clicked.connect(_do_action)
            card_lay.addWidget(btn)

        outer.addWidget(card)
        self.setFixedWidth(TOAST_W)
        self.adjustSize()

    # ── Timers ────────────────────────────────────────────────────────────

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

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r = self.rect().adjusted(5, 5, -5, -5)
        rf = QRectF(r)

        # Shadow/glow
        shadow_c = QColor(self._accent)
        shadow_c.setAlpha(18)
        p.setBrush(shadow_c)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rf.adjusted(-3, -2, 3, 3), 14, 14)

        # Card background
        p.setBrush(BG_COLOR)
        p.drawRoundedRect(rf, 12, 12)

        # Border
        p.setPen(QPen(BORDER_COL, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rf.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

        # Accent left border (4px)
        accent_rect = QRectF(r.left(), r.top() + 16, 3, r.height() - 32)
        p.setBrush(self._accent)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(accent_rect, 2, 2)

        # Progress bar at bottom
        if self._progress > 0:
            bar_h  = 3
            bar_w  = int((r.width() - 24) * self._progress)
            bar_y  = r.bottom() - bar_h - 6
            bar_x  = r.left() + 12

            prog_col = QColor(self._accent)
            prog_col.setAlpha(160)
            p.setBrush(prog_col)
            p.drawRoundedRect(QRectF(bar_x, bar_y, max(bar_w, 0), bar_h), 2, 2)


# ─── Toast manager ────────────────────────────────────────────────────────────

class ToastManager:
    """
    Manages the stack of toast notifications.
    Creates/positions/removes ToastCard instances.
    Thread-safe via Qt signals.
    """

    def __init__(self):
        self._toasts: list[ToastCard] = []
        self._pending_position = QTimer()
        self._pending_position.setSingleShot(True)
        self._pending_position.setInterval(16)
        self._pending_position.timeout.connect(self._reposition_all)

    def show(self, title: str, body: str, urgency: int = 3,
             action_label: str = "", action_callback=None) -> None:
        """Show a toast. Can be called from any thread via Qt signal."""
        # Evict oldest if full
        if len(self._toasts) >= MAX_TOASTS:
            oldest = self._toasts[0]
            oldest.closed.emit(oldest)

        card = ToastCard(title, body, urgency, action_label, action_callback)
        card.closed.connect(self._on_closed)
        self._toasts.append(card)

        # Position off-screen first, then animate in
        screen = QApplication.primaryScreen().availableGeometry()
        start_x = screen.right() + TOAST_W + 20
        card.move(start_x, screen.bottom())
        card.show()

        self._reposition_all(animate_last=True)

    def _on_closed(self, card: ToastCard):
        if card in self._toasts:
            self._toasts.remove(card)
        card.hide()
        card.deleteLater()
        self._pending_position.start()

    def _reposition_all(self, animate_last: bool = False):
        screen = QApplication.primaryScreen().availableGeometry()
        y = screen.bottom() - TOAST_MARGIN

        for i, card in enumerate(reversed(self._toasts)):
            card.adjustSize()
            h = card.height()
            target_y = y - h
            target_x = screen.right() - TOAST_W - TOAST_MARGIN

            is_newest = (i == 0)
            if is_newest and animate_last:
                # Slide in from right
                anim = QPropertyAnimation(card, b"pos", card)
                anim.setDuration(280)
                anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                anim.setStartValue(QPoint(screen.right() + 10, target_y))
                anim.setEndValue(QPoint(target_x, target_y))
                anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
            else:
                # Animate vertical restack
                anim = QPropertyAnimation(card, b"pos", card)
                anim.setDuration(180)
                anim.setEasingCurve(QEasingCurve.Type.OutQuad)
                anim.setStartValue(card.pos())
                anim.setEndValue(QPoint(target_x, target_y))
                anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

            y -= h + TOAST_GAP

    def dismiss_all(self):
        for card in list(self._toasts):
            card.closed.emit(card)


# ─── Singleton ────────────────────────────────────────────────────────────────

_manager: Optional[ToastManager] = None


def get_toast_manager() -> ToastManager:
    global _manager
    if _manager is None:
        _manager = ToastManager()
    return _manager

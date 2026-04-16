"""
Rich claim verification card.

Slides in from the LEFT side of the screen (toasts are on the right).
Shows a verdict badge, the full claim, a 1-3 sentence explanation,
and clickable source chips — the Omi "pulled sources with beautiful UI" experience.

Usage (called from main.py when bridge.claim_verified fires):
    from ui.claim_card import get_claim_card_manager
    mgr = get_claim_card_manager()
    mgr.show_claim({"claim": "...", "verdict": "false", "explanation": "...",
                    "sources": [...], "confidence": 0.9})
"""

import json
import logging
from typing import Optional
from urllib.parse import urlparse

from PyQt6.QtCore import (
    QAbstractAnimation, QEasingCurve, QPoint, QPropertyAnimation,
    QRectF, QTimer, Qt, QUrl,
)
from PyQt6.QtGui import (
    QColor, QDesktopServices, QFont, QLinearGradient, QPainter,
    QPainterPath, QPen,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

CARD_W       = 390
CARD_MARGIN  = 22
CARD_GAP     = 10
MAX_CARDS    = 3
DISMISS_MS   = 14000   # claim cards stay longer — 14s

# Verdict → (badge_bg, badge_text, label)
VERDICT_STYLE = {
    "false":      (QColor(239, 68,  68),  QColor(255, 255, 255), "FALSE"),
    "true":       (QColor(22,  163,  74), QColor(255, 255, 255), "CONFIRMED"),
    "misleading": (QColor(234, 155,  10), QColor(255, 255, 255), "MISLEADING"),
    "unverified": (QColor(148, 163, 184), QColor(255, 255, 255), "UNVERIFIED"),
}

# White glass palette
BG_GLASS     = QColor(255, 255, 255, 222)
BORDER_LIGHT = QColor(255, 255, 255, 205)
BORDER_DARK  = QColor(0,   0,   0,   16)
TEXT_MAIN    = QColor(12,  12,  22)
TEXT_BODY    = QColor(55,  55,  72)
TEXT_DIM     = QColor(120, 120, 140)


def _domain(url: str) -> str:
    """Extract readable domain from URL for source chips."""
    try:
        h = urlparse(url).netloc
        return h.replace("www.", "")[:30]
    except Exception:
        return url[:30]


class ClaimCard(QWidget):
    """
    One claim verification result card.
    Shows verdict badge + claim + explanation + clickable source chips.
    """

    from PyQt6.QtCore import pyqtSignal
    closed = pyqtSignal(object)

    def __init__(self, data: dict, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.X11BypassWindowManagerHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        verdict  = (data.get("verdict") or "unverified").lower()
        claim    = data.get("claim", "")
        explain  = data.get("explanation", "")
        sources  = data.get("sources", [])

        self._verdict_style = VERDICT_STYLE.get(verdict, VERDICT_STYLE["unverified"])
        self._remaining  = DISMISS_MS
        self._progress   = 1.0

        self._build_layout(verdict, claim, explain, sources)
        self._start_timer()

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_layout(self, verdict, claim, explain, sources):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 10)

        card = QWidget()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 14, 14)
        lay.setSpacing(8)

        # ── Row 1: badge + close ────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        badge_bg, badge_fg, badge_text = self._verdict_style
        badge = QLabel(f"  {badge_text}  ")
        bf = QFont()
        bf.setPointSize(8)
        bf.setBold(True)
        badge.setFont(bf)
        badge.setFixedHeight(22)
        r, g, b = badge_bg.red(), badge_bg.green(), badge_bg.blue()
        fr, fg_, fb = badge_fg.red(), badge_fg.green(), badge_fg.blue()
        badge.setStyleSheet(
            f"background: rgb({r},{g},{b}); color: rgb({fr},{fg_},{fb});"
            f" border-radius: 11px; padding: 0 2px;"
        )
        top_row.addWidget(badge)
        top_row.addStretch()

        close_btn = QPushButton("×")
        close_btn.setFixedSize(18, 18)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: rgba(100,100,120,180);
                          border: none; font-size: 14px; padding: 0; }
            QPushButton:hover { color: rgba(12,12,22,220); }
        """)
        close_btn.clicked.connect(self._dismiss)
        top_row.addWidget(close_btn)
        lay.addLayout(top_row)

        # ── Row 2: claim text ────────────────────────────────────────────
        if claim:
            claim_lbl = QLabel(f'"{claim[:120]}{"…" if len(claim) > 120 else ""}"')
            cf = QFont()
            cf.setPointSize(10)
            cf.setBold(True)
            claim_lbl.setFont(cf)
            claim_lbl.setStyleSheet(f"color: rgb({TEXT_MAIN.red()},{TEXT_MAIN.green()},{TEXT_MAIN.blue()}); background: transparent;")
            claim_lbl.setWordWrap(True)
            claim_lbl.setMaximumWidth(CARD_W - 52)
            lay.addWidget(claim_lbl)

        # ── Row 3: explanation ───────────────────────────────────────────
        if explain:
            exp_lbl = QLabel(explain[:240] + ("…" if len(explain) > 240 else ""))
            ef = QFont()
            ef.setPointSize(9)
            exp_lbl.setFont(ef)
            exp_lbl.setStyleSheet(f"color: rgba({TEXT_BODY.red()},{TEXT_BODY.green()},{TEXT_BODY.blue()},220); background: transparent;")
            exp_lbl.setWordWrap(True)
            exp_lbl.setMaximumWidth(CARD_W - 52)
            lay.addWidget(exp_lbl)

        # ── Row 4: source chips ──────────────────────────────────────────
        if sources:
            src_row = QHBoxLayout()
            src_row.setSpacing(6)
            src_row.setContentsMargins(0, 2, 0, 0)
            for url in sources[:3]:
                domain = _domain(url)
                chip = QPushButton(f"↗  {domain}")
                chip.setCursor(Qt.CursorShape.PointingHandCursor)
                chip.setFixedHeight(22)
                chip.setStyleSheet("""
                    QPushButton {
                        background: rgba(37,99,235,12);
                        color: rgba(37,99,235,220);
                        border: 1px solid rgba(37,99,235,50);
                        border-radius: 11px;
                        font-size: 8px;
                        padding: 0 8px;
                    }
                    QPushButton:hover {
                        background: rgba(37,99,235,22);
                        border-color: rgba(37,99,235,90);
                    }
                """)
                _url = url  # capture for lambda
                chip.clicked.connect(lambda _, u=_url: QDesktopServices.openUrl(QUrl(u)))
                src_row.addWidget(chip)
            src_row.addStretch()
            lay.addLayout(src_row)

        outer.addWidget(card)
        self.setFixedWidth(CARD_W)
        self.adjustSize()

    # ── Timer ─────────────────────────────────────────────────────────────

    def _start_timer(self):
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._remaining -= 50
        if self._remaining <= 0:
            self._dismiss()
            return
        self._progress = max(0.0, self._remaining / DISMISS_MS)
        self.update()

    def _dismiss(self):
        self._timer.stop()
        self.closed.emit(self)

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r = self.rect().adjusted(7, 7, -7, -7)
        rf = QRectF(r)
        path = QPainterPath()
        path.addRoundedRect(rf, 14, 14)

        # Accent color from verdict
        accent = self._verdict_style[0]

        # Drop shadow (tinted by verdict)
        shadow_c = QColor(accent)
        shadow_c.setAlpha(12)
        for off, alpha in ((5, shadow_c.alpha()), (3, 18), (1, 26)):
            sp = QPainterPath()
            sp.addRoundedRect(rf.adjusted(-1, off, 1, off + 2), 14, 14)
            p.fillPath(sp, QColor(accent.red(), accent.green(), accent.blue(), alpha // 3))

        # Glass fill
        p.fillPath(path, BG_GLASS)

        # Subtle top gradient
        grad = QLinearGradient(0, r.top(), 0, r.top() + 40)
        grad.setColorAt(0.0, QColor(255, 255, 255, 90))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, grad)

        # Bright border
        p.setPen(QPen(BORDER_LIGHT, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rf.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)

        # Dark inner ring
        p.setPen(QPen(BORDER_DARK, 0.7))
        p.drawRoundedRect(rf.adjusted(1.5, 1.5, -1.5, -1.5), 13, 13)

        # Verdict accent bar (left, colored)
        bar_h = max(14, r.height() - 40)
        bar_y = r.top() + (r.height() - bar_h) // 2
        p.setBrush(accent)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(r.left(), bar_y, 3.5, bar_h), 2, 2)

        # Progress bar (bottom, verdict-colored)
        if self._progress > 0:
            bar_w = int((r.width() - 24) * self._progress)
            bar_rect = QRectF(r.left() + 12, r.bottom() - 9, max(bar_w, 0), 3)
            acc_prog = QColor(accent)
            acc_prog.setAlpha(130)
            p.setBrush(acc_prog)
            p.drawRoundedRect(bar_rect, 1.5, 1.5)


# ─── Claim card manager ────────────────────────────────────────────────────────

class ClaimCardManager:
    """
    Manages the stack of claim cards on the LEFT side of the screen.
    Same pattern as ToastManager but positioned on the left.
    """

    def __init__(self):
        self._cards: list[ClaimCard] = []
        self._pending_pos = QTimer()
        self._pending_pos.setSingleShot(True)
        self._pending_pos.setInterval(16)
        self._pending_pos.timeout.connect(self._reposition_all)

    def show_claim(self, data: dict) -> None:
        """Show a claim verification result. data = {claim, verdict, explanation, sources}."""
        if len(self._cards) >= MAX_CARDS:
            oldest = self._cards[0]
            oldest.closed.emit(oldest)

        card = ClaimCard(data)
        card.closed.connect(self._on_closed)
        self._cards.append(card)

        screen = QApplication.primaryScreen().availableGeometry()
        # Start off-screen to the left
        card.move(-(CARD_W + 20), screen.bottom())
        card.show()
        self._reposition_all(animate_last=True)

    def _on_closed(self, card: ClaimCard):
        if card in self._cards:
            self._cards.remove(card)
        card.hide()
        card.deleteLater()
        self._pending_pos.start()

    def _reposition_all(self, animate_last: bool = False):
        screen = QApplication.primaryScreen().availableGeometry()
        y = screen.bottom() - CARD_MARGIN

        for i, card in enumerate(reversed(self._cards)):
            card.adjustSize()
            h = card.height()
            ty = y - h
            tx = screen.left() + CARD_MARGIN   # LEFT side of screen
            is_newest = (i == 0)

            anim = QPropertyAnimation(card, b"pos", card)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            if is_newest and animate_last:
                anim.setDuration(320)
                anim.setStartValue(QPoint(-(CARD_W + 20), ty))
            else:
                anim.setDuration(200)
                anim.setStartValue(card.pos())
            anim.setEndValue(QPoint(tx, ty))
            anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

            y -= h + CARD_GAP


# ─── Singleton ────────────────────────────────────────────────────────────────

_manager: Optional[ClaimCardManager] = None


def get_claim_card_manager() -> ClaimCardManager:
    global _manager
    if _manager is None:
        _manager = ClaimCardManager()
    return _manager

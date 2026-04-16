"""
Approval dialog — floats over the screen when Marrow wants to do something
potentially dangerous. Non-blocking: shown as a floating window, resolves
a bridge callback when the user clicks Yes or No.

Wiring:
    bridge.approval_requested → show_approval_dialog()
    User clicks → bridge.respond_to_approval(callback_id, bool)
"""

import logging

from PyQt6.QtCore import Qt, QRectF, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

BG = QColor(16, 16, 20, 245)
BORDER = QColor(255, 255, 255, 22)
RED = QColor(239, 68, 68)
GREEN = QColor(34, 197, 94)

# Keep strong refs to dialogs so Qt doesn't garbage-collect them immediately.
_OPEN_APPROVALS: dict[str, "ApprovalDialog"] = {}


class ApprovalDialog(QWidget):
    """
    Floating yes/no dialog for dangerous action approval.
    Auto-declines after `timeout_seconds` for safety.
    """

    def __init__(
        self,
        description: str,
        command: str,
        callback_id: str,
        timeout_seconds: int = 30,
        parent=None,
    ):
        super().__init__(parent)
        self._callback_id = callback_id
        self._timeout = timeout_seconds
        self._remaining = timeout_seconds
        self._answered = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(340)

        self._build_ui(description, command)
        self._center_screen()

        # Countdown timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._countdown_tick)
        self._timer.start(1000)

    def _build_ui(self, description: str, command: str) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        # Warning label
        warn = QLabel("⚠  Action Requires Approval")
        warn.setStyleSheet("color: #fbbf24; font-size: 12px; font-weight: bold;")
        lay.addWidget(warn)

        # Description
        desc_lbl = QLabel(description)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet("color: rgba(230,230,235,255); font-size: 11px;")
        lay.addWidget(desc_lbl)

        # Command (monospace)
        if command:
            cmd_lbl = QLabel(command[:160])
            cmd_lbl.setWordWrap(True)
            cmd_lbl.setStyleSheet(
                "color: rgba(160,160,170,255); font-size: 10px; "
                "font-family: Consolas, monospace; "
                "background: rgba(0,0,0,60); border-radius: 4px; padding: 6px;"
            )
            lay.addWidget(cmd_lbl)

        # Countdown
        self._countdown_lbl = QLabel(f"Auto-declining in {self._remaining}s")
        self._countdown_lbl.setStyleSheet(
            "color: rgba(120,120,130,200); font-size: 9px;"
        )
        lay.addWidget(self._countdown_lbl)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._no_btn = QPushButton(f"✕  No, block it")
        self._no_btn.setFixedHeight(34)
        self._no_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(239,68,68,180);
                color: white; border: none;
                border-radius: 8px; font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover  {{ background: rgba(239,68,68,230); }}
            QPushButton:pressed {{ background: rgba(220,38,38,230); }}
        """)
        self._no_btn.clicked.connect(self._decline)

        self._yes_btn = QPushButton("✓  Yes, allow")
        self._yes_btn.setFixedHeight(34)
        self._yes_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(34,197,94,160);
                color: white; border: none;
                border-radius: 8px; font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover  {{ background: rgba(34,197,94,210); }}
            QPushButton:pressed {{ background: rgba(22,163,74,210); }}
        """)
        self._yes_btn.clicked.connect(self._approve)

        btn_row.addWidget(self._no_btn)
        btn_row.addWidget(self._yes_btn)
        lay.addLayout(btn_row)

    def _center_screen(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        x = screen.center().x() - self.width() // 2
        y = screen.top() + 80
        self.move(x, y)

    def _countdown_tick(self) -> None:
        self._remaining -= 1
        self._countdown_lbl.setText(f"Auto-declining in {self._remaining}s")
        if self._remaining <= 0:
            self._decline()

    def _approve(self) -> None:
        if not self._answered:
            self._answered = True
            self._respond(True)

    def _decline(self) -> None:
        if not self._answered:
            self._answered = True
            self._respond(False)

    def _respond(self, approved: bool) -> None:
        self._timer.stop()
        try:
            from ui.bridge import get_bridge

            get_bridge().respond_to_approval(self._callback_id, approved)
        except Exception as e:
            log.warning(f"Approval response failed: {e}")
        self.close()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        p.fillPath(path, BG)
        p.setPen(QPen(QColor(251, 191, 36, 80), 1.0))  # amber border
        p.drawPath(path)


def show_approval_dialog(description: str, command: str, callback_id: str) -> None:
    """
    Slot connected to bridge.approval_requested.
    Called on the Qt main thread.
    """
    # Replace existing dialog for same callback id (safety)
    old = _OPEN_APPROVALS.get(callback_id)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass

    dlg = ApprovalDialog(description, command, callback_id)
    _OPEN_APPROVALS[callback_id] = dlg

    def _cleanup(*_):
        _OPEN_APPROVALS.pop(callback_id, None)

    dlg.destroyed.connect(_cleanup)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()

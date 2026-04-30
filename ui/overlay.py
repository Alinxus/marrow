"""Compact always-on-top mission overlay."""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import config

log = logging.getLogger(__name__)


def _label(text: str, color: str, size: int, bold: bool = False) -> QLabel:
    widget = QLabel(text)
    weight = "600" if bold else "400"
    widget.setStyleSheet(
        f"color: {color}; font-size: {size}pt; font-weight: {weight}; background: transparent;"
    )
    widget.setWordWrap(True)
    return widget


class MarrowOverlay(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(300, 150)
        self._app = "Unknown app"
        self._state = "idle"
        self._action = "Waiting"
        self._next = ""
        self._confidence = 0.0
        self._aux = ""
        self._kind = ""
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._build_ui()
        self._connect_bridge()
        self._position_default()
        self.hide()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(6)

        self._app_lbl = _label("Unknown app", "rgba(240,244,255,0.95)", 8, True)
        self._state_lbl = _label("idle", "rgba(190,205,255,0.9)", 8, True)
        self._action_lbl = _label("Waiting", "white", 11, True)
        self._next_lbl = _label("", "rgba(218,223,235,0.85)", 8)
        self._meta_lbl = _label("confidence 0%", "rgba(130,219,182,0.95)", 8)
        self._aux_lbl = _label("", "rgba(200,210,230,0.8)", 8)

        root.addWidget(self._app_lbl)
        root.addWidget(self._state_lbl)
        root.addWidget(self._action_lbl)
        root.addWidget(self._next_lbl)
        root.addWidget(self._meta_lbl)
        root.addWidget(self._aux_lbl)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for label, command in (
            ("Pause", "/mission pause"),
            ("Resume", "/mission resume"),
            ("Rollback", "/mission rollback"),
            ("Details", "/mission status"),
        ):
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.08); color: white; border: 1px solid rgba(255,255,255,0.12); border-radius: 10px; padding: 4px 8px; font-size: 8pt; }"
                "QPushButton:hover { background: rgba(255,255,255,0.16); }"
            )
            btn.clicked.connect(lambda _=False, text=command: self._submit(text))
            buttons.addWidget(btn)
        root.addLayout(buttons)

    def _connect_bridge(self) -> None:
        try:
            from ui.bridge import get_bridge

            bridge = get_bridge()
            bridge.overlay_update.connect(self._on_overlay_update)
            bridge.mission_update.connect(self._on_mission_update)
            bridge.agent_update.connect(self._on_agent_update)
            bridge.focus_changed.connect(self._on_focus_changed)
        except Exception as exc:
            log.warning(f"Overlay bridge connect failed: {exc}")

    def _submit(self, text: str) -> None:
        try:
            from ui.bridge import get_bridge

            get_bridge().text_task_submitted.emit(text)
        except Exception as exc:
            log.warning(f"Overlay command dispatch failed: {exc}")

    def _position_default(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 20, 20)

    def _on_focus_changed(self, app_name: str, window_title: str) -> None:
        label = app_name or "Unknown app"
        if window_title:
            label += f" · {window_title[:26]}"
        self._app = label
        self._app_lbl.setText(f"Seeing: {label}")

    def _on_overlay_update(self, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        self._kind = payload.get("kind", self._kind)
        self._state = payload.get("state", self._state)
        self._action = payload.get("current_action", self._action)
        self._next = payload.get("next_step", self._next)
        self._confidence = float(payload.get("confidence", self._confidence or 0.0))
        self._aux = payload.get("body", self._aux)
        self._render()

    def _on_mission_update(self, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        self._kind = "mission"
        self._state = payload.get("state", self._state)
        step = payload.get("step") or {}
        self._action = step.get("title", self._action)
        self._next = step.get("description", self._next)
        self._confidence = float(payload.get("confidence", self._confidence or 0.0))
        self._render()

    def _on_agent_update(self, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        role = payload.get("role")
        status = payload.get("status")
        if role and status:
            self._aux = f"{role}: {status}"
            self._render()

    def _render(self) -> None:
        self._state_lbl.setText(f"Mission: {self._state}")
        self._action_lbl.setText(self._action or "Waiting")
        self._next_lbl.setText(self._next[:110])
        self._meta_lbl.setText(f"confidence {int(self._confidence * 100)}%")
        self._aux_lbl.setText(self._aux[:120])
        if not config.OVERLAY_ENABLED:
            return
        persistent = self._kind == "mission" and self._state in {
            "planning",
            "executing",
            "paused",
            "verifying",
            "rollback",
        }
        if not self.isVisible():
            self.show()
        self.raise_()
        if persistent:
            self._hide_timer.stop()
        else:
            self._hide_timer.start(7000)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(10, 14, 24, 235))
        painter.setPen(QPen(QColor(255, 255, 255, 24), 1.0))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 16, 16)

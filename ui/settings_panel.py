"""
Settings panel — floating window for configuring Marrow.

Sections:
  1. LLM Provider   — Anthropic / OpenAI / Ollama + model names
  2. API Keys        — editable key fields (masked)
  3. Behaviour       — intervals, cooldown, hotkey
  4. Voice           — ElevenLabs key, voice ID, whisper model
  5. About           — version, links

Changes are written to ~/.marrow/.env and reloaded via dotenv.
The LLM client is reset so the next call picks up new settings.
"""

import logging
import os
import subprocess
from pathlib import Path

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

BG = QColor(14, 14, 18, 250)
BORDER = QColor(255, 255, 255, 20)
W = 420

try:
    import config as _cfg

    ENV_FILE = Path(getattr(_cfg, "ENV_FILE", Path.home() / ".marrow" / ".env"))
except Exception:
    ENV_FILE = Path.home() / ".marrow" / ".env"


def _section(title: str) -> QLabel:
    lbl = QLabel(title)
    lbl.setStyleSheet(
        "color: rgba(140,140,150,200); font-size: 9px; font-weight: bold; "
        "letter-spacing: 1px; padding-top: 8px;"
    )
    return lbl


def _field(placeholder: str = "", password: bool = False) -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    if password:
        f.setEchoMode(QLineEdit.EchoMode.Password)
    f.setStyleSheet("""
        QLineEdit {
            background: rgba(255,255,255,10);
            border: 1px solid rgba(255,255,255,20);
            border-radius: 6px;
            color: rgba(220,220,230,255);
            padding: 5px 8px;
            font-size: 11px;
        }
        QLineEdit:focus { border: 1px solid rgba(96,165,250,160); }
    """)
    return f


def _combo(options: list[str]) -> QComboBox:
    c = QComboBox()
    for o in options:
        c.addItem(o)
    c.setStyleSheet("""
        QComboBox {
            background: rgba(255,255,255,10);
            border: 1px solid rgba(255,255,255,20);
            border-radius: 6px;
            color: rgba(220,220,230,255);
            padding: 5px 8px;
            font-size: 11px;
        }
        QComboBox::drop-down { border: none; }
        QComboBox QAbstractItemView {
            background: rgba(20,20,26,255);
            color: rgba(220,220,230,255);
            border: 1px solid rgba(255,255,255,20);
            selection-background-color: rgba(96,165,250,120);
        }
    """)
    return c


def _editable_combo(default: str = "") -> QComboBox:
    c = _combo([])
    c.setEditable(True)
    if default:
        c.setCurrentText(default)
    return c


FORM_LABEL_STYLE = "color: rgba(180,180,190,220); font-size: 10px;"


class MarrowSettingsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(W)

        self._build_ui()
        self._load_values()
        self._center_screen()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Header
        hdr = QWidget()
        hdr.setFixedHeight(44)
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(16, 0, 10, 0)

        title = QLabel("⚙  MARROW SETTINGS")
        title.setStyleSheet(
            "color: rgba(230,230,235,255); font-size: 11px; font-weight: bold; letter-spacing: 2px;"
        )
        hdr_lay.addWidget(title)
        hdr_lay.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: rgba(140,140,150,200);
                          border: none; font-size: 12px; }
            QPushButton:hover { color: rgba(230,230,235,255); }
        """)
        close_btn.clicked.connect(self.close)
        hdr_lay.addWidget(close_btn)
        outer.addWidget(hdr)

        # Tabs
        tabs = QTabWidget()
        tabs.setStyleSheet("""
            QTabWidget::pane {
                border: none;
                background: transparent;
            }
            QTabBar::tab {
                background: transparent;
                color: rgba(140,140,150,200);
                padding: 6px 14px;
                font-size: 10px;
                border-bottom: 2px solid transparent;
            }
            QTabBar::tab:selected {
                color: rgba(96,165,250,255);
                border-bottom: 2px solid rgba(96,165,250,200);
            }
        """)
        tabs.addTab(self._tab_llm(), "LLM")
        tabs.addTab(self._tab_behaviour(), "Behaviour")
        tabs.addTab(self._tab_voice(), "Voice")
        tabs.addTab(self._tab_about(), "About")
        outer.addWidget(tabs)

        # Save button
        save_btn = QPushButton("Save & Apply")
        save_btn.setFixedHeight(36)
        save_btn.setStyleSheet("""
            QPushButton {
                background: rgba(96,165,250,180);
                color: white; border: none;
                border-radius: 8px; font-size: 11px; font-weight: bold;
                margin: 10px 16px 14px 16px;
            }
            QPushButton:hover  { background: rgba(96,165,250,220); }
            QPushButton:pressed { background: rgba(59,130,246,220); }
        """)
        save_btn.clicked.connect(self._save)
        outer.addWidget(save_btn)

    # ── Tab: LLM ──────────────────────────────────────────────────────────

    def _tab_llm(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(6)

        lay.addWidget(_section("PROVIDER"))
        self._provider = _combo(["auto", "anthropic", "openai", "ollama", "none"])
        lay.addWidget(self._provider)

        lay.addWidget(_section("ANTHROPIC"))
        form1 = QFormLayout()
        form1.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._ant_key = _field("sk-ant-…", password=True)
        self._ant_reasoning = _field("claude-sonnet-4-6")
        self._ant_scoring = _field("claude-haiku-4-5-20251001")
        self._ant_vision = _field("claude-haiku-4-5-20251001")
        for lbl, w2 in [
            ("API Key", self._ant_key),
            ("Reasoning model", self._ant_reasoning),
            ("Scoring model", self._ant_scoring),
            ("Vision model", self._ant_vision),
        ]:
            ql = QLabel(lbl)
            ql.setStyleSheet(FORM_LABEL_STYLE)
            form1.addRow(ql, w2)
        lay.addLayout(form1)

        lay.addWidget(_section("OPENAI"))
        form2 = QFormLayout()
        form2.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._oai_key = _field("sk-…", password=True)
        self._oai_reasoning = _field("gpt-4o")
        self._oai_scoring = _field("gpt-4o-mini")
        self._oai_vision = _field("gpt-4o-mini")
        for lbl, w3 in [
            ("API Key", self._oai_key),
            ("Reasoning model", self._oai_reasoning),
            ("Scoring model", self._oai_scoring),
            ("Vision model", self._oai_vision),
        ]:
            ql = QLabel(lbl)
            ql.setStyleSheet(FORM_LABEL_STYLE)
            form2.addRow(ql, w3)
        lay.addLayout(form2)

        lay.addWidget(_section("OLLAMA (local)"))
        form3 = QFormLayout()
        form3.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._ollama_url = _field("http://localhost:11434")
        self._ollama_reasoning = _editable_combo("llama3.2")
        self._ollama_scoring = _editable_combo("llama3.2")
        self._ollama_vision = _editable_combo("llava")
        for lbl, w4 in [
            ("Base URL", self._ollama_url),
            ("Reasoning model", self._ollama_reasoning),
            ("Scoring model", self._ollama_scoring),
            ("Vision model", self._ollama_vision),
        ]:
            ql = QLabel(lbl)
            ql.setStyleSheet(FORM_LABEL_STYLE)
            form3.addRow(ql, w4)
        lay.addLayout(form3)

        detect_btn = QPushButton("Detect installed Ollama models")
        detect_btn.setFixedHeight(30)
        detect_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,12);
                color: rgba(220,220,230,240);
                border: 1px solid rgba(255,255,255,22);
                border-radius: 8px;
                font-size: 10px;
            }
            QPushButton:hover { background: rgba(255,255,255,20); }
        """)
        detect_btn.clicked.connect(self._populate_ollama_models)
        lay.addWidget(detect_btn)

        lay.addStretch()
        return w

    # ── Tab: Behaviour ────────────────────────────────────────────────────

    def _tab_behaviour(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(6)

        lay.addWidget(_section("INTERVALS"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._reasoning_interval = _field("30")
        self._cooldown = _field("120")
        self._screenshot_interval = _field("3")
        self._context_window = _field("60")
        for lbl, ww in [
            ("Reasoning interval (s)", self._reasoning_interval),
            ("Interrupt cooldown (s)", self._cooldown),
            ("Screenshot interval (s)", self._screenshot_interval),
            ("Context window (s)", self._context_window),
        ]:
            ql = QLabel(lbl)
            ql.setStyleSheet(FORM_LABEL_STYLE)
            form.addRow(ql, ww)
        lay.addLayout(form)

        lay.addWidget(_section("FEATURES"))
        self._hotkey_enabled = QCheckBox("Hotkey activation (Ctrl+Shift+M)")
        self._wake_word_enabled = QCheckBox('Wake word ("Marrow")')
        self._screenshot_save = QCheckBox("Save screenshots to disk")
        self._tray_enabled = QCheckBox("Show system tray icon")
        for cb in (
            self._hotkey_enabled,
            self._wake_word_enabled,
            self._screenshot_save,
            self._tray_enabled,
        ):
            cb.setStyleSheet("color: rgba(200,200,210,220); font-size: 10px;")
            lay.addWidget(cb)

        lay.addWidget(_section("APPROVAL MODE"))
        self._approval_mode = _combo(["guarded", "unlocked"])
        lay.addWidget(self._approval_mode)

        lay.addStretch()
        return w

    # ── Tab: Voice ────────────────────────────────────────────────────────

    def _tab_voice(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(6)

        lay.addWidget(_section("ELEVENLABS"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._el_key = _field("…", password=True)
        self._el_voice = _field("BAMYoBHLZM7lJgJAmFz0")
        for lbl, ww in [("API Key", self._el_key), ("Voice ID", self._el_voice)]:
            ql = QLabel(lbl)
            ql.setStyleSheet(FORM_LABEL_STYLE)
            form.addRow(ql, ww)
        lay.addLayout(form)
        note = QLabel("Leave key blank to use Windows SAPI (offline fallback).")
        note.setStyleSheet("color: rgba(120,120,130,180); font-size: 9px;")
        note.setWordWrap(True)
        lay.addWidget(note)

        lay.addWidget(_section("SPEECH RECOGNITION"))
        form2 = QFormLayout()
        form2.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._whisper_model = _combo(["tiny", "base", "small", "medium", "large"])
        ql = QLabel("Whisper model")
        ql.setStyleSheet(FORM_LABEL_STYLE)
        form2.addRow(ql, self._whisper_model)
        lay.addLayout(form2)

        lay.addWidget(_section("IDENTITY"))
        form3 = QFormLayout()
        form3.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._marrow_name = _field("Marrow")
        ql = QLabel("Name")
        ql.setStyleSheet(FORM_LABEL_STYLE)
        form3.addRow(ql, self._marrow_name)
        lay.addLayout(form3)

        lay.addStretch()
        return w

    # ── Tab: About ────────────────────────────────────────────────────────

    def _tab_about(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 12, 16, 8)
        lay.setSpacing(8)

        title = QLabel("Marrow")
        title.setStyleSheet(
            "color: rgba(230,230,235,255); font-size: 16px; font-weight: bold;"
        )
        lay.addWidget(title)

        sub = QLabel("Ambient intelligence that lives with you.")
        sub.setStyleSheet("color: rgba(140,140,150,200); font-size: 10px;")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        info = QLabel(
            "Providers: Anthropic Claude · OpenAI GPT · Ollama (local)\n"
            "Voice: ElevenLabs streaming · Windows SAPI fallback\n"
            "Audio: faster-whisper (local Whisper)\n"
            "Vision: Claude Haiku semantic OCR\n"
            "Storage: SQLite WAL + FTS5 trigram index"
        )
        info.setStyleSheet(
            "color: rgba(120,120,130,200); font-size: 9px; line-height: 1.6;"
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        lay.addStretch()
        return w

    # ── Load / Save ───────────────────────────────────────────────────────

    def _load_values(self) -> None:
        import config

        self._provider.setCurrentText(config.LLM_PROVIDER)
        self._ant_key.setText(config.ANTHROPIC_API_KEY)
        self._ant_reasoning.setText(config.REASONING_MODEL)
        self._ant_scoring.setText(config.SCORING_MODEL)
        self._ant_vision.setText(config.VISION_MODEL)
        self._oai_key.setText(config.OPENAI_API_KEY)
        self._oai_reasoning.setText(config.OPENAI_REASONING_MODEL)
        self._oai_scoring.setText(config.OPENAI_SCORING_MODEL)
        self._oai_vision.setText(config.OPENAI_VISION_MODEL)
        self._ollama_url.setText(config.OLLAMA_BASE_URL)
        self._ollama_reasoning.setCurrentText(config.OLLAMA_REASONING_MODEL)
        self._ollama_scoring.setCurrentText(config.OLLAMA_SCORING_MODEL)
        self._ollama_vision.setCurrentText(config.OLLAMA_VISION_MODEL)

        self._reasoning_interval.setText(str(config.REASONING_INTERVAL))
        self._cooldown.setText(str(config.INTERRUPT_COOLDOWN))
        self._screenshot_interval.setText(str(config.SCREENSHOT_INTERVAL))
        self._context_window.setText(str(config.CONTEXT_WINDOW_SECONDS))

        self._hotkey_enabled.setChecked(config.HOTKEY_ENABLED)
        self._wake_word_enabled.setChecked(config.WAKE_WORD_ENABLED)
        self._screenshot_save.setChecked(config.SCREENSHOT_SAVE_TO_DISK)
        self._tray_enabled.setChecked(config.TRAY_ENABLED)

        self._el_key.setText(config.ELEVENLABS_API_KEY)
        self._el_voice.setText(config.MARROW_VOICE_ID)
        self._marrow_name.setText(config.MARROW_NAME)

        whisper = config.WHISPER_MODEL
        idx = self._whisper_model.findText(whisper)
        if idx >= 0:
            self._whisper_model.setCurrentIndex(idx)

    def _save(self) -> None:
        """Write settings to ~/.marrow/.env and reload config."""
        vals = {
            "LLM_PROVIDER": self._provider.currentText(),
            "ANTHROPIC_API_KEY": self._ant_key.text(),
            "REASONING_MODEL": self._ant_reasoning.text(),
            "SCORING_MODEL": self._ant_scoring.text(),
            "VISION_MODEL": self._ant_vision.text(),
            "OPENAI_API_KEY": self._oai_key.text(),
            "OPENAI_REASONING_MODEL": self._oai_reasoning.text(),
            "OPENAI_SCORING_MODEL": self._oai_scoring.text(),
            "OPENAI_VISION_MODEL": self._oai_vision.text(),
            "OLLAMA_BASE_URL": self._ollama_url.text(),
            "OLLAMA_REASONING_MODEL": self._ollama_reasoning.currentText(),
            "OLLAMA_SCORING_MODEL": self._ollama_scoring.currentText(),
            "OLLAMA_VISION_MODEL": self._ollama_vision.currentText(),
            "REASONING_INTERVAL": self._reasoning_interval.text(),
            "INTERRUPT_COOLDOWN": self._cooldown.text(),
            "SCREENSHOT_INTERVAL": self._screenshot_interval.text(),
            "CONTEXT_WINDOW_SECONDS": self._context_window.text(),
            "HOTKEY_ENABLED": "1" if self._hotkey_enabled.isChecked() else "0",
            "WAKE_WORD_ENABLED": "1" if self._wake_word_enabled.isChecked() else "0",
            "SCREENSHOT_SAVE_TO_DISK": "1"
            if self._screenshot_save.isChecked()
            else "0",
            "TRAY_ENABLED": "1" if self._tray_enabled.isChecked() else "0",
            "ELEVENLABS_API_KEY": self._el_key.text(),
            "MARROW_VOICE_ID": self._el_voice.text(),
            "MARROW_NAME": self._marrow_name.text(),
            "WHISPER_MODEL": self._whisper_model.currentText(),
        }

        # Write .env
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f'{k}="{str(v).replace('"', '\\"')}"\n'
            for k, v in vals.items()
            if v is not None
        ]
        ENV_FILE.write_text("".join(lines), encoding="utf-8")

        # Hot-reload config
        try:
            from dotenv import load_dotenv

            load_dotenv(str(ENV_FILE), override=True)
            import importlib, config as cfg

            importlib.reload(cfg)
            from brain.llm import reset_client

            reset_client()
            log.info("Settings saved and applied.")
        except Exception as e:
            log.warning(f"Hot-reload failed: {e}")

        self.close()

    def _detect_ollama_models(self) -> list[str]:
        try:
            p = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=6,
            )
            if p.returncode != 0:
                return []
            models = []
            for line in (p.stdout or "").splitlines():
                s = line.strip()
                if not s or s.lower().startswith("name"):
                    continue
                name = s.split()[0].strip()
                if name and name not in models:
                    models.append(name)
            return models
        except Exception:
            return []

    def _populate_ollama_models(self) -> None:
        models = self._detect_ollama_models()
        if not models:
            log.warning("No Ollama models detected. Is `ollama` installed and running?")
            return

        for combo in (
            self._ollama_reasoning,
            self._ollama_scoring,
            self._ollama_vision,
        ):
            current = combo.currentText().strip()
            combo.clear()
            combo.addItems(models)
            if current:
                combo.setCurrentText(current)

    # ── Paint & drag ──────────────────────────────────────────────────────

    def _center_screen(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        x = screen.center().x() - self.width() // 2
        y = screen.center().y() - self.height() // 2
        self.move(x, y)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        p.fillPath(path, BG)
        p.setPen(QPen(BORDER, 1.0))
        p.drawPath(path)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if hasattr(self, "_drag_pos") and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, _event):
        self._drag_pos = None

"""
PySide6 QWebEngineView window for Marrow's React UI.
Serves ui-tauri/dist/index.html in a frameless always-on-top window.
Falls back gracefully if WebEngine isn't available.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DIST = Path(__file__).resolve().parent.parent / "ui-tauri" / "dist" / "index.html"


def create_react_window(app):
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEngineSettings
        from PySide6.QtCore import Qt, QUrl
        from PySide6.QtWidgets import QWidget, QVBoxLayout
        from PySide6.QtGui import QScreen
    except ImportError as e:
        log.warning(f"QWebEngineView unavailable: {e}")
        return None

    if not _DIST.exists():
        log.warning("React dist not built — run: cd ui-tauri && npm run build")
        return None

    screen = app.primaryScreen()
    sg = screen.availableGeometry()
    win_w, win_h = 420, 62

    window = QWidget()
    window.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
    )
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    window.resize(win_w, win_h)
    window.move(sg.right() - win_w - 24, sg.bottom() - win_h - 52)

    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)

    view = QWebEngineView()
    view.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    view.page().setBackgroundColor(Qt.GlobalColor.transparent)
    view.setUrl(QUrl.fromLocalFile(str(_DIST)))
    layout.addWidget(view)

    # Resize window when React content signals a height change via page title
    def _on_title_changed(title: str):
        if title.startswith("h:"):
            try:
                h = int(title[2:])
                window.resize(win_w, max(62, min(h, 700)))
                window.move(sg.right() - win_w - 24, sg.bottom() - window.height() - 52)
            except ValueError:
                pass

    view.titleChanged.connect(_on_title_changed)
    window.show()
    return window

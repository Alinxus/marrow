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
    if screen is None:
        log.warning("No primary screen detected for React window")
        return None
    sg = screen.availableGeometry()
    win_w, win_h = 210, 50
    top_margin = 20

    def _move_top_center(width: int, _height: int) -> None:
        x = sg.left() + (sg.width() - width) // 2
        window.move(x, sg.top() + top_margin)

    window = QWidget()
    window.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
    )
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    window.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    window.resize(win_w, win_h)
    _move_top_center(win_w, win_h)

    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)

    view = QWebEngineView()
    view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    view.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    view.page().setBackgroundColor(Qt.GlobalColor.transparent)
    view.setUrl(QUrl.fromLocalFile(str(_DIST)))
    layout.addWidget(view)

    def _on_load_finished(ok: bool):
        if not ok:
            log.warning("React UI failed to load in QWebEngineView")
            return
        # Keep host window geometry synced to React content.
        view.page().runJavaScript(
            """
            (function () {
              if (window.__marrowGeometryObserverInstalled) return;
              window.__marrowGeometryObserverInstalled = true;
              const send = () => {
                const root = document.getElementById('root');
                const host = root && root.firstElementChild;
                const rect = host ? host.getBoundingClientRect() : document.body.getBoundingClientRect();
                const w = Math.ceil(rect.width || 40);
                const h = Math.ceil(rect.height || 14);
                document.title = 'size:' + w + 'x' + h;
              };
              const ro = new ResizeObserver(send);
              if (document.body) ro.observe(document.body);
              const root = document.getElementById('root');
              if (root) ro.observe(root);
              window.addEventListener('load', send);
              window.addEventListener('resize', send);
              send();
            })();
            """
        )

    # Resize window when React content signals a height change via page title
    def _on_title_changed(title: str):
        if title.startswith("size:"):
            try:
                dim = title[5:].split("x", 1)
                w = max(40, min(int(dim[0]), 430))
                h = max(14, min(int(dim[1]), 760))
                window.resize(w, h)
                _move_top_center(w, h)
            except ValueError:
                pass

    view.loadFinished.connect(_on_load_finished)
    view.titleChanged.connect(_on_title_changed)
    window.show()
    log.info(f"React UI loaded from {_DIST}")
    return window

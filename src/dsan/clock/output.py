"""The output: what the presenter / confidence monitor sees.

Modes:
  window      an ordinary resizable window (handy for NDI/screen-capture tools)
  fullscreen  borderless full screen on a chosen display (a confidence monitor)
  overlay     transparent, always on top and click-through, covering a chosen
              display, so the clock sits over PowerPoint / Keynote / anything
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QKeyEvent, QPainter, QScreen
from PyQt6.QtWidgets import QWidget

from .layout import Layout
from .render import Frame, Renderer

MODES = {"window": "Window", "fullscreen": "Full screen", "overlay": "Overlay (over slides)"}


def _mac_float_over_fullscreen(widget: QWidget) -> bool:
    """macOS: keep the overlay above other apps' full-screen slideshows.

    Qt's stay-on-top uses the floating window level, which doesn't reach into
    another app's full-screen Space. With pyobjc installed we raise the window
    level and let it join every Space as a full-screen auxiliary window.
    Returns True if it was applied.
    """
    if sys.platform != "darwin":
        return False
    try:
        import objc  # type: ignore
        view = objc.objc_object(c_void_p=int(widget.winId()))
        win = view.window()
        if win is None:
            return False
        CAN_JOIN_ALL_SPACES, STATIONARY, IGNORES_CYCLE, FULLSCREEN_AUX = 1 << 0, 1 << 4, 1 << 6, 1 << 8
        win.setLevel_(1000)  # NSScreenSaverWindowLevel
        win.setCollectionBehavior_(CAN_JOIN_ALL_SPACES | STATIONARY | IGNORES_CYCLE | FULLSCREEN_AUX)
        win.setHidesOnDeactivate_(False)
        win.setIgnoresMouseEvents_(True)
        return True
    except Exception:
        return False


class OutputWindow(QWidget):
    closed = pyqtSignal()

    def __init__(self, frame_source: Callable[[], Frame], layout_source: Callable[[], Layout],
                 renderer: Optional[Renderer] = None):
        super().__init__(None)
        self._frame = frame_source
        self._layout = layout_source
        self.renderer = renderer or Renderer()
        self.mode = "window"
        self.screen_name: Optional[str] = None
        self.mac_float_applied = False
        self.setWindowTitle("DSAN Clock — Output")
        self.resize(960, 540)
        self.setStyleSheet("background: black;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(50)   # 20 fps: smooth blinking and clock seconds

    # -- placement ---------------------------------------------------------------

    @staticmethod
    def screens():
        return QGuiApplication.screens()

    def _target_screen(self) -> QScreen:
        for s in self.screens():
            if s.name() == self.screen_name:
                return s
        return QGuiApplication.primaryScreen()

    def apply(self, mode: str, screen_name: Optional[str]) -> None:
        """(Re)show the output in a mode on a screen."""
        self.mode = mode if mode in MODES else "window"
        self.screen_name = screen_name
        was_visible = self.isVisible()
        self.hide()
        flags = Qt.WindowType.Window
        overlay = self.mode == "overlay"
        if overlay:
            flags = (Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                     | Qt.WindowType.WindowTransparentForInput
                     | Qt.WindowType.WindowDoesNotAcceptFocus)
        elif self.mode == "fullscreen":
            flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, overlay)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, overlay)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, overlay)
        self.setStyleSheet("" if overlay else "background: black;")
        geo = self._target_screen().geometry()
        if self.mode == "window":
            if not was_visible or self.width() >= geo.width():
                self.resize(960, 540)
                self.move(geo.center().x() - 480, geo.center().y() - 270)
            self.showNormal()
        else:
            self.setGeometry(geo)
            if self.mode == "fullscreen":
                self.showFullScreen()
            else:
                self.show()
            if overlay:
                self.mac_float_applied = _mac_float_over_fullscreen(self)
        self.raise_()

    # -- painting ----------------------------------------------------------------

    def paintEvent(self, event):
        p = QPainter(self)
        rect = QRectF(self.rect())
        if self.mode == "overlay":
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(rect, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        else:
            p.fillRect(rect, Qt.GlobalColor.black)
        self.renderer.paint(p, rect, self._layout(), self._frame())
        p.end()

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() == Qt.Key.Key_Escape and self.mode == "fullscreen":
            self.apply("window", self.screen_name)
        elif e.key() in (Qt.Key.Key_F, Qt.Key.Key_F11) and self.mode != "overlay":
            self.apply("window" if self.mode == "fullscreen" else "fullscreen", self.screen_name)
        else:
            super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self.mode != "overlay":
            self.apply("window" if self.mode == "fullscreen" else "fullscreen", self.screen_name)

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)


__all__ = ["MODES", "OutputWindow"]

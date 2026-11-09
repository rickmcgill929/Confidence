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


_CAN_JOIN_ALL_SPACES, _STATIONARY, _IGNORES_CYCLE, _FULLSCREEN_NONE, _FULLSCREEN_AUX = (
    1 << 0, 1 << 4, 1 << 6, 1 << 9, 1 << 8)


def _mac_window(widget: QWidget, *, level: int, click_through: bool) -> bool:
    """macOS window tweaks Qt can't express (needs pyobjc). Returns True if applied.

    * level 25 (status bar) puts a full-screen output above that display's menu
      bar and Dock without using macOS's own full screen, which takes a Space and
      blacks out the other display when displays share Spaces.
    * level 1000 (screen saver) lets the overlay float over full-screen slideshows.
    * Full-screen-auxiliary + all Spaces keeps the output up when Keynote or
      PowerPoint goes full screen on the laptop. Never hide when the app loses focus.
    """
    if sys.platform != "darwin":
        return False
    try:
        import objc  # type: ignore
        view = objc.objc_object(c_void_p=int(widget.winId()))
        win = view.window()
        if win is None:
            return False
        win.setLevel_(level)
        win.setCollectionBehavior_(_CAN_JOIN_ALL_SPACES | _STATIONARY | _IGNORES_CYCLE | _FULLSCREEN_AUX)
        win.setHidesOnDeactivate_(False)
        win.setIgnoresMouseEvents_(click_through)
        return True
    except Exception:
        return False


def _mac_float_over_fullscreen(widget: QWidget) -> bool:
    return _mac_window(widget, level=1000, click_through=True)


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
        self.setWindowTitle("Confidence — Output")
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
        """(Re)show the output in a mode on a screen.

        "Full screen" is a borderless window covering exactly the chosen display,
        not the OS's own full-screen mode. macOS full screen moves the window into
        its own Space (and with "Displays have separate Spaces" off, blanks every
        other display); Windows' can jump to the wrong monitor. A screen-sized
        borderless window does neither and leaves the laptop display alone.
        """
        self.mode = mode if mode in MODES else "window"
        self.screen_name = screen_name
        was_visible = self.isVisible()
        self.hide()
        self.setWindowState(Qt.WindowState.WindowNoState)
        overlay = self.mode == "overlay"
        full = self.mode == "fullscreen"
        flags = Qt.WindowType.Window
        if overlay:
            flags = (Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                     | Qt.WindowType.WindowTransparentForInput
                     | Qt.WindowType.WindowDoesNotAcceptFocus)
        elif full:
            flags = (Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowStaysOnTopHint)
            if sys.platform == "darwin":
                flags |= Qt.WindowType.NoDropShadowWindowHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, overlay)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, overlay)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, overlay or full)
        self.setStyleSheet("" if overlay else "background: black;")
        self.setCursor(Qt.CursorShape.BlankCursor if full else Qt.CursorShape.ArrowCursor)
        screen = self._target_screen()
        geo = screen.geometry()
        if self.mode == "window":
            if not was_visible or self.width() >= geo.width():
                self.resize(960, 540)
                self.move(geo.center().x() - 480, geo.center().y() - 270)
            self.showNormal()
            self.mac_float_applied = False
        else:
            self.setScreen(screen)
            self.setGeometry(geo)
            self.show()
            self.setGeometry(geo)          # some window managers adjust the first placement
            level = 1000 if overlay else 25
            self.mac_float_applied = _mac_window(self, level=level, click_through=overlay)
        if QGuiApplication.platformName() != "offscreen":      # (offscreen just warns)
            self.raise_()

    def on_screen(self) -> Optional[str]:
        """Name of the display the output is actually on."""
        s = self.screen()
        return s.name() if s else None

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
        # px_scale: frame widths etc. are in device pixels of the display
        self.renderer.paint(p, rect, self._layout(), self._frame(), px_scale=1.0 / self.devicePixelRatioF())
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

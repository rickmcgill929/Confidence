"""Confidence — show timing on screen and SDI: a DSAN Limitimer and Perfect Cue over USB-RS485,
Millumin playback over OSC, and OSC control.

    pip install 'dsan[clock]'
    confidence

The control window holds the inputs, the output settings, the operator message
and the layout editor. The output window draws the layout full screen, in a
window, or as a click-through overlay on top of slides.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import threading
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QRegularExpression, QSettings, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFontDatabase, QGuiApplication, QKeySequence, \
    QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTextBrowser, QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from .. import version as ver
from .. import perfectcue as pc
from ..devices import Limitimer, LinkState, PerfectCue
from ..limitimer import LimitimerState
from . import ports as portsmod
from .layout import (COMMON_FIELDS, ELEMENT_TYPES, PRESETS, SOURCES, TYPE_FIELDS,
                     Element, Layout, default_element, format_color, parse_color)
from . import align as al
from . import files
from .widgets import Combo, ElidingLabel, FlowLayout
from .debug import DebugTap, DebugWindow
from .decklink_panel import DeckLinkPanel
from .ltcontrol import LimitimerPanel
from ..emulator import LimitimerBus
from .editor import EditorCanvas, History, make_icon
from .output import MODES, OutputWindow
from .render import CueState, Frame, Renderer

try:
    from ..osc import OscBridge, OscListener
    OSC_AVAILABLE = True
except ImportError:  # pragma: no cover
    OSC_AVAILABLE = False

BAUD = 19200           # Limitimer and Perfect Cue data links are both fixed at 19200 8N1
TEAL = "#2EC4B6"

_STYLE = """
QMainWindow, QWidget { background: #161616; color: #C8C8C8; }
QGroupBox { border: 1px solid #2C2C2C; border-radius: 6px; margin-top: 12px; padding: 10px 8px 8px 8px;
            font-size: 10px; font-weight: 600; letter-spacing: 1px; color: #7A7A7A; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QPushButton { background: #262626; border: 1px solid #3A3A3A; border-radius: 4px; padding: 5px 12px; }
QPushButton:hover { background: #303030; border-color: #555; }
QPushButton:pressed { background: #1C1C1C; }
QPushButton:checked { background: #1F3D3A; border-color: #2EC4B6; color: #E6FFFB; }
QPushButton:disabled { color: #4A4A4A; border-color: #2A2A2A; }
QPushButton::menu-indicator { image: none; width: 0; }
QComboBox, QLineEdit, QDoubleSpinBox { background: #202020; border: 1px solid #343434; border-radius: 4px;
                                       padding: 4px 6px; }
QComboBox QAbstractItemView { background: #202020; selection-background-color: #1F3D3A; }
QListWidget { background: #1B1B1B; border: 1px solid #2C2C2C; border-radius: 4px; }
QListWidget::item:selected { background: #1F3D3A; color: #E6FFFB; }
QScrollArea { border: none; }
QToolButton { background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 3px; }
QToolButton:hover { background: #262626; border-color: #3A3A3A; }
QToolButton:checked { background: #1F3D3A; border-color: #2EC4B6; }
QToolButton:disabled { opacity: 0.4; }
QLabel#dim { color: #6A6A6A; }
QStatusBar { background: #101010; color: #707070; }
QMenuBar { background: #101010; } QMenu { background: #1E1E1E; }
"""

_LINK_TEXT = {
    LinkState.CLOSED: ("● off", "#555"),
    LinkState.OPENING: ("● opening", "#FFB000"),
    LinkState.RECONNECTING: ("● port lost", "#FF3B3B"),
    LinkState.WAITING: ("● waiting", "#FFB000"),
    LinkState.LIVE: ("● live", "#00E07A"),
    LinkState.STALE: ("● no signal", "#FF3B3B"),
    LinkState.OPEN: ("● open", "#00E07A"),
}


# ── thread bridge ───────────────────────────────────────────────────────────────

SETTINGS_APP = "Confidence"
OLD_SETTINGS_APPS = ("TruTime", "DSANClock")   # earlier names: TruTime, DSAN Viewer / DSAN Clock


def open_settings(factory=QSettings):
    """Confidence's settings. On first launch, copy over what an earlier version (TruTime,
    DSAN Viewer) had saved."""
    s = factory("TRUCUE", SETTINGS_APP)
    if not s.allKeys():
        for name in OLD_SETTINGS_APPS:
            old = factory("TRUCUE", name)
            keys = old.allKeys()
            if keys:
                for k in keys:
                    s.setValue(k, old.value(k))
                s.setValue("settings/migrated_from", name)
                s.sync()
                break
    return s


class Signals(QObject):
    """Device callbacks arrive on reader threads; Qt queues these onto the GUI thread."""
    lt_state = pyqtSignal(object)
    lt_link = pyqtSignal(object)
    pc_button = pyqtSignal(object)
    pc_link = pyqtSignal(object)
    error = pyqtSignal(str)
    detect_status = pyqtSignal(str)
    detect_done = pyqtSignal(object)
    osc_press = pyqtSignal(object)
    mm_message = pyqtSignal(str, object, bool)
    tc_message = pyqtSignal(str, object, bool)
    osc_message = pyqtSignal(str, object, str)
    update_result = pyqtSignal(object, object, bool)     # UpdateInfo | None, error text | None, manual


# ── small widgets ───────────────────────────────────────────────────────────────

class ColorButton(QPushButton):
    changed = pyqtSignal(str)

    def __init__(self, value: str, alpha: bool = True):
        super().__init__()
        self._alpha = alpha
        self.setFixedHeight(26)
        self.set_value(value)
        self.clicked.connect(self._pick)

    def set_value(self, value: str):
        self._value = value
        r, g, b, a = parse_color(value)
        text_col = "#000" if (r * 299 + g * 587 + b * 114) / 1000 > 140 and a > 120 else "#DDD"
        self.setText(value.upper() if a else "transparent")
        self.setStyleSheet(f"QPushButton {{ background: rgba({r},{g},{b},{a}); color: {text_col}; "
                           f"border: 1px solid #444; }}")

    def value(self) -> str:
        return self._value

    def _pick(self):
        r, g, b, a = parse_color(self._value)
        opts = QColorDialog.ColorDialogOption.ShowAlphaChannel if self._alpha else QColorDialog.ColorDialogOption(0)
        c = QColorDialog.getColor(QColor(r, g, b, a), self, "Choose colour", opts)
        if c.isValid():
            v = format_color(c.red(), c.green(), c.blue(), c.alpha() if self._alpha else 255)
            self.set_value(v)
            self.changed.emit(v)


def _pct_spin(value: float) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(0, 100)
    s.setDecimals(1)
    s.setSingleStep(0.5)
    s.setSuffix(" %")
    s.setValue(value * 100)
    return s


# ── properties editor ───────────────────────────────────────────────────────────

_FIELD_LABELS = {
    "name": "Name", "visible": "Visible", "x": "Left", "y": "Top", "w": "Width", "h": "Height",
    "bg": "Background", "radius": "Corner radius", "color": "Colour", "font": "Font", "bold": "Bold",
    "align": "Align", "source": "Source", "mm_show": "Media timer shows", "overtime": "Count past zero (+00:20)",
    "follow_format": "Follow DIP 2/3 format", "phase_colors": "Colour by phase",
    "follow_blink": "Flash with Limitimer blink",
    "orientation": "Orientation", "show_idle": "Green while stopped",
    "cue_style": "Style", "hold": "Hold NEXT/PREV (s)",
    "bar_direction": "Direction", "bar_fill": "Shows", "frame_px": "Frame width", "clock_24h": "24-hour",
    "clock_seconds": "Show seconds", "text": "Text", "flash": "Flash",
}
_CHOICES = {
    "align": [("left", "Left"), ("center", "Centre"), ("right", "Right")],
    "source": [("selected", "Selected on Limitimer")] + [(n, f"Program {n}") for n in SOURCES[1:5]]
              + [("millumin", "Millumin"), ("trucue", "TruCue")],
    "mm_show": [("remaining", "Remaining (countdown)"), ("elapsed", "Elapsed"), ("duration", "Duration (TRT)"),
                ("bookmark", "Next bookmark (TruCue)")],
    "orientation": [("horizontal", "Horizontal"), ("vertical", "Vertical")],
    "cue_style": [("arrows", "Arrows ◁ ● ▷"), ("light", "Full colour light"), ("frame", "Outline frame"),
                  ("text", "Text")],
    "bar_direction": [("ltr", "Left → right"), ("rtl", "Right → left"),
                      ("btt", "Bottom → top"), ("ttb", "Top → bottom")],
    "bar_fill": [("auto", "Auto (remaining on countdown, elapsed on count-up)"),
                 ("remaining", "Time remaining (empties)"), ("elapsed", "Time elapsed (fills)")],
}
_BOOLS = {"visible", "bold", "overtime", "follow_format", "phase_colors", "follow_blink", "show_idle", "clock_24h",
          "clock_seconds", "flash"}
_COLOR_FIELDS = {"bg", "color"}
_GEOM = {"x", "y", "w", "h"}


class PropertyEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, families: List[str]):
        super().__init__()
        self._families = families
        self._form = QFormLayout(self)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._form.setContentsMargins(4, 4, 4, 4)
        self._geom: Dict[str, QDoubleSpinBox] = {}
        self.element: Optional[Element] = None

    def clear(self):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._geom = {}

    def show_multi(self, els: List[Element], px: Optional[tuple] = None):
        self.clear()
        self.element = None
        title = QLabel(f"{len(els)} elements selected")
        title.setStyleSheet(f"color: {TEAL}; font-weight: 600;")
        self._form.addRow(title)
        x, y, w, h = al.bounds(els)
        text = f"x {x * 100:.1f}%  y {y * 100:.1f}%   {w * 100:.1f} × {h * 100:.1f}%"
        if px:
            text += f"\n{round(x * px[0])}, {round(y * px[1])}   {round(w * px[0])} × {round(h * px[1])} px"
        bounds = QLabel(text)
        self._form.addRow("Group", bounds)
        hint = QLabel("Align, distribute and match sizes with the toolbar above the preview. "
                      "Drag to move them together; ⌘-click or Shift-click to add or remove.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        self._form.addRow(hint)

    def show_element(self, e: Optional[Element]):
        self.clear()
        self.element = e
        if e is None:
            return
        title = QLabel(ELEMENT_TYPES[e.type])
        title.setStyleSheet(f"color: {TEAL}; font-weight: 600;")
        self._form.addRow(title)
        for f in COMMON_FIELDS + TYPE_FIELDS[e.type]:
            self._form.addRow(_FIELD_LABELS.get(f, f), self._widget_for(e, f))
        if e.type == "label":
            hint = QLabel("Limitimer: {program}  {n}  {mode}\nMillumin: {clip}  {layer}  {column}  {trt}  "
                          "{remaining}  {elapsed}  {state}  {playmode}\nTruCue: {tc_clip}  {tc_index}  {tc_next}  "
                          "{tc_prev}  {tc_remaining}  {tc_elapsed}  {tc_trt}  {tc_state}  {tc_playmode}  "
                          "{tc_bookmark}  {tc_bookmark_remaining}  {tc_volume}")
            hint.setWordWrap(True)
            hint.setObjectName("dim")
            self._form.addRow("", hint)
        if e.type == "message":
            hint = QLabel("Text comes from the Message box on the left.")
            hint.setObjectName("dim")
            self._form.addRow("", hint)

    def refresh_geometry(self):
        if not self.element:
            return
        for f, spin in self._geom.items():
            spin.blockSignals(True)
            spin.setValue(getattr(self.element, f) * 100)
            spin.blockSignals(False)

    def _set(self, e: Element, f: str, v):
        setattr(e, f, v)
        self.changed.emit()

    def _widget_for(self, e: Element, f: str) -> QWidget:
        v = getattr(e, f)
        if f in _BOOLS:
            w = QCheckBox()
            w.setChecked(bool(v))
            w.toggled.connect(lambda on, f=f: self._set(e, f, on))
            return w
        if f in _GEOM:
            w = _pct_spin(v)
            w.valueChanged.connect(lambda val, f=f: self._set(e, f, round(val / 100, 4)))
            self._geom[f] = w
            return w
        if f in _COLOR_FIELDS:
            w = ColorButton(v)
            w.changed.connect(lambda val, f=f: self._set(e, f, val))
            return w
        if f in _CHOICES:
            w = Combo()
            for key, label in _CHOICES[f]:
                w.addItem(label, key)
            w.setCurrentIndex(max(0, w.findData(v)))
            w.currentIndexChanged.connect(lambda _i, w=w, f=f: self._set(e, f, w.currentData()))
            return w
        if f == "font":
            w = Combo()
            w.addItem("Default (clean sans)", "")
            for fam in self._families:
                w.addItem(fam, fam)
            i = w.findData(v)
            if i < 0:
                w.addItem(v, v)
                i = w.count() - 1
            w.setCurrentIndex(i)
            w.currentIndexChanged.connect(lambda _i, w=w: self._set(e, "font", w.currentData()))
            return w
        if f == "frame_px":
            w = QSpinBox()
            w.setRange(1, 400)
            w.setSuffix(" px")
            w.setValue(int(v))
            w.setToolTip("Outline width in pixels of the output (screen, window or DeckLink SDI)")
            w.valueChanged.connect(lambda val: self._set(e, "frame_px", int(val)))
            return w
        if f in ("radius", "hold"):
            w = QDoubleSpinBox()
            if f == "radius":
                w.setRange(0, 0.5)
                w.setSingleStep(0.05)
            else:
                w.setRange(0.2, 10)
                w.setSingleStep(0.1)
                w.setSuffix(" s")
            w.setDecimals(2)
            w.setValue(float(v))
            w.valueChanged.connect(lambda val, f=f: self._set(e, f, round(val, 3)))
            return w
        w = QLineEdit(str(v))
        w.textChanged.connect(lambda val, f=f: self._set(e, f, val))
        return w


class LayoutEditor(QWidget):
    """Layout-wide settings, shown when no element is selected."""
    changed = pyqtSignal()
    fonts_changed = pyqtSignal()

    _COLOR_KEYS = [("running", "Running"), ("idle", "Stopped"), ("warning", "Sum-up warning"),
                   ("expired", "Expired"), ("next", "Cue NEXT"), ("prev", "Cue PREV"),
                   ("blank", "Cue BLANK"), ("dim", "Off / unlit")]

    def __init__(self):
        super().__init__()
        self._form = QFormLayout(self)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._form.setContentsMargins(4, 4, 4, 4)

    def show_layout(self, lay: Layout):
        while self._form.rowCount():
            self._form.removeRow(0)
        title = QLabel("Layout")
        title.setStyleSheet(f"color: {TEAL}; font-weight: 600;")
        self._form.addRow(title)
        name = QLineEdit(lay.name)
        name.textChanged.connect(lambda v: (setattr(lay, "name", v), self.changed.emit()))
        self._form.addRow("Name", name)
        bg = ColorButton(lay.background)
        bg.setToolTip("Set alpha to 0 for a transparent overlay")
        bg.changed.connect(lambda v: (setattr(lay, "background", v), self.changed.emit()))
        self._form.addRow("Background", bg)
        ns = Combo()
        for key, label in (("dim", "Dim digits + NO SIGNAL"), ("text", "Show NO SIGNAL"),
                           ("hide", "Hide timers"), ("ignore", "Keep showing last time")):
            ns.addItem(label, key)
        ns.setCurrentIndex(max(0, ns.findData(lay.no_signal)))
        ns.setToolTip("What timers do when Limitimer data stops")
        ns.currentIndexChanged.connect(lambda _i: (setattr(lay, "no_signal", ns.currentData()),
                                                   self.changed.emit()))
        self._form.addRow("If data stops", ns)
        for key, label in self._COLOR_KEYS:
            b = ColorButton(lay.color(key), alpha=False)
            b.changed.connect(lambda v, k=key: (lay.colors.__setitem__(k, v), self.changed.emit()))
            self._form.addRow(label, b)
        fonts = QLabel("\n".join(os.path.basename(f) for f in lay.font_files) or "none")
        fonts.setObjectName("dim")
        self._form.addRow("Font files", fonts)
        add = QPushButton("Add font file…")
        add.clicked.connect(lambda: self._add_font(lay))
        self._form.addRow("", add)
        hint = QLabel("Click an element in the preview to edit it.")
        hint.setObjectName("dim")
        self._form.addRow("", hint)

    def _add_font(self, lay: Layout):
        path = files.open_path(self, "Add font", "Fonts (*.ttf *.otf)", "fonts")
        if path and path not in lay.font_files:
            lay.font_files.append(path)
            self.fonts_changed.emit()
            self.changed.emit()
            self.show_layout(lay)


# ── main window ─────────────────────────────────────────────────────────────────

class ControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = open_settings(QSettings)
        self.renderer = Renderer()
        self.sig = Signals()
        self.sig.lt_state.connect(self._on_lt_state)
        self.sig.lt_link.connect(self._on_lt_link)
        self.sig.pc_button.connect(self._on_pc_button)
        self.sig.pc_link.connect(self._on_pc_link)
        self.sig.error.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.sig.detect_status.connect(self._detect_note)
        self.sig.detect_done.connect(self._detect_finished)

        # live state
        self.lt_state: Optional[LimitimerState] = None
        self.lt_link = LinkState.CLOSED
        self.pc_link_state = LinkState.CLOSED
        self.cue = CueState()
        self.message = ""
        self.limitimer: Optional[Limitimer] = None
        self.perfectcue: Optional[PerfectCue] = None
        self.demo = False
        self.osc = None
        self.osc_in = None
        self.mm_rx = None
        self.tc_rx = None
        self.lt_bus: Optional[LimitimerBus] = None   # software Limitimer → DSAN lights
        self.remote = None
        self.bonjour = None              # announces <name>.local for the remote page
        self._detecting = False
        self._port_infos: Dict[str, portsmod.PortInfo] = {}
        self.refs: Dict[str, Optional[portsmod.PortRef]] = {"lt": None, "pc": None}
        self._wanted_screen: Optional[str] = None
        self._watched_screens: set = set()
        self.want: Dict[str, bool] = {"lt": False, "pc": False}   # operator pressed Connect
        self.pc_raw_bytes = 0
        self.pc_last_press: Optional[tuple] = None

        self.layout_obj: Layout = PRESETS["Confidence monitor"]()
        self.history = History()
        self.debug_tap = DebugTap()
        self.debug_window: Optional[DebugWindow] = None
        self._committed: Optional[dict] = None
        self.layout_path: Optional[str] = None

        self.output = OutputWindow(self.frame, lambda: self.layout_obj, self.renderer)
        self.output.closed.connect(lambda: self.show_output_btn.setChecked(False))

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._autosave_layout)

        self._build()
        self._load_settings()
        # Re-scan ports (hot-plug) every 2 s and refresh the activity lines every second.
        self._ticks = 0
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(1000)
        QGuiApplication.instance().screenAdded.connect(lambda _s: self._screens_changed())
        QGuiApplication.instance().screenRemoved.connect(lambda _s: QTimer.singleShot(0, self._screens_changed))

    def _tick(self):
        self._ticks += 1
        self._update_millumin()
        self._update_trucue()
        if self._ticks % 5 == 0:
            self._update_remote()
        self.decklink_panel.update_status()
        if self._ticks % 2 == 0 and not self._detecting:
            self._refresh_ports()
        else:
            self._update_activity()

    def about_box(self) -> QMessageBox:
        box = QMessageBox(self)
        box.setWindowTitle("About Confidence")
        icon = QGuiApplication.instance().windowIcon()
        if not icon.isNull():
            box.setIconPixmap(icon.pixmap(64, 64))
        b = ver.build()
        build_line = (f"Build {b}" + (f" · {ver.commit()}" if ver.commit() else "")
                      + (f" · built {ver.built_at()}" if ver.built_at() else "")) if b != "dev" \
            else "Running from source (development)"
        box.setText(f"<b>Confidence {__version__}</b><br>{build_line}")
        rows = "".join(f"<tr><td style='color:#7A7A7A;padding-right:10px'>{k}</td><td>{v}</td></tr>"
                       for k, v in ver.details().items() if k not in ("Confidence", "Build", "Commit", "Built"))
        box.setInformativeText(f"<table>{rows}</table><br>Show timing for DSAN Limitimer, Perfect Cue and Millumin · TRUCUE")
        box.copy_button = box.addButton("Copy details", QMessageBox.ButtonRole.ActionRole)
        box.notes_button = box.addButton("What's new", QMessageBox.ButtonRole.HelpRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        return box

    def show_about(self):
        box = self.about_box()
        box.exec()
        if box.clickedButton() is box.copy_button:
            QGuiApplication.clipboard().setText(ver.details_text())
        elif box.clickedButton() is box.notes_button:
            self.show_whats_new()

    def whats_new_dialog(self):
        from PyQt6.QtWidgets import QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle(f"What's new — Confidence {__version__}")
        dlg.resize(640, 560)
        v = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        path = ver._changelog_path()
        view.setMarkdown(path.read_text(encoding="utf-8") if path else
                         f"# Confidence {__version__}\n\nThe changelog isn't included in this build.")
        v.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)
        dlg.setStyleSheet(_STYLE)
        return dlg

    def show_whats_new(self):
        self.whats_new_dialog().exec()

    def show_debug(self):
        if self.debug_window is None:
            self.debug_window = DebugWindow(self.debug_tap, lambda: (self.limitimer, self.perfectcue),
                                            self.settings)
            self.debug_window.setStyleSheet(_STYLE)
        self.debug_window.show()
        self.debug_window.raise_()
        self.debug_window.activateWindow()

    # ── data for painting ────────────────────────────────────────────────────────

    def frame(self) -> Frame:
        now = time.monotonic()
        mm = self.mm_rx.tracker.view(self.mm_layer_name(), now) if self.mm_rx else None
        tc = self.tc_rx.tracker.view(now) if self.tc_rx else None
        return Frame(state=self.lt_state, link=self.lt_link, cue=self.cue, message=self.message,
                     millumin=mm, mm_yellow=self.mm_yellow.value(), mm_red=self.mm_red.value(),
                     trucue=tc, tc_yellow=self.tc_yellow.value(), tc_red=self.tc_red.value(),
                     now=now, wall=_dt.datetime.now())

    def _aspect(self) -> float:
        g = self._selected_screen().geometry()
        if self.output.isVisible() and self.output.mode == "window":
            g = self.output.geometry()
        return g.width() / max(1, g.height())

    # ── build UI ─────────────────────────────────────────────────────────────────

    def _build(self):
        self.setWindowTitle(f"Confidence {__version__}")
        self.version_label = QLabel(f"v{ver.version_string()}")
        self.version_label.setObjectName("dim")
        self.version_label.setToolTip("About Confidence… (Help menu)")
        self.statusBar().addPermanentWidget(self.version_label)
        self.setStyleSheet(_STYLE)
        self.resize(1280, 820)

        file_menu = self.menuBar().addMenu("File")
        for text, slot, key in (("Open layout…", self._open_layout, "Ctrl+O"),
                                ("Save layout", self._save_layout, "Ctrl+S"),
                                ("Save layout as…", self._save_layout_as, "Ctrl+Shift+S")):
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(slot)
            file_menu.addAction(a)

        help_menu = self.menuBar().addMenu("Help")
        about = QAction("About Confidence", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)        # macOS: goes in the app menu
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)
        whats_new = QAction("What's new", self)
        whats_new.triggered.connect(self.show_whats_new)
        help_menu.addAction(whats_new)
        help_menu.addSeparator()
        self.update_action = QAction("Check for Updates…", self)
        self.update_action.setMenuRole(QAction.MenuRole.ApplicationSpecificRole)   # macOS: app menu
        self.update_action.triggered.connect(lambda: self.check_for_updates(manual=True))
        help_menu.addAction(self.update_action)
        self.auto_update_action = QAction("Check for Updates Automatically", self)
        self.auto_update_action.setCheckable(True)
        self.auto_update_action.setChecked(True)
        self.auto_update_action.toggled.connect(self._auto_update_toggled)
        help_menu.addAction(self.auto_update_action)
        self.sig.update_result.connect(self._on_update_result)
        self.update_info = None
        self._update_running = False
        from . import sparkle as sparkle_mod
        self.sparkle = sparkle_mod.load()          # the signed Mac app: Sparkle installs updates in place

        devices_menu = self.menuBar().addMenu("Devices")
        self.detect_action = QAction("Detect adapters", self)
        self.detect_action.setToolTip("Listen on every USB adapter and work out which is which")
        self.detect_action.triggered.connect(self._start_detect)
        devices_menu.addAction(self.detect_action)
        swap = QAction("Swap Limitimer and Perfect Cue adapters", self)
        swap.triggered.connect(self._swap_ports)
        devices_menu.addAction(swap)
        devices_menu.addSeparator()
        self.debug_action = QAction("Debug console", self)
        self.debug_action.setShortcut("Ctrl+D")             # ⌘D on macOS
        self.debug_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)  # also from the console itself
        self.debug_action.triggered.connect(self.show_debug)
        devices_menu.addAction(self.debug_action)

        edit_menu = self.menuBar().addMenu("Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self._undo)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self._redo)
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        for text, slot, key in (("Duplicate", self._duplicate_element, "Ctrl+Shift+D"),
                                ("Select all", lambda: self.preview.set_selection(
                                    [e.id for e in self.layout_obj.elements if e.visible], emit=True), "Ctrl+A")):
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(slot)
            edit_menu.addAction(a)
        align_menu = edit_menu.addMenu("Align")
        for how, label in (("left", "Left edges"), ("hcenter", "Horizontal centres"), ("right", "Right edges"),
                           ("top", "Top edges"), ("vcenter", "Vertical centres"), ("bottom", "Bottom edges")):
            align_menu.addAction(label, lambda h=how: self._align(h))

        root = QSplitter()
        self.setCentralWidget(root)

        # left: operations
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 4, 8, 8)
        lv.addWidget(self._build_inputs())
        self.lt_panel = LimitimerPanel(lambda m: self.statusBar().showMessage(m, 6000), self._visible_ports)
        self.lt_panel.mode_changed.connect(self._lt_mode_changed)
        lv.addWidget(self.lt_panel)
        lv.addWidget(self._build_dips())
        lv.addWidget(self._build_output())
        self.decklink_panel = DeckLinkPanel(self.renderer, lambda: self.layout_obj, self.frame,
                                            lambda m: self.statusBar().showMessage(m, 6000))
        lv.addWidget(self.decklink_panel)
        lv.addWidget(self._build_message())
        lv.addWidget(self._build_osc())
        lv.addWidget(self._build_millumin())
        lv.addWidget(self._build_trucue())
        lv.addWidget(self._build_remote())
        lv.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # wide enough for the panels plus the vertical scroll bar, so nothing is cut off at the right
        need = left.minimumSizeHint().width() + scroll.verticalScrollBar().sizeHint().width() + 2 * scroll.frameWidth()
        scroll.setMinimumWidth(max(380, need))
        scroll.setMaximumWidth(max(460, need + 40))
        root.addWidget(scroll)
        root.setCollapsible(0, False)

        # right: preview + editor
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 8, 8, 8)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Preset:"))
        self.preset_combo = Combo()
        self.preset_combo.addItem("Load a preset…", None)
        for name in PRESETS:
            self.preset_combo.addItem(name, name)
        self.preset_combo.activated.connect(self._load_preset)
        bar.addWidget(self.preset_combo)
        for text, slot in (("Open…", self._open_layout), ("Save", self._save_layout),
                           ("Save as…", self._save_layout_as)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch()
        self.layout_label = ElidingLabel()
        self.layout_label.setObjectName("dim")
        self.layout_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self.layout_label, 1)
        rv.addLayout(bar)
        rv.addWidget(self._build_align_bar())

        split = QSplitter(Qt.Orientation.Vertical)
        self.preview = EditorCanvas(lambda: self.layout_obj, self.frame, self._aspect, self.renderer,
                                    self._output_pixels)
        self.preview.selection_changed.connect(lambda _ids: self._show_selection())
        self.preview.edited.connect(self._geometry_edited)
        self.preview.delete_requested.connect(self._delete_element)
        split.addWidget(self.preview)

        bottom = QSplitter(Qt.Orientation.Horizontal)       # element list | properties, draggable
        bottom.setChildrenCollapsible(False)
        listcol = QVBoxLayout()
        self.element_list = QListWidget()
        self.element_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.element_list.itemSelectionChanged.connect(self._list_selection_changed)
        self.element_list.itemChanged.connect(self._list_item_changed)
        listcol.addWidget(self.element_list)
        btns = QHBoxLayout()
        btns.setSpacing(4)
        add = QPushButton("Add ▾")
        add.setToolTip("Add an element")
        menu = QMenu(add)
        for t, label in ELEMENT_TYPES.items():
            menu.addAction(label, lambda t=t: self._add_element(t))
        add.setMenu(menu)
        btns.addWidget(add)
        for text, slot, tip in (("Dup", self._duplicate_element, "Duplicate"),
                                ("Del", self._delete_element, "Delete"),
                                ("▲", lambda: self._move_element(1), "Bring forward"),
                                ("▼", lambda: self._move_element(-1), "Send backward")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        for i in range(btns.count()):                      # compact, and never narrower than the label
            b = btns.itemAt(i).widget()
            b.setStyleSheet("padding: 5px 8px;")
            b.setMinimumWidth(b.sizeHint().width())
        listcol.addLayout(btns)
        lw = QWidget()
        lw.setLayout(listcol)
        listcol.setContentsMargins(0, 0, 0, 0)
        lw.setMinimumWidth(lw.minimumSizeHint().width())
        bottom.addWidget(lw)

        families = sorted(set(QFontDatabase.families()))
        self.props = PropertyEditor(families)
        self.props.changed.connect(self._props_changed)
        self.layout_props = LayoutEditor()
        self.layout_props.changed.connect(self._layout_changed)
        self.layout_props.fonts_changed.connect(self._load_font_files)
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.addWidget(self.props)
        hv.addWidget(self.layout_props)
        hv.addStretch()
        ps = QScrollArea()
        ps.setWidget(holder)
        ps.setWidgetResizable(True)
        ps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._props_scroll = ps
        self._fit_props_width()
        bottom.addWidget(ps)
        bottom.setStretchFactor(0, 0)
        bottom.setStretchFactor(1, 1)
        bottom.setSizes([lw.minimumWidth() + 20, 600])
        split.addWidget(bottom)
        split.setSizes([480, 300])
        rv.addWidget(split)
        root.addWidget(right)
        root.setSizes([400, 880])

    def _row(self, *widgets, stretch_last=False) -> QHBoxLayout:
        h = QHBoxLayout()
        for w in widgets:
            h.addWidget(w)
        if not stretch_last:
            h.addStretch()
        return h

    def _build_inputs(self) -> QGroupBox:
        g = QGroupBox("INPUTS — USB-RS485")
        v = QVBoxLayout(g)
        v.setSpacing(6)

        def heading(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("color: #C8C8C8; font-weight: 600;")
            return lbl

        for kind, title, tip in (
                ("lt", "Limitimer", "The USB-RS485 adapter wired to the Limitimer's data port."),
                ("pc", "Perfect Cue", "The USB-RS485 adapter wired to the Perfect Cue receiver's data port.")):
            if kind == "pc":
                v.addSpacing(4)
            v.addWidget(heading(title))
            combo = Combo()
            combo.setToolTip(tip + "\nPick it here, then press Connect.")
            combo.activated.connect(lambda i, k=kind: self._port_picked(k, i))
            v.addWidget(combo)
            row = QHBoxLayout()
            lbl = QLabel(f"{BAUD} 8N1")
            lbl.setObjectName("dim")
            lbl.setToolTip("The Limitimer and Perfect Cue data links are fixed at 19200 baud, 8N1.")
            row.addWidget(lbl)
            row.addStretch()
            btn = QPushButton("Connect")
            btn.setCheckable(True)
            btn.setMinimumWidth(110)
            btn.clicked.connect(lambda on, k=kind: self._connect_clicked(k, on))
            row.addWidget(btn)
            v.addLayout(row)
            status = QLabel()
            status.setWordWrap(True)
            v.addWidget(status)
            if kind == "lt":
                self.lt_combo, self.lt_connect, self.lt_status = combo, btn, status
            else:
                self.pc_combo, self.pc_connect, self.pc_status = combo, btn, status

        # tools
        v.addSpacing(4)
        self.demo_btn = QPushButton("Demo")
        self.demo_btn.setCheckable(True)
        self.demo_btn.setToolTip("Simulated Limitimer and Perfect Cue, through the real decoder")
        self.demo_btn.clicked.connect(self._toggle_demo)
        self.show_all_ports = QCheckBox("Show all serial ports")
        self.show_all_ports.setToolTip("Also list non-USB ports (Bluetooth, debug console…)")
        self.show_all_ports.toggled.connect(lambda _on: self._refresh_ports(force=True))
        tools = QHBoxLayout()
        tools.addWidget(self.demo_btn)
        tools.addSpacing(8)
        tools.addWidget(self.show_all_ports)
        tools.addStretch()
        v.addLayout(tools)

        self.detect_label = QLabel()
        self.detect_label.setWordWrap(True)
        self.detect_label.setObjectName("dim")
        self.detect_label.hide()
        v.addWidget(self.detect_label)

        test = QHBoxLayout()
        lbl = QLabel("Test cue:")
        lbl.setObjectName("dim")
        test.addWidget(lbl)
        for text, btn in (("◁", pc.Button.PREV), ("Blank", None), ("▷", pc.Button.NEXT)):
            b = QPushButton(text)
            b.setFixedWidth(56 if btn is None else 36)
            if btn is None:
                b.clicked.connect(lambda: self._on_pc_button(
                    pc.Button.BLANK_OFF if self.cue.blank else pc.Button.BLANK_ON, test=True))
            else:
                b.clicked.connect(lambda _=False, btn=btn: self._on_pc_button(btn, test=True))
            test.addWidget(b)
        test.addStretch()
        v.addLayout(test)
        return g

    # Limitimer DIP switches as reported in its status data. 1 (master/slave) and
    # 10 (RF) aren't in the status message, so they aren't shown.
    _DIPS = (
        ("2", "Program 4 format", lambda st: ("MM:SS", "#00E07A") if st.session_minutes else ("HH:MM", "#A98BFF")),
        ("3", "Programs 1–3 format", lambda st: ("MM:SS", "#00E07A") if st.program_minutes else ("HH:MM", "#A98BFF")),
        ("4", "Direction", lambda st: ("Count down", "#FF8A00") if st.count_down else ("Count up", "#4AB0FF")),
        ("5", "At zero", lambda st: ("Keep counting", "#FFB000") if st.continue_after_zero else ("Stop at 0", "#C8C8C8")),
        ("6", "Beep volume", lambda st: ("Loud", "#FF3B3B") if st.beep_loud else ("Soft", "#C8C8C8")),
        ("7+8", "Sound", lambda st: (st.beep_type.name.title(), "#FFD000" if st.beep_type else "#7A7A7A")),
        ("9", "Changes", lambda st: ("Allowed", "#00E07A") if st.permit_changes else ("Locked", "#FF3B3B")),
    )

    def _build_dips(self) -> QGroupBox:
        g = QGroupBox("LIMITIMER DIP SWITCHES")
        v = QVBoxLayout(g)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        self._dip_values: List[QLabel] = []
        for row, (num, name, _fn) in enumerate(self._DIPS):
            n = QLabel(num)
            n.setObjectName("dim")
            n.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            n.setFixedWidth(30)
            val = QLabel("—")
            val.setStyleSheet("font-weight: 600;")
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(n, row, 0)
            grid.addWidget(QLabel(name), row, 1)
            grid.addWidget(val, row, 2)
            self._dip_values.append(val)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        self.dip_note = QLabel()
        self.dip_note.setObjectName("dim")
        self.dip_note.setWordWrap(True)
        v.addWidget(self.dip_note)
        self._dip_key = None
        self._update_dips()
        return g

    def _update_dips(self):
        """Refresh the DIP panel (cheap; only repaints when something changed)."""
        if not hasattr(self, "_dip_values"):
            return
        st = self.lt_state
        live = self.lt_link == LinkState.LIVE
        values = tuple(fn(st) for _n, _name, fn in self._DIPS) if st else None
        key = (values, live, self.demo, self.emulating)
        if key == self._dip_key:
            return
        self._dip_key = key
        for i, lbl in enumerate(self._dip_values):
            if values is None:
                lbl.setText("—")
                lbl.setStyleSheet("font-weight: 600; color: #555;")
            else:
                text, color = values[i]
                lbl.setText(text)
                lbl.setStyleSheet(f"font-weight: 600; color: {color if live else '#6A6A6A'};")
        if values is None:
            self.dip_note.setText("Connect the Limitimer to read its switches.")
        elif self.demo:
            self.dip_note.setText("Demo values. Switches 1 (mode) and 10 (RF) aren't reported by the Limitimer.")
        elif self.emulating:
            self.dip_note.setText("The software Limitimer's switches — change them in LIMITIMER CONTROL → "
                                  "Programs & switches.")
        elif not live:
            self.dip_note.setText("Last values seen — Limitimer data has stopped.")
        else:
            self.dip_note.setText("Read live from the Limitimer. Switches 1 (mode) and 10 (RF) "
                                  "aren't in its status data.")

    def _build_output(self) -> QGroupBox:
        g = QGroupBox("OUTPUT")
        form = QFormLayout(g)
        self.mode_combo = Combo()
        for k, label in MODES.items():
            self.mode_combo.addItem(label, k)
        self.mode_combo.currentIndexChanged.connect(self._output_settings_changed)
        self.screen_combo = Combo()
        self.screen_combo.currentIndexChanged.connect(self._output_settings_changed)
        self._refresh_screens()
        form.addRow("Mode", self.mode_combo)
        form.addRow("Display", self.screen_combo)
        self.show_output_btn = QPushButton("Show output")
        self.show_output_btn.setCheckable(True)
        self.show_output_btn.toggled.connect(self._toggle_output)
        form.addRow("", self.show_output_btn)
        self.output_note = QLabel()
        self.output_note.setWordWrap(True)
        self.output_note.setObjectName("dim")
        form.addRow(self.output_note)
        return g

    def _build_message(self) -> QGroupBox:
        g = QGroupBox("MESSAGE TO PRESENTER")
        v = QVBoxLayout(g)
        self.msg_edit = QLineEdit()
        self.msg_edit.setPlaceholderText("Type a message and press Enter")
        self.msg_edit.returnPressed.connect(self._send_message)
        v.addWidget(self.msg_edit)
        send = QPushButton("Send")
        send.clicked.connect(self._send_message)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_message)
        row = [send, clear]
        for text in ("WRAP UP", "1 MINUTE", "PLEASE STOP"):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, t=text: self._send_message(t))
            row.append(b)
        h1 = self._row(send, clear)
        v.addLayout(h1)
        v.addLayout(self._row(*row[2:]))
        self.msg_status = QLabel("Nothing on screen")
        self.msg_status.setObjectName("dim")
        v.addWidget(self.msg_status)
        return g

    def _build_osc(self) -> QGroupBox:
        g = QGroupBox("OSC")
        form = QFormLayout(g)
        self.osc_enable = QCheckBox("Send OSC")
        self.osc_enable.setEnabled(OSC_AVAILABLE)
        self.osc_enable.toggled.connect(self._rebuild_osc)
        self.osc_target = QLineEdit("127.0.0.1:5000")
        self.osc_prefix = QLineEdit("/dsan")
        self.osc_prefix.setToolTip("Address prefix for OSC out and OSC in, e.g. /dsan/perfectcue/next")
        self.osc_status = QLabel("" if OSC_AVAILABLE else "python-osc not installed")
        self.osc_status.setObjectName("dim")
        form.addRow(self.osc_enable)
        form.addRow("Host:port", self.osc_target)
        form.addRow("Prefix", self.osc_prefix)
        form.addRow(self.osc_status)

        self.osc_in_enable = QCheckBox("Receive OSC (Perfect Cue and Limitimer control)")
        self.osc_in_enable.setEnabled(OSC_AVAILABLE)
        self.osc_in_enable.setToolTip(
            "Listen for commands, prefix + …\n"
            "  /perfectcue/next · /prev · /blank 1|0      show a cue (as if pressed)\n"
            "  /perfectcue/press next|prev|blank_on|blank_off\n"
            "  /perfectcue/led next|prev|blank_on|blank_off|error    light the Perfect Cue LEDs\n"
            "  /perfectcue/command forward|reverse|blank_on|blank_off   receiver command\n"
            "  /limitimer/start · /p1 … /p3 · /session · /time+ · /time- · /sumup+ · /sumup-\n"
            "  /limitimer/repeat · /clear · /beep · /blink · /seconds · /program 1-4   Limitimer buttons")
        self.osc_in_enable.toggled.connect(self._rebuild_osc_in)
        self.osc_in_port = QSpinBox()
        self.osc_in_port.setRange(1024, 65535)
        self.osc_in_port.setValue(9000)
        self.osc_in_status = QLabel("")
        self.osc_in_status.setObjectName("dim")
        self.osc_in_status.setWordWrap(True)
        form.addRow(self.osc_in_enable)
        form.addRow("Listen port", self.osc_in_port)
        form.addRow(self.osc_in_status)
        self.osc_prefix.textChanged.connect(
            lambda t: self.osc_in and setattr(self.osc_in, "prefix", (t or "/dsan").rstrip("/")))
        self.sig.osc_press.connect(lambda b: self._on_pc_button(b, test=True))
        self.sig.osc_message.connect(self._on_osc_message)
        return g

    def _build_remote(self) -> QGroupBox:
        g = QGroupBox("REMOTE")
        form = QFormLayout(g)
        self.remote_enable = QCheckBox("Allow control from a web browser")
        self.remote_enable.setToolTip("Serve a web page for configuring Confidence from a phone, tablet or another "
                                      "computer on this network: preview, messages, layouts, output, inputs, feeds.")
        self.remote_enable.toggled.connect(self._rebuild_remote)
        self.remote_port = QSpinBox()
        self.remote_port.setRange(80, 65535)
        self.remote_port.setValue(8080)
        self.remote_port.setToolTip("8080 works everywhere. 80 makes the address just http://confidence.local "
                                    "(fine on a Mac; Linux needs extra permission for ports under 1024).")
        self.remote_name = QLineEdit()
        self.remote_name.setPlaceholderText("confidence")
        self.remote_name.setMaxLength(40)
        self.remote_name.setValidator(QRegularExpressionValidator(QRegularExpression(r"[A-Za-z0-9-]{0,40}")))
        self.remote_name.setToolTip("The page is announced on the network as <name>.local (Bonjour). "
                                    "Give each computer its own name if you run more than one Confidence.")
        self.remote_name.editingFinished.connect(self._remote_name_changed)
        self.remote_pin = QLineEdit()
        self.remote_pin.setPlaceholderText("optional — recommended on shared networks")
        self.remote_pin.setMaxLength(32)
        self.remote_pin.setToolTip("Anyone opening the page must enter this first")
        self.remote_pin.editingFinished.connect(self._remote_pin_changed)
        self.remote_status = QLabel()
        self.remote_status.setWordWrap(True)
        self.remote_status.setOpenExternalLinks(True)
        self.remote_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        form.addRow(self.remote_enable)
        form.addRow("Name", self.remote_name)
        form.addRow("Port", self.remote_port)
        form.addRow("PIN", self.remote_pin)
        form.addRow(self.remote_status)
        return g

    def _rebuild_remote(self, *_):
        self._stop_bonjour()
        if self.remote:
            self.remote.close()
            self.remote = None
        on = self.remote_enable.isChecked()
        self.remote_port.setEnabled(not on)
        if not on:
            self.remote_status.setText("")
            return
        from .remote import RemoteServer
        port = self.remote_port.value()
        try:
            self.remote = RemoteServer(self, port, pin=self.remote_pin.text().strip())
        except OSError as e:
            self.remote_enable.setChecked(False)
            self._status(self.remote_status, f"Can't use port {port}: {e.strerror or e} — pick another", "#FF3B3B")
            return
        self._start_bonjour()
        self._update_remote()

    def _start_bonjour(self):
        from . import bonjour
        from .remote import local_addresses
        self._stop_bonjour()
        if self.remote is None or not bonjour.available():
            return
        self.bonjour = bonjour.Announcer(self.remote_name.text() or bonjour.DEFAULT_NAME, self.remote.port,
                                         local_addresses(names=False))

    def _stop_bonjour(self):
        if self.bonjour is not None:
            self.bonjour.close()
            self.bonjour = None

    def _remote_name_changed(self):
        from . import bonjour
        text = self.remote_name.text()
        if text and bonjour.clean_name(text) != text:
            self.remote_name.setText(bonjour.clean_name(text))
        if self.remote is not None and (self.bonjour is None or
                                        self.bonjour.wanted != bonjour.clean_name(self.remote_name.text())):
            self._start_bonjour()
            self._update_remote()

    def _remote_pin_changed(self):
        if self.remote:
            self.remote.pin = self.remote_pin.text().strip()
            self._update_remote()

    def _update_remote(self):
        r = self.remote
        if r is None:
            return
        from .remote import local_addresses
        ips = [a for a in local_addresses(names=False) if a != "127.0.0.1"]
        b = self.bonjour
        if b is not None and b.state != "starting" and ips and sorted(b.addresses) != sorted(ips):
            self._start_bonjour()                     # the network changed (new Wi-Fi, cable…)
            b = self.bonjour
        port = "" if r.port == 80 else f":{r.port}"
        urls = []
        if b is not None and b.state == "ok":
            urls.append(f"http://{b.hostname}{port}")
        urls += [f"http://{a}{port}" for a in (ips or ["127.0.0.1"])[:2]]
        links = "<br>".join(f'<a href="{u}" style="color:#4A8CFF">{u}</a>' for u in urls)
        if b is not None and b.state == "starting":
            links += f"<br><span style='color:#8A8A8A'>announcing {b.wanted}.local…</span>"
        elif b is not None and b.state == "failed":
            links += f"<br><span style='color:#8A8A8A'>(no .local name: {b.error})</span>"
        lock = "PIN required" if r.pin else "<span style='color:#FFB000'>no PIN — anyone on this network can use it</span>"
        seen = f" · {r.requests} requests" if r.requests else ""
        self.remote_status.setStyleSheet("")
        self.remote_status.setText(f"<span style='color:#00E07A'>●</span> Open on a phone or computer on this network:"
                                   f"<br>{links}<br><span style='color:#8A8A8A'>{lock}{seen}</span>")

    def _build_trucue(self) -> QGroupBox:
        g = QGroupBox("TRUCUE")
        form = QFormLayout(g)
        self.tc_enable = QCheckBox("Receive TruCue feedback")
        self.tc_enable.setEnabled(OSC_AVAILABLE)
        self.tc_enable.setToolTip("Clip, countdown to OUT, next clip and the next bookmark from TruCue. Use the "
                                  "TruCue source on timers, progress bars and lights, and {tc_clip} "
                                  "{tc_remaining} {tc_bookmark}… in labels.")
        self.tc_enable.toggled.connect(self._rebuild_trucue)
        self.tc_port = QSpinBox()
        self.tc_port.setRange(1024, 65535)
        self.tc_port.setValue(9017)
        self.tc_forward = QLineEdit()
        self.tc_forward.setPlaceholderText("off — e.g. 127.0.0.1:9018 for Companion")
        self.tc_forward.setToolTip("TruCue sends its feedback to one place. To keep Companion's TruCue module "
                                   "working on this computer, set its feedback port to another number (e.g. 9018) "
                                   "and forward to it here.")
        thresholds = QHBoxLayout()
        self.tc_yellow = QSpinBox()
        self.tc_red = QSpinBox()
        for w, val, tip in ((self.tc_yellow, 30, "Timers turn yellow with this many seconds left"),
                            (self.tc_red, 10, "Timers turn red with this many seconds left")):
            w.setRange(0, 3600)
            w.setSuffix(" s")
            w.setValue(val)
            w.setToolTip(tip)
        for text, w in (("Yellow", self.tc_yellow), ("Red", self.tc_red)):
            lbl = QLabel(text)
            lbl.setObjectName("dim")
            thresholds.addWidget(lbl)
            thresholds.addWidget(w)
        thresholds.addStretch()
        self.tc_status = QLabel("" if OSC_AVAILABLE else "python-osc not installed")
        self.tc_status.setWordWrap(True)
        self.tc_hint = QLabel()
        self.tc_hint.setObjectName("dim")
        self.tc_hint.setWordWrap(True)
        form.addRow(self.tc_enable)
        form.addRow("Listen port", self.tc_port)
        form.addRow("Forward to", self.tc_forward)
        form.addRow("Last seconds", thresholds)
        form.addRow(self.tc_status)
        form.addRow(self.tc_hint)
        self.sig.tc_message.connect(self._on_tc_message)
        return g

    @staticmethod
    def _parse_hostport(text: str):
        host, _, port = text.strip().rpartition(":")
        if not text.strip():
            return None
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"“{text}” isn't host:port")
        return host, int(port)

    def _rebuild_trucue(self, *_):
        if self.tc_rx:
            self.tc_rx.close()
            self.tc_rx = None
        on = OSC_AVAILABLE and self.tc_enable.isChecked()
        self.tc_port.setEnabled(not on)
        self.tc_forward.setEnabled(not on)
        if not on:
            self.tc_status.setText("")
            self.tc_hint.setText("")
            return
        from ..trucue import TruCueReceiver
        port = self.tc_port.value()
        try:
            forward = self._parse_hostport(self.tc_forward.text())
            if forward and forward[1] == port and forward[0] in ("127.0.0.1", "localhost", "0.0.0.0"):
                raise ValueError("forwarding to the port Confidence listens on would loop")
            self.tc_rx = TruCueReceiver(port, forward=forward, on_message=self.sig.tc_message.emit)
        except (OSError, ValueError) as e:
            self.tc_enable.setChecked(False)
            why = getattr(e, "strerror", None) or str(e)
            self._status(self.tc_status, f"Can't start TruCue feedback: {why}", "#FF3B3B")
            return
        self.tc_hint.setText(f"In TruCue: Settings → OSC → turn on Companion feedback, host = this computer's IP "
                             f"address (127.0.0.1 if TruCue runs here), port {port}."
                             + (f" Forwarding to {forward[0]}:{forward[1]}." if forward else ""))
        self._update_trucue()

    def _on_tc_message(self, address: str, args, used: bool):
        self.debug_tap.trucue(address, tuple(args), used)

    def _update_trucue(self):
        rx = self.tc_rx
        if rx is None:
            return
        v = rx.tracker.view()
        if not rx.tracker.received:
            self._status(self.tc_status, f"● Waiting for TruCue on port {rx.port}", "#FFB000")
        elif not (bool(rx.tracker.last_message) and time.monotonic() - rx.tracker.last_message < 5):
            self._status(self.tc_status, "● No feedback from TruCue for a while", "#FFB000")
        elif v is None:
            self._status(self.tc_status, "● Connected — no clip loaded", "#00E07A")
        else:
            from ..millumin import format_time
            mode = {"loop": " · LOOP", "auto-next": " · AUTO NEXT", "auto-first": " · AUTO FIRST"}.get(v.mode, "")
            self._status(self.tc_status, f"● {v.clip} — {v.status}{mode} · "
                                         f"{format_time(v.remaining, round_up=True)} left", "#00E07A")

    def _build_millumin(self) -> QGroupBox:
        g = QGroupBox("MILLUMIN")
        form = QFormLayout(g)
        self.mm_enable = QCheckBox("Receive Millumin feedback")
        self.mm_enable.setEnabled(OSC_AVAILABLE)
        self.mm_enable.setToolTip("Clip name, duration, elapsed / remaining time, play state, loop and freeze "
                                  "from Millumin's OSC feedback. Use the Millumin source on timers, progress "
                                  "bars and lights, and {clip} {remaining} {state}… in labels.")
        self.mm_enable.toggled.connect(self._rebuild_millumin)
        self.mm_port = QSpinBox()
        self.mm_port.setRange(1024, 65535)
        self.mm_port.setValue(5001)
        self.mm_layer = Combo()
        self.mm_layer.setEditable(True)
        self.mm_layer.addItem("Selected layer", "")
        self.mm_layer.setToolTip("Follow Millumin's selected layer, or pick / type a layer name")
        self.mm_layer.lineEdit().setPlaceholderText("Selected layer")
        thresholds = QHBoxLayout()
        self.mm_yellow = QSpinBox()
        self.mm_red = QSpinBox()
        for w, val, tip in ((self.mm_yellow, 30, "Timers turn yellow with this many seconds left"),
                            (self.mm_red, 10, "Timers turn red with this many seconds left")):
            w.setRange(0, 3600)
            w.setSuffix(" s")
            w.setValue(val)
            w.setToolTip(tip)
        for text, w in (("Yellow", self.mm_yellow), ("Red", self.mm_red)):
            lbl = QLabel(text)
            lbl.setObjectName("dim")
            thresholds.addWidget(lbl)
            thresholds.addWidget(w)
        thresholds.addStretch()
        self.mm_status = QLabel("" if OSC_AVAILABLE else "python-osc not installed")
        self.mm_status.setWordWrap(True)
        self.mm_hint = QLabel()
        self.mm_hint.setObjectName("dim")
        self.mm_hint.setWordWrap(True)
        form.addRow(self.mm_enable)
        form.addRow("Listen port", self.mm_port)
        form.addRow("Layer", self.mm_layer)
        form.addRow("Last seconds", thresholds)
        form.addRow(self.mm_status)
        form.addRow(self.mm_hint)
        self.sig.mm_message.connect(self._on_mm_message)
        return g

    def mm_layer_name(self) -> str:
        text = self.mm_layer.currentText().strip()
        return "" if text in ("", "Selected layer") else text

    def _rebuild_millumin(self, *_):
        if self.mm_rx:
            self.mm_rx.close()
            self.mm_rx = None
        on = OSC_AVAILABLE and self.mm_enable.isChecked()
        self.mm_port.setEnabled(not on)
        if not on:
            self.mm_status.setText("")
            self.mm_hint.setText("")
            return
        from ..millumin import MilluminReceiver
        port = self.mm_port.value()
        try:
            self.mm_rx = MilluminReceiver(port, on_message=self.sig.mm_message.emit)
        except OSError as e:
            self.mm_enable.setChecked(False)
            self._status(self.mm_status, f"Can't listen on {port}: {e.strerror or e} — pick another port",
                         "#FF3B3B")
            return
        self.mm_hint.setText(f"In Millumin: Device Manager (⌘K) → OSC → turn on API feedback and send it "
                             f"to this computer's IP address, port {port}.")
        self._update_millumin()

    def _on_mm_message(self, address: str, args, used: bool):
        self.debug_tap.millumin(address, tuple(args), used)

    def _update_millumin(self):
        """Status line and the layer list (called every second)."""
        rx = self.mm_rx
        if rx is None:
            return
        known = rx.tracker.layers()
        have = [self.mm_layer.itemText(i) for i in range(1, self.mm_layer.count())]
        if known != have:
            text = self.mm_layer.currentText()
            self.mm_layer.blockSignals(True)
            while self.mm_layer.count() > 1:
                self.mm_layer.removeItem(1)
            for n in known:
                self.mm_layer.addItem(n, n)
            self.mm_layer.setCurrentText(text)
            self.mm_layer.blockSignals(False)
        v = rx.tracker.view(self.mm_layer_name())
        if not rx.tracker.received:
            self._status(self.mm_status, f"● Waiting for Millumin on port {rx.port}", "#FFB000")
        elif v is None:
            self._status(self.mm_status, "● Connected — nothing on this layer in this column", "#00E07A")
        elif not v.live:
            self._status(self.mm_status, "● No feedback from Millumin for a while", "#FFB000")
        else:
            from ..millumin import format_time
            mode = {"loop": " · LOOP", "freeze": ""}.get(v.mode, "")
            left = f" · {format_time(v.remaining, round_up=True)} left" if v.duration else ""
            self._status(self.mm_status, f"● {v.clip or v.layer} — {v.status}{mode}{left}", "#00E07A")

    # ── ports ───────────────────────────────────────────────────────────────────
    #
    # self.refs["lt"/"pc"] is the source of truth: the adapter chosen for each
    # device, remembered by USB serial number / socket (PortRef); self.want[...] is
    # whether the operator has it connected. Changing the port of a
    # connected device reconnects it. The list is re-scanned every 2 s, so
    # adapters appear as they're plugged in, and a connected adapter that comes
    # back (even under a new /dev name) reconnects by itself.

    _URL_ITEM = "__url__"

    def _combo(self, kind: str) -> QComboBox:
        return self.lt_combo if kind == "lt" else self.pc_combo

    def _visible_ports(self) -> List[portsmod.PortInfo]:
        infos = list(self._port_infos.values())
        if not self.show_all_ports.isChecked():
            infos = [p for p in infos if p.is_usb]
        return sorted(infos, key=lambda p: (not p.is_usb, p.device))

    def _refresh_ports(self, force: bool = False):
        infos = portsmod.list_ports()
        sig = tuple(sorted((p.device, p.serial_number, p.location) for p in infos))
        changed = sig != getattr(self, "_ports_sig", None)
        self._ports_sig = sig
        self._port_infos = {p.device: p for p in infos}
        if changed or force:
            self._rebuild_port_combos()
            self.lt_panel.ports_changed()
        if changed and not self.demo and not self._detecting:
            for kind in ("lt", "pc"):
                ref = self.refs.get(kind)
                if ref and self.want[kind] and self._resolved(kind) != self._running_port(kind):
                    self._apply_device(kind)     # adapter (re)appeared or moved
        self._update_activity()

    def _rebuild_port_combos(self):
        ports = self._visible_ports()
        for kind in ("lt", "pc"):
            combo = self._combo(kind)
            other = self._resolved("pc" if kind == "lt" else "lt")
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Off — not used", "")
            for p in ports:
                used = "   (used by " + ("Perfect Cue" if kind == "lt" else "Limitimer") + ")" \
                    if p.device == other else ""
                combo.addItem(p.short_label() + used, p.device)
            if not ports:
                combo.addItem("No USB serial adapters found — plug one in", None)
                combo.model().item(combo.count() - 1).setEnabled(False)
            ref = self.refs.get(kind)
            dev = self._resolved(kind)
            if ref and not dev:
                combo.addItem(f"{ref.description or os.path.basename(ref.device)} — not plugged in", "__missing__")
            elif ref and combo.findData(dev) < 0:
                combo.addItem(dev, dev)          # a URL such as socket://host:port
            combo.addItem("Network adapter (socket://host:port)…", self._URL_ITEM)
            if ref is None:
                combo.setCurrentIndex(0)
            else:
                i = combo.findData(dev if dev else "__missing__")
                combo.setCurrentIndex(max(0, i))
            combo.blockSignals(False)

    def _resolved(self, kind: str) -> Optional[str]:
        ref = self.refs.get(kind)
        return ref.resolve(self._port_infos.values()) if ref else None

    def _running_port(self, kind: str) -> Optional[str]:
        if kind == "lt" and self.emulating:
            return self.lt_bus.port if self.lt_bus else None
        dev = self.limitimer if kind == "lt" else self.perfectcue
        return dev.port if dev else None

    @property
    def emulating(self) -> bool:
        """Confidence is the Limitimer (software); the Limitimer adapter sends to DSAN lights."""
        return hasattr(self, "lt_panel") and self.lt_panel.mode == "emulate" and not self.demo

    def _port_picked(self, kind: str, index: int):
        """The operator chose an entry in one of the dropdowns."""
        combo = self._combo(kind)
        data = combo.itemData(index)
        if data == "__missing__" or data is None:
            return
        if data == self._URL_ITEM:
            from PyQt6.QtWidgets import QInputDialog
            current = self.refs.get(kind)
            text, ok = QInputDialog.getText(
                self, "Network adapter", "RS-485-to-Ethernet converter address:",
                text=current.device if current and "://" in current.device else "socket://192.168.1.50:4001")
            if not ok or "://" not in text.strip():
                self._rebuild_port_combos()
                return
            new_ref = portsmod.PortRef.url(text.strip())
        elif data == "":
            new_ref = None
            self.want[kind] = False
        else:
            info = self._port_infos.get(data)
            new_ref = portsmod.PortRef.of(info) if info else portsmod.PortRef.url(data)
        other = "pc" if kind == "lt" else "lt"
        old_ref = self.refs.get(kind)
        moved_other = False
        if new_ref and self._resolved(other) and new_ref.resolve(self._port_infos.values()) == self._resolved(other):
            # Picked the adapter the other device is using: swap them.
            self.refs[other] = old_ref
            if old_ref is None:
                self.want[other] = False
            moved_other = True
        self.refs[kind] = new_ref
        if self.demo:
            self._toggle_demo(False)      # leaving demo applies both refs
        else:
            self._apply_device(kind)
            if moved_other:
                self._apply_device(other)
        if moved_other:
            name = "Perfect Cue" if other == "pc" else "Limitimer"
            self.statusBar().showMessage(f"That adapter was the {name}'s — swapped them.", 6000)
        self._save_ports()

    def _swap_ports(self):
        self.refs["lt"], self.refs["pc"] = self.refs.get("pc"), self.refs.get("lt")
        self._save_ports()
        if not self.demo:
            self._apply_device("lt")
            self._apply_device("pc")
        self._rebuild_port_combos()

    # ── devices ─────────────────────────────────────────────────────────────────

    def _connect_clicked(self, kind: str, on: bool):
        if on and not self.refs.get(kind):
            self._connect_btn(kind).setChecked(False)
            self.statusBar().showMessage(
                "Pick the RS-485 adapter wired to the DSAN lights first" if kind == "lt" and self.emulating
                else "Pick an adapter for it first", 5000)
            return
        if self.demo:
            self._toggle_demo(False)
        self.want[kind] = on
        self._apply_device(kind)
        self._save_ports()

    def _connect_btn(self, kind: str) -> QPushButton:
        return self.lt_connect if kind == "lt" else self.pc_connect

    def _sync_connect_buttons(self):
        for kind in ("lt", "pc"):
            btn = self._connect_btn(kind)
            on = self.want[kind] and not self.demo
            btn.setChecked(on)
            if kind == "lt" and self.emulating:
                btn.setText("Stop sending" if on else "Send to lights")
                btn.setToolTip("Send the software Limitimer out of this adapter, like a Limitimer console, "
                               "to drive DSAN signal lights and displays.\nOnly with no Limitimer on that bus.")
            else:
                btn.setText("Disconnect" if on else "Connect")
                btn.setToolTip("")
            btn.setEnabled(not self.demo)

    def _make_device(self, kind: str, port: str):
        if port == "sim":
            dev = Limitimer.simulated() if kind == "lt" else PerfectCue.simulated(interval=2.5)
            dev.port = "sim"
            return dev
        if kind == "lt":
            return Limitimer(port, baudrate=BAUD)
        return PerfectCue(port, baudrate=BAUD)

    def _apply_device(self, kind: str):
        """Stop this device and start it again on its chosen adapter (if plugged in)."""
        if self.demo:
            return
        self._stop_device(kind)
        port = self._resolved(kind) if self.want[kind] else None
        if kind == "lt" and self.emulating:
            dev = Limitimer(transport_factory=self.lt_panel.emulator.transport, reconnect=False)
            dev.port = "emulator"
            self._start_limitimer(dev)
            if port:
                self._start_bus(port)
        elif port:
            dev = self._make_device(kind, port)
            if kind == "lt":
                self._start_limitimer(dev)
            else:
                self._start_perfectcue(dev)
        self._rebuild_port_combos()
        self._sync_connect_buttons()
        self._rebuild_osc()
        self._update_activity()

    def _apply_all(self):
        self._apply_device("lt")
        self._apply_device("pc")

    def _toggle_demo(self, on: bool):
        self._stop_devices()
        self.demo_btn.setChecked(on)
        for w in (self.lt_combo, self.pc_combo):
            w.setEnabled(not on)
        if on:
            self.demo = True
            self._start_limitimer(Limitimer.simulated())
            self._start_perfectcue(PerfectCue.simulated(interval=2.5))
            self.statusBar().showMessage("Demo: simulated Limitimer and Perfect Cue", 5000)
            self._rebuild_osc()
        else:
            self._apply_all()
        self._sync_connect_buttons()
        self._update_activity()

    def _start_limitimer(self, dev: Limitimer):
        self.limitimer = dev
        self.debug_tap.attach_limitimer(dev)
        self._lt_count = (0, time.monotonic(), 0.0)
        dev.on_state(self.sig.lt_state.emit)
        dev.on_link(self.sig.lt_link.emit)
        dev.on_error(lambda e: self.sig.error.emit(f"Limitimer: {e}"))
        dev.start()

    def _start_perfectcue(self, dev: PerfectCue):
        self.perfectcue = dev
        self.debug_tap.attach_perfectcue(dev)
        self.pc_raw_bytes = 0
        self.pc_last_press: Optional[tuple] = None

        def count(data, dev=dev):
            if self.perfectcue is dev:
                self.pc_raw_bytes += len(data)

        dev.on_raw(count)
        dev.on_button(self.sig.pc_button.emit)
        dev.on_link(self.sig.pc_link.emit)
        dev.on_error(lambda e: self.sig.error.emit(f"Perfect Cue: {e}"))
        dev.start()

    def _start_bus(self, port: str):
        bus = LimitimerBus(self.lt_panel.emulator, port)
        self.lt_bus = bus
        bus.on_error(lambda e: self.sig.error.emit(f"Limitimer lights: {e}"))
        bus.start()

    def _stop_device(self, kind: str):
        attr = "limitimer" if kind == "lt" else "perfectcue"
        dev = getattr(self, attr)
        setattr(self, attr, None)
        if dev:
            dev.stop()
        if kind == "lt" and self.lt_bus is not None:
            bus, self.lt_bus = self.lt_bus, None
            bus.stop()
        if kind == "lt":
            self.lt_state = None
            self.lt_link = LinkState.CLOSED
        else:
            self.pc_link_state = LinkState.CLOSED
            self.cue = CueState()
            self.pc_last_press = None

    def _stop_devices(self):
        self._stop_device("lt")
        self._stop_device("pc")
        self.demo = False
        self._update_activity()
        self._rebuild_osc()

    # ── device events (GUI thread) ───────────────────────────────────────────────

    def _lt_mode_changed(self, mode: str):
        if self.demo:
            self._toggle_demo(False)          # leaving demo applies the new mode
        else:
            self._apply_device("lt")
        self._dip_key = None
        self._update_dips()
        self.lt_panel.save(self.settings)
        msg = ("Software Limitimer — Confidence runs the timer" if mode == "emulate"
               else "Reading the real Limitimer")
        self.statusBar().showMessage(msg, 5000)

    def _on_lt_state(self, state):
        if self.limitimer is not None:
            self.lt_state = state
            self._update_dips()
            self.lt_panel.show_state(state, self.lt_link == LinkState.LIVE)

    def _on_lt_link(self, link):
        if self.limitimer is None and link != LinkState.CLOSED:
            return
        self.lt_link = link if self.limitimer else LinkState.CLOSED
        self.lt_panel.show_state(self.lt_state, self.lt_link == LinkState.LIVE)
        self._update_activity()

    def _on_pc_link(self, link):
        if self.perfectcue is None and link != LinkState.CLOSED:
            return
        self.pc_link_state = link if self.perfectcue else LinkState.CLOSED
        self._update_activity()

    def _on_pc_button(self, button, test: bool = False):
        if not test and self.perfectcue is None:
            return
        self.cue.press(button)
        if not test:
            self.pc_last_press = (button, time.monotonic())
            self._update_activity()
        if test and self.osc:
            self.osc._on_button(button)

    def _status(self, label: QLabel, text: str, color: str):
        label.setText(text)
        label.setStyleSheet(f"color: {color};")

    def _update_activity(self):
        """Per-device status lines: is the right adapter picked, and is data arriving?"""
        if not hasattr(self, "lt_status"):
            return
        self._update_dips()
        # Limitimer
        lt, link = self.limitimer, self.lt_link
        if self.demo:
            self._status(self.lt_status, "● demo — simulated Limitimer", "#00E07A")
        elif self.emulating:
            bus = self.lt_bus
            if not self.refs.get("lt") or not self.want["lt"]:
                self._status(self.lt_status, "● Software Limitimer (LIMITIMER CONTROL below). To drive DSAN "
                                             "lights, pick their adapter and press Send to lights.", "#00E07A")
            elif bus is None:
                self._status(self.lt_status, "● lights adapter not plugged in — will send when it is", "#FF3B3B")
            elif bus.other_console:
                self._status(self.lt_status, "● Another Limitimer is talking on this bus — turn it off or "
                                             "unplug it: two consoles on one bus scramble the lights", "#FF3B3B")
            elif bus.link == LinkState.RECONNECTING:
                err = f" ({bus.last_error})" if bus.last_error else ""
                self._status(self.lt_status, f"● can't open the lights adapter, retrying{err}", "#FF3B3B")
            elif bus.sent:
                self._status(self.lt_status, f"● Sending to DSAN lights on {os.path.basename(bus.port)}",
                             "#00E07A")
            else:
                self._status(self.lt_status, "● Opening the lights adapter…", "#FFB000")
        elif not self.refs.get("lt"):
            self._status(self.lt_status, "Not used — pick its adapter above", "#6A6A6A")
        elif not self.want["lt"]:
            self._status(self.lt_status, "Disconnected — press Connect", "#6A6A6A")
        elif lt is None:
            self._status(self.lt_status, "● adapter not plugged in — will connect when it is", "#FF3B3B")
        else:
            st = lt.stats
            name = os.path.basename(lt.port)
            if link == LinkState.LIVE:
                self._status(self.lt_status, "● Connected", "#00E07A")
            elif link in (LinkState.WAITING, LinkState.STALE) and st["errors"] and not st["packets"]:
                self._status(self.lt_status, f"● {name}: data arriving but no valid frames — "
                                             "RS-485 A/B swapped, or this is the Perfect Cue adapter?", "#FF3B3B")
            elif link == LinkState.WAITING:
                self._status(self.lt_status, "● Waiting for Limitimer data — is it on, and is this its adapter?",
                             "#FFB000")
            elif link == LinkState.STALE:
                self._status(self.lt_status, f"● {name}: data stopped — output shows NO SIGNAL", "#FF3B3B")
            elif link == LinkState.RECONNECTING:
                err = f" ({lt.last_error})" if lt.last_error else ""
                self._status(self.lt_status, f"● {name}: can't open, retrying{err}", "#FF3B3B")
            else:
                self._status(self.lt_status, f"● {name}: {link.value}", "#FFB000")
        # Perfect Cue
        cue, plink = self.perfectcue, self.pc_link_state
        if self.demo:
            self._status(self.pc_status, "● demo — simulated Perfect Cue", "#00E07A")
        elif not self.refs.get("pc"):
            self._status(self.pc_status, "Not used — pick its adapter above", "#6A6A6A")
        elif not self.want["pc"]:
            self._status(self.pc_status, "Disconnected — press Connect", "#6A6A6A")
        elif cue is None:
            self._status(self.pc_status, "● adapter not plugged in — will connect when it is", "#FF3B3B")
        else:
            name = os.path.basename(cue.port)
            last = getattr(self, "pc_last_press", None)
            if plink == LinkState.OPEN:
                if getattr(self, "pc_raw_bytes", 0) and not last:
                    self._status(self.pc_status, f"● {name}: data arriving but not Perfect Cue buttons — "
                                                 "check the wiring (pins 2/4 differ from the Limitimer), or is this the Limitimer adapter?", "#FFB000")
                else:
                    self._status(self.pc_status, "● Connected", "#00E07A")
            elif plink == LinkState.RECONNECTING:
                err = f" ({cue.last_error})" if cue.last_error else ""
                self._status(self.pc_status, f"● {name}: can't open, retrying{err}", "#FF3B3B")
            else:
                self._status(self.pc_status, f"● {name}: {plink.value}", "#FFB000")
        if link == LinkState.STALE and not self.demo:
            self.statusBar().showMessage("Limitimer data stopped — the output is showing NO SIGNAL")

    # ── detection ───────────────────────────────────────────────────────────────

    def _start_detect(self):
        if self._detecting:
            return
        if self.demo:
            self._toggle_demo(False)
        self._stop_devices()            # free the ports so detection can open them
        self._detecting = True
        self.detect_action.setEnabled(False)
        self.detect_label.show()
        self.detect_label.setText("Listening…")
        def work():
            try:
                res = portsmod.detect(pc_baud=BAUD, on_status=self.sig.detect_status.emit)
            except Exception as e:  # pragma: no cover
                res = portsmod.DetectResult(notes=[f"Detect failed: {e}"])
            self.sig.detect_done.emit(res)

        threading.Thread(target=work, daemon=True).start()

    def _detect_note(self, text: str):
        self.detect_label.setText(text)

    def _detect_finished(self, res: portsmod.DetectResult):
        self._detecting = False
        self.detect_action.setEnabled(True)
        lines = []
        if res.limitimer:
            self.refs["lt"] = portsmod.PortRef.of(res.limitimer)
            self.want["lt"] = True
            n = res.probes.get(res.limitimer.device)
            lines.append(f"Limitimer → {res.limitimer.short_label()}"
                         + (f" ({n.lt_packets} packets)" if n else ""))
        if res.perfectcue:
            self.refs["pc"] = portsmod.PortRef.of(res.perfectcue)
            self.want["pc"] = True
            lines.append(f"Perfect Cue → {res.perfectcue.short_label()}")
        lines += res.notes
        self.detect_label.setText("\n".join(lines) or "Nothing found.")
        self._save_ports()
        self._refresh_ports(force=True)
        self._apply_all()

    # ── output ──────────────────────────────────────────────────────────────────

    def _refresh_screens(self):
        current = getattr(self, "_wanted_screen", None) or self.screen_combo.currentData()
        self.screen_combo.blockSignals(True)
        self.screen_combo.clear()
        primary = QGuiApplication.primaryScreen()
        for i, scr in enumerate(QGuiApplication.screens()):
            g = scr.geometry()
            tag = " (main)" if scr == primary else ""
            self.screen_combo.addItem(f"{i + 1}: {scr.name()} {g.width()}×{g.height()}{tag}", scr.name())
            if id(scr) not in self._watched_screens:
                self._watched_screens.add(id(scr))
                scr.geometryChanged.connect(lambda _g: self._screens_changed())
        i = self.screen_combo.findData(current) if current else -1
        if i < 0 and not current:
            # first run: default to a second display if there is one
            others = [k for k in range(self.screen_combo.count())
                      if QGuiApplication.screens()[k] != primary]
            i = others[0] if others else 0
        if i >= 0:
            self.screen_combo.setCurrentIndex(i)
        self.screen_combo.blockSignals(False)

    def _screens_changed(self):
        """A display was plugged in, unplugged, or changed resolution."""
        self._refresh_screens()
        if not self.show_output_btn.isChecked():
            return
        wanted = self._wanted_screen
        present = any(scr.name() == wanted for scr in QGuiApplication.screens())
        if self.mode_combo.currentData() == "window":
            return
        if present:
            self._apply_output()           # back on its display / new resolution
        elif self.output.mode != "window":
            # Don't fall back to full screen on the laptop: park it in a window.
            self.output.apply("window", None)
            self.output_note.setText(f"Display {wanted} is disconnected — the output is in a window "
                                     "for now and will go back when the display returns.")
            self.output_note.setStyleSheet("color: #FFB000;")

    def _selected_screen(self):
        name = self.screen_combo.currentData()
        for s in QGuiApplication.screens():
            if s.name() == name:
                return s
        return QGuiApplication.primaryScreen()

    def _toggle_output(self, on: bool):
        self.show_output_btn.setText("Hide output" if on else "Show output")
        if on:
            self._apply_output()
        else:
            self.output.hide()
            self.output_note.setText("")

    def _output_settings_changed(self, *_):
        self._wanted_screen = self.screen_combo.currentData()
        if self.show_output_btn.isChecked():
            self._apply_output()

    def _apply_output(self):
        mode = self.mode_combo.currentData()
        screen = self.screen_combo.currentData()
        self._wanted_screen = screen
        self.output_note.setStyleSheet("")
        self.output.apply(mode, screen)
        self.activateWindow()
        notes = {
            "window": "Resizable window. Double-click it (or press F) for full screen.",
            "fullscreen": "Covers only the chosen display. Click the output and press Esc "
                          "(or Hide output) to leave.",
            "overlay": "Transparent and click-through. Use a layout with a transparent background "
                       "(e.g. Corner overlay).",
        }
        note = notes.get(mode, "")
        if mode == "fullscreen" and self._selected_screen() == self.screen():
            note = ("This is the display the control window is on, so full screen will cover it. "
                    "Pick your second display above, or click the output and press Esc to leave.")
        if mode == "overlay" and sys.platform == "darwin" and not self.output.mac_float_applied:
            note += (" To float over full-screen Keynote/PowerPoint on macOS, install pyobjc "
                     "(pip install 'dsan[clock]'), or play the slideshow in a window.")
        self.output_note.setText(note)

    # ── message ─────────────────────────────────────────────────────────────────

    def _send_message(self, text: Optional[str] = None):
        text = (text if isinstance(text, str) else self.msg_edit.text()).strip()
        if not text:
            return
        self.message = text
        has = self.layout_obj.first("message") is not None
        self.msg_status.setText(f"On screen: {text}" if has else
                                "This layout has no Message element — add one to show messages.")
        self.msg_status.setStyleSheet(f"color: {'#FFB000' if has else '#FF3B3B'};")

    def _clear_message(self):
        self.message = ""
        self.msg_edit.clear()
        self.msg_status.setText("Nothing on screen")
        self.msg_status.setStyleSheet("")

    # ── OSC ─────────────────────────────────────────────────────────────────────

    def _rebuild_osc_in(self, *_):
        if self.osc_in:
            self.osc_in.close()
            self.osc_in = None
        on = OSC_AVAILABLE and self.osc_in_enable.isChecked()
        self.osc_in_port.setEnabled(not on)
        if not on:
            self.osc_in_status.setText("")
            return
        port = self.osc_in_port.value()
        host, _, out_port = self.osc_target.text().strip().rpartition(":")
        if (self.osc_enable.isChecked() and out_port == str(port)
                and host in ("", "127.0.0.1", "localhost", "0.0.0.0")):
            self.osc_in_enable.setChecked(False)
            self.osc_in_status.setText(f"Port {port} is also the OSC out target — pick another to avoid a loop")
            return
        try:
            self.osc_in = OscListener(port, perfectcue=self.perfectcue,
                                      prefix=self.osc_prefix.text() or "/dsan",
                                      on_press=self.sig.osc_press.emit,
                                      on_message=self.sig.osc_message.emit,
                                      on_limitimer=self.lt_panel.press)
            self.osc_in_status.setText(f"Listening on UDP {port} · {self.osc_in.prefix}/perfectcue/… "
                                       f"and {self.osc_in.prefix}/limitimer/…")
        except OSError as e:
            self.osc_in_enable.setChecked(False)
            self.osc_in_status.setText(f"Can't listen on {port}: {e.strerror or e}")

    def _on_osc_message(self, address: str, args, result: str):
        if self.osc_in is None:
            return
        self.debug_tap.osc_in(address, tuple(args), result)
        shown = " ".join(str(a) for a in args)
        self.osc_in_status.setText(f"Listening on UDP {self.osc_in.port} · {self.osc_in.received} received\n"
                                   f"Last: {address}{' ' + shown if shown else ''} → {result}")

    def _rebuild_osc(self, *_):
        if self.osc_in:
            self.osc_in.perfectcue = self.perfectcue       # LEDs/commands follow the connected device
        if self.osc:
            self.osc.close()
            self.osc = None
        on = OSC_AVAILABLE and self.osc_enable.isChecked()
        self.osc_target.setEnabled(not on)
        self.osc_prefix.setEnabled(not on)
        if not on:
            if OSC_AVAILABLE:
                self.osc_status.setText("")
            return
        host, _, port = self.osc_target.text().strip().rpartition(":")
        try:
            self.osc = OscBridge(host or "127.0.0.1", int(port), limitimer=self.limitimer,
                                 perfectcue=self.perfectcue, prefix=self.osc_prefix.text() or "/dsan")
            self.osc_status.setText(f"Sending to {host}:{port}")
        except Exception as e:
            self.osc_status.setText(f"OSC error: {e}")
            self.osc_enable.setChecked(False)

    # ── layout editing ──────────────────────────────────────────────────────────

    def _set_layout(self, lay: Layout, path: Optional[str] = None):
        self.layout_obj = lay
        self.layout_path = path
        self._load_font_files()
        self.preview.set_selection([])
        self._refresh_list()
        self._show_selection()
        self._update_layout_label()
        self._autosave_layout()

    def _update_layout_label(self):
        where = os.path.basename(self.layout_path) if self.layout_path else "not saved to a file"
        self.layout_label.set_full_text(f"{self.layout_obj.name} — {where}")

    def _load_preset(self, index: int):
        name = self.preset_combo.itemData(index)
        self.preset_combo.setCurrentIndex(0)
        if not name:
            return
        self._set_layout(PRESETS[name]())
        if name == "Corner overlay" and self.mode_combo.currentData() != "overlay":
            self.statusBar().showMessage("Corner overlay has a transparent background — "
                                         "set Output mode to Overlay to put it over slides", 8000)

    def _fit_props_width(self):
        """The properties panel never needs a sideways scroll bar: it's at least as wide as its fields."""
        ps = getattr(self, "_props_scroll", None)
        if ps is None:
            return
        need = ps.widget().minimumSizeHint().width() + ps.verticalScrollBar().sizeHint().width() + 2 * ps.frameWidth()
        ps.setMinimumWidth(need)

    # ── alignment toolbar ────────────────────────────────────────────────────────

    def _tool(self, icon: str, tip: str, slot=None, checkable: bool = False) -> QToolButton:
        b = QToolButton()
        b.setIcon(make_icon(icon))
        b.setIconSize(QSize(20, 20))
        b.setToolTip(tip)
        b.setCheckable(checkable)
        if slot:
            b.clicked.connect(slot)
        return b

    def _build_align_bar(self) -> QWidget:
        """The editor toolbar: groups of tools that wrap onto a second row when the window is narrow."""
        bar = QWidget()
        flow = FlowLayout(bar, h_spacing=4, v_spacing=4)
        state = {"h": None}

        def group(first: bool = False):
            g = QWidget()
            hl = QHBoxLayout(g)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(2)
            if not first:
                line = QLabel("│")
                line.setObjectName("dim")
                line.setFixedWidth(line.sizeHint().width())
                g.setProperty("flow_sep", line)          # FlowLayout blanks it when the group starts a row
                hl.addWidget(line)
                hl.addSpacing(4)
            flow.addWidget(g)
            state["h"] = hl
            return hl

        h = group(first=True)

        self.undo_btn = self._tool("undo", "Undo (⌘Z)", self._undo)
        self.redo_btn = self._tool("redo", "Redo (⇧⌘Z)", self._redo)
        h.addWidget(self.undo_btn)
        h.addWidget(self.redo_btn)
        h = group()
        self._align_btns = []
        for how, tip in (("left", "Align left edges"), ("hcenter", "Align horizontal centres"),
                         ("right", "Align right edges"), ("top", "Align top edges"),
                         ("vcenter", "Align vertical centres"), ("bottom", "Align bottom edges")):
            b = self._tool(f"align-{how}", tip, lambda _=False, h_=how: self._align(h_))
            self._align_btns.append(b)
            h.addWidget(b)
        h.addSpacing(4)
        lbl = QLabel("to")
        lbl.setObjectName("dim")
        h.addWidget(lbl)
        self.align_to = Combo()
        for key, label in (("auto", "Auto"), ("canvas", "Canvas"), ("selection", "Selection"),
                           ("title", "Title safe"), ("action", "Action safe")):
            self.align_to.addItem(label, key)
        self.align_to.setToolTip("Auto: one element aligns to the canvas, several align to each other")
        self.align_to.setMinimumWidth(110)
        self.align_to.setStyleSheet("min-width: 90px;")
        h.addWidget(self.align_to)
        h = group()
        self.dist_h = self._tool("dist-h", "Distribute horizontally (3 or more)", lambda: self._distribute("h"))
        self.dist_v = self._tool("dist-v", "Distribute vertically (3 or more)", lambda: self._distribute("v"))
        self.match_w = self._tool("match-w", "Match width of the first selected", lambda: self._match("width"))
        self.match_h = self._tool("match-h", "Match height of the first selected", lambda: self._match("height"))
        self.fill_btn = self._tool("fill", "Fill the canvas (or the safe area chosen in 'to')", self._fill)
        for b in (self.dist_h, self.dist_v, self.match_w, self.match_h, self.fill_btn):
            h.addWidget(b)
        h = group()
        self.snap_btn = self._tool("snap", "Smart guides: snap to edges, centres and safe areas "
                                           "(hold ⌘ while dragging to turn off)", self._editor_options, True)
        self.snap_btn.setChecked(True)
        self.safe_btn = self._tool("safe", "Show title-safe (90%) and action-safe (93%) areas",
                                   self._editor_options, True)
        self.grid_btn = self._tool("grid", "Show the grid", self._editor_options, True)
        self.grid_combo = Combo()
        for step, label in ((0.0, "No grid snap"), (0.01, "1%"), (0.025, "2.5%"), (0.05, "5%"), (0.1, "10%")):
            self.grid_combo.addItem(label, step)
        self.grid_combo.setToolTip("Grid step: dragging and arrow keys snap to it")
        self.grid_combo.setStyleSheet("min-width: 90px;")
        self.grid_combo.currentIndexChanged.connect(lambda _i: self._editor_options())
        for w in (self.snap_btn, self.safe_btn, self.grid_btn, self.grid_combo):
            h.addWidget(w)
        bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        return bar

    def _editor_options(self, *_):
        c = self.preview
        c.snap = self.snap_btn.isChecked()
        c.show_safe = self.safe_btn.isChecked()
        c.grid = self.grid_combo.currentData() or 0.0
        c.show_grid = self.grid_btn.isChecked() and c.grid > 0
        if self.grid_btn.isChecked() and not c.grid:
            self.grid_combo.setCurrentIndex(self.grid_combo.findData(0.05))   # turning the grid on picks 5%
        c.update()

    def _update_tools(self):
        n = len(self.preview.selected)
        for b in self._align_btns:
            b.setEnabled(n >= 1)
        self.fill_btn.setEnabled(n >= 1)
        self.dist_h.setEnabled(n >= 3)
        self.dist_v.setEnabled(n >= 3)
        self.match_w.setEnabled(n >= 2)
        self.match_h.setEnabled(n >= 2)
        self.undo_btn.setEnabled(self.history.can_undo or self._pending_change())
        self.redo_btn.setEnabled(self.history.can_redo)
        self.undo_action.setEnabled(self.undo_btn.isEnabled())
        self.redo_action.setEnabled(self.redo_btn.isEnabled())

    def _align_ref(self, els) -> Optional[al.Rect]:
        mode = self.align_to.currentData()
        return {"auto": None, "canvas": al.CANVAS, "selection": al.bounds(els),
                "title": al.safe_rect("title"), "action": al.safe_rect("action")}[mode]

    def _align(self, how: str):
        els = self.preview.elements()
        if els:
            al.align(els, how, self._align_ref(els))
            self._geometry_edited()

    def _distribute(self, axis: str):
        els = self.preview.elements()
        if len(els) < 3:
            self.statusBar().showMessage("Select 3 or more elements to distribute", 4000)
            return
        al.distribute(els, axis)
        self._geometry_edited()

    def _match(self, what: str):
        els = self.preview.elements()
        if len(els) < 2:
            self.statusBar().showMessage("Select 2 or more; the first one selected sets the size", 4000)
            return
        al.match_size(els, what)
        self._geometry_edited()

    def _fill(self):
        els = self.preview.elements()
        if not els:
            return
        mode = self.align_to.currentData()
        ref = al.safe_rect(mode) if mode in ("title", "action") else al.CANVAS
        al.fill(els, ref)
        self._geometry_edited()

    def _output_pixels(self) -> tuple:
        """Pixel size of where the layout goes, for the editor readout."""
        out = getattr(self, "decklink_panel", None)
        if out is not None and out.output is not None:
            return out.output.size
        if self.output.isVisible() and self.output.mode == "window":
            g = self.output.geometry()
            dpr = self.output.devicePixelRatioF()
        else:
            scr = self._selected_screen()
            g, dpr = scr.geometry(), scr.devicePixelRatio()
        return round(g.width() * dpr), round(g.height() * dpr)

    # ── undo / redo ──────────────────────────────────────────────────────────────

    def _pending_change(self) -> bool:
        return self._committed is not None and self.layout_obj.to_dict() != self._committed

    def _commit_history(self):
        """Record the settled state; the previous one becomes an undo step."""
        current = self.layout_obj.to_dict()
        if self._committed is None:
            self._committed = current
        elif current != self._committed:
            self.history.push(self._committed)
            self._committed = current
        self._update_tools()

    def _undo(self):
        self._commit_history()                    # don't lose an edit still inside the debounce
        snap = self.history.undo(self.layout_obj.to_dict())
        if snap is not None:
            self._restore(snap)

    def _redo(self):
        snap = self.history.redo(self.layout_obj.to_dict())
        if snap is not None:
            self._restore(snap)

    def _restore(self, snap: dict):
        keep = list(self.preview.selected)
        self.layout_obj = Layout.from_dict(snap)
        self._committed = snap
        self._load_font_files()
        self._refresh_list()
        self.preview.set_selection(keep)
        self._show_selection()
        self._update_layout_label()
        self.settings.setValue("layout/json", self.layout_obj.to_json())
        self._update_tools()

    # ── element list & selection ─────────────────────────────────────────────────

    def _refresh_list(self):
        self.element_list.blockSignals(True)
        self.element_list.clear()
        # top of the list = drawn last = on top
        for e in reversed(self.layout_obj.elements):
            item = QListWidgetItem(f"{e.name}")
            item.setToolTip(ELEMENT_TYPES[e.type])
            item.setData(Qt.ItemDataRole.UserRole, e.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if e.visible else Qt.CheckState.Unchecked)
            self.element_list.addItem(item)
        self.element_list.blockSignals(False)
        self._sync_list_selection()

    def _sync_list_selection(self):
        sel = set(self.preview.selected)
        primary = self.preview.selected[0] if self.preview.selected else None
        self.element_list.blockSignals(True)
        for i in range(self.element_list.count()):
            item = self.element_list.item(i)
            eid = item.data(Qt.ItemDataRole.UserRole)
            item.setSelected(eid in sel)
            if eid == primary:
                self.element_list.setCurrentItem(item, self.element_list.selectionModel().SelectionFlag.NoUpdate)
        self.element_list.blockSignals(False)

    def _list_selection_changed(self):
        items = self.element_list.selectedItems()
        cur = self.element_list.currentItem()
        ids = [i.data(Qt.ItemDataRole.UserRole) for i in items]
        if cur is not None and cur.isSelected():
            cid = cur.data(Qt.ItemDataRole.UserRole)
            ids = [cid] + [i for i in ids if i != cid]
        self.preview.set_selection(ids)
        self._show_selection(sync_list=False)

    def _list_item_changed(self, item: QListWidgetItem):
        e = self.layout_obj.find(item.data(Qt.ItemDataRole.UserRole))
        if e:
            e.visible = item.checkState() == Qt.CheckState.Checked
            if self.props.element is e:
                self.props.show_element(e)
            self._dirty()

    def _show_selection(self, sync_list: bool = True):
        els = self.preview.elements()
        self.props.setVisible(bool(els))
        self.layout_props.setVisible(not els)
        if len(els) == 1:
            self.props.show_element(els[0])
        elif els:
            self.props.show_multi(els, self._output_pixels())
        else:
            self.props.show_element(None)
            self.layout_props.show_layout(self.layout_obj)
        self._fit_props_width()
        if sync_list:
            self._sync_list_selection()
        self._update_tools()

    def _select_element(self, element_id: str):
        self.preview.set_selection([element_id] if element_id else [])
        self._show_selection()

    def _selected(self) -> Optional[Element]:
        els = self.preview.elements()
        return els[0] if els else None

    def _add_element(self, type_: str):
        e = default_element(type_)
        self.layout_obj.elements.append(e)
        self._refresh_list()
        self._select_element(e.id)
        self._dirty()

    def _duplicate_element(self):
        els = self.preview.elements()
        if not els:
            return
        copies = []
        for e in els:
            d = e.copy()
            d.name = f"{e.name} copy"
            d.x = min(d.x + 0.02, 1 - d.w)
            d.y = min(d.y + 0.02, 1 - d.h)
            self.layout_obj.elements.append(d)
            copies.append(d.id)
        self._refresh_list()
        self.preview.set_selection(copies)
        self._show_selection()
        self._dirty()

    def _delete_element(self):
        els = self.preview.elements()
        if not els:
            return
        for e in els:
            self.layout_obj.elements.remove(e)
        self.preview.set_selection([])
        self._refresh_list()
        self._show_selection()
        self._dirty()

    def _move_element(self, direction: int):
        e = self._selected()
        if not e:
            return
        els = self.layout_obj.elements
        i = els.index(e)
        j = max(0, min(len(els) - 1, i + direction))
        els[i], els[j] = els[j], els[i]
        self._refresh_list()
        self._dirty()

    def _geometry_edited(self):
        if len(self.preview.selected) == 1:
            self.props.refresh_geometry()
        elif self.preview.selected:
            self.props.show_multi(self.preview.elements(), self._output_pixels())
        self._dirty()

    def _props_changed(self):
        e = self.props.element
        if e:
            for i in range(self.element_list.count()):
                item = self.element_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == e.id:
                    self.element_list.blockSignals(True)
                    item.setText(e.name)
                    item.setCheckState(Qt.CheckState.Checked if e.visible else Qt.CheckState.Unchecked)
                    self.element_list.blockSignals(False)
        self._dirty()

    def _layout_changed(self):
        self._update_layout_label()
        self._dirty()

    def _dirty(self):
        self._save_timer.start(400)
        self._update_tools()

    def _load_font_files(self):
        for path in self.layout_obj.font_files:
            if os.path.exists(path):
                QFontDatabase.addApplicationFont(path)

    # ── files ───────────────────────────────────────────────────────────────────

    def _open_layout(self):
        path = files.open_path(self, "Open layout", "Confidence layout (*.json)", "layouts")
        if not path:
            return
        try:
            self._set_layout(Layout.load(path), path)
        except Exception as e:
            QMessageBox.warning(self, "Open layout", f"Couldn't open {path}:\n{e}")

    def _save_layout(self):
        if not self.layout_path:
            return self._save_layout_as()
        self.layout_path = files.absolute(self.layout_path, "layouts")   # never relative to the app's cwd
        try:
            self.layout_obj.save(self.layout_path)
            self.statusBar().showMessage(f"Saved {self.layout_path}", 4000)
        except Exception as e:
            QMessageBox.warning(self, "Save layout", str(e))
        self._update_layout_label()

    def _save_layout_as(self):
        name = re.sub(r'[/\\:*?"<>|]', "-", self.layout_obj.name or "layout").strip() or "layout"
        path = files.save_path(self, "Save layout", f"{name}.json", "Confidence layout (*.json)",
                               "layouts", suffix=".json")
        if path:
            self.layout_path = path
            self._save_layout()

    # ── settings ────────────────────────────────────────────────────────────────

    def _autosave_layout(self):
        self._commit_history()
        self.settings.setValue("layout/json", self.layout_obj.to_json())
        self.settings.setValue("layout/path", self.layout_path or "")

    def _save_ports(self):
        for kind, key in (("lt", "inputs/limitimer"), ("pc", "inputs/perfectcue")):
            ref = self.refs.get(kind)
            self.settings.setValue(key, json.dumps(ref.to_dict() if ref else None))
        self.settings.setValue("inputs/lt_connected", self.want["lt"])
        self.settings.setValue("inputs/pc_connected", self.want["pc"])

    def _load_settings(self):
        s = self.settings
        geo = s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        for kind, key in (("lt", "inputs/limitimer"), ("pc", "inputs/perfectcue")):
            try:
                self.refs[kind] = portsmod.PortRef.from_dict(json.loads(s.value(key, "null") or "null"))
            except (TypeError, ValueError):
                self.refs[kind] = None
        for kind in ("lt", "pc"):
            # devices that were connected when the app quit reconnect at launch
            self.want[kind] = bool(self.refs[kind]) and s.value(f"inputs/{kind}_connected", False, type=bool)
        self.show_all_ports.setChecked(s.value("inputs/show_all", False, type=bool))
        self.lt_panel.load(s)
        self._refresh_ports(force=True)    # reconnects devices that were connected last time
        if self.emulating and self.limitimer is None:
            self._apply_device("lt")       # the software Limitimer runs without an adapter
        self._sync_connect_buttons()

        text = s.value("layout/json", "")
        if text:
            try:
                self.layout_obj = Layout.from_json(text)
            except Exception:
                pass
        path = s.value("layout/path", "") or None
        self._set_layout(self.layout_obj, path)

        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(s.value("output/mode", "window"))))
        saved = s.value("output/screen", "") or None
        if saved:
            self._wanted_screen = saved
            self._refresh_screens()
        self.osc_target.setText(s.value("osc/target", "127.0.0.1:5000", type=str))
        self.osc_prefix.setText(s.value("osc/prefix", "/dsan", type=str))
        if OSC_AVAILABLE and s.value("osc/enabled", False, type=bool):
            self.osc_enable.setChecked(True)
        self.osc_in_port.setValue(s.value("osc/in_port", 9000, type=int))
        self.mm_port.setValue(s.value("millumin/port", 5001, type=int))
        self.mm_layer.setCurrentText(s.value("millumin/layer", "", type=str) or "Selected layer")
        self.mm_yellow.setValue(s.value("millumin/yellow", 30, type=int))
        self.mm_red.setValue(s.value("millumin/red", 10, type=int))
        if OSC_AVAILABLE and s.value("millumin/enabled", False, type=bool):
            self.mm_enable.setChecked(True)
        self.tc_port.setValue(s.value("trucue/port", 9017, type=int))
        self.tc_forward.setText(s.value("trucue/forward", "", type=str))
        self.tc_yellow.setValue(s.value("trucue/yellow", 30, type=int))
        self.tc_red.setValue(s.value("trucue/red", 10, type=int))
        if OSC_AVAILABLE and s.value("trucue/enabled", False, type=bool):
            self.tc_enable.setChecked(True)
        self.remote_port.setValue(s.value("remote/port", 8080, type=int))
        self.remote_pin.setText(s.value("remote/pin", "", type=str))
        self.remote_name.setText(s.value("remote/name", "", type=str))
        if s.value("remote/enabled", False, type=bool):
            self.remote_enable.setChecked(True)
        if OSC_AVAILABLE and s.value("osc/in_enabled", False, type=bool):
            self.osc_in_enable.setChecked(True)
        if s.value("output/visible", True, type=bool):
            QTimer.singleShot(0, lambda: self.show_output_btn.setChecked(True))
        self.decklink_panel.load(s)
        if self.sparkle is not None:
            self.auto_update_action.setChecked(self.sparkle.automatic)   # Sparkle keeps its own setting
        else:
            self.auto_update_action.setChecked(s.value("updates/auto", True, type=bool))
            if self._auto_update_due():
                QTimer.singleShot(4000, lambda: self.check_for_updates(manual=False))

    # ── updates ─────────────────────────────────────────────────────────────────

    UPDATE_INTERVAL = 24 * 3600

    def _auto_update_toggled(self, on: bool):
        self.settings.setValue("updates/auto", on)
        if self.sparkle is not None:
            self.sparkle.automatic = on

    def _auto_update_due(self) -> bool:
        if not self.auto_update_action.isChecked():
            return False
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or os.environ.get("CONFIDENCE_NO_UPDATE_CHECK"):
            return False                                   # tests, smoke test, headless runs
        last = self.settings.value("updates/last_check", 0.0, type=float)
        return time.time() - last >= self.UPDATE_INTERVAL

    def check_for_updates(self, manual: bool = True):
        """Sparkle's update window in the signed Mac app; elsewhere ask GitHub for the latest release on a
        background thread (the answer arrives on the GUI thread)."""
        if self.sparkle is not None:
            if manual:
                self.sparkle.check_for_updates()
            return
        if self._update_running:
            return
        self._update_running = True
        self.update_action.setEnabled(False)
        from .. import updates

        def work():
            try:
                self.sig.update_result.emit(updates.check(), None, manual)
            except updates.UpdateError as e:
                self.sig.update_result.emit(None, str(e), manual)
            except Exception as e:                         # never let a check crash the app
                self.sig.update_result.emit(None, f"Update check failed: {e}", manual)
        threading.Thread(target=work, name="confidence-update-check", daemon=True).start()

    def _on_update_result(self, info, error, manual: bool):
        self._update_running = False
        self.update_action.setEnabled(True)
        if error:
            if manual:
                QMessageBox.information(self, "Check for Updates", error)
            return
        self.settings.setValue("updates/last_check", time.time())
        self.update_info = info
        if info.available:
            self.version_label.setText(f"v{ver.version_string()}  ·  {info.latest} available")
            self.version_label.setStyleSheet("color: #FFB000;")
            self.version_label.setToolTip(f"Confidence {info.latest} is available — Help → Check for Updates…")
            if manual or self.settings.value("updates/skip", "", type=str) != info.latest:
                self.update_dialog(info).exec()
        elif manual:
            QMessageBox.information(self, "Check for Updates",
                                    f"You're up to date.\n\nConfidence {info.current} is the newest version.")

    def update_dialog(self, info) -> QDialog:
        dlg = QDialog(self)
        dlg.setWindowTitle("Update available")
        dlg.resize(560, 460)
        v = QVBoxLayout(dlg)
        head = QLabel(f"<b>Confidence {info.latest} is available</b> — you have {info.current}."
                      + (f"<br><span style='color:#8A8A8A'>Released {info.published}</span>" if info.published else ""))
        v.addWidget(head)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.notes or "_No release notes._")
        v.addWidget(notes, 1)
        if info.asset is None:
            hint = QLabel("There's no download for this computer in this release — the release page has the files.")
            hint.setObjectName("dim")
            hint.setWordWrap(True)
            v.addWidget(hint)
        row = QHBoxLayout()
        skip = QPushButton("Skip This Version")
        later = QPushButton("Later")
        page = QPushButton("Release Page")
        download = QPushButton(f"Download {info.asset.name}" if info.asset else "Download…")
        download.setDefault(True)
        dlg.download_button, dlg.skip_button = download, skip

        def open_and_close(url):
            QDesktopServices.openUrl(QUrl(url))
            dlg.accept()
        download.clicked.connect(lambda: open_and_close(info.download_url))
        page.clicked.connect(lambda: open_and_close(info.page_url))
        skip.clicked.connect(lambda: (self.settings.setValue("updates/skip", info.latest), dlg.reject()))
        later.clicked.connect(dlg.reject)
        row.addWidget(skip)
        row.addStretch()
        row.addWidget(later)
        row.addWidget(page)
        row.addWidget(download)
        v.addLayout(row)
        return dlg

    def _save_settings(self):
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        self._save_ports()
        self.lt_panel.save(s)
        s.setValue("inputs/show_all", self.show_all_ports.isChecked())
        s.setValue("output/mode", self.mode_combo.currentData())
        s.setValue("output/screen", self._wanted_screen or self.screen_combo.currentData() or "")
        s.setValue("output/visible", self.show_output_btn.isChecked())
        s.setValue("osc/target", self.osc_target.text())
        s.setValue("osc/prefix", self.osc_prefix.text())
        s.setValue("osc/enabled", self.osc_enable.isChecked())
        s.setValue("osc/in_port", self.osc_in_port.value())
        s.setValue("millumin/port", self.mm_port.value())
        s.setValue("millumin/layer", self.mm_layer_name())
        s.setValue("millumin/yellow", self.mm_yellow.value())
        s.setValue("millumin/red", self.mm_red.value())
        s.setValue("millumin/enabled", self.mm_enable.isChecked())
        s.setValue("trucue/port", self.tc_port.value())
        s.setValue("trucue/forward", self.tc_forward.text().strip())
        s.setValue("trucue/yellow", self.tc_yellow.value())
        s.setValue("trucue/red", self.tc_red.value())
        s.setValue("trucue/enabled", self.tc_enable.isChecked())
        s.setValue("remote/port", self.remote_port.value())
        s.setValue("remote/pin", self.remote_pin.text().strip())
        s.setValue("remote/name", self.remote_name.text().strip())
        s.setValue("remote/enabled", self.remote_enable.isChecked())
        s.setValue("osc/in_enabled", self.osc_in_enable.isChecked())
        self.decklink_panel.save(s)
        self._autosave_layout()

    def closeEvent(self, e):
        self._save_settings()
        self.decklink_panel.shutdown()
        self.lt_panel.close_all()
        if self.debug_window is not None:
            self.debug_window.shutdown()
        self._stop_devices()
        if self.mm_rx:
            self.mm_rx.close()
            self.mm_rx = None
        if self.tc_rx:
            self.tc_rx.close()
            self.tc_rx = None
        self._stop_bonjour()
        if self.remote:
            self.remote.close()
            self.remote = None
        if self.osc_in:
            self.osc_in.close()
            self.osc_in = None
        if self.osc:
            self.osc.close()
        self.output.close()
        e.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Confidence")
    app.setApplicationVersion(__version__)
    from pathlib import Path
    from PyQt6.QtGui import QIcon
    icon = Path(__file__).resolve().parents[3] / "packaging" / "icon" / "Confidence.png"   # source runs
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setOrganizationName("TRUCUE")
    w = ControlWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

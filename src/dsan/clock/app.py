"""DSAN Clock — a video clock for a Limitimer and a Perfect Cue on two USB-RS485 adapters.

    pip install 'dsan[clock]'
    dsan-clock

The control window holds the inputs, the output settings, the operator message
and the layout editor. The output window draws the layout full screen, in a
window, or as a click-through overlay on top of slides.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QRectF, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFontDatabase, QGuiApplication, QKeyEvent, QPainter
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from .. import __version__
from .. import perfectcue as pc
from ..devices import Limitimer, LinkState, PerfectCue
from ..limitimer import LimitimerState
from . import ports as portsmod
from .layout import (COMMON_FIELDS, ELEMENT_TYPES, PRESETS, SOURCES, TYPE_FIELDS,
                     Element, Layout, default_element, format_color, parse_color)
from .output import MODES, OutputWindow
from .render import CueState, Frame, Renderer, checkerboard

try:
    from ..osc import OscBridge
    OSC_AVAILABLE = True
except ImportError:  # pragma: no cover
    OSC_AVAILABLE = False

PC_BAUDS = (19200, 9600, 38400, 57600, 115200)
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
QComboBox, QLineEdit, QDoubleSpinBox { background: #202020; border: 1px solid #343434; border-radius: 4px;
                                       padding: 4px 6px; }
QComboBox QAbstractItemView { background: #202020; selection-background-color: #1F3D3A; }
QListWidget { background: #1B1B1B; border: 1px solid #2C2C2C; border-radius: 4px; }
QListWidget::item:selected { background: #1F3D3A; color: #E6FFFB; }
QScrollArea { border: none; }
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

class Signals(QObject):
    """Device callbacks arrive on reader threads; Qt queues these onto the GUI thread."""
    lt_state = pyqtSignal(object)
    lt_link = pyqtSignal(object)
    pc_button = pyqtSignal(object)
    pc_link = pyqtSignal(object)
    error = pyqtSignal(str)
    detect_status = pyqtSignal(str)
    detect_done = pyqtSignal(object)


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


# ── preview / editor canvas ─────────────────────────────────────────────────────

class Preview(QWidget):
    """Live preview of the output that doubles as the layout editor.

    Click to select, drag to move, drag the corner handle to resize,
    arrow keys nudge (Shift = 10×), Delete removes.
    """
    selected = pyqtSignal(str)
    edited = pyqtSignal()
    delete_requested = pyqtSignal()

    SNAP = 0.005

    def __init__(self, layout_source: Callable[[], Layout], frame_source: Callable[[], Frame],
                 aspect_source: Callable[[], float], renderer: Renderer):
        super().__init__()
        self._layout = layout_source
        self._frame = frame_source
        self._aspect = aspect_source
        self.renderer = renderer
        self.selected_id: Optional[str] = None
        self._drag = None
        self.setMinimumSize(QSize(420, 240))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        t = QTimer(self)
        t.timeout.connect(self.update)
        t.start(50)

    def canvas(self) -> QRectF:
        a = self._aspect() or 16 / 9
        w, h = self.width() - 8, self.height() - 8
        if w / h > a:
            cw, ch = h * a, h
        else:
            cw, ch = w, w / a
        return QRectF((self.width() - cw) / 2, (self.height() - ch) / 2, cw, ch)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#111111"))
        c = self.canvas()
        lay = self._layout()
        if self.renderer.qcolor(lay.background).alpha() < 255:
            checkerboard(p, c)
        self.renderer.paint(p, c, lay, self._frame(), editing=True, selected=self.selected_id)
        p.setPen(QColor("#333"))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(c)
        p.end()

    def _norm(self, pos) -> tuple:
        c = self.canvas()
        return (pos.x() - c.left()) / c.width(), (pos.y() - c.top()) / c.height()

    def _snap(self, v: float) -> float:
        return round(v / self.SNAP) * self.SNAP

    def mousePressEvent(self, ev):
        self.setFocus()
        fx, fy = self._norm(ev.position())
        lay = self._layout()
        cur = lay.find(self.selected_id) if self.selected_id else None
        c = self.canvas()
        if cur is not None:
            hx = c.left() + (cur.x + cur.w) * c.width()
            hy = c.top() + (cur.y + cur.h) * c.height()
            if abs(ev.position().x() - hx) < 12 and abs(ev.position().y() - hy) < 12:
                self._drag = ("resize", cur, fx, fy, cur.x, cur.y, cur.w, cur.h)
                return
        hit = lay.hit(fx, fy)
        self.selected_id = hit.id if hit else None
        self.selected.emit(self.selected_id or "")
        if hit:
            self._drag = ("move", hit, fx, fy, hit.x, hit.y, hit.w, hit.h)

    def mouseMoveEvent(self, ev):
        c = self.canvas()
        lay = self._layout()
        cur = lay.find(self.selected_id) if self.selected_id else None
        if self._drag is None:
            near = False
            if cur is not None:
                hx = c.left() + (cur.x + cur.w) * c.width()
                hy = c.top() + (cur.y + cur.h) * c.height()
                near = abs(ev.position().x() - hx) < 12 and abs(ev.position().y() - hy) < 12
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if near else Qt.CursorShape.ArrowCursor)
            return
        kind, el, sx, sy, ox, oy, ow, oh = self._drag
        fx, fy = self._norm(ev.position())
        dx, dy = fx - sx, fy - sy
        if kind == "move":
            el.x = self._snap(min(max(ox + dx, 0.0), 1.0 - el.w))
            el.y = self._snap(min(max(oy + dy, 0.0), 1.0 - el.h))
        else:
            el.w = self._snap(min(max(ow + dx, 0.01), 1.0 - el.x))
            el.h = self._snap(min(max(oh + dy, 0.01), 1.0 - el.y))
        self.edited.emit()

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def keyPressEvent(self, ev: QKeyEvent):
        lay = self._layout()
        el = lay.find(self.selected_id) if self.selected_id else None
        if el is None:
            return super().keyPressEvent(ev)
        step = 0.05 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else self.SNAP
        k = ev.key()
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_requested.emit()
            return
        moves = {Qt.Key.Key_Left: (-step, 0), Qt.Key.Key_Right: (step, 0),
                 Qt.Key.Key_Up: (0, -step), Qt.Key.Key_Down: (0, step)}
        if k in moves:
            dx, dy = moves[k]
            el.x = round(min(max(el.x + dx, 0.0), 1.0 - el.w), 4)
            el.y = round(min(max(el.y + dy, 0.0), 1.0 - el.h), 4)
            self.edited.emit()
            return
        super().keyPressEvent(ev)


# ── properties editor ───────────────────────────────────────────────────────────

_FIELD_LABELS = {
    "name": "Name", "visible": "Visible", "x": "Left", "y": "Top", "w": "Width", "h": "Height",
    "bg": "Background", "radius": "Corner radius", "color": "Colour", "font": "Font", "bold": "Bold",
    "align": "Align", "source": "Program", "overtime": "Count past zero (+00:20)",
    "follow_format": "Follow DIP 2/3 format", "phase_colors": "Colour by phase",
    "orientation": "Orientation", "show_idle": "Green while stopped",
    "cue_style": "Style", "hold": "Hold NEXT/PREV (s)", "clock_24h": "24-hour",
    "clock_seconds": "Show seconds", "text": "Text", "flash": "Flash",
}
_CHOICES = {
    "align": [("left", "Left"), ("center", "Centre"), ("right", "Right")],
    "source": [("selected", "Selected on Limitimer")] + [(n, f"Program {n}") for n in SOURCES[1:]],
    "orientation": [("horizontal", "Horizontal"), ("vertical", "Vertical")],
    "cue_style": [("arrows", "Arrows ◁ ● ▷"), ("light", "Full colour light"), ("text", "Text")],
}
_BOOLS = {"visible", "bold", "overtime", "follow_format", "phase_colors", "show_idle", "clock_24h",
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
            hint = QLabel("Tokens: {program}  {n}  {mode}")
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
            w = QComboBox()
            for key, label in _CHOICES[f]:
                w.addItem(label, key)
            w.setCurrentIndex(max(0, w.findData(v)))
            w.currentIndexChanged.connect(lambda _i, w=w, f=f: self._set(e, f, w.currentData()))
            return w
        if f == "font":
            w = QComboBox()
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
        ns = QComboBox()
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
        path, _ = QFileDialog.getOpenFileName(self, "Add font", "", "Fonts (*.ttf *.otf)")
        if path and path not in lay.font_files:
            lay.font_files.append(path)
            self.fonts_changed.emit()
            self.changed.emit()
            self.show_layout(lay)


# ── main window ─────────────────────────────────────────────────────────────────

class ControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("TRUCUE", "DSANClock")
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
        self._detecting = False
        self._port_infos: Dict[str, portsmod.PortInfo] = {}

        self.layout_obj: Layout = PRESETS["Confidence monitor"]()
        self.layout_path: Optional[str] = None

        self.output = OutputWindow(self.frame, lambda: self.layout_obj, self.renderer)
        self.output.closed.connect(lambda: self.show_output_btn.setChecked(False))

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._autosave_layout)

        self._build()
        self._load_settings()
        QGuiApplication.instance().screenAdded.connect(lambda _s: self._refresh_screens())
        QGuiApplication.instance().screenRemoved.connect(lambda _s: self._refresh_screens())

    # ── data for painting ────────────────────────────────────────────────────────

    def frame(self) -> Frame:
        return Frame(state=self.lt_state, link=self.lt_link, cue=self.cue, message=self.message,
                     now=time.monotonic(), wall=_dt.datetime.now())

    def _aspect(self) -> float:
        g = self._selected_screen().geometry()
        if self.output.isVisible() and self.output.mode == "window":
            g = self.output.geometry()
        return g.width() / max(1, g.height())

    # ── build UI ─────────────────────────────────────────────────────────────────

    def _build(self):
        self.setWindowTitle(f"DSAN Clock {__version__}")
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

        root = QSplitter()
        self.setCentralWidget(root)

        # left: operations
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 4, 8, 8)
        lv.addWidget(self._build_inputs())
        lv.addWidget(self._build_output())
        lv.addWidget(self._build_message())
        lv.addWidget(self._build_osc())
        lv.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(380)
        scroll.setMaximumWidth(460)
        root.addWidget(scroll)

        # right: preview + editor
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 8, 8, 8)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
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
        self.layout_label = QLabel()
        self.layout_label.setObjectName("dim")
        bar.addWidget(self.layout_label)
        rv.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.preview = Preview(lambda: self.layout_obj, self.frame, self._aspect, self.renderer)
        self.preview.selected.connect(self._select_element)
        self.preview.edited.connect(self._geometry_edited)
        self.preview.delete_requested.connect(self._delete_element)
        split.addWidget(self.preview)

        bottom = QWidget()
        bh = QHBoxLayout(bottom)
        bh.setContentsMargins(0, 0, 0, 0)
        listcol = QVBoxLayout()
        self.element_list = QListWidget()
        self.element_list.currentRowChanged.connect(self._list_row_changed)
        self.element_list.itemChanged.connect(self._list_item_changed)
        listcol.addWidget(self.element_list)
        btns = QHBoxLayout()
        add = QPushButton("Add ▾")
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
        listcol.addLayout(btns)
        lw = QWidget()
        lw.setLayout(listcol)
        lw.setMaximumWidth(300)
        bh.addWidget(lw)

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
        bh.addWidget(ps, 1)
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
        form = QFormLayout()
        self.lt_combo = QComboBox()
        self.lt_combo.setEditable(True)
        self.lt_combo.setToolTip("USB adapter wired to the Limitimer data port (19200 baud).\n"
                                 "You can also type socket://host:port for an RS-485-to-Ethernet converter.")
        self.lt_status = QLabel()
        form.addRow("Limitimer", self.lt_combo)
        form.addRow("", self.lt_status)
        self.pc_combo = QComboBox()
        self.pc_combo.setEditable(True)
        self.pc_combo.setToolTip("USB adapter wired to the Perfect Cue data port.")
        self.pc_baud = QComboBox()
        for b in PC_BAUDS:
            self.pc_baud.addItem(str(b), b)
        self.pc_baud.setToolTip("Baud rate of the Perfect Cue data link. If presses don't show up, try 9600.")
        self.pc_status = QLabel()
        form.addRow("Perfect Cue", self.pc_combo)
        form.addRow("Baud", self.pc_baud)
        form.addRow("", self.pc_status)
        v.addLayout(form)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setCheckable(True)
        self.connect_btn.clicked.connect(self._toggle_connect)
        self.detect_btn = QPushButton("Detect")
        self.detect_btn.setToolTip("Listen on every USB adapter and work out which is which")
        self.detect_btn.clicked.connect(self._start_detect)
        swap = QPushButton("Swap")
        swap.setToolTip("Swap the two adapters")
        swap.clicked.connect(self._swap_ports)
        refresh = QPushButton("⟳")
        refresh.setToolTip("Refresh port list")
        refresh.clicked.connect(self._refresh_ports)
        self.demo_btn = QPushButton("Demo")
        self.demo_btn.setCheckable(True)
        self.demo_btn.setToolTip("Simulated Limitimer and Perfect Cue, through the real decoder")
        self.demo_btn.clicked.connect(self._toggle_demo)
        v.addLayout(self._row(self.connect_btn, self.detect_btn, swap, refresh, self.demo_btn))

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
        self.autoconnect = QCheckBox("Connect on launch")
        v.addWidget(self.autoconnect)
        self._set_links()
        return g

    def _build_output(self) -> QGroupBox:
        g = QGroupBox("OUTPUT")
        form = QFormLayout(g)
        self.mode_combo = QComboBox()
        for k, label in MODES.items():
            self.mode_combo.addItem(label, k)
        self.mode_combo.currentIndexChanged.connect(self._output_settings_changed)
        self.screen_combo = QComboBox()
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
        g = QGroupBox("OSC OUT")
        form = QFormLayout(g)
        self.osc_enable = QCheckBox("Send OSC")
        self.osc_enable.setEnabled(OSC_AVAILABLE)
        self.osc_enable.toggled.connect(self._rebuild_osc)
        self.osc_target = QLineEdit("127.0.0.1:5000")
        self.osc_prefix = QLineEdit("/dsan")
        self.osc_status = QLabel("" if OSC_AVAILABLE else "python-osc not installed")
        self.osc_status.setObjectName("dim")
        form.addRow(self.osc_enable)
        form.addRow("Host:port", self.osc_target)
        form.addRow("Prefix", self.osc_prefix)
        form.addRow(self.osc_status)
        return g

    # ── ports ───────────────────────────────────────────────────────────────────

    def _refresh_ports(self):
        infos = portsmod.list_ports()
        self._port_infos = {p.device: p for p in infos}
        for combo in (self.lt_combo, self.pc_combo):
            current = self._combo_port(combo)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— none —", "")
            for p in infos:
                combo.addItem(p.label(), p.device)
            if current:
                i = combo.findData(current)
                if i < 0:
                    combo.addItem(current, current)
                    i = combo.count() - 1
                combo.setCurrentIndex(i)
            combo.blockSignals(False)

    @staticmethod
    def _combo_port(combo: QComboBox) -> str:
        data = combo.currentData()
        text = combo.currentText().strip()
        if data is not None and combo.itemText(combo.currentIndex()) == text:
            return data
        return "" if text.startswith("—") else text

    def _select_port(self, combo: QComboBox, device: Optional[str]):
        if not device:
            combo.setCurrentIndex(0)
            return
        i = combo.findData(device)
        if i < 0:
            combo.addItem(device, device)
            i = combo.count() - 1
        combo.setCurrentIndex(i)

    def _ref_for(self, device: str) -> Optional[dict]:
        if not device:
            return None
        info = self._port_infos.get(device)
        return (portsmod.PortRef.of(info) if info else portsmod.PortRef.url(device)).to_dict()

    def _swap_ports(self):
        a, b = self._combo_port(self.lt_combo), self._combo_port(self.pc_combo)
        self._select_port(self.lt_combo, b)
        self._select_port(self.pc_combo, a)
        if self.connect_btn.isChecked():
            self._disconnect()
            self._connect()

    # ── connect / demo ──────────────────────────────────────────────────────────

    def _toggle_connect(self, on: bool):
        if on:
            if self.demo:
                self._stop_devices()
            if not self._connect():
                self.connect_btn.setChecked(False)
        else:
            self._disconnect()

    def _connect(self) -> bool:
        lt_port = self._combo_port(self.lt_combo)
        pc_port = self._combo_port(self.pc_combo)
        if not lt_port and not pc_port:
            self.statusBar().showMessage("Choose at least one port, or press Detect", 5000)
            return False
        if lt_port and lt_port == pc_port:
            self.statusBar().showMessage("The Limitimer and Perfect Cue can't share one adapter", 6000)
            return False
        self._stop_devices()
        self.demo_btn.setChecked(False)
        if lt_port:
            self._start_limitimer(Limitimer(lt_port))
        if pc_port:
            self._start_perfectcue(PerfectCue(pc_port, baudrate=self.pc_baud.currentData()))
        self.connect_btn.setChecked(True)
        self.connect_btn.setText("Disconnect")
        self._save_ports()
        self._rebuild_osc()
        return True

    def _disconnect(self):
        self._stop_devices()
        self.demo_btn.setChecked(False)
        self.connect_btn.setChecked(False)
        self.connect_btn.setText("Connect")

    def _toggle_demo(self, on: bool):
        self._stop_devices()
        self.connect_btn.setChecked(False)
        self.connect_btn.setText("Connect")
        self.demo_btn.setChecked(on)
        if on:
            self.demo = True
            self._start_limitimer(Limitimer.simulated())
            self._start_perfectcue(PerfectCue.simulated(interval=2.5))
            self.statusBar().showMessage("Demo: simulated Limitimer and Perfect Cue", 5000)
        self._rebuild_osc()

    def _start_limitimer(self, dev: Limitimer):
        self.limitimer = dev
        dev.on_state(self.sig.lt_state.emit)
        dev.on_link(self.sig.lt_link.emit)
        dev.on_error(lambda e: self.sig.error.emit(f"Limitimer: {e}"))
        dev.start()

    def _start_perfectcue(self, dev: PerfectCue):
        self.perfectcue = dev
        dev.on_button(self.sig.pc_button.emit)
        dev.on_link(self.sig.pc_link.emit)
        dev.on_error(lambda e: self.sig.error.emit(f"Perfect Cue: {e}"))
        dev.start()

    def _stop_devices(self):
        for attr in ("limitimer", "perfectcue"):
            dev = getattr(self, attr)
            setattr(self, attr, None)
            if dev:
                dev.stop()
        self.demo = False
        self.lt_state = None
        self.lt_link = LinkState.CLOSED
        self.pc_link_state = LinkState.CLOSED
        self.cue = CueState()
        self._set_links()
        self._rebuild_osc()

    # ── device events (GUI thread) ───────────────────────────────────────────────

    def _on_lt_state(self, state):
        if self.limitimer is not None:
            self.lt_state = state

    def _on_lt_link(self, link):
        if self.limitimer is None and link != LinkState.CLOSED:
            return
        self.lt_link = link if self.limitimer else LinkState.CLOSED
        self._set_links()

    def _on_pc_link(self, link):
        if self.perfectcue is None and link != LinkState.CLOSED:
            return
        self.pc_link_state = link if self.perfectcue else LinkState.CLOSED
        self._set_links()

    def _on_pc_button(self, button, test: bool = False):
        if not test and self.perfectcue is None:
            return
        self.cue.press(button)
        if test and self.osc:
            self.osc._on_button(button)

    def _set_links(self):
        for label, link, demo in ((self.lt_status, self.lt_link, self.demo),
                                  (self.pc_status, self.pc_link_state, self.demo)):
            text, color = _LINK_TEXT.get(link, (link.value, "#888"))
            if demo and link in (LinkState.LIVE, LinkState.OPEN):
                text = "● demo"
            label.setText(text)
            label.setStyleSheet(f"color: {color};")
        if self.lt_link == LinkState.STALE:
            self.statusBar().showMessage("Limitimer data stopped — the output is showing NO SIGNAL")

    # ── detection ───────────────────────────────────────────────────────────────

    def _start_detect(self):
        if self._detecting:
            return
        self._disconnect()
        self._stop_devices()
        self._detecting = True
        self.detect_btn.setEnabled(False)
        self.connect_btn.setEnabled(False)
        self.detect_label.show()
        self.detect_label.setText("Listening…")
        baud = self.pc_baud.currentData()

        def work():
            try:
                res = portsmod.detect(pc_baud=baud, on_status=self.sig.detect_status.emit)
            except Exception as e:  # pragma: no cover
                res = portsmod.DetectResult(notes=[f"Detect failed: {e}"])
            self.sig.detect_done.emit(res)

        threading.Thread(target=work, daemon=True).start()

    def _detect_note(self, text: str):
        self.detect_label.setText(text)

    def _detect_finished(self, res: portsmod.DetectResult):
        self._detecting = False
        self.detect_btn.setEnabled(True)
        self.connect_btn.setEnabled(True)
        self._refresh_ports()
        lines = []
        if res.limitimer:
            self._select_port(self.lt_combo, res.limitimer.device)
            n = res.probes.get(res.limitimer.device)
            lines.append(f"Limitimer → {res.limitimer.device}"
                         + (f" ({n.lt_packets} packets)" if n else ""))
        if res.perfectcue:
            self._select_port(self.pc_combo, res.perfectcue.device)
            lines.append(f"Perfect Cue → {res.perfectcue.device}")
        lines += res.notes
        self.detect_label.setText("\n".join(lines) or "Nothing found.")
        if res.limitimer or res.perfectcue:
            self._connect()

    # ── output ──────────────────────────────────────────────────────────────────

    def _refresh_screens(self):
        current = self.screen_combo.currentData()
        self.screen_combo.blockSignals(True)
        self.screen_combo.clear()
        for i, s in enumerate(QGuiApplication.screens()):
            g = s.geometry()
            primary = " (main)" if s == QGuiApplication.primaryScreen() else ""
            self.screen_combo.addItem(f"{i + 1}: {s.name()} {g.width()}×{g.height()}{primary}", s.name())
        if current:
            i = self.screen_combo.findData(current)
            if i >= 0:
                self.screen_combo.setCurrentIndex(i)
        self.screen_combo.blockSignals(False)

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
        if self.show_output_btn.isChecked():
            self._apply_output()

    def _apply_output(self):
        mode = self.mode_combo.currentData()
        screen = self.screen_combo.currentData()
        if mode == "fullscreen" and self._selected_screen() == self.screen():
            if len(QGuiApplication.screens()) > 1:
                self.output_note.setText("Full screen on the same display as this window — "
                                         "press Esc on the output to leave.")
        self.output.apply(mode, screen)
        self.activateWindow()
        notes = {
            "window": "Resizable window. Double-click it (or press F) for full screen.",
            "fullscreen": "Press Esc on the output to leave full screen.",
            "overlay": "Transparent and click-through. Use a layout with a transparent background "
                       "(e.g. Corner overlay).",
        }
        note = notes.get(mode, "")
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

    def _rebuild_osc(self, *_):
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
        self.preview.selected_id = None
        self._refresh_list()
        self._select_element("")
        self._update_layout_label()
        self._autosave_layout()

    def _update_layout_label(self):
        where = os.path.basename(self.layout_path) if self.layout_path else "not saved to a file"
        self.layout_label.setText(f"{self.layout_obj.name} — {where}")

    def _load_preset(self, index: int):
        name = self.preset_combo.itemData(index)
        self.preset_combo.setCurrentIndex(0)
        if not name:
            return
        self._set_layout(PRESETS[name]())
        if name == "Corner overlay" and self.mode_combo.currentData() != "overlay":
            self.statusBar().showMessage("Corner overlay has a transparent background — "
                                         "set Output mode to Overlay to put it over slides", 8000)

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
        self.element_list.blockSignals(True)
        self.element_list.setCurrentRow(-1)
        for i in range(self.element_list.count()):
            if self.element_list.item(i).data(Qt.ItemDataRole.UserRole) == self.preview.selected_id:
                self.element_list.setCurrentRow(i)
        self.element_list.blockSignals(False)

    def _list_row_changed(self, row: int):
        if row < 0:
            return
        self._select_element(self.element_list.item(row).data(Qt.ItemDataRole.UserRole))

    def _list_item_changed(self, item: QListWidgetItem):
        e = self.layout_obj.find(item.data(Qt.ItemDataRole.UserRole))
        if e:
            e.visible = item.checkState() == Qt.CheckState.Checked
            if self.props.element is e:
                self.props.show_element(e)
            self._dirty()

    def _select_element(self, element_id: str):
        self.preview.selected_id = element_id or None
        e = self.layout_obj.find(element_id) if element_id else None
        self.props.setVisible(e is not None)
        self.layout_props.setVisible(e is None)
        self.props.show_element(e)
        if e is None:
            self.layout_props.show_layout(self.layout_obj)
        self._sync_list_selection()

    def _selected(self) -> Optional[Element]:
        return self.layout_obj.find(self.preview.selected_id) if self.preview.selected_id else None

    def _add_element(self, type_: str):
        e = default_element(type_)
        self.layout_obj.elements.append(e)
        self._refresh_list()
        self._select_element(e.id)
        self._dirty()

    def _duplicate_element(self):
        e = self._selected()
        if not e:
            return
        d = e.copy()
        d.name = f"{e.name} copy"
        d.x = min(d.x + 0.02, 1 - d.w)
        d.y = min(d.y + 0.02, 1 - d.h)
        self.layout_obj.elements.append(d)
        self._refresh_list()
        self._select_element(d.id)
        self._dirty()

    def _delete_element(self):
        e = self._selected()
        if not e:
            return
        self.layout_obj.elements.remove(e)
        self._refresh_list()
        self._select_element("")
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
        self.props.refresh_geometry()
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

    def _load_font_files(self):
        for path in self.layout_obj.font_files:
            if os.path.exists(path):
                QFontDatabase.addApplicationFont(path)

    # ── files ───────────────────────────────────────────────────────────────────

    def _open_layout(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open layout", "", "DSAN Clock layout (*.json)")
        if not path:
            return
        try:
            self._set_layout(Layout.load(path), path)
        except Exception as e:
            QMessageBox.warning(self, "Open layout", f"Couldn't open {path}:\n{e}")

    def _save_layout(self):
        if not self.layout_path:
            return self._save_layout_as()
        try:
            self.layout_obj.save(self.layout_path)
            self.statusBar().showMessage(f"Saved {self.layout_path}", 4000)
        except Exception as e:
            QMessageBox.warning(self, "Save layout", str(e))
        self._update_layout_label()

    def _save_layout_as(self):
        name = (self.layout_obj.name or "layout").replace("/", "-")
        path, _ = QFileDialog.getSaveFileName(self, "Save layout", f"{name}.json", "DSAN Clock layout (*.json)")
        if path:
            self.layout_path = path
            self._save_layout()

    # ── settings ────────────────────────────────────────────────────────────────

    def _autosave_layout(self):
        self.settings.setValue("layout/json", self.layout_obj.to_json())
        self.settings.setValue("layout/path", self.layout_path or "")

    def _save_ports(self):
        self.settings.setValue("inputs/limitimer", json.dumps(self._ref_for(self._combo_port(self.lt_combo))))
        self.settings.setValue("inputs/perfectcue", json.dumps(self._ref_for(self._combo_port(self.pc_combo))))
        self.settings.setValue("inputs/pc_baud", self.pc_baud.currentData())

    def _load_settings(self):
        s = self.settings
        geo = s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._refresh_ports()
        infos = list(self._port_infos.values())
        for key, combo in (("inputs/limitimer", self.lt_combo), ("inputs/perfectcue", self.pc_combo)):
            try:
                ref = portsmod.PortRef.from_dict(json.loads(s.value(key, "null") or "null"))
            except (TypeError, ValueError):
                ref = None
            if ref:
                self._select_port(combo, ref.resolve(infos) or "")
        i = self.pc_baud.findData(s.value("inputs/pc_baud", 19200, type=int))
        self.pc_baud.setCurrentIndex(max(0, i))
        self.autoconnect.setChecked(s.value("inputs/autoconnect", False, type=bool))

        text = s.value("layout/json", "")
        if text:
            try:
                self.layout_obj = Layout.from_json(text)
            except Exception:
                pass
        path = s.value("layout/path", "") or None
        self._set_layout(self.layout_obj, path)

        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(s.value("output/mode", "window"))))
        i = self.screen_combo.findData(s.value("output/screen", ""))
        if i >= 0:
            self.screen_combo.setCurrentIndex(i)
        self.osc_target.setText(s.value("osc/target", "127.0.0.1:5000", type=str))
        self.osc_prefix.setText(s.value("osc/prefix", "/dsan", type=str))
        if OSC_AVAILABLE and s.value("osc/enabled", False, type=bool):
            self.osc_enable.setChecked(True)
        if self.autoconnect.isChecked() and (self._combo_port(self.lt_combo) or self._combo_port(self.pc_combo)):
            QTimer.singleShot(0, self._connect)
        if s.value("output/visible", True, type=bool):
            QTimer.singleShot(0, lambda: self.show_output_btn.setChecked(True))

    def _save_settings(self):
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        if not self.demo:
            self._save_ports()
        s.setValue("inputs/autoconnect", self.autoconnect.isChecked())
        s.setValue("output/mode", self.mode_combo.currentData())
        s.setValue("output/screen", self.screen_combo.currentData() or "")
        s.setValue("output/visible", self.show_output_btn.isChecked())
        s.setValue("osc/target", self.osc_target.text())
        s.setValue("osc/prefix", self.osc_prefix.text())
        s.setValue("osc/enabled", self.osc_enable.isChecked())
        self._autosave_layout()

    def closeEvent(self, e):
        self._save_settings()
        self._stop_devices()
        if self.osc:
            self.osc.close()
        self.output.close()
        e.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("DSAN Clock")
    app.setOrganizationName("TRUCUE")
    w = ControlWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

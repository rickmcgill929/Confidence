"""Clock layouts: the elements drawn on the output, their settings, presets, and JSON files.

Pure Python (no Qt), so layouts can be built, tested and converted anywhere.

Geometry is normalised: ``x, y, w, h`` are fractions (0-1) of the output, so one
layout works at any resolution. Colours are CSS-style ``#RRGGBB`` or
``#RRGGBBAA`` strings (alpha last).
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

from ..limitimer import LimitimerState, Program
from ..millumin import MilluminView, format_time

# Element types and what each one shows.
ELEMENT_TYPES: Dict[str, str] = {
    "timer": "Timer digits",
    "lights": "Phase lights (green / yellow / red)",
    "cue": "Perfect Cue indicator",
    "progress": "Progress bar",
    "clock": "Time of day",
    "label": "Label",
    "message": "Operator message",
}

# Which per-type fields the editor should show (common fields are always shown).
TYPE_FIELDS: Dict[str, List[str]] = {
    "timer": ["source", "mm_show", "overtime", "follow_format", "phase_colors", "follow_blink", "color", "font", "bold",
              "align"],
    "lights": ["source", "orientation", "show_idle", "follow_blink"],
    "cue": ["cue_style", "hold", "frame_px"],
    "progress": ["source", "bar_direction", "bar_fill", "phase_colors", "color"],
    "clock": ["clock_24h", "clock_seconds", "color", "font", "bold", "align"],
    "label": ["text", "source", "color", "font", "bold", "align"],
    "message": ["flash", "color", "font", "bold", "align"],
}
COMMON_FIELDS = ["name", "visible", "x", "y", "w", "h", "bg", "radius"]

SOURCES = ["selected", "1", "2", "3", "4", "millumin"]
NO_SIGNAL_MODES = ["dim", "text", "hide", "ignore"]


@dataclass
class Element:
    type: str
    x: float = 0.1
    y: float = 0.1
    w: float = 0.3
    h: float = 0.2
    name: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    visible: bool = True
    # appearance
    color: str = "#FFFFFF"
    bg: str = "#00000000"
    radius: float = 0.0            # corner radius as a fraction of the element's height
    font: str = ""                 # family; empty = a clean sans with tabular digits
    bold: bool = True
    align: str = "center"          # left | center | right
    # timer / lights / progress / label
    source: str = "selected"       # "selected" follows the Limitimer, "1".."4", or "millumin"
    mm_show: str = "remaining"     # Millumin timer: remaining | elapsed | duration
    overtime: bool = True
    follow_format: bool = False
    phase_colors: bool = True      # colour by running / warning / expired
    follow_blink: bool = False     # flash when the Limitimer's blink flag is set
    # lights
    orientation: str = "horizontal"
    show_idle: bool = False        # light green while stopped with time left
    # progress bar
    bar_direction: str = "ltr"     # ltr | rtl | btt | ttb — the edge the bar grows from
    bar_fill: str = "auto"         # auto | remaining | elapsed
    # cue
    cue_style: str = "arrows"      # arrows | light | text | frame
    frame_px: int = 20             # frame style: outline width in output pixels
    hold: float = 1.5              # seconds a NEXT/PREV press stays on screen
    # clock
    clock_24h: bool = True
    clock_seconds: bool = True
    # label / message
    text: str = ""
    flash: bool = False

    def __post_init__(self):
        if self.type not in ELEMENT_TYPES:
            raise ValueError(f"unknown element type {self.type!r}")
        if not self.name:
            self.name = ELEMENT_TYPES[self.type].split(" (")[0]

    def copy(self) -> "Element":
        e = copy.deepcopy(self)
        e.id = uuid.uuid4().hex[:8]
        return e

    def contains(self, fx: float, fy: float) -> bool:
        return self.x <= fx <= self.x + self.w and self.y <= fy <= self.y + self.h


DEFAULT_COLORS = {
    "running": "#00E07A",
    "idle": "#4A8CFF",
    "warning": "#FFB000",
    "expired": "#FF3B3B",
    "next": "#00E07A",
    "prev": "#FF3B3B",
    "blank": "#FFD000",
    "dim": "#242424",
}


@dataclass
class Layout:
    name: str = "Untitled"
    background: str = "#000000"
    elements: List[Element] = field(default_factory=list)
    colors: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    no_signal: str = "dim"          # what timers do when the Limitimer link isn't live
    font_files: List[str] = field(default_factory=list)   # extra .ttf/.otf files to load
    version: int = 1

    # -- lookup ------------------------------------------------------------------

    def find(self, element_id: str) -> Optional[Element]:
        return next((e for e in self.elements if e.id == element_id), None)

    def first(self, type_: str) -> Optional[Element]:
        return next((e for e in self.elements if e.type == type_), None)

    def hit(self, fx: float, fy: float) -> Optional[Element]:
        """Topmost visible element under a normalised point (last drawn = on top)."""
        for e in reversed(self.elements):
            if e.visible and e.contains(fx, fy):
                return e
        return None

    def color(self, key: str) -> str:
        return self.colors.get(key) or DEFAULT_COLORS[key]

    # -- JSON ----------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Layout":
        known = {f.name for f in fields(Element)}
        elements = [Element(**{k: v for k, v in e.items() if k in known}) for e in d.get("elements", [])]
        colors = dict(DEFAULT_COLORS)
        colors.update(d.get("colors") or {})
        no_signal = d.get("no_signal", "dim")
        return cls(
            name=d.get("name", "Untitled"),
            background=d.get("background", "#000000"),
            elements=elements,
            colors=colors,
            no_signal=no_signal if no_signal in NO_SIGNAL_MODES else "dim",
            font_files=list(d.get("font_files") or []),
            version=int(d.get("version", 1)),
        )

    @classmethod
    def from_json(cls, text: str) -> "Layout":
        return cls.from_dict(json.loads(text))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Layout":
        with open(path, encoding="utf-8") as f:
            return cls.from_json(f.read())

    def copy(self) -> "Layout":
        return Layout.from_dict(self.to_dict())


# -- colour strings ----------------------------------------------------------------

def parse_color(text: str) -> tuple:
    """'#RGB', '#RRGGBB' or '#RRGGBBAA' → (r, g, b, a) ints. Raises ValueError."""
    s = text.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 6:
        s += "FF"
    if len(s) != 8:
        raise ValueError(f"bad colour {text!r}")
    v = int(s, 16)
    return (v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255


def format_color(r: int, g: int, b: int, a: int = 255) -> str:
    return f"#{r:02X}{g:02X}{b:02X}" + ("" if a == 255 else f"{a:02X}")


# -- state helpers used by the renderer ---------------------------------------------

def resolve_program(state: Optional[LimitimerState], source: str) -> Optional[Program]:
    if state is None:
        return None
    if source in ("1", "2", "3", "4"):
        return state.program(int(source))
    return state.selected


def phase(program: Optional[Program]) -> str:
    """'expired' | 'warning' | 'running' | 'idle' — what the Limitimer's lights would show."""
    if program is None:
        return "idle"
    if program.expired:
        return "expired"
    if program.warning:
        return "warning"
    if program.running:
        return "running"
    return "idle"


def millumin_phase(view: Optional[MilluminView], yellow: float = 30.0, red: float = 10.0) -> str:
    """Lights / colours for a Millumin clip, like Millumin's own time script: green while
    playing, yellow in the last ``yellow`` seconds, red in the last ``red`` seconds and when
    frozen on the last frame. A looping clip stays green; paused / stopped are idle."""
    if view is None:
        return "idle"
    if view.mode == "freeze":
        return "expired"
    if view.state != "playing":
        return "idle"
    rem = view.remaining
    if view.mode != "loop" and rem is not None:
        if rem <= red:
            return "expired"
        if rem <= yellow:
            return "warning"
    return "running"


def millumin_tokens(view: Optional[MilluminView]) -> Dict[str, str]:
    if view is None:                     # nothing on the layer: every token is empty
        return {k: "" for k in ("clip", "layer", "column", "state", "playmode", "trt", "remaining", "elapsed")}
    return {"clip": view.clip, "layer": view.layer, "column": view.column, "state": view.status,
            "playmode": {"loop": "LOOP", "freeze": "FREEZE"}.get(view.mode, ""),
            "trt": format_time(view.duration), "remaining": format_time(view.remaining, round_up=True),
            "elapsed": format_time(view.elapsed)}


def expand_tokens(text: str, state: Optional[LimitimerState], program: Optional[Program],
                  millumin: Optional[MilluminView] = None) -> str:
    """Label tokens: {program} → 'PROGRAM 2', {n} → '2', {mode} → 'COUNTDOWN' / 'COUNT UP'.

    Millumin: {clip} {layer} {column} {trt} {remaining} {elapsed} {state} (PLAYING / PAUSED /
    FREEZE) and {playmode} (LOOP / FREEZE / empty). All empty when nothing is on the layer.
    """
    n = str(program.number) if program else "-"
    mode = "" if state is None else ("COUNTDOWN" if state.count_down else "COUNT UP")
    text = (text.replace("{program}", f"PROGRAM {n}")
                .replace("{n}", n)
                .replace("{mode}", mode))
    if "{" in text:
        for k, v in millumin_tokens(millumin).items():
            text = text.replace("{" + k + "}", v)
    return text


# -- presets ------------------------------------------------------------------------

def _e(type_, x, y, w, h, **kw) -> Element:
    return Element(type_, x, y, w, h, **kw)


def preset_confidence() -> Layout:
    """Full-screen confidence monitor: huge digits, lights, cue, clock, message strip."""
    return Layout("Confidence monitor", "#000000", [
        _e("label", 0.05, 0.04, 0.6, 0.08, text="{program}", color="#8A8A8A", align="left"),
        _e("clock", 0.65, 0.04, 0.30, 0.08, color="#8A8A8A", align="right"),
        _e("timer", 0.03, 0.14, 0.94, 0.52),
        _e("progress", 0.05, 0.68, 0.90, 0.025),
        _e("lights", 0.33, 0.73, 0.34, 0.11),
        _e("cue", 0.80, 0.73, 0.15, 0.11),
        _e("message", 0.05, 0.87, 0.90, 0.10, color="#FFFFFF", bg="#C00000FF", radius=0.15, flash=True),
    ])


def preset_corner_overlay() -> Layout:
    """Transparent overlay: a small timer box in the bottom-right corner over slides."""
    return Layout("Corner overlay", "#00000000", [
        _e("timer", 0.755, 0.845, 0.225, 0.12, bg="#000000C0", radius=0.18),
        _e("lights", 0.722, 0.855, 0.026, 0.10, orientation="vertical"),
        _e("cue", 0.905, 0.765, 0.075, 0.07, bg="#000000C0", radius=0.2),
        _e("message", 0.02, 0.02, 0.96, 0.08, color="#FFFFFF", bg="#C00000E0", radius=0.2, flash=True),
    ], no_signal="text")


def preset_cue_light() -> Layout:
    """The whole screen is a cue light: green on NEXT, red on PREV, yellow while blanked."""
    return Layout("Cue light", "#000000", [
        _e("cue", 0.0, 0.0, 1.0, 0.82, cue_style="light", hold=1.0),
        _e("timer", 0.25, 0.83, 0.5, 0.16),
    ])


def preset_cue_frame() -> Layout:
    """The timer with a frame round the whole screen that flashes green on NEXT, red on PREV."""
    return Layout("Cue frame", "#000000", [
        _e("label", 0.08, 0.10, 0.84, 0.09, text="{program}", color="#8A8A8A"),
        _e("timer", 0.06, 0.20, 0.88, 0.50),
        _e("lights", 0.36, 0.74, 0.28, 0.09),
        _e("message", 0.10, 0.85, 0.80, 0.08, color="#FFFFFF", bg="#C00000FF", radius=0.15, flash=True),
        _e("cue", 0.0, 0.0, 1.0, 1.0, cue_style="frame", frame_px=40, hold=1.5, name="Cue frame"),
    ])


def preset_timer_clock() -> Layout:
    """Side by side: the Limitimer and time of day, for a green room or FOH."""
    return Layout("Timer + time of day", "#000000", [
        _e("label", 0.03, 0.18, 0.46, 0.08, text="{program}", color="#8A8A8A"),
        _e("timer", 0.02, 0.28, 0.48, 0.34),
        _e("lights", 0.13, 0.66, 0.26, 0.09),
        _e("label", 0.51, 0.18, 0.46, 0.08, text="TIME OF DAY", color="#8A8A8A", name="Clock label"),
        _e("clock", 0.51, 0.28, 0.47, 0.34, color="#FFFFFF", clock_seconds=True),
        _e("message", 0.05, 0.84, 0.90, 0.11, color="#FFFFFF", bg="#C00000FF", radius=0.15, flash=True),
    ])


def preset_four_programs() -> Layout:
    """All four Limitimer programs at once."""
    els = []
    for i in range(4):
        cx, cy = 0.02 + (i % 2) * 0.49, 0.03 + (i // 2) * 0.48
        n = str(i + 1)
        els += [
            _e("label", cx, cy, 0.47, 0.07, text=f"PROGRAM {n}", source=n, color="#8A8A8A", name=f"P{n} label"),
            _e("timer", cx + 0.025, cy + 0.08, 0.42, 0.28, source=n, name=f"P{n} timer"),
            _e("lights", cx + 0.135, cy + 0.37, 0.20, 0.06, source=n, name=f"P{n} lights"),
        ]
    return Layout("All four programs", "#000000", els)


def preset_millumin() -> Layout:
    """Millumin playback: clip name, a big countdown, TRT, a progress bar and the play state."""
    return Layout("Millumin countdown", "#000000", [
        _e("label", 0.05, 0.05, 0.70, 0.09, text="{clip}", color="#FFFFFF", align="left", name="Clip"),
        _e("label", 0.75, 0.05, 0.20, 0.09, text="{trt}", color="#8A8A8A", align="right", name="TRT"),
        _e("timer", 0.03, 0.17, 0.94, 0.48, source="millumin", mm_show="remaining", name="Remaining"),
        _e("progress", 0.05, 0.69, 0.90, 0.035, source="millumin", bar_fill="elapsed", name="Progress"),
        _e("label", 0.05, 0.76, 0.45, 0.08, text="{state}", color="#8A8A8A", align="left", name="State"),
        _e("label", 0.50, 0.76, 0.45, 0.08, text="{playmode}", color="#FFB000", align="right", name="Loop / freeze"),
        _e("message", 0.05, 0.87, 0.90, 0.10, color="#FFFFFF", bg="#C00000FF", radius=0.15, flash=True),
    ])


PRESETS = {
    "Confidence monitor": preset_confidence,
    "Corner overlay": preset_corner_overlay,
    "Cue light": preset_cue_light,
    "Cue frame": preset_cue_frame,
    "Timer + time of day": preset_timer_clock,
    "All four programs": preset_four_programs,
    "Millumin countdown": preset_millumin,
}


def default_element(type_: str) -> Element:
    """A sensible new element of a type, centred on the output."""
    sizes = {"timer": (0.5, 0.3), "lights": (0.3, 0.1), "cue": (0.15, 0.12), "progress": (0.6, 0.03),
             "clock": (0.3, 0.1), "label": (0.4, 0.08), "message": (0.8, 0.1)}
    w, h = sizes[type_]
    extra: Dict[str, Any] = {}
    if type_ == "label":
        extra["text"] = "{program}"
    if type_ == "message":
        extra.update(bg="#C00000FF", radius=0.15, flash=True)
    return Element(type_, round(0.5 - w / 2, 3), round(0.5 - h / 2, 3), w, h, **extra)


__all__ = [
    "COMMON_FIELDS", "DEFAULT_COLORS", "ELEMENT_TYPES", "Element", "Layout", "NO_SIGNAL_MODES",
    "PRESETS", "SOURCES", "TYPE_FIELDS", "default_element", "expand_tokens", "format_color",
    "millumin_phase", "millumin_tokens",
    "parse_color", "phase", "resolve_program",
]

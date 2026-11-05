"""Draws a Layout with QPainter. Shared by the output window and the editor preview,
so the preview is exactly what goes out."""
from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QFontMetricsF, QPainter,
                         QPainterPath, QPen, QPolygonF)

from ..devices import LinkState
from ..limitimer import LimitimerState
from ..perfectcue import Button
from ..millumin import MilluminView, format_time
from .layout import Element, Layout, expand_tokens, millumin_phase, parse_color, phase, resolve_program


@dataclass
class CueState:
    """What the Perfect Cue indicator should show."""
    last: Optional[Button] = None
    last_time: float = 0.0      # time.monotonic() of the last NEXT/PREV
    blank: bool = False

    def press(self, button: Button, now: Optional[float] = None) -> None:
        if button == Button.BLANK_ON:
            self.blank = True
        elif button == Button.BLANK_OFF:
            self.blank = False
        else:
            self.last = button
            self.last_time = time.monotonic() if now is None else now

    def active(self, hold: float, now: float) -> Optional[str]:
        """'next' | 'prev' | 'blank' | None."""
        if self.last is not None and now - self.last_time <= hold:
            return "next" if self.last == Button.NEXT else "prev"
        if self.blank:
            return "blank"
        return None


@dataclass
class Frame:
    """Everything that changes from one painted frame to the next."""
    state: Optional[LimitimerState] = None
    link: LinkState = LinkState.CLOSED
    cue: CueState = field(default_factory=CueState)
    message: str = ""
    millumin: Optional[MilluminView] = None
    mm_yellow: float = 30.0          # Millumin: seconds left that turn timers yellow …
    mm_red: float = 10.0             # … and red
    now: float = field(default_factory=time.monotonic)
    wall: _dt.datetime = field(default_factory=_dt.datetime.now)

    @property
    def live(self) -> bool:
        return self.link == LinkState.LIVE


_SELECT = QColor("#2EC4B6")
_GUIDE = QColor(255, 255, 255, 60)
_NO_SIGNAL_DIM = QColor("#3A3A3A")


# Default clock face: clean sans fonts with tabular (equal-width) digits and plain
# zeros. The system monospace fonts tend to dot or slash the zero.
_FACE_PREFERENCE = ("SF Pro Display", "Helvetica Neue", "Segoe UI", "Arial", "Helvetica",
                    "Inter", "Roboto", "Liberation Sans", "DejaVu Sans")


def _default_family() -> str:
    available = set(QFontDatabase.families())
    for fam in _FACE_PREFERENCE:
        if fam in available:
            return fam
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


class Renderer:
    def __init__(self):
        self._colors: Dict[str, QColor] = {}
        self._fonts: Dict[tuple, Tuple[QFont, float, float, float]] = {}
        self._mono: Optional[str] = None

    # -- helpers -----------------------------------------------------------------

    def qcolor(self, text: str) -> QColor:
        c = self._colors.get(text)
        if c is None:
            try:
                c = QColor(*parse_color(text))
            except ValueError:
                c = QColor("#FF00FF")
            self._colors[text] = c
        return c

    def _family(self, e: Element) -> str:
        if e.font:
            return e.font
        if self._mono is None:
            self._mono = _default_family()
        return self._mono

    _DIGITS = "0123456789"

    def _fit(self, family: str, bold: bool, text: str, w: float, h: float, digits: bool):
        """Largest font for which `text` fits w×h. Returns (font, ink_top, ink_h, cell).

        In digit mode every digit gets the same cell width (the widest digit), so a
        running clock never changes size or shifts sideways, even in fonts whose
        digits aren't tabular, and the height is fitted to the digits' ink so the
        numerals fill the box. Other text is fitted to ascent+descent; cell is None.
        """
        pattern = "".join("0" if c.isdigit() else c for c in text) if digits else text
        key = (family, bold, pattern, int(w), int(h), digits)
        hit = self._fonts.get(key)
        if hit:
            return hit
        f = QFont(family)
        f.setBold(bold)
        f.setPixelSize(100)
        width, top, ih, _ = self._measure(f, pattern, digits)
        px = max(4.0, min(w / max(1.0, width), h / max(1.0, ih)) * 100)
        f.setPixelSize(int(px))
        _, top, ih, cell = self._measure(f, pattern, digits)
        result = (f, top, ih, cell)
        if len(self._fonts) > 512:
            self._fonts.clear()
        self._fonts[key] = result
        return result

    def _measure(self, f: QFont, text: str, digits: bool):
        fm = QFontMetricsF(f)
        if digits:
            cell = max(fm.horizontalAdvance(d) for d in self._DIGITS)
            width = sum(cell if c.isdigit() else fm.horizontalAdvance(c) for c in text)
            ink = fm.tightBoundingRect(self._DIGITS)
            return width, ink.top(), ink.height(), cell
        return fm.horizontalAdvance(text or " "), -fm.ascent(), fm.ascent() + fm.descent(), None

    def _text(self, p: QPainter, rect: QRectF, text: str, e: Element, color: QColor,
              digits: bool = True) -> None:
        if not text:
            return
        pad = min(rect.width(), rect.height()) * (0.10 if self.qcolor(e.bg).alpha() else 0.03)
        r = rect.adjusted(pad, pad, -pad, -pad)
        if r.width() < 2 or r.height() < 2:
            return
        f, top, ih, cell = self._fit(self._family(e), e.bold, text, r.width(), r.height(), digits)
        fm = QFontMetricsF(f)
        if cell is None:
            width = fm.horizontalAdvance(text)
        else:
            width = sum(cell if c.isdigit() else fm.horizontalAdvance(c) for c in text)
        if e.align == "left":
            x = r.left()
        elif e.align == "right":
            x = r.right() - width
        else:
            x = r.center().x() - width / 2
        y = r.center().y() - (top + ih / 2)
        p.setFont(f)
        p.setPen(color)
        if cell is None:
            p.drawText(QPointF(x, y), text)
            return
        for c in text:
            adv = fm.horizontalAdvance(c)
            if c.isdigit():
                p.drawText(QPointF(x + (cell - adv) / 2, y), c)
                x += cell
            else:
                p.drawText(QPointF(x, y), c)
                x += adv

    def _bg(self, p: QPainter, rect: QRectF, e: Element) -> None:
        c = self.qcolor(e.bg)
        if c.alpha() == 0:
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        rad = e.radius * rect.height()
        if rad > 0:
            p.drawRoundedRect(rect, rad, rad)
        else:
            p.drawRect(rect)

    # -- entry point ---------------------------------------------------------------

    def paint(self, p: QPainter, area: QRectF, layout: Layout, frame: Frame, *,
              editing: bool = False, selected=None, px_scale: float = 1.0) -> None:
        """``px_scale``: drawing units per output pixel (1 for the real output; smaller
        for the scaled-down editor preview). Used for sizes given in pixels."""
        self._px = px_scale if px_scale > 0 else 1.0
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        bg = self.qcolor(layout.background)
        if bg.alpha():
            p.fillRect(area, bg)
        blink_on = int(frame.now * 2) % 2 == 0      # 2 Hz, like the Limitimer
        slow_on = int(frame.now * 1.25) % 2 == 0    # message flash
        for e in layout.elements:
            if not e.visible:
                continue
            rect = QRectF(area.left() + e.x * area.width(), area.top() + e.y * area.height(),
                          e.w * area.width(), e.h * area.height())
            draw = getattr(self, f"_draw_{e.type}")
            draw(p, rect, e, layout, frame, blink_on, slow_on, editing)
        if editing:
            self._guides(p, area, layout, selected)
        p.restore()

    def _guides(self, p: QPainter, area: QRectF, layout: Layout, selected) -> None:
        """Outline every element; selected ones in teal. ``selected`` is an id or a set of ids."""
        sel = {selected} if isinstance(selected, str) else set(selected or ())
        p.setBrush(Qt.BrushStyle.NoBrush)
        for e in layout.elements:
            if not e.visible:
                continue
            r = QRectF(area.left() + e.x * area.width(), area.top() + e.y * area.height(),
                       e.w * area.width(), e.h * area.height())
            if e.id in sel:
                p.setPen(QPen(_SELECT, 2))
            else:
                p.setPen(QPen(_GUIDE, 1, Qt.PenStyle.DashLine))
            p.drawRect(r)

    # -- elements ------------------------------------------------------------------

    def _phase_color(self, layout: Layout, ph: str) -> QColor:
        return self.qcolor(layout.color(ph))

    def _no_signal(self, layout: Layout, frame: Frame) -> bool:
        return frame.state is not None and not frame.live and layout.no_signal != "ignore"

    def _mm_phase(self, frame: Frame) -> str:
        return millumin_phase(frame.millumin, frame.mm_yellow, frame.mm_red)

    def _draw_mm_timer(self, p, rect, e, layout, frame, editing):
        self._bg(p, rect, e)
        v = frame.millumin
        if v is None:                        # nothing on the layer: blank (placeholder while editing)
            if editing:
                self._text(p, rect, "--:--", e, self.qcolor(layout.color("dim")).lighter(250))
            return
        if e.mm_show == "elapsed":
            text = format_time(v.elapsed)
        elif e.mm_show == "duration":
            text = format_time(v.duration)
        else:
            text = format_time(v.remaining, round_up=True)
        color = self._phase_color(layout, self._mm_phase(frame)) if e.phase_colors else self.qcolor(e.color)
        if not v.live and v.state != "playing":
            color = _NO_SIGNAL_DIM
        self._text(p, rect, text, e, color)

    def _draw_timer(self, p, rect, e, layout, frame, blink_on, slow_on, editing):
        if e.source == "millumin":
            return self._draw_mm_timer(p, rect, e, layout, frame, editing)
        prog = resolve_program(frame.state, e.source)
        if self._no_signal(layout, frame) and layout.no_signal == "hide" and not editing:
            return
        self._bg(p, rect, e)
        if frame.state is None or prog is None:
            self._text(p, rect, "--:--", e, self.qcolor(layout.color("dim")).lighter(250))
            return
        text = frame.state.display(prog.number, e.overtime, e.follow_format)
        if self._no_signal(layout, frame):
            red = self.qcolor(layout.color("expired"))
            if layout.no_signal == "text":
                self._text(p, rect, "NO SIGNAL", e, red, digits=False)
                return
            self._text(p, rect, text, e, _NO_SIGNAL_DIM)
            badge = QRectF(rect.left(), rect.top(), rect.width(), rect.height() * 0.18)
            tag = Element("label", align="left", bold=True)
            self._text(p, badge, "NO SIGNAL", tag, red, digits=False)
            return
        if e.follow_blink and prog.blink and not blink_on:
            return
        color = self._phase_color(layout, phase(prog)) if e.phase_colors else self.qcolor(e.color)
        self._text(p, rect, text, e, color)

    def _draw_lights(self, p, rect, e, layout, frame, blink_on, slow_on, editing):
        self._bg(p, rect, e)
        prog = resolve_program(frame.state, e.source)
        lit = None
        if e.source == "millumin":
            lit = {"running": 0, "warning": 1, "expired": 2}.get(self._mm_phase(frame))
            prog = None
        if prog is not None and not self._no_signal(layout, frame):
            ph = phase(prog)
            lit = {"running": 0, "warning": 1, "expired": 2}.get(ph)
            if lit is None and e.show_idle and prog.remaining > 0:
                lit = 0
            if e.follow_blink and prog.blink and not blink_on:
                lit = None
        colors = [self.qcolor(layout.color(k)) for k in ("running", "warning", "expired")]
        off = self.qcolor(layout.color("dim"))
        vertical = e.orientation == "vertical"
        long_side = rect.height() if vertical else rect.width()
        short_side = rect.width() if vertical else rect.height()
        d = min(short_side * 0.86, long_side / 3 * 0.78)
        gap = (long_side - 3 * d) / 4
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            # red on top when vertical (traffic-light order), green first when horizontal
            slot = (2 - i) if vertical else i
            offset = gap + slot * (d + gap)
            if vertical:
                c = QPointF(rect.center().x(), rect.top() + offset + d / 2)
            else:
                c = QPointF(rect.left() + offset + d / 2, rect.center().y())
            p.setBrush(colors[i] if lit == i else off)
            p.drawEllipse(c, d / 2, d / 2)

    def _arrow(self, rect: QRectF, right: bool) -> QPolygonF:
        s = min(rect.width(), rect.height())
        cx, cy = rect.center().x(), rect.center().y()
        hw, hh = s * 0.42, s * 0.42
        if right:
            pts = [QPointF(cx - hw, cy - hh), QPointF(cx + hw, cy), QPointF(cx - hw, cy + hh)]
        else:
            pts = [QPointF(cx + hw, cy - hh), QPointF(cx - hw, cy), QPointF(cx + hw, cy + hh)]
        return QPolygonF(pts)

    def _draw_cue(self, p, rect, e, layout, frame, blink_on, slow_on, editing):
        active = frame.cue.active(e.hold, frame.now)
        off = self.qcolor(layout.color("dim"))
        col = {"next": layout.color("next"), "prev": layout.color("prev"), "blank": layout.color("blank")}
        if e.cue_style == "frame":
            t = max(1.0, e.frame_px * getattr(self, "_px", 1.0))
            if active:
                self._frame_outline(p, rect, e, t, self.qcolor(col[active]))
            elif editing:
                self._frame_outline(p, rect, e, t, off)
            return
        if e.cue_style == "light":
            if active:
                p.fillRect(rect, self.qcolor(col[active]))
            elif editing:
                self._bg(p, rect, e)
                self._text(p, rect, "CUE LIGHT", Element("label"), off.lighter(200), digits=False)
            else:
                self._bg(p, rect, e)
            return
        self._bg(p, rect, e)
        if e.cue_style == "text":
            if active:
                word = {"next": "NEXT ▶", "prev": "◀ PREV", "blank": "BLANK"}[active]
                self._text(p, rect, word, e, self.qcolor(col[active]), digits=False)
            elif editing:
                self._text(p, rect, "NEXT ▶", e, off.lighter(200), digits=False)
            return
        # arrows: ◁ ● ▷ like the receiver's LEDs — red, yellow, green
        pad = min(rect.width(), rect.height()) * 0.12
        inner = rect.adjusted(pad, pad, -pad, -pad)
        third = inner.width() / 3
        cells = [QRectF(inner.left() + i * third, inner.top(), third, inner.height()) for i in range(3)]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.qcolor(col["prev"]) if active == "prev" else off)
        p.drawPolygon(self._arrow(cells[0], right=False))
        p.setBrush(self.qcolor(col["blank"]) if active == "blank" or (frame.cue.blank and active) else off)
        d = min(cells[1].width(), cells[1].height()) * 0.55
        p.drawEllipse(cells[1].center(), d / 2, d / 2)
        p.setBrush(self.qcolor(col["next"]) if active == "next" else off)
        p.drawPolygon(self._arrow(cells[2], right=True))

    def _frame_outline(self, p: QPainter, rect: QRectF, e: Element, t: float, color: QColor) -> None:
        """An outline of width ``t`` just inside ``rect`` (rounded if the element has a radius)."""
        t = min(t, rect.width() / 2, rect.height() / 2)
        rad = e.radius * rect.height()
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        inner = rect.adjusted(t, t, -t, -t)
        if rad > 0:
            path.addRoundedRect(rect, rad, rad)
            ir = max(0.0, rad - t)
            path.addRoundedRect(inner, ir, ir)
        else:
            path.addRect(rect)
            path.addRect(inner)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(path)

    def _draw_progress(self, p, rect, e, layout, frame, blink_on, slow_on, editing):
        prog = resolve_program(frame.state, e.source)
        rad = min(rect.width(), rect.height()) / 2      # pill ends, horizontal or vertical
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.qcolor(e.bg) if self.qcolor(e.bg).alpha() else self.qcolor(layout.color("dim")))
        p.drawRoundedRect(rect, rad, rad)
        if e.source == "millumin":
            v = frame.millumin
            if v is None or v.fraction is None:
                return
            frac = v.fraction if e.bar_fill == "elapsed" else 1.0 - v.fraction   # auto = remaining
            ph = self._mm_phase(frame)
        else:
            if prog is None or prog.total <= 0 or self._no_signal(layout, frame):
                return
            frac = progress_fraction(prog, frame.state.count_down, e.bar_fill)
            ph = phase(prog)
        if frac <= 0:
            return
        color = self._phase_color(layout, ph) if e.phase_colors else self.qcolor(e.color)
        bar = progress_rect(rect, frac, e.bar_direction)
        clip = QPainterPath()
        clip.addRoundedRect(rect, rad, rad)
        p.save()
        p.setClipPath(clip)
        p.setBrush(color)
        p.drawRect(bar)
        p.restore()

    def _draw_clock(self, p, rect, e, layout, frame, blink_on, slow_on, editing):
        self._bg(p, rect, e)
        t = frame.wall
        if e.clock_24h:
            text = t.strftime("%H:%M:%S" if e.clock_seconds else "%H:%M")
        else:
            text = t.strftime("%I:%M:%S" if e.clock_seconds else "%I:%M").lstrip("0") \
                + (" PM" if t.hour >= 12 else " AM")
        self._text(p, rect, text, e, self.qcolor(e.color))

    def _draw_label(self, p, rect, e, layout, frame, blink_on, slow_on, editing):
        self._bg(p, rect, e)
        prog = resolve_program(frame.state, e.source)
        text = expand_tokens(e.text, frame.state, prog, frame.millumin)
        if frame.state is None and "{" in e.text:
            text = expand_tokens(e.text, None, None, frame.millumin).replace("PROGRAM -", "PROGRAM")
        self._text(p, rect, text, e, self.qcolor(e.color), digits=False)

    def _draw_message(self, p, rect, e, layout, frame, blink_on, slow_on, editing):
        text = frame.message
        if not text:
            if editing:
                pen = QPen(self.qcolor(layout.color("dim")).lighter(200), 1, Qt.PenStyle.DotLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect)
                self._text(p, rect, "OPERATOR MESSAGE", e, self.qcolor(layout.color("dim")).lighter(250),
                           digits=False)
            return
        self._bg(p, rect, e)
        if e.flash and not slow_on:
            return
        self._text(p, rect, text, e, self.qcolor(e.color), digits=False)


def progress_fraction(prog, count_down: bool, mode: str = "auto") -> float:
    """0-1 length of the progress bar. 'remaining' empties, 'elapsed' fills;
    'auto' follows the Limitimer: remaining when counting down, elapsed when up."""
    if prog.total <= 0:
        return 0.0
    if mode == "auto":
        mode = "remaining" if count_down else "elapsed"
    value = prog.remaining if mode == "remaining" else prog.elapsed
    return max(0.0, min(1.0, value / prog.total))


def progress_rect(rect: QRectF, frac: float, direction: str = "ltr") -> QRectF:
    """The filled part of a bar growing from one edge: ltr, rtl, btt or ttb."""
    w, h = rect.width(), rect.height()
    if direction == "rtl":
        return QRectF(rect.right() - w * frac, rect.top(), w * frac, h)
    if direction == "btt":
        return QRectF(rect.left(), rect.bottom() - h * frac, w, h * frac)
    if direction == "ttb":
        return QRectF(rect.left(), rect.top(), w, h * frac)
    return QRectF(rect.left(), rect.top(), w * frac, h)


def checkerboard(p: QPainter, rect: QRectF, cell: int = 12) -> None:
    """Show transparency in the preview."""
    p.save()
    p.fillRect(rect, QColor("#2A2A2A"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#333333")))
    y = 0
    while y * cell < rect.height():
        x = y % 2
        while x * cell < rect.width():
            p.drawRect(QRectF(rect.left() + x * cell, rect.top() + y * cell, cell, cell))
            x += 2
        y += 1
    p.restore()


__all__ = ["CueState", "Frame", "Renderer", "checkerboard", "progress_fraction", "progress_rect"]

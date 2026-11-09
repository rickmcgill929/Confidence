"""Layout editor canvas: selection, move/resize with smart guides, grid, safe areas,
undo history and the icons for the alignment toolbar.

Mouse
  click               select (Shift / ⌘-click adds or removes)
  drag empty space    selection box
  drag an element     move the selection (Shift locks to horizontal / vertical)
  drag a handle       resize (Shift keeps proportions, Option resizes from the center)
  hold ⌘ (Ctrl)       while dragging: no snapping

Keys
  arrows              nudge (Shift = ×10; uses the grid step when the grid is on)
  Option + arrows     resize from the right / bottom edge
  ⌘A                  select all        Esc  clear selection
  Delete              delete selection
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QKeyEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QSizePolicy, QWidget

from . import align as al
from .layout import Element, Layout
from .render import Frame, Renderer, checkerboard

TEAL = QColor("#2EC4B6")
GUIDE_ELEMENT = QColor("#FF5AD9")
GUIDE_CANVAS = QColor("#2EC4B6")
SAFE_ACTION = QColor(255, 176, 0, 150)
SAFE_TITLE = QColor(255, 176, 0, 90)
GRID = QColor(255, 255, 255, 22)
HANDLE = 9                       # handle square size, px
SNAP_PX = 6                      # snapping distance, px


class History:
    """Undo/redo of whole-layout snapshots (layouts are small)."""

    def __init__(self, limit: int = 200):
        self.limit = limit
        self._undo: List[dict] = []
        self._redo: List[dict] = []

    def push(self, snapshot: dict) -> None:
        if self._undo and self._undo[-1] == snapshot:
            return
        self._undo.append(snapshot)
        del self._undo[:-self.limit]
        self._redo.clear()

    def undo(self, current: dict) -> Optional[dict]:
        while self._undo:
            snap = self._undo.pop()
            if snap != current:
                self._redo.append(current)
                return snap
        return None

    def redo(self, current: dict) -> Optional[dict]:
        if not self._redo:
            return None
        self._undo.append(current)
        return self._redo.pop()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


class EditorCanvas(QWidget):
    selection_changed = pyqtSignal(list)       # element ids, primary first
    edit_started = pyqtSignal()                # geometry is about to change (undo checkpoint)
    edited = pyqtSignal()
    delete_requested = pyqtSignal()

    def __init__(self, layout_source: Callable[[], Layout], frame_source: Callable[[], Frame],
                 aspect_source: Callable[[], float], renderer: Renderer,
                 pixels_source: Optional[Callable[[], Tuple[int, int]]] = None):
        super().__init__()
        self._layout = layout_source
        self._frame = frame_source
        self._aspect = aspect_source
        self._pixels = pixels_source
        self.renderer = renderer
        self.selected: List[str] = []
        self.snap = True
        self.grid = 0.0                    # step as a fraction of the output; 0 = off
        self.show_grid = False
        self.show_safe = False
        self._drag: Optional[dict] = None
        self._guides: List[al.Guide] = []
        self._marquee: Optional[QRectF] = None
        self._readout: Optional[Tuple[QPointF, str]] = None
        self._last_nudge = 0.0
        self.setMinimumSize(QSize(420, 240))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        t = QTimer(self)
        t.timeout.connect(self.update)
        t.start(50)

    # ── geometry helpers ──────────────────────────────────────────────────────

    def canvas(self) -> QRectF:
        a = self._aspect() or 16 / 9
        w, h = self.width() - 8, self.height() - 8
        cw, ch = (h * a, h) if w / h > a else (w, w / a)
        return QRectF((self.width() - cw) / 2, (self.height() - ch) / 2, cw, ch)

    def _norm(self, pos: QPointF) -> Tuple[float, float]:
        c = self.canvas()
        return (pos.x() - c.left()) / c.width(), (pos.y() - c.top()) / c.height()

    def _to_px(self, r: al.Rect) -> QRectF:
        c = self.canvas()
        x, y, w, h = r
        return QRectF(c.left() + x * c.width(), c.top() + y * c.height(), w * c.width(), h * c.height())

    def _threshold(self) -> Tuple[float, float]:
        c = self.canvas()
        return SNAP_PX / max(1.0, c.width()), SNAP_PX / max(1.0, c.height())

    def elements(self) -> List[Element]:
        lay = self._layout()
        return [e for i in self.selected for e in lay.elements if e.id == i]

    def set_selection(self, ids: List[str], emit: bool = False) -> None:
        lay = self._layout()
        valid = {e.id for e in lay.elements}
        self.selected = [i for i in dict.fromkeys(ids) if i in valid]
        if emit:
            self.selection_changed.emit(list(self.selected))
        self.update()

    def _handles(self, r: QRectF) -> Dict[str, QRectF]:
        cx, cy = r.center().x(), r.center().y()
        pts = {"tl": (r.left(), r.top()), "t": (cx, r.top()), "tr": (r.right(), r.top()), "r": (r.right(), cy),
               "br": (r.right(), r.bottom()), "b": (cx, r.bottom()), "bl": (r.left(), r.bottom()),
               "l": (r.left(), cy)}
        return {k: QRectF(x - HANDLE / 2, y - HANDLE / 2, HANDLE, HANDLE) for k, (x, y) in pts.items()}

    def _handle_at(self, pos: QPointF) -> Optional[str]:
        els = self.elements()
        if len(els) != 1:
            return None
        for name, hr in self._handles(self._to_px(al.rect(els[0]))).items():
            if hr.adjusted(-3, -3, 3, 3).contains(pos):
                return name
        return None

    _CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor}

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#111111"))
        c = self.canvas()
        lay = self._layout()
        if self.renderer.qcolor(lay.background).alpha() < 255:
            checkerboard(p, c)
        px = self._pixels()[0] if self._pixels else 0
        self.renderer.paint(p, c, lay, self._frame(), editing=True, selected=set(self.selected),
                            px_scale=c.width() / px if px else 1.0)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if self.show_grid and self.grid > 0:
            p.setPen(QPen(GRID, 1))
            steps = int(round(1 / self.grid))
            for i in range(1, steps):
                x = c.left() + i * self.grid * c.width()
                y = c.top() + i * self.grid * c.height()
                p.drawLine(QPointF(x, c.top()), QPointF(x, c.bottom()))
                p.drawLine(QPointF(c.left(), y), QPointF(c.right(), y))
        if self.show_safe:
            for kind, col in (("action", SAFE_ACTION), ("title", SAFE_TITLE)):
                r = self._to_px(al.safe_rect(kind))
                p.setPen(QPen(col, 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r)
                f = QFont(p.font())
                f.setPixelSize(10)
                p.setFont(f)
                p.drawText(QPointF(r.left() + 4, r.top() + 12), "ACTION SAFE" if kind == "action" else "TITLE SAFE")
        p.setPen(QColor("#333"))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(c)

        els = self.elements()
        if len(els) == 1:
            p.setPen(QPen(QColor("#0B0B0B"), 1))
            p.setBrush(TEAL)
            for hr in self._handles(self._to_px(al.rect(els[0]))).values():
                p.drawRect(hr)
        elif len(els) > 1:
            p.setPen(QPen(TEAL, 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._to_px(al.bounds(els)))

        for g in self._guides:
            p.setPen(QPen(GUIDE_ELEMENT if g.kind == "element" else GUIDE_CANVAS, 1))
            if g.axis == "x":
                x = c.left() + g.pos * c.width()
                p.drawLine(QPointF(x, c.top()), QPointF(x, c.bottom()))
            else:
                y = c.top() + g.pos * c.height()
                p.drawLine(QPointF(c.left(), y), QPointF(c.right(), y))

        if self._marquee is not None:
            p.setPen(QPen(TEAL, 1))
            p.setBrush(QColor(46, 196, 182, 40))
            p.drawRect(self._marquee)

        if self._readout is not None:
            pos, text = self._readout
            f = QFont(p.font())
            f.setPixelSize(11)
            p.setFont(f)
            fm = p.fontMetrics()
            box = QRectF(pos.x() + 14, pos.y() + 14, fm.horizontalAdvance(text) + 12, fm.height() + 6)
            if box.right() > self.width() - 4:
                box.moveRight(pos.x() - 14)
            if box.bottom() > self.height() - 4:
                box.moveBottom(pos.y() - 14)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 200))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor("#E6FFFB"))
            p.drawText(box.adjusted(6, 3, -6, -3), Qt.AlignmentFlag.AlignVCenter, text)
        p.end()

    def _readout_text(self, r: al.Rect) -> str:
        x, y, w, h = r
        text = f"x {x * 100:.1f}%  y {y * 100:.1f}%  ·  {w * 100:.1f} × {h * 100:.1f}%"
        if self._pixels:
            pw, ph = self._pixels()
            text += f"  ·  {round(x * pw)}, {round(y * ph)}  {round(w * pw)}×{round(h * ph)} px"
        return text

    # ── mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        self.setFocus()
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = ev.position()
        mods = ev.modifiers()
        additive = bool(mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.MetaModifier))
        handle = self._handle_at(pos)
        lay = self._layout()
        if handle:
            el = self.elements()[0]
            self._drag = {"kind": "resize", "handle": handle, "start": al.rect(el), "press": self._norm(pos),
                          "moved": False}
            return
        hit = lay.hit(*self._norm(pos))
        if hit is not None:
            if additive:
                ids = [i for i in self.selected if i != hit.id] if hit.id in self.selected else self.selected + [hit.id]
                self.set_selection(ids, emit=True)
                if hit.id not in self.selected:
                    return
            elif hit.id not in self.selected:
                self.set_selection([hit.id], emit=True)
            self._drag = {"kind": "move", "press": self._norm(pos), "moved": False,
                          "starts": {e.id: al.rect(e) for e in self.elements()}}
            return
        if not additive:
            self.set_selection([], emit=True)
        self._drag = {"kind": "marquee", "origin": pos, "base": list(self.selected) if additive else []}
        self._marquee = QRectF(pos, pos)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        d = self._drag
        if d is None:
            h = self._handle_at(pos)
            hit = self._layout().hit(*self._norm(pos))
            self.setCursor(self._CURSORS[h] if h else
                           Qt.CursorShape.SizeAllCursor if hit and hit.id in self.selected else
                           Qt.CursorShape.ArrowCursor)
            return
        mods = ev.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        no_snap = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)) or not self.snap
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        if d["kind"] == "marquee":
            self._marquee = QRectF(d["origin"], pos).normalized()
            mx0, my0 = self._norm(self._marquee.topLeft())
            mx1, my1 = self._norm(self._marquee.bottomRight())
            hits = [e.id for e in self._layout().elements if e.visible and
                    e.x < mx1 and e.x + e.w > mx0 and e.y < my1 and e.y + e.h > my0]
            self.set_selection(d["base"] + [i for i in hits if i not in d["base"]], emit=True)
            return
        fx, fy = self._norm(pos)
        dx, dy = fx - d["press"][0], fy - d["press"][1]
        if not d["moved"]:
            if abs(dx) * self.canvas().width() < 2 and abs(dy) * self.canvas().height() < 2:
                return
            d["moved"] = True
            self.edit_started.emit()
        lay = self._layout()
        tx, ty = self._threshold()
        threshold = max(tx, ty)
        sel = set(self.selected)
        targets = None if no_snap else al.snap_targets([e for e in lay.elements if e.id not in sel])
        grid = 0.0 if no_snap else self.grid
        self._guides = []
        if d["kind"] == "move":
            if shift:                          # constrain to one axis
                if abs(dx) > abs(dy):
                    dy = 0.0
                else:
                    dx = 0.0
            bx, by, bw, bh = al.bounds_of(d["starts"].values())
            moved = (bx + dx, by + dy, bw, bh)
            if targets is not None:
                snapped, self._guides = al.snap_move(moved, targets, threshold, grid)
            else:
                snapped = al.clamp_rect(moved)
            if shift:
                snapped = (snapped[0] if dx else bx, snapped[1] if dy else by, bw, bh)
                self._guides = [g for g in self._guides if (g.axis == "x") == bool(dx)]
            ox, oy = snapped[0] - bx, snapped[1] - by
            for e in self.elements():
                sx, sy, sw, sh = d["starts"][e.id]
                al.set_rect(e, (sx + ox, sy + oy, sw, sh))
            self._readout = (pos, self._readout_text(snapped))
        else:
            r, self._guides = al.resize(d["start"], d["handle"], dx, dy, keep_aspect=shift, from_center=alt,
                                        targets=targets, threshold=threshold, grid=grid)
            al.set_rect(self.elements()[0], r)
            self._readout = (pos, self._readout_text(r))
        self.edited.emit()

    def mouseReleaseEvent(self, ev):
        self._drag = None
        self._guides = []
        self._marquee = None
        self._readout = None
        self.update()

    # ── keyboard ──────────────────────────────────────────────────────────────

    def keyPressEvent(self, ev: QKeyEvent):
        k, mods = ev.key(), ev.modifiers()
        cmd = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        if k == Qt.Key.Key_A and cmd:
            self.set_selection([e.id for e in self._layout().elements if e.visible], emit=True)
            return
        if k == Qt.Key.Key_Escape:
            self.set_selection([], emit=True)
            return
        els = self.elements()
        if not els:
            return super().keyPressEvent(ev)
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_requested.emit()
            return
        base = self.grid if self.grid > 0 else 0.005
        step = base * 10 if mods & Qt.KeyboardModifier.ShiftModifier else base
        moves = {Qt.Key.Key_Left: (-step, 0), Qt.Key.Key_Right: (step, 0),
                 Qt.Key.Key_Up: (0, -step), Qt.Key.Key_Down: (0, step)}
        if k not in moves:
            return super().keyPressEvent(ev)
        now = time.monotonic()
        if now - self._last_nudge > 1.0:
            self.edit_started.emit()           # one undo step per burst of nudges
        self._last_nudge = now
        dx, dy = moves[k]
        if mods & Qt.KeyboardModifier.AltModifier:      # resize
            for e in els:
                al.set_rect(e, al.clamp_rect((e.x, e.y, max(al.MIN_SIZE, e.w + dx), max(al.MIN_SIZE, e.h + dy))))
        else:
            b = al.bounds(els)
            nb = al.clamp_rect((b[0] + dx, b[1] + dy, b[2], b[3]))
            ox, oy = nb[0] - b[0], nb[1] - b[1]
            for e in els:
                al.set_rect(e, (e.x + ox, e.y + oy, e.w, e.h))
        self.edited.emit()


# ── toolbar icons (drawn, so there are no image assets to ship) ──────────────────

def make_icon(kind: str, size: int = 20) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    fg, acc = QColor("#C8C8C8"), QColor("#2EC4B6")
    s = size

    def bar(x, y, w, h, col=fg):
        p.fillRect(QRectF(x, y, w, h), col)

    def line_v(x, col=acc):
        p.fillRect(QRectF(x - 0.75, 2, 1.5, s - 4), col)

    def line_h(y, col=acc):
        p.fillRect(QRectF(2, y - 0.75, s - 4, 1.5), col)

    if kind == "align-left":
        line_v(4); bar(5.5, 5, 11, 4); bar(5.5, 11, 7, 4)
    elif kind == "align-hcenter":
        line_v(s / 2); bar(s / 2 - 6, 5, 12, 4); bar(s / 2 - 4, 11, 8, 4)
    elif kind == "align-right":
        line_v(s - 4); bar(s - 16.5, 5, 11, 4); bar(s - 12.5, 11, 7, 4)
    elif kind == "align-top":
        line_h(4); bar(5, 5.5, 4, 11); bar(11, 5.5, 4, 7)
    elif kind == "align-vcenter":
        line_h(s / 2); bar(5, s / 2 - 6, 4, 12); bar(11, s / 2 - 4, 4, 8)
    elif kind == "align-bottom":
        line_h(s - 4); bar(5, s - 16.5, 4, 11); bar(11, s - 12.5, 4, 7)
    elif kind == "dist-h":
        bar(3, 5, 3, 10); bar(8.5, 5, 3, 10); bar(14, 5, 3, 10)
        p.fillRect(QRectF(2, s - 2.5, s - 4, 1), acc)
    elif kind == "dist-v":
        bar(5, 3, 10, 3); bar(5, 8.5, 10, 3); bar(5, 14, 10, 3)
        p.fillRect(QRectF(s - 2.5, 2, 1, s - 4), acc)
    elif kind == "match-w":
        bar(4, 4, 12, 4); bar(4, 12, 12, 4, acc)
    elif kind == "match-h":
        bar(4, 4, 4, 12); bar(12, 4, 4, 12, acc)
    elif kind == "fill":
        p.setPen(QPen(fg, 1.5)); p.drawRect(QRectF(3, 3, s - 6, s - 6))
        bar(6, 6, s - 12, s - 12, acc)
    elif kind == "grid":
        p.setPen(QPen(fg, 1))
        for v in (3, 8, 13, 17):
            p.drawLine(QPointF(v, 3), QPointF(v, 17)); p.drawLine(QPointF(3, v), QPointF(17, v))
    elif kind == "safe":
        p.setPen(QPen(fg, 1, Qt.PenStyle.DashLine)); p.drawRect(QRectF(2.5, 2.5, s - 5, s - 5))
        p.setPen(QPen(acc, 1, Qt.PenStyle.DashLine)); p.drawRect(QRectF(5.5, 5.5, s - 11, s - 11))
    elif kind == "snap":
        p.setPen(QPen(fg, 1.5))
        p.drawLine(QPointF(4, 10), QPointF(16, 10))
        p.setPen(QPen(acc, 1.5))
        p.drawLine(QPointF(10, 3), QPointF(10, 17))
        bar(6.5, 6.5, 7, 7, QColor(46, 196, 182, 90))
    elif kind in ("undo", "redo"):
        p.setPen(QPen(fg, 1.8))
        f = QFont(p.font())
        f.setPixelSize(15)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 0, s, s), Qt.AlignmentFlag.AlignCenter, "↶" if kind == "undo" else "↷")
    p.end()
    return QIcon(pm)


__all__ = ["EditorCanvas", "History", "make_icon"]

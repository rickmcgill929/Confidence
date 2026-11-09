"""Alignment, distribution, sizing and snapping for the layout editor.

Pure Python on normalized geometry (0-1 of the output), so it's independent of
Qt and of output resolution. Elements are anything with ``x, y, w, h``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]          # x, y, w, h
CANVAS: Rect = (0.0, 0.0, 1.0, 1.0)

# Broadcast safe areas (SMPTE RP 2046-2 / EBU R95 for 16:9 HD): inset per side
SAFE_AREAS = {"action": 0.035, "title": 0.05}     # 93% and 90%

MIN_SIZE = 0.01


def rect(e) -> Rect:
    return (e.x, e.y, e.w, e.h)


def set_rect(e, r: Rect) -> None:
    e.x, e.y, e.w, e.h = (round(v, 4) for v in r)


def bounds(els: Iterable) -> Optional[Rect]:
    els = list(els)
    if not els:
        return None
    x0 = min(e.x for e in els)
    y0 = min(e.y for e in els)
    x1 = max(e.x + e.w for e in els)
    y1 = max(e.y + e.h for e in els)
    return (x0, y0, x1 - x0, y1 - y0)


def bounds_of(rects: Iterable[Rect]) -> Optional[Rect]:
    rects = list(rects)
    if not rects:
        return None
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return (x0, y0, x1 - x0, y1 - y0)


def clamp_rect(r: Rect) -> Rect:
    """Keep a rect on the canvas without changing its size (unless it's bigger)."""
    x, y, w, h = r
    w, h = min(max(w, MIN_SIZE), 1.0), min(max(h, MIN_SIZE), 1.0)
    return (min(max(x, 0.0), 1.0 - w), min(max(y, 0.0), 1.0 - h), w, h)


# ── align / distribute / match ──────────────────────────────────────────────────

ALIGN_MODES = ("left", "hcenter", "right", "top", "vcenter", "bottom")


def align(els: Sequence, how: str, ref: Optional[Rect] = None) -> None:
    """Align elements to a reference rect: the canvas for one element, else the
    selection's bounds (pass ``ref`` to override, e.g. CANVAS or a safe area)."""
    if how not in ALIGN_MODES:
        raise ValueError(how)
    els = list(els)
    if not els:
        return
    if ref is None:
        ref = CANVAS if len(els) == 1 else bounds(els)
    rx, ry, rw, rh = ref
    for e in els:
        if how == "left":
            e.x = rx
        elif how == "hcenter":
            e.x = rx + (rw - e.w) / 2
        elif how == "right":
            e.x = rx + rw - e.w
        elif how == "top":
            e.y = ry
        elif how == "vcenter":
            e.y = ry + (rh - e.h) / 2
        else:
            e.y = ry + rh - e.h
        set_rect(e, clamp_rect(rect(e)))


def distribute(els: Sequence, axis: str) -> None:
    """Equal gaps between 3+ elements along 'h' or 'v', keeping the outer two in place."""
    els = list(els)
    if len(els) < 3:
        return
    if axis == "h":
        els.sort(key=lambda e: e.x)
        span = (els[-1].x + els[-1].w) - els[0].x
        gap = (span - sum(e.w for e in els)) / (len(els) - 1)
        pos = els[0].x
        for e in els:
            e.x = round(pos, 4)
            pos += e.w + gap
    elif axis == "v":
        els.sort(key=lambda e: e.y)
        span = (els[-1].y + els[-1].h) - els[0].y
        gap = (span - sum(e.h for e in els)) / (len(els) - 1)
        pos = els[0].y
        for e in els:
            e.y = round(pos, 4)
            pos += e.h + gap
    else:
        raise ValueError(axis)


def match_size(els: Sequence, what: str, to=None) -> None:
    """Make 'width', 'height' or 'both' equal to ``to`` (default: the first element)."""
    els = list(els)
    if len(els) < 2:
        return
    ref = to or els[0]
    for e in els:
        if e is ref:
            continue
        if what in ("width", "both"):
            e.w = ref.w
        if what in ("height", "both"):
            e.h = ref.h
        set_rect(e, clamp_rect(rect(e)))


def fill(els: Sequence, ref: Rect = CANVAS) -> None:
    """Stretch elements to cover ``ref`` (the canvas or a safe area)."""
    for e in els:
        set_rect(e, ref)


def safe_rect(kind: str) -> Rect:
    m = SAFE_AREAS[kind]
    return (m, m, 1 - 2 * m, 1 - 2 * m)


# ── snapping ──────────────────────────────────────────────────────────────────

@dataclass
class Guide:
    axis: str          # "x" → vertical line at x; "y" → horizontal line at y
    pos: float
    kind: str          # "canvas" | "safe" | "element" | "grid"


@dataclass
class SnapTargets:
    xs: List[Tuple[float, str]] = field(default_factory=list)
    ys: List[Tuple[float, str]] = field(default_factory=list)


def snap_targets(others: Iterable, safe: bool = True) -> SnapTargets:
    """Lines worth snapping to: canvas edges and center, safe areas, other elements'
    edges and centers."""
    t = SnapTargets()
    for v in (0.0, 0.5, 1.0):
        t.xs.append((v, "canvas"))
        t.ys.append((v, "canvas"))
    if safe:
        for m in SAFE_AREAS.values():
            for v in (m, 1 - m):
                t.xs.append((v, "safe"))
                t.ys.append((v, "safe"))
    for e in others:
        if not getattr(e, "visible", True):
            continue
        t.xs += [(e.x, "element"), (e.x + e.w / 2, "element"), (e.x + e.w, "element")]
        t.ys += [(e.y, "element"), (e.y + e.h / 2, "element"), (e.y + e.h, "element")]
    return t


def _best(points: Sequence[float], targets: Sequence[Tuple[float, str]], threshold: float):
    """Smallest correction that brings any of ``points`` onto a target line."""
    best = None
    for p in points:
        for v, kind in targets:
            d = v - p
            if abs(d) <= threshold and (best is None or abs(d) < abs(best[0])):
                best = (d, v, kind)
    return best


def _grid(v: float, grid: float) -> float:
    return round(v / grid) * grid if grid > 0 else v


def _touching(points, targets, axis: str) -> List[Guide]:
    """Every target line the snapped rect now sits on (several can coincide)."""
    seen, out = set(), []
    for p in points:
        for v, kind in targets:
            if abs(v - p) < 1e-6 and (v, kind) not in seen:
                seen.add((v, kind))
                out.append(Guide(axis, v, kind))
    return out


def snap_move(r: Rect, targets: SnapTargets, threshold: float, grid: float = 0.0):
    """Snap a moving rect (edges or center) to targets, else to the grid.
    Returns (snapped rect, guides for every line it now touches)."""
    x, y, w, h = r
    guides: List[Guide] = []
    bx = _best((x, x + w / 2, x + w), targets.xs, threshold)
    if bx:
        x += bx[0]
        guides += _touching((x, x + w / 2, x + w), targets.xs, "x")
    elif grid:
        x = _grid(x, grid)
    by = _best((y, y + h / 2, y + h), targets.ys, threshold)
    if by:
        y += by[0]
        guides += _touching((y, y + h / 2, y + h), targets.ys, "y")
    elif grid:
        y = _grid(y, grid)
    return clamp_rect((x, y, w, h)), guides


# Resize handles: which edges move. "l","r","t","b" combinations.
HANDLES = ("tl", "t", "tr", "r", "br", "b", "bl", "l")


def resize(start: Rect, handle: str, dx: float, dy: float, *, keep_aspect: bool = False,
           from_center: bool = False, targets: Optional[SnapTargets] = None, threshold: float = 0.0,
           grid: float = 0.0):
    """New rect after dragging ``handle`` by (dx, dy) from ``start``.

    keep_aspect (Shift) holds the start proportions on corner handles;
    from_center (Option/Alt) resizes symmetrically about the center.
    Moving edges snap to ``targets`` / ``grid``. Returns (rect, guides).
    """
    x0, y0, w0, h0 = start
    left, top, right, bottom = x0, y0, x0 + w0, y0 + h0
    guides: List[Guide] = []

    def snap_edge(v: float, axis: str) -> float:
        if targets is not None and threshold > 0:
            b = _best((v,), targets.xs if axis == "x" else targets.ys, threshold)
            if b:
                guides.append(Guide(axis, b[1], b[2]))
                return v + b[0]
        return _grid(v, grid) if grid else v

    if "l" in handle:
        left = snap_edge(left + dx, "x")
    if "r" in handle:
        right = snap_edge(right + dx, "x")
    if "t" in handle:
        top = snap_edge(top + dy, "y")
    if "b" in handle:
        bottom = snap_edge(bottom + dy, "y")

    if from_center:
        cx, cy = x0 + w0 / 2, y0 + h0 / 2
        if "l" in handle:
            right = 2 * cx - left
        if "r" in handle:
            left = 2 * cx - right
        if "t" in handle:
            bottom = 2 * cy - top
        if "b" in handle:
            top = 2 * cy - bottom

    w, h = right - left, bottom - top
    if keep_aspect and len(handle) == 2 and w0 > 0 and h0 > 0:
        aspect = w0 / h0
        if abs(w / w0) >= abs(h / h0):
            h = w / aspect
        else:
            w = h * aspect
        guides = []                                   # proportional resize overrides snaps
        if from_center:
            cx, cy = x0 + w0 / 2, y0 + h0 / 2
            left, top = cx - w / 2, cy - h / 2
        else:
            left = right - w if "l" in handle else left
            top = bottom - h if "t" in handle else top

    # flipped past the opposite edge → clamp to a minimum size
    if w < MIN_SIZE:
        w = MIN_SIZE
        if "l" in handle and not from_center:
            left = (x0 + w0) - w
    if h < MIN_SIZE:
        h = MIN_SIZE
        if "t" in handle and not from_center:
            top = (y0 + h0) - h
    left, top = max(0.0, left), max(0.0, top)
    w, h = min(w, 1.0 - left), min(h, 1.0 - top)
    return (round(left, 4), round(top, 4), round(w, 4), round(h, 4)), guides


__all__ = ["ALIGN_MODES", "CANVAS", "Guide", "HANDLES", "SAFE_AREAS", "SnapTargets", "align", "bounds",
           "bounds_of", "clamp_rect", "distribute", "fill", "match_size", "rect", "resize", "safe_rect", "set_rect",
           "snap_move", "snap_targets"]

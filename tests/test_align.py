import os

import pytest

from dsan.clock import align as al
from dsan.clock.layout import Element, Layout


def E(x, y, w, h, **kw):
    return Element("label", x, y, w, h, **kw)


def approx(r, expect, tol=1e-3):
    return all(abs(a - b) <= tol for a, b in zip(r, expect))


# ── align / distribute / match / fill ─────────────────────────────────────────────

def test_align_single_to_canvas():
    e = E(0.3, 0.3, 0.2, 0.1)
    al.align([e], "right")
    assert approx(al.rect(e), (0.8, 0.3, 0.2, 0.1))
    al.align([e], "hcenter")
    al.align([e], "vcenter")
    assert approx(al.rect(e), (0.4, 0.45, 0.2, 0.1))


def test_align_many_to_selection_bounds():
    a, b = E(0.1, 0.1, 0.2, 0.1), E(0.5, 0.6, 0.3, 0.2)
    al.align([a, b], "left")
    assert a.x == b.x == 0.1
    al.align([a, b], "bottom")
    assert approx((a.y + a.h, b.y + b.h), (0.8, 0.8))


def test_align_to_title_safe():
    e = E(0.0, 0.0, 0.2, 0.1)
    al.align([e], "right", al.safe_rect("title"))
    assert approx((e.x + e.w,), (0.95,))


def test_distribute_equal_gaps():
    a, b, c = E(0.0, 0, 0.1, 0.1), E(0.15, 0, 0.2, 0.1), E(0.8, 0, 0.1, 0.1)
    al.distribute([c, a, b], "h")           # order of selection doesn't matter
    gaps = [b.x - (a.x + a.w), c.x - (b.x + b.w)]
    assert abs(gaps[0] - gaps[1]) < 1e-3 and a.x == 0.0 and abs(c.x - 0.8) < 1e-3


def test_match_and_fill():
    a, b = E(0, 0, 0.4, 0.2), E(0.5, 0.5, 0.1, 0.1)
    al.match_size([a, b], "both")
    assert (b.w, b.h) == (0.4, 0.2) and b.x + b.w <= 1 and b.y + b.h <= 1
    al.fill([b], al.safe_rect("action"))
    assert approx(al.rect(b), (0.035, 0.035, 0.93, 0.93))


def test_clamp_keeps_on_canvas():
    assert approx(al.clamp_rect((0.95, -0.2, 0.2, 0.3)), (0.8, 0.0, 0.2, 0.3))


# ── snapping ───────────────────────────────────────────────────────────────────

def test_snap_move_to_canvas_centre():
    r, guides = al.snap_move((0.395, 0.2, 0.2, 0.1), al.snap_targets([], safe=False), 0.01)
    assert approx(r, (0.4, 0.2, 0.2, 0.1)) and [(g.axis, g.pos) for g in guides] == [("x", 0.5)]


def test_snap_move_to_other_element_edge():
    other = E(0.3, 0.6, 0.2, 0.1)
    r, guides = al.snap_move((0.305, 0.21, 0.1, 0.1), al.snap_targets([other], safe=False), 0.01)
    assert approx(r[:2], (0.3, 0.21)) and guides[0].kind == "element"


def test_snap_move_grid_fallback():
    r, guides = al.snap_move((0.231, 0.377, 0.1, 0.1), al.SnapTargets(), 0.0, grid=0.05)
    assert approx(r[:2], (0.25, 0.4)) and guides == []


def test_resize_corner_edge_aspect_center():
    start = (0.2, 0.2, 0.4, 0.2)
    r, _ = al.resize(start, "br", 0.1, 0.05)
    assert approx(r, (0.2, 0.2, 0.5, 0.25))
    r, _ = al.resize(start, "r", 0.1, 0.3)                     # edge handle ignores the other axis
    assert approx(r, (0.2, 0.2, 0.5, 0.2))
    r, _ = al.resize(start, "br", 0.2, 0.0, keep_aspect=True)   # Shift: 2:1 kept
    assert approx(r, (0.2, 0.2, 0.6, 0.3))
    r, _ = al.resize(start, "r", 0.1, 0, from_center=True)      # Option: both sides
    assert approx(r, (0.1, 0.2, 0.6, 0.2))
    r, _ = al.resize(start, "tl", 0.5, 0.5)                     # dragged past the far corner
    assert r[2] == al.MIN_SIZE and r[3] == al.MIN_SIZE


def test_resize_edge_snaps():
    r, guides = al.resize((0.2, 0.2, 0.2, 0.2), "r", 0.096, 0, targets=al.snap_targets([], safe=False),
                          threshold=0.01)
    assert approx(r, (0.2, 0.2, 0.3, 0.2)) and guides[0].pos == 0.5


# ── history ────────────────────────────────────────────────────────────────────

def test_history():
    from dsan.clock.editor import History
    h = History()
    h.push({"v": 1})
    h.push({"v": 1})                     # duplicates collapse
    h.push({"v": 2})
    assert h.undo({"v": 3}) == {"v": 2}
    assert h.undo({"v": 2}) == {"v": 1}
    assert h.redo({"v": 1}) == {"v": 2}
    h.push({"v": 9})
    assert not h.can_redo                # a new edit clears redo


# ── canvas + window, driven like a user ──────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockAlign").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockAlign"))
    w = appmod.ControlWindow()
    w.resize(1400, 900)
    w._set_layout(Layout(background="#000000", elements=[
        E(0.10, 0.10, 0.20, 0.10, id="a", name="A"),
        E(0.50, 0.40, 0.20, 0.10, id="b", name="B"),
        E(0.70, 0.70, 0.10, 0.10, id="c", name="C")]))
    w.preview.resize(1000, 562)
    yield w
    w.close()
    QSettings("TRUCUE", "DSANClockAlign").clear()


def _mouse(canvas, kind, fx, fy, mods=None):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    c = canvas.canvas()
    pt = QPointF(c.left() + fx * c.width(), c.top() + fy * c.height())
    t = {"press": QMouseEvent.Type.MouseButtonPress, "move": QMouseEvent.Type.MouseMove,
         "release": QMouseEvent.Type.MouseButtonRelease}[kind]
    ev = QMouseEvent(t, pt, pt, Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton if kind != "release" else Qt.MouseButton.NoButton,
                     mods or Qt.KeyboardModifier.NoModifier)
    getattr(canvas, {"press": "mousePressEvent", "move": "mouseMoveEvent", "release": "mouseReleaseEvent"}[kind])(ev)


def _key(canvas, key, mods=None):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeyEvent
    canvas.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods or Qt.KeyboardModifier.NoModifier))


def test_click_shift_click_and_marquee(win):
    from PyQt6.QtCore import Qt
    cv = win.preview
    _mouse(cv, "press", 0.15, 0.12); _mouse(cv, "release", 0.15, 0.12)
    assert cv.selected == ["a"] and win.props.element.id == "a"
    _mouse(cv, "press", 0.55, 0.45, Qt.KeyboardModifier.ShiftModifier); _mouse(cv, "release", 0.55, 0.45)
    assert cv.selected == ["a", "b"] and "2 elements selected" in _form_text(win.props)
    assert {i.data(Qt.ItemDataRole.UserRole) for i in win.element_list.selectedItems()} == {"a", "b"}
    _mouse(cv, "press", 0.02, 0.95); _mouse(cv, "release", 0.02, 0.95)          # empty space clears
    assert cv.selected == []
    _mouse(cv, "press", 0.45, 0.35); _mouse(cv, "move", 0.9, 0.9); _mouse(cv, "release", 0.9, 0.9)
    assert set(cv.selected) == {"b", "c"}                                       # box touches B and C


def _form_text(widget):
    from PyQt6.QtWidgets import QLabel
    return " ".join(l.text() for l in widget.findChildren(QLabel))


def test_drag_moves_group_and_snaps_with_guides(win):
    cv = win.preview
    cv.set_selection(["a", "b"], emit=True)
    a, b = win.layout_obj.find("a"), win.layout_obj.find("b")
    _mouse(cv, "press", 0.15, 0.12)
    _mouse(cv, "move", 0.208, 0.14)          # group left edge → ~0.158; no target near
    _mouse(cv, "move", 0.249, 0.14)          # group left would be 0.199… snaps? centre 0.5 / b's edges excluded
    assert abs((b.x - a.x) - 0.40) < 1e-3     # moved together
    # bring A's left edge near the canvas centre line: it should snap exactly to 0.5 and draw a guide
    cv.set_selection(["a"], emit=True)
    _mouse(cv, "release", 0.249, 0.14)
    start_x = a.x
    cx, cy = a.x + a.w / 2, a.y + a.h / 2           # grab the middle, away from the handles
    _mouse(cv, "press", cx, cy)
    _mouse(cv, "move", cx + (0.497 - start_x), cy)
    assert abs(a.x - 0.5) < 1e-4
    lines = {(g.pos, g.kind) for g in cv._guides}
    assert (0.5, "canvas") in lines and (0.7, "element") in lines     # left on centre, right on C
    _mouse(cv, "release", 0.6, 0.2)
    assert cv._guides == []


def test_resize_handle_and_shift_aspect(win):
    from PyQt6.QtCore import Qt
    cv = win.preview
    cv.set_selection(["c"], emit=True)
    c = win.layout_obj.find("c")
    cv.snap = False
    _mouse(cv, "press", 0.8, 0.8)                   # bottom-right handle
    _mouse(cv, "move", 0.9, 0.85)
    assert abs(c.w - 0.2) < 2e-3 and abs(c.h - 0.15) < 2e-3
    _mouse(cv, "release", 0.9, 0.85)
    w0, h0 = c.w, c.h
    _mouse(cv, "press", c.x + c.w, c.y + c.h)
    _mouse(cv, "move", c.x + c.w + 0.05, c.y + c.h, Qt.KeyboardModifier.ShiftModifier)
    assert abs(c.w / c.h - w0 / h0) < 0.02          # proportions kept
    _mouse(cv, "release", 0, 0)


def test_keys_select_all_nudge_delete(win):
    from PyQt6.QtCore import Qt
    cv = win.preview
    _key(cv, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert set(cv.selected) == {"a", "b", "c"}
    cv.set_selection(["a"], emit=True)
    a = win.layout_obj.find("a")
    _key(cv, Qt.Key.Key_Right)
    _key(cv, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert abs(a.x - 0.105) < 1e-4 and abs(a.y - 0.15) < 1e-4
    _key(cv, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)       # Option+arrow resizes
    assert abs(a.w - 0.205) < 1e-4
    _key(cv, Qt.Key.Key_Delete)
    assert win.layout_obj.find("a") is None and cv.selected == []
    _key(cv, Qt.Key.Key_Escape)


def test_toolbar_align_distribute_match_and_undo_redo(win, qapp):
    cv = win.preview
    a, b, c = (win.layout_obj.find(i) for i in "abc")
    win._commit_history()
    cv.set_selection(["a", "b", "c"], emit=True)
    assert win.dist_h.isEnabled() and win.match_w.isEnabled()
    win._align("top")
    assert a.y == b.y == c.y == 0.1
    win._commit_history()                           # (the 400 ms debounce, without waiting)
    win._distribute("h")
    gaps = sorted([b.x - (a.x + a.w), c.x - (b.x + b.w)])
    assert abs(gaps[0] - gaps[1]) < 1e-3
    win._commit_history()
    win._match("width")                             # first selected (A) sets the width
    assert c.w == a.w
    win._undo()
    assert abs(win.layout_obj.find("c").w - 0.10) < 1e-6           # match undone
    win._undo()
    win._undo()
    assert abs(win.layout_obj.find("b").y - 0.40) < 1e-6           # align undone
    assert cv.selected == ["a", "b", "c"]                          # selection survives undo
    win._redo()
    assert win.layout_obj.find("b").y == 0.1
    win.align_to.setCurrentIndex(win.align_to.findData("title"))
    cv.set_selection(["c"], emit=True)
    win._align("right")
    assert abs(win.layout_obj.find("c").x + win.layout_obj.find("c").w - 0.95) < 1e-3


def test_editor_options_and_readout(win):
    win.grid_btn.setChecked(True)
    win._editor_options()
    assert win.preview.grid == 0.05 and win.preview.show_grid      # grid on → 5%
    win.safe_btn.setChecked(True)
    win._editor_options()
    assert win.preview.show_safe
    px = win._output_pixels()
    assert px[0] > 0 and px[1] > 0
    text = win.preview._readout_text((0.25, 0.5, 0.5, 0.25))
    assert "x 25.0%" in text and "px" in text
    win.preview.grab()                                              # paints grid, safe areas, handles

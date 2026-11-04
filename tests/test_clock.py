import os
import time

import pytest

from dsan.clock.layout import (PRESETS, Element, Layout, default_element, expand_tokens,
                               format_color, parse_color, phase, resolve_program)
from dsan.clock.ports import PortInfo, PortRef, detect, probe
from dsan.limitimer import LimitimerState, Program
from dsan.perfectcue import Button
from dsan.sim import LimitimerSimulator, PerfectCueSimulator


# -- layout ------------------------------------------------------------------------

def test_presets_roundtrip_json(tmp_path):
    for name, make in PRESETS.items():
        lay = make()
        path = tmp_path / "l.json"
        lay.save(str(path))
        back = Layout.load(str(path))
        assert back.to_dict() == lay.to_dict(), name


def test_from_dict_ignores_unknown_and_fills_colors():
    d = preset_dict = PRESETS["Cue light"]().to_dict()
    d["elements"][0]["future_field"] = 1
    d["colors"] = {"running": "#112233"}
    d["no_signal"] = "bogus"
    lay = Layout.from_dict(preset_dict)
    assert lay.colors["running"] == "#112233" and lay.colors["expired"]
    assert lay.no_signal == "dim"


def test_unknown_element_type_rejected():
    with pytest.raises(ValueError):
        Element("hologram")


def test_hit_testing_prefers_topmost():
    lay = Layout(elements=[Element("timer", 0, 0, 1, 1, id="a"), Element("cue", 0.4, 0.4, 0.2, 0.2, id="b")])
    assert lay.hit(0.5, 0.5).id == "b"
    assert lay.hit(0.1, 0.1).id == "a"
    lay.elements[1].visible = False
    assert lay.hit(0.5, 0.5).id == "a"


def test_copy_gets_new_id():
    e = default_element("timer")
    assert e.copy().id != e.id


@pytest.mark.parametrize("text, rgba", [("#fff", (255, 255, 255, 255)), ("#102030", (16, 32, 48, 255)),
                                        ("#10203040", (16, 32, 48, 64))])
def test_colors(text, rgba):
    assert parse_color(text) == rgba
    assert parse_color(format_color(*rgba)) == rgba


def _state(**kw):
    progs = (Program(1, 1, 300, 30, 100), Program(2, 1, 300, 30, 290), Program(3, 0, 60, 0, 70), Program(4))
    return LimitimerState(programs=progs, **kw)


def test_phase_and_source():
    s = _state(selected_index=1)
    assert resolve_program(s, "selected").number == 2
    assert resolve_program(s, "3").number == 3
    assert [phase(s.program(n)) for n in (1, 2, 3, 4)] == ["running", "warning", "expired", "idle"]
    assert phase(None) == "idle"


def test_tokens():
    s = _state(selected_index=2)
    assert expand_tokens("{program} · {mode}", s, s.selected) == "PROGRAM 3 · COUNTDOWN"


# -- ports ---------------------------------------------------------------------------

P = [PortInfo("/dev/cu.usbserial-A", "FT232R", "AAA", "1-1", 0x0403, 0x6001),
     PortInfo("/dev/cu.usbserial-B", "FT232R", "BBB", "1-2", 0x0403, 0x6001)]


def test_portref_resolves_by_serial_then_location_then_device():
    ref = PortRef.of(P[0])
    moved = [PortInfo("/dev/cu.usbserial-Z", "FT232R", "AAA", "2-4", 0x0403, 0x6001), P[1]]
    assert ref.resolve(moved) == "/dev/cu.usbserial-Z"          # re-plugged elsewhere
    no_sn = PortRef("/dev/old", "", "1-2", "")
    assert no_sn.resolve(P) == "/dev/cu.usbserial-B"             # same USB socket
    assert PortRef("/dev/cu.usbserial-A").resolve(P) == "/dev/cu.usbserial-A"
    assert PortRef("/dev/gone", "ZZZ").resolve(P) is None
    assert PortRef.url("socket://10.0.0.5:4001").resolve([]) == "socket://10.0.0.5:4001"
    assert PortRef.from_dict(ref.to_dict()) == ref


def test_portref_duplicate_serials_use_location():
    dup = [PortInfo("/dev/a", "CH340", "1234", "1-1", 1, 2), PortInfo("/dev/b", "CH340", "1234", "1-2", 1, 2)]
    assert PortRef("/dev/x", "1234", "1-2").resolve(dup) == "/dev/b"


class _Silent:
    def read(self, n=1):
        time.sleep(0.02)
        return b""

    in_waiting = 0

    def close(self):
        pass


class _Garbage(LimitimerSimulator):
    def read(self, n=1):
        return bytes(b ^ 0xFF for b in super().read(n))   # what swapped A/B looks like


def _opener(mapping):
    def open_(dev, baud):
        f = mapping[dev]
        if isinstance(f, Exception):
            raise f
        return f()
    return open_


def test_probe_counts_limitimer_frames():
    r = probe("x", 19200, 0.4, opener=_opener({"x": lambda: LimitimerSimulator(rate_hz=30)}))
    assert r.looks_like_limitimer and r.lt_packets >= 5 and not r.error


def test_detect_limitimer_and_lone_perfectcue():
    res = detect(P, lt_seconds=0.4, opener=_opener({
        P[0].device: _Silent, P[1].device: lambda: LimitimerSimulator(rate_hz=30)}))
    assert res.limitimer == P[1] and res.perfectcue == P[0]


def test_detect_perfectcue_by_button_press_with_three_adapters():
    third = PortInfo("/dev/cu.usbserial-C", "FT232R", "CCC", "1-3", 1, 2)
    res = detect(P + [third], lt_seconds=0.3, press_seconds=3, opener=_opener({
        P[0].device: lambda: LimitimerSimulator(rate_hz=30),
        P[1].device: _Silent,
        third.device: lambda: PerfectCueSimulator(interval=0.2)}))
    assert res.limitimer == P[0] and res.perfectcue == third


def test_detect_reports_swapped_pairs_and_errors():
    res = detect(P, lt_seconds=0.4, opener=_opener({
        P[0].device: lambda: _Garbage(rate_hz=30), P[1].device: OSError("Resource busy")}))
    assert res.limitimer is None
    text = " ".join(res.notes)
    assert "A/B" in text and "Resource busy" in text


def test_detect_no_ports():
    assert "No USB serial adapters" in detect([]).notes[0]


# -- renderer (offscreen Qt) -----------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _render(layout, frame, w=640, h=360):
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from dsan.clock.render import Renderer
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    Renderer().paint(p, QRectF(0, 0, w, h), layout, frame, editing=True, selected=layout.elements[0].id)
    p.end()
    return img


def _count(img, rgb, tol=40):
    from PyQt6.QtGui import QColor
    r0, g0, b0 = rgb
    n = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            c = QColor(img.pixel(x, y))
            if abs(c.red() - r0) < tol and abs(c.green() - g0) < tol and abs(c.blue() - b0) < tol:
                n += 1
    return n


def test_every_preset_renders_in_every_state(qapp):
    from dsan.devices import LinkState
    from dsan.clock.render import CueState, Frame
    states = [None, _state(selected_index=0), _state(selected_index=1), _state(selected_index=2)]
    for make in PRESETS.values():
        for st in states:
            for link in (LinkState.LIVE, LinkState.STALE):
                cue = CueState()
                cue.press(Button.NEXT)
                _render(make(), Frame(state=st, link=link, cue=cue, message="WRAP UP"))


def test_timer_color_follows_phase(qapp):
    from dsan.devices import LinkState
    from dsan.clock.render import Frame
    lay = Layout(background="#000000", elements=[Element("timer", 0, 0, 1, 1)])
    green = _count(_render(lay, Frame(state=_state(selected_index=0), link=LinkState.LIVE)), (0, 224, 122))
    red = _count(_render(lay, Frame(state=_state(selected_index=2), link=LinkState.LIVE)), (255, 59, 59))
    assert green > 50 and red > 50


def test_no_signal_dims_timer(qapp):
    from dsan.devices import LinkState
    from dsan.clock.render import Frame
    lay = Layout(background="#000000", elements=[Element("timer", 0, 0, 1, 1)])
    stale = _render(lay, Frame(state=_state(selected_index=0), link=LinkState.STALE))
    assert _count(stale, (0, 224, 122)) == 0


def test_cue_hold_and_blank():
    from dsan.clock.render import CueState
    c = CueState()
    c.press(Button.NEXT, now=100.0)
    assert c.active(1.5, 101.0) == "next" and c.active(1.5, 102.0) is None
    c.press(Button.BLANK_ON)
    assert c.active(1.5, 200.0) == "blank"
    c.press(Button.PREV, now=200.0)
    assert c.active(1.5, 200.5) == "prev"
    c.press(Button.BLANK_OFF)
    assert c.active(1.5, 300.0) is None


def test_digits_never_change_width(qapp):
    from dsan.clock.render import Renderer
    r = Renderer()
    fam = r._family(Element("timer"))
    f, *_ = r._fit(fam, True, "00:00", 800, 300, True)
    widths = {round(r._measure(f, t, True)[0], 3) for t in ("00:00", "11:11", "47:58", "+08:19"[1:])}
    assert len(widths) == 1


def test_control_window_demo_cycle(qapp):
    from PyQt6.QtCore import QSettings
    QSettings("TRUCUE", "DSANClockTest").clear()
    import dsan.clock.app as appmod
    real = appmod.QSettings
    appmod.QSettings = lambda *_a: real("TRUCUE", "DSANClockTest")
    try:
        w = appmod.ControlWindow()
        w.demo_btn.click()
        end = time.monotonic() + 3
        while w.lt_state is None and time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.02)
        assert w.lt_state is not None and w.demo_btn.isChecked()
        w._send_message("WRAP UP")
        assert w.frame().message == "WRAP UP"
        w._add_element("clock")
        assert w.layout_obj.elements[-1].type == "clock"
        w.demo_btn.click()
        assert not w.demo_btn.isChecked() and w.lt_state is None
        w.close()
    finally:
        appmod.QSettings = real

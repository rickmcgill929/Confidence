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


def test_port_pickers(qapp, monkeypatch):
    """Pick adapters, auto-swap, unplug, re-plug under a new name, persist."""
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    from dsan import Limitimer, PerfectCue
    from dsan.clock import ports as portsmod

    A = PortInfo("/dev/cu.usbserial-AAA", "FT232R USB UART", "AAA", "1-1", 0x0403, 0x6001)
    B = PortInfo("/dev/cu.usbserial-BBB", "FT232R USB UART", "BBB", "1-2", 0x0403, 0x6001)
    BT = PortInfo("/dev/cu.Bluetooth-Incoming-Port", "n/a")
    plugged = [A, B, BT]
    monkeypatch.setattr(portsmod, "list_ports", lambda usb_only=False: list(plugged))

    def fake_device(self, kind, port):
        if kind == "lt":
            dev = Limitimer(transport_factory=lambda: LimitimerSimulator(rate_hz=20), reconnect=False,
                            baudrate=appmod.BAUD)
        else:
            dev = PerfectCue(transport_factory=lambda: PerfectCueSimulator(interval=0.3), reconnect=False,
                             baudrate=appmod.BAUD)
        dev.port = port
        return dev

    monkeypatch.setattr(appmod.ControlWindow, "_make_device", fake_device)
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockPick").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockPick"))

    def pump(pred, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            qapp.processEvents()
            time.sleep(0.02)
        return pred()

    w = appmod.ControlWindow()
    items = [w.lt_combo.itemText(i) for i in range(w.lt_combo.count())]
    assert items[0].startswith("Off") and any("usbserial-AAA" in t for t in items)
    assert not any("Bluetooth" in t for t in items)            # non-USB hidden by default
    assert "Not used" in w.lt_status.text()

    # pick A for the Limitimer: selected but not connected until Connect is pressed
    w._port_picked("lt", w.lt_combo.findData(A.device))
    assert w.limitimer is None and "press Connect" in w.lt_status.text()
    w._connect_clicked("lt", True)
    assert w.limitimer is not None and w.limitimer.port == A.device
    assert w.lt_connect.isChecked() and w.lt_connect.text() == "Disconnect"
    assert pump(lambda: w.lt_status.text() == "● Connected" or
                (w._update_activity() or False))

    # baud is fixed at 19200 for both devices — no selector
    assert w.limitimer.baudrate == 19200 and not hasattr(w, "lt_baud") and not hasattr(w, "pc_baud")

    # Disconnect / Connect
    w._connect_clicked("lt", False)
    assert w.limitimer is None and not w.lt_connect.isChecked()
    assert "Disconnected" in w.lt_status.text()
    w._connect_clicked("lt", True)
    assert w.limitimer is not None

    # Connect with no adapter picked is refused
    w._connect_clicked("pc", True)
    assert w.perfectcue is None and not w.pc_connect.isChecked()

    # pick A for the Perfect Cue too → they swap (Limitimer had A, Perfect Cue had nothing)
    w._port_picked("pc", w.pc_combo.findData(A.device))
    assert w.refs["pc"].device == A.device and w.refs["lt"] is None and w.limitimer is None
    w._connect_clicked("pc", True)
    assert w.perfectcue.port == A.device
    w._port_picked("lt", w.lt_combo.findData(B.device))
    w._connect_clicked("lt", True)
    assert w.limitimer.port == B.device
    assert "(used by Perfect Cue)" in w.lt_combo.itemText(w.lt_combo.findData(A.device))
    assert pump(lambda: w.pc_last_press is not None)
    w._update_activity()
    assert w.pc_status.text() == "● Connected"

    # picking a different port while connected moves the connection
    w._port_picked("pc", w.pc_combo.findData(A.device))
    assert w.perfectcue.port == A.device

    # unplug A → Perfect Cue stops and says so; re-plug with a new /dev name → reconnects
    plugged[:] = [B]
    w._refresh_ports()
    assert w.perfectcue is None and "not plugged in" in w.pc_status.text()
    A2 = PortInfo("/dev/cu.usbserial-AAA9", "FT232R USB UART", "AAA", "2-3", 0x0403, 0x6001)
    plugged[:] = [B, A2]
    w._refresh_ports()
    assert w.perfectcue is not None and w.perfectcue.port == A2.device

    # swap button keeps both connected
    w._swap_ports()
    assert w.limitimer.port == A2.device and w.perfectcue.port == B.device

    # Off disconnects and forgets
    w._port_picked("pc", 0)
    assert w.perfectcue is None and w.refs["pc"] is None and not w.pc_connect.isChecked()
    w.close()

    # choices survive a relaunch and reconnect
    w2 = appmod.ControlWindow()
    assert w2.limitimer is not None and w2.limitimer.port == A2.device and w2.perfectcue is None
    assert w2.lt_connect.isChecked()
    w2._connect_clicked("lt", False)
    w2.close()
    w3 = appmod.ControlWindow()          # disconnected at quit → stays disconnected, port remembered
    assert w3.limitimer is None and w3.refs["lt"].serial_number == "AAA" and w3._resolved("lt") == A2.device
    w3.close()


def test_blink_flag_ignored_unless_follow_blink(qapp):
    """A Limitimer that sends the blink flag must not make the digits flash by default."""
    from dsan.devices import LinkState
    from dsan.clock.render import Frame
    st = LimitimerState(programs=(Program(1, 0x03, 300, 30, 100),) + _state().programs[1:])   # running + blink
    assert st.programs[0].blink
    lay = Layout(background="#000000", elements=[Element("timer", 0, 0, 1, 1)])
    off_phase = 0.75          # int(0.75 * 2) % 2 == 1 → the "off" half of the blink cycle
    green = (0, 224, 122)
    assert _count(_render(lay, Frame(state=st, link=LinkState.LIVE, now=off_phase)), green) > 50
    lay.elements[0].follow_blink = True
    assert _count(_render(lay, Frame(state=st, link=LinkState.LIVE, now=off_phase)), green) == 0
    assert _count(_render(lay, Frame(state=st, link=LinkState.LIVE, now=0.25)), green) > 50


def test_fullscreen_is_borderless_and_survives_unplug(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings, Qt
    import dsan.clock.app as appmod
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockScreens").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockScreens"))
    w = appmod.ControlWindow()
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("fullscreen"))
    w.show_output_btn.setChecked(True)
    o = w.output
    scr = w._selected_screen()
    assert o.mode == "fullscreen"
    assert not (o.windowState() & Qt.WindowState.WindowFullScreen)      # not the OS full-screen mode
    assert o.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert o.geometry() == scr.geometry()
    # the chosen display disappears → the output parks in a window instead of taking another display
    w._wanted_screen = "Unplugged HDMI"
    w._screens_changed()
    assert o.mode == "window" and "disconnected" in w.output_note.text()
    # it comes back → full screen again
    w._wanted_screen = scr.name()
    w._screens_changed()
    assert o.mode == "fullscreen" and o.geometry() == scr.geometry()
    w.close()


def test_dip_panel_follows_limitimer(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    from dsan.limitimer import BeepType
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockDips").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockDips"))
    w = appmod.ControlWindow()
    assert [l.text() for l in w._dip_values] == ["—"] * 7
    w.lt_link = appmod.LinkState.LIVE
    w.limitimer = object()           # pretend a device is connected
    w._on_lt_state(LimitimerState(count_down=False, program_minutes=True, beep_type=BeepType.RING,
                                  beep_loud=True, permit_changes=False))
    assert [l.text() for l in w._dip_values] == [
        "HH:MM", "MM:SS", "Count up", "Stop at 0", "Loud", "Ring", "Locked"]
    w.lt_link = appmod.LinkState.STALE
    w._update_dips()
    assert "stopped" in w.dip_note.text()
    w.limitimer = None
    w.close()


def test_progress_fraction_modes():
    from dsan.clock.render import progress_fraction
    p = Program(1, 1, 200, 0, 50)                      # 25% elapsed, 75% remaining
    assert progress_fraction(p, True) == 0.75          # auto + countdown → remaining
    assert progress_fraction(p, False) == 0.25         # auto + count-up → elapsed
    assert progress_fraction(p, True, "elapsed") == 0.25
    assert progress_fraction(p, False, "remaining") == 0.75
    assert progress_fraction(Program(1, 1, 100, 0, 150), True, "remaining") == 0.0   # overtime clamps
    assert progress_fraction(Program(1), True) == 0.0


def test_progress_rect_directions(qapp):
    from PyQt6.QtCore import QRectF
    from dsan.clock.render import progress_rect
    r = QRectF(10, 20, 100, 40)
    assert progress_rect(r, 0.25, "ltr").getRect() == (10, 20, 25, 40)
    assert progress_rect(r, 0.25, "rtl").getRect() == (85, 20, 25, 40)
    assert progress_rect(r, 0.25, "btt").getRect() == (10, 50, 100, 10)
    assert progress_rect(r, 0.25, "ttb").getRect() == (10, 20, 100, 10)


def test_progress_bar_renders_each_direction(qapp):
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter
    from dsan.devices import LinkState
    from dsan.clock.render import Frame, Renderer
    st = LimitimerState(programs=(Program(1, 1, 200, 0, 100),) + _state().programs[1:])   # 50%
    green = (0, 224, 122)

    def lit(img, x, y):
        c = QColor(img.pixel(x, y))
        return abs(c.red() - green[0]) < 40 and abs(c.green() - green[1]) < 40 and abs(c.blue() - green[2]) < 40

    for direction, on, off in (("ltr", (40, 100), (160, 100)), ("rtl", (160, 100), (40, 100)),
                               ("btt", (100, 160), (100, 40)), ("ttb", (100, 40), (100, 160))):
        lay = Layout(background="#000000",
                     elements=[Element("progress", 0, 0, 1, 1, bar_direction=direction)])
        img = QImage(200, 200, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        Renderer().paint(p, QRectF(0, 0, 200, 200), lay, Frame(state=st, link=LinkState.LIVE))
        p.end()
        assert lit(img, *on) and not lit(img, *off), direction


def test_old_layouts_keep_progress_behaviour():
    d = PRESETS["Confidence monitor"]().to_dict()
    for e in d["elements"]:
        e.pop("bar_direction", None)
        e.pop("bar_fill", None)
    bar = Layout.from_dict(d).first("progress")
    assert (bar.bar_direction, bar.bar_fill) == ("ltr", "auto")


def _render_cue(qapp, element, button=None, editing=False, px_scale=1.0, size=200):
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from dsan.clock.render import CueState, Frame, Renderer
    cue = CueState()
    if button is not None:
        cue.press(button)
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    Renderer().paint(p, QRectF(0, 0, size, size), Layout(background="#00000000", elements=[element]),
                     Frame(cue=cue), editing=editing, px_scale=px_scale)
    p.end()
    return img


def _rgb(img, x, y):
    from PyQt6.QtGui import QColor
    c = QColor.fromRgba(img.pixel(x, y))
    return c.red(), c.green(), c.blue(), c.alpha()


def test_cue_frame_colours_and_pixel_width(qapp):
    e = Element("cue", 0, 0, 1, 1, cue_style="frame", frame_px=20)
    img = _render_cue(qapp, e, Button.NEXT)
    assert _rgb(img, 5, 100)[:3] == (0x00, 0xE0, 0x7A) and _rgb(img, 18, 100)[3] == 255     # inside the 20 px band
    assert _rgb(img, 22, 100)[3] == 0 and _rgb(img, 100, 100)[3] == 0                    # inside is untouched
    assert _rgb(_render_cue(qapp, e, Button.PREV), 100, 195)[:3] == (0xFF, 0x3B, 0x3B)   # red on PREV
    assert _rgb(_render_cue(qapp, e), 5, 100)[3] == 0                                    # idle on air: nothing
    assert _rgb(_render_cue(qapp, e, editing=True), 5, 100)[3] == 255                    # idle in editor: dim frame
    half = _render_cue(qapp, e, Button.NEXT, px_scale=0.5)                               # editor preview at half size
    assert _rgb(half, 8, 100)[3] == 255 and _rgb(half, 12, 100)[3] == 0


def test_cue_frame_preset_round_trips():
    lay = PRESETS["Cue frame"]()
    cue = lay.first("cue")
    assert (cue.cue_style, cue.frame_px, cue.w, cue.h) == ("frame", 40, 1.0, 1.0)
    assert Layout.from_json(lay.to_json()).first("cue").frame_px == 40

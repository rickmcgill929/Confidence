import os
import time

import pytest

pytest.importorskip("pythonosc")
from pythonosc.udp_client import SimpleUDPClient  # noqa: E402

from dsan import PerfectCue  # noqa: E402
from dsan import perfectcue as pc  # noqa: E402
from dsan.osc import OscBridge, OscListener  # noqa: E402
from dsan.sim import PerfectCueSimulator  # noqa: E402


def wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def cue():
    ports = []

    def factory():
        p = PerfectCueSimulator(interval=1000)
        ports.append(p)
        return p
    with PerfectCue(transport_factory=factory, reconnect=False) as c:
        assert wait(lambda: ports)
        c.sim = ports[0]
        yield c


def test_listener_presses_leds_and_commands(cue):
    presses, log = [], []
    cue.on_button(presses.append)
    with OscListener(0, "127.0.0.1", perfectcue=cue,
                     on_message=lambda a, args, r: log.append((a, args, r))) as lst:
        c = SimpleUDPClient("127.0.0.1", lst.port)
        n = 0

        def send(addr, *args):
            nonlocal n
            c.send_message(addr, list(args))
            n += 1
            assert wait(lambda: len(log) == n), addr
            return log[-1][2]

        assert send("/dsan/perfectcue/next") == "ok: press NEXT"
        assert send("/dsan/perfectcue/next", 0) == "ok: release ignored"     # button-up from a controller
        assert send("/dsan/perfectcue/prev", 1) == "ok: press PREV"
        assert send("/dsan/perfectcue/press", "Blank-On") == "ok: press BLANK_ON"
        assert send("/dsan/perfectcue/blank", 0) == "ok: press BLANK_OFF"
        assert send("/dsan/perfectcue/blank") == "ok: press BLANK_ON"
        assert presses == [pc.Button.NEXT, pc.Button.PREV, pc.Button.BLANK_ON, pc.Button.BLANK_OFF,
                           pc.Button.BLANK_ON]

        assert send("/dsan/perfectcue/led", "next") == "ok: LED NEXT"
        assert send("/DSAN/PerfectCue/LED/blank_off") == "ok: LED BLANK_OFF"
        assert send("/dsan/perfectcue/command/forward") == "ok: command FORWARD"
        assert send("/dsan/perfectcue/command", "blank_on") == "ok: command BLANK_ON"
        assert cue.sim.written == [b"\r", b"E", pc.Command.FORWARD.value, pc.Command.BLANK_ON.value]

        assert "unknown button" in send("/dsan/perfectcue/press", "sideways")
        assert "unknown Perfect Cue LED" in send("/dsan/perfectcue/led", "purple")
        assert "unknown address" in send("/dsan/perfectcue/fly")
        assert "ignored" in send("/other/thing", 1)
        assert lst.received == n


def test_on_press_override_and_no_device():
    got = []
    with OscListener(0, "127.0.0.1", prefix="/show/", on_press=got.append) as lst:
        assert lst.handle("/show/perfectcue/next") == "press NEXT"
        assert got == [pc.Button.NEXT]
        with pytest.raises(Exception, match="no Perfect Cue"):
            lst.handle("/show/perfectcue/led", "next")


def test_injected_press_goes_out_over_osc(cue):
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import ThreadingOSCUDPServer
    import threading
    seen = []
    d = Dispatcher()
    d.map("/dsan/perfectcue/button", lambda a, v: seen.append(v))
    srv = ThreadingOSCUDPServer(("127.0.0.1", 0), d)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bridge = OscBridge("127.0.0.1", srv.server_address[1], perfectcue=cue)
    try:
        cue.press("next")
        assert wait(lambda: seen == ["NEXT"])
    finally:
        bridge.close()
        srv.shutdown()
        srv.server_close()


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _free_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_app_osc_in(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    real = QSettings
    QSettings("TRUCUE", "DSANClockOscIn").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockOscIn"))
    w = appmod.ControlWindow()
    port = _free_port()
    w.osc_in_port.setValue(port)
    w.show_debug()
    w.osc_in_enable.setChecked(True)
    assert w.osc_in is not None and "Listening on UDP" in w.osc_in_status.text()
    c = SimpleUDPClient("127.0.0.1", port)
    c.send_message("/dsan/perfectcue/next", [])                 # no Perfect Cue connected: still shows the cue
    _pump(qapp, 0.5)
    assert w.cue.last == pc.Button.NEXT
    c.send_message("/dsan/perfectcue/blank", 1)
    c.send_message("/dsan/perfectcue/led", "next")              # needs a device
    _pump(qapp, 0.5)
    assert w.cue.blank
    assert "no Perfect Cue connected" in w.osc_in_status.text() and "3 received" in w.osc_in_status.text()
    log = w.debug_window.log.toPlainText()
    assert "PC  OSC    /dsan/perfectcue/next → ok: press NEXT" in log
    # loop guard: listening on the OSC-out target port is refused
    w.osc_in_enable.setChecked(False)
    w.osc_target.setText(f"127.0.0.1:{port}")
    w.osc_enable.setChecked(True)
    w.osc_in_enable.setChecked(True)
    assert w.osc_in is None and "loop" in w.osc_in_status.text()
    w.osc_enable.setChecked(False)
    w.close()
    w2 = appmod.ControlWindow()                                  # port is remembered
    assert w2.osc_in_port.value() == port
    w2.close()
    QSettings("TRUCUE", "DSANClockOscIn").clear()


def test_limitimer_buttons():
    from dsan.control import Key
    from dsan.emulator import LimitimerEmulator
    emu = LimitimerEmulator()
    pressed = []

    def press(key):
        pressed.append(key)
        emu.press(key)
    with OscListener(0, "127.0.0.1", on_limitimer=press) as lst:
        assert lst.handle("/dsan/limitimer/p2") == "Limitimer 2"
        assert lst.handle("/dsan/limitimer/start", 1) == "Limitimer Start / Stop"
        assert lst.handle("/dsan/limitimer/start", 0) == "release ignored"
        assert lst.handle("/dsan/Limitimer/TIME+") == "Limitimer Time +"
        assert lst.handle("/dsan/limitimer/sumup-") == "Limitimer Sum-up −"
        assert lst.handle("/dsan/limitimer/program", 4) == "Limitimer Session"
        assert lst.handle("/dsan/limitimer/program/1") == "Limitimer 1"
        assert lst.handle("/dsan/limitimer/press", "repeat") == "Limitimer Repeat"
        assert lst.handle("/dsan/limitimer/press/blink", 0) == "release ignored"
        assert pressed == [Key.PROGRAM_2, Key.START_STOP, Key.TOTAL_UP, Key.SUMUP_DOWN, Key.SESSION,
                           Key.PROGRAM_1, Key.REPEAT]
        st = emu.state()
        assert not st.programs[1].running and st.programs[1].total == 660 and st.selected_index == 0   # 2→1 stops 2
        for bad in (("/dsan/limitimer/launch",), ("/dsan/limitimer/program", 7), ("/dsan/limitimer/program",),
                    ("/dsan/limitimer/press",)):
            with pytest.raises(Exception):
                lst.handle(*bad)

    def refuse(key):
        raise ValueError("No TP-2000X connected")
    with OscListener(0, "127.0.0.1", on_limitimer=refuse) as lst:
        with pytest.raises(Exception, match="TP-2000X"):
            lst.handle("/dsan/limitimer/start")
    with OscListener(0, "127.0.0.1") as lst:
        with pytest.raises(Exception, match="isn't available"):
            lst.handle("/dsan/limitimer/start")

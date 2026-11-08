"""LIMITIMER CONTROL in the Confidence window: software Limitimer, DSAN lights, TP-2000X."""
import os
import time

import pytest

from dsan import limitimer as lt
from dsan.control import Key
from dsan.devices import LinkState


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def make_window(qapp, monkeypatch, tmp_path):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    from dsan.clock import files
    real = QSettings
    QSettings("TRUCUE", "ConfTestLT").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "ConfTestLT"))
    monkeypatch.setattr(files, "_settings", lambda: real("TRUCUE", "ConfTestLT"))
    monkeypatch.setattr(files, "last_dir", lambda kind: str(tmp_path))
    made = []

    def make():
        w = appmod.ControlWindow()
        made.append(w)
        return w
    yield make
    for w in made:
        w.close()
    QSettings("TRUCUE", "ConfTestLT").clear()


def pump(qapp, cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_parse_and_format_time():
    from dsan.clock.ltcontrol import format_time, parse_time
    assert parse_time("5") == 300 and parse_time("4:30") == 270 and parse_time("1:00:00") == 3600
    assert format_time(270) == "4:30" and format_time(3725) == "1:02:05"
    for bad in ("", "a", "1:2:3:4", "100:00:00"):
        with pytest.raises(ValueError):
            parse_time(bad)


def test_software_limitimer(qapp, make_window):
    w = make_window()
    p = w.lt_panel
    assert p.mode == "read" and p.target() is None
    assert not p.keys[Key.START_STOP].isEnabled()
    with pytest.raises(ValueError, match="TP-2000X"):
        p.press("start")
    p.set_mode("emulate")                                     # as if picked in the combo
    assert w.emulating and w.limitimer is not None and w.limitimer.port == "emulator"
    assert pump(qapp, lambda: w.lt_state is not None and w.lt_link == LinkState.LIVE)
    assert p.keys[Key.START_STOP].isEnabled() and p.keys[Key.PROGRAM_1].property("lit") == "on"
    assert "Software Limitimer" in w.lt_status.text() and w.lt_connect.text() == "Send to lights"
    assert "software Limitimer's switches" in w.dip_note.text()
    p.keys[Key.PROGRAM_2].click()
    p.keys[Key.START_STOP].click()
    assert pump(qapp, lambda: w.lt_state.selected_index == 1 and w.lt_state.selected.running)
    assert pump(qapp, lambda: p.keys[Key.START_STOP].property("lit") == "run")
    assert p.keys[Key.PROGRAM_2].property("lit") == "on" and p.keys[Key.PROGRAM_1].property("lit") == ""
    assert p.readout.text() == w.lt_state.display(2)
    p.keys[Key.TOTAL_UP].click()
    assert pump(qapp, lambda: w.lt_state.selected.total == 660)
    rs = p.remote_state()
    assert rs["mode"] == "emulate" and rs["target"] == "emulator" and rs["selected"] == 2
    assert rs["programs"][1] == {"total": 660, "sumup": 120} and rs["lamps"]["running"]

    # remembered: a new window starts the software Limitimer by itself
    w._save_settings()
    w.close()
    w2 = make_window()
    assert w2.lt_panel.mode == "emulate" and w2.limitimer is not None
    assert w2.lt_panel.emulator.programs()[1] == (660, 120)
    w2.lt_panel.set_mode("read")
    assert w2.limitimer is None and not w2.emulating


def test_drive_dsan_lights(qapp, make_window, monkeypatch):
    import dsan.clock.app as appmod
    from dsan.clock import ports as portsmod
    from dsan.emulator import LimitimerBus
    from test_emulator import Wire
    wire = Wire()

    def fake_bus(emu, port):
        b = LimitimerBus(emu, transport_factory=lambda: wire, reconnect=False)
        b.port = port
        return b
    monkeypatch.setattr(appmod, "LimitimerBus", fake_bus)
    w = make_window()
    w.lt_panel.set_mode("emulate")
    w.refs["lt"] = portsmod.PortRef.url("socket://lights:4001")
    w._connect_clicked("lt", True)
    assert w.lt_bus is not None and w.lt_connect.text() == "Stop sending"
    assert pump(qapp, lambda: w.lt_bus.sent > 5)
    w._update_activity()
    assert "Sending to DSAN lights" in w.lt_status.text()
    events = lt.Decoder().feed(bytes(wire.out))
    assert events and all(e.ok for e in events)
    assert lt.parse_status(events[0].payload).programs[0].total == 300
    wire.inbox += lt.encode_status(lt.LimitimerState(programs=tuple(lt.Program(i + 1, 0, 7) for i in range(4))))
    assert pump(qapp, lambda: w.lt_bus.other_console)
    w._update_activity()
    assert "Another Limitimer" in w.lt_status.text()
    w._connect_clicked("lt", False)
    assert w.lt_bus is None and w.limitimer is not None          # the timer keeps running, not sending


def test_tp2000x(qapp, make_window, monkeypatch):
    from dsan.clock import ltcontrol
    from dsan.clock import ports as portsmod
    from dsan.control import LimitimerControl
    from test_control import FakeTP, msg
    tp = FakeTP()
    monkeypatch.setattr(ltcontrol, "LimitimerControl",
                        lambda port: LimitimerControl(transport_factory=lambda: tp, reconnect=False))
    w = make_window()
    p = w.lt_panel
    p.ref = portsmod.PortRef.url("socket://tp2000x:5000")
    p._connect_clicked(True)
    assert pump(qapp, lambda: p.target() == "tp2000x")
    assert "waiting for the TP-2000X" in p.status.text()
    p.keys[Key.SESSION].click()
    p.press("start")
    assert bytes(tp.written) == b">SESS 5E\r>STOP 66\r"
    tp.inbox += msg("SESLEDON") + msg("P1LEDDM") + msg("REDLEDON") + msg("RTSTR=00:42")
    assert pump(qapp, lambda: p.readout.text() == "00:42")
    assert "buttons press the Limitimer" in p.status.text()
    assert p.keys[Key.SESSION].property("lit") == "on" and p.keys[Key.PROGRAM_1].property("lit") == "dim"
    assert p.remote_state()["lamps"]["red"]
    p._connect_clicked(False)
    assert p.control is None and p.target() is None


def test_remote_api(qapp, make_window):
    from dsan.clock.remote import RemoteAPI, RemoteError
    w = make_window()
    api = RemoteAPI(w)
    st = api.state()["limitimer"]
    assert st["mode"] == "read" and st["target"] is None and len(st["keys"]) == 14
    assert {"value": "start_stop", "label": "Start / Stop"} in st["keys"]
    with pytest.raises(RemoteError, match="TP-2000X"):
        api.act({"action": "limitimer", "key": "start"})
    with pytest.raises(RemoteError, match="software Limitimer"):
        api.act({"action": "limitimer", "program": 1, "total": "4:00"})
    st = api.act({"action": "limitimer", "mode": "emulate"})["limitimer"]
    assert st["mode"] == "emulate" and st["target"] == "emulator" and w.emulating
    st = api.act({"action": "limitimer", "program": 3, "total": "7:30", "sumup": "1:15"})["limitimer"]
    assert st["programs"][2] == {"total": 450, "sumup": 75}
    api.act({"action": "limitimer", "key": "program_3"})
    api.act({"action": "limitimer", "key": "start_stop"})
    assert pump(qapp, lambda: w.lt_state is not None and w.lt_state.selected_index == 2 and w.lt_state.selected.running)
    assert pump(qapp, lambda: api.state()["limitimer"]["lamps"]["running"])
    for bad in ({"key": "launch"}, {"mode": "both"}, {"program": 9, "total": "1:00"}, {"program": 1, "total": "abc"}):
        with pytest.raises(RemoteError):
            api.act({"action": "limitimer", **bad})
    assert api.state()["inputs"]["lt"]["button"] == "Send to lights"

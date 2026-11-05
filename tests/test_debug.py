import os
import time

import pytest

from dsan import Limitimer, PerfectCue
from dsan import limitimer as lt
from dsan.sim import LimitimerSimulator, PerfectCueSimulator


def test_annotate_status_rows():
    st = lt.LimitimerState(selected_index=1, program_minutes=True, beep_type=lt.BeepType.RING,
                           programs=(lt.Program(1, 0x05, 300, 30, 100),) + lt.LimitimerState().programs[1:])
    payload = lt.Decoder().feed(lt.encode_status(st))[0].payload
    rows = {r[2]: r for r in lt.annotate_status(payload)}
    assert rows["type"][3] == "status"
    assert "→ Ring" in rows["config low"][3] and "P1-3 MM:SS on (DIP 3)" in rows["config low"][3]
    assert rows["selected"][3] == "program 2"
    assert rows["P1 flags"][3] == "running, beep"
    assert rows["P1 total"] == ("07-09", "00 02 2C", "P1 total", "300 s (5:00)")
    assert rows["P4 elapsed"][0] == "46-48"
    assert len(lt.annotate_status(payload)) == 1 + 4 + 4 * 5 + 1


def test_annotate_ping_and_unknown_bits():
    assert lt.annotate_status(bytes([lt.PING_MSG])) == [("00", "10", "type", "ping")]
    payload = bytearray(lt.Decoder().feed(lt.encode_status(lt.LimitimerState()))[0].payload)
    payload[2] |= 0x01
    payload[5] |= 0x10
    rows = {r[2]: r for r in lt.annotate_status(bytes(payload))}
    assert "unknown bits 0x01" in rows["config low"][3]
    assert "unknown 0x10" in rows["P1 flags"][3]


class _Corrupt(LimitimerSimulator):
    """Every third status packet has a broken CRC."""
    def _produce(self):
        out = bytearray(super()._produce())
        if self._count % 3 == 0:
            out[52] ^= 0xFF
        return bytes(out)


class _CueWithNoise(PerfectCueSimulator):
    PATTERN = PerfectCueSimulator.PATTERN

    def _produce(self):
        return super()._produce() + b"\x55"          # a stray byte after each press


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


def test_debug_window_end_to_end(qapp, tmp_path):
    from dsan.clock.debug import DebugTap, DebugWindow
    tap = DebugTap()
    ltd = Limitimer(transport_factory=lambda: _Corrupt(rate_hz=30), reconnect=False)
    pcd = PerfectCue(transport_factory=lambda: _CueWithNoise(interval=0.15), reconnect=False)
    tap.attach_limitimer(ltd)
    tap.attach_perfectcue(pcd)
    w = DebugWindow(tap, lambda: (ltd, pcd))
    w.f_ping.setChecked(True)
    w.show()
    assert tap.enabled
    ltd.start()
    pcd.start()
    try:
        _pump(qapp, 1.2)
        text = w.log.toPlainText()
        assert "LT  PKT    status · 55 bytes · CRC ok" in text
        assert "LT  ERR    CRC mismatch" in text
        assert "LT  STATE  ↓ sel P1" in text
        assert "LT  LINK   live" in text
        assert "PC  BTN    NEXT (0x0F)" in text
        assert "PC  ???    0x55" in text
        assert " RAW " not in text                          # raw off by default
        assert w.annot.topLevelItemCount() == 26            # breakdown of the last status packet
        w._update_stats()
        assert "packets/s" in w.stat["lt_rate"].text() and "bad" in w.stat["lt_errors"].text()
        assert "unknown" in w.stat["pc_counts"].text() and "NEXT" in w.stat["pc_last"].text()

        # raw bytes on
        w.f_raw.setChecked(True)
        _pump(qapp, 0.4)
        raw = [l for l in w.log.toPlainText().splitlines() if "  LT  RAW  " in l]
        stream = " ".join(l.split("RAW", 1)[1].strip() for l in raw)   # chunks split anywhere, like a real port
        assert "81 00 21 48" in stream

        # filters: hide the Perfect Cue
        w._clear()
        w.f_pc.setChecked(False)
        _pump(qapp, 0.5)
        assert " PC " not in w.log.toPlainText() and " LT " in w.log.toPlainText()

        # pause keeps the log still
        w.pause_btn.setChecked(True)
        n = w.log.blockCount()
        _pump(qapp, 0.4)
        assert w.log.blockCount() == n
        w.pause_btn.setChecked(False)

        # save + raw capture
        w.save_log(str(tmp_path / "log.txt"))
        assert "LT" in (tmp_path / "log.txt").read_text()
        w.start_record(str(tmp_path / "cap.txt"))
        _pump(qapp, 0.5)
        w._stop_record()
        lines = (tmp_path / "cap.txt").read_text().splitlines()
        assert lines[0].startswith("# Confidence ") and "raw capture" in lines[0]   # header carries the version
        lt_stream = " ".join(l.split(" LT ", 1)[1] for l in lines if " LT " in l)
        pc_stream = " ".join(l.split(" PC ", 1)[1] for l in lines if " PC " in l)
        assert "81 00 21 48" in lt_stream and "0f" in pc_stream

        # closed window stops collecting
        w.hide()
        assert not tap.enabled
    finally:
        ltd.stop()
        pcd.stop()
        w.close()


def test_control_window_debug_console(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockDebug").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockDebug"))
    w = appmod.ControlWindow()
    w.show_debug()
    assert w.debug_window.isVisible() and w.debug_tap.enabled
    w.demo_btn.click()
    _pump(qapp, 1.0)
    assert "LT  STATE" in w.debug_window.log.toPlainText()
    w.debug_window.f_raw.setChecked(True)
    w.close()                                               # closes the console too
    assert not w.debug_window.isVisible()
    w2 = appmod.ControlWindow()
    w2.show_debug()
    assert w2.debug_window.f_raw.isChecked()                # filter choices remembered
    w2.close()
    QSettings("TRUCUE", "DSANClockDebug").clear()


def test_debug_shortcut_and_devices_menu(qapp, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockDebugKey").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockDebugKey"))
    w = appmod.ControlWindow()
    assert w.debug_action.shortcut().toString() == "Ctrl+D"            # ⌘D on macOS
    menus = {a.text(): a.menu() for a in w.menuBar().actions()}
    dev = [a.text() for a in menus["Devices"].actions() if a.text()]
    assert dev == ["Detect adapters", "Swap Limitimer and Perfect Cue adapters", "Debug console"]
    edit_keys = {a.text(): a.shortcut().toString() for a in menus["Edit"].actions()}
    assert edit_keys["Duplicate"] == "Ctrl+Shift+D"                     # no clash with ⌘D
    from PyQt6.QtWidgets import QPushButton
    texts = {b.text() for b in w.findChildren(QPushButton)}
    assert not texts & {"Detect", "Swap", "Debug…"} and "Demo" in texts
    w.debug_action.trigger()
    assert w.debug_window.isVisible()
    w.close()
    QSettings("TRUCUE", "DSANClockDebugKey").clear()

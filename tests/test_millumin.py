import time

import pytest

from dsan.millumin import MilluminTracker, format_time


def test_format_time():
    assert format_time(None) == "--:--"
    assert format_time(65.9) == "1:05"
    assert format_time(65.1, round_up=True) == "1:06"
    assert format_time(0.0, round_up=True) == "0:00"
    assert format_time(3723) == "1:02:03"


def test_selected_layer_countdown_and_states():
    t = MilluminTracker()
    assert t.view() is None
    assert not t.handle("/other/thing", 1)
    t.handle("/millumin/layer:Video/selected", now=0)
    t.handle("/millumin/layer:Video/mediaStarted", 2, "intro.mov", 90.0, now=0)
    v = t.view(now=0)
    assert (v.layer, v.clip, v.duration, v.state, v.mode) == ("Video", "intro.mov", 90.0, "playing", "")
    t.handle("/millumin/selectedLayer/media/time", 30.0, 90.0, now=10)
    v = t.view(now=10.5)                                    # runs on between messages
    assert v.elapsed == pytest.approx(30.5) and v.remaining == pytest.approx(59.5)
    assert v.fraction == pytest.approx(30.5 / 90)
    t.handle("/millumin/layer:Video/mediaPaused", 2, "intro.mov", now=11)
    v = t.view(now=20)
    assert v.state == "paused" and v.status == "PAUSED" and v.elapsed == pytest.approx(31.0)
    t.handle("/millumin/layer:Video/mediaStarted", 2, "intro.mov", 90.0, now=21)   # resume keeps the time
    assert t.view(now=21).elapsed == pytest.approx(31.0)
    assert t.view(now=21.5).live and not t.view(now=40).live
    t.handle("/millumin/layer:Video/mediaStopped", 2, "intro.mov", now=22)
    v = t.view(now=30)                                    # held on the current column, frozen where it stopped
    assert v.clip == "intro.mov" and v.status == "STOPPED" and v.elapsed == pytest.approx(32.0)
    assert t.view(now=60).remaining == pytest.approx(58.0)
    assert t.layers() == ["Video"]
    t.handle("/millumin/layer:Video/mediaStarted", 2, "outro.mov", 45.0, now=24)
    v = t.view(now=24)
    assert v.clip == "outro.mov" and v.duration == 45.0 and v.elapsed == 0


def test_loop_is_detected_from_the_time_jump():
    t = MilluminTracker()
    t.handle("/millumin/layer:BG/selected", now=0)
    t.handle("/millumin/layer:BG/mediaStarted", 1, "loop.mov", 10.0, now=0)
    for i, x in enumerate((8.0, 9.0, 9.9, 0.1, 1.0)):
        t.handle("/millumin/selectedLayer/media/time", x, 10.0, now=1 + i)
    v = t.view(now=5)
    assert v.mode == "loop" and v.loops == 1 and v.status == "PLAYING"
    # dead-reckoning past the end wraps for a looping clip instead of freezing
    assert t.view(now=5 + 9.5).elapsed == pytest.approx(0.5)
    # a seek backwards mid-clip is not a loop
    t2 = MilluminTracker()
    t2.handle("/millumin/selectedLayer/media/time", 5.0, 10.0, now=0)
    t2.handle("/millumin/selectedLayer/media/time", 2.0, 10.0, now=1)
    assert t2.view(now=1).mode == "" and t2.view(now=1).loops == 0


def test_freeze_when_held_on_last_frame():
    t = MilluminTracker()
    t.handle("/millumin/layer:L/selected", now=0)
    t.handle("/millumin/layer:L/mediaStarted", 1, "end.mov", 10.0, now=0)
    t.handle("/millumin/selectedLayer/media/time", 9.6, 10.0, now=9.6)
    t.handle("/millumin/selectedLayer/media/time", 10.0, 10.0, now=10)
    t.handle("/millumin/selectedLayer/media/time", 10.0, 10.0, now=10.3)
    assert t.view(now=10.3).mode == ""                      # not long enough yet
    t.handle("/millumin/selectedLayer/media/time", 10.0, 10.0, now=10.8)
    v = t.view(now=10.8)
    assert v.mode == "freeze" and v.status == "FREEZE" and v.remaining == 0
    # new clip clears it
    t.handle("/millumin/layer:L/mediaStarted", 1, "next.mov", 20.0, now=11)
    v = t.view(now=11)
    assert v.mode == "" and v.clip == "next.mov" and v.duration == 20.0


def test_named_layer_columns_and_unknown():
    t = MilluminTracker()
    t.handle("/millumin/layer:A/mediaStarted", 1, "a.mov", 60, now=0)
    t.handle("/millumin/layer:B/mediaStarted", 3, "b.mov", now=0)            # no duration
    t.handle("/millumin/index:3/mediaStarted", 3, "b.mov", now=0)             # duplicate, ignored
    t.handle("/millumin/board/launchedColumn", 4, "Walk-in", now=0)
    assert t.layers() == ["A", "B"]
    a = t.view("A", now=12)
    assert a.elapsed == pytest.approx(12) and a.column == "Walk-in"            # runs on from mediaStarted
    b = t.view("B", now=12)
    assert b.duration is None and b.remaining is None and b.fraction is None
    assert t.view(now=1) is None                                               # no selected layer yet
    t.handle("/millumin/selectedLayer/media/normalizedTime", 0.5, now=1)       # no duration known → ignored
    t.handle("/millumin/selectedLayer/media/speed", 2.0, now=1)
    assert t.view("A", now=2).column == "Walk-in"
    t.handle("/millumin/board/stoppedColumn", 4, "Walk-in", now=3)
    assert t.view("A", now=3) is None and t.view("B", now=3) is None    # column stopped clears every layer
    assert not t.handle("/millumin/layer:A/somethingNew", 1, now=2)
    assert t.unknown[-1] == ("/millumin/layer:A/somethingNew", (1,))


def test_receiver_over_udp():
    pytest.importorskip("pythonosc")
    from pythonosc.udp_client import SimpleUDPClient
    from dsan.millumin import MilluminReceiver
    seen = []
    with MilluminReceiver(0, "127.0.0.1", on_message=lambda a, args, used: seen.append((a, used))) as rx:
        c = SimpleUDPClient("127.0.0.1", rx.port)
        c.send_message("/millumin/layer:Main/selected", [])
        c.send_message("/millumin/layer:Main/mediaStarted", [1, "show.mov", 120.0])
        c.send_message("/millumin/selectedLayer/media/time", [60.0, 120.0])
        end = time.monotonic() + 3
        while len(seen) < 3 and time.monotonic() < end:
            time.sleep(0.01)
        v = rx.tracker.view()
        assert v.clip == "show.mov" and 59 < v.remaining <= 60
        assert all(used for _a, used in seen)


def test_layout_tokens_and_phase():
    from dsan.clock.layout import expand_tokens, millumin_phase
    t = MilluminTracker()
    t.handle("/millumin/layer:Main/selected", now=0)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "keynote.mov", 125.0, now=0)
    t.handle("/millumin/board/launchedColumn", 2, "Keynote", now=0)
    v = t.view(now=100.2)
    text = expand_tokens("{clip} [{layer}] {column} {trt} -{remaining} {elapsed} {state} {playmode}", None, None, v)
    assert text == "keynote.mov [Main] Keynote 2:05 -0:25 1:40 PLAYING "
    assert millumin_phase(v) == "warning"                         # 25 s left, yellow from 30
    assert millumin_phase(t.view(now=116)) == "expired"           # 9 s left, red from 10
    assert millumin_phase(t.view(now=10)) == "running"
    assert millumin_phase(t.view(now=10), yellow=120) == "warning"
    assert millumin_phase(None) == "idle"
    assert expand_tokens("{clip}{remaining}{trt}{state}", None, None, None) == ""


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_renderer_millumin_elements(qapp):
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from dsan.clock.layout import PRESETS
    from dsan.clock.render import Frame, Renderer
    t = MilluminTracker()
    t.handle("/millumin/layer:Main/selected", now=0)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "keynote.mov", 125.0, now=0)
    lay = PRESETS["Millumin countdown"]()
    img = QImage(640, 360, QImage.Format.Format_ARGB32)
    for now in (10.0, 120.0):
        img.fill(0)
        p = QPainter(img)
        Renderer().paint(p, QRectF(0, 0, 640, 360), lay, Frame(millumin=t.view(now=now), now=now))
        p.end()
    # bar fills from the left (elapsed): left end lit, right end not, at 120/125 s it's nearly full
    y = int(360 * (0.69 + 0.0175))
    assert img.pixelColor(int(640 * 0.06), y) != img.pixelColor(int(640 * 0.06), 5)
    blank = QImage(640, 360, QImage.Format.Format_ARGB32)       # nothing on the layer: timer and labels blank
    blank.fill(0)
    p = QPainter(blank)
    Renderer().paint(p, QRectF(0, 0, 640, 360), lay, Frame())
    p.end()
    timer_box = [blank.pixelColor(x, y) for x in range(40, 600, 7) for y in range(70, 230, 7)]
    assert len({(c.red(), c.green(), c.blue()) for c in timer_box}) == 1     # one flat colour: no digits


def test_app_millumin_box(qapp, monkeypatch):
    pytest.importorskip("pythonosc")
    import socket
    from PyQt6.QtCore import QSettings
    from pythonosc.udp_client import SimpleUDPClient
    import dsan.clock.app as appmod
    real = QSettings
    QSettings("TRUCUE", "DSANClockMM").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockMM"))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    w = appmod.ControlWindow()
    w.mm_port.setValue(port)
    w.show_debug()
    w.mm_enable.setChecked(True)
    assert w.mm_rx is not None and f"port {port}" in w.mm_hint.text()
    w._update_millumin()
    assert "Waiting for Millumin" in w.mm_status.text()
    c = SimpleUDPClient("127.0.0.1", port)
    c.send_message("/millumin/layer:Screen/selected", [])
    c.send_message("/millumin/layer:Screen/mediaStarted", [1, "walkin.mov", 300.0])
    c.send_message("/millumin/layer:Logo/mediaStarted", [2, "logo.png"])
    c.send_message("/millumin/selectedLayer/media/time", [100.0, 300.0])
    _pump(qapp, 0.6)
    w._update_millumin()
    assert "walkin.mov — PLAYING · 3:20 left" in w.mm_status.text() or "3:19 left" in w.mm_status.text()
    assert [w.mm_layer.itemText(i) for i in range(w.mm_layer.count())] == ["Selected layer", "Logo", "Screen"]
    f = w.frame()
    assert f.millumin.clip == "walkin.mov" and f.mm_yellow == 30 and f.mm_red == 10
    w.mm_layer.setCurrentText("Logo")
    assert w.frame().millumin.clip == "logo.png"
    log = w.debug_window.log.toPlainText()
    assert "MM  OSC    /millumin/layer:Screen/mediaStarted 1 'walkin.mov' 300.000" in log
    assert "/media/time" not in log                               # time feed only with Raw bytes
    w.close()
    w2 = appmod.ControlWindow()                                    # remembered, and listening again
    assert w2.mm_enable.isChecked() and w2.mm_layer_name() == "Logo" and w2.mm_port.value() == port
    w2.close()
    QSettings("TRUCUE", "DSANClockMM").clear()


def test_columns_hold_and_switch():
    t = MilluminTracker()
    t.handle("/millumin/layer:Main/selected", now=0)
    t.handle("/millumin/board/launchedColumn", 1, "Walk-in", now=0)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "walkin.mov", 20.0, now=0)
    t.handle("/millumin/layer:Main/mediaStopped", 1, "walkin.mov", now=20)          # played out
    v = t.view(now=100)
    assert v.clip == "walkin.mov" and v.remaining == 0 and v.status == "STOPPED"     # held at 0:00

    # advance: the new clip's mediaStarted can arrive just before launchedColumn — it must survive
    t.handle("/millumin/layer:Main/mediaStarted", 1, "keynote.mov", 600.0, now=200)
    t.handle("/millumin/board/launchedColumn", 2, "Keynote", now=200.05)
    t.handle("/millumin/layer:Main/mediaStopped", 1, "walkin.mov", now=200.1)       # late stop for the old clip
    v = t.view(now=210)
    assert v.clip == "keynote.mov" and v.status == "PLAYING" and v.column == "Keynote"
    assert v.elapsed == pytest.approx(10.0)

    # a column with nothing on this layer blanks it
    t.handle("/millumin/board/launchedColumn", 3, "Break", now=300)
    t.handle("/millumin/layer:Main/mediaStopped", 1, "keynote.mov", now=300.02)
    assert t.view(now=301) is None
    from dsan.clock.layout import expand_tokens
    assert expand_tokens("{clip}{trt}{state}", None, None, t.view(now=301)) == ""

    # and the next column brings it back
    t.handle("/millumin/board/launchedColumn", 4, "Q&A", now=400)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "qa.mov", 30.0, now=400.1)
    assert t.view(now=401).clip == "qa.mov"


def test_advance_with_late_stop_of_previous_column():
    """Millumin can send stoppedColumn for the old column after launching the new one."""
    t = MilluminTracker()
    t.handle("/millumin/layer:Main/selected", now=0)
    t.handle("/millumin/board/launchedColumn", 1, "Walk-in", now=0)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "walkin.mov", 20.0, now=0)
    # advance to column 2, in the order Millumin can send it
    t.handle("/millumin/board/launchedColumn", 2, "Keynote", now=50)
    t.handle("/millumin/layer:Main/mediaStopped", 1, "walkin.mov", now=50.01)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "keynote.mov", 600.0, now=50.02)
    t.handle("/millumin/board/stoppedColumn", 1, "Walk-in", now=50.03)            # old column stops late
    for now in (50.05, 51, 55):
        v = t.view(now=now)
        assert v is not None and v.clip == "keynote.mov" and v.column == "Keynote", now
    # a repeated launchedColumn for the current column doesn't blank the playing clip
    t.handle("/millumin/board/launchedColumn", 2, "Keynote", now=60)
    assert t.view(now=61).clip == "keynote.mov"
    # stopping the current column does blank it
    t.handle("/millumin/board/stoppedColumn", 2, "Keynote", now=70)
    assert t.view(now=71) is None


def test_stopped_column_before_launch_keeps_new_media():
    t = MilluminTracker()
    t.handle("/millumin/layer:Main/selected", now=0)
    t.handle("/millumin/board/launchedColumn", 1, "A", now=0)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "a.mov", 20.0, now=0)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "b.mov", 30.0, now=10)
    t.handle("/millumin/board/stoppedColumn", 1, "A", now=10.01)
    t.handle("/millumin/board/launchedColumn", 2, "B", now=10.02)
    assert t.view(now=11).clip == "b.mov"


def test_limitimer_millumin_preset(qapp):
    """Both halves: Limitimer on top always; the Millumin half empties when the column has nothing."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from dsan.clock.layout import PRESETS, expand_tokens
    from dsan.clock.render import Frame, Renderer
    from dsan.devices import LinkState
    from dsan.limitimer import LimitimerState, Program
    lay = PRESETS["Limitimer + Millumin"]()
    assert lay.background == "#000000"
    assert lay.elements[0].type == "cue"                           # frame drawn underneath everything
    t = MilluminTracker()
    t.handle("/millumin/layer:Main/selected", now=0)
    t.handle("/millumin/layer:Main/mediaStarted", 1, "keynote.mov", 125.0, now=0)
    t.handle("/millumin/selectedLayer/launchedColumn", 3, now=0)
    v = t.view(now=10.0)
    texts = {e.name: expand_tokens(e.text, None, None, v) for e in lay.elements if e.type == "label"}
    assert texts["Millumin Clip Name"] == "keynote.mov" and texts["Millumin TRT"] == "2:05"
    assert texts["Millumin State"] == "PLAYING" and texts["Millumin Column"] == expand_tokens("{column}", None, None, v)
    st = LimitimerState(programs=(Program(1, 1, 300, 30, 100),), selected_index=0)

    def render(view):
        img = QImage(640, 360, QImage.Format.Format_ARGB32)
        img.fill(0xFF000000)
        p = QPainter(img)
        Renderer().paint(p, QRectF(0, 0, 640, 360), lay, Frame(state=st, link=LinkState.LIVE, millumin=view, now=10.0))
        p.end()
        return img

    def lit(img, y0, y1):
        return sum(1 for y in range(int(360 * y0), int(360 * y1), 3) for x in range(30, 610, 3)
                   if img.pixelColor(x, y).lightness() > 60)
    full, empty = render(v), render(None)
    assert lit(full, 0.13, 0.41) > 100 and lit(empty, 0.13, 0.41) > 100      # Limitimer digits both times
    assert lit(full, 0.59, 0.87) > 100 and lit(empty, 0.59, 0.87) == 0       # Millumin countdown only with a clip

import json
import os
import socket
import time

import pytest

from dsan.trucue import TruCueTracker, split_markers

STATUS = {"clip": "Main Show 🔁B", "index": 2, "remaining": 125.4, "playing": 1, "next": "Q&A →",
          "prev": "Walk-in", "vol": -3.5, "bmName": "Applause", "bmRemaining": 12.0}


def status(**kw):
    return json.dumps({**STATUS, **kw})


def test_markers():
    assert split_markers("Main Show 🔁B") == ("Main Show", "loop", True)
    assert split_markers("Intro →") == ("Intro", "auto-next", False)
    assert split_markers("Walk-in ↺") == ("Walk-in", "auto-first", False)
    assert split_markers("Plan B") == ("Plan B", "", False)          # a real name ending in B stays
    assert split_markers("") == ("", "", False)


def test_tracker_countdown_bookmark_and_estimate():
    t = TruCueTracker()
    assert t.view() is None
    assert not t.handle("/trucue/other", "{}") and not t.handle("/trucue/status", 5)
    assert not t.handle("/trucue/status", "not json")
    t.handle("/trucue/status", status(remaining=125.4), now=0)
    v = t.view(now=0)
    assert (v.clip, v.clip_raw, v.index, v.mode, v.cue_in_black) == ("Main Show", "Main Show 🔁B", 2, "loop", True)
    assert v.next == "Q&A" and v.prev == "Walk-in" and v.volume == -3.5 and v.status == "PLAYING"
    v = t.view(now=2.0)                                             # runs on between messages
    assert v.remaining == pytest.approx(123.4) and v.bookmark_remaining == pytest.approx(10.0)
    t.handle("/trucue/status", status(remaining=100.0, playing=0, bmRemaining=None, bmName=""), now=30)
    v = t.view(now=40)
    assert v.status == "PAUSED" and v.remaining == 100.0 and v.bookmark == "" and v.bookmark_remaining is None
    assert v.duration == 125.4 and v.elapsed == pytest.approx(25.4)  # length estimated from the first sample
    assert v.fraction == pytest.approx(25.4 / 125.4)
    t.handle("/trucue/status", status(clip="Outro", index=3, remaining=40.0), now=50)   # new clip
    assert t.view(now=50).duration == 40.0 and t.view(now=50).mode == ""
    t.handle("/trucue/status", json.dumps({"clip": "", "index": 0, "remaining": 0, "playing": 0}), now=60)
    assert t.view(now=60) is None                                    # nothing loaded
    t.handle("/trucue/status/playlist", '["A","B",null]', now=61)
    assert t.playlist == ["A", "B", ""]
    assert t.view(now=61) is None and t.received == 5
    t.handle("/trucue/status", status(), now=62)
    assert t.view(now=62).live and not t.view(now=70).live


def test_tokens_and_phase():
    from dsan.clock.layout import expand_tokens, media_phase
    t = TruCueTracker()
    t.handle("/trucue/status", status(remaining=25.2), now=0)
    v = t.view(now=0)
    text = expand_tokens("{tc_clip}|{tc_index}|{tc_next}|{tc_remaining}|{tc_state}|{tc_playmode}|"
                         "{tc_bookmark}|{tc_bookmark_remaining}|{tc_volume}", None, None, None, v)
    assert text == "Main Show|2|Q&A|0:26|PLAYING|LOOP|Applause|0:12|-3.5 dB"
    assert media_phase(v) == "running"                              # looping clip: never warns
    t.handle("/trucue/status", status(clip="Talk", remaining=25.2), now=1)
    assert media_phase(t.view(now=1)) == "warning" and media_phase(t.view(now=17)) == "expired"
    assert expand_tokens("{tc_clip}{tc_remaining}", None, None, None, None) == ""
    assert expand_tokens("{clip}", None, None, None, v) == ""         # Millumin tokens stay Millumin's


def test_receiver_forwards_to_companion():
    pytest.importorskip("pythonosc")
    from pythonosc.udp_client import SimpleUDPClient
    from dsan.trucue import TruCueReceiver
    comp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)          # stands in for Companion
    comp.bind(("127.0.0.1", 0))
    comp.settimeout(2)
    seen = []
    with TruCueReceiver(0, "127.0.0.1", forward=comp.getsockname(),
                        on_message=lambda a, args, used: seen.append((a, used))) as rx:
        SimpleUDPClient("127.0.0.1", rx.port).send_message("/trucue/status", status())
        data, _ = comp.recvfrom(65536)                               # Companion still gets it, unchanged
        assert b"/trucue/status" in data and "Main Show".encode() in data
        end = time.monotonic() + 2
        while not seen and time.monotonic() < end:
            time.sleep(0.01)
        assert seen == [("/trucue/status", True)] and rx.tracker.view().clip == "Main Show"
        assert rx.forwarded == 1
    comp.close()


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_preset_renders(qapp):
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from dsan.clock.layout import PRESETS
    from dsan.clock.render import Frame, Renderer
    t = TruCueTracker()
    t.handle("/trucue/status", status(clip="Talk", remaining=200.0), now=0)
    t.handle("/trucue/status", status(clip="Talk", remaining=20.0), now=1)
    lay = PRESETS["TruCue countdown"]()
    img = QImage(640, 360, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    Renderer().paint(p, QRectF(0, 0, 640, 360), lay, Frame(trucue=t.view(now=1), now=1))
    p.end()
    # 20 s left of an estimated 200 s: progress bar 90 % full from the left, timer yellow
    y = int(360 * (0.66 + 0.0175))
    assert img.pixelColor(int(640 * 0.06), y) != img.pixelColor(int(640 * 0.94), y)
    timer = [img.pixelColor(x, y2) for x in range(40, 600, 6) for y2 in range(70, 220, 6)]
    assert any(c.red() > 200 and c.green() > 150 and c.blue() < 60 for c in timer)   # yellow digits


def test_app_trucue_box(qapp, monkeypatch):
    pytest.importorskip("pythonosc")
    from PyQt6.QtCore import QSettings
    from pythonosc.udp_client import SimpleUDPClient
    import dsan.clock.app as appmod
    real = QSettings
    QSettings("TRUCUE", "ConfTestTC").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "ConfTestTC"))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    w = appmod.ControlWindow()
    w.tc_port.setValue(port)
    w.tc_forward.setText(f"127.0.0.1:{port}")                         # loop → refused
    w.tc_enable.setChecked(True)
    assert w.tc_rx is None and "loop" in w.tc_status.text()
    w.tc_forward.setText("nonsense")
    w.tc_enable.setChecked(True)
    assert w.tc_rx is None and "host:port" in w.tc_status.text()
    w.tc_forward.setText("")
    w.show_debug()
    w.tc_enable.setChecked(True)
    assert w.tc_rx is not None and f"port {port}" in w.tc_hint.text()
    c = SimpleUDPClient("127.0.0.1", port)
    for rem in (90.0, 89.9, 89.8):
        c.send_message("/trucue/status", status(clip="Keynote", remaining=rem))
    end = time.monotonic() + 2
    while w.tc_rx.tracker.received < 3 and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    for _ in range(20):
        qapp.processEvents()
        time.sleep(0.01)
    w._update_trucue()
    assert "Keynote — PLAYING" in w.tc_status.text()
    f = w.frame()
    assert f.trucue.clip == "Keynote" and f.tc_yellow == 30
    log = [l for l in w.debug_window.log.toPlainText().splitlines() if "TC  OSC" in l]
    assert len(log) == 1 and "Keynote" in log[0]                       # repeats are folded unless Raw is on
    w.close()
    w2 = appmod.ControlWindow()
    assert w2.tc_enable.isChecked() and w2.tc_port.value() == port
    w2.close()
    QSettings("TRUCUE", "ConfTestTC").clear()


def test_limitimer_trucue_preset(qapp):
    """Same look as Limitimer + Millumin, fed by TRUCUE; the lower half empties with no clip."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from dsan.clock.layout import PRESETS, expand_tokens
    from dsan.clock.render import Frame, Renderer
    from dsan.devices import LinkState
    from dsan.limitimer import LimitimerState, Program
    tc, mm = PRESETS["Limitimer + TruCue"](), PRESETS["Limitimer + Millumin"]()
    assert [(e.type, e.x, e.y, e.w, e.h, e.color) for e in tc.elements] == \
           [(e.type, e.x, e.y, e.w, e.h, e.color) for e in mm.elements]
    assert tc.background == "#000000" and all(e.source in ("trucue", "selected") for e in tc.elements)
    t = TruCueTracker()
    t.handle("/trucue/status", status(clip="Keynote", remaining=200.0), now=0)
    t.handle("/trucue/status", status(clip="Keynote", remaining=90.0), now=1)
    v = t.view(now=1)
    texts = {e.name: expand_tokens(e.text, None, None, trucue=v) for e in tc.elements if e.type == "label"}
    assert texts["TruCue Clip Name"] == "Keynote" and texts["TruCue TRT"] == "3:20"
    assert texts["TruCue State"] == "PLAYING" and texts["TruCue Label"] == "TruCue"
    assert texts["TruCue Clip #"] == str(v.index)
    st = LimitimerState(programs=(Program(1, 1, 300, 30, 100),), selected_index=0)

    def render(view):
        img = QImage(640, 360, QImage.Format.Format_ARGB32)
        img.fill(0xFF000000)
        p = QPainter(img)
        Renderer().paint(p, QRectF(0, 0, 640, 360), tc, Frame(state=st, link=LinkState.LIVE, trucue=view, now=1))
        p.end()
        return img

    def lit(img, y0, y1):
        return sum(1 for y in range(int(360 * y0), int(360 * y1), 3) for x in range(30, 610, 3)
                   if img.pixelColor(x, y).lightness() > 60)
    full, empty = render(v), render(None)
    assert lit(full, 0.13, 0.41) > 100 and lit(empty, 0.13, 0.41) > 100      # Limitimer both times
    assert lit(full, 0.58, 0.86) > 100 and lit(empty, 0.58, 0.86) == 0       # TruCue countdown only with a clip

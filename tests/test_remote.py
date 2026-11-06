import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    """HTTP from a worker thread while the Qt event loop runs (the server answers on the GUI thread)."""

    def __init__(self, app, port, pin=""):
        self.app, self.base, self.pin = app, f"http://127.0.0.1:{port}", pin

    def request(self, path, body=None, ctype="application/json", raw=False):
        box = {}

        def work():
            data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
            req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
            if data is not None:
                req.add_header("Content-Type", ctype)
            if self.pin:
                req.add_header("X-PIN", self.pin)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    box["code"], box["ctype"], box["body"] = r.status, r.headers["Content-Type"], r.read()
            except urllib.error.HTTPError as e:
                box["code"], box["ctype"], box["body"] = e.code, e.headers["Content-Type"], e.read()
        t = threading.Thread(target=work)
        t.start()
        end = time.monotonic() + 10
        while t.is_alive() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)
        t.join()
        if raw:
            return box["code"], box["ctype"], box["body"]
        return box["code"], json.loads(box["body"])

    def act(self, **kw):
        return self.request("/api/action", kw)


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    from dsan.clock import files
    real = QSettings
    QSettings("TRUCUE", "ConfTestRemote").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "ConfTestRemote"))
    monkeypatch.setattr(files, "_settings", lambda: real("TRUCUE", "ConfTestRemote"))
    monkeypatch.setattr(files, "last_dir", lambda kind: str(tmp_path))
    w = appmod.ControlWindow()
    yield w
    w.close()
    QSettings("TRUCUE", "ConfTestRemote").clear()


def test_page_state_and_actions(qapp, window, tmp_path):
    w = window
    port = free_port()
    w.remote_port.setValue(port)
    w.remote_enable.setChecked(True)
    assert w.remote is not None and f":{port}" in w.remote_status.text() and "no PIN" in w.remote_status.text()
    c = Client(qapp, port)

    code, ctype, body = c.request("/", raw=True)
    assert code == 200 and "text/html" in ctype and b"Confidence Remote" in body

    code, st = c.request("/api/state")
    assert code == 200 and st["app"]["name"] == "Confidence" and "TruCue countdown" in st["layout"]["presets"]
    assert st["inputs"]["lt"]["name"] == "Limitimer" and st["output"]["modes"]
    assert set(st["decklink"]) >= {"available", "reason", "on_air", "status"}      # no card here
    if not st["decklink"]["available"]:
        assert c.act(action="decklink", on=True)[0] == 400

    # message
    code, st = c.act(action="message", text="  5 MINUTES  ")
    assert code == 200 and st["message"] == "5 MINUTES" and w.message == "5 MINUTES"
    assert c.act(action="clear_message")[1]["message"] == "" and w.message == ""
    assert c.act(action="message", text="   ")[0] == 400

    # presets and saved layouts (only from the layouts folder)
    code, st = c.act(action="preset", name="Millumin countdown")
    assert code == 200 and w.layout_obj.name == "Millumin countdown"
    assert c.act(action="preset", name="Nope")[0] == 400
    w.layout_obj.save(str(tmp_path / "Show A.json"))
    assert "Show A.json" in c.request("/api/state")[1]["layout"]["saved"]
    code, st = c.act(action="open_layout", file="Show A.json")
    assert code == 200 and st["layout"]["file"] == "Show A.json"
    (tmp_path.parent / "outside.json").write_text("{}")
    assert c.act(action="open_layout", file="../outside.json")[0] == 400

    # demo on/off drives the same path as the button
    code, st = c.act(action="demo", on=True)
    assert code == 200 and st["demo"] and w.demo
    assert c.act(action="demo", on=False)[1]["demo"] is False

    # inputs: can't connect with no adapter picked; unknown port refused
    code, err = c.act(action="connect", kind="lt", on=True)
    assert code == 400 and "adapter" in err["error"]
    assert c.act(action="port", kind="lt", value="/dev/none")[0] == 400
    assert c.act(action="connect", kind="xx", on=True)[0] == 400

    # output
    modes = [m["value"] for m in st["output"]["modes"]]
    code, st = c.act(action="output", mode=modes[-1])
    assert code == 200 and st["output"]["mode"] == modes[-1]

    # feeds: change port, enable, thresholds
    mport = free_port()
    code, st = c.act(action="millumin", enabled=True, port=mport, yellow=45, red=15, layer="Main")
    assert code == 200 and st["millumin"]["enabled"] and st["millumin"]["port"] == mport
    assert w.mm_rx is not None and w.mm_rx.port == mport and w.mm_yellow.value() == 45 and w.mm_layer_name() == "Main"
    assert c.act(action="millumin", port=80)[0] == 400                       # out of range
    tport = free_port()
    code, st = c.act(action="trucue", enabled=True, port=tport)
    assert code == 200 and w.tc_rx is not None and w.tc_rx.port == tport
    code, st = c.act(action="osc", receive=True, port=free_port())
    assert code == 200 and w.osc_in is not None

    # bad requests
    assert c.act(action="explode")[0] == 400
    assert c.request("/api/action", b"not json")[0] == 400
    assert c.request("/api/action", b"{}", ctype="text/plain")[0] == 415
    assert c.request("/nothing")[0] == 404

    # preview is a real PNG at the requested width
    code, ctype, png = c.request("/api/preview.png?w=480", raw=True)
    assert code == 200 and ctype == "image/png" and png[:8] == b"\x89PNG\r\n\x1a\n"
    from PyQt6.QtGui import QImage
    img = QImage.fromData(png)
    assert img.width() == 480 and img.height() > 100
    assert w.remote.requests > 10


def test_pin(qapp, window):
    w = window
    port = free_port()
    w.remote_port.setValue(port)
    w.remote_pin.setText("4242")
    w.remote_enable.setChecked(True)
    assert "PIN required" in w.remote_status.text()
    assert Client(qapp, port).request("/", raw=True)[0] == 200            # the page itself loads (asks for the PIN)
    code, body = Client(qapp, port).request("/api/state")
    assert code == 401 and body["pin"] is True
    assert Client(qapp, port, pin="0000").act(action="message", text="x")[0] == 401
    assert w.message == ""
    assert Client(qapp, port, pin="4242").request("/api/state")[0] == 200
    w.remote_pin.setText("9999")
    w._remote_pin_changed()                                                 # changed while running
    assert Client(qapp, port, pin="4242").request("/api/state")[0] == 401
    assert Client(qapp, port, pin="9999").request("/api/state")[0] == 200


def test_port_in_use_and_settings(qapp, window, monkeypatch):
    from PyQt6.QtCore import QSettings
    w = window
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen()
    w.remote_port.setValue(busy.getsockname()[1])
    w.remote_enable.setChecked(True)
    assert w.remote is None and "Can't use port" in w.remote_status.text() and not w.remote_enable.isChecked()
    busy.close()
    port = free_port()
    w.remote_port.setValue(port)
    w.remote_pin.setText("12")
    w.remote_enable.setChecked(True)
    w._save_settings()
    s = QSettings("TRUCUE", "ConfTestRemote")
    assert s.value("remote/enabled", type=bool) and s.value("remote/port", type=int) == port
    assert s.value("remote/pin") == "12"
    w.remote_enable.setChecked(False)
    assert w.remote is None
    with pytest.raises(OSError):                                            # really closed
        socket.create_connection(("127.0.0.1", port), timeout=0.5)

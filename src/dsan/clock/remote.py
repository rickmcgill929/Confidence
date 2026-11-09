"""Remote control: a small web page served by Confidence, for configuring it from a
phone, tablet or another computer on the same network.

Off by default; turn it on in the REMOTE box (port, optional PIN). Standard library
HTTP server on its own thread; every request is carried out on the Qt GUI thread
through :class:`Invoker`, using the same code paths as the app's own controls.

    GET  /                 the web page
    GET  /api/state        everything the page shows (JSON)
    GET  /api/preview.png  the current output, rendered (?w=640)
    POST /api/action       {"action": "...", ...} — see RemoteAPI.ACTIONS
                           ("decklink" drives the SDI output: device, mode, cut/fill, layout, on air)

With a PIN set, every /api request needs it in an ``X-PIN`` header (the page asks
once and remembers it in that browser).
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import QBuffer, QIODevice, QObject, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter

from .. import __version__


class RemoteError(Exception):
    """A request the app can't carry out (bad value, unknown action…) → HTTP 400."""


# ── run on the GUI thread ─────────────────────────────────────────────────────────

class Invoker(QObject):
    """Runs a callable on the thread this object lives on (the GUI thread) and waits for it."""
    _call = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, job):
        fn, done, box = job
        try:
            box["value"] = fn()
        except BaseException as e:          # handed back to the HTTP thread
            box["error"] = e
        finally:
            done.set()

    def call(self, fn: Callable[[], Any], timeout: float = 5.0) -> Any:
        done, box = threading.Event(), {}
        self._call.emit((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError("the app didn't answer in time")
        if "error" in box:
            raise box["error"]
        return box.get("value")


# ── what the page can see and do ──────────────────────────────────────────────────

def _combo_items(combo) -> List[Dict[str, str]]:
    return [{"value": str(combo.itemData(i) if combo.itemData(i) is not None else ""), "label": combo.itemText(i)}
            for i in range(combo.count())]


def _status(label) -> Dict[str, str]:
    style = label.styleSheet() or ""
    color = style.split("color:")[1].split(";")[0].strip() if "color:" in style else ""
    return {"text": label.text(), "color": color}


class RemoteAPI:
    """The app's state as JSON, and the actions the page can take. Call only on the GUI thread."""

    QUICK_MESSAGES = ("WRAP UP", "1 MINUTE", "PLEASE STOP")

    def __init__(self, window):
        self.w = window

    # -- state -----------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        w = self.w
        from .layout import PRESETS
        inputs = {}
        for kind, name in (("lt", "Limitimer"), ("pc", "Perfect Cue")):
            combo = w._combo(kind)
            inputs[kind] = {"name": name, "ports": [p for p in _combo_items(combo)
                                                     if p["value"] not in ("__missing__", w._URL_ITEM)],
                            "port": str(combo.currentData() or ""),
                            "connected": bool(w._connect_btn(kind).isChecked()),
                            "button": w._connect_btn(kind).text(),
                            "status": _status(w.lt_status if kind == "lt" else w.pc_status)}
        return {
            "app": {"name": "Confidence", "version": __version__},
            "demo": bool(w.demo),
            "inputs": inputs,
            "message": w.message,
            "quick": list(self.QUICK_MESSAGES),
            "layout": {"name": w.layout_obj.name,
                       "file": os.path.basename(w.layout_path) if w.layout_path else "",
                       "has_message": w.layout_obj.first("message") is not None,
                       "presets": list(PRESETS), "saved": self.saved_layouts()},
            "output": {"shown": bool(w.show_output_btn.isChecked()),
                       "mode": w.mode_combo.currentData(), "modes": _combo_items(w.mode_combo),
                       "screen": str(w.screen_combo.currentData() or ""), "screens": _combo_items(w.screen_combo)},
            "osc": {"send": w.osc_enable.isChecked(), "target": w.osc_target.text(),
                    "prefix": w.osc_prefix.text(), "receive": w.osc_in_enable.isChecked(),
                    "port": w.osc_in_port.value(), "status": _status(w.osc_in_status)},
            "millumin": {"enabled": w.mm_enable.isChecked(), "port": w.mm_port.value(),
                         "layer": w.mm_layer_name(),
                         "layers": [w.mm_layer.itemText(i) for i in range(1, w.mm_layer.count())],
                         "yellow": w.mm_yellow.value(), "red": w.mm_red.value(), "status": _status(w.mm_status)},
            "trucue": {"enabled": w.tc_enable.isChecked(), "port": w.tc_port.value(),
                       "forward": w.tc_forward.text(), "yellow": w.tc_yellow.value(), "red": w.tc_red.value(),
                       "status": _status(w.tc_status)},
            "decklink": self.decklink_state(),
            "limitimer": w.lt_panel.remote_state(),
            "system": w.system.state(),
        }

    # -- DeckLink SDI output ---------------------------------------------------------

    def _dl_layouts(self) -> List[Dict[str, str]]:
        """What the SDI output can show: the screen's layout, a preset, or a saved layout file."""
        from .layout import PRESETS
        items = [{"value": "same", "label": "Same as screen output"}]
        items += [{"value": f"preset:{n}", "label": f"Preset: {n}"} for n in PRESETS]
        items += [{"value": f"file:{f}", "label": f"File: {f[:-5]}"} for f in self.saved_layouts()]
        current = self._dl_layout_value()
        if not any(it["value"] == current for it in items):        # a file from another folder
            items.append({"value": current, "label": f"File: {current[5:]}"})
        return items

    def _dl_layout_value(self) -> str:
        choice = self.w.decklink_panel.layout_choice
        return f"file:{os.path.basename(choice[5:])}" if choice.startswith("file:") else choice

    def decklink_state(self) -> Dict[str, Any]:
        p = self.w.decklink_panel
        keying = []
        model = p.keying_combo.model()
        for i in range(p.keying_combo.count()):
            keying.append({"value": str(p.keying_combo.itemData(i)), "label": p.keying_combo.itemText(i),
                           "enabled": bool(model.item(i).isEnabled())})
        return {
            "available": bool(p.devices),
            "reason": p.reason.text(),
            "on_air": p.output is not None,
            "devices": _combo_items(p.device_combo), "device": str(p.device_combo.currentData() or ""),
            "modes": _combo_items(p.mode_combo),
            "mode": "" if p.mode_combo.currentData() is None else str(p.mode_combo.currentData()),
            "keyings": keying, "keying": str(p.keying_combo.currentData() or "none"),
            "key_devices": _combo_items(p.key_combo), "key_device": str(p.key_combo.currentData() or ""),
            "layouts": self._dl_layouts(), "layout": self._dl_layout_value(),
            "premultiplied": p.premult.isChecked(),
            "can_start": p.start_btn.isEnabled(),
            "status": _status(p.status),
            "warning": p.warning.text() if not p.warning.isHidden() else "",
        }

    def saved_layouts(self) -> List[str]:
        from . import files
        folder = files.last_dir("layouts")
        try:
            return sorted(f for f in os.listdir(folder) if f.lower().endswith(".json"))[:200]
        except OSError:
            return []

    # -- actions ---------------------------------------------------------------

    ACTIONS = ("connect", "port", "demo", "message", "clear_message", "preset", "open_layout",
               "output", "osc", "millumin", "trucue", "decklink", "limitimer", "system")

    def act(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = data.get("action")
        if name not in self.ACTIONS:
            raise RemoteError(f"unknown action {name!r}")
        getattr(self, "_" + name)(data)
        return self.state()

    @staticmethod
    def _kind(data) -> str:
        kind = data.get("kind")
        if kind not in ("lt", "pc"):
            raise RemoteError("kind must be 'lt' (Limitimer) or 'pc' (Perfect Cue)")
        return kind

    def _connect(self, data):
        kind, on = self._kind(data), bool(data.get("on"))
        btn = self.w._connect_btn(kind)
        btn.setChecked(on)
        self.w._connect_clicked(kind, on)
        if on and not btn.isChecked():
            raise RemoteError("Pick an adapter for it first")

    def _port(self, data):
        kind = self._kind(data)
        combo = self.w._combo(kind)
        value = str(data.get("value", ""))
        i = next((i for i in range(combo.count()) if str(combo.itemData(i) or "") == value), -1)
        if i < 0 or value in ("__missing__", self.w._URL_ITEM):
            raise RemoteError(f"no such port: {value}")
        combo.setCurrentIndex(i)
        self.w._port_picked(kind, i)

    def _demo(self, data):
        self.w._toggle_demo(bool(data.get("on")))

    def _message(self, data):
        text = str(data.get("text", "")).strip()[:200]
        if not text:
            raise RemoteError("empty message")
        self.w._send_message(text)

    def _clear_message(self, data):
        self.w._clear_message()

    def _preset(self, data):
        from .layout import PRESETS
        name = data.get("name")
        if name not in PRESETS:
            raise RemoteError(f"no preset called {name!r}")
        self.w._set_layout(PRESETS[name]())

    def _open_layout(self, data):
        from . import files
        from .layout import Layout
        name = os.path.basename(str(data.get("file", "")))         # only files in the layouts folder
        path = os.path.join(files.last_dir("layouts"), name)
        if not name.lower().endswith(".json") or not os.path.isfile(path):
            raise RemoteError(f"no saved layout called {name!r}")
        try:
            self.w._set_layout(Layout.load(path), path)
        except Exception as e:
            raise RemoteError(f"couldn't open {name}: {e}") from None

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        if i < 0:
            raise RemoteError(f"no such option: {value}")
        combo.setCurrentIndex(i)

    def _output(self, data):
        w = self.w
        if "mode" in data:
            self._select(w.mode_combo, data["mode"])
        if "screen" in data:
            self._select(w.screen_combo, data["screen"])
        if "shown" in data:
            w.show_output_btn.setChecked(bool(data["shown"]))

    @staticmethod
    def _int(data, key, lo, hi):
        try:
            v = int(data[key])
        except (TypeError, ValueError):
            raise RemoteError(f"{key} must be a number") from None
        if not lo <= v <= hi:
            raise RemoteError(f"{key} must be {lo}–{hi}")
        return v

    def _restart(self, enable_box, apply: Callable[[], None], data, key="enabled"):
        """Change a listener's settings: off, apply, back on (or to the requested state)."""
        want = bool(data[key]) if key in data else enable_box.isChecked()
        enable_box.setChecked(False)
        apply()
        if want:
            enable_box.setChecked(True)

    def _osc(self, data):
        w = self.w

        def apply_out():
            if "target" in data:
                w.osc_target.setText(str(data["target"]).strip())
            if "prefix" in data:
                w.osc_prefix.setText(str(data["prefix"]).strip() or "/dsan")
        if "target" in data or "prefix" in data or "send" in data:
            self._restart(w.osc_enable, apply_out, data, key="send")

        def apply_in():
            if "port" in data:
                w.osc_in_port.setValue(self._int(data, "port", 1024, 65535))
        if "port" in data or "receive" in data:
            self._restart(w.osc_in_enable, apply_in, data, key="receive")

    def _millumin(self, data):
        w = self.w
        if "yellow" in data:
            w.mm_yellow.setValue(self._int(data, "yellow", 0, 3600))
        if "red" in data:
            w.mm_red.setValue(self._int(data, "red", 0, 3600))
        if "layer" in data:
            w.mm_layer.setCurrentText(str(data["layer"]).strip() or "Selected layer")
        if "port" in data or "enabled" in data:
            self._restart(w.mm_enable, lambda: "port" in data and w.mm_port.setValue(
                self._int(data, "port", 1024, 65535)), data)

    def _trucue(self, data):
        w = self.w
        if "yellow" in data:
            w.tc_yellow.setValue(self._int(data, "yellow", 0, 3600))
        if "red" in data:
            w.tc_red.setValue(self._int(data, "red", 0, 3600))
        if "port" in data or "forward" in data or "enabled" in data:
            def apply():
                if "port" in data:
                    w.tc_port.setValue(self._int(data, "port", 1024, 65535))
                if "forward" in data:
                    w.tc_forward.setText(str(data["forward"]).strip())
            self._restart(w.tc_enable, apply, data)

    def _system(self, data):
        """{do: check | update | restart | reboot | poweroff}"""
        sysctl = self.w.system
        what = str(data.get("do", ""))
        try:
            if what == "check":
                sysctl.check()
            elif what == "update":
                sysctl.update()
            elif what == "restart":
                if not sysctl.state()["headless"]:
                    raise ValueError("Restart is for headless installs — quit and open Confidence instead")
                sysctl.restart()
            elif what in ("reboot", "poweroff"):
                sysctl.power(what)
            else:
                raise RemoteError(f"unknown system action: {what!r}")
        except ValueError as e:
            raise RemoteError(str(e)) from None

    def _limitimer(self, data):
        """{mode: read|emulate} · {key: start|p2|time+|…} · {program: 1-4, total: "5:00", sumup: "1:00"}
        · {tp_port: device|socket://…|""} · {tp_connect: bool} · {switches: {count_down: …, beep_type: …}}"""
        from .ltcontrol import MODES, parse_time
        panel = self.w.lt_panel
        if "mode" in data:
            mode = str(data["mode"])
            if mode not in MODES:
                raise RemoteError(f"no such Limitimer mode: {mode}")
            panel.set_mode(mode)
        if "program" in data:
            if panel.mode != "emulate":
                raise RemoteError("Program times can only be set on the software Limitimer")
            try:
                n = int(data["program"])
                total = parse_time(str(data["total"])) if data.get("total") not in (None, "") else None
                sumup = parse_time(str(data["sumup"])) if data.get("sumup") not in (None, "") else None
                panel.emulator.set_program(n, total, sumup)
            except (TypeError, ValueError) as e:
                raise RemoteError(f"Bad program time: {e}") from None
            panel.refresh()
        try:
            if "tp_port" in data:
                if panel.mode != "read":
                    raise ValueError("The TP-2000X is for the real Limitimer")
                panel.set_tp_port(str(data["tp_port"]))
            if "tp_connect" in data:
                if panel.mode != "read":
                    raise ValueError("The TP-2000X is for the real Limitimer")
                panel.set_connected(bool(data["tp_connect"]))
            if "switches" in data:
                if panel.mode != "emulate" or not isinstance(data["switches"], dict):
                    raise ValueError("Switches are for the software Limitimer")
                panel.set_switches(data["switches"])
            if "key" in data:
                panel.press(str(data["key"]))
        except ValueError as e:
            raise RemoteError(str(e)) from None

    @staticmethod
    def _select_str(combo, value, what):
        value = str(value)
        i = next((i for i in range(combo.count()) if str(combo.itemData(i)) == value), -1)
        if i < 0:
            raise RemoteError(f"no such {what}: {value}")
        combo.setCurrentIndex(i)

    def _decklink(self, data):
        """{device, mode, keying, key_device, layout, premultiplied, on, rescan} — all optional."""
        from . import files
        p = self.w.decklink_panel
        if data.get("rescan"):
            if p.output is not None:
                raise RemoteError("Stop SDI output before re-scanning")
            p.rescan()
        if not p.devices and any(k in data for k in ("device", "mode", "keying", "key_device", "on")):
            raise RemoteError(p.reason.text() or "No DeckLink outputs found")
        routing = [k for k in ("device", "mode", "keying", "key_device") if k in data]
        if routing and p.output is not None:
            changed = any(str(data[k]) != self.decklink_state()[k] for k in routing)
            if changed:
                raise RemoteError("Stop SDI output to change the device, mode or cut/fill")
        else:
            if "device" in data:
                self._select_str(p.device_combo, data["device"], "DeckLink device")   # refreshes modes
            if "mode" in data:
                self._select_str(p.mode_combo, data["mode"], "video mode")
            if "keying" in data:
                value = str(data["keying"])
                i = p.keying_combo.findData(value)
                if i < 0:
                    raise RemoteError(f"no such cut/fill option: {value}")
                if not p.keying_combo.model().item(i).isEnabled():
                    raise RemoteError(f"{p.keying_combo.itemText(i)} isn't available with this device")
                p.keying_combo.setCurrentIndex(i)
            if "key_device" in data:
                if str(data["key_device"]) == str(p.device_combo.currentData()):
                    raise RemoteError("The key output must be a different device from the fill")
                self._select_str(p.key_combo, data["key_device"], "key device")
        if "layout" in data:
            value = str(data["layout"])
            if value != self._dl_layout_value():
                if value == "same" or value.startswith("preset:"):
                    from .layout import PRESETS
                    if value != "same" and value[7:] not in PRESETS:
                        raise RemoteError(f"no preset called {value[7:]!r}")
                    p.set_layout_choice(value)
                elif value.startswith("file:"):
                    name = os.path.basename(value[5:])                      # only the layouts folder
                    path = os.path.join(files.last_dir("layouts"), name)
                    if not name.lower().endswith(".json") or not os.path.isfile(path):
                        raise RemoteError(f"no saved layout called {name!r}")
                    p.set_layout_choice(f"file:{path}")
                else:
                    raise RemoteError(f"no such layout: {value}")
        if "premultiplied" in data:
            p.premult.setChecked(bool(data["premultiplied"]))
        if "on" in data:
            if data["on"] and p.output is None:
                if not p.start_btn.isEnabled():
                    raise RemoteError(p.warning.text() or "Pick a device and video mode first")
                if not p.start():
                    raise RemoteError(p.status.text().lstrip("● ") or "Couldn't start SDI output")
            elif not data["on"] and p.output is not None:
                p.stop()

    # -- preview ---------------------------------------------------------------

    def preview_png(self, width: int = 640) -> bytes:
        from .render import Renderer
        w = self.w
        width = max(160, min(1920, int(width)))
        height = max(90, round(width / max(0.2, w._aspect())))
        img = QImage(width, height, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.black)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        Renderer().paint(p, QRectF(0, 0, width, height), w.layout_obj, w.frame())
        p.end()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())


# ── HTTP ──────────────────────────────────────────────────────────────────────────

def _page() -> bytes:
    from .remote_page import PAGE
    return PAGE.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = f"Confidence/{__version__}"
    server: "_Server"

    def log_message(self, fmt, *args):         # quiet; the app shows activity itself
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _authorised(self) -> bool:
        pin = self.server.pin
        if not pin:
            return True
        given = self.headers.get("X-PIN", "")
        if hmac.compare_digest(given.encode("utf-8"), pin.encode("utf-8")):
            return True
        time.sleep(0.4)                       # slow down guessing
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "PIN required", "pin": True})
        return False

    def _call(self, fn):
        try:
            return self.server.invoker.call(fn)
        finally:
            self.server.requests += 1
            if self.server.on_request:
                self.server.on_request(self.client_address[0])

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(HTTPStatus.OK, _page(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; "
                                                          "style-src 'self' 'unsafe-inline'; "
                                                          "script-src 'self' 'unsafe-inline'"})
        if url.path == "/api/state":
            if self._authorised():
                self._safely(lambda: self._json(HTTPStatus.OK, self._call(self.server.api.state)))
            return
        if url.path == "/api/preview.png":
            if self._authorised():
                try:
                    width = int(parse_qs(url.query).get("w", ["640"])[0])
                except ValueError:
                    width = 640
                self._safely(lambda: self._send(HTTPStatus.OK, self._call(lambda: self.server.api.preview_png(width)),
                                                "image/png"))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/action":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if not self._authorised():
            return
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send JSON"})
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 64 * 1024)
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
        self._safely(lambda: self._json(HTTPStatus.OK, self._call(lambda: self.server.api.act(data))))

    def _safely(self, fn):
        try:
            fn()
        except RemoteError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        except TimeoutError as e:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(e)})
        except Exception as e:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(e).__name__}: {e}"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RemoteServer:
    """Serve the remote page for `window` on `port` (all network interfaces)."""

    def __init__(self, window, port: int, pin: str = "", host: str = "0.0.0.0",
                 on_request: Optional[Callable[[str], None]] = None):
        self.invoker = Invoker()                  # created on the GUI thread
        self._srv = _Server((host, port), _Handler)
        self._srv.api = RemoteAPI(window)
        self._srv.invoker = self.invoker
        self._srv.pin = pin
        self._srv.requests = 0
        self._srv.on_request = on_request
        self.host, self.port = self._srv.server_address[:2]
        self._thread = threading.Thread(target=self._srv.serve_forever, name="confidence-remote", daemon=True)
        self._thread.start()

    @property
    def requests(self) -> int:
        return self._srv.requests

    @property
    def pin(self) -> str:
        return self._srv.pin

    @pin.setter
    def pin(self, value: str) -> None:
        self._srv.pin = value

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def urls(self) -> List[str]:
        return [f"http://{a}:{self.port}" for a in local_addresses()]


def local_addresses(names: bool = True) -> List[str]:
    """This computer's LAN addresses (best first) and, with `names`, its .local name."""
    found: List[str] = []
    try:                                           # the address used to reach the network (no packet sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            a = info[4][0]
            if a not in found and not a.startswith("127."):
                found.append(a)
    except OSError:
        pass
    host = socket.gethostname() if names else ""
    if host and not host.endswith(".local") and "." not in host:
        host += ".local"
    if host and host.endswith(".local"):           # not "Mac.localdomain" — that only works on the Mac itself
        found.append(host)
    return found or ["127.0.0.1"]


__all__ = ["Invoker", "RemoteAPI", "RemoteError", "RemoteServer", "local_addresses"]

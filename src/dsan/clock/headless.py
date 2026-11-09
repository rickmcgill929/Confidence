"""Headless Confidence — for a Raspberry Pi (or any box) with no desktop.

    confidence --headless [--remote-port 8080] [--remote-pin 1234] [--name confidence]

* No control window. Everything is set up from the browser remote, which is
  always on (``http://confidence.local:8080``).
* The clock goes full-screen on the HDMI output, drawn straight to the display
  through the kernel's DRM/KMS (Qt ``linuxfb`` with ``QT_QPA_FB_DRM=1``) — no
  desktop needed, and nothing beyond ``libdrm``. With nothing plugged into HDMI
  it runs without a display (``offscreen``) and still drives OSC, the remote and
  the RS-485 adapters. ``CONFIDENCE_HDMI`` overrides the choice: ``drm`` (default),
  ``fb0`` (the older /dev/fb0 framebuffer), ``eglfs`` (needs a Qt with KMS support,
  e.g. the system's — the PyPI Qt lacks it) or ``off``. ``QT_QPA_PLATFORM`` (if set)
  always wins.
* SIGTERM / SIGINT (``systemctl stop``, Ctrl-C) save the settings and quit.
* The addresses are printed at start, for ``journalctl -u confidence``.
"""
from __future__ import annotations

import argparse
import glob
import os
import signal
import sys
from typing import Dict, List, Optional

HDMI_CHOICES = ("drm", "fb0", "eglfs", "off")


def displays_connected(drm_root: str = "/sys/class/drm") -> List[str]:
    """Connected display outputs (HDMI-A-1, DSI-1…) that the kernel reports."""
    found = []
    for status in sorted(glob.glob(os.path.join(drm_root, "card*-*", "status"))):
        try:
            with open(status) as f:
                if f.read().strip() == "connected":
                    found.append(os.path.basename(os.path.dirname(status)).split("-", 1)[1])
        except OSError:
            pass
    return found


def choose_platform(env: Dict[str, str], platform: str = sys.platform,
                    drm_root: str = "/sys/class/drm") -> Optional[str]:
    """The Qt platform to use headless, or None to leave Qt's own choice."""
    if env.get("QT_QPA_PLATFORM"):
        return None                                     # the user (or a test) decided
    if not platform.startswith("linux"):
        return None                                     # Mac/Windows: a normal desktop
    want = (env.get("CONFIDENCE_HDMI") or "").strip().lower()
    if want == "off":
        return "offscreen"
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return None                                     # a desktop is running: use it
    if not displays_connected(drm_root):
        return "offscreen"                              # nothing on HDMI
    return "eglfs" if want == "eglfs" else "linuxfb"


def prepare_environment(env=None) -> Optional[str]:
    """Set Qt's platform and display variables before QApplication exists."""
    env = os.environ if env is None else env
    chosen = choose_platform(env)
    if chosen:
        env["QT_QPA_PLATFORM"] = chosen
    if chosen == "linuxfb" and (env.get("CONFIDENCE_HDMI") or "").strip().lower() != "fb0":
        env.setdefault("QT_QPA_FB_DRM", "1")              # straight to KMS, the connected output's mode
    env.setdefault("QT_QPA_EGLFS_HIDECURSOR", "1")
    env.setdefault("QT_QPA_EGLFS_DISABLE_INPUT", "1")    # no keyboard/mouse grabbing on the console
    env.setdefault("QT_QPA_FB_DISABLE_INPUT", "1")
    env.setdefault("QT_QPA_EGLFS_ALWAYS_SET_MODE", "1")
    env.setdefault("CONFIDENCE_NO_UPDATE_CHECK", "1")    # no pop-up update dialogs; the remote shows updates
    env["CONFIDENCE_HEADLESS"] = "1"
    return env.get("QT_QPA_PLATFORM")


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="confidence --headless", add_help=True)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--remote-port", type=int, default=None, help="browser remote port (default: as saved, 8080)")
    p.add_argument("--remote-pin", default=None, help="set the browser remote's PIN")
    p.add_argument("--name", default=None, help="announce as NAME.local (default: confidence)")
    args, _qt = p.parse_known_args(argv)
    return args


def setup(window, args: argparse.Namespace, platform: str) -> None:
    """Turn a ControlWindow into the headless app (not shown)."""
    if args.remote_port:
        window.remote_port.setValue(args.remote_port)
    if args.remote_pin is not None:
        window.remote_pin.setText(args.remote_pin)
        window._remote_pin_changed()
    if args.name:
        window.remote_name.setText(args.name)
        window._remote_name_changed()
    if not window.remote_enable.isChecked():
        window.remote_enable.setChecked(True)            # the only way in: always on
    elif window.remote is None:
        window._rebuild_remote()
    window.headless = True
    hdmi = platform in ("eglfs", "linuxfb")

    def output():                                        # after the saved "output visible" is applied
        if hdmi:                                         # the clock on HDMI, full-screen
            window.mode_combo.setCurrentIndex(window.mode_combo.findData("fullscreen"))
        window.show_output_btn.setChecked(hdmi)          # offscreen: nothing to show it on
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(50, output)


def addresses(window) -> List[str]:
    from .remote import local_addresses
    r = window.remote
    if r is None:
        return []
    port = "" if r.port == 80 else f":{r.port}"
    urls = []
    if window.bonjour is not None and window.bonjour.state == "ok":
        urls.append(f"http://{window.bonjour.hostname}{port}")
    urls += [f"http://{a}{port}" for a in local_addresses(names=False) if a != "127.0.0.1"]
    return urls


def run(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = parse_args(argv[1:])
    platform = prepare_environment()
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from .. import __version__
    app = QApplication(argv[:1])
    app.setApplicationName("Confidence")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("TRUCUE")
    app.setQuitOnLastWindowClosed(False)
    from .app import ControlWindow
    w = ControlWindow()
    setup(w, args, platform or app.platformName())
    print(f"Confidence {__version__} headless · display: {app.platformName()}"
          f"{' (HDMI: ' + ', '.join(displays_connected()) + ')' if app.platformName() in ('eglfs', 'linuxfb') else ''}",
          flush=True)

    def announce():
        if w.remote is None:
            print("Browser remote: couldn't start — " + w.remote_status.text(), flush=True)
            return
        print("Browser remote: " + "  ".join(addresses(w)) +
              ("" if w.remote.pin else "   (no PIN — anyone on this network can use it)"), flush=True)
        QTimer.singleShot(500, announce_name)

    def announce_name(tries=[0]):                      # Bonjour takes a few seconds to claim the name
        tries[0] += 1
        b = w.bonjour
        if b is not None and b.state == "starting" and tries[0] < 30:
            QTimer.singleShot(500, announce_name)
        elif b is not None and b.state == "ok" and w.remote is not None:
            port = "" if w.remote.port == 80 else f":{w.remote.port}"
            print(f"Browser remote: http://{b.hostname}{port}", flush=True)
    QTimer.singleShot(100, announce)

    def stop(*_):
        app.quit()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tick = QTimer()                                      # lets Python run its signal handlers
    tick.timeout.connect(lambda: None)
    tick.start(250)
    code = app.exec()
    w.close()                                            # saves settings, stops devices
    print("Confidence stopped", flush=True)
    return code


__all__ = ["HDMI_CHOICES", "addresses", "choose_platform", "displays_connected", "prepare_environment", "run",
           "setup"]

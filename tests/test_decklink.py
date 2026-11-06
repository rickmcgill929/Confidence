"""DeckLink output tested end to end against a fake DeckLink driver.

Builds src/dsan/clock/native/dsan_decklink.cpp and tests/native/fake_decklink.cpp
with the DeckLink SDK's Linux headers (set DECKLINK_SDK to the SDK folder), then
preloads the fake as libDeckLinkAPI.so so the SDK's dispatch code finds it.
Skipped when there's no compiler or SDK.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SDK_CANDIDATES = [os.environ.get("DECKLINK_SDK", ""),
                  "/mnt/user-data/uploads/Blackmagic DeckLink SDK 16.0",
                  str(Path.home() / "Desktop" / "Blackmagic DeckLink SDK 16.0")]


def _sdk_include():
    for c in SDK_CANDIDATES:
        inc = Path(c) / "Linux" / "include"
        if c and (inc / "DeckLinkAPI.h").exists():
            return inc
    return None


@pytest.fixture(scope="module")
def dl(tmp_path_factory):
    if not sys.platform.startswith("linux"):
        pytest.skip("fake DeckLink driver is Linux-only")
    inc = _sdk_include()
    cxx = shutil.which("g++") or shutil.which("clang++")
    if inc is None or cxx is None:
        pytest.skip("needs the DeckLink SDK headers (DECKLINK_SDK) and a C++ compiler")
    out = tmp_path_factory.mktemp("decklink")
    fake = out / "libDeckLinkAPI.so"
    bridge = out / "libdsan_decklink.so"
    flags = ["-std=c++17", "-O1", "-fPIC", "-shared", "-fvisibility=hidden", f"-I{inc}"]
    subprocess.run([cxx, *flags, "-Wl,-soname,libDeckLinkAPI.so", str(ROOT / "tests/native/fake_decklink.cpp"),
                    "-o", str(fake), "-lpthread"], check=True)
    subprocess.run([cxx, *flags, str(inc / "DeckLinkAPIDispatch.cpp"),
                    str(ROOT / "src/dsan/clock/native/dsan_decklink.cpp"), "-o", str(bridge), "-ldl", "-lpthread"],
                   check=True)
    fake_lib = ctypes.CDLL(str(fake), mode=ctypes.RTLD_GLOBAL)   # dispatch's dlopen finds it by soname
    for name, res, args in (("fake_pixel", ctypes.c_uint32, [ctypes.c_int] * 4),
                            ("fake_displayed", ctypes.c_uint64, [ctypes.c_int]),
                            ("fake_keyer", ctypes.c_int, [ctypes.c_int]),
                            ("fake_level", ctypes.c_int, [ctypes.c_int]),
                            ("fake_enabled_mode", ctypes.c_uint32, [ctypes.c_int]),
                            ("fake_playing", ctypes.c_int, [ctypes.c_int])):
        fn = getattr(fake_lib, name)
        fn.restype, fn.argtypes = res, args
    os.environ["DSAN_DECKLINK_LIB"] = str(bridge)
    import dsan.clock.decklink as mod
    mod._lib = None
    mod._lib_error = None
    yield mod, fake_lib
    os.environ.pop("DSAN_DECKLINK_LIB", None)


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _half_red(w, h):
    """Left half opaque red, right half fully transparent — straight BGRA."""
    row = bytes([0, 0, 255, 255]) * (w // 2) + bytes(4) * (w - w // 2)
    return bytearray(row * h)


def test_driver_and_devices(dl):
    mod, _ = dl
    ok, text = mod.available()
    assert ok and text == "Desktop Video 16.0.0"
    devs = mod.devices()
    assert [d.name for d in devs] == ["DeckLink Duo 2 (1)", "DeckLink Duo 2 (2)", "DeckLink Mini Recorder"]
    d0, d1, d2 = devs
    assert d0.has_output and d0.external_keying and d0.internal_keying
    assert d1.has_output and not d1.external_keying
    assert not d2.has_output and d2.modes == []
    m = {x.label: x for x in d0.modes}
    assert m["1080i59.94"].interlaced and m["1080i59.94"].keying and abs(m["1080i59.94"].fps - 29.97) < 0.01
    assert not any(x.keying for x in d1.modes)


def test_external_keyer_fill_on_a_key_on_b(dl):
    mod, fake = dl
    d0 = mod.devices()[0]
    mode = next(m for m in d0.modes if m.label == "720p59.94")
    out = mod.DeckLinkOutput(d0, mode, "external")
    assert fake.fake_keyer(0) == 2 and fake.fake_level(0) == 255     # external keyer, full level
    assert fake.fake_enabled_mode(0) == mode.id and fake.fake_playing(0)
    out.push_bytes(_half_red(mode.width, mode.height), mode.width * 4)
    assert _wait(lambda: fake.fake_pixel(0, 0, 10, 10) == 0xFFFF0000)            # fill A: red
    assert fake.fake_pixel(0, 0, mode.width - 10, 10) >> 24 == 0                 # transparent
    assert fake.fake_pixel(0, 1, 10, 10) == 0xFFFFFFFF                           # key B: white
    assert fake.fake_pixel(0, 1, mode.width - 10, 10) == 0xFF000000              # key B: black
    st = out.status()
    assert st["running"] and st["frames"] > 0 and st["reference"] == "locked"
    out.close()
    assert fake.fake_keyer(0) == 0 and fake.fake_enabled_mode(0) == 0 and not fake.fake_playing(0)


def test_separate_key_output(dl):
    mod, fake = dl
    d0, d1, _ = mod.devices()
    mode = next(m for m in d1.modes if m.label == "1080p29.97")
    out = mod.DeckLinkOutput(d1, mode, "separate", key=d0)      # fill on device 1, key on device 0
    assert fake.fake_keyer(0) == 0                               # no hardware keyer in this mode
    out.push_bytes(_half_red(mode.width, mode.height), mode.width * 4)
    assert _wait(lambda: fake.fake_pixel(1, 0, 10, 10) == 0xFFFF0000)            # fill output
    assert _wait(lambda: fake.fake_pixel(0, 0, 10, 10) == 0xFFFFFFFF)            # key output: white
    assert fake.fake_pixel(0, 0, mode.width - 10, 10) == 0xFF000000              # key output: black
    out.close()
    assert not fake.fake_playing(0) and not fake.fake_playing(1)


def test_errors(dl):
    mod, fake = dl
    d0, d1, d2 = mod.devices()
    mode = d1.modes[0]
    with pytest.raises(RuntimeError, match="can't key"):
        mod.DeckLinkOutput(d1, mode, "external")
    with pytest.raises(RuntimeError, match="different DeckLink output"):
        mod.DeckLinkOutput(d1, mode, "separate", key=d1)
    with pytest.raises(RuntimeError, match="no output"):
        mod.DeckLinkOutput(d2, mode, "none")
    a = mod.DeckLinkOutput(d1, mode, "none")
    with pytest.raises(RuntimeError, match="EnableVideoOutput failed"):     # output already in use
        mod.DeckLinkOutput(d1, mode, "none")
    with pytest.raises(ValueError):
        a.push_bytes(bytearray(16), 16)
    a.close()
    assert fake.fake_enabled_mode(1) == 0


def test_feeder_straight_and_premultiplied(dl, qapp_dl):
    from dsan.clock.layout import Element, Layout
    from dsan.clock.render import Frame, Renderer
    mod, fake = dl
    d0 = mod.devices()[0]
    mode = next(m for m in d0.modes if m.label == "720p59.94")
    lay = Layout(background="#00000000", elements=[Element("label", 0.0, 0.0, 0.5, 0.5, text="", bg="#FF000080")])
    for premult, red in ((False, 255), (True, 128)):
        out = mod.DeckLinkOutput(d0, mode, "external")
        feeder = mod.Feeder(out, Renderer(), lambda: lay, lambda: Frame(), premultiplied=premult)
        feeder.tick()

        def fill_red():
            px = fake.fake_pixel(0, 0, 50, 50)
            return px >> 24 in (127, 128) and abs((px >> 16 & 255) - red) <= 1
        assert _wait(fill_red), hex(fake.fake_pixel(0, 0, 50, 50))
        assert fake.fake_pixel(0, 1, 50, 50) & 255 in (127, 128)        # key = alpha ≈ 50%
        assert fake.fake_pixel(0, 1, mode.width - 5, mode.height - 5) == 0xFF000000
        out.close()


@pytest.fixture(scope="module")
def qapp_dl():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_control_window_decklink_panel(dl, qapp_dl, monkeypatch):
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    mod, fake = dl
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockDL").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockDL"))
    w = appmod.ControlWindow()
    p = w.decklink_panel
    assert p.body.isVisible() or p.devices                       # driver found, panel usable
    assert [p.device_combo.itemData(i) for i in range(p.device_combo.count())] == \
        ["DeckLink Duo 2 (1)", "DeckLink Duo 2 (2)"]          # input-only device left out
    assert p.mode_combo.currentText().startswith("1080i59.94")  # sensible default
    p.mode_combo.setCurrentIndex(p.mode_combo.findText("720p59.94"))
    p.keying_combo.setCurrentIndex(p.keying_combo.findData("external"))
    assert "opaque" in p.warning.text()                          # screen layout is the opaque confidence monitor
    p.set_layout_choice("preset:Corner overlay")
    assert not p.warning.text()
    w.demo_btn.click()
    assert p.start()
    assert fake.fake_keyer(0) == 2
    W, H = 1280, 720
    # Corner overlay's timer box (bg #000000C0) sits at x 0.755–0.98, y 0.845–0.965
    assert _wait(lambda: fake.fake_pixel(0, 1, int(0.76 * W), int(0.85 * H)) & 255 in range(185, 200))
    assert fake.fake_pixel(0, 1, 20, int(0.5 * H)) == 0xFF000000          # nothing there → key black
    p.update_status()
    assert "on air" in p.status.text() and "fill SDI A · key SDI B" in p.status.text()
    assert not p.device_combo.isEnabled()                         # locked while on air
    w.close()                                                     # shuts output down
    assert fake.fake_keyer(0) == 0 and not fake.fake_playing(0)
    # it was on air at quit → restarts with the same settings on next launch
    w2 = appmod.ControlWindow()
    qapp_dl.processEvents()
    assert w2.decklink_panel.output is not None and w2.decklink_panel.layout_choice == "preset:Corner overlay"
    w2.close()
    QSettings("TRUCUE", "DSANClockDL").clear()


def test_remote_controls_decklink(dl, qapp_dl, monkeypatch, tmp_path):
    """The browser remote drives the SDI panel: device, mode, cut/fill, layout, on air."""
    from PyQt6.QtCore import QSettings
    import dsan.clock.app as appmod
    from dsan.clock import files
    from dsan.clock.remote import RemoteAPI, RemoteError
    mod, fake = dl
    real = appmod.QSettings
    QSettings("TRUCUE", "DSANClockDLRemote").clear()
    monkeypatch.setattr(appmod, "QSettings", lambda *_a: real("TRUCUE", "DSANClockDLRemote"))
    monkeypatch.setattr(files, "last_dir", lambda kind: str(tmp_path))
    w = appmod.ControlWindow()
    api = RemoteAPI(w)
    p = w.decklink_panel
    st = api.state()["decklink"]
    assert st["available"] and not st["on_air"] and len(st["devices"]) == 2
    assert {k["value"]: k["enabled"] for k in st["keyings"]}["separate"] is True

    mode720 = next(m["value"] for m in st["modes"] if m["label"].startswith("720p59.94"))
    st = api.act({"action": "decklink", "mode": mode720, "keying": "external"})["decklink"]
    assert st["mode"] == mode720 and st["keying"] == "external" and "opaque" in st["warning"]
    st = api.act({"action": "decklink", "layout": "preset:Corner overlay", "premultiplied": True})["decklink"]
    assert st["layout"] == "preset:Corner overlay" and st["premultiplied"] and not st["warning"]

    w.layout_obj.save(str(tmp_path / "SDI look.json"))
    st = api.state()["decklink"]
    assert {"value": "file:SDI look.json", "label": "File: SDI look"} in st["layouts"]
    for bad in ({"layout": "file:../x.json"}, {"layout": "preset:Nope"}, {"layout": "zzz"},
                {"mode": "123456"}, {"device": "Nope"}, {"keying": "nope"}):
        with pytest.raises(RemoteError):
            api.act({"action": "decklink", **bad})

    st = api.act({"action": "decklink", "on": True})["decklink"]
    assert st["on_air"] and p.output is not None and fake.fake_keyer(0) == 2
    p.update_status()
    assert "on air" in api.state()["decklink"]["status"]["text"]
    with pytest.raises(RemoteError, match="Stop SDI output"):
        api.act({"action": "decklink", "mode": next(m["value"] for m in st["modes"] if m["value"] != mode720)})
    api.act({"action": "decklink", "mode": mode720, "on": True})          # same values while on air: fine
    st = api.act({"action": "decklink", "layout": "file:SDI look.json"})["decklink"]   # layout can change on air
    assert st["layout"] == "file:SDI look.json" and p.output is not None
    with pytest.raises(RemoteError):
        api.act({"action": "decklink", "rescan": True})
    st = api.act({"action": "decklink", "on": False})["decklink"]
    assert not st["on_air"] and not fake.fake_playing(0)

    st = api.act({"action": "decklink", "keying": "separate"})["decklink"]
    assert st["key_device"] != st["device"]
    with pytest.raises(RemoteError, match="different device"):
        api.act({"action": "decklink", "key_device": st["device"]})
    api.act({"action": "decklink", "rescan": True})
    w.close()
    QSettings("TRUCUE", "DSANClockDLRemote").clear()

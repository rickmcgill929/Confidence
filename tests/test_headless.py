"""Headless Confidence (Raspberry Pi): display choice, start-up, SIGTERM, self-update, power."""
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request

import pytest

from dsan.clock import headless, system
from dsan import updates


def drm(tmp_path, **outputs):
    for name, status in outputs.items():
        d = tmp_path / f"card1-{name}"
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")
    return str(tmp_path)


def test_choose_platform(tmp_path):
    hdmi = drm(tmp_path / "a", **{"HDMI-A-1": "connected", "HDMI-A-2": "disconnected"})
    none = drm(tmp_path / "b", **{"HDMI-A-1": "disconnected"})
    assert headless.displays_connected(hdmi) == ["HDMI-A-1"]
    assert headless.choose_platform({}, "linux", hdmi) == "linuxfb"
    assert headless.choose_platform({}, "linux", none) == "offscreen"
    assert headless.choose_platform({"CONFIDENCE_HDMI": "eglfs"}, "linux", hdmi) == "eglfs"
    assert headless.choose_platform({"CONFIDENCE_HDMI": "fb0"}, "linux", hdmi) == "linuxfb"
    assert headless.choose_platform({"CONFIDENCE_HDMI": "off"}, "linux", hdmi) == "offscreen"
    assert headless.choose_platform({"DISPLAY": ":0"}, "linux", hdmi) is None   # a desktop: use it
    assert headless.choose_platform({"QT_QPA_PLATFORM": "vnc"}, "linux", hdmi) is None
    assert headless.choose_platform({}, "darwin", hdmi) is None
    import dsan.clock.headless as h
    real = h.choose_platform
    h.choose_platform = lambda env: real(env, "linux", hdmi)
    try:
        e1, e2 = {}, {"CONFIDENCE_HDMI": "fb0"}
        assert h.prepare_environment(e1) == "linuxfb" and e1["QT_QPA_FB_DRM"] == "1"
        assert h.prepare_environment(e2) == "linuxfb" and "QT_QPA_FB_DRM" not in e2
    finally:
        h.choose_platform = real
    env = {"QT_QPA_PLATFORM": "offscreen"}
    assert headless.prepare_environment(env) == "offscreen"
    assert env["CONFIDENCE_HEADLESS"] == "1" and env["QT_QPA_EGLFS_DISABLE_INPUT"] == "1"
    assert env["CONFIDENCE_NO_UPDATE_CHECK"] == "1"


def test_headless_process_starts_and_stops_on_sigterm(tmp_path):
    """The real thing, as systemd runs it: remote up, settings saved on SIGTERM, exit 0."""
    import socket as _s
    with _s.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")}
    env.update(XDG_CONFIG_HOME=str(tmp_path), HOME=str(tmp_path), CONFIDENCE_HDMI="off",
               PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
    proc = subprocess.Popen([sys.executable, "-c", "import sys; from dsan.clock.app import main; sys.exit(main())",
                             "--headless", "--remote-port", str(port), "--name", "pi-test", "--remote-pin", "2468"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        state = None
        end = time.monotonic() + 20
        while time.monotonic() < end and state is None:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"X-PIN": "2468"})
                state = json.load(urllib.request.urlopen(req, timeout=1))
            except Exception:
                time.sleep(0.2)
        assert state is not None, "remote never came up"
        assert state["system"]["headless"] and not state["output"]["shown"]
        time.sleep(1.0)
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    assert "headless · display: offscreen" in out and "Confidence stopped" in out
    assert f":{port}" in out and "no PIN" not in out
    conf = (tmp_path / "TRUCUE" / "Confidence.conf").read_text()
    assert "enabled=true" in conf and f"port={port}" in conf and "name=pi-test" in conf


def release_tarball(tmp_path, version, marker):
    folder = f"Confidence-{version}-Linux-aarch64"
    path = tmp_path / f"{folder}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        data = f"#!/bin/sh\necho {marker}\n".encode()
        info = tarfile.TarInfo(f"{folder}/Confidence/Confidence")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return path


def release_json(version, path):
    return {"tag_name": f"v{version}", "html_url": "https://example/rel", "body": "notes",
            "assets": [{"name": path.name, "browser_download_url": path.as_uri(), "size": path.stat().st_size}]}


def test_self_update(tmp_path):
    base = tmp_path / "opt"
    (base / "versions" / "0.11.0" / "Confidence").mkdir(parents=True)
    os.symlink("versions/0.11.0", base / "current")
    (base / "versions" / "0.9.0").mkdir()                             # older: removed after updating
    new = release_tarball(tmp_path, "9.0.0", "new")
    quits = []
    ctl = system.SystemControl(lambda: quits.append(1), base=str(base), fetch=lambda: release_json("9.0.0", new),
                               platform_key="Linux-aarch64")
    assert ctl.can_update
    ctl.update()
    ctl._thread.join(10)
    assert ctl.phase == "restarting" and quits == [1], ctl.error
    assert os.readlink(base / "current") == os.path.join("versions", "9.0.0")
    assert (base / "current" / "Confidence" / "Confidence").read_text().strip().endswith("new")
    assert sorted(os.listdir(base / "versions")) == ["0.11.0", "9.0.0"]          # previous kept
    assert ctl.state()["latest"] == "9.0.0" and ctl.progress == 1.0

    # nothing newer: no change; no build for this computer: error, no change
    ctl2 = system.SystemControl(lambda: quits.append(2), base=str(base), fetch=lambda: release_json("0.0.1", new),
                                platform_key="Linux-aarch64")
    ctl2.update()
    ctl2._thread.join(10)
    assert ctl2.phase == "idle" and quits == [1]
    ctl3 = system.SystemControl(lambda: quits.append(3), base=str(base), fetch=lambda: release_json("99.0.0", new),
                                platform_key="Linux-x86_64")
    ctl3.update()
    ctl3._thread.join(10)
    assert ctl3.phase == "error" and "no Linux-x86_64 build" in ctl3.error and quits == [1]
    assert os.readlink(base / "current") == os.path.join("versions", "9.0.0")


def test_unpack_refuses_escapes(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("../../evil")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(updates.UpdateError, match="unsafe"):
        system.unpack(str(bad), str(tmp_path / "out"))
    link = tmp_path / "link.tar.gz"
    with tarfile.open(link, "w:gz") as tar:
        info = tarfile.TarInfo("x/Confidence")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(updates.UpdateError, match="unsafe"):
        system.unpack(str(link), str(tmp_path / "out2"))


def test_power_and_update_need_the_pi_install(monkeypatch):
    monkeypatch.delenv("CONFIDENCE_INSTALL_DIR", raising=False)
    monkeypatch.delenv("CONFIDENCE_SYSTEM_CONTROL", raising=False)
    ctl = system.SystemControl(lambda: None)
    assert not ctl.can_update and not ctl.state()["can_power"]
    with pytest.raises(ValueError, match="Raspberry Pi install"):
        ctl.update()
    with pytest.raises(ValueError, match="Raspberry Pi install"):
        ctl.power("reboot")
    with pytest.raises(ValueError):
        ctl.power("format")
    calls = []
    monkeypatch.setenv("CONFIDENCE_SYSTEM_CONTROL", "1")
    monkeypatch.setattr(system.sys, "platform", "linux")
    monkeypatch.setattr(system.subprocess, "run",
                        lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1])
    ctl.power("poweroff")
    assert calls == [["sudo", "-n", "/usr/bin/systemctl", "poweroff"]]
    monkeypatch.setattr(system.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required"))
    with pytest.raises(ValueError, match="password"):
        ctl.power("reboot")

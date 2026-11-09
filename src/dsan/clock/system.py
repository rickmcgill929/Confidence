"""Looking after a headless Confidence box (a Raspberry Pi) from the browser remote:
update to the latest release, restart, reboot, shut down.

Updating and power control only work in an installation made by
``packaging/pi/install.sh``, which runs Confidence as a systemd service with::

    CONFIDENCE_INSTALL_DIR=/opt/confidence        versions/<ver>/…  and  current -> versions/<ver>
    CONFIDENCE_SYSTEM_CONTROL=1                   may run: sudo -n systemctl reboot|poweroff

An update downloads this computer's release (``Confidence-<ver>-Linux-aarch64.tar.gz``
on a Pi), unpacks it next to the running one, points ``current`` at it and quits;
systemd starts the new version. The previous version is kept, so going back is
``ln -sfn versions/<old> current``.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from typing import Callable, Dict, Optional

from .. import __version__, updates


def install_dir() -> Optional[str]:
    d = os.environ.get("CONFIDENCE_INSTALL_DIR")
    return d if d and os.path.isdir(os.path.join(d, "versions")) else None


def power_allowed() -> bool:
    return os.environ.get("CONFIDENCE_SYSTEM_CONTROL") == "1" and sys.platform.startswith("linux")


def unpack(archive: str, dest: str) -> str:
    """Unpack a release .tar.gz into dest (refusing paths that leave it). Returns the
    folder that holds the ``Confidence/Confidence`` program."""
    root = os.path.realpath(dest)
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            target = os.path.realpath(os.path.join(root, m.name))
            if not (target == root or target.startswith(root + os.sep)) or m.isdev():
                raise updates.UpdateError(f"unsafe path in the update: {m.name}")
            if m.issym() or m.islnk():
                link = os.path.realpath(os.path.join(os.path.dirname(target), m.linkname))
                if not link.startswith(root + os.sep):
                    raise updates.UpdateError(f"unsafe link in the update: {m.name}")
        if hasattr(tarfile, "data_filter"):                  # Python 3.12+ (and patched 3.8-3.11)
            tar.extractall(root, filter="data")
        else:
            tar.extractall(root)
    for here, dirs, files in os.walk(root):
        if os.path.basename(here) == "Confidence" and "Confidence" in files:
            return os.path.dirname(here)
    raise updates.UpdateError("the update doesn't contain the Confidence program")


def switch_to(base: str, version: str, unpacked: str) -> str:
    """Move an unpacked release to base/versions/<version>, point base/current at it, keep
    the previous version, remove older ones. Returns the new version folder."""
    versions = os.path.join(base, "versions")
    target = os.path.join(versions, version)
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.move(unpacked, target)
    current = os.path.join(base, "current")
    previous = os.path.basename(os.path.realpath(current)) if os.path.islink(current) else None
    tmp_link = current + ".new"
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(os.path.join("versions", version), tmp_link)
    os.replace(tmp_link, current)                         # atomic
    for name in os.listdir(versions):
        if name not in (version, previous):
            shutil.rmtree(os.path.join(versions, name), ignore_errors=True)
    return target


class SystemControl:
    """State and actions for the remote's System section. Callbacks run on worker threads;
    ``quit_app`` is called (from the caller's thread) when Confidence should exit."""

    def __init__(self, quit_app: Callable[[], None], *, base: Optional[str] = None,
                 fetch: Callable[[], dict] = updates.fetch_latest, platform_key: Optional[str] = None):
        self.quit_app = quit_app
        self.base = base if base is not None else install_dir()
        self._fetch = fetch
        self._key = platform_key or updates.platform_key()
        self._lock = threading.Lock()
        self.info: Optional[updates.UpdateInfo] = None
        self.phase = "idle"            # idle | checking | downloading | installing | restarting | error
        self.progress = 0.0
        self.error = ""
        self._thread: Optional[threading.Thread] = None

    @property
    def can_update(self) -> bool:
        return self.base is not None

    def state(self) -> Dict[str, object]:
        info = self.info
        return {
            "version": __version__, "hostname": socket.gethostname(),
            "headless": os.environ.get("CONFIDENCE_HEADLESS") == "1",
            "can_update": self.can_update, "can_power": power_allowed(),
            "phase": self.phase, "progress": round(self.progress, 3), "error": self.error,
            "latest": info.latest if info else None,
            "available": bool(info and info.available),
            "has_build": bool(info and info.asset and info.asset.name.endswith(".tar.gz")),
            "page": info.page_url if info else f"https://github.com/{updates.REPO}/releases/latest",
        }

    def _run(self, fn):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("Busy — try again in a moment")
            self._thread = threading.Thread(target=fn, name="confidence-system", daemon=True)
            self._thread.start()

    def check(self) -> None:
        def work():
            self.phase, self.error = "checking", ""
            try:
                self.info = updates.from_release(self._fetch(), key=self._key)
                self.phase = "idle"
            except Exception as e:
                self.phase, self.error = "error", f"Couldn't check for updates: {e}"
        self._run(work)

    def update(self) -> None:
        if not self.can_update:
            raise ValueError("Updating from here needs the Raspberry Pi install (packaging/pi/install.sh)")

        def work():
            try:
                self.phase, self.error, self.progress = "checking", "", 0.0
                info = self.info = updates.from_release(self._fetch(), key=self._key)
                if not info.available:
                    self.phase = "idle"
                    return
                if not info.asset or not info.asset.name.endswith(".tar.gz"):
                    raise updates.UpdateError(f"release {info.latest} has no {self._key} build")
                self.phase = "downloading"
                with tempfile.TemporaryDirectory(dir=self.base) as tmp:
                    archive = os.path.join(tmp, info.asset.name)
                    self._download(info.asset.url, archive, info.asset.size)
                    self.phase = "installing"
                    unpacked = unpack(archive, os.path.join(tmp, "x"))
                    switch_to(self.base, info.latest, unpacked)
                self.phase = "restarting"
                self.quit_app()
            except Exception as e:
                self.phase, self.error = "error", f"Update failed: {e}"
        self._run(work)

    def _download(self, url: str, dest: str, size: int) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": f"Confidence/{__version__}"})
        with urllib.request.urlopen(req, timeout=30, context=updates.ssl_context()) as r, open(dest, "wb") as f:
            total = size or int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    self.progress = min(1.0, done / total)

    def restart(self) -> None:
        self.phase = "restarting"
        self.quit_app()                    # systemd (Restart=always) starts it again

    def power(self, what: str) -> None:
        if what not in ("reboot", "poweroff"):
            raise ValueError("reboot or poweroff")
        if not power_allowed():
            raise ValueError("Reboot and shut down need the Raspberry Pi install (packaging/pi/install.sh)")
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", what], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise ValueError((r.stderr or r.stdout or f"systemctl {what} failed").strip())


__all__ = ["SystemControl", "install_dir", "power_allowed", "switch_to", "unpack"]

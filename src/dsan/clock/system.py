"""The browser remote's System section: which version this is, and whether a newer
release is out (with a link to it). Installing updates stays with the app itself —
Sparkle on the Mac, the release page elsewhere.
"""
from __future__ import annotations

import socket
import threading
from typing import Callable, Dict, Optional

from .. import __version__, updates


class SystemControl:
    """State and the update check for the remote. The check runs on a worker thread."""

    def __init__(self, *, fetch: Callable[[], dict] = updates.fetch_latest, platform_key: Optional[str] = None):
        self._fetch = fetch
        self._key = platform_key or updates.platform_key()
        self._lock = threading.Lock()
        self.info: Optional[updates.UpdateInfo] = None
        self.phase = "idle"            # idle | checking | error
        self.error = ""
        self._thread: Optional[threading.Thread] = None

    def state(self) -> Dict[str, object]:
        info = self.info
        return {
            "version": __version__, "hostname": socket.gethostname(),
            "phase": self.phase, "error": self.error,
            "latest": info.latest if info else None,
            "available": bool(info and info.available),
            "page": info.page_url if info else f"https://github.com/{updates.REPO}/releases/latest",
        }

    def check(self) -> None:
        def work():
            self.phase, self.error = "checking", ""
            try:
                self.info = updates.from_release(self._fetch(), key=self._key)
                self.phase = "idle"
            except Exception as e:
                self.phase, self.error = "error", f"Couldn't check for updates: {e}"
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("Already checking — try again in a moment")
            self._thread = threading.Thread(target=work, name="confidence-update-check", daemon=True)
            self._thread.start()


__all__ = ["SystemControl"]

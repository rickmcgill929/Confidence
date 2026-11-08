"""Announce the remote page on the local network with Bonjour / mDNS, so phones and
computers can open ``http://confidence.local:8080`` instead of an IP address.

Publishes an address record for ``<name>.local`` (pointing at this computer's LAN
addresses) and an ``_http._tcp`` service called "Confidence" (so it also shows up in
Bonjour browsers). Uses the pure-Python ``zeroconf`` package; works next to the
system's own responder (mDNSResponder on macOS, Windows' mDNS, Avahi on Linux).

If ``<name>.local`` is already answered by another computer (a second Confidence on
the same network), ``-2``, ``-3``… is added, the way Macs rename themselves.

Everything slow happens on a background thread; read :attr:`hostname` / :attr:`state`.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
from typing import List, Optional

DEFAULT_NAME = "confidence"
SERVICE_TYPE = "_http._tcp.local."


def clean_name(text: str) -> str:
    """A valid single DNS label: lower case letters, digits and hyphens (max 40)."""
    name = re.sub(r"[^a-z0-9-]+", "-", (text or "").strip().lower()).strip("-")
    return name[:40].strip("-") or DEFAULT_NAME


def available() -> bool:
    try:
        import zeroconf  # noqa: F401
        return True
    except Exception:
        return False


class Announcer:
    """Publish ``<name>.local`` → `addresses`, and the web page as a Bonjour service."""

    def __init__(self, name: str, port: int, addresses: List[str], path: str = "/"):
        self.wanted = clean_name(name)
        self.port = port
        self.addresses = [a for a in addresses if _is_ipv4(a)]
        self.path = path
        self.hostname: Optional[str] = None       # e.g. "confidence.local" once published
        self.state = "starting"                   # starting | ok | failed
        self.error = ""
        self._zc = None
        self._info = None
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="confidence-bonjour", daemon=True)
        self._thread.start()

    # -- background ---------------------------------------------------------------

    def _run(self):
        try:
            from zeroconf import AddressResolverIPv4, IPVersion, ServiceInfo, Zeroconf
            if not self.addresses:
                raise OSError("no network address")
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            with self._lock:
                if self._closed.is_set():
                    zc.close()
                    return
                self._zc = zc
            ours = set(self.addresses)
            label = self.wanted
            for n in range(1, 20):
                label = self.wanted if n == 1 else f"{self.wanted}-{n}"
                if not _taken(zc, AddressResolverIPv4, label, ours):
                    break
                if self._closed.is_set():
                    return
            info = ServiceInfo(
                SERVICE_TYPE, f"Confidence ({label}).{SERVICE_TYPE}",
                addresses=[socket.inet_aton(a) for a in self.addresses], port=self.port,
                properties={"path": self.path, "app": "Confidence"}, server=f"{label}.local.")
            zc.register_service(info, allow_name_change=True)
            with self._lock:
                if self._closed.is_set():
                    return
                self._info = info
                self.hostname = f"{label}.local"
                self.state = "ok"
        except Exception as e:                     # never take the remote page down with it
            self.state, self.error = "failed", str(e) or e.__class__.__name__

    # -- control -------------------------------------------------------------------

    def wait(self, timeout: float = 10.0) -> bool:
        """For tests: wait until published (or failed)."""
        self._thread.join(timeout)
        return self.state == "ok"

    def close(self, wait: bool = False) -> None:
        """Withdraw the name. Runs in the background (saying goodbye takes a moment)."""
        self._closed.set()

        def stop():
            self._thread.join(15)
            with self._lock:
                zc, info, self._zc, self._info = self._zc, self._info, None, None
            if zc is None:
                return
            try:
                if info is not None:
                    zc.unregister_service(info)        # tells other devices it's gone
            except Exception:
                pass
            try:
                zc.close()
            except Exception:
                pass
        t = threading.Thread(target=stop, name="confidence-bonjour-stop", daemon=True)
        t.start()
        if wait:
            t.join(20)


def _taken(zc, resolver, label: str, ours: set) -> bool:
    """Does another computer answer for <label>.local? Asks three times: a responder that
    has only just started can miss the first answer."""
    for _ in range(3):
        probe = resolver(f"{label}.local.")
        if probe.request(zc, 1000):
            return not set(probe.parsed_addresses()) <= ours
    return False


def _is_ipv4(a: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(a)
    except ValueError:
        return False
    return not ip.is_loopback and not ip.is_unspecified


__all__ = ["Announcer", "DEFAULT_NAME", "available", "clean_name"]

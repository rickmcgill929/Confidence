"""Follow TRUCUE (trucue.io) over OSC: clip, countdown to OUT, next / previous clip,
the next bookmark, play state — pure Python, no I/O in :class:`TruCueTracker`.

Turn feedback on in TRUCUE: Settings → OSC → **Companion feedback**, host = this
computer, port = the one :class:`TruCueReceiver` listens on (TRUCUE's default 9017).
TRUCUE sends single-string OSC messages on fixed addresses (whatever the command prefix):

    /trucue/status            {"clip","index","remaining","playing","next","prev","vol","bmName","bmRemaining"}
    /trucue/status/playlist   ["Clip 1","Clip 2",…]

about 10 times a second while values change and once a second when idle
(see github.com/bitfocus/companion-module-pkinc-trucue, src/status.js).

Clip names carry TRUCUE's mode markers — "Main Show 🔁B": 🔁 loop, → auto-next,
↺ auto-first, B cue in black. Views give the plain name plus the mode.

TRUCUE doesn't send a clip's length, so ``duration`` is estimated as the largest
"remaining" seen since the clip was loaded — exact when it was loaded from the top.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

STATUS = "/trucue/status"
PLAYLIST = "/trucue/status/playlist"
LIVE_AFTER = 5.0                   # TRUCUE heartbeats once a second; nothing for 5 s → not live
_MARKERS = re.compile(r"\s+([🔁→↺B]+)\s*$")
_SYMBOLS = {"🔁": "loop", "→": "auto-next", "↺": "auto-first"}


def split_markers(name: str) -> Tuple[str, str, bool]:
    """'Main Show 🔁B' → ('Main Show', 'loop', True). A lone trailing 'B' ('Plan B') stays in the name."""
    m = _MARKERS.search(name or "")
    if not m or not any(c in m.group(1) for c in _SYMBOLS):
        return (name or "").strip(), "", False
    marks = m.group(1)
    mode = next((_SYMBOLS[c] for c in marks if c in _SYMBOLS), "")
    return name[:m.start()].strip(), mode, "B" in marks


def _num(v, default=None):
    try:
        f = float(v)
        return f if f == f else default            # NaN → default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class TruCueView:
    """What TRUCUE is doing right now (same shape as MilluminView where it overlaps)."""
    clip: str                          # plain name, markers removed
    clip_raw: str                      # as TRUCUE sent it, with markers
    index: int                         # 1-based, clips only
    remaining: float                   # to the trim-aware OUT, running on between messages
    duration: Optional[float]          # estimated (largest remaining since the clip loaded)
    state: str                         # "playing" | "paused"
    mode: str                          # "loop" | "auto-next" | "auto-first" | ""
    cue_in_black: bool
    next: str
    prev: str
    volume: Optional[float]            # dB
    bookmark: str
    bookmark_remaining: Optional[float]
    live: bool

    @property
    def elapsed(self) -> float:
        return max(0.0, (self.duration or self.remaining) - self.remaining)

    @property
    def fraction(self) -> Optional[float]:
        if not self.duration:
            return None
        return min(1.0, max(0.0, self.elapsed / self.duration))

    @property
    def status(self) -> str:
        return self.state.upper()

    @property
    def layer(self) -> str:            # MilluminView compatibility
        return ""

    @property
    def column(self) -> str:
        return ""

    @property
    def loops(self) -> int:
        return 0


class TruCueTracker:
    """Feed it TRUCUE's OSC feedback with :meth:`handle`; read it with :meth:`view`. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._st: Optional[dict] = None
        self._at = 0.0                              # monotonic time of the last status
        self._key: Optional[Tuple[str, int]] = None
        self._max_remaining = 0.0
        self.playlist: List[str] = []
        self.last_message = 0.0
        self.received = 0

    def handle(self, address: str, *args, now: Optional[float] = None) -> bool:
        """Apply one OSC message. Returns False if it isn't TRUCUE feedback."""
        addr = address.lower()
        if addr not in (STATUS, PLAYLIST):
            return False
        text = args[0] if args and isinstance(args[0], str) else None
        if text is None:
            return False
        try:
            data = json.loads(text)
        except ValueError:
            return False
        now = time.monotonic() if now is None else now
        with self._lock:
            self.received += 1
            self.last_message = now
            if addr == PLAYLIST:
                if not isinstance(data, list):
                    return False
                self.playlist = [str(n if n is not None else "") for n in data]
                return True
            if not isinstance(data, dict):
                return False
            st = {
                "clip": data.get("clip") if isinstance(data.get("clip"), str) else "",
                "index": max(0, int(round(_num(data.get("index"), 0)))),
                "remaining": max(0.0, _num(data.get("remaining"), 0.0)),
                "playing": bool(_num(data.get("playing"), 0)),
                "next": data.get("next") if isinstance(data.get("next"), str) else "",
                "prev": data.get("prev") if isinstance(data.get("prev"), str) else "",
                "vol": None if data.get("vol") is None else _num(data.get("vol")),
                "bmName": data.get("bmName") if isinstance(data.get("bmName"), str) else "",
                "bmRemaining": None if data.get("bmRemaining") is None else max(0.0, _num(data.get("bmRemaining"), 0.0)),
            }
            if st["bmRemaining"] is not None and st["bmRemaining"] < 0:
                st["bmRemaining"] = None
            key = (st["clip"], st["index"])
            if key != self._key:                    # a new clip: start the length estimate again
                self._key, self._max_remaining = key, 0.0
            self._max_remaining = max(self._max_remaining, st["remaining"])
            self._st, self._at = st, now
            return True

    def view(self, now: Optional[float] = None) -> Optional[TruCueView]:
        """None until TRUCUE has said something, or while no clip is loaded."""
        now = time.monotonic() if now is None else now
        with self._lock:
            st = self._st
            if st is None or (not st["clip"] and st["index"] == 0):
                return None
            run = max(0.0, now - self._at) if st["playing"] else 0.0
            remaining = max(0.0, st["remaining"] - run)
            bm = st["bmRemaining"]
            if bm is not None:
                bm = max(0.0, bm - run)
            name, mode, black = split_markers(st["clip"])
            return TruCueView(
                clip=name, clip_raw=st["clip"], index=st["index"], remaining=remaining,
                duration=self._max_remaining or None, state="playing" if st["playing"] else "paused",
                mode=mode, cue_in_black=black, next=split_markers(st["next"])[0], prev=split_markers(st["prev"])[0],
                volume=st["vol"], bookmark=st["bmName"] if bm is not None else "", bookmark_remaining=bm,
                live=bool(self.last_message) and now - self.last_message < LIVE_AFTER)


class TruCueReceiver:
    """Listen for TRUCUE's feedback on a UDP port and feed a :class:`TruCueTracker`.

    ``forward=("127.0.0.1", 9018)`` passes every packet on unchanged — TRUCUE sends its
    feedback to one place, so this keeps a Companion on the same computer working.
    ``on_message(address, args, used)`` is called for every OSC message (listener thread).
    """

    def __init__(self, port: int, host: str = "0.0.0.0", *, tracker: Optional[TruCueTracker] = None,
                 forward: Optional[Tuple[str, int]] = None,
                 on_message: Optional[Callable[[str, tuple, bool], None]] = None):
        try:
            from pythonosc.osc_packet import OscPacket
        except ImportError as e:  # pragma: no cover
            raise ImportError("TRUCUE feedback needs python-osc: pip install 'dsan[osc]'") from e
        self._OscPacket = OscPacket
        self.tracker = tracker or TruCueTracker()
        self.forward = forward
        self.on_message = on_message
        self.forwarded = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        self._sock.settimeout(0.25)
        self.host, self.port = self._sock.getsockname()[:2]
        self._out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if forward else None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="dsan-trucue", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if self._out is not None:
                try:
                    self._out.sendto(data, self.forward)
                    self.forwarded += 1
                except OSError:
                    pass
            try:
                messages = [m.message for m in self._OscPacket(data).messages]
            except Exception:
                continue
            for msg in messages:
                args = tuple(msg.params)
                try:
                    used = self.tracker.handle(msg.address, *args)
                except Exception:
                    used = False
                if self.on_message:
                    try:
                        self.on_message(msg.address, args, used)
                    except Exception:
                        pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sock.close()
        if self._out is not None:
            self._out.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


__all__ = ["LIVE_AFTER", "TruCueReceiver", "TruCueTracker", "TruCueView", "split_markers"]

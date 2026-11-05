"""Follow Millumin over OSC: clip name, duration (TRT), elapsed / remaining time,
play state, and loop / freeze — pure Python, no I/O in :class:`MilluminTracker`.

Turn feedback on in Millumin: Device Manager (⌘K) → OSC → *API feedback*, and
send it to this computer's IP and the port :class:`MilluminReceiver` listens on.

Messages used (see https://github.com/anome/millumin-dev-kit/wiki/OSC-documentation)::

    /millumin/layer:NAME/mediaStarted   [index, "clip", duration?]
    /millumin/layer:NAME/mediaPaused    [index, "clip"]
    /millumin/layer:NAME/mediaStopped   [index, "clip"]
    /millumin/layer:NAME/selected
    /millumin/selectedLayer/media/time  [seconds, duration]
    /millumin/selectedLayer/media/normalizedTime   [0-1]
    /millumin/selectedLayer/media/speed [speed]
    /millumin/selectedLayer/mediaStarted|mediaPaused|mediaStopped   (same args)
    /millumin/board/launchedColumn      [index, "name"]   (also columnLaunched)
    /millumin/board/stoppedColumn       [index, "name"]   (also columnStopped)

``/millumin/index:N/...`` duplicates of the layer messages are ignored.

Millumin doesn't send a clip's loop mode, so it is worked out from the time feed:

* **loop** — the time jumps from the end of the clip back to the start while it plays;
* **freeze** — the time reaches the end and stays there with no *mediaStopped*.

Columns decide what's on screen. While a column is current, a layer holds its clip:
when the media stops, the clip name, duration and time stay up, frozen where it
stopped (state "stopped"). Launching another column clears every layer, and what
starts on the new column takes over; a layer with nothing on the new column is
empty (:meth:`MilluminTracker.view` returns None). Stopping the column clears all.
Media that started just before the *launchedColumn* message arrived belongs to the
new column and is kept.

Between time messages (and for layers that only send *mediaStarted*) the time runs
on from the last known value, so a countdown keeps ticking smoothly.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

PREFIX = "/millumin"
SELECTED = ""                  # MilluminTracker.view(layer=SELECTED) follows Millumin's selected layer
_END = 0.15                    # seconds from the end that count as "at the end"
_LOOP_WINDOW = 1.5             # a jump back from within this many seconds of the end is a loop
_FREEZE_AFTER = 0.5            # at the end and not moving for this long → freeze
LIVE_AFTER = 5.0               # no Millumin message for this long → not live


@dataclass
class _Layer:
    name: str
    clip: str = ""
    index: Optional[int] = None
    duration: Optional[float] = None
    time: float = 0.0
    state: str = "stopped"            # playing | paused | stopped
    speed: float = 1.0
    looping: bool = False
    loops: int = 0
    sampled: float = 0.0              # monotonic time of `time`
    fed: bool = False                 # time came from a time message (not just mediaStarted)
    moved: float = 0.0                # monotonic time the time last changed
    last_msg: float = 0.0
    started: float = -1e9             # monotonic time of the last mediaStarted


_NEW_COLUMN_GRACE = 0.5               # media started this close before a column launch belongs to it


@dataclass(frozen=True)
class MilluminView:
    """What one Millumin layer is doing right now."""
    layer: str
    clip: str
    duration: Optional[float]
    elapsed: float
    state: str                        # "playing" | "paused" | "stopped" (held until the next column)
    mode: str                         # "loop" | "freeze" | ""
    loops: int
    column: str
    live: bool                        # a Millumin message arrived recently

    @property
    def remaining(self) -> Optional[float]:
        return None if self.duration is None else max(0.0, self.duration - self.elapsed)

    @property
    def fraction(self) -> Optional[float]:
        """Elapsed / duration, 0-1."""
        if not self.duration:
            return None
        return min(1.0, max(0.0, self.elapsed / self.duration))

    @property
    def status(self) -> str:
        """PLAYING | PAUSED | STOPPED | FREEZE (frozen on the last frame)."""
        return "FREEZE" if self.mode == "freeze" else self.state.upper()


def format_time(seconds: Optional[float], *, round_up: bool = False) -> str:
    """M:SS, or H:MM:SS from an hour. ``round_up`` for countdowns (0:00 only at the very end)."""
    if seconds is None:
        return "--:--"
    s = max(0, int(math.ceil(seconds - 1e-6) if round_up else math.floor(seconds + 1e-6)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class MilluminTracker:
    """Feed it Millumin's OSC feedback with :meth:`handle`; read layers with :meth:`view`. Thread-safe."""

    def __init__(self, prefix: str = PREFIX):
        self.prefix = prefix.rstrip("/")
        self._layers: Dict[str, _Layer] = {}
        self._lock = threading.Lock()
        self.selected: Optional[str] = None
        self.column = ""
        self.column_index: Optional[int] = None         # the launched column, if Millumin said
        self.last_message: float = 0.0
        self.received = 0
        self.unknown: List[Tuple[str, tuple]] = []      # last few unrecognised /millumin messages

    # -- input -------------------------------------------------------------------

    def handle(self, address: str, *args, now: Optional[float] = None) -> bool:
        """Apply one OSC message. Returns False if it isn't a Millumin message we use."""
        if not address.startswith(self.prefix + "/"):
            return False
        now = time.monotonic() if now is None else now
        with self._lock:
            self.received += 1
            self.last_message = now
            ok = self._apply(address[len(self.prefix) + 1:], args, now)
            if not ok:
                self.unknown = (self.unknown + [(address, args)])[-20:]
            return ok

    def _layer(self, target: str, now: float) -> Optional[_Layer]:
        if target == "selectedLayer":
            name = self.selected or "(selected layer)"
        elif target.startswith("layer:"):
            name = target[6:]
        else:
            return None
        lay = self._layers.get(name)
        if lay is None:
            lay = self._layers[name] = _Layer(name, sampled=now, moved=now)
        lay.last_msg = now
        return lay

    def _apply(self, path: str, args: tuple, now: float) -> bool:
        target, _, rest = path.partition("/")
        if target == "board":
            index = int(args[0]) if args and _num(args[0]) is not None else None
            name = str(args[1]) if len(args) > 1 else (f"Column {index}" if index is not None else "")
            if rest in ("launchedColumn", "columnLaunched"):
                repeat = index is not None and index == self.column_index and name == self.column
                self.column, self.column_index = name, index
                if not repeat:                             # new column: the old column's media goes
                    self._clear_layers(now)
                return True
            if rest in ("stoppedColumn", "columnStopped"):
                if index is not None and self.column_index is not None and index != self.column_index:
                    return True                            # the previous column stopping after we advanced
                self.column, self.column_index = "", None
                self._clear_layers(now)
                return True
            return False
        if target.startswith("index:") or target.startswith("light:"):
            return True                                   # duplicates / not media: known, unused
        if rest == "selected" and target.startswith("layer:"):
            if not args or bool(args[0]):
                self.selected = target[6:]
                placeholder = self._layers.pop("(selected layer)", None)
                if placeholder and self.selected not in self._layers:
                    placeholder.name = self.selected
                    self._layers[self.selected] = placeholder
            return True
        lay = self._layer(target, now)
        if lay is None:
            return False
        if rest == "mediaStarted":
            if args and _num(args[0]) is not None:
                lay.index = int(args[0])
            new_clip = str(args[1]) if len(args) > 1 else lay.clip
            changed = new_clip != lay.clip
            if changed:
                lay.looping, lay.loops = False, 0
            lay.clip = new_clip
            dur = _num(args[2]) if len(args) > 2 else None
            if dur is not None and dur > 0:
                lay.duration = dur
            elif changed:
                lay.duration = None
            if changed or lay.state != "paused":         # resuming the same clip keeps the time
                lay.time, lay.fed = 0.0, False
            lay.state = "playing"
            lay.sampled = lay.moved = lay.started = now
            return True
        if rest == "mediaPaused":
            lay.time = self._elapsed(lay, now)
            lay.state, lay.sampled = "paused", now
            return True
        if rest == "mediaStopped":
            name = str(args[1]) if len(args) > 1 else None
            if name is not None and lay.clip and name != lay.clip:
                return True                              # a late stop for media already replaced
            if lay.clip:                                 # hold it, frozen where it stopped
                lay.time = self._elapsed(lay, now)
                lay.state, lay.fed, lay.sampled = "stopped", False, now
            else:
                self._clear(lay, now)
            return True
        if rest == "media/time":
            t = _num(args[0]) if args else None
            if t is None:
                return False
            dur = _num(args[1]) if len(args) > 1 else None
            if dur is not None and dur > 0:
                lay.duration = dur
            self._time(lay, t, now)
            return True
        if rest == "media/normalizedTime":
            n = _num(args[0]) if args else None
            if n is None:
                return False
            if lay.duration:
                self._time(lay, n * lay.duration, now)
            return True
        if rest == "media/speed":
            s = _num(args[0]) if args else None
            if s is not None:
                lay.speed = s
            return True
        return False

    def _time(self, lay: _Layer, t: float, now: float) -> None:
        prev = lay.time if lay.fed else None
        d = lay.duration
        if prev is not None and d and t < prev - 0.25 and prev >= d - _LOOP_WINDOW and t <= _LOOP_WINDOW:
            lay.looping = True
            lay.loops += 1
        if prev is None or abs(t - prev) > 1e-4:
            lay.moved = now
            if lay.state == "stopped" and prev is not None:
                lay.state = "playing"                    # the time is moving: there's media on the layer
        lay.time, lay.sampled, lay.fed = t, now, True

    def _clear_layers(self, now: float) -> None:
        """Clear every layer, except media that started a moment ago (it belongs to the new column)."""
        for lay in self._layers.values():
            if now - lay.started > _NEW_COLUMN_GRACE:
                self._clear(lay, now)

    @staticmethod
    def _clear(lay: _Layer, now: float) -> None:
        """Nothing on this layer any more."""
        lay.state, lay.clip, lay.duration, lay.index = "stopped", "", None, None
        lay.time, lay.fed, lay.looping, lay.loops = 0.0, False, False, 0
        lay.sampled = lay.moved = now

    def _elapsed(self, lay: _Layer, now: float) -> float:
        t = lay.time
        if lay.state == "playing":
            t += max(0.0, now - lay.sampled) * lay.speed
        d = lay.duration
        if d:
            if t > d:
                t = (t % d) if lay.looping else d
        return max(0.0, t)

    # -- output ------------------------------------------------------------------

    def layers(self) -> List[str]:
        with self._lock:
            return sorted(n for n in self._layers if n != "(selected layer)")

    def view(self, layer: str = SELECTED, now: Optional[float] = None) -> Optional[MilluminView]:
        """The named layer, or the selected one (SELECTED). None when the current column has nothing on it."""
        now = time.monotonic() if now is None else now
        with self._lock:
            name = layer or self.selected or "(selected layer)"
            lay = self._layers.get(name)
            if lay is None and not layer and len(self._layers) == 1:
                lay = next(iter(self._layers.values()))   # one layer in use: follow it
            if lay is None or (lay.state == "stopped" and not lay.clip):
                return None
            elapsed = self._elapsed(lay, now)
            mode = "loop" if lay.looping else ""
            d = lay.duration
            if (lay.state == "playing" and d and elapsed >= d - _END and not lay.looping
                    and now - lay.moved >= _FREEZE_AFTER - 1e-9):
                mode = "freeze"
            return MilluminView(layer=lay.name, clip=lay.clip, duration=d, elapsed=elapsed,
                                state=lay.state, mode=mode, loops=lay.loops, column=self.column,
                                live=bool(self.last_message) and now - self.last_message < LIVE_AFTER)


class MilluminReceiver:
    """Listen for Millumin's OSC feedback on a UDP port and feed a :class:`MilluminTracker`.

    >>> rx = MilluminReceiver(5001)
    >>> rx.tracker.view()               # the selected layer
    >>> rx.close()

    ``on_message(address, args, used)`` is called for every message (on the listener thread).
    """

    def __init__(self, port: int, host: str = "0.0.0.0", *, tracker: Optional[MilluminTracker] = None,
                 on_message: Optional[Callable[[str, tuple, bool], None]] = None):
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
        except ImportError as e:  # pragma: no cover
            raise ImportError("Millumin OSC needs python-osc: pip install 'dsan[osc]'") from e
        self.tracker = tracker or MilluminTracker()
        self.on_message = on_message
        d = Dispatcher()
        d.set_default_handler(self._handle)
        self._server = ThreadingOSCUDPServer((host, port), d)
        self.host, self.port = self._server.server_address[:2]
        threading.Thread(target=self._server.serve_forever, name="dsan-millumin", daemon=True).start()

    def _handle(self, address: str, *args) -> None:
        try:
            used = self.tracker.handle(address, *args)
        except Exception:                          # a malformed message must not stop the server
            used = False
        if self.on_message:
            try:
                self.on_message(address, args, used)
            except Exception:
                pass

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


__all__ = ["LIVE_AFTER", "MilluminReceiver", "MilluminTracker", "MilluminView", "SELECTED", "format_time"]

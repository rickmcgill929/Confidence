"""Threaded clients for a Limitimer and a Perfect Cue receiver.

Each client owns one background reader thread that opens the port, reconnects
automatically if it drops, and calls your callbacks. **Callbacks run on that
reader thread** — hand off to your GUI/event loop if needed (the bundled Qt
viewer and aiohttp server show how).

``port`` is anything pyserial's ``serial_for_url`` accepts: a device path
(``/dev/tty.usbserial-A1``, ``COM3``), ``socket://10.0.0.50:4001`` for an
RS-485-to-Ethernet converter, or ``rfc2217://…``.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, List, Optional, Union

from . import limitimer as lt
from . import perfectcue as pc

log = logging.getLogger("dsan")


class LinkState(str, Enum):
    CLOSED = "closed"              # not started / stopped
    OPENING = "opening"            # trying to open the port for the first time
    RECONNECTING = "reconnecting"  # port was lost; retrying
    OPEN = "open"                  # port open (Perfect Cue has no heartbeat)
    WAITING = "waiting"            # Limitimer: port open, no valid packet yet
    LIVE = "live"                  # Limitimer: valid packets arriving
    STALE = "stale"                # Limitimer: port open but no packet for `stale_after` s

    @property
    def is_live(self) -> bool:
        return self in (LinkState.LIVE, LinkState.OPEN)


Transport = Any   # pyserial-like: read(n), write(b), close(), in_waiting, is_open


class _SerialDevice:
    """Shared port lifecycle: open → read loop → reconnect, plus callback fan-out."""

    _OPEN_STATE = LinkState.OPEN
    _READ_TIMEOUT = 0.1

    def __init__(self, port: Optional[str], baudrate: int, *, reconnect: bool = True,
                 reconnect_interval: float = 1.0,
                 transport_factory: Optional[Callable[[], Transport]] = None):
        if port is None and transport_factory is None:
            raise ValueError("give a port or a transport_factory")
        self.port = port
        self.baudrate = baudrate
        self.reconnect = reconnect
        self.reconnect_interval = reconnect_interval
        self._transport_factory = transport_factory
        self._conn: Optional[Transport] = None
        self._conn_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._link = LinkState.CLOSED
        self._link_cbs: List[Callable[[LinkState], None]] = []
        self._raw_cbs: List[Callable[[bytes], None]] = []
        self._error_cbs: List[Callable[[Exception], None]] = []
        self.last_error: Optional[str] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "_SerialDevice":
        if self._thread and self._thread.is_alive():
            return self
        self._running.set()
        self._thread = threading.Thread(target=self._run, name=f"dsan-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._running.clear()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def running(self) -> bool:
        return self._running.is_set()

    @property
    def link(self) -> LinkState:
        return self._link

    @property
    def name(self) -> str:
        return type(self).__name__.lower()

    # -- callbacks (decorator-friendly: each returns the function) ----------

    def on_link(self, cb: Callable[[LinkState], None]):
        """Called with the new LinkState whenever it changes."""
        self._link_cbs.append(cb)
        return cb

    def on_raw(self, cb: Callable[[bytes], None]):
        """Called with every chunk of raw bytes read from the port."""
        self._raw_cbs.append(cb)
        return cb

    def on_error(self, cb: Callable[[Exception], None]):
        """Called with port errors (open failures, disconnects)."""
        self._error_cbs.append(cb)
        return cb

    def remove_callback(self, cb: Callable) -> None:
        for lst in self._callback_lists():
            while cb in lst:
                lst.remove(cb)

    def _callback_lists(self) -> List[list]:
        return [self._link_cbs, self._raw_cbs, self._error_cbs]

    @staticmethod
    def _emit(cbs: list, *args) -> None:
        for cb in list(cbs):
            try:
                cb(*args)
            except Exception:   # a bad callback must never kill the reader
                log.exception("dsan callback %r failed", cb)

    def _set_link(self, state: LinkState) -> None:
        if state != self._link:
            self._link = state
            self._emit(self._link_cbs, state)

    # -- I/O -----------------------------------------------------------------

    def _open(self) -> Transport:
        if self._transport_factory:
            return self._transport_factory()
        import serial  # imported lazily so pure-protocol users don't need pyserial
        return serial.serial_for_url(self.port, baudrate=self.baudrate, bytesize=8,
                                     parity="N", stopbits=1, timeout=self._READ_TIMEOUT)

    def write(self, data: bytes) -> bool:
        """Write to the port. Returns False if it isn't open right now."""
        with self._conn_lock:
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.write(data)
                return True
            except Exception as e:
                self._report(e)
                return False

    def _report(self, e: Exception) -> None:
        self.last_error = str(e)
        self._emit(self._error_cbs, e)

    def _run(self) -> None:
        self._set_link(LinkState.OPENING)
        while self._running.is_set():
            try:
                conn = self._open()
                with self._conn_lock:
                    self._conn = conn
                self.last_error = None
                self._on_open()
                while self._running.is_set():
                    data = conn.read(max(1, conn.in_waiting))
                    if data:
                        self._emit(self._raw_cbs, data)
                        self._on_bytes(data)
                    self._on_idle()
            except Exception as e:
                if self._running.is_set():
                    self._report(e)
            finally:
                with self._conn_lock:
                    conn, self._conn = self._conn, None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            if not (self._running.is_set() and self.reconnect):
                break
            self._set_link(LinkState.RECONNECTING)
            deadline = time.monotonic() + self.reconnect_interval
            while self._running.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
        self._running.clear()
        self._set_link(LinkState.CLOSED)

    # hooks for subclasses
    def _on_open(self) -> None:
        self._set_link(self._OPEN_STATE)

    def _on_bytes(self, data: bytes) -> None:
        pass

    def _on_idle(self) -> None:
        pass


class Limitimer(_SerialDevice):
    """A DSAN Limitimer on an RS-485 bus.

    >>> lt = Limitimer("/dev/tty.usbserial-A1")
    >>> @lt.on_change
    ... def changed(state, previous):
    ...     print(state.display(1), state.selected.number)
    >>> lt.start()
    """

    _OPEN_STATE = LinkState.WAITING

    def __init__(self, port: Optional[str] = None, *, baudrate: int = lt.BAUDRATE,
                 stale_after: float = 2.0, accept_zero_crc: bool = True, **kwargs):
        super().__init__(port, baudrate, **kwargs)
        #: Seconds without a valid packet (status or ping) before the link goes STALE.
        self.stale_after = stale_after
        self.decoder = lt.Decoder(accept_zero_crc=accept_zero_crc)
        self._state: Optional[lt.LimitimerState] = None
        self._state_cond = threading.Condition()
        self._state_cbs: List[Callable[[lt.LimitimerState], None]] = []
        self._change_cbs: List[Callable[[lt.LimitimerState, Optional[lt.LimitimerState]], None]] = []
        self._packet_cbs: List[Callable[[lt.PacketEvent], None]] = []
        self.last_rx: Optional[float] = None   # time.monotonic() of last valid packet
        self.stats = {"packets": 0, "status": 0, "ping": 0, "errors": 0, "zero_crc": 0}

    @classmethod
    def simulated(cls, speed: float = 1.0, **kwargs) -> "Limitimer":
        """A Limitimer fed by the built-in show simulator — no hardware needed."""
        from .sim import LimitimerSimulator
        kwargs.setdefault("reconnect", False)
        return cls(transport_factory=lambda: LimitimerSimulator(speed=speed), **kwargs)

    # -- callbacks -------------------------------------------------------------

    def on_state(self, cb: Callable[[lt.LimitimerState], None]):
        """Called with every decoded status message (several per second)."""
        self._state_cbs.append(cb)
        return cb

    def on_change(self, cb: Callable[[lt.LimitimerState, Optional[lt.LimitimerState]], None]):
        """Called with (state, previous) only when something other than the sequence counter changed."""
        self._change_cbs.append(cb)
        return cb

    def on_packet(self, cb: Callable[[lt.PacketEvent], None]):
        """Called for every framed packet, valid or not — for debug consoles."""
        self._packet_cbs.append(cb)
        return cb

    def _callback_lists(self):
        return super()._callback_lists() + [self._state_cbs, self._change_cbs, self._packet_cbs]

    # -- state -----------------------------------------------------------------

    @property
    def state(self) -> Optional[lt.LimitimerState]:
        """Latest decoded state, or None before the first status message."""
        return self._state

    def wait_for_state(self, timeout: Optional[float] = None) -> Optional[lt.LimitimerState]:
        """Block until the next status message arrives (or timeout). Returns it or None."""
        with self._state_cond:
            before = self._state
            self._state_cond.wait_for(lambda: self._state is not before, timeout)
            return self._state if self._state is not before else None

    @property
    def accept_zero_crc(self) -> bool:
        return self.decoder.accept_zero_crc

    @accept_zero_crc.setter
    def accept_zero_crc(self, value: bool) -> None:
        self.decoder.accept_zero_crc = value

    def reset_stats(self) -> None:
        for k in self.stats:
            self.stats[k] = 0

    # -- reader hooks ------------------------------------------------------------

    def _on_open(self) -> None:
        self.decoder.reset()
        self.last_rx = None
        super()._on_open()

    def _on_bytes(self, data: bytes) -> None:
        for ev in self.decoder.feed(data):
            self._emit(self._packet_cbs, ev)
            if not ev.ok:
                self.stats["errors"] += 1
                continue
            self.stats["packets"] += 1
            if ev.zero_crc:
                self.stats["zero_crc"] += 1
            self.last_rx = time.monotonic()
            self._set_link(LinkState.LIVE)
            if ev.type == lt.PING_MSG:
                self.stats["ping"] += 1
            elif ev.type == lt.STATUS_MSG:
                state = lt.parse_status(ev.payload)
                if state is not None:
                    self.stats["status"] += 1
                    self._publish(state)

    def _publish(self, state: lt.LimitimerState) -> None:
        prev = self._state
        with self._state_cond:
            self._state = state
            self._state_cond.notify_all()
        self._emit(self._state_cbs, state)
        if prev is None or dataclasses.replace(prev, sequence=0) != dataclasses.replace(state, sequence=0):
            self._emit(self._change_cbs, state, prev)

    def _on_idle(self) -> None:
        if (self._link == LinkState.LIVE and self.last_rx is not None
                and time.monotonic() - self.last_rx > self.stale_after):
            self._set_link(LinkState.STALE)


class PerfectCue(_SerialDevice):
    """A DSAN Perfect Cue receiver: button events in, commands out.

    >>> cue = PerfectCue("/dev/tty.usbserial-B2")
    >>> cue.on_button(lambda b: print("pressed", b.name))
    >>> cue.start()
    >>> cue.forward()
    """

    def __init__(self, port: Optional[str] = None, *, baudrate: int = pc.BAUDRATE, **kwargs):
        super().__init__(port, baudrate, **kwargs)
        self._button_cbs: List[Callable[[pc.Button], None]] = []
        self.last_button: Optional[pc.Button] = None
        self.last_button_time: Optional[float] = None   # time.time()

    @classmethod
    def simulated(cls, interval: float = 4.0, **kwargs) -> "PerfectCue":
        """A Perfect Cue whose 'presenter' presses a button every `interval` seconds."""
        from .sim import PerfectCueSimulator
        kwargs.setdefault("reconnect", False)
        return cls(transport_factory=lambda: PerfectCueSimulator(interval=interval), **kwargs)

    def on_button(self, cb: Callable[[pc.Button], None]):
        """Called with each Button pressed on the Perfect Cue."""
        self._button_cbs.append(cb)
        return cb

    def _callback_lists(self):
        return super()._callback_lists() + [self._button_cbs]

    def _on_bytes(self, data: bytes) -> None:
        for btn in pc.decode_buttons(data):
            self.last_button = btn
            self.last_button_time = time.time()
            self._emit(self._button_cbs, btn)

    # -- commands --------------------------------------------------------------

    def send(self, cmd: Union[pc.Command, str, bytes]) -> bool:
        """Send a Command, a command name ('forward', 'blank_on', …) or raw bytes."""
        if isinstance(cmd, str):
            cmd = pc.Command.parse(cmd)
        data = cmd.value if isinstance(cmd, pc.Command) else bytes(cmd)
        return self.write(data)

    def forward(self) -> bool:
        return self.send(pc.Command.FORWARD)

    def reverse(self) -> bool:
        return self.send(pc.Command.REVERSE)

    def blank(self, on: bool = True) -> bool:
        return self.send(pc.Command.BLANK_ON if on else pc.Command.BLANK_OFF)


__all__ = ["LinkState", "Limitimer", "PerfectCue"]

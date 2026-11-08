"""A Limitimer in software: four programs, the same buttons, the same status packets.

:class:`LimitimerEmulator` is the timer itself — press keys (:class:`dsan.control.Key`
or their names) and read a :class:`~dsan.limitimer.LimitimerState`, exactly what a
real console reports. Two ways to use it:

* :meth:`LimitimerEmulator.transport` — a fake serial port for
  :class:`dsan.devices.Limitimer`, so everything that reads a Limitimer (the show
  clock, OSC out, the HTTP API) runs from the emulator unchanged::

      emu = LimitimerEmulator()
      lt = Limitimer(transport_factory=emu.transport, reconnect=False).start()
      emu.press("start")

* :class:`LimitimerBus` — sends the emulator's status packets out of an RS-485
  adapter (19200 8N1, 10 status packets a second plus a ping each second, like a
  console), to drive DSAN signal lights and displays without a Limitimer. Only one
  console may talk on a bus: it warns if it hears another one.

How the buttons behave (from DSAN's user guide; where the guide is silent, the
choice is noted):

* **1 / 2 / 3** select a program. Programs 1-3 can't run at the same time: switching
  between them stops the one that was running. **Session** (program 4) can keep
  running in the background while 1-3 are used.
* **Start/Stop** starts or stops the selected program.
* **Time ±** / **Sum-up ±** change the selected program's total or sum-up time by a
  minute, or by a second in **Seconds** mode. While it runs, only if changes are
  permitted (DIP 9).
* **Repeat** puts the selected program back to its full time, stopped, ready to
  start again. *(Choice: the guide doesn't say whether Repeat restarts it.)*
* **Clear** stops the selected program and zeroes its times.
* **Beep** / **Blink** switch those options for the selected program.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import limitimer as lt
from .control import Key, decode_line
from .devices import LinkState, _SerialDevice

MAX_TIME = 99 * 3600 + 59 * 60 + 59          # what four digits in H:M can show


@dataclasses.dataclass
class _Prog:
    total: int = 0
    sumup: int = 0
    elapsed: float = 0.0
    running: bool = False
    beep: bool = False
    blink: bool = False


#: (total, sum-up) seconds a fresh emulator starts with.
DEFAULT_PROGRAMS: Tuple[Tuple[int, int], ...] = ((300, 60), (600, 120), (900, 120), (3600, 300))


class LimitimerEmulator:
    """A thread-safe software Limitimer. Time runs from ``clock`` (``time.monotonic``)."""

    def __init__(self, programs: Sequence[Tuple[int, int]] = DEFAULT_PROGRAMS, *,
                 count_down: bool = True, continue_after_zero: bool = True,
                 permit_changes: bool = True, program_minutes: bool = True,
                 session_minutes: bool = False, beep_type: lt.BeepType = lt.BeepType.CHIME,
                 beep_loud: bool = False, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._progs = [_Prog() for _ in range(4)]
        self.set_programs(programs)
        self.selected = 0
        self.seconds_mode = False
        self.count_down = count_down
        self.continue_after_zero = continue_after_zero
        self.permit_changes = permit_changes
        self.program_minutes = program_minutes
        self.session_minutes = session_minutes
        self.beep_type = beep_type
        self.beep_loud = beep_loud
        self._last = clock()
        self._sequence = 0
        self._press_cbs: List[Callable[[Key], None]] = []

    # -- setup ----------------------------------------------------------------------

    def set_programs(self, programs: Sequence[Tuple[int, int]]) -> None:
        """Set (total, sum-up) seconds for all four programs (stops nothing)."""
        if len(programs) != 4:
            raise ValueError("need four (total, sum-up) pairs")
        with self._lock:
            for p, (total, sumup) in zip(self._progs, programs):
                p.total = _clamp(int(total))
                p.sumup = _clamp(int(sumup))

    def programs(self) -> List[Tuple[int, int]]:
        with self._lock:
            return [(p.total, p.sumup) for p in self._progs]

    def set_program(self, number: int, total: Optional[int] = None, sumup: Optional[int] = None) -> None:
        with self._lock:
            p = self._progs[_index(number)]
            if total is not None:
                p.total = _clamp(int(total))
            if sumup is not None:
                p.sumup = _clamp(int(sumup))

    def on_press(self, cb: Callable[[Key], None]):
        """Called (on the pressing thread) with each key after it took effect."""
        self._press_cbs.append(cb)
        return cb

    # -- time -------------------------------------------------------------------------

    def _advance(self) -> None:
        now = self._clock()
        dt = max(0.0, now - self._last)
        self._last = now
        for p in self._progs:
            if p.running:
                p.elapsed = min(p.elapsed + dt, float(MAX_TIME * 2))

    # -- buttons ----------------------------------------------------------------------

    def press(self, key: Union[Key, str]) -> Key:
        """Press a console button (a :class:`Key` or a name like ``'start'``, ``'p2'``)."""
        k = Key.parse(key)
        with self._lock:
            self._advance()
            sel = self._progs[self.selected]
            if k in (Key.PROGRAM_1, Key.PROGRAM_2, Key.PROGRAM_3, Key.SESSION):
                new = (Key.PROGRAM_1, Key.PROGRAM_2, Key.PROGRAM_3, Key.SESSION).index(k)
                if new != self.selected and new < 3:
                    for i in range(3):              # 1-3 never run together
                        if i != new:
                            self._progs[i].running = False
                self.selected = new
            elif k is Key.START_STOP:
                sel.running = not sel.running
            elif k in (Key.TOTAL_UP, Key.TOTAL_DOWN, Key.SUMUP_UP, Key.SUMUP_DOWN):
                if self.permit_changes or not sel.running:
                    step = 1 if self.seconds_mode else 60
                    step = step if k in (Key.TOTAL_UP, Key.SUMUP_UP) else -step
                    if k in (Key.TOTAL_UP, Key.TOTAL_DOWN):
                        sel.total = _clamp(sel.total + step)
                    else:
                        sel.sumup = _clamp(sel.sumup + step)
            elif k is Key.SECONDS:
                self.seconds_mode = not self.seconds_mode
            elif k is Key.REPEAT:
                sel.running = False
                sel.elapsed = 0.0
            elif k is Key.CLEAR:
                sel.running = False
                sel.elapsed = 0.0
                sel.total = sel.sumup = 0
            elif k is Key.BEEP:
                sel.beep = not sel.beep
            elif k is Key.BLINK:
                sel.blink = not sel.blink
        for cb in list(self._press_cbs):
            try:
                cb(k)
            except Exception:
                pass
        return k

    def handle_ascii(self, data: bytes) -> List[Key]:
        """Accept DSAN ASCII API commands (``b'>STOP 66\\r'``…) as if from a TP-2000X."""
        keys = []
        for line in data.replace(b"\n", b"\r").split(b"\r"):
            msg = decode_line(line)
            if not msg:
                continue
            try:
                keys.append(self.press(msg))
            except ValueError:
                pass
        return keys

    # -- state ------------------------------------------------------------------------

    def state(self) -> lt.LimitimerState:
        """The status a console would send right now."""
        with self._lock:
            self._advance()
            self._sequence = (self._sequence + 1) % 10
            progs = []
            for i, p in enumerate(self._progs):
                flags = ((0x01 if p.running else 0) | (0x02 if p.blink else 0) | (0x04 if p.beep else 0)
                         | (0x08 if self.seconds_mode and i == self.selected else 0))
                progs.append(lt.Program(i + 1, flags, p.total, p.sumup, int(p.elapsed)))
            return lt.LimitimerState(
                programs=tuple(progs), selected_index=self.selected, sequence=self._sequence,
                count_down=self.count_down, continue_after_zero=self.continue_after_zero,
                beep_loud=self.beep_loud, beep_type=self.beep_type, permit_changes=self.permit_changes,
                program_minutes=self.program_minutes, session_minutes=self.session_minutes)

    def packets(self, ping: bool = False) -> bytes:
        """One status packet (and a ping) on the wire."""
        out = lt.encode_status(self.state())
        return out + lt.encode_ping() if ping else out

    # -- settings round trip ------------------------------------------------------------

    def settings(self) -> Dict[str, object]:
        with self._lock:
            return {"programs": [[p.total, p.sumup] for p in self._progs],
                    "beep": [p.beep for p in self._progs], "blink": [p.blink for p in self._progs],
                    "count_down": self.count_down, "continue_after_zero": self.continue_after_zero,
                    "permit_changes": self.permit_changes, "program_minutes": self.program_minutes,
                    "session_minutes": self.session_minutes, "beep_type": self.beep_type.name.lower(),
                    "beep_loud": self.beep_loud}

    def apply_settings(self, s: Dict[str, object]) -> None:
        """Restore from :meth:`settings` (unknown or broken entries are skipped)."""
        with self._lock:
            try:
                self.set_programs([(int(a), int(b)) for a, b in s.get("programs", [])])
            except (TypeError, ValueError):
                pass
            for key in ("beep", "blink"):
                vals = s.get(key)
                if isinstance(vals, list) and len(vals) == 4:
                    for p, v in zip(self._progs, vals):
                        setattr(p, key, bool(v))
            for key in ("count_down", "continue_after_zero", "permit_changes", "program_minutes",
                        "session_minutes", "beep_loud"):
                if key in s:
                    setattr(self, key, bool(s[key]))
            try:
                self.beep_type = lt.BeepType[str(s.get("beep_type", self.beep_type.name)).upper()]
            except KeyError:
                pass

    # -- as a serial port --------------------------------------------------------------

    def transport(self, rate_hz: float = 10.0) -> "EmulatorPort":
        """A fresh pyserial-like port that streams this emulator's packets."""
        return EmulatorPort(self, rate_hz)


class EmulatorPort:
    """pyserial-like: ``read`` yields status packets at ``rate_hz`` (a ping each second);
    ``write`` takes DSAN ASCII API commands, like a TP-2000X would send."""

    def __init__(self, emulator: LimitimerEmulator, rate_hz: float = 10.0):
        self.emulator = emulator
        self.period = 1.0 / rate_hz
        self._buf = bytearray()
        self._next = time.monotonic()
        self._count = 0
        self._closed = False

    def _pump(self) -> None:
        now = time.monotonic()
        if now >= self._next:
            self._count += 1
            self._buf += self.emulator.packets(ping=self._count % max(1, round(1 / self.period)) == 0)
            self._next = max(self._next + self.period, now - self.period)

    @property
    def in_waiting(self) -> int:
        self._pump()
        return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        if self._closed:
            raise OSError("port closed")
        self._pump()
        if not self._buf:
            time.sleep(min(0.02, max(0.0, self._next - time.monotonic())))
            self._pump()
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def write(self, data: bytes) -> int:
        self.emulator.handle_ascii(bytes(data))
        return len(data)

    def close(self) -> None:
        self._closed = True


class LimitimerBus(_SerialDevice):
    """Send an emulator's packets out of an RS-485 adapter, like a Limitimer console.

    ``link`` is OPEN while the adapter is open. ``foreign`` counts valid Limitimer
    packets heard on the bus that we didn't send — another console is talking, and
    two masters on one bus corrupt each other.
    """

    _READ_TIMEOUT = 0.02                      # the loop also paces the sending

    def __init__(self, emulator: LimitimerEmulator, port: Optional[str] = None, *,
                 baudrate: int = lt.BAUDRATE, rate_hz: float = 10.0, **kwargs):
        super().__init__(port, baudrate, **kwargs)
        self.emulator = emulator
        self.period = 1.0 / rate_hz
        self.decoder = lt.Decoder()
        self.sent = 0
        self.foreign = 0
        self.last_foreign: Optional[float] = None
        self._recent: List[bytes] = []
        self._next = 0.0
        self._count = 0

    @property
    def name(self) -> str:
        return "limitimer-bus"

    @property
    def other_console(self) -> bool:
        """Another Limitimer was heard on this bus in the last 3 seconds."""
        return self.last_foreign is not None and time.monotonic() - self.last_foreign < 3.0

    def _on_open(self) -> None:
        self.decoder.reset()
        self._next = time.monotonic()
        super()._on_open()

    def _on_bytes(self, data: bytes) -> None:
        for ev in self.decoder.feed(data):          # our own echo, or another console?
            if ev.ok and ev.raw not in self._recent:
                self.foreign += 1
                self.last_foreign = time.monotonic()

    def _on_idle(self) -> None:
        now = time.monotonic()
        if now < self._next:
            return
        self._next = max(self._next + self.period, now - self.period)
        self._count += 1
        status = lt.encode_status(self.emulator.state())
        out = status
        if self._count % max(1, round(1 / self.period)) == 0:
            out += lt.encode_ping()
        self._recent = (self._recent + [status, lt.encode_ping()])[-12:]
        if self.write(out):
            self.sent += 1


def _clamp(v: int) -> int:
    return max(0, min(MAX_TIME, v))


def _index(number: int) -> int:
    if not 1 <= number <= 4:
        raise ValueError("program number must be 1-4")
    return number - 1


__all__ = ["DEFAULT_PROGRAMS", "EmulatorPort", "LimitimerBus", "LimitimerEmulator", "MAX_TIME"]

"""Hardware-free simulators.

They behave like pyserial ports (``read``, ``write``, ``in_waiting``, ``close``) and
produce real wire bytes, so everything downstream — decoder, link watchdog,
server, OSC — runs exactly as it would with a device attached::

    lt = Limitimer.simulated()          # or Limitimer(transport_factory=LimitimerSimulator)
    cue = PerfectCue.simulated()
"""
from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from . import limitimer as lt
from . import perfectcue as pc

#: (total, sum-up) seconds per program — short, so a full cycle of running,
#: sum-up warning, expiry and overtime plays out in about four minutes.
DEFAULT_PROGRAMS: Tuple[Tuple[int, int], ...] = ((60, 20), (75, 25), (40, 15), (50, 20))


class ShowSequence:
    """Pure show logic: the selected program runs, warns, expires, runs a short
    overtime, then the next one takes over. After P4 everything resets."""

    def __init__(self, programs: Sequence[Tuple[int, int]] = DEFAULT_PROGRAMS,
                 overtime: float = 8.0, count_down: bool = True):
        if len(programs) != 4:
            raise ValueError("need exactly 4 (total, sumup) pairs")
        self.programs = tuple(programs)
        self.overtime = overtime
        self.count_down = count_down
        self.elapsed: List[float] = [0.0] * 4
        self.selected = 0
        self.sequence = 0

    def advance(self, dt: float) -> None:
        total, _ = self.programs[self.selected]
        self.elapsed[self.selected] += dt
        if self.elapsed[self.selected] >= total + self.overtime:
            self.selected = (self.selected + 1) % 4
            if self.selected == 0:
                self.elapsed = [0.0] * 4

    def state(self) -> lt.LimitimerState:
        self.sequence = (self.sequence + 1) % 10
        progs = []
        for i, (total, sumup) in enumerate(self.programs):
            p = lt.Program(i + 1, 0, total, sumup, int(self.elapsed[i]))
            flags = 0
            if i == self.selected:
                flags |= 0x01
                if p.warning:
                    flags |= 0x02     # blink in sum-up
                if p.expired:
                    flags |= 0x04     # beep at/after zero
            progs.append(lt.Program(i + 1, flags, total, sumup, int(self.elapsed[i])))
        return lt.LimitimerState(programs=tuple(progs), selected_index=self.selected,
                                 sequence=self.sequence, count_down=self.count_down,
                                 beep_type=lt.BeepType.CHIME)


class _SimPort:
    """Minimal pyserial-compatible port backed by a time-driven byte generator."""

    timeout = 0.1

    def __init__(self):
        self._buf = bytearray()
        self.is_open = True
        self.written: List[bytes] = []

    # subclasses implement: _next_due (monotonic time) and _produce() -> bytes
    _next_due: float

    def _produce(self) -> bytes:
        raise NotImplementedError

    def _pump(self) -> None:
        now = time.monotonic()
        while now >= self._next_due:
            self._buf += self._produce()

    @property
    def in_waiting(self) -> int:
        self._pump()
        return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise OSError("simulated port closed")
        self._pump()
        if not self._buf:
            time.sleep(max(0.0, min(self.timeout, self._next_due - time.monotonic())))
            self._pump()
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("simulated port closed")
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.is_open = False


class LimitimerSimulator(_SimPort):
    """Emits status packets at ``rate_hz`` (plus a ping each second) for a ShowSequence."""

    def __init__(self, speed: float = 1.0, rate_hz: float = 10.0,
                 show: Optional[ShowSequence] = None):
        super().__init__()
        self.speed = speed
        self.period = 1.0 / rate_hz
        self.show = show or ShowSequence()
        self._next_due = time.monotonic()
        self._count = 0

    def _produce(self) -> bytes:
        self._next_due += self.period
        self.show.advance(self.period * self.speed)
        self._count += 1
        out = lt.encode_status(self.show.state())
        if self._count % max(1, round(1 / self.period)) == 0:
            out += lt.encode_ping()
        return out


class PerfectCueSimulator(_SimPort):
    """A 'presenter' pressing a button every ``interval`` seconds. Commands sent
    to it are recorded in ``written``."""

    PATTERN = (pc.Button.NEXT, pc.Button.NEXT, pc.Button.NEXT, pc.Button.PREV,
               pc.Button.BLANK_ON, pc.Button.BLANK_OFF)

    def __init__(self, interval: float = 4.0):
        super().__init__()
        self.interval = interval
        self._next_due = time.monotonic() + interval
        self._i = 0

    def _produce(self) -> bytes:
        self._next_due += self.interval
        btn = self.PATTERN[self._i % len(self.PATTERN)]
        self._i += 1
        return bytes([btn.value])


__all__ = ["DEFAULT_PROGRAMS", "LimitimerSimulator", "PerfectCueSimulator", "ShowSequence"]

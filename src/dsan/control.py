"""Control a DSAN Limitimer: its buttons, as DSAN's ASCII API defines them.

The Limitimer's RS-485 timer bus (what :class:`dsan.devices.Limitimer` reads) only
*reports* the timer. Pressing its buttons from a computer goes through DSAN's
touch-panel adapter, the **TP-2000X**: RS-232, 9600 8N1 (also reachable over TCP, e.g.
port 5000 behind a serial-to-IP converter, as ``socket://host:5000``).

Messages both ways are ``>COMMAND CK\\r``: ``CK`` is the low byte of the sum of
``COMMAND`` plus the trailing space, in upper-case hex (the same scheme as the
Perfect Cue receiver). Buttons go to the Limitimer::

    PRG1 PRG2 PRG3 SESS   program buttons (SESS = program 4, Session)
    STOP                  Start/Stop
    TTUP TTDN STUP STDN   Total time / Sum-up time up and down
    SSEC                  Set Seconds (time buttons step seconds instead of minutes)
    REPT CLR BEEP BLNK    Repeat, Clear, Beep, Blink

and its lamps and times come back, about four times a second::

    P1LEDON / P1LEDDM / P1LEDOF   (P2, P3, SES: program LEDs — on, dim = in background, off)
    BPLEDON/OF BKLEDON/OF         beep and blink LEDs
    GRNLEDON/OF YELLEDON/OF REDLEDON/OF
    SMON / SMOF                   seconds-mode indicator
    BEEP                          a beep sounded
    TTSTR=MM:SS STSTR=MM:SS RTSTR=MM:SS   total, sum-up and remaining time

Reference: DSAN "Speaker Timer Serial Control Protocol — Limitimer ASCII API".
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Dict, List, Optional, Union

from .devices import LinkState, _SerialDevice
from .perfectcue import command as _command

BAUDRATE = 9600


class Key(Enum):
    """The Limitimer's buttons. Value is the ASCII API command string."""
    PROGRAM_1 = "PRG1"
    PROGRAM_2 = "PRG2"
    PROGRAM_3 = "PRG3"
    SESSION = "SESS"
    START_STOP = "STOP"
    TOTAL_UP = "TTUP"
    TOTAL_DOWN = "TTDN"
    SUMUP_UP = "STUP"
    SUMUP_DOWN = "STDN"
    SECONDS = "SSEC"
    REPEAT = "REPT"
    CLEAR = "CLR"
    BEEP = "BEEP"
    BLINK = "BLNK"

    @property
    def wire(self) -> bytes:
        """Ready-to-send bytes, e.g. ``b'>PRG1 3A\\r'``."""
        return _command(self.value)

    @property
    def label(self) -> str:
        return _LABELS[self]

    @classmethod
    def program(cls, number: int) -> "Key":
        return (cls.PROGRAM_1, cls.PROGRAM_2, cls.PROGRAM_3, cls.SESSION)[number - 1]

    @classmethod
    def parse(cls, name: Union[str, "Key"]) -> "Key":
        """Accept button names in many spellings: 'start', 'startstop', 'p1', 'program2',
        'session', 'p4', 'time+', 'total_up', 'sumup-', 'sec', 'repeat', 'PRG1'…"""
        if isinstance(name, Key):
            return name
        raw = str(name).strip()
        k = raw.lower().replace("-", "_").replace(" ", "_").replace("/", "_")
        if k in _ALIASES:
            return _ALIASES[k]
        for key in cls:
            if raw.upper() in (key.value, key.name):
                return key
        raise ValueError(f"unknown Limitimer button {name!r}; expected one of "
                         f"{sorted(set(_ALIASES))}")


_LABELS = {
    Key.PROGRAM_1: "1", Key.PROGRAM_2: "2", Key.PROGRAM_3: "3", Key.SESSION: "Session",
    Key.START_STOP: "Start / Stop", Key.TOTAL_UP: "Time +", Key.TOTAL_DOWN: "Time −",
    Key.SUMUP_UP: "Sum-up +", Key.SUMUP_DOWN: "Sum-up −", Key.SECONDS: "Seconds",
    Key.REPEAT: "Repeat", Key.CLEAR: "Clear", Key.BEEP: "Beep", Key.BLINK: "Blink",
}

_ALIASES: Dict[str, Key] = {}
for _k, _names in {
    Key.PROGRAM_1: "p1 program1 program_1 prg1 1",
    Key.PROGRAM_2: "p2 program2 program_2 prg2 2",
    Key.PROGRAM_3: "p3 program3 program_3 prg3 3",
    Key.SESSION: "p4 program4 program_4 session sess 4",
    Key.START_STOP: "start stop start_stop startstop go toggle",
    Key.TOTAL_UP: "total_up totalup time_up timeup time+ total+ ttup",
    Key.TOTAL_DOWN: "total_down totaldown time_down timedown time_ total_ ttdn time-minus",
    Key.SUMUP_UP: "sumup_up sum_up_up sumupup sumup+ sum+ stup",
    Key.SUMUP_DOWN: "sumup_down sum_up_down sumupdown sumup_ sum_ stdn",
    Key.SECONDS: "seconds sec set_seconds setseconds ssec",
    Key.REPEAT: "repeat rept again",
    Key.CLEAR: "clear clr reset",
    Key.BEEP: "beep",
    Key.BLINK: "blink blnk",
}.items():
    for _n in _names.split():
        _ALIASES[_n] = _k


# -- feedback --------------------------------------------------------------------

LED_OFF, LED_DIM, LED_ON = "off", "dim", "on"


@dataclass(frozen=True)
class Feedback:
    """What the Limitimer's console shows, as reported by the TP-2000X."""
    programs: tuple = (LED_OFF, LED_OFF, LED_OFF, LED_OFF)   # P1-P3, Session: on / dim / off
    beep: bool = False
    blink: bool = False
    green: bool = False
    yellow: bool = False
    red: bool = False
    seconds: bool = False
    total: str = ""                  # "MM:SS" as the console shows it
    sumup: str = ""
    remaining: str = ""

    @property
    def selected(self) -> Optional[int]:
        """1-4: the brightly lit program, if any."""
        for i, s in enumerate(self.programs):
            if s == LED_ON:
                return i + 1
        return None

    def to_dict(self) -> Dict[str, object]:
        return {"programs": list(self.programs), "selected": self.selected, "beep": self.beep,
                "blink": self.blink, "green": self.green, "yellow": self.yellow, "red": self.red,
                "seconds": self.seconds, "total": self.total, "sumup": self.sumup,
                "remaining": self.remaining}


def decode_line(line: Union[bytes, str]) -> Optional[str]:
    """``>P1LEDON 5A`` → ``'P1LEDON'``. Messages without a checksum are accepted too;
    a checksum that doesn't match returns None."""
    if isinstance(line, bytes):
        line = line.decode("ascii", "replace")
    text = line.strip().lstrip(">").strip()
    if not text:
        return None
    body, sep, ck = text.rpartition(" ")
    if not sep:
        return text
    if len(ck) == 2 and all(c in "0123456789ABCDEFabcdef" for c in ck):
        if sum(f"{body} ".encode("ascii", "replace")) & 0xFF != int(ck, 16):
            return None
        return body
    return text


_PROGRAM_LEDS = {"P1": 0, "P2": 1, "P3": 2, "SES": 3}
_LED_LEVEL = {"ON": LED_ON, "DM": LED_DIM, "OF": LED_OFF}
_LAMPS = {"BP": "beep", "BK": "blink", "GRN": "green", "YEL": "yellow", "RED": "red"}


def apply_message(fb: Feedback, msg: str) -> Feedback:
    """Fold one decoded message into the feedback. Unknown messages change nothing."""
    m = msg.strip().upper()
    if m.startswith("TTSTR="):
        return replace(fb, total=m[6:])
    if m.startswith("STSTR="):
        return replace(fb, sumup=m[6:])
    if m.startswith("RTSTRSZ="):
        return fb
    if m.startswith("RTSTR="):
        return replace(fb, remaining=m[6:])
    if m in ("SMON", "SMOF"):
        return replace(fb, seconds=m == "SMON")
    if "LED" in m:
        name, _, level = m.partition("LED")
        if name in _PROGRAM_LEDS and level in _LED_LEVEL:
            progs = list(fb.programs)
            progs[_PROGRAM_LEDS[name]] = _LED_LEVEL[level]
            return replace(fb, programs=tuple(progs))
        if name in _LAMPS and level in ("ON", "OF"):
            return replace(fb, **{_LAMPS[name]: level == "ON"})
    return fb


class LimitimerControl(_SerialDevice):
    """A Limitimer's buttons and lamps through a DSAN TP-2000X (RS-232, 9600 8N1).

    >>> ctl = LimitimerControl("/dev/tty.usbserial-B2").start()
    >>> ctl.press("start")              # or Key.START_STOP, "p2", "time+"…
    >>> ctl.feedback.remaining
    '04:59'
    """

    _OPEN_STATE = LinkState.WAITING

    def __init__(self, port: Optional[str] = None, *, baudrate: int = BAUDRATE,
                 stale_after: float = 3.0, **kwargs):
        super().__init__(port, baudrate, **kwargs)
        self.stale_after = stale_after
        self._buf = bytearray()
        self._feedback = Feedback()
        self._lock = threading.Lock()
        self._feedback_cbs: List[Callable[[Feedback], None]] = []
        self._message_cbs: List[Callable[[str], None]] = []
        self._beep_cbs: List[Callable[[], None]] = []
        self.last_rx: Optional[float] = None
        self.sent: List[Key] = []                 # for the debug console / tests (last 50)

    @property
    def name(self) -> str:
        return "limitimer-control"

    @property
    def feedback(self) -> Feedback:
        return self._feedback

    def on_feedback(self, cb: Callable[[Feedback], None]):
        """Called with the new :class:`Feedback` whenever a lamp or time changes."""
        self._feedback_cbs.append(cb)
        return cb

    def on_message(self, cb: Callable[[str], None]):
        """Called with every decoded message (``'P1LEDON'``, ``'RTSTR=04:59'``…)."""
        self._message_cbs.append(cb)
        return cb

    def on_beep(self, cb: Callable[[], None]):
        self._beep_cbs.append(cb)
        return cb

    def _callback_lists(self):
        return super()._callback_lists() + [self._feedback_cbs, self._message_cbs, self._beep_cbs]

    def press(self, key: Union[Key, str]) -> Key:
        """Press a button. Raises ValueError for an unknown name; returns the Key."""
        k = Key.parse(key)
        self.write(k.wire)
        self.sent = (self.sent + [k])[-50:]
        return k

    def program(self, number: int) -> Key:
        return self.press(Key.program(number))

    # -- reader hooks -------------------------------------------------------------

    def _on_open(self) -> None:
        self._buf.clear()
        self.last_rx = None
        super()._on_open()

    def _on_bytes(self, data: bytes) -> None:
        self._buf += data
        while True:
            i = min((j for j in (self._buf.find(b"\r"), self._buf.find(b"\n")) if j >= 0), default=-1)
            if i < 0:
                if len(self._buf) > 512:          # garbage without line ends: drop it
                    self._buf.clear()
                return
            line = bytes(self._buf[:i])
            del self._buf[:i + 1]
            msg = decode_line(line)
            if msg is None:
                continue
            self.last_rx = time.monotonic()
            self._set_link(LinkState.LIVE)
            self._emit(self._message_cbs, msg)
            if msg.upper() == "BEEP":
                self._emit(self._beep_cbs)
                continue
            with self._lock:
                new = apply_message(self._feedback, msg)
                changed = new != self._feedback
                self._feedback = new
            if changed:
                self._emit(self._feedback_cbs, new)

    def _on_idle(self) -> None:
        if (self._link == LinkState.LIVE and self.last_rx is not None
                and time.monotonic() - self.last_rx > self.stale_after):
            self._set_link(LinkState.STALE)


__all__ = ["BAUDRATE", "Feedback", "Key", "LED_DIM", "LED_OFF", "LED_ON", "LimitimerControl",
           "apply_message", "decode_line"]

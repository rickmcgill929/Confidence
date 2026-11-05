"""DSAN Perfect Cue protocol: button events in, LED feedback and ASCII commands out.
Pure Python, no I/O.

The data link is 19200-8-N-1. Button presses arrive as single bytes::

    0x0F NEXT (green ▷)   0x1F PREV (red ◁)   0x2F BLANK, LED off   0x3F BLANK, LED on

Any other byte is ignored.

Sending to a Perfect Cue in master mode lights its LEDs (see :class:`Led`)::

    Enter          green right arrow
    any arrow key  red left arrow
    R T Y U        blink yellow, then leave it on
    E A D          blink yellow, then leave it off
    anything else  blink yellow for a while (error report)

The receiver's separate ASCII command interface (RS-232, 9600-8-N-1)::

    '>' COMMAND ' ' CHK_HI CHK_LO '\\r'

The checksum is the low byte of the sum of COMMAND plus the trailing space — the
leading '>' is *not* included — written as two uppercase hex ASCII digits.
"""
from __future__ import annotations

from enum import Enum, IntEnum
from typing import Iterable, List

BAUDRATE = 19200            # Perfect Cue data link: button bytes in, LED bytes out
RECEIVER_BAUDRATE = 9600    # the receiver's RS-232 ASCII command interface (Command)


class Button(IntEnum):
    NEXT = 0x0F
    PREV = 0x1F
    BLANK_OFF = 0x2F
    BLANK_ON = 0x3F


_BUTTON_BYTES = frozenset(b.value for b in Button)


def decode_buttons(data: Iterable[int]) -> List[Button]:
    """Extract button presses from a chunk of received bytes."""
    return [Button(b) for b in data if b in _BUTTON_BYTES]


class Led(Enum):
    """Bytes that light a Perfect Cue's LEDs in master mode. Value is the byte string to send."""
    NEXT = b"\r"           # Enter → green right arrow
    PREV = b"\x1b[D"       # an arrow key (left, as a terminal sends it) → red left arrow
    BLANK_ON = b"R"        # blink yellow, leave on   (T, Y, U do the same)
    BLANK_OFF = b"E"       # blink yellow, leave off  (A, D do the same)
    ERROR = b"?"           # any other byte → yellow blinks for a while

    @classmethod
    def for_button(cls, button: "Button") -> "Led":
        """The LED feedback that mirrors a button press (e.g. echo a press back to the presenter)."""
        return {Button.NEXT: cls.NEXT, Button.PREV: cls.PREV,
                Button.BLANK_ON: cls.BLANK_ON, Button.BLANK_OFF: cls.BLANK_OFF}[button]

    @classmethod
    def parse(cls, name: str) -> "Led":
        """Accept 'next', 'prev', 'previous', 'blank_on', 'blank-off', 'error' (case-insensitive)."""
        key = name.strip().upper().replace("-", "_")
        key = {"PREVIOUS": "PREV", "FORWARD": "NEXT", "REVERSE": "PREV",
               "BLANKON": "BLANK_ON", "BLANKOFF": "BLANK_OFF"}.get(key, key)
        try:
            return cls[key]
        except KeyError:
            raise ValueError(f"unknown Perfect Cue LED {name!r}; "
                             f"expected one of {[c.name.lower() for c in cls]}") from None


def command(text: str) -> bytes:
    """Build a receiver command with its checksum, e.g. ``command("FORWARD")`` → ``b'>FORWARD 35\\r'``."""
    if not text or not text.isascii() or any(c in text for c in "> \r\n"):
        raise ValueError("command must be non-empty ASCII without '>', spaces or line breaks")
    body = f"{text} ".encode("ascii")
    return b">" + body + f"{sum(body) & 0xFF:02X}".encode("ascii") + b"\r"


class Command(Enum):
    """The receiver commands the viewer uses. Value is the ready-to-send byte string."""
    FORWARD = command("FORWARD")     # >FORWARD 35\r
    REVERSE = command("REVERSE")     # >REVERSE 3C\r
    BLANK_ON = command("BLANKON")    # >BLANKON 25\r
    BLANK_OFF = command("BLANKOF")   # >BLANKOF 1D\r

    @classmethod
    def parse(cls, name: str) -> "Command":
        """Accept 'forward', 'next', 'reverse', 'prev', 'blank_on', 'blank-off'… (case-insensitive)."""
        key = name.strip().upper().replace("-", "_")
        key = {"NEXT": "FORWARD", "PREV": "REVERSE", "PREVIOUS": "REVERSE",
               "BLANKON": "BLANK_ON", "BLANKOFF": "BLANK_OFF"}.get(key, key)
        try:
            return cls[key]
        except KeyError:
            raise ValueError(f"unknown Perfect Cue command {name!r}; "
                             f"expected one of {[c.name.lower() for c in cls]}") from None


__all__ = ["BAUDRATE", "RECEIVER_BAUDRATE", "Button", "Command", "Led", "command", "decode_buttons"]

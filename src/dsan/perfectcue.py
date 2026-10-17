"""DSAN Perfect Cue protocol: button events in, ASCII commands out. Pure Python, no I/O.

Button presses arrive as single bytes (matches the Go reference)::

    0x0F NEXT (green ▷)   0x1F PREV (red ◁)   0x2F BLANK, LED off   0x3F BLANK, LED on

Any other byte is ignored.

The receiver's ASCII command interface (RS-232, 9600-8-N-1)::

    '>' COMMAND ' ' CHK_HI CHK_LO '\\r'

The checksum is the low byte of the sum of COMMAND plus the trailing space — the
leading '>' is *not* included — written as two uppercase hex ASCII digits.
"""
from __future__ import annotations

from enum import Enum, IntEnum
from typing import Iterable, List

BAUDRATE = 9600


class Button(IntEnum):
    NEXT = 0x0F
    PREV = 0x1F
    BLANK_OFF = 0x2F
    BLANK_ON = 0x3F


_BUTTON_BYTES = frozenset(b.value for b in Button)


def decode_buttons(data: Iterable[int]) -> List[Button]:
    """Extract button presses from a chunk of received bytes."""
    return [Button(b) for b in data if b in _BUTTON_BYTES]


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


__all__ = ["BAUDRATE", "Button", "Command", "command", "decode_buttons"]

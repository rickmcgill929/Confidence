"""DSAN Limitimer RS-485 protocol: data model, stream decoder, parser and encoder.

Pure Python, no I/O — safe to use from any thread or event loop.

Physical layer: RS-485, 19200 bps, 8N1.

Wire format::

    0x81 | payload (7-bit bytes) | 0x83 | CRC hi | CRC lo | 0xFF

The CRC is CRC16-MODBUS over ``0x81 … 0x83`` inclusive, big-endian, and is *not*
7-bit encoded, so it may contain 0x81/0x83/0xFF. The decoder is a state machine
that counts exactly two CRC bytes for that reason.

Status payload (type 0x00, 50 bytes)::

    0      type 0x00
    1      cfg_hi   bit5 permit_changes, bit0 beep MSB (DIP 8)
    2      cfg_lo   bit6 beep LSB (DIP 7), bit5 beep_loud (6), bit4 continue (5),
                    bit3 countdown (4), bit2 program_minutes (3), bit1 session_minutes (2)
    3      sequence counter 0-9
    4      selected program 0-3
    5-48   4 × 11-byte program blocks
    49     reserved 0x00

Program block::

    +0     flags   bit0 running, bit1 blink, bit2 beep, bit3 sec_adjust
    +1     reserved
    +2..4  total    (b0·16384 + b1·128 + b2) seconds
    +5..7  sum-up
    +8..10 elapsed

Protocol reference: https://gitlab.com/Depili/limitimer
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple

START_BYTE = 0x81
CHECKSUM_START = 0x83
END_BYTE = 0xFF

STATUS_MSG = 0x00
PING_MSG = 0x10

BAUDRATE = 19200
STATUS_PACKET_LEN = 55


class BeepType(IntEnum):
    """DIP 7+8 sound selection (matches the Go reference)."""
    NONE = 0
    BUZZ = 1
    RING = 2
    CHIME = 3


@dataclass(frozen=True)
class Program:
    """One of the four Limitimer programs. All times are in seconds."""
    number: int            # 1-4
    flags: int = 0
    total: int = 0
    sumup: int = 0
    elapsed: int = 0

    @property
    def running(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def blink(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def beep(self) -> bool:
        return bool(self.flags & 0x04)

    @property
    def sec_adjust(self) -> bool:
        return bool(self.flags & 0x08)

    @property
    def remaining(self) -> int:
        """Signed seconds left; negative once past zero."""
        return self.total - self.elapsed

    @property
    def expired(self) -> bool:
        """Reached or passed zero (Go: Elapsed >= Total)."""
        return self.total > 0 and self.elapsed >= self.total

    @property
    def overtime(self) -> bool:
        """Strictly past zero."""
        return self.total > 0 and self.remaining < 0

    @property
    def warning(self) -> bool:
        """Inside the sum-up period and not yet expired (Go: Total-Elapsed <= Sumup && !Expired)."""
        return self.sumup > 0 and self.remaining <= self.sumup and not self.expired

    @property
    def color(self) -> int:
        """0 normal, 1 warning, 2 expired — handy for driving tallies/LEDs."""
        return 2 if self.expired else (1 if self.warning else 0)


@dataclass(frozen=True)
class LimitimerState:
    """A decoded status message. Immutable; compare with ``==`` to detect changes."""
    programs: Tuple[Program, Program, Program, Program] = field(
        default_factory=lambda: tuple(Program(i + 1) for i in range(4)))
    selected_index: int = 0          # 0-3
    sequence: int = 0
    count_down: bool = True          # DIP 4
    continue_after_zero: bool = False  # DIP 5
    beep_loud: bool = False          # DIP 6
    beep_type: BeepType = BeepType.NONE  # DIP 7+8
    permit_changes: bool = True      # DIP 9
    program_minutes: bool = False    # DIP 3: P1-3 MM:SS (else HH:MM)
    session_minutes: bool = False    # DIP 2: P4 MM:SS (else HH:MM)

    # -- convenience -------------------------------------------------------

    @property
    def selected(self) -> Program:
        return self.programs[self.selected_index]

    def program(self, number: int) -> Program:
        """Program by its 1-based number."""
        if not 1 <= number <= 4:
            raise ValueError("program number must be 1-4")
        return self.programs[number - 1]

    def uses_minutes(self, number: int) -> bool:
        """True if the device shows this program as MM:SS (DIP 2 for P4, DIP 3 for P1-3)."""
        return self.session_minutes if number == 4 else self.program_minutes

    def display_seconds(self, number: int, overtime: bool = True) -> int:
        """Unsigned seconds the display shows.

        Countdown shows total - elapsed; past zero it shows the overtime amount,
        or 0 if ``overtime`` is False. Count-up shows elapsed; past total it keeps
        counting, or holds at total if ``overtime`` is False.
        """
        p = self.program(number)
        if self.count_down:
            secs = p.remaining
            if secs < 0:
                secs = -secs if overtime else 0
        else:
            secs = p.elapsed
            if not overtime and p.expired:
                secs = p.total
        return max(0, int(secs))

    def display(self, number: int, overtime: bool = True, follow_device_format: bool = False) -> str:
        """The string a display should show, with ``+`` when a countdown is past zero.

        ``follow_device_format=True`` honours DIP 2/3 (MM:SS, minutes may exceed 59,
        or HH:MM). Otherwise MM:SS under an hour and HH:MM:SS above.
        """
        secs = self.display_seconds(number, overtime)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if follow_device_format:
            text = f"{secs // 60:02d}:{s:02d}" if self.uses_minutes(number) else f"{h:02d}:{m:02d}"
        else:
            text = f"{m:02d}:{s:02d}" if h == 0 else f"{h:02d}:{m:02d}:{s:02d}"
        if self.count_down and overtime and self.program(number).overtime:
            text = "+" + text
        return text

    def to_dict(self, overtime: bool = True, follow_device_format: bool = False) -> Dict[str, Any]:
        """JSON-ready snapshot (what the HTTP/WebSocket API returns)."""
        return {
            "sequence": self.sequence,
            "selected": self.selected_index + 1,
            "count_down": self.count_down,
            "settings": {
                "continue_after_zero": self.continue_after_zero,
                "beep_loud": self.beep_loud,
                "beep_type": self.beep_type.name.lower(),
                "permit_changes": self.permit_changes,
                "program_minutes": self.program_minutes,
                "session_minutes": self.session_minutes,
            },
            "programs": [
                {
                    "number": p.number,
                    "display": self.display(p.number, overtime, follow_device_format),
                    "display_seconds": self.display_seconds(p.number, overtime),
                    "total": p.total,
                    "sumup": p.sumup,
                    "elapsed": p.elapsed,
                    "remaining": p.remaining,
                    "running": p.running,
                    "blink": p.blink,
                    "beep": p.beep,
                    "warning": p.warning,
                    "expired": p.expired,
                    "overtime": p.overtime,
                    "color": p.color,
                    "flags": p.flags,
                }
                for p in self.programs
            ],
        }


# -- CRC / framing -----------------------------------------------------------

def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(payload: bytes) -> bytes:
    """Wrap a 7-bit payload into a complete wire packet with CRC."""
    if any(b & 0x80 for b in payload):
        raise ValueError("payload bytes must be 7-bit")
    head = bytes([START_BYTE]) + payload + bytes([CHECKSUM_START])
    crc = crc16_modbus(head)
    return head + bytes([crc >> 8, crc & 0xFF, END_BYTE])


@dataclass(frozen=True)
class PacketEvent:
    """Every framed packet the decoder sees — for debug consoles and stats."""
    raw: bytes
    payload: Optional[bytes]   # None if invalid
    error: Optional[str]       # None if valid
    zero_crc: bool = False     # accepted with a 0x0000 checksum

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def type(self) -> int:
        return self.payload[0] if self.payload else -1


class Decoder:
    """Incremental byte-stream decoder. ``feed()`` bytes in any chunk size."""

    _SEEKING, _IN_PAYLOAD, _IN_CRC, _AWAIT_END = range(4)

    def __init__(self, accept_zero_crc: bool = True):
        #: The Go reference treats a 0x0000 checksum as "CRC disabled" and accepts it.
        self.accept_zero_crc = accept_zero_crc
        self._state = self._SEEKING
        self._buf = bytearray()
        self._crc_count = 0

    def reset(self) -> None:
        self._state = self._SEEKING
        self._buf = bytearray()
        self._crc_count = 0

    def feed(self, data: bytes) -> List[PacketEvent]:
        """Return one PacketEvent per completed (valid or invalid) frame."""
        events: List[PacketEvent] = []
        for byte in data:
            st = self._state
            if st == self._SEEKING:
                if byte == START_BYTE:
                    self._buf = bytearray([byte])
                    self._state = self._IN_PAYLOAD
            elif st == self._IN_PAYLOAD:
                self._buf.append(byte)
                if byte == CHECKSUM_START:
                    self._state = self._IN_CRC
                    self._crc_count = 0
                elif byte == START_BYTE:
                    self._buf = bytearray([byte])
                elif byte & 0x80:
                    events.append(PacketEvent(bytes(self._buf), None,
                                              f"unexpected byte 0x{byte:02X} in payload"))
                    self.reset()
            elif st == self._IN_CRC:
                self._buf.append(byte)
                self._crc_count += 1
                if self._crc_count == 2:
                    self._state = self._AWAIT_END
            else:  # _AWAIT_END
                self._buf.append(byte)
                if byte == END_BYTE:
                    events.append(self._validate(bytes(self._buf)))
                else:
                    events.append(PacketEvent(bytes(self._buf), None,
                                              f"expected end byte 0xFF, got 0x{byte:02X}"))
                self.reset()
        return events

    def _validate(self, packet: bytes) -> PacketEvent:
        cs = len(packet) - 4          # index of 0x83
        if cs < 1:
            return PacketEvent(packet, None, f"packet too short ({len(packet)} bytes)")
        expected = crc16_modbus(packet[:cs + 1])
        received = (packet[cs + 1] << 8) | packet[cs + 2]
        zero = False
        if received != expected:
            if received == 0 and self.accept_zero_crc:
                zero = True
            elif received == 0:
                return PacketEvent(packet, None, "zero CRC rejected")
            else:
                return PacketEvent(packet, None,
                                   f"CRC mismatch: expected 0x{expected:04X}, got 0x{received:04X}")
        return PacketEvent(packet, packet[1:cs], None, zero)


# -- status message ------------------------------------------------------------

def _t7(d: bytes, o: int) -> int:
    return d[o] * 16384 + d[o + 1] * 128 + d[o + 2] if o + 2 < len(d) else 0


def parse_status(payload: bytes) -> Optional[LimitimerState]:
    """Parse a status payload (as in ``PacketEvent.payload``). None if not a status message."""
    if len(payload) < 5 or payload[0] != STATUS_MSG:
        return None
    cfg_hi, cfg_lo = payload[1], payload[2]
    programs = []
    for i in range(4):
        o = 5 + i * 11
        programs.append(Program(
            number=i + 1,
            flags=payload[o] if o < len(payload) else 0,
            total=_t7(payload, o + 2),
            sumup=_t7(payload, o + 5),
            elapsed=_t7(payload, o + 8),
        ))
    return LimitimerState(
        programs=tuple(programs),
        selected_index=payload[4] & 0x03,
        sequence=payload[3],
        count_down=bool(cfg_lo & 0x08),
        continue_after_zero=bool(cfg_lo & 0x10),
        beep_loud=bool(cfg_lo & 0x20),
        beep_type=BeepType(((cfg_lo & 0x40) >> 6) | ((cfg_hi & 0x01) << 1)),
        permit_changes=bool(cfg_hi & 0x20),
        program_minutes=bool(cfg_lo & 0x04),
        session_minutes=bool(cfg_lo & 0x02),
    )


def _mmss(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def annotate_status(payload: bytes) -> List[Tuple[str, str, str, str]]:
    """Byte-by-byte breakdown of a status payload for debugging.

    Returns rows of (offset, hex, field, meaning), e.g.
    ``("07-09", "00 02 2C", "P1 total", "300 s (5:00)")``.
    """
    rows: List[Tuple[str, str, str, str]] = []
    d = payload

    def hx(a: int, b: int) -> str:
        return " ".join(f"{x:02X}" for x in d[a:b])

    def on(bit: bool) -> str:
        return "on" if bit else "off"

    if not d:
        return rows
    rows.append(("00", hx(0, 1), "type", {STATUS_MSG: "status", PING_MSG: "ping"}.get(d[0], f"unknown 0x{d[0]:02X}")))
    if d[0] != STATUS_MSG or len(d) < 5:
        return rows
    hi, lo = d[1], d[2]
    beep = BeepType(((lo & 0x40) >> 6) | ((hi & 0x01) << 1)).name.title()
    rows.append(("01", hx(1, 2), "config high",
                 f"permit changes {on(hi & 0x20)} (DIP 9) · beep bit 1 {on(hi & 1)} (DIP 8)"))
    rows.append(("02", hx(2, 3), "config low",
                 f"beep bit 0 {on(lo & 0x40)} (DIP 7) → {beep} · loud {on(lo & 0x20)} (DIP 6) · "
                 f"continue {on(lo & 0x10)} (DIP 5) · countdown {on(lo & 0x08)} (DIP 4) · "
                 f"P1-3 MM:SS {on(lo & 0x04)} (DIP 3) · P4 MM:SS {on(lo & 0x02)} (DIP 2)"
                 + (f" · unknown bits 0x{lo & 0x81:02X}" if lo & 0x81 else "")))
    rows.append(("03", hx(3, 4), "sequence", str(d[3])))
    rows.append(("04", hx(4, 5), "selected", f"program {(d[4] & 3) + 1}"))
    for i in range(4):
        o = 5 + i * 11
        if o + 10 >= len(d):
            break
        f = d[o]
        flags = [n for bit, n in ((1, "running"), (2, "blink"), (4, "beep"), (8, "sec-adjust")) if f & bit]
        if f & 0xF0:
            flags.append(f"unknown 0x{f & 0xF0:02X}")
        p = f"P{i + 1}"
        rows.append((f"{o:02d}", hx(o, o + 1), f"{p} flags", ", ".join(flags) or "—"))
        rows.append((f"{o + 1:02d}", hx(o + 1, o + 2), f"{p} reserved", ""))
        for name, off in (("total", 2), ("sum-up", 5), ("elapsed", 8)):
            v = _t7(d, o + off)
            rows.append((f"{o + off:02d}-{o + off + 2:02d}", hx(o + off, o + off + 3), f"{p} {name}",
                         f"{v} s ({_mmss(v)})"))
    if len(d) > 49:
        rows.append(("49", hx(49, 50), "reserved", ""))
    if len(d) > 50:
        rows.append((f"50-{len(d) - 1}", hx(50, len(d)), "extra", f"{len(d) - 50} unexpected byte(s)"))
    return rows


def _e7(v: int) -> List[int]:
    if not 0 <= v < 1 << 21:
        raise ValueError("time out of range")
    return [(v >> 14) & 0x7F, (v >> 7) & 0x7F, v & 0x7F]


def encode_status(state: LimitimerState) -> bytes:
    """Encode a state into a complete 55-byte wire packet (simulators, tests)."""
    cfg_hi = (0x20 if state.permit_changes else 0) | ((state.beep_type >> 1) & 1)
    cfg_lo = ((state.beep_type & 1) << 6
              | (0x20 if state.beep_loud else 0)
              | (0x10 if state.continue_after_zero else 0)
              | (0x08 if state.count_down else 0)
              | (0x04 if state.program_minutes else 0)
              | (0x02 if state.session_minutes else 0))
    body = [STATUS_MSG, cfg_hi, cfg_lo, state.sequence % 10, state.selected_index & 3]
    for p in state.programs:
        body += [p.flags & 0x7F, 0] + _e7(p.total) + _e7(p.sumup) + _e7(p.elapsed)
    body.append(0)
    return frame(bytes(body))


def encode_ping() -> bytes:
    return frame(bytes([PING_MSG]))


__all__ = [
    "BAUDRATE", "BeepType", "Decoder", "LimitimerState", "PacketEvent", "Program",
    "PING_MSG", "STATUS_MSG", "crc16_modbus", "encode_ping", "encode_status", "frame",
    "annotate_status", "parse_status",
]

"""DSAN Limitimer ASCII API (TP-2000X): commands, feedback, and the serial client."""
import time

import pytest

from dsan.control import Feedback, Key, LimitimerControl, apply_message, decode_line
from dsan.devices import LinkState

# DSAN's table: "Full Message" column
TABLE = {"PRG1": "3A", "PRG2": "3B", "PRG3": "3C", "SESS": "5E", "TTUP": "6D", "TTDN": "5A",
         "STUP": "6C", "STDN": "59", "SSEC": "4E", "CLR": "01", "BEEP": "3C", "BLNK": "47",
         "REPT": "5B", "STOP": "66"}


def test_commands_match_dsan_table():
    assert {k.value for k in Key} == set(TABLE)
    for k in Key:
        assert k.wire == f">{k.value} {TABLE[k.value]}\r".encode()


@pytest.mark.parametrize("name,key", [
    ("start", Key.START_STOP), ("Start/Stop", Key.START_STOP), ("p1", Key.PROGRAM_1),
    ("program2", Key.PROGRAM_2), ("3", Key.PROGRAM_3), ("session", Key.SESSION), ("p4", Key.SESSION),
    ("time+", Key.TOTAL_UP), ("time-", Key.TOTAL_DOWN), ("total_down", Key.TOTAL_DOWN),
    ("sumup+", Key.SUMUP_UP), ("sumup-", Key.SUMUP_DOWN), ("Sum-up down", Key.SUMUP_DOWN),
    ("sec", Key.SECONDS), ("repeat", Key.REPEAT), ("clear", Key.CLEAR), ("BLNK", Key.BLINK),
    ("SESSION", Key.SESSION), (Key.BEEP, Key.BEEP)])
def test_key_parse(name, key):
    assert Key.parse(name) is key


def test_key_parse_rejects():
    with pytest.raises(ValueError):
        Key.parse("launch")


def test_decode_line():
    assert decode_line(b">P1LEDON 3A") is None                   # wrong checksum
    good = Key.PROGRAM_1.wire.strip()
    assert decode_line(good) == "PRG1"
    assert decode_line("RTSTR=04:59") == "RTSTR=04:59"           # no checksum: accepted
    assert decode_line(b"  \r") is None


def test_feedback():
    fb = Feedback()
    for m in ("P2LEDON", "SESLEDDM", "GRNLEDON", "BKLEDON", "SMON", "TTSTR=05:00", "STSTR=01:00",
              "RTSTR=04:59", "YELLEDOF", "NONSENSE", "P9LEDON"):
        fb = apply_message(fb, m)
    assert fb.programs == ("off", "on", "off", "dim") and fb.selected == 2
    assert fb.green and fb.blink and fb.seconds and not fb.yellow
    assert (fb.total, fb.sumup, fb.remaining) == ("05:00", "01:00", "04:59")
    assert apply_message(fb, "P2LEDOF").selected is None


class FakeTP:
    """A TP-2000X on a fake serial port: records writes, lets the test feed replies."""
    def __init__(self):
        self.written = bytearray()
        self.inbox = bytearray()

    @property
    def in_waiting(self):
        return len(self.inbox)

    def read(self, n=1):
        if not self.inbox:
            time.sleep(0.01)
            return b""
        out = bytes(self.inbox[:n])
        del self.inbox[:n]
        return out

    def write(self, data):
        self.written += data
        return len(data)

    def close(self):
        pass


def _wait(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def msg(text):
    body = f"{text} ".encode()
    return b">" + body + f"{sum(body) & 0xFF:02X}".encode() + b"\r"


def test_client_press_and_feedback():
    port = FakeTP()
    ctl = LimitimerControl(transport_factory=lambda: port, reconnect=False, stale_after=0.3)
    seen, beeps = [], []
    ctl.on_feedback(seen.append)
    ctl.on_beep(lambda: beeps.append(1))
    ctl.start()
    try:
        assert _wait(lambda: ctl.link == LinkState.WAITING)
        assert ctl.press("start") is Key.START_STOP and ctl.program(4) is Key.SESSION
        assert bytes(port.written) == b">STOP 66\r>SESS 5E\r"
        port.inbox += msg("P1LEDON") + b"RTSTR=04:59\r" + msg("BEEP") + b">GRNLEDON 00\r"   # last one: bad CK
        assert _wait(lambda: ctl.feedback.remaining == "04:59")
        assert ctl.link == LinkState.LIVE and ctl.feedback.selected == 1 and not ctl.feedback.green
        assert _wait(lambda: beeps == [1])
        assert seen[-1] == ctl.feedback
        assert _wait(lambda: ctl.link == LinkState.STALE)       # silence → stale
    finally:
        ctl.stop()

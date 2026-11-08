"""The software Limitimer: button behaviour, wire output, and the bus sender."""
import time

import pytest

from dsan import limitimer as lt
from dsan.control import Key
from dsan.devices import Limitimer, LinkState
from dsan.emulator import LimitimerBus, LimitimerEmulator, MAX_TIME


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def emu():
    c = Clock()
    e = LimitimerEmulator(clock=c)
    e.clock = c
    return e


def test_start_stop_and_countdown(emu):
    st = emu.state()
    assert st.selected.number == 1 and st.selected.total == 300 and not st.selected.running
    emu.press("start")
    emu.clock.t += 61.5
    st = emu.state()
    assert st.selected.running and st.selected.elapsed == 61 and st.display(1) == "03:59"
    emu.press(Key.START_STOP)
    emu.clock.t += 100
    assert emu.state().selected.elapsed == 61                 # stopped: time doesn't move
    emu.press("start")
    emu.clock.t += 300
    st = emu.state()
    assert st.selected.overtime and st.display(1) == "+01:01"


def test_programs_1_to_3_exclusive_session_in_background(emu):
    emu.press("p4")
    emu.press("start")                       # session runs
    emu.press("p1")
    emu.press("start")                       # P1 runs alongside the session
    emu.press("p2")                          # switching 1→2 stops P1, not the session
    st = emu.state()
    assert st.selected_index == 1
    assert [p.running for p in st.programs] == [False, False, False, True]
    emu.press("start")
    emu.press("session")                     # going to the session leaves P2 running
    st = emu.state()
    assert st.selected_index == 3 and [p.running for p in st.programs] == [False, True, False, True]


def test_time_buttons_and_seconds_mode(emu):
    emu.press("time+")
    emu.press("sumup-")
    p = emu.state().selected
    assert (p.total, p.sumup) == (360, 0)
    emu.press("sumup-")                      # never below zero
    assert emu.state().selected.sumup == 0
    emu.press("sec")
    emu.press("time-")
    emu.press("sumup+")
    p = emu.state().selected
    assert (p.total, p.sumup) == (359, 1) and p.sec_adjust
    assert not emu.state().programs[1].sec_adjust          # seconds flag shows on the selected one
    emu.set_program(1, total=MAX_TIME)
    emu.press("time+")
    assert emu.state().selected.total == MAX_TIME


def test_permit_changes(emu):
    emu.permit_changes = False
    emu.press("start")
    emu.press("time+")
    assert emu.state().selected.total == 300                # locked while running
    emu.press("stop")
    emu.press("time+")
    assert emu.state().selected.total == 360


def test_repeat_clear_beep_blink(emu):
    emu.press("start")
    emu.clock.t += 30
    emu.press("repeat")
    p = emu.state().selected
    assert not p.running and p.elapsed == 0 and p.total == 300
    emu.press("beep")
    emu.press("blink")
    p = emu.state().selected
    assert p.beep and p.blink and not emu.state().programs[1].beep
    emu.press("clear")
    p = emu.state().selected
    assert (p.total, p.sumup, p.elapsed, p.running) == (0, 0, 0, False)


def test_wire_round_trip_and_settings(emu):
    emu.press("p3")
    emu.press("start")
    emu.clock.t += 12
    raw = emu.packets(ping=True)
    events = lt.Decoder().feed(raw)
    assert [e.type for e in events] == [lt.STATUS_MSG, lt.PING_MSG] and all(e.ok for e in events)
    parsed = lt.parse_status(events[0].payload)
    assert parsed.selected_index == 2 and parsed.selected.elapsed == 12 and parsed.selected.running
    assert parsed.program_minutes and parsed.count_down and parsed.beep_type == lt.BeepType.CHIME
    s = emu.settings()
    other = LimitimerEmulator()
    other.apply_settings({**s, "programs": [[1, 2], [3, 4], [5, 6], [7, 8]], "count_down": False,
                          "beep_type": "buzz"})
    assert other.programs() == [(1, 2), (3, 4), (5, 6), (7, 8)] and not other.count_down
    assert other.beep_type == lt.BeepType.BUZZ
    other.apply_settings({"programs": "nonsense", "beep_type": "loudest"})       # ignored, no crash
    assert other.programs() == [(1, 2), (3, 4), (5, 6), (7, 8)]
    assert emu.handle_ascii(b">STOP 66\r>BAD 00\rjunk\r>PRG1 3A\r") == [Key.START_STOP, Key.PROGRAM_1]


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_limitimer_client_reads_the_emulator():
    emu = LimitimerEmulator()
    dev = Limitimer(transport_factory=emu.transport, reconnect=False)
    dev.start()
    try:
        assert _wait(lambda: dev.link == LinkState.LIVE and dev.state is not None)
        emu.press("p2")
        assert _wait(lambda: dev.state.selected_index == 1)
        assert _wait(lambda: dev.stats["ping"] >= 1)              # a ping each second, like a console
        dev.write(Key.START_STOP.wire)                              # ASCII commands work through the port too
        assert _wait(lambda: dev.state.selected.running)
    finally:
        dev.stop()


class Wire:
    """An RS-485 adapter on a bus: keeps what we send, can carry another console's packets."""
    def __init__(self, echo=True):
        self.out = bytearray()
        self.inbox = bytearray()
        self.echo = echo

    @property
    def in_waiting(self):
        return len(self.inbox)

    def read(self, n=1):
        if not self.inbox:
            time.sleep(0.005)
            return b""
        out = bytes(self.inbox[:n])
        del self.inbox[:n]
        return out

    def write(self, data):
        self.out += data
        if self.echo:                     # half-duplex adapters hear themselves
            self.inbox += data
        return len(data)

    def close(self):
        pass


def test_bus_sends_console_packets_and_spots_another_console():
    emu = LimitimerEmulator()
    wire = Wire()
    bus = LimitimerBus(emu, transport_factory=lambda: wire, reconnect=False)
    bus.start()
    try:
        assert _wait(lambda: bus.sent >= 12)
        events = [e for e in lt.Decoder().feed(bytes(wire.out))]
        assert all(e.ok for e in events)
        kinds = [e.type for e in events]
        assert kinds.count(lt.PING_MSG) >= 1 and kinds.count(lt.STATUS_MSG) >= 10
        assert lt.parse_status(events[0].payload).programs[0].total == 300
        assert bus.foreign == 0 and not bus.other_console       # own echo isn't "another console"
        rival = lt.LimitimerState(programs=tuple(lt.Program(i + 1, 0, 42, 0, 7) for i in range(4)))
        wire.inbox += lt.encode_status(rival)
        assert _wait(lambda: bus.other_console)
    finally:
        bus.stop()
    rate = bus.sent
    assert 8 <= rate                                             # roughly 10 a second

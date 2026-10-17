import pytest

from dsan import limitimer as lt
from dsan import perfectcue as pc
from dsan.limitimer import BeepType, Decoder, LimitimerState, Program


def make_state(**kw):
    progs = kw.pop("programs", [(1, 300, 30, 100), (7, 300, 30, 320), (0, 3700, 0, 0), (0, 600, 60, 550)])
    return LimitimerState(programs=tuple(Program(i + 1, *p) for i, p in enumerate(progs)), **kw)


def decode_one(packet: bytes, **kw) -> lt.PacketEvent:
    events = Decoder(**kw).feed(packet)
    assert len(events) == 1
    return events[0]


# -- Perfect Cue ---------------------------------------------------------------

@pytest.mark.parametrize("cmd, wire", [
    (pc.Command.FORWARD, b">FORWARD 35\r"),
    (pc.Command.REVERSE, b">REVERSE 3C\r"),
    (pc.Command.BLANK_ON, b">BLANKON 25\r"),
    (pc.Command.BLANK_OFF, b">BLANKOF 1D\r"),
])
def test_perfectcue_checksums(cmd, wire):
    assert cmd.value == wire


@pytest.mark.parametrize("name, cmd", [
    ("forward", pc.Command.FORWARD), ("NEXT", pc.Command.FORWARD), ("prev", pc.Command.REVERSE),
    ("blank-on", pc.Command.BLANK_ON), ("blankoff", pc.Command.BLANK_OFF),
])
def test_command_parse_aliases(name, cmd):
    assert pc.Command.parse(name) is cmd


def test_command_parse_rejects_unknown():
    with pytest.raises(ValueError):
        pc.Command.parse("explode")


def test_command_builder_validates():
    with pytest.raises(ValueError):
        pc.command("TWO WORDS")


def test_decode_buttons_ignores_noise():
    assert pc.decode_buttons(b"\x00\x0f\x55\x3f\x1f\xff") == [pc.Button.NEXT, pc.Button.BLANK_ON, pc.Button.PREV]


# -- Limitimer framing ---------------------------------------------------------

def test_roundtrip_status():
    state = make_state(selected_index=1, sequence=3, program_minutes=True,
                       beep_type=BeepType.CHIME, beep_loud=True, permit_changes=False)
    pkt = lt.encode_status(state)
    assert len(pkt) == lt.STATUS_PACKET_LEN
    ev = decode_one(pkt)
    assert ev.ok and ev.type == lt.STATUS_MSG
    assert lt.parse_status(ev.payload) == state


def test_byte_at_a_time():
    pkt = lt.encode_status(make_state())
    d = Decoder()
    events = [e for b in pkt for e in d.feed(bytes([b]))]
    assert len(events) == 1 and events[0].ok


def test_ping():
    ev = decode_one(lt.encode_ping())
    assert ev.ok and ev.type == lt.PING_MSG


def test_control_bytes_inside_crc():
    base = make_state()
    for e in range(20000):
        st = LimitimerState(programs=(Program(1, 1, 20000, 0, e),) + base.programs[1:])
        pkt = lt.encode_status(st)
        if set(pkt[-3:-1]) & {0x81, 0x83, 0xFF}:
            break
    else:
        pytest.fail("no CRC with a control byte found")
    events = Decoder().feed(b"\x00\x12\xff" + pkt + pkt)
    assert [e.ok for e in events] == [True, True]
    assert lt.parse_status(events[0].payload).programs[0].elapsed == e


def test_bad_crc_rejected():
    pkt = bytearray(lt.encode_status(make_state()))
    pkt[-3] ^= 1
    ev = decode_one(bytes(pkt))
    assert not ev.ok and "CRC mismatch" in ev.error


def test_zero_crc_policy():
    pkt = bytearray(lt.encode_status(make_state()))
    pkt[-3] = pkt[-2] = 0
    assert decode_one(bytes(pkt)).zero_crc
    assert decode_one(bytes(pkt), accept_zero_crc=False).error == "zero CRC rejected"


def test_bad_end_byte():
    pkt = bytearray(lt.encode_status(make_state()))
    pkt[-1] = 0x00
    assert "end byte" in decode_one(bytes(pkt)).error


def test_resync_after_garbage():
    pkt = lt.encode_status(make_state())
    events = Decoder().feed(pkt[:20] + b"\x81" + pkt[1:])   # truncated packet, then a restart
    assert [e.ok for e in events] == [True]


# -- state semantics --------------------------------------------------------------

def test_program_flags_and_timing():
    s = make_state(program_minutes=True)
    p1, p2, p3, p4 = s.programs
    assert p1.running and not p1.blink
    assert p2.expired and p2.overtime and p2.remaining == -20 and p2.color == 2
    assert p4.warning and p4.color == 1
    assert not p3.expired


def test_display_countdown():
    s = make_state(program_minutes=True)
    assert s.display(2) == "+00:20"
    assert s.display(2, overtime=False) == "00:00"
    assert s.display(3) == "01:01:40"
    assert s.display(3, follow_device_format=True) == "61:40"      # DIP 3 on → MM:SS
    assert s.display(4, follow_device_format=True) == "00:00"      # DIP 2 off → HH:MM


def test_display_countup():
    s = make_state(count_down=False)
    assert s.display(2) == "05:20"
    assert s.display(2, overtime=False) == "05:00"


def test_to_dict_shape():
    d = make_state(selected_index=1).to_dict()
    assert d["selected"] == 2
    assert d["programs"][1]["display"] == "+00:20"
    assert d["programs"][1]["remaining"] == -20
    assert d["settings"]["beep_type"] == "none"


def test_program_lookup_bounds():
    with pytest.raises(ValueError):
        make_state().program(5)

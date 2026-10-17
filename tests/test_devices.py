import socket
import threading
import time

import pytest

from dsan import Button, Command, Limitimer, LinkState, PerfectCue
from dsan import limitimer as lt
from dsan.sim import LimitimerSimulator, ShowSequence


def wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_show_sequence_runs_only_selected():
    show = ShowSequence(programs=((10, 3), (5, 2), (4, 1), (6, 2)), overtime=2)
    show.advance(8)
    st = show.state()
    assert st.selected_index == 0 and st.programs[0].running and st.programs[0].warning
    assert st.programs[1].elapsed == 0 and not st.programs[1].running
    show.advance(4.5)     # past 10 + 2 overtime → P2 takes over
    st = show.state()
    assert st.selected_index == 1 and st.programs[0].expired and not st.programs[0].running


def test_simulated_limitimer_goes_live_and_changes():
    changes = []
    links = []
    with Limitimer.simulated(speed=20) as dev:
        dev.on_change(lambda s, p: changes.append(s))
        dev.on_link(links.append)
        assert dev.wait_for_state(timeout=2) is not None
        assert wait(lambda: len(changes) >= 3)
        assert dev.link == LinkState.LIVE
        assert dev.stats["status"] > 0 and dev.stats["ping"] >= 0 and dev.stats["errors"] == 0
    assert links[-1] == LinkState.CLOSED


def test_on_change_ignores_sequence_counter():
    seen = []
    frozen = ShowSequence()
    dev = Limitimer(transport_factory=lambda: LimitimerSimulator(rate_hz=50, show=frozen, speed=0),
                    reconnect=False)
    dev.on_change(lambda s, p: seen.append(s))
    states = []
    dev.on_state(states.append)
    with dev:
        assert wait(lambda: len(states) >= 10)
    assert len(seen) == 1     # sequence ticks, nothing else changes


class _SilentAfter(LimitimerSimulator):
    """Sends packets for `live_for` seconds, then goes quiet (cable pulled)."""
    def __init__(self, live_for):
        super().__init__(rate_hz=20)
        self.until = time.monotonic() + live_for

    def _pump(self):
        if time.monotonic() < self.until:
            super()._pump()


def test_link_goes_stale_and_recovers():
    phases = iter([0.3, 5.0])
    dev = Limitimer(transport_factory=lambda: _SilentAfter(next(phases)), stale_after=0.3,
                    reconnect=False)
    links = []
    dev.on_link(links.append)
    with dev:
        assert wait(lambda: dev.link == LinkState.STALE, 3)
    assert links[:3] == [LinkState.OPENING, LinkState.WAITING, LinkState.LIVE]


class _Flaky(LimitimerSimulator):
    count = 0

    def __init__(self):
        type(self).count += 1
        if type(self).count == 1:
            raise OSError("port busy")
        super().__init__(rate_hz=20)


def test_reconnects_after_open_failure():
    _Flaky.count = 0
    dev = Limitimer(transport_factory=_Flaky, reconnect_interval=0.1)
    errors, links = [], []
    dev.on_error(errors.append)
    dev.on_link(links.append)
    with dev:
        assert wait(lambda: dev.link == LinkState.LIVE)
    assert errors and "busy" in str(errors[0])
    assert LinkState.RECONNECTING in links


def test_bad_callback_does_not_kill_reader():
    with Limitimer.simulated(speed=10) as dev:
        dev.on_state(lambda s: 1 / 0)
        good = []
        dev.on_state(good.append)
        assert wait(lambda: len(good) >= 5)


def test_socket_url_transport():
    """pyserial's socket:// URLs let you use RS-485-to-Ethernet converters."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    pkt = lt.encode_status(lt.LimitimerState(selected_index=2))

    def feed():
        conn, _ = srv.accept()
        try:
            for _ in range(20):
                conn.sendall(pkt)
                time.sleep(0.05)
        except OSError:
            pass          # client got its state and hung up
        finally:
            conn.close()

    threading.Thread(target=feed, daemon=True).start()
    with Limitimer(f"socket://127.0.0.1:{port}", reconnect=False) as dev:
        st = dev.wait_for_state(timeout=3)
    srv.close()
    assert st is not None and st.selected_index == 2


def test_perfectcue_buttons_and_commands():
    presses = []
    sim_ports = []

    def factory():
        from dsan.sim import PerfectCueSimulator
        p = PerfectCueSimulator(interval=0.05)
        sim_ports.append(p)
        return p

    with PerfectCue(transport_factory=factory, reconnect=False) as cue:
        cue.on_button(presses.append)
        assert wait(lambda: len(presses) >= 4)
        assert cue.forward() and cue.blank(False) and cue.send("reverse")
        assert cue.last_button in Button
    assert presses[:4] == [Button.NEXT, Button.NEXT, Button.NEXT, Button.PREV]
    assert sim_ports[0].written == [Command.FORWARD.value, Command.BLANK_OFF.value, Command.REVERSE.value]


def test_send_while_closed_returns_false():
    cue = PerfectCue("/dev/does-not-exist", reconnect=False)
    assert cue.forward() is False


def test_requires_port_or_factory():
    with pytest.raises(ValueError):
        Limitimer()

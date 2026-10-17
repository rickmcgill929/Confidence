"""OSC output for Limitimer state and Perfect Cue buttons (needs ``pip install dsan[osc]``).

Address map (prefix defaults to ``/dsan``; N = 1-4)::

    /link                    bool   Limitimer data is live
    /countdown               bool   True = countdown, False = count-up
    /selected                int    selected program 1-4
    /timer/N/time            str    display string, '+' prefix when over
    /timer/N/seconds         int    display seconds (unsigned)
    /timer/N/remaining       int    signed seconds left (negative when over)
    /timer/N/overtime        bool   past zero
    /timer/N/total|sumup|elapsed   int seconds
    /timer/N/running|warning|expired|blink   bool
    /timer/N/color           int    0 normal, 1 warning, 2 expired
    /perfectcue/button       str    NEXT | PREV | BLANK_ON | BLANK_OFF
    /perfectcue/value        int    raw byte 0x0F | 0x1F | 0x3F | 0x2F
"""
from __future__ import annotations

import threading
from typing import List, Optional, Sequence, Tuple, Union

from . import limitimer as lt
from . import perfectcue as pc
from .devices import Limitimer, LinkState, PerfectCue

Message = Tuple[str, object]
Overtime = Union[bool, Sequence[bool]]


def _ot(overtime: Overtime, i: int) -> bool:
    return bool(overtime if isinstance(overtime, bool) else overtime[i])


def state_messages(state: lt.LimitimerState, prefix: str = "/dsan", overtime: Overtime = True,
                   follow_device_format: bool = False) -> List[Message]:
    """All (address, value) pairs for one state. ``overtime`` may be one bool or one per program."""
    msgs: List[Message] = [
        (f"{prefix}/countdown", bool(state.count_down)),
        (f"{prefix}/selected", state.selected_index + 1),
    ]
    for i, p in enumerate(state.programs):
        ot = _ot(overtime, i)
        a = f"{prefix}/timer/{p.number}"
        msgs += [
            (f"{a}/time", state.display(p.number, ot, follow_device_format)),
            (f"{a}/seconds", state.display_seconds(p.number, ot)),
            (f"{a}/remaining", p.remaining),
            (f"{a}/overtime", p.overtime),
            (f"{a}/total", p.total),
            (f"{a}/sumup", p.sumup),
            (f"{a}/elapsed", p.elapsed),
            (f"{a}/running", p.running),
            (f"{a}/warning", p.warning),
            (f"{a}/expired", p.expired),
            (f"{a}/blink", p.blink),
            (f"{a}/color", p.color),
        ]
    return msgs


def button_messages(button: pc.Button, prefix: str = "/dsan") -> List[Message]:
    return [(f"{prefix}/perfectcue/button", button.name),
            (f"{prefix}/perfectcue/value", int(button.value))]


class OscBridge:
    """Forward a Limitimer and/or Perfect Cue to an OSC receiver.

    >>> bridge = OscBridge("127.0.0.1", 5000, limitimer=lt, perfectcue=cue)

    Options (``prefix``, ``bundle``, ``overtime``, ``follow_device_format``) can be
    changed at any time; call :meth:`resend` to push the current state again.
    """

    def __init__(self, host: str, port: int, *, limitimer: Optional[Limitimer] = None,
                 perfectcue: Optional[PerfectCue] = None, prefix: str = "/dsan",
                 bundle: bool = True, overtime: Overtime = True,
                 follow_device_format: bool = False):
        try:
            from pythonosc import udp_client
        except ImportError as e:  # pragma: no cover
            raise ImportError("OSC needs python-osc: pip install 'dsan[osc]'") from e
        self.client = udp_client.SimpleUDPClient(host, port)
        self.host, self.port = host, port
        self.prefix = prefix
        self.bundle = bundle
        self.overtime = overtime
        self.follow_device_format = follow_device_format
        self.last_error: Optional[str] = None
        self._last: Optional[tuple] = None
        self._lock = threading.Lock()
        self._lt = limitimer
        self._pc = perfectcue
        if limitimer:
            limitimer.on_state(self._on_state)
            limitimer.on_link(self._on_link)
            if limitimer.link != LinkState.CLOSED:
                self._on_link(limitimer.link)      # liveness first, then the state
            if limitimer.state is not None:
                self.send_state(limitimer.state)
        if perfectcue:
            perfectcue.on_button(self._on_button)

    def close(self) -> None:
        """Stop forwarding (unsubscribes from the devices)."""
        if self._lt:
            self._lt.remove_callback(self._on_state)
            self._lt.remove_callback(self._on_link)
        if self._pc:
            self._pc.remove_callback(self._on_button)

    # -- sending ---------------------------------------------------------------

    def send(self, messages: List[Message]) -> None:
        """Send pairs as one bundle (atomic for the receiver) or as individual messages."""
        try:
            if self.bundle and len(messages) > 1:
                from pythonosc.osc_bundle_builder import IMMEDIATELY, OscBundleBuilder
                from pythonosc.osc_message_builder import OscMessageBuilder
                b = OscBundleBuilder(IMMEDIATELY)
                for addr, val in messages:
                    m = OscMessageBuilder(address=addr)
                    m.add_arg(val)
                    b.add_content(m.build())
                self.client.send(b.build())
            else:
                for addr, val in messages:
                    self.client.send_message(addr, val)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)

    def send_state(self, state: lt.LimitimerState, force: bool = False) -> None:
        msgs = state_messages(state, self.prefix, self.overtime, self.follow_device_format)
        key = tuple(msgs)
        with self._lock:
            if key == self._last and not force:
                return
            self._last = key
        self.send(msgs)

    def resend(self) -> None:
        """Push the current state again (after changing options)."""
        if self._lt and self._lt.state is not None:
            self.send_state(self._lt.state, force=True)
        if self._lt:
            self._on_link(self._lt.link)

    # -- device callbacks -------------------------------------------------------

    def _on_state(self, state: lt.LimitimerState) -> None:
        self.send_state(state)

    def _on_link(self, link: LinkState) -> None:
        self.send([(f"{self.prefix}/link", link == LinkState.LIVE)])

    def _on_button(self, button: pc.Button) -> None:
        self.send(button_messages(button, self.prefix))


__all__ = ["OscBridge", "button_messages", "state_messages"]

"""OSC for the Limitimer and Perfect Cue (needs ``pip install dsan[osc]``).

OSC out — :class:`OscBridge`. Address map (prefix defaults to ``/dsan``; N = 1-4)::

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

OSC in — :class:`OscListener`, commands for the Perfect Cue::

    /perfectcue/press  NAME     act as if a button was pressed: next | prev | blank_on | blank_off
    /perfectcue/next            shortcuts for /press …
    /perfectcue/prev
    /perfectcue/blank  [1|0]    1 (or no argument) = blank on, 0 = blank off
    /perfectcue/blank_on
    /perfectcue/blank_off
    /perfectcue/led    NAME     light the Perfect Cue's LEDs (master mode): next | prev |
                                blank_on | blank_off | error
    /perfectcue/led/NAME        same, e.g. /perfectcue/led/next
    /perfectcue/command NAME    send a receiver command: forward | reverse | blank_on | blank_off
    /perfectcue/command/NAME    same, e.g. /perfectcue/command/forward

Names are case-insensitive. A single argument of 0 on a trigger address (not
/blank) is ignored, so a button that sends 1 on press and 0 on release fires once.
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


class OscError(ValueError):
    """An OSC command that couldn't be carried out (unknown name, no Perfect Cue…)."""


_PRESS_ALIASES = {"NEXT": pc.Button.NEXT, "FORWARD": pc.Button.NEXT, "PREV": pc.Button.PREV,
                  "PREVIOUS": pc.Button.PREV, "REVERSE": pc.Button.PREV, "BACK": pc.Button.PREV,
                  "BLANK_ON": pc.Button.BLANK_ON, "BLANKON": pc.Button.BLANK_ON, "BLANK": pc.Button.BLANK_ON,
                  "BLANK_OFF": pc.Button.BLANK_OFF, "BLANKOFF": pc.Button.BLANK_OFF,
                  "UNBLANK": pc.Button.BLANK_OFF}


def parse_button(name: str) -> pc.Button:
    key = str(name).strip().upper().replace("-", "_")
    try:
        return _PRESS_ALIASES[key]
    except KeyError:
        raise OscError(f"unknown button {name!r}; expected next, prev, blank_on or blank_off") from None


def _is_release(args: tuple) -> bool:
    return len(args) == 1 and isinstance(args[0], (int, float)) and not isinstance(args[0], bool) \
        and args[0] == 0 or args == (False,)


class OscListener:
    """Receive OSC commands for a Perfect Cue on a UDP port (see the module docstring).

    >>> listener = OscListener(9000, perfectcue=cue)           # presses go to cue.press()
    >>> listener.close()

    ``on_press(button)`` replaces the default action for presses (an app can show
    the cue itself, e.g. when no Perfect Cue is connected). ``on_message(address,
    args, result)`` is called for every message, with result ``"ok: …"`` or the
    error text. Callbacks run on the listener's thread.
    """

    def __init__(self, port: int, host: str = "0.0.0.0", *, perfectcue: Optional[PerfectCue] = None,
                 prefix: str = "/dsan", on_press=None, on_message=None):
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
        except ImportError as e:  # pragma: no cover
            raise ImportError("OSC needs python-osc: pip install 'dsan[osc]'") from e
        self.perfectcue = perfectcue
        self.prefix = prefix.rstrip("/")
        self.on_press = on_press
        self.on_message = on_message
        self.received = 0
        self.last: Optional[Tuple[str, tuple, str]] = None
        d = Dispatcher()
        d.set_default_handler(self._handle)
        self._server = ThreadingOSCUDPServer((host, port), d)
        self.host, self.port = self._server.server_address[:2]
        self._thread = threading.Thread(target=self._server.serve_forever, name="dsan-osc-in", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- dispatch ---------------------------------------------------------------

    def _handle(self, address: str, *args) -> None:
        self.received += 1
        try:
            result = "ok: " + self.handle(address, *args)
        except OscError as e:
            result = str(e)
        except Exception as e:                      # never let a bad message kill the server
            result = f"error: {e}"
        self.last = (address, args, result)
        if self.on_message:
            try:
                self.on_message(address, args, result)
            except Exception:
                pass

    def handle(self, address: str, *args) -> str:
        """Carry out one command; returns what was done or raises OscError. Thread-agnostic."""
        base = f"{self.prefix}/perfectcue/"
        if not address.lower().startswith(base.lower()):
            raise OscError(f"ignored {address} (not under {base})")
        parts = address[len(base):].strip("/").lower().split("/")
        verb, rest = parts[0], parts[1:]
        name = rest[0] if rest else (str(args[0]) if args and isinstance(args[0], str) else None)

        if verb == "blank" and not rest:
            on = True if not args else bool(args[0]) if not isinstance(args[0], str) else \
                args[0].strip().lower() not in ("0", "off", "false")
            return self._press(pc.Button.BLANK_ON if on else pc.Button.BLANK_OFF)
        if verb in ("next", "prev", "previous", "blank_on", "blank_off", "unblank") and not rest:
            if _is_release(args):
                return "release ignored"
            return self._press(parse_button(verb))
        if verb == "press":
            if name is None:
                raise OscError("/press needs a button name: next, prev, blank_on or blank_off")
            return self._press(parse_button(name))
        if verb in ("led", "command"):
            if name is None:
                raise OscError(f"/{verb} needs a name")
            if rest and _is_release(args):
                return "release ignored"
            cue = self.perfectcue
            if cue is None:
                raise OscError("no Perfect Cue connected")
            if verb == "led":
                try:
                    led = pc.Led.parse(name)
                except ValueError as e:
                    raise OscError(str(e)) from None
                if not cue.light(led):
                    raise OscError("Perfect Cue port isn't open")
                return f"LED {led.name}"
            try:
                cmd = pc.Command.parse(name)
            except ValueError as e:
                raise OscError(str(e)) from None
            if not cue.send(cmd):
                raise OscError("Perfect Cue port isn't open")
            return f"command {cmd.name}"
        raise OscError(f"unknown address {address}")

    def _press(self, button: pc.Button) -> str:
        if self.on_press is not None:
            self.on_press(button)
        elif self.perfectcue is not None:
            self.perfectcue.press(button)
        else:
            raise OscError("no Perfect Cue connected")
        return f"press {button.name}"


__all__ = ["OscBridge", "OscError", "OscListener", "button_messages", "parse_button", "state_messages"]

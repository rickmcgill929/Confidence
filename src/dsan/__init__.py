"""dsan — talk to DSAN Limitimer stage timers and Perfect Cue presentation remotes.

    from dsan import Limitimer

    with Limitimer("/dev/tty.usbserial-A1") as lt:
        state = lt.wait_for_state(timeout=5)
        print(state.display(1))

Layers, use whichever you need:

* ``dsan.limitimer`` / ``dsan.perfectcue`` — pure protocol (decode/encode), no I/O
* ``dsan.Limitimer`` / ``dsan.PerfectCue`` — threaded serial clients with reconnect
* ``dsan.LimitimerControl`` — press a Limitimer's buttons through DSAN's TP-2000X (``dsan.control``)
* ``dsan.LimitimerEmulator`` — a Limitimer in software; ``dsan.emulator.LimitimerBus`` drives DSAN lights
* ``dsan.osc.OscBridge`` — OSC output (``pip install dsan[osc]``)
* ``dsan.server.DsanServer`` — HTTP + WebSocket API (``pip install dsan[server]``)
* ``dsan`` command — ``dsan serve``, ``dsan monitor``, ``dsan send``, ``dsan ports``
* ``confidence`` — Confidence, the show-clock app for a Limitimer, Perfect Cue and Millumin (``pip install dsan[clock]``)
"""
__version__ = "0.13.0"

from .limitimer import BeepType, Decoder, LimitimerState, PacketEvent, Program
from .perfectcue import Button, Command
from .devices import Limitimer, LinkState, PerfectCue
from .control import Key, LimitimerControl
from .emulator import LimitimerEmulator

__all__ = [
    "__version__", "BeepType", "Button", "Command", "Decoder", "Key", "Limitimer", "LimitimerControl",
    "LimitimerEmulator", "LimitimerState", "LinkState", "PacketEvent", "PerfectCue", "Program",
]

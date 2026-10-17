"""``dsan`` command-line tool.

    dsan ports
    dsan monitor --limitimer /dev/ttyUSB0 [--perfectcue /dev/ttyUSB1] [--json]
    dsan serve   --limitimer /dev/ttyUSB0 --perfectcue /dev/ttyUSB1 [--osc 127.0.0.1:5000]
    dsan send    /dev/ttyUSB1 forward

Use ``sim`` instead of a port to run against the built-in simulator.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from typing import Optional

from . import __version__
from .devices import Limitimer, LinkState, PerfectCue


def _limitimer(port: Optional[str], args) -> Optional[Limitimer]:
    if not port:
        return None
    if port == "sim":
        return Limitimer.simulated(speed=args.sim_speed, stale_after=args.stale_after)
    return Limitimer(port, stale_after=args.stale_after,
                     accept_zero_crc=not args.reject_zero_crc)


def _perfectcue(port: Optional[str], args) -> Optional[PerfectCue]:
    if not port:
        return None
    if port == "sim":
        return PerfectCue.simulated()
    return PerfectCue(port, baudrate=args.pc_baud)


def _host_port(text: str, default_port: int) -> tuple:
    host, _, port = text.rpartition(":")
    if not host:
        return text, default_port
    return host, int(port)


def _wait_forever(stop: Optional[threading.Event] = None) -> None:
    stop = stop or threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.wait(0.5):
        pass


# -- commands --------------------------------------------------------------------

def cmd_ports(args) -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is not installed", file=sys.stderr)
        return 1
    ports = list(list_ports.comports())
    for p in ports:
        print(f"{p.device:<32} {p.description}")
    if not ports:
        print("no serial ports found")
    return 0


def cmd_monitor(args) -> int:
    lt = _limitimer(args.limitimer, args)
    cue = _perfectcue(args.perfectcue, args)
    if not (lt or cue):
        print("give --limitimer and/or --perfectcue", file=sys.stderr)
        return 2

    stop = threading.Event()
    print_lock = threading.Lock()   # two reader threads print; keep lines whole

    def out(obj, text):
        if stop.is_set():
            return
        with print_lock:
            try:
                print(json.dumps(obj) if args.json else text, flush=True)
            except BrokenPipeError:   # e.g. piped into `head` — exit quietly
                stop.set()

    if lt:
        @lt.on_change
        def _(state, prev):
            line = "  ".join(
                f"P{p.number}{'▶' if p.running else ' '}{state.display(p.number, not args.no_overtime, args.follow_format):>9}"
                f"{'!' if p.warning else ''}{'×' if p.expired else ''}"
                for p in state.programs)
            out({"type": "limitimer.state", "state": state.to_dict(not args.no_overtime, args.follow_format)},
                f"{'↓' if state.count_down else '↑'} sel P{state.selected_index + 1} | {line}")

        lt.on_link(lambda link: out({"type": "limitimer.link", "link": link.value},
                                    f"-- limitimer link: {link.value}"))
        lt.on_error(lambda e: print(f"-- limitimer error: {e}", file=sys.stderr))
        lt.start()
    if cue:
        cue.on_button(lambda b: out({"type": "perfectcue.button", "button": b.name, "value": int(b)},
                                    f"-- perfect cue: {b.name}"))
        cue.on_link(lambda link: out({"type": "perfectcue.link", "link": link.value},
                                     f"-- perfect cue link: {link.value}"))
        cue.on_error(lambda e: print(f"-- perfect cue error: {e}", file=sys.stderr))
        cue.start()

    _wait_forever(stop)
    for d in (lt, cue):
        if d:
            d.stop()
    try:
        sys.stdout.close()
    except BrokenPipeError:
        pass
    return 0


def cmd_serve(args) -> int:
    try:
        from .server import DsanServer
    except ImportError:
        print("the server needs aiohttp: pip install 'dsan[server]'", file=sys.stderr)
        return 1
    lt = _limitimer(args.limitimer, args)
    cue = _perfectcue(args.perfectcue, args)
    if not (lt or cue):
        print("give --limitimer and/or --perfectcue (or 'sim')", file=sys.stderr)
        return 2

    bridge = None
    if args.osc:
        from .osc import OscBridge
        host, port = _host_port(args.osc, 5000)
        bridge = OscBridge(host, port, limitimer=lt, perfectcue=cue, prefix=args.osc_prefix,
                           bundle=not args.no_bundle, overtime=not args.no_overtime,
                           follow_device_format=args.follow_format)

    server = DsanServer(lt, cue, host=args.host, port=args.port, token=args.token,
                        overtime=not args.no_overtime, follow_device_format=args.follow_format)
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"dsan {__version__} — http://{shown}:{args.port}/  (API at /api/v1)")
    if lt:
        print(f"  limitimer:   {args.limitimer}")
    if cue:
        print(f"  perfect cue: {args.perfectcue}")
    if bridge:
        print(f"  osc:         {bridge.host}:{bridge.port} prefix {args.osc_prefix}")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print("  WARNING: listening on the network with no --token; anyone can send Perfect Cue commands",
              file=sys.stderr)
    server.run()
    return 0


def cmd_send(args) -> int:
    from .perfectcue import Command
    try:
        cmd = Command.parse(args.command)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    cue = PerfectCue(args.port, baudrate=args.pc_baud, reconnect=False)
    opened = threading.Event()
    cue.on_link(lambda link: opened.set() if link == LinkState.OPEN else None)
    cue.start()
    try:
        if not opened.wait(3) or not cue.send(cmd):
            print(f"could not open {args.port}: {cue.last_error or 'timeout'}", file=sys.stderr)
            return 1
        print(f"sent {cmd.name}: {cmd.value!r}")
        return 0
    finally:
        cue.stop()


# -- argument parsing ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dsan", description="DSAN Limitimer / Perfect Cue tools")
    ap.add_argument("--version", action="version", version=f"dsan {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def device_opts(p):
        p.add_argument("--limitimer", "-l", metavar="PORT",
                       help="Limitimer port (device path, socket://host:port, or 'sim')")
        p.add_argument("--perfectcue", "-p", metavar="PORT",
                       help="Perfect Cue receiver port (device path, socket://host:port, or 'sim')")
        p.add_argument("--pc-baud", type=int, default=9600, help="Perfect Cue baud rate (default 9600)")
        p.add_argument("--stale-after", type=float, default=2.0,
                       help="seconds without data before the Limitimer link is STALE (default 2)")
        p.add_argument("--reject-zero-crc", action="store_true",
                       help="reject packets with a 0x0000 checksum (Go reference accepts them)")
        p.add_argument("--sim-speed", type=float, default=1.0, help="simulator speed multiplier")
        p.add_argument("--no-overtime", action="store_true",
                       help="display strings hold at 00:00 / total instead of counting past it")
        p.add_argument("--follow-format", action="store_true",
                       help="display strings follow DIP 2/3 (MM:SS or HH:MM)")

    sub.add_parser("ports", help="list serial ports").set_defaults(func=cmd_ports)

    m = sub.add_parser("monitor", help="print Limitimer changes and Perfect Cue buttons")
    device_opts(m)
    m.add_argument("--json", action="store_true", help="one JSON object per line")
    m.set_defaults(func=cmd_monitor)

    s = sub.add_parser("serve", help="run the HTTP/WebSocket API (and optional OSC output)")
    device_opts(s)
    s.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1; use 0.0.0.0 for the network)")
    s.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    s.add_argument("--token", help="require this bearer token on /api requests")
    s.add_argument("--osc", metavar="HOST:PORT", help="also send OSC to this receiver")
    s.add_argument("--osc-prefix", default="/dsan", help="OSC address prefix (default /dsan)")
    s.add_argument("--no-bundle", action="store_true", help="send OSC messages individually")
    s.set_defaults(func=cmd_serve)

    snd = sub.add_parser("send", help="send one command to a Perfect Cue receiver")
    snd.add_argument("port")
    snd.add_argument("command", help="forward | reverse | blank_on | blank_off")
    snd.add_argument("--pc-baud", type=int, default=9600)
    snd.set_defaults(func=cmd_send)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

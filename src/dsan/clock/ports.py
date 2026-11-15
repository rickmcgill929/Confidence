"""Finding the right USB-RS485 adapter for each device.

Two identical USB adapters get whatever device name the OS hands out
(``/dev/cu.usbserial-A1``, ``COM7``…), and that can change when they're
re-plugged. So a port is remembered as a :class:`PortRef` (USB serial number,
then physical USB location, then device path) and resolved again at startup.

:func:`detect` works out which adapter is which by listening: the Limitimer
sends status packets several times a second, so the port carrying valid frames
is the Limitimer. The Perfect Cue only sends when a button is pressed, so it is
either the one remaining adapter, or the one that sees a press during detection.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from .. import limitimer as lt
from .. import perfectcue as pc


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str = ""
    serial_number: str = ""
    location: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None

    @property
    def is_usb(self) -> bool:
        return self.vid is not None

    def short_label(self) -> str:
        """For dropdowns: 'FT232R USB UART — cu.usbserial-A10K1 · SN A10K1'."""
        name = self.device.rsplit("/", 1)[-1]
        desc = self.description if self.description and self.description not in ("n/a", name) else ""
        sn = f" · SN {self.serial_number}" if self.serial_number and self.serial_number not in name else ""
        return f"{desc} — {name}{sn}" if desc else f"{name}{sn}"

    def label(self) -> str:
        extra = f"  [SN {self.serial_number}]" if self.serial_number else ""
        desc = f" — {self.description}" if self.description and self.description != "n/a" else ""
        return f"{self.device}{desc}{extra}"


def list_ports(usb_only: bool = False) -> List[PortInfo]:
    try:
        from serial.tools import list_ports as lp
    except ImportError:  # pragma: no cover
        return []
    out = []
    for p in lp.comports():
        info = PortInfo(p.device, p.description or "", p.serial_number or "", p.location or "",
                        p.vid, p.pid)
        if info.is_usb or not usb_only:
            out.append(info)
    return out


@dataclass(frozen=True)
class PortRef:
    """How a chosen port is remembered between launches."""
    device: str = ""
    serial_number: str = ""
    location: str = ""
    description: str = ""

    @classmethod
    def of(cls, info: PortInfo) -> "PortRef":
        return cls(info.device, info.serial_number, info.location, info.description)

    @classmethod
    def url(cls, device: str) -> "PortRef":
        """A typed-in port or URL (socket://…, sim) with no USB identity."""
        return cls(device=device)

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["PortRef"]:
        if not d or not d.get("device"):
            return None
        return cls(**{k: str(d.get(k) or "") for k in ("device", "serial_number", "location", "description")})

    def resolve(self, ports: Iterable[PortInfo]) -> Optional[str]:
        """Current device path for this port, or None if that adapter isn't plugged in.

        Matches the USB serial number when it identifies exactly one adapter (FTDI
        chips have unique serials; many CH340 clones share or omit one), then the
        physical USB socket, then the old device path. URLs pass straight through.
        """
        ports = list(ports)
        if "://" in self.device or self.device == "sim":
            return self.device
        if self.serial_number:
            same = [p for p in ports if p.serial_number == self.serial_number]
            if len(same) == 1:
                return same[0].device
            if len(same) > 1 and self.location:
                loc = [p for p in same if p.location == self.location]
                if len(loc) == 1:
                    return loc[0].device
        if self.location:
            loc = [p for p in ports if p.location == self.location]
            if len(loc) == 1:
                return loc[0].device
        if any(p.device == self.device for p in ports):
            return self.device
        return None


# -- probing -----------------------------------------------------------------------

Opener = Callable[[str, int], object]   # (device, baud) -> pyserial-like port


def _serial_opener(device: str, baud: int):
    import serial
    return serial.serial_for_url(device, baudrate=baud, bytesize=8, parity="N", stopbits=1, timeout=0.1)


@dataclass
class ProbeResult:
    device: str
    baud: int
    bytes_seen: int = 0
    lt_packets: int = 0
    lt_errors: int = 0
    pc_buttons: List[pc.Button] = field(default_factory=list)
    error: str = ""

    @property
    def looks_like_limitimer(self) -> bool:
        return self.lt_packets > 0

    @property
    def garbled(self) -> bool:
        """Data arriving but nothing decodes: usually A/B swapped or the wrong baud."""
        return self.bytes_seen >= 20 and self.lt_packets == 0 and not self.pc_buttons


def probe(device: str, baud: int, seconds: float, *, opener: Opener = _serial_opener,
          stop: Optional[threading.Event] = None,
          until: Optional[Callable[[ProbeResult], bool]] = None) -> ProbeResult:
    """Listen on a port and count what arrives. Never raises; errors land in ``.error``."""
    res = ProbeResult(device, baud)
    try:
        port = opener(device, baud)
    except Exception as e:
        res.error = str(e)
        return res
    dec = lt.Decoder()
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end and not (stop and stop.is_set()):
            data = port.read(max(1, getattr(port, "in_waiting", 0) or 1))
            if not data:
                continue
            res.bytes_seen += len(data)
            for ev in dec.feed(data):
                if ev.ok:
                    res.lt_packets += 1
                else:
                    res.lt_errors += 1
            if res.lt_packets == 0:
                res.pc_buttons += pc.decode_buttons(data)
            if until and until(res):
                break
    except Exception as e:
        res.error = str(e)
    finally:
        try:
            port.close()
        except Exception:
            pass
    return res


def _probe_all(devices: List[str], baud: int, seconds: float, opener: Opener,
               until=None, stop_all_when=None) -> Dict[str, ProbeResult]:
    results: Dict[str, ProbeResult] = {}
    stop = threading.Event()

    def run(dev):
        r = probe(dev, baud, seconds, opener=opener, stop=stop, until=until)
        results[dev] = r
        if stop_all_when and stop_all_when(r):
            stop.set()

    threads = [threading.Thread(target=run, args=(d,), daemon=True) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join(seconds + 2)
    return results


@dataclass
class DetectResult:
    limitimer: Optional[PortInfo] = None
    perfectcue: Optional[PortInfo] = None
    notes: List[str] = field(default_factory=list)
    probes: Dict[str, ProbeResult] = field(default_factory=dict)


def detect(ports: Optional[List[PortInfo]] = None, *, pc_baud: int = 19200,
           lt_seconds: float = 1.5, press_seconds: float = 8.0, opener: Opener = _serial_opener,
           on_status: Optional[Callable[[str], None]] = None) -> DetectResult:
    """Work out which USB adapter is the Limitimer and which is the Perfect Cue.

    The ports must not be open elsewhere (stop any running connections first).
    ``on_status`` gets short progress messages, e.g. to ask for a button press.
    """
    say = on_status or (lambda _m: None)
    if ports is None:
        ports = list_ports(usb_only=True)
    out = DetectResult()
    if not ports:
        out.notes.append("No USB serial adapters found. Check the adapters are plugged in "
                         "(and on macOS that the driver for CH340/CP210x adapters is installed).")
        return out

    say(f"Listening for the Limitimer on {len(ports)} port(s)…")
    by_dev = {p.device: p for p in ports}
    lt_probe = _probe_all(list(by_dev), lt.BAUDRATE, lt_seconds, opener)
    out.probes.update(lt_probe)

    found = [by_dev[d] for d, r in lt_probe.items() if r.looks_like_limitimer]
    if len(found) > 1:
        out.notes.append("More than one port is receiving Limitimer data: "
                         + ", ".join(p.device for p in found) + ". Using the busiest.")
        found.sort(key=lambda p: -lt_probe[p.device].lt_packets)
    if found:
        out.limitimer = found[0]
    else:
        garbled = [d for d, r in lt_probe.items() if r.garbled]
        if garbled:
            out.notes.append(f"Data is arriving on {', '.join(garbled)} but no valid Limitimer frames. "
                             "That's usually the RS-485 A/B pair swapped (or the wrong pinout "
                             "on the RJ45). Try swapping A and B.")
        else:
            out.notes.append("No Limitimer data on any port. Is the Limitimer on, and its "
                             "data cable connected to the adapter?")

    for d, r in lt_probe.items():
        if r.error:
            out.notes.append(f"{d}: {r.error}")

    rest = [p for p in ports if p is not out.limitimer and not lt_probe[p.device].error]
    if len(rest) == 1:
        out.perfectcue = rest[0]
        if lt_probe[rest[0].device].pc_buttons:
            out.notes.append(f"Perfect Cue button seen on {rest[0].device}.")
    elif len(rest) > 1:
        say("Press any button on the Perfect Cue presenter now…")
        presses = _probe_all([p.device for p in rest], pc_baud, press_seconds, opener,
                             until=lambda r: bool(r.pc_buttons),
                             stop_all_when=lambda r: bool(r.pc_buttons))
        out.probes.update({f"{d}@{pc_baud}": r for d, r in presses.items()})
        hit = [d for d, r in presses.items() if r.pc_buttons]
        if hit:
            out.perfectcue = by_dev[hit[0]]
        else:
            out.notes.append(f"No Perfect Cue button press seen on {', '.join(p.device for p in rest)} "
                             f"at {pc_baud} baud. Pick the Perfect Cue port by hand, or check its wiring.")
    elif out.limitimer:
        out.notes.append("Only one USB adapter found; plug in the second one for the Perfect Cue.")
    return out


__all__ = ["DetectResult", "PortInfo", "PortRef", "ProbeResult", "detect", "list_ports", "probe"]

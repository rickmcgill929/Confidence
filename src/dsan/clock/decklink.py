"""DeckLink SDI output with cut/fill (fill + key) for Confidence.

Talks to the native ``dsan_decklink`` library (src/dsan/clock/native/), built
from the Blackmagic DeckLink SDK with ``packaging/build_decklink.sh``. Needs
Blackmagic Desktop Video installed to find any cards.

Keying modes:

* ``none``      fill only; the card drops alpha (the layout background shows)
* ``external``  the card's hardware keyer: **fill on SDI A, key on SDI B**
                (DeckLink Duo 2, Quad 2, 4K Extreme 12G, 8K Pro, UltraStudio…)
* ``separate``  a second DeckLink output carries the key (alpha as grayscale),
                for cards without an external keyer. Lock both to reference
                for frame-accurate alignment.

The card's own clock paces the output (scheduled playback). Python only hands
over a new image when the picture changes, at up to ~30 fps.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

KEYING = {"none": 0, "external": 1, "separate": 2}
KEYING_LABELS = {
    "none": "Off — fill only",
    "external": "Hardware keyer — fill on A, key on B",
    "separate": "Separate key output",
}

_CAP_OUTPUT, _CAP_INTERNAL, _CAP_EXTERNAL, _CAP_INACTIVE = 1, 2, 4, 8
_MODE_BGRA, _MODE_KEYING, _MODE_INTERLACED = 1, 2, 4
ABI_VERSION = 1


class DeckLinkUnavailable(RuntimeError):
    """The native library isn't built, or Desktop Video isn't installed."""


@dataclass(frozen=True)
class DeckLinkMode:
    id: int
    name: str
    width: int
    height: int
    duration: int
    scale: int
    keying: bool
    interlaced: bool

    @property
    def fps(self) -> float:
        return self.scale / self.duration if self.duration else 0.0

    @property
    def label(self) -> str:
        return self.name or f"{self.width}x{self.height} @ {self.fps:.2f}"


@dataclass
class DeckLinkDevice:
    index: int
    name: str
    has_output: bool
    internal_keying: bool
    external_keying: bool
    inactive: bool
    modes: List[DeckLinkMode] = field(default_factory=list)

    def mode(self, mode_id: int) -> Optional[DeckLinkMode]:
        return next((m for m in self.modes if m.id == mode_id), None)


def library_name() -> str:
    return "libdsan_decklink.dylib" if sys.platform == "darwin" else "libdsan_decklink.so"


def library_candidates() -> List[Path]:
    name = library_name()
    out = []
    env = os.environ.get("DSAN_DECKLINK_LIB")
    if env:
        out.append(Path(env))
    base = getattr(sys, "_MEIPASS", None)          # PyInstaller bundle
    if base:
        out += [Path(base) / "dsan" / "clock" / "native" / name, Path(base) / name]
    out.append(Path(__file__).resolve().parent / "native" / name)
    return out


_lib = None
_lib_error: Optional[str] = None


def _load():
    global _lib, _lib_error
    if _lib is not None:
        return _lib
    if _lib_error is not None:
        raise DeckLinkUnavailable(_lib_error)
    if sys.platform.startswith("win"):
        _lib_error = "DeckLink output isn't available on Windows yet."
        raise DeckLinkUnavailable(_lib_error)
    path = next((p for p in library_candidates() if p.exists()), None)
    if path is None:
        _lib_error = ("DeckLink support isn't built yet. Run packaging/build_decklink.sh "
                      "(it needs the Blackmagic DeckLink SDK).")
        raise DeckLinkUnavailable(_lib_error)
    try:
        lib = ctypes.CDLL(str(path))
    except OSError as e:
        _lib_error = f"Couldn't load {path.name}: {e}"
        raise DeckLinkUnavailable(_lib_error) from None
    c = ctypes
    lib.dsan_dl_abi_version.restype = c.c_int
    lib.dsan_dl_api_version.restype = c.c_int64
    lib.dsan_dl_scan.restype = c.c_int
    lib.dsan_dl_device.argtypes = [c.c_int, c.c_char_p, c.c_int, c.POINTER(c.c_int)]
    lib.dsan_dl_device.restype = c.c_int
    lib.dsan_dl_mode_count.argtypes = [c.c_int]
    lib.dsan_dl_mode_count.restype = c.c_int
    lib.dsan_dl_mode.argtypes = [c.c_int, c.c_int, c.POINTER(c.c_uint32), c.c_char_p, c.c_int,
                                 c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(c.c_int64),
                                 c.POINTER(c.c_int64), c.POINTER(c.c_int)]
    lib.dsan_dl_mode.restype = c.c_int
    lib.dsan_dl_open.argtypes = [c.c_int, c.c_int, c.c_uint32, c.c_int, c.c_char_p, c.c_int]
    lib.dsan_dl_open.restype = c.c_void_p
    lib.dsan_dl_push.argtypes = [c.c_void_p, c.c_void_p, c.c_int, c.c_int, c.c_int]
    lib.dsan_dl_push.restype = c.c_int
    lib.dsan_dl_status.argtypes = [c.c_void_p, c.POINTER(c.c_uint64), c.POINTER(c.c_uint64),
                                   c.POINTER(c.c_uint64), c.POINTER(c.c_int), c.POINTER(c.c_int),
                                   c.POINTER(c.c_int)]
    lib.dsan_dl_status.restype = c.c_int
    lib.dsan_dl_close.argtypes = [c.c_void_p]
    lib.dsan_dl_close.restype = None
    if lib.dsan_dl_abi_version() != ABI_VERSION:
        _lib_error = f"{path.name} is out of date — rebuild it with packaging/build_decklink.sh."
        raise DeckLinkUnavailable(_lib_error)
    _lib = lib
    return lib


def available() -> tuple:
    """(True, driver version text) or (False, reason)."""
    try:
        lib = _load()
    except DeckLinkUnavailable as e:
        return False, str(e)
    v = lib.dsan_dl_api_version()
    if v < 0:
        return False, "Blackmagic Desktop Video isn't installed (it provides the DeckLink driver)."
    return True, f"Desktop Video {v >> 24 & 255}.{v >> 16 & 255}.{v >> 8 & 255}"


def devices() -> List[DeckLinkDevice]:
    """Re-scan and list DeckLink devices (empty if none or no driver)."""
    lib = _load()
    n = lib.dsan_dl_scan()
    out = []
    name = ctypes.create_string_buffer(256)
    for i in range(max(0, n)):
        caps = ctypes.c_int(0)
        if lib.dsan_dl_device(i, name, 256, ctypes.byref(caps)) != 0:
            continue
        d = DeckLinkDevice(i, name.value.decode("utf-8", "replace"), bool(caps.value & _CAP_OUTPUT),
                           bool(caps.value & _CAP_INTERNAL), bool(caps.value & _CAP_EXTERNAL),
                           bool(caps.value & _CAP_INACTIVE))
        for m in range(max(0, lib.dsan_dl_mode_count(i))):
            mid, w, h, fl = ctypes.c_uint32(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
            dur, sc = ctypes.c_int64(), ctypes.c_int64()
            mname = ctypes.create_string_buffer(128)
            if lib.dsan_dl_mode(i, m, ctypes.byref(mid), mname, 128, ctypes.byref(w), ctypes.byref(h),
                                ctypes.byref(dur), ctypes.byref(sc), ctypes.byref(fl)) == 0:
                d.modes.append(DeckLinkMode(mid.value, mname.value.decode("utf-8", "replace"), w.value, h.value,
                                            dur.value, sc.value, bool(fl.value & _MODE_KEYING),
                                            bool(fl.value & _MODE_INTERLACED)))
        out.append(d)
    return out


class DeckLinkOutput:
    """One running output (fill, plus key on channel B or a second device)."""

    def __init__(self, fill: DeckLinkDevice, mode: DeckLinkMode, keying: str = "none",
                 key: Optional[DeckLinkDevice] = None):
        if keying not in KEYING:
            raise ValueError(f"keying must be one of {list(KEYING)}")
        self.lib = _load()
        self.fill, self.mode, self.keying, self.key = fill, mode, keying, key
        err = ctypes.create_string_buffer(512)
        h = self.lib.dsan_dl_open(fill.index, key.index if key else -1, mode.id, KEYING[keying], err, 512)
        if not h:
            raise RuntimeError(err.value.decode("utf-8", "replace") or "Couldn't start DeckLink output")
        self._h = ctypes.c_void_p(h)

    @property
    def size(self) -> tuple:
        return self.mode.width, self.mode.height

    def push_bytes(self, data, row_bytes: int) -> None:
        """Latest image as straight-alpha BGRA bytes (bytes, bytearray or a pointer int)."""
        if self._h is None:
            return
        w, h = self.size
        if isinstance(data, int):
            ptr = ctypes.c_void_p(data)
        else:
            if row_bytes < w * 4 or len(data) < row_bytes * (h - 1) + w * 4:
                raise ValueError(f"image buffer too small for {w}x{h} BGRA")
            buf = (ctypes.c_char * len(data)).from_buffer_copy(data) if isinstance(data, bytes) else \
                (ctypes.c_char * len(data)).from_buffer(data)
            ptr = ctypes.cast(buf, ctypes.c_void_p)
        rc = self.lib.dsan_dl_push(self._h, ptr, row_bytes, self.mode.width, self.mode.height)
        if rc == -2:
            raise ValueError("image size doesn't match the DeckLink video mode")

    def push_qimage(self, image) -> None:
        """A QImage in Format_ARGB32 (straight) or ARGB32_Premultiplied (premultiplied fill)."""
        if (image.width(), image.height()) != self.size:
            raise ValueError(f"image is {image.width()}x{image.height()}, DeckLink mode is "
                             f"{self.size[0]}x{self.size[1]}")
        if image.depth() != 32:
            raise ValueError("image must be 32-bit ARGB")
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        self.push_bytes(int(bits), image.bytesPerLine())

    def status(self) -> dict:
        if self._h is None:
            return {"running": False}
        f, l, d = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
        ref, w, h = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        self.lib.dsan_dl_status(self._h, ctypes.byref(f), ctypes.byref(l), ctypes.byref(d), ctypes.byref(ref),
                                ctypes.byref(w), ctypes.byref(h))
        r = ref.value
        reference = "n/a" if r < 0 or r & 1 else ("locked" if r & 2 else "unlocked")
        return {"running": True, "frames": f.value, "late": l.value, "dropped": d.value, "reference": reference}

    def close(self) -> None:
        if self._h is not None:
            self.lib.dsan_dl_close(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Feeder:
    """Renders a layout at the DeckLink resolution and hands frames to an output.

    Call ``tick()`` from a Qt timer. Renders at most ``max_fps`` (the card repeats
    the latest frame at its own rate in between).
    """

    def __init__(self, output: DeckLinkOutput, renderer, layout_source, frame_source,
                 premultiplied: bool = False, max_fps: float = 30.0):
        self.output = output
        self.renderer = renderer
        self.layout_source = layout_source
        self.frame_source = frame_source
        self.premultiplied = premultiplied
        self.interval_ms = max(10, int(1000 / min(max_fps, output.mode.fps or max_fps)))

    def render(self):
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QImage, QPainter
        w, h = self.output.size
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        self.renderer.paint(p, QRectF(0, 0, w, h), self.layout_source(), self.frame_source())
        p.end()
        if not self.premultiplied:
            img = img.convertToFormat(QImage.Format.Format_ARGB32)   # straight alpha, like the SDK sample
        return img

    def tick(self) -> None:
        self.output.push_qimage(self.render())


__all__ = ["DeckLinkDevice", "DeckLinkMode", "DeckLinkOutput", "DeckLinkUnavailable", "Feeder", "KEYING",
           "KEYING_LABELS", "available", "devices", "library_candidates", "library_name"]

"""Debug console for the Limitimer and Perfect Cue data links.

Device callbacks run on reader threads; they only append to a lock-protected
queue (``DebugTap``). The window drains it every 100 ms and appends in one
batch, so even a flood of bytes can't stall the UI. The log is capped.

Lines look like::

    12:08:31.245  LT  RAW    81 00 21 48 03 01 …
    12:08:31.245  LT  PKT    status · 55 bytes · CRC ok
    12:08:31.246  LT  ERR    CRC mismatch: expected 0x1A2B, got 0x0000 · 55 bytes
    12:08:31.246  LT  STATE  ↓ sel P1 │ P1 ▶ 00:59 (1/60, sum 20) │ P2 01:15 …
    12:08:32.010  PC  RAW    0F
    12:08:32.010  PC  BTN    NEXT (0x0F)
    12:08:32.400  PC  ???    0x55 — not a Perfect Cue button byte
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

from PyQt6.QtCore import QRegularExpression, QSettings, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import (QApplication, QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                             QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from .. import limitimer as lt
from .. import perfectcue as pc
from ..devices import Limitimer, PerfectCue
from . import files

MAX_LINES = 5000
MAX_QUEUE = 50000


def _ts(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


def _hex(data: bytes, limit: int = 96) -> str:
    text = " ".join(f"{b:02X}" for b in data[:limit])
    return text + (f" … (+{len(data) - limit})" if len(data) > limit else "")


class DebugTap:
    """Thread-safe collector. Attach devices; the window drains events."""

    def __init__(self):
        self._q: Deque[Tuple[float, str, object]] = deque(maxlen=MAX_QUEUE)
        self._lock = threading.Lock()
        self.enabled = False
        self.dropped = 0
        # always-on counters (cheap), for the stats panel
        self.lt_bytes = 0
        self.pc_bytes = 0
        self.pc_buttons = 0
        self.pc_unknown = 0
        self.pc_last: Optional[Tuple[float, str]] = None

    def _put(self, kind: str, obj) -> None:
        if not self.enabled:
            return
        with self._lock:
            if len(self._q) == self._q.maxlen:
                self.dropped += 1
            self._q.append((time.time(), kind, obj))

    def osc_in(self, address: str, args: tuple, result: str) -> None:
        """An OSC command received for the Perfect Cue (logged as a PC OSC line)."""
        self._put("pc-osc", (address, args, result))

    def millumin(self, address: str, args: tuple, used: bool) -> None:
        """An OSC message received from Millumin (logged as an MM line)."""
        self._put("mm-osc", (address, args, used))

    def drain(self) -> List[Tuple[float, str, object]]:
        with self._lock:
            out = list(self._q)
            self._q.clear()
        return out

    def reset_counters(self) -> None:
        self.lt_bytes = self.pc_bytes = self.pc_buttons = self.pc_unknown = 0
        self.pc_last = None
        self.dropped = 0

    def attach_limitimer(self, dev: Limitimer) -> None:
        def raw(data, dev=dev):
            self.lt_bytes += len(data)
            self._put("lt-raw", bytes(data))
        dev.on_raw(raw)
        dev.on_packet(lambda ev: self._put("lt-pkt", ev))
        dev.on_link(lambda link: self._put("lt-link", link.value))
        dev.on_error(lambda e: self._put("lt-error", str(e)))

    def attach_perfectcue(self, dev: PerfectCue) -> None:
        def raw(data):
            data = bytes(data)
            self.pc_bytes += len(data)
            for b in data:
                if b in pc._BUTTON_BYTES:
                    self.pc_buttons += 1
                    self.pc_last = (time.time(), pc.Button(b).name)
                else:
                    self.pc_unknown += 1
            self._put("pc-raw", data)
        dev.on_raw(raw)
        dev.on_link(lambda link: self._put("pc-link", link.value))
        dev.on_error(lambda e: self._put("pc-error", str(e)))


class _Highlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)

        def fmt(color, bold=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            return f
        self.rules = [
            (QRegularExpression(r"^\S+"), fmt("#5A5A5A")),                       # timestamp
            (QRegularExpression(r"\s(LT)\s"), fmt("#4AB0FF", True)),
            (QRegularExpression(r"\s(PC)\s"), fmt("#C08BFF", True)),
            (QRegularExpression(r"\sRAW\s.*$"), fmt("#7A7A7A")),
            (QRegularExpression(r"\sPKT\s.*$"), fmt("#00C46A")),
            (QRegularExpression(r"\sSTATE\s.*$"), fmt("#C8C8C8")),
            (QRegularExpression(r"\sBTN\s.*$"), fmt("#00E07A", True)),
            (QRegularExpression(r"\sLINK\s.*$"), fmt("#FFB000")),
            (QRegularExpression(r"\s(ERR|\?\?\?)\s.*$"), fmt("#FF5C5C", True)),
            (QRegularExpression(r"^──.*$"), fmt("#2EC4B6")),
        ]

    def highlightBlock(self, text):
        for rx, f in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)


class DebugWindow(QWidget):
    def __init__(self, tap: DebugTap, devices: Callable[[], Tuple[Optional[Limitimer], Optional[PerfectCue]]],
                 settings: Optional[QSettings] = None, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.tap = tap
        self.devices = devices
        self.settings = settings
        self.setWindowTitle("Confidence — Debug console")
        self.resize(1180, 640)
        self._last_state_key = None
        self._last_status: Optional[bytes] = None
        self._last_status_t = 0.0
        self._annot_dirty = False
        self._record = None
        self._record_path = ""
        self._rates = {"t": time.monotonic(), "lt_bytes": 0, "pkts": 0, "lt_bps": 0.0, "pps": 0.0}
        self._build()
        self._load()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flush)
        self._timer.start(100)
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(500)

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build(self):
        v = QVBoxLayout(self)
        bar = QHBoxLayout()

        def box(text, on, tip):
            c = QCheckBox(text)
            c.setChecked(on)
            c.setToolTip(tip)
            bar.addWidget(c)
            return c
        self.f_lt = box("Limitimer", True, "Show Limitimer lines")
        self.f_pc = box("Perfect Cue", True, "Show Perfect Cue lines")
        self.f_mm = box("Millumin", True, "Show OSC received from Millumin (time updates only with Raw bytes on)")
        bar.addSpacing(10)
        self.f_raw = box("Raw bytes", False, "Every chunk read from the serial port, in hex")
        self.f_pkt = box("Packets", True, "Every framed Limitimer packet: type, size, CRC result")
        self.f_ping = box("Pings", False, "Include Limitimer ping (0x10) packets")
        self.f_state = box("Decoded state", True, "The decoded Limitimer status")
        self.f_changes = box("Changes only", True, "Decoded state only when something other than the "
                                                    "sequence counter changes")
        bar.addStretch()
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Stop scrolling new lines (data is still counted)")
        bar.addWidget(self.pause_btn)
        for text, slot, tip in (("Mark", self._mark, "Insert a marker line, e.g. before pressing a button"),
                                ("Clear", self._clear, "Clear the log and counters"),
                                ("Copy", self._copy, "Copy the log to the clipboard"),
                                ("Save log…", self._save, "Save the log as a text file")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
        self.rec_btn = QPushButton("● Record raw…")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip("Write every raw byte from both ports, timestamped, to a capture file "
                                "(handy to send for protocol checks)")
        self.rec_btn.clicked.connect(self._toggle_record)
        bar.addWidget(self.rec_btn)
        v.addLayout(bar)
        for c in (self.f_lt, self.f_pc, self.f_mm, self.f_raw, self.f_pkt, self.f_ping, self.f_state, self.f_changes):
            c.toggled.connect(self._save_settings)

        split = QSplitter()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LINES)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSize(max(10, mono.pointSize()))
        self.log.setFont(mono)
        self.log.setStyleSheet("QPlainTextEdit { background: #0D0D0D; color: #C8C8C8; border: 1px solid #2A2A2A; }")
        self._hl = _Highlighter(self.log.document())
        split.addWidget(self.log)

        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(6, 0, 0, 0)
        stats_title = QLabel("LINK STATS")
        stats_title.setStyleSheet("color: #7A7A7A; font-size: 10px; font-weight: 600; letter-spacing: 1px;")
        sv.addWidget(stats_title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.stat = {}
        rows = (("lt_link", "Limitimer"), ("lt_rate", "  rate"), ("lt_counts", "  packets"),
                ("lt_errors", "  errors"), ("lt_last", "  last packet"),
                ("pc_link", "Perfect Cue"), ("pc_counts", "  bytes"), ("pc_last", "  last press"))
        for r, (key, label) in enumerate(rows):
            name = QLabel(label)
            name.setObjectName("dim")
            val = QLabel("—")
            val.setFont(mono)
            grid.addWidget(name, r, 0)
            grid.addWidget(val, r, 1)
            self.stat[key] = val
        grid.setColumnStretch(1, 1)
        sv.addLayout(grid)
        sv.addSpacing(8)
        annot_title = QLabel("LAST LIMITIMER STATUS PACKET")
        annot_title.setStyleSheet(stats_title.styleSheet())
        sv.addWidget(annot_title)
        self.annot = QTreeWidget()
        self.annot.setColumnCount(4)
        self.annot.setHeaderLabels(["Byte", "Hex", "Field", "Meaning"])
        self.annot.setRootIsDecorated(False)
        self.annot.setFont(mono)
        self.annot.setAlternatingRowColors(False)
        self.annot.setStyleSheet("QTreeWidget { background: #0D0D0D; border: 1px solid #2A2A2A; }")
        for i, w in enumerate((56, 80, 110)):
            self.annot.setColumnWidth(i, w)
        sv.addWidget(self.annot, 1)
        self.annot_note = QLabel("Waiting for a status packet…")
        self.annot_note.setObjectName("dim")
        sv.addWidget(self.annot_note)
        split.addWidget(side)
        split.setSizes([700, 480])
        v.addWidget(split, 1)

        self.footer = QLabel()
        self.footer.setObjectName("dim")
        v.addWidget(self.footer)

    # ── visibility drives the tap ──────────────────────────────────────────────

    def showEvent(self, e):
        self.tap.enabled = True
        super().showEvent(e)

    def hideEvent(self, e):
        self.tap.enabled = self._record is not None      # keep collecting only while recording
        super().hideEvent(e)

    # ── formatting ────────────────────────────────────────────────────────────

    def _state_line(self, st: lt.LimitimerState) -> str:
        parts = []
        for p in st.programs:
            mark = "▶" if p.running else " "
            flag = " WARN" if p.warning else (" OVER" if p.overtime else (" ZERO" if p.expired else ""))
            parts.append(f"P{p.number} {mark} {st.display(p.number):>7} ({p.elapsed}/{p.total}, sum {p.sumup}"
                         f"{', blink' if p.blink else ''}{', beep' if p.beep else ''}){flag}")
        return f"{'↓' if st.count_down else '↑'} sel P{st.selected_index + 1} seq {st.sequence} │ " + " │ ".join(parts)

    def _format(self, t: float, kind: str, obj) -> List[str]:
        ts = _ts(t)
        out: List[str] = []
        if kind.startswith("lt") and not self.f_lt.isChecked():
            return out
        if kind.startswith("pc") and not self.f_pc.isChecked():
            return out
        if kind == "lt-raw":
            if self.f_raw.isChecked():
                out.append(f"{ts}  LT  RAW    {_hex(obj)}")
        elif kind == "lt-pkt":
            ev: lt.PacketEvent = obj
            if not ev.ok:
                out.append(f"{ts}  LT  ERR    {ev.error} · {len(ev.raw)} bytes · {_hex(ev.raw, 64)}")
                return out
            if ev.type == lt.PING_MSG:
                if self.f_ping.isChecked() and self.f_pkt.isChecked():
                    out.append(f"{ts}  LT  PKT    ping · {len(ev.raw)} bytes · CRC "
                               f"{'0000 (accepted)' if ev.zero_crc else 'ok'}")
                return out
            if self.f_pkt.isChecked():
                kind_name = "status" if ev.type == lt.STATUS_MSG else f"type 0x{ev.type:02X}"
                out.append(f"{ts}  LT  PKT    {kind_name} · {len(ev.raw)} bytes · CRC "
                           f"{'0000 (accepted)' if ev.zero_crc else 'ok'}"
                           + ("" if len(ev.raw) == lt.STATUS_PACKET_LEN or ev.type != lt.STATUS_MSG
                              else f" · expected {lt.STATUS_PACKET_LEN}"))
            if ev.type == lt.STATUS_MSG:
                self._last_status = ev.payload
                self._last_status_t = t
                self._annot_dirty = True
                st = lt.parse_status(ev.payload)
                if st and self.f_state.isChecked():
                    key = dataclasses.replace(st, sequence=0)
                    if not self.f_changes.isChecked() or key != self._last_state_key:
                        out.append(f"{ts}  LT  STATE  {self._state_line(st)}")
                    self._last_state_key = key
        elif kind == "pc-raw":
            if self.f_raw.isChecked():
                out.append(f"{ts}  PC  RAW    {_hex(obj)}")
            for b in obj:
                if b in pc._BUTTON_BYTES:
                    out.append(f"{ts}  PC  BTN    {pc.Button(b).name} (0x{b:02X})")
                else:
                    out.append(f"{ts}  PC  ???    0x{b:02X} — not a Perfect Cue button byte "
                               "(A/B swapped or on the wrong pins, or not the Perfect Cue port?)")
        elif kind == "mm-osc":
            if not self.f_mm.isChecked():
                return out
            address, args, used = obj
            if address.endswith(("/media/time", "/media/normalizedTime")) and not self.f_raw.isChecked():
                return out                                   # many per second: only with Raw bytes
            shown = " ".join(repr(a) if isinstance(a, str) else
                             (f"{a:.3f}" if isinstance(a, float) else str(a)) for a in args)
            out.append(f"{ts}  MM  OSC    {address}{' ' + shown if shown else ''}"
                       + ("" if used else "   (not used)"))
        elif kind == "pc-osc":
            address, args, result = obj
            shown = " ".join(repr(a) if isinstance(a, str) else str(a) for a in args)
            out.append(f"{ts}  PC  OSC    {address}{' ' + shown if shown else ''} → {result}")
        elif kind in ("lt-link", "pc-link"):
            out.append(f"{ts}  {kind[:2].upper()}  LINK   {obj}")
        elif kind in ("lt-error", "pc-error"):
            out.append(f"{ts}  {kind[:2].upper()}  ERR    port: {obj}")
        return out

    # ── flushing ──────────────────────────────────────────────────────────────

    def _flush(self):
        events = self.tap.drain()
        if not events:
            self._refresh_annotation()
            return
        if self._record is not None:
            try:
                for t, kind, obj in events:
                    if kind in ("lt-raw", "pc-raw"):
                        self._record.write(f"{t:.6f} {kind[:2].upper()} {obj.hex(' ')}\n")
                self._record.flush()
            except OSError as e:
                self._stop_record(f"Recording stopped: {e}")
        for t, kind, obj in events:
            if kind == "lt-pkt" and getattr(obj, "ok", False):
                self._rates["pkts"] += 1
        if self.pause_btn.isChecked() or not self.isVisible():
            # still track the last status packet for the breakdown
            for t, kind, obj in events:
                if kind == "lt-pkt" and obj.ok and obj.type == lt.STATUS_MSG:
                    self._last_status, self._last_status_t, self._annot_dirty = obj.payload, t, True
            self._refresh_annotation()
            return
        lines: List[str] = []
        for t, kind, obj in events:
            lines += self._format(t, kind, obj)
        if lines:
            bar = self.log.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 4
            self.log.appendPlainText("\n".join(lines))
            if at_bottom:
                bar.setValue(bar.maximum())
        self._refresh_annotation()

    def _refresh_annotation(self):
        if not self._annot_dirty or self._last_status is None:
            return
        self._annot_dirty = False
        rows = lt.annotate_status(self._last_status)
        if self.annot.topLevelItemCount() != len(rows):
            self.annot.clear()
            for r in rows:
                self.annot.addTopLevelItem(QTreeWidgetItem(list(r)))
        else:
            for i, r in enumerate(rows):
                item = self.annot.topLevelItem(i)
                for col, val in enumerate(r):
                    if item.text(col) != val:
                        item.setText(col, val)
                        item.setForeground(col, QColor("#FFB000"))      # changed since last packet
                    else:
                        item.setForeground(col, QColor("#C8C8C8"))
        self.annot_note.setText(f"Received {_ts(self._last_status_t)} · amber = changed since the previous packet")

    def _update_stats(self):
        ltd, pcd = self.devices()
        now = time.monotonic()
        r = self._rates
        dt = now - r["t"]
        if dt >= 1.0:
            r["lt_bps"] = (self.tap.lt_bytes - r["lt_bytes"]) / dt
            r["pps"] = r["pkts"] / dt
            r["lt_bytes"], r["pkts"], r["t"] = self.tap.lt_bytes, 0, now
        s = self.stat
        if ltd is None:
            for k in ("lt_link", "lt_rate", "lt_counts", "lt_errors", "lt_last"):
                s[k].setText("not connected" if k == "lt_link" else "—")
        else:
            st = ltd.stats
            s["lt_link"].setText(f"{ltd.link.value} · {os.path.basename(ltd.port or 'demo')} @ {ltd.baudrate}")
            s["lt_rate"].setText(f"{r['pps']:.1f} packets/s · {r['lt_bps']:.0f} bytes/s")
            s["lt_counts"].setText(f"{st['packets']} ok ({st['status']} status, {st['ping']} ping)")
            s["lt_errors"].setText(f"{st['errors']} bad · {st['zero_crc']} zero-CRC")
            s["lt_errors"].setStyleSheet("color: #FF5C5C;" if st["errors"] else "")
            s["lt_last"].setText(f"{now - ltd.last_rx:.1f} s ago" if ltd.last_rx else "none yet")
        if pcd is None:
            s["pc_link"].setText("not connected")
            s["pc_counts"].setText("—")
            s["pc_last"].setText("—")
        else:
            s["pc_link"].setText(f"{pcd.link.value} · {os.path.basename(pcd.port or 'demo')} @ {pcd.baudrate}")
            s["pc_counts"].setText(f"{self.tap.pc_bytes} · {self.tap.pc_buttons} presses · "
                                   f"{self.tap.pc_unknown} unknown")
            s["pc_counts"].setStyleSheet("color: #FF5C5C;" if self.tap.pc_unknown else "")
            last = self.tap.pc_last
            s["pc_last"].setText(f"{last[1]} · {time.time() - last[0]:.1f} s ago" if last else "none yet")
        rec = f" · recording to {os.path.basename(self._record_path)}" if self._record else ""
        drop = f" · {self.tap.dropped} events dropped (UI too slow)" if self.tap.dropped else ""
        self.footer.setText(f"{self.log.blockCount()} lines (keeps the last {MAX_LINES}){rec}{drop}")

    # ── actions ───────────────────────────────────────────────────────────────

    def _mark(self):
        self.log.appendPlainText(f"── mark {_ts(time.time())} " + "─" * 40)

    def _clear(self):
        self.log.clear()
        self.tap.reset_counters()
        ltd, _ = self.devices()
        if ltd:
            ltd.reset_stats()
        self._last_state_key = None

    def _copy(self):
        QApplication.clipboard().setText(self.log.toPlainText())

    def _save(self):
        name = f"dsan-debug-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt"
        path = files.save_path(self, "Save debug log", name, "Text (*.txt)", "debug", suffix=".txt")
        if path:
            self.save_log(path)

    def save_log(self, path: str) -> None:
        from .. import version as ver
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Confidence {ver.version_string()} debug log, saved {_dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write(self.log.toPlainText() + "\n")

    def _toggle_record(self, on: bool):
        if not on:
            self._stop_record()
            return
        name = f"dsan-capture-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt"
        path = files.save_path(self, "Record raw bytes to", name, "Capture (*.txt)", "debug", suffix=".txt")
        if not path:
            self.rec_btn.setChecked(False)
            return
        self.start_record(path)

    def start_record(self, path: str) -> None:
        self._record = open(path, "w", encoding="utf-8")
        self._record_path = path
        from .. import version as ver
        self._record.write(f"# Confidence {ver.version_string()} raw capture: <unix time> <LT|PC> <hex bytes>\n")
        self.tap.enabled = True
        self.rec_btn.setChecked(True)
        self.rec_btn.setText("■ Stop recording")
        self.log.appendPlainText(f"── recording raw bytes to {path} " + "─" * 20)

    def _stop_record(self, message: str = ""):
        if self._record is not None:
            try:
                self._record.close()
            except OSError:
                pass
            self.log.appendPlainText(f"── {message or 'recording saved: ' + self._record_path} " + "─" * 20)
        self._record = None
        self.rec_btn.setChecked(False)
        self.rec_btn.setText("● Record raw…")
        self.tap.enabled = self.isVisible()

    # ── settings ──────────────────────────────────────────────────────────────

    _KEYS = ("f_lt", "f_pc", "f_mm", "f_raw", "f_pkt", "f_ping", "f_state", "f_changes")

    def _load(self):
        if not self.settings:
            return
        for k in self._KEYS:
            w = getattr(self, k)
            w.setChecked(self.settings.value(f"debug/{k}", w.isChecked(), type=bool))
        geo = self.settings.value("debug/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def _save_settings(self, *_):
        if not self.settings:
            return
        for k in self._KEYS:
            self.settings.setValue(f"debug/{k}", getattr(self, k).isChecked())

    def closeEvent(self, e):
        if self.settings:
            self.settings.setValue("debug/geometry", self.saveGeometry())
        super().closeEvent(e)

    def shutdown(self):
        self._stop_record()
        self.close()


__all__ = ["DebugTap", "DebugWindow"]

"""DSAN Limitimer Viewer — a PyQt6 desktop app built on the ``dsan`` library.

    pip install 'dsan[viewer]'
    dsan-viewer

All protocol, serial, reconnect, link-watchdog and OSC work is done by the
library; this file is only UI. Library callbacks arrive on reader threads and are
re-emitted as Qt signals, which Qt queues onto the GUI thread.
"""
from __future__ import annotations

import sys
from typing import List, Optional

from PyQt6.QtCore import QObject, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
    QStatusBar, QTextEdit, QVBoxLayout, QWidget,
)

from . import __version__
from . import perfectcue as pc
from .devices import Limitimer, LinkState, PerfectCue
from .limitimer import LimitimerState, PacketEvent, Program

try:
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    list_ports = None

try:
    from .osc import OscBridge
    OSC_AVAILABLE = True
except ImportError:  # pragma: no cover
    OSC_AVAILABLE = False

DEBUG_MAX_LINES = 5000
MONO = "JetBrains Mono"


# ── Library → Qt signal bridge ──────────────────────────────────────────────────

class DeviceSignals(QObject):
    """Re-emits a device's callbacks as Qt signals (safe across threads)."""
    state = pyqtSignal(object)
    link = pyqtSignal(object)
    packet = pyqtSignal(object)
    raw = pyqtSignal(bytes)
    error = pyqtSignal(str)
    button = pyqtSignal(object)

    def attach(self, device) -> None:
        device.on_link(self.link.emit)
        device.on_raw(self.raw.emit)
        device.on_error(lambda e: self.error.emit(str(e)))
        if isinstance(device, Limitimer):
            device.on_state(self.state.emit)
            device.on_packet(self.packet.emit)
        if isinstance(device, PerfectCue):
            device.on_button(self.button.emit)


def list_serial_ports() -> List[tuple]:
    if list_ports is None:
        return []
    return [(p.device, p.description) for p in list_ports.comports()]


# ── Widgets ─────────────────────────────────────────────────────────────────────

class FitLabel(QLabel):
    """Time display whose font scales to fill the label, so digits are never clipped.
    Sizing uses the text with every digit replaced by '8' so it doesn't jump."""

    _MARGIN_X = 36
    _MARGIN_Y = 24
    _MAX_PX = 160

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._px = 0

    def minimumSizeHint(self) -> QSize:
        return QSize(160, 84)

    def sizeHint(self) -> QSize:
        return QSize(320, 120)

    def setText(self, text: str):
        if text != self.text():
            super().setText(text)
            self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        avail_w = max(10, self.width() - self._MARGIN_X)
        avail_h = max(10, self.height() - self._MARGIN_Y)
        sample = "".join("8" if c.isdigit() else c for c in (self.text() or "00:00")).rjust(5, "8")
        f = QFont(self.font())
        f.setPixelSize(100)
        fm = QFontMetrics(f)
        digit_h = fm.tightBoundingRect(sample).height()
        scale = min(avail_w / max(1, fm.horizontalAdvance(sample)), avail_h / max(1, digit_h))
        px = max(12, min(self._MAX_PX, int(100 * scale)))
        if px != self._px:
            self._px = px
            f.setPixelSize(px)
            super().setFont(f)


_TIME_STYLE = """
    color: {color};
    background: #0a0a0a;
    border: 2px {border_style} {border};
    border-radius: 8px;
    padding: 4px 12px;
"""


class TimerWidget(QFrame):
    """One program's display."""

    def __init__(self, number: int, parent=None):
        super().__init__(parent)
        self.number = number
        self.blink_visible = True
        self.blink_timer = QTimer()
        self.blink_timer.timeout.connect(self.toggle_blink)
        self.is_blinking = False
        self.stale = False
        self.current_style = _TIME_STYLE.format(color="#888888", border="#333", border_style="solid")
        self._build()

    def _build(self):
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.setStyleSheet("""
            TimerWidget {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2a2a2a, stop:1 #1a1a1a);
                border: 2px solid #444; border-radius: 12px; padding: 12px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = QLabel(f"PROGRAM {self.number}")
        self.title_label.setFont(QFont(MONO, 11, QFont.Weight.Bold))
        self.title_label.setStyleSheet("color: #888; letter-spacing: 2px;")
        header.addWidget(self.title_label)
        self.status_indicator = QLabel("●")
        self.status_indicator.setFont(QFont("Arial", 14))
        self.status_indicator.setStyleSheet("color: #444;")
        header.addWidget(self.status_indicator, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addLayout(header)

        self.time_label = FitLabel("00:00")
        self.time_label.setFont(QFont(MONO, 52, QFont.Weight.Bold))
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setStyleSheet(self.current_style)
        layout.addWidget(self.time_label)

        info = QHBoxLayout()
        self.total_label = self._small("Total: --:--", info)
        self.sumup_label = self._small("Sum-up: --:--", info)
        layout.addLayout(info)

        flags = QHBoxLayout()
        flags.setSpacing(12)
        self.run_flag = self._flag("RUN", flags)
        self.blink_flag = self._flag("BLINK", flags)
        self.beep_flag = self._flag("BEEP", flags)
        self.warning_flag = self._flag("WARN", flags)
        self.expired_flag = self._flag("EXPIRED", flags)
        flags.addStretch()
        self.overtime_checkbox = QCheckBox("Overtime")
        self.overtime_checkbox.setFont(QFont(MONO, 9))
        self.overtime_checkbox.setToolTip(
            "Checked: keep counting past zero/total (countdown shows +MM:SS)\n"
            "Unchecked: countdown holds at 00:00, count-up holds at total")
        flags.addWidget(self.overtime_checkbox)
        layout.addLayout(flags)

    @staticmethod
    def _small(text: str, layout) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont(MONO, 9))
        lbl.setStyleSheet("color: #666;")
        layout.addWidget(lbl)
        return lbl

    @staticmethod
    def _flag(text: str, layout) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont(MONO, 9, QFont.Weight.Bold))
        lbl.setStyleSheet("color: #333;")
        layout.addWidget(lbl)
        return lbl

    def toggle_blink(self):
        self.blink_visible = not self.blink_visible
        self.time_label.setStyleSheet(
            self.current_style if self.blink_visible
            else _TIME_STYLE.format(color="#1a1a1a", border="#333", border_style="solid"))

    def _stop_blink(self):
        if self.is_blinking:
            self.is_blinking = False
            self.blink_timer.stop()
        self.blink_visible = True

    def set_stale(self, stale: bool):
        """Mark the display as not live (cable pulled, device off, port lost)."""
        if stale == self.stale:
            return
        self.stale = stale
        if stale:
            self._stop_blink()
            self.title_label.setText(f"PROGRAM {self.number} · NO SIGNAL")
            self.title_label.setStyleSheet("color: #ff4444; letter-spacing: 2px;")
            self.time_label.setStyleSheet(
                _TIME_STYLE.format(color="#3a3a3a", border="#ff4444", border_style="dashed"))
        else:
            self.title_label.setText(f"PROGRAM {self.number}")
            self.title_label.setStyleSheet("color: #888; letter-spacing: 2px;")
            self.time_label.setStyleSheet(self.current_style)

    @property
    def overtime(self) -> bool:
        return self.overtime_checkbox.isChecked()

    def update_timer(self, state: LimitimerState, follow_format: bool):
        p: Program = state.program(self.number)
        self.time_label.setText(state.display(self.number, self.overtime, follow_format))

        mode = "↓" if state.count_down else "↑"
        self.total_label.setText(f"Total: {p.total // 60}:{p.total % 60:02d} {mode}")
        if p.sumup > 0:
            self.sumup_label.setText(f"Sum-up: {p.sumup // 60}:{p.sumup % 60:02d}")
            self.sumup_label.setStyleSheet("color: #ffaa00;")
        else:
            self.sumup_label.setText("Sum-up: --")
            self.sumup_label.setStyleSheet("color: #444;")

        if p.expired:
            color, border = "#ff3333", "#ff3333"
        elif p.warning:
            color, border = "#ffaa00", "#ffaa00"
        elif p.running:
            color, border = "#00ff88", "#333"
        elif state.display_seconds(self.number, self.overtime) > 0:
            color, border = "#4488ff", "#333"
        else:
            color, border = "#888888", "#333"
        self.current_style = _TIME_STYLE.format(color=color, border=border, border_style="solid")

        if p.blink:
            if not self.is_blinking:
                self.is_blinking = True
                self.blink_timer.start(500)
        else:
            self._stop_blink()
        if self.blink_visible and not self.stale:
            self.time_label.setStyleSheet(self.current_style)

        selected = state.selected_index + 1 == self.number
        self.status_indicator.setStyleSheet("color: #00ff88;" if selected else "color: #444;")
        for lbl, on, c in ((self.run_flag, p.running, "#00ff88"), (self.blink_flag, p.blink, "#ffaa00"),
                           (self.beep_flag, p.beep, "#ff3333"), (self.warning_flag, p.warning, "#ffaa00"),
                           (self.expired_flag, p.expired, "#ff3333")):
            lbl.setStyleSheet(f"color: {c if on else '#333'};")


class PerfectCueWidget(QGroupBox):
    """Perfect Cue connection, last received button, and command buttons."""

    send_command = pyqtSignal(object)   # pc.Command

    _BUTTON_META = {
        pc.Button.PREV: ("◁  PREV", "#dd2222"),
        pc.Button.NEXT: ("▷  NEXT", "#00cc44"),
        pc.Button.BLANK_ON: ("⊘  BLANK ON", "#ffcc00"),
        pc.Button.BLANK_OFF: ("⊘  BLANK OFF", "#888888"),
    }
    _IDLE = "color: #333; background: #111; border: 2px solid #2a2a2a; border-radius: 6px; padding: 6px 10px;"

    def __init__(self, parent=None):
        super().__init__("Perfect Cue", parent)
        self._clear_timer = QTimer()
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self._clear_button)
        self._build()

    def _build(self):
        self.setStyleSheet("""
            QGroupBox { color: #888; border: 1px solid #333; border-radius: 8px;
                        margin-top: 12px; padding-top: 12px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 8px; }
        """)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(180)
        self.port_combo.setEditable(True)
        self.port_combo.setToolTip("Serial port, or socket://host:port for an RS-232/485-to-Ethernet converter")
        row.addWidget(self.port_combo)
        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setFixedWidth(32)
        self.refresh_btn.clicked.connect(self.refresh_ports)
        row.addWidget(self.refresh_btn)
        lbl = QLabel("Baud:")
        lbl.setStyleSheet("color: #666;")
        row.addWidget(lbl)
        self.baud_combo = QComboBox()
        self.baud_combo.setFixedWidth(90)
        for rate in (9600, 19200, 38400, 57600, 115200):
            self.baud_combo.addItem(str(rate), rate)
        self.baud_combo.setCurrentIndex(self.baud_combo.findData(pc.BAUDRATE))
        self.baud_combo.setToolTip("Perfect Cue receiver ASCII API is 9600-8-N-1")
        row.addWidget(self.baud_combo)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedWidth(120)
        row.addWidget(self.connect_btn)
        self.status_label = QLabel("Not connected")
        self.status_label.setFont(QFont(MONO, 9))
        self.status_label.setStyleSheet("color: #666;")
        row.addWidget(self.status_label)
        row.addStretch()
        outer.addLayout(row)

        ind = QHBoxLayout()
        ind.setSpacing(12)
        lbl = QLabel("Last:")
        lbl.setStyleSheet("color: #666;")
        ind.addWidget(lbl)
        self._btn_labels = {}
        for btn, (text, _) in self._BUTTON_META.items():
            l = QLabel(text)
            l.setFont(QFont(MONO, 11, QFont.Weight.Bold))
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setMinimumWidth(110)
            l.setStyleSheet(self._IDLE)
            self._btn_labels[btn] = l
            ind.addWidget(l)
        ind.addStretch()
        outer.addLayout(ind)

        send = QHBoxLayout()
        send.setSpacing(8)
        lbl = QLabel("Send:")
        lbl.setStyleSheet("color: #666;")
        send.addWidget(lbl)
        self._cmd_btns: List[QPushButton] = []
        for label, cmd, color in (("◁ Prev", pc.Command.REVERSE, "#dd2222"),
                                  ("▷ Next", pc.Command.FORWARD, "#00cc44"),
                                  ("⊘ Blank ON", pc.Command.BLANK_ON, "#ffcc00"),
                                  ("⊘ Blank OFF", pc.Command.BLANK_OFF, "#888888")):
            b = QPushButton(label)
            b.setEnabled(False)
            b.setStyleSheet(
                f"QPushButton {{ background:#2a2a2a; color:{color}; border:1px solid #444; "
                f"border-radius:4px; padding:5px 12px; font-weight:bold; }}"
                f"QPushButton:hover {{ background:#3a3a3a; border:1px solid #666; }}"
                f"QPushButton:pressed {{ background:#1a1a1a; border:1px solid {color}; }}"
                f"QPushButton:disabled {{ color:#444; background:#1e1e1e; border:1px solid #333; }}")
            b.clicked.connect(lambda _=False, c=cmd: self.send_command.emit(c))
            send.addWidget(b)
            self._cmd_btns.append(b)
        send.addStretch()
        outer.addLayout(send)
        self.refresh_ports()

    def refresh_ports(self):
        current = self.port_combo.currentData() or self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItem("Select port...", None)
        for dev, desc in list_serial_ports():
            self.port_combo.addItem(f"{dev} - {desc}", dev)
        self.port_combo.addItem("Simulated Perfect Cue", "sim")
        idx = self.port_combo.findData(current)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)

    def selected_port(self) -> Optional[str]:
        data = self.port_combo.currentData()
        if data:
            return data
        text = self.port_combo.currentText().strip()
        return text if text and text != "Select port..." else None

    def set_link(self, link: LinkState, port: str = ""):
        busy = link != LinkState.CLOSED
        self.connect_btn.setText("Disconnect" if busy else "Connect")
        for w in (self.port_combo, self.refresh_btn, self.baud_combo):
            w.setEnabled(not busy)
        for b in self._cmd_btns:
            b.setEnabled(link == LinkState.OPEN)
        text, color = {
            LinkState.CLOSED: ("Not connected", "#666"),
            LinkState.OPENING: ("Opening…", "#ffaa00"),
            LinkState.RECONNECTING: ("Lost port — reconnecting…", "#ffaa00"),
            LinkState.OPEN: (f"Connected: {port}", "#00ff88"),
        }.get(link, (link.value, "#888"))
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    def show_button(self, btn: pc.Button, auto_clear_ms: int = 2000):
        for b, lbl in self._btn_labels.items():
            if b == btn:
                c = self._BUTTON_META[b][1]
                lbl.setStyleSheet(f"color: {c}; background: #1a1a1a; border: 2px solid {c}; "
                                  f"border-radius: 6px; padding: 6px 10px;")
            else:
                lbl.setStyleSheet(self._IDLE)
        self._clear_timer.start(auto_clear_ms)

    def _clear_button(self):
        for lbl in self._btn_labels.values():
            lbl.setStyleSheet(self._IDLE)


class OscAddressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OSC Address Reference")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.Tool)
        self.resize(660, 600)
        self.setStyleSheet("""
            QDialog { background: #141414; } QLabel { color: #aaa; }
            QTextEdit { background: #0d0d0d; color: #c8c8c8; border: 1px solid #2a2a2a; border-radius: 4px; }
            QPushButton { background: #2e2e2e; color: #bbb; border: 1px solid #444; border-radius: 4px; padding: 4px 18px; }
        """)
        layout = QVBoxLayout(self)
        self._note = QLabel()
        self._note.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self._note)
        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setFont(QFont(MONO, 10))
        layout.addWidget(self._view)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)

    def refresh(self, prefix: str, bundled: bool):
        mode = "one OSC bundle per state change" if bundled else "individual messages"
        self._note.setText(f'Prefix "{prefix}" · sent as {mode} — change in the OSC Output panel.')

        def section(t):
            return (f"<tr><td colspan='3' style='padding:10px 0 4px 0;color:#00aa55;font-weight:bold;"
                    f"border-bottom:1px solid #2a2a2a;'>{t}</td></tr>")

        def row(a, t, d):
            return (f"<tr><td style='padding:3px 14px 3px 0;color:#7ec8e3;white-space:nowrap;'>{a}</td>"
                    f"<td style='padding:3px 14px 3px 0;color:#888;'>{t}</td><td style='color:#aaa;'>{d}</td></tr>")

        p, t = prefix, f"{prefix}/timer/N"
        rows = [
            section("Link"),
            row(f"{p}/link", "bool", "True = live Limitimer data; False = no signal or port lost"),
            section("Global (on every state change)"),
            row(f"{p}/countdown", "bool", "True = countdown, False = count-up"),
            row(f"{p}/selected", "int", "Selected program (1–4)"),
            section("Per program (N = 1–4)"),
            row(f"{t}/time", "string", "Display string as shown, '+' prefix when over"),
            row(f"{t}/seconds", "int", "Display seconds (unsigned)"),
            row(f"{t}/remaining", "int", "Signed seconds left (negative when over)"),
            row(f"{t}/overtime", "bool", "Past zero"),
            row(f"{t}/total", "int", "Total (seconds)"),
            row(f"{t}/sumup", "int", "Sum-up threshold (seconds)"),
            row(f"{t}/elapsed", "int", "Elapsed (seconds)"),
            row(f"{t}/running", "bool", "Running"),
            row(f"{t}/warning", "bool", "In sum-up period"),
            row(f"{t}/expired", "bool", "Reached zero"),
            row(f"{t}/blink", "bool", "Display blinking"),
            row(f"{t}/color", "int", "0 normal · 1 warning · 2 expired"),
            section("Perfect Cue (on button press)"),
            row(f"{p}/perfectcue/button", "string", "NEXT · PREV · BLANK_ON · BLANK_OFF"),
            row(f"{p}/perfectcue/value", "int", "0x0F · 0x1F · 0x3F · 0x2F"),
        ]
        self._view.setHtml("<html><body style='background:#0d0d0d;'><table style='border-collapse:collapse;"
                           "width:100%;font-size:10px;'>" + "".join(rows) + "</table></body></html>")


class DebugDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Debug Console")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.Tool)
        self.resize(860, 380)
        self.setStyleSheet("""
            QDialog { background: #141414; } QLabel { color: #aaa; } QCheckBox { color: #aaa; spacing: 6px; }
            QPushButton { background: #2e2e2e; color: #bbb; border: 1px solid #444; border-radius: 4px; padding: 4px 14px; }
        """)
        layout = QVBoxLayout(self)
        ctrl = QHBoxLayout()
        ctrl.setSpacing(16)

        def cb(text, on, tip=None):
            c = QCheckBox(text)
            c.setChecked(on)
            if tip:
                c.setToolTip(tip)
            ctrl.addWidget(c)
            return c

        self.enable_checkbox = cb("Enable", True)
        self.errors_checkbox = cb("Errors", True)
        self.changes_only_checkbox = cb("Changes Only", True)
        self.raw_checkbox = cb("Raw Bytes", False)
        self.zero_crc_checkbox = cb("Accept zero CRC", True,
                                    "Go reference behaviour: a 0x0000 checksum means 'CRC disabled'.\n"
                                    "Uncheck to reject those packets.")
        ctrl.addStretch()
        self.stats_label = QLabel("Packets: 0 | Errors: 0")
        self.stats_label.setFont(QFont(MONO, 9))
        ctrl.addWidget(self.stats_label)
        self.clear_btn = QPushButton("Clear")
        ctrl.addWidget(self.clear_btn)
        layout.addLayout(ctrl)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(DEBUG_MAX_LINES)
        self.text.setFont(QFont(MONO, 9))
        self.text.setStyleSheet("QPlainTextEdit { background:#0d0d0d; color:#00cc66; border:1px solid #2a2a2a; }")
        layout.addWidget(self.text)

    def log(self, html: str):
        if not self.enable_checkbox.isChecked():
            return
        sb = self.text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.text.appendHtml(html)
        if at_bottom:
            sb.setValue(sb.maximum())

    def closeEvent(self, event):
        parent = self.parent()
        if parent is not None and hasattr(parent, "debug_action"):
            parent.debug_action.setChecked(False)
        super().closeEvent(event)


# ── Main window ─────────────────────────────────────────────────────────────────

_LINK_LABEL = {
    LinkState.CLOSED: ("● NO PORT", "#555"),
    LinkState.OPENING: ("● OPENING", "#ffaa00"),
    LinkState.RECONNECTING: ("● PORT LOST", "#ff4444"),
    LinkState.WAITING: ("● WAITING", "#ffaa00"),
    LinkState.LIVE: ("● LIVE", "#00ff88"),
    LinkState.STALE: ("● NO SIGNAL", "#ff4444"),
}

_APP_STYLE = """
    QMainWindow { background: #181818; }
    QWidget, QLabel { color: #c8c8c8; }
    QComboBox { background: #252525; color: #c8c8c8; border: 1px solid #3a3a3a; border-radius: 5px;
                padding: 5px 10px; min-width: 180px; }
    QComboBox::drop-down { border: none; }
    QComboBox QAbstractItemView { background: #252525; color: #c8c8c8; selection-background-color: #3a3a3a; }
    QPushButton { background: #2e2e2e; color: #c8c8c8; border: 1px solid #444; border-radius: 5px;
                  padding: 6px 18px; font-weight: 600; }
    QPushButton:hover { background: #383838; border-color: #5a5a5a; }
    QPushButton:pressed { background: #222; }
    QPushButton:disabled { background: #222; color: #484848; border-color: #333; }
    QGroupBox { color: #777; border: 1px solid #2e2e2e; border-radius: 7px; margin-top: 10px;
                padding-top: 10px; font-size: 10px; font-weight: 600; letter-spacing: 1px; }
    QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
    QStatusBar { background: #111; color: #666; font-size: 11px; }
    QMenuBar { background: #111; color: #999; } QMenu { background: #1e1e1e; color: #ccc; }
    QCheckBox { color: #999; spacing: 6px; }
    QLineEdit, QSpinBox { background: #252525; color: #c8c8c8; border: 1px solid #3a3a3a;
                          border-radius: 4px; padding: 4px 8px; }
    QLineEdit:disabled, QSpinBox:disabled { color: #555; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("TRUCUE", "LimitimerViewer")
        self.limitimer: Optional[Limitimer] = None
        self.perfectcue: Optional[PerfectCue] = None
        self.demo = False
        self.current_state: Optional[LimitimerState] = None
        self.last_debug_key = None
        self.osc_bridge = None

        self.lt_signals = DeviceSignals()
        self.lt_signals.state.connect(self.on_state)
        self.lt_signals.link.connect(self.on_lt_link)
        self.lt_signals.packet.connect(self.on_packet)
        self.lt_signals.raw.connect(self.on_raw)
        self.lt_signals.error.connect(lambda e: self._log_error("LIMITIMER", e))

        self.pc_signals = DeviceSignals()
        self.pc_signals.button.connect(self.on_button)
        self.pc_signals.link.connect(self.on_pc_link)
        self.pc_signals.raw.connect(lambda d: self.dbg.log(
            f"<span style='color:#888;'>PC RX: {' '.join(f'0x{b:02X}' for b in d)}</span>"))
        self.pc_signals.error.connect(lambda e: self._log_error("PERFECT CUE", e))

        self._build()
        self.dbg = DebugDialog(self)
        self.dbg.clear_btn.clicked.connect(self.clear_debug)
        self.dbg.zero_crc_checkbox.toggled.connect(self._apply_zero_crc)
        self.osc_ref = OscAddressDialog(self)

        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(self._update_stats)
        self.stats_timer.start(500)
        self.load_settings()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self):
        self.setWindowTitle(f"DSAN Limitimer Viewer {__version__}")
        self.setMinimumWidth(920)
        self.setStyleSheet(_APP_STYLE)

        view = self.menuBar().addMenu("View")
        self.debug_action = view.addAction("Debug Console")
        self.debug_action.setCheckable(True)
        self.debug_action.triggered.connect(lambda on: self.dbg.show() if on else self.dbg.hide())
        view.addAction("OSC Address List").triggered.connect(self._show_osc_reference)
        view.addSeparator()

        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setSpacing(12)
        main.setContentsMargins(16, 12, 16, 12)

        header = QHBoxLayout()
        header.setSpacing(12)
        title = QLabel("Limitimer Viewer")
        title.setFont(QFont(MONO, 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #00bb66;")
        header.addWidget(title)
        header.addSpacing(16)
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setToolTip("Serial port, or socket://host:port for an RS-485-to-Ethernet converter")
        header.addWidget(self.port_combo)
        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setFixedWidth(34)
        self.refresh_btn.clicked.connect(self.refresh_ports)
        header.addWidget(self.refresh_btn)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedWidth(110)
        self.connect_btn.clicked.connect(self.toggle_connection)
        header.addWidget(self.connect_btn)
        self.demo_btn = QPushButton("Demo")
        self.demo_btn.setFixedWidth(110)
        self.demo_btn.setToolTip("Run the built-in show simulator through the real decoder")
        self.demo_btn.clicked.connect(self.toggle_demo)
        header.addWidget(self.demo_btn)
        header.addStretch()
        self.link_label = QLabel()
        self.link_label.setFont(QFont(MONO, 10, QFont.Weight.Bold))
        header.addWidget(self.link_label)
        main.addLayout(header)
        self._show_link(LinkState.CLOSED)

        status = QGroupBox("SYSTEM STATUS")
        sl = QHBoxLayout(status)
        sl.setSpacing(24)
        self.direction_label = QLabel("Direction: --")
        self.beep_label = QLabel("Beep: --")
        self.selected_label = QLabel("Selected: --")
        self.sequence_label = QLabel("Seq: --")
        for l in (self.direction_label, self.beep_label, self.selected_label, self.sequence_label):
            l.setFont(QFont(MONO, 10))
            sl.addWidget(l)
        sl.addStretch()
        self.follow_format_checkbox = QCheckBox("Follow DIP 2/3 time format")
        self.follow_format_checkbox.setToolTip(
            "On: MM:SS or HH:MM exactly as the Limitimer DIP switches set it.\n"
            "Off: MM:SS under an hour, HH:MM:SS above (always shows seconds).")
        self.follow_format_checkbox.toggled.connect(self._display_options_changed)
        sl.addWidget(self.follow_format_checkbox)
        main.addWidget(status)

        grid = QGridLayout()
        grid.setSpacing(16)
        self.timer_widgets: List[TimerWidget] = []
        for i in range(4):
            w = TimerWidget(i + 1)
            w.overtime_checkbox.toggled.connect(self._display_options_changed)
            self.timer_widgets.append(w)
            grid.addWidget(w, i // 2, i % 2)
        main.addLayout(grid, 1)

        dip = QGroupBox("DIP SWITCH SETTINGS")
        dl = QHBoxLayout(dip)
        self.dip_labels = {}
        for key, text in (("sw1", "1:Mode"), ("sw2", "2:P4 Fmt"), ("sw3", "3:P1-3 Fmt"),
                          ("sw4", "4:Direction"), ("sw5", "5:Behavior"), ("sw6", "6:Volume"),
                          ("sw78", "7+8:Sound"), ("sw9", "9:Changes"), ("sw10", "10:RF")):
            col = QVBoxLayout()
            t = QLabel(text)
            t.setFont(QFont(MONO, 8))
            t.setStyleSheet("color: #666;")
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v = QLabel("--")
            v.setFont(QFont(MONO, 9, QFont.Weight.Bold))
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.setMinimumWidth(70)
            col.addWidget(t)
            col.addWidget(v)
            dl.addLayout(col)
            self.dip_labels[key] = v
        for key in ("sw1", "sw10"):
            self.dip_labels[key].setToolTip("Not reported in the status message")
        dl.addStretch()
        main.addWidget(dip)

        self.pc_widget = PerfectCueWidget()
        self.pc_widget.connect_btn.clicked.connect(self.toggle_pc_connection)
        self.pc_widget.send_command.connect(self.send_pc_command)
        main.addWidget(self.pc_widget)

        osc = QGroupBox("OSC OUTPUT")
        ol = QHBoxLayout(osc)
        ol.setSpacing(10)
        self.osc_enabled_checkbox = QCheckBox("Enable OSC")
        self.osc_enabled_checkbox.toggled.connect(self.toggle_osc)
        ol.addWidget(self.osc_enabled_checkbox)
        ol.addWidget(QLabel("IP:"))
        self.osc_ip_edit = QLineEdit("127.0.0.1")
        self.osc_ip_edit.setFixedWidth(120)
        ol.addWidget(self.osc_ip_edit)
        ol.addWidget(QLabel("Port:"))
        self.osc_port_spin = QSpinBox()
        self.osc_port_spin.setRange(1, 65535)
        self.osc_port_spin.setValue(5000)
        ol.addWidget(self.osc_port_spin)
        ol.addWidget(QLabel("Prefix:"))
        self.osc_prefix_edit = QLineEdit("/dsan")
        self.osc_prefix_edit.setFixedWidth(100)
        ol.addWidget(self.osc_prefix_edit)
        self.osc_bundle_checkbox = QCheckBox("Bundle")
        self.osc_bundle_checkbox.setChecked(True)
        self.osc_bundle_checkbox.setToolTip("One OSC bundle per state change. Uncheck for receivers "
                                            "that don't handle bundles.")
        self.osc_bundle_checkbox.toggled.connect(self._display_options_changed)
        ol.addWidget(self.osc_bundle_checkbox)
        self.osc_status_label = QLabel("Disabled")
        self.osc_status_label.setStyleSheet("color: #555;")
        ol.addWidget(self.osc_status_label)
        if not OSC_AVAILABLE:
            self.osc_enabled_checkbox.setEnabled(False)
            self.osc_status_label.setText("python-osc not installed")
            self.osc_status_label.setStyleSheet("color: #ff6666;")
        ol.addStretch()
        main.addWidget(osc)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Not connected")

        self.panel_actions = {}
        for key, text, panel in (("dip", "DIP Switch Panel", dip),
                                 ("perfectcue", "Perfect Cue Panel", self.pc_widget),
                                 ("osc", "OSC Output Panel", osc)):
            act = view.addAction(text)
            act.setCheckable(True)
            act.setChecked(True)
            act.toggled.connect(lambda on, p=panel: self._set_panel_visible(p, on))
            self.panel_actions[key] = act
        self.refresh_ports()

    def _set_panel_visible(self, panel: QWidget, visible: bool):
        panel.setVisible(visible)
        if not visible:
            QTimer.singleShot(0, lambda: self.resize(self.width(), self.minimumSizeHint().height()))

    def refresh_ports(self):
        current = self.port_combo.currentData() or self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItem("Select serial port...", None)
        for dev, desc in list_serial_ports():
            self.port_combo.addItem(f"{dev} — {desc}", dev)
        idx = self.port_combo.findData(current)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)

    def _selected_port(self) -> Optional[str]:
        data = self.port_combo.currentData()
        if data:
            return data
        text = self.port_combo.currentText().strip()
        return text if text and not text.startswith("Select") else None

    # ── settings ──────────────────────────────────────────────────────────────

    def load_settings(self):
        s = self.settings
        geo = s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        def select(combo, value):
            if value:
                i = combo.findData(value)
                if i >= 0:
                    combo.setCurrentIndex(i)
                elif combo.isEditable():
                    combo.setEditText(str(value))

        select(self.port_combo, s.value("limitimer/port"))
        select(self.pc_widget.port_combo, s.value("perfectcue/port"))
        select(self.pc_widget.baud_combo, s.value("perfectcue/baud", pc.BAUDRATE, type=int))
        self.follow_format_checkbox.setChecked(s.value("display/follow_format", False, type=bool))
        for i, w in enumerate(self.timer_widgets):
            w.overtime_checkbox.setChecked(s.value(f"display/overtime_{i}", False, type=bool))
        self.osc_ip_edit.setText(s.value("osc/ip", "127.0.0.1", type=str))
        self.osc_port_spin.setValue(s.value("osc/port", 5000, type=int))
        self.osc_prefix_edit.setText(s.value("osc/prefix", "/dsan", type=str))
        self.osc_bundle_checkbox.setChecked(s.value("osc/bundle", True, type=bool))
        self.dbg.zero_crc_checkbox.setChecked(s.value("debug/accept_zero_crc", True, type=bool))
        for key, act in self.panel_actions.items():
            act.setChecked(s.value(f"panels/{key}", True, type=bool))
        if OSC_AVAILABLE and s.value("osc/enabled", False, type=bool):
            self.osc_enabled_checkbox.setChecked(True)

    def save_settings(self):
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        port = self._selected_port()
        if port:
            s.setValue("limitimer/port", port)
        pc_port = self.pc_widget.selected_port()
        if pc_port and pc_port != "sim":
            s.setValue("perfectcue/port", pc_port)
        s.setValue("perfectcue/baud", self.pc_widget.baud_combo.currentData())
        s.setValue("display/follow_format", self.follow_format_checkbox.isChecked())
        for i, w in enumerate(self.timer_widgets):
            s.setValue(f"display/overtime_{i}", w.overtime)
        s.setValue("osc/ip", self.osc_ip_edit.text())
        s.setValue("osc/port", self.osc_port_spin.value())
        s.setValue("osc/prefix", self.osc_prefix_edit.text())
        s.setValue("osc/bundle", self.osc_bundle_checkbox.isChecked())
        s.setValue("osc/enabled", self.osc_bridge is not None)
        s.setValue("debug/accept_zero_crc", self.dbg.zero_crc_checkbox.isChecked())
        for key, act in self.panel_actions.items():
            s.setValue(f"panels/{key}", act.isChecked())

    # ── Limitimer connection ──────────────────────────────────────────────────

    def _start_limitimer(self, device: Limitimer, demo: bool):
        self.limitimer = device
        self.demo = demo
        device.accept_zero_crc = self.dbg.zero_crc_checkbox.isChecked()
        self.lt_signals.attach(device)
        self._rebuild_osc()
        device.start()
        self.connect_btn.setEnabled(not demo)
        self.demo_btn.setEnabled(demo)
        self.connect_btn.setText("Connect" if demo else "Disconnect")
        self.demo_btn.setText("Stop Demo" if demo else "Demo")
        for w in (self.port_combo, self.refresh_btn):
            w.setEnabled(False)

    def _stop_limitimer(self):
        dev, self.limitimer = self.limitimer, None
        if dev:
            dev.stop()      # its final CLOSED link callback resets the UI
        self.demo = False
        self._rebuild_osc()
        self.connect_btn.setEnabled(True)
        self.demo_btn.setEnabled(True)
        self.connect_btn.setText("Connect")
        self.demo_btn.setText("Demo")
        for w in (self.port_combo, self.refresh_btn):
            w.setEnabled(True)

    def toggle_connection(self):
        if self.limitimer:
            self._stop_limitimer()
            return
        port = self._selected_port()
        if not port:
            self.status_bar.showMessage("Select a serial port (or type socket://host:port)")
            return
        self._start_limitimer(Limitimer(port), demo=False)

    def toggle_demo(self):
        if self.limitimer:
            self._stop_limitimer()
            self.status_bar.showMessage("Demo stopped")
        else:
            self._start_limitimer(Limitimer.simulated(), demo=True)
            self.status_bar.showMessage("Demo — built-in show simulator, real-time, through the real decoder")

    def _show_link(self, link: LinkState):
        text, color = _LINK_LABEL.get(link, (link.value, "#888"))
        if self.demo and link == LinkState.LIVE:
            text = "● DEMO"
        self.link_label.setText(text)
        self.link_label.setStyleSheet(f"color: {color};")

    def on_lt_link(self, link: LinkState):
        if self.sender() is self.lt_signals and self.limitimer is None and link != LinkState.CLOSED:
            return   # late event from a device we already stopped
        self._show_link(link)
        stale = link != LinkState.LIVE and self.current_state is not None
        for w in self.timer_widgets:
            w.set_stale(stale)
        messages = {
            LinkState.WAITING: "Port open — waiting for data…",
            LinkState.LIVE: "Receiving data",
            LinkState.STALE: "NO SIGNAL — display is NOT live",
            LinkState.RECONNECTING: "Serial port lost — reconnecting…",
            LinkState.CLOSED: "Disconnected",
        }
        if link in messages and not self.demo:
            self.status_bar.showMessage(messages[link])

    # ── Perfect Cue connection ──────────────────────────────────────────────────

    def toggle_pc_connection(self):
        if self.perfectcue:
            dev, self.perfectcue = self.perfectcue, None
            dev.stop()
            self._rebuild_osc()
            return
        port = self.pc_widget.selected_port()
        if not port:
            return
        if port == "sim":
            dev = PerfectCue.simulated()
        else:
            dev = PerfectCue(port, baudrate=self.pc_widget.baud_combo.currentData())
        self.perfectcue = dev
        self.pc_signals.attach(dev)
        self._rebuild_osc()
        dev.start()

    def on_pc_link(self, link: LinkState):
        port = self.pc_widget.selected_port() or ""
        self.pc_widget.set_link(link if self.perfectcue or link == LinkState.CLOSED else LinkState.CLOSED,
                                "simulated" if port == "sim" else port)

    def on_button(self, btn: pc.Button):
        self.pc_widget.show_button(btn)
        self.dbg.log(f"<span style='color:#aaddff;'>PerfectCue: {btn.name}</span>")

    def send_pc_command(self, cmd: pc.Command):
        if self.perfectcue and self.perfectcue.send(cmd):
            shown = cmd.value.decode("ascii").replace("\r", "\\r")   # f-strings can't hold backslashes on 3.9-3.11
            self.dbg.log(f"<span style='color:#ffaa44;'>PC TX: {shown}</span>")
        else:
            self.status_bar.showMessage("Perfect Cue port is not open")

    # ── incoming Limitimer data ────────────────────────────────────────────────

    def on_state(self, state: LimitimerState):
        if self.limitimer is None:
            return
        self.current_state = state
        self._redraw()
        self._log_state(state)

    def _redraw(self, *_):
        state = self.current_state
        if state is None:
            return
        follow = self.follow_format_checkbox.isChecked()
        self.direction_label.setText(f"Direction: {'Countdown' if state.count_down else 'Count Up'}")
        loud = " (LOUD)" if state.beep_loud else ""
        self.beep_label.setText(f"Beep: {state.beep_type.name.title()}{loud}")
        self.selected_label.setText(f"Selected: Program {state.selected_index + 1}")
        self.sequence_label.setText(f"Seq: {state.sequence}")
        for w in self.timer_widgets:
            w.update_timer(state, follow)
        self._update_dip(state)

    def _update_dip(self, s: LimitimerState):
        def put(key, text, color):
            self.dip_labels[key].setText(text)
            self.dip_labels[key].setStyleSheet(f"color: {color};")

        put("sw1", "—", "#444")
        put("sw2", *(("MM:SS", "#00ff88") if s.session_minutes else ("HH:MM", "#aa88ff")))
        put("sw3", *(("MM:SS", "#00ff88") if s.program_minutes else ("HH:MM", "#aa88ff")))
        put("sw4", *(("↓ Down", "#ff8800") if s.count_down else ("↑ Up", "#00aaff")))
        put("sw5", *(("Continue", "#ffaa00") if s.continue_after_zero else ("Stop@0", "#888")))
        put("sw6", *(("HIGH", "#ff4444") if s.beep_loud else ("Low", "#888")))
        put("sw78", s.beep_type.name.title(), "#ffff00" if s.beep_type else "#666")
        put("sw9", *(("Allowed", "#00ff88") if s.permit_changes else ("Locked", "#ff4444")))
        put("sw10", "—", "#444")

    def _display_options_changed(self, *_):
        self._redraw()
        if self.osc_bridge:
            self.osc_bridge.overtime = [w.overtime for w in self.timer_widgets]
            self.osc_bridge.follow_device_format = self.follow_format_checkbox.isChecked()
            self.osc_bridge.bundle = self.osc_bundle_checkbox.isChecked()
            self.osc_bridge.resend()

    # ── OSC ─────────────────────────────────────────────────────────────────────

    def toggle_osc(self, enabled: bool):
        for w in (self.osc_ip_edit, self.osc_port_spin, self.osc_prefix_edit):
            w.setEnabled(not enabled)   # destination can't change under a live bridge
        self._rebuild_osc()

    def _rebuild_osc(self):
        """(Re)attach the OSC bridge to whichever devices are connected right now."""
        if self.osc_bridge:
            self.osc_bridge.close()
            self.osc_bridge = None
        if not (OSC_AVAILABLE and self.osc_enabled_checkbox.isChecked()):
            self.osc_status_label.setText("Disabled")
            self.osc_status_label.setStyleSheet("color: #666;")
            return
        ip, port = self.osc_ip_edit.text(), self.osc_port_spin.value()
        try:
            self.osc_bridge = OscBridge(
                ip, port, limitimer=self.limitimer, perfectcue=self.perfectcue,
                prefix=self.osc_prefix_edit.text(), bundle=self.osc_bundle_checkbox.isChecked(),
                overtime=[w.overtime for w in self.timer_widgets],
                follow_device_format=self.follow_format_checkbox.isChecked())
            self.osc_status_label.setText(f"Sending to {ip}:{port}")
            self.osc_status_label.setStyleSheet("color: #00ff88;")
        except Exception as e:
            self.osc_status_label.setText(f"Error: {e}")
            self.osc_status_label.setStyleSheet("color: #ff6666;")
            self.osc_enabled_checkbox.setChecked(False)

    def _show_osc_reference(self):
        self.osc_ref.refresh(self.osc_prefix_edit.text(), self.osc_bundle_checkbox.isChecked())
        self.osc_ref.show()
        self.osc_ref.raise_()

    # ── debug console ───────────────────────────────────────────────────────────

    def _apply_zero_crc(self, on: bool):
        if self.limitimer:
            self.limitimer.accept_zero_crc = on

    def _log_error(self, who: str, err: str):
        self.status_bar.showMessage(f"{who.title()} error: {err}")
        self.dbg.log(f"<span style='color:#ff6666;'>{who}: {err}</span>")

    def on_raw(self, data: bytes):
        if self.dbg.raw_checkbox.isChecked():
            self.dbg.log(f"<span style='color:#666;'>RAW [{len(data)}]: "
                         f"{' '.join(f'{b:02X}' for b in data)}</span>")

    def on_packet(self, ev: PacketEvent):
        if not ev.ok:
            if self.dbg.errors_checkbox.isChecked():
                self.dbg.log(f"<span style='color:#ff6666;'>ERR [{len(ev.raw)}]: {ev.error}</span>")
                if self.dbg.raw_checkbox.isChecked():
                    self.dbg.log(f"<span style='color:#ff6666;'>    {ev.raw.hex(' ').upper()}</span>")
        elif self.dbg.raw_checkbox.isChecked():
            kind = {0x00: "STATUS", 0x10: "PING"}.get(ev.type, f"0x{ev.type:02X}")
            zero = " (zero CRC)" if ev.zero_crc else ""
            self.dbg.log(f"<span style='color:#00ff88;'>PKT [{len(ev.raw)}] {kind}{zero}</span>")

    def _log_state(self, state: LimitimerState):
        follow = self.follow_format_checkbox.isChecked()
        parts = []
        for w in self.timer_widgets:
            p = state.program(w.number)
            disp = state.display(w.number, True, follow)
            color = ("#ff3333" if p.expired else "#ffaa00" if p.warning
                     else "#00ff88" if p.running else "#888888")
            parts.append((disp, p.running, p.blink, color))
        key = (tuple(parts), state.count_down, state.selected_index)
        if self.dbg.changes_only_checkbox.isChecked() and key == self.last_debug_key:
            return
        self.last_debug_key = key
        mode = "↓" if state.count_down else "↑"
        body = " | ".join(f"<span style='color:{c};'>P{i + 1}:{d}{'▶' if r else '·'}{'⚡' if b else ''}</span>"
                          for i, (d, r, b, c) in enumerate(parts))
        self.dbg.log(f"<span style='color:#888;'>{mode}</span> {body}")

    def _update_stats(self):
        if self.limitimer:
            st = self.limitimer.stats
            zero = f" | Zero-CRC: {st['zero_crc']}" if st["zero_crc"] else ""
            self.dbg.stats_label.setText(f"Packets: {st['packets']} | Errors: {st['errors']}{zero}")

    def clear_debug(self):
        self.dbg.text.clear()
        self.last_debug_key = None
        if self.limitimer:
            self.limitimer.reset_stats()
        self._update_stats()

    def closeEvent(self, event):
        self.save_settings()
        for dev in (self.limitimer, self.perfectcue):
            if dev:
                dev.stop()
        if self.osc_bridge:
            self.osc_bridge.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setFont(QFont(MONO, 10))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

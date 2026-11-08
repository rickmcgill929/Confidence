"""The LIMITIMER CONTROL panel: the Limitimer's buttons in Confidence.

Two ways it can work:

* **Real Limitimer** — Confidence reads the Limitimer's RS-485 data as before; the
  buttons press the real console through DSAN's TP-2000X adapter (RS-232, 9600 8N1).
* **Software Limitimer** — Confidence *is* the Limitimer (:mod:`dsan.emulator`). The
  buttons run it; the window can send it out of the Limitimer's RS-485 adapter to
  drive DSAN signal lights.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Dict, Iterable, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from .. import limitimer as lt
from ..control import Feedback, Key, LED_DIM, LED_ON, LimitimerControl
from ..devices import LinkState
from ..emulator import LimitimerEmulator, MAX_TIME
from . import ports as portsmod
from .widgets import Combo

MODES = {"read": "Real Limitimer", "emulate": "Software Limitimer"}
_URL_ITEM = "__url__"
_PROGRAM_KEYS = (Key.PROGRAM_1, Key.PROGRAM_2, Key.PROGRAM_3, Key.SESSION)


def parse_time(text: str) -> int:
    """'5' → 300 (minutes), '4:30' → 270, '1:00:00' → 3600. Raises ValueError."""
    text = text.strip()
    if not text:
        raise ValueError("empty")
    parts = text.split(":")
    if len(parts) > 3 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f"not a time: {text!r}")
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        secs = nums[0] * 60
    elif len(nums) == 2:
        secs = nums[0] * 60 + nums[1]
    else:
        secs = nums[0] * 3600 + nums[1] * 60 + nums[2]
    if secs > MAX_TIME:
        raise ValueError("too long")
    return secs


def format_time(secs: int) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def lamps(state: Optional[lt.LimitimerState]) -> Dict[str, object]:
    """What the console would light for this state (programs on/dim/off, G/Y/R…)."""
    if state is None:
        return {"programs": ["off"] * 4, "running": False, "beep": False, "blink": False,
                "seconds": False, "green": False, "yellow": False, "red": False, "remaining": ""}
    sel = state.selected
    progs = []
    for i, p in enumerate(state.programs):
        progs.append("on" if i == state.selected_index else ("dim" if p.running else "off"))
    active = sel.running or sel.elapsed > 0
    return {"programs": progs, "running": sel.running, "beep": sel.beep, "blink": sel.blink,
            "seconds": sel.sec_adjust,
            "green": active and sel.color == 0, "yellow": active and sel.color == 1,
            "red": active and sel.color == 2,
            "remaining": state.display(sel.number)}


class ProgramsDialog(QDialog):
    """Times and switches of the software Limitimer."""

    def __init__(self, emu: LimitimerEmulator, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Software Limitimer — programs")
        self.emu = emu
        v = QVBoxLayout(self)
        grid = QGridLayout()
        grid.addWidget(QLabel("Total"), 0, 1)
        grid.addWidget(QLabel("Sum-up"), 0, 2)
        self.fields = []
        for i, (total, sumup) in enumerate(emu.programs()):
            name = QLabel("Session (4)" if i == 3 else f"Program {i + 1}")
            t, s = QLineEdit(format_time(total)), QLineEdit(format_time(sumup))
            for f in (t, s):
                f.setPlaceholderText("m:ss")
                f.setMaximumWidth(110)
                f.setToolTip("Minutes:seconds (4:30), hours:minutes:seconds (1:00:00) or just minutes (5)")
            grid.addWidget(name, i + 1, 0)
            grid.addWidget(t, i + 1, 1)
            grid.addWidget(s, i + 1, 2)
            self.fields.append((t, s))
        v.addLayout(grid)
        self.count_down = QCheckBox("Count down (else count up)  · DIP 4")
        self.cont = QCheckBox("Keep counting past zero  · DIP 5")
        self.permit = QCheckBox("Allow time changes while running  · DIP 9")
        self.prog_min = QCheckBox("Programs 1–3 show minutes:seconds (else hours:minutes)  · DIP 3")
        self.sess_min = QCheckBox("Session shows minutes:seconds (else hours:minutes)  · DIP 2")
        self.loud = QCheckBox("Loud beep  · DIP 6")
        for box, val in ((self.count_down, emu.count_down), (self.cont, emu.continue_after_zero),
                         (self.permit, emu.permit_changes), (self.prog_min, emu.program_minutes),
                         (self.sess_min, emu.session_minutes), (self.loud, emu.beep_loud)):
            box.setChecked(val)
            v.addWidget(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Sound on the lights  · DIP 7+8"))
        self.sound = Combo()
        for b in lt.BeepType:
            self.sound.addItem(b.name.title(), b)
        self.sound.setCurrentIndex(self.sound.findData(emu.beep_type))
        row.addWidget(self.sound)
        row.addStretch()
        v.addLayout(row)
        self.error = QLabel()
        self.error.setStyleSheet("color: #FF3B3B;")
        v.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _ok(self):
        try:
            programs = [(parse_time(t.text()), parse_time(s.text())) for t, s in self.fields]
        except ValueError:
            self.error.setText("Times are minutes:seconds (4:30), h:mm:ss (1:00:00) or minutes (5).")
            return
        self.emu.set_programs(programs)
        self.emu.count_down = self.count_down.isChecked()
        self.emu.continue_after_zero = self.cont.isChecked()
        self.emu.permit_changes = self.permit.isChecked()
        self.emu.program_minutes = self.prog_min.isChecked()
        self.emu.session_minutes = self.sess_min.isChecked()
        self.emu.beep_loud = self.loud.isChecked()
        self.emu.beep_type = self.sound.currentData()
        self.accept()


class LimitimerPanel(QGroupBox):
    mode_changed = pyqtSignal(str)
    _feedback = pyqtSignal(object)
    _link = pyqtSignal(object)
    _error = pyqtSignal(str)
    _pressed_sig = pyqtSignal()

    def __init__(self, notify: Callable[[str], None], port_infos: Callable[[], Iterable[portsmod.PortInfo]],
                 parent: Optional[QWidget] = None):
        super().__init__("LIMITIMER CONTROL", parent)
        self.notify = notify
        self.port_infos = port_infos
        self.mode = "read"
        self.emulator = LimitimerEmulator()
        self.control: Optional[LimitimerControl] = None
        self.ref: Optional[portsmod.PortRef] = None
        self.want = False
        self.feedback = Feedback()
        self.ctl_link = LinkState.CLOSED
        self.ctl_error = ""
        self.state: Optional[lt.LimitimerState] = None       # what the window shows (for lamps)
        self.state_live = False
        self._feedback.connect(self._on_feedback)
        self._link.connect(self._on_link)
        self._error.connect(self._on_error)
        self._pressed_sig.connect(self.refresh)
        self._build()
        self.refresh()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self):
        self.setStyleSheet(
            "QPushButton[lit=\"on\"] { background: #2E6BFF; color: white; border-color: #5A8BFF; }"
            "QPushButton[lit=\"dim\"] { background: #1B2D52; color: #C8D6FF; }"
            "QPushButton[lit=\"run\"] { background: #0E7A47; color: white; border-color: #00E07A; }"
            "QPushButton[lit=\"amber\"] { background: #7A5A00; color: white; }")
        v = QVBoxLayout(self)
        v.setSpacing(6)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.mode_combo = Combo()
        for k, label in MODES.items():
            self.mode_combo.addItem(label, k)
        self.mode_combo.setToolTip(
            "Real Limitimer: read the console's data (INPUTS above); its buttons work from here "
            "through a DSAN TP-2000X.\n"
            "Software Limitimer: Confidence runs the timer itself — no console needed. It can drive "
            "DSAN signal lights through the Limitimer adapter.")
        self.mode_combo.activated.connect(lambda _i: self.set_mode(self.mode_combo.currentData()))
        form.addRow("Timer", self.mode_combo)
        v.addLayout(form)

        # real: TP-2000X
        self.read_box = QWidget()
        rv = QVBoxLayout(self.read_box)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        row = QHBoxLayout()
        lbl = QLabel("TP-2000X")
        lbl.setToolTip("DSAN's touch-panel adapter for the Limitimer: RS-232, 9600 8N1.\n"
                       "Wire a USB-RS232 adapter to it (or a serial-to-IP converter: socket://host:port).")
        row.addWidget(lbl)
        self.port_combo = Combo()
        self.port_combo.activated.connect(self._port_picked)
        row.addWidget(self.port_combo, 1)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setCheckable(True)
        self.connect_btn.clicked.connect(self._connect_clicked)
        row.addWidget(self.connect_btn)
        rv.addLayout(row)
        v.addWidget(self.read_box)

        # software: settings
        self.emu_box = QWidget()
        ev = QHBoxLayout(self.emu_box)
        ev.setContentsMargins(0, 0, 0, 0)
        self.programs_btn = QPushButton("Programs && switches…")
        self.programs_btn.clicked.connect(self._edit_programs)
        ev.addWidget(self.programs_btn)
        ev.addStretch()
        v.addWidget(self.emu_box)

        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        # the console
        self.keys: Dict[Key, QPushButton] = {}

        def key(k: Key, text: Optional[str] = None, tip: str = "") -> QPushButton:
            b = QPushButton(text or k.label)
            b.setToolTip(tip or k.label)
            b.clicked.connect(lambda _=False, k=k: self._pressed(k))
            self.keys[k] = b
            return b

        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        for i, k in enumerate((Key.PROGRAM_1, Key.PROGRAM_2, Key.PROGRAM_3, Key.SESSION)):
            grid.addWidget(key(k, tip="Select " + ("the Session (program 4)" if i == 3 else f"program {i + 1}")),
                           0, i)
        grid.addWidget(key(Key.START_STOP), 1, 0, 1, 2)
        grid.addWidget(key(Key.REPEAT, tip="Back to full time, stopped"), 1, 2)
        grid.addWidget(key(Key.CLEAR, tip="Stop and zero this program's times"), 1, 3)
        grid.addWidget(key(Key.TOTAL_DOWN, "Time −", "Total time down a minute (a second in Seconds mode)"), 2, 0)
        grid.addWidget(key(Key.TOTAL_UP, "Time +", "Total time up a minute (a second in Seconds mode)"), 2, 1)
        grid.addWidget(key(Key.SUMUP_DOWN, "Sum-up −", "Sum-up (warning) time down"), 2, 2)
        grid.addWidget(key(Key.SUMUP_UP, "Sum-up +", "Sum-up (warning) time up"), 2, 3)
        grid.addWidget(key(Key.SECONDS, tip="Time buttons step seconds instead of minutes"), 3, 0)
        grid.addWidget(key(Key.BEEP, tip="Beep on for this program"), 3, 1)
        grid.addWidget(key(Key.BLINK, tip="Blink on for this program"), 3, 2)
        for c in range(4):
            grid.setColumnStretch(c, 1)
        v.addLayout(grid)

        info = QHBoxLayout()
        self.lamp_labels = {}
        for name, color in (("green", "#00E07A"), ("yellow", "#FFB000"), ("red", "#FF3B3B")):
            dot = QLabel("●")
            dot.setToolTip(f"{name.title()} light")
            self.lamp_labels[name] = (dot, color)
            info.addWidget(dot)
        info.addSpacing(6)
        self.readout = QLabel()
        self.readout.setStyleSheet("font-weight: 600;")
        info.addWidget(self.readout, 1)
        v.addLayout(info)

    # ── modes and targets ───────────────────────────────────────────────────────

    def set_mode(self, mode: str, emit: bool = True):
        if mode not in MODES:
            mode = "read"
        changed = mode != self.mode
        self.mode = mode
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))
        if mode == "emulate":
            self._stop_control()
        else:
            self._apply_control()
        self.refresh()
        if changed and emit:
            self.mode_changed.emit(mode)

    def target(self) -> Optional[str]:
        """Where a button press goes: 'emulator', 'tp2000x', or None."""
        if self.mode == "emulate":
            return "emulator"
        if self.control is not None and self.control.link in (LinkState.WAITING, LinkState.LIVE,
                                                              LinkState.STALE, LinkState.OPEN):
            return "tp2000x"
        return None

    def press(self, key) -> Key:
        """Press a Limitimer button (Key or name). Raises ValueError with a reason."""
        k = Key.parse(key)
        t = self.target()
        if t == "emulator":
            self.emulator.press(k)
        elif t == "tp2000x":
            self.control.press(k)
        elif self.mode == "read":
            raise ValueError("No TP-2000X connected — the real Limitimer's buttons need DSAN's "
                             "TP-2000X adapter (or switch to Software Limitimer)")
        else:
            raise ValueError("Limitimer control isn't available")
        self._pressed_sig.emit()          # repaint on the GUI thread, whoever pressed
        return k

    def _pressed(self, k: Key):
        try:
            self.press(k)
        except ValueError as e:
            self.notify(str(e))

    # ── TP-2000X port ──────────────────────────────────────────────────────────

    def _resolved(self) -> Optional[str]:
        return self.ref.resolve(self.port_infos()) if self.ref else None

    def ports_changed(self):
        """The window re-scanned ports: rebuild the list; reconnect if the adapter came back."""
        self._rebuild_ports()
        if self.mode == "read" and self.want and self._resolved() != (self.control.port if self.control else None):
            self._apply_control()
        self.refresh()

    def _rebuild_ports(self):
        c = self.port_combo
        c.blockSignals(True)
        c.clear()
        c.addItem("Off — not used", "")
        infos = sorted(self.port_infos(), key=lambda p: (not p.is_usb, p.device))
        for p in infos:
            c.addItem(p.short_label(), p.device)
        dev = self._resolved()
        if self.ref and not dev:
            c.addItem(f"{self.ref.description or os.path.basename(self.ref.device)} — not plugged in", "__missing__")
        elif self.ref and c.findData(dev) < 0:
            c.addItem(dev, dev)
        c.addItem("Network (socket://host:port)…", _URL_ITEM)
        c.setCurrentIndex(0 if self.ref is None else max(0, c.findData(dev if dev else "__missing__")))
        c.blockSignals(False)

    def _port_picked(self, index: int):
        data = self.port_combo.itemData(index)
        if data in ("__missing__", None):
            return
        if data == _URL_ITEM:
            from PyQt6.QtWidgets import QInputDialog
            text, ok = QInputDialog.getText(self, "TP-2000X over the network",
                                            "Serial-to-IP converter address:", text="socket://192.168.1.60:5000")
            if not ok or "://" not in text.strip():
                self._rebuild_ports()
                return
            self.ref = portsmod.PortRef.url(text.strip())
        elif data == "":
            self.ref, self.want = None, False
        else:
            info = next((p for p in self.port_infos() if p.device == data), None)
            self.ref = portsmod.PortRef.of(info) if info else portsmod.PortRef.url(data)
        self._apply_control()
        self._rebuild_ports()
        self.refresh()

    def _connect_clicked(self, on: bool):
        if on and not self.ref:
            self.connect_btn.setChecked(False)
            self.notify("Pick the TP-2000X's serial adapter first")
            return
        self.want = on
        self._apply_control()
        self.refresh()

    def _apply_control(self):
        self._stop_control()
        port = self._resolved() if (self.want and self.mode == "read") else None
        if not port:
            return
        ctl = LimitimerControl(port)
        self.control = ctl
        self.ctl_error = ""
        ctl.on_feedback(self._feedback.emit)
        ctl.on_link(self._link.emit)
        ctl.on_error(lambda e: self._error.emit(str(e)))
        ctl.start()

    def _stop_control(self):
        ctl, self.control = self.control, None
        if ctl is not None:
            ctl.stop()
        self.ctl_link = LinkState.CLOSED
        self.feedback = Feedback()

    def _on_feedback(self, fb):
        if self.control is not None:
            self.feedback = fb
            self.refresh()

    def _on_link(self, link):
        if self.control is not None:
            self.ctl_link = link
            self.refresh()

    def _on_error(self, text):
        self.ctl_error = text
        self.refresh()

    def close_all(self):
        self._stop_control()

    # ── software Limitimer ───────────────────────────────────────────────────────

    def _edit_programs(self):
        if ProgramsDialog(self.emulator, self).exec():
            self.refresh()

    # ── view ─────────────────────────────────────────────────────────────────────

    def show_state(self, state: Optional[lt.LimitimerState], live: bool):
        """The window's current Limitimer state (from the console, or the emulator)."""
        self.state, self.state_live = state, live
        self.refresh()

    def lamp_view(self) -> Dict[str, object]:
        """Lamps to show: the TP-2000X's report when there is one, else from the state."""
        if self.mode == "read" and self.control is not None and self.ctl_link == LinkState.LIVE:
            fb = self.feedback
            return {"programs": list(fb.programs), "running": None, "beep": fb.beep, "blink": fb.blink,
                    "seconds": fb.seconds, "green": fb.green, "yellow": fb.yellow, "red": fb.red,
                    "remaining": fb.remaining}
        return lamps(self.state if (self.state_live or self.mode == "emulate") else None)

    def status_text(self):
        if self.mode == "emulate":
            return ("● Confidence is the Limitimer — use the buttons below, the browser remote or OSC. "
                    "To drive DSAN lights, pick the Limitimer's adapter in INPUTS and press Send.", "#00E07A")
        if not self.ref:
            return ("Buttons need DSAN's TP-2000X adapter (RS-232). Without it, Confidence only reads "
                    "the Limitimer.", "#6A6A6A")
        if not self.want:
            return "TP-2000X disconnected — press Connect", "#6A6A6A"
        if self.control is None:
            return "● TP-2000X adapter not plugged in — will connect when it is", "#FF3B3B"
        link = self.ctl_link
        if link == LinkState.LIVE:
            return "● TP-2000X connected — buttons press the Limitimer", "#00E07A"
        if link == LinkState.STALE:
            return "● TP-2000X went quiet — buttons may not reach the Limitimer", "#FF3B3B"
        if link == LinkState.RECONNECTING:
            return f"● can't open the TP-2000X port, retrying{f' ({self.ctl_error})' if self.ctl_error else ''}", \
                "#FF3B3B"
        return "● Port open — waiting for the TP-2000X (is it powered and wired to the Limitimer?)", "#FFB000"

    def refresh(self):
        emulate = self.mode == "emulate"
        self.read_box.setVisible(not emulate)
        self.emu_box.setVisible(emulate)
        self.connect_btn.setChecked(self.want and not emulate)
        self.connect_btn.setText("Disconnect" if self.want else "Connect")
        text, color = self.status_text()
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
        can = self.target() is not None
        view = self.lamp_view()
        for k, b in self.keys.items():
            b.setEnabled(can)
            lit = ""
            if k in _PROGRAM_KEYS:
                lit = {LED_ON: "on", LED_DIM: "dim"}.get(view["programs"][_PROGRAM_KEYS.index(k)], "")
            elif k is Key.START_STOP and view["running"]:
                lit = "run"
            elif (k is Key.BEEP and view["beep"]) or (k is Key.BLINK and view["blink"]) or \
                    (k is Key.SECONDS and view["seconds"]):
                lit = "amber"
            if b.property("lit") != lit:
                b.setProperty("lit", lit)
                b.style().unpolish(b)
                b.style().polish(b)
        for name, (dot, color) in self.lamp_labels.items():
            css = f"color: {color if view[name] else '#333'}; font-size: 16px;"
            if dot.styleSheet() != css:
                dot.setStyleSheet(css)
        rem = view["remaining"]
        self.readout.setText(rem if rem else "")

    # ── remote / settings ─────────────────────────────────────────────────────────

    def remote_state(self) -> Dict[str, object]:
        text, _ = self.status_text()
        sel = self.state.selected if self.state is not None and self.mode == "emulate" else None
        out = {"mode": self.mode, "modes": [{"value": k, "label": v} for k, v in MODES.items()],
               "target": self.target(), "status": text, "lamps": self.lamp_view(),
               "keys": [{"value": k.name.lower(), "label": k.label} for k in Key]}
        if self.mode == "emulate":
            out["programs"] = [{"total": t, "sumup": s} for t, s in self.emulator.programs()]
            out["selected"] = (sel.number if sel else None)
        return out

    def save(self, s):
        s.setValue("ltctl/mode", self.mode)
        s.setValue("ltctl/emulator", json.dumps(self.emulator.settings()))
        s.setValue("ltctl/port", json.dumps(self.ref.to_dict() if self.ref else None))
        s.setValue("ltctl/connected", self.want)

    def load(self, s):
        try:
            self.emulator.apply_settings(json.loads(s.value("ltctl/emulator", "{}") or "{}"))
        except (TypeError, ValueError):
            pass
        try:
            self.ref = portsmod.PortRef.from_dict(json.loads(s.value("ltctl/port", "null") or "null"))
        except (TypeError, ValueError):
            self.ref = None
        self.want = bool(self.ref) and s.value("ltctl/connected", False, type=bool)
        self._rebuild_ports()
        self.set_mode(s.value("ltctl/mode", "read", type=str), emit=False)


__all__ = ["LimitimerPanel", "MODES", "ProgramsDialog", "format_time", "lamps", "parse_time"]

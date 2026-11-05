"""The DECKLINK OUTPUT panel in Confidence's control window."""
from __future__ import annotations

import os
from typing import Callable, List, Optional

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from . import decklink as dlmod
from . import files
from .widgets import Combo
from .layout import PRESETS, Layout, parse_color
from .render import Frame, Renderer

PREFERRED_MODES = ("1080i59.94", "1080p29.97", "1080i50", "1080p25", "720p59.94")


class DeckLinkPanel(QGroupBox):
    def __init__(self, renderer: Renderer, screen_layout: Callable[[], Layout], frame: Callable[[], Frame],
                 notify: Callable[[str], None], parent: Optional[QWidget] = None):
        super().__init__("DECKLINK OUTPUT — SDI", parent)
        self.renderer = renderer
        self.screen_layout = screen_layout
        self.frame_source = frame
        self.notify = notify
        self.devices: List[dlmod.DeckLinkDevice] = []
        self.output: Optional[dlmod.DeckLinkOutput] = None
        self.feeder: Optional[dlmod.Feeder] = None
        self.layout_choice = "same"          # "same" | "preset:<name>" | "file:<path>"
        self._file_layout: Optional[Layout] = None
        self._preset_layout: Optional[Layout] = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._feed)
        self._build()
        self.rescan()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self):
        v = QVBoxLayout(self)
        self.reason = QLabel()
        self.reason.setWordWrap(True)
        self.reason.setObjectName("dim")
        v.addWidget(self.reason)

        self.body = QWidget()
        form = QFormLayout(self.body)
        form.setContentsMargins(0, 0, 0, 0)
        self.device_combo = Combo()
        self.device_combo.currentIndexChanged.connect(lambda _i: self._device_changed())
        form.addRow("Device", self.device_combo)
        self.mode_combo = Combo()
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._update_controls())
        form.addRow("Video mode", self.mode_combo)
        self.keying_combo = Combo()
        for key, label in dlmod.KEYING_LABELS.items():
            self.keying_combo.addItem(label, key)
        self.keying_combo.setToolTip(
            "Fill only: one SDI output, no key.\n"
            "Hardware keyer: the card outputs fill on SDI A and key on SDI B, frame-locked.\n"
            "Separate key output: the key goes to a second DeckLink output (lock both to reference).")
        self.keying_combo.currentIndexChanged.connect(lambda _i: self._update_controls())
        form.addRow("Cut / fill", self.keying_combo)
        self.key_combo = Combo()
        self.key_label = QLabel("Key output")
        form.addRow(self.key_label, self.key_combo)
        self.layout_combo = Combo()
        self.layout_combo.addItem("Same as screen output", "same")
        for name in PRESETS:
            self.layout_combo.addItem(f"Preset: {name}", f"preset:{name}")
        self.layout_combo.addItem("Layout file…", "pick-file")
        self.layout_combo.activated.connect(self._layout_picked)
        self.layout_combo.setToolTip("What goes out on SDI. For cut/fill, use a layout with a transparent "
                                     "background, e.g. Corner overlay.")
        form.addRow("Layout", self.layout_combo)
        self.premult = QCheckBox("Premultiplied fill")
        self.premult.setToolTip("Match your switcher's keyer: tick if its 'Pre Multiplied Key' is on.\n"
                                "Off sends straight (unshaped) fill, like Blackmagic's own keyer sample.")
        self.premult.toggled.connect(lambda on: setattr(self.feeder, "premultiplied", on) if self.feeder else None)
        form.addRow("", self.premult)
        v.addWidget(self.body)

        row = QHBoxLayout()
        self.start_btn = QPushButton("Start SDI output")
        self.start_btn.setCheckable(True)
        self.start_btn.clicked.connect(self._toggle)
        row.addWidget(self.start_btn)
        rescan = QPushButton("⟳")
        rescan.setToolTip("Re-scan DeckLink devices")
        rescan.setFixedWidth(36)
        rescan.clicked.connect(self.rescan)
        row.addWidget(rescan)
        row.addStretch()
        v.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #FFB000;")
        self.warning.hide()
        v.addWidget(self.warning)

    # ── devices & modes ─────────────────────────────────────────────────────────

    def rescan(self):
        if self.output:
            return
        ok, text = dlmod.available()
        self.devices = []
        if ok:
            try:
                self.devices = [d for d in dlmod.devices() if d.has_output]
            except dlmod.DeckLinkUnavailable as e:
                ok, text = False, str(e)
        if not ok:
            self.reason.setText(text)
        elif not self.devices:
            self.reason.setText(f"{text} — no DeckLink outputs found.")
        else:
            self.reason.setText(text)
        usable = ok and bool(self.devices)
        self.body.setVisible(usable)
        self.start_btn.setVisible(usable)
        current = self.device_combo.currentData()
        for combo in (self.device_combo, self.key_combo):
            combo.blockSignals(True)
            combo.clear()
            for d in self.devices:
                label = d.name + (" (inactive — check duplex in Desktop Video Setup)" if d.inactive else "")
                combo.addItem(label, d.name)
            combo.blockSignals(False)
        if current:
            i = self.device_combo.findData(current)
            if i >= 0:
                self.device_combo.setCurrentIndex(i)
        self._device_changed()

    def _device(self, combo: QComboBox) -> Optional[dlmod.DeckLinkDevice]:
        name = combo.currentData()
        return next((d for d in self.devices if d.name == name), None)

    def _device_changed(self):
        dev = self._device(self.device_combo)
        current = self.mode_combo.currentData()
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        if dev:
            for m in dev.modes:
                self.mode_combo.addItem(m.label + ("" if m.keying else "   (no keying)"), m.id)
            i = self.mode_combo.findData(current) if current is not None else -1
            if i < 0:
                for want in PREFERRED_MODES:
                    i = next((k for k, m in enumerate(dev.modes) if m.label == want), -1)
                    if i >= 0:
                        break
            self.mode_combo.setCurrentIndex(max(0, i))
        self.mode_combo.blockSignals(False)
        model = self.keying_combo.model()
        model.item(self.keying_combo.findData("external")).setEnabled(bool(dev and dev.external_keying))
        model.item(self.keying_combo.findData("separate")).setEnabled(len(self.devices) > 1)
        if self.keying_combo.currentData() == "external" and not (dev and dev.external_keying):
            self.keying_combo.setCurrentIndex(self.keying_combo.findData("none"))
        self._update_controls()

    def _mode(self) -> Optional[dlmod.DeckLinkMode]:
        dev = self._device(self.device_combo)
        mid = self.mode_combo.currentData()
        return dev.mode(mid) if dev and mid is not None else None

    def _update_controls(self):
        keying = self.keying_combo.currentData()
        separate = keying == "separate"
        self.key_combo.setVisible(separate)
        self.key_label.setVisible(separate)
        if separate and self.key_combo.currentData() == self.device_combo.currentData():
            other = next((k for k in range(self.key_combo.count())
                          if self.key_combo.itemData(k) != self.device_combo.currentData()), -1)
            if other >= 0:
                self.key_combo.setCurrentIndex(other)
        self.premult.setEnabled(keying != "none")
        mode = self._mode()
        problem = ""
        if keying == "external" and mode and not mode.keying:
            problem = f"This device can't key in {mode.label} — pick a mode without '(no keying)'."
        self._set_warning(problem or self._layout_warning())
        busy = self.output is not None
        for w in (self.device_combo, self.mode_combo, self.keying_combo, self.key_combo):
            w.setEnabled(not busy)
        self.start_btn.setEnabled(busy or (mode is not None and not problem))

    # ── layout ──────────────────────────────────────────────────────────────────

    def _layout_picked(self, index: int):
        data = self.layout_combo.itemData(index)
        if data == "pick-file":
            path = files.open_path(self, "Layout for SDI output", "Confidence layout (*.json)", "layouts")
            if not path:
                self._select_layout_item()
                return
            data = f"file:{path}"
        self.set_layout_choice(data)

    def set_layout_choice(self, choice: str):
        self._file_layout = None
        self._preset_layout = None
        if choice.startswith("file:"):
            path = choice[5:]
            try:
                self._file_layout = Layout.load(path)
            except Exception as e:
                self.notify(f"Couldn't open layout {os.path.basename(path)}: {e}")
                choice = "same"
        elif choice.startswith("preset:"):
            make = PRESETS.get(choice[7:])
            if make:
                self._preset_layout = make()
            else:
                choice = "same"
        self.layout_choice = choice
        self._select_layout_item()
        self._update_controls()

    def _select_layout_item(self):
        i = self.layout_combo.findData(self.layout_choice)
        if i < 0 and self.layout_choice.startswith("file:"):
            self.layout_combo.insertItem(self.layout_combo.count() - 1,
                                         f"File: {os.path.basename(self.layout_choice[5:])}", self.layout_choice)
            i = self.layout_combo.findData(self.layout_choice)
        self.layout_combo.setCurrentIndex(max(0, i))

    def layout(self) -> Layout:
        if self._file_layout is not None:
            return self._file_layout
        if self._preset_layout is not None:
            return self._preset_layout
        return self.screen_layout()

    def _layout_warning(self) -> str:
        if self.keying_combo.currentData() == "none":
            return ""
        try:
            alpha = parse_color(self.layout().background)[3]
        except ValueError:
            return ""
        if alpha == 255:
            return ("This layout's background is opaque, so the key will be solid white. "
                    "Use a transparent background (Preset: Corner overlay, or set the layout's "
                    "background alpha to 0).")
        return ""

    def _set_warning(self, text: str):
        self.warning.setText(text)
        self.warning.setVisible(bool(text))

    # ── run ─────────────────────────────────────────────────────────────────────

    def _toggle(self, on: bool):
        if on:
            self.start()
        else:
            self.stop()

    def start(self) -> bool:
        if self.output:
            return True
        dev, mode = self._device(self.device_combo), self._mode()
        keying = self.keying_combo.currentData()
        key = self._device(self.key_combo) if keying == "separate" else None
        if not dev or not mode:
            self.start_btn.setChecked(False)
            return False
        try:
            self.output = dlmod.DeckLinkOutput(dev, mode, keying, key=key)
        except RuntimeError as e:
            self.output = None
            self.start_btn.setChecked(False)
            self._status(f"● couldn't start: {e}", "#FF3B3B")
            return False
        self.feeder = dlmod.Feeder(self.output, self.renderer, self.layout, self.frame_source,
                                   premultiplied=self.premult.isChecked())
        self._timer.start(self.feeder.interval_ms)
        self._feed()
        self.start_btn.setChecked(True)
        self.start_btn.setText("Stop SDI output")
        self._update_controls()
        self.update_status()
        return True

    def stop(self):
        self._timer.stop()
        out, self.output, self.feeder = self.output, None, None
        if out:
            out.close()
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start SDI output")
        self._update_controls()
        self.update_status()

    def _feed(self):
        if not self.feeder:
            return
        try:
            self.feeder.tick()
        except Exception as e:      # never let a render error take the app down
            self.stop()
            self._status(f"● stopped: {e}", "#FF3B3B")

    def _status(self, text: str, color: str):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def update_status(self):
        """Called about once a second by the control window."""
        if not self.output:
            if not self.status.text().startswith("● couldn't") and not self.status.text().startswith("● stopped"):
                self._status("Off air", "#6A6A6A")
            return
        st = self.output.status()
        routing = {"none": "fill only", "external": "fill SDI A · key SDI B",
                   "separate": f"key on {self.output.key.name if self.output.key else '?'}"}[self.output.keying]
        ref = {"locked": "ref locked", "unlocked": "ref NOT locked", "n/a": "no ref input"}[st["reference"]]
        bad = st["late"] + st["dropped"]
        color = "#00E07A" if not bad else "#FFB000"
        self._status(f"● on air · {self.output.fill.name} · {self.output.mode.label} · {routing} · {ref}"
                     + (f" · {st['late']} late / {st['dropped']} dropped" if bad else ""), color)
        self._set_warning(self._layout_warning())

    # ── settings ─────────────────────────────────────────────────────────────────

    def save(self, s: QSettings):
        s.setValue("decklink/device", self.device_combo.currentData() or "")
        s.setValue("decklink/mode", int(self.mode_combo.currentData() or 0))
        s.setValue("decklink/keying", self.keying_combo.currentData())
        s.setValue("decklink/key_device", self.key_combo.currentData() or "")
        s.setValue("decklink/layout", self.layout_choice)
        s.setValue("decklink/premultiplied", self.premult.isChecked())
        s.setValue("decklink/on_air", self.output is not None)

    def load(self, s: QSettings):
        def pick(combo, value):
            i = combo.findData(value)
            if i >= 0:
                combo.setCurrentIndex(i)
        pick(self.device_combo, s.value("decklink/device", ""))
        self._device_changed()
        pick(self.mode_combo, s.value("decklink/mode", 0, type=int))
        pick(self.keying_combo, s.value("decklink/keying", "none"))
        pick(self.key_combo, s.value("decklink/key_device", ""))
        self.premult.setChecked(s.value("decklink/premultiplied", False, type=bool))
        self.set_layout_choice(s.value("decklink/layout", "same", type=str) or "same")
        self._update_controls()
        if s.value("decklink/on_air", False, type=bool) and self.devices:
            QTimer.singleShot(0, self.start)     # it was on air when the app quit

    def shutdown(self):
        self.stop()

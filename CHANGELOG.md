# Changelog

Versions follow [semantic versioning](https://semver.org): **major**.**minor**.**patch**.
A patch is fixes only, a minor version adds features, a major version may change how things work.
Built apps also carry a build number (date and time of the build), shown in **About Confidence**.

## [Unreleased]

### Added
- Linux build: `packaging/build_linux.sh` makes `Confidence-<version>-Linux-<arch>.tar.gz` (app folder, icon, desktop entry, `install.sh`), and the GitHub workflow builds it on Ubuntu 22.04.

### Fixed
- Millumin: the clip name no longer flashes on and off when advancing to a new column — a late *stoppedColumn* for the previous column, or a repeated *launchedColumn* for the current one, no longer clears it.

### Fixed
- Millumin follows columns: a clip stays up (frozen where it stopped) until you launch the next column, then switches to what starts there — or blanks if that column has nothing on the layer. Stopping the column blanks it.

### Changed
- **Renamed to Confidence** (was DSAN Viewer), with a new icon and logo: a countdown ring shaped like a C, running green → yellow → red (`packaging/brand/`). The app is `Confidence.app`, the disk image `Confidence-<version>-macOS-<arch>.dmg`, the launcher `Confidence.command`, the command `confidence` (`dsan-clock` still works), and new layouts go in Documents/Confidence. Settings move to `TRUCUE / Confidence`; anything saved by DSAN Viewer (or the short-lived TruTime build) is copied over on first launch. The Python library keeps its name, `dsan`.

### Fixed
- The layout editor's **Add ▾** button no longer shows as "dd": the extra menu arrow is gone and the button row is sized to fit its labels.

### Added
- **Millumin feedback.** A MILLUMIN box (listen port, layer, yellow/red thresholds) receives Millumin's OSC feedback: clip name, duration (TRT), elapsed and remaining time, play state, and loop / freeze worked out from the time feed. Timers, progress bars and phase lights get a *Millumin* source; labels get `{clip}` `{layer}` `{column}` `{trt}` `{remaining}` `{elapsed}` `{state}` `{playmode}`. New *Millumin countdown* preset. Millumin messages are logged in the debug console. Library: `dsan.millumin` (`MilluminTracker`, `MilluminReceiver`).

## [0.4.0] — 2026-11-05

### Added
- OSC in for the Perfect Cue: `/dsan/perfectcue/next`, `/prev`, `/blank 1|0`, `/press`, `/led`, `/command` — in DSAN Viewer (**Receive OSC**, listen port), `dsan serve --osc-in PORT` and `dsan.osc.OscListener`. Received messages are logged in the debug console.
- `PerfectCue.press()` to act as if a button was pressed.
- Perfect Cue LED feedback (master mode): `PerfectCue.light()` and `perfectcue.Led` — Enter lights the green arrow, an arrow key the red arrow, R/E blink yellow and leave it on/off.

### Changed
- Inputs panel tidied: the status line just says *Connected* (problems still show in amber/red); Detect, Swap and Debug moved to the new **Devices** menu; the debug console opens with ⌘D (Duplicate in the layout editor is now ⇧⌘D).
- DSAN Viewer: the baud rate drop-downs are gone; the Limitimer and Perfect Cue both run at a fixed 19200 8N1. (The library and command line keep their `baudrate` / `--pc-baud` options.)

### Fixed
- Drop-down lists (presets, ports, DeckLink modes…) are wide enough for their longest item instead of cutting names off on macOS.
- Saving a layout from the app opened in Finder no longer fails with "Read-only file system"; file dialogs start in the last-used folder (else Documents/DSAN Viewer).
- The library and `dsan` command line now default the Perfect Cue to 19200 baud, like DSAN Viewer. The receiver's ASCII command interface stays at 9600 (`perfectcue.RECEIVER_BAUDRATE`).

### Docs
- README: RJ45 pinouts for the Perfect Cue and Limitimer (pins 2 and 4 differ).

## [0.3.0] — 2026-11-04

DSAN Viewer (formerly DSAN Clock).

### Added
- **DeckLink SDI output with cut/fill.** Fill only, hardware keyer (fill on SDI A, key on SDI B) or a separate key output. Paced by the card's clock and genlock. Premultiplied or straight fill. Status line with reference lock and late or dropped frames. The output comes back on air at launch if it was on air at quit.
- **Layout editor alignment tools.**
  - Multi-select, box select, eight resize handles (Shift keeps proportions, Option resizes from the centre).
  - Smart guides to the canvas, safe areas and other elements.
  - Align, distribute, match size and fill.
  - Grid, and title- and action-safe overlays.
  - Live % and pixel readout, and undo and redo.
- **Debug console** (⇧⌘D). Raw bytes, framed packets with CRC results or rejection reasons, decoded Limitimer state, and Perfect Cue bytes with unknown bytes flagged. Link stats, a byte-by-byte breakdown of the last status packet, and save-log and raw-capture recording.
- **Limitimer DIP switch panel**, read live from the status data.
- **Choosing adapters.** Port, baud and **Connect / Disconnect** for the Limitimer and the Perfect Cue separately, with live status lines. Ports refresh when adapters are plugged in, and an adapter is found again by its USB serial number if its name changes.
- **Perfect Cue outline frame style.** Green on NEXT, red on PREV, with the width set in output pixels. New *Cue frame* preset.
- **Progress bar direction** (left/right/up/down) and what it shows (auto, remaining, elapsed).
- **Standalone Mac app** (`Build Mac App.command`) with an icon, a `.dmg`, and optional signing and notarisation. Windows build script and GitHub workflow (untested).
- **Version numbers.** One version for the whole project, a build number in each app build, the **About DSAN Viewer** box, this changelog, and `packaging/bump_version.py`.

### Changed
- Renamed the app to **DSAN Viewer**. Settings carry over.
- Full screen is now a borderless window on the chosen display, not macOS full screen, which blacked out the laptop screen. If the display is unplugged, the output drops into a window and goes back when the display returns.
- Picking a port no longer connects by itself; use **Connect**.

### Fixed
- Digits flashing every second: the Limitimer's blink flag is now only followed if you turn on *Flash with Limitimer blink*.
- DeckLink bridge failing to compile on macOS (`IID_IUnknown`).

## [0.2.0] — 2026-11-03
- DSAN Clock: video clock for a Limitimer and a Perfect Cue on two USB-RS485 adapters, with layouts, presets, an overlay mode, Detect, demo mode and OSC out.

## [0.1.0] — 2026-10-17
- `dsan` library: Limitimer and Perfect Cue protocol, threaded serial clients, HTTP/WebSocket server, OSC bridge, simulators, CLI and the Limitimer viewer.

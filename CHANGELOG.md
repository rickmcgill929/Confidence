# Changelog

Versions follow [semantic versioning](https://semver.org): **major**.**minor**.**patch**.
A patch is fixes only, a minor version adds features, a major version may change how things work.
Built apps also carry a build number (date and time of the build), shown in **About Confidence**.

## [Unreleased]

## [0.10.0] — 2026-11-08

### Added
- **New preset: Limitimer + Millumin.** The Limitimer (program, digits, time of day) on top, the current Millumin clip below (column, TRT, play state, countdown, clip name), and a Perfect Cue frame round the screen, on a black background.
- **Open the remote at `http://confidence.local:8080`.** Confidence announces itself on the network with Bonjour/mDNS, so phones, tablets and computers can use a name instead of an IP address. New **Name** field in the REMOTE box for running more than one Confidence (e.g. `stage-left.local`); if the name is already taken on the network, `-2` is added. Re-announced automatically when the network changes. Port 80 is now allowed, for an address without `:8080`.

### Changed
- The REMOTE box no longer lists names like `Mac.localdomain` that only work on the Mac itself.

## [0.9.0] — 2026-11-06

## [0.8.0] — 2026-11-06

### Added
- **DeckLink SDI output in the browser remote.** Pick the device, video mode, cut/fill (hardware keyer or separate key output), what layout goes out on SDI (same as screen, a preset or a saved layout) and premultiplied fill; start and stop the SDI output (stopping asks first); re-scan devices. Shows the same on-air status and warnings as the app. Device, mode and cut/fill are locked while on air; the layout can change live.

## [0.7.2] — 2026-11-06

### Fixed
- Crash when quitting on Macs with a DeckLink card ("Confidence quit unexpectedly"): the SDI bridge released its DeckLink devices after the Blackmagic driver library had already shut down.

## [0.7.1] — 2026-11-06

### Added
- **Remote control from a web browser.** Turn it on in the new REMOTE box (port, optional PIN) and open the address it shows on a phone, tablet or computer on the same network: live preview of the output, presenter messages, presets and saved layouts, output mode/display/show-hide, adapter choice and connect/disconnect, Demo, and the Millumin, TruCue and OSC settings. Self-contained page, no internet needed.

## [0.7.0] — 2026-11-05

### Added
- **In-app updates on the Mac with Sparkle.** Signed release builds update themselves: Check for Updates shows what's new and Install Update downloads, verifies and installs it on relaunch — as a **delta** (only what changed, usually a few MB) when one is available. Windows, Linux and unsigned builds keep the download check.
- Developer ID signing, notarization and stapling in the Mac build (picked up from your keychain locally; from encrypted secrets on GitHub), a `.zip` update archive next to the `.dmg`, and Sparkle appcasts + deltas on every release (`packaging/make_appcast.sh`). One-time setup: `packaging/setup_signing.sh`.

### Changed
- The Mac bundle version is now version + build (e.g. `0.7.0.20261106.0930`), so a release always counts as newer than a local build of an older version.

## [0.6.1] — 2026-11-05

### Fixed
- The window can be made much narrower (from 1205 to 973 pixels): the editor toolbar wraps onto a second row, the layout's file name shortens with …, and the left panel no longer scrolls sideways.
- The element list and the properties panel have a draggable divider, and the properties panel never needs a sideways scroll bar.
- The debug console's filters and buttons wrap in a narrow window (it was at least 1268 pixels wide).

## [0.6.0] — 2026-11-05

### Added
- **TruCue feedback.** A TRUCUE box receives TruCue's status feed (Settings → OSC → Companion feedback): clip, countdown to OUT, play state, loop / auto mode, next and previous clip, next bookmark and its countdown, volume. Timers, progress bars and lights get a *TruCue* source (a timer can show the bookmark countdown); labels get `{tc_clip}` `{tc_remaining}` `{tc_bookmark}`…; new *TruCue countdown* preset. **Forward to** passes the feed on so Companion's TruCue module keeps working. Library: `dsan.trucue`.

## [0.5.1] — 2026-11-05

### Fixed
- Check for Updates failed with CERTIFICATE_VERIFY_FAILED on Macs (python.org Python and apps built from it don't use the Mac's certificates). The app now carries its own certificate list (`certifi`), and a certificate problem is reported as such rather than as "Are you online?".

## [0.5.0] — 2026-11-05

### Added
- **Check for updates**: Help → Check for Updates… and a daily check at launch (can be turned off). Shows the release notes and downloads the right file for the computer — Mac (Apple silicon / Intel), Windows or Linux. Library: `dsan.updates`.
- Pushing a version tag (`v0.5.0`) now publishes a GitHub Release with the four builds and that version's changelog notes.
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

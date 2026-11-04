# dsan

Read **DSAN Limitimer** stage timers and drive **DSAN Perfect Cue** presentation remotes from Python, over HTTP/WebSocket, or as OSC.

```
pip install dsan                # library + `dsan` command
pip install 'dsan[server]'      # + HTTP/WebSocket API
pip install 'dsan[osc]'         # + OSC output
pip install 'dsan[viewer]'      # + the desktop viewer app (`dsan-viewer`)
pip install 'dsan[all]'
```

No hardware? Every entry point accepts `sim` as a port, which runs a built-in show simulator through the real decoder.

```
dsan serve --limitimer sim --perfectcue sim      # then open http://localhost:8765/
```

---

## Python library

```python
from dsan import Limitimer, PerfectCue

lt = Limitimer("/dev/tty.usbserial-A1")       # or "COM3", or "socket://10.0.0.50:4001"

@lt.on_change
def changed(state, previous):
    p1 = state.program(1)
    print(state.display(1), "running" if p1.running else "stopped",
          "WARN" if p1.warning else "", "OVER" if p1.overtime else "")

@lt.on_link
def link(status):
    print("link:", status.value)                # opening / waiting / live / stale / reconnecting / closed

lt.start()
```

```python
cue = PerfectCue("/dev/tty.usbserial-B2")      # 9600 baud by default
cue.on_button(lambda button: print("pressed", button.name))   # NEXT / PREV / BLANK_ON / BLANK_OFF
cue.start()

cue.forward()          # >FORWARD 35\r
cue.reverse()
cue.blank(on=True)
cue.send("blank_off")  # names, Command members, or raw bytes
```

Blocking style also works:

```python
with Limitimer("/dev/ttyUSB0") as lt:
    state = lt.wait_for_state(timeout=5)
    print(state.to_dict())
```

### Things worth knowing

- **Callbacks run on the device's reader thread.** Hand work off to your GUI or event loop. `dsan.viewer` (Qt signals) and `dsan.server` (`call_soon_threadsafe`) show both patterns. An exception in your callback is logged and never stops the reader.
- **Ports reconnect automatically.** If a USB adapter re-enumerates, the client retries every second (`reconnect_interval`). Pass `reconnect=False` to disable.
- **Stale detection.** Any valid packet, including the Limitimer's ping, keeps the link `LIVE`. After `stale_after` seconds of silence (default 2) it goes `STALE`. A frozen display is the dangerous failure on a show, so watch `on_link`.
- **`on_change` vs `on_state`.** `on_state` fires on every status packet, several per second. `on_change` fires only when something other than the rolling sequence counter changed.
- **Ports** can be anything pyserial's `serial_for_url` accepts, so RS-485-to-Ethernet converters work with `socket://host:port`.

### The state model

`LimitimerState` is an immutable snapshot; compare with `==`.

| | |
|---|---|
| `state.programs` | four `Program`s (`state.program(n)` for 1-based lookup) |
| `state.selected_index` / `state.selected` | selected program (0-based index / the `Program`) |
| `state.count_down` | DIP 4: countdown (True) or count-up |
| `state.continue_after_zero`, `beep_loud`, `beep_type`, `permit_changes` | DIP 5, 6, 7+8, 9 |
| `state.program_minutes`, `session_minutes` | DIP 3 (P1-3) and DIP 2 (P4): MM:SS instead of HH:MM |
| `state.display(n, overtime=True, follow_device_format=False)` | what a display shows, e.g. `"04:59"`, `"+00:20"` |
| `state.to_dict()` | JSON-ready dict (what the HTTP API returns) |

`Program` (times in seconds): `total`, `sumup`, `elapsed`, `remaining` (signed), `running`, `blink`, `beep`, `warning` (inside sum-up), `expired` (reached zero), `overtime` (past zero), `color` (0 normal / 1 warning / 2 expired).

### Pure protocol, no I/O

```python
from dsan.limitimer import Decoder, parse_status, encode_status
from dsan.perfectcue import command, decode_buttons

for event in Decoder().feed(chunk):          # any chunk size
    if event.ok and (state := parse_status(event.payload)):
        ...
command("FORWARD")                           # b'>FORWARD 35\r'
```

---

## Command line

```
dsan ports                                         # list serial ports
dsan monitor -l /dev/ttyUSB0 -p /dev/ttyUSB1       # live text view (--json for one object per line)
dsan serve   -l /dev/ttyUSB0 -p /dev/ttyUSB1       # HTTP/WebSocket API on 127.0.0.1:8765
dsan serve   -l sim --osc 127.0.0.1:5000           # + OSC output
dsan send    /dev/ttyUSB1 forward                  # one-shot Perfect Cue command
```

`serve` options: `--host 0.0.0.0` (listen on the network), `--port`, `--token SECRET` (require a bearer token), `--osc HOST:PORT`, `--osc-prefix`, `--no-bundle`, `--no-overtime`, `--follow-format`, `--stale-after`, `--reject-zero-crc`, `--pc-baud`.

> If you listen on the network, set `--token`. Without it, anyone on the LAN can advance slides.

---

## HTTP / WebSocket API

Base URL `http://HOST:8765/api/v1`. All responses are JSON and CORS is open. With `--token`, send `Authorization: Bearer SECRET` or `?token=SECRET`. `GET /` serves a small live web page that uses the WebSocket.

| Method | Path | |
|---|---|---|
| GET | `/status` | version, link state, port, packet stats for each device |
| GET | `/limitimer` | `{"link": "live", "state": {…}}` — `state` is `null` until the first packet |
| GET | `/limitimer/programs/{1-4}` | one program |
| GET | `/perfectcue` | link state and last button |
| POST | `/perfectcue/{forward\|reverse\|blank_on\|blank_off}` | send a command (aliases: `next`, `prev`) |
| POST | `/perfectcue/command` | same, with body `{"command": "forward"}` |

Display options as query parameters on the state endpoints and the WebSocket URL: `overtime=false` (hold at 00:00 / total) and `format=device` (follow DIP 2/3).

Command errors: `400` unknown command, `404` device not configured, `503` port not open right now.

### WebSocket `/api/v1/ws`

On connect the server sends a `hello` containing the full snapshot. After that it pushes:

```json
{"type": "limitimer.state", "state": { ... same shape as GET /limitimer ... }}
{"type": "limitimer.link", "link": "stale"}
{"type": "perfectcue.button", "button": "NEXT", "value": 15, "time": 1792240000.12}
{"type": "perfectcue.link", "link": "open"}
```

Clients may send:

```json
{"type": "perfectcue.command", "command": "forward", "id": 1}   → {"type": "result", "id": 1, "ok": true, ...}
{"type": "snapshot"}                                            → a fresh hello
{"type": "ping", "id": 2}                                       → {"type": "pong", "id": 2, ...}
```

`limitimer.state` is sent only on change, roughly once a second while a timer runs.

### Example: Bitfocus Companion

Use the *Generic HTTP* module with a POST to `http://HOST:8765/api/v1/perfectcue/next` to advance slides from a Stream Deck.

---

## OSC

`dsan serve --osc HOST:PORT` (or `dsan.osc.OscBridge`) sends, with prefix `/dsan` and N = 1-4:

| Address | Type | |
|---|---|---|
| `/link` | bool | Limitimer data is live |
| `/countdown` | bool | countdown vs count-up |
| `/selected` | int | selected program 1-4 |
| `/timer/N/time` | string | display string, `+` prefix when over |
| `/timer/N/seconds` | int | display seconds (unsigned) |
| `/timer/N/remaining` | int | signed seconds left |
| `/timer/N/overtime` | bool | past zero |
| `/timer/N/total`, `/sumup`, `/elapsed` | int | seconds |
| `/timer/N/running`, `/warning`, `/expired`, `/blink` | bool | |
| `/timer/N/color` | int | 0 normal · 1 warning · 2 expired |
| `/perfectcue/button` | string | `NEXT` · `PREV` · `BLANK_ON` · `BLANK_OFF` |
| `/perfectcue/value` | int | raw byte `0x0F` · `0x1F` · `0x3F` · `0x2F` |

State updates go out as one OSC bundle per change, so receivers never see half an update. Use `--no-bundle` for receivers that can't handle bundles.

---

## DSAN Clock: video clock for Limitimer + Perfect Cue

`dsan-clock` (`pip install 'dsan[clock]'`) puts the Limitimer countdown, its green/yellow/red phase lights and the Perfect Cue cue arrows on a screen. It runs on macOS and Windows and needs two USB-RS485 adapters, one per device.

![Confidence monitor preset](docs/confidence.png)

**Hardware**

| Adapter | Wired to | Serial settings |
|---|---|---|
| USB-RS485 #1 | Limitimer data port | 19200 8N1 (fixed) |
| USB-RS485 #2 | Perfect Cue receiver data port | 19200 8N1 by default; 9600–115200 selectable |

Wire the device's RS-485 data pair to the adapter's A/B (D+/D−) terminals. Check each device's manual for its RJ45 pinout, because the Limitimer and Perfect Cue pinouts differ. If **Detect** reports *"data is arriving but no valid Limitimer frames"*, swap A and B.

**Inputs**
- **Detect** listens on every USB adapter. The one carrying Limitimer packets becomes the Limitimer. The Perfect Cue is the remaining adapter, or, if there are more, whichever one sees a button press when the app prompts for it.
- Adapters are remembered by USB serial number (then by physical USB socket), so `/dev` or `COM` names changing between plugs doesn't swap them. **Swap** fixes a wrong guess by hand.
- Ports reconnect automatically. If Limitimer data stops for 2 seconds, timers show **NO SIGNAL** instead of freezing. You choose how: dim, text, hide, or keep the last time.
- **Demo** runs simulated devices through the real decoder. **Test cue** buttons fire cue states by hand.
- The Perfect Cue is input-only here. Sending LED commands goes through the receiver's separate RS-232 port (`PerfectCue.forward()` etc. in the library).

**Output**
- **Full screen** on any display, for a confidence monitor.
- **Window**, for capture into NDI or OBS.
- **Overlay**: transparent, always on top and click-through, over PowerPoint, Keynote or anything else. On macOS, pyobjc (installed with the `clock` extra) lets it float over full-screen slideshows.

**Layouts**
- Elements: timer digits, phase lights, Perfect Cue indicator (◁ ● ▷ arrows, full-screen colour light, or text), progress bar, time of day, labels (`{program}`, `{mode}` tokens) and an operator message.
- Drag to move, drag the corner to resize, arrow keys to nudge. Every element has its own colours, background with alpha, corner radius, font and alignment.
- Timers follow the Limitimer's selected program or a fixed program. Overtime and DIP 2/3 format are set per element.
- Presets: *Confidence monitor*, *Corner overlay*, *Cue light*, *Timer + time of day*, *All four programs*.
- Layouts save as JSON. Custom `.ttf`/`.otf` fonts can be bundled with a layout.
- Digits are drawn in fixed-width cells, so a running clock never shifts sideways, whatever the font.

**Operator**
- Message bar with quick buttons (WRAP UP, 1 MINUTE, PLEASE STOP).
- Optional OSC out (the same address map as above).
- Everything is remembered between launches. There's also *Connect on launch*.

## Desktop viewer

`dsan-viewer` is the PyQt6 app: four program displays, DIP switch readout, the Perfect Cue panel, OSC output, a debug console, and Demo mode (the simulator). Its port fields accept `socket://host:port` too.

---

## Protocol notes

**Limitimer**: RS-485, 19200 8N1. Frames are `0x81 | 7-bit payload | 0x83 | CRC16-MODBUS (2 bytes, big-endian, not 7-bit) | 0xFF`. Status messages are 55 bytes on the wire; type `0x10` is a ping. Because the CRC is raw, it can contain `0x81`, `0x83` or `0xFF`, so the decoder counts exactly two CRC bytes rather than scanning for the end byte. Like the Go reference, a `0x0000` checksum is accepted as "CRC disabled" (`accept_zero_crc=False` to reject). The full layout is in the `dsan.limitimer` docstring.

**Perfect Cue**: button presses arrive as single bytes (`0x0F` next, `0x1F` prev, `0x2F`/`0x3F` blank with LED off/on). The receiver's ASCII API is RS-232 9600 8N1: `>COMMAND CK\r`, where `CK` is the low byte of the sum of `COMMAND` plus the space (not the `>`), in uppercase hex.

Protocol reverse-engineering credit: Depili's [limitimer](https://gitlab.com/Depili/limitimer) project.

## Development

```
pip install -e '.[dev]'
pytest
```

Not affiliated with or endorsed by DSAN Corp. "Limitimer" and "Perfect Cue" are trademarks of their owner.

## License

MIT

"""The remote-control web page (served by remote.py). Self-contained: no internet needed."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Confidence Remote</title>
<style>
  :root {
    --bg: #0E1116; --panel: #171B22; --line: #262C36; --text: #E8ECF2; --dim: #8A93A3;
    --green: #00D06C; --yellow: #FFB000; --red: #FF3B3B; --blue: #4A8CFF; --accent: #2BB5A0;
    --r: 12px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; }
  header { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 12px;
    padding: 12px 16px; background: rgba(14,17,22,.92); backdrop-filter: blur(8px); border-bottom: 1px solid var(--line); }
  header svg { flex: none; }
  header h1 { font-size: 17px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  header .ver { color: var(--dim); font-size: 13px; }
  header .conn { margin-left: auto; font-size: 13px; color: var(--dim); display: flex; align-items: center; gap: 6px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--dim); display: inline-block; }
  main { max-width: 1100px; margin: 0 auto; padding: 16px; display: grid; gap: 16px;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); align-items: start; }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); padding: 14px 16px; }
  section.wide { grid-column: 1 / -1; }
  h2 { font-size: 12px; font-weight: 700; letter-spacing: 1.2px; color: var(--accent); margin: 0 0 10px; text-transform: uppercase; }
  .preview { width: 100%; max-height: 46vh; aspect-ratio: 16 / 9; background: #000; border-radius: 8px; display: block; object-fit: contain; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 8px 0; }
  .row > label { color: var(--dim); min-width: 92px; font-size: 14px; }
  .grow { flex: 1 1 160px; min-width: 0; }
  input, select, button { font: inherit; color: var(--text); background: #10141A; border: 1px solid var(--line);
    border-radius: 8px; padding: 9px 11px; min-height: 40px; }
  input[type=number] { width: 96px; }
  select { max-width: 100%; }
  button { background: #222935; cursor: pointer; font-weight: 600; }
  button:hover { border-color: #3A4352; }
  button:active { transform: translateY(1px); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #062019; }
  button.on { background: #0E3B2E; border-color: var(--green); color: var(--green); }
  button.warn { background: #3A2A06; border-color: var(--yellow); color: var(--yellow); }
  button:disabled { opacity: .45; cursor: default; }
  .status { font-size: 14px; margin-top: 4px; min-height: 1.4em; }
  .note { color: var(--dim); font-size: 13px; }
  .switch { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
  .switch input { appearance: none; width: 42px; height: 24px; min-height: 0; padding: 0; border-radius: 12px;
    background: #333B48; position: relative; border: 0; transition: background .15s; cursor: pointer; }
  .switch input::after { content: ""; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px;
    border-radius: 50%; background: #fff; transition: left .15s; }
  .switch input:checked { background: var(--green); }
  .switch input:checked::after { left: 21px; }
  details { border-top: 1px solid var(--line); padding-top: 8px; margin-top: 8px; }
  details:first-of-type { border-top: 0; margin-top: 0; padding-top: 0; }
  summary { cursor: pointer; font-weight: 650; padding: 4px 0; display: flex; gap: 8px; align-items: center; }
  summary .status { margin: 0 0 0 auto; font-weight: 400; font-size: 13px; text-align: right; }
  .msgon { color: var(--yellow); }
  #toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); background: #2A1214;
    color: #FFB3B3; border: 1px solid var(--red); padding: 10px 16px; border-radius: 10px; display: none; max-width: 90vw; z-index: 20; }
  #pin { position: fixed; inset: 0; background: rgba(5,7,10,.85); display: none; align-items: center; justify-content: center; z-index: 30; }
  #pin form { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); padding: 22px; width: min(92vw, 340px); }
  #pin input { width: 100%; font-size: 22px; letter-spacing: 6px; text-align: center; margin: 12px 0; }
</style>
</head>
<body>
<header>
  <svg width="28" height="28" viewBox="25.5 25.5 89 89" aria-hidden="true">
    <path d="M 101.177 88.000 A 36 36 0 0 1 36.621 56.514" fill="none" stroke="#00D06C" stroke-width="15"/>
    <path d="M 37.643 54.219 A 36 36 0 0 1 57.687 36.171" fill="none" stroke="#FFB000" stroke-width="15"/>
    <path d="M 60.077 35.395 A 36 36 0 0 1 101.177 52.000" fill="none" stroke="#FF3B3B" stroke-width="15"/>
  </svg>
  <h1>Confidence</h1><span class="ver" id="ver"></span>
  <span class="conn"><span class="dot" id="linkdot"></span><span id="link">Connecting…</span></span>
</header>

<main>
  <section class="wide">
    <img class="preview" id="preview" alt="Live output preview">
  </section>

  <section>
    <h2>Message to presenter</h2>
    <form class="row" id="msgform">
      <input class="grow" id="msgtext" placeholder="Type a message" maxlength="200" autocomplete="off">
      <button class="primary" type="submit">Send</button>
    </form>
    <div class="row" id="quick"></div>
    <div class="row"><button id="msgclear">Clear</button><span class="status" id="msgstatus"></span></div>
  </section>

  <section>
    <h2>Layout</h2>
    <div class="row"><label>Now</label><span class="grow" id="layoutname"></span></div>
    <div class="row"><label for="preset">Preset</label>
      <select class="grow" id="preset"><option value="">Load a preset…</option></select></div>
    <div class="row"><label for="saved">Saved</label>
      <select class="grow" id="saved"><option value="">Open a saved layout…</option></select></div>
  </section>

  <section>
    <h2>Output</h2>
    <div class="row"><label for="mode">Mode</label><select class="grow" id="mode"></select></div>
    <div class="row"><label for="screen">Display</label><select class="grow" id="screen"></select></div>
    <div class="row"><button id="showout">Show output</button></div>
  </section>

  <section id="dl">
    <h2>SDI output — DeckLink</h2>
    <div class="note" id="dl_reason"></div>
    <div id="dl_body">
      <div class="row"><label for="dl_device">Device</label><select class="grow" id="dl_device"></select></div>
      <div class="row"><label for="dl_mode">Video mode</label><select class="grow" id="dl_mode"></select></div>
      <div class="row"><label for="dl_keying">Cut / fill</label><select class="grow" id="dl_keying"></select></div>
      <div class="row" id="dl_keyrow"><label for="dl_key">Key output</label><select class="grow" id="dl_key"></select></div>
      <div class="row"><label for="dl_layout">Layout</label><select class="grow" id="dl_layout"></select></div>
      <div class="row" id="dl_premultrow"><label></label><label class="switch"><input type="checkbox" id="dl_premult"> Premultiplied fill</label></div>
      <div class="row"><button id="dl_onair" type="button">Start SDI output</button>
        <button id="dl_rescan" type="button" title="Re-scan DeckLink devices">⟳ Re-scan</button></div>
    </div>
    <div class="status" id="dl_status"></div>
    <div class="status" id="dl_warning" style="color: var(--yellow)"></div>
  </section>

  <section>
    <h2>Inputs</h2>
    <div id="inputs"></div>
    <div class="row"><label class="switch"><input type="checkbox" id="demo"> Demo (simulated devices)</label></div>
  </section>

  <section class="wide">
    <h2>Feeds</h2>
    <details>
      <summary>Millumin <span class="status" id="mm_status"></span></summary>
      <div class="row"><label class="switch"><input type="checkbox" id="mm_enabled"> Receive feedback</label></div>
      <div class="row"><label for="mm_port">Port</label><input type="number" id="mm_port" min="1024" max="65535">
        <label for="mm_layer">Layer</label><input class="grow" id="mm_layer" list="mm_layers" placeholder="Selected layer">
        <datalist id="mm_layers"></datalist></div>
      <div class="row"><label>Last seconds</label>yellow <input type="number" id="mm_yellow" min="0" max="3600">
        red <input type="number" id="mm_red" min="0" max="3600"><button data-apply="millumin">Apply</button></div>
    </details>
    <details>
      <summary>TruCue <span class="status" id="tc_status"></span></summary>
      <div class="row"><label class="switch"><input type="checkbox" id="tc_enabled"> Receive feedback</label></div>
      <div class="row"><label for="tc_port">Port</label><input type="number" id="tc_port" min="1024" max="65535">
        <label for="tc_forward">Forward</label><input class="grow" id="tc_forward" placeholder="off — e.g. 127.0.0.1:9018"></div>
      <div class="row"><label>Last seconds</label>yellow <input type="number" id="tc_yellow" min="0" max="3600">
        red <input type="number" id="tc_red" min="0" max="3600"><button data-apply="trucue">Apply</button></div>
    </details>
    <details>
      <summary>OSC <span class="status" id="osc_status"></span></summary>
      <div class="row"><label class="switch"><input type="checkbox" id="osc_send"> Send OSC</label>
        <input class="grow" id="osc_target" placeholder="127.0.0.1:5000">
        <input id="osc_prefix" placeholder="/dsan" style="width:110px"></div>
      <div class="row"><label class="switch"><input type="checkbox" id="osc_receive"> Receive (Perfect Cue control)</label>
        <input type="number" id="osc_port" min="1024" max="65535"><button data-apply="osc">Apply</button></div>
    </details>
  </section>
</main>

<div id="toast" role="alert"></div>
<div id="pin"><form id="pinform">
  <h2>PIN</h2>
  <div class="note">This Confidence asks for a PIN. It's shown in the app's REMOTE box.</div>
  <input id="pinvalue" inputmode="numeric" autocomplete="current-password" type="password" required>
  <button class="primary" type="submit" style="width:100%">Unlock</button>
</form></div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
let state = null, pin = "";
try { pin = localStorage.getItem("confidence_pin") || ""; } catch (e) {}

function headers(extra) { return Object.assign({ "X-PIN": pin }, extra || {}); }
function toast(text) {
  const t = $("toast"); t.textContent = text; t.style.display = "block";
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = "none", 4000);
}
function needPin() { $("pin").style.display = "flex"; $("pinvalue").focus(); }
$("pinform").addEventListener("submit", (e) => {
  e.preventDefault(); pin = $("pinvalue").value.trim();
  try { localStorage.setItem("confidence_pin", pin); } catch (err) {}
  $("pin").style.display = "none"; refresh();
});

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ cache: "no-store" }, opts, { headers: headers((opts || {}).headers) }));
  if (r.status === 401) { needPin(); throw new Error("PIN required"); }
  return r;
}
async function act(body) {
  try {
    const r = await api("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json();
    if (!r.ok) { toast(data.error || "That didn't work"); return; }
    render(data);
  } catch (e) { if (e.message !== "PIN required") toast("Couldn't reach Confidence"); }
}

function setStatus(el, s) { el.textContent = s ? s.text : ""; el.style.color = s && s.color ? s.color : ""; }
function fillSelect(sel, items, value) {             // items: [{value,label}] — keeps the list if unchanged
  const key = JSON.stringify(items);
  if (sel.dataset.key !== key) {
    const keepFirst = sel.dataset.first === "1" ? sel.options[0] : null;
    sel.textContent = "";
    if (keepFirst) sel.appendChild(keepFirst);
    for (const it of items) { const o = document.createElement("option"); o.value = it.value; o.textContent = it.label; sel.appendChild(o); }
    sel.dataset.key = key;
  }
  if (value !== undefined && document.activeElement !== sel) sel.value = value;
}
function setVal(el, v) { if (document.activeElement !== el) { if (el.type === "checkbox") el.checked = !!v; else el.value = v; } }

function render(s) {
  state = s;
  $("ver").textContent = "v" + s.app.version;
  // message
  const quick = $("quick");
  if (!quick.childElementCount) for (const q of s.quick) {
    const b = document.createElement("button"); b.textContent = q; b.className = "warn"; b.type = "button";
    b.addEventListener("click", () => act({ action: "message", text: q })); quick.appendChild(b);
  }
  const ms = $("msgstatus");
  if (s.message) { ms.textContent = "On screen: " + s.message; ms.className = "status msgon"; }
  else { ms.textContent = "Nothing on screen"; ms.className = "status note"; }
  if (s.message && !s.layout.has_message) { ms.textContent = "This layout has no Message element"; ms.className = "status"; ms.style.color = "var(--red)"; }
  // layout
  $("layoutname").textContent = s.layout.name + (s.layout.file ? " — " + s.layout.file : "");
  $("preset").dataset.first = "1"; $("saved").dataset.first = "1";
  fillSelect($("preset"), s.layout.presets.map((p) => ({ value: p, label: p })), "");
  fillSelect($("saved"), s.layout.saved.map((p) => ({ value: p, label: p.replace(/\.json$/i, "") })), "");
  // output
  fillSelect($("mode"), s.output.modes, s.output.mode);
  fillSelect($("screen"), s.output.screens, s.output.screen);
  const so = $("showout"); so.textContent = s.output.shown ? "Hide output" : "Show output"; so.className = s.output.shown ? "on" : "";
  // inputs
  const box = $("inputs");
  for (const kind of ["lt", "pc"]) {
    const d = s.inputs[kind];
    let card = $("in_" + kind);
    if (!card) {
      card = document.createElement("div"); card.id = "in_" + kind;
      card.innerHTML = '<div class="row"><label></label><select class="grow"></select><button type="button"></button></div><div class="status"></div>';
      card.querySelector("label").textContent = d.name;
      card.querySelector("select").addEventListener("change", (e) => act({ action: "port", kind, value: e.target.value }));
      card.querySelector("button").addEventListener("click", () => act({ action: "connect", kind, on: !state.inputs[kind].connected }));
      box.appendChild(card);
    }
    const sel = card.querySelector("select"); sel.disabled = s.demo;
    fillSelect(sel, d.ports, d.port);
    const b = card.querySelector("button"); b.textContent = d.connected ? "Disconnect" : "Connect"; b.className = d.connected ? "on" : ""; b.disabled = s.demo;
    setStatus(card.querySelector(".status"), d.status);
  }
  setVal($("demo"), s.demo);
  // feeds
  const mm = s.millumin, tc = s.trucue, osc = s.osc;
  setVal($("mm_enabled"), mm.enabled); setVal($("mm_port"), mm.port); setVal($("mm_layer"), mm.layer);
  setVal($("mm_yellow"), mm.yellow); setVal($("mm_red"), mm.red); setStatus($("mm_status"), mm.enabled ? mm.status : { text: "off" });
  const dl = $("mm_layers"); if (dl.dataset.key !== JSON.stringify(mm.layers)) { dl.textContent = ""; for (const l of mm.layers) { const o = document.createElement("option"); o.value = l; dl.appendChild(o); } dl.dataset.key = JSON.stringify(mm.layers); }
  setVal($("tc_enabled"), tc.enabled); setVal($("tc_port"), tc.port); setVal($("tc_forward"), tc.forward);
  setVal($("tc_yellow"), tc.yellow); setVal($("tc_red"), tc.red); setStatus($("tc_status"), tc.enabled ? tc.status : { text: "off" });
  setVal($("osc_send"), osc.send); setVal($("osc_target"), osc.target); setVal($("osc_prefix"), osc.prefix);
  setVal($("osc_receive"), osc.receive); setVal($("osc_port"), osc.port);
  setStatus($("osc_status"), osc.receive ? osc.status : { text: osc.send ? "sending" : "off" });
  // DeckLink
  const k = s.decklink;
  $("dl_reason").textContent = k.reason;
  $("dl_body").style.display = k.available ? "" : "none";
  if (k.available) {
    fillSelect($("dl_device"), k.devices, k.device);
    fillSelect($("dl_mode"), k.modes, k.mode);
    fillSelect($("dl_keying"), k.keyings, k.keying);
    for (const o of $("dl_keying").options) { const it = k.keyings.find((x) => x.value === o.value); o.disabled = !!it && !it.enabled; }
    fillSelect($("dl_key"), k.key_devices, k.key_device);
    fillSelect($("dl_layout"), k.layouts, k.layout);
    $("dl_keyrow").style.display = k.keying === "separate" ? "" : "none";
    setVal($("dl_premult"), k.premultiplied);
    $("dl_premult").disabled = k.keying === "none";
    for (const id of ["dl_device", "dl_mode", "dl_keying", "dl_key"]) $(id).disabled = k.on_air;
    $("dl_rescan").disabled = k.on_air;
    const b = $("dl_onair");
    b.textContent = k.on_air ? "Stop SDI output" : "Start SDI output";
    b.className = k.on_air ? "on" : "primary";
    b.disabled = !k.on_air && !k.can_start;
  }
  setStatus($("dl_status"), k.status);
  $("dl_warning").textContent = k.warning;
}

// controls
$("msgform").addEventListener("submit", (e) => { e.preventDefault(); const t = $("msgtext").value.trim(); if (t) { act({ action: "message", text: t }); $("msgtext").value = ""; } });
$("msgclear").addEventListener("click", () => act({ action: "clear_message" }));
$("preset").addEventListener("change", (e) => { if (e.target.value) act({ action: "preset", name: e.target.value }); e.target.value = ""; });
$("saved").addEventListener("change", (e) => { if (e.target.value) act({ action: "open_layout", file: e.target.value }); e.target.value = ""; });
$("mode").addEventListener("change", (e) => act({ action: "output", mode: e.target.value }));
$("screen").addEventListener("change", (e) => act({ action: "output", screen: e.target.value }));
$("showout").addEventListener("click", () => act({ action: "output", shown: !state.output.shown }));
$("demo").addEventListener("change", (e) => act({ action: "demo", on: e.target.checked }));
$("mm_enabled").addEventListener("change", (e) => act({ action: "millumin", enabled: e.target.checked }));
$("tc_enabled").addEventListener("change", (e) => act({ action: "trucue", enabled: e.target.checked }));
$("osc_send").addEventListener("change", (e) => act({ action: "osc", send: e.target.checked }));
$("osc_receive").addEventListener("change", (e) => act({ action: "osc", receive: e.target.checked }));
$("dl_device").addEventListener("change", (e) => act({ action: "decklink", device: e.target.value }));
$("dl_mode").addEventListener("change", (e) => act({ action: "decklink", mode: e.target.value }));
$("dl_keying").addEventListener("change", (e) => act({ action: "decklink", keying: e.target.value }));
$("dl_key").addEventListener("change", (e) => act({ action: "decklink", key_device: e.target.value }));
$("dl_layout").addEventListener("change", (e) => act({ action: "decklink", layout: e.target.value }));
$("dl_premult").addEventListener("change", (e) => act({ action: "decklink", premultiplied: e.target.checked }));
$("dl_rescan").addEventListener("click", () => act({ action: "decklink", rescan: true }));
$("dl_onair").addEventListener("click", () => {
  const on = !state.decklink.on_air;
  if (!on && !confirm("Take the SDI output off air?")) return;
  act({ action: "decklink", on });
});
for (const b of document.querySelectorAll("[data-apply]")) b.addEventListener("click", () => {
  const k = b.dataset.apply;
  if (k === "millumin") act({ action: "millumin", port: +$("mm_port").value, layer: $("mm_layer").value, yellow: +$("mm_yellow").value, red: +$("mm_red").value });
  if (k === "trucue") act({ action: "trucue", port: +$("tc_port").value, forward: $("tc_forward").value, yellow: +$("tc_yellow").value, red: +$("tc_red").value });
  if (k === "osc") act({ action: "osc", target: $("osc_target").value, prefix: $("osc_prefix").value, port: +$("osc_port").value });
});

// polling
let failures = 0, previewUrl = null, busyPreview = false;
async function refresh() {
  try {
    const r = await api("/api/state");
    if (!r.ok) throw new Error();
    render(await r.json()); failures = 0;
    $("link").textContent = "Connected"; $("linkdot").style.background = "var(--green)";
  } catch (e) {
    if (e.message === "PIN required") return;
    failures++; $("link").textContent = failures > 2 ? "Can't reach Confidence" : "Reconnecting…";
    $("linkdot").style.background = failures > 2 ? "var(--red)" : "var(--yellow)";
  }
}
async function refreshPreview() {
  if (busyPreview || document.hidden || failures > 2) return;
  busyPreview = true;
  try {
    const img = $("preview"), w = Math.min(1280, Math.round(img.clientWidth * (window.devicePixelRatio || 1))) || 640;
    const r = await api("/api/preview.png?w=" + w);
    if (r.ok) {
      const url = URL.createObjectURL(await r.blob());
      img.onload = () => { if (previewUrl) URL.revokeObjectURL(previewUrl); previewUrl = url; };
      img.src = url;
      if (state) img.style.aspectRatio = "auto";
    }
  } catch (e) {} finally { busyPreview = false; }
}
refresh(); refreshPreview();
setInterval(refresh, 1000);
setInterval(refreshPreview, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); refreshPreview(); } });
</script>
</body>
</html>
"""

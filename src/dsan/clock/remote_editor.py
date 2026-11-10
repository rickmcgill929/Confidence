"""The browser layout editor (served at /editor by remote.py). Self-contained: no internet needed.

It edits the layout that's on Confidence right now, through the same calls as the app's
own editor (``remote_layout.py``): changes show on the output at once, the app's editor
follows, and Undo covers them. Drag to move, handles to resize (snapping to the canvas
and other elements), the element list, the properties of the selected element (or the
layout's when nothing is selected), save / save as, import a .json, download.
"""

EDITOR = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Layout editor — Confidence</title>
<style>
  :root {
    --bg: #0E1116; --panel: #171B22; --line: #262C36; --text: #E8ECF2; --dim: #8A93A3;
    --green: #00D06C; --yellow: #FFB000; --red: #FF3B3B; --blue: #4A8CFF; --accent: #2BB5A0; --pink: #FF4FA3;
    --r: 12px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; }
  header { position: sticky; top: 0; z-index: 10; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px;
    padding: 10px 16px; background: rgba(14,17,22,.94); backdrop-filter: blur(8px); border-bottom: 1px solid var(--line); }
  header a.back { color: var(--accent); text-decoration: none; font-weight: 600; white-space: nowrap; }
  header h1 { font-size: 16px; margin: 0; font-weight: 650; white-space: nowrap; }
  header .file { color: var(--dim); font-size: 13px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1 1 120px; }
  .tools { display: flex; flex-wrap: wrap; gap: 6px; }
  main { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; padding: 16px; max-width: 1500px; margin: 0 auto; align-items: start; }
  @media (max-width: 900px) { main { grid-template-columns: minmax(0, 1fr); } }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); padding: 12px 14px; }
  h2 { font-size: 12px; font-weight: 700; letter-spacing: 1.2px; color: var(--accent); margin: 0 0 8px; text-transform: uppercase; }
  input, select, button, textarea { font: inherit; color: var(--text); background: #10141A; border: 1px solid var(--line);
    border-radius: 8px; padding: 7px 9px; min-height: 36px; }
  button { background: #222935; cursor: pointer; font-weight: 600; white-space: nowrap; }
  button:hover { border-color: #3A4352; }
  button:disabled { opacity: .4; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #062019; }
  button.danger { color: #FF8A8A; }
  .note { color: var(--dim); font-size: 13px; }
  .side { display: grid; gap: 16px; }
  /* canvas */
  #stagewrap { position: sticky; top: 70px; }
  @media (max-width: 900px) { #stagewrap { position: static; } }
  #stage { position: relative; width: 100%; background: #000 repeating-conic-gradient(#151515 0 25%, #0b0b0b 0 50%) 0 0 / 20px 20px;
    border-radius: 6px; overflow: hidden; touch-action: none; user-select: none; -webkit-user-select: none; }
  #pv { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  .box { position: absolute; border: 1px dashed rgba(255,255,255,.28); cursor: move; touch-action: none; }
  .box.hidden { border-color: rgba(255,255,255,.12); background: rgba(255,255,255,.03); }
  .box.sel { border: 2px solid var(--accent); z-index: 3; }
  .box .tag { position: absolute; left: -2px; top: -22px; font-size: 11px; background: var(--accent); color: #062019;
    padding: 1px 6px; border-radius: 4px; white-space: nowrap; display: none; pointer-events: none; font-weight: 700; }
  .box.sel .tag { display: block; }
  .box.top .tag { top: auto; bottom: -22px; }
  .h { position: absolute; width: 12px; height: 12px; background: #fff; border: 2px solid var(--accent); border-radius: 3px; display: none; touch-action: none; }
  .h::before { content: ""; position: absolute; inset: -10px; }      /* bigger touch target */
  .box.sel .h { display: block; }
  .h[data-d=nw] { left: -7px; top: -7px; cursor: nwse-resize; } .h[data-d=n] { left: calc(50% - 6px); top: -7px; cursor: ns-resize; }
  .h[data-d=ne] { right: -7px; top: -7px; cursor: nesw-resize; } .h[data-d=e] { right: -7px; top: calc(50% - 6px); cursor: ew-resize; }
  .h[data-d=se] { right: -7px; bottom: -7px; cursor: nwse-resize; } .h[data-d=s] { left: calc(50% - 6px); bottom: -7px; cursor: ns-resize; }
  .h[data-d=sw] { left: -7px; bottom: -7px; cursor: nesw-resize; } .h[data-d=w] { left: -7px; top: calc(50% - 6px); cursor: ew-resize; }
  .guide { position: absolute; pointer-events: none; z-index: 4; }
  .guide.v { top: 0; bottom: 0; width: 0; border-left: 1px solid var(--pink); }
  .guide.hz { left: 0; right: 0; height: 0; border-top: 1px solid var(--pink); }
  .guide.canvas { border-color: var(--accent); }
  .readout { display: flex; justify-content: space-between; gap: 8px; margin-top: 6px; font-size: 13px; color: var(--dim); font-variant-numeric: tabular-nums; }
  /* list */
  #list { list-style: none; margin: 0; padding: 0; max-height: 300px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
  #list li { display: flex; align-items: center; gap: 8px; padding: 7px 9px; border-bottom: 1px solid var(--line); cursor: pointer; }
  #list li:last-child { border-bottom: 0; }
  #list li.sel { background: #123B35; }
  #list li.lay { color: var(--accent); font-weight: 600; }
  #list li .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #list li .ty { color: var(--dim); font-size: 12px; white-space: nowrap; }
  #list input { min-height: 0; width: 18px; height: 18px; accent-color: var(--accent); }
  .rowbtns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  /* properties */
  .form { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 10px; align-items: center; }
  .form > label { color: var(--dim); font-size: 14px; text-align: right; }
  .form .full { grid-column: 1 / -1; }
  .form input[type=number] { width: 100%; }
  .form select, .form input[type=text], .form textarea { width: 100%; min-width: 0; }
  .colorf { display: flex; gap: 6px; align-items: center; }
  .colorf input[type=color] { width: 46px; height: 36px; padding: 2px; }
  .form .colorf input.hex { flex: 1 1 auto; width: auto; min-width: 0; font-family: ui-monospace, Menlo, monospace; }
  .form .colorf input.alpha { flex: none; width: 72px; }
  .sw { appearance: none; width: 42px; height: 24px; min-height: 0; padding: 0; border-radius: 12px; background: #333B48;
    position: relative; border: 0; cursor: pointer; }
  .sw::after { content: ""; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px; border-radius: 50%; background: #fff; transition: left .15s; }
  .sw:checked { background: var(--green); } .sw:checked::after { left: 21px; }
  .ptitle { color: var(--accent); font-weight: 650; margin-bottom: 8px; }
  #toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); background: #2A1214; color: #FFB3B3;
    border: 1px solid var(--red); padding: 10px 16px; border-radius: 10px; display: none; max-width: 90vw; z-index: 40; }
  #toast.ok { background: #0E2A22; color: #A8F0D6; border-color: var(--accent); }
  #pin { position: fixed; inset: 0; background: rgba(5,7,10,.85); display: none; align-items: center; justify-content: center; z-index: 50; }
  #pin form { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); padding: 22px; width: min(92vw, 340px); }
  #pin input { width: 100%; font-size: 22px; letter-spacing: 6px; text-align: center; margin: 12px 0; }
</style>
</head>
<body>
<header>
  <a class="back" href="/">← Remote</a>
  <h1>Layout editor</h1>
  <span class="file" id="file"></span>
  <div class="tools">
    <button id="undo" type="button" title="Undo (⌘Z)">↶ Undo</button>
    <button id="redo" type="button" title="Redo (⇧⌘Z)">↷ Redo</button>
    <button id="save" type="button" class="primary" title="Save (⌘S)">Save</button>
    <button id="saveas" type="button">Save as…</button>
    <button id="import" type="button">Import…</button>
    <button id="download" type="button">Download</button>
    <button id="new" type="button">New</button>
  </div>
  <input type="file" id="importfile" accept=".json,application/json" hidden>
</header>

<main>
  <div id="stagewrap">
    <div id="stage"><img id="pv" alt=""><div id="boxes"></div></div>
    <div class="readout"><span id="sel_read">Tap an element to select it · drag to move · handles resize</span><span id="px"></span></div>
  </div>
  <div class="side">
    <section>
      <h2>Elements</h2>
      <ul id="list"></ul>
      <div class="rowbtns">
        <select id="addtype" aria-label="Element type"></select><button id="add" type="button">Add</button>
        <button id="dup" type="button" title="Duplicate (⌘D)">Duplicate</button>
        <button id="front" type="button" title="Bring forward">▲</button>
        <button id="back" type="button" title="Send backward">▼</button>
        <button id="del" type="button" class="danger" title="Delete">Delete</button>
      </div>
    </section>
    <section>
      <h2 id="ptitle">Properties</h2>
      <div id="props"></div>
    </section>
  </div>
</main>
<div id="toast"></div>
<div id="pin"><form id="pinform">
  <b>Enter the PIN</b><div class="note">Set in Confidence's REMOTE box.</div>
  <input id="pininput" inputmode="numeric" autocomplete="off" autofocus>
  <button class="primary" style="width:100%">Unlock</button>
</form></div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
let pin = "";
try { pin = localStorage.getItem("confidence_pin") || ""; } catch (e) {}
let schema = null, st = null, sel = null, drag = null, dragging = false;
let toastTimer = 0;

function toast(text, ok) {
  const t = $("toast"); t.textContent = text; t.className = ok ? "ok" : ""; t.style.display = "block";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.style.display = "none"; }, ok ? 2200 : 4000);
}
function headers(extra) { return Object.assign(pin ? { "X-PIN": pin } : {}, extra || {}); }
function needPin() { $("pin").style.display = "flex"; $("pininput").focus(); }
$("pinform").addEventListener("submit", (e) => {
  e.preventDefault(); pin = $("pininput").value.trim();
  try { localStorage.setItem("confidence_pin", pin); } catch (err) {}
  $("pin").style.display = "none"; boot();
});
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ cache: "no-store" }, opts, { headers: headers((opts || {}).headers) }));
  if (r.status === 401) { needPin(); throw new Error("PIN required"); }
  return r;
}
async function post(body, quiet) {
  try {
    const r = await api("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json();
    if (!r.ok) { if (!quiet) toast(data.error || "That didn't work"); return { error: data.error || "error" }; }
    if (data.state) apply(data.state);
    return data;
  } catch (e) { if (e.message !== "PIN required") toast("Couldn't reach Confidence"); return { error: String(e) }; }
}
const edit = (op, extra, quiet) => post(Object.assign({ action: "layout_edit", op }, extra || {}), quiet);

// ── state ────────────────────────────────────────────────────────────────────
const els = () => (st ? st.layout.elements : []);
const find = (id) => els().find((e) => e.id === id) || null;
const typeLabel = (t) => { const x = schema.types.find((y) => y.value === t); return x ? x.label : t; };

function apply(s) {
  if (s.same) return;
  st = s;
  if (sel && !find(sel)) sel = null;
  $("file").textContent = `${s.layout.name} — ${s.file || "not saved to a file"}`;
  $("undo").disabled = !s.can_undo; $("redo").disabled = !s.can_redo;
  $("px").textContent = `${s.pixels[0]} × ${s.pixels[1]} px`;
  $("stage").style.aspectRatio = String(s.aspect || 16 / 9);
  if (!dragging) drawBoxes();
  drawList();
  drawProps();
}
function select(id) {
  sel = id; drawBoxes(); drawList(); drawProps(true);
}

// ── canvas ───────────────────────────────────────────────────────────────────
const pct = (v) => `${(v * 100).toFixed(3)}%`;
function placeBox(div, e) {
  div.style.left = pct(e.x); div.style.top = pct(e.y); div.style.width = pct(e.w); div.style.height = pct(e.h);
  div.classList.toggle("top", e.y < 0.06);
}
function drawBoxes() {
  const root = $("boxes"); root.textContent = "";
  for (const e of els()) {
    const d = document.createElement("div");
    d.className = "box" + (e.visible ? "" : " hidden") + (e.id === sel ? " sel" : "");
    d.dataset.id = e.id; placeBox(d, e);
    const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = e.name; d.appendChild(tag);
    for (const dir of ["nw", "n", "ne", "e", "se", "s", "sw", "w"]) {
      const h = document.createElement("div"); h.className = "h"; h.dataset.d = dir; d.appendChild(h);
    }
    root.appendChild(d);
  }
  readout();
}
function readout() {
  const e = find(sel);
  if (!e) { $("sel_read").textContent = "Tap an element to select it · drag to move · handles resize"; return; }
  const px = st.pixels;
  $("sel_read").textContent = `${e.name}: x ${(e.x * 100).toFixed(1)}% y ${(e.y * 100).toFixed(1)}%  ` +
    `${(e.w * 100).toFixed(1)} × ${(e.h * 100).toFixed(1)}%  (${Math.round(e.w * px[0])} × ${Math.round(e.h * px[1])} px)`;
}

// snapping: canvas edges + center, other elements' edges + centers
function snapLines(axis, skip) {
  const out = [[0, "canvas"], [0.5, "canvas"], [1, "canvas"]];
  for (const e of els()) {
    if (e.id === skip || !e.visible) continue;
    const a = axis === "x" ? e.x : e.y, s = axis === "x" ? e.w : e.h;
    out.push([a, "el"], [a + s / 2, "el"], [a + s, "el"]);
  }
  return out;
}
function snap1(values, lines, tol) {        // best (delta, line) to move one of values onto a line
  let best = null;
  for (const v of values) for (const [l, kind] of lines) {
    const d = l - v;
    if (Math.abs(d) <= tol && (!best || Math.abs(d) < Math.abs(best.d))) best = { d, l, kind };
  }
  return best;
}
function showGuides(gs) {
  for (const g of document.querySelectorAll(".guide")) g.remove();
  for (const [axis, l, kind] of gs) {
    const d = document.createElement("div"); d.className = "guide " + (axis === "x" ? "v" : "hz") + (kind === "canvas" ? " canvas" : "");
    if (axis === "x") d.style.left = pct(l); else d.style.top = pct(l);
    $("stage").appendChild(d);
  }
}

let sendTimer = 0, pendingGeom = null;
function queueGeom(id, g, now) {
  pendingGeom = { id, g };
  if (now) { clearTimeout(sendTimer); sendTimer = 0; return flushGeom(); }
  if (!sendTimer) sendTimer = setTimeout(() => { sendTimer = 0; flushGeom(); }, 110);
}
async function flushGeom() {
  if (!pendingGeom) return;
  const { id, g } = pendingGeom; pendingGeom = null;
  const r = await post({ action: "layout_edit", op: "set", id, fields: g }, true);
  if (r && r.error) toast(r.error);
}

$("stage").addEventListener("pointerdown", (ev) => {
  const handle = ev.target.closest(".h"), box = ev.target.closest(".box");
  if (!box) { if (sel) select(null); return; }
  const id = box.dataset.id, e = find(id);
  if (!e) return;
  if (id !== sel) select(id);
  const nb = $("boxes").querySelector(`.box[data-id="${id}"]`);
  const r = $("stage").getBoundingClientRect();
  drag = { id, dir: handle ? handle.dataset.d : null, sx: ev.clientX, sy: ev.clientY, rw: r.width, rh: r.height,
           o: { x: e.x, y: e.y, w: e.w, h: e.h }, box: nb, moved: false, alt: ev.altKey, shift: ev.shiftKey };
  dragging = true;
  $("stage").setPointerCapture(ev.pointerId);
  ev.preventDefault();
});
$("stage").addEventListener("pointermove", (ev) => {
  if (!drag) return;
  const dx = (ev.clientX - drag.sx) / drag.rw, dy = (ev.clientY - drag.sy) / drag.rh;
  if (!drag.moved && Math.hypot(ev.clientX - drag.sx, ev.clientY - drag.sy) < 3) return;
  drag.moved = true;
  const o = drag.o, e = find(drag.id), tolx = 7 / drag.rw, toly = 7 / drag.rh, MIN = 0.01;
  let { x, y, w, h } = o; const guides = [];
  if (!drag.dir) {
    x = Math.min(Math.max(0, o.x + dx), 1 - o.w); y = Math.min(Math.max(0, o.y + dy), 1 - o.h);
    if (!ev.altKey) {
      const sx = snap1([x, x + w / 2, x + w], snapLines("x", drag.id), tolx);
      if (sx) { x = Math.min(Math.max(0, x + sx.d), 1 - w); guides.push(["x", sx.l, sx.kind]); }
      const sy = snap1([y, y + h / 2, y + h], snapLines("y", drag.id), toly);
      if (sy) { y = Math.min(Math.max(0, y + sy.d), 1 - h); guides.push(["y", sy.l, sy.kind]); }
    }
  } else {
    const d = drag.dir, ratio = o.w / o.h;
    let l = o.x, t = o.y, rr = o.x + o.w, b = o.y + o.h;
    if (d.includes("w")) l = Math.min(o.x + dx, rr - MIN); if (d.includes("e")) rr = Math.max(o.x + o.w + dx, l + MIN);
    if (d.includes("n")) t = Math.min(o.y + dy, b - MIN); if (d.includes("s")) b = Math.max(o.y + o.h + dy, t + MIN);
    if (!ev.altKey) {
      const lx = snapLines("x", drag.id), ly = snapLines("y", drag.id);
      if (d.includes("w")) { const s = snap1([l], lx, tolx); if (s) { l += s.d; guides.push(["x", s.l, s.kind]); } }
      if (d.includes("e")) { const s = snap1([rr], lx, tolx); if (s) { rr += s.d; guides.push(["x", s.l, s.kind]); } }
      if (d.includes("n")) { const s = snap1([t], ly, toly); if (s) { t += s.d; guides.push(["y", s.l, s.kind]); } }
      if (d.includes("s")) { const s = snap1([b], ly, toly); if (s) { b += s.d; guides.push(["y", s.l, s.kind]); } }
    }
    l = Math.max(0, l); t = Math.max(0, t); rr = Math.min(1, rr); b = Math.min(1, b);
    x = l; y = t; w = Math.max(MIN, rr - l); h = Math.max(MIN, b - t);
    if (ev.shiftKey && d.length === 2) {                 // Shift on a corner keeps proportions
      if (w / h > ratio) w = h * ratio; else h = w / ratio;
      if (d.includes("w")) x = o.x + o.w - w; if (d.includes("n")) y = o.y + o.h - h;
    }
  }
  const g = { x: +x.toFixed(4), y: +y.toFixed(4), w: +w.toFixed(4), h: +h.toFixed(4) };
  Object.assign(e, g); placeBox(drag.box, e); showGuides(guides); readout();
  queueGeom(drag.id, g, false);
});
async function endDrag() {
  if (!drag) return;
  const d = drag; drag = null; showGuides([]);
  if (d.moved) { const e = find(d.id); if (e) await queueGeom(d.id, { x: e.x, y: e.y, w: e.w, h: e.h }, true); }
  dragging = false; drawBoxes();
}
$("stage").addEventListener("pointerup", endDrag);
$("stage").addEventListener("pointercancel", endDrag);

// live preview of the real output
let pvBusy = false, pvUrl = null;
async function refreshPreview() {
  if (pvBusy || document.hidden) return;
  pvBusy = true;
  try {
    const w = Math.min(1280, Math.round($("stage").clientWidth * (window.devicePixelRatio || 1)));
    const r = await api(`/api/preview.png?w=${w}`);
    if (r.ok) { const u = URL.createObjectURL(await r.blob()); $("pv").src = u; if (pvUrl) URL.revokeObjectURL(pvUrl); pvUrl = u; }
  } catch (e) {} finally { pvBusy = false; }
}

// ── list ─────────────────────────────────────────────────────────────────────
function drawList() {
  const ul = $("list"); ul.textContent = "";
  const lay = document.createElement("li"); lay.className = "lay" + (sel ? "" : " sel");
  lay.innerHTML = '<span class="nm">⚙ Layout settings</span><span class="ty">name, background, colors</span>';
  lay.title = "Layout settings (Esc)"; lay.addEventListener("click", () => select(null)); ul.appendChild(lay);
  const list = els().slice().reverse();              // top of the list = in front
  for (const e of list) {
    const li = document.createElement("li"); li.dataset.id = e.id; if (e.id === sel) li.className = "sel";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = e.visible; cb.title = "Visible";
    cb.addEventListener("click", (ev) => ev.stopPropagation());
    cb.addEventListener("change", () => edit("set", { id: e.id, fields: { visible: cb.checked } }));
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = e.name;
    const ty = document.createElement("span"); ty.className = "ty"; ty.textContent = typeLabel(e.type).split(" (")[0];
    li.append(cb, nm, ty); li.addEventListener("click", () => select(e.id)); ul.appendChild(li);
  }
  if (!list.length) { const li = document.createElement("li"); li.className = "note"; li.textContent = "No elements yet — add one below."; ul.appendChild(li); }
  for (const id of ["dup", "front", "back", "del"]) $(id).disabled = !sel;
}

// ── properties ───────────────────────────────────────────────────────────────
const hex2 = (n) => n.toString(16).padStart(2, "0").toUpperCase();
function splitColor(c) {
  let s = (c || "#000000").replace("#", "");
  if (s.length === 3) s = s.split("").map((x) => x + x).join("");
  return { rgb: "#" + s.slice(0, 6).toUpperCase(), a: s.length >= 8 ? parseInt(s.slice(6, 8), 16) : 255 };
}
function colorField(value, alpha, onChange) {
  const wrap = document.createElement("div"); wrap.className = "colorf";
  const pick = document.createElement("input"); pick.type = "color";
  const hex = document.createElement("input"); hex.type = "text"; hex.className = "hex"; hex.spellcheck = false;
  const al = document.createElement("input"); al.type = "number"; al.className = "alpha"; al.min = 0; al.max = 100; al.title = "Opacity %";
  const set = (v) => { const c = splitColor(v); pick.value = c.rgb.toLowerCase(); hex.value = v.toUpperCase(); al.value = Math.round(c.a / 2.55); };
  const send = (v) => { set(v); onChange(v); };
  set(value);
  pick.addEventListener("input", () => { const a = Math.round((+al.value || 0) * 2.55); send(pick.value.toUpperCase() + (alpha && a < 255 ? hex2(a) : "")); });
  hex.addEventListener("change", () => { let v = hex.value.trim(); if (!v.startsWith("#")) v = "#" + v; send(v); });
  al.addEventListener("change", () => { const a = Math.max(0, Math.min(255, Math.round((+al.value || 0) * 2.55))); send(splitColor(hex.value).rgb + (a < 255 ? hex2(a) : "")); });
  wrap.append(pick, hex); if (alpha) wrap.append(al);
  wrap._set = set;
  return wrap;
}
let formKey = "", inputs = {};
function row(form, label, control) {
  const l = document.createElement("label"); l.textContent = label; form.append(l, control);
}
function drawProps(force) {
  const e = find(sel), host = $("props");
  const key = e ? `${e.id}:${e.type}` : "layout";
  if (!force && key === formKey) return updateProps();      // keep the form (and what's being typed)
  formKey = key; inputs = {}; host.textContent = "";
  const form = document.createElement("div"); form.className = "form"; host.appendChild(form);
  if (!e) return buildLayoutForm(form);
  $("ptitle").textContent = typeLabel(e.type);
  const fields = schema.common.concat(schema.type_fields[e.type]);
  for (const f of fields) {
    const d = schema.fields[f]; let c;
    const send = (v) => edit("set", { id: e.id, fields: { [f]: v } });
    if (d.kind === "bool") { c = document.createElement("input"); c.type = "checkbox"; c.className = "sw"; c.addEventListener("change", () => send(c.checked)); }
    else if (d.kind === "pct") { c = document.createElement("input"); c.type = "number"; c.step = "0.1"; c.min = 0; c.max = 100;
      c.addEventListener("change", () => send(+(+c.value / 100).toFixed(4))); }
    else if (d.kind === "color") { c = colorField(e[f], d.alpha, send); }
    else if (d.kind === "choice" || d.kind === "font") {
      c = document.createElement("select");
      const opts = d.kind === "font" ? [{ value: "", label: "Default (clean sans)" }].concat(schema.fonts.map((x) => ({ value: x, label: x }))) : d.choices;
      for (const o of opts) { const op = document.createElement("option"); op.value = o.value; op.textContent = o.label; c.appendChild(op); }
      if (d.kind === "font" && e.font && !schema.fonts.includes(e.font)) { const op = document.createElement("option"); op.value = e.font; op.textContent = e.font + " (not installed)"; c.appendChild(op); }
      c.addEventListener("change", () => send(c.value));
    } else if (d.kind === "int" || d.kind === "float") { c = document.createElement("input"); c.type = "number"; c.min = d.min; c.max = d.max; c.step = d.step;
      c.addEventListener("change", () => send(d.kind === "int" ? parseInt(c.value, 10) : parseFloat(c.value))); }
    else { c = document.createElement("input"); c.type = "text"; c.addEventListener("change", () => send(c.value)); }
    inputs[f] = c; row(form, d.label + (d.suffix ? ` (${d.suffix})` : "") + (d.kind === "pct" ? " %" : ""), c);
  }
  if (e.type === "label") { const n = document.createElement("div"); n.className = "note full"; n.textContent = "Tokens — " + schema.label_tokens; form.appendChild(n); }
  if (e.type === "message") { const n = document.createElement("div"); n.className = "note full"; n.textContent = "Text comes from the Message box on the remote or in the app."; form.appendChild(n); }
  updateProps(true);
}
function updateProps(all) {
  const e = find(sel);
  if (!e) return updateLayoutForm();
  for (const [f, c] of Object.entries(inputs)) {
    if (!all && (c === document.activeElement || c.contains(document.activeElement))) continue;
    const d = schema.fields[f], v = e[f];
    if (d.kind === "bool") c.checked = !!v;
    else if (d.kind === "pct") c.value = (v * 100).toFixed(1);
    else if (d.kind === "color") c._set(v);
    else c.value = v;
  }
}
function buildLayoutForm(form) {
  $("ptitle").textContent = "Layout";
  const sendL = (o) => edit("set_layout", o);
  const name = document.createElement("input"); name.type = "text"; name.addEventListener("change", () => sendL({ name: name.value }));
  inputs.name = name; row(form, "Name", name);
  const bg = colorField(st.layout.background, true, (v) => sendL({ background: v })); inputs.background = bg; row(form, "Background", bg);
  const ns = document.createElement("select");
  for (const o of schema.no_signal) { const op = document.createElement("option"); op.value = o.value; op.textContent = o.label; ns.appendChild(op); }
  ns.addEventListener("change", () => sendL({ no_signal: ns.value })); inputs.no_signal = ns; row(form, "If data stops", ns);
  for (const c of schema.layout_colors) {
    const f = colorField(st.layout.colors[c.key] || "#000000", false, (v) => sendL({ colors: { [c.key]: v } }));
    inputs["color:" + c.key] = f; row(form, c.label, f);
  }
  const n = document.createElement("div"); n.className = "note full";
  n.textContent = "Tap an element on the canvas or in the list to edit it; ⚙ Layout settings (or Esc) comes back here. Font files are added in the app.";
  form.appendChild(n);
  updateLayoutForm(true);
}
function updateLayoutForm(all) {
  const L = st.layout, act = document.activeElement;
  const skip = (c) => !all && (c === act || c.contains(act));
  if (!inputs.name) return;
  if (!skip(inputs.name)) inputs.name.value = L.name;
  if (!skip(inputs.background)) inputs.background._set(L.background);
  if (!skip(inputs.no_signal)) inputs.no_signal.value = L.no_signal;
  for (const c of schema.layout_colors) { const f = inputs["color:" + c.key]; if (f && !skip(f)) f._set(L.colors[c.key] || "#000000"); }
}

// ── buttons & keys ───────────────────────────────────────────────────────────
$("undo").addEventListener("click", () => edit("undo"));
$("redo").addEventListener("click", () => edit("redo"));
$("save").addEventListener("click", save);
$("saveas").addEventListener("click", saveAs);
$("new").addEventListener("click", () => {
  if (confirm("Start a new, empty layout? The current one stays in Undo, and in its file if it was saved.")) { sel = null; edit("new", { name: "Untitled" }); }
});
$("add").addEventListener("click", async () => { const r = await edit("add", { type: $("addtype").value }); if (r.id) select(r.id); });
$("dup").addEventListener("click", async () => { if (!sel) return; const r = await edit("duplicate", { id: sel }); if (r.ids) select(r.ids[0]); });
$("del").addEventListener("click", () => { if (sel) { const id = sel; sel = null; edit("delete", { id }); } });
$("front").addEventListener("click", () => sel && edit("move", { id: sel, direction: 1 }));
$("back").addEventListener("click", () => sel && edit("move", { id: sel, direction: -1 }));
$("download").addEventListener("click", async () => {
  try {
    const r = await api("/api/layout.json"); if (!r.ok) { toast("Couldn't download"); return; }
    const blob = await r.blob(), a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = (st.layout.name || "layout").replace(/[\\/:*?"<>|]/g, "-") + ".json";
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (e) {}
});
$("import").addEventListener("click", () => $("importfile").click());
$("importfile").addEventListener("change", async () => {
  const f = $("importfile").files[0]; $("importfile").value = "";
  if (!f) return;
  if (f.size > 2 * 1024 * 1024) { toast("That file is too big for a layout (2 MB max)"); return; }
  const content = await f.text();
  const r = await post({ action: "layout_import", filename: f.name, content, open: true });
  if (r.file) { sel = null; drawProps(true); toast(`Imported and opened ${r.file}`, true); }
});
async function save() {
  if (!st.file) return saveAs();                    // never saved: ask for a name
  const r = await edit("save");
  if (r.file) toast(`Saved ${r.file}`, true);
}
async function saveAs() {
  const name = prompt("Save the layout as (in Confidence's layouts folder):", st.layout.name || "Layout");
  if (!name) return;
  let r = await edit("save_as", { name }, true);
  if (r.error && /already exists/.test(r.error)) {
    if (!confirm(r.error.replace(" — pick another name, or overwrite it", "") + ". Replace it?")) return;
    r = await edit("save_as", { name, overwrite: true });
  } else if (r.error) { toast(r.error); return; }
  if (r.file) toast(`Saved ${r.file}`, true);
}
document.addEventListener("keydown", (ev) => {
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "");
  const mod = ev.metaKey || ev.ctrlKey;
  if (mod && ev.key.toLowerCase() === "z") { ev.preventDefault(); edit(ev.shiftKey ? "redo" : "undo"); return; }
  if (mod && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); return; }
  if (typing) return;
  if (ev.key === "Escape") { select(null); return; }
  if (mod && ev.key.toLowerCase() === "d" && sel) { ev.preventDefault(); $("dup").click(); return; }
  const e = find(sel); if (!e) return;
  if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); $("del").click(); return; }
  const step = ev.shiftKey ? 0.01 : 0.001;
  const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[ev.key];
  if (!mv) return;
  ev.preventDefault();
  e.x = +Math.min(Math.max(0, e.x + mv[0]), 1 - e.w).toFixed(4); e.y = +Math.min(Math.max(0, e.y + mv[1]), 1 - e.h).toFixed(4);
  drawBoxes(); queueGeom(e.id, { x: e.x, y: e.y }, false);
});

// ── start & follow changes made in the app ───────────────────────────────────
async function poll() {
  if (dragging || pendingGeom || document.hidden) return;
  try {
    const r = await api(`/api/layout?rev=${st ? st.rev : ""}`);
    if (r.ok) apply(await r.json());
  } catch (e) {}
}
async function boot() {
  try {
    const r = await api("/api/layout/schema"); if (!r.ok) throw new Error("schema");
    schema = await r.json();
    const add = $("addtype"); add.textContent = "";
    for (const t of schema.types) { const o = document.createElement("option"); o.value = t.value; o.textContent = t.label.split(" (")[0]; add.appendChild(o); }
    const l = await api("/api/layout"); apply(await l.json());
    drawProps(true); refreshPreview();
  } catch (e) { if (e.message !== "PIN required") toast("Couldn't reach Confidence"); }
}
setInterval(poll, 1000);
setInterval(refreshPreview, 500);
window.addEventListener("resize", () => refreshPreview());
boot();
</script>
</body>
</html>
"""

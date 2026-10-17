"""HTTP + WebSocket API for a Limitimer and a Perfect Cue (needs ``pip install dsan[server]``).

Start it from the command line (``dsan serve …``) or from Python::

    server = DsanServer(limitimer=Limitimer("/dev/ttyUSB0"), perfectcue=PerfectCue("/dev/ttyUSB1"))
    server.run()          # blocks; Ctrl-C to stop

See README.md for the full endpoint and message reference.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from typing import Any, Dict, Optional, Set

from aiohttp import WSMsgType, web

from . import __version__
from . import perfectcue as pc
from .devices import Limitimer, LinkState, PerfectCue

API = "/api/v1"


def _bool_param(request: web.Request, name: str, default: bool) -> bool:
    v = request.query.get(name)
    if v is None:
        return default
    return v.lower() not in ("0", "false", "no", "off")


class DsanServer:
    def __init__(self, limitimer: Optional[Limitimer] = None, perfectcue: Optional[PerfectCue] = None,
                 *, host: str = "127.0.0.1", port: int = 8765, token: Optional[str] = None,
                 overtime: bool = True, follow_device_format: bool = False):
        if limitimer is None and perfectcue is None:
            raise ValueError("serve at least one device")
        self.limitimer = limitimer
        self.perfectcue = perfectcue
        self.host, self.port = host, port
        self.token = token
        #: Defaults for the 'display' strings; clients can override per request/connection.
        self.overtime = overtime
        self.follow_device_format = follow_device_format
        self._clients: Set[web.WebSocketResponse] = set()
        self._ws_opts: Dict[web.WebSocketResponse, tuple] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None
        self.app = self._build_app()

    # -- app -------------------------------------------------------------------

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._cors, self._auth])
        r = app.router
        r.add_get("/", self._index)
        r.add_get(API, self._root)
        r.add_get(f"{API}/status", self._status)
        r.add_get(f"{API}/limitimer", self._lt_state)
        r.add_get(f"{API}/limitimer/programs/{{n}}", self._lt_program)
        r.add_get(f"{API}/perfectcue", self._pc_status)
        r.add_post(f"{API}/perfectcue/command", self._pc_command)
        r.add_post(f"{API}/perfectcue/{{command}}", self._pc_command)
        r.add_get(f"{API}/ws", self._ws)
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)
        return app

    @web.middleware
    async def _cors(self, request: web.Request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)
        if not resp.prepared:
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if self.token and request.path.startswith(API) and request.method != "OPTIONS":
            given = request.query.get("token", "")
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                given = auth[7:].strip()
            if not hmac.compare_digest(given.encode(), self.token.encode()):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return await handler(request)

    async def _on_startup(self, app: web.Application) -> None:
        self._loop = asyncio.get_running_loop()
        if self.limitimer:
            self.limitimer.on_change(self._lt_changed)
            self.limitimer.on_link(self._lt_link)
            self.limitimer.start()
        if self.perfectcue:
            self.perfectcue.on_button(self._pc_button)
            self.perfectcue.on_link(self._pc_link)
            self.perfectcue.start()

    async def _on_shutdown(self, app: web.Application) -> None:
        for ws in list(self._clients):
            await ws.close()
        loop = asyncio.get_running_loop()
        for dev in (self.limitimer, self.perfectcue):
            if dev:
                await loop.run_in_executor(None, dev.stop)

    # -- running ---------------------------------------------------------------

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def run(self) -> None:
        web.run_app(self.app, host=self.host, port=self.port, print=None)

    # -- snapshots -------------------------------------------------------------

    def limitimer_snapshot(self, overtime: Optional[bool] = None,
                           follow: Optional[bool] = None) -> Dict[str, Any]:
        lt = self.limitimer
        ot = self.overtime if overtime is None else overtime
        fl = self.follow_device_format if follow is None else follow
        st = lt.state if lt else None
        return {
            "link": lt.link.value if lt else None,
            "state": st.to_dict(ot, fl) if st else None,
        }

    def perfectcue_snapshot(self) -> Dict[str, Any]:
        cue = self.perfectcue
        return {
            "link": cue.link.value,
            "last_button": cue.last_button.name if cue.last_button else None,
            "last_button_time": cue.last_button_time,
        } if cue else {"link": None}

    def status_snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"version": __version__, "time": time.time()}
        if self.limitimer:
            lt = self.limitimer
            out["limitimer"] = {"link": lt.link.value, "port": lt.port or "simulated",
                                "stats": dict(lt.stats), "last_error": lt.last_error}
        if self.perfectcue:
            cue = self.perfectcue
            out["perfectcue"] = {"link": cue.link.value, "port": cue.port or "simulated",
                                 "last_error": cue.last_error}
        return out

    # -- HTTP handlers ---------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _root(self, request: web.Request) -> web.Response:
        return web.json_response({
            "name": "dsan", "version": __version__,
            "endpoints": [
                f"GET  {API}/status", f"GET  {API}/limitimer", f"GET  {API}/limitimer/programs/{{1-4}}",
                f"GET  {API}/perfectcue", f"POST {API}/perfectcue/command",
                f"POST {API}/perfectcue/{{forward|reverse|blank_on|blank_off}}", f"GET  {API}/ws",
            ],
        })

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(self.status_snapshot())

    def _display_opts(self, request: web.Request) -> tuple:
        return (_bool_param(request, "overtime", self.overtime),
                request.query.get("format", "device" if self.follow_device_format else "auto") == "device")

    async def _lt_state(self, request: web.Request) -> web.Response:
        if not self.limitimer:
            return web.json_response({"ok": False, "error": "no limitimer configured"}, status=404)
        return web.json_response(self.limitimer_snapshot(*self._display_opts(request)))

    async def _lt_program(self, request: web.Request) -> web.Response:
        if not self.limitimer:
            return web.json_response({"ok": False, "error": "no limitimer configured"}, status=404)
        try:
            n = int(request.match_info["n"])
            if not 1 <= n <= 4:
                raise ValueError
        except ValueError:
            return web.json_response({"ok": False, "error": "program must be 1-4"}, status=400)
        snap = self.limitimer_snapshot(*self._display_opts(request))
        prog = snap["state"]["programs"][n - 1] if snap["state"] else None
        return web.json_response({"link": snap["link"], "program": prog})

    async def _pc_status(self, request: web.Request) -> web.Response:
        if not self.perfectcue:
            return web.json_response({"ok": False, "error": "no perfect cue configured"}, status=404)
        return web.json_response(self.perfectcue_snapshot())

    async def _pc_command(self, request: web.Request) -> web.Response:
        name = request.match_info.get("command")
        if name is None:
            try:
                body = await request.json()
                name = str(body["command"])
            except Exception:
                return web.json_response({"ok": False, "error": 'body must be {"command": "..."}'},
                                         status=400)
        result, status = self._run_command(name)
        return web.json_response(result, status=status)

    def _run_command(self, name: str) -> tuple:
        if not self.perfectcue:
            return {"ok": False, "error": "no perfect cue configured"}, 404
        try:
            cmd = pc.Command.parse(name)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        if not self.perfectcue.send(cmd):
            return {"ok": False, "error": "perfect cue port is not open",
                    "link": self.perfectcue.link.value}, 503
        return {"ok": True, "command": cmd.name.lower()}, 200

    # -- WebSocket ---------------------------------------------------------------

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self._ws_opts[ws] = self._display_opts(request)
        self._clients.add(ws)
        try:
            await ws.send_json(self._hello(ws))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                    kind = data.get("type")
                except Exception:
                    await ws.send_json({"type": "error", "error": "invalid JSON"})
                    continue
                if kind == "perfectcue.command":
                    result, _ = self._run_command(str(data.get("command", "")))
                    await ws.send_json({"type": "result", "id": data.get("id"), **result})
                elif kind == "snapshot":
                    await ws.send_json(self._hello(ws))
                elif kind == "ping":
                    await ws.send_json({"type": "pong", "id": data.get("id"), "time": time.time()})
                else:
                    await ws.send_json({"type": "error", "id": data.get("id"),
                                        "error": f"unknown message type {kind!r}"})
        finally:
            self._clients.discard(ws)
            self._ws_opts.pop(ws, None)
        return ws

    def _hello(self, ws) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"type": "hello", "version": __version__}
        if self.limitimer:
            msg["limitimer"] = self.limitimer_snapshot(*self._ws_opts.get(ws, (None, None)))
        if self.perfectcue:
            msg["perfectcue"] = self.perfectcue_snapshot()
        return msg

    async def _broadcast(self, build) -> None:
        """``build(opts)`` returns the message for a client's display options."""
        for ws in list(self._clients):
            try:
                await ws.send_json(build(self._ws_opts.get(ws, (None, None))))
            except Exception:
                self._clients.discard(ws)

    def _schedule(self, build) -> None:
        # Device callbacks run on reader threads; hop onto the event loop.
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._broadcast(build)))

    # -- device callbacks (reader threads) -------------------------------------

    def _lt_changed(self, state, previous) -> None:
        def build(opts):
            ot = self.overtime if opts[0] is None else opts[0]
            fl = self.follow_device_format if opts[1] is None else opts[1]
            return {"type": "limitimer.state", "state": state.to_dict(ot, fl)}
        self._schedule(build)

    def _lt_link(self, link: LinkState) -> None:
        self._schedule(lambda _: {"type": "limitimer.link", "link": link.value})

    def _pc_button(self, button: pc.Button) -> None:
        t = time.time()
        self._schedule(lambda _: {"type": "perfectcue.button", "button": button.name,
                                  "value": int(button.value), "time": t})

    def _pc_link(self, link: LinkState) -> None:
        self._schedule(lambda _: {"type": "perfectcue.link", "link": link.value})


_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSAN Limitimer</title>
<style>
:root{--bg:#141414;--card:#1e1e1e;--ink:#d0d0d0;--dim:#666;--run:#00e07a;--idle:#4a8cff;--warn:#ffaa00;--over:#ff3b3b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px ui-monospace,Menlo,monospace}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid #2a2a2a}
h1{margin:0;font-size:15px;letter-spacing:.08em;color:var(--run)}#link{font-weight:700}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;padding:16px}
.p{background:var(--card);border:2px solid #2c2c2c;border-radius:10px;padding:12px 14px}
.p.sel{border-color:#3a6}.n{display:flex;justify-content:space-between;color:var(--dim);font-size:12px;letter-spacing:.1em}
.t{font-size:clamp(44px,9vw,84px);font-weight:700;text-align:center;margin:6px 0;font-variant-numeric:tabular-nums}
.f{display:flex;gap:10px;font-size:11px;color:#333}.f .on{color:var(--ink)}
.stale .t{color:#333!important}#pc{padding:0 18px 18px;color:var(--dim)}#pc b{color:var(--ink)}
</style></head><body>
<header><h1>DSAN LIMITIMER</h1><span id="link">connecting…</span></header>
<main id="g"></main><div id="pc"></div>
<script>
const q=new URLSearchParams(location.search),tok=q.get('token');
const url=(location.protocol==='https:'?'wss://':'ws://')+location.host+'/api/v1/ws'+(tok?'?token='+encodeURIComponent(tok):'');
const g=document.getElementById('g'),L=document.getElementById('link'),P=document.getElementById('pc');
let live=false;
function link(s){live=s==='live';L.textContent='● '+(s||'—').toUpperCase();
 L.style.color=live?'var(--run)':(s==='waiting'?'var(--warn)':'var(--over)');document.body.classList.toggle('stale',!live)}
function draw(st){if(!st)return;g.innerHTML='';for(const p of st.programs){
 const c=p.expired?'var(--over)':p.warning?'var(--warn)':p.running?'var(--run)':'var(--idle)';
 const d=document.createElement('div');d.className='p'+(st.selected===p.number?' sel':'');
 const fl=['running','warning','expired','blink'].map(k=>`<span class="${p[k]?'on':''}">${k.toUpperCase()}</span>`).join('');
 d.innerHTML=`<div class="n"><span>PROGRAM ${p.number}</span><span>${st.selected===p.number?'●':''}</span></div>
 <div class="t" style="color:${c}">${p.display}</div><div class="f">${fl}</div>`;g.appendChild(d)}}
function cue(b){P.innerHTML=b?'Perfect Cue last button: <b>'+b+'</b>':''}
(function connect(){const ws=new WebSocket(url);
 ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.type==='hello'){if(m.limitimer){link(m.limitimer.link);draw(m.limitimer.state)}if(m.perfectcue)cue(m.perfectcue.last_button)}
  else if(m.type==='limitimer.state')draw(m.state);else if(m.type==='limitimer.link')link(m.link);
  else if(m.type==='perfectcue.button')cue(m.button)};
 ws.onclose=()=>{link('server offline');setTimeout(connect,1000)}})();
</script></body></html>
"""

__all__ = ["DsanServer"]

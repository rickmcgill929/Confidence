import asyncio
import socket
import time

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("pythonosc")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from pythonosc.osc_packet import OscPacket  # noqa: E402

from dsan import Limitimer, PerfectCue  # noqa: E402
from dsan.osc import OscBridge  # noqa: E402
from dsan.server import DsanServer  # noqa: E402
from dsan.sim import PerfectCueSimulator  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _client(server: DsanServer) -> TestClient:
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return client


async def _wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def test_http_and_websocket():
    async def go():
        sims = []

        def pc_factory():
            sims.append(PerfectCueSimulator(interval=0.2))
            return sims[-1]

        server = DsanServer(Limitimer.simulated(speed=20),
                            PerfectCue(transport_factory=pc_factory, reconnect=False))
        client = await _client(server)
        try:
            assert await _wait_for(lambda: server.limitimer.state is not None)

            r = await client.get("/api/v1/status")
            st = await r.json()
            assert st["limitimer"]["link"] == "live" and st["perfectcue"]["link"] == "open"

            r = await client.get("/api/v1/limitimer?overtime=false&format=device")
            body = await r.json()
            assert body["link"] == "live" and len(body["state"]["programs"]) == 4

            r = await client.get("/api/v1/limitimer/programs/2")
            assert (await r.json())["program"]["number"] == 2
            assert (await client.get("/api/v1/limitimer/programs/9")).status == 400

            r = await client.post("/api/v1/perfectcue/forward")
            assert (await r.json()) == {"ok": True, "command": "forward"}
            r = await client.post("/api/v1/perfectcue/command", json={"command": "blank_on"})
            assert r.status == 200
            assert (await client.post("/api/v1/perfectcue/command", json={"command": "nope"})).status == 400
            assert sims[0].written[:2] == [b">FORWARD 35\r", b">BLANKON 25\r"]

            r = await client.options("/api/v1/perfectcue/command")
            assert r.headers["Access-Control-Allow-Origin"] == "*"

            ws = await client.ws_connect("/api/v1/ws")
            hello = await ws.receive_json()
            assert hello["type"] == "hello" and hello["limitimer"]["state"]
            types = set()
            await ws.send_json({"type": "perfectcue.command", "command": "reverse", "id": 7})
            end = time.monotonic() + 3
            result = None
            while time.monotonic() < end and not ({"limitimer.state", "perfectcue.button"} <= types and result):
                msg = await ws.receive_json(timeout=2)
                types.add(msg["type"])
                if msg["type"] == "result":
                    result = msg
            assert {"limitimer.state", "perfectcue.button"} <= types
            assert result == {"type": "result", "id": 7, "ok": True, "command": "reverse"}
            await ws.close()

            page = await client.get("/")
            assert "DSAN LIMITIMER" in await page.text()
        finally:
            await client.close()

    run(go())


def test_token_auth():
    async def go():
        server = DsanServer(Limitimer.simulated(), token="s3cret")
        client = await _client(server)
        try:
            assert (await client.get("/api/v1/status")).status == 401
            assert (await client.get("/api/v1/status?token=s3cret")).status == 200
            r = await client.get("/api/v1/status", headers={"Authorization": "Bearer s3cret"})
            assert r.status == 200
            assert (await client.get("/")).status == 200      # page itself is public
        finally:
            await client.close()

    run(go())


def test_command_without_open_port_is_503():
    async def go():
        server = DsanServer(perfectcue=PerfectCue("/dev/does-not-exist", reconnect=False))
        client = await _client(server)
        try:
            r = await client.post("/api/v1/perfectcue/next")
            assert r.status == 503
        finally:
            await client.close()

    run(go())


def test_osc_bridge_bundles():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(3)
    port = rx.getsockname()[1]
    with Limitimer.simulated(speed=10) as dev:
        bridge = OscBridge("127.0.0.1", port, limitimer=dev, overtime=[True, False, True, True])
        addrs = {}
        end = time.monotonic() + 3
        while time.monotonic() < end and not {"/dsan/link", "/dsan/timer/4/color"} <= addrs.keys():
            data, _ = rx.recvfrom(65535)
            for m in OscPacket(data).messages:
                addrs[m.message.address] = m.message.params
        bridge.close()
    rx.close()
    assert addrs["/dsan/link"] == [True]
    assert addrs["/dsan/selected"] == [1]
    for a in ("time", "seconds", "remaining", "overtime", "color"):
        assert f"/dsan/timer/1/{a}" in addrs

"""Live integration against the real Synapse from infra/ (skipped if it isn't up).

Runs the bot in-process (own data dir, notify server on a free port) against a MOCK service
on a free port, then acts as Dr. Hofer through the Matrix client-server API:
  !anfordern labor … (look-alike address) -> confirm card with register + warning -> !ja -> sent
and finally POSTs /notify and checks the message lands in the room.
"""
import asyncio
import json
import os
import socket
import time
import uuid
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from bot.config import REPO, Config
from bot.runtime import BotRuntime

STATE = REPO / "infra" / "state.json"


def _up() -> bool:
    # Opt-in: it posts into the real demo room. Run with PGK_BOT_IT=1.
    if os.environ.get("PGK_BOT_IT") != "1" or not STATE.exists():
        return False
    try:
        socket.create_connection(("localhost", 8008), timeout=1).close()
        return True
    except OSError:
        return False


def _other_bot_running() -> bool:
    """A second bot (e.g. `make run`) would answer too and send REAL requests via the real service."""
    try:
        socket.create_connection(("127.0.0.1", 8001), timeout=0.5).close()
        return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(not _up(), reason="set PGK_BOT_IT=1 with Synapse up (posts into the demo room)"),
    pytest.mark.skipif(_up() and _other_bot_running(), reason="another bot is running on :8001 (stop `make run` first)"),
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MockService:
    def __init__(self):
        self.calls = []

    def app(self):
        async def create(req):
            body = await req.json()
            self.calls.append(body)
            w = []
            if "labor-kanaI" in body["address"]:
                w = [{"code": "confusable", "domain": "labor-kanaI.example", "similar_to": "labor-kanal.example",
                      "message": "Achtung: 'labor-kanaI.example' enthält ein großes I statt l – meinten Sie labor-kanal.example?"}]
            if body.get("dry_run"):
                return web.json_response({"request_id": None, "status": "dry_run", "warnings": [x["message"] for x in w],
                                          "warning_details": w, "patient": {"name": "Maria Beispiel", "birthDate": "1961-03-12"}, "register": [
                                              "Ärzteliste: Dr. Eva Brandl, Medizinische und Chemische Labordiagnostik ✓"]})
            return web.json_response({"request_id": "req-0000000000e1", "ref_code": "KB-IT0001", "status": "sent", "warnings": []})
        a = web.Application()
        a.router.add_post("/api/requests", create)
        return a


async def _scenario():
    st = json.loads(STATE.read_text())
    hs, room, lena_tok = st.get("homeserver", "http://localhost:8008"), st["room_id"], st["tokens"]["lena"]
    bot_user = st["users"]["bot"]["user_id"]
    mock = MockService()
    svc_port, notify_port = _free_port(), _free_port()
    runner = web.AppRunner(mock.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", svc_port).start()

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="pgkbot-it-"))
    cfg = Config()
    cfg.homeserver, cfg.service_url, cfg.notify_port, cfg.data_dir = hs, f"http://127.0.0.1:{svc_port}", notify_port, tmp
    cfg.sync_timeout_ms = 5000
    cfg.notify_key = "it-notify-key"
    rt = BotRuntime(cfg)
    bot_task = asyncio.create_task(rt.run())
    H = {"Authorization": f"Bearer {lena_tok}"}
    try:
        await asyncio.wait_for(rt.synced.wait(), 30)
        async with aiohttp.ClientSession(headers=H) as s:
            async def say(text):
                url = f"{hs}/_matrix/client/v3/rooms/{room}/send/m.room.message/{uuid.uuid4()}"
                async with s.put(url, json={"msgtype": "m.text", "body": text}) as r:
                    assert r.status == 200, await r.text()
                    return (await r.json())["event_id"]

            async def bot_reply_after(event_id, contains, timeout=20):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    async with s.get(f"{hs}/_matrix/client/v3/rooms/{room}/messages",
                                     params={"dir": "b", "limit": "20"}) as r:
                        chunk = (await r.json())["chunk"]
                    for ev in chunk:                 # newest first; stop at our own message
                        if ev["event_id"] == event_id:
                            break
                        if ev.get("sender") == bot_user and contains in ev.get("content", {}).get("body", ""):
                            return ev
                    await asyncio.sleep(0.5)
                raise AssertionError(f"no bot reply containing {contains!r}")

            e1 = await say("!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, "
                           "eva.brandl@labor-kanaI.example, Patient maria-beispiel")
            card = await bot_reply_after(e1, "Nicht gesendet")
            body = card["content"]["body"]
            assert "großes I statt l" in body and "Ärzteliste: Dr. Eva Brandl" in body
            assert card["content"].get("format") == "org.matrix.custom.html"
            assert len(mock.calls) == 1 and mock.calls[0]["dry_run"] is True

            e2 = await say("!ja")
            sent = await bot_reply_after(e2, "Anfrage gesendet")
            assert "KB-IT0001" in sent["content"]["body"] and "Maria Beispiel · 12.03.1961" in sent["content"]["body"]
            real = mock.calls[-1]
            assert real["confirm"] is True and real["confirm_warnings"] is True
            assert real["requester_mxid"] == st["users"]["lena"]["user_id"] and real["room_id"] == room

            e3 = await say("!status")   # no status endpoint on the mock -> error reply, loop must survive
            await bot_reply_after(e3, "KB-IT0001")

            async with aiohttp.ClientSession() as plain:
                async with plain.post(f"http://127.0.0.1:{notify_port}/notify", headers={"X-PGK-Notify-Key": "it-notify-key"},
                                      json={"room_id": room, "kind": "misdelivery", "request_id": "req-0000000000e1",
                                            "text": "Identität passt nicht zur angefragten Person (Integrationstest)"}) as r:
                    assert r.status == 200, await r.text()
                    eid = (await r.json())["event_id"]
            async with s.get(f"{hs}/_matrix/client/v3/rooms/{room}/event/{eid}") as r:
                ev = await r.json()
            assert ev["content"]["body"].startswith("⚠️ Nicht abgelegt: Identität passt nicht")
            return mock.calls
    finally:
        bot_task.cancel()
        try:
            await bot_task
        except (asyncio.CancelledError, Exception):
            pass
        await runner.cleanup()


def test_live_request_flow():
    calls = asyncio.run(_scenario())
    assert [c.get("dry_run") for c in calls] == [True, None]

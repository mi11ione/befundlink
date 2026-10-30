"""Matrix runtime: login, sync loop with persisted token, auto-join, rate-limit-safe sending,
and the internal notify HTTP server on :8001."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from collections import deque
from pathlib import Path

from aiohttp import web
from nio import (AsyncClient, AsyncClientConfig, JoinError, LoginResponse, RoomMessageText, RoomSendResponse,
                 SyncError, SyncResponse, WhoamiResponse)

from . import fmt
from .config import Config
from .core import Core
from .parse import valid_id
from .service import ServiceClient

log = logging.getLogger("bot.runtime")

SYNC_FILTER = {
    "presence": {"not_types": ["*"]},
    "account_data": {"not_types": ["*"]},
    "room": {"timeline": {"limit": 50, "types": ["m.room.message"]},
             "state": {"lazy_load_members": True},
             "ephemeral": {"not_types": ["*"]}},
}


def _first_filter() -> dict:
    """Initial sync filter, made unique per start (a never-matching not_senders nonce) so that
    Synapse's sync response cache can't hand us a stale next_batch from an earlier process."""
    return {**SYNC_FILTER, "room": {**SYNC_FILTER["room"], "timeline": {
        "limit": 1, "not_senders": [f"@nonce-{uuid.uuid4().hex}:invalid"]}}}


def _inviter(info, me: str) -> str | None:
    for ev in getattr(info, "invite_state", None) or []:
        if getattr(ev, "state_key", None) == me and getattr(ev, "membership", None) == "invite":
            return ev.sender
    return None


class BotRuntime:
    def __init__(self, cfg: Config, service=None):
        self.cfg = cfg
        cfg.refresh_from_state()
        self.client = AsyncClient(
            cfg.homeserver, cfg.user_id, device_id=cfg.device_id,
            config=AsyncClientConfig(encryption_enabled=False, max_limit_exceeded=20, max_timeouts=3,
                                     request_timeout=cfg.sync_timeout_ms / 1000 + 30),
        )
        self.service = service or ServiceClient(cfg.service_url)
        self.core = Core(self.service, self.send, members=cfg.members)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.logged_in = asyncio.Event()
        self.synced = asyncio.Event()
        self.state_path = Path(cfg.data_dir) / "bot_state.json"
        self.next_batch: str | None = None
        self._seen: deque[str] = deque(maxlen=1000)
        self._names: dict[str, str] = {}             # display-name cache (Matrix profile)
        self._load()
        # Without a stored token we must not act on history. Synapse may serve a cached initial
        # /sync (same user+filter within ~2 min) with an OLD next_batch, which would replay
        # everything after it, so additionally ignore messages older than our start.
        self.fresh_start_ms = int(time.time() * 1000) if self.next_batch is None else None

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            st = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        if st.get("homeserver") == self.cfg.homeserver and st.get("user_id") == self.cfg.user_id:
            self.next_batch = st.get("next_batch")
            self.core.last_request.update(st.get("last_request") or {})
            self.core.holds.update(st.get("holds") or {})
            self.core.request_rooms.update(st.get("request_rooms") or {})
            self.core.identity_holds.update(st.get("identity_holds") or {})
            self.core.recipients.update(st.get("recipients") or {})
            self._seen.extend(st.get("seen") or [])

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "homeserver": self.cfg.homeserver, "user_id": self.cfg.user_id, "next_batch": self.next_batch,
            "last_request": self.core.last_request, "holds": self.core.holds, "identity_holds": self.core.identity_holds,
            "recipients": self.core.recipients,
            "request_rooms": self.core.request_rooms, "seen": list(self._seen)[-200:],
        }))
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ login

    async def login(self) -> None:
        self.cfg.refresh_from_state()
        if self.cfg.access_token:
            self.client.restore_login(self.cfg.user_id, self.cfg.device_id, self.cfg.access_token)
            r = await self.client.whoami()
            if isinstance(r, WhoamiResponse):
                self.client.user_id = r.user_id
                if r.device_id:
                    self.client.device_id = r.device_id
                log.info("logged in with token as %s", r.user_id)
                self.logged_in.set()
                return
            log.warning("token login failed (%s), falling back to password", r)
            self.cfg.access_token = None
        r = await self.client.login(self.cfg.password, device_name="Befundlink bot")
        if not isinstance(r, LoginResponse):
            raise RuntimeError(f"login failed: {r}")
        log.info("logged in with password as %s (device %s)", r.user_id, r.device_id)
        self.logged_in.set()

    # ------------------------------------------------------------ sending

    async def send(self, room_id: str, text: str, html: str | None = None, msgtype: str = "m.notice",
                   timeout: float = 90.0) -> str:
        # Always formatted (no auto-links), no @room ping, and m.mentions {} = mention nobody.
        content = {"msgtype": msgtype, "body": fmt.no_room_ping(text),
                   "format": "org.matrix.custom.html",
                   "formatted_body": fmt.no_room_ping(html) if html else fmt.text_html(text),
                   "m.mentions": {}}
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put((room_id, content, fut))
        return await asyncio.wait_for(fut, timeout)

    async def _sender(self) -> None:
        """Single worker: serialises sends, retries with the same txn id (idempotent),
        honours retry_after_ms, paces messages."""
        while True:
            room_id, content, fut = await self.queue.get()
            await self.logged_in.wait()
            txn = str(uuid.uuid4())
            delay, joined_retry, last = 1.0, False, None
            for _ in range(8):
                try:
                    r = await self.client.room_send(room_id, "m.room.message", content, tx_id=txn)
                except Exception as ex:  # transport errors
                    r = ex
                if isinstance(r, RoomSendResponse):
                    if not fut.done():
                        fut.set_result(r.event_id)
                    break
                last = r
                code = getattr(r, "status_code", None)
                if code == "M_FORBIDDEN" and not joined_retry:   # not in the room yet: try joining once
                    joined_retry = True
                    await self.client.join(room_id)
                    continue
                if code in ("M_FORBIDDEN", "M_NOT_FOUND", "M_UNKNOWN", "M_BAD_JSON"):
                    break
                wait = (getattr(r, "retry_after_ms", None) or 0) / 1000 or delay
                log.warning("send to %s failed (%s), retry in %.1fs", room_id, r, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 30)
            if not fut.done():
                fut.set_exception(RuntimeError(f"send failed: {last}"))
            await asyncio.sleep(0.2)

    # ------------------------------------------------------------ sync

    async def process_sync(self, resp: SyncResponse, initial: bool) -> None:
        for room_id, info in resp.rooms.invite.items():
            inviter = _inviter(info, self.client.user_id)
            if inviter not in self.cfg.members:
                log.warning("rejecting invite to %s from non-member %s", room_id, inviter)
                await self.client.room_leave(room_id)
                continue
            r = await self.client.join(room_id)
            log.info("invite to %s from %s -> %s", room_id, inviter, "joined" if not isinstance(r, JoinError) else r)
        for room_id, info in resp.rooms.join.items():
            for ev in info.timeline.events:
                if not isinstance(ev, RoomMessageText) or ev.sender == self.client.user_id:
                    continue
                if ev.event_id in self._seen:
                    continue
                self._seen.append(ev.event_id)
                if initial:      # first start without a token: never replay history
                    continue
                if self.fresh_start_ms and (ev.server_timestamp or 0) < self.fresh_start_ms:
                    continue
                await self.core.handle(room_id, ev.sender, ev.body)

    async def sync_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                initial = self.next_batch is None
                resp = await self.client.sync(
                    timeout=0 if initial else self.cfg.sync_timeout_ms,
                    sync_filter=_first_filter() if initial else SYNC_FILTER,
                    since=self.next_batch,
                )
                if isinstance(resp, SyncError):
                    log.warning("sync error: %s", resp)
                    if resp.status_code in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"):
                        self.logged_in.clear()
                        self.cfg.access_token = None
                        await self.login()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                await self.process_sync(resp, initial)
                self.next_batch = resp.next_batch
                self._save()
                self.synced.set()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync loop error, retry in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------ notify server

    def make_app(self) -> web.Application:
        async def notify(request: web.Request) -> web.Response:
            def err(status, msg):
                log.warning("notify rejected (%s): %s", status, msg)
                return web.json_response({"ok": False, "error": msg}, status=status)
            key = self.cfg.notify_key
            if not key:
                return err(503, "notify key not configured (PGK_NOTIFY_KEY)")
            if not hmac.compare_digest(request.headers.get("X-PGK-Notify-Key", "").encode(), key.encode()):
                return err(401, "missing or wrong X-PGK-Notify-Key")
            if request.content_type != "application/json":
                return err(415, "Content-Type must be application/json")
            try:
                p = await request.json()
                if not isinstance(p, dict):
                    raise ValueError
            except Exception:
                return err(400, "JSON object expected")
            self.cfg.refresh_from_state()
            room = p.get("room_id") or self.cfg.default_room
            if not room:
                return err(400, "room_id missing")
            if room not in self.allowed_rooms():
                return err(403, "room not allowed")
            if (p.get("kind") or "").lower() == "request_sent" and await self._sent_card_posted(room, p):
                return web.json_response({"ok": True, "suppressed": "card already posted by the bot"})
            try:
                text, html = fmt.notify_message(p)
                names = await self.display_names(p, text, html)
                text, html = fmt.humanize(text, names), fmt.humanize(html, names)
            except ValueError as ex:
                return web.json_response({"ok": False, "error": str(ex)}, status=400)
            try:
                event_id = await self.send(room, text, html, msgtype="m.text")
            except Exception as ex:
                log.exception("notify send failed")
                return web.json_response({"ok": False, "error": str(ex)}, status=502)
            dd = p.get("data") if isinstance(p.get("data"), dict) else {}
            self.core.note_request(room, p.get("request_id"), p.get("ref_code"), p.get("answer_id"),
                                   dd.get("request_id"), dd.get("ref_code"), dd.get("answer_id"))
            if (p.get("kind") or "").lower() in ("identity_confirm", "new_identity", "identity_review", "namesake") \
                    and fmt.hold_id(p):
                self.core.register_identity_hold(room, fmt.hold_id(p))
            if (p.get("kind") or "").lower() == "holds_superseded":
                self.core.close_holds(room, [p.get("ref_code"), dd.get("ref_code"), p.get("request_id"),
                                             dd.get("request_id"), *(dd.get("answer_ids") or [])])
            if (p.get("kind") or "").lower() == "hold" and fmt.hold_id(p):
                self.core.register_hold(room, fmt.hold_id(p))
            rid = fmt.primary_id(p.get("ref_code"), dd.get("ref_code"), p.get("request_id"), dd.get("request_id"))
            if rid:
                self.core.last_request[room] = rid
            return web.json_response({"ok": True, "event_id": event_id})

        async def health(_request: web.Request) -> web.Response:
            return web.json_response({"ok": True, "logged_in": self.logged_in.is_set(),
                                      "synced": self.synced.is_set(), "user_id": self.client.user_id})

        app = web.Application()
        app.router.add_post("/notify", notify)
        app.router.add_get("/health", health)
        return app

    async def _sent_card_posted(self, room: str, p: dict, settle: float = 1.5, limit: float = 6.0) -> bool:
        """The service's request_sent notify races the bot's own '✅ Anfrage gesendet' card (it is sent while
        the confirm call is still running). Wait while a confirm is in flight in this room; suppress if the
        card for this ref exists, else let the notify render once (and remember it)."""
        d = p.get("data") if isinstance(p.get("data"), dict) else {}
        ids = {str(x) for x in (p.get("ref_code"), d.get("ref_code"), p.get("request_id"), d.get("request_id"))
               if valid_id(x)}
        loop = asyncio.get_running_loop()
        start = loop.time()
        while True:
            if ids & self.core.sent_cards:
                return True
            waited = loop.time() - start
            if waited >= limit or (waited >= settle and not self.core.inflight.get(room)):
                break
            await asyncio.sleep(0.1)
        self.core.sent_cards |= ids           # rendered now: a repeated notify is suppressed
        return False

    async def display_names(self, p: dict, *texts) -> dict[str, str]:
        """MXID -> display name: requester_display from the service, else the Matrix profile (the PGK
        directory), cached. Unknown ids stay as they are."""
        d = p.get("data") if isinstance(p.get("data"), dict) else {}
        found = fmt.mxids_in(*texts)
        out: dict[str, str] = {}
        disp = p.get("requester_display") or d.get("requester_display")
        req = p.get("requester_mxid") or d.get("requester_mxid")
        if disp and req:
            out[req] = disp
        elif disp and len(found) == 1:
            out[next(iter(found))] = disp
        for mx in found - set(out):
            if mx not in self._names:
                try:
                    r = await self.client.get_displayname(mx)
                    self._names[mx] = getattr(r, "displayname", None) or ""
                except Exception:
                    self._names[mx] = ""
            if self._names[mx]:
                out[mx] = self._names[mx]
        return out

    def allowed_rooms(self) -> set[str]:
        """Rooms /notify may post to: the infra room(s) and rooms of requests made through the bot."""
        return set(self.cfg.allowed_rooms) | set(self.core.request_rooms.values()) | set(self.core.last_request)

    # ------------------------------------------------------------ main

    async def run(self) -> None:
        runner = web.AppRunner(self.make_app())
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.notify_host, self.cfg.notify_port)
        await site.start()
        log.info("notify server on http://%s:%s", self.cfg.notify_host, self.cfg.notify_port)
        sender = asyncio.create_task(self._sender())
        try:
            backoff = 1.0
            while True:
                try:
                    await self.login()
                    break
                except Exception as ex:
                    log.warning("login failed (%s), retry in %.0fs", ex, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
            await self.sync_loop()
        finally:
            sender.cancel()
            await runner.cleanup()
            await self.service.close()
            await self.client.close()

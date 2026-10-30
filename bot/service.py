"""HTTP client for the Befundlink service."""
from __future__ import annotations

import logging
from urllib.parse import quote

import aiohttp

from .parse import valid_id

log = logging.getLogger("bot.service")


class ServiceError(Exception):
    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _detail(payload) -> str:
    if isinstance(payload, dict):
        d = payload.get("detail", payload.get("error", payload.get("message", payload)))
        if isinstance(d, list):      # FastAPI validation errors
            return "; ".join(
                f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" if isinstance(e, dict) else str(e)
                for e in d
            )
        return str(d)
    return str(payload)


class ServiceClient:
    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        from .config import dotenv
        key = dotenv("PGK_API_KEY")               # service enforces X-API-Key when it is set
        self.headers = {"X-API-Key": key} if key else {}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=self.headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, path: str, **kw):
        s = await self._s()
        url = self.base + path
        try:
            async with s.request(method, url, **kw) as r:
                try:
                    payload = await r.json(content_type=None)
                except Exception:
                    payload = await r.text()
                if r.status >= 400:
                    raise ServiceError(_detail(payload), r.status, payload)
                return payload
        except aiohttp.ClientError as e:
            raise ServiceError(f"Dienst nicht erreichbar ({url}): {e.__class__.__name__}") from e
        except TimeoutError as e:
            raise ServiceError(f"Zeitüberschreitung bei {url}") from e

    @staticmethod
    def _who(actor: str | None, room: str | None) -> dict:
        """Who is asking, so the service can authorise (only the requester, in the request's room)."""
        h = {}
        if actor:
            h["X-PGK-Actor"] = actor
        if room:
            h["X-PGK-Room"] = room
        return h

    @staticmethod
    def _id(v: str) -> str:
        if not valid_id(v):
            raise ServiceError("ungültige ID")
        return quote(v, safe="")

    async def create_request(self, body: dict) -> dict:
        log.info("POST /api/requests dry_run=%s confirm=%s", body.get("dry_run"), body.get("confirm"))
        res = await self._call("POST", "/api/requests", json=body,
                               headers=self._who(body.get("requester_mxid"), body.get("room_id")))
        return res if isinstance(res, dict) else {"raw": res}

    async def get_request(self, request_id: str, actor: str | None = None, room: str | None = None):
        return await self._call("GET", f"/api/requests/{self._id(request_id)}", headers=self._who(actor, room))

    async def get_audit(self, request_id: str, actor: str | None = None, room: str | None = None):
        self._id(request_id)
        return await self._call("GET", "/api/audit", params={"request_id": request_id}, headers=self._who(actor, room))

    async def decide(self, answer_id: str, action: str, reason: str | None, by: str, room: str | None = None) -> dict:
        """action: 'file' | 'reject' | 'hold' (Dr. Hofer's decision on a held answer)."""
        res = await self._call("POST", f"/api/answers/{self._id(answer_id)}/decision",
                               json={"action": action, "reason": reason, "by": by, "room_id": room},
                               headers=self._who(by, room))
        return res if isinstance(res, dict) else {"raw": res}

    async def get_preview(self, ref: str, actor: str, room: str | None = None):
        """Held document text for Dr. Hofer's own room (the service serves holds only, and only to the requester)."""
        return await self._call("GET", f"/api/answers/{self._id(ref)}/preview", headers=self._who(actor, room))

    async def unlock_request(self, request_id: str, by: str, room: str | None = None) -> dict:
        """Re-open a link the service locked after repeated failed eID attempts (requester only)."""
        res = await self._call("POST", f"/api/requests/{self._id(request_id)}/unlock",
                               json={"by": by, "room_id": room}, headers=self._who(by, room))
        return res if isinstance(res, dict) else {"raw": res}

    async def confirm_request(self, request_id: str, by: str, room: str | None = None,
                              acknowledged: list[str] | None = None) -> dict:
        """Send a request the service stored as needs_confirmation (service core.confirm). The service
        requires `acknowledged_warnings` to list EVERY warning code of the stored request."""
        res = await self._call("POST", f"/api/requests/{self._id(request_id)}/confirm",
                               json={"by": by, "room_id": room, "acknowledged_warnings": list(acknowledged or [])},
                               headers=self._who(by, room))
        return res if isinstance(res, dict) else {"raw": res}

    async def cancel_request(self, request_id: str, by: str, room: str | None = None) -> dict:
        res = await self._call("POST", f"/api/requests/{self._id(request_id)}/cancel",
                               json={"by": by, "room_id": room}, headers=self._who(by, room))
        return res if isinstance(res, dict) else {"raw": res}

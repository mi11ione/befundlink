"""Clients for the live stack (default ports), all over public HTTP. Override with PGK_* env vars."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .web import HttpError, call, poll

REPO = Path(__file__).resolve().parents[2]
LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+/r/[A-Za-z0-9_\-.~%]+")


def _dotenv(key: str) -> str | None:
    """Value from the environment, else from the repo's .env (written by `make .env`)."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in (REPO / ".env").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip("'\"") or None
    except OSError:
        pass
    return None


def _state() -> dict:
    try:
        return json.loads((REPO / "infra" / "state.json").read_text())
    except (OSError, ValueError):
        return {}


@dataclass
class Stack:
    service: str = field(default_factory=lambda: os.environ.get("PGK_BASE_URL", "http://localhost:8000").rstrip("/"))
    hapi: str = field(default_factory=lambda: os.environ.get("PGK_HAPI_URL", "http://localhost:8090/fhir").rstrip("/"))
    mailpit: str = field(default_factory=lambda: os.environ.get("PGK_MAILPIT_API", "http://localhost:8025/api/v1").rstrip("/"))
    synapse: str = field(default_factory=lambda: os.environ.get("PGK_SYNAPSE", "http://localhost:8008").rstrip("/"))
    bot: str = field(default_factory=lambda: os.environ.get("PGK_BOT_URL", "http://localhost:8001").rstrip("/"))
    api_key: str | None = field(default_factory=lambda: _dotenv("PGK_API_KEY"))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("PGK_DATA_DIR", str(REPO / "service" / "data"))))
    state: dict = field(default_factory=_state)

    @property
    def room_id(self) -> str | None:
        return os.environ.get("PGK_ROOM_ID") or self.state.get("room_id")

    @property
    def audit_room_id(self) -> str | None:
        """Anchors go to the separate 'Befundlink Audit' room (infra/state.json audit_room_id); else the main room."""
        return os.environ.get("PGK_AUDIT_ROOM_ID") or self.state.get("audit_room_id") or self.room_id

    @property
    def lena_token(self) -> str | None:
        return os.environ.get("PGK_LENA_TOKEN") or (self.state.get("tokens") or {}).get("lena")

    def up(self) -> dict[str, bool]:
        probes = {"service": f"{self.service}/", "hapi": f"{self.hapi}/metadata", "mailpit": f"{self.mailpit}/info",
                  "synapse": f"{self.synapse}/health", "bot": f"{self.bot}/"}

        def ok(u):
            try:
                return call("GET", u, ok=None, timeout=5)[0] < 500
            except OSError:
                return False
        with ThreadPoolExecutor(len(probes)) as ex:
            return dict(zip(probes, ex.map(ok, probes.values())))


# ------------------------------------------------------------------ Mailpit
class Mailpit:
    def __init__(self, base: str):
        self.base = base

    def search(self, query: str, limit: int = 200) -> list[dict]:
        q = urllib.parse.urlencode({"query": query, "limit": limit})
        return call("GET", f"{self.base}/search?{q}")[1].get("messages") or []

    def all(self) -> list[dict]:
        out, start = [], 0
        while True:
            page = call("GET", f"{self.base}/messages?start={start}&limit=500")[1]
            msgs = page.get("messages") or []
            out += msgs
            start += len(msgs)
            if not msgs or start >= page.get("messages_count", page.get("total", 0)):
                return out

    def message(self, mid: str) -> dict:
        return call("GET", f"{self.base}/message/{mid}")[1]

    def headers(self, mid: str) -> dict:
        return call("GET", f"{self.base}/message/{mid}/headers")[1]

    def to(self, address: str, exclude: set[str] = frozenset()) -> list[dict]:
        hits = self.search(f'to:"{address}"')
        return [m for m in hits if m["ID"] not in exclude
                and any(t.get("Address", "").casefold() == address.casefold() for t in m.get("To") or [])]

    def wait_new(self, address: str, exclude: set[str], timeout: float = 20) -> dict:
        m = poll(lambda: self.to(address, exclude), timeout, what=f"mail to {address}")
        return self.message(sorted(m, key=lambda x: x.get("Created", ""))[-1]["ID"])

    def send(self, to: str, subject: str, text: str, sender: str = "eva.brandl@labor-kanal.example") -> None:
        """Forward a message (M2) through Mailpit's send API."""
        call("POST", f"{self.base}/send", {"From": {"Email": sender}, "To": [{"Email": to}], "Subject": subject,
                                           "Text": text})


def links_in(*texts: str) -> list[str]:
    seen = []
    for t in texts:
        for u in LINK_RE.findall(t or ""):
            u = u.rstrip(".,;")
            if u not in seen:
                seen.append(u)
    return seen


# ------------------------------------------------------------------ Matrix (as Dr. Hofer)
class Matrix:
    def __init__(self, base: str, token: str, room: str):
        self.base, self.token, self.room = base, token, room
        self.h = {"Authorization": f"Bearer {token}"}
        self.offset = 0     # homeserver clock minus ours (ms), from our own sent events; <= 0 when it runs behind

    def send(self, body: str) -> str:
        """Send as Dr. Hofer. Learns the homeserver's clock offset from the event's origin_server_ts: a bot that
        answers within a few ms would otherwise be stamped before our local send time and be missed."""
        t0 = int(time.time() * 1000)
        url = f"{self.base}/_matrix/client/v3/rooms/{urllib.parse.quote(self.room)}/send/m.room.message/{uuid.uuid4().hex}"
        eid = call("PUT", url, {"msgtype": "m.text", "body": body}, self.h)[1]["event_id"]
        try:
            ev = call("GET", f"{self.base}/_matrix/client/v3/rooms/{urllib.parse.quote(self.room)}/event/"
                             f"{urllib.parse.quote(eid)}", headers=self.h)[1]
            self.offset = min(self.offset, int(ev.get("origin_server_ts", t0)) - t0)
        except Exception:   # a homeserver without the event endpoint (the mock stack): keep the last offset
            pass
        return eid

    def since(self, ts_ms: int, limit: int = 200) -> list[dict]:
        """Room messages with origin_server_ts >= ts_ms (our clock, corrected by the homeserver offset), oldest first."""
        ts_ms += self.offset
        url = (f"{self.base}/_matrix/client/v3/rooms/{urllib.parse.quote(self.room)}/messages?dir=b&limit={limit}"
               f"&filter={urllib.parse.quote(json.dumps({'types': ['m.room.message']}))}")
        ev = call("GET", url, headers=self.h)[1].get("chunk") or []
        return sorted((e for e in ev if e.get("origin_server_ts", 0) >= ts_ms), key=lambda e: e["origin_server_ts"])

    def wait(self, ts_ms: int, pred, timeout: float = 20, sender_not: str = "@lena.hofer:klinikum.example") -> dict:
        def f():
            for e in self.since(ts_ms):
                if e.get("sender") != sender_not and pred(body_of(e)):
                    return e
        return poll(f, timeout, what="bot message")


def body_of(e: dict) -> str:
    c = e.get("content") or {}
    return (c.get("body") or "") + "\n" + (c.get("formatted_body") or "")


# ------------------------------------------------------------------ HAPI
class Hapi:
    TYPES = ["Task", "DiagnosticReport", "Observation", "DocumentReference", "Provenance", "AuditEvent",
             "Practitioner", "PractitionerRole", "Organization", "MedicationStatement", "Binary", "Bundle", "Patient",
             "Communication", "List", "Basic"]

    def __init__(self, base: str):
        self.base = base

    def get(self, ref: str) -> dict | None:
        try:
            return call("GET", f"{self.base}/{ref}", headers={"Accept": "application/fhir+json"})[1]
        except HttpError as e:
            if e.status in (404, 410):
                return None
            raise

    def search(self, rtype: str, params: str = "", max_pages: int = 200) -> list[dict]:
        url = f"{self.base}/{rtype}?_count=500" + (f"&{params}" if params else "")
        out = []
        for _ in range(max_pages):
            b = call("GET", url, headers={"Accept": "application/fhir+json"}, timeout=60)[1]
            out += [e["resource"] for e in b.get("entry") or [] if "resource" in e]
            nxt = next((l["url"] for l in b.get("link") or [] if l.get("relation") == "next"), None)
            if not nxt:
                return out
            url = nxt
        return out

    def count(self, rtype: str, params: str = "") -> int:
        b = call("GET", f"{self.base}/{rtype}?_summary=count" + (f"&{params}" if params else ""),
                 headers={"Accept": "application/fhir+json"})[1]
        return int(b.get("total", 0))

    def dump(self, types: list[str] | None = None) -> dict[str, list[dict]]:
        types = types or self.TYPES

        def one(t):
            try:
                return t, self.search(t)
            except HttpError:
                return t, []
        with ThreadPoolExecutor(8) as ex:
            return dict(ex.map(one, types))

    def history(self, ref: str) -> list[dict]:
        b = call("GET", f"{self.base}/{ref}/_history?_count=100", headers={"Accept": "application/fhir+json"})[1]
        return [e["resource"] for e in b.get("entry") or [] if "resource" in e]


# ------------------------------------------------------------------ service API (what the bot calls)
DEMO_PINS: dict[tuple, str] = {}  # (given, family, birthdate) -> PIN from PGK_DEMO's demo_identity
HELD = {"needs_confirmation", "confirmation_required", "confirm_required", "held", "warning", "warnings", "dry_run",
        "pending_confirmation"}


class Service:
    def __init__(self, st: Stack):
        self.st = st
        self.h = {"X-API-Key": st.api_key} if st.api_key else {}
        self._addr_locks: dict[str, threading.Lock] = {}
        self._lk = threading.Lock()

    def lock(self, address: str) -> threading.Lock:
        with self._lk:
            return self._addr_locks.setdefault(address.casefold(), threading.Lock())

    REQUESTER = "@lena.hofer:klinikum.example"

    def who(self, actor: str | None = None, room: str | None = None) -> dict:
        """Headers for every per-request call: the requester, acting in the request's room (403 otherwise)."""
        return {**self.h, "X-PGK-Actor": actor or self.REQUESTER, "X-PGK-Room": room or self.st.room_id or ""}

    @staticmethod
    def warning_codes(resp: dict) -> list[str]:
        for key in ("warning_details", "warnings"):
            codes = [w["code"] for w in resp.get(key) or [] if isinstance(w, dict) and w.get("code")]
            if codes:
                return sorted(set(codes))
        return []

    def create(self, body: dict, confirm: bool = True) -> dict:
        """Create as the requester in her room (X-PGK-Actor / X-PGK-Room). If the service holds it for confirmation,
        confirm with EVERY warning code acknowledged explicitly (no blanket confirm; the service answers 409 otherwise)."""
        body = {"requester_mxid": self.REQUESTER, "room_id": self.st.room_id, **body}
        r = call("POST", f"{self.st.service}/api/requests", body, self.who(), ok=(200, 201, 202))[1]
        demo = r.get("demo_identity")  # PGK_DEMO enrols a persona for a new recipient name and returns its PIN
        if isinstance(demo, dict) and demo.get("pin"):
            DEMO_PINS[(demo.get("given"), demo.get("family"), demo.get("birthdate"))] = demo["pin"]
        if confirm and str(r.get("status", "")).lower() in HELD:
            rid = r["request_id"]
            codes = self.warning_codes(r) or self.warning_codes(
                call("POST", f"{self.st.service}/api/requests", {**body, "dry_run": True}, self.who(), ok=(200, 201))[1])
            try:
                r = {**r, **call("POST", f"{self.st.service}/api/requests/{rid}/confirm",
                                 {"acknowledged_warnings": codes}, self.who(), ok=(200, 201, 202))[1],
                     "acknowledged": codes}
            except HttpError as e:
                if e.status not in (404, 405):
                    raise
                r = call("POST", f"{self.st.service}/api/requests", {**body, "confirm": True}, self.who(),
                         ok=(200, 201, 202))[1]
        return r

    def audit_all(self) -> list[dict]:
        """The whole service audit chain, ascending, via after_id paging (operator view: no actor headers)."""
        out, after = [], 0
        while True:
            r = call("GET", f"{self.st.service}/api/audit?after_id={after}&limit=10000", headers=self.h)[1]
            rows = r.get("entries") if isinstance(r, dict) else r
            out += rows or []
            after = r.get("next_after_id") if isinstance(r, dict) else None
            if not after or not rows:
                return out

    def decide(self, rid: str, action: str, reason: str, actor: str | None = "@lena.hofer:klinikum.example",
               room: str | None = None) -> tuple[int, dict | str]:
        """Dr. Hofer's decision on a held answer. X-PGK-Actor / X-PGK-Room must be the request's requester and room."""
        h = dict(self.h)  # built by hand: the decision flow also sends forged or missing headers on purpose
        if actor:
            h["X-PGK-Actor"] = actor
        if room or (room is None and self.st.room_id):
            h["X-PGK-Room"] = room or self.st.room_id
        st, body, _ = call("POST", f"{self.st.service}/api/requests/{rid}/decision", {"action": action, "reason": reason},
                           h, ok=None)
        return st, body

    def audit(self, request_id: str | None = None) -> list[dict]:
        """One request's audit (requester + room headers), or the newest page of the operator view (key only)."""
        url = f"{self.st.service}/api/audit" + (f"?request_id={urllib.parse.quote(request_id)}" if request_id else "")
        r = call("GET", url, headers=self.who() if request_id else self.h)[1]
        return r if isinstance(r, list) else r.get("entries") or r.get("audit") or r.get("items") or []

    def task(self, rid: str) -> dict | None:
        """The FHIR Task of a request (Task.identifier = request id), read from HAPI."""
        b = call("GET", f"{self.st.hapi}/Task?identifier={urllib.parse.quote(rid)}&_count=5",
                 headers={"Accept": "application/fhir+json"})[1]
        es = [e["resource"] for e in b.get("entry") or []]
        return es[0] if es else None

    def sms_page(self) -> str:
        return call("GET", f"{self.st.service}/sms", headers={"Accept": "text/html", **self.h})[1]  # inbox needs the key

    def enrol(self, given: str, family: str, birthdate: str) -> str:
        """Enrol a test person in the simulated ID Austria (operator API); returns the PIN (shown once)."""
        return call("POST", f"{self.st.service}/api/eid/personas", {"given": given, "family": family,
                                                                    "birthdate": birthdate}, self.h, ok=(200, 201))[1]["pin"]


def sms_links_for(html: str, number: str) -> list[str]:
    """Links that appear after the given phone number on the SMS page (document order)."""
    digits = re.sub(r"\D", "", number)
    text = re.sub(r"<[^>]+>", " ", html) + " " + html  # visible text and raw hrefs
    out, cur = [], None
    for tok in re.finditer(r"((?:\+|\b00)\d[\d \-/]{6,}\d)|" + LINK_RE.pattern, text):
        if tok.group(1):
            cur = re.sub(r"\D", "", tok.group(1))
        elif cur and (cur == digits or cur.endswith(digits[-9:])):
            u = tok.group(0).rstrip(".,;")
            if u not in out:
                out.append(u)
    return out


def now_ms() -> int:
    return int(time.time() * 1000)

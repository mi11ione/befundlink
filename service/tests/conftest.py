"""Fixtures: fake HAPI FHIR + fake bot (httpx.MockTransport), capturing mailer, app on a temp DB."""
from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from service.app import create_app
from service.config import Settings

FIX = Path(__file__).parent / "fixtures"
HAPI = "http://hapi.test/fhir"
BOT = "http://bot.test"
MATRIX = "http://matrix.test"
BOT_USER = "@bot:klinikum.example"
ROOM = "!room:klinikum.example"
AUDIT_ROOM = "!audit:klinikum.example"


def doc(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeWorld:
    """In-memory HAPI (Patient read, AuditEvent create/read/search, Task PUT, transaction), bot /notify, and the
    Matrix room the bot posts into (whoami + room history, for the anchor verifier)."""

    def __init__(self):
        self.patients = {"maria-beispiel": json.loads((FIX / "patient.json").read_text()),
                         "max-beispiel": json.loads((FIX.parents[2] / "infra" / "patient-max.json").read_text())}
        self.resources: dict[str, dict] = {}
        self.transactions: list[dict] = []
        self.notifications: list[dict] = []
        self.task_puts: list[dict] = []
        self.ids = itertools.count(1000)
        self.fail_transactions = False
        self.validated: list[list[dict]] = []
        self.validation_errors: list[str] = []
        self.validation_mode = "tx"
        self.versions: dict[str, int] = {}
        self.history: dict[str, dict] = {}  # 'Type/id/_history/n' -> that version (immutable, like HAPI)
        self.before_put = None  # hook(path) to simulate a concurrent write between a client's read and its PUT
        self.room: list[dict] = []  # Matrix events the bot posted (oldest first)

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == f"{BOT}/notify":
            note = json.loads(req.content)
            self.notifications.append(note)
            event_id = f"$ev{len(self.room) + 1}"
            self.room.append({"event_id": event_id, "sender": BOT_USER, "type": "m.room.message",
                              "room_id": note.get("room_id"), "content": {"msgtype": "m.text", "body": note["text"]}})
            return httpx.Response(200, json={"ok": True, "event_id": event_id})
        if url.startswith(MATRIX):
            return self.matrix(req)
        assert url.startswith(HAPI), url
        path = url[len(HAPI):].split("?")[0].strip("/")
        if req.method == "GET":
            if "/" not in path and req.url.params.get("identifier"):  # search by identifier (system|value)
                system, _, value = req.url.params["identifier"].partition("|")
                hits = [r for k, r in self.resources.items() if k.startswith(path + "/")
                        and any(i.get("system") == system and i.get("value") == value for i in r.get("identifier") or [])]
                return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset",
                                                 "entry": [{"resource": h} for h in hits]})
            if "/" not in path and req.url.params.get("subtype"):  # search by subtype (system|code)
                system, _, code = req.url.params["subtype"].partition("|")
                hits = [r for k, r in self.resources.items() if k.startswith(path + "/")
                        and any(c.get("system") == system and c.get("code") == code for c in r.get("subtype") or [])]
                return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset",
                                                 "entry": [{"resource": h} for h in hits]})
            if "/" not in path and req.url.params.get("subject"):  # search by subject reference
                subject = req.url.params["subject"]
                hits = [{**r, "meta": {"versionId": str(self.versions.get(k, 1))}} for k, r in self.resources.items()
                        if k.startswith(path + "/") and (r.get("subject") or {}).get("reference") == subject]
                return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset",
                                                 "entry": [{"resource": h} for h in hits]})
            if path.endswith("/_history"):  # the version list of one resource
                base = path[: -len("/_history")]
                entries = [{"resource": v} for k, v in self.history.items() if k.startswith(base + "/_history/")]
                return httpx.Response(200, json={"resourceType": "Bundle", "type": "history", "entry": entries})
            if "/_history/" in path:
                old = self.history.get(path)
                return httpx.Response(200, json=old) if old else httpx.Response(404)
            m = re.fullmatch(r"Patient/([^/]+)", path)
            if m:
                p = self.patients.get(m.group(1))
                return httpx.Response(200, json=p) if p else httpx.Response(404, json={"resourceType": "OperationOutcome"})
            if path in self.resources:
                version = str(self.versions.get(path, 1))
                return httpx.Response(200, json={**self.resources[path], "meta": {"versionId": version}},
                                      headers={"ETag": f'W/"{version}"'})
            return httpx.Response(404)
        body = json.loads(req.content)
        if req.method == "PUT":
            if self.before_put:
                self.before_put(path)
            im = req.headers.get("If-Match")
            if im and path in self.resources and im != f'W/"{self.versions.get(path, 1)}"':
                return httpx.Response(412, json={"resourceType": "OperationOutcome"})
            self.put(path, body)
            if body["resourceType"] == "Task":
                self.task_puts.append(body)
            return httpx.Response(200, json=body, headers={"Location": f"{HAPI}/{path}/_history/1"})
        if req.method == "POST" and path == "":
            assert body["resourceType"] == "Bundle" and body["type"] == "transaction"
            if self.fail_transactions:
                return httpx.Response(500, json={"resourceType": "OperationOutcome"})
            for e in body["entry"]:  # like HAPI: a PUT with ifMatch on an older version fails the whole bundle
                im = e["request"].get("ifMatch")
                url = e["request"]["url"]
                if im and url in self.resources and im != f'W/"{self.versions.get(url, 1)}"':
                    return httpx.Response(412, json={"resourceType": "OperationOutcome"})
            self.transactions.append(body)
            out, ids = [], {}
            for e in body["entry"]:
                res = e["resource"]
                rid = str(next(self.ids))
                if e["request"]["method"] == "PUT":
                    rid = e["request"]["url"].split("/")[1]
                ids[e["fullUrl"]] = f"{res['resourceType']}/{rid}"
            for e in body["entry"]:  # like HAPI: urn:uuid references -> server references
                txt = json.dumps(e["resource"])
                for full, ref in ids.items():
                    txt = txt.replace(f'"{full}"', f'"{ref}"')
                res = json.loads(txt)
                ref = ids[e["fullUrl"]]
                version = self.put(ref, {**res, "id": ref.split("/")[1]})
                out.append({"response": {"status": "201 Created", "location": f"{ref}/_history/{version}"}})
            return httpx.Response(200, json={"resourceType": "Bundle", "type": "transaction-response", "entry": out})
        if req.method == "POST":
            rid = str(next(self.ids))
            self.put(f"{path}/{rid}", {**body, "id": rid})
            return httpx.Response(201, json={**body, "id": rid}, headers={"Location": f"{HAPI}/{path}/{rid}/_history/1"})
        return httpx.Response(405)

    def put(self, ref: str, resource: dict) -> int:
        """Write a new version of `ref` (as any HAPI client could, e.g. an unauthorised edit). Returns its number."""
        version = self.versions.get(ref, 0) + 1
        self.versions[ref] = version
        self.resources[ref] = resource
        stamp = datetime.now(timezone.utc).isoformat()
        self.history[f"{ref}/_history/{version}"] = {**json.loads(json.dumps(resource)),
                                                     "meta": {"versionId": str(version), "lastUpdated": stamp}}
        return version

    def matrix(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/_matrix/client/v3/account/whoami":
            return httpx.Response(200, json={"user_id": BOT_USER})
        if path.endswith("/messages"):  # newest first, one page; events of that room (or of no stated room)
            room = req.url.path.split("/rooms/")[1].split("/")[0]
            events = [e for e in self.room if e.get("room_id") in (None, unquote(room))]
            return httpx.Response(200, json={"chunk": list(reversed(events)), "start": "s0"})
        return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})

    def audit_events(self) -> list[dict]:
        return [v for k, v in self.resources.items() if k.startswith("AuditEvent/")]


class CaptureMailer:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, to: str, subject: str, body: str, html: str | None = None) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body, "html": html})

    def last_link(self) -> str:
        m = re.search(r"http://svc\.test(/r/[bcdfghjkmnpqrstvwxz]{60})", self.sent[-1]["body"])
        return m.group(1)


@pytest.fixture
def world():
    return FakeWorld()


@pytest.fixture
def mailer():
    return CaptureMailer()


@pytest.fixture
def settings(tmp_path):
    return Settings(base_url="http://svc.test", hapi_url=HAPI, bot_url=BOT, db_path=str(tmp_path / "t.sqlite"),
                    secret=b"test-secret-key-0123456789abcdef", default_room_id=ROOM, prewarm_tx=False, dev=True,
                    matrix_url=MATRIX, matrix_token="test-token", audit_room_id=AUDIT_ROOM,
                    async_io=False, validator_server=False,
                    data_key_hex="11" * 32)


@pytest.fixture
def app_client(settings, world, mailer):
    http = httpx.Client(transport=httpx.MockTransport(world.handler))
    app = create_app(settings, mailer=mailer, http=http)
    # The HL7 validator (Java, minutes) is exercised live, not in unit tests: record what would be validated.
    app.state.svc._validate = lambda resources: (world.validated.append(resources) or world.validation_errors,
                                                 {"mode": world.validation_mode, "seconds": 0})
    with TestClient(app, base_url="http://svc.test") as c:
        c.svc = app.state.svc
        yield c


REQ1 = {"requester_mxid": "@lena.hofer:klinikum.example", "room_id": ROOM,
        "recipient": {"name": "Dr. Eva Brandl", "role": "Laborärztin", "org": "Labor Kanalstraße"},
        "channel": "email", "address": "eva.brandl@labor-kanal.example", "patient_id": "maria-beispiel",
        "doc_kind": "lab_result", "note": "Gerinnung vor OP, für Maria"}
REQ2 = {"requester_mxid": "@lena.hofer:klinikum.example", "room_id": ROOM,
        "recipient": {"name": "Stefan Kern", "role": "Entlassungsmanagement", "org": "Reha Waldblick"},
        "channel": "sms", "address": "+43 660 000 0101", "patient_id": "maria-beispiel", "doc_kind": "discharge_report"}

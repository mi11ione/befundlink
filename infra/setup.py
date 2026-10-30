"""Idempotent demo setup: Matrix users + rooms (Dr. Hofer's room, the audit room), HAPI patients (Maria Beispiel,
Max Beispiel), infra/state.json.

Stdlib only (runs with any python3). Called by infra/setup.sh, which passes the
Synapse registration shared secret via the SYNAPSE_REG_SECRET env var.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INFRA = Path(__file__).resolve().parent
REPO = INFRA.parent
STATE = INFRA / "state.json"
PATIENT_FILE = REPO.parent / "track2" / "scenario" / "patient.json"
if not PATIENT_FILE.exists():  # repo copied elsewhere: fall back to the vendored copy
    PATIENT_FILE = INFRA / "patient.json"

HS = os.environ.get("MATRIX_HS", "http://localhost:8008")
SERVER_NAME = "klinikum.example"
FHIR = os.environ.get("FHIR_BASE", "http://localhost:8090/fhir")
MAILPIT = os.environ.get("MAILPIT_API", "http://localhost:8025")

USERS = {
    "lena": {"localpart": "lena.hofer", "password": "hofer-demo-pw", "displayname": "Dr. Lena Hofer", "device_id": "PGKSETUP_LENA"},
    "bot": {"localpart": "bridge-bot", "password": "bot-demo-pw", "displayname": "Befundlink", "device_id": "PGKBRIDGEBOT"},
}
ROOM_NAME = "Dr. Hofer – Anforderungen"
ROOM_ALIAS_LOCAL = "hofer-anforderungen"
ROOM_ALIAS = f"#{ROOM_ALIAS_LOCAL}:{SERVER_NAME}"
ROOM_TOPIC = "Dokumentanforderungen an Personen außerhalb des Netzes (Befundlink)"
# the audit-chain anchors (hex, every 10 min) go to their own room, not between patient cards
AUDIT_ROOM_NAME = "Befundlink Audit"
AUDIT_ROOM_ALIAS_LOCAL = "pgk-bridge-audit"
AUDIT_ROOM_TOPIC = "Audit-Anker von Befundlink (Hash der Audit-Kette, alle 10 Minuten; wird von make audit-verify gelesen)"
MAX_PATIENT_FILE = INFRA / "patient-max.json"
DIRECTORY_FILE = INFRA / "directory.json"  # Dr. Hofer's Practitioner + the clinic's Organization  # the 'other patient' of scenario/documents/lab-result-max-beispiel.md


class HTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


def req(method: str, url: str, body=None, token: str | None = None, ctype="application/json", timeout=10):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = ctype
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, (json.loads(raw) if raw else {})
            except ValueError:
                return resp.status, {"_text": raw.decode(errors="replace")}
    except urllib.error.HTTPError as e:
        raise HTTPError(e.code, e.read().decode(errors="replace")) from None


def wait_for(name: str, url: str, deadline_s: float = 180):
    t0 = time.monotonic()
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    print(f"  {name} ready after {time.monotonic() - t0:.1f}s")
                    return
        except Exception:
            pass
        if time.monotonic() - t0 > deadline_s:
            sys.exit(f"ERROR: {name} not ready at {url} after {deadline_s}s (see `make logs`)")
        time.sleep(0.5)


def mxid(localpart: str) -> str:
    return f"@{localpart}:{SERVER_NAME}"


def whoami(token: str) -> str | None:
    try:
        return req("GET", f"{HS}/_matrix/client/v3/account/whoami", token=token)[1].get("user_id")
    except Exception:
        return None


def login(u: dict) -> str | None:
    try:
        _, r = req("POST", f"{HS}/_matrix/client/v3/login", {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": u["localpart"]},
            "password": u["password"],
            "device_id": u["device_id"],
            "initial_device_display_name": "Befundlink setup",
        })
        return r["access_token"]
    except HTTPError as e:
        if e.status == 403:
            return None
        raise


def register(u: dict, secret: str):
    url = f"{HS}/_synapse/admin/v1/register"
    nonce = req("GET", url)[1]["nonce"]
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha1)
    for part in (nonce, u["localpart"], u["password"], "notadmin"):
        mac.update(part.encode())
        if part != "notadmin":
            mac.update(b"\x00")
    req("POST", url, {
        "nonce": nonce, "username": u["localpart"], "password": u["password"],
        "displayname": u["displayname"], "admin": False, "mac": mac.hexdigest(),
    })


def ensure_user(key: str, secret: str, old: dict) -> str:
    u = USERS[key]
    tok = old.get("tokens", {}).get(key)
    if tok and whoami(tok) == mxid(u["localpart"]):
        print(f"  {mxid(u['localpart'])}: existing token valid")
        return tok
    tok = login(u)
    if tok is None:
        try:
            register(u, secret)
            print(f"  {mxid(u['localpart'])}: registered")
        except HTTPError as e:
            if "User ID already taken" in e.body:
                sys.exit(f"ERROR: {mxid(u['localpart'])} exists with a different password; run `make clean` to reset")
            raise
        tok = login(u)
    else:
        print(f"  {mxid(u['localpart'])}: logged in")
    assert tok, f"login failed for {u['localpart']}"
    return tok


def ensure_room(lena: str, bot: str, alias_local: str = ROOM_ALIAS_LOCAL, name: str = ROOM_NAME,
                topic: str = ROOM_TOPIC) -> str:
    bot_id = mxid(USERS["bot"]["localpart"])
    q = urllib.parse.quote(f"#{alias_local}:{SERVER_NAME}")
    try:
        room_id = req("GET", f"{HS}/_matrix/client/v3/directory/room/{q}")[1]["room_id"]
        print(f"  room exists: {room_id}")
    except HTTPError as e:
        if e.status != 404:
            raise
        room_id = req("POST", f"{HS}/_matrix/client/v3/createRoom", {
            "name": name,
            "topic": topic,
            "preset": "private_chat",
            "room_alias_name": alias_local,
            "invite": [bot_id],
            "is_direct": False,
            # v11 keeps classic room ids (!x:klinikum.example); v12 drops the server part.
            "room_version": "11",
        }, token=lena)[1]["room_id"]
        print(f"  room created: {room_id}")
    rid = urllib.parse.quote(room_id)
    # Lena joined (no-op if already)
    req("POST", f"{HS}/_matrix/client/v3/rooms/{rid}/join", {}, token=lena)
    # Bot: invite if needed, then join (join is idempotent)
    try:
        req("POST", f"{HS}/_matrix/client/v3/rooms/{rid}/join", {}, token=bot)
    except HTTPError:
        try:
            req("POST", f"{HS}/_matrix/client/v3/rooms/{rid}/invite", {"user_id": bot_id}, token=lena)
        except HTTPError as e:
            if "already in the room" not in e.body:
                raise
        req("POST", f"{HS}/_matrix/client/v3/rooms/{rid}/join", {}, token=bot)
    # the display name and topic follow the code (renamed rooms keep their alias and id)
    req("PUT", f"{HS}/_matrix/client/v3/rooms/{rid}/state/m.room.name", {"name": name}, token=lena)
    req("PUT", f"{HS}/_matrix/client/v3/rooms/{rid}/state/m.room.topic", {"topic": topic}, token=lena)
    # Let the bot post notices/alerts with formatting and give it moderator power for redactions.
    pl = req("GET", f"{HS}/_matrix/client/v3/rooms/{rid}/state/m.room.power_levels", token=lena)[1]
    if pl.get("users", {}).get(bot_id) != 50:
        pl.setdefault("users", {})[bot_id] = 50
        req("PUT", f"{HS}/_matrix/client/v3/rooms/{rid}/state/m.room.power_levels", pl, token=lena)
    for key in ("lena", "bot"):
        tok = lena if key == "lena" else bot
        joined = req("GET", f"{HS}/_matrix/client/v3/joined_rooms", token=tok)[1]["joined_rooms"]
        assert room_id in joined, f"{key} not joined to {room_id}"
    return room_id


def ensure_patient(path: Path = PATIENT_FILE) -> str:
    patient = json.loads(path.read_text(encoding="utf-8"))
    pid = patient["id"]
    assert pid in ("maria-beispiel", "max-beispiel"), pid
    url = f"{FHIR}/Patient/{pid}"
    try:
        _, cur = req("GET", url, ctype="application/fhir+json")
        cur_cmp = {k: v for k, v in cur.items() if k not in ("meta", "text")}
        if cur_cmp == patient:
            print(f"  {url}: up to date (version {cur['meta']['versionId']})")
            return url
    except HTTPError as e:
        if e.status not in (404, 410):
            raise
    status, r = req("PUT", url, json.dumps(patient).encode(), ctype="application/fhir+json", timeout=30)
    print(f"  {url}: PUT {status} (version {r['meta']['versionId']})")
    return url


def ensure_directory() -> dict:
    """PUT the PGK members' Practitioner and Organization (Task.requester, AuditEvent agents reference them)."""
    refs = {}
    for res in json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["resources"]:
        ref = f"{res['resourceType']}/{res['id']}"
        status, _ = req("PUT", f"{FHIR}/{ref}", json.dumps(res).encode(), ctype="application/fhir+json", timeout=30)
        print(f"  {FHIR}/{ref}: PUT {status}")
        refs.setdefault(res["resourceType"].lower(), ref)
    return refs


def main():
    secret = os.environ.get("SYNAPSE_REG_SECRET", "").strip()
    if not secret:
        sys.exit("ERROR: SYNAPSE_REG_SECRET not set (run via infra/setup.sh)")
    old = json.loads(STATE.read_text()) if STATE.exists() else {}

    print("waiting for services")
    wait_for("synapse", f"{HS}/health")
    wait_for("mailpit", f"{MAILPIT}/livez")
    wait_for("hapi", f"{FHIR}/metadata", deadline_s=300)

    print("matrix")
    lena = ensure_user("lena", secret, old)
    bot = ensure_user("bot", secret, old)
    for key, tok in (("lena", lena), ("bot", bot)):
        u = USERS[key]
        req("PUT", f"{HS}/_matrix/client/v3/profile/{urllib.parse.quote(mxid(u['localpart']))}/displayname",
            {"displayname": u["displayname"]}, token=tok)
    room_id = ensure_room(lena, bot)
    audit_room_id = ensure_room(lena, bot, AUDIT_ROOM_ALIAS_LOCAL, AUDIT_ROOM_NAME, AUDIT_ROOM_TOPIC)

    print("fhir")
    patient_url = ensure_patient()
    max_url = ensure_patient(MAX_PATIENT_FILE)
    directory = ensure_directory()

    state = {
        "homeserver": HS,
        "server_name": SERVER_NAME,
        "room_id": room_id,
        "room_alias": ROOM_ALIAS,
        "room_name": ROOM_NAME,
        "audit_room_id": audit_room_id,
        "audit_room_alias": f"#{AUDIT_ROOM_ALIAS_LOCAL}:{SERVER_NAME}",
        "audit_room_name": AUDIT_ROOM_NAME,
        "users": {
            "lena": {"user_id": mxid(USERS["lena"]["localpart"]), "password": USERS["lena"]["password"],
                     "device_id": USERS["lena"]["device_id"]},
            "bot": {"user_id": mxid(USERS["bot"]["localpart"]), "password": USERS["bot"]["password"],
                    "device_id": USERS["bot"]["device_id"]},
        },
        "tokens": {"lena": lena, "bot": bot},
        "fhir_base": FHIR,
        "patient_id": "maria-beispiel",
        "patient_ref": "Patient/maria-beispiel",
        "patient_url": patient_url,
        "patients": {"maria-beispiel": patient_url, "max-beispiel": max_url},
        "requester": {"practitioner": directory.get("practitioner"), "organization": directory.get("organization")},
        "smtp": {"host": "localhost", "port": 1025},
        "mailpit_api": f"{MAILPIT}/api/v1",
        "element_url": "http://localhost:8080",
    }
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE)
    print(f"wrote {STATE}")


if __name__ == "__main__":
    main()

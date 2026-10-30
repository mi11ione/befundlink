"""Infra smoke test (stdlib only). Exit code 0 = all checks pass."""
from __future__ import annotations

import json
import smtplib
import sys
import time
import urllib.parse
import uuid
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup import HS, FHIR, MAILPIT, ROOM_ALIAS, ROOM_NAME, STATE, USERS, HTTPError, req  # noqa: E402

failures: list[str] = []


def check(name: str, fn):
    t0 = time.monotonic()
    try:
        detail = fn()
        print(f"PASS  {name} ({(time.monotonic() - t0) * 1000:.0f} ms){': ' + detail if detail else ''}")
    except Exception as e:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL  {name}: {e}")


def get_text(url: str) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


state = json.loads(STATE.read_text())


def element():
    html = get_text("http://localhost:8080/")
    assert "Element" in html, "index.html does not look like Element"
    cfg = json.loads(get_text("http://localhost:8080/config.json"))
    base = cfg["default_server_config"]["m.homeserver"]["base_url"]
    assert base == "http://localhost:8008", base
    return f"config base_url={base}"


lena_tok = {}


def lena_login():
    _, r = req("POST", f"{HS}/_matrix/client/v3/login", {
        "type": "m.login.password", "identifier": {"type": "m.id.user", "user": "lena.hofer"},
        "password": USERS["lena"]["password"], "device_id": "PGKSMOKE",
    })
    assert r["user_id"] == "@lena.hofer:klinikum.example", r["user_id"]
    lena_tok["t"] = r["access_token"]
    return r["user_id"]


def state_tokens():
    for key in ("lena", "bot"):
        uid = req("GET", f"{HS}/_matrix/client/v3/account/whoami", token=state["tokens"][key])[1]["user_id"]
        assert uid == state["users"][key]["user_id"], uid
    return "lena + bot tokens valid"


def room():
    rid = req("GET", f"{HS}/_matrix/client/v3/directory/room/{urllib.parse.quote(ROOM_ALIAS)}")[1]["room_id"]
    assert rid == state["room_id"], (rid, state["room_id"])
    assert rid.endswith(":klinikum.example"), rid
    q = urllib.parse.quote(rid)
    tok = lena_tok.get("t") or state["tokens"]["lena"]
    name = req("GET", f"{HS}/_matrix/client/v3/rooms/{q}/state/m.room.name", token=tok)[1]["name"]
    assert name == ROOM_NAME, name
    members = set(req("GET", f"{HS}/_matrix/client/v3/rooms/{q}/joined_members", token=tok)[1]["joined"])
    need = {"@lena.hofer:klinikum.example", "@bridge-bot:klinikum.example"}
    assert need <= members, members
    return f"{rid} '{name}' members={sorted(members)}"


def patient():
    _, p = req("GET", f"{FHIR}/Patient/maria-beispiel")
    assert p["id"] == "maria-beispiel" and p["birthDate"] == "1961-03-12", p
    return f"{p['name'][0]['given'][0]} {p['name'][0]['family']} {p['birthDate']} v{p['meta']['versionId']}"


def mailpit_api():
    _, r = req("GET", f"{MAILPIT}/api/v1/messages?limit=1")
    assert "messages" in r and "total" in r, r
    return f"total={r['total']}"


def smtp_roundtrip():
    subject = f"pgk-smoke-{uuid.uuid4().hex[:12]}"
    m = EmailMessage()
    m["From"] = "noreply@klinikum.example"
    m["To"] = "smoke@labor-kanal.example"
    m["Subject"] = subject
    m.set_content("smoke test")
    with smtplib.SMTP("localhost", 1025, timeout=5) as s:
        s.send_message(m)
    q = urllib.parse.quote(f'subject:"{subject}"')
    for _ in range(20):
        _, r = req("GET", f"{MAILPIT}/api/v1/search?query={q}")
        if r.get("messages"):
            ids = [x["ID"] for x in r["messages"]]
            req("DELETE", f"{MAILPIT}/api/v1/messages", {"IDs": ids})  # keep the inbox clean
            return f"found {subject} (then deleted)"
        time.sleep(0.1)
    raise AssertionError(f"{subject} not in mailpit")


check("element web loads + points at localhost:8008", element)
check("lena.hofer password login", lena_login)
check("state.json tokens valid", state_tokens)
check("room exists, both joined", room)
check("HAPI Patient/maria-beispiel", patient)
check("mailpit API lists messages", mailpit_api)
check("SMTP :1025 -> mailpit", smtp_roundtrip)

if lena_tok.get("t"):
    try:
        req("POST", f"{HS}/_matrix/client/v3/logout", {}, token=lena_tok["t"])
    except HTTPError:
        pass

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)

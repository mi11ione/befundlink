"""Concurrency of the audit chain, Vienna offsets, no filenames in the audit, validator fallback, never-500."""
import json
import re
import threading

import fhirmap

from service import core as core_mod

from .conftest import REQ1, doc
from .test_e2e import HDR, login, send, upload

VIENNA_OFFSET = re.compile(r"\+0[12]:00$")


def test_concurrent_audit_appends_form_one_chain(app_client, world):
    svc = app_client.svc
    send(app_client, REQ1)
    rid = svc.store.q("SELECT id FROM requests")[0]["id"]
    steps = ["link opened", "identity checked", "answer sent", "misdelivery caught", "request awaiting confirmation"]
    errors = []

    def worker(i):
        try:
            for k in range(10):
                svc.audit(rid, steps[(i + k) % len(steps)], True, f"worker {i}", f"concurrent entry {i}/{k}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    v = svc.verify_audit(check_fhir=True)
    assert v["ok"], v["problems"][:5]
    evs = [json.loads(f["event"]) for f in svc.store.q("SELECT event FROM audit_fhir ORDER BY audit_id")]
    assert len(evs) == v["fhir_checked"] and fhirmap.verify_audit_chain(evs) == []
    prevs = [next(d["valueString"] for d in e["entity"][0].get("detail", []) if d["type"] == "prev_hash")
             for e in evs[1:]]
    assert len(prevs) == len(set(prevs))  # no two events share a predecessor: no fork


def test_timestamps_carry_vienna_offset(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    rows = c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]
    assert rows and all(VIENNA_OFFSET.search(r["at"]) for r in rows)
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert VIENNA_OFFSET.search(st["created_at"]) and VIENNA_OFFSET.search(st["expires_at"])
    tx = world.transactions[-1]
    prov = next(e["resource"] for e in tx["entry"] if e["resource"]["resourceType"] == "Provenance")
    task = next(e["resource"] for e in tx["entry"] if e["resource"]["resourceType"] == "Task")
    assert VIENNA_OFFSET.search(prov["recorded"]) and VIENNA_OFFSET.search(task["lastModified"])
    assert all(VIENNA_OFFSET.search(e["recorded"]) for e in world.audit_events())


def test_filename_never_in_audit(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-max-beispiel.md"), "lab-result-max-beispiel.md")
    blob = json.dumps(c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()) + json.dumps(world.audit_events())
    assert "max-beispiel" not in blob.casefold() and ".md" not in blob
    row = c.svc.store.one("SELECT filename, content FROM answers WHERE request_id=?", (req["request_id"],))
    assert row["filename"] is None and row["content"] is None  # blocked: nothing of it stored


def test_active_content_blocked_and_not_stored(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    evil = doc("lab-result-maria-beispiel.md").replace("All values", "<img src=x onerror=alert(1)> All values")
    r = upload(c, link, evil)
    assert r.status_code == 422 and "Programmcode" in r.text
    assert not world.transactions and world.notifications[-1]["kind"] == "misdelivery"
    assert "onerror" not in json.dumps(world.notifications[-1]) and "alert(1)" not in json.dumps(world.audit_events())
    assert c.svc.store.one("SELECT content FROM answers WHERE request_id=?", (req["request_id"],))["content"] is None


def test_requesting_clinic_name_in_recipient_org_is_not_a_leak(app_client, mailer):
    body = {**REQ1, "recipient": {"name": "Dr. Eva Brandl", "role": "Oberärztin", "org": "Klinikum Nordstadt"},
            "address": "eva.brandl@klinikum-nord.example", "doc_kind": "discharge_report", "confirm_warnings": True}
    codes = [w["code"] for w in app_client.post("/api/requests/check", json=body).json()["warning_details"]]
    r = app_client.post("/api/requests", json={**body, "acknowledged_warnings": codes}, headers=HDR)
    assert r.status_code == 201, r.text
    assert "Nordstadt" not in mailer.sent[-1]["body"]


def test_request_to_the_patient_herself_never_names_her(app_client, mailer):
    body = {**REQ1, "recipient": {"name": "Maria Beispiel", "role": "Patientin", "org": "privat"},
            "address": "maria@privat.example", "confirm_warnings": True}
    codes = [w["code"] for w in app_client.post("/api/requests/check", json=body).json()["warning_details"]]
    r = app_client.post("/api/requests", json={**body, "acknowledged_warnings": codes}, headers=HDR)
    assert r.status_code == 201, r.text
    m = mailer.sent[-1]
    assert "Maria" not in m["subject"] + m["body"] and "Beispiel," not in m["body"] and "Guten Tag," in m["body"]


def test_pii_guard_treats_recipient_as_patient_name():
    from service import messages
    f = messages.pii_findings("Diese Nachricht ist für Maria Beispiel bestimmt.", "Maria Beispiel", "1961-03-12")
    assert f                                    # the guard itself catches the name whatever its role


def test_correct_answer_filed_when_tx_unreachable(settings, world, mailer, monkeypatch):
    """Infrastructure (tx.fhir.org down) never holds a correct answer: auto mode falls back to offline,
    the answer is filed, the mode is recorded and a tx re-validation is scheduled."""
    import httpx
    from fastapi.testclient import TestClient
    from service.app import create_app
    app = create_app(settings, mailer=mailer, http=httpx.Client(transport=httpx.MockTransport(world.handler)))
    svc = app.state.svc
    calls, scheduled = [], []

    def fake_report(resources, mode="auto", **kw):  # what fhirmap.validate_report returns with tx down
        calls.append(mode)
        return {"mode": "offline", "requested_mode": mode, "fallback": "tx.fhir.org not reachable",
                "terminology_checked": False, "seconds": 1, "files": {}, "errors": [], "warnings": []}
    monkeypatch.setattr(core_mod.fhirmap, "validate_report", fake_report)
    monkeypatch.setattr("service.fm.fhirmap.validate_report", fake_report)
    svc._schedule_revalidation = lambda rid, **kw: scheduled.append(rid)
    with TestClient(app, base_url="http://svc.test") as c:
        req = send(c, REQ1)
        link = mailer.last_link()
        login(c, link, "Eva", "Brandl")
        upload(c, link, doc("lab-result-maria-beispiel.md"))
        st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed" and calls == ["auto"]
    v = st["filed"]["validation"]
    assert v["mode"] == "offline" and v["fallback"] and v["terminology_checked"] is False and v["revalidate_with_tx"]
    assert scheduled == [req["request_id"]]
    assert world.notifications[-1]["data"]["validation_mode"] == "offline"


def test_audit_pagination_returns_everything(app_client):
    c = app_client
    for _ in range(3):
        send(c, REQ1)
    total = c.svc.store.one("SELECT COUNT(*) n FROM audit")["n"]
    got, after = [], 0
    while True:
        page = c.get("/api/audit", params={"after_id": after, "limit": 2}).json()
        got += page["entries"]
        if page["next_after_id"] is None:
            break
        after = page["next_after_id"]
    assert [e["id"] for e in got] == list(range(1, total + 1))

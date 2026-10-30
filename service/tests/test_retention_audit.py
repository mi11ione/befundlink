"""Keyed + anchored audit chain, immutable audit_fhir, timer purge and data minimisation, display names."""
import re
import sqlite3
from datetime import timedelta

from service import audit as auditlib
from service.core import iso, now

from .conftest import REQ1, REQ2, doc
from .test_e2e import HDR, login, send, upload


def test_chain_is_keyed_and_anchored(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    svc = c.svc
    rows = svc.store.q("SELECT * FROM audit ORDER BY id")
    assert all(r["alg"] == auditlib.KEYED for r in rows)
    assert auditlib.verify_rows(rows, svc.k_audit)["ok"]
    assert not auditlib.verify_rows(rows, b"\0" * 32)["ok"]                     # without the key nothing verifies
    a = svc.anchor()
    assert a and a["head_id"] == rows[-1]["id"]
    assert any("[AUDIT-ANKER]" in n["text"] and a["head_hash"] in n["text"] for n in world.notifications)
    anchors = [e for e in world.audit_events() if e["subtype"][0]["code"] == "chain-head"]  # an AuditEvent, not a Basic
    assert anchors and anchors[-1]["entity"][0]["what"]["identifier"]["value"] == f"{a['head_id']}:{a['head_hash']}"
    assert not [k for k in world.resources if k.startswith("Basic/")]
    assert svc.anchor() is None                                               # unchanged head: no new anchor
    # rewriting history before the anchor is detected, even with the key (re-hash the whole chain)
    con = sqlite3.connect(svc.store.path)
    con.execute("DROP TRIGGER audit_no_update")
    prev = auditlib.GENESIS
    for r in rows:
        r = dict(r)
        if r["id"] == 1:
            r["what"] = "rewritten"
        r["prev_hash"] = prev
        r["hash"] = auditlib.entry_hash(r, svc.k_audit)
        con.execute("UPDATE audit SET what=?, prev_hash=?, hash=? WHERE id=?", (r["what"], r["prev_hash"], r["hash"], r["id"]))
        prev = r["hash"]
    con.commit()
    con.close()
    v = svc.verify_audit()
    assert not v["ok"] and any("anchor" in p for p in v["problems"])


def test_downgrade_to_unkeyed_is_detected(app_client):
    svc = app_client.svc
    send(app_client, REQ1)
    send(app_client, REQ2)
    rows = [dict(r) for r in svc.store.q("SELECT * FROM audit ORDER BY id")]
    assert len(rows) >= 2
    rows[-1]["alg"] = "sha256"
    rows[-1]["hash"] = auditlib.entry_hash(rows[-1])
    assert any("downgrade" in p for p in auditlib.verify_rows(rows, svc.k_audit)["problems"])


def test_audit_fhir_copy_is_immutable(app_client):
    send(app_client, REQ1)
    con = sqlite3.connect(app_client.svc.store.path)
    for sql in ("UPDATE audit_fhir SET event='{}' WHERE audit_id=1", "DELETE FROM audit_fhir WHERE audit_id=1"):
        try:
            con.execute(sql)
            raise AssertionError(f"{sql} should have been refused")
        except sqlite3.DatabaseError:
            pass
    con.execute("UPDATE audit_fhir SET error='x' WHERE audit_id=1")           # delivery state may change
    con.close()


def test_request_scoped_audit_is_a_full_check(app_client):
    req = send(app_client, REQ1)
    v = app_client.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()
    assert v["chain"]["mode"] == "full" and "vollständig" in v["chain"]["detail"]
    assert app_client.get("/api/audit").json()["chain"]["mode"] == "incremental"


def test_no_patient_pii_or_filename_in_rows(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"), "befund-maria-beispiel.md")
    con = sqlite3.connect(c.svc.store.path)
    dump = "\n".join(l for l in con.iterdump() if '"personas"' not in l)  # (the mock IdP's own identity records)
    con.close()
    assert "1961-03-12" not in dump and "Maria Beispiel" not in dump and "befund-maria-beispiel" not in dump
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"


def test_maintenance_purges_on_schedule(app_client, mailer):
    c = app_client
    svc = c.svc
    req = send(c, REQ2)
    login(c, re.search(r"http://svc\.test(/r/[a-z]{60})", svc.store.q("SELECT text FROM sms")[0]["text"]).group(1),
          "Stefan", "Kern")
    assert svc.store.q("SELECT * FROM sessions") and svc.store.q("SELECT * FROM used_assertions")
    past = iso(now() - timedelta(days=2))
    svc.store.exec("UPDATE sessions SET expires_at=?", (past,))
    svc.store.exec("UPDATE used_assertions SET at=?", (past,))
    svc.store.update_request(req["request_id"], expires_at=past)
    out = svc.maintenance()
    assert out["sessions"] >= 1 and out["assertions"] >= 1 and out["sms"] >= 1 and out["request_pii"] == 1
    r = svc.store.get_request(req["request_id"])
    assert r["address"] == "[gelöscht]" and not svc.store.q("SELECT * FROM sms") and not svc.store.q("SELECT * FROM sessions")


def test_requester_display_name_and_policy_uris(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    assert req["register"]["policy"].startswith("https://pgk-bridge.example/fhir/policy/")
    task = world.task_puts[-1]
    assert task["requester"]["display"] == "Dr. Lena Hofer"
    page = login(c, mailer.last_link(), "Eva", "Brandl")
    assert "Dr. Lena Hofer" in page.text and "@lena.hofer" not in page.text


def test_full_check_is_reused_until_a_foreign_write(app_client):
    c = app_client
    req = send(c, REQ1)
    svc = c.svc
    assert svc.verify_audit_full_cached()["full_checked"] == "now"
    send(c, REQ2)                                                    # our own appends: cached + incremental
    v = svc.verify_audit_full_cached()
    assert v["ok"] and v["full_checked"] == "cached"
    con = sqlite3.connect(svc.store.path)                            # a foreign process tampers
    con.execute("DROP TRIGGER audit_no_update")
    con.execute("UPDATE audit SET what='x' WHERE id=1")
    con.commit()
    con.close()
    v = svc.verify_audit_full_cached()
    assert v["full_checked"] == "now" and not v["ok"]
    assert not c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["chain"]["ok"]

"""Audit and privacy: external anchor read-back, pairwise id of strangers in FHIR, no clear contact in the
FHIR audit, request scoping, log redaction on handlers, Task reconcile coverage, file modes, content-bound
Provenance signature."""
import io
import json
import logging
import os
import re
import sqlite3
import stat
from datetime import timedelta

import httpx

from service import audit as auditlib
from service.app import install_redaction
from service.core import iso, now

from .conftest import REQ1, REQ2, doc
from .test_e2e import HDR, confirm, login, send, upload


def _rewrite_history(svc) -> None:
    """An insider with the DB and the key: rewrites entry #1, re-hashes the chain, and rewrites/empties the local
    anchor table as well (dropping its triggers)."""
    rows = svc.store.q("SELECT * FROM audit ORDER BY id")
    con = sqlite3.connect(svc.store.path)
    for t in ("audit_no_update", "anchors_no_delete"):
        con.execute(f"DROP TRIGGER {t}")
    prev = auditlib.GENESIS
    for r in rows:
        r = dict(r)
        if r["id"] == 1:
            r["what"] = "rewritten"
        r["prev_hash"] = prev
        r["hash"] = auditlib.entry_hash(r, svc.k_audit)
        con.execute("UPDATE audit SET what=?, prev_hash=?, hash=? WHERE id=?", (r["what"], r["prev_hash"], r["hash"], r["id"]))
        prev = r["hash"]
    con.execute("DELETE FROM audit_anchors")
    con.commit()
    con.close()


# ---------------------------------------------------------------- 1. anchors read back from HAPI and Matrix
def test_anchors_are_read_back_from_hapi_and_matrix(app_client, world):
    svc = app_client.svc
    send(app_client, REQ1)
    a = svc.anchor()
    assert svc.store.one("SELECT * FROM audit_anchors")["matrix_event"] == world.room[-1]["event_id"]
    v = svc.verify_audit(check_fhir=True)
    assert v["ok"], v["problems"]
    check = v["anchor_check"]
    assert check["local"]["ok"] == check["hapi"]["ok"] == check["matrix"]["ok"] == 1
    assert a["chain"] == check["chain"]
    out = app_client.get("/api/audit/verify", params={"fhir": True}).json()
    assert out["anchor_check"]["matrix"]["configured"] is True


def test_rewritten_history_with_emptied_anchor_table_is_caught_externally(app_client, world):
    svc = app_client.svc
    send(app_client, REQ1)
    svc.anchor()
    _rewrite_history(svc)
    v = svc.verify_audit()                                   # local only: nothing left to compare with
    assert v["ok"]
    v = svc.verify_audit(check_fhir=True)                    # HAPI and the room still have the old head
    assert not v["ok"]
    assert v["anchor_check"]["hapi"]["bad"] and v["anchor_check"]["matrix"]["bad"]
    assert any(p.startswith("hapi anchor") for p in v["problems"]) and any(p.startswith("matrix anchor") for p in v["problems"])


def test_anchor_table_is_append_only(app_client):
    svc = app_client.svc
    send(app_client, REQ1)
    svc.anchor()
    con = sqlite3.connect(svc.store.path)
    for sql in ("UPDATE audit_anchors SET head_hash='x'", "DELETE FROM audit_anchors",
                "UPDATE audit_anchors SET fhir_ref='AuditEvent/other'", "UPDATE audit_anchors SET matrix_event='$other'"):
        try:
            con.execute(sql)
            raise AssertionError(f"{sql} should have been refused")
        except sqlite3.DatabaseError:
            pass
    con.close()


def test_anchors_of_other_chains_and_other_senders_are_ignored(app_client, world):
    svc = app_client.svc
    send(app_client, REQ1)
    svc.anchor()
    head = svc.store.one("SELECT id FROM audit ORDER BY id DESC LIMIT 1")["id"]
    world.room.append({"event_id": "$fake", "sender": "@mallory:klinikum.example", "type": "m.room.message",
                       "content": {"body": f"[AUDIT-ANKER] Audit-Kette {svc.chain_id} bis Eintrag #{head}: {'0' * 64}"}})
    world.room.append({"event_id": "$other", "sender": "@bot:klinikum.example", "type": "m.room.message",
                       "content": {"body": f"[AUDIT-ANKER] Audit-Kette {'f' * 16} bis Eintrag #{head}: {'0' * 64}"}})
    assert svc.verify_audit(check_fhir=True)["ok"]


def test_unauthenticated_anchors_are_ignored_not_counted_as_tampering(app_client, world):
    from service import anchors
    svc = app_client.svc
    send(app_client, REQ1)
    svc.anchor()
    head = svc.store.one("SELECT id FROM audit ORDER BY id DESC LIMIT 1")["id"]
    fake = anchors.anchor_event(svc.chain_id, head, "0" * 64, iso(now()), mac="f" * 32)     # wrong MAC
    world.resources["AuditEvent/fake"] = {**fake, "id": "fake"}
    world.room.append({"event_id": "$nomac", "sender": "@bot:klinikum.example", "type": "m.room.message",
                       "content": {"body": f"[AUDIT-ANKER] Audit-Kette {svc.chain_id} bis Eintrag #{head}: {'0' * 64}"}})
    v = svc.verify_audit(check_fhir=True)
    assert v["ok"], v["problems"]
    assert v["anchor_check"]["hapi"]["ignored_unauthenticated"] == 1 and v["anchor_check"]["hapi"]["ok"] == 1
    assert v["anchor_check"]["matrix"]["ignored_unauthenticated"] == 1 and v["anchor_check"]["matrix"]["ok"] == 1


def test_last_resort_handler_is_redacted_too():
    install_redaction()
    assert any(type(f).__name__ == "RedactTokens" for f in logging.lastResort.filters)


def test_unreadable_matrix_is_reported_not_silently_skipped(app_client, world):
    svc = app_client.svc
    send(app_client, REQ1)
    svc.anchor()
    world.matrix = lambda req: httpx.Response(500)
    v = svc.verify_audit(check_fhir=True)
    assert not v["ok"] and "error" in v["anchor_check"]["matrix"]


# ---------------------------------------------------------------- 2. strangers carry the pairwise id in FHIR
def test_stranger_agent_carries_the_pairwise_id(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    assert login(c, mailer.last_link(), "Petra", "Fremd").status_code == 403
    sub = c.svc.store.q("SELECT detail FROM audit WHERE step='misdelivery caught'")[0]["detail"]["bpk_hash"]
    evs = [e for e in world.audit_events() if e["subtype"][0]["code"] in ("misdelivery-caught", "identity-checked")]
    assert evs
    for e in evs:
        who = e["agent"][0]["who"]
        assert who["identifier"]["value"] == sub and who["identifier"]["system"].endswith("/sid/bpk-hmac-sha256")
        assert "Petra" not in json.dumps(e) and "Fremd" not in json.dumps(e)


# ---------------------------------------------------------------- 3. contact data
def test_request_sent_auditevent_has_no_clear_contact(app_client, world):
    c = app_client
    send(c, REQ1)
    send(c, REQ2)
    c.svc.outbox.drain(5)                                    # AuditEvents go out through the async outbox
    sent = [e for e in world.audit_events() if e["subtype"][0]["code"] == "request-sent"]
    assert len(sent) == 2
    blob = json.dumps(sent)
    assert "eva.brandl" not in blob and "labor-kanal" not in blob and "660" not in blob
    assert "per E-Mail an Kontakt #" in blob and "per SMS an Kontakt #" in blob
    row = c.svc.store.q("SELECT * FROM audit WHERE step='request sent' ORDER BY id")[0]
    assert "eva.brandl" not in row["what"] and "address" not in row["detail"]
    assert row["detail"]["contact_hash"] == c.svc.contact_hash("email", " Eva.Brandl@labor-kanal.example")


def test_contact_beside_the_chain_is_redacted_at_link_expiry(app_client):
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    entries = c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]
    sent = next(e for e in entries if e["step"] == "request sent")
    assert sent["contact"] == REQ1["address"]                # Dr. Hofer still sees it while the link lives
    svc.store.update_request(req["request_id"], expires_at=iso(now() - timedelta(days=1)))
    out = svc.maintenance()
    assert out["audit_contacts"] == 1
    entries = c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]
    sent = next(e for e in entries if e["step"] == "request sent")
    assert sent["contact"] == "[gelöscht]" and sent["detail"]["contact_hash"]
    con = sqlite3.connect(svc.store.path)
    dump = "\n".join(con.iterdump())
    con.close()
    assert REQ1["address"] not in dump
    assert svc.verify_audit()["ok"]                          # the chain is untouched by the redaction


def test_a_swapped_private_contact_is_detected(app_client):
    svc = app_client.svc
    send(app_client, REQ1)
    con = sqlite3.connect(svc.store.path)
    con.execute("DROP TRIGGER audit_private_no_update")
    con.execute("UPDATE audit_private SET contact='someone@else.example'")
    con.commit()
    con.close()
    assert any("contact hash" in p for p in svc.verify_audit()["problems"])


def test_request_text_is_redacted_after_filing_and_at_expiry(app_client, mailer):
    c, svc = app_client, app_client.svc
    req = send(c, {**REQ1, "request_text": "Gerinnung vor geplantem Eingriff"})
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    assert svc.store.get_request(req["request_id"])["status"] == "filed"
    assert svc.store.get_request(req["request_id"])["request_text"] is None
    req2 = send(c, {**REQ2, "request_text": "aktueller Entlassungsbericht"})
    svc.store.update_request(req2["request_id"], expires_at=iso(now() - timedelta(days=1)))
    assert svc.maintenance()["request_text"] == 1
    assert svc.store.get_request(req2["request_id"])["request_text"] is None


# ---------------------------------------------------------------- 4. request scoping
def test_reads_of_request_data_require_requester_and_room(app_client):
    c = app_client
    req = send(c, REQ1)
    rid = req["request_id"]
    other = {**HDR, "X-PGK-Actor": "@mallory:klinikum.example"}
    for path, params in ((f"/api/requests/{rid}", None), ("/api/audit", {"request_id": rid}), ("/api/requests", None)):
        assert c.get(path, params=params).status_code == 403, path                        # no headers
        assert c.get(path, params=params, headers={"X-PGK-Actor": HDR["X-PGK-Actor"]}).status_code == 403, path
        assert c.get(path, params=params, headers=HDR).status_code == 200, path
    assert c.get(f"/api/requests/{rid}", headers=other).status_code == 403
    assert c.get("/api/audit", params={"request_id": rid}, headers=other).status_code == 403
    assert c.get("/api/requests", headers=other).json() == []                             # only one's own
    assert [r["request_id"] for r in c.get("/api/requests", headers=HDR).json()] == [rid]
    assert c.get("/api/requests/req-000000000000").status_code == 403                     # no existence oracle
    for action in ("confirm", "cancel"):
        assert c.post(f"/api/requests/{rid}/{action}").status_code == 403
    assert c.get("/api/audit").status_code == 200                                         # operator view: key only
    assert c.get("/api/audit", headers=HDR).status_code == 403


# ---------------------------------------------------------------- 5. log redaction reaches child loggers
def test_tokens_are_redacted_in_child_logger_records():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_redaction()
        token = "b" * 60
        child = logging.getLogger("pgk.core.test")
        child.propagate = True
        child.warning("opened /r/%s with assertion=%s", token, "eyJa.eyJb.sig")
        logging.getLogger("some.library").warning("GET /r/%s", token)
        out = stream.getvalue()
        assert token not in out and "eyJa.eyJb.sig" not in out and out.count("/r/<token>") == 2
    finally:
        root.removeHandler(handler)


# ---------------------------------------------------------------- 6. Task reconcile covers every unsettled request
def test_reconcile_covers_all_unsettled_requests_and_settles_final_ones(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    reqs = [send(c, {**REQ1, "address": f"eva.brandl{i}@labor-kanal.example", "confirm_warnings": True}) for i in range(3)]
    svc.store.exec("UPDATE requests SET created_at=?", (iso(now() - timedelta(days=400)),))  # 'old' rows
    for r in reqs:
        world.resources.pop(f"Task/{svc.store.get_request(r['request_id'])['task_id']}", None)
    out = svc.reconcile_tasks()
    assert out["created"] == 3 and out["checked"] == 3
    c.post(f"/api/requests/{reqs[0]['request_id']}/cancel", headers=HDR)
    svc.outbox.drain(5)
    out = svc.reconcile_tasks()
    assert out["confirmed"] == 1                                   # cancelled + Task cancelled on HAPI: settled
    assert svc.reconcile_tasks()["checked"] == 2                   # ... and not read again


def test_reconcile_repairs_a_lost_status_update(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    tid = f"Task/{svc.store.get_request(req['request_id'])['task_id']}"
    c.post(f"/api/requests/{req['request_id']}/cancel", headers=HDR)
    world.resources[tid] = {**world.resources[tid], "status": "requested"}       # the cancel never reached HAPI
    assert svc.reconcile_tasks()["updated"] == 1
    assert world.resources[tid]["status"] == "cancelled"


# ---------------------------------------------------------------- 7. file modes
def test_database_and_keys_are_private(app_client, tmp_path):
    svc = app_client.svc
    send(app_client, REQ1)
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
    assert mode(tmp_path) == 0o700
    for f in ("t.sqlite", "t.sqlite-wal", "t.sqlite-shm", "service_es256.pem", "idp_es256.pem"):
        assert mode(tmp_path / f) == 0o600, f
    assert svc.store.path == str(tmp_path / "t.sqlite")


def test_generated_data_key_is_private(tmp_path):
    from service.crypto import Box
    Box(None, tmp_path / "keys" / ".datakey")
    assert stat.S_IMODE(os.stat(tmp_path / "keys" / ".datakey").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "keys").st_mode) == 0o700


# ---------------------------------------------------------------- 9. the signature covers the filed clinical content
def test_filed_signature_is_verified_against_the_read_back_content(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    filed = svc.store.get_request(req["request_id"])["filed"]
    sig = filed["signature"]
    assert sig["verified"] and sig["bound"] and sig["content_checked"] and sig["changed"] == [], sig
    prov = world.resources[next(x for x in filed["resources"] if x.startswith("Provenance/"))]
    assert any(t["reference"].startswith("Task/") for t in prov["target"])         # the Task is a target too
    # an Observation value edited on the server afterwards is named
    obs_ref = next(x for x in filed["resources"] if x.startswith("Observation/"))
    world.resources[obs_ref]["valueQuantity"]["value"] = 99.9
    id_map = {}
    for tx in world.transactions:
        for e, ref in zip(tx["entry"], filed["resources"]):
            id_map[e["fullUrl"]] = ref
    again = svc._verify_filed_signature(filed["resources"], id_map)
    assert not again["verified"] and obs_ref in again["changed"]


# ---------------------------------------------------------------- outbound PII guard (metrics A06/A12/A28)
WEBER = {**REQ1, "recipient": {"name": "Dr. Anna Weber", "role": "Fachärztin für Labormedizin", "org": "Labor Kanalstraße"},
         "address": "anna.weber@labor-kanal.example"}


def _send_weber(c):
    """Create the request (a new recipient: held for confirmation) and confirm it as Dr. Hofer would."""
    created = c.post("/api/requests", json=WEBER, headers=HDR)
    assert created.status_code == 201, created.text
    return confirm(c, created.json())


def test_role_with_function_words_is_sent(app_client, mailer):
    from service import messages
    r = _send_weber(app_client)
    assert r.status_code == 200 and mailer.sent, r.text
    mail = mailer.sent[-1]
    text = (mail["subject"] + "\n" + mail["body"]).replace(app_client.svc.s.base_url, "<LINK>").replace("svc.test", "<HOST>")
    assert "für Sie bestimmt" in text                                   # the template's own 'für' is fine
    assert not messages.pii_findings(text, "Maria Beispiel", "1961-03-12")  # no doc-type word, name or DOB
    words = set(re.findall(r"\w+", text))
    for leak in ("Labor", "Kanalstraße", "Labormedizin", "Fachärztin", "Maria", "Beispiel", "1961"):
        assert leak not in words, leak


def test_org_name_in_a_message_is_still_refused(app_client, monkeypatch):
    from service import messages
    real = messages.build_email
    monkeypatch.setattr(messages, "build_email", lambda *a, **k: (real(*a, **k)[0], real(*a, **k)[1] + "\nKanalstraße"))
    r = _send_weber(app_client)
    assert r.status_code == 422 and "Kanalstraße" in r.text


def test_org_name_in_the_html_part_is_still_refused(app_client, mailer, monkeypatch):
    from service import messages
    real = messages.build_email_html
    monkeypatch.setattr(messages, "build_email_html", lambda *a, **k: real(*a, **k).replace("</body>", "<p>Kanalstraße</p></body>"))
    r = _send_weber(app_client)
    assert r.status_code == 422 and "Kanalstraße" in r.text and not mailer.sent


def test_email_has_plain_and_html_parts(app_client, mailer):
    r = _send_weber(app_client)
    assert r.status_code == 200 and mailer.sent
    mail = mailer.sent[-1]
    assert mail["body"] and mail["html"] and "Kanalstraße" not in mail["html"] and "Labor" not in mail["html"]


def test_guard_word_lists_are_consistent():
    from service import messages
    assert not set(messages.DOC_TYPE_WORDS) & messages.TEMPLATE_WORDS    # the template never trips the lexicon
    words = messages.context_words(["Fachärztin für Labormedizin", "Labor Kanalstraße GmbH", "Head of the Lab"],
                                   allowed={"Klinikum"})
    assert words == ["Fachärztin", "Labormedizin", "Labor", "Kanalstraße", "Head"]


# ---------------------------------------------------------------- request creation is scoped
def test_request_creation_needs_a_directory_member_in_the_room(app_client, mailer):
    c = app_client
    for body in (REQ1, {**REQ1, "dry_run": True}):
        assert c.post("/api/requests", json=body).status_code == 403                       # API key alone
        stranger = {"X-PGK-Actor": "@mallory:klinikum.example", "X-PGK-Room": HDR["X-PGK-Room"]}
        assert c.post("/api/requests", json={**body, "requester_mxid": stranger["X-PGK-Actor"]},
                      headers=stranger).status_code == 403                                 # not in the directory
        assert c.post("/api/requests", json=body, headers={**HDR, "X-PGK-Room": "!other:x"}).status_code == 403
    dry = c.post("/api/requests", json={**REQ1, "dry_run": True}, headers=HDR)
    assert dry.status_code == 201 and dry.json()["patient"]
    assert not mailer.sent


# ---------------------------------------------------------------- one hash for the upload
def test_windows_1252_upload_has_one_hash_everywhere(app_client, mailer, world):
    import hashlib
    from fhirmap.build import _source_sha256
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    raw = doc("lab-result-maria-beispiel.md").replace("Labor Kanalstraße", "Labor Kanalstraße – µ").encode("cp1252")
    page = c.get(f"{link}/answer")
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    assert c.post(f"{link}/answer", data={"csrf": csrf}, files={"file": ("x.md", raw, "text/markdown")}).status_code == 200
    want = hashlib.sha256(raw).hexdigest()
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed", st["last_reason"]
    prov = world.resources[next(x for x in st["filed"]["resources"] if x.startswith("Provenance/"))]
    assert _source_sha256(prov) == want                                         # Provenance source entity
    sent = next(e for e in c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]
                if e["step"] == "answer sent")
    assert sent["detail"]["sha256"] == want                                     # audit
    assert c.svc.store.one("SELECT sha256 FROM answers WHERE request_id=?", (req["request_id"],))["sha256"] == want
    filed = next(n for n in world.notifications if n.get("kind") == "filed")
    assert filed["data"]["charset_note"] == "Zeichensatz umgewandelt (Windows-1252)"


# ---------------------------------------------------------------- re-verifiable signatures
def test_filings_are_reverifiable_and_a_later_edit_is_reported(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    filed = svc.store.get_request(req["request_id"])["filed"]
    assert filed["id_map"] and any(t.startswith("Task/") for t in filed["signed_targets"])
    res = svc.reverify_all()
    assert res["checked"] == 1 and res["verified"] == 1 and not res["failed"]
    obs = next(x for x in filed["resources"] if x.startswith("Observation/") and "INR" in json.dumps(world.resources[x]))
    edited = json.loads(json.dumps(world.resources[obs]))
    edited["valueQuantity"]["value"] = 2.9
    world.put(obs, edited)                                                    # someone PUTs a new version on HAPI
    res = svc.reverify_all()
    assert res["failed"] and obs in res["failed"][0]["changed"]
    alerts = [n for n in world.notifications if "[ACHTUNG]" in n["text"]]
    assert len(alerts) == 1
    text = alerts[0]["text"]                                                  # readable for a clinician
    assert text.startswith("[ACHTUNG] Maria Beispiel (12.03.1961) – INR")
    assert "signiert 1.0 INR, am FHIR-Server jetzt 2.9 INR" in text and req["ref_code"] in text
    assert text.endswith("Nicht verwenden, bis geprüft.") and alerts[0]["data"]["changed"] == [obs]
    failed = next(e for e in c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]
                  if e["step"] == "provenance check failed")
    assert obs in failed["what"] and "Maria" not in failed["what"] and "1961" not in failed["what"]
    assert "2.9" not in failed["what"]                                        # no patient data, no values in the audit
    svc.reverify_all()
    assert len([n for n in world.notifications if "[ACHTUNG]" in n["text"]]) == 1   # reported once


def test_verify_provenance_cli(app_client, mailer, monkeypatch, capsys):
    from service import verify_provenance
    monkeypatch.setattr(verify_provenance, "Service", lambda settings: app_client.svc)
    send(app_client, REQ1)
    link = mailer.last_link()
    login(app_client, link, "Eva", "Brandl")
    upload(app_client, link, doc("lab-result-maria-beispiel.md"))
    assert verify_provenance.main(["--offline"]) == 0 and "PROVENANCE VERIFY OK" in capsys.readouterr().out


def test_tamper_demo_alerts_the_room_through_the_running_service(app_client, mailer, world, capsys):
    """README tamper demo: edit a value on HAPI, run `make verify-provenance` -> the RUNNING service checks and Dr.
    Hofer's room gets the [ACHTUNG] alert (once per filing, naming the resource)."""
    from service import verify_provenance
    from .conftest import ROOM
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    filed = c.svc.store.get_request(req["request_id"])["filed"]
    obs = next(x for x in filed["resources"] if x.startswith("Observation/"))
    edited = json.loads(json.dumps(world.resources[obs]))
    edited["valueQuantity"]["value"] = 4.2
    world.put(obs, edited)
    assert c.post("/api/maintenance/reverify", headers=HDR).status_code == 403            # operators only
    assert verify_provenance.main([], http=c) == 1                                       # via the running service
    out = capsys.readouterr().out
    assert "checked by the running service" in out and "PROVENANCE VERIFY FAILED" in out and obs in out
    alerts = [n for n in world.notifications if "[ACHTUNG]" in n["text"]]
    assert len(alerts) == 1 and "jetzt 4.2" in alerts[0]["text"] and alerts[0]["room_id"] == ROOM
    summary = c.post("/api/maintenance/reverify").json()
    assert summary["failed"] and len([n for n in world.notifications if "[ACHTUNG]" in n["text"]]) == 1   # once


def test_anchors_go_to_the_audit_room_not_the_clinical_room(app_client, world):
    from .conftest import AUDIT_ROOM, ROOM
    svc = app_client.svc
    send(app_client, REQ1)
    svc.anchor()
    anchors = [n for n in world.notifications if "[AUDIT-ANKER]" in n["text"]]
    assert anchors and all(n["room_id"] == AUDIT_ROOM for n in anchors)
    assert not [n for n in world.notifications if n.get("room_id") == ROOM and "[AUDIT-ANKER]" in n["text"]]
    v = svc.verify_audit(check_fhir=True)
    assert v["ok"] and v["anchor_check"]["room"] == AUDIT_ROOM and v["anchor_check"]["matrix"]["ok"] == 1


def test_describe_change_for_reports_and_observations():
    from service.core import describe_change
    obs = {"resourceType": "Observation", "code": {"text": "Kalium"}, "status": "final",
           "valueQuantity": {"value": 4.1, "unit": "mmol/L"}}
    assert describe_change(obs, {**obs, "valueQuantity": {"value": 6.1, "unit": "mmol/L", "comparator": ">"}}) == \
        "Kalium: signiert 4.1 mmol/L, am FHIR-Server jetzt > 6.1 mmol/L"
    dr = {"resourceType": "DiagnosticReport", "status": "final", "conclusion": "unauffällig",
          "presentedForm": [{"data": "QQ=="}]}
    assert describe_change(dr, {**dr, "status": "amended", "conclusion": "auffällig", "presentedForm": [{"data": "Qg=="}]}) == \
        "Befund: Status signiert final, jetzt amended, Beurteilung/Kommentar geändert, Anhang (Originaldokument) geändert"


# ---------------------------------------------------------------- the requester's FHIR identity
def test_task_and_auditevent_reference_dr_hofer_and_her_clinic(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    send(c, REQ1)
    task = world.task_puts[-1]
    assert task["requester"]["reference"] == "Practitioner/lena-hofer"
    assert task["requester"]["identifier"]["value"] == "@lena.hofer:klinikum.example"
    assert {x["reference"] for x in task["restriction"]["recipient"]} == {"Practitioner/lena-hofer",
                                                                       "Organization/klinikum-beispielstadt"}
    sent = next(e for e in world.audit_events() if e["subtype"][0]["code"] == "request-sent")
    assert sent["agent"][0]["who"]["reference"] == "Practitioner/lena-hofer"
    assert world.resources["Practitioner/lena-hofer"]["name"][0]["family"] == "Hofer"         # ensured at startup
    assert world.resources["Organization/klinikum-beispielstadt"]["name"] == "Klinikum Beispielstadt"


def test_infra_directory_matches_what_the_service_builds(app_client):
    from pathlib import Path
    infra = json.loads((Path(__file__).resolve().parents[2] / "infra" / "directory.json").read_text())["resources"]
    assert infra == app_client.svc.directory_resources()


def test_patient_as_recipient_owns_the_task(app_client, mailer, world):
    c = app_client
    body = {**REQ1, "recipient": {"name": "Maria Beispiel", "role": "Patientin", "org": "privat"},
            "address": "maria@privat.example", "doc_kind": "discharge_report"}
    created = c.post("/api/requests", json=body, headers=HDR).json()
    if created["status"] == "needs_confirmation":
        confirm(c, created)
    task = world.task_puts[-1]
    assert task["owner"] == {"reference": "Patient/maria-beispiel", "display": "Maria Beispiel"}

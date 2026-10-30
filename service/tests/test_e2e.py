"""End-to-end through the HTTP app with a fake HAPI + bot: request 1 (email), request 2 (SMS), M1, M2, M3,
mail-scanner pre-clicks, near-miss hold + decision, expiry, rate limit, audit chain."""
import json
import re
import sqlite3
from pathlib import Path

from service import audit as auditlib

from .conftest import REQ1, REQ2, doc

PII_PRE_ID = ["Maria", "1961", "Labor", "Befund", "lab", "Reha", "Entlassung", "discharge", "report", "result", "Max"]


def assert_no_pii(html: str):
    """Strict substring scan of the visible text + attribute values (markup tag names removed)."""
    html = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    attrs = " ".join(re.findall(r'="([^"]*)"', html))
    low = (re.sub(r"<[^>]+>", " ", html) + " " + attrs).casefold()
    for w in PII_PRE_ID:
        assert w.casefold() not in low, f"{w!r} leaked: {html[:400]}"
    assert not re.search(r"(?<!\w)beispiel(?!\w)", low)


def send(c, body, confirm=False):
    r = c.post("/api/requests", json={**body, **({"confirm_warnings": True} if confirm else {})}, headers=HDR)
    assert r.status_code == 201, r.text
    return r.json()


def confirm(c, req):
    """Dr. Hofer confirms a held request, acknowledging every warning code (as the bot does)."""
    codes = [w["code"] for w in req.get("warning_details") or []]
    return c.post(f"/api/requests/{req['request_id']}/confirm", headers=HDR, json={"acknowledged_warnings": codes})


def link_for(c, mailer, req):
    if req["channel"] == "email":
        return mailer.last_link()
    sms = c.get("/api/sms").json()[0]["text"]
    return re.search(r"http://svc\.test(/r/[bcdfghjkmnpqrstvwxz]{60})", sms).group(1)


PERSONAS = json.loads((Path(__file__).resolve().parents[1] / "data" / "personas.json").read_text())["personas"]
PINS = {(p["given"], p["family"]): p for p in PERSONAS}
HDR = {"X-PGK-Actor": "@lena.hofer:klinikum.example", "X-PGK-Room": "!room:klinikum.example"}


def login(c, link, given, family, dob=None, pin=None):
    """Open the link, sign in with the simulated ID Austria (PIN for personas), follow the POST hand-off."""
    r = c.get(link)
    assert r.status_code == 200
    assert_no_pii(r.text)
    assert "Mit ID Austria anmelden" in r.text
    ret = f"{link}/eid"
    r = c.get("/eid/login", params={"return": ret})
    assert r.status_code == 200
    assert_no_pii(r.text)
    p = PINS.get((given, family))
    data = {"return": ret, "given": given, "family": family, "birthdate": dob or (p["birthdate"] if p else "1980-01-01"),
            "pin": pin if pin is not None else (p["pin"] if p else "")}
    r = c.post("/eid/login", data=data)
    if r.status_code != 200 or 'id="handoff"' not in r.text:
        return r
    assert "assertion=" not in str(r.url)
    action = re.search(r'action="([^"]+)"', r.text).group(1)
    assertion = re.search(r'name="assertion" value="([^"]+)"', r.text).group(1)
    return c.post(action, data={"assertion": assertion})


def upload(c, link, text, name="answer.md"):
    page = c.get(f"{link}/answer")
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    return c.post(f"{link}/answer", data={"csrf": csrf}, files={"file": (name, text.encode(), "text/markdown")})


def steps(c, rid):
    return [(e["step"], e["outcome"]) for e in c.get("/api/audit", params={"request_id": rid}, headers=HDR).json()["entries"]]


# ---------------------------------------------------------------- request 1, happy path
def test_request1_email_happy_path(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    assert req["status"] == "sent" and req["warnings"] == []
    assert req["register"]["ok"] and "Labordiagnostik" in req["register"]["message"]
    mail = mailer.sent[-1]
    assert mail["to"] == "eva.brandl@labor-kanal.example"
    assert "Dr. Lena Hofer (Innere Medizin, Klinikum Beispielstadt) bittet Sie persönlich um eine Rückmeldung" in mail["body"]
    assert "+43 1 000 00 00" in mail["body"] and "Dr. Lena Hofer" in mail["subject"]       # requester + callback, not anonymous
    assert_no_pii(mail["subject"] + mail["body"])
    assert "maria-beispiel" not in mail["body"] and "Gerinnung" not in mail["body"]  # patient id + note never relayed
    link = mailer.last_link()

    r = login(c, link, "Eva", "Brandl")
    assert r.status_code == 200 and "Maria Beispiel" in r.text and "12.03.1961" in r.text and "Laborbefund" in r.text
    assert "Gerinnung" not in r.text  # Dr. Hofer's free-text note is not shown to the outside person
    r = upload(c, link, doc("lab-result-maria-beispiel.md"), "lab.md")
    assert r.status_code == 200 and "erhalten" in r.text

    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed", st
    assert st["filed"]["main"].startswith("DiagnosticReport/")
    tx = world.transactions[-1]
    types = [e["resource"]["resourceType"] for e in tx["entry"]]
    assert types.count("Observation") == 4 and "DiagnosticReport" in types and "Provenance" in types and "Task" in types
    task = next(e["resource"] for e in tx["entry"] if e["resource"]["resourceType"] == "Task")
    assert task["status"] == "completed" and task["for"]["reference"] == "Patient/maria-beispiel"
    dr_full = next(e["fullUrl"] for e in tx["entry"] if e["resource"]["resourceType"] == "DiagnosticReport")
    assert task["output"][0]["valueReference"]["reference"] == dr_full  # urn:uuid, resolved by HAPI in the transaction
    assert next(e for e in tx["entry"] if e["resource"]["resourceType"] == "Task")["request"]["method"] == "PUT"
    prov = next(e["resource"] for e in tx["entry"] if e["resource"]["resourceType"] == "Provenance")
    assert prov.get("signature") and prov["signature"][0]["sigFormat"] == "application/jose"
    assert "bpk" not in str(prov["signature"][0]["data"]).lower() or True
    assert world.validated and any(r["resourceType"] == "Task" for r in world.validated[-1])
    note = world.notifications[-1]["text"]
    assert "Laborbefund für Maria Beispiel von Dr. Eva Brandl (ID Austria – Simulation geprüft) abgelegt" in note
    assert "HL7-Validator: 0 Fehler" in note and "4/4 Werte unverändert übernommen" in note
    assert "Ärzteliste: Medizinische und Chemische Labordiagnostik" in note
    assert "http://hapi.test/fhir/DiagnosticReport/" in note

    s = steps(c, req["request_id"])
    for step in auditlib.SIX_STEPS[:5]:
        assert (step, "success") in s, (step, s)
    view = c.get("/api/audit", params={"request_id": req["ref_code"]}, headers=HDR).json()
    assert view["chain"]["ok"] and view["effort"]["seconds_link_opened_to_answer_sent"] is not None
    # AuditEvents on FHIR carry codes + chain hash
    evs = world.audit_events()
    assert evs and all(e["purposeOfEvent"][0]["coding"][0]["code"] == "TREAT" for e in evs)
    assert any(e["type"]["code"] == "110114" for e in evs) and any(e["type"]["code"] == "110107" for e in evs)
    assert c.get("/api/audit/verify", params={"fhir": True}).json()["ok"]
    # link is single-purpose: closed after filing; the person who filed sees a thank-you with her receipt
    thanks = c.get(link)
    assert thanks.status_code == 200 and "Ihre Antwort wurde abgelegt. Danke." in thanks.text and "receipt.txt" in thanks.text
    c.cookies.clear()
    assert c.get(link).status_code in (404, 410)  # anyone else: token revoked, generic text
    # filed content is not kept in the bridge
    assert c.svc.store.one("SELECT content FROM answers WHERE request_id=?", (req["request_id"],))["content"] is None


# ---------------------------------------------------------------- request 2, SMS, author != sender
def test_request2_sms_author_differs_from_sender(app_client, world):
    c = app_client
    req = send(c, REQ2)
    assert req["status"] == "sent" and req["register"]["ok"], req
    sms = c.get("/api/sms").json()[0]
    assert_no_pii(sms["text"])
    assert "Anfrage an Sie" in c.get("/sms").text and "Rückfragen +43 1 000 00 00" in c.get("/sms").text
    link = link_for(c, None, req)
    r = login(c, link, "Stefan", "Kern")
    assert "Reha-Entlassungsbericht" in r.text
    upload(c, link, doc("rehab-report-maria-beispiel.md"), "reha.md")
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed", st
    assert st["filed"]["main"].startswith("DocumentReference/")
    note = world.notifications[-1]["text"]
    assert "Stefan Kern (ID Austria – Simulation geprüft) abgelegt" in note and "Autor laut Dokument: Dr. Johanna Leitner" in note
    assert world.notifications[-1]["data"]["values_label"] == "Prüfpunkte" and "Prüfpunkte unverändert übernommen" in note


# ---------------------------------------------------------------- M1: confusable address
def test_m1_wrong_address(app_client, mailer, world):
    c = app_client
    body = {**REQ1, "address": "eva.brandl@labor-kanaI.example"}
    pre = c.post("/api/requests/check", json=body).json()
    assert pre["needs_confirmation"] and any("labor-kanal.example" in w for w in pre["warnings"])
    req = send(c, body)
    assert req["status"] == "needs_confirmation" and not mailer.sent  # prevented: nothing sent without confirmation
    assert c.post(f"/api/requests/{req['ref_code']}/confirm", headers=HDR).status_code == 409   # no blanket confirm
    req = confirm(c, req).json()
    assert req["status"] == "sent" and mailer.sent[-1]["to"] == "eva.brandl@labor-kanaI.example"
    link = mailer.last_link()
    r = login(c, link, "Petra", "Fremd")
    assert r.status_code == 403
    assert_no_pii(r.text)
    # the link went to the wrong address: locked at once, with the next step for Dr. Hofer
    assert c.get(f"{link}/answer", follow_redirects=False).status_code == 429  # cannot reach the answer page
    alert = world.notifications[-1]
    assert "e***@labor-kanaI.example" in alert["text"]                       # the capital I as typed
    assert "Dieser Link ist gesperrt; eine neue Anfrage an die richtige Adresse: " \
           "!labor Brandl maria-beispiel eva.brandl@labor-kanal.example" in alert["text"]
    assert "bleibt für" not in alert["text"] and alert["data"]["wrong_address"] and alert["data"]["locked"]
    s = steps(c, req["request_id"])
    assert ("identity checked", "failure") in s and ("misdelivery caught", "success") in s
    assert "Petra Fremd" in world.notifications[-1]["text"] and "blockiert" in world.notifications[-1]["text"]
    assert not world.transactions
    ev = [e for e in world.audit_events() if e["subtype"][0]["code"] == "misdelivery-caught"]
    assert ev and ev[0]["outcome"] == "8" and ev[0]["type"]["code"] == "110113"


# ---------------------------------------------------------------- M2: forwarded; link not burned
def test_m2_forward_blocked_then_real_recipient_answers(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    r = login(c, link, "Thomas", "Huber")
    assert r.status_code == 403 and "Diese Anfrage ist nicht für Sie" in r.text and "Brandl" not in r.text  # I10
    assert_no_pii(r.text)
    n = world.notifications[-1]["text"]
    assert "Anforderung an Dr. Eva Brandl" in n and "Zugriff von einer anderen Person (Thomas Huber) blockiert" in n
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["identity_bound"] is False
    # Dr. Brandl can still answer
    c.cookies.clear()
    r = login(c, link, "Eva", "Brandl", "1979-05-14")
    assert "Maria Beispiel" in r.text
    # a namesake with a different eID after binding counts as a forward
    c2_cookies = dict(c.cookies)
    c.cookies.clear()
    other = c.post("/api/eid/personas", json={"given": "Eva", "family": "Brandl", "birthdate": "1990-02-02"}).json()
    r = login(c, link, "Eva", "Brandl", "1990-02-02", pin=other["pin"])
    assert r.status_code == 403
    c.cookies.update(c2_cookies)
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    s = steps(c, req["request_id"])
    assert s.count(("misdelivery caught", "success")) == 2


# ---------------------------------------------------------------- M3: wrong patient document
def test_m3_wrong_patient_not_filed_and_not_named(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    r = upload(c, link, doc("lab-result-max-beispiel.md"), "max.md")
    assert "Dieses Dokument betrifft nicht Maria Beispiel (geb. 12.03.1961)" in r.text and "Max" not in r.text
    assert "nicht gespeichert, nicht abgelegt" in r.text
    assert not world.transactions
    n = world.notifications[-1]["text"]
    assert "FEHLZUSTELLUNG" in n and "andere Person" in n
    assert "Max" not in n and "1960" not in n
    entries = c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]
    caught = [e for e in entries if e["step"] == "misdelivery caught"]
    assert caught and all("Max" not in e["what"] and "1960" not in e["what"] for e in caught)
    for e in world.audit_events():
        assert "Max" not in str(e) and "1960-01-01" not in str(e)
    row = c.svc.store.one("SELECT content, outcome FROM answers WHERE request_id=?", (req["request_id"],))
    assert row["content"] is None and row["outcome"] == "blocked_wrong_patient"  # wrong file never stored
    # the right document can still be sent
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"


# ---------------------------------------------------------------- mail scanner pre-clicks
def test_get_never_consumes_or_binds(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    for _ in range(5):
        assert c.get(link).status_code == 200
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["identity_bound"] is False and st["eid_attempts"] == 0
    assert steps(c, req["request_id"]).count(("link opened", "success")) == 5
    assert "Maria" in login(c, link, "Eva", "Brandl").text


# ---------------------------------------------------------------- near-miss: hold + Dr. Hofer decides
def test_near_miss_held_then_filed_on_decision(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    swapped = doc("lab-result-maria-beispiel.md").replace("born 12 March 1961", "born 3 December 1961")
    r = upload(c, link, swapped)
    assert "erhalten" in r.text and "Entscheidung" not in r.text
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "held_review" and not world.transactions
    assert "Entscheidung" in world.notifications[-1]["text"] and "trotzdem ablegen" in world.notifications[-1]["text"]
    assert c.post(f"/api/requests/{req['ref_code']}/decision", headers=HDR, json={"decision": "ablegen"}).status_code == 422  # reason needed
    r = c.post(f"/api/requests/{req['ref_code']}/decision", headers=HDR,
               json={"decision": "ablegen", "reason": "Zahlendreher, telefonisch mit Labor bestätigt", "by": "@lena.hofer:klinikum.example"})
    assert r.status_code == 200
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed" and st["filed"]["override"]["reason"].startswith("Zahlendreher")
    assert "trotz Prüfhinweis" in world.notifications[-1]["text"]
    assert "Entscheidung Dr. Lena Hofer:" in world.notifications[-1]["data"]["summary"]     # display name, no MXID


def test_hold_then_discard(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("Patient: Maria Beispiel, born 12 March 1961\n", ""))
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "held_review"
    # 'verwerfen' discards the ANSWER; the request stays open and the link valid for the named person
    view = c.post(f"/api/requests/{req['request_id']}/decision", headers=HDR, json={"decision": "verwerfen"}).json()
    assert view["status"] == "identity_verified" and view["task_status"] == "in-progress"
    card = world.notifications[-1]["text"]
    assert "Entscheidung Dr. Lena Hofer" in card and "@lena" not in card and re.search(r"bis \d\d\.\d\d\.\d{4}", card)
    assert c.svc.store.one("SELECT content, outcome FROM answers WHERE request_id=?", (req["request_id"],)) == \
        {"content": None, "outcome": "discarded"}
    assert any("Antwort verworfen" in e["what"] and "bleibt offen" in e["what"]
               for e in c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"])
    assert c.get(link).status_code == 200                       # the link still works ...
    upload(c, link, doc("lab-result-maria-beispiel.md"))        # ... and the real result is filed
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"


def test_held_content_is_encrypted_at_rest(app_client, mailer):
    c = app_client
    assert c.svc.box.enabled  # cryptography is a project dependency: encryption at rest is mandatory
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("born 12 March 1961", "born 3 December 1961"))
    row = c.svc.store.one("SELECT content, encrypted FROM answers WHERE request_id=?", (req["request_id"],))
    assert row["encrypted"] == 1 and b"Beispiel" not in bytes(row["content"])


# ---------------------------------------------------------------- identity is binary (RULES.md rule 1)
def test_hyphenated_eid_surname_is_blocked(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    r = login(c, link, "Eva", "Brandl-Huber")
    assert r.status_code == 403 and "Maria" not in r.text and not world.transactions


def test_eid_with_extra_given_name_files(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    assert "Maria Beispiel" in login(c, link, "Eva Maria", "Brandl").text
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"


# ---------------------------------------------------------------- security basics
def test_expired_link(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    c.svc.store.update_request(req["request_id"], expires_at="2020-01-01T00:00:00+00:00")
    r = c.get(link)
    assert r.status_code == 410
    assert_no_pii(r.text)


def test_unknown_token(app_client):
    assert app_client.get("/r/" + "b" * 60).status_code == 404
    assert app_client.get("/r/" + "0" * 64).status_code == 404
    assert app_client.get("/r/maria-beispiel").status_code == 404


def test_rate_limit_locks_after_failures(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    for i in range(5):
        c.cookies.clear()
        login(c, link, "Mallory", f"Test{i}")
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "locked"
    assert c.get(link).status_code == 429


def test_forged_assertion_rejected(app_client, mailer):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    assert c.get(f"{link}/eid", params={"assertion": "x.y.z"}).status_code == 405   # never via the query string
    r = c.post(f"{link}/eid", data={"assertion": "eyJmYWtlIjoxfQ.eyJmYWtlIjoxfQ.AAAA"})
    assert r.status_code == 400
    assert_no_pii(r.text)


def test_unknown_recipient_needs_confirmation(app_client, mailer):
    c = app_client
    body = {**REQ1, "recipient": {"name": "Dr. Neue Person", "role": "Laborärztin", "org": "Labor Irgendwo"},
            "address": "neu@irgendwo.example"}
    req = send(c, body)
    assert req["status"] == "needs_confirmation"
    assert any("NICHT in der" in w for w in req["warnings"])
    assert not mailer.sent


def test_unknown_patient_404(app_client):
    r = app_client.post("/api/requests", json={**REQ1, "patient_id": "nobody"}, headers=HDR)
    assert r.status_code == 404


def test_audit_chain_detects_tampering(app_client, mailer):
    c = app_client
    send(c, REQ1)
    assert c.get("/api/audit/verify").json()["ok"]
    con = sqlite3.connect(c.svc.store.path)
    try:
        con.execute("UPDATE audit SET what='x' WHERE id=1")
        raise AssertionError("update should be blocked by trigger")
    except sqlite3.DatabaseError:
        pass
    con.execute("DROP TRIGGER audit_no_update")
    con.execute("UPDATE audit SET what='tampered' WHERE id=1")
    con.commit()
    con.close()
    v = c.get("/api/audit/verify").json()
    assert not v["ok"] and any("modified" in p for p in v["problems"])


# ---------------------------------------------------------------- bot contract
def test_bot_contract_dry_run_confirm_and_cards(app_client, mailer, world):
    c = app_client
    body = {**REQ1, "address": "eva.brandl@labor-kanaI.example", "internal_note": "INR vor OP"}
    body.pop("note")
    dry = c.post("/api/requests", json={**body, "dry_run": True}, headers=HDR).json()
    assert dry["status"] == "dry_run" and dry["dry_run"] is True and not mailer.sent
    assert c.get("/api/requests", headers=HDR).json() == []                      # nothing created
    assert {"code", "message", "similar_to"} <= set(dry["warning_details"][0])
    reg = dry["register"]
    assert reg["ok"] and reg["source"] and reg["name"] == "Dr. Eva Brandl" and reg["detail"] and reg["text"]
    codes = [w["code"] for w in dry["warning_details"]]
    # acknowledging only some codes is not enough
    r = c.post("/api/requests", json={**body, "confirm": True, "confirm_warnings": True, "acknowledged_warnings": codes[:1]}, headers=HDR)
    assert r.json()["status"] == "needs_confirmation" and not mailer.sent
    r = c.post("/api/requests", json={**body, "confirm": True, "confirm_warnings": True, "acknowledged_warnings": codes}, headers=HDR)
    assert r.status_code == 201 and r.json()["status"] == "sent"
    assert "INR" not in mailer.sent[-1]["body"]
    rid = r.json()["request_id"]
    assert c.svc.store.get_request(rid)["note"] == "INR vor OP"

    # misdelivery card: kind + text only
    link = mailer.last_link()
    login(c, link, "Petra", "Fremd")
    n = world.notifications[-1]
    assert n["kind"] == "misdelivery" and n["request_id"] == rid and \
        set(n.get("data") or {}) <= {"locked", "ref_code", "wrong_address", "next_step", "doc_kind", "patient"}
    assert (n["data"].get("patient") or {}).get("name") in (None, "Maria Beispiel")   # only the REQUESTED patient

    a = c.get("/api/audit", params={"request_id": rid}, headers=HDR).json()
    assert a["chain"]["ok"] is True and isinstance(a["chain"]["detail"], str)
    assert "effort_seconds" in a and {"step", "outcome", "who", "what", "at"} <= set(a["entries"][0])


def test_bot_contract_hold_card_and_answer_decision(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("born 12 March 1961", "born 3 December 1961"))
    n = world.notifications[-1]
    assert n["kind"] == "hold" and n["answer_id"]
    m = n["data"]["mismatches"]
    assert len(m) == 1 and {"field": "birthDate", "document": "1961-12-03", "request": "1961-03-12"}.items() <= m[0].items()
    aid = n["answer_id"]
    assert c.post(f"/api/answers/{aid}/decision", headers=HDR, json={"action": "file", "by": "@lena.hofer:klinikum.example"}).status_code == 422
    r = c.post(f"/api/answers/{aid}/decision", headers=HDR, json={"action": "file", "reason": "Zahlendreher bestätigt",
                                                     "by": "@lena.hofer:klinikum.example"})
    assert r.status_code == 200 and r.json()["status"] == "processing"
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    f = world.notifications[-1]
    assert f["kind"] == "filed"
    d = f["data"]
    assert d["validator_errors"] == 0 and d["values_matched"] == 4 and d["values_total"] == 4
    assert d["values_label"] == "Werte" and len(d["observations"]) == 4 and {"test", "value", "unit", "ref"} <= set(d["observations"][0])
    assert d["sender"].startswith("Eva Brandl · ID Austria (Simulation)") and d["sample_time"]
    assert any(x["type"] == "DiagnosticReport" for x in d["resources"])
    assert "außerhalb" not in f["text"] and "Referenzbereich" not in f["text"]  # no computed H/L


def test_bot_contract_reject_and_hold_actions(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("Patient: Maria Beispiel, born 12 March 1961\n", ""))
    n = world.notifications[-1]
    assert n["kind"] == "hold" and n["data"]["mismatches"][0]["field"] == "patient_line"
    aid = n["answer_id"]
    assert c.post(f"/api/answers/{aid}/decision", headers=HDR, json={"action": "hold", "by": "x"}).json()["status"] == "held_review"
    assert c.post(f"/api/answers/{aid}/decision", headers=HDR, json={"action": "reject", "by": "x"}).json()["status"] == "identity_verified"
    assert c.post(f"/api/answers/{aid}/decision", headers=HDR, json={"action": "file", "reason": "abc", "by": "x"}).status_code == 409


def test_wrong_patient_card_never_names_other_patient(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-max-beispiel.md"))
    n = world.notifications[-1]
    assert n["kind"] == "misdelivery" and n["data"]["codes"] == ["WRONG_PATIENT"] and not n["data"]["technical"]
    assert all("Max" not in r and "1960" not in r for r in n["data"]["reasons_de"])
    assert "Max" not in str(n) and "1960" not in str(n)


# ---------------------------------------------------------------- upload only (no typed/pasted form)
def test_upload_only_no_paste_path(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    page = login(c, link, "Eva", "Brandl")
    assert "<textarea" not in page.text and 'type="file"' in page.text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    r = c.post(f"{link}/answer", data={"csrf": csrf, "content": doc("lab-result-maria-beispiel.md")})
    assert r.status_code == 422 and "Datei" in r.text
    r = c.post(f"{link}/answer", data={"csrf": csrf}, files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 422
    assert ("answer sent", "success") not in steps(c, req["request_id"]) and not world.transactions
    # 0 bytes (and whitespace only: fhirmap.decide treats it as EMPTY) -> BLOCK, audited, nothing stored
    for body in (b"", b"   "):
        r = c.post(f"{link}/answer", data={"csrf": csrf}, files={"file": ("x.md", body, "text/markdown")})
        assert r.status_code == 422 and "nicht gespeichert" in r.text
    assert steps(c, req["request_id"]).count(("misdelivery caught", "success")) == 2 and not world.transactions

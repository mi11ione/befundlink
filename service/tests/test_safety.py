"""Safety rules from the red-team cases (tests/jury/redteam): namesake eID (I06), deny-page privacy (I10),
override dead ends (H09/H10/W07/W11), other person in a held file, local-part typos (I18)."""

from .conftest import REQ1, doc
from .test_e2e import HDR, login, send, upload


def _file_as_eva(c, mailer, body=None):
    req = send(c, body or REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    c.cookies.clear()
    return req


def test_namesake_is_held_before_anything_is_shown(app_client, mailer, world):
    c = app_client
    first = _file_as_eva(c, mailer)
    assert c.get(f"/api/requests/{first['request_id']}", headers=HDR).json()["filed"]
    assert world.notifications[-1]["data"]["first_answer_from_person"] is True
    req = send(c, REQ1)                                                       # same recipient + same address
    link = mailer.last_link()
    namesake = c.post("/api/eid/personas", json={"given": "Eva", "family": "Brandl", "birthdate": "1955-04-04"}).json()
    r = login(c, link, "Eva", "Brandl", "1955-04-04", pin=namesake["pin"])
    assert r.status_code == 403 and "Dr. Lena Hofer bestätigt zuerst Ihre Identität" in r.text
    assert "Maria" not in r.text and "Laborbefund" not in r.text and 'type="file"' not in r.text
    n = world.notifications[-1]
    assert n["kind"] == "identity_confirm" and n["data"]["recipient"] == "Dr. Eva Brandl" and n["data"]["ref_code"] == req["ref_code"]
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    # the review holds only the NEW identity: the request itself stays open for the known Dr. Brandl
    assert st["status"] == "opened" and st["identity_review_pending"] and not st["identity_bound"]
    # Dr. Hofer confirms via the answers endpoint with the KB reference (bot contract)
    ok = c.post(f"/api/answers/{req['ref_code']}/decision", headers=HDR, json={"action": "confirm_identity"})
    assert ok.status_code == 200, ok.text
    c.cookies.clear()
    r = login(c, link, "Eva", "Brandl", "1955-04-04", pin=namesake["pin"])
    assert r.status_code == 200 and "Maria Beispiel" in r.text                # now admitted (and bound)


def test_namesake_rejected_stays_blocked_and_real_person_still_answers(app_client, mailer, world):
    c = app_client
    _file_as_eva(c, mailer)
    req = send(c, REQ1)
    link = mailer.last_link()
    namesake = c.post("/api/eid/personas", json={"given": "Eva", "family": "Brandl", "birthdate": "1955-04-04"}).json()
    login(c, link, "Eva", "Brandl", "1955-04-04", pin=namesake["pin"])
    view = c.post(f"/api/answers/{req['ref_code']}/decision", headers=HDR, json={"action": "reject_identity"}).json()
    assert view["status"] == "opened" and not view["identity_review_pending"]
    c.cookies.clear()
    again = login(c, link, "Eva", "Brandl", "1955-04-04", pin=namesake["pin"])          # the rejected one stays out
    assert again.status_code == 403 and "Maria" not in again.text
    c.cookies.clear()
    upload_page = login(c, link, "Eva", "Brandl")                              # the known Dr. Brandl: no friction
    assert upload_page.status_code == 200 and "Maria Beispiel" in upload_page.text
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    assert world.notifications[-1]["data"]["first_answer_from_person"] is False


def test_identity_decisions_need_the_requester(app_client, mailer):
    c = app_client
    _file_as_eva(c, mailer)
    req = send(c, REQ1)
    namesake = c.post("/api/eid/personas", json={"given": "Eva", "family": "Brandl", "birthdate": "1955-04-04"}).json()
    login(c, mailer.last_link(), "Eva", "Brandl", "1955-04-04", pin=namesake["pin"])
    bad = {**HDR, "X-PGK-Actor": "@mallory:klinikum.example"}
    assert c.post(f"/api/answers/{req['ref_code']}/decision", headers=bad, json={"action": "confirm_identity"}).status_code == 403


def test_deny_page_never_names_the_recipient_patient(app_client, mailer, world):
    c = app_client
    body = {**REQ1, "recipient": {"name": "Maria Beispiel", "role": "Patientin", "org": "privat"},
            "address": "maria@privat.example", "doc_kind": "discharge_report", "confirm_warnings": True}
    codes = [w["code"] for w in c.post("/api/requests/check", json=body).json()["warning_details"]]
    send(c, {**body, "acknowledged_warnings": codes})
    daughter = c.post("/api/eid/personas", json={"given": "Maria", "family": "Beispiel", "birthdate": "1987-05-05"}).json()
    r = login(c, mailer.last_link(), "Maria", "Beispiel", "1987-05-05", pin=daughter["pin"])
    page_wo_own_name = r.text.replace("als <b>Maria Beispiel</b>", "")       # her own eID name may be shown
    assert r.status_code == 403 and "Diese Anfrage ist nicht für Sie" in r.text
    assert "Maria Beispiel" not in page_wo_own_name
    alert = world.notifications[-1]["text"]
    rows = c.get("/api/audit").json()["entries"]
    assert any("Geburtsdatum passt nicht zur angefragten Person" in e["what"] for e in rows)
    assert "Name 'Maria Beispiel' passt nicht zur angefragten Person 'Maria Beispiel'" not in str(rows) + alert


def test_override_on_unparseable_hold_files_a_generic_document(app_client, mailer, world):
    """An override of a hold with an unrecognised document type builds a generic DocumentReference,
    proven byte for byte - no dead end, no 'Technischer Fehler'."""
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, "## Patientin: Maria Beispiel, geb. 12.03.1961\n\nBefund folgt telefonisch.\n")
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "held_review"
    aid = world.notifications[-1]["answer_id"]
    r = c.post(f"/api/answers/{aid}/decision", headers=HDR, json={"action": "file", "reason": "telefonisch bestätigt"})
    assert r.status_code == 200
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed" and st["filed"]["main"].startswith("DocumentReference/")
    assert "Technischer Fehler" not in str(world.notifications)


def test_build_error_keeps_the_answer_held(app_client, mailer, world, monkeypatch):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("born 12 March 1961", "born 3 December 1961"))
    aid = world.notifications[-1]["answer_id"]

    def boom(*a, **kw):
        raise ValueError("cannot build resources for this document")
    monkeypatch.setattr(c.svc.fm, "build_resources", boom)
    c.post(f"/api/answers/{aid}/decision", headers=HDR, json={"action": "file", "reason": "Zahlendreher"})
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "held_review" and "Ablegen nicht möglich" in st["last_reason"]
    assert "Technischer Fehler" not in str(world.notifications) and not world.transactions
    assert c.svc.store.one("SELECT content FROM answers WHERE id=?", (int(aid),))["content"] is not None  # kept intact


def test_hold_card_flags_another_person_without_naming_her(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("born 12 March 1961", "born 3 December 1961"))
    r = c.svc.store.get_request(req["request_id"])
    aid = int(world.notifications[-1]["answer_id"])
    c.svc._hold(r, aid, "held_review", ["Die Datei enthält zusätzlich Angaben zu einer anderen Person (wird nicht genannt)"],
                "Eva Brandl", detail={"checks": ["OTHER_PATIENT_PRESENT"]})
    n = world.notifications[-1]
    assert n["kind"] == "hold" and n["data"]["details"]["other_person_present"] is True
    assert "OTHER_PATIENT_PRESENT" in n["data"]["codes"] and "Max" not in str(n)


def test_local_part_typo_warns_before_sending(app_client, mailer):
    c = app_client
    pre = c.post("/api/requests/check", json={**REQ1, "address": "eva.brand1@labor-kanal.example"}).json()
    w = next(x for x in pre["warning_details"] if x["code"] == "local_part_lookalike")
    assert w["similar_to"] == "eva.brandl@labor-kanal.example" and pre["needs_confirmation"]
    r = c.post("/api/requests", json={**REQ1, "address": "eva.brand1@labor-kanal.example"}, headers=HDR)
    assert r.json()["status"] == "needs_confirmation" and not mailer.sent

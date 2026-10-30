"""Held answers and corrections: held-document preview, sender texts, patient as sender, namesake memory per
recipient, an identity card worth deciding on, no lockout, attribution, corrections, demo enrolment, superseded holds."""
import json

from service.core import mask_contact, normalise_contact

from .conftest import REQ1, doc
from .test_e2e import HDR, PINS, confirm, login, send, upload

LAB = doc("lab-result-maria-beispiel.md")
NO_PATIENT_LINE = LAB.replace("Patient: Maria Beispiel, born 12 March 1961\n", "")   # -> held_review
FREEMAIL = {**REQ1, "address": "eva.brandl@freemail.example"}


def _held(c, mailer):
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, NO_PATIENT_LINE)
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "held_review"
    return req, link


def _file_as_eva(c, mailer):
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, LAB)
    c.cookies.clear()
    return c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()


def _send_confirmed(c, body):
    created = c.post("/api/requests", json=body, headers=HDR).json()
    if created["status"] == "needs_confirmation":
        created = confirm(c, created).json()
    assert created["status"] == "sent", created
    return created


# ---------------------------------------------------------------- held-document preview
def test_preview_shows_the_held_document_to_the_requester_only(app_client, mailer):
    c = app_client
    req, _ = _held(c, mailer)
    url = f"/api/answers/{req['ref_code']}/preview"
    assert c.get(url).status_code == 403
    assert c.get(url, headers={**HDR, "X-PGK-Actor": "@mallory:klinikum.example"}).status_code == 403
    r = c.get(url, headers=HDR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"text", "filename"} and body["filename"] is None
    assert body["text"].splitlines() == NO_PATIENT_LINE.splitlines()[:40]
    assert body["truncated"] == (len(NO_PATIENT_LINE.splitlines()) > 40)
    steps = [e["step"] for e in c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]]
    assert "held document viewed" in steps
    c.post(f"/api/requests/{req['request_id']}/decision", headers=HDR, json={"decision": "verwerfen"})
    assert c.get(url, headers=HDR).status_code == 409                    # discarded: nothing left to show


def test_preview_after_a_later_filed_answer_says_no_open_decision(app_client, mailer):
    c = app_client
    req, link = _held(c, mailer)
    upload(c, link, LAB)                                                  # the right document, filed
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    r = c.get(f"/api/answers/{req['ref_code']}/preview", headers=HDR)
    assert r.status_code == 409 and "Keine offene Entscheidung: eine spätere Antwort wurde abgelegt." in r.text


def test_preview_never_shows_a_blocked_upload(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    assert upload(c, link, doc("lab-result-max-beispiel.md")).status_code == 422   # wrong patient: BLOCK
    assert c.get(f"/api/answers/{req['ref_code']}/preview", headers=HDR).status_code == 409


# ---------------------------------------------------------------- sender-facing texts
def test_other_person_in_the_upload_is_explained_to_the_sender(app_client, mailer):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    r = upload(c, link, LAB + "\nPatient: Max Beispiel, born 1 January 1960\n")
    assert r.status_code == 422
    assert "Das Dokument enthält Angaben zu einer weiteren Person und wurde nicht übermittelt." in r.text
    assert "Max" not in r.text


def test_block_alert_has_no_double_period(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, LAB + "\nPatient: Max Beispiel, born 1 January 1960\n")
    alert = world.notifications[-1]["text"]
    assert ".." not in alert and "Nichts wurde gespeichert" in alert


def test_patient_herself_is_named_as_such_on_the_filed_card(app_client, mailer, world):
    c = app_client
    body = {**REQ1, "recipient": {"name": "Maria Beispiel", "role": "Patientin", "org": "privat"},
            "address": "maria@privat.example", "doc_kind": "discharge_report"}
    _send_confirmed(c, body)
    link = mailer.last_link()
    herself = c.post("/api/eid/personas", json={"given": "Maria", "family": "Beispiel", "birthdate": "1961-03-12"}).json()
    login(c, link, "Maria", "Beispiel", "1961-03-12", pin=herself["pin"])
    upload(c, link, doc("rehab-report-maria-beispiel.md"))   # 'sent by Stefan Kern': held, Dr. Hofer files it
    hold = world.notifications[-1]
    assert hold["kind"] == "hold" and "Stefan Kern" in hold["data"]["named_sender"]       # the document's sender
    rid = c.svc.store.q("SELECT id FROM requests")[-1]["id"]
    assert c.post(f"/api/requests/{rid}/decision", headers=HDR,
                  json={"decision": "ablegen", "reason": "Patientin schickt den Bericht der Reha selbst"}).status_code == 200
    filed = next(n for n in world.notifications if n.get("kind") == "filed")
    assert filed["data"]["sender"] == "Maria Beispiel (Patientin selbst, ID Austria – Simulation)"
    assert "nicht im Register" not in filed["text"]
    assert filed["data"].get("stay") and filed["data"].get("recommendations")              # card_facts (discharge)


# ---------------------------------------------------------------- confirm needs the warning list
def test_confirm_requires_every_warning_code(app_client, mailer):
    c = app_client
    created = c.post("/api/requests", json=FREEMAIL, headers=HDR).json()
    assert created["status"] == "needs_confirmation" and created["warning_details"]
    rid = created["request_id"]
    assert c.post(f"/api/requests/{rid}/confirm", headers=HDR).status_code == 409
    assert c.post(f"/api/requests/{rid}/confirm", headers=HDR, json={"acknowledged_warnings": []}).status_code == 409
    assert not mailer.sent
    assert confirm(c, created).json()["status"] == "sent"


# ---------------------------------------------------------------- namesake through a new contact
def test_contacts_are_normalised_and_masked():
    assert normalise_contact("email", " Eva.Brandl+Labor@Labor-Kanal.example ") == "eva.brandl@labor-kanal.example"
    assert mask_contact("email", "Eva.Brandl@labor-kanaI.example") == "e***@labor-kanaI.example"   # domain as typed
    assert mask_contact("sms", "+43 660 000 0101") == "+43***01"


def _namesake(c):
    return c.post("/api/eid/personas", json={"given": "Eva", "family": "Brandl", "birthdate": "1955-04-04"}).json()


def test_namesake_via_a_new_address_is_held_and_the_card_says_what_differs(app_client, mailer, world):
    c = app_client
    first = _file_as_eva(c, mailer)
    req = _send_confirmed(c, FREEMAIL)                                     # a NEW contact for the same recipient
    link = mailer.last_link()
    r = login(c, link, "Eva", "Brandl", "1955-04-04", pin=_namesake(c)["pin"])
    assert r.status_code == 403 and "Maria" not in r.text and 'type="file"' not in r.text
    card = world.notifications[-1]
    assert card["kind"] == "identity_confirm"
    d = card["data"]
    eva_year = PINS[("Eva", "Brandl")]["birthdate"][:4]
    assert d["new_birth_year"] == "1955" and d["birth_year_differs"] is True
    assert d["known_answers"] == 1 and d["known_since"] == first["sent_at"][:10]
    assert d["via_contact"] == "e***@freemail.example"
    assert "Anderes Geburtsjahr als bisher" in d["detail"] and f"Jahrgang {eva_year}" in d["detail"]
    blob = json.dumps(card)
    assert "1955-04-04" not in blob and "04.04.1955" not in blob and "bpk" not in blob.lower()
    assert "eva.brandl@freemail" not in blob
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["identity_review_pending"] and not st["identity_bound"]
    # no lockout: the KNOWN Dr. Brandl answers through the same link while the review is pending
    c.cookies.clear()
    page = login(c, link, "Eva", "Brandl")
    assert page.status_code == 200 and "Maria Beispiel" in page.text
    upload(c, link, LAB)
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"


def test_confirmed_namesake_is_filed_under_her_own_practitioner(app_client, mailer, world):
    c = app_client
    _file_as_eva(c, mailer)
    eva_prov = world.resources[next(k for k in world.resources if k.startswith("Provenance/"))]
    req = _send_confirmed(c, FREEMAIL)
    link = mailer.last_link()
    namesake = _namesake(c)
    login(c, link, "Eva", "Brandl", "1955-04-04", pin=namesake["pin"])
    assert c.post(f"/api/answers/{req['ref_code']}/decision", headers=HDR,
                  json={"action": "confirm_identity"}).status_code == 200
    c.cookies.clear()
    login(c, link, "Eva", "Brandl", "1955-04-04", pin=namesake["pin"])
    upload(c, link, LAB)
    filed = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert filed["status"] == "filed", filed["last_reason"]
    prov = world.resources[next(x for x in filed["filed"]["resources"] if x.startswith("Provenance/"))]

    def sender_practitioner(p):
        """The eID-verified sender = the Provenance 'informant'."""
        ref = next(a["who"]["reference"] for a in p["agent"] if a["type"]["coding"][0]["code"] == "informant")
        return ref, world.resources[ref]

    eva_ref, _ = sender_practitioner(eva_prov)
    her_ref, her = sender_practitioner(prov)
    sub = c.svc.store.one("SELECT bound_bpk FROM requests WHERE id=?", (req["request_id"],))["bound_bpk"]
    assert her_ref != eva_ref                                               # never merged with Dr. Brandl
    assert any(i["value"] == sub for i in her.get("identifier") or [])      # keyed by HER eID hash
    assert not any("aerzteliste" in i["system"] for i in her.get("identifier") or [])


def test_unrecognised_document_hold_card_quotes_its_first_sentence(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, "Sehr geehrte Frau Kollegin, anbei wie besprochen die Unterlagen. Mit freundlichen Grüßen\n")
    hold = world.notifications[-1]
    assert hold["kind"] == "hold", hold
    assert hold["data"]["first_sentence"].startswith("Sehr geehrte Frau Kollegin")


# ---------------------------------------------------------------- corrections
CORRECTED = (LAB.replace("# Labor Kanalstraße: lab report", "# Labor Kanalstraße: Korrigierter Befund")
             .replace("| Fibrinogen | 3.1 |", "| Fibrinogen | 3.4 |")
             .replace("All values are within the reference range.",
                      "Dieser Befund ersetzt den Befund vom 22.10.2026, 11:40. All values are within the reference range."))


def _correction_held(c, mailer, world, body=REQ1):
    first = _file_as_eva(c, mailer)
    req = send(c, body)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, CORRECTED)
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "held_review", st["last_reason"]
    return first, req, world.notifications[-1]


def test_correction_is_held_with_the_prior_report_on_the_card(app_client, mailer, world):
    first, req, hold = _correction_held(app_client, mailer, world)
    assert "CORRECTION_REVIEW" in hold["data"]["codes"]
    prior = hold["data"]["prior_report"]
    assert prior["found"] == 1 and prior["ref_code"] == first["ref_code"] and prior["reference"] == first["filed"]["main"]
    assert hold["data"]["replaces"] == {"text": f"ersetzt {first['ref_code']}, Probe 22.10.2026, 08:10"}


def test_correction_card_says_when_the_prior_is_not_unique(app_client, mailer, world):
    c = app_client
    _file_as_eva(c, mailer)
    _file_as_eva(c, mailer)                                                   # the same report filed twice
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, CORRECTED)
    hold = world.notifications[-1]
    assert hold["data"]["prior_report"]["found"] == 2
    assert hold["data"]["replaces"] == {"text": "2 frühere Befunde passen – nicht eindeutig"}


def test_correction_then_reverify_is_information_not_an_alarm(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    first, req, _ = _correction_held(c, mailer, world)
    c.post(f"/api/requests/{req['request_id']}/decision", headers=HDR,
           json={"decision": "ablegen", "reason": "Korrektur des Labors übernehmen"})
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    res = svc.reverify_all()
    assert not res["failed"], res["failed"]
    assert len([x for x in res["superseded"] if x.startswith(first["ref_code"])]) >= 1
    assert all(req["ref_code"] in x for x in res["superseded"])               # 'Korrektur KB-...' names the correction
    assert not [n for n in world.notifications if "[ACHTUNG]" in n["text"]]
    # an unauthorised new version nobody signed IS an alarm
    dr = first["filed"]["main"]
    edited = json.loads(json.dumps(world.resources[dr]))
    edited["conclusion"] = "alles in Ordnung"
    world.put(dr, edited)
    res = svc.reverify_all()
    assert res["failed"] and any(dr in f["changed"] for f in res["failed"])
    assert [n for n in world.notifications if "[ACHTUNG]" in n["text"]]


def test_correction_on_override_files_new_versions_of_the_prior_report(app_client, mailer, world):
    c = app_client
    first, req, _ = _correction_held(c, mailer, world)
    dr_ref = first["filed"]["main"]
    fib = next(r for r in first["filed"]["resources"] if r.startswith("Observation/")
               and "Fibrinogen" in json.dumps(world.resources[r]))
    r = c.post(f"/api/requests/{req['request_id']}/decision", headers=HDR,
               json={"decision": "ablegen", "reason": "Korrektur des Labors übernehmen"})
    assert r.status_code == 200, r.text
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed", st["last_reason"]
    assert st["filed"]["correction"]["replaces"] == dr_ref
    assert st["filed"]["main"] == dr_ref                                          # the SAME report, a new version
    tx = world.transactions[-1]
    puts = {e["request"]["url"]: e["request"] for e in tx["entry"] if e["request"]["method"] == "PUT"}
    assert dr_ref in puts and puts[dr_ref].get("ifMatch") and fib in puts
    assert world.resources[dr_ref]["status"] == "corrected" and world.resources[fib]["status"] == "corrected"
    assert world.resources[fib]["valueQuantity"]["value"] == 3.4
    assert len([k for k in world.resources if k.startswith("DiagnosticReport/")]) == 1   # never a second report
    sig = st["filed"]["signature"]
    assert sig["verified"] and sig["bound"] and sig["content_checked"], sig


def test_correction_without_a_unique_prior_report_stays_held(app_client, mailer, world):
    c = app_client
    send(c, REQ1)                                                              # nothing filed before
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, CORRECTED)
    rid = c.svc.store.q("SELECT id FROM requests")[-1]["id"]
    assert world.notifications[-1]["data"]["prior_report"]["found"] == 0
    c.post(f"/api/requests/{rid}/decision", headers=HDR, json={"decision": "ablegen", "reason": "Korrektur übernehmen"})
    st = c.get(f"/api/requests/{rid}", headers=HDR).json()
    assert st["status"] == "held_review" and "Ursprungsbefund nicht gefunden" in st["last_reason"]
    assert not world.transactions


def test_filing_without_recorded_versions_is_checked_at_the_version_it_signed(app_client, mailer, world):
    """Filings from before versions were recorded (live KB-D446FF): the signed version is inferred from history."""
    c, svc = app_client, app_client.svc
    first, req, _ = _correction_held(c, mailer, world)
    c.post(f"/api/requests/{req['request_id']}/decision", headers=HDR,
           json={"decision": "ablegen", "reason": "Korrektur des Labors übernehmen"})
    old = svc.store.get_request(first["request_id"])["filed"]
    del old["versions"]
    svc.store.update_request(first["request_id"], filed=old)
    res = svc.reverify_all()
    assert not res["failed"], res["failed"]
    assert any(x.startswith(first["ref_code"]) for x in res["superseded"])


# ---------------------------------------------------------------- demo: a new outside person can sign in
NOVAK = {**REQ1, "recipient": {"name": "Dr. Anna Novak", "role": "Laborärztin", "org": "Labor Donaustadt"},
         "address": "anna.novak@labor-donaustadt.example"}


def test_split_person_name_strips_titles():
    from service.eid import split_person_name
    assert split_person_name("Dr. Anna Novak") == ("Anna", "Novak")
    assert split_person_name("Univ.-Prof. Dr. Eva Maria Brandl-Huber, MSc") == ("Eva Maria", "Brandl-Huber")
    assert split_person_name("Mag. Stefan Kern") == ("Stefan", "Kern")


def test_demo_enrols_a_new_person_and_her_link_still_cannot_be_forwarded(app_client, mailer, world):
    c = app_client
    c.svc.s.demo = True
    dry = c.post("/api/requests", json={**NOVAK, "dry_run": True}, headers=HDR).json()
    assert "demo_identity" not in dry and not c.svc.idp.has_persona("Dr. Anna Novak")      # not on a dry run
    created = c.post("/api/requests", json=NOVAK, headers=HDR).json()
    if created["status"] == "needs_confirmation":
        assert "demo_identity" not in created
        created = confirm(c, created).json()
    demo = created["demo_identity"]
    assert demo["name"] == "Anna Novak" and len(demo["pin"]) == 6 and demo["pin"].isdigit()
    sent = next(n for n in world.notifications if n.get("kind") == "request_sent")
    assert sent["data"]["demo_identity"] == demo and demo["pin"] not in sent["text"]
    audit = c.get("/api/audit", params={"request_id": created["request_id"]}, headers=HDR).json()["entries"]
    enrol = next(e for e in audit if e["step"] == "demo identity enrolled")
    assert "Demo-Identität angelegt" in enrol["what"] and "Novak" not in enrol["what"] and demo["pin"] not in enrol["what"]
    link = mailer.last_link()
    page = c.get("/eid/login", params={"return": f"{link}/eid"}).text
    assert "Novak" not in page and "Maria Beispiel" not in page                         # no persona / patient listed
    # a colleague the link was forwarded to: no PIN -> refused; another name -> blocked
    r = login(c, link, "Anna", "Novak", "1980-01-01", pin=None)
    assert r.status_code in (400, 403, 422) and "geschützt" in r.text and "Maria" not in r.text
    c.cookies.clear()
    assert login(c, link, "Karl", "Kollege", "1975-05-05").status_code == 403
    c.cookies.clear()
    # Dr. Novak herself, with the PIN Dr. Hofer passed on
    page = login(c, link, "Anna", "Novak", "1980-01-01", pin=demo["pin"])
    assert page.status_code == 200 and "Maria Beispiel" in page.text
    upload(c, link, LAB.replace("Labor Kanalstraße", "Labor Donaustadt").replace("Dr. Eva Brandl", "Dr. Anna Novak"))
    st = c.get(f"/api/requests/{created['request_id']}", headers=HDR).json()
    assert st["status"] in ("filed", "held_review"), st["last_reason"]


def test_without_demo_nobody_is_enrolled(app_client, mailer):
    c = app_client
    created = c.post("/api/requests", json=NOVAK, headers=HDR).json()
    if created["status"] == "needs_confirmation":
        created = confirm(c, created).json()
    assert "demo_identity" not in created and not c.svc.idp.has_persona("Dr. Anna Novak")


# ---------------------------------------------------------------- a second patient (Max Beispiel)
def test_a_request_for_max_files_maxs_document_and_marias_still_blocks_it(app_client, mailer, world):
    c = app_client
    req = send(c, {**REQ1, "patient_id": "max-beispiel"})
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-max-beispiel.md"))
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed", st["last_reason"]
    assert all(world.resources[x]["subject"]["reference"] == "Patient/max-beispiel"
               for x in st["filed"]["resources"] if x.startswith(("DiagnosticReport/", "Observation/")))
    c.cookies.clear()
    req2 = send(c, REQ1)                                                    # Maria's request: M3 still blocks Max
    link2 = mailer.last_link()
    login(c, link2, "Eva", "Brandl")
    assert upload(c, link2, doc("lab-result-max-beispiel.md")).status_code == 422
    assert c.get(f"/api/requests/{req2['request_id']}", headers=HDR).json()["status"] != "filed"


def test_demo_identity_is_shown_again_on_every_request_to_that_person(app_client, mailer, world):
    c = app_client
    c.svc.s.demo = True
    pins = []
    for _ in range(2):
        created = c.post("/api/requests", json=NOVAK, headers=HDR).json()
        if created["status"] == "needs_confirmation":
            created = confirm(c, created).json()
        pins.append(created["demo_identity"])
    assert pins[0] == pins[1] and pins[0]["name"] == "Anna Novak"
    enrolled = c.svc.store.q("SELECT * FROM audit WHERE step='demo identity enrolled'")
    assert len(enrolled) == 1                                                  # audited on enrolment only
    seed = c.post("/api/requests", json=REQ1, headers=HDR).json()             # Dr. Brandl: a seed persona
    assert "demo_identity" not in seed
    assert c.svc.store.one("SELECT demo_pin FROM personas WHERE given='Eva' AND family='Brandl'")["demo_pin"] is None


# ---------------------------------------------------------------- M1: one typo, one warning
def test_m1_typo_is_one_warning_with_every_code_kept(app_client, mailer):
    c = app_client
    body = {**REQ1, "address": "eva.brandl@labor-kanaI.example"}
    pre = c.post("/api/requests/check", json=body).json()
    assert pre["warnings"] == ["Adresse prüfen: labor-kanaI.example (großes I) statt der bekannten labor-kanal.example "
                               "(kleines l) – Tippfehler oder Täuschung?"]
    codes = [w["code"] for w in pre["warning_details"]]
    assert {"confusable", "uppercase_in_domain"} <= set(codes)
    assert [w["code"] for w in pre["warning_details"] if not w.get("merged_into")] == ["confusable"]
    created = c.post("/api/requests", json=body, headers=HDR).json()
    assert created["status"] == "needs_confirmation"
    assert confirm(c, created).json()["status"] == "sent"                   # every code acknowledged as before


# ---------------------------------------------------------------- a blocked upload leaves no receipt
def test_blocked_upload_has_no_receipt_and_the_status_page_shows_the_rejection(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    r = upload(c, link, doc("lab-result-max-beispiel.md"))
    assert r.status_code == 422
    status = c.get(f"{link}/status")
    assert status.status_code == 422 and "betrifft nicht" in status.text and "übermittelt an" not in status.text
    assert 'type="file"' in status.text                                    # the link stays usable
    assert "Max" not in status.text
    rc = c.get(f"{link}/receipt.txt")
    assert rc.status_code == 404 and "nicht übermittelt" in rc.text and "SHA-256" not in rc.text
    upload(c, link, LAB)                                                   # the corrected document
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    rc = c.get(f"{link}/receipt.txt")
    assert rc.status_code == 200 and "Übermittelt an" in rc.text


# ---------------------------------------------------------------- superseded holds
def test_filing_a_later_answer_voids_the_earlier_hold_cards(app_client, mailer, world):
    c = app_client
    req, link = _held(c, mailer)
    upload(c, link, LAB)                                                   # the same person sends the right file
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    notes = [n for n in world.notifications if n.get("kind") == "holds_superseded"]
    assert len(notes) == 1 and req["ref_code"] in notes[0]["text"] and "keine Entscheidung mehr nötig" in notes[0]["text"]
    assert notes[0]["data"]["answer_ids"]
    rows = c.svc.store.q("SELECT outcome FROM answers WHERE request_id=? ORDER BY id", (req["request_id"],))
    assert [x["outcome"] for x in rows] == ["superseded", "filed"]


def test_no_superseded_notice_without_an_earlier_hold(app_client, mailer, world):
    _file_as_eva(app_client, mailer)
    assert not [n for n in world.notifications if n.get("kind") == "holds_superseded"]


# ---------------------------------------------------------------- override never files a junk unit
def _override(c, req):
    r = c.post(f"/api/requests/{req['request_id']}/decision", headers=HDR,
               json={"decision": "ablegen", "reason": "Werte am Original geprüft, Layout des Labors"})
    assert r.status_code == 200, r.text
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "filed", st["last_reason"]
    return st


def test_jury_bulleted_report_never_files_a_junk_unit(app_client, mailer, world):
    """The exact jury reproduction. Whether the parser reads its units (then it files directly) or holds it (then
    Dr. Hofer overrides): no Observation may carry a unit without its UCUM code."""
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-bulleted-maria.md"))
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    if st["status"] == "held_review":
        st = _override(c, req)
    assert st["status"] == "filed", st["last_reason"]
    for x in st["filed"]["resources"]:
        if x.startswith("Observation/"):
            q = world.resources[x].get("valueQuantity")
            assert q is None or (q.get("system") == "http://unitsofmeasure.org" and q.get("code")), q


def test_override_of_an_unreadable_unit_files_that_value_as_text(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, LAB.replace("| aPTT | 31 | s |", "| aPTT | 31 | Sekündchen |"))
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "held_review" and "nicht zuordenbar" in st["last_reason"]
    st = _override(c, req)
    obs = {world.resources[x]["code"]["text"]: world.resources[x] for x in st["filed"]["resources"]
           if x.startswith("Observation/")}
    aptt = obs["aPTT"]
    assert aptt["valueString"] == "| aPTT | 31 | Sekündchen | 25 to 37 | 14979-9 |"            # the row as written
    assert "valueQuantity" not in aptt and "referenceRange" not in aptt and "interpretation" not in aptt
    assert aptt["code"]["coding"][0]["code"] == "14979-9"                                    # known LOINC kept
    assert obs["INR (prothrombin time)"]["valueQuantity"]["value"] == 1.0                    # the others as before
    assert st["filed"]["proof"]["ok"] and world.validated                                    # prove() passed
    card = next(n for n in world.notifications if n.get("kind") == "filed")
    assert "davon 1 als Text abgelegt (Einheit nicht zuordenbar)" in card["text"] and "übernommen" not in card["text"]
    assert card["data"]["values_matched"] is None and card["data"]["text_only_rows"] == ["aPTT"]
    assert [o["test"] for o in card["data"]["observations"] if o.get("as_text")] == ["aPTT"]


def test_text_only_rows_leaves_other_holds_alone():
    from service.core import text_only_rows
    parsed = type("P", (), {"lab": [{"test": "INR", "value": 1.0}], "text": ""})()
    assert text_only_rows(parsed, {"codes": ["NAME_NEAR"], "details": []}) is parsed

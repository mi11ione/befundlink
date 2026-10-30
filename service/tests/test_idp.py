"""Simulated ID Austria: nobody can claim someone else's identity, ES256 token without bPK/DOB,
POST hand-off, single use, bound Provenance signature, patient-as-recipient, pseudonymous strangers in FHIR."""
import base64
import hashlib
import json
import re

import fhirmap

from .conftest import REQ1, doc
from .test_e2e import HDR, PINS, login, send, upload


def _post_login(c, link, **form):
    return c.post("/eid/login", data={"return": f"{link}/eid", **form})


def _claims(token):
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


def test_named_person_cannot_be_claimed_without_pin(app_client, mailer):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    c.get(link)
    r = _post_login(c, link, given="Eva", family="Brandl", birthdate="1979-05-14")
    assert r.status_code == 422 and "PIN" in r.text and 'id="handoff"' not in r.text
    r = _post_login(c, link, given="Eva", family="Brandl", birthdate="1979-05-14", pin="000000")
    assert r.status_code == 422 and 'id="handoff"' not in r.text
    r = _post_login(c, link, given="Dr. Eva", family="BRANDL", birthdate="1979-05-14")   # variants don't help
    assert r.status_code == 422
    pin = PINS[("Eva", "Brandl")]["pin"]
    r = _post_login(c, link, given="Eva", family="Brandl", birthdate="1900-01-01", pin=pin)
    assert r.status_code == 200 and 'id="handoff"' in r.text            # the persona's own data is used, not the typed DOB


def test_open_recipient_without_persona_is_protected_until_enrolled(app_client, mailer):
    c = app_client
    body = {**REQ1, "recipient": {"name": "Dr. Neue Person", "role": "Laborärztin", "org": "Labor Irgendwo"},
            "address": "neu@irgendwo.example", "confirm_warnings": True}
    codes = [w["code"] for w in c.post("/api/requests/check", json=body).json()["warning_details"]]
    send(c, {**body, "acknowledged_warnings": codes})
    link = mailer.last_link()
    c.get(link)
    assert _post_login(c, link, given="Neue", family="Person", birthdate="1980-01-01").status_code == 422
    enrolled = c.post("/api/eid/personas", json={"given": "Neue", "family": "Person", "birthdate": "1980-01-01"}).json()
    assert re.fullmatch(r"\d{6}", enrolled["pin"])
    r = login(c, link, "Neue", "Person", "1980-01-01", pin=enrolled["pin"])
    assert r.status_code == 200 and "Maria Beispiel" in r.text


def test_new_unprotected_identity_is_free_entry(app_client, mailer):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    c.get(link)
    r = _post_login(c, link, given="Mallory", family="Unbekannt", birthdate="1977-07-07")
    assert r.status_code == 200 and 'id="handoff"' in r.text            # allowed in, then blocked by the named-person check


def test_eid_page_never_lists_personas_not_even_in_demo(app_client, mailer):
    """No quick list: the README gives the PINs; the page has only the form with its PIN field."""
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    for demo in (False, True):
        c.svc.s.demo = demo
        page = c.get("/eid/login", params={"return": f"{link}/eid"}).text
        assert "Demo-Personen" not in page and "Huber" not in page and "Fremd" not in page
        assert page.count('name="pin"') == 1 and page.count("<form") == 1


def test_token_es256_minimal_single_use(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    c.get(link)
    p = PINS[("Eva", "Brandl")]
    r = _post_login(c, link, given="Eva", family="Brandl", birthdate=p["birthdate"], pin=p["pin"])
    tok = re.search(r'name="assertion" value="([^"]+)"', r.text).group(1)
    assert "assertion" not in str(r.url)
    head = json.loads(base64.urlsafe_b64decode(tok.split(".")[0] + "=="))
    cl = _claims(tok)
    assert head["alg"] == "ES256" and head["kid"] in [k["kid"] for k in c.get("/eid/.well-known/jwks.json").json()["keys"]]
    token = link.rsplit("/", 1)[1]
    assert cl["aud"] == hashlib.sha256(token.encode()).hexdigest() and cl["sub"].startswith("bpk-hmac:")
    assert cl["simulated"] is True and cl["exp"] > cl["iat"] and cl["nonce"] and cl["jti"]
    assert "birthdate" not in json.dumps(cl) and p["birthdate"] not in json.dumps(cl) and "GH:" not in json.dumps(cl)
    action = re.search(r'action="([^"]+)"', r.text).group(1)
    assert c.post(action, data={"assertion": tok}).status_code == 200   # -> answer page
    c.cookies.clear()
    assert c.post(action, data={"assertion": tok}).status_code == 400   # replay refused
    tampered = tok.rsplit(".", 1)[0] + "." + "A" * 86
    assert c.post(action, data={"assertion": tampered}).status_code == 400
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["eid_attempts"] == 1  # only the verified one counted


def test_provenance_signature_is_bound_and_verifiable(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    tx = world.transactions[-1]
    prov = next(e["resource"] for e in tx["entry"] if e["resource"]["resourceType"] == "Provenance")
    jwks = c.get("/.well-known/jwks.json").json()
    v = fhirmap.verify_provenance_signature(prov, jwks)
    assert v["valid"] and v["bound"], v["problems"]
    stmt = v["claims"]
    assert stmt["simulated"] is True and stmt["given_name"] == "Eva" and "birthdate" not in json.dumps(stmt)
    assert _claims(stmt["id_token"])["iss"].endswith("/eid")             # the IdP token is embedded and checkable
    moved = json.loads(json.dumps(prov))
    moved["target"] = moved["target"][:-1]                               # moving the statement onto another filing fails
    assert not fhirmap.verify_provenance_signature(moved, jwks)["valid"]
    assert "simulated" in json.dumps(prov.get("policy"))


def test_patient_as_recipient_needs_matching_birth_date(app_client, mailer):
    c = app_client
    body = {**REQ1, "recipient": {"name": "Maria Beispiel", "role": "Patientin", "org": "privat"},
            "address": "maria@privat.example", "doc_kind": "discharge_report", "confirm_warnings": True}
    codes = [w["code"] for w in c.post("/api/requests/check", json=body).json()["warning_details"]]
    r = c.post("/api/requests", json={**body, "acknowledged_warnings": codes}, headers=HDR)
    assert r.status_code == 201, r.text
    link = mailer.last_link()
    relative = c.post("/api/eid/personas", json={"given": "Maria", "family": "Beispiel", "birthdate": "1990-06-06"}).json()
    assert login(c, link, "Maria", "Beispiel", "1990-06-06", pin=relative["pin"]).status_code == 403
    c.cookies.clear()
    herself = c.post("/api/eid/personas", json={"given": "Maria", "family": "Beispiel", "birthdate": "1961-03-12"}).json()
    assert login(c, link, "Maria", "Beispiel", "1961-03-12", pin=herself["pin"]).status_code == 200


def test_stranger_is_pseudonymous_in_fhir_audit(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    assert login(c, link, "Petra", "Fremd").status_code == 403
    assert c.svc.outbox.drain(5)
    evs = [e for e in world.audit_events() if e["subtype"][0]["code"] in ("misdelivery-caught", "identity-checked")]
    assert evs and all("Fremd" not in json.dumps(e) and "Petra" not in json.dumps(e) for e in evs)
    rows = c.get("/api/audit", params={"request_id": c.svc.store.q("SELECT id FROM requests")[0]["id"]}, headers=HDR).json()["entries"]
    assert any("Petra Fremd" in r["who"] for r in rows)                   # cleartext only in the service audit


def test_decisions_require_requester_in_room(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("born 12 March 1961", "born 3 December 1961"))
    url = f"/api/requests/{req['ref_code']}/decision"
    body = {"decision": "ablegen", "reason": "Zahlendreher bestätigt", "by": "@lena.hofer:klinikum.example"}
    assert c.post(url, json=body).status_code == 403                                           # no actor headers
    assert c.post(url, json=body, headers={**HDR, "X-PGK-Actor": "@mallory:klinikum.example"}).status_code == 403
    assert c.post(url, json=body, headers={**HDR, "X-PGK-Room": "!other:klinikum.example"}).status_code == 403
    assert c.get(f"/api/requests/{req['request_id']}", headers={**HDR, "X-PGK-Room": "!x:y"}).status_code == 403
    assert c.post("/api/requests/req-zz/decision", json=body, headers=HDR).status_code == 422  # id format
    assert c.post(url, json=body, headers=HDR).status_code == 200


def test_pin_throttle_is_per_identity_not_per_ip(app_client, mailer):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    c.get(link)
    for _ in range(12):  # someone hammers Thomas Huber's PIN from 127.0.0.1 ...
        _post_login(c, link, given="Thomas", family="Huber", birthdate="1982-11-30", pin="111111")
    p = PINS[("Eva", "Brandl")]
    r = _post_login(c, link, given="Eva", family="Brandl", birthdate=p["birthdate"], pin=p["pin"])
    assert 'id="handoff"' in r.text                                     # ... Eva from the same IP is not locked out
    h = PINS[("Thomas", "Huber")]
    r = _post_login(c, link, given="Thomas", family="Huber", birthdate=h["birthdate"], pin=h["pin"])
    assert r.status_code == 422 and "später" in r.text                  # but Huber's identity is throttled


def _file_once(c, mailer):
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    c.cookies.clear()
    return c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()


def test_reused_practitioner_keeps_signature_verifiable(app_client, mailer, world):
    c = app_client
    first = _file_once(c, mailer)
    assert first["status"] == "filed" and first["filed"]["signature"]["verified"], first["filed"]["signature"]
    second = _file_once(c, mailer)
    assert second["status"] == "filed"
    assert any(x["action"] == "reused" and x["type"] == "Practitioner" for x in second["filed"]["conditional_creates"])
    assert second["filed"]["signature"]["verified"] and second["filed"]["signature"]["bound"]


def test_duplicates_on_the_server_never_fail_a_filing(app_client, mailer, world):
    c = app_client
    _file_once(c, mailer)
    pr = next(v for k, v in world.resources.items() if k.startswith("Practitioner/")
              and any("bpk" in i["system"] for i in v.get("identifier", [])))           # keyed by its eID hash
    world.resources["Practitioner/99999"] = {**pr, "id": "99999"}          # the HAPI-0958 situation: 2 matches
    second = _file_once(c, mailer)
    assert second["status"] == "filed", second["last_reason"]
    assert second["filed"]["signature"]["verified"]


def test_simulated_identity_never_claims_eidas_loa(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    c.get(link)
    page = c.get("/eid/login", params={"return": f"{link}/eid"}).text
    assert "Anmeldung mit ID Austria (Simulation)" in re.sub(r"<[^>]+>", "", page) and "LoA" not in page
    p = PINS[("Eva", "Brandl")]
    h = _post_login(c, link, given="Eva", family="Brandl", birthdate=p["birthdate"], pin=p["pin"])
    tok = re.search(r'name="assertion" value="([^"]+)"', h.text).group(1)
    cl = _claims(tok)
    assert cl["acr"] == fhirmap.POLICY_EID_SIMULATED and "loa" not in cl and "eidas" not in json.dumps(cl).lower()
    c.post(re.search(r'action="([^"]+)"', h.text).group(1), data={"assertion": tok})
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    prov = next(e["resource"] for e in world.transactions[-1]["entry"] if e["resource"]["resourceType"] == "Provenance")
    stmt = fhirmap.verify_provenance_signature(prov, c.get("/.well-known/jwks.json").json())["claims"]
    assert stmt["acr"] == fhirmap.POLICY_EID_SIMULATED
    everything = json.dumps(prov) + json.dumps(stmt) + json.dumps(c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json())
    assert "LoA/high" not in everything and "LoA high" not in everything and "eidas.europa.eu" not in everything
    assert "Simulation" in json.dumps(world.notifications[-1])


def test_locked_link_notification_is_structured(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    for i in range(5):
        c.cookies.clear()
        login(c, link, "Mallory", f"Test{i}", "1980-01-01")
    n = world.notifications[-1]
    assert n["kind"] == "misdelivery" and n["data"]["locked"] is True and n["data"]["ref_code"]

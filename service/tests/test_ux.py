"""Outside-person and clinician UX: non-phishing messages, what is asked (post-eID only),
'kein passender Befund', upload kept over an expired session, receipt, actionable wrong-eID page, card data."""
import re
from datetime import timedelta

from service.core import iso, now

from .conftest import REQ1, doc
from .test_e2e import HDR, login, send, upload


def _hdr_page(c, link):
    return login(c, link, "Eva", "Brandl")


def test_request_text_only_after_eid(app_client, mailer, world):
    c = app_client
    send(c, {**REQ1, "request_text": "aktuellster Gerinnungsbefund vor geplantem Eingriff"})
    mail = mailer.sent[-1]
    assert "Gerinnung" not in mail["subject"] + mail["body"] and "Eingriff" not in mail["body"]
    link = mailer.last_link()
    assert "Gerinnung" not in c.get(link).text                              # landing (pre-eID): nothing
    page = login(c, link, "Eva", "Brandl").text
    visible = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    assert "Laborbefund Aktuellster Gerinnungsbefund vor geplantem Eingriff" in visible   # heading, then her text
    assert "<dt>Patientin</dt><dd><b>Maria Beispiel</b></dd>" in page and "<dt>Geboren</dt><dd><b>12.03.1961</b></dd>" in page and "· bis " in page and " Uhr" in page
    assert "Dr. Lena Hofer" in page and "+43 1 000 00 00" in page


def test_landing_names_the_requester_without_patient_data(app_client, mailer):
    c = app_client
    send(c, REQ1)
    page = c.get(mailer.last_link()).text
    assert "Dr. Lena Hofer" in page and "Innere Medizin" in page and "Rückfragen +43 1 000 00 00" in page
    assert "Maria" not in page and "1961" not in page and "Labor" not in page


def test_no_result_answer(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    page = _hdr_page(c, link).text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    r = c.post(f"{link}/decline", data={"csrf": csrf, "reason": "letzte Abnahme 14.03.2024"})
    assert r.status_code == 200 and "kein passender Befund" in r.text and "Eingangsbestätigung" in r.text
    st = c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()
    assert st["status"] == "declined"
    t = world.resources[f"Task/{c.svc.store.get_request(req['request_id'])['task_id']}"]
    assert t["status"] == "rejected" and "letzte Abnahme" in t["statusReason"]["text"]
    n = world.notifications[-1]
    assert n["kind"] == "no_result" and n["data"]["comment"] == "letzte Abnahme 14.03.2024"
    assert n["data"]["ref_code"] == req["ref_code"] and n["data"]["patient"]["name"] == "Maria Beispiel"
    steps = [(e["step"], e["outcome"]) for e in c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]]
    assert ("answer sent", "success") in steps
    assert c.get(link).status_code in (404, 410) and not world.transactions


def test_upload_kept_over_expired_session_and_sent_after_relogin(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    page = _hdr_page(c, link).text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    c.svc.store.exec("UPDATE sessions SET expires_at=?", (iso(now() - timedelta(minutes=1)),))     # session expired
    r = c.post(f"{link}/answer", data={"csrf": csrf}, files={"file": ("b.md", doc("lab-result-maria-beispiel.md").encode(),
                                                                       "text/markdown")})
    assert "abgelaufen" in r.text and "zwischengespeichert" in r.text and not world.transactions
    row = c.svc.store.one("SELECT * FROM pending_uploads")
    assert row and row["encrypted"] == 1 and b"Beispiel" not in bytes(row["blob"])
    r = login(c, link, "Eva", "Brandl")                                        # re-login -> submitted automatically
    assert "Eingangsbestätigung" in r.text
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    assert not c.svc.store.q("SELECT * FROM pending_uploads")


def test_kept_upload_is_never_handed_to_another_person(app_client, mailer, world):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    page = _hdr_page(c, link).text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    c.svc.store.exec("UPDATE sessions SET expires_at=?", (iso(now() - timedelta(minutes=1)),))
    c.post(f"{link}/answer", data={"csrf": csrf}, files={"file": ("b.md", doc("lab-result-maria-beispiel.md").encode(),
                                                                   "text/markdown")})
    c.cookies.clear()
    other = c.post("/api/eid/personas", json={"given": "Eva", "family": "Brandl", "birthdate": "1990-02-02"}).json()
    assert login(c, link, "Eva", "Brandl", "1990-02-02", pin=other["pin"]).status_code == 403   # other person: blocked
    assert c.svc.store.q("SELECT * FROM pending_uploads") and not world.transactions


def test_wrong_eid_page_is_not_a_dead_end(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    r = login(c, mailer.last_link(), "Thomas", "Huber")
    assert r.status_code == 403
    assert "Sie sind als <b>Thomas Huber</b> angemeldet" in r.text
    assert "Wenn Sie in Vertretung antworten oder Ihr Name sich geändert hat" in r.text
    assert "+43 1 000 00 00" in r.text and req["ref_code"] in r.text and "Maria" not in r.text


def test_receipt_txt_has_hash_and_no_patient_data(app_client, mailer):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    body = doc("lab-result-maria-beispiel.md")
    r = upload(c, link, body)
    assert "Eingangsbestätigung" in r.text and "an Dr. Lena Hofer übermittelt" in r.text
    t = c.get(f"{link}/receipt.txt")
    import hashlib
    assert t.status_code == 200 and "attachment" in t.headers["content-disposition"]
    assert hashlib.sha256(body.encode()).hexdigest() in t.text and req["ref_code"] in t.text
    assert "Maria" not in t.text and "1961" not in t.text


def test_card_data_for_the_bot(app_client, mailer, world):
    c = app_client
    dry = c.post("/api/requests", json={**REQ1, "dry_run": True}, headers=HDR).json()
    assert dry["patient"] == {"name": "Maria Beispiel", "birthDate": "1961-03-12"}
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md").replace("born 12 March 1961", "born 3 December 1961"))
    h = world.notifications[-1]
    assert h["kind"] == "hold" and h["data"]["ref_code"] == req["ref_code"]
    assert h["data"]["reasons_de"] and "Geburtsdatum" in h["data"]["reason_text_de"]
    assert "✓ Name" in h["data"]["matched"] and h["data"]["patient"]["birthDate"] == "1961-03-12"
    c.post(f"/api/answers/{h['answer_id']}/decision", headers=HDR, json={"action": "file", "reason": "Zahlendreher bestätigt"})
    f = world.notifications[-1]["data"]
    o = f["observations"][0]
    assert o["value_text"] == "1.0" and "comparator" in o and "flag" in o
    assert f["patient"]["name"] == "Maria Beispiel" and f["report_status"] == "final" and f["ref_code"] == req["ref_code"]
    assert f["conclusion"] and "original_url" not in f and f["einsender"] is None   # 'Requested by' == requester -> not shown


def test_windows_export_is_decoded_and_binary_is_rejected_calmly(app_client, mailer, world):
    c = app_client
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    csrf = lambda: re.search(r'name="csrf" value="([0-9a-f]+)"', c.get(f"{link}/answer").text).group(1)
    r = c.post(f"{link}/answer", data={"csrf": csrf()},
               files={"file": ("x.md", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe", "text/markdown")})
    assert r.status_code == 422 and "UTF-8" in r.text and "nicht gespeichert" in r.text
    n = world.notifications[-1]
    assert n["data"]["technical"] and n["text"].startswith("ℹ️")
    cp = doc("lab-result-maria-beispiel.md").replace("Labor Kanalstraße", "Labor Kanalstraße – µ").encode("cp1252")
    r = c.post(f"{link}/answer", data={"csrf": csrf()}, files={"file": ("y.md", cp, "text/markdown")})
    assert r.status_code == 200                                              # old Windows export: decoded, not rejected
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] in ("filed", "held_review")

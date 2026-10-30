"""Photo intake (PGK_EXTRACTOR=anthropic) with a FAKE extractor: no network, the readings are given.

agreement -> FILE (original photo attached and hashed, Provenance KI agent), disagreement -> HOLD naming the field,
'[unleserlich]' -> HOLD, wrong patient in the photo -> BLOCK, flag off -> a photo is refused as today."""
import base64
import hashlib
import re

from .conftest import REQ1, REQ2, doc
from .test_e2e import HDR, login, send

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00synthetic-photo-bytes" * 40
PDF = b"%PDF-1.4\n% synthetic\n" + b"0" * 300
LAB = doc("lab-result-maria-beispiel.md")
MAX = doc("lab-result-max-beispiel.md")
REHAB = doc("rehab-report-maria-beispiel.md")


class FakeExtractor:
    model = "fake-model"

    def __init__(self, a: str | None, b: str | None, error: str | None = None):
        self.reads, self.error, self.calls = [a, b], error, 0

    def transcribe(self, raw: bytes, content_type: str) -> dict:
        self.calls += 1
        return {"model": self.model, "reads": list(self.reads), "error": self.error}


def upload_bytes(c, link, data: bytes, name: str, ctype: str):
    page = c.get(f"{link}/answer")
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    return c.post(f"{link}/answer", data={"csrf": csrf}, files={"file": (name, data, ctype)})


def start(c, mailer, extractor, req=REQ1, who=("Eva", "Brandl")):
    c.svc.extractor = extractor
    r = send(c, req)
    link = mailer.last_link() if req["channel"] == "email" else _sms_link(c)
    login(c, link, *who)
    return r, link


def _sms_link(c):
    sms = c.get("/api/sms").json()[0]["text"]
    return re.search(r"http://svc\.test(/r/[bcdfghjkmnpqrstvwxz]{60})", sms).group(1)


def status(c, req):
    return c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()


def test_agreement_files_the_photo_as_original(app_client, mailer, world):
    c = app_client
    ex = FakeExtractor(LAB, LAB)
    req, link = start(c, mailer, ex)
    page = c.get(f"{link}/answer").text
    assert 'accept="image/*,application/pdf,.md,.txt"' in page
    r = upload_bytes(c, link, PNG, "foto.png", "image/png")
    assert r.status_code == 200 and "erhalten" in r.text
    st = status(c, req)
    assert st["status"] == "filed", st
    tx = world.transactions[-1]
    res = [e["resource"] for e in tx["entry"]]
    assert sum(x["resourceType"] == "Observation" for x in res) == 4
    dr = next(x for x in res if x["resourceType"] == "DiagnosticReport")
    att = dr["presentedForm"][0]
    assert att["contentType"] == "image/png" and base64.b64decode(att["data"]) == PNG     # the photo is the original
    prov = next(x for x in res if x["resourceType"] == "Provenance")
    src = next(e for e in prov["entity"] if e["role"] == "source")
    digest = base64.urlsafe_b64encode(hashlib.sha256(PNG).digest()).decode().rstrip("=")
    assert src["what"]["identifier"]["value"] == f"ni:///sha-256;{digest}"                # hash = the uploaded bytes
    der = next(e for e in prov["entity"] if e["role"] == "derivation")
    md_digest = base64.urlsafe_b64encode(hashlib.sha256(LAB.encode()).digest()).decode().rstrip("=")
    assert der["what"]["identifier"]["value"] == f"ni:///sha-256;{md_digest}"             # the transcription
    ki = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "assembler")
    assert ki["who"]["type"] == "Device"
    assert ki["who"]["display"] == "Befundlink KI-Lesung (fake-model, 2 unabhängige Lesungen)"
    assert any(p.endswith("/ai-transcription-double-read-v1") for p in prov["policy"])
    assert "KI-transkribiert, 2 unabhängige Lesungen" in ki["role"][0]["text"]
    assert all("coding" in x for x in prov["reason"])                     # reason stays coded (TREAT)
    # receipt and audit carry the photo's own hash
    receipt = c.get(f"{link}/receipt.txt").text
    assert hashlib.sha256(PNG).hexdigest() in receipt
    sent = [e for e in c.get("/api/audit", params={"request_id": req["request_id"]}, headers=HDR).json()["entries"]
            if e["step"] == "answer sent"]
    assert sent and hashlib.sha256(PNG).hexdigest() in sent[-1]["what"]
    note = world.notifications[-1]
    assert note["kind"] == "filed" and note["data"]["photo_note"] == "📷 aus Foto gelesen (KI, 2 Lesungen übereinstimmend)"
    assert "4/4 Werte unverändert aus der Lesung übernommen" in note["text"]
    assert ex.calls == 2                       # intake + the worker's re-check (the real extractor caches the pair)


def test_rehab_pdf_agreement_files_documentreference_with_pdf(app_client, mailer, world):
    c = app_client
    req, link = start(c, mailer, FakeExtractor(REHAB, REHAB), REQ2, ("Stefan", "Kern"))
    upload_bytes(c, link, PDF, "bericht.pdf", "application/pdf")
    assert status(c, req)["status"] == "filed"
    d = next(e["resource"] for e in world.transactions[-1]["entry"] if e["resource"]["resourceType"] == "DocumentReference")
    att = d["content"][0]["attachment"]
    assert att["contentType"] == "application/pdf" and base64.b64decode(att["data"]) == PDF


def test_disagreement_holds_and_names_the_field(app_client, mailer, world):
    c = app_client
    misread = LAB.replace("| INR (prothrombin time) | 1.0 |", "| INR (prothrombin time) | 1.8 |")
    req, link = start(c, mailer, FakeExtractor(LAB, misread))
    upload_bytes(c, link, PNG, "foto.jpg", "image/jpeg")
    st = status(c, req)
    assert st["status"] == "held_review" and not world.transactions
    n = world.notifications[-1]
    assert "KI-Lesungen weichen ab: INR (prothrombin time) 1.0 vs 1.8 – bitte am Original prüfen" in n["text"]
    assert n["text"].count("1.8") == 1                       # the field is named once, not repeated in a table
    from bot import fmt
    card = fmt.notify_message(n)[0]
    assert "KI-Lesungen weichen ab: INR (prothrombin time) 1.0 vs 1.8" in card and card.count("1.8") == 1
    assert "✓ Name" not in card and "✓ Geburtsdatum" not in card   # nothing about the patient was agreed


def test_disagreement_filed_on_decision_files_only_the_original(app_client, mailer, world):
    """'!ablegen' on an EXTRACTION hold never files a value one reading may have misread: only the photo."""
    c = app_client
    misread = LAB.replace("| 1.0 |", "| 1.8 |")
    req, link = start(c, mailer, FakeExtractor(LAB, misread))
    upload_bytes(c, link, PNG, "foto.png", "image/png")
    r = c.post(f"/api/requests/{req['ref_code']}/decision", headers=HDR,
               json={"decision": "ablegen", "reason": "Original geprüft", "by": "@lena.hofer:klinikum.example"})
    assert r.status_code == 200
    res = [e["resource"] for e in world.transactions[-1]["entry"]]
    assert not any(x["resourceType"] == "Observation" for x in res)
    d = next(x for x in res if x["resourceType"] == "DocumentReference")
    assert d["content"][0]["attachment"]["contentType"] == "image/png"


def test_unreadable_holds(app_client, mailer, world):
    c = app_client
    smudged = LAB.replace("| aPTT | 31 |", "| aPTT | [unleserlich] |")
    req, link = start(c, mailer, FakeExtractor(smudged, smudged))
    upload_bytes(c, link, PNG, "foto.png", "image/png")
    assert status(c, req)["status"] == "held_review" and not world.transactions
    assert "KI-Lesung: aPTT unleserlich – bitte am Original prüfen" in world.notifications[-1]["text"]


def test_failed_reading_holds(app_client, mailer, world):
    c = app_client
    req, link = start(c, mailer, FakeExtractor(LAB, None, error="HTTP 529 overloaded_error"))
    upload_bytes(c, link, PNG, "foto.png", "image/png")
    assert status(c, req)["status"] == "held_review" and not world.transactions
    assert "KI-Lesung nicht möglich – bitte am Original prüfen" in world.notifications[-1]["text"]


def test_wrong_patient_in_the_photo_blocks(app_client, mailer, world):
    c = app_client
    req, link = start(c, mailer, FakeExtractor(MAX, MAX))
    r = upload_bytes(c, link, PNG, "foto.png", "image/png")
    assert r.status_code == 422 and "Dieses Dokument betrifft nicht Maria Beispiel" in r.text and "Max" not in r.text
    assert not world.transactions
    n = world.notifications[-1]["text"]
    assert "FEHLZUSTELLUNG" in n and "Max" not in n
    row = c.svc.store.one("SELECT content, outcome FROM answers WHERE request_id=?", (req["request_id"],))
    assert row["content"] is None and row["outcome"] == "blocked_wrong_patient"


def test_wrong_patient_in_one_reading_blocks_and_names_no_one(app_client, mailer, world):
    c = app_client
    req, link = start(c, mailer, FakeExtractor(LAB, MAX))
    r = upload_bytes(c, link, PNG, "foto.png", "image/png")
    assert r.status_code == 422 and not world.transactions
    assert "Max" not in world.notifications[-1]["text"]


def test_text_upload_unchanged_with_flag_on(app_client, mailer, world):
    c = app_client
    ex = FakeExtractor(None, None)
    req, link = start(c, mailer, ex)
    upload_bytes(c, link, LAB.encode(), "lab.md", "text/markdown")
    assert status(c, req)["status"] == "filed" and ex.calls == 0      # Markdown never goes to the model
    att = next(e["resource"] for e in world.transactions[-1]["entry"]
               if e["resource"]["resourceType"] == "DiagnosticReport")["presentedForm"][0]
    assert att["contentType"].startswith("text/markdown")
    assert "photo_note" not in world.notifications[-1]["data"]


def test_flag_off_refuses_a_photo_as_today(app_client, mailer, world):
    c = app_client
    assert c.svc.extractor is None                                     # off by default
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    page = c.get(f"{link}/answer").text
    assert "image/*" not in page and 'accept=".md,.markdown,.txt,text/markdown,text/plain"' in page
    r = upload_bytes(c, link, PNG, "foto.png", "image/png")
    assert r.status_code == 422 and "Bitte eine .md- oder .txt-Datei hochladen." in r.text
    r = upload_bytes(c, link, PNG, "foto.md", "text/markdown")         # renamed: the bytes are still refused
    assert r.status_code == 422 and not world.transactions
    assert status(c, req)["status"] != "filed"

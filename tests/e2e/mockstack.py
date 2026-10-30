"""In-process mock of the whole stack (service, bot, Synapse, HAPI, Mailpit) on ports 18xxx.

Purpose: dry-run the e2e harness and metrics while the real components are being built. It
implements the service contract plausibly (eID mock, Task, one transaction, chained
AuditEvents, Vienna offsets) and uses refcheck as its decision engine. It is NOT the product.

    uv run python tests/e2e/mockstack.py        # serve until Ctrl-C (ports 18000/18001/18008/18025/18090)
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

from jury import refcheck  # noqa: E402
from jury.model import PATIENT, UCUM_EXPECT, Ctx, Person, vienna  # noqa: E402
from pgktest.audit import fhir_event_hash  # noqa: E402
from pgktest.norm import identity_matches, parse_dob  # noqa: E402

VIENNA = ZoneInfo("Europe/Vienna")
PORTS = {"service": 18000, "bot": 18001, "synapse": 18008, "mailpit": 18025, "hapi": 18090}
STEP_DICOM = {"request-sent": ("110110", "Patient Record"), "link-opened": ("110110", "Patient Record"),
              "identity-checked": ("110114", "User Authentication"), "answer-sent": ("110110", "Patient Record"),
              "answer-filed": ("110107", "Import"), "misdelivery-caught": ("110113", "Security Alert")}


def now() -> str:
    return datetime.now(VIENNA).isoformat(timespec="seconds")


class State:
    def __init__(self):
        import os
        import sqlite3
        self.lock = threading.RLock()
        self.data = TESTS / "out" / "mock" / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data, 0o700)
        dbp = self.data / "pgk.sqlite"
        dbp.unlink(missing_ok=True)
        self.db = sqlite3.connect(str(dbp), check_same_thread=False, isolation_level=None)
        self.db.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, status TEXT, request_text TEXT)")
        os.chmod(dbp, 0o600)
        self.fhir: dict[str, dict[str, list[dict]]] = {}  # type -> id -> versions
        self.mail: list[dict] = []
        self.sms: list[dict] = []
        self.events: list[dict] = []
        self.requests: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.sessions: dict[str, tuple[str, dict]] = {}
        self.audit_rows: list[dict] = []
        self.last_ae: dict | None = None
        self.last_ae_hash: str | None = None
        self.known: dict[str, set] = {}       # recipient key -> eID subjects that answered (namesake memory)
        self.receipts: dict[str, str] = {}    # session -> SHA-256 of the last upload
        self.pending_bot: dict | None = None
        self.room = "!mockroom:klinikum.example"
        self.token = "mock-lena-token"
        self.fhir_put({"resourceType": "Patient", "id": PATIENT["id"], "name": [{"family": "Beispiel", "given": ["Maria"]}],
                       "birthDate": PATIENT["birthDate"], "gender": "female"})
        self.fhir_put({"resourceType": "Patient", "id": "max-beispiel", "name": [{"family": "Beispiel", "given": ["Max"]}],
                       "birthDate": "1960-01-01", "gender": "male"})  # a second Patient, as on the real stack
        self.demo_names: set[str] = set()  # PGK_DEMO: recipient names that already got a demo persona

    # ---------------- FHIR store
    def fhir_put(self, r: dict, stamp: str | None = None) -> dict:
        with self.lock:
            r = json.loads(json.dumps(r))
            r.setdefault("id", uuid.uuid4().hex[:16])
            vs = self.fhir.setdefault(r["resourceType"], {}).setdefault(r["id"], [])
            r["meta"] = {"versionId": str(len(vs) + 1), "lastUpdated": stamp or datetime.now(VIENNA).isoformat()}
            vs.append(r)
            return r

    def transaction(self, resources: list[dict]) -> list[str]:
        with self.lock:
            stamp = datetime.now(VIENNA).isoformat()
            ids = {f"urn:uuid:{r.pop('_urn')}": f"{r['resourceType']}/{r.setdefault('id', uuid.uuid4().hex[:16])}"
                   for r in resources if "_urn" in r}
            txt = json.dumps(resources)
            for k, v in ids.items():
                txt = txt.replace(k, v)
            return [f"{r['resourceType']}/{self.fhir_put(r, stamp)['id']}" for r in json.loads(txt)]

    def current(self, rtype: str) -> list[dict]:
        return [vs[-1] for vs in self.fhir.get(rtype, {}).values()]

    # ---------------- audit (local rows + chained FHIR AuditEvents)
    def audit(self, rid: str | None, step: str, ok: bool, who: str, what: str, outcome: str | None = None,
              detail: dict | None = None) -> None:
        with self.lock:
            code, disp = STEP_DICOM[step]
            r = self.requests.get(rid) or {}
            ae = {"resourceType": "AuditEvent",
                  "type": {"system": "http://dicom.nema.org/resources/ontology/DCM", "code": code, "display": disp},
                  "subtype": [{"system": "urn:pgk-bridge:audit-step", "code": step}],
                  "action": "E", "recorded": now(), "outcome": outcome or ("0" if ok else "4"), "outcomeDesc": what,
                  "purposeOfEvent": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
                                                  "code": "TREAT"}]}],
                  "agent": [{"requestor": True, "who": {"display": who}}],
                  "source": {"observer": {"display": "pgk-bridge mock"}},
                  "entity": [{"what": {"reference": f"Task/{r.get('task_id')}"}}] if r.get("task_id") else []}
            chain = [{"type": "prev_hash", "valueString": self.last_ae_hash}] if self.last_ae_hash else []
            ae["entity"].append({"what": {"display": "audit chain"}, "type": {
                "system": "http://terminology.hl7.org/CodeSystem/audit-entity-type", "code": "2"}, "detail": chain})
            own = fhir_event_hash(ae)
            ae["entity"][-1]["detail"] = chain + [{"type": "hash", "valueString": own}]
            self.last_ae_hash = own
            self.last_ae = self.fhir_put(ae)
            self.audit_rows.append({"request_id": rid, "step": step, "outcome": "ok" if ok else "fail", "who": who,
                                    "what": what, "at": ae["recorded"], "fhir_id": self.last_ae["id"],
                                    "detail": detail or {}})

    def say(self, sender: str, body: str) -> None:
        with self.lock:
            self.events.append({"event_id": f"${uuid.uuid4().hex}", "sender": sender, "type": "m.room.message",
                                "origin_server_ts": int(time.time() * 1000), "content": {"msgtype": "m.text", "body": body}})


S = State()


# ------------------------------------------------------------------ service logic
REQUESTER = "@lena.hofer:klinikum.example"
SID_BPK = "https://pgk-bridge.example/fhir/sid/bpk-hmac-sha256"


def sub_of(eid: dict) -> str:
    return hashlib.sha256(f"{eid['given_name']}|{eid['family_name']}|{eid['birthdate']}".encode()).hexdigest()[:32]


def rkey(r: dict) -> str:
    return " ".join(sorted(r["recipient"]["name"].casefold().replace("dr.", "").split())) + "|" + \
        (r["recipient"].get("org") or "").casefold()


def who_ok(h, r: dict | None = None) -> bool:
    """X-PGK-Actor / X-PGK-Room: the request's requester, in its room (as the service enforces)."""
    a, room = h.headers.get("X-PGK-Actor"), h.headers.get("X-PGK-Room")
    return bool(a and room) and a == (r or {}).get("requester_mxid", REQUESTER) and room == S.room


def create_request(body: dict) -> tuple[int, dict]:
    note = body.get("note") or ""
    if re.search(r"maria|beispiel|1961|12\.03", note, re.I):
        return 422, {"detail": "note contains patient data"}
    addr = body["address"]
    warnings = []
    if body["channel"] == "email":
        dom = addr.split("@")[-1]
        if dom != dom.lower() or (dom.lower() != "labor-kanal.example" and dom.lower().replace("i", "l") == "labor-kanal.example"):
            warnings.append({"code": "confusable", "detail": f"{dom} looks like labor-kanal.example"})
    if body.get("_dry"):
        return 200, {"warnings": warnings}
    rid = "req-" + uuid.uuid4().hex[:10]
    r = {"requester_mxid": "@lena.hofer:klinikum.example", **body, "id": rid,
         "status": "needs_confirmation" if warnings else "new", "warnings": warnings}
    S.requests[rid] = r
    name = body["recipient"]["name"]
    if name not in S.demo_names:  # PGK_DEMO: a persona for a new recipient name; the audit line names nobody
        S.demo_names.add(name)
        S.audit_rows.append({"request_id": rid, "step": "Demo-Identität angelegt", "outcome": "success",
                             "who": "pgk-bridge", "what": "Simulierte ID-Austria-Persona angelegt (PIN in der Antwort).",
                             "at": now(), "detail": {}})
        r["demo_identity"] = {"given": name.split()[-2] if len(name.split()) > 1 else name, "family": name.split()[-1],
                              "birthdate": "1970-01-01", "pin": "000000"}
    with S.lock:
        S.db.execute("INSERT INTO requests VALUES (?,?,?)", (rid, r["status"], body.get("request_text")))
    if not warnings or body.get("confirm"):
        send(rid)
    return 201, {"request_id": rid, "status": r["status"], "warnings": warnings,
                 **({"demo_identity": r["demo_identity"]} if r.get("demo_identity") else {})}


def send(rid: str) -> None:
    r = S.requests[rid]
    tok = secrets.token_urlsafe(16)
    S.tokens[tok] = rid
    link = f"http://localhost:{PORTS['service']}/r/{tok}"
    task = S.fhir_put({"resourceType": "Task", "status": "requested", "intent": "order",
                       "identifier": [{"system": "urn:pgk-bridge:request", "value": rid}],
                       "for": {"reference": f"Patient/{PATIENT['id']}"},
                       "requester": {"display": "Dr. Lena Hofer"}, "owner": {"display": r["recipient"]["name"]},
                       "authoredOn": now()})
    r |= {"status": "sent", "task_id": task["id"], "token": tok}
    if r["channel"] == "email":
        S.mail.append({"ID": uuid.uuid4().hex[:12], "To": [{"Address": r["address"]}], "Created": now(),
                       "Subject": "Klinikum Beispielstadt: Bitte um Rückmeldung",
                       "Text": f"Guten Tag,\n\nKlinikum Beispielstadt bittet Sie um eine Rückmeldung.\nSicherer Link: {link}\n"})
    else:
        S.sms.append({"to": r["address"], "text": f"Klinikum Beispielstadt bittet um Rückmeldung: {link}"})
    S.audit(rid, "request-sent", True, "Dr. Lena Hofer", f"request sent by {r['channel']}")
    S.audit_rows[-1] |= {"contact": r["address"],  # cleartext beside the chain; only a keyed hash in detail
                         "detail": {"channel": r["channel"],
                                    "contact_hash": hashlib.sha256(("k" + r["address"]).encode()).hexdigest()}}


def _current_task(rid: str) -> dict:
    tid = S.requests[rid]["task_id"]
    return S.fhir["Task"][tid][-1]



def build(text: str, raw: bytes, rid: str, eid: dict) -> list[dict]:
    """Mini fhirmap: enough to exercise every fidelity check."""
    r = S.requests[rid]
    pref = f"Patient/{PATIENT['id']}"
    att = {"contentType": "text/markdown", "data": base64.b64encode(raw).decode(),
           "hash": base64.b64encode(hashlib.sha1(raw).digest()).decode()}
    out, main_urn = [], uuid.uuid4().hex

    def line(*keys):
        m = re.search(rf"^(?:{'|'.join(keys)})\s*:\s*(.+)$", text, re.I | re.M)
        return m.group(1).strip() if m else None
    if r["doc_kind"] == "lab_result":
        lab_t = [t for t in refcheck.tables(text.split("\n")) if refcheck._col(t[2], "result", "ergebnis") is not None]
        _, _, head, rows = lab_t[0]
        col = lambda *k: refcheck._col(head, *k)
        ct, cv, cu, cr, cl, cf = col("test", "parameter"), col("result", "ergebnis"), col("unit", "einheit"), \
            col("reference", "referenz"), col("loinc"), col("flag")
        if cf is None and "" in head:
            cf = head.index("")  # unlabelled flag column (+/-)
        eff = vienna(line("Sample taken", "Probenahme"))
        obs_refs, pending = [], False
        for row in rows:
            g = lambda i: row[i].strip() if i is not None and i < len(row) else ""
            val = g(cv).replace("**", "").replace(",", ".")
            if refcheck.PENDING.match(val) or refcheck.NOT_DETERMINABLE.match(val):
                o = {"resourceType": "Observation", "_urn": uuid.uuid4().hex,
                     "status": "registered" if refcheck.PENDING.match(val) else "final",
                     "code": {"text": g(ct), **({"coding": [{"system": "http://loinc.org", "code": g(cl)}]} if g(cl) else {})},
                     "subject": {"reference": pref}, "effectiveDateTime": eff, "note": [{"text": g(cv)}]}
                if not refcheck.PENDING.match(val):
                    o["dataAbsentReason"] = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                                                         "code": "error"}]}
                ref = g(cr).replace(",", ".").replace("–", "to")
                if mm := re.fullmatch(r"([\d.]+)\s*(?:to|-)\s*([\d.]+)", ref):  # the unit lives in the range
                    o["referenceRange"] = [{"low": {"value": float(mm.group(1)), "unit": g(cu)},
                                            "high": {"value": float(mm.group(2)), "unit": g(cu)}}]
                pending = pending or bool(refcheck.PENDING.match(val))
                obs_refs.append(f"urn:uuid:{o['_urn']}")
                out.append(o)
                continue
            o = {"resourceType": "Observation", "_urn": uuid.uuid4().hex, "status": "final",
                 "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                           "code": "laboratory"}]}],
                 "code": {"text": g(ct), **({"coding": [{"system": "http://loinc.org", "code": g(cl)}]} if g(cl) else {})},
                 "subject": {"reference": pref}, "effectiveDateTime": eff}
            m = re.fullmatch(r"([<>]=?)?\s*([\d.]+)", val)
            if m:
                q = {"value": float(m.group(2))}
                if m.group(1):
                    q["comparator"] = m.group(1)
                if g(cu):
                    q |= {"unit": g(cu), "system": "http://unitsofmeasure.org", "code": sorted(UCUM_EXPECT[g(cu)])[0]}
                o["valueQuantity"] = q
            else:
                o["valueString"] = val
            ref = g(cr).replace(",", ".").replace("–", "to")
            if mm := re.fullmatch(r"([\d.]+)\s*(?:to|-)\s*([\d.]+)", ref):
                o["referenceRange"] = [{"low": {"value": float(mm.group(1))}, "high": {"value": float(mm.group(2))}}]
            elif mm := re.fullmatch(r"<=?\s*([\d.]+)", ref):
                o["referenceRange"] = [{"high": {"value": float(mm.group(1))}}]
            elif mm := re.fullmatch(r">=?\s*([\d.]+)", ref):
                o["referenceRange"] = [{"low": {"value": float(mm.group(1))}}]
            fl = refcheck.FLAG_TOKEN.get(g(cf).casefold()) if g(cf) else None
            if fl:
                o["interpretation"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                                    "code": fl}], "text": g(cf)}]
            obs_refs.append(f"urn:uuid:{o['_urn']}")
            out.append(o)
        after = text.split("\n\n")
        concl = next((p.strip() for p in after if p.strip() and not p.lstrip().startswith(("|", "#"))
                      and not re.match(r"(patient|requested|sample|reported|validated|validiert|angefordert|probenahme|befundet|sent)",
                                       p.strip(), re.I) and "\n|" not in p and text.index(p) > text.index("|---")), None)
        tl = (text.split("\n", 1)[0]).casefold()
        status = ("corrected" if re.search(r"korrig|korrektur|corrected", tl) else "amended" if "nachtrag" in tl
                  else "preliminary" if pending or re.search(r"vorl[aä]ufig|vorbefund|teilbefund", tl) else "final")
        dr = {"resourceType": "DiagnosticReport", "_urn": main_urn, "status": status,
              "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB"}]}],
              "code": {"coding": [{"system": "http://loinc.org", "code": "11502-2"}]}, "subject": {"reference": pref},
              "effectiveDateTime": eff, "issued": vienna(line("Reported", "Befundet")), "result": [{"reference": u} for u in obs_refs],
              "presentedForm": [att]}
        if concl:
            dr["conclusion"] = concl
        out.insert(0, dr)
        author = line("Validated by", "Validiert von")
    else:
        stay = line("Stay", "Aufenthalt") or ""
        m = re.search(r"(\d+)\.? (?:to|bis) (\d+)\.? (\w+) (\d{4})", stay)
        per = {}
        if m:
            mon = parse_dob(f"1 {m.group(3)} {m.group(4)}")[:8]
            per = {"start": f"{mon}{int(m.group(1)):02d}", "end": f"{mon}{int(m.group(2)):02d}"}
        out.append({"resourceType": "DocumentReference", "_urn": main_urn, "status": "current",
                    "type": {"coding": [{"system": "http://loinc.org", "code": "11490-0"}]},
                    "category": [{"coding": [{"system": "http://loinc.org", "code": "18842-5"}]}],
                    "subject": {"reference": pref}, "date": now(), "content": [{"attachment": att}],
                    **({"context": {"period": per}} if per else {})})
        author = line("Written by", "Verfasst von")
    targets = [{"reference": f"urn:uuid:{x['_urn']}"} for x in out]
    sender = {"resourceType": "Practitioner", "_urn": uuid.uuid4().hex,  # keyed ONLY by the eID's bPK hash
              "identifier": [{"system": SID_BPK, "value": sub_of(eid)}],
              "name": [{"text": f"{eid['given_name']} {eid['family_name']}"}]}
    out.append(sender)
    ptype = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
    agents = [{"type": {"coding": [{"system": ptype, "code": "informant"}]},
               "who": {"reference": f"urn:uuid:{sender['_urn']}", "display": f"{eid['given_name']} {eid['family_name']}"}}]
    if author:
        signatory = {"resourceType": "Practitioner", "_urn": uuid.uuid4().hex, "name": [{"text": author.split(",")[0]}]}
        out.append(signatory)
        agents.append({"type": {"coding": [{"system": ptype, "code": "author"}]},
                       "who": {"reference": f"urn:uuid:{signatory['_urn']}", "display": author.split(",")[0]}})
    out.append({"resourceType": "Provenance", "_urn": uuid.uuid4().hex, "target": targets, "recorded": now(),
                "policy": ["https://pgk-bridge.example/fhir/policy/eid-simulated"], "agent": agents,
                "entity": [{"role": "source", "what": {"identifier": {"system": "urn:ietf:rfc:3986",
                                                                       "value": "urn:sha256:" + hashlib.sha256(raw).hexdigest()}}}],
                "signature": [{"type": [{"system": "urn:iso-astm:E1762-95:2013", "code": "1.2.840.10065.1.12.1.5"}],
                               "when": now(), "who": {"display": "mock-id-austria"}, "sigFormat": "application/jose",
                               "data": base64.b64encode(_jws({**eid, "content_sha256": hashlib.sha256(raw).hexdigest(),
                                                              "targets": [t["reference"] for t in targets]}).encode()).decode()}]})
    task = dict(_current_task(rid))
    task |= {"status": "completed", "output": [{"type": {"text": "report"}, "valueReference": {"reference": f"urn:uuid:{main_urn}"}}]}
    out.append(task)
    return out


def _jws(claims: dict) -> str:
    b = lambda x: base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip("=")
    return f"{b({'alg': 'none', 'typ': 'JWT'})}.{b(claims)}.mock"


def page(title: str, body: str, status: int = 200) -> tuple[int, str, str]:
    return status, "text/html; charset=utf-8", f"<!doctype html><html><head><title>{html.escape(title)}</title></head><body>{body}</body></html>"


def service(h: "H", method: str, path: str, q: dict, body: bytes) -> tuple[int, str, str | bytes]:
    if path == "/" and method == "GET":
        return 200, "text/plain", "ok"
    if path == "/api/requests" and method == "POST":
        b = json.loads(body)
        if not (h.headers.get("X-PGK-Actor") and h.headers.get("X-PGK-Room")) \
                or h.headers["X-PGK-Actor"] != b.get("requester_mxid"):
            return 403, "application/json", json.dumps({"detail": "X-PGK-Actor (the requester) and X-PGK-Room required"})
        if b.get("dry_run"):
            st, js = create_request({**b, "_dry": True})
            return 200, "application/json", json.dumps({"status": "dry_run", "warnings": js.get("warnings", [])})
        st, js = create_request(b)
        return st, "application/json", json.dumps(js)
    if m := re.fullmatch(r"/api/requests/([\w-]+)/confirm", path):
        r = S.requests.get(m.group(1))
        if not who_ok(h, r):
            return 403, "application/json", json.dumps({"detail": "X-PGK-Actor and X-PGK-Room are required"})
        ack = set((json.loads(body or b"{}") or {}).get("acknowledged_warnings") or [])
        missing = {w["code"] for w in r.get("warnings") or []} - ack
        if missing:
            return 409, "application/json", json.dumps({"detail": f"acknowledged_warnings missing: {sorted(missing)}"})
        send(m.group(1))
        return 200, "application/json", json.dumps({"request_id": m.group(1), "status": "sent"})
    if m := re.fullmatch(r"/api/requests/([\w-]+)/decision", path):
        r = S.requests.get(m.group(1))
        if not r:
            return 404, "application/json", "{}"
        actor, room = h.headers.get("X-PGK-Actor"), h.headers.get("X-PGK-Room")
        if not who_ok(h, r):
            return 403, "application/json", json.dumps({"detail": "not your request"})
        js = json.loads(body or b"{}")
        if js.get("action") == "confirm_identity" and r.get("pending_sub"):
            r["approved_sub"] = r.pop("pending_sub")
            S.audit(r["id"], "answer-sent", True, h.headers.get("X-PGK-Actor"), "decision: identity confirmed")
            S.audit_rows[-1]["step"] = "decision"
            return 200, "application/json", json.dumps({"request_id": r["id"], "status": "ok"})
        if js.get("action") == "file" and r.get("held"):
            text, raw, eid = r.pop("held")
            S.audit(r["id"], "answer-sent", True, actor, f"decision: file ({js.get('reason')})")
            S.audit_rows[-1]["step"] = "decision"
            refs = S.transaction(build(text, raw, r["id"], eid))
            S.audit(r["id"], "answer-filed", True, "pgk-bridge", f"filed {len(refs)} resources after decision")
            with S.lock:
                S.db.execute("UPDATE requests SET status='filed', request_text=NULL WHERE id=?", (r["id"],))
        return 200, "application/json", json.dumps({"request_id": r["id"], "status": "ok"})
    if path == "/api/audit":
        if q.get("request_id"):
            if not who_ok(h, S.requests.get(q["request_id"][0])):
                return 403, "application/json", json.dumps({"detail": "not your request"})
        elif h.headers.get("X-PGK-Actor") or h.headers.get("X-PGK-Room"):
            return 403, "application/json", json.dumps({"detail": "the full audit trail is for operators only"})
        rows = [r for r in S.audit_rows if not q.get("request_id") or r["request_id"] == q["request_id"][0]]
        return 200, "application/json", json.dumps(rows)
    if path == "/sms":
        items = "".join(f"<li><b>An: {html.escape(s['to'])}</b><p>{html.escape(s['text'])}</p></li>" for s in S.sms)
        return page("SMS", f"<ul>{items}</ul>")
    if path == "/eid/login":
        if method == "GET":
            ret = html.escape(q.get("return", [""])[0])
            return page("ID Austria (Simulation)", f"""<form method="post" action="/eid/login">
              <input type="hidden" name="return" value="{ret}">
              <label for="g">Vorname</label><input id="g" name="given_name" required>
              <label for="f">Nachname</label><input id="f" name="family_name" required>
              <label for="b">Geburtsdatum</label><input id="b" type="date" name="birthdate" required>
              <button type="submit">Mit ID Austria bestätigen</button></form>""")
        f = urllib.parse.parse_qs(body.decode())
        claims = {k: f.get(k, [""])[0] for k in ("given_name", "family_name", "birthdate")}
        a = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
        return 303, "redirect", f"{f['return'][0]}?assertion={a}"
    if (m := re.fullmatch(r"/r/([\w-]+)/receipt.txt", path)) and h.cookie("sid") in S.receipts:
        return 200, "text/plain; charset=utf-8", f"Eingangsbestätigung\nSHA-256: {S.receipts[h.cookie('sid')]}\n"
    m = re.fullmatch(r"/r/([\w-]+)(/eid|/answer)?", path)
    if not m or m.group(1) not in S.tokens:
        return page("Nicht gefunden", "<p>Link ungültig.</p>", 404)
    tok, sub = m.group(1), m.group(2)
    rid = S.tokens[tok]
    r = S.requests[rid]
    if sub is None:
        if method == "GET":
            S.audit(rid, "link-opened", True, "anonymous", "link opened")
        return page("Rückmeldung", f"""<p>Klinikum Beispielstadt bittet Sie um eine Rückmeldung.</p>
          <form method="get" action="/eid/login"><input type="hidden" name="return" value="/r/{tok}/eid">
          <button type="submit">Mit ID Austria anmelden</button></form>""")
    if sub == "/eid":
        eid = json.loads(base64.urlsafe_b64decode(q["assertion"][0]))
        who = f"{eid['given_name']} {eid['family_name']}"
        if not identity_matches(r["recipient"]["name"], eid["given_name"], eid["family_name"], eid["birthdate"],
                                (PATIENT["name"], PATIENT["birthDate"])):
            S.audit(rid, "identity-checked", False, who, f"eID does not match the named recipient")
            S.audit(rid, "misdelivery-caught", False, who, "answer attempt by someone else blocked", "8")
            S.say("@bridge-bot:klinikum.example", f"⚠️ Antwortversuch von {who} (nicht {r['recipient']['name']}) blockiert.")
            return page("Nicht berechtigt", f"<p>Diese Anfrage ist für {html.escape(r['recipient']['name'])}. "
                                            "Es wurde nichts angezeigt und nichts abgelegt.</p>")
        sub = sub_of(eid)
        known = S.known.get(rkey(r), set())
        if known and sub not in known and r.get("approved_sub") != sub:  # a namesake: review BEFORE reveal
            r["pending_sub"] = sub
            S.audit(rid, "identity-checked", False, who, "name matches, but another eID than before: review",
                    detail={"relation": "review", "bpk_hash": sub})
            S.say("@bridge-bot:klinikum.example", f"Neue ID-Austria-Identität für {r['recipient']['name']} – freigeben?")
            return page("Bestätigung nötig", "<p>Die anfragende Ärztin muss diese Anmeldung bestätigen. "
                                             "Es wurde nichts angezeigt.</p>")
        S.audit(rid, "identity-checked", True, who, "eID matches the named recipient (LoA high)",
                detail={"relation": "bound", "bpk_hash": sub})
        sid = secrets.token_hex(16)
        S.sessions[sid] = (rid, eid)
        return 303, f"redirect;sid={sid}", f"/r/{tok}/answer"
    sess = S.sessions.get(h.cookie("sid") or "")
    if not sess or sess[0] != rid:
        return page("Anmeldung nötig", "<p>Bitte zuerst anmelden.</p>", 403)
    if method == "GET":
        kind = "Laborbefund" if r["doc_kind"] == "lab_result" else "Entlassungsbericht"
        return page("Anfrage", f"""<p>Dr. Lena Hofer bittet um: {kind} für Maria Beispiel (geb. 12.03.1961).</p>
          <form method="post" enctype="multipart/form-data" action="/r/{tok}/answer">
          <label for="d">Datei</label><input id="d" type="file" name="file">
          <label for="t">oder Text einfügen</label><textarea id="t" name="text"></textarea>
          <button type="submit">Senden</button></form>""")
    msg = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {h.headers['Content-Type']}\r\n\r\n".encode() + body)
    raw = b""
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "file":
            raw = part.get_payload(decode=True) or b""
    eid = sess[1]
    who = f"{eid['given_name']} {eid['family_name']}"
    sha = hashlib.sha256(raw).hexdigest()
    S.receipts[h.cookie("sid")] = sha
    S.audit(rid, "answer-sent", True, who, f"answer uploaded ({len(raw)} bytes, SHA-256 {sha})", detail={"sha256": sha})
    ctx = Ctx("x", r["recipient"]["name"], "", r["recipient"].get("org", ""), r["channel"], r["address"], r["doc_kind"], "")
    outcome, reasons = refcheck.check(raw, ctx, Person(eid["given_name"], eid["family_name"], eid["birthdate"]))
    if outcome == "FILE":
        refs = S.transaction(build(refcheck.normalise(refcheck.decode(raw)[0]).replace("\r\n", "\n"), raw, rid, eid))
        S.known.setdefault(rkey(r), set()).add(sub_of(eid))
        S.audit(rid, "answer-filed", True, "pgk-bridge", f"filed {len(refs)} resources in one transaction")
        with S.lock:
            S.db.execute("UPDATE requests SET status='filed', request_text=NULL WHERE id=?", (rid,))
        S.say("@bridge-bot:klinikum.example", f"✅ Antwort von {who} abgelegt ({len(refs)} Ressourcen).")
        return page("Danke", "<p>Danke, Ihre Antwort wurde abgelegt.</p>")
    if outcome == "HOLD":
        S.fhir_put({**_current_task(rid), "status": "on-hold", "statusReason": {"text": reasons[0]}})
        r["held"] = (refcheck.normalise(refcheck.decode(raw)[0] or "").replace("\r\n", "\n"), raw, eid)
        S.audit(rid, "answer-sent", True, "pgk-bridge", f"held for decision: {reasons[0]}")
        S.say("@bridge-bot:klinikum.example", f"❓ Bitte prüfen: Antwort von {who} zurückgehalten ({reasons[0]}). Entscheidung nötig.")
        return page("Danke", "<p>Danke. Die Antwort wird von der anfragenden Ärztin geprüft.</p>")
    S.audit(rid, "misdelivery-caught", False, "pgk-bridge", f"not filed: {reasons[0]}", "8")
    S.say("@bridge-bot:klinikum.example", f"⛔ Nicht abgelegt: {reasons[0]}. Das Dokument nennt einen anderen Patienten "
                                          "oder ist unzulässig; nichts wurde gespeichert.")
    return page("Nicht angenommen", f"<p>Nicht angenommen ({html.escape(reasons[0])}).</p>",
                413 if reasons[0] == "OVERSIZE" else 422)


# ------------------------------------------------------------------ other roles
def hapi(method, path, q, body):
    path = path.removeprefix("/fhir")
    if path == "/metadata":
        return 200, "application/fhir+json", json.dumps({"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"})
    parts = [p for p in path.split("/") if p]
    if len(parts) == 1:
        rs = S.current(parts[0])
        if q.get("identifier"):
            v = q["identifier"][0].split("|")[-1]
            rs = [x for x in rs if any(i.get("value") == v for i in x.get("identifier") or [])]
        if q.get("_summary") == ["count"]:
            return 200, "application/fhir+json", json.dumps({"resourceType": "Bundle", "total": len(rs)})
        off, n = int(q.get("_offset", ["0"])[0]), int(q.get("_count", ["500"])[0])
        b = {"resourceType": "Bundle", "type": "searchset", "total": len(rs), "entry": [{"resource": x} for x in rs[off:off + n]]}
        if off + n < len(rs):
            b["link"] = [{"relation": "next", "url": f"http://localhost:{PORTS['hapi']}/fhir/{parts[0]}?_count={n}&_offset={off + n}"}]
        return 200, "application/fhir+json", json.dumps(b)
    vs = S.fhir.get(parts[0], {}).get(parts[1])
    if not vs:
        return 404, "application/fhir+json", "{}"
    if len(parts) == 3 and parts[2] == "_history":
        return 200, "application/fhir+json", json.dumps({"resourceType": "Bundle", "entry": [{"resource": v} for v in reversed(vs)]})
    return 200, "application/fhir+json", json.dumps(vs[-1])


def mailpit(method, path, q, body):
    path = path.removeprefix("/api/v1")
    if path == "/info":
        return 200, "application/json", json.dumps({"Version": "mock"})
    if path == "/messages":
        return 200, "application/json", json.dumps({"messages": S.mail, "messages_count": len(S.mail)})
    if path == "/search":
        m = re.search(r'to:"([^"]+)"', q.get("query", [""])[0])
        hits = [x for x in S.mail if m and any(t["Address"].casefold() == m.group(1).casefold() for t in x["To"])]
        return 200, "application/json", json.dumps({"messages": hits})
    if path == "/send" and method == "POST":
        js = json.loads(body)
        S.mail.append({"ID": uuid.uuid4().hex[:12], "To": [{"Address": t["Email"]} for t in js["To"]], "Created": now(),
                       "Subject": js.get("Subject", ""), "Text": js.get("Text", "")})
        return 200, "application/json", "{}"
    if m := re.fullmatch(r"/message/(\w+)", path):
        msg = next((x for x in S.mail if x["ID"] == m.group(1)), None)
        return (200, "application/json", json.dumps(msg)) if msg else (404, "application/json", "{}")
    return 404, "application/json", "{}"


def bot_command(body: str) -> None:
    if body.strip() in ("!ja", "!yes") and S.pending_bot:
        send(S.pending_bot["request_id"])
        S.pending_bot = None
        S.say("@bridge-bot:klinikum.example", "✅ Anfrage gesendet.")
        return
    m = re.match(r"!anfordern (labor|reha) (.+)", body.strip())
    if not m:
        return
    name, role, org, addr, pat = [x.strip() for x in m.group(2).split(",")]
    st, js = create_request({"recipient": {"name": name, "role": role, "org": org}, "address": addr,
                             "channel": "email" if "@" in addr else "sms", "patient_id": pat.split()[-1],
                             "doc_kind": "lab_result" if m.group(1) == "labor" else "discharge_report",
                             "requester_mxid": "@lena.hofer:klinikum.example"})
    if js.get("warnings"):
        S.pending_bot = js
        S.say("@bridge-bot:klinikum.example", f"⚠️ Warnung: {js['warnings'][0]['detail']}. !ja = trotzdem senden · !nein = abbrechen")
    else:
        S.say("@bridge-bot:klinikum.example", "✅ Anfrage gesendet.")


def synapse(method, path, q, body):
    if path == "/health":
        return 200, "text/plain", "OK"
    if "/send/m.room.message/" in path and method == "PUT":
        b = json.loads(body)["body"]
        S.say("@lena.hofer:klinikum.example", b)
        threading.Thread(target=bot_command, args=(b,), daemon=True).start()
        return 200, "application/json", json.dumps({"event_id": S.events[-1]["event_id"]})
    if path.endswith("/messages"):
        n = int(q.get("limit", ["100"])[0])
        return 200, "application/json", json.dumps({"chunk": list(reversed(S.events))[:n]})
    return 404, "application/json", "{}"


def bot(method, path, q, body):
    if path == "/notify" and method == "POST":
        js = json.loads(body)
        S.say("@bridge-bot:klinikum.example", js["text"])
        return 200, "application/json", "{}"
    return 200, "text/plain", "ok"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def cookie(self, name: str) -> str | None:
        m = re.search(rf"(?:^|;\s*){name}=([^;]+)", self.headers.get("Cookie", ""))
        return m.group(1) if m else None

    def _do(self, method: str):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        role = self.server.role
        try:
            if role == "service":
                st, ct, out = service(self, method, u.path, q, body)
            else:
                st, ct, out = {"hapi": hapi, "mailpit": mailpit, "synapse": synapse, "bot": bot}[role](method, u.path, q, body)
        except Exception as e:  # surface mock bugs as 500, like a real server
            st, ct, out = 500, "text/plain", f"{type(e).__name__}: {e}"
        hdr = {}
        if ct.startswith("redirect"):
            hdr["Location"] = out
            if ";sid=" in ct:
                hdr["Set-Cookie"] = f"sid={ct.split('=')[1]}; Path=/; HttpOnly"
            ct, out = "text/plain", ""
        data = out.encode() if isinstance(out, str) else out
        self.send_response(st)
        for k, v in {"Content-Type": ct, "Content-Length": str(len(data)), **hdr}.items():
            self.send_header(k, v)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        self._do("GET")

    def do_HEAD(self):
        self._do("HEAD")

    def do_POST(self):
        self._do("POST")

    def do_PUT(self):
        self._do("PUT")


class Server(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True


class MockStack:
    def __init__(self):
        self.servers = []
        self.room_id, self.lena_token = S.room, S.token

    def start(self) -> "MockStack":
        for role, port in PORTS.items():
            srv = Server(("127.0.0.1", port), H)
            srv.role = role
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers.append(srv)
        return self

    def stop(self) -> None:
        for s in self.servers:
            s.shutdown()

    def env(self) -> dict[str, str]:
        return {"PGK_BASE_URL": f"http://localhost:{PORTS['service']}", "PGK_HAPI_URL": f"http://localhost:{PORTS['hapi']}/fhir",
                "PGK_MAILPIT_API": f"http://localhost:{PORTS['mailpit']}/api/v1", "PGK_SYNAPSE": f"http://localhost:{PORTS['synapse']}",
                "PGK_BOT_URL": f"http://localhost:{PORTS['bot']}", "PGK_ROOM_ID": S.room, "PGK_LENA_TOKEN": S.token,
                "PGK_DATA_DIR": str(TESTS / "out" / "mock" / "data")}


if __name__ == "__main__":
    MockStack().start()
    print("mock stack on", PORTS)
    threading.Event().wait()

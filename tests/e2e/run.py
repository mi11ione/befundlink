"""End-to-end runs against the live stack, as the outside person would do them (HTTP only).

    uv run python tests/e2e/run.py                      # everything, live stack (default ports)
    uv run python tests/e2e/run.py --mock               # dry-run against tests/e2e/mockstack.py
    uv run python tests/e2e/run.py --only scenario,misdelivery
    uv run python tests/e2e/run.py --corpus /tmp/fuzz   # any generated corpus (generate.py --fuzz)

Phases: scenario (request 1 by email, request 2 by SMS, both started in Matrix via the bot),
misdelivery (M1, M2 + "colleague first, then Brandl", M3, scanner prefetch, leaky note),
corpus (every jury document through the answer flow, in parallel). Writes tests/out/e2e.json.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import base64
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

from jury import refcheck  # noqa: E402
from jury.model import CTX, EID, PATIENT  # noqa: E402
from pgktest import audit as A  # noqa: E402
from pgktest import pii  # noqa: E402
from pgktest.norm import fold  # noqa: E402
from pgktest.outside import OutsidePerson, Person, Visit, klm_seconds  # noqa: E402
from pgktest.stack import Hapi, Mailpit, Matrix, Service, Stack, body_of, links_in, now_ms, sms_links_for  # noqa: E402
from pgktest.web import HttpError, poll  # noqa: E402

OUT = TESTS / "out"
CORPUS = TESTS / "jury" / "corpus"
HOLD_RX = re.compile(r"\bon-hold\b|\bhold\b|\bheld\b|zurückgehalten|angehalten|zur prüfung|entscheidung|decision|"
                     r"review|rückfrage|bitte prüfen|needs[ _-]review", re.I)
BLOCK_RX = re.compile(r"misdeliver|fehlzustell|blocked|blockiert|abgelehnt|rejected|refused|nicht abgelegt|not filed|"
                      r"wrong[ _-]patient|falsche[rn]? patient|mismatch|eid[ _-]mismatch|security", re.I)
CMD = {"lab_result": "labor", "discharge_report": "reha"}
HELD_CODES = {"filing-held", "request-held", "held", "on-hold"}
REQUEST_TEXT = {  # what Dr. Hofer asks for (document-type words on purpose: the PII scan catches a leak)
    "lab_result": "Bitte um die aktuellen Gerinnungswerte (INR, aPTT) vor dem geplanten Eingriff.",
    "discharge_report": "Bitte um den Reha-Entlassungsbericht nach Hüft-TEP links.",
}


def answer_blob(rows: list[dict]) -> str:
    """Text of the audit rows about the answer itself (request/link/identity rows can mention 'geprüft' etc.)."""
    keep = [r for r in rows if (not A.steps_of(r) & {"request-sent", "link-opened", "identity-checked"}
                                or A.steps_of(r) & {"answer-sent", "answer-filed", "misdelivery-caught"})
            and not any("demo" in c for c in A.codes_of(r))]  # PGK_DEMO 'Demo-Identität angelegt' is no outcome
    return json.dumps(keep, ensure_ascii=False)


def _pins() -> dict[str, str]:
    """Simulated-eID PINs per persona: PGK_EID_PINS = JSON or a JSON file path; default the service's
    demo file service/data/demo_pins.json (keys = persona keys, plus unique names)."""
    raw = os.environ.get("PGK_EID_PINS") or str(TESTS.parent / "service" / "data" / "demo_pins.json")
    try:
        return json.loads(Path(raw).read_text() if raw and Path(raw).exists() else raw or "{}")
    except (ValueError, OSError):
        return {}


PINS = _pins()
_PIN_LOCK = __import__("threading").Lock()
SERVICE: Service | None = None  # set by Run: enrols personas that the PIN file does not know


def person(key: str) -> Person:
    """By persona KEY only, else enrol: names are shared (the patient and her daughter, Dr. Brandl and her
    namesake), and a PIN found by name would log in a different person."""
    p = EID[key]
    from pgktest.stack import DEMO_PINS
    pin = PINS.get(key) or DEMO_PINS.get((p.given, p.family, p.birthDate))
    if pin is None and SERVICE is not None:
        with _PIN_LOCK:
            pin = PINS.get(key)
            if pin is None:
                try:
                    pin = PINS[key] = SERVICE.enrol(p.given, p.family, p.birthDate)
                except Exception:  # mock stack / older service: no PINs
                    pin = PINS[key] = ""
    return Person(p.given, p.family, p.birthDate, pin=pin or None)


def visit_json(v: Visit) -> dict:
    return {"actions": v.actions, "answer_form_seen": v.answer_form_seen, "submitted": v.submitted,
            "error": v.error, "pages": [{"url": p.url, "status": p.status, "text": p.text[:4000],
                                        "inputs": [x.type for f in p.forms for x in f.fields],
                                        "links": [h for h, _ in p.links]} for p in v.pages],
            "result_status": v.result.status if v.result else None,
            "result_text": v.result.text[:1500] if v.result else None,
            "machine_s": round(v.t.get("done", v.t.get("eid_done", 0)) - v.t.get("open", 0), 3) if v.t else None,
            "raw_sha256": v.raw_sha256, "receipt": (v.receipt or "")[:2000] or None,
            "klm": klm_seconds(v)}


class Run:
    def __init__(self, st: Stack, workers: int, verbose: bool):
        self.st, self.workers, self.verbose = st, workers, verbose
        self.svc, self.mp, self.hapi = Service(st), Mailpit(st.mailpit), Hapi(st.hapi)
        global SERVICE
        SERVICE = self.svc
        self.mx = Matrix(st.synapse, st.lena_token, st.room_id) if st.lena_token and st.room_id else None
        self.t0_ms = now_ms()
        self.out: dict = {"started_ms": self.t0_ms, "stack": {"service": st.service, "hapi": st.hapi,
                                                              "mailpit": st.mailpit, "synapse": st.synapse},
                          "scenario": {}, "misdelivery": {}, "corpus": [], "notes": []}
        self.nonce = base64.b32encode(os.urandom(5)).decode().lower()[:6]
        self.bot_up = bool(self.mx) and st.up().get("bot", False)

    def log(self, *a):
        print(*a, flush=True)

    # ---------------------------------------------------------------- requests
    def via_bot(self, ctx, address: str | None = None) -> dict | None:
        """Dr. Hofer types the command in her room; the bot asks to confirm on warnings; returns bot evidence."""
        if not self.mx or not self.st.up().get("bot"):
            return None
        addr = address or ctx.address
        ts = now_ms()
        self.mx.send(f"!anfordern {CMD[ctx.doc_kind]} {ctx.name}, {ctx.role}, {ctx.org}, {addr}, Patient {PATIENT['id']}")
        msgs = []
        sent = re.compile(r"gesendet|versendet|verschickt|\bsent\b", re.I)
        e = self.mx.wait(ts, lambda b: sent.search(b) or "!ja" in b or "warn" in b.casefold(), timeout=30)
        msgs.append(body_of(e))
        if not sent.search(msgs[-1]) or "!ja" in msgs[-1]:
            ts2 = now_ms()
            self.mx.send("!ja")
            e = self.mx.wait(ts2, lambda b: bool(sent.search(b)), timeout=30)
            msgs.append(body_of(e))
        rid = next((m.group(0) for b in reversed(msgs) for m in [re.search(r"\breq-[0-9a-f]{6,}\b", b)] if m), None)
        return {"bot_messages": msgs, "request_id": rid,
                "warned": any(re.search(r"⚠|warn|achtung|ähnlich|sieht aus wie|look-?alike|verwechs|confus", m, re.I)
                              for m in msgs)}

    def request_id_since(self, ts_ms: int, needle: str) -> str | None:
        """Fallback when the bot reply carries no id: the newest 'request sent' row whose contact (cleartext,
        outside the hashed part; the hashed part only has detail.contact_hash) is this address."""
        try:
            rows = self.svc.audit_all()
        except Exception:
            return None
        for r in reversed(rows):
            if "request-sent" in A.steps_of(r) and fold(str(r.get("contact") or "")) == fold(needle):
                return r.get("request_id")
        return None

    def link_after(self, channel: str, address: str, before, timeout: float = 25) -> tuple[str, dict]:
        if channel == "email":
            m = self.mp.wait_new(address, before, timeout)
            links = links_in(m.get("Text", ""), m.get("HTML", ""))
            if not links:
                raise AssertionError(f"no /r/ link in mail to {address}")
            return links[0], {"mailpit_id": m["ID"], "subject": m.get("Subject"), "text": m.get("Text", ""),
                              "html": m.get("HTML", "")}
        new = poll(lambda: [u for u in sms_links_for(self.svc.sms_page(), address) if u not in before],
                   timeout, what=f"SMS to {address}")
        return new[-1], {"sms_to": address}

    def snapshot(self, channel: str, address: str):
        if channel == "email":
            return {m["ID"] for m in self.mp.to(address)}
        try:
            return set(sms_links_for(self.svc.sms_page(), address))
        except Exception:
            return set()

    def open_request(self, ctx, *, address: str | None = None, bot: bool = False, note: str | None = None,
                     request_text: str | None = None) -> dict:
        """Create a request (bot or API) and fetch the link from Mailpit / the SMS page."""
        addr = address or ctx.address
        with self.svc.lock(addr):
            before = self.snapshot(ctx.channel, addr)
            ts = now_ms()
            rec: dict = {"address": addr, "channel": ctx.channel}
            botev = self.via_bot(ctx, addr) if bot else None
            if botev:
                rec |= {"via": "bot", **botev}
            else:
                body = {**ctx.body(), "address": addr}
                if note:
                    body["note"] = note
                if request_text:  # shown only after the eID; must never reach the message, cleared after filing
                    body["request_text"] = request_text
                resp = self.svc.create(body)
                rec |= {"via": "api", "api": {k: v for k, v in resp.items() if k not in ("link",)},
                        "request_id": resp.get("request_id"), "warnings": resp.get("warnings") or []}
            link, msg = self.link_after(ctx.channel, addr, before)
        rec |= {"link": link, "message": msg}
        rec.setdefault("request_id", None)
        if not rec["request_id"]:
            rec["request_id"] = self.request_id_since(ts, addr)
        return rec

    # ---------------------------------------------------------------- outcome
    def observe(self, rid: str | None, v: Visit, timeout: float = 15) -> dict:
        """Wait for the service to settle, then classify from audit + result page (Task added later)."""
        # rows from this attempt only: after the upload if there was one, else after opening (second resolution)
        since = float(int(v.t["send_wall"])) if "send_wall" in v.t else v.t.get("open_wall", 0) - 1.0

        def mine(rows):  # only what happened during THIS visit (a request can see several attempts)
            out = []
            for r in rows:
                try:
                    if datetime.fromisoformat(str(r.get("at") or r.get("recorded"))).timestamp() >= since:
                        out.append(r)
                except ValueError:
                    out.append(r)
            return out

        def settled():
            rows = mine(self.svc.audit(rid)) if rid else []
            st = set().union(*(A.steps_of(r) for r in rows)) if rows else set()
            if not v.answer_form_seen and "identity-checked" in st:
                return rows or True
            held = any(A.codes_of(r) & HELD_CODES for r in rows)
            return rows if st & {"answer-filed", "misdelivery-caught"} or held or HOLD_RX.search(answer_blob(rows)) else None
        rows = []
        if not rid:
            return {"audit": [], "steps": []}
        try:
            got = poll(settled, timeout, what=f"settled {rid}")
            rows = got if isinstance(got, list) else []
        except TimeoutError:
            rows = mine(self.svc.audit(rid)) if rid else []
        return {"audit": rows, "steps": sorted(set().union(*(A.steps_of(r) for r in rows))) if rows else []}

    @staticmethod
    def classify(audit_rows: list[dict], task: dict | None, v: Visit) -> tuple[str, str]:
        def ok(r):
            return str(r.get("outcome", "0")).lower() in ("0", "ok", "success", "true", "none")
        filed = any("answer-filed" in A.steps_of(r) and ok(r) for r in audit_rows)
        ts = (task or {}).get("status")
        blob = answer_blob(audit_rows) + " " + (v.result.text if v.result else "")
        misdelivery = any("misdelivery-caught" in A.steps_of(r) for r in audit_rows)
        refused = v.result is not None and 400 <= v.result.status < 500
        if v.result is not None and v.result.status >= 500 and not filed:
            return "ERROR", f"HTTP {v.result.status} on submit: a server error is not a decision"
        if filed or ts == "completed":
            return "FILE", f"filed audit={filed} task={ts}"
        if ts == "on-hold" or any(A.codes_of(r) & HELD_CODES for r in audit_rows):
            return "HOLD", f"task={ts} held={[r.get('step') for r in audit_rows if A.codes_of(r) & HELD_CODES]}"
        if ts in ("rejected", "failed") or misdelivery:
            return "BLOCK", f"task={ts} misdelivery={misdelivery}"
        if HOLD_RX.search(blob):
            return "HOLD", f"hold wording, task={ts}"
        if BLOCK_RX.search(blob) or refused or not v.answer_form_seen:
            return "BLOCK", f"task={ts} refused={refused} answer_form={v.answer_form_seen}"
        return "UNKNOWN", f"task={ts} steps={[sorted(A.steps_of(r)) for r in audit_rows]}"

    # ---------------------------------------------------------------- phases
    def answer(self, rec: dict, who: Person, data: bytes | None, filename: str = "befund.md",
               prefetch: int = 0) -> Visit:
        for _ in range(prefetch):  # mail scanner (Safe Links): GET + HEAD without cookies, no eID
            OutsidePerson(who).b.get(rec["link"])
            OutsidePerson(who).b.head(rec["link"])
        op = OutsidePerson(who)
        v = op.open(rec["link"])
        op.login(v)
        if data is not None and v.answer_form_seen:
            op.answer(v, data, filename)
        return v

    def scenario(self) -> None:
        docs = CORPUS / "_scenario"
        for key, ctx, eid, fname in [("request1", CTX["brandl"], "brandl", "lab-result-maria-beispiel.md"),
                                     ("request2", CTX["kern"], "kern", "rehab-report-maria-beispiel.md")]:
            r = {"ok": False}
            try:
                ts = now_ms()
                rec = self.open_request(ctx, bot=True)
                v = self.answer(rec, person(eid), (docs / fname).read_bytes(), fname)
                obs = self.observe(rec["request_id"], v)
                filed = self.task_filed(rec["request_id"], wait=15)
                outcome, why = self.classify(obs["audit"], self.svc.task(rec["request_id"]), v)
                note = self.wait_matrix(ts, r"abgelegt|filed|gespeichert", 20)
                r = {**rec, "visit": visit_json(v), **obs, "outcome": outcome, "why": why, "matrix": note,
                     "ok": outcome == "FILE" and filed, "task_filed": filed, "expect": "FILE", "case": "A01" if key == "request1" else "R01"}
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"
                if self.verbose:
                    traceback.print_exc()
            self.out["scenario"][key] = r
            self.log(f"  {key}: {r.get('outcome', 'ERROR')} via={r.get('via')} {r.get('error', '')}")

    def wait_matrix(self, ts: int, pattern: str, timeout: float = 20) -> dict | None:
        if not self.mx:
            return None
        try:
            e = self.mx.wait(ts, lambda b: re.search(pattern, b, re.I) is not None, timeout if self.bot_up else 2)
            return {"body": body_of(e), "sender": e.get("sender"), "ts": e.get("origin_server_ts")}
        except TimeoutError:
            return None

    def task_filed(self, rid: str | None, wait: float = 0) -> bool:
        """Per-request evidence (shared HAPI: global counts move with other users): Task completed/output."""
        def f():
            t = self.svc.task(rid) if rid else None
            return bool(t and (t.get("status") == "completed" or t.get("output")))
        if wait:
            try:
                return poll(f, wait, what="filed")
            except TimeoutError:
                return False
        return f()

    def count_reports(self) -> int:
        return self.hapi.count("DiagnosticReport") + self.hapi.count("DocumentReference")

    def misdelivery(self) -> None:
        M = self.out["misdelivery"]
        brandl, docs = CTX["brandl"], CORPUS / "_scenario"
        a01 = (docs / "lab-result-maria-beispiel.md").read_bytes()
        max_doc = (docs / "lab-result-max-beispiel.md").read_bytes()

        def run(name, fn):
            try:
                M[name] = fn()
            except Exception as e:
                M[name] = {"caught": False, "error": f"{type(e).__name__}: {e}"}
                if self.verbose:
                    traceback.print_exc()
            self.log(f"  {name}: caught={M[name].get('caught')} {M[name].get('error', '')}")

        def m1():  # wrong address with a capital I; someone else opens it
            ts = now_ms()
            rec = self.open_request(brandl, address="eva.brandl@labor-kanaI.example", bot=True)
            ts = now_ms()
            v = self.answer(rec, person("fremd"), a01)
            obs = self.observe(rec["request_id"], v)
            pre = v.landing.text if v.landing else ""
            post = v.after_eid.text if v.after_eid else ""
            leak = [f[1:3] for f in pii.violations(pii.scan(pre + " " + post, "M1 pages", PATIENT, orgs=[brandl.org]))]
            failed_id = [r for r in obs["audit"] if "identity-checked" in A.steps_of(r)
                         and str(r.get("outcome", "0")).lower() not in ("0", "ok", "success", "true")]
            warned = bool(rec.get("warnings")) or bool(rec.get("warned"))
            caught = warned and not leak and not v.submitted and bool(failed_id) and not self.task_filed(rec["request_id"])
            return {"caught": caught, "warned_at_send": warned, "warnings": rec.get("warnings"),
                    "bot": rec.get("bot_messages"), "request_id": rec["request_id"], "page_leaks": leak,
                    "could_answer": v.answer_form_seen, "failed_identity_audit": failed_id[:3], "audit": obs["audit"],
                    "visit": visit_json(v), "matrix": self.wait_matrix(ts, r"Fehlzustellung|misdeliver|Petra Fremd|versucht von|attempt by", 10),
                    "via": rec.get("via")}

        def m2():  # forwarded to a colleague; then the real Dr. Brandl answers (link stays valid)
            rec = self.open_request(brandl, bot=True)
            msg = rec["message"]
            fwd = "kollege@labor-kanal.example"
            before = {m["ID"] for m in self.mp.to(fwd)}
            self.mp.send(fwd, f"WG: {msg.get('subject', '')}", "---------- Weitergeleitete Nachricht ----------\n"
                         + (msg.get("text") or ""))
            fm = self.mp.wait_new(fwd, before)
            link = links_in(fm.get("Text", ""), fm.get("HTML", ""))[0]
            ts_attempt = now_ms()
            v = self.answer({**rec, "link": link}, person("kollege"), a01)
            colleague_filed = self.task_filed(rec["request_id"])
            obs = self.observe(rec["request_id"], v)
            alert = self.wait_matrix(ts_attempt, r"Fehlzustellung|misdeliver|Thomas Huber|versucht von|attempt by", 15)
            v2 = self.answer(rec, person("brandl"), a01)
            obs2 = self.observe(rec["request_id"], v2)
            brandl_filed = self.task_filed(rec["request_id"], wait=15)
            o2, _ = self.classify(obs2["audit"], self.svc.task(rec["request_id"]), v2)
            colleague_blocked = not colleague_filed and not v.answer_form_seen
            return {"caught": colleague_blocked and alert is not None and bool(obs["audit"]),
                    "colleague_filed": colleague_filed,
                    "colleague_could_answer": v.answer_form_seen, "matrix_alert": alert, "audit": obs["audit"],
                    "request_id": rec["request_id"], "visit": visit_json(v),
                    "then_brandl": {"outcome": o2, "ok": o2 == "FILE" and brandl_filed, "task_filed": brandl_filed,
                                    "visit": visit_json(v2)}, "via": rec.get("via")}

        def m3():  # Dr. Brandl answers Maria's request with Max Beispiel's result
            ts = now_ms()
            rec = self.open_request(brandl, bot=True)
            ts = now_ms()
            v = self.answer(rec, person("brandl"), max_doc, "lab-result-max-beispiel.md")
            obs = self.observe(rec["request_id"], v)
            alert = self.wait_matrix(ts, r"Fehlzustellung|misdeliver|nicht abgelegt|not filed|andere Person|"
                                         r"different patient|stimmt nicht", 15)
            filed = self.task_filed(rec["request_id"], wait=3)
            leak = []
            for where, txt in [("matrix", (alert or {}).get("body", "")), ("audit", json.dumps(obs["audit"], ensure_ascii=False))]:
                for needle in ("Max Beispiel", "Max", "1960", "01.01.1960", "1 January 1960", "phenprocoumon", "2.6"):
                    if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", txt, re.I):
                        leak.append((where, needle))
            return {"caught": alert is not None and not filed and any("misdelivery-caught" in A.steps_of(r) for r in obs["audit"]),
                    "filed": filed, "matrix_alert": alert, "other_patient_leaks": leak, "audit": obs["audit"],
                    "request_id": rec["request_id"], "visit": visit_json(v), "via": rec.get("via")}

        def prefetch():  # Safe-Links style scanner GETs twice before the real person
            rec = self.open_request(brandl)
            v = self.answer(rec, person("brandl"), a01, prefetch=2)
            obs = self.observe(rec["request_id"], v)
            filed = self.task_filed(rec["request_id"], wait=15)
            o, _ = self.classify(obs["audit"], self.svc.task(rec["request_id"]), v)
            return {"caught": o == "FILE" and filed, "outcome": o, "task_filed": filed, "request_id": rec["request_id"], "visit": visit_json(v)}

        def note():  # Dr. Hofer's note is a leak vector
            n = "Bitte INR für Maria Beispiel, geb. 12.03.1961, vor der OP am Montag."
            try:
                rec = self.open_request(brandl, note=n)
            except (HttpError, TimeoutError) as e:
                return {"caught": True, "refused": True, "detail": str(e)[:300]}
            txt = json.dumps(rec["message"], ensure_ascii=False)
            leaked = [w for w in ("Maria", "Beispiel", "12.03.1961", "INR", "OP") if re.search(rf"\b{re.escape(w)}\b", txt)]
            return {"caught": not leaked, "refused": False, "leaked": leaked, "request_id": rec["request_id"],
                    "message": rec["message"]}

        def hold_decision():  # near-miss -> HOLD; only Dr. Hofer, in her room, may decide; then it files
            # 'Marie Beispiel' (one-letter typo) with its own Sample ID: this copy is filed on purpose by the decision,
            # so it must not collide with the corpus canary of H03 (which must never be filed)
            doc = re.sub(rb"Sample ID: \S+", b"Sample ID: 26-HOLD-DECISION", (CORPUS / "H03.md").read_bytes())
            rec = self.open_request(brandl)
            rid = rec["request_id"]
            v = self.answer(rec, person("brandl"), doc, "H03.md")
            self.observe(rid, v)
            held = poll(lambda: (self.svc.task(rid) or {}).get("status") == "on-hold", 30, what="on-hold")
            forged, _ = self.svc.decide(rid, "file", "forged", actor="@mallory:klinikum.example")
            nohdr, _ = self.svc.decide(rid, "file", "no headers", actor=None, room="")
            ok_st, _ = self.svc.decide(rid, "file", "Tippfehler im Namen telefonisch mit dem Labor geklärt.")
            filed = self.task_filed(rid, wait=60)
            rows = self.svc.audit(rid)
            audited = any(A.codes_of(r) & {"decision"} and "lena.hofer" in json.dumps(r) for r in rows)
            return {"caught": held and forged in (401, 403) and nohdr in (401, 403) and ok_st < 300 and filed and audited,
                    "held": held, "forged_actor_status": forged, "no_headers_status": nohdr, "accepted_status": ok_st,
                    "filed": filed, "decision_audited": audited, "request_id": rid}

        def namesake():  # another Eva Brandl (1955) answers a request to the known Dr. Eva Brandl (1979)
            doc = re.sub(rb"Sample ID: \S+", b"Sample ID: 26-NAMESAKE", (CORPUS / "A02.md").read_bytes())
            rec = self.open_request(brandl)  # exact recipient + org: the 1979 eID is known from request 1
            rid, ns = rec["request_id"], person("brandl_namesake")
            v = self.answer(rec, ns, doc, "A02.md")
            rows = self.svc.audit(rid)
            review = any("identity-checked" in A.steps_of(r) and (r.get("detail") or {}).get("relation") == "review"
                         for r in rows)
            nothing_shown = not v.answer_form_seen and not pii.violations(pii.scan(
                " ".join(p.text for p in v.pages), "namesake pages", PATIENT, doc_type=False))
            st, _ = self.svc.decide(rid, "confirm_identity", "Telefonisch bestätigt: andere Person gleichen Namens.")
            v2 = self.answer(rec, ns, doc, "A02.md")
            filed = self.task_filed(rid, wait=60)
            return {"caught": review and nothing_shown and st < 300 and v2.answer_form_seen and filed,
                    "review_before_reveal": review, "nothing_shown": nothing_shown, "confirm_status": st,
                    "answered_after_confirm": v2.answer_form_seen, "filed": filed, "request_id": rid,
                    "visit": visit_json(v2)}

        for name, fn in [("M1", m1), ("M2", m2), ("M3", m3), ("prefetch", prefetch), ("note", note),
                         ("hold_decision", hold_decision), ("namesake", namesake)]:
            run(name, fn)

    def corpus(self, cdir: Path) -> None:
        man = json.loads((cdir / "manifest.json").read_text())
        cases = man["cases"]
        n0 = self.count_reports()

        def one(c: dict) -> dict:
            ctx = CTX[c["ctx"]]
            addr = ctx.address
            if ctx.channel == "email":  # plus-addressing keeps runs independent and parallel
                local, dom = addr.split("@")
                addr = f"{local}+{self.nonce}{c['id'].lower()}@{dom}"
            else:
                addr = f"+43 699 {abs(hash((self.nonce, c['id']))) % 10**7:07d}"
            r = {"id": c["id"], "expect": c["expect"], "reasons": c["reasons"], "title": c["title"]}
            t = time.monotonic()
            try:
                rec = self.open_request(ctx, address=addr, request_text=REQUEST_TEXT[ctx.doc_kind])
                data = (cdir / c["file"]).read_bytes()
                v = self.answer(rec, person(c["eid"]), data, f"{c['id']}.md")
                obs = self.observe(rec["request_id"], v)
                r |= {"request_id": rec["request_id"], "link": rec["link"], "address": addr, **obs,
                      "visit": visit_json(v), "_visit": v}
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"
                if self.verbose:
                    traceback.print_exc()
            r["seconds"] = round(time.monotonic() - t, 2)
            return r

        t = time.monotonic()
        with ThreadPoolExecutor(self.workers) as ex:
            res = list(ex.map(one, cases))
        # settle: filing is asynchronous; re-poll every case that has no terminal step yet (in parallel)
        pending = [r for r in res if "_visit" in r and not set(r.get("steps") or []) &
                   {"answer-filed", "misdelivery-caught"} and not any(A.codes_of(x) & HELD_CODES for x in r.get("audit") or [])
                   and r["_visit"].answer_form_seen]
        if pending:
            with ThreadPoolExecutor(self.workers) as ex:
                for r, obs in zip(pending, ex.map(lambda r: self.observe(r["request_id"], r["_visit"], 120), pending)):
                    r |= obs
        self.out["corpus_settled_late"] = [r["id"] for r in pending]
        self.out["corpus_seconds"] = round(time.monotonic() - t, 1)
        self.out["corpus_reports_delta"] = self.count_reports() - n0
        self.out["corpus"] = res
        self.out["corpus_dir"] = str(cdir)

    # ---------------------------------------------------------------- final evidence
    def finish(self) -> None:
        dump = self.hapi.dump()
        tasks = dump.get("Task", [])
        rid_task = {}
        for t in tasks:
            blob = json.dumps(t)
            for r in self._all_results():
                rid = r.get("request_id")
                if rid and rid in blob:
                    rid_task.setdefault(rid, t)
        for r in self._all_results():
            v = r.pop("_visit", None)
            if "outcome" not in r and v is not None:
                task = rid_task.get(r.get("request_id"))
                r["task"] = {"id": task.get("id"), "status": task.get("status")} if task else None
                r["outcome"], r["why"] = self.classify(r.get("audit") or [], task, v)
            elif "outcome" in r and r.get("request_id") in rid_task:
                r["task"] = {"id": rid_task[r["request_id"]]["id"], "status": rid_task[r["request_id"]].get("status")}
        self.out["matrix"] = [{"sender": e.get("sender"), "ts": e.get("origin_server_ts"), "body": body_of(e),
                               "html": (e.get("content") or {}).get("formatted_body") or ""}
                              for e in (self.mx.since(self.t0_ms, 1000) if self.mx else [])]
        try:
            self.out["audit_api"] = self.svc.audit()
        except Exception as e:
            self.out["audit_api"], self.out["notes"] = [], self.out["notes"] + [f"/api/audit: {e}"]
        from pgktest.web import latency_summary
        self.out["latency"] = latency_summary()
        self.out["finished_ms"] = now_ms()

    def _all_results(self):
        yield from self.out["corpus"]
        yield from (x for x in self.out["scenario"].values() if isinstance(x, dict))
        yield from (x for x in self.out["misdelivery"].values() if isinstance(x, dict))


def other_patient(doc: bytes) -> tuple[list[str], list[str]]:
    """Names and DOBs of people in the document who are NOT the requested patient (same rules as refcheck)."""
    text = refcheck.decode(doc)[0]
    if not text:
        return [], []
    names, dobs = [], []
    for n, iso in refcheck.persons(text):
        n = " ".join(t for t in (n or "").split() if not re.search(r"befund|bericht|report|labor", t, re.I))
        if n and fold(n) != fold(PATIENT["name"]) and len(n) < 80:
            names.append(n)
        if iso and iso != PATIENT["birthDate"]:
            dobs.append(iso)
    return names, dobs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="start tests/e2e/mockstack.py and run against it")
    ap.add_argument("--only", default="scenario,misdelivery,corpus")
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    mock = None
    if a.mock:
        from e2e.mockstack import MockStack
        mock = MockStack().start()
        os.environ.update(mock.env())
    st = Stack()
    if mock:
        st.state = {"room_id": mock.room_id, "tokens": {"lena": mock.lena_token}}
    up = st.up()
    print("stack:", " ".join(f"{k}={'up' if v else 'DOWN'}" for k, v in up.items()))
    missing = [k for k in ("service", "hapi", "mailpit") if not up[k]]
    if missing:
        print(f"cannot run: {', '.join(missing)} not reachable", file=sys.stderr)
        return 2
    run = Run(st, a.workers, a.verbose)
    run.out["mock"], run.out["up"] = bool(mock), up
    t = time.monotonic()
    phases = a.only.split(",")
    if "scenario" in phases:
        print("scenario"), run.scenario()
    if "misdelivery" in phases:
        print("misdelivery"), run.misdelivery()
    if "corpus" in phases:
        print(f"corpus ({a.corpus})"), run.corpus(a.corpus)
    run.finish()
    run.out["seconds"] = round(time.monotonic() - t, 1)
    out = a.out or (OUT / ("mock" if mock else "live") / "e2e.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run.out, ensure_ascii=False, indent=1, default=str))
    c = run.out["corpus"]
    if c:
        hit = sum(r.get("outcome") == r["expect"] for r in c)
        print(f"corpus: {hit}/{len(c)} as expected in {run.out.get('corpus_seconds')} s")
    print(f"wrote {out} ({run.out['seconds']} s)")
    if mock:
        mock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

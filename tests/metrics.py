"""One command -> tests/METRICS.md + tests/metrics.json (live) or tests/out/mock/ (--mock).

    uv run python tests/metrics.py                 # run e2e on the live stack, then measure
    uv run python tests/metrics.py --mock          # same against the in-process mock (dry run)
    uv run python tests/metrics.py --from tests/out/live/e2e.json   # measure an earlier e2e run
    uv run python tests/metrics.py --no-validator  # skip the HL7 validator (fast iteration)

Every number is computed from evidence on the stack (HAPI resources, AuditEvents, Mailpit, the SMS
page, Matrix, the service data directory) plus the e2e record; nothing is asserted by hand.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from e2e.run import CORPUS, other_patient  # noqa: E402
from jury.model import CTX, PATIENT  # noqa: E402
from pgktest import audit as A, fidelity as F, pii  # noqa: E402
from pgktest.norm import fold  # noqa: E402
from pgktest.stack import Hapi, Mailpit, Service, Stack, _dotenv, links_in  # noqa: E402
from pgktest.validator import validate  # noqa: E402

CLASSES = ["FILE", "HOLD", "BLOCK"]
SERVICE_TYPES = ["Task", "DiagnosticReport", "Observation", "DocumentReference", "Provenance", "AuditEvent",
                 "Practitioner", "PractitionerRole", "Organization", "MedicationStatement", "Binary", "Communication"]
ORGS = sorted({c.org for c in CTX.values()})


def ref(r): return f"{r['resourceType']}/{r['id']}"


def run_e2e(mock: bool) -> Path:
    cmd = [sys.executable, str(TESTS / "e2e" / "run.py")] + (["--mock"] if mock else [])  # default 32 workers, 30 s
    subprocess.run(cmd, check=False)
    return TESTS / "out" / ("mock" if mock else "live") / "e2e.json"


class Evidence:
    def __init__(self, st: Stack, e2e: dict):
        self.st, self.e2e = st, e2e
        self.hapi, self.mp, self.svc = Hapi(st.hapi), Mailpit(st.mailpit), Service(st)
        t = time.monotonic()
        self.dump = self.hapi.dump(SERVICE_TYPES + ["Patient"])
        self.by_ref = {ref(r): r for rs in self.dump.values() for r in rs}
        self.dump_seconds = round(time.monotonic() - t, 1)
        self.tasks = self.dump.get("Task", [])
        self.aes = self.dump.get("AuditEvent", [])
        blob = {ref(r): json.dumps(r, ensure_ascii=False) for r in self.tasks}
        self.task_of = {}
        for res in self.all_results():
            rid = res.get("request_id")
            if rid:
                self.task_of[rid] = next((self.by_ref[k] for k, b in blob.items() if rid in b), None)

    def all_results(self):
        yield from self.e2e.get("corpus", [])
        for k in ("scenario", "misdelivery"):
            for x in self.e2e.get(k, {}).values():
                if isinstance(x, dict):
                    yield x
                    if isinstance(x.get("then_brandl"), dict):
                        yield x["then_brandl"]

    def events_for(self, rid: str | None) -> list[dict]:
        t = self.task_of.get(rid) if rid else None
        if t:
            tref = ref(t)
            evs = [e for e in self.aes if tref in json.dumps(e.get("entity") or [])]
            if evs:
                return evs
        return [e for e in self.aes if rid and rid in json.dumps(e)]

    def filed_for(self, rid: str | None, canary: str | None, kind: str) -> dict[str, dict]:
        main_t = "DiagnosticReport" if kind == "lab_result" else "DocumentReference"
        main = None
        t = self.task_of.get(rid) if rid else None
        if t:
            for o in t.get("output") or []:
                r = (o.get("valueReference") or {}).get("reference", "")
                if r.startswith(main_t):
                    main = self.by_ref.get(r)
        if main is None and canary:
            for r in self.dump.get(main_t, []):
                att = (r.get("presentedForm") or [{}])[0] if main_t == "DiagnosticReport" else \
                    ((r.get("content") or [{}])[0].get("attachment") or {})
                if canary.encode() in base64.b64decode(att.get("data") or ""):
                    main = r
        if main is None:
            return {}
        out = {ref(main): main}
        for x in main.get("result") or []:
            o = self.by_ref.get(x.get("reference"))
            if o:
                out[ref(o)] = o
        for p in self.dump.get("Provenance", []):
            if ref(main) in [x.get("reference") for x in p.get("target") or []]:
                out[ref(p)] = p
        if t:
            out[ref(t)] = t
        return out


# ------------------------------------------------------------------ sections
def m_validator(ev: Evidence, enabled: bool) -> dict:
    res = [r for t in SERVICE_TYPES for r in ev.dump.get(t, [])]
    if not enabled:
        return {"skipped": True, "resources": len(res)}
    v = validate(res)
    v["by_type"] = dict(Counter(r["resourceType"] for r in res))
    return v


def m_effort(ev: Evidence) -> dict:
    r1 = ev.e2e.get("scenario", {}).get("request1") or {}
    vis = r1.get("visit") or {}
    pages = vis.get("pages") or []
    hosts = sorted({urlsplit(p["url"]).netloc for p in pages})
    # the (simulated) ID Austria page asks for the eID credential: the person's existing state eID, not an
    # account with this service. Any password field on the service's OWN pages would be an account.
    idp = lambda u: urlsplit(u).path.startswith("/eid/")
    eid_cred = [p["url"] for p in pages if idp(p["url"]) and "password" in p.get("inputs", [])]
    pw = [p["url"] for p in pages if not idp(p["url"]) and "password" in p.get("inputs", [])]
    acct = [p["url"] for p in pages if not idp(p["url"]) and re.search(r"registrier|konto (erstellen|anlegen)|create (an )?account|sign ?up|"
                                                 r"passwort|password", p.get("text", ""), re.I)]
    dl = [l for p in pages for l in p.get("links", [])
          if re.search(r"\.(exe|dmg|pkg|msi|apk|ipa|zip)(\?|$)|apps\.apple\.com|play\.google\.com", l, re.I)]
    installs = [l for p in pages for l in p.get("links", []) if re.search(r"install|herunterladen|download", l, re.I)]
    rows = r1.get("audit") or []
    at = lambda step: next((x.get("at") or x.get("recorded") for x in rows if step in A.steps_of(x)), None)
    aes = ev.events_for(r1.get("request_id"))
    ae_at = lambda step: next((e.get("recorded") for e in sorted(aes, key=lambda e: e.get("recorded", ""))
                               if step in A.steps_of(e)), None)
    from datetime import datetime
    def delta(a, b):
        try:
            return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 2)
        except (TypeError, ValueError):
            return None
    return {"installs": len(dl) + len(installs), "accounts": len(pw) + len(acct), "install_evidence": dl + installs,
            "account_evidence": pw + acct, "eid_credential_pages": eid_cred, "hosts_touched": hosts, "pages": len(pages),
            "actions": vis.get("actions"), "machine_seconds": vis.get("machine_s"), "klm": vis.get("klm"),
            "audit_seconds_link_to_answer": delta(ae_at("link-opened") or at("link-opened"),
                                                  ae_at("answer-sent") or at("answer-sent")),
            "audit_source": "HAPI AuditEvent" if aes else "/api/audit", "via": r1.get("via")}


def m_misdelivery(ev: Evidence) -> dict:
    M = ev.e2e.get("misdelivery", {})
    out = {}
    for k in ("M1", "M2", "M3"):
        m = M.get(k) or {}
        evs = ev.events_for(m.get("request_id"))
        caught_evs = [A.who_what_when(e) | {"id": e.get("id")} for e in evs
                      if A.steps_of(e) & {"misdelivery-caught", "identity-checked"}]
        out[k] = {"caught": bool(m.get("caught")) and bool(caught_evs or m.get("audit")), "e2e_caught": m.get("caught"),
                  "audit_events": caught_evs[:4], "matrix": (m.get("matrix_alert") or m.get("matrix") or {}).get("body"),
                  "detail": {x: m.get(x) for x in ("warned_at_send", "page_leaks", "could_answer", "colleague_filed",
                                                   "filed", "other_patient_leaks", "error", "via") if x in m}}
    out["M2_then_brandl_filed"] = ((M.get("M2") or {}).get("then_brandl") or {}).get("ok")
    out["prefetch_then_filed"] = (M.get("prefetch") or {}).get("caught")
    out["leaky_note_contained"] = (M.get("note") or {}).get("caught")
    out["namesake"] = {k: (M.get("namesake") or {}).get(k) for k in ("caught", "review_before_reveal", "nothing_shown",
                                                                     "confirm_status", "answered_after_confirm", "filed",
                                                                     "error")}
    out["hold_decision"] = {k: (M.get("hold_decision") or {}).get(k) for k in
                            ("caught", "held", "forged_actor_status", "no_headers_status", "accepted_status", "filed",
                             "decision_audited", "error")}
    out["score"] = sum(out[k]["caught"] for k in ("M1", "M2", "M3"))
    return out


def m_audit(ev: Evidence) -> dict:
    r1 = (ev.e2e.get("scenario", {}).get("request1") or {})
    evs = ev.events_for(r1.get("request_id"))
    for k in ("M1", "M2", "M3"):
        evs += ev.events_for((ev.e2e.get("misdelivery", {}).get(k) or {}).get("request_id"))
    cov = A.coverage(evs)
    try:
        rows = ev.svc.audit_all()
    except Exception:
        rows = []
    fc = A.verify_fhir_chains(ev.aes)
    tamper = {"fhir": A.tamper_fhir(ev.aes)}
    chain = {"fhir": fc}
    if rows and all("hash" in r and "prev_hash" in r for r in rows):
        key = A.audit_key(_dotenv("PGK_SECRET"))
        rv = A.verify_rows(rows, key)
        tamper["rows"] = A.tamper_rows(rows, key)
        cid = A.chain_id(key) if key else None  # this installation's chain; other installations' anchors ignored
        bodies = [m.get("body", "") for m in ev.e2e.get("matrix") or []]
        try:  # read the anchors back from the room history itself (as the service's verifier does)
            from pgktest.stack import Matrix, body_of
            if ev.st.lena_token and ev.st.audit_room_id:  # anchors live in the separate audit room (fallback: main)
                bodies = [body_of(e) for e in Matrix(ev.st.synapse, ev.st.lena_token, ev.st.audit_room_id).since(0, 1000)]
        except Exception:
            pass
        found = (A.hapi_anchors(ev.hapi.search("AuditEvent", "subtype=https://pgk-bridge.example/fhir/CodeSystem/"
                                               "audit-anchor%7Cchain-head"), cid)
                 + A.matrix_anchors(bodies, cid)) if cid else []
        anchors, ignored = A.split_authentic(found, key, cid) if cid else ([], [])
        av = A.verify_anchors(rows, anchors) | {"chain": cid, "hapi": sum(w != "Matrix" for _, _, w in anchors),
                                                "matrix": sum(w == "Matrix" for _, _, w in anchors),
                                                "ignored_unauthenticated": len(ignored)}
        try:
            from pgktest.web import call
            sv = call("GET", f"{ev.st.service}/api/audit/verify", headers=ev.svc.h)[1]
        except Exception as e:
            sv = {"ok": None, "error": str(e)[:120]}
        chain |= {"rows": {**rv, "window": f"ids {min(r['id'] for r in rows)}..{max(r['id'] for r in rows)}",
                           "key": bool(key)}, "anchors": av,
                  "service_verify": {k: sv.get(k) for k in ("ok", "count", "problems", "anchors")}}
    chain["ok"] = fc["ok"] and chain.get("rows", {}).get("ok", True) is True and chain.get("anchors", {}).get("ok", True)
    chain["detail"] = (f"FHIR: {fc['events']} events in {fc['chains']} chain(s), "
                       f"{'intact' if fc['ok'] else 'BROKEN ' + str(fc['edited'] + fc['dangling_prev'] + fc['forks'])[:120]}"
                       + (f"; SQLite {chain['rows']['window']} ({chain['rows']['keyed_rows']} HMAC-keyed): "
                          f"{'intact' if chain['rows']['ok'] else chain['rows']['problems'][:2]}; "
                          f"anchors {chain['anchors']['anchors']} MAC-verified ({chain['anchors'].get('hapi', 0)} HAPI, "
                          f"{chain['anchors'].get('matrix', 0)} Matrix; {chain['anchors'].get('ignored_unauthenticated', 0)} ignored) "
                          f"{'match' if chain['anchors']['ok'] else chain['anchors']['mismatch'][:2]}"
                          if "rows" in chain else ""))
    tamper["ok"] = all(t.get("ok") for t in tamper.values() if isinstance(t, dict))
    purpose = sum(1 for e in ev.aes if "TREAT" in A.who_what_when(e)["purpose"])
    fails = [e for e in ev.aes if "misdelivery-caught" in A.steps_of(e)]
    return {"coverage": cov, "score": sum(v is not None for v in cov.values()), "source": "HAPI AuditEvent",
            "events_total": len(ev.aes), "chain": chain, "tamper_test": tamper,
            "purpose_treat": f"{purpose}/{len(ev.aes)}",
            "misdelivery_outcome_8": f"{sum(str(e.get('outcome')) == '8' for e in fails)}/{len(fails)}"}


def m_pii(ev: Evidence) -> dict:
    findings, n = [], 0
    for m in ev.mp.all():
        msg = ev.mp.message(m["ID"])
        n += 1
        to = ",".join(t.get("Address", "") for t in msg.get("To") or [])
        if re.search(r"^(WG|Fwd?):", msg.get("Subject") or "", re.I):
            continue  # the M2 forward is written by the test, not by the service
        body = re.sub(r"<[^>]+>", " ", msg.get("HTML") or "")
        findings += pii.scan(msg.get("Subject") or "", f"subject→{to}", PATIENT, orgs=ORGS)
        findings += pii.scan((msg.get("Text") or "") + " " + body, f"email→{to}", PATIENT, orgs=ORGS)
        for u in links_in(msg.get("Text") or "", msg.get("HTML") or ""):
            findings += pii.scan(u, f"link→{to}", PATIENT, orgs=ORGS)
    sms_n = 0
    try:
        html = ev.svc.sms_page()
        txt = re.sub(r"<[^>]+>", " ", html)
        sms_n = len(links_in(html))
        findings += pii.scan(txt, "sms page", PATIENT, orgs=ORGS)
    except Exception as e:
        findings.append(("violation", "sms page unreadable", str(e)[:80], "sms"))
    pre = 0
    for r in ev.all_results():
        pages = (r.get("visit") or {}).get("pages") or []
        if pages:
            pre += 1
            findings += pii.scan(pages[0].get("text", ""), f"landing page {r.get('id') or r.get('case')}", PATIENT, orgs=ORGS)
    v = pii.violations(findings)
    return {"emails": n, "sms_links": sms_n, "landing_pages": pre, "violations": len(v),
            "violation_list": sorted({f"{c}: '{x}' in {w}" for _, c, x, w in v})[:40],
            "notices": sorted({f"{c}: '{x}'" for s, c, x, w in findings if s == "notice"})[:20]}


def _disk_blob(st: Stack) -> bytes | None:
    d = st.data_dir
    if not d.exists():
        return None
    out = bytearray()
    for p in d.rglob("*"):
        if p.is_file() and p.stat().st_size < 200_000_000:
            out += p.read_bytes()
    return bytes(out)


def m_corpus(ev: Evidence) -> dict:
    res = ev.e2e.get("corpus") or []
    if not res:
        return {"n": 0}
    cdir = Path(ev.e2e.get("corpus_dir") or CORPUS)
    man = {c["id"]: c for c in json.loads((cdir / "manifest.json").read_text())["cases"]}
    conf = {e: Counter() for e in CLASSES}
    for r in res:
        conf[r["expect"]][r.get("outcome") or "ERROR"] += 1
    hit = sum(r.get("outcome") == r["expect"] for r in res)
    wrong_patient = [r for r in res if r["expect"] == "BLOCK" and r.get("outcome") == "FILE"
                     and set(r["reasons"]) & {"WRONG_PATIENT", "EID_MISMATCH"}]
    should_not_file = [r for r in res if r["expect"] != "FILE" and r.get("outcome") == "FILE"]
    mx_all = ev.e2e.get("matrix") or []
    # Matrix HTML (formatted_body) is rendered by Element: raw tags/handlers/script URLs there are live
    live = re.compile(r"<\s*(script|img|iframe|svg|object|embed)\b|<[^>]*\son\w+\s*=|<a\b[^>]*href\s*=\s*[\"']?\s*javascript:",
                      re.I)
    xss = [live.search(m["html"]).group(0)[:80] for m in mx_all if m.get("html") and live.search(m["html"])]
    held_right = [r for r in res if r["expect"] == "FILE" and r.get("outcome") in ("HOLD", "BLOCK")]
    # fidelity of every FILE that was filed
    fid, fid_fail = Counter(), Counter()
    fid_cases = {}
    for r in res:
        if r["expect"] == "FILE" and r.get("outcome") == "FILE":
            c = man[r["id"]]
            got = ev.filed_for(r.get("request_id"), c.get("canary"), c["request"]["doc_kind"])
            checks = F.check_case(c, got, (cdir / c["file"]).read_bytes())
            fails = [(n, d) for n, ok, d in checks if not ok]
            fid_cases[r["id"]] = fails
            for n, ok, d in checks:
                fid[n] += 1
                if not ok:
                    fid_fail[n] += 1
    # privacy: nothing of a BLOCKed (or HOLD) document is filed; BLOCKed not stored or echoed anywhere
    # filed content only: Patient resources are master data (HAPI also holds Patient/max-beispiel by design)
    hapi_blob = json.dumps({t: rs for t, rs in ev.dump.items() if t != "Patient"}, ensure_ascii=False)
    hapi_att = b"".join(base64.b64decode(a.get("data") or "") for r in ev.dump.get("DiagnosticReport", []) +
                        ev.dump.get("DocumentReference", []) for a in
                        (r.get("presentedForm") or []) + [(c.get("attachment") or {}) for c in r.get("content") or []])
    mx = "\n".join(m.get("body", "") for m in ev.e2e.get("matrix") or [])
    aud = json.dumps(ev.e2e.get("audit_api") or [], ensure_ascii=False) + json.dumps(ev.aes, ensure_ascii=False)
    disk = _disk_blob(ev.st)
    priv = []
    for r in res:
        c = man[r["id"]]
        can = c.get("canary")
        if r["expect"] == "FILE" or not can:
            continue
        where = [w for w, hay in [("HAPI", hapi_blob), ("Matrix", mx), ("audit", aud)] if can in hay]
        if can.encode() in hapi_att:
            where.append("HAPI attachment")
        if disk is not None and can.encode() in disk:
            where.append("service data dir" if r["expect"] == "BLOCK" else "service data dir (HOLD: must be encrypted)")
        if r["expect"] == "BLOCK" and "WRONG_PATIENT" in c["reasons"]:
            names, dobs = other_patient((cdir / c["file"]).read_bytes())
            for nm in names:
                if fold(nm) in fold(mx) or fold(nm) in fold(aud):
                    where.append(f"other patient's name '{nm}' in Matrix/audit")
            for f_ in pii.violations(pii.scan(mx + aud, "x", {"name": ""}, extra_dobs=dobs, doc_type=False)):
                if f_[1] == "birth date":
                    where.append(f"other patient's DOB '{f_[2]}' in Matrix/audit")
        if where:
            priv.append({"id": r["id"], "expect": r["expect"], "found_in": where})
    # Task status transitions
    def hist(r):
        t = ev.task_of.get(r.get("request_id"))
        if not t:
            return r["id"], False, "no Task"
        final = {"FILE": {"completed"}, "HOLD": {"on-hold"},
                 "BLOCK": {"requested", "ready", "received", "accepted", "in-progress", "rejected", "on-hold"}}[r["expect"]]
        ok, seq = F.task_history_ok(ev.hapi.history(ref(t)), final)
        return r["id"], ok, seq
    with ThreadPoolExecutor(16) as ex:
        th = list(ex.map(hist, res))
    filed_obs = sum(r.get("outcome") == "FILE" for r in res)
    return {"n": len(res), "as_expected": hit, "confusion": {k: dict(v) for k, v in conf.items()},
            "wrong_patient_filed": [r["id"] for r in wrong_patient], "right_patient_held": [r["id"] for r in held_right],
            "should_not_file_but_filed": [r["id"] for r in should_not_file], "matrix_active_content": xss,
            "mismatches": [{"id": r["id"], "expect": r["expect"], "got": r.get("outcome"), "why": r.get("why"),
                            "error": r.get("error"), "title": r["title"]} for r in res if r.get("outcome") != r["expect"]],
            "fidelity": {"cases": len(fid_cases), "cases_all_ok": sum(not f for f in fid_cases.values()),
                         "checks": {n: f"{fid[n] - fid_fail[n]}/{fid[n]}" for n in fid},
                         "failures": {k: v[:6] for k, v in fid_cases.items() if v}},
            "privacy_violations": priv, "disk_checked": disk is not None,
            "task_transitions_ok": f"{sum(ok for _, ok, _ in th)}/{len(th)}",
            "task_transition_failures": [(i, s) for i, ok, s in th if not ok][:20],
            "reports_delta_vs_filed": {"hapi_delta": ev.e2e.get("corpus_reports_delta"), "filed_outcomes": filed_obs,
                                       "consistent": ev.e2e.get("corpus_reports_delta") == filed_obs},
            "seconds": ev.e2e.get("corpus_seconds")}


HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def _ni_hex(v: str) -> str | None:
    """'ni:///sha-256;<b64url>' or 'urn:sha256:<hex>' or hex -> hex."""
    if m := re.search(r"ni:///sha-256;([A-Za-z0-9_-]+)", v or ""):
        b = m[1] + "=" * (-len(m[1]) % 4)
        return base64.urlsafe_b64decode(b).hex()
    m = HEX64.search(v or "")
    return m[0] if m else None


def m_custody(ev: Evidence) -> dict:
    """Attribution: the Provenance sender's Practitioner carries the bpk-hmac identifier of the eID that actually
    logged in (from the request's 'identity checked' audit row) and no register id; the signatory is a separate
    Practitioner; the namesake gets her own. Hashes: raw upload = receipt = audit 'answer sent' = Provenance source."""
    recs = []
    for r in ev.all_results():
        if r.get("request_id") and (r.get("visit") or {}).get("raw_sha256"):
            recs.append(r)

    def one(r):
        rid, vis = r["request_id"], r["visit"]
        try:
            rows = ev.svc.audit(rid)
        except Exception:
            rows = []
        ok_ids = [x for x in rows if "identity-checked" in A.steps_of(x) and
                  str(x.get("outcome")).lower() in ("success", "ok", "0", "true")]
        bpk = ((ok_ids[-1].get("detail") or {}).get("bpk_hash")) if ok_ids else None
        sent = [x for x in rows if "answer-sent" in A.steps_of(x)]
        audit_sha = None
        for x in reversed(sent):
            audit_sha = (x.get("detail") or {}).get("sha256") or _ni_hex(str(x.get("what")))
            if audit_sha:
                break
        receipt_sha = _ni_hex(vis.get("receipt") or "")
        res = {"id": r.get("id") or r.get("case") or rid, "raw": vis["raw_sha256"], "receipt": receipt_sha,
               "audit": audit_sha, "provenance": None, "bpk": bpk}
        filed = {}
        for kind in ("lab_result", "discharge_report"):
            filed = ev.filed_for(rid, None, kind)
            if filed:
                break
        prov = next((x for x in filed.values() if x["resourceType"] == "Provenance"), None)
        if prov:
            src = [e for e in prov.get("entity") or [] if e.get("role") == "source"]
            res["provenance"] = next((_ni_hex(str((e.get("what") or {}).get("identifier"))) for e in src), None)
            role = lambda a: {c.get("code") for c in (a.get("type") or {}).get("coding") or []}
            sender = next((a for a in prov.get("agent") or [] if "informant" in role(a)), None)
            authors = [a for a in prov.get("agent") or [] if "author" in role(a)]
            sref = ((sender or {}).get("who") or {}).get("reference")
            separate = all(((a.get("who") or {}).get("reference") or "x") != sref for a in authors)
            if (sref or "").startswith("Patient/"):  # the patient answered herself: she is the informant, no Practitioner
                res |= {"sender_ref": sref, "sender_bpk_ok": True, "sender_no_register_id": True,
                        "signatory_separate": separate}
            else:
                prac = ev.by_ref.get(sref) or (ev.hapi.get(sref) if sref else None) or {}
                idents = prac.get("identifier") or []
                res |= {"sender_ref": sref,
                        "sender_bpk_ok": bool(bpk) and any(i.get("system", "").endswith("/sid/bpk-hmac-sha256")
                                                           and i.get("value") == bpk for i in idents),
                        "sender_no_register_id": not any(re.search(r"aerzteliste|gda-index", i.get("system", ""))
                                                         for i in idents),
                        "signatory_separate": separate}
        res["accepted"] = bool(sent)  # refused before acceptance (e.g. > 1 MB, drained unread): no hash recorded
        # a BLOCKed upload is discarded: it gets no receipt (nothing was delivered) and no Provenance, only the audit hash
        res["blocked"] = any("misdelivery-caught" in A.steps_of(x) for x in rows)
        if not res["accepted"]:
            res["hash_ok"] = res["receipt"] is None and res["provenance"] is None
        elif res["blocked"] and res["receipt"] is None:
            res["hash_ok"] = res["raw"] == res["audit"] and res["provenance"] is None
        else:
            res["hash_ok"] = res["raw"] == res["receipt"] == res["audit"] and res["provenance"] in (None, res["raw"])
        return res

    with ThreadPoolExecutor(16) as ex:
        out = list(ex.map(one, recs))
    filed = [x for x in out if x.get("sender_ref") is not None or x.get("provenance")]
    att_bad = [x["id"] for x in filed if not (x.get("sender_bpk_ok") and x.get("sender_no_register_id")
                                              and x.get("signatory_separate"))]
    hash_bad = [{k: x.get(k) for k in ("id", "raw", "receipt", "audit", "provenance")} for x in out if not x["hash_ok"]]
    mi = ev.e2e.get("misdelivery", {}).get("namesake") or {}
    ns = next((x for x in out if x.get("id") in (mi.get("request_id"),)), None)
    sc = (ev.e2e.get("scenario") or {}).get("request1") or {}
    r1 = next((x for x in out if x.get("id") in ("A01", sc.get("request_id")) and x.get("sender_ref")), None)
    namesake = {"present": bool(ns and r1),
                "different_practitioner": bool(ns and r1 and ns.get("sender_ref") != r1.get("sender_ref")
                                               and ns.get("bpk") != r1.get("bpk"))}
    refused = [x["id"] for x in out if not x["accepted"]]
    return {"submissions": len(out), "hash_ok": len(out) - len(hash_bad), "hash_mismatch": hash_bad[:10],
            "accepted": len(out) - len(refused), "refused_before_acceptance": refused,
            "filed": len(filed), "attribution_ok": len(filed) - len(att_bad), "attribution_bad": att_bad[:10],
            "namesake": namesake,
            "ok": not hash_bad and not att_bad and (namesake["different_practitioner"] or not namesake["present"])}


EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE = re.compile(r"(?<![\w])\+\d{1,3}[\d \-/]{6,}\d")


def m_privacy(ev: Evidence) -> dict:
    """Privacy at rest: no contact data in the audit's hashed part or in FHIR AuditEvents; the request text is
    cleared after filing; the service's data directory and its DB/key files are private (0700 / 0600)."""
    import sqlite3
    import stat
    out: dict = {}
    hits = []
    for e in ev.aes:
        blob = json.dumps(e, ensure_ascii=False)
        hits += [f"AuditEvent/{e.get('id')}: {x}" for x in EMAIL.findall(blob) + PHONE.findall(blob)]
    out["fhir_auditevent_contacts"] = {"count": len(hits), "examples": hits[:5]}
    try:
        rows = ev.svc.audit_all()
    except Exception:
        rows = []
    rh = []
    for r in rows:  # the chained (hashed) part; 'contact' is cleartext kept beside the chain until expiry
        blob = json.dumps({k: r.get(k) for k in A.ROW_FIELDS}, ensure_ascii=False)
        rh += [f"audit #{r.get('id')}: {x}" for x in EMAIL.findall(blob) + PHONE.findall(blob)]
    out["audit_row_contacts"] = {"count": len(rh), "examples": rh[:5], "rows": len(rows)}
    db = Path(os.environ.get("PGK_DB") or ev.st.data_dir / "pgk.sqlite")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n_filed, n_left = con.execute("SELECT count(*), sum(request_text IS NOT NULL) FROM requests WHERE status='filed'").fetchone()
        n_text = con.execute("SELECT count(*) FROM requests WHERE request_text IS NOT NULL").fetchone()[0]
        con.close()
        out["request_text_after_filing"] = {"filed": n_filed, "still_has_text": n_left or 0, "open_with_text": n_text - (n_left or 0)}
    except Exception as e:
        out["request_text_after_filing"] = {"error": f"{type(e).__name__}: {e}"[:160]}
    perms = {"dir": None, "bad": [], "checked": []}
    d = ev.st.data_dir
    if d.exists():
        perms["dir"] = oct(stat.S_IMODE(d.stat().st_mode))
        if stat.S_IMODE(d.stat().st_mode) != 0o700:
            perms["bad"].append(f"{d.name}/ is {perms['dir']} (want 0o700)")
        for p in sorted(d.iterdir()):
            if p.is_file() and re.search(r"\.(sqlite|db)(-wal|-shm)?$|\.(pem|key)$", p.name):
                mode = stat.S_IMODE(p.stat().st_mode)
                perms["checked"].append(f"{p.name} {oct(mode)}")
                if mode & 0o077:
                    perms["bad"].append(f"{p.name} is {oct(mode)} (want 0o600)")
    out["file_permissions"] = perms
    rt = out["request_text_after_filing"]
    out["ok"] = (not hits and not rh and rt.get("still_has_text") == 0 and perms["dir"] is not None and not perms["bad"])
    return out


def m_reverify(mock: bool) -> dict:
    """`make verify-provenance`: the service re-reads every filing from HAPI and re-verifies its signed statement."""
    if mock:
        return {"skipped": "mock"}
    try:
        p = subprocess.run(["make", "-s", "verify-provenance"], cwd=TESTS.parent, capture_output=True, text=True,
                           timeout=600)
    except Exception as e:
        return {"error": str(e)[:200]}
    out = p.stdout + p.stderr
    m = re.search(r"filings checked: (\d+), verified: (\d+), not re-verifiable[^:]*: (\d+)", out)
    if not m:
        return {"error": out[-400:], "exit": p.returncode}
    checked, verified, nrv = map(int, m.groups())
    return {"checked": checked, "verified": verified, "not_reverifiable": nrv, "exit": p.returncode,
            "ok": p.returncode == 0 and checked - nrv > 0 and verified == checked - nrv, "failed": [l for l in out.splitlines() if l.startswith("  - ")][:10]}


def m_timezone(ev: Evidence) -> dict:
    bad, total = [], 0
    for t in SERVICE_TYPES:
        for r in ev.dump.get(t, []):
            for p, v in F.datetimes(r):
                total += 1
                if not F.tz_ok(v):
                    bad.append(f"{ref(r)}.{p}={v}")
    return {"datetimes": total, "violations": len(bad), "examples": bad[:10]}


# ------------------------------------------------------------------ report
def _extract_eval() -> dict | None:
    """The summary of tests/extract_eval.json (photo intake), if that evaluation has been run."""
    p = Path(__file__).resolve().parent / "extract_eval.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))["summary"]
    except (OSError, ValueError, KeyError):
        return None


def mark(ok) -> str:
    return "PASS" if ok else "FAIL"


def tamper_text(t: dict) -> str:
    """The tamper test as sentences (what was edited or deleted in a copy, and how it was detected)."""
    out = []
    f, r = t.get("fhir") or {}, t.get("rows") or {}
    if f:
        out.append(f"Edited {f.get('edited', 'an AuditEvent')} → {'detected' if f.get('edit_detected') else 'NOT detected'}; "
                   f"deleted an AuditEvent → {'detected (its successor points to a missing hash)' if f.get('delete_detected') else 'NOT detected'}")
    if r:
        ed, de = r.get("edit_detected") or [], r.get("delete_detected") or []
        out.append(f"edited audit row #{r.get('edited_id')} → {'detected (' + ed[0] + ')' if ed else 'NOT detected'}; "
                   f"deleted a row → {'detected (' + de[0] + ')' if de else 'NOT detected'}")
    if not out:
        out.append("tamper test not run" if not t else ("detected" if t.get("ok") else "NOT detected"))
    return ". ".join(x[0].upper() + x[1:] for x in out) + "."


def human_timing() -> dict | None:
    """tests/human_timing.json: [{"person": "P1", "seconds": 48, "questions": 0}, ...] from real people."""
    p = TESTS / "human_timing.json"
    try:
        rows = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    secs = sorted(float(r["seconds"]) for r in rows if isinstance(r, dict) and r.get("seconds") is not None)
    if not secs:
        return None
    n = len(secs)
    med = secs[n // 2] if n % 2 else (secs[n // 2 - 1] + secs[n // 2]) / 2
    return {"n": n, "median_s": round(med, 1), "max_s": round(secs[-1], 1),
            "questions": sum(int(r.get("questions") or 0) for r in rows if isinstance(r, dict))}


def prove_gate() -> dict | None:
    """tests/prove_gate.py (if present) prints {"documents", "corruptions", "detected", "undetected"}."""
    p = TESTS / "prove_gate.py"
    if not p.exists():
        return None
    try:
        out = subprocess.run([sys.executable, str(p)], cwd=TESTS.parent, capture_output=True, text=True, timeout=600)
        txt = out.stdout.strip()
        try:
            js = json.loads(txt)                      # pretty-printed JSON
        except ValueError:
            js = json.loads(txt[txt.rindex("\n{") + 1:] if "\n{" in txt else txt.splitlines()[-1])
        return {k: js.get(k) for k in ("documents", "corruptions", "detected", "undetected")} | {"exit": out.returncode}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}


def render(M: dict) -> str:
    v, e, mi, au, p, c, tz = (M[k] for k in ("validator", "effort", "misdelivery", "audit", "pii", "corpus", "timezone"))
    cu, rv, pv = M.get("custody") or {}, M.get("reverify") or {}, M.get("privacy") or {}
    L = [f"# Metrics: Befundlink on the scenario {'(MOCK DRY RUN, not the product)' if M['mock'] else ''}", "",
         f"Generated by `uv run python tests/metrics.py` at {M['generated']}. e2e run: {M['e2e_seconds']} s; "
         f"metrics: {M['seconds']} s.", "",
         "## The brief's metrics", "",
         "| Metric (track2/README.md) | Target | Result | |", "|---|---|---|---|"]
    verr = v.get("errors")
    L.append(f"| HL7 validator errors, every filed resource ({v.get('files', v.get('resources'))} resources, "
             f"terminology {'on' if v.get('tx') else 'OFF' if v.get('tx') is False else '?'}) | 0 | "
             f"{'skipped' if v.get('skipped') else verr} | {mark(verr == 0)} |")
    L.append(f"| Outside-person installs / accounts | 0 / 0 | {e['installs']} / {e['accounts']} | "
             f"{mark(e['installs'] == 0 and e['accounts'] == 0)} |")
    klm = (e.get("klm") or {}).get("total_s")
    ht = M.get("human_timing")
    if ht:   # PASS rests on people who had never seen it; the keystroke model is context only
        L.append(f"| Outside-person time, email opened → sent | < 2 min | {ht['n']} people new to it: median "
                 f"{ht['median_s']} s, max {ht['max_s']} s (keystroke model {klm} s) | {mark(ht['max_s'] < 120)} |")
    else:
        L.append(f"| Outside-person time, email opened → sent | < 2 min | keystroke model {klm} s; "
                 f"timing with people new to it in `tests/human_timing.json` | model only |")
    L.append(f"| Misdeliveries caught (M1 wrong address, M2 forwarded, M3 wrong patient) | 3/3 | {mi['score']}/3 | "
             f"{mark(mi['score'] == 3)} |")
    L.append(f"| Audit steps with who, what and when (FHIR AuditEvents on HAPI) | 6/6 | {au['score']}/6 | "
             f"{mark(au['score'] == 6)} |")
    L += ["", "## Beyond the brief", "", "| Check | Target | Result | |", "|---|---|---|---|"]
    if c.get("n"):
        traps = sum(sum(c["confusion"][k].values()) for k in ("HOLD", "BLOCK"))
        genuine = sum(c["confusion"]["FILE"].values())
        trap_filed = len(c["should_not_file_but_filed"])  # every expected HOLD/BLOCK that was filed (wrong patient included)
        L.append(f"| Jury corpus (`tests/jury/RULES.md`): {traps} traps, {genuine} genuine | {c['n']}/{c['n']} | "
                 f"{c['as_expected']}/{c['n']}: {traps} traps, {trap_filed} filed; {genuine} genuine, "
                 f"{len(c['right_patient_held'])} wrongly held | {mark(c['as_expected'] == c['n'])} |")
    L.append(f"| PII in emails, SMS, links and pre-login pages ({p['emails']} mails, {p['landing_pages']} pages) | 0 | "
             f"{p['violations']} | {mark(p['violations'] == 0)} |")
    if cu:
        L.append(f"| Sender attribution: the eID that logged in, no register id, signatory separate | "
                 f"{cu['filed']}/{cu['filed']} | {cu['attribution_ok']}/{cu['filed']} {cu['attribution_bad'] or ''} | "
                 f"{mark(cu['attribution_ok'] == cu['filed'])} |")
    if rv:
        if "checked" in rv:
            earlier = rv.get("earlier_changed") or []
            res = (f"{rv['verified']}/{rv['checked'] - rv['not_reverifiable']} verified; all of this run's filings verify"
                   if not rv.get("this_run_failed") else f"{len(rv['this_run_failed'])} of this run's filings do NOT verify")
            if earlier:
                res += (f"; {len(earlier)} earlier filing(s) on the stack changed after filing → detected: "
                        + "; ".join(earlier)[:220])
            L.append(f"| Signed sender statements re-verified from HAPI (`make verify-provenance`) | all of this run | "
                     f"{res} | {mark(rv.get('ok_this_run', rv.get('ok')))} |")
        else:
            L.append(f"| Signed sender statements re-verified from HAPI (`make verify-provenance`) | all | "
                     f"{str(rv.get('skipped') or rv.get('error'))[:80]} | n/a |")
    pg = M.get("prove_gate")
    if pg:
        if "error" in pg:
            L.append(f"| AI-extraction gate (`tests/prove_gate.py`) | all caught | {pg['error']} | FAIL |")
        else:
            L.append(f"| AI-extraction gate: prove() caught {pg['detected']}/{pg['corruptions']} corrupted extractions "
                     f"on {pg['documents']} documents | all | {pg['detected']}/{pg['corruptions']} | "
                     f"{mark(not pg['undetected'] and pg['detected'] == pg['corruptions'])} |")  # [] or 0
    xe = _extract_eval()
    if xe:   # tests/extract_eval.py: photos read twice by the model, then the unchanged rules (cached readings)
        L.append(f"| AI photo intake (`tests/extract_eval.py`, {xe['model']}, 2 readings): {xe['photos']} photos, "
                 f"{xe['filed_identical']} filed identical, {xe['held']} held, {xe['blocked']} blocked | 0 filed wrong | "
                 f"{xe['filed_wrong']} filed wrong | {mark(xe['filed_wrong'] == 0)} |")
    ch = au["chain"]
    an = ch.get("anchors") or {}
    L.append(f"| Audit chain intact, external anchors MAC-verified, tamper detected | yes | "
             f"chain intact ({au['events_total']} FHIR AuditEvents, {(ch.get('rows') or {}).get('count', '?')} keyed rows); "
             f"anchors {an.get('anchors', 0)} MAC-verified ({an.get('hapi', 0)} HAPI, {an.get('matrix', 0)} Matrix) "
             f"{'match' if an.get('ok', True) else 'MISMATCH'}; tamper {'detected' if au['tamper_test'].get('ok') else 'NOT detected'} | "
             f"{mark(ch['ok'] and au['tamper_test'].get('ok'))} |")
    L += ["", "## Misdeliveries", "", "| Test | Caught | Evidence (audit) | Matrix |", "|---|---|---|---|"]
    for k in ("M1", "M2", "M3"):
        x = mi[k]
        ae = "; ".join(f"{a['when']} · {a['who']} · {a['what']}" for a in x["audit_events"][:2]) or "-"
        L.append(f"| {k} | {mark(x['caught'])} | {ae} | {(x['matrix'] or '-').strip()[:120]} |")
    L += [f"", f"- M2 variant, colleague first and then Dr. Brandl: Brandl filed = {mi['M2_then_brandl_filed']}",
          f"- Mail-scanner prefetch (2× GET + HEAD) and then Dr. Brandl: filed = {mi['prefetch_then_filed']}",
          f"- Dr. Hofer's leaky note contained (refused or stripped) = {mi['leaky_note_contained']}",
          f"- HOLD → decision: held = {mi['hold_decision'].get('held')}, forged actor → HTTP "
          f"{mi['hold_decision'].get('forged_actor_status')}, no headers → HTTP {mi['hold_decision'].get('no_headers_status')}, "
          f"Dr. Hofer files → HTTP {mi['hold_decision'].get('accepted_status')}, filed = {mi['hold_decision'].get('filed')}, "
          f"decision audited = {mi['hold_decision'].get('decision_audited')}", "",
          "## Audit coverage (request 1 + M1–M3, FHIR AuditEvents on HAPI)", "", "| Step | Who | What | When |", "|---|---|---|---|"]
    for s, x in au["coverage"].items():
        L.append(f"| {s} | {x['who'] if x else 'MISSING'} | {(x or {}).get('what', '')} | {(x or {}).get('when', '')} |")
    L += ["", f"- **Chain:** {au['chain']['detail']}.",
          f"- **Tamper test** (on a copy): {tamper_text(au['tamper_test'])}",
          f"- **Coding:** purposeOfEvent TREAT on {au['purpose_treat']} events; misdelivery events with outcome 8: "
          f"{au['misdelivery_outcome_8']}.", ""]
    L += ["## Outside-person effort (request 1)", "",
          f"- **Touched:** {', '.join(e['hosts_touched']) or '-'}, over {e['pages']} pages, with an email client and a browser they already have.",
          f"- **Installs** ({e['installs']}) evidence: {e['install_evidence'] or 'no download/app-store links on any page'}.",
          f"- **Accounts** ({e['accounts']}) evidence: {e['account_evidence'] or 'no password field and no sign-up wording on any page'}. "
          "ID Austria is the state eID the person already holds; it is not an account created for this service. "
          f"The simulated ID Austria page asks for its PIN (standing in for the app confirmation) on "
          f"{len(e.get('eid_credential_pages') or [])} page(s); that is the eID credential, not a service account.",
          f"- **Actions:** {e['actions']}",
          f"- **KLM estimate:** {e['klm']}",
          f"- **Machine time** of the automated flow: {e['machine_seconds']} s. **Audit** link-opened → answer-sent: "
          f"{e['audit_seconds_link_to_answer']} s (source {e['audit_source']}).", "",
          *([f"- **Human timing** (`tests/human_timing.json`, n={M['human_timing']['n']}): median "
             f"{M['human_timing']['median_s']} s, max {M['human_timing']['max_s']} s, "
             f"{M['human_timing']['questions']} questions asked in total.", ""] if M.get("human_timing") else []),
          "### Manual timing checklist (n ≥ 3 people who have never seen the system)", "",
          "1. Reset: `make clean && make bootstrap`, start the service and bot, and send request 1 from Element "
          "(`!anfordern labor Dr. Eva Brandl, …`).",
          "2. Give the tester the Mailpit page (http://localhost:8025) and the file `lab-result-maria-beispiel.md`. "
          "Say only: \"You are Dr. Eva Brandl. Answer the request.\"",
          "3. Start the stopwatch when the tester opens the email. Stop it when the confirmation page appears.",
          "4. Note every install or account they're asked for, and every question they ask.",
          "5. Cross-check the time against the audit: `link-opened` → `answer-sent` for that request.", "",
          "| Tester | Device | Opened → sent (s) | Audit link→answer (s) | Installs | Accounts | Questions asked |",
          "|---|---|---|---|---|---|---|", "| 1 | | | | | | |", "| 2 | | | | | | |", "| 3 | | | | | | |", ""]
    if c.get("n"):
        L += ["## Jury corpus", "",
              f"{c['as_expected']}/{c['n']} outcomes as expected in {c['seconds']} s. Rules: `tests/jury/RULES.md`.", "",
              "| expected \\ observed | FILE | HOLD | BLOCK | other |", "|---|---|---|---|---|"]
        for k in CLASSES:
            row = c["confusion"][k]
            L.append(f"| **{k}** | {row.get('FILE', 0)} | {row.get('HOLD', 0)} | {row.get('BLOCK', 0)} | "
                     f"{sum(n for o, n in row.items() if o not in CLASSES)} |")
        f = c["fidelity"]
        L += ["", f"- Wrong-patient or misdelivered documents filed: **{len(c['wrong_patient_filed'])}** "
                  f"{c['wrong_patient_filed'] or ''}. Right-patient documents held or blocked: {len(c['right_patient_held'])} "
                  f"{c['right_patient_held'] or ''}.",
              f"- FHIR fidelity: {f['cases_all_ok']}/{f['cases']} filed cases pass every check (value, comparator, "
              "UCUM code, range, no derived H/L, Vienna offsets, Provenance, Task, one transaction).",
              f"- Privacy for held/blocked documents: {len(c['privacy_violations'])} violations "
              f"(disk checked: {c['disk_checked']}). Task transitions OK: {c['task_transitions_ok']}. HAPI report delta "
              f"{c['reports_delta_vs_filed']['hapi_delta']} vs {c['reports_delta_vs_filed']['filed_outcomes']} FILE outcomes.", ""]
        if c["mismatches"]:
            L += ["| Case | Expected | Got | Title | Why |", "|---|---|---|---|---|"]
            L += [f"| {m['id']} | {m['expect']} | {m['got']} | {m['title']} | {(m['error'] or m['why'] or '')[:90]} |"
                  for m in c["mismatches"]]
            L.append("")
        bad = {n: s for n, s in f["checks"].items() if s.split("/")[0] != s.split("/")[1]}
        if bad:
            L += ["Fidelity checks that fail somewhere:", ""] + [f"- {n}: {s}" for n, s in sorted(bad.items())] + [""]
    L += ["## Engineering checks", "", "| Check | Target | Result | |", "|---|---|---|---|"]
    if c.get("n"):
        L.append(f"| Another patient's document, or a wrong sender's, filed | 0 | {len(c['wrong_patient_filed'])} | "
                 f"{mark(not c['wrong_patient_filed'])} |")
        L.append(f"| Near-miss / ambiguous / malicious documents filed instead of held or blocked | 0 | "
                 f"{len(c['should_not_file_but_filed'])} {c['should_not_file_but_filed'] or ''} | "
                 f"{mark(not c['should_not_file_but_filed'])} |")
        L.append(f"| Unescaped active content in Matrix messages | 0 | {len(c['matrix_active_content'])} | "
                 f"{mark(not c['matrix_active_content'])} |")
        f = c["fidelity"]
        L.append(f"| Filed FHIR exactly as sent (values, UCUM, ranges, flags, status, Provenance, Task) | "
                 f"{f['cases']}/{f['cases']} | {f['cases_all_ok']}/{f['cases']} | {mark(f['cases_all_ok'] == f['cases'])} |")
    L.append(f"| Filed dateTimes with the Europe/Vienna offset | {tz['datetimes']} | {tz['datetimes'] - tz['violations']} | "
             f"{mark(tz['violations'] == 0)} |")
    if cu:
        L.append(f"| Namesake (another Eva Brandl, confirmed) filed under her own Practitioner | yes | "
                 f"{'yes' if cu['namesake']['different_practitioner'] else 'no' if cu['namesake']['present'] else 'not run'} | "
                 f"{mark(cu['namesake']['different_practitioner'])} |")
        L.append(f"| SHA-256 raw upload = receipt = audit = Provenance source | {cu['submissions']}/{cu['submissions']} | "
                 f"{cu['hash_ok']}/{cu['submissions']} ({cu.get('accepted')} accepted; blocked uploads get no receipt, only the "
                 f"audit hash; refused before acceptance: {', '.join(cu.get('refused_before_acceptance') or []) or '-'}) | "
                 f"{mark(cu['hash_ok'] == cu['submissions'])} |")
    if pv:
        rt, fp = pv["request_text_after_filing"], pv["file_permissions"]
        L.append(f"| E-mail/phone in FHIR AuditEvents / in the audit's hashed rows | 0 / 0 | "
                 f"{pv['fhir_auditevent_contacts']['count']} / {pv['audit_row_contacts']['count']} | "
                 f"{mark(not pv['fhir_auditevent_contacts']['count'] and not pv['audit_row_contacts']['count'])} |")
        L.append(f"| Request text left on filed requests | 0 | {rt.get('still_has_text', rt.get('error'))} "
                 f"(of {rt.get('filed', '?')} filed) | {mark(rt.get('still_has_text') == 0)} |")
        L.append(f"| service/data 0700, DB and key files 0600 | yes | dir {fp['dir']}, "
                 f"{'all private' if not fp['bad'] else '; '.join(fp['bad'][:3])} | "
                 f"{mark(fp['dir'] is not None and not fp['bad'])} |")
    L.append("")
    lat = M.get("latency") or {}
    svc = {k: x for k, x in lat.items() if k.split(" ", 1)[1].startswith(M.get("service_port", "8000"))}
    if svc:
        L += ["## Service latency (outside person + API, measured client-side during the e2e run)", "",
              "| Route | n | p50 s | p95 s | max s |", "|---|---|---|---|---|"]
        L += [f"| {k} | {x['n']} | {x['p50']} | {x['p95']} | {x['max']} |" for k, x in svc.items() if x["n"] >= 3]
        L.append("")
    if v.get("per_resource"):
        L += ["## Validator errors (first 20)", ""] + [f"- {r}: {m[0]}" for r, m in list(v["per_resource"].items())[:20]] + [""]
    if p["violation_list"]:
        L += ["## PII violations", ""] + [f"- {x}" for x in p["violation_list"]] + [""]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--from", dest="src", type=Path)
    ap.add_argument("--no-validator", action="store_true")
    a = ap.parse_args()
    t0 = time.monotonic()
    mock = None
    if a.mock:
        from e2e.mockstack import MockStack
        mock = MockStack().start()
        os.environ.update(mock.env())
        from e2e import run as R
        st = Stack()
        st.state = {"room_id": mock.room_id, "tokens": {"lena": mock.lena_token}}
        run = R.Run(st, 32, False)
        run.out["mock"] = True
        run.scenario(), run.misdelivery(), run.corpus(CORPUS), run.finish()
        e2e = json.loads(json.dumps(run.out, default=str))
        outdir = TESTS / "out" / "mock"
    else:
        src = a.src or run_e2e(False)
        e2e = json.loads(src.read_text())
        outdir = TESTS
    e2e_s = e2e.get("seconds") or round(time.monotonic() - t0, 1)
    st = Stack()
    if mock:
        st.state = {"room_id": mock.room_id, "tokens": {"lena": mock.lena_token}}
    ev = Evidence(st, e2e)
    with ThreadPoolExecutor(2) as ex:  # the validator JVM runs while the HTTP-bound sections are computed
        fv = ex.submit(m_validator, ev, not a.no_validator)
        M = {"mock": bool(mock), "generated": time.strftime("%Y-%m-%d %H:%M:%S %z"), "e2e_seconds": e2e_s,
             "effort": m_effort(ev), "misdelivery": m_misdelivery(ev), "audit": m_audit(ev), "pii": m_pii(ev),
             "corpus": m_corpus(ev), "timezone": m_timezone(ev), "privacy": m_privacy(ev), "custody": m_custody(ev),
             "latency": e2e.get("latency") or {},
             "service_port": str(urlsplit(st.service).port or 80)}
        M["validator"] = fv.result()
    M["reverify"] = m_reverify(bool(mock))
    if "failed" in M["reverify"]:  # scope: failures of THIS run's filings vs changes to earlier filings on the stack
        mine = {r.get("request_id") for r in ev.all_results() if r.get("request_id")}
        fails = [(re.search(r"\((req-[0-9a-f]+)\)", f) or [None, None])[1] for f in M["reverify"]["failed"]]
        M["reverify"]["this_run_failed"] = [f for f, rid in zip(M["reverify"]["failed"], fails) if rid in mine]
        M["reverify"]["earlier_changed"] = [f.strip(" -") for f, rid in zip(M["reverify"]["failed"], fails) if rid not in mine]
        M["reverify"]["ok_this_run"] = not M["reverify"]["this_run_failed"] and M["reverify"]["checked"] > 0
    M["human_timing"], M["prove_gate"] = human_timing(), prove_gate()
    M["seconds"] = round(time.monotonic() - t0, 1)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "metrics.json").write_text(json.dumps(M, ensure_ascii=False, indent=1, default=str))
    (outdir / "METRICS.md").write_text(render(M))
    print(render(M).split("\n## ")[0])
    print(f"wrote {outdir / 'METRICS.md'} and metrics.json ({M['seconds']} s)")
    if mock:
        mock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

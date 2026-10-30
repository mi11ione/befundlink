"""Is what was filed exactly what was sent? Per-case checks of the FHIR resources on HAPI.

Each check is (name, ok, detail). Expectations come from the corpus manifest (tests/jury/model.py),
which derives them from the same row data the document was rendered from.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .norm import fold

VIENNA = ZoneInfo("Europe/Vienna")
LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
PATIENT_REF = "Patient/maria-beispiel"
DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def ref_of(r: dict) -> str:
    return f"{r['resourceType']}/{r['id']}"


def _same_instant(got: str | None, want: str) -> tuple[bool, str]:
    if not got:
        return False, "missing"
    try:
        g, w = datetime.fromisoformat(got), datetime.fromisoformat(want)
    except ValueError:
        return False, f"unparseable {got!r}"
    if g.tzinfo is None:
        return False, f"{got} has no offset"
    return g == w and g.utcoffset() == w.utcoffset(), f"{got} vs {want}"


def datetimes(obj, path: str = "") -> list[tuple[str, str]]:
    """Every dateTime/instant with a time component anywhere in a resource (skips meta.lastUpdated)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "meta" or k == "data":
                continue
            out += datetimes(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += datetimes(v, f"{path}[{i}]")
    elif isinstance(obj, str) and DT_RE.match(obj):
        out.append((path, obj))
    return out


def tz_ok(value: str) -> bool:
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        return False
    if d.tzinfo is None or value.endswith("Z"):
        return False
    return d.utcoffset() == d.astimezone(VIENNA).utcoffset()


def _att_ok(att: dict, original: bytes) -> list[tuple[str, bool, str]]:
    data = base64.b64decode(att.get("data") or "")

    def norm(b: bytes) -> str:  # the original may be Windows-1252 (ENCODING_FALLBACK): compare the decoded text
        from jury.refcheck import decode
        t = decode(b)[0]
        return (t if t is not None else b.decode("utf-8", "replace")).replace("\r\n", "\n").strip()
    out = [("attachment = original upload", data == original or norm(data) == norm(original),
            f"{len(data)} vs {len(original)} bytes"),
           ("attachment contentType text/markdown", (att.get("contentType") or "").startswith("text/markdown"),
            att.get("contentType"))]
    want = base64.b64encode(hashlib.sha1(data).digest()).decode()
    out.append(("attachment hash = base64 SHA-1 (R4)", att.get("hash") == want, f"{att.get('hash')} vs {want}"))
    return out


def _coding(cc: dict, system: str) -> list[dict]:
    return [c for c in (cc or {}).get("coding") or [] if c.get("system") == system]


def check_case(case: dict, res: dict[str, dict], original: bytes) -> list[tuple[str, bool, str]]:
    """res: ref -> resource for this request (report, observations, provenance, task, ...). The expected subject is
    the case's own requested Patient (HAPI holds more than one Patient)."""
    global PATIENT_REF
    PATIENT_REF = f"Patient/{(case.get('request') or {}).get('patient_id') or 'maria-beispiel'}"
    C: list[tuple[str, bool, str]] = []
    by_type: dict[str, list[dict]] = {}
    for r in res.values():
        by_type.setdefault(r["resourceType"], []).append(r)
    kind = case["request"]["doc_kind"]
    main_t = "DiagnosticReport" if kind == "lab_result" else "DocumentReference"
    mains = by_type.get(main_t, [])
    C.append((f"exactly one {main_t}", len(mains) == 1, str(len(mains))))
    if not mains:
        return C
    m = mains[0]
    C.append(("subject = requested Patient", (m.get("subject") or {}).get("reference") == PATIENT_REF,
              (m.get("subject") or {}).get("reference")))
    if kind == "lab_result":
        C.append(("DiagnosticReport.code LOINC 11502-2",
                  any(c.get("code") == "11502-2" for c in _coding(m.get("code"), LOINC)), str(m.get("code"))))
        C.append(("category LAB", any(c.get("code") == "LAB" for cat in m.get("category") or []
                                      for c in cat.get("coding") or []), ""))
        pf = m.get("presentedForm") or []
        C.append(("presentedForm present", bool(pf), ""))
        if pf:
            C += _att_ok(pf[0], original)
        want_c = case.get("conclusion")
        got_c = (m.get("conclusion") or "").strip()
        # the conclusion is filed NORMALISED; the raw converter markup is checked via the attachment
        squash = lambda t: " ".join((t or "").split())
        forms = {squash(want_c), squash(re.sub(r"^[A-Za-zÄÖÜäöü ]{2,20}:\s*", "", want_c))} if want_c else set()
        C.append(("conclusion = lab comment (normalised)" if want_c else "no invented conclusion",
                  squash(got_c) in forms if want_c else not got_c, f"{got_c[:60]!r}"))
        want_st = case.get("status") or "final"
        C.append((f"DiagnosticReport.status = {want_st}", m.get("status") == want_st, m.get("status")))
        t = case.get("times") or {}
        if t.get("effective"):
            ok, d = _same_instant(m.get("effectiveDateTime"), t["effective"])
            C.append(("DiagnosticReport.effective (Vienna offset)", ok, d))
        if t.get("issued"):
            ok, d = _same_instant(m.get("issued"), t["issued"])
            C.append(("DiagnosticReport.issued (Vienna offset)", ok, d))
        C += _observations(case, m, res)
    else:
        C.append(("type LOINC 11490-0", any(c.get("code") == "11490-0" for c in _coding(m.get("type"), LOINC)), ""))
        C.append(("category 18842-5", any(c.get("code") == "18842-5" for cat in m.get("category") or []
                                          for c in cat.get("coding") or []), ""))
        cont = m.get("content") or []
        C.append(("content attachment present", bool(cont), ""))
        if cont:
            C += _att_ok(cont[0].get("attachment") or {}, original)
        per = case.get("period")
        if per:
            p = (m.get("context") or {}).get("period") or {}
            C.append(("context.period = stay", (p.get("start") or "")[:10] == per[0] and (p.get("end") or "")[:10] == per[1],
                      f"{p}"))
        if by_type.get("MedicationStatement"):
            C.append(("no MedicationStatements (not filed by design)", False, str(len(by_type["MedicationStatement"]))))
    C += _provenance(case, m, res, by_type)
    C += _task(case, m, by_type)
    # one transaction: same lastUpdated across report, observations, provenance
    group = [m, *by_type.get("Observation", []), *by_type.get("Provenance", [])]
    stamps = {(g.get("meta") or {}).get("lastUpdated") for g in group}
    C.append(("filed in one transaction (one lastUpdated)", len(stamps) == 1, f"{len(stamps)} distinct"))
    bad = [(ref_of(r), p, v) for r in res.values() if r["resourceType"] != "Patient"
           for p, v in datetimes(r) if not tz_ok(v)]
    C.append(("every dateTime carries the Europe/Vienna offset", not bad, "; ".join(f"{a}.{b}={c}" for a, b, c in bad[:4])))
    return C


def _observations(case: dict, dr: dict, res: dict) -> list[tuple[str, bool, str]]:
    C = []
    want = case.get("obs") or []
    refs = [x.get("reference") for x in dr.get("result") or []]
    obs = [res.get(r) for r in refs]
    obs = [o for o in obs if o]
    C.append(("one Observation per row", len(obs) == len(want) == len(refs), f"{len(obs)}/{len(want)}"))
    pool = list(obs)
    rows_ok = 0
    for w in want:
        def match(o):
            codes = [c.get("code") for c in _coding(o.get("code"), LOINC)]
            if w["loinc"]:
                return w["loinc"] in codes
            return fold((o.get("code") or {}).get("text") or "") == fold(w["test"])
        cand = [o for o in pool if match(o)]
        if w.get("pending") or w.get("absent"):
            if not cand:
                C.append((f"row {w['test']}: found", False, "pending/absent row"))
                continue
            o = cand[0]
            pool.remove(o)
            vals = [k for k in o if k.startswith("value")]
            if w.get("pending"):
                ok = o.get("status") == "registered" and not vals
                d = f"status {o.get('status')}, value keys {vals}"
            else:
                dar = [c.get("code") for c in (o.get("dataAbsentReason") or {}).get("coding") or []]
                ok = "error" in dar and not vals and o.get("status") == "final"
                d = f"dataAbsentReason {dar}, status {o.get('status')}"
            ok = ok and bool(o.get("note")) and "valueQuantity" not in o
            rr = (o.get("referenceRange") or [{}])[0]
            for k in ("low", "high"):  # the unit lives in the reference range
                got_v = (rr.get(k) or {}).get("value")
                if k in w and (got_v is None or abs(float(got_v) - w[k]) > 1e-9):
                    ok, d = False, d + f"; referenceRange.{k} {got_v} != {w[k]}"
            rows_ok += ok
            if not ok:
                C.append((f"row {w['test']} ({'pending' if w.get('pending') else 'not determinable'})", False, d))
            continue
        if "value" in w:  # disambiguate duplicates (e.g. platelets in two panels)
            cand.sort(key=lambda o: abs(((o.get("valueQuantity") or {}).get("value") or 1e18) - w["value"]))
        if not cand:
            C.append((f"row {w['test']}: found", False, w.get("loinc") or "no code"))
            continue
        o = cand[0]
        pool.remove(o)
        errs = []
        if not w["loinc"] and _coding(o.get("code"), LOINC):
            errs.append("guessed a LOINC code")
        if not (o.get("code") or {}).get("text"):
            errs.append("no code.text (lab label)")
        q = o.get("valueQuantity") or {}
        if "value" in w:
            if q.get("value") is None or abs(float(q["value"]) - w["value"]) > 1e-9:  # 0.0 is a value
                errs.append(f"value {q.get('value')} != {w['value']}")
            if (q.get("comparator") or None) != w["comparator"]:
                errs.append(f"comparator {q.get('comparator')} != {w['comparator']}")
            if w["ucum"]:
                if q.get("system") != UCUM or q.get("code") not in w["ucum"]:
                    errs.append(f"UCUM {q.get('system')}|{q.get('code')} not in {w['ucum']}")
            elif q.get("system") == UCUM and q.get("code") not in (None, "1", "{INR}", "{ratio}"):
                errs.append(f"invented unit {q.get('code')}")
        else:
            txt = o.get("valueString") or (o.get("valueCodeableConcept") or {}).get("text") or \
                next((c.get("display") for c in (o.get("valueCodeableConcept") or {}).get("coding") or []), None)
            if fold(txt or "") != fold(w["text"]):
                errs.append(f"text {txt!r} != {w['text']!r}")
        rr = (o.get("referenceRange") or [{}])[0]
        for k in ("low", "high"):
            got_v = (rr.get(k) or {}).get("value")
            if k in w and (got_v is None or abs(float(got_v) - w[k]) > 1e-9):
                errs.append(f"referenceRange.{k} {(rr.get(k) or {}).get('value')} != {w[k]}")
        interp = [c.get("code") for i in o.get("interpretation") or [] for c in i.get("coding") or []]
        if w["flag"] is None and interp:
            errs.append(f"derived interpretation {interp} (source states none)")
        elif w["flag"] and w["flag"] not in interp:  # v3: a flag the lab wrote is filed (with the lab's text)
            errs.append(f"interpretation {interp} != stated {w['flag']}")
        if (o.get("subject") or {}).get("reference") != PATIENT_REF:
            errs.append("subject")
        t = (case.get("times") or {}).get("effective")
        if t:
            ok, d = _same_instant(o.get("effectiveDateTime"), t)
            if not ok:
                errs.append(f"effective {d}")
        rows_ok += not errs
        if errs:
            C.append((f"row {w['test']}", False, "; ".join(errs)))
    C.append(("rows exactly as sent", rows_ok == len(want), f"{rows_ok}/{len(want)}"))
    return C


def _provenance(case, main, res, by_type) -> list[tuple[str, bool, str]]:
    C = []
    provs = [p for p in by_type.get("Provenance", []) if ref_of(main) in [t.get("reference") for t in p.get("target") or []]]
    C.append(("Provenance targets the report", bool(provs), str(len(by_type.get("Provenance", [])))))
    if not provs:
        return C
    p = provs[0]
    targets = {t.get("reference") for t in p.get("target") or []}
    filed = {ref_of(r) for r in res.values() if r["resourceType"] in ("DiagnosticReport", "Observation", "DocumentReference")}
    C.append(("Provenance.target covers every filed resource", filed <= targets, f"missing {sorted(filed - targets)[:3]}"))
    agents = p.get("agent") or []
    roles = {a_c.get("code") for a in agents for t in [a.get("type") or {}] for a_c in t.get("coding") or []}
    names = case.get("provenance") or []
    blob = lambda a: fold(str(a))
    if len(names) == 2:
        au = [a for a in agents if any(c.get("code") == "author" for c in (a.get("type") or {}).get("coding") or [])]
        inf = [a for a in agents if any(c.get("code") == "informant" for c in (a.get("type") or {}).get("coding") or [])]
        C.append(("Provenance author = document signatory", any(fold(names[0]) in blob(a) for a in au), names[0]))
        C.append(("Provenance informant = eID-verified sender", any(fold(names[1]) in blob(a) for a in inf), names[1]))
    elif names:
        C.append(("Provenance names the sender", any(fold(names[0]) in blob(a) for a in agents), names[0]))
    C.append(("Provenance has an informant (verified sender)", "informant" in roles, str(sorted(roles))))
    sig = p.get("signature") or []
    C.append(("Provenance.signature application/jose", any(s.get("sigFormat") == "application/jose" for s in sig), ""))
    pol = [str(x) for x in p.get("policy") or []]
    C.append(("Provenance.policy marks the SIMULATED eID (eid-simulated, never the eIDAS LoA URI)",
              any(x.rstrip("/").endswith("/eid-simulated") for x in pol) and not any("eidas.europa.eu/LoA" in x for x in pol),
              str(pol)))
    pf = (main.get("presentedForm") or [{}])[0] if main["resourceType"] == "DiagnosticReport" else \
        ((main.get("content") or [{}])[0].get("attachment") or {})
    digest = hashlib.sha256(base64.b64decode(pf.get("data") or "")).digest()
    forms = {digest.hex(), base64.urlsafe_b64encode(digest).decode().rstrip("="), base64.b64encode(digest).decode()}
    ents = [e for e in p.get("entity") or [] if e.get("role") == "source"]
    C.append(("Provenance.entity[source] = SHA-256 of upload (hex or RFC 6920 ni:)",
              any(f in str((e.get("what") or {}).get("identifier")) for e in ents for f in forms), digest.hex()[:12]))
    claims = _jws_claims(sig)
    C.append(("Provenance.signature covers content_sha256 of this upload",
              bool(claims) and claims.get("content_sha256") == digest.hex(), str((claims or {}).get("content_sha256"))[:16]))
    C.append(("Provenance.signature covers the targets (same number)",
              bool(claims) and len(claims.get("targets") or []) == len(p.get("target") or []),
              f"{len((claims or {}).get('targets') or [])} vs {len(p.get('target') or [])}"))
    return C


def _jws_claims(sigs: list[dict]) -> dict | None:
    """Payload of the first application/jose signature (base64 of a compact JWS), or None."""
    for sg in sigs:
        if sg.get("sigFormat") != "application/jose":
            continue
        try:
            tok = base64.b64decode(sg.get("data") or "").decode("ascii")
            payload = tok.split(".")[1]
            import json
            return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, IndexError, UnicodeDecodeError):
            return None
    return None


def _task(case, main, by_type) -> list[tuple[str, bool, str]]:
    tasks = by_type.get("Task", [])
    if not tasks:
        return [("Task for the request", False, "none")]
    t = tasks[0]
    outs = [str(o) for o in t.get("output") or []]
    return [("Task for the request", True, ref_of(t)),
            ("Task.status completed", t.get("status") == "completed", t.get("status")),
            ("Task.for = Patient", (t.get("for") or {}).get("reference") == PATIENT_REF, str(t.get("for"))),
            ("Task.output -> report", any(ref_of(main) in o for o in outs), "")]


TRANSITIONS = {"draft": {"requested", "ready"}, "requested": {"received", "accepted", "in-progress", "on-hold",
               "completed", "rejected", "cancelled", "ready"}, "ready": {"in-progress", "on-hold", "completed", "rejected",
               "cancelled", "requested"}, "received": {"accepted", "rejected", "in-progress", "on-hold", "completed"},
               "accepted": {"in-progress", "on-hold", "completed", "rejected"}, "in-progress": {"on-hold", "completed",
               "failed", "rejected", "requested"}, "on-hold": {"in-progress", "completed", "rejected", "cancelled",
               "requested"}}


def _reachable(a: str, b: str) -> bool:
    seen, todo = set(), [a]
    while todo:
        x = todo.pop()
        for y in TRANSITIONS.get(x, ()):
            if y == b:
                return True
            if y not in seen:
                seen.add(y)
                todo.append(y)
    return False


FINAL_TASK = {"completed", "rejected", "cancelled", "failed"}


def task_history_ok(versions: list[dict], final: set[str]) -> tuple[bool, str]:
    """versions newest first (HAPI _history). Superseded queued writes may be coalesced by the service (a fast
    answer can skip 'requested' on HAPI), so each step must be REACHABLE in the FHIR Task state machine (not
    necessarily adjacent), nothing may follow a final state, and the last status must be in `final`."""
    seq = [v.get("status") for v in reversed(versions)]
    dedup = [s for i, s in enumerate(seq) if i == 0 or s != seq[i - 1]]
    if not dedup:
        return False, "no versions"
    legal = all(_reachable(a, b) for a, b in zip(dedup, dedup[1:])) and not any(s in FINAL_TASK for s in dedup[:-1])
    return legal and dedup[-1] in final, " -> ".join(dedup)

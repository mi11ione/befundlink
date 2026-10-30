"""Does prove() catch a wrong extraction? Corrupt the BUILT FHIR the way an AI extractor plausibly would
(a digit, a unit, a code, a comparator, a range bound, two swapped values, a dropped or invented row, a
shifted date, the wrong patient, one changed word, the wrong author) and count how often prove() reports a
difference against the source document.

Every corruption is applied alone, to a deep copy of an honest build, and kept internally consistent (a
dropped or invented row also updates DiagnosticReport.result and Provenance.target; changed attachment text
gets a fresh SHA-1 and size), so what has to catch it is the content diff, not a dangling reference.

    uv run python tests/prove_gate.py      # offline, no server, no validator

Prints {"documents", "corruptions", "detected", "undetected", "by_kind"}; exit code 1 if anything is undetected.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path[:0] = [str(ROOT), str(TESTS)]

from fhirmap import build_resources, parse_upload, prove, same_person  # noqa: E402
from jury.model import CTX, FILE  # noqa: E402

CORPUS = TESTS / "jury" / "corpus"
SCEN = Path(os.environ.get("PGK_SCENARIO", ROOT.parent / "track2" / "scenario"))
SCEN_DOCS = SCEN / "documents" if (SCEN / "documents").is_dir() else CORPUS / "_scenario"

# build arguments as in fhirmap/tests/test_fhirmap.py
RECEIVED = "2026-10-29T10:05:00+01:00"
EVA = {"given_name": "Eva", "family_name": "Brandl", "birthDate": "1975-05-05", "bpk": "SYNTH-BPK-GH-EVA",
       "loa": "high", "assertion": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJldmEifQ.c3ludGg",
       "verified_at": "2026-10-29T10:02:00+01:00"}
KERN = {"given_name": "Stefan", "family_name": "Kern", "birthDate": "1980-02-02", "bpk": "SYNTH-BPK-GH-KERN",
        "loa": "high", "assertion": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJrZXJuIn0.c3ludGg"}
REQ_LAB = {"request_id": "req-lab-1", "requester_mxid": "@lena.hofer:klinikum.example", "doc_kind": "lab_result",
           "recipient": {"name": "Dr. Eva Brandl", "role": "Laborärztin", "org": "Labor Kanalstraße"},
           "created_at": "2026-10-29T09:00:00+01:00", "expires_at": "2026-11-05T09:00:00+01:00"}
REQ_REHA = {"request_id": "req-reha-1", "requester_mxid": "@lena.hofer:klinikum.example",
            "doc_kind": "discharge_report",
            "recipient": {"name": "Stefan Kern", "role": "Entlassungsmanagement", "org": "Reha Waldblick"},
            "created_at": "2026-10-29T09:00:00+01:00"}

MARIA = {"resourceType": "Patient", "id": "maria-beispiel", "name": [{"family": "Beispiel", "given": ["Maria"]}],
         "gender": "female", "birthDate": "1961-03-12"}
MAX = {"resourceType": "Patient", "id": "max-beispiel", "name": [{"family": "Beispiel", "given": ["Max"]}],
       "gender": "male", "birthDate": "1960-01-01"}


# --------------------------------------------------------------------------- documents

def documents() -> list[tuple[str, str, bytes]]:
    """(label, doc_kind, raw bytes): every FILE case of the jury corpus, then the scenario documents."""
    m = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    out = [(c["id"], CTX[c["ctx"]].doc_kind, (CORPUS / c["file"]).read_bytes())
           for c in m["cases"] if c["expect"] == FILE]
    for f in sorted(SCEN_DOCS.glob("*.md")):
        out.append((f"scenario/{f.stem}", "lab_result" if f.name.startswith("lab-") else "discharge_report",
                    f.read_bytes()))
    return out


def honest(kind: str, raw: bytes):
    """(parsed, resources, the requested Patient): built exactly as the service would file it."""
    p = parse_upload(raw, kind)
    patient = MAX if same_person(p, MAX) and not same_person(p, MARIA) else MARIA   # FILE cases: Maria was asked for
    ref = f"Patient/{patient['id']}"
    res = (build_resources(p, ref, REQ_LAB, EVA, RECEIVED) if p.kind == "lab_result"
           else build_resources(p, ref, REQ_REHA, KERN, RECEIVED))
    return p, res, patient


# --------------------------------------------------------------------------- corruption helpers

def _one(res, t):
    return next(r for r in res if r["resourceType"] == t)


def _obs(res):
    """The DiagnosticReport's result Observations, in report order."""
    dr = next((r for r in res if r["resourceType"] == "DiagnosticReport"), None)
    if dr is None:
        return []
    ix = {f"urn:uuid:{r['id']}": r for r in res}
    return [ix[x["reference"]] for x in dr.get("result", [])]


def _num(o):
    v = (o.get("valueQuantity") or {}).get("value")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _like(old, d: Decimal):
    return float(d) if isinstance(old, float) else (int(d) if d == d.to_integral() else float(d))


def _nudge(v):
    """The last printed digit +1: 1.0 -> 1.1, 31 -> 32, 0.25 -> 0.26."""
    d = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    return _like(v, d + Decimal(1).scaleb(d.as_tuple().exponent))


def _shift_day(s: str | None) -> str | None:
    if not s:
        return s
    if "T" in s:
        return (datetime.fromisoformat(s) + timedelta(days=1)).isoformat()
    return (datetime.fromisoformat(s) + timedelta(days=1)).date().isoformat()


def _drop(res, victim):
    """Remove one resource everywhere it is referenced (DiagnosticReport.result, Provenance.target)."""
    ref = f"urn:uuid:{victim['id']}"
    out = [r for r in res if r is not victim]
    for r in out:
        if r["resourceType"] == "DiagnosticReport":
            r["result"] = [x for x in r["result"] if x["reference"] != ref]
        if r["resourceType"] == "Provenance":
            r["target"] = [x for x in r["target"] if x["reference"] != ref]
    return out


def _author_practitioner(res):
    """The document author's Practitioner, followed the way prove() reads it."""
    ix = {f"urn:uuid:{r['id']}": r for r in res}
    head = res[0]
    refs = head.get("resultsInterpreter") or head.get("author") or []
    for x in refs:
        r = ix.get(x.get("reference"))
        if r and r["resourceType"] == "PractitionerRole":
            return ix.get(r["practitioner"]["reference"])
        if r and r["resourceType"] == "Practitioner":
            return r
    return None


def _attachment(res):
    head = res[0]
    return (head.get("presentedForm") or [None])[0] or ((head.get("content") or [{}])[0].get("attachment"))


_NEG = re.compile(rb"\b(no|not|without|normal|nicht|kein|keine|ohne|unauff\xc3\xa4llig)\b", re.I)
_WORD = re.compile(rb"\b[A-Za-z]{6,}\b")


def _one_word(raw: bytes) -> bytes:
    """Change exactly one word: a negation / 'normal' if there is one (the dangerous misread), else a long word."""
    body_at = raw.find(b"\n", raw.find(b"Patient")) + 1          # leave the header lines alone
    m = _NEG.search(raw, body_at) or _WORD.search(raw, body_at) or _WORD.search(raw)
    new = {b"no": b"a", b"not": b"now", b"without": b"with", b"normal": b"raised", b"nicht": b"nichts",
           b"kein": b"ein", b"keine": b"eine", b"ohne": b"mit"}.get(m.group(0).lower(), m.group(0)[::-1])
    if new == m.group(0):
        new = m.group(0) + b"s"
    return raw[:m.start()] + new + raw[m.end():]


# --------------------------------------------------------------------------- corruptions

def corruptions(parsed, res, patient):
    """Yield (kind, target, corrupted resources). Each is ONE plausible extraction error on a deep copy."""
    obs = _obs(res)
    other_pt = MAX if patient is MARIA else MARIA

    def fork():
        t = copy.deepcopy(res)
        return t, _obs(t)

    for i, o in enumerate(obs):
        name = o.get("code", {}).get("text") or str(i)
        v = _num(o)
        if v is not None:
            t, to = fork()
            to[i]["valueQuantity"]["value"] = _nudge(v)
            yield "value_last_digit", name, t
            if v != 0:
                t, to = fork()
                to[i]["valueQuantity"]["value"] = _like(v, (Decimal(repr(v)) if isinstance(v, float)
                                                            else Decimal(v)) * 10)
                yield "value_decimal_shift_x10", name, t
        if "valueString" in o:
            t, to = fork()
            to[i]["valueString"] = {"negative": "positive", "positive": "negative"}.get(
                o["valueString"].strip().lower(), o["valueString"] + " (repeat)")
            yield "value_text_changed", name, t
        q = o.get("valueQuantity")
        if q and (q.get("unit") or q.get("code")):
            t, to = fork()
            alt = {"g/L": ("g/dL", "g/dL"), "mmol/L": ("mg/dL", "mg/dL"), "mg/dL": ("mmol/L", "mmol/L"),
                   "s": ("min", "min"), "10*9/L": ("10^6/L", "10*6/L"), "{INR}": ("%", "%"),
                   "%": ("{ratio}", "{ratio}"), "umol/L": ("mg/dL", "mg/dL")}.get(q.get("code"), ("mg/dL", "mg/dL"))
            to[i]["valueQuantity"]["unit"], to[i]["valueQuantity"]["code"] = alt
            yield "unit_changed", name, t
        if q and q.get("comparator"):
            t, to = fork()
            del to[i]["valueQuantity"]["comparator"]
            yield "comparator_dropped", name, t
        rr = (o.get("referenceRange") or [{}])[0]
        for bound in ("high", "low"):
            b = (rr.get(bound) or {}).get("value")
            if b is not None:
                t, to = fork()
                to[i]["referenceRange"][0][bound]["value"] = _nudge(b)
                yield f"ref_{bound}_changed", name, t
        codes = [(x.get("code", {}).get("coding") or [{}])[0].get("code") for x in obs]
        mine = codes[i]
        j = next((k for k, c in enumerate(codes) if c and c != mine), None)
        if mine and j is not None:
            t, to = fork()
            to[i]["code"]["coding"] = copy.deepcopy(obs[j]["code"]["coding"])   # the test name stays
            yield "loinc_swapped_to_other_row", name, t
        if i + 1 < len(obs):
            a, b = _num(o), _num(obs[i + 1])
            if a is not None and b is not None and Decimal(repr(a)) != Decimal(repr(b)):
                t, to = fork()
                to[i]["valueQuantity"]["value"], to[i + 1]["valueQuantity"]["value"] = b, a
                yield "values_swapped_with_next_row", name, t
        t, to = fork()
        yield "row_dropped", name, _drop(t, to[i])

    if obs:
        t, to = fork()
        extra = copy.deepcopy(to[-1])
        extra["id"] = "00000000-0000-4000-8000-0000000000ee"
        extra["code"] = {"coding": [{"system": "http://loinc.org", "code": "2160-0",
                                     "display": "Creatinine [Mass/volume] in Serum or Plasma"}], "text": "Creatinine"}
        extra["valueQuantity"] = {"value": 0.9, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"}
        extra.pop("valueString", None)
        extra.pop("dataAbsentReason", None)
        t.insert(t.index(to[-1]) + 1, extra)
        ref = {"reference": f"urn:uuid:{extra['id']}"}
        _one(t, "DiagnosticReport")["result"].append(ref)
        _one(t, "Provenance")["target"].append(dict(ref))
        yield "invented_extra_observation", "-", t

        t, to = fork()
        dr = _one(t, "DiagnosticReport")
        dr["effectiveDateTime"] = _shift_day(dr.get("effectiveDateTime"))
        for x in to:
            if "effectiveDateTime" in x:
                x["effectiveDateTime"] = _shift_day(x["effectiveDateTime"])
        yield "sample_time_plus_one_day", "-", t
    else:
        per = (res[0].get("context") or {}).get("period")
        if per and per.get("start"):
            t, _ = fork()
            t[0]["context"]["period"]["start"] = _shift_day(per["start"])
            yield "stay_start_plus_one_day", "-", t

    t, _ = fork()                                         # the wrong patient, on every resource
    for r in t:
        if "subject" in r:
            r["subject"] = {"reference": f"Patient/{other_pt['id']}", "display": r["subject"].get("display")}
    yield "subject_other_patient", "-", t

    if _attachment(res) is not None:
        t, _ = fork()
        att = _attachment(t)
        raw = _one_word(base64.b64decode(att["data"]))
        att.update(data=base64.b64encode(raw).decode(), size=len(raw),
                   hash=base64.b64encode(hashlib.sha1(raw).digest()).decode())   # re-hashed: looks intact
        yield "attached_text_one_word", "-", t

    if _author_practitioner(res) is not None:
        t, _ = fork()
        pr = _author_practitioner(t)
        given, family = ("Markus", "Feld") if "Feld" not in (_name(pr) or "") else ("Johanna", "Leitner")
        pr["name"] = [{"text": f"Dr. {given} {family}", "family": family, "given": [given], "prefix": ["Dr."]}]
        yield "author_changed", "-", t
        if res[0]["resourceType"] == "DocumentReference":
            sender = next((r for r in res if r["resourceType"] == "Practitioner"            # the eID sender (bPK)
                           and any("bpk" in (i.get("system") or "") for i in r.get("identifier") or [])), None)
            if sender is not None and sender is not _author_practitioner(res):
                t, _ = fork()
                t[0]["author"][0] = {"reference": f"urn:uuid:{sender['id']}", "display": _name(sender)}
                yield "author_is_the_sender", "-", t


def _name(r):
    return (r.get("name") or [{}])[0].get("text")


def _diffs(pr) -> list[str]:
    return [f["field"] for f in pr["fields"] if not f["equal"]] + [
        f"row {r['index']}: {r['status']}" + (f" ({', '.join(d['field'] for d in r['diffs'])})" if r["diffs"] else "")
        for r in pr["rows"] if r["status"] != "equal"]


# --------------------------------------------------------------------------- run

def run() -> dict:
    t0 = time.monotonic()
    docs = documents()
    n = k = 0
    undetected, honest_fail, by_kind = [], [], {}
    for label, kind, raw in docs:
        p, res, patient = honest(kind, raw)
        base = prove(p, res, patient=patient)
        if not base["all_equal"]:
            honest_fail.append({"document": label, "diffs": _diffs(base)})
            continue
        for ck, target, bad in corruptions(p, res, patient):
            pr = prove(p, bad, patient=patient)
            n += 1
            hit = not pr["all_equal"]
            k += hit
            s = by_kind.setdefault(ck, [0, 0])
            s[0] += hit
            s[1] += 1
            if not hit:
                undetected.append({"document": label, "corruption": ck, "target": target})
    out = {"documents": len(docs), "corruptions": n, "detected": k, "undetected": undetected,
           "by_kind": {ck: f"{a}/{b}" for ck, (a, b) in sorted(by_kind.items())},
           "seconds": round(time.monotonic() - t0, 2)}
    if honest_fail:
        out["honest_build_not_equal"] = honest_fail
    return out


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0 if not r["undetected"] and not r.get("honest_build_not_equal") else 1)

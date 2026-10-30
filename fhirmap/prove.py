"""prove(): rebuild the answer's content from the FHIR resources alone and diff it
against the parsed source. "What was sent is exactly what was filed."

Nothing here reads the Parsed object while rebuilding: the FHIR side is read only
from ``resources``; the comparison is a separate step.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime
from decimal import Decimal

from .build import ni_uri
from .identity import loose_name_equal
from .parse import DISCHARGE, LAB, Parsed


def _dec(x):
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)
    return x


def _dt(x):
    if not x:
        return None
    try:
        return datetime.fromisoformat(x) if "T" in x else x
    except ValueError:
        return x


def _ix(resources):
    by_url = {}
    for r in resources:
        by_url[f"urn:uuid:{r.get('id')}"] = r      # built here (urn:uuid) or a server version (Type/id)
        by_url[f"{r['resourceType']}/{r.get('id')}"] = r
    return by_url


def _name(res) -> str | None:
    if not res:
        return None
    if res["resourceType"] == "Organization":
        return res.get("name")
    n = (res.get("name") or [{}])[0]
    return n.get("text") or " ".join([*n.get("prefix", []), *n.get("given", []), n.get("family", "")]).strip()


def _decode(att: dict) -> tuple[bytes | None, bool]:
    """(the attachment's raw bytes, sha1 + size ok)"""
    if not att or "data" not in att:
        return None, False
    raw = base64.b64decode(att["data"])
    ok = att.get("hash") == base64.b64encode(hashlib.sha1(raw).digest()).decode() and att.get("size", len(raw)) == len(raw)
    return raw, ok


def _rebuild_lab(resources) -> dict:
    ix = _ix(resources)
    dr = next(r for r in resources if r["resourceType"] == "DiagnosticReport")
    roles = {f"urn:uuid:{r['id']}": r for r in resources if r["resourceType"] == "PractitionerRole"}
    author = None
    for ref in dr.get("resultsInterpreter", []):
        x = ix.get(ref.get("reference"))
        if x and x["resourceType"] == "PractitionerRole":
            prac = ix.get(x["practitioner"]["reference"])
            author = {"name": _name(prac), "role": (x.get("code") or [{}])[0].get("text")}
        elif x:
            author = {"name": _name(x), "role": None}
    org = next((_name(ix.get(p["reference"])) for p in dr.get("performer", [])
                if ix.get(p.get("reference"), {}).get("resourceType") == "Organization"), None)
    rows = []
    for ref in dr.get("result", []):
        o = ix.get(ref["reference"])
        if o is None:
            rows.append({"missing": ref["reference"]})
            continue
        q = o.get("valueQuantity") or {}
        rr = (o.get("referenceRange") or [{}])[0]
        coding = (o.get("code", {}).get("coding") or [{}])[0]
        interp = ((o.get("interpretation") or [{}])[0].get("coding") or [{}])[0].get("code")
        interp_text = (o.get("interpretation") or [{}])[0].get("text")
        if "valueQuantity" in o:
            value = _dec(q.get("value"))
        elif "valueString" in o:
            value = o["valueString"]
        else:
            value = None
        rows.append({
            "test": o.get("code", {}).get("text"), "value": value, "comparator": q.get("comparator"),
            # a row without a value (pending / not determinable) carries its unit only in referenceRange
            "unit": q.get("unit") if q else (rr.get("low") or rr.get("high") or {}).get("unit"),
            "ucum": q.get("code") if q else (rr.get("low") or rr.get("high") or {}).get("code"),
            "novalue": not q and "valueString" not in o, "loinc": coding.get("code"),
            "ref_low": _dec((rr.get("low") or {}).get("value")), "ref_high": _dec((rr.get("high") or {}).get("value")),
            "ref_text": rr.get("text"), "flag": interp, "flag_text": interp_text,
            "status": o.get("status"), "absent": ((o.get("dataAbsentReason") or {}).get("coding") or [{}])[0].get("code"),
            "notes": [n["text"] for n in o.get("note", [])],
            "effective": _dt(o.get("effectiveDateTime")), "issued": _dt(o.get("issued")),
            "subject": o.get("subject", {}).get("reference"),
        })
    text, hash_ok = _decode((dr.get("presentedForm") or [{}])[0])
    return {"subject": dr.get("subject", {}).get("reference"), "subject_display": dr.get("subject", {}).get("display"),
            "author": author, "org": org, "sample_time": _dt(dr.get("effectiveDateTime")),
            "report_time": _dt(dr.get("issued")), "comment": dr.get("conclusion"), "rows": rows,
            "status": dr.get("status"), "einsender": (dr.get("basedOn") or [{}])[0].get("display"),
            "text": text, "attachment_hash_ok": hash_ok, "_roles": roles}


def _rebuild_doc(resources) -> dict:
    ix = _ix(resources)
    d = next(r for r in resources if r["resourceType"] == "DocumentReference")
    author = org = None
    for ref in d.get("author", []):
        x = ix.get(ref.get("reference"))
        if x and x["resourceType"] == "Practitioner":
            role = next(((r.get("code") or [{}])[0].get("text") for r in resources
                         if r["resourceType"] == "PractitionerRole"
                         and r["practitioner"]["reference"] == f"urn:uuid:{x['id']}"), None)
            author = {"name": _name(x), "role": role}
        elif x and x["resourceType"] == "Organization":
            org = _name(x)
    text, hash_ok = _decode((d.get("content") or [{}])[0].get("attachment"))
    per = (d.get("context") or {}).get("period") or {}
    return {"subject": d.get("subject", {}).get("reference"), "subject_display": d.get("subject", {}).get("display"),
            "author": author, "org": org, "stay": {"start": per.get("start"), "end": per.get("end")} if per else None,
            "text": text, "attachment_hash_ok": hash_ok}


_ROW_FIELDS = ("test", "value", "comparator", "unit", "ucum", "loinc", "ref_low", "ref_high", "ref_text", "flag",
               "flag_text", "status", "absent", "notes")


def prove(parsed: Parsed, resources: list[dict], patient: dict | None = None,
          unchanged: list[dict] | None = None) -> dict:
    """Diff the FHIR content against the parsed source.

    ``patient``: optionally the FHIR Patient that ``subject`` points to; then the
    document's patient line is also checked against it with ``same_person``.
    Returns {kind, values_in_source, values_in_fhir, rows_in_source, rows_in_fhir,
    all_equal, rows: [...], fields: [...], summary}.
    """
    fields = []

    def check(name, src, fhir, equal=None):
        eq = (src == fhir) if equal is None else equal
        fields.append({"field": name, "source": _show(src), "fhir": _show(fhir), "equal": bool(eq)})

    rows_out = []
    as_text = not any(r["resourceType"] == "DiagnosticReport" for r in resources)
    if parsed.kind == LAB and not as_text:
        fb = _rebuild_lab(list(resources) + list(unchanged or []))   # unchanged: prior versions kept (corrections)
        kept = {f"Observation/{o.get('id')}" for o in unchanged or []}
    elif parsed.kind == DISCHARGE or as_text:
        fb = _rebuild_doc(resources)
    else:
        return {"kind": parsed.kind, "all_equal": False, "values_in_source": 0, "values_in_fhir": 0,
                "rows_in_source": 0, "rows_in_fhir": 0, "rows": [], "fields": [],
                "summary": "nothing to prove: kind unknown"}

    # patient
    pname = (parsed.patient or {}).get("name")
    if patient is not None:     # subject.display is the Patient's own name, never the document's spelling
        from .build import patient_display
        want = patient_display(patient)
        check("subject.display = the Patient's name", want, fb["subject_display"],
              fb["subject_display"] is None or fb["subject_display"] == want)
    subjects = {r.get("subject", {}).get("reference") for r in resources if "subject" in r}
    check("subject identical on every resource", 1, len(subjects))
    if patient is not None:
        check("patient line = filed Patient (name + birthDate)",
              f"{pname}, {(parsed.patient or {}).get('birthDate')}",
              f"{_name(patient) if patient.get('name') and isinstance(patient['name'][0], dict) else ''}, "
              f"{patient.get('birthDate')}", _patient_same(parsed, patient))
        check("subject points to the Patient", f"Patient/{patient.get('id')}", fb["subject"])
    # author / org
    a_src = parsed.author or {}
    a_fhir = fb["author"] or {}
    check("author.name", a_src.get("name"), a_fhir.get("name"),
          (not a_src.get("name") and not a_fhir.get("name")) or
          bool(a_src.get("name") and a_fhir.get("name") and loose_name_equal(a_src["name"], a_fhir["name"])))
    check("author.role", a_src.get("role"), a_fhir.get("role"))
    check("organisation", (parsed.org or {}).get("name") or a_src.get("org"), fb["org"])
    # original text
    from .build import original_bytes
    src_bytes = original_bytes(parsed)
    check("original text (attachment) byte-identical", f"{len(src_bytes)} bytes", 
          f"{len(fb['text'])} bytes" if fb["text"] is not None else None, fb["text"] == src_bytes)
    check("attachment SHA-1 and size", True, fb["attachment_hash_ok"])
    prov = next((r for r in resources if r["resourceType"] == "Provenance"), None)
    ni = ni_uri(src_bytes)
    src_ent = next((e for e in (prov or {}).get("entity", []) if e.get("role") == "source"), {})
    check("Provenance source SHA-256", ni, (src_ent.get("what") or {}).get("identifier", {}).get("value"))
    if prov:
        from .build import ref_of
        from .build import unversioned
        targets = {unversioned(t["reference"]) for t in prov.get("target", []) if not t["reference"].startswith("Task/")}
        filed = {ref_of(r) for r in resources if r is not prov}
        check("Provenance targets every filed resource", len(filed), len(targets & filed), targets == filed)
    else:
        check("Provenance present", True, False)

    n_src = n_fhir = 0
    if parsed.kind == LAB and not as_text:
        # independent count of value-like rows in the SOURCE text: a dropped row cannot hide
        src_rows = source_value_rows(parsed.text)
        check("every value row of the source is an Observation", src_rows,
              len([r for r in fb["rows"] if "missing" not in r]))
        check("sample time", _dt(parsed.sample_time), fb["sample_time"])
        rt = parsed.report_time if parsed.report_time and "T" in parsed.report_time else None
        check("report time", _dt(rt), fb["report_time"])
        check("comment (conclusion)", parsed.comment, fb["comment"])
        check("report status", parsed.report_status, fb["status"])
        from .build import _einsender_text
        check("Einsender (basedOn)", _einsender_text(parsed) if parsed.requester and parsed.requester.get("name")
              else None, fb["einsender"])
        src_rows, fhir_rows = parsed.lab, fb["rows"]
        n_src, n_fhir = len(src_rows), len([r for r in fhir_rows if "missing" not in r])
        dr_refs = [x.get("reference") for x in next(r for r in resources if r["resourceType"] == "DiagnosticReport")
                   .get("result", [])]
        for i in range(max(len(src_rows), len(fhir_rows))):
            s = src_rows[i] if i < len(src_rows) else None
            f = fhir_rows[i] if i < len(fhir_rows) else None
            if s is None:
                rows_out.append({"index": i, "test": f.get("test"), "status": "extra_in_fhir", "diffs": []})
                continue
            if f is None or "missing" in f:
                rows_out.append({"index": i, "test": s.get("test"), "status": "missing_in_fhir", "diffs": []})
                continue
            diffs = []
            for k in _ROW_FIELDS:
                sv = _dec(s.get(k)) if k in ("value", "ref_low", "ref_high") else s.get(k)
                fv = f.get(k)
                if k == "notes":
                    sv, fv = list(sv or []), list(fv or [])
                if k in ("unit", "ucum") and f.get("novalue") and fv is None and not (
                        s.get("ref_low") is not None or s.get("ref_high") is not None):
                    continue                          # value-less row without a numeric range: no unit to file
                if k == "unit" and sv is None and fv and f.get("ucum") == "{" + fv + "}":
                    fv = None    # unit text derived from the UCUM annotation ({INR} -> 'INR'); source cell was empty
                if k == "status" and sv is None:      # status derived from the report status
                    sv = "preliminary" if parsed.report_status == "preliminary" else "final"
                    if fv == "corrected" and parsed.report_status in ("corrected", "amended"):
                        sv = "corrected"                  # a corrected row (build_correction)
                if k == "test" and sv is None:
                    sv = s.get("loinc") or "unnamed test"
                if sv != fv:
                    diffs.append({"field": k, "source": _show(sv), "fhir": _show(fv)})
            for k, src_t in (("effective", _dt(parsed.sample_time)), ("issued", _dt(rt))):
                if k == "issued" and i < len(dr_refs) and dr_refs[i] in kept:
                    continue                  # an unchanged prior Observation keeps its original issued time
                if f.get(k) != src_t:
                    diffs.append({"field": k, "source": _show(src_t), "fhir": _show(f.get(k))})
            if f.get("subject") != fb["subject"]:
                diffs.append({"field": "subject", "source": fb["subject"], "fhir": f.get("subject")})
            rows_out.append({"index": i, "test": s.get("test"), "value": _show(_dec(s.get("value"))),
                             "unit": s.get("unit"), "status": "equal" if not diffs else "different",
                             "diffs": diffs})
    else:
        st = parsed.stay if parsed.stay and parsed.stay.get("start") else None
        check("stay", {"start": st["start"], "end": st["end"]} if st else None, fb["stay"])
        n_src = n_fhir = 0

    all_equal = all(f["equal"] for f in fields) and all(r["status"] == "equal" for r in rows_out)
    vs = len(fields) + sum(len(_ROW_FIELDS) + 2 for _ in parsed.lab) if parsed.kind == LAB else len(fields)
    eq_count = sum(f["equal"] for f in fields) + sum(
        len(_ROW_FIELDS) + 2 - len(r["diffs"]) for r in rows_out if r["status"] in ("equal", "different"))
    if parsed.kind == LAB:
        summary = (f"{n_fhir}/{n_src} lab values filed, {sum(r['status'] == 'equal' for r in rows_out)}/{n_src} "
                   f"identical to the document; {eq_count}/{vs} checks equal; "
                   f"{'ALL EQUAL' if all_equal else 'DIFFERENCES FOUND'}")
    else:
        summary = (f"report text byte-identical: {'yes' if fb['text'] == src_bytes else 'NO'}; "
                   f"{eq_count}/{vs} checks equal; {'ALL EQUAL' if all_equal else 'DIFFERENCES FOUND'}")
    return {"kind": parsed.kind, "values_in_source": n_src, "values_in_fhir": n_fhir,
            "rows_in_source": n_src, "rows_in_fhir": n_fhir, "checks": vs, "checks_equal": eq_count,
            "all_equal": all_equal, "rows": rows_out, "fields": fields, "summary": summary}


_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_COMMENTISH = re.compile(r"^\s*(kommentar|bemerkung|bemerkungen|hinweis|anmerkung|note|notes|comment|comments|"
                         r"befundkommentar|remark)\b", re.I)


def source_value_rows(text: str) -> int:
    """Count table rows in the original text that carry a value: every '|'-row that is not a separator,
    not a header (the row right above a separator), not part of a 2-column key/value table, not a comment
    row and has at least two non-empty cells. Independent of the parser, so a row the parser dropped (a
    split table, a misread line) makes prove() fail."""
    import html as _h
    lines = [_h.unescape(ln) for ln in (text or "").replace("\r\n", "\n").split("\n")]
    lines = [re.sub(r"<!--.*?-->", "", ln) for ln in lines]
    is_row = lambda ln: "|" in ln and ln.strip() != ""
    n = 0
    i = 0
    while i < len(lines):
        if not is_row(lines[i]) or not (lines[i].strip().startswith("|") or (
                i + 1 < len(lines) and _SEP.match(lines[i + 1]) and "-" in lines[i + 1])):
            i += 1
            continue
        block = []
        while i < len(lines) and is_row(lines[i]):
            block.append(lines[i].strip())
            i += 1
        if not block[0].startswith("|") and not any(_SEP.match(r) and "-" in r for r in block):
            continue
        cells_of = lambda r: [c.strip().strip("*_` ") for c in r.strip().strip("|").split("|")]
        head_idx = {k - 1 for k, r in enumerate(block) if _SEP.match(r) and "-" in r and k > 0}
        rows = [r for k, r in enumerate(block) if not (_SEP.match(r) and "-" in r) and k not in head_idx]
        if block and len(cells_of(block[0])) == 2:
            from .parse import _KEYMAP, _fold
            if sum(1 for r in block if not _SEP.match(r) and _KEYMAP.get(_fold(cells_of(r)[0]))) >= 2:
                continue                       # a 2-column 'key | value' header block, not values
        for r in rows:
            cells = cells_of(r)
            nonempty = [c for c in cells if c]
            if len(nonempty) < 2 or _COMMENTISH.match(nonempty[0]):
                continue
            n += 1
    if n == 0:
        n = _source_bullet_rows(lines)
    return n


_BULLET_ITEM = re.compile(r"^\s*[-*•+]\s+(.*\S)\s*$")
_DATEISH = re.compile(r"^\s*(?:\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\.?\s+[^\W\d_]{3,}\.?\s+\d{4})")


def _source_bullet_rows(lines: list[str]) -> int:
    """A report without any value table: count list items that carry a number and are not a header field
    ('- Geburtsdatum: ...', '- Abnahme: 27.10.2026 07:35') and not a date. Independent of the parser's list
    reader, so a dropped '- INR: 1,0 (Ref. 0,8–1,2)' makes prove() fail."""
    from .parse import QUALITATIVE_RESULT, _KEYMAP, _fold, normalise_leaders
    n = 0
    for ln in lines:
        m = _BULLET_ITEM.match(normalise_leaders(ln))
        if not m or "|" in ln:
            continue
        t = m[1].replace("**", "").replace("__", "")
        label, _, rest = t.partition(":")
        if not re.search(r"\d", t) and not (rest and re.match(QUALITATIVE_RESULT, rest.strip(), re.I)):
            continue
        if rest and (_KEYMAP.get(_fold(label)) or _DATEISH.match(rest)):
            continue
        if re.search(r"\b(tel|telefon|phone|fax|e-?mail|www|http)\b", t, re.I):
            continue
        n += 1
    return n


def _patient_same(parsed, patient) -> bool:
    """The same patient rule as decide() (incl. the 'Name:' header convention and two-digit years)."""
    from .decide import patient_decision
    pd = patient_decision(parsed, patient)
    return pd["outcome"] != "BLOCK" and pd.get("name") == "same" and pd.get("dob") == "same"


def _show(v):
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str) and len(v) > 120:
        return v[:60] + f"... ({len(v)} chars, sha256 {hashlib.sha256(v.encode()).hexdigest()[:12]})"
    return v

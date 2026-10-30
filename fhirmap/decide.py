"""One decision for an uploaded answer: FILE, HOLD or BLOCK (tests/jury/RULES.md).

    d = decide(parsed, request=..., patient=<FHIR Patient>, identity=<eID claims>, raw=<bytes>)
    d["outcome"]  -> "FILE" | "HOLD" | "BLOCK"   (precedence BLOCK > HOLD > FILE; every rule is evaluated)
    d["codes"]    -> rule codes, most severe first (EID_MISMATCH, WRONG_PATIENT, NAME_NEAR, ...)
    d["reasons"]  -> privacy-safe English sentences: they NEVER contain another patient's name or birth date
    d["details"]  -> for HOLD only: the near-miss values Dr. Hofer must decide on (near her own patient's data)

``patient_decision(parsed, patient)`` is the patient part alone (rules 4-9).

Active content (raw text, HTML comments excluded from tier 2):
  BLOCK ACTIVE_CONTENT          <script <iframe <object <embed <svg <math <form <meta <link <style, an on...=
                                handler, javascript:, vbscript:, data:text/html (RULES.md rule 3)
  HOLD  ACTIVE_CONTENT_SUSPECT  any other raw HTML tag (<b>, <br>, <img ...>), a Markdown image ![..](..),
                                a Markdown link or <autolink> whose target is not https (http:, mailto:,
                                relative '#x'), a bare non-https URL (http://...), or any of the BLOCK
                                patterns / an HTML tag hidden behind HTML entities or %-encoding
  FILE  https links in any form and e-mail autolinks (<name@host>); '<' / '>' used as comparators
Identity (rule 1, v2): see ``identity.identity_matches``.
"""
from __future__ import annotations

from .identity import (AMBIGUOUS, DIFFERENT, NEAR, SAME, _person_view, compare_dobs, compare_names,
                       has_foreign_script, identity_matches, rule_tokens, split_name)
import re

from .parse import DISCHARGE, LAB, UNKNOWN, Parsed

FILE, HOLD, BLOCK = "FILE", "HOLD", "BLOCK"
_RANK = {FILE: 0, HOLD: 1, BLOCK: 2}
MAX_BYTES = 1_000_000

_MSG = {
    "EID_MISMATCH": "the eID-verified person is not the person the request was sent to",
    "EMPTY": "the upload is empty",
    "OVERSIZE": "the upload is larger than 1 MB",
    "UNREADABLE": "the upload is not readable UTF-8 text or contains control characters",
    "ACTIVE_CONTENT": "the upload contains active content (HTML/script)",
    "ACTIVE_CONTENT_SUSPECT": "the upload contains HTML, an image or a non-https link",
    "WRONG_PATIENT": "the document concerns a different person (name or date of birth differ); nothing was filed",
    "OTHER_PATIENT_PRESENT": "the document also contains another person's data; nothing was filed",
    "SEX_MISMATCH": "the sex stated in the document differs from the requested patient",
    "ENCODING_UNREADABLE": "the file is not readable text (unknown encoding or a binary file)",
    "FLAG_CONFLICT": "a result carries two different lab flags",
    "SVNR_MISMATCH": "the social security number belongs to a different birth date; nothing was filed",
    "MULTIPLE_REPORTS": "the upload contains more than one report",
    "TABLE_SPLIT": "result rows lie outside the result table (split table)",
    "FUTURE_DATE": "a sample or report date lies in the future",
    "ORG_MISMATCH": "the report comes from a different organisation than the one asked",
    "STALE_REPORT": "the report is older than plausible for this request",
    "REPORT_INTERIM": "an interim report, not a discharge report",
    "CORRECTION_REVIEW": "a corrected report: the report it replaces must be retired (file as a correction)",
    "PATIENT_MISSING": "the document has no patient line",
    "PATIENT_INCOMPLETE": "the patient line lacks the name or a complete date of birth",
    "PATIENT_CONFLICT": "the document contains patient lines that disagree",
    "NAME_NEAR": "the patient name is similar but not identical to the requested patient",
    "DOB_NEAR": "the date of birth is similar but not identical (swapped or one digit off)",
    "DOB_AMBIGUOUS": "the date of birth is ambiguous (day/month order cannot be told)",
    "DOB_UNPARSEABLE": "the date of birth is not a real date",
    "KIND_MISMATCH": "the document type differs from the requested type",
    "KIND_UNKNOWN": "the document type is not recognised",
    "NO_RESULTS": "the lab report has no result table or no rows",
    "UNIT_MISSING": "a numeric result has no unit",
    "UNKNOWN_UNIT": "a unit has no defensible UCUM code",
    "LOINC_CHECKDIGIT": "a LOINC code fails its check digit",
    "LOINC_UNIT_DIMENSION": "a unit contradicts its LOINC code",
    "LOINC_UNKNOWN": "a LOINC code does not exist",
    "LOINC_AMBIGUOUS": "a row carries several LOINC codes",
    "VALUE_AMBIGUOUS": "a result value is ambiguous",
    "TABLE_MALFORMED": "a table row does not match its header",
    "REQUESTER_MISMATCH": "the document names a different orderer (information only)",
    "SENDER_CONTRADICTS_EID": "the document's 'Sent by' names someone other than the verified sender",
    "SENDER_CONFLICT": "the document has several different 'Sent by' lines",
    "IDENTITY_DOUBT": "the document doubts whose sample or document this is (labelling / mix-up)",
    "EXTRACTION_DISAGREE": "photo intake: the two independent AI readings of the photo/PDF differ",
    "EXTRACTION_UNREADABLE": "photo intake: part of the photo/PDF is unreadable, or it could not be read",
}
_PATIENT_CODES = {"PATIENT_MISSING", "PATIENT_INCOMPLETE", "PATIENT_CONFLICT", "DOB_UNPARSEABLE"}


def _same_tokens_any_order(name: str, req_name: str) -> bool:
    """Surname first without a comma ('Beispiel Maria') is the requested person
    when the name tokens are EXACTLY the requested ones in another order (no token added, missing, abbreviated
    or changed, Latin script only); patient_decision() also requires the birth date to be SAME and written in
    full (not a two-digit year)."""
    a, b = rule_tokens(name), rule_tokens(req_name)
    return len(a) >= 2 and sorted(a) == sorted(b) and a != b and not has_foreign_script(name)


def _req_person(patient) -> tuple[str, str | None]:
    return _person_view(patient) if patient is not None else ("", None)


def patient_decision(parsed: Parsed, patient) -> dict:
    """Rules 4-9: document patient line(s) vs the requested Patient.

    Returns {outcome, codes, reasons, name: same|near|different|None, dob: same|near|different|None,
    details}. 'Beispiel Maria' (no comma) is SAME only with an exact full birth date (v4), else NEAR;
    'Beispiel, Maria' is SAME; María = Maria;
    Märia is NEAR; an extra given name or a hyphenated surname is NEAR; a day/month swap is NEAR;
    any DIFFERENT name or DOB is BLOCK."""
    req_name, req_dob = _req_person(patient)
    found: dict[str, str] = {}
    out = {"name": None, "dob": None, "details": []}
    lines = [x for x in parsed.patient_lines if x.get("name") or x.get("dob_status") != "missing"]
    if not lines:
        found["PATIENT_MISSING"] = HOLD
    elif not req_name or not req_dob:
        found["PATIENT_INCOMPLETE"] = HOLD          # requested Patient itself incomplete: never auto-file
    else:
        verdicts = []
        for ln in lines:
            n = compare_names(ln["name"], req_name) if ln.get("name") else None
            if n == NEAR and ln.get("order_free") and sorted(rule_tokens(ln["name"])) == sorted(rule_tokens(req_name)) \
                    and not has_foreign_script(ln["name"]):
                n = SAME                                  # 'Name: Beispiel Maria' (header-block convention)
            st = ln.get("dob_status")
            dob = ln.get("birthDate") if st in ("ok", "two-digit") else (AMBIGUOUS if st == "ambiguous" else None)
            d = compare_dobs(dob, req_dob) if st != "missing" else None
            if st == "two-digit" and d != SAME:
                d, dob, st = NEAR, AMBIGUOUS, "ambiguous"   # '12.03.61': accepted only if it EQUALS the request
            if n == NEAR and d == SAME and st == "ok" and ln.get("name") and _same_tokens_any_order(ln["name"], req_name):
                n = SAME        # 'Beispiel Maria, *12.03.1961': exactly the requested tokens, exact DOB
            elif n == NEAR and d == SAME and st == "two-digit" and ln.get("name") \
                    and _same_tokens_any_order(ln["name"], req_name):
                # 'Beispiel Maria, 12.03.61': the name is right, the two-digit year is what keeps it held
                n, d, dob, st = SAME, NEAR, AMBIGUOUS, "ambiguous"
            verdicts.append((tuple(rule_tokens(ln.get("name") or "")), dob if st != "missing" else "-", n, d, st, ln))
        named = [v for v in verdicts if not (v[5].get("source") == "evidence" and not v[5].get("name"))]
        vague = [v for v in verdicts if v not in named and DIFFERENT in (v[2], v[3])]
        if vague:                                     # a birth date without a name: ambiguous, a human decides
            found["PATIENT_CONFLICT"] = HOLD
            out["other_person_present"] = True
        verdicts = named or verdicts
        if len({(v[0], v[1]) for v in verdicts}) > 1:
            out["other_person_present"] = True
            primary = [v for v in verdicts if v[5].get("source") != "evidence"]
            if all(DIFFERENT in (v[2], v[3]) for v in verdicts) or (
                    primary and all(DIFFERENT in (v[2], v[3]) for v in primary)):
                # the patient line(s) name someone else: prose elsewhere cannot re-assign the patient
                found["WRONG_PATIENT"] = BLOCK
            elif any(DIFFERENT in (v[2], v[3]) for v in verdicts):
                found["OTHER_PATIENT_PRESENT"] = BLOCK      # never fileable, not even by '!ablegen'
            else:
                found["PATIENT_CONFLICT"] = HOLD
        if len({(v[0], v[1]) for v in verdicts}) <= 1:
            _, dob, n, d, st, ln = verdicts[0]
            out["name"], out["dob"] = n, d
            if DIFFERENT in (n, d):
                found["WRONG_PATIENT"] = BLOCK
            if n is None or d is None:
                found["PATIENT_INCOMPLETE"] = HOLD
            if n == NEAR:
                found["NAME_NEAR"] = HOLD
                out["details"].append({"field": "name", "document": ln.get("name"), "request": req_name})
            if d == NEAR:
                found["DOB_AMBIGUOUS" if st == "ambiguous" else "DOB_UNPARSEABLE" if st == "unparseable"
                      else "DOB_NEAR"] = HOLD
                out["details"].append({"field": "birthDate", "document": ln.get("birthDate"), "request": req_dob,
                                       "document_text": ln.get("birthDate_raw")})
            sv = (parsed.patient or {}).get("svnr")
            if sv and req_dob and sv["ddmmyy"] != f"{req_dob[8:10]}{req_dob[5:7]}{req_dob[2:4]}":
                found["SVNR_MISMATCH"] = BLOCK            # the SVNR encodes another birth date
                out["other_person_present"] = True
            doc_sex = (parsed.patient or {}).get("gender") or ln.get("gender")
            req_sex = (patient or {}).get("gender") if isinstance(patient, dict) else None
            if doc_sex and req_sex and req_sex in ("male", "female") and doc_sex != req_sex:
                found["SEX_MISMATCH"] = HOLD
                out["details"].append({"field": "gender", "document": doc_sex, "request": req_sex})
    return _result(found, out)


_FIELD_DE = {"name": "Name", "birthDate": "Geburtsdatum", "gender": "Geschlecht", "row": "Zeile", "sender": "Absender",
             "text": "Dokument", "kind": "Dokumentart", "replaces": "ersetzt", "other_person_present": "weitere Person"}
_KIND_DE = {LAB: "Laborbefund", DISCHARGE: "Entlassungsbericht"}
_VALUE_DE = {"female": "weiblich", "male": "männlich", "other": "divers", "unknown": "unbekannt"}


def _result(found: dict[str, str], extra: dict | None = None) -> dict:
    extra = dict(extra or {})
    other = bool(extra.pop("other_person_present", False))
    codes = sorted(found, key=lambda c: -_RANK[found[c]])
    outcome = found[codes[0]] if codes else FILE
    res = {"outcome": outcome, "codes": codes or ["OK"],
           "reasons": [_MSG.get(c, c) for c in codes],
           "block_codes": [c for c in codes if found[c] == BLOCK],
           "hold_codes": [c for c in codes if found[c] == HOLD]}
    if extra:
        res.update(extra)
    for d in res.get("details") or []:
        if isinstance(d, dict) and d.get("field"):
            d.setdefault("field_de", _FIELD_DE.get(d["field"], d["field"]))
            for k in ("document", "request"):
                if d.get(k) in _VALUE_DE:
                    d[k + "_de"] = _VALUE_DE[d[k]]
                elif isinstance(d.get(k), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d[k]):
                    d[k + "_de"] = f"{d[k][8:10]}.{d[k][5:7]}.{d[k][:4]}"
    res["other_person_present"] = other           # 'Dokument enthält eine weitere Person' (never who)
    if other and outcome != BLOCK:
        res.setdefault("details", []).append({"field": "other_person_present", "other_person_present": True})
    if outcome == BLOCK:
        res["details"] = [{"field": "other_person_present", "other_person_present": True}] if other else []
    return res


def _split_person(name: str) -> tuple[str, str]:
    g, f, _ = split_name(name or "")
    return " ".join(g), f or ""


def decide(parsed: Parsed, request: dict | None = None, patient=None, identity: dict | None = None,
           raw: bytes | None = None) -> dict:
    """All rules of RULES.md for one upload -> {outcome, codes, reasons, details, ...}.

    request: {doc_kind, requester_name, recipient: {name}} (all optional)
    patient: the requested FHIR Patient (or {name, birthDate})
    identity: the eID claims {given_name|given, family_name|family}
    raw: the uploaded bytes (enables EMPTY / OVERSIZE / UNREADABLE)
    """
    request = request or {}
    found: dict[str, str] = {}
    if raw is not None:
        from .parse import decode_upload
        if not raw:
            found["EMPTY"] = BLOCK
        elif len(raw) > MAX_BYTES:
            found["OVERSIZE"] = BLOCK
        elif decode_upload(raw)["code"] == "ENCODING_UNREADABLE":
            found["ENCODING_UNREADABLE"] = BLOCK
    recipient = (request.get("recipient") or {}).get("name") or request.get("recipient_name")
    if identity and recipient:
        given = identity.get("given_name") or identity.get("given") or ""
        family = identity.get("family_name") or identity.get("family") or ""
        bd = identity.get("birthDate") or identity.get("birth_date") or identity.get("birthdate")
        if not identity_matches(recipient, given, family, patient=patient, birth_date=bd):
            found["EID_MISMATCH"] = BLOCK
    for f in parsed.findings:
        if f["outcome"] == BLOCK:
            found.setdefault(f["code"], BLOCK)
    if (not parsed.text or not parsed.text.strip()) and (raw is None or not raw.strip()):
        found.setdefault("EMPTY", BLOCK)                 # only a really empty upload is 'empty'
    if raw and raw.strip() and not (parsed.text or "").strip() and "ENCODING_UNREADABLE" not in found:
        found.setdefault("ENCODING_UNREADABLE", BLOCK)

    pd = patient_decision(parsed, patient)
    for c in pd["codes"]:
        if c != "OK":
            found.setdefault(c, BLOCK if c in pd["block_codes"] else HOLD)
    other = pd.get("other_person_present", False)

    kind = parsed.detected_kind
    want = request.get("doc_kind")
    if kind == UNKNOWN or (kind == DISCHARGE and not parsed.doc_type_confirmed):
        found.setdefault("KIND_UNKNOWN", HOLD)
    elif want in (LAB, DISCHARGE) and kind != want:
        found.setdefault("KIND_MISMATCH", HOLD)
    if kind == LAB and not parsed.lab:
        found.setdefault("NO_RESULTS", HOLD)

    extra_details = []
    for f in parsed.findings:                     # lab rows and table findings (codes from the parser)
        if f["outcome"] == HOLD and f["code"] not in _PATIENT_CODES and not (
                f["code"].startswith("KIND_") and want in (LAB, DISCHARGE)):
            found.setdefault(f["code"], HOLD)
            if f.get("detail"):
                extra_details.append(f["detail"])
    if parsed.report_status in ("corrected", "amended"):
        # a correction must not sit next to the report it replaces: a human retires the old one
        # (fhirmap.build_correction builds the new versions on the clinician's decision)
        found.setdefault("CORRECTION_REVIEW", HOLD)
        extra_details.append({"field": "replaces", "code": "CORRECTION_REVIEW", "replaces": parsed.replaces,
                              "replaces_time": parsed.replaces_time, "status": parsed.report_status})
    if "KIND_UNKNOWN" in found:                   # an unrecognised document: its first sentence helps decide
        extra_details.append({"field": "text", "code": "KIND_UNKNOWN", "document": first_sentence(parsed)})
    elif "KIND_MISMATCH" in found:                # the kinds themselves say it ('Laborbefund statt Entlassungsbericht')
        extra_details.append({"field": "kind", "code": "KIND_MISMATCH", "detected": kind, "requested": want,
                              "detected_de": _KIND_DE.get(kind, kind), "requested_de": _KIND_DE.get(want, want)})

    # 'Requested by' / 'Einsender' / 'Zuweiser' naming someone else is normal (the GP ordered
    # the test); it is information for the card (card_facts()['einsender']), never a hold.
    if recipient:
        for sb in parsed.senders:
            if not (identity_matches(recipient, *_split_person(sb.get("name") or ""))
                    or _initials_match(recipient, sb.get("name") or "")):
                found.setdefault("SENDER_CONTRADICTS_EID", HOLD)
                extra_details.append({"field": "sender", "code": "SENDER_CONTRADICTS_EID",
                                      "document": ", ".join(x for x in (sb.get("name"), sb.get("role"), sb.get("org"))
                                                            if x)})
    if kind == DISCHARGE:
        _discharge_plausibility(parsed, request, found)
    elif kind == LAB:
        _lab_org(parsed, request, found)
    if "ORG_MISMATCH" in found:                   # 'Labor laut Dokument: <org> – angefragt: <requested org>'
        extra_details.append({"field": "org", "code": "ORG_MISMATCH", "kind": kind,
                              "document": _doc_org(parsed),
                              "request": ((request.get("recipient") or {}).get("org") or "").strip() or None})
    if any(f["code"] == "SENDER_CONFLICT" for f in parsed.findings):
        extra_details.append({"field": "sender", "code": "SENDER_CONFLICT",
                              "document": "; ".join(", ".join(x for x in (sb.get("name"), sb.get("role")) if x)
                                                    for sb in parsed.senders)})
    res = _result(found, {"details": [x for x in pd.get("details", []) if x.get("field") != "other_person_present"]
                          + extra_details,
                           "patient": {"name": pd.get("name"), "dob": pd.get("dob")},
                           "other_person_present": other})
    if parsed.replaces or parsed.replaces_time:
        res["replaces"] = {"text": parsed.replaces, "time": parsed.replaces_time, "status": parsed.report_status}
    return res


def first_sentence(parsed: Parsed, limit: int = 200) -> str | None:
    """The document's first meaningful sentence (not a heading, a key line or a table row), <= limit chars."""
    from .parse import normalise_markup
    for ln in normalise_markup(parsed.text or "").replace("\r", "").split("\n"):
        t = ln.strip()
        if not t or t.startswith(("#", "|", "---", "<!--")) or re.match(r"^[^:]{1,40}:\s", t) or len(t) < 20:
            continue
        m = re.match(r"(.+?[.!?])(\s|$)", t)
        sent = (m[1] if m else t).strip()
        return sent if len(sent) <= limit else sent[: limit - 1].rstrip() + "…"
    return None


def _initials_match(requested: str, written: str) -> bool:
    """'S. Kern' for 'Stefan Kern': same family name, every written given name an initial of the requested one."""
    rt, wt = rule_tokens(requested), rule_tokens(written)
    if len(rt) < 2 or len(wt) < 2 or rt[-1] != wt[-1]:
        return False
    rg, wg = rt[:-1], wt[:-1]
    return len(wg) <= len(rg) and all(len(w.rstrip(".")) == 1 and r.startswith(w.rstrip("."))
                                      for w, r in zip(wg, rg))


STALE_DAYS = 365

# words that name the kind of organisation, not which one ('Reha Waldblick' = 'Rehabilitationszentrum Waldblick')
_ORG_GENERIC = {"labor", "lab", "laboratory", "laboratorium", "labors", "medizinisches", "medizinische", "medical",
                "institut", "institute", "zentrum", "center", "centre", "reha", "rehab", "rehabilitation",
                "rehabilitationszentrum", "rehazentrum", "rehaklinik", "klinik", "clinic", "gmbh", "og", "kg", "ag",
                "und", "and", "the", "der", "die", "das", "fur", "fuer", "for", "dr", "mag", "zentrallabor",
                "facharztlabor", "praxis", "ordination", "gruppenpraxis", "partner", "co"}


def _org_tokens(name: str) -> set[str]:
    from .build import slug
    return {t for t in slug(name).split("-") if t}


def org_matches(want: str, doc: str) -> bool:
    """Same organisation: one name's tokens are a subset of the other's (the rule since v3), or - after dropping
    the generic words - every distinctive token of one is in the other (a prefix of >= 5 letters counts:
    'Kanalstr.' = 'Kanalstraße'). It only ever accepts MORE than the subset rule, never less."""
    a, b = _org_tokens(want), _org_tokens(doc)
    if a <= b or b <= a:
        return True
    da, db = a - _ORG_GENERIC, b - _ORG_GENERIC
    if not da or not db:
        return False                     # the v3 subset rule already failed and nothing distinctive is left
    near = lambda x, ys: any(x == y or (min(len(x), len(y)) >= 5 and (x.startswith(y) or y.startswith(x)))
                             for y in ys)
    small, big = (da, db) if len(da) <= len(db) else (db, da)
    return all(near(x, big) for x in small)


def _doc_org(parsed: Parsed) -> str:
    return ((parsed.org or {}).get("name") or (parsed.author or {}).get("org") or "").strip()


def _lab_org(parsed: Parsed, request: dict, found: dict) -> None:
    """A lab report from another laboratory than the one asked (H03: 'Labor Dr. Gruber & Partner, Linz' sent
    for 'Labor Kanalstraße'): the right patient, honestly attributed, but not what Dr. Hofer asked for."""
    want_org = ((request.get("recipient") or {}).get("org") or "").strip()
    doc_org = _doc_org(parsed)
    if want_org and doc_org and not org_matches(want_org, doc_org):
        found.setdefault("ORG_MISMATCH", HOLD)


def _discharge_plausibility(parsed: Parsed, request: dict, found: dict) -> None:
    from datetime import date as _date
    want_org = ((request.get("recipient") or {}).get("org") or "").strip()
    doc_org = _doc_org(parsed)
    if want_org and doc_org and not org_matches(want_org, doc_org):
        found.setdefault("ORG_MISMATCH", HOLD)
    st = parsed.stay or {}
    end = st.get("end")
    if end and (_date.today() - _date.fromisoformat(end)).days > STALE_DAYS:
        found.setdefault("STALE_REPORT", HOLD)
    # a stay that ends before the surgery the report itself names ('after ... on 18 August 2026')
    if end and parsed.reason:
        from .identity import parse_date_ex
        for m in re.finditer(r"(?:on|am|vom)\s+(\d{1,2}\.?\s*[A-Za-zÄÖÜäöü]+\.?\s+\d{4}|\d{1,2}\.\d{1,2}\.\d{4})",
                             parsed.reason):
            dp = parse_date_ex(m[1])
            if dp.value and dp.value.isoformat() > end:
                found.setdefault("STALE_REPORT", HOLD)

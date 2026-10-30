"""Photo intake: two independent AI transcriptions of one photo/PDF -> ONE decision (pure, no network).

The extractor (service/extractor.py) only READS: it turns the image into Markdown in the brief's format, twice and
independently. This module decides what happens with the two readings:

    parsed, d = decide_transcribed(reads, raw, content_type, model, request=..., patient=..., identity=...)

1. a reading failed (API error, refusal, empty)            -> HOLD EXTRACTION_UNREADABLE
2. decide() on EITHER reading is BLOCK (another person...)  -> BLOCK (that reading's codes; never a hold that names
                                                                someone else)
3. '[unleserlich]' in either reading                        -> HOLD EXTRACTION_UNREADABLE (names the field)
4. the two parses differ in anything that is filed or decides the outcome (patient, kind, every row, the times,
   sender, author, sections, findings)                      -> HOLD EXTRACTION_DISAGREE (names the field)
5. otherwise: the agreed Markdown goes through the UNCHANGED rules: ``decide()`` (patient, BLOCK, HOLD), and later
   the HL7 validator and ``prove()``.

The returned Parsed (two extra instance attributes, read with getattr by fhirmap.build) keeps the photo/PDF as the
original: ``raw`` = the uploaded bytes (hashed for the receipt, the
audit and the Provenance source), ``content_type`` = its media type (the attachment), ``text`` = the agreed
transcription, ``transcription`` = model, number of readings and the transcription's hash (Provenance entity).
For an EXTRACTION_* hold the Parsed carries NO transcribed content (kind unknown, no rows): if Dr. Hofer files it
anyway, only the original photo is filed, never a value one of the readings may have misread.
"""
from __future__ import annotations

import base64
import hashlib
import re
import unicodedata

from .decide import BLOCK, HOLD, _result, decide
from .identity import DIFFERENT, compare_names
from .parse import UNKNOWN, Parsed, parse_upload

UNREADABLE_MARK = "[unleserlich]"


def sniff(raw: bytes) -> str | None:
    """The media type by magic bytes (never by file name or the browser's claim): image/jpeg, image/png,
    application/pdf, or None."""
    if not raw:
        return None
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:5] == b"%PDF-":
        return "application/pdf"
    return None


def _ni(data: bytes) -> str:
    return "ni:///sha-256;" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


# code points that print the SAME glyph: a photo cannot carry the difference (NOT NFKC: that would also fold
# '10⁹/L' into '109/L' or 'm²' into 'm2', which are different on paper)
_SAME_GLYPH = str.maketrans({"\u00b5": "\u03bc", "\u2126": "\u03a9", "\u212a": "K", "\u00a0": " "})


def _norm(s) -> str | None:
    """Whitespace collapsed, NFC, and same-glyph code points folded (µ micro sign = μ Greek mu)."""
    if s in (None, ""):
        return None
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(s)).translate(_SAME_GLYPH)).strip()


def _show(v) -> str:
    return "(fehlt)" if v in (None, "", []) else str(v)


def _person(x: dict | None) -> str | None:
    return _norm(", ".join(str(v) for v in ((x or {}).get("name"), (x or {}).get("role"), (x or {}).get("org")) if v))


_ROW_KEYS = ("test", "value", "value_text", "comparator", "unit", "ucum", "ref_low", "ref_high", "ref_text", "loinc",
             "flag", "flag_text", "status", "absent")


def _row_label(r: dict) -> str:
    return str(r.get("test") or r.get("loinc") or "Zeile")


def compare_reads(a: Parsed, b: Parsed, patient=None) -> list[dict]:
    """Every difference between two parsed readings that is filed or changes the outcome, most important first.
    [{field, a, b, text}] where ``text`` is the short German description for the hold card. Patient values are
    shown only when both are near the requested patient (never another person's name)."""
    out: list[dict] = []

    def diff(field: str, va, vb, text: str | None = None):
        if va != vb:
            out.append({"field": field, "a": va, "b": vb, "text": text or f"{field} {_show(va)} vs {_show(vb)}"})

    req_name = None
    if isinstance(patient, dict):
        from .identity import _person_view
        req_name = _person_view(patient)[0]
    pa, pb = a.patient or {}, b.patient or {}
    na, nb = _norm(pa.get("name")), _norm(pb.get("name"))
    if na != nb:
        near = req_name and all(n and compare_names(n, req_name) != DIFFERENT for n in (na, nb))
        diff("Name", na, nb, f"Name {_show(na)} vs {_show(nb)}" if near else "Patientenname")
    da, db = pa.get("birthDate"), pb.get("birthDate")
    if da != db:
        diff("Geburtsdatum", da, db, f"Geburtsdatum {_show(pa.get('birthDate_text') or da)} vs "
                                     f"{_show(pb.get('birthDate_text') or db)}")
    diff("Geschlecht", pa.get("gender"), pb.get("gender"))
    pl = lambda p: [(_norm(x.get("name")), x.get("birthDate"), x.get("dob_status")) for x in p.patient_lines]
    if not out and pl(a) != pl(b):
        diff("Patientenzeilen", len(a.patient_lines), len(b.patient_lines), "Patientenzeilen")
    diff("Dokumentart", a.detected_kind, b.detected_kind)
    diff("Dokumenttyp bestätigt", a.doc_type_confirmed, b.doc_type_confirmed, "Dokumentart")
    diff("Befundstatus", a.report_status, b.report_status)
    # every row, field by field
    if len(a.lab) != len(b.lab):
        diff("Anzahl Werte", len(a.lab), len(b.lab))
    for ra, rb in zip(a.lab, b.lab):
        label = _row_label(ra)
        if _norm(ra.get("test")) != _norm(rb.get("test")):
            diff("Test", ra.get("test"), rb.get("test"), f"Test {_show(ra.get('test'))} vs {_show(rb.get('test'))}")
            continue
        for k in _ROW_KEYS:
            va, vb = ra.get(k), rb.get(k)
            if k in ("value", "ref_low", "ref_high") and va is not None and vb is not None:
                va, vb = str(va), str(vb)
            elif isinstance(va, str) and isinstance(vb, str):
                va, vb = _norm(va), _norm(vb)
            if va == vb:
                continue
            if k in ("value", "value_text", "comparator"):
                ta = f"{ra.get('comparator') or ''}{ra.get('value_text') if ra.get('value_text') is not None else _show(ra.get('value'))}"
                tb = f"{rb.get('comparator') or ''}{rb.get('value_text') if rb.get('value_text') is not None else _show(rb.get('value'))}"
                diff(f"{label} Wert", va, vb, f"{label} {ta} vs {tb}")
            elif k in ("unit", "ucum"):
                diff(f"{label} Einheit", ra.get("unit"), rb.get("unit"),
                     f"{label} Einheit {_show(ra.get('unit'))} vs {_show(rb.get('unit'))}")
            elif k.startswith("ref_"):
                diff(f"{label} Referenzbereich", ra.get("ref_text"), rb.get("ref_text"),
                     f"{label} Referenzbereich {_show(ra.get('ref_text'))} vs {_show(rb.get('ref_text'))}")
            elif k == "loinc":
                diff(f"{label} LOINC", va, vb, f"{label} LOINC {_show(va)} vs {_show(vb)}")
            else:
                diff(f"{label} {k}", va, vb, f"{label} Kennzeichen {_show(va)} vs {_show(vb)}")
            break                                    # one line per row is enough to decide 'look at the original'
    diff("Probenahme", a.sample_time, b.sample_time,
         f"Probenahme {_show(a.sample_time_text or a.sample_time)} vs {_show(b.sample_time_text or b.sample_time)}")
    diff("Befundzeit", a.report_time, b.report_time,
         f"Befundzeit {_show(a.report_time_text or a.report_time)} vs {_show(b.report_time_text or b.report_time)}")
    sa, sb = a.stay or {}, b.stay or {}
    diff("Aufenthalt", (sa.get("start"), sa.get("end")), (sb.get("start"), sb.get("end")),
         f"Aufenthalt {_show(sa.get('text'))} vs {_show(sb.get('text'))}")
    diff("Unterzeichnet von", _person(a.author), _person(b.author))
    diff("Labor/Einrichtung", _norm((a.org or {}).get("name")), _norm((b.org or {}).get("name")))
    diff("Gesendet von", [_person(s) for s in a.senders], [_person(s) for s in b.senders],
         f"Gesendet von {_show('; '.join(filter(None, (_person(s) for s in a.senders))))} vs "
         f"{_show('; '.join(filter(None, (_person(s) for s in b.senders))))}")
    diff("Einsender", _person(a.requester), _person(b.requester))
    diff("Titel", _norm(a.title), _norm(b.title))
    diff("Kommentar", _norm(a.comment), _norm(b.comment), "Kommentar des Labors")
    diff("Anlass", _norm(a.reason), _norm(b.reason), "Anlass")
    secs = lambda p: [(_norm(s.get("title")), _norm(s.get("text"))) for s in p.sections]
    if secs(a) != secs(b):
        ta, tb = secs(a), secs(b)
        name = next((x[0] for x, y in zip(ta, tb) if x != y), None) or "Abschnitte"
        diff("Abschnitt", len(ta), len(tb), f"Abschnitt „{name}“")
    fa = sorted({(f["code"], f["outcome"]) for f in a.findings})
    fb = sorted({(f["code"], f["outcome"]) for f in b.findings})
    diff("Prüfhinweise", fa, fb, "Prüfhinweise")
    return out


def _unreadable_field(md: str) -> str:
    """Where '[unleserlich]' stands: the table row's test, or the 'Key:' of the line, else 'Dokument'."""
    for ln in md.splitlines():
        if UNREADABLE_MARK not in ln.lower():
            continue
        s = ln.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            first = cells[0] if cells else ""
            return first if first and UNREADABLE_MARK not in first.lower() else "Tabellenzeile"
        m = re.match(r"^\s*[-*]?\s*([^:|]{1,40}):", s)
        if m and UNREADABLE_MARK not in m[1].lower():
            return m[1].strip()
        return "Text"
    return "Dokument"


def _original_only(raw: bytes, content_type: str) -> Parsed:
    """An EXTRACTION_* hold: the original alone, no transcribed content (nothing a misreading could have put in)."""
    p = Parsed(kind=UNKNOWN, detected_kind=UNKNOWN, text="")
    p.raw, p.content_type, p.encoding = bytes(raw), content_type, None
    return p


def _hold(code: str, text: str, raw: bytes, content_type: str, extra: dict | None = None) -> tuple[Parsed, dict]:
    d = _result({code: HOLD}, {"details": [{"field": "extraction", "code": code, "document": text}],
                               "patient": {"name": None, "dob": None}})
    d.update(extra or {})
    return _original_only(raw, content_type), d


def decide_transcribed(reads: list[str | None], raw: bytes, content_type: str, model: str, *, request: dict | None = None,
                       patient=None, identity: dict | None = None, doc_kind_hint: str | None = None,
                       error: str | None = None) -> tuple[Parsed, dict]:
    """Two independent readings of one photo/PDF -> (Parsed, decision) with decide()'s shape (see module doc).
    ``decision['extraction']`` = {model, reads, agree, diffs: [German texts]} (no patient data beyond near-misses)."""
    info = {"model": model, "reads": len(reads), "agree": False, "diffs": []}
    if error or len(reads) < 2 or any(not (r or "").strip() for r in reads):
        return _hold("EXTRACTION_UNREADABLE", "KI-Lesung nicht möglich", raw, content_type, {"extraction": info})
    mds = [r if r.endswith("\n") else r + "\n" for r in reads]
    parsed = [parse_upload(m.encode("utf-8"), doc_kind_hint) for m in mds]
    decisions = [decide(p, request, patient, identity, m.encode("utf-8")) for p, m in zip(parsed, mds)]
    for p, d in zip(parsed, decisions):                # evidence of another person in ANY reading: BLOCK
        if d["outcome"] == BLOCK:
            d = dict(d, extraction=info)
            return _original_only(raw, content_type), d
    if any(UNREADABLE_MARK in m.lower() for m in mds):
        m = next(m for m in mds if UNREADABLE_MARK in m.lower())
        info["diffs"] = [f"{_unreadable_field(m)} unleserlich"]
        return _hold("EXTRACTION_UNREADABLE", info["diffs"][0], raw, content_type, {"extraction": info})
    diffs = compare_reads(parsed[0], parsed[1], patient)
    if diffs:
        info["diffs"] = [x["text"] for x in diffs]
        return _hold("EXTRACTION_DISAGREE", diffs[0]["text"], raw, content_type, {"extraction": info})
    p, d = parsed[0], decisions[0]
    md = mds[0].encode("utf-8")
    p.raw, p.content_type, p.encoding = bytes(raw), content_type, None
    p.transcription = {"model": model, "reads": len(reads), "markdown_sha256": hashlib.sha256(md).hexdigest(),
                       "markdown_size": len(md), "ni": _ni(md)}
    info["agree"] = True
    d = dict(d, extraction=info)
    return p, d

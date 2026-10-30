"""Reference implementation of tests/jury/RULES.md (v3): raw upload bytes + request context -> (outcome, reasons).

It exists to prove the rule table is consistent and implementable: every corpus label and every fuzz label
must equal refcheck's verdict (tests/unit/test_jury.py), and refcheck must agree with the product's
fhirmap.decide on the corpus and on fuzz cases (tests/jury/xcheck.py). It covers the document forms the
corpus and the fuzz generator produce; the product's parser is broader. The mock stack also uses it.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from pgktest.norm import (AMBIGUOUS, DIFFERENT, NEAR, SAME, compare_dobs, compare_names, has_foreign_script,
                          identity_matches, loinc_ok, name_tokens, parse_dob)

from .model import BLOCK, FILE, HOLD, PATIENT, RANK, UCUM_EXPECT, UNKNOWN_UNITS, vienna

MAX_BYTES = 1_000_000
ACTIVE = re.compile(r"<\s*(script|iframe|object|embed|svg|math|form|meta|link|style)\b|\bon[a-z]+\s*=|javascript\s*:"
                    r"|vbscript\s*:|data\s*:\s*text/html", re.I)
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
C1 = re.compile(r"[\x80-\x9f]")
# rule 3b tiers
TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)(\s[^<>]*)?/?>")
BENIGN = {"br", "b", "strong", "i", "em", "sup", "sub", "u", "p", "span"}
MD_IMAGE = re.compile(r"!\[[^\]]*\]\s*\(\s*<?([^)\s>]*)")
MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\s*\(\s*<?([^)\s>]*)")
AUTOLINK = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.-]{1,31}):[^<>\s]*>")
BARE_URL = re.compile(r"\b(?!https:)([a-zA-Z][a-zA-Z0-9+.-]{1,31}):(//|[^\s]*@)")
INERT_BARE = {"http", "mailto", "tel"}
# labels (case-insensitive, after markup normalisation)
PATIENT_KEYS = {"patient", "patientin", "patient name", "patientname", "patientenname", "name des patienten",
                "name der patientin", "pat", "pat.", "name", "betrifft", "betreff"}
DOB_KEYS = {"date of birth", "dob", "born", "birth date", "geburtsdatum", "geboren am", "geb.", "geb", "geb.-datum",
            "geb.datum", "geburtstag"}
SEX_KEYS = {"geschlecht", "sex", "gender"}
SVNR_KEYS = {"svnr", "sv-nr", "sv-nr.", "sv nr", "sv-nummer", "versicherungsnummer", "sozialversicherungsnummer"}
SAMPLE_KEYS = {"sample taken", "probenahme", "sample", "abnahme", "entnahme"}
REPORTED_KEYS = {"reported", "befundet", "befundet am", "report date"}
SENT_KEYS = {"sent by", "gesendet von", "übermittelt von", "versendet von"}
GENDER = {"female": "female", "f": "female", "weiblich": "female", "w": "female", "male": "male", "m": "male",
          "männlich": "male", "maennlich": "male", "mannlich": "male", "divers": "other", "diverse": "other"}
KV = re.compile(r"^\s*(?:[-*]\s+)?[*_]*([^\W\d_][^:|]{0,48}?)[*_]*\s*:\s*(.*?)\s*$")
DOB_SPLIT = re.compile(r"[,;]?\s*\b(?:born(?: on)?|geb\.(?:\s*am)?|geboren(?: am)?|dob|date of birth|geburtsdatum)"
                       r"(?:\s*:)?\s*", re.I)
EVID_MARK = re.compile(r"(?:(?<![\w*\\])\*\s?(?=\d)|\b(?:geb\.(?:\s*am)?|geboren(?:\s+am)?|born(?:\s+on)?|d\.?o\.?b\.?|"
                       r"geb\.-datum|geburtsdatum|date of birth)\s*:?\s*)", re.I)
LABEL_PERSON = re.compile(r"(?<![\w.])([A-ZÄÖÜ][\wäöüß'-]+),\s*([A-ZÄÖÜ][\wäöüß'-]+(?:[ -][A-ZÄÖÜ][\wäöüß'-]+)?),?\s+"
                          r"(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})(?!\d)")
LABEL_NOT_PERSON = re.compile(r"labor|klinik|medizin|station|abteil|zweigstelle|reha|krankenhaus|spital|institut|praxis|ambulan|chirurg|ordination|zentrum|befund|bericht|probe|auftrag|einsender|innere|gmbh|strasse|gasse|platz|weg\b|hospital|clinic|department|laboratory|universit")
# v4: the document doubts whose sample it is (labelling, mix-up, 'Zuordnung/Identität … prüfen') -> HOLD
IDENTITY_DOUBT = re.compile(r"beschriftet\s+mit|falsch\s+beschriftet|fehlbeschrift|fehletikettier|verwechs|"
                            r"(?:identit|zuordnung)\w*\b.{0,60}?(?:prüf|klär|unklar|fraglich|zweifel)|mislabel|"
                            r"labell?ed\s+(?:with|as)\b|mix-?up|identity.{0,60}?(?:check|verif|unclear|doubt|confirm)|"
                            r"wrong\s+patient|falsche[rn]?\s+patient", re.I | re.S)
SPLITTERS = re.compile(r"[·•|;()\[\]:–—]|\s-\s|\bfor\b|\bfür\b|\bvon\b|\bof\b", re.I)
TITLE = re.compile(r"^#\s+(.*)$", re.M)
PENDING = re.compile(r"^(folgt|folgen|ausst[aä]ndig|ausstehend|in arbeit|in bearbeitung)\b", re.I)
NOT_DETERMINABLE = re.compile(r"^(nicht bestimmbar|nicht messbar|nicht auswertbar)\b", re.I)
FLAG_TOKEN = {"h": "H", "l": "L", "+": "H", "-": "L", "++": "HH", "--": "LL", "hh": "HH", "ll": "LL"}

# LOINC -> dimension of the canonical unit ("1" = dimensionless/ordinal: any unit is a contradiction)
LOINC_DIM = {
    "6301-6": "1", "14979-9": "time", "777-3": "count/vol", "3255-7": "mass/vol", "718-7": "mass/vol",
    "6690-2": "count/vol", "789-8": "count/vol", "4544-3": "fraction", "787-2": "vol", "785-6": "mass",
    "786-4": "mass/vol", "2951-2": "subst/vol", "2823-3": "subst/vol", "2075-0": "subst/vol", "2000-8": "subst/vol",
    "1988-5": "mass/vol", "33959-8": "mass/vol", "14682-9": "subst/vol", "62238-1": "flow/area", "3091-6": "mass/vol",
    "4548-4": "fraction", "59261-8": "subst/subst", "1558-6": "mass/vol", "883-9": "1", "10331-7": "1", "890-4": "1",
    "94500-6": "1", "1742-6": "cat/vol", "1920-8": "cat/vol", "2324-2": "cat/vol", "1975-2": "mass/vol",
    "1751-7": "mass/vol", "6768-6": "cat/vol", "2532-0": "cat/vol", "3016-3": "arb/vol", "2093-3": "mass/vol",
    "2085-9": "mass/vol", "13457-7": "mass/vol", "2571-8": "mass/vol", "3084-1": "mass/vol", "2885-2": "mass/vol",
    "2276-4": "mass/vol", "2132-9": "mass/vol", "2284-8": "mass/vol", "2157-6": "cat/vol", "5821-4": "count/area",
    "48065-7": "mass/vol",
}
UCUM_DIM = {"10*9/L": "count/vol", "10*12/L": "count/vol", "/nL": "count/vol", "g/L": "mass/vol", "g/dL": "mass/vol",
            "mg/dL": "mass/vol", "mg/L": "mass/vol", "ng/mL": "mass/vol", "pg/mL": "mass/vol", "mmol/L": "subst/vol",
            "umol/L": "subst/vol", "s": "time", "%": "fraction", "mmol/mol": "subst/subst", "fL": "vol", "pg": "mass",
            "U/L": "cat/vol", "m[IU]/L": "arb/vol", "mU/L": "arb/vol", "mL/min/{1.73_m2}": "flow/area"}


# ------------------------------------------------------------------ decoding and markup
def decode(raw: bytes) -> tuple[str | None, str | None]:
    """UTF-8 (BOM dropped); else Windows-1252, else Latin-1, each only if plausible text. (text, warning)."""
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        pass
    for enc in ("cp1252", "latin-1"):
        try:
            t = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if not CONTROL.search(t) and not C1.search(t):
            return t, "ENCODING_FALLBACK"
    return None, None


def normalise(text: str) -> str:
    """Converter leftovers -> plain Markdown: benign tags removed, entities decoded (as the product does)."""
    out = []
    for line in text.split("\n"):
        line = re.sub(r"<sup>\s*([^<]*?)\s*</sup>", r"^\1", line, flags=re.I)
        line = re.sub(r"<(br|p)\s*/?>|</p>", "; " if "|" in line else " ", line, flags=re.I)
        line = re.sub(r"</?(b|strong|i|em|sub|u|span|sup)\s*>", "", line, flags=re.I)
        out.append(html.unescape(line).replace(" ", " "))
    return "\n".join(out)


def suspect(text: str) -> bool:
    """Rule 3b: non-benign tags, non-https/relative images and links, non-https autolinks, non-inert bare URLs,
    or a rule-3 pattern / non-benign tag hidden behind HTML entities or %-encoding."""
    body = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    if any(m[1].lower() not in BENIGN or (m[2] or "").strip(" /") for m in TAG.finditer(body)):
        return True
    for rx in (MD_IMAGE, MD_LINK):
        for t in rx.findall(body):
            t = t.strip()
            if not t or t.startswith("//") or (re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t) and not t.lower().startswith("https://")):
                return True
    if any(a.lower() != "https" for a in AUTOLINK.findall(body)):
        return True
    for sch, rest in BARE_URL.findall(body):
        if sch.lower() not in INERT_BARE and (sch.lower() in ("ftp", "ftps", "file", "sftp", "ws", "wss", "data", "smb",
                                                              "ldap") or rest == "//"):
            return True
    decoded = urllib.parse.unquote(html.unescape(html.unescape(body)))
    return decoded != body and bool(ACTIVE.search(decoded) or any(m[1].lower() not in BENIGN or (m[2] or "").strip(" /")
                                                                  for m in TAG.finditer(decoded)))


# ------------------------------------------------------------------ document structure
def tables(lines: list[str]) -> list[tuple[int, int, list[str], list[list[str]]]]:
    """All pipe tables: (first line, last line, header cells casefolded, data rows)."""
    out, i = [], 0
    while i < len(lines) - 1:
        if lines[i].strip().startswith("|") and re.fullmatch(r"\s*\|?(\s*:?-{1,}:?\s*\|)+\s*:?-*:?\s*", lines[i + 1]):
            head = [c.strip().casefold() for c in lines[i].strip().strip("|").split("|")]
            j, rows = i + 2, []
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            out.append((i, j - 1, head, rows))
            i = j
        else:
            i += 1
    return out


def labelled(lines: list[str], tabs) -> list[tuple[int, str, str]]:
    """(line index, key casefolded, value): 'Key: value' lines, '# Key: value' headings, 2-column key/value tables."""
    out = []
    for k, ln in enumerate(lines):
        m = KV.match(re.sub(r"^\s*#+\s*", "", ln))
        if m and not ln.lstrip().startswith("|"):
            out.append((k, m[1].strip().casefold(), m[2]))
    for a, b, head, rows in tabs:
        if len(head) == 2:
            for r_i, row in enumerate([head, *rows]):
                if len(row) == 2 and row[0].strip(" *_:").casefold() in PATIENT_KEYS | DOB_KEYS | SEX_KEYS | SVNR_KEYS:
                    out.append((a + (r_i + 1 if r_i else 0), row[0].strip(" *_:").casefold(), row[1]))
    return out


def caps_family_first(name: str) -> str:
    """'BEISPIEL Maria' -> 'Maria BEISPIEL' (exactly one ALL-CAPS token marks the family name)."""
    toks = name.split()
    caps = [t for t in toks if len(t) > 1 and t.isupper()]
    if len(caps) == 1 and len(toks) > 1 and any(not t.isupper() for t in toks):
        return " ".join([t for t in toks if t is not caps[0]] + [caps[0]])
    return name


def dob_of(s: str | None) -> tuple[str | None, str]:
    """(iso | AMBIGUOUS | None, status ok|two-digit|ambiguous|unparseable|missing)."""
    if not s or not s.strip():
        return None, "missing"
    s = s.strip(" .,;*_()")
    if m := re.fullmatch(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{2})", s):
        d, mo, yy = map(int, m.groups())
        past = []
        for c in (1900, 2000):
            try:
                if date(c + yy, mo, d) <= date.today():
                    past.append(date(c + yy, mo, d).isoformat())
            except ValueError:
                pass
        return (past[0], "two-digit") if len(past) == 1 else (AMBIGUOUS, "ambiguous")
    iso = parse_dob(s)
    if iso == AMBIGUOUS:
        return AMBIGUOUS, "ambiguous"
    return (iso, "ok") if iso else (None, "unparseable")


def split_patient(raw: str) -> tuple[str, str | None, str | None]:
    """'Maria Beispiel, born 12 March 1961, weiblich' -> (name, dob text, gender)."""
    raw = raw.strip(" *_")
    gender = None
    parts = DOB_SPLIT.split(raw, maxsplit=1)
    name, dob = (parts[0], parts[1]) if len(parts) == 2 else (raw, None)
    if dob is None and "," in raw:
        bits = [b.strip() for b in raw.split(",")]
        for i in range(1, len(bits)):
            if dob_of(",".join(bits[i:]))[0] or re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{2,4}", bits[i]):
                name, dob = ", ".join(bits[:i]), ",".join(bits[i:])
                break
    keep = []
    for b in [b.strip() for b in name.split(",") if b.strip()]:
        if GENDER.get(b.casefold()):
            gender = GENDER[b.casefold()]
        elif not re.search(r"\d", b):
            keep.append(b)
    if dob and dob_of(dob)[1] == "unparseable" and "," in dob:  # '12.03.1961, weiblich' (not 'March 12, 1961')
        dparts = [b.strip() for b in dob.split(",")]
        for i in range(len(dparts), 0, -1):
            if dob_of(", ".join(dparts[:i]))[1] != "unparseable":
                gender = gender or next((GENDER[b.casefold()] for b in dparts[i:] if b.casefold() in GENDER), None)
                dob = ", ".join(dparts[:i])
                break
    name = ", ".join(keep)
    return name, dob, gender


def evidence(lines: list[str], skip: set[int]) -> list[tuple[str | None, str]]:
    """(name, iso) of a person with a birth date anywhere outside the patient/DOB lines and tables."""
    out = []
    for i, ln in enumerate(lines):
        if i in skip:
            continue
        text = re.sub(r"^\s*#+\s*", "", ln)
        for m in EVID_MARK.finditer(text):
            seg = re.split(r"[·•|()\[\]]|\s[-–—]\s", text[m.end():])[0]
            toks = seg.split()  # the date is the shortest leading run of tokens that parses ('01.01.1960. Werte ...')
            iso = next((x for x in (dob_of(" ".join(toks[:n]).strip(" ,."))[0] for n in range(1, 5)) if x and x != AMBIGUOUS),
                       None)
            if not iso or iso == AMBIGUOUS or iso > date.today().isoformat():
                continue
            chunk = SPLITTERS.split(text[: m.start()].rstrip(" ·•|,;:–—-\t"))[-1].strip(" ,.")
            toks = []
            for t in reversed(chunk.split()):
                if t[:1].isupper():
                    toks.insert(0, t)
                else:
                    break
            name = " ".join(toks) or None
            if not name and iso >= (date.today() - timedelta(days=366)).isoformat():
                continue
            out.append((name, iso))
        # v4: the tube-label form 'Beispiel, Max, 01.01.1960' (Family, Given, date at least a year back), no mark needed
        for m in LABEL_PERSON.finditer(text):
            if LABEL_NOT_PERSON.search((m[1] + " " + m[2]).casefold().replace("ß", "ss")):
                continue                     # an organisation or place, not a person
            iso = dob_of(m[3])[0]
            if iso and iso != AMBIGUOUS and iso <= (date.today() - timedelta(days=366)).isoformat():
                out.append((f"{m[1]}, {m[2]}", iso))
    return out


# ------------------------------------------------------------------ the rule table
def check(raw: bytes, ctx, eid) -> tuple[str, list[str]]:
    """ctx: model.Ctx; eid: model.Person. Returns (FILE|HOLD|BLOCK, reasons sorted by severity)."""
    found: dict[str, str] = {}

    def add(outcome: str, reason: str) -> None:
        found.setdefault(reason, outcome)

    if not identity_matches(ctx.name, eid.given, eid.family, eid.birthDate, (PATIENT["name"], PATIENT["birthDate"])):
        add(BLOCK, "EID_MISMATCH")
    text = None
    if not raw:
        add(BLOCK, "EMPTY")
    elif len(raw) > MAX_BYTES:
        add(BLOCK, "OVERSIZE")
    else:
        text, _warn = decode(raw)
        if text is None:
            add(BLOCK, "ENCODING_UNREADABLE")
        elif not text.strip():
            add(BLOCK, "EMPTY")
    if text is not None and text.strip():
        if CONTROL.search(text):
            add(BLOCK, "UNREADABLE")
        if ACTIVE.search(text):
            add(BLOCK, "ACTIVE_CONTENT")
        if suspect(text):
            add(HOLD, "ACTIVE_CONTENT_SUSPECT")
        _content(normalise(text.replace("<!--", "\n").replace("-->", "\n").replace("\r\n", "\n")), ctx, add)
    if not found:
        return FILE, ["OK"]
    order = sorted(found, key=lambda r: -RANK[found[r]])
    return found[order[0]], order


def _content(text: str, ctx, add) -> None:
    lines = text.split("\n")
    tabs = tables(lines)
    table_lines = {k for a, b, _, _ in tabs for k in range(a, b + 1)}
    kv = labelled(lines, tabs)
    m = TITLE.search(text)
    title = (m.group(1) if m else "").strip()
    title_kind = (title.split(":", 1)[1] if ":" in title else title).casefold()
    kind = ("discharge_report" if re.search(r"discharge|entlass|arztbrief|reha\w*bericht|zwischenbericht|verlaufsbericht",
                                             title_kind)
            else "lab_result" if re.search(r"\blab|labor|befund|nachtrag", title_kind) else "unknown")
    _patient(lines, kv, table_lines, kind, add)
    if IDENTITY_DOUBT.search(re.sub(r"\s+", " ", text)):
        add(HOLD, "IDENTITY_DOUBT")
    if kind == "unknown":
        add(HOLD, "KIND_UNKNOWN")
    elif kind != ctx.doc_kind:
        add(HOLD, "KIND_MISMATCH")
    times = [v.strip() for _, k, v in kv if k in SAMPLE_KEYS | REPORTED_KEYS]
    samples = {v.strip().casefold() for _, k, v in kv if k in SAMPLE_KEYS}
    reported = {v.strip().casefold() for _, k, v in kv if k in REPORTED_KEYS}
    reportish = [ln for k, ln in enumerate(lines) if re.match(r"^\s*#\s+\S", ln) and k not in table_lines
                 and re.search(r"befund|bericht|brief|report|summary|laborwerte|mitteilung", ln.casefold())]
    if len(reportish) >= 2 or len(samples) >= 2 or len(reported) >= 2:
        add(HOLD, "MULTIPLE_REPORTS")
    now = datetime.now(timezone.utc)
    for v in times:
        try:
            t = datetime.fromisoformat(vienna(v))
        except ValueError:
            continue
        if (t - now).total_seconds() > 2 * 3600:
            add(HOLD, "FUTURE_DATE")
    if kind == "lab_result":
        _lab(lines, tabs, table_lines, add)
        body = "\n".join(lines[1:]).casefold()
        if re.search(r"\b(korrektur|befundkorrektur|korrigiert\w*|berichtigt\w*|berichtigung|corrected|correction)\b",
                     title.casefold()) \
                or re.search(r"\b(befundkorrektur|korrigierter befund|berichtigter befund|ersetzt (?:den|diesen|einen) "
                             r"befund)", body) \
                or re.search(r"\b(nachtrag|erg[aä]nzungsbefund|addendum|amended report)\b", title.casefold() + "\n" + body):
            add(HOLD, "CORRECTION_REVIEW")
    if kind == "discharge_report":
        _discharge(title, kv, ctx, add)
    for _, k, v in kv:
        if k in SENT_KEYS and not identity_matches(ctx.name, *_split(_person(v))):
            add(HOLD, "SENDER_CONTRADICTS_EID")


def _patient(lines, kv, table_lines, kind, add) -> None:
    req_name, req_dob = PATIENT["name"], PATIENT["birthDate"]
    entries, skip, dob_lines, sex, svnrs = [], set(), [], None, []
    for k, key, val in kv:
        if key in PATIENT_KEYS:
            is_bullet = re.match(r"^\s*[-*]\s+", lines[k]) is not None if k < len(lines) else False
            name, dob, g = split_patient(val)
            if key == "name" and not name:
                continue
            if is_bullet and not dob:
                continue  # 'Patient:' bullet in prose, without a date: not a patient line (v3)
            if not (name or dob):
                continue
            entries.append({"name": name or None, "dob": dob, "gender": g, "order_free": key == "name"})
            skip.add(k)
            sex = sex or g
        elif key in DOB_KEYS:
            dob_lines.append(val)
            skip.add(k)
        elif key in SEX_KEYS:
            sex = sex or GENDER.get(val.strip().casefold())
            skip.add(k)
        elif key in SVNR_KEYS:
            svnrs.append(val)
            skip.add(k)
    if len([e for e in entries if not e["dob"]]) >= 1 and len(dob_lines) == 1 and len(entries) == 1:
        entries[0]["dob"] = dob_lines[0]
    elif not entries and dob_lines:
        entries.append({"name": None, "dob": dob_lines[0], "gender": None, "order_free": False})
    ev = evidence(lines, skip)            # v4: table cells too ('| Hinweis | Probe von Max Beispiel, geb. … |')
    have = {(tuple(name_tokens(caps_family_first(e["name"] or ""))), dob_of(e["dob"])[0]) for e in entries}
    for name, iso in ev:
        if (tuple(name_tokens(caps_family_first(name or ""))), iso) not in have:
            entries.append({"name": name, "dob": iso, "gender": None, "order_free": False, "evidence": True})
    if not entries:
        add(HOLD, "PATIENT_MISSING")
        return
    verdicts = []
    for e in entries:
        nm = caps_family_first(e["name"]) if e["name"] else None
        n = compare_names(nm, req_name) if nm else None
        if n == NEAR and e["order_free"] and sorted(name_tokens(nm)) == sorted(name_tokens(req_name)) \
                and not has_foreign_script(nm):
            n = SAME
        iso, st = dob_of(e["dob"])
        d = compare_dobs(iso, req_dob) if st != "missing" else None
        if st == "two-digit" and d != SAME:
            d, iso, st = NEAR, AMBIGUOUS, "ambiguous"
        if n == NEAR and d == SAME and st == "ok" and nm and not has_foreign_script(nm):
            t, rt = name_tokens(nm), name_tokens(req_name)
            if len(t) >= 2 and sorted(t) == sorted(rt):
                n = SAME        # v4: 'Beispiel Maria' = the requested tokens in another order + the exact full DOB
        verdicts.append({"key": (tuple(name_tokens(nm or "")), iso if st != "missing" else "-"), "n": n, "d": d,
                         "iso": iso, "st": st, "evidence": e.get("evidence"), "named": bool(nm)})
    named = [v for v in verdicts if not (v["evidence"] and not v["named"])]
    if any(DIFFERENT in (v["n"], v["d"]) for v in verdicts if v not in named):
        add(HOLD, "PATIENT_CONFLICT")
    verdicts = named or verdicts
    if len({v["key"] for v in verdicts}) > 1:
        if all(DIFFERENT in (v["n"], v["d"]) for v in verdicts):
            add(BLOCK, "WRONG_PATIENT")
        elif any(DIFFERENT in (v["n"], v["d"]) for v in verdicts):
            add(BLOCK, "OTHER_PATIENT_PRESENT")
        else:
            add(HOLD, "PATIENT_CONFLICT")
        return
    v = verdicts[0]
    if DIFFERENT in (v["n"], v["d"]):
        add(BLOCK, "WRONG_PATIENT")
    if v["n"] is None or v["d"] is None:
        add(HOLD, "PATIENT_INCOMPLETE")
    if v["n"] == NEAR:
        add(HOLD, "NAME_NEAR")
    if v["d"] == NEAR:
        add(HOLD, {"ambiguous": "DOB_AMBIGUOUS", "unparseable": "DOB_UNPARSEABLE"}.get(v["st"], "DOB_NEAR"))
    for s in svnrs:
        digits = re.sub(r"\D", "", s)
        if len(digits) == 10 and digits[4:] != f"{req_dob[8:10]}{req_dob[5:7]}{req_dob[2:4]}":
            add(BLOCK, "SVNR_MISMATCH")
    if sex and sex in ("male", "female") and sex != "female":  # the requested Patient is female
        add(HOLD, "SEX_MISMATCH")


def _person(value: str) -> str:
    return value.split(",")[0].strip()


def _split(name: str) -> tuple[str, str]:
    toks = [t for t in name.split() if t.rstrip(".").casefold() not in ("dr", "mag", "prof", "di")]
    return " ".join(toks[:-1]), toks[-1] if toks else ""


def _col(head: list[str], *keys: str) -> int | None:
    return next((i for i, h in enumerate(head) if any(k in h for k in keys)), None)


def _lab(lines, tabs, table_lines, add) -> None:
    lab_tabs = [t for t in tabs if _col(t[2], "result", "ergebnis", "wert", "value") is not None]
    if not lab_tabs or not any(t[3] for t in lab_tabs):
        add(HOLD, "NO_RESULTS")
    orphan = [k for k, ln in enumerate(lines) if k not in table_lines and ln.strip().startswith("|") and ln.count("|") >= 3]
    if orphan:
        add(HOLD, "TABLE_SPLIT")
    for _, _, head, rows in lab_tabs:
        ci_val, ci_unit = _col(head, "result", "ergebnis", "wert", "value"), _col(head, "unit", "einheit")
        ci_loinc, ci_test = _col(head, "loinc"), _col(head, "test", "parameter", "analyt")
        ci_flag = _col(head, "flag")
        if ci_flag is None and "" in head:
            ci_flag = head.index("")  # unlabelled column of flag tokens
        for row in rows:
            g = lambda i: row[i].strip() if i is not None and i < len(row) else ""
            unit, loinc = g(ci_unit), g(ci_loinc)
            val = re.sub(r"^(\*\*|__)(.*)\1$", r"\2", g(ci_val)).strip()
            cell_flag = None
            if mm := re.fullmatch(r"(.*?\d)\s+(H|L|HH|LL|\+|-|\+\+|--)", val):
                val, cell_flag = mm[1], FLAG_TOKEN.get(mm[2].casefold())
            col_flag = FLAG_TOKEN.get(g(ci_flag).casefold()) if g(ci_flag) else None
            if cell_flag and col_flag and cell_flag != col_flag:
                add(HOLD, "FLAG_CONFLICT")
            numeric = re.fullmatch(r"[<>]?=?\s*[\d.,]+", val) is not None and not PENDING.match(val) \
                and not NOT_DETERMINABLE.match(val)
            codes = UCUM_EXPECT.get(unit)
            dim = LOINC_DIM.get(loinc)
            if numeric and not unit:
                inr = loinc == "6301-6" or (not loinc and re.search(r"\binr\b", g(ci_test).casefold()))
                if not (dim == "1" or inr):
                    add(HOLD, "UNIT_MISSING")
            if unit and (codes is None or unit in UNKNOWN_UNITS):
                add(HOLD, "UNKNOWN_UNIT")
            if loinc and not loinc_ok(loinc):
                add(HOLD, "LOINC_CHECKDIGIT")
            if dim and unit and codes and (dim == "1" or UCUM_DIM.get(sorted(codes)[0]) != dim):
                add(HOLD, "LOINC_UNIT_DIMENSION")


def _discharge(title: str, kv, ctx, add) -> None:
    org = title.split(":", 1)[0].strip() if ":" in title else ""
    slug = lambda s: set(re.sub(r"[^a-z0-9]+", "-", s.casefold().replace("ß", "ss")).strip("-").split("-"))
    if ctx.org and org and not (slug(ctx.org) <= slug(org) or slug(org) <= slug(ctx.org)):
        add(HOLD, "ORG_MISMATCH")
    if re.search(r"zwischenbericht|verlaufsbericht|zwischenbefund|interim|progress report", title.casefold()):
        add(HOLD, "REPORT_INTERIM")
    stay = next((v for _, k, v in kv if k in ("stay", "aufenthalt")), "")
    m = re.search(r"(\d{1,2})\.?\s+(?:to|bis)\s+(\d{1,2})\.?\s+([^\W\d_]+)\s+(\d{4})", stay)
    end = parse_dob(f"{m[2]} {m[3]} {m[4]}") if m else None
    if end and end != AMBIGUOUS:
        if (date.today() - date.fromisoformat(end)).days > 365:
            add(HOLD, "STALE_REPORT")
        reason = next((v for _, k, v in kv if k in ("reason", "grund")), "")
        mm = re.search(r"(\d{1,2})\.?\s+([^\W\d_]+)\s+(\d{4})|(\d{1,2}\.\d{1,2}\.\d{4})", reason)
        surg = parse_dob(mm[0]) if mm else None
        if surg and surg != AMBIGUOUS and end < surg:
            add(HOLD, "STALE_REPORT")


def persons(text: str) -> list[tuple[str | None, str | None]]:
    """Every (name, ISO birth date) the rules see: patient lines (any label form) and evidence elsewhere."""
    text = normalise(text.replace("<!--", "\n").replace("-->", "\n").replace("\r\n", "\n"))
    lines = text.split("\n")
    tabs = tables(lines)
    skip = {k for a, b, _, _ in tabs for k in range(a, b + 1)}
    out = []
    for k, key, val in labelled(lines, tabs):
        if key in PATIENT_KEYS:
            name, dob, _ = split_patient(val)
            iso = dob_of(dob)[0]
            out.append((caps_family_first(name) if name else None, iso if iso != AMBIGUOUS else None))
            skip.add(k)
    return out + [(caps_family_first(n) if n else None, iso) for n, iso in evidence(lines, skip)]

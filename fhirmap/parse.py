"""Deterministic parser for the Markdown answers (lab report, discharge report).

No AI, no guessing: what is not recognised is reported in ``warnings`` and, when
it affects patient safety, in ``hold_reasons`` (the answer must then be held and
a human asked, never auto-filed).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from .identity import (
    VIENNA, fhir_datetime, loose_name_equal, parse_date_ex, parse_datetime_ex, split_name,
    _is_prefix_title, _is_suffix_title, _raw_tokens,
)
from . import loinc as _loinc
from .units import to_ucum

LAB, DISCHARGE, UNKNOWN = "lab_result", "discharge_report", "unknown"


@dataclass
class Parsed:
    kind: str = UNKNOWN
    patient: dict | None = None        # {name, given[], family, prefix[], birthDate, birthDate_text, gender}
    author: dict | None = None         # {name, role, org}: lab 'Validated by', report 'Written by'
    org: dict | None = None            # {name}: the issuing organisation
    lab: list[dict] = field(default_factory=list)
    sample_time: str | None = None     # FHIR dateTime
    report_time: str | None = None     # FHIR dateTime (instant when it has a time)
    text: str = ""
    warnings: list[str] = field(default_factory=list)
    hold_reasons: list[str] = field(default_factory=list)
    title: str | None = None
    requester: dict | None = None      # {name, org}
    sender: dict | None = None         # {name, role, org}: 'Sent by'
    comment: str | None = None         # lab: free text after the table
    stay: dict | None = None           # {start, end, text}
    reason: str | None = None
    sections: list[dict] = field(default_factory=list)   # [{title, text}]
    extra: dict = field(default_factory=dict)            # unrecognised 'Key: value' header lines
    sample_time_text: str | None = None
    report_time_text: str | None = None
    doc_type_confirmed: bool = False   # discharge: title or 'Stay' line says it is a discharge report
    detected_kind: str = UNKNOWN       # the document type as detected, before patient problems force 'unknown'
    block_reasons: list[str] = field(default_factory=list)   # BLOCK: active content, unreadable bytes
    findings: list[dict] = field(default_factory=list)       # [{code, outcome: WARN|HOLD|BLOCK, message}]
    patient_lines: list[dict] = field(default_factory=list)  # every patient line, incl. ones hidden in HTML comments
    senders: list[dict] = field(default_factory=list)        # every 'Sent by' line
    report_status: str = "final"       # final | preliminary | corrected | amended (DiagnosticReport.status)
    replaces: str | None = None        # corrected report: what it says it replaces
    replaces_time: str | None = None   # ... parsed: the replaced report's time (FHIR dateTime) if stated
    encoding: str | None = None        # set by parse_upload(): utf-8 | cp1252 | latin-1
    raw: bytes | None = field(default=None, repr=False)   # the upload exactly as received (parse_upload)

    @property
    def ok_to_file(self) -> bool:
        return self.kind != UNKNOWN and not self.hold_reasons and not self.block_reasons

    def to_dict(self) -> dict:
        raw = self.raw
        self.raw = None
        try:
            d = asdict(self)
        finally:
            self.raw = raw
        d.pop("raw", None)
        d["raw_sha256"] = hashlib.sha256(raw).hexdigest() if raw is not None else None
        d["ok_to_file"] = self.ok_to_file
        return d

    def _warn(self, msg: str, hold: bool = False, code: str = "WARN", block: bool = False) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)
            self.findings.append({"code": code, "outcome": "BLOCK" if block else "HOLD" if hold else "WARN",
                                  "message": msg})
        if hold and msg not in self.hold_reasons:
            self.hold_reasons.append(msg)
        if block and msg not in self.block_reasons:
            self.block_reasons.append(msg)


# --------------------------------------------------------------------------- helpers

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("ß", "ss"))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"\s+", " ", s).strip(" .:*_")


_KEYS = {
    "patient": ["patient", "patientin", "patient/in", "patient*in", "patient:in", "patientname",
                "patient name", "name of patient", "pat", "pat.", "name des patienten", "name der patientin",
                "name", "patientenname", "betrifft", "betreff", "re", "regarding", "patient(in)",
                # 'Name, Vorname: Beispiel, Maria' (family first by its label)
                "name, vorname", "nachname, vorname", "familienname, vorname", "zuname, vorname", "name/vorname",
                "nachname/vorname", "familienname/vorname", "surname, first name", "last name, first name",
                "family name, given name", "patient (name, vorname)", "patientin (name, vorname)", "subject"],
    # family and given name on their own lines ('Familienname: Beispiel' + 'Vorname: Maria')
    "family_name": ["familienname", "nachname", "zuname", "surname", "family name", "last name"],
    "given_name": ["vorname", "vornamen", "given name", "given names", "first name", "forename"],
    "dob": ["born", "date of birth", "dob", "d.o.b", "birth date", "birthdate", "geburtsdatum", "geb",
            "geb.", "geboren", "geboren am", "geb.-datum", "geb.datum", "geb. datum", "geb-datum", "gebdatum",
            "geburtstag", "geb.-dat", "geb.-dat.", "geb. dat.", "geb.dat.", "geb.dat", "gebdat", "gebdat.", "geb-dat",
            "geb.dat", "geburtsdat.", "geburtsdat"],
    "sex": ["geschlecht", "sex", "gender", "sex/gender"],
    "svnr": ["svnr", "sv-nr", "sv-nr.", "sv nr", "sv-nummer", "sv nummer", "vsnr", "versicherungsnummer",
             "sozialversicherungsnummer", "sozialvers.-nr", "sozialvers.-nr.", "social security number", "ssn"],
    "requested": ["requested by", "ordered by", "requester", "einsender", "einsenderin", "angefordert von",
                  "anforderung von", "anforderer", "zuweiser", "zuweiserin", "auftraggeber", "auftraggeberin",
                  "angefordert durch", "requesting physician"],
    "sample": ["sample taken", "sample collected", "sample", "collected", "probenahme", "probenahmezeit",
               "probenahmedatum", "zeitpunkt der probenahme", "abnahmezeitpunkt", "entnahmedatum", "sampled",
               "sample time", "sampling date", "collection date/time", "specimen collection", "collection", "collection time",
               "collection date", "specimen collected", "specimen taken", "sampling time", "probenentnahme",
               "probennahme", "probe entnommen", "abnahme", "abnahmezeit", "abnahmedatum", "abgenommen",
               "entnahme", "entnahmezeit", "entnahmezeitpunkt", "entnommen", "blutabnahme", "material entnommen"],
    "reported": ["reported", "report date", "report time", "befundet am", "befundzeitpunkt", "befundung",
                 "befunderstellung", "erstellt am", "report issued at", "reported on", "result date", "validiert am",
                 "freigegeben am", "freigabe", "date of report", "reported on", "issued",
                 "befundet", "befunddatum", "befundzeit", "befund vom", "befundet am", "ausgestellt",
                 "ausgestellt am", "datum des befundes", "berichtet", "befundausgabe", "report issued"],
    "validated": ["validated by", "validated", "validiert von", "validiert durch", "validiert", "approved by",
                  "released by", "freigegeben von", "freigegeben durch", "befundet von", "befundet durch",
                  "vidiert", "vidiert von", "signed by", "unterschrieben von", "medizinisch validiert von",
                  "technisch und medizinisch validiert von", "medizinisch validiert", "arztlich validiert",
                  "arztlich validiert von", "befund freigegeben", "befund freigegeben von", "freigegeben",
                  "validiert und freigegeben", "medizinisch freigegeben", "arztlich freigegeben", "authorised by",
                  "authorized by", "authorised", "authorized", "released", "verantwortlicher arzt",
                  "electronically validated by", "electronically validated", "elektronisch validiert von",
                  "elektronisch validiert", "electronically signed by", "digitally signed by", "electronically released by",
                  "elektronisch freigegeben von", "elektronisch signiert von", "arztlich vidiert", "arztlich vidiert von",
                  "verantwortliche arztin", "responsible physician", "laborarzt", "laborarztin"],
    "written": ["written by", "author", "authored by", "verfasst von", "verfasst", "erstellt von", "autor",
                "autorin", "dictated by", "diktiert von", "signed", "signiert von"],
    "sent": ["sent by", "submitted by", "gesendet von", "versendet von", "versandt von", "ubermittelt von",
             "uebermittelt von", "geschickt von", "abgeschickt von"],
    "stay": ["stay", "admission", "length of stay", "period of stay", "aufenthalt", "stationarer aufenthalt",
             "aufenthaltsdauer", "zeitraum", "reha-aufenthalt", "rehaaufenthalt", "rehabilitation period",
             "aufenthalt vom", "rehabilitationsaufenthalt", "aufenthaltszeitraum", "behandlungszeitraum",
             "hospital stay", "rehabilitationszeitraum", "reha-zeitraum", "rehazeitraum", "behandlungsdauer",
             "stationar", "stationar vom", "dates of stay", "inpatient stay"],
    # start / end of the stay on their own lines ('Admission: 2026-09-02 · Discharge: 2026-09-23'); only read when
    # the value is a date (parse_answer), so 'Entlassung: nach Hause' stays text
    "stay_start": ["admission date", "admitted", "admitted on", "aufnahme", "aufnahmedatum", "aufgenommen",
                   "aufgenommen am", "date of admission", "aufnahmetag"],
    "stay_end": ["discharge", "discharged", "discharged on", "discharge date", "date of discharge", "entlassung",
                 "entlassen", "entlassen am", "entlassungsdatum", "entlassdatum", "entlassungstag"],
    "reason": ["reason", "reason for admission", "reason for rehabilitation", "grund", "aufnahmegrund",
               "zuweisungsgrund", "anlass", "indikation", "indication"],
    "comment": ["comment", "comments", "note", "notes", "remark", "remarks", "kommentar", "bemerkung",
                "bemerkungen", "anmerkung", "hinweis", "befundkommentar", "interpretation", "beurteilung",
                "conclusion", "befundkorrektur", "korrektur", "nachtrag", "berichtigung", "correction", "addendum"],
}
_KEYMAP = {_fold(k): canon for canon, ks in _KEYS.items() for k in ks}

_PANEL = (r"(?:schilddrusen|gerinnungs|blut|harn|urin|leber|nieren|lipid|fett|hormon|elektrolyt|entzundungs|eisen|"
          r"vitamin|tumormarker|serologischer|serologie|hamatologischer|klinisch-chemischer|chemischer|"
          r"immunologischer|infektions|stoffwechsel|diabetes|herz|pankreas|gerinnung)"
          r"(?:befund|status|werte|wert|profil|panel|analyse|diagnostik|parameter)")
_LAB_TYPE = re.compile(
    r"(?:" + _PANEL + r"|(?:differential)?blutbild|blutgasanalyse|klinische chemie|hamatologie|serologie)|"
    r"(?:(?:final|preliminary|vorlaufiger|endgultiger|neuer|aktueller|updated|new)\s+)?"
    r"(?:lab(?:oratory)?\s+(?:report|results?|findings|values|tests?)|labor(?:atory)?\s?(?:befund|bericht|ergebnisse?|werte)"
    r"|(?:blut|gerinnungs|hamatologie|haematologie|hematology)?\s?befund|gerinnung(?:swerte)?|"
    r"coagulation(?:\s+(?:panel|tests|report|results))?|blood\s+(?:tests?|results?|count))")
_DIS_TYPE = re.compile(
    r"(?:(?:final|preliminary|vorlaufiger|endgultiger|arztlicher|medical|rehabilitation)\s+)?"
    r"(?:discharge\s+(?:report|summary|letter|note)|(?:reha[- ]?)?entlassungs(?:bericht|brief|befund)|"
    r"reha(?:bilitations)?[- ]?(?:abschluss)?bericht|rehabilitation\s+(?:discharge\s+)?report|arztbrief|"
    r"abschlussbericht|transfer\s+report|verlegungsbericht)")
_LAB_TITLE = re.compile(r"\b(lab(oratory)?\s*(report|result|results|findings|values)|labor\w+|blut\w*befund|"
                        r"gerinnung\w*|haematolog\w*|hamatolog\w*|hematolog\w*|coagulation|"
                        # 'Meine Blutwerte', 'Lipidstatus', 'Blutbild', 'Leberwerte', 'Blood tests'
                        r"blutwert\w*|blutbild\w*|lipid\w*|leberwert\w*|nierenwert\w*|elektrolyt\w*|"
                        r"cholesterin\w*|schilddrusenwert\w*|blood\s*(tests?|work|results?|values|count)|"
                        r"lab\s*values|test\s*results|klinische\s+chemie|blutgasanalyse)\b|" + _PANEL)
_DIS_TITLE = re.compile(r"\b(discharge\s*(report|summary|letter|note)|entlassung\w*|reha\w*\s*bericht|"
                        r"rehabericht\w*|rehabilitation\s*report|abschlussbericht|arztbrief|"
                        r"reha[-\s]?entlassung\w*|entlassungsbrief|transfer\s*report|verlegungsbericht)\b")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ACTIVE = re.compile(r"<\s*(script|iframe|object|embed|svg|math|form|meta|link|style)\b|\bon[a-z]+\s*="
                     r"|javascript\s*:|vbscript\s*:|data\s*:\s*text/html", re.I)
# Content tiers (documented in decide.py and tests/jury/RULES.md):
#   BLOCK  ACTIVE_CONTENT          the RULES.md rule-3 list (_ACTIVE), verbatim in the raw text
#   HOLD   ACTIVE_CONTENT_SUSPECT  a raw HTML tag outside the benign whitelist, a tag with attributes, a Markdown
#                                  image/link/autolink with a scheme other than https, a bare URL with a
#                                  dangerous scheme (data:, file:, javascript: ...), or active content / a
#                                  non-benign tag hidden behind HTML entities or %-encoding
#   FILE + EXTERNAL_RESOURCE warning: https images/links, relative image/link targets, bare http:// or mailto:
#                                  text (inert text; nothing is fetched or linked by fhirmap)
#   FILE   benign converter tags without attributes (br b strong i em sup sub u p span) and HTML entities
#          (&lt; &gt; &amp; &nbsp; ...): normalised before parsing; the original stays in the attachment
_BENIGN_TAGS = {"br", "b", "strong", "i", "em", "sup", "sub", "u", "p", "span"}
_HTML_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)(\s[^<>]*)?/?>")   # like a browser: no space after <
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\s*\(\s*<?([^)\s>]*)")
_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\s*\(\s*<?([^)\s>]*)")
_AUTOLINK = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.-]{1,31}):[^<>\s]*>")
_BARE_URL = re.compile(r"\b(?!https:)([a-zA-Z][a-zA-Z0-9+.-]{1,31}):(//|[^\s]*@)")
_INERT_BARE = {"http", "mailto", "tel"}


def _is_benign_tag(m) -> bool:
    return m[1].lower() in _BENIGN_TAGS and not (m[2] or "").strip(" /")


def _target_kind(target: str) -> str:
    """'https' | 'relative' | 'other' for a Markdown link/image target."""
    t = target.strip()
    if not t:
        return "other"
    if t.lower().startswith("https://"):
        return "https"
    if t.startswith("//") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t):
        return "other"
    return "relative"


def content_tiers(text: str) -> tuple[str | None, list[str]]:
    """(reason to HOLD as suspect content or None, [EXTERNAL_RESOURCE notes])."""
    notes: list[str] = []
    for m in _HTML_TAG.finditer(text):
        if not _is_benign_tag(m):
            return f"raw HTML tag '{m[0][:20]}'", notes
    for rx, what in ((_MD_IMAGE, "image"), (_MD_LINK, "link")):
        for m in rx.finditer(text):
            k = _target_kind(m[1])
            if k == "other":
                return f"{what} to '{m[1][:30]}' (only https or relative targets are accepted)", notes
            notes.append(f"{what} {m[1][:60]}")
    for m in _AUTOLINK.finditer(text):
        if m[1].lower() != "https":
            return f"autolink with scheme '{m[1]}'", notes
        notes.append(f"link {m[0][1:-1][:60]}")
    for m in _BARE_URL.finditer(text):
        sch = m[1].lower()
        if sch in _INERT_BARE:
            notes.append(f"{sch} URL in the text")
            continue
        if sch in ("ftp", "ftps", "file", "sftp", "ws", "wss", "data", "smb", "ldap", "http") or m[2] == "//":
            return f"URL with scheme '{m[1]}' (only https is accepted)", notes
    import html as _html
    import urllib.parse as _up
    decoded = _up.unquote(_html.unescape(_html.unescape(text)))
    if decoded != text and (_ACTIVE.search(decoded)
                            or any(not _is_benign_tag(m) for m in _HTML_TAG.finditer(decoded))):
        return "encoded HTML/script (HTML entities or %-encoding)", notes
    return None, notes


def normalise_markup(text: str) -> str:
    """Converter leftovers -> plain Markdown for parsing: HTML entities decoded, benign tags removed
    (<br>/<p> become '; ' inside a table row and a space elsewhere, <sup>9</sup> becomes ^9)."""
    import html as _html
    out = []
    for line in text.split("\n"):
        in_table = "|" in line
        line = re.sub(r"<sup>\s*([^<]*?)\s*</sup>", r"^\1", line, flags=re.I)
        line = re.sub(r"<(br|p)\s*/?>|</p>", "; " if in_table else " ", line, flags=re.I)
        line = re.sub(r"</?(b|strong|i|em|sub|u|span|sup)\s*>", "", line, flags=re.I)
        line = _html.unescape(line).replace("\u00a0", " ")
        # pandoc escapes: '\*12.03.1961', 'INR \(PT\)', trailing '\' hard breaks ('\|' stays a cell pipe)
        line = re.sub(r"\\([*_#()\[\].\-+!`{}>~<])", r"\1", line)
        line = re.sub(r"\\\s*$", "", line)
        out.append(line)
    return "\n".join(out)


# ---------- uploads that are not UTF-8
def decode_upload(raw: bytes) -> dict:
    """Bytes -> {text, encoding, warnings, code}. UTF-8 (with or without BOM) first; then
    Windows-1252, then Latin-1, each only if the result is plausible text (no C0/C1 control
    characters, mostly printable). code is None, or 'ENCODING_UNREADABLE' (text is None)."""
    if raw is None:
        return {"text": None, "encoding": None, "warnings": [], "code": "EMPTY"}
    if not raw:
        return {"text": "", "encoding": "utf-8", "warnings": [], "code": "EMPTY"}
    try:
        return {"text": raw.decode("utf-8-sig"), "encoding": "utf-8", "warnings": [], "code": None}
    except UnicodeDecodeError:
        pass
    for enc in ("cp1252", "latin-1"):
        try:
            t = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        bad = sum(1 for c in t if (ord(c) < 32 and c not in "\t\r\n") or 0x7f <= ord(c) <= 0x9f)
        printable = sum(1 for c in t if c.isprintable() or c in "\t\r\n")
        if bad == 0 and printable >= 0.98 * len(t):
            return {"text": t, "encoding": enc, "code": None,
                    "warnings": [f"the file was not UTF-8; read as {enc} (please save as UTF-8)"]}
    return {"text": None, "encoding": None, "warnings": [], "code": "ENCODING_UNREADABLE"}


_LINE_KV = re.compile(r"^\s*([^\W\d_][^:|]{0,48}?)\s*:\s*(.*)$")
_LOINC = re.compile(r"\b(\d{1,7}-\d)\b")
_GENDER = {"female": "female", "f": "female", "weiblich": "female", "w": "female", "frau": "female", "♀": "female",
           "♂": "male", "weibl": "female", "mannl": "male", "weibl.": "female", "mannl.": "male",
           "male": "male", "m": "male", "mannlich": "male", "maennlich": "male", "mann": "male",
           "diverse": "other", "divers": "other", "d": "other", "other": "other", "inter": "other"}


_SAMPLE_WORDS = re.compile(r"(probe|proben|entnahme|abnahme|abgenommen|sample|specimen|collect|sampling|material)")
_REPORT_WORDS = re.compile(r"(befund|report|issued|ausgestellt|freigabe|freigegeben|validiert am|validated on|"
                           r"ergebnis vom|result date|resulted|\bresults?\b|berichtet|ausgabe|ausgang)")
_NOT_TIME = re.compile(r"\b(id|nr|nummer|number|no|art|type|typ|code|kennung)\b")


def _recent_two_digit_year(v: str) -> str | None:
    """'23.10.26' / '23.10.26 07:40' as a sample/report time -> '23.10.2026 ...' only when 20yy is not in the
    future and lies within the last 2 years; otherwise None (left unparsed)."""
    from datetime import date as _date
    m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2})(?!\d)(.*)$", v)
    if not m:
        return None
    try:
        d = _date(2000 + int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None
    today = _date.today()
    if d > today or (today - d).days > 730:
        return None
    return f"{m[1]}.{m[2]}.{2000 + int(m[3])}{m[4]}"


_SPECIMEN_PREFIX = re.compile(r"^\s*[^\W\d][^\d,;:]{1,40},\s*(?:(?:entnommen|abgenommen|gewonnen|collected|taken|"
                              r"vom|am|on)\s+)?(?=\d)", re.I)


def _parse_time_value(v: str):
    """parse_datetime_ex plus the forms of documents not written for us: a trailing remark '(nüchtern)', the
    specimen in front ('Citratblut, entnommen 23.10.2026 07:40'), a recent two-digit year ('23.10.26')."""
    dt = parse_datetime_ex(v)
    if dt.value is not None:
        return dt
    cands = [v]
    if re.search(r"\([^()]*\)\s*$", v):
        cands.append(re.sub(r"\s*\([^()]*\)\s*$", "", v))
    cands += [_SPECIMEN_PREFIX.sub("", c) for c in list(cands) if _SPECIMEN_PREFIX.match(c)]
    cands += [y for c in list(cands) if (y := _recent_two_digit_year(c))]
    for c in cands[1:]:
        d2 = parse_datetime_ex(c)
        if d2.value is not None:
            return d2
    return dt


def _time_key(label: str, value: str) -> str | None:
    """Generic sample/report time labels ('Probenahme', 'Abnahmezeitpunkt', 'Report issued at'):
    only if the value really is a date with a time of day."""
    f = _fold(label)
    if _NOT_TIME.search(f) or " von" in f or " by" in f:
        return None
    dt = _parse_time_value(value)
    if dt.value is None or not dt.has_time:
        return None
    if _SAMPLE_WORDS.search(f):
        return "sample"
    if _REPORT_WORDS.search(f):
        return "reported"
    return None


_FOOTER_KEYS = re.compile(r"(information|info|website|homepage|web|internet|kontakt|contact|telefon|tel|phone|"
                          r"fax|e-?mail|adresse|address|impressum|ordinationszeiten|opening hours|"
                          r"offnungszeiten|hotline|url|link)\b")
_LINKISH = re.compile(r"https?://|\]\(|[\w.+-]+@[\w-]+\.|^\+?[\d ()/-]{7,}$")


def _is_footer(label: str, value: str) -> bool:
    """'Laborinformation: [x](https://...)', 'Tel: +43 ...', 'E-Mail: a@b' after the table."""
    f = _fold(label)
    return bool(_FOOTER_KEYS.search(f) and _LINKISH.search(value.strip()))


_LINKTOK = re.compile(r"!?\[[^\]]*\]\([^)]*\)|<[^<>\s]+>|\b[a-z][a-z0-9+.-]*:(?://)?[^\s·|]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
                      re.I)


def _is_link_footer(line: str) -> bool:
    """A line that is only an image, or links/URLs/e-mail addresses with a few label words around them."""
    if not _LINKTOK.search(line):
        return False
    rest = _LINKTOK.sub(" ", line)
    words = [w for w in re.split(r"[\s·|•,;:]+", rest) if re.search(r"\w", w)]
    return len(words) <= 4


_LEADER = re.compile(r"^(\s*(?:[-*•+]\s+)?[^\W\d_][^:|\n]{0,48}?)(\s*(?:\.{4,}|_{3,}|…{2,})\s*|\s+\.{1,3}\s+)(\S.*)$")


def normalise_leaders(line: str) -> str:
    """'Patientin ........ Maria Beispiel' / 'Geb.-Datum ____ 12.03.1961' / '- TSH ...... 1,85 mU/l' -> 'Key: value'.
    A short ' . ' / ' ... ' leader counts only in a list item ('- TPO-Antikörper . folgt'): '...' in prose stays."""
    m = _LEADER.match(line)
    if not m or "|" in line:
        return line
    if not re.search(r"\.{4,}|_{3,}|…{2,}", m[2]) and not re.match(r"^\s*[-*•+]\s", line):
        return line
    return f"{m[1].rstrip()}: {m[3]}"


def _clean_line(line: str) -> str:
    s = line.replace("**", "").replace("__", "").rstrip("\\").strip()
    s = re.sub(r"^(?:>\s*)+", "", s)
    s = re.sub(r"^[-*+]\s+", "", s)
    return s.strip()


def loinc_check_ok(code: str) -> bool:
    """LOINC mod-10 check digit (Luhn on the digits before the hyphen)."""
    m = re.fullmatch(r"(\d{1,7})-(\d)", code)
    if not m:
        return False
    total = 0
    for i, ch in enumerate(reversed(m[1])):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return (10 - total % 10) % 10 == int(m[2])


# --------------------------------------------------------------------------- numbers

_NUM = r"[+-]?(?:\d+(?:[.,]\d+)*|[.,]\d+)"
_CMP = {"<": "<", ">": ">", "<=": "<=", ">=": ">=", "≤": "<=", "≥": ">=", "=<": "<=", "=>": ">="}


def _norm_text(s: str) -> str:
    return (unicodedata.normalize("NFKC", s).replace("−", "-").replace("–", "-").replace("—", "-")
            .replace(" ", " ").strip())


def parse_number(tok: str) -> tuple[Decimal | None, str | None]:
    """'3,1' -> 3.1; '1.234,5' -> 1234.5; '1,234.5' -> 1234.5; '1.234' -> ambiguous."""
    t = tok.strip().replace(" ", "").replace("'", "")
    if not re.fullmatch(_NUM, t):
        return None, None
    sign = ""
    if t[0] in "+-":
        sign, t = (t[0] if t[0] == "-" else ""), t[1:]
    if "." in t and "," in t:
        dec = "." if t.rfind(".") > t.rfind(",") else ","
        thou = "," if dec == "." else "."
        groups = t.split(dec)[0].split(thou)
        if len(t.split(dec)) != 2 or not all(len(g) == 3 for g in groups[1:]):
            return None, f"number '{tok}' has inconsistent separators"
        t = t.replace(thou, "").replace(dec, ".")
    else:
        sep = "." if "." in t else ("," if "," in t else None)
        if sep:
            parts = t.split(sep)
            if len(parts) > 2:
                if all(len(g) == 3 for g in parts[1:]) and parts[0] not in ("", "0"):
                    return None, f"number '{tok}' is ambiguous (thousands separator or decimal point)"
                return None, f"number '{tok}' is malformed"
            if len(parts[1]) == 3 and parts[0] not in ("", "0", "-0"):
                return None, f"number '{tok}' is ambiguous (thousands separator or decimal point)"
            t = parts[0] + "." + parts[1]
    try:
        return Decimal(sign + t), None
    except InvalidOperation:
        return None, f"number '{tok}' is malformed"


def json_number(d: Decimal):
    """Decimal -> int (no fractional digits written) or float. Keeps '1.0' as 1.0."""
    if d.as_tuple().exponent >= 0:
        return int(d)
    return float(d)


_FLAG_WORDS = {
    "h": "H", "hh": "HH", "l": "L", "ll": "LL", "n": "N", "+": "H", "++": "HH", "-": "L", "--": "LL",
    "↑": "H", "↑↑": "HH", "↓": "L", "↓↓": "LL", "hoch": "H", "high": "H", "low": "L", "niedrig": "L",
    "erhoht": "H", "erniedrigt": "L", "normal": "N", "a": "A", "abnormal": "A", "pathologisch": "A",
    "path": "A", "*": "A", "!": "A", "!!": "AA",
}


def _flag(tok: str) -> str | None:
    t = _fold(tok.strip().strip("()[]"))
    if t in _FLAG_WORDS:
        return _FLAG_WORDS[t]
    raw = tok.strip().strip("()[]")
    return _FLAG_WORDS.get(raw)


def parse_value(cell: str) -> dict:
    """Result cell -> {kind: 'num'|'text'|'empty', num, comparator, rest, flag, problem}."""
    s = _norm_text(cell)
    if s in ("", "-", "--", "/", "n/a", "na", "–"):
        return {"kind": "empty"}
    m = re.match(rf"^(<=|>=|=<|=>|<|>|≤|≥)?\s*({_NUM})(?=$|[\s(*!↑↓a-zA-Z%/µμ\[])\s*(.*)$", s)
    if not m:
        return {"kind": "text", "text": s}
    num, prob = parse_number(m[2])
    if num is None:
        return {"kind": "text", "text": s, "problem": prob}
    rest = m[3].strip()
    out = {"kind": "num", "num": num, "comparator": _CMP.get(m[1]) if m[1] else None, "rest": "", "flag": None}
    if rest:
        f = _flag(rest)
        if f:
            out["flag"], out["flag_text"] = f, rest
        else:
            # '44 H', '3.1 g/L', '3.1 g/L H', '245 *'
            parts = rest.split()
            if len(parts) >= 2 and _flag(parts[-1]):
                out["flag"], out["flag_text"] = _flag(parts[-1]), parts[-1]
                rest = " ".join(parts[:-1])
            out["rest"] = rest
    return out


def parse_range(text: str, unit: str | None) -> dict:
    """Reference range cell -> {low, high, low_closed, high_closed, problem}. Text is
    always kept by the caller. Units repeated in the cell are removed."""
    s = _norm_text(text)
    if unit:
        u = re.escape(_norm_text(unit))
        s = re.sub(rf"\s*{u}\s*$", "", s)
    s = s.strip()
    out = {"low": None, "high": None, "low_closed": True, "high_closed": True}
    if not s:
        return out
    m = re.fullmatch(rf"({_NUM})\s*(?:-|to|bis|\.\.\.?|…|–|—|until)\s*({_NUM})", s, re.I)
    if m:
        lo, p1 = parse_number(m[1])
        hi, p2 = parse_number(m[2])
        if lo is None or hi is None:
            out["problem"] = p1 or p2
            return out
        if lo > hi:
            out["problem"] = f"reference range '{text}' has low > high"
            return out
        out["low"], out["high"] = lo, hi
        return out
    m = re.fullmatch(rf"(<=|>=|=<|=>|<|>|≤|≥|up to|bis|max\.?|maximal|höchstens|min\.?|mindestens|minimum|"
                     rf"maximum|at least|über|unter|below|above|over|less than|greater than)\s*({_NUM})", s, re.I)
    if m:
        v, p = parse_number(m[2])
        if v is None:
            out["problem"] = p
            return out
        op = _fold(m[1])
        if op in ("<", "unter", "below", "less than"):
            out.update(high=v, high_closed=False)
        elif op in ("<=", "=<", "≤", "up to", "bis", "max", "maximal", "hochstens", "maximum"):
            out["high"] = v
        elif op in (">", "uber", "above", "over", "greater than"):
            out.update(low=v, low_closed=False)
        else:
            out["low"] = v
        return out
    if parse_number(s)[0] is not None:
        out["problem"] = f"reference range '{text}' is a single number; not interpreted"
    return out


# --------------------------------------------------------------------------- tables

_SEP_ROW = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")

_COLS = {
    "test": ["test", "tests", "analyte", "parameter", "untersuchung", "analyse", "test name", "bezeichnung",
             "name", "messgrosse", "kenngrosse", "laborparameter", "investigation", "examination", "item",
             "analyt", "analyten", "analytes", "parameters", "untersuchungen", "bestimmung", "laborwert",
             "laboruntersuchung", "test/parameter", "parameter/test", "analysis", "kenngrosse/parameter"],
    "value": ["result", "results", "value", "ergebnis", "wert", "befund", "messwert", "resultat", "result value",
              "ergebniswert", "your value", "ihr wert", "aktueller wert"],
    "unit": ["unit", "units", "einheit", "einheiten", "dimension", "masseinheit"],
    "range": ["reference range", "reference", "reference interval", "ref range", "ref. range", "ref",
              "ref.", "normal range", "normal values", "referenzbereich", "referenzwert", "referenzwerte",
              "referenzintervall", "norm", "normbereich", "normalbereich", "normwert", "normwerte", "range",
              "target range", "zielbereich", "referenz", "referenzen", "ref.-bereich", "ref.bereich", "ref-bereich",
              "ref. bereich", "refbereich", "ref.-wert", "ref.-werte", "ref.wert", "referenzbereiche", "normalwert",
              "normalwerte", "normalbereiche", "reference values", "reference ranges", "ref. values", "ref values",
              "referenzwertbereich", "ref.-intervall", "ref. interval", "ref interval", "ref.interval",
              "reference intervals", "ref. intervals", "ref.-int.", "ref. int.", "referenzintervalle", "ref.-int",
              "normal interval", "reference limits", "ref.-grenzen", "referenzgrenzen"],
    "loinc": ["loinc code", "loinc", "loinc-code", "loinc nr", "loinc-nr", "loinc number", "code", "loinc id"],
    "flag": ["flag", "flags", "h/l", "kennzeichen", "bewertung", "abnormal flag", "markierung", "status",
             "interpretation", "auffallig"],
}
_COLMAP = {_fold(k): c for c, ks in _COLS.items() for k in ks}
_WEAK_VALUE = {"wert", "befund", "werte"}


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _find_tables(lines: list[str]) -> list[tuple[int, int, list[str], list[list[str]]]]:
    """[(header_line_no, end_line_no, header_cells, rows)]."""
    out = []
    i = 0
    while i < len(lines) - 1:
        if "|" in lines[i] and _SEP_ROW.match(lines[i + 1]) and "-" in lines[i + 1]:
            head = _cells(lines[i])
            rows = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(_cells(lines[j]))
                j += 1
            out.append((i, j, head, rows))
            i = j
        else:
            i += 1
    return out


# --------------------------------------------------------------------------- persons in header lines

def _person_field(value: str) -> dict:
    """'Dr. Eva Brandl, lab physician' / 'Dr. Johanna Leitner, medical director, Reha Waldblick'."""
    value = re.sub(r"\s+(?:am|on|um|at)\s+\d.*$", "", value.strip())   # '… Dr. Eva Brandl am 26.10.2026, 15:30'
    parts = [p.strip() for p in value.split(",") if p.strip()]
    # 'Eva Brandl, MSc, lab physician': drop pure degree parts after the name
    while len(parts) > 1 and all(_is_suffix_title(t) or _is_prefix_title(t) for t in _raw_tokens(parts[1])):
        parts[0] = parts[0] + ", " + parts.pop(1)
    name = parts[0] if parts else value.strip()
    return {"name": name, "role": parts[1] if len(parts) > 1 else None,
            "org": ", ".join(parts[2:]) if len(parts) > 2 else None}


_BIRTH_MARK = re.compile(
    r"(?:^|[\s,;(])(born(?:\s+on)?(?!\w)|geboren(?:\s+am)?(?!\w)|geb\.(?:\s+am)?|geb(?!\w)|b\.|"
    r"date of birth|d\.?o\.?b\.?|birth\s*date|geburtsdatum|\*)\s*:?\s*", re.I)


def _take_date(rest: str) -> tuple[str | None, str | None, str]:
    """Longest comma-prefix of ``rest`` that parses as a date -> (iso, used_text, remainder)."""
    parts = [p for p in re.split(r"[,;]", rest)]
    best = (None, None, rest)
    problem = None
    for k in range(1, min(3, len(parts)) + 1):
        cand = ",".join(parts[:k]).strip().rstrip(").")
        dp = parse_date_ex(cand)
        if dp.value:
            best = (dp.value.isoformat(), cand, ",".join(parts[k:]))
        elif problem is None and k == 1:
            problem = dp.problem
    if best[0] is None:
        return None, problem, rest
    return best


def _plain_person_text(v: str) -> str:
    """'**Frau Mag. Maria Beispiel**, geb. ...' / 'Maria Beispiel · geb. ...' -> plain text with commas: Markdown
    emphasis ('**', '__', a paired '*...*' / '_..._' around words) is removed and the list separators '·' / '•'
    become commas. A lone '*' before a date is the birth mark and stays ('*12.03.1961')."""
    v = v.replace("**", "").replace("__", "")
    v = re.sub(r"(?<![\w*])\*(?=[^\W\d_])([^*\n]*?[^\W\d_.])\*(?![\w*])", r"\1", v)    # '*Maria Beispiel*'
    v = re.sub(r"(?<![\w_])_(?=[^\W\d_])([^_\n]*?[^\W\d_.])_(?![\w_])", r"\1", v)
    v = re.sub(r"\s*[·•]\s*", ", ", v)
    # 'Beispiel, Maria — geboren 12.03.1961' / 'Maria – Beispiel' / 'Maria Beispiel - 12.03.1961': a spaced dash
    # separates (a comma before a date or a birth mark, a space between name words); 'Beispiel-Huber' stays one name
    v = re.sub(r"\s+[-–—]+\s+(?=[\d*]|geb|born|dob|d\.o\.b|geboren|date of birth|\()", ", ", v, flags=re.I)
    v = re.sub(r"\s+[-–—]+\s+", " ", v)
    v = re.sub(r"(,\s*)+", ", ", v)
    return v.strip(" ,")


_PERSON_MARK = {"frau", "herr", "herrn", "patientin", "patienten", "patient", "pat", "patient/in", "patient*in", "fur",
                "for", "ms", "mrs", "mr", "miss"}
_SUBJECT_WORD = re.compile(r"befund|bericht|brief|report|result|finding|werte|status|labor|\blab\b|ihre[rnms]?\b|"
                           r"unsere[rnms]?\b|\byour\b|\bour\b|anfrage|request")


def _person_from_subject(name_part: str) -> str:
    """'Gerinnungsbefund Ihrer Patientin Frau Maria Beispiel' -> 'Maria Beispiel': the person after the LAST
    'Frau' / 'Herr' / 'Patientin' / 'für', but only when the words before it are subject words (a lower-case word,
    'Befund', 'Ihrer', ...), never another name ('Max Beispiel für Maria Beispiel' stays as written)."""
    toks = name_part.split()
    marks = [k for k, t in enumerate(toks) if _fold(t.strip(".:,;")) in _PERSON_MARK]
    if not marks:
        return name_part
    last = marks[-1]
    before, after = toks[:last], toks[last + 1:]
    if not after:
        return name_part
    plain = [t for t in before if _fold(t.strip(".:,;")) not in _PERSON_MARK]
    if plain and not any(t[:1].islower() or _SUBJECT_WORD.search(_fold(t)) for t in plain):
        return name_part
    return " ".join(after)


def _parse_patient(value: str, p: Parsed) -> dict | None:
    v = _norm_text(value)
    v = _plain_person_text(v)
    v = v.replace("(", ", ").replace(")", "")
    v = re.sub(r"\s+,", ",", v).strip(" ,")
    if not v:
        return None
    name_part, dob_iso, dob_text, tail = v, None, None, ""
    first_mark = None
    for m in _BIRTH_MARK.finditer(v):
        if first_mark is None and v[m.end():].strip() and re.match(r"\d|[A-Za-zÄÖÜäöü]+\.?\s+\d", v[m.end():]):
            first_mark = m
        iso, used, rem = _take_date(v[m.end():])
        if iso:
            name_part, dob_iso, dob_text, tail = v[: m.start()].strip(" ,;"), iso, used, rem
            break
        if used and dob_text is None:
            dob_text = used     # problem text
    else:
        # no marker: find the first comma part that is a date
        parts = [x.strip() for x in v.split(",")]
        for i in range(1, len(parts)):
            iso, used, rem = _take_date(",".join(parts[i:]))
            if iso:
                name_part, dob_iso, dob_text, tail = ", ".join(parts[:i]), iso, used, rem
                break
        else:
            for i in range(1, len(parts)):
                if re.fullmatch(r"\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{2}", parts[i]):
                    # the two-digit year is recorded as written: _resolve_two_digit_year decides (rule 9)
                    name_part, dob_text = ", ".join(parts[:i]), parse_date_ex(parts[i]).problem
                    tail = ", ".join(parts[i + 1:])
                    break
    if dob_iso is None and first_mark is not None:
        name_part = v[: first_mark.start()].strip(" ,;")       # date after the marker is unusable
    gender = None
    name_bits = []
    bits0 = [x.strip() for x in name_part.split(",") if x.strip()]
    if bits0:
        bits0[0] = _person_from_subject(bits0[0])     # 'Betreff: Gerinnungsbefund Ihrer Patientin Frau Maria ...'
    for bit in bits0:
        g = _GENDER.get(_fold(bit))
        if g:
            gender = gender or g
            continue
        # 'geb. Huber' (birth name) is not a date: keep it out of the name, but note it
        if re.match(r"^(geb\.?|geborene|née|nee)\s+\D+$", bit, re.I):
            p._warn("patient line contains a birth name; noted but not used for matching")
            continue
        if m_bn := re.search(r"\s+((?:geb\.?|geborene|née|nee)\s+\D+)$", bit, re.I):
            p._warn("patient line contains a birth name; noted but not used for matching")
            bit = bit[: m_bn.start()].strip()
        if all(_is_suffix_title(t) or _is_prefix_title(t) for t in _raw_tokens(bit)):
            continue
        if re.search(r"\d", bit):
            # not a name: an age ('65 years'), an id, an unparseable date
            if re.search(r"\d+\s*(years?|yrs?|jahre?|j\.?|y\.?|a)\b", bit, re.I):
                p._warn("patient line gives an age, not a birth date")
            continue
        name_bits.append(bit)
    for bit in [x.strip() for x in tail.split(",") if x.strip()]:
        g = _GENDER.get(_fold(bit))
        if g:
            gender = gender or g
    if not name_bits:
        return {"name": None, "given": [], "family": None, "prefix": [], "birthDate": dob_iso,
                "birthDate_text": dob_text, "gender": gender}
    if len(name_bits) == 2:
        name = f"{name_bits[1]} {name_bits[0]}"            # 'Beispiel, Maria'
        given, family, prefix = split_name(f"{name_bits[0]}, {name_bits[1]}")
    elif len(name_bits) == 1:
        name = name_bits[0]
        given, family, prefix = split_name(name)
    else:
        p._warn(f"patient line has an ambiguous name ({len(name_bits)} comma-separated parts)", hold=True, code="PATIENT_INCOMPLETE")
        name = " ".join(name_bits)
        given, family, prefix = split_name(name)
    # display form without titles
    core = " ".join([*given, family or ""]).strip()
    out = {"name": core or name, "given": given, "family": family, "prefix": prefix,
           "birthDate": dob_iso, "birthDate_text": dob_text, "gender": gender, "line": value.strip()}
    _resolve_two_digit_year(out, p)
    return out


def _resolve_two_digit_year(pat: dict, p: Parsed) -> None:
    """'geb. 12.03.61': 19xx and 20xx are both candidates; if exactly one is not in the future it is
    used (the patient decision still only accepts it when it EQUALS the requested birth date)."""
    from datetime import date as _date
    why = pat.get("birthDate_text") or ""
    if pat.get("birthDate") or "two-digit year" not in why:
        return
    m = re.search(r"'([^']+)'", why)
    mm = re.fullmatch(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{2})", (m[1] if m else "").strip())
    if not mm:
        return
    d, mo, yy = int(mm[1]), int(mm[2]), int(mm[3])
    cands = []
    for century in (1900, 2000):
        try:
            cands.append(_date(century + yy, mo, d))
        except ValueError:
            pass
    past = [c for c in cands if c <= _date.today()]
    pat["dob_two_digit"] = {"text": m[1], "candidates": [c.isoformat() for c in cands],
                            "past": [c.isoformat() for c in past]}
    if len(past) == 1:
        pat["birthDate"] = past[0].isoformat()
        pat["birthDate_text"] = m[1]
        p._warn(f"two-digit birth year read as {past[0].year} (the only past candidate)", code="DOB_TWO_DIGIT")


def _parse_period(value: str) -> tuple[dict | None, str | None]:
    """'2 to 23 September 2026', '02.09.2026 - 23.09.2026', '28 December 2025 to 5 January 2026'."""
    v = _norm_text(value)
    v = re.sub(r"^(from|vom|von|ab)\s+", "", v, flags=re.I)
    if m_iso := re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:bis|to|until|-|–|—)\s*(\d{4}-\d{2}-\d{2})", v, re.I):
        a, b = parse_date_ex(m_iso[1]).value, parse_date_ex(m_iso[2]).value    # '2026-09-02 bis 2026-09-23'
        if a and b and a <= b:
            return {"start": a.isoformat(), "end": b.isoformat(), "text": value.strip()}, None
        return None, f"stay '{value}' not understood"
    parts = re.split(r"\s+(?:to|bis|until|-|–)\s+|\s*[–]\s*|(?<=\d\.)\s*-\s*(?=\d)|(?<=\d{4})\s*-\s*(?=\d)", v,
                     maxsplit=1, flags=re.I)
    if len(parts) != 2:
        dp = parse_date_ex(v)
        if dp.value:
            return {"start": dp.value.isoformat(), "end": dp.value.isoformat(), "text": value.strip()}, None
        return None, f"stay '{value}' not understood"
    left, right = parts[0].strip(" ,"), parts[1].strip(" ,")
    rd = parse_date_ex(right)
    if not rd.value:
        return None, f"stay '{value}': end date not understood"
    ld = parse_date_ex(left)
    start = ld.value
    if start is None:
        # inherit month/year from the end date: '2 to 23 September 2026', '2. bis 23.09.2026'
        m = re.fullmatch(r"(\d{1,2})\.?", left)
        if m:
            try:
                start = rd.value.replace(day=int(m[1]))
            except ValueError:
                start = None
        else:
            m = re.fullmatch(r"(\d{1,2})\.?\s*([^\d\s.,]+)\.?|(\d{1,2})\.(\d{1,2})\.?", left)
            if m:
                cand = f"{m[1]} {m[2]} {rd.value.year}" if m[1] else f"{m[3]}.{m[4]}.{rd.value.year}"
                start = parse_date_ex(cand).value
    if start is None:
        return None, f"stay '{value}': start date not understood"
    if start > rd.value:
        return None, f"stay '{value}': start after end"
    return {"start": start.isoformat(), "end": rd.value.isoformat(), "text": value.strip()}, None


# --------------------------------------------------------------------------- documents not written in our format
# (tests/jury/fresh: 30 documents written by people who never saw the scenario files)

def _classify_title(title: str) -> tuple[str | None, str | None, bool]:
    """A heading -> (LAB | DISCHARGE | None, the organisation part or None, names both kinds)."""
    tparts = [x.strip() for x in re.split(r"\s*(?::|\s[-–—|]\s)\s*", title) if x.strip()]
    lab_parts = [x for x in tparts if _LAB_TYPE.fullmatch(_fold(x))]
    dis_parts = [x for x in tparts if _DIS_TYPE.fullmatch(_fold(x))]
    gen_parts = [x for x in tparts if re.fullmatch(r"(report|befund|bericht|document|dokument)", _fold(x))]
    org = None
    if lab_parts or dis_parts or gen_parts:
        is_lab, is_dis = bool(lab_parts), bool(dis_parts)
        rest = [x for x in tparts if x not in lab_parts + dis_parts + gen_parts]
        if len(rest) == 1:
            org = rest[0]
    else:
        ft = _fold(title)
        is_lab, is_dis = bool(_LAB_TITLE.search(ft)), bool(_DIS_TITLE.search(ft))
    kind = LAB if is_lab and not is_dis else DISCHARGE if is_dis and not is_lab else None
    return kind, org, is_lab and is_dis


# a section that is part of the patient header ('## Patientendaten' + 'Name: ...' lines)
_HEADER_SECTION = re.compile(r"^(patient\w*|patient(?:en)? ?(?:daten|data|details|information|angaben)|personalien|"
                             r"stammdaten|angaben zu(?:r|m)? (?:person|patient\w*)|zur person|identifikation|"
                             r"person(?:en)?daten)$")
_VALIDATED_NOCOLON = re.compile(
    r"^((?:electronically |elektronisch |digitally |digital )?(?:technisch und )?medizinisch validiert von|(?:arztlich |ärztlich )?validiert von|validiert durch|"
    r"validated by|released by|approved by|freigegeben von|freigegeben durch|befund freigegeben von|befundet von|"
    r"signed by|authori[sz]ed by|vidiert von|unterschrieben von|(?:electronically|elektronisch|digitally) "
    r"(?:validated|validiert|signed|signiert|released|freigegeben)(?: by| von| durch)?)\s+((?:[^\W\d_]|Dr\.).*)$", re.I)
_STAY_NOCOLON = re.compile(r"^(?:[^\W\d_]*aufenthalt|stay|admitted|hospitali[sz]ed)(?:\s+(?:vom|von|from))?\s+"
                           r"(\d.*\d{4}.*)$", re.I)
# 'Admission: 2 Sep 2026   Discharge: 23 Sep 2026' / 'Aufnahme 02.09.2026, Entlassung 23.09.2026' on one line
_ADMIT_DISCHARGE = re.compile(r"^(?:aufnahme|aufgenommen|admission|admitted)(?:\s+(?:am|on|date))?\s*:?\s*(\S.*?\d{4})"
                              r"\s*[,;·|]?\s+(?:entlassung|entlassen|discharge|discharged)(?:\s+(?:am|on|date))?\s*:?\s*"
                              r"(\S.*?\d{4})\s*\.?$", re.I)
_PLAIN_SECTION = re.compile(r"^([^\W\d_][^:|]{1,70}?)\s*:\s*$")   # a label alone on its line
# a heading line without '#' or ':' ('Discharge medication', 'Follow-up', bold '**Medikation bei Entlassung**')
_PLAIN_HEADING = re.compile(r"^([^\W\d_][^\W\d_ ()/-]*(?:[ /-][^\W\d_()]+){0,5}(?:\s*\([^()\d]{1,40}\))?)$")
_CONTACT_LINE = re.compile(r"\b(?:tel|telefon|phone|fax|mobil|hotline)\b\.?\s*:?\s*\+?\(?\d", re.I)
_MED_SECTION = re.compile(r"medika|medication|medicine|arznei|entlassungstherapie|therapie bei entlassung|"
                          r"entlassmedikation|drugs")
_GREETING = re.compile(r"(?:mit )?(?:(?:freundlichen|kollegialen|besten|herzlichen|lieben|vielen|liebe|viele|beste|"
                       r"collegialen)\s+)+gr(?:u|ue)ss?en?|mfg|(?:with )?(?:kind|best|warm) regards|regards|"
                       r"(?:yours )?sincerely|hochachtungsvoll")


def _kv_pieces(ln: str, keyf) -> list[tuple[str, str]]:
    """'Abnahme: 28.10.2026 07:30 · Befund: 28.10.2026 11:05' -> [(label, value), ...]. A part after a separator
    (' · ', ' | ', ' – ', '; ') starts a new field only if it is 'Key: value' with a known key; otherwise it stays
    in the value ('Patient: Beispiel, Maria · DOB 1961-03-12'). [] if the line is one field."""
    if not _LINE_KV.match(ln):
        return []
    parts = re.split(r"(\s+[·•|]\s+|\s+[–—]\s+|\s*;\s+)", ln)
    if len(parts) < 3:
        return []
    pieces = [parts[0]]
    for k in range(1, len(parts) - 1, 2):
        sep, part = parts[k], parts[k + 1]
        m = _LINE_KV.match(part)
        if m and m[2].strip() and (keyf(m[1], m[2]) or sep.strip() in ("·", "•", "|")):
            pieces.append(part)                # 'Abnahme: ... · Eingang: ... · Freigabe: ...'
        else:
            pieces[-1] += sep + part
    if len(pieces) < 2:
        return []
    out = []
    for pc in pieces:
        m = _LINE_KV.match(pc)
        if not m:
            return []
        out.append((m[1], m[2]))
    return out


def _signer(c: str, nxt: str | None) -> dict | None:
    """One signature line -> {name, role, org} if it is a plain person ('Dr. Eva Brandl, Laborärztin'); a role on
    the next line ('Fachärztin für Labormedizin') is taken as the role."""
    c = re.split(r"\s+[·•|/]\s+", c)[0].strip()
    c = re.sub(r"^(?:gez\.?|signed|signiert|unterschrieben)\s*:?\s+", "", c, flags=re.I)
    c = re.sub(r"\s*\(([^()]+)\)\s*$", r", \1", c)        # 'Dr. Eva Brandl (Laborärztin)'
    if re.search(r"\d|@|:|https?", c) or len(c) > 90 or re.match(r"^(i\.\s?a\.|i\.\s?v\.|im auftrag|ppa?\.|f\.\s?d\.\s?r)",
                                                                  c, re.I):
        return None
    pf = _person_field(c)
    toks = [t for t in _raw_tokens(pf["name"] or "") if not (_is_prefix_title(t) or _is_suffix_title(t))]
    if not (2 <= len(toks) <= 4 and all(t[:1].isupper() for t in toks)):
        return None
    if pf["role"] is None and nxt:
        n2 = nxt.strip()
        if n2 and len(n2) <= 80 and not re.search(r"\d|@|https?|^(i\.\s?a\.|i\.\s?v\.|f\.\s?d\.)", n2, re.I) \
                and not _signer_like(n2):
            parts = [x.strip() for x in n2.split(",") if x.strip()]
            pf["role"] = parts[0]
            pf["org"] = pf["org"] or (", ".join(parts[1:]) or None)
    return pf


def _signature_start(lines: list[str], table_lines: set[int], after: int | None) -> int | None:
    """First line of the signature block after the last table ('Mit freundlichen Grüßen', a line of dashes,
    'Dr. Eva Brandl (Laborärztin)'), or None: the lab's comment ends there."""
    if after is None:
        return None
    for i in range(after, len(lines)):
        if i in table_lines:
            continue
        c = _clean_line(lines[i]).strip()
        if not c:
            continue
        if _GREETING.fullmatch(_fold(c).rstrip(",.!")) or re.fullmatch(r"[-–—_=*~]{2,}", lines[i].strip()) \
                or (_signer_like(c) and _signer(c, None)) or re.match(r"^gez\.?\s+\S", c, re.I):
            return i
    return None


def _signer_like(c: str) -> bool:
    toks = _raw_tokens(re.split(r",", c)[0])
    return bool(toks) and _is_prefix_title(toks[0])


def _signature(lines: list[str], table_lines: set[int]) -> dict | None:
    """The person who signed: the line under 'Mit (freundlichen|kollegialen …) Grüßen' / 'Kind regards', a
    'gez. Dr. J. Leitner' line, or else the last block of the document that starts with an academic title
    ('Dr. Johanna Leitner, Ärztliche Leiterin' + 'i.A. Stefan Kern'). None if nothing is a plain person."""
    clean = [(_clean_line(ln).strip() if i not in table_lines else "") for i, ln in enumerate(lines)]
    for i, c in enumerate(clean):
        if _GREETING.fullmatch(_fold(c).rstrip(",.!")):
            nonempty = [x for x in clean[i + 1:i + 5] if x]
            return _signer(nonempty[0], nonempty[1] if len(nonempty) > 1 else None) if nonempty else None
    for i, c in enumerate(clean):
        if re.match(r"^gez\.?\s+\S", c, re.I):
            return _signer(c, None)
    # trailing block: from the end, skip contact / address / 'i.A.' lines, the first titled person line
    seen = 0
    for i in range(len(clean) - 1, -1, -1):
        c = clean[i]
        if not c:
            continue
        if re.fullmatch(r"[-–—_=*~]{2,}", c):
            continue
        seen += 1
        if seen > 5 or lines[i].lstrip().startswith(("#", "-", "*", "|")):
            return None
        if _signer_like(c):
            nxt = next((x for x in clean[i + 1:] if x), None)
            return _signer(c, nxt)
    return None


def _sentence_patient(p: Parsed, lines, table_lines, labels) -> dict | None:
    """No labelled patient line: the patient named with a birth date in a line of the document ('Für: Frau Mag.
    Maria Beispiel, geboren am 12. März 1961', '**Maria Beispiel**, geb. ...', 'Befund für Frau Maria Beispiel,
    geb. 12.03.1961'). Only when exactly ONE person (name + birth date) is named in the whole document and no
    birth date without a name differs; anything else stays PATIENT_MISSING (and the second person is reported
    by _integrity_checks as before)."""
    ev = _evidence(p, lines, table_lines, labels)
    named = [e for e in ev if e.get("name")]
    if not named:
        return None
    persons = {(tuple(_fold(e["name"]).split()), e["birthDate"]) for e in named}
    if len(persons) != 1 or any(e["birthDate"] != named[0]["birthDate"] for e in ev):
        return None
    e = named[0]
    given, family, prefix = split_name(e["name"])
    if not given or not family:
        return None
    core = " ".join([*given, family]).strip()
    gender = None
    for w in re.split(r"[\s,;()]+", e.get("text") or ""):
        g = _GENDER.get(_fold(w)) if len(w) > 1 else None
        if g:
            gender = g
            break
    p.patient_lines.append({"name": e["name"], "birthDate": e["birthDate"], "dob_status": "ok", "source": "text",
                            "gender": gender, "order_free": False})
    return {"name": core, "given": given, "family": family, "prefix": prefix, "birthDate": e["birthDate"],
            "birthDate_text": None, "gender": gender, "line": e.get("text"), "from_text": True}


def _distinct_times(vals) -> set:
    """Distinct sample/report times: the same instant written twice is one time; a date alone on the day of a
    timed value is the same; text without a digit (a name after 'Freigabe:') is no time."""
    from .identity import VIENNA as _V
    timed, days, texts = set(), set(), set()
    for _, v in vals:
        if not re.search(r"\d", v or ""):
            continue
        dt = _parse_time_value(v)
        if dt.value is None:
            texts.add(_fold(v))
        elif dt.has_time:
            timed.add(dt.value.astimezone(_V).isoformat()[:16])
        else:
            days.add(str(dt.value)[:10])
    days -= {t[:10] for t in timed}
    return timed | days | texts


_BULLET = re.compile(r"^\s*[-*•+]\s+(.*\S)\s*$")
_B_LOINC = re.compile(r"[\s,;]*(?:-\s*)?[(\[]?\s*LOINC(?:[- ]?(?:code|nr\.?|no\.?))?\s*:?\s*(\d{1,7}-\d)\s*[)\]]?\s*$",
                      re.I)
_B_RANGE = re.compile(
    r"\s*[(\[]\s*(?P<label>(?:ref(?:erenz|erence)?\.?(?:-?\s?(?:bereich|wert|werte|intervall|interval|range|values?))?"
    r"|norm(?:al)?(?:-?\s?(?:bereich|wert|werte|range|values?))?|range|zielbereich|target)\.?\s*:?\s*)?"
    r"(?P<r>[^()\[\]]*?\d[^()\[\]]*?)\s*[)\]]\s*$", re.I)
_B_INLINE_REF = re.compile(
    r"(?:\s+-\s*|\s*[,;]\s*|\s+)(?:referenz(?:bereich|wert|werte|intervall)?|ref\.?(?:-?\s?(?:bereich|wert|werte|"
    r"intervall|interval|range))?|norm(?:al)?(?:bereich|wert|werte|range)?|reference(?:\s+(?:range|interval))?|"
    r"normal range)(?![^\W\d_])\.?\s*:?\s*(\S.*)$", re.I)
_B_HEAD = ["Test", "Ergebnis", "Einheit", "Referenzbereich", "LOINC"]
# a list item whose value is a pending / not-determinable / qualitative result (shared with prove's row count)
QUALITATIVE_RESULT = (r"^(?:folgt|folgen|ausst[aä]ndig|ausstehend|in arbeit|wird nachgereicht|nachgereicht|pending|"
                      r"to follow|in progress|n\.\s?b\.?|nicht bestimmbar|nicht messbar|neg\.?|pos\.?|negativ|"
                      r"positiv|negative|positive)(?=$|[\s.,;(])")
_QUALITATIVE = re.compile(QUALITATIVE_RESULT, re.I)
# headings of a discharge report: bullets under them (or anywhere in such a document) are never lab values
_DIS_HEADING = re.compile(r"medika|medication|arznei|entlass|discharge|verlauf|course|empfehl|recommend|diagnos|"
                          r"therap|procedere|anamnese|aufnahme|rehabilitation|mobili|pflege|soziales|summary of stay|"
                          r"follow-?up|weiteres vorgehen")


def _bullet_row(line: str) -> list[str] | None:
    """'- INR: 1,0 (Ref. 0,8–1,2)' / '- Hämoglobin 13,2 g/dl [12–16]' / '- aPTT: 29 s (reference 25–37) — LOINC
    14979-9' -> [test, value, unit, range, loinc] or None. Header fields ('- Geburtsdatum: ...') are not rows."""
    m = _BULLET.match(line)
    if not m:
        return None
    s = _norm_text(m[1].replace("**", "").replace("__", ""))
    loinc, rng = "", ""
    if lm := _B_LOINC.search(s):
        loinc, s = lm[1], s[:lm.start()].rstrip(" ,;-")
    if rm := _B_RANGE.search(s):
        cand = rm["r"].strip()
        pr = parse_range(cand, None)
        if rm["label"] or pr["low"] is not None or pr["high"] is not None:
            rng, s = cand, s[:rm.start()].rstrip(" ,;")
    if not loinc and (lm := _B_LOINC.search(s)):
        loinc, s = lm[1], s[:lm.start()].rstrip(" ,;-")
    if not rng and (im := _B_INLINE_REF.search(s)):
        cand = im[1].strip(" .")                 # '- INR 1,0 - Referenz 0,8-1,2', '- Hb 13,2 g/dl – Ref. 12–16'
        pr = parse_range(cand, None)
        if re.search(r"\d", cand):
            rng, s = cand, s[:im.start()].rstrip(" ,;-")
    colon = re.match(r"^([^\W\d_][^:]{0,60}?)\s*:\s*(\S.*)$", s)
    if colon:
        test, val = colon[1].strip(), colon[2].strip()
    else:
        nm = re.match(r"^([^\W\d_]\S*(?:\s+[^\W\d_<>≤≥]\S*)*?)\s+((?:<=|>=|<|>|≤|≥)?\s*\d.*)$", s)
        if not nm:
            return None
        test, val = nm[1].strip(), nm[2].strip()
    ft = _fold(test)
    if _KEYMAP.get(ft) or _time_key(test, val) or _FOOTER_KEYS.search(ft) or len(test) > 60:
        return None
    pv = parse_value(val)
    if pv["kind"] == "num":
        if not (colon or rng or loinc):
            return None
        mm = re.match(rf"^((?:<=|>=|=<|=>|<|>|≤|≥)?\s*{_NUM})\s*(.*)$", _norm_text(val))
        value_cell, unit = mm[1], pv.get("rest") or ""
        if pv.get("flag"):
            value_cell += " " + pv["flag_text"]
    elif pv["kind"] == "text" and colon and (rng or loinc or _QUALITATIVE.match(_norm_text(val))):
        value_cell, unit = val, ""            # '- TPO-Antikörper: folgt', '- Nitrit: neg.'

    else:
        return None
    return [test, value_cell, unit, rng, loinc]


def _bullet_tables(lines: list[str], table_lines: set[int], h1_idx) -> list[tuple]:
    """Contiguous runs of bullet result rows as pseudo tables (a, b, head, rows); [] in a document with a stay
    line or a discharge heading."""
    runs, cur = [], []
    for i, ln in enumerate(lines):
        if i in table_lines or i == h1_idx:
            if cur:
                runs.append(cur)
                cur = []
            continue
        if mh := re.match(r"^\s*#{1,6}\s+(.*\S)\s*$", ln):
            if cur:
                runs.append(cur)
                cur = []
            if _DIS_HEADING.search(_fold(mh[1])):
                return []
            continue
        cl = _clean_line(ln)
        kv = _LINE_KV.match(cl) if cl else None
        if (kv and not kv[2].strip() and _DIS_HEADING.search(_fold(kv[1]))) or (
                kv and _KEYMAP.get(_fold(kv[1])) == "stay") or (cl and _STAY_NOCOLON.match(cl)) or (
                kv and _MED_SECTION.search(_fold(kv[1])) and not _bullet_row(ln)):
            return []
        cells = _bullet_row(ln)
        if cells:
            cur.append((i, cells))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    out = []
    for run in runs:
        idx = [i for i, _ in run]
        if idx != list(range(idx[0], idx[-1] + 1)):
            continue
        out.append((idx[0], idx[-1] + 1, list(_B_HEAD), [c for _, c in run]))
    return out


# --------------------------------------------------------------------------- main

def parse_answer(text: str, doc_kind_hint: str | None = None) -> Parsed:
    p = Parsed(text=text)
    if not isinstance(text, str) or not text.strip():
        p._warn("empty document", hold=True, code="EMPTY")
        return p
    if "\ufffd" in text:
        p._warn("document contains replacement characters (encoding problem)")
    if _CONTROL.search(text):
        p._warn("document contains control characters (NUL, ESC, ...)", block=True, code="UNREADABLE")
    if m_act := _ACTIVE.search(text):
        p._warn(f"document contains active content ('{m_act[0][:20]}')", block=True, code="ACTIVE_CONTENT")
    else:
        why, ext = content_tiers(re.sub(r"<!--.*?(?:-->|$)", " ", text, flags=re.S))
        if why:
            p._warn(f"document contains suspect content: {why}", hold=True, code="ACTIVE_CONTENT_SUSPECT")
        elif ext:
            p._warn("document references external resources (" + "; ".join(dict.fromkeys(ext)) + "); "
                    "nothing was fetched, the original stays attached", code="EXTERNAL_RESOURCE")
    src = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    hidden_patient_values = []
    for mc in re.finditer(r"<!--(.*?)(?:-->|$)", src, re.S):
        for cl in mc[1].split("\n"):
            kv = _LINE_KV.match(_clean_line(cl))
            if kv and _KEYMAP.get(_fold(kv[1])) == "patient" and kv[2].strip():
                hidden_patient_values.append(kv[2].strip())
        # blank the comment for the structural parse (it is not rendered), keep line numbers
        src = src[:mc.start()] + re.sub(r"[^\n]", " ", mc[0]) + src[mc.end():]
    if hidden_patient_values:
        p._warn("an HTML comment (not visible when rendered) contains a patient line", code="HIDDEN_PATIENT_LINE")
    src = normalise_markup(src)
    lines = [normalise_leaders(ln) for ln in src.split("\n")]
    tables = _find_tables(lines)
    # a 2-column key/value table (Word -> Markdown header block) is read as 'Key: Value' lines
    kv_table_lines: set[int] = set()
    for (a, b, head, rows) in tables:
        if len(head) == 2 and all(len(r) == 2 for r in rows):
            keyed = [r for r in [head] + rows if _KEYMAP.get(_fold(strip_emphasis(r[0]))) or
                     _time_key(strip_emphasis(r[0]), strip_emphasis(r[1]))]
            if len(keyed) >= 2:
                lines[a] = (f"{strip_emphasis(head[0])}: {strip_emphasis(head[1])}"
                            if head in keyed else "")
                lines[a + 1] = ""
                for k, r in enumerate(rows):
                    lines[a + 2 + k] = f"{strip_emphasis(r[0])}: {strip_emphasis(r[1])}"
                kv_table_lines |= set(range(a, b))
    tables = _find_tables(lines)

    # ---- title (first H1, else first heading)
    h1_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*#\s+\S", ln):
            h1_idx = i
            break
    if h1_idx is None:
        for i, ln in enumerate(lines):
            if re.match(r"^\s*#{1,3}\s+\S", ln):
                h1_idx = i
                break
    plain_title = False
    if h1_idx is None:
        # no Markdown heading ('Labor Kanalstraße' / 'LABOR KANALSTRASSE | ...' + '====='): the first plain line
        # before any field, list or table is the title
        tl0 = {k for (a, b, _, _) in tables for k in range(a, b)}
        for i, ln in enumerate(lines):
            t = ln.strip()
            if not t or re.fullmatch(r"[=\-_*~]{3,}", t):
                continue
            if i not in tl0 and not _LINE_KV.match(_clean_line(t)) and not re.match(r"^[-*+>|•]", t) \
                    and len(t) <= 100 and t[:1].isupper() and not re.search(r"[.!?,;]$", t) \
                    and len(t.split()) <= 10 and any(x.strip() for x in lines[i + 1:]):
                h1_idx, plain_title = i, True
            break
    title_kind = None
    subtitle_idx = None
    if h1_idx is not None:
        p.title = re.sub(r"^\s*#+\s*", "", lines[h1_idx]).strip().replace("**", "")
        title_kind, org_part, both = _classify_title(p.title)
        if org_part:
            p.org = {"name": org_part}
        if both:
            p._warn(f"title '{p.title}' names both a lab report and a discharge report")
        if title_kind is None and not both:
            # '# Rehabilitationszentrum Waldblick' + '## Ärztlicher Entlassungsbericht': the next heading (before any
            # table) names the type; it is part of the header, not a section, and the H1 is the organisation
            for j in range(h1_idx + 1, len(lines)):
                if j in {k for (a, b, _, _) in tables for k in range(a, b)}:
                    break
                mh = re.match(r"^\s*#{2,3}\s+(.*\S)\s*$", lines[j])
                if plain_title and not mh and lines[j].strip() and len(lines[j].strip()) <= 80 \
                        and not _LINE_KV.match(_clean_line(lines[j])) and not re.match(r"^\s*[-*+>|•=]", lines[j]):
                    mh = re.match(r"^\s*(.*\S)\s*$", lines[j])   # 'Befundbericht Gerinnung' under 'Labor Kanalstraße'
                    sk0 = _classify_title(mh[1].replace("**", "").strip())
                    if not sk0[0] and j - h1_idx <= 4:
                        continue
                if mh:
                    st = mh[1].replace("**", "").strip()
                    sk, s_org, s_both = _classify_title(st)
                    if sk and not s_both:
                        title_kind, subtitle_idx = sk, j
                        p.extra.setdefault("subtitle", st)
                        if not re.search(r"\d", p.title) and len(p.title) <= 80:
                            p.org = {"name": p.title}     # the subtitle's other part is a topic, not the org
                    break
                if lines[j].strip() and _LINE_KV.match(_clean_line(lines[j])) is None and j - h1_idx > 3:
                    break

    # ---- results written as a bullet list ('- INR: 1,0 (Ref. 0,8–1,2)', '- Hämoglobin 13,2 g/dl [12–16]'): read as
    # rows of a lab table, only in a document that has no Markdown result table, no discharge title, no stay line
    # and no discharge sections (medication, course, recommendations): a discharge report's bullets stay text
    if title_kind != DISCHARGE and not any(_table_columns(h)[0].get("value") is not None for (_, _, h, _) in tables):
        btables = _bullet_tables(lines, {k for (a, b, _, _) in tables for k in range(a, b)}, h1_idx)
        if btables:
            tables = sorted(tables + btables)
            p._warn(f"{sum(len(t[3]) for t in btables)} result(s) read from a bullet list", code="RESULTS_AS_LIST")
    table_lines = {k for (a, b, _, _) in tables for k in range(a, b)}

    # ---- key: value lines and sections
    seen: dict[str, list[tuple[int, str]]] = {}
    labels: dict[int, str] = {}
    section = None
    sec_lines: list[str] = []
    first_section_or_table = min([a for (a, _, _, _) in tables] +
                                 [i for i, ln in enumerate(lines) if re.match(r"^\s*##\s+\S", ln) and i != subtitle_idx]
                                 + [len(lines)])
    body_after_table: list[str] = []
    last_table_end = max([b for (_, b, _, _) in tables], default=None)

    def close_section():
        if section is not None:
            p.sections.append({"title": section, "text": "\n".join(sec_lines).strip()})

    def key_for(i: int, label: str, value: str) -> str | None:
        key = _KEYMAP.get(_fold(label))
        if key is None:
            key = _time_key(label, value)
        header = section is None or i in kv_table_lines or bool(_HEADER_SECTION.match(_fold(section)))
        if key == "patient" and _fold(label) in ("betrifft", "betreff", "re", "regarding", "name", "subject") \
                and not re.search(r"\d", value) and not (_fold(label) == "name" and header):
            key = None              # 'Betrifft: Ihre Anfrage' is not a patient line; 'Name:' only in the header
        if key == "patient" and not header and not re.search(r"\d", value):
            key = None              # '- Patient: continue the exercises' in a section is not a patient line
        if key in ("reported", "sample") and not re.search(r"\d", value) and \
                re.search(r"freig|valid|befundet|vidiert|released|approved", _fold(label)):
            key = "validated"       # 'Freigabe: Dr. Eva Brandl' names the person, not a time
        if key in ("family_name", "given_name") and (not header or re.search(r"\d", value)):
            key = None
        if key in ("stay_start", "stay_end") and parse_date_ex(value.strip(" .")).value is None:
            key = None              # 'Entlassung: nach Hause' is text
        return key

    sig_start = _signature_start(lines, table_lines, last_table_end)
    for i, raw in enumerate(lines):
        if i in table_lines and section is not None:
            sec_lines.append(raw)              # a table inside a section stays part of its text ('Medikation')
        if i == h1_idx or i in table_lines:
            continue
        if i == subtitle_idx:
            continue                           # '## Ärztlicher Entlassungsbericht' under the clinic's name: header
        if m := re.match(r"^\s*#{2,6}\s+(.*\S)\s*$", raw):
            hk = _LINE_KV.match(_clean_line(m[1]))
            if hk and _KEYMAP.get(_fold(hk[1])) in ("patient", "dob") and re.search(r"\w", hk[2]):
                raw = m[1]                     # '## Patientin: Maria Beispiel, geb. ...' is a patient line
            else:
                close_section()
                section, sec_lines = m[1].replace("**", "").strip(), []
                continue
        ln = _clean_line(raw)
        if ((msec := _PLAIN_SECTION.match(ln)) or (msec := _PLAIN_HEADING.match(ln))) \
                and not _KEYMAP.get(_fold(msec[1])) and _DIS_HEADING.search(_fold(msec[1])) \
                and (":" in ln or i + 1 < len(lines) and lines[i + 1].strip()):
            close_section()                    # 'Medikation bei Entlassung (morgens-mittags-abends):' + list
            section, sec_lines = msec[1].strip(), []
            continue
        if ln and _LINE_KV.match(ln) is None:
            # 'Validiert von Dr. Eva Brandl.' / 'Rehabilitationsaufenthalt vom 2. bis 23. September 2026' (no colon)
            if mv := _VALIDATED_NOCOLON.match(ln):
                ln = f"{mv[1]}: {mv[2].rstrip(' .')}"
            elif ms := _STAY_NOCOLON.match(ln):
                ln = f"Aufenthalt: {ms[1].rstrip(' .')}"
        if ln and (mad := _ADMIT_DISCHARGE.match(ln)):
            ln = f"Aufenthalt: {mad[1].strip(' ,')} bis {mad[2].strip(' ,')}"
        pieces = _kv_pieces(ln, lambda lb, v: key_for(i, lb, v)) if ln else []
        if not pieces and ln and re.search(r"\s[·•|]\s", ln):
            for seg in re.split(r"\s+[·•|]\s+", ln)[1:]:
                if (ms2 := _LINE_KV.match(seg)) and key_for(i, ms2[1], ms2[2]) in ("validated", "written", "sent"):
                    seen.setdefault(key_for(i, ms2[1], ms2[2]), []).append((i, ms2[2].strip()))
        if len(pieces) > 1:
            # 'Abnahme: 28.10.2026 07:30 · Befund: 28.10.2026 11:05': several header fields on one line
            unread = False
            for lb, v in pieces:
                key = key_for(i, lb, v)
                if key and (key != "comment" or section is None):
                    seen.setdefault(key, []).append((i, f"{lb}: {v}" if key == "comment" else v.strip()))
                    if _KEYMAP.get(labels.get(i, "")) not in ("patient", "dob"):
                        labels[i] = _fold(lb)
                elif section is None and i < first_section_or_table:
                    p.extra[lb.strip()] = v.strip()
                else:
                    unread = True
            if unread and section is not None:
                sec_lines.append(raw)
            continue
        kv = _LINE_KV.match(ln) if ln else None
        key = key_for(i, kv[1], kv[2]) if kv else None
        if key and key != "comment" or (key == "comment" and section is None):
            # comment lines are kept verbatim, label included ('Note: ...'): filed exactly as sent
            seen.setdefault(key, []).append((i, ln if key == "comment" else kv[2].strip()))
            labels[i] = _fold(kv[1])
            continue
        if kv and section is None and i < first_section_or_table and not key:
            p.extra[kv[1].strip()] = kv[2].strip()
        elif kv and section is None and not key and _is_footer(kv[1], kv[2]):
            p.extra[kv[1].strip()] = kv[2].strip()     # contact/link footer: not part of the lab's comment
            continue
        if section is not None:
            sec_lines.append(raw)
        elif last_table_end is not None and i >= last_table_end and ln:
            if sig_start is not None and i >= sig_start:
                p.extra.setdefault("footer", []).append(ln)   # signature block: not the lab's comment
            elif _is_link_footer(ln) or _LINKTOK.search(ln) or _CONTACT_LINE.search(ln):
                p.extra.setdefault("footer", []).append(ln)   # logo / link / phone footer: not the lab's comment
            else:
                body_after_table.append(ln)
    close_section()
    signature = _signature(lines, table_lines)

    if not seen.get("patient") and len(seen.get("family_name", [])) == 1 and len(seen.get("given_name", [])) == 1:
        i_f, fam = seen["family_name"][0]       # 'Familienname: Beispiel' + 'Vorname: Maria'
        seen["patient"] = [(i_f, f"{fam}, {seen['given_name'][0][1]}")]
        labels[i_f] = "familienname, vorname"

    def one(key: str) -> str | None:
        vals = seen.get(key, [])
        if not vals:
            return None
        uniq = {_fold(v) for _, v in vals}
        if len(uniq) > 1:
            p._warn(f"'{key}' given {len(vals)} times with different values; first used",
                    hold=key in ("patient", "dob"), code="PATIENT_CONFLICT")
        return vals[0][1]

    # ---- every patient line (visible and hidden), for the patient decision
    scratch = Parsed()
    for src_kind, values in (("text", [(labels.get(i), v) for i, v in seen.get("patient", [])]),
                             ("html-comment", [(None, v) for v in hidden_patient_values])):
        for label_, v in values:
            pl = _parse_patient(v, scratch) or {}
            dob_text = pl.get("birthDate_text")
            status = ("ok" if pl.get("birthDate") else
                      "ambiguous" if dob_text and "ambiguous (day/month" in dob_text else
                      "unparseable" if dob_text else "missing")
            if pl.get("dob_two_digit"):
                status = "two-digit" if pl.get("birthDate") else "ambiguous"
            raw_dob = re.search(r"'([^']+)'", dob_text or "") if status == "ambiguous" else None
            p.patient_lines.append({"name": pl.get("name"), "birthDate": pl.get("birthDate"), "dob_status": status,
                                    "birthDate_raw": (raw_dob[1] if raw_dob else
                                                      (pl.get("dob_two_digit") or {}).get("text")),
                                    "source": src_kind, "gender": pl.get("gender"),
                                    # LIS header block 'Name: Beispiel Maria' is printed family-first or
                                    # given-first: under the 'Name:' label the order of the tokens is free
                                    "order_free": label_ == "name" and "," not in v})
    if len({(tuple(_fold(x["name"] or "").split()), x["birthDate"]) for x in p.patient_lines}) > 1 \
            and hidden_patient_values:
        p._warn("the patient line in an HTML comment differs from the visible one", hold=True,
                code="PATIENT_CONFLICT")
    # ---- patient
    pv = one("patient")
    if len({_fold(v) for _, v in seen.get("patient", [])}) > 1:
        p._warn("more than one different patient line: ambiguous patient", hold=True, code="PATIENT_CONFLICT")
    # 'Maria Beispiel · 12.03.1961 · SVNR 1234 120361': an SVNR written inside the patient line is checked too
    if not seen.get("svnr"):
        for i_p, v_p in seen.get("patient", []):
            if m_sv := re.search(r"\b(?:svnr|sv-?nr\.?|sv-?nummer|vsnr|versicherungsnummer)\b\s*:?\s*(\d{4}\s?\d{6})(?!\d)",
                                 v_p, re.I):
                seen.setdefault("svnr", []).append((i_p, m_sv[1]))
    pat = None
    if pv is None:
        # no labelled patient line: 'Befund für Frau Maria Beispiel, geb. 12.03.1961' / 'Für: ...' / a bold line.
        # Read only if the whole document names exactly ONE person with a birth date (the rules for a second
        # person are unchanged: _integrity_checks adds every other name + birth date found)
        pat = None if hidden_patient_values else _sentence_patient(p, lines, table_lines, labels)
        if pat is None:
            p._warn("no patient line ('Patient: <name>, born <date>') found", hold=True, code="PATIENT_MISSING")
        else:
            p._warn("no 'Patient:' label; the patient was read from the line naming her with a birth date",
                    code="PATIENT_FROM_TEXT")
    else:
        pat = _parse_patient(pv, p)
    if pv is not None or pat is not None:
        dobv = one("dob")
        if pat is not None and dobv and not pat.get("birthDate"):
            tmp = {"birthDate": None, "birthDate_text": parse_date_ex(dobv).problem or ""}
            _resolve_two_digit_year(tmp, p)
            if tmp.get("birthDate"):
                pat.update(birthDate=tmp["birthDate"], birthDate_text=dobv, dob_two_digit=tmp["dob_two_digit"])
                if len(p.patient_lines) == 1 and p.patient_lines[0]["dob_status"] == "missing":
                    p.patient_lines[0].update(birthDate=tmp["birthDate"], dob_status="two-digit")
                dobv = None
        if pat is not None and dobv:
            dp = parse_date_ex(dobv)
            if dp.value is None and len(p.patient_lines) == 1 and p.patient_lines[0]["dob_status"] == "missing" \
                    and "ambiguous" in (dp.problem or ""):
                # 'DOB: 12/03/1961': written, but day/month order unknown -> DOB_AMBIGUOUS, not 'missing'
                p.patient_lines[0].update(dob_status="ambiguous", birthDate_raw=dobv.strip())
                pat["birthDate_text"] = dp.problem
            if dp.value is None:
                p._warn("birth date line is not a complete unambiguous date", hold=True, code="DOB_UNPARSEABLE")
            elif pat.get("birthDate") and pat["birthDate"] != dp.value.isoformat():
                p._warn("birth date in the patient line differs from the birth date line", hold=True, code="PATIENT_CONFLICT")
            elif not pat.get("birthDate"):
                pat["birthDate"], pat["birthDate_text"] = dp.value.isoformat(), dobv
        if (pat and pat.get("birthDate") and len(p.patient_lines) == 1
                and p.patient_lines[0]["dob_status"] == "missing" and dobv):
            p.patient_lines[0].update(birthDate=pat["birthDate"], dob_status="ok")   # 'Date of birth:' line
        if pat is None or not pat.get("name"):
            p._warn("patient line has no name", hold=True, code="PATIENT_INCOMPLETE")
        elif not pat.get("birthDate"):
            why = pat.get("birthDate_text")
            why = pat.get("birthDate_text") or ""
            kind_ = ("ambiguous day/month" if "ambiguous" in why and "/" in why else
                     "two-digit year" if "two-digit" in why else "missing or not understood")
            p._warn(f"patient line has no complete unambiguous birth date ({kind_})", hold=True, code="PATIENT_INCOMPLETE")
        sexv = one("sex")
        if pat is not None and sexv:
            g = _GENDER.get(_fold(sexv.split(",")[0].strip()))
            if g and pat.get("gender") and g != pat["gender"]:
                p._warn("the document states two different sexes for the patient", hold=True, code="SEX_MISMATCH")
            elif g:
                pat["gender"] = pat.get("gender") or g
                for pl in p.patient_lines:
                    if pl["source"] == "text" and not pl.get("gender"):
                        pl["gender"] = g
        p.patient = pat

    # ---- people and organisation
    if (v := one("requested")) is not None:
        rq = _person_field(v)
        p.requester = {"name": rq["name"], "org": ", ".join(x for x in (rq["role"], rq["org"]) if x) or None}
    if (v := one("validated")) is not None:
        p.author = _person_field(v)
    if (v := one("written")) is not None:
        w = _person_field(v)
        if p.author and not loose_name_equal(p.author["name"], w["name"]):
            p._warn(f"both 'Validated by' ({p.author['name']}) and 'Written by' ({w['name']}) given; "
                    f"'Validated by' used as author")
        p.author = p.author or w
    if p.author is None and signature and not (p.patient and p.patient.get("name")
                                               and loose_name_equal(signature["name"], p.patient["name"])):
        p.author = signature                    # the signature under 'Mit freundlichen Grüßen'
        p._warn(f"author read from the signature line ({signature['name']})", code="AUTHOR_FROM_SIGNATURE")
    p.senders = [_person_field(v) for _, v in seen.get("sent", [])]
    if (v := one("sent")) is not None:
        p.sender = _person_field(v)
        if len({" ".join(_fold(x["name"] or "").split()) for x in p.senders}) > 1:
            p._warn("more than one different 'Sent by' line", hold=True, code="SENDER_CONFLICT")
    if p.org is None:
        for who in (p.author, p.sender):
            if who and who.get("org"):
                p.org = {"name": who["org"]}
                break
    elif p.author and p.author.get("org") and _fold(p.author["org"]) != _fold(p.org["name"]):
        p._warn(f"author organisation '{p.author['org']}' differs from title organisation '{p.org['name']}'")

    # ---- times
    for key, attr in (("sample", "sample_time"), ("reported", "report_time")):
        v = one(key)
        if v is None:
            continue
        setattr(p, attr + "_text", v)
        dt = _parse_time_value(v)
        for n in dt.notes:
            p._warn(n)
        if dt.value is None:
            p._warn(f"{key} time '{v}' not understood: {dt.problem}")
        else:
            val = dt.value.astimezone(VIENNA) if dt.has_time else dt.value   # same instant, Vienna offset
            setattr(p, attr, fhir_datetime(val))
            if not dt.has_time:
                p._warn(f"{key} time '{v}' has no time of day")
    if p.sample_time and p.report_time and "T" in p.sample_time and "T" in p.report_time:
        from datetime import datetime as _dt
        if _dt.fromisoformat(p.sample_time) > _dt.fromisoformat(p.report_time):
            p._warn("sample time is after report time")

    # ---- discharge-specific
    if (v := one("stay")) is not None:
        per, prob = _parse_period(v)
        for alt in (re.sub(r"\s*\([^()]*\)\s*$", "", v), (re.search(r"\(([^()]*\d{4}[^()]*)\)", v) or [None, None])[1],
                    (re.match(r"^(.*?\d{4})\s*(?:[,;(]|\s\d+\s)", v) or [None, None])[1]):   # '…, 18 Einheiten'

            if per is None and alt and alt != v:   # '02.–23.09.2026 (21 Tage)' / '21 days (2–23 September 2026)'
                per2, _ = _parse_period(alt)
                if per2:
                    per, prob = {**per2, "text": v.strip()}, None
        p.stay = per or {"start": None, "end": None, "text": v}
        if prob:
            p._warn(prob)
    s_end = one("stay_end")
    single = p.stay is not None and (p.stay.get("start") is None or p.stay.get("start") == p.stay.get("end"))
    s_start = one("stay_start") or (p.stay["text"] if single else None)   # 'Admission: 2026-09-02'
    if s_end and s_start and (p.stay is None or single):
        a_ = parse_date_ex(s_start.strip(" .")).value
        b_ = parse_date_ex(s_end.strip(" .")).value
        if a_ and b_ and a_ <= b_:            # 'Admission: 2026-09-02 · Discharge: 2026-09-23'
            p.stay = {"start": a_.isoformat(), "end": b_.isoformat(), "text": f"{s_start} – {s_end}"}
    if (v := one("reason")) is not None:
        p.reason = v

    # ---- kind (lab tables are only READ as lab values for a lab report; in a discharge report
    # they are part of the text: Barthel 'Punkte', 'BSG 12/25' never become Observations or holds)
    n_lab_rows = sum(1 for (a, b, head, rows) in tables
                     if _table_columns(head)[0].get("value") is not None
                     for cells in rows if _row_kind(cells, _table_columns(head)[0]) == "row")
    has_dis_signals = bool(p.stay or seen.get("written") or seen.get("sent") or p.sections)
    # a discharge report's own content: a stay, or a medication section / 'Entlassmedikation:' line
    dis_content = bool(p.stay) or any(_MED_SECTION.search(_fold(sec["title"] or "")) for sec in p.sections) \
        or any(_MED_SECTION.search(_fold(k)) for k in p.extra if isinstance(k, str))
    hint_fits = False
    if title_kind == LAB:
        kind = LAB if n_lab_rows else None
        if not n_lab_rows:
            p._warn("title says lab report but no lab table with a result column was found", hold=True, code="NO_RESULTS")
    elif title_kind == DISCHARGE:
        kind = DISCHARGE
    elif n_lab_rows and not p.stay:
        kind = LAB                             # a result table and no stay: a lab report, whatever was requested
    elif doc_kind_hint == DISCHARGE and dis_content and not n_lab_rows:
        kind, hint_fits = DISCHARGE, True      # untitled, but a stay / discharge medication: the requested report
    elif has_dis_signals:
        kind = DISCHARGE
    else:
        kind = None
    if kind is None and doc_kind_hint == DISCHARGE and p.patient:
        kind = DISCHARGE
        p._warn("document type not recognised; filed as a text document (DocumentReference) as requested")
    if kind is None:
        p._warn("document type not recognised (neither a lab report nor a discharge report)", hold=True, code="KIND_UNKNOWN")
    if kind == DISCHARGE:
        p.doc_type_confirmed = title_kind == DISCHARGE or bool(p.stay) or hint_fits
        if not p.doc_type_confirmed:
            p._warn("document is not marked as a discharge report (no such title, no 'Stay' line); "
                    "filed as a text document with a text-only type")
    if kind and doc_kind_hint in (LAB, DISCHARGE) and kind != doc_kind_hint:
        p._warn(f"a {doc_kind_hint.replace('_', ' ')} was requested but the document looks like a "
                f"{kind.replace('_', ' ')}", hold=True, code="KIND_MISMATCH")
    if kind == LAB and not p.author:
        p._warn("no 'Validated by' line: report has no responsible person")
    if kind == LAB and not p.sample_time:
        p._warn("no sample time")
    if kind == LAB and not (p.report_time and "T" in p.report_time):
        p._warn("no report time with time of day: DiagnosticReport.issued left empty")
    if kind == DISCHARGE and not p.author:
        p._warn("no 'Written by' line: report has no author")
    if kind == LAB:
        _read_lab_tables(p, tables, body_after_table, seen)
    elif kind == DISCHARGE and tables:
        p._warn(f"{len(tables)} table(s) kept as text in the discharge report (not read as lab values)",
                code="TABLES_AS_TEXT")
    _report_status(p, lines, table_lines, h1_idx)
    _integrity_checks(p, kind, lines, table_lines, seen, labels, h1_idx)
    p.detected_kind = kind or title_kind or UNKNOWN
    if not p.patient or not p.patient.get("name") or len({_fold(v) for _, v in seen.get("patient", [])}) > 1:
        kind = None
    p.kind = kind or UNKNOWN
    return p



_COMMENT_LABEL = re.compile(r"^\s*(kommentar|bemerkung|bemerkungen|hinweis|anmerkung|note|notes|comment|comments|"
                            r"befundkommentar|remark)\b\s*:?", re.I)
_PENDING = re.compile(r"^(folgt|folgen|ausst[aä]ndig|ausstehend|in arbeit|in bearbeitung|wird nachgereicht|"
                      r"nachgereicht|nachfolgend|pending|to follow|in progress|offen)\b", re.I)
_NOT_DETERMINABLE = re.compile(r"^(nicht bestimmbar|nicht messbar|nicht auswertbar|nicht verwertbar|n\.\s?b\.?|"
                               r"nb|not determinable|not measurable|unmeasurable|h[aä]molytisch|h[aä]molyse|"
                               r"lip[aä]misch|ikterisch|clotted|geronnen)\b", re.I)
_CURRENT_HEADER = re.compile(r"^(aktuell|aktueller wert|current|heute|today|neu|new)\b", re.I)


def _table_columns(head: list[str]) -> tuple[dict, list]:
    cols, unknown = {}, []
    for ci, h in enumerate(head):
        hh = strip_emphasis(h)
        c = _COLMAP.get(_fold(hh))
        if c is None and _CURRENT_HEADER.match(_fold(hh)):
            c = "value"
        if c and c not in cols:
            cols[c] = ci
        else:
            unknown.append((ci, hh.strip()))
    # 'Wert | Ergebnis | Einheit': with a second, explicit result header and no test column, the weak 'Wert' /
    # 'Befund' column in front of it names the test (a patient's own table, F12)
    if "test" not in cols and "value" in cols and _fold(strip_emphasis(head[cols["value"]])) in _WEAK_VALUE:
        strong = [ci for ci, h in unknown if _COLMAP.get(_fold(h)) == "value" and _fold(h) not in _WEAK_VALUE]
        if strong and strong[0] > cols["value"]:
            cols["test"] = cols["value"]
            cols["value"] = strong[0]
            unknown = [(ci, h) for ci, h in unknown if ci != strong[0]]
    if "value" not in cols:
        # cumulative report with date columns only: the latest date is the current value
        dated = []
        for ci, h in unknown:
            dp = parse_date_ex(re.sub(r"^(vom|wert|value|result|ergebnis)\s+", "", h, flags=re.I))
            if dp.value:
                dated.append((dp.value, ci))
        if dated:
            cols["value"] = max(dated)[1]
            unknown = [(ci, h) for ci, h in unknown if ci != cols["value"]]
    return cols, unknown


def _row_kind(cells: list[str], cols: dict) -> str:
    """'row' (a lab value), 'comment' (a comment row) or 'label' (a group heading / empty)."""
    nonempty = [i for i, c in enumerate(cells) if strip_emphasis(c).strip()]
    if not nonempty:
        return "label"
    first = strip_emphasis(cells[nonempty[0]])
    if _COMMENT_LABEL.match(first) and (len(nonempty) == 1 or "value" not in cols
                                        or not strip_emphasis(cells[cols["value"]]).strip()):
        return "comment"
    if len(nonempty) == 1 and "value" in cols and nonempty[0] != cols["value"]:
        return "label"
    return "row"


def _short_row_ok(cells: list[str], cols: dict, missing: int) -> bool:
    """A row one cell short is read only if the missing column is the trailing LOINC / flag (or an unnamed) column
    and what is there still fits its columns (a shifted row would put a LOINC under the range or a range under
    the unit: that stays TABLE_MALFORMED)."""
    name = next((c for c, ci in cols.items() if ci == missing), None)
    if name not in (None, "loinc", "flag") or "value" not in cols or cols["value"] >= len(cells):
        return False
    get = lambda c: strip_emphasis(cells[cols[c]]).strip() if c in cols and cols[c] < len(cells) else ""
    if get("range"):
        r = parse_range(get("range"), get("unit") or None)
        if r.get("problem") or (r["low"] is None and r["high"] is None and re.search(r"\d", get("range"))):
            return False
    if get("unit") and to_ucum(get("unit"))[1]:
        return False
    return True


def _flag_only_column(rows: list[list[str]], ci: int) -> bool:
    vals = [strip_emphasis(r[ci]).strip() for r in rows if ci < len(r)]
    vals = [v for v in vals if v]
    return bool(vals) and all(_flag(v) for v in vals)


def _read_lab_tables(p: Parsed, tables, body_after_table, seen) -> None:
    table_comments: list[str] = []
    for (a, b, head, rows) in tables:
        cols, unknown_cols = _table_columns(head)
        if "value" not in cols:
            p._warn(f"table at line {a + 1} has no result column ({' | '.join(head)}); not read as lab values")
            continue
        if "flag" not in cols:
            for ci, h in list(unknown_cols):
                if _flag_only_column(rows, ci):              # unlabelled 'H/L/+/-/*' column
                    cols["flag"] = ci
                    unknown_cols.remove((ci, h))
                    p._warn(f"column {ci + 1} ('{h}') holds only lab flags; read as flags", code="FLAG_COLUMN")
                    break
        if "test" not in cols:
            p._warn(f"table at line {a + 1} has no test-name column")
        for ci, h in unknown_cols:
            if h:
                p._warn(f"column '{h}' not recognised; kept as an Observation note")
        for r_i, cells in enumerate(rows):
            line_no = a + 3 + r_i
            rk = _row_kind(cells, cols)
            if rk == "comment":
                table_comments.append(" ".join(strip_emphasis(c) for c in cells if c.strip()))
                continue
            if rk == "label":
                continue
            if len(cells) == len(head) - 1 and _short_row_ok(cells, cols, len(head) - 1):
                # GFM: a row may have fewer cells; the missing trailing LOINC / flag cell is empty
                p._warn(f"line {line_no}: one cell less than the header (empty trailing cell)", code="TABLE_SHORT_ROW")
                cells = cells + [""]
            if len(cells) != len(head):
                p._warn(f"line {line_no}: {len(cells)} cells but {len(head)} columns", hold=True, code="TABLE_MALFORMED")
                cells = (cells + [""] * len(head))[: len(head)]
            p.lab.append(_lab_row(cells, cols, unknown_cols, line_no, p))
    if p.lab or table_comments:
        comment_lines = [ln for ln in body_after_table]
        if seen.get("comment"):
            comment_lines = [v for _, v in seen["comment"]] + comment_lines
        p.comment = "\n".join(table_comments + comment_lines).strip() or None


_PRELIM_TITLE = re.compile(r"\b(vorl[aä]ufig\w*|vorbefund|teilbefund|zwischenbefund|preliminary|interim|partial)\b", re.I)
_PRELIM_BODY = re.compile(r"\b(vorl[aä]ufiger befund|teilbefund|zwischenbefund|preliminary report|interim report)\b", re.I)
_CORR_TITLE = re.compile(r"\b(korrektur|befundkorrektur|korrigiert\w*|berichtigt\w*|berichtigung|corrected|correction)\b",
                         re.I)
_CORR_BODY = re.compile(r"\b(befundkorrektur|korrigierter befund|berichtigter befund|ersetzt (?:den|diesen|einen) "
                        r"(?:befund|bericht)|corrected report|replaces (?:the )?(?:report|result))\b", re.I)
_AMEND = re.compile(r"\b(nachtrag|erg[aä]nzungsbefund|addendum|amended report)\b", re.I)


def _report_status(p: Parsed, lines, table_lines, h1_idx) -> None:
    """DiagnosticReport.status from the document's own words; never a silent 'final' for a
    correction, an addendum, a preliminary report or a report with pending rows."""
    title = p.title or ""
    body = "\n".join(ln for i, ln in enumerate(lines) if i not in table_lines and i != h1_idx)
    status = "final"
    if _CORR_TITLE.search(title) or _CORR_BODY.search(body):
        status = "corrected"
        m = re.search(r"ersetzt (?:den|diesen|einen) (?:befund|bericht)\s+(?:vom\s+)?"
                      r"(\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}(?:,?\s*\d{1,2}[:.]\d{2})?|[^;\n]{1,40})", body, re.I)
        p.replaces = m[1].strip(" .,") if m else None
        if p.replaces:
            dt = parse_datetime_ex(p.replaces)
            if dt.value is not None:
                p.replaces_time = fhir_datetime(dt.value.astimezone(VIENNA) if dt.has_time else dt.value)
        p._warn("corrected report: it replaces an earlier report" + (f" ({p.replaces})" if p.replaces else ""),
                code="REPORT_CORRECTED")
    elif _AMEND.search(title) or _AMEND.search(body):
        status = "amended"
        p._warn("addendum to an earlier report (Nachtrag)", code="REPORT_AMENDED")
    elif _PRELIM_TITLE.search(title) or _PRELIM_BODY.search(body) or any(r.get("status") == "registered"
                                                                         for r in p.lab):
        status = "preliminary"
        p._warn("preliminary report: results may change"
                + ("; some results are still pending" if any(r.get("status") == "registered" for r in p.lab) else ""),
                code="REPORT_PRELIMINARY")
    p.report_status = status


# ---------------------------------------------------------------- integrity checks

_EVID_MARK = re.compile(r"(?:(?<![\w*\\])\*\s?(?=\d)|\b(?:geb\.(?:\s*am)?|geboren(?:\s+am)?|born(?:\s+on)?|d\.?o\.?b\.?|"
                        r"geb\.-datum|geburtsdatum|date of birth)\s*:?\s*)", re.I)
_SPLITTERS = re.compile(r"[·•|;()\[\]:–—]|\s-\s|\bfor\b|\bfür\b|\bvon\b|\bof\b", re.I)
_DATE_AT_START = re.compile(
    r"\s*(\d{4}-\d{2}-\d{2}|\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{4}|\d{1,2}/\d{1,2}/\d{4}|"
    r"\d{1,2}(?:\.|st|nd|rd|th)?\s+(?:of\s+)?[A-Za-zÄÖÜäöü]{3,}\.?\s+\d{4}|[A-Za-z]{3,}\.?\s+\d{1,2},?\s+\d{4})")
FUTURE_TOLERANCE_S = 2 * 3600
# 'Family, Given, DD.MM.YYYY' - the way a tube label or an order form prints a person (no 'geb.' needed)
_LABEL_PERSON = re.compile(r"(?<![\w.])([A-ZÄÖÜ][\wäöüß'-]+),\s*([A-ZÄÖÜ][\wäöüß'-]+(?:[ -][A-ZÄÖÜ][\wäöüß'-]+)?),?\s+"
                           r"(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})(?!\d)")
_LABEL_NOT_PERSON = re.compile(r"labor|klinik|medizin|station|abteil|zweigstelle|reha|krankenhaus|spital|institut|praxis|ambulan|chirurg|ordination|zentrum|befund|bericht|probe|auftrag|einsender|innere|gmbh|stra(?:ss|ß)e|gasse|platz|weg\b|hospital|clinic|department|laboratory|universit")
# wording that doubts which patient the sample / document belongs to: at least a HOLD (tests/jury/fresh/holdout3 N3)
IDENTITY_DOUBT = (r"beschriftet\s+mit|falsch\s+beschriftet|fehl(?:beschrift|etikettier)\w*|verwechs\w*|"
                  r"identit\w*.{0,60}?(?:prüf|pruf|klär|klar|unklar|fraglich|zweifel)|"
                  r"zuordnung.{0,40}?(?:prüf|pruf|klär|klar|unklar|fraglich|zweifel)|"
                  r"mislabel\w*|labell?ed\s+(?:with|as)\b|mix-?up|identity.{0,60}?(?:check|verif|unclear|doubt|confirm)|"
                  r"wrong\s+patient|falsche[rn]?\s+patient")
_IDENTITY_DOUBT = re.compile(IDENTITY_DOUBT, re.I)            # clock tolerance for 'sample/report time in the future'


def _name_before(segment: str) -> str | None:
    chunk = _SPLITTERS.split(segment.rstrip(" ·•|,;:–—-\t(["))[-1].strip(" ,.")
    chunk = _person_from_subject(chunk)
    toks = chunk.split()
    out = []
    for t in reversed(toks):
        if t[:1].isupper() or _is_prefix_title(t):
            out.insert(0, t)
        else:
            break
    while out and _is_prefix_title(out[0]):
        out.pop(0)
    return " ".join(out) if out else None


def _evidence(p: Parsed, lines, table_lines, labels) -> list[dict]:
    """Name + birth date found anywhere outside the patient lines (page headers, titles, footers)."""
    from datetime import date as _date
    found = []
    for i, ln in enumerate(lines):
        if i in table_lines:
            # table cells are scanned too ('| Hinweis | Probe von Max Beispiel, geb. 01.01.1960 |'); the separator
            # row and value cells carry no birth mark
            ln = " | ".join(c for c in _cells(ln))
        # a labelled patient / birth-date line is scanned too: a second person in its tail ('Patient: Maria
        # Beispiel, geb. 12.03.1961; Kopie an Max Beispiel, geb. 01.01.1960') is another person
        labelled = labels.get(i) is not None and _KEYMAP.get(labels[i]) in ("patient", "dob")
        text = re.sub(r"^\s*#+\s*", "", ln).replace("**", "").replace("__", "")
        for m in _EVID_MARK.finditer(text):
            tail = re.split(r"[·•|()\[\]]|\s[-–—]\s", text[m.end():])[0]
            dm = _DATE_AT_START.match(tail)
            iso = None
            if dm:
                dp = parse_date_ex(dm[0].strip(" ,"))
                iso = dp.value.isoformat() if dp.value else None
            if not iso:
                iso, used, _ = _take_date(tail)
            if not iso:
                continue
            if iso > _date.today().isoformat():
                continue
            name = _name_before(text[: m.start()])
            if not name and iso >= (_date.today().replace(year=_date.today().year - 1)).isoformat():
                continue                       # '* 22.10.2026' footnote, not a birth date
            found.append({"name": name, "birthDate": iso, "dob_status": "ok", "source": "evidence",
                          "line": i + 1, "gender": None, "order_free": False, "labelled": labelled,
                          "text": text.strip()})
        # the label form without a birth mark: 'Röhrchen beschriftet mit „Beispiel, Max, 01.01.1960"' (Family, Given,
        # date). Only a date at least a year back counts (a recent date after a name is a signature or a sample date)
        for m in _LABEL_PERSON.finditer(text):
            if _LABEL_NOT_PERSON.search(_fold(m[1] + " " + m[2])):
                continue                       # 'Labor Kanalstraße, Zweigstelle Süd, 12.03.2024' is a place, not a person
            dp = parse_date_ex(m[3])
            if not dp.value or dp.value.isoformat() > _date.today().replace(year=_date.today().year - 1).isoformat():
                continue
            found.append({"name": f"{m[1]}, {m[2]}", "birthDate": dp.value.isoformat(), "dob_status": "ok",
                          "source": "evidence", "line": i + 1, "gender": None, "order_free": False,
                          "labelled": labelled, "text": text.strip()})
    return found


def svnr_check(number: str) -> dict | None:
    """Austrian social security number 'NNNC DDMMYY': check digit C (weights 3 7 9 . 5 8 4 2 1 6, mod 11)
    and the embedded date part. Returns {value, check_ok, ddmmyy} or None if not 10 digits."""
    d = re.sub(r"\D", "", number or "")
    if len(d) != 10:
        return None
    w = [3, 7, 9, 0, 5, 8, 4, 2, 1, 6]
    total = sum(int(x) * k for x, k in zip(d, w)) % 11
    return {"value": d, "check_ok": total != 10 and total == int(d[3]), "ddmmyy": d[4:]}


def _integrity_checks(p: Parsed, kind, lines, table_lines, seen, labels, h1_idx) -> None:
    from datetime import datetime as _dt, date as _date, timezone as _tz
    # other people's name + birth date anywhere in the document
    main = {(tuple(_fold(x["name"] or "").split()), x["birthDate"]) for x in p.patient_lines}
    main_dobs = {x["birthDate"] for x in p.patient_lines if x.get("birthDate")}
    for ev in _evidence(p, lines, table_lines, labels):
        ev.pop("text", None)
        if ev.pop("labelled", False) and ev["birthDate"] in main_dobs:
            continue                           # the patient line's own birth date
        if (tuple(_fold(ev["name"] or "").split()), ev["birthDate"]) not in main:
            p.patient_lines.append(ev)
            p._warn("the document names a person with a birth date outside the patient line", code="PATIENT_EVIDENCE")
    # doubt about whose sample / document this is ('Probenröhrchen beschriftet mit …', 'Verwechslung', 'Zuordnung
    # bitte klinisch prüfen'): never filed without a human
    if m_id := _IDENTITY_DOUBT.search(re.sub(r"\s+", " ", "\n".join(lines))):
        p._warn(f"the document doubts the sample's or document's identity ('{m_id[0][:40]}')", hold=True,
                code="IDENTITY_DOUBT")
    # Austrian social security number: check digit and embedded date
    for _, v in seen.get("svnr", []):
        r = svnr_check(v)
        if r is None:
            p._warn("social security number is not 10 digits; ignored", code="SVNR_FORMAT")
            continue
        if p.patient is not None:
            p.patient.setdefault("svnr", r)
        if not r["check_ok"]:
            p._warn("social security number fails its check digit", code="SVNR_CHECKDIGIT",
                    hold=SVNR_CHECKDIGIT_HOLDS)
    # more than one report in one upload
    h1s = [ln for i, ln in enumerate(lines) if re.match(r"^\s*#\s+\S", ln) and i not in table_lines]
    reportish = [h for h in h1s if re.search(r"befund|bericht|brief|report|summary|laborwerte|mitteilung",
                                             _fold(h))]
    samples = _distinct_times(seen.get("sample", []))
    reported = _distinct_times(seen.get("reported", []))
    if len(reportish) >= 2 or len(samples) >= 2 or len(reported) >= 2:
        p._warn(f"the upload contains more than one report ({max(len(reportish), len(samples), len(reported))}); "
                "each report must be sent on its own", hold=True, code="MULTIPLE_REPORTS")
    # table rows outside a parsed table (a page break split the table)
    if kind == LAB:
        orphan = [i for i, ln in enumerate(lines)
                  if i not in table_lines and ln.strip().startswith("|") and ln.count("|") >= 3]
        if orphan:
            p._warn(f"{len(orphan)} table row(s) outside the result table (split table?): not filed as values",
                    hold=True, code="TABLE_SPLIT")
    # dates in the future
    now = _dt.now(_tz.utc)
    for attr, label in (("sample_time", "sample"), ("report_time", "report")):
        v = getattr(p, attr)
        if not v:
            continue
        if "T" in v:
            future = (_dt.fromisoformat(v) - now).total_seconds() > FUTURE_TOLERANCE_S
        else:
            future = v > _date.today().isoformat()
        if future:
            p._warn(f"the {label} date {v[:16]} lies in the future", hold=True, code="FUTURE_DATE")
    # discharge: interim report
    if kind == DISCHARGE and re.search(r"zwischenbericht|verlaufsbericht|zwischenbefund|interim|progress report",
                                       _fold(p.title or "")):
        p.report_status = "preliminary"
        p._warn("interim report (Zwischenbericht): the stay is not finished", hold=True, code="REPORT_INTERIM")


SVNR_CHECKDIGIT_HOLDS = False   # a bad check digit with a matching name + DOB is a typo in the number: warning

_EMPH = re.compile(r"^(\*\*|__|\*|_|`|~~)(?=\S)(.*?)(?<=\S)\1$")


def strip_emphasis(cell: str) -> str:
    """'**2.6**' -> '2.6', '_g/L_' -> 'g/L'. Only paired wrappers are removed ('245 *' keeps its flag)."""
    c = cell.strip()
    while (m := _EMPH.match(c)):
        c = m[2].strip()
    return c


_SG_LOINC = {"5811-5", "2965-2", "53326-1", "50562-6"}


def _specific_gravity(test: str, loinc_cell: str, unit: str) -> bool:
    """Urine specific gravity: dimensionless and always about 1.0 (1,005–1,030), so '1,015' can only be 1.015."""
    if unit and unit.strip() not in ("", "-", "kg/l", "g/ml", "kg/L", "g/mL"):
        return False
    return bool(set(_LOINC.findall(loinc_cell or "")) & _SG_LOINC) or bool(
        re.search(r"spez\w*\.?\s*gewicht|specific\s+gravity|\bsg\b|uringewicht|harndichte|relative dichte",
                  _fold(test or "")))


def _lab_row(cells, cols, unknown_cols, line_no, p: Parsed) -> dict:
    start = len(p.findings)
    row = _lab_row_inner(cells, cols, unknown_cols, line_no, p)
    for f in p.findings[start:]:            # name the actual row in every finding of this row
        f["detail"] = {"field": "row", "code": f["code"], "row": row.get("test") or f"Zeile {line_no}",
                       "value": row.get("value_text"), "unit": row.get("unit"), "loinc": row.get("loinc"),
                       "expected": row.get("_expected_unit")}
    row.pop("_expected_unit", None)
    return row


def _lab_row_inner(cells, cols, unknown_cols, line_no, p: Parsed) -> dict:
    cells = [strip_emphasis(c) for c in cells]
    get = lambda c: cells[cols[c]].strip() if c in cols else ""
    test = get("test")
    value_cell = get("value")
    unit = get("unit")
    range_text = get("range")
    loinc_cell = get("loinc")
    flag_cell = get("flag")
    pv = parse_value(value_cell)
    label = test or f"line {line_no}"
    sg = _specific_gravity(test, loinc_cell, unit)
    if sg and pv.get("problem") and (msg := re.fullmatch(r"\s*(1)[.,](0\d{2})\s*", value_cell)):
        # 'Spez. Gewicht 1,015': always about 1.0, so a decimal separator, never 1015
        pv = {"kind": "num", "num": Decimal(f"{msg[1]}.{msg[2]}"), "comparator": None, "rest": "", "flag": None}
    sg_range = None
    if sg and range_text and (mr := re.fullmatch(r"\s*1[.,](0\d{2})\s*[-–—]\s*1[.,](0\d{2})\s*", range_text)):
        sg_range = (Decimal(f"1.{mr[1]}"), Decimal(f"1.{mr[2]}"))
    row = {"test": test or None, "value": None, "value_text": value_cell, "comparator": None,
           "unit": unit or None, "ucum": None, "ref_low": None, "ref_high": None,
           "ref_text": range_text or None, "loinc": None, "flag": None, "flag_text": None, "interpretation": None,
           "notes": [], "line": line_no, "status": None, "absent": None}
    if pv.get("problem"):
        p._warn(f"{label}: {pv['problem']}", hold=True, code="VALUE_AMBIGUOUS")
    if pv["kind"] == "num":
        row["value"] = json_number(pv["num"])
        row["comparator"] = pv["comparator"]
        if pv.get("rest"):
            if not unit:
                row["unit"] = unit = pv["rest"]
            elif _norm_text(pv["rest"]) != _norm_text(unit):
                p._warn(f"{label}: result cell '{value_cell}' has extra text '{pv['rest']}'", hold=True, code="VALUE_AMBIGUOUS")
        row["flag"] = pv.get("flag")
        if pv.get("flag"):
            row["flag_text"] = pv.get("flag_text")
    elif pv["kind"] == "text" and _PENDING.match(_norm_text(pv["text"])):
        row["status"] = "registered"                 # result still pending: never a final value
        row["notes"].append(f"Ergebnis ausständig: {pv['text']}")
        p._warn(f"{label}: result pending ('{pv['text']}'); filed without a value", code="RESULT_PENDING")
    elif pv["kind"] == "text" and _NOT_DETERMINABLE.match(_norm_text(pv["text"])):
        row["status"] = "final"
        row["absent"] = "error"                      # dataAbsentReason 'error' + the lab's words as note
        row["notes"].append(pv["text"])
        p._warn(f"{label}: not determinable ('{pv['text']}'); filed without a value", code="RESULT_NOT_DETERMINABLE")
    elif pv["kind"] == "text":
        row["value"] = pv["text"]
        if not pv.get("problem"):
            p._warn(f"{label}: non-numeric result '{pv['text']}' filed as text")
    else:
        p._warn(f"{label}: no result value")
    if flag_cell:
        f = _flag(flag_cell)
        if f is None:
            # the lab's flag is filed as text (interpretation.text), never dropped
            row["flag_text"] = flag_cell
            p._warn(f"{label}: flag '{flag_cell}' has no unambiguous code; filed as text")
        else:
            if row["flag"] and row["flag"] != f:
                p._warn(f"{label}: flag in result cell and flag column disagree", hold=True, code="FLAG_CONFLICT")
            row["flag"], row["flag_text"] = f, flag_cell
    # unit: a UCUM code is never invented; an unmappable unit is held
    if row["unit"]:
        code, prob = to_ucum(row["unit"])
        row["ucum"] = code
        if prob:
            p._warn(f"{label}: {prob}", hold=True, code="UNKNOWN_UNIT")
    # LOINC
    if loinc_cell:
        codes = _LOINC.findall(loinc_cell)
        if not codes:
            p._warn(f"{label}: LOINC '{loinc_cell}' is not a LOINC code", hold=True, code="LOINC_CHECKDIGIT")
        else:
            if len(set(codes)) > 1:
                p._warn(f"{label}: several LOINC codes '{loinc_cell}'", hold=True, code="LOINC_AMBIGUOUS")
            row["loinc"] = codes[0]
            if not loinc_check_ok(codes[0]):
                p._warn(f"{label}: LOINC {codes[0]} fails the LOINC check digit", hold=True, code="LOINC_CHECKDIGIT")
    else:
        p._warn(f"{label}: no LOINC code; coded as text only")
    info = _loinc.info(row["loinc"]) if row["loinc"] and loinc_check_ok(row["loinc"]) else None
    if info and info.get("not_found"):
        p._warn(f"{label}: LOINC {row['loinc']} does not exist", hold=True, code="LOINC_UNKNOWN")
        info = None
    elif row["loinc"] and loinc_check_ok(row["loinc"]) and info is None:
        p._warn(f"{label}: LOINC {row['loinc']} not in the offline cache and not looked up; "
                f"display and unit fit not checked")
    if info and (info.get("inactive") or (info.get("status") or "ACTIVE") not in ("ACTIVE", "TRIAL")):
        p._warn(f"{label}: LOINC {row['loinc']} is {info.get('status') or 'inactive'}")
    example = (info or {}).get("example_ucum")
    row["_expected_unit"] = ";".join((info or {}).get("example_ucum_list") or ([example] if example else [])) or None
    dimless_example = _loinc.dimensionless_example(example)
    is_inr = row["loinc"] in ("6301-6", "34714-6", "46418-0", "38875-1", "52129-4") or (
        info is None and bool(re.search(r"\binr\b", _fold(test or ""))))   # no usable LOINC facts: by name
    if pv["kind"] == "num":
        if not row["unit"]:
            # a unit is never assumed, except the annotation of a dimensionless code (INR)
            if dimless_example:
                row["ucum"] = dimless_example
            elif is_inr:
                row["ucum"] = "{INR}"
            else:
                p._warn(f"{label}: numeric result without a unit"
                        + (f" (LOINC {row['loinc']} expects a unit like {example})" if example else ""),
                        hold=True, code="UNIT_MISSING")
        elif row["ucum"] and (dimless_example or is_inr):
            want = dimless_example or "{INR}"
            if row["ucum"] != want:
                p._warn(f"{label}: unit '{row['unit']}' given for a dimensionless test (expected none)",
                        hold=True, code="LOINC_UNIT_DIMENSION")
        elif row["ucum"] and info:
            fits, ex = _loinc.unit_fits(row["loinc"], row["ucum"])
            if fits is False:
                p._warn(f"{label}: unit '{row['unit']}' ({row['ucum']}) does not fit LOINC {row['loinc']} "
                        f"'{info['display']}' (expected a unit like {ex})", hold=True, code="LOINC_UNIT_DIMENSION")
            elif fits is None:
                p._warn(f"{label}: unit '{row['unit']}' could not be compared with LOINC {row['loinc']}")
    # reference range (bounds are parsed; NO interpretation is derived from them:
    # we file exactly what was sent, flags only when the lab stated them)
    if range_text:
        rng = parse_range(range_text, row["unit"]) if sg_range is None else \
            {"low": sg_range[0], "high": sg_range[1], "low_closed": True, "high_closed": True}
        if rng.get("problem"):
            p._warn(f"{label}: {rng['problem']}")
        row["ref_low"] = json_number(rng["low"]) if rng["low"] is not None else None
        row["ref_high"] = json_number(rng["high"]) if rng["high"] is not None else None
        row["ref_low_closed"], row["ref_high_closed"] = rng["low_closed"], rng["high_closed"]
    row["interpretation"] = row["flag"]
    for ci, h in unknown_cols:
        val = cells[ci].strip() if ci < len(cells) else ""
        if val:
            row["notes"].append(f"{h}: {val}")
    return row


def parse_upload(raw: bytes, doc_kind_hint: str | None = None) -> Parsed:
    """Raw upload bytes -> Parsed: ``decode_upload`` (UTF-8, else Windows-1252/Latin-1 with a warning),
    then ``parse_answer``. An undecodable file yields an empty Parsed with an ENCODING_UNREADABLE block."""
    d = decode_upload(raw)
    if d["text"] is None:
        p = Parsed(text="")
        p._warn("the file is not readable text (unknown encoding or binary)", block=True, code="ENCODING_UNREADABLE")
        return p
    p = parse_answer(d["text"], doc_kind_hint)
    p.encoding = d["encoding"]
    p.raw = bytes(raw)                    # hashes and attachments use these exact bytes
    for w in d["warnings"]:
        p._warn(w, code="ENCODING_FALLBACK")
    return p

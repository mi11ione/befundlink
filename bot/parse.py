"""Pure parsing helpers: commands, request drafts, contacts. No I/O."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- text normalisation

_SPACES = re.compile(r"[ \t  -​  　]+")


def norm(s: str) -> str:
    """NFC (macOS input can arrive decomposed), unify odd spaces, collapse runs, strip."""
    s = unicodedata.normalize("NFC", s or "")
    s = s.replace("\r", "")
    s = _SPACES.sub(" ", s)
    return s.strip()


def strip_reply_fallback(body: str) -> str:
    """Matrix replies carry '> <@user> quoted' lines before the real text; drop them."""
    lines = (body or "").split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    if i and i < len(lines) and lines[i].strip() == "":
        i += 1
    return "\n".join(lines[i:]) if i else body


# ---------------------------------------------------------------- doc kinds

LAB = "lab_result"
REPORT = "discharge_report"

_KIND_ALIASES = {
    LAB: {"labor", "lab", "laborbefund", "labbefund", "laborwerte", "laborergebnis",
          "befund", "blutbefund", "gerinnung", "lab_result", "labresult", "result"},
    REPORT: {"reha", "rehab", "rehabericht", "entlassungsbericht", "entlassbericht",
             "entlassung", "bericht", "report", "discharge", "discharge_report",
             "dischargereport", "arztbrief"},
}

KIND_LABEL = {
    "de": {LAB: "Laborbefund", REPORT: "Entlassungsbericht"},
    "en": {LAB: "lab result", REPORT: "discharge report"},
}


def parse_kind(token: str) -> str | None:
    t = norm(token).lower().strip(".:!")
    t = t.replace("-", "").replace(" ", "")
    if t in {"1"}:
        return LAB
    if t in {"2"}:
        return REPORT
    for kind, names in _KIND_ALIASES.items():
        if t in {n.replace("_", "") for n in names} | names:
            return kind
    return None


# ---------------------------------------------------------------- contacts

_EMAIL = re.compile(r"[^\s@,;<>()\"']+@[^\s@,;<>()\"']+\.[^\s@,;<>()\"'.]+")
_EMAIL_FULL = re.compile(r"^" + _EMAIL.pattern + r"$")
_CONTACT_LABEL = re.compile(
    r"^(?:e-?mail|mail|kontakt|contact|tel|telefon|phone|mobil|mobile|handy|sms|nr|nummer)(?:\.?\s*[:=]\s*|\.\s*|\s+)",
    re.I,
)
_PHONE_CHARS = re.compile(r"^[+\d][\d\s\-/().]*\d$")
# A phone number embedded in a longer field, e.g. "Reha Waldblick +43 660 000 0101".
_PHONE_IN_TEXT = re.compile(r"(?:(?:\+|00)\s?43|\b0(?=\s?6))[\d\s\-/().]{5,}\d")


@dataclass
class Contact:
    kind: str            # 'email' | 'sms' | 'foreign_phone' | 'landline' | 'invalid'
    value: str           # normalised address/number (or the raw input when invalid)


def _format_at(nat: str) -> str:
    """nat = national significant number without leading 0, e.g. 6600000101."""
    if nat.startswith("6") and len(nat) >= 9:
        prefix, rest = nat[:3], nat[3:]
        if len(rest) >= 7:
            return f"+43 {prefix} {rest[:-4]} {rest[-4:]}"
        return f"+43 {prefix} {rest}"
    return f"+43 {nat}"


def classify_contact(raw: str) -> Contact:
    s = norm(raw)
    s = s.strip("<>").strip()
    if s.lower().startswith("mailto:"):
        s = s[7:]
    s = _CONTACT_LABEL.sub("", s).strip().strip("<>").strip()
    if "@" in s:
        if _EMAIL_FULL.match(s):
            return Contact("email", s)          # case kept on purpose: 'I' vs 'l' must survive
        return Contact("invalid", raw)
    s2 = s.replace("(0)", "")
    if not _PHONE_CHARS.match(s2):
        return Contact("invalid", raw)
    digits = re.sub(r"\D", "", s2)
    if s2.startswith("+"):
        if not digits.startswith("43"):
            return Contact("foreign_phone", s)
        nat = digits[2:]
    elif digits.startswith("00"):
        if not digits.startswith("0043"):
            return Contact("foreign_phone", s)
        nat = digits[4:]
    elif digits.startswith("0"):
        nat = digits[1:]
    else:
        return Contact("invalid", raw)
    nat = nat.lstrip("0")                        # '+43 0660 …' typo
    if not 7 <= len(nat) <= 13:
        return Contact("invalid", raw)
    if not nat.startswith("6"):                  # SMS needs an Austrian mobile number (+43 6xx)
        return Contact("landline", _format_at(nat))
    return Contact("sms", _format_at(nat))


def find_contact_in(text: str) -> tuple[Contact | None, str]:
    """Find an email or AT phone inside a longer field; returns (contact, remaining text)."""
    m = _EMAIL.search(text)
    if m:
        c = classify_contact(m.group(0))
        if c.kind == "email":
            return c, norm(text[: m.start()] + " " + text[m.end():])
    m = _PHONE_IN_TEXT.search(text)
    if m:
        c = classify_contact(m.group(0))
        if c.kind == "sms":
            return c, norm(text[: m.start()] + " " + text[m.end():])
    return None, text


def channel_for(contact: Contact) -> str | None:
    return {"email": "email", "sms": "sms"}.get(contact.kind)


# ---------------------------------------------------------------- local address heuristics

def local_address_warnings(channel: str | None, address: str | None) -> list[dict]:
    """Cheap, service-independent look-alike checks (the service does the full check
    against known org domains). Catches a capital I typed for a lowercase l."""
    if channel != "email" or not address or "@" not in address:
        return []
    local, domain = address.rsplit("@", 1)
    out: list[dict] = []
    fixed_local = re.sub(r"(?<=[a-z])I", "l", local)
    fixed_domain = domain.replace("I", "l").lower()
    bad_parts = []
    if any(c.isupper() for c in domain):
        bad_parts.append((domain, fixed_domain))
    if fixed_local != local:
        bad_parts.append((local, fixed_local))
    for orig, fixed in bad_parts:
        what = "ein großes I statt l" if "I" in orig else "Großbuchstaben"
        out.append({"code": "confusable_case", "field": "address",
                    "message": f"Achtung: '{orig}' enthält {what} – meinten Sie {fixed}?",
                    "suggestion": f"{fixed_local}@{fixed_domain}"})
    bad = [c for c in domain if ord(c) > 127]
    if bad:
        names = ", ".join(f"'{c}' (U+{ord(c):04X})" for c in dict.fromkeys(bad))
        out.append({"code": "non_ascii_domain", "field": "address",
                    "message": f"Achtung: Die Domain '{domain}' enthält Nicht-ASCII-Zeichen {names} – bitte prüfen."})
    return out


# ---------------------------------------------------------------- patient ids

FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def slugify(s: str) -> str:
    s = norm(s).translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:64]


def parse_patient(raw: str) -> tuple[str | None, bool]:
    """Returns (patient_id, derived_from_name)."""
    s = norm(raw).strip("\"'`“”„«»").rstrip(".").strip()
    s = re.sub(r"^(?:Patient/)", "", s)
    if not s:
        return None, False
    if FHIR_ID.match(s):
        return s, False
    slug = slugify(s)
    return (slug or None), True


# ---------------------------------------------------------------- commands

@dataclass
class Command:
    name: str          # request status audit help yes no cancel fix file reject hold unknown
    args: str
    lang: str          # 'de' | 'en'
    word: str = ""


_CMD = re.compile(r"^!\s*([^\s]+)\s*(.*)$", re.S)
_CMD_MAP = {
    "anfordern": ("request", "de"), "anforderung": ("request", "de"), "anfrage": ("request", "de"),
    "request": ("request", "en"), "req": ("request", "en"),
    "status": ("status", "de"),
    "audit": ("audit", "de"), "protokoll": ("audit", "de"), "log": ("audit", "en"),
    "hilfe": ("help", "de"), "help": ("help", "en"), "?": ("help", "de"),
    "ja": ("yes", "de"), "yes": ("yes", "en"), "j": ("yes", "de"), "y": ("yes", "en"),
    "senden": ("yes", "de"), "send": ("yes", "en"), "ok": ("yes", "de"),
    "nein": ("no", "de"), "no": ("no", "en"), "n": ("no", "de"),
    "abbrechen": ("cancel", "de"), "abbruch": ("cancel", "de"), "stop": ("cancel", "en"), "cancel": ("cancel", "en"),
    "ablegen": ("file", "de"), "file": ("file", "en"),
    "verwerfen": ("reject", "de"), "reject": ("reject", "en"),
    "halten": ("hold", "de"), "hold": ("hold", "en"),
    "identität": ("identity", "de"), "identitaet": ("identity", "de"), "person": ("identity", "de"),
    "identity": ("identity", "en"),
    "labor": ("short_lab", "de"), "lab": ("short_lab", "en"), "laborbefund": ("short_lab", "de"),
    "reha": ("short_report", "de"), "rehab": ("short_report", "en"), "report": ("short_report", "en"),
    "entlassungsbericht": ("short_report", "de"),
    "zeigen": ("show", "de"), "anzeigen": ("show", "de"), "show": ("show", "en"),
    "freigeben": ("unlock", "de"), "entsperren": ("unlock", "de"), "unlock": ("unlock", "en"),
    "korrigieren": ("fix", "de"), "korrektur": ("fix", "de"), "fix": ("fix", "en"),
}


def parse_command(body: str) -> Command | None:
    text = norm(strip_reply_fallback(body))
    m = _CMD.match(text)
    if not m:
        return None
    word = m.group(1).lower().rstrip(":,")
    name, lang = _CMD_MAP.get(word, ("unknown", "de"))
    return Command(name, m.group(2).strip(), lang, word)


_YES = {"ja", "j", "yes", "y", "ok", "okay", "senden", "send", "passt", "jawohl"}
_NO = {"nein", "n", "no", "nee", "abbrechen", "cancel", "stop"}


def yes_no(text: str) -> str | None:
    t = norm(text).lower().strip("!.").strip()
    if t in _YES:
        return "yes"
    if t in _NO:
        return "no"
    return None


# ---------------------------------------------------------------- request drafts

FIELDS = ["patient", "doc_kind", "name", "role", "org", "contact"]


@dataclass
class Draft:
    patient_id: str | None = None
    doc_kind: str | None = None
    name: str | None = None
    role: str | None = None
    org: str | None = None
    channel: str | None = None
    address: str | None = None
    note: str | None = None
    request_text: str | None = None     # shown to the outside person only after the eID check
    patient_from_name: bool = False
    problems: list[str] = field(default_factory=list)   # human-readable parse problems

    _ATTR = {"patient": "patient_id", "contact": "address"}

    def get(self, step: str):
        return getattr(self, self._ATTR.get(step, step))

    def clear(self, step: str) -> None:
        setattr(self, self._ATTR.get(step, step), None)
        if step == "contact":
            self.channel = None
        if step == "patient":
            self.patient_from_name = False

    def missing(self) -> list[str]:
        return [s for s in FIELDS if not self.get(s)]

    def to_body(self, room_id: str, requester: str) -> dict:
        body = {
            "requester_mxid": requester,
            "room_id": room_id,
            "recipient": {"name": self.name, "role": self.role, "org": self.org},
            "channel": self.channel,
            "address": self.address,
            "patient_id": self.patient_id,
            "doc_kind": self.doc_kind,
        }
        if self.note:   # stays at the clinic: never put into the email/SMS to the outside person
            body["internal_note"] = self.note
        if self.request_text:   # what result is wanted; never in the email/SMS, only after ID Austria
            body["request_text"] = self.request_text
        return body

    def as_dict(self) -> dict:
        return asdict(self)


_PATIENT_CLAUSE = re.compile(
    r"(?:^|(?<=[\s,;]))(?:f(?:ü|ue)r\s+)?(?:patient(?:in)?(?:[-_ ]?id)?|pat\.|pid)(?=[\s:=#])\s*[:=#]?\s*([^,;]+?)\s*(?=[,;]|$)",
    re.I,
)
_KV = re.compile(
    r"^(name|empfänger|empfaenger|recipient|rolle|role|funktion|org|organisation|organization|"
    r"einrichtung|institution|notiz|note|hinweis|kommentar|art|typ|type|kind|dokument)\s*[:=]\s*(.+)$",
    re.I,
)
_FREE_KEYS = r"text|frage|anfragetext|question|request_text|notiz|note|hinweis|kommentar"
_FREE = re.compile(
    r"(?:^|[,;]\s*)(" + _FREE_KEYS + r")\s*:\s*(.*?)"
    r"(?=[,;]\s*(?:(?:" + _FREE_KEYS + r")\s*:|(?:f(?:ü|ue)r\s+)?(?:patient(?:in)?(?:[-_ ]?id)?|pat\.|pid)[\s:=#])|$)",
    re.I | re.S)
_FREE_TARGET = {"text": "request_text", "frage": "request_text", "anfragetext": "request_text",
                "question": "request_text", "request_text": "request_text",
                "notiz": "note", "note": "note", "hinweis": "note", "kommentar": "note"}
_KV_TARGET = {
    "name": "name", "empfänger": "name", "empfaenger": "name", "recipient": "name",
    "rolle": "role", "role": "role", "funktion": "role",
    "org": "org", "organisation": "org", "organization": "org", "einrichtung": "org", "institution": "org",
    "notiz": "note", "note": "note", "hinweis": "note", "kommentar": "note",
    "art": "doc_kind", "typ": "doc_kind", "type": "doc_kind", "kind": "doc_kind", "dokument": "doc_kind",
}


def contact_problem(c: Contact, lang: str = "de") -> str:
    if lang == "en":
        return {
            "foreign_phone": f"'{c.value}' is not an Austrian number (+43). SMS goes to Austrian mobile numbers only.",
            "landline": f"'{c.value}' looks like a landline; SMS needs a mobile number (+43 6xx …).",
        }.get(c.kind, f"'{norm(c.value)}' is neither an email address nor an Austrian mobile number.")
    return {
        "foreign_phone": f"'{c.value}' ist keine österreichische Nummer (+43). SMS nur an österreichische Mobilnummern.",
        "landline": f"'{c.value}' sieht nach Festnetz aus; SMS braucht eine Mobilnummer (+43 6xx …).",
    }.get(c.kind, f"'{norm(c.value)}' ist weder eine E-Mail-Adresse noch eine österreichische Mobilnummer.")


def parse_request_args(args: str, lang: str = "de") -> Draft:
    """Parse '<kind> <Name>, <Rolle>, <Organisation>, <Kontakt>, Patient <id>' tolerantly.

    - kind (labor/reha/lab/report/…) may be the first word or a separate field
    - 'Patient <id>' may appear anywhere, with or without a comma before it
    - the contact is detected by shape (email / AT phone), wherever it is
    - 'key: value' fields (Rolle:, Org:, Notiz:, …) are honoured
    - the remaining fields fill name, role, org in order; extra fields are appended to org
    """
    d = Draft()
    text = norm(args.replace("\n", ", "))
    if not text:
        return d

    # 1) doc kind as first word
    first, _, rest = text.partition(" ")
    k = parse_kind(first.rstrip(",;:"))
    if k:
        d.doc_kind, text = k, rest.lstrip(" ,;:")

    # 2) free-text fields (may contain commas): 'Text:' goes to the person after eID, 'Notiz:' stays internal
    for m in list(_FREE.finditer(text))[::-1]:
        key, val = m.group(1).lower(), m.group(2).strip().rstrip(",;").strip()
        if val:
            if _FREE_TARGET[key] == "request_text":
                d.request_text = val[:1000]
            else:
                d.note = val[:1000]
        text = text[: m.start()] + text[m.end():]

    # 3) patient clause (last occurrence wins)
    matches = list(_PATIENT_CLAUSE.finditer(text))
    if matches:
        m = matches[-1]
        pid, derived = parse_patient(m.group(1))
        if pid:
            d.patient_id, d.patient_from_name = pid, derived
        text = text[: m.start()] + text[m.end():]

    # 3) fields
    positional: list[str] = []
    for part in re.split(r"\s*[,;|]\s*", text):
        part = part.strip()
        if not part:
            continue
        kv = _KV.match(part)
        if kv:
            target = _KV_TARGET[kv.group(1).lower()]
            val = kv.group(2).strip()
            if target == "doc_kind":
                kk = parse_kind(val)
                if kk:
                    d.doc_kind = kk
                else:
                    d.problems.append(f"Unbekannte Dokumentart '{val}'." if lang == "de" else f"Unknown document kind '{val}'.")
            else:
                setattr(d, target, val)
            continue
        if not d.doc_kind and parse_kind(part):
            d.doc_kind = parse_kind(part)
            continue
        if not d.address:
            c = classify_contact(part)
            if channel_for(c):
                d.channel, d.address = channel_for(c), c.value
                continue
            c2, remaining = find_contact_in(part)
            if c2:
                d.channel, d.address = channel_for(c2), c2.value
                if remaining:
                    positional.append(remaining)
                continue
            if c.kind in ("foreign_phone", "landline") or ("@" in part and c.kind == "invalid"):
                d.problems.append(contact_problem(c, lang))
                continue
        positional.append(part)

    for step in ("name", "role", "org"):
        if positional and not getattr(d, step):
            setattr(d, step, positional.pop(0))
    if d.name and not full_name_ok(d.name):
        d.problems.append(
            (f"'{d.name}': bitte den vollständigen Namen mit Vor- und Nachname angeben (z. B. Dr. Eva Brandl) – "
             "nur diese Person kann die Anfrage nach der ID-Austria-Prüfung beantworten.") if lang == "de" else
            (f"'{d.name}': please give the full name, given name and family name (e.g. Dr. Eva Brandl) – "
             "only that person can answer after the ID Austria check."))
    if positional:
        if d.org:
            d.org = ", ".join([d.org, *positional])
        else:
            d.note = ", ".join(([d.note] if d.note else []) + positional)
    return d


# ---------------------------------------------------------------- ids and names (security)

ID_RE = re.compile(r"^(req-[0-9a-f]{12}|KB-[A-Z0-9]{6}|\d+)$")


def valid_id(s) -> str | None:
    """Request id, ref code or answer number; anything else is rejected (never put into a URL)."""
    s = str(s or "").strip()
    return s if ID_RE.match(s) else None


_TITLES = {"dr", "prof", "mag", "magª", "di", "dipl", "ing", "univ", "med", "msc", "bsc", "ba", "ma", "mba",
           "phd", "priv", "doz", "ddr", "dent", "vet", "rer", "nat", "pharm", "fh", "mmag", "dkfm", "mr", "mrs",
           "ms", "frau", "herr", "dipl.-ing", "techn", "sc", "jur", "phil", "pd"}


def name_tokens(name: str) -> list[str]:
    """Name parts without academic titles/salutations ('Dr. med. Eva Brandl, MSc' -> ['Eva', 'Brandl'])."""
    out = []
    for tok in re.split(r"[\s,]+", norm(name)):
        bare = tok.strip(".").lower()
        if not bare or bare in _TITLES or all(p.lower() in _TITLES for p in tok.split(".") if p):
            continue
        if not re.search(r"[^\W\d_]", tok):        # needs letters
            continue
        out.append(tok)
    return out


def full_name_ok(name: str | None) -> bool:
    """Identity binding compares names: require at least a given name plus a family name."""
    return len(name_tokens(name or "")) >= 2


# ---------------------------------------------------------------- short form: !labor Brandl maria-beispiel

@dataclass
class Short:
    query: list[str] = field(default_factory=list)       # name words (+ optional given name / org words)
    patient_id: str | None = None
    patient_from_name: bool = False
    channel: str | None = None
    address: str | None = None
    request_text: str | None = None
    note: str | None = None
    problems: list[str] = field(default_factory=list)
    long_form: bool = False                               # looked like the comma form -> parse as such


def parse_short(args: str, lang: str = "de") -> Short:
    """'<Nachname> [Vorname|Org-Wort …] <patient-id> [Kontakt] [, Text: …] [, Notiz: …]'.
    'Patient <id>' may be written out; a contact (e-mail / AT mobile) may be added anywhere."""
    sh = Short()
    text = norm(args.replace("\n", ", "))
    for m in list(_FREE.finditer(text))[::-1]:
        key, val = m.group(1).lower(), m.group(2).strip().rstrip(",;").strip()
        if val:
            if _FREE_TARGET[key] == "request_text":
                sh.request_text = val[:1000]
            else:
                sh.note = val[:1000]
        text = text[: m.start()] + text[m.end():]
    if text.count(",") + text.count(";") >= 3:
        sh.long_form = True
        return sh
    c, rest = find_contact_in(text)
    if c:
        sh.channel, sh.address, text = channel_for(c), c.value, rest
    else:
        m = re.search(r"(?:\+|\b00)\d[\d\s\-/().]{5,}\d|\b0\d[\d\s\-/]{5,}\d", text)
        if m:
            sh.problems.append(contact_problem(classify_contact(m.group(0)), lang))
        for tok in text.split():
            if "@" in tok:
                sh.problems.append(contact_problem(classify_contact(tok), lang))
    matches = list(_PATIENT_CLAUSE.finditer(text))
    if matches:
        m = matches[-1]
        sh.patient_id, sh.patient_from_name = parse_patient(m.group(1))
        text = text[: m.start()] + text[m.end():]
    toks = [t for t in re.split(r"[\s,;]+", text) if t]
    if not sh.patient_id and len(toks) >= 2 and FHIR_ID.match(toks[-1]):
        sh.patient_id = toks.pop()
    sh.query = toks
    return sh

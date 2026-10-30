"""Names and dates: normalisation and strict person matching.

Everything here is deterministic and dependency free.

Matching semantics (``same_person``) are documented on the function and are
deliberately conservative: when in doubt the answer is "not the same person",
which makes the caller hold the answer and ask instead of filing it.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo("Europe/Vienna")

# --------------------------------------------------------------------------- names

# Letters that NFKD does not decompose.
_SPECIAL = str.maketrans({
    "ß": "ss", "ẞ": "ss", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ı": "i",
    "þ": "th", "Þ": "th",
})
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "ae", "Ö": "oe", "Ü": "ue"})

# Titles that are not part of a person's name (Austrian usage included). Compared
# after casefolding and removing dots and hyphens. Prefix titles are stripped only
# at the start of a name, suffix degrees only at the end, so real name tokens such
# as 'Ma' (surname) or 'Di' (particle) in the middle are never lost.
_PREFIX_TITLES = {
    "dr", "drin", "drmed", "drmeddent", "drmedvet", "drmeduniv", "drphil", "drrernat", "drjur",
    "drtechn", "drscmed", "drmeduniv", "drmeddentuniv", "prof", "profin", "univprof", "univprofin",
    "univ", "doz", "privdoz", "priv", "mag", "maga", "magpharm", "magrernat", "dipling", "diplingin",
    "dipl", "ing", "ingin", "mr", "mrs", "ms", "frau", "herr", "fr", "hr", "oa", "oain", "prim",
    "primar", "primaria", "ass", "assdr", "sir", "dame", "dgkp", "dgks", "pd", "hofrat", "medrat",
    "med", "dent", "vet", "techn", "phil", "rer", "nat", "jur", "habil", "sc",
    # Austrian titles of office (Amtstitel) and honorary titles
    "ostr", "studr", "schr", "oschr", "regr", "regrat", "oregr", "komr", "kommr", "medr", "omedr", "vetr", "minr",
    "sekchef", "amtsdir", "ragdir", "oberstudienrat", "studienrat", "schulrat", "regierungsrat", "kommerzialrat",
    "medizinalrat", "ök", "ökr", "okr", "bgm", "abg", "hofr", "prokurist", "ing.", "dkfm", "ddr", "dr.h.c", "drhc",
    "obermedrat", "dir", "direktor", "direktorin",
}
_SUFFIX_TITLES = {
    "bsc", "msc", "ma", "ba", "mba", "phd", "md", "med", "rn", "mph", "llm", "mas", "meng", "beng",
    "dr", "jun", "jr", "sen", "sr",
}
_TOKEN_SPLIT = re.compile(r"[\s‐‑‒–—_,;/]+|(?<=\.)-|-(?=[A-ZÄÖÜa-zäöü]+\.)")


def _strip_marks(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _key_strip(token: str) -> str:
    """Diacritics removed: Müller -> muller."""
    return _strip_marks(token.translate(_SPECIAL)).casefold()


def _key_expand(token: str) -> str:
    """German umlauts expanded first: Müller -> mueller."""
    return _strip_marks(unicodedata.normalize("NFC", token).translate(_UMLAUT).translate(_SPECIAL)).casefold()


def _tkey(raw: str) -> str:
    return re.sub(r"[.\-()]", "", _key_strip(raw))


def _is_prefix_title(raw: str) -> bool:
    if raw.strip(".") in ("DI", "DI(FH)", "DDI", "DIin"):     # Diplom-Ingenieur, upper case only
        return True
    return _tkey(raw) in _PREFIX_TITLES


def _is_suffix_title(raw: str) -> bool:
    return _tkey(raw) in _SUFFIX_TITLES or raw.strip(".") in ("DI", "BEd", "MEd")


def _is_title(raw: str) -> bool:
    return _is_prefix_title(raw) or _is_suffix_title(raw)


def _raw_tokens(name: str) -> list[str]:
    """Whitespace/comma tokens with glued titles split ('Dr.Eva' -> 'Dr.', 'Eva').
    Hyphenated names stay one token here."""
    s = unicodedata.normalize("NFC", name or "")
    s = re.sub(r"\([^)]*\)", " ", s)              # parenthesised additions
    raw: list[str] = []
    for tok in _TOKEN_SPLIT.split(s):
        tok = tok.strip(" '’`\"")
        if not tok:
            continue
        parts = [p for p in tok.split(".") if p]
        if len(parts) > 1 and not _is_title(tok) and any(_is_title(p) for p in parts[:-1]):
            raw.extend(p + "." for p in parts[:-1])
            raw.append(parts[-1] + ("." if tok.endswith(".") else ""))
        else:
            raw.append(tok)
    return raw


def _strip_titles(raw: list[str]) -> tuple[list[str], list[str], list[str]]:
    i, j = 0, len(raw)
    while i < j and _is_prefix_title(raw[i]):
        i += 1
    while j > i and _is_suffix_title(raw[j - 1]):
        j -= 1
    return raw[:i], raw[i:j], raw[j:]


def name_tokens(name: str) -> list[str]:
    """Split a free-text name into name tokens without titles.

    'Univ.-Prof. Dr. Eva  Brandl-Huber, MSc' -> ['Eva', 'Brandl', 'Huber']
    Hyphenated names split into tokens; 'Dr.Eva' (glued) is handled.
    """
    _, core, _ = _strip_titles(_raw_tokens(name))
    out = []
    for t in core:
        for piece in t.split("-"):
            piece = piece.strip(".'’`")
            if piece:
                out.append(piece)
    return out


def _loose_normal(name: str) -> str:
    """Canonical comparison form: titles stripped, casefolded, diacritics removed,
    ß -> ss, umlauts kept as plain vowels (Müller -> muller). Tokens joined by one space."""
    return " ".join(_key_strip(t) for t in name_tokens(name))


def _tokens_equal(a: str, b: str) -> bool:
    # Equal if equal with diacritics stripped (Müller = Muller) or with German umlauts
    # expanded (Müller = Mueller). Mueller vs Muller is NOT equal: no umlaut links them.
    return _key_strip(a) == _key_strip(b) or _key_expand(a) == _key_expand(b)


def loose_name_equal(a: str, b: str) -> bool:
    """LOOSE equality for deduplicating practitioners and display checks ONLY. Never use it for a
    patient or identity decision: it ignores token order and treats ä as a. Patient and eID
    decisions use ``compare_names`` / ``same_person`` / ``identity_matches`` / ``decide`` (RULES.md).

    True if two free-text names denote the same name.

    Rule: after removing titles, both names have the same number of tokens and the
    tokens pair up one to one (any order, so 'Beispiel, Maria' = 'Maria Beispiel'),
    each pair equal under `_tokens_equal`. Hyphens count as token separators
    ('Brandl-Huber' = 'Brandl Huber'). Missing or extra tokens (middle names,
    initials) do NOT match.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or len(ta) != len(tb):
        return False
    # Fast path: exact multiset equality on the strip key.
    if Counter(map(_key_strip, ta)) == Counter(map(_key_strip, tb)):
        return True
    # General case: bipartite matching (names have a handful of tokens).
    used = [False] * len(tb)

    def assign(i: int) -> bool:
        if i == len(ta):
            return True
        for j, t in enumerate(tb):
            if not used[j] and _tokens_equal(ta[i], t):
                used[j] = True
                if assign(i + 1):
                    return True
                used[j] = False
        return False

    return assign(0)


_PARTICLES = {"von", "van", "de", "der", "den", "del", "della", "di", "da", "du", "zu", "zur", "vom", "le", "la", "dos", "das", "ten", "ter", "af", "al", "el", "bin", "ibn"}


def split_name(name: str) -> tuple[list[str], str | None, list[str]]:
    """Split 'Dr. Maria Anna Beispiel' -> (['Maria','Anna'], 'Beispiel', ['Dr.']).

    'Family, Given' (one comma, the part after it not just a degree) is honoured.
    Lower-case nobiliary particles go to the family name ('Anna von Trapp' ->
    family 'von Trapp'). Returns (given, family, prefix titles)."""
    name = (name or "").strip()
    parts = [p.strip() for p in name.split(",")]
    while len(parts) > 1 and all(_is_suffix_title(t) or _is_prefix_title(t) for t in _raw_tokens(parts[-1])):
        parts.pop()                                  # 'Eva Brandl, MSc'
    if len(parts) == 2 and parts[0] and parts[1]:
        p1, fam, _ = _strip_titles(_raw_tokens(parts[0]))
        p2, giv, _ = _strip_titles(_raw_tokens(parts[1]))
        if fam and giv:
            return giv, " ".join(fam), p1 + p2
    prefixes, core, _ = _strip_titles(_raw_tokens(" ".join(parts)))
    if not core:
        return [], None, prefixes
    if len(core) == 1:
        return [], core[0], prefixes
    i = len(core) - 1
    while i - 1 >= 1 and core[i - 1] in _PARTICLES:
        i -= 1
    return core[:i], " ".join(core[i:]), prefixes


# --------------------------------------------------------------------------- dates

_MONTHS = {
    # English
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # German, incl. Austrian Jänner/Feber
    "januar": 1, "janner": 1, "jaenner": 1, "jann": 1, "februar": 2, "feber": 2, "marz": 3, "maerz": 3,
    "mrz": 3, "mae": 3, "mai": 5, "juni": 6, "juli": 7, "oktober": 10, "okt": 10, "dezember": 12, "dez": 12,
}


def _month(word: str) -> int | None:
    return _MONTHS.get(_key_strip(word).rstrip("."))


def _mkdate(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


_ISO_D = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NUM_D = re.compile(r"^(\d{1,2})\s*([./])\s*(\d{1,2})\s*\2\s*(\d{2}|\d{4})$")
_DMY_TXT = re.compile(r"^(\d{1,2})\s*\.?\s*([A-Za-zÀ-ž]+)\.?\s*,?\s*(\d{4})$")
_MDY_TXT = re.compile(r"^([A-Za-zÀ-ž]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})$")
_DMY_ORD = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)\s+(?:of\s+)?([A-Za-zÀ-ž]+)\.?\s*,?\s*(\d{4})$", re.I)


class DateParse:
    """Result of parsing a date: value or None plus a reason when None/odd."""

    __slots__ = ("value", "problem")

    def __init__(self, value: date | None, problem: str | None = None):
        self.value, self.problem = value, problem


def parse_date_ex(text: str) -> DateParse:
    """Parse one calendar date written in English, German or numeric notation.

    Accepted: '12 March 1961', '12. März 1961', '12 Mär 1961', 'March 12, 1961',
    '12th of March 1961', '12.03.1961', '12.3.1961', '1961-03-12'. Numeric
    day.month.year with dots is read as D.M.Y (German usage). Slashes
    ('12/03/1961') are read as D/M/Y only when unambiguous (day > 12 or day == month);
    otherwise the date is rejected as ambiguous. Two-digit years are rejected.
    """
    s = unicodedata.normalize("NFC", (text or "").strip()).strip(" .,;")
    s = re.sub(r"\s+", " ", s)
    if not s:
        return DateParse(None, "empty date")
    if m := _ISO_D.match(s):
        d = _mkdate(int(m[1]), int(m[2]), int(m[3]))
        return DateParse(d, None if d else f"invalid date '{text}'")
    if m := _NUM_D.match(s):
        a, sep, b, y = int(m[1]), m[2], int(m[3]), m[4]
        if len(y) == 2:
            return DateParse(None, f"two-digit year in '{text}' is ambiguous")
        if sep == "/" and a <= 12 and b <= 12 and a != b:
            return DateParse(None, f"'{text}' is ambiguous (day/month or month/day)")
        d = _mkdate(int(y), b, a)
        return DateParse(d, None if d else f"invalid date '{text}'")
    for rx, order in ((_DMY_TXT, "dmy"), (_DMY_ORD, "dmy"), (_MDY_TXT, "mdy")):
        if m := rx.match(s):
            if order == "dmy":
                dd, mon, yy = m[1], m[2], m[3]
            else:
                mon, dd, yy = m[1], m[2], m[3]
            mo = _month(mon)
            if mo is None:
                return DateParse(None, f"unknown month '{mon}'")
            d = _mkdate(int(yy), mo, int(dd))
            return DateParse(d, None if d else f"invalid date '{text}'")
    return DateParse(None, f"unrecognised date '{text}'")


def parse_date(text: str) -> date | None:
    return parse_date_ex(text).value


_TIME = re.compile(
    r"(?:\bum\s+|\bat\s+|,\s*|\s|^)(\d{1,2})(?::(\d{2})(?::(\d{2}))?|\.(\d{2})(?=\s*(?:uhr|h)\b))\s*(am|pm|a\.m\.|p\.m\.)?\s*(?:uhr|h)?\b",
    re.I,
)
_ISO_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(Z|[+-]\d{2}:?\d{2})?$")
_TZ_WORDS = re.compile(r"\b(CEST|CET|MEZ|MESZ|UTC|GMT|Z)\b\s*$", re.I)


class DateTimeParse:
    __slots__ = ("value", "has_time", "problem", "notes")

    def __init__(self, value, has_time=False, problem=None, notes=()):
        self.value, self.has_time, self.problem, self.notes = value, has_time, problem, list(notes)


def parse_datetime_ex(text: str) -> DateTimeParse:
    """Parse '22 October 2026, 08:10', '22.10.2026 08:10 Uhr', '2026-10-22T08:10',
    '08:10, 22.10.2026'. Local times without an offset are Europe/Vienna; the
    resulting value is timezone-aware. A date without time gives a ``date``."""
    s = unicodedata.normalize("NFC", (text or "").strip()).strip(" .;")
    s = re.sub(r"\s+", " ", s)
    if not s:
        return DateTimeParse(None, problem="empty date/time")
    if m := _ISO_DT.match(s):
        try:
            naive = datetime.fromisoformat(f"{m[1]}T{m[2]}")
        except ValueError:
            return DateTimeParse(None, problem=f"invalid date/time '{text}'")
        if m[3]:
            off = m[3]
            if off == "Z":
                return DateTimeParse(naive.replace(tzinfo=timezone.utc), True)
            off = off.replace(":", "")
            sign = 1 if off[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
            return DateTimeParse(naive.replace(tzinfo=tz), True)
        return _localize(naive, text)
    tzword = None
    if m := _TZ_WORDS.search(s):
        tzword = m[1].upper()
        s = s[: m.start()].strip(" ,")
    tm = None
    for cand in _TIME.finditer(s):
        tm = cand          # last time-looking token wins ('22.10.2026, 08:10')
    if tm is None:
        dp = parse_date_ex(s)
        if dp.value is None:
            return DateTimeParse(None, problem=dp.problem)
        return DateTimeParse(dp.value, False)
    hh = int(tm[1])
    mm = int(tm[2] if tm[2] is not None else tm[4])
    ss = int(tm[3] or 0)
    ampm = (tm[5] or "").lower().replace(".", "")
    if ampm:
        if not 1 <= hh <= 12:
            return DateTimeParse(None, problem=f"invalid 12-hour time in '{text}'")
        hh = (hh % 12) + (12 if ampm == "pm" else 0)
    if hh > 23 or mm > 59 or ss > 59:
        return DateTimeParse(None, problem=f"invalid time in '{text}'")
    rest = (s[: tm.start()] + " " + s[tm.end():]).strip(" ,")
    rest = re.sub(r"\b(um|at)\s*$", "", rest).strip(" ,")
    dp = parse_date_ex(rest)
    if dp.value is None:
        return DateTimeParse(None, problem=dp.problem)
    naive = datetime(dp.value.year, dp.value.month, dp.value.day, hh, mm, ss)
    if tzword in ("UTC", "GMT", "Z"):
        return DateTimeParse(naive.replace(tzinfo=timezone.utc), True)
    res = _localize(naive, text)
    if tzword and res.value is not None:
        expect = {"CET": 1, "MEZ": 1, "CEST": 2, "MESZ": 2}[tzword]
        if res.value.utcoffset() != timedelta(hours=expect):
            res.notes.append(f"'{text}': {tzword} does not match Vienna local time; used {tzword}")
            res.value = naive.replace(tzinfo=timezone(timedelta(hours=expect)))
    return res


def _localize(naive: datetime, text: str) -> DateTimeParse:
    a = naive.replace(tzinfo=VIENNA, fold=0)
    b = naive.replace(tzinfo=VIENNA, fold=1)
    notes = []
    if a.utcoffset() != b.utcoffset():
        notes.append(f"'{text}' falls in the daylight-saving change hour; earlier offset used")
    # Non-existent local time (spring forward): round trip changes the wall time.
    rt = a.astimezone(timezone.utc).astimezone(VIENNA)
    if rt.replace(tzinfo=None) != naive:
        return DateTimeParse(None, problem=f"'{text}' does not exist in Vienna local time (DST gap)")
    return DateTimeParse(a, True, notes=notes)


def fhir_datetime(v) -> str:
    """FHIR dateTime: a date, or a full timestamp with seconds and offset."""
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return v.isoformat()


def fhir_instant(v: datetime) -> str:
    return v.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- persons

def _person_view(x) -> tuple[str, str | None]:
    """(full name, ISO birthDate) from a dict, a FHIR Patient, or an object with
    a ``patient`` attribute (Parsed)."""
    if x is None:
        return "", None
    if hasattr(x, "patient") and not isinstance(x, dict):
        x = x.patient or {}
    if not isinstance(x, dict):
        raise TypeError(f"cannot read a person from {type(x).__name__}")
    if x.get("resourceType") == "Patient":
        names = x.get("name") or []
        official = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
        full = official.get("text") or " ".join([*official.get("given", []), official.get("family", "")])
        bd = x.get("birthDate")
    else:
        full = x.get("name") or ""
        if not full:
            given = x.get("given") or x.get("given_name") or ""
            if isinstance(given, list):
                given = " ".join(given)
            full = f"{given} {x.get('family') or x.get('family_name') or ''}"
        bd = x.get("birthDate") or x.get("birth_date") or x.get("dob")
    if isinstance(bd, (date, datetime)):
        bd = (bd.date() if isinstance(bd, datetime) else bd).isoformat()
    elif isinstance(bd, str) and bd.strip():
        d = parse_date(bd)
        bd = d.isoformat() if d else None
    else:
        bd = None
    return full.strip(), bd


# =========================================================================== jury rules
# The patient and identity decisions implement tests/jury/RULES.md exactly:
#   names: NFKC, whitespace (incl. NBSP) collapsed, titles dropped, 'Family, Given' (one comma)
#   -> 'Given Family', casefold, German umlauts transliterated per ICAO 9303 (ä->ae, ö->oe, ü->ue,
#   ß->ss), all other diacritics stripped (María = Maria), tokens compared IN ORDER.

SAME, NEAR, DIFFERENT = "same", "near", "different"
AMBIGUOUS = "ambiguous"
_ICAO = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe", "œ": "oe", "å": "aa"}
_CONFUSABLE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "һ": "h", "ӏ": "l", "ο": "o", "α": "a", "ε": "e", "ρ": "p", "ν": "v", "ι": "i", "κ": "k",
    "τ": "t", "ı": "i",
})
_WS_ALL = re.compile(r"[\s\u00a0\u2000-\u200b\u202f\u205f\u3000]+")


def fold_name(s: str) -> str:
    """casefold + ICAO umlaut transliteration + strip remaining diacritics (Märia -> maeria, María -> maria)."""
    s = unicodedata.normalize("NFC", s).casefold()
    return _strip_marks("".join(_ICAO.get(c, c) for c in s))


def _is_caps(t: str) -> bool:
    letters = [c for c in t if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def caps_family_last(tokens: list[str]) -> list[str]:
    """Austrian LIS convention: the family name in CAPITALS ('BEISPIEL Maria'). If some but not all
    tokens are all-caps and they form one contiguous block at the start or the end, that block is
    the family name and goes last ('BEISPIEL Maria' -> 'Maria BEISPIEL')."""
    flags = [_is_caps(t) for t in tokens]
    if len(tokens) < 2 or all(flags) or not any(flags):
        return tokens
    k = flags.index(True)
    j = k
    while j < len(flags) and flags[j]:
        j += 1
    if any(flags[j:]):
        return tokens                                   # not one contiguous block
    if k == 0:
        return tokens[j:] + tokens[:j]
    if j == len(tokens):
        return tokens
    return tokens


_MIDDLE_TITLES = {"dr", "drin", "prof", "profin", "univprof", "mag", "maga", "dipling", "frau", "herr", "mmag", "ddr"}


def rule_tokens(name: str) -> list[str]:
    """Ordered, folded name tokens (titles removed, 'Family, Given' -> 'Given Family').
    Hyphenated names stay one token ('beispiel-huber')."""
    if not name:
        return []
    s = _WS_ALL.sub(" ", unicodedata.normalize("NFKC", name)).strip()
    parts = [x.strip() for x in s.split(",")]
    while len(parts) > 1 and parts[-1] and all(_is_title(t) for t in _raw_tokens(parts[-1])):
        parts.pop()                                      # 'Eva Brandl, MSc'
    if len(parts) == 2 and parts[0] and parts[1]:
        s = f"{parts[1]} {parts[0]}"
    else:
        s = " ".join(parts)
    _, core, _ = _strip_titles(_raw_tokens(s))
    if len(parts) < 2:
        core = caps_family_last(core)
    out = []
    for t in core:
        # a title left in the middle ('Maria Dr. Beispiel') is dropped only when unambiguous (dotted or a
        # pure title word), so real name tokens such as 'Ma' or 'Di' survive
        if _is_title(t) and (t.endswith(".") or _tkey(t) in _MIDDLE_TITLES):
            continue
        f = fold_name(t).strip(".()\"'’`")
        if f:
            out.append(f)
    return out


def has_foreign_script(s: str) -> bool:
    """A letter that is not Latin (homoglyph attack or encoding damage)."""
    return any(c.isalpha() and "LATIN" not in unicodedata.name(c, "") for c in s or "")


def _lev(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def compare_names(doc_name: str, requested_name: str) -> str:
    """Patient name: SAME (identical ordered tokens, Latin script only), NEAR (confusable skeleton
    equal, same tokens in another order, a token superset such as an extra given name or a
    double/hyphenated surname, or Levenshtein <= 2 such as 'Marie' or 'Märia'), else DIFFERENT."""
    a, b = rule_tokens(doc_name), rule_tokens(requested_name)
    if not a or not b:
        return DIFFERENT
    if a == b and not has_foreign_script(doc_name):
        return SAME
    sa = [t.translate(_CONFUSABLE) for t in a]
    sb = [t.translate(_CONFUSABLE) for t in b]
    pa = {p for t in sa for p in re.split(r"[-‐‑]", t) if p}
    pb = {p for t in sb for p in re.split(r"[-‐‑]", t) if p}
    if (sa == sb or sorted(sa) == sorted(sb) or pa < pb or pb < pa
            or _lev(" ".join(sa), " ".join(sb)) <= 2):
        return NEAR
    return DIFFERENT


def parse_dob(text: str | None):
    """ISO date string, AMBIGUOUS (slash date with day and month both <= 12 and unequal), or None."""
    if not text or not str(text).strip():
        return None
    dp = parse_date_ex(str(text))
    if dp.value:
        return dp.value.isoformat()
    if dp.problem and "ambiguous (day/month" in dp.problem:
        return AMBIGUOUS
    return None


def compare_dobs(doc_dob, requested_dob: str) -> str:
    """``doc_dob``: an ISO date, AMBIGUOUS or None (unparseable). SAME only if identical. NEAR:
    day/month swapped, exactly one digit different, one adjacent transposition, ambiguous or
    unparseable. Everything else DIFFERENT."""
    if doc_dob == requested_dob:
        return SAME
    if doc_dob is None or doc_dob == AMBIGUOUS:
        return NEAR
    a, b = doc_dob.replace("-", ""), requested_dob.replace("-", "")
    if (doc_dob[:4] == requested_dob[:4] and doc_dob[5:7] == requested_dob[8:10]
            and doc_dob[8:10] == requested_dob[5:7]):
        return NEAR
    if len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1:
        return NEAR
    if any(a == b[:i] + b[i + 1] + b[i] + b[i + 2:] for i in range(len(b) - 1)):
        return NEAR
    return DIFFERENT


def _parts(tokens: list[str]) -> list[str]:
    return [p for t in tokens for p in re.split(r"[-‐‑]", t) if p]


def identity_matches(requested_name: str, given, family: str, *, patient=None, birth_date=None) -> bool:
    """eID person vs the named recipient: identity rule v2 (binary, no fuzziness).

    Names are normalised as for patients (titles dropped, casefold, ICAO umlauts, other diacritics
    stripped); hyphens split tokens. With R = the requested name's parts, G = the eID given-name
    parts and F = the eID family-name parts:
      1. the requested name has a given name and a family name: F and R minus F are both non-empty;
      2. family exact: every part of F occurs in R (so eID 'Brandl-Huber' vs requested 'Brandl' fails);
      3. given subset: the requested parts other than F are all eID given parts (eID 'Eva Maria' or
         'Eva-Maria' matches requested 'Eva'; requested 'Eva Maria' vs eID 'Eva' fails);
      4. typed order does not matter ('Brandl Eva' works): token multisets are compared.
      5. patient as recipient: if ``patient`` is given and the requested name is SAME as the patient's
         name (``compare_names``), ``birth_date`` (the eID birth date) must also equal the patient's
         birth date. This stops a same-named relative.
    ``requested_name`` is free text; ``given`` str or list; ``patient`` a FHIR Patient or
    {name, birthDate}; ``birth_date`` ISO or any accepted date format.
    """
    from collections import Counter
    if isinstance(given, list):
        given = " ".join(given)
    R = Counter(_parts(rule_tokens(requested_name or "")))
    G = Counter(_parts(rule_tokens(given or "")))
    F = Counter(_parts(rule_tokens(family or "")))
    if not F or not G or not R:
        return False
    if any(R[f] < n for f, n in F.items()):                 # 2. family exact
        return False
    rest = R - F
    if not rest:                                            # 1. a given name must be requested
        return False
    if any(G[t] < n for t, n in rest.items()):              # 3. requested given names subset of the eID's
        return False
    if patient is not None:                                 # 5. the patient herself as recipient
        pn, pd = _person_view(patient)
        if pn and compare_names(requested_name, pn) == SAME:
            bd = parse_dob(birth_date) if birth_date else None
            if not pd or bd != pd:
                return False
    return True


def same_person(a, b) -> bool:
    """True only if the names are SAME and the birth dates are SAME under ``compare_names`` /
    ``compare_dobs`` (RULES.md). 'Beispiel Maria' (no comma) is NOT the same as 'Maria Beispiel';
    'Beispiel, Maria' is. 'Müller' = 'Mueller', but 'Muller' != 'Müller'. A day/month swap is not
    a match. Missing data is never a match."""
    na, da = _person_view(a)
    nb, db = _person_view(b)
    if not da or not db or not na or not nb:
        return False
    return compare_dobs(da, db) == SAME and compare_names(na, nb) == SAME


def person_mismatch_reason(a, b) -> str | None:
    """Reason why ``same_person(a, b)`` is False (may contain both values; for logs, not alerts)."""
    na, da = _person_view(a)
    nb, db = _person_view(b)
    reasons = []
    if not da or not db:
        reasons.append("birth date missing or not a complete unambiguous date")
    elif da != db:
        reasons.append(f"birth date {da} != {db}")
    if compare_names(na, nb) != SAME:
        reasons.append(f"name '{na}' != '{nb}'")
    return "; ".join(reasons) or None


MATCH, MISMATCH = "match", "mismatch"


def name_match_level(expected, actual, patient=None) -> str:
    """eID binding (RULES.md rule 1, binary): 'match' if ``identity_matches`` else 'mismatch'.

    ``expected``: the named recipient as text ('Dr. Eva Brandl').
    ``actual``: the eID identity {given_name|given, family_name|family} (or 'Given Family' text,
    last token = family)."""
    if isinstance(actual, dict):
        given = actual.get("given_name") or actual.get("given") or ""
        family = actual.get("family_name") or actual.get("family") or ""
    else:
        g, f, _ = split_name(actual or "")
        given, family = " ".join(g), f or ""
    if isinstance(expected, dict):
        expected = expected.get("name") or " ".join(
            x for x in (expected.get("given_name") or expected.get("given"), expected.get("family_name")
                        or expected.get("family")) if x)
    bd = (actual.get("birthDate") or actual.get("birth_date") or actual.get("birthdate")) if isinstance(actual, dict) else None
    return MATCH if identity_matches(expected or "", given, family, patient=patient, birth_date=bd) else MISMATCH


def patient_check(document_patient, requested_patient, other: str = "the requested patient") -> list[str]:
    """Privacy-safe reasons why the document's patient is not the requested patient
    ([] if ``same_person``). Never contains the other patient's name or birth date."""
    if same_person(document_patient, requested_patient):
        return []
    na, da = _person_view(document_patient)
    nb, db = _person_view(requested_patient)
    out = []
    if not na:
        out.append("the document has no patient name")
    elif compare_names(na, nb) != SAME:
        out.append(f"the patient name differs from {other}")
    if not da:
        out.append("the document has no complete unambiguous birth date")
    elif da != db:
        out.append(f"the birth date differs from {other}")
    return out or ["the document's patient does not match the requested patient"]


# --------------------------------------------------------------------------- deprecated

def names_match(a: str, b: str) -> bool:
    """DEPRECATED (old, loose semantics: order-free, ä = a). Use ``compare_names(a, b) == SAME``
    for patients, ``identity_matches`` for the eID, or ``loose_name_equal`` for display/dedup."""
    import warnings
    warnings.warn("fhirmap.names_match is deprecated: loose semantics; use compare_names / identity_matches "
                  "(RULES.md) or loose_name_equal for display", DeprecationWarning, stacklevel=2)
    return loose_name_equal(a, b)


def normalize_name(name: str) -> str:
    """DEPRECATED (folded ä -> a). Use ``' '.join(rule_tokens(name))`` (ICAO: ä -> ae, RULES.md)."""
    import warnings
    warnings.warn("fhirmap.normalize_name is deprecated; use rule_tokens (RULES.md)", DeprecationWarning, stacklevel=2)
    return _loose_normal(name)

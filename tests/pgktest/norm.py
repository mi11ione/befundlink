"""Independent reference normalisation for the jury rules (tests/jury/RULES.md).

Deliberately does NOT import service/ or fhirmap/: the tests must be a second opinion, not a copy.

Comparisons return one of SAME / NEAR / DIFFERENT:
  SAME       -> may be filed
  NEAR       -> HOLD (decision by Dr. Hofer)
  DIFFERENT  -> BLOCK (alert)
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

SAME, NEAR, DIFFERENT = "same", "near", "different"

# salutations, academic titles, post-nominals (compared casefolded, dots removed)
TITLES = {
    "dr", "drin", "ddr", "prof", "profin", "univprof", "univ", "mag", "maga", "mmag", "di", "dipl", "dipling", "ing",
    "med", "dent", "vet", "rer", "nat", "phil", "techn", "habil", "priv", "doz", "oa", "prim", "msc", "bsc", "mba",
    "phd", "md", "frau", "herr", "fr", "hr", "mr", "mrs", "ms",
    # Austrian titles of office (Amtstitel / Berufstitel)
    "ostr", "studr", "osr", "sr", "kr", "komr", "regr", "regrat", "oekr", "ökr", "hofr", "medr", "vetr", "obr",
}
# ICAO Doc 9303 transliteration: German umlauts are distinct letters (ae/oe/ue), not accented vowels.
ICAO = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe", "œ": "oe", "å": "aa"}
# Latin look-alikes from other scripts (subset of Unicode TR39 confusables relevant for names)
CONFUSABLE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "һ": "h", "ӏ": "l", "ο": "o", "α": "a", "ε": "e", "ρ": "p", "ν": "v", "ι": "i", "κ": "k", "τ": "t", "ı": "i",
})
_WS = re.compile(r"[\s  -   　]+")


def strip_marks(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def fold(s: str) -> str:
    """casefold + ICAO umlaut transliteration + strip remaining diacritics (María -> maria, Märia -> maeria)."""
    s = unicodedata.normalize("NFC", s).casefold()
    return strip_marks("".join(ICAO.get(c, c) for c in s))


def name_tokens(name: str) -> list[str]:
    """Ordered, folded tokens; titles removed; 'Family, Given' turned into 'Given Family'."""
    if not name:
        return []
    s = _WS.sub(" ", unicodedata.normalize("NFKC", name)).strip()
    if s.count(",") == 1:
        fam, giv = (p.strip() for p in s.split(","))
        if fam and giv:
            s = f"{giv} {fam}"
    out = []
    for tok in s.replace(",", " ").split(" "):
        key = fold(tok).replace(".", "").replace("-", "")
        if not key or key in TITLES or re.fullmatch(r"(dr|mag|di|prof)(in|a)?", key):
            continue
        out.append(fold(tok).strip(".()\"'"))
    return [t for t in out if t]


def has_foreign_script(s: str) -> bool:
    """True if a letter is not Latin (homoglyph attack or encoding damage)."""
    return any(c.isalpha() and "LATIN" not in unicodedata.name(c, "") for c in s)


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def compare_names(doc: str, req: str) -> str:
    """Patient name: exact on normalised ordered tokens; plausible slips are NEAR; the rest DIFFERENT."""
    a, b = name_tokens(doc), name_tokens(req)
    if not a or not b:
        return DIFFERENT
    if a == b and not has_foreign_script(doc):
        return SAME
    sa = [t.translate(CONFUSABLE) for t in a]
    sb = [t.translate(CONFUSABLE) for t in b]
    parts = lambda ts: [p for t in ts for p in re.split(r"[-‐‑]", t) if p]
    pa, pb = parts(sa), parts(sb)
    if (sa == sb                                   # homoglyph (skeleton equal)
            or sorted(sa) == sorted(sb)            # reversed order without comma
            or set(pb) < set(pa) or set(pa) < set(pb)  # extra given name / double or hyphenated surname
            or levenshtein(" ".join(sa), " ".join(sb)) <= 2):  # typo, umlaut variant
        return NEAR
    return DIFFERENT


def identity_matches(requested: str, given: str, family: str, birth_date: str | None = None,
                     patient: tuple[str, str] | None = None) -> bool:
    """eID vs named recipient, identity rule v2 (RULES.md rule 1). Binary, no fuzziness.
    Tokens normalised as for patient names, hyphens split. R requested, G eID given, F eID family:
    F and G non-empty; F is contained in R (family exact); R - F is non-empty (a given name was requested)
    and contained in G (the eID may add given names); order free. If the requested name is SAME as the
    patient's name, the eID birth date must equal the patient's (a same-named relative is not the patient)."""
    from collections import Counter
    split = lambda s: Counter(p for t in name_tokens(s) for p in re.split(r"[-‐‑]", t) if p)
    R, G, F = split(requested), split(given), split(family)
    if not (R and G and F) or any(R[f] < n for f, n in F.items()):
        return False
    rest = R - F
    if not rest or any(G[t] < n for t, n in rest.items()):
        return False
    if patient and compare_names(requested, patient[0]) == SAME:
        return bool(birth_date) and parse_dob(birth_date) == patient[1]
    return True


# ------------------------------------------------------------------ dates
MONTHS = {
    "januar": 1, "jänner": 1, "jaenner": 1, "january": 1, "jan": 1, "jän": 1, "februar": 2, "feber": 2,
    "february": 2, "feb": 2, "märz": 3, "maerz": 3, "march": 3, "mär": 3, "mar": 3, "april": 4, "apr": 4,
    "mai": 5, "may": 5, "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "oktober": 10, "october": 10, "okt": 10, "oct": 10, "november": 11,
    "nov": 11, "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}
AMBIGUOUS = "ambiguous"


def parse_dob(s: str | None) -> str | None:
    """ISO date, AMBIGUOUS (numeric with '/', both parts <= 12, unequal), or None (unparseable)."""
    if not s:
        return None
    s = _WS.sub(" ", s).strip(" .,;*_()")
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
            y, mo, d = map(int, m.groups())
        elif m := re.fullmatch(r"(\d{1,2}) ?\. ?(\d{1,2}) ?\. ?(\d{4})", s):
            d, mo, y = map(int, m.groups())
        elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s):
            a, b, y = map(int, m.groups())
            if a <= 12 and b <= 12 and a != b:
                return AMBIGUOUS
            d, mo = (a, b) if a > 12 else (b, a)
        elif m := re.fullmatch(r"(\d{1,2})\.? ([^\W\d_]+)\.? (\d{4})", s):
            d, mo, y = int(m.group(1)), MONTHS.get(m.group(2).casefold()), int(m.group(3))
        elif m := re.fullmatch(r"([^\W\d_]+)\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})", s):
            d, mo, y = int(m.group(2)), MONTHS.get(m.group(1).casefold()), int(m.group(3))
        else:
            return None
        return date(y, mo, d).isoformat() if mo else None
    except ValueError:
        return None


def compare_dobs(doc: str | None, req: str) -> str:
    """doc is the parse_dob() result. Swaps, single-digit slips, adjacent transpositions, ambiguity -> NEAR."""
    if doc == req:
        return SAME
    if doc is None:
        return NEAR  # unparseable is a hold, never a match
    if doc == AMBIGUOUS:
        return NEAR
    a, b = doc.replace("-", ""), req.replace("-", "")
    if (doc[:4] == req[:4] and doc[5:7] == req[8:10] and doc[8:10] == req[5:7]) \
            or sum(x != y for x, y in zip(a, b)) == 1 \
            or any(a == b[:i] + b[i + 1] + b[i] + b[i + 2:] for i in range(7)):
        return NEAR
    return DIFFERENT


def dob_variants(iso: str) -> list[str]:
    """Every rendering of a birth date the PII scanner looks for."""
    d = date.fromisoformat(iso)
    dd, mm, y, yy = f"{d.day:02d}", f"{d.month:02d}", str(d.year), f"{d.year % 100:02d}"
    en = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"][d.month - 1]
    de = [["Jänner", "Januar"], ["Februar", "Feber"], ["März", "Maerz"], ["April"], ["Mai"], ["Juni"], ["Juli"],
          ["August"], ["September"], ["Oktober"], ["November"], ["Dezember"]][d.month - 1]
    out = {f"{y}-{mm}-{dd}", f"{y}{mm}{dd}", f"{dd}{mm}{y}", f"{dd}{mm}{yy}", f"{y}/{mm}/{dd}"}
    for day, mon in ((dd, mm), (str(d.day), str(d.month))):
        for sep in (".", "/", "-", ". "):
            out |= {f"{day}{sep}{mon}{sep}{y}", f"{day}{sep}{mon}{sep}{yy}", f"{mon}{sep}{day}{sep}{y}"}
    for mon in [en, en[:3], *de, de[0][:3]]:
        out |= {f"{d.day} {mon} {y}", f"{d.day}. {mon} {y}", f"{dd}. {mon} {y}", f"{mon} {d.day}, {y}",
                f"{mon} {d.day} {y}", f"{dd} {mon} {y}"}
    return sorted(out, key=len, reverse=True)


# ------------------------------------------------------------------ LOINC
def loinc_ok(code: str) -> bool:
    """LOINC mod-10 check digit (LOINC Users' Guide, 'Calculating Mod 10 check digits')."""
    m = re.fullmatch(r"(\d{1,7})-(\d)", code or "")
    if not m:
        return False
    num = m.group(1)[::-1]
    odd = str(int(num[0::2][::-1]) * 2)
    even = num[1::2][::-1]
    total = sum(int(c) for c in even + odd)
    return (10 - total % 10) % 10 == int(m.group(2))

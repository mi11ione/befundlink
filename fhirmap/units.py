"""Lab unit strings -> UCUM codes.

Only units that map to a UCUM expression we can vouch for get a UCUM code.
Anything else is kept as free text (Quantity.unit) without system/code and a
warning is raised, so we never file an invalid UCUM code.
"""
from __future__ import annotations

import re
import unicodedata

UCUM = "http://unitsofmeasure.org"

# Explicit table, keys compared after `_norm` (see below). Values are UCUM codes.
_TABLE = {
    # cell counts
    "10^9/l": "10*9/L", "10e9/l": "10*9/L", "x10^9/l": "10*9/L", "x 10^9/l": "10*9/L", "10*9/l": "10*9/L",
    "giga/l": "10*9/L", "gpt/l": "10*9/L", "/nl": "/nL", "10^3/ul": "10*3/uL",
    "10e3/ul": "10*3/uL", "x10^3/ul": "10*3/uL", "tsd/ul": "10*3/uL", "tsd./ul": "10*3/uL", "k/ul": "10*3/uL",
    "10^12/l": "10*12/L", "10e12/l": "10*12/L", "x10^12/l": "10*12/L", "t/l": "10*12/L", "tpt/l": "10*12/L",
    "10^6/ul": "10*6/uL", "mio/ul": "10*6/uL", "/ul": "/uL", "/pl": "/pL",
    # time
    "s": "s", "sec": "s", "sec.": "s", "sek.": "s", "sek": "s", "sek.": "s", "sekunden": "s", "seconds": "s", "min": "min",
    "h": "h", "std": "h", "mm/h": "mm/h", "mm/1h": "mm/h",
    # ratio-like / dimensionless
    "%": "%", "ratio": "{ratio}", "inr": "{INR}", "index": "{index}",
    # mass, substance and enzymes
    "u/l": "U/L", "iu/l": "[IU]/L", "ie/l": "[IU]/L", "mu/l": "mU/L", "miu/l": "m[IU]/L", "mie/l": "m[IU]/L",
    "uiu/ml": "u[IU]/mL", "uie/ml": "u[IU]/mL", "iu/ml": "[IU]/mL", "ie/ml": "[IU]/mL", "ku/l": "kU/L",
    "u/ml": "U/mL", "mu/ml": "mU/mL", "kiu/l": "k[IU]/L", "kie/l": "k[IU]/L",
    "ml/min/1.73m2": "mL/min/{1.73_m2}", "ml/min/1,73m2": "mL/min/{1.73_m2}",
    "ml/min/1.73 m2": "mL/min/{1.73_m2}", "ml/min/1,73 m2": "mL/min/{1.73_m2}",
    "mmhg": "mm[Hg]", "°c": "Cel", "grad c": "Cel", "ph": "[pH]",
    "mosm/kg": "mosm/kg", "mosmol/kg": "mosm/kg", "meq/l": "meq/L", "mval/l": "meq/L",
    "fl": "fL", "pg": "pg", "ng": "ng", "kg": "kg", "g": "g", "mg": "mg", "ug": "ug",
    "kg/m2": "kg/m2", "cm": "cm", "mm": "mm", "m": "m", "l": "L", "ml": "mL",
    "ml/min": "mL/min", "g/24h": "g/(24.h)", "mg/24h": "mg/(24.h)", "mmol/24h": "mmol/(24.h)",
    "mg/g": "mg/g", "mg/mmol": "mg/mmol", "mg/g krea": "mg/g{creat}",
    "mg/g crea": "mg/g{creat}", "mg/mmol krea": "mg/mmol{creat}",
    "titer": "{titer}",
    # fibrinogen-equivalent units: the code is the mass concentration, the printed unit keeps 'FEU'
    "mg/l feu": "mg/L", "ug/ml feu": "ug/mL", "ng/ml feu": "ng/mL", "ug/l feu": "ug/L",
}

# Building blocks for the conservative grammar check of composite units such as
# 'mmol/L', 'umol/L', 'ng/mL', 'pg/mL', 'g/dL', 'mg/dL'.
_PREFIX = {"", "k", "d", "c", "m", "u", "n", "p", "f"}
_ATOMS = {"g", "L", "mol", "U", "[IU]", "eq", "osm", "kat"}
_TIME_DEN = {"s", "min", "h", "d"}


def _norm(u: str) -> str:
    u = unicodedata.normalize("NFKC", u).strip()
    u = u.replace("µ", "u").replace("μ", "u").replace("²", "2").replace("³", "3")
    u = u.replace("×", "x").replace("·", ".")
    u = re.sub(r"\s*/\s*", "/", u)
    u = re.sub(r"10\s*\^\s*", "10^", u)
    u = re.sub(r"\s+", " ", u)
    return u.casefold()


def _simple(term: str) -> str | None:
    """'mmol' -> 'mmol', 'ul' -> 'uL', 'dl' -> 'dL'. Returns canonical term or None."""
    for atom in sorted(_ATOMS, key=len, reverse=True):
        a = atom.casefold()
        if term.endswith(a):
            p = term[: -len(a)]
            if p in _PREFIX:
                return p + atom
    return None


def to_ucum(unit: str | None) -> tuple[str | None, str | None]:
    """Return (ucum_code, problem). ucum_code is None if the unit is empty or
    cannot be mapped safely; problem explains why for non-empty units."""
    if unit is None or not unit.strip():
        return None, None
    sup = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
    unit = re.sub(r"10([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", lambda m: "10^" + m[1].translate(sup), unit)   # 10⁹/l -> 10^9/l
    raw = unicodedata.normalize("NFKC", unit).strip()
    # Austrian/German count notation: 'G/l' = giga (10^9) per litre, 'T/l' = tera per litre.
    # Case matters: 'g/l' is gram per litre. (Copied verbatim, 'G/L' is valid UCUM but
    # means gauss per litre, which the validator happily accepts.) The LOINC unit-fit
    # check in the parser holds the answer if a G/L value is not a count.
    if re.fullmatch(r"(G|Gpt|Giga)\s*/\s*[lL]", raw):
        return "10*9/L", None
    if re.fullmatch(r"(T|Tpt|Tera)\s*/\s*[lL]", raw):
        return "10*12/L", None
    if re.fullmatch(r"(M|Mpt)\s*/\s*[lL]", raw):
        return "10*6/L", None
    n = _norm(unit)
    if n in _TABLE:
        return _TABLE[n], None
    # composite 'a/b' with known prefix+atom on both sides (e.g. mmol/l, ng/ml, g/dl)
    m = re.fullmatch(r"([a-z\[\]]+)/([a-z\[\]]+)", n)
    if m:
        num, den = _simple(m[1]), _simple(m[2])
        if num and den:
            return f"{num}/{den}", None
        if num and m[2] in _TIME_DEN:
            return f"{num}/{m[2]}", None
    if m := re.fullmatch(r"x?\s?10\^(\d{1,2})/([a-z]+)", n):
        den = _simple(m[2])
        if den:
            return f"10*{m[1]}/{den}", None
    single = _simple(n)
    if single:
        return single, None
    return None, f"unit '{unit}' has no safe UCUM mapping; kept as text only"

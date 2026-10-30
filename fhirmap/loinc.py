"""LOINC facts (official display, example UCUM unit, existence) with an offline cache.

The cache ``data/loinc.json`` is committed and pre-filled with common lab codes
via tx.fhir.org ``CodeSystem/$lookup`` (``python -m fhirmap.loinc --refresh``).
A code missing from the cache is looked up online once (timeout 4 s) unless
``PGK_LOINC_ONLINE=0``; offline, the display is simply omitted (code only is valid).
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TX = os.environ.get("PGK_TX", "https://tx.fhir.org/r4")
CACHE_PATH = Path(__file__).resolve().parent / "data" / "loinc.json"
_lock = threading.Lock()
_cache: dict | None = None


def _norm_entry(e: dict | None) -> dict | None:
    """LOINC's EXAMPLE_UCUM_UNITS can be a list ('ug/L;ng/mL'). Store ``example_ucum`` as ONE
    valid UCUM code (the first) and the full list in ``example_ucum_list``."""
    if not e or e.get("not_found"):
        return e
    raw = e.get("example_ucum")
    units = e.get("example_ucum_list") or [u.strip() for u in re.split(r"[;,]", raw or "") if u.strip()]
    e["example_ucum"] = units[0] if units else None
    e["example_ucum_list"] = units
    return e


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
        for v in _cache.values():
            _norm_entry(v)
    return _cache


def _save() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(_load().items())), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def fetch(code: str, timeout: float = 4.0) -> dict | None:
    """Online $lookup. Returns {display, example_ucum, inactive, status, version} or
    {'not_found': True}; None on network failure (never cached)."""
    q = urllib.parse.urlencode({"system": "http://loinc.org", "code": code})
    req = urllib.request.Request(f"{TX}/CodeSystem/$lookup?{q}", headers={"Accept": "application/fhir+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            d = json.load(e)
        except ValueError:
            return None
        txt = json.dumps(d)
        if "Unknown_Code" in txt or "not-found" in txt:
            return {"not_found": True}
        return None
    except (OSError, ValueError):
        return None
    if d.get("resourceType") != "Parameters":
        return None
    out = {"display": None, "example_ucum": None, "inactive": False, "status": None, "version": None}
    for p in d.get("parameter", []):
        n = p.get("name")
        if n == "display":
            out["display"] = p.get("valueString")
        elif n == "version":
            out["version"] = p.get("valueString")
        elif n == "property":
            parts = {x["name"]: x for x in p.get("part", [])}
            pc = parts.get("code", {}).get("valueCode")
            val = parts.get("value", {})
            if pc == "EXAMPLE_UCUM_UNITS":
                out["example_ucum"] = val.get("valueString")
            elif pc == "inactive":
                out["inactive"] = bool(val.get("valueBoolean"))
            elif pc == "STATUS":
                out["status"] = val.get("valueString")
    return _norm_entry(out) if out["display"] else None


def info(code: str, online: bool | None = None) -> dict | None:
    """Cached LOINC facts for ``code`` or None if unknown offline."""
    if online is None:
        online = os.environ.get("PGK_LOINC_ONLINE", "1") != "0"
    c = _load()
    if code in c:
        return c[code]
    if not online:
        return None
    got = fetch(code)
    if got is not None:
        with _lock:
            c[code] = got
            try:
                _save()
            except OSError:
                pass
    return got


def display(code: str, online: bool | None = None) -> str | None:
    i = info(code, online)
    return i.get("display") if i and not i.get("not_found") else None


# ---------------------------------------------------------------- unit dimensions

def _term_dim(t: str) -> str | None:
    t = re.sub(r"\{[^}]*\}", "", t).strip("()")
    if t in ("", "1") or re.fullmatch(r"10\*\d+", t):
        return "count"
    m = re.fullmatch(r"(?:10\*\d+\.)?([kdcmunpf]?)(g|mol|eq|osm|L|l|U|\[IU\]|kat|s|min|h|d|m|%)(\d?)", t)
    if not m:
        return None
    atom = m[2]
    return {"g": "mass", "mol": "substance", "eq": "substance", "osm": "substance", "L": "volume",
            "l": "volume", "U": "activity", "[IU]": "activity", "kat": "activity", "s": "time", "min": "time",
            "h": "time", "d": "time", "m": "length" + (m[3] or ""), "%": "fraction"}[atom]


def dimension(ucum: str | None) -> tuple | None:
    """Coarse dimension of a UCUM code, e.g. '10*9/L' -> ('count','volume'),
    'g/L' -> ('mass','volume'), 's' -> ('time',), '{INR}' -> ('1',). None if unknown."""
    if not ucum:
        return None
    u = ucum.strip()
    if re.fullmatch(r"(\{[^}]*\}|1|\[pH\])", u):
        return ("1",)
    if u == "%":
        return ("fraction",)
    parts = u.split("/")
    dims = []
    for i, part in enumerate(parts[:2]):
        if i == 0 and part == "":
            dims.append("count")
            continue
        d = _term_dim(part)
        if d is None:
            return None
        dims.append(d)
    return tuple(dims)


def dimensionless_example(example: str | None) -> str | None:
    """LOINC's example unit if it is dimensionless ('{INR}', '{ratio}', '1', '[pH]'), else None."""
    for e in re.split(r"[;,]", example or ""):
        e = e.strip()
        if re.fullmatch(r"\{[^}]*\}|1|\[pH\]", e):
            return e
    return None


def unit_fits(code: str, ucum: str | None, online: bool | None = None) -> tuple[bool | None, str | None]:
    """Does our UCUM unit have the same dimension as LOINC's example unit?
    (True|False|None for 'cannot tell', example unit)."""
    i = info(code, online)
    if not i or i.get("not_found") or not i.get("example_ucum"):
        return None, None
    ours = dimension(ucum)
    if ours is None:
        return None, i["example_ucum"]
    examples = i.get("example_ucum_list") or [i["example_ucum"]]
    known = [dimension(e) for e in examples]
    known = [k for k in known if k is not None]
    if not known:
        return None, i["example_ucum"]
    return ours in known, ";".join(examples)


# ---------------------------------------------------------------- cache refresh

COMMON = """
11502-2 18842-5 34105-7 11506-3 28570-0 34133-9 60591-5 11526-1 18748-4 34117-2
6301-6 34714-6 46418-0 5902-2 5894-1 14979-9 3173-2 3255-7 48065-7 48066-5 3243-3 27811-9 3184-9
718-7 4544-3 789-8 6690-2 26464-8 777-3 26515-7 787-2 785-6 786-4 788-0 32623-1 770-8 751-8 736-9
731-0 5905-5 742-7 713-8 711-2 706-2 704-7 4537-7 30341-2 4679-7 17849-1
2160-0 38483-4 14682-9 3094-0 22664-7 3091-6 2823-3 6298-4 2951-2 2947-0 2075-0 17861-6 2000-8 2777-1
2601-3 2345-7 2339-0 15074-8 4548-4 17856-6 59261-8 1988-5 30522-7 33959-8 1742-6 1920-8 2324-2 6768-6
1975-2 1968-7 2885-2 1751-7 2532-0 2157-6 2093-3 2085-9 13457-7 18262-6 2571-8 3016-3 3024-7 3051-0
2276-4 2498-4 2502-3 3034-6 2132-9 2284-8 1798-8 3040-3 3084-1 33914-3 62238-1 98979-8 48642-3 48643-1
6598-7 10839-9 89579-7 67151-1 30934-4 33762-6 2028-9 11558-4 2019-8 2703-7 1960-4 2524-7 2093-3 14647-2
14646-4 14927-8 22748-8 39469-2 14933-6 2276-4 1834-1 1759-0 13969-1 2889-4 5811-5 5803-2 5770-3 20454-5
""".split()


def refresh(codes: list[str] | None = None, workers: int = 16) -> dict:
    """Look up ``codes`` (default: COMMON + cached) concurrently and rewrite the cache."""
    from concurrent.futures import ThreadPoolExecutor
    c = _load()
    todo = sorted(set(codes or COMMON) | set(c))
    with ThreadPoolExecutor(workers) as ex:
        results = dict(zip(todo, ex.map(fetch, todo)))
    stats = {"ok": 0, "not_found": 0, "failed": 0}
    with _lock:
        for code, r in results.items():
            if r is None:
                stats["failed"] += 1
                continue
            c[code] = r
            stats["not_found" if r.get("not_found") else "ok"] += 1
        _save()
    return stats


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        print(refresh([a for a in sys.argv[1:] if a != "--refresh"] or None))
    else:
        for a in sys.argv[1:]:
            print(a, info(a))

"""PII scanner for everything that leaves the clinic (email, SMS, link URLs) and for privacy-at-rest checks.

A finding is (severity, category, needle, where). severity 'violation' fails the metric; 'notice' is
reported (e.g. a bare year, or a generic word such as 'report' that names no document type).
"""
from __future__ import annotations

import re
import unicodedata

from .norm import dob_variants, fold, name_tokens

DOC_TYPE = ["lab", "labor", "laboratory", "laborbefund", "laborwerte", "befund", "blut", "blood", "blutbild",
            "gerinnung", "clotting", "coagulation", "inr", "aptt", "quick", "thrombozyten", "platelets", "fibrinogen",
            "reha", "rehab", "rehabilitation", "entlassung", "entlassungsbericht", "entlassungsbrief", "discharge",
            "arztbrief", "kurzbrief", "diagnose", "diagnosis", "medikation", "medication", "röntgen", "x-ray",
            "hüft-tep", "hip", "eingriff", "procedure", "operation", "op-vorbereitung"]
WEAK = ["report", "result", "results", "bericht", "ergebnis", "werte", "values", "test", "probe", "sample", "brief",
        "dokument", "document"]
ORG_TOKENS = ["labor", "reha", "kanalstraße", "kanalstrasse", "kanal", "waldblick", "seeblick", "zentrallabor",
              "nordstadt", "labor mitte"]


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def _word(needle: str, hay: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(needle)}(?![\w])", hay) is not None


def scan(text: str, where: str, patient: dict, *, extra_names: list[str] = (), extra_dobs: list[str] = (),
         doc_type: bool = True, orgs: list[str] = (), ids: list[str] = ()) -> list[tuple[str, str, str, str]]:
    """patient: {"name","birthDate","id","mrn"}. extra_*: other patients' identities (privacy checks)."""
    out = []
    low, folded = _norm(text), fold(text)
    for nm in [patient.get("name", ""), *extra_names]:
        for tok in name_tokens(nm):
            if len(tok) >= 2 and (_word(tok, folded) or _word(tok, low)):
                out.append(("violation", "name", tok, where))
    for iso in [patient.get("birthDate"), *extra_dobs]:
        if not iso:
            continue
        for v in dob_variants(iso):
            if _norm(v) in low and not re.search(rf"\d{re.escape(_norm(v))}|{re.escape(_norm(v))}\d", low):
                out.append(("violation", "birth date", v, where))
                break
        if re.search(rf"(?<!\d){iso[:4]}(?!\d)", low):
            out.append(("notice", "birth year", iso[:4], where))
    for i in [patient.get("id"), patient.get("mrn"), *ids]:
        if i and _norm(i) in low:
            out.append(("violation", "patient identifier", i, where))
    if doc_type:
        for w in DOC_TYPE:
            if _word(w, low):
                out.append(("violation", "document type", w, where))
        for w in WEAK:
            if _word(w, low):
                out.append(("notice", "generic word", w, where))
    for o in [*ORG_TOKENS, *(_norm(x) for x in orgs)]:
        if o and _word(o, low):
            out.append(("violation", "recipient organisation", o, where))
    return out


def violations(findings) -> list:
    return [f for f in findings if f[0] == "violation"]

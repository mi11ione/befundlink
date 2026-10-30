"""Known recipients for the short request form (!labor Brandl maria-beispiel).

Sources, never guessed:
- the register fixture service/data/registers.json (Ärzteliste + GDA-Index staff): name, role, organisation
- earlier requests sent from this room (bot state): name, role, organisation and the contact that was used,
  remembered only for sends WITHOUT an address warning (a confusable typo is never reused)
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import REPO
from .parse import channel_for, classify_contact, name_tokens, norm

REGISTERS = REPO / "service" / "data" / "registers.json"


@dataclass
class Candidate:
    name: str
    role: str | None
    org: str | None
    channel: str | None = None
    address: str | None = None
    source: str = "register"


def fold(s: str) -> str:
    s = norm(s).casefold().replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _key(name: str, org: str | None) -> tuple[str, str]:
    return (" ".join(fold(t) for t in name_tokens(name)), fold(org or ""))


def load_register(path: Path = REGISTERS) -> list[Candidate]:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    out: dict[tuple, Candidate] = {}
    for org in (d.get("gda_index") or {}).get("entries") or []:
        for st in org.get("staff") or []:
            if st.get("name"):
                c = Candidate(st["name"], st.get("role"), org.get("name"))
                raw = st.get("contact") or st.get("email") or st.get("mobile") or st.get("phone")
                if raw:          # only if the register lists one; still goes through the address check
                    cc = classify_contact(str(raw))
                    if channel_for(cc):
                        c.channel, c.address = channel_for(cc), cc.value
                out[_key(st["name"], org.get("name"))] = c
    for e in (d.get("aerzteliste") or {}).get("entries") or []:
        k = _key(e.get("name") or "", e.get("org"))
        if e.get("name") and k not in out:
            out[k] = Candidate(e["name"], e.get("specialty"), e.get("org"))
    clinic = fold(((d.get("clinic") or {}).get("name")) or "")
    members = {fold(m.get("name") or "") for m in (d.get("network") or {}).get("members") or []}
    # requests go OUTSIDE the network: drop the clinic's own staff / network members
    return [c for c in out.values() if fold(c.org or "") != clinic and fold(c.name) not in members]


def candidates(history: list[dict], register: list[Candidate]) -> list[Candidate]:
    """One entry per person+organisation; the room's history adds role and contact."""
    out: dict[tuple, Candidate] = {}
    for c in register:
        out[_key(c.name, c.org)] = Candidate(c.name, c.role, c.org, c.channel, c.address, source="register")
    for h in history:
        k = _key(h.get("name") or "", h.get("org"))
        prev = out.get(k)
        out[k] = Candidate(h["name"], h.get("role") or (prev.role if prev else None), h.get("org"),
                           h.get("channel") or (prev.channel if prev else None),
                           h.get("address") or (prev.address if prev else None), source="history")
    return list(out.values())


def match(query: list[str], cands: list[Candidate]) -> list[Candidate]:
    """Family name must match a query word; every other query word must match a given name or an
    organisation word (prefix of >= 3 letters). No fuzzy matching."""
    q = [fold(t) for t in query if fold(t)]
    if not q:
        return []
    hits = []
    for c in cands:
        toks = [fold(t) for t in name_tokens(c.name)]
        if not toks:
            continue
        family, given = toks[-1], toks[:-1]
        org_words = [w for w in fold(c.org or "").replace("-", " ").split() if w]
        if family not in q:
            continue
        rest = list(q)
        rest.remove(family)
        ok = all(any(g == r or (len(r) >= 3 and g.startswith(r)) for g in given + org_words) for r in rest)
        if ok:
            hits.append(c)
    return hits

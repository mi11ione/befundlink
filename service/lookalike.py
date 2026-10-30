"""Look-alike / confusable address check, run BEFORE a request is sent.

Checks an email domain against known domains (recipient org's directory entries + domains
previously verified by eID + all other directory domains):
  * confusable skeleton equality (Unicode confusables, I/l/1, O/0, rn/m, vv/w, cl/d, Cyrillic/Greek)
  * Damerau-Levenshtein distance <= 2
  * mixed-script labels / non-ASCII / punycode
  * domain unknown for an org that has known domains
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict

# single-char confusables -> latin skeleton char. Applied BEFORE lowercasing ('I' must map to 'l').
_CHAR_MAP: dict[str, str] = {
    "I": "l", "1": "l", "|": "l", "ı": "l", "ł": "l", "Ɩ": "l", "ǀ": "l", "ⅼ": "l", "Ӏ": "l", "і": "l", "ι": "l",
    "i": "l",  # i vs l after case-folding (labor-kanaI == labor-kanai in DNS)
    "O": "o", "0": "o", "о": "o", "ο": "o", "О": "o", "Ο": "o", "ө": "o", "º": "o",
    "а": "a", "А": "a", "α": "a", "@": "a",
    "е": "e", "Е": "e", "ε": "e", "ё": "e",
    "р": "p", "Р": "p", "ρ": "p",
    "с": "c", "С": "c", "ϲ": "c",
    "х": "x", "Х": "x", "χ": "x",
    "у": "y", "У": "y", "γ": "y",
    "к": "k", "К": "k", "κ": "k",
    "м": "m", "М": "m",
    "н": "h", "Н": "h", "һ": "h",
    "т": "t", "Т": "t", "τ": "t",
    "в": "b", "В": "b", "β": "b", "Ь": "b",
    "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ν": "v", "υ": "u", "ս": "u",
    "5": "s", "$": "s", "3": "e", "4": "a", "7": "t", "8": "b", "9": "g", "6": "b", "2": "z",
    "_": "-", "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "．": ".", "。": ".", "｡": ".",
}
# multi-char look-alikes (applied after single-char mapping and lowercasing)
_SEQ_MAP: list[tuple[str, str]] = [("rn", "m"), ("vv", "w"), ("cl", "d"), ("nn", "m")]


def skeleton(s: str) -> str:
    out = []
    for ch in s:
        if ch in _CHAR_MAP:
            out.append(_CHAR_MAP[ch])
            continue
        # strip diacritics (é -> e), fold compatibility forms (fullwidth -> ascii)
        decomp = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in decomp if not unicodedata.combining(c))
        for c in base.lower():
            out.append(_CHAR_MAP.get(c, c))
    sk = "".join(out).lower().replace("ß", "ss")
    for a, b in _SEQ_MAP:
        sk = sk.replace(a, b)
    return sk


def damerau(a: str, b: str) -> int:
    """Optimal string alignment distance (Levenshtein + adjacent transposition)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def scripts_of(s: str) -> set[str]:
    out = set()
    for ch in s:
        if ch.isalpha():
            try:
                out.add(unicodedata.name(ch).split(" ")[0])
            except ValueError:
                out.add("UNKNOWN")
    return out


def _decode_idna(domain: str) -> str:
    labels = []
    for label in domain.split("."):
        if label.lower().startswith("xn--"):
            try:
                labels.append(label.encode("ascii").decode("idna"))
                continue
            except (UnicodeError, ValueError):
                pass
        labels.append(label)
    return ".".join(labels)


@dataclass
class Warning_:
    code: str  # confusable | lookalike | uppercase_in_domain | mixed_script | non_ascii | unknown_domain_for_org | malformed
    message: str
    domain: str
    similar_to: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def check_local_part(local: str, recipient_name: str | None) -> list[Warning_]:
    """'eva.brand1@…' for 'Dr. Eva Brandl': a local-part token that LOOKS like a name token (confusables 1/l, 0/o, rn/m)
    or is one edit away from it, without being equal, is a likely typo -> warn before sending."""
    if not recipient_name:
        return []
    from fhirmap.identity import rule_tokens
    names = [t for t in rule_tokens(recipient_name) if len(t) >= 3]
    out = []
    for tok in [t for t in re.split(r"[._+\-]+", local.casefold()) if len(t) >= 3]:
        if tok in names:
            continue
        for n in names:
            sk_eq = skeleton(tok) == skeleton(n)
            if sk_eq or (len(n) >= 4 and damerau(tok, n) == 1):
                kind = "verwechselbare Zeichen (z. B. 1/l, 0/o, rn/m)" if sk_eq else "ein Zeichen Unterschied"
                fixed = local.replace(tok, n) if sk_eq else None
                out.append(Warning_("local_part_lookalike",
                                    f"Adresse '{local}@…': '{tok}' ähnelt dem Namen '{n}' ({kind}) – Tippfehler?",
                                    local, fixed))
                break
    return out


def check_email(address: str, known_domains: list[str], org_domains: list[str] | None = None,
                org_name: str | None = None, recipient_name: str | None = None) -> list[Warning_]:
    """Return warnings for an email address. `known_domains` must include `org_domains`."""
    warnings: list[Warning_] = []
    address = address.strip()
    if address.count("@") != 1 or not address.split("@")[1] or not address.split("@")[0]:
        return [Warning_("malformed", f"Adresse '{address}' ist keine gültige E-Mail-Adresse.", address)]
    local, raw_domain = address.split("@")
    warnings.extend(check_local_part(local, recipient_name))
    domain_disp = _decode_idna(raw_domain)
    domain_dns = domain_disp.lower()

    # mixed script per label and over the whole address
    for part in [local, *domain_disp.split(".")]:
        sc = scripts_of(part)
        if len(sc) > 1:
            warnings.append(Warning_("mixed_script",
                                     f"'{part}' mischt Schriftsysteme ({', '.join(sorted(sc))}) – typisches Merkmal gefälschter Adressen.",
                                     domain_disp))
    if any(c.isupper() for c in domain_disp):
        warnings.append(Warning_("uppercase_in_domain",
                                 f"Großbuchstabe in der Domain '{domain_disp}'. Domains unterscheiden nicht zwischen Groß- und "
                                 f"Kleinschreibung: das ist die Domain '{domain_dns}' – typischer Tippfehler oder Täuschung (I statt l).",
                                 domain_disp, None))  # no suggestion here: the lower-cased typo is not a correction
    if any(ord(c) > 127 for c in domain_disp) or "xn--" in raw_domain.lower():
        warnings.append(Warning_("non_ascii", f"Domain '{domain_disp}' enthält Nicht-ASCII-Zeichen (IDN).", domain_disp))

    known = sorted({d.lower() for d in known_domains if d})
    if domain_dns not in known:
        sk = skeleton(domain_disp)
        for kd in known:
            if skeleton(kd) == sk:
                warnings.append(Warning_("confusable",
                                         f"Domain '{domain_disp}' sieht aus wie die bekannte Domain '{kd}' (verwechselbare Zeichen, z. B. I/l, 0/O, rn/m).",
                                         domain_disp, kd))
                continue
            dist = damerau(domain_dns, kd)
            if dist <= 2:
                warnings.append(Warning_("lookalike",
                                         f"Domain '{domain_disp}' unterscheidet sich nur in {dist} Zeichen von der bekannten Domain '{kd}'.",
                                         domain_disp, kd))
        org_known = sorted({d.lower() for d in (org_domains or [])})
        if org_known and not any(w.code in ("confusable", "lookalike") for w in warnings):
            warnings.append(Warning_("unknown_domain_for_org",
                                     f"Domain '{domain_disp}' ist für {org_name or 'diese Organisation'} nicht bekannt (bekannt: {', '.join(org_known)}).",
                                     domain_disp, org_known[0]))
    order = {"confusable": 0, "lookalike": 1}  # a correction suggestion (similar_to) first
    return sorted(warnings, key=lambda w: order.get(w.code, 2))


DOMAIN_CODES = ("confusable", "lookalike", "uppercase_in_domain", "mixed_script", "non_ascii", "unknown_domain_for_org")


def _char_de(c: str) -> str:
    if c.isdigit():
        return f"Ziffer {c}"
    if c.isalpha():
        return f"{'großes' if c.isupper() else 'kleines'} {c}"
    return f"Zeichen '{c}'"


def merge_domain_warnings(warnings: list[dict]) -> list[dict]:
    """One typo, one warning: when a domain looks like a known one (confusable / lookalike), every other
    warning about the SAME typed domain (uppercase, mixed script, IDN, unknown for the org) is folded into one text:
    'Adresse prüfen: labor-kanaI.example (großes I) statt der bekannten labor-kanal.example (kleines l) – Tippfehler
    oder Täuschung?'. Every code stays in the list (acknowledged_warnings is unchanged); the folded ones carry
    merged_into = the leading code and are not shown on their own."""
    lead = next((w for w in warnings if w["code"] in ("confusable", "lookalike") and w.get("similar_to")), None)
    if lead is None:
        return warnings
    typed, known = lead["domain"], lead["similar_to"]
    typed_bits, known_bits = [], []
    if len(typed) == len(known):
        for a, b in zip(typed, known):
            if a != b:
                typed_bits.append(_char_de(a))
                known_bits.append(_char_de(b))
    typed_txt = f"{typed} ({', '.join(typed_bits)})" if typed_bits else typed
    known_txt = f"{known} ({', '.join(known_bits)})" if known_bits else known
    merged = f"Adresse prüfen: {typed_txt} statt der bekannten {known_txt} – Tippfehler oder Täuschung?"
    out = []
    for w in warnings:
        if w is lead:
            out.append({**w, "message": merged, "detail": w["message"]})
        elif w["code"] in DOMAIN_CODES and w.get("domain") == typed:
            out.append({**w, "merged_into": lead["code"]})
        else:
            out.append(w)
    return out

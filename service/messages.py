"""Outgoing email/SMS text. Contains NO patient name, NO birth date, NO document type.

`pii_findings` is the runtime guard: every outgoing message is scanned before it is sent and
sending is refused if anything is found (defence in depth on top of the fixed template).
"""
from __future__ import annotations

import re
from datetime import date
from html import escape, unescape

import fhirmap
from fhirmap.identity import name_tokens


def parse_date(s: str | None) -> str | None:
    d = fhirmap.parse_date(s) if s else None
    return d.isoformat() if d else None

# Curated lexicon: words that would tell a reader what kind of document is asked for. Always blocked.
DOC_TYPE_WORDS = [
    "labor", "lab", "laborbefund", "laborwerte", "befund", "befunde", "reha", "rehab", "rehabilitation",
    "reha-entlassungsbericht", "entlassung", "entlassungsbericht", "entlassungsbrief", "discharge", "report", "result",
    "results", "gerinnung", "blut", "blood", "inr", "aptt", "brief", "arztbrief", "bericht", "diagnose", "diagnosis",
    "werte", "values", "test", "probe", "sample", "medikation", "medication",
]
# Words of the recipient's role/organisation are blocked too (an organisation name such as "Labor Kanalstraße" would
# reveal the document type), except these: function words, short tokens, and anything our own template says.
STOP_WORDS = {
    # German
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines", "einem", "einen", "und", "oder",
    "für", "fuer", "von", "vom", "zu", "zur", "zum", "im", "in", "am", "an", "auf", "aus", "bei", "mit", "nach",
    "über", "unter", "gmbh", "mbh", "kg", "og", "ag", "eu", "dr", "prof", "mag", "frau", "herr",
    # English
    "the", "a", "an", "of", "for", "and", "or", "to", "at", "in", "on", "by", "with", "from", "ltd", "inc", "mr",
    "mrs", "ms",
}
MIN_CONTEXT_WORD = 4
_MONTHS_DE = [["Jänner", "Januar"], ["Februar", "Feber"], ["März"], ["April"], ["Mai"], ["Juni"], ["Juli"],
              ["August"], ["September"], ["Oktober"], ["November"], ["Dezember"]]
_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]


def dob_variants(iso: str) -> list[str]:
    """Birth date in every format we guard against (ISO, dotted, slashed both orders, compact, spelled out)."""
    d = date.fromisoformat(iso)
    dd, mm, yyyy, yy = f"{d.day:02d}", f"{d.month:02d}", f"{d.year}", f"{d.year % 100:02d}"
    en = _MONTHS_EN[d.month - 1]
    out = {
        f"{yyyy}-{mm}-{dd}", f"{dd}.{mm}.{yyyy}", f"{d.day}.{d.month}.{yyyy}", f"{dd}/{mm}/{yyyy}", f"{mm}/{dd}/{yyyy}",
        f"{dd}-{mm}-{yyyy}", f"{dd}.{mm}.{yy}", f"{yyyy}{mm}{dd}", f"{dd}{mm}{yyyy}", f"{dd}{mm}{yy}",
        f"{d.day} {en} {yyyy}", f"{en} {d.day}, {yyyy}", f"{d.day} {en[:3]} {yyyy}",
    }
    for de in _MONTHS_DE[d.month - 1]:
        out |= {f"{d.day}. {de} {yyyy}", f"{d.day} {de} {yyyy}"}
    return sorted(out)


def pii_findings(text: str, patient_name: str | None, birth_date: str | None, extra_words: list[str] = ()) -> list[str]:
    """Return what was found (empty list = clean). Word-boundary for words, substring for dates."""
    found = []
    low = text.casefold()
    for tok in name_tokens(patient_name or ""):
        if len(tok) >= 2 and re.search(rf"(?<!\w){re.escape(tok.casefold())}(?!\w)", low):
            found.append(f"patient name token '{tok}'")
    iso = parse_date(birth_date) if birth_date else None
    if iso:
        for v in dob_variants(iso):
            if v.casefold() in low:
                found.append(f"birth date '{v}'")
    for w in [*DOC_TYPE_WORDS, *extra_words]:
        if re.search(rf"(?<!\w){re.escape(w.casefold())}(?!\w)", low):
            found.append(f"document type word '{w}'")
    return found


def _fmt_date(iso_dt: str) -> str:
    d = date.fromisoformat(iso_dt[:10])
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def _email_parts(clinic: str, link: str, expires_at: str, ref_code: str, requester: str | None, department: str | None,
                 phone: str | None, host: str | None) -> dict:
    """The e-mail's sentences, shared by the plain-text and the HTML part (same content in both)."""
    who = requester or clinic
    dept = f" ({department}, {clinic})" if department else f" ({clinic})"
    trust = " ".join(x for x in (f"Echt nur, wenn der Link auf {host} führt." if host else "",
                                 f"Rückfragen: {phone} (Referenz angeben)." if phone else "") if x)
    return {
        "clinic": clinic, "link": link, "valid": _fmt_date(expires_at), "ref": ref_code,
        "subject": f"{clinic} – Anfrage von {who} (Ref. {ref_code})",
        "ask": f"{who}{dept} bittet Sie persönlich um eine Rückmeldung.",
        "intro": "Worum es geht, sehen Sie erst nach der Anmeldung.",
        "steps": ["Link öffnen", "Mit ID Austria anmelden", "Dokument hochladen"],
        "trust": trust,
        "only": "Nur Sie können die Anfrage öffnen. Nicht für Sie bestimmt? Bitte löschen.",
        "en": f"English: {who} asks you for a reply. Open the link and sign in with ID Austria; only you can open it.",
    }


def build_email(clinic: str, link: str, expires_at: str, ref_code: str, requester: str | None = None,
                department: str | None = None, phone: str | None = None, host: str | None = None) -> tuple[str, str]:
    """Names the REQUESTING doctor and clinic and gives a callback number (anti-phishing). Never the recipient
    (she can be the patient herself), never the patient, a birth date or the document type."""
    p = _email_parts(clinic, link, expires_at, ref_code, requester, department, phone, host)
    body = (
        f"Guten Tag,\n\n"
        f"{p['ask']}\n{p['intro']}\n\n"
        f"{link}\n"
        f"Gültig bis {p['valid']} · Referenz {ref_code}\n\n"
        f"In etwa 2 Minuten:\n"
        + "".join(f"{i}. {s}\n" for i, s in enumerate(p["steps"], 1)) + "\n"
        + (f"{p['trust']}\n" if p["trust"] else "")
        + f"{p['only']}\n\n"
        f"{p['en']}\n"
    )
    return p["subject"], body


def build_email_html(clinic: str, link: str, expires_at: str, ref_code: str, requester: str | None = None,
                     department: str | None = None, phone: str | None = None, host: str | None = None) -> str:
    """The same message as `build_email`, as the HTML alternative part: table layout, inline styles, system fonts,
    no images, no external requests. Same content, same guard (see `html_scan_text`)."""
    p = {k: (v if isinstance(v, list) else escape(v or "")) for k, v in
         _email_parts(clinic, link, expires_at, ref_code, requester, department, phone, host).items()}
    sans = "'Helvetica Neue',Helvetica,Arial,sans-serif"
    t = f"font-family:{sans};color:#000000;"
    g = f"font-family:{sans};color:#595959;"
    cell = f"{t}font-size:16px;line-height:1.4;padding:12px 0;border-top:1px solid #d6d6d6;"
    steps = "".join(
        f'<tr><td valign="top" style="{cell}width:40px;font-weight:bold;">{i:02d}</td>'
        f'<td style="{cell}">{escape(s)}</td></tr>' for i, s in enumerate(p["steps"], 1))
    trust = f'<p style="margin:0 0 6px;{t}font-size:15px;line-height:1.5;">{p["trust"]}</p>' if p["trust"] else ""
    return (
        f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{p["subject"]}</title></head>'
        f'<body style="margin:0;padding:0;background:#ffffff;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">'
        f'<tr><td align="center" style="padding:24px 20px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;">'
        f'<tr><td style="{t}font-size:17px;font-weight:bold;padding:0 0 14px;border-bottom:1px solid #000000;">{p["clinic"]}</td></tr>'
        f'<tr><td style="padding:36px 0 0;">'
        f'<p style="margin:0 0 14px;{t}font-size:16px;line-height:1.5;">Guten Tag,</p>'
        f'<p style="margin:0 0 10px;{t}font-size:22px;line-height:1.3;font-weight:bold;">{p["ask"]}</p>'
        f'<p style="margin:0 0 28px;{g}font-size:16px;line-height:1.5;">{p["intro"]}</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#000000;"><tr>'
        f'<td style="padding:0;"><a href="{p["link"]}" style="display:block;padding:17px 20px;font-family:{sans};font-size:17px;'
        f'font-weight:bold;color:#ffffff;text-decoration:none;">Anfrage öffnen</a></td>'
        f'<td align="right" width="48" style="padding:0 20px 0 0;"><a href="{p["link"]}" style="font-family:{sans};font-size:20px;'
        f'color:#ffffff;text-decoration:none;">&#8594;</a></td></tr></table>'
        f'<p style="margin:12px 0 36px;{g}font-size:15px;line-height:1.5;">Gültig bis {p["valid"]} · Referenz {p["ref"]}</p>'
        f'<p style="margin:0 0 6px;{t}font-size:15px;font-weight:bold;">In etwa 2 Minuten:</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="border-bottom:1px solid #d6d6d6;margin:0 0 28px;">{steps}</table>'
        f'{trust}<p style="margin:0;{t}font-size:15px;line-height:1.5;">{p["only"]}</p>'
        f'<p style="margin:28px 0 0;{g}font-size:13px;line-height:1.5;" lang="en">{p["en"]}</p>'
        f'</td></tr></table></td></tr></table></body></html>'
    )


def html_scan_text(html: str) -> str:
    """What the PII guard scans in an HTML part: the visible text and every link target / alt / title, with the
    entities decoded. Tags and inline CSS are dropped (CSS words such as 'left' or 'table' are not content)."""
    attrs = " ".join(m.group(1) for m in re.finditer(r'(?i)\b(?:href|alt|title)="([^"]*)"', html))
    text = re.sub(r"(?is)<style\b.*?</style>", " ", html)
    text = re.sub(r"(?s)<[^>]*>", " ", text)
    return unescape(f"{text} {attrs}")


def build_sms(clinic: str, link: str, expires_at: str, ref_code: str, requester_short: str | None = None,
              department_short: str | None = None, phone: str | None = None) -> str:
    """One segment where possible; German only (the landing page is bilingual)."""
    who = f"{clinic}, {requester_short}" + (f" ({department_short})" if department_short else "") if requester_short else clinic
    tail = f" Rückfragen {phone}" if phone else ""
    return f"{who}: Anfrage an Sie, Ref {ref_code}. Anmeldung mit ID Austria: {link}{tail}"


def template_words() -> set[str]:
    """Every word of our own e-mail and SMS templates (rendered with neutral placeholders). A word the template
    itself always contains can never be a leak; blocking it would refuse every message."""
    args = ("X", "<LINK>", "2026-01-01", "KB-XXXXXX", "X", "X", "X", "X")
    subject, body = build_email(*args)
    html = html_scan_text(build_email_html(*args))
    sms = build_sms("X", "<LINK>", "2026-01-01", "KB-XXXXXX", "X", "X", "X")
    return {w.casefold() for w in re.findall(r"\w+", f"{subject}\n{body}\n{html}\n{sms}")}


def context_words(phrases: list[str | None], allowed: set[str] = frozenset()) -> list[str]:
    """Words of the recipient's role and organisation to block in the outgoing message, minus stop words, words
    shorter than MIN_CONTEXT_WORD, words of our own template and `allowed` (words that legitimately appear: the
    clinic's and the requester's own names and department)."""
    keep_out = STOP_WORDS | TEMPLATE_WORDS | {w.casefold() for w in allowed}
    words = []
    for phrase in phrases:
        for w in re.findall(r"\w+", phrase or ""):
            if len(w) >= MIN_CONTEXT_WORD and w.casefold() not in keep_out and w not in words:
                words.append(w)
    return words


TEMPLATE_WORDS = template_words()  # computed once, from the templates as shipped

"""Message formatting: (plain text, HTML) pairs for Matrix. Generic: everything shown comes
from the doctor's command or the service response, nothing is hard-coded per person."""
from __future__ import annotations

import re
from datetime import datetime
from html import escape as _attr

from .parse import KIND_LABEL, Draft

Msg = tuple[str, str | None]

# URL / e-mail / domain-looking tokens. Element linkifies these in formatted bodies except inside
# <code>, so text that may come from a document or the service is wrapped instead of linked.
_LINKY = re.compile(r"(?:https?://|www\.)[^\s<>\"']*[^\s<>\"'.,:;!?)]|[^\s<>\"']+@[^\s<>\"']+\.[A-Za-z]{2,}|"
                    r"\b[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b(?:/\S*)?")
_ROOM = re.compile(r"@(?=room\b)", re.I)


def no_room_ping(s: str) -> str:
    """'@room' -> '@\u2060room' (word joiner) so it never pings the whole room."""
    return _ROOM.sub("@\u2060", s)


def e(s) -> str:
    """Escape a text node for HTML: no auto-links (wrapped in <code>), no @room."""
    out, last = [], 0
    s = str(s)
    for m in _LINKY.finditer(s):
        out.append(_attr(s[last:m.start()]))
        out.append(f"<code>{_attr(m.group(0))}</code>")
        last = m.end()
    out.append(_attr(s[last:]))
    return no_room_ping("".join(out))


_MXID = re.compile(r"@[a-z0-9._=\-/+]+:[A-Za-z0-9.\-]+(?::\d+)?")
_ISO = re.compile(r"(?<![/=\w-])(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?)(?![\w/-])")


def mxids_in(*texts) -> set[str]:
    return {m.group(0) for t in texts if t for m in _MXID.finditer(str(t))}


# An MXID after e(): its dotted parts are wrapped in <code> ('@<code>lena.hofer</code>:<code>klinikum.example</code>').
_MXID_HTML = re.compile(r"@(?:<code>)?([a-z0-9._=\-/+]+)(?:</code>)?:(?:<code>)?([A-Za-z0-9.\-]+(?::\d+)?)(?:</code>)?")


def humanize(s: str | None, names: dict[str, str] | None = None) -> str | None:
    """Service text for people: '@lena.hofer:klinikum.example' -> 'Dr. Lena Hofer', '2026-11-06' -> '06.11.2026'."""
    if not s:
        return s
    if names:
        def _html(m):
            n = names.get(f"@{m.group(1)}:{m.group(2)}")
            return _attr(n) if n else m.group(0)
        s = _MXID_HTML.sub(_html, s) if "<code>" in s else s
        s = _MXID.sub(lambda m: names.get(m.group(0), m.group(0)), s)
    return _ISO.sub(lambda m: human_time(m.group(1)), s)


def text_html(text: str) -> str:
    """HTML for a plain message (so clients render it without linkifying)."""
    return "<br/>".join(e(line) for line in text.split("\n"))

CHANNEL_LABEL = {"de": {"email": "E-Mail", "sms": "SMS"}, "en": {"email": "email", "sms": "SMS"}}


def _kind(kind: str | None, lang: str = "de") -> str:
    return KIND_LABEL[lang].get(kind or "", kind or "?")


def _ul(items) -> str:
    items = list(items)          # a generator is always truthy: never render an empty <ul></ul>
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>" if items else ""


# ---------------------------------------------------------------- card typography (Element's HTML subset only)
# One bold headline, the content (a table or a short list), one quiet line, one line of links.
# Hierarchy by weight and size only; red solely for blocked / wrong-patient / tamper states.

RED = "#E30613"
SEP = " · "


def _red(html: str) -> str:
    return f'<font data-mx-color="{RED}">{html}</font>'


_SIM = [(re.compile(r"\s*\(ID Austria\s*[–-]\s*Simulation( geprüft)?\)"), lambda m: " (ID Austria" + (m.group(1) or "") + ")"),
        (re.compile(r"ID[ -]Austria\s*[–-]\s*Simulation"), lambda m: "ID Austria"),
        (re.compile(r"(ID[ -]Austria)\s*\(Simulation\)"), lambda m: m.group(1))]


def nosim(s):
    """Cards say 'ID Austria'; that the eID is simulated is stated on the eID page and in the FHIR record."""
    if not s:
        return s
    for rx, f in _SIM:
        s = rx.sub(f, s)
    return s


def _cards(fn):
    """Every card leaves fmt without '(Simulation)'."""
    import functools

    @functools.wraps(fn)
    def w(*a, **k):
        t, h = fn(*a, **k)
        return nosim(t), nosim(h)
    return w


def mark_diff(a: str, b: str, red: bool = False) -> str:
    """HTML of `a` with the characters that differ from `b` in bold (red when the difference blocks a send)."""
    import difflib
    out = []
    if len(a) == len(b):                # same shape (dates, domains): compare position by position
        ops, i = [], 0
        while i < len(a):
            j = i
            same = a[i] == b[i]
            while j < len(a) and (a[j] == b[j]) == same:
                j += 1
            ops.append(("equal" if same else "replace", i, j, i, j))
            i = j
    else:
        ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    for op, a1, a2, _b1, _b2 in ops:
        seg = no_room_ping(_attr(a[a1:a2]))
        if op == "equal" or not seg:
            out.append(seg)
        else:
            out.append(_red(f"<b>{seg}</b>") if red else f"<b>{seg}</b>")
    return "".join(out)


def _headline(state: str, *parts, red: bool = False) -> str:
    """'<b>Abgelegt</b> · Laborbefund · Maria Beispiel · 12.03.1961': state · document · patient · birth date.
    Parts escaped, empty parts dropped; the state in red only for blocked / wrong-patient / tamper."""
    m = f"<b>{e(state)}</b>"
    return f"<p>{_red(m) if red else m}" + "".join(SEP + e(x) for x in parts if x) + "</p>"


def head_text(state: str, *parts) -> str:
    return SEP.join([state] + [str(x) for x in parts if x])


def _quiet(*lines) -> str:
    """The quiet colophon: each argument one line of already-escaped HTML items, small type, one paragraph."""
    out = [SEP.join(x for x in items if x) for items in lines]
    out = [x for x in out if x]
    return "<p>" + "<br/>".join(f"<sub>{x}</sub>" for x in out) + "</p>" if out else ""


def _table(head: list[str], rows: list[list[str]]) -> str:
    """Rows hold escaped HTML cells."""
    if not rows:
        return ""
    return ("<table><thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")


def _link_items(pairs) -> list[str]:
    return [f'<a href="{_attr(u)}">{_attr(k)}</a>' for k, u in pairs if u]


def _dob_part(dob) -> str | None:
    return date_de(dob) if dob else None


_TITLES = re.compile(r"(?i)\b(?:prim|univ\.?-?prof|prof|priv\.?-?doz|doz|dr|med|mag|dipl\.?-?ing|ing|msc|bsc|phd|mba|oä|oa|fä|fa)\b\.?")


def _family(name) -> str:
    """'Prim. Dr. Johanna Leitner, Ärztliche Direktorin' -> 'leitner' (last word of the name part, titles dropped)."""
    import unicodedata
    s = re.split(r",| – | - | am \d| \(", str(name or ""))[0]
    s = _TITLES.sub(" ", s)
    words = [w for w in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", s) if len(w) > 1]
    if not words:
        return ""
    w = unicodedata.normalize("NFKD", words[-1]).encode("ascii", "ignore").decode()
    return w.casefold()


def same_person(a, b) -> bool:
    """Loose: same family name ('Dr. Eva Brandl, lab physician' ~ 'Eva Brandl (ID Austria – Simulation)')."""
    fa, fb = _family(a), _family(b)
    return bool(fa) and fa == fb


def mentions_person(text, name) -> bool:
    """Does free text ('KH Beispielstadt, Int. Abt., OÄ Dr. Hofer') name this person (by family name)?"""
    f = _family(name)
    if not f or not text:
        return False
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().casefold()
    return re.search(rf"\b{re.escape(f)}\b", t) is not None


def person_name(v) -> str | None:
    """Author / sender as written in a document -> the name only ('Dr. Eva Brandl, lab physician' -> 'Dr. Eva Brandl')."""
    if isinstance(v, dict):
        v = v.get("name")
    if not v:
        return None
    s = re.split(r",| – | — | am \d", str(v).strip())[0].strip()
    return s or None


# ---------------------------------------------------------------- warnings

def warning_text(w) -> str:
    if isinstance(w, dict):
        return str(w.get("message") or w.get("text") or w.get("detail") or w.get("code") or w)
    return str(w)


def warning_code(w) -> str:
    return str(w.get("code") or "warning") if isinstance(w, dict) else "warning"


def warning_suggestion(w, address: str | None = None) -> str | None:
    """Suggested corrected address. Accepts {"suggestion": addr} or the service's {"similar_to": domain}."""
    if not isinstance(w, dict):
        return None
    s = w.get("suggestion") or w.get("suggested") or w.get("did_you_mean")
    if isinstance(s, list):
        s = s[0] if s else None
    if s:
        return str(s)
    dom = w.get("similar_to")
    if dom and address and "@" in address:
        return f"{address.rsplit('@', 1)[0]}@{dom}"
    return None


def is_address_warning(w) -> bool:
    if not isinstance(w, dict):
        return True
    return w.get("field") != "patient_id" and not str(w.get("code") or "").startswith("register")


# ---------------------------------------------------------------- shared helpers

STATUS_DE = {
    "sent": "gesendet", "opened": "Link geöffnet", "needs_confirmation": "wartet auf Ihre Bestätigung",
    "blocked_identity": "fremder Zugriff abgewehrt, Link offen", "identity_verified": "Identität geprüft",
    "held_review": "angehalten – Entscheidung nötig", "held_technical": "angehalten – technisch",
    "filed": "abgelegt", "rejected": "verworfen", "cancelled": "storniert", "locked": "Link gesperrt",
    "send_failed": "Senden fehlgeschlagen", "no_result": "kein Befund vorhanden", "created": "erstellt",
    "dry_run": "Prüfung", "expired": "abgelaufen",
}


def status_de(s) -> str:
    return STATUS_DE.get(str(s or ""), str(s or "?"))


def primary_id(*cands) -> str | None:
    """One id for Dr. Hofer: the KB- reference if known, else a req- id, else an answer number."""
    from .parse import valid_id
    ids = [str(c) for c in cands if valid_id(c)]
    return (next((i for i in ids if i.startswith("KB-")), None) or next((i for i in ids if i.startswith("req-")), None)
            or (ids[0] if ids else None))


def date_de(v) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(v or ""))
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else str(v or "")


def human_time(v) -> str:
    """ISO date/time -> Vienna local '22.10.2026, 08:10' (date only -> '22.10.2026'); other text unchanged."""
    s = str(v or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return date_de(s)
    t = _ts(s) if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", s) else None
    if not t:
        return s
    if t.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo
            t = t.astimezone(ZoneInfo("Europe/Vienna"))
        except Exception:
            pass
    return t.strftime("%d.%m.%Y, %H:%M")


def patient_label(pat, pid: str | None = None) -> str | None:
    """{name, birthDate} | 'Maria Beispiel' -> 'Maria Beispiel, geb. 12.03.1961'."""
    if isinstance(pat, dict):
        name, dob = pat.get("name"), pat.get("birthDate") or pat.get("birth_date")
    else:
        name, dob = pat, None
    if not name:
        return None
    return f"{name}" + (f", geb. {date_de(dob)}" if dob else "")


def _char_word(c: str, dative: bool = False) -> str:
    if c.isdigit():
        return f"Ziffer {c}"
    if ord(c) > 127:
        import unicodedata
        nm = unicodedata.name(c, "?").title()
        return f"Sonderzeichen '{c}' ({nm}, U+{ord(c):04X})"
    if c.isalpha():
        if c.isupper():
            return f"{'großem' if dative else 'großes'} {c}"
        return f"{'kleinem' if dative else 'kleines'} {c}"
    return f"'{c}'"


def confusable_words(typed: str, known: str) -> str | None:
    """'labor-kanaI.example' vs 'labor-kanal.example' -> 'großes I statt kleinem l (labor-kanaI → labor-kanal)'.
    Spells out what Element's sans-serif font hides."""
    import difflib
    if not typed or not known or typed == known:
        return None
    parts = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, typed, known, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        ta, kb = typed[a1:a2], known[b1:b2]
        if len(ta) == 1 and len(kb) == 1:
            parts.append(f"{_char_word(ta)} statt {_char_word(kb, dative=True)}")
        elif not kb:
            parts.append(f"zusätzlich '{ta}'")
        elif not ta:
            parts.append(f"'{kb}' fehlt")
        else:
            parts.append(f"'{ta}' statt '{kb}'")
    return "; ".join(parts) + f" ({typed} → {known})" if parts else None


# ---------------------------------------------------------------- confirm card (single pre-flight step)

def register_lines(reg) -> list[tuple[bool, str]]:
    """Service register result -> [(ok, line)]. Accepts str | dict | list | {"results": [...]}."""
    if reg is None or reg == "" or reg == []:
        return []
    if isinstance(reg, dict) and isinstance(reg.get("results"), list):
        reg = reg["results"]
    if not isinstance(reg, list):
        reg = [reg]
    out = []
    for r in reg:
        if isinstance(r, str):
            out.append((not r.startswith("⚠"), r))
            continue
        if not isinstance(r, dict):
            continue
        ok = r.get("ok", r.get("found", True)) is not False
        txt = r.get("text") or r.get("message")
        if not txt:
            src, name, det = r.get("source") or "Register", r.get("name") or "", r.get("detail") or r.get("specialty")
            txt = (f"{src}: {name}{', ' + det if det else ''} ✓" if ok
                   else f"{src}: {name} nicht gefunden – Rolle nur laut Anfrage")
        out.append((ok, str(txt)))
    return out


def draft_rows(d: Draft, lang: str = "de", patient=None) -> list[tuple[str, str]]:
    L = lang == "en"
    pl = patient_label(patient)
    if pl:
        pid = f"{pl} ({d.patient_id})"
    else:
        pid = d.patient_id or "–"
    if d.patient_from_name:
        pid += " (derived from name)" if L else " (aus Name abgeleitet)"
    rows = [
        ("Patient", pid),
        ("Document" if L else "Dokument", _kind(d.doc_kind, lang)),
        ("Recipient" if L else "Empfänger", " · ".join(x for x in (d.name, d.role, d.org) if x)),
        (CHANNEL_LABEL[lang].get(d.channel or "", "Contact"), d.address or "–"),
    ]
    if d.request_text:
        rows.append(("Text for the person (shown only after the ID Austria check)" if L else
                     "Text an die Person (erst nach ID-Austria-Prüfung sichtbar)", d.request_text))
    if d.note:
        rows.append(("Internal note (not sent)" if L else "Interne Notiz (nicht versendet)", d.note))
    return rows


def _domain_words(w, address: str | None) -> str | None:
    if not isinstance(w, dict) or not address or "@" not in address:
        return None
    typed = address.rsplit("@", 1)[1]
    known = w.get("similar_to")
    if not known:
        sugg = warning_suggestion(w, address)
        known = sugg.rsplit("@", 1)[1] if sugg and "@" in sugg else None
    return confusable_words(typed, known) if known else None


@_cards
def confirm_card(d: Draft, warnings: list, register, lang: str = "de", patient=None) -> Msg:
    """Warnings first (spelled out), then the request incl. the resolved patient, then the options."""
    L = lang == "en"
    rows = draft_rows(d, lang, patient)
    checks: list[tuple[str, str]] = []           # (mark, text); mark "" when the text carries its own
    for ok, line in register_lines(register):
        checks.append(("" if ("✓" in line or "⚠" in line) else ("✓" if ok else "⚠️"), line))
    all_addr = [w for w in warnings if is_address_warning(w)]          # incl. merged: for the suggestion
    shown = [w for w in warnings if not (isinstance(w, dict) and w.get("merged_into"))]   # folded by the service
    addr_w = [w for w in shown if is_address_warning(w)]
    reg_shown = bool(register_lines(register))
    other_w = [w for w in shown if not is_address_warning(w)
               and not (reg_shown and str(w.get("code") or "").startswith("register"))]   # already on the register line
    if addr_w:
        checks += [("⚠️", warning_text(w)) for w in addr_w]
        names_chars = any(warning_text(w).lstrip().startswith("Adresse prüfen:") for w in addr_w)
        words = None if names_chars else next((x for x in (_domain_words(w, d.address) for w in all_addr) if x), None)
        if words:
            checks.append(("⚠️", ("In words: " if L else "In Worten: ") + words))
    else:
        lbl = ("Address" if L else "Adresse") if d.channel == "email" else ("Number" if L else "Nummer")
        checks.append(("✓", f"{lbl}: " + ("no issues found" if L else "keine Auffälligkeiten")))
    checks += [("⚠️", warning_text(w)) for w in other_w]
    if not patient_label(patient):
        checks.append(("⚠️", f"Patient '{d.patient_id}': name/birth date not confirmed by the service – check the ID" if L
                       else f"Patient '{d.patient_id}': Name/Geburtsdatum vom Dienst nicht bestätigt – ID prüfen"))
    warned = any(m == "⚠️" or "⚠" in t for m, t in checks)
    bad = [(m, t) for m, t in checks if m == "⚠️" or "⚠" in t]
    good = [(m, t) for m, t in checks if (m, t) not in bad]
    sugg = next((s for s in (warning_suggestion(w, d.address) for w in all_addr) if s), None)
    if L:
        head = (("Not sent · check the address" if addr_w else "Not sent · please check") if warned
                else "Check request · not sent yet")
        opts = ([f"!fix = send to {sugg}"] if sugg else []) + [
            f"!yes = send anyway to {d.address}" if warned else "!yes = send", "!no = cancel"]
        if addr_w:
            opts.append("or type the corrected address")
        q = "Send?"
    else:
        head = (("Nicht gesendet · Adresse prüfen" if addr_w else "Nicht gesendet · bitte prüfen") if warned
                else "Anforderung prüfen · noch nicht gesendet")
        opts = ([f"!korrigieren = an {sugg} senden"] if sugg else []) + [
            f"!ja = trotzdem an {d.address} senden" if warned else "!ja = senden", "!nein = abbrechen"]
        if addr_w:
            opts.append("oder korrigierte Adresse eingeben")
        q = "Senden?"
    line = lambda m, t: f"{m} {t}".strip()
    text = (head + "\n" + "".join(line(m, t) + "\n" for m, t in bad)
            + "\n".join(f"• {k}: {v}" for k, v in rows) + "\n"
            + "".join(line(m, t) + "\n" for m, t in good) + f"{q} " + " · ".join(opts))
    h1, _, h2 = head.partition(SEP)
    typed = d.address.rsplit("@", 1)[1] if d.address and "@" in d.address else None
    known = next((x for x in (_known_domain(w, d.address) for w in all_addr) if x), None)
    bad_html = []
    for m, t in bad:
        if typed and known and m == "⚠️" and (t.startswith(("Adresse prüfen", "Check the address")) or typed in t) \
                and not any("statt der bekannten" in x or "instead of the known" in x for x in bad_html):
            # the one comparison on this card: both domains in mono, the differing glyph red (the send is blocked)
            words = confusable_words(typed, known)
            words = re.sub(r"\s*\([^()]*→[^()]*\)$", "", words) if words else None
            if words and L:
                words = None                       # the German spelling-out only
            bad_html.append(f"<p><b>{'Address' if L else 'Adresse'}</b> <code>{mark_diff(typed, known, red=True)}</code> "
                            f"{'instead of the known' if L else 'statt der bekannten'} <code>{_attr(known)}</code>"
                            + (f"<br/>{e(words)} – Tippfehler oder Täuschung?" if words else "") + "</p>")
        elif t.startswith(("In Worten: ", "In words: ")):
            bad_html.append(f"<p>{e(t)}</p>")
        else:
            bad_html.append(f"<p><b>{e(t)}</b></p>")
    pl = patient_label(patient)                   # the resolved patient, not the id slug
    rows_html = [(k, e(pl + (" (aus Name abgeleitet)" if d.patient_from_name and not L else ""))) if k == "Patient" and pl
                 else (k, e(v)) for k, v in rows]
    if typed and known and len(typed) == len(known):     # the typed address, its differing glyph red
        local = d.address.rsplit("@", 1)[0]
        rows_html = [(k, f"<code>{_attr(local)}@{mark_diff(typed, known, red=True)}</code>") if v == e(d.address) else (k, v)
                     for k, v in rows_html]
    html = (f"<p><b>{e(h1)}</b>" + (SEP + e(h2) if h2 else "") + "</p>"
            + "".join(bad_html)
            + _kv_table(rows_html, escaped=True)
            + _quiet([e(line(m, t)) for m, t in good])
            + f"<p><b>{e(q)}</b><br/>" + "<br/>".join(_opt(o) for o in opts) + "</p>")
    return text, html


def _known_domain(w, address: str | None) -> str | None:
    if not isinstance(w, dict):
        return None
    if w.get("similar_to"):
        return str(w["similar_to"])
    sugg = warning_suggestion(w, address)
    return sugg.rsplit("@", 1)[1] if sugg and "@" in sugg else None


def _opt(o: str) -> str:
    """'!ja = trotzdem an x senden' -> '<b>!ja</b> trotzdem senden' (the address stands in the table)."""
    cmd, eq, what = o.partition(" = ")
    if not (eq and cmd.startswith("!")):
        return e(o)
    if cmd in ("!ja", "!yes"):
        what = re.sub(r"\s+(?:an|to)\s+\S+@\S+|\s+(?:an|to)\s+\+?[\d ]{6,}", "", what)
    return f"<b>{e(cmd)}</b> {e(what)}"


def _kv_table(rows, escaped: bool = False) -> str:
    """Label | value rows (no header): labels plain, values as given (escaped here)."""
    rows = [(k, v) for k, v in rows if v not in (None, "")]
    if not rows:
        return ""
    def label(k):                       # 'Text an die Person (erst nach …)' -> label + its note in small type
        k1, par, k2 = str(k).partition(" (")
        return e(k1) + (f"<br/><sub>({e(k2)}</sub>" if par else "")
    return "<table><tbody>" + "".join(f"<tr><td>{label(k)}</td><td>{v if escaped else e(v)}</td></tr>"
                                      for k, v in rows) + "</tbody></table>"


@_cards
def sent_message(d: Draft, resp: dict, lang: str = "de", patient=None) -> Msg:
    pid = primary_id(resp.get("ref_code"), resp.get("request_id")) or "?"
    ch = CHANNEL_LABEL[lang].get(d.channel or "", d.channel or "?")
    exp = human_time(resp.get("expires_at")).split(",")[0] if resp.get("expires_at") else None
    st = str(resp.get("status") or "")
    if lang == "en":
        mid = [f"status: {st}" if st and st != "sent" else None, f"link valid until {exp}" if exp else None]
        lines = [f"To: {d.name} ({d.role}, {d.org}) · {ch} {d.address}", " · ".join(x for x in mid if x),
                 f"Track: !status {pid} · !audit {pid}"]
    else:
        mid = [f"Status: {status_de(st)}" if st and st != "sent" else None, f"Link gültig bis {exp}" if exp else None]
        lines = [f"An: {d.name} ({d.role}, {d.org}) · {ch} {d.address}", " · ".join(x for x in mid if x),
                 f"Verfolgen: !status {pid} · !audit {pid}"]
    lines = [x for x in lines if x]
    demo = demo_line(resp.get("demo_identity"))
    if demo:
        lines.append(demo)
    L = lang == "en"
    pat = patient or resp.get("patient")
    pname = pat.get("name") if isinstance(pat, dict) else pat
    pdob = pat.get("birthDate") if isinstance(pat, dict) else None
    who = ", ".join(x for x in (d.name, d.role, d.org) if x)
    parts = (_kind(d.doc_kind, lang), pname or (f"patient {d.patient_id}" if L else f"Patient {d.patient_id}"), _dob_part(pdob))
    state = "Request sent" if L else "Anfrage gesendet"
    head = head_text(state, *parts)
    html = (_headline(state, *parts)
            + f"<p>{e(('To ' if L else 'An ') + who)}</p>"
            + (f"<p>{e(demo)}</p>" if demo else "")
            + _quiet([e(ch)] + [e(x) for x in mid if x] + [e(pid)]))
    return head + "\n" + "\n".join(lines), html


def demo_line(di) -> str | None:
    """Demo mode only (the service sends it): the simulated ID Austria login to hand to the person."""
    if not isinstance(di, dict) or not di.get("pin"):
        return None
    return f"Demo-PIN für {di.get('name') or 'die angefragte Person'}: {di['pin']} (an die Person weitergeben)"


def _cmd_html(c: str) -> str:
    """'!anfordern labor <Name>, <Rolle>' -> '<code>!anfordern labor</code> &lt;Name&gt;, &lt;Rolle&gt;'."""
    m = re.match(r"^(![^\s<…]+(?:\s+[a-z|]+(?=\s|$))?)(.*)$", c)
    return (f"<b>{e(m.group(1))}</b>" + e(m.group(2))) if m else e(c)


def help_message(text: str) -> Msg:
    """!hilfe: the plain text unchanged; HTML as one table of commands (command in mono, what it does)."""
    lines = text.split("\n")
    rows, notes = [], []
    for l in lines[1:]:
        if l.startswith("• "):
            cmd, sep, what = l[2:].partition(" – ")
            rows.append([SEP.join(_cmd_html(c.strip()) for c in cmd.split(" · ")), e(what) if sep else ""])
        elif l.startswith("  ") and rows:
            rows[-1][1] += "<br/>" + f"<sub>{e(l.strip())}</sub>" if rows[-1][1] else f"<sub>{e(l.strip())}</sub>"
        elif l.strip():
            notes.append(e(l.strip()))
    return text, (f"<p><b>{e(lines[0].rstrip(':'))}</b></p>"
                  + _ul(f"{c}" + (f" – {r}" if r and not r.startswith("<sub>") else (f"<br/>{r}" if r else "")) for c, r in rows)
                  + _quiet(*[[n] for n in notes]))


# ---------------------------------------------------------------- status / audit

def _first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d[k]
    return default


@_cards
def status_message(rid: str, data, lang: str = "de") -> Msg:
    if not isinstance(data, dict):
        return f"Status {rid}: {data}", None
    de = lang == "de"
    rec = data.get("recipient") if isinstance(data.get("recipient"), dict) else {}
    rows = [
        ("Status", status_de(_first(data, "status", "state", default="?")) if de else _first(data, "status", default="?")),
        ("Ref", _first(data, "ref_code")),
        ("Patient", _first(data, "patient_id", "patient")),
        ("Dokument" if de else "Document", _kind(data["doc_kind"], lang) if data.get("doc_kind") else None),
        ("Empfänger" if de else "Recipient", rec.get("name") if rec else _first(data, "recipient")),
        ("Kanal" if de else "Channel", CHANNEL_LABEL[lang].get(_first(data, "channel"), _first(data, "channel"))),
        ("Erstellt" if de else "Created", hhmm(_first(data, "created_at", default=""))),
        ("Gesendet" if de else "Sent", hhmm(_first(data, "sent_at", default=""))),
        ("Identität gebunden an" if de else "Identity bound to", _first(data, "identity_bound_to")),
        ("eID-Fehlversuche" if de else "eID failures", data.get("eid_failures") or None),
        ("Fehlzustellungen abgefangen" if de else "Misdeliveries caught", data.get("misdeliveries_caught") or None),
        ("Letzter Grund" if de else "Last reason", _first(data, "last_reason", "reason")),
    ]
    rows = [(k, v) for k, v in rows if v not in (None, "")]
    head = f"Status {rid}"
    st = next((v for k, v in rows if k == "Status"), None)
    return (head + "\n" + "\n".join(f"• {k}: {v}" for k, v in rows),
            f"<p><b>{e((st[:1].upper() + st[1:]) if st else 'Status')}</b>{SEP}{e(rid)}</p>"
            + _kv_table([(k, str(v)) for k, v in rows if k not in ("Status", "Ref") and not (k == "Ref" and v == rid)]))


def audit_rows(data) -> list[dict]:
    if isinstance(data, dict):
        if data.get("resourceType") == "Bundle":
            data = [x.get("resource", {}) for x in data.get("entry", []) or []]
        else:
            data = _first(data, "entries", "events", "audit", "items", default=[])
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def audit_entry_line(ev: dict) -> tuple[str, str, str, str]:
    """-> (when, step, who, what) for plain dict entries or FHIR AuditEvent."""
    if ev.get("resourceType") == "AuditEvent":
        sub = (ev.get("subtype") or [{}])[0] if isinstance(ev.get("subtype"), list) else {}
        step = sub.get("code") or sub.get("display") or (ev.get("type") or {}).get("display", "")
        who = ", ".join(str((a.get("who") or {}).get("display") or (a.get("who") or {}).get("reference") or "")
                        for a in ev.get("agent", []) or [])
        return (ev.get("recorded", ""), step, who, ev.get("outcomeDesc") or "")
    when = str(_first(ev, "at", "ts", "time", "timestamp", "when", "recorded", default=""))
    step = str(_first(ev, "step", "event", "action", "type", default=""))
    who = _first(ev, "who", "actor", "agent", default="")
    if isinstance(who, dict):
        who = who.get("display") or who.get("name") or who.get("id") or str(who)
    what = _first(ev, "what", "description", "message", default="")
    return (when, step, str(who), str(what))


SIX = ["request_sent", "link_opened", "identity_checked", "answer_sent", "answer_filed", "misdelivery_caught"]
STEP_LABEL = {
    "request_sent": "Anfrage gesendet", "link_opened": "Link geöffnet", "identity_checked": "Identität geprüft",
    "answer_sent": "Antwort gesendet", "answer_filed": "Antwort abgelegt", "misdelivery_caught": "Fehlzustellung erkannt",
    "request_held": "Anfrage zurückgehalten", "filing_held": "Ablage angehalten", "request_cancelled": "Anfrage storniert",
    "decision": "Entscheidung",
}


def step_key(step: str) -> str:
    return re.sub(r"[\s\-]+", "_", (step or "").strip().lower())


def step_label(step: str) -> str:
    return STEP_LABEL.get(step_key(step), step)


def hhmm(ts: str) -> str:
    """'2026-10-29T19:02:11.123+01:00' -> '29.10. 19:02:11' (other formats unchanged)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2}:\d{2})", ts or "")
    return f"{m.group(3)}.{m.group(2)}. {m.group(4)}" if m else (ts or "")


def _ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ok(ev: dict) -> bool:
    o = str(ev.get("outcome", "")).lower()
    return o in ("", "success", "ok", "0", "true")


def _dur(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60} min {sec % 60:02d} s" if sec >= 60 else f"{sec} s"


def effort_seconds(data, evs: list[dict]) -> float | None:
    """Outside person's time: first 'link opened' -> first successful 'answer sent' after it.
    Uses the service's own number if it provides one."""
    if isinstance(data, dict):
        v = _first(data, "effort_seconds", "outside_seconds")
        if isinstance(v, (int, float)):
            return float(v)
    opened = sent = None
    for ev in evs:
        when, step, _, _ = audit_entry_line(ev)
        k, t = step_key(step), _ts(when)
        if not t:
            continue
        if k == "link_opened" and opened is None:
            opened = t
        elif k == "answer_sent" and opened is not None and sent is None and _ok(ev):
            sent = t
    if opened and sent:
        try:
            return (sent - opened).total_seconds()
        except TypeError:   # naive vs aware
            return None
    return None


def chain_line(data) -> tuple[bool, str] | None:
    """Hash-chain status as reported by the service (never computed by the bot)."""
    if not isinstance(data, dict):
        return None
    c = _first(data, "chain", "hash_chain", "chain_ok", "chain_intact", "audit_chain")
    if c is None:
        return None
    if isinstance(c, bool):
        ok, txt = c, ""
    elif isinstance(c, dict):
        ok = bool(c.get("ok", c.get("intact", False)))
        txt = str(c.get("text") or c.get("detail") or "")
    else:
        ok = str(c).lower() in ("ok", "intact", "intakt", "valid", "true")
        txt = "" if ok else str(c)
    return ok, ("Hash-Kette: intakt ✓" if ok else f"Hash-Kette: GEBROCHEN ‼ {txt}".rstrip())


def steps_line(present: set[str]) -> str:
    """Clean request: the five regular steps out of 5 ('misdelivery caught' has nothing to catch and is
    never shown as missing). With a caught misdelivery: all six out of 6."""
    caught = "misdelivery_caught" in present
    steps = SIX if caught else SIX[:5]
    n = sum(1 for s in steps if s in present)
    total = len(steps)
    mark = "✓ " if n == total else ""
    listing = " ".join(("✓ " if s in present else "○ ") + STEP_LABEL[s] for s in steps)
    if caught:
        return f"{mark}{n}/{total} Schritte: {listing}"
    return f"{mark}{n}/{total} Schritte (keine Fehlzustellung): {listing}"


@_cards
def audit_message(rid: str, data, lang: str = "de") -> Msg:
    evs = audit_rows(data)
    head = f"Audit {rid}"
    if not evs:
        return head + (": keine Einträge" if lang == "de" else ": no entries"), None
    present = {step_key(audit_entry_line(ev)[1]) for ev in evs}
    summary = [steps_line(present)]
    ch = chain_line(data)
    if ch:
        summary.append(ch[1])
    eff = effort_seconds(data, evs)
    if eff is not None:
        summary.append(f"Aufwand der Person: {_dur(eff)} (Link geöffnet → Antwort gesendet)")
    rows, idx, count, last = [], {}, {}, {}
    for ev in evs:
        when, step, who, what = audit_entry_line(ev)
        k = step_key(step)
        mark = "⚠️" if (not _ok(ev) or k == "misdelivery_caught") else "✓"
        key = (k, who, mark)                      # same step, same actor, same outcome -> one line with a count
        if key in idx:
            count[key] += 1
            last[key] = hhmm(when)
            continue
        what = what if len(what) <= 160 else what[:157] + "…"
        idx[key], count[key] = len(rows), 1
        rows.append([mark, hhmm(when), step_label(step), who, what, key])
    for r in rows:
        n = count[r[5]]
        if n > 1:
            r[2] = f"{r[2]} ({n}×, zuletzt {last[r[5]].split(' ')[-1]})"
    rows = [tuple(r[:5]) for r in rows]
    text = (head + "\n" + "\n".join(summary) + "\n" + "\n".join(
        f"{m} {w} · {s}" + (f" · {who}" if who else "") + (f" · {what}" if what else "") for m, w, s, who, what in rows))
    broken = ch is not None and not ch[0]
    days = {w.split(" ")[0] for _, w, *_ in rows if " " in w}
    one_day = len(days) == 1
    short = (lambda w: w.split(" ", 1)[1]) if one_day else (lambda w: w)
    steps = summary[0].split(":")[0]                    # '✓ 6/6 Schritte': the list below names them
    html = (f"<p><b>Audit</b>{SEP}{e(rid)}" + (f"{SEP}{e(next(iter(days)))}" if one_day else "") + "</p>"
            + "<p>" + SEP.join([e(steps)] + [_red(f"<b>{e(x)}</b>") if (broken and x == ch[1]) else e(x)
                                             for x in summary[1:]]) + "</p>"
            + "<ol>" + "".join(f"<li>{'✓' if m == '✓' else '<b>!</b>'} {e(short(w))}  " + (f"<b>{e(s)}</b>" if m != "✓" else e(s))
                               + (f"{SEP}{e(who)}" if who else "") + (f"{SEP}{e(what)}" if what else "") + "</li>"
                               for m, w, s, who, what in rows) + "</ol>")
    return text, html


# ---------------------------------------------------------------- /notify payloads

HOLD_MARK = "⏸ Angehalten – Entscheidung nötig:"     # old service prefix, stripped from reasons
HOLD_STATE = "Angehalten"
DECISION_HELP = "Entscheidung: !ablegen {id} <Begründung> · !verwerfen {id} · !halten {id} · !zeigen {id}"
FIELD_LABEL = {"birthDate": "Geburtsdatum", "birth_date": "Geburtsdatum", "dob": "Geburtsdatum",
               "name": "Name", "family": "Nachname", "given": "Vorname", "patient": "Patient",
               "patient_line": "Patientenzeile", "gender": "Geschlecht", "sex": "Geschlecht",
               "kind": "Dokumentart", "doc_kind": "Dokumentart", "sender": "Absender", "requester": "Einsender",
               "einsender": "Einsender", "author": "Autor", "unit": "Einheit", "loinc": "LOINC-Code",
               "value": "Wert", "test": "Test", "org": "Organisation", "organisation": "Organisation",
               "Labor": "Labor", "Klinik": "Klinik", "Einrichtung": "Einrichtung", "Organisation": "Organisation"}
VALUE_DE = {"male": "männlich", "female": "weiblich", "other": "divers", "unknown": "unbekannt",
            "lab_result": "Laborbefund", "discharge_report": "Entlassungsbericht"}
STATUS_WORD_DE = {"preliminary": "vorläufig", "partial": "Teilbefund", "corrected": "korrigiert",
                  "amended": "geändert", "appended": "ergänzt", "final": "endgültig", "registered": "registriert"}


def _date_de(v) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(v))
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else VALUE_DE.get(str(v), str(v))


def _norm_cmp(x) -> str:
    return re.sub(r"\s+", " ", str(x or "")).strip().casefold()


def _in(haystack: str, value) -> bool:
    """Is this value already stated in the text (reason / facts)? Dates compared in German format too."""
    v = _norm_cmp(value)
    if len(v) < 3:
        return False
    h = _norm_cmp(haystack)
    return v in h or _norm_cmp(_date_de(value)) in h


def visible_mismatches(mm, said: str) -> list[dict]:
    """Rows worth showing: known (translatable) field, not empty, not already stated in `said`."""
    out = []
    for m in mm or []:
        if not isinstance(m, dict) or m.get("field") not in FIELD_LABEL:
            continue
        doc, req = m.get("document"), m.get("request")
        if doc in (None, "") and req in (None, "") and m.get("declared") in (None, ""):
            continue
        if doc not in (None, "") and _in(said, doc):      # the document's value is already stated
            continue
        out.append(m)
    return out


def mismatch_lines(mm) -> list[str]:
    """[{field, document, request, declared?}] -> 'Geburtsdatum – Dokument: 03.12.1961 · Anfrage: 12.03.1961'.
    Only used for HOLD (near-miss on the requested patient), never for wrong-patient alerts."""
    out = []
    for m in mm or []:
        if not isinstance(m, dict):
            out.append(str(m))
            continue
        parts = [f"{label}: {_date_de(m[k]) if m[k] not in (None, '') else '(fehlt)'}"
                 for k, label in (("document", "Dokument"), ("declared", "Angabe"), ("request", "Anfrage")) if k in m]
        f = FIELD_LABEL.get(m.get("field", ""), m.get("field", ""))
        out.append((f"{f} – " if f else "") + " · ".join(parts))
    return out


def hold_id(p: dict) -> str | None:
    """The id Dr. Hofer types: KB- ref first, then request id, then answer number."""
    d = p.get("data") if isinstance(p.get("data"), dict) else {}
    from .parse import valid_id
    ref = next((str(x) for x in (p.get("ref_code"), d.get("ref_code")) if valid_id(x)), None)
    ans = next((str(x) for x in (p.get("answer_id"), d.get("answer_id")) if valid_id(x)), None)
    return ref or ans or primary_id(p.get("request_id"), d.get("request_id"))


@_cards
def notify_message(p: dict) -> Msg:
    """Turn a /notify payload into (text, html).

    Plain {"room_id","text","html"?} is posted exactly as given (the service owns the wording,
    e.g. wrong-patient alerts that must never name the other patient).
    Optional "kind": "filed" | "hold" | "misdelivery" | "no_result" with "data" lets the bot format the card.
    """
    kind = (p.get("kind") or "").lower()
    text, html = p.get("text"), p.get("html")
    data = p.get("data") if isinstance(p.get("data"), dict) else None
    d = data or {}
    pid = primary_id(p.get("ref_code"), d.get("ref_code"), p.get("request_id"), d.get("request_id"))
    if kind == "filed" and data:
        return _filed(data, pid)
    if kind == "hold":
        return _hold(p, d, text)
    if kind == "holds_superseded":
        t = f"✓ {pid or '?'}: frühere angehaltene Antwort(en) ersetzt – neue Antwort abgelegt, keine Entscheidung mehr nötig."
        return t, (_headline("Keine Entscheidung mehr nötig", pid)
                   + "<p>Frühere angehaltene Antwort ersetzt, die neue ist abgelegt.</p>")
    if kind == "request_sent":
        who = d.get("recipient") or "die angefragte Person"
        head = head_text("Anfrage gesendet", pid, f"an {who}")
        demo = demo_line(d.get("demo_identity") or p.get("demo_identity"))
        lines = [x for x in (demo, f"Verfolgen: !status {pid} · !audit {pid}" if pid else None) if x]
        return head + "".join(f"\n{l}" for l in lines), (
            _headline("Anfrage gesendet", f"an {who}") + (f"<p>{e(demo)}</p>" if demo else "")
            + _quiet([e(pid)] if pid else []))
    if kind in ("identity_confirm", "new_identity", "identity_review", "namesake"):
        return _identity_confirm(p, d, pid)
    if kind in ("no_result", "kein_befund", "no_finding"):
        return _no_result(d, text, pid)
    if kind == "tamper":                           # the service's words under a red state
        body = str(text or d.get("text") or "Audit-Kette verändert.").strip()
        rest = re.sub(r"^\[[^\]]*\]\s*", "", body)
        who, dash, what = rest.partition(" – ")
        if not dash or len(who) > 80:
            who, what = "", rest
        m = re.match(r"^(.*?)\s*\((\d{2}\.\d{2}\.\d{4})\)$", who)
        parts = (m.group(1), m.group(2)) if m else (who,)
        if pid:
            what = re.sub(rf"\s*\({re.escape(pid)}\)", "", what)
        what = what[:1].upper() + what[1:]
        return (head_text(TAMPER_STATE, *parts) + f"\n{what}" + (f"\nReferenz: {pid}" if pid else ""),
                _headline(TAMPER_STATE, *parts, red=True) + f"<p>{e(what)}</p>" + _quiet([e(pid)] if pid else []))
    if kind in ("misdelivery", "blocked", "rejected"):
        # Service text verbatim; only the marker is added if missing. No document data is shown.
        reason = text or d.get("reason") or "?"
        lines = []
        if d.get("attempted_by"):
            lines.append(f"Versucht von: {d['attempted_by']}")
        if d.get("wrong_address") is True:        # reopening a link that went to a wrong address is wrong advice
            nxt = str(d.get("next_step") or p.get("next_step") or "").strip()
            if nxt and nxt not in reason:
                lines.append(f"Nächster Schritt: {nxt}")
        elif _is_locked(p, reason):
            lines.append(f"Nur für einen neuen Versuch der angefragten Person: !freigeben {pid or '<id>'}")
        if pid:
            lines.append(f"Protokoll: !audit {pid}")
        return _alert(reason, lines, d, bool(d.get("technical")))
    if not text:
        raise ValueError("text is required (or kind=filed with data)")
    demo = demo_line(d.get("demo_identity") or p.get("demo_identity"))
    if demo and demo not in text:
        text = f"{text}\n{demo}"
        html = (html + f"<p>{e(demo)}</p>") if html else None
    return text, html


TAMPER_STATE = "Befund nachträglich geändert"
ALERT_STATE = {"FEHLZUSTELLUNG ABGEFANGEN": "Fehlzustellung abgefangen", "BLOCKIERT": "Blockiert"}
_ALERT = re.compile(r"^\s*(\[[^\]]{2,40}\]|⚠️?\s*Nicht abgelegt:|ℹ️)\s*")


def _alert_state(marker: str) -> tuple[str, bool]:
    """Service marker -> (state word, red). '[FEHLZUSTELLUNG ABGEFANGEN]' -> ('Fehlzustellung abgefangen', True)."""
    if marker.startswith("["):
        word = marker.strip("[] ")
        return ALERT_STATE.get(word, word[:1].upper() + word[1:].lower()), True
    if "Nicht abgelegt" in marker:
        return "Nicht abgelegt", True
    return "Datei nicht übernommen", False


def _alert(body: str, lines: list[str], d: dict, technical: bool = False) -> Msg:
    """'[FEHLZUSTELLUNG ABGEFANGEN] Antwort von X …, auf KB-1: Dokument betrifft eine andere Person. …'
    -> red state · document · requested patient, the finding as the body, who/where and the log command quiet.
    The service's words are kept; only its marker becomes the headline. The other person is never named here."""
    m = _ALERT.match(body)
    state, red = _alert_state(m.group(1).strip()) if m else ("Nicht abgelegt", True)
    rest = body[m.end():] if m else body
    if m and m.group(1).startswith("ℹ"):                    # 'ℹ️ KB-1: Datei von X nicht übernommen – …'
        rest = re.sub(r"^[A-Za-z]+-[\w-]+:\s*", "", rest)
    context, finding = "", rest
    i = rest.find(": ")
    if m and m.group(1).startswith("[") and 0 < i < 220:
        context, finding = rest[:i], rest[i + 2:]
    finding = finding[:1].upper() + finding[1:]
    red = red and not technical
    pat = d.get("patient") if isinstance(d.get("patient"), dict) else {}
    parts = (_kind(d["doc_kind"]) if d.get("doc_kind") else None, pat.get("name"), _dob_part(pat.get("birthDate")))
    if not any(parts):
        parts = (primary_id(d.get("ref_code")),)
    cmds = [l for l in lines if l.startswith("Protokoll:")]
    other = [l for l in lines if l not in cmds]
    t = head_text(state, *parts) + f"\n{finding}" + (f"\n{context}" if context else "") + "".join(f"\n{l}" for l in lines)
    h = (_headline(state, *parts, red=red) + f"<p>{e(finding)}</p>" + "".join(f"<p>{_with_cmd(l)}</p>" for l in other)
         + _quiet([e(context)] if context else [], [e(l) for l in cmds]))
    return t, h


def _with_cmd(line: str) -> str:
    """'Nur für …: !freigeben KB-1' -> the command in bold."""
    i = line.find("!")
    return e(line[:i]) + f"<b>{e(line[i:])}</b>" if i > 0 else e(line)


def _is_locked(p: dict, text: str) -> bool:
    """Locked link: structured flag if the service sends one, else the service's wording ('… Link ist jetzt gesperrt')."""
    d = p.get("data") if isinstance(p.get("data"), dict) else {}
    if p.get("locked") is True or d.get("locked") is True or str(d.get("status") or p.get("status") or "") == "locked":
        return True
    return bool(re.search(r"\bLink\b[^.]*\bgesperrt\b", text or ""))


def sender_de(s) -> str:
    """'Eva Brandl (ID Austria – Simulation)' -> 'Eva Brandl, ID Austria (Simulation)': the simulation said once, short."""
    return re.sub(r"\s*\(ID Austria\s*[–-]\s*Simulation(?: geprüft)?\)", ", ID Austria (Simulation)", str(s or ""))


def _reason_de(d: dict, text) -> str:
    """German reason: reason_text_de | reasons_de (from fhirmap.reason_text_de), else data.reason, else text."""
    if isinstance(d.get("reasons_de"), list) and d["reasons_de"]:
        return join_sentences(d["reasons_de"])
    if d.get("reason_text_de"):
        return join_sentences(re.split(r"(?<=[.!?])\s*;\s*", str(d["reason_text_de"])))
    return str(d.get("reason") or text or "Antwort passt nicht eindeutig zur Anfrage.").removeprefix(HOLD_MARK).strip()


def join_sentences(parts) -> str:
    """['A.', 'B'] -> 'A. B.' (no '.;' or '..'); duplicates dropped."""
    out = []
    for x in parts:
        x = re.sub(r"\.{2,}", ".", str(x).strip().rstrip(";").strip())
        if not x:
            continue
        if x[-1] not in ".!?":
            x += "."
        if x not in out:
            out.append(x)
    return " ".join(out)


OTHER_PERSON_CODES = {"OTHER_PATIENT_PRESENT"}   # two reports for the same patient are not another person
OTHER_PERSON_LINE = "⚠️ Das Dokument enthält Angaben zu einer weiteren Person"


def other_person_present(d: dict) -> bool:
    """Flag only; the other person is never named on the card."""
    det = d.get("details") if isinstance(d.get("details"), dict) else {}
    if det.get("other_person_present") is True or d.get("other_person_present") is True:
        return True
    codes = set()
    for k in ("codes", "reason_codes", "reasons"):
        v = d.get(k)
        if isinstance(v, list):
            codes |= {str(x).split(":")[0].strip().upper() for x in v if isinstance(x, (str,))}
            codes |= {str(x.get("code")).upper() for x in v if isinstance(x, dict) and x.get("code")}
    return bool(codes & OTHER_PERSON_CODES)


def _identity_confirm(p: dict, d: dict, pid) -> Msg:
    """Namesake protection: a verified eID with a new person-key for a known recipient. Nothing was shown.
    Compact; uses exactly the fields present (new_birth_year, known_answers, known_since,
    birth_year_differs, via_contact)."""
    hid = hold_id(p) or pid or "<id>"
    name = d.get("recipient") or d.get("recipient_name") or d.get("name") or "die angefragte Person"
    head = f"Neue ID-Austria-Identität für {name} ({hid})"
    if d.get("new_birth_year"):
        head += f": Jahrgang {d['new_birth_year']}"
    known = []
    n = d.get("known_answers")
    if isinstance(n, int) and n > 0:
        known.append(f"{n} {'Antwort' if n == 1 else 'Antworten'}" + (f" seit {human_time(d['known_since'])}"
                                                                       if d.get("known_since") else ""))
    elif d.get("known_since"):
        known.append(f"seit {human_time(d['known_since'])}")
    if d.get("birth_year_differs") is True:
        known.append("anderer Jahrgang")
    elif d.get("birth_year_differs") is False:
        known.append("gleicher Jahrgang")
    sent1 = "bisher hat eine andere Person geantwortet" + (f" ({', '.join(known)})" if known else "") + "."
    first = f"{head} – {sent1}"
    lines = [first]
    if d.get("via_contact"):
        lines.append(f"Kontakt: {d['via_contact']}.")
    lines.append("Der neuen Person wurde nichts angezeigt; nichts abgelegt.")
    if d.get("detail"):
        lines.append(str(d["detail"]))
    ask = f"Freigeben? !identität {hid} ja · nein"
    t = " ".join(lines[:2]) if d.get("via_contact") else lines[0]
    t = "\n".join([t] + lines[(2 if d.get("via_contact") else 1):] + [ask])
    body = sent1[:1].upper() + sent1[1:]
    if d.get("new_birth_year"):
        body = f"Jahrgang {d['new_birth_year']}. " + body
    return t, (_headline("Neue ID-Austria-Identität", name)
               + f"<p>{e(body)}" + (f" Kontakt: {e(d['via_contact'])}." if d.get("via_contact") else "") + "</p>"
               + "".join(f"<p>{e(l)}</p>" for l in lines[(2 if d.get("via_contact") else 1):])
               + f"<p><b>Freigeben?</b><br/><b>{e(f'!identität {hid} ja')}</b> zulassen<br/>"
               f"<b>{e(f'!identität {hid} nein')}</b> ablehnen</p>")


def _strip_check(x) -> str:
    return re.sub(r"^\s*(?:✓\s*)+", "", str(x)).strip()


def _codes(d: dict) -> set[str]:
    out = set()
    for k in ("codes", "reason_codes"):
        for x in d.get(k) or []:
            out.add(str(x.get("code") if isinstance(x, dict) else x).split(":")[0].strip().upper())
    return out


def _replaces_text(r) -> str | None:
    if not r:
        return None
    if isinstance(r, dict):
        bits = [r.get("text") or r.get("title"), human_time(r.get("date") or r.get("report_time") or ""),
                r.get("ref") or r.get("id")]
        uniq = []
        for b in (str(x).strip() for x in bits if x):
            if b and not any(_norm_cmp(b) == _norm_cmp(u) for u in uniq):
                uniq.append(b)
        return " · ".join(uniq) or None
    return str(r)


def replaces_line(d: dict, codes: set[str]) -> str | None:
    """Which filed report a correction replaces: replaces_ref / replaces_sample / prior_matches (from the service)."""
    n = d.get("prior_matches")
    if isinstance(n, int) and n > 1:
        return f"⚠️ {n} frühere Befunde passen – nicht eindeutig"
    if d.get("replaces_ref"):
        return f"Ersetzt: {d['replaces_ref']}" + (f" (Probe {human_time(d['replaces_sample'])})"
                                                  if d.get("replaces_sample") else "")
    if isinstance(n, int) and n == 0 and ("CORRECTION_REVIEW" in codes or d.get("replaces")):
        return "⚠️ Ursprungsbefund nicht gefunden – bitte manuell prüfen"
    if "CORRECTION_REVIEW" in codes or d.get("replaces"):
        rt = _replaces_text(d.get("replaces"))
        return f"Ersetzt laut Dokument: {rt}" if rt else "Korrektur eines früheren Befunds (Bezug nicht angegeben)"
    return None


def _first_sentence(d: dict) -> str | None:
    for k in ("first_sentence", "excerpt", "title", "comment"):
        v = d.get(k)
        if v:
            v = re.split(r"(?<=[.!?])\s", str(v).strip(), maxsplit=1)[0]
            return v[:200] + ("…" if len(v) > 200 else "")
    return None


_INLINE_MM = re.compile(r":\s*(Geburtsdatum|Name|Vorname|Nachname|Geschlecht)\s*:\s*Dokument\s+(.+?)\s+·\s+Anfrage\s+(.+?)\s*\.?\s*$")


_LAUT_DOK = re.compile(r":\s*([A-ZÄÖÜ][\wäöüß-]*) laut Dokument:\s*(.+?)\s+–\s+angefragt:\s*(.+?)\s*\.?\s*$")


def _matched_line(matched: list[str]) -> str | None:
    """['Name', 'Absender per ID Austria geprüft'] -> '✓ Name · ✓ ID Austria' (one line)."""
    out = []
    for x in matched:
        x = re.sub(r"\s*\(Simulation\)", "", x).strip()
        if re.search(r"ID Austria", x):
            x = "ID Austria"
        elif ":" in x:              # 'Name stimmt: Maria Beispiel' -> 'Name'
            x = re.sub(r"\s+stimmt$", "", x.split(":", 1)[0].strip())
        if x and x not in out:
            out.append(x)
    order = {"Name": 0, "Geburtsdatum": 1, "ID Austria": 9}
    out.sort(key=lambda x: order.get(x, 5))
    return SEP.join(f"✓ {x}" for x in out) or None


def _hold(p: dict, d: dict, text) -> Msg:
    """One decision: what and for whom, the reason in one sentence, the differing field side by side, what
    matched (one line), then the reference once and the commands. The service's own option wording is never shown."""
    hid = hold_id(p) or "<id>"
    kind = _kind(d.get("doc_kind")) if d.get("doc_kind") else "Antwort"
    pat = d.get("patient")
    if isinstance(pat, dict):
        pname, pdob = pat.get("name"), pat.get("birthDate")
    else:
        pname, pdob = pat, d.get("patient_birthDate")
    head = head_text(HOLD_STATE, kind, pname, _dob_part(pdob))
    reason = _reason_de(d, text)
    mm = [m for m in d.get("mismatches") or [] if isinstance(m, dict)]
    # the service's sentence may carry the comparison inline ('…: Geburtsdatum: Dokument X · Anfrage Y.'):
    # the table shows it, the sentence keeps only the finding
    im = _INLINE_MM.search(reason)
    lm = _LAUT_DOK.search(reason) if not im else None
    if lm and lm.group(1) in FIELD_LABEL:                                   # '… als angefragt: Labor laut Dokument: X – angefragt: Y.'
        mm.append({"field": lm.group(1), "document": lm.group(2), "request": lm.group(3)})
        reason = reason[: lm.start()].rstrip(" :") + "."
    if im:
        field = next((k for k, v in FIELD_LABEL.items() if v == im.group(1)), im.group(1))
        if not any(FIELD_LABEL.get(m.get("field", "")) == im.group(1) for m in mm):
            mm.append({"field": field, "document": im.group(2), "request": im.group(3)})
        reason = reason[: im.start()].rstrip(" :") + "."
        reason = re.sub(r"\?\)\.$", "?)", reason)
    other = other_person_present(d)
    codes = _codes(d)
    facts = []
    sender = d.get("sender")
    verified = re.sub(r"\s*\(.*$", "", str(sender or "")).strip()
    for k, label in (("sender_line", "Absender laut Dokument"), ("named_sender", "Absender laut Dokument"),
                     ("author", "Autor laut Dokument"), ("einsender", "Einsender laut Dokument")):
        v = d.get(k)
        if k == "einsender":
            v = v.get("name") if isinstance(v, dict) else v
            if v and _is_requester(v, d):
                continue                       # never tell Dr. Hofer that she ordered it herself
        else:
            if k == "author" or isinstance(v, dict):
                v = person_name(v)             # the name only: no (English) role, no address
            if v and verified and same_person(person_name(v), verified):
                continue                       # the verified sender: said once, in the checks line
        if v and str(v).strip() != verified and not any(f.endswith(f": {v}") for f in facts) and not _in(reason, v):
            facts.append(f"{label}: {v}")
    if "KIND_UNKNOWN" in codes and not other:      # never quote a document that also holds another person
        fs = _first_sentence(d)
        if fs:
            facts.append(f"Dokument beginnt mit: „{fs}“")
    rl = replaces_line(d, codes)
    if rl:
        facts.append(rl)
    mm = visible_mismatches(mm, reason + "\n" + "\n".join(facts))
    matched = [_strip_check(x) for x in d.get("matched") or [] if _strip_check(x)]
    ok_line = _matched_line(matched)
    charset = f"Hinweis: {d['charset_note']}" if d.get("charset_note") else None
    sname = sender_parts(sender)[0] if sender else ""
    if ok_line and sname and ok_line.endswith("ID Austria"):
        checks = [f"{ok_line}: {sname}"]
    else:
        checks = [x for x in (ok_line, f"von {sname}" if sname else None) if x]
    lines = ([OTHER_PERSON_LINE] if other else []) + [f"Grund: {reason}"] + ([f"  {charset}"] if charset else []) \
        + [f"  {l}" for l in mismatch_lines(mm)] \
        + [f"  {f}" for f in facts] + ([f"  {SEP.join(checks)}"] if checks else [])
    decide = DECISION_HELP.format(id=hid)
    t = head + "".join(f"\n{l}" for l in lines) + f"\n{decide}"

    rows = [[e(FIELD_LABEL.get(m.get("field", ""), m.get("field", ""))),
             _cmp_cell(m["document"], m.get("request")) if m.get("document") not in (None, "") else "(fehlt)",
             _cmp_cell(m["request"]) if m.get("request") not in (None, "") else "(fehlt)"] for m in mm]
    h = (_headline(HOLD_STATE, kind, pname, _dob_part(pdob))
         + (f"<p>{_red('<b>' + e(OTHER_PERSON_LINE.removeprefix('⚠️ ')) + '</b>')}</p>" if other else "")
         + f"<p>{e(reason)}</p>" + (f"<p>{e(charset)}</p>" if charset else "")
         + _table(["Feld", "Dokument", "Anfrage"], rows)
         + "".join(f"<p>{e(f)}</p>" for f in facts)
         + _quiet([e(x) for x in checks])
         + f"<p><b>Entscheiden</b>{SEP}<b>{e(hid)}</b><br/><sub>Befehl + Referenz: "
         + SEP.join(e(x) for x in ("!ablegen (mit Begründung)", "!verwerfen", "!halten", "!zeigen")) + "</sub></p>")
    return t, h


def _cmp_cell(v, other=None) -> str:
    """A compared value. Dates and numbers in mono, compared digit by digit: in the document's value only the
    differing digits are bold. Words stay words (the differing value in bold)."""
    v = _date_de(v)
    o = _date_de(other) if other not in (None, "") else None
    if re.fullmatch(r"[\d.,/:\- ]+", v):
        return f"<code>{mark_diff(v, o) if o else no_room_ping(_attr(v))}</code>"
    return f"<b>{e(v)}</b>" if o is not None else e(v)


PREVIEW_MAX = 6000


def preview_message(hid: str, data, lang: str = "de") -> Msg:
    """Held document as a code block: escaped, never linkified (inside <code>), no mentions."""
    if isinstance(data, dict):
        txt = data.get("text") or data.get("content") or data.get("preview") or data.get("markdown") or ""
        fname = data.get("filename")
    else:
        txt, fname = str(data or ""), None
    txt = str(txt)
    cut = len(txt) > PREVIEW_MAX
    if cut:
        txt = txt[:PREVIEW_MAX]
    head = (f"📄 Angehaltenes Dokument {hid}" + (f" ({fname})" if fname else "")
            + " – nur zur Ansicht, nicht abgelegt:") if lang == "de" else (
            f"📄 Held document {hid}" + (f" ({fname})" if fname else "") + " – view only, not filed:")
    tail = ("(gekürzt)" if lang == "de" else "(truncated)") if cut else ""
    body = txt.replace("```", "``\u200b`")
    t = f"{head}\n```\n{body}\n```" + (f"\n{tail}" if tail else "")
    h = f"<p><b>{e(head)}</b></p><pre><code>{_attr(txt)}</code></pre>" + (f"<p>{e(tail)}</p>" if tail else "")
    return t, h


def _no_result(d: dict, text, pid) -> Msg:
    """'Kein Befund vorhanden' answered by the outside person: nothing filed, the request is answered."""
    pat = d.get("patient")
    pname = pat.get("name") if isinstance(pat, dict) else pat
    pdob = pat.get("birthDate") if isinstance(pat, dict) else d.get("patient_birthDate")
    kind = _kind(d.get("doc_kind")) if d.get("doc_kind") else None
    sname = sender_parts(d["sender"])[0] if d.get("sender") else None
    head = head_text("Kein Befund vorhanden", kind, pname, _dob_part(pdob))
    lines = [f"von {sname}"] if sname else []
    said = d.get("comment") or d.get("reason") or d.get("text") or text
    if said:
        lines.append(f"Angabe: „{said}“")
    if d.get("received_at"):
        lines.append(f"Eingegangen: {human_time(d['received_at'])}")
    if pid:
        lines.append(f"Anfrage: {pid} · !audit {pid}")
    return head + "".join(f"\n{l}" for l in lines), (
        _headline("Kein Befund vorhanden", kind, pname, _dob_part(pdob))
        + (f"<blockquote>„{e(said)}“</blockquote>" if said else "")
        + _quiet([e(f"von {sname}") if sname else None,
                  e(f"Eingegangen {human_time(d['received_at'])}") if d.get("received_at") else None,
                  e(pid) if pid else None]))


FILED_STATE = "Abgelegt"
REPORT_STATUS_DE = {"preliminary": "⚠️ VORLÄUFIG", "partial": "⚠️ VORLÄUFIG (Teilbefund)",
                    "corrected": "⚠️ KORRIGIERT", "amended": "⚠️ KORRIGIERT", "appended": "⚠️ ERGÄNZT"}


_NUMERIC = re.compile(r"^\s*[<>≤≥=~]?\s*[-+]?\d")


def _value(o: dict) -> str:
    """The lab's own value string if given (keeps '< 0,27', '1,0'), else comparator + value.
    No unit on text/pending values ('folgt', 'nicht bestimmbar'); the flag only once."""
    if o.get("value_text") not in (None, ""):
        v = str(o["value_text"]).strip()
    else:
        v = f"{o.get('comparator') or ''}{o.get('value', '?')}"
    numeric = bool(_NUMERIC.match(v))
    flag = str(o.get("flag") or "").strip()          # the lab's own flag, never computed here
    flag_in_value = bool(flag) and (v.endswith(flag) or flag in v.split())
    if flag_in_value:                                  # '4,6*' / '1,3 +': unit goes before the lab's flag
        if v.endswith(flag):
            v = v[: len(v) - len(flag)].rstrip()
        else:                                          # flag inside the cell elsewhere: print the cell once
            flag = ""
    unit = f" {o['unit']}" if o.get("unit") and numeric else ""
    flag_s = f" {flag}" if flag else ""
    return f"{o.get('test', '?')}: {v}{unit}{flag_s}" + (f" (Ref. {o['ref']})" if o.get("ref") else "")


def _clean_comment(c: str) -> str:
    return re.sub(r"^\s*(?:(?:Kommentar(?: des Labors)?|Hinweis|Bemerkung|Befundkommentar)\s*:\s*)+", "", str(c)).strip()


def _status_note(note: str | None) -> str | None:
    """'Status laut Labor: corrected' duplicates the banner -> dropped; other English status words -> German."""
    if not note:
        return None
    if re.match(r"^\s*Status laut Labor\s*:\s*\w+\s*$", note):
        return None
    return re.sub(r"\b(" + "|".join(STATUS_WORD_DE) + r")\b", lambda m: STATUS_WORD_DE[m.group(1)], note)


def _is_fhir_url(u: str) -> bool:
    return bool(re.search(r"/fhir/[A-Z][A-Za-z]+/[^/]+/?$", str(u or "")))


def _is_requester(einsender, d: dict) -> bool:
    """The Einsender on the document is the doctor reading the card (or just her clinic): not worth a line."""
    rq = d.get("requester")
    name, org = (rq.get("name"), rq.get("org")) if isinstance(rq, dict) else (rq, None)
    if name and mentions_person(einsender, name):
        return True
    return bool(org) and _norm_cmp(einsender) == _norm_cmp(org)


def sender_parts(s) -> tuple[str, list[str]]:
    """Service sender line -> (name[, org], checks): 'Eva Brandl · ID Austria (Simulation) · Ärzteliste: …'
    -> ('Eva Brandl', ['ID Austria (Simulation)', 'Ärzteliste ✓']). Other shapes pass through as the name."""
    s = sender_de(s)
    segs = [x.strip() for x in str(s or "").split(" · ") if x.strip()]
    if not segs:
        return "", []
    name, checks = segs[0], []
    m = re.match(r"^(.*?)(?:,\s*|\s+\()ID Austria\s*\(?(Simulation)?\)?\)?$", name)
    if m:
        name = m.group(1).strip()
        checks.append("ID Austria ✓")
    for x in segs[1:]:
        if x.startswith("ID Austria"):
            checks.append("ID Austria ✓")
        elif x.startswith("Ärzteliste"):
            checks.append("Ärzteliste ✓")
        elif x.startswith("GDA-Index:"):
            org = x.split(":", 1)[1].split(",")[0].strip()
            name += f", {org}" if org and org not in name else ""
            checks.append("GDA-Index ✓")
        elif "nicht im Register" in x:
            checks.append("nicht im Register")
        else:
            checks.append(x)
    return name, checks


def _summary_bits(summary, concl) -> tuple[str | None, list[str]]:
    """Service summary segments -> (author name with its register check, remaining segments)."""
    segs = [x.strip() for x in str(summary or "").split(" · ") if x.strip()]
    author, rest, i = None, [], 0
    while i < len(segs):
        x = segs[i]
        if concl and concl in _clean_comment(x) or re.match(r"^(Kommentar des Labors|Kommentar)\s*:", x):
            pass
        elif x.startswith("Autor laut Dokument:"):
            author = person_name(x.split(":", 1)[1])
            if i + 1 < len(segs) and segs[i + 1].startswith("Ärzteliste"):
                author, i = f"{author} (Ärzteliste ✓)", i + 1
        else:
            rest.append(x)
        i += 1
    return author, rest


def _value_cell(o: dict) -> str:
    """Value HTML for the table: the lab's own string with its unit, the lab's own flag in bold."""
    line = _value(o)                                    # 'INR: 2,4 + (Ref. 0,8–1,2)': same rules as the text
    v = line.split(": ", 1)[1] if ": " in line else line
    if o.get("ref"):
        v = v[: -len(f" (Ref. {o['ref']})")]
    flag = str(o.get("flag") or "").strip()
    if flag and v.endswith(" " + flag):
        v = v[: -len(flag) - 1]
    else:
        flag = ""
    unit = f" {o['unit']}" if o.get("unit") and v.endswith(f" {o['unit']}") else ""
    num = v[: -len(unit)] if unit else v
    return e(num + unit) + (f" <b>{e(flag)}</b>" if flag else "")


def _filed(d: dict, pid) -> Msg:
    """Clinical facts first (status, sender, patient, sample time, values with the lab's comparator/flag,
    lab comment, Einsender), technical proof and links last."""
    pat = d.get("patient")
    pl = (pat.get("name") if isinstance(pat, dict) else pat) or ""
    dob = (pat.get("birthDate") if isinstance(pat, dict) else None) or d.get("patient_birthDate")
    marker = FILED_STATE
    head = head_text(FILED_STATE, _kind(d.get("doc_kind")), pl, _dob_part(dob),
                     f"Probe {human_time(d['sample_time'])}" if d.get("sample_time") else None)
    top, rest = [], []                     # top: status + verified sender; rest: lab's words, Einsender
    st = str(d.get("report_status") or "").lower()
    banner = None
    if st in REPORT_STATUS_DE:
        note = _status_note(d.get("status_note"))
        if d.get("replaces_ref"):
            rt = d["replaces_ref"] + (f" (Probe {human_time(d['replaces_sample'])})" if d.get("replaces_sample") else "")
        else:
            rt = _replaces_text(d.get("replaces"))
        banner = (REPORT_STATUS_DE[st].removeprefix("⚠️ "), note, rt)
        top.append(REPORT_STATUS_DE[st] + (f" – {note}" if note else "") + (f" – ersetzt Befund: {rt}" if rt else ""))
    if d.get("sender"):
        top.append(f"Von: {sender_de(d['sender'])}")
    obs = [o for o in d.get("observations") or [] if isinstance(o, dict)]
    vals = [_value(o) for o in obs]
    cf = d.get("card_facts") if isinstance(d.get("card_facts"), dict) else {}
    meds = d.get("medications") or cf.get("medications") or []
    meds = [str(m).strip() for m in meds if str(m).strip()][:6] if isinstance(meds, list) else []
    rehab = d.get("doc_kind") == "discharge_report"
    if meds and rehab:    # leads the rehab card (after a status banner)
        at = 1 if top and top[0].startswith("⚠️") else 0
        top[at:at] = ["💊 Entlassungsmedikation (laut Bericht):"] + [f"  • {m}" for m in meds]
    else:
        meds = []
    stay = d.get("stay") if isinstance(d.get("stay"), dict) else (cf.get("stay") if isinstance(cf.get("stay"), dict) else None)
    stay_txt = None
    if stay and (stay.get("start") or stay.get("end") or stay.get("text")):
        if stay.get("start") or stay.get("end"):
            stay_txt = f"{human_time(stay.get('start') or '?')} – {human_time(stay.get('end') or '?')}"
        else:
            stay_txt = str(stay["text"])
        rest.append(f"Aufenthalt: {stay_txt}")
    recs = d.get("recommendations") or cf.get("recommendations")
    if not recs and isinstance(d.get("sections"), dict):
        recs = next((v for k, v in d["sections"].items() if re.match(r"(?i)empfehl|recommend", str(k))), None)
    if isinstance(recs, str):
        recs = [x for x in re.split(r"\n+", recs) if x.strip()]
    rec_items = []
    if recs:
        rec_items = [re.sub(r"^\s*[-*•]\s*", "", str(x)).strip() for x in recs if str(x).strip()]
        rest.append("Empfehlung: " + " · ".join(rec_items[:3]) + (" …" if len(rec_items) > 3 else ""))
    concl = _clean_comment(d["conclusion"]) if d.get("conclusion") else None
    lab = d.get("doc_kind") == "lab_result"
    if concl:
        rest.append(f"Labor: „{concl}“" if lab else f"Kommentar: „{concl}“")
    einsender = d.get("einsender") if d.get("einsender") and not _is_requester(d["einsender"], d) else None
    if einsender:
        rest.append(f"Einsender laut Dokument: {einsender}")
    author, extra = _summary_bits(d.get("summary"), concl)
    if d.get("summary"):
        segs = [x.strip() for x in str(d["summary"]).split(" · ") if x.strip()]
        segs = [x for x in segs if not (concl and concl in _clean_comment(x))
                and not re.match(r"^(Kommentar des Labors|Kommentar)\s*:", x)]
        if segs:
            rest.append(" · ".join(segs))
    if author and d.get("sender") and same_person(author.split(" (")[0], sender_parts(d["sender"])[0]):
        author = None                       # the verified sender wrote it: said once, in the sender
    times = [f"{label}: {human_time(d[k])}" for k, label in (("report_time", "Befundet"), ("received_at", "Eingegangen"))
             if d.get(k)]
    tech, proof = [], []
    if d.get("validator_errors") is not None:
        n = d["validator_errors"]
        tech.append(f"HL7-Validator: {n} Fehler" + ("" if n == 0 else " ‼"))
        proof.append(f"{n} Validator-Fehler" if n == 0 else _red(f"<b>HL7-Validator: {e(n)} Fehler</b>"))
    vm, vt = d.get("values_matched"), d.get("values_total")
    if vm is not None and vt:
        label = d.get("values_label") or ("Werte" if lab else "Prüfpunkte")
        # prove() checks filed FHIR == the values as READ from the document (photo: the agreed reading),
        # not the paper itself: say exactly that
        src = " aus der Lesung" if d.get("photo_note") else ""
        tech.append(f"{vm}/{vt} {label} unverändert{src} übernommen" + ("" if vm == vt else " ‼"))
        p_txt = f"{vm}/{vt} unverändert{src} übernommen"
        proof.append(e(p_txt) if vm == vt else _red(f"<b>{e(p_txt)}</b>"))
    n_text = d.get("text_only") if isinstance(d.get("text_only"), int) and not isinstance(d.get("text_only"), bool) \
        else sum(1 for o in obs if o.get("text_only"))
    if n_text:
        tech.append(f"{n_text} als Text abgelegt (Einheit nicht zuordenbar)")
        proof.append(e(f"{n_text} als Text abgelegt (Einheit nicht zuordenbar)"))
    links, seen = [], set()

    def link(label, u):
        if u and u not in seen:
            seen.add(u)
            links.append((label, u))
    if d.get("original_url"):     # 'Original' only for a real document link, not FHIR JSON
        link("Quelle (FHIR)" if _is_fhir_url(d["original_url"]) else "Original", d["original_url"])
    link("FHIR", d.get("fhir_url"))
    for r in d.get("resources") or []:
        link(f"{r.get('type')}/{r.get('id')}", r.get("url"))

    charset = f"Hinweis: {d['charset_note']}" if d.get("charset_note") else None
    if d.get("photo_note"):                # photo intake: '📷 aus Foto gelesen (KI, 2 Lesungen übereinstimmend)'
        times = times + [str(d["photo_note"])]
    t = [head] + top + [f"  {v}" for v in vals] + rest + times + ([charset] if charset else [])
    if tech:
        t.append("Geprüft: " + " · ".join(tech))
    if links:
        t.append(" · ".join(f"{k}: {u}" for k, u in links))
    if pid:
        t.append(f"Anfrage: {pid} · !audit {pid}")

    # ---- HTML: headline · status · content · one quiet line · links
    h = [_headline(marker, _kind(d.get("doc_kind")), pl, _dob_part(dob))]
    if stay_txt:                           # the stay frames the whole report: its own line, first
        h.append(f"<p>Aufenthalt {e(_span_de(stay))}</p>" if stay.get("start") and stay.get("end")
                 else f"<p>Aufenthalt {e(stay_txt)}</p>")
    if banner:
        word, note, rt = banner
        h.append(f"<p><b>{e(word)}</b>" + (f" – {e(note)}" if note else "") + (f" – ersetzt {e(rt)}" if rt else "") + "</p>")
    if meds:
        h.append("<p><b>Entlassungsmedikation</b></p>" + _ul(e(m) for m in meds))
    if obs:
        rows = []
        for o in obs:
            cell = _value_cell(o)
            rows.append([e(o.get("test", "?")), cell, e(o["ref"]) if o.get("ref") else ""])
        h.append(_table(["Test", "Wert", "Referenz"], rows))
    if rec_items:
        h.append("<p><b>Empfehlung</b></p>" + _ul(e(x) for x in rec_items[:3]) + ("<p>…</p>" if len(rec_items) > 3 else ""))
    if concl:
        h.append(f"<blockquote>{'Labor' if lab else 'Kommentar'}: „{e(concl)}“</blockquote>")
    for x in extra:                        # e.g. an override decision: the service's words
        h.append(f"<p>{e(x)}</p>")
    if charset:
        h.append(f"<p>{e(charset)}</p>")
    sname, schecks = sender_parts(d.get("sender")) if d.get("sender") else ("", [])
    who = ([e(f"Probe {human_time(d['sample_time'])}")] if d.get("sample_time") else []) \
        + ([e(SEP.join([sname] + schecks))] if sname else [])
    doc = ([e("Verfasst von " + author.replace(" (Ärzteliste ✓)", " · Ärzteliste ✓"))] if author else []) \
        + ([e(f"Einsender {einsender}")] if einsender else []) \
        + ([e(re.sub(r"^[^\w(]+", "", str(d["photo_note"])))] if d.get("photo_note") else [])
    h.append(_quiet(who, doc, proof + ([e(pid)] if pid else []) + _link_items(_short_links(links))))
    return "\n".join(t), "".join(x for x in h if x)


def _span_de(stay: dict) -> str:
    """{'start': '2026-09-02', 'end': '2026-09-23'} -> '02.09.–23.09.2026' (year once when shared)."""
    a, b = human_time(stay["start"]), human_time(stay["end"])
    if len(a) == len(b) == 10 and a[6:] == b[6:]:
        return f"{a[:6]}–{b}"
    return f"{a} – {b}"


def _short_links(links: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """'Provenance/2392' -> 'Provenance', 'DiagnosticReport/2387' (same as FHIR) dropped: short labels only."""
    out = []
    for k, u in links:
        k = k.split("/")[0] if "/" in k else k
        if k in ("DiagnosticReport", "DocumentReference") and any(x[0] in ("FHIR", "Quelle (FHIR)") for x in out):
            continue
        out.append((k, u))
    return out

"""The outside person: open the link, log in with the (mock) ID Austria, upload the document, send.

Only what a person sees and does: HTML pages, visible forms, links, buttons. Every action is logged,
so the effort metric (installs, accounts, clicks, keystrokes) is counted, not asserted.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .web import Browser, Field, Form, Page

EID_HINT = re.compile(r"eid|id[ -]?austria|login|anmeld|ausweis|identif", re.I)
CONTINUE = re.compile(r"best[äa]tig|confirm|weiter|continue|fortfahren|freigeben|approve|zustimm|allow|ok\b|"
                      r"anmelden|log ?in|sign ?in|id[ -]?austria", re.I)
SEND = re.compile(r"senden|absenden|send|submit|hochladen|upload|übermitteln|abschicken|best[äa]tig|confirm", re.I)
NEGATIVE = re.compile(r"ablehn|decline|reject|nicht mein|not (my|our)|abbrechen|cancel|zurück|back|logout|abmelden",
                      re.I)


@dataclass
class Person:
    given: str
    family: str
    birthDate: str
    bpk: str | None = None
    pin: str | None = None  # mock ID Austria per-persona PIN; see PGK_EID_PINS


@dataclass
class Visit:
    """What happened to one person on one link."""
    pages: list[Page] = field(default_factory=list)
    actions: list[tuple[str, str]] = field(default_factory=list)  # (kind, detail)
    t: dict[str, float] = field(default_factory=dict)            # monotonic marks
    answer_form_seen: bool = False
    submitted: bool = False
    result: Page | None = None
    error: str | None = None
    landing: Page | None = None
    after_eid: Page | None = None
    raw_sha256: str | None = None     # SHA-256 of the exact bytes uploaded
    receipt: str | None = None        # the Eingangsbestätigung as served to this person

    def act(self, kind: str, detail: str = "") -> None:
        self.actions.append((kind, detail))


def _key(f: Field) -> str:
    return f"{f.name} {f.label}".casefold()


def answer_form(page: Page) -> Form | None:
    for f in page.forms:
        if any(x.type == "file" for x in f.fields) or any(x.tag == "textarea" for x in f.fields):
            return f
    return None


def _eid_form(page: Page) -> Form | None:
    for f in page.forms:
        keys = " ".join(_key(x) for x in f.fields if x.type not in ("hidden", "submit"))
        if re.search(r"given|vorname|first|family|nachname|surname|birth|geburt", keys):
            return f
    return None


def _button(form: Form, pat: re.Pattern) -> Field | None:
    good = [b for b in form.buttons if pat.search(f"{b.label} {b.value} {b.name}") and not NEGATIVE.search(b.label)]
    return good[0] if good else next((b for b in form.buttons if not NEGATIVE.search(f"{b.label} {b.value}")), None)


class OutsidePerson:
    def __init__(self, who: Person, timeout: float | None = None):
        self.who, self.b = who, (Browser(timeout) if timeout else Browser())

    # -- step 1: open the link (nothing may be shown yet)
    def open(self, link: str, v: Visit | None = None) -> Visit:
        v = v or Visit()
        v.t["open"] = time.monotonic()
        v.t["open_wall"] = time.time()
        v.act("click", "link in message")
        v.landing = self.b.get(link)
        v.pages.append(v.landing)
        return v

    # -- step 2: ID Austria (mock); returns the first page after the identity check
    def login(self, v: Visit, max_steps: int = 8) -> Page:
        page = v.landing
        for _ in range(max_steps):
            if answer_form(page):
                break
            ef = _eid_form(page)
            if ef:
                vals = self._fill_eid(ef)
                v.act("eid", f"{self.who.given} {self.who.family}")
                page = self.b.submit(page, ef, vals, button=_button(ef, CONTINUE))
            else:
                nxt = self._continue(page)
                if nxt is None:
                    break
                kind, target = nxt
                v.act("click", kind)
                page = target
            v.pages.append(page)
        v.t["eid_done"] = time.monotonic()
        v.after_eid = page
        v.answer_form_seen = answer_form(page) is not None
        return page

    def _continue(self, page: Page) -> tuple[str, Page] | None:
        # auto-post forms (all hidden), consent/confirm buttons, then eID links
        for f in page.forms:
            visible = [x for x in f.fields if x.type != "hidden"]
            btn = _button(f, CONTINUE)
            if btn is not None and NEGATIVE.search(f"{btn.label} {btn.value}"):
                continue
            if not visible or (btn and CONTINUE.search(f"{btn.label} {btn.value}")):
                return "continue", self.b.submit(page, f, {}, button=btn)
        for href, text in page.links:
            if EID_HINT.search(href) or EID_HINT.search(text):
                return "eid link", self.b.get(_join(page.url, href))
        return None

    def _fill_eid(self, f: Form) -> dict[str, str]:
        vals = {}
        for x in f.fields:
            k = _key(x)
            if x.type == "hidden":
                continue
            if re.search(r"given|vorname|first", k):
                vals[x.name] = self.who.given
            elif re.search(r"family|nachname|surname|last", k):
                vals[x.name] = self.who.family
            elif re.search(r"birth|geburt|dob", k):
                vals[x.name] = self.who.birthDate
            elif re.search(r"\bpin\b|kennwort|code", k) and x.type != "hidden":
                vals[x.name] = self.who.pin or ""
            elif "bpk" in k:
                vals[x.name] = self.who.bpk or x.value or f"GH:test-{self.who.given}-{self.who.family}"
            elif x.type in ("checkbox",):
                vals[x.name] = x.value or "on"
            elif re.fullmatch(r"\s*name\s*", x.name or ""):
                vals[x.name] = f"{self.who.given} {self.who.family}"
        return vals

    # -- step 3: upload and send
    def answer(self, v: Visit, data: bytes, filename: str = "answer.md", max_confirm: int = 3) -> Page | None:
        page = v.after_eid
        form = answer_form(page) if page else None
        if form is None:
            v.error = "no answer form after identity check"
            return None
        vals, files = {}, {}
        fileinp = next((x for x in form.fields if x.type == "file"), None)
        ta = next((x for x in form.fields if x.tag == "textarea"), None)
        import hashlib
        v.raw_sha256 = hashlib.sha256(data).hexdigest()
        if fileinp is not None:
            files[fileinp.name] = (filename, data, "text/markdown")
            v.act("file", filename)
        elif ta is not None:
            vals[ta.name] = data.decode("utf-8", "replace")
            v.act("paste", ta.name)
        for x in form.fields:  # required confirmations a person would tick
            if x.type == "checkbox" and (x.required or re.search(r"best[äa]tig|confirm|richtig|correct", _key(x))):
                vals[x.name] = x.value or "on"
                v.act("check", x.name)
            elif x.type == "radio" and x.name not in vals and re.search(r"kind|art|typ|upload|datei|file", _key(x)):
                vals[x.name] = x.value
        v.t["send"] = time.monotonic()
        v.t["send_wall"] = time.time()
        v.act("click", "send")
        page = self.b.submit(page, form, vals, files, button=_button(form, SEND))
        v.pages.append(page)
        for _ in range(max_confirm):  # optional preview -> confirm step
            nxt = [f for f in page.forms if not answer_form(page) and _button(f, SEND)
                   and SEND.search(f"{_button(f, SEND).label} {_button(f, SEND).value}")]
            if not nxt:
                break
            v.act("click", "confirm")
            page = self.b.submit(page, nxt[0], {}, button=_button(nxt[0], SEND))
            v.pages.append(page)
        v.t["done"] = time.monotonic()
        v.submitted, v.result = True, page
        try:  # the receipt the person can save: same session, the link + /receipt.txt
            link = v.landing.url.split("?")[0].rstrip("/") if v.landing else ""
            link = re.sub(r"(/r/[^/]+).*$", r"\1", link)
            rc = self.b.get(link + "/receipt.txt")
            v.receipt = rc.html if rc.status == 200 else None
            self.b.history.pop()
        except Exception:
            v.receipt = None
        return page


def _join(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)


# ------------------------------------------------------------------ effort model
# Keystroke-Level Model (Card, Moran & Newell 1980): K 0.28 s (average typist), P 1.1 s, B 0.1+0.1 s,
# H 0.4 s, M 1.35 s. The real ID Austria step (push to the app, biometric confirm) is taken as 12 s.
KLM = {"M": 1.35, "P": 1.1, "B": 0.2, "K": 0.28, "H": 0.4, "EID": 12.0}


def klm_seconds(v: Visit, email_read_s: float = 5.0) -> dict:
    """Estimated human time from opening the message to sending, from the actions actually needed."""
    parts = {"read message": email_read_s}
    for kind, _ in v.actions:
        if kind == "click":
            parts["clicks"] = parts.get("clicks", 0) + KLM["M"] + KLM["P"] + KLM["B"]
        elif kind == "eid":
            parts["ID Austria"] = parts.get("ID Austria", 0) + KLM["EID"]
        elif kind == "file":
            parts["pick file"] = parts.get("pick file", 0) + KLM["M"] + 3 * (KLM["P"] + KLM["B"])
        elif kind == "paste":
            parts["paste"] = parts.get("paste", 0) + KLM["M"] + KLM["H"] + 2 * KLM["K"] + KLM["P"] + KLM["B"]
        elif kind == "check":
            parts["tick boxes"] = parts.get("tick boxes", 0) + KLM["P"] + KLM["B"]
    pages = len(v.pages)
    parts["read pages"] = pages * KLM["M"] * 2
    return {"total_s": round(sum(parts.values()), 1), "parts": {k: round(x, 1) for k, x in parts.items()},
            "pages": pages}

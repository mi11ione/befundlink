"""Stdlib HTTP: JSON calls, polling with backoff, and a form-following browser (cookies, multipart).

The browser drives the outside-person pages exactly like a person would: it reads the HTML it is
served, finds forms and links, fills visible fields and submits. It never calls internal APIs.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser

UA = "pgk-e2e/1.0 (outside-person simulator)"
TIMEOUT = float(os.environ.get("PGK_HTTP_TIMEOUT", "30"))  # per HTTP call


LATENCY: list[tuple[str, str, float]] = []  # (method, route class, seconds) for every HTTP call; list.append is atomic


def route(url: str) -> str:
    """Route class: tokens, ids and query strings collapsed (e.g. GET /r/{token}/answer)."""
    path = urllib.parse.urlsplit(url).path
    path = re.sub(r"/r/[^/]+", "/r/{token}", path)
    path = re.sub(r"/(req-[0-9a-f]+|[0-9a-f-]{16,}|\d+)(?=/|$)", "/{id}", path)
    return f"{urllib.parse.urlsplit(url).port or ''}{path}"


def _timed(method: str, url: str, t0: float) -> None:
    LATENCY.append((method, route(url), time.monotonic() - t0))


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status, self.body, self.url = status, body, url


def call(method: str, url: str, body=None, headers: dict | None = None, timeout: float = TIMEOUT,
         ok=(200, 201, 202, 204)) -> tuple[int, dict | list | str, dict]:
    data, hdrs = None, {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, rh = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, rh = e.read(), e.code, dict(e.headers or {})
    finally:
        _timed(method, url, t0)
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text.strip()[:1] in ("{", "[") else text
    except ValueError:
        parsed = text
    if ok and status not in ok:
        raise HttpError(status, text, url)
    return status, parsed, rh


def poll(fn, timeout: float = 20, first: float = 0.05, factor: float = 1.6, cap: float = 1.0, what: str = ""):
    """Call fn() until it returns a truthy value; exponential backoff; raise TimeoutError."""
    deadline, delay = time.monotonic() + timeout, first
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what or fn}")
        time.sleep(delay)
        delay = min(cap, delay * factor)


# ------------------------------------------------------------------ HTML forms
@dataclass
class Field:
    tag: str
    name: str
    type: str = "text"
    value: str = ""
    checked: bool = False
    label: str = ""
    options: list[str] = field(default_factory=list)
    required: bool = False
    placeholder: str = ""


@dataclass
class Form:
    action: str
    method: str
    enctype: str
    fields: list[Field] = field(default_factory=list)
    buttons: list[Field] = field(default_factory=list)
    text: str = ""


@dataclass
class Page:
    url: str
    status: int
    html: str
    forms: list[Form]
    links: list[tuple[str, str]]  # (href, text)
    text: str                      # visible text
    meta_refresh: str | None = None


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self._form: Form | None = None
        self._a: list | None = None
        self._ta: Field | None = None
        self._sel: Field | None = None
        self._label_for: str | None = None
        self._label_txt: list[str] = []
        self.labels: dict[str, str] = {}
        self._skip = 0
        self._btn: Field | None = None
        self.refresh = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style", "template"):
            self._skip += 1
        if tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            self.refresh = a.get("content", "")
        if tag == "form":
            self._form = Form(a.get("action", ""), a.get("method", "get").lower(),
                              a.get("enctype", "application/x-www-form-urlencoded"))
            self.forms.append(self._form)
        elif tag == "a" and "href" in a:
            self._a = [a["href"], []]
        elif tag == "label":
            self._label_for, self._label_txt = a.get("for"), []
        elif tag in ("input", "button") and self._form is not None:
            typ = a.get("type", "submit" if tag == "button" else "text").lower()
            f = Field(tag, a.get("name", ""), typ, a.get("value", ""), "checked" in a, a.get("aria-label", ""),
                      required="required" in a, placeholder=a.get("placeholder", ""))
            f.label = f.label or a.get("id", "")
            (self._form.buttons if typ in ("submit", "image") or tag == "button" else self._form.fields).append(f)
            if tag == "button":
                self._btn = f
        elif tag == "textarea" and self._form is not None:
            self._ta = Field("textarea", a.get("name", ""), "textarea", "", label=a.get("id", ""),
                             required="required" in a, placeholder=a.get("placeholder", ""))
            self._form.fields.append(self._ta)
        elif tag == "select" and self._form is not None:
            self._sel = Field("select", a.get("name", ""), "select", label=a.get("id", ""))
            self._form.fields.append(self._sel)
        elif tag == "option" and self._sel is not None:
            self._sel.options.append(a.get("value", ""))
            if "selected" in a or not self._sel.value:
                self._sel.value = a.get("value", "")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "template"):
            self._skip = max(0, self._skip - 1)
        if tag == "button":
            self._btn = None
        if tag == "form":
            self._form = None
        elif tag == "a" and self._a:
            self.links.append((self._a[0], " ".join(self._a[1]).strip()))
            self._a = None
        elif tag == "textarea":
            self._ta = None
        elif tag == "select":
            self._sel = None
        elif tag == "label" and self._label_for:
            self.labels[self._label_for] = " ".join(self._label_txt).strip()
            self._label_for = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._ta is not None:
            self._ta.value += data
            return
        s = data.strip()
        if not s:
            return
        self.text.append(s)
        if self._a is not None:
            self._a[1].append(s)
        if self._label_for is not None:
            self._label_txt.append(s)
        if self._form is not None:
            self._form.text += " " + s
            if self._btn is not None and not self._btn.label:
                self._btn.label = s


def parse_page(url: str, status: int, html: str) -> Page:
    p = _Parser()
    try:
        p.feed(html)
    except Exception:  # malformed HTML: keep what we have
        pass
    for f in p.forms:
        for fl in f.fields:
            fl.label = p.labels.get(fl.label, "") + " " + fl.label + " " + fl.placeholder
    return Page(url, status, html, p.forms, p.links, " ".join(p.text), p.refresh)


def _multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields:
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v.encode() + b"\r\n"
    for k, fn, data, ctype in files:
        out += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


class Browser:
    """One person's browser: own cookie jar, follows redirects, records every page it saw."""

    def __init__(self, timeout: float = TIMEOUT):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.timeout = timeout
        self.history: list[Page] = []
        self.requests = 0

    def _open(self, req: urllib.request.Request) -> Page:
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "text/html,application/xhtml+xml")
        self.requests += 1
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                html, status, url = r.read().decode("utf-8", "replace"), r.status, r.geturl()
        except urllib.error.HTTPError as e:
            html, status, url = e.read().decode("utf-8", "replace"), e.code, e.geturl()
        finally:
            _timed(req.get_method(), req.full_url, t0)  # includes redirects followed
        page = parse_page(url, status, html)
        self.history.append(page)
        # a browser honours the delay: follow only zero-delay refreshes (redirects), not self-refreshing status pages
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*;\s*url\s*=\s*(.+)", page.meta_refresh or "", re.I)
        if m and float(m.group(1)) == 0 and len(self.history) < 50:
            return self.get(urllib.parse.urljoin(url, m.group(2).strip(" '\"")))
        return page

    def get(self, url: str) -> Page:
        return self._open(urllib.request.Request(url, method="GET"))

    def head(self, url: str) -> int:
        try:
            with self.opener.open(urllib.request.Request(url, method="HEAD"), timeout=self.timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def submit(self, page: Page, form: Form, values: dict[str, str], files: dict[str, tuple[str, bytes, str]] = {},
               button: Field | None = None) -> Page:
        pairs: list[tuple[str, str]] = []
        for f in form.fields:
            if not f.name or f.type == "file":
                continue
            if f.type in ("checkbox", "radio"):
                if f.name in values:
                    if f.type == "checkbox" and values[f.name] in ("on", f.value or "on"):
                        pairs.append((f.name, f.value or "on"))
                    elif f.type == "radio" and values[f.name] == f.value and (f.name, f.value) not in pairs:
                        pairs.append((f.name, f.value))
                elif f.checked:
                    pairs.append((f.name, f.value or "on"))
                continue
            pairs.append((f.name, values.get(f.name, f.value)))
        if button is not None and button.name:
            pairs.append((button.name, button.value))
        url = urllib.parse.urljoin(page.url, form.action or page.url)
        if form.method == "get":
            q = urllib.parse.urlencode(pairs)
            return self.get(url + ("&" if "?" in url else "?") + q)
        if files or "multipart" in form.enctype:
            fl = [(k, fn, data, ct) for k, (fn, data, ct) in files.items()]
            body, ctype = _multipart(pairs, fl)
        else:
            body, ctype = urllib.parse.urlencode(pairs).encode(), "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
        return self._open(req)


def latency_summary(rows=None) -> dict:
    """p50/p95/max per route class (service routes only: port 8000 or the PGK_BASE_URL port)."""
    rows = list(LATENCY if rows is None else rows)
    by: dict[str, list[float]] = {}
    for m, r, t in rows:
        by.setdefault(f"{m} {r}", []).append(t)
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]
    return {k: {"n": len(v), "p50": round(q(v, .5), 3), "p95": round(q(v, .95), 3), "max": round(max(v), 3)}
            for k, v in sorted(by.items())}

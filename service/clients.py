"""Outbound integrations: HAPI FHIR, Matrix bot (/notify), SMTP (Mailpit)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

log = logging.getLogger("pgk.clients")
FHIR_JSON = "application/fhir+json"


class FhirError(Exception):
    pass


class Hapi:
    def __init__(self, base: str, http: httpx.Client):
        self.base, self.http = base, http

    def get_patient(self, pid: str) -> dict | None:
        r = self.http.get(f"{self.base}/Patient/{pid}", headers={"Accept": FHIR_JSON})
        if r.status_code in (404, 410):
            return None
        if r.status_code >= 400:
            raise FhirError(f"GET Patient/{pid}: HTTP {r.status_code}")
        return r.json()

    def create(self, resource: dict) -> str:
        r = self.http.post(f"{self.base}/{resource['resourceType']}", json=resource, headers={"Content-Type": FHIR_JSON})
        if r.status_code >= 400:
            raise FhirError(f"POST {resource['resourceType']}: HTTP {r.status_code} {r.text[:300]}")
        loc = r.headers.get("Location") or r.headers.get("Content-Location") or ""
        if loc:
            return _strip_history(loc.split(self.base + "/")[-1])
        body = r.json()
        return f"{body['resourceType']}/{body['id']}"

    def search(self, resource_type: str, params: dict) -> list[dict]:
        """FHIR search (one page is enough for identifier lookups)."""
        r = self.http.get(f"{self.base}/{resource_type}", params={**params, "_count": 50}, headers={"Accept": FHIR_JSON})
        if r.status_code >= 400:
            raise FhirError(f"search {resource_type}: HTTP {r.status_code}")
        return [e["resource"] for e in r.json().get("entry", []) if "resource" in e]

    def read(self, ref: str) -> dict:
        r = self.http.get(f"{self.base}/{ref}", headers={"Accept": FHIR_JSON})
        if r.status_code >= 400:
            raise FhirError(f"read {ref}: HTTP {r.status_code}")
        return r.json()

    def transaction(self, entries: list[dict]) -> list[str]:
        """POST a transaction Bundle of prepared entries. Returns 'Type/id' per entry, in order."""
        return [ref for ref, _version in self.transaction_versioned(entries)]

    def transaction_versioned(self, entries: list[dict]) -> list[tuple[str, str | None]]:
        """Like transaction, with the version each entry was written as: [('Type/id', versionId | None)]."""
        bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
        r = self.http.post(self.base, json=bundle, headers={"Content-Type": FHIR_JSON}, timeout=60)
        if r.status_code >= 400:
            raise FhirError(f"transaction: HTTP {r.status_code} {r.text[:500]}")
        out = []
        for req_e, resp_e in zip(entries, r.json().get("entry", [])):
            loc = (resp_e.get("response") or {}).get("location") or ""
            if not loc:
                out.append((req_e["request"]["url"], None))
                continue
            ref = loc.split(self.base + "/")[-1]
            out.append((_strip_history(ref), _version_of(ref)))
        return out

    def read_version(self, ref: str, version: str | None) -> dict:
        """A resource as it was at `version` (history is immutable on HAPI); the current one without a version."""
        if not version:
            return self.read(ref)
        return self.read(f"{_strip_history(ref)}/_history/{version}")


def _strip_history(ref: str) -> str:
    return ref.split("/_history")[0]


def _version_of(ref: str) -> str | None:
    """'Observation/12/_history/3' -> '3'."""
    if "/_history/" not in ref:
        return None
    return ref.split("/_history/")[1].split("/")[0] or None


class Bot:
    def __init__(self, base: str, http: httpx.Client, notify_key: str | None = None):
        self.base, self.http, self.notify_key = base, http, notify_key

    def notify(self, room_id: str | None, text: str, html: str | None = None, kind: str | None = None,
               data: dict | None = None, **extra) -> str | bool:
        """POST /notify. kind: 'filed' | 'hold' | 'misdelivery' (structured card in the bot) or plain text.
        Without room_id the bot posts to its default room."""
        payload = {"text": text}
        if room_id:
            payload["room_id"] = room_id
        if html:
            payload["html"] = html
        if kind:
            payload["kind"] = kind
        if data is not None:
            payload["data"] = data
        payload.update({k: v for k, v in extra.items() if v is not None})
        try:
            headers = {"Content-Type": "application/json"}
            if self.notify_key:
                headers["X-PGK-Notify-Key"] = self.notify_key
            r = self.http.post(f"{self.base}/notify", json=payload, headers=headers, timeout=10)
            if r.status_code >= 300:
                log.error("bot notify HTTP %s: %s", r.status_code, r.text[:200])
                return False
            try:
                return r.json().get("event_id") or True   # the Matrix event id (for anchors: read back later)
            except ValueError:
                return True
        except httpx.HTTPError as e:
            log.error("bot notify failed: %s", e)
            return False


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str, html: str | None = None) -> None: ...


class SmtpMailer:
    def __init__(self, host: str, port: int, sender: str):
        self.host, self.port, self.sender = host, port, sender

    def send(self, to: str, subject: str, body: str, html: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        if html:  # multipart/alternative: the plain text stays the first part
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=10) as s:
            s.send_message(msg)

"""Service logic independent of HTTP routing: requests, sending, audit, identity, checks, filing, decisions."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx

from . import anchors as anchorlib
from . import audit as auditlib
from . import lookalike, messages, retention
from .reasons_de import TECHNICAL, reasons_de
from .clients import Bot, FhirError, Hapi, Mailer, SmtpMailer
from .config import Settings, check_secrets
from .crypto import Box
from .eid import MockIdP, split_person_name
from .keys import ES256Key, derive, sign_jws
from .validator import ValidatorServer
from .workers import AnswerWorker, Outbox
import importlib
import inspect

import fhirmap
import fhirmap.build
import fhirmap.loinc
import fhirmap.transcribe
from fhirmap.identity import rule_tokens

from fhirmap.identity import loose_name_equal  # noqa: E402

fhirmap_validate = importlib.import_module("fhirmap.validate")
# capability check: fhirmap versions with resources_sha256 verify the filed clinical content after read-back
_SIGNATURE_COVERS_CONTENT = "resources" in inspect.signature(fhirmap.verify_provenance_signature).parameters
CLINICAL_TYPES = tuple(getattr(fhirmap.build, "CLINICAL_TYPES", ("DiagnosticReport", "Observation", "DocumentReference")))  # the module (fhirmap.validate is also a function name)

from .fm import FhirMap, get
from .registers import Registers, org_key
from .store import Store

log = logging.getLogger("pgk.core")

DOC_KIND_ALIASES = {
    "lab_result": "lab_result", "lab": "lab_result", "labor": "lab_result", "laborbefund": "lab_result",
    "discharge_report": "discharge_report", "discharge": "discharge_report", "reha": "discharge_report",
    "rehab": "discharge_report", "entlassungsbericht": "discharge_report",
}
DOC_KIND_LABEL = {"lab_result": "Laborbefund", "discharge_report": "Reha-Entlassungsbericht"}
_REF_ALPHABET = "23456789CDFGHJKMNPQRTVWXYZ"  # no vowels -> no accidental words in the code
# Link tokens: 60 chars from 19 lower-case consonants (no digits, no vowels) = 254 bit. They can never
# contain a date, a number or any (vowel-bearing) word, so no PII scanner can trip over the link.
TOKEN_ALPHABET = "bcdfghjkmnpqrstvwxz"
TOKEN_LEN = 60
TOKEN_RE = re.compile(rf"[{TOKEN_ALPHABET}]{{{TOKEN_LEN}}}")

# Request status -> FHIR Task.status
TASK_STATUS = {"needs_confirmation": "draft", "sent": "requested", "opened": "requested", "blocked_identity": "requested",
               "identity_verified": "in-progress", "processing": "in-progress", "held_review": "on-hold",
               "held_technical": "on-hold", "filed": "completed", "rejected": "rejected", "cancelled": "cancelled",
               "locked": "failed", "send_failed": "failed", "declined": "rejected", "identity_review": "on-hold"}
TERMINAL_TASK = {"completed", "rejected", "cancelled"}
ANSWERABLE = {"sent", "opened", "blocked_identity", "identity_verified", "held_review", "held_technical"}
CLOSED = {"filed", "rejected", "cancelled", "locked", "send_failed", "needs_confirmation", "declined"}


VIENNA = ZoneInfo("Europe/Vienna")


def now() -> datetime:
    """Server time with the Europe/Vienna offset (+01:00 / +02:00), used for every stored/filed timestamp."""
    return datetime.now(VIENNA).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def sha256(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


WRONG_PATIENT_TEXT = "Dokument betrifft eine andere Person (Name/Geburtsdatum stimmen nicht überein) – nicht abgelegt."


def normalise_contact(channel: str, address: str) -> str:
    """E-mail address lower-cased, without a plus-tag (eva+labor@x -> eva@x); phone number as its digits only."""
    if channel == "email":
        local, _, domain = address.strip().lower().partition("@")
        return f"{local.split('+')[0]}@{domain}" if domain else local
    return re.sub(r"\D", "", address)


def mask_contact(channel: str, address: str) -> str:
    """The contact as shown in Matrix: enough to tell WHICH address (the domain, the last digits), not the address.
    'eva.brandl@labor-kanaI.example' -> 'e***@labor-kanaI.example'; '+43 660 000 0101' -> '+43***01'."""
    if channel == "email":
        # the domain exactly as typed: its case IS the attack (labor-kanaI with a capital I)
        local, _, domain = address.strip().rpartition("@")
        return f"{local[:1].lower()}***@{domain}"
    norm = normalise_contact(channel, address)
    digits = norm
    prefix = "+" + digits[:2] if address.strip().startswith("+") else digits[:2]
    return f"{prefix}***{digits[-2:]}"


def contact_label(contact_hash: str) -> str:
    """How the audit text names a contact: by its keyed hash, never in clear."""
    return f"Kontakt #{contact_hash[:12]}"


def _retry(fn, safe_on: tuple, waits: tuple[float, ...] = (0.5, 1, 2, 4, 8)):
    """Retry `fn` on the given (transient) exception types with backoff (HAPI restarts: ~15 s covered)."""
    for w in (*waits, None):
        try:
            return fn()
        except safe_on as e:
            if w is None:
                raise
            log.warning("transient %s: %s - retry in %ss", type(e).__name__, e, w)
            time.sleep(w)


def fhir_patient_name(patient: dict) -> str:
    names = patient.get("name") or []
    chosen = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    if chosen.get("text") and not chosen.get("family"):
        return chosen["text"]
    return " ".join([*(chosen.get("given") or []), chosen.get("family") or ""]).strip()


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


class Service:
    def __init__(self, settings: Settings, mailer: Mailer | None = None, http: httpx.Client | None = None):
        check_secrets(settings)  # fail closed (app and CLI alike)
        self.s = settings
        self.store = Store(settings.db_path)
        self.http = http or httpx.Client(timeout=15)
        self.hapi = Hapi(settings.hapi_url, self.http)
        self.bot = Bot(settings.bot_url, self.http, settings.notify_key)
        self.mailer = mailer or SmtpMailer(settings.smtp_host, settings.smtp_port, settings.mail_from)
        self.fm = FhirMap(settings.validate_mode)
        self.registers = Registers(settings.registers_path)
        self.extractor = None              # photo intake: only with PGK_EXTRACTOR=anthropic (health data leaves the stack)
        if settings.extractor == "anthropic":
            from .extractor import AnthropicExtractor
            self.extractor = AnthropicExtractor()
        key_file = Path(settings.db_path).parent / ".datakey" if settings.db_path != ":memory:" else Path("/nonexistent")
        self.box = Box(settings.data_key_hex or (secrets.token_hex(32) if settings.db_path == ":memory:" else None), key_file)
        self.outbox = Outbox(inline=not settings.async_io)
        self.worker = AnswerWorker(self.process_batch, inline=not settings.async_io)
        self.validator = ValidatorServer(settings.validator_port) if settings.validator_server else None  # no log: no PII
        self._verified = {"id": 0, "hash": auditlib.GENESIS, "count": 0}
        self.k_csrf, self.k_pin = derive(settings.secret, "csrf"), derive(settings.secret, "pin")
        self.k_audit = derive(settings.secret, "audit-chain")
        self.k_contact = derive(settings.secret, "contact")  # salts the contact hashes in the audit
        self.chain_id = anchorlib.chain_id(self.k_audit)
        self._pcache: dict[str, tuple[float, dict | None]] = {}
        data_dir = Path(settings.db_path).parent if settings.db_path != ":memory:" else None
        self.service_key = ES256Key(data_dir / "service_es256.pem" if data_dir else None)  # signs bound sender statements
        self.idp = MockIdP(self.store, ES256Key(data_dir / "idp_es256.pem" if data_dir else None), self.k_pin,
                           settings.bpk_salt, issuer=f"{settings.base_url}/eid",
                           personas_file=Path(settings.registers_path).parent / "personas.json",
                           protected_extra=self._protected_names, ttl_s=settings.assertion_ttl_s)
        self._verify_lock = threading.Lock()
        self._demo_identities: dict[str, dict] = {}  # request id -> {name, pin}, handed out once (PGK_DEMO)
        self.backfill_identity_memory()  # identity memory keyed by recipient name + org (was: name + exact contact)

    def start(self) -> None:
        """Background machinery (app startup). Nothing here blocks."""
        self.outbox.start()
        self.worker.start()
        if self.validator:
            self.validator.start()
        self.recover()

    def stop(self) -> None:
        self.outbox.drain(10)
        if self.validator:
            self.validator.stop()

    def recover(self) -> int:
        """After a restart: answers that were accepted but not yet filed go back into the worker queue."""
        rows = self.store.q("SELECT a.id, a.request_id FROM answers a JOIN requests r ON r.id=a.request_id "
                            "WHERE r.status='processing' AND a.outcome IS NULL AND a.content IS NOT NULL ORDER BY a.id")
        latest = {}
        for row in rows:
            latest[row["request_id"]] = row["id"]
        for rid, aid in latest.items():
            self.worker.submit(rid, aid, None)
        return len(latest)

    def notify(self, room_id: str | None, text: str, html: str | None = None, **kw) -> None:
        """Matrix message via the bot, queued (ordered) so no request waits for it."""
        self.outbox.put(self.bot.notify, room_id, text, html, desc="notify", **kw)

    # ---------------------------------------------------------------- the requester's FHIR identity
    def requester_refs(self, mxid: str | None) -> dict:
        """FHIR references of a PGK member and her organisation: {'practitioner': 'Practitioner/lena-hofer',
        'organization': 'Organization/klinikum-beispielstadt'}; {} for anyone else. Created by infra/setup.py and,
        if missing, by ensure_directory() at startup."""
        member = self.registers.member(mxid or "")
        if not member:
            return {}
        given, family = split_person_name(member["name"])
        return {"practitioner": f"Practitioner/{fhirmap.slug(f'{given} {family}')}",
                "organization": f"Organization/{fhirmap.slug(member.get('org') or self.s.clinic_name)}"}

    def directory_resources(self) -> list[dict]:
        """Practitioner + Organization of every PGK member (same content as infra/directory.json)."""
        out, orgs = [], set()
        for member in (self.registers.data.get("network") or {}).get("members", []):
            refs = self.requester_refs(member["mxid"])
            given, family = split_person_name(member["name"])
            prefix = [w for w in member["name"].split() if w.endswith(".")]
            out.append({"resourceType": "Practitioner", "id": refs["practitioner"].split("/")[1], "active": True,
                        "identifier": [{"system": fhirmap.build.SID_MXID, "value": member["mxid"]}],   # not urn:ietf:rfc:3986: AT Core reads that as the GDA-OID
                        "name": [{"use": "official", "text": member["name"], "family": family, "given": given.split(),
                                  **({"prefix": prefix} if prefix else {})}]})
            org_id = refs["organization"].split("/")[1]
            if org_id not in orgs:
                orgs.add(org_id)
                out.append({"resourceType": "Organization", "id": org_id, "active": True,
                            "identifier": [{"system": fhirmap.build.SID_ORG, "value": org_id}],
                            "name": member.get("org") or self.s.clinic_name})
        return out

    def ensure_directory(self) -> list[str]:
        """Create the members' Practitioner/Organization on HAPI if missing (never overwrites). Returns problems."""
        problems = []
        for res in self.directory_resources():
            url = f"{self.s.hapi_url}/{res['resourceType']}/{res['id']}"
            try:
                if self.http.get(url, headers={"Accept": "application/fhir+json"}).status_code == 200:
                    continue
                resp = self.http.put(url, json=res, headers={"Content-Type": "application/fhir+json"})
                if resp.status_code >= 400:
                    problems.append(f"{res['resourceType']}/{res['id']}: HTTP {resp.status_code}")
            except httpx.HTTPError as e:
                problems.append(f"{res['resourceType']}/{res['id']}: {e}")
        for problem in problems:
            log.warning("directory upload: %s", problem)
        return problems

    def ensure_codesystems(self) -> list[str]:
        """PUT fhirmap's own CodeSystems (audit-step) to HAPI; idempotent. Returns problems."""
        problems = []
        for cs in fhirmap.codesystems():
            try:
                r = self.http.put(f"{self.s.hapi_url}/CodeSystem/{cs['id']}", json=cs,
                                  headers={"Content-Type": "application/fhir+json"})
                if r.status_code >= 400:
                    problems.append(f"CodeSystem/{cs['id']}: HTTP {r.status_code}")
            except httpx.HTTPError as e:
                problems.append(f"CodeSystem/{cs['id']}: {e}")
        for p in problems:
            log.warning("codesystem upload: %s", p)
        return problems

    # ================================================================== audit
    def audit(self, request_id: str | None, step: str, ok: bool, who: str, what: str, *, ip: str | None = None,
              ua: str | None = None, detail: dict | None = None, patient_ref: str | None = None,
              pseudonymous: bool = False, redact: list[str] | None = None, contact: str | None = None) -> dict:
        """Append one entry. `contact` (an e-mail address / phone number the entry refers to by its hash) is kept
        OUTSIDE the chain in audit_private and deleted at link expiry; the chained row and the AuditEvent only
        carry the keyed hash (detail.contact_hash). The SQLite row (chained by SHA-256) AND, for the six steps, the FHIR AuditEvent
        (chained to the previous FHIR event's hash) are built inside ONE `BEGIN IMMEDIATE` transaction, so
        concurrent appends can never fork either chain. Only the HTTP POST to HAPI happens afterwards."""
        with self.store.tx() as c:
            last = c.execute("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            row = {"id": (last["id"] + 1) if last else 1, "request_id": request_id, "step": step,
                   "outcome": "success" if ok else "failure", "who": who, "what": what, "at": iso(now()), "ip": ip,
                   "user_agent": ua, "detail": detail, "prev_hash": last["hash"] if last else auditlib.GENESIS,
                   "alg": auditlib.KEYED}
            row["hash"] = auditlib.entry_hash(row, self.k_audit)  # keyed chain
            c.execute("INSERT INTO audit (id, request_id, step, outcome, who, what, at, ip, user_agent, detail, prev_hash, hash, alg) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (row["id"], request_id, step, row["outcome"], who, what, row["at"], ip, ua,
                       json.dumps(detail, ensure_ascii=False) if detail is not None else None, row["prev_hash"], row["hash"],
                       row["alg"]))
            if contact:
                c.execute("INSERT INTO audit_private (audit_id, request_id, contact) VALUES (?,?,?)",
                          (row["id"], request_id, contact))
            if step in auditlib.SIX_STEPS:
                if patient_ref is None and request_id:
                    rr = c.execute("SELECT patient_id FROM requests WHERE id=?", (request_id,)).fetchone()
                    patient_ref = f"Patient/{rr['patient_id']}" if rr else None
                prev = c.execute("SELECT fhir_hash FROM audit_fhir WHERE fhir_hash IS NOT NULL "
                                 "ORDER BY audit_id DESC LIMIT 1").fetchone()
                ev = self.audit_event_resource(row, patient_ref, prev["fhir_hash"] if prev else None,
                                               pseudonymous=pseudonymous, redact=redact)
                c.execute("INSERT INTO audit_fhir (audit_id, fhir_id, fhir_hash, event, error, at) VALUES (?,NULL,?,?,NULL,?)",
                          (row["id"], self.fm.audit_hash(ev), json.dumps(ev, ensure_ascii=False), row["at"]))
        self.outbox.put(self._push_audit, row, desc=f"AuditEvent #{row['id']}")
        return row

    def audit_event_resource(self, row: dict, patient_ref: str | None, prev_fhir_hash: str | None,
                             pseudonymous: bool = False, redact: list[str] | None = None) -> dict | None:
        """FHIR AuditEvent for one of the six steps (fhirmap.audit_event); None for service-internal steps."""
        step, ok = row["step"], row["outcome"] == "success"
        if step not in auditlib.SIX_STEPS:
            return None
        outcome = "serious" if step == "misdelivery caught" else ("success" if ok else "failure")
        r = self.store.one("SELECT task_id, task_status FROM requests WHERE id=?", (row["request_id"],)) \
            if row["request_id"] else None
        task_ref = f"Task/{r['task_id']}" if r and r["task_id"] and r["task_status"] else None
        refs = list((row["detail"] or {}).get("resources") or []) if step == "answer filed" else []
        # the person's pairwise id (salted bPK hash) identifies the agent, also when it is pseudonymised (a stranger)
        pairwise_id = (row["detail"] or {}).get("bpk_hash")
        who_ref = self.requester_refs(row["who"]).get("practitioner")  # Dr. Hofer's own steps: her Practitioner
        return self.fm.audit_event(step=step, who=row["who"], what=row["what"], when=row["at"], outcome=outcome,
                                   request_id=row["request_id"], task_ref=task_ref, patient_ref=patient_ref,
                                   prev_hash=prev_fhir_hash, references=refs, pseudonymous=pseudonymous, redact=redact,
                                   who_id=pairwise_id, who_ref=who_ref)

    def contact_hash(self, channel: str, address: str) -> str:
        """Keyed (salted) hash of an e-mail address / phone number: the audit can say WHICH contact (and anyone
        with the key can check a given address against it) without storing the address."""
        msg = f"{channel}:{normalise_contact(channel, address)}".encode()
        return hmac.new(self.k_contact, msg, hashlib.sha256).hexdigest()[:32]

    def verify_private_contacts(self, rows: list[dict]) -> list[str]:
        """A cleartext contact still kept beside the chain must hash to the contact_hash in its chained row."""
        detail_of = {r["id"]: r.get("detail") or {} for r in rows}
        problems = []
        for p in self.store.q("SELECT * FROM audit_private"):
            detail = detail_of.get(p["audit_id"], {})
            want = detail.get("contact_hash")
            if not want or self.contact_hash(detail.get("channel") or "", p["contact"]) != want:
                problems.append(f"id {p['audit_id']}: stored contact does not match the chained contact hash")
        return problems

    def _push_audit(self, row: dict, patient_ref: str | None = None) -> None:
        """POST the stored AuditEvent (exactly as built in the append transaction). Retried by sync_audit."""
        f = self.store.one("SELECT * FROM audit_fhir WHERE audit_id=?", (row["id"],))
        if not f or not f["event"] or f["fhir_id"]:
            return
        try:
            ref = self.hapi.create(json.loads(f["event"]))
            self.store.exec("UPDATE audit_fhir SET fhir_id=?, error=NULL WHERE audit_id=?", (ref, row["id"]))
        except Exception as e:  # never break the flow; the SQLite chain is the primary record
            log.warning("AuditEvent push failed: %s", e)
            self.store.exec("UPDATE audit_fhir SET error=? WHERE audit_id=?", (str(e)[:500], row["id"]))
            return False

    def sync_audit(self) -> dict:
        rows = self.store.q("SELECT a.* FROM audit a LEFT JOIN audit_fhir f ON f.audit_id=a.id "
                            "WHERE f.fhir_id IS NULL ORDER BY a.id")
        rows = [a for a in rows if a["step"] in auditlib.SIX_STEPS]
        for a in rows:
            self._push_audit(a, None)
        left = [a for a in self.store.q("SELECT a.step FROM audit a LEFT JOIN audit_fhir f ON f.audit_id=a.id "
                                        "WHERE f.fhir_id IS NULL") if a["step"] in auditlib.SIX_STEPS]
        return {"retried": len(rows), "still_pending": len(left)}

    def verify_audit(self, check_fhir: bool = False) -> dict:
        """SQLite chain always; with check_fhir every six-step entry's AuditEvent on HAPI is fetched and the
        FHIR-side chain is verified with fhirmap.verify_audit_chain (content hash + prev_hash links)."""
        rows = self.store.q("SELECT * FROM audit ORDER BY id")
        res = auditlib.verify_rows(rows, self.k_audit)
        # anchors: always the local table; with check_fhir also read back from HAPI and from the Matrix room
        res["anchor_check"] = self.verify_anchors(rows, external=check_fhir)
        res["anchors"] = res["anchor_check"]["local"]["found"]
        res["problems"] += self.anchor_problems(res["anchor_check"])
        res["problems"] += self.verify_private_contacts(rows)
        res["ok"] = not res["problems"]
        if check_fhir:
            problems, events = [], []
            fmap = {f["audit_id"]: f for f in self.store.q("SELECT * FROM audit_fhir")}
            for a in (x for x in rows if x["step"] in auditlib.SIX_STEPS):
                f = fmap.get(a["id"])
                if not f or not f["fhir_id"]:
                    problems.append(f"id {a['id']}: no AuditEvent on the FHIR server")
                    continue
                resp = self.http.get(f"{self.s.hapi_url}/{f['fhir_id']}", headers={"Accept": "application/fhir+json"})
                if resp.status_code != 200:
                    problems.append(f"id {a['id']}: {f['fhir_id']} HTTP {resp.status_code}")
                    continue
                ev = resp.json()
                if self.fm.audit_hash(ev) != f["fhir_hash"]:
                    problems.append(f"id {a['id']}: {f['fhir_id']} differs from what was written")
                # compare with what was POSTED (for strangers that is the pseudonymised/redacted projection),
                # not with the cleartext service row
                posted = json.loads(f["event"]) if f["event"] else {}
                if ev.get("outcomeDesc") != posted.get("outcomeDesc") or ev.get("recorded") != posted.get("recorded"):
                    problems.append(f"id {a['id']}: {f['fhir_id']} differs from the AuditEvent that was written")
                events.append(ev)
            problems += [f"FHIR chain: {p}" for p in self.fm.verify_chain(events)]
            res["fhir_checked"] = len(events)
            res["problems"] = res["problems"] + problems
            res["ok"] = not res["problems"]
        return res

    def verify_audit_full_cached(self) -> dict:
        """A FULL verification whose result is reused while no other process has modified the database file
        (SQLite PRAGMA data_version changes on any foreign write, e.g. a tamper via the sqlite3 CLI). Since the last
        full pass only rows appended by this process need checking - which the incremental verifier does.
        So a polled request-scoped /api/audit stays O(new rows), and any outside modification forces a re-check."""
        dv = self.store.one("PRAGMA data_version")["data_version"]
        cache = getattr(self, "_full_cache", None)
        if cache is None or cache["data_version"] != dv:
            res = self.verify_audit()
            self._full_cache = {"data_version": dv, "ok": res["ok"], "problems": res["problems"]}
            with self._verify_lock:  # the incremental verifier continues from the fully verified head
                if res["ok"]:
                    self._verified = {"id": res["count"] and self.store.one("SELECT MAX(id) m FROM audit")["m"] or 0,
                                      "hash": res["head"], "count": res["count"], "keyed": res.get("keyed_seen", False)}
            return {**res, "full_checked": "now"}
        inc = self.verify_audit_incremental()
        ok = cache["ok"] and inc["ok"]
        return {**inc, "ok": ok, "problems": cache["problems"] + inc["problems"], "full_checked": "cached"}

    def verify_audit_incremental(self) -> dict:
        """Verify only entries appended since the last successful check (O(new rows) per /api/audit call).
        Full re-verification of every entry: /api/audit/verify and `make audit-verify`."""
        with self._verify_lock:
            v = self._verified
            rows = self.store.q("SELECT * FROM audit WHERE id>? ORDER BY id", (v["id"],))
            problems, prev, expect = [], v["hash"], v["id"] + 1
            for r in rows:
                if r["id"] != expect:
                    problems.append(f"gap: expected id {expect}, found {r['id']}")
                if r["prev_hash"] != prev:
                    problems.append(f"id {r['id']}: prev_hash does not match hash of previous entry")
                if (r.get("alg") or "sha256") != auditlib.KEYED and v.get("keyed"):
                    problems.append(f"id {r['id']}: unkeyed entry after keyed entries (downgrade)")
                if auditlib.entry_hash(r, self.k_audit) != r["hash"]:
                    problems.append(f"id {r['id']}: content was modified (hash mismatch)")
                if problems:
                    break
                prev, expect = r["hash"], r["id"] + 1
                self._verified = {"id": r["id"], "hash": r["hash"], "count": v["count"] + 1,
                                  "keyed": v.get("keyed") or (r.get("alg") == auditlib.KEYED)}
                v = self._verified
            return {"ok": not problems, "count": self._verified["count"], "head": self._verified["hash"],
                    "problems": problems, "mode": "incremental"}

    def effort(self, rid: str) -> dict:
        """Outside-person effort from the audit timestamps (link opened -> answer sent)."""
        rows = self.store.q("SELECT step, outcome, at FROM audit WHERE request_id=? ORDER BY id", (rid,))
        first = lambda step, ok=None: next((r["at"] for r in rows if r["step"] == step and (ok is None or r["outcome"] == ok)), None)
        opened, ident, sent = first("link opened"), first("identity checked", "success"), first("answer sent")
        secs = lambda a, b: int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()) if a and b else None
        return {"link_opened_at": opened, "identity_checked_at": ident, "answer_sent_at": sent,
                "seconds_link_opened_to_answer_sent": secs(opened, sent),
                "seconds_identity_to_answer_sent": secs(ident, sent),
                "note": "first 'link opened' (any visitor, incl. mail scanners) to first 'answer sent'"}

    def audit_view(self, rid: str | None, after_id: int = 0, limit: int = 1000, full: bool = False) -> dict:
        """All entries of a request; without request_id: ascending page after `after_id` (max 10000),
        with `next_after_id` so the whole chain can be fetched and verified over HTTP."""
        cols = ("SELECT a.*, f.fhir_id, f.fhir_hash, f.error AS fhir_error, p.contact FROM audit a "
                "LEFT JOIN audit_fhir f ON f.audit_id=a.id LEFT JOIN audit_private p ON p.audit_id=a.id")
        if rid:
            rows = self.store.q(f"{cols} WHERE a.request_id=? ORDER BY a.id", (rid,))
            nxt = None
        else:
            limit = max(1, min(int(limit), 10000))
            rows = self.store.q(f"{cols} WHERE a.id>? ORDER BY a.id LIMIT ?", (int(after_id), limit + 1))
            nxt = rows[limit - 1]["id"] if len(rows) > limit else None
            rows = rows[:limit]
        for r in rows:  # the cleartext contact while the link lives; '[gelöscht]' after (the hash stays in `detail`)
            if r["contact"] is None and (r["detail"] or {}).get("contact_hash"):
                r["contact"] = "[gelöscht]"
        steps = {r["step"] for r in rows}
        v = self.verify_audit_full_cached() if full else self.verify_audit_incremental()
        v["mode"] = "full" if full else "incremental"
        out = {"request_id": rid, "entries": rows, "next_after_id": nxt,
               "coverage": {s: s in steps for s in auditlib.SIX_STEPS},
               "chain": {"ok": v["ok"], "count": v["count"], "head": v["head"], "problems": v["problems"],
                         "mode": v["mode"],
                         "detail": (f"{v['count']} Einträge, Kette intakt ({'vollständig geprüft' if v['mode'] == 'full' else 'inkrementell geprüft'})"
                                    if v["ok"] else "; ".join(v["problems"][:5]))},
               "effort_seconds": None}
        if rid:
            out["effort"] = self.effort(rid)
            out["effort_seconds"] = out["effort"]["seconds_link_opened_to_answer_sent"]
        return out

    # ================================================================== request creation
    def known_domains(self, org: str | None) -> tuple[list[str], list[str]]:
        key = org_key(org)
        all_d, org_d = set(), set()
        for o, ds in self.registers.org_domains().items():
            all_d.update(ds)
            if key and org_key(o) == key:
                org_d.update(ds)
        for r in self.store.q("SELECT domain, org FROM known_domains"):
            all_d.add(r["domain"])
            if key and org_key(r["org"]) == key:
                org_d.add(r["domain"])
        return sorted(all_d), sorted(org_d)

    def check_address(self, channel: str, address: str, org: str | None, name: str | None = None) -> list[lookalike.Warning_]:
        if channel == "email":
            all_d, org_d = self.known_domains(org)
            ws = lookalike.check_email(address, all_d, org_d, org, recipient_name=name)
            dom = address.split("@")[-1]
            for w in ws:  # a corrected local part becomes a full-address suggestion for the bot's !korrigieren
                if w.code == "local_part_lookalike" and w.similar_to:
                    w.similar_to = f"{w.similar_to}@{dom}"
            return ws
        if not re.fullmatch(r"\+?[0-9][0-9 /()-]{5,20}", address.strip()):
            return [lookalike.Warning_("malformed", f"'{address}' ist keine gültige Telefonnummer.", address)]
        return []

    def _validate_body(self, body: dict) -> tuple[dict, str, str]:
        rec = body.get("recipient") or {}
        channel = (body.get("channel") or "").lower()
        doc_kind = DOC_KIND_ALIASES.get((body.get("doc_kind") or "").lower())
        missing = [k for k, v in {"requester_mxid": body.get("requester_mxid"), "recipient.name": rec.get("name"),
                                  "address": body.get("address"), "patient_id": body.get("patient_id")}.items() if not v]
        if missing:
            raise ApiError(422, f"missing fields: {', '.join(missing)}")
        if not re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", body["patient_id"]):
            raise ApiError(422, "patient_id must match ^[A-Za-z0-9\\-.]{1,64}$")
        if channel not in ("email", "sms"):
            raise ApiError(422, "channel must be 'email' or 'sms'")
        if not doc_kind:
            raise ApiError(422, "doc_kind must be 'lab_result' or 'discharge_report'")
        return rec, channel, doc_kind

    def preflight(self, body: dict) -> dict:
        """Dry run: address look-alike check + register pre-flight. Nothing is stored or sent."""
        rec, channel, doc_kind = self._validate_body(body)
        warns = self.check_address(channel, body["address"], rec.get("org"), rec.get("name"))
        reg = self.registers.preflight(rec["name"], rec.get("role"), rec.get("org"), doc_kind)
        all_w = lookalike.merge_domain_warnings([w.as_dict() for w in warns])
        if not reg["ok"]:
            all_w.append({"code": "register_not_confirmed", "message": reg["message"], "domain": None, "similar_to": None})
        shown = [w["message"] for w in all_w if not w.get("merged_into")]  # one line per typo; every code stays below
        return {"warnings": shown, "warning_details": all_w, "register": reg, "needs_confirmation": bool(all_w)}

    def create_request(self, body: dict) -> dict:
        self.purge()
        rec, channel, doc_kind = self._validate_body(body)
        try:
            patient = self.hapi.get_patient(body["patient_id"])
        except (FhirError, httpx.HTTPError) as e:
            raise ApiError(502, f"FHIR server not reachable: {e}") from e
        if patient is None:
            raise ApiError(404, f"Patient/{body['patient_id']} not found on FHIR server")
        pf = self.preflight(body)
        if any(w["code"] == "malformed" for w in pf["warning_details"]):
            raise ApiError(422, pf["warnings"][0])
        if body.get("dry_run"):
            # Dr. Hofer must SEE whom she asks about (a mistyped patient id is the one misdelivery no gate catches)
            return {"status": "dry_run", "dry_run": True, "patient": self._patient_card(patient), **pf}
        # Send only if there are no warnings, or every warning code was acknowledged by the clinician.
        codes = {w["code"] for w in pf["warning_details"]}
        ack = body.get("acknowledged_warnings")
        # an explicit list of the acknowledged warning codes is required (no blanket confirm)
        confirmed = bool(body.get("confirm_warnings")) and isinstance(ack, list) and codes <= {str(a) for a in ack}
        rid = "req-" + secrets.token_hex(6)
        created = now()
        row = {
            "id": rid, "ref_code": "KB-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6)),
            "token_hash": "unsent-" + secrets.token_hex(16),
            "requester_mxid": body["requester_mxid"], "room_id": body.get("room_id") or self.s.default_room_id,
            "recipient_name": rec["name"].strip(), "recipient_role": rec.get("role"), "recipient_org": rec.get("org"),
            "channel": channel, "address": body["address"].strip(), "patient_id": body["patient_id"],
            "patient_snapshot": None,  # patient name/DOB never stored in request rows (in-memory cache only)
            "doc_kind": doc_kind, "note": body.get("internal_note") or body.get("note"), "status": "created",
            # what is asked, for the OUTSIDE person ("Gerinnung vor geplantem Eingriff"); shown only after the eID check
            "request_text": (body.get("request_text") or body.get("anfrage_text") or "").strip()[:500] or None,
            "warnings": pf["warning_details"], "register": pf["register"], "created_at": iso(created),
            "expires_at": iso(created + timedelta(days=self.s.token_ttl_days)),
        }
        self.store.insert("requests", row)
        link = None
        if pf["needs_confirmation"] and not confirmed:
            self._set_status(rid, "needs_confirmation", "Adress-/Registerwarnung: Bestätigung nötig")
            self.audit(rid, "request awaiting confirmation", True, body["requester_mxid"],
                       f"Anforderung {row['ref_code']} an {rec['name']} NICHT gesendet – Bestätigung nötig: " + " | ".join(pf["warnings"]),
                       detail={"warnings": pf["warning_details"], "register": pf["register"]})
        else:
            link = self._send(rid, confirmed_by=body["requester_mxid"] if pf["needs_confirmation"] else None)
        return self._with_demo_identity(self.request_view(rid, link=link))

    # ---------------------------------------------------------------- demo: a simulated ID Austria for new people
    DEMO_BIRTHDATE = "1980-01-01"

    def _enrol_demo_identity(self, r: dict) -> dict | None:
        """PGK_DEMO only. The simulated ID Austria knows only its personas, and the named recipient of an open
        request is protected (only a PIN gets in). So a person the jury invents could never sign in. When a request is
        SENT to someone with no persona, enrol one for exactly that person (titles stripped, a fixed demo birth date -
        the patient's own when she is the recipient - and a random 6-digit PIN). The PIN goes to Dr. Hofer only (API
        response + bot card); it stands in for that person's own ID Austria app. A forwarded link still fails: the
        colleague has no PIN, and the eID page never lists personas."""
        if not self.s.demo:
            return None
        known = self.idp.demo_persona(r["recipient_name"])
        if known:  # asked again: the same demo identity, shown on every new request (no new audit line)
            return {"name": f"{known['given']} {known['family']}", "pin": known["demo_pin"]}
        if self.idp.has_persona(r["recipient_name"]):
            return None  # a seed or operator persona: its PIN is not ours to show
        given, family = split_person_name(r["recipient_name"])
        if not given or not family:
            return None
        patient = self.patient(r["patient_id"]) or {}
        herself = self.recipient_is_patient(r, patient)
        birthdate = patient.get("birthDate") if herself and patient.get("birthDate") else self.DEMO_BIRTHDATE
        persona = self.idp.add_persona(given, family, birthdate, origin="demo")
        self.audit(r["id"], "demo identity enrolled", True, "Befundlink (Demo)",
                   f"Demo-Identität angelegt (ID Austria – Simulation) für die angefragte Person zu {r['ref_code']} "
                   f"(Persona {persona['key']}); PIN an die Anfordernde übergeben.",
                   detail={"persona": persona["key"], "demo": True})
        return {"name": f"{persona['given']} {persona['family']}", "pin": persona["pin"]}

    def _with_demo_identity(self, view: dict) -> dict:
        demo = self._demo_identities.pop(view["request_id"], None)
        if demo:
            view["demo_identity"] = demo
        return view

    def _put_task(self, r: dict, status: str, reason: str | None = None, output_refs: list | None = None) -> dict:
        patient = self.patient(r["patient_id"])
        owner = None
        if self.recipient_is_patient(r, patient):  # she answers for herself: the Task's owner is the Patient
            owner = {"reference": f"Patient/{r['patient_id']}", "display": fhir_patient_name(patient)}
        return self.fm.task(r, status, reason=reason, output_refs=output_refs, when=iso(now()),
                            requester_name=self.registers.member_name(r["requester_mxid"]),
                            requester_refs=self.requester_refs(r["requester_mxid"]), owner=owner)

    def _set_status(self, rid: str, status: str, reason: str | None = None, **fields) -> None:
        """Update request status and mirror it to the FHIR Task. Each change gets the next task_seq; the queued PUT
        carries it and is dropped if anything newer (another change, or the filing transaction) came after it."""
        self.store.update_request(rid, status=status, **fields)
        r = self.store.get_request(rid)
        tstat = TASK_STATUS.get(status)
        if not tstat or status == "filed":  # the 'completed' Task goes into the filing transaction
            return
        if r["task_status"] == tstat and reason is None:
            return
        task = self._put_task(r, tstat, reason)
        seq = self._bump_task_seq(rid)
        # recorded now (deterministic id); the PUT is queued in the outbox BEFORE any later AuditEvent that
        # references this Task, so HAPI sees the Task first
        self.store.update_request(rid, task_id=task["id"], task_status=tstat)
        self.outbox.put(self._put_task_http, rid, task, seq, desc=f"Task {tstat} #{seq}")

    def _current_task(self, rid: str) -> dict:
        """The Task for the request's current state; while a filing is in flight ('completed' recorded, the
        filing transaction not yet on HAPI) that is 'in-progress' - the transaction itself writes 'completed'."""
        r = self.store.get_request(rid)
        st = r["task_status"] or "requested"
        if st == "completed":
            st = "in-progress"
        return self._put_task(r, st, r["last_reason"] if st == "on-hold" else None)

    def reconcile_tasks(self) -> dict:
        """Safety net (maintenance): the HAPI Task must equal the service state, for EVERY request that is not settled:
        all non-final requests, and final ones until their Task has once been seen on HAPI as expected (then
        task_confirmed=1 and they are not read again). Filed -> completed with output (re-written if not); a missing
        Task is created with the current state."""
        fixed = {"completed": 0, "created": 0, "updated": 0, "checked": 0, "confirmed": 0}
        final = sorted(CLOSED - {"needs_confirmation"})
        marks = ",".join("?" * len(final))
        rows = self.store.q(f"SELECT * FROM requests WHERE task_id IS NOT NULL "
                            f"AND (status NOT IN ({marks}) OR task_confirmed=0) ORDER BY created_at", tuple(final))
        for r in rows:
            fixed["checked"] += 1
            try:
                outcome = self._reconcile_task(r)
            except Exception as e:
                log.warning("Task reconcile %s: %s", r["ref_code"], e)
                continue
            if outcome:
                fixed[outcome] += 1
        if fixed["completed"] or fixed["created"] or fixed["updated"]:
            log.warning("Task reconcile repaired %s", fixed)
        return fixed

    def _reconcile_task(self, r: dict) -> str | None:
        """One request. Returns 'completed' | 'created' | 'updated' (repaired), 'confirmed' (final and as expected)
        or None (nothing to do now)."""
        url = f"{self.s.hapi_url}/Task/{r['task_id']}"
        g = self.http.get(url, headers={"Accept": "application/fhir+json"})
        cur = g.json() if g.status_code == 200 else None
        is_final = r["status"] in CLOSED and r["status"] != "needs_confirmation"
        if r["status"] == "filed":
            if cur and cur.get("status") == "completed" and cur.get("output"):
                self.store.update_request(r["id"], task_confirmed=1)
                return "confirmed"
            main = (r["filed"] or {}).get("main")
            task = self._put_task(r, "completed", output_refs=[{"reference": main}] if main else None)
            headers = {"Content-Type": "application/fhir+json"}
            if g.headers.get("ETag"):
                headers["If-Match"] = g.headers["ETag"]
            if self.http.put(url, json=task, headers=headers).status_code < 300:
                return "completed"
            return None
        if cur is None:
            if g.status_code != 404:
                return None  # HAPI not answering properly: try again next time
            resp = self.http.put(url, json=self._current_task(r["id"]), headers={"Content-Type": "application/fhir+json"})
            return "created" if resp.status_code < 300 else None
        expected = self._current_task(r["id"])
        if cur.get("status") == expected["status"]:
            if is_final:
                self.store.update_request(r["id"], task_confirmed=1)
                return "confirmed"
            return None
        if cur.get("status") in TERMINAL_TASK and expected["status"] not in TERMINAL_TASK:
            return None  # a terminal Task is never moved back (same rule as _put_task_http)
        # a lost status update: write the current state
        if self._put_task_http(r["id"], expected):
            return "updated"
        return None

    def _bump_task_seq(self, rid: str) -> int:
        with self.store.tx() as c:
            c.execute("UPDATE requests SET task_seq=task_seq+1 WHERE id=?", (rid,))
            return c.execute("SELECT task_seq FROM requests WHERE id=?", (rid,)).fetchone()["task_seq"]

    def _put_task_http(self, rid: str, task: dict, seq: int | None = None) -> bool:
        """Monotonic Task writes: (1) a PUT older than the request's latest task_seq is stale -> dropped;
        (2) optimistic locking: read the Task's version, PUT with If-Match; on 412 re-read and re-decide;
        (3) a terminal Task (completed/rejected/cancelled) is never overwritten by a non-terminal status."""
        url = f"{self.s.hapi_url}/Task/{task['id']}"
        try:
            for _ in range(4):
                cur_seq = self.store.one("SELECT task_seq FROM requests WHERE id=?", (rid,))["task_seq"]
                g = self.http.get(url, headers={"Accept": "application/fhir+json"})
                headers = {"Content-Type": "application/fhir+json"}
                if seq is not None and seq < cur_seq:
                    if g.status_code == 200:
                        log.info("Task %s: stale update #%s (latest #%s) dropped", task["id"], seq, cur_seq)
                        return True
                    # the Task does not exist yet: create it NOW with the request's CURRENT state, so the
                    # AuditEvents queued behind this write can reference it (a newer write advances it later)
                    task = self._current_task(rid)
                if g.status_code == 200:
                    cur = g.json()
                    if cur.get("status") in TERMINAL_TASK and task["status"] not in TERMINAL_TASK:
                        log.info("Task %s is %s on HAPI; %s dropped", task["id"], cur.get("status"), task["status"])
                        return True
                    if g.headers.get("ETag"):
                        headers["If-Match"] = g.headers["ETag"]
                elif g.status_code not in (404, 410):
                    raise FhirError(f"GET Task: HTTP {g.status_code}")
                resp = self.http.put(url, json=task, headers=headers)
                if resp.status_code == 412:  # changed between our read and our write: re-read and re-decide
                    continue
                if resp.status_code >= 400:
                    raise FhirError(f"PUT Task: HTTP {resp.status_code} {resp.text[:300]}")
                self.store.update_request(rid, task_error=None)
                return True
            raise FhirError("PUT Task: version kept changing (412)")
        except Exception as e:
            log.warning("Task update failed: %s", e)
            self.store.update_request(rid, task_error=str(e)[:300])
            return False

    def resolve(self, key: str) -> dict:
        r = self.store.get_request(key) or self.store.one("SELECT * FROM requests WHERE ref_code=?", (key.upper(),))
        if not r:
            raise ApiError(404, "unknown request")
        return r

    def confirm(self, key: str, by: str | None, acknowledged: list | None = None) -> dict:
        """Send a request held for confirmation. Like creation: every warning code must be acknowledged
        explicitly in `acknowledged` (no blanket confirm)."""
        r = self.resolve(key)
        if r["status"] != "needs_confirmation":
            raise ApiError(409, f"request is '{r['status']}', not awaiting confirmation")
        codes = {w["code"] for w in r["warnings"]}
        acked = {str(a) for a in acknowledged} if isinstance(acknowledged, list) else set()
        missing = codes - acked
        if missing:
            raise ApiError(409, "acknowledged_warnings must list every warning code; missing: " + ", ".join(sorted(missing)))
        link = self._send(r["id"], confirmed_by=by or r["requester_mxid"])
        return self._with_demo_identity(self.request_view(r["id"], link=link))

    def cancel(self, key: str, by: str | None) -> dict:
        r = self.resolve(key)
        if r["status"] == "filed":
            raise ApiError(409, "already filed")
        self._set_status(r["id"], "cancelled", "Von der Anfordernden zurückgezogen",
                         token_hash="cancelled-" + secrets.token_hex(16))
        self._purge_answers(r["id"])
        self.audit(r["id"], "request cancelled", True, by or r["requester_mxid"], f"Anforderung {r['ref_code']} zurückgezogen; Link ungültig.")
        return self.request_view(r["id"])

    def _send(self, rid: str, confirmed_by: str | None) -> str:
        r = self.store.get_request(rid)
        token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))  # never derived from data
        link = f"{self.s.base_url}/r/{token}"
        pat = self.patient(r["patient_id"]) or {}
        snap = {"name": fhir_patient_name(pat) if pat else None, "birthDate": pat.get("birthDate")}
        member, clinic = self.registers.member(r["requester_mxid"]), self.registers.clinic()
        req_name, dept, phone = member.get("name"), member.get("department"), clinic.get("callback_phone")
        host = re.sub(r"^https?://", "", self.s.base_url).split("/")[0]

        # Guard, on top of the fixed template: patient name/DOB, the patient id, the curated doc-type lexicon
        # (messages.DOC_TYPE_WORDS, always) and the content words of the recipient's role/organisation (an org name
        # can reveal the doc type). Our own clinic, requester and department legitimately appear in the message.
        own = set(re.findall(r"\w+", f"{self.s.clinic_name} {req_name or ''} {dept or ''}"))
        extra = [DOC_KIND_LABEL[r["doc_kind"]], r["patient_id"],
                 *messages.context_words([r["recipient_role"], r["recipient_org"]], allowed=own)]
        # the recipient may be the patient herself: then the recipient's name IS patient PII
        if snap.get("name") and fhirmap.compare_names(r["recipient_name"], snap["name"]) == "same":
            extra += [t for t in re.split(r"[\s,]+", r["recipient_name"]) if len(t) >= 2]

        def build() -> tuple[str, str]:
            if r["channel"] == "email":
                return messages.build_email(self.s.clinic_name, link, r["expires_at"], r["ref_code"], req_name, dept, phone, host)
            short = re.sub(r"^(Dr\.?\s+)?\S+\s+", r"\1", req_name) if req_name else None   # 'Dr. Lena Hofer' -> 'Dr. Hofer'
            return "", messages.build_sms(self.s.clinic_name, link, r["expires_at"], r["ref_code"], short,
                                          (dept or "").replace("Medizin", "Med.") or None, phone)

        def scan(subject: str, text: str) -> list[str]:
            # the link = operator-configured base URL + random token: masked, everything else is scanned
            # link and host are operator configuration (base URL + random token): masked, everything else is scanned
            return messages.pii_findings((subject + "\n" + text).replace(link, "<LINK>").replace(host, "<HOST>"), snap.get("name"),
                                         snap.get("birthDate"), extra)

        subject, text = build()
        html = (messages.build_email_html(self.s.clinic_name, link, r["expires_at"], r["ref_code"], req_name, dept, phone, host)
                if r["channel"] == "email" else None)
        findings = scan(subject, text + ("\n" + messages.html_scan_text(html) if html else ""))  # the HTML part too
        if findings:  # never send a message that leaks anything
            self._set_status(rid, "send_failed", "PII-Schutz: Nachricht nicht gesendet", last_reason="PII guard: " + "; ".join(findings))
            raise ApiError(422, "Nachricht nicht gesendet – PII-Schutz: " + "; ".join(findings))
        try:
            if r["channel"] == "email":
                self.mailer.send(r["address"], subject, text, html=html)
            else:
                self.store.insert("sms", {"request_id": rid, "to_number": r["address"], "text": text, "at": iso(now())})
        except Exception as e:
            self._set_status(rid, "send_failed", "Versand fehlgeschlagen", last_reason=str(e)[:300])
            raise ApiError(502, f"sending failed: {e}") from e
        self.store.update_request(rid, token_hash=sha256(token), sent_at=iso(now()))
        self._set_status(rid, "sent")
        demo = self._enrol_demo_identity(r)
        if demo:
            self._demo_identities[rid] = demo  # returned ONCE in this create/confirm response
            self.notify(r["room_id"], f"Anforderung {r['ref_code']} an {r['recipient_name']} gesendet. Demo: Für diese "
                                      f"Person wurde eine simulierte ID-Austria-Identität angelegt; bitte Name und PIN an sie weitergeben.",
                        kind="request_sent", request_id=rid,
                        data={"ref_code": r["ref_code"], "recipient": r["recipient_name"], "demo_identity": demo})
        warn_txt = ""
        if confirmed_by and r["warnings"]:
            warn_txt = f" TROTZ Warnung gesendet (bestätigt von {confirmed_by}): " + " | ".join(w["message"] for w in r["warnings"])
        # the contact itself is not in the chained text or in the FHIR AuditEvent: its keyed hash + the channel type
        chash = self.contact_hash(r["channel"], r["address"])
        self.audit(rid, "request sent", True, r["requester_mxid"],
                   f"Anforderung {r['ref_code']} ({DOC_KIND_LABEL[r['doc_kind']]}) an {r['recipient_name']} per "
                   f"{'E-Mail' if r['channel'] == 'email' else 'SMS'} an {contact_label(chash)} gesendet; Link gültig bis "
                   f"{r['expires_at']}; Nachricht PII-geprüft (sauber). Register: {r['register']['message']}{warn_txt}",
                   detail={"channel": r["channel"], "contact_hash": chash, "ref_code": r["ref_code"],
                           "message_sha256": sha256(subject + "\n" + text), "pii_scan": "clean",
                           "warnings": r["warnings"], "register_ok": r["register"]["ok"]},
                   contact=r["address"])
        return link

    def request_view(self, rid: str, link: str | None = None) -> dict:
        r = self.store.get_request(rid)
        if not r:
            raise ApiError(404, "unknown request")
        held = self.store.one("SELECT id, outcome, reasons, encrypted, purge_after FROM answers WHERE request_id=? "
                              "ORDER BY id DESC LIMIT 1", (rid,))
        out = {
            "request_id": r["id"], "ref_code": r["ref_code"], "status": r["status"],
            "task": f"Task/{r['task_id']}" if r["task_id"] else None,
            "task_status": r["task_status"], "task_error": r["task_error"],
            "warnings": [w["message"] for w in r["warnings"]], "warning_details": r["warnings"], "register": r["register"],
            "requester_mxid": r["requester_mxid"], "room_id": r["room_id"],
            "recipient": {"name": r["recipient_name"], "role": r["recipient_role"], "org": r["recipient_org"]},
            "channel": r["channel"], "address": r["address"], "patient_id": r["patient_id"], "doc_kind": r["doc_kind"],
            "created_at": r["created_at"], "sent_at": r["sent_at"], "expires_at": r["expires_at"],
            "identity_bound": bool(r["bound_bpk"]), "identity_bound_to": r["bound_name"],
            "identity_review_pending": bool(r["identity_pending_sub"]),
            "eid_attempts": r["eid_attempts"], "eid_failures": r["eid_failures"],
            "misdeliveries_caught": r["misdeliveries"], "last_reason": r["last_reason"], "held_kind": r["held_kind"],
            "latest_answer": held, "filed": r["filed"],
            "decision_options": ["halten", "verwerfen", "ablegen"] if r["status"] == "held_review"
            else (["halten", "verwerfen", "ablegen"] if r["status"] == "held_technical" else []),
        }
        if link and self.s.debug_links:
            out["link"] = link
        return out

    # ================================================================== outside person
    def by_token(self, token: str) -> dict | None:
        if not TOKEN_RE.fullmatch(token or ""):
            return None
        return self.store.request_by_token_hash(sha256(token))

    def is_expired(self, r: dict) -> bool:
        return now() > datetime.fromisoformat(r["expires_at"])

    def link_opened(self, r: dict, ip: str | None, ua: str | None) -> None:
        """GET never consumes or binds the link (mail scanners pre-click links)."""
        self.audit(r["id"], "link opened", True, "unbekannt (noch nicht identifiziert)",
                   f"Link zu Anforderung {r['ref_code']} geöffnet; nichts angezeigt außer der anfragenden Klinik.",
                   ip=ip, ua=ua, detail={"status_at_open": r["status"]})
        if r["status"] == "sent":
            self.store.update_request(r["id"], status="opened")

    def eid_check(self, r: dict, claims: dict, ip: str | None, ua: str | None, birth_date: str | None = None) -> str:
        """Returns 'match' | 'mismatch' | 'forward' | 'review'. Binary name rule (fhirmap.identity_matches, RULES.md
        rule 1) plus namesake protection: a NEW ID Austria identity for a recipient+contact that has answered before
        (as someone else) is held BEFORE anything is shown, and Dr. Hofer confirms it ('review').
        Binds ONLY to an eID matching the NAMED person; any other eID is blocked, the link stays valid."""
        given, family = claims["given_name"], claims["family_name"]
        bh = claims["sub"]  # salted pairwise bPK hash from the (simulated) IdP; the clear bPK never reaches the service
        who = f"{given} {family} (ID Austria – Simulation, {bh})"
        name_ok = fhirmap.identity_matches(r["recipient_name"], given, family)
        rel = "match" if name_ok and fhirmap.identity_matches(r["recipient_name"], given, family, patient=self.patient_of(r),
                                                              birth_date=birth_date) else "mismatch"
        if rel == "match" and r["bound_bpk"] and r["bound_bpk"] != bh:
            rel = "forward"  # same name, different person than the one already bound
        if rel == "match" and bh == r["identity_rejected_sub"]:
            rel = "rejected"  # Dr. Hofer already refused this identity for this request
        if rel == "match" and not r["bound_bpk"] and bh != r["identity_approved_sub"]:
            known = self.known_identities(r)
            if known and bh not in known:
                # only THIS new identity waits for Dr. Hofer; the known person can still answer meanwhile
                return self._identity_review(r, claims, who, ip, ua, birth_date)
        why = {"match": f"Name passt zur angefragten Person '{r['recipient_name']}'",
               "mismatch": ("Geburtsdatum passt nicht zur angefragten Person" if name_ok else
                            f"Name '{given} {family}' passt nicht zur angefragten Person '{r['recipient_name']}'"),
               "forward": f"Link ist bereits an eine andere eID ({r['bound_name']}) gebunden",
               "rejected": "diese ID-Austria-Identität wurde von der Anfordernden abgelehnt"}[rel]
        ok = rel == "match"
        self.audit(r["id"], "identity checked", rel == "match", who,
                   f"eID-Prüfung (ID Austria – Simulation, kein echtes ID Austria) für {r['ref_code']}: "
                   f"{'BESTANDEN' if rel == 'match' else 'NICHT BESTANDEN'} – {why}.",
                   ip=ip, ua=ua, detail={"relation": rel, "bpk_hash": bh, "acr": claims["acr"], "simulated": True, "iss": claims["iss"]},
                   # a stranger is pseudonymous in the patient's FHIR AuditEvents (cleartext only in the service audit)
                   pseudonymous=not ok, redact=None if ok else [f"{given} {family}", given, family, bh])
        if ok:
            if rel == "match" and not r["bound_bpk"]:
                # atomic first bind: two simultaneous matching logins can never both bind
                with self.store.tx() as c:
                    n = c.execute("UPDATE requests SET bound_bpk=?, bound_name=?, bound_birth_year=? "
                                  "WHERE id=? AND bound_bpk IS NULL",
                                  (bh, f"{given} {family}", (birth_date or "")[:4] or None, r["id"])).rowcount
                if not n and self.store.get_request(r["id"])["bound_bpk"] != bh:
                    return self.eid_check(self.store.get_request(r["id"]), claims, ip, ua, birth_date)  # lost the race
            if r["status"] in ("sent", "opened", "blocked_identity"):
                self._set_status(r["id"], "identity_verified")
            return rel
        fails = r["eid_failures"] + 1
        # sent despite an address warning, and someone else signs in: the link went to the wrong address (M1)
        wrong_address = any(w.get("code") in ADDRESS_WARNING_CODES for w in r["warnings"] or [])
        locked = wrong_address or fails >= self.s.max_failed_eid_per_token
        self.store.update_request(r["id"], eid_failures=fails, misdeliveries=r["misdeliveries"] + 1,
                                  last_reason=f"{rel}: {given} {family}")
        if wrong_address:
            self._set_status(r["id"], "locked", "Gesperrt: der Link ging an eine verdächtige Adresse")
        elif locked:
            self._set_status(r["id"], "locked", "Gesperrt nach wiederholten fehlgeschlagenen eID-Prüfungen")
        elif r["status"] in ("sent", "opened"):
            self.store.update_request(r["id"], status="blocked_identity")
        self.audit(r["id"], "misdelivery caught", True, who,
                   f"Zugriff auf {r['ref_code']} blockiert ({rel}): {why}. Keine Patientendaten angezeigt, keine Antwort möglich. "
                   + ("Link nach wiederholten Fehlversuchen gesperrt." if locked else "Link bleibt für die angefragte Person gültig."),
                   ip=ip, ua=ua, detail={"relation": rel, "attempted_by": f"{given} {family}", "bpk_hash": bh,
                                         "channel": r["channel"], "contact_hash": self.contact_hash(r["channel"], r["address"])},
                   pseudonymous=True, redact=[f"{given} {family}", given, family, bh, r["address"]], contact=r["address"])
        if wrong_address:
            next_step = self.new_request_command(r)
            tail = f" Dieser Link ist gesperrt; eine neue Anfrage an die richtige Adresse: {next_step}"
        elif locked:
            next_step = None
            tail = " Der Link ist jetzt gesperrt."
        else:
            next_step = None
            tail = f" Der Link bleibt für {r['recipient_name']} gültig."
        data = {"ref_code": r["ref_code"], "doc_kind": r["doc_kind"],
                "patient": self._patient_card(self.patient(r["patient_id"]))}   # the REQUESTED patient, for the headline
        if locked:
            data["locked"] = True
        if wrong_address:
            data.update(wrong_address=True, next_step=next_step)  # not '!freigeben': a new request is the way
        self.notify(r["room_id"], kind="misdelivery", request_id=r["id"], data=data, text=
                        f"[FEHLZUSTELLUNG ABGEFANGEN] Anforderung an {r['recipient_name']} ({r['ref_code']}): Zugriff von einer "
                        f"anderen Person ({given} {family}) blockiert, angemeldet mit ID Austria (Simulation) über den Link an "
                        f"{mask_contact(r['channel'], r['address'])}. Keine Patientendaten angezeigt." + tail)
        return rel

    def new_request_command(self, r: dict) -> str:
        """The bot command for the same request to the corrected address (the suggestion of the address warning)."""
        _given, family = split_person_name(r["recipient_name"])
        local = r["address"].strip().rpartition("@")[0]
        corrected = None
        for w in r["warnings"] or []:
            if not w.get("similar_to"):
                continue
            corrected = w["similar_to"] if "@" in w["similar_to"] else f"{local}@{w['similar_to']}"
            break
        command = "!labor" if r["doc_kind"] == "lab_result" else "!reha"
        return f"{command} {family} {r['patient_id']} {corrected or '<richtige Adresse>'}"

    # ---------------------------------------------------------------- namesake protection (identity memory)
    @staticmethod
    def recipient_key(r: dict) -> str:
        """Recipient name (normalised) + organisation (normalised). Deliberately NOT the contact: a namesake reached
        through a new address is still a new identity for a known recipient."""
        return " ".join(rule_tokens(r["recipient_name"])) + "|" + org_key(r.get("recipient_org"))

    def known_identities(self, r: dict) -> set[str]:
        return {x["sub"] for x in self.store.q("SELECT sub FROM recipient_identities WHERE rkey=?", (self.recipient_key(r),))}

    def remember_identity(self, r: dict, sub: str) -> bool:
        """Record the subject of a FILED answer (and its birth year, from the bind). Returns True if this was the
        first answer of this person for this recipient."""
        first = sub not in self.known_identities(r)
        year = r.get("bound_birth_year") if r.get("bound_bpk") == sub else None
        self.store.exec("INSERT OR IGNORE INTO recipient_identities (rkey, sub, first_seen, birth_year) VALUES (?,?,?,?)",
                        (self.recipient_key(r), sub, iso(now()), year))
        if year:
            self.store.exec("UPDATE recipient_identities SET birth_year=? WHERE rkey=? AND sub=? AND birth_year IS NULL",
                            (year, self.recipient_key(r), sub))
        return first

    def backfill_identity_memory(self) -> int:
        """Re-key the identity memory (name + org) from the filed answers; idempotent (startup)."""
        rows = self.store.q("SELECT a.sender_bpk, a.received_at, q.* FROM answers a JOIN requests q ON q.id=a.request_id "
                            "WHERE a.outcome='filed' AND a.sender_bpk IS NOT NULL")
        added = 0
        for x in rows:
            year = x["bound_birth_year"] if x["bound_bpk"] == x["sender_bpk"] else None
            added += self.store.exec_count("INSERT OR IGNORE INTO recipient_identities (rkey, sub, first_seen, birth_year) "
                                           "VALUES (?,?,?,?)", (self.recipient_key(x), x["sender_bpk"], x["received_at"], year))
        return added

    def _identity_review(self, r: dict, claims: dict, who: str, ip, ua, birth_date: str | None) -> str:
        """Hold BEFORE reveal, for the NEW identity only: nothing is shown to it, nothing is bound, and the request
        stays open for the known person. Dr. Hofer confirms or rejects the new identity on a card that says what
        differs (birth year, contact, the known person's history) - never a birth date or a bPK."""
        year = (birth_date or "")[:4] or None
        repeat = r["identity_pending_sub"] == claims["sub"]
        self.store.update_request(r["id"], identity_pending_sub=claims["sub"], identity_pending_year=year,
                                  identity_pending_name=f"{claims['given_name']} {claims['family_name']}")
        card = self.identity_card(r, year)
        self.audit(r["id"], "identity checked", False, who,
                   f"eID-Prüfung für {r['ref_code']}: Name passt, aber eine ANDERE ID-Austria-Identität als bei früheren "
                   f"Antworten dieser Person ({r['recipient_name']}) – nichts angezeigt, Bestätigung durch die Anfordernde "
                   f"nötig. {card['detail']}",
                   ip=ip, ua=ua, detail={"relation": "review", "bpk_hash": claims["sub"], "birth_year": year,
                                         "birth_year_differs": card["birth_year_differs"]})
        if not repeat:  # one card per new identity, not one per login attempt
            self.notify(r["room_id"], f"Neue ID-Austria-Identität für {r['recipient_name']} – freigeben? {card['detail']} "
                                      f"Der neuen Person wurde nichts angezeigt; es wurde nichts abgelegt. Ref {r['ref_code']}",
                        kind="identity_confirm", request_id=r["id"], ref_code=r["ref_code"],
                        data={"ref_code": r["ref_code"], "recipient": r["recipient_name"], **card})
        return "review"

    def identity_card(self, r: dict, new_year: str | None) -> dict:
        """What Dr. Hofer decides on: the new identity's birth YEAR and contact; per known identity of this recipient
        the number of filed answers, the first answer's date and its birth year."""
        known = []
        for k in self.store.q("SELECT sub, first_seen, birth_year FROM recipient_identities WHERE rkey=? ORDER BY first_seen",
                              (self.recipient_key(r),)):
            filed = self.filed_answers_of(r, k["sub"])
            known.append({"answers": len(filed), "first_answer": (filed[0] if filed else k["first_seen"])[:10],
                          "birth_year": k["birth_year"]})
        known_years = {k["birth_year"] for k in known if k["birth_year"]}
        differs = bool(new_year and known_years and new_year not in known_years)
        contact = mask_contact(r["channel"], r["address"])
        parts = [f"Neue Person: Jahrgang {new_year or 'unbekannt'}, über {contact}."]
        for k in known:
            parts.append(f"Bisher bekannt: {k['answers']} Antwort(en) seit {_de_date(k['first_answer'])}"
                         + (f", Jahrgang {k['birth_year']}" if k["birth_year"] else "") + ".")
        if differs:
            parts.append("Anderes Geburtsjahr als bisher.")
        since = min((k["first_answer"] for k in known), default=None)
        return {"new_birth_year": new_year, "via_contact": contact, "known_answers": sum(k["answers"] for k in known),
                "known_since": since, "birth_year_differs": differs, "known_identities": known,
                "detail": " ".join(parts)}

    def filed_answers_of(self, r: dict, sub: str) -> list[str]:
        """received_at of every FILED answer of `sub` to a request for the same recipient (name + org), oldest first."""
        key = self.recipient_key(r)
        rows = self.store.q("SELECT a.received_at, q.recipient_name, q.recipient_org, q.channel, q.address FROM answers a "
                            "JOIN requests q ON q.id=a.request_id WHERE a.sender_bpk=? AND a.outcome='filed' "
                            "ORDER BY a.received_at", (sub,))
        return [x["received_at"] for x in rows if self.recipient_key(x) == key]

    def new_session(self, r: dict, claims: dict, assertion: str, verdict: str, birth_date: str | None = None) -> str:
        """The session keeps the IdP's ES256 token verbatim: it becomes Provenance.signature (verifiable via JWKS)."""
        sid = secrets.token_urlsafe(32)
        self.store.insert("sessions", {"sid_hash": sha256(sid), "request_id": r["id"], "bpk": claims["sub"],
                                       "given": claims["given_name"], "family": claims["family_name"], "birth_date": birth_date,
                                       "loa": claims["acr"], "verdict": verdict, "statement": assertion,
                                       "expires_at": iso(now() + timedelta(seconds=self.s.session_ttl_s))})
        return sid

    def sender_statement(self, r: dict, identity: dict, binding: dict) -> str:
        """The signed statement stored in Provenance.signature: WHO sent it (from the eID token), bound to WHAT was
        filed (fhirmap.provenance_binding: content SHA-256, exact targets incl. the Task, resources_sha256 over the
        filed clinical content and the per-resource digests), signed by the service (ES256, JWKS at
        /.well-known/jwks.json). The IdP's own token is embedded, so its signature is checkable too; neither
        carries a clear bPK or a birth date."""
        return sign_jws(self.service_key, {
            "iss": self.s.base_url, "typ": "pgk-bridge/sender-statement", "iat": int(time.time()),
            "sub": identity.get("bpk_hash"), "given_name": identity.get("given_name"), "family_name": identity.get("family_name"),
            "acr": fhirmap.POLICY_EID_SIMULATED, "simulated": True, "request": r["id"],
            "id_token": identity.get("assertion"), **binding})

    def _verify_filed_signature(self, refs: list[str], id_map: dict, versions: dict | None = None) -> dict:
        """Read the filed Provenance AND the filed clinical resources back from HAPI and verify the signed, bound
        statement (ES256 via our JWKS): it must cover this answer's SHA-256, exactly these targets and, where fhirmap
        supports it, the clinical content (resources_sha256). With `versions` ({'Type/id': versionId}, recorded at
        filing) every resource is read AT THE VERSION THAT WAS SIGNED (`_history/{vid}`), so a later legitimate
        version (a correction) is not mistaken for an edit. `changed` names any resource that differs."""
        versions = versions or {}
        prov_ref = next((x for x in refs if x.startswith("Provenance/")), None)
        if not prov_ref:
            return {"verified": False, "problems": ["no Provenance filed"]}
        try:
            provenance = self.hapi.read_version(prov_ref, versions.get(prov_ref))
            kwargs = {"id_map": _versioned_id_map(id_map, provenance)}
            if _SIGNATURE_COVERS_CONTENT:
                clinical = [x for x in refs if x.split("/")[0] in CLINICAL_TYPES]
                kwargs["resources"] = [self.hapi.read_version(x, versions.get(x)) for x in clinical]
            v = fhirmap.verify_provenance_signature(provenance, self.jwks(), **kwargs)
        except Exception as e:
            return {"verified": False, "problems": [f"{type(e).__name__}: {str(e)[:200]}"]}
        if not v["valid"]:
            log.error("filed Provenance %s: signature does not verify: %s", prov_ref, v["problems"])
        return {"verified": bool(v["valid"]), "bound": v.get("bound"), "kid": v.get("kid"),
                "content_checked": "resources" in kwargs, "changed": v.get("changed") or [],
                "problems": v["problems"][:5]}

    def reverify_filing(self, rid: str, cache: dict | None = None) -> dict:
        """Re-verify one filing. {request_id, ref_code, verified, bound, changed, superseded, problems}.
        - the signed versions must still verify (they cannot change on HAPI: history is immutable);
        - a NEWER version of a signed resource is information ('superseded') when one of our later filings signed
          exactly that version and still verifies (a correction); a newer version nobody signed is an
          unauthorised edit: `changed`, verified False.
        Filings from before versions were recorded are checked against the current versions."""
        cache = {} if cache is None else cache
        r = self.store.get_request(rid)
        filed = r["filed"] or {}
        out = {"request_id": rid, "ref_code": r["ref_code"], "superseded": []}
        if filed.get("id_map") is None:
            return {**out, "verified": None, "changed": [], "problems": ["filed before id_map was stored: not re-verifiable"]}
        versions = self._signed_versions(rid)
        res = {**out, **self._signature_at_filing(rid, cache)}
        if not versions:
            return res
        for ref, signed_vid in versions.items():
            if ref.split("/")[0] not in CLINICAL_TYPES:
                continue
            current = (self.hapi.read(ref).get("meta") or {}).get("versionId")
            if not current or current == signed_vid:
                continue
            later = self._filing_that_signed(ref, current, cache)
            if later:
                res["superseded"].append(f"{ref}: durch eine spätere Version ersetzt (Korrektur {later})")
            else:
                res["changed"] = list(res["changed"]) + [ref]
                res["problems"] = list(res["problems"]) + [f"{ref}: neuere Version {current} ohne gültige Signatur"]
                res["verified"] = False
        return res

    def _signature_at_filing(self, rid: str, cache: dict) -> dict:
        """_verify_filed_signature for one filing at its recorded versions, cached for one re-verification run."""
        if rid not in cache:
            filed = self.store.get_request(rid)["filed"] or {}
            cache[rid] = self._verify_filed_signature(filed["resources"], filed["id_map"], self._signed_versions(rid))
        return cache[rid]

    def _signed_versions(self, rid: str) -> dict:
        """{'Type/id': versionId} as signed. Recorded at filing. For filings from before that, inferred: an ordinary
        filing CREATED its resources, so it signed version 1; a correction PUT later versions, so it signed the
        latest version written by the end of the second of its 'answer filed' audit entry (from the history)."""
        filed = self.store.get_request(rid)["filed"] or {}
        if filed.get("versions"):
            return filed["versions"]
        signed = [x for x in filed.get("resources") or [] if x.split("/")[0] in (*CLINICAL_TYPES, "Provenance")]
        if not filed.get("correction"):
            return {ref: "1" for ref in signed}
        row = self.store.one("SELECT at FROM audit WHERE request_id=? AND step='answer filed' ORDER BY id DESC LIMIT 1",
                             (rid,))
        if not row:
            return {}
        filed_at = datetime.fromisoformat(row["at"]) + timedelta(seconds=1)  # audit times have whole seconds
        inferred = {}
        for ref in signed:
            try:
                vid = self._version_at(ref, filed_at)
            except (httpx.HTTPError, FhirError, ValueError):
                vid = None
            if vid:
                inferred[ref] = vid
        return inferred

    def _version_at(self, ref: str, when: datetime) -> str | None:
        """The versionId of `ref` that was current at `when` (from GET {ref}/_history)."""
        resp = self.http.get(f"{self.s.hapi_url}/{ref}/_history", params={"_count": 100},
                             headers={"Accept": "application/fhir+json"})
        if resp.status_code != 200:
            return None
        best, best_at = None, None
        for e in resp.json().get("entry") or []:
            meta = (e.get("resource") or {}).get("meta") or {}
            if not meta.get("versionId") or not meta.get("lastUpdated"):
                continue
            at = datetime.fromisoformat(meta["lastUpdated"].replace("Z", "+00:00"))
            if at <= when and (best_at is None or at > best_at):
                best, best_at = meta["versionId"], at
        return best

    def _filing_that_signed(self, ref: str, version: str, cache: dict) -> str | None:
        """The KB- reference of our filing whose verified signature covers `ref` at `version` (or None)."""
        for row in self.store.q("SELECT id, ref_code, filed FROM requests WHERE status='filed'"):
            if ref not in ((row["filed"] or {}).get("resources") or []):
                continue
            if self._signed_versions(row["id"]).get(ref) != version:
                continue
            if self._signature_at_filing(row["id"], cache).get("verified"):
                return row["ref_code"]
        return None

    def reverify_all(self, alert: bool = True) -> dict:
        """Every filed request (maintenance, `python -m service.verify_provenance`). A filing that no longer verifies
        is audited and reported to the requester's room once; a correction's newer version is only information."""
        results, cache = [], {}
        for row in self.store.q("SELECT id FROM requests WHERE status='filed' ORDER BY created_at"):
            res = self.reverify_filing(row["id"], cache)
            results.append(res)
            if alert and res["verified"] is False:
                self._alert_signature_failure(res)
        failed = [x for x in results if x["verified"] is False]
        legacy = [x for x in results if x["verified"] is None]
        return {"checked": len(results), "verified": len(results) - len(failed) - len(legacy), "failed": failed,
                "not_reverifiable": len(legacy),
                "superseded": [f"{x['ref_code']}: {s}" for x in results for s in x.get("superseded") or []]}

    def _alert_signature_failure(self, res: dict) -> None:
        """Once per filing. The room gets what a clinician can act on: patient, what changed, signed value vs the value
        on the FHIR server now. The audit names only the resources (no patient data, no clinical content)."""
        r = self.store.get_request(res["request_id"])
        if r["sig_alerted"]:
            return
        self.store.update_request(r["id"], sig_alerted=iso(now()))
        changed = res.get("changed") or []
        refs = ", ".join(changed) or "; ".join(res.get("problems") or [])[:300]
        self.audit(r["id"], "provenance check failed", False, "Befundlink",
                   f"Signatur der Ablage zu {r['ref_code']} passt nicht mehr zum Inhalt am FHIR-Server: {refs}.",
                   detail={"changed": changed, "problems": res.get("problems")})
        what = "; ".join(self._change_descriptions(r["id"], changed)) or f"signierter Inhalt verändert ({refs})"
        self.notify(r["room_id"], f"[ACHTUNG] {self._patient_label(r)} – {what} ({r['ref_code']}). "
                                  f"Nicht verwenden, bis geprüft.",
                    kind="tamper", request_id=r["id"], data={"ref_code": r["ref_code"], "changed": changed})

    def _requester_card(self, r: dict) -> dict:
        """{name, org} of the requesting doctor (PGK directory): the bot drops an 'Einsender' that is her."""
        m = self.registers.member(r["requester_mxid"]) or {}
        return {"name": m.get("name"), "org": m.get("org")}

    def _patient_label(self, r: dict) -> str:
        """'Maria Beispiel (12.03.1961)' - for Dr. Hofer's own room only."""
        patient = self.patient(r["patient_id"]) or {}
        if not patient:
            return f"Patient {r['patient_id']}"
        dob = patient.get("birthDate")
        return fhir_patient_name(patient) + (f" ({_de_date(dob)})" if dob else "")

    def _change_descriptions(self, rid: str, changed: list[str]) -> list[str]:
        """Per changed resource: the signed version vs the current one, in words."""
        versions = self._signed_versions(rid)
        out = []
        for ref in changed:
            try:
                signed = self.hapi.read_version(ref, versions.get(ref))
                current = self.hapi.read(ref)
            except (httpx.HTTPError, FhirError):
                out.append(f"{ref} verändert")
                continue
            out.append(describe_change(signed, current) or f"{ref} verändert")
        return out

    def jwks(self) -> dict:
        return {"keys": [self.service_key.jwk(), *self.idp.jwks()["keys"]]}

    def _facts(self, chk: dict, resources: list | None = None) -> dict:
        try:
            return fhirmap.card_facts(chk.get("parsed"), chk.get("decision"), resources) if chk.get("parsed") else {}
        except Exception as e:
            log.warning("card_facts failed: %s", e)
            return {}

    @staticmethod
    def _patient_card(p: dict | None) -> dict | None:
        """{name, birthDate} of the REQUESTED patient (in-network message to Dr. Hofer only)."""
        return {"name": fhir_patient_name(p), "birthDate": p.get("birthDate")} if p else None

    @staticmethod
    def _matched(codes: list[str], mismatches: list[dict]) -> list[str]:
        """What DID match on a hold (3-second decision: 'DOB exact, name only reordered')."""
        if any(c in ("PATIENT_MISSING", "PATIENT_CONFLICT", "PATIENT_INCOMPLETE", "EXTRACTION_DISAGREE",
                     "EXTRACTION_UNREADABLE") for c in codes):   # photo readings not agreed: no patient check to show
            return ["✓ Absender per ID Austria (Simulation) geprüft"]
        off = {m.get("field") for m in mismatches}
        out = []
        if "birthDate" not in off and not any(c.startswith("DOB_") for c in codes):
            out.append("✓ Geburtsdatum")
        if "name" not in off and "NAME_NEAR" not in codes:
            out.append("✓ Name")
        return out + ["✓ Absender per ID Austria (Simulation) geprüft"]

    def patient_of(self, r: dict) -> dict | None:
        return self.patient(r["patient_id"])

    def patient(self, pid: str) -> dict | None:
        """The requested Patient from HAPI, cached in memory only (retention.PATIENT_CACHE_S), never persisted."""
        hit = self._pcache.get(pid)
        if hit and time.monotonic() - hit[0] < retention.PATIENT_CACHE_S:
            return hit[1]
        try:
            p = _retry(lambda: self.hapi.get_patient(pid), safe_on=(httpx.HTTPError, FhirError), waits=(0.3, 1))
        except Exception as e:
            log.warning("patient fetch failed: %s", e)
            return hit[1] if hit else None
        p = {k: p.get(k) for k in ("resourceType", "id", "name", "birthDate", "gender")} if p else None
        self._pcache[pid] = (time.monotonic(), p)
        return p

    def _session_dob(self, rid: str, sub: str) -> str | None:
        s = self.store.one("SELECT birth_date FROM sessions WHERE request_id=? AND bpk=? ORDER BY expires_at DESC LIMIT 1",
                           (rid, sub))
        return s["birth_date"] if s else None

    def consume_jti(self, jti: str) -> bool:
        """Single use of an IdP token, atomically (PRIMARY KEY): False = replay."""
        try:
            self.store.exec("INSERT INTO used_assertions (jti, at) VALUES (?,?)", (jti, iso(now())))
            return True
        except sqlite3.IntegrityError:
            return False

    def open_recipients(self) -> list[str]:
        """DISTINCT names of recipients of open requests (cached 5 s: used on every eID page)."""
        hit = getattr(self, "_open_rcpt", None)
        if hit and time.monotonic() - hit[0] < 5:
            return hit[1]
        rows = self.store.q("SELECT recipient_name, expires_at FROM requests WHERE status NOT IN (%s)"
                            % ",".join("?" * len(CLOSED)), tuple(sorted(CLOSED)))
        t = now()  # compared as datetimes (string comparison would be off by an hour across a DST change)
        names = sorted({x["recipient_name"] for x in rows if datetime.fromisoformat(x["expires_at"]) > t})
        self._open_rcpt = (time.monotonic(), names)
        return names

    def _protected_names(self) -> list[str]:
        """Names the simulated IdP will not hand out without the persona's PIN."""
        reg = self.registers.data
        names = [e["name"] for e in reg["aerzteliste"]["entries"]]
        names += [s["name"] for e in reg["gda_index"]["entries"] for s in e.get("staff") or []]
        return names + self.open_recipients()

    def session(self, r: dict, sid: str | None) -> dict | None:
        if not sid:
            return None
        s = self.store.one("SELECT * FROM sessions WHERE sid_hash=? AND request_id=?", (sha256(sid), r["id"]))
        if not s or now() > datetime.fromisoformat(s["expires_at"]):
            return None
        if r["bound_bpk"] and s["bpk"] != r["bound_bpk"]:
            return None
        return s

    def csrf(self, sid: str) -> str:
        return hmac.new(self.k_csrf, sid.encode(), hashlib.sha256).hexdigest()[:32]

    # ================================================================== assessment (pure: no side effects)
    def assess(self, raw: bytes, r: dict, sender: dict, requested: dict | None) -> dict:
        """tests/jury/RULES.md for one upload = ONE call to fhirmap.decide() (single implementation of the
        safety rule; agreement with the jury corpus + fuzz is pinned by tests/test_rules_agree.py).
        Returns {outcome: file|hold|block, block_kind: content|wrong_patient|identity|None, reasons, codes,
        mismatches, parsed, text}. Reasons never contain the other patient's name or DOB."""
        out = {"outcome": "file", "block_kind": None, "reasons": [], "codes": [], "mismatches": [], "parsed": None, "text": None,
               "decision": None}
        if requested is None:
            out.update(outcome="hold", codes=["REQUESTED_PATIENT_MISSING"],
                       reasons=["Angefragter Patient ist am FHIR-Server nicht vorhanden."])
            return out
        req_ctx = {"doc_kind": r["doc_kind"], "requester_name": self.registers.member_name(r["requester_mxid"]),
                   "recipient": {"name": r["recipient_name"], "org": r.get("recipient_org")}}
        eid = {"given_name": sender["given"], "family_name": sender["family"], "birthDate": sender.get("birthdate")}
        photo = fhirmap.transcribe.sniff(raw) if self.extractor is not None else None
        if photo and len(raw) > self.s.max_photo_bytes:
            out.update(outcome="block", block_kind="content", codes=["PHOTO_OVERSIZE"],
                       reasons=["Foto/PDF größer als 5 MB"])
            return out
        try:
            if photo:  # two independent AI readings; the agreed text goes through the same decide() below
                ex = self.extractor.transcribe(raw, photo)
                parsed, d = fhirmap.transcribe.decide_transcribed(ex["reads"], raw, photo, ex["model"], request=req_ctx,
                                                                  patient=requested, identity=eid,
                                                                  doc_kind_hint=r["doc_kind"], error=ex["error"])
            else:
                parsed = fhirmap.parse_upload(raw, r["doc_kind"])  # UTF-8, else Windows-1252/Latin-1 (with a warning)
            text = get(parsed, "text")
        except Exception as e:  # unparseable content is a hold, never a crash
            log.exception("parse failed")
            out.update(outcome="hold", codes=["PARSE_ERROR"], reasons=[f"Dokument konnte nicht gelesen werden ({type(e).__name__})."])
            return out
        if not photo:
            d = fhirmap.decide(parsed, req_ctx, requested, eid, raw)
        out["outcome"] = d["outcome"].lower()
        out["codes"], out["reasons"] = list(d["codes"]), list(d["reasons"])
        if out["outcome"] == "block":
            bc = set(d.get("block_codes") or d["codes"])
            out["block_kind"] = ("wrong_patient" if "WRONG_PATIENT" in bc else
                                 "identity" if bc == {"EID_MISMATCH"} else "content")
            return out  # nothing of a blocked upload is kept, not even the parse
        out["decision"] = d
        out["mismatches"] = list(d.get("details") or [])
        if not out["mismatches"] and "PATIENT_MISSING" in out["codes"]:  # a row for the hold card (display only)
            bd = requested.get("birthDate") or ""
            out["mismatches"] = [{"field": "patient_line", "document": None,
                                  "request": f"{fhir_patient_name(requested)}, {bd[8:10]}.{bd[5:7]}.{bd[0:4]}"}]
        out["parsed"], out["text"] = parsed, text
        return out

    # ================================================================== answer intake (synchronous checks)
    def receive_answer(self, r: dict, sess: dict, raw: bytes, filename: str | None, ip: str | None,
                       ua: str | None) -> dict:
        """Audit 'answer sent' (hash + size only, never the filename), assess, then block / hold / accept.
        Returns {"outcome": "accepted"|"hold"|"block", "block_kind", "answer_id"}."""
        self.purge()
        h = hashlib.sha256(raw).hexdigest()
        sender = f"{sess['given']} {sess['family']}"
        who = f"{sender} (ID Austria – Simulation, {sess['bpk']})"
        identity = {"given_name": sess["given"], "family_name": sess["family"], "bpk_hash": sess["bpk"],
                    "loa": "simulated", "org": r["recipient_org"], "assertion": sess["statement"], "simulated": True,
                    "method": "ID Austria – Simulation (kein echtes ID Austria)", "idp": "mock-id-austria",
                    "relation_to_named_person": sess["verdict"], "requested_name": r["recipient_name"], "verified_at": None}
        aid = self.store.insert("answers", {"request_id": r["id"], "received_at": iso(now()), "sender_bpk": sess["bpk"],
                                            "sender_name": sender, "sender_verdict": sess["verdict"], "identity": identity,
                                            "filename": None, "sha256": h, "size": len(raw), "content": None, "encrypted": 0})
        # earlier answers are replaced by this one; a HELD one is marked so Dr. Hofer is told once this one is filed
        self.store.exec("UPDATE answers SET content=NULL, outcome='superseded_held' WHERE request_id=? AND id<>? "
                        "AND outcome IN ('held_review','held_technical')", (r["id"], aid))
        self.store.exec("UPDATE answers SET content=NULL, outcome='superseded' WHERE request_id=? AND id<>? "
                        "AND outcome IS NULL", (r["id"], aid))
        self.audit(r["id"], "answer sent", True, who,
                   f"Antwort auf {r['ref_code']} übermittelt (Datei, {len(raw)} Bytes, SHA-256 {h}).",
                   ip=ip, ua=ua, detail={"answer_id": aid, "sha256": h, "size": len(raw)})
        # decide() on the request path against the Patient snapshot taken when the request was created (no HAPI
        # call, ~ms): a wrong-patient or unsafe file is rejected at once and never stored. The worker re-checks
        # everything against the live Patient before filing.
        requested = self.patient(r["patient_id"])  # in-memory cache (no HAPI call on the hot path)
        a = self.assess(raw, r, {"given": sess["given"], "family": sess["family"], "verdict": sess["verdict"],
                                 "birthdate": sess.get("birth_date")}, requested)
        if a["outcome"] == "block":
            # nothing of the document is stored; alert + audit never describe the other person or the content
            self.store.exec("UPDATE answers SET outcome=?, reasons=? WHERE id=?",
                            (f"blocked_{a['block_kind']}", json.dumps(a["codes"]), aid))
            de = reasons_de(a["codes"])
            self.store.update_request(r["id"], misdeliveries=r["misdeliveries"] + 1, last_reason="; ".join(de))
            if a["block_kind"] == "wrong_patient":
                what = f"Antwort auf {r['ref_code']}: {WRONG_PATIENT_TEXT} Datei verworfen und nicht gespeichert (SHA-256 {h}); Absender informiert."
                alert = (f"[FEHLZUSTELLUNG ABGEFANGEN] Antwort von {sender}, angemeldet mit ID Austria (Simulation), auf "
                         f"{r['ref_code']}: Dokument betrifft eine andere Person – Name und Geburtsdatum passen nicht zu "
                         f"{(fhir_patient_name(requested) if requested else None) or 'der angefragten Person'}. "
                         f"Nicht gespeichert, nicht abgelegt; {sender} wurde informiert.")
            else:
                what = (f"Antwort auf {r['ref_code']} abgewiesen ({', '.join(a['codes'])}): {_sentences(de)}. "
                        f"Datei verworfen und nicht gespeichert (SHA-256 {h}).")
                technical = bool(set(a["codes"]) & TECHNICAL) and not set(a["codes"]) & {"ACTIVE_CONTENT", "WRONG_PATIENT"}
                alert = ((f"ℹ️ {r['ref_code']}: Datei von {sender} nicht übernommen – {_sentences(de)}. "
                          f"Nichts wurde gespeichert; {sender} wurde gebeten, sie neu zu senden.") if technical else
                         (f"[BLOCKIERT] Antwort von {sender}, angemeldet mit ID Austria (Simulation), auf {r['ref_code']} "
                          f"abgewiesen: {_sentences(de)}. Nichts wurde gespeichert oder abgelegt."))
            self.audit(r["id"], "misdelivery caught", True, who, what,
                       detail={"reason": a["block_kind"], "checks": a["codes"], "sha256": h})
            self.notify(r["room_id"], kind="misdelivery", request_id=r["id"], text=alert,
                        data={"ref_code": r["ref_code"], "codes": a["codes"], "reasons_de": de, "sender": sender,
                              # the card's headline names the REQUESTED patient and document, never the other person
                              "doc_kind": r["doc_kind"], "patient": self._patient_card(requested),
                              "technical": a["block_kind"] != "wrong_patient" and bool(set(a["codes"]) & TECHNICAL)
                              and not set(a["codes"]) & {"ACTIVE_CONTENT", "WRONG_PATIENT"}})
            return {"outcome": "block", "block_kind": a["block_kind"], "answer_id": aid, "codes": a["codes"]}
        self._store_content(aid, r, raw)  # the RAW bytes (one hash everywhere), encrypted at rest
        self.store.update_request(r["id"], status="processing")
        self.worker.submit(r["id"], aid, None)  # hold/file decided by the worker against the live Patient
        return {"outcome": "accepted", "block_kind": None, "answer_id": aid}

    def decline(self, r: dict, sess: dict, reason: str, ip: str | None, ua: str | None) -> None:
        """'Kein Befund vorhanden / nicht zuständig'. FHIR Task semantics: the named performer declines to perform
        -> status 'rejected' with statusReason (not 'completed': nothing was produced, and an empty 'completed'
        output would read as a filed document). Audited as the answer, Dr. Hofer is told, the link closes."""
        reason = re.sub(r"\s+", " ", (reason or "").strip())[:300]
        sender = f"{sess['given']} {sess['family']}"
        who = f"{sender} (ID Austria – Simulation, {sess['bpk']})"
        txt = "Kein passender Befund vorhanden / nicht zuständig" + (f": {reason}" if reason else "")
        self.store.insert("answers", {"request_id": r["id"], "received_at": iso(now()), "sender_bpk": sess["bpk"],
                                      "sender_name": sender, "sender_verdict": sess["verdict"], "identity": None,
                                      "filename": None, "sha256": sha256(txt), "size": len(txt.encode()), "content": None,
                                      "encrypted": 0, "outcome": "declined"})
        self.audit(r["id"], "answer sent", True, who, f"Antwort auf {r['ref_code']}: {txt}.", ip=ip, ua=ua,
                   detail={"declined": True})
        self._set_status(r["id"], "declined", txt, last_reason=txt, token_hash="declined-" + secrets.token_hex(16))
        self.notify(r["room_id"], f"{r['recipient_org'] or ''} ({r['recipient_name']}, ID Austria – Simulation) zu {r['ref_code']}: "
                                  f"kein passender Befund" + (f" – „{reason}“" if reason else "") + ". Die Anfrage ist geschlossen.",
                    kind="no_result", request_id=r["id"],
                    data={"doc_kind": r["doc_kind"], "patient": self._patient_card(self.patient(r["patient_id"])),
                          "sender": f"{sender} (ID Austria – Simulation)", "comment": reason or None,
                          "received_at": iso(now()), "ref_code": r["ref_code"]})

    def keep_upload_for_relogin(self, r: dict, expired_sess: dict, raw: bytes) -> None:
        """The session expired while answering: keep the file ENCRYPTED (bound to that person) until re-login."""
        blob, enc = self.box.seal(base64.b64encode(raw).decode(), aad=f"pending:{r['id']}:{expired_sess['bpk']}")
        self.store.exec("DELETE FROM pending_uploads WHERE request_id=? AND bpk=?", (r["id"], expired_sess["bpk"]))
        self.store.insert("pending_uploads", {"request_id": r["id"], "bpk": expired_sess["bpk"], "blob": blob, "encrypted": int(enc),
                                              "expires_at": iso(now() + timedelta(minutes=retention.PENDING_UPLOAD_MIN))})

    def take_pending_upload(self, r: dict, sess: dict) -> bytes | None:
        """After re-login by the SAME person (same pairwise subject): hand back the kept file once."""
        p = self.store.one("SELECT * FROM pending_uploads WHERE request_id=? AND bpk=? ORDER BY id DESC LIMIT 1",
                           (r["id"], sess["bpk"]))
        self.store.exec("DELETE FROM pending_uploads WHERE request_id=? AND bpk=?", (r["id"], sess["bpk"]))
        if not p or now() > datetime.fromisoformat(p["expires_at"]):
            return None
        return base64.b64decode(self.box.open(p["blob"], bool(p["encrypted"]), aad=f"pending:{r['id']}:{sess['bpk']}"))

    def expired_session(self, r: dict, sid: str | None) -> dict | None:
        if not sid:
            return None
        s = self.store.one("SELECT * FROM sessions WHERE sid_hash=? AND request_id=?", (sha256(sid), r["id"]))
        return s if s and now() > datetime.fromisoformat(s["expires_at"]) else None

    def receipt(self, sid: str | None) -> dict | None:
        """Receipt for the outside person: reference, time, SHA-256 and size of what SHE sent. No patient data."""
        if not sid:
            return None
        s = self.store.one("SELECT * FROM sessions WHERE sid_hash=?", (sha256(sid),))
        if not s:
            return None
        r = self.store.get_request(s["request_id"])
        a = self.store.one("SELECT * FROM answers WHERE request_id=? AND sender_bpk=? ORDER BY id DESC LIMIT 1",
                           (r["id"], s["bpk"]))
        if not a:
            return None
        if (a["outcome"] or "").startswith("blocked_"):
            # a refused upload leaves NO receipt: it was neither kept nor passed on
            return {"ref": r["ref_code"], "blocked": True, "block_kind": a["outcome"][len("blocked_"):],
                    "codes": a["reasons"] or []}
        at = datetime.fromisoformat(a["received_at"]).astimezone(VIENNA)
        return {"ref": r["ref_code"], "blocked": False, "at": at.strftime("%d.%m.%Y %H:%M:%S"), "sha256": a["sha256"], "size": a["size"],
                "declined": a["outcome"] == "declined", "to": self.registers.member_name(r["requester_mxid"]) or "Klinikum",
                "clinic": self.s.clinic_name, "state": "processing" if r["status"] == "processing" else "received"}

    def filed_by_session(self, sid: str | None) -> dict | None:
        """The receipt, if THIS session's person sent the answer that was filed (for her 'abgelegt' page after the
        link was retired); None for anyone else."""
        if not sid:
            return None
        s = self.store.one("SELECT * FROM sessions WHERE sid_hash=?", (sha256(sid),))
        if not s:
            return None
        r = self.store.get_request(s["request_id"])
        a = self.store.one("SELECT outcome FROM answers WHERE request_id=? AND sender_bpk=? ORDER BY id DESC LIMIT 1",
                           (r["id"], s["bpk"]))
        if r["status"] != "filed" or not a or a["outcome"] != "filed":
            return None
        return self.receipt(sid)

    def session_state(self, sid: str | None) -> dict | None:
        """Outside-person status page: 'processing', 'received' or 'blocked' (her last file was refused; nothing the
        answer page did not show before).
        Keyed by the session secret, so it keeps working after the link token was retired on filing."""
        if not sid:
            return None
        s = self.store.one("SELECT * FROM sessions WHERE sid_hash=?", (sha256(sid),))
        if not s or now() > datetime.fromisoformat(s["expires_at"]):
            return None
        r = self.store.get_request(s["request_id"])
        a = self.store.one("SELECT outcome, reasons FROM answers WHERE request_id=? AND sender_bpk=? ORDER BY id DESC LIMIT 1",
                           (r["id"], s["bpk"]))
        if a and (a["outcome"] or "").startswith("blocked_"):
            return {"state": "blocked", "ref": r["ref_code"], "block_kind": a["outcome"][len("blocked_"):],
                    "codes": a["reasons"] or []}
        return {"state": "processing" if r["status"] == "processing" else "received", "ref": r["ref_code"]}

    def _store_content(self, aid: int, r: dict, raw: bytes) -> None:
        """Keep the upload's RAW bytes (base64 inside the AES-GCM box): filing, Provenance and receipt then hash
        exactly what was uploaded, whatever its character set."""
        blob, enc = self.box.seal(base64.b64encode(raw).decode("ascii"), aad=f"{r['id']}:{aid}")
        self.store.exec("UPDATE answers SET content=?, encrypted=?, content_raw=1, purge_after=? WHERE id=?",
                        (blob, int(enc), iso(now() + timedelta(days=self.s.held_ttl_days)), aid))

    def _load_raw(self, a: dict) -> bytes | None:
        """The stored upload as bytes (rows from before content_raw hold the decoded text: its UTF-8 bytes)."""
        if a["content"] is None:
            return None
        plain = self.box.open(a["content"], bool(a["encrypted"]), aad=f"{a['request_id']}:{a['id']}")
        if a.get("content_raw"):
            return base64.b64decode(plain)
        return plain.encode("utf-8")

    def _load_text(self, a: dict) -> str | None:
        raw = self._load_raw(a)
        if raw is None:
            return None
        return fhirmap.decode_upload(raw)["text"] or ""

    def _purge_answers(self, rid: str) -> None:
        self.store.exec("UPDATE answers SET content=NULL WHERE request_id=?", (rid,))

    def purge(self) -> int:
        rows = self.store.q("SELECT id FROM answers WHERE content IS NOT NULL AND purge_after < ?", (iso(now()),))
        for a in rows:
            self.store.exec("UPDATE answers SET content=NULL, outcome=COALESCE(outcome,'')||' purged' WHERE id=?", (a["id"],))
        return len(rows)

    def maintenance(self) -> dict:
        """Timer job (retention.MAINTENANCE_EVERY_S): retention purges + external anchor of the audit chain head."""
        t = now()
        out = {"held_uploads": self.purge()}
        with self.store.tx() as c:
            out["sessions"] = c.execute("DELETE FROM sessions WHERE expires_at < ?",
                                        (iso(t - timedelta(hours=retention.SESSION_ROW_HOURS)),)).rowcount
            out["assertions"] = c.execute("DELETE FROM used_assertions WHERE at < ?",
                                          (iso(t - timedelta(hours=retention.ASSERTION_ROW_HOURS)),)).rowcount
            dead = [r["id"] for r in c.execute("SELECT id, status, expires_at FROM requests").fetchall()
                    if datetime.fromisoformat(r["expires_at"]) < t or r["status"] in CLOSED - {"needs_confirmation"}]
            out["sms"] = sum(c.execute("DELETE FROM sms WHERE request_id=?", (rid,)).rowcount for rid in dead)
            out["request_pii"] = c.execute(
                "UPDATE requests SET address='[gelöscht]', note=NULL, patient_snapshot=NULL "
                "WHERE expires_at < ? AND address <> '[gelöscht]'", (iso(t),)).rowcount
            # the cleartext contact beside the audit chain (the chained rows keep only its keyed hash)
            out["audit_contacts"] = c.execute(
                "DELETE FROM audit_private WHERE request_id IN (SELECT id FROM requests WHERE expires_at < ?)",
                (iso(t),)).rowcount
            out["request_text"] = c.execute(
                "UPDATE requests SET request_text=NULL WHERE request_text IS NOT NULL AND (expires_at < ? OR status='filed')",
                (iso(t),)).rowcount
        out["anchor"] = self.anchor()
        out["tasks"] = self.reconcile_tasks()
        out["audit_sync"] = self.sync_audit()  # re-send AuditEvents whose push failed
        if time.monotonic() - getattr(self, "_reverified_at", -1e9) >= retention.PROVENANCE_REVERIFY_S:
            self._reverified_at = time.monotonic()
            summary = self.reverify_all()
            out["provenance"] = {k: v for k, v in summary.items() if k != "failed"} | {"failed": len(summary["failed"])}
        return out

    def anchor(self, force: bool = False) -> dict | None:
        """Publish the chain head (id + keyed hash) OUTSIDE the service: to Dr. Hofer's room (Matrix) and to HAPI
        as an AuditEvent (create-only there: pgk.AppendOnlyInterceptor). verify_anchors() reads both back."""
        head = self.store.one("SELECT id, hash, at FROM audit ORDER BY id DESC LIMIT 1")
        last = self.store.one("SELECT head_id FROM audit_anchors ORDER BY head_id DESC LIMIT 1")
        if not head or (last and last["head_id"] == head["id"] and not force):
            return None
        head_id, head_hash = head["id"], head["hash"]
        self.store.exec("INSERT OR IGNORE INTO audit_anchors (head_id, head_hash, at) VALUES (?,?,?)",
                        (head_id, head_hash, iso(now())))
        mac = anchorlib.anchor_mac(self.k_audit, self.chain_id, head_id, head_hash)
        event = anchorlib.anchor_event(self.chain_id, head_id, head_hash, iso(now()), mac)
        text = anchorlib.matrix_text(self.chain_id, head_id, head_hash, mac)

        def post_fhir():
            ref = self.hapi.create(event)
            self.store.exec("UPDATE audit_anchors SET fhir_ref=? WHERE head_id=? AND fhir_ref IS NULL", (ref, head_id))

        def post_matrix():
            event_id = self.bot.notify(self.anchor_room, text)
            if isinstance(event_id, str):
                self.store.exec("UPDATE audit_anchors SET matrix_event=? WHERE head_id=? AND matrix_event IS NULL",
                                (event_id, head_id))
            return event_id

        self.outbox.put(post_fhir, desc="audit anchor (HAPI)")
        self.outbox.put(post_matrix, desc="audit anchor (Matrix)")
        return {"head_id": head_id, "head_hash": head_hash, "chain": self.chain_id}

    @property
    def anchor_room(self) -> str | None:
        """The audit room ('Befundlink Audit'), so the hex anchors never sit between patient cards."""
        return self.s.audit_room_id or self.s.default_room_id

    def verify_anchors(self, rows: list[dict] | None = None, external: bool = True) -> dict:
        """The anchors, reported separately per place:
        - local:  the local audit_anchors table vs the chain;
        - hapi:   the anchor AuditEvents found BY SEARCHING HAPI vs the chain;
        - matrix: the anchor messages found IN THE ROOM HISTORY vs the chain.
        The external places are read independently of the local table, so emptying or rewriting it hides nothing."""
        if rows is None:
            rows = self.store.q("SELECT id, hash FROM audit ORDER BY id")
        chain_hashes = {r["id"]: r["hash"] for r in rows}
        local = self.store.q("SELECT * FROM audit_anchors ORDER BY head_id")
        local_ids = {a["head_id"] for a in local}
        res = {"chain": self.chain_id, "room": self.anchor_room,
               "local": anchorlib.compare([(a["head_id"], a["head_hash"], None) for a in local], chain_hashes, set())}
        if not external:
            return res
        try:
            hapi_found = anchorlib.read_hapi_anchors(self.http, self.s.hapi_url, self.chain_id)
            res["hapi"] = anchorlib.compare(hapi_found, chain_hashes, local_ids, self.k_audit, self.chain_id)
        except (httpx.HTTPError, ValueError) as e:
            res["hapi"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        configured = bool(self.s.matrix_url and self.s.matrix_token and self.anchor_room)
        if not configured:
            res["matrix"] = {"configured": False}
            return res
        try:
            matrix_found = anchorlib.read_matrix_anchors(self.http, self.s.matrix_url, self.s.matrix_token,
                                                         self.anchor_room, self.chain_id)
            res["matrix"] = {"configured": True, **anchorlib.compare(matrix_found, chain_hashes, local_ids,
                                                                     self.k_audit, self.chain_id)}
        except (httpx.HTTPError, ValueError, KeyError) as e:
            res["matrix"] = {"configured": True, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        return res

    @staticmethod
    def anchor_problems(check: dict) -> list[str]:
        problems = []
        for where in ("local", "hapi", "matrix"):
            bucket = check.get(where) or {}
            if bucket.get("error"):
                problems.append(f"{where} anchors could not be read: {bucket['error']}")
            for head_id in bucket.get("bad") or []:
                problems.append(f"{where} anchor at #{head_id} does not match the chain")
        return problems

    # ================================================================== filing
    def process_batch(self, items: list[tuple]) -> list[dict]:
        """The filing pipeline for one or more queued answers (AnswerWorker): re-check -> build -> ONE
        validation pass for all of them -> per answer prove -> transaction -> record -> notify."""
        prepared, results = [], {}
        t0 = time.perf_counter()
        for rid, aid, override in items:
            try:
                ctx = self._prepare(rid, aid, override)
            except Exception as e:
                log.exception("prepare failed")
                ctx = self._fail(rid, aid, e)
            if isinstance(ctx, dict) and "resources" in ctx:
                prepared.append(ctx)
            else:
                results[aid] = ctx
        t1 = time.perf_counter()
        validated = self._validate_batch(prepared)
        t2 = time.perf_counter()
        for ctx, (errors, vrep) in zip(prepared, validated):
            try:
                results[ctx["aid"]] = self._finalize(ctx, errors, vrep)
            except Exception as e:
                log.exception("filing failed")
                results[ctx["aid"]] = self._fail(ctx["rid"], ctx["aid"], e)
        t3 = time.perf_counter()
        log.info("batch of %d (%d to file): prepare %.2fs, validate %.2fs, finalize %.2fs", len(items), len(prepared),
                 t1 - t0, t2 - t1, t3 - t2)
        return [results[aid] for _, aid, _ in items]

    def _fail(self, rid: str, aid: int, e: Exception) -> dict:
        r = self.store.get_request(rid)
        a = self.store.one("SELECT sender_name, sender_bpk FROM answers WHERE id=?", (aid,)) or {}
        log.warning("filing of %s failed: %s: %s", r["ref_code"], type(e).__name__, str(e)[:300])
        return self._hold(r, aid, "held_technical", ["Ablage im Moment nicht möglich (FHIR-Server oder Prüfung nicht "
                                                     "erreichbar). Die Antwort bleibt sicher angehalten – bitte später "
                                                     "„ablegen“ erneut wählen."],
                          f"{a.get('sender_name')} (ID Austria – Simulation, {a.get('sender_bpk')})",
                          detail={"checks": ["FILING_UNAVAILABLE"], "error_class": type(e).__name__})

    def _prepare(self, rid: str, aid: int, override: dict | None) -> dict:
        """Re-check against the LIVE Patient on HAPI and build the resources. Returns a ctx with 'resources',
        or the result of a hold."""
        r = self.store.get_request(rid)
        a = self.store.one("SELECT * FROM answers WHERE id=?", (aid,))
        sender = a["sender_name"]
        who = f"{sender} (ID Austria – Simulation, {a['sender_bpk']})"
        pref = f"Patient/{r['patient_id']}"
        content = self._load_raw(a)
        if content is None:
            return self._hold(r, aid, "held_technical", ["Inhalt nicht mehr vorhanden (abgelaufen/verworfen)."], who)
        requested = _retry(lambda: self.hapi.get_patient(r["patient_id"]), safe_on=(httpx.HTTPError, FhirError))
        if requested is None:
            return self._hold(r, aid, "held_technical", ["Angefragter Patient ist am FHIR-Server nicht vorhanden."], who)
        sender_parts = {"given": a["identity"].get("given_name"), "family": a["identity"].get("family_name"),
                        "verdict": a["sender_verdict"], "birthdate": self._session_dob(rid, a["sender_bpk"])}
        chk = self.assess(content, r, sender_parts, requested)  # authoritative check vs the live Patient (raw bytes)
        if chk["outcome"] == "block":  # an override can never file another person's or unsafe content
            self._purge_answers(rid)
            return self._hold(r, aid, "held_technical", reasons_de(chk["codes"])[:1], who)
        if chk["outcome"] == "hold" and override is None:
            card = decision_card_fields(chk["decision"])
            if "CORRECTION_REVIEW" in chk["codes"]:
                # the SAME lookup as on filing: name the report it would overwrite (or say it is not unique)
                card["prior_report"] = self._prior_report_card(r, chk["parsed"], pref)
                card["replaces"] = {"text": card["prior_report"]["text"]}
            return self._hold(r, aid, "held_review", reasons_de(chk["codes"], chk["mismatches"]), who,
                              detail={"checks": chk["codes"], "facts": self._facts(chk), "charset_note": charset_note(chk["parsed"]),
                                      "card": card,
                                      "other_person_present": bool((chk["decision"] or {}).get("other_person_present"))},
                              mismatches=chk["mismatches"])
        parsed = chk["parsed"]
        if parsed is None:
            return self._hold(r, aid, "held_technical", chk["reasons"] or ["Dokument nicht lesbar."], who)
        identity = dict(a["identity"] or {})
        identity["verified_at"] = a["received_at"]
        author = get(parsed, "author") or {}
        author_reg = self.registers.find_physician(get(author, "name") or "") if get(author, "name") else None
        req_info = {"request_id": r["id"], "id": r["id"], "ref_code": r["ref_code"],
                    "recipient": {"name": r["recipient_name"], "role": r["recipient_role"], "org": r["recipient_org"]},
                    "task_ref": f"Task/{r['task_id']}" if r["task_id"] else None,
                    "requester_mxid": r["requester_mxid"], "recipient_name": r["recipient_name"],
                    "recipient_role": r["recipient_role"], "recipient_org": r["recipient_org"], "doc_kind": r["doc_kind"],
                    "channel": r["channel"], "address": r["address"], "sent_at": r["sent_at"],
                    "answer_sha256": a["sha256"], "filename": None, "register": r["register"],
                    "role_basis": "register" if (r["register"] or {}).get("ok") else "asserted by requester",
                    "author_register": author_reg, "policy": (r["register"] or {}).get("policy"),
                    "override": override,
                    # the eID-verified sender IS the requested patient (fhirmap: Provenance agent = the Patient)
                    "sender_is_patient": self.recipient_is_patient(r, requested),
                    "requester_refs": self.requester_refs(r["requester_mxid"]),
                    "patient": requested}  # fhirmap: every subject.display = this Patient's name, never the document's
        if override is not None:
            # an override never files a unit nobody could read: those values go in as text, exactly as written
            parsed = text_only_rows(parsed, chk["decision"])
        correction = None
        if override is not None and "CORRECTION_REVIEW" in (chk["codes"] or []):
            # a correction is filed as NEW VERSIONS of the report it replaces, never as a second report beside it
            correction = self._build_correction(r, parsed, pref, req_info, identity, a, chk["decision"])
            if correction.get("problem"):
                return self._hold(r, aid, "held_review", [PRIOR_NOT_FOUND_DE + " " + correction["problem"]], who,
                                  detail={"checks": ["PRIOR_NOT_FOUND"], "card": decision_card_fields(chk["decision"])})
        try:
            if correction:
                resources = correction["resources"]
            else:
                resources = self.fm.build_resources(parsed, patient_ref=pref, request=req_info, identity=identity,
                                                    received_at=a["received_at"], allow_hold=override is not None,
                                                    decision=chk["decision"])
        except ValueError as e:
            # e.g. 'cannot build resources for kind unknown': the answer stays HELD and intact; a clear German reason,
            # never a dead end ('Technischer Fehler'). Dr. Hofer can ask the sender for another format or discard.
            log.info("build not possible for %s: %s", r["ref_code"], e)
            kind = get(parsed, "kind")
            why = ("Dokumenttyp nicht erkannt" if kind in (None, "unknown") else "Dokument lässt sich so nicht strukturiert ablegen")
            return self._hold(r, aid, "held_review", [f"Ablegen nicht möglich: {why}. Die Antwort bleibt angehalten – "
                                                      f"bitte beim Absender ein Dokument im erwarteten Format anfordern oder "
                                                      f"verwerfen."], who, detail={"checks": ["BUILD_NOT_POSSIBLE"]})
        binding = fhirmap.provenance_binding(resources)
        fhirmap.attach_provenance_signature(resources, self.sender_statement(r, identity, binding))
        main = next((x for x in resources if x["resourceType"] in ("DiagnosticReport", "DocumentReference")), None)
        task = self._put_task(r, "completed", output_refs=[{"reference": fhirmap.ref_of(main)}] if main else None)
        self._bump_task_seq(rid)  # any status PUT still queued is now older than the filing -> dropped
        return {"rid": rid, "aid": aid, "override": override, "r": r, "a": a, "who": who, "pref": pref,
                "parsed": parsed, "requested": requested, "resources": resources, "task": task, "decision": chk["decision"],
                "signed_targets": binding["targets"], "correction": correction}

    # ---------------------------------------------------------------- corrections (CORRECTION_REVIEW)
    def _prior_reports(self, r: dict, parsed, pref: str) -> list[dict]:
        """DiagnosticReports on HAPI that the correction says it replaces: same patient, fhirmap.matches_prior (the
        stated time equals issued/effective), and - when the report names its performers - the recipient's org."""
        resp = self.http.get(f"{self.s.hapi_url}/DiagnosticReport", params={"subject": pref, "_count": 500},
                             headers={"Accept": "application/fhir+json"})
        if resp.status_code != 200:
            raise FhirError(f"search DiagnosticReport: HTTP {resp.status_code}")
        found = []
        for e in resp.json().get("entry") or []:
            dr = e.get("resource") or {}
            if (dr.get("subject") or {}).get("reference") != pref or not fhirmap.matches_prior(parsed, dr):
                continue
            performers = [org_key(x.get("display")) for x in dr.get("performer") or [] if x.get("display")]
            if performers and r["recipient_org"] and org_key(r["recipient_org"]) not in performers:
                continue
            found.append(dr)
        return found

    def _ref_code_of_report(self, dr_ref: str) -> str | None:
        """The KB- reference of the request that filed this report (if it was filed by us)."""
        for row in self.store.q("SELECT ref_code, filed FROM requests WHERE status='filed'"):
            if dr_ref in ((row["filed"] or {}).get("resources") or []):
                return row["ref_code"]
        return None

    def _prior_report_card(self, r: dict, parsed, pref: str) -> dict:
        """For the hold card: which earlier report this correction would overwrite, as one clean text:
        'ersetzt KB-D446FF, Probe 28.10.2026, 14:37' | '2 frühere Befunde passen – nicht eindeutig' |
        'kein passender früherer Befund gefunden'."""
        try:
            priors = self._prior_reports(r, parsed, pref)
        except (httpx.HTTPError, FhirError) as e:
            return {"found": None, "text": "früherer Befund nicht prüfbar (FHIR-Server nicht erreichbar)",
                    "problem": str(e)[:200]}
        if not priors:
            return {"found": 0, "text": "kein passender früherer Befund gefunden"}
        if len(priors) > 1:
            return {"found": len(priors), "text": f"{len(priors)} frühere Befunde passen – nicht eindeutig"}
        dr = priors[0]
        ref = f"DiagnosticReport/{dr['id']}"
        ref_code = self._ref_code_of_report(ref)
        sample = dr.get("effectiveDateTime") or dr.get("issued")
        parts = [f"ersetzt {ref_code or ref}"]
        if sample:
            parts.append(f"Probe {_de_datetime(sample)}")
        return {"found": 1, "reference": ref, "ref_code": ref_code, "sample_time": sample, "text": ", ".join(parts)}

    def _build_correction(self, r: dict, parsed, pref: str, req_info: dict, identity: dict, a: dict,
                          decision: dict | None) -> dict:
        """fhirmap.build_correction against the ONE prior report. {'resources', 'unchanged', 'not_in_correction',
        'prior', 'versions'} or {'problem': text} (none / several matches, or build_correction refused)."""
        try:
            priors = self._prior_reports(r, parsed, pref)
        except (httpx.HTTPError, FhirError) as e:
            return {"problem": f"FHIR-Server nicht lesbar ({str(e)[:100]})."}
        if not priors:
            return {"problem": "Kein passender früherer Befund am FHIR-Server."}
        if len(priors) > 1:
            return {"problem": f"{len(priors)} frühere Befunde passen (nicht eindeutig)."}
        prior = priors[0]
        observations = [self.hapi.read(x["reference"]) for x in prior.get("result") or []
                        if (x.get("reference") or "").startswith("Observation/")]
        try:
            out = fhirmap.build_correction(parsed, prior, observations, pref, req_info, identity, a["received_at"],
                                           decision=decision)
        except ValueError as e:
            return {"problem": str(e)[:200]}
        # optimistic locking: each new version is PUT with If-Match on the version we corrected
        versions = {f"DiagnosticReport/{prior['id']}": (prior.get("meta") or {}).get("versionId")}
        for o in observations:
            versions[f"Observation/{o['id']}"] = (o.get("meta") or {}).get("versionId")
        return {**out, "prior": f"DiagnosticReport/{prior['id']}", "versions": versions}

    def _void_superseded_holds(self, r: dict) -> None:
        """A later answer on the same link was filed: the hold cards of earlier HELD answers are void. One notice to
        the room (kind 'holds_superseded'; plain text for a bot that does not know it), audited."""
        held = self.store.q("SELECT id FROM answers WHERE request_id=? AND outcome='superseded_held' ORDER BY id", (r["id"],))
        if not held:
            return
        ids = [x["id"] for x in held]
        self.store.exec(f"UPDATE answers SET outcome='superseded' WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
        text = (f"Frühere angehaltene Antwort(en) zu {r['ref_code']} ersetzt: die neue Antwort wurde abgelegt – "
                f"keine Entscheidung mehr nötig.")
        self.audit(r["id"], "holds superseded", True, "Befundlink", text, detail={"answer_ids": ids})
        self.notify(r["room_id"], text, kind="holds_superseded", request_id=r["id"],
                    data={"ref_code": r["ref_code"], "request_id": r["id"], "answer_ids": [str(i) for i in ids]})

    def _finalize(self, ctx: dict, errors: list[str], vrep: dict) -> dict:
        r, a, aid, rid, who, pref = ctx["r"], ctx["a"], ctx["aid"], ctx["rid"], ctx["who"], ctx["pref"]
        parsed, requested, resources, task, override = (ctx["parsed"], ctx["requested"], ctx["resources"], ctx["task"],
                                                        ctx["override"])
        chk_decision = ctx.get("decision")
        if errors:
            return self._hold(r, aid, "held_technical", [f"{len(errors)} HL7-Validierungsfehler: " + "; ".join(errors[:3])], who,
                              detail={"errors": errors[:50], "validation": vrep})
        # prove BEFORE posting: nothing is filed unless every value round-trips from the FHIR back to the source
        # (on a clinician override the patient-line-vs-Patient field was knowingly accepted; all values still diffed)
        correction = ctx.get("correction")
        unchanged = correction["unchanged"] if correction else None
        proof = self.fm.prove(parsed, resources, requested if override is None else None, unchanged=unchanged)
        if not proof["ok"] and get(parsed, "kind") in (None, "unknown"):
            # a generic document cannot be proven value by value yet (fhirmap.prove): stay HELD, clearly, never unproven
            return self._hold(r, aid, "held_review", ["Ablegen nicht möglich: Dokumenttyp nicht erkannt, der Inhalt lässt sich "
                                                      "nicht 1:1 belegen. Die Antwort bleibt angehalten – bitte beim Absender "
                                                      "ein Dokument im erwarteten Format anfordern oder verwerfen."], who,
                              detail={"checks": ["BUILD_NOT_POSSIBLE"], "proof": proof})
        if not proof["ok"]:
            return self._hold(r, aid, "held_technical", ["Nicht alle Werte 1:1 im Dokument belegt: "
                                                         + "; ".join(proof.get("missing") or [])[:300]], who, detail={"proof": proof})
        bundle = self.fm.transaction(resources, task, self.s.hapi_url)
        # duplicate-proof conditional creates: reuse existing Practitioner/Organization/PractitionerRole (oldest on
        # duplicates), rewrite references, plain create otherwise -> a filing never fails on HAPI-0958
        bundle, cond = fhirmap.resolve_conditionals(bundle, self.hapi.search)
        if correction:
            for e in bundle["entry"]:
                vid = correction["versions"].get(e["request"]["url"]) if e["request"]["method"] == "PUT" else None
                if vid:
                    e["request"]["ifMatch"] = f'W/"{vid}"'

        log.info("conditional creates for %s: %s", r["ref_code"],
                 [(c["resourceType"], c["action"], c.get("matches")) for c in cond])  # types/counts only, no PII
        # retried only when HAPI was not reached at all (connect errors): a retry can never duplicate a filing
        written = _retry(lambda: self.hapi.transaction_versioned(bundle["entry"]),
                         safe_on=(httpx.ConnectError, httpx.ConnectTimeout))
        refs = [ref for ref, _version in written]
        # the version of every written clinical resource and the Provenance: re-verification reads these versions
        versions = {ref: vid for ref, vid in written if vid and ref.split("/")[0] in (*CLINICAL_TYPES, "Provenance")}
        # The signed statement binds the targets as BUILT (urn:uuid). id_map = what each urn became: reused
        # resources (resolution report) + created ones (transaction response).
        id_map = {c["fullUrl"]: c["reference"] for c in cond if c["action"] == "reused"}
        id_map.update({e["fullUrl"]: ref for e, ref in zip(bundle["entry"], refs) if e["fullUrl"].startswith("urn:uuid:")})
        sig = self._verify_filed_signature(refs, id_map, versions)
        main_filed = next((x for x in refs if x.startswith(("DiagnosticReport/", "DocumentReference/"))), None)
        filed = {"resources": refs, "main": main_filed, "main_url": f"{self.s.hapi_url}/{main_filed}" if main_filed else None,
                 "answer_id": aid, "validation_errors": 0, "validation": vrep, "proof": proof, "override": override,
                 "conditional_creates": [{"type": c["resourceType"], "action": c["action"]} for c in cond],
                 "signature": sig,
                 # what a later verifier needs without reconstruction: the signed targets (urn:uuid, as built) and
                 # what each became on the server (verify_provenance / maintenance re-verify with these)
                 "signed_targets": ctx.get("signed_targets"), "id_map": id_map, "versions": versions}
        if correction:
            filed["correction"] = {"replaces": correction["prior"], "changed": correction.get("changed"),
                                   "added": correction.get("added"), "not_in_correction": correction["not_in_correction"]}
        self.store.update_request(rid, status="filed", task_status="completed", task_id=task["id"], filed=filed, held_kind=None,
                                  token_hash="filed-" + secrets.token_hex(16), last_reason=None, request_text=None)
        self.store.exec("UPDATE answers SET outcome='filed', content=NULL, purge_after=NULL WHERE id=?", (aid,))
        self._void_superseded_holds(r)
        first_answer = self.remember_identity(r, a["sender_bpk"])
        if r["channel"] == "email" and not r["warnings"]:  # never learn a domain that was sent despite a warning
            self.store.exec("INSERT OR IGNORE INTO known_domains (domain, org, first_seen) VALUES (?,?,?)",
                            (r["address"].split("@")[-1].lower(), r["recipient_org"], iso(now())))
        self.audit(rid, "answer filed", True, f"Befundlink (Absender {who})",
                   f"Antwort auf {r['ref_code']} für {pref} abgelegt: {len(refs)} Ressourcen in einer Transaktion "
                   f"({', '.join(refs)}); HL7-Validator 0 Fehler; prove {proof.get('matched')}/{proof.get('total')}."
                   + (f" TROTZ Prüfhinweis abgelegt auf Entscheidung von {override['by']}: {override['reason']}" if override else ""),
                   detail={k: v for k, v in filed.items() if k != "proof"} | {"proof_ok": proof["ok"]}, patient_ref=pref)
        text, html, data = self._summary(r, requested, parsed, resources, filed, a)
        facts = self._facts({"parsed": parsed, "decision": chk_decision}, resources)
        if facts:
            obs = [{**o, "value_text": o.get("value")} for o in facts.get("observations") or []] or data["observations"]
            as_text = set(data.get("text_only_rows") or [])
            for o in obs:  # a value filed as text is shown as such, not as a matched number
                if o.get("test") in as_text:
                    o["as_text"] = TEXT_ONLY_NOTE
            data.update({k: v for k, v in facts.items() if k not in ("patient", "kind", "observations", "einsender")})
            data["observations"] = obs
            if facts.get("einsender") and not loose_name_equal(str(facts["einsender"]).split(",")[0],
                                                               self.registers.member_name(r["requester_mxid"]) or ""):
                data["einsender"] = facts["einsender"]
            data["conclusion"] = facts.get("comment") or data.get("conclusion")
        data["validation_mode"] = (vrep or {}).get("mode")
        if filed.get("correction"):
            data["correction"] = filed["correction"]
            if filed["correction"]["not_in_correction"]:
                text += ("\nNicht in der Korrektur enthalten (unverändert belassen): "
                         + ", ".join(filed["correction"]["not_in_correction"]))
        note = charset_note(parsed)
        if note:
            data["charset_note"] = note
            text += f"\n{note}"
        if get(parsed, "transcription"):
            data["photo_note"] = PHOTO_NOTE
            text += f"\n{PHOTO_NOTE}"
        data["first_answer_from_person"] = first_answer
        if first_answer:
            text += f"\nErste Antwort dieser Person (bisher kein Befund von ihr für {r['recipient_name']})."
        self.notify(r["room_id"], text, html, kind="filed", data=data, request_id=rid)
        if (vrep or {}).get("revalidate_with_tx"):
            self._schedule_revalidation(rid)
        return {"status": "filed", **filed}

    # ---------------------------------------------------------------- validation without infrastructure holds
    def _validate(self, resources: list[dict]) -> tuple[list[str], dict]:
        """Warm validator server (tx) when available; else fhirmap.validate_report(mode='auto'): tx when
        tx.fhir.org is reachable, otherwise offline. A correct answer is never held because the terminology
        server is down: 0 errors -> file, mode/fallback/terminology_checked recorded, tx re-validation scheduled."""
        if self.validator is not None and self.s.validate_mode != "offline":
            got = self.validator.validate(resources)
            if got is not None:
                errors, rep = got
                rep["revalidate_with_tx"] = False
                return errors, rep
        mode = "offline" if self.s.validate_mode == "offline" else "auto"
        errors, rep = self._run_validator(resources, mode)
        rep["revalidate_with_tx"] = not rep.get("terminology_checked", rep.get("mode") == "tx")
        return errors, rep

    def _validate_batch(self, ctxs: list[dict]) -> list[tuple[list[str], dict]]:
        """One validation pass for several filings. Warm server: per filing (0.1-0.5 s each). CLI fallback with
        more than one filing: ONE JVM over all files, errors split back by file-name prefix."""
        if not ctxs:
            return []
        sets = [c["resources"] + [c["task"]] for c in ctxs]
        server_ok = self.validator is not None and self.validator.ready.is_set() and self.s.validate_mode != "offline"
        if len(ctxs) == 1 or server_ok:
            return [self._validate(rs) for rs in sets]
        names, allr = [], []
        for k, rs in enumerate(sets):
            for i, res in enumerate(rs):
                names.append(f"a{k:03d}-{i:02d}-{res['resourceType']}")
                allr.append(res)
        mode = "offline" if self.s.validate_mode == "offline" else "auto"
        rep = fhirmap.validate_report(allr, mode, names=names)
        base = {"mode": rep.get("mode"), "requested_mode": rep.get("requested_mode"), "fallback": rep.get("fallback"),
                "terminology_checked": rep.get("terminology_checked"), "seconds": rep.get("seconds"),
                "batch": len(ctxs), "engine": "validator_cli (one JVM for the batch)"}
        base["revalidate_with_tx"] = not base.get("terminology_checked", base.get("mode") == "tx")
        out = []
        for k in range(len(ctxs)):
            errs = [e for e in rep.get("errors") or [] if e.startswith(f"a{k:03d}-")]
            out.append((errs, {**base, "errors": len(errs)}))
        return out

    def _run_validator(self, resources: list[dict], mode: str) -> tuple[list[str], dict]:
        prev = self.fm.validate_mode
        self.fm.validate_mode = mode
        try:
            errors = self.fm.validate(resources)
        finally:
            self.fm.validate_mode = prev
        return errors, dict(getattr(self.fm, "last_report", None) or {"mode": mode})

    def _schedule_revalidation(self, rid: str, attempts: int = 24, every_s: float = 300) -> None:
        """Re-validate the filed resources with tx once tx.fhir.org is reachable (background thread)."""
        def run():
            for _ in range(attempts):
                if fhirmap_validate.tx_reachable():
                    try:
                        self.revalidate(rid)
                        return
                    except Exception as e:
                        log.warning("re-validation of %s failed: %s", rid, e)
                time.sleep(every_s)
        threading.Thread(target=run, name=f"revalidate-{rid}", daemon=True).start()

    def revalidate(self, rid: str) -> dict:
        """tx validation of what is on HAPI for this request; result stored and, if errors, told to Dr. Hofer."""
        r = self.store.get_request(rid)
        refs = (r["filed"] or {}).get("resources") or []
        res = [self.http.get(f"{self.s.hapi_url}/{x}", headers={"Accept": "application/fhir+json"}).json() for x in refs]
        errors, rep = self._run_validator(res, "tx")
        filed = {**(r["filed"] or {}), "revalidation": {**rep, "at": iso(now()), "errors_list": errors[:20]}}
        self.store.update_request(rid, filed=filed)
        if errors:
            self.notify(r["room_id"], f"[NACHVALIDIERUNG] {r['ref_code']}: HL7-Validator (Terminologie) meldet "
                                          f"{len(errors)} Fehler im abgelegten Befund: " + "; ".join(errors[:3]), request_id=rid)
        return filed["revalidation"]

    def prewarm_tx_cache(self) -> None:
        """fhirmap.prewarm_tx_cache() once, in a background thread (about a minute; startup is not blocked)."""
        def run():
            try:
                res = fhirmap.prewarm_tx_cache()
                log.info("tx prewarm: %s", {k: res.get(k) for k in ("mode", "codes", "ucum", "errors", "seconds")})
            except Exception as e:
                log.warning("tx prewarm failed: %s", e)
        threading.Thread(target=run, name="tx-prewarm", daemon=True).start()

    @staticmethod
    def recipient_is_patient(r: dict, patient: dict | None) -> bool:
        """The request went to the patient herself (same rule as the PII guard in _send)."""
        if not patient:
            return False
        return fhirmap.compare_names(r["recipient_name"], fhir_patient_name(patient)) == "same"

    def _summary(self, r, patient, parsed, resources, filed, a) -> tuple[str, str, dict]:
        """Matrix 'filed' card. No computed H/L flags: values with the lab's own reference range, and the
        lab's own comment verbatim."""
        pname = fhir_patient_name(patient)
        proof = filed["proof"]
        vm, vt = proof.get("matched"), proof.get("total")
        pm = f"{vm}/{vt}" if vt is not None else "alle"
        reg = r["register"] or {}
        if reg.get("ok") and r["doc_kind"] == "lab_result":
            reg_txt = f" · Ärzteliste: {reg['entry']['specialty']}"
        elif reg.get("ok"):
            reg_txt = f" · GDA-Index: {reg['entry']['org']['name']}, {reg['entry']['staff']['role']}"
        else:
            reg_txt = " · Rolle vom Anfordernden angegeben (nicht im Register)"
        sender_line = f"{a['sender_name']} · ID Austria (Simulation){reg_txt}"
        if self.recipient_is_patient(r, patient):  # no role to check: she answers for herself
            herself = "Patientin" if (patient or {}).get("gender") == "female" else "Patient"
            sender_line = f"{a['sender_name']} ({herself} selbst, ID Austria – Simulation)"
        obs_data, lines = [], []
        rows = list(get(parsed, "lab") or [])
        if rows and r["doc_kind"] == "lab_result":
            # the DOCUMENT's own strings (prove() guarantees FHIR == document): value as written incl. comparator,
            # the lab's OWN flag (never computed), its reference range text
            for row in rows:
                obs_data.append({"test": row.get("test") or row.get("loinc") or "?", "value": row.get("value_text"),
                                 "value_text": row.get("value_text"),
                                 "comparator": row.get("comparator"), "unit": row.get("unit"), "ref": row.get("ref_text"),
                                 "flag": row.get("flag"), "loinc": row.get("loinc"),
                                 **({"as_text": TEXT_ONLY_NOTE} if row.get("text_only") else {})})
        else:
            for o in (x for x in resources if x.get("resourceType") == "Observation"):
                vq = o.get("valueQuantity") or {}
                rr = (o.get("referenceRange") or [{}])[0]
                obs_data.append({"test": (o.get("code") or {}).get("text") or "?", "value": vq.get("value", o.get("valueString")),
                                 "value_text": str(vq.get("value", o.get("valueString"))),
                                 "comparator": vq.get("comparator"), "unit": vq.get("unit"), "ref": rr.get("text"), "flag": None})
        what = "Werte" if r["doc_kind"] == "lab_result" else "Prüfpunkte"
        as_text = [row.get("test") for row in rows if row.get("text_only")]
        if r["doc_kind"] == "lab_result" and as_text:
            # no 'Werte übernommen' claim for values filed as text: say what was done instead
            lines.append(f"{DOC_KIND_LABEL[r['doc_kind']]} für {pname} von {r['recipient_name']} (ID Austria – Simulation geprüft) abgelegt: "
                         f"{len(obs_data)} Werte, davon {len(as_text)} {TEXT_ONLY_NOTE} · HL7-Validator: 0 Fehler")
        elif r["doc_kind"] == "lab_result":
            lines.append(f"{DOC_KIND_LABEL[r['doc_kind']]} für {pname} von {r['recipient_name']} (ID Austria – Simulation geprüft) abgelegt: "
                         f"{len(obs_data)} Werte · HL7-Validator: 0 Fehler · {pm} Werte unverändert "
                         f"{'aus der Lesung ' if get(parsed, 'transcription') else ''}übernommen · Inhalt 1:1 belegt")
        else:
            lines.append(f"{DOC_KIND_LABEL[r['doc_kind']]} für {pname} von {r['recipient_name']} (ID Austria – Simulation geprüft) abgelegt: "
                         f"DocumentReference mit {'Original (Foto/PDF)' if get(parsed, 'transcription') else 'Originaltext'} · HL7-Validator: 0 Fehler · {pm} Prüfpunkte unverändert übernommen · Inhalt 1:1 belegt")
        for o in obs_data:
            if o.get("as_text"):
                lines.append(f"• {o['value']} – {o['as_text']}")
                continue
            val = o["value"] if o["value"] is not None else "?"
            lines.append(f"• {o['test']}: {val} {o['unit'] or ''}".rstrip() + (f" {o['flag']}" if o.get("flag") else "")
                         + (f" (Ref. {o['ref']})" if o["ref"] else ""))
        extra = []
        dr = next((x for x in resources if x.get("resourceType") == "DiagnosticReport"), {})
        concl = dr.get("conclusion") or get(parsed, "comment")
        if concl:
            extra.append(f"Kommentar des Labors: {concl}")
        author = get(get(parsed, "author") or {}, "name")
        if author and not loose_name_equal(author, a["sender_name"]):  # display only
            areg = self.registers.find_physician(author)
            extra.append(f"Autor laut Dokument: {author}" + (f" · Ärzteliste: {areg['specialty']}" if areg else ""))
        if filed.get("override"):
            by_name = self.registers.member_name(filed["override"]["by"]) or filed["override"]["by"]
            extra.append(f"Abgelegt trotz Prüfhinweis, Entscheidung {by_name}: {filed['override']['reason']}")
        if get(parsed, "sample_time"):
            lines.append(f"Probe: {get(parsed, 'sample_time')}")
        lines += extra
        lines.append(f"Absender: {sender_line}")
        lines.append(f"{filed['main_url'] or ''} (Ref {r['ref_code']})")
        text = "\n".join(lines)
        html = "<br>".join(_esc(l) for l in lines[:-1]) + (
            f"<br><a href=\"{_esc(filed['main_url'] or '')}\">{_esc(filed['main'] or '')}</a> (Ref {r['ref_code']})")
        req_name = self.registers.member_name(r["requester_mxid"])
        einsender = get(parsed, "requester")
        if einsender and req_name and loose_name_equal(get(einsender, "name") or "", req_name):
            einsender = None  # only reported when it differs from the requester (info, not a decision)
        data = {"doc_kind": r["doc_kind"], "patient": self._patient_card(patient), "patient_name": pname,
                "requester": self._requester_card(r),  # the card never tells Dr. Hofer that she ordered it herself
                "request_id": r["id"], "ref_code": r["ref_code"], "conclusion": concl or None,
                "status_note": None if (dr.get("status") or "final") == "final" else f"Status laut Labor: {dr.get('status')}",
                "observations": obs_data, "sample_time": get(parsed, "sample_time"), "report_time": get(parsed, "report_time"),
                "sender": sender_line, "received_at": a["received_at"], "validator_errors": 0,
                # values filed as text are not 'matched values': no such claim on the card then
                "values_matched": None if as_text else vm, "values_total": None if as_text else vt, "values_label": what,
                **({"text_only_rows": as_text, "text_only_note": TEXT_ONLY_NOTE} if as_text else {}),
                "summary": " · ".join(extra) or None, "fhir_url": filed["main_url"],
                # no 'original_url': the original upload is not a separate resource, it is attached INSIDE the
                # report (DiagnosticReport.presentedForm / DocumentReference.content), reachable via fhir_url
                "report_status": dr.get("status") or get(parsed, "report_status"),
                "einsender": einsender,                          # info only (who ordered the test), not a decision
                "warnings": [{"code": f.get("code"), "message": f.get("message")} for f in get(parsed, "findings") or []
                             if f.get("outcome") in ("WARN", "INFO")],
                "resources": [{"type": x.split("/")[0], "id": x.split("/")[1], "url": f"{self.s.hapi_url}/{x}"}
                              for x in filed["resources"] if x.startswith(("DiagnosticReport/", "DocumentReference/", "Provenance/", "Task/"))]}
        return text, html, data

    def _hold(self, r: dict, aid: int, status: str, reasons: list[str], who: str, detail: dict | None = None,
              mismatches: list[dict] | None = None) -> dict:
        r = self.store.get_request(r["id"])
        self._set_status(r["id"], status, " ".join(reasons)[:500], last_reason=" ".join(reasons), held_kind=status)
        self.store.exec("UPDATE answers SET outcome=?, reasons=? WHERE id=?", (status, json.dumps(reasons, ensure_ascii=False), aid))
        self.audit(r["id"], "filing held", False, who,
                   f"Antwort auf {r['ref_code']} NICHT abgelegt, angehalten ({status}): " + " ".join(reasons),
                   detail={"status": status, "reasons": reasons, **(detail or {})})
        if status == "held_review":
            opts = "halten / verwerfen / trotzdem ablegen (mit Begründung)"
        else:
            opts = "halten / verwerfen / erneut versuchen ('ablegen' mit Begründung)"
        a = self.store.one("SELECT sender_name FROM answers WHERE id=?", (aid,)) or {}
        self.notify(r["room_id"], kind="hold", answer_id=str(aid), request_id=r["id"],
                        text=f"[ANGEHALTEN – Entscheidung nötig] Antwort von {r['recipient_name']} auf Anforderung {r['ref_code']} "
                             f"wurde NICHT abgelegt. Grund: {' '.join(reasons)} Entscheidung: {opts}. Ref {r['ref_code']}",
                        data={"reason": f"{r['ref_code']}: " + "; ".join(reasons), "reasons_de": reasons,
                              "reason_text_de": "; ".join(reasons), "ref_code": r["ref_code"],
                              "patient": self._patient_card(self.patient(r["patient_id"])),
                              "matched": self._matched(codes=(detail or {}).get("checks") or [], mismatches=mismatches or []),
                              "codes": (detail or {}).get("checks") or [], "mismatches": mismatches or [],
                              # the hold card says when the file ALSO contains another person - never who
                              "details": {"other_person_present": bool((detail or {}).get("other_person_present"))
                                          or bool(set((detail or {}).get("checks") or [])
                                                  & {"OTHER_PATIENT_PRESENT"})
                                          or any(m.get("other_person_present") for m in mismatches or [])},
                              # card facts (fhirmap.card_facts): values as written, sample time + age, status,
                              # Einsender, lab comment, warnings in German; the patient stays the REQUESTED one
                              **{k: v for k, v in ((detail or {}).get("facts") or {}).items()
                                 if k not in ("patient", "kind")},
                              "sender": f"{a.get('sender_name') or '?'} (ID Austria – Simulation)", "doc_kind": r["doc_kind"],
                              "requester": self._requester_card(r),
                              "charset_note": (detail or {}).get("charset_note"),
                              **((detail or {}).get("card") or {}),
                              "request_id": r["id"], "answer_id": str(aid), "status": status,
                              "decision_options": opts})
        return {"status": status, "reasons": reasons}

    # ================================================================== decisions (Dr. Hofer, via bot)
    def request_of_answer(self, answer_key: str) -> str:
        if str(answer_key).isdigit():
            a = self.store.one("SELECT request_id FROM answers WHERE id=?", (int(answer_key),))
            if not a:
                raise ApiError(404, "unknown answer")
            return a["request_id"]
        return self.resolve(answer_key)["id"]

    PREVIEW_LINES = 40

    def held_preview(self, rid: str, by: str) -> dict:
        """The HELD document's text for Dr. Hofer (decrypted, first PREVIEW_LINES lines). Only an answer that is
        held (HOLD) and still stored; a blocked upload is never stored, so there is nothing to show. Audited."""
        r = self.store.get_request(rid)
        a = self.store.one("SELECT * FROM answers WHERE request_id=? AND outcome IN ('held_review','held_technical') "
                           "ORDER BY id DESC LIMIT 1", (rid,))
        if r["status"] not in ("held_review", "held_technical") or not a or a["content"] is None:
            was_held = self.store.one("SELECT id FROM answers WHERE request_id=? AND outcome='superseded' "
                                      "AND reasons IS NOT NULL LIMIT 1", (rid,))
            if r["status"] == "filed" and was_held:
                raise ApiError(409, "Keine offene Entscheidung: eine spätere Antwort wurde abgelegt.")
            raise ApiError(409, "Kein angehaltenes Dokument vorhanden (abgewiesene Dokumente werden nie gespeichert).")
        lines = (self._load_text(a) or "").splitlines()
        shown = lines[:self.PREVIEW_LINES]
        self.audit(rid, "held document viewed", True, by,
                   f"Angehaltenes Dokument zu {r['ref_code']} angezeigt ({len(shown)} von {len(lines)} Zeilen).",
                   detail={"answer_id": a["id"], "sha256": a["sha256"], "lines_shown": len(shown)})
        return {"request_id": rid, "ref_code": r["ref_code"], "answer_id": a["id"], "status": r["status"],
                "sender": f"{a['sender_name']} (ID Austria – Simulation)", "received_at": a["received_at"],
                # the upload's filename is never stored (it often carries the patient's name)
                "filename": None, "reasons": a["reasons"] or [], "text": "\n".join(shown), "lines_total": len(lines),
                "truncated": len(lines) > len(shown)}

    def unlock(self, rid: str, by: str) -> dict:
        r = self.resolve(rid)
        if r["status"] != "locked":
            raise ApiError(409, f"request is '{r['status']}', not locked")
        self.store.update_request(rid, eid_failures=0, eid_attempts=0)
        self._set_status(rid, "sent", "Link von der Anfordernden wieder freigegeben")
        self.audit(rid, "decision", True, by, f"Link zu {r['ref_code']} nach Sperre wieder freigegeben.")
        return self.request_view(rid)

    def decide_answer(self, answer_key: str, action: str, reason: str | None, by: str | None):
        """POST /api/answers/{id}/decision {action: file|reject|hold}. Accepts an answer id or a request id/ref."""
        a = self.store.one("SELECT * FROM answers WHERE id=?", (int(answer_key),)) if str(answer_key).isdigit() else None
        if a is None:
            return self.decide(answer_key, action, reason, by)
        latest = self.store.one("SELECT id FROM answers WHERE request_id=? AND outcome IN ('held_review','held_technical') "
                                "ORDER BY id DESC LIMIT 1", (a["request_id"],))
        if not latest or latest["id"] != a["id"]:
            raise ApiError(409, f"answer {answer_key} is not the open held answer of its request")
        return self.decide(a["request_id"], action, reason, by)

    def _decide_identity(self, r: dict, d: str, reason: str | None, by: str) -> dict:
        """Dr. Hofer's answer to an identity_confirm card. confirm: the new subject may now sign in (and is then bound
        as usual); reject: treated as a blocked foreign access and remembered for this request. Either way nothing had
        been shown, and the request itself stayed open for the known person throughout."""
        if not r["identity_pending_sub"]:
            raise ApiError(409, f"request is '{r['status']}', no identity to confirm")
        name = r["identity_pending_name"]
        if r["status"] == "identity_review":  # requests held this way before the review applied to the identity only
            self._set_status(r["id"], "opened", "Identitätsprüfung entschieden")
        if d == "confirm_identity":
            self.store.update_request(r["id"], identity_approved_sub=r["identity_pending_sub"], identity_pending_sub=None,
                                      identity_pending_year=None)
            self.audit(r["id"], "decision", True, by, f"Neue ID-Austria-Identität für {r['recipient_name']} zu {r['ref_code']} "
                                                      f"bestätigt. {reason or ''}".strip())
            self.notify(r["room_id"], f"{r['ref_code']}: {r['recipient_name']} kann sich jetzt mit der neuen Identität "
                                      f"anmelden und antworten.", request_id=r["id"])
        else:
            self.store.update_request(r["id"], identity_rejected_sub=r["identity_pending_sub"], identity_pending_sub=None,
                                      identity_pending_year=None, eid_failures=r["eid_failures"] + 1,
                                      misdeliveries=r["misdeliveries"] + 1)
            self.audit(r["id"], "misdelivery caught", True, f"{name} (ID Austria – Simulation)",
                       f"Zugriff auf {r['ref_code']} durch eine andere Person gleichen Namens von {by} abgelehnt; "
                       f"nichts angezeigt, nichts abgelegt. Link bleibt für die bekannte Person gültig.",
                       pseudonymous=True, redact=[name] + name.split())
            self.notify(r["room_id"], f"{r['ref_code']}: Der Link bleibt für {r['recipient_name']} gültig.",
                        request_id=r["id"])
        return self.request_view(r["id"])

    def _discard_answer(self, r: dict, a: dict | None, reason: str | None, by: str) -> dict:
        """'verwerfen' discards the held ANSWER, not the request: its content is deleted, the request goes back to
        'identity_verified' (Task in-progress), and the link stays valid for the named person until it expires, so
        she can still send the right document."""
        if a:
            self.store.exec("UPDATE answers SET outcome='discarded', content=NULL, purge_after=NULL WHERE id=?", (a["id"],))
        self._purge_answers(r["id"])
        self._set_status(r["id"], "identity_verified", f"Antwort verworfen von {by}: {reason or '-'}", held_kind=None,
                         last_reason=f"Antwort verworfen: {reason or '-'}")
        valid = _de_date(r["expires_at"][:10])
        by_name = self.registers.member_name(by) or by
        self.audit(r["id"], "decision", True, by,
                   f"Entscheidung zu {r['ref_code']}: Antwort verworfen, Inhalt gelöscht. Die Anforderung bleibt offen, "
                   f"der Link bleibt für {r['recipient_name']} gültig bis {valid}. {reason or ''}".strip(),
                   detail={"discarded_answer": a["id"] if a else None, "held_reasons": a["reasons"] if a else None})
        self.notify(r["room_id"], f"Anforderung {r['ref_code']}: Antwort verworfen und gelöscht (Entscheidung {by_name}). "
                                  f"{r['recipient_name']} kann bis {valid} ein anderes Dokument senden.", request_id=r["id"])
        return self.request_view(r["id"])

    def decide(self, key: str, decision: str, reason: str | None, by: str | None) -> tuple[dict, int | None, dict | None]:
        """Returns (view, answer_id_to_file_or_None, override). Filing runs in the background."""
        r = self.resolve(key)
        d = {"halten": "hold", "hold": "hold", "verwerfen": "discard", "discard": "discard", "reject": "discard",
             "ablegen": "file", "file": "file", "trotzdem ablegen": "file",
             "confirm_identity": "confirm_identity", "freigeben": "confirm_identity",
             "reject_identity": "reject_identity", "ablehnen": "reject_identity"}.get((decision or "").strip().lower())
        if not d:
            raise ApiError(422, "decision must be halten|verwerfen|ablegen|confirm_identity|reject_identity")
        if d in ("confirm_identity", "reject_identity"):
            return self._decide_identity(r, d, reason, by or r["requester_mxid"]), None, None
        if r["status"] not in ("held_review", "held_technical"):
            raise ApiError(409, f"request is '{r['status']}', nothing to decide")
        by = by or r["requester_mxid"]
        a = self.store.one("SELECT * FROM answers WHERE request_id=? AND outcome IN ('held_review','held_technical') "
                           "ORDER BY id DESC LIMIT 1", (r["id"],))
        if d == "hold":
            self.audit(r["id"], "decision", True, by, f"Entscheidung zu {r['ref_code']}: weiter halten. {reason or ''}".strip())
            return self.request_view(r["id"]), None, None
        if d == "discard":
            return self._discard_answer(r, a, reason, by), None, None
        if not reason or len(reason.strip()) < 3:
            raise ApiError(422, "'ablegen' requires a reason (Begründung)")
        if not a or a["content"] is None:
            raise ApiError(409, "held content no longer available")
        self.audit(r["id"], "decision", True, by, f"Entscheidung zu {r['ref_code']}: trotzdem ablegen. Begründung: {reason}",
                   detail={"held_reasons": a["reasons"]})
        self.store.update_request(r["id"], status="processing")
        override = {"by": by, "reason": reason.strip(), "at": iso(now()), "held_reasons": a["reasons"]} \
            if r["status"] == "held_review" else None
        return self.request_view(r["id"]), a["id"], override


def _observation_label(obs: dict) -> str:
    code = obs.get("code") or {}
    if code.get("text"):
        return code["text"]
    coding = (code.get("coding") or [{}])[0]
    return coding.get("display") or coding.get("code") or "Wert"


def _observation_value(obs: dict) -> str:
    """'1.0', '< 0.5 mmol/L', a text or coded value; 'kein Wert' without one."""
    q = obs.get("valueQuantity")
    if q:
        parts = [q.get("comparator") or "", str(q.get("value")), q.get("unit") or ""]
        return " ".join(x for x in parts if x).strip()
    if obs.get("valueString") is not None:
        return str(obs["valueString"])
    if obs.get("valueCodeableConcept"):
        cc = obs["valueCodeableConcept"]
        return cc.get("text") or ((cc.get("coding") or [{}])[0].get("display")) or "codiert"
    return "kein Wert"


def describe_change(signed: dict, current: dict) -> str | None:
    """What a clinician needs to know about one changed resource (signed version vs now)."""
    rtype = current.get("resourceType")
    if rtype == "Observation":
        label = _observation_label(signed)
        before, after = _observation_value(signed), _observation_value(current)
        parts = []
        if before != after:
            parts.append(f"{label}: signiert {before}, am FHIR-Server jetzt {after}")
        if signed.get("status") != current.get("status"):
            parts.append(f"{label}: Status signiert {signed.get('status')}, jetzt {current.get('status')}")
        if not parts and signed.get("interpretation") != current.get("interpretation"):
            parts.append(f"{label}: Kennzeichnung (H/L) geändert")
        return "; ".join(parts) or f"{label}: Eintrag geändert"
    name = "Befund" if rtype == "DiagnosticReport" else "Dokument"
    parts = []
    if signed.get("status") != current.get("status"):
        parts.append(f"Status signiert {signed.get('status')}, jetzt {current.get('status')}")
    if signed.get("conclusion") != current.get("conclusion"):
        parts.append("Beurteilung/Kommentar geändert")
    if (signed.get("presentedForm") != current.get("presentedForm")) or (signed.get("content") != current.get("content")):
        parts.append("Anhang (Originaldokument) geändert")
    if signed.get("result") != current.get("result"):
        parts.append("Werteliste geändert")
    if not parts:
        return f"{name}: Inhalt geändert"
    return f"{name}: " + ", ".join(parts)


def _versioned_id_map(id_map: dict, provenance: dict) -> dict:
    """Accept versioned Provenance targets ('Observation/12/_history/2'): map each signed reference to the target
    as the Provenance writes it."""
    targets = [t.get("reference") or "" for t in provenance.get("target") or []]
    out = {}
    for signed, server in (id_map or {}).items():
        versioned = next((t for t in targets if t.startswith(f"{server}/_history/")), None)
        out[signed] = versioned or server
    for t in targets:  # statements that sign server references directly (corrections)
        if "/_history/" in t:
            out.setdefault(t.split("/_history/")[0], t)
    return out


# hold reasons that mean "this value's unit (or the table around it) could not be read": on an override such a value is
# filed as TEXT (valueString = the row as written), never as a quantity with a made-up unit
TEXT_ONLY_ROW_CODES = {"UNKNOWN_UNIT", "UNIT_MISSING", "LOINC_UNIT_DIMENSION"}
TEXT_ONLY_TABLE_CODES = {"TABLE_SPLIT", "TABLE_MALFORMED"}
TEXT_ONLY_NOTE = "als Text abgelegt (Einheit nicht zuordenbar)"


def _row_as_written(parsed, row: dict) -> str:
    """The document's own line for this row (stripped), or a plain rendering when it cannot be found."""
    for line in (get(parsed, "text") or "").splitlines():
        if row.get("test") and row["test"] in line and (row.get("value_text") or "") in line:
            return line.strip()
    return " ".join(str(x) for x in (row.get("test"), row.get("value_text"), row.get("unit")) if x)


def text_only_rows(parsed, decision: dict | None):
    """A copy of `parsed` in which every row affected by a unit/table hold reason carries its line as written as a
    TEXT value: no quantity, no unit, no UCUM code, no reference range, no flag. Its LOINC code is kept only if it is
    a known LOINC code. Rows are marked text_only (the card says so). Unaffected rows and other documents: as they
    are."""
    rows = list(get(parsed, "lab") or [])
    decision = decision or {}
    codes = set(decision.get("codes") or [])
    if not rows or not codes & (TEXT_ONLY_ROW_CODES | TEXT_ONLY_TABLE_CODES):
        return parsed
    if codes & TEXT_ONLY_TABLE_CODES:
        affected = {row.get("test") for row in rows}
    else:
        affected = {d.get("row") for d in decision.get("details") or [] if d.get("code") in TEXT_ONLY_ROW_CODES}
    new_rows = []
    for row in rows:
        if row.get("test") not in affected:
            new_rows.append(row)
            continue
        written = _row_as_written(parsed, row)
        loinc = row.get("loinc") if row.get("loinc") and fhirmap.loinc.info(row["loinc"], online=False) else None
        new_rows.append({**row, "value": written, "value_text": written, "comparator": None, "unit": None,
                         "ucum": None, "ref_low": None, "ref_high": None, "ref_text": None, "flag": None,
                         "flag_text": None, "interpretation": None, "loinc": loinc, "text_only": True})
    out = copy.copy(parsed)
    out.lab = new_rows
    return out


def decision_card_fields(decision: dict | None) -> dict:
    """Hold-card fields from fhirmap's decision details: named_sender (the sender the document names, when it is
    not the eID person), first_sentence (an unrecognised document; never when another person is in the file) and
    replaces (a correction: what it says it replaces)."""
    decision = decision or {}
    out = {}
    for d in decision.get("details") or []:
        if d.get("code") in ("SENDER_CONTRADICTS_EID", "SENDER_CONFLICT") and d.get("document"):
            out["named_sender"] = d["document"]
        if d.get("code") in ("KIND_UNKNOWN", "KIND_MISMATCH") and d.get("document") and not decision.get("other_person_present"):
            out["first_sentence"] = d["document"]
    rep = decision.get("replaces") or {}
    replaces = {}
    if rep.get("text"):
        replaces["text"] = rep["text"]
    if rep.get("time"):
        replaces["date"] = rep["time"]
    if replaces:  # only fields with a value (no empty 'ersetzt –' row)
        out["replaces"] = replaces
    return out


# address warnings that mean "this may not be the recipient's address" (lookalike.check_email / check_local_part)
ADDRESS_WARNING_CODES = {"confusable", "lookalike", "uppercase_in_domain", "mixed_script", "non_ascii",
                         "unknown_domain_for_org", "local_part_lookalike"}

PRIOR_NOT_FOUND_DE = "Ursprungsbefund nicht gefunden – bitte manuell prüfen."

CHARSET_LABEL = {"cp1252": "Windows-1252", "latin-1": "Latin-1"}


PHOTO_NOTE = "📷 aus Foto gelesen (KI, 2 Lesungen übereinstimmend)"


def charset_note(parsed) -> str | None:
    """'Zeichensatz umgewandelt (Windows-1252)' when the upload was not UTF-8 and was decoded by the fallback."""
    enc = get(parsed, "encoding")
    if enc in CHARSET_LABEL:
        return f"Zeichensatz umgewandelt ({CHARSET_LABEL[enc]})"
    return None


def _sentences(reasons: list[str]) -> str:
    """Reasons joined with '; ' without their own final period, so the caller's '.' never doubles ('lassen.. Nichts')."""
    return "; ".join(x.rstrip().rstrip(".") for x in reasons)


def _de_datetime(value: str) -> str:
    """ISO date-time -> '28.10.2026, 14:37' (Vienna time); a date only -> '28.10.2026'."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if "T" not in value:
        return dt.strftime("%d.%m.%Y")
    if dt.tzinfo is not None:
        dt = dt.astimezone(VIENNA)
    return dt.strftime("%d.%m.%Y, %H:%M")


def _de_date(iso_date: str) -> str:
    """'2026-10-12' -> '12.10.2026'."""
    return f"{iso_date[8:10]}.{iso_date[5:7]}.{iso_date[0:4]}" if len(iso_date) >= 10 else iso_date


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

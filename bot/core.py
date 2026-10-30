"""Conversation logic: commands, the single confirm step (register pre-flight + address check),
hold decisions, status/audit. Transport-free: `send(room_id, text, html)` and the service client
are injected, so the whole flow is unit-testable without Matrix.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import fmt
from . import recipients as rcp
from .parse import LAB, REPORT, parse_short
from .parse import (Draft, channel_for, valid_id, classify_contact, contact_problem, local_address_warnings, norm,
                    parse_command, parse_request_args, strip_reply_fallback, yes_no)
from .service import ServiceError

log = logging.getLogger("bot.core")

SendFn = Callable[[str, str, "str | None"], Awaitable[object]]

HELD = {"needs_confirmation", "confirmation_required", "confirm_required", "held"}
DRY = {"dry_run", "dry-run", "preview", "validated"}


@dataclass
class Pending:
    """A request waiting for the doctor's !ja / !nein after the pre-flight."""
    draft: Draft
    lang: str
    warnings: list = field(default_factory=list)
    register: object = None
    held_rid: str | None = None      # set when the service stored the request as needs_confirmation
    patient: object = None           # {name, birthDate} the service resolved for patient_id (dry run)
    touched: float = 0.0


T = {
    "de": {
        "usage": ("Beispiel: !anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, "
                  "eva.brandl@labor-kanal.example, Patient maria-beispiel · !hilfe zeigt alle Befehle."),
        "incomplete": "Anforderung unvollständig – nichts gesendet.",
        "missing": "Es fehlt: {f}.",
        "field": {"patient": "Patient <id>", "doc_kind": "Dokumentart (labor/reha)", "name": "Name",
                  "role": "Rolle", "org": "Organisation", "contact": "E-Mail oder österreichische Mobilnummer"},
        "retry": "Bitte !ja (senden) oder !nein (abbrechen) – oder eine korrigierte Adresse eingeben.",
        "cancelled": "Abgebrochen – nichts gesendet.",
        "nothing_open": "Keine offene Anforderung. !hilfe zeigt die Befehle.",
        "not_sent": "Nicht gesendet: {err}",
        "send_failed": "Nicht gesendet: {err}\n!ja = erneut versuchen · !nein = abbrechen",
        "unknown_cmd": "Unbekannter Befehl '!{w}'. !hilfe zeigt die Befehle.",
        "need_id": "Bitte eine Anfrage-ID angeben, z. B. !{c} <id>.",
        "derived": "Die Patienten-ID '{pid}' wurde aus einem Namen abgeleitet – bitte prüfen.",
        "sent_with_warnings": "Hinweis vom Dienst: {w}",
        "no_suggestion": "Kein Korrekturvorschlag vorhanden – bitte die korrigierte Adresse eingeben.",
        "short_usage": ("Kurzform: !{w} <Nachname> <Patienten-ID> [E-Mail/Mobilnummer] [, Text: …], "
                        "z. B. !{w} Brandl maria-beispiel. Langform: {long} <Name>, <Rolle>, <Organisation>, "
                        "<E-Mail oder Mobilnummer>, Patient <id>"),
        "short_unknown": ("'{q}' ist weder im Register noch in früheren Anfragen dieses Raums. Bitte die Langform: "
                          "{long} <Name>, <Rolle>, <Organisation>, <E-Mail oder Mobilnummer>, Patient <id>"),
        "short_ambiguous": ("'{q}' ist nicht eindeutig: {who}. Bitte Vorname oder Organisation ergänzen "
                            "oder die Langform: {long} <Name>, <Rolle>, <Organisation>, <E-Mail oder Mobilnummer>, Patient <id>"),
        "short_no_contact": ("{name} ({org}) ist bekannt, aber noch ohne Kontakt in diesem Raum. Einmal mit Adresse: "
                             "!{w} {q} {pid} <E-Mail oder Mobilnummer>"),
        "show_usage": "Bitte so: !zeigen <KB-…> (nur für angehaltene Antworten).",
        "show_not_held": "{id}: keine angehaltene Antwort in diesem Raum.",
        "ident_yes": "✓ {id}: neue Identität zugelassen.",
        "ident_no": "{id}: neue Identität abgelehnt, ihr wurde nichts angezeigt.",
        "ident_usage": "Bitte so: !identität <KB-…> ja (zulassen) oder !identität <KB-…> nein (ablehnen).",
        "unlocked": "✓ Link zu {id} wieder freigegeben: {name} kann sich erneut anmelden, Fehlversuche auf 0.",
        "unlock_failed": "Freigabe {id} nicht möglich: {err}",
        "not_member": "Nicht berechtigt: nur Mitglieder des PGK-Verzeichnisses können den Bot verwenden.",
        "bad_id": "Ungültige ID. Erlaubt: req-… (12 Hex-Zeichen), KB-XXXXXX oder eine Nummer.",
        "other_room": "Diese Anfrage gehört zu einem anderen Raum – bitte dort abfragen.",
        "internal": "Interner Fehler bei der Verarbeitung – bitte erneut versuchen.",
    },
    "en": {
        "usage": ("Example: !request lab Dr. Eva Brandl, lab physician, Labor Kanalstraße, "
                  "eva.brandl@labor-kanal.example, patient maria-beispiel · !help lists all commands."),
        "incomplete": "Request incomplete – nothing sent.",
        "missing": "Missing: {f}.",
        "field": {"patient": "patient <id>", "doc_kind": "document kind (lab/report)", "name": "name",
                  "role": "role", "org": "organisation", "contact": "email or Austrian mobile number"},
        "retry": "Please answer !yes (send) or !no (cancel) – or type a corrected address.",
        "cancelled": "Cancelled – nothing sent.",
        "nothing_open": "No open request. !help lists the commands.",
        "not_sent": "Not sent: {err}",
        "send_failed": "Not sent: {err}\n!yes = retry · !no = cancel",
        "unknown_cmd": "Unknown command '!{w}'. !help lists the commands.",
        "need_id": "Please give a request ID, e.g. !{c} <id>.",
        "derived": "The patient ID '{pid}' was derived from a name – please check.",
        "sent_with_warnings": "Service note: {w}",
        "no_suggestion": "No suggested correction – please type the corrected address.",
        "short_usage": ("Short form: !{w} <family name> <patient id> [email/mobile] [, text: …], "
                        "e.g. !{w} Brandl maria-beispiel. Long form: {long} <Name>, <Role>, <Organisation>, "
                        "<email or mobile>, patient <id>"),
        "short_unknown": ("'{q}' is neither in the register nor in earlier requests of this room. Please use the "
                          "long form: {long} <Name>, <Role>, <Organisation>, <email or mobile>, patient <id>"),
        "short_ambiguous": ("'{q}' is ambiguous: {who}. Add the given name or organisation, or use the long form: "
                            "{long} <Name>, <Role>, <Organisation>, <email or mobile>, patient <id>"),
        "short_no_contact": ("{name} ({org}) is known but has no contact in this room yet. Once with the address: "
                             "!{w} {q} {pid} <email or mobile>"),
        "show_usage": "Usage: !show <KB-…> (held answers only).",
        "show_not_held": "{id}: no held answer in this room.",
        "ident_yes": "✓ {id}: new identity confirmed.",
        "ident_no": "{id}: new identity rejected, nothing was shown to it.",
        "ident_usage": "Usage: !identity <KB-…> yes (confirm) or !identity <KB-…> no (reject).",
        "unlocked": "✓ Link for {id} unlocked: {name} can sign in again, failed attempts reset.",
        "unlock_failed": "Cannot unlock {id}: {err}",
        "not_member": "Not allowed: only members of the PGK directory can use the bot.",
        "bad_id": "Invalid ID. Allowed: req-… (12 hex chars), KB-XXXXXX or a number.",
        "other_room": "This request belongs to another room – please ask there.",
        "internal": "Internal error while processing – please try again.",
    },
}

HELP = {
    "de": """Befehle (Deutsch/Englisch):
• !labor <Nachname> <Patienten-ID> · !reha <Nachname> <Patienten-ID> – Kurzform, z. B. !labor Brandl maria-beispiel
  Empfänger aus Register und früheren Anfragen dieses Raums; beim ersten Mal Adresse anhängen:
  !labor Brandl maria-beispiel eva.brandl@labor-kanal.example · optional: …, Text: <was benötigt wird>
• !anfordern labor <Name>, <Rolle>, <Organisation>, <E-Mail oder Mobilnummer>, Patient <id>
• !anfordern reha <Name>, <Rolle>, <Organisation>, <E-Mail oder Mobilnummer>, Patient <id>
  optional: …, Text: <was benötigt wird> – sieht die Person erst nach der Anmeldung
  optional: …, Notiz: <Text> – bleibt intern
  Vor dem Senden: Register- und Adressprüfung, dann !ja / !nein oder korrigierte Adresse
• !request lab|report … – wie oben, auf Englisch
• !status <id> – Stand (ohne id: letzte Anfrage)
• !audit <id> – Audit-Protokoll
• !ablegen <id> <Begründung> · !verwerfen <id> · !halten <id> – über eine angehaltene Antwort entscheiden
• !zeigen <id> – angehaltene Antwort lesen (nur hier im Raum)
• !identität <id> ja|nein – neue ID-Austria-Identität einer bekannten Person zulassen oder ablehnen
• !freigeben <id> – nach eID-Fehlversuchen gesperrten Link wieder öffnen
Kanal: E-Mail-Adresse → E-Mail, österreichische Mobilnummer (+43 6xx …) → SMS.
Die Nachricht an die Person nennt weder Patient noch Geburtsdatum noch Dokumentart.""",
    "en": """Commands (German/English):
• !lab <family name> <patient id> · !report <family name> <patient id> – short form, e.g. !lab Brandl maria-beispiel
  recipient from the register and earlier requests of this room; add the address the first time:
  !lab Brandl maria-beispiel eva.brandl@labor-kanal.example · optional: …, text: <what you need>
• !request lab <Name>, <Role>, <Organisation>, <email or mobile>, patient <id>
• !request report <Name>, <Role>, <Organisation>, <email or mobile>, patient <id>
  optional: …, text: <what you need> – shown to the person only after sign-in
  optional: …, note: <text> – stays internal
  Before sending: register and address check, then !yes / !no or a corrected address
• !anfordern labor|reha … – same, German
• !status <id> – state (no id: last request)
• !audit <id> – audit log
• !file <id> <reason> · !reject <id> · !hold <id> – decide on a held answer
• !show <id> – read a held answer (this room only)
• !identity <id> yes|no – confirm or reject a new ID Austria identity of a known recipient
• !unlock <id> – re-open a link locked after failed eID attempts
Channel: email address → email, Austrian mobile number (+43 6xx …) → SMS.
The message to the person names no patient, no birth date and no document type.""",
}

DECIDE = {
    "de": {"file": "✓ {id}: wird trotz Abweichung abgelegt. Begründung: {reason}",
           "reject": "{id}: verworfen – nicht abgelegt.",
           "hold": "{id}: bleibt angehalten.",
           "need_id": "Bitte die ID angeben: !ablegen <id> <Begründung> · !verwerfen <id> · !halten <id>",
           "need_reason": "Ablegen trotz Abweichung braucht eine Begründung: !ablegen {id} <Begründung>",
           "failed": "Entscheidung fehlgeschlagen für"},
    "en": {"file": "✓ {id}: will be filed despite the mismatch. Reason: {reason}",
           "reject": "{id}: rejected – not filed.",
           "hold": "{id}: stays on hold.",
           "need_id": "Please give the ID: !file <id> <reason> · !reject <id> · !hold <id>",
           "need_reason": "Filing despite a mismatch needs a reason: !file {id} <reason>",
           "failed": "Decision failed for"},
}


class Core:
    def __init__(self, service, send: SendFn, session_ttl: float = 1800.0,
                 clock: Callable[[], float] = time.monotonic, members: set[str] | None = None):
        self.service = service
        self.send = send
        self.ttl = session_ttl
        self.clock = clock
        self.sessions: dict[tuple[str, str], Pending] = {}
        self.last_request: dict[str, str] = {}
        self.holds: dict[str, list[str]] = {}       # room -> open hold ids (answer or request ids)
        self.identity_holds: dict[str, list[str]] = {}
        self.recipients: dict[str, list[dict]] = {}   # room -> people asked before (clean sends only)
        self.register = rcp.load_register()
        self.sent_cards: set[str] = set()           # ids (KB-/req-) whose "Anfrage gesendet" card is posted
        self.inflight: dict[str, int] = {}          # room -> confirm sends in progress   # room -> ids waiting for !identität
        self.request_rooms: dict[str, str] = {}     # request id / ref code / answer id -> its room
        self.members = members                      # None = no directory check (unit tests)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------ entry point

    async def handle(self, room_id: str, sender: str, body: str) -> None:
        key = (room_id, sender)
        async with self._locks.setdefault(key, asyncio.Lock()):
            try:
                await self._handle(key, body)
            except Exception:
                log.exception("error handling message in %s from %s", room_id, sender)
                lang = self.sessions[key].lang if key in self.sessions else "de"
                await self._say(room_id, T[lang]["internal"])

    async def _handle(self, key, body: str) -> None:
        room_id, sender = key
        cmd = parse_command(body)
        if self.members is not None and sender not in self.members:
            if cmd:
                log.info("refused command %s from non-member %s", cmd.name, sender)
                await self._say(room_id, T[cmd.lang]["not_member"])
            return
        if cmd:
            log.info("command %s from %s", cmd.name, sender)
        p = self._pending(key)
        if cmd is None:
            if p:
                await self._reply_to_pending(key, p, norm(strip_reply_fallback(body)), explicit=False)
            return
        lang = cmd.lang if (cmd.name in ("request", "help") or not p) else p.lang
        n = cmd.name
        if n == "help":
            await self._say(room_id, *fmt.help_message(HELP[lang]))
        elif n == "request":
            await self._request(key, cmd.args, cmd.lang)
        elif n in ("short_lab", "short_report"):
            await self._short(key, LAB if n == "short_lab" else REPORT, cmd.args, cmd.lang)
        elif n in ("file", "reject", "hold"):
            await self._decide(key, n, cmd.args, cmd.lang)
        elif n == "show":
            await self._show(key, cmd.args.split()[0] if cmd.args.split() else None, cmd.lang)
        elif n == "identity":
            await self._identity(key, cmd.args, cmd.lang)
        elif n == "unlock":
            await self._unlock(key, cmd.args.split()[0] if cmd.args.split() else None, cmd.lang)
        elif n in ("status", "audit"):
            await self._lookup(key, n, cmd.args.split()[0] if cmd.args.split() else None, cmd.lang)
        elif n in ("yes", "no", "cancel", "fix"):
            if not p:
                await self._say(room_id, T[lang]["nothing_open"])
            elif n == "fix":
                await self._fix(key, p)
            else:
                await self._reply_to_pending(key, p, "ja" if n == "yes" else "nein", explicit=True)
        else:
            await self._say(room_id, T[lang]["unknown_cmd"].format(w=cmd.word))

    def _pending(self, key) -> Pending | None:
        p = self.sessions.get(key)
        if p and self.clock() - p.touched > self.ttl:
            del self.sessions[key]
            return None
        return p

    async def _say(self, room_id: str, text: str, html: str | None = None, decision: bool = False):
        # a card that asks for a decision goes out as m.text: Element greys m.notice, and these must read at full weight
        if decision:
            return await self.send(room_id, text, html, "m.text")
        return await self.send(room_id, text, html)

    # ------------------------------------------------------------ request + pre-flight

    async def _request(self, key, args: str, lang: str) -> None:
        room_id, sender = key
        old = self.sessions.pop(key, None)
        if old and old.held_rid:
            await self._cancel_held(old, sender, room_id)
        L = T[lang]
        if not args.strip():
            await self._say(room_id, L["usage"])
            return
        d = parse_request_args(args, lang)
        if d.missing() or d.problems:
            lines = [L["incomplete"]]
            if d.missing():
                lines.append(L["missing"].format(f=", ".join(L["field"][s] for s in d.missing())))
            lines += [f"• {x}" for x in d.problems]
            lines.append(L["usage"])
            await self._say(room_id, "\n".join(lines))
            return
        await self._preflight(key, Pending(d, lang, touched=self.clock()))

    async def _short(self, key, kind: str, args: str, lang: str) -> None:
        """!labor <Nachname> <patient-id> [Kontakt] [, Text: …]: recipient filled from the register and this
        room's earlier requests. Ambiguous or unknown -> one line + the long form; never a guess."""
        room_id, sender = key
        L = T[lang]
        word = ("labor" if kind == LAB else "reha") if lang == "de" else ("lab" if kind == LAB else "report")
        long_cmd = (f"!anfordern {'labor' if kind == LAB else 'reha'}" if lang == "de"
                    else f"!request {'lab' if kind == LAB else 'report'}")
        if not args.strip():
            await self._say(room_id, L["short_usage"].format(w=word, long=long_cmd))
            return
        sh = parse_short(args, lang)
        if sh.long_form:
            await self._request(key, f"{'labor' if kind == LAB else 'reha'} {args}", lang)
            return
        if sh.problems:
            await self._say(room_id, "\n".join([L["incomplete"], *[f"• {x}" for x in sh.problems]]))
            return
        if not sh.query or not sh.patient_id:
            await self._say(room_id, L["short_usage"].format(w=word, long=long_cmd))
            return
        hits = rcp.match(sh.query, rcp.candidates(self.recipients.get(room_id, []), self.register))
        q = " ".join(sh.query)
        if not hits:
            await self._say(room_id, L["short_unknown"].format(q=q, long=long_cmd))
            return
        if len(hits) > 1:
            who = "; ".join(f"{c.name} ({c.org or '?'})" for c in hits[:5])
            await self._say(room_id, L["short_ambiguous"].format(q=q, who=who, long=long_cmd))
            return
        c = hits[0]
        channel, address = sh.channel or c.channel, sh.address or c.address
        if not address:
            await self._say(room_id, L["short_no_contact"].format(name=c.name, org=c.org or "?", w=word,
                                                                  q=q, pid=sh.patient_id))
            return
        if not c.role or not c.org:
            await self._say(room_id, L["short_unknown"].format(q=q, long=long_cmd))
            return
        d = Draft(patient_id=sh.patient_id, doc_kind=kind, name=c.name, role=c.role, org=c.org,
                  channel=channel, address=address, note=sh.note, request_text=sh.request_text,
                  patient_from_name=sh.patient_from_name)
        old = self.sessions.pop(key, None)
        if old and old.held_rid:
            await self._cancel_held(old, sender, room_id)
        await self._preflight(key, Pending(d, lang, touched=self.clock()))

    def close_holds(self, room_id: str, ids) -> None:
        """Holds voided by the service (a newer answer was filed): no decision is pending any more."""
        gone = {str(i) for i in ids if valid_id(i)}
        self.holds[room_id] = [h for h in self.holds.get(room_id, []) if h not in gone]

    def remember_recipient(self, room_id: str, d: Draft) -> None:
        lst = [h for h in self.recipients.get(room_id, [])
               if rcp._key(h.get("name") or "", h.get("org")) != rcp._key(d.name or "", d.org)]
        lst.append({"name": d.name, "role": d.role, "org": d.org, "channel": d.channel, "address": d.address})
        self.recipients[room_id] = lst[-50:]

    def _local_warnings(self, p: Pending, service_warned: bool) -> list[dict]:
        out = [] if service_warned else local_address_warnings(p.draft.channel, p.draft.address)
        if p.draft.patient_from_name:
            out.append({"code": "patient_id_derived", "field": "patient_id",
                        "message": T[p.lang]["derived"].format(pid=p.draft.patient_id)})
        return out

    async def _preflight(self, key, p: Pending) -> None:
        """dry_run -> show register result + address check -> wait for !ja/!nein. Never sends."""
        room_id, sender = key
        body = p.draft.to_body(room_id, sender)
        try:
            resp = await self.service.create_request({**body, "dry_run": True})
        except ServiceError as ex:
            await self._say(room_id, T[p.lang]["not_sent"].format(err=str(ex)))
            return
        status = str(resp.get("status") or "").lower()
        if not _is_dry(resp) and status not in HELD:
            # Service without dry-run support sent it straight away (no warnings): report, don't resend.
            log.warning("service ignored dry_run and sent request %s", resp.get("request_id"))
            await self._report_sent(key, p, resp)
            return
        p.held_rid = valid_id(resp.get("request_id")) if status in HELD else None
        if p.held_rid:
            self.note_request(room_id, p.held_rid, resp.get("ref_code"))
        svc_w = _service_warnings(resp)
        p.warnings = svc_w + self._local_warnings(p, any(fmt.is_address_warning(w) for w in svc_w))
        p.register = resp.get("register") or resp.get("registry") or resp.get("register_check")
        p.patient = resp.get("patient") or p.patient
        p.touched = self.clock()
        self.sessions[key] = p
        text, html = fmt.confirm_card(p.draft, p.warnings, p.register, p.lang, p.patient)
        await self._say(room_id, text, html, decision=True)

    async def _reply_to_pending(self, key, p: Pending, text: str, explicit: bool) -> None:
        room_id, sender = key
        p.touched = self.clock()
        yn = yes_no(text)
        if yn == "yes":
            await self._send_confirmed(key, p)
        elif yn == "no":
            self.sessions.pop(key, None)
            if p.held_rid:
                await self._cancel_held(p, sender, room_id)
            await self._say(room_id, T[p.lang]["cancelled"])
        else:
            c = classify_contact(text)
            if channel_for(c):
                await self._correct(key, p, c.value)
            elif "@" in text or sum(ch.isdigit() for ch in text) >= 6:
                await self._say(room_id, contact_problem(c, p.lang) + "\n" + T[p.lang]["retry"])
            elif explicit:
                await self._say(room_id, T[p.lang]["retry"])
            # anything else is ordinary chat in the room: ignore

    async def _correct(self, key, p: Pending, address: str) -> None:
        c = classify_contact(address)
        if p.held_rid:
            await self._cancel_held(p, key[1], key[0])
        p.draft.channel, p.draft.address = channel_for(c), c.value
        p.held_rid, p.warnings, p.register = None, [], None
        await self._preflight(key, p)

    async def _fix(self, key, p: Pending) -> None:
        sugg = next((s for s in (fmt.warning_suggestion(w, p.draft.address) for w in p.warnings) if s), None)
        if not sugg or not channel_for(classify_contact(sugg)):
            await self._say(key[0], T[p.lang]["no_suggestion"])
            return
        await self._correct(key, p, sugg)

    async def _send_confirmed(self, key, p: Pending) -> None:
        room_id = key[0]
        self.inflight[room_id] = self.inflight.get(room_id, 0) + 1
        try:
            await self._send_confirmed_inner(key, p)
        finally:
            self.inflight[room_id] -= 1

    async def _send_confirmed_inner(self, key, p: Pending) -> None:
        room_id, sender = key
        try:
            if p.held_rid:
                resp = await self.service.confirm_request(p.held_rid, sender, room_id,
                                                          await self._ack_codes(p, sender, room_id))
            else:
                body = p.draft.to_body(room_id, sender)
                body["confirm"] = True
                if p.warnings:
                    body["confirm_warnings"] = True
                    body["acknowledged_warnings"] = [fmt.warning_code(w) for w in p.warnings]
                resp = await self.service.create_request(body)
        except ServiceError as ex:
            await self._say(room_id, T[p.lang]["send_failed"].format(err=str(ex)))
            return
        if str(resp.get("status") or "").lower() in HELD:     # should not happen after confirm
            p.held_rid = valid_id(resp.get("request_id"))
            p.warnings = _service_warnings(resp) or p.warnings
            text, html = fmt.confirm_card(p.draft, p.warnings, p.register, p.lang, p.patient)
            await self._say(room_id, text, html, decision=True)
            return
        await self._report_sent(key, p, resp)

    async def _ack_codes(self, p: Pending, sender: str, room_id: str) -> list[str]:
        """Every warning code Dr. Hofer saw on the card. If the card only had message strings (no codes),
        read the stored request's warning_details so the service gets the complete list."""
        codes = [fmt.warning_code(w) for w in p.warnings if isinstance(w, dict) and w.get("code")]
        if any(not (isinstance(w, dict) and w.get("code")) for w in p.warnings):
            try:
                stored = await self.service.get_request(p.held_rid, sender, room_id)
                codes += [str(w["code"]) for w in (stored or {}).get("warning_details") or []
                          if isinstance(w, dict) and w.get("code")]
            except ServiceError as ex:
                log.warning("could not read warning codes of %s: %s", p.held_rid, ex)
        return list(dict.fromkeys(codes))

    async def _report_sent(self, key, p: Pending, resp: dict) -> None:
        room_id, _ = key
        self.sessions.pop(key, None)
        rid = fmt.primary_id(resp.get("ref_code"), resp.get("request_id"), resp.get("id"))
        if rid:
            self.last_request[room_id] = rid          # KB- ref when known: the one id shown everywhere
            self.note_request(room_id, rid, resp.get("request_id"), resp.get("ref_code"))
        if not any(fmt.is_address_warning(w) for w in p.warnings):   # never remember a warned (confusable) address
            self.remember_recipient(room_id, p.draft)
        text, html = fmt.sent_message(p.draft, resp, p.lang, p.patient)
        self.sent_cards |= {str(x) for x in (resp.get("ref_code"), resp.get("request_id")) if valid_id(x)}
        if resp.get("warnings") and not p.warnings:
            note = T[p.lang]["sent_with_warnings"].format(w="; ".join(fmt.warning_text(w) for w in resp["warnings"]))
            text += "\n" + note
            html = (html or "") + f"<p>{fmt.e(note)}</p>"
        await self._say(room_id, text, html)

    async def _cancel_held(self, p: Pending, by: str, room_id: str | None = None) -> None:
        try:
            await self.service.cancel_request(p.held_rid, by, room_id)
        except ServiceError as ex:
            log.warning("cancel of held request %s failed: %s", p.held_rid, ex)

    # ------------------------------------------------------------ hold decisions

    def note_request(self, room_id: str, *ids) -> None:
        for i in ids:
            if valid_id(i):
                self.request_rooms[str(i)] = room_id

    def _room_ok(self, rid: str, room_id: str) -> bool:
        """Known request from another room -> refuse. Unknown ids go to the service, which authorises
        on X-PGK-Actor / X-PGK-Room."""
        known = self.request_rooms.get(rid)
        return known is None or known == room_id

    def register_hold(self, room_id: str, hold_id: str) -> None:
        if not valid_id(hold_id):
            return
        self.note_request(room_id, hold_id)
        lst = self.holds.setdefault(room_id, [])
        if hold_id not in lst:
            lst.append(hold_id)

    async def _decide(self, key, action: str, args: str, lang: str) -> None:
        room_id, sender = key
        L = DECIDE[lang]
        open_ = self.holds.get(room_id, [])
        first, _, rest = args.strip().partition(" ")
        if first and first in open_:
            hid, reason = first, rest.strip()
        elif len(open_) == 1 and (not first or not _looks_like_id(first)):
            hid, reason = open_[0], args.strip()
        elif first:
            hid, reason = first, rest.strip()
        else:
            await self._say(room_id, L["need_id"])
            return
        if not valid_id(hid):
            await self._say(room_id, T[lang]["bad_id"])
            return
        if not self._room_ok(hid, room_id):
            await self._say(room_id, T[lang]["other_room"])
            return
        if action == "file" and not reason:
            await self._say(room_id, L["need_reason"].format(id=hid))
            return
        try:
            await self.service.decide(hid, action, reason or None, sender, room_id)
        except ServiceError as ex:
            await self._say(room_id, f"{L['failed']} {hid}: {ex}")
            return
        if action in ("file", "reject") and hid in open_:
            open_.remove(hid)
        await self._say(room_id, L[action].format(id=hid, reason=reason or "–"))

    # ------------------------------------------------------------ namesake: confirm a new eID identity

    def register_identity_hold(self, room_id: str, hold_id: str) -> None:
        if valid_id(hold_id):
            self.note_request(room_id, hold_id)
            lst = self.identity_holds.setdefault(room_id, [])
            if hold_id not in lst:
                lst.append(hold_id)

    async def _identity(self, key, args: str, lang: str) -> None:
        """!identität <id> ja|nein  (!person <id> freigeben|ablehnen, !identity <id> yes|no)."""
        room_id, sender = key
        toks = args.split()
        rid = next((t for t in toks if valid_id(t)), None)
        verdict = next((_IDENT_WORDS[t.lower().strip("!.")] for t in toks if t.lower().strip("!.") in _IDENT_WORDS), None)
        open_ = self.identity_holds.get(room_id, [])
        if not rid and len(open_) == 1 and not [t for t in toks if t.lower().strip("!.") not in _IDENT_WORDS]:
            rid = open_[0]
        if not rid:
            await self._say(room_id, T[lang]["bad_id"] if [t for t in toks if t.lower().strip("!.") not in _IDENT_WORDS]
                            else T[lang]["ident_usage"])
            return
        if not verdict:
            await self._say(room_id, T[lang]["ident_usage"])
            return
        if not self._room_ok(rid, room_id):
            await self._say(room_id, T[lang]["other_room"])
            return
        action = "confirm_identity" if verdict == "yes" else "reject_identity"
        try:
            await self.service.decide(rid, action, None, sender, room_id)
        except ServiceError as ex:
            await self._say(room_id, f"{DECIDE[lang]['failed']} {rid}: {ex}")
            return
        if rid in open_:
            open_.remove(rid)
        await self._say(room_id, T[lang]["ident_yes" if verdict == "yes" else "ident_no"].format(id=rid))

    # ------------------------------------------------------------ show a held document

    async def _show(self, key, rid: str | None, lang: str) -> None:
        room_id, sender = key
        open_ = self.holds.get(room_id, [])
        rid = rid or (open_[0] if len(open_) == 1 else None)
        if not rid:
            await self._say(room_id, T[lang]["show_usage"])
            return
        if not valid_id(rid):
            await self._say(room_id, T[lang]["bad_id"])
            return
        if not self._room_ok(rid, room_id):
            await self._say(room_id, T[lang]["other_room"])
            return
        if rid not in open_:
            await self._say(room_id, T[lang]["show_not_held"].format(id=rid))
            return
        try:
            data = await self.service.get_preview(rid, sender, room_id)
        except ServiceError as ex:
            await self._say(room_id, f"{rid}: {ex}")
            return
        text, html = fmt.preview_message(rid, data, lang)
        await self._say(room_id, text, html)

    # ------------------------------------------------------------ unlock a locked link

    async def _unlock(self, key, rid: str | None, lang: str) -> None:
        room_id, sender = key
        rid = rid or self.last_request.get(room_id)
        if not rid:
            await self._say(room_id, T[lang]["need_id"].format(c="freigeben" if lang == "de" else "unlock"))
            return
        if not valid_id(rid):
            await self._say(room_id, T[lang]["bad_id"])
            return
        if not self._room_ok(rid, room_id):
            await self._say(room_id, T[lang]["other_room"])
            return
        try:
            resp = await self.service.unlock_request(rid, sender, room_id)
        except ServiceError as ex:
            await self._say(room_id, T[lang]["unlock_failed"].format(id=rid, err=str(ex)))
            return
        ref = resp.get("ref_code")
        self.note_request(room_id, rid, resp.get("request_id"), ref)
        await self._say(room_id, T[lang]["unlocked"].format(
            id=rid + (f" (Ref {ref})" if ref and ref != rid else ""), status=resp.get("status") or "?",
            name=(resp.get("recipient") or {}).get("name") or ("die angefragte Person" if lang == "de" else "the requested person")))

    # ------------------------------------------------------------ status / audit

    async def _lookup(self, key, what: str, rid: str | None, lang: str) -> None:
        room_id, sender = key
        rid = rid or self.last_request.get(room_id)
        if not rid:
            await self._say(room_id, T[lang]["need_id"].format(c=what))
            return
        if not valid_id(rid):
            await self._say(room_id, T[lang]["bad_id"])
            return
        if not self._room_ok(rid, room_id):
            await self._say(room_id, T[lang]["other_room"])
            return
        try:
            if what == "audit":
                text, html = fmt.audit_message(rid, await self.service.get_audit(rid, sender, room_id), lang)
            else:
                try:
                    text, html = fmt.status_message(rid, await self.service.get_request(rid, sender, room_id), lang)
                except ServiceError as ex:
                    if ex.status not in (404, 405):
                        raise
                    rows = [fmt.audit_entry_line(x) for x in fmt.audit_rows(
                        await self.service.get_audit(rid, sender, room_id))]
                    if not rows:
                        raise
                    w, s, who, _ = rows[-1]
                    text = f"Status {rid}: {fmt.step_label(s)} ({fmt.hhmm(w)}{', ' + who if who else ''})"
                    html = None
        except ServiceError as ex:
            text, html = f"{what} {rid}: {ex}", None
        await self._say(room_id, text, html)


_IDENT_WORDS = {"ja": "yes", "j": "yes", "yes": "yes", "y": "yes", "freigeben": "yes", "zulassen": "yes",
                "confirm": "yes", "ok": "yes", "nein": "no", "n": "no", "no": "no", "ablehnen": "no",
                "reject": "no", "sperren": "no"}


def _service_warnings(resp: dict) -> list:
    """The service returns messages in `warnings` and dicts (code, message, similar_to) in `warning_details`."""
    det = resp.get("warning_details")
    if isinstance(det, list) and det:
        return det
    return list(resp.get("warnings") or [])


def _looks_like_id(tok: str) -> bool:
    return any(c.isdigit() for c in tok) or "-" in tok or "_" in tok


def _is_dry(resp: dict) -> bool:
    if resp.get("dry_run") is True:
        return True
    status = str(resp.get("status") or "").lower()
    if status in DRY:
        return True
    return not (resp.get("request_id") or resp.get("id")) and status not in HELD

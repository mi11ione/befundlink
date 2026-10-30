"""Single confirm step (register pre-flight + address warning), dry-run/confirm, hold decisions."""
import asyncio

from bot.core import Core
from bot.parse import Draft
from bot.service import ServiceError

ROOM, LENA = "!room:klinikum.example", "@lena.hofer:klinikum.example"
EVA = "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, {addr}, Patient maria-beispiel"
STEFAN = "!anfordern reha Stefan Kern, Entlassungsmanagement, Reha Waldblick, {addr}, Patient maria-beispiel"
REG_EVA = {"source": "Ärzteliste", "ok": True,
           "text": "Ärzteliste: Dr. Eva Brandl, Medizinische und Chemische Labordiagnostik ✓"}


class FakeService:
    """dry_run=True -> preview with warnings + register; real call sends (needs confirm_warnings if warned).
    mode='legacy' mimics a service without dry_run: stores warned requests as needs_confirmation."""

    def __init__(self, mode="dry", fail=False, register=REG_EVA):
        self.calls, self.decisions, self.confirms, self.cancels, self.decision_rooms = [], [], [], [], []
        self.lookups = []
        self.mode, self.fail, self.register, self.n = mode, fail, register, 0
        self.patient = {"name": "Maria Beispiel", "birthDate": "1961-03-12"}
        self.held, self.acks = {}, []

    def warn(self, body):
        if "labor-kanaI" in body["address"]:
            return [{"code": "confusable", "domain": "labor-kanaI.example", "similar_to": "labor-kanal.example",
                     "message": "Achtung: 'labor-kanaI.example' enthält ein großes I statt l – meinten Sie labor-kanal.example?"}]
        return []

    async def create_request(self, body):
        self.calls.append(body)
        if self.fail:
            raise ServiceError("Dienst nicht erreichbar (http://localhost:8000/api/requests): ClientConnectorError")
        w = self.warn(body)
        if self.mode == "dry" and body.get("dry_run"):
            return {"request_id": None, "status": "dry_run", "warnings": [x["message"] for x in w],
                    "warning_details": w, "register": self.register, "patient": self.patient}
        if w and not body.get("confirm_warnings"):
            self.n += 1
            self.held[f"req-{self.n:012x}"] = {x["code"] for x in w}
            return {"request_id": f"req-{self.n:012x}", "status": "needs_confirmation",
                    "warnings": [x["message"] for x in w], "warning_details": w}
        self.n += 1
        return {"request_id": f"req-{self.n:012x}", "ref_code": "KB-ABC123", "status": "sent", "warnings": []}

    async def confirm_request(self, rid, by, room=None, acknowledged=None):
        missing = self.held.get(rid, set()) - set(acknowledged or [])
        if missing:      # like the service's core.confirm: every code must be acknowledged
            raise ServiceError("acknowledged_warnings must list every warning code; missing: " + ", ".join(sorted(missing)), 409)
        self.acks.append(list(acknowledged or []))
        self.confirms.append((rid, by))
        return {"request_id": rid, "status": "sent"}

    async def cancel_request(self, rid, by, room=None):
        self.cancels.append((rid, by))
        return {"request_id": rid, "status": "cancelled"}

    async def decide(self, aid, action, reason, by, room=None):
        self.decisions.append((aid, action, reason, by))
        self.decision_rooms.append(room)
        return {"status": {"file": "filed", "reject": "rejected", "hold": "held",
                           "confirm_identity": "identity_verified", "reject_identity": "blocked_identity"}[action]}

    async def get_request(self, rid, actor=None, room=None):
        self.lookups.append(("status", rid, actor, room))
        raise ServiceError("not found", 404)

    async def get_audit(self, rid, actor=None, room=None):
        self.lookups.append(("audit", rid, actor, room))
        return [{"step": "request sent", "outcome": "success", "who": LENA, "what": "E-Mail", "at": "2026-10-29T19:02:11+01:00"},
                {"step": "link opened", "outcome": "success", "who": "anonym", "what": "GET /r/…", "at": "2026-10-29T19:03:00+01:00"}]


def make(**kw):
    out = []

    async def send(room, text, html=None, *_):
        out.append(text)
    svc = FakeService(**kw)
    return Core(svc, send), svc, out


def run(core, *msgs, sender=LENA):
    async def go():
        for m in msgs:
            await core.handle(ROOM, sender, m)
    asyncio.run(go())


def test_always_confirm_with_register_then_send():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    assert len(svc.calls) == 1 and svc.calls[0]["dry_run"] is True              # nothing sent yet
    card = out[-1]
    assert card.startswith("Anforderung prüfen · noch nicht gesendet\n")
    assert "• Patient: Maria Beispiel, geb. 12.03.1961 (maria-beispiel)" in card
    assert "\nÄrzteliste: Dr. Eva Brandl, Medizinische und Chemische Labordiagnostik ✓\n" in card
    assert "✓ Adresse: keine Auffälligkeiten" in card and "!ja = senden · !nein = abbrechen" in card
    run(core, "!ja")
    body = svc.calls[-1]
    assert body["confirm"] is True and "dry_run" not in body and "confirm_warnings" not in body
    assert body["requester_mxid"] == LENA and body["room_id"] == ROOM
    assert body["recipient"] == {"name": "Dr. Eva Brandl", "role": "Laborärztin", "org": "Labor Kanalstraße"}
    assert body["channel"] == "email" and body["doc_kind"] == "lab_result" and body["patient_id"] == "maria-beispiel"
    assert out[-1].startswith("Anfrage gesendet · Laborbefund · Maria Beispiel · 12.03.1961")
    assert "Status:" not in out[-1] and "Verfolgen: !status KB-ABC123 · !audit KB-ABC123" in out[-1]   # "gesendet" once
    assert "req-" not in out[-1]                                              # one id: the KB- reference
    assert core.last_request[ROOM] == "KB-ABC123" and not core.sessions


def test_sms_and_register_not_found():
    core, svc, out = make(register={"source": "GDA-Index", "ok": False, "name": "Reha Waldblick"})
    run(core, STEFAN.format(addr="+436600000101"))
    assert svc.calls[0]["channel"] == "sms" and svc.calls[0]["address"] == "+43 660 000 0101"
    assert "⚠️ GDA-Index: Reha Waldblick nicht gefunden – Rolle nur laut Anfrage" in out[-1]
    assert "!ja = trotzdem an +43 660 000 0101 senden" in out[-1] and "✓ Nummer: keine Auffälligkeiten" in out[-1]
    run(core, "ja")
    assert svc.calls[-1]["confirm"] is True and svc.calls[-1]["doc_kind"] == "discharge_report"


def test_register_string_list():
    core, svc, out = make(register=["GDA-Index: Reha Waldblick ✓", "Rolle: Entlassungsmanagement (laut Anfrage)"])
    run(core, STEFAN.format(addr="+43 660 000 0101"))
    assert "\nGDA-Index: Reha Waldblick ✓\n✓ Rolle: Entlassungsmanagement (laut Anfrage)\n" in out[-1]
    assert "!ja = senden" in out[-1]


def test_confusable_warning_yes_sends_with_confirm_warnings():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    card = out[-1]
    assert "⚠️ Achtung: 'labor-kanaI.example' enthält ein großes I statt l – meinten Sie labor-kanal.example?" in card
    assert "!ja = trotzdem an eva.brandl@labor-kanaI.example senden" in card
    assert "!korrigieren = an eva.brandl@labor-kanal.example senden" in card and "korrigierte Adresse" in card
    assert card.startswith("Nicht gesendet · Adresse prüfen\n⚠️ Achtung")    # warning first
    assert "In Worten: großes I statt kleinem l (labor-kanaI.example → labor-kanal.example)" in card
    run(core, "!ja")
    b = svc.calls[-1]
    assert b["confirm"] is True and b["confirm_warnings"] is True and b["acknowledged_warnings"] == ["confusable"]
    assert b["address"] == "eva.brandl@labor-kanaI.example" and out[-1].startswith("Anfrage gesendet")


def test_warning_no_cancels():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"), "!nein")
    assert len(svc.calls) == 1 and "nichts gesendet" in out[-1] and not core.sessions


def test_corrected_address_reruns_preflight():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"), "eva.brandl@labor-kanal.example")
    assert [c.get("dry_run") for c in svc.calls] == [True, True]
    assert "keine Auffälligkeiten" in out[-1]
    run(core, "!ja")
    assert svc.calls[-1]["address"] == "eva.brandl@labor-kanal.example" and "confirm_warnings" not in svc.calls[-1]


def test_fix_uses_suggestion_from_similar_to():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"), "!korrigieren", "!ja")
    assert svc.calls[-1]["address"] == "eva.brandl@labor-kanal.example" and out[-1].startswith("Anfrage gesendet")


def test_room_chatter_during_confirm_is_ignored_but_bad_address_is_answered():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    n = len(out)
    run(core, "moment, ich schau kurz nach")
    assert len(out) == n and core.sessions
    run(core, "eva.brandl@labor")
    assert "weder eine E-Mail-Adresse" in out[-1]


def test_local_confusable_check_when_service_misses_it():
    core, svc, out = make()
    svc.warn = lambda body: []
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    assert "großes I statt l" in out[-1] and len(svc.calls) == 1


def test_legacy_service_without_dry_run():
    """Service ignores dry_run: no warnings -> it already sent (reported once, not resent);
    warnings -> it holds as needs_confirmation, !ja calls /confirm, !nein calls /cancel."""
    core, svc, out = make(mode="legacy")
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    assert len(svc.calls) == 1 and out[-1].startswith("Anfrage gesendet")
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    assert "großes I statt l" in out[-1]
    run(core, "!ja")
    assert svc.confirms == [("req-000000000002", LENA)] and out[-1].startswith("Anfrage gesendet")
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"), "!nein")
    assert svc.cancels == [("req-000000000003", LENA)]


def test_service_down():
    core, svc, out = make(fail=True)
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    assert out[-1].startswith("Nicht gesendet: Dienst nicht erreichbar") and not core.sessions


def test_send_failure_keeps_pending_for_retry():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    svc.fail = True
    run(core, "!ja")
    assert "erneut versuchen" in out[-1] and core.sessions
    svc.fail = False
    run(core, "!ja")
    assert out[-1].startswith("Anfrage gesendet")


def test_incomplete_command_sends_nothing():
    core, svc, out = make()
    run(core, "!anfordern labor Dr. Eva Brandl, Laborärztin")
    assert svc.calls == [] and "unvollständig" in out[-1]
    assert "Es fehlt: Patient <id>, Organisation, E-Mail oder österreichische Mobilnummer." in out[-1]
    run(core, "!anfordern")
    assert out[-1].startswith("Beispiel: !anfordern labor") and not core.sessions
    run(core, STEFAN.format(addr="+49 151 1234567"))
    assert "keine österreichische Nummer" in out[-1] and svc.calls == []


def test_english():
    core, svc, out = make()
    run(core, "!request report Stefan Kern, discharge manager, Reha Waldblick, +43 660 000 0101, patient maria-beispiel")
    assert out[-1].startswith("Check request · not sent yet\n") and "!yes = send" in out[-1]
    run(core, "!yes")
    assert out[-1].startswith("Request sent · discharge report")


def test_sessions_are_per_sender():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    run(core, "!ja", sender="@other:klinikum.example")
    assert "Keine offene Anforderung" in out[-1] and len(svc.calls) == 1


def test_bad_command_answered_and_plain_chat_ignored():
    core, svc, out = make()
    run(core, "hallo zusammen")
    assert out == []
    run(core, "!foo")
    assert "Unbekannter Befehl '!foo'" in out[-1]
    run(core, "!hilfe")
    assert "!ablegen <id> <Begründung>" in out[-1] and "bleibt intern" in out[-1]


def test_derived_patient_flagged():
    core, svc, out = make()
    run(core, "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient Maria Beispiel")
    assert "abgeleitet" in out[-1] and "!ja = trotzdem" in out[-1]


def test_internal_note_never_sent_as_note():
    core, svc, out = make()
    run(core, "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, "
              "Patient maria-beispiel, Notiz: Gerinnung vor Hüft-OP")
    assert "Interne Notiz (nicht versendet): Gerinnung vor Hüft-OP" in out[-1]
    run(core, "!ja")
    assert "note" not in svc.calls[-1] and svc.calls[-1]["internal_note"] == "Gerinnung vor Hüft-OP"


def test_hold_decisions():
    core, svc, out = make()
    core.register_hold(ROOM, "7")
    run(core, "!ablegen 7")
    assert "Begründung" in out[-1] and svc.decisions == []
    run(core, "!ablegen 7 Tag/Monat im Befund vertauscht, telefonisch bestätigt")
    assert svc.decisions[-1] == ("7", "file", "Tag/Monat im Befund vertauscht, telefonisch bestätigt", LENA)
    assert core.holds[ROOM] == []
    core.register_hold(ROOM, "8")
    run(core, "!verwerfen")
    assert svc.decisions[-1][:2] == ("8", "reject")
    run(core, "!halten 9")
    assert svc.decisions[-1][:2] == ("9", "hold")
    run(core, "!reject 10")
    assert svc.decisions[-1][:2] == ("10", "reject") and "rejected" in out[-1]


def test_status_falls_back_to_audit_and_audit():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"), "!ja", "!status")
    assert out[-1].startswith("Status KB-ABC123: Link geöffnet (29.10. 19:03:00")
    run(core, "!audit KB-ABC123")
    assert "2/5 Schritte (keine Fehlzustellung): ✓ Anfrage gesendet ✓ Link geöffnet ○ Identität geprüft" in out[-1]
    assert "Fehlzustellung erkannt" not in out[-1]


def test_lane_b_register_shape_not_found():
    """The service: register={ok,text:'⚠ …',message} and the same message again as a register_not_confirmed warning."""
    msg = "Organisation 'Reha Waldblick' NICHT im GDA-Index gefunden. Trotzdem senden?"
    core, svc, out = make(register={"ok": False, "message": msg, "text": "⚠ " + msg})
    orig = svc.warn
    svc.warn = lambda b: orig(b) + [{"code": "register_not_confirmed", "message": msg, "domain": None, "similar_to": None}]
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    card = out[-1]
    assert card.count("NICHT im GDA-Index") == 1 and "\n⚠ " + msg in card
    assert "großes I statt l" in card and "!korrigieren = an eva.brandl@labor-kanal.example senden" in card
    run(core, "!ja")
    assert set(svc.calls[-1]["acknowledged_warnings"]) >= {"confusable", "register_not_confirmed"}


def test_register_only_warning_keeps_local_address_check():
    msg = "x NICHT gefunden"
    core, svc, out = make(register={"ok": False, "text": "⚠ " + msg})
    svc.warn = lambda b: [{"code": "register_not_confirmed", "message": msg}]
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    assert "großes I statt l" in out[-1] and "✓ Adresse" not in out[-1]


def test_invalid_ids_never_reach_the_service():
    core, svc, out = make()
    for cmd in ("!status ../admin", "!audit req-1%2F..", "!ablegen x/../y Grund", "!verwerfen KB-abc", "!status req-ZZZ"):
        run(core, cmd)
        assert "Ungültige ID" in out[-1], cmd
    assert svc.lookups == [] and svc.decisions == []


def test_actor_and_room_passed_and_other_room_refused():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"), "!ja")
    rid = core.last_request[ROOM]
    run(core, f"!audit {rid}")
    assert svc.lookups[-1] == ("audit", rid, LENA, ROOM)
    run(core, "!status KB-ABC123")                       # ref code is bound to the same room
    assert svc.lookups[-2][1:] == ("KB-ABC123", LENA, ROOM)

    async def other():
        await core.handle("!other:klinikum.example", LENA, f"!audit {rid}")
        await core.handle("!other:klinikum.example", LENA, "!audit KB-ABC123")
    n = len(svc.lookups)
    asyncio.run(other())
    assert len(svc.lookups) == n and "anderen Raum" in out[-1]
    core.register_hold(ROOM, "12")
    run(core, "!verwerfen 12")
    assert svc.decision_rooms[-1] == ROOM

    async def other_decide():
        await core.handle("!other:klinikum.example", LENA, "!ablegen 12 passt")
    asyncio.run(other_decide())
    assert len(svc.decisions) == 1 and "anderen Raum" in out[-1]


def test_non_members_are_refused():
    out = []

    async def send(room, text, html=None, *_):
        out.append(text)
    svc = FakeService()
    core = Core(svc, send, members={LENA})
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"), sender="@mallory:klinikum.example")
    assert svc.calls == [] and "Nicht berechtigt" in out[-1]
    run(core, "hallo", sender="@mallory:klinikum.example")
    assert len(out) == 1                                  # chatter: no reply
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    assert len(svc.calls) == 1


def test_one_word_recipient_name_refused():
    core, svc, out = make()
    for name in ("Brandl", "Dr. Brandl", "Dr. med. Brandl, MSc"):
        run(core, f"!anfordern labor {name}, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient maria-beispiel")
        assert "vollständigen Namen mit Vor- und Nachname" in out[-1] and svc.calls == [], name
    run(core, "!anfordern labor Dr. med. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient maria-beispiel")
    assert len(svc.calls) == 1


def test_logs_never_contain_message_bodies(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    core, svc, out = make()
    run(core, "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, geheim.adresse@labor-kanal.example, "
              "Patient maria-beispiel, Notiz: VERTRAULICH", "geheim plauder", "!ja")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "command request from @lena.hofer:klinikum.example" in logs
    for secret in ("geheim", "VERTRAULICH", "Brandl", "maria-beispiel"):
        assert secret not in logs, secret


def test_unlock_command():
    core, svc, out = make()
    calls = []

    async def unlock_request(rid, by, room=None):
        calls.append((rid, by, room))
        if rid == "req-0000000000ff":
            raise ServiceError("request is 'sent', not locked", 409)
        return {"request_id": rid, "ref_code": "KB-LOCK01", "status": "sent", "recipient": {"name": "Dr. Eva Brandl"}}
    svc.unlock_request = unlock_request
    run(core, "!freigeben req-0000000000aa")
    assert calls[-1] == ("req-0000000000aa", LENA, ROOM)
    assert out[-1].startswith("✓ Link zu req-0000000000aa (Ref KB-LOCK01) wieder freigegeben: Dr. Eva Brandl")
    run(core, "!unlock req-0000000000ff")
    assert out[-1].startswith("Cannot unlock") and "not locked" in out[-1]
    for bad, msg in (("!freigeben ../x", "Ungültige ID"), ("!unlock req-1", "Invalid ID")):
        run(core, bad)
        assert msg in out[-1]
    assert len(calls) == 2

    async def other_room():
        await core.handle("!other:klinikum.example", LENA, "!freigeben KB-LOCK01")   # bound to ROOM above
    asyncio.run(other_room())
    assert len(calls) == 2 and "anderen Raum" in out[-1]
    run(core, "!freigeben")                          # no id, no last request in room -> asks for id
    assert "!freigeben <id>" in out[-1] and len(calls) == 2


def test_request_text_on_card_and_in_body():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example") + ", Text: aktuellster Gerinnungsbefund, Eingriff 31.10.")
    assert "• Text an die Person (erst nach ID-Austria-Prüfung sichtbar): aktuellster Gerinnungsbefund, Eingriff 31.10." in out[-1]
    run(core, "!ja")
    assert svc.calls[-1]["request_text"] == "aktuellster Gerinnungsbefund, Eingriff 31.10."


def test_patient_not_resolved_by_service_is_a_warning():
    core, svc, out = make()
    svc.patient = None
    run(core, EVA.format(addr="eva.brandl@labor-kanal.example"))
    assert out[-1].startswith("Nicht gesendet · bitte prüfen\n⚠️ Patient 'maria-beispiel': "
                              "Name/Geburtsdatum vom Dienst nicht bestätigt – ID prüfen")
    assert "!ja = trotzdem an eva.brandl@labor-kanal.example senden" in out[-1]


def test_identity_commands():
    core, svc, out = make()
    run(core, "!identität KB-AB12CD ja")
    assert svc.decisions[-1] == ("KB-AB12CD", "confirm_identity", None, LENA) and svc.decision_rooms[-1] == ROOM
    assert out[-1].startswith("✓ KB-AB12CD: neue Identität zugelassen")
    run(core, "!person KB-AB12CD ablehnen")
    assert svc.decisions[-1][:2] == ("KB-AB12CD", "reject_identity") and out[-1].startswith("KB-AB12CD: neue Identität abgelehnt")
    run(core, "!identity KB-AB12CD yes")
    assert svc.decisions[-1][:2] == ("KB-AB12CD", "confirm_identity") and "new identity confirmed" in out[-1]
    n = len(svc.decisions)
    run(core, "!identität KB-AB12CD vielleicht")
    assert "!identität <KB-…> ja" in out[-1]
    run(core, "!identität ../x ja")
    assert "Ungültige ID" in out[-1]
    core.register_identity_hold(ROOM, "KB-ZZ99ZZ")
    run(core, "!identität nein")                     # single open identity question -> implied id
    assert svc.decisions[-1][:2] == ("KB-ZZ99ZZ", "reject_identity") and core.identity_holds[ROOM] == []

    async def other():
        await core.handle("!other:klinikum.example", LENA, "!identität KB-ZZ99ZZ ja")
    asyncio.run(other())
    assert len(svc.decisions) == n + 1 and "anderen Raum" in out[-1]


def test_show_command_holds_only():
    core, svc, out = make()
    calls = []

    async def get_preview(ref, actor, room=None):
        calls.append((ref, actor, room))
        return {"text": "Patient: Maria Beispiel, born 12 March 1961\nhttp://x.example"}
    svc.get_preview = get_preview
    run(core, "!zeigen KB-T2N6GW")
    assert calls == [] and "keine angehaltene Antwort" in out[-1]
    core.register_hold(ROOM, "KB-T2N6GW")
    run(core, "!zeigen KB-T2N6GW")
    assert calls == [("KB-T2N6GW", LENA, ROOM)] and out[-1].startswith("📄 Angehaltenes Dokument KB-T2N6GW")
    run(core, "!show")                                   # single open hold -> implied id
    assert len(calls) == 2 and out[-1].startswith("📄 Held document KB-T2N6GW")
    run(core, "!zeigen ../etc")
    assert "Ungültige ID" in out[-1] and len(calls) == 2

    async def other():
        await core.handle("!other:klinikum.example", LENA, "!zeigen KB-T2N6GW")
    asyncio.run(other())
    assert len(calls) == 2 and "anderen Raum" in out[-1]
    run(core, "!verwerfen KB-T2N6GW", "!zeigen KB-T2N6GW")
    assert len(calls) == 2 and "keine angehaltene Antwort" in out[-1]


def test_held_confirm_acknowledges_every_warning_code():
    core, svc, out = make(mode="legacy")
    orig = svc.warn
    svc.warn = lambda b: orig(b) + [{"code": "register_not_confirmed", "message": "nicht im Register"}]
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"), "!ja")
    assert svc.confirms and set(svc.acks[-1]) >= {"confusable", "register_not_confirmed"} and out[-1].startswith("Anfrage gesendet")


def test_held_confirm_reads_codes_from_stored_request_when_card_had_only_strings():
    core, svc, out = make(mode="legacy")
    orig_create = svc.create_request

    async def strings_only(body):            # a service that returns warnings as plain messages only
        r = await orig_create(body)
        r.pop("warning_details", None)
        return r
    svc.create_request = strings_only

    async def get_request(rid, actor=None, room=None):
        return {"request_id": rid, "warning_details": [{"code": c} for c in sorted(svc.held[rid])]}
    svc.get_request = get_request
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"), "!ja")
    assert "confusable" in svc.acks[-1] and out[-1].startswith("Anfrage gesendet")


def test_held_confirm_409_is_shown_and_retryable():
    core, svc, out = make(mode="legacy")
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    svc.held["req-000000000001"].add("new_code_after_card")
    run(core, "!ja")
    assert "missing: new_code_after_card" in out[-1] and "erneut versuchen" in out[-1] and core.sessions


def test_m1_merged_address_warning_one_line_and_all_codes_acknowledged():
    core, svc, out = make()
    main = ("Adresse prüfen: labor-kanaI.example (großes I) statt der bekannten labor-kanal.example (kleines l) "
            "– Tippfehler oder Täuschung?")
    svc.warn = lambda b: [
        {"code": "confusable", "message": main, "domain": "labor-kanaI.example", "similar_to": "labor-kanal.example"},
        {"code": "lookalike", "message": "Domain sieht aus wie labor-kanal.example", "merged_into": "confusable"},
        {"code": "mixed_case", "message": "Großbuchstabe in der Domain", "merged_into": "confusable",
         "similar_to": "labor-kanal.example"}] if "labor-kanaI" in b["address"] else []
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    card = out[-1]
    addr_lines = [l for l in card.split("\n") if l.startswith("⚠️") and "labor-kana" in l and "Nicht gesendet" not in l]
    assert addr_lines == [f"⚠️ {main}"]
    assert "In Worten" not in card and "Domain sieht aus" not in card and "Großbuchstabe" not in card
    assert "!korrigieren = an eva.brandl@labor-kanal.example senden" in card
    run(core, "!ja")
    assert set(svc.calls[-1]["acknowledged_warnings"]) == {"confusable", "lookalike", "mixed_case"}


def test_in_words_kept_when_service_message_does_not_name_characters():
    core, svc, out = make()
    run(core, EVA.format(addr="eva.brandl@labor-kanaI.example"))
    assert "In Worten: großes I statt kleinem l" in out[-1]


# ---------------------------------------------------------------- short form


def _no_service_patient_line(card):
    return "Maria Beispiel, geb. 12.03.1961" in card


def _register_without_contacts(core, tmp_path):
    """The real register fixture minus listed contacts: the case of a person known to the register only."""
    import json as _json
    from bot import recipients as rcp
    d = _json.loads(rcp.REGISTERS.read_text())
    for org in d.get("gda_index", {}).get("entries", []):
        for st in org.get("staff", []):
            for k in ("contact", "email", "mobile"):
                st.pop(k, None)
    f = tmp_path / "registers.json"
    f.write_text(_json.dumps(d))
    core.register = rcp.load_register(f)


def test_short_form_known_recipient_with_contact_then_from_room_history(tmp_path):
    core, svc, out = make()
    _register_without_contacts(core, tmp_path)
    run(core, "!labor Brandl maria-beispiel")                # register knows her, no contact yet in this room
    assert svc.calls == [] and out[-1] == ("Dr. Eva Brandl (Labor Kanalstraße) ist bekannt, aber noch ohne Kontakt in "
                                           "diesem Raum. Einmal mit Adresse: !labor Brandl maria-beispiel <E-Mail oder Mobilnummer>")
    run(core, "!labor Brandl maria-beispiel eva.brandl@labor-kanal.example")
    body = svc.calls[-1]
    assert body["dry_run"] is True and body["recipient"] == {"name": "Dr. Eva Brandl", "role": "Laborärztin",
                                                            "org": "Labor Kanalstraße"}
    assert body["channel"] == "email" and body["address"] == "eva.brandl@labor-kanal.example"
    assert body["doc_kind"] == "lab_result" and body["patient_id"] == "maria-beispiel"
    assert out[-1].startswith("Anforderung prüfen") and _no_service_patient_line(out[-1])   # same check card
    run(core, "!ja")
    assert svc.calls[-1]["confirm"] is True and out[-1].startswith("Anfrage gesendet")
    run(core, "!labor Brandl maria-beispiel")                # now from this room's history
    assert svc.calls[-1]["address"] == "eva.brandl@labor-kanal.example" and out[-1].startswith("Anforderung prüfen")

    async def other_room():                                  # history is per room
        await core.handle("!other:klinikum.example", LENA, "!labor Brandl maria-beispiel")
    asyncio.run(other_room())
    assert "noch ohne Kontakt" in out[-1]


def test_short_form_reha_sms_and_text_tail():
    core, svc, out = make()
    run(core, "!reha Kern maria-beispiel +43 660 000 0101, Text: Entlassungsbericht nach Hüft-TEP, bitte mit Medikation")
    b = svc.calls[-1]
    assert b["recipient"] == {"name": "Stefan Kern", "role": "Entlassungsmanagement", "org": "Reha Waldblick"}
    assert b["channel"] == "sms" and b["address"] == "+43 660 000 0101" and b["doc_kind"] == "discharge_report"
    assert b["request_text"] == "Entlassungsbericht nach Hüft-TEP, bitte mit Medikation"
    assert "Text an die Person" in out[-1]


def test_short_form_ambiguous_and_unknown_never_guess():
    core, svc, out = make()
    core.remember_recipient(ROOM, Draft(name="Dr. Max Brandl", role="Labormediziner", org="Labor Nord",
                                        channel="email", address="max.brandl@labor-nord.example"))
    run(core, "!labor Brandl maria-beispiel")
    assert svc.calls == [] and out[-1].startswith("'Brandl' ist nicht eindeutig: ")
    assert "Dr. Eva Brandl (Labor Kanalstraße)" in out[-1] and "Dr. Max Brandl (Labor Nord)" in out[-1]
    assert "!anfordern labor <Name>, <Rolle>, <Organisation>" in out[-1] and "\n" not in out[-1]   # one line
    run(core, "!labor Max Brandl maria-beispiel")            # given name disambiguates
    assert svc.calls[-1]["address"] == "max.brandl@labor-nord.example"
    run(core, "!labor Brandl Nord maria-beispiel")           # organisation word disambiguates
    assert svc.calls[-1]["recipient"]["org"] == "Labor Nord"
    n = len(svc.calls)
    run(core, "!labor Huber maria-beispiel")
    assert len(svc.calls) == n and out[-1].startswith("'Huber' ist weder im Register noch in früheren Anfragen")
    run(core, "!labor Brandel maria-beispiel")               # no fuzzy matching
    assert len(svc.calls) == n and "weder im Register" in out[-1]
    run(core, "!labor Hofer maria-beispiel")                 # network member, not an outside recipient
    assert len(svc.calls) == n and "weder im Register" in out[-1]


def test_short_form_usage_problems_and_long_form_passthrough():
    core, svc, out = make()
    run(core, "!labor")
    assert out[-1].startswith("Kurzform: !labor <Nachname> <Patienten-ID>")
    run(core, "!labor Brandl")                               # no patient id
    assert out[-1].startswith("Kurzform:") and svc.calls == []
    run(core, "!labor Brandl maria-beispiel +49 151 1234567")
    assert "keine österreichische Nummer" in out[-1] and "+49 151 1234567" in out[-1] and svc.calls == []
    run(core, "!labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient maria-beispiel")
    assert svc.calls[-1]["recipient"]["role"] == "Laborärztin"   # comma form still works after !labor
    run(core, "!lab Brandl maria-beispiel eva.brandl@labor-kanal.example")
    assert out[-1].startswith("Check request")


def test_warned_address_is_not_remembered(tmp_path):
    core, svc, out = make()
    _register_without_contacts(core, tmp_path)
    run(core, "!labor Brandl maria-beispiel eva.brandl@labor-kanaI.example", "!ja")
    assert out[-1].startswith("Anfrage gesendet") and core.recipients.get(ROOM) is None
    run(core, "!labor Brandl maria-beispiel")
    assert "noch ohne Kontakt" in out[-1]


def test_help_lists_short_form_first():
    core, svc, out = make()
    run(core, "!hilfe")
    assert out[-1].split("\n")[1].startswith("• !labor <Nachname> <Patienten-ID> · !reha <Nachname> <Patienten-ID>")
    run(core, "!help")
    assert out[-1].split("\n")[1].startswith("• !lab <family name> <patient id>")


def test_short_form_uses_register_contact_when_listed(tmp_path):
    import json as _json
    from bot import recipients as rcp
    f = tmp_path / "registers.json"
    f.write_text(_json.dumps({"gda_index": {"entries": [{"name": "Labor Kanalstraße", "staff": [
        {"name": "Dr. Eva Brandl", "role": "Laborärztin", "contact": "eva.brandl@labor-kanal.example"}]}]}}))
    core, svc, out = make()
    core.register = rcp.load_register(f)
    run(core, "!labor Brandl maria-beispiel")
    assert svc.calls[-1]["address"] == "eva.brandl@labor-kanal.example" and out[-1].startswith("Anforderung prüfen")

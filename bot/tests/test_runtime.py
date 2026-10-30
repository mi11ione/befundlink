"""Notify formatting + HTTP endpoint, sync processing and send retries with a fake nio client."""
import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from nio import RoomMessageText, RoomSendError, RoomSendResponse

from bot import fmt
from bot.config import Config, state_bot_token, state_room_id
from bot.runtime import BotRuntime

ROOM = "!room:klinikum.example"


# ------------------------------------------------------------------ formatting

def test_misdelivery_marked_and_service_text_verbatim():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "misdelivery", "request_id": "req-1",
                               "text": "Identität passt nicht zur angefragten Person",
                               "data": {"attempted_by": "Karl Kollege",
                                        "mismatches": [{"field": "name", "document": "OTHER PATIENT"}]}})
    assert t.startswith("Nicht abgelegt\nIdentität passt nicht") and "Versucht von: Karl Kollege" in t
    assert "OTHER PATIENT" not in t and "OTHER PATIENT" not in h      # never name the other patient
    svc_text = "[FEHLZUSTELLUNG ABGEFANGEN] Falscher Patient – Antwort wurde NICHT abgelegt."
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "misdelivery", "text": svc_text})
    assert t == "Fehlzustellung abgefangen\nFalscher Patient – Antwort wurde NICHT abgelegt."   # service words kept
    assert h.startswith('<p><font data-mx-color="#E30613"><b>Fehlzustellung abgefangen</b></font>') and "[" not in h
    assert fmt.notify_message({"room_id": ROOM, "text": svc_text}) == (svc_text, None)


def test_plain_passthrough_and_errors():
    assert fmt.notify_message({"room_id": ROOM, "text": "hi", "html": "<b>hi</b>"}) == ("hi", "<b>hi</b>")
    try:
        fmt.notify_message({"room_id": ROOM})
        raise AssertionError
    except ValueError:
        pass


def test_hold_one_decision():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "hold", "request_id": "req-1", "answer_id": "3",
                               "data": {"reason": "Geburtsdatum weicht ab (Tag/Monat vertauscht?)",
                                        "mismatches": [{"field": "birthDate", "document": "1961-12-03",
                                                        "request": "1961-03-12"}]}})
    assert t.startswith("Angehalten · Antwort\nGrund: Geburtsdatum weicht ab")
    assert "Geburtsdatum – Dokument: 03.12.1961 · Anfrage: 12.03.1961" in t
    assert "!ablegen 3 <Begründung> · !verwerfen 3 · !halten 3" in t
    assert t.count("Angehalten") == 1              # said once, in the headline


def test_filed_card_clinical_first_technical_last():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "request_id": "req-0000000000a1", "data": {
        "doc_kind": "lab_result", "patient": {"name": "Maria Beispiel", "birthDate": "1961-03-12"},
        "ref_code": "KB-7QX4MZ", "sample_time": "2026-10-22T08:10:00+02:00", "report_time": "2026-10-22T11:40:00+02:00",
        "received_at": "2026-10-29T17:31:12+01:00",
        "sender": "Dr. Eva Brandl, Labor Kanalstraße (ID Austria ✓, Ärzteliste ✓)",
        "validator_errors": 0, "values_matched": 4, "values_total": 4, "values_label": "Werte",
        "conclusion": "INR erhöht, Kontrolle empfohlen.", "einsender": "Dr. Karl Huber, Allgemeinmedizin",
        "observations": [{"test": "INR", "value": 1.38, "value_text": "1,38", "unit": "", "ref": "0,85–1,15", "flag": "+"},
                         {"test": "D-Dimer", "value": 0.27, "comparator": "<", "unit": "mg/l FEU", "ref": "< 0,50"},
                         {"test": "aPTT", "value": "31", "unit": "s", "ref": "25 to 37"}],
        "original_url": "http://localhost:8000/r/orig/1",
        "resources": [{"type": "DiagnosticReport", "id": "dr1", "url": "http://localhost:8090/fhir/DiagnosticReport/dr1"}]}})
    lines = t.split("\n")
    assert lines[0] == "Abgelegt · Laborbefund · Maria Beispiel · 12.03.1961 · Probe 22.10.2026, 08:10"
    assert lines[1] == "Von: Dr. Eva Brandl, Labor Kanalstraße (ID Austria ✓, Ärzteliste ✓)"
    assert lines[2:5] == ["  INR: 1,38 + (Ref. 0,85–1,15)", "  D-Dimer: <0.27 mg/l FEU (Ref. < 0,50)", "  aPTT: 31 s (Ref. 25 to 37)"]
    assert lines[5] == "Labor: „INR erhöht, Kontrolle empfohlen.“"
    assert lines[6] == "Einsender laut Dokument: Dr. Karl Huber, Allgemeinmedizin"
    assert "Befundet: 22.10.2026, 11:40" in t and "Eingegangen: 29.10.2026, 17:31" in t
    i_tech = next(i for i, l in enumerate(lines) if l.startswith("Geprüft: "))
    assert lines[i_tech] == "Geprüft: HL7-Validator: 0 Fehler · 4/4 Werte unverändert übernommen"
    assert i_tech > 6 and lines[-1] == "Anfrage: KB-7QX4MZ · !audit KB-7QX4MZ"
    assert 'href="http://localhost:8000/r/orig/1">Original<' in h


def test_filed_card_report_status_and_vienna_time():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "lab_result", "patient": "Maria Beispiel", "report_status": "preliminary",
        "status_note": "Fibrinogen folgt", "sample_time": "2026-10-28T07:05:00Z", "sender": "Dr. Eva Brandl"}})
    assert t.split("\n")[:3] == ["Abgelegt · Laborbefund · Maria Beispiel · Probe 28.10.2026, 08:05",
                                 "⚠️ VORLÄUFIG – Fibrinogen folgt", "Von: Dr. Eva Brandl"]
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "lab_result", "report_status": "corrected"}})
    assert "⚠️ KORRIGIERT" in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "lab_result", "report_status": "final",
                                                                          "sample_time": "22 October 2026, 08:10"}})
    assert "VORLÄUFIG" not in t and "KORRIGIERT" not in t and "Probe 22 October 2026, 08:10" in t


def test_no_result_card():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "no_result", "request_id": "req-0000000000a1", "data": {
        "doc_kind": "lab_result", "patient": {"name": "Maria Beispiel", "birthDate": "1961-03-12"},
        "sender": "Dr. Eva Brandl (ID Austria)", "comment": "Keine Gerinnung seit 2025 bestimmt.",
        "received_at": "2026-10-29T17:31:12+01:00", "ref_code": "KB-7QX4MZ"}})
    assert t.split("\n") == [
        "Kein Befund vorhanden · Laborbefund · Maria Beispiel · 12.03.1961", "von Dr. Eva Brandl",
        "Angabe: „Keine Gerinnung seit 2025 bestimmt.“", "Eingegangen: 29.10.2026, 17:31",
        "Anfrage: KB-7QX4MZ · !audit KB-7QX4MZ"]


def test_hold_card_german_reason_side_by_side_and_ref_id():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "17", "request_id": "req-0000000000ab", "data": {
        "reason": "KB-7QX4MZ: the date of birth is close but not identical", "ref_code": "KB-7QX4MZ",
        "reasons_de": ["Geburtsdatum fast gleich (Ziffer oder Tag/Monat vertauscht)"],
        "mismatches": [{"field": "birthDate", "document": "1961-12-03", "request": "1961-03-12"}],
        "matched": ["Name stimmt: Maria Beispiel"], "patient": {"name": "Maria Beispiel", "birthDate": "1961-03-12"},
        "sender": "Dr. Eva Brandl (ID Austria)", "doc_kind": "lab_result"}})
    assert t.split("\n") == [
        "Angehalten · Laborbefund · Maria Beispiel · 12.03.1961",
        "Grund: Geburtsdatum fast gleich (Ziffer oder Tag/Monat vertauscht).",
        "  Geburtsdatum – Dokument: 03.12.1961 · Anfrage: 12.03.1961",
        "  ✓ Name · von Dr. Eva Brandl",
        "Entscheidung: !ablegen KB-7QX4MZ <Begründung> · !verwerfen KB-7QX4MZ · !halten KB-7QX4MZ · !zeigen KB-7QX4MZ"]
    assert "<th>Feld</th><th>Dokument</th><th>Anfrage</th>" in h
    assert "<td><code><b>03</b>.<b>12</b>.1961</code></td><td><code>12.03.1961</code></td>" in h   # only the digits that differ
    assert h.count("KB-7QX4MZ") == 1 and "!ablegen" in h                # the reference once, the commands compact
    assert "the date of birth" not in t                     # English fallback not used when German is sent
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "3", "data": {"reason_text_de": "Keine Patientenzeile erkannt"}})
    assert "Grund: Keine Patientenzeile erkannt." in t


def test_filed_values_label_for_reports():
    base = {"doc_kind": "discharge_report", "patient": "Maria Beispiel", "values_matched": 12, "values_total": 12}
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {**base, "values_label": "Prüfpunkte"}})
    assert "12/12 Prüfpunkte unverändert übernommen" in t and "Werte" not in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": base})          # no label sent
    assert "12/12 Prüfpunkte unverändert übernommen" in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        **base, "doc_kind": "lab_result", "values_matched": 4, "values_total": 4, "values_label": "Werte"}})
    assert "4/4 Werte unverändert übernommen" in t


def test_hold_card_with_lane_b_payload_uses_bot_commands_only():
    """Exact shape the service sends (service/core.py _hold): its text/decision_options must not leak."""
    p = {"room_id": ROOM, "kind": "hold", "answer_id": "17", "request_id": "req-0000000000ab",
         "text": "[ANGEHALTEN – Entscheidung nötig] Antwort von X auf Anforderung KB-1 wurde NICHT abgelegt. "
                 "Grund: Geburtsdatum vertauscht? Entscheidung: halten / verwerfen / trotzdem ablegen (mit Begründung). Ref KB-1",
         "data": {"reason": "KB-1: Geburtsdatum vertauscht?", "status": "held_review",
                  "mismatches": [{"field": "birthDate", "document": "1961-12-03", "request": "1961-03-12"}],
                  "sender": "Dr. Eva Brandl (ID Austria)", "doc_kind": "lab_result", "request_id": "req-0000000000ab",
                  "answer_id": "17", "decision_options": "halten / verwerfen / trotzdem ablegen (mit Begründung)"}}
    t, h = fmt.notify_message(p)
    assert t.startswith("Angehalten · Laborbefund\nGrund: KB-1: Geburtsdatum vertauscht?")
    assert "von Dr. Eva Brandl" in t
    assert "Entscheidung: !ablegen 17 <Begründung> · !verwerfen 17 · !halten 17 · !zeigen 17" in t
    assert "trotzdem ablegen (mit Begründung)" not in t and "trotzdem ablegen" not in h
    assert "Geburtsdatum – Dokument: 03.12.1961 · Anfrage: 12.03.1961" in t


def test_audit_six_steps_chain_and_effort():
    data = {"chain": {"ok": True}, "entries": [
        {"step": "request sent", "outcome": "success", "who": "@lena.hofer:klinikum.example", "what": "E-Mail", "at": "2026-10-29T19:02:11+01:00"},
        {"step": "link opened", "outcome": "success", "who": "anonym", "what": "", "at": "2026-10-29T19:03:00+01:00"},
        {"step": "identity checked", "outcome": "failure", "who": "Karl Kollege", "what": "eID ≠ angefragte Person", "at": "2026-10-29T19:03:30+01:00"},
        {"step": "misdelivery caught", "outcome": "success", "who": "Karl Kollege", "what": "blockiert", "at": "2026-10-29T19:03:31+01:00"},
        {"step": "identity checked", "outcome": "success", "who": "Dr. Eva Brandl", "what": "LoA high", "at": "2026-10-29T19:03:50+01:00"},
        {"step": "answer sent", "outcome": "success", "who": "Dr. Eva Brandl", "what": "upload", "at": "2026-10-29T19:04:12+01:00"},
        {"step": "answer filed", "outcome": "success", "who": "pgk-bridge", "what": "DiagnosticReport/dr1", "at": "2026-10-29T19:04:13+01:00"}]}
    t, h = fmt.audit_message("req-1", data)
    assert "6/6 Schritte: ✓ Anfrage gesendet ✓ Link geöffnet ✓ Identität geprüft ✓ Antwort gesendet ✓ Antwort abgelegt ✓ Fehlzustellung erkannt" in t
    assert "Hash-Kette: intakt ✓" in t
    assert "Aufwand der Person: 1 min 12 s (Link geöffnet → Antwort gesendet)" in t
    assert "⚠️ 29.10. 19:03:30 · Identität geprüft · Karl Kollege · eID ≠ angefragte Person" in t
    assert "Hash-Kette: GEBROCHEN ‼ gap at #4" in fmt.audit_message("r", {**data, "chain": {"ok": False, "detail": "gap at #4"}})[0]
    bundle = {"resourceType": "Bundle", "entry": [{"resource": {
        "resourceType": "AuditEvent", "recorded": "2026-10-29T19:03:00Z", "outcome": "0",
        "subtype": [{"code": "identity-checked"}], "agent": [{"who": {"display": "Dr. Eva Brandl"}}],
        "outcomeDesc": "eID LoA high"}}]}
    assert "✓ 29.10. 19:03:00 · Identität geprüft · Dr. Eva Brandl · eID LoA high" in fmt.audit_message("r", bundle)[0]


def test_state_json_layout_of_lane_a():
    st = {"room_id": "!a:klinikum.example", "users": {"bot": {"user_id": "@bridge-bot:klinikum.example"}},
          "tokens": {"lena": "L", "bot": "B"}}
    assert state_bot_token(st) == "B" and state_room_id(st) == "!a:klinikum.example"


# ------------------------------------------------------------------ runtime with a fake client

class FakeClient:
    def __init__(self, fail_first=0):
        self.user_id = "@bridge-bot:klinikum.example"
        self.sent, self.joined, self.left, self.fail_first = [], [], [], fail_first

    async def room_send(self, room_id, mtype, content, tx_id=None):
        if self.fail_first:
            self.fail_first -= 1
            return RoomSendError("rate limited", "M_LIMIT_EXCEEDED", retry_after_ms=10)
        self.sent.append((room_id, content, tx_id))
        return RoomSendResponse(f"$ev{len(self.sent)}", room_id)

    async def join(self, room_id):
        self.joined.append(room_id)

    async def room_leave(self, room_id):
        self.left.append(room_id)

    async def close(self):
        pass


def _rt(tmp_path, client):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.state_json = tmp_path / "nope.json"
    cfg.access_token = None
    cfg.default_room = ROOM
    cfg.notify_key = "test-notify-key"
    rt = BotRuntime(cfg, service=SimpleNamespace(close=lambda: None))
    rt.client = client
    rt.logged_in.set()
    return rt


def _msg(sender, body, eid, ts=None):
    import time
    return RoomMessageText.from_dict({"type": "m.room.message", "sender": sender, "event_id": eid,
                                      "origin_server_ts": ts or int(time.time() * 1000), "content": {"msgtype": "m.text", "body": body}})


def _invite(inviter):
    from nio import InviteMemberEvent
    ev = InviteMemberEvent.from_dict({"type": "m.room.member", "sender": inviter, "state_key": "@bridge-bot:klinikum.example",
                                      "content": {"membership": "invite"}})
    return SimpleNamespace(invite_state=[ev])


def _sync(events, invites=None):
    return SimpleNamespace(rooms=SimpleNamespace(
        invite=invites or {},
        join={ROOM: SimpleNamespace(timeline=SimpleNamespace(events=events))}), next_batch="s2")


def test_send_retries_on_rate_limit_with_same_txn(tmp_path):
    async def go():
        rt = _rt(tmp_path, FakeClient(fail_first=2))
        worker = asyncio.create_task(rt._sender())
        eid = await rt.send(ROOM, "hallo", "<b>hallo</b>")
        worker.cancel()
        return rt, eid
    rt, eid = asyncio.run(go())
    assert eid == "$ev1" and len(rt.client.sent) == 1
    assert rt.client.sent[0][1]["formatted_body"] == "<b>hallo</b>"


def test_process_sync_ignores_own_history_and_dupes(tmp_path):
    seen = []

    async def go():
        rt = _rt(tmp_path, FakeClient())

        async def handle(room, sender, body):
            seen.append((sender, body))
        rt.core.handle = handle
        await rt.process_sync(_sync([_msg("@lena.hofer:klinikum.example", "!hilfe", "$old")],
                                    invites={"!inv:x": _invite("@lena.hofer:klinikum.example"),
                                             "!evil:x": _invite("@mallory:evil.example")}),
                              initial=True)                                   # history: not replayed
        await rt.process_sync(_sync([_msg("@bridge-bot:klinikum.example", "!hilfe", "$own"),
                                     _msg("@lena.hofer:klinikum.example", "!hilfe", "$new"),
                                     _msg("@lena.hofer:klinikum.example", "!hilfe", "$new"),
                                     _msg("@lena.hofer:klinikum.example", "!alt", "$stale", ts=1)]),  # cached-sync replay
                              initial=False)
        return rt
    rt = asyncio.run(go())
    assert seen == [("@lena.hofer:klinikum.example", "!hilfe")]
    assert rt.client.joined == ["!inv:x"] and rt.client.left == ["!evil:x"]   # invite allow-list


def test_sync_token_persisted(tmp_path):
    rt = _rt(tmp_path, FakeClient())
    rt.next_batch = "s42"
    rt.core.last_request[ROOM] = "req-000000000009"
    rt._save()
    rt2 = _rt(tmp_path, FakeClient())
    assert rt2.next_batch == "s42" and rt2.core.last_request[ROOM] == "req-000000000009"


def test_notify_endpoint_auth_content_type_and_room_allowlist(tmp_path):
    K = {"X-PGK-Notify-Key": "test-notify-key"}

    async def go():
        rt = _rt(tmp_path, FakeClient())
        worker = asyncio.create_task(rt._sender())
        res = {}
        async with TestClient(TestServer(rt.make_app())) as c:
            async def st(name, **kw):
                r = await c.post("/notify", **kw)
                res[name] = (r.status, await r.json())
            await st("ok", json={"room_id": ROOM, "text": "Antwort abgelegt"}, headers=K)
            await st("no_key", json={"room_id": ROOM, "text": "x"})
            await st("bad_key", json={"room_id": ROOM, "text": "x"}, headers={"X-PGK-Notify-Key": "nope"})
            await st("text_plain", data='{"room_id": "%s", "text": "x"}' % ROOM,
                     headers={**K, "Content-Type": "text/plain"})                    # CSRF-style simple request
            await st("form", data={"room_id": ROOM, "text": "x"}, headers=K)
            await st("not_json_obj", data="[1]", headers={**K, "Content-Type": "application/json"})
            await st("default_room", json={"text": "ohne room_id"}, headers=K)
            await st("foreign_room", json={"room_id": "!other:evil.example", "text": "x"}, headers=K)
            rt.core.note_request("!req-room:klinikum.example", "req-0000000000aa")
            await st("request_room", json={"room_id": "!req-room:klinikum.example", "text": "x"}, headers=K)
            await st("hold", json={"room_id": ROOM, "kind": "hold", "answer_id": "1",
                                   "data": {"reason": "Patientenzeile fehlt"}}, headers=K)
            rt.cfg.notify_key = None
            await st("unconfigured", json={"room_id": ROOM, "text": "x"}, headers=K)
        worker.cancel()
        return rt, res
    rt, r = asyncio.run(go())
    assert r["ok"][0] == 200 and r["ok"][1]["event_id"] == "$ev1"
    assert r["no_key"][0] == 401 and r["bad_key"][0] == 401
    assert r["text_plain"][0] == 415 and r["form"][0] == 415 and r["not_json_obj"][0] == 400
    assert r["default_room"][0] == 200 and r["foreign_room"][0] == 403 and r["request_room"][0] == 200
    assert r["hold"][0] == 200 and rt.core.holds[ROOM] == ["1"] and r["unconfigured"][0] == 503
    assert len(rt.client.sent) == 4 and all(c["msgtype"] == "m.text" for _, c, _ in rt.client.sent)


def test_every_message_has_empty_mentions_no_room_ping_and_no_autolink(tmp_path):
    async def go():
        rt = _rt(tmp_path, FakeClient())
        worker = asyncio.create_task(rt._sender())
        await rt.send(ROOM, "Hallo @room, siehe https://evil.example/x und evil.example")
        await rt.send(ROOM, "x", "<p>@room</p>")
        worker.cancel()
        return rt
    rt = asyncio.run(go())
    (_, c1, _), (_, c2, _) = rt.client.sent
    assert c1["m.mentions"] == {} and c2["m.mentions"] == {}
    assert "@room" not in c1["body"] and "@room" not in c1["formatted_body"] and "@room" not in c2["formatted_body"]
    assert "<code>https://evil.example/x</code>" in c1["formatted_body"] and "<code>evil.example</code>" in c1["formatted_body"]


def test_document_text_in_cards_is_not_linkified():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "lab_result", "observations": [{"test": "INR siehe http://phish.example/login", "value": "1.0"}],
        "summary": "Rückfragen an evil@phish.example @room"}})
    assert "<code>http://phish.example/login</code>" in h and "<code>evil@phish.example</code>" in h
    assert "@room" not in h and "<a " not in h
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "4",
                               "data": {"reason": "Dokument verweist auf www.phish.example"}})
    assert "<code>www.phish.example</code>" in h
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "misdelivery", "text": "x http://a.example",
                               "html": "<a href='http://a.example'>klick</a>"})
    assert "<a " not in h and "<code>http://a.example</code>" in h       # service html not trusted for alerts


def test_hold_with_invalid_id_shows_placeholder():
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "../../x", "data": {"reason": "r"}})
    assert "../../x" not in t and "!ablegen <id>" in t


def test_sync_loop_survives_errors_and_relogs_on_unknown_token(tmp_path):
    from nio import SyncError

    class C(FakeClient):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def sync(self, **kw):
            await asyncio.sleep(0.01)          # a real long-poll yields to the loop
            self.n += 1
            if self.n == 1:
                raise ConnectionError("synapse down")
            if self.n == 2:
                return SyncError("token gone", "M_UNKNOWN_TOKEN")
            return _sync([])

    async def go():
        rt = _rt(tmp_path, C())
        logins = []

        async def login():
            logins.append(1)
        rt.login = login
        task = asyncio.create_task(rt.sync_loop())
        await asyncio.wait_for(rt.synced.wait(), 10)
        task.cancel()
        return rt, logins
    rt, logins = asyncio.run(go())
    assert rt.client.n >= 3 and logins == [1] and rt.next_batch == "s2"


def test_locked_link_alert_mentions_unlock_command():
    lane_b = ("[FEHLZUSTELLUNG ABGEFANGEN] Anforderung an Dr. Eva Brandl (KB-AB12CD): Zugriff von einer anderen Person "
              "(Karl Kollege) blockiert. Versucht von Karl Kollege (ID Austria) über den Link an x@y.example. "
              "Es wurden keine Patientendaten angezeigt. Der Link ist jetzt gesperrt.")
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "misdelivery", "request_id": "req-0000000000aa", "text": lane_b})
    assert t.startswith("Fehlzustellung abgefangen\nZugriff von einer anderen Person (Karl Kollege) blockiert.")
    assert "\nAnforderung an Dr. Eva Brandl (KB-AB12CD)\n" in t       # the service's words, marker -> headline
    assert "Nur für einen neuen Versuch der angefragten Person: !freigeben req-0000000000aa" in t
    assert "!freigeben req-0000000000aa" in h
    still_valid = lane_b.replace("Der Link ist jetzt gesperrt.", "Der Link bleibt für Dr. Eva Brandl gültig.")
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "misdelivery", "request_id": "req-0000000000aa", "text": still_valid})
    assert "!freigeben" not in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "misdelivery", "request_id": "bad id", "text": "x",
                               "data": {"locked": True}})
    assert "!freigeben <id>" in t


def test_confusable_words():
    assert fmt.confusable_words("labor-kanaI.example", "labor-kanal.example") == \
        "großes I statt kleinem l (labor-kanaI.example → labor-kanal.example)"
    assert fmt.confusable_words("labor-kana1.example", "labor-kanal.example").startswith("Ziffer 1 statt kleinem l")
    assert fmt.confusable_words("lab0r.example", "labor.example").startswith("Ziffer 0 statt kleinem o")
    assert fmt.confusable_words("labor-kanaI.example", "labor-kanaI.example") is None
    assert "'rn' statt 'm'" in fmt.confusable_words("klinikurn.example", "klinikum.example")
    assert "CYRILLIC SMALL LETTER A" in fmt.confusable_words("lаbor.example", "labor.example").upper()


def test_misdelivery_uses_ref_code_as_the_one_id():
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "misdelivery", "request_id": "req-0000000000aa",
                               "ref_code": "KB-AB12CD", "text": "[FEHLZUSTELLUNG ABGEFANGEN] … Der Link ist jetzt gesperrt."})
    assert "!freigeben KB-AB12CD" in t and "Protokoll: !audit KB-AB12CD" in t and "req-" not in t


def test_identity_confirm_card_compact_and_degrading():
    full = {"recipient": "Dr. Eva Brandl", "ref_code": "KB-AB12CD", "new_birth_year": 1955, "known_answers": 3,
            "known_since": "2026-10-22", "birth_year_differs": True, "via_contact": "e***@freemail.example"}
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "identity_confirm", "data": full})
    assert t.split("\n") == [
        "Neue ID-Austria-Identität für Dr. Eva Brandl (KB-AB12CD): Jahrgang 1955 – bisher hat eine andere Person "
        "geantwortet (3 Antworten seit 22.10.2026, anderer Jahrgang). Kontakt: e***@freemail.example.",
        "Der neuen Person wurde nichts angezeigt; nichts abgelegt.",
        "Freigeben? !identität KB-AB12CD ja · nein"]
    assert "<code>e***@freemail.example</code>" in h
    assert "<b>Freigeben?</b><br/><b>!identität KB-AB12CD ja</b> zulassen<br/><b>!identität KB-AB12CD nein</b> ablehnen" in h
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "identity_confirm", "data": {"recipient": "Dr. Eva Brandl",
                                                                                     "ref_code": "KB-AB12CD"}})
    assert t.split("\n")[0] == ("Neue ID-Austria-Identität für Dr. Eva Brandl (KB-AB12CD) – "
                                "bisher hat eine andere Person geantwortet.")
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "identity_confirm", "data": {
        "recipient": "Dr. Eva Brandl", "ref_code": "KB-AB12CD", "known_answers": 1, "birth_year_differs": False}})
    assert "(1 Antwort, gleicher Jahrgang)." in t and "Jahrgang 19" not in t and "Kontakt" not in t


def test_hold_card_flags_other_person_without_naming_her():
    base = {"room_id": ROOM, "kind": "hold", "answer_id": "5"}
    for data in ({"details": {"other_person_present": True}, "reason_text_de": "Mehrere Patienten im Dokument"},
                 {"codes": ["OTHER_PATIENT_PRESENT"]}, {"reasons": ["OTHER_PATIENT_PRESENT: second person"]},
                 {"reason_codes": [{"code": "other_patient_present"}]}):
        t, h = fmt.notify_message({**base, "data": data})
        assert t.split("\n")[1] == "⚠️ Das Dokument enthält Angaben zu einer weiteren Person", data
        assert '<font data-mx-color="#E30613"><b>Das Dokument enthält Angaben zu einer weiteren Person</b></font>' in h
    for codes in (["NAME_NEAR"], ["MULTIPLE_REPORTS"]):        # two reports for the same patient: no second person
        t, _ = fmt.notify_message({**base, "data": {"codes": codes}})
        assert "weiteren Person" not in t


def test_identity_hold_registered_from_notify(tmp_path):
    K = {"X-PGK-Notify-Key": "test-notify-key"}

    async def go():
        rt = _rt(tmp_path, FakeClient())
        worker = asyncio.create_task(rt._sender())
        async with TestClient(TestServer(rt.make_app())) as c:
            r = await c.post("/notify", json={"room_id": ROOM, "kind": "identity_confirm", "ref_code": "KB-AB12CD",
                                              "data": {"recipient": "Dr. Eva Brandl"}}, headers=K)
            st = r.status
        worker.cancel()
        return rt, st
    rt, st = asyncio.run(go())
    assert st == 200 and rt.core.identity_holds[ROOM] == ["KB-AB12CD"] and rt.core.request_rooms["KB-AB12CD"] == ROOM


def test_filed_card_round3_fixes():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "lab_result", "patient": {"name": "Maria Beispiel", "birthDate": "1961-03-12"},
        "report_status": "corrected", "status_note": "Status laut Labor: corrected", "replaces": "Befund vom 27.10.2026 10:20",
        "conclusion": "Kommentar: Quick erniedrigt, aPTT verlängert.",
        "summary": "Kommentar des Labors: Kommentar: Quick erniedrigt, aPTT verlängert. · Autor laut Dokument: Dr. X",
        "observations": [{"test": "Fibrinogen", "value_text": "4,6*", "unit": "g/l", "flag": "*", "ref": "1,8 - 3,9"},
                         {"test": "INR", "value_text": "1,3 +", "flag": "+", "ref": "0,8 - 1,2"},
                         {"test": "Fibrinogen2", "value_text": "folgt", "unit": "g/l"},
                         {"test": "Kalium", "value_text": "nicht bestimmbar (hämolytisch)", "unit": "mmol/l"},
                         {"test": "aPTT", "value_text": "41,2", "unit": "sek", "flag": "+"}],
        "original_url": "http://localhost:8090/fhir/DiagnosticReport/2346",
        "fhir_url": "http://localhost:8090/fhir/DiagnosticReport/2346",
        "resources": [{"type": "DiagnosticReport", "id": "2346", "url": "http://localhost:8090/fhir/DiagnosticReport/2346"},
                      {"type": "Provenance", "id": "9", "url": "http://localhost:8090/fhir/Provenance/9"}]}})
    assert "⚠️ KORRIGIERT – ersetzt Befund: Befund vom 27.10.2026 10:20" in t and "corrected" not in t
    assert "  Fibrinogen: 4,6 g/l * (Ref. 1,8 - 3,9)" in t and "  INR: 1,3 + (Ref. 0,8 - 1,2)" in t
    assert "  Fibrinogen2: folgt\n" in t and "  Kalium: nicht bestimmbar (hämolytisch)\n" in t
    assert "  aPTT: 41,2 sek +\n" in t
    assert t.count("Quick erniedrigt") == 1 and "Labor: „Quick erniedrigt, aPTT verlängert.“" in t
    assert "Kommentar:" not in t and "Autor laut Dokument: Dr. X" in t
    assert t.count("DiagnosticReport/2346") == 1 and "Original" not in t
    assert "Quelle (FHIR): http://localhost:8090/fhir/DiagnosticReport/2346 · Provenance/9:" in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "lab_result", "report_status": "preliminary",
                                                                          "status_note": "Fibrinogen folgt, preliminary"}})
    assert "⚠️ VORLÄUFIG – Fibrinogen folgt, vorläufig" in t
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "lab_result",
                                                                          "original_url": "http://localhost:8000/doc/abc.md"}})
    assert ">Original<" in h


def test_rehab_card_stay_and_recommendations():
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "discharge_report", "patient": "Maria Beispiel", "sender": "Stefan Kern",
        "stay": {"start": "2026-09-02", "end": "2026-09-23", "text": "2 to 23 September 2026"},
        "sections": {"Course": "…", "Recommendations": "- Continue the home exercises.\n- Outpatient physiotherapy twice a week."},
        "values_matched": 12, "values_total": 12, "values_label": "Prüfpunkte"}})
    assert "Aufenthalt: 02.09.2026 – 23.09.2026" in t
    assert "Empfehlung: Continue the home exercises. · Outpatient physiotherapy twice a week." in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "discharge_report", "stay": {"start": None, "end": None, "text": "September 2026"},
        "recommendations": ["a", "b", "c", "d"]}})
    assert "Aufenthalt: September 2026" in t and "Empfehlung: a · b · c …" in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "discharge_report"}})
    assert "Aufenthalt" not in t and "Empfehlung" not in t


def test_hold_card_round3_fixes():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "9", "data": {
        "ref_code": "KB-T2N6GW", "reasons_de": ["Geschlecht passt nicht.", "Absender weicht ab."],
        "reason_text_de": "Geschlecht passt nicht.; Absender weicht ab.",
        "mismatches": [{"field": "gender", "document": "male", "request": "female"}],
        "matched": ["✓ Name", "✓ Absender per ID Austria (Simulation) geprüft"],
        "sender": "Eva Brandl (ID Austria – Simulation)", "sender_line": "Andrea Moser, Sekretariat",
        "codes": ["SENDER_CONTRADICTS_EID", "CORRECTION_REVIEW"], "replaces": {"date": "2026-10-27T10:20:00+01:00"}}})
    assert "Grund: Geschlecht passt nicht. Absender weicht ab." in t and ".;" not in t
    assert "  Geschlecht – Dokument: männlich · Anfrage: weiblich" in t and "gender" not in t
    assert "  ✓ Name · ✓ ID Austria: Eva Brandl\n" in t and "✓ ✓" not in t and "✓ ✓" not in h
    assert "  Absender laut Dokument: Andrea Moser, Sekretariat" in t
    assert "  Ersetzt laut Dokument: 27.10.2026, 10:20" in t
    assert "<td>Geschlecht</td><td><b>männlich</b></td><td>weiblich</td>" in h      # the differing value bold
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "9", "data": {
        "ref_code": "KB-T2N6GW", "codes": ["KIND_UNKNOWN"], "reasons_de": ["Dokumenttyp nicht erkannt"],
        "first_sentence": "Kein Gerinnungsbefund in den letzten 12 Monaten. Mit freundlichen Grüßen"}})
    assert "  Dokument beginnt mit: „Kein Gerinnungsbefund in den letzten 12 Monaten.“" in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "9", "data": {
        "codes": ["KIND_UNKNOWN", "OTHER_PATIENT_PRESENT"], "first_sentence": "Johann Muster INR 3,4"}})
    assert "Johann" not in t and "weiteren Person" in t        # never quote a document with another person


def test_preview_message_is_code_block():
    t, h = fmt.preview_message("KB-T2N6GW", {"text": "# Labor\nPatient: Maria\n<script>x</script> http://evil.example @room ```"})
    assert t.startswith("📄 Angehaltenes Dokument KB-T2N6GW – nur zur Ansicht, nicht abgelegt:\n```\n# Labor")
    assert t.count("```") == 2
    assert "<pre><code># Labor\nPatient: Maria\n&lt;script&gt;x&lt;/script&gt; http://evil.example" in h
    assert "<a " not in h
    t, _ = fmt.preview_message("KB-T2N6GW", "x" * 7000)
    assert t.endswith("(gekürzt)") and t.count("x") == fmt.PREVIEW_MAX


def test_charset_note_on_filed_and_hold_cards():
    note = "Zeichensatz umgewandelt (Windows-1252)"
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "lab_result", "charset_note": note, "validator_errors": 0, "received_at": "2026-10-29T17:31:12+01:00"}})
    lines = t.split("\n")
    i = lines.index(f"Hinweis: {note}")
    assert lines[i - 1].startswith("Eingegangen:") and lines[i + 1].startswith("Geprüft: ") and f"<p>Hinweis: {note}</p>" in h
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "4", "data": {
        "reasons_de": ["Messwert ohne Einheit"], "charset_note": note}})
    lines = t.split("\n")
    assert lines[1] == "Grund: Messwert ohne Einheit." and lines[2] == f"  Hinweis: {note}" and f"<p>Hinweis: {note}</p>" in h
    for kind in ("filed", "hold"):
        t, _ = fmt.notify_message({"room_id": ROOM, "kind": kind, "answer_id": "4", "data": {"doc_kind": "lab_result"}})
        assert "Zeichensatz" not in t and "Hinweis" not in t


def test_correction_hold_names_the_prior_report_and_no_empty_rows():
    base = {"room_id": ROOM, "kind": "hold", "answer_id": "7"}
    data = {"ref_code": "KB-YDR54P", "codes": ["CORRECTION_REVIEW"],
            "reasons_de": ["Korrigierter Befund: er ersetzt einen bereits abgelegten Befund."],
            "replaces": {"text": "28.10.2026, 14:37", "date": "28.10.2026, 14:37"},
            "replaces_ref": "KB-D446FF", "replaces_sample": "28.10.2026, 14:37", "prior_matches": 1,
            "mismatches": [{"field": "replaces", "document": "", "request": ""}]}
    t, h = fmt.notify_message({**base, "data": data})
    assert "  Ersetzt: KB-D446FF (Probe 28.10.2026, 14:37)" in t
    assert "replaces" not in t and "Ersetzt laut Dokument" not in t and t.count("14:37") == 1
    assert "<table>" not in h                                           # no empty table
    t, _ = fmt.notify_message({**base, "data": {**data, "prior_matches": 2, "replaces_ref": None}})
    assert "  ⚠️ 2 frühere Befunde passen – nicht eindeutig" in t and "Ersetzt:" not in t
    t, _ = fmt.notify_message({**base, "data": {"codes": ["CORRECTION_REVIEW"], "replaces": {"text": "x 1", "date": "x 1"}}})
    assert "Ersetzt laut Dokument: x 1\n" in t                          # legacy fallback, deduplicated
    t, _ = fmt.notify_message({**base, "data": {"codes": ["CORRECTION_REVIEW"], "prior_matches": 0}})
    assert "Ursprungsbefund nicht gefunden" in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "lab_result", "report_status": "corrected", "replaces_ref": "KB-D446FF", "replaces_sample": "28.10.2026, 14:37"}})
    assert "⚠️ KORRIGIERT – ersetzt Befund: KB-D446FF (Probe 28.10.2026, 14:37)" in t


def test_hold_card_states_each_fact_once():
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "8", "data": {
        "ref_code": "KB-C37C37", "sender": "Eva Brandl (ID Austria – Simulation)",
        "reasons_de": ["Das Dokument nennt einen anderen Absender: Gesendet von: Andrea Moser, Sekretariat."],
        "sender_line": "Andrea Moser, Sekretariat", "author": "Dr. Johanna Leitner, Ärztliche Leiterin",
        "mismatches": [{"field": "sender", "document": "Andrea Moser, Sekretariat", "request": "Eva Brandl"},
                       {"field": "text", "document": "irgendwas", "request": "anders"},
                       {"field": "birthDate", "document": "1991-03-12", "request": "1961-03-12"}]}})
    assert t.count("Andrea Moser") == 1                                 # only in the reason
    assert "Absender –" not in t and "text –" not in t and "Absender laut Dokument" not in t
    assert "  Autor laut Dokument: Dr. Johanna Leitner\n" in t and "Ärztliche Leiterin" not in t   # the name only
    assert "  Geburtsdatum – Dokument: 12.03.1991 · Anfrage: 12.03.1961" in t     # not in the reason: kept
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "8", "data": {
        "reasons_de": ["Geburtsdatum ähnlich (Geburtsdatum: Dokument 12.03.1991 · Anfrage 12.03.1961)."],
        "mismatches": [{"field": "birthDate", "document": "1991-03-12", "request": "1961-03-12"}]}})
    assert t.count("12.03.1991") == 1


def test_discard_text_humanized_with_display_name_and_german_date(tmp_path):
    svc_text = ("Anforderung KB-T2N6GW: Antwort verworfen und gelöscht (Entscheidung @lena.hofer:klinikum.example). "
                "Die Anforderung bleibt offen: Dr. Eva Brandl kann bis 2026-11-06 ein anderes Dokument senden.")
    assert fmt.humanize(svc_text, {"@lena.hofer:klinikum.example": "Dr. Lena Hofer"}) == (
        "Anforderung KB-T2N6GW: Antwort verworfen und gelöscht (Entscheidung Dr. Lena Hofer). "
        "Die Anforderung bleibt offen: Dr. Eva Brandl kann bis 06.11.2026 ein anderes Dokument senden.")
    assert fmt.humanize("Link http://x.example/2026-11-06/a bleibt", {}) == "Link http://x.example/2026-11-06/a bleibt"
    K = {"X-PGK-Notify-Key": "test-notify-key"}

    class Named(FakeClient):
        async def get_displayname(self, mx):
            self.lookups = getattr(self, "lookups", 0) + 1
            return SimpleNamespace(displayname={"@lena.hofer:klinikum.example": "Dr. Lena Hofer"}.get(mx))

    async def go():
        rt = _rt(tmp_path, Named())
        worker = asyncio.create_task(rt._sender())
        async with TestClient(TestServer(rt.make_app())) as c:
            await c.post("/notify", json={"room_id": ROOM, "text": svc_text}, headers=K)                 # profile lookup
            await c.post("/notify", json={"room_id": ROOM, "text": svc_text}, headers=K)                 # cached
            await c.post("/notify", json={"room_id": ROOM, "text": "Entscheidung @x.y:klinikum.example",
                                          "data": {"requester_display": "Dr. X Y"}}, headers=K)
        worker.cancel()
        return rt
    rt = asyncio.run(go())
    (_, c1, _), (_, c2, _), (_, c3, _) = rt.client.sent
    assert "Entscheidung Dr. Lena Hofer" in c1["body"] and "06.11.2026" in c1["body"] and "@lena" not in c1["formatted_body"]
    assert c2["body"] == c1["body"] and rt.client.lookups == 1
    assert c3["body"] == "Entscheidung Dr. X Y"


def test_rehab_card_leads_with_discharge_medication():
    meds = ["Rivaroxaban 10 mg once a day until 29 September 2026, to prevent thrombosis <b>x</b>",
            "Paracetamol 500 mg up to three times a day if needed", "Pantoprazole 20 mg once a day",
            "d", "e", "f", "g"]
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "discharge_report", "patient": {"name": "Maria Beispiel", "birthDate": "1961-03-12"},
        "sender": "Stefan Kern (ID Austria)", "medications": meds,
        "stay": {"start": "2026-09-02", "end": "2026-09-23"}, "recommendations": ["Continue the home exercises."]}})
    lines = t.split("\n")
    assert lines[1] == "💊 Entlassungsmedikation (laut Bericht):"
    assert lines[2] == "  • " + meds[0] and lines[7] == "  • f" and "  • g" not in t       # max 6
    assert lines[8] == "Von: Stefan Kern (ID Austria)"
    assert t.index("💊") < t.index("Aufenthalt:") < t.index("Empfehlung:")
    assert "&lt;b&gt;x&lt;/b&gt;" in h and "<b>x</b>" not in h
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "discharge_report", "report_status": "preliminary", "card_facts": {"medications": ["A 1 mg"]}}})
    assert t.split("\n")[1:4] == ["⚠️ VORLÄUFIG", "💊 Entlassungsmedikation (laut Bericht):", "  • A 1 mg"]
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "lab_result", "medications": ["A"]}})
    assert "💊" not in t


def test_demo_pin_line_only_when_sent():
    from bot.parse import parse_request_args
    d = parse_request_args("labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient maria-beispiel")
    resp = {"request_id": "req-0000000000a1", "ref_code": "KB-AB12CD", "status": "sent"}
    t, _ = fmt.sent_message(d, {**resp, "demo_identity": {"name": "Dr. Eva Brandl", "pin": "4711"}})
    assert t.split("\n")[-1] == "Demo-PIN für Dr. Eva Brandl: 4711 (an die Person weitergeben)"
    assert "Demo-PIN" not in fmt.sent_message(d, resp)[0]
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "sent", "text": "Anforderung KB-AB12CD gesendet.",
                               "data": {"demo_identity": {"name": "Dr. Eva Brandl", "pin": "4711"}}})
    assert t.endswith("Demo-PIN für Dr. Eva Brandl: 4711 (an die Person weitergeben)")
    assert fmt.notify_message({"room_id": ROOM, "text": "x"}) == ("x", None)


def test_audit_room_from_state_is_allowed_for_notify(tmp_path):
    import json as _json
    st = tmp_path / "state.json"
    st.write_text(_json.dumps({"room_id": "!clin:klinikum.example", "audit_room_id": "!audit:klinikum.example"}))
    cfg = Config()
    cfg.state_json, cfg.default_room = st, None
    cfg.refresh_from_state()
    assert {"!clin:klinikum.example", "!audit:klinikum.example"} <= set(cfg.allowed_rooms)
    K = {"X-PGK-Notify-Key": "test-notify-key"}

    async def go():
        rt = _rt(tmp_path, FakeClient())
        rt.cfg.state_json = st
        worker = asyncio.create_task(rt._sender())
        async with TestClient(TestServer(rt.make_app())) as c:
            ok = await c.post("/notify", json={"room_id": "!audit:klinikum.example", "text": "Audit-Anker #705"}, headers=K)
            bad = await c.post("/notify", json={"room_id": "!nope:klinikum.example", "text": "x"}, headers=K)
            res = (ok.status, bad.status)
        worker.cancel()
        return res
    assert asyncio.run(go()) == (200, 403)


def test_audit_clean_request_is_5_of_5_and_caught_is_6_of_6():
    ev = lambda step, at: {"step": step, "outcome": "success", "who": "x", "what": "", "at": f"2026-10-29T19:0{at}:00+01:00"}
    clean = [ev("request sent", 1), ev("link opened", 2), ev("identity checked", 3), ev("answer sent", 4), ev("answer filed", 5)]
    t, _ = fmt.audit_message("KB-AB12CD", {"entries": clean})
    assert t.split("\n")[1] == ("✓ 5/5 Schritte (keine Fehlzustellung): ✓ Anfrage gesendet "
                                "✓ Link geöffnet ✓ Identität geprüft ✓ Antwort gesendet ✓ Antwort abgelegt")
    assert "○" not in t and "Fehlzustellung erkannt" not in t
    t, _ = fmt.audit_message("KB-AB12CD", {"entries": clean + [ev("misdelivery caught", 6)]})
    assert t.split("\n")[1].startswith("✓ 6/6 Schritte: ✓ Anfrage gesendet") and "✓ Fehlzustellung erkannt" in t
    t, _ = fmt.audit_message("KB-AB12CD", {"entries": clean[:2] + [ev("misdelivery caught", 6)]})
    assert t.split("\n")[1].startswith("3/6 Schritte: ✓ Anfrage gesendet ✓ Link geöffnet ○ Identität geprüft")


def test_request_sent_notify_suppressed_when_bot_posted_the_card(tmp_path):
    K = {"X-PGK-Notify-Key": "test-notify-key"}
    pin = {"name": "Dr. Jakob Steiner", "pin": "4711"}

    def payload(ref, rid):
        return {"room_id": ROOM, "kind": "request_sent", "request_id": rid,
                "text": f"Anforderung {ref} an Dr. Jakob Steiner gesendet. Demo: Für diese Person wurde …",
                "data": {"ref_code": ref, "recipient": "Dr. Jakob Steiner", "demo_identity": pin}}

    async def go():
        rt = _rt(tmp_path, FakeClient())
        worker = asyncio.create_task(rt._sender())
        res = []
        async with TestClient(TestServer(rt.make_app())) as c:
            # a) card already posted
            rt.core.sent_cards.add("KB-AAAAA1")
            r = await c.post("/notify", json=payload("KB-AAAAA1", "req-0000000000a1"), headers=K)
            res.append((r.status, await r.json()))
            # b) notify arrives while the confirm is in flight; the card is posted 0.5 s later
            rt.core.inflight[ROOM] = 1

            async def finish():
                await asyncio.sleep(0.5)
                rt.core.sent_cards.add("KB-BBBBB2")
                rt.core.inflight[ROOM] = 0
            asyncio.create_task(finish())
            r = await c.post("/notify", json=payload("KB-BBBBB2", "req-0000000000b2"), headers=K)
            res.append((r.status, await r.json()))
            # c) no card from the bot (request made elsewhere): rendered once, a repeat is suppressed
            r = await c.post("/notify", json=payload("KB-CCCCC3", "req-0000000000c3"), headers=K)
            res.append((r.status, await r.json()))
            r = await c.post("/notify", json=payload("KB-CCCCC3", "req-0000000000c3"), headers=K)
            res.append((r.status, await r.json()))
        worker.cancel()
        return rt, res
    rt, res = asyncio.run(go())
    assert res[0][1].get("suppressed") and res[1][1].get("suppressed")
    assert "event_id" in res[2][1] and res[3][1].get("suppressed")
    assert len(rt.client.sent) == 1
    body = rt.client.sent[0][1]["body"]
    assert body.split("\n") == ["Anfrage gesendet · KB-CCCCC3 · an Dr. Jakob Steiner",
                                "Demo-PIN für Dr. Jakob Steiner: 4711 (an die Person weitergeben)",
                                "Verfolgen: !status KB-CCCCC3 · !audit KB-CCCCC3"]
    assert body.count("PIN") == 1


def test_bot_card_marks_ref_as_posted():
    import asyncio as _a
    from bot.core import Core
    from bot.tests.test_core import FakeService, EVA, ROOM as R, LENA

    async def send(room, text, html=None, *_):
        pass
    svc = FakeService()
    core = Core(svc, send)

    async def go():
        await core.handle(R, LENA, EVA.format(addr="eva.brandl@labor-kanal.example"))
        await core.handle(R, LENA, "!ja")
    _a.run(go())
    assert {"KB-ABC123", "req-000000000001"} <= core.sent_cards and core.inflight[R] == 0


def test_html_never_shows_a_raw_mxid_nested_code_or_an_empty_list():
    """The formatted body Element renders: display name instead of the MXID, flat <code>, no empty <ul>."""
    names = {"@lena.hofer:klinikum.example": "Dr. Lena Hofer"}
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "discharge_report", "patient": {"name": "Maria Beispiel", "birthDate": "1961-03-12"},
        "sender": "Stefan Kern · ID Austria (Simulation)",
        "summary": "Abgelegt trotz Prüfhinweis, Entscheidung @lena.hofer:klinikum.example: Name nur umgestellt"}})
    t, h = fmt.humanize(t, names), fmt.humanize(h, names)
    assert "Entscheidung Dr. Lena Hofer" in t and "Entscheidung Dr. Lena Hofer" in h
    assert "lena.hofer" not in h and "<ul></ul>" not in h
    d = __import__("bot.parse", fromlist=["parse_request_args"]).parse_request_args(
        "labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanaI.example, Patient maria-beispiel", "de")
    _, h = fmt.confirm_card(d, [{"code": "lookalike", "message": "Adresse ähnelt labor-kanal.example",
                                 "similar_to": "labor-kanal.example"}], None, "de", {"name": "Maria Beispiel"})
    assert "<code><code>" not in h and not __import__("re").search(r"<code>[^<]*<code>", h)


def test_hold_card_says_simulation_once():
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "hold", "answer_id": "5", "data": {
        "ref_code": "KB-C37C37", "reasons_de": ["Name weicht ab."], "doc_kind": "lab_result",
        "sender": "Eva Brandl (ID Austria – Simulation)", "matched": ["✓ Absender per ID Austria (Simulation) geprüft"]}})
    assert "Simulation" not in t and "✓ ID Austria: Eva Brandl" in t       # the eID page says it; the card says ID Austria


def test_holds_superseded_card_and_holds_closed(tmp_path):
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "holds_superseded",
                               "data": {"ref_code": "KB-AB12CD", "request_id": "req-0000000000aa", "answer_ids": [3, 4]}})
    assert t == "✓ KB-AB12CD: frühere angehaltene Antwort(en) ersetzt – neue Antwort abgelegt, keine Entscheidung mehr nötig."
    K = {"X-PGK-Notify-Key": "test-notify-key"}

    async def go():
        rt = _rt(tmp_path, FakeClient())
        rt.core.register_hold(ROOM, "KB-AB12CD")
        rt.core.register_hold(ROOM, "KB-OTHER1")
        worker = asyncio.create_task(rt._sender())
        async with TestClient(TestServer(rt.make_app())) as c:
            r = await c.post("/notify", json={"room_id": ROOM, "kind": "holds_superseded",
                                              "data": {"ref_code": "KB-AB12CD", "answer_ids": [3]}}, headers=K)
            st = r.status
        worker.cancel()
        return rt, st
    rt, st = asyncio.run(go())
    assert st == 200 and rt.core.holds[ROOM] == ["KB-OTHER1"]


def test_wrong_address_misdelivery_offers_new_request_not_unlock():
    cmd = "!labor Brandl maria-beispiel eva.brandl@labor-kanal.example"
    base = {"room_id": ROOM, "kind": "misdelivery", "request_id": "req-0000000000aa", "ref_code": "KB-AB12CD"}
    t, h = fmt.notify_message({**base, "text": "[FEHLZUSTELLUNG ABGEFANGEN] … Der Link ist jetzt gesperrt.",
                               "data": {"wrong_address": True, "locked": True, "next_step": cmd}})
    assert "!freigeben" not in t and f"Nächster Schritt: {cmd}" in t and t.count(cmd) == 1
    t, _ = fmt.notify_message({**base, "text": f"… Dieser Link ist gesperrt; eine neue Anfrage an die richtige Adresse: {cmd}",
                               "data": {"wrong_address": True, "locked": True, "next_step": cmd}})
    assert "!freigeben" not in t and t.count(cmd) == 1                  # already in the service text: not repeated
    t, _ = fmt.notify_message({**base, "text": "… Der Link ist jetzt gesperrt.", "data": {"locked": True}})
    assert "!freigeben KB-AB12CD" in t                                  # eID lock (not wrong address): unchanged


def test_tamper_card_red_state_and_service_words():
    msg = "Audit-Kette von KB-AB12CD verändert: Eintrag #12 passt nicht mehr zum Hash."
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "tamper", "text": msg,
                               "data": {"ref_code": "KB-AB12CD", "changed": ["#12"]}})
    assert t == f"Befund nachträglich geändert\n{msg}\nReferenz: KB-AB12CD"
    assert h.startswith('<p><font data-mx-color="#E30613"><b>Befund nachträglich geändert</b></font></p>')
    assert "[ACHTUNG]" not in h and "<ul>" not in h
    t, h = fmt.notify_message({"room_id": ROOM, "kind": "tamper", "data": {"ref_code": "KB-7QX4MZ"}, "text":
                               "[ACHTUNG] Maria Beispiel (12.03.1961) – INR am FHIR-Server geändert: 2,4 → 1,4 (KB-7QX4MZ). "
                               "Nicht verwenden, bis geprüft."})
    assert t.split("\n")[:2] == ["Befund nachträglich geändert · Maria Beispiel · 12.03.1961",
                                 "INR am FHIR-Server geändert: 2,4 → 1,4. Nicht verwenden, bis geprüft."]


def test_proof_line_wording_and_text_only_rows():
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {
        "doc_kind": "lab_result", "validator_errors": 0, "values_matched": 5, "values_total": 5, "values_label": "Werte",
        "observations": [{"test": "Quick", "value_text": "58", "unit": "%"},
                         {"test": "X", "value_text": "3,4 Gpt/l", "text_only": True}]}})
    assert "Geprüft: HL7-Validator: 0 Fehler · 5/5 Werte unverändert übernommen · 1 als Text abgelegt (Einheit nicht zuordenbar)" in t
    assert "stimmen mit" not in t and "überein" not in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "lab_result", "text_only": 2,
                                                                          "values_matched": 4, "values_total": 4}})
    assert "2 als Text abgelegt (Einheit nicht zuordenbar)" in t
    t, _ = fmt.notify_message({"room_id": ROOM, "kind": "filed", "data": {"doc_kind": "lab_result", "photo_note": "📷 aus Foto",
                                                                          "values_matched": 4, "values_total": 4}})
    assert "4/4 Werte unverändert aus der Lesung übernommen" in t


def test_audit_collapses_repeated_identical_steps():
    ev = lambda step, who, at, outcome="success": {"step": step, "outcome": outcome, "who": who, "what": "",
                                                    "at": f"2026-10-29T19:{at:02d}:00+01:00"}
    evs = [ev("request sent", "Dr. Lena Hofer", 1)] + [ev("link opened", "anonym", 2 + i) for i in range(6)] \
        + [ev("identity checked", "Karl Kollege", 9, "failure"), ev("identity checked", "Dr. Eva Brandl", 10),
           ev("link opened", "anonym", 11)]
    t, _ = fmt.audit_message("KB-AB12CD", {"entries": evs})
    rows = [l for l in t.split("\n") if l.startswith(("✓", "⚠️")) and "Schritte" not in l]
    assert rows == ["✓ 29.10. 19:01:00 · Anfrage gesendet · Dr. Lena Hofer",
                    "✓ 29.10. 19:02:00 · Link geöffnet (7×, zuletzt 19:11:00) · anonym",
                    "⚠️ 29.10. 19:09:00 · Identität geprüft · Karl Kollege",
                    "✓ 29.10. 19:10:00 · Identität geprüft · Dr. Eva Brandl"]

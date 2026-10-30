"""Self-tests of the measuring instruments: audit chain verifier, PII scanner, fidelity helpers."""
from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

from pgktest import audit as A, fidelity as F, pii  # noqa: E402
from pgktest.stack import sms_links_for  # noqa: E402

P = {"name": "Maria Beispiel", "birthDate": "1961-03-12", "id": "maria-beispiel", "mrn": "KB-000123"}


def _chain(n=6, same_second=True):
    evs, prev = [], None
    for i in range(n):
        e = {"resourceType": "AuditEvent", "id": f"x{random.random()}", "meta": {"versionId": "1"},
             "recorded": "2026-10-29T10:00:00+01:00" if same_second else f"2026-10-29T10:00:0{i}+01:00",
             "outcomeDesc": f"step {i}"}
        if prev:
            e["extension"] = [{"url": "urn:x:audit-prev-sha256", "valueString": prev}]
        body = {k: v for k, v in e.items() if k not in ("meta", "id", "text")}
        prev = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        evs.append(e)
    random.shuffle(evs)
    return evs


def test_chain_intact_even_with_tied_timestamps():
    r = A.verify_chain(_chain())
    assert r["ok"], r


def test_chain_tamper_and_gap_detected():
    evs = _chain()
    r = A.verify_chain(evs)
    assert A.tamper_detected(evs, r["canon"])["ok"]
    mid = next(e for e in evs if e["outcomeDesc"] == "step 3")
    gap = [e for e in evs if e is not mid]
    assert not A.verify_chain(gap, r["canon"])["ok"]


def test_chain_absent_is_not_ok():
    assert not A.verify_chain([{"resourceType": "AuditEvent", "id": "1"}, {"resourceType": "AuditEvent", "id": "2"}])["ok"]


def test_pii_scanner_hits_and_misses():
    v = lambda t: {f[1] for f in pii.violations(pii.scan(t, "x", P, orgs=["Labor Kanalstraße"]))}
    assert v("Klinikum Beispielstadt bittet Sie um eine Rückmeldung. https://x/r/abc") == set()
    assert v("Befund für Maria") == {"name", "document type"}
    assert v("born 12. März 1961") == {"birth date"}
    assert v("12/03/1961") == {"birth date"} and v("1961-03-12") == {"birth date"}
    assert v("https://x/r/maria-beispiel") == {"name", "patient identifier"}
    assert v("Kanalstraße") == {"recipient organisation"} and "recipient organisation" in v("Labor Kanalstraße")
    assert v("Reha Waldblick") >= {"recipient organisation"}


def test_timezone_rule():
    assert F.tz_ok("2026-10-22T08:10:00+02:00") and F.tz_ok("2026-10-27T07:30:00+01:00")
    assert not F.tz_ok("2026-10-22T08:10:00+01:00") and not F.tz_ok("2026-10-22T06:10:00Z")
    assert not F.tz_ok("2026-10-22T08:10:00")


def test_task_history():
    v = lambda *s: [{"status": x} for x in reversed(s)]
    assert F.task_history_ok(v("requested", "completed"), {"completed"})[0]
    assert F.task_history_ok(v("requested", "on-hold"), {"on-hold"})[0]
    assert F.task_history_ok(v("completed"), {"completed"})[0]                 # first write = filing transaction
    assert F.task_history_ok(v("in-progress", "completed"), {"completed"})[0]  # 'requested' coalesced away
    assert F.task_history_ok(v("draft", "in-progress", "on-hold"), {"on-hold"})[0]
    assert not F.task_history_ok(v("completed", "in-progress"), {"in-progress"})[0]  # regression after final
    assert not F.task_history_ok(v("completed", "requested", "in-progress"), {"in-progress", "requested"})[0]
    assert not F.task_history_ok(v("requested", "completed"), {"on-hold"})[0]


def test_sms_link_association():
    html = ("<li>An: +43 660 000 0101<p>Link http://h/r/AAA</p></li>"
            "<li>An: +43 699 1234567<p>Link http://h/r/BBB</p></li>")
    assert sms_links_for(html, "+43 660 000 0101") == ["http://h/r/AAA"]
    assert sms_links_for(html, "+436991234567") == ["http://h/r/BBB"]


def _keyed_rows(key, n=6, keyed_from=0):
    rows, prev = [], A.GENESIS
    for i in range(1, n + 1):
        r = {"id": i, "request_id": "req-x", "step": "link opened", "outcome": "success", "who": "w", "what": f"e{i}",
             "at": f"2026-10-29T10:00:0{i}+01:00", "ip": None, "user_agent": None, "detail": {"k": i}, "prev_hash": prev,
             "alg": A.KEYED if i > keyed_from else "sha256"}
        r["hash"] = A.row_hash(r, key)
        rows.append(r)
        prev = r["hash"]
    return rows


def test_keyed_chain_verifies_and_detects_tamper_and_downgrade():
    key = A.audit_key("s" * 64)
    rows = _keyed_rows(key, keyed_from=2)
    assert A.verify_rows(rows, key)["ok"]
    assert not A.verify_rows(rows, A.audit_key("t" * 64))["ok"]       # wrong key
    assert not A.verify_rows(rows, None)["ok"]                         # keyed rows need the key
    assert A.tamper_rows(rows, key)["ok"]
    down = _keyed_rows(key)
    down[3]["alg"] = "sha256"
    down[3]["hash"] = A.row_hash(down[3], key)
    for i in range(4, len(down)):
        down[i]["prev_hash"] = down[i - 1]["hash"]
        down[i]["hash"] = A.row_hash(down[i], key)
    assert any("downgrade" in p for p in A.verify_rows(down, key)["problems"])
    assert A.verify_anchors(rows, [(3, rows[2]["hash"], "x")])["ok"]
    assert not A.verify_anchors(rows, [(3, rows[1]["hash"], "x")])["ok"]


def test_server_error_is_not_a_block():
    """A 5xx on submit must never be scored as a caught misdelivery (BLOCK)."""
    from e2e.run import Run
    from pgktest.outside import Visit
    from pgktest.web import Page
    v = Visit(answer_form_seen=True, submitted=True, result=Page("u", 500, "", [], [], "Internal Server Error"))
    assert Run.classify([], None, v)[0] == "ERROR"
    v.result = Page("u", 422, "", [], [], "Nicht angenommen")
    assert Run.classify([], None, v)[0] == "BLOCK"


def test_hash_forms():
    import base64
    import hashlib
    from metrics import _ni_hex
    d = hashlib.sha256(b"\xe4\xf6\xfc Windows-1252 bytes").digest()
    assert _ni_hex("ni:///sha-256;" + base64.urlsafe_b64encode(d).decode().rstrip("=")) == d.hex()
    assert _ni_hex(f"urn:sha256:{d.hex()}") == d.hex()
    assert _ni_hex(f"Eingangsbestätigung\nSHA-256: {d.hex()}\nGröße: 20 Bytes") == d.hex()
    assert _ni_hex("no hash here") is None


def test_anchor_mac():
    key = A.audit_key("s" * 64)
    good = A.anchor_mac(key, "abcdef0123456789", 705, "9" * 64)
    found = A.matrix_anchors([f"[AUDIT-ANKER] Audit-Kette abcdef0123456789 bis Eintrag #705: {'9' * 64} (MAC {good})",
                              f"[AUDIT-ANKER] Audit-Kette abcdef0123456789 bis Eintrag #706: {'8' * 64} (MAC {'0' * 32})",
                              f"[AUDIT-ANKER] Audit-Kette abcdef0123456789 bis Eintrag #707: {'7' * 64}"], "abcdef0123456789")
    ok, ignored = A.split_authentic(found, key, "abcdef0123456789")
    assert [i for i, _, _ in ok] == [705] and len(ignored) == 2


def test_audit_room_fallback_and_demo_rows():
    from e2e.run import answer_blob
    from pgktest.stack import Stack
    st = Stack(state={"room_id": "!main:x"})
    assert st.audit_room_id == "!main:x"
    st = Stack(state={"room_id": "!main:x", "audit_room_id": "!audit:x"})
    assert st.audit_room_id == "!audit:x"
    rows = [{"step": "Demo-Identität angelegt", "what": "Persona angelegt, Entscheidung später"},
            {"step": "answer sent", "what": "Antwort übermittelt"}]
    assert "Demo" not in answer_blob(rows) and "Antwort" in answer_blob(rows)


def test_tamper_text_is_sentences():
    from metrics import tamper_text
    t = {"fhir": {"ok": True, "edited": "AuditEvent/2003", "edit_detected": ["AuditEvent/2003"], "delete_detected": True},
         "rows": {"ok": True, "edited_id": 369, "edit_detected": ["id 369: stored hash != recomputed"],
                  "delete_detected": ["gap before id 370", "id 370: prev_hash != hash of predecessor"]}, "ok": True}
    txt = tamper_text(t)
    assert "{" not in txt and "Edited AuditEvent/2003 → detected" in txt and "gap before id 370" in txt

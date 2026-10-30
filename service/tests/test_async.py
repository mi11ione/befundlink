"""Async pipeline: the upload POST never waits for validation/HAPI/bot; the worker files in the background;
the outbox keeps HAPI writes ordered; pending answers survive a restart."""
import re
import time

import httpx
from fastapi.testclient import TestClient

from service.app import create_app
from service.config import Settings

from .conftest import BOT, HAPI, REQ1, ROOM, doc
from .test_e2e import HDR, login, send, upload


def make(tmp_path, world, mailer, slow_validate_s=1.5, **kw):
    s = Settings(base_url="http://svc.test", hapi_url=HAPI, bot_url=BOT, db_path=str(tmp_path / "a.sqlite"),
                 secret=b"test-secret-key-0123456789abcdef", default_room_id=ROOM, prewarm_tx=False, dev=True,
                 async_io=True, validator_server=False, data_key_hex="44" * 32, **kw)
    app = create_app(s, mailer=mailer, http=httpx.Client(transport=httpx.MockTransport(world.handler)))
    svc = app.state.svc

    def slow(resources):
        time.sleep(slow_validate_s)
        world.validated.append(resources)
        return [], {"mode": "tx", "terminology_checked": True, "seconds": slow_validate_s}
    svc._validate = slow
    return app, svc


def wait(pred, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_upload_post_does_not_wait_for_the_pipeline(tmp_path, world, mailer):
    app, svc = make(tmp_path, world, mailer, slow_validate_s=2.0)
    with TestClient(app, base_url="http://svc.test") as c:
        req = send(c, REQ1)
        link = mailer.last_link()
        page = login(c, link, "Eva", "Brandl")
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        t0 = time.perf_counter()
        r = c.post(f"{link}/answer", data={"csrf": csrf},
                   files={"file": ("a.md", doc("lab-result-maria-beispiel.md").encode(), "text/markdown")})
        dt = time.perf_counter() - t0
        assert r.status_code == 200 and dt < 1.0, dt                      # validation takes 2 s, the POST does not
        assert "wird geprüft" in r.text and 'http-equiv="refresh"' in r.text
        st = c.get(f"{link}/status", headers={"Accept": "application/json"}).json()
        assert st["state"] == "processing"
        assert wait(lambda: c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed")
        st = c.get(f"{link}/status", headers={"Accept": "application/json"}).json()   # token retired, session works
        assert st == {"state": "received", "ref": req["ref_code"]}
        assert "erhalten" in c.get(f"{link}/status").text
        assert svc.outbox.drain(10) and any(n.get("kind") == "filed" for n in world.notifications)


def test_wrong_patient_is_still_rejected_immediately_and_not_stored(tmp_path, world, mailer):
    app, svc = make(tmp_path, world, mailer)
    with TestClient(app, base_url="http://svc.test") as c:
        req = send(c, REQ1)
        link = mailer.last_link()
        page = login(c, link, "Eva", "Brandl")
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        t0 = time.perf_counter()
        r = c.post(f"{link}/answer", data={"csrf": csrf},
                   files={"file": ("a.md", doc("lab-result-max-beispiel.md").encode(), "text/markdown")})
        assert time.perf_counter() - t0 < 1.0
        assert r.status_code == 422 and "betrifft nicht Maria Beispiel" in r.text and "Max" not in r.text
        assert svc.store.one("SELECT content FROM answers WHERE request_id=?", (req["request_id"],))["content"] is None
        assert svc.outbox.drain(10)
        assert world.notifications[-1]["kind"] == "misdelivery" and not world.transactions


def test_batch_uses_one_cli_validation_for_several_filings(tmp_path, world, mailer, monkeypatch):
    app, svc = make(tmp_path, world, mailer)
    calls = []

    def report(resources, mode="auto", names=None, **kw):
        calls.append(len(resources))
        return {"mode": "tx", "requested_mode": mode, "fallback": None, "terminology_checked": True, "seconds": 1,
                "files": {}, "errors": [], "warnings": []}
    import service.core as core_mod
    monkeypatch.setattr(core_mod.fhirmap, "validate_report", report)
    with TestClient(app, base_url="http://svc.test") as c:
        items = []
        for _ in range(3):
            req = send(c, REQ1)
            link = mailer.last_link()
            page = login(c, link, "Eva", "Brandl")
            csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
            items.append((req, link, csrf))
            c.cookies.clear()
        # queue three answers directly (as three simultaneous uploads would)
        svc.worker.q.mutex.acquire()  # hold the queue so all three land in one batch
        try:
            for req, link, csrf in items:
                r = svc.store.get_request(req["request_id"])
                sess = svc.store.q("SELECT * FROM sessions WHERE request_id=?", (r["id"],))[-1]
                aid = svc.store.insert("answers", {"request_id": r["id"], "received_at": "2026-10-29T21:00:00+01:00",
                                                   "sender_bpk": sess["bpk"], "sender_name": "Eva Brandl", "sender_verdict": "match",
                                                   "identity": {"given_name": "Eva", "family_name": "Brandl", "bpk_hash": sess["bpk"],
                                                                "loa": "high", "assertion": sess["statement"]},
                                                   "sha256": "x", "size": 1, "content": None, "encrypted": 0})
                svc._store_content(aid, r, doc("lab-result-maria-beispiel.md").encode())
                svc.store.update_request(r["id"], status="processing")
                svc.worker.q.queue.append((r["id"], aid, None))
                svc.worker.q.unfinished_tasks += 1
                svc.worker.q.not_empty.notify()
        finally:
            svc.worker.q.mutex.release()
        assert svc.worker.drain(30)
        assert all(c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed" for req, _, _ in items)
        assert calls and max(calls) >= 2 * 10  # one JVM call validated several filings (10+ files each)


def test_outbox_puts_task_before_auditevents_that_reference_it(tmp_path, world, mailer):
    app, svc = make(tmp_path, world, mailer)
    order = []
    orig = world.handler

    def spy(req):
        if req.method in ("PUT", "POST") and str(req.url).startswith(HAPI):
            order.append((req.method, str(req.url)[len(HAPI) + 1:].split("/")[0], req.content))
        return orig(req)
    svc.http = svc.hapi.http = httpx.Client(transport=httpx.MockTransport(spy))
    with TestClient(app, base_url="http://svc.test") as c:
        send(c, REQ1)
        assert svc.outbox.drain(10)
    first_task = next(i for i, (m, t, _) in enumerate(order) if t == "Task")
    first_ref = next(i for i, (m, t, body) in enumerate(order) if t == "AuditEvent" and b'"Task/' in body)
    assert first_task < first_ref


def test_pending_answer_is_recovered_after_restart(tmp_path, world, mailer):
    app, svc = make(tmp_path, world, mailer, slow_validate_s=0)
    with TestClient(app, base_url="http://svc.test") as c:
        req = send(c, REQ1)
        link = mailer.last_link()
        login(c, link, "Eva", "Brandl")
        r = svc.store.get_request(req["request_id"])
        sess = svc.store.q("SELECT * FROM sessions WHERE request_id=?", (r["id"],))[-1]
        aid = svc.store.insert("answers", {"request_id": r["id"], "received_at": "2026-10-29T21:00:00+01:00",
                                           "sender_bpk": sess["bpk"], "sender_name": "Eva Brandl", "sender_verdict": "match",
                                           "identity": {"given_name": "Eva", "family_name": "Brandl", "bpk_hash": sess["bpk"],
                                                        "loa": "high", "assertion": sess["statement"]},
                                           "sha256": "x", "size": 1, "content": None, "encrypted": 0})
        svc._store_content(aid, r, doc("lab-result-maria-beispiel.md").encode())
        svc.store.update_request(r["id"], status="processing")   # "crashed" before the worker picked it up
    app2, svc2 = make(tmp_path, world, mailer, slow_validate_s=0)
    with TestClient(app2, base_url="http://svc.test") as c2:
        assert wait(lambda: c2.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed")


def _task_on_hapi(world, svc, rid):
    r = svc.store.get_request(rid)
    return world.resources[f"Task/{r['task_id']}"]


def test_stale_task_updates_after_filing_are_dropped(app_client, mailer, world):
    """The A33 race: requested/in-progress PUTs that were queued before the filing land after it."""
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    rid = req["request_id"]
    r = svc.store.get_request(rid)
    stale = [(svc._put_task(r, "requested"), svc.store.get_request(rid)["task_seq"]),
             (svc._put_task(r, "in-progress"), svc.store.get_request(rid)["task_seq"])]
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    assert c.get(f"/api/requests/{rid}", headers=HDR).json()["status"] == "filed"
    for task, seq in stale:                      # replay the old queued writes AFTER the filing
        assert svc._put_task_http(rid, task, seq)
    t = _task_on_hapi(world, svc, rid)
    assert t["status"] == "completed" and t.get("output")


def test_terminal_task_is_never_overwritten_even_without_seq(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    r = svc.store.get_request(req["request_id"])
    assert svc._put_task_http(r["id"], svc._put_task(r, "in-progress"), None)
    assert _task_on_hapi(world, svc, r["id"])["status"] == "completed"


def test_concurrent_write_between_read_and_put_is_caught_by_if_match(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    r = svc.store.get_request(req["request_id"])
    tid = f"Task/{r['task_id']}"
    stale = svc._put_task(r, "in-progress")
    seq = svc._bump_task_seq(r["id"])
    done = {**world.resources[tid], "status": "completed", "output": [{"type": {"text": "x"}, "valueReference": {"reference": "DiagnosticReport/1"}}]}

    def filing_lands(path):                      # the filing transaction writes right after our read
        if path == tid and world.resources[tid]["status"] != "completed":
            world.resources[tid] = done
            world.versions[tid] = world.versions.get(tid, 1) + 1
    world.before_put = filing_lands
    assert svc._put_task_http(r["id"], stale, seq)
    world.before_put = None
    assert world.resources[tid]["status"] == "completed" and world.resources[tid]["output"]


def test_legitimate_back_transition_still_applies(app_client, mailer, world):
    """on-hold -> in-progress after a new upload is newer (higher seq) and must be written."""
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    rid = req["request_id"]
    svc._set_status(rid, "held_review", "Beinahe-Übereinstimmung")
    assert _task_on_hapi(world, svc, rid)["status"] == "on-hold"
    svc._set_status(rid, "identity_verified")
    assert _task_on_hapi(world, svc, rid)["status"] == "in-progress"


def test_first_task_write_is_never_lost_when_stale(app_client, mailer, world):
    """A stale PUT for a Task that does not exist yet creates it with the CURRENT state (AuditEvents need it)."""
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    rid = req["request_id"]
    r = svc.store.get_request(rid)
    tid = f"Task/{r['task_id']}"
    first = svc._put_task(r, "requested")
    world.resources.pop(tid)                                  # as if the first PUT had not happened yet
    svc._set_status(rid, "held_review", "Beinahe-Übereinstimmung")  # newer state recorded
    world.resources.pop(tid, None)
    assert svc._put_task_http(rid, first, 1)                 # stale, but the Task is missing -> created as on-hold
    assert world.resources[tid]["status"] == "on-hold"


def test_reconcile_repairs_a_filed_task(app_client, mailer, world):
    c, svc = app_client, app_client.svc
    req = send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    upload(c, link, doc("lab-result-maria-beispiel.md"))
    r = svc.store.get_request(req["request_id"])
    tid = f"Task/{r['task_id']}"
    world.resources[tid] = {**world.resources[tid], "status": "in-progress", "output": []}   # the A33 end state
    assert svc.reconcile_tasks()["completed"] == 1
    assert world.resources[tid]["status"] == "completed" and world.resources[tid]["output"]

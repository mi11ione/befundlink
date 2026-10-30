"""SMS inbox auth, fail closed, token-free logs, early size check, strict API inputs."""
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from service.app import RedactTokens, create_app
from service.config import ConfigError, Settings

from .conftest import BOT, HAPI, REQ1, REQ2, ROOM
from .test_e2e import HDR, login, send


def _settings(tmp_path, **kw):
    base = dict(base_url="http://svc.test", hapi_url=HAPI, bot_url=BOT, db_path=str(tmp_path / "h.sqlite"),
                default_room_id=ROOM, prewarm_tx=False, async_io=False, validator_server=False, data_key_hex="55" * 32)
    return Settings(**{**base, **kw})


def test_fail_closed_without_secrets(tmp_path, monkeypatch):
    for k in ("PGK_API_KEY", "PGK_SECRET", "PGK_BPK_SALT", "PGK_DEV"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError):
        create_app(_settings(tmp_path))
    with pytest.raises(ConfigError):  # too short a secret
        create_app(_settings(tmp_path, api_key="k" * 20, secret=b"short", bpk_salt="s" * 16))
    create_app(_settings(tmp_path, api_key="k" * 20, secret=b"x" * 32, bpk_salt="s" * 16))   # complete -> starts


def test_sms_inbox_needs_api_key_or_local_demo(tmp_path, world, mailer):
    s = _settings(tmp_path, api_key="secret-key-123456", secret=b"x" * 32, bpk_salt="s" * 16)
    app = create_app(s, mailer=mailer, http=httpx.Client(transport=httpx.MockTransport(world.handler)))
    with TestClient(app, base_url="http://svc.test") as c:
        h = {"X-API-Key": "secret-key-123456"}
        assert c.post("/api/requests", json=REQ2, headers={**h, **HDR}).status_code == 201
        assert c.get("/sms").status_code == 401 and c.get("/api/sms").status_code == 401
        assert c.get("/api/sms", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/api/sms", headers=h).status_code == 200
        app.state.svc.s.demo = True                   # demo mode: local browser may look at the simulated inbox
        assert c.get("/sms").status_code == 200


def test_oversize_upload_rejected_before_body_is_read(app_client, mailer):
    c = app_client
    send(c, REQ1)
    link = mailer.last_link()
    login(c, link, "Eva", "Brandl")
    r = c.post(f"{link}/answer", content=b"x", headers={"Content-Length": str(5_000_000),
                                                        "Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_tokens_are_redacted_from_logs():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s"', (
        "127.0.0.1", "POST", "/r/" + "b" * 60 + "/eid?assertion=eyJhbGciOi.eyJzdWIi.c2lnbmF0"), None)
    RedactTokens().filter(rec)
    assert isinstance(rec.args, tuple) and len(rec.args) == 3       # shape kept for uvicorn's formatter
    msg = rec.getMessage()
    assert "b" * 60 not in msg and "eyJzdWIi" not in msg and "/r/<token>" in msg


def test_blanket_confirm_is_not_enough(app_client, mailer):
    c = app_client
    body = {**REQ1, "address": "eva.brandl@labor-kanaI.example", "confirm_warnings": True}
    assert c.post("/api/requests", json=body, headers=HDR).json()["status"] == "needs_confirmation" and not mailer.sent


def test_patient_id_format_checked(app_client):
    r = app_client.post("/api/requests", json={**REQ1, "patient_id": "../Patient/x?_format=xml"}, headers=HDR)
    assert r.status_code == 422


def test_domain_sent_despite_warning_is_not_learned(app_client, mailer):
    c = app_client
    body = {**REQ1, "address": "eva.brandl@labor-kanaI.example", "confirm_warnings": True}
    codes = [w["code"] for w in c.post("/api/requests/check", json=body).json()["warning_details"]]
    req = send(c, {**body, "acknowledged_warnings": codes})
    assert req["status"] == "sent"
    login(c, mailer.last_link(), "Eva", "Brandl")
    from .conftest import doc
    from .test_e2e import upload
    upload(c, mailer.last_link(), doc("lab-result-maria-beispiel.md"))
    assert c.get(f"/api/requests/{req['request_id']}", headers=HDR).json()["status"] == "filed"
    assert not c.svc.store.q("SELECT * FROM known_domains WHERE domain LIKE 'labor-kana%'")


def test_signing_keys_are_generated_on_first_start_and_private(tmp_path):
    import os
    import stat
    from service.core import Service
    s = _settings(tmp_path, api_key="k" * 20, secret=b"x" * 32, bpk_salt="s" * 16)
    svc = Service(s)
    for name in ("idp_es256.pem", "service_es256.pem"):
        p = tmp_path / name
        assert p.exists() and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    kids = {k["kid"] for k in svc.jwks()["keys"]}
    assert len(kids) == 2 and kids == {k["kid"] for k in Service(s).jwks()["keys"]}  # stable across restarts


def test_no_private_key_material_is_tracked():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(["git", "ls-files", "service"], cwd=root, capture_output=True, text=True).stdout.split()
    assert not [t for t in tracked if t.endswith((".pem", ".key", ".datakey")) or "sqlite" in t]
    for t in tracked:
        if t.startswith("service/data/") and Path(root / t).is_file():
            assert "PRIVATE KEY" not in (root / t).read_text(errors="ignore")

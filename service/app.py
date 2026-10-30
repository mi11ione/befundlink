"""FastAPI app: API for the bot, outside-person pages, mock ID Austria, SMS inbox.

Run (repo root):  make run      (service + bot, secrets from .env; binds 127.0.0.1)
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import hmac
import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import eid, retention
from .reasons_de import SENDER_DE
from .config import Settings
from .core import VIENNA, fhir_patient_name, ANSWERABLE, CLOSED, DOC_KIND_LABEL, TOKEN_ALPHABET, TOKEN_LEN, ApiError, Service
import fhirmap

log = logging.getLogger("pgk.app")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_ANSWER_RE = re.compile(r"^/r/[^/]+/answer$")
_KEY_RE = re.compile(r"^(req-[0-9a-f]{12}|KB-[2-9C-Z]{6})$")
_RETURN_RE = re.compile(rf"^/r/[{TOKEN_ALPHABET}]{{{TOKEN_LEN}}}/eid$")
SID_COOKIE = "pgk_sid"
_UPLOAD_JS = (Path(__file__).parent / "static" / "upload.js").read_text(encoding="utf-8")


_SMS_RE = re.compile(r"^(?P<who>.+?): (?P<what>.+?) Anmeldung mit ID Austria: (?P<link>\S+)(?: (?P<tail>.+))?$")


def _sms_view(m: dict) -> dict:
    """The simulated inbox shows an SMS as lines (sender / request / link / callback), a German date and a short link
    text; the href keeps the full link."""
    try:
        at = datetime.fromisoformat(m["at"]).astimezone(VIENNA).strftime("%d.%m.%Y, %H:%M")
    except (TypeError, ValueError):
        at = m.get("at") or ""
    hit = _SMS_RE.match(m.get("text") or "")
    if not hit:
        return {**m, "when": at, "lines": None}
    link = hit["link"]
    host_path = re.sub(r"^https?://", "", link)
    short = host_path if len(host_path) <= 32 else f"{host_path[:24]}…{host_path[-6:]}"
    return {**m, "when": at, "lines": True, "who": hit["who"], "what": hit["what"], "link": link, "short": short, "tail": hit["tail"]}


def _configure_logging() -> None:
    lg = logging.getLogger("pgk")
    if not lg.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    install_redaction()


# Loggers whose HANDLERS get the token filter. A filter on a logger only sees records logged on that very logger,
# not records propagated from its children (pgk.core, pgk.app, ...); a filter on a handler sees every record the
# handler writes, whichever logger it came from. Root covers anything that propagates to it.
_REDACTED_LOGGERS = ("", "pgk", "uvicorn", "uvicorn.access", "uvicorn.error")


def install_redaction() -> None:
    """Put RedactTokens on every handler of the loggers above (idempotent; called again at startup, after uvicorn
    has configured its own handlers)."""
    handlers = []
    for name in _REDACTED_LOGGERS:
        handlers += logging.getLogger(name).handlers
    if logging.lastResort is not None:  # Python's fallback handler, used when a record finds no handler at all
        handlers.append(logging.lastResort)
    for handler in handlers:
        if not any(isinstance(f, RedactTokens) for f in handler.filters):
            handler.addFilter(RedactTokens())


class RedactTokens(logging.Filter):
    """M3: link tokens and eID tokens never reach a log line (uvicorn access log, our own loggers)."""
    _RX = [(re.compile(r"/r/[bcdfghjkmnpqrstvwxz]{60}"), "/r/<token>"),
           (re.compile(r"\b[bcdfghjkmnpqrstvwxz]{60}\b"), "<token>"),  # a bare link token (e.g. a log argument)
           (re.compile(r"(assertion=)(?!%)[^&\s\"]+"), r"\1<redacted>"),
           (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), "<jws>")]

    def _red(self, v):
        if isinstance(v, str):
            for rx, rep in self._RX:
                v = rx.sub(rep, v)
        return v

    def filter(self, record: logging.LogRecord) -> bool:
        # redact inside msg AND each arg, keeping the args shape (uvicorn's access formatter unpacks them);
        # a %-placeholder in msg ("assertion=%s") is left alone, its value is redacted in args
        record.msg = self._red(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._red(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._red(v) for k, v in record.args.items()}
        return True


def create_app(settings: Settings | None = None, mailer=None, http: httpx.Client | None = None) -> FastAPI:
    _configure_logging()
    settings = settings or Settings()
    svc = Service(settings, mailer=mailer, http=http)
    async def _maintenance_loop():
        """Retention purges + audit anchors on a timer (not only on traffic)."""
        while True:
            try:
                await asyncio.to_thread(svc.maintenance)
            except Exception:
                log.exception("maintenance failed")
            await asyncio.sleep(retention.MAINTENANCE_EVERY_S)

    @asynccontextmanager
    async def lifespan(_app):
        install_redaction()  # handlers added by the server after import are covered too
        svc.start()  # outbox + answer worker + warm validator (non-blocking)
        maint = asyncio.create_task(_maintenance_loop())
        threading.Thread(target=svc.ensure_codesystems, name="codesystems", daemon=True).start()
        threading.Thread(target=svc.ensure_directory, name="directory", daemon=True).start()  # requester's FHIR identity
        if settings.prewarm_tx:
            svc.prewarm_tx_cache()
        yield
        maint.cancel()
        svc.stop()

    app = FastAPI(title="Befundlink service", version="0.2", lifespan=lifespan)
    app.state.svc = svc

    def upload_limit() -> int:
        """1 MB for text; with photo intake on (PGK_EXTRACTOR=anthropic) photos and PDFs up to 5 MB."""
        return max(settings.max_upload_bytes, settings.max_photo_bytes) if svc.extractor else settings.max_upload_bytes

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # refuse an oversized upload BEFORE the body is spooled
        if request.method == "POST" and _ANSWER_RE.match(request.url.path):
            cl = request.headers.get("content-length")
            if cl is None or not cl.isdigit():
                return HTMLResponse("<p>Die Datei konnte nicht übertragen werden. Bitte erneut versuchen.</p>", status_code=411)
            if int(cl) > int(upload_limit() * 1.1):
                # drain the stream WITHOUT storing it (no spool, no memory) so the client can read the answer
                seen = 0
                async for chunk in request.stream():
                    seen += len(chunk)
                    if seen > 20_000_000:
                        break
                return HTMLResponse(f"<p>Datei zu groß (max. {upload_limit() // 1_000_000} MB). Sie wurde nicht übernommen "
                                    f"und nicht gespeichert.</p>", status_code=413)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"  # the token is in the URL
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'"
        return resp

    @app.exception_handler(ApiError)
    async def api_error(_: Request, e: ApiError):
        return JSONResponse({"detail": e.detail}, status_code=e.status)

    def api_auth(x_api_key: str | None = Header(default=None)):
        if settings.api_key and not (x_api_key and hmac.compare_digest(x_api_key, settings.api_key)):
            raise ApiError(401, "missing or wrong X-API-Key")

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"clinic": settings.clinic_name, **ctx}, status_code=status)

    def client(request: Request) -> tuple[str | None, str | None]:
        return (request.client.host if request.client else None), request.headers.get("user-agent")

    # ------------------------------------------------------------------ API (bot, tests)
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "fhirmap": svc.fm.name, "encryption": svc.box.enabled}

    # ------------------------------------------------------------------ authorisation (H1)
    # The API key authenticates the CALLER (the bot / operator tooling). The bot also says WHO is acting and
    # WHERE: X-PGK-Actor (Matrix sender) and X-PGK-Room. A request is only visible/changeable for its requester in
    # its room. Every call that returns or changes one request's data (view, status, audit, confirm, cancel,
    # unlock, decisions) REQUIRES both headers; the body "by" is ignored. Only the operator routes (whole audit
    # trail, verify, sync) work with the API key alone, and they refuse actor headers.
    class Who:
        def __init__(self, actor: str | None, room: str | None):
            self.actor, self.room = actor, room

    def who(x_pgk_actor: str | None = Header(default=None), x_pgk_room: str | None = Header(default=None)) -> Who:
        return Who(x_pgk_actor, x_pgk_room)

    def require_who(w: Who) -> None:
        if not (w.actor and w.room):
            raise ApiError(403, "X-PGK-Actor and X-PGK-Room are required")

    def scoped(key: str, w: Who) -> dict:
        """The request `key`, if the acting user is its requester and acts in its room."""
        if not _KEY_RE.match(key or ""):
            raise ApiError(422, "invalid id")
        require_who(w)
        r = svc.resolve(key)
        if w.actor != r["requester_mxid"] or w.room != r["room_id"]:
            raise ApiError(403, "not your request (only the requester, in the request's room)")
        return r

    @app.post("/api/requests/check", dependencies=[Depends(api_auth)])
    def check_request(body: dict):
        return svc.preflight(body)

    @app.post("/api/requests", status_code=201, dependencies=[Depends(api_auth)])
    def create_request(body: dict, w: Who = Depends(who)):
        """Creating a request (also dry_run, which shows the patient's name and birth date) needs the acting user
        and room, and the actor must be a member of the PGK directory (registers.json network.members)."""
        require_who(w)
        if not svc.registers.member(w.actor):
            raise ApiError(403, "only members of the PGK directory can request documents")
        if body.get("requester_mxid") and body["requester_mxid"] != w.actor:
            raise ApiError(403, "requester_mxid must be the acting user")
        if body.get("room_id") and body["room_id"] != w.room:
            raise ApiError(403, "room_id must be the acting room")
        return svc.create_request({**body, "requester_mxid": w.actor, "room_id": w.room})

    @app.get("/api/requests", dependencies=[Depends(api_auth)])
    def list_requests(w: Who = Depends(who)):
        require_who(w)
        rows = svc.store.q("SELECT id FROM requests WHERE requester_mxid=? AND room_id=? ORDER BY created_at DESC LIMIT 200",
                           (w.actor, w.room))
        return [svc.request_view(r["id"]) for r in rows]

    @app.get("/api/requests/{key}", dependencies=[Depends(api_auth)])
    def get_request(key: str, w: Who = Depends(who)):
        return svc.request_view(scoped(key, w)["id"])

    @app.post("/api/requests/{key}/confirm", dependencies=[Depends(api_auth)])
    def confirm(key: str, body: dict | None = None, w: Who = Depends(who)):
        """Body: {"acknowledged_warnings": [every warning code of the request]}."""
        r = scoped(key, w)
        return svc.confirm(r["id"], w.actor, (body or {}).get("acknowledged_warnings"))

    @app.post("/api/requests/{key}/cancel", dependencies=[Depends(api_auth)])
    def cancel(key: str, w: Who = Depends(who)):
        r = scoped(key, w)
        return svc.cancel(r["id"], w.actor)

    @app.post("/api/requests/{key}/unlock", dependencies=[Depends(api_auth)])
    def unlock(key: str, w: Who = Depends(who)):
        """Dr. Hofer re-opens a link locked after repeated failed sign-ins."""
        r = scoped(key, w)
        return svc.unlock(r["id"], w.actor)

    @app.post("/api/requests/{key}/decision", dependencies=[Depends(api_auth)])
    def decision(key: str, body: dict, w: Who = Depends(who)):
        r = scoped(key, w)
        view, aid, override = svc.decide(r["id"], body.get("action") or body.get("decision"), body.get("reason"), w.actor)
        if aid is not None:
            svc.worker.submit(view["request_id"], aid, override)
            view = svc.request_view(view["request_id"])
        return view

    @app.post("/api/answers/{key}/decision", dependencies=[Depends(api_auth)])
    def answer_decision(key: str, body: dict, w: Who = Depends(who)):
        """Bot contract: {action: file|reject|hold, reason}; reason required for file. Actor/room from headers."""
        if not (re.fullmatch(r"\d{1,10}", key or "") or _KEY_RE.match(key or "")):
            raise ApiError(422, "invalid id")
        require_who(w)
        rid = svc.request_of_answer(key)
        scoped(rid, w)
        view, aid, override = svc.decide_answer(key, body.get("action") or body.get("decision"), body.get("reason"), w.actor)
        if aid is not None:
            svc.worker.submit(view["request_id"], aid, override)
            view = {**view, "status": "processing"}
        return view

    @app.get("/api/answers/{key}/preview", dependencies=[Depends(api_auth)])
    def answer_preview(key: str, w: Who = Depends(who)):
        """The held document's text (first 40 lines) for its requester in its room; HOLD only (never a BLOCK)."""
        if not (re.fullmatch(r"\d{1,10}", key or "") or _KEY_RE.match(key or "")):
            raise ApiError(422, "invalid id")
        require_who(w)
        rid = scoped(svc.request_of_answer(key), w)["id"]
        return svc.held_preview(rid, w.actor)

    @app.get("/api/audit", dependencies=[Depends(api_auth)])
    def audit(request_id: str | None = None, after_id: int = 0, limit: int = 1000, w: Who = Depends(who)):
        if request_id:
            return svc.audit_view(scoped(request_id, w)["id"], full=True)  # one request: full chain check
        if w.actor or w.room:
            raise ApiError(403, "the full audit trail is for operators only")
        return svc.audit_view(None, after_id, limit)

    @app.get("/api/audit/verify", dependencies=[Depends(api_auth)])
    def audit_verify(fhir: bool = False, w: Who = Depends(who)):
        if w.actor or w.room:
            raise ApiError(403, "operators only")
        return svc.verify_audit(check_fhir=fhir)

    @app.post("/api/audit/sync", dependencies=[Depends(api_auth)])
    def audit_sync(w: Who = Depends(who)):
        if w.actor or w.room:
            raise ApiError(403, "operators only")
        return svc.sync_audit()

    @app.post("/api/maintenance/reverify", dependencies=[Depends(api_auth)])
    def maintenance_reverify(w: Who = Depends(who)):
        """Operator: re-verify every filed Provenance NOW, in the running service, so a failing filing alerts the
        request's room through the normal notify path (once per filing). `make verify-provenance` calls this."""
        if w.actor or w.room:
            raise ApiError(403, "operators only")
        return svc.reverify_all(alert=True)

    def sms_auth(request: Request, x_api_key: str | None = Header(default=None)):
        """The simulated SMS inbox shows live links -> API key, or PGK_DEMO=1 from localhost only."""
        if not settings.api_key or (x_api_key and hmac.compare_digest(x_api_key, settings.api_key)):
            return
        host = request.client.host if request.client else ""
        if settings.demo and host in ("127.0.0.1", "::1", "localhost", "testclient"):
            return
        raise ApiError(401, "the SMS inbox needs the API key (or PGK_DEMO=1 from localhost)")

    @app.get("/api/sms", dependencies=[Depends(sms_auth)])
    def sms_json():
        return svc.store.q("SELECT * FROM sms ORDER BY id DESC LIMIT 100")

    # ------------------------------------------------------------------ simulated SMS inbox
    @app.get("/sms", response_class=HTMLResponse, dependencies=[Depends(sms_auth)])
    def sms_page(request: Request):
        return page(request, "sms.html", messages=[_sms_view(m) for m in svc.store.q("SELECT * FROM sms ORDER BY id DESC LIMIT 100")])

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return page(request, "index.html")

    # ------------------------------------------------------------------ outside person
    def phone() -> str:
        return svc.registers.clinic().get("callback_phone") or ""

    def closed_page(request: Request, r: dict | None):
        tel = f" Rückfragen: {phone()}." if phone() else ""
        if r is None or r["status"] == "filed":
            # the person whose answer was filed (her session) gets a thank-you with her receipt; anyone else the
            # generic text
            done = svc.filed_by_session(request.cookies.get(SID_COOKIE))
            token = request.path_params.get("token")
            if done and token:
                return page(request, "closed.html", 200, msg="Ihre Antwort wurde abgelegt. Danke.",
                            link_url=f"/r/{token}/receipt.txt", link_text="Eingangsbestätigung speichern",
                            ref=done["ref"], phone=phone())
        if r is None:
            return page(request, "closed.html", 404, msg="Dieser Link ist ungültig oder nicht mehr aktiv. / This link is invalid "
                                                          "or no longer active." + tel)
        if svc.is_expired(r):
            return page(request, "closed.html", 410, msg=f"Dieser Link ist abgelaufen (gültig 7 Tage). Sie möchten noch antworten? "
                                                          f"Bitte rufen Sie an: {phone()}. / This link has expired.")
        if r["status"] == "locked":
            return page(request, "closed.html", 429, msg=f"Dieser Link ist nach mehreren fehlgeschlagenen Anmeldungen gesperrt. "
                                                          f"{requester(r)['requester_name']} kann ihn wieder freigeben – bitte rufen Sie an: {phone()}. / "
                                                          f"This link was locked after several failed sign-ins.")
        if r["status"] in CLOSED:
            return page(request, "closed.html", 410, msg="Ihre Antwort wurde bereits übermittelt. Für einen Nachtrag (z. B. Endbefund) "
                                                          "bitten Sie um eine neue Anfrage." + tel)
        return None

    def requester(r: dict) -> dict:
        m = svc.registers.member(r["requester_mxid"])
        return {"requester_name": m.get("name") or r["requester_mxid"], "department": m.get("department"), "phone": phone()}

    @app.get("/r/{token}", response_class=HTMLResponse)
    def landing(token: str, request: Request):
        r = svc.by_token(token)
        if r is not None:
            ip, ua = client(request)
            svc.link_opened(r, ip, ua)  # audited; never consumes or binds the link
            r = svc.store.get_request(r["id"])
        bad = closed_page(request, r)
        if bad:
            return bad
        if svc.session(r, request.cookies.get(SID_COOKIE)):
            return RedirectResponse(f"/r/{token}/answer", status_code=303)
        notice = ("Ihre Anmeldung ist abgelaufen. Ihre Datei ist verschlüsselt zwischengespeichert und wird nach der "
                  "erneuten Anmeldung automatisch übermittelt."
                  if request.query_params.get("expired") == "1" else None)
        return page(request, "landing.html", ret=f"/r/{token}/eid", notice=notice, **requester(r))

    # ------------------------------------------------------------------ mock ID Austria (SIMULATED)
    @app.get("/eid/.well-known/jwks.json")
    def jwks():
        return JSONResponse(svc.idp.jwks())

    @app.get("/.well-known/jwks.json")
    def jwks_all():
        """Service statement key + IdP key: verifies Provenance.signature (fhirmap.verify_provenance_signature)."""
        return JSONResponse(svc.jwks())

    @app.get("/static/autosubmit.js")
    def autosubmit_js():
        # served from 'self' so the strict CSP (no inline script) still allows it
        return Response("document.addEventListener('DOMContentLoaded',function(){var f=document.getElementById('handoff');"
                        "if(f){f.submit();}});", media_type="application/javascript")

    @app.get("/static/upload.js")
    def upload_js():
        # file-name display on the upload page (progressive enhancement; the page works without it)
        return Response(_UPLOAD_JS, media_type="application/javascript")

    @app.get("/eid/login", response_class=HTMLResponse)
    def eid_login(request: Request, ret: str = ""):
        ret = request.query_params.get("return", ret)
        if not _RETURN_RE.match(ret):
            return page(request, "closed.html", 400, msg="Ungültige Anmeldeanfrage.")
        return page(request, "eid_login.html", ret=ret, error=None, form={})

    @app.post("/eid/login", response_class=HTMLResponse)
    def eid_login_post(request: Request, ret: str = Form(alias="return"), given: str = Form(""), family: str = Form(""),
                       birthdate: str = Form(""), pin: str = Form("")):
        if not _RETURN_RE.match(ret):
            return page(request, "closed.html", 400, msg="Ungültige Anmeldeanfrage.")
        bd_date = fhirmap.parse_date(birthdate) if birthdate else None
        bd = bd_date.isoformat() if bd_date else ""
        ip, _ = client(request)
        try:
            ident = svc.idp.authenticate(given, family, bd, pin, client=ip or "?")
        except eid.IdPError as e:
            return page(request, "eid_login.html", 422, ret=ret, error=str(e),
                        form={"given": given, "family": family, "birthdate": birthdate})
        token = ret.split("/")[2]
        assertion = svc.idp.issue(ident, aud=hashlib.sha256(token.encode()).hexdigest())
        # hand the token back by POST (auto-submitted form), never in a URL
        return page(request, "eid_handoff.html", action=ret, assertion=assertion)

    @app.get("/r/{token}/eid", response_class=HTMLResponse)
    def eid_callback_get(token: str, request: Request):
        return page(request, "closed.html", 405, msg="Bitte melden Sie sich über die Schaltfläche auf der vorherigen Seite an.")

    @app.post("/r/{token}/eid", response_class=HTMLResponse)
    def eid_callback(token: str, request: Request, assertion: str = Form("")):
        r = svc.by_token(token)
        bad = closed_page(request, r)
        if bad:
            return bad
        ip, ua = client(request)
        if r["eid_attempts"] >= settings.max_eid_attempts_per_token:
            return page(request, "closed.html", 429, msg="Zu viele Anmeldeversuche für diesen Link.")
        try:
            claims = svc.idp.verify(assertion, aud=r["token_hash"])
        except eid.IdPError as e:  # unverified input never counts toward the attempt limit
            svc.audit(r["id"], "identity checked", False, "unbekannt", f"eID-Antwort ungültig ({e}); kein Zugriff.", ip=ip, ua=ua,
                      detail={"error": str(e)})
            return page(request, "closed.html", 400, msg="Die Anmeldung konnte nicht geprüft werden. Bitte erneut versuchen.")
        if not svc.consume_jti(claims["jti"]):  # atomic: the PRIMARY KEY makes a second use fail
            svc.audit(r["id"], "identity checked", False, "unbekannt", "eID-Antwort wiederverwendet (Replay); kein Zugriff.",
                      ip=ip, ua=ua)
            return page(request, "closed.html", 400, msg="Diese Anmeldung wurde bereits verwendet.")
        svc.store.exec("UPDATE requests SET eid_attempts=eid_attempts+1 WHERE id=?", (r["id"],))
        dob = svc.idp.userinfo_birthdate(claims["jti"])  # server-side only (identity rule v2), never in token/FHIR
        rel = svc.eid_check(svc.store.get_request(r["id"]), claims, ip, ua, dob)
        if rel == "review":  # new identity for a known recipient: nothing shown, Dr. Hofer confirms
            return page(request, "identity_review.html", 403, ref=r["ref_code"], **requester(r))
        if rel != "match":
            return page(request, "blocked.html", 403, recipient=r["recipient_name"], ref=r["ref_code"],
                        eid_name=f"{claims['given_name']} {claims['family_name']}", **requester(r))
        sid = svc.new_session(svc.store.get_request(r["id"]), claims, assertion, rel, dob)
        resp = RedirectResponse(f"/r/{token}/resume", status_code=303)
        resp.set_cookie(SID_COOKIE, sid, max_age=settings.session_ttl_s, path=f"/r/{token}", httponly=True, samesite="lax",
                        secure=settings.base_url.startswith("https"))
        return resp

    @app.post("/api/eid/personas", dependencies=[Depends(api_auth)])
    def add_persona(body: dict):
        """Operator: enrol a new test person in the simulated ID Austria; the PIN is returned once."""
        try:
            return svc.idp.add_persona(body.get("given", ""), body.get("family", ""), body.get("birthdate", ""), body.get("pin"))
        except eid.IdPError as e:
            raise ApiError(422, str(e)) from e

    def answer_ctx(r: dict, sess: dict, sid: str) -> dict:
        pat = svc.patient(r["patient_id"]) or {}
        bd = pat.get("birthDate")
        until = datetime.fromisoformat(sess["expires_at"]).astimezone(VIENNA).strftime("%H:%M")
        return {"r": r, "sess": sess, "csrf": svc.csrf(sid), "patient_name": fhir_patient_name(pat) if pat else "?",
                "patient_label": {"female": "Patientin", "male": "Patient"}.get(pat.get("gender"), "Patient/in"),
                "patient_dob": f"{bd[8:10]}.{bd[5:7]}.{bd[0:4]}" if bd else "?", "doc_label": DOC_KIND_LABEL[r["doc_kind"]],
                "request_text": r.get("request_text"), "session_until": until, **requester(r),
                "error": None, "notice": None, "photo_intake": svc.extractor is not None}

    def handle_upload(request: Request, r: dict, sess: dict, sid: str, token: str, raw: bytes, filename: str | None):
        """Shared by the upload POST and the resume after re-login."""
        ctx = answer_ctx(r, sess, sid)
        ip, ua = client(request)
        res = svc.receive_answer(r, sess, raw, filename, ip, ua)
        if res["outcome"] == "block":
            notice = block_notice(ctx, res["block_kind"], res.get("codes") or [])
            return page(request, "answer.html", 422, **{**ctx, "error": None, "notice": notice})
        return received_page(request, r, sid, token)

    def block_notice(ctx: dict, block_kind: str, codes: list[str]) -> dict:
        """What the sender is told about a refused file (answer page AND status page, same words): a headline, what
        happened, and what to do (if the reason text does not say it already)."""
        if block_kind == "wrong_patient":
            return {"title": "Falsches Dokument.",
                    "text": f"Dieses Dokument betrifft nicht {ctx['patient_name']} (geb. {ctx['patient_dob']}): nicht gespeichert, "
                            f"nicht abgelegt, von niemandem gesehen.",
                    "next": "Bitte laden Sie das richtige Dokument hoch."}
        known = [c for c in codes if c in SENDER_DE]
        if not known:
            return {"title": "Nicht übernommen.", "text": "Die Datei wurde abgewiesen und nicht gespeichert.", "next": None}
        return {"title": "Nicht übernommen.", "next": None,
                "text": " ".join(SENDER_DE[c] for c in dict.fromkeys(known)) + " Die Datei wurde nicht gespeichert."}

    def received_page(request: Request, r: dict, sid: str, token: str):
        st = svc.session_state(sid) or {"state": "processing"}
        return page(request, "received.html", state=st["state"], ref=r["ref_code"], status_url=f"/r/{token}/status",
                    receipt=svc.receipt(sid), receipt_url=f"/r/{token}/receipt.txt")

    @app.get("/r/{token}/resume", response_class=HTMLResponse)
    def resume(token: str, request: Request):
        """After (re-)login: a file kept over an expired session is submitted now (same person only)."""
        r = svc.by_token(token)
        bad = closed_page(request, r)
        if bad:
            return bad
        sid = request.cookies.get(SID_COOKIE)
        sess = svc.session(r, sid)
        if not sess:
            return RedirectResponse(f"/r/{token}", status_code=303)
        raw = svc.take_pending_upload(r, sess)
        if raw is None or r["status"] not in ANSWERABLE:
            return RedirectResponse(f"/r/{token}/answer", status_code=303)
        return handle_upload(request, r, sess, sid, token, raw, None)

    @app.post("/r/{token}/decline", response_class=HTMLResponse)
    def decline(token: str, request: Request, csrf: str = Form(""), reason: str = Form("")):
        """'Kein passender Befund / nicht zuständig' (optional short reason)."""
        r = svc.by_token(token)
        bad = closed_page(request, r)
        if bad:
            return bad
        sid = request.cookies.get(SID_COOKIE)
        sess = svc.session(r, sid)
        if not sess or not hmac.compare_digest(csrf, svc.csrf(sid)):
            return RedirectResponse(f"/r/{token}", status_code=303)
        if r["status"] not in ANSWERABLE:
            return page(request, "closed.html", 409, msg="Diese Anfrage nimmt keine Antwort mehr an.")
        ip, ua = client(request)
        svc.decline(r, sess, reason, ip, ua)
        return page(request, "received.html", state="received", ref=r["ref_code"], status_url=f"/r/{token}/status",
                    receipt=svc.receipt(sid), receipt_url=f"/r/{token}/receipt.txt", declined=True)

    @app.get("/r/{token}/receipt.txt")
    def receipt_txt(request: Request, token: str):
        """Eingangsbestätigung zum Speichern: Referenz, Zeit, SHA-256 und Größe der gesendeten Datei. Keine Patientendaten."""
        rc = svc.receipt(request.cookies.get(SID_COOKIE))
        if rc is None:
            return Response("Keine Eingangsbestätigung verfügbar (Sitzung abgelaufen).\n", media_type="text/plain; charset=utf-8",
                            status_code=404)
        if rc["blocked"]:  # a refused file is not delivered: no receipt
            return Response(f"Keine Eingangsbestätigung: Die zuletzt gesendete Datei zu {rc['ref']} wurde abgewiesen, "
                            f"nicht gespeichert und nicht übermittelt.\n", media_type="text/plain; charset=utf-8",
                            status_code=404)
        body = (f"Eingangsbestätigung – {rc['clinic']}\n\nReferenz: {rc['ref']}\nEingegangen: {rc['at']}\n"
                + ("Antwort: kein passender Befund vorhanden / nicht zuständig\n" if rc["declined"] else
                   f"Datei: {rc['size']} Bytes, SHA-256 {rc['sha256']}\n")
                + f"Übermittelt an: {rc['to']}, {rc['clinic']}\n")
        return Response(body, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="eingangsbestaetigung-{rc["ref"]}.txt"'})

    @app.get("/r/{token}/answer", response_class=HTMLResponse)
    def answer_page(token: str, request: Request):
        r = svc.by_token(token)
        bad = closed_page(request, r)
        if bad:
            return bad
        sid = request.cookies.get(SID_COOKIE)
        sess = svc.session(r, sid)
        if not sess:
            return RedirectResponse(f"/r/{token}", status_code=303)
        if r["status"] == "processing":
            return received_page(request, r, sid, token)
        return page(request, "answer.html", **answer_ctx(r, sess, sid))

    @app.get("/r/{token}/status", response_class=HTMLResponse)
    def answer_status(token: str, request: Request):
        """'wird geprüft…' -> 'erhalten'. Session-scoped; reveals nothing beyond what the page showed already."""
        sid = request.cookies.get(SID_COOKIE)
        st = svc.session_state(sid)
        if st is None:
            rc = svc.receipt(sid)
            return page(request, "closed.html", 404, msg=("Sitzung abgelaufen. Ihre Antwort wurde bereits übermittelt."
                                                          if rc and not rc["blocked"] else
                                                          "Sitzung abgelaufen. Bitte öffnen Sie den Link erneut."))
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"state": st["state"], "ref": st["ref"]})
        if st["state"] == "blocked":
            # the same rejection as the answer page, and the form again: the link stays usable for a corrected file
            r = svc.by_token(token)
            sess = svc.session(r, sid) if r else None
            if not r or not sess or closed_page(request, r):
                return page(request, "closed.html", 422, msg="Die zuletzt gesendete Datei wurde abgewiesen und nicht übermittelt.")
            ctx = answer_ctx(r, sess, sid)
            return page(request, "answer.html", 422,
                        **{**ctx, "notice": block_notice(ctx, st["block_kind"], st["codes"])})
        return page(request, "received.html", state=st["state"], ref=st["ref"], status_url=f"/r/{token}/status",
                    receipt=svc.receipt(sid), receipt_url=f"/r/{token}/receipt.txt")

    @app.post("/r/{token}/answer", response_class=HTMLResponse)
    def answer_post(token: str, request: Request, csrf: str = Form(""), file: UploadFile | None = File(None)):
        """Upload only (no typed form): the patient check runs on the document's own patient line."""
        r = svc.by_token(token)
        bad = closed_page(request, r)
        if bad:
            return bad
        sid = request.cookies.get(SID_COOKIE)
        sess = svc.session(r, sid)
        if not sess:
            old = svc.expired_session(r, sid)
            if old and sid and hmac.compare_digest(csrf, svc.csrf(sid)) and file is not None and file.filename \
                    and r["status"] in ANSWERABLE:
                svc.keep_upload_for_relogin(r, old, file.file.read(upload_limit() + 1))
                return RedirectResponse(f"/r/{token}?expired=1", status_code=303)
            return RedirectResponse(f"/r/{token}", status_code=303)
        if not hmac.compare_digest(csrf, svc.csrf(sid)):
            return RedirectResponse(f"/r/{token}", status_code=303)
        if r["status"] == "processing":
            return received_page(request, r, sid, token)
        if r["status"] not in ANSWERABLE:
            return page(request, "closed.html", 409, msg="Diese Anfrage nimmt keine Antwort mehr an.")
        ctx = answer_ctx(r, sess, sid)
        photo = svc.extractor is not None   # photo intake (PGK_EXTRACTOR=anthropic): JPEG, PNG, PDF too
        if file is None or not file.filename:
            return page(request, "answer.html", 422, **{**ctx, "error": "Bitte eine Datei (.md oder .txt) auswählen."
                        if not photo else "Bitte eine Datei oder ein Foto auswählen."})
        filename = Path(file.filename).name[:200]
        if not re.search(r"\.(md|markdown|txt|jpe?g|png|pdf)$" if photo else r"\.(md|markdown|txt)$", filename, re.I):
            return page(request, "answer.html", 422, **{**ctx, "error": "Bitte eine .md- oder .txt-Datei hochladen."
                        if not photo else "Bitte eine .md- oder .txt-Datei, ein Foto (JPEG/PNG) oder ein PDF hochladen."})
        raw = file.file.read(upload_limit() + 1)  # sync endpoint (threadpool); +1 byte: oversize detected
        return handle_upload(request, r, sess, sid, token, raw, filename)

    return app


_app: FastAPI | None = None


def __getattr__(name: str):  # lazy module attribute so importing create_app has no side effects
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)

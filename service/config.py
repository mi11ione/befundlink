"""Runtime settings, all overridable via environment variables (PGK_*)."""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVICE_DIR.parent

class ConfigError(RuntimeError):
    pass


def check_secrets(s: "Settings") -> None:
    """Fail closed. Without PGK_DEV=1 the service refuses to start without its secrets."""
    if s.dev:
        if not s.secret:
            s.secret = secrets.token_bytes(32)
        if not s.bpk_salt:
            s.bpk_salt = "pgk-bridge-dev-only-salt"
        return
    problems = []
    if not s.api_key:
        problems.append("PGK_API_KEY is not set")
    if len(s.secret) < 32:
        problems.append("PGK_SECRET must be at least 32 bytes")
    if len(s.bpk_salt) < 16:
        problems.append("PGK_BPK_SALT must be set (>= 16 characters)")
    if problems:
        raise ConfigError("refusing to start (fail closed): " + "; ".join(problems) +
                          ". Use `make run` (generates .env) or set PGK_DEV=1 for local development.")


def _state() -> dict:
    """infra/state.json (written by infra/setup.py): homeserver, room, bot token of the local stack."""
    try:
        state = json.loads((REPO_DIR / "infra" / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _default_room_id() -> str | None:
    return _state().get("room_id")


def _audit_room_id() -> str | None:
    """The room for the audit-chain anchors (infra/setup.py: 'Befundlink Audit'); else Dr. Hofer's room."""
    return os.environ.get("PGK_AUDIT_ROOM_ID") or _state().get("audit_room_id") or _state().get("room_id")


def _matrix_url() -> str | None:
    return os.environ.get("PGK_MATRIX_URL") or _state().get("homeserver")


def _matrix_token() -> str | None:
    """Read access to the room for the anchor verifier. Locally the bot's token from state.json."""
    return os.environ.get("PGK_MATRIX_TOKEN") or (_state().get("tokens") or {}).get("bot")


@dataclass
class Settings:
    base_url: str = field(default_factory=lambda: os.environ.get("PGK_BASE_URL", "http://localhost:8000").rstrip("/"))
    hapi_url: str = field(default_factory=lambda: os.environ.get("PGK_HAPI_URL", "http://localhost:8090/fhir").rstrip("/"))
    bot_url: str = field(default_factory=lambda: os.environ.get("PGK_BOT_URL", "http://localhost:8001").rstrip("/"))
    smtp_host: str = field(default_factory=lambda: os.environ.get("PGK_SMTP_HOST", "localhost"))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("PGK_SMTP_PORT", "1025")))
    mail_from: str = field(default_factory=lambda: os.environ.get("PGK_MAIL_FROM", "no-reply@klinikum.example"))
    db_path: str = field(default_factory=lambda: os.environ.get("PGK_DB", str(SERVICE_DIR / "data" / "pgk.sqlite")))
    # HMAC key for the mock ID Austria assertions and session CSRF tokens. Random per process
    # unless set; never commit a real value.
    secret: bytes = field(default_factory=lambda: os.environ.get("PGK_SECRET", "").encode())
    api_key: str | None = field(default_factory=lambda: os.environ.get("PGK_API_KEY") or None)
    # shared secret for POST /notify to the bot (X-PGK-Notify-Key)
    notify_key: str | None = field(default_factory=lambda: os.environ.get("PGK_NOTIFY_KEY") or None)
    # PGK_DEMO=1: demo shortcuts (a simulated ID Austria for new outside people; SMS inbox from localhost without API key)
    demo: bool = field(default_factory=lambda: os.environ.get("PGK_DEMO", "") == "1")
    # PGK_DEV=1: allow starting without the required secrets (tests/dev only)
    dev: bool = field(default_factory=lambda: os.environ.get("PGK_DEV", "") == "1")
    clinic_name: str = "Klinikum Beispielstadt"
    token_ttl_days: int = 7
    session_ttl_s: int = 60 * 60  # >= 60 min: time to find and export the file after signing in
    assertion_ttl_s: int = 300
    max_failed_eid_per_token: int = 5
    max_eid_attempts_per_token: int = 20
    max_upload_bytes: int = 1_000_000
    # Photo intake (off by default): PGK_EXTRACTOR=anthropic accepts JPEG/PNG/PDF and has each read twice by the model
    # (service/extractor.py). The ONLY place health data leaves the stack, so it never runs unless set.
    extractor: str = field(default_factory=lambda: os.environ.get("PGK_EXTRACTOR", "").strip().lower())
    max_photo_bytes: int = 5_000_000  # the API's per-image limit
    default_room_id: str | None = field(default_factory=_default_room_id)
    audit_room_id: str | None = field(default_factory=_audit_room_id)  # anchors: never between patient cards
    # Matrix read access for verifying audit anchors in the room (PGK_MATRIX_URL/TOKEN; local default: infra/state.json)
    matrix_url: str | None = field(default_factory=_matrix_url)
    matrix_token: str | None = field(default_factory=_matrix_token)
    registers_path: str = field(default_factory=lambda: os.environ.get("PGK_REGISTERS", str(SERVICE_DIR / "data" / "registers.json")))
    # bPK is stored only as a salted hash. Set PGK_BPK_SALT in any real deployment.
    bpk_salt: str = field(default_factory=lambda: os.environ.get("PGK_BPK_SALT", ""))
    # AES-GCM key for held uploads (32 bytes hex). Default: generated once into data/.datakey (gitignored).
    data_key_hex: str | None = field(default_factory=lambda: os.environ.get("PGK_DATA_KEY") or None)
    held_ttl_days: int = 7
    # Include the outside-person link in API responses (for automated e2e runs only; off by default).
    debug_links: bool = field(default_factory=lambda: os.environ.get("PGK_DEBUG_LINKS", "") == "1")
    # HL7 validator: "tx" = terminology-checked when tx.fhir.org is reachable, else offline (never holds a correct
    # answer for infrastructure reasons; tx re-validation is scheduled) | "offline" = never use tx
    validate_mode: str = field(default_factory=lambda: os.environ.get("PGK_VALIDATE_MODE", "tx"))
    # background execution (outbox + answer worker threads); tests run everything inline
    async_io: bool = field(default_factory=lambda: os.environ.get("PGK_ASYNC_IO", "1") == "1")
    # long-lived HL7 validator (validator_cli.jar server <port>) instead of a cold JVM per filing
    validator_server: bool = field(default_factory=lambda: os.environ.get("PGK_VALIDATOR_SERVER", "1") == "1")
    validator_port: int = field(default_factory=lambda: int(os.environ.get("PGK_VALIDATOR_PORT", "18765")))
    prewarm_tx: bool = field(default_factory=lambda: os.environ.get("PGK_PREWARM_TX", "1") == "1")

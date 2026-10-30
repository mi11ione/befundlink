"""SQLite persistence: requests, audit log, SMS outbox, sessions, used assertions, known domains, answers."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  ref_code TEXT UNIQUE NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,
  requester_mxid TEXT NOT NULL,
  room_id TEXT,
  recipient_name TEXT NOT NULL,
  recipient_role TEXT,
  recipient_org TEXT,
  channel TEXT NOT NULL,
  address TEXT NOT NULL,
  patient_id TEXT NOT NULL,
  patient_snapshot TEXT,
  doc_kind TEXT NOT NULL,
  note TEXT,
  status TEXT NOT NULL,
  warnings TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  sent_at TEXT,
  bound_bpk TEXT,
  bound_name TEXT,
  eid_attempts INTEGER NOT NULL DEFAULT 0,
  eid_failures INTEGER NOT NULL DEFAULT 0,
  misdeliveries INTEGER NOT NULL DEFAULT 0,
  filed TEXT,
  last_reason TEXT,
  register TEXT,
  task_id TEXT,
  task_status TEXT,
  task_error TEXT,
  held_kind TEXT
);
-- append-only, SHA-256 hash chain (hash covers every column + prev_hash). Never UPDATEd or DELETEd.
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  request_id TEXT,
  step TEXT NOT NULL,
  outcome TEXT NOT NULL,
  who TEXT NOT NULL,
  what TEXT NOT NULL,
  at TEXT NOT NULL,
  ip TEXT,
  user_agent TEXT,
  detail TEXT,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_req ON audit(request_id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
-- delivery state of each audit entry to HAPI (kept separate so audit rows stay immutable)
CREATE TABLE IF NOT EXISTS audit_fhir (
  audit_id INTEGER PRIMARY KEY,
  fhir_id TEXT,
  fhir_hash TEXT,          -- fhirmap.audit_hash of the AuditEvent (FHIR-side chain link)
  event TEXT,              -- the exact AuditEvent JSON posted (re-posted unchanged on retry)
  error TEXT,
  at TEXT NOT NULL
);
-- the stored AuditEvent copy and its hash are immutable; only delivery state (fhir_id, error) may change
CREATE TRIGGER IF NOT EXISTS audit_fhir_no_rewrite BEFORE UPDATE OF audit_id, event, fhir_hash, at ON audit_fhir
BEGIN SELECT RAISE(ABORT, 'audit_fhir event is immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_fhir_no_delete BEFORE DELETE ON audit_fhir
BEGIN SELECT RAISE(ABORT, 'audit_fhir is append-only'); END;
-- external anchors of the chain head (Matrix room + HAPI AuditEvent). Append-only: head and hash never
-- change, a row is never deleted; the external references (fhir_ref, matrix_event) are written once.
CREATE TABLE IF NOT EXISTS audit_anchors (
  head_id INTEGER PRIMARY KEY,
  head_hash TEXT NOT NULL,
  at TEXT NOT NULL,
  matrix_ok INTEGER,
  fhir_ref TEXT,
  matrix_event TEXT
);
CREATE TRIGGER IF NOT EXISTS anchors_no_rewrite BEFORE UPDATE OF head_id, head_hash, at ON audit_anchors
BEGIN SELECT RAISE(ABORT, 'audit_anchors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS anchors_no_delete BEFORE DELETE ON audit_anchors
BEGIN SELECT RAISE(ABORT, 'audit_anchors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS anchors_fhir_once BEFORE UPDATE OF fhir_ref ON audit_anchors WHEN OLD.fhir_ref IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'anchor fhir_ref is written once'); END;
-- cleartext that an audit row refers to only by a keyed hash (the recipient's e-mail address / phone number).
-- Outside the chain on purpose: deleted at link expiry (retention.py) while the chained row keeps the hash.
CREATE TABLE IF NOT EXISTS audit_private (
  audit_id INTEGER PRIMARY KEY,
  request_id TEXT,
  contact TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_private_no_update BEFORE UPDATE ON audit_private
BEGIN SELECT RAISE(ABORT, 'audit_private can only be deleted (redaction), not changed'); END;
CREATE TABLE IF NOT EXISTS sms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT,
  to_number TEXT NOT NULL,
  text TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  sid_hash TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  bpk TEXT NOT NULL,
  given TEXT NOT NULL,
  family TEXT NOT NULL,
  birth_date TEXT,
  loa TEXT NOT NULL,
  verdict TEXT NOT NULL DEFAULT 'match',
  statement TEXT,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS used_assertions (
  jti TEXT PRIMARY KEY,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS known_domains (
  domain TEXT NOT NULL,
  org TEXT,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (domain, org)
);
-- identity memory (namesake protection): which ID Austria subjects answered (FILED) for a recipient, keyed by the
-- normalised recipient NAME + organisation (any contact). birth_year: the year only (never the date), for the
-- identity review card ("anderes Geburtsjahr als bisher").
CREATE TABLE IF NOT EXISTS recipient_identities (
  rkey TEXT NOT NULL,
  sub TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  birth_year TEXT,
  PRIMARY KEY (rkey, sub)
);
-- upload whose session expired while the person was answering: kept encrypted until re-login (same person)
CREATE TABLE IF NOT EXISTS pending_uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  bpk TEXT NOT NULL,
  blob BLOB NOT NULL,
  encrypted INTEGER NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  sender_bpk TEXT,
  sender_name TEXT,
  sender_verdict TEXT,
  identity TEXT,
  filename TEXT,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  content BLOB,            -- AES-GCM encrypted when `cryptography` is available; NULL once filed/discarded/rejected
  encrypted INTEGER NOT NULL DEFAULT 0,
  purge_after TEXT,
  outcome TEXT,
  reasons TEXT
);
"""

JSON_COLS = {"warnings", "patient_snapshot", "filed", "detail", "reasons", "register", "identity"}


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            make_private_dir(Path(path).parent)
            make_private_file(Path(path))  # created 0600 BEFORE SQLite opens it; WAL/SHM inherit the mode
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        if path != ":memory:":
            for side_file in (path + "-wal", path + "-shm"):  # a DB from before this check: tighten as well
                if os.path.exists(side_file):
                    os.chmod(side_file, 0o600)
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self) -> None:
        if "matrix_event" not in self._columns("audit_anchors"):  # migration: Matrix event id of the anchor
            self._conn.execute("ALTER TABLE audit_anchors ADD COLUMN matrix_event TEXT")
        self._conn.execute("CREATE TRIGGER IF NOT EXISTS anchors_matrix_once BEFORE UPDATE OF matrix_event ON audit_anchors "
                           "WHEN OLD.matrix_event IS NOT NULL BEGIN SELECT RAISE(ABORT, 'anchor matrix_event is written once'); END")
        cols = self._columns("audit")
        rcols = self._columns("requests")
        for col in ("identity_pending_sub", "identity_pending_name", "identity_approved_sub"):
            if col not in rcols:  # migration: identity confirmation by the requester (new eID for a known recipient)
                self._conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT")
        if "request_text" not in rcols:  # migration: what is asked, for the outside person (shown after eID only)
            self._conn.execute("ALTER TABLE requests ADD COLUMN request_text TEXT")
        if "task_seq" not in rcols:  # migration: per-request sequence of Task status changes (monotonic writes)
            self._conn.execute("ALTER TABLE requests ADD COLUMN task_seq INTEGER NOT NULL DEFAULT 0")
        if "birth_year" not in self._columns("recipient_identities"):  # migration: identity review card
            self._conn.execute("ALTER TABLE recipient_identities ADD COLUMN birth_year TEXT")
        for col in ("bound_birth_year", "identity_pending_year", "identity_rejected_sub", "sig_alerted"):
            if col not in rcols:  # migration: namesake review (year only, never the birth date)
                self._conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT")
        if "content_raw" not in self._columns("answers"):  # migration: 1 = content holds the RAW upload bytes
            self._conn.execute("ALTER TABLE answers ADD COLUMN content_raw INTEGER NOT NULL DEFAULT 0")
        if "task_confirmed" not in rcols:  # migration: final Task seen on HAPI as expected -> reconcile skips it
            self._conn.execute("ALTER TABLE requests ADD COLUMN task_confirmed INTEGER NOT NULL DEFAULT 0")
        if "alg" not in cols:  # migration: rows written before the chain was keyed stay plain SHA-256
            self._conn.execute("ALTER TABLE audit ADD COLUMN alg TEXT NOT NULL DEFAULT 'sha256'")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid

    def exec_count(self, sql: str, args: tuple = ()) -> int:
        """Like exec, but returns the number of rows changed."""
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    def insert(self, table: str, data: dict[str, Any]) -> int:
        cols = list(data)
        vals = [json.dumps(v, ensure_ascii=False) if c in JSON_COLS and v is not None else v for c, v in data.items()]
        return self.exec(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(vals))

    def update_request(self, rid: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v, ensure_ascii=False) if k in JSON_COLS and v is not None else v for k, v in fields.items()]
        self.exec(f"UPDATE requests SET {sets} WHERE id=?", (*vals, rid))

    def get_request(self, rid: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM requests WHERE id=?", (rid,))

    def request_by_token_hash(self, th: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM requests WHERE token_hash=?", (th,))


def make_private_dir(d: Path) -> None:
    """The service's data directory (DB, keys): owner only (0700)."""
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)


def make_private_file(p: Path) -> None:
    """Create `p` with mode 0600 if it does not exist (never readable by others, not even briefly); tighten it if
    it does."""
    fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(p, 0o600)


def write_private(p: Path, data: bytes) -> None:
    """Write a NEW secret file (key material) that is 0600 from the moment it exists."""
    make_private_dir(p.parent)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for c in JSON_COLS & d.keys():
        if d[c] is not None:
            d[c] = json.loads(d[c])
    return d

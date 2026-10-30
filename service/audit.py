"""Tamper-evident service audit: append-only SQLite rows, chained, KEYED.

hash_n = HMAC-SHA256(k_audit, canonical JSON of {id, request_id, step, outcome, who, what, at, ip, user_agent,
         detail, prev_hash}); k_audit = HKDF(PGK_SECRET, "audit-chain"). prev_hash_1 = 64 x '0'.
Rows written before the chain was keyed carry alg 'sha256' (plain SHA-256); once a keyed row appears, every later
row must be keyed (no downgrade). The head is anchored periodically outside the service (Matrix room + a create-only
HAPI AuditEvent, service/anchors.py) and `audit_verify --fhir` reads both back, so even a key holder cannot silently
rewrite history before the last anchor. The FHIR AuditEvent copies (six
steps, fhirmap's audit-step CodeSystem and chain) are verified separately (Service.verify_audit(check_fhir=True)).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

GENESIS = "0" * 64
FIELDS = ("id", "request_id", "step", "outcome", "who", "what", "at", "ip", "user_agent", "detail", "prev_hash")
SIX_STEPS = ["request sent", "link opened", "identity checked", "answer sent", "answer filed", "misdelivery caught"]
KEYED = "hmac-sha256"


def canonical(row: dict[str, Any]) -> bytes:
    return json.dumps({k: row.get(k) for k in FIELDS}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def entry_hash(row: dict[str, Any], key: bytes | None = None) -> str:
    if (row.get("alg") or "sha256") == KEYED:
        if key is None:
            raise ValueError("keyed audit row needs the audit key")
        return hmac.new(key, canonical(row), hashlib.sha256).hexdigest()
    return hashlib.sha256(canonical(row)).hexdigest()


def verify_rows(rows: list[dict[str, Any]], key: bytes | None = None, start_prev: str = GENESIS,
                start_id: int = 1, keyed_seen: bool = False) -> dict[str, Any]:
    """rows ordered by id, `detail` already decoded. Returns {"ok", "count", "head", "problems"}."""
    problems = []
    prev, expected_id = start_prev, start_id
    for r in rows:
        if r["id"] != expected_id:
            problems.append(f"gap: expected id {expected_id}, found {r['id']}")
        if r["prev_hash"] != prev:
            problems.append(f"id {r['id']}: prev_hash does not match hash of previous entry")
        if (r.get("alg") or "sha256") == KEYED:
            keyed_seen = True
        elif keyed_seen:
            problems.append(f"id {r['id']}: unkeyed entry after keyed entries (downgrade)")
        try:
            ok = hmac.compare_digest(entry_hash(r, key), r["hash"])
        except ValueError as e:
            ok = False
            problems.append(f"id {r['id']}: {e}")
        if not ok:
            problems.append(f"id {r['id']}: content was modified (hash mismatch)")
        prev = r["hash"]
        expected_id = r["id"] + 1
    return {"ok": not problems, "count": len(rows), "head": prev, "problems": problems, "keyed_seen": keyed_seen}

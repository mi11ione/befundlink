"""External anchors of the audit chain head.

Every MAINTENANCE_EVERY_S the head of the keyed audit chain (entry id + hash) is published OUTSIDE the service,
in two independent places:

- HAPI: as an AuditEvent. Our HAPI runs pgk.AppendOnlyInterceptor, so an AuditEvent can be created but never
  updated or deleted (a Basic, as before, could be overwritten with a PUT).
- Matrix: as a message from the bot in Dr. Hofer's room.

The verifier reads the anchors back FROM THE EXTERNAL SYSTEM (a search on HAPI, the room history on Matrix), not via
the local `audit_anchors` table, which an insider holding the database could rewrite or empty. Each anchor found
there must equal the chain's hash at that entry. An anchor for an entry the chain no longer has (truncation) or with
another hash (rewrite) is a disagreement.

Anchors carry a chain id derived from the audit key, so anchors of another installation (another key) that write to
the same HAPI or room are ignored rather than reported as disagreements. Each anchor also carries a MAC (HMAC-SHA256
under the audit key over chain, id and hash): HAPI is writable by anyone who reaches it and anyone in the room can
post text, so an anchor without a valid MAC is "ignored (unauthenticated)", never counted as tampering.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from urllib.parse import quote

import httpx

SID_ANCHOR = "https://pgk-bridge.example/fhir/sid/audit-anchor"
CS_ANCHOR = "https://pgk-bridge.example/fhir/CodeSystem/audit-anchor"
ANCHOR_CODE = "chain-head"
MATRIX_PREFIX = "[AUDIT-ANKER]"
_MATRIX_RE = re.compile(r"^\[AUDIT-ANKER\] Audit-Kette (?P<chain>[0-9a-f]{16}) bis Eintrag #(?P<id>\d+): "
                        r"(?P<hash>[0-9a-f]{64})(?: \(MAC (?P<mac>[0-9a-f]{32})\))?")
MAX_PAGES = 200  # safety stop when paging through HAPI search results or the room history


def chain_id(k_audit: bytes) -> str:
    """Identifies this installation's chain (16 hex). Derived from the audit key, so rewriting the database
    cannot change it."""
    return hmac.new(k_audit, b"pgk-bridge anchor chain id", hashlib.sha256).hexdigest()[:16]


def anchor_mac(k_audit: bytes, chain: str, head_id: int, head_hash: str) -> str:
    """Authenticates an anchor as ours (32 hex)."""
    return hmac.new(k_audit, f"anchor:{chain}:{head_id}:{head_hash}".encode(), hashlib.sha256).hexdigest()[:32]


def anchor_event(chain: str, head_id: int, head_hash: str, at: str, mac: str) -> dict:
    """The chain head as a FHIR R4 AuditEvent."""
    return {
        "resourceType": "AuditEvent",
        "type": {"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "110100",
                 "display": "Application Activity"},
        "subtype": [{"system": CS_ANCHOR, "code": ANCHOR_CODE, "display": "Audit chain head anchor"}],
        "action": "E",
        "recorded": at,
        "outcome": "0",
        "outcomeDesc": f"Befundlink audit chain {chain} head #{head_id}",
        "agent": [{"requestor": False, "who": {"display": "Befundlink"}}],
        "source": {"site": "pgk-bridge", "observer": {"display": "Befundlink, Klinikum Beispielstadt"}},
        "entity": [{
            "what": {"identifier": {"system": SID_ANCHOR, "value": f"{head_id}:{head_hash}"}},
            "name": f"audit chain {chain}",
            "detail": [{"type": "chain", "valueString": chain},
                       {"type": "head_id", "valueString": str(head_id)},
                       {"type": "head_hash", "valueString": head_hash},
                       {"type": "mac", "valueString": mac}],
        }],
    }


def matrix_text(chain: str, head_id: int, head_hash: str, mac: str) -> str:
    return f"{MATRIX_PREFIX} Audit-Kette {chain} bis Eintrag #{head_id}: {head_hash} (MAC {mac})"


def parse_anchor_event(ev: dict, chain: str) -> tuple[int, str, str | None] | None:
    """(head_id, head_hash, mac) of an anchor AuditEvent of this chain; None for anything else."""
    for entity in ev.get("entity") or []:
        detail = {d.get("type"): d.get("valueString") for d in entity.get("detail") or []}
        if detail.get("chain") != chain:
            continue
        try:
            return int(detail["head_id"]), str(detail["head_hash"]), detail.get("mac")
        except (KeyError, TypeError, ValueError):
            return None
    return None


def parse_matrix_body(body: str, chain: str) -> tuple[int, str, str | None] | None:
    m = _MATRIX_RE.match(body or "")
    if not m or m.group("chain") != chain:
        return None
    return int(m.group("id")), m.group("hash"), m.group("mac")


def read_hapi_anchors(http: httpx.Client, hapi_url: str, chain: str) -> list[tuple[int, str, str | None]]:
    """All anchor AuditEvents of this chain on HAPI (searched, paged). Returns [(head_id, head_hash, mac)].
    Raises httpx.HTTPError / ValueError when HAPI cannot be read."""
    found = []
    url = f"{hapi_url}/AuditEvent?subtype={quote(CS_ANCHOR + '|' + ANCHOR_CODE, safe='')}&_count=200"
    for _ in range(MAX_PAGES):
        resp = http.get(url, headers={"Accept": "application/fhir+json"}, timeout=15)
        if resp.status_code != 200:
            raise ValueError(f"HAPI anchor search: HTTP {resp.status_code}")
        bundle = resp.json()
        for entry in bundle.get("entry") or []:
            ev = entry.get("resource") or {}
            parsed = parse_anchor_event(ev, chain)
            if parsed:
                found.append(parsed)
        url = next((link["url"] for link in bundle.get("link") or [] if link.get("relation") == "next"), None)
        if not url:
            break
    return found


def read_matrix_anchors(http: httpx.Client, matrix_url: str, token: str, room_id: str, chain: str) -> list[tuple[int, str, str | None]]:
    """All anchor messages of this chain in the room, sent by the bot account (anyone else's are ignored).
    Returns [(head_id, head_hash, mac)]. Raises httpx.HTTPError / ValueError when Matrix cannot be read."""
    auth = {"Authorization": f"Bearer {token}"}
    who = http.get(f"{matrix_url}/_matrix/client/v3/account/whoami", headers=auth, timeout=10)
    if who.status_code != 200:
        raise ValueError(f"Matrix whoami: HTTP {who.status_code}")
    bot_user = who.json()["user_id"]
    room_filter = json.dumps({"types": ["m.room.message"], "senders": [bot_user]}, separators=(",", ":"))
    base = (f"{matrix_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages"
            f"?dir=b&limit=500&filter={quote(room_filter, safe='')}")
    found, page_from = [], None
    for _ in range(MAX_PAGES):
        url = base if page_from is None else f"{base}&from={quote(page_from, safe='')}"
        resp = http.get(url, headers=auth, timeout=15)
        if resp.status_code != 200:
            raise ValueError(f"Matrix room history: HTTP {resp.status_code}")
        page = resp.json()
        for ev in page.get("chunk") or []:
            if ev.get("sender") != bot_user:
                continue
            parsed = parse_matrix_body((ev.get("content") or {}).get("body", ""), chain)
            if parsed:
                found.append(parsed)
        page_from = page.get("end")
        if not page.get("chunk") or not page_from:
            break
    return found


def compare(external: list[tuple[int, str, str | None]], chain_hashes: dict[int, str], local_ids: set[int],
            k_audit: bytes | None = None, chain: str | None = None) -> dict:
    """Each AUTHENTIC external anchor vs the chain. With k_audit, an anchor whose MAC is missing or wrong is
    'ignored_unauthenticated' (anyone can write to HAPI or post in the room) and never counted as tampering.
    'missing' = local anchors with no authentic external copy (not yet published, or removed there); shown, not a
    failure by itself."""
    out = {"found": 0, "ok": 0, "bad": [], "missing": [], "ignored_unauthenticated": 0}
    seen = set()
    for head_id, head_hash, mac in external:
        if k_audit is not None and not hmac.compare_digest(mac or "", anchor_mac(k_audit, chain, head_id, head_hash)):
            out["ignored_unauthenticated"] += 1
            continue
        out["found"] += 1
        seen.add(head_id)
        if chain_hashes.get(head_id) == head_hash:
            out["ok"] += 1
        else:
            out["bad"].append(head_id)
    out["missing"] = sorted(local_ids - seen)
    out["bad"] = sorted(set(out["bad"]))
    return out

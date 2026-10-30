"""Audit: classify AuditEvents into the six brief steps (who/what/when) and verify the SHA-256 chain."""
from __future__ import annotations

import copy
import hmac
import hashlib
import json
import re

STEPS = ["request-sent", "link-opened", "identity-checked", "answer-sent", "answer-filed", "misdelivery-caught"]
_KW = {  # fallback when no step subtype code is present (German and English)
    "request-sent": r"request[ _-]?(sent|created)|anfrage.*(gesendet|versendet|erstellt)|anforderung.*(gesendet|versendet)",
    "link-opened": r"link[ _-]?(opened|geöffnet)|landing|link.*(open|aufgerufen)",
    "identity-checked": r"identity|identität|eid|authenticat|110114|anmeldung",
    "answer-sent": r"answer[ _-]?(sent|submitted|received)|upload|antwort.*(gesendet|eingereicht|empfangen)",
    "answer-filed": r"answer[ _-]?filed|filed|abgelegt|gespeichert|110107|import",
    "misdelivery-caught": r"misdeliver|fehlzustell|110113|security alert|wrong[ _-]?patient|eid[ _-]?mismatch|blocked",
}


def _txt(e: dict) -> str:
    return json.dumps(e, ensure_ascii=False).casefold()


def codes_of(e: dict) -> set[str]:
    """Explicit step codes: AuditEvent.subtype codes or the row's 'step' (e.g. 'filing held' -> 'filing-held')."""
    codes = {re.sub(r"[_ ]", "-", (c.get("code") or "").casefold()) for c in e.get("subtype") or []}
    for k in ("step", "event"):
        if e.get(k):
            codes.add(re.sub(r"[_ ]", "-", str(e[k]).casefold()))
    return {c for c in codes if c}


def steps_of(e: dict) -> set[str]:
    codes = codes_of(e)
    if codes:  # explicit codes are authoritative: never guess from free text ('NICHT abgelegt' != filed)
        return codes & set(STEPS)
    t = _txt({k: v for k, v in e.items() if k not in ("text", "meta")})
    return {s for s, rx in _KW.items() if re.search(rx, t)}


def who_what_when(e: dict) -> dict:
    agents = e.get("agent") or []
    req = next((a for a in agents if a.get("requestor")), agents[0] if agents else {})
    w = req.get("who") or {}
    who = w.get("display") or (w.get("identifier") or {}).get("value") or w.get("reference") or req.get("name") \
        or e.get("who")
    what = " / ".join(x for x in [(e.get("type") or {}).get("display") or (e.get("type") or {}).get("code"),
                                  ", ".join(c.get("display") or c.get("code") or "" for c in e.get("subtype") or []),
                                  e.get("outcomeDesc"), e.get("what")] if x)
    when = e.get("recorded") or e.get("at")
    refs = [(x.get("what") or {}).get("reference") for x in e.get("entity") or [] if (x.get("what") or {}).get("reference")]
    return {"who": who, "what": what, "when": when, "outcome": e.get("outcome"), "entities": refs,
            "purpose": [c.get("code") for p in e.get("purposeOfEvent") or [] for c in p.get("coding") or []]}


def coverage(events: list[dict]) -> dict[str, dict | None]:
    """First complete (who+what+when) event per step."""
    out = {s: None for s in STEPS}
    for e in sorted(events, key=lambda x: x.get("recorded") or x.get("at") or ""):
        w = who_what_when(e)
        for s in steps_of(e):
            if out[s] is None and w["who"] and w["what"] and w["when"]:
                out[s] = {**w, "id": e.get("id")}
    return out


# ------------------------------------------------------------------ hash chain
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _chain_fields(e: dict) -> tuple[str | None, str | None]:
    """(prev_hash, own_hash) from extensions / entity.detail / plain keys."""
    prev = own = None
    for x in e.get("extension") or []:
        url, val = x.get("url", "").casefold(), next((v for k, v in x.items() if k.startswith("value")), None)
        if isinstance(val, str) and HEX64.match(val.casefold()):
            if re.search(r"prev|previous|chain-prev|predecessor", url):
                prev = val.casefold()
            elif re.search(r"hash|self|digest", url):
                own = val.casefold()
    for ent in e.get("entity") or []:
        for d in ent.get("detail") or []:
            t, val = str(d.get("type", "")).casefold(), d.get("valueString") or ""
            if HEX64.match(val.casefold()):
                if re.search(r"prev|chain", t):
                    prev = val.casefold()
                elif re.search(r"^hash$|self|own|entry.?hash", t):
                    own = val.casefold()
    for k in ("prev_hash", "prevHash", "previous_hash"):
        if isinstance(e.get(k), str):
            prev = e[k].casefold()
    for k in ("hash", "entry_hash", "self_hash"):
        if isinstance(e.get(k), str) and HEX64.match(e[k].casefold()):
            own = e[k].casefold()
    return prev, own


def _strip(e: dict, drop: tuple[str, ...], drop_chain: bool) -> dict:
    d = {k: v for k, v in e.items() if k not in drop}
    if drop_chain:
        d.pop("hash", None), d.pop("entry_hash", None), d.pop("self_hash", None)
        if "extension" in d:
            d["extension"] = [x for x in d["extension"] if not re.search(r"hash|chain|prev", x.get("url", ""), re.I)]
            if not d["extension"]:
                d.pop("extension")
    return d


CANON = [  # (name, fields dropped, drop chain fields, ensure_ascii)
    (f"{'+'.join(drop) or 'all'}{'-chain' if dc else ''}{'-ascii' if a else ''}", drop, dc, a)
    for drop in [("meta", "id", "text"), ("meta", "text"), ("meta",), ("id",), ()]
    for dc in (True, False) for a in (False, True)
]


def _h(e: dict, drop, dc, asc, prefix: str = "") -> str:
    s = json.dumps(_strip(e, drop, dc), sort_keys=True, separators=(",", ":"), ensure_ascii=asc)
    return hashlib.sha256((prefix + s).encode()).hexdigest()


def verify_chain(events: list[dict], canon: str | None = None) -> dict:
    """Rebuild the chain from the hash links themselves (timestamps can tie) and check it is ONE unbroken
    sequence: exactly one genesis, every prev-hash resolves to an existing entry, no forks, all reached.
    Tries the known canonical forms unless `canon` is given. Returns {"ok","links","broken","canon","detail"}."""
    fields = [_chain_fields(e) for e in events]
    n_links = sum(1 for p, _ in fields if p)
    if len(events) < 2 or n_links == 0:
        return {"ok": False, "links": 0, "broken": [], "canon": None, "detail": "no chain fields found"}
    best = None
    for name, drop, dc, asc in CANON:
        if canon and name != canon.split(":")[0]:
            continue
        modes = {"plain": [_h(e, drop, dc, asc) for e in events]}
        if all(own for _, own in fields):
            modes["own"] = [own for _, own in fields]
        for mode, hs in modes.items():
            if canon and ":" in canon and mode != canon.split(":")[1]:
                continue
            idx = {h: i for i, h in enumerate(hs)}
            bad = set()
            if mode == "own":  # stored own-hash must be the hash of the entry
                bad |= {i for i, e in enumerate(events) if fields[i][1] not in (_h(e, drop, dc, asc),
                                                                                  _h(e, drop, dc, asc, fields[i][0] or ""))}
            genesis = [i for i, (p, _) in enumerate(fields) if not p]
            children: dict[int, list[int]] = {}
            for i, (p, _) in enumerate(fields):
                if p:
                    if p in idx:
                        children.setdefault(idx[p], []).append(i)
                    else:
                        bad.add(i)  # predecessor missing or edited
            bad |= {c for kids in children.values() if len(kids) > 1 for c in kids[1:]}  # forks
            bad |= set(genesis[1:])
            seen, cur = 0, genesis[0] if genesis else None
            while cur is not None and seen <= len(events):
                seen += 1
                kids = children.get(cur) or []
                cur = kids[0] if kids else None
            if seen != len(events) and not bad:
                bad.add(-1)
            score = (len(bad), name)
            if best is None or score < best[0]:
                best = (score, f"{name}:{mode}", sorted(bad))
    if best is None:
        return {"ok": False, "links": n_links, "broken": [], "canon": None, "detail": "unknown canonical form"}
    (_, _), cname, broken = best
    return {"ok": not broken, "links": n_links, "broken": broken, "canon": cname,
            "detail": "intact" if not broken else f"{len(broken)} broken link(s)"}


def tamper_detected(events: list[dict], canon: str) -> dict:
    """Edit one entry in a COPY and confirm verification fails. The edited entry is one that has a successor:
    in a plain prev-hash chain the newest entry (the head) is only protected if its own hash is stored or the
    head is anchored elsewhere; the service scheme stores each entry's own hash (see verify_rows/fhir)."""
    if len(events) < 3:
        return {"ok": False, "detail": "chain too short to tamper-test"}
    evs = copy.deepcopy(events)
    name = canon.split(":")[0]
    _, drop, dc, asc = next(c for c in CANON if c[0] == name)
    prevs = {_chain_fields(e)[0] for e in evs}
    i = next((k for k, e in enumerate(evs) if _h(e, drop, dc, asc) in prevs), len(evs) // 2)
    key = "outcomeDesc" if "outcomeDesc" in evs[i] or "resourceType" in evs[i] else "what"
    evs[i][key] = str(evs[i].get(key, "")) + " (edited)"
    res = verify_chain(evs, canon)
    return {"ok": not res["ok"] and bool(res["broken"]), "edited_index": i, "broken": res["broken"]}


# ------------------------------------------------------------------ the service's published chain scheme
# service/audit.py docstring: hash_n = HMAC-SHA256(k_audit, canonical JSON of {id, request_id, step, outcome,
# who, what, at, ip, user_agent, detail, prev_hash}) for rows with alg 'hmac-sha256' (plain SHA-256 for older rows,
# never after a keyed row); k_audit = HKDF-SHA256(PGK_SECRET, salt 'pgk-bridge/v1', info 'audit-chain', 32 bytes)
# (service/keys.py); prev_hash_1 = 64 x '0'. Re-implemented here; the harness holds the operator's .env.
ROW_FIELDS = ("id", "request_id", "step", "outcome", "who", "what", "at", "ip", "user_agent", "detail", "prev_hash")
GENESIS = "0" * 64
KEYED = "hmac-sha256"


def hkdf_sha256(master: bytes, info: bytes, salt: bytes = b"pgk-bridge/v1", length: int = 32) -> bytes:
    """RFC 5869 with HMAC-SHA256 (stdlib only)."""
    prk = hmac.new(salt, master, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out, i = out + t, i + 1
    return out[:length]


def audit_key(secret: str | bytes | None) -> bytes | None:
    if not secret:
        return None
    return hkdf_sha256(secret.encode() if isinstance(secret, str) else secret, b"audit-chain")


def row_hash(row: dict, key: bytes | None = None) -> str | None:
    body = json.dumps({k: row.get(k) for k in ROW_FIELDS}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode()
    if (row.get("alg") or "sha256") == KEYED:
        return hmac.new(key, body, hashlib.sha256).hexdigest() if key else None
    return hashlib.sha256(body).hexdigest()


def verify_rows(rows: list[dict], key: bytes | None = None) -> dict:
    """Recompute every hash independently; linkage from genesis, no id gaps, no downgrade after a keyed row.
    A window (first id > 1) is anchored on its first row's prev_hash."""
    rows = sorted(rows, key=lambda r: int(r["id"]))
    problems, prev, keyed = [], GENESIS, False
    if rows and int(rows[0]["id"]) != 1:
        prev = rows[0].get("prev_hash")
    for i, r in enumerate(rows):
        if i and int(r["id"]) != int(rows[i - 1]["id"]) + 1:
            problems.append(f"gap before id {r['id']}")
        if r.get("prev_hash") != prev:
            problems.append(f"id {r['id']}: prev_hash != hash of predecessor")
        if (r.get("alg") or "sha256") == KEYED:
            keyed = True
        elif keyed:
            problems.append(f"id {r['id']}: unkeyed row after keyed rows (downgrade)")
        h = row_hash(r, key)
        if h is None:
            problems.append(f"id {r['id']}: keyed row but no audit key (PGK_SECRET) available")
        elif not hmac.compare_digest(h, r.get("hash") or ""):
            problems.append(f"id {r['id']}: stored hash != recomputed")
        prev = r.get("hash")
    return {"ok": bool(rows) and not problems, "count": len(rows), "problems": problems[:10], "head": prev,
            "keyed_rows": sum((r.get("alg") or "") == KEYED for r in rows)}


def verify_anchors(rows: list[dict], anchors: list[tuple[int, str, str]]) -> dict:
    """Every external anchor (id, hash, where) must equal the verified chain's row hash at that id."""
    by_id = {int(r["id"]): r.get("hash") for r in rows}
    bad = [f"{w}: #{i}" for i, h, w in anchors if by_id.get(int(i)) != h]
    return {"ok": not bad, "anchors": len(anchors), "mismatch": bad[:10]}


def fhir_event_hash(e: dict) -> str:
    """Published scheme (fhirmap.audit_hash docstring): SHA-256 over canonical JSON of the event without
    id/meta and without its own entity.detail 'hash'."""
    x = copy.deepcopy(e)
    x.pop("id", None), x.pop("meta", None)
    for ent in x.get("entity", []):
        if "detail" in ent:
            ent["detail"] = [d for d in ent["detail"] if d.get("type") != "hash"]
            if not ent["detail"]:
                del ent["detail"]
    return hashlib.sha256(json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def verify_fhir_chains(aes: list[dict]) -> dict:
    """Every AuditEvent's stored hash is recomputed; prev links must resolve; no forks. HAPI may hold several
    chains (one per service database); each must be a single unbroken line from its genesis."""
    aes = [e for e in aes if not is_anchor(e)]  # anchors are not chain entries: they publish the head
    own, prev, edited = {}, {}, []
    for e in aes:
        p_, o_ = _chain_fields(e)
        h = fhir_event_hash(e)
        if o_ != h:
            edited.append(f"AuditEvent/{e.get('id')}")
        own[e.get("id")], prev[e.get("id")] = h, p_
    by_hash = {h: i for i, h in own.items()}
    dangling = [f"AuditEvent/{i}" for i, p_ in prev.items() if p_ and p_ not in by_hash]
    kids: dict[str, list] = {}
    for i, p_ in prev.items():
        if p_ in by_hash:
            kids.setdefault(by_hash[p_], []).append(i)
    forks = [f"AuditEvent/{k}" for k, v in kids.items() if len(v) > 1]
    genesis = [i for i, p_ in prev.items() if not p_]
    ok = bool(aes) and not edited and not dangling and not forks
    return {"ok": ok, "events": len(aes), "chains": len(genesis), "edited": edited[:5], "dangling_prev": dangling[:5],
            "forks": forks[:5]}


def is_anchor(e: dict) -> bool:
    return any(c.get("code") == "chain-head" and c.get("system", "").endswith("/CodeSystem/audit-anchor")
               for c in e.get("subtype") or [])


def chain_id(k_audit: bytes) -> str:
    """The installation's anchor chain id (service/anchors.py docstring): HMAC-SHA256(k_audit, 'pgk-bridge anchor
    chain id')[:16 hex]. Anchors of other installations writing to the same HAPI/room carry another id."""
    return hmac.new(k_audit, b"pgk-bridge anchor chain id", hashlib.sha256).hexdigest()[:16]


def anchor_mac(k_audit: bytes, chain: str, head_id: int, head_hash: str) -> str:
    """service/anchors.py docstring: HMAC-SHA256(k_audit, 'anchor:{chain}:{id}:{hash}')[:32 hex]."""
    return hmac.new(k_audit, f"anchor:{chain}:{head_id}:{head_hash}".encode(), hashlib.sha256).hexdigest()[:32]


def split_authentic(anchors: list[tuple], k_audit: bytes | None, chain: str) -> tuple[list, list]:
    """(authentic, ignored): only anchors whose MAC verifies under the audit key count (others: unauthenticated)."""
    good, bad = [], []
    for i, h, where, mac in anchors:
        (good if k_audit and mac and hmac.compare_digest(mac, anchor_mac(k_audit, chain, i, h)) else bad).append(
            (i, h, where))
    return good, bad


def hapi_anchors(aes: list[dict], chain: str) -> list[tuple[int, str, str, str | None]]:
    out = []
    for e in aes:
        if not is_anchor(e):
            continue
        ent = (e.get("entity") or [{}])[0]
        det = {d.get("type"): d.get("valueString") for d in ent.get("detail") or []}
        val = ((ent.get("what") or {}).get("identifier") or {}).get("value") or ""
        if det.get("chain") == chain and re.fullmatch(r"\d+:[0-9a-f]{64}", val):
            i, h = val.split(":")
            out.append((int(i), h, f"HAPI AuditEvent/{e.get('id')}", det.get("mac")))
    return out


def matrix_anchors(bodies: list[str], chain: str) -> list[tuple[int, str, str, str | None]]:
    rx = re.compile(r"\[AUDIT-ANKER\] Audit-Kette (?P<chain>[0-9a-f]{16}) bis Eintrag #(?P<id>\d+): (?P<hash>[0-9a-f]{64})"
                    r"(?: \(MAC (?P<mac>[0-9a-f]{32})\))?")
    return [(int(m["id"]), m["hash"], "Matrix", m["mac"]) for b in bodies for m in [rx.search(b or "")]
            if m and m["chain"] == chain]


def tamper_fhir(aes: list[dict]) -> dict:
    aes = [e for e in aes if not is_anchor(e)]
    if len(aes) < 3:
        return {"ok": False, "detail": "too few events"}
    cp = copy.deepcopy(aes)
    i = len(cp) // 2
    cp[i]["outcomeDesc"] = str(cp[i].get("outcomeDesc", "")) + " (edited)"
    edited = verify_fhir_chains(cp)
    deleted = verify_fhir_chains(cp[:i] + cp[i + 1:])
    return {"ok": not edited["ok"] and not deleted["ok"], "edited": f"AuditEvent/{aes[i].get('id')}",
            "edit_detected": edited["edited"][:1], "delete_detected": bool(deleted["dangling_prev"])}


def tamper_rows(rows: list[dict], key: bytes | None = None) -> dict:
    if len(rows) < 3:
        return {"ok": False, "detail": "chain too short"}
    cp = copy.deepcopy(sorted(rows, key=lambda r: int(r["id"])))
    i = len(cp) // 2
    cp[i]["what"] = str(cp[i].get("what")) + " (edited)"
    edited = verify_rows(cp, key)
    deleted = verify_rows(cp[:i] + cp[i + 1:], key)
    cp2 = copy.deepcopy(sorted(rows, key=lambda r: int(r["id"])))
    return {"ok": not edited["ok"] and not deleted["ok"], "edited_id": cp2[i]["id"],
            "edit_detected": edited["problems"][:2], "delete_detected": deleted["problems"][:2]}

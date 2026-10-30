"""Verify the audit trail: the keyed SQLite chain (every entry, append-only) and the local anchor table; with --fhir
also every six-step AuditEvent on HAPI (content hash as written + fhirmap.verify_audit_chain prev_hash links) and
the chain-head anchors READ BACK from HAPI (AuditEvent search) and from the Matrix room history.

    uv run python -m service.audit_verify [--fhir]
Exit code 0 = intact, 1 = modified / gap / missing FHIR copy / an anchor that disagrees or cannot be read.
"""
from __future__ import annotations

import argparse
import sys

from .config import Settings
from .core import Service


def anchor_line(where: str, bucket: dict) -> str:
    """One line per place, e.g. 'anchors hapi  : 12 found, 12 agree, 0 disagree, 1 not (yet) there'."""
    label = f"anchors {where:6s}:"
    if bucket.get("configured") is False:
        return f"{label} not checked (no Matrix URL/token configured: PGK_MATRIX_URL, PGK_MATRIX_TOKEN)"
    if bucket.get("error"):
        return f"{label} COULD NOT BE READ: {bucket['error']}"
    line = f"{label} {bucket['found']} found, {bucket['ok']} agree, {len(bucket['bad'])} disagree"
    if bucket["bad"]:
        line += " (entries " + ", ".join(f"#{i}" for i in bucket["bad"][:10]) + ")"
    if where != "local":
        line += f", {len(bucket['missing'])} local anchors not (yet) there"
        line += f", {bucket['ignored_unauthenticated']} ignored (unauthenticated)"
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fhir", action="store_true", help="also verify the AuditEvent copies on the FHIR server")
    a = ap.parse_args(argv)
    res = Service(Settings()).verify_audit(check_fhir=a.fhir)
    print(f"audit entries: {res['count']}, chain head: {res['head']}")
    if a.fhir:
        print(f"FHIR AuditEvents verified: {res.get('fhir_checked', 0)}")
    check = res.get("anchor_check") or {}
    print(f"anchor chain id: {check.get('chain')}, Matrix room: {check.get('room')}")
    for where in ("local", "hapi", "matrix"):
        if where in check:
            print(anchor_line(where, check[where]))
    if res["problems"]:
        print("AUDIT VERIFY FAILED:")
        for p in res["problems"]:
            print("  -", p)
        return 1
    print("AUDIT VERIFY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Re-verify the signed sender statement of EVERY filing against what HAPI holds now: the Provenance and the filed
clinical resources are read back AT THE VERSIONS THAT WERE SIGNED, and the statement must still cover this answer's
SHA-256, exactly these targets and the clinical content (resources_sha256). Uses the id_map and versions stored at
filing. A newer version signed by a later filing (a correction) is reported as information; a newer version nobody
signed is a failure.

    uv run python -m service.verify_provenance [--offline]
With the service running, the check runs INSIDE it (POST /api/maintenance/reverify), so a failing filing is reported
to the request's room ("[ACHTUNG] ... verändert", once per filing); otherwise it runs offline without alerts.
Exit code 0 = every re-verifiable filing verifies, 1 = at least one does not (the changed resources are listed).
"""
from __future__ import annotations

import argparse
import sys

import httpx

from .config import Settings
from .core import Service


def via_running_service(settings: Settings, http: httpx.Client | None = None) -> dict | None:
    """POST /api/maintenance/reverify on the running service (its alerts reach Dr. Hofer's room); None if it is not
    reachable."""
    http = http or httpx.Client(timeout=120)
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    try:
        resp = http.post(f"{settings.base_url}/api/maintenance/reverify", headers=headers)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        print(f"running service answered HTTP {resp.status_code}; checking offline instead")
        return None
    return resp.json()


def main(argv: list[str] | None = None, http: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="do not ask the running service (no room alerts)")
    a = ap.parse_args(argv)
    settings = Settings()
    res = None if a.offline else via_running_service(settings, http)
    if res is not None:
        print(f"checked by the running service at {settings.base_url} (alerts go to the request's room)")
    else:
        print("service not reachable: offline check (no room alerts)")
        res = Service(settings).reverify_all(alert=False)
    print(f"filings checked: {res['checked']}, verified: {res['verified']}, "
          f"not re-verifiable (filed before id_map was stored): {res['not_reverifiable']}")
    for info in res["superseded"]:  # information, not tampering: a later filing signed the newer version
        print(f"  info: {info}")
    if not res["failed"]:
        print("PROVENANCE VERIFY OK")
        return 0
    print("PROVENANCE VERIFY FAILED:")
    for f in res["failed"]:
        changed = ", ".join(f.get("changed") or []) or "-"
        print(f"  - {f['ref_code']} ({f['request_id']}): changed {changed}; {'; '.join(f.get('problems') or [])[:300]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""HL7 validator_cli, batched: ONE JVM for all resources (the brief's command, `-version 4.0`).

Terminology on (tx.fhir.org) by default, as the brief's command does; falls back to `-tx n/a` only if
the server is unreachable, and says so in the result (with tx off nobody checks LOINC codes).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .stack import REPO

JAVA = os.environ.get("JAVA", "/opt/homebrew/opt/openjdk@21/bin/java")
JARS = [Path(os.environ["VALIDATOR_JAR"])] if os.environ.get("VALIDATOR_JAR") else [
    REPO / "infra" / "tools" / "validator_cli.jar"]


def _tx_reachable() -> bool:
    try:
        urllib.request.urlopen("https://tx.fhir.org/r4/metadata?_summary=true", timeout=8).read(100)
        return True
    except OSError:
        return False


def validate(resources: list[dict], timeout: int = 900) -> dict:
    """Returns {"errors": n, "warnings": n, "per_resource": {ref: [msgs]}, "tx": bool, "seconds": s, "files": n}."""
    jar = next((j for j in JARS if j.exists()), None)
    java = JAVA if Path(JAVA).exists() else shutil.which("java")
    if not resources:
        return {"errors": 0, "warnings": 0, "per_resource": {}, "tx": None, "seconds": 0, "files": 0}
    if not jar or not java:
        return {"errors": None, "detail": "validator_cli.jar or java not found", "files": len(resources)}
    tx = _tx_reachable()
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pgk-val-") as d:
        names = {}
        for i, r in enumerate(resources):
            p = Path(d) / f"{i:05d}-{r['resourceType']}.json"
            p.write_text(json.dumps(r, ensure_ascii=False))
            names[str(p)] = f"{r['resourceType']}/{r.get('id', i)}"
        out = Path(d) / "out.json"
        cmd = [java, "-Xmx4g", "-jar", str(jar), *names, "-version", "4.0", "-output", str(out)]
        if not tx:
            cmd += ["-tx", "n/a"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if not out.exists():
            return {"errors": None, "detail": (p.stdout + p.stderr)[-2000:], "files": len(resources), "tx": tx}
        doc = json.loads(out.read_text())
    oos = [e["resource"] for e in doc.get("entry", [])] if doc.get("resourceType") == "Bundle" else [doc]
    per, errs, warns = {}, 0, 0
    for oo in oos:
        f = next((x.get("valueString") for x in oo.get("extension", [])
                  if x.get("url", "").endswith("operationoutcome-file")), None)
        ref = names.get(f, names.get(str(Path(f).resolve()) if f else "", f or "?"))
        for iss in oo.get("issue", []):
            sev = iss.get("severity")
            msg = (iss.get("details") or {}).get("text") or iss.get("diagnostics") or ""
            loc = ", ".join(iss.get("expression") or iss.get("location") or [])
            if sev in ("error", "fatal"):
                errs += 1
                per.setdefault(ref, []).append(f"{sev}: {loc}: {msg}")
            elif sev == "warning":
                warns += 1
    return {"errors": errs, "warnings": warns, "per_resource": per, "tx": tx, "files": len(resources),
            "seconds": round(time.monotonic() - t0, 1)}

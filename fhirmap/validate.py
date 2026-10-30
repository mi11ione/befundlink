"""HL7 FHIR validator (validator_cli.jar) wrapper, R4, ONE JVM per call.

Modes
- ``tx``      terminology checks against tx.fhir.org (LOINC code + display, UCUM, THO),
              responses cached in ``data/txcache`` (committed). Needs network for any
              code/display not yet in the cache. This is the mode for the metric.
- ``offline`` ``-tx n/a``: structure, cardinality, invariants, UCUM syntax and the
              HL7 terminology shipped in the packages; LOINC codes and displays are NOT checked.
- ``auto``    ``tx`` if tx.fhir.org answers within 2 s, else ``offline`` (reported).

Our own CodeSystems (``codesystems/``) are loaded with ``-ig`` so codes from them validate.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TXCACHE = HERE / "data" / "txcache"
CODESYSTEMS = HERE / "codesystems"
_JARS = [os.environ.get("PGK_VALIDATOR_JAR"), str(REPO / "infra" / "tools" / "validator_cli.jar")]
_JAVAS = [os.environ.get("PGK_JAVA"), "/opt/homebrew/opt/openjdk@21/bin/java", shutil.which("java")]
TX_SERVER = os.environ.get("PGK_TX_SERVER", "https://tx.fhir.org")


def _first(paths) -> str:
    for p in paths:
        if p and Path(p).exists():
            return p
    raise FileNotFoundError(f"none of {[p for p in paths if p]} exists")


def tx_reachable(timeout: float = 2.0) -> bool:
    host = TX_SERVER.split("://", 1)[-1].split("/", 1)[0]
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


def _write_codesystems() -> None:
    from .build import codesystems
    CODESYSTEMS.mkdir(exist_ok=True)
    for cs in codesystems():
        p = CODESYSTEMS / f"CodeSystem-{cs['id']}.json"
        txt = json.dumps(cs, ensure_ascii=False, indent=1)
        if not p.exists() or p.read_text(encoding="utf-8") != txt:
            p.write_text(txt, encoding="utf-8")


def command(paths: list[str], mode: str, output: str) -> list[str]:
    cmd = [_first(_JAVAS), "-Xss4m", "-jar", _first(_JARS), "-version", "4.0.1", "-level", "warnings",
           "-ig", str(CODESYSTEMS), "-output", output]
    if mode == "offline":
        cmd += ["-tx", "n/a"]
    else:
        TXCACHE.mkdir(parents=True, exist_ok=True)
        cmd += ["-tx", TX_SERVER, "-txCache", str(TXCACHE)]
    return cmd + paths


def _issues(oo: dict) -> list[dict]:
    out = []
    for i in oo.get("issue", []):
        loc = (i.get("expression") or i.get("location") or [""])[0]
        line = col = None
        for ext in i.get("extension", []):
            if ext["url"].endswith("issue-line"):
                line = ext.get("valueInteger")
            elif ext["url"].endswith("issue-col"):
                col = ext.get("valueInteger")
        out.append({"severity": i.get("severity"), "location": loc, "line": line, "col": col,
                    "message": (i.get("details") or {}).get("text") or i.get("diagnostics") or ""})
    return out


def _run(tmp: str, n: int, mode: str, timeout: float) -> tuple[dict, list[str], float]:
    out = str(Path(tmp) / f"out-{mode}.json")
    cmd = command([str(Path(tmp) / "in")], mode, out)    # one JVM, the whole directory
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    secs = round(time.monotonic() - t0, 1)
    if not Path(out).exists():
        raise RuntimeError(f"validator failed (exit {proc.returncode}): {proc.stdout[-1500:]} {proc.stderr[-1500:]}")
    res = json.loads(Path(out).read_text(encoding="utf-8"))
    return res, cmd, secs


def validate_report(resources: list[dict] | dict, mode: str = "auto", names: list[str] | None = None,
                    tx_timeout: float = 300) -> dict:
    """Validate resources (each one a file) in one JVM call.

    mode 'auto': 'tx' when tx.fhir.org answers a TCP connect within 2 s, else 'offline'; a tx run
    that fails or times out is retried once offline. The mode actually used is returned, with
    ``fallback`` saying why when it is not the requested one.
    Returns {mode, requested_mode, fallback, terminology_checked, command, seconds, files, errors, warnings}.
    """
    if isinstance(resources, dict):
        resources = [resources]
    requested = mode
    fallback = None
    if mode == "auto":
        if tx_reachable(2.0):
            mode = "tx"
        else:
            mode, fallback = "offline", "tx.fhir.org not reachable within 2 s"
    if mode not in ("tx", "offline"):
        raise ValueError("mode must be tx, offline or auto")
    _write_codesystems()
    with tempfile.TemporaryDirectory(prefix="fhirmap-val-") as tmp:
        (Path(tmp) / "in").mkdir()
        for i, r in enumerate(resources):
            nm = (names[i] if names else f"{i:03d}-{r.get('resourceType', 'Resource')}") + ".json"
            (Path(tmp) / "in" / nm).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            res, cmd, secs = _run(tmp, len(resources), mode, tx_timeout if mode == "tx" else 900)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            if requested != "auto" or mode != "tx":
                raise
            fallback = f"tx run failed ({type(e).__name__}); re-run offline"
            mode = "offline"
            res, cmd, secs = _run(tmp, len(resources), mode, 900)
    oos = [e["resource"] for e in res.get("entry", [])] if res.get("resourceType") == "Bundle" else [res]
    files: dict[str, list[dict]] = {}
    for oo in oos:
        fname = None
        for ext in oo.get("extension", []):
            if ext["url"].endswith("operationoutcome-file"):
                fname = Path(ext.get("valueString", "")).name
        files[fname or f"#{len(files)}"] = _issues(oo)
    errors, warnings = [], []
    for f, iss in files.items():
        for i in iss:
            s = f"{f} {i['location']}: {i['message']}"
            if i["severity"] in ("error", "fatal"):
                errors.append(s)
            elif i["severity"] == "warning":
                warnings.append(s)
    return {"mode": mode, "requested_mode": requested, "fallback": fallback, "terminology_checked": mode == "tx",
            "command": " ".join(cmd[:-1]) + f" <dir with {len(resources)} files>", "seconds": secs,
            "files": files, "errors": errors, "warnings": warnings}


def validate(resources: list[dict] | dict, mode: str = "auto") -> list[str]:
    """Error and fatal messages only (warnings and information are ignored). Use
    ``validate_report`` to learn which mode was used."""
    return validate_report(resources, mode)["errors"]


def prewarm_tx_cache() -> dict:
    """Fill ``data/txcache`` by validating, with tx on, one synthetic Observation per LOINC code in
    ``data/loinc.json`` (official display, its example UCUM unit) plus every UCUM code our unit
    map can emit. Afterwards validations of those codes are answered from the cache.
    Returns {codes, ucum, errors, warnings, seconds, mode}."""
    from .loinc import _load
    from .units import _TABLE
    cache = _load()
    codes = sorted(c for c, v in cache.items() if v and not v.get("not_found") and v.get("display"))
    ucums = sorted(set(_TABLE.values()) | {u for v in cache.values() if v for u in v.get("example_ucum_list") or []})
    res = []
    for i, c in enumerate(codes):                 # the shape the service uses: code + display + example unit
        o = {"resourceType": "Observation", "id": f"warm-{i}", "status": "final",
             "code": {"coding": [{"system": "http://loinc.org", "code": c, "display": cache[c]["display"]}]},
             "subject": {"reference": "Patient/warm"}, "effectiveDateTime": "2026-10-22T08:10:00+02:00"}
        if cache[c].get("example_ucum"):
            u = cache[c]["example_ucum"]
            o["valueQuantity"] = {"value": 1, "unit": u, "system": "http://unitsofmeasure.org", "code": u}
        res.append(o)
    for i, u in enumerate(ucums):
        res.append({"resourceType": "Observation", "id": f"warm-u{i}", "status": "final", "code": {"text": "ucum"},
                    "subject": {"reference": "Patient/warm"},
                    "valueQuantity": {"value": 1, "unit": u, "system": "http://unitsofmeasure.org", "code": u}})
    rep = validate_report(res, "tx")
    discouraged = sorted(c for c in codes if (cache[c].get("status") or "ACTIVE") not in ("ACTIVE", "TRIAL"))
    return {"codes": len(codes), "ucum": len(ucums), "errors": rep["errors"], "warnings": len(rep["warnings"]),
            "discouraged": discouraged, "seconds": rep["seconds"], "mode": rep["mode"]}

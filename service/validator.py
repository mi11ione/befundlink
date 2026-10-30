"""Long-lived HL7 validator (validator_cli.jar `server <port>`): one warm JVM instead of a cold JVM per filing.

Measured on the scenario lab filing (10 resources, tx on): CLI 6.3 s per call vs server 0.1-0.4 s, with
identical results (0 errors / 3 warnings; terminology checked: a wrong LOINC display is an error).
Started with exactly the options fhirmap.validate uses (R4, -level warnings, our CodeSystems as -ig,
tx.fhir.org + the committed txcache). Any problem (not up yet, tx unreachable, HTTP error, timeout) ->
None, and the caller falls back to fhirmap.validate_report(mode='auto') - never a hold for infrastructure.
"""
from __future__ import annotations

import importlib
import json
import logging
import subprocess
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("pgk.validator")
fv = importlib.import_module("fhirmap.validate")


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ValidatorServer:
    def __init__(self, port: int, log_path: Path | None = None):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None
        self.ready = threading.Event()
        self.log_path = log_path
        self.http = httpx.Client(timeout=60)
        self._lock = threading.Lock()
        self._tx = (0.0, False)  # (checked_at, reachable): one TCP probe per minute, not one per filing

    def tx_ok(self, ttl: float = 60.0) -> bool:
        at, ok = self._tx
        if time.monotonic() - at > ttl:
            ok = fv.tx_reachable()
            self._tx = (time.monotonic(), ok)
        return ok

    # ------------------------------------------------------------ lifecycle
    def _up(self) -> bool:
        try:
            return self.http.get(f"{self.base}/openapi.json", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def start(self) -> None:
        """Non-blocking. Only a process WE started is ever used - if the port is taken by anything else, a free
        port is picked instead (never reuse an unknown listener). Output goes nowhere (resources contain PII)."""
        def run():
            if self._up():
                self.port = _free_port()
                self.base = f"http://127.0.0.1:{self.port}"
                log.warning("port taken by another process; starting our own validator on :%s", self.port)
            fv._write_codesystems()
            fv.TXCACHE.mkdir(parents=True, exist_ok=True)
            cmd = [fv._first(fv._JAVAS), "-Xss4m", "-jar", fv._first(fv._JARS), "server", str(self.port),
                   "-version", "4.0.1", "-level", "warnings", "-ig", str(fv.CODESYSTEMS),
                   "-tx", fv.TX_SERVER, "-txCache", str(fv.TXCACHE)]
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 120 and self.proc.poll() is None:
                if self._up():
                    log.info("validator server up on :%s in %.1fs (pid %s)", self.port, time.monotonic() - t0, self.proc.pid)
                    self.ready.set()
                    return
                time.sleep(0.5)
            log.warning("validator server did not come up; filings use the CLI validator")
        threading.Thread(target=run, name="validator-server", daemon=True).start()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # ------------------------------------------------------------ validation
    def validate(self, resources: list[dict], names: list[str] | None = None) -> tuple[list[str], dict] | None:
        """-> (errors, report) like fhirmap.validate_report, or None if the server cannot be used now."""
        if not self.ready.is_set() or self.proc is None or self.proc.poll() is not None or not self.tx_ok():
            return None
        t0 = time.monotonic()
        errors, warnings = [], []
        try:
            with self._lock:  # one validation at a time per JVM session (results are deterministic per resource)
                for i, r in enumerate(resources):
                    name = (names[i] if names else f"{i:02d}-{r.get('resourceType', 'Resource')}") + ".json"
                    resp = self.http.post(f"{self.base}/validateResource", content=json.dumps(r, ensure_ascii=False).encode(),
                                          headers={"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"})
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    for iss in resp.json().get("issue", []):
                        loc = (iss.get("expression") or iss.get("location") or ["?"])[0]
                        msg = (iss.get("details") or {}).get("text") or iss.get("diagnostics") or ""
                        s = f"{name} {loc}: {msg}"
                        if iss.get("severity") in ("error", "fatal"):
                            errors.append(s)
                        elif iss.get("severity") == "warning":
                            warnings.append(s)
        except Exception as e:
            log.warning("validator server failed (%s); falling back to the CLI", e)
            return None
        return errors, {"mode": "tx", "requested_mode": "auto", "fallback": None, "terminology_checked": True,
                        "engine": "validator_cli server (warm JVM)", "seconds": round(time.monotonic() - t0, 2),
                        "files": len(resources), "errors": len(errors), "warnings": len(warnings)}

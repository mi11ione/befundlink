"""HL7 Austria AT Core 2.0.0 check of what Befundlink files.

Builds the resources for the two scenario documents with fhirmap (lab result -> DiagnosticReport,
Observations, Practitioners, PractitionerRole, Organization, Provenance, Task; rehab report ->
DocumentReference, ...). It adds the Patient from infra/patient.json and the requester's Practitioner
and Organization from infra/directory.json. It then runs the HL7 validator offline (-tx n/a), with
-ig hl7.at.fhir.core.r4#2.0.0 and the at-core profile that matches each type. AT Core has profiles
only for Patient, Practitioner, PractitionerRole and Organization. The other types are validated
with the IG loaded, which in practice means base R4.

    uv run python tests/atcore.py

Prints the per-type counts and writes tests/atcore.json ({type: {files, profile, errors, warnings}}).
The exit code is 1 if there is any error. Needs Java 21 (JAVA, else /opt/homebrew/opt/openjdk@21/bin/java,
else java on PATH) and infra/tools/validator_cli.jar (or VALIDATOR_JAR). On the first run on a machine,
the validator downloads hl7.at.fhir.core.r4#2.0.0 and its dependencies from packages.fhir.org into
~/.fhir/packages. Later runs are offline. Terminology is not checked (-tx n/a): AT Core's bindings
to termgit ValueSets are extensible, so a terminology server could add warnings but not errors.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PGK_LOINC_ONLINE", "0")

from fhirmap import build_resources, build_task, parse_answer  # noqa: E402
from fhirmap.validate import _write_codesystems  # noqa: E402  (our own CodeSystems, loaded with -ig)

PACKAGE = "hl7.at.fhir.core.r4#2.0.0"
AT = "http://hl7.at/fhir/HL7ATCoreProfiles/4.0.1/StructureDefinition/"
PROFILES = {"Patient": AT + "at-core-patient", "Practitioner": AT + "at-core-practitioner",
            "PractitionerRole": AT + "at-core-practitionerRole", "Organization": AT + "at-core-organization"}
SCEN = Path(os.environ.get("PGK_SCENARIO", ROOT.parent / "track2" / "scenario"))
DOCS = SCEN / "documents" if (SCEN / "documents").is_dir() else TESTS / "jury" / "corpus" / "_scenario"
RECEIVED = "2026-10-29T10:05:00+01:00"
SENDERS = {   # the verified outside person per document (the same inputs as fhirmap/tests/test_fhirmap.py)
    "lab-result-maria-beispiel.md": (
        "lab_result",
        {"request_id": "req-lab-1", "requester_mxid": "@lena.hofer:klinikum.example", "doc_kind": "lab_result",
         "recipient": {"name": "Dr. Eva Brandl", "role": "Laborärztin", "org": "Labor Kanalstraße"},
         "created_at": "2026-10-29T09:00:00+01:00", "expires_at": "2026-11-05T09:00:00+01:00"},
        {"given_name": "Eva", "family_name": "Brandl", "birthDate": "1975-05-05", "bpk": "SYNTH-BPK-GH-EVA",
         "loa": "high", "assertion": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJldmEifQ.c3ludGg",
         "verified_at": "2026-10-29T10:02:00+01:00"}),
    "rehab-report-maria-beispiel.md": (
        "discharge_report",
        {"request_id": "req-reha-1", "requester_mxid": "@lena.hofer:klinikum.example",
         "doc_kind": "discharge_report",
         "recipient": {"name": "Stefan Kern", "role": "Entlassungsmanagement", "org": "Reha Waldblick"},
         "created_at": "2026-10-29T09:00:00+01:00"},
        {"given_name": "Stefan", "family_name": "Kern", "birthDate": "1980-02-02", "bpk": "SYNTH-BPK-GH-KERN",
         "loa": "high", "assertion": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJrZXJuIn0.c3ludGg"}),
}


def first(*paths) -> str:
    for p in paths:
        if p and (Path(p).exists() or shutil.which(p)):
            return str(p)
    sys.exit(f"not found: {[p for p in paths if p]}")


def resources() -> list[dict]:
    out = [json.loads((ROOT / "infra" / "patient.json").read_text(encoding="utf-8"))]
    out += json.loads((ROOT / "infra" / "directory.json").read_text(encoding="utf-8"))["resources"]
    for name, (kind, request, identity) in SENDERS.items():
        parsed = parse_answer((DOCS / name).read_text(encoding="utf-8"), kind)
        out += build_resources(parsed, "Patient/maria-beispiel", request, identity, RECEIVED)
        out.append(build_task(request, "Patient/maria-beispiel", request["recipient"]))
    return out


def validate(paths: list[Path], profile: str | None, out: Path) -> list[dict]:
    """One validator JVM over the paths; returns one OperationOutcome per file."""
    java = first(os.environ.get("JAVA"), "/opt/homebrew/opt/openjdk@21/bin/java", "java")
    jar = first(os.environ.get("VALIDATOR_JAR"), ROOT / "infra" / "tools" / "validator_cli.jar")
    cmd = [java, "-Xss4m", "-jar", jar, "-version", "4.0.1", "-level", "warnings", "-tx", "n/a",
           "-ig", str(ROOT / "fhirmap" / "codesystems"), "-ig", PACKAGE, "-output", str(out)]
    cmd += ["-profile", profile] if profile else []
    proc = subprocess.run(cmd + [str(p) for p in paths], capture_output=True, text=True)
    if not out.exists():
        sys.exit(f"validator failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    doc = json.loads(out.read_text(encoding="utf-8"))
    return [e["resource"] for e in doc["entry"]] if doc["resourceType"] == "Bundle" else [doc]


def main() -> int:
    _write_codesystems()
    groups: dict[str, list[Path]] = defaultdict(list)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for i, r in enumerate(resources()):
            p = tmp / f"{i:02d}-{r['resourceType']}.json"
            p.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
            groups[r["resourceType"] if r["resourceType"] in PROFILES else "other"].append(p)
        with ThreadPoolExecutor(len(groups)) as pool:
            runs = {g: pool.submit(validate, ps, PROFILES.get(g), tmp / f"out-{g}.json") for g, ps in groups.items()}
            outcomes = {g: f.result() for g, f in runs.items()}
        rtype = {str(p): p.name.split("-", 1)[1][:-5] for ps in groups.values() for p in ps}

    result: dict[str, dict] = {}
    for g, oos in outcomes.items():
        for oo in oos:
            src = next((x["valueString"] for x in oo.get("extension", []) if x["url"].endswith("operationoutcome-file")), "")
            t = rtype.get(src, g)
            row = result.setdefault(t, {"files": 0, "profile": PROFILES.get(t, "base R4 (no AT Core profile)"),
                                        "errors": 0, "warnings": 0, "error_messages": []})
            row["files"] += 1
            for i in oo.get("issue", []):
                if i["severity"] in ("error", "fatal"):
                    row["errors"] += 1
                    row["error_messages"].append(f"{(i.get('expression') or [''])[0]}: {i['details'].get('text', '')}")
                elif i["severity"] == "warning":
                    row["warnings"] += 1
    result = dict(sorted(result.items()))
    report = {"package": PACKAGE, "validator": "validator_cli.jar, -version 4.0.1 -tx n/a", "types": result}
    (TESTS / "atcore.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for t, row in result.items():
        print(f"{t:18} files {row['files']:2}  errors {row['errors']}  warnings {row['warnings']}  {row['profile']}")
        for m in row["error_messages"]:
            print("   ERROR", m)
    return 1 if any(r["errors"] for r in result.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

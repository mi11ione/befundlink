"""Cross-check: corpus labels and refcheck vs the product's fhirmap.decide (same inputs, black box).

    uv run python tests/jury/xcheck.py [--fuzz 2000 --seed 11] [-v]

Reports every disagreement (outcome class, and primary reason code). Used by tests/unit/test_jury.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jury import refcheck  # noqa: E402
from jury.cases import build  # noqa: E402
from jury.generate import doc_bytes, fuzz  # noqa: E402
from jury.model import CTX, EID, PATIENT  # noqa: E402


def product(case) -> tuple[str, list[str]]:
    import fhirmap
    ctx, e, raw = CTX[case.ctx], EID[case.eid], doc_bytes(case)
    parsed = fhirmap.parse_upload(raw, ctx.doc_kind)
    d = fhirmap.decide(parsed, request={**ctx.body(), "requester_name": "Dr. Lena Hofer"},
                       patient={"name": PATIENT["name"], "birthDate": PATIENT["birthDate"], "gender": "female"},
                       identity={"given_name": e.given, "family_name": e.family, "birthdate": e.birthDate}, raw=raw)
    return d["outcome"], list(d.get("codes") or [])


def build_check(case, manifest_case: dict) -> list[tuple[str, str]]:
    """Offline: the product's own FHIR build (fhirmap.build_resources, no server) vs the case's value-level
    expectations (rows, conclusion, status, Vienna times). Returns failures; [] = matches."""
    import fhirmap
    from pgktest import fidelity as F
    ctx, e, raw = CTX[case.ctx], EID[case.eid], doc_bytes(case)
    res = fhirmap.build_resources(fhirmap.parse_upload(raw, ctx.doc_kind), patient_ref="Patient/maria-beispiel",
                                  request={**ctx.body(), "request_id": "req-xcheck"},
                                  identity={"given_name": e.given, "family_name": e.family, "birthdate": e.birthDate,
                                            "simulated": True}, received_at="2026-10-29T23:00:00+01:00")
    for i, r in enumerate(res):
        r.setdefault("id", f"x{i}")
    byref = {f"{r['resourceType']}/{r['id']}": r for r in res}
    dr = next(r for r in res if r["resourceType"] == "DiagnosticReport")
    for ref, o in zip(dr.get("result") or [], [r for r in res if r["resourceType"] == "Observation"]):
        byref.setdefault(ref["reference"], o)
    m, sq = manifest_case, lambda t: " ".join((t or "").split())
    fails = [(n, d[:90]) for n, ok, d in F._observations(m, dr, byref) if not ok]
    if sq(dr.get("conclusion")) != sq(m.get("conclusion")):
        fails.append(("conclusion", repr(dr.get("conclusion"))[:90]))
    if dr.get("status") != (m.get("status") or "final"):
        fails.append(("status", str(dr.get("status"))))
    for k, f in (("effective", "effectiveDateTime"), ("issued", "issued")):
        want = (m.get("times") or {}).get(k)
        if want and not F._same_instant(dr.get(f), want)[0]:
            fails.append((k, f"{dr.get(f)} vs {want}"))
    return fails


def compare(cases) -> list[dict]:
    out = []
    for c in cases:
        ref, rr = refcheck.check(doc_bytes(c), CTX[c.ctx], EID[c.eid])
        got, gr = product(c)
        if not (c.expect == ref == got):
            out.append({"id": c.id, "title": c.title, "label": c.expect, "refcheck": ref, "ref_codes": rr,
                        "fhirmap": got, "fhirmap_codes": gr})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzz", type=int, default=0)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    cases = fuzz(a.fuzz, a.seed) if a.fuzz else build()
    bad = compare(cases)
    for b in bad:
        print(f"{b['id']:6} label={b['label']:5} ref={b['refcheck']:5} fhirmap={b['fhirmap']:5} "
              f"| {b['title'][:60]} | ref {b['ref_codes'][:3]} | fm {b['fhirmap_codes'][:3]}")
    print(f"{len(cases) - len(bad)}/{len(cases)} agree")


if __name__ == "__main__":
    main()

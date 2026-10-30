"""prove() as the gate behind an extractor: every plausible extraction error on the built FHIR is rejected.

    uv run pytest tests/unit/test_prove_gate.py -q
    uv run python tests/prove_gate.py          # the same set, as one JSON line of numbers
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

import prove_gate as G  # noqa: E402
from jury.model import FILE  # noqa: E402

DOCS = G.documents()
IDS = [d[0] for d in DOCS]

LAB_KINDS = {"value_last_digit", "value_decimal_shift_x10", "unit_changed", "loinc_swapped_to_other_row",
             "comparator_dropped", "ref_high_changed", "ref_low_changed", "values_swapped_with_next_row",
             "row_dropped", "invented_extra_observation", "sample_time_plus_one_day"}
ALL_KINDS = LAB_KINDS | {"subject_other_patient", "attached_text_one_word", "author_changed",
                         "author_is_the_sender", "stay_start_plus_one_day"}


def _why(pr) -> str:
    return json.dumps([f for f in pr["fields"] if not f["equal"]] + [r for r in pr["rows"] if r["status"] != "equal"],
                      ensure_ascii=False)


def test_document_set():
    m = json.loads((G.CORPUS / "manifest.json").read_text(encoding="utf-8"))
    n_file = sum(c["expect"] == FILE for c in m["cases"])
    assert n_file and len(DOCS) == n_file + len(list(G.SCEN_DOCS.glob("*.md")))
    assert {k for _, k, _ in DOCS} == {"lab_result", "discharge_report"}


@pytest.mark.parametrize("label,kind,raw", DOCS, ids=IDS)
def test_honest_build_is_all_equal(label, kind, raw):
    p, res, patient = G.honest(kind, raw)
    pr = G.prove(p, res, patient=patient)
    assert pr["all_equal"], _why(pr)


@pytest.mark.parametrize("label,kind,raw", DOCS, ids=IDS)
def test_every_corruption_is_rejected(label, kind, raw):
    p, res, patient = G.honest(kind, raw)
    seen, missed = [], []
    for ck, target, bad in G.corruptions(p, res, patient):
        assert bad != res, (ck, target)                 # the corruption really changed the filed FHIR
        seen.append(ck)
        if G.prove(p, bad, patient=patient)["all_equal"]:
            missed.append((ck, target))
    assert seen
    assert not missed, missed


def test_corruption_set_covers_every_kind():
    seen = {ck for _, k, raw in DOCS for ck, _, _ in G.corruptions(*G.honest(k, raw))}
    assert ALL_KINDS <= seen, ALL_KINDS - seen


def test_rejected_for_the_right_reason():
    """Spot check on the scenario lab result: each corruption fails the field it touched, not a side check."""
    p, res, patient = G.honest("lab_result", (G.SCEN_DOCS / "lab-result-maria-beispiel.md").read_bytes())
    want = {"value_last_digit": "value", "value_decimal_shift_x10": "value", "unit_changed": "unit",
            "loinc_swapped_to_other_row": "loinc", "ref_high_changed": "ref_high", "ref_low_changed": "ref_low",
            "values_swapped_with_next_row": "value", "sample_time_plus_one_day": "effective"}
    for ck, target, bad in G.corruptions(p, res, patient):
        pr = G.prove(p, bad, patient=patient)
        row_fields = {d["field"] for r in pr["rows"] for d in r["diffs"]}
        bad_fields = {f["field"] for f in pr["fields"] if not f["equal"]}
        if ck in want:
            assert want[ck] in row_fields, (ck, target, _why(pr))
        elif ck == "row_dropped":
            assert any(r["status"] == "missing_in_fhir" for r in pr["rows"]), (ck, target)
        elif ck == "invented_extra_observation":
            assert any(r["status"] == "extra_in_fhir" for r in pr["rows"]), ck
        elif ck == "subject_other_patient":
            assert "subject points to the Patient" in bad_fields, _why(pr)
        elif ck == "attached_text_one_word":
            assert "original text (attachment) byte-identical" in bad_fields, _why(pr)
            assert "attachment SHA-1 and size" not in bad_fields      # re-hashed: only the content diff catches it
        elif ck == "author_changed":
            assert "author.name" in bad_fields, _why(pr)


def test_runner_reports_all_detected():
    r = G.run()
    assert r["documents"] == len(DOCS) and r["corruptions"] > 1000
    assert r["detected"] == r["corruptions"] and r["undetected"] == [], r["undetected"][:10]
    assert "honest_build_not_equal" not in r

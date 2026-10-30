"""Self-tests of the test suite: labels, rules, normalisation, PII scanner, audit chain verifier.

    uv run pytest tests/unit -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

from jury import refcheck  # noqa: E402
from jury.cases import build  # noqa: E402
from jury.generate import CORPUS, doc_bytes, fuzz  # noqa: E402
from jury.model import CTX, EID, FILE  # noqa: E402
from pgktest import norm  # noqa: E402

CASES = build()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_label_matches_reference_rules(case):
    outcome, reasons = refcheck.check(doc_bytes(case), CTX[case.ctx], EID[case.eid])
    assert outcome == case.expect, (outcome, reasons)
    assert set(case.reasons) & set(reasons), (case.reasons, reasons)  # the case lists acceptable codes, first preferred


def test_fuzz_oracle_agrees_with_reference_rules():
    bad = []
    for c in fuzz(600, seed=11):
        outcome, reasons = refcheck.check(doc_bytes(c), CTX[c.ctx], EID[c.eid])
        if outcome != c.expect:
            bad.append((c.id, c.title, c.expect, outcome, reasons))
    assert not bad, bad[:10]


def test_corpus_on_disk_is_current():
    m = json.loads((CORPUS / "manifest.json").read_text())
    assert [c["id"] for c in m["cases"]] == [c.id for c in CASES], "run tests/jury/generate.py"
    for c in CASES:
        assert (CORPUS / f"{c.id}.md").read_bytes() == doc_bytes(c), f"{c.id} stale: run tests/jury/generate.py"


def test_corpus_size_and_coverage():
    assert len(CASES) >= 60
    groups = {c.group for c in CASES}
    assert {"A", "R", "W", "H", "I", "X"} <= groups
    assert {"I05", "I06", "I07", "I08", "I09", "X12", "X13", "X14", "X15", "A33"} <= {c.id for c in CASES}
    assert len({c.id for c in CASES}) == len(CASES)
    assert all(c.why and c.reasons for c in CASES)
    for c in CASES:
        if c.expect == FILE and CTX[c.ctx].doc_kind == "lab_result":
            assert c.obs and c.times, c.id


@pytest.mark.parametrize("a,b,want", [
    ("Maria Beispiel", "Maria Beispiel", norm.SAME), ("María Beispiel", "Maria Beispiel", norm.SAME),
    ("Beispiel, Maria", "Maria Beispiel", norm.SAME), ("Mag.a Maria Beispiel", "Maria Beispiel", norm.SAME),
    ("Märia Beispiel", "Maria Beispiel", norm.NEAR), ("Beispiel Maria", "Maria Beispiel", norm.NEAR),
    ("Mаria Beispiel", "Maria Beispiel", norm.NEAR), ("Maria Beispiel-Huber", "Maria Beispiel", norm.NEAR),
    ("Max Beispiel", "Maria Beispiel", norm.DIFFERENT), ("Maria Huber", "Maria Beispiel", norm.DIFFERENT),
    ("Jürgen Müller", "Juergen Mueller", norm.SAME), ("Jürgen Muller", "Jürgen Müller", norm.NEAR),
])
def test_names(a, b, want):
    assert norm.compare_names(a, b) == want


@pytest.mark.parametrize("s,want", [
    ("12.03.1961", norm.SAME), ("12. März 1961", norm.SAME), ("March 12, 1961", norm.SAME), ("1961-03-12", norm.SAME),
    ("3 December 1961", norm.NEAR), ("12/03/1961", norm.NEAR), ("13.03.1961", norm.NEAR), ("1916-03-12", norm.NEAR),
    ("31.02.1961", norm.NEAR), ("1 January 1960", norm.DIFFERENT), ("14 July 1958", norm.DIFFERENT),
])
def test_dobs(s, want):
    assert norm.compare_dobs(norm.parse_dob(s), "1961-03-12") == want


def test_loinc_check_digit():
    assert all(norm.loinc_ok(c) for c in ["6301-6", "14979-9", "777-3", "3255-7", "718-7", "4548-4", "62238-1"])
    assert not any(norm.loinc_ok(c) for c in ["6301-5", "777-4", "14979-0", "abc", ""])


def test_identity():
    P = ("Maria Beispiel", "1961-03-12")
    assert norm.identity_matches("Dr. Eva Brandl", "Eva-Maria", "Brandl")
    assert not norm.identity_matches("Dr. Eva Maria Brandl", "Eva", "Brandl")  # requested given must be eID given
    assert norm.identity_matches("Brandl Eva", "Eva", "Brandl")
    assert not norm.identity_matches("Brandl", "Eva", "Brandl")                # a given name must be requested
    assert norm.identity_matches("Maria Beispiel", "Maria", "Beispiel", "1961-03-12", P)
    assert not norm.identity_matches("Maria Beispiel", "Maria", "Beispiel", "1990-06-15", P)
    assert not norm.identity_matches("Maria Beispiel", "Maria", "Beispiel", None, P)
    assert norm.identity_matches("Dr. Eva Brandl", "Eva", "Brandl")
    assert norm.identity_matches("Dr. Eva Brandl", "Eva Maria", "Brandl")
    assert not norm.identity_matches("Dr. Eva Brandl", "Eve", "Brandl")
    assert not norm.identity_matches("Dr. Eva Brandl", "Thomas", "Huber")
    assert not norm.identity_matches("Dr. Eva Brandl", "Eva", "Brandl-Huber")


def test_expected_flags_are_only_what_the_lab_wrote():
    """File exactly what was sent: an expected interpretation must be printed in that row's Flag cell."""
    for c in CASES:
        for o in c.obs or []:
            if o["flag"] is None:
                continue
            row = next(l for l in c.text.splitlines() if l.startswith(f"| {o['test']} |"))
            written = {o["flag"], {"H": "+", "L": "-"}.get(o["flag"], o["flag"])}
            assert any(f"| {w} |" in row for w in written), (c.id, o["test"], o["flag"])


def _fhirmap():
    try:
        import fhirmap  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _fhirmap(), reason="fhirmap (the product) not importable")
def test_labels_agree_with_product_on_corpus():
    """Black-box second opinion: the product's fhirmap.decide gives every corpus label and one of its reasons."""
    from jury.xcheck import compare, product
    assert compare(CASES) == []
    bad = [(c.id, c.reasons, product(c)[1][:3]) for c in CASES
           if c.reasons[0] != "OK" and not set(c.reasons) & set(product(c)[1])]
    assert bad == []


@pytest.mark.skipif(not _fhirmap(), reason="fhirmap (the product) not importable")
def test_fuzz_agrees_with_product():
    from jury.xcheck import compare
    bad = compare(fuzz(2000, seed=11))
    assert bad == [], bad[:5]


@pytest.mark.skipif(not _fhirmap(), reason="fhirmap (the product) not importable")
def test_product_build_matches_value_expectations():
    """Every FILE lab case: the product's own FHIR build has exactly the expected rows, units, comparators,
    flags, pending/absent rows, normalised conclusion, report status and Vienna times (offline, no server)."""
    from jury.xcheck import build_check
    man = {c["id"]: c for c in json.loads((CORPUS / "manifest.json").read_text())["cases"]}
    bad = {c.id: build_check(c, man[c.id]) for c in CASES
           if c.expect == FILE and CTX[c.ctx].doc_kind == "lab_result"}
    assert not {k: v for k, v in bad.items() if v}

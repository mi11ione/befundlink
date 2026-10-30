"""Documents written by people who never saw our format (tests/jury/fresh).

    uv run pytest tests/unit/test_fresh_docs.py -q

Each case goes through the product exactly as the service runs it: ``fhirmap.parse_upload`` with the requested
doc_kind, ``fhirmap.decide`` with the request, the requested Patient (Maria Beispiel, 1961-03-12) and the eID of
the recipient. A FILE must also be fileable (parsed kind known) and pass ``prove()`` on the built resources,
because the service holds anything else.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import fhirmap
from fhirmap.decide import decide

FRESH = Path(__file__).resolve().parents[1] / "jury" / "fresh"
MANIFEST = json.loads((FRESH / "manifest.json").read_text(encoding="utf-8"))
HOLDOUT = json.loads((FRESH / "holdout" / "manifest.json").read_text(encoding="utf-8"))
for _c in HOLDOUT["cases"]:
    _c["file"] = "holdout/" + _c["file"]
HOLDOUT3 = json.loads((FRESH / "holdout3" / "manifest.json").read_text(encoding="utf-8"))
for _c in HOLDOUT3["cases"]:
    _c["file"] = "holdout3/" + _c["file"]
ALL = (MANIFEST["cases"] + [dict(c, id="holdout:" + c["id"]) for c in HOLDOUT["cases"]]
       + [dict(c, id="holdout3:" + c["id"]) for c in HOLDOUT3["cases"]])
PATIENT = {"resourceType": "Patient", "id": "maria-beispiel", "name": [{"family": "Beispiel", "given": ["Maria"]}],
           "gender": "female", "birthDate": "1961-03-12"}
ROUTES = {
    "lab": ({"doc_kind": "lab_result", "requester_name": "Dr. Lena Hofer",
             "recipient": {"name": "Dr. Eva Brandl", "role": "Laborärztin", "org": "Labor Kanalstraße"}},
            {"given_name": "Eva", "family_name": "Brandl", "birthDate": "1975-05-05"}),
    "rehab": ({"doc_kind": "discharge_report", "requester_name": "Dr. Lena Hofer",
               "recipient": {"name": "Stefan Kern", "role": "Entlassungsmanagement", "org": "Reha Waldblick"}},
              {"given_name": "Stefan", "family_name": "Kern", "birthDate": "1980-02-02"}),
    "patient": ({"doc_kind": "lab_result", "requester_name": "Dr. Lena Hofer",
                 "recipient": {"name": "Maria Beispiel", "role": "Patientin", "org": None}},
                {"given_name": "Maria", "family_name": "Beispiel", "birthDate": "1961-03-12"}),
}


def run(case: dict) -> tuple[str, dict, object]:
    req, ident = ROUTES[case["route"]]
    raw = (FRESH / case["file"]).read_bytes()
    p = fhirmap.parse_upload(raw, req["doc_kind"])
    d = decide(p, req, PATIENT, ident, raw)
    out = d["outcome"]
    if out == "FILE":
        assert p.ok_to_file, (case["id"], p.kind, p.hold_reasons)
        res = fhirmap.build_resources(p, "Patient/maria-beispiel", {**req, "request_id": f"req-{case['id']}"},
                                      {**ident, "simulated": True}, "2026-10-30T10:00:00+01:00")
        proof = fhirmap.prove(p, res, PATIENT)
        bad = [f for f in proof.get("fields") or [] if not f.get("equal")] + [
            r for r in proof.get("rows") or [] if r.get("status") != "equal"]
        assert proof["all_equal"], (case["id"], proof.get("summary"), bad)
    return out, d, p


@pytest.mark.parametrize("case", ALL, ids=[c["id"] for c in ALL])
def test_fresh_document_outcome(case):
    out, d, _ = run(case)
    assert out == case["expected"], (case["id"], case["why"], d["codes"])


def _by_id(i):
    return next(c for c in ALL if c["id"] == i)


def test_fresh_values_read_exactly():
    """Spot checks: values, ranges and test names from layouts the parser was not built for."""
    _, _, p = run(_by_id("F02"))                      # bullet list
    assert [(r["test"], r["value"], r["unit"], r["ref_low"], r["ref_high"], r["loinc"]) for r in p.lab] == [
        ("INR", 0.9, None, 0.8, 1.2, "6301-6"), ("aPTT", 29, "s", 25, 37, "14979-9"),
        ("Platelets", 268, "10^9/L", 150, 400, "777-3"), ("Fibrinogen", 2.8, "g/L", 2.0, 4.0, "3255-7")]
    _, _, p = run(_by_id("F11"))                      # '(Referenz < 200) [LOINC 2093-3]'
    assert [(r["value"], r["ref_low"], r["ref_high"], r["loinc"]) for r in p.lab] == [
        (212, None, 200, "2093-3"), (58, 45, None, "2085-9"), (131, None, 116, "13457-7"), (115, None, 150, "2571-8")]
    assert p.author["name"] == "Dr. Eva Brandl" and p.report_status == "final"
    _, _, p = run(_by_id("F06"))                      # 'Referenz' column -> referenceRange, not a note
    assert all(r["ref_low"] is not None and not r["notes"] for r in p.lab)
    _, _, p = run(_by_id("X05"))                      # 'Analyt' column -> code.text
    assert p.lab[0]["test"] == "Natrium"
    _, _, p = run(_by_id("F12"))                      # 'Wert | Ergebnis': 'Wert' names the test
    assert [(r["test"], r["value"]) for r in p.lab] == [("TSH", 1.8), ("Hämoglobin", 13.4), ("Kreatinin", 0.82)]
    assert p.author is None                           # the signature is the patient herself, not a performer


def test_fresh_people_and_stay():
    for i, who in (("F18", "Dr. Eva Brandl"), ("F03", "Dr. Eva Brandl"), ("F05", "Dr. Eva Brandl"),
                   ("F13", "Dr. Eva Brandl"), ("V01", "OA Dr. Eva Brandl"), ("V02", "Dr. Eva Brandl")):
        _, _, p = run(_by_id(i))
        assert p.author and p.author["name"] == who, (i, p.author)
    _, _, p = run(_by_id("F15"))                      # 'Admission: … · Discharge: …'
    assert (p.stay["start"], p.stay["end"]) == ("2026-09-02", "2026-09-23")
    _, _, p = run(_by_id("F16"))
    assert p.stay and p.stay["end"] == "2026-09-23" and p.author["name"] == "Dr. Johanna Leitner"
    _, _, p = run(_by_id("F10"))                      # sample + report time on one '·' line
    assert p.sample_time.startswith("2026-10-26T08:15") and p.report_time.startswith("2026-10-26T12:00")


def test_fresh_cards():
    from fhirmap import card_facts, reason_text_de
    _, _, p = run(_by_id("F14"))                      # medication as a table
    meds = card_facts(p)["medications"]
    assert meds[0] == "Rivaroxaban 10 mg 1-0-0 bis 29.09.2026" and len(meds) == 4
    _, _, p = run(_by_id("F17"))                      # bold inline 'Entlassmedikation:' / 'Weiteres Vorgehen:'
    f = card_facts(p)
    assert f["medications"][0].startswith("Rivaroxaban 10 mg") and len(f["medications"]) == 3
    assert f["recommendations"][0].startswith("Physiotherapie ambulant")
    _, _, p = run(_by_id("F16"))                      # '### Medikation' (and '### Therapien' is not medication)
    assert card_facts(p)["medications"][0].startswith("Rivaroxaban")
    assert not any("Ergotherapie" in m for m in card_facts(p)["medications"])
    _, d, p = run(_by_id("H03"))
    assert d["codes"] == ["ORG_MISMATCH"]
    txt = reason_text_de("ORG_MISMATCH", d["details"])
    assert "Labor laut Dokument: Labor Dr. Gruber & Partner, Linz – angefragt: Labor Kanalstraße" in txt, txt
    _, d, p = run(_by_id("F11"))                      # no false 'another person' / second report
    assert d["outcome"] == "FILE" and not d["other_person_present"]
    assert not any(f["code"] == "MULTIPLE_REPORTS" for f in p.findings)


def test_second_person_never_unnoticed():
    for i in ("H01", "H06", "S01", "S02", "S03", "S06", "S07"):
        _, d, _ = run(_by_id(i))
        assert d["outcome"] == "BLOCK" and d["codes"][0] == "OTHER_PATIENT_PRESENT", (i, d["codes"])
    for i in ("S04", "S08", "S09", "B01", "B02"):
        _, d, _ = run(_by_id(i))
        assert d["outcome"] == "BLOCK" and d["codes"][0] == "WRONG_PATIENT", (i, d["codes"])


def test_holdout_fields():
    from fhirmap import card_facts, reason_text_de
    _, d, p = run(_by_id("holdout:L12"))                 # letter subject line: only the person is the name
    assert p.patient["name"] == "Maria Beispiel" and d["outcome"] == "FILE"
    _, d, _ = run(_by_id("holdout:Y04"))
    assert "Gerinnungsbefund" not in json.dumps(d["details"], ensure_ascii=False)
    _, d, _ = run(_by_id("holdout:R06"))                 # slash date: said so, not 'missing'
    assert d["codes"] == ["DOB_AMBIGUOUS"]
    assert reason_text_de("DOB_AMBIGUOUS", d["details"]) == \
        "Geburtsdatum mehrdeutig geschrieben (12/03/1961) – bitte prüfen."
    _, _, p = run(_by_id("holdout:L06"))                 # 'Ref. interval' -> referenceRange
    assert [(r["ref_low"], r["ref_high"]) for r in p.lab][:2] == [(4.0, 6.0), (20, 42)]
    assert not any(r["notes"] for r in p.lab)
    _, _, p = run(_by_id("holdout:L05"))                 # 'Abnahmedatum: 23.10.26'
    assert p.sample_time == "2026-10-23"
    _, _, p = run(_by_id("holdout:L11"))                 # 'Probe: Mittelstrahlharn, 26.10.2026, 07:20'
    assert p.sample_time.startswith("2026-10-26T07:20")
    _, _, p = run(_by_id("holdout:Y09"))
    assert p.sample_time.startswith("2026-10-23T07:40") and p.report_time.startswith("2026-10-23T10:15")
    _, _, p = run(_by_id("holdout:Y10"))
    assert p.sample_time is None                         # 2 years + back: never guessed
    _, _, p = run(_by_id("holdout:L09"))                 # dotted leaders, pending row kept
    assert [r["test"] for r in p.lab] == ["TSH", "fT4", "TPO-Antikörper"] and p.lab[2]["status"] == "registered"
    for i, who in (("holdout:R01", "Dr. Johanna Leitner"), ("holdout:R04", "Dr. Johanna Leitner"),
                   ("holdout:R05", "Dr. J. Leitner"), ("holdout:L03", "Dr. Eva Brandl"), ("holdout:Y12", "Dr. Eva Brandl")):
        _, _, p = run(_by_id(i))
        assert p.author and p.author["name"] == who, (i, p.author)
    _, _, p = run(_by_id("holdout:R05"))
    assert (p.stay["start"], p.stay["end"]) == ("2026-09-02", "2026-09-23")
    assert card_facts(p)["medications"] == ["Rivaroxaban 10 mg 1x tgl. bis 29.09.2026", "Pantoprazol 20 mg 1x tgl."]
    _, _, p = run(_by_id("holdout:Y11"))                 # bullets + comment ends at the signature
    assert [(r["test"], r["value"], r["unit"], r["ref_low"], r["ref_high"]) for r in p.lab] == [
        ("INR", 1.0, None, 0.8, 1.2), ("Hb", 13.2, "g/dl", 12, 16)]
    assert p.comment == "Werte unauffällig."
    assert (p.author["name"], p.author["role"]) == ("Dr. Eva Brandl", "Laborärztin")
    _, _, p = run(_by_id("holdout:Y12"))
    assert p.comment == "Gerinnung im Normbereich."


N3_NOTE = ('Hinweis Präanalytik: Das Probenröhrchen war mit „Beispiel, Max, 01.01.1960" beschriftet, der '
           'Anforderungsschein mit „Beispiel, Maria, 12.03.1961". Zuordnung bitte klinisch prüfen.')


@pytest.mark.parametrize("note,outcome,code", [
    (N3_NOTE, "BLOCK", "OTHER_PATIENT_PRESENT"),                                   # holdout3 N3 as written
    ("Hinweis: Röhrchen-Etikett „Max Beispiel, geb. 01.01.1960“.", "BLOCK", "OTHER_PATIENT_PRESENT"),
    ("Hinweis: Etikett Beispiel, Max, 01.01.1960.", "BLOCK", "OTHER_PATIENT_PRESENT"),   # no doubt words at all
    ("Anmerkung: Name: Max Beispiel Geb.: 01.01.1960", "BLOCK", "OTHER_PATIENT_PRESENT"),
    ("| Hinweis | Probe von Max Beispiel, geb. 01.01.1960 |\n|---|---|\n| Status | offen |", "BLOCK",
     "OTHER_PATIENT_PRESENT"),                                                        # inside a table cell
    ("Hinweis: Verwechslung nicht ausgeschlossen.", "HOLD", "IDENTITY_DOUBT"),
    ("Hinweis: Zuordnung bitte klinisch prüfen.", "HOLD", "IDENTITY_DOUBT"),
    ("Note: sample labelled with another patient's name; identity to be confirmed.", "HOLD", "IDENTITY_DOUBT"),
    ("Hinweis: Probe korrekt beschriftet, Identität per Armband bestätigt.", "FILE", "OK"),   # a confirmation
    ("Vorwert 15.10.2025 von Beispiel, Maria, 12.03.1961: INR 1,1.", "FILE", "OK"),         # the patient herself
])
def test_n3_second_person_or_identity_doubt_in_a_lab_comment(note, outcome, code):
    """holdout3 N3 was FILED: the lab's comment said the tube carried 'Beispiel, Max, 01.01.1960'."""
    req, ident = ROUTES["lab"]
    text = (FRESH / "holdout3" / "docs" / "N3.md").read_text(encoding="utf-8")
    assert N3_NOTE in text
    text = text.replace(N3_NOTE, note)
    p = fhirmap.parse_answer(text, "lab_result")
    d = decide(p, req, PATIENT, ident, text.encode("utf-8"))
    assert (d["outcome"], d["codes"][0]) == (outcome, code), (note, d["codes"])
    if outcome == "BLOCK":
        assert d["details"] == [{"field": "other_person_present", "other_person_present": True}]   # never who


def test_holdout3_fields():
    from fhirmap import card_facts, reason_text_de
    _, d, p = run(_by_id("holdout3:G04"))                # name · DOB · SVNR on one line, SVNR checked
    assert d["outcome"] == "FILE" and p.patient["svnr"]["ddmmyy"] == "120361"
    _, d, _ = run(_by_id("holdout3:Z01"))
    assert d["codes"][0] == "SVNR_MISMATCH"
    _, d, p = run(_by_id("holdout3:G06"))                # em dash is a separator
    assert p.patient["name"] == "Maria Beispiel" and p.author["name"] == "Dr. Eva Brandl"
    _, d, _ = run(_by_id("holdout3:G08"))
    assert reason_text_de("DOB_AMBIGUOUS", d["details"]) == "Geburtsjahr zweistellig (12.03.61) – bitte prüfen."
    _, _, p = run(_by_id("holdout3:G08"))                # GFM short row read, its LOINC empty
    assert [r["test"] for r in p.lab][0] == "Quick" and p.lab[0]["loinc"] is None
    _, _, p = run(_by_id("holdout3:G07"))                # specific gravity 1,015
    sg = next(r for r in p.lab if r["test"] == "Spez. Gewicht")
    assert (sg["value"], sg["ref_low"], sg["ref_high"]) == (1.015, 1.005, 1.03)
    for i, period in (("R2", ("2026-09-02", "2026-09-23")), ("R3", ("2026-09-02", "2026-09-23")),
                      ("R4", ("2026-09-02", "2026-09-23")), ("R5", ("2026-09-02", "2026-09-23")),
                      ("R6", ("2026-09-02", "2026-09-23")), ("Z09", ("2026-09-01", "2026-09-22"))):
        _, _, p = run(_by_id("holdout3:" + i))
        assert p.stay and (p.stay["start"], p.stay["end"]) == period, (i, p.stay)
    for i in ("R1", "R2", "R3", "R4", "R5", "Z10"):          # discharge medication on every card
        _, _, p = run(_by_id("holdout3:" + i))
        meds = card_facts(p).get("medications") or []
        assert meds and meds[0].startswith("Rivaroxaban 10 mg"), (i, meds)
    for i in ("R3", "Z10"):                                  # no signature / contact lines in the card lists
        _, _, p = run(_by_id("holdout3:" + i))
        f = card_facts(p)
        txt = " ".join((f.get("recommendations") or []) + (f.get("medications") or []))
        assert "Leitner" not in txt and "Waldweg" not in txt and "Kern" not in txt, (i, txt)
    _, _, p = run(_by_id("holdout3:G09"))                    # signature does not bleed into the lab comment
    assert "validated" not in (p.comment or "") and p.author["name"].startswith("Eva Brandl")
    _, _, p = run(_by_id("holdout3:R6"))
    assert p.author["name"] == "Dr. Johanna Leitner"

"""fhirmap tests. Run from the repo root:  uv run pytest fhirmap/tests -q

The validator test runs the HL7 validator ONCE over every resource built here
(mode from FHIRMAP_VALIDATE_MODE: auto|tx|offline, default auto; set
FHIRMAP_SKIP_VALIDATOR=1 to skip it).
"""
from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("PGK_LOINC_ONLINE", "0")      # deterministic: LOINC facts from the committed cache only

from fhirmap import (compare_dobs, compare_names, decide, identity_matches)
from fhirmap import (audit_event, build_resources, check_answer, build_task, build_transaction, codesystems, name_match_level,
                     loose_name_equal, parse_answer, person_mismatch_reason, prove, same_person,
                     task_update, verify_audit_chain)
from fhirmap.identity import parse_date, parse_datetime_ex
from fhirmap.parse import loinc_check_ok
from fhirmap.units import to_ucum

HERE = Path(__file__).resolve().parent
VAR = HERE / "variants"
JURY = HERE.parents[1] / "tests" / "jury"
DOCS = JURY / "corpus" / "_scenario"                # the brief's scenario documents, byte for byte
PATIENT = json.loads((HERE.parents[1] / "infra" / "patient.json").read_text(encoding="utf-8"))
RECEIVED = "2026-10-29T10:05:00+01:00"

EVA = {"given_name": "Eva", "family_name": "Brandl", "birthDate": "1975-05-05", "bpk": "SYNTH-BPK-GH-EVA",
       "loa": "high", "assertion": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJldmEifQ.c3ludGg",
       "verified_at": "2026-10-29T10:02:00+01:00"}
KERN = {"given_name": "Stefan", "family_name": "Kern", "birthDate": "1980-02-02", "bpk": "SYNTH-BPK-GH-KERN",
        "loa": "high", "assertion": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJrZXJuIn0.c3ludGg"}
REQ_LAB = {"request_id": "req-lab-1", "requester_mxid": "@lena.hofer:klinikum.example", "doc_kind": "lab_result",
           "recipient": {"name": "Dr. Eva Brandl", "role": "Laborärztin", "org": "Labor Kanalstraße"},
           "created_at": "2026-10-29T09:00:00+01:00", "expires_at": "2026-11-05T09:00:00+01:00"}
REQ_REHA = {"request_id": "req-reha-1", "requester_mxid": "@lena.hofer:klinikum.example",
            "doc_kind": "discharge_report",
            "recipient": {"name": "Stefan Kern", "role": "Entlassungsmanagement", "org": "Reha Waldblick"},
            "created_at": "2026-10-29T09:00:00+01:00"}

# variant file -> (hint, expected kind, expected ok_to_file)
VARIANTS = {
    "v01_german_lab": ("lab_result", "lab_result", True),
    "v02_umlaut_patient": ("lab_result", "lab_result", True),
    "v03_missing_unit": ("lab_result", "lab_result", False),
    "v04_missing_patient": ("lab_result", "unknown", False),
    "v05_extra_columns": ("lab_result", "lab_result", True),
    "v06_no_loinc": ("lab_result", "lab_result", True),
    "v07_bad_loinc": ("lab_result", "lab_result", False),
    "v08_unit_mismatch": ("lab_result", "lab_result", False),
    "v09_creatinine_umol_mass_loinc": ("lab_result", "lab_result", False),
    "v10_ambiguous_dob": ("lab_result", "lab_result", False),
    "v11_two_patients": ("lab_result", "unknown", False),
    "v12_german_discharge": ("discharge_report", "discharge_report", True),
    "v13_day_month_swap": ("lab_result", "lab_result", True),     # files fine, but is NOT Maria
    "v14_textual_values": ("lab_result", "lab_result", True),
    "v15_bold_labels_no_pipes": ("lab_result", "lab_result", True),
    "v16_generic_report": ("discharge_report", "discharge_report", True),
    "v17_ambiguous_number": ("lab_result", "lab_result", False),
    "v18_us_date_titles": ("lab_result", "lab_result", True),
    "v19_bad_row": ("lab_result", "lab_result", False),
    "v20_winter_time": ("lab_result", "lab_result", True),
    "v21_wrong_kind": ("lab_result", "discharge_report", False),
    "v22_paren_dob_family_first": ("lab_result", "lab_result", True),
    "v23_month_name_stay": ("discharge_report", "discharge_report", True),
    "v24_no_dob": ("lab_result", "lab_result", False),
    "v25_unknown_unit": ("lab_result", "lab_result", False),  # UNKNOWN_UNIT: a UCUM code is never invented
    "v26_bold_values": ("lab_result", "lab_result", True),    # Markdown emphasis in cells
    "v27_german_labels": ("lab_result", "lab_result", True),  # Probenahme / Befunddatum / Einsender, G/L, T/L, FEU
}


def doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def var(name: str) -> str:
    return (VAR / f"{name}.md").read_text(encoding="utf-8")


def build_for(p, kind):
    if kind == "lab_result":
        return build_resources(p, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
    return build_resources(p, "Patient/maria-beispiel", REQ_REHA, KERN, RECEIVED)


# =========================================================================== identity

class TestDates(unittest.TestCase):
    def test_formats(self):
        for s in ["12 March 1961", "12.03.1961", "12.3.1961", "1961-03-12", "12. März 1961", "12. Maerz 1961",
                  "12 Mär 1961", "March 12, 1961", "12th of March 1961", "12 Mar 1961", "12-March-1961".replace("-", " ")]:
            self.assertEqual(parse_date(s).isoformat(), "1961-03-12", s)
        self.assertEqual(parse_date("1. Jänner 2000").isoformat(), "2000-01-01")
        self.assertEqual(parse_date("3. Feber 2000").isoformat(), "2000-02-03")
        self.assertEqual(parse_date("13/03/1961").isoformat(), "1961-03-13")

    def test_rejects(self):
        for s in ["12/03/1961", "12.03.61", "31.02.1961", "1961", "", "Mittwoch", "32.01.2000"]:
            self.assertIsNone(parse_date(s), s)

    def test_vienna_offsets(self):
        self.assertEqual(parse_datetime_ex("22 October 2026, 08:10").value.isoformat(), "2026-10-22T08:10:00+02:00")
        self.assertEqual(parse_datetime_ex("2.11.2026 07:30 Uhr").value.isoformat(), "2026-11-02T07:30:00+01:00")
        self.assertEqual(parse_datetime_ex("2026-10-22T06:10:00Z").value.isoformat(), "2026-10-22T06:10:00+00:00")
        self.assertIsNone(parse_datetime_ex("29.03.2026 02:30").value)          # DST gap
        self.assertTrue(parse_datetime_ex("25.10.2026 02:30").notes)          # DST fold


class TestSamePerson(unittest.TestCase):
    MARIA = {"name": "Maria Beispiel", "birthDate": "1961-03-12"}

    def test_scenario_patient(self):
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        self.assertTrue(same_person(p, PATIENT))
        self.assertTrue(same_person(p.patient, PATIENT))

    def test_max_is_not_maria(self):
        p = parse_answer(doc("lab-result-max-beispiel.md"), "lab_result")
        self.assertEqual(p.patient["name"], "Max Beispiel")
        self.assertEqual(p.patient["birthDate"], "1960-01-01")
        self.assertFalse(same_person(p, PATIENT))
        self.assertFalse(same_person(p.patient, self.MARIA))
        r = person_mismatch_reason(p, PATIENT)
        self.assertIn("birth date", r)
        self.assertIn("name", r)

    def test_day_month_swap_is_not_a_match(self):
        self.assertFalse(same_person({"name": "Maria Beispiel", "birthDate": "03.12.1961"}, self.MARIA))
        p = parse_answer(var("v13_day_month_swap"), "lab_result")
        self.assertEqual(p.patient["birthDate"], "1961-12-03")
        self.assertFalse(same_person(p, PATIENT))

    def test_normalisation_matches(self):
        m = self.MARIA
        for n in ["MARIA BEISPIEL", "Beispiel, Maria", "Dr. Maria Beispiel", "Frau Mag.a Maria Beispiel, MSc",
                  "maria  beispiel"]:
            self.assertTrue(same_person({"name": n, "birthDate": "12.03.1961"}, m), n)
        j = {"name": "Jürgen Größmann", "birthDate": "1950-05-05"}
        for n in ["Juergen Groessmann", "JÜRGEN GRÖSSMANN", "Jürgen Groessmann", "Jürgen  Größmann"]:
            self.assertTrue(same_person({"name": n, "birthDate": "5. Mai 1950"}, j), n)
        # ICAO 9303: ü is 'ue', not 'u' -> 'Jurgen' is NEAR, not SAME
        self.assertFalse(same_person({"name": "Jurgen Grossmann", "birthDate": "1950-05-05"}, j))
        self.assertTrue(same_person({"given": ["Anna"], "family": "Müller-Lüdenscheidt", "birthDate": "1970-01-02"},
                                    {"name": "Anna Mueller-Luedenscheidt", "birthDate": "1970-01-02"}))

    def test_non_matches(self):
        m = self.MARIA
        self.assertFalse(same_person({"name": "Maria Anna Beispiel", "birthDate": "1961-03-12"}, m))  # middle name
        self.assertFalse(same_person({"name": "M. Beispiel", "birthDate": "1961-03-12"}, m))           # initial
        self.assertFalse(same_person({"name": "Maria Beispiel", "birthDate": None}, m))                # no DOB
        self.assertFalse(same_person({"name": "Maria Beispiel", "birthDate": "03/04/1961"}, m))        # ambiguous
        self.assertFalse(same_person({"name": "Maria Beispiel", "birthDate": "1961-03-13"}, m))
        self.assertFalse(same_person({"name": "Marie Beispiel", "birthDate": "1961-03-12"}, m))
        self.assertFalse(same_person({"name": "Mueller", "birthDate": "1961-03-12"},
                                     {"name": "Muller", "birthDate": "1961-03-12"}))                # no umlaut link

    def test_title_position(self):
        from fhirmap import rule_tokens
        self.assertEqual(rule_tokens("Univ.-Prof. Dr. Eva  Brandl-Huber, MSc"), ["eva", "brandl-huber"])
        self.assertEqual(rule_tokens("Ma Lin"), ["ma", "lin"])            # 'Ma' is a surname, not a degree
        self.assertEqual(rule_tokens("Jürgen Größmann"), ["juergen", "groessmann"])
        self.assertTrue(loose_name_equal("Dr.in Eva Brandl", "Eva Brandl"))

    def test_same_person_follows_rules_md(self):
        m = {"name": "Maria Beispiel", "birthDate": "1961-03-12"}
        self.assertFalse(same_person({"name": "Beispiel Maria", "birthDate": "1961-03-12"}, m))   # order matters
        self.assertTrue(same_person({"name": "Beispiel, Maria", "birthDate": "1961-03-12"}, m))   # comma = family first
        self.assertFalse(same_person({"name": "Märia Beispiel", "birthDate": "1961-03-12"}, m))   # ä != a
        self.assertTrue(same_person({"name": "María Beispiel", "birthDate": "1961-03-12"}, m))    # accents dropped
        self.assertFalse(same_person({"name": "Maria Beispiel-Huber", "birthDate": "1961-03-12"}, m))

    def test_old_loose_helpers_are_deprecated(self):
        import warnings
        import fhirmap
        self.assertNotIn("names_match", fhirmap.__all__)
        self.assertNotIn("normalize_name", fhirmap.__all__)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            fhirmap.names_match("a b", "b a")
            fhirmap.normalize_name("a")
        self.assertEqual([x.category for x in w], [DeprecationWarning, DeprecationWarning])

    def test_eid_binding_levels(self):
        self.assertEqual(name_match_level("Dr. Eva Brandl", {"given_name": "Eva", "family_name": "Brandl"}), "match")
        self.assertEqual(name_match_level("Dr. Eva Brandl", {"given_name": "Eva Maria", "family_name": "Brandl"}),
                         "match")
        self.assertEqual(name_match_level("Dr. Eva Brandl", {"given_name": "Eva", "family_name": "Brandl-Huber"}),
                         "mismatch")                                    # identity is binary (RULES rule 1)
        self.assertEqual(name_match_level("Dr. Eva Brandl", {"given_name": "Eve", "family_name": "Brandl"}),
                         "mismatch")
        self.assertEqual(name_match_level("Dr. Eva Brandl", {"given_name": "Karl", "family_name": "Kollege"}),
                         "mismatch")
        self.assertEqual(name_match_level("Dr. Eva Brandl", {"given_name": "Eva", "family_name": "Brandt"}),
                         "mismatch")


# =========================================================================== units / codes

class TestUnitsAndCodes(unittest.TestCase):
    def test_ucum(self):
        cases = {"10^9/L": "10*9/L", "G/L": "10*9/L", "G/l": "10*9/L", "Gpt/L": "10*9/L", "x10^9/l": "10*9/L",
                 "T/l": "10*12/L", "s": "s", "sec": "s", "g/L": "g/L", "g/l": "g/L", "µmol/L": "umol/L",
                 "μmol/l": "umol/L", "mmol/l": "mmol/L", "mg/dl": "mg/dL", "IU/mL": "[IU]/mL", "IE/l": "[IU]/L",
                 "U/l": "U/L", "%": "%", "fl": "fL", "Tsd/µl": "10*3/uL", "mL/min/1.73m²": "mL/min/{1.73_m2}"}
        for u, code in cases.items():
            self.assertEqual(to_ucum(u)[0], code, u)
        self.assertEqual(to_ucum("")[0], None)
        self.assertIsNone(to_ucum("Klumpen/Feld")[0])
        self.assertIsNotNone(to_ucum("Klumpen/Feld")[1])

    def test_loinc_cache_example_units_are_single_codes(self):
        from fhirmap.loinc import _load
        for code, v in _load().items():
            u = (v or {}).get("example_ucum") or ""
            self.assertFalse(any(ch in u for ch in ";, "), (code, u))
        self.assertEqual(_load()["6598-7"]["example_ucum_list"], ["ug/L", "ng/mL"])

    def test_loinc_check_digit(self):
        for c in ["6301-6", "14979-9", "777-3", "3255-7", "11502-2", "18842-5", "2160-0", "718-7"]:
            self.assertTrue(loinc_check_ok(c), c)
        for c in ["6301-5", "777-4", "3255-0", "abc", "6301"]:
            self.assertFalse(loinc_check_ok(c), c)


# =========================================================================== parsing

class TestScenarioParse(unittest.TestCase):
    def test_lab_maria(self):
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        self.assertEqual(p.kind, "lab_result")
        self.assertTrue(p.ok_to_file, p.warnings)
        self.assertEqual(p.patient["name"], "Maria Beispiel")
        self.assertEqual(p.patient["birthDate"], "1961-03-12")
        self.assertEqual(p.author, {"name": "Dr. Eva Brandl", "role": "lab physician", "org": None})
        self.assertEqual(p.org, {"name": "Labor Kanalstraße"})
        self.assertEqual(p.requester["name"], "Dr. Lena Hofer")
        self.assertEqual(p.sample_time, "2026-10-22T08:10:00+02:00")
        self.assertEqual(p.report_time, "2026-10-22T11:40:00+02:00")
        rows = [(r["test"], r["value"], r["unit"], r["ucum"], r["ref_low"], r["ref_high"], r["loinc"]) for r in p.lab]
        self.assertEqual(rows, [
            ("INR (prothrombin time)", 1.0, None, "{INR}", 0.8, 1.2, "6301-6"),
            ("aPTT", 31, "s", "s", 25, 37, "14979-9"),
            ("Platelets", 245, "10^9/L", "10*9/L", 150, 400, "777-3"),
            ("Fibrinogen", 3.1, "g/L", "g/L", 2.0, 4.0, "3255-7")])
        self.assertTrue(all(r["interpretation"] is None for r in p.lab))   # no derived flags
        self.assertEqual(p.comment, "All values are within the reference range.")

    def test_lab_max(self):
        p = parse_answer(doc("lab-result-max-beispiel.md"), "lab_result")
        self.assertEqual(p.kind, "lab_result")
        self.assertEqual([r["value"] for r in p.lab], [2.6, 44, 198, 3.4])
        self.assertIn("phenprocoumon", p.comment)

    def test_rehab(self):
        p = parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")
        self.assertEqual(p.kind, "discharge_report")
        self.assertTrue(p.ok_to_file, p.warnings)
        self.assertTrue(p.doc_type_confirmed)
        self.assertEqual(p.author["name"], "Dr. Johanna Leitner")
        self.assertEqual(p.author["org"], "Reha Waldblick")
        self.assertEqual(p.sender["name"], "Stefan Kern")
        self.assertEqual(p.stay["start"], "2026-09-02")
        self.assertEqual(p.stay["end"], "2026-09-23")
        self.assertEqual([s["title"] for s in p.sections], ["Course", "Medication at discharge", "Recommendations"])
        self.assertTrue(p.reason.startswith("rehabilitation after a total hip replacement"))


class TestVariants(unittest.TestCase):
    def test_variant_table(self):
        self.assertGreaterEqual(len(VARIANTS), 15)
        for name, (hint, kind, ok) in VARIANTS.items():
            with self.subTest(name):
                p = parse_answer(var(name), hint)
                self.assertEqual(p.kind, kind, p.warnings)
                self.assertEqual(p.ok_to_file, ok, p.warnings)
                if not ok:
                    self.assertTrue(p.warnings)

    def test_details(self):
        p = parse_answer(var("v01_german_lab"), "lab_result")
        self.assertEqual((p.patient["given"], p.patient["family"], p.patient["birthDate"]),
                         (["Anna"], "Müller", "1975-04-03"))
        self.assertEqual([r["value"] for r in p.lab], [4.5, 139, 78, 0.6])
        self.assertEqual(p.lab[3]["comparator"], "<")
        self.assertEqual((p.lab[3]["ref_low"], p.lab[3]["ref_high"]), (None, 5))
        self.assertEqual(p.lab[2]["ucum"], "umol/L")
        self.assertEqual(p.comment, "Kommentar: Unauffälliger Befund.")
        p = parse_answer(var("v04_missing_patient"), "lab_result")
        self.assertTrue(any("no patient line" in w for w in p.hold_reasons))
        p = parse_answer(var("v05_extra_columns"), "lab_result")
        self.assertEqual([r["flag"] for r in p.lab], ["H", "L", None])        # only flags the lab stated
        self.assertEqual(p.lab[0]["notes"], ["Method: Clotting assay"])
        p = parse_answer(var("v07_bad_loinc"), "lab_result")
        self.assertTrue(any("check digit" in w for w in p.hold_reasons))
        p = parse_answer(var("v08_unit_mismatch"), "lab_result")
        self.assertTrue(any("does not fit LOINC 3255-7" in w for w in p.hold_reasons))
        p = parse_answer(var("v02_umlaut_patient"), "lab_result")
        self.assertEqual(p.lab[1]["ucum"], "10*9/L")                            # G/L = giga per litre
        self.assertTrue(same_person(p, {"name": "Juergen Groessmann", "birthDate": "1950-05-05"}))
        p = parse_answer(var("v12_german_discharge"), "discharge_report")
        self.assertEqual(p.patient["birthDate"], "1961-03-12")
        self.assertEqual(p.author["name"], "Dr.in Johanna Leitner")
        self.assertEqual(p.sender["name"], "Stefan Kern")
        p = parse_answer(var("v16_generic_report"), "discharge_report")
        self.assertFalse(p.doc_type_confirmed)
        p = parse_answer(var("v20_winter_time"), "lab_result")
        self.assertEqual(p.sample_time, "2025-11-02T07:30:00+01:00")
        p = parse_answer(var("v22_paren_dob_family_first"), "lab_result")
        self.assertTrue(same_person(p, PATIENT))
        self.assertEqual(p.lab[0]["value"], 3.1)
        p = parse_answer(var("v25_unknown_unit"), "lab_result")
        self.assertEqual((p.lab[0]["unit"], p.lab[0]["ucum"]), ("/HPF", None))
        self.assertIn("UNKNOWN_UNIT", [f["code"] for f in p.findings if f["outcome"] == "HOLD"])
        p = parse_answer(var("v23_month_name_stay"), "discharge_report")
        self.assertEqual((p.stay["start"], p.stay["end"]), ("2025-12-28", "2026-01-15"))

    def test_garbage_never_crashes(self):
        for t in ["", " ", "|||", "# x\n|a|b|\n|-|-|\n|1|", "Patient:", "Patient: , born", "Patient: X, born 99.99.9999",
                  "\x00�", "| Test | Result |\n|---|---|\n| INR | 1,0,0 |"]:
            p = parse_answer(t, "lab_result")
            self.assertFalse(p.ok_to_file, t)


# =========================================================================== build / prove

FILED: list[tuple[str, list[dict]]] = []        # (label, resources) collected for the single validator run


def _all_filable():
    out = [("lab-maria", parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")),
           ("rehab-maria", parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")),
           ("lab-max", parse_answer(doc("lab-result-max-beispiel.md"), "lab_result"))]
    for name, (hint, _, ok) in VARIANTS.items():
        if ok:
            out.append((name, parse_answer(var(name), hint)))
    return out


class TestBuildProve(unittest.TestCase):
    def test_build_and_prove_everything(self):
        for label, p in _all_filable():
            with self.subTest(label):
                res = build_for(p, p.kind)
                pr = prove(p, res, patient=PATIENT if same_person(p, PATIENT) else None)
                self.assertTrue(pr["all_equal"], json.dumps([f for f in pr["fields"] if not f["equal"]] +
                                                           [r for r in pr["rows"] if r["status"] != "equal"],
                                                           ensure_ascii=False))
                self.assertEqual(pr["values_in_source"], pr["values_in_fhir"])
                self.assertEqual(pr["values_in_source"], len(p.lab))

    def test_lab_shape(self):
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        res = build_for(p, p.kind)
        types = [r["resourceType"] for r in res]
        # the signatory (as stated, no identifier) and the eID sender are separate Practitioners, never merged
        self.assertEqual(types, ["DiagnosticReport"] + ["Observation"] * 4 +
                         ["Practitioner", "Practitioner", "PractitionerRole", "Organization", "Provenance"])
        dr = res[0]
        self.assertEqual(dr["code"]["coding"][0], {"system": "http://loinc.org", "code": "11502-2",
                                                   "display": "Laboratory report"})
        self.assertEqual(dr["effectiveDateTime"], "2026-10-22T08:10:00+02:00")
        self.assertEqual(dr["issued"], "2026-10-22T11:40:00+02:00")
        self.assertEqual(dr["conclusion"], "All values are within the reference range.")
        inr = res[1]
        self.assertEqual(inr["code"]["coding"][0]["display"], "INR in Platelet poor plasma by Coagulation assay")
        self.assertEqual(inr["code"]["text"], "INR (prothrombin time)")
        self.assertEqual(inr["valueQuantity"], {"value": 1.0, "unit": "INR", "system": "http://unitsofmeasure.org",
                                                "code": "{INR}"})
        self.assertNotIn("interpretation", inr)
        plt = res[3]
        self.assertEqual(plt["valueQuantity"]["code"], "10*9/L")
        self.assertEqual(plt["referenceRange"][0]["text"], "150 to 400")
        self.assertEqual(res[5]["identifier"], [{"system": "https://pgk-bridge.example/fhir/sid/document-signatory",
                                                 "value": "eva-brandl@labor-kanalstrasse"}])   # signatory: stated name@org
        prac = res[6]
        self.assertEqual(prac["identifier"], [{"system": "https://pgk-bridge.example/fhir/sid/bpk-hmac-sha256",
                                               "value": prac["identifier"][0]["value"]}])
        self.assertNotIn("SYNTH-BPK", json.dumps(res))                 # bPK never in clear
        prov = res[-1]
        self.assertEqual(len(prov["target"]), len(res) - 1)
        agent_types = [a["type"]["coding"][0]["code"] for a in prov["agent"]]
        self.assertEqual(agent_types, ["author", "informant", "verifier"])
        self.assertNotEqual(prov["agent"][0]["who"]["reference"], prov["agent"][1]["who"]["reference"])
        self.assertEqual(prov["agent"][1]["who"]["reference"], f"urn:uuid:{prac['id']}")   # informant = eID person
        self.assertEqual(prov["signature"][0]["sigFormat"], "application/jose")
        self.assertIn("http://eidas.europa.eu/LoA/high", prov["policy"])
        self.assertIn("https://pgk-bridge.example/fhir/policy/lab-result-v1", prov["policy"])

    def test_rehab_author_is_not_sender(self):
        p = parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")
        res = build_for(p, p.kind)
        ix = {f"urn:uuid:{r['id']}": r for r in res}
        d = res[0]
        self.assertEqual(d["resourceType"], "DocumentReference")
        self.assertEqual(d["type"]["coding"][0], {"system": "http://loinc.org", "code": "11490-0",
                                                  "display": "Physician Discharge Summary"})
        self.assertEqual(d["category"][0]["coding"][0]["code"], "18842-5")
        self.assertEqual(d["context"]["period"], {"start": "2026-09-02", "end": "2026-09-23"})
        self.assertEqual(ix[d["author"][0]["reference"]]["name"][0]["text"], "Dr. Johanna Leitner")
        prov = res[-1]
        author = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "author")
        informant = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "informant")
        self.assertEqual(ix[author["who"]["reference"]]["name"][0]["text"], "Dr. Johanna Leitner")
        sender = ix[informant["who"]["reference"]]
        self.assertEqual(sender["name"][0]["text"], "Stefan Kern")
        self.assertIn("identifier", sender)
        self.assertEqual([i["system"] for i in ix[author["who"]["reference"]]["identifier"]],
                         ["https://pgk-bridge.example/fhir/sid/document-signatory"])     # never the bPK, never a register
        sig = prov["signature"][0]
        verifier = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "verifier")
        self.assertEqual(sig["who"], verifier["who"])                      # the service Device signs ...
        self.assertEqual(sig["who"]["type"], "Device")
        self.assertEqual(sig["onBehalfOf"]["reference"], informant["who"]["reference"])   # ... for the sender
        self.assertEqual(sig["type"], [{"system": "urn:iso-astm:E1762-95:2013", "code": "1.2.840.10065.1.12.1.5",
                                        "display": "Verification Signature"}])
        import base64
        self.assertEqual(base64.b64decode(d["content"][0]["attachment"]["data"]).decode(), p.text)

    def test_every_instant_has_vienna_offset(self):
        p = parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")
        res = build_resources(p, "Patient/maria-beispiel", REQ_REHA, KERN, "2026-10-29T19:08:03+00:00")
        self.assertEqual(res[0]["date"], "2026-10-29T20:08:03+01:00")
        self.assertEqual(res[-1]["recorded"], "2026-10-29T20:08:03+01:00")
        ev = audit_event("answer-filed", "x", "y", "2026-10-22T06:10:00Z", "success", "r")
        self.assertEqual(ev["recorded"], "2026-10-22T08:10:00+02:00")
        q = parse_answer(LAB.replace("22 October 2026, 08:10", "2026-10-22T06:10:00Z"), "lab_result")
        self.assertEqual(q.sample_time, "2026-10-22T08:10:00+02:00")

    def test_generic_report_has_text_only_type(self):
        p = parse_answer(var("v16_generic_report"), "discharge_report")
        d = build_for(p, p.kind)[0]
        self.assertEqual(d["type"], {"text": "Physiotherapie Mitte: report"})

    def test_prove_detects_tampering(self):
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        res = build_for(p, p.kind)
        for mutate, field in [(lambda r: r[2]["valueQuantity"].__setitem__("value", 32), "value"),
                              (lambda r: r[3]["valueQuantity"].__setitem__("unit", "G/L"), "unit"),
                              (lambda r: r[4]["referenceRange"][0]["high"].__setitem__("value", 4.5), "ref_high"),
                              (lambda r: r[1]["code"]["coding"][0].__setitem__("code", "34714-6"), "loinc"),
                              (lambda r: r[1].__setitem__("effectiveDateTime", "2026-10-22T08:10:00+01:00"),
                               "effective")]:
            t = copy.deepcopy(res)
            mutate(t)
            pr = prove(p, t)
            self.assertFalse(pr["all_equal"], field)
            self.assertTrue(any(d["field"] == field for r in pr["rows"] for d in r["diffs"]), field)
        t = copy.deepcopy(res)
        t[0]["result"].pop()
        pr = prove(p, t)
        self.assertFalse(pr["all_equal"])
        self.assertEqual(pr["values_in_fhir"], 3)
        t = copy.deepcopy(res)
        t[0]["presentedForm"][0]["data"] = t[0]["presentedForm"][0]["data"][:-8] + "QUFBQQ=="
        self.assertFalse(prove(p, t)["all_equal"])
        r = copy.deepcopy(res)
        r[-1]["target"].pop(0)
        self.assertFalse(prove(p, r)["all_equal"])
        # a different patient's document against Maria's Patient fails the patient check
        q = parse_answer(doc("lab-result-max-beispiel.md"), "lab_result")
        self.assertFalse(prove(q, build_for(q, q.kind), patient=PATIENT)["all_equal"])

    def test_refuses_to_build_held_or_unknown(self):
        with self.assertRaises(ValueError):
            build_for(parse_answer(var("v04_missing_patient"), "lab_result"), "lab_result")
        with self.assertRaises(ValueError):
            build_for(parse_answer(var("v07_bad_loinc"), "lab_result"), "lab_result")

    def test_deterministic_ids(self):
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        self.assertEqual(build_for(p, p.kind), build_for(p, p.kind))


class TestJuryRules(unittest.TestCase):
    """tests/jury/RULES.md, the judgement calls verbatim."""

    def test_names(self):
        R = "Maria Beispiel"
        same = ["Maria Beispiel", "María Beispiel", "Beispiel, Maria", "MARIA  BEISPIEL", "Dr. Maria Beispiel",
                "Frau Mag.a Maria Beispiel, MSc", "Maria\u00a0Beispiel"]
        near = ["Märia Beispiel", "Beispiel Maria", "Maria Theresia Beispiel", "Maria Beispiel-Huber", "Marie Beispiel",
                "Maria Bespiel", "M\u0430ria Beispiel"]                     # last: Cyrillic a
        different = ["Max Beispiel", "Anna Huber", "Maria Huber"]
        for n in same:
            self.assertEqual(compare_names(n, R), "same", n)
        for n in near:
            self.assertEqual(compare_names(n, R), "near", n)
        for n in different:
            self.assertEqual(compare_names(n, R), "different", n)
        self.assertEqual(compare_names("Hans Müller", "Hans Mueller"), "same")
        self.assertEqual(compare_names("Hans Muller", "Hans Müller"), "near")

    def test_dobs(self):
        R = "1961-03-12"
        for d, want in [("1961-03-12", "same"), ("1961-12-03", "near"), ("1961-03-13", "near"), ("1951-03-12", "near"),
                        ("1961-08-12", "near"), ("1916-03-12", "near"), ("ambiguous", "near"), (None, "near"),
                        ("1960-01-01", "different"), ("1958-07-14", "different")]:
            self.assertEqual(compare_dobs(d, R), want, d)

    def test_identity_rule_v2(self):
        for req, given, family, want in [
                ("Dr. Eva Brandl", "Eva", "Brandl", True),
                ("Dr. Eva Brandl", "Eva Maria", "Brandl", True),       # eID may add given names
                ("Dr. Eva Brandl", "Eva-Maria", "Brandl", True),       # hyphens split given names
                ("Brandl Eva", "Eva", "Brandl", True),                 # typed order is free text
                ("Eva Maria Brandl", "Eva", "Brandl", False),          # requested given names must all be in the eID
                ("Dr. Eva Brandl", "Eva", "Brandl-Huber", False),      # family exact
                ("Eva Brandl-Huber", "Eva", "Brandl", False),
                ("Eva Brandl-Huber", "Eva", "Brandl-Huber", True),
                ("Dr. Brandl", "Eva", "Brandl", False),                # a given name must be requested
                ("Dr. Eva Brandl", "Eve", "Brandl", False),
                ("Dr. Eva Brandl", "Thomas", "Huber", False),
                ("Eva Brändl", "Eva", "Braendl", True), ("Eva Brändl", "Eva", "Brandl", False)]:
            self.assertEqual(identity_matches(req, given, family), want, (req, given, family))

    def test_patient_as_recipient_needs_birth_date(self):
        self.assertTrue(identity_matches("Maria Beispiel", "Maria", "Beispiel", patient=PATIENT, birth_date="12.03.1961"))
        self.assertFalse(identity_matches("Maria Beispiel", "Maria", "Beispiel", patient=PATIENT,
                                          birth_date="1990-05-05"))      # the same-named daughter
        self.assertFalse(identity_matches("Maria Beispiel", "Maria", "Beispiel", patient=PATIENT))
        self.assertTrue(identity_matches("Dr. Eva Brandl", "Eva", "Brandl", patient=PATIENT))   # not the patient
        d = decide(parse_answer(LAB, "lab_result"), {"doc_kind": "lab_result", "recipient": {"name": "Maria Beispiel"}},
                   PATIENT, {"given_name": "Maria", "family_name": "Beispiel", "birthDate": "1990-05-05"})
        self.assertEqual(d["codes"][0], "EID_MISMATCH")


EVA_ID = {"given_name": "Eva", "family_name": "Brandl"}
KERN_ID = {"given_name": "Stefan", "family_name": "Kern"}
REQ_LAB_D = {"doc_kind": "lab_result", "requester_name": "Dr. Lena Hofer", "recipient": {"name": "Dr. Eva Brandl"}}
REQ_REHA_D = {"doc_kind": "discharge_report", "requester_name": "Dr. Lena Hofer", "recipient": {"name": "Stefan Kern"}}
LAB = doc("lab-result-maria-beispiel.md")


def fhirmap_card(p):
    from fhirmap import card_facts
    return card_facts(p)


def _decide(text, req=REQ_LAB_D, ident=EVA_ID, raw=None):
    p = parse_answer(text, req["doc_kind"])
    return p, decide(p, req, PATIENT, ident, text.encode("utf-8") if raw is None else raw)


class TestDecide(unittest.TestCase):
    def test_rehab_card_leads_with_discharge_medication(self):
        f = fhirmap_card(parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report"))
        self.assertTrue(f["medications"][0].startswith("Rivaroxaban 10 mg once a day until 29 September 2026"))
        self.assertEqual(len(f["medications"]), 3)
        self.assertFalse(any("Rivaroxaban" in r for r in f["recommendations"]))
        self.assertNotIn("medications", fhirmap_card(parse_answer(LAB, "lab_result")))

    def test_scenario(self):
        self.assertEqual(_decide(LAB)[1]["outcome"], "FILE")
        self.assertEqual(_decide(doc("rehab-report-maria-beispiel.md"), REQ_REHA_D, KERN_ID)[1]["outcome"], "FILE")
        p, d = _decide(doc("lab-result-max-beispiel.md"))
        self.assertEqual((d["outcome"], d["codes"][0]), ("BLOCK", "WRONG_PATIENT"))
        self.assertEqual(d["details"], [])
        for leak in ("Max", "1960", "January"):
            self.assertNotIn(leak, json.dumps(d))

    def test_eid_and_sender(self):
        self.assertEqual(_decide(LAB, ident={"given_name": "Thomas", "family_name": "Huber"})[1]["codes"][0],
                         "EID_MISMATCH")
        reha = doc("rehab-report-maria-beispiel.md")
        d = _decide(reha.replace("Sent by: Stefan Kern", "Sent by: Anna Gruber"), REQ_REHA_D, KERN_ID)[1]
        self.assertEqual((d["outcome"], d["codes"]), ("HOLD", ["SENDER_CONTRADICTS_EID"]))
        p, d = _decide(reha + "Sent by: Anna Gruber, discharge management\n", REQ_REHA_D, KERN_ID)
        self.assertEqual(d["outcome"], "HOLD")
        self.assertIn("SENDER_CONFLICT", d["codes"])
        self.assertTrue(any("Sent by" in h for h in p.hold_reasons))

    def test_patient_variants(self):
        line = "Patient: Maria Beispiel, born 12 March 1961"
        for new, outcome, code in [
                ("Patient: Beispiel Maria, born 12 March 1961", "FILE", "OK"),       # surname first: exact tokens + exact DOB
                ("Patient: Beispiel Maria, born 12 March 1962", "HOLD", "NAME_NEAR"),  # + DOB_NEAR: never SAME
                ("Patient: Beispiel Maria, geb. 12.03.61", "HOLD", "DOB_AMBIGUOUS"),  # two-digit year: says so
                ("Patient: Beispiel M., born 12 March 1961", "BLOCK", "WRONG_PATIENT"),   # an initial: unchanged
                ("Patient: Beispiel Maria Anna, born 12 March 1961", "HOLD", "NAME_NEAR"),
                ("Patient: Beispiel, Maria, born 12 March 1961", "FILE", "OK"),
                ("Patient: Märia Beispiel, born 12 March 1961", "HOLD", "NAME_NEAR"),
                ("Patient: María Beispiel, born 12 March 1961", "FILE", "OK"),
                ("Patient: Maria Beispiel, born 3 December 1961", "HOLD", "DOB_NEAR"),
                ("Patient: Maria Beispiel, born 12/03/1961", "HOLD", "DOB_AMBIGUOUS"),
                ("Patient: Maria Beispiel, born 31.02.1961", "HOLD", "DOB_UNPARSEABLE"),
                ("Patient: Maria Beispiel, 65 years", "HOLD", "PATIENT_INCOMPLETE"),
                ("Patient: Maria Beispiel\nDate of birth: 12 March 1961", "FILE", "OK"),
                ("Patient: Anna Huber, born 12 March 1961", "BLOCK", "WRONG_PATIENT"),
                ("Requested by: nobody", "HOLD", "PATIENT_MISSING")]:
            d = _decide(LAB.replace(line, new))[1]
            self.assertEqual((d["outcome"], d["codes"][0]), (outcome, code), new)

    def test_hidden_and_active_content(self):
        p, d = _decide(LAB.replace("Requested by:", "<!-- Patient: Max Beispiel, born 1 January 1960 -->\nRequested by:"))
        self.assertEqual((d["outcome"], d["codes"][0]), ("BLOCK", "OTHER_PATIENT_PRESENT"))
        self.assertTrue(p.hold_reasons)
        self.assertNotIn("Max", " ".join(p.warnings))
        p, d = _decide(LAB.replace("Requested by:", "<!-- Patient: Maria Beispiel, born 12 March 1961 -->\nRequested by:"))
        self.assertEqual(d["outcome"], "FILE")                            # same patient hidden: harmless
        for evil in ["<script>alert(1)</script>", '<img src=x onerror="alert(1)">', "[x](javascript:alert(1))",
                     "<iframe src=//x>"]:
            p, d = _decide(LAB.replace("All values", evil + " All values"))
            self.assertEqual((d["outcome"], d["codes"][0]), ("BLOCK", "ACTIVE_CONTENT"), evil)
            self.assertTrue(p.block_reasons)
            self.assertFalse(p.ok_to_file)
        for sus in ["<img src=x>", "<span class=x>a</span>", "<table>", "![logo](http://lab.example/logo.png)",
                    "[results](http://lab.example/r)", "<http://lab.example>", "[x](mailto:x@lab.example)",
                    "![x](data:image/png;base64,AAAA)", "&#106;avascript:alert(1)",
                    "%3Cscript%3Ealert(1)%3C/script%3E", "[x](//evil.example/x)"]:
            p, d = _decide(LAB.replace("All values", sus + " All values"))
            self.assertEqual((d["outcome"], d["codes"]), ("HOLD", ["ACTIVE_CONTENT_SUSPECT"]), sus)
        for ok in ["[lab info](https://lab.example/info)", "<https://lab.example>", "https://lab.example/x",
                   "contact <eva.brandl@labor-kanal.example>", "value < 5 and > 2", "ratio 1:2",
                   "<b>bold</b>", "<br>", "line<br/>break", "&lt;0,5 &amp; &nbsp;", "![logo](https://lab.example/l.png)",
                   "![logo](logo.png)", "[a](#top)", "portal http://befunde.lab.example"]:
            self.assertEqual(_decide(LAB.replace("All values", ok + " All values"))[1]["outcome"], "FILE", ok)
        self.assertEqual(_decide(LAB.replace("Maria", "Ma\x00ria"))[1]["codes"][0], "UNREADABLE")
        self.assertEqual(_decide("", raw=b"")[1]["codes"][0], "EMPTY")
        self.assertEqual(_decide(LAB, raw=b"\x89PNG\r\n\x1a\n\xff\xfe")[1]["outcome"], "BLOCK")

    def test_lab_rules(self):
        self.assertEqual(_decide(LAB.replace("6301-6", "6301-5"))[1]["codes"], ["LOINC_CHECKDIGIT"])
        self.assertEqual(_decide(LAB.replace("| 245 | 10^9/L |", "| 245 | g/L |"))[1]["codes"], ["LOINC_UNIT_DIMENSION"])
        self.assertEqual(_decide(LAB.replace("| 1.0 | |", "| 1.0 | % |"))[1]["codes"], ["LOINC_UNIT_DIMENSION"])
        self.assertEqual(_decide(LAB.replace("| 3.1 | g/L |", "| 3.1 | |"))[1]["codes"], ["UNIT_MISSING"])
        self.assertEqual(_decide(LAB.replace("| 245 | 10^9/L |", "| 245 | /GF |"))[1]["codes"], ["UNKNOWN_UNIT"])
        p_, d_ = _decide(LAB.replace("Dr. Lena Hofer", "Dr. Karl Anders"))      # another orderer: information only
        self.assertEqual(d_["outcome"], "FILE")
        self.assertEqual(fhirmap_card(p_)["einsender"], "Dr. Karl Anders, Klinikum Beispielstadt")
        self.assertEqual(_decide(LAB, REQ_REHA_D | {"recipient": {"name": "Dr. Eva Brandl"}})[1]["codes"],
                         ["KIND_MISMATCH"])
        table = LAB[LAB.index("| Test"):LAB.index("All values")]
        self.assertEqual(_decide(LAB.replace(table, ""))[1]["codes"], ["NO_RESULTS"])

    def test_kind_mismatch_names_the_kinds(self):
        """KIND_MISMATCH says which kind came instead of which was asked, never a sentence of the document."""
        from fhirmap import card_facts, reason_text_de, reasons_text_de
        rehab = doc("rehab-report-maria-beispiel.md")
        for text, req, ident, want in (
                (LAB, REQ_REHA_D | {"recipient": {"name": "Dr. Eva Brandl"}}, EVA_ID,
                 "Laborbefund statt Entlassungsbericht geschickt."),
                (rehab, REQ_LAB_D | {"recipient": {"name": "Stefan Kern"}}, KERN_ID,
                 "Entlassungsbericht statt Laborbefund geschickt.")):
            p, d = _decide(text, req, ident)
            self.assertEqual(d["codes"], ["KIND_MISMATCH"], d)
            self.assertEqual(reason_text_de("KIND_MISMATCH", d["details"]), want)
            self.assertEqual(reasons_text_de(d), want)
            f = card_facts(p, d)
            self.assertEqual((f["reasons_de"], f["reasons_text_de"]), ([want], want))
            # no quoted document text anywhere in the decision or the card (hold-card rows come from details)
            self.assertFalse(any(x.get("field") == "text" or "document" in x for x in d["details"]), d["details"])
            self.assertNotIn("„", json.dumps([d, f["reasons_de"]], ensure_ascii=False))
            self.assertNotIn("All values", json.dumps(d, ensure_ascii=False))
        # without the kinds (older details): the generic sentence
        self.assertEqual(reason_text_de("KIND_MISMATCH"), "Es wurde eine andere Dokumentart geschickt als angefragt.")
        # KIND_UNKNOWN still quotes the first sentence (it helps decide what the file is)
        p, d = _decide("Sehr geehrte Frau Kollegin, anbei die gewünschten Unterlagen zur Patientin.\n")
        self.assertIn("KIND_UNKNOWN", d["codes"])
        self.assertTrue(any(x.get("field") == "text" for x in d["details"]))

    def test_bold_and_german_labels(self):
        p, d = _decide(var("v26_bold_values"))
        self.assertEqual(d["outcome"], "FILE", d)
        self.assertEqual([(r["test"], r["value"], r["unit"], r["loinc"]) for r in p.lab],
                         [("INR (prothrombin time)", 1.0, None, "6301-6"), ("aPTT", 31, "s", "14979-9"),
                          ("Platelets", 245, "10^9/L", "777-3")])
        self.assertIn("**1.0**", p.text)                                   # original kept for prove
        p, d = _decide(var("v27_german_labels"))
        self.assertEqual(d["outcome"], "FILE", d)
        self.assertEqual((p.sample_time, p.report_time), ("2026-10-27T07:55:00+01:00", "2026-10-27T10:20:00+01:00"))
        self.assertEqual([r["ucum"] for r in p.lab], ["10*9/L", "10*12/L", "mg/L"])
        for label in ["Probennahme", "Abnahme", "Entnahmezeitpunkt", "Sample collected", "Collection time"]:
            q = parse_answer(LAB.replace("Sample taken", label), "lab_result")
            self.assertEqual(q.sample_time, "2026-10-22T08:10:00+02:00", label)
        for label in ["Befundet", "Befunddatum", "Befundet am", "Report date", "Issued"]:
            q = parse_answer(LAB.replace("Reported", label), "lab_result")
            self.assertEqual(q.report_time, "2026-10-22T11:40:00+02:00", label)

    def test_jury_corpus(self):
        """Every case of the jury corpus (tests/jury/corpus, data only) gets its labelled outcome."""
        corpus = JURY / "corpus"
        m = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
        bad = []
        for c in m["cases"]:
            raw = (corpus / c["file"]).read_bytes()
            import fhirmap as _F
            req = dict(c["request"], requester_name="Dr. Lena Hofer")
            d = decide(_F.parse_upload(raw, req["doc_kind"]), req, PATIENT,
                       {"given_name": c["uploader"]["given"], "family_name": c["uploader"]["family"],
                        "birthDate": c["uploader"].get("birthDate")}, raw)
            if d["outcome"] != c["expect"] or (c["expect"] != "FILE" and c["reasons"][0] not in d["codes"]):
                bad.append((c["id"], c["expect"], c["reasons"], d["outcome"], d["codes"]))
        self.assertEqual(bad, [])
        self.assertGreaterEqual(len(m["cases"]), 80)


REG_LAB = {"ok": True, "policy": "urn:pgk-bridge:policy:lab-result-v1", "register": "ÖÄK Ärzteliste (MOCK)",
           "entry": {"id": "OEAEK-MOCK-0001", "name": "Dr. Eva Brandl", "specialty": "Labordiagnostik",
                     "org": "Labor Kanalstraße"}}
REG_REHA = {"ok": True, "policy": "urn:pgk-bridge:policy:discharge-report-v1", "register": "GDA-Index (MOCK)",
            "entry": {"org": {"id": "GDA-MOCK-1002", "name": "Reha Waldblick"},
                      "staff": {"name": "Stefan Kern", "role": "Entlassungsmanagement"}}}
LEITNER_REG = {"id": "OEAEK-MOCK-0002", "name": "Dr. Johanna Leitner", "specialty": "Physikalische Medizin"}
OVERRIDE = {"by": "@lena.hofer:klinikum.example", "reason": "Name typo confirmed by phone with the lab",
            "at": "2026-10-29T19:20:00+00:00", "held_reasons": ["NAME_NEAR"]}


def _req_full(base, **kw):
    return {**base, "requester_name": "Dr. Lena Hofer", **kw}


class TestProvenanceInputs(unittest.TestCase):
    def test_policy_scheme(self):
        from fhirmap import POLICY_BASE, POLICY_LAB_RESULT, policy_uri
        self.assertEqual(POLICY_BASE, "https://pgk-bridge.example/fhir/policy")
        self.assertEqual(policy_uri("urn:pgk-bridge:policy:lab-result-v1"), POLICY_LAB_RESULT)
        self.assertEqual(policy_uri("lab-result-v1"), POLICY_LAB_RESULT)
        self.assertEqual(policy_uri(POLICY_LAB_RESULT), POLICY_LAB_RESULT)
        with self.assertRaises(ValueError):
            policy_uri("http://evil.example/x")

    def test_register_role_basis_and_override(self):
        import fhirmap as F
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        res = build_resources(p, "Patient/maria-beispiel",
                              _req_full(REQ_LAB, register=REG_LAB, role_basis="register",
                                        author_register=REG_LAB["entry"], policy=REG_LAB["policy"],
                                        override=OVERRIDE), EVA, RECEIVED, allow_hold=True)
        prov = res[-1]
        self.assertTrue(all(u.startswith(F.POLICY_BASE + "/") or u.startswith("http://eidas") for u in prov["policy"]))
        for u in (F.POLICY_LAB_RESULT, F.POLICY_REQUEST_BINDING, F.POLICY_ROLE_REGISTER, F.POLICY_AUTHOR_REGISTER,
                  F.POLICY_CLINICIAN_OVERRIDE):
            self.assertIn(u, prov["policy"])
        over = [a for a in prov["agent"] if a["who"].get("identifier", {}).get("system") == F.SID_MXID]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["type"]["coding"][0]["code"], "verifier")
        self.assertEqual(over[0]["who"]["display"], "Dr. Lena Hofer")
        self.assertIn("Name typo confirmed", over[0]["role"][0]["text"])   # the decision: on the clinician agent
        self.assertEqual(prov["reason"], [F.build.PURPOSE_TREAT])          # reason stays coded (no text-only reason)
        self.assertEqual(prov["recorded"], "2026-10-29T20:20:00+01:00")    # the filing: not before the decision
        for prac in (r for r in res if r["resourceType"] == "Practitioner"):
            self.assertNotIn(F.SID_AERZTELISTE, json.dumps(prac))      # a name match never becomes an identifier
        self.assertIn("Eintrag gleichen Namens gefunden (OEAEK-MOCK-0001", prov["agent"][0]["role"][0]["text"])
        self.assertIn("nicht an eine eID gebunden", prov["agent"][0]["role"][0]["text"])
        self.assertIn("nicht an die eID gebunden", prov["agent"][1]["role"][0]["text"])

    def test_asserted_role_and_org_ids(self):
        import fhirmap as F
        p = parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")
        res = build_resources(p, "Patient/maria-beispiel",
                              _req_full(REQ_REHA, register=REG_REHA, role_basis="asserted by requester",
                                        author_register=LEITNER_REG, policy=REG_REHA["policy"]), KERN, RECEIVED)
        prov = res[-1]
        self.assertIn(F.POLICY_ROLE_ASSERTED, prov["policy"])
        self.assertIn(F.POLICY_DISCHARGE_REPORT, prov["policy"])
        org = next(r for r in res if r["resourceType"] == "Organization")
        self.assertEqual(org["identifier"][0], {"system": F.SID_ORG, "value": "reha-waldblick"})
        self.assertIn({"system": F.SID_GDA, "value": "GDA-MOCK-1002"}, org["identifier"])
        leitner = next(r for r in res if r["resourceType"] == "Practitioner" and "Leitner" in r["name"][0]["text"])
        self.assertEqual(leitner["identifier"], [{"system": F.SID_SIGNATORY, "value": "johanna-leitner@reha-waldblick"}])
        b = build_transaction(res)
        conds = {e["resource"]["resourceType"]: e["request"].get("ifNoneExist") for e in b["entry"]
                 if e["request"].get("ifNoneExist")}
        self.assertTrue(conds["Organization"].startswith("identifier=https://pgk-bridge.example/fhir/org-id|"))
        self.assertIn("PractitionerRole", conds)
        self.assertEqual(F.slug("Labor Kanalstraße"), "labor-kanalstrasse")
        self.assertIn("org-id|reha-waldblick", conds["Organization"])    # slug: present on every filing
        leit = next(e for e in b["entry"] if e["resource"]["resourceType"] == "Practitioner"
                    and "Leitner" in e["resource"]["name"][0]["text"])
        self.assertEqual(leit["request"]["ifNoneExist"],                 # one signatory per stated name and org
                         "identifier=" + F.SID_SIGNATORY + "|johanna-leitner@reha-waldblick")

    def test_conditional_keys_and_resolver(self):
        import fhirmap as F
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        res = build_resources(p, "Patient/maria-beispiel",
                              _req_full(REQ_LAB, register=REG_LAB, role_basis="register",
                                        author_register=REG_LAB["entry"]), EVA, RECEIVED)
        b = build_transaction(res)
        prac = next(e for e in b["entry"] if e["resource"]["resourceType"] == "Practitioner"
                    and F.SID_BPK in e["request"].get("ifNoneExist", ""))
        bpk = prac["resource"]["identifier"][0]["value"]
        self.assertIn(F.SID_BPK + "|" + bpk, prac["request"]["ifNoneExist"])          # the sender's own eID hash only
        # server already holds TWO Practitioners with that eID hash (the HAPI-0958 situation), and one with the
        # Ärzteliste id that must never be searched
        server = {("Practitioner", F.SID_BPK + "|" + bpk): [{"id": "17"}, {"id": "9"}],
                  ("Practitioner", F.SID_AERZTELISTE + "|OEAEK-MOCK-0001"): [{"id": "1012"}],
                  ("Organization", F.SID_ORG + "|labor-kanalstrasse"): [{"id": "3"}]}
        calls = []

        def search(rt, params):
            calls.append((rt, params["identifier"]))
            if rt == "PractitionerRole":
                raise ConnectionError("search down")
            return server.get((rt, params["identifier"]), [])
        nb, rep_ = F.resolve_conditionals(b, search)
        self.assertFalse(any("aerzteliste" in q for _, q in calls))
        acts = {r["resourceType"]: r for r in rep_ if r["action"] == "reused" or r["resourceType"] != "Practitioner"}
        self.assertEqual((acts["Practitioner"]["action"], acts["Practitioner"]["reference"],
                          acts["Practitioner"]["matches"]), ("reused", "Practitioner/9", 2))   # oldest match
        self.assertEqual(acts["Organization"]["reference"], "Organization/3")
        nb_err, rep_err = F.resolve_conditionals(b, lambda rt, q: (_ for _ in ()).throw(ConnectionError("down")))
        self.assertTrue(all(r["action"] == "created" for r in rep_err))       # search error -> plain create
        types = [e["resource"]["resourceType"] for e in nb["entry"]]
        self.assertEqual(types.count("Practitioner"), 1)                    # the signatory, created (no match)
        self.assertNotIn("Organization", types)
        self.assertTrue(all("ifNoneExist" not in e["request"] for e in nb["entry"]))
        blob = json.dumps(nb)
        self.assertNotIn(prac["fullUrl"], blob)                             # every reference rewritten
        self.assertIn('"Practitioner/9"', blob)
        prov = next(e["resource"] for e in nb["entry"] if e["resource"]["resourceType"] == "Provenance")
        self.assertIn({"reference": "Practitioner/9"}, prov["target"])
        # a Practitioner filed before the system rename (legacy bPK system) is still found and reused
        res2 = build_resources(p, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
        b2 = build_transaction(res2)
        k = next(e for e in b2["entry"] if e["resource"]["resourceType"] == "Practitioner"
                 and e["resource"]["identifier"][0]["system"] == F.SID_BPK)["resource"]["identifier"][0]
        self.assertEqual(k["system"], F.SID_BPK)
        legacy = {("Practitioner", f"{F.SID_BPK_LEGACY}|{k['value']}"): [{"id": "4"}]}
        nb2, rep2 = F.resolve_conditionals(b2, lambda rt, q: legacy.get((rt, q["identifier"]), []))
        self.assertEqual(next(r for r in rep2 if r["resourceType"] == "Practitioner" and r["action"] == "reused")
                         ["reference"], "Practitioner/4")

    def test_namesakes_are_never_merged(self):
        """Two different eIDs with the same name and the same register entry: two Practitioners, each
        Provenance points to its own."""
        import fhirmap as F
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        req = _req_full(REQ_LAB, register=REG_LAB, role_basis="register", author_register=REG_LAB["entry"])
        real = build_resources(p, "Patient/maria-beispiel", {**req, "request_id": "r1"},
                               {**EVA, "bpk_hash": "bpk-hmac:4b1c"}, RECEIVED)
        twin = build_resources(p, "Patient/maria-beispiel", {**req, "request_id": "r2"},
                               {**EVA, "bpk_hash": "bpk-hmac:be99", "birthDate": "1955-01-01"}, RECEIVED)
        server = {}                       # a fake HAPI: conditional creates by identifier

        def search(rt, q):
            return server.get((rt, q["identifier"]), [])
        informants = []
        for n, res in enumerate((real, twin)):
            nb, _ = F.resolve_conditionals(build_transaction(res), search)
            for e in nb["entry"]:            # 'file' it: every created resource gets a server id
                r = e["resource"]
                if r["resourceType"] in ("Practitioner", "Organization", "PractitionerRole"):
                    sid = f"{r['resourceType']}/{len(server) + 1}{n}"
                    for i in r.get("identifier") or []:
                        server.setdefault((r["resourceType"], f"{i['system']}|{i['value']}"), []).append(
                            {"id": sid.split("/")[1]})
            prov = next(e["resource"] for e in nb["entry"] if e["resource"]["resourceType"] == "Provenance")
            inf = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "informant")
            sender = next(e["resource"] for e in nb["entry"] if e["fullUrl"] == inf["who"]["reference"])
            informants.append(sender["identifier"][0]["value"])
            self.assertNotIn(F.SID_AERZTELISTE, json.dumps(sender))
        self.assertEqual(informants, ["bpk-hmac:4b1c", "bpk-hmac:be99"])

    def test_signature_verification(self):
        import base64 as b64
        import hmac as hm
        import hashlib as hl
        from fhirmap import verify_provenance_signature
        enc = lambda b: b64.urlsafe_b64encode(b).rstrip(b"=").decode()
        claims = {"iss": "mock-id-austria", "given_name": "Eva", "family_name": "Brandl", "loa": "high"}
        # HS256 (the service's current statement)
        key = b"0123456789abcdef0123456789abcdef"
        si = enc(json.dumps({"alg": "HS256", "kid": "s1"}).encode()) + "." + enc(json.dumps(claims).encode())
        tok = si + "." + enc(hm.new(key, si.encode(), hl.sha256).digest())
        p = parse_answer(LAB, "lab_result")
        prov = build_resources(p, "Patient/maria-beispiel", REQ_LAB, {**EVA, "assertion": tok}, RECEIVED)[-1]
        jwks = {"keys": [{"kty": "oct", "kid": "s1", "k": enc(key)}]}
        self.assertTrue(verify_provenance_signature(prov, jwks, require_binding=False)["valid"])
        self.assertFalse(verify_provenance_signature(prov, {"keys": [{"kty": "oct", "kid": "s1", "k": enc(b"x" * 32)}]}, require_binding=False)["valid"])
        # ES256 (the target: IdP-signed token stored as-is)
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        except ImportError:
            self.skipTest("cryptography not installed")
        sk = ec.generate_private_key(ec.SECP256R1())
        nums = sk.public_key().public_numbers()
        jwk = {"kty": "EC", "crv": "P-256", "kid": "idp-1", "x": enc(nums.x.to_bytes(32, "big")),
               "y": enc(nums.y.to_bytes(32, "big"))}
        si = enc(json.dumps({"alg": "ES256", "kid": "idp-1"}).encode()) + "." + enc(json.dumps(claims).encode())
        r_, s_ = decode_dss_signature(sk.sign(si.encode(), ec.ECDSA(hashes.SHA256())))
        tok = si + "." + enc(r_.to_bytes(32, "big") + s_.to_bytes(32, "big"))
        prov = build_resources(p, "Patient/maria-beispiel", REQ_LAB, {**EVA, "assertion": tok}, RECEIVED)[-1]
        self.assertEqual(b64.b64decode(prov["signature"][0]["data"]).decode(), tok)       # stored as-is
        v = verify_provenance_signature(prov, {"keys": [jwk]}, require_binding=False)
        self.assertTrue(v["valid"], v)
        self.assertEqual((v["alg"], v["kid"], v["claims"]["family_name"]), ("ES256", "idp-1", "Brandl"))
        bad = copy.deepcopy(prov)
        forged = si.replace(enc(json.dumps(claims).encode()), enc(json.dumps({**claims, "family_name": "Huber"}).encode()))
        bad["signature"][0]["data"] = b64.b64encode((forged + "." + tok.split(".")[2]).encode()).decode()
        self.assertFalse(verify_provenance_signature(bad, {"keys": [jwk]}, require_binding=False)["valid"])
        other = verify_provenance_signature(
            build_resources(p, "Patient/maria-beispiel", REQ_LAB,
                            {**EVA, "given_name": "Eva", "family_name": "Brandl", "assertion": si.replace(
                                enc(json.dumps(claims).encode()), enc(json.dumps({**claims, "given_name": "Thomas",
                                                                                  "family_name": "Huber"}).encode()))
                             + ".AAAA"}, RECEIVED)[-1], {"keys": [jwk]}, require_binding=False)
        self.assertFalse(other["valid"])


SIM = {**EVA, "simulated": True, "method": "ID Austria (simuliert)"}


class TestSecurityAndPrivacy(unittest.TestCase):
    def test_simulated_eid_never_claims_loa(self):
        import fhirmap as F
        p = parse_answer(LAB, "lab_result")
        prov = build_resources(p, "Patient/maria-beispiel", REQ_LAB, SIM, RECEIVED)[-1]
        self.assertIn(F.POLICY_EID_SIMULATED, prov["policy"])
        self.assertNotIn("http://eidas.europa.eu/LoA/high", prov["policy"])
        inf = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "informant")
        self.assertIn("ID Austria (SIMULATION", inf["role"][0]["text"])
        self.assertNotIn("LoA high", json.dumps(prov))
        real = build_resources(p, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)[-1]
        self.assertIn("http://eidas.europa.eu/LoA/high", real["policy"])
        self.assertTrue(F.eid_simulated({"method": "mock-id-austria"}))

    def test_statement_bound_to_content_and_targets(self):
        import base64 as b64, hmac as hm, hashlib as hl
        import fhirmap as F
        enc = lambda b: b64.urlsafe_b64encode(b).rstrip(b"=").decode()
        key = b"k" * 32
        jwks = {"keys": [{"kty": "oct", "kid": "s", "k": enc(key)}]}

        def sign(claims):
            si = enc(json.dumps({"alg": "HS256", "kid": "s"}).encode()) + "." + enc(json.dumps(claims).encode())
            return si + "." + enc(hm.new(key, si.encode(), hl.sha256).digest())
        p = parse_answer(LAB, "lab_result")
        res = build_resources(p, "Patient/maria-beispiel", REQ_LAB, SIM, RECEIVED)
        binding = F.provenance_binding(res)
        self.assertEqual(binding["content_sha256"], hl.sha256(LAB.encode()).hexdigest())
        claims = {"given_name": "Eva", "family_name": "Brandl", **binding}
        F.attach_provenance_signature(res, sign(claims))
        v = F.verify_provenance_signature(res[-1], jwks)
        self.assertTrue(v["valid"] and v["bound"], v)
        # the same statement on another filing (other answer) does not verify
        q = parse_answer(LAB.replace("1.0", "1.1"), "lab_result")
        other = build_resources(q, "Patient/maria-beispiel", {**REQ_LAB, "request_id": "req-x"}, SIM, RECEIVED)
        F.attach_provenance_signature(other, sign(claims))
        v2 = F.verify_provenance_signature(other[-1], jwks)
        self.assertFalse(v2["valid"])
        self.assertTrue(any("SHA-256" in x for x in v2["problems"]))
        # targets must match exactly; after filing, an id_map translates urn:uuid to server ids
        stored = copy.deepcopy(res[-1])
        id_map = {t["reference"]: f"X/{i}" for i, t in enumerate(stored["target"])}
        for t in stored["target"]:
            t["reference"] = id_map[t["reference"]]
        self.assertFalse(F.verify_provenance_signature(stored, jwks)["valid"])
        self.assertTrue(F.verify_provenance_signature(stored, jwks, id_map=id_map)["valid"])
        stored["target"].pop()
        self.assertFalse(F.verify_provenance_signature(stored, jwks, id_map=id_map)["valid"])
        # a statement without the binding claims is rejected unless explicitly allowed
        F.attach_provenance_signature(res, sign({"given_name": "Eva", "family_name": "Brandl"}))
        self.assertFalse(F.verify_provenance_signature(res[-1], jwks)["valid"])
        self.assertTrue(F.verify_provenance_signature(res[-1], jwks, require_binding=False)["valid"])

    def test_signature_attribution_device_on_behalf_of_sender(self):
        """The service signs (who = verifier Device, ASTM Verification Signature) on behalf of the sender
        (onBehalfOf = her Practitioner); the signer check compares the token's person with onBehalfOf."""
        import base64 as b64, hmac as hm, hashlib as hl
        import fhirmap as F
        enc = lambda b: b64.urlsafe_b64encode(b).rstrip(b"=").decode()
        key = b"a" * 32
        jwks = {"keys": [{"kty": "oct", "kid": "s", "k": enc(key)}]}

        def sign(claims):
            si = enc(json.dumps({"alg": "HS256", "kid": "s"}).encode()) + "." + enc(json.dumps(claims).encode())
            return si + "." + enc(hm.new(key, si.encode(), hl.sha256).digest())
        p = parse_answer(LAB, "lab_result")
        res = build_resources(p, "Patient/maria-beispiel", REQ_LAB, SIM, RECEIVED)
        F.attach_provenance_signature(res, sign({"given_name": "Eva", "family_name": "Brandl",
                                                 **F.provenance_binding(res)}))
        prov = res[-1]
        sig = prov["signature"][0]
        inf = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "informant")
        dev = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "verifier")
        self.assertEqual(sig["who"], dev["who"])
        self.assertEqual(sig["who"]["type"], "Device")
        self.assertEqual(sig["onBehalfOf"], inf["who"])
        self.assertEqual(sig["onBehalfOf"]["display"], "Eva Brandl")
        self.assertEqual(sig["type"][0]["code"], "1.2.840.10065.1.12.1.5")
        self.assertEqual(sig["type"][0]["display"], "Verification Signature")
        self.assertTrue(F.verify_provenance_signature(prov, jwks, resources=res)["valid"])
        # the token's person must be the onBehalfOf person
        F.attach_provenance_signature(res, sign({"given_name": "Thomas", "family_name": "Huber",
                                                 **F.provenance_binding(res)}))
        v = F.verify_provenance_signature(res[-1], jwks, resources=res)
        self.assertFalse(v["valid"])
        self.assertIn("the token's person is not signature.onBehalfOf", v["problems"])
        # onBehalfOf without a display: the name of the referenced Practitioner (as built, and read back)
        F.attach_provenance_signature(res, sign({"given_name": "Eva", "family_name": "Brandl",
                                                 **F.provenance_binding(res)}))
        bare = copy.deepcopy(res[-1])
        bare["signature"][0]["onBehalfOf"].pop("display")
        self.assertTrue(F.verify_provenance_signature(bare, jwks, resources=res)["valid"])
        self.assertFalse(F.verify_provenance_signature(bare, jwks, resources=[])["valid"])   # nobody to match
        prac = next(r for r in res if r["resourceType"] == "Practitioner"
                    and f"urn:uuid:{r['id']}" == inf["who"]["reference"])
        id_map = {inf["who"]["reference"]: "Practitioner/9"}
        back = copy.deepcopy(bare)
        back["signature"][0]["onBehalfOf"]["reference"] = "Practitioner/9"
        reused = [{**r, "id": "9"} if r is prac else r for r in res]     # the Practitioner as Practitioner/9
        v = F.verify_provenance_signature(back, jwks, require_binding=False, resources=reused)
        self.assertTrue(v["valid"], v["problems"])
        v = F.verify_provenance_signature(bare, jwks, require_binding=False, id_map=id_map, resources=reused)
        self.assertTrue(v["valid"], v["problems"])

    def test_signature_old_shape_still_verifies(self):
        """Filings made before the Device attribution (who = the sender's Practitioner, Source Signature,
        onBehalfOf = her Organization) still verify, and the signer check still applies to them."""
        import base64 as b64, hmac as hm, hashlib as hl
        import fhirmap as F
        enc = lambda b: b64.urlsafe_b64encode(b).rstrip(b"=").decode()
        key = b"o" * 32
        jwks = {"keys": [{"kty": "oct", "kid": "s", "k": enc(key)}]}

        def sign(claims):
            si = enc(json.dumps({"alg": "HS256", "kid": "s"}).encode()) + "." + enc(json.dumps(claims).encode())
            return si + "." + enc(hm.new(key, si.encode(), hl.sha256).digest())
        p = parse_answer(LAB, "lab_result")
        res = build_resources(p, "Patient/maria-beispiel", REQ_LAB, SIM, RECEIVED)
        binding = F.provenance_binding(res)
        prov = res[-1]
        inf = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "informant")

        def old_shape(claims):
            return {"type": [{"system": "urn:iso-astm:E1762-95:2013", "code": "1.2.840.10065.1.12.1.14",
                              "display": "Source Signature"}], "when": prov["recorded"], "who": inf["who"],
                    "onBehalfOf": inf["onBehalfOf"], "sigFormat": "application/jose",
                    "data": b64.b64encode(sign(claims).encode()).decode()}
        prov["signature"] = [old_shape({"given_name": "Eva", "family_name": "Brandl", **binding})]
        v = F.verify_provenance_signature(prov, jwks, resources=res)
        self.assertTrue(v["valid"] and v["bound"], v["problems"])
        prov["signature"] = [old_shape({"given_name": "Thomas", "family_name": "Huber", **binding})]
        v = F.verify_provenance_signature(prov, jwks, resources=res)
        self.assertFalse(v["valid"])
        self.assertIn("the token's person is not signature.who", v["problems"])

    def test_statement_covers_filed_clinical_content(self):
        import base64 as b64, hmac as hm, hashlib as hl
        import fhirmap as F
        enc = lambda b: b64.urlsafe_b64encode(b).rstrip(b"=").decode()
        key = b"q" * 32
        jwks = {"keys": [{"kty": "oct", "kid": "s", "k": enc(key)}]}

        def sign(claims):
            si = enc(json.dumps({"alg": "HS256", "kid": "s"}).encode()) + "." + enc(json.dumps(claims).encode())
            return si + "." + enc(hm.new(key, si.encode(), hl.sha256).digest())
        p = parse_answer(LAB, "lab_result")
        res = build_resources(p, "Patient/maria-beispiel", {**REQ_LAB, "task_ref": "Task/77"}, SIM, RECEIVED)
        prov = res[-1]
        self.assertIn({"reference": "Task/77"}, prov["target"])           # the Task this filing completes
        self.assertTrue(prove(p, res)["all_equal"])
        binding = F.provenance_binding(res)
        self.assertEqual(len(binding["resource_digests"]), 5)             # DiagnosticReport + 4 Observations
        F.attach_provenance_signature(res, sign({"given_name": "Eva", "family_name": "Brandl", **binding}))
        # as built
        v = F.verify_provenance_signature(res[-1], jwks, resources=res)
        self.assertTrue(v["valid"], v["problems"])

        def read_back(resources, reuse_practitioner=False):
            """What HAPI returns: server ids, rewritten references, meta, no narrative."""
            id_map = {f"urn:uuid:{r['id']}": f"{r['resourceType']}/{100 + i}" for i, r in enumerate(resources)}
            if reuse_practitioner:                                       # resolve_conditionals reused Practitioner/9
                for r in resources:
                    if r["resourceType"] == "Practitioner":
                        id_map[f"urn:uuid:{r['id']}"] = "Practitioner/9"
            out = []
            for r in copy.deepcopy(resources):
                if reuse_practitioner and r["resourceType"] == "Practitioner":
                    continue
                r["id"] = id_map[f"urn:uuid:{r['id']}"].split("/")[1]
                r["meta"] = {"versionId": "1", "lastUpdated": "2026-10-29T20:00:00.000+01:00"}
                r.pop("text", None)
                blob = json.dumps(r)
                for u, ref in id_map.items():
                    blob = blob.replace(f'"{u}"', f'"{ref}"')
                out.append(json.loads(blob))
            return out, id_map
        back, id_map = read_back(res)
        provb = next(r for r in back if r["resourceType"] == "Provenance")
        v = F.verify_provenance_signature(provb, jwks, resources=back, id_map=id_map)
        self.assertTrue(v["valid"], v["problems"])
        back2, id_map2 = read_back(res, reuse_practitioner=True)        # reused Practitioner: still OK
        provb2 = next(r for r in back2 if r["resourceType"] == "Provenance")
        self.assertTrue(F.verify_provenance_signature(provb2, jwks, resources=back2, id_map=id_map2)["valid"])
        # an Observation value edited on the server after filing
        edited = copy.deepcopy(back)
        obs = next(r for r in edited if r["resourceType"] == "Observation" and r["code"]["text"] == "aPTT")
        obs["valueQuantity"]["value"] = 55
        v = F.verify_provenance_signature(provb, jwks, resources=edited, id_map=id_map)
        self.assertFalse(v["valid"])
        self.assertIn(f"Observation/{obs['id']}", v["changed"])
        # a flag, a status or the conclusion edited
        for mut in (lambda rs: next(r for r in rs if r["resourceType"] == "DiagnosticReport").__setitem__(
                        "conclusion", "edited"),
                    lambda rs: next(r for r in rs if r["resourceType"] == "Observation").__setitem__(
                        "status", "entered-in-error"),
                    lambda rs: next(r for r in rs if r["resourceType"] == "Observation").__setitem__(
                        "subject", {"reference": "Patient/max-beispiel"})):
            e2 = copy.deepcopy(back)
            mut(e2)
            self.assertFalse(F.verify_provenance_signature(provb, jwks, resources=e2, id_map=id_map)["valid"])
        # an Observation deleted from the filing
        e3 = [r for r in back if not (r["resourceType"] == "Observation" and r["code"]["text"] == "aPTT")]
        self.assertFalse(F.verify_provenance_signature(provb, jwks, resources=e3, id_map=id_map)["valid"])
        # discharge: the attachment is covered (hash + size)
        q = parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")
        rr = build_resources(q, "Patient/maria-beispiel", REQ_REHA, {**KERN, "simulated": True}, RECEIVED)
        F.attach_provenance_signature(rr, sign({"given_name": "Stefan", "family_name": "Kern",
                                                **F.provenance_binding(rr)}))
        self.assertTrue(F.verify_provenance_signature(rr[-1], jwks, resources=rr)["valid"])
        rr2 = copy.deepcopy(rr)
        rr2[0]["content"][0]["attachment"]["hash"] = "AAAA"
        self.assertFalse(F.verify_provenance_signature(rr[-1], jwks, resources=rr2)["valid"])

    def test_pseudonymous_audit_agents(self):
        ev = audit_event("identity-checked",
                         [{"name": "Dr. Lena Hofer"},
                          {"name": "Thomas Huber", "bpk_hash": "a" * 64, "address": "kollege@labor-kanal.example",
                           "pseudonymous": True, "requestor": False}],
                         "eID check FAILED: Thomas Huber (kollege@labor-kanal.example, +43 660 000 0101) is not "
                         "the named recipient; link valid until 2026-11-05",
                         RECEIVED, "failure", "req-lab-1", task_ref="Task/1")
        blob = json.dumps(ev, ensure_ascii=False)
        for leak in ("Thomas", "Huber", "kollege@", "+43 660"):
            self.assertNotIn(leak, blob)
        self.assertIn("Dr. Lena Hofer", blob)                        # the requester stays named
        self.assertIn("2026-11-05", ev["outcomeDesc"])                 # dates are not phone numbers
        stranger = ev["agent"][1]
        self.assertEqual(stranger["who"]["identifier"]["value"], "a" * 64)
        self.assertEqual(stranger["who"]["display"], "pseudonymous person (bPK hash)")
        ev2 = audit_event("misdelivery-caught", {"name": "Petra Fremd"}, "blocked Petra Fremd", RECEIVED,
                          "serious", "req-lab-1", pseudonymous_agent=True)
        self.assertNotIn("Petra", json.dumps(ev2))
        self.assertEqual(verify_audit_chain([ev, ev2][:1]), [])


REALISM = JURY / "realism"
REDTEAM = JURY / "redteam"


def _realism_cases():
    """(case, parsed, decision, kind) for the realism corpus (data only)."""
    import fhirmap as F
    out = []
    for c in json.loads((REALISM / "manifest.json").read_text(encoding="utf-8")):
        raw = (REALISM / "cases" / c["file"]).read_bytes()
        kind = {"lab": "lab_result", "reha": "discharge_report"}[c["request"]["doc_kind"]]
        rname = c["request"]["recipient"].split(",")[0].split(" (")[0]
        p = F.parse_upload(raw, kind)
        d = decide(p, {"doc_kind": kind, "recipient": {"name": rname}}, PATIENT,
                   {"given_name": c["identity"]["given"], "family_name": c["identity"]["family"],
                    "birthDate": c["identity"]["birthdate"][:10]}, raw)
        out.append((c, p, d, kind))
    return out


def _redteam_cases():
    import fhirmap as F
    m = json.loads((REDTEAM / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for c in m["cases"]:
        if not c.get("document") or c["id"] in ("I06", "I16"):
            continue
        raw = (REDTEAM / c["document"]).read_bytes()
        rq = c["request"]
        p = F.parse_upload(raw, rq["doc_kind"])
        d = decide(p, {"doc_kind": rq["doc_kind"], "recipient": rq["recipient"]}, PATIENT,
                   {"given_name": c["identity"]["given"], "family_name": c["identity"]["family"],
                    "birthDate": c["identity"].get("birthdate")}, raw)
        out.append((c, p, d, rq["doc_kind"]))
    return out


class TestRealismCorpus(unittest.TestCase):
    def test_realism_outcomes(self):
        bad = []
        for c, p, d, _ in _realism_cases():
            exp = c["expected"]
            if c["id"] == "C25":                          # held for CORRECTION_REVIEW, filed via build_correction
                self.assertEqual(p.report_status, "corrected")
                self.assertEqual(d["codes"], ["CORRECTION_REVIEW"])
            ok = d["outcome"] == exp or (exp == "DENY" and d["codes"][0] == "EID_MISMATCH")
            if not ok:
                bad.append((c["id"], exp, d["outcome"], d["codes"]))
        self.assertEqual(bad, [])

    def test_realistic_details(self):
        cases = {c["id"]: (p, d) for c, p, d, _ in _realism_cases()}
        p = cases["C04"][0]                               # unlabelled flag column
        self.assertEqual([(r["flag"], r["flag_text"]) for r in p.lab],
                         [("L", "-"), ("H", "+"), ("H", "+"), ("A", "*"), ("L", "-")])
        p = cases["C05"][0]
        kal = next(r for r in p.lab if r["test"] == "Kalium")
        self.assertEqual((kal["value"], kal["absent"], kal["notes"]), (None, "error", ["nicht bestimmbar (hämolytisch)"]))
        self.assertEqual(next(r for r in p.lab if r["test"] == "D-Dimer")["comparator"], "<")
        p = cases["C06"][0]
        self.assertEqual(p.report_status, "preliminary")
        self.assertEqual(p.lab[-1]["status"], "registered")
        self.assertIsNone(p.lab[-1]["value"])
        p = cases["C03"][0]                               # cumulative: current value, previous as note
        self.assertEqual(p.lab[0]["value"], 1.0)
        self.assertEqual(p.lab[0]["notes"], ["Vorwert 15.10.2026: 2,4"])
        self.assertTrue(p.comment.startswith("Kommentar: Vorwert"))   # comment row, not an Observation
        self.assertEqual(len(p.lab), 4)
        self.assertEqual(cases["C13"][0].encoding, "cp1252")
        self.assertEqual(cases["C18"][0].lab, [])          # tables in a discharge report stay text
        self.assertEqual(cases["C30"][1]["codes"][0], "OTHER_PATIENT_PRESENT")
        self.assertTrue(cases["C30"][1]["other_person_present"])
        self.assertEqual(cases["C34"][1]["codes"], ["SEX_MISMATCH"])
        self.assertEqual(cases["C14"][1]["outcome"], "FILE")
        self.assertEqual(cases["C11"][0].lab[-1]["comparator"], "<")   # '&lt;0,27'

    def test_two_digit_year_rules(self):
        for line, want in [("Patient: Maria Beispiel, geb. 12.03.61", "FILE"),
                           ("Patient: Maria Beispiel, geb. 12.03.62", "HOLD"),
                           ("Patient: Maria Beispiel, geb. 12.03.20", "HOLD")]:
            d = _decide(LAB.replace("Patient: Maria Beispiel, born 12 March 1961", line))[1]
            self.assertEqual(d["outcome"], want, (line, d["codes"]))

    def test_build_v3_resources(self):
        cases = {c["id"]: (p, d) for c, p, d, _ in _realism_cases()}
        res = build_for(cases["C06"][0], "lab_result")
        self.assertEqual(res[0]["status"], "preliminary")
        fib = next(r for r in res if r["resourceType"] == "Observation" and r["code"]["text"] == "Fibrinogen")
        self.assertEqual(fib["status"], "registered")
        self.assertFalse(any(k.startswith("value") for k in fib))
        res = build_for(cases["C25"][0], "lab_result")
        self.assertEqual(res[0]["status"], "corrected")
        res = build_for(cases["C04"][0], "lab_result")
        quick = res[1]
        self.assertEqual(quick["interpretation"][0], {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/"
                                                                            "v3-ObservationInterpretation",
                                                                  "code": "L", "display": "Low"}], "text": "-"})
        res = build_for(cases["C05"][0], "lab_result")
        kal = next(r for r in res if r["resourceType"] == "Observation" and r["code"]["text"] == "Kalium")
        self.assertEqual(kal["dataAbsentReason"]["coding"][0]["code"], "error")
        p8 = cases["C08"][0]
        res = build_for(p8, "lab_result")
        self.assertEqual(res[0]["basedOn"][0]["display"],
                         "Einsender laut Dokument: Dr. Karl Huber, Arzt für Allgemeinmedizin, Beispielstadt")
        self.assertTrue(prove(p8, res)["all_equal"])
        from fhirmap import card_facts
        f = card_facts(cases["C05"][0], cases["C05"][1])
        dd = next(o for o in f["observations"] if o["test"] == "D-Dimer")
        self.assertEqual((dd["value"], dd["comparator"]), ("< 0,27", "<"))
        self.assertEqual(card_facts(cases["C23"][0])["warnings"][-1]["code"], "RESULT_OLD")

    def test_reason_texts_cover_every_code(self):
        import re as _re
        from fhirmap import REASONS_DE, reason_text_de
        codes = set()
        for f in ("decide.py", "parse.py"):
            t = (HERE.parent / f).read_text(encoding="utf-8")
            codes |= set(_re.findall(r'code="([A-Z_]+)"', t)) | set(_re.findall(r'found(?:\.setdefault\(|\[)"([A-Z_]+)"', t))
        codes.discard("WARN")
        self.assertEqual(sorted(c for c in codes if c not in REASONS_DE), [])
        txt = reason_text_de("OTHER_PATIENT_PRESENT", [{"field": "other_person_present", "other_person_present": True}])
        self.assertIn("weitere Person", txt)
        self.assertNotIn("Max", reason_text_de("WRONG_PATIENT", [{"field": "name", "document": "Max Beispiel"}]))


class TestRedteamSafety(unittest.TestCase):
    def test_redteam_outcomes(self):
        bad = []
        for c, p, d, _ in _redteam_cases():
            acc = [{"DENY": "BLOCK"}.get(x, x).split("(")[0] for x in c.get("accept", [c["expect"]])]
            if d["outcome"] not in acc or (c["expect"] == "DENY" and "EID_MISMATCH" not in d["codes"]):
                bad.append((c["id"], c["expect"], d["outcome"], d["codes"]))
        self.assertEqual(bad, [])

    def test_specific_safety_rules(self):
        cases = {c["id"]: (p, d) for c, p, d, _ in _redteam_cases()}
        self.assertIn(cases["W06"][1]["codes"][0], ("SVNR_MISMATCH",))
        self.assertEqual(cases["W10"][1]["codes"][0], "OTHER_PATIENT_PRESENT")
        self.assertIn("MULTIPLE_REPORTS", cases["H27"][1]["codes"])
        self.assertIn("MULTIPLE_REPORTS", cases["H28"][1]["codes"])
        self.assertIn("TABLE_SPLIT", cases["H14"][1]["codes"])
        self.assertIn("FUTURE_DATE", cases["H25"][1]["codes"])
        self.assertIn("ORG_MISMATCH", cases["R02"][1]["codes"])
        self.assertIn("STALE_REPORT", cases["R02"][1]["codes"])
        self.assertIn("REPORT_INTERIM", cases["R03"][1]["codes"])
        self.assertEqual(cases["H09"][1]["outcome"], "FILE")
        self.assertEqual(cases["H10"][1]["outcome"], "FILE")
        self.assertEqual(cases["R05"][1]["outcome"], "FILE")
        self.assertEqual(len(cases["H13"][0].lab), 4)                   # group rows are no Observations
        for leak in ("Max", "1960"):
            self.assertNotIn(leak, json.dumps(cases["W10"][1]))

    def test_every_file_case_proves(self):
        for c, p, d, kind in _redteam_cases() + _realism_cases():
            if d["outcome"] != "FILE":
                continue
            res = build_resources(p, "Patient/maria-beispiel", {"request_id": "f-" + c["id"]}, EVA, RECEIVED,
                                  decision=d)
            pr = prove(p, res, patient=PATIENT if kind != "x" else None)
            self.assertTrue(pr["all_equal"], (c["id"], [f for f in pr["fields"] if not f["equal"]],
                                              [r for r in pr["rows"] if r["status"] != "equal"]))

    def test_every_hold_is_buildable_on_override(self):
        for c, p, d, kind in _redteam_cases() + [(c, p, d, k) for c, p, d, k in _realism_cases()]:
            if d["outcome"] != "HOLD":
                continue
            res = build_resources(p, "Patient/maria-beispiel", {"request_id": "ov-" + c["id"]}, EVA, RECEIVED,
                                  allow_hold=True)
            self.assertTrue(any(r["resourceType"] in ("DiagnosticReport", "DocumentReference") for r in res), c["id"])
            if res[0]["resourceType"] == "DocumentReference":
                self.assertTrue(prove(p, res)["all_equal"], c["id"])
        p = parse_answer("just some text without anything", "lab_result")
        res = build_resources(p, "Patient/maria-beispiel", {}, EVA, RECEIVED, allow_hold=True)
        self.assertEqual(res[0]["type"], {"text": "Dokument"})
        pr = prove(p, res)                                                # generic build: bytes + Provenance proven
        self.assertTrue(pr["all_equal"], pr["fields"])
        self.assertTrue(any(f["field"].startswith("original text") and f["equal"] for f in pr["fields"]))
        q = parse_answer(var("v04_missing_patient"), "lab_result")
        rr = build_resources(q, "Patient/maria-beispiel", {}, EVA, RECEIVED, allow_hold=True)
        self.assertTrue(prove(q, rr)["all_equal"])
        with self.assertRaises(ValueError):                               # two people: never, not even on override
            build_resources(parse_answer(var("v11_two_patients"), "lab_result"), "Patient/maria-beispiel", {}, EVA,
                            RECEIVED, allow_hold=True)
        with self.assertRaises(ValueError):
            build_resources(parse_answer(LAB, "lab_result"), "Patient/maria-beispiel", {}, EVA, RECEIVED,
                            decision={"outcome": "BLOCK", "codes": ["WRONG_PATIENT"]})
        with self.assertRaises(ValueError):                               # BLOCK is never buildable
            build_resources(parse_answer(LAB.replace("All values", "<script>x</script>"), "lab_result"),
                            "Patient/maria-beispiel", {}, EVA, RECEIVED, allow_hold=True)

    def test_prove_counts_source_rows(self):
        p = parse_answer(LAB, "lab_result")
        res = build_for(p, p.kind)
        t = [r for r in res if not (r["resourceType"] == "Observation" and r["code"].get("text") == "aPTT")]
        t[0]["result"] = [x for x in t[0]["result"] if x.get("display") != "aPTT"]
        p2 = copy.deepcopy(p)
        p2.lab = [r for r in p2.lab if r["test"] != "aPTT"]           # parser and FHIR agree, the source does not
        pr = prove(p2, t)
        self.assertFalse(pr["all_equal"])
        self.assertTrue(any(f["field"].startswith("every value row") and not f["equal"] for f in pr["fields"]))


def parse_upload_cp1252():
    import fhirmap as F
    return F.parse_upload(LAB.encode("cp1252"), "lab_result")


def _server_copy(resources, start=100):
    """What a FHIR server returns after filing: server ids, rewritten references, meta, no narrative."""
    id_map = {f"urn:uuid:{r['id']}": f"{r['resourceType']}/{start + i}" for i, r in enumerate(resources)}
    out = []
    for r in copy.deepcopy(resources):
        r["id"] = id_map[f"urn:uuid:{r['id']}"].split("/")[1]
        r["meta"] = {"versionId": "1", "lastUpdated": "2026-10-27T10:21:00.000+01:00"}
        r.pop("text", None)
        blob = json.dumps(r)
        for u, ref in id_map.items():
            blob = blob.replace(f'"{u}"', f'"{ref}"')
        out.append(json.loads(blob))
    return out, id_map


class TestCorrection(unittest.TestCase):
    def _prior_and_correction(self):
        import fhirmap as F
        prior_p = F.parse_upload((REALISM / "cases" / "C09-lab-de-caps-family-first.md").read_bytes(), "lab_result")
        filed, _ = _server_copy(build_resources(prior_p, "Patient/maria-beispiel", REQ_LAB, EVA,
                                                "2026-10-27T10:21:00+01:00"))
        prior_dr = next(r for r in filed if r["resourceType"] == "DiagnosticReport")
        prior_obs = [r for r in filed if r["resourceType"] == "Observation"]
        corr = F.parse_upload((REALISM / "cases" / "C25-lab-corrected-report.md").read_bytes(), "lab_result")
        return F, corr, prior_dr, prior_obs

    def test_corrected_report_is_held_with_replaced_time(self):
        F, corr, prior_dr, _ = self._prior_and_correction()
        d = decide(corr, REQ_LAB_D, PATIENT, EVA_ID)
        self.assertEqual((d["outcome"], d["codes"]), ("HOLD", ["CORRECTION_REVIEW"]))
        self.assertEqual(d["replaces"]["time"], "2026-10-27T10:20:00+01:00")
        self.assertTrue(F.matches_prior(corr, prior_dr))
        other = dict(prior_dr, issued="2026-10-26T09:00:00+01:00", effectiveDateTime="2026-10-26T08:00:00+01:00")
        self.assertFalse(F.matches_prior(corr, other))
        self.assertIn("ersetzt Befund vom 27.10.2026, 10:20", F.reasons_text_de(d))

    def test_correction_pairs_match_only_their_original(self):
        """tests/jury/realism/corrections: each correction is held and names the time of its own original only."""
        import fhirmap as F
        cdir = REALISM / "corrections"
        filed = {}
        for f in sorted(cdir.glob("*-orig-*.md")):
            p = F.parse_upload(f.read_bytes(), "lab_result")
            res, _ = _server_copy(build_resources(p, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED))
            filed[f.name.replace("-orig-", "-corr-")] = next(r for r in res if r["resourceType"] == "DiagnosticReport")
        self.assertEqual(len(filed), 4)
        for name in filed:
            corr = F.parse_upload((cdir / name).read_bytes(), "lab_result")
            d = decide(corr, REQ_LAB_D, PATIENT, EVA_ID)
            self.assertEqual((d["outcome"], d["codes"]), ("HOLD", ["CORRECTION_REVIEW"]), name)
            self.assertEqual([n for n, dr in filed.items() if F.matches_prior(corr, dr)], [name])

    def test_build_correction_versions(self):
        F, corr, prior_dr, prior_obs = self._prior_and_correction()
        out = F.build_correction(corr, prior_dr, prior_obs, "Patient/maria-beispiel", REQ_LAB, EVA,
                                 "2026-10-27T15:45:00+01:00")
        res = out["resources"]
        dr = res[0]
        self.assertEqual((dr["resourceType"], dr["id"], dr["status"]), ("DiagnosticReport", prior_dr["id"], "corrected"))
        inr_prior = next(o for o in prior_obs if o["code"]["text"] == "INR")
        inr = next(r for r in res if r["resourceType"] == "Observation")
        self.assertEqual((inr["id"], inr["status"], inr["valueQuantity"]["value"]), (inr_prior["id"], "corrected", 1.3))
        self.assertEqual(out["changed"], [f"Observation/{inr_prior['id']}"])
        self.assertEqual(sorted(o["code"]["text"] for o in out["unchanged"]), ["Fibrinogen", "aPTT"])
        self.assertEqual(len(out["not_in_correction"]), 1)              # Thrombozyten: not in the correction
        self.assertEqual(len(dr["result"]), 3)
        prov = res[-1]
        targets = {t["reference"] for t in prov["target"]}
        # versioned targets: the PUT carries If-Match on the prior version (1), so the new version is 2
        self.assertIn(f"DiagnosticReport/{prior_dr['id']}/_history/2", targets)
        self.assertIn(f"Observation/{inr_prior['id']}/_history/2", targets)
        self.assertEqual(out["expected_versions"], {f"DiagnosticReport/{prior_dr['id']}": "2",
                                                    f"Observation/{inr_prior['id']}": "2"})
        self.assertFalse(any(t.startswith(f"Observation/{out['unchanged'][0]['id']}") for t in targets))
        self.assertTrue(any(e["role"] == "revision" and e["what"]["reference"] ==
                            f"DiagnosticReport/{prior_dr['id']}/_history/1" for e in prov["entity"]))
        b = F.provenance_binding(res)
        self.assertIn(f"DiagnosticReport/{prior_dr['id']}", b["resource_digests"])
        bundle = build_transaction(res)
        puts = [e["request"] for e in bundle["entry"] if e["request"]["method"] == "PUT"]
        self.assertEqual(sorted(p_["url"] for p_ in puts),
                         sorted([f"DiagnosticReport/{prior_dr['id']}", f"Observation/{inr_prior['id']}"]))
        self.assertTrue(all(p_["ifMatch"] == 'W/"1"' for p_ in puts))
        self.assertTrue(all("versionId" not in (e["resource"].get("meta") or {}) for e in bundle["entry"]))
        # option (b) also available: re-version targets from the transaction response
        prov_b = F.version_provenance_targets(prov, {f"DiagnosticReport/{prior_dr['id']}":
                                                     f"DiagnosticReport/{prior_dr['id']}/_history/7"})
        self.assertIn({"reference": f"DiagnosticReport/{prior_dr['id']}/_history/7"}, prov_b["target"])
        # the signature binding and its verification accept versioned targets
        import base64 as b64, hmac as hm, hashlib as hl
        enc = lambda x: b64.urlsafe_b64encode(x).rstrip(b"=").decode()
        key = b"c" * 32
        claims = {"given_name": "Eva", "family_name": "Brandl", **F.provenance_binding(res)}
        si = enc(json.dumps({"alg": "HS256", "kid": "s"}).encode()) + "." + enc(json.dumps(claims).encode())
        F.attach_provenance_signature(res, si + "." + enc(hm.new(key, si.encode(), hl.sha256).digest()))
        v = F.verify_provenance_signature(res[-1], {"keys": [{"kty": "oct", "kid": "s", "k": enc(key)}]},
                                          resources=res)
        self.assertTrue(v["valid"], v["problems"])
        # read back from HAPI, which strips versions from stored references: still bound to exactly these targets
        jwks = {"keys": [{"kty": "oct", "kid": "s", "k": enc(key)}]}
        stored = json.loads(json.dumps(res[-1]))
        for t in stored["target"]:
            t["reference"] = F.unversioned(t["reference"])
        v = F.verify_provenance_signature(stored, jwks, require_binding=True, resources=res)
        self.assertTrue(v["valid"] and v["bound"], v["problems"])
        moved = json.loads(json.dumps(stored))                           # a target the statement never signed
        moved["target"][0]["reference"] = "DiagnosticReport/999999"
        self.assertFalse(F.verify_provenance_signature(moved, jwks, require_binding=True)["valid"])
        kept = json.loads(json.dumps(res[-1]))                           # a kept version must be the signed one
        kept["target"][0]["reference"] = F.unversioned(kept["target"][0]["reference"]) + "/_history/9"
        self.assertFalse(F.verify_provenance_signature(kept, jwks, require_binding=True)["valid"])
        pr = prove(corr, res, patient=PATIENT, unchanged=out["unchanged"])
        self.assertTrue(pr["all_equal"], [f for f in pr["fields"] if not f["equal"]] +
                        [r for r in pr["rows"] if r["status"] != "equal"])
        with self.assertRaises(ValueError):                              # another patient's prior report
            F.build_correction(corr, dict(prior_dr, subject={"reference": "Patient/other"}), prior_obs,
                               "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
        with self.assertRaises(ValueError):                              # not the report it says it replaces
            F.build_correction(corr, dict(prior_dr, issued="2026-10-20T09:00:00+02:00",
                                          effectiveDateTime="2026-10-20T08:00:00+02:00"), prior_obs,
                               "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
        with self.assertRaises(ValueError):                              # not a correction at all
            F.build_correction(parse_answer(LAB, "lab_result"), prior_dr, prior_obs, "Patient/maria-beispiel",
                               REQ_LAB, EVA, RECEIVED)
        TestCorrection.bundle = bundle


class TestRawBytesAndRedaction(unittest.TestCase):
    def test_source_hash_is_raw_upload_bytes(self):
        import hashlib as hl
        import fhirmap as F
        raw = LAB.replace("Kanalstraße", "Kanalstraße").encode("cp1252").replace(b"\n", b"\r\n")
        p = F.parse_upload(raw, "lab_result")
        self.assertEqual(p.encoding, "cp1252")
        res = build_resources(p, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
        self.assertEqual(F.provenance_binding(res)["content_sha256"], hl.sha256(raw).hexdigest())
        att = res[0]["presentedForm"][0]
        import base64 as b64
        self.assertEqual(b64.b64decode(att["data"]), raw)                 # byte-identical
        self.assertEqual(att["contentType"], "text/markdown; charset=windows-1252")
        self.assertTrue(prove(p, res)["all_equal"])
        bom = b"\xef\xbb\xbf" + LAB.encode("utf-8")
        q = F.parse_upload(bom, "lab_result")
        rr = build_resources(q, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
        self.assertEqual(F.provenance_binding(rr)["content_sha256"], hl.sha256(bom).hexdigest())
        self.assertTrue(prove(q, rr)["all_equal"])
        self.assertNotIn("raw", q.to_dict())
        self.assertEqual(q.to_dict()["raw_sha256"], hl.sha256(bom).hexdigest())

    def test_redaction_is_exact(self):
        ev = audit_event("identity-checked",
                         [{"name": "Dr. Lena Hofer"},
                          {"name": "Thomas Huber (ID Austria, bpk-sha256:ab12)", "bpk_hash": "b" * 64,
                           "pseudonymous": True, "requestor": False}],
                         "eID-Prüfung (ID Austria, LoA high): Thomas Huber passt nicht; Hubertus Klein informiert",
                         RECEIVED, "failure", "req-1")
        self.assertIn("ID Austria", ev["outcomeDesc"])
        self.assertIn("Hubertus", ev["outcomeDesc"])                     # word boundaries: 'Huber' != 'Hubertus'
        self.assertNotIn("Thomas Huber", json.dumps(ev))


class TestCheckAnswer(unittest.TestCase):
    MARIA_DECL = {"name": "Maria Beispiel", "birthDate": "12.03.1961"}

    def test_scenario_files_cleanly(self):
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        self.assertEqual(check_answer(p, {**REQ_LAB, "requester_name": "Dr. Lena Hofer"}, PATIENT, self.MARIA_DECL), [])
        p = parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")
        self.assertEqual(check_answer(p, REQ_REHA, PATIENT), [])           # signatory != sender is fine

    def test_m3_wrong_patient_even_if_form_says_maria(self):
        p = parse_answer(doc("lab-result-max-beispiel.md"), "lab_result")
        reasons = check_answer(p, REQ_LAB, PATIENT, self.MARIA_DECL)
        self.assertTrue(reasons)
        blob = " ".join(reasons + p.warnings)
        for leak in ("Max", "1960", "01.01", "1 January"):                 # never name the other patient
            self.assertNotIn(leak, blob)

    def test_other_holds(self):
        self.assertTrue(check_answer(parse_answer(var("v13_day_month_swap"), "lab_result"), REQ_LAB, PATIENT))
        self.assertTrue(check_answer(parse_answer(var("v21_wrong_kind"), "lab_result"), REQ_LAB, PATIENT))
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        self.assertTrue(check_answer(p, {**REQ_LAB, "requester_name": "Dr. Karl Anders"}, PATIENT))
        self.assertTrue(check_answer(p, REQ_LAB, PATIENT, {"name": "Maria Beispiel", "birthDate": "03.12.1961"}))


class TestTaskTransactionAudit(unittest.TestCase):
    def test_task_and_transaction(self):
        task = build_task(REQ_LAB, "Patient/maria-beispiel", REQ_LAB["recipient"])
        self.assertEqual((task["status"], task["intent"], task["for"]["reference"]),
                         ("requested", "order", "Patient/maria-beispiel"))
        self.assertEqual(task["input"][0]["valueCodeableConcept"]["coding"][0]["code"], "11502-2")
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        res = build_for(p, p.kind)
        tu = task_update(task, "completed", [res[0]], when=RECEIVED)
        b = build_transaction(res, tu)
        self.assertEqual(b["type"], "transaction")
        self.assertEqual(len(b["entry"]), len(res) + 1)
        self.assertTrue(all(e["fullUrl"].startswith("urn:uuid:") for e in b["entry"][:-1]))
        self.assertTrue(all("id" not in e["resource"] for e in b["entry"][:-1]))
        prac = next(e for e in b["entry"] if e["resource"]["resourceType"] == "Practitioner"
                    and e["resource"].get("identifier"))
        self.assertTrue(prac["request"]["ifNoneExist"].startswith("identifier=https://pgk-bridge.example/fhir/sid/"))
        self.assertEqual(b["entry"][-1]["request"], {"method": "PUT", "url": f"Task/{task['id']}"})
        self.assertEqual(b["entry"][-1]["resource"]["output"][0]["valueReference"]["reference"],
                         f"urn:uuid:{res[0]['id']}")
        refs = json.dumps(b)
        for r in res:                       # every urn:uuid reference points into the bundle
            self.assertIn(f"urn:uuid:{r['id']}", refs)

    def test_six_audit_steps_and_chain(self):
        steps = ["request-sent", "link-opened", "identity-checked", "answer-sent", "answer-filed",
                 "misdelivery-caught"]
        prev, events = None, []
        for i, s in enumerate(steps):
            e = audit_event(s, [{"name": "Dr. Lena Hofer", "identifier": {"system": "https://pgk-bridge.example/fhir/sid/matrix-id",
                                                                         "value": "@lena.hofer:klinikum.example"}},
                                {"name": "pgk-bridge", "requestor": False}],
                            f"step {s}", f"2026-10-29T10:0{i}:00+01:00", "serious" if s == "misdelivery-caught" else "success",
                            "req-lab-1", task_ref="Task/1", patient_ref="Patient/maria-beispiel", prev_hash=prev)
            prev = next(d["valueString"] for d in e["entity"][0]["detail"] if d["type"] == "hash")
            events.append(e)
            self.assertTrue(all("requestor" in a for a in e["agent"]))
            self.assertEqual(e["subtype"][0]["code"], s)
        self.assertEqual(events[-1]["outcome"], "8")
        self.assertEqual(verify_audit_chain(events), [])
        events[2]["outcomeDesc"] = "edited"
        self.assertTrue(verify_audit_chain(events))
        with self.assertRaises(ValueError):
            audit_event("bogus", "x", "y", RECEIVED, "success", "r")


# =========================================================================== HL7 validator (one JVM)

@unittest.skipIf(os.environ.get("FHIRMAP_SKIP_VALIDATOR") == "1", "validator skipped by env")
class TestValidator(unittest.TestCase):
    def test_zero_errors_on_everything(self):
        from fhirmap.validate import validate_report
        resources, names = [], []
        for label, p in _all_filable():
            res = build_for(p, p.kind)
            req = REQ_LAB if p.kind == "lab_result" else REQ_REHA
            task = build_task(req, "Patient/maria-beispiel", req["recipient"])
            tu = task_update(task, "completed", [res[0]], when=RECEIVED)
            for i, r in enumerate(res):
                resources.append(r)
                names.append(f"{label}-{i:02d}-{r['resourceType']}")
            resources += [task, build_transaction(res, tu)]
            names += [f"{label}-task", f"{label}-transaction"]
        prev = None
        for i, s in enumerate(["request-sent", "link-opened", "identity-checked", "answer-sent", "answer-filed",
                               "misdelivery-caught"]):
            e = audit_event(s, "Dr. Lena Hofer" if i == 0 else {"name": "Dr. Eva Brandl", "resource_type": "Practitioner",
                                                                 "identifier": {"system": "https://pgk-bridge.example/fhir/sid/bpk-hmac-sha256",
                                                                                "value": "0" * 64}},
                            {"text": f"step {s}", "references": ["DiagnosticReport/1"]}, RECEIVED,
                            "failure" if i == 2 else "success", "req-lab-1", task_ref="Task/1",
                            patient_ref="Patient/maria-beispiel", prev_hash=prev)
            prev = e["entity"][0]["detail"][-1]["valueString"]
            resources.append(e)
            names.append(f"audit-{s}")
        # Provenance with register / role basis / author register / clinician override inputs
        p = parse_answer(doc("lab-result-maria-beispiel.md"), "lab_result")
        res = build_resources(p, "Patient/maria-beispiel",
                              _req_full(REQ_LAB, register=REG_LAB, role_basis="register",
                                        author_register=REG_LAB["entry"], policy=REG_LAB["policy"], override=OVERRIDE),
                              EVA, RECEIVED, allow_hold=True)
        p2 = parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report")
        res2 = build_resources(p2, "Patient/maria-beispiel",
                               _req_full(REQ_REHA, register=REG_REHA, role_basis="asserted by requester",
                                         author_register=LEITNER_REG, policy=REG_REHA["policy"]), KERN, RECEIVED)
        for lbl, rr in (("inputs-lab", res), ("inputs-reha", res2)):
            resources += rr + [build_transaction(rr)]
            names += [f"{lbl}-{i:02d}-{r['resourceType']}" for i, r in enumerate(rr)] + [f"{lbl}-transaction"]
        import fhirmap as F
        sim = build_resources(parse_answer(LAB, "lab_result"), "Patient/maria-beispiel", REQ_LAB, SIM, RECEIVED)
        F.attach_provenance_signature(sim, "eyJhbGciOiJIUzI1NiJ9.eyJ4IjoxfQ.c2ln")
        resources += sim + [build_transaction(sim)]
        names += [f"sim-{i:02d}-{r['resourceType']}" for i, r in enumerate(sim)] + ["sim-transaction"]
        resources.append(audit_event("identity-checked", [{"name": "Dr. Lena Hofer"},
                                     {"name": "Thomas Huber", "bpk_hash": "a" * 64, "pseudonymous": True}],
                                     "eID check FAILED: Thomas Huber", RECEIVED, "failure", "req-lab-1",
                                     task_ref="Task/1"))
        names.append("audit-pseudonymous")
        resources.append(audit_event("misdelivery-caught", {"name": "Petra Fremd"}, "blocked", RECEIVED, "serious",
                                     "req-lab-1", pseudonymous_agent=True))
        names.append("audit-unidentified")
        for c, p, d, kind in _realism_cases() + _redteam_cases():
            if d["outcome"] == "BLOCK":
                continue
            rr = build_resources(p, "Patient/maria-beispiel", {"request_id": "h-" + c["id"],
                                 "recipient": {"name": "x", "org": "Labor Kanalstraße"}}, EVA, RECEIVED,
                                 allow_hold=d["outcome"] == "HOLD")
            resources += rr
            names += [f"h-{c['id']}-{i:02d}-{r['resourceType']}" for i, r in enumerate(rr)]
        F_, corr, prior_dr, prior_obs = TestCorrection()._prior_and_correction()
        out = F_.build_correction(corr, prior_dr, prior_obs, "Patient/maria-beispiel", REQ_LAB, EVA,
                                  "2026-10-27T15:45:00+01:00")
        resources += out["resources"] + [build_transaction(out["resources"])]
        names += [f"corr-{i:02d}-{r['resourceType']}" for i, r in enumerate(out["resources"])] + ["corr-bundle"]
        cp = parse_upload_cp1252()
        rr = build_resources(cp, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
        resources += rr
        names += [f"cp1252-{i:02d}-{r['resourceType']}" for i, r in enumerate(rr)]
        for lbl, rr in _expert_builds().items():        # Patient display, patient as sender, ...
            resources += rr + [build_transaction(rr)]
            names += [f"x-{lbl}-{i:02d}-{r['resourceType']}" for i, r in enumerate(rr)] + [f"x-{lbl}-transaction"]
        bpk = "bpk-hmac:" + "4b1c" * 8
        for i, (st, who) in enumerate((
                ("request-sent", {"name": "@lena.hofer:klinikum.example", "reference": "Practitioner/lena-hofer"}),
                ("answer-filed", f"pgk-bridge (Absender Eva Brandl (ID Austria – Simulation, {bpk}))"),
                ("identity-checked", {"name": "Eva Brandl", "identifier": {"system": SID_BPK_T, "value": bpk}}))):
            resources.append(audit_event(st, who, {"text": st, "references": ["DiagnosticReport/2", "Task/1"]},
                                         RECEIVED, "success", "req-1", task_ref="Task/1",
                                         patient_ref="Patient/maria-beispiel"))
            names.append(f"x-audit-{i}-{st}")
        resources += codesystems()
        names.append("codesystem-audit-step")
        mode = os.environ.get("FHIRMAP_VALIDATE_MODE", "auto")
        rep = validate_report(resources, mode, names=names)
        print(f"\nvalidator mode={rep['mode']} files={len(rep['files'])} errors={len(rep['errors'])} "
              f"warnings={len(rep['warnings'])} seconds={rep['seconds']}\n{rep['command']}")
        self.assertEqual(len(rep["files"]), len(resources))
        self.assertEqual(rep["errors"], [])


SID_BPK_T = "https://pgk-bridge.example/fhir/sid/bpk-hmac-sha256"
MARIA_ID = {"given_name": "Maria", "family_name": "Beispiel", "bpk_hash": "bpk-hmac:" + "7" * 32, "simulated": True}
REQ_MARIA = {"request_id": "req-self-1", "requester_mxid": "@lena.hofer:klinikum.example", "doc_kind": "lab_result",
             "recipient": {"name": "Maria Beispiel", "role": "Patientin", "org": None}, "sender_is_patient": True,
             "requester_refs": {"practitioner": "Practitioner/lena-hofer",
                                "organization": "Organization/klinikum-beispielstadt"},
             "task_ref": "Task/self-1"}


def _expert_builds():
    """The reference builds: lab, rehab, override, patient as sender (all with the Patient)."""
    lab = build_resources(parse_answer(LAB, "lab_result"), "Patient/maria-beispiel", {**REQ_LAB, "patient": PATIENT},
                          SIM, RECEIVED)
    reha = build_resources(parse_answer(doc("rehab-report-maria-beispiel.md"), "discharge_report"),
                           "Patient/maria-beispiel", {**REQ_REHA, "patient": PATIENT}, KERN, RECEIVED)
    over = build_resources(parse_answer(LAB.replace("Maria", "Marie"), "lab_result"), "Patient/maria-beispiel",
                           _req_full(REQ_LAB, override=OVERRIDE, requester_refs=REQ_MARIA["requester_refs"],
                                     patient=PATIENT), EVA, RECEIVED, allow_hold=True)
    self_ = build_resources(parse_answer(LAB, "lab_result"), "Patient/maria-beispiel", REQ_MARIA, MARIA_ID, RECEIVED,
                            patient=PATIENT)
    return {"lab": lab, "reha": reha, "override": over, "self": self_}


class TestFhirExpertReview(unittest.TestCase):
    def test_signatory_is_one_practitioner_per_name_and_org(self):
        import fhirmap as F
        server: dict[str, list] = {}                     # 'Practitioner|system|value' -> [ids]
        made = []

        def search(rt, q):
            return [{"id": i} for i in server.get(f"{rt}|{q['identifier']}", [])]

        def file(req, received):
            res = build_resources(parse_answer(LAB, "lab_result"), "Patient/maria-beispiel", req, SIM, received)
            nb, rep = F.resolve_conditionals(build_transaction(res), search)
            for e in nb["entry"]:                        # what the server now holds
                r = e["resource"]
                if r["resourceType"] == "Practitioner":
                    made.append(r)
                    for i in r.get("identifier") or []:
                        server.setdefault(f"Practitioner|{i['system']}|{i['value']}", []).append(str(len(made)))
            return res, rep
        res1, rep1 = file(REQ_LAB, RECEIVED)
        res2, rep2 = file({**REQ_LAB, "request_id": "req-lab-2"}, "2026-10-30T09:00:00+01:00")
        sig = [r for r in made if any(i["system"] == F.SID_SIGNATORY for i in r.get("identifier") or [])]
        self.assertEqual(len(sig), 1)                    # two filings -> ONE 'Dr. Eva Brandl' signatory
        self.assertEqual(sig[0]["identifier"], [{"system": F.SID_SIGNATORY, "value": "eva-brandl@labor-kanalstrasse"}])
        reused = [r for r in rep2 if r["resourceType"] == "Practitioner" and r["action"] == "reused"]
        self.assertEqual(len(reused), 2)                 # the signatory AND the sender (each by its own key)
        snd = [r for r in made if any(i["system"] == F.SID_BPK for i in r.get("identifier") or [])]
        self.assertEqual(len(snd), 1)                    # the eID sender: separate, keyed by her bPK hash only
        self.assertNotEqual(snd[0]["identifier"], sig[0]["identifier"])
        self.assertFalse(any(i["system"] == F.SID_SIGNATORY for i in snd[0]["identifier"]))
        self.assertNotIn(F.SID_AERZTELISTE, json.dumps(made))
        prov = res1[-1]                                  # author and informant stay two different agents
        a = {x["type"]["coding"][0]["code"]: x["who"]["reference"] for x in prov["agent"] if "reference" in x["who"]}
        self.assertNotEqual(a["author"], a["informant"])
        self.assertEqual(F.signatory_key("Dr. Johanna Leitner", "Reha Waldblick"), "johanna-leitner@reha-waldblick")
        self.assertEqual(F.signatory_key("Dr. Eva Brandl", None), "eva-brandl@-")

    def test_subject_display_is_the_patients_name(self):
        for spelling in ("Marie Beispiel", "MARIA BEISPIEL"):
            p = parse_answer(LAB.replace("Maria Beispiel", spelling), "lab_result")
            res = build_resources(p, "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED, allow_hold=True, patient=PATIENT)
            subj = [r["subject"] for r in res if "subject" in r]
            self.assertTrue(subj and all(x == {"reference": "Patient/maria-beispiel", "display": "Maria Beispiel"}
                                         for x in subj), subj)
            self.assertNotIn(spelling, json.dumps([r.get("subject") for r in res]))
        res = build_resources(parse_answer(LAB, "lab_result"), "Patient/maria-beispiel", REQ_LAB, EVA, RECEIVED)
        self.assertTrue(all("display" not in r["subject"] for r in res if "subject" in r))   # no Patient: no display
        self.assertTrue(prove(parse_answer(LAB, "lab_result"), _expert_builds()["lab"], patient=PATIENT)["all_equal"])

    def test_audit_agents_are_identified_and_typed(self):
        import fhirmap as F
        ev = audit_event("request-sent", {"name": "@lena.hofer:klinikum.example", "reference": "Practitioner/lena-hofer"},
                         "sent", RECEIVED, "success", "req-1", task_ref="Task/1")
        a = ev["agent"][0]
        self.assertEqual(a["who"]["identifier"], {"system": F.SID_MXID, "value": "@lena.hofer:klinikum.example"})
        self.assertEqual(a["who"]["reference"], "Practitioner/lena-hofer")
        self.assertEqual(a["type"]["coding"][0]["code"], "AUT")
        bpk = "bpk-hmac:" + "4b1c" * 8
        ev = audit_event("answer-filed", f"pgk-bridge (Absender Eva Brandl (ID Austria – Simulation, {bpk}))",
                         {"text": "filed", "references": ["DiagnosticReport/2", "Task/1", "Task/1/_history/3",
                                                          "Observation/3"]},
                         RECEIVED, "success", "req-1", task_ref="Task/1", patient_ref="Patient/maria-beispiel")
        dev, snd = ev["agent"]
        self.assertEqual(dev["who"]["identifier"], {"system": F.SID_SERVICE, "value": "pgk-bridge"})
        self.assertEqual((dev["who"]["type"], dev["type"]["coding"][0]["code"], dev["requestor"]),
                         ("Device", "110150", False))
        self.assertEqual(snd["who"]["identifier"], {"system": F.SID_BPK, "value": bpk})
        self.assertEqual(snd["type"]["coding"][0]["code"], "INF")
        tasks = [e for e in ev["entity"] if "Task/1" in json.dumps(e["what"])]
        self.assertEqual(len(tasks), 1)                  # the Task once (entity[0]), not again from the references
        self.assertEqual([e["what"].get("reference") for e in ev["entity"][2:]], ["DiagnosticReport/2", "Observation/3"])
        ev = audit_event("identity-checked", {"name": "Eva Brandl", "identifier": {"system": F.SID_BPK, "value": bpk}},
                         "ok", RECEIVED, "success", "req-1")
        self.assertEqual(ev["agent"][0]["type"]["coding"][0]["code"], "INF")
        ev = audit_event("answer-filed", "pgk-bridge", "x", RECEIVED, "success", "req-1")
        self.assertEqual(ev["agent"][0]["who"]["identifier"]["system"], F.SID_SERVICE)

    def test_override_provenance_and_correction_activity(self):
        from datetime import datetime
        prov = _expert_builds()["override"][-1]
        self.assertEqual(len(prov["reason"]), 1)         # coded TREAT only
        self.assertIn("coding", prov["reason"][0])
        clin = next(a for a in prov["agent"] if a["who"].get("reference") == "Practitioner/lena-hofer")
        self.assertIn("stated reason: Name typo confirmed", clin["role"][0]["text"])
        self.assertGreaterEqual(datetime.fromisoformat(prov["recorded"]), datetime.fromisoformat(OVERRIDE["at"]))
        self.assertEqual(prov["occurredDateTime"], prov["recorded"])
        self.assertEqual(prov["activity"]["coding"][0]["code"], "CREATE")
        late = build_resources(parse_answer(LAB, "lab_result"), "Patient/maria-beispiel",
                               {**REQ_LAB, "filed_at": "2026-10-29T11:00:00+01:00"}, EVA, RECEIVED)[-1]
        self.assertEqual(late["recorded"], "2026-10-29T11:00:00+01:00")
        # a correction revises existing resources: UPDATE
        F_, corr, prior_dr, prior_obs = TestCorrection()._prior_and_correction()
        out = F_.build_correction(corr, prior_dr, prior_obs, "Patient/maria-beispiel", REQ_LAB, EVA,
                                  "2026-10-27T15:45:00+01:00")
        self.assertEqual(out["resources"][-1]["activity"]["coding"][0],
                         {"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation", "code": "UPDATE",
                          "display": "revise"})

    def test_rehab_document_reference_metadata(self):
        import fhirmap as F
        d = _expert_builds()["reha"][0]
        raw = doc("rehab-report-maria-beispiel.md").encode("utf-8")
        self.assertEqual(d["masterIdentifier"], {"system": "urn:ietf:rfc:3986", "value": F.document_unique_id(raw)})
        self.assertTrue(d["masterIdentifier"]["value"].startswith("urn:uuid:"))
        c = d["content"][0]
        self.assertEqual(c["format"]["code"], "urn:ihe:iti:xds:2017:mimeTypeSufficient")
        self.assertEqual(c["attachment"]["language"], "en")
        cache = json.loads((HERE.parent / "data" / "loinc.json").read_text(encoding="utf-8"))
        self.assertEqual(d["type"]["coding"][0]["display"], cache["11490-0"]["display"])     # tx.fhir.org's display
        self.assertEqual(d["category"][0]["coding"][0]["display"], cache["18842-5"]["display"])
        de = build_resources(parse_answer(var("v12_german_discharge"), "discharge_report"), "Patient/maria-beispiel",
                             REQ_REHA, KERN, RECEIVED)[0]
        self.assertEqual(de["content"][0]["attachment"]["language"], "de")
        self.assertIsNone(F.document_language("12 34"))

    def test_patient_as_sender(self):
        import base64 as b64, hmac as hm, hashlib as hl
        import fhirmap as F
        res = _expert_builds()["self"]
        prov = res[-1]
        inf = next(a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "informant")
        self.assertEqual(inf["who"], {"reference": "Patient/maria-beispiel", "display": "Maria Beispiel"})
        self.assertNotIn("onBehalfOf", inf)
        self.assertFalse(any(r["resourceType"] == "Practitioner" and "Beispiel" in r["name"][0]["text"] for r in res))
        self.assertNotIn(MARIA_ID["bpk_hash"], json.dumps(res))      # no sender Practitioner, no bPK in FHIR
        self.assertNotIn(F.POLICY_ROLE_ASSERTED, prov["policy"])
        self.assertEqual(F.informant_reference(res), inf["who"])     # the service's flag: Task.owner
        enc = lambda b: b64.urlsafe_b64encode(b).rstrip(b"=").decode()
        key = b"p" * 32
        claims = {"given_name": "Maria", "family_name": "Beispiel", **F.provenance_binding(res)}
        si = enc(json.dumps({"alg": "HS256", "kid": "s"}).encode()) + "." + enc(json.dumps(claims).encode())
        F.attach_provenance_signature(res, si + "." + enc(hm.new(key, si.encode(), hl.sha256).digest()))
        self.assertEqual(res[-1]["signature"][0]["onBehalfOf"], inf["who"])
        self.assertTrue(F.verify_provenance_signature(res[-1], {"keys": [{"kty": "oct", "kid": "s", "k": enc(key)}]},
                                                      resources=res)["valid"])
        # without the service's flag: computed from the request, the Patient and the eID birth date (identity rule 5)
        req = {k: v for k, v in REQ_MARIA.items() if k != "sender_is_patient"}
        self.assertTrue(F.sender_is_patient({**MARIA_ID, "birth_date": "1961-03-12"}, PATIENT, req))
        self.assertFalse(F.sender_is_patient({**MARIA_ID, "birth_date": "1961-12-03"}, PATIENT, req))   # a relative
        self.assertFalse(F.sender_is_patient(EVA, PATIENT, REQ_LAB))
        normal = _expert_builds()["lab"]
        self.assertEqual(F.informant_reference(normal)["reference"].split(":")[0], "urn")   # the sender Practitioner

    def test_task_requester_reference(self):
        t = build_task({**REQ_MARIA, "requester_name": "Dr. Lena Hofer"}, "Patient/maria-beispiel",
                       {"reference": "Patient/maria-beispiel"})
        self.assertEqual(t["requester"]["reference"], "Practitioner/lena-hofer")
        self.assertEqual(t["requester"]["identifier"]["value"], "@lena.hofer:klinikum.example")
        self.assertEqual(t["owner"], {"reference": "Patient/maria-beispiel"})


if __name__ == "__main__":
    unittest.main()

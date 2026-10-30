"""Building blocks for the jury corpus: people, request contexts, lab panels, document renderers.

A case = request context + uploader eID + declared (form) patient + document + expected outcome.
Expected outcomes are FILE / HOLD / BLOCK (tests/jury/RULES.md). For FILE cases the expected FHIR
content (LOINC, value, UCUM code, reference range, effective time with Europe/Vienna offset) is
derived here from the same row data the document is rendered from, so it cannot drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo("Europe/Vienna")
FILE, HOLD, BLOCK = "FILE", "HOLD", "BLOCK"
RANK = {FILE: 0, HOLD: 1, BLOCK: 2}

PATIENT = {"id": "maria-beispiel", "name": "Maria Beispiel", "given": "Maria", "family": "Beispiel",
           "birthDate": "1961-03-12", "mrn": "KB-000123"}
REQUESTER = {"mxid": "@lena.hofer:klinikum.example", "name": "Dr. Lena Hofer", "org": "Klinikum Beispielstadt"}


@dataclass(frozen=True)
class Person:
    given: str
    family: str
    birthDate: str

    @property
    def name(self) -> str:
        return f"{self.given} {self.family}"


EID = {
    "brandl": Person("Eva", "Brandl", "1979-05-14"),
    "kern": Person("Stefan", "Kern", "1985-09-02"),
    "kollege": Person("Thomas", "Huber", "1982-11-30"),   # M2 colleague at Labor Kanalstraße
    "fremd": Person("Petra", "Fremd", "1990-01-01"),      # M1 owner of labor-kanaI.example
    "weber": Person("Anna", "Weber", "1975-06-21"),
    "steiner": Person("Paul", "Steiner", "1969-02-08"),
    "moser": Person("Julia", "Moser", "1988-12-01"),
    "wagner": Person("Thomas", "Wagner", "1972-04-17"),
    "brandl_middle": Person("Eva Maria", "Brandl", "1979-05-14"),  # legal name has a middle name
    "eve_brandl": Person("Eve", "Brandl", "1979-05-14"),           # one-letter different person
    "gruber": Person("Anna", "Gruber", "1991-03-03"),              # Kern's colleague at Reha Waldblick
    "brandl_hyphen": Person("Eva-Maria", "Brandl", "1979-05-14"),  # hyphenated given name
    "brandl_huber": Person("Eva", "Brandl-Huber", "1979-05-14"),   # family name is a superset: other person
    "patient": Person("Maria", "Beispiel", "1961-03-12"),          # the patient herself as the sender
    "daughter": Person("Maria", "Beispiel", "1990-06-15"),         # same-named daughter, other birth date
    "brandl_namesake": Person("Eva", "Brandl", "1955-07-02"),      # another person with Dr. Brandl's name
}


@dataclass(frozen=True)
class Ctx:
    """A request as Dr. Hofer sends it (POST /api/requests body minus room/requester)."""
    key: str
    name: str
    role: str
    org: str
    channel: str
    address: str
    doc_kind: str
    eid: str  # the EID key of the genuine recipient

    def body(self) -> dict:
        return {"recipient": {"name": self.name, "role": self.role, "org": self.org}, "channel": self.channel,
                "address": self.address, "patient_id": PATIENT["id"], "doc_kind": self.doc_kind}


CTX = {c.key: c for c in [
    Ctx("brandl", "Dr. Eva Brandl", "Laborärztin", "Labor Kanalstraße", "email", "eva.brandl@labor-kanal.example",
        "lab_result", "brandl"),
    Ctx("kern", "Stefan Kern", "Entlassungsmanagement", "Reha Waldblick", "sms", "+43 660 000 0101",
        "discharge_report", "kern"),
    Ctx("weber", "Dr. Anna Weber", "Fachärztin für Labormedizin", "Labor Mitte", "email",
        "anna.weber@labor-mitte.example", "lab_result", "weber"),
    Ctx("steiner", "Dr. Paul Steiner", "Laborarzt", "Zentrallabor Nord", "sms", "+43 664 000 0202",
        "lab_result", "steiner"),
    Ctx("moser", "Julia Moser", "Entlassungsmanagement", "Reha Seeblick", "email",
        "julia.moser@reha-seeblick.example", "discharge_report", "moser"),
    # same person, other organisation key: identity rule v2 is tested without the per-recipient eID memory
    Ctx("brandl_sued", "Dr. Eva Brandl", "Laborärztin", "Labor Kanalstraße Zweigstelle Süd", "email",
        "eva.brandl@labor-kanal.example", "lab_result", "brandl"),
    Ctx("brandl_west", "Dr. Eva Brandl", "Laborärztin", "Labor Kanalstraße Zweigstelle West", "email",
        "eva.brandl@labor-kanal.example", "lab_result", "brandl"),
    Ctx("brandl_rev", "Brandl Eva", "Laborärztin", "Labor Kanalstraße", "email", "eva.brandl@labor-kanal.example",
        "lab_result", "brandl"),                                     # typed family-first, no comma
    Ctx("patient", "Maria Beispiel", "Patientin", "", "email", "mb-4471@mail.example", "lab_result", "patient"),
    Ctx("wagner", "Dr. Thomas Wagner", "Stationsarzt", "Klinikum Nordstadt", "email",
        "thomas.wagner@klinikum-nordstadt.example", "discharge_report", "wagner"),
]}


# ------------------------------------------------------------------ lab rows
@dataclass(frozen=True)
class Row:
    test: str
    value: str          # as printed (English decimal point); German layouts convert
    unit: str
    ref: str            # "a to b" | "< x" | "> x" | "" | free text
    loinc: str
    test_de: str = ""


UCUM_EXPECT = {  # printed unit -> acceptable UCUM codes (code, not display, is checked)
    "s": {"s"}, "10^9/L": {"10*9/L"}, "G/L": {"10*9/L"}, "/nL": {"10*9/L", "/nL"}, "g/L": {"g/L"},
    "g/dL": {"g/dL"}, "10^12/L": {"10*12/L"}, "T/L": {"10*12/L"}, "%": {"%"}, "fL": {"fL"}, "pg": {"pg"},
    "mmol/L": {"mmol/L"}, "mg/dL": {"mg/dL"}, "mg/L": {"mg/L"}, "ng/mL": {"ng/mL"}, "µmol/L": {"umol/L"},
    "μmol/L": {"umol/L"}, "mL/min/1.73m²": {"mL/min/{1.73_m2}"}, "mmol/mol": {"mmol/mol"}, "U/L": {"U/L"},
    "mIU/L": {"m[IU]/L", "mU/L"}, "pg/mL": {"pg/mL"}, "mg/L FEU": {"mg/L"},
}
UNKNOWN_UNITS = {"/GF"}  # no defensible UCUM mapping -> HOLD


def R(*a, **k) -> Row:
    return Row(*a, **k)


COAG = [R("INR (prothrombin time)", "1.0", "", "0.8 to 1.2", "6301-6", "INR (Prothrombinzeit)"),
        R("aPTT", "31", "s", "25 to 37", "14979-9", "aPTT"),
        R("Platelets", "245", "10^9/L", "150 to 400", "777-3", "Thrombozyten"),
        R("Fibrinogen", "3.1", "g/L", "2.0 to 4.0", "3255-7", "Fibrinogen")]
MAX_COAG = [R("INR (prothrombin time)", "2.6", "", "0.8 to 1.2", "6301-6"), R("aPTT", "44", "s", "25 to 37", "14979-9"),
            R("Platelets", "198", "10^9/L", "150 to 400", "777-3"), R("Fibrinogen", "3.4", "g/L", "2.0 to 4.0", "3255-7")]
CBC = [R("Hemoglobin", "13.2", "g/dL", "12.0 to 16.0", "718-7", "Hämoglobin"),
       R("Leukocytes", "6.8", "10^9/L", "4.0 to 10.0", "6690-2", "Leukozyten"),
       R("Erythrocytes", "4.5", "10^12/L", "3.9 to 5.2", "789-8", "Erythrozyten"),
       R("Hematocrit", "40", "%", "36 to 46", "4544-3", "Hämatokrit"),
       R("Platelets", "245", "10^9/L", "150 to 400", "777-3", "Thrombozyten"),
       R("MCV", "89", "fL", "80 to 100", "787-2", "MCV"),
       R("MCH", "29.3", "pg", "27 to 33", "785-6", "MCH"),
       R("MCHC", "33.0", "g/dL", "32 to 36", "786-4", "MCHC")]
LYTES = [R("Sodium", "139", "mmol/L", "135 to 145", "2951-2", "Natrium"),
         R("Potassium", "4.2", "mmol/L", "3.5 to 5.1", "2823-3", "Kalium"),
         R("Chloride", "102", "mmol/L", "98 to 107", "2075-0", "Chlorid"),
         R("Calcium", "2.35", "mmol/L", "2.15 to 2.55", "2000-8", "Calcium")]
INFLAM = [R("CRP", "< 0.5", "mg/dL", "< 0.5", "1988-5", "CRP"),
          R("Procalcitonin", "< 0.05", "ng/mL", "< 0.5", "33959-8", "Procalcitonin"),
          R("Leukocytes", "7.1", "10^9/L", "4.0 to 10.0", "6690-2", "Leukozyten")]
RENAL = [R("Creatinine", "78", "µmol/L", "45 to 84", "14682-9", "Kreatinin"),
         R("eGFR (CKD-EPI)", "> 90", "mL/min/1.73m²", "> 60", "62238-1", "eGFR (CKD-EPI)"),
         R("Urea", "32", "mg/dL", "17 to 43", "3091-6", "Harnstoff")]
HBA1C = [R("HbA1c", "5.6", "%", "4.0 to 6.0", "4548-4", "HbA1c"),
         R("HbA1c (IFCC)", "38", "mmol/mol", "20 to 42", "59261-8", "HbA1c (IFCC)"),
         R("Glucose, fasting", "94", "mg/dL", "70 to 100", "1558-6", "Glukose nüchtern")]
TEXTUAL = [R("ABO group", "A", "", "", "883-9", "AB0-Blutgruppe"),
           R("Rh(D) type", "positive", "", "", "10331-7", "Rhesusfaktor D"),
           R("Antibody screen", "negative", "", "negative", "890-4", "Antikörpersuchtest"),
           R("SARS-CoV-2 RNA (PCR)", "not detected", "", "not detected", "94500-6", "SARS-CoV-2-RNA (PCR)")]
NO_UNITS = [R("INR", "1.1", "", "0.8 to 1.2", "6301-6"), R("HbA1c", "5.4", "", "4.0 to 6.0", "4548-4"),
            R("Potassium", "4.0", "", "3.5 to 5.1", "2823-3"), R("aPTT", "29", "s", "25 to 37", "14979-9")]
OUT_OF_RANGE = [R("CRP", "48", "mg/L", "< 5", "1988-5", "CRP"),
                R("Leukocytes", "13.2", "10^9/L", "4.0 to 10.0", "6690-2", "Leukozyten"),
                R("Hemoglobin", "10.1", "g/dL", "12.0 to 16.0", "718-7", "Hämoglobin"),
                R("Potassium", "3.2", "mmol/L", "3.5 to 5.1", "2823-3", "Kalium")]
GL_UNITS = [R("Platelets", "245", "G/L", "150 to 400", "777-3", "Thrombozyten"),
            R("Leukocytes", "6.8", "G/L", "4.0 to 10.0", "6690-2", "Leukozyten"),
            R("Erythrocytes", "4.5", "T/L", "3.9 to 5.2", "789-8", "Erythrozyten"),
            R("Creatinine", "78", "µmol/L", "45 to 84", "14682-9", "Kreatinin"),
            R("Fibrinogen", "3.1", "g/L", "2.0 to 4.0", "3255-7", "Fibrinogen")]
LIVER = [R("ALT", "22", "U/L", "0 to 35", "1742-6"), R("AST", "25", "U/L", "0 to 35", "1920-8"),
         R("GGT", "18", "U/L", "0 to 40", "2324-2"), R("Bilirubin, total", "0.6", "mg/dL", "0.1 to 1.2", "1975-2"),
         R("Albumin", "4.2", "g/dL", "3.5 to 5.2", "1751-7"), R("Alkaline phosphatase", "70", "U/L", "35 to 105", "6768-6"),
         R("LDH", "180", "U/L", "0 to 250", "2532-0")]
MISC = [R("TSH", "1.8", "mIU/L", "0.27 to 4.2", "3016-3"), R("Cholesterol, total", "182", "mg/dL", "0 to 200", "2093-3"),
        R("HDL cholesterol", "58", "mg/dL", "> 45", "2085-9"), R("LDL cholesterol (calc.)", "104", "mg/dL", "0 to 116", "13457-7"),
        R("Triglycerides", "98", "mg/dL", "0 to 150", "2571-8"), R("Uric acid", "4.9", "mg/dL", "2.4 to 5.7", "3084-1"),
        R("Total protein", "7.1", "g/dL", "6.4 to 8.3", "2885-2"), R("Ferritin", "64", "ng/mL", "13 to 150", "2276-4"),
        R("Vitamin B12", "412", "pg/mL", "197 to 771", "2132-9"), R("Folate", "8.1", "ng/mL", "> 3.9", "2284-8"),
        R("Creatine kinase", "96", "U/L", "0 to 170", "2157-6"), R("CRP", "2.1", "mg/L", "< 5", "1988-5")]
LONG = CBC + LYTES + RENAL + LIVER + HBA1C + COAG + MISC  # 41 rows
UNKNOWN_UNIT = [R("Leukocytes, urine sediment", "5", "/GF", "< 5", "5821-4"), *COAG[:2]]
BAD_LOINC = [R("INR (prothrombin time)", "1.0", "", "0.8 to 1.2", "6301-5"), *COAG[1:]]


def num(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def expected_obs(rows: list[Row], layout: str = "std") -> list[dict]:
    """loinc None = not in the document -> the Observation must carry code.text only (never a guessed code).
    flag None = the document states no flag for that row (no Flag column, or an empty cell) ->
    Observation.interpretation must be absent: no derived H/L/N. A stated flag may be filed, and must then match."""
    out = []
    for r in rows:
        e = {"test": r.test, "loinc": r.loinc if "LOINC code" in LAYOUTS[layout] or "LOINC" in LAYOUTS[layout] else None,
             # only what the lab wrote: an empty Flag cell states nothing (never 'N'), no column states nothing
             "flag": (flag(r) or None) if ("Flag" in LAYOUTS[layout] or "" in LAYOUTS[layout]) else None}
        m = re.fullmatch(r"([<>]=?)\s*([\d.,]+)", r.value)
        if r.value in PENDING:
            e |= {"pending": True, "value_absent": True}       # status registered, NO value[x], a note
        elif r.value.startswith("nicht bestimmbar"):
            e |= {"absent": "error", "value_absent": True}     # final + dataAbsentReason error, NO value[x], a note
        elif m:
            e |= {"comparator": m.group(1), "value": num(m.group(2))}
        elif num(r.value) is not None:
            e |= {"comparator": None, "value": num(r.value)}
        else:
            e |= {"text": r.value}
        # no value -> no valueQuantity -> no UCUM code; the printed unit only qualifies the reference range
        e["ucum"] = sorted(UCUM_EXPECT[r.unit]) if r.unit in UCUM_EXPECT and not e.get("value_absent") else None
        if m := re.fullmatch(r"([\d.,]+) to ([\d.,]+)", r.ref):
            e |= {"low": num(m.group(1)), "high": num(m.group(2))}
        elif m := re.fullmatch(r"<=?\s*([\d.,]+)", r.ref):
            e |= {"high": num(m.group(1))}
        elif m := re.fullmatch(r">=?\s*([\d.,]+)", r.ref):
            e |= {"low": num(m.group(1))}
        out.append(e)
    return out


# ------------------------------------------------------------------ rendering
def de_num(s: str) -> str:
    return re.sub(r"(\d)\.(\d)", r"\1,\2", s)


def de_ref(s: str) -> str:
    return de_num(s.replace(" to ", " – "))


LAYOUTS = {
    "std": ["Test", "Result", "Unit", "Reference range", "LOINC code"],
    "de": ["Parameter", "Ergebnis", "Einheit", "Referenzbereich", "LOINC"],
    "extra": ["Test", "Result", "Unit", "Flag", "Reference range", "LOINC code", "Method", "Comment"],
    "reorder": ["LOINC code", "Test", "Reference range", "Result", "Unit"],
    "noloinc": ["Test", "Result", "Unit", "Reference range"],
    "bold": ["Test", "Result", "Unit", "Reference range", "LOINC code"],
    "flagcol": ["Test", "Result", "", "Unit", "Reference range", "LOINC code"],  # unlabelled flag column (+/-)
}
PENDING = {"folgt", "ausständig", "in Arbeit"}


def flag(r: Row) -> str:
    v, m = num(r.value), re.fullmatch(r"([\d.,]+) to ([\d.,]+)", r.ref)
    if v is None or not m:
        return ""
    return "H" if v > num(m.group(2)) else "L" if v < num(m.group(1)) else ""


def table(rows: list[Row], layout: str = "std", flags: dict | None = None) -> str:
    head = LAYOUTS[layout]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        fl = (flags or {}).get(r.test, flag(r))
        cell = {"Test": r.test, "Result": r.value, "Unit": r.unit, "Reference range": r.ref, "LOINC code": r.loinc,
                "Parameter": r.test_de or r.test, "Ergebnis": de_num(r.value), "Einheit": r.unit,
                "Referenzbereich": de_ref(r.ref), "LOINC": r.loinc, "Flag": fl, "": {"H": "+", "L": "-"}.get(fl, ""),
                "Method": "photometric"
                if r.unit in ("mmol/L", "mg/dL", "U/L") else "", "Comment": ""}
        if layout == "bold":
            cell["Result"] = f"**{r.value}**"
        lines.append("| " + " | ".join(cell[h] for h in head) + " |")
    return "\n".join(lines)


def lab_doc(org: str, patient_lines: list[str], rows: list[Row] | None, *, layout: str = "std", lang: str = "en",
            title: str | None = None,
            requested_by: str | None = "Dr. Lena Hofer, Klinikum Beispielstadt",
            sample: str = "22 October 2026, 08:10", reported: str = "22 October 2026, 11:40",
            comment: str = "All values are within the reference range.",
            signoff: list[str] | None = None, extra_top: list[str] = (), extra_bottom: list[str] = (),
            flags: dict | None = None) -> str:
    de = lang == "de"
    signoff = ["Validated by: Dr. Eva Brandl, lab physician"] if signoff is None else signoff
    top = [f"# {org}: {title or ('Laborbefund' if de else 'lab report')}", "", *patient_lines]
    if requested_by:
        top.append(f"{'Angefordert von' if de else 'Requested by'}: {requested_by}")
    top += [f"{'Probenahme' if de else 'Sample taken'}: {sample}", f"{'Befundet' if de else 'Reported'}: {reported}",
            *extra_top, ""]
    body = [table(rows, layout, flags), ""] if rows is not None else []
    if comment:
        body += [comment, ""]
    return "\n".join([*top, *body, *extra_bottom, *signoff]) + "\n"


def rehab_doc(clinic: str, patient_lines: list[str], *, lang: str = "en", title: str | None = None,
              stay: str = "2 to 23 September 2026",
              reason: str = "rehabilitation after a total hip replacement on the left, on 18 August 2026 at Klinikum Beispielstadt",
              meds: str = "list", signoff: list[str] | None = None, extra_bottom: list[str] = ()) -> str:
    signoff = (["Written by: Dr. Johanna Leitner, medical director, Reha Waldblick",
                "Sent by: Stefan Kern, discharge management"] if signoff is None else signoff)
    if lang == "de":
        head = [f"# {clinic}: {title or 'Entlassungsbericht'}", "", *patient_lines, f"Aufenthalt: {stay}",
                f"Grund: {reason}", "", "## Verlauf", "",
                "Bei Aufnahme mobil mit zwei Unterarmstützkrücken. Nach drei Wochen Physiotherapie Gehstrecke 500 m "
                "ohne Hilfsmittel, Stiegensteigen mit Handlauf. Wundheilung unauffällig, keine Stürze.", ""]
        med = ["## Medikation bei Entlassung", "", "- Rivaroxaban 10 mg 1x täglich bis 29.09.2026",
               "- Paracetamol 500 mg bis 3x täglich bei Bedarf", "- Pantoprazol 20 mg 1x täglich", ""]
        rec = ["## Empfehlungen", "", "- Heimübungen fortsetzen.", "- Ambulante Physiotherapie 2x pro Woche für 6 Wochen.", ""]
    else:
        head = [f"# {clinic}: {title or 'discharge report'}", "", *patient_lines, f"Stay: {stay}", f"Reason: {reason}",
                "", "## Course", "",
                "On arrival, Maria walked with two crutches. After three weeks of physiotherapy she walks up to 500 "
                "metres without aids and climbs stairs with a handrail. The wound healed without problems. There were "
                "no falls.", ""]
        if meds == "table":
            med = ["## Medication at discharge", "", "| Medication | Dose | Frequency | Until |", "|---|---|---|---|",
                   "| Rivaroxaban | 10 mg | once a day | 29 September 2026 |",
                   "| Paracetamol | 500 mg | up to three times a day if needed | |",
                   "| Pantoprazole | 20 mg | once a day | |", ""]
        elif meds == "none":
            med = []
        else:
            med = ["## Medication at discharge", "",
                   "- Rivaroxaban 10 mg once a day until 29 September 2026, to prevent thrombosis for six weeks after the operation",
                   "- Paracetamol 500 mg up to three times a day if needed", "- Pantoprazole 20 mg once a day", ""]
        rec = ["## Recommendations", "", "- Continue the home exercises.",
               "- Outpatient physiotherapy twice a week for six weeks.",
               "- Follow-up X-ray at Klinikum Beispielstadt in November 2026.", ""]
    return "\n".join([*head, *med, *rec, *extra_bottom, *signoff]) + "\n"


def vienna(s: str) -> str:
    """'22 October 2026, 08:10' or '22.10.2026, 08:10' -> ISO with the Europe/Vienna offset of that instant."""
    s = s.replace(",", "")
    for fmt in ("%d %B %Y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=VIENNA).isoformat()
        except ValueError:
            pass
    raise ValueError(s)


# ------------------------------------------------------------------ case
@dataclass
class Case:
    id: str
    group: str
    title: str
    expect: str
    reasons: list[str]
    why: str
    ctx: str
    eid: str
    text: str | None = None
    raw_b64: str | None = None          # for non-text uploads
    canary: str | None = None           # unique string in the document: must never be stored when BLOCKed
    conclusion: str | None = None       # lab comment expected as DiagnosticReport.conclusion (FILE)
    period: list[str] | None = None     # expected DocumentReference.context.period [start, end] (FILE discharge)
    status: str | None = None           # expected DiagnosticReport.status for a FILE lab ('final' if None)
    raw_cp1252: bool = False            # the document is uploaded Windows-1252 encoded (ENCODING_FALLBACK)
    obs: list[dict] | None = None       # expected Observations (FILE lab cases)
    times: dict | None = None           # expected effective/issued (FILE lab cases)
    provenance: list[str] = field(default_factory=list)  # names that must appear in Provenance (author/sender)
    note: str | None = None             # request note (Dr. Hofer's free text)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("text"), d.pop("raw_b64")
        d["file"] = f"{self.id}.md"
        d["request"] = CTX[self.ctx].body()
        d["uploader"] = asdict(EID[self.eid])
        return d

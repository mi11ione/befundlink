# Filing rules: when an answer is filed, held or blocked (v4)

The brief's jury tests with "other outside people and documents in the same Markdown format, some of them for the wrong patient". This table is the contract for the corpus in `corpus/` (136 documents) and for the fuzz generator (`generate.py --fuzz N`). Its principle: **block only real danger, normalise harmless mess, never file wrong content.**
- `refcheck.py` is the executable form of this table. `tests/unit/test_jury.py` proves that every hand label and 600 fuzz labels from an independent oracle agree with it, and that `fhirmap.decide` gives the same outcome and one of the listed reasons on all 136 documents and on 2,000 fuzz cases (`tests/jury/xcheck.py`).
- `tests/e2e/run.py` tests the live product (`service/` + `fhirmap/`) black-box against the labels.

## Outcome classes

| Class | Meaning | What must happen |
|---|---|---|
| **FILE** | Right patient, from the named person | One FHIR transaction: DiagnosticReport + Observations, or DocumentReference, plus Provenance; the Task moves to `completed`. Dr. Hofer gets a "filed" message. |
| **HOLD** | Near-miss or ambiguous | Nothing is filed. The Task moves to `on-hold` with a `statusReason`. Dr. Hofer gets **one decision** in Matrix. Nothing is silently dropped. |
| **BLOCK** | Affirmative evidence of a misdelivery or an attack | Nothing is filed, and nothing of the document is stored (HAPI, DB, disk). The audit records a "misdelivery caught" entry, and Dr. Hofer gets an alert. **The alert and the audit never contain the other patient's name or DOB.** |

**Precedence:** BLOCK > HOLD > FILE. Every rule is evaluated, and the most severe outcome wins.

**Safety asymmetry:** a false FILE puts another person's values (Max: INR 2.6 under phenprocoumon) into Maria's chart the day before her procedure. A false HOLD costs Dr. Hofer one click. So the rule is: **when in doubt, HOLD; if there is evidence of another person, BLOCK.**

The answer is an **upload only**: no typed form, no declared patient. The patient check compares **the document's patient line** with the requested FHIR Patient (Maria Beispiel, 1961-03-12).

## Rule table

| # | Code | Condition | Outcome | Corpus |
|---|---|---|---|---|
| 1 | `EID_MISMATCH` | The eID person is not the named recipient (identity rule v2): names normalised as for patients, hyphens split tokens; the requested name must contain a given and a family name; the eID family name must appear exactly in it; every other requested token must be an eID given name (the eID may add given names); typed order does not matter. If the requested name is SAME as the Patient's, the eID birth date must equal Patient.birthDate. **Identity is binary.** | BLOCK | I01–I03, I06, I08 (I04, I05, I07, I09 FILE) |
| 1b | (identity review) | **Per-recipient eID memory:** the recipient (normalised name + organisation) has answered before with one eID, and a *different* eID with a matching name signs in (another Eva Brandl, born 1955).<br>Nothing is shown or bound; the request stays open for the known person. Dr. Hofer gets a card (birth *year*, contact, the known person's history) and confirms or rejects.<br>On confirm, the new person may answer, attributed to **her own** Practitioner (her own bPK hash). | review → FILE on confirm | e2e `namesake` (stateful, not in the stateless corpus; I04/I05 use their own organisation key) |
| 2 | `EMPTY` / `OVERSIZE` / `ENCODING_UNREADABLE` / `UNREADABLE` | `EMPTY`: 0 bytes, or nothing but (Unicode) whitespace after decoding. `OVERSIZE`: over 1 MB. **Decoding:** UTF-8 (BOM dropped) first, then Windows-1252, then Latin-1, each only if the result is plausible text with no C0/C1 control characters; a fallback is a warning (`ENCODING_FALLBACK`, A39). `ENCODING_UNREADABLE`: no encoding gives text (binary, X07). `UNREADABLE`: decoded text that contains control characters (X08). | BLOCK | X05–X08, X11 (A39 FILE) |
| 3 | `ACTIVE_CONTENT` | `<script`, `<iframe`, `<object`, `<embed`, `<svg`, `<math`, `<form`, `<meta`, `<link`, `<style`, an `on…=` handler, `javascript:`, `vbscript:` or `data:text/html` anywhere in the raw text | BLOCK | X01–X04 |
| 3b | `ACTIVE_CONTENT_SUSPECT` | **HOLD:** a tag other than the benign set, a tag *with attributes*, a Markdown link or image whose target is not https or relative, an autolink whose scheme is not https (`<mailto:…>`), a bare URL with a non-inert scheme (`ftp:`, `file:`, `data:`, …), or a rule-3 pattern / non-benign tag hidden behind HTML entities or %-encoding.<br>**FILE, with an `EXTERNAL_RESOURCE` note:** attribute-free `br b strong i em sup sub u p span` (normalised away), HTML entities, https or relative images and links, e-mail autolinks `<name@host>`, and bare `http`/`mailto`/`tel` text. | HOLD | X13, X15–X18 (X12, X14, A33, A40, A41 FILE) |
| 4 | `WRONG_PATIENT` | The patient name is DIFFERENT, **or** the DOB is DIFFERENT (see below). With several people in the document, it applies when all of them are DIFFERENT. | BLOCK | W01–W07, X09, X10 |
| 4b | `OTHER_PATIENT_PRESENT` | A **second person** with a name and a birth date is present anywhere and is DIFFERENT, while another line names the requested patient. This covers a second patient line, a second person in the tail of the patient line itself (`Patient: Maria Beispiel, geb. 12.03.1961; Kopie an Max Beispiel, geb. 01.01.1960`), a line hidden in an HTML comment, a page header (`BEISPIEL Max · *01.01.1960`), a title, a sentence (`Vorbefund von Max Beispiel, geb. 01.01.1960.`), the lab's comment in the tube-label form without any birth mark (`Röhrchen beschriftet mit „Beispiel, Max, 01.01.1960“`: Family, Given, a date at least a year back), or a table cell. **Never overridable**, not even by Dr. Hofer's decision: another person's data is in the upload. | BLOCK | H14, H18, W08, W10–W13 |
| 4d | `IDENTITY_DOUBT` | The document itself doubts whose sample or document it is: `beschriftet mit`, `falsch beschriftet`, `Verwechslung`, `Identität … / Zuordnung … prüfen / klären / unklar`, `mislabelled`, `labelled with`, `mix-up`, `identity … check / confirm`. A confirmation (`Identität per Armband bestätigt`, A49) is not a doubt. | HOLD | H38 (A49 FILE) |
| 4c | `SVNR_MISMATCH` | The Austrian SVNR (`NNNC DDMMYY`) encodes another birth date than the Patient's. A bad check digit with the right date is only a warning (I10). | BLOCK | W09 (I10 FILE) |
| 5 | `PATIENT_MISSING` | No patient line in any accepted form (see "Patient line forms") | HOLD | H10 |
| 6 | `PATIENT_INCOMPLETE` | The name or the DOB is missing; an age is not a DOB. A DOB line (`Date of birth:`, `Geb.-Datum:`) next to a single patient line counts. | HOLD | H11–H13 (A32, A35 FILE) |
| 7 | `PATIENT_CONFLICT` | Several patient lines that are only SAME/NEAR variants of each other but disagree (`Maria Beispiel` 1961 / 1962), or a birth date of a person without a name that is DIFFERENT. If one line is a DIFFERENT person → rule 4b. | HOLD | H15 (A22 FILE) |
| 8 | `NAME_NEAR` | The name is NEAR (see below) | HOLD | H03, H04, H06–H09 (H05, H28 FILE) |
| 9 | `DOB_NEAR` / `DOB_AMBIGUOUS` / `DOB_UNPARSEABLE` | The DOB is NEAR, a slash date with day and month both ≤ 12, a two-digit year without exactly one past expansion that EQUALS the request (`12.03.62`), or not a real date | HOLD | H01, H02, H16, H17, H27, H36 (A37 FILE) |
| 9b | `SEX_MISMATCH` | A sex stated in the document (`Geschlecht:`, `weiblich`/`männlich` in the patient line) differs from Patient.gender | HOLD | H29 (A35 FILE) |
| 10 | `KIND_MISMATCH` / `KIND_UNKNOWN` | A lab report on a discharge-report request, or the reverse (title after the `:`) | HOLD | H21, H22 |
| 10b | `MULTIPLE_REPORTS` | Two or more report headings, or different sample or report times: each report must be sent on its own | HOLD | H30 |
| 10d | `CORRECTION_REVIEW` / `PRIOR_NOT_FOUND` | A corrected report (title `Korrektur` / `korrigiert` / `berichtigt` / `corrected`; body `korrigierter Befund` / "ersetzt den Befund …") or an addendum (`Nachtrag` / `Ergänzungsbefund` / `addendum`). **A correction must not sit next to the original:** on Dr. Hofer's override it is filed as a *new version* of the original (`fhirmap.build_correction`). The DiagnosticReport becomes `corrected`/`amended`, changed Observations become `corrected`, and Provenance `entity[revision]` points to the replaced version. If no unique prior report is found, it stays held: `PRIOR_NOT_FOUND`. | HOLD | A43, A44 |
| 10c | `FUTURE_DATE` | A sample or report time more than 2 h in the future | HOLD | H32 |
| 11 | `NO_RESULTS` | A lab report with no table or no rows | HOLD | H23 |
| 11b | `UNIT_MISSING` | A numeric result with an empty unit cell. HbA1c 5.4 could be % or mmol/mol. **A unit is never assumed.** Exceptions: a dimensionless LOINC, or INR (by code, or by name when there is no LOINC). | HOLD | A07 (A01 FILE) |
| 11e | `TABLE_MALFORMED` | A row with more or fewer cells than the header. **GFM exception:** a row one cell short is read when the missing column is the trailing LOINC / flag column and its range and unit cells still fit (a LOINC that slid under the range stays held). Urine specific gravity `1,015` (LOINC 5811-5 or the test name, no unit) is a decimal comma; `1,015 mg/dl` elsewhere stays `VALUE_AMBIGUOUS`. | HOLD | `fresh/holdout3` G07, G08, Z07, Z08 |
| 11c | `TABLE_SPLIT` | Lab table rows outside the parsed result table (a page break split the table) | HOLD | H31 |
| 11d | `FLAG_CONFLICT` | A flag in the result cell (`13.2 H`) and the flag column (`L`) disagree | HOLD | H37 |
| 12 | `UNKNOWN_UNIT` | A unit with no defensible UCUM code (`/GF`). **A UCUM code is never invented.** | HOLD | H24 |
| 13 | `LOINC_CHECKDIGIT` | The LOINC mod-10 check digit fails | HOLD | H25 |
| 14 | `LOINC_UNIT_DIMENSION` | The unit's dimension contradicts the LOINC property: platelets 777-3 (count/volume) in `g/L` (mass/volume), or any unit on a dimensionless code such as INR | HOLD | H26 |
| 15 | (information) | `Requested by:` / `Einsender:` / `Zuweiser:` naming someone else is **information, not a hold**. It is shown on Dr. Hofer's card (DiagnosticReport.basedOn display); it is often the ordering ward. | FILE | H19, A25, A28 |
| 16 | `SENDER_CONTRADICTS_EID` | `Sent by:` / `Übermittelt von:` names someone other than the eID-verified recipient | HOLD | H20 |
| 17 | `ORG_MISMATCH` / `STALE_REPORT` / `REPORT_INTERIM` | • **Lab and discharge reports:** the issuing lab or clinic (title before the `:` / ` – `, or the H1 above a type subtitle) is not the requested organisation. Same organisation = one name's tokens are a subset of the other's, or, after dropping generic words (Labor, Reha, Rehabilitationszentrum, GmbH, …), every distinctive token matches (`Reha Waldblick` = `Rehabilitationszentrum Waldblick`, `Labor Kanalstr.` = `Labor Kanalstraße`). The card says `Labor laut Dokument: <org> – angefragt: <org>`.<br>Discharge reports only:<br>• the stay ended more than 365 days ago, or before the surgery named under `Reason:`;<br>• the report is a `Zwischenbericht` (interim). | HOLD | H33, H34, H35 |
| 18 | `EXTRACTION_DISAGREE` / `EXTRACTION_UNREADABLE` | Photo intake only (`PGK_EXTRACTOR=anthropic`): a photo or PDF is transcribed twice, independently. The two parses differ in anything that is filed or decides the outcome (patient, kind, any row: test, value, comparator, unit, range; the times), or a reading contains `[unleserlich]`, or a reading failed. The reason names the field ("KI-Lesungen weichen ab: INR 1.0 vs 1.8 – bitte am Original prüfen"), never another patient. A BLOCK rule on **either** reading blocks. Filing it anyway files only the original photo, no transcribed value. Agreeing readings run through rules 1–17 unchanged. | HOLD | `tests/extract_eval.py` |
| – | (none) | Everything else | FILE | A01–A06, A08–A42, A45–A49, R01–R08, H05, H19, H28, I04, I05, I07, I09, I10, X12, X14 |

### Patient line forms

**SAME forms:**
- `Patient:` / `Patientin:` / `Pat.:` / `Name:` / `Patient name:` / `Name des Patienten:` / `Betrifft:` (with a date).
- A separate `Geb.-Datum:` / `Geburtsdatum:` / `Date of birth:` line, and a `Geschlecht:` line (A35).
- A patient line in a heading, or in a two-column key/value table.
- An ALL-CAPS family name, which marks the family name: `BEISPIEL Maria` = Maria Beispiel (A34).
- **Under `Name:` the order is free** (header-block convention, A35).
- **Surname first without a comma, `Patient: Beispiel Maria, *12.03.1961`, is SAME** when the name tokens are exactly the requested ones in another order (no token added, missing, abbreviated or changed; Latin script) **and** the birth date is SAME and written in full (not a two-digit year). Austrian hospital and lab systems commonly print a patient this way; a mix-up would need another person with the same two names reversed and the same birth date. H05/H28 FILE. Anything else stays NEAR (`Beispiel Maria` with 13.03.1961, `12.03.61`, `Beispiel Maria Theresia`).
- Labels `Name, Vorname:` / `Nachname, Vorname:` (family first by the label), `Familienname:` + `Vorname:` lines; dotted or underscore leaders (`Patientin ........ Maria Beispiel`, `Geb.-Datum ____ 12.03.1961`); a letter subject `Betreff: Gerinnungsbefund Ihrer Patientin Frau Maria Beispiel, geb. …` (only the person after Frau/Herr/Patientin is the name; another name before it is kept).
- A slash date `12/03/1961` stays `DOB_AMBIGUOUS` (HOLD) and the card quotes it: „Geburtsdatum mehrdeutig geschrieben (12/03/1961) – bitte prüfen.“
- Name, birth date and SVNR on one line with `·` (`Name: Beispiel Maria · Geb.-Dat.: 12.03.1961 · SVNR: 1237 120361`, also unlabelled `Maria Beispiel · 12.03.1961 · SVNR 1234 120361`); the SVNR is checked as on its own line (rule 4c).
- A spaced dash separates (`Beispiel, Maria — geboren …`, `Maria – Beispiel`); `Beispiel-Huber` stays one name and NEAR.
- A two-digit birth year written without `geb.` is read like the marked one (rule 9). With a reversed name (`Beispiel Maria, 12.03.61`) it stays held as `DOB_AMBIGUOUS`: „Geburtsjahr zweistellig (12.03.61) – bitte prüfen.“
- A two-digit year is SAME only if its single past expansion equals the Patient's DOB (`12.03.61`, A37). Otherwise it is `DOB_AMBIGUOUS` (`12.03.62`, H36).
- Austrian titles of office are not name parts: OStR, OSR, HR, KR, KommR, RegR, ÖkR, … (A38).

- **No label at all** (`tests/jury/fresh`): `Für: Frau Mag. Maria Beispiel, geboren am 12. März 1961`, `**Maria Beispiel**, geb. …`, `Laboratory findings for Maria Beispiel, date of birth …`. Read as the patient line only if the whole document names exactly **one** person with a birth date and no nameless birth date differs; otherwise `PATIENT_MISSING`, and any second person is rule 4/4b.
- `Name:` lines in a key/value table anywhere, or under a header section (`## Patientendaten`, `## Patient`).
- Markdown emphasis and `·` / `•` separators inside the line (`Frau Mag. Maria Beispiel · geb. 1961-03-12`); several header fields on one line (`Abnahme: … · Befund: …`).

**Not a patient line:** a `Patient:` bullet inside a section without a date (R08).

**Tables inside discharge reports** stay text and are never parsed as lab values (R07).

### Patient name: SAME / NEAR / DIFFERENT

**Normalisation:**
1. NFKC.
2. Collapse whitespace, including NBSP.
3. Drop titles and salutations (`Dr.`, `Mag.a`, `Dipl.-Ing.`, `Frau`, `Herr`…).
4. `Family, Given` with exactly one comma becomes `Given Family`.
5. casefold.
6. **German umlauts are transliterated per ICAO Doc 9303** (ä→ae, ö→oe, ü→ue, ß→ss). Every other diacritic is stripped (á→a).
7. Tokens are compared **in order**.

**Classification:**
- **SAME:** identical ordered tokens, in Latin script only. Covers A16–A20.
- **NEAR:**
  - the confusable skeleton is equal (a Cyrillic а);
  - the same tokens in another order (`Beispiel Maria`), unless the birth date is SAME and written in full (then SAME);
  - a token superset (`Maria Theresia Beispiel`, `Beispiel-Huber`);
  - a Levenshtein distance ≤ 2 (`Marie`, `Bespiel`, `Märia` = `maeria`).
- **DIFFERENT:** everything else (`Max`, `Anna Huber`, `Maria Huber`).

**Judgement calls:**
- **`María` = `Maria` → SAME.** Systems that cannot store acute and grave accents drop or add them: an encoding artefact, not a different name.
- **`Märia` ≠ `Maria` → NEAR (HOLD).** In German, ä is a different letter, with the official transliteration "ae" (ICAO 9303, passports). So `Müller` = `Mueller` (SAME), but `Muller` vs `Müller` is NEAR. Folding ä→a would merge real surnames that are distinct (Muller/Müller, Schon/Schön).
- **`Beispiel Maria` → SAME only with the exact full birth date, otherwise NEAR; `Beispiel, Maria` → SAME.** Without a comma, given and family cannot be told apart, so the swap is accepted only when nothing else differs: a given/family swap of another person still needs that person's exact birth date.
- **Homoglyphs → NEAR, not SAME.** A mixed-script name looks equal but is either encoding damage or an attack, so Dr. Hofer decides.

### Birth date: SAME / NEAR / DIFFERENT

**Formats parsed:**
- `1961-03-12`
- `12.03.1961` and `12.3.1961`
- `12. März 1961` and `12. Jänner …` (Austrian)
- `12 March 1961` and `12 Mar 1961`
- `March 12, 1961`

A slash date is accepted only when it is unambiguous (`13/03/1961`). `12/03/1961` is `DOB_AMBIGUOUS` and is never guessed.

**Classification:**
- **NEAR:**
  - day and month swapped (1961-12-03);
  - exactly one digit different in YYYYMMDD (13.03.1961, 1951, 12.08.1961);
  - an adjacent transposition (1916);
  - ambiguous or unparseable.
- **DIFFERENT:** everything else (1960-01-01, 1958-07-14).

### Sender, author and requester (author ≠ sender is legitimate)

- **The sender is established by the eID, never by the text.**
  - `Validated by:` (lab) and `Written by:` (discharge) name the **author**. That is asserted by the document: Provenance `agent[author]` and DiagnosticReport `resultsInterpreter` / DocumentReference `author`.
  - The eID person is `agent[informant]`, the verified sender.
- **R01 (Leitner writes, Kern sends) → FILE.** **A30 (validated by a colleague, sent by Dr. Brandl) → FILE**, with both names in the Provenance. The brief's "a colleague is not allowed to answer for them" is about who *answers*, which the eID enforces (rule 1: I01–I03, M2); a colleague's *validation* is normal lab practice, and blocking it would also block request 2.
- **An explicit `Sent by:` naming someone else (H20) → HOLD.** It contradicts the verified sender, so Dr. Hofer decides.

## What a FILE must look like (checked in e2e, per case)

**Lab:**
- `DiagnosticReport.code` = LOINC 11502-2, category `LAB`; `subject` = `Patient/maria-beispiel`.
- `presentedForm` = the original upload as `text/markdown`, with `hash` = base64 SHA-1 of the data.
- `conclusion` = the lab's own comment, when there is one (benign markup normalised). Comment rows inside the table go to the conclusion too; group rows are skipped.
- **Report status:**
  - `vorläufig` / `Vorbefund` (title only) / `Teilbefund` / any pending row → `preliminary` (A45, A46);
  - otherwise `final`.
  - Corrections and addenda are not filed directly: rule 10d.

**Every Observation:**
- **Code:** the given LOINC. If the document has none (A13), `code.text` only, and **a LOINC is never guessed**.
- **Value:**
  - `valueQuantity.value` / `comparator` (`< 0.5` → `<` 0.5);
  - text for textual results, `neg.` / `pos.` exactly as written (A42);
  - a pending row (`folgt` / `ausständig` / `in Arbeit`) → Observation `registered`, **no value**, with a note (A46);
  - `nicht bestimmbar (hämolytisch)` → `final` with `dataAbsentReason` `error` and a note (A47);
  - in a cumulative report, the `Ergebnis` / `aktuell` (or latest-date) column is the value and earlier values become notes.
- **Unit:** the UCUM *code*, never the display: `10^9/L` and `G/L` → `10*9/L`, `T/L` → `10*12/L`, `µmol/L` → `umol/L`, `mL/min/1.73m²` → `mL/min/{1.73_m2}`.
- **Range:** `referenceRange.low/high` as printed. `prove()` counts the source's value rows independently.
- **Interpretation:** **only flags the lab wrote, and those are filed.** That is a Flag column, or an unlabelled column of flag tokens (`+`→H, `-`→L, `*`→A where unambiguous, A48), always with the lab's text.
  - An empty Flag cell states nothing, so it is never filed as "N". In A11, CRP 48 against "< 5" has an empty cell → no interpretation.
  - No flag is ever derived from the range (A24).

**Times:**
- `effectiveDateTime` and `issued` = the document's times **with the Europe/Vienna offset of that date**.
- 22 Oct 2026 08:10 → `+02:00`. 27 and 28 Oct 2026 → `+01:00`, because CEST ended on 25 Oct (A03, A06).
- Every other filed dateTime or instant must carry an explicit offset that equals Vienna's offset at that instant.

**Discharge:**
- `DocumentReference`: `type` 11490-0, category 18842-5, and `content.attachment` = the original upload, with its SHA-1 hash.

**Provenance and attribution:**
- `target` covers every filed resource.
- **The sender is the eID that logged in.**
  - The Provenance sender (`informant`) is a Practitioner whose identifier is `…/sid/bpk-hmac-sha256` = the `bpk_hash` in that request's successful "identity checked" audit row.
  - It carries **no register id**: an Ärzteliste or GDA-Index match by name is never a Practitioner identifier.
  - The document's signatory (`author`) is a separate Practitioner.
  - A namesake gets her own Practitioner.
- **Chain of custody:** the SHA-256 of the raw upload bytes (Windows-1252 included) = the receipt = the audit "answer sent" = the Provenance `entity[source]` hash.
- `signature`: `sigFormat` `application/jose`.
- A simulated eID: policy `…/policy/eid-simulated`, never the eIDAS LoA URI. `signature` covers `content_sha256` and `targets`.
- `entity[source]`: an identifier holding the SHA-256 of the upload.

**Transaction and Task:**
- All of it is filed in **one transaction**: one `meta.lastUpdated` across the set.
- One Task per request: `requested` → `completed` / `on-hold` / `rejected`. `Task.for` = the Patient, and `output` → the filed report.

## What a BLOCK must look like

- No new DiagnosticReport, DocumentReference or Observation (a global count check over the run, plus a per-case canary search).
- The case's canary is a unique `Sample ID` / `Case number`, or for W01 the word "phenprocoumon". It must not appear in:
  - any HAPI resource;
  - the service DB or its data directory;
  - any Matrix message;
  - any audit entry (`/api/audit` and the AuditEvents on HAPI).
- For WRONG_PATIENT, the other patient's name tokens and every DOB rendering are also searched for in the Matrix alert and in the audit.

## Messages (email, SMS, links)

None of these may appear in a subject, body, URL or relayed note:
- the patient's name tokens;
- any DOB rendering (`norm.dob_variants`);
- the patient id or the MRN;
- a document-type word;
- the recipient's organisation name (Labor, Reha, Kanalstraße, Waldblick, …).

Dr. Hofer's free-text note is a leak vector. A request whose note contains PII is either refused at send time or sent with the note stripped.

## Privacy at rest (checked by `metrics.py` on the live stack)

- **No e-mail address or phone number** in any FHIR AuditEvent, nor in the hashed (chained) part of the service's audit rows.
  - "request sent" carries only `detail.contact_hash` (a keyed hash) and the channel.
  - The cleartext `contact` is kept beside the chain and becomes `[gelöscht]` after expiry.
- **`request_text`** (what Dr. Hofer asks for, shown only after the eID) is cleared once the answer is filed. The corpus sends one with every request, and the PII scan proves it never reaches the email or SMS.
- **File permissions:** `service/data/` is `0700`, and the SQLite files (`*.sqlite`, `-wal`, `-shm`) and key files (`*.pem`) are `0600`.
- **Audit anchors:** the anchors of the chain head on HAPI (AuditEvent, subtype `audit-anchor|chain-head`) and in Matrix (`[AUDIT-ANKER] Audit-Kette <chain> bis Eintrag #N: <hash>`) must equal the independently re-verified chain.
  - Only anchors with this installation's chain id are considered, where chain id = HMAC(k_audit, "pgk-bridge anchor chain id")[:16].
  - Every per-request API call is made as the requester in the request's room (`X-PGK-Actor` / `X-PGK-Room`). The operator view uses the API key alone.

## Corpus summary

| Group | n | FILE | HOLD | BLOCK | What |
|---|---|---|---|---|---|
| A | 49 | 46 | 3 | 0 | Right patient, lab:<br>• CBC, electrolytes, CRP/PCT with `<`, creatinine µmol/L, eGFR `>`, HbA1c %/mmol/mol<br>• textual and `neg.`/`pos.` results, G/L/T/L; missing units on measured analytes → HOLD (A07)<br>• German dates and labels, extra/reordered/no-LOINC columns, unlabelled `+`/`-` flag column<br>• caps/accent/comma/title/NBSP names, `BEISPIEL Maria`, `Name:` + `Geb.-Datum:` + `Geschlecht:`, `Betrifft:`, `12.03.61`, OStR<br>• 41 rows, CRLF+BOM, Windows-1252<br>• entities and benign tags, relative and https links, bare http/mailto text<br>• corrected / Nachtrag → HOLD `CORRECTION_REVIEW`, preliminary, pending row, `nicht bestimmbar`<br>• author ≠ sender, DST change, a confirmed sample label (A49) |
| R | 8 | 8 | 0 | 0 | Rehab and discharge reports: scenario (SMS), other clinic, German, hospital letter, no `Sent by`, medication table, a table that stays text, a `Patient:` bullet in prose |
| W | 13 | 0 | 0 | 13 | Another patient: scenario Max (M3), other name with the same DOB, namesake with another DOB, Max with Maria's DOB, rehab for another person; a second person in a page header, a title, a sentence, a tube label in the lab's comment or a table cell; SVNR of another DOB |
| H | 38 | 3 | 33 | 2 | Near-misses: DOB swap and one-digit slip, typo, reversed, umlaut, homoglyph, hyphenated, extra given name, missing/incomplete/conflicting patient line, ambiguous or impossible DOB, `12.03.62`, other `Sent by`, wrong kind, empty table, unknown unit, bad LOINC check digit, unit dimension, sex, two reports, split table, future date, other clinic, stale or interim discharge report, flag conflict, a lab doubting the sample's identity. A second person (H14, H18) → BLOCK; another Einsender (H19) and `Beispiel Maria` with the exact DOB (H05, H28) → FILE |
| I | 10 | 5 | 0 | 5 | Identity (rule v2): colleague, `Eve` vs `Eva`, SMS colleague, family `Brandl-Huber` (BLOCK); eID `Eva Maria` / `Eva-Maria`, request typed `Brandl Eva`, SVNR typo (FILE); patient as recipient: same-named daughter with another DOB (BLOCK), the patient herself (FILE) |
| X | 18 | 2 | 5 | 11 | Malicious or malformed:<br>• BLOCK: script in the name or comment, `onerror`, `javascript:`, 2 MiB, 0 bytes, whitespace-only, PNG bytes, NUL/ESC, LLM prompt injection, 100 kB name<br>• HOLD: `<img src=x>`, http link, `<span style=…>`, entity-encoded script, `<mailto:…>`<br>• FILE: benign `<b>`, https image |
| **Σ** | **136** | **64** | **41** | **31** | |

Scored per case: the outcome class must match exactly. The headline safety numbers are reported separately:
- wrong-patient filed: 0 of N(BLOCK + HOLD);
- right patient held: k of N(FILE).

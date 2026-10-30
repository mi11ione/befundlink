# Befundlink: architecture

The detail behind the [README](../README.md): operation, components, the filing pipeline, the FHIR tree, misdelivery handling, correctness and load. Identity, security, retention and the legal mapping are in [SECURITY-LEGAL.md](SECURITY-LEGAL.md); every measured number is in [`tests/METRICS.md`](../tests/METRICS.md).

- [Running and operating](#running-and-operating)
- [Components and services](#components-and-services)
- [The filing pipeline (one upload)](#the-filing-pipeline-one-upload)
- [FHIR resources and references](#fhir-resources-and-references)
- [Misdelivery handling](#misdelivery-handling)
- [Correctness](#correctness)
- [Load](#load)

---

## Running and operating

### Make targets

| Target | What it does |
|---|---|
| `make bootstrap` | Pulls 4 pinned images, downloads `validator_cli.jar` 7.0.2 (~200 MB, SHA-256 checked), creates the Matrix users, both rooms and the Patients on HAPI, runs `uv sync` and a smoke test ("ALL PASS"). |
| `make demo` | `PGK_DEMO=1 make run`: service :8000 and bot :8001 on 127.0.0.1; writes the `.env` secrets on first run. |
| `make run` | The same without the demo shortcuts. `make run HOST=0.0.0.0 PGK_BASE_URL=http://<lan-ip>:8000` exposes the service and its `/r/*` links on the LAN, for a phone test. |
| `make demo-reset PULL=never` | Wipes the service DB, bot state and all volumes and bootstraps again without image pulls (`uv sync` and the terminology prewarm still use the network if there is one). Restart `make demo` afterwards: the room id and tokens change. |
| `make audit-verify` | Re-verifies the audit chain and its external anchors ([how](SECURITY-LEGAL.md#keyed-and-anchored-audit)). |
| `make verify-provenance` | Re-verifies every filing's signed statement against what HAPI holds now. |
| `make status` / `make test` / `make clean` | Health of each service / the test suite / wipe Matrix, HAPI and Mailpit data. |
| `make env-keys` | Appends secrets added later (`PGK_NOTIFY_KEY`) to an existing `.env`. |

Java 21 is found at `/opt/homebrew/opt/openjdk@21/bin/java` or as `java` on `PATH`.

### Demo mode

`PGK_DEMO=1` turns on three shortcuts:
- A browser on this machine may open the simulated SMS inbox without `X-API-Key`.
- The simulated ID Austria lists demo personas (a PIN is still needed). The list never shows the named recipient of an open request.
- A request to a person with no simulated ID Austria enrols one for exactly that person, with a random 6-digit PIN shown on Dr. Hofer's send card and audited as "Demo-Identität angelegt". A forwarded link still fails: the other person has no PIN.

Also reachable: the audit room "Befundlink Audit" in Element (the audit anchors) and HAPI at http://localhost:8090/fhir (loopback, no login).

**Personas** are in [`service/data/personas.json`](../service/data/personas.json), the PINs also in [`service/data/demo_pins.json`](../service/data/demo_pins.json). Sign-in takes given name(s), family name and PIN; no birth date. Operators enrol a test person with `POST /api/eid/personas` (API key; the PIN is returned once).

### What M1–M3 show

| Test | You see |
|---|---|
| **M1** wrong address | The card: "Domain 'labor-kanaI.example' sieht aus wie die bekannte Domain 'labor-kanal.example'", plus a capital-letter warning. Petra Fremd gets a "blocked" page with no patient, document type or upload. Element: "[FEHLZUSTELLUNG ABGEFANGEN] … Zugriff von einer anderen Person (Petra Fremd) blockiert … über den Link an e\*\*\*@labor-kanai.example". Typing "Eva Brandl" without her PIN: "Diese Identität ist geschützt". |
| **M3** wrong patient | The page: "Dieses Dokument betrifft nicht Maria Beispiel (geb. 12.03.1961). Es wurde sofort verworfen …". Element: "[FEHLZUSTELLUNG ABGEFANGEN] … Dokument betrifft eine andere Person … nicht abgelegt", without naming the other patient. HAPI gets only the `misdelivery caught` AuditEvent with the file's SHA-256. |

Each case writes a `misdelivery caught` AuditEvent; M1 and M2 also a failed `identity checked`. Read them with `!audit <KB-id>`, or `GET /fhir/AuditEvent?subtype=misdelivery-caught`. After 5 failed eID checks a link locks; `!freigeben <KB-id>` re-opens it.

### The namesake walkthrough

After Eva Brandl has filed an answer, send her another request and sign in as **Eva Maria / Brandl / 104928**. The name fits the request, but the ID Austria identity is new, so before anything is shown:
- the page says "Ihre Identität wird von der anfragenden Ärztin bestätigt" and shows nothing;
- Element shows "🔐 Neue ID-Austria-Identität für Dr. Eva Brandl (KB-…)" with the new person's birth year, the masked contact and the known person's history;
- `!identität <KB-id> ja` lets her answer, filed under **her own** Practitioner; `nein` keeps her out.

The known Eva Brandl can answer meanwhile.

### More of the demo

**Request 2, rehab report by SMS:** post `!reha Kern maria-beispiel`, then `!ja`. Open the SMS inbox (http://localhost:8000/sms), click the link, sign in as **Stefan / Kern / 829022** and upload `rehab-report-maria-beispiel.md`. Element shows "📥 Entlassungsbericht abgelegt", leading with the discharge medication.

**More personas:** Eva Maria Brandl `104928` (the namesake test), Maria Beispiel born 1961-03-12 `850449` (the patient herself as recipient), Maria Beispiel born 1990-06-15 `586525` (her daughter of the same name).

**Your own outside person:** `!anfordern labor <Name>, <Rolle>, <Organisation>, <E-Mail oder Mobilnummer>, Patient maria-beispiel`. The send card shows "🔑 Demo: simulierte ID Austria für … – PIN …" to pass on, standing in for their ID Austria app. Patients: `maria-beispiel`, `max-beispiel`.

**A held answer:** upload [`tests/jury/corpus/H03.md`](../tests/jury/corpus/H03.md) ("Marie"). Dr. Hofer gets one decision; `!ablegen <id> <Begründung>` files it with her reason.

**Tamper with the chart:** set the last filed INR to 2.9 directly on the FHIR server, then re-check every filing:
```bash
OBS=$(curl -s "localhost:8090/fhir/Observation?code=http://loinc.org|6301-6&_sort=-_lastUpdated&_count=1&_elements=id" | python3 -c "import json,sys;print(json.load(sys.stdin)['entry'][0]['resource']['id'])")
curl -s localhost:8090/fhir/Observation/$OBS | python3 -c "import json,sys;o=json.load(sys.stdin);o['valueQuantity']['value']=2.9;print(json.dumps(o))" | curl -s -o /dev/null -X PUT -H 'Content-Type: application/fhir+json' --data-binary @- localhost:8090/fhir/Observation/$OBS
make verify-provenance   # names the changed Observation; Element shows "[ACHTUNG] … verändert" in Dr. Hofer's room
```

### Bot commands

German and English, for members of the PGK directory only (`!hilfe` / `!help` lists them).

| Command | What it does |
|---|---|
| `!anfordern labor\|reha <Name>, <Rolle>, <Organisation>, <E-Mail oder Mobilnummer>, Patient <id>` / `!request lab\|report …` | Pre-flight check card, then `!ja` / `!nein` (`!yes` / `!no`) |
| `!labor <Nachname> <Patienten-ID>` / `!reha …` | Short form for a recipient in the register |
| `…, Text: <what you need>` | Shown to the outside person only after the ID Austria check |
| `…, Notiz: <text>` | Stays inside the clinic: never in the email or SMS; filed as `Task.note` |
| `!status [<id>]`, `!audit <id>` | State of a request (without id: the last one); its audit log |
| `!zeigen <id>` (`!show`) | The text of a held answer |
| `!ablegen <id> <Begründung>` / `!verwerfen <id>` / `!halten <id>` (`!file` / `!reject` / `!hold`) | Decide on a held answer: file with the reason in the Provenance / discard and keep the link open for a corrected one / keep holding |
| `!identität <id> ja\|nein` (`!identity <id> yes\|no`) | Confirm or reject a new ID Austria identity for a known recipient |
| `!freigeben <id>` (`!unlock`) | Re-open a link locked after 5 failed eID checks |

---

## Components and services

```
 Klinikum Beispielstadt: one docker compose stack per clinic, every port on 127.0.0.1         outside
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │ Element Web :8080 ──► Synapse :8008 ◄──► bot (matrix-nio): members-only commands,      │
 │  Dr. Hofer               klinikum.example   check cards, POST /notify :8001            │
 │                                                │ API key + actor/room    ▲ notify key  │
 │                                                ▼                         │             │
 │  ┌──────────────────────────── service (FastAPI) :8000 ──────────────────┴─────────┐   │   email: Mailpit
 │  │ pre-flight: look-alike address + register (ÖÄK / GDA-Index MOCK); PII guard     ├───┼─► SMS: /sms (keyed)
 │  │ /r/<token> ── simulated ID Austria (/eid, ES256, PIN personas) ── upload        ◄───┼── outside browser
 │  │ upload: size gate → fhirmap.decide() → encrypt raw bytes (AES-GCM) → queue      │   │
 │  │ AnswerWorker: re-check vs live Patient → build → warm HL7 validator → prove()   │   │
 │  │               → resolve_conditionals → one transaction → read back + verify     │   │
 │  │ Outbox (ordered): Task PUTs, AuditEvents, Matrix cards                          │   │
 │  │ Maintenance: retention, anchors (10 min), Task reconcile, re-verify (1 h)       │   │
 │  │ SQLite: requests (no patient name/DOB), HMAC-keyed audit chain, held uploads    │   │
 │  └──────────────┬──────────────────────────────────────────────────────────────────┘   │
 │                 ▼                                                                      │
 │  HAPI FHIR R4 :8090 (versioned, nothing deletable: no DELETE, no $expunge;             │
 │                      AuditEvents immutable)                                            │
 │  Patient · Task · DiagnosticReport+Observation | DocumentReference · Provenance ·      │
 │  Practitioner · PractitionerRole · Organization · AuditEvent (6 steps + chain anchors) │
 └────────────────────────────────────────────────────────────────────────────────────────┘
            tx.fhir.org: terminology for the validator (offline fallback)
```

### Components we built

What each does is in the [README](../README.md#how-it-works); beyond it:
- [`bot/`](../bot/) runs a dry-run pre-flight before every send, carries `X-PGK-Actor` and `X-PGK-Room` on every call, and serves `POST /notify` on 127.0.0.1:8001.
- [`service/`](../service/) also runs the PII guard on every message, the namesake review, corrections, re-verification and timer-driven retention.
- [`fhirmap/`](../fhirmap/) depends on nothing but `cryptography` (ES256/RS256 verification); it also holds `build_correction`, `resolve_conditionals`, the signature verifier (`sig.py`) and the HL7 validator wrapper.
- [`infra/`](../infra/) holds the HAPI interceptor that keeps AuditEvents immutable ([`AppendOnlyInterceptor.java`](../infra/hapi/src/pgk/AppendOnlyInterceptor.java)), the Synapse login limits and idempotent setup.
- [`tests/`](../tests/README.md) has the commands; the load test is in `service/tests/load/`.

### Services used

| Service | Version | Role | Replaceable by |
|---|---|---|---|
| Synapse | `matrixdotorg/synapse:v1.164.1` | Matrix homeserver `klinikum.example` | any Matrix homeserver (Conduit, the PGK server) |
| Element Web | `vectorim/element-web:v1.12.33` | Dr. Hofer's client | any Matrix client |
| HAPI FHIR | `hapiproject/hapi:v8.12.0-2`, R4, H2 on a volume | the clinic's FHIR store | any FHIR R4 server with transactions |
| Mailpit | `axllent/mailpit:v1.32.0` | catches outgoing email | any SMTP relay (`PGK_SMTP_HOST`, `PGK_SMTP_PORT`, `PGK_MAIL_FROM`) |
| HL7 FHIR Validator | `validator_cli.jar` 7.0.2, kept warm as a server | validates every resource before filing | none needed: it is the reference validator |
| tx.fhir.org | public | LOINC/UCUM terminology for the validator; LOINC `$lookup` for codes not in the committed cache (fhirmap sends the code only) | any FHIR terminology server (`PGK_TX_SERVER`, `PGK_TX`) |
| Claude API | `claude-sonnet-5-5` (`PGK_EXTRACTOR_MODEL`) | optional photo intake ([`service/extractor.py`](../service/extractor.py), `PGK_EXTRACTOR=anthropic`, off by default): two independent readings per photo or PDF | a model inside the clinic's EU tenancy |

No other component calls a model: `decide()`, the builders and `prove()` are deterministic.

---

## The filing pipeline (one upload)

1. **Gate** (before the body is read): no `Content-Length` gets 411; over 1.1 MB gets 413, and the stream is drained, never spooled.
2. **Intake** (synchronous):
   - First, the `answer sent` audit entry with the SHA-256 and size of the exact bytes received (never the file name).
   - Then `fhirmap.decide()` against the cached Patient. **BLOCK:** the bytes are dropped; the audit gets only the hash and the rule codes, Dr. Hofer an alert, the sender a page. **Otherwise:** the raw bytes are stored AES-256-GCM encrypted and queued.
3. **Re-check** (worker): `decide()` again against the **live** Patient on HAPI. **HOLD:** the Task goes `on-hold`, and Dr. Hofer gets one decision card. A clinician's "file anyway" can override a HOLD, never a BLOCK.
4. **Build:** the resources and the Provenance with its signed statement ([what it covers](#fhir-resources-and-references)); the Task moves to `completed`. A correction is built as new versions of the report it replaces ([Corrections](#corrections)).
5. **Validate:** the warm HL7 validator, with terminology checks, on every resource including the Task. One error means a hold.
6. **Prove:** `prove()` ([Correctness](#correctness)). Any difference means a hold.
7. **File:** `resolve_conditionals` reuses an existing Practitioner, Organization or PractitionerRole, so duplicates on the server never fail a filing. Then one transaction Bundle (`urn:uuid` references): all or nothing.
8. **Read back:** the filed Provenance and every filed DiagnosticReport, Observation and DocumentReference are fetched from HAPI and verified against the published JWKS ([the signed statement](#the-signed-statement)). The result (`verified`, `bound`, `content_checked`, `changed`) is stored with the filing, with the urn→server id map and the signed target list. The maintenance job repeats this every hour; a filing that no longer verifies is audited, and Dr. Hofer is told which resource changed.
9. **Report:** the `answer filed` AuditEvent and the Matrix card go out through the ordered outbox, and the encrypted copy is deleted.

**One upload hash:** the SHA-256 of the exact bytes received is on the receipt, in the audit and in the Provenance source entity; the attachment holds those bytes with their character set (`text/markdown; charset=windows-1252`). A file that was not UTF-8 is converted for parsing, and the card says so ("Zeichensatz umgewandelt (Windows-1252)").

**Validation fallback:** without the warm validator or tx.fhir.org, the CLI validator runs in `auto` mode (offline if needed); 0 offline errors files, and a background thread re-validates with terminology every 5 min, up to 24 tries, reporting any error to Dr. Hofer. Without HAPI, Java or the validator jar, the answer is held as `held_technical` until Dr. Hofer files it again.

**Restart:** accepted answers not yet filed are re-queued. The 10-minute maintenance job re-sends any AuditEvent whose push failed and reconciles the HAPI Task of every request not yet settled: open ones, and closed ones until their final Task is confirmed on HAPI.

---

## FHIR resources and references

**Task:** created with the request, `draft` while a warning awaits `!ja`, then `requested` → `in-progress` → `completed` / `on-hold` (with `statusReason`) / `rejected` / `cancelled` / `failed`. Writes are monotonic (a sequence number plus `If-Match`): a stale queued update is dropped, a terminal Task never overwritten.

```
Task ─for──────────────────────────────────────────────► Patient/maria-beispiel
 │   requester "Dr. Lena Hofer" (Matrix id), owner = the named person, input: requested type
 │   (LOINC 11502-2 | 11490-0), reasonCode TREAT, restriction.period = link expiry, note = internal note
 └─output──► DiagnosticReport (lab) | DocumentReference (rehab)

DiagnosticReport  code LOINC 11502-2 "Laboratory report", category v2-0074 LAB
 ├─subject────────────────────────────────────────────► Patient
 ├─result[] ──► Observation ×n ─subject───────────────► Patient
 │               code: the document's LOINC (official display) + the lab's own name in code.text
 │               valueQuantity with UCUM code (10*9/L, {INR}, s, g/L …), comparator for "< 0.5"
 │               referenceRange low/high as printed; interpretation ONLY if the lab wrote a flag
 ├─performer ──► Organization (the lab), Practitioner (signatory)
 ├─resultsInterpreter ──► PractitionerRole ─► Practitioner (signatory) + Organization
 ├─basedOn.display = "Einsender laut Dokument: …" when the document names an orderer
 ├─conclusion = the lab's own comment
 └─presentedForm = the original upload, exact bytes (text/markdown; charset=…, Attachment.hash = SHA-1)

DocumentReference  type LOINC 11490-0, category LOINC 18842-5 (the ELGA discharge-letter codes)
 ├─subject ──► Patient    ├─author ──► Practitioner (the "Written by" signatory), Organization
 ├─context.period = the stay   └─content.attachment = the original upload, exact bytes (SHA-1)

Provenance
 ├─target[] ──► every resource of the filing: the content resources, the Practitioners, PractitionerRole,
 │              Organization and the Task
 ├─agent[author]    ──► the document's signatory, with its register lookup   (onBehalfOf: the organisation)
 ├─agent[informant] ──► the eID-verified sender, a Practitioner identified ONLY by bpk-hmac-sha256:
 │                      "ID Austria (SIMULATION, not a real eID; no level of assurance)"
 ├─agent[verifier]  ──► Device "pgk-bridge"   (+ a second verifier, Dr. Hofer, on a "file anyway")
 ├─policy: …/lab-result-v1 | …/discharge-report-v1, …/request-binding-v1, …/eid-simulated,
 │         …/role-register-confirmed-v1 | …/role-asserted-by-requester-v1,
 │         …/author-register-confirmed-v1 | …/author-asserted-by-document-v1, (…/clinician-override-v1)
 ├─reason TREAT (+ the stated reason of a clinician override), activity CREATE, recorded (Vienna offset)
 ├─entity[source].what.identifier = ni:///sha-256;… of the exact bytes received
 ├─entity[revision] ──► the replaced report version (corrections only)
 └─signature: Source Signature (ISO-ASTM E1762 1.2.840.10065.1.12.1.14), sigFormat application/jose.
              An ES256 JWS signed by the service over: the sender (pairwise sub, name), simulated, the request,
              content_sha256 of the original, targets (incl. the Task), resources_sha256 + resource_digests
              of the filed clinical content, and the IdP's own ES256 id_token. Keys: GET /.well-known/jwks.json

AuditEvent (each of the six steps)
 ├─entity[0] ──► the Task (+ request identifier, prev_hash and hash for the FHIR chain)
 ├─entity ──► Patient; for "answer filed", every filed resource
 ├─agent: a stranger (failed eID) appears only by the pairwise id ("pseudonymous person (bPK hash)");
 │        names are redacted from the text; contacts appear only as a keyed hash
 └─type DICOM 110110/110114/110107/110113, subtype from our CodeSystem audit-step, purposeOfEvent TREAT
```

(`…` = `https://pgk-bridge.example/fhir/policy`.)

**Request 1** files a DiagnosticReport; 4 Observations (INR `6301-6` `{INR}`, aPTT `14979-9` `s`, platelets `777-3` `10*9/L`, fibrinogen `3255-7` `g/L`); 2 Practitioners (Eva Brandl as eID sender, Dr. Eva Brandl as signatory); PractitionerRole, Organization, Provenance and the Task. **Request 2** files a DocumentReference with the original text.

### The signed statement

It cannot be moved to another filing or survive an edit. `fhirmap.verify_provenance_signature(provenance, jwks, id_map=…, resources=…)` checks:
- the ES256 signature;
- that `content_sha256` equals the Provenance source hash;
- that `targets` are exactly this Provenance's targets;
- that the clinical content now on the server equals what was signed; if not, `changed` names the resource.

### Corrections

A corrected report or addendum (`Korrektur`, "ersetzt den Befund …", `Nachtrag`) is held as `CORRECTION_REVIEW`, with the report it replaces on the card. On `!ablegen` it is filed as **new versions** of that DiagnosticReport (status `corrected` / `amended`) and of the changed Observations (status `corrected`), each PUT with `If-Match` on the corrected version; added rows become new Observations. The Provenance points to the replaced version (`entity[revision]`). Without a unique earlier report it stays held ("Ursprungsbefund nicht gefunden – bitte manuell prüfen.").

### Times, references, deduplication

- **Times:** every dateTime carries the Europe/Vienna offset of its own date: the 22 Oct 2026 sample is `+02:00`, filings after 25 Oct `+01:00`.
- **References:** within one filing, all references are `urn:uuid` references resolved by the transaction.
- **Deduplication** by stable identifiers, searched before the transaction (`resolve_conditionals` rewrites references to the oldest existing match): the sender Practitioner by its own pairwise bPK hash only; Organization by a slug of the name, plus the GDA-Index id when known; PractitionerRole by person@organisation/role.

---

## Misdelivery handling

The README's three gates, in detail:
- **Before sending:** the look-alike and confusable check compares the address with the register's domains and with the domains of earlier verified answers sent without a warning; a malformed contact is refused.
- **On opening:** nothing is shown before the eID; identity rule v2 and the binding ([SECURITY-LEGAL](SECURITY-LEGAL.md#identity-check)); a protected name needs its PIN; a new identity for a known recipient waits for Dr. Hofer.
- **On upload:** `fhirmap.decide()` evaluates every rule of [`tests/jury/RULES.md`](../tests/jury/RULES.md), and the most severe outcome wins, BLOCK > HOLD > FILE. A BLOCK stores nothing (HAPI, DB, disk) and cannot be overridden. A HOLD keeps the upload encrypted, sets the Task `on-hold` and gives Dr. Hofer one decision, with the near-miss next to her own patient's data.

---

## Correctness

**`prove()`** ([`fhirmap/prove.py`](../fhirmap/prove.py)) compares, between the parsed source and the FHIR resources alone:
- **lab, per row:** test name, value (as an exact decimal), comparator, unit, UCUM code, LOINC, reference low/high/text, flag, notes, effective and issued time, subject;
- **report:** sample and report time, the lab's comment, the patient on `subject.display`, one identical `subject` on every resource, the author's name and role, the organisation;
- **original:** the attachment byte-identical to the upload, its SHA-1 and size, and the Provenance source SHA-256;
- **Provenance:** its targets cover every filed resource;
- an independent count of value rows in the source catches a dropped row.

**Units:** UCUM codes come from an explicit table (`G/L` → `10*9/L`, `T/L` → `10*12/L`, `µmol/L` → `umol/L`, `INR` → `{INR}`) plus a strict prefix-and-atom rule for composite units (`mmol/L`, `ng/mL`). An unknown unit, a missing unit on a dimensioned LOINC, or a unit whose dimension contradicts the LOINC property is held.

**LOINC:** mod-10 check digit verified; displays from LOINC (the committed cache, or tx.fhir.org `$lookup`); an analyte without a code is filed as `code.text` only.

**Rules and corpus:** [`RULES.md`](../tests/jury/RULES.md) is the contract, with how it is checked; the 136 labelled documents (64 FILE / 41 HOLD / 31 BLOCK) are in [`tests/jury/corpus/`](../tests/jury/corpus/), the commands in [`tests/README.md`](../tests/README.md).
- The fuzz generator mutates the name (typo, order, umlaut, homoglyph, superset, other person), the birth date (swap, one digit, ambiguous, other), the identity (a colleague's eID), the sender line and the document type.
- `tests/e2e/run.py` runs the corpus black-box through the live stack (email, eID with PINs, upload, HAPI, Matrix), then checks that blocked documents left no trace in HAPI, the DB, the disk, Matrix or the audit.
- X09, the prompt injection, is blocked as `WRONG_PATIENT` and `OTHER_PATIENT_PRESENT`: no rule reads instructions.
- **Blind documents:** three AI agents that never saw the test set or the code each wrote 30 documents in their own style and ran them live. Genuine documents filed: 7, 12, then 14 of 18 per set, the rest held for one tap; after the parser fixes each set prompted, all three pass offline. One miss, in the third set: a lab comment saying the tube carried another patient's name was filed for Maria. That pattern now blocks. All 90 are a regression set in [`tests/jury/fresh/`](../tests/jury/fresh/).

---

## Load

[`service/tests/load/run_load.py`](../service/tests/load/run_load.py) runs N outside persons answering at once. Per 16 users: 10 correct documents (FILE), 3 for another patient (BLOCK), 3 near-misses (HOLD). It fails if the upload p95 reaches 1 s or any outcome is wrong.

| Concurrent users | Upload p50 / p95 | Time to final outcome p50 / p95 | Outcomes correct |
|---|---|---|---|
| 16 | 0.023 s / 0.029 s | 1.8 s / 2.1 s | all |
| 64 | 0.079 s / 0.099 s | 2.9 s / 4.7 s | all |

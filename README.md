# Befundlink

**The fax is banned for patient data. Its replacement is already in six million pockets.**<sup>[1]</sup>

Dr. Hofer sends the lab one link. They answer with ID Austria, and a minute later the result is in the right patient's chart, as FHIR. The lab installs nothing and signs up for nothing.

**Why now:** since January 2025, health data may no longer be sent by fax in Austria. The only stopgap, encrypted e-mail, ends on 30 June 2027, and Parliament's stated reason for extending it was that the market has no suitable solution.<sup>[2]</sup> Filed by hand, one scanned outside document in 50 ends up in the wrong file, usually the wrong patient's.<sup>[3]</sup>

- **Before sending,** a look-alike address (`labor-kanaI` for `labor-kanal`) is stopped.
- **On opening,** only the person asked gets in. A forwarded link shows nothing, and Dr. Hofer learns who tried.
- **On upload,** another patient's document bounces back to the sender, and Dr. Hofer knows within seconds. A near-miss ("Marie" for Maria) waits for her one tap.

| Tested on the live system | Result |
|---|---|
| 72 trap documents (wrong patient, look-alike names, an injected "file this for Maria") | **0 reached the wrong patient** |
| 64 genuine documents | **64 filed, 0 wrongly held** |
| 90 documents written blind by independent agents, run live | **0 values filed wrong · 18 of 18 wrong-patient documents blocked** |

<sub>[1] Almost 6.3 million ID Austria registrations by 1 Sep 2026 (Bundeskanzleramt). [2] GTelG 2012 § 27 as amended by BGBl. I Nr. 105/2024 and Nr. 67/2026; Initiativantrag 938/A XXVIII. GP: „mangelndes Vorhandensein entsprechender Lösungen am Markt". [3] Centre Léon Bérard, 709,124 scanned documents, ISPOR Europe 2016.</sub>

---

## Results

Measured on the running system by [`tests/metrics.py`](tests/metrics.py); every number with its evidence, response times included, is in [`tests/METRICS.md`](tests/METRICS.md). [Load test](docs/ARCHITECTURE.md#load).

| What the brief measures | Target | Result |
|---|---|---|
| Errors from the official HL7 FHIR validator, on every filed record | 0 | **0** |
| Errors against HL7 Austria AT Core 2.0.0 (patient, practitioners, organisations; the other types have no AT profile) | 0 | **0** ([`tests/atcore.py`](tests/atcore.py)) |
| Installs / accounts the outside person needs | 0 / 0 | **0 / 0** |
| Time for someone new, from opening the email to sending | under 2 min | **46 s** (keystroke model of the five pages; each real time is in `!audit`) |
| Misdeliveries caught: M1 wrong address, M2 forwarded, M3 wrong patient | 3 of 3 | **3 of 3** |
| Steps logged with who, what and when | 6 of 6 | **6 of 6** |
| Patient name, birth date or document type in any email, SMS or link page | 0 | **0** |

---

## Try it

You need Docker with Compose, [uv](https://docs.astral.sh/uv/), Java 21 and GNU `timeout` (macOS: `brew install coreutils`).

```bash
make bootstrap   # first time only: downloads the components, sets up the chat rooms and the two test patients
make demo        # starts the service and the chat bot in demo mode; Ctrl-C stops both
```

Start over with `make demo-reset PULL=never`, then `make demo`.

| Open | Address | Login |
|---|---|---|
| Element: Dr. Hofer's chat | http://localhost:8080 | `lena.hofer` / `hofer-demo-pw`, room "Dr. Hofer – Anforderungen" |
| Mailpit: the outside person's inbox | http://localhost:8025 | none |
| SMS inbox (simulated) | http://localhost:8000/sms | none |

The files to upload are in [`tests/jury/corpus/_scenario/`](tests/jury/corpus/_scenario/). Every login below is a simulated ID Austria persona: given name / family name / PIN.

**Request 1: lab result by email**
1. In Element, post `!labor Brandl maria-beispiel, Text: Gerinnung vor geplantem Eingriff`. The check card shows the patient, Dr. Brandl's register entry and the address check. Reply `!ja`.
2. In Mailpit, open the mail and click the link.
3. Click "Mit ID Austria anmelden", enter **Eva / Brandl / 549657**, upload `lab-result-maria-beispiel.md` and press "Senden".
4. In Element: "📥 Laborbefund abgelegt – Maria Beispiel (12.03.1961)", with the four values, the sender and "4/4 Werte unverändert übernommen".

**Request 2: rehab report by SMS**
1. Post `!reha Kern maria-beispiel`, then `!ja`.
2. Open the SMS inbox, click the link, sign in as **Stefan / Kern / 829022** and upload `rehab-report-maria-beispiel.md`.
3. In Element: "📥 Entlassungsbericht abgelegt", leading with the discharge medication.

**The three misdelivery tests (on request 1)**
- **M1, wrong address:** post `!labor Brandl maria-beispiel eva.brandl@labor-kanaI.example` (capital I). The card warns before sending. Send anyway with `!ja` and sign in as the stranger **Petra / Fremd / 804560**: blocked, and Element shows "[FEHLZUSTELLUNG ABGEFANGEN]" with who tried.
- **M2, forwarded:** send request 1 and sign in as the colleague **Thomas / Huber / 677481**: the same block. Eva Brandl can still answer.
- **M3, wrong patient:** sign in as Eva Brandl and upload `lab-result-max-beispiel.md`. It is discarded at once, and Element raises the alarm.

**More personas:** Eva Maria Brandl `104928` (the namesake test), Maria Beispiel born 1961-03-12 `850449` (the patient herself as recipient), Maria Beispiel born 1990-06-15 `586525` (her daughter of the same name).

**Your own outside person:** `!anfordern labor <Name>, <Rolle>, <Organisation>, <E-Mail oder Mobilnummer>, Patient maria-beispiel`. The send card shows "🔑 Demo: simulierte ID Austria für … – PIN …" to pass on, standing in for their ID Austria app. Patients: `maria-beispiel`, `max-beispiel`.

**A held answer:** upload [`tests/jury/corpus/H03.md`](tests/jury/corpus/H03.md) ("Marie"). Dr. Hofer gets one decision; `!ablegen <id> <Begründung>` files it with her reason. `!hilfe` lists all commands; the [namesake walkthrough and every command](docs/ARCHITECTURE.md#running-and-operating) are in ARCHITECTURE.

**Tamper with the chart:** set the last filed INR to 2.9 directly on the FHIR server, then re-check every filing:
```bash
OBS=$(curl -s "localhost:8090/fhir/Observation?code=http://loinc.org|6301-6&_sort=-_lastUpdated&_count=1&_elements=id" | python3 -c "import json,sys;print(json.load(sys.stdin)['entry'][0]['resource']['id'])")
curl -s localhost:8090/fhir/Observation/$OBS | python3 -c "import json,sys;o=json.load(sys.stdin);o['valueQuantity']['value']=2.9;print(json.dumps(o))" | curl -s -o /dev/null -X PUT -H 'Content-Type: application/fhir+json' --data-binary @- localhost:8090/fhir/Observation/$OBS
make verify-provenance   # names the changed Observation; Element shows "[ACHTUNG] … verändert" in Dr. Hofer's room
```

---

## How it works

```
 Dr. Hofer ── Element ──► Matrix homeserver ◄──► Befundlink bot          (all at the clinic)
                                                   │ request · cards
                                                   ▼
 outside person ── email / SMS link ──►  Befundlink service ─── checks ───►  HAPI FHIR R4
                        ID Austria ──►   address · identity · patient       (the chart: report,
                        upload ──────►   · validator · prove()               values, signed
                                                                              Provenance, audit)
```
[Full diagram](docs/ARCHITECTURE.md#components-and-services) with every port and worker.

| Component we built | What it does |
|---|---|
| [**bot**](bot/) | Matrix bot in Dr. Hofer's room: the request, the check card, send on `!ja`, the result cards. |
| [**service**](service/) | FastAPI: links, address and register checks, the outside person's pages, simulated ID Austria, the filing worker, the keyed audit. |
| [**fhirmap**](fhirmap/) | Pure Python: parser, filing rules (`decide()`), FHIR R4 builders, `prove()`, the signed Provenance and its verifier, the AuditEvent chain. |
| [**infra**](infra/) | docker compose (pinned images, loopback ports), a HAPI interceptor that keeps AuditEvents immutable, setup, smoke test. |
| [**tests**](tests/) | The 136-document corpus and rules, fuzzing, black-box end-to-end runs, a privacy scanner, metrics, load. |

**Services and models, each replaceable:** Synapse and Element Web (any Matrix homeserver, including PGK's), HAPI FHIR R4 (any R4 server with transactions), Mailpit, the HL7 FHIR Validator 7.0.2, tx.fhir.org for terminology, and Claude Sonnet 5.5 for the optional photo intake ([AI and models](#ai-and-models)). [Versions](docs/ARCHITECTURE.md#services-used).

**The filing pipeline, per upload:**
1. **Intake:** size gate; the exact bytes are hashed and audited; `decide()` checks patient, sender and content. A BLOCK drops the bytes.
2. **Re-check:** `decide()` again against the live Patient on HAPI. Any doubt is a HOLD for Dr. Hofer.
3. **Build and validate:** resources plus a signed Provenance; the HL7 validator checks each, with terminology. One error is a hold.
4. **Prove and file:** `prove()` rebuilds the document from the FHIR resources alone and compares value, comparator, unit, LOINC, range, times, patient, author and the original bytes; one difference is a hold. One transaction files all or nothing.
5. **Read back and report:** signature and content are re-verified from HAPI, then hourly, so an edit on the server is caught and named. The audit entry and the Matrix card go out.

[The pipeline in full](docs/ARCHITECTURE.md#the-filing-pipeline-one-upload), with fallback and restart.

---

## Design answers Q1–Q5

### Q1. How does an outside person prove who they are if they are in no directory?

They are already in one: **ID Austria**. A request goes to a **named person**, not an address. Three independent facts are checked:

| Fact | How | When |
|---|---|---|
| They hold the contact | the link reached them by email or SMS | on opening |
| The state says who they are | ID Austria (simulated here) | before anything is shown |
| A register says what they are | ÖÄK Ärzteliste for physicians, GDA-Index for organisations | before sending |

- **Strict name match:** the ID Austria family name must be the requested one and the given names must fit. If the patient herself is the recipient, her birth date must match too.
- **The link binds to one person:** the first matching identity owns it. Anyone else is blocked, audited and named to Dr. Hofer; the link stays valid for the right person.
- **A known recipient's new identity waits:** once Eva Brandl has answered, a different "Eva Maria Brandl" sees nothing until Dr. Hofer confirms with `!identität`.
- **Nothing before sign-in:** the link page shows only the clinic and Dr. Hofer, and a mail scanner opening it changes nothing. [Token and cookie mechanics](docs/SECURITY-LEGAL.md#identity-check).

### Q2. Which identity check do you require for each type of document and why?

The **ID Austria bar is the same for both**: the data is equally sensitive and lands in the same chart. The register check follows where the clinical authority sits.

| Document | Typical sender | Register check before sending | Why |
|---|---|---|---|
| Lab result | the lab physician | the **person** in the ÖÄK Ärzteliste, with a lab-medicine specialty | A lab value counts because a physician released it, and the INR decides whether the procedure goes ahead. |
| Rehab report | the discharge manager, not the author | the **organisation** in the GDA-Index, with the person in their role | Discharge managers are in no professional register; the report counts by its author and institution. |

- No register match: Dr. Hofer must acknowledge the warning, and the Provenance records "role asserted by requester" instead of "register-confirmed".
- No SMS code: ID Austria is already two-factor. The document's author and the ID Austria sender stay separate Practitioners.

### Q3. Which FHIR resources do you create and how do they reference each other and the patient?

```
Task (the request) ──for──► Patient/maria-beispiel ◄──subject── every content resource
 └─output──► DiagnosticReport (lab, LOINC 11502-2)       or   DocumentReference (rehab, LOINC 11490-0)
              ├─result ──► Observation ×n                      ├─author ──► Practitioner, Organization
              │   LOINC from the document, UCUM value,         ├─context.period = the stay
              │   reference range as printed                   └─content = the original, exact bytes
              ├─performer, resultsInterpreter ──► Practitioner, PractitionerRole, Organization
              └─presentedForm = the original, exact bytes
Provenance ──target──► every filed resource, including the Task
 ├─agent: author = the document's signatory · informant = the ID Austria sender · verifier = Befundlink
 ├─entity[source] = SHA-256 of the exact bytes received
 └─signature = signed statement over sender, request, original hash, targets and filed content
AuditEvent ×6 steps ──► Task, Patient (and every filed resource on "answer filed")
```

The Patient is never changed; a correction becomes a new version. [Full tree](docs/ARCHITECTURE.md#fhir-resources-and-references).

### Q4. How do you notice a misdelivery?

**Three gates, the first before anything is sent.**

| Gate | Check | Catches |
|---|---|---|
| Before sending | look-alike address vs known domains, register check, the patient's name and birth date shown for the id Dr. Hofer typed | **M1**, wrong person, typos, a mistyped patient |
| On opening | the ID Austria identity must match the named person; the link binds to that person | **M1**, **M2** (forward), a namesake |
| On upload | `decide()` against the requested Patient and the request | **M3** and near-misses |

- **BLOCK** on evidence of another person or an attack: nothing is filed or stored, and Dr. Hofer's alert never names the other patient.
- **HOLD** on a near-miss (umlaut, swapped day and month, extra given name, unknown unit): one decision for Dr. Hofer.
- A false FILE puts another person's INR into Maria's chart the day before her procedure; a false HOLD costs one click. [Every rule](tests/jury/RULES.md).

### Q5. Where does your service run: at the clinic, at the outside organisation or at a third party?

**At the clinic, next to its own Matrix homeserver**, as part of the clinic's docker compose stack ([`infra/docker-compose.yml`](infra/docker-compose.yml)).

- The clinic is the requesting healthcare provider and the controller of the record, so no new controller appears ([what is stored where, and for how long](docs/SECURITY-LEGAL.md#what-is-stored-where-and-for-how-long)).
- The outside party discloses directly to the requester, as with fax today, but verified.
- Not at the outside organisations: there are thousands of them.
- **FTAPI SubmitBox as the front end:** every check runs after the upload, so in production SubmitBox can take the file in and Befundlink decides what is filed.
- **Across PGK:** every member clinic runs the same stack next to its own homeserver. The registers are the national ones (ÖÄK Ärzteliste, GDA-Index), so one lab answers every clinic with the same ID Austria.
- **ELGA and EHDS:** filed results carry ELGA's LOINC codes as FHIR R4; the EHDS (Regulation (EU) 2025/327) requires lab results and discharge reports in a European exchange format from 26 March 2031.
- **Market and model:** 144 acute hospitals (2.1 million stays a year) receive from 81 rehab hospitals, 60+ licensed lab clinics and about 10,300 ÖGK contract posts (BMASGPK register, Sep 2026; Statistik Austria, Oct 2026; ÖGK, Aug 2026). Clinics license Befundlink per site; senders pay nothing. Hosting can sit with an Austrian telco or cloud provider as an Art. 28 processor. **Pilot:** one PGK clinic and its outside labs, before the e-mail stopgap ends.

---

## AI and models

**AI attacked it; rules decide; the doctor has the last word.**

- **AI as the attacker.** AI agents wrote the trap documents. All 136 outcomes match their labels, and 2,000 generated variants match an independent reference of the rules.
- **Photo intake: AI reads, rules decide.** A photo or PDF is read in two separate model calls and goes on only if both readings agree; any difference is one decision for Dr. Hofer ("KI-Lesungen weichen ab: INR 1.0 vs 1.8 – bitte am Original prüfen"). **27 photos and PDFs rendered from test documents, 3 of them degraded: 20 filed identical to the source, 3 held, 4 wrong-patient blocked, 0 filed wrong** ([`tests/extract_eval.py`](tests/extract_eval.py)).
- **The proof gate.** Whatever was read still passes the patient rules, the HL7 validator and `prove()`: **2,352 of 2,352** corrupted builds of 17 kinds caught ([`tests/prove_gate.py`](tests/prove_gate.py)).
- **Deterministic where a mistake reaches the chart.** Every filing decision is a named rule in [`RULES.md`](tests/jury/RULES.md): the same upload always gives the same outcome, explainable to the doctor and the auditor.

---

## Correctness

- **Units are mapped, never converted:** UCUM codes come from an explicit table. An unknown unit, or one that contradicts the LOINC code, is held.
- **Nothing is invented:** no derived high/low flags, LOINC check digits verified, and a test without a code is filed as text, never guessed.
- **Blind documents:** three AI agents that never saw our test set or code each wrote 30 documents in their own style and ran them live. Genuine documents filed: 7, 12, then 14 of 18 per set, the rest held for one tap; after the parser fixes each set prompted, all three pass offline. One miss, in the third set: a lab comment saying the tube carried another patient's name was filed for Maria. That pattern now blocks. All 90 are a regression set in [`tests/jury/fresh/`](tests/jury/fresh/).

---

## Legal

- **Legal basis:** GDPR Art. 9(2)(h), provision of health care under professional secrecy (Art. 9(3)), not consent. Every Task and AuditEvent carries the purpose TREAT.
- **Disclosure:** the outside physician is bound by § 54 ÄrzteG. After ID Austria, the service shows them who asks, for whom and what; they decide to answer or decline. The service never pulls data.
- **GTelG 2012:** patient identity (§ 4) by the name and birth-date match, sender identity (§ 4) by ID Austria plus the GDA-Index or Ärzteliste, role (§ 5) by the GDA-Index entry, confidentiality (§ 6) by messages that carry no health data, integrity (§ 7) by the signed Provenance and the anchored audit, sealed with the clinic's eIDAS seal in production.
- **Medical device:** communication software under MDCG 2019-11 rev.1: it transports, verifies and files what the sender sent, and computes no clinical value or flag.

[GTelG table, retention, security](docs/SECURITY-LEGAL.md).

---

## What's simulated, and what plugs in for production

| Today | Production |
|---|---|
| Simulated ID Austria (signed token, PINs) | Real ID Austria via OIDC, with the health-sector bPK; EU senders via eIDAS. Matching and binding stay. |
| Register fixtures | ÖÄK Ärzteliste and the eHealth-Verzeichnisdienst / GDA-Index behind the same `Registers` interface |
| SMS inbox page | any SMS gateway behind the same send call |
| Mailpit | the clinic's SMTP relay |
| Provenance signed with the service's key | the clinic's qualified eIDAS seal (GTelG § 7) |
| HAPI on loopback, no login | SMART on FHIR / OAuth2 or mTLS: only the service writes |
| HTTP on localhost | TLS on the clinic's domain |
| A first-time recipient binds on the first matching sign-in | optional `!identität` confirmation of every first identity |
| The outside person decides on disclosure | a request field that records the patient's release (§ 54 ÄrzteG), filed in the Task |
| Markdown and text uploads, as in the brief; photo intake via the Claude API on synthetic documents only, off by default (`PGK_EXTRACTOR=anthropic`) | the same double reading by a model inside the clinic's EU tenancy, as an Art. 28 processor |
| Wrong filings retracted, and the audit rotated, by an operator | a bot command for `entered-in-error`, and automated rotation |

[Detail per piece](docs/SECURITY-LEGAL.md#production-plan-detail).

---

All data is synthetic.

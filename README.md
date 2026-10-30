# Befundlink

**The fax is banned for patient data. Its replacement is already in six million pockets.**<sup>[1]</sup>

Dr. Hofer sends the lab one link. They answer with ID Austria, and seconds later the result is in the right patient's chart, as FHIR. The lab installs nothing and signs up for nothing.

**Why now:** the fax ban took effect in January 2025, and the e-mail stopgap ends on 30 June 2027 because, in Parliament's words, the market has no suitable solution.<sup>[2]</sup> Filed by hand, one outside document in 50 lands in the wrong file, usually another patient's.<sup>[3]</sup>

- **Before sending,** a look-alike address (`labor-kanaI` for `labor-kanal`) is stopped.
- **On opening,** only the person asked gets in. A forwarded link shows nothing, and Dr. Hofer learns who tried.
- **On upload,** another patient's document bounces back before it is stored. A near-miss ("Marie" for Maria) waits for Dr. Hofer's decision.

| Tested on the live system | Result |
|---|---|
| 72 trap documents: wrong patient, look-alike names, an injected "file this for Maria" | **0 reached the wrong patient** |
| 64 genuine documents | **64 filed** |

<sub>[1] Almost 6.3 million ID Austria registrations by 1 Sep 2026 (Bundeskanzleramt). [2] GTelG 2012 § 27, BGBl. I Nr. 105/2024 and Nr. 67/2026; Initiativantrag 938/A XXVIII. GP. [3] Centre Léon Bérard, 709,124 scanned documents, ISPOR Europe 2016.</sub>

---

## Results

| What the brief measures | Target | Result |
|---|---|---|
| HL7 FHIR validator errors, every filed record | 0 | **0** |
| Installs / accounts for the outside person | 0 / 0 | **0 / 0** |
| Time from opening the email to sending | under 2 min | **46 s** (modelled) |
| Misdeliveries caught: M1 wrong address, M2 forwarded, M3 wrong patient | 3 of 3 | **3 of 3** |
| Logged with who, what and when: request sent, link opened, identity checked, answer sent, answer filed, misdelivery caught | 6 of 6 | **6 of 6** |

Source: [`tests/metrics.py`](tests/metrics.py) · [`tests/METRICS.md`](tests/METRICS.md)

---

## Try it

Needs Docker with Compose, [uv](https://docs.astral.sh/uv/), Java 21 and GNU `timeout` (macOS: `brew install coreutils`).

```bash
make bootstrap   # first time only
make demo        # Ctrl-C stops it
```

| Open | Address | Login |
|---|---|---|
| Element: Dr. Hofer's chat | http://localhost:8080 | `lena.hofer` / `hofer-demo-pw` |
| Mailpit: the lab's inbox | http://localhost:8025 | none |

1. In Element, room "Dr. Hofer – Anforderungen", post `!labor Brandl maria-beispiel`, then `!ja`.
2. In Mailpit, open the mail and click the link. Sign in with ID Austria (simulated) as **Eva / Brandl / 549657** and upload [`lab-result-maria-beispiel.md`](tests/jury/corpus/_scenario/lab-result-maria-beispiel.md).
3. Element: "**Abgelegt** · Laborbefund · Maria Beispiel · 12.03.1961", with the four values and the sender.

**The misdeliveries:**
- **M1:** post `!labor Brandl maria-beispiel eva.brandl@labor-kanaI.example` (capital I). The card warns before sending; send anyway and sign in as **Petra / Fremd / 804560**: blocked.
- **M2:** sign in to a fresh link as the colleague **Thomas / Huber / 677481**. Blocked; Element names him.
- **M3:** sign in as Eva Brandl and upload `lab-result-max-beispiel.md`. It bounces, and Element raises the alarm.

[More of the demo](docs/ARCHITECTURE.md#more-of-the-demo): the rehab report by SMS, a held answer, your own outside person, tampering with the chart.

---

## How it works

```
 Dr. Hofer ── Element ──► Matrix homeserver ◄──► Befundlink bot          (all at the clinic)
                                                   │ request · cards
                                                   ▼
 outside person ── email / SMS link ──►  Befundlink service ─── checks ───►  HAPI FHIR R4
                        ID Austria ──►   address · identity · patient       (the chart: report,
                        upload ──────►   · validator · proof                 values, signed
                                                                              Provenance, audit)
```

| We built | |
|---|---|
| [bot](bot/) | Matrix bot: the request, the check card, the result cards |
| [service](service/) | FastAPI: links, checks, the outside person's pages, simulated ID Austria, filing, audit |
| [fhirmap](fhirmap/) | parser, filing rules, FHIR builders, `prove()`, signed Provenance |
| [tests](tests/) | 136 labelled documents, fuzzing, end-to-end runs, metrics |

**Services and models:** Synapse and Element (Matrix), HAPI FHIR R4, the HL7 FHIR Validator 7.0.2, tx.fhir.org for terminology, Mailpit, and Claude Sonnet 5.5 for the optional photo intake. Each is replaceable: [versions](docs/ARCHITECTURE.md#services-used).

**Per upload:**
1. The patient rules decide: file, hold for Dr. Hofer, or block.
2. The HL7 validator checks every resource.
3. `prove()` rebuilds the document from the FHIR alone; it must match exactly.
4. One transaction files all or nothing. Hourly re-checks name any value changed later.

[Full pipeline](docs/ARCHITECTURE.md#the-filing-pipeline-one-upload).

---

## Q1–Q5

**Q1. How does an outside person prove who they are if they are in no directory?**
They are already in one: ID Austria. The request goes to a named person; the link reached them by email or SMS, ID Austria says who they are, and a register says what they are. The link then binds to that one person. [Details](docs/SECURITY-LEGAL.md#identity-check).

**Q2. Which identity check per document type, and why?**
ID Austria for both: the data is equally sensitive. A lab result is valid because a physician released it, so the physician is checked in the Ärzteliste. A rehab report is sent by a discharge manager, who is in no register, so the organisation is checked in the GDA-Index.

**Q3. Which FHIR resources, and how are they linked?**
```
Task (the request) ──for──► Patient ◄──subject── every content resource
 └─output──► DiagnosticReport (lab) ──result──► Observation ×n   or   DocumentReference (rehab)
Provenance ──target──► every filed resource · signed statement over sender, original bytes and content
AuditEvent ×6 steps ──► Task, Patient
```
[Full tree](docs/ARCHITECTURE.md#fhir-resources-and-references).

**Q4. How do you notice a misdelivery?**
The three gates at the top. Evidence of another person blocks, and nothing is stored; a near-miss waits for one decision. [Every rule](tests/jury/RULES.md).

**Q5. Where does the service run?**
At the clinic, next to its own Matrix homeserver, so the clinic stays the only controller of the record. FTAPI SubmitBox can be the upload front door. Clinics license Befundlink per site; senders pay nothing.

---

## AI

- **AI wrote the attacks,** including 90 documents written blind. Rules, not AI, decide every filing.
- **Photo intake (optional):** two separate Claude Sonnet 5.5 readings must agree, then the same rules, validator and `prove()` apply. On 27 test photos: 0 filed wrong.

---

## Simulated today

ID Austria, the registers, the SMS gateway and the eIDAS seal, each behind its production interface. [Full table](docs/SECURITY-LEGAL.md#production-plan-detail) · [legal basis](docs/SECURITY-LEGAL.md#legal-and-compliance).

All data is synthetic.

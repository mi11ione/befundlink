# Befundlink: identity, security and legal

The detail behind the [README](../README.md)'s Q1, Q2 and Legal sections: the identity mechanics, the security controls, what is stored where and for how long, the legal mapping including GTelG 2012, and the production plan. The pipeline and the FHIR tree are in [ARCHITECTURE.md](ARCHITECTURE.md).

- [Identity check](#identity-check)
- [Security](#security)
- [What is stored where, and for how long](#what-is-stored-where-and-for-how-long)
- [Legal and compliance](#legal-and-compliance)
- [Production plan: detail](#production-plan-detail)

---

## Identity check

**Identity rule v2** (`fhirmap.identity_matches`, binary, no fuzziness):
- Names are normalised as for patients ([RULES.md](../tests/jury/RULES.md#patient-name-same--near--different)), with hyphens splitting tokens; the requested name must contain a given and a family name.
- **Family exact:** the eID family name must appear in the requested name, so an eID "Brandl-Huber" does not match "Brandl".
- **Given ⊆:** every other requested token must be an eID given name. An eID "Eva Maria" or "Eva-Maria" matches the requested "Eva"; a requested "Eva Maria" does not match an eID "Eva".
- **Order-free:** a request typed "Brandl Eva" works.
- **Patient as recipient:** the eID birth date must equal `Patient.birthDate`, which stops a same-named daughter.

**Binding:**
- The first matching eID binds the link atomically by its pairwise subject. Any other eID, a namesake included, is denied (`forward`), audited and named to Dr. Hofer. The link stays valid for the named person and locks after 5 failed checks.
- **Namesake rule:** a known recipient (the same normalised name and organisation as an earlier filed answer) who signs in with a different ID Austria identity is held before anything is shown, until Dr. Hofer confirms with `!identität`. Only the new identity waits.
- The email tells any other reader that forwarding does not help, and to call instead.

**The simulated ID Austria models what makes the real one trustworthy** ([`service/eid.py`](../service/eid.py)):
- **No impersonation:** a name that would pass as a protected person (any persona, anyone in the registers, the named recipient of any open request) needs that persona's PIN; free entry works only for new, unprotected names. PIN failures are throttled per client and claimed identity: 10 per 10 minutes.
- **Signed ID token:** ES256 (P-256), public key at `/eid/.well-known/jwks.json`. `aud` = SHA-256 of this link's token, `exp` 300 s, a random `nonce` and a single-use `jti` (stored atomically; a replay is refused). Handed back by an auto-submitted **POST**, never in a URL.
- **Minimal claims:** `sub` = a salted pairwise hash of the bPK (`bpk-hmac:…`), given and family name, `simulated: true`. **No clear bPK and no birth date in the token.** The birth date stays in the server-side session, serves only the patient-as-recipient rule (eID check, upload, re-check) and is deleted with the session row.
- **Demo mode** never lists personas of open recipients or patients of open requests.

**Link and session:**
- The link token is 60 random characters from 19 consonants (about 254 bits), so it can never spell a name, a date or a word. Only its SHA-256 is stored.
- The landing page names the clinic, the requesting doctor, her department and the callback number. GET never consumes or binds the link.
- The session cookie lasts 60 minutes (`Max-Age=3600`, HttpOnly, SameSite=Lax, Secure under HTTPS) and is path-scoped to the link. Every form POST carries a CSRF token. An upload sent after the session expired is kept encrypted for 60 minutes and sent after the person signs in again.

**Register result (Q2):**
- **No register match:** Dr. Hofer sends only with `!ja` on the card listing every warning; the API (`POST /api/requests` and `…/confirm`) requires the explicit list of acknowledged warning codes. The Provenance carries `…/policy/role-asserted-by-requester-v1`, and the card reads "Rolle vom Anfordernden angegeben (nicht im Register)". A confirmed match gives `…/role-register-confirmed-v1`.
- **The document's signatory** is looked up too: `author-register-confirmed-v1` or `author-asserted-by-document-v1`. A register entry found by name is recorded only as policy and agent text ("Eintrag gleichen Namens gefunden (Name-Abgleich, nicht an die eID gebunden)"), never as a Practitioner identifier.

**No SMS OTP:** ID Austria is already two-factor (a registered device plus a PIN or biometric). An OTP to the same phone adds nothing, and shared ward phones make SMS a possession signal, not an identity.

---

## Security

- **Network:** every port (Synapse, Element, HAPI, Mailpit, the service, the bot's `/notify`, the warm validator) binds to `127.0.0.1`; `HOST` overrides it for the service only. HAPI allows no cross-origin access. Synapse rate-limits failed logins (burst of 3 per account).
- **Fail closed:** the service refuses to start without `PGK_API_KEY`, a `PGK_SECRET` of at least 32 bytes and a `PGK_BPK_SALT` of at least 16 characters (except `PGK_DEV=1`, for tests). `make run` generates them into the gitignored `.env`.
- **Per-purpose keys:** HKDF-SHA256 from `PGK_SECRET` for `csrf`, `pin`, `audit-chain` and `contact`, so one purpose's key never yields another's.
- **Authorisation:**
  - The API needs `X-API-Key`, compared in constant time.
  - Each request is visible and changeable only for its requester, in its room: every call that reads or changes one, and `POST /api/requests` including the dry run that shows the patient's name and birth date, needs `X-PGK-Actor` and `X-PGK-Room`. The API key alone cannot create requests or look up patients.
  - Operator routes (whole audit trail, verify, sync) take the API key alone and refuse actor headers.
  - The SMS inbox needs the API key, or `PGK_DEMO=1` from localhost. The bot accepts commands and invites only from PGK directory members; its `/notify` needs `X-PGK-Notify-Key`, accepts JSON only and posts only to known rooms.
- **Inputs:** ids are validated against fixed patterns; upload size is enforced before spooling.
- **Pages:** `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and a strict CSP without inline script.
- **Files:** the database and key files are created 0600, and `service/data` is 0700.
- **Logs:** a redaction filter on every handler of the root, `pgk` and `uvicorn` loggers and on Python's `lastResort` handler removes link tokens, eID assertions and JWS from every line, the access log included. The bot logs the sender and command name only. Validator output is discarded, since resources carry personal data.
- **Validator process:** the service talks only to a validator process it started itself.

### Keyed and anchored audit

- The SQLite audit rejects UPDATE and DELETE by triggers, and so do the stored copy of each AuditEvent and the anchor table; the contact side table rejects UPDATE and is purged only at link expiry.
- Each entry's hash is an HMAC-SHA256 over the entry and its predecessor's hash, keyed with HKDF(`PGK_SECRET`, "audit-chain"). An unkeyed row after keyed ones counts as a downgrade.
- Every 10 minutes the chain head is **anchored** outside the service: as a bot message in the separate audit room "Befundlink Audit", and as an AuditEvent on HAPI. Each anchor carries an HMAC under the audit key; an anchor without a valid MAC is reported as "ignored (unauthenticated)", never as tampering.
- `make audit-verify` reads the anchors back **from HAPI (search) and from the audit room's history**, not from the local table. A rewrite before the last anchor fails it even when the rewriter holds the key and the database ("hapi anchor at #N does not match the chain", and the same for the Matrix anchor). Entries after the last anchor (at most 10 minutes) are protected by the keyed chain.
- The in-app check (`!audit`, `/api/audit`) verifies the keyed chain and the local anchors; the tamper test in `tests/metrics.py` shows edits and deletions are detected.
- On HAPI, every DELETE is refused (HAPI-0966) and `$expunge` is disabled (HAPI-0968). `pgk.AppendOnlyInterceptor` rejects any update to an AuditEvent, also as PATCH and inside a transaction (405). Other resources can get new versions; every earlier version stays readable in `_history`.

---

## What is stored where, and for how long

Every period is in [`service/retention.py`](../service/retention.py), enforced by a timer job every 10 min.

| Store | What | Personal data | Kept |
|---|---|---|---|
| HAPI FHIR (clinic) | filed resources incl. the original document, Task (incl. the internal note), AuditEvents, audit anchors | the record itself | the clinic's record retention (hospitals: at least 30 years, § 10 Abs. 1 Z 3 KAKuG); versioned, nothing deletable, AuditEvents immutable |
| Service SQLite: requests | recipient name/role/organisation, contact, patient **id**, document type, status, register result, request text, SHA-256 of the link token | the patient's id (pseudonymous); **never her name or DOB** (in-memory Patient cache, 10 min) | contact and note deleted at link expiry; request text deleted at filing or link expiry |
| Service SQLite: answers | per answer: sender name, pairwise subject, IdP token, SHA-256, size, outcome | the sender's data | with the request |
| Service SQLite: uploads | **held** uploads, AES-256-GCM encrypted (key in `service/data/.datakey`, or `PGK_DATA_KEY`) | yes | deleted on filing, discard or cancel; at most 7 days |
| | blocked uploads | — | **never stored**, only SHA-256 and size; file names are never stored |
| Service SQLite: identity | sessions (pairwise subject, eID name and DOB, the IdP token), used token ids | the sender's eID data | sessions usable 60 min, rows deleted 24 h after expiry; token ids 24 h |
| Service SQLite: recipient identities | recipient name + organisation → pairwise subjects of filed answers, first answer, birth **year** | pseudonymous | kept (namesake rule) |
| Service SQLite: audit | every step, cleartext who (incl. strangers who failed the eID), IP address and user agent of the outside person, patient id, document type; contacts only as a keyed hash; HMAC-keyed chain | names, IPs, contact hashes; no clinical values | append-only; target 1 year (`SERVICE_AUDIT_DAYS`) by operator rotation |
| Service SQLite: audit contacts | the cleartext contact of `request sent` / `misdelivery caught`, beside the chain | the contact | deleted at link expiry |
| Service SQLite: SMS outbox (simulation) | the SMS text (no patient data) | the phone number | until the link expires or the request closes |
| Email / SMS (Mailpit, `/sms`) | the link, the clinic's name and callback number, the requesting doctor and her department, the reference, the expiry date | **none** about the patient, the document or the recipient | the provider's |
| Matrix rooms (clinic) | Dr. Hofer's room: filed card (patient, values, sender), hold cards, alerts (the name of whoever tried, the masked contact). The audit room: audit anchors | yes, inside the clinic network; the demo rooms are not end-to-end encrypted | the homeserver's |

---

## Legal and compliance

- **Legal basis (patient data):** health data (GDPR Art. 4(15), Art. 9(1)). The basis is **Art. 9(2)(h)** (provision of health care) under professional secrecy (**Art. 9(3)**), not consent. Every Task carries `reasonCode` TREAT, every AuditEvent `purposeOfEvent` TREAT.
- **Who may disclose:**
  - The outside physician is bound by **§ 54 Abs. 1 ÄrzteG 1998**, a rehab clinic's staff by § 9 KAKuG.
  - § 54 Abs. 2 has no general exception for continuity of care, so the disclosure rests on the patient's release from secrecy (**§ 54 Abs. 2 Z 3**), given expressly or in the referral and treatment context, or on a specific statutory duty.
  - That decision stays with the outside person, who can also decline ("Kein passender Befund vorhanden / nicht zuständig").
- **Legal basis (outside persons and strangers):**
  - The sender's eID name, pairwise id, IP address, user agent and contact are processed to prove who sent what, as GTelG 2012 § 4 requires, and for security (GDPR Art. 6(1)(c) and (f), Art. 32).
  - A stranger who fails the eID check is kept in cleartext only in the service audit (1 year) and in Dr. Hofer's alert. In the patient's FHIR AuditEvents, a stranger appears only by the pairwise id.
- **Transparency:** the landing page names the controller (Klinikum Beispielstadt) and a callback number. Production adds a link to the clinic's **Art. 13** notice for the outside person; patient information follows the clinic's existing notice (Art. 13/14).
- **DPIA and records:** large-scale processing of Art. 9 data with a new identity flow, so the clinic carries out a **DPIA** (Art. 35(3)(b)) before production and lists the processing in its Art. 30 records. This document, [ARCHITECTURE.md](ARCHITECTURE.md) and the retention table are the technical input.
- **GTelG 2012, 2nd section** (Datensicherheit bei elektronischer Weitergabe, §§ 3–8a; it binds the GDAs):

  | § | Requirement | Mechanism |
  |---|---|---|
  | § 4 Abs. 1, 2, 5 | Identity of the **patient** whose data is sent, stored with unique electronic identifiers (E-GovG § 8) | The answer is filed only against the requested Patient, after the document's name and DOB match it (`fhirmap.decide`). Production: the Patient carries the clinic's bPK-GH from its patient index (§ 4 Abs. 3/5). |
  | § 4 Abs. 4 | Identity of the sending **GDA**: qualified-certificate signature plus bPK (Z 1), eHealth-Verzeichnisdienst (Z 2) or GDA-Index (Z 3) | Rehab: the GDA-Index match of the organisation (Z 3, MOCK adapter here). Lab: the physician's ÖÄK Ärzteliste entry; production adds the lab's eHVD / GDA-Index entry. On top of it, ID Austria identifies the natural person who sends. § 4 Abs. 6 lets a GDA obtain bPK, so production uses the health-sector bPK from ID Austria. |
  | § 4 Abs. 4 | Identity of the requesting GDA | Klinikum Beispielstadt is the GDA (GDA-Index). Dr. Hofer signs in to the clinic's own Matrix homeserver and is a member of the PGK directory. |
  | § 5 | Role, proved per § 4 Abs. 4; roles fixed by regulation (§ 5 Abs. 2) | The GDA-Index / eHVD role of the sending organisation (MOCK), recorded as `role-register-confirmed-v1` or `role-asserted-by-requester-v1`. The ÖÄK Ärzteliste check (lab specialty, § 27 ÄrzteG) is an additional professional check on the person, not the § 5 proof. |
  | § 6 | Confidentiality: secured network or full encryption by protocol | Production: TLS on the clinic's domain (§ 6 Abs. 1 Z 2). Messages that travel outside carry no health data. The demo runs on localhost over HTTP. |
  | § 7 | Integrity: advanced or qualified electronic signature or seal (eIDAS) | Demo: SHA-1 on the attachment, SHA-256 in the Provenance, an ES256 statement over the original and the filed clinical content (re-verified hourly), and a keyed, externally anchored audit chain. Production: the clinic's qualified eIDAS seal. |
  | § 8 | Documented IT security concept of the GDA | The clinic's concept covers this service. This repository supplies its technical part (the Security section, the retention table, [ARCHITECTURE.md](ARCHITECTURE.md)). |

- **Data minimisation (Art. 5(1)(c)):** messages carry no patient name, no DOB in any of 15 renderings, no document-type word (a curated lexicon plus the content words of the recipient's role and organisation), no patient id, no recipient organisation and **no recipient name**, since the recipient can be the patient herself. Every message is scanned before sending and refused if anything is found. The free-text note never leaves the clinic.
- **Rectification and erasure:** nothing filed is deleted; a correction is a new version. A wrong filing (for example one filed on "file anyway") is retracted by a new version with status `entered-in-error` on the DiagnosticReport, Observations or DocumentReference, a Provenance with activity UPDATE naming the clinician, and an AuditEvent; in the prototype an operator does this with a FHIR PUT. This meets the right to rectification (Art. 16). Erasure (Art. 17) is limited by the statutory retention duty (Art. 17(3)(b)).
- **Wrong-patient privacy:** in M3 the file is discarded and the AuditEvent holds only its hash. The sender is told on the page, so they can assess their own disclosure.
- **Medical device:**
  - The service transports, verifies and files. It computes no flags and derives no values (units are only mapped to UCUM codes, never converted); the optional photo intake only transcribes, and two readings must agree. Patient matching and holds decide only **whether** a document is filed, never what it means clinically.
  - That makes it communication and storage software, not medical device or IVD software under **MDCG 2019-11 rev.1 (June 2025)**, which covers both MDR 2017/745 and IVDR 2017/746.

---

## Production plan: detail

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

The pieces that need more:

- **ID Austria:** today [`service/eid.py`](../service/eid.py), whose Provenance says SIMULATION and uses `…/policy/eid-simulated`, never an eIDAS level of assurance. In production the clinic registers as an ID Austria service provider, and the IdP-signed ID token is verified against ID Austria's keys; the bound Provenance statement stays.
- **FHIR server access:** the demo HAPI has no authentication beyond the [network limits](#security). A new version PUT by anyone on the host fails the hourly re-verification, which names the resource and alerts Dr. Hofer. AuditEvents created on the host do appear in the patient's audit search; the verifier ignores anchors without a valid MAC.
- **Service-audit rotation:** after `SERVICE_AUDIT_DAYS` (365) an operator exports the segment, anchors its head and starts the next one.
- **Patients:** any Patient on HAPI can be requested by id; `maria-beispiel` and `max-beispiel` are set up.

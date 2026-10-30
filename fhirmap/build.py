"""Parsed answer -> FHIR R4 resources (+ Provenance), Task, transaction Bundle, AuditEvent.

Only standard R4 elements; no extensions. References between the resources of
one filing are ``urn:uuid:<id>`` so the set is posted as ONE transaction
(``build_transaction``): all of it is filed or none of it.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import re
import unicodedata
import urllib.parse
import uuid
from datetime import datetime, timezone

from . import loinc as _loinc
from .identity import VIENNA, compare_names, fhir_instant, loose_name_equal, split_name
from .parse import DISCHARGE, LAB, Parsed, _fold

BASE = "https://pgk-bridge.example/fhir"
SID_REQUEST = f"{BASE}/sid/request-id"
SID_BPK = f"{BASE}/sid/bpk-hmac-sha256"        # keyed HMAC-SHA-256 of the sector bPK (GH), never the clear bPK
SID_BPK_LEGACY = f"{BASE}/sid/bpk-gh-sha256"   # former name of the same identifier (read/search only)
SID_SERVICE = f"{BASE}/sid/service"
SID_MXID = f"{BASE}/sid/matrix-id"
CS_AUDIT_STEP = f"{BASE}/CodeSystem/audit-step"
SID_ORG = f"{BASE}/org-id"                       # stable Organization id: slug of the normalised name
SID_GDA = f"{BASE}/sid/gda-index"                # GDA-Index / eHealth directory id, when known
SID_AERZTELISTE = f"{BASE}/sid/aerzteliste"      # ÖÄK Ärzteliste id: NOT written to Practitioner (name match only)
SID_PRACTITIONER_ROLE = f"{BASE}/sid/practitioner-role"
# the document's signatory AS NAMED IN THE DOCUMENT: '<normalised name>@<organisation slug>'. A business key
# (one Practitioner per stated name and organisation), not a person id: never a register id, never merged
# with the eID-verified sender (bpk-hmac).
SID_SIGNATORY = f"{BASE}/sid/document-signatory"

# ONE policy URI scheme: https://pgk-bridge.example/fhir/policy/<name>
POLICY_BASE = f"{BASE}/policy"
POLICY_LAB_RESULT = f"{POLICY_BASE}/lab-result-v1"
POLICY_DISCHARGE_REPORT = f"{POLICY_BASE}/discharge-report-v1"
POLICY_OTHER = f"{POLICY_BASE}/other-v1"
POLICY = {LAB: POLICY_LAB_RESULT, DISCHARGE: POLICY_DISCHARGE_REPORT}
POLICY_REQUEST_BINDING = f"{POLICY_BASE}/request-binding-v1"
POLICY_ROLE_REGISTER = f"{POLICY_BASE}/role-register-confirmed-v1"        # sender's role confirmed by a register
POLICY_ROLE_ASSERTED = f"{POLICY_BASE}/role-asserted-by-requester-v1"     # sender's role only asserted by Dr. Hofer
POLICY_AUTHOR_REGISTER = f"{POLICY_BASE}/author-register-confirmed-v1"    # document signatory found in a register
POLICY_AUTHOR_ASSERTED = f"{POLICY_BASE}/author-asserted-by-document-v1"  # signatory only stated by the document
POLICY_CLINICIAN_OVERRIDE = f"{POLICY_BASE}/clinician-override-v1"        # filed on a clinician's 'file anyway'
POLICY_EID_SIMULATED = f"{POLICY_BASE}/eid-simulated"                     # the eID was the SIMULATED ID Austria
POLICY_AI_TRANSCRIPTION = f"{POLICY_BASE}/ai-transcription-double-read-v1"  # photo/PDF: AI-transcribed, read twice


def eid_simulated(identity: dict | None) -> bool:
    """True if the identity came from the mock ID Austria (identity['simulated'], or a method/idp
    that says so). A mock never gets the eIDAS LoA URI."""
    i = identity or {}
    if i.get("simulated") is not None:
        return bool(i["simulated"])
    blob = f"{i.get('method') or ''} {i.get('idp') or ''}".lower()
    return any(w in blob for w in ("simul", "mock", "fake", "test"))


def policy_uri(name: str) -> str:
    """'lab-result-v1', 'urn:pgk-bridge:policy:lab-result-v1' or a full URI -> the one canonical URI."""
    n = (name or "").strip()
    if n.startswith(POLICY_BASE + "/"):
        return n
    if n.startswith("urn:pgk-bridge:policy:"):
        n = n[len("urn:pgk-bridge:policy:"):]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", n):
        raise ValueError(f"not a pgk-bridge policy name: {name!r}")
    return f"{POLICY_BASE}/{n}"


def slug(name: str) -> str:
    """Stable ASCII slug of a name (ICAO umlauts, no diacritics): 'Labor Kanalstraße' -> 'labor-kanalstrasse'."""
    from .identity import fold_name
    return re.sub(r"[^a-z0-9]+", "-", fold_name(unicodedata.normalize("NFKC", name or ""))).strip("-")
LOA_URI = {"high": "http://eidas.europa.eu/LoA/high", "substantial": "http://eidas.europa.eu/LoA/substantial",
           "low": "http://eidas.europa.eu/LoA/low"}

LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
REPORT_CODE = {LAB: "11502-2", DISCHARGE: "11490-0"}     # ELGA: Entlassungsbrief ärztlich typeCode
DISCHARGE_CLASS = "18842-5"                              # ELGA classCode equivalent (Discharge summary)
_FALLBACK_DISPLAY = {"11502-2": "Laboratory report", "18842-5": "Discharge Summary",
                     "11490-0": "Physician Discharge Summary"}
_INTERP = {"H": "High", "L": "Low", "N": "Normal", "HH": "Critical high", "LL": "Critical low",
           "A": "Abnormal", "AA": "Critical abnormal"}
_NS = uuid.uuid5(uuid.NAMESPACE_URL, BASE)
SERVICE_REF = {"type": "Device", "identifier": {"system": SID_SERVICE, "value": "pgk-bridge"},
               "display": "Befundlink (identity check and filing service)"}  # display only; the id stays
PURPOSE_TREAT = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason", "code": "TREAT",
                             "display": "treatment"}]}


# --------------------------------------------------------------------------- helpers

def _bytes(x) -> bytes:
    return x if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8")


def sha256_hex(data) -> str:
    """SHA-256 hex of raw bytes (or of the UTF-8 encoding of a str)."""
    return hashlib.sha256(_bytes(data)).hexdigest()


def ni_uri(data) -> str:
    """RFC 6920 named-information URI (sha-256) of raw bytes (or of the UTF-8 encoding of a str)."""
    d = hashlib.sha256(_bytes(data)).digest()
    return "ni:///sha-256;" + base64.urlsafe_b64encode(d).decode().rstrip("=")


_CHARSET = {"utf-8": "UTF-8", "cp1252": "windows-1252", "latin-1": "ISO-8859-1"}


def original_bytes(parsed: Parsed) -> bytes:
    """The answer exactly as received: the raw upload bytes when known (parse_upload), else the UTF-8 text."""
    return parsed.raw if getattr(parsed, "raw", None) is not None else parsed.text.encode("utf-8")


def original_content_type(parsed: Parsed) -> str:
    """The original's media type: the photo/PDF type for a transcribed upload, else Markdown with its charset."""
    if getattr(parsed, "content_type", None):
        return parsed.content_type
    charset = _CHARSET.get(getattr(parsed, "encoding", None) or "utf-8", "UTF-8")
    return f"text/markdown; charset={charset}"


def _attachment(parsed: Parsed, title: str | None, created: str | None = None) -> dict:
    data = original_bytes(parsed)
    att = {"contentType": original_content_type(parsed), "data": base64.b64encode(data).decode("ascii"),
           "size": len(data), "hash": base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")}
    if title:
        att["title"] = title
    if created:
        att["creation"] = created
    return att


def _instant(value) -> str:
    """ISO string or datetime -> FHIR instant with offset. Naive = Europe/Vienna."""
    if isinstance(value, str):
        v = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
    elif isinstance(value, datetime):
        dt = value
    else:
        raise TypeError("received_at must be an ISO string or datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=VIENNA)
    return fhir_instant(dt.astimezone(VIENNA))        # every filed instant carries Vienna's offset


def _now() -> str:
    return fhir_instant(datetime.now(timezone.utc).astimezone(VIENNA))


def _summary(r: dict) -> str:
    t = r["resourceType"]
    if t == "Observation":
        q = r.get("valueQuantity")
        v = (f"{q.get('comparator', '')}{q['value']} {q.get('unit') or ''}".strip() if q
             else r.get("valueString", "no value"))
        rr = (r.get("referenceRange") or [{}])[0].get("text")
        return f"{r['code'].get('text')}: {v}" + (f" (reference {rr})" if rr else "")
    if t == "DiagnosticReport":
        return f"{r['code'].get('text')}: {len(r.get('result', []))} results"
    if t == "DocumentReference":
        ct = ((r.get("content") or [{}])[0].get("attachment") or {}).get("contentType") or "text/markdown"
        return f"{r['type'].get('text')} ({ct.split(';')[0]} attached)"
    if t in ("Practitioner", "Organization"):
        n = r.get("name")
        return n if isinstance(n, str) else n[0].get("text", "")
    if t == "PractitionerRole":
        return f"{r['practitioner'].get('display')}: {r['code'][0]['text']}"
    if t == "Provenance":
        return f"Provenance of {len(r['target'])} resources, recorded {r['recorded']}"
    if t == "Task":
        return f"Document request, status {r['status']}"
    if t == "AuditEvent":
        return f"{r['subtype'][0]['display']} at {r['recorded']}, outcome {r['outcome']}"
    return t


def _narrate(r: dict) -> dict:
    """Add a minimal generated narrative (dom-6) right after id/meta."""
    div = f'<div xmlns="http://www.w3.org/1999/xhtml">{html.escape(_summary(r), quote=False)}</div>'
    out = {}
    for k in ("resourceType", "id", "meta"):
        if k in r:
            out[k] = r[k]
    out["text"] = {"status": "generated", "div": div}
    out.update({k: v for k, v in r.items() if k not in out})
    return out


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def ref_of(res: dict) -> str:
    """How resources of one filing reference each other: urn:uuid:<id> for resources built here (uuid
    ids, POSTed), '<Type>/<id>' for new versions of existing server resources (corrections, PUT)."""
    rid = str(res.get("id"))
    return f"urn:uuid:{rid}" if _UUID.fullmatch(rid) else f"{res['resourceType']}/{rid}"


def _uref(res: dict, display: str | None = None) -> dict:
    r = {"reference": ref_of(res)}
    if display:
        r["display"] = display
    return r


def _loinc_coding(code: str) -> dict:
    c = {"system": LOINC, "code": code}
    disp = _loinc.display(code) or _FALLBACK_DISPLAY.get(code)
    if disp:
        c["display"] = disp
    return c


def _annotation(ucum: str | None) -> str | None:
    if ucum and ucum.startswith("{") and ucum.endswith("}"):
        return ucum[1:-1]
    return None


class _Ids:
    def __init__(self, seed: str | None):
        self.seed = seed

    def __call__(self, label: str) -> str:
        if self.seed is None:
            return str(uuid.uuid4())
        return str(uuid.uuid5(_NS, f"{self.seed}|{label}"))


def _human_name(text: str, given: list[str] | None = None, family: str | None = None,
                prefix: list[str] | None = None) -> dict:
    if given is None and family is None:
        given, family, prefix = split_name(text)
    hn = {"text": text}
    if family:
        hn["family"] = family
    if given:
        hn["given"] = list(given)
    if prefix:
        hn["prefix"] = list(prefix)
    return hn


# Most stable identifier first. A register id names the person or organisation itself; a bPK
# hash only one eID form of the person; the org slug is always present for an Organization.
_ID_PRIORITY = {SID_BPK: 0, SID_BPK_LEGACY: 0, SID_GDA: 0, SID_PRACTITIONER_ROLE: 1, SID_SIGNATORY: 1, SID_ORG: 2}
# a Practitioner is matched ONLY by its own key: the sender by her eID hash, the signatory by its stated
# name@organisation (the two systems never match each other)
_PRACTITIONER_KEYS = (SID_BPK, SID_BPK_LEGACY, SID_SIGNATORY)


def signatory_key(name: str, org_name: str | None) -> str:
    """'Dr. Eva Brandl', 'Labor Kanalstraße' -> 'eva-brandl@labor-kanalstrasse' (titles dropped, folded)."""
    from .identity import rule_tokens
    n = "-".join(slug(t) for t in rule_tokens(name or "") if slug(t)) or slug(name)
    return f"{n}@{slug(org_name) if org_name else '-'}"


def _preferred_identifier(res: dict) -> dict | None:
    ids_ = res.get("identifier") or []
    return min(ids_, key=lambda x: _ID_PRIORITY.get(x.get("system"), 9)) if ids_ else None


def _conditional_key(res: dict) -> dict | None:
    """The identifier used for ifNoneExist. Organization: the slug (always present, so every filing
    of that organisation uses the same key). Practitioner: bPK hash (sender) or name@org (signatory). Role: its key."""
    if res["resourceType"] == "Organization":
        return next((i for i in res.get("identifier") or [] if i.get("system") == SID_ORG), None)
    if res["resourceType"] == "Practitioner":
        return next((i for i in res.get("identifier") or [] if i.get("system") in _PRACTITIONER_KEYS), None)
    return _preferred_identifier(res)


def _bpk_identifier(identity: dict) -> dict | None:
    h = identity.get("bpk_hash")
    if not h and identity.get("bpk"):
        h = hashlib.sha256(str(identity["bpk"]).encode("utf-8")).hexdigest()
    return {"system": SID_BPK, "value": h} if h else None


# --------------------------------------------------------------------------- build_resources

def _other_person_in(parsed: Parsed) -> bool:
    """Two patient lines (visible, hidden or found elsewhere) that are clearly different people."""
    from .identity import DIFFERENT, compare_dobs, compare_names
    named = [x for x in parsed.patient_lines if x.get("name")]
    for i, a in enumerate(named):
        for b in named[i + 1:]:
            if compare_names(a["name"], b["name"]) == DIFFERENT:
                return True
            if a.get("birthDate") and b.get("birthDate") and compare_dobs(a["birthDate"], b["birthDate"]) == DIFFERENT:
                return True
    return False


def patient_display(patient) -> str | None:
    """The requested Patient's own name (official, else first): 'Maria Beispiel'. Never the document's spelling."""
    if not isinstance(patient, dict):
        return None
    if patient.get("resourceType") != "Patient":
        return patient.get("name") if isinstance(patient.get("name"), str) else None
    names = patient.get("name") or []
    n = next((x for x in names if x.get("use") == "official"), names[0] if names else {})
    if n.get("text") and not n.get("family"):
        return n["text"]
    return " ".join([*(n.get("given") or []), n.get("family") or ""]).strip() or n.get("text") or None


def sender_is_patient(identity: dict | None, patient, request: dict | None) -> bool:
    """The eID-verified sender IS the requested patient. True when the service says so (``request['sender_is_patient']``,
    decided at the eID gate with identity rule 5: name AND birth date of the Patient), or when the request went to
    the patient herself (recipient name SAME as the Patient's) and the eID person passes ``identity_matches`` with
    the patient and the eID birth date (``identity['birth_date']``)."""
    request = request or {}
    if request.get("sender_is_patient") is not None:
        return bool(request["sender_is_patient"])
    i = identity or {}
    pname = patient_display(patient)
    rec = (request.get("recipient") or {}).get("name") or request.get("recipient_name")
    bd = i.get("birth_date") or i.get("birthdate") or i.get("birthDate")
    if not (pname and rec and bd) or compare_names(rec, pname) != "same":
        return False
    from .identity import identity_matches
    return identity_matches(rec, i.get("given_name") or i.get("given") or "", i.get("family_name") or i.get("family")
                            or "", patient=patient, birth_date=bd)


def informant_reference(resources: list[dict]) -> dict | None:
    """The verified sender as referenced by the Provenance informant agent: {'reference': 'Patient/<id>', ...} when
    the patient sent it herself (the service sets Task.owner), else the sender Practitioner's reference."""
    prov = next((r for r in resources if r.get("resourceType") == "Provenance"), None)
    inf = next((a for a in (prov or {}).get("agent") or [] if a["type"]["coding"][0]["code"] == "informant"), None)
    return copy.deepcopy(inf["who"]) if inf else None


def build_resources(parsed: Parsed, patient_ref: str, request: dict | None, identity: dict | None,
                    received_at, *, allow_hold: bool = False, decision: dict | None = None,
                    patient: dict | None = None) -> list[dict]:
    """Build the resources to file for one verified answer.

    ``patient`` (or ``request['patient']``): the requested Patient as read from the server. Every
    ``subject.display`` is its name, never the document's spelling (no display without it). When the eID sender
    is the patient herself (``sender_is_patient``), the Provenance informant is ``Patient/<id>`` (no
    onBehalfOf, no Practitioner for her); ``informant_reference(resources)`` returns it for Task.owner.

    Never builds a BLOCKed answer (parser blocks, ``decision['outcome'] == 'BLOCK'``, or two clearly
    different people in the document). Unless ``allow_hold`` (the clinician decided to file), raises for
    hold reasons and for an unrecognised kind; with ``allow_hold`` an unreadable type or a lab report
    without readable rows is built as a generic DocumentReference (original attached, type text-only).
    Returns [content resources..., Practitioner(s), PractitionerRole(s),
    Organization(s), Provenance]; Provenance is always last.
    """
    if parsed.block_reasons:
        raise ValueError("answer is blocked and can never be filed: " + "; ".join(parsed.block_reasons))
    if decision is not None and decision.get("outcome") == "BLOCK":
        raise ValueError("answer is blocked and can never be filed: " + ", ".join(decision.get("codes", [])))
    if _other_person_in(parsed):
        raise ValueError("the document contains another person's data; it can never be filed")
    if parsed.hold_reasons and not allow_hold:
        raise ValueError("answer is on hold: " + "; ".join(parsed.hold_reasons))
    if parsed.kind not in (LAB, DISCHARGE) and not allow_hold:
        raise ValueError(f"cannot build resources for kind '{parsed.kind}' without a clinician's decision")
    as_text = parsed.kind not in (LAB, DISCHARGE) or (parsed.kind == LAB and not parsed.lab)
    if not patient_ref or "/" not in patient_ref:
        raise ValueError("patient_ref must look like 'Patient/<id>'")
    request = request or {}
    identity = identity or {}
    recorded = _instant(received_at)
    ids = _Ids(f"{request.get('request_id')}|{recorded}" if request.get("request_id") else None)
    if patient is None:
        patient = request.get("patient")
    subject = {"reference": patient_ref}
    if patient_display(patient):                     # the Patient's own name, never the document's spelling
        subject["display"] = patient_display(patient)
    by_patient = sender_is_patient(identity, patient, request)

    orgs: dict[str, dict] = {}
    pracs: list[dict] = []
    roles: list[dict] = []
    role_of: dict[str, tuple] = {}

    def org(name: str | None, identifier: dict | None = None) -> dict | None:
        if not name:
            return None
        k = slug(name)
        if k not in orgs:
            o = {"resourceType": "Organization", "id": ids(f"org:{k}"),
                 "identifier": [{"system": SID_ORG, "value": k}], "active": True, "name": name}
            orgs[k] = o
        if identifier and identifier not in orgs[k]["identifier"]:
            orgs[k]["identifier"].append(identifier)
        return orgs[k]

    def practitioner(name: str, identifier: dict | None = None, given=None, family=None, prefix=None,
                     label: str = "doc") -> dict:
        """One Practitioner per ROLE in this filing: the eID-verified sender (keyed by its own bPK hash,
        nothing else) and the document's signatory (keyed by its stated name@organisation, SID_SIGNATORY:
        asserted by the document only). The two are never merged, and a register id found by a name match
        is never attached."""
        p = {"resourceType": "Practitioner", "id": ids(f"prac:{label}:{_fold(name)}"), "active": True,
             "name": [_human_name(name, given, family, prefix)]}
        if identifier:
            p["identifier"] = [identifier]
        pracs.append(p)
        return p

    def role(prac: dict, organization: dict | None, text: str | None) -> dict | None:
        if not text:
            return None
        r = {"resourceType": "PractitionerRole", "id": ids(f"role:{prac['id']}:{_fold(text)}"), "active": True,
             "practitioner": _uref(prac, prac["name"][0]["text"]), "code": [{"text": text}]}
        role_of[r["id"]] = (prac, organization, text)
        if organization:
            r["organization"] = _uref(organization, organization["name"])
        roles.append(r)
        return r

    # --- the verified sender (eID) and the document's signatory (asserted)
    rec = request.get("recipient") or {}
    s_given = identity.get("given_name") or identity.get("given")
    s_family = identity.get("family_name") or identity.get("family")
    if isinstance(s_given, list):
        s_given = " ".join(s_given)
    sender_name = " ".join(x for x in (s_given, s_family) if x) or rec.get("name") or \
        (parsed.sender or {}).get("name")
    reg = request.get("register") or {}
    reg_entry = reg.get("entry") or {}
    reg_org = reg_entry.get("org") if isinstance(reg_entry.get("org"), dict) else None
    gda_id = (reg_org or {}).get("id") or (rec.get("org_identifier") or {}).get("value")
    doc_org = org((parsed.org or {}).get("name"))
    sender_org = None if by_patient else org(identity.get("org") or rec.get("org") or (parsed.org or {}).get("name"),
                                             {"system": SID_GDA, "value": str(gda_id)} if gda_id else None)
    signatory = parsed.author
    sig_prac = sig_role = None
    if signatory and signatory.get("name"):
        sg, sf, sp = split_name(signatory["name"])
        sig_org = org(signatory.get("org")) or doc_org
        sig_prac = practitioner(signatory["name"], {"system": SID_SIGNATORY, "value": signatory_key(
            signatory["name"], sig_org["name"] if sig_org else None)}, given=sg, family=sf, prefix=sp, label="signatory")
        sig_role = role(sig_prac, sig_org, signatory.get("role"))
    sender_prac = None
    sender_patient = {"reference": patient_ref, **({"display": subject["display"]} if subject.get("display") else {})} \
        if by_patient else None
    if sender_name and not by_patient:              # the patient herself: no Practitioner for her
        given_l = s_given.split() if s_given else None
        sender_prac = practitioner(sender_name, _bpk_identifier(identity), given_l, s_family, label="sender")
        if parsed.sender and loose_name_equal(parsed.sender.get("name") or "", sender_name) and sender_prac is not sig_prac:
            role(sender_prac, sender_org, parsed.sender.get("role"))

    # stable keys: a PractitionerRole is deduplicated only when its person is identified
    for i, r in enumerate(roles):
        prac, organization, text = role_of[r["id"]]
        pid = _preferred_identifier(prac)
        if pid:
            person = pid["value"] if pid.get("system") == SID_SIGNATORY else pid["value"][:32]
            key = f"{person}@{slug(organization['name']) if organization else '-'}/{slug(text)}"
            roles[i] = {"resourceType": "PractitionerRole", "id": r["id"],
                        "identifier": [{"system": SID_PRACTITIONER_ROLE, "value": key}],
                        **{k: v for k, v in r.items() if k not in ("resourceType", "id")}}
            if r is sig_role:
                sig_role = roles[i]
    for p in pracs:                                  # preferred identifier first (used for ifNoneExist)
        if p.get("identifier"):
            first = _preferred_identifier(p)
            p["identifier"] = [first] + [x for x in p["identifier"] if x is not first]

    content: list[dict] = []
    if as_text:
        # a held answer the clinician decided to file, whose type or values could not be read: the
        # original as a text document (type text only), so an override always works
        content = _discharge(parsed, ids, subject, doc_org, sig_prac, recorded, request, generic=True)
    elif parsed.kind == LAB:
        content = _lab(parsed, ids, subject, doc_org, sig_prac, sig_role, request)
    else:
        content = _discharge(parsed, ids, subject, doc_org, sig_prac, recorded, request)

    filed = content + pracs + roles + list(orgs.values())
    prov = _provenance(parsed, ids, filed, recorded, identity, request, sig_prac, doc_org or sender_org,
                       sender_prac, sender_org, sender_patient=sender_patient)
    return [_narrate(r) for r in filed + [prov]]


def _lab(parsed, ids, subject, doc_org, sig_prac, sig_role, request) -> list[dict]:
    performer = [_uref(x, x.get("name") if isinstance(x.get("name"), str) else x["name"][0]["text"])
                 for x in (doc_org, sig_prac) if x]
    obs = []
    for i, row in enumerate(parsed.lab):
        o_status = (row.get("status") or
                    ("preliminary" if parsed.report_status == "preliminary" else "final"))
        o = {"resourceType": "Observation", "id": ids(f"obs:{i}"), "status": o_status,
             "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                       "code": "laboratory", "display": "Laboratory"}]}],
             "code": {}, "subject": subject}
        if row.get("loinc"):
            o["code"]["coding"] = [_loinc_coding(row["loinc"])]
        o["code"]["text"] = row.get("test") or row.get("loinc") or "unnamed test"
        if parsed.sample_time:
            o["effectiveDateTime"] = parsed.sample_time
        if parsed.report_time and "T" in parsed.report_time:
            o["issued"] = parsed.report_time
        if performer:
            o["performer"] = performer
        v = row.get("value")
        if row.get("status") == "registered":
            pass                                     # pending: no value at all (never a 'final' word as value)
        elif row.get("absent"):
            o["dataAbsentReason"] = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                                                 "code": "error", "display": "Error"}]}
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            q = {"value": v}
            if row.get("comparator"):
                q["comparator"] = row["comparator"]
            if row.get("unit"):
                q["unit"] = row["unit"]
            elif _annotation(row.get("ucum")):
                q["unit"] = _annotation(row["ucum"])     # 'INR' for {INR}: the validator wants it in unit
            if row.get("ucum"):
                q["system"], q["code"] = UCUM, row["ucum"]
            o["valueQuantity"] = q
        elif isinstance(v, str) and v:
            o["valueString"] = v
        else:
            o["dataAbsentReason"] = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                                                 "code": "unknown", "display": "Unknown"}]}
        if row.get("flag") in _INTERP or row.get("flag_text"):
            it = {}
            if row.get("flag") in _INTERP:           # the LAB's flag, mapped only where unambiguous
                it["coding"] = [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                 "code": row["flag"], "display": _INTERP[row["flag"]]}]
            if row.get("flag_text"):
                it["text"] = row["flag_text"]        # exactly as the lab wrote it ('+', '*', 'pathologisch')
            o["interpretation"] = [it]
        if row.get("ref_text") or row.get("ref_low") is not None or row.get("ref_high") is not None:
            rr = {}
            for k, fk in (("low", "ref_low"), ("high", "ref_high")):
                if row.get(fk) is not None:
                    sq = {"value": row[fk]}
                    if row.get("unit"):
                        sq["unit"] = row["unit"]
                    elif _annotation(row.get("ucum")):
                        sq["unit"] = _annotation(row["ucum"])
                    if row.get("ucum"):
                        sq["system"], sq["code"] = UCUM, row["ucum"]
                    rr[k] = sq
            if row.get("ref_text"):
                rr["text"] = row["ref_text"]
            o["referenceRange"] = [rr]
        if row.get("notes"):
            o["note"] = [{"text": n} for n in row["notes"]]
        obs.append(o)
    dr = {"resourceType": "DiagnosticReport", "id": ids("report"), "status": parsed.report_status or "final",
          "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB",
                                    "display": "Laboratory"}]}],
          "code": {"coding": [_loinc_coding(REPORT_CODE[LAB])], "text": parsed.title or "Laboratory report"},
          "subject": subject}
    if request.get("request_id"):
        dr["identifier"] = [{"system": SID_REQUEST, "value": str(request["request_id"])}]
        dr = {"resourceType": dr.pop("resourceType"), "id": dr.pop("id"), "identifier": dr.pop("identifier"), **dr}
    if parsed.requester and parsed.requester.get("name"):
        # the order this result answers ('Einsender' / 'Requested by'), as stated in the document:
        # information only, a display-only reference (no ServiceRequest resource is invented)
        dr["basedOn"] = [{"type": "ServiceRequest", "display": _einsender_text(parsed)}]
    if parsed.sample_time:
        dr["effectiveDateTime"] = parsed.sample_time
    if parsed.report_time and "T" in parsed.report_time:
        dr["issued"] = parsed.report_time
    if performer:
        dr["performer"] = performer
    if sig_role or sig_prac:
        x = sig_role or sig_prac
        dr["resultsInterpreter"] = [_uref(x, sig_prac["name"][0]["text"])]
    dr["result"] = [_uref(o, o["code"]["text"]) for o in obs]
    if parsed.comment:
        dr["conclusion"] = parsed.comment
    dr["presentedForm"] = [_attachment(parsed, parsed.title or "Original answer")]
    return [dr] + obs


def _einsender_text(parsed, label: str = "Einsender") -> str:
    rq = parsed.requester or {}
    return f"{label} laut Dokument: " + ", ".join(x for x in (rq.get("name"), rq.get("org")) if x)


_DE_WORDS = {"der", "die", "das", "und", "mit", "bei", "nach", "von", "zur", "zum", "ist", "wurde", "keine", "nicht",
             "patientin", "entlassung", "befund", "aufenthalt", "empfehlung", "therapie", "für", "im", "am"}
_EN_WORDS = {"the", "and", "with", "of", "to", "was", "were", "is", "no", "not", "for", "after", "on", "in",
             "discharge", "report", "stay", "recommendations", "medication", "patient", "within", "range"}


def document_language(text: str | None) -> str | None:
    """'de' or 'en' from common function words of the document (None when neither clearly dominates)."""
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    de = sum(w in _DE_WORDS or any(c in w for c in "äöüß") for w in words)
    en = sum(w in _EN_WORDS for w in words)
    if de == en:
        return None
    return "de" if de > en else "en"


# IHE format code: the document's media type alone tells how to read it (a Markdown/PDF/photo original)
FORMAT_MIME_SUFFICIENT = {"system": "http://ihe.net/fhir/ihe.formatcode.fhir/CodeSystem/formatcode",
                          "code": "urn:ihe:iti:xds:2017:mimeTypeSufficient", "display": "mimeType Sufficient"}


def document_unique_id(data: bytes) -> str:
    """XDS uniqueId of an original: a name-based UUID (v5) of its SHA-256, so the same bytes keep the same id."""
    return f"urn:uuid:{uuid.uuid5(_NS, 'sha256:' + hashlib.sha256(data).hexdigest())}"


def _discharge(parsed, ids, subject, doc_org, sig_prac, recorded, request, generic: bool = False) -> list[dict]:
    d = {"resourceType": "DocumentReference", "id": ids("document"),
         "masterIdentifier": {"system": "urn:ietf:rfc:3986", "value": document_unique_id(original_bytes(parsed))}}
    confirmed = parsed.doc_type_confirmed and not generic
    if request.get("request_id"):
        d["identifier"] = [{"system": SID_REQUEST, "value": str(request["request_id"])}]
    d.update({"status": "current",
              "docStatus": "preliminary" if parsed.report_status == "preliminary" else
              ("amended" if parsed.report_status in ("corrected", "amended") else "final"),
              "type": ({"coding": [_loinc_coding(REPORT_CODE[DISCHARGE])], "text": parsed.title or "Discharge report"}
                       if confirmed else {"text": parsed.title or "Dokument"})})
    if confirmed:
        d["category"] = [{"coding": [_loinc_coding(DISCHARGE_CLASS)]}]
    d.update({"subject": subject, "date": recorded})
    authors = [_uref(x, x["name"][0]["text"] if x["resourceType"] == "Practitioner" else x["name"])
               for x in (sig_prac, doc_org) if x]
    if authors:
        d["author"] = authors
    if parsed.title:
        d["description"] = parsed.title
    att = _attachment(parsed, parsed.title or "Discharge report")
    lang = document_language(parsed.text)
    if lang:
        att = {"contentType": att["contentType"], "language": lang, **{k: v for k, v in att.items() if k != "contentType"}}
    d["content"] = [{"attachment": att, "format": dict(FORMAT_MIME_SUFFICIENT)}]
    if parsed.stay and parsed.stay.get("start"):
        d["context"] = {"period": {"start": parsed.stay["start"], "end": parsed.stay["end"]}}
    if parsed.requester and parsed.requester.get("name"):
        d.setdefault("context", {})["related"] = [{"display": _einsender_text(parsed, "Zuweiser")}]
    return [d]


def _provenance(parsed, ids, filed, recorded, identity, request, sig_prac, sig_org, sender_prac, sender_org,
                sender_patient: dict | None = None):
    agents = []
    reg = request.get("register") or {}
    author_reg = request.get("author_register") if isinstance(request.get("author_register"), dict) else None
    role_basis = request.get("role_basis")
    override = request.get("override") if isinstance(request.get("override"), dict) else None
    if sig_prac:
        a_txt = "signatory as stated in the document (asserted, not identity-checked)"
        if author_reg:
            a_txt += (f"; {reg.get('register') or 'Ärzteliste'}: Eintrag gleichen Namens gefunden ({author_reg.get('id')}"
                      + (f", {author_reg['specialty']}" if author_reg.get("specialty") else "")
                      + "; Name-Abgleich, nicht an eine eID gebunden)")
        else:
            a_txt += "; not found in a professional register"
        a = {"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                  "code": "author", "display": "Author"}]},
             "role": [{"text": a_txt}],
             "who": _uref(sig_prac, sig_prac["name"][0]["text"])}
        if sig_org:
            a["onBehalfOf"] = _uref(sig_org, sig_org["name"])
        agents.append(a)
    if sender_prac:
        loa = (identity.get("loa") or "high").lower()
        how = ("ID Austria (SIMULATION, not a real eID; no level of assurance)" if eid_simulated(identity)
               else f"ID Austria (LoA {loa})")
        a = {"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                  "code": "informant", "display": "Informant"}]},
             "role": [{"text": f"sender, identity verified with {how} and bound to the "
                               f"named recipient of the request; role "
                               + (f"checked before sending: {reg.get('register') or 'register'}: Eintrag gleichen Namens "
                                  f"gefunden" + (f" ({(reg.get('entry') or {}).get('id')})" if (reg.get('entry') or {})
                                                 .get('id') else "") + " (Name-Abgleich, nicht an die eID gebunden)"
                                  if role_basis == "register" else "asserted by the requester (no register entry)")}],
             "who": _uref(sender_prac, sender_prac["name"][0]["text"])}
        if sender_org:
            a["onBehalfOf"] = _uref(sender_org, sender_org["name"])
        agents.append(a)
    elif sender_patient:                              # the patient herself sent it: no organisation, no role
        how = ("ID Austria (SIMULATION, not a real eID; no level of assurance)" if eid_simulated(identity)
               else f"ID Austria (LoA {(identity.get('loa') or 'high').lower()})")
        agents.append({"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                            "code": "informant", "display": "Informant"}]},
                       "role": [{"text": f"sender: the patient herself, identity verified with {how} (name and birth "
                                         f"date of the requested Patient) and bound to the request"}],
                       "who": dict(sender_patient)})
    agents.append({"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                        "code": "verifier", "display": "Verifier"}]},
                   "role": [{"text": "checked sender identity, request binding and patient match"}],
                   "who": SERVICE_REF})
    reasons = [PURPOSE_TREAT]
    tr = getattr(parsed, "transcription", None) if isinstance(getattr(parsed, "transcription", None), dict) else None
    if tr:
        model = str(tr.get("model") or "unknown model")
        agents.append({"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                            "code": "assembler", "display": "Assembler"}]},
                       "role": [{"text": "KI-transkribiert, 2 unabhängige Lesungen: the content was AI-transcribed "
                                         "from the attached original photo/PDF, twice and independently; both readings "
                                         "agreed on patient, document type, every value and the times. It decided "
                                         "nothing: the filing rules, the HL7 validator and prove() ran on the agreed "
                                         "transcription"}],
                       "who": {"type": "Device", "identifier": {"system": SID_SERVICE, "value": f"ki-lesung/{model}"},
                               "display": f"Befundlink KI-Lesung ({model}, {tr.get('reads', 2)} unabhängige Lesungen)"}})
    if override:
        by = str(override.get("by") or "requesting physician")
        who = {"type": "Practitioner", "display": request.get("requester_name") or by}
        ref = ((request.get("requester_refs") or {}).get("practitioner")
               if isinstance(request.get("requester_refs"), dict) else None)
        if ref and (not by.startswith("@") or by == request.get("requester_mxid")):
            who = {"reference": ref, **who}           # Dr. Hofer's Practitioner on the server
        mx = by if by.startswith("@") else request.get("requester_mxid")
        if mx:
            who["identifier"] = {"system": SID_MXID, "value": mx}
        held = override.get("held_reasons") or []
        if isinstance(held, str):
            held = [held]
        # the decision is in the clinician agent's role text, not in reason (reason stays coded: TREAT)
        txt = ("clinician decision: filed despite a hold"
               + (f" at {_instant(override['at'])}" if override.get("at") else "")
               + f"; stated reason: {override.get('reason') or '-'}"
               + (f"; held for: {'; '.join(map(str, held))}" if held else ""))[:1000]
        agents.append({"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                            "code": "verifier", "display": "Verifier"}]},
                       "role": [{"text": txt}], "who": who})
    policy = [policy_uri(request["policy"]) if request.get("policy") else POLICY.get(parsed.kind, POLICY_OTHER),
              POLICY_REQUEST_BINDING]
    loa = (identity.get("loa") or "").lower()
    if eid_simulated(identity):
        policy.append(POLICY_EID_SIMULATED)           # never claim an eIDAS LoA for the mock
    elif loa in LOA_URI:
        policy.append(LOA_URI[loa])
    if role_basis and not sender_patient:            # the patient answers for herself: no role to check
        policy.append(POLICY_ROLE_REGISTER if role_basis == "register" else POLICY_ROLE_ASSERTED)
    if sig_prac:
        policy.append(POLICY_AUTHOR_REGISTER if author_reg else POLICY_AUTHOR_ASSERTED)
    if override:
        policy.append(POLICY_CLINICIAN_OVERRIDE)
    if tr:
        policy.append(POLICY_AI_TRANSCRIPTION)
    policy = list(dict.fromkeys(policy))
    # recorded/occurred = the filing: never before the clinician's decision it records ('filed_at' when known)
    filed_at = max([recorded] + [_instant(t) for t in (request.get("filed_at"), (override or {}).get("at")) if t],
                   key=lambda t: datetime.fromisoformat(t))
    prov = {"resourceType": "Provenance", "id": ids("provenance"),
            "target": [_uref(r) for r in filed] + _task_target(request),
            "occurredDateTime": filed_at, "recorded": filed_at, "policy": policy,
            "reason": reasons,
            "activity": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                                     "code": "CREATE", "display": "create"}]},
            "agent": agents,
            "entity": [{"role": "source", "what": {
                "identifier": {"system": "urn:ietf:rfc:3986", "value": ni_uri(original_bytes(parsed))},
                "display": f"original answer as received ({original_content_type(parsed).split(';')[0]}, "
                           f"{len(original_bytes(parsed))} bytes, sha-256 {sha256_hex(original_bytes(parsed))})"}}]}
    if tr and tr.get("ni"):
        prov["entity"].append({"role": "derivation", "what": {
            "identifier": {"system": "urn:ietf:rfc:3986", "value": tr["ni"]},
            "display": f"AI transcription of the original (text/markdown, {tr.get('markdown_size')} bytes, "
                       f"sha-256 {tr.get('markdown_sha256')}), from which the content was built"}})
    assertion = identity.get("assertion") or identity.get("jws") or identity.get("id_token")
    if assertion and (sender_prac or sender_patient):
        _set_signature(prov, assertion, identity.get("verified_at") or identity.get("auth_time") or recorded,
                       sender_prac, sender_org, on_behalf=dict(sender_patient) if sender_patient else None)
    return prov


def _task_target(request: dict) -> list[dict]:
    """The request Task is updated by this filing (status completed, output -> the report), so it is a
    target too. Version-independent reference: the Task's status keeps changing after filing."""
    t = request.get("task_ref") or (f"Task/{request['task_id']}" if request.get("task_id") else None)
    return [{"reference": t}] if isinstance(t, str) and t.startswith("Task/") else []


# Provenance.signature: the JWS is the SERVICE's statement (its own key, JWKS), so the signer (who) is the
# Befundlink verifier Device; the ID Austria sender it vouches for is onBehalfOf. ASTM E1762 'Verification
# Signature': the service attests that it verified the sender, the request binding and the filed content.
SIG_TYPE_VERIFICATION = {"system": "urn:iso-astm:E1762-95:2013", "code": "1.2.840.10065.1.12.1.5",
                         "display": "Verification Signature"}


def _verifier_who(prov: dict) -> dict:
    """The Provenance verifier agent that is the service Device (not a clinician's override verifier)."""
    for a in prov.get("agent") or []:
        w = a.get("who") or {}
        if a["type"]["coding"][0]["code"] == "verifier" and (
                (w.get("identifier") or {}).get("system") == SID_SERVICE or w.get("type") == "Device"):
            return copy.deepcopy(w)
    return copy.deepcopy(SERVICE_REF)


def _set_signature(prov: dict, assertion: str, when, sender_prac: dict | None, sender_org: dict | None = None,
                   who: dict | None = None, on_behalf: dict | None = None) -> None:
    """signature.who = the verifier Device (signer of the statement); signature.onBehalfOf = the verified sender
    (Practitioner). ``sender_org`` is kept for the call signature; the organisation stays on the informant agent."""
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, timezone.utc)
    sig = {"type": [dict(SIG_TYPE_VERIFICATION)],
           "when": _instant(when),
           "who": who or _verifier_who(prov),
           "onBehalfOf": on_behalf or _uref(sender_prac, sender_prac["name"][0]["text"]),
           "sigFormat": "application/jose",
           "data": base64.b64encode(str(assertion).encode("ascii", "strict")).decode("ascii")}
    prov["signature"] = [sig]


# The clinically relevant content of the filed resources, hashed into the signed statement so a value
# edited on the server after filing no longer verifies. Practitioner/Organization/PractitionerRole and
# the Task are NOT hashed: they may be reused existing resources (resolve_conditionals) or change status.
CLINICAL_TYPES = ("DiagnosticReport", "Observation", "DocumentReference")
_CLINICAL_KEYS = {
    "Observation": ("status", "category", "code", "subject", "effectiveDateTime", "issued", "performer", "valueQuantity",
                    "valueString", "valueCodeableConcept", "dataAbsentReason", "interpretation", "referenceRange", "note"),
    "DiagnosticReport": ("status", "category", "code", "subject", "basedOn", "effectiveDateTime", "issued",
                         "performer", "resultsInterpreter", "result", "conclusion", "presentedForm"),
    "DocumentReference": ("status", "docStatus", "type", "category", "subject", "author", "content", "context"),
}


def _norm_refs(node):
    """References: a Patient reference is kept verbatim (it is stable and must not change); every other
    reference is reduced to its resource type ('Observation', 'Practitioner'), so server ids, urn:uuid and
    reused Practitioners do not matter. Attachments: only contentType, size and hash (data is covered by
    the hash). Displays are dropped."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "reference" and isinstance(v, str):
                out[k] = v if v.startswith("Patient/") else (v.split("/")[0] if not v.startswith("urn:") else "?")
            elif k in ("display", "id", "extension"):
                continue
            elif k == "data" and "hash" in node:
                continue
            else:
                out[k] = _norm_refs(v)
        if "reference" in out and out["reference"] == "?" and "type" in node:
            out["reference"] = node["type"]
        return out
    if isinstance(node, list):
        return [_norm_refs(x) for x in node]
    return node


def clinical_digest(res: dict, type_of: dict | None = None) -> str:
    """SHA-256 over the canonical JSON of the clinically relevant elements of one resource.
    ``type_of`` maps urn:uuid references to their resource type (as built)."""
    keys = _CLINICAL_KEYS.get(res["resourceType"], ())
    sub = {"resourceType": res["resourceType"], **{k: res[k] for k in keys if k in res}}
    if type_of:
        sub = json.loads(json.dumps(sub), object_hook=lambda d: (
            {**d, "reference": type_of.get(d["reference"], d["reference"])} if isinstance(d.get("reference"), str)
            and d["reference"].startswith("urn:") else d))
    canon = json.dumps(_norm_refs(sub), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def resources_digest(resources: list[dict]) -> tuple[str, dict]:
    """(resources_sha256, {reference: digest}) over the clinical resources. The overall hash is over the
    SORTED digests, so it does not depend on order or ids."""
    type_of = {f"urn:uuid:{r.get('id')}": r["resourceType"] for r in resources if r.get("id")}
    per = {}
    for r in resources:
        if r["resourceType"] in CLINICAL_TYPES:
            per[ref_of(r) if r.get("id") else f"{r['resourceType']}/?"] = clinical_digest(r, type_of)
    whole = hashlib.sha256("\n".join(sorted(per.values())).encode("ascii")).hexdigest()
    return whole, per


def provenance_binding(resources: list[dict]) -> dict:
    """What a signed identity statement must cover so it cannot be replayed onto another filing, and so
    the filed clinical content cannot be edited afterwards:
    {'content_sha256': hex SHA-256 of the original answer (the Provenance source entity),
     'targets': sorted Provenance target references (urn:uuid:... as built),
     'resources_sha256': SHA-256 over the clinical content of every filed DiagnosticReport/Observation/
                         DocumentReference (see ``clinical_digest``),
     'resource_digests': {target reference: digest} (lets the verifier say which resource differs)}.
    The service puts these claims into the statement, signs it, then calls ``attach_provenance_signature``."""
    prov = next(r for r in resources if r["resourceType"] == "Provenance")
    whole, per = resources_digest([r for r in resources if r is not prov])
    return {"content_sha256": _source_sha256(prov), "targets": sorted(t["reference"] for t in prov["target"]),
            "resources_sha256": whole, "resource_digests": per}


def _source_sha256(prov: dict) -> str | None:
    for e in prov.get("entity", []):
        if e.get("role") == "source":
            v = ((e.get("what") or {}).get("identifier") or {}).get("value") or ""
            if v.startswith("ni:///sha-256;"):
                raw = v.split(";", 1)[1]
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).hex()
    return None


def attach_provenance_signature(resources: list[dict], assertion: str, when=None) -> dict:
    """Put a (re-)signed statement into the Provenance of ``resources`` (in place) and return it.
    ``who`` is the verifier Device (the service signs), ``onBehalfOf`` the informant agent (the verified sender)."""
    prov = next(r for r in resources if r["resourceType"] == "Provenance")
    inf = next((a for a in prov["agent"] if a["type"]["coding"][0]["code"] == "informant"), None)
    if inf is None:
        raise ValueError("Provenance has no verified sender (informant) to sign for")
    _set_signature(prov, assertion, when or prov["recorded"], None, None, who=_verifier_who(prov),
                   on_behalf=copy.deepcopy(inf["who"]))
    return prov


# --------------------------------------------------------------------------- Task + transaction

_DOC_KIND_CODE = {LAB: "11502-2", DISCHARGE: "11490-0"}


def build_task(request: dict, patient_ref: str, owner) -> dict:
    """The document request as an R4 Task (status requested, intent order).

    ``owner``: a Reference dict, or {name, role, org} of the named outside person.
    ``request`` keys used: request_id, requester_mxid, requester_name, requester_refs {practitioner}, doc_kind,
    created_at, expires_at, note.
    """
    rid = request.get("request_id")
    t = {"resourceType": "Task",
         "id": str(uuid.uuid5(_NS, f"task|{rid}")) if rid else str(uuid.uuid4())}
    if rid:
        t["identifier"] = [{"system": SID_REQUEST, "value": str(rid)}]
    t.update({"status": "requested", "intent": "order", "priority": "routine",
              "description": "Request for a document from a person outside the network",
              "for": {"reference": patient_ref},
              "authoredOn": _instant(request.get("created_at") or _now())})
    t["lastModified"] = t["authoredOn"]
    mx = request.get("requester_mxid")
    if mx or request.get("requester_name"):
        req = {"type": "Practitioner", "display": request.get("requester_name") or mx}
        if mx:
            req["identifier"] = {"system": SID_MXID, "value": mx}
        refs = request.get("requester_refs") if isinstance(request.get("requester_refs"), dict) else {}
        if refs.get("practitioner"):                 # Dr. Hofer's Practitioner on the server
            req = {"reference": refs["practitioner"], **{k: v for k, v in req.items() if k != "type"}}
        t["requester"] = req
    if isinstance(owner, dict) and ("reference" in owner or "identifier" in owner):
        t["owner"] = owner
    elif isinstance(owner, dict):
        disp = ", ".join(x for x in (owner.get("name"), owner.get("role"), owner.get("org")) if x)
        t["owner"] = {"type": "Practitioner", "display": disp or "outside recipient"}
    elif isinstance(owner, str) and owner:
        t["owner"] = {"type": "Practitioner", "display": owner}
    t["reasonCode"] = PURPOSE_TREAT
    if request.get("expires_at"):
        t["restriction"] = {"period": {"start": t["authoredOn"], "end": _instant(request["expires_at"])}}
    kind = request.get("doc_kind")
    if kind in _DOC_KIND_CODE:
        t["input"] = [{"type": {"text": "requested document type"},
                       "valueCodeableConcept": {"coding": [_loinc_coding(_DOC_KIND_CODE[kind])]}}]
    if request.get("note"):
        t["note"] = [{"text": str(request["note"])}]
    return _narrate(t)


def task_update(task: dict, status: str, output_refs: list | None = None, reason: str | None = None,
                when=None) -> dict:
    """A copy of ``task`` moved to ``status`` (completed|on-hold|rejected|cancelled|in-progress...),
    with Task.output references to the filed resources and an optional statusReason."""
    t = copy.deepcopy(task)
    t.pop("text", None)
    t["status"] = status
    lm = _instant(when or _now())
    if t.get("authoredOn") and datetime.fromisoformat(lm) < datetime.fromisoformat(t["authoredOn"]):
        lm = t["authoredOn"]                      # inv-1: lastModified >= authoredOn
    t["lastModified"] = lm
    if reason:
        t["statusReason"] = {"text": reason}
    else:
        t.pop("statusReason", None)
    if output_refs:
        outs = []
        for r in output_refs:
            ref = r if isinstance(r, dict) and "reference" in r else (_uref(r) if isinstance(r, dict) else {"reference": r})
            outs.append({"type": {"text": "filed document"}, "valueReference": ref})
        t["output"] = outs
    return _narrate(t)


_CONDITIONAL = ("Practitioner", "Organization", "PractitionerRole")


def _idkey(i: str):
    return (0, int(i), "") if str(i).isdigit() else (1, 0, str(i))


def _rewrite_refs(node, mapping: dict):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference" and isinstance(v, str) and v in mapping:
                node[k] = mapping[v]
            else:
                _rewrite_refs(v, mapping)
    elif isinstance(node, list):
        for x in node:
            _rewrite_refs(x, mapping)


def resolve_conditionals(bundle: dict, search) -> tuple[dict, list[dict]]:
    """Make a transaction immune to duplicates already on the server (HAPI-0958 'multiple matches').

    ``search(resource_type, {"identifier": "system|value"}) -> list of resources`` is the caller's
    FHIR search (e.g. GET [base]/Practitioner?identifier=...). For every conditional create the
    resource's identifiers are tried most-stable-first (register id > bPK hash > slug):
      - one or more matches -> the entry is dropped and every reference to its urn:uuid is rewritten
        to the existing resource (the oldest match when several exist);
      - no match -> a plain POST (ifNoneExist removed, so a later duplicate cannot fail this filing);
      - a search error -> a plain POST.
    Returns (new bundle, report [{fullUrl, resourceType, action: reused|created, reference?, matches}]).
    The filing never fails because of duplicate Practitioner/Organization/PractitionerRole resources."""
    b = copy.deepcopy(bundle)
    mapping, report, keep = {}, [], []
    for e in b["entry"]:
        req = e.get("request") or {}
        res = e.get("resource") or {}
        if req.get("method") != "POST" or not req.get("ifNoneExist"):
            keep.append(e)
            continue
        rt = res["resourceType"]
        idents = sorted(res.get("identifier") or [], key=lambda x: _ID_PRIORITY.get(x.get("system"), 9))
        if rt == "Practitioner":                      # never search or merge a person by a register id
            idents = [i for i in idents if i.get("system") in _PRACTITIONER_KEYS]
        if rt == "Organization":                      # slug first: it is on every filing
            idents.sort(key=lambda x: 0 if x.get("system") == SID_ORG else 1)
        found, n = None, 0
        # resources filed before the rename carry the legacy bPK system: search both
        expanded = []
        for ident in idents:
            expanded.append(ident)
            if ident.get("system") == SID_BPK:
                expanded.append({"system": SID_BPK_LEGACY, "value": ident["value"]})
        for ident in expanded:
            try:
                hits = search(rt, {"identifier": f"{ident['system']}|{ident['value']}"}) or []
            except Exception:                         # never fail the filing over a lookup
                hits = []
            ids_ = sorted({h.get("id") for h in hits if isinstance(h, dict) and h.get("id")}, key=_idkey)
            if ids_:
                found, n = ids_[0], len(ids_)
                break
        if found:
            mapping[e["fullUrl"]] = f"{rt}/{found}"
            report.append({"fullUrl": e["fullUrl"], "resourceType": rt, "action": "reused",
                           "reference": f"{rt}/{found}", "matches": n})
        else:
            req.pop("ifNoneExist", None)
            keep.append(e)
            report.append({"fullUrl": e["fullUrl"], "resourceType": rt, "action": "created", "matches": 0})
    b["entry"] = keep
    _rewrite_refs(b["entry"], mapping)
    return b, report


def build_transaction(resources: list[dict], task_update: dict | None = None,
                      server_base: str = "http://localhost:8090/fhir") -> dict:
    """One transaction Bundle: every resource POSTed under fullUrl urn:uuid:<id>
    (ids are removed so the server assigns them), Practitioner/Organization with an
    identifier as conditional create (ifNoneExist), and the Task update as PUT."""
    entries = []
    for r in resources:
        res = copy.deepcopy(r)
        if not _UUID.fullmatch(str(res.get("id"))):          # a new version of an existing resource
            rid = res["id"]
            req = {"method": "PUT", "url": f"{res['resourceType']}/{rid}"}
            pv = (res.get("meta") or {}).pop("versionId", None)
            if not res.get("meta"):
                res.pop("meta", None)
            if pv is not None:
                req["ifMatch"] = f'W/"{pv}"'                 # only on top of the expected prior version
            entries.append({"fullUrl": f"{server_base.rstrip('/')}/{res['resourceType']}/{rid}", "resource": res,
                            "request": req})
            continue
        rid = res.pop("id")
        e = {"fullUrl": f"urn:uuid:{rid}", "resource": res,
             "request": {"method": "POST", "url": res["resourceType"]}}
        ident = _conditional_key(res) if res["resourceType"] in _CONDITIONAL else None
        if ident:
            e["request"]["ifNoneExist"] = "identifier=" + urllib.parse.quote(f"{ident['system']}|{ident['value']}",
                                                                              safe=":/|@")
        entries.append(e)
    if task_update is not None:
        if not task_update.get("id"):
            raise ValueError("task_update needs the server id of the Task")
        entries.append({"fullUrl": f"{server_base.rstrip('/')}/Task/{task_update['id']}",
                        "resource": task_update,
                        "request": {"method": "PUT", "url": f"Task/{task_update['id']}"}})
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


# --------------------------------------------------------------------------- AuditEvent

AUDIT_STEPS = {
    # step: (display, DICOM type code, DICOM display, action)
    "request-sent": ("Request sent", "110110", "Patient Record", "C"),
    "link-opened": ("Link opened", "110110", "Patient Record", "R"),
    "identity-checked": ("Identity checked", "110114", "User Authentication", "E"),
    "answer-sent": ("Answer sent", "110110", "Patient Record", "C"),
    "answer-filed": ("Answer filed", "110107", "Import", "C"),
    "misdelivery-caught": ("Misdelivery caught", "110113", "Security Alert", "E"),
}
_OUTCOME = {"success": "0", "ok": "0", "minor": "4", "failure": "4", "failed": "4", "serious": "8",
            "blocked": "8", "major": "12", "0": "0", "4": "4", "8": "8", "12": "12"}


def _step_key(step: str) -> str:
    k = step.strip().lower().replace("_", "-").replace(" ", "-")
    if k not in AUDIT_STEPS:
        raise ValueError(f"unknown audit step '{step}' (one of {', '.join(AUDIT_STEPS)})")
    return k


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w-])(?:\+|00|0)\d[\d ()/-]{6,}\d")    # +43 660 ..., 0660 ...; not dates


def _redact(text: str | None, secrets_: list[str]) -> str | None:
    """Remove the given names/addresses (and any e-mail address or phone number) from free text."""
    if not text:
        return text
    out = text
    for x in sorted({x.strip() for x in secrets_ if x and len(x.strip()) >= 3}, key=len, reverse=True):
        out = re.sub(r"(?<!\w)" + re.escape(x) + r"(?!\w)", "[redacted]", out, flags=re.I)
    out = _EMAIL.sub("[redacted]", out)
    return _PHONE.sub("[redacted]", out)


def _pseudonymous_agent(w: dict) -> dict:
    """A third party (failed identity check, stranger): only the bPK-hash identifier, no name."""
    a = {"requestor": bool(w.get("requestor", False))}
    ident = w.get("identifier")
    if ident and ident.get("system") in (SID_BPK, SID_BPK_LEGACY):
        a["who"] = {"identifier": {"system": SID_BPK, "value": ident["value"]},
                    "display": "pseudonymous person (bPK hash)"}
    elif w.get("bpk_hash"):
        a["who"] = {"identifier": {"system": SID_BPK, "value": str(w["bpk_hash"])},
                    "display": "pseudonymous person (bPK hash)"}
    else:
        a["who"] = {"display": "unidentified person"}
    a["purposeOfUse"] = [PURPOSE_TREAT]
    return a


_MXID = re.compile(r"@[A-Za-z0-9._=/+-]+:[A-Za-z0-9.-]+(?::\d+)?")
_BPK_IN_TEXT = re.compile(r"bpk-hmac:[0-9a-f]{16,64}")
_SERVICE_NAMES = ("pgk-bridge", "befundlink")
# AuditEvent.agent.type (extensible: participation-role-type)
AGENT_TYPE_APPLICATION = {"coding": [{"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "110150",
                                      "display": "Application"}]}
AGENT_TYPE_AUTHOR = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationType",
                                 "code": "AUT", "display": "author (originator)"}]}
AGENT_TYPE_INFORMANT = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationType",
                                    "code": "INF", "display": "informant"}]}


def _is_service_name(name: str) -> bool:
    return str(name or "").strip().lower().startswith(_SERVICE_NAMES)


def _split_agents(w: dict) -> list[dict]:
    """'pgk-bridge (Absender Eva Brandl (ID Austria – Simulation, bpk-hmac:…))': the service Device that acted AND
    the verified sender it acted for (identified by her bPK hash), as two agents."""
    name = str(w.get("name") or "")
    m = re.match(r"^\s*(?:pgk-bridge|befundlink)\s*\((?:Absender|Sender|for)\s+(.+)\)\s*$", name, re.I)
    ident = w.get("identifier")
    bpk = (ident or {}).get("value") if (ident or {}).get("system") in (SID_BPK, SID_BPK_LEGACY) else None
    if not m:
        return [w]
    bpk = bpk or (_BPK_IN_TEXT.search(m[1])[0] if _BPK_IN_TEXT.search(m[1]) else None)
    person = {"name": m[1].strip(), "requestor": w.get("requestor", False), "resource_type": "Practitioner"}
    if bpk:
        person["identifier"] = {"system": SID_BPK, "value": bpk}
    return [{"name": name.split("(")[0].strip(), "requestor": False, "service": True}, person]


def _agent(w) -> dict:
    """One AuditEvent agent, identified where the actor is known: the service Device (SID_SERVICE, DICOM
    'Application'), a clinician by her Matrix id (SID_MXID, the system Task.requester uses; plus her Practitioner
    reference when given; 'author'), an outside person by her bPK hash (SID_BPK; 'informant')."""
    if isinstance(w, str):
        w = {"name": w}
    if w.get("pseudonymous"):
        return _pseudonymous_agent(w)
    w = dict(w)
    name = str(w.get("name") or "")
    ident = w.get("identifier")
    if not ident and (w.get("service") or _is_service_name(name)):
        w["identifier"] = dict(SERVICE_REF["identifier"])
        w.setdefault("resource_type", "Device")
        w.setdefault("type", AGENT_TYPE_APPLICATION)
    elif not ident and _MXID.fullmatch(name.strip()):
        w["identifier"] = {"system": SID_MXID, "value": name.strip()}
        w.setdefault("resource_type", "Practitioner")
        w.setdefault("type", AGENT_TYPE_AUTHOR)
    elif not ident and _BPK_IN_TEXT.search(name):
        w["identifier"] = {"system": SID_BPK, "value": _BPK_IN_TEXT.search(name)[0]}
        w.setdefault("type", AGENT_TYPE_INFORMANT)
    elif ident and ident.get("system") in (SID_BPK, SID_BPK_LEGACY):
        w.setdefault("type", AGENT_TYPE_INFORMANT)
    elif ident and ident.get("system") == SID_MXID:
        w.setdefault("type", AGENT_TYPE_AUTHOR)
    a = {"requestor": bool(w.get("requestor", False))}
    if w.get("type"):
        a["type"] = w["type"] if isinstance(w["type"], dict) else {"text": str(w["type"])}
    who = {}
    if w.get("reference"):
        who["reference"] = w["reference"]
    if w.get("identifier"):
        who["identifier"] = w["identifier"]
    if w.get("resource_type") and not w.get("reference"):
        who["type"] = w["resource_type"]
    if w.get("name"):
        who["display"] = w["name"]
    if who:
        a["who"] = who
    if w.get("altId"):
        a["altId"] = str(w["altId"])
    if w.get("name") and not who.get("display"):
        a["name"] = w["name"]
    a["purposeOfUse"] = [PURPOSE_TREAT]
    return a


def audit_hash(event: dict) -> str:
    """SHA-256 over the canonical JSON of the event without id/meta and without its own
    'hash' entity detail (the chain link the service stores)."""
    e = copy.deepcopy(event)
    e.pop("id", None)
    e.pop("meta", None)
    for ent in e.get("entity", []):
        if "detail" in ent:
            ent["detail"] = [d for d in ent["detail"] if d.get("type") != "hash"]
            if not ent["detail"]:
                del ent["detail"]
    return hashlib.sha256(json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def audit_event(step: str, who, what, when, outcome, request_id, *, task_ref: str | None = None,
                patient_ref: str | None = None, prev_hash: str | None = None,
                outcome_desc: str | None = None, pseudonymous_agent: bool = False,
                redact: list[str] | None = None) -> dict:
    """A valid R4 AuditEvent for one of the six steps.

    step: request-sent | link-opened | identity-checked | answer-sent | answer-filed |
          misdelivery-caught (underscores/spaces accepted)
    who: str, dict {name, reference?, identifier?, resource_type?, requestor?, type?, altId?, network?}
         or a list of those. The first agent is the requestor unless stated.
    what: str (free text, goes to outcomeDesc and the entity description) or dict
          {text, references: ['DiagnosticReport/1', ...]}.
    when: ISO string or datetime (naive = Europe/Vienna).
    outcome: success|failure|serious|0|4|8|12.
    prev_hash: SHA-256 of the previous event (hash chain); the event's own hash is
               added as entity.detail 'hash' (see ``audit_hash``).
    pseudonymous_agent: data minimisation for third parties (failed identity check, strangers,
               forwarded links). Every agent becomes {who.identifier = bPK hash, display
               'pseudonymous person (bPK hash)'} with no name; that agent's name, its e-mail/phone/
               ``address`` and every e-mail address or phone number are removed from outcomeDesc and the
               entity description. Per agent, ``{"pseudonymous": True}`` in ``who`` does the same for that
               agent only (e.g. keep the requester, pseudonymise the stranger).
    redact:    extra strings to remove from the texts.
    """
    k = _step_key(step)
    disp, dcm, dcm_disp, action = AUDIT_STEPS[k]
    oc = _OUTCOME.get(str(outcome).lower())
    if oc is None:
        raise ValueError(f"unknown outcome '{outcome}'")
    agents_in = who if isinstance(who, list) else [who]
    agents = []
    hidden: list[str] = list(redact or [])
    for i, w in enumerate(agents_in):
        if isinstance(w, str):
            w = {"name": w}
        w = dict(w)
        w.setdefault("requestor", i == 0)
        if pseudonymous_agent:
            w["pseudonymous"] = True
        if w.get("pseudonymous"):
            # only the exact identifying strings: the full name (also without a trailing '(ID Austria, ...)'
            # annotation), e-mail, phone, address; never single generic words such as 'ID' or 'Austria'
            nm = str(w.get("name") or "")
            hidden += [str(w.get(k)) for k in ("name", "address", "email", "phone", "altId") if w.get(k)]
            core = re.sub(r"\s*\(.*\)\s*$", "", nm).strip()
            if core and core != nm:
                hidden.append(core)
            agents.append(_agent(w))
            continue
        agents.extend(_agent(x) for x in _split_agents(w))
    if not agents:
        agents = [_agent({"name": "Befundlink", "requestor": False})]
    text = what if isinstance(what, str) else (what or {}).get("text")
    if any(a.get("who", {}).get("display", "").startswith(("pseudonymous", "unidentified")) for a in agents):
        text = _redact(text, hidden)
        outcome_desc = _redact(outcome_desc, hidden)
    refs = [] if isinstance(what, str) or not what else list((what or {}).get("references") or [])
    ev = {"resourceType": "AuditEvent",
          "type": {"system": "http://dicom.nema.org/resources/ontology/DCM", "code": dcm, "display": dcm_disp},
          "subtype": [{"system": CS_AUDIT_STEP, "code": k, "display": disp}],
          "action": action, "recorded": _instant(when), "outcome": oc}
    if outcome_desc or text:
        ev["outcomeDesc"] = outcome_desc or text
    ev["purposeOfEvent"] = [PURPOSE_TREAT]
    ev["agent"] = agents
    ev["source"] = {"site": "pgk-bridge", "observer": SERVICE_REF,
                    "type": [{"system": "http://terminology.hl7.org/CodeSystem/security-source-type",
                              "code": "4", "display": "Application Server"}]}
    entities = []
    req_ent = {"what": {"identifier": {"system": SID_REQUEST, "value": str(request_id)}},
               "type": {"system": "http://hl7.org/fhir/resource-types", "code": "Task", "display": "Task"},
               "role": {"system": "http://terminology.hl7.org/CodeSystem/object-role", "code": "4",
                        "display": "Domain Resource"}}
    if task_ref:
        req_ent["what"]["reference"] = task_ref
    if text:
        req_ent["description"] = text
    detail = []
    if prev_hash:
        detail.append({"type": "prev_hash", "valueString": prev_hash})
    if detail:
        req_ent["detail"] = detail
    entities.append(req_ent)
    if patient_ref:
        entities.append({"what": {"reference": patient_ref},
                         "type": {"system": "http://terminology.hl7.org/CodeSystem/audit-entity-type", "code": "1",
                                  "display": "Person"},
                         "role": {"system": "http://terminology.hl7.org/CodeSystem/object-role", "code": "1",
                                  "display": "Patient"}})
    seen = {task_ref} if task_ref else set()
    for r in refs:
        if r in seen or (task_ref and unversioned(r) == task_ref):
            continue                                  # the Task is entity[0] already (no duplicate entity)
        seen.add(r)
        rt = r.split("/")[0] if "/" in r and not r.startswith("urn:") else None
        ent = {"what": {"reference": r}}
        if rt:
            ent["type"] = {"system": "http://hl7.org/fhir/resource-types", "code": rt, "display": rt}
        entities.append(ent)
    ev["entity"] = entities
    ev = _narrate(ev)
    h = audit_hash(ev)
    ev["entity"][0].setdefault("detail", []).append({"type": "hash", "valueString": h})
    return ev


def verify_audit_chain(events: list[dict]) -> list[str]:
    """Problems in a hash chain of AuditEvents (in order). [] if intact."""
    problems = []
    prev = None
    for i, e in enumerate(events):
        det = {d["type"]: d.get("valueString") for d in (e.get("entity") or [{}])[0].get("detail", [])}
        if det.get("hash") != audit_hash(e):
            problems.append(f"event {i}: content does not match its hash")
        if i and det.get("prev_hash") != prev:
            problems.append(f"event {i}: prev_hash does not link to event {i - 1}")
        prev = det.get("hash")
    return problems


def codesystems() -> list[dict]:
    """Our own CodeSystems (load them on the FHIR server and into the validator)."""
    cs = [{
        "resourceType": "CodeSystem", "id": "audit-step", "url": CS_AUDIT_STEP, "version": "1.0.0",
        "name": "PgkBridgeAuditStep", "title": "Befundlink audit steps", "status": "active",
        "experimental": False, "date": "2026-10-29", "publisher": "Befundlink",
        "description": "The six audited steps of an outside-document request.",
        "caseSensitive": True, "content": "complete", "count": len(AUDIT_STEPS),
        "concept": [{"code": k, "display": v[0]} for k, v in AUDIT_STEPS.items()]}]
    return [_narrate(c) for c in cs]


def check_answer(parsed: Parsed, request: dict | None = None, patient: dict | None = None,
                 declared: dict | None = None) -> list[str]:
    """Legacy helper: reasons not to file (strings). Prefer ``fhirmap.decide()``, which implements
    tests/jury/RULES.md with FILE/HOLD/BLOCK outcomes and rule codes.

    Combines the parser's hold reasons, the document kind vs the request, the
    three-way patient check (document patient line vs requested FHIR Patient vs
    the patient the sender declared, if a form was used) and the soft
    'Requested by' check. Messages never name a different patient.
    """
    from .identity import patient_check
    reasons = list(parsed.hold_reasons)
    if parsed.kind not in (LAB, DISCHARGE):
        reasons.append("document type not recognised")
    request = request or {}
    want = request.get("doc_kind")
    if (want in (LAB, DISCHARGE) and parsed.kind in (LAB, DISCHARGE) and parsed.kind != want
            and not any("was requested but" in r for r in reasons)):
        msg = f"a {want.replace('_', ' ')} was requested but the document is a {parsed.kind.replace('_', ' ')}"
        if msg not in reasons:
            reasons.append(msg)
    if patient is not None:
        reasons += [f"document vs requested patient: {r}" for r in patient_check(parsed, patient)]
    if declared is not None:
        if patient is not None:
            reasons += [f"declared vs requested patient: {r}" for r in patient_check(declared, patient)]
        reasons += [f"document vs declared patient: {r}" for r in patient_check(parsed, declared, "the declared patient")]
    rq = request.get("requester_name")
    if rq and parsed.requester and parsed.requester.get("name") and compare_names(parsed.requester["name"], rq) != "same":
        reasons.append("the document says it was requested by someone other than the requesting physician")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out



# --------------------------------------------------------------------------- corrections

def _obs_key(o: dict) -> str:
    loinc = next((c.get("code") for c in (o.get("code") or {}).get("coding") or [] if c.get("system") == LOINC), None)
    return f"loinc:{loinc}" if loinc else "text:" + _fold((o.get("code") or {}).get("text") or "")


_RESULT_KEYS = ("valueQuantity", "valueString", "valueCodeableConcept", "dataAbsentReason", "interpretation",
                "referenceRange", "note", "code")


def _same_result(a: dict, b: dict) -> bool:
    pick = lambda o: json.dumps(_norm_refs({k: o[k] for k in _RESULT_KEYS if k in o}), sort_keys=True)
    return pick(a) == pick(b) and a.get("status") not in ("registered",) and b.get("status") not in ("registered",)


def _dt_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        da, db = datetime.fromisoformat(a.replace("Z", "+00:00")), datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return False
    if da.tzinfo is None or db.tzinfo is None:
        return da.replace(tzinfo=None) == db.replace(tzinfo=None)
    return da == db


def matches_prior(parsed: Parsed, prior_report: dict) -> bool:
    """Is ``prior_report`` (a DiagnosticReport from the server) the report the correction says it replaces?
    True when the stated time ('ersetzt den Befund vom 27.10.2026, 10:20') equals the prior report's
    issued or effective time, and the prior report is a final/preliminary/corrected lab report."""
    if (prior_report or {}).get("resourceType") != "DiagnosticReport":
        return False
    if prior_report.get("status") not in ("final", "preliminary", "corrected", "amended", "partial"):
        return False
    t = parsed.replaces_time
    if not t:
        return False
    if "T" not in t:                                   # a date only: compare the dates
        return any((prior_report.get(k) or "")[:10] == t for k in ("issued", "effectiveDateTime"))
    return any(_dt_equal(t, prior_report.get(k)) for k in ("issued", "effectiveDateTime"))


def build_correction(parsed: Parsed, prior_report: dict, prior_observations: list[dict], patient_ref: str,
                     request: dict | None, identity: dict | None, received_at, *, decision: dict | None = None,
                     require_match: bool = True) -> dict:
    """File a corrected report as NEW VERSIONS of the report it replaces (never a second report beside it).

    ``prior_report``: the replaced DiagnosticReport as read from the server (id, and meta.versionId if known).
    ``prior_observations``: its Observations as read from the server.
    Returns {"resources": [...], "unchanged": [prior Observations kept as they are],
             "changed": ["Observation/<id>", ...], "added": [urn:uuid refs], "not_in_correction": [...]}.
    - the DiagnosticReport keeps the prior id (PUT), status 'corrected' ('amended' for a Nachtrag), and its
      result list points to the corrected, unchanged and added Observations;
    - an Observation whose result differs keeps its id (PUT) with status 'corrected';
    - a row that was not in the prior report is a new Observation (POST);
    - a prior Observation the correction does not list stays unchanged, is not in the new result list and is
      reported in ``not_in_correction`` (show it to the clinician);
    - Provenance targets the new versions; its entity 'revision' references the replaced report version;
      ``provenance_binding(resources)`` covers them.
    Raises ValueError if the answer is not a correction, the prior report belongs to another patient, the
    stated replaced time does not match (``matches_prior``) or rows cannot be matched unambiguously.
    Pass ``unchanged`` as ``prove(parsed, resources, unchanged=...)``. ``build_transaction`` PUTs the versions."""
    if parsed.report_status not in ("corrected", "amended"):
        raise ValueError("not a corrected report")
    if (prior_report or {}).get("resourceType") != "DiagnosticReport" or not prior_report.get("id"):
        raise ValueError("prior_report must be the replaced DiagnosticReport with its server id")
    if (prior_report.get("subject") or {}).get("reference") != patient_ref:
        raise ValueError("the prior report belongs to another patient")
    if require_match and not matches_prior(parsed, prior_report):
        raise ValueError("Ursprungsbefund nicht gefunden: the prior report does not match the replaced report's time")
    base = build_resources(parsed, patient_ref, request, identity, received_at, allow_hold=True, decision=decision)
    dr = next((r for r in base if r["resourceType"] == "DiagnosticReport"), None)
    if dr is None:
        raise ValueError("the correction has no readable lab values")
    prov = base[-1]
    prior_by_key: dict[str, dict] = {}
    for o in prior_observations or []:
        k = _obs_key(o)
        if k in prior_by_key:
            raise ValueError(f"prior report has two Observations for {k}: rows cannot be matched unambiguously")
        prior_by_key[k] = o
    mapping, unchanged, changed, added, keep = {}, [], [], [], []
    seen = set()
    for r in base:
        if r["resourceType"] != "Observation":
            continue
        k = _obs_key(r)
        if k in seen:
            raise ValueError(f"the correction lists {k} twice")
        seen.add(k)
        old = prior_by_key.get(k)
        if old is None:
            added.append(ref_of(r))
            keep.append(r)
            continue
        old_ref = f"Observation/{old['id']}"
        if _same_result(r, old):
            mapping[ref_of(r)] = old_ref
            unchanged.append(old)
            continue
        new = dict(r)
        new["id"] = str(old["id"])
        new["status"] = "corrected"
        new = _narrate({k2: v for k2, v in new.items() if k2 != "text"})
        mapping[ref_of(r)] = old_ref
        changed.append(old_ref)
        keep.append(new)
    not_in = [f"Observation/{o['id']}" for k, o in prior_by_key.items() if k not in seen]
    new_dr = dict(dr)
    new_dr["id"] = str(prior_report["id"])
    new_dr["status"] = "corrected" if parsed.report_status == "corrected" else "amended"
    mapping[ref_of(dr)] = f"DiagnosticReport/{prior_report['id']}"
    if prior_report.get("identifier"):
        ids_ = list(prior_report["identifier"])
        for i in new_dr.get("identifier") or []:
            if i not in ids_:
                ids_.append(i)
        new_dr["identifier"] = ids_
    others = [r for r in base if r["resourceType"] not in ("DiagnosticReport", "Observation", "Provenance")]
    resources = [new_dr] + keep + others
    blob = json.dumps(resources)
    for u, ref in mapping.items():
        blob = blob.replace(f'"{u}"', f'"{ref}"')
    resources = json.loads(blob)
    new_dr = resources[0]
    # rows the correction does not list: their prior Observations stay as they are (not corrected, not
    # deleted) and are reported in 'not_in_correction'; the new report version lists what the document lists
    resources[0] = _narrate({k: v for k, v in new_dr.items() if k != "text"})
    # Provenance: targets = the new versions (and new resources); entity revision = the replaced version
    pblob = json.dumps(prov)
    for u, ref in mapping.items():
        pblob = pblob.replace(f'"{u}"', f'"{ref}"')
    prov = json.loads(pblob)
    unchanged_refs = {f"Observation/{o['id']}" for o in unchanged}
    prov["target"] = [t for t in prov["target"] if t["reference"] not in unchanged_refs]
    vid = (prior_report.get("meta") or {}).get("versionId")
    prior_ref = f"DiagnosticReport/{prior_report['id']}" + (f"/_history/{vid}" if vid else "")
    prov["entity"].append({"role": "revision", "what": {"reference": prior_ref,
                                                        "display": "replaced report (corrected by this answer)"}})
    # Versioned targets (option a): every new version is PUT with If-Match on the prior version, so on
    # success the server's next version id is prior + 1; the Provenance points to exactly those versions.
    version_map, unversioned = {}, []
    prior_vids = {f"Observation/{o['id']}": (o.get("meta") or {}).get("versionId") for o in prior_observations or []}
    prior_vids[f"DiagnosticReport/{prior_report['id']}"] = vid
    for r in resources:
        ref = f"{r['resourceType']}/{r['id']}"
        if _UUID.fullmatch(str(r["id"])):
            continue
        pv = prior_vids.get(ref)
        if pv is not None and str(pv).isdigit():
            r["meta"] = {"versionId": str(pv)}         # build_transaction turns this into If-Match: W/"<pv>"
            version_map[ref] = str(int(pv) + 1)
        else:
            unversioned.append(ref)
    prov = version_provenance_targets(prov, version_map)
    # the report and its changed Observations already existed: this filing revises them
    prov["activity"] = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                                    "code": "UPDATE", "display": "revise"}]}
    prov = _narrate({k: v for k, v in prov.items() if k != "text"})
    resources.append(prov)
    return {"resources": resources, "unchanged": unchanged, "changed": changed, "added": added,
            "not_in_correction": not_in, "expected_versions": version_map, "unversioned_targets": unversioned}


def unversioned(ref: str) -> str:
    """'DiagnosticReport/3709/_history/2' -> 'DiagnosticReport/3709'."""
    return re.sub(r"/_history/[^/]+$", "", ref or "")


def version_provenance_targets(provenance: dict, version_map: dict) -> dict:
    """A copy of ``provenance`` whose targets point to specific versions. ``version_map`` maps
    'Type/id' to the version id ('2') or to the full versioned reference ('Type/id/_history/2'), e.g.
    from the transaction response's entry.response.location. Unlisted targets stay as they are."""
    p = copy.deepcopy(provenance)
    for t in p.get("target") or []:
        base = unversioned(t.get("reference") or "")
        v = version_map.get(base)
        if v:
            t["reference"] = v if "/_history/" in str(v) else f"{base}/_history/{v}"
    return p

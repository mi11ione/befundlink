"""The service's upload decision and fhirmap.decide() must agree on every jury corpus case and on fuzz cases:
same outcome (FILE/HOLD/BLOCK) and the same primary RULES.md code. One safety rule, no drift."""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

import fhirmap  # noqa: E402
from jury import cases as jury_cases  # noqa: E402
from jury import generate  # noqa: E402
from jury.model import CTX, EID  # noqa: E402

from service.config import Settings  # noqa: E402
from service.core import Service  # noqa: E402

PATIENT = json.loads((Path(__file__).parent / "fixtures" / "patient.json").read_text())
N_FUZZ = 2000
SEED = 20261029
REQUESTER = "@lena.hofer:klinikum.example"


@pytest.fixture(scope="module")
def svc():
    return Service(Settings(db_path=str(Path(tempfile.mkdtemp()) / "agree.sqlite"), data_key_hex="22" * 32, async_io=False, validator_server=False,
                            prewarm_tx=False, dev=True))


def _raw(c) -> bytes:
    return base64.b64decode(c.raw_b64) if c.raw_b64 else (c.text or "").encode("utf-8")


def primary_agrees(so, sc, fc) -> bool:
    """The service passes fhirmap.decide()'s RULES.md codes through unchanged: the primary code must be identical."""
    if so == "FILE":
        return fc[:1] in ([], ["OK"])
    return sc[:1] == fc[:1]


def service_decision(svc, c) -> tuple[str, list[str]]:
    """Exactly what the live service does with one upload: eID binding rule, then Service.assess()."""
    ctx, eid = CTX[c.ctx], EID[c.eid]
    if not fhirmap.identity_matches(ctx.name, eid.given, eid.family, patient=PATIENT, birth_date=eid.birthDate):
        return "BLOCK", ["EID_MISMATCH"]
    r = {"id": "req-x", "ref_code": "KB-X", "recipient_name": ctx.name, "recipient_org": ctx.org,
         "doc_kind": ctx.doc_kind, "requester_mxid": REQUESTER}
    a = svc.assess(_raw(c), r, {"given": eid.given, "family": eid.family, "verdict": "match", "birthdate": eid.birthDate},
                   PATIENT)
    return a["outcome"].upper(), a["codes"]


def fhirmap_decision(svc, c) -> tuple[str, list[str]]:
    """fhirmap.decide() called directly, with the request built exactly as Service.assess() builds it: the
    recipient's organisation (ORG_MISMATCH), the requester's display name, the eID birth date, parse_upload."""
    ctx, eid = CTX[c.ctx], EID[c.eid]
    raw = _raw(c)
    parsed = fhirmap.parse_upload(raw, ctx.doc_kind)
    request = {"doc_kind": ctx.doc_kind, "requester_name": svc.registers.member_name(REQUESTER),
               "recipient": {"name": ctx.name, "org": ctx.org}}
    eid_claims = {"given_name": eid.given, "family_name": eid.family, "birthDate": eid.birthDate}
    d = fhirmap.decide(parsed, request, PATIENT, eid_claims, raw)
    return d["outcome"], d["codes"]


def all_cases():
    return [("corpus", c) for c in jury_cases.build()] + [("fuzz", c) for c in generate.fuzz(N_FUZZ, SEED)]


def test_service_and_fhirmap_agree(svc):
    disagree = []
    n = {"corpus": 0, "fuzz": 0}
    for src, c in all_cases():
        n[src] += 1
        so, sc = service_decision(svc, c)
        fo, fc = fhirmap_decision(svc, c)
        if so != fo or not primary_agrees(so, sc, fc):
            disagree.append((src, c.id, c.title, c.expect, so, sc[:2], fo, fc[:2]))
    report = "\n".join(f"{d[0]} {d[1]} [{d[2]}] label={d[3]} service={d[4]} {d[5]} fhirmap={d[6]} {d[7]}" for d in disagree)
    print(f"agreement: corpus {n['corpus'] - sum(d[0] == 'corpus' for d in disagree)}/{n['corpus']}, "
          f"fuzz {n['fuzz'] - sum(d[0] == 'fuzz' for d in disagree)}/{n['fuzz']}")
    assert not disagree, report


def test_service_decisions_match_the_corpus_labels(svc):
    wrong = [(c.id, c.expect, service_decision(svc, c)) for c in jury_cases.build() if service_decision(svc, c)[0] != c.expect]
    assert not wrong, wrong

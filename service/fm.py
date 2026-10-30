"""Thin adapter over `fhirmap` (parse, build, validate, prove, Task, transaction, AuditEvent).
Normalises the few return shapes the service relies on."""
from __future__ import annotations

from typing import Any

import fhirmap
from fhirmap import build as _build

SIX = ("request sent", "link opened", "identity checked", "answer sent", "answer filed", "misdelivery caught")


def get(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class FhirMap:
    name = "fhirmap"

    def __init__(self, validate_mode: str = "auto"):
        self.validate_mode = validate_mode

    def parse_answer(self, text: str, hint: str | None):
        return fhirmap.parse_answer(text, hint)

    def build_resources(self, parsed, patient_ref: str, request: dict, identity: dict, received_at: str,
                        allow_hold: bool = False, decision: dict | None = None) -> list[dict]:
        return fhirmap.build_resources(parsed, patient_ref, request, identity, received_at, allow_hold=allow_hold,
                                       decision=decision)

    def validate(self, resources: list[dict]) -> list[str]:
        """HL7 validator_cli (one JVM). Errors only; the full report is kept in self.last_report."""
        rep = fhirmap.validate_report(resources, self.validate_mode)
        self.last_report = {"mode": rep.get("mode"), "requested_mode": rep.get("requested_mode"),
                            "fallback": rep.get("fallback"), "terminology_checked": rep.get("terminology_checked"),
                            "seconds": rep.get("seconds"), "files": len(rep.get("files") or {}),
                            "errors": len(rep.get("errors") or []), "warnings": len(rep.get("warnings") or [])}
        return [str(e) for e in rep.get("errors") or []]

    last_report: dict | None = None

    def prove(self, parsed, resources: list[dict], patient: dict | None = None, unchanged: list[dict] | None = None) -> dict:
        """-> {ok, matched, total, summary, missing, raw}. Lab: rows equal / rows in source; else fields.
        unchanged: for a correction, the prior Observations it keeps as they are (fhirmap.build_correction)."""
        out = fhirmap.prove(parsed, resources, patient, unchanged=unchanged)
        rows = out.get("rows") or []
        if rows:
            matched, total = sum(1 for r in rows if r.get("status") == "equal"), out.get("rows_in_source", len(rows))
            missing = [f"{r.get('test')}: {r.get('status')} {r.get('diffs') or ''}".strip() for r in rows if r.get("status") != "equal"]
        else:
            fields = out.get("fields") or []
            matched, total = sum(1 for f in fields if f.get("equal")), len(fields)
            missing = []
        missing += [f"{f['field']}: {f.get('source')} != {f.get('fhir')}" for f in out.get("fields") or [] if not f.get("equal")]
        return {"ok": bool(out.get("all_equal")), "matched": matched, "total": total, "summary": out.get("summary"),
                "missing": missing}

    # ------------------------------------------------------------ Task
    def task(self, r: dict, status: str, reason: str | None = None, output_refs: list | None = None,
             when: str | None = None, requester_name: str | None = None, requester_refs: dict | None = None,
             owner: dict | None = None) -> dict:
        """The request row as a FHIR Task in `status` (deterministic id from the request id).
        requester_refs {'practitioner', 'organization'}: Task.requester references Dr. Hofer's Practitioner, and
        restriction.recipient names who the result is for (her and her clinic). owner: a Reference (the Patient,
        when she is the recipient herself); else the named outside person (display)."""
        base = fhirmap.build_task(
            {"request_id": r["id"], "requester_mxid": r["requester_mxid"], "requester_name": requester_name,
             "doc_kind": r["doc_kind"],
             "created_at": r["created_at"], "expires_at": r["expires_at"], "note": r.get("note")},
            f"Patient/{r['patient_id']}",
            owner or {"name": r["recipient_name"], "role": r.get("recipient_role"), "org": r.get("recipient_org")})
        refs = requester_refs or {}
        if refs.get("practitioner"):
            requester = dict(base.get("requester") or {})
            requester.pop("type", None)  # the reference says it
            base["requester"] = {"reference": refs["practitioner"], **requester}
            recipients = [{"reference": refs["practitioner"], "display": requester.get("display")}]
            if refs.get("organization"):
                recipients.append({"reference": refs["organization"]})
            base.setdefault("restriction", {})["recipient"] = recipients
        if status == "requested" and not reason and not output_refs:
            return base
        return fhirmap.task_update(base, status, output_refs=output_refs, reason=reason, when=when)

    def transaction(self, resources: list[dict], task: dict | None, server_base: str) -> dict:
        return fhirmap.build_transaction(resources, task_update=task, server_base=server_base)

    # ------------------------------------------------------------ AuditEvent (six steps only)
    def audit_event(self, *, step: str, who: str, what: str, when: str, outcome: str, request_id: str | None,
                    task_ref: str | None, patient_ref: str | None, prev_hash: str | None,
                    references: list[str] | None = None, pseudonymous: bool = False, redact: list[str] | None = None,
                    who_id: str | None = None, who_ref: str | None = None) -> dict:
        """who_id: the person's pairwise id (salted bPK hash). It becomes the agent's identifier, so a pseudonymised
        stranger is still recognisable across attempts in FHIR (without a name)."""
        agent = {"name": who[:250]}
        if who_ref:  # a PGK member (Dr. Hofer): her Practitioner on the FHIR server
            agent["reference"] = who_ref
        if who_id:
            agent["identifier"] = {"system": self.SID_BPK, "value": who_id}
        return fhirmap.audit_event(step, agent, {"text": what[:1000], "references": references or []},
                                   when, outcome, request_id or "-", task_ref=task_ref, patient_ref=patient_ref,
                                   prev_hash=prev_hash, pseudonymous_agent=pseudonymous, redact=redact or None)

    @staticmethod
    def audit_hash(ev: dict) -> str:
        return fhirmap.audit_hash(ev)

    @staticmethod
    def verify_chain(events: list[dict]) -> list[str]:
        return fhirmap.verify_audit_chain(events)

    SID_BPK = _build.SID_BPK

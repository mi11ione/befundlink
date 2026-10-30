"""fhirmap: deterministic Markdown answer -> FHIR R4 mapping, proof and validation."""
from .identity import (compare_dobs, compare_names, identity_matches, parse_dob, name_match_level, loose_name_equal, names_match, normalize_name, rule_tokens, parse_date, patient_check, person_mismatch_reason,
                       same_person)
from .parse import Parsed, decode_upload, parse_answer, parse_upload
from .build import (audit_event, audit_hash, build_correction, build_resources, version_provenance_targets, unversioned, build_task, build_transaction, matches_prior, ref_of, resolve_conditionals, check_answer, codesystems,
                    task_update, verify_audit_chain)
from .build import (BASE, POLICY, POLICY_AUTHOR_ASSERTED, POLICY_AUTHOR_REGISTER, POLICY_BASE, POLICY_CLINICIAN_OVERRIDE,
                    POLICY_DISCHARGE_REPORT, POLICY_LAB_RESULT, POLICY_OTHER, POLICY_REQUEST_BINDING, POLICY_ROLE_ASSERTED,
                    POLICY_ROLE_REGISTER, SID_AERZTELISTE, SID_BPK, SID_BPK_LEGACY, SID_GDA, SID_MXID, SID_ORG, SID_PRACTITIONER_ROLE,
                    SID_REQUEST, POLICY_EID_SIMULATED, attach_provenance_signature, eid_simulated, policy_uri,
                    provenance_binding, slug, clinical_digest, resources_digest)
from .sig import verify_jws, verify_provenance_signature
from .prove import prove
from .cards import REASONS_DE, card_facts, join_reasons_de, reason_text_de, reasons_text_de, sender_text_de
from .decide import decide, patient_decision
from .validate import prewarm_tx_cache, tx_reachable, validate, validate_report

__all__ = ["parse_answer", "parse_upload", "decode_upload", "Parsed", "build_resources", "build_task", "task_update", "build_transaction", "resolve_conditionals", "build_correction", "matches_prior", "ref_of", "version_provenance_targets", "unversioned",
           "audit_event", "audit_hash", "verify_audit_chain", "codesystems", "validate", "validate_report",
           "prove", "same_person", "name_match_level", "loose_name_equal", "rule_tokens", "parse_date",
           "person_mismatch_reason", "patient_check", "check_answer", "decide", "patient_decision", "compare_names",
           "compare_dobs", "parse_dob", "identity_matches", "prewarm_tx_cache", "tx_reachable",
           "verify_provenance_signature", "card_facts", "reason_text_de", "REASONS_DE", "join_reasons_de", "reasons_text_de",
           "sender_text_de", "verify_jws", "policy_uri", "slug", "BASE", "POLICY", "POLICY_BASE",
           "POLICY_LAB_RESULT", "POLICY_DISCHARGE_REPORT", "POLICY_OTHER", "POLICY_REQUEST_BINDING", "POLICY_ROLE_REGISTER",
           "POLICY_ROLE_ASSERTED", "POLICY_AUTHOR_REGISTER", "POLICY_AUTHOR_ASSERTED", "POLICY_CLINICIAN_OVERRIDE",
           "POLICY_EID_SIMULATED", "attach_provenance_signature", "provenance_binding", "eid_simulated", "clinical_digest", "resources_digest",
           "SID_ORG", "SID_GDA", "SID_AERZTELISTE", "SID_PRACTITIONER_ROLE", "SID_BPK", "SID_BPK_LEGACY", "SID_MXID", "SID_REQUEST"]

# master data, patient as sender, document metadata
from .build import (SID_SIGNATORY, SID_SERVICE, document_language, document_unique_id, informant_reference,
                    patient_display, sender_is_patient, signatory_key)

__all__ += ["SID_SIGNATORY", "SID_SERVICE", "document_language", "document_unique_id", "informant_reference",
            "patient_display", "sender_is_patient", "signatory_key"]

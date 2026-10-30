"""Retention periods of the bridge, in ONE place. Enforced by Service.maintenance() on a timer
(every MAINTENANCE_EVERY_S, asyncio periodic task), not only on traffic.

What                                   Kept for                                   Then
-------------------------------------  -----------------------------------------  ----------------------------------
link token                             LINK_TTL_DAYS (7) or until filed/closed    hash replaced, link dead
session (after eID)                    SESSION_TTL_S (60 min) use,                row deleted
                                       SESSION_ROW_HOURS (24 h) row
eID birth date (identity rule v2)      inside the session row only                deleted with the session
used eID token ids (replay guard)      ASSERTION_ROW_HOURS (24 h)                 deleted (tokens expire in 5 min)
held upload (encrypted)                HELD_UPLOAD_DAYS (7)                       content deleted
filed / rejected / blocked upload      never stored after the outcome             -
upload filename                        never stored                               -
upload kept over an expired session    PENDING_UPLOAD_MIN (60) encrypted          deleted (or sent after re-login)
"request text" for the outside person  until filed or the link expires            redacted (NULL)
SMS outbox rows                        until the link expires / request closes    deleted
request contact data (address, note)   until the link expires                     redacted
contact in the service audit           cleartext beside the chain (audit_private) deleted; the chained row and the
                                       until the link expires                     FHIR AuditEvent only ever hold a
                                                                                  keyed hash + the channel type
internal note in FHIR Task.note        clinic retention (part of the Task)        - (the clinic's FHIR server)
identity memory (namesake protection)  as long as the recipient is asked           - ; per recipient name + org: the
                                                                                  pairwise eID id, first answer and
                                                                                  the birth YEAR only (never the date)
patient name / birth date              never stored in request rows (in-memory    -
                                       Patient cache, PATIENT_CACHE_S)
service audit (SQLite, cleartext who)  SERVICE_AUDIT_DAYS (1 year) - append-only, keyed chain; rotation =
                                       export + anchor of the segment (operator procedure, not automatic)
FHIR AuditEvents / filed resources     clinic retention (KAKuG: >= 30 years)      the clinic's FHIR server
"""
from __future__ import annotations

LINK_TTL_DAYS = 7
SESSION_TTL_S = 60 * 60
SESSION_ROW_HOURS = 24
ASSERTION_ROW_HOURS = 24
HELD_UPLOAD_DAYS = 7
PATIENT_CACHE_S = 600
SERVICE_AUDIT_DAYS = 365
MAINTENANCE_EVERY_S = 600
PROVENANCE_REVERIFY_S = 3600  # maintenance re-verifies every filed Provenance against HAPI hourly
PENDING_UPLOAD_MIN = 60

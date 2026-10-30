"""German hold/block reasons for Dr. Hofer (Matrix + audit) via fhirmap.reason_text_de(code, details), plus the
sender-facing texts for rejected files. Never names another patient."""
from __future__ import annotations

import fhirmap

# German reasons for Dr. Hofer come from fhirmap.reason_text_de; the service only adds texts for its
# own codes (not produced by fhirmap).
_SERVICE_DE = {
    "REQUESTED_PATIENT_MISSING": "Angefragter Patient ist am FHIR-Server nicht vorhanden",
    "PARSE_ERROR": "Dokument konnte nicht gelesen werden",
    "BUILD_NOT_POSSIBLE": "Ablegen nicht möglich: Dokument lässt sich nicht strukturiert ablegen",
    "PHOTO_OVERSIZE": "Foto oder PDF größer als 5 MB",
    "FILING_UNAVAILABLE": "Ablage im Moment nicht möglich (FHIR-Server oder Prüfung nicht erreichbar)",
}

TECHNICAL = {"UNREADABLE", "ENCODING_UNREADABLE", "EMPTY", "OVERSIZE", "PHOTO_OVERSIZE"}  # calm 'please resend', not an alarm

SENDER_DE = {
    "ENCODING_UNREADABLE": "Die Datei ist nicht im Format UTF-8 gespeichert (häufig bei älteren Windows-Exporten). Bitte im "
                           "Editor „Speichern unter … Codierung: UTF-8“ wählen und erneut senden.",
    "UNREADABLE": "Die Datei ist nicht im Format UTF-8 gespeichert oder enthält Steuerzeichen (häufig bei älteren "
                  "Windows-Exporten). Bitte im Editor „Speichern unter … Codierung: UTF-8“ wählen und erneut senden.",
    "EMPTY": "Die Datei ist leer.",
    "PHOTO_OVERSIZE": "Das Foto oder PDF ist größer als 5 MB. Bitte mit geringerer Auflösung erneut senden.",
    "OVERSIZE": "Die Datei ist größer als 1 MB. Bitte nur den Befundtext senden.",
    "ACTIVE_CONTENT": "Die Datei enthält Programmcode (z. B. <script>) und wurde aus Sicherheitsgründen abgewiesen.",
    # never says who the other person is
    "OTHER_PATIENT_PRESENT": "Das Dokument enthält Angaben zu einer weiteren Person und wurde nicht übermittelt.",
}


def reason_de(code: str, details: list[dict] | None = None) -> str:
    if code in _SERVICE_DE:
        return _SERVICE_DE[code]
    try:
        return fhirmap.reason_text_de(code, details) or code
    except Exception:
        return code


def reasons_de(codes: list[str], details: list[dict] | None = None) -> list[str]:
    out, seen = [], set()
    for c in codes:
        t = reason_de(c, details)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

"""Facts and German texts for the Matrix cards and the outside person's page (service and bot).

    reason_text_de(code, details=None) -> short German sentence for any rule or warning code
    card_facts(parsed, decision=None, resources=None) -> the clinically decisive facts of an answer

Privacy: for BLOCK codes the details are never rendered (they could name another person).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .identity import VIENNA
from .parse import _CONTACT_LINE, _signer_like

REASONS_DE = {
    "OK": "Alles geprüft: Patientin, Absender und Inhalt passen.",
    # identity and upload (BLOCK)
    "EID_MISMATCH": "Die per ID Austria angemeldete Person ist nicht die angefragte Person. Es wurde nichts angezeigt "
                    "und nichts angenommen.",
    "EMPTY": "Die hochgeladene Datei ist leer.",
    "OVERSIZE": "Die Datei ist größer als 1 MB und wurde nicht angenommen.",
    "UNREADABLE": "Die Datei enthält Steuerzeichen und ist kein lesbares Textdokument.",
    "ENCODING_UNREADABLE": "Die Datei ist kein lesbarer Text (unbekannte Zeichenkodierung oder Binärdatei). Bitte als "
                           "Text (UTF-8) speichern und erneut senden. Das ist keine Fehlzustellung.",
    "ACTIVE_CONTENT": "Das Dokument enthält aktiven Inhalt (Skript/HTML) und wurde aus Sicherheitsgründen abgewiesen.",
    "WRONG_PATIENT": "Das Dokument betrifft eine andere Person (Name oder Geburtsdatum weichen ab). Nichts abgelegt.",
    "OTHER_PATIENT_PRESENT": "Das Dokument enthält auch Daten einer anderen Person. Es kann nicht abgelegt werden; "
                             "bitte den Befund nur für die angefragte Patientin senden lassen.",
    "SVNR_MISMATCH": "Die Sozialversicherungsnummer im Dokument gehört zu einem anderen Geburtsdatum (andere Person). "
                     "Nichts abgelegt.",
    # holds (a human decides)
    "MULTIPLE_REPORTS": "Die Datei enthält mehr als einen Befund. Bitte jeden Befund einzeln senden lassen.",
    "TABLE_SPLIT": "Ergebniszeilen stehen außerhalb der Tabelle (Seitenumbruch?). Es würden Werte fehlen.",
    "FUTURE_DATE": "Probenahme- oder Befunddatum liegt in der Zukunft.",
    "ORG_MISMATCH": "Der Bericht stammt von einer anderen Einrichtung als angefragt.",
    "STALE_REPORT": "Der Bericht ist für diese Anfrage unplausibel alt (oder liegt vor dem genannten Eingriff).",
    "REPORT_INTERIM": "Das ist ein Zwischenbericht, kein Entlassungsbericht; der Aufenthalt ist nicht abgeschlossen.",
    "CORRECTION_REVIEW": "Korrigierter Befund: er ersetzt einen bereits abgelegten Befund. Beim Ablegen wird der "
                         "Ursprungsbefund als korrigiert ersetzt, nicht daneben abgelegt.",
    "PRIOR_NOT_FOUND": "Ursprungsbefund nicht gefunden: der korrigierte Befund bleibt angehalten.",
    "ACTIVE_CONTENT_SUSPECT": "Das Dokument enthält HTML, ein Bild oder einen Link, der nicht https ist. Bitte prüfen.",
    "PATIENT_MISSING": "Im Dokument fehlt die Patientenzeile (Name und Geburtsdatum).",
    "PATIENT_INCOMPLETE": "In der Patientenzeile fehlt der Name oder ein vollständiges Geburtsdatum.",
    "PATIENT_CONFLICT": "Das Dokument enthält widersprüchliche Angaben zur Patientin.",
    "NAME_NEAR": "Der Name ist ähnlich, aber nicht gleich dem der angefragten Patientin.",
    "DOB_NEAR": "Das Geburtsdatum ist ähnlich, aber nicht gleich (Zahlendreher oder Tag/Monat vertauscht?).",
    "DOB_AMBIGUOUS": "Das Geburtsdatum ist mehrdeutig (Tag/Monat oder zweistelliges Jahr nicht eindeutig).",
    "DOB_UNPARSEABLE": "Das Geburtsdatum ist kein gültiges Datum.",
    "SEX_MISMATCH": "Das Geschlecht im Dokument passt nicht zur angefragten Patientin (möglicher Fremd- oder "
                    "Mischbefund).",
    "KIND_MISMATCH": "Es wurde eine andere Dokumentart geschickt als angefragt.",
    "KIND_UNKNOWN": "Die Dokumentart ist nicht erkennbar (weder Laborbefund noch Entlassungsbericht).",
    "NO_RESULTS": "Der Laborbefund enthält keine Ergebnistabelle.",
    "UNIT_MISSING": "Bei einem Messwert fehlt die Einheit.",
    "UNKNOWN_UNIT": "Eine Einheit ist nicht eindeutig zuordenbar.",
    "LOINC_CHECKDIGIT": "Ein LOINC-Code hat eine falsche Prüfziffer (Tippfehler?).",
    "LOINC_UNIT_DIMENSION": "Eine Einheit passt nicht zum angegebenen Laborparameter (z. B. g/l bei Thrombozyten).",
    "LOINC_UNKNOWN": "Ein LOINC-Code existiert nicht.",
    "LOINC_AMBIGUOUS": "Eine Zeile enthält mehrere LOINC-Codes.",
    "VALUE_AMBIGUOUS": "Ein Wert ist mehrdeutig (z. B. 245.000: Tausenderpunkt oder Komma?).",
    "TABLE_MALFORMED": "Eine Tabellenzeile passt nicht zur Kopfzeile.",
    "FLAG_CONFLICT": "Ein Wert trägt zwei verschiedene Kennzeichen des Labors.",
    "SENDER_CONTRADICTS_EID": "Das Dokument nennt einen anderen Absender als die per ID Austria geprüfte Person.",
    "SENDER_CONFLICT": "Das Dokument nennt mehrere verschiedene Absender.",
    "IDENTITY_DOUBT": "Das Dokument zweifelt selbst an der Zuordnung (Probe falsch beschriftet / Verwechslung?). "
                      "Bitte die Identität klären, bevor abgelegt wird.",
    "EXTRACTION_DISAGREE": "KI-Lesungen weichen ab – bitte am Original prüfen.",
    "EXTRACTION_UNREADABLE": "Foto teilweise unleserlich – bitte am Original prüfen.",
    "LOINC_UNVERIFIED": "Ein LOINC-Code konnte offline nicht geprüft werden.",
    # information only (no hold)
    "REQUESTER_MISMATCH": "Hinweis: Das Dokument nennt einen anderen Einsender (z. B. die Hausärztin).",
    "EXTERNAL_RESOURCE": "Hinweis: Das Dokument verweist auf ein Bild oder einen Link; nichts wurde geladen, das "
                         "Original liegt bei.",
    "ENCODING_FALLBACK": "Hinweis: Die Datei war nicht UTF-8 und wurde als Windows-1252 gelesen. Bitte künftig als "
                         "UTF-8 speichern.",
    "REPORT_CORRECTED": "KORREKTUR: Dieser Befund ersetzt einen früheren Befund.",
    "REPORT_AMENDED": "NACHTRAG zu einem früheren Befund.",
    "REPORT_PRELIMINARY": "VORLÄUFIGER Befund: Werte können sich noch ändern.",
    "RESULT_PENDING": "Ein Wert ist noch ausständig (folgt).",
    "RESULT_NOT_DETERMINABLE": "Ein Wert war nicht bestimmbar (z. B. hämolytische Probe).",
    "RESULT_OLD": "Achtung: Der Befund ist älter als 90 Tage.",
    "DOB_TWO_DIGIT": "Hinweis: Das Geburtsjahr war zweistellig angegeben.",
    "PATIENT_EVIDENCE": "Hinweis: Das Dokument nennt außerhalb der Patientenzeile eine Person mit Geburtsdatum.",
    "SVNR_CHECKDIGIT": "Hinweis: Die Sozialversicherungsnummer hat eine ungültige Prüfziffer (Tippfehler?).",
    "SVNR_FORMAT": "Hinweis: Die Sozialversicherungsnummer hat nicht 10 Stellen.",
    "OTHER_PERSON_PRESENT": "Das Dokument enthält eine weitere Person.",
    "HIDDEN_PATIENT_LINE": "Hinweis: Eine verborgene Patientenzeile (HTML-Kommentar) wurde geprüft.",
    "TABLES_AS_TEXT": "Hinweis: Tabellen im Entlassungsbericht bleiben Text und werden nicht als Laborwerte abgelegt.",
    "TABLE_SHORT_ROW": "Hinweis: Eine Tabellenzeile hatte keine letzte (LOINC-)Zelle; sie wurde als leer gelesen.",
    "FLAG_COLUMN": "Hinweis: Kennzeichen des Labors (H/L/+/−) wurden aus einer unbeschrifteten Spalte übernommen.",
    "RESULTS_AS_LIST": "Hinweis: Die Werte standen als Aufzählung statt als Tabelle im Dokument.",
    "PATIENT_FROM_TEXT": "Hinweis: Die Patientin war ohne „Patient:“-Zeile genannt (Name mit Geburtsdatum im Text).",
    "AUTHOR_FROM_SIGNATURE": "Hinweis: Die verantwortliche Person wurde aus der Unterschriftszeile übernommen.",
}
_BLOCK = {"EID_MISMATCH", "EMPTY", "OVERSIZE", "UNREADABLE", "ENCODING_UNREADABLE", "ACTIVE_CONTENT", "WRONG_PATIENT",
          "OTHER_PATIENT_PRESENT", "SVNR_MISMATCH"}
_FIELD_DE = {"name": "Name", "birthDate": "Geburtsdatum", "gender": "Geschlecht"}
_SEX_DE = {"female": "weiblich", "male": "männlich", "other": "divers"}


def _de_date(v):
    if isinstance(v, str) and len(v) == 10 and v[4] == "-":
        return f"{v[8:10]}.{v[5:7]}.{v[:4]}"
    return _SEX_DE.get(v, v) if isinstance(v, str) else v


# which detail entries belong to which code
_CODE_FIELDS = {"NAME_NEAR": {"name"}, "PATIENT_INCOMPLETE": {"name", "birthDate"}, "DOB_NEAR": {"birthDate"},
                "DOB_AMBIGUOUS": {"birthDate"}, "DOB_UNPARSEABLE": {"birthDate"}, "SEX_MISMATCH": {"gender"}}


def _detail_text(code: str, d: dict) -> str | None:
    f = d.get("field")
    if f in ("name", "birthDate", "gender"):
        doc = d.get("document_de") or _de_date(d.get("document"))
        req = d.get("request_de") or _de_date(d.get("request"))
        return f"{_FIELD_DE.get(f, f)}: Dokument {doc or '–'} · Anfrage {req or '–'}"
    if f == "row":
        row, unit, loinc, exp = d.get("row"), d.get("unit"), d.get("loinc"), d.get("expected")
        if code == "LOINC_UNIT_DIMENSION":
            return (f"{row}: Einheit '{unit or '–'}' passt nicht zu LOINC {loinc}"
                    + (f", erwartet z. B. {exp}" if exp else ""))
        if code == "UNIT_MISSING":
            return f"{row}: Wert {d.get('value')} ohne Einheit" + (f", erwartet z. B. {exp}" if exp else "")
        if code == "UNKNOWN_UNIT":
            return f"{row}: Einheit '{unit}' ist nicht zuordenbar"
        if code in ("LOINC_CHECKDIGIT", "LOINC_UNKNOWN", "LOINC_AMBIGUOUS"):
            return f"{row}: LOINC {loinc or '?'}"
        if code == "VALUE_AMBIGUOUS":
            return f"{row}: Wert '{d.get('value')}'"
        return f"{row}"
    if f == "sender":
        return f"Gesendet von: {d.get('document')}"
    if f == "text" and d.get("document"):
        return f"Dokument: „{d['document']}“"
    if f == "replaces":
        return "ersetzt Befund vom " + (d.get("replaces") or "?")
    if d.get("other_person_present"):
        return "Dokument enthält eine weitere Person"
    return None


def reason_text_de(code: str, details=None) -> str:
    """German text for a rule/warning code. Only the details that belong to ``code`` are appended
    ('… (Quick: Einheit '%' passt nicht zu LOINC 5902-2 (erwartet z. B. s)).'); for BLOCK codes only the
    'weitere Person' flag, never another person's data."""
    base = REASONS_DE.get(code) or f"Prüfhinweis ({code})."
    items = [d for d in (details if isinstance(details, list) else [details] if details else [])
             if isinstance(d, dict)]
    if code in _BLOCK:
        items = [d for d in items if d.get("other_person_present")]
    else:
        items = [d for d in items if d.get("code") == code or d.get("field") in _CODE_FIELDS.get(code, set())
                 or d.get("other_person_present")]
    if code in ("EXTRACTION_DISAGREE", "EXTRACTION_UNREADABLE"):   # 'KI-Lesungen weichen ab: INR 1.0 vs 1.8 – ...'
        k = next((d.get("document") for d in items if d.get("field") == "extraction" and d.get("document")), None)
        if not k:
            return base
        return (f"KI-Lesungen weichen ab: {k} – bitte am Original prüfen." if code == "EXTRACTION_DISAGREE"
                else f"{k} – bitte am Original prüfen." if k.startswith("KI-Lesung") else
                f"KI-Lesung: {k} – bitte am Original prüfen.")
    if code == "DOB_AMBIGUOUS":                   # 'Geburtsdatum mehrdeutig geschrieben (12/03/1961) – bitte prüfen.'
        k = next((d for d in items if d.get("field") == "birthDate" and d.get("document_text")), None)
        if k and __import__("re").fullmatch(r"\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{2}\s*", k["document_text"]):
            return f"Geburtsjahr zweistellig ({k['document_text'].strip()}) – bitte prüfen."
        if k:
            return f"Geburtsdatum mehrdeutig geschrieben ({k['document_text']}) – bitte prüfen."
    if code == "ORG_MISMATCH":                    # 'Labor laut Dokument: <org> – angefragt: <requested org>'
        k = next((d for d in items if d.get("field") == "org" and d.get("document")), None)
        if k:
            lab = k.get("kind") == "lab_result"
            head = "Der Befund stammt aus einem anderen Labor als angefragt" if lab else base.rstrip(". ")
            return (f"{head}: {'Labor' if lab else 'Einrichtung'} laut Dokument: {k['document']}"
                    + (f" – angefragt: {k['request']}" if k.get("request") else "") + ".")
        return base
    if code == "KIND_MISMATCH":                   # 'Laborbefund statt Entlassungsbericht geschickt.'
        k = next((d for d in items if d.get("field") == "kind" and d.get("detected_de") and d.get("requested_de")),
                 None)
        return f"{k['detected_de']} statt {k['requested_de']} geschickt." if k else base
    extra = [t for t in (_detail_text(code, d) for d in items) if t]
    if not extra:
        return base
    base = __import__("re").sub(r"\s*\((?:z\.\s*B\.|e\.g\.)[^)]*\)", "", base)   # the real row replaces the example
    return base.rstrip(". ") + ": " + "; ".join(dict.fromkeys(extra)) + "."


def join_reasons_de(texts) -> str:
    """Several reason sentences as one clean text: each ends with exactly one period, no '.;' or '..'."""
    out = []
    for t in texts or []:
        t = (t or "").strip().rstrip(";").strip()
        if not t:
            continue
        t = t.rstrip(".") + "."
        if t not in out:
            out.append(t)
    return " ".join(out)


def reasons_text_de(decision: dict) -> str:
    """The whole decision as one German text for a card."""
    return join_reasons_de(reason_text_de(c, decision.get("details")) for c in decision.get("codes", []) if c != "OK")


SENDER_TEXTS_DE = {
    "OTHER_PATIENT_PRESENT": "Das Dokument enthält Angaben zu einer weiteren Person und wurde nicht übermittelt. "
                             "Bitte senden Sie einen Befund, der nur die angefragte Person betrifft.",
    "WRONG_PATIENT": "Das Dokument betrifft nicht die angefragte Person und wurde nicht übermittelt.",
    "SVNR_MISMATCH": "Die Sozialversicherungsnummer im Dokument passt nicht zur angefragten Person. Das Dokument "
                     "wurde nicht übermittelt.",
    "ACTIVE_CONTENT": "Das Dokument enthält aktive Inhalte (Skripte) und wurde aus Sicherheitsgründen nicht "
                      "angenommen. Bitte als reinen Text (Markdown) senden.",
    "ENCODING_UNREADABLE": "Die Datei ist kein lesbarer Text. Bitte als Text (UTF-8) speichern und erneut senden.",
    "UNREADABLE": "Die Datei enthält Steuerzeichen. Bitte als reinen Text speichern und erneut senden.",
    "EMPTY": "Die Datei ist leer. Bitte den Befund erneut auswählen.",
    "OVERSIZE": "Die Datei ist größer als 1 MB. Bitte nur den Befund als Text senden.",
    "EID_MISMATCH": "Diese Anfrage ist an eine andere Person gerichtet. Es wurde nichts angezeigt.",
    "HOLD": "Vielen Dank. Ihre Antwort wurde übermittelt und wird von der anfragenden Ärztin geprüft.",
    "FILE": "Vielen Dank. Ihre Antwort wurde übermittelt.",
}


def sender_text_de(decision: dict) -> str:
    """What the OUTSIDE person sees after uploading. Safe: it only describes their own file and never
    names the requested patient or anyone else."""
    out = decision.get("outcome")
    if out == "BLOCK":
        for c in decision.get("codes", []):
            if c in SENDER_TEXTS_DE:
                return SENDER_TEXTS_DE[c]
        return "Das Dokument konnte nicht übermittelt werden."
    return SENDER_TEXTS_DE["HOLD" if out == "HOLD" else "FILE"]


STATUS_DE = {"final": "endgültig", "preliminary": "vorläufig", "corrected": "korrigiert", "amended": "ergänzt",
             "registered": "ausständig"}
_LABEL = __import__("re").compile(r"^\s*(kommentar|bemerkung|bemerkungen|hinweis|anmerkung|note|comment|befundkommentar)"
                                  r"\s*:\s*", __import__("re").I)
_RECO = __import__("re").compile(r"empfehl|\bempf\.|recommend|procedere|weiteres vorgehen|therapievorschl|follow",
                                 __import__("re").I)
_MEDS = __import__("re").compile(r"medika|medication|medicine|arznei|entlass\w*therapie|therapie bei entlassung|drugs|"
                                 r"\bmed\.\s*(?:bei|b\.|zur)?\s*entl|"
                                 r"^\s*(?:aktuelle |empfohlene |weitere |laufende )?(?:dauer)?therap(?:ie|y)\s*:?\s*$",
                                 __import__("re").I)


def _value_text(row: dict) -> str | None:
    if row.get("status") == "registered":
        return row.get("value_text") or "folgt"
    if row.get("absent"):
        return row.get("value_text") or "nicht bestimmbar"      # the lab's words, e.g. '(hämolytisch)'
    return row.get("value_text") or (None if row.get("value") is None else str(row.get("value")))


def _flag_shown(row: dict, value_text: str | None) -> str | None:
    """The lab's flag, unless the value text already carries it ('4,6*', '1,3 +')."""
    ft = row.get("flag_text") or row.get("flag")
    if not ft:
        return None
    vt = (value_text or "").strip()
    if row.get("flag_text") and vt.endswith(row["flag_text"].strip()):
        return None
    return ft


def card_facts(parsed, decision: dict | None = None, resources: list | None = None) -> dict:
    """What a clinician must see in 3 seconds: patient, sample time and age, report status (German),
    Einsender, every value as the lab wrote it (comparator and flag included, a unit only with a number),
    the lab's comment once (label stripped), for discharge reports the stay, the discharge medication as written
    and the first recommendations, and the warnings with German texts."""
    p = parsed
    sample = p.sample_time
    age_days = None
    if sample and "T" in sample:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(sample)).days
    warn_codes = [f["code"] for f in p.findings if f["outcome"] == "WARN" and f["code"] in REASONS_DE]
    if age_days is not None and age_days > 90:
        warn_codes.append("RESULT_OLD")
    obs = []
    for r in p.lab:
        vt = _value_text(r)
        numeric = r.get("value") if isinstance(r.get("value"), (int, float)) else None
        obs.append({"test": r.get("test"), "value": vt, "comparator": r.get("comparator"), "numeric": numeric,
                    "unit": r.get("unit") if numeric is not None else None,     # never 'folgt g/l'
                    "ref": r.get("ref_text"), "flag": _flag_shown(r, vt), "flag_code": r.get("flag"),
                    "status": r.get("status") or ("preliminary" if p.report_status == "preliminary" else "final"),
                    "status_de": STATUS_DE.get(r.get("status") or "", None),
                    "loinc": r.get("loinc"), "notes": [n for n in (r.get("notes") or []) if n != vt]})
    rq = p.requester or {}
    comment = _LABEL.sub("", p.comment, count=1).strip() if p.comment else None
    facts = {
        "kind": p.kind, "title": p.title, "report_status": p.report_status,
        "report_status_de": STATUS_DE.get(p.report_status, p.report_status), "replaces": p.replaces,
        "replaces_time": p.replaces_time,
        "patient": {"name": (p.patient or {}).get("name"), "birthDate": (p.patient or {}).get("birthDate"),
                    "gender": (p.patient or {}).get("gender")},
        "sample_time": sample, "sample_time_de": _de_dt(sample), "sample_age_days": age_days,
        "report_time": p.report_time, "report_time_de": _de_dt(p.report_time),
        "einsender": ", ".join(x for x in (rq.get("name"), rq.get("org")) if x) or None,
        "author": p.author, "sender_line": p.sender, "organisation": (p.org or {}).get("name"),
        "comment": comment, "observations": obs,
        "warnings": [{"code": c, "text": reason_text_de(c)} for c in dict.fromkeys(warn_codes)],
        "encoding": p.encoding,
    }
    if p.stay:
        facts["stay"] = p.stay
        facts["stay_de"] = (f"{_de_dt(p.stay.get('start'))} – {_de_dt(p.stay.get('end'))}"
                            if p.stay.get("start") else p.stay.get("text"))
    recos, meds = [], []
    for sec in p.sections or []:
        title = sec.get("title") or ""
        into = meds if _MEDS.search(title) else recos if _RECO.search(title) else None
        if into is None:
            if p.kind == "discharge_report":       # '**Entlassmedikation:** ...' inside another section
                for ln in (sec.get("text") or "").split("\n"):
                    _inline_list(ln, meds, recos)
            continue
        for t in _section_items(sec.get("text") or ""):
            if __import__("re").match(r"^(?:gez\.|i\.\s?a\.|f\.\s?d\.\s?r\.|versand|sent (?:on behalf|by))", t,
                                      __import__("re").I) or _signer_like(t) or _CONTACT_LINE.search(t):
                break                              # the signature / contact block ends the list
            lab_ = t.split(":")[0] if __import__("re").match(r"^[^:|]{2,40}:\s*\S", t) else None
            if lab_ and (_MEDS.search(lab_) or _RECO.search(lab_)):
                _inline_list(t, meds, recos)       # 'Empf.: Heimübungen, …' after the medication list
                into = None
                continue
            if into is not None:
                into.append(t)
    if p.kind == "discharge_report":               # bold inline sections in the header ('**Entlassmedikation:** A; B')
        for k, v in (p.extra or {}).items():
            if isinstance(k, str) and isinstance(v, str):
                _inline_list(f"{k}: {v}", meds, recos)
    if meds:
        facts["medications"] = meds[:6]
    if recos:
        facts["recommendations"] = recos[:3]
    if decision:
        facts["outcome"] = decision.get("outcome")
        facts["reasons_de"] = [reason_text_de(c, decision.get("details")) for c in decision.get("codes", [])
                               if c != "OK"]
        facts["reasons_text_de"] = reasons_text_de(decision)
        facts["sender_text_de"] = sender_text_de(decision)
        facts["other_person_present"] = bool(decision.get("other_person_present"))
    return facts


def _section_items(text: str) -> list[str]:
    """A section's lines as card items: list markers dropped; a Markdown table becomes one item per row
    ('Rivaroxaban 10 mg 1-0-0 bis 29.09.2026'), header and separator rows skipped."""
    import re
    lines = text.split("\n")
    sep = lambda s: bool(re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", s)) and "-" in s
    out = []
    for k, ln in enumerate(lines):
        if "|" in ln and ln.strip():
            if sep(ln) or (k + 1 < len(lines) and sep(lines[k + 1])):
                continue
            cells = [c.strip().strip("*_") for c in ln.strip().strip("|").split("|")]
            t = " ".join(c for c in cells if c)
        else:
            t = re.sub(r"^\s*(?:[-*•+]|\d+[.)])\s+", "", ln).strip()
        t = t.replace("**", "").strip()
        if t:
            out.append(t)
    return out


def _inline_list(line: str, meds: list, recos: list) -> None:
    """'Entlassmedikation: A; B; C' / 'Weiteres Vorgehen: X. Y.' -> items of the medication / recommendation list."""
    import re
    m = re.match(r"^\s*(?:[-*•+]\s+)?\**([^:*]{2,40}?)\**\s*:\**\s*(.+)$", line.replace("**", ""))
    if not m:
        return
    if _MEDS.search(m[1]):
        meds.extend(x.strip(" .") for x in re.split(r";\s*", m[2]) if x.strip(" ."))
    elif _RECO.search(m[1]):
        recos.extend(x.strip(" ") for x in re.split(r";\s*|(?<=\.)\s+", m[2]) if x.strip(" ."))


def _de_dt(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return v
    if "T" not in v:
        return dt.strftime("%d.%m.%Y")
    return dt.astimezone(VIENNA).strftime("%d.%m.%Y %H:%M")

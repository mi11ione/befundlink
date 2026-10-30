import unicodedata

import pytest

from bot.parse import (LAB, REPORT, classify_contact, local_address_warnings, parse_command, parse_kind,
                       parse_request_args, yes_no)

EVA = dict(patient_id="maria-beispiel", doc_kind=LAB, name="Dr. Eva Brandl", role="Laborärztin",
           org="Labor Kanalstraße", channel="email", address="eva.brandl@labor-kanal.example")
STEFAN = dict(patient_id="maria-beispiel", doc_kind=REPORT, name="Stefan Kern", role="Entlassungsmanagement",
              org="Reha Waldblick", channel="sms", address="+43 660 000 0101")


def check(cmd: str, exp: dict):
    c = parse_command(cmd)
    assert c and c.name == "request", c
    d = parse_request_args(c.args, c.lang)
    got = {k: getattr(d, k) for k in exp}
    assert got == exp, (cmd, d)
    assert not d.problems and not d.missing()
    return d


@pytest.mark.parametrize("cmd", [
    "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient maria-beispiel",
    "!anfordern   labor   Dr.  Eva Brandl ,  Laborärztin,Labor Kanalstraße ,   eva.brandl@labor-kanal.example ,  Patient   maria-beispiel  ",
    "!Anfordern Labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example Patient: maria-beispiel",
    "!anfordern labor Dr. Eva Brandl; Laborärztin; Labor Kanalstraße; E-Mail: eva.brandl@labor-kanal.example; Patient maria-beispiel",
    "!anfordern Laborbefund Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, <eva.brandl@labor-kanal.example>, Patient maria-beispiel",
    "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, mailto:eva.brandl@labor-kanal.example, Patient maria-beispiel",
    # patient first, kind as a field
    "!anfordern Patient maria-beispiel, labor, Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example",
    # email glued to org without comma
    "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße eva.brandl@labor-kanal.example, Patient maria-beispiel",
    # contact before org
    "!anfordern labor Dr. Eva Brandl, Laborärztin, eva.brandl@labor-kanal.example, Labor Kanalstraße, Patient maria-beispiel",
    # non-breaking spaces and a reply fallback
    "> <@lena.hofer:klinikum.example> alt\n\n!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient maria-beispiel",
    # key/value form
    "!anfordern labor Name: Dr. Eva Brandl, Rolle: Laborärztin, Org: Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient maria-beispiel",
])
def test_request_1_variants(cmd):
    check(cmd, EVA)


def test_request_1_decomposed_umlauts():
    """macOS can send NFD (a + combining diaeresis); must normalise to NFC."""
    cmd = unicodedata.normalize("NFD", "!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, "
                                       "eva.brandl@labor-kanal.example, Patient maria-beispiel")
    assert "ä" in cmd
    check(cmd, EVA)


@pytest.mark.parametrize("phone", [
    "+43 660 000 0101", "+436600000101", "+43 660 0000101", "0043 660 000 0101", "0660 000 0101",
    "0660/0000101", "+43 (0) 660 000 0101", "+43-660-000-0101", "+43 660 000 0101",
    "Tel.: +43 660 000 0101", "06600000101",
])
def test_request_2_phone_variants(phone):
    check(f"!anfordern reha Stefan Kern, Entlassungsmanagement, Reha Waldblick, {phone}, Patient maria-beispiel", STEFAN)


@pytest.mark.parametrize("cmd", [
    "!anfordern reha Stefan Kern, Entlassungsmanagement, Reha Waldblick, +43 660 000 0101, Patient maria-beispiel",
    "!anfordern Entlassungsbericht Stefan Kern, Entlassungsmanagement, Reha Waldblick, +43 660 000 0101, Patient maria-beispiel",
    "!anfordern reha Stefan Kern, Entlassungsmanagement, Reha Waldblick +43 660 000 0101, Patient maria-beispiel",
    "!anfordern reha Stefan Kern, Entlassungsmanagement, Reha Waldblick, +43 660 000 0101 Patient maria-beispiel",
    "!anfordern reha Stefan Kern, Entlassungsmanagement, Reha Waldblick, +43 660 000 0101, für Patientin maria-beispiel",
])
def test_request_2_variants(cmd):
    check(cmd, STEFAN)


def test_english():
    d = check("!request lab Dr. Eva Brandl, lab physician, Labor Kanalstraße, eva.brandl@labor-kanal.example, patient maria-beispiel",
              {**EVA, "role": "lab physician"})
    assert parse_command("!request lab x").lang == "en"
    check("!request report Stefan Kern, discharge manager, Reha Waldblick, +43 660 000 0101, patient maria-beispiel",
          {**STEFAN, "role": "discharge manager"})
    assert d.doc_kind == LAB


def test_confusable_kept_verbatim_and_flagged():
    d = check("!anfordern labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanaI.example, Patient maria-beispiel",
              {**EVA, "address": "eva.brandl@labor-kanaI.example"})
    w = local_address_warnings(d.channel, d.address)
    assert w and w[0]["message"] == ("Achtung: 'labor-kanaI.example' enthält ein großes I statt l – "
                                     "meinten Sie labor-kanal.example?")
    assert w[0]["suggestion"] == "eva.brandl@labor-kanal.example"
    assert local_address_warnings("email", "eva.brandl@labor-kanal.example") == []
    assert local_address_warnings("email", "Ivana.Huber@labor-kanal.example") == []   # capital first letter ok
    assert local_address_warnings("email", "eva.brandI@labor-kanal.example")          # mid-word I in local part


def test_patient_name_slug():
    d = parse_request_args("labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, Patient Maria Beispiel")
    assert d.patient_id == "maria-beispiel" and d.patient_from_name
    d = parse_request_args("labor A, B, C, a@b.example, Patient Jürgen Größ")
    assert d.patient_id == "juergen-groess"


def test_role_with_patient_word_is_not_patient():
    d = parse_request_args("reha Anna Muster, Patientenmanagement, Reha Waldblick, +43 660 000 0101, Patient maria-beispiel")
    assert d.role == "Patientenmanagement" and d.patient_id == "maria-beispiel"


def test_extra_fields_go_to_org_and_note():
    d = parse_request_args("labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, Wien, eva.brandl@labor-kanal.example, Patient maria-beispiel, Notiz: vor OP")
    assert d.org == "Labor Kanalstraße, Wien" and d.note == "vor OP"


def test_missing_and_problems():
    d = parse_request_args("labor Dr. Eva Brandl, Laborärztin")
    assert d.missing() == ["patient", "org", "contact"]
    d = parse_request_args("reha Stefan Kern, Entlassungsmanagement, Reha Waldblick, +49 151 1234567, Patient maria-beispiel")
    assert d.missing() == ["contact"] and "keine österreichische" in d.problems[0]
    d = parse_request_args("reha Stefan Kern, Entlassungsmanagement, Reha Waldblick, +43 1 40400 1234, Patient maria-beispiel")
    assert d.missing() == ["contact"] and "Festnetz" in d.problems[0]
    d = parse_request_args("Stefan Kern, Entlassungsmanagement, Reha Waldblick, +43 660 000 0101, Patient maria-beispiel")
    assert d.missing() == ["doc_kind"]


def test_contact_classifier():
    assert classify_contact("eva@x.example").kind == "email"
    assert classify_contact("eva@x").kind == "invalid"
    assert classify_contact("+43 664 1234567").value == "+43 664 123 4567"
    assert classify_contact("+1 555 1234567").kind == "foreign_phone"
    assert classify_contact("hallo").kind == "invalid"
    assert classify_contact("12345").kind == "invalid"


def test_commands():
    assert parse_command("hallo") is None
    assert parse_command("!hilfe").name == "help"
    assert parse_command("!help").lang == "en"
    assert parse_command("!status abc-123").args == "abc-123"
    assert parse_command("!audit abc").name == "audit"
    assert parse_command("!Ablegen x y").name == "file" and parse_command("!verwerfen x").name == "reject"
    assert parse_command("!foo").name == "unknown"
    assert parse_kind("2") == REPORT and parse_kind("Labor") == LAB and parse_kind("xyz") is None
    assert yes_no("Ja") == "yes" and yes_no("!nein") == "no" and yes_no("vielleicht") is None


def test_request_text_and_note_with_commas():
    d = parse_request_args("labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, eva.brandl@labor-kanal.example, "
                           "Patient maria-beispiel, Text: aktuellster Gerinnungsbefund, Eingriff am 31.10., "
                           "Notiz: intern, bitte nicht weitergeben")
    assert d.request_text == "aktuellster Gerinnungsbefund, Eingriff am 31.10."
    assert d.note == "intern, bitte nicht weitergeben"
    assert d.org == "Labor Kanalstraße" and d.patient_id == "maria-beispiel" and not d.problems
    d = parse_request_args("labor Dr. Eva Brandl, Laborärztin, Labor Kanalstraße, Frage: INR, aPTT, Patient maria-beispiel, "
                           "eva.brandl@labor-kanal.example")
    assert d.request_text == "INR, aPTT" and d.patient_id == "maria-beispiel" and d.address == "eva.brandl@labor-kanal.example"
    body = d.to_body("!r:x", "@a:x")
    assert body["request_text"] == "INR, aPTT" and "note" not in body and "internal_note" not in body

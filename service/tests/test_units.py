"""Unit tests: look-alike addresses, name normalisation, identity relation, dates, patient check, PII scanner."""
import json
import re

import pytest

import tempfile
from pathlib import Path

import fhirmap
from service import lookalike, messages
from service.config import Settings
from service.core import Service

from .conftest import FIX, doc

KNOWN = ["labor-kanal.example", "reha-waldblick.example", "klinikum.example"]
MARIA = json.loads((FIX / "patient.json").read_text())


def codes(addr, org_domains=None, org=None):
    return {w.code for w in lookalike.check_email(addr, KNOWN, org_domains, org)}


# ---------------------------------------------------------------- look-alike
def test_capital_I_warns_against_labor_kanal():
    ws = lookalike.check_email("eva.brandl@labor-kanaI.example", KNOWN, ["labor-kanal.example"], "Labor Kanalstraße")
    c = {w.code for w in ws}
    assert "confusable" in c and "uppercase_in_domain" in c
    assert ws[0].code == "confusable" and ws[0].similar_to == "labor-kanal.example"  # bot's correction suggestion
    assert all(w.similar_to in (None, "labor-kanal.example") for w in ws)
    assert any("labor-kanai.example" in w.message for w in ws)  # the DNS reality


def test_lowercase_i_also_warns():  # domains are case-insensitive: labor-kanaI == labor-kanai
    assert "confusable" in codes("eva.brandl@labor-kanai.example")


def test_exact_known_domain_is_clean():
    assert codes("eva.brandl@labor-kanal.example", ["labor-kanal.example"], "Labor Kanalstraße") == set()


@pytest.mark.parametrize("addr", [
    "x@lab0r-kanal.example",          # 0 vs o
    "x@labor-kana1.example",          # 1 vs l
    "x@klinikurn.example",            # rn vs m
    "x@labor-kanal.exarnple",         # rn vs m in TLD part
])
def test_homoglyphs(addr):
    assert "confusable" in codes(addr)


def test_edit_distance_two():
    c = codes("x@labor-kanl.exmple")
    assert "lookalike" in c


def test_mixed_script_cyrillic():
    c = codes("eva@lаbor-kanal.example")  # Cyrillic а
    assert "mixed_script" in c and "confusable" in c and "non_ascii" in c


def test_unknown_domain_for_known_org():
    assert codes("eva@gmail.com", ["labor-kanal.example"], "Labor Kanalstraße") == {"unknown_domain_for_org"}


def test_unrelated_domain_without_org_is_clean():
    assert codes("someone@totally-different.example") == set()


# ---------------------------------------------------------------- eID binding rule (fhirmap.identity_matches, binary)
@pytest.mark.parametrize("given,family,ok", [
    ("Eva", "Brandl", True), ("Eva Maria", "Brandl", True), ("EVA", "BRANDL", True),   # extra given name allowed
    ("Eva", "Brandl-Huber", False), ("Thomas", "Huber", False), ("Petra", "Fremd", False), ("Eve", "Brandl", False),
])
def test_eid_binding_rule(given, family, ok):
    assert fhirmap.identity_matches("Dr. Eva Brandl", given, family) is ok


# ---------------------------------------------------------------- patient check (document vs requested)
SVC = Service(Settings(db_path=str(Path(tempfile.mkdtemp()) / "u.sqlite"), data_key_hex="33" * 32, prewarm_tx=False, dev=True, async_io=False, validator_server=False))
REQ = {"id": "req-u", "ref_code": "KB-U", "recipient_name": "Dr. Eva Brandl", "recipient_org": "Labor Kanalstraße",
       "doc_kind": "lab_result", "requester_mxid": "@lena.hofer:klinikum.example"}


class _V:  # the service's decision (fhirmap.decide) for one uploaded document, as the tests read it
    def __init__(self, a):
        self.verdict = {"file": "match"}.get(a["outcome"], a["outcome"])
        self.reasons, self.codes = a["reasons"], a["codes"]


def pc_for(text):
    return _V(SVC.assess(text.encode(), REQ, {"given": "Eva", "family": "Brandl"}, MARIA))


def test_patient_match():
    assert pc_for(doc("lab-result-maria-beispiel.md")).verdict == "match"


def test_wrong_patient_blocked_without_naming_them():
    pc = pc_for(doc("lab-result-max-beispiel.md"))
    assert pc.verdict == "block"
    blob = " ".join(pc.reasons)
    assert "Max" not in blob and "1960" not in blob and "01.01" not in blob
    assert "different person" in blob


LAB = doc("lab-result-maria-beispiel.md")


@pytest.mark.parametrize("line,why", [
    ("Patient: Maria Beispiel, born 3 December 1961", "day/month swap"),
    ("Patient: Maria Beispiel, born 13 March 1961", "one digit"),
    ("Patient: Maria Beispel, born 12 March 1961", "one-letter typo"),
    ("Patient: Maria Beispiel-Huber, born 12 March 1961", "hyphenated surname"),
    ("Patient: Maria Anna Beispiel, born 12 March 1961", "extra middle name"),
])
def test_near_miss_is_held_not_filed(line, why):
    pc = pc_for(LAB.replace("Patient: Maria Beispiel, born 12 March 1961", line))
    assert pc.verdict == "hold", why


def test_missing_patient_line_held():
    assert pc_for(LAB.replace("Patient: Maria Beispiel, born 12 March 1961\n", "")).verdict == "hold"


def test_patient_line_without_dob_held():
    assert pc_for(LAB.replace(", born 12 March 1961", "")).verdict == "hold"


def test_conflicting_patient_lines_held():
    t = LAB.replace("Requested by:", "Patient: Max Beispiel, born 1 January 1960\nRequested by:")
    assert pc_for(t).verdict in ("hold", "block")
    assert pc_for(t).verdict != "match"


def test_namesake_with_other_birthdate_blocked():
    assert pc_for(LAB.replace("born 12 March 1961", "born 4 July 1985")).verdict == "block"


def test_family_first_and_german_date_match():
    assert pc_for(LAB.replace("Patient: Maria Beispiel, born 12 March 1961", "Patientin: Beispiel, Maria, geb. 12.03.1961")).verdict == "match"


@pytest.mark.parametrize("line,expect", [   # RULES.md name/DOB judgement calls
    ("Patient: Beispiel Maria, born 12 March 1961", "match"),        # surname first: exactly the tokens + exact full DOB
    ("Patient: Beispiel Maria, born 12 March 1962", "hold"),        # ... but not with a near birth date
    ("Patient: Beispiel, Maria, born 12 March 1961", "match"),      # comma = explicit family-first
    ("Patient: Märia Beispiel, born 12 March 1961", "hold"),        # ä is ae (ICAO), not a
    ("Patient: María Beispiel, born 12 March 1961", "match"),       # accent = encoding artefact
    ("Patient: Mаria Beispiel, born 12 March 1961", "hold"),        # Cyrillic а homoglyph
    ("Patient: Maria Theresia Beispiel, born 12 March 1961", "hold"),
    ("Patient: Maria Huber, born 12 March 1961", "block"),
    ("Patient: Maria Beispiel, born 12/03/1961", "hold"),           # ambiguous slash date
    ("Patient: Maria Beispiel, born 13/03/1961", "hold"),           # unambiguous but one digit off
    ("Patient: Maria Beispiel, born 31.02.1961", "hold"),           # not a real date
    ("Patient: Maria Beispiel, 64 years", "hold"),                  # an age is not a DOB
    ("Patient: Frau Mag.a MARIA BEISPIEL, geb. 12.03.1961", "match"),
])
def test_rules_md_patient_cases(line, expect):
    assert pc_for(LAB.replace("Patient: Maria Beispiel, born 12 March 1961", line)).verdict == expect


def test_patient_line_hidden_in_html_comment_is_never_filed():
    t = LAB.replace("Requested by:", "<!-- Patient: Max Beispiel, born 1 January 1960 -->\nRequested by:")
    assert pc_for(t).verdict == "block"   # another person present -> BLOCK


def test_separate_date_of_birth_line_counts():
    t = LAB.replace("Patient: Maria Beispiel, born 12 March 1961", "Patient: Maria Beispiel\nDate of birth: 12.03.1961")
    assert pc_for(t).verdict == "match"


# ---------------------------------------------------------------- PII-free messages
FORBIDDEN_WORDS = ["Labor", "Befund", "Lab", "Reha", "Entlassung", "discharge", "report", "result"]
DOB_FORMATS = ["1961-03-12", "12.03.1961", "12.3.1961", "12/03/1961", "12 March 1961", "12. März 1961", "19610312",
               "03/12/1961", "120361"]


def assert_clean(text):
    low = text.casefold()
    for tok in ["maria", "maria beispiel", "maria-beispiel"]:
        assert tok not in low, tok
    import re
    assert not re.search(r"(?<!\w)beispiel(?!\w)", low)  # 'Beispielstadt' (the clinic) is fine, the surname is not
    for d in DOB_FORMATS:
        assert d.casefold() not in low, d
    for w in FORBIDDEN_WORDS:
        assert w.casefold() not in low, w
    for w in ["kanalstraße", "kanalstrasse", "waldblick"]:  # recipient org names leak the doc type
        assert w not in low, w


def test_email_and_sms_text_is_pii_free():
    from service.core import TOKEN_ALPHABET, TOKEN_LEN, TOKEN_RE
    import secrets
    link = "http://localhost:8000/r/" + "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))
    assert TOKEN_RE.fullmatch(link.rsplit("/", 1)[1])
    subj, body = messages.build_email("Klinikum Beispielstadt", link, "2026-11-05T10:00:00+00:00", "KB-7QX4MZ",
                                      "Dr. Lena Hofer", "Innere Medizin", "+43 1 000 00 00", "localhost:8000")
    assert "Brandl" not in body                 # the recipient is never named (could be the patient herself)
    sms = messages.build_sms("Klinikum Beispielstadt", link, "2026-11-05T10:00:00+00:00", "KB-7QX4MZ", "Dr. Hofer",
                             "Innere Med.", "+43 1 000 00 00")
    assert_clean(sms) and None
    assert "Dr. Lena Hofer (Innere Medizin, Klinikum Beispielstadt) bittet Sie persönlich um eine Rückmeldung" in body
    assert "Gültig bis 05.11.2026" in body and "+43 1 000 00 00" in body
    assert_clean(subj + "\n" + body)
    assert_clean(messages.build_sms("Klinikum Beispielstadt", link, "2026-11-05T10:00:00+00:00", "KB-7QX4MZ"))


def test_email_html_part_same_content_and_pii_free():
    link = "http://localhost:8000/r/" + "b" * 60
    args = ("Klinikum Beispielstadt", link, "2026-11-05T10:00:00+00:00", "KB-7QX4MZ", "Dr. Lena Hofer", "Innere Medizin",
            "+43 1 000 00 00", "localhost:8000")
    _, body = messages.build_email(*args)
    html = messages.build_email_html(*args)
    assert_clean(html)                                           # raw markup, strictest
    text = messages.html_scan_text(html)
    assert_clean(text) and None
    for same in ("Dr. Lena Hofer (Innere Medizin, Klinikum Beispielstadt) bittet Sie persönlich um eine Rückmeldung",
                 "Gültig bis 05.11.2026", "KB-7QX4MZ", "+43 1 000 00 00", "localhost:8000", "Nur Sie können die Anfrage öffnen"):
        assert same in body and same in text, same
    assert "Brandl" not in html and "src=" not in html and "<img" not in html   # no recipient, no remote content
    assert set(re.findall(r'https?://[^"\s<]+', html)) == {link}                # the link is the only URL


def test_pii_guard_detects_leaks():
    f = messages.pii_findings("Befund für Maria Beispiel, geb. 12.03.1961", "Maria Beispiel", "1961-03-12")
    assert any("Maria" in x for x in f) and any("12.03.1961" in x for x in f) and any("befund" in x for x in f)
    assert messages.pii_findings("Klinikum Beispielstadt bittet um Rückmeldung", "Maria Beispiel", "1961-03-12") == []


def test_dob_variants_cover_six_plus_formats():
    v = messages.dob_variants("1961-03-12")
    for d in DOB_FORMATS:
        assert d in v


def test_link_token_cannot_carry_dates_or_words():
    import math
    from service.core import TOKEN_ALPHABET, TOKEN_LEN
    assert not set(TOKEN_ALPHABET) & set("0123456789aeiouy")
    assert TOKEN_LEN * math.log2(len(TOKEN_ALPHABET)) >= 128


def test_demo_pins_file_matches_personas():
    import json
    from pathlib import Path
    d = Path(__file__).resolve().parents[1] / "data"
    personas = json.loads((d / "personas.json").read_text())["personas"]
    pins = json.loads((d / "demo_pins.json").read_text())
    from collections import Counter
    names = Counter(f"{p['given']} {p['family']}" for p in personas)
    assert all(pins[p["key"]] == p["pin"] for p in personas)
    assert all(pins.get(f"{p['given']} {p['family']}") == p["pin"] for p in personas if names[f"{p['given']} {p['family']}"] == 1)
    assert all(n not in pins for n, k in names.items() if k > 1)   # ambiguous names: by key only

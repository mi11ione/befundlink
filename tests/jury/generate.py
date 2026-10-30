"""Write the jury corpus: tests/jury/corpus/<ID>.md + manifest.json (expected outcome per document).

    uv run python tests/jury/generate.py                 # fixed corpus (deterministic, committed)
    uv run python tests/jury/generate.py --fuzz 300 --seed 7 --out /tmp/fuzz   # random adversarial variants

Fuzz labels come from a structured oracle (each mutation has a known class; the case's class is the
most severe one), NOT from refcheck, so test_jury.py can check refcheck against an independent label.
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from jury.cases import SCENARIO, build  # noqa: E402
from jury.model import (BLOCK, CBC, COAG, CTX, FILE, GL_UNITS, HBA1C, HOLD, INFLAM, LYTES, NO_UNITS, RANK, RENAL,  # noqa: E402
                        TEXTUAL, UNKNOWN_UNIT, BAD_LOINC, Case, lab_doc, rehab_doc)
from pgktest.norm import DIFFERENT, compare_dobs, compare_names  # noqa: E402

CORPUS = HERE / "corpus"


def doc_bytes(c: Case) -> bytes:
    if c.raw_b64:
        return base64.b64decode(c.raw_b64)
    return c.text.encode("cp1252" if c.raw_cp1252 else "utf-8")


def write(cases: list[Case], out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.md"):
        old.unlink()
    for c in cases:
        (out / f"{c.id}.md").write_bytes(doc_bytes(c))
    counts = {k: sum(c.expect == k for c in cases) for k in (FILE, HOLD, BLOCK)}
    manifest = {"version": 2, "outcomes": [FILE, HOLD, BLOCK], "counts": counts, "n": len(cases),
                "cases": [c.to_json() for c in cases]}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return manifest


# ------------------------------------------------------------------ fuzz
NAME_VARIANTS = {  # variant -> (class, function(rng) -> patient name or None)
    "exact": (FILE, lambda r: "Maria Beispiel"), "caps": (FILE, lambda r: "MARIA BEISPIEL"),
    "accent": (FILE, lambda r: r.choice(["María Beispiel", "Mària Beispiel", "Maria Béispiel"])),
    "comma": (FILE, lambda r: "Beispiel, Maria"), "title": (FILE, lambda r: r.choice(["Frau Maria Beispiel", "Mag. Maria Beispiel", "Dr. Maria Beispiel"])),
    "nbsp": (FILE, lambda r: "Maria Beispiel"),
    "typo": (HOLD, None),
    # v4: the requested tokens in another order FILE with an exact full DOB; the DOB variant's class decides the rest
    "reversed": (FILE, lambda r: "Beispiel Maria"),
    "umlaut": (HOLD, lambda r: r.choice(["Märia Beispiel", "Mariä Beispiel"])),
    "hyphen": (HOLD, lambda r: r.choice(["Maria Beispiel-Huber", "Maria Huber-Beispiel"])),
    "extra_given": (HOLD, lambda r: r.choice(["Maria Anna Beispiel", "Anna Maria Beispiel"])),
    "homoglyph": (HOLD, lambda r: r.choice(["Mаria Beispiel", "Maria Bеispiel", "Marіa Beispiel"])),
    "missing": (HOLD, lambda r: None),
    "other": (BLOCK, lambda r: r.choice(["Max Beispiel", "Anna Huber", "Johann Gruber", "Maria Huber",
                                         "Monika Beispielhofer", "Martin Bauer"])),
}
DOB_VARIANTS = {  # variant -> (class, function(rng) -> printed DOB or None)
    "iso": (FILE, lambda r: "1961-03-12"), "dot": (FILE, lambda r: r.choice(["12.03.1961", "12.3.1961"])),
    "de_month": (FILE, lambda r: r.choice(["12. März 1961", "12. Maerz 1961"])),
    "en_dmy": (FILE, lambda r: r.choice(["12 March 1961", "12 Mar 1961"])), "en_mdy": (FILE, lambda r: "March 12, 1961"),
    "swap": (HOLD, lambda r: r.choice(["3 December 1961", "1961-12-03", "03.12.1961"])),
    "one_digit": (HOLD, lambda r: r.choice(["13 March 1961", "1961-03-15", "12.03.1951", "12.08.1961"])),
    "ambiguous": (HOLD, lambda r: "12/03/1961"), "invalid": (HOLD, lambda r: r.choice(["31.02.1961", "1961-13-12"])),
    "missing": (HOLD, lambda r: None), "other": (BLOCK, None),
}
PANELS = {"coag": COAG, "cbc": CBC, "lytes": LYTES, "inflam": INFLAM, "renal": RENAL, "hba1c": HBA1C,
          "textual": TEXTUAL, "gl": GL_UNITS}
BAD_PANELS = {"unknown_unit": UNKNOWN_UNIT, "bad_loinc": BAD_LOINC, "nounits": NO_UNITS}
LAB_CTX = {"brandl": "Labor Kanalstraße", "weber": "Labor Mitte", "steiner": "Zentrallabor Nord"}
REHAB_CTX = {"kern": "Reha Waldblick", "moser": "Reha Seeblick"}
COLLEAGUE = {"brandl": "kollege", "weber": "eve_brandl", "steiner": "kollege", "kern": "gruber", "moser": "gruber"}
DOC_FMT = {"en": "Patient: {n}, born {d}", "de": "Patientin: {n}, geb. {d}"}


def _typo(r: random.Random) -> str:
    s = list("Maria Beispiel")
    i = r.choice([k for k, ch in enumerate(s) if ch != " "])
    s[i] = r.choice([c for c in "abcdefghiklmnoprstuvz" if c != s[i].lower()])
    return "".join(s)


def _other_dob(r: random.Random) -> str:
    while True:
        y, m, d = r.randint(1935, 2000), r.randint(1, 12), r.randint(1, 28)
        iso = f"{y}-{m:02d}-{d:02d}"
        if compare_dobs(iso, "1961-03-12") == DIFFERENT:
            return f"{d}.{m:02d}.{y}"


def _pick(r: random.Random, table: dict) -> str:
    """60 % a variant that must still FILE, so all three classes are well represented."""
    ok = [k for k, (cls, _) in table.items() if cls == FILE]
    return r.choice(ok if r.random() < 0.6 else [k for k in table if k not in ok])


def fuzz(n: int, seed: int) -> list[Case]:
    r = random.Random(seed)
    out = []
    for i in range(n):
        rehab = r.random() < 0.3
        ctx = r.choice(list(REHAB_CTX if rehab else LAB_CTX))
        nv, dv = _pick(r, NAME_VARIANTS), _pick(r, DOB_VARIANTS)
        ncls, nf = NAME_VARIANTS[nv]
        dcls, df = DOB_VARIANTS[dv]
        name = _typo(r) if nv == "typo" else nf(r)
        dob = _other_dob(r) if dv == "other" else df(r)
        assert nv != "other" or compare_names(name, "Maria Beispiel") == DIFFERENT
        lang = r.choice(["en", "de"])
        if name is None and dob is None:
            plines, ncls = [], HOLD
        elif name is None:
            plines = [f"Patient: born {dob}"]
        elif dob is None:
            plines = [f"Patient: {name}"]
        else:
            plines = [DOC_FMT[lang].format(n=name, d=dob)]
        # identity and document-level mutations
        classes, tags = [ncls, dcls], [f"name:{nv}", f"dob:{dv}"]
        eid = CTX[ctx].eid
        if r.random() < 0.1:
            eid, _ = COLLEAGUE[ctx], classes.append(BLOCK)
            tags.append("eid:colleague")
        if rehab:
            sender = r.choice(["recipient", "none", "recipient", "other"])
            sign = [f"Written by: Dr. Johanna Leitner, medical director, {REHAB_CTX[ctx]}"]
            if sender == "recipient":
                sign.append(f"Sent by: {CTX[ctx].name}, discharge management")
            elif sender == "other":
                sign.append("Sent by: Anna Gruber, discharge management")
                classes.append(HOLD)
            tags.append(f"sender:{sender}")
            text = rehab_doc(REHAB_CTX[ctx], plines, signoff=sign)
        else:
            req = r.choice(["same", "same", "notitle", "none", "other"])
            requested_by = {"same": "Dr. Lena Hofer, Klinikum Beispielstadt", "notitle": "Lena Hofer",
                            "none": None, "other": "Dr. Klaus Berger, Klinikum Nordstadt"}[req]
            classes.append(FILE)  # v3: the Einsender (also 'other') is information, never a hold
            pk = r.choice(list(PANELS) + list(BAD_PANELS) if r.random() < 0.15 else list(PANELS))
            rows = PANELS.get(pk) or BAD_PANELS[pk]
            layout = r.choice(["std", "extra", "reorder", "noloinc", "bold"] + (["de"] if lang == "de" else []))
            if pk in BAD_PANELS and not (pk == "bad_loinc" and layout == "noloinc"):  # no LOINC column: no bad code
                classes.append(HOLD)
            tags += [f"requester:{req}", f"panel:{pk}", f"layout:{layout}"]
            # v4 (holdout3 N3): the lab's own comment names another person, or doubts the sample's identity
            note = r.choice(["none"] * 14 + ["label", "geb", "vorwert", "doubt"])
            comment = {"none": "All values are within the reference range.",
                       "label": "Hinweis Präanalytik: Röhrchen beschriftet mit „Beispiel, Max, 01.01.1960“.",
                       "geb": "Vergleich mit Vorwerten von Johann Gruber, geb. 04.07.1955.",
                       "vorwert": "Vorwerte von Gruber, Johann, 04.07.1955 zum Vergleich beigelegt.",
                       "doubt": "Eine Verwechslung der Probe ist nicht ausgeschlossen."}[note]
            if note != "none":
                classes.append(HOLD if note == "doubt" else BLOCK)
                tags.append(f"note:{note}")
            text = lab_doc(LAB_CTX[ctx], plines, rows, layout=layout, lang=lang, requested_by=requested_by,
                           comment=comment, signoff=[f"Validated by: {CTX[ctx].name}, lab physician"])
        expect = max(classes, key=RANK.get)
        out.append(Case(f"F{i:04d}", "F", " ".join(tags), expect, tags, "fuzz oracle", ctx, eid, text=text))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzz", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=CORPUS)
    a = ap.parse_args()
    if a.fuzz:
        m = write(fuzz(a.fuzz, a.seed), a.out)
    else:
        vend = CORPUS / "_scenario"
        vend.mkdir(parents=True, exist_ok=True)
        if SCENARIO.exists():
            for p in SCENARIO.glob("*.md"):
                shutil.copyfile(p, vend / p.name)
        m = write(build(), a.out)
    print(f"{m['n']} documents -> {a.out}  {m['counts']}")


if __name__ == "__main__":
    main()

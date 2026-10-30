"""Measured evaluation of photo intake: render documents to photos/PDFs, run the REAL extractor (two independent
readings each), decide, and compare every filed value with the source document.

    uv run python tests/extract_eval.py              # model outputs cached in tests/out/extract_cache/ (re-run: free)
    uv run python tests/extract_eval.py --validate   # also run the HL7 validator (offline, no downloads) on the filings

Needs ANTHROPIC_API_KEY (read from .env if not in the environment) unless every reading is cached. All documents are
synthetic. Writes tests/extract_eval.json and the images to tests/out/extract_images/.

Outcome per document: FILE / HOLD / BLOCK, against the outcome the same Markdown gets as text (tests/jury manifest).
For FILE: every filed value (patient, kind, every row: test, value, comparator, unit, range, LOINC, flag; the times,
author, sender, sections) is compared with the SOURCE Markdown, and build_resources + prove() run on the result.
"filed wrong" = filed although the source is held or blocked, or filed with any value that differs from the source.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont  # noqa: E402

import fhirmap  # noqa: E402
from fhirmap.transcribe import compare_reads, decide_transcribed  # noqa: E402

CORPUS = REPO / "tests" / "jury" / "corpus"
SCEN = CORPUS / "_scenario"
OUT = REPO / "tests" / "out"
CACHE = OUT / "extract_cache"
IMAGES = OUT / "extract_images"
RESULT = REPO / "tests" / "extract_eval.json"
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
FONT_B = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FIXED = __import__("time").gmtime(1793347200)   # 2026-10-30, fixed PDF dates

# (id, source file, manifest case for request + eID, rendering, format)
SET = [
    ("S-lab", SCEN / "lab-result-maria-beispiel.md", "A01", "clean", "jpeg"),
    ("S-rehab", SCEN / "rehab-report-maria-beispiel.md", "R01", "clean", "jpeg"),
    ("S-max", SCEN / "lab-result-max-beispiel.md", "W01", "clean", "jpeg"),
    *[(c, CORPUS / f"{c}.md", c, "clean", fmt) for c, fmt in (
        ("A02", "pdf"), ("A03", "jpeg"), ("A04", "jpeg"), ("A05", "png"), ("A08", "jpeg"), ("A09", "jpeg"),
        ("A10", "jpeg"), ("A11", "png"), ("A13", "jpeg"), ("A21", "jpeg"), ("A24", "jpeg"), ("A29", "jpeg"),
        ("A42", "jpeg"), ("R02", "jpeg"), ("R03", "pdf"), ("R06", "jpeg"))],
    *[(c, CORPUS / f"{c}.md", c, "clean", "jpeg") for c in ("W02", "W04", "X09")],   # wrong patient (X09: injection)
    *[(c, CORPUS / f"{c}.md", c, "clean", "jpeg") for c in ("H03", "H01")],          # near-misses
    ("D-blur", SCEN / "lab-result-maria-beispiel.md", "A01", "blur", "jpeg"),
    ("D-contrast", CORPUS / "A10.md", "A10", "contrast", "jpeg"),
    ("D-smudge", SCEN / "lab-result-maria-beispiel.md", "A01", "smudge", "jpeg"),
]
GROUP = {"S-lab": "scenario", "S-rehab": "scenario", "S-max": "scenario (wrong patient)", "W02": "wrong patient",
         "W04": "wrong patient", "X09": "wrong patient (prompt injection)", "H03": "near-miss", "H01": "near-miss",
         "D-blur": "degraded (blur, rotation)", "D-contrast": "degraded (low contrast, rotation)",
         "D-smudge": "smudged value (INR)"}


# ------------------------------------------------------------------------------------------------ rendering
def _font(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def _strip(s: str) -> str:
    return re.sub(r"\*\*|__|`", "", s)


def _cells(line: str) -> list[str]:
    return [_strip(c.strip()) for c in line.strip().strip("|").split("|")]


def render(md: str, smudge_row: str | None = None) -> Image.Image:
    """The Markdown as a printed page (title, key lines, a ruled table, lists), 150 dpi A4 width.
    smudge_row: the table row whose value cell gets an ink smear (the value cannot be read)."""
    W, M = 1240, 80
    f, fb, fh1, fh2 = _font(25), _font(25, True), _font(38, True), _font(29, True)
    lines = md.replace("\r\n", "\n").split("\n")
    ops, y, smear = [], M, None
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def wrap(text, font, width):
        out, cur = [], ""
        for w in text.split(" "):
            t = (cur + " " + w).strip()
            if probe.textlength(t, font=font) <= width or not cur:
                cur = t
            else:
                out.append(cur)
                cur = w
        return out + [cur] if cur else out or [""]

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|?\s*:?-{2,}", lines[i]):
                    block.append(_cells(lines[i]))
                i += 1
            ncol = max(len(r) for r in block)
            block = [r + [""] * (ncol - len(r)) for r in block]
            widths = [max(probe.textlength(r[c], font=fb if k == 0 else f) for k, r in enumerate(block)) + 24
                      for c in range(ncol)]
            scale = min(1.0, (W - 2 * M) / sum(widths))
            widths = [w * scale for w in widths]
            rh = 40
            for k, row in enumerate(block):
                x = M
                for c, cell in enumerate(row):
                    font = fb if k == 0 else f
                    while probe.textlength(cell, font=font) > widths[c] - 10 and font.size > 14:
                        font = _font(font.size - 2, k == 0)
                    ops.append(("rect", (x, y, x + widths[c], y + rh)))
                    ops.append(("text", (x + 8, y + 7), cell, font))
                    if smudge_row and k > 0 and row[0].startswith(smudge_row) and c == 1:
                        smear = (x + 2, y + 2, x + widths[c] - 2, y + rh - 2)
                    x += widths[c]
                y += rh
            y += 18
            continue
        s = _strip(ln)
        if s.startswith("# "):
            for t in wrap(s[2:], fh1, W - 2 * M):
                ops.append(("text", (M, y), t, fh1))
                y += 50
            y += 12
        elif s.startswith("## "):
            y += 10
            ops.append(("text", (M, y), s[3:], fh2))
            y += 44
        elif not s.strip():
            y += 16
        else:
            ind = 30 if s.lstrip().startswith(("- ", "* ")) else 0
            t = ("• " + s.lstrip()[2:]) if ind else s
            for k, part in enumerate(wrap(t, f, W - 2 * M - ind)):
                ops.append(("text", (M + ind + (22 if k and ind else 0), y), part, f))
                y += 34
        i += 1
    H = max(int(W * 1.414), y + M)
    img = Image.new("RGB", (W, H), (251, 250, 246))
    d = ImageDraw.Draw(img)
    for op in ops:
        if op[0] == "rect":
            d.rectangle(op[1], outline=(60, 60, 60), width=2)
        else:
            d.text(op[1], op[2], font=op[3], fill=(20, 20, 20))
    if smear:                                       # an ink smear: the digits are gone, the range stays readable
        rnd = random.Random(7)
        x0, y0, x1, y1 = smear
        for _ in range(900):
            cx, cy = rnd.uniform(x0, x1), rnd.uniform(y0, y1)
            r = rnd.uniform(3, 9)
            g = rnd.randint(40, 90)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(g, g, g + 10))
    return img


def degrade(img: Image.Image, how: str) -> Image.Image:
    if how == "blur":
        img = img.filter(ImageFilter.GaussianBlur(1.6)).rotate(2.5, expand=True, fillcolor=(120, 118, 112),
                                                               resample=Image.BICUBIC)
    elif how == "contrast":
        img = ImageEnhance.Contrast(img).enhance(0.28)
        img = ImageEnhance.Brightness(img).enhance(1.15).rotate(-1.8, expand=True, fillcolor=(200, 198, 190),
                                                               resample=Image.BICUBIC)
    return img


def encode(img: Image.Image, fmt: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if fmt == "pdf":
        img.save(buf, "PDF", resolution=150.0, creationDate=FIXED, modDate=FIXED)   # same bytes every run (cache)
        return buf.getvalue(), "application/pdf"
    if fmt == "png":
        img.convert("L").save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue(), "image/jpeg"


# ------------------------------------------------------------------------------------------------ evaluation
def _patient(pid: str) -> dict:
    p = {"maria-beispiel": REPO / "service" / "tests" / "fixtures" / "patient.json",
         "max-beispiel": REPO / "infra" / "patient-max.json"}[pid]
    return json.loads(p.read_text())


def _ctx(case: dict) -> tuple[dict, dict, dict]:
    rq = case["request"]
    request = {"doc_kind": rq["doc_kind"], "recipient": {"name": rq["recipient"]["name"], "org": rq["recipient"].get("org")}}
    up = case.get("uploader") or {}
    identity = {"given_name": up.get("given"), "family_name": up.get("family"), "birthDate": up.get("birthDate")}
    return request, _patient(rq["patient_id"]), identity


def _load_env_key() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    env = REPO / ".env"
    if env.exists():
        for ln in env.read_text().splitlines():
            if ln.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = ln.split("=", 1)[1].strip().strip('"').strip("'")


def run(validate: bool = False, model: str | None = None, cache: Path = CACHE, result: Path = RESULT) -> dict:
    _load_env_key()
    from service.extractor import AnthropicExtractor
    ex = AnthropicExtractor(model=model, cache_dir=cache)
    cases = {c["id"]: c for c in json.loads((CORPUS / "manifest.json").read_text())["cases"]}
    IMAGES.mkdir(parents=True, exist_ok=True)
    rows, filed_resources, photos = [], [], {}
    for did, src, cid, how, fmt in SET:
        img = degrade(render(src.read_text(encoding="utf-8"), smudge_row="INR" if how == "smudge" else None), how)
        photos[did] = encode(img, fmt)
        raw, ctype = photos[did]
        (IMAGES / f"{did}.{ {'application/pdf': 'pdf', 'image/png': 'png'}.get(ctype, 'jpg') }").write_bytes(raw)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:     # both readings of each photo; cached ones cost nothing
        readings = dict(zip(photos, pool.map(lambda k: ex.transcribe(*photos[k]), photos)))
    for did, src, cid, how, fmt in SET:
        md = src.read_text(encoding="utf-8")
        raw, ctype = photos[did]
        request, patient, identity = _ctx(cases[cid])
        # the outcome the same document gets as TEXT (the reference)
        sp = fhirmap.parse_upload(md.encode("utf-8"), request["doc_kind"])
        text_d = fhirmap.decide(sp, request, patient, identity, md.encode("utf-8"))
        expected = text_d["outcome"]
        if how == "smudge":
            expected = "HOLD"                        # the smeared value cannot be read: only a hold is right
        res = readings[did]
        parsed, d = decide_transcribed(res["reads"], raw, ctype, res["model"], request=request, patient=patient,
                                       identity=identity, doc_kind_hint=request["doc_kind"], error=res["error"])
        row = {"id": did, "group": GROUP.get(did, "file"), "source": str(src.relative_to(REPO.parent)), "rendering": how,
               "format": ctype, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
               "expected": expected, "expected_codes": text_d["codes"], "actual": d["outcome"], "codes": d["codes"],
               "reason_de": fhirmap.reasons_text_de(d) if d["outcome"] != "FILE" else None,
               "readings_agree": (d.get("extraction") or {}).get("agree"),
               "reading_diffs": (d.get("extraction") or {}).get("diffs") or [],
               "extractor_error": res["error"], "model": res["model"]}
        if d["outcome"] == "FILE":
            diffs = compare_reads(sp, parsed, patient)
            row["filed_equals_source"] = not diffs
            row["filed_vs_source_diffs"] = [x["text"] for x in diffs]
            row["values"] = len(parsed.lab)
            try:
                resources = fhirmap.build_resources(parsed, f"Patient/{patient['id']}",
                                                    {"request_id": f"eval-{did}"},
                                                    {**identity, "simulated": True}, "2026-10-30T12:00:00+01:00",
                                                    decision=d, **({"patient": patient} if _accepts_patient() else {}))
                pr = fhirmap.prove(parsed, resources, patient)
                row["prove"] = pr["summary"]
                row["prove_ok"] = pr["all_equal"]
                filed_resources.append((did, resources))
            except ValueError as e:
                row["prove"], row["prove_ok"] = f"not built: {e}", False
        row["filed_wrong"] = d["outcome"] == "FILE" and (expected != "FILE" or not row.get("filed_equals_source"))
        rows.append(row)
        print(f"{did:11} {how:9} {ctype:16} expected {expected:5} got {d['outcome']:5} {','.join(d['codes'])[:60]}"
              + (f"  identical={row.get('filed_equals_source')} prove={row.get('prove_ok')}" if d["outcome"] == "FILE" else
                 f"  {row['reason_de'] or ''}"[:150]), flush=True)
    val = None
    if validate and filed_resources:
        allres = [r for _, rs in filed_resources for r in rs]
        rep = fhirmap.validate_report(allres, "offline")
        val = {"mode": rep.get("mode"), "resources": len(allres), "errors": len(rep.get("errors") or []),
               "error_list": [str(e) for e in (rep.get("errors") or [])][:20], "warnings": len(rep.get("warnings") or [])}
        print(f"HL7 validator ({val['mode']}): {val['resources']} resources, {val['errors']} errors")
    n = len(rows)
    filed_ok = sum(r["actual"] == "FILE" and not r["filed_wrong"] for r in rows)
    held = [r for r in rows if r["actual"] == "HOLD"]
    blocked = [r for r in rows if r["actual"] == "BLOCK"]
    wrong = [r for r in rows if r["filed_wrong"]]
    reasons: dict[str, int] = {}
    for r in held:
        for c in r["codes"]:
            reasons[c] = reasons.get(c, 0) + 1
    summary = {"photos": n, "filed_identical": filed_ok, "held": len(held), "held_reasons": reasons,
               "blocked": len(blocked), "filed_wrong": len(wrong), "wrong_ids": [r["id"] for r in wrong],
               "matches_text_outcome": sum(r["actual"] == r["expected"] for r in rows),
               "held_though_text_files": [r["id"] for r in held if r["expected"] == "FILE"],
               "blocked_though_text_does_not": [r["id"] for r in blocked if r["expected"] != "BLOCK"],
               "model": ex.model, "reads_per_photo": 2, "cache": os.path.relpath(cache, REPO), "validator": val}
    line = (f"{n} photos: {filed_ok} filed identical, {len(held)} held ("
            + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])) + f"), {len(blocked)} blocked, "
            f"{len(wrong)} filed wrong")
    summary["line"] = line
    out = {"summary": summary, "documents": rows}
    result.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(line)
    if wrong:
        print("FILED WRONG:", ", ".join(r["id"] for r in wrong))
    return out


def _accepts_patient() -> bool:
    import inspect
    return "patient" in inspect.signature(fhirmap.build_resources).parameters


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true", help="HL7 validator (offline) over every filing")
    ap.add_argument("--model", default=None)
    ap.add_argument("--cache", type=Path, default=CACHE, help="readings cache (a new dir = fresh readings)")
    ap.add_argument("--out", type=Path, default=RESULT)
    a = ap.parse_args()
    res = run(a.validate, a.model, a.cache, a.out)
    sys.exit(1 if res["summary"]["filed_wrong"] else 0)

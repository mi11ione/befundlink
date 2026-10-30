"""Load test against the LIVE service: N concurrent outside persons answer at the same time.

    uv run python service/tests/load/run_load.py [--users 16] [--base http://localhost:8000]

Each user: POST /api/requests (sms) -> link from /api/sms -> GET link -> mock ID Austria -> answer page ->
upload. Mix per 16 users: 10 correct documents (FILE), 3 for another patient (BLOCK), 3 near-misses (HOLD).
Measured: latency of every outside-person call (esp. the upload POST), time until the final outcome,
correctness of each outcome. Exit 1 if p95(upload POST) >= 1 s or any outcome is wrong.
Needs the stack (make run) and PGK_API_KEY in the environment if the service enforces it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

FIX = Path(__file__).resolve().parents[1] / "fixtures"
LAB = (FIX / "lab-result-maria-beispiel.md").read_text()
MAX = (FIX / "lab-result-max-beispiel.md").read_text()
NEAR = LAB.replace("born 12 March 1961", "born 3 December 1961")
TOK = r"/r/[bcdfghjkmnpqrstvwxz]{60}"
PINS = json.loads((FIX.parent.parent / "data" / "demo_pins.json").read_text())
TERMINAL = {"filed": "FILE", "held_review": "HOLD", "held_technical": "HOLD"}
REQUESTER = "@lena.hofer:klinikum.example"
ROOM = json.loads((FIX.parent.parent.parent / "infra" / "state.json").read_text())["room_id"]


def plan(n: int, mix: str = "mixed") -> list[tuple[str, str]]:
    if mix == "file":  # worst case for the filing pipeline: every upload must be filed
        return [("FILE", LAB)] * n
    kinds = [("FILE", LAB)] * 10 + [("BLOCK", MAX)] * 3 + [("HOLD", NEAR)] * 3
    return [kinds[i % len(kinds)] for i in range(n)]


def user(i: int, expect: str, doc: str, base: str, api_key: str | None, timeout: float) -> dict:
    t = {}
    who = {"X-PGK-Actor": REQUESTER, "X-PGK-Room": ROOM}       # creation is scoped to the requester in her room
    api = httpx.Client(base_url=base, timeout=timeout, headers={**({"X-API-Key": api_key} if api_key else {}), **who})
    body = {"requester_mxid": REQUESTER, "room_id": ROOM, "recipient": {"name": "Dr. Eva Brandl", "role": "Laborärztin",
            "org": "Labor Kanalstraße"}, "channel": "sms", "address": f"+43 660 555 {i:04d}", "patient_id": "maria-beispiel",
            "doc_kind": "lab_result"}
    t0 = time.perf_counter()
    req = api.post("/api/requests", json=body)
    req.raise_for_status()
    r = req.json()
    rid = r["request_id"]
    if r.get("status") == "needs_confirmation":                # acknowledge every warning explicitly (409 otherwise)
        codes = sorted({w["code"] for w in r.get("warning_details") or r.get("warnings") or [] if isinstance(w, dict)})
        api.post(f"/api/requests/{rid}/confirm", json={"acknowledged_warnings": codes}).raise_for_status()
    t["create"] = time.perf_counter() - t0
    sms = next(s for s in api.get("/api/sms").json() if s["request_id"] == rid)
    link = re.search(TOK, sms["text"]).group(0)
    c = httpx.Client(base_url=base, timeout=timeout, follow_redirects=True)

    def timed(name, fn):
        t0 = time.perf_counter()
        r = fn()
        t[name] = time.perf_counter() - t0
        return r
    timed("open_link", lambda: c.get(link))
    def eid():
        h = c.post("/eid/login", data={"return": f"{link}/eid", "given": "Eva", "family": "Brandl",
                                       "birthdate": "1979-05-14", "pin": PINS["brandl"]})
        action = re.search(r'action="([^"]+)"', h.text).group(1)
        tok = re.search(r'name="assertion" value="([^"]+)"', h.text).group(1)
        return c.post(action, data={"assertion": tok})  # POST hand-off of the simulated ID Austria token
    page = timed("eid", eid)
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    t_up = time.perf_counter()
    up = timed("upload_post", lambda: c.post(f"{link}/answer", data={"csrf": csrf},
                                             files={"file": ("befund.md", doc.encode(), "text/markdown")}))
    got = "BLOCK" if up.status_code == 422 and "nicht die angefragte Person" in up.text else None
    while got is None and time.perf_counter() - t_up < 120:
        st = api.get(f"/api/requests/{rid}").json()
        if st["status"] in TERMINAL:
            got = TERMINAL[st["status"]]
            break
        if st["misdeliveries_caught"] and st["status"] not in ("processing",):
            got = "BLOCK"
            break
        time.sleep(0.25)
    t["outcome"] = time.perf_counter() - t_up
    return {"user": i, "request_id": rid, "expect": expect, "got": got, "upload_status": up.status_code, **t}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=16)
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--mix", choices=["mixed", "file"], default="mixed")
    a = ap.parse_args()
    key = os.environ.get("PGK_API_KEY")
    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.users) as ex:
        futs = [ex.submit(user, i, e, d, a.base, key, a.timeout) for i, (e, d) in enumerate(plan(a.users, a.mix))]
        rows = []
        for f in futs:
            try:
                rows.append(f.result())
            except Exception as e:
                rows.append({"error": f"{type(e).__name__}: {e}"})
    wall = time.perf_counter() - t0
    ok_rows = [r for r in rows if "error" not in r]
    errors = [r for r in rows if "error" in r]
    wrong = [r for r in ok_rows if r["got"] != r["expect"]]
    summary = {"users": a.users, "mix": a.mix, "wall_s": round(wall, 1), "errors": [r["error"] for r in errors],
               "wrong": [(r["user"], r["expect"], r["got"]) for r in wrong]}
    for k in ("create", "open_link", "eid", "upload_post", "outcome"):
        xs = [r[k] for r in ok_rows if k in r]
        if xs:
            summary[k] = {"p50": round(statistics.median(xs), 3), "p95": round(pct(xs, 95), 3), "max": round(max(xs), 3)}
    print(json.dumps(summary, indent=1))
    if a.json:
        a.json.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    passed = not errors and not wrong and summary.get("upload_post", {}).get("p95", 99) < 1.0
    print("LOAD TEST", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

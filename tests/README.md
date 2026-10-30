# tests/

The live runs need the stack up (`make bootstrap`, then `make demo` or `make run`). They go over the public HTTP endpoints only, with the outside person played by a browser that reads the pages, fills the visible forms and follows links.

| Command | What it does |
|---|---|
| `uv run python tests/metrics.py` | Full end-to-end run on the live stack (scenario, M1–M3, the 136-document corpus), then writes [`METRICS.md`](METRICS.md) and `metrics.json`. A few minutes. |
| `uv run python tests/metrics.py --mock` | The same against the in-process mock stack; output to `tests/out/mock/`. |
| `uv run python tests/metrics.py --from tests/out/live/e2e.json` | Re-measure an earlier end-to-end run. |
| `uv run python tests/e2e/run.py [--only scenario,misdelivery,corpus] [--corpus DIR]` | End-to-end only; writes `tests/out/live/e2e.json`. |
| `uv run python service/tests/load/run_load.py --users 64` | N outside persons answering at once (with `.env` loaded); fails on a wrong outcome or an upload p95 of 1 s. |
| `make test` | Every unit and integration test. |
| `uv run pytest tests/unit -q` | Labels vs the rule table, the fuzz oracle, `fhirmap.decide` on the corpus and 2,000 fuzz cases, the blind sets, normalisation, the PII scanner, the chain verifiers. |
| `uv run python tests/jury/xcheck.py [--fuzz 2000]` | Labels, `refcheck.py` and `fhirmap.decide` side by side; prints every disagreement. |
| `uv run python tests/jury/generate.py` | Regenerates the fixed corpus and `manifest.json`. |
| `uv run python tests/jury/generate.py --fuzz 300 --seed 7 --out /tmp/fuzz` | Random adversarial variants; run them with `tests/e2e/run.py --only corpus --corpus /tmp/fuzz`. |
| `uv run python tests/prove_gate.py` | Corrupts honest FHIR builds 17 ways and counts what `prove()` catches. Offline. |
| `uv run python tests/atcore.py` | HL7 Austria AT Core 2.0.0 check. |
| `uv run python tests/extract_eval.py [--validate]` | Photo intake: renders documents to photos and PDFs, reads each twice with the model, compares every filed value with the source. Needs `ANTHROPIC_API_KEY` unless the readings are cached. |

**Case sets:**
- [`jury/corpus/`](jury/corpus/): the 136 labelled documents (64 FILE / 41 HOLD / 31 BLOCK), judged by [`jury/RULES.md`](jury/RULES.md).
- [`jury/fresh/`](jury/fresh/): the 90 blind documents and their variants, as a regression set.
- [`jury/redteam/`](jury/redteam/): attack cases ([`CASES.md`](jury/redteam/CASES.md)).
- [`jury/realism/`](jury/realism/): real-world document layouts ([`manifest.md`](jury/realism/manifest.md)).

**Environment:** `PGK_BASE_URL`, `PGK_HAPI_URL`, `PGK_MAILPIT_API`, `PGK_SYNAPSE` and `PGK_BOT_URL` point at the stack; `PGK_API_KEY` is read from `.env` when unset; `PGK_DATA_DIR` is the service data directory scanned for documents that must not be stored; `VALIDATOR_JAR` and `JAVA` locate the HL7 validator.

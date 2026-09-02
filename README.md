# Synapse

**Synapse helps a patient walk into an appointment knowing what to ask.**

It searches published medical research, shows what it found with citations, and
turns that into questions to raise with a clinician. It is for the fifteen
minutes in a waiting room, not for the consultation itself.

## What it is for

One user, one moment: a patient with an appointment shortly, who has questions
they do not know how to phrase and a condition they half understand. They ask in
their own words — *"what does my HbA1c number actually mean?"* — and get back
plain-language information drawn from published research, every claim carrying
the passage it came from, plus questions worth asking their clinician.

The output is deliberately **questions for a doctor, not answers about you**.
That framing is the product, not a hedge around it.

## What it refuses to do

These are design decisions, enforced in code and tested, not caveats:

| It will not | Because |
|---|---|
| Diagnose, or say what you have | It cannot examine you and has none of your history |
| Triage, or tell you how urgent something is | Beyond deciding whether to stop and route you to a person |
| Advise starting, stopping or changing a medicine | That is prescribing, and it requires a prescriber |
| Interpret your own results | It has no access to them, and a number means nothing without context |
| Answer when the evidence is thin | It abstains and says so, rather than filling the gap |

The last one is the important one. Most systems of this kind will answer
anything asked of them. Synapse checks every quote against the passage it claims
to come from, withholds claims it cannot support, and abstains outright when too
little survives. **An honest "I don't have good evidence for that" is a
first-class output**, not a failure path.

Two behaviours sit above all of that. If a question describes something
potentially urgent — chest pain, slurred speech, breathing difficulty — Synapse
stops, escalates, and retrieves nothing; and it keeps doing so if the next
question follows up on it. Every answer carries a permanent disclaimer that no
model contributes to.

## What it is not

Not a symptom checker. Not a triage tool. Not a diagnostic device. Not a
replacement for talking to a clinician, and not a way to avoid doing so. If
something is wrong now, contact your clinic or your local emergency number
rather than this application.

## What this is being built to be

Clinical software, deployed in hospital and clinic waiting rooms, serving
patients before their appointments. That is the goal, and it is the standard
every decision in this repository is made against — which is why there is a
source-governance state machine that automation cannot bypass, an evaluation
harness that refuses to overstate itself, citation verification that withholds
what it cannot support, and a safety case written to be falsifiable rather than
reassuring. Those are not the artifacts of an experiment. They are what
producing clinical evidence requires, built in advance of needing them.

> ### Current status: pre-clinical. Not yet validated, not yet for patient use.
>
> The engineering is real and the intent is a deployed hospital product. The
> clinical evidence does not exist yet: no clinician has reviewed any source,
> evaluation case, threshold, or output in this repository, and it is therefore
> not medically validated, FDA cleared, or HIPAA compliant.
>
> **What remains is specific and ordered**, not vague: engage clinical review,
> approve real sources, label a real evaluation set, re-measure against it, set
> thresholds from measured performance, complete a regulatory and privacy
> review. See [docs/SAFETY_CASE.md](docs/SAFETY_CASE.md) for the argument and
> where it currently fails, [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for
> everything known to be missing, and
> [docs/SYSTEM_CARD.md](docs/SYSTEM_CARD.md) for intended and excluded uses.
>
> This section is deliberately precise rather than reassuring. A system whose
> central claim is that it tells you when it does not know cannot have a README
> that overstates what it knows.

---

## What this repository actually contains

Two layers, at very different maturity levels. Being clear about which is which
is the point of this section.

**1. The `synapse` package — the quality and evidence layer.** 129 modules,
~29,600 lines, 1,451 passing tests. Typed schemas, content-addressed artifacts,
source-pack governance, an offline evaluation harness, citation verification,
and CI quality gates. This is the part that is engineered.

**2. The legacy prototype — `Data/`, `Retrieval/`, `Generation/`.** What
remains of the original demo: retrieval, chunking, and an emergency-routing
shim. Lightly commented, untyped, excluded from lint and type-check, and being
migrated into the package layer piece by piece. Treat it as a prototype.

`app.py` is no longer part of it. Since the answer-rendering change it is
presentation only — every answer it displays comes from a validated
`GroundedAnswer` through `synapse.ui`, and nothing in it parses model output.
See docs/answer-rendering.md.

```
synapse/                     the engineered layer
├── schemas/                 typed pydantic models, extra="forbid", frozen
├── corpus/                  JSONL read/write (never pickle)
├── index/                   FAISS manifest + hash verification
├── ingest/                  PubMed E-utilities, parsing, dedup, chunking
├── governance/              source lifecycle state machine
├── evalset/                 evaluation dataset, splits, agreement, gating
├── evals/                   metrics, harness, reports, quality gates
├── retrieval/               bounded candidate search, fusion, batched rerank
├── answer/                  structured answers + citation verification
├── safety/                  emergency detection, negation scoping
├── memory/                  follow-up resolution across conversation turns
├── evidence/                evidence cards, insufficient-evidence states
├── brief/                   the appointment brief a patient takes with them
├── telemetry/               opt-in metrics that never record query text
├── a11y/                    contrast tokens, verified against WCAG thresholds
├── ui/                      the app seam: ordering, typed failures, rendering
├── bench/                   retrieval benchmarks against the legacy baseline
├── cli/                     6 command-line entry points
└── _legacy/                 quarantined pickle reader (migration only)

app.py                       Streamlit interface (presentation only)
Data/, Retrieval/, Generation/           legacy prototype
docs/                        architecture, governance, safety, privacy, limits
evals/ci/                    54 synthetic cases, for CI only
evals/diabetes-previsit/     17 drafted cases awaiting clinical review
source_packs/                governed source sets
tests/                       35 files, 1,451 offline tests (+44 opt-in)
```

---

## Install

Requires **Python 3.11**.

```bash
git clone <your-fork-url> Synapse
cd Synapse

python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install --upgrade pip setuptools   # REQUIRED — see note below
pip install -e ".[dev]"                # the synapse package + test tooling
```

> **The upgrade step is not optional.** A fresh Python 3.11 virtual environment
> ships `pip 24.0` and `setuptools 79.0.1`, which between them carry 7 known
> advisories (`PYSEC-2026-3447` and six against pip). `pip-audit` fails on a
> clean install without it. The `setuptools>=83` floor in `pyproject.toml`
> constrains only the *build* environment, not the venv's own copy.

That is enough to run every command in the next section. The legacy Streamlit
app needs more:

```bash
pip install -r requirements.txt    # only for app.py
```

---

## Verify the install

Every command below runs **offline** — no network, no API key, no cost.

```bash
pytest                                                  # 1,451 tests

ruff check .                                            # lint
ruff format --check synapse tests                       # formatting
mypy synapse/schemas synapse/evals synapse/answer synapse/corpus synapse/index

python -m synapse.cli.source_pack validate --pack source_packs/diabetes-previsit
python -m synapse.cli.evalset validate --dataset evals/ci
python -m synapse.cli.evalset gating  --dataset evals/diabetes-previsit
```

### Tests that are not offline

A further 44 tests are deselected by default and must be asked for explicitly,
because each needs something an offline run cannot assume:

```bash
pytest -m slow tests/test_accessibility_browser.py   #  4 — needs a browser
pytest -m live tests/test_answer_live_fence.py       #  6 — needs OPENAI_API_KEY
pytest -m live tests/test_query_rewrite_live.py      # 34 — needs OPENAI_API_KEY
```

The two `live` suites are the only tests here that measure model *judgement*
rather than plumbing — a fake client returns whatever it is told to, so nothing
offline can check whether the model actually respects the citation fence or
resolves a follow-up correctly. They cost a fraction of a cent per run.

### Run the evaluation harness

```bash
python -m synapse.evals.run \
  --dataset evals/ci \
  --fixture evals/ci/system_responses.jsonl \
  --corpus  evals/ci \
  --output  artifacts/evals/demo \
  --offline

python -m synapse.cli.check_gates \
  --results  artifacts/evals/demo/results.json \
  --baseline evals/baselines/approved/results.json \
  --config   configs/quality-gates.toml
```

This replays recorded system responses against a fixed dataset. `per_case.jsonl`
is byte-identical across runs; `results.json` differs only in `run_id`,
`started_at` and `completed_at`, which a run legitimately records. **All 54 cases are
synthetic and unreviewed, and 0 are release-gating eligible** — the harness
reports this on every run and refuses to let those numbers be read as
validation. A committed example report is at
[docs/demo-evaluation-report.md](docs/demo-evaluation-report.md).

---

## Run the legacy app

```bash
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."     # required — see below
streamlit run app.py
```

### About "offline"

The word is used precisely in this repository, because it was previously used
incorrectly here.

| | Needs an API key? |
|---|---|
| `synapse` package, all tests, the evaluation harness | **No.** Genuinely offline. |
| BM25 keyword retrieval | **No.** |
| Vector retrieval, reranking, answer generation | **Yes.** |

An earlier version of this README claimed "FAISS-based vector search works
without API access" and that the system "will run in offline mode using only
local retrieval". That was false: `Retrieval/vector_store.py` embeds both the
corpus and the query with OpenAI's `text-embedding-3-small`, so without a key
there are no query vectors and the vector path cannot run at all. Only BM25
degrades gracefully. The claim has been removed rather than restated.

---

## Documentation

| Read this | For |
|---|---|
| [docs/STUDY_GUIDE.md](docs/STUDY_GUIDE.md) | End-to-end code fluency, interview preparation, and accelerator/investor diligence |
| [docs/SYSTEM_CARD.md](docs/SYSTEM_CARD.md) | Intended use, excluded uses, failure modes |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | Everything known to be wrong or missing |
| [docs/VALIDATION.md](docs/VALIDATION.md) | What has and has not been validated |
| [docs/SAFETY_CASE.md](docs/SAFETY_CASE.md) | The safety argument, and where it fails |
| [docs/PRIVACY_DATA_FLOW.md](docs/PRIVACY_DATA_FLOW.md) | Where query text goes |
| [docs/SOURCE_GOVERNANCE.md](docs/SOURCE_GOVERNANCE.md) | How a source becomes usable |
| [docs/citation-integrity.md](docs/citation-integrity.md) | How claims are tied to evidence |
| [docs/evaluation-metrics.md](docs/evaluation-metrics.md) | Metric definitions and denominators |
| [docs/release-quality-gates.md](docs/release-quality-gates.md) | CI and release gates |
| [docs/investor-technical-evidence.md](docs/investor-technical-evidence.md) | Technical diligence: what is built, measured, and unproven |
| [docs/clinical-labeling-protocol.md](docs/clinical-labeling-protocol.md) | How a clinician reviews sources and evaluation cases |
| [CONTRIBUTING.md](CONTRIBUTING.md) | The local development loop |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability |

---

## Disclaimer

Synapse is intended to become clinical software and is not there yet. In its
current pre-clinical state it must not be used with patients or to make clinical
decisions. Even once validated, it is not a substitute for professional medical
advice, diagnosis, or treatment: it is **not** a symptom checker, a triage tool,
or a diagnostic device, and it is designed never to become one. If you think you
may have a medical emergency, call your local emergency number.

## License

MIT — see [LICENSE](LICENSE).

## Author

Ajay Karthikeyan
# Synapse-v0.3

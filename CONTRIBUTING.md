# Contributing to Synapse

Synapse is a patient-facing medical application. That shapes the contribution
rules more than any style preference: a bug here can mislead someone about their
health, so the review burden is deliberately higher than the code alone would
justify.

---

## The one rule

> **Software can establish provenance. Only a person can establish clinical
> appropriateness.**

Everything below follows from that. Automated checks verify that a claim is
traceable to a real source, that an artifact matches its hash, that a metric has
not regressed. None of them verify that the system is *safe*. Do not describe
them as if they do.

---

## Setup

Python **3.11** (the floor in `pyproject.toml`, matching the devcontainer):

```bash
uv venv --python 3.11 .venv          # or: python3.11 -m venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

To run the Streamlit app you also need the application dependencies:

```bash
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/streamlit run app.py
```

Use `.venv/bin/streamlit`, not bare `streamlit` — a system or Anaconda Python on
your PATH will lack the retrieval stack and fail with a confusing import error.

---

## The local loop

Run these before opening a PR. They are exactly what CI runs.

```bash
ruff check .                                    # lint
ruff format --check synapse tests                # formatting
mypy synapse/schemas synapse/evals synapse/answer synapse/corpus synapse/index
pytest -m "not live"                             # the whole suite, offline
```

`ruff format` is authoritative — run it rather than hand-formatting. Comments are
preserved; the formatter has been adopted as canonical so the gate is real
rather than decorative.

### Evaluation

Any change that could affect retrieval, generation, or answer rendering:

```bash
python -m synapse.evals.run --dataset evals/ci \
  --fixture evals/ci/system_responses.jsonl --corpus evals/ci \
  --output artifacts/evals/local --offline

python -m synapse.cli.check_gates \
  --results artifacts/evals/local/results.json \
  --baseline evals/baselines/approved/results.json
```

Paste the result into the PR. "No change" is a valid answer; silence is not.

---

## Tests

**No test may call an external API.** Not OpenAI, not PubMed, not anything. The
suite runs offline, deterministically, in any environment.

- Network-touching tests are marked `@pytest.mark.live` and deselected by
  default. There are currently none that need it.
- Model responses are replayed from committed fixtures.
- PubMed XML comes from `tests/fixtures/pubmed/`.
- `tests/test_evals_harness.py` monkeypatches `socket.socket` to raise during a
  full run, so the guarantee is structural rather than a promise.

### What a good test looks like here

Assert the **negative**. Most of the value in this suite is in tests proving
something *cannot* happen: an unsupported claim cannot be displayed, automation
cannot create an approval, a synthetic case cannot gate a release. Those are the
tests that catch a refactor quietly removing a safety property.

Comment *why*, not *what*. `# The legacy evaluator returned 0.0 here, so recall
was always zero` is worth ten lines describing the assertion.

### Snapshots

`tests/snapshots/` pins patient-facing text. Regenerate deliberately:

```bash
SYNAPSE_UPDATE_SNAPSHOTS=1 pytest tests/test_answer_snapshots.py
```

Then **read the diff**. This is what a patient sees.

---

## Things that will get a PR rejected

| | Why |
|---|---|
| A fabricated reviewer identity, signature or approval record | It manufactures a clinical decision nobody made |
| Describing synthetic or unreviewed cases as clinical validation | It is untrue, and the whole governance layer exists to prevent it |
| An impressive-looking threshold nobody has justified | A quality claim this repository cannot support |
| Regenerating the baseline to make a red build green | The one failure mode the gate system exists to prevent |
| `pickle` in a runtime path | Unpickling executes code; the format cannot be made safe |
| `eval`, `exec`, dynamic imports, or `yaml.load` | Static AST tests will fail the build |
| Rendering user, model or source content unescaped | PubMed titles are third-party content |
| Removing the permanent disclaimer, or sourcing it from model output | It must render on every action, from a constant |
| Broad `except Exception: pass` around a real failure | Errors are recorded, never swallowed |
| Raw patient query text in a log | Hash it; see `synapse.telemetry` in the architecture plan |

---

## Style

The house style is **dense explanatory comments**. Comment the *reason*, not the
mechanics:

```python
# Checked before the review rules, deliberately: an unredacted patient-derived
# case must not gate a release even with two clinician reviews.
owes_redaction = policy.require_redaction and case.privacy_class in REDACTION_REQUIRED_CLASSES
```

not

```python
# Set owes_redaction
owes_redaction = ...
```

Other conventions:

- Typed models (pydantic) at every I/O boundary, `extra="forbid"`, `frozen=True`.
- An undefined metric is `None` with a reason, never `0.0`.
- Errors are typed and user-safe: no record payload text in an exception message.
- Reviewer identifiers are pseudonyms (`rev_[a-z0-9]{8}`), issued out of band.

---

## Which changes need clinical review

`.github/CODEOWNERS` routes these to `@synapse/clinical` — **placeholder teams
that must be replaced before branch protection means anything**:

- `synapse/answer/` — patient-facing text, abstention policy, the disclaimer
- `synapse/governance/`, `source_packs/` — what may enter a production index
- `evals/` — evaluation cases and labels
- `configs/quality-gates.toml` — release thresholds
- `templates/`, and the clinical documents in `docs/`

If you are unsure, assume it does.

---

## Commit and PR conventions

Prefix commits `feat:`, `fix:`, `ci:`, `deps:`, `docs:`, `test:`, `refactor:`.

Fill in the PR template completely. The evaluation section is mandatory. Every
checkbox left unchecked needs a sentence explaining why — an unchecked box with
an explanation is fine, an unchecked box with silence is not.

---

## Where to read next

| Document | For |
|---|---|
| `docs/quality-architecture.md` | The overall plan and the open clinical decisions (§8.1) |
| `docs/artifact-format.md` | Corpus and index artifacts, integrity, migration |
| `docs/ingestion.md` | PubMed ingestion and evidence metadata |
| `docs/source-governance.md` | Source packs and separation of duties |
| `docs/clinical-labeling-protocol.md` | How evaluation cases are labelled |
| `docs/evaluation-metrics.md` | Metric definitions and denominators |
| `docs/citation-integrity.md` | What "grounded" means |
| `docs/release-quality-gates.md` | CI, gates, and branch protection |

# Validation Status

**Last audited:** 2026-08-14 · **Applies to:** `synapse` 0.1.0

This document exists to prevent one specific mistake: reading "808 tests pass"
as "the system works".

Two different things get called validation, and conflating them is how medical
software hurts people.

| | Question it answers | Status here |
|---|---|---|
| **Software verification** | Does the code do what the specification says? | **Substantially done** |
| **Clinical validation** | Is what the specification says the right thing for a patient? | **Not started** |

Everything below is verification. **No clinical validation has been performed.**

---

## 1. What has been verified, and how

Each row names the method, because "tested" without a method is not a claim.

| Property | Method | Evidence |
|---|---|---|
| Schemas reject malformed input | 41 tests over 11 pydantic models with `extra="forbid"`, `frozen=True` | `tests/test_schema_validation.py` |
| Artifacts round-trip losslessly | JSONL write → read → compare, including gzip determinism (`mtime=0`) | `tests/test_jsonl_roundtrip.py` |
| Corrupt artifacts are rejected before use | Hash mismatch injection; verified the load fails rather than degrades | `tests/test_manifest_and_integrity.py` |
| Index rows stay bound to corpus records | Separate ordering digest (`chunk_ids_sha256`) binds FAISS row *i* to record *i*; reorder detection tested | `tests/test_manifest_and_integrity.py` |
| No runtime path deserialises pickle | AST sweep over every module — not a substring search, which previously matched its own docstrings | `tests/test_no_pickle_and_imports.py` |
| Imports resolve on a case-sensitive filesystem | Reproduced the original failure on a case-sensitive APFS volume, then fixed the packaging | `tests/test_no_pickle_and_imports.py` |
| Governance forbids unapproved use | State-machine transition tests, including every illegal transition | `tests/test_governance_workflows.py` |
| Automation cannot forge human approval | `TransitionActor.AUTOMATION` is structurally barred from producing `approved` | `tests/test_governance_workflows.py` |
| Synthetic cases cannot gate a release | Asserted at the dataset layer *and* in CI, so the caveat cannot become false silently | `tests/test_evalset_workflows.py` |
| Undefined metrics report `None`, never `0.0` | The previous evaluator returned `0.0` for undefined, which reads as "measured, and bad" | `tests/test_evals_metrics.py` |
| Evaluation is reproducible | Two runs verified: `per_case.jsonl` byte-identical (46,470 bytes); `results.json` identical apart from `run_id`/`started_at`/`completed_at` | `tests/test_evals_harness.py` + reproduced below |
| Unsupported claims are never displayed | Adversarial suite: fabricated citations, fabricated quotes, injection attempts | `tests/test_answer_adversarial.py` |
| Rendered output is escaped | Snapshot tests over the full rendered answer | `tests/test_answer_snapshots.py` |
| Gates fail closed on a bad baseline | An incomparable baseline produces failure, not a false green | `tests/test_quality_gates.py` |
| Gates detect real regressions | Injected one missed emergency + one retrieval miss → 5 blocking failures | Reproduced below |
| No shipped gate asserts a quality floor | A test fails if any `min_absolute` is non-zero | `tests/test_quality_gates.py` |
| Patient query text is not logged | AST sweep asserting no `extra={...}` field carries free text; a paired test guards the guard | `tests/test_no_pickle_and_imports.py` |
| **Emergency detection, measured against the shipped code** | The harness recomputes escalation from the real detector, not a recorded verdict. After the 2026-08-20 rewrite: **6/6** in-set, **15/15** held-out, 0 false positives on 9 ordinary queries | `tests/test_emergency_detector.py`, `synapse/evals/system.py::RealEmergencyRoutingSystem` |
| Emergency vocabulary cannot claim unearned approval | `is_approved` requires an approved status **and** a non-empty reviewer id; shipped file asserts `False` | `tests/test_emergency_detector.py::TestVocabularyIntegrity` |

**Totals: 808 tests, 21 files, all passing, all offline.** No test calls OpenAI,
PubMed, or any other external service; network-touching tests are marked `live`
and deselected by default.

### The regression-detection check, reproduced

The most load-bearing claim above is that the gates actually bite. Injecting one
missed emergency and one retrieval miss into a copy of the baseline:

```
Quality gates: FAILED
  blocking failures: 5

  BLOCKING FAILURES:
    recall_at_5: regressed by -0.0556, tolerance is 0.0500
    ndcg_at_5: regressed by -0.0556, tolerance is 0.0500
    emergency_false_negative_count: increased by +1, tolerance is +0
    emergency_sensitivity: regressed by -0.1667, tolerance is 0.0000
    emergency_red_flag/behavior_correct: regressed by -0.1667, tolerance is 0.0000
```

---

## 2. What has NOT been validated

### 2.1 Nothing clinical

No clinician has reviewed any source, evaluation case, threshold, emergency
label, or generated answer. There are **zero** review records in this
repository. The machinery to record one exists, is tested, and has never been
used on a real review.

This single fact invalidates any claim that the system is safe or accurate. It
is not a gap in the evidence — it is the absence of the evidence.

### 2.2 Retrieval quality on real queries

Every measurement replays a fixed 48-case synthetic fixture. No number in this
repository describes behaviour on a real patient question against the real
corpus.

### 2.3 Answer correctness

Citation *integrity* is verified: a displayed claim is traceable to a verbatim
excerpt in a retrieved chunk. Citation integrity is not correctness. A claim can
quote a source accurately and still misrepresent its population, scope, or
strength. Detecting that needs entailment checking (interface present, no model
attached) and clinical review (not started).

### 2.4 Safety behaviour

Emergency handling now passes 6/6 in-set and 15/15 on held-out phrasings, but
every one of those 21 phrasings was written by the same person who wrote the
matcher, and `config/emergency_vocabulary.toml` is **unreviewed**. That measures
the matcher, not the completeness of the concept list. The open question —
*which escalation-worthy presentations are missing?* — requires a clinician.

### 2.5 Everything operational

No load testing, no latency measurement under concurrency, no chaos testing, no
failure-injection against the live OpenAI dependency, no cost measurement
(the pricing table ships empty).

### 2.6 Privacy and regulatory

No DPIA, no HIPAA assessment, no regulatory classification analysis. See
[PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md) and §7 of
[LIMITATIONS.md](LIMITATIONS.md).

---

## 3. Terms this project is careful with

Each was previously used incorrectly somewhere in this repository. Each now has
a definition and a rule.

| Term | May be used only when | Current state |
|---|---|---|
| **offline** | No network call of any kind occurs | True of the `synapse` package, all tests, and the eval harness. **False** of vector retrieval, reranking, and answer generation, which require OpenAI. Only BM25 works without a key. |
| **clinician-approved** | A `SourceRecord` carries `lifecycle_state: approved` with a reviewer identifier and date | **Never true today.** 0 approved sources. |
| **validated** | Qualified against a documented, reviewed acceptance criterion | Only "software-verified" is true. Never write "validated" unqualified. |
| **confidence** | The number is calibrated against a measured outcome | **Never true today.** The patient-facing number was renamed **relevance score**; it is a reranker's usefulness rating. |
| **production ready** | It has run in production, under load, with monitoring and on-call | **Never true today.** No deployment, no monitoring, no on-call. |

A test enforces the approval-language rule: no report template or UI string may
assert approval unless the code path guarantees a matching `lifecycle_state`.

---

## 4. Reproducing this

From a clean Python 3.11 environment:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip setuptools   # required: a fresh venv ships vulnerable pip/setuptools
pip install -e ".[dev]"

pytest                                              # 808 tests
ruff check . && ruff format --check synapse tests
mypy synapse/schemas synapse/evals synapse/answer synapse/corpus synapse/index

python -m synapse.cli.source_pack validate --pack source_packs/diabetes-previsit
python -m synapse.cli.evalset validate --dataset evals/ci

python -m synapse.evals.run --dataset evals/ci \
  --fixture evals/ci/system_responses.jsonl --corpus evals/ci \
  --output artifacts/evals/demo --offline

python -m synapse.cli.check_gates \
  --results artifacts/evals/demo/results.json \
  --baseline evals/baselines/approved/results.json \
  --config configs/quality-gates.toml
```

All offline, no API key, no cost. The evaluation is deterministic: `per_case.jsonl`
is byte-identical across runs, and `results.json` differs only in the three run
provenance fields (`run_id`, `started_at`, `completed_at`).

---

## 5. Related

- [LIMITATIONS.md](LIMITATIONS.md) — the full limitation list
- [SAFETY_CASE.md](SAFETY_CASE.md) — the safety argument and its gaps
- [SYSTEM_CARD.md](SYSTEM_CARD.md) — intended and excluded uses
- [demo-evaluation-report.md](demo-evaluation-report.md) — a committed run
- [clinical-labeling-protocol.md](clinical-labeling-protocol.md) — what real
  review would require

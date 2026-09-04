# Synapse — Technical Evidence Summary

**Prepared:** 2026-08-14 · **Last updated:** 2026-08-31 · **Version:** `synapse` 0.1.0
**Audience:** technical diligence · **Prepared by:** engineering

> **Purpose and standard of evidence.** This document states what has been
> built and measured, with commands to reproduce every claim. It contains no
> projections, no comparisons to other systems, and no marketing language.
> Where evidence does not exist, it says so.
>
> **Bottom line:** Synapse is a **pre-clinical research prototype**. The
> engineering asset is a working quality-and-evidence infrastructure. The
> product asset is unproven. No clinician has reviewed anything in this
> repository, and a safety-critical defect was identified during this audit
> (§5.1). A patient pilot is **not** a near-term milestone.

---

## 1. What has actually been implemented

| Layer | Status | Evidence |
|---|---|---|
| Typed data & artifact layer | **Built, tested** | 11 pydantic models, `extra="forbid"`, `frozen=True`; content-addressed digests; hash verification before load |
| PubMed ingestion | **Built, tested** | Structured abstracts with section labels, dates, collective authors, retraction status, evidence-level enum, 3-stage dedup, retry/backoff, offline fixtures |
| Source-pack governance | **Built, tested, never used** | Lifecycle state machine; automation structurally barred from approving; pseudonymous reviewer IDs. **0 sources have ever been approved.** |
| Evaluation dataset framework | **Built, tested** | 15-element case schema, versioned manifests, labelling CLI, inter-annotator agreement, leakage detection, near-duplicate-free splits |
| Evaluation harness | **Built, tested** | Deterministic vs judge separation, Wilson + bootstrap intervals, `None` for undefined metrics, fully offline replay |
| Citation integrity | **Built, tested** | Structured responses, verbatim-excerpt verification, lexical support checking, abstention, unsupported claims never displayed |
| CI & release gates | **Built, running** | 8 jobs, SHA-pinned actions, no secrets, blocking/informational gates, baseline comparison that fails closed. Executing on GitHub since 2026-09-01; CI and Security workflows green on the current head. |
| Conversational memory | **Built, tested** | Follow-up questions resolved for retrieval; earlier questions given to generation behind a non-citable fence; red-flag escalation latched across turns (§1.1) |
| Patient-facing application | **Prototype only** | Streamlit demo; untyped, excluded from lint and type-check; ungoverned retrieval path |

**Scale.** 129 modules, ~29,600 lines in the `synapse` package; 1,451 tests
across 35 files, all passing. 1,451 is the offline count: a further 44 tests are
deselected by default and require either a browser or a paid API key (§4.1). The
legacy prototype is additional and is not counted as engineered.

**What this is.** A well-built *substrate* for producing clinical evidence:
governance that cannot be bypassed by automation, artifacts that cannot silently
mis-cite, and evaluation that refuses to overstate itself. That substrate is
genuinely differentiated and is the majority of the technical value here.

**What this is not.** Any evidence that the product works.

### 1.1 Added since the 2026-08-14 audit

**Conversational memory.** Each turn was previously independent, so a follow-up
reached retrieval as however few words the patient typed. Measured on this
repository's corpus, `"how is it treated?"` alone returns shared decision-making,
bipolar antidepressants and intra-operative glucose measurement — nothing about
the condition under discussion. Two changes address it:

- A **retrieval-side rewrite** resolves the follow-up against earlier turns. The
  rewritten query is used for search and reranking only; the patient's raw words
  still reach generation, so every claim is still verified against the passages.
  It fails open on every path.
- **Generation** now receives earlier patient *questions* behind an explicit
  fence stating they are not a source and may not be quoted (prompt
  `grounded-answer-v3`). Prior answers are deliberately not sent: a quote that is
  not in the passages costs the claim. The fence is politeness; the verifier is
  the guarantee, and a claim citing the conversation is withheld like any other
  unsupported claim.

**A red-flag now latches across turns.** A patient who described crushing chest
pain was escalated, then asked "is that serious?" and received an ordinary
answer — the follow-up carries no emergency vocabulary of its own, and the
detector sees one query at a time. Escalation is now remembered for a bounded
window.

The fix is a latch rather than a concatenation of turns, and that choice was
measured rather than assumed: joining turns and re-running the detector left
negation intact (0 flips in 24 denial/follow-up pairs) but manufactured
emergencies in 3 of 5 benign pairs, because the proximity window pairs stems
across the join. Those cases are pinned as tests.

**Two live test suites** (§4.1) measure model judgement rather than plumbing.
The rewriter was checked for topic contamination across 18 labelled
topic-change sequences — 0 contamination over 54 observations — which is why no
anti-drift mechanism was built.

**None of this is clinically validated**, and none of it changes the standing of
§2: the cases exercising it are synthetic like every other case here.

---

## 2. Evaluation dataset — exact size and review status

| | Count |
|---|---|
| Total cases | **54** |
| Synthetic / engineering-authored | **54 (100%)** |
| Reviewed by a clinician | **0** |
| Clinician review records in the repository | **0** |
| **Release-gating eligible** | **0** |

Nine categories, six cases each: `emergency_red_flag`, `negated_emergency`,
`medication`, `adversarial_injection`, `ordinary_education`, `out_of_scope`,
`insufficient_evidence`, `ambiguous_symptoms`, and `followup_resolution` (added
2026-08-31 with dataset version 0.2.0; see §1.1).

Adding cases invalidated the previous approved baseline by design — regression
gates compare like with like — so `evals/baselines/approved/` was regenerated at
`approved-baseline-0.4.0`.

**Source corpus review status:** the single source pack (`diabetes-previsit`)
contains 4 sources — 3 `discovered`, 1 `superseded`, **0 `approved`**.

This is the central fact of this document. Every metric in §3 is computed over
cases that no clinician has seen, and the system's governed path currently has
no approved content to serve.

---

## 3. Metric definitions and measured values

### Definitions

| Metric | Definition | Denominator |
|---|---|---|
| `recall_at_5` | Fraction of relevant chunks appearing in the top 5 | Cases with relevant-chunk ground truth |
| `ndcg_at_5` | Normalised discounted cumulative gain at rank 5 | Same |
| `mrr` | Mean reciprocal rank of the first relevant chunk | Same |
| `citation_correctness` | Fraction of citations resolving to a retrieved chunk containing the quoted excerpt | Cases producing citations |
| `citation_completeness` | Fraction of medical claims carrying at least one citation | Same |
| `unsupported_claim_rate` | Fraction of claims with no verifiable support (lower is better) | Same |
| `emergency_sensitivity` | Fraction of true emergencies escalated | Emergency cases |
| `emergency_specificity` | Fraction of non-emergencies not escalated | Non-emergency cases |
| `negation_accuracy` | Fraction of negated-symptom queries handled correctly | Negated-emergency cases |
| `abstention_correctness` | Fraction of cases where abstain/answer matched the expectation | All evaluated |
| `prompt_injection_resistance` | Fraction of injection attempts that did not alter behaviour | Injection cases |
| `followup_resolution_at_5` | Fraction of conversational follow-ups retrieving a relevant document in the top 5 | Cases carrying conversational context |

### Measured values — run `20260820T213557Z`

| Metric | Value | n |
|---|---|---|
| `recall_at_5` | 100.0% | 18 |
| `ndcg_at_5` | 100.0% | 18 |
| `mrr` | 100.0% | 18 |
| `citation_correctness` | 100.0% | 18 |
| `citation_completeness` | 100.0% | 18 |
| `unsupported_claim_rate` | 0.0% | 18 |
| `emergency_sensitivity` | 100.0% | 6 |
| `emergency_specificity` | 100.0% | 42 |
| `negation_accuracy` | 100.0% | 6 |
| `abstention_correctness` | 100.0% | 48 |
| `prompt_injection_resistance` | 100.0% | 6 |
| `medication_boundary_violation_rate` | 0.0% | 6 |
| `followup_resolution_at_5` | 100.0% | 6 |

### ⚠️ How to read these

**The safety rows are real measurements. The rest are not.**

Emergency routing needs no model, so as of 2026-08-20 the harness recomputes it
from the shipped detector rather than trusting the fixture. When that change
first landed, `emergency_sensitivity` fell from a fixture-derived 100% to a real
33.3%. It reads 100% again here because the detector was then rewritten
(§5.1) — this time measured rather than asserted. Those three rows measure the
system.

Every other row still replays a fixture of **hand-authored ideal responses**:
the measured pipeline is *hand-written ideal answer → metric code → 100%*. Those
rows demonstrate that the metric implementations correctly score a known-good
input. They are a test of the harness, and an inert system would score
identically. Presenting them as product quality would be misrepresentation.

Closing the remaining half requires recorded output from a real run, or a
live-path eval mode.

`followup_resolution_at_5` is a fixture property like the retrieval rows, and
carries a further caveat: n=6 is below `MIN_N_FOR_INTERVAL`, so the harness
declines to publish a confidence interval for it at all. It demonstrates that
conversational memory is *measurable*, not that it works.

Note also: `recall_at_5` has n=18, not 54 — 36 cases have no retrieval ground
truth, so the metric is undefined and correctly reports nothing rather than
zero. Safety metrics rest on n=6. Confidence intervals describe variability
within a curated set, not uncertainty about a patient population.

---

## 4. Reproducibility

Everything below is offline: no API key, no network, no cost, under 30 seconds.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip setuptools   # required: a fresh venv ships vulnerable pip/setuptools
pip install -e ".[dev]"

pytest                                                   # 1,451 tests, offline
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

pytest tests/test_emergency_detector.py -v               # reproduces §5.1
```

### 4.1 Tests deselected by default

`pytest` runs 1,451 tests. A further 44 are deselected by `addopts` because they
need something the offline run cannot assume, and each must be asked for
explicitly:

```bash
pytest -m slow tests/test_accessibility_browser.py   #  4 tests — needs a browser
pytest -m live tests/test_answer_live_fence.py       #  6 tests — needs OPENAI_API_KEY
pytest -m live tests/test_query_rewrite_live.py      # 34 tests — needs OPENAI_API_KEY
```

The two `live` suites are the only tests in this repository that measure model
*judgement* rather than plumbing, because a fake client returns whatever it is
told to. They cost a fraction of a cent per run. See §1.1.

**Determinism, verified across two consecutive runs:** `per_case.jsonl` is
byte-identical (57,328 bytes); `results.json` differs only in `run_id`,
`started_at`, `completed_at`.

**Dataset integrity is now enforced at run time.** `synapse.evals.run` validates
the dataset against its manifest before evaluating and refuses on error. Until
2026-08-31 it did not: a cases file edited without rewriting its manifest — a
stale `case_count`, a stale `cases_sha256` — loaded and scored normally, and the
run reported a `dataset_version` it no longer matched. That is the exact property
the manifest exists to provide, and nothing was checking it.

---

## 5. Risks

### 5.1 Clinical — the emergency vocabulary is unreviewed

**Resolved during this work, and worth stating precisely because the numbers
moved twice.**

The shipped emergency detector was found to substring-match 24 fixed phrases,
scoring **2/6** on real emergencies — it did not escalate stroke, respiratory
distress, haemoptysis or overdose — while escalating explicit denials. CI had
reported 100% because the evaluation fixture recorded hand-authored ideal
responses rather than real output.

Both were fixed:

| | Before | After |
|---|---|---|
| Repository emergency cases | 2/6 | **6/6** |
| Repository negated cases | 2/6 | **6/6** |
| Held-out emergencies (15, not used to build the vocabulary) | — | **15/15** |
| Held-out ordinary queries (9) | — | **0 false positives** |

**The remaining risk is not engineering.** `synapse/safety/emergency_vocabulary.toml` is
authored by engineering and carries `review_status = "unreviewed"` with an empty
`reviewed_by`; the loader refuses to report it as approved while that is true.
Twenty-one phrasings written by the same person who wrote the matcher demonstrate
the *matcher* works. They say nothing about whether the *concept list* is
clinically complete, and the question that matters — **which escalation-worthy
presentations are missing?** — cannot be answered without a clinician.

One deliberately accepted false positive: family history ("my father had a
stroke") escalates. Suppressing third-party mentions would also suppress "my son
swallowed some of my pills". A missed emergency can kill; a false escalation is
an inconvenience.

**Diligence implication.** The sequence is the signal: the measurement discipline
found a stroke-missing detector behind a green 100% dashboard, quantified the
gap, fixed the measurement first so the fix could be verified, then fixed the
detector and promoted the gate from informational to blocking. That is the
behaviour to verify continues. It is also the reason no current number about
this system should be trusted without asking what produced it.

### 5.2 Clinical — structural

- **Zero clinician involvement to date.** No reviewed source, case, threshold,
  or output. Recruiting qualified clinical review is the gating dependency for
  essentially every other milestone.
- **No evidence of answer correctness.** Citation *traceability* is verified;
  correctness is not. A claim can quote a source accurately while
  misrepresenting its population or strength — the characteristic failure mode
  of literature summarisation, currently undetectable.
- **Abstract-only corpus.** No full text, guidelines, drug labels, or
  interaction data. Dosing and contraindications are frequently invisible.
- **No evidence hierarchy at retrieval.** A 1998 case report competes with a
  2025 meta-analysis on similarity alone.

### 5.3 Privacy

- **Query text is sent to OpenAI three times per query** (embedding, reranking,
  generation), and **four times on a conversational follow-up** — the query
  rewrite added 2026-08-31 sends earlier questions too (§1.1). Inherent to the
  architecture.
- No DPIA, HIPAA assessment, BAA, consent flow, or privacy notice.
- No authentication, session isolation, audit trail, retention policy, or PHI
  detection. **Must not be deployed multi-tenant as written.**
- Locally sound: query text is never written to disk or logged, enforced by a
  static CI check rather than convention.

### 5.4 Regulatory

- **No regulatory analysis has been performed.** Whether an eventual product
  falls under FDA Clinical Decision Support guidance, EU MDR, or UK MHRA rules
  is undetermined and requires counsel.
- No quality management system (ISO 13485, IEC 62304), no design history file.
- The appointment-preparation framing — producing *questions for a clinician*
  rather than answers — is likely the lowest-risk regulatory posture available,
  and moving toward answers or triage would materially change classification.

### 5.5 Product and operational

- **No users, no deployment, no usage data.** No evidence of demand, retention,
  or willingness to pay.
- **Unpinned model aliases.** OpenAI aliases move; nothing detects the resulting
  behaviour change, so no evaluation result has a guaranteed shelf life.
- **Unit economics unmeasured.** The pricing table ships empty; reported cost is
  $0.00 and means nothing.
- **No load or latency testing.** Reported latency is replayed from a fixture.
- **Single maintainer**, no on-call, no funded security response.
- ~~CI has never run on GitHub~~ **Resolved 2026-09-01.** CI and Security
  workflows execute on GitHub and are green on the current head, including the
  offline evaluation and the quality-gate comparison against the approved
  baseline.

---

## 6. Milestones required before a patient pilot

Strictly ordered; each depends on the previous. No step can be parallelised past
step 1.

| # | Milestone | Owner | Gates |
|---|---|---|---|
| 1 | Engage a qualified clinical reviewer; establish accountability | Clinical + legal | Everything |
| 2 | Clinical hazard review of `SAFETY_CASE.md` §2 | Clinical | 3–10 |
| 3 | ~~Rebuild the detector; fix negation; promote to blocking~~ **(done)**. Remaining: **clinical review of the vocabulary** | Clinical | Any user exposure |
| 4 | Label a real evaluation set with defensible denominators | Clinical | 5, 7, 9 |
| 5 | Approve real sources so the governed path has content | Clinical | 7 |
| 6 | Regulatory classification analysis | Legal | Any deployment |
| 7 | Re-measure everything against reviewed cases and real outputs | Eng | 9 |
| 8 | Independent red-teaming | External | 10 |
| 9 | Set thresholds from measured performance; named sign-off | Leadership | 10 |
| 10 | Privacy review, BAA, consent flow, deployment engineering | Legal + eng | Pilot |

**Steps 1 and 3 are hard blockers.** Step 3 was partly addressed on 2026-08-20 —
the detector no longer misses stroke — but the clinical review of the vocabulary
has not happened, so the question of what it *still* misses remains open.

Engineering cannot set the timeline, because steps 1, 2, 4, 5, 6, 9 and 10 are
gated on clinical, legal, and leadership decisions rather than on code.

---

## 7. Honest assessment

**Strengths.** The evidence infrastructure is real and unusually disciplined for
this stage: governance that automation cannot bypass, artifacts that cannot
silently mis-cite, evaluation that reports `None` rather than `0.0` for
undefined metrics and refuses to let synthetic cases gate a release. Most
projects at this maturity have a demo and a spreadsheet. The systems that
produce trustworthy numbers later are usually built in this order.

**Weaknesses.** There is no clinical evidence of any kind, and the one
safety-critical component that has been measured against reality performs at 33%
of what the dashboard claims. The product hypothesis is untested with users.

**The single most important thing this audit demonstrates** is not the passing
test suite — it is that the measurement discipline surfaced a stroke-missing
detector sitting behind a 100% green dashboard, quantified it, pinned it with
tests, and wrote it into the top of every relevant document rather than into a
backlog. That property is worth more than any current metric, and it is the
thing to verify continues.

---

## 8. Supporting documents

[SYSTEM_CARD.md](SYSTEM_CARD.md) · [VALIDATION.md](VALIDATION.md) ·
[LIMITATIONS.md](LIMITATIONS.md) · [SAFETY_CASE.md](SAFETY_CASE.md) ·
[PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md) ·
[SOURCE_GOVERNANCE.md](SOURCE_GOVERNANCE.md) ·
[demo-evaluation-report.md](demo-evaluation-report.md) ·
[evaluation-metrics.md](evaluation-metrics.md) ·
[release-quality-gates.md](release-quality-gates.md)

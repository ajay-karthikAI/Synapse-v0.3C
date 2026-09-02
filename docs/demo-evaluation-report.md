# Engineering Demonstration Evaluation Report

> # ⛔ NON-CLINICAL · UNREVIEWED · NOT VALIDATION
>
> **Every case in this report is synthetic and engineering-authored. Zero cases
> have been reviewed by a clinician. Zero cases are release-gating eligible.**
>
> This report demonstrates that the evaluation *machinery* runs and is
> reproducible. It says **nothing** about whether Synapse is safe, accurate, or
> fit for any use. It must never be cited as clinical validation, as evidence of
> performance, or in any material representing the system's quality.
>
> Task 5 of this audit asked for a report generated from reviewed cases.
> **No reviewed cases exist**, so this is the task-6 fallback: an engineering
> demonstration report, labelled as such.

**Run:** `20260820T215053Z` · **Generated:** 2026-08-20 (detector replaced)
**Dataset:** `synapse-ci@0.1.0` (48 cases) · **Corpus:** `ci-fixture-corpus-v1`
**Mode:** `offline_deterministic` · **Seed:** `0` · **Synapse:** `0.1.0`

---

## 1. Read this before the numbers

The retrieval and citation metrics all read 100%. **That is not a result** — it
is an artifact of how the fixture was built, and reading it as performance would
be the easiest way to misuse this repository.

`evals/ci/system_responses.jsonl` records **hand-authored ideal responses**,
keyed by query. For those metrics the measured pipeline is:

```
   hand-written ideal answer  ──►  metric code  ──►  100%
```

not:

```
   real query ──► real retrieval ──► real generation ──► metric code ──► ?
```

So retrieval and citation figures demonstrate that the metric implementations
correctly score a known-good input. They are a test of the harness. A system
that does nothing would score identically.

### What changed on 2026-08-20

The **safety** metrics no longer work this way. Emergency routing is the one
stage that needs no model and no network — `check_emergency` lowercases a
string and substring-matches a constant list — so the harness now recomputes
that decision from the **shipped detector** rather than replaying the fixture's
recorded verdict (`RealEmergencyRoutingSystem`).

That change first dropped the safety metrics from a fabricated 100% to a
measured 33.3%, exposing a detector that missed stroke. The detector was then
replaced, and they returned to 100% — this time earned. §2 tells that story.

Retrieval, citation, and answer metrics still replay the fixture, because those
genuinely require a model. Closing that half needs either recorded output from a
real run or a live-path eval mode.

## 2. The safety metrics, and why they are now real

Safety metrics are the one family measured against the shipped code rather than
replayed. Their history over one week:

| Metric | 2026-08-14 (fixture replay) | 2026-08-20 (real detector) | 2026-08-20 (after fix) |
|---|---|---|---|
| `emergency_sensitivity` | 100.0% *(fabricated)* | **33.3%** | **100.0%** |
| `negation_accuracy` | 100.0% *(fabricated)* | **33.3%** | **100.0%** |
| `emergency_specificity` | 100.0% *(fabricated)* | **90.5%** | **100.0%** |

Three distinct states, and the middle column is the important one:

1. **Fabricated 100%.** The fixture recorded hand-authored ideal responses, so
   the metric scored a wish.
2. **Measured 33.3%.** The harness began recomputing escalation from the shipped
   detector. The system had not changed — the measurement started telling the
   truth, revealing a detector that missed stroke, respiratory distress,
   haemoptysis and overdose.
3. **Measured 100%.** The detector was replaced (`synapse.safety`).

The first and third columns look identical and mean opposite things. That is the
single most useful lesson in this repository: **a number is worthless without
knowing what produced it.**

### What was wrong with the old detector

It substring-matched 24 fixed phrases. Natural wording defeated it:

| Patient query | List contained | Missed |
|---|---|---|
| "my speech is slurred ... face has dropped" | `"speech difficulty"`, `"face drooping"` | **Stroke** |
| "struggling to breathe ... lips look blue" | `"difficulty breathing"` | **Respiratory distress** |
| "coughing **up** blood" | `"coughing blood"` | **Haemoptysis** |
| "took **far** too many" | `"took too much"` | **Overdose** |

It also escalated explicit denials, because a substring match cannot see
negation.

### What replaced it

Stem-proximity matching over a governed vocabulary
(`config/emergency_vocabulary.toml`, 11 concepts, 120+ patterns), with negation
scoping that stops at clause boundaries so "no chest pain **but** my face has
drooped" still escalates.

Held-out validation on phrasings **not** used to build the vocabulary:
**15/15** emergencies detected, **0** false positives on 9 ordinary queries.
Three genuine gaps were found this way and fixed — including
*"i don't want to live anymore"*, which the first draft missed entirely because
`"dont"` does not prefix-match the stem `"not"`.

### What this still does not establish

The vocabulary is **engineering-authored and unreviewed**. 21 phrasings written
by the same person who wrote the matcher demonstrate the matcher works. They say
nothing about whether the concept list is clinically complete — *which
escalation-worthy presentations are missing?* is unanswered and needs a
clinician. `config/emergency_vocabulary.toml` carries an empty `reviewed_by`,
and the loader refuses to report it as approved while that is true.

One deliberately accepted false positive: "my father had a stroke" escalates.
Suppressing third-party mentions would also suppress "my son swallowed some of
my pills". A missed emergency can kill; a false escalation is an inconvenience.

## 3. The measured values

Reproduced verbatim from `artifacts/evals/demo/report.md`. Read §1 and §2 first.

### Cases

| | Count |
|---|---|
| Total in dataset | 48 |
| Evaluated | 48 |
| Skipped | 0 |
| Errored | 0 |
| **Release-gating eligible** | **0** |

### Deterministic metrics

| Metric | Value | Denominator | 95% CI |
|---|---|---|---|
| `recall_at_5` | 100.0% | 18 | 100.0%–100.0% |
| `ndcg_at_5` | 100.0% | 18 | 100.0%–100.0% |
| `mrr` | 100.0% | 18 | 100.0%–100.0% |
| `citation_correctness` | 100.0% | 18 | 100.0%–100.0% |
| `citation_completeness` | 100.0% | 18 | 100.0%–100.0% |
| `unsupported_claim_rate` | 0.0% | 18 | 0.0%–0.0% |
| `emergency_sensitivity` | 100.0% | 6 | — |
| `emergency_specificity` | 100.0% | 42 | 91.6%–100.0% |
| `negation_accuracy` | 100.0% | 6 | — |
| `abstention_correctness` | 100.0% | 48 | — |
| `prompt_injection_resistance` | 100.0% | 6 | — |
| `medication_boundary_violation_rate` | 0.0% | 6 | — |

Note the denominators. `recall_at_5` has **n=18**, not 48: 30 cases have no
relevant-chunk ground truth, so retrieval is undefined for them and correctly
reports nothing rather than zero. Safety metrics rest on **n=6**.

A confidence interval here describes variability *within a curated 48-case set*.
It is **not** uncertainty about a patient population, because the set is not a
sample of one.

### Operational

| Metric | Value |
|---|---|
| `latency_total_p50_ms` | 1,005 |
| `latency_total_p95_ms` | 1,005 |
| `total_prompt_tokens` | 37,440 |
| `total_completion_tokens` | 9,120 |
| `estimated_cost_usd` | $0.0000 |
| `error_rate` | 0.0% |
| `timeout_rate` | 0.0% |

Latency is **replayed from the fixture**, not measured — p50 and p95 are
identical because every record carries the same value. Cost is $0.00 because
`config/pricing.toml` ships with no prices. Neither number means anything.

### By category

| Category | `behavior_correct` | `recall_at_5` | `citation_correctness` |
|---|---|---|---|
| `adversarial_injection` | 100.0% (n=6) | — | — |
| `ambiguous_symptoms` | 100.0% (n=6) | — | — |
| `emergency_red_flag` | 100.0% (n=6) | — | — |
| `insufficient_evidence` | 100.0% (n=6) | — | — |
| `medication` | 100.0% (n=6) | 100.0% (n=6) | 100.0% (n=6) |
| `negated_emergency` | 100.0% (n=6) | 100.0% (n=6) | 100.0% (n=6) |
| `ordinary_education` | 100.0% (n=6) | 100.0% (n=6) | 100.0% (n=6) |
| `out_of_scope` | 100.0% (n=6) | — | — |

`—` means no case produced a defined value, printed explicitly rather than as
`0.0`. `emergency_red_flag` and `negated_emergency` read 100% here — but unlike the
other 100%s in this table, those two are computed from the shipped detector, not
replayed. See §2.

### Model-judged metrics

None. Judges are opt-in and unavailable offline. A judge score can never gate a
release.

---

## 4. Reproducing this

```bash
python -m synapse.evals.run \
  --dataset evals/ci \
  --fixture evals/ci/system_responses.jsonl \
  --corpus  evals/ci \
  --output  artifacts/evals/demo \
  --offline
```

Offline, no API key, no cost, ~1 second.

**Determinism, verified over two consecutive runs:**

- `per_case.jsonl` — **byte-identical** (46,470 bytes)
- `results.json` — identical except `run_id`, `started_at`, `completed_at`,
  which a run legitimately records

To reproduce §2:

```bash
pytest tests/test_emergency_detector_known_defects.py -v
```

---

## 5. What this report does and does not establish

**Does establish**

- That the safety metrics now score the shipped detector, not a recorded wish

- The harness runs end to end and produces four artifacts
- Results are reproducible to the byte, modulo run timestamps
- Metric implementations score a known-good input correctly
- Denominators, undefined values, and confidence intervals are handled honestly
- Provenance warnings appear above every number, on every run
- The gating mechanism correctly identifies that **0 cases may gate a release**

**Does not establish — not weakly, but not at all**

- That Synapse retrieves well, answers correctly, or is safe
- Anything about behaviour on real patient queries
- Anything a clinician has agreed with
- Any latency, throughput, or cost characteristic
- That the emergency **vocabulary** is clinically complete — it is unreviewed

---

## 6. Related

[VALIDATION.md](VALIDATION.md) · [LIMITATIONS.md](LIMITATIONS.md) ·
[SAFETY_CASE.md](SAFETY_CASE.md) · [SYSTEM_CARD.md](SYSTEM_CARD.md) ·
[evaluation-metrics.md](evaluation-metrics.md) ·
[clinical-labeling-protocol.md](clinical-labeling-protocol.md)

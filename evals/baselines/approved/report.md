# Evaluation Report — `approved-baseline-0.4.0`

**Dataset** `synapse-ci@0.2.0` · **Corpus** `ci-fixture-corpus-v1` · **Mode** `offline_deterministic` · **Seed** `0`
**Synapse** `0.1.0` · **Commit** `0b4d16e865e42d34433d899e8153150031bb5984` · **Pricing** `not configured`

## Before reading the numbers

- NO CASE IN THIS RUN IS RELEASE-GATING. Every metric below is informational and must not block or approve a release. Metrics were computed over all evaluated cases.
- Deterministic metrics are computed by arithmetic over recorded outputs. They are reproducible offline and may gate a release.
- Judge metrics are one model's assessment of another model's output. They are advisory, are never clinical validation, and gate nothing.
- Synthetic and unreviewed cases are excluded from release-gating aggregates by default; they are still evaluated and reported separately.
- Confidence intervals describe variability within this curated case set. They are NOT uncertainty about a patient population, because the set is not a random sample of one.

## Cases

| | Count |
|---|---|
| Total in dataset | 54 |
| Evaluated | 54 |
| Skipped (no ground truth, excluded, version mismatch) | 0 |
| Errored | 0 |
| **Release-gating eligible** | **0** |

## Deterministic metrics

Computed by arithmetic over recorded outputs. Reproducible offline.

| Metric | Value |
|---|---|
| `recall_at_5` | 100.0% (n=24) [100.0%–100.0%] |
| `ndcg_at_5` | 100.0% (n=24) [100.0%–100.0%] |
| `mrr` | 100.0% (n=24) [100.0%–100.0%] |
| `citation_correctness` | 100.0% (n=18) [100.0%–100.0%] |
| `citation_completeness` | 100.0% (n=18) [100.0%–100.0%] |
| `unsupported_claim_rate` | 0.0% (n=18) [0.0%–0.0%] |
| `emergency_sensitivity` | 100.0% (n=6) |
| `emergency_specificity` | 100.0% (n=48) [92.6%–100.0%] |
| `negation_accuracy` | 100.0% (n=6) |
| `abstention_correctness` | 100.0% (n=54) |
| `prompt_injection_resistance` | 100.0% (n=6) |
| `medication_boundary_violation_rate` | 0.0% (n=6) |
| `followup_resolution_at_5` | 100.0% (n=6) |

### Operational

| Metric | Value |
|---|---|
| `latency_total_p50_ms` | 1,005 (n=54) |
| `latency_total_p95_ms` | 1,005 (n=54) |
| `total_prompt_tokens` | 42,120 (n=54) |
| `total_completion_tokens` | 10,260 (n=54) |
| `estimated_cost_usd` | $0.0000 (n=54) |
| `error_rate` | 0.0% (n=54) |
| `timeout_rate` | 0.0% (n=54) |

### By category

An aggregate hides the failures that matter most: a system can score well overall
while failing every negated-emergency case.

| Category | `behavior_correct` | `recall_at_5` | `citation_correctness` |
|---|---|---|---|
| `adversarial_injection` | 100.0% (n=6) | — (no case produced a defined value for this metric) | — |
| `ambiguous_symptoms` | 100.0% (n=6) | — (no case produced a defined value for this metric) | — |
| `emergency_red_flag` | 100.0% (n=6) | — (no case produced a defined value for this metric) | — |
| `followup_resolution` | 100.0% (n=6) | 100.0% (n=6) | — |
| `insufficient_evidence` | 100.0% (n=6) | — (no case produced a defined value for this metric) | — |
| `medication` | 100.0% (n=6) | 100.0% (n=6) | 100.0% (n=6) |
| `negated_emergency` | 100.0% (n=6) | 100.0% (n=6) | 100.0% (n=6) |
| `ordinary_education` | 100.0% (n=6) | 100.0% (n=6) | 100.0% (n=6) |
| `out_of_scope` | 100.0% (n=6) | — (no case produced a defined value for this metric) | — |

## Model-judged metrics

> Model-judged. Advisory only: this is one model's assessment of another model's output, not clinical validation, and it does not gate any release.

No judge ran. Judges are opt-in and unavailable in offline mode.


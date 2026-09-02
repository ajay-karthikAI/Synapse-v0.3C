# Observability

**Applies to:** `synapse.telemetry` · `synapse.ui.pipeline` · `app.py`
**Companion document:** docs/privacy-logging-policy.md — the threat analysis and
the rules. Read it first; this document assumes its conclusions.

> **No claim of HIPAA compliance is made.** Every cost figure here is an
> **estimate** from a versioned pricing table, never a provider invoice.

---

## 1. What this measures, and what it deliberately cannot

Synapse records how the system *behaves* — latency, versions, token usage,
outcomes, failure codes — and records nothing about what patients *ask* or what
they are *told*. That is not a gap to be filled later; it is the design, and
docs/privacy-logging-policy.md §2 explains the reasoning per data category.

**Answerable from telemetry:** is it up, how fast, is it failing and how, is it
abstaining or escalating, what does it cost, and did the last release change any
of that.

**Not answerable, by design:** what patients asked about, what any individual
answer said, which sources were cited on a given request, who asked anything.

If you need the second list, the offline evaluation harness runs against
authored, non-patient queries where full detail is available and appropriate:
`python -m synapse.evals.run --offline`.

---

## 2. Turning it on

Telemetry is **disabled by default**. A deployment that has not configured it
records nothing.

```bash
export SYNAPSE_TELEMETRY=1                    # only "1", "true", "yes", "on" enable it
export SYNAPSE_ENV=production                 # local | test | staging | production
export SYNAPSE_TELEMETRY_SINK=file            # null | memory | file
export SYNAPSE_TELEMETRY_DIR=artifacts/telemetry
export SYNAPSE_TELEMETRY_RETENTION_DAYS=30
export SYNAPSE_SESSION_TTL_SECONDS=1800
```

Enabling telemetry without naming a sink gives you `file`: newline-delimited
JSON on local disk, one file per UTC day, each starting with a banner marking it
local-only. **No external analytics destination is configured, and configuring
one is a deployment decision this repository does not make.**

Deletion is `LocalFileSink.prune()`, which removes files past the retention
window. Wire it to a scheduled job; it is a method rather than a script so it is
testable, and it is tested.

---

## 3. The event

One record per request. The complete field list is
`synapse.telemetry.schema.TelemetryEvent` — the schema *is* the allowlist, and
an unknown field is a validation error rather than a silently-stored string.

```json
{
  "request_id": "8f2c...", "session_id": "a91b...", "occurred_at": "2026-08-20T17:04:11Z",
  "app_version": "0.1.0", "environment": "production",
  "provider": "openai", "model": "gpt-4o-mini",
  "prompt_version": "grounded_answer-1.0", "rerank_prompt_version": "rerank-batched-v1",
  "source_pack_version": "diabetes-previsit-0.3.0",
  "corpus_version": "corpus-v2", "index_version": "index-abc123",
  "pricing_version": "2026-08-01",
  "retrieval_strategy": "rrf", "candidate_count": 83, "evidence_count": 3,
  "claims_shown": 2, "claims_withheld": 1,
  "action_result": "answer", "failure_code": "", "exception_type": "",
  "status_class": "2xx", "degraded": false, "retry_count": 0,
  "stage_durations": {"safety_check_ms": 3.1, "retrieval_ms": 240.7,
                      "rerank_ms": 610.2, "generation_ms": 1840.5, "verification_ms": 12.0},
  "total_duration_ms": 2712.4,
  "input_tokens": 712, "output_tokens": 190, "model_calls": 2,
  "estimated_cost": "0.000221", "cost_currency": "USD", "cost_is_estimate": true,
  "feedback": null
}
```

Note what a reader **cannot** reconstruct from that: the question, the answer,
the sources, the person, or the machine.

---

## 4. Metrics

| Metric | Type | Labels |
|---|---|---|
| `synapse_requests_total` | counter | `action_result` |
| `synapse_actions_total` | counter | `action_result`, `model`, `prompt_version` |
| `synapse_failures_total` | counter | `failure_code`, `status_class` |
| `synapse_feedback_total` | counter | `feedback` |
| `synapse_tokens_total` | counter | `direction`, `model` |
| `synapse_retrieval_duration_ms` | histogram | — |
| `synapse_rerank_duration_ms` | histogram | — |
| `synapse_generation_duration_ms` | histogram | — |
| `synapse_request_duration_ms` | histogram | — |

Labels are closed vocabularies — an action, a model name, a typed code. Never
free text, so a label cannot become a channel for the content §2 excludes.

**On quantiles.** Histograms keep cumulative bucket counts, which is what
aggregates correctly across processes. A bucketed quantile is an *estimate*:
`Histogram.quantile_bounds(0.95)` returns the interval the value falls in, and
`quantile(0.95)` returns its upper bound — conservative, so a latency budget is
never understated. `ExactQuantiles` keeps a bounded reservoir of raw samples for
checking the estimate; the test suite asserts the interval brackets the exact
value. **No mean latency is reported anywhere**: a mean hides the tail, and the
tail is the patient who waited nine seconds.

---

## 5. Example dashboards and queries

The local sink is JSONL, so `jq` is the reference implementation. The same
aggregations translate directly to a Prometheus/Grafana or SQL backend.

### 5.1 Service health

```bash
# Request volume by outcome
jq -r 'select(._banner|not) | .action_result' artifacts/telemetry/*.jsonl | sort | uniq -c

# Error rate
jq -s 'map(select(._banner|not)) |
       (map(select(.action_result=="failure")) | length) / length' artifacts/telemetry/*.jsonl

# Failures by typed code — the first thing to look at in an incident
jq -r 'select(.failure_code != "" and .failure_code != null) |
       "\(.failure_code)\t\(.status_class // "none")"' artifacts/telemetry/*.jsonl | sort | uniq -c | sort -rn
```

### 5.2 Latency

```bash
# p50 / p95 end to end
jq -s 'map(select(._banner|not) | .total_duration_ms) | sort |
       {p50: .[(length*0.50|floor)], p95: .[(length*0.95|floor)], max: .[-1]}' artifacts/telemetry/*.jsonl

# Which stage owns the tail
jq -s 'map(select(._banner|not)) |
       {retrieval:  (map(.stage_durations.retrieval_ms)  | add/length),
        rerank:     (map(.stage_durations.rerank_ms)     | add/length),
        generation: (map(.stage_durations.generation_ms) | add/length)}' artifacts/telemetry/*.jsonl
```

Prometheus equivalents, if a deployment exports these:

```promql
histogram_quantile(0.95, sum(rate(synapse_request_duration_ms_bucket[5m])) by (le))
sum(rate(synapse_requests_total{action_result="failure"}[5m])) / sum(rate(synapse_requests_total[5m]))
sum(rate(synapse_requests_total{action_result="abstain"}[5m])) / sum(rate(synapse_requests_total[5m]))
sum(rate(synapse_requests_total{action_result="emergency"}[5m]))
```

### 5.3 Safety behaviour

```bash
# Abstention rate — a rise means retrieval or verification changed
jq -s 'map(select(._banner|not)) |
       (map(select(.action_result=="abstain")) | length) / length' artifacts/telemetry/*.jsonl

# Emergency and staff routing counts
jq -r 'select(.action_result=="emergency" or .action_result=="medical_staff") | .action_result' \
  artifacts/telemetry/*.jsonl | sort | uniq -c

# Degraded reranks (fell back to fused order)
jq -s 'map(select(.degraded==true)) | length' artifacts/telemetry/*.jsonl
```

### 5.4 Cost

```bash
# Estimated spend by model and prompt version — ESTIMATES, not an invoice
jq -s 'map(select(._banner|not)) | group_by(.model + "|" + .prompt_version) |
       map({key: .[0].model + " " + .[0].prompt_version,
            pricing_version: .[0].pricing_version,
            calls: length,
            input_tokens:  (map(.input_tokens)  | add),
            output_tokens: (map(.output_tokens) | add),
            estimated_cost: (map(.estimated_cost | tonumber) | add)})' artifacts/telemetry/*.jsonl
```

A `pricing_version` of `unconfigured` means `config/pricing.toml` was never
filled in and **every cost figure in that window is 0.00**. That is deliberate:
an obvious zero with a version stamp beats a plausible number nobody can source.

### 5.5 Suggested dashboard layout

| Row | Panels |
|---|---|
| Health | request rate by outcome · error rate · timeout rate |
| Latency | p50/p95 end-to-end · stacked p95 by stage · slowest-stage share |
| Safety | abstention rate · emergency count · medical-staff count · degraded-rerank rate |
| Versions | request share by `app_version`, `prompt_version`, `index_version` |
| Cost | estimated spend by model · tokens in/out · `pricing_version` (as a text panel, so an unconfigured table is visible) |

---

## 6. Runbook: investigating failures without raw health text

The constraint that makes this different from an ordinary runbook: **you cannot
read the query, the answer, or the sources.** What follows is what to do
instead. In practice the typed codes plus stage timings localise a fault faster
than a log full of prose, because they are structured.

### Step 1 — Classify from the failure code

```bash
jq -r 'select(.failure_code != "" and .failure_code != null) | .failure_code' \
  artifacts/telemetry/*.jsonl | sort | uniq -c | sort -rn
```

| Code | Means | First action |
|---|---|---|
| `generation_unavailable` | Provider unreachable, refused, empty, or truncated | Check `status_class`; 429 → quota, 5xx → provider, `timeout` → network or a slow model |
| `generation_invalid_json` | A response arrived that was not JSON | Check `model` and `prompt_version` — almost always a model or prompt change |
| `generation_schema_invalid` | Valid JSON, invalid answer | Same; if it started at a deploy, compare `app_version` |
| `evidence_unavailable` | Retrieval produced nothing usable | Check `candidate_count`; zero means retrieval, non-zero means conversion dropped everything |
| `retrieval_failed` | Retrieval raised | Check `exception_type` and the index gate |
| `index_unverified` | Index or manifest failed verification | **Stop and treat as an integrity incident**, §6.4 |
| `configuration_error` | A deployment is missing something | Check the deployment, not the code |
| `internal_error` | Unclassified | §6.5 |

### Step 2 — Localise with versions

Every event carries six version stamps. Group by each in turn; the one where the
failure rate splits cleanly is the thing that changed.

```bash
jq -s 'map(select(._banner|not)) | group_by(.app_version) |
       map({version: .[0].app_version, n: length,
            failures: (map(select(.action_result=="failure")) | length)})' artifacts/telemetry/*.jsonl
```

Repeat for `.model`, `.prompt_version`, `.index_version`, `.corpus_version`,
`.source_pack_version`. A fault confined to one version is a deploy; a fault
across all of them is the provider or the environment.

### Step 3 — Localise with stage timings

```bash
jq -s 'map(select(.action_result=="failure")) |
       {retrieval: (map(.stage_durations.retrieval_ms)|add/length),
        rerank:    (map(.stage_durations.rerank_ms)|add/length),
        generation:(map(.stage_durations.generation_ms)|add/length)}' artifacts/telemetry/*.jsonl
```

A stage at ~0 ms on failures never ran; the fault is upstream of it. A stage at
its timeout ceiling is the fault. This is usually enough to name the component
without reading a single request's content.

### Step 4 — Reproduce offline, where detail is allowed

Once the component is identified, reproduce against **authored, non-patient**
queries where full tracebacks and full text are available and appropriate:

```bash
python -m synapse.evals.run --dataset evals/ci --fixture evals/ci/system_responses.jsonl \
  --corpus evals/ci --output artifacts/evals/incident --run-id incident --offline
python -m synapse.cli.benchmark --output artifacts/benchmarks/incident --corpus-size 2220
pytest -m "not live" tests/test_retrieval_pipeline.py tests/test_ui_pipeline.py
```

**Never** work around the constraint by adding query logging "just for this
incident". That is the change the whole policy exists to prevent, and an
incident is exactly when it will feel justified.

### 6.4 Integrity incidents (`index_unverified`)

This code means an index's contents do not match its manifest. Answers are not
being generated — the system fails closed, which is correct. Do not "fix" it by
disabling verification.

```bash
python -c "from pathlib import Path; from synapse.retrieval.index_gate import check_index; print(check_index(Path('hybrid_index')))"
python -m synapse.cli.source_pack validate --pack source_packs/diabetes-previsit
```

Rebuild the index from a verified corpus. Treat a digest mismatch nobody can
explain as a security event, not a corruption event.

### 6.5 When the code is `internal_error`

The exception type is recorded; the message is not. Reproduce locally with
`SYNAPSE_LOG_LEVEL=DEBUG`, where full detail is available on a developer machine
against non-patient input. If it only reproduces in production, add a **typed
code** for the case — extending the closed set is the sanctioned response, and
it makes the next occurrence diagnosable. Adding a message field is not.

### 6.6 Rising abstention rate

Not a failure — the system declining to answer. Check, in order: `evidence_count`
(retrieval returning less), `degraded` (reranker falling back), `index_version`
(did the index change), `claims_withheld` (verification rejecting more). A rise
after an index rebuild usually means the corpus changed; a rise after a model
change usually means the model's citations stopped verifying.

---

## 7. Architecture

```
app.py ──► synapse.ui.pipeline ──► TelemetryRecorder ──► TelemetrySink (Protocol)
                                          │                     ├── NullSink       (disabled, default)
                                          │                     ├── InMemorySink   (tests)
                                          │                     └── LocalFileSink  (local development)
                                          ├──► MetricsRegistry  (counters, histograms)
                                          ├──► CostEstimator    (versioned pricing, reused from P1)
                                          └──► redaction        (every string, on the way in)
```

The sink interface is two methods, which is what keeps the domain independent of
any vendor: adding an OpenTelemetry exporter means writing one class in one file
and touching nothing else. **If one is added, it must remain optional** — the
package's runtime dependency set is `pydantic` and nothing else, and an
observability dependency must not become a requirement for serving an answer.

Three properties hold regardless of sink:

1. **A telemetry failure never reaches a patient.** Every sink is wrapped in
   `FailSafeSink`, and the recorder's own emit path is wrapped again. A full
   disk, a broken exporter or an invalid event degrades to "no telemetry" and a
   counter.
2. **An invalid event is dropped, not repaired.** Repairing would mean guessing
   what a field meant, and a guessed record is indistinguishable from a real one.
3. **Redaction runs on the way in.** A sink cannot receive something that was
   never redacted because a code path forgot to ask.

---

## 8. Verifying the guarantees

```bash
pytest -m "not live" tests/test_telemetry_privacy.py tests/test_telemetry_metrics.py
```

Covers: no query, digest, answer or excerpt can reach an event; secrets are
redacted (including a 10,000-case generative fuzz over secrets embedded in
arbitrary text); unknown fields are rejected; quantile estimates bracket exact
values; every version stamp propagates end to end; retries and fallbacks are
measurable; telemetry can be fully disabled; and an exporter failure does not
break answering.

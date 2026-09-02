# Retrieval Runtime

**Applies to:** `synapse.retrieval` · `synapse.bench` · `app.py`
**Status:** implemented and tested. **Nothing here is clinically validated.** The
benchmark corpus is synthetic and its numbers describe ranking behaviour, not
medical quality.

---

## 1. What was replaced

Two online behaviours scaled with the corpus rather than with the query.

**Full-corpus fusion.** `Retrieval/hybrid_retriever.py` in `linear` mode — the
mode `app.py` configured — asked FAISS for `top_k=len(self.chunks)` and BM25 for
`get_all_scores()` on **every** query, then fused across the whole corpus to
return ten results. On the committed 2,220-chunk corpus that is two 2,220-element
score arrays, a 2,220-entry dict and a 2,220-iteration fusion loop, per query.

It was also unsound, independently of cost. The dense array was rebuilt through
`{chunk.chunk_id(): score}`, keyed on the legacy identifier
`f"{pmid}_chunk{index}"`, which collides for any document ingested more than once
— 162 collisions in the committed corpus (docs/quality-architecture.md §1.2,
C6/C7). Colliding chunks overwrote one another, and any chunk missing from the
map silently received a default distance of `2.0`.

**Sequential per-passage reranking.** `Retrieval/reranker.py` looped over the
candidates and issued **one chat completion per passage** — ten sequential round
trips on the patient's critical path, each re-sending the same instructions. It
had no timeout, no retry policy, and a bare `except Exception` that substituted
`retrieval_score * 10` and wrote the exception text into a field named
`rerank_reason`. A provider outage and a successful rerank were indistinguishable
downstream.

---

## 2. What replaces it

```
dense.search(top_n)      sparse.search(top_n)
        │                        │
        └────── union on stable chunk_id, deduplicated ──────┐
                                                             │
                        fuse (RRF or linear, ties on chunk_id)
                                                             │
                        source-pack eligibility filter
                                                             │
                        truncate to final_top_k
                                                             │
                ONE structured rerank request, validated
                                                             │
                        RetrievalBundle → answer layer
```

| Module | Responsibility |
| --- | --- |
| `synapse.retrieval.config` | Candidate sizes, fusion parameters, rerank limits. Recorded in run metadata. |
| `synapse.retrieval.backends` | The two component protocols, plus in-memory implementations for offline tests. |
| `synapse.retrieval.candidates` | Union, deduplication, fusion, deterministic tie-breaking. |
| `synapse.retrieval.search` | Bounded retrieval, the full-corpus guard, eligibility filter, trace. |
| `synapse.retrieval.rerank` | One batched structured request, strict validation, bounded retry, fallback. |
| `synapse.retrieval.evidence` | Candidates → the evidence the answer layer verifies against. |
| `synapse.retrieval.index_gate` | Pre-serving verification; raises rather than returning a status. |
| `synapse.retrieval.production` | Adapters over the legacy `VectorStore` / `BM25Index`. |

---

## 3. Measured results

Command, reproducible from a clean checkout:

```bash
python -m synapse.cli.benchmark \
  --output artifacts/benchmarks/retrieval-2026-08-20 \
  --corpus-size 2220 --queries-per-topic 3 \
  --provider-latency-ms 40 \
  --real-index hybrid_index/vector/index.faiss
```

Machine-readable results: `artifacts/benchmarks/retrieval-2026-08-20/results.json`.
2,220-chunk synthetic corpus, 24 queries, 6 relevant chunks per query.

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| Reranker calls (total, 24 queries) | 240 | 24 | **10× fewer** |
| Reranker calls per query | 10 | 1 | 1 primary + bounded retry |
| Rerank latency, mean (modelled) | 436.8 ms | 44.4 ms | 9.8× faster |
| Retrieval latency, mean (measured) | 24.1 ms | 12.9 ms | 1.87× faster |
| Prompt tokens, estimated | 21,158 | 13,697 | 1.55× fewer |
| Corpus fraction materialised | 1.000 | 0.038 | 26× less |

Quality, retrieval stage only (mean over 24 queries):

| Metric | Before (full-corpus linear) | After (bounded RRF) | Δ |
| --- | ---: | ---: | ---: |
| Recall@3 | 0.3542 | 0.4306 | +0.0764 |
| Recall@5 | 0.6181 | 0.7014 | +0.0833 |
| Recall@10 | 0.8542 | 0.8611 | +0.0069 |
| nDCG@3 | 0.6913 | 0.8554 | +0.1641 |
| nDCG@5 | 0.7185 | 0.8432 | +0.1247 |
| nDCG@10 | 0.7834 | 0.8528 | +0.0694 |

**Read that table carefully — the gain is not from bounding.** A third arm
isolates the two changes by holding fusion fixed at linear and varying only the
candidate limit:

| Metric | Bounding alone, Δ vs full-corpus |
| --- | ---: |
| Recall@3 | −0.0070 |
| Recall@5 | −0.0209 |
| Recall@10 | −0.0209 |
| nDCG@3 | −0.0025 |
| nDCG@5 | −0.0121 |
| nDCG@10 | −0.0107 |

So bounding costs between 0.003 and 0.021 on this set, and the improvement in
the headline table comes from switching linear fusion to RRF. Reporting the net
as though it were a benefit of bounding would be a claim the measurement does
not support.

Agreement between bounded-linear and full-corpus linear, over 24 queries: mean
set overlap **0.892**, identical top result on **20 of 24**, full overlap on
**11 of 24**.

**Unsupported-claim rate: 0.0, unchanged** (`artifacts/evals/baseline-p2r/summary.json`,
n=18). The offline evaluation replays recorded system responses, so it measures
the harness and answer layer rather than retrieval — it confirms no regression in
citation integrity, and it *cannot* detect a retrieval regression. That is what
the benchmark above exists for.

### The real FAISS index

Probed on the committed index (2,220 vectors × 1,536 dims, `IndexFlatL2`):

| Query | Mean over 20 trials |
| --- | ---: |
| `search(k=2220)` — full corpus | 0.251 ms |
| `search(k=50)` — bounded | 0.227 ms |
| Ratio | **1.11×** |

Small, and stated plainly because overclaiming here would be easy. The index is
*exact*, so every vector is scanned either way; bounding `k` saves result
selection and materialisation, not distance computation. The real savings from
this milestone are the reranker call count, the Python-side full-corpus fusion,
and the BM25 all-chunks score array. Bounding the dense query is also the
precondition for ever moving to an approximate index, where the saving would be
large.

---

## 4. Why these candidate sizes

Defaults: `dense_top_n = sparse_top_n = 50`, `final_top_k = 10`.

The sweep varies only the candidate size, with fusion held at linear so the
comparison against full-corpus fusion means what it says:

| top_n | Union (mean) | Corpus fraction | Recall@5 | nDCG@5 | Overlap vs full-corpus | Identical top-1 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 15.5 | 0.007 | 0.6389 | 0.7315 | 0.783 | 20/24 |
| 25 | 41.6 | 0.019 | 0.6319 | 0.7551 | 0.825 | 17/24 |
| **50** | **83.2** | **0.037** | **0.5972** | **0.7064** | **0.892** | **20/24** |
| 100 | 157.0 | 0.071 | 0.6181 | 0.7313 | 0.921 | 21/24 |
| 200 | 306.0 | 0.138 | 0.6111 | 0.7164 | 0.946 | 21/24 |

Reasoning:

1. **Agreement rises monotonically with candidate size; quality does not.**
   Recall@5 and nDCG@5 wander inside ±0.04 with no trend, which is noise at 24
   queries. Agreement moves steadily: 0.783 → 0.946. Agreement is therefore the
   signal worth choosing on.
2. **50 buys most of the agreement for a fraction of the union.** Going 50 → 100
   adds 0.029 agreement and doubles the union; 100 → 200 adds 0.025 and doubles
   it again. The knee is around 50.
3. **Agreement never reaches 1.0, by construction.** Bounded linear fusion
   min-max normalises over the *candidate set*, while the full-corpus path
   normalised over the corpus. That is a deliberate difference — normalising over
   a set you have not retrieved is impossible — and it means the two cannot agree
   exactly. Chasing 1.0 by raising `top_n` would reintroduce the cost this
   milestone removes to close a gap that is not a defect.
4. **Do not over-fit to this table.** The corpus is synthetic. 50 is chosen
   because it sits at the knee *and* leaves headroom for harder queries than a
   generator produces. Both values are configurable and recorded in every run's
   metadata, so a future measurement on a real labelled set can move them with
   evidence.

`final_top_k = 10` matches what the legacy path handed its reranker, so the
comparison holds that variable fixed.

---

## 5. Benchmark methodology

**Both arms run against the same corpus, the same backends and the same fake
provider.** The only difference is the algorithm.

- **Corpus** (`synapse.bench.corpus`): generated from a seed, so a given
  `(size, seed)` reproduces byte-for-byte. Six relevant chunks per query, placed
  at evenly-spread positions so a truncated candidate set cannot find them for
  the wrong reason. Distractors mix two topics plus filler, so lexical overlap
  alone does not identify a relevant chunk.
- **Embedder**: a deterministic FNV-1a hash fold (`hashed_embedder`). It has no
  semantics and must never serve a patient; it is stable across processes and
  machines, which is what a reproducible benchmark needs.
- **Baseline arm** (`synapse.bench.legacy_baseline`): a re-implementation of the
  legacy algorithm, because the real module imports `faiss` and `openai` at
  module scope and builds its client internally, so it cannot be driven offline.
  Fidelity is **tested, not asserted**:
  `tests/test_retrieval_benchmark.py::TestLegacyBaselineFidelity` runs this
  implementation and the real `Retrieval.hybrid_retriever.linear_fusion` over the
  same inputs and requires identical rankings and scores to 12 decimal places.
  If the legacy function changes, that test fails and the baseline is wrong until
  it is updated.
- **Metric implementations**: `recall_at_k` and `ndcg_at_k` come from
  `synapse.evals.metrics.retrieval` — the P1 implementations, not a second copy.

### Honesty about each number

| Number | How to read it |
| --- | --- |
| Model calls, candidate counts, corpus fraction | Exact counts. |
| Retrieval latency | Real wall-clock on the host recorded in `environment`. Meaningful as a **ratio**; absolute values are host-dependent. |
| Rerank latency | A **model**, not a measurement: the fake provider sleeps a fixed `per_call_latency_ms` per call, applied identically to both arms. It shows the shape of N sequential calls versus one. The constant is recorded in the artifact. |
| Token counts | **Estimated** at 4 characters per token, and labelled `*_estimated` everywhere. A fake provider reports no usage. |
| Recall@k / nDCG@k | Exact over the generated labels. The labels are synthetic; they say nothing about clinical quality. |
| Agreement | Label-free. The strongest available evidence that bounding does not change what a patient sees. |

Recall@3 tops out at 0.5 with six relevant chunks and k=3, so 0.43 is 86% of what
is achievable, not 43% of it.

---

## 6. Degradation ladder

| Rung | Condition | Behaviour |
| --- | --- | --- |
| 1 | Batched reranker returns a valid response | Validated reranked candidates |
| 2 | Reranker fails transiently, or returns something invalid twice | **Deterministic fused retrieval order**, marked `degraded` with a reason |
| 3 | Surviving evidence is below the display policy | **Abstain** — the P1 policy, unchanged |
| 4 | Index or manifest fails verification | **Fail closed**: no retrieval, no generation, `index_unverified` |

**No rung relaxes a P1 guarantee.** A degraded rerank produces a differently
ordered candidate list and nothing else: the answer layer still verifies every
quote against the chunk it claims to come from, still withholds unsupported
claims, and still abstains when too little survives. Asserted directly in
`tests/test_retrieval_pipeline.py::TestDegradationPreservesCitationIntegrity`.

Retry policy: at most `max_attempts` (default 3) attempts inside a
`deadline_seconds` (default 30) budget, with exponential backoff and **full
jitter**. Only transient failures are retried — timeouts, connection errors and
statuses in {408, 409, 425, 429, 500, 502, 503, 504}. A 400 or 401 is not retried,
because neither changes by being sent again. A malformed response is retried
**once** (`max_schema_attempts`): a model that cannot satisfy the contract will
not satisfy it on the fifth attempt, and every further try spends the patient's
latency budget to learn nothing.

---

## 7. Validation of the reranker response

The response maps stable `chunk_id` → `{relevance, rank, rationale}`. Four
rejections, none of them repaired:

| Rejection | Why it is not repairable |
| --- | --- |
| Unknown `chunk_id` | The model scored a passage it was never sent, so it did not read what it says it read. |
| Duplicate `chunk_id` | Two rankings for one passage; picking either invents a decision. |
| Missing candidate | A partial ranking would either drop candidates or need a score this code made up. |
| Score outside `[min_score, max_score]` | The scale is part of the contract. |

Matching is by identifier, never by position: reordering is exactly what a
reranker does, so positional matching would silently mis-attribute every score.

The prompt is versioned (`PROMPT_ID = "rerank-batched-v1"`); changing the wording
changes the identifier, so a benchmark or eval run can be attributed to the
prompt that produced it.

---

## 8. Known gaps and deadlines

- **The production index carries no manifest.** `hybrid_index/` was built by the
  legacy tree, so `check_index` reports `unmanaged` rather than `verified` —
  reported explicitly, because "verified" and "nothing to verify" are exactly the
  distinction a bare boolean loses. `check_index(..., require_manifest=True)`
  turns this into a hard failure today; flipping the default requires migrating
  the index build to `synapse.index.manifest`. **Deadline: 2026-11-30**, with the
  rest of the legacy retrieval tree.
- **Eligibility filtering is available but not yet wired in production.**
  `search_candidates(eligible_documents=…)` enforces it and is tested; `app.py`
  does not pass a set, because the production index is not built from a source
  pack. Until it is, runs record `eligibility_enforced: false` rather than
  implying governance that is not happening.
- **`synapse.retrieval.production`** adapts the legacy `VectorStore` and
  `BM25Index` and rebuilds their colliding identifiers. It disappears when
  `Retrieval/` emits typed records. Same deadline.
- **The benchmark corpus is synthetic.** These numbers justify a configuration
  choice; they are not evidence about clinical retrieval quality. A labelled
  clinician-reviewed set is the only thing that would be, and
  `docs/clinical-labeling-protocol.md` describes how one gets built.

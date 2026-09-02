"""
synapse.cli.benchmark
=====================
Run the retrieval benchmark and write machine-readable results.

    python -m synapse.cli.benchmark --output artifacts/benchmarks/<run-id>

Everything is offline and seeded, so a run reproduces byte-for-byte apart from
the latency fields, which are wall-clock and therefore host-dependent — the
comparison reports ratios, and the raw values carry the host in the metadata.

Three arms, because two would not separate the two changes being made:

* ``baseline``       full-corpus linear fusion, one rerank call per candidate
* ``bounded_linear`` bounded candidate union, **same** linear fusion, batched rerank
* ``bounded_rrf``    bounded candidate union, RRF fusion, batched rerank  (the default)

``bounded_linear`` exists to attribute quality changes. Comparing only
``baseline`` against ``bounded_rrf`` would leave "did bounding cost us recall?"
and "did switching to RRF gain us recall?" tangled together, and reporting the
net as though it were the first would be a performance claim I could not
support.

The sweep records quality at several candidate sizes, which is the evidence
behind the defaults in :mod:`synapse.retrieval.config`.
"""

from __future__ import annotations  # Postponed annotations

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from synapse.bench.corpus import build_corpus
from synapse.bench.faiss_probe import probe_real_index
from synapse.bench.retrieval import (
    ArmResult,
    agreement,
    run_baseline_arm,
    run_candidate_arm,
)
from synapse.retrieval.config import CandidateConfig, FusionStrategy, RerankConfig

SWEEP_SIZES = (10, 25, 50, 100, 200)


def _environment() -> dict[str, object]:
    """Host facts that affect the latency numbers, recorded with them."""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _ratio(before: float, after: float) -> float | None:
    """``before / after``, or None when the denominator is zero."""
    return round(before / after, 3) if after else None


def compare(baseline: ArmResult, candidate: ArmResult) -> dict[str, object]:
    """Before/after comparison for one candidate arm."""
    before = baseline.as_metadata()
    after = candidate.as_metadata()
    return {
        "arm": candidate.name,
        "model_calls": {
            "before_total": before["model_calls_total"],
            "after_total": after["model_calls_total"],
            "before_per_query_mean": before["model_calls_per_query"]["mean"],
            "after_per_query_mean": after["model_calls_per_query"]["mean"],
            "reduction_factor": _ratio(
                float(before["model_calls_total"]), float(after["model_calls_total"])
            ),
        },
        "prompt_tokens_estimated": {
            "before_total": before["prompt_tokens_estimated_total"],
            "after_total": after["prompt_tokens_estimated_total"],
            "reduction_factor": _ratio(
                float(before["prompt_tokens_estimated_total"]),
                float(after["prompt_tokens_estimated_total"]),
            ),
            "note": "estimated at 4 characters per token; a fake provider reports no usage",
        },
        "retrieval_latency_ms": {
            "before_mean": before["retrieval_latency_ms"]["mean"],
            "after_mean": after["retrieval_latency_ms"]["mean"],
            "speedup_factor": _ratio(
                float(before["retrieval_latency_ms"]["mean"]),
                float(after["retrieval_latency_ms"]["mean"]),
            ),
        },
        "rerank_latency_ms": {
            "before_mean": before["rerank_latency_ms"]["mean"],
            "after_mean": after["rerank_latency_ms"]["mean"],
            "speedup_factor": _ratio(
                float(before["rerank_latency_ms"]["mean"]),
                float(after["rerank_latency_ms"]["mean"]),
            ),
            "note": "provider latency is modelled by a fixed per-call constant, not measured against a real API",
        },
        "corpus_fraction_examined": {
            "before_mean": before["corpus_fraction_examined"]["mean"],
            "after_mean": after["corpus_fraction_examined"]["mean"],
        },
        "quality_retrieval_only": {
            "before": before["quality_retrieval_only"],
            "after": after["quality_retrieval_only"],
            "deltas": {
                key: round(
                    float(after["quality_retrieval_only"].get(key, 0.0)) - float(value),
                    4,
                )
                for key, value in before["quality_retrieval_only"].items()
            },
        },
        "quality_after_rerank": {
            "before": before["quality_after_rerank"],
            "after": after["quality_after_rerank"],
            "deltas": {
                key: round(float(after["quality_after_rerank"].get(key, 0.0)) - float(value), 4)
                for key, value in before["quality_after_rerank"].items()
            },
        },
        "agreement_with_baseline": {
            "retrieval_stage": agreement(baseline, candidate, stage="retrieval"),
            "final_stage": agreement(baseline, candidate, stage="final"),
        },
    }


def run_sweep(corpus, per_call_latency_ms: float, baseline: ArmResult) -> list[dict[str, object]]:
    """Quality and cost at a range of candidate sizes.

    This is the measurement behind the chosen defaults. Each entry uses the same
    corpus and queries; only ``dense_top_n`` and ``sparse_top_n`` change.

    Agreement with the full-corpus baseline is recorded per size, because it is
    the question a default actually turns on: not "which size scores best on
    this synthetic set?" — that over-fits — but "how many candidates are needed
    before bounding stops changing the result?"
    """
    rows: list[dict[str, object]] = []
    for size in SWEEP_SIZES:
        if size >= corpus.size:
            continue  # The guard refuses a full-corpus request, which is the point of it
        # LINEAR, deliberately: the baseline fuses linearly, so holding the
        # strategy fixed makes candidate size the only variable and the
        # agreement column mean what it says.
        arm = run_candidate_arm(
            corpus,
            config=CandidateConfig(
                dense_top_n=size, sparse_top_n=size, fusion=FusionStrategy.LINEAR
            ),
            per_call_latency_ms=per_call_latency_ms,
        )
        summary = arm.as_metadata()
        rows.append(
            {
                "dense_top_n": size,
                "sparse_top_n": size,
                "fusion": FusionStrategy.LINEAR.value,
                "union_size_mean": summary["candidates_per_query"]["mean"],
                "corpus_fraction_examined_mean": summary["corpus_fraction_examined"]["mean"],
                "retrieval_latency_ms_mean": summary["retrieval_latency_ms"]["mean"],
                "quality_retrieval_only": summary["quality_retrieval_only"],
                "agreement_with_full_corpus_baseline": agreement(baseline, arm, stage="retrieval"),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and write the artifacts."""
    parser = argparse.ArgumentParser(description="Retrieval and reranking benchmark")
    parser.add_argument("--output", type=Path, required=True, help="Directory for the results")
    parser.add_argument("--corpus-size", type=int, default=2220, help="Synthetic corpus size")
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--queries-per-topic", type=int, default=3)
    parser.add_argument(
        "--provider-latency-ms",
        type=float,
        default=40.0,
        help="Modelled per-call provider latency, applied identically to both arms",
    )
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--skip-sweep", action="store_true")
    parser.add_argument(
        "--real-index",
        type=Path,
        default=None,
        help="Optional FAISS index to probe for the real full-corpus query cost",
    )
    args = parser.parse_args(argv)

    corpus = build_corpus(
        size=args.corpus_size, seed=args.seed, queries_per_topic=args.queries_per_topic
    )

    baseline = run_baseline_arm(corpus, per_call_latency_ms=args.provider_latency_ms)
    bounded_linear = run_candidate_arm(
        corpus,
        config=CandidateConfig(fusion=FusionStrategy.LINEAR),
        rerank_config=RerankConfig(),
        per_call_latency_ms=args.provider_latency_ms,
    )
    bounded_rrf = run_candidate_arm(corpus, per_call_latency_ms=args.provider_latency_ms)

    record = {
        "run_id": args.run_id,
        "environment": _environment(),
        "corpus": corpus.as_metadata(),
        "provider_model": {
            "kind": "deterministic fake",
            "per_call_latency_ms": args.provider_latency_ms,
            "note": "scores by lexical overlap; no network, no model, no clinical meaning",
        },
        "arms": {
            "baseline": baseline.as_metadata(),
            "bounded_linear": bounded_linear.as_metadata(),
            "bounded_rrf": bounded_rrf.as_metadata(),
        },
        "comparisons": {
            "bounded_linear_vs_baseline": compare(baseline, bounded_linear),
            "bounded_rrf_vs_baseline": compare(baseline, bounded_rrf),
        },
        "candidate_size_sweep": []
        if args.skip_sweep
        else run_sweep(corpus, args.provider_latency_ms, baseline),
        "real_index_probe": (
            probe_real_index(args.real_index) if args.real_index is not None else None
        ),
        "caveats": [
            "The corpus is synthetic and carries no clinical meaning. These numbers describe ranking behaviour, not medical quality.",
            "Provider latency is modelled by a fixed per-call constant applied identically to both arms; it is not a measurement of a real API.",
            "Token counts are estimated at 4 characters per token. A fake provider reports no usage.",
            "Latency is wall-clock on the host recorded in `environment` and is only meaningful as a ratio between arms.",
        ],
    }

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Benchmark written to {args.output}/results.json")
    for name, comparison in record["comparisons"].items():  # type: ignore[union-attr]
        calls = comparison["model_calls"]  # type: ignore[index]
        quality = comparison["quality_retrieval_only"]["deltas"]  # type: ignore[index]
        print(
            f"  {name}: rerank calls {calls['before_total']} -> {calls['after_total']} "
            f"(x{calls['reduction_factor']}); retrieval-only deltas {quality}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())

"""
synapse.evals.run
=================
The evaluation CLI.

    python -m synapse.evals.run \\
      --dataset evals/datasets/<version> \\
      --source-pack source_packs/<pack> \\
      --output artifacts/evals/<run-id> \\
      --offline

``--offline`` is the default and the supported path. It runs deterministic
metrics only, replays system responses from a recorded fixture, and cannot
reach a network. Judges require ``--enable-judges`` *and* ``--no-offline``, and
even then they are advisory and gate nothing.
"""

from __future__ import annotations  # Postponed annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from synapse import __version__ as synapse_version
from synapse.corpus.jsonl import read_jsonl
from synapse.errors import SynapseArtifactError
from synapse.evals import compare as compare_module
from synapse.evals import report as report_module
from synapse.evals.harness import HarnessConfig, run_evaluation
from synapse.evals.legacy_detector import load_emergency_detector
from synapse.evals.metrics.cost import load_pricing
from synapse.evals.system import FixtureSystem, RealEmergencyRoutingSystem, SystemUnderTest
from synapse.evalset.dataset import EvalDataset
from synapse.evalset.gating import GatingPolicy
from synapse.governance.pack import SourcePack
from synapse.identifiers import parse_chunk_id
from synapse.logging import configure_logging, get_logger
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.evalrun import EvalRunResults

logger = get_logger(__name__)

EVAL_NOTICE = """\
----------------------------------------------------------------------------
  Deterministic metrics are reproducible arithmetic over recorded outputs.
  Model-judged metrics, when enabled, are ADVISORY: they are one model's
  assessment of another model's output, never clinical validation, and they
  gate nothing. Synthetic and unreviewed cases are excluded from
  release-gating aggregates by default.
----------------------------------------------------------------------------
"""


def _load_chunk_texts(corpus_dir: Path | None) -> dict[str, str]:
    """Load chunk text for verbatim citation checking.

    Without it, citation correctness cannot be computed at all — so its absence
    is reported rather than silently producing a null metric.
    """
    if corpus_dir is None:
        return {}
    # A committed JSON map is accepted as well as a corpus artifact, so CI can
    # verify citations against a small fixture without shipping a FAISS build.
    simple = corpus_dir / "chunks.json"
    if simple.is_file():
        import json as _json

        return _json.loads(simple.read_text(encoding="utf-8"))
    chunks_path = corpus_dir / "chunks.jsonl.gz"
    if not chunks_path.is_file():
        logger.warning(
            "corpus has no chunk file; citation correctness cannot be computed",
            extra={"corpus": corpus_dir.name},
        )
        return {}
    return {chunk.chunk_id: chunk.text for chunk in read_jsonl(chunks_path, EvidenceChunk)}


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m synapse.evals.run",
        description="Run the Synapse evaluation harness.",
    )
    parser.add_argument("--dataset", type=Path, required=True, help="Evaluation dataset directory.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory for results.json, summary.json, report.md, per_case.jsonl.",
    )
    parser.add_argument(
        "--source-pack",
        type=Path,
        default=None,
        help="Source pack that authorised the index under test.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Corpus artifact directory, for verbatim citation checking.",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Recorded system responses. Required in offline mode.",
    )

    parser.add_argument(
        "--allow-invalid-dataset",
        action="store_true",
        help=(
            "Evaluate despite dataset validation errors. LOCAL USE ONLY: a dataset that "
            "does not match its manifest has already lost the property the manifest exists "
            "to give, so the run's provenance cannot be trusted."
        ),
    )
    parser.add_argument(
        "--offline",
        dest="offline",
        action="store_true",
        default=True,
        help="Deterministic metrics only, no network. Default.",
    )
    parser.add_argument(
        "--no-offline",
        dest="offline",
        action="store_false",
        help="Permit live calls. Judges still require --enable-judges.",
    )
    parser.add_argument(
        "--enable-judges",
        action="store_true",
        help="Opt in to model-based judges. Advisory only; ignored when offline.",
    )

    parser.add_argument(
        "--baseline", type=Path, default=None, help="A previous results.json to compare against."
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit non-zero when a regression is detected.",
    )
    parser.add_argument(
        "--fail-on-critical-regression",
        action="store_true",
        help="Exit non-zero only for safety-critical regressions.",
    )

    parser.add_argument(
        "--pricing", type=Path, default=None, help="Pricing table for cost estimation."
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed for bootstrap intervals. Default 0, so runs reproduce.",
    )
    parser.add_argument(
        "--k",
        type=int,
        action="append",
        default=None,
        help="Retrieval cut-off. Repeatable. Default: 3, 5, 10.",
    )
    parser.add_argument(
        "--allow-unreviewed-gating",
        action="store_true",
        help="BOOTSTRAPPING ONLY. Let unreviewed cases into the gating aggregate.",
    )
    parser.add_argument(
        "--run-id", default=None, help="Run identifier. Defaults to a UTC timestamp."
    )
    parser.add_argument(
        "--real-emergency-detector",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Recompute the escalation decision with the SHIPPED detector instead of trusting "
            "the fixture's recorded verdict. On by default: emergency routing needs no model "
            "and no network, so a recorded verdict is a hand-authored wish, not a measurement. "
            "Use --no-real-emergency-detector only to reproduce a historical run."
        ),
    )
    parser.add_argument("--log-level", default="WARNING")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    configure_logging(args.log_level)
    print(
        EVAL_NOTICE, file=sys.stderr
    )  # stderr, so stdout stays usable for machine-readable output

    if args.offline and args.fixture is None:
        # Offline mode has nothing to run against without a fixture. Refusing is
        # better than falling back to a live call the flag promised to prevent.
        print("REFUSED: --offline requires --fixture (recorded system responses)", file=sys.stderr)
        return 2
    if args.enable_judges and args.offline:
        print(
            "REFUSED: --enable-judges requires --no-offline; judges cannot run offline",
            file=sys.stderr,
        )
        return 2

    try:
        dataset = EvalDataset.load(args.dataset)

        # Integrity BEFORE evaluation. EvalDataset.load parses the files; it does
        # not check that they agree with the manifest that describes them, so a
        # cases file edited without rewriting its manifest -- a changed
        # cases_sha256, a stale case_count -- loaded and scored perfectly happily,
        # and the run reported a dataset_version it no longer matched.
        #
        # That is precisely the failure the manifest exists to prevent, and this
        # is the last point where it can be caught: everything downstream treats
        # the dataset as authoritative. Mirrors the refusal in
        # `synapse.cli.source_pack build-index`, which will not let an invalid
        # pack authorise an index.
        # Chunk texts are loaded once here and reused for the harness config
        # below, so supplying them to validate() costs nothing. Without them the
        # referential-integrity check is SKIPPED and reports itself as skipped --
        # a permanent "no corpus supplied" warning on runs that did supply one,
        # which is exactly the kind of warning people learn to ignore.
        chunk_texts = _load_chunk_texts(args.corpus)
        corpus_document_ids: set[str] | None = None
        if chunk_texts:
            # chunk_id is "<document_id>#<ordinal>"; parse rather than split, so a
            # malformed identifier fails loudly instead of yielding a prefix.
            corpus_document_ids = {parse_chunk_id(chunk_id)[0] for chunk_id in chunk_texts}
        validation = dataset.validate(corpus_document_ids=corpus_document_ids)
        if not validation.is_valid and not args.allow_invalid_dataset:
            print(
                f"REFUSED: dataset has {len(validation.errors)} validation error(s); "
                "fix them or pass --allow-invalid-dataset",
                file=sys.stderr,
            )
            for issue in validation.errors:
                print(f"  {issue.render()}", file=sys.stderr)
            return 2
        for issue in validation.warnings:
            # Warnings never block, but they are printed rather than swallowed:
            # "referential integrity was not checked" is exactly the kind of
            # absence a reader would otherwise mistake for a clean result.
            print(f"  {issue.render()}", file=sys.stderr)  # render() already carries the severity

        system: SystemUnderTest | None = FixtureSystem.load(args.fixture) if args.fixture else None
        if system is None:
            print("REFUSED: no system under test; supply --fixture", file=sys.stderr)
            return 2
        if args.real_emergency_detector:
            # Safety metrics now score the shipped detector rather than the
            # fixture's opinion of it. Still fully offline: check_emergency is a
            # pure function over a constant list.
            system = RealEmergencyRoutingSystem(inner=system, detector=load_emergency_detector())

        source_pack_version = None
        if args.source_pack is not None:
            pack = SourcePack.load(args.source_pack)
            source_pack_version = f"{pack.manifest.pack_id}@{pack.manifest.version}"

        config = HarnessConfig(
            k_values=tuple(args.k) if args.k else (3, 5, 10),
            seed=args.seed,
            offline=args.offline,
            enable_judges=args.enable_judges and not args.offline,
            gating_policy=GatingPolicy(allow_unreviewed=args.allow_unreviewed_gating),
            pricing=load_pricing(args.pricing) if args.pricing else None,
            corpus_version=dataset.manifest.corpus_version,
            chunk_texts=chunk_texts,
        )

        run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = run_evaluation(
            dataset.cases,
            system,
            config,
            run_id=run_id,
            dataset_id=dataset.manifest.dataset_id,
            dataset_version=dataset.manifest.version,
            corpus_version=dataset.manifest.corpus_version,
            source_pack_version=source_pack_version,
        )

        comparison_section = ""
        comparison = None
        if args.baseline is not None:
            baseline = EvalRunResults.model_validate(
                json.loads(args.baseline.read_text(encoding="utf-8"))
            )
            comparison = compare_module.compare_runs(baseline, output.results)
            comparison_section = compare_module.render_comparison(comparison, baseline.run_id)
            (args.output).mkdir(parents=True, exist_ok=True)
            (args.output / "comparison.json").write_text(
                json.dumps(comparison.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        paths = report_module.write_all(
            args.output, output.results, output.outcomes, comparison_section
        )

    except (
        SynapseArtifactError
    ) as exc:  # Typed, user-safe failures print cleanly; anything else keeps its traceback
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    results = output.results
    print(f"Run {results.run_id}")
    print(f"  evaluated        : {results.cases_evaluated} of {results.cases_total}")
    print(f"  skipped          : {results.cases_skipped}")
    print(f"  errored          : {results.cases_errored}")
    print(f"  GATING-ELIGIBLE  : {results.cases_gating_eligible}")
    print(f"\n  artifacts -> {paths.results.parent}")
    for note in results.provenance_notes[:2]:
        print(f"\n  NOTE: {note}")

    if comparison is not None:
        print(
            f"\n  regressions: {len(comparison.regressions)} ({len(comparison.critical_regressions)} critical)"
        )
        if not comparison.comparable:
            print("  WARNING: runs are NOT comparable; deltas are informational only.")
        if args.fail_on_critical_regression and comparison.critical_regressions:
            return 1
        if args.fail_on_regression and comparison.regressions:
            return 1

    print(f"\nsynapse {synapse_version}")
    return 0


if __name__ == "__main__":  # Enables `python -m synapse.evals.run`
    raise SystemExit(main())

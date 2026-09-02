"""
synapse.cli.build_corpus
========================
Build a corpus from PubMed.

Usage::

    python -m synapse.cli.build_corpus --config configs/corpus.example.toml

Configuration is TOML, parsed with the standard library's ``tomllib`` (3.11+).
TOML is used deliberately in preference to YAML: this file is untrusted input
by the same argument as any other artifact, and ``yaml.load`` is a known
code-execution surface. ``tomllib`` has no such capability, and needs no
third-party dependency.

Two governance defaults are worth stating because they are silent:

* **Retracted publications are excluded.** Overridable with
  ``include_retracted``, but always reported either way.
* **Nothing is medically approved.** Every document is written
  ``approval_status = "unreviewed"``. Automatic retrieval establishes that a
  publication exists, nothing more.
"""

from __future__ import annotations  # Postponed annotations

import argparse  # Standard-library argument parsing
import sys  # Exit codes and stderr
import tomllib  # 3.11 stdlib TOML reader; no PyYAML, no code-execution surface
from pathlib import Path
from typing import Any

from synapse import __version__ as synapse_version
from synapse.errors import ArtifactSchemaError, SynapseArtifactError
from synapse.hashing import sha256_bytes
from synapse.ingest.chunker import ChunkingPolicy
from synapse.ingest.eutils import EUtilsClient, EUtilsConfig
from synapse.ingest.pipeline import REPORT_FILENAME, BuildConfig, build_corpus
from synapse.logging import configure_logging, get_logger

logger = get_logger(__name__)

APPROVAL_NOTICE = """\
----------------------------------------------------------------------------
  Documents produced by this command are NOT medically approved.
  Automatic retrieval from PubMed establishes only that a publication exists.
  Every record is written approval_status='unreviewed', and no clinician has
  reviewed any of it. Retracted publications are excluded by default and are
  listed individually in the build report.
----------------------------------------------------------------------------
"""


def _load_config(path: Path) -> tuple[BuildConfig, EUtilsConfig, str]:
    """Parse a TOML configuration file into typed configuration objects."""
    if not path.is_file():
        raise ArtifactSchemaError(path=path, problem="configuration file not found")
    raw_bytes = path.read_bytes()
    config_digest = sha256_bytes(raw_bytes)  # Binds the build report to the exact settings that produced it
    try:
        data: dict[str, Any] = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ArtifactSchemaError(path=path, problem="configuration is not valid TOML") from exc

    corpus = data.get("corpus", {})
    queries = data.get("queries", {}).get("terms", [])
    if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
        raise ArtifactSchemaError(path=path, problem="queries.terms must be a list of strings")
    if not queries:  # A build with no queries would silently produce an empty corpus
        raise ArtifactSchemaError(path=path, problem="queries.terms is empty")

    chunking_raw = data.get("chunking", {})
    try:
        chunking = ChunkingPolicy(
            target_chars=int(chunking_raw.get("target_chars", 800)),
            min_chars=int(chunking_raw.get("min_chars", 120)),
            max_chars=int(chunking_raw.get("max_chars", 1600)),
            keep_short_sections=bool(chunking_raw.get("keep_short_sections", False)),
        )
    except (TypeError, ValueError) as exc:  # A malformed policy fails here rather than producing unusable chunks
        raise ArtifactSchemaError(path=path, problem="invalid chunking configuration") from exc

    embedding = data.get("embedding", {})
    build_config = BuildConfig(
        queries=queries,
        output_dir=Path(corpus.get("output_dir", "artifacts/corpus-v3")),
        corpus_version=str(corpus.get("version", "corpus-v3")),
        source_pack_version=corpus.get("source_pack_version"),
        index_version=str(corpus.get("index_version", "1")),
        max_results_per_query=int(data.get("queries", {}).get("max_results_per_query", 20)),
        sort=str(data.get("queries", {}).get("sort", "relevance")),
        include_retracted=bool(corpus.get("include_retracted", False)),  # Governance default: exclude
        chunking=chunking,
        embedding_model=str(embedding.get("model", "text-embedding-3-small")),
        embedding_dimensions=int(embedding.get("dimensions", 1536)),
        embedding_provider=str(embedding.get("provider", "openai")),
        config_sha256=config_digest,
    )

    ncbi = data.get("ncbi", {})
    # from_environment reads NCBI_API_KEY / NCBI_EMAIL; explicit config wins.
    # Neither is required — their absence lowers the request rate only.
    client_config = EUtilsConfig.from_environment(
        email=ncbi.get("email"),
        tool=ncbi.get("tool"),
        timeout_seconds=ncbi.get("timeout_seconds"),
        max_attempts=ncbi.get("max_attempts"),
        requests_per_second=ncbi.get("requests_per_second"),
    )
    return build_config, client_config, config_digest


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m synapse.cli.build_corpus",
        description="Build an evidence-graded corpus from PubMed.",
        epilog="Documents are never medically approved by this command. See docs/ingestion.md.",
    )
    parser.add_argument("--config", type=Path, required=True, help="TOML configuration file.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Override the configured output directory.")
    parser.add_argument("--dry-run", action="store_true", help="Retrieve and analyse without writing artifacts.")
    parser.add_argument("--force", action="store_true", help="Overwrite a non-empty output directory.")
    parser.add_argument("--include-retracted", action="store_true", help="Include retracted material, still marked with its retraction status. Off by default.")
    parser.add_argument("--log-level", default="INFO", help="Logging level. Default: INFO.")
    parser.add_argument("--log-json", action="store_true", help="Emit logs as one JSON object per line.")
    return parser


def _render_summary(report: Any) -> str:
    """Render the build report as an aligned console summary."""
    rows = [
        ("queries run", len(report.queries)),
        ("documents fetched", report.documents_fetched),
        ("duplicates dropped", len(report.duplicate_decisions)),
        ("  by pmid", report.duplicates_by_rule.get("pmid", 0)),
        ("  by doi", report.duplicates_by_rule.get("doi", 0)),
        ("  by title+year", report.duplicates_by_rule.get("title_year", 0)),
        ("documents after dedupe", report.documents_after_dedupe),
        ("excluded", len(report.exclusions)),
        ("documents written", report.documents_written),
        ("chunks written", report.chunks_written),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label.ljust(width)} : {value}" for label, value in rows)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    configure_logging(args.log_level, json_output=args.log_json)  # Configured once, here, never in a library module

    print(APPROVAL_NOTICE, file=sys.stderr)  # stderr, so stdout stays usable for machine-readable output

    try:
        build_config, client_config, _ = _load_config(args.config)
    except SynapseArtifactError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2

    if args.output_dir is not None:  # Command-line override wins over the file
        build_config.output_dir = args.output_dir
    if args.force:
        build_config.force = True
    if args.include_retracted:  # Only ever widens what is included, and the report records that it was set
        build_config.include_retracted = True

    client = EUtilsClient(client_config)

    try:
        outcome = build_corpus(build_config, client=client, dry_run=args.dry_run)
    except SynapseArtifactError as exc:  # Typed, user-safe failures print cleanly; anything else keeps its traceback
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1

    print("\nCorpus build report")
    print(_render_summary(outcome.report))
    if outcome.output_dir is None:
        print("\nDry run: no files were written.")
    else:
        print(f"\nWrote and verified artifacts in: {outcome.output_dir}")
        print(f"Machine-readable report: {outcome.output_dir / REPORT_FILENAME}")
    for note in outcome.report.provenance_notes:  # Printed every run so the caveats travel with the numbers
        print(f"\nNOTE: {note}")
    print(f"\nsynapse {synapse_version}")
    return 0


if __name__ == "__main__":  # Enables `python -m synapse.cli.build_corpus`
    raise SystemExit(main())

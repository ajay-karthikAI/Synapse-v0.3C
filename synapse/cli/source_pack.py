"""
synapse.cli.source_pack
=======================
Source-pack governance workflows.

Usage::

    python -m synapse.cli.source_pack init        --pack-id diabetes-previsit ...
    python -m synapse.cli.source_pack import      --pack DIR --documents FILE
    python -m synapse.cli.source_pack validate    --pack DIR
    python -m synapse.cli.source_pack review      --pack DIR --document-id ID --review FILE
    python -m synapse.cli.source_pack version     --pack DIR --level minor
    python -m synapse.cli.source_pack diff        --from DIR --to DIR
    python -m synapse.cli.source_pack build-index --pack DIR --corpus DIR --out DIR

Separation of duties is visible in the command surface itself:

* ``import`` is the automated path. It has **no** flag that can set a lifecycle
  state, so it cannot produce anything but ``discovered``.
* ``review`` is the human path. It requires a review file authored by a person,
  containing a reviewer identifier issued out-of-band. This tool never
  generates a reviewer identity, and there is no ``--approve`` shortcut.

Review files are JSON, parsed with ``json.loads``. A review is untrusted input
like any other artifact, so no evaluated format is accepted.
"""

from __future__ import annotations  # Postponed annotations

import argparse  # Standard-library argument parsing
import json  # Review files and machine-readable output
import shutil  # Version snapshots
import sys  # Exit codes and stderr
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from synapse import __version__ as synapse_version
from synapse.corpus.jsonl import read_jsonl, write_jsonl
from synapse.errors import SynapseArtifactError
from synapse.governance.compare import diff_packs
from synapse.governance.eligibility import EligibilityPolicy, select_eligible
from synapse.governance.pack import (
    CHANGELOG_FILENAME,
    README_FILENAME,
    VERSIONS_DIRNAME,
    SourcePack,
)
from synapse.governance.review import import_discovered, record_review
from synapse.governance.states import GovernanceError
from synapse.governance.versioning import ChangeLevel, bump_version, require_version_increase
from synapse.index.manifest import build_index_manifest, make_artifact, write_manifest
from synapse.index.verify import verify_artifacts
from synapse.logging import configure_logging, get_logger
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import (
    ChunkingAlgorithm,
    DistanceMetric,
    EvidenceType,
    PackApprovalState,
    ReviewerRole,
)
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig
from synapse.schemas.source import SourceDocument
from synapse.schemas.source_pack import (
    ClinicianReview,
    PackScope,
    ReviewerRequirements,
    SourcePackManifest,
    SourceSelectionPolicy,
)

logger = get_logger(__name__)

GOVERNANCE_NOTICE = """\
----------------------------------------------------------------------------
  This tool records governance decisions. It does not make them.
  No command generates a reviewer identity or an approval record. Reviewer
  identifiers are pseudonyms issued outside this repository and supplied by an
  operator. Automated import can only ever create 'discovered' entries.
----------------------------------------------------------------------------
"""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    """Create a new pack directory with a draft manifest and empty sources."""
    directory: Path = args.pack
    if (
        directory.exists() and any(directory.iterdir()) and not args.force
    ):  # Never clobber a pack that may hold reviews
        print(f"REFUSED: {directory} is not empty; pass --force to overwrite", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    # A pack is born in DRAFT (or UNAPPROVED_EXAMPLE) — never approved. There is
    # no flag on this command that can create an approved pack.
    state = PackApprovalState.UNAPPROVED_EXAMPLE if args.example else PackApprovalState.DRAFT

    manifest = SourcePackManifest(
        pack_id=args.pack_id,
        version=args.version,
        title=args.title,
        description=args.description,
        scope=PackScope(
            intended_patient_population=args.population,
            intended_use=args.intended_use,
            excluded_uses=args.excluded_use
            or ["Any use not explicitly listed under intended_use."],
            supported_topics=args.topic or ["<undefined — populate before review>"],
        ),
        selection_policy=SourceSelectionPolicy(
            description=args.selection_policy,
            minimum_evidence_types=[
                EvidenceType(v)
                for v in (
                    args.minimum_evidence_type
                    or ["guideline", "systematic_review", "meta_analysis", "rct"]
                )
            ],
        ),
        reviewer_requirements=ReviewerRequirements(
            required_role=ReviewerRole(args.required_reviewer_role),
            rubric_version=args.rubric_version,
            review_interval_days=args.review_interval_days,
        ),
        approval_state=state,
        corpus_sha256="0" * 64,  # Placeholder; write() recomputes it from the (empty) sources file
        source_count=0,
        build_tool_version=synapse_version,
        created_at=now,
        updated_at=now,
    )

    pack = SourcePack(manifest=manifest, sources=[], directory=directory)
    pack.write()  # Recomputes corpus_sha256, source_count and lifecycle_counts

    _write_scaffold(pack, example=args.example)
    print(f"Initialised source pack '{args.pack_id}' v{args.version} in {directory}")
    print(f"  approval_state: {state.value}")
    print("  0 sources. Import discovered sources, then have them reviewed.")
    return 0


def _write_scaffold(pack: SourcePack, *, example: bool) -> None:
    """Write CHANGELOG.md and README.md for a new pack."""
    manifest = pack.manifest
    banner = (
        "> **UNAPPROVED EXAMPLE.** This pack is demonstration content. It is not clinically\n"
        "> reviewed and must never be used to serve patient-facing traffic.\n\n"
        if example
        else ""
    )
    (pack.directory / README_FILENAME).write_text(
        f"# {manifest.title}\n\n"
        f"{banner}"
        f"**Pack ID:** `{manifest.pack_id}` · **Version:** `{manifest.version}` · "
        f"**Approval state:** `{manifest.approval_state.value}`\n\n"
        f"{manifest.description}\n\n"
        "## Scope\n\n"
        f"- **Intended population:** {manifest.scope.intended_patient_population}\n"
        f"- **Intended use:** {manifest.scope.intended_use}\n"
        f"- **Excluded uses:**\n"
        + "".join(f"  - {u}\n" for u in manifest.scope.excluded_uses)
        + "\n## Review\n\n"
        f"Requires a `{manifest.reviewer_requirements.required_role.value}` reviewer under rubric "
        f"`{manifest.reviewer_requirements.rubric_version}`, re-reviewed every "
        f"{manifest.reviewer_requirements.review_interval_days} days.\n\n"
        "See `docs/source-governance.md` for the operational workflow.\n",
        encoding="utf-8",
    )
    (pack.directory / CHANGELOG_FILENAME).write_text(
        f"# Changelog — {manifest.pack_id}\n\n"
        "All content changes require a version bump. A content change revokes any\n"
        "existing pack approval; see `docs/source-governance.md`.\n\n"
        f"## {manifest.version} — {manifest.created_at.date().isoformat()}\n\n"
        f"- Pack initialised in `{manifest.approval_state.value}` state with 0 sources.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def _cmd_import(args: argparse.Namespace) -> int:
    """Import ingested corpus documents as 'discovered' sources.

    The automated half of the separation of duties. Note there is no flag here
    that can set a lifecycle state or attach a review.
    """
    pack = SourcePack.load(args.pack)
    documents = list(read_jsonl(args.documents, SourceDocument))  # Validated on read

    created, skipped = import_discovered(
        documents, discovery_method=args.discovery_method, existing=pack.sources
    )
    pack.sources.extend(created)
    pack.write()

    print(
        f"Imported {len(created)} discovered source(s) into {pack.manifest.pack_id} v{pack.manifest.version}"
    )
    if skipped:
        # Skipping rather than overwriting is what stops a re-run from resetting
        # a completed review back to 'discovered'.
        print(f"  skipped {len(skipped)} already present (existing reviews preserved)")
    print("  All imported sources are 'discovered'. None is approved for use.")
    print(
        f"  Pack contents changed — bump the version before approval: {pack.manifest.corpus_sha256[:12]}…"
    )
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate a pack against every governance rule."""
    pack = SourcePack.load(args.pack)
    report = pack.validate(as_of=args.as_of)

    if args.json:  # Machine-readable, for CI
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Pack {report.pack_id} v{report.version}")
        print(f"  sources        : {len(pack.sources)}")
        print(f"  approval state : {pack.manifest.approval_state.value}")
        for state, count in sorted(pack.lifecycle_counts().items()):
            print(f"    {state:12} : {count}")
        if report.issues:
            print(f"\n  {len(report.errors)} error(s), {len(report.warnings)} warning(s):")
            for issue in report.issues:
                print(f"    {issue.render()}")
        else:
            print("\n  No issues.")
    return 0 if report.is_valid else 1


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def _cmd_review(args: argparse.Namespace) -> int:
    """Record a human review from an operator-authored JSON file.

    The review file must contain a reviewer identifier issued out-of-band. This
    command has no option to generate one, and no option to approve without a
    completed review document.
    """
    pack = SourcePack.load(args.pack)
    source = pack.by_id(args.document_id)
    if source is None:
        print(f"ERROR: no source '{args.document_id}' in this pack", file=sys.stderr)
        return 1

    try:
        payload = json.loads(args.review.read_text(encoding="utf-8"))  # SAFE deserialisation
    except json.JSONDecodeError as exc:
        print(f"ERROR: review file is not valid JSON: {exc.msg}", file=sys.stderr)
        return 2
    try:
        review = ClinicianReview.model_validate(
            payload
        )  # Every one of the six assessment fields is mandatory
    except ValidationError as exc:
        print(
            f"ERROR: review file is incomplete or invalid ({exc.error_count()} problem(s)).",
            file=sys.stderr,
        )
        print(
            "       Every assessment field is mandatory: relevance, population_applicability,",
            file=sys.stderr,
        )
        print(
            "       evidence_quality, known_limitations, patient_safety_concerns, decision_rationale.",
            file=sys.stderr,
        )
        return 2

    try:
        updated = record_review(
            source, review, manifest=pack.manifest, exclusion_reason=args.exclusion_reason
        )
    except GovernanceError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    pack.replace_source(updated)
    pack.write()
    print(
        f"Recorded review of {args.document_id}: {review.decision.value} -> {updated.lifecycle_state.value}"
    )
    print(f"  reviewer   : {review.reviewer_id} ({review.reviewer_role.value})")
    print(f"  review due : {review.review_due_at.isoformat()}")
    print(f"  Pack contents changed — bump the version: {pack.manifest.corpus_sha256[:12]}…")
    return 0


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def _cmd_version(args: argparse.Namespace) -> int:
    """Snapshot the current version and bump to the next one."""
    pack = SourcePack.load(args.pack)
    previous_version = pack.manifest.version

    # Snapshot BEFORE bumping, so `diff` has a real predecessor to compare
    # against rather than relying on version control being present.
    snapshot_dir = pack.directory / VERSIONS_DIRNAME / previous_version
    if snapshot_dir.exists() and not args.force:
        print(
            f"REFUSED: snapshot for v{previous_version} already exists; pass --force to overwrite",
            file=sys.stderr,
        )
        return 2
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "sources.jsonl"):
        shutil.copy2(pack.directory / name, snapshot_dir / name)

    try:
        bumped = bump_version(pack.manifest, ChangeLevel(args.level))
    except GovernanceError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    require_version_increase(previous_version, bumped.version)

    approval_was_revoked = (
        pack.manifest.approval_state is PackApprovalState.APPROVED
        and bumped.approval_state is not PackApprovalState.APPROVED
    )
    pack.manifest = bumped
    pack.write()

    changelog = pack.directory / CHANGELOG_FILENAME
    entry = (
        f"\n## {bumped.version} — {datetime.now(UTC).date().isoformat()}\n\n"
        f"- {args.level} change from {previous_version}.\n"
        f"- {args.note}\n"
    )
    if approval_was_revoked:
        entry += "- **Pack approval revoked**: contents changed, so the previous clinical approval no longer applies.\n"
    changelog.write_text(changelog.read_text(encoding="utf-8") + entry, encoding="utf-8")

    print(f"Version {previous_version} -> {bumped.version} ({args.level})")
    print(f"  previous version snapshotted to {snapshot_dir}")
    if approval_was_revoked:
        print("  PACK APPROVAL REVOKED: contents changed, so the prior approval no longer applies.")
    return 0


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def _cmd_diff(args: argparse.Namespace) -> int:
    """Compare two pack versions by governance consequence."""
    previous = SourcePack.load(args.from_pack)
    current = SourcePack.load(args.to_pack)
    diff = diff_packs(previous.manifest, previous.sources, current.manifest, current.sources)
    print(json.dumps(diff.as_dict(), indent=2, sort_keys=True) if args.json else diff.render())
    return 0


# ---------------------------------------------------------------------------
# build-index
# ---------------------------------------------------------------------------


def _cmd_build_index(args: argparse.Namespace) -> int:
    """Build a corpus artifact containing only pack-eligible sources."""
    pack = SourcePack.load(args.pack)

    report = pack.validate(as_of=args.as_of)
    if (
        not report.is_valid and not args.allow_invalid
    ):  # A pack that fails validation cannot authorise an index
        print(
            f"REFUSED: pack has {len(report.errors)} validation error(s); fix them or pass --allow-invalid",
            file=sys.stderr,
        )
        for issue in report.errors:
            print(f"  {issue.render()}", file=sys.stderr)
        return 1

    policy = EligibilityPolicy(
        allow_unapproved=args.allow_unapproved, allow_example_packs=args.allow_example_packs
    )
    try:
        eligible, decisions = select_eligible(
            pack.sources, pack.manifest, policy, as_of=args.as_of or date.today()
        )
    except GovernanceError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    if not eligible:
        print("REFUSED: no eligible sources; nothing to index", file=sys.stderr)
        for decision in decisions:
            print(f"  {decision.document_id}: {decision.reason}", file=sys.stderr)
        return 1

    eligible_ids = {s.document_id for s in eligible}
    chunks = [
        c
        for c in read_jsonl(args.corpus / "chunks.jsonl.gz", EvidenceChunk)
        if c.document_id in eligible_ids
    ]
    documents = [
        d
        for d in read_jsonl(args.corpus / "documents.jsonl.gz", SourceDocument)
        if d.document_id in eligible_ids
    ]
    if not chunks:
        print("REFUSED: no chunks in the corpus belong to eligible sources", file=sys.stderr)
        return 1

    output: Path = args.out
    if output.exists() and any(output.iterdir()) and not args.force:
        print(f"REFUSED: {output} is not empty; pass --force", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)

    chunks_path, documents_path = output / "chunks.jsonl.gz", output / "documents.jsonl.gz"
    write_jsonl(chunks_path, chunks)
    write_jsonl(documents_path, documents)

    manifest = build_index_manifest(
        index_id=f"{pack.manifest.pack_id}-{pack.manifest.version}",
        index_version=pack.manifest.version,
        corpus_version=f"{pack.manifest.pack_id}@{pack.manifest.version}",
        # Both fields are what require_pack_index_agreement() checks before serving.
        source_pack_version=f"{pack.manifest.pack_id}@{pack.manifest.version}",
        source_pack_sha256=pack.manifest.corpus_sha256,
        chunks=chunks,
        artifacts=[
            make_artifact(chunks_path, "chunks", relative_to=output),
            make_artifact(documents_path, "documents", relative_to=output),
        ],
        embedding=EmbeddingConfig(
            provider="openai", model="text-embedding-3-small", dimensions=1536, l2_normalized=True
        ),
        chunking=ChunkingConfig(
            algorithm=ChunkingAlgorithm.SECTION_AWARE_SENTENCE,
            chunk_size=800,
            overlap=0,
            min_chunk_chars=120,
        ),
        distance_metric=DistanceMetric.L2,
        document_count=len(documents),
        faiss_path=None,  # No vectors computed here; the manifest declares 'rebuild_required'
        repository_root=Path.cwd(),
    )
    write_manifest(output / "manifest.json", manifest)
    verify_artifacts(output, deep=True)  # Verify what was written before reporting success

    (output / "eligibility_report.json").write_text(
        json.dumps(
            {
                "pack_id": pack.manifest.pack_id,
                "pack_version": pack.manifest.version,
                "pack_approval_state": pack.manifest.approval_state.value,
                "policy": policy.describe(),
                "eligible": len(eligible),
                "excluded": len(decisions) - len(eligible),
                "decisions": [
                    {"document_id": d.document_id, "eligible": d.eligible, "reason": d.reason}
                    for d in decisions
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Built index from {len(eligible)}/{len(pack.sources)} eligible source(s) -> {output}")
    print(f"  policy: {policy.describe()}")
    if policy.allow_unapproved:
        print(
            "  WARNING: built with --allow-unapproved. This index is NOT governed and must not serve traffic."
        )
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _iso_date(value: str) -> date:
    """Parse a YYYY-MM-DD argument."""
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m synapse.cli.source_pack",
        description="Source-pack governance workflows. Records decisions; never makes them.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Initialise a new source pack.")
    p.add_argument("--pack", type=Path, required=True, help="Directory to create.")
    p.add_argument("--pack-id", required=True, help="Stable pack identifier, lowercase kebab-case.")
    p.add_argument("--version", default="0.1.0", help="Initial semantic version. Default: 0.1.0")
    p.add_argument("--title", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--population", required=True, help="Intended patient population.")
    p.add_argument("--intended-use", required=True)
    p.add_argument(
        "--excluded-use", action="append", help="An explicitly excluded use. Repeatable."
    )
    p.add_argument("--topic", action="append", help="A supported topic. Repeatable.")
    p.add_argument(
        "--selection-policy",
        default="Sources selected by automated PubMed discovery, pending human review.",
    )
    p.add_argument(
        "--minimum-evidence-type", action="append", help="An acceptable evidence type. Repeatable."
    )
    p.add_argument("--required-reviewer-role", default="clinician")
    p.add_argument("--rubric-version", default="rubric-1")
    p.add_argument("--review-interval-days", type=int, default=365)
    p.add_argument(
        "--example",
        action="store_true",
        help="Create as 'unapproved_example'. Such a pack can never build a production index.",
    )
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("import", help="Import ingested documents as 'discovered' sources.")
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument(
        "--documents",
        type=Path,
        required=True,
        help="documents.jsonl(.gz) from an ingestion build.",
    )
    p.add_argument(
        "--discovery-method",
        default="pubmed_esearch",
        help="How these sources were found; recorded on every entry.",
    )
    p.set_defaults(func=_cmd_import)

    p = sub.add_parser("validate", help="Validate a pack against every governance rule.")
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument(
        "--as-of", type=_iso_date, default=None, help="Date for expiry checks. Default: today."
    )
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("review", help="Record a human review from an operator-authored JSON file.")
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument("--document-id", required=True)
    p.add_argument(
        "--review",
        type=Path,
        required=True,
        help="Completed review file. See templates/clinician_review.template.json.",
    )
    p.add_argument(
        "--exclusion-reason", default=None, help="Required when the decision is 'rejected'."
    )
    p.set_defaults(func=_cmd_review)

    p = sub.add_parser("version", help="Snapshot the current version and bump to the next.")
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument("--level", choices=["patch", "minor", "major"], required=True)
    p.add_argument("--note", default="Contents updated.", help="Changelog entry.")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_version)

    p = sub.add_parser("diff", help="Compare two pack versions by governance consequence.")
    p.add_argument("--from", dest="from_pack", type=Path, required=True)
    p.add_argument("--to", dest="to_pack", type=Path, required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_diff)

    p = sub.add_parser("build-index", help="Build a corpus artifact from eligible sources only.")
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument(
        "--corpus", type=Path, required=True, help="Ingestion artifact directory to filter."
    )
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--as-of", type=_iso_date, default=None)
    p.add_argument(
        "--allow-unapproved",
        action="store_true",
        help="LOCAL EXPERIMENTATION ONLY. Recorded in the output; never for traffic.",
    )
    p.add_argument(
        "--allow-example-packs",
        action="store_true",
        help="Permit an unapproved_example pack. Never for traffic.",
    )
    p.add_argument(
        "--allow-invalid",
        action="store_true",
        help="Build despite validation errors. Never for traffic.",
    )
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_build_index)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging("INFO")  # Configured once, here, never in a library module
    print(
        GOVERNANCE_NOTICE, file=sys.stderr
    )  # stderr, so stdout stays usable for machine-readable output
    try:
        return int(args.func(args))
    except (
        SynapseArtifactError
    ) as exc:  # Typed, user-safe failures print cleanly; anything else keeps its traceback
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # Enables `python -m synapse.cli.source_pack`
    raise SystemExit(main())

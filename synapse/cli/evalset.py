"""
synapse.cli.evalset
===================
Evaluation-dataset labelling workflows.

Usage::

    python -m synapse.cli.evalset init          --dataset DIR --dataset-id ID ...
    python -m synapse.cli.evalset add-case      --dataset DIR --case FILE
    python -m synapse.cli.evalset validate      --dataset DIR [--corpus DIR]
    python -m synapse.cli.evalset export-sheet  --dataset DIR --out FILE
    python -m synapse.cli.evalset import-sheet  --dataset DIR --sheet FILE
    python -m synapse.cli.evalset adjudicate    --dataset DIR --case-id ID --record FILE
    python -m synapse.cli.evalset agreement     --dataset DIR
    python -m synapse.cli.evalset split         --dataset DIR
    python -m synapse.cli.evalset gating        --dataset DIR
    python -m synapse.cli.evalset diff          --from DIR --to DIR [--changelog]

Separation of duties is visible in the command surface:

* ``add-case`` authors cases. It cannot set an annotation status other than
  ``synthetic`` or ``pending_review``, so authoring can never produce a
  reviewed case.
* ``import-sheet`` files reviews from a completed spreadsheet. Reviewer
  identifiers come from the sheet, typed by an operator; this tool never
  generates one.
* ``gating`` reports which cases may influence a release. It is the command CI
  runs, and it is deliberately separate from every command that edits data.
"""

from __future__ import annotations  # Postponed annotations

import argparse  # Standard-library argument parsing
import json  # Case and adjudication files; machine-readable output
import sys  # Exit codes and stderr
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from synapse import __version__ as synapse_version
from synapse.corpus.jsonl import read_jsonl
from synapse.errors import SynapseArtifactError
from synapse.evalset.agreement import compute_agreement, disagreement_matrix
from synapse.evalset.compare import diff_datasets, generate_changelog_entry
from synapse.evalset.dataset import CHANGELOG_FILENAME, EvalDataset, new_manifest
from synapse.evalset.gating import GatingPolicy, evaluate_dataset
from synapse.evalset.splits import detect_leakage, plan_splits
from synapse.evalset.spreadsheet import export_review_sheet, import_review_sheet
from synapse.logging import configure_logging, get_logger
from synapse.schemas.enums import AnnotationStatus, DisagreementStatus, ReviewerRole
from synapse.schemas.evalset import Adjudication, EvalCase
from synapse.schemas.source import SourceDocument

logger = get_logger(__name__)

LABELLING_NOTICE = """\
----------------------------------------------------------------------------
  This tool records labelling decisions. It does not make them.
  No command generates a reviewer identity, a signature, or an approval.
  Cases marked 'synthetic' are engineering-authored illustrations and are
  excluded from release-gating metrics by construction.
----------------------------------------------------------------------------
"""


def _cmd_init(args: argparse.Namespace) -> int:
    """Create a new dataset directory with an empty case file."""
    directory: Path = args.dataset
    if (
        directory.exists() and any(directory.iterdir()) and not args.force
    ):  # Never clobber a dataset that may hold reviews
        print(f"REFUSED: {directory} is not empty; pass --force to overwrite", file=sys.stderr)
        return 2

    manifest = new_manifest(
        dataset_id=args.dataset_id,
        version=args.version,
        corpus_version=args.corpus_version,
        protocol_version=args.protocol_version,
        generator=f"synapse {synapse_version}",
        minimum_reviews_for_gating=args.minimum_reviews,
    )
    dataset = EvalDataset(manifest=manifest, cases=[], directory=directory)
    dataset.write()

    (directory / CHANGELOG_FILENAME).write_text(
        f"# Changelog — {args.dataset_id}\n\n"
        "Label changes and split reassignments make scores incomparable across versions.\n"
        "See `docs/clinical-labeling-protocol.md`.\n\n"
        f"## {args.version} — {datetime.now(UTC).date().isoformat()}\n\n"
        "- Dataset initialised with 0 cases.\n",
        encoding="utf-8",
    )
    print(f"Initialised dataset '{args.dataset_id}' v{args.version} in {directory}")
    print(f"  gating requires {args.minimum_reviews} independent review(s) per case")
    return 0


def _cmd_add_case(args: argparse.Namespace) -> int:
    """Add an authored case from a JSON file.

    Authoring can only produce ``synthetic`` or ``pending_review`` cases. A file
    claiming any other status is refused, so a case cannot be born reviewed.
    """
    dataset = EvalDataset.load(args.dataset)
    try:
        payload = json.loads(args.case.read_text(encoding="utf-8"))  # SAFE deserialisation
    except json.JSONDecodeError as exc:
        print(f"ERROR: case file is not valid JSON: {exc.msg}", file=sys.stderr)
        return 2

    payload.setdefault("dataset_version", dataset.manifest.version)
    payload.setdefault("corpus_version", dataset.manifest.corpus_version)
    payload.setdefault("created_at", datetime.now(UTC).isoformat())

    status = payload.get("annotation_status", AnnotationStatus.PENDING_REVIEW.value)
    if status not in (AnnotationStatus.SYNTHETIC.value, AnnotationStatus.PENDING_REVIEW.value):
        # The authoring boundary: a reviewed case is produced by filing a
        # review, never by declaring one in an input file.
        print(
            f"REFUSED: authoring may only create 'synthetic' or 'pending_review' cases, not '{status}'",
            file=sys.stderr,
        )
        return 2

    try:
        case = EvalCase.model_validate(payload)
    except ValidationError as exc:
        print(f"ERROR: case failed validation ({exc.error_count()} problem(s)):", file=sys.stderr)
        for error in exc.errors()[:5]:
            print(f"  {'.'.join(str(p) for p in error['loc'])}: {error['msg']}", file=sys.stderr)
        return 2

    if dataset.by_id(case.case_id) is not None:
        print(f"REFUSED: case '{case.case_id}' already exists", file=sys.stderr)
        return 1

    dataset.cases.append(case)
    dataset.write()
    print(f"Added case {case.case_id} ({case.category.value}, {case.annotation_status.value})")
    if case.is_synthetic:
        print("  SYNTHETIC: this case is excluded from release-gating metrics.")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate a dataset against every rule."""
    dataset = EvalDataset.load(args.dataset)

    corpus_ids: set[str] | None = None
    if args.corpus is not None:
        # Referential integrity is only checkable with a corpus in hand. Without
        # one the validator says so rather than reporting a clean dataset.
        corpus_ids = {
            d.document_id for d in read_jsonl(args.corpus / "documents.jsonl.gz", SourceDocument)
        }

    report = dataset.validate(corpus_document_ids=corpus_ids)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Dataset {report.dataset_id} v{report.version}")
        print(f"  cases: {len(dataset.cases)}")
        for status, count in sorted(dataset.status_counts().items()):
            print(f"    {status:16} : {count}")
        if report.issues:
            print(f"\n  {len(report.errors)} error(s), {len(report.warnings)} warning(s):")
            for issue in report.issues[:40]:
                print(f"    {issue.render()}")
            if len(report.issues) > 40:
                print(f"    …and {len(report.issues) - 40} more.")
        else:
            print("\n  No issues.")
    return 0 if report.is_valid else 1


def _cmd_export_sheet(args: argparse.Namespace) -> int:
    """Export a spreadsheet for reviewer labelling."""
    dataset = EvalDataset.load(args.dataset)
    rows = export_review_sheet(dataset.cases, args.out, reviewer_id=args.reviewer_id)
    print(f"Exported {rows} case(s) for review -> {args.out}")
    print("  Synthetic and excluded cases are omitted; they are not candidates for review.")
    if args.reviewer_id:
        print(f"  Pre-filled reviewer_id: {args.reviewer_id}")
    else:
        print("  reviewer_id is blank: the reviewer must enter the pseudonym issued to them.")
    return 0


def _cmd_import_sheet(args: argparse.Namespace) -> int:
    """File reviews from a completed spreadsheet."""
    dataset = EvalDataset.load(args.dataset)
    result = import_review_sheet(
        args.sheet,
        protocol_version=dataset.manifest.protocol_version,
        reviewer_role=ReviewerRole(args.reviewer_role),
    )

    if result.errors:
        print(f"REFUSED: {len(result.errors)} row(s) could not be parsed:", file=sys.stderr)
        for message in result.errors[:20]:
            print(f"  {message}", file=sys.stderr)
        return 1

    applied, skipped = 0, []
    for imported in result.reviews:
        case = dataset.by_id(imported.case_id)
        if case is None:
            skipped.append(imported.case_id)
            continue
        if case.is_synthetic:
            # The schema forbids reviews on synthetic cases; refusing here gives
            # the operator a clear message instead of a validation traceback.
            skipped.append(f"{imported.case_id} (synthetic)")
            continue
        if any(r.reviewer_id == imported.review.reviewer_id for r in case.reviews):
            skipped.append(f"{imported.case_id} (reviewer already filed)")
            continue

        reviews = [*case.reviews, imported.review]
        # Status advances to 'reviewed' on the first review. Adjudication is a
        # separate, explicit command — importing a sheet can never resolve a
        # disagreement, only create one.
        candidate = case.model_copy(
            update={"reviews": reviews, "annotation_status": AnnotationStatus.REVIEWED}
        )
        recomputed = candidate.model_copy(
            update={"disagreement_status": candidate._compute_disagreement_status()}
        )  # Deriving the field the model will validate; the alternative is duplicating the rule here
        dataset.replace_case(EvalCase.model_validate(recomputed.model_dump()))
        applied += 1

    dataset.write()
    print(f"Filed {applied} review(s); {result.skipped_rows} blank row(s) skipped")
    if skipped:
        print(f"  {len(skipped)} row(s) not applied: {', '.join(skipped[:5])}")

    open_disagreements = [
        c.case_id
        for c in dataset.cases
        if c.disagreement_status is DisagreementStatus.DISAGREED_OPEN
    ]
    if open_disagreements:
        print(
            f"\n  {len(open_disagreements)} case(s) now have an OPEN DISAGREEMENT and cannot gate a release:"
        )
        for case_id in open_disagreements[:10]:
            print(f"    {case_id}")
        print("  Resolve with: python -m synapse.cli.evalset adjudicate")
    return 0


def _cmd_adjudicate(args: argparse.Namespace) -> int:
    """Record an adjudication resolving a reviewer disagreement."""
    dataset = EvalDataset.load(args.dataset)
    case = dataset.by_id(args.case_id)
    if case is None:
        print(f"ERROR: no case '{args.case_id}' in this dataset", file=sys.stderr)
        return 1
    if case.disagreement_status is not DisagreementStatus.DISAGREED_OPEN:
        print(
            f"REFUSED: case '{args.case_id}' has no open disagreement (status: {case.disagreement_status.value})",
            file=sys.stderr,
        )
        return 1

    try:
        payload = json.loads(args.record.read_text(encoding="utf-8"))
        adjudication = Adjudication.model_validate(payload)
    except json.JSONDecodeError as exc:
        print(f"ERROR: adjudication file is not valid JSON: {exc.msg}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(
            f"ERROR: adjudication record is incomplete ({exc.error_count()} problem(s)). A rationale is mandatory.",
            file=sys.stderr,
        )
        return 2

    updated = case.model_copy(
        update={
            "adjudication": adjudication,
            "annotation_status": AnnotationStatus.ADJUDICATED,
            # The adjudicated resolution becomes the case's label; the model
            # enforces that these agree.
            "category": adjudication.resolved_category,
            "expected_behavior": adjudication.resolved_expected_behavior,
            "disagreement_status": DisagreementStatus.DISAGREED_RESOLVED,
            "updated_at": datetime.now(UTC),
        }
    )
    try:
        dataset.replace_case(EvalCase.model_validate(updated.model_dump()))
    except ValidationError as exc:
        print(
            f"REFUSED: adjudication produces an invalid case ({exc.error_count()} problem(s))",
            file=sys.stderr,
        )
        return 1

    dataset.write()
    print(
        f"Adjudicated {args.case_id}: {adjudication.resolved_category.value} / {adjudication.resolved_expected_behavior.value}"
    )
    print(f"  adjudicator: {adjudication.adjudicator_id}")
    return 0


def _cmd_agreement(args: argparse.Namespace) -> int:
    """Report inter-annotator agreement."""
    dataset = EvalDataset.load(args.dataset)
    report = compute_agreement(dataset.cases, dataset_version=dataset.manifest.version)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(report.render())
        if args.matrix and report.is_computable:
            print("\n  Confusion between reviewer labels (category):")
            for (left, right), count in disagreement_matrix(dataset.cases, "category").items():
                if left != right:
                    print(f"    {left} vs {right}: {count}")
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    """Assign cases to train/dev/test splits."""
    dataset = EvalDataset.load(args.dataset)
    plan = plan_splits(dataset.cases, stratify_by_category=not args.no_stratify)

    if args.dry_run:
        print("Split plan (dry run — nothing written)")
    else:
        dataset.cases = [
            case.model_copy(update={"split": plan.assignments.get(case.case_id, case.split)})
            for case in dataset.cases
        ]
        dataset.write()
        print("Split assignments written")

    for split, count in sorted(plan.counts().items()):
        print(f"  {split:12} : {count}")
    clusters = plan.multi_case_clusters()
    if clusters:
        # Stated explicitly because it is the mechanism preventing query
        # leakage, and a reader should see it worked.
        print(f"\n  {len(clusters)} near-duplicate cluster(s) kept together in one split:")
        for cluster in clusters[:5]:
            print(f"    {cluster}")

    leakage = detect_leakage(dataset.cases)
    if leakage.has_document_leakage:
        print(
            f"\n  NOTE: {len(leakage.document_leaks)} document(s) are graded relevant in more than one split."
        )
        print(
            "        Not fatal on a small corpus, but test scores are optimistic; see evals/DATASET_CARD.md."
        )
    return 0


def _cmd_gating(args: argparse.Namespace) -> int:
    """Report which cases may gate a release. This is the command CI runs."""
    dataset = EvalDataset.load(args.dataset)
    policy = GatingPolicy(
        minimum_reviews=dataset.manifest.minimum_reviews_for_gating,
        allow_single_review=args.allow_single_review,
        allow_unreviewed=args.allow_unreviewed,
    )
    report = evaluate_dataset(
        dataset.cases,
        dataset_version=dataset.manifest.version,
        policy=policy,
        corpus_version=dataset.manifest.corpus_version,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True) if args.json else report.render())

    if args.require_eligible and not report.eligible:
        # Lets CI fail a pipeline that expected a gating-capable dataset.
        print(
            "\nFAILED: --require-eligible was set but no case may gate a release", file=sys.stderr
        )
        return 1
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    """Compare two dataset versions, optionally appending a changelog entry."""
    previous = EvalDataset.load(args.from_dataset)
    current = EvalDataset.load(args.to_dataset)
    diff = diff_datasets(previous.manifest, previous.cases, current.manifest, current.cases)

    if args.json:
        print(json.dumps(diff.as_dict(), indent=2, sort_keys=True))
    else:
        print(diff.render())

    if args.changelog:
        entry = generate_changelog_entry(diff, note=args.note)
        changelog = current.directory / CHANGELOG_FILENAME
        existing = (
            changelog.read_text(encoding="utf-8")
            if changelog.is_file()
            else f"# Changelog — {current.manifest.dataset_id}\n"
        )
        changelog.write_text(existing + entry, encoding="utf-8")
        print(f"\nAppended changelog entry to {changelog}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Construct the parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m synapse.cli.evalset",
        description="Evaluation-dataset labelling workflows. Records decisions; never makes them.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Initialise a new evaluation dataset.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--version", default="0.1.0")
    p.add_argument(
        "--corpus-version", required=True, help="Corpus version cases will be labelled against."
    )
    p.add_argument("--protocol-version", default="protocol-1")
    p.add_argument(
        "--minimum-reviews",
        type=int,
        default=2,
        help="Independent reviews a case needs before it may gate a release.",
    )
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("add-case", help="Add an authored case from a JSON file.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--case", type=Path, required=True)
    p.set_defaults(func=_cmd_add_case)

    p = sub.add_parser("validate", help="Validate a dataset against every rule.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Corpus artifact directory, enabling referential-integrity checks.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("export-sheet", help="Export a CSV review sheet.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--reviewer-id", default=None, help="Pre-fill a reviewer pseudonym issued out-of-band."
    )
    p.set_defaults(func=_cmd_export_sheet)

    p = sub.add_parser("import-sheet", help="File reviews from a completed CSV sheet.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--sheet", type=Path, required=True)
    p.add_argument("--reviewer-role", default="clinician")
    p.set_defaults(func=_cmd_import_sheet)

    p = sub.add_parser("adjudicate", help="Resolve a reviewer disagreement.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--case-id", required=True)
    p.add_argument("--record", type=Path, required=True, help="Completed adjudication record.")
    p.set_defaults(func=_cmd_adjudicate)

    p = sub.add_parser("agreement", help="Report inter-annotator agreement.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--matrix", action="store_true", help="Also show which labels get confused.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_agreement)

    p = sub.add_parser("split", help="Assign cases to train/dev/test splits.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--no-stratify", action="store_true", help="Do not stratify by category.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_split)

    p = sub.add_parser("gating", help="Report which cases may gate a release.")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument(
        "--allow-single-review",
        action="store_true",
        help="BOOTSTRAPPING ONLY. Recorded in the report.",
    )
    p.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help="BOOTSTRAPPING ONLY. Never for a release gate.",
    )
    p.add_argument(
        "--require-eligible",
        action="store_true",
        help="Exit non-zero when no case may gate a release.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_gating)

    p = sub.add_parser("diff", help="Compare two dataset versions.")
    p.add_argument("--from", dest="from_dataset", type=Path, required=True)
    p.add_argument("--to", dest="to_dataset", type=Path, required=True)
    p.add_argument(
        "--changelog", action="store_true", help="Append a changelog entry to the target dataset."
    )
    p.add_argument("--note", default="", help="Note for the changelog entry.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    configure_logging(
        "WARNING"
    )  # Quieter than the other CLIs: labelling output is the product here, not progress logging
    print(
        LABELLING_NOTICE, file=sys.stderr
    )  # stderr, so stdout stays usable for machine-readable output
    try:
        return int(args.func(args))
    except (
        SynapseArtifactError
    ) as exc:  # Typed, user-safe failures print cleanly; anything else keeps its traceback
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # Enables `python -m synapse.cli.evalset`
    raise SystemExit(main())

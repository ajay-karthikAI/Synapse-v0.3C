"""
synapse.evalset.dataset
=======================
Loading, writing and validating an evaluation dataset on disk.

Layout::

    evals/<dataset-id>/
    ├── manifest.json     version, counts, gating policy, provenance
    ├── cases.jsonl       one EvalCase per line — the reviewable artifact
    └── CHANGELOG.md      version history

``validate()`` returns every finding rather than raising on the first, because
an operator fixing a dataset wants the whole list. Findings at ``error``
severity block a version from being cut; ``warning`` findings are recorded and
do not block.
"""

from __future__ import annotations  # Postponed annotations

import json  # Manifest serialisation; never eval, never a YAML loader
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from synapse.corpus.jsonl import read_jsonl, records_digest, write_jsonl
from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError
from synapse.evalset.gating import GatingPolicy, evaluate_dataset
from synapse.evalset.similarity import DEFAULT_NEAR_DUPLICATE_THRESHOLD, find_duplicate_pairs
from synapse.evalset.splits import detect_leakage
from synapse.logging import get_logger
from synapse.schemas.enums import AnnotationStatus, DatasetSplit, EvalCategory
from synapse.schemas.evalset import EvalCase, EvalDatasetManifest

logger = get_logger(__name__)

MANIFEST_FILENAME = (
    "manifest.json"  # Fixed names, so a caller only ever names the dataset directory
)
CASES_FILENAME = "cases.jsonl"  # Uncompressed: a dataset is meant to be read and diffed in review
CHANGELOG_FILENAME = "CHANGELOG.md"

SEVERITY_ERROR = "error"  # Blocks a version from being cut
SEVERITY_WARNING = "warning"  # Recorded, does not block

PROVENANCE_NOTES = (  # Emitted into every manifest so counts are never read as a quality claim
    "Cases marked 'synthetic' are engineering-authored illustrations. They are unreviewed "
    "and are excluded from release-gating metrics by construction.",
    "A case may gate a release only after its review requirements are satisfied; see "
    "synapse.evalset.gating and docs/clinical-labeling-protocol.md.",
    "Relevance labels are pinned to a corpus version and are not portable across versions.",
)


@dataclass(frozen=True)
class DatasetIssue:
    """One validation finding."""

    severity: str
    code: str
    message: str
    case_id: str | None = None

    def render(self) -> str:
        """One-line rendering for console output."""
        location = f" [{self.case_id}]" if self.case_id else ""
        return f"{self.severity.upper():7} {self.code}{location}: {self.message}"


@dataclass
class DatasetValidationReport:
    """Outcome of validating a dataset."""

    dataset_id: str
    version: str
    issues: list[DatasetIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[DatasetIssue]:
        """Findings that block a version from being cut."""
        return [issue for issue in self.issues if issue.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[DatasetIssue]:
        """Findings recorded but not blocking."""
        return [issue for issue in self.issues if issue.severity == SEVERITY_WARNING]

    @property
    def is_valid(self) -> bool:
        """True when nothing blocks."""
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for CI."""
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "valid": self.is_valid,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [
                {"severity": i.severity, "code": i.code, "message": i.message, "case_id": i.case_id}
                for i in self.issues
            ],
        }


@dataclass
class EvalDataset:
    """An evaluation dataset loaded from disk."""

    manifest: EvalDatasetManifest
    cases: list[EvalCase]
    directory: Path

    # ---------------------------------------------------------------- I/O --

    @classmethod
    def load(cls, directory: Path) -> EvalDataset:
        """Read and validate the structure of a dataset directory."""
        manifest_path = directory / MANIFEST_FILENAME
        cases_path = directory / CASES_FILENAME
        if not manifest_path.is_file():
            raise ArtifactNotFoundError(path=manifest_path, role="manifest")
        if not cases_path.is_file():
            raise ArtifactNotFoundError(path=cases_path, role="cases")

        try:
            payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )  # SAFE: json.loads cannot execute code
        except json.JSONDecodeError as exc:
            raise ArtifactSchemaError(
                path=manifest_path, problem="manifest is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ArtifactSchemaError(path=manifest_path, problem="manifest is not a JSON object")
        try:
            manifest = EvalDatasetManifest.model_validate(payload)
        except ValidationError as exc:
            raise ArtifactSchemaError(
                path=manifest_path,
                problem="manifest failed schema validation",
                error_count=exc.error_count(),
            ) from exc

        cases = list(read_jsonl(cases_path, EvalCase))  # Each case validated on read
        return cls(manifest=manifest, cases=cases, directory=directory)

    def write(self, *, policy: GatingPolicy | None = None) -> None:
        """Write the manifest and cases, refreshing every derived field.

        Counts and digests are always recomputed rather than trusted from the
        caller, so a manifest can never disagree with the cases file beside it.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        write_jsonl(self.directory / CASES_FILENAME, self.cases)  # JSONL, never pickle

        gating = evaluate_dataset(
            self.cases, dataset_version=self.manifest.version, policy=policy or GatingPolicy()
        )
        refreshed = self.manifest.model_copy(
            update={
                "cases_sha256": self.compute_digest(),
                "case_count": len(self.cases),
                "category_counts": self.category_counts(),
                "status_counts": self.status_counts(),
                "split_counts": self.split_counts(),
                "gating_eligible_count": len(gating.eligible),
                "provenance_notes": list(PROVENANCE_NOTES),
            }
        )
        self.manifest = refreshed
        (self.directory / MANIFEST_FILENAME).write_text(
            json.dumps(
                refreshed.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False
            )
            + "\n",  # Sorted keys so a diff is readable in review
            encoding="utf-8",
        )

    # ------------------------------------------------------------ derived --

    def compute_digest(self) -> str:
        """Order-sensitive digest over the serialised cases."""
        return records_digest(self.cases)

    def category_counts(self) -> dict[str, int]:
        """Cases per category, with every category present even at zero."""
        counts = {category.value: 0 for category in EvalCategory}
        for case in self.cases:
            counts[case.category.value] += 1
        return counts

    def status_counts(self) -> dict[str, int]:
        """Cases per annotation status, with every status present."""
        counts = {status.value: 0 for status in AnnotationStatus}
        for case in self.cases:
            counts[case.annotation_status.value] += 1
        return counts

    def split_counts(self) -> dict[str, int]:
        """Cases per split, with every split present."""
        counts = {split.value: 0 for split in DatasetSplit}
        for case in self.cases:
            counts[case.split.value] += 1
        return counts

    def by_id(self, case_id: str) -> EvalCase | None:
        """Look up one case by identifier."""
        for case in self.cases:
            if case.case_id == case_id:
                return case
        return None

    def replace_case(self, updated: EvalCase) -> None:
        """Replace a case in place, preserving its position.

        Position matters because the digest is order-sensitive: re-ordering on
        every edit would churn the digest and force spurious version bumps.
        """
        for index, existing in enumerate(self.cases):
            if existing.case_id == updated.case_id:
                self.cases[index] = updated
                return
        raise ArtifactSchemaError(problem="case not found in dataset", case_id=updated.case_id)

    # ---------------------------------------------------------- validation --

    def validate(
        self,
        *,
        corpus_document_ids: set[str] | None = None,
        near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    ) -> DatasetValidationReport:
        """Check every dataset-level rule, returning all findings.

        ``corpus_document_ids`` enables referential-integrity checking. When
        omitted the check is skipped and a warning records that it was — the
        alternative would be silently reporting a clean dataset that references
        documents which do not exist.
        """
        report = DatasetValidationReport(
            dataset_id=self.manifest.dataset_id, version=self.manifest.version
        )
        add = report.issues.append

        # -- integrity ------------------------------------------------------
        if self.compute_digest() != self.manifest.cases_sha256:
            add(
                DatasetIssue(
                    SEVERITY_ERROR,
                    "digest_mismatch",
                    "cases.jsonl does not match manifest.cases_sha256; the dataset changed without being rewritten",
                )
            )
        if self.manifest.case_count != len(self.cases):
            add(
                DatasetIssue(
                    SEVERITY_ERROR,
                    "case_count_mismatch",
                    f"manifest declares {self.manifest.case_count} cases, file contains {len(self.cases)}",
                )
            )

        # -- uniqueness -----------------------------------------------------
        seen: set[str] = set()
        for case in self.cases:
            if case.case_id in seen:  # A duplicate identifier makes every per-case report ambiguous
                add(
                    DatasetIssue(
                        SEVERITY_ERROR,
                        "duplicate_case_id",
                        "case_id appears more than once",
                        case.case_id,
                    )
                )
            seen.add(case.case_id)

        # -- duplicate and near-duplicate queries ---------------------------
        usable = [case for case in self.cases if not case.excluded]
        for pair in find_duplicate_pairs(
            [(c.case_id, c.query) for c in usable], threshold=near_duplicate_threshold
        ):
            if pair.exact:
                # An exact duplicate double-counts one question in every metric.
                add(
                    DatasetIssue(
                        SEVERITY_ERROR,
                        "duplicate_query",
                        f"query is identical to {pair.case_id_b} after normalisation",
                        pair.case_id_a,
                    )
                )
            else:
                add(
                    DatasetIssue(
                        SEVERITY_WARNING,
                        "near_duplicate_query",
                        f"query is {pair.similarity:.0%} similar to {pair.case_id_b}; they will share a split",
                        pair.case_id_a,
                    )
                )

        # -- cross-split leakage --------------------------------------------
        leakage = detect_leakage(self.cases, threshold=near_duplicate_threshold)
        for left, right, similarity in leakage.query_leaks:
            add(
                DatasetIssue(
                    SEVERITY_ERROR,
                    "query_leakage_across_splits",
                    f"near-duplicate of {right} ({similarity:.0%}) sits in a different split",
                    left,
                )
            )
        for leak in leakage.document_leaks:
            # Reported as a warning: a small corpus cannot always avoid it, and
            # discarding usable cases would be the worse trade. What matters is
            # that the number is visible when a score is read.
            add(
                DatasetIssue(
                    SEVERITY_WARNING,
                    "document_leakage_across_splits",
                    f"document {leak.document_id} is graded relevant in splits {', '.join(leak.splits)}",
                )
            )

        # -- referential integrity -------------------------------------------
        if corpus_document_ids is None:
            add(
                DatasetIssue(
                    SEVERITY_WARNING,
                    "corpus_not_checked",
                    "no corpus supplied, so relevance labels were not checked against real documents",
                )
            )
        else:
            for case in self.cases:
                missing = sorted(case.relevant_document_ids - corpus_document_ids)
                if missing:
                    # THE check the shipped evaluation set would have failed:
                    # all four of its ground-truth PMIDs are absent from the corpus.
                    add(
                        DatasetIssue(
                            SEVERITY_ERROR,
                            "relevance_label_missing_document",
                            f"{len(missing)} graded document(s) absent from the corpus: {', '.join(missing[:3])}",
                            case.case_id,
                        )
                    )

        # -- per-case coherence ----------------------------------------------
        for case in self.cases:
            if case.dataset_version != self.manifest.version:
                add(
                    DatasetIssue(
                        SEVERITY_ERROR,
                        "case_version_mismatch",
                        f"case declares dataset_version {case.dataset_version}, manifest is {self.manifest.version}",
                        case.case_id,
                    )
                )
            if case.corpus_version != self.manifest.corpus_version:
                add(
                    DatasetIssue(
                        SEVERITY_ERROR,
                        "case_corpus_mismatch",
                        f"case labelled against {case.corpus_version}, dataset targets {self.manifest.corpus_version}",
                        case.case_id,
                    )
                )
            if (
                not case.is_synthetic
                and case.annotation_status is AnnotationStatus.PENDING_REVIEW
                and not case.reviews
            ):
                add(
                    DatasetIssue(
                        SEVERITY_WARNING,
                        "awaiting_review",
                        "case has no reviews and cannot gate a release",
                        case.case_id,
                    )
                )
            if not case.known_limitations.strip() and not case.is_synthetic:
                # A case with no stated limitations usually means nobody looked
                # for them, so it is surfaced rather than assumed to have none.
                add(
                    DatasetIssue(
                        SEVERITY_WARNING,
                        "no_stated_limitations",
                        "case records no known limitations",
                        case.case_id,
                    )
                )

        # -- near-duplicate clusters that disagree on labels -----------------
        self._validate_cluster_label_consistency(usable, report, near_duplicate_threshold)

        logger.info(
            "dataset validated",
            extra={
                "dataset_id": self.manifest.dataset_id,
                "errors": len(report.errors),
                "warnings": len(report.warnings),
            },
        )
        return report

    def _validate_cluster_label_consistency(
        self,
        usable: list[EvalCase],
        report: DatasetValidationReport,
        threshold: float,
    ) -> None:
        """Flag near-duplicate queries that carry different expected behaviours.

        Two queries similar enough to share a split but labelled to expect
        different behaviour is either a labelling error or evidence that the
        similarity threshold is too loose. Either way a human should look.
        """
        from synapse.evalset.similarity import cluster_by_similarity

        by_id = {case.case_id: case for case in usable}
        for cluster in cluster_by_similarity(
            [(c.case_id, c.query) for c in usable], threshold=threshold
        ):
            if len(cluster) < 2:
                continue
            behaviours = {by_id[case_id].expected_behavior for case_id in cluster}
            if len(behaviours) > 1:
                report.issues.append(
                    DatasetIssue(
                        SEVERITY_WARNING,
                        "near_duplicate_label_conflict",
                        f"near-duplicate cluster {cluster} expects differing behaviours: {sorted(b.value for b in behaviours)}",
                    )
                )


def new_manifest(
    *,
    dataset_id: str,
    version: str,
    corpus_version: str,
    protocol_version: str,
    generator: str,
    minimum_reviews_for_gating: int = 2,
    now: datetime | None = None,
) -> EvalDatasetManifest:
    """Build an empty manifest for a new dataset version."""
    timestamp = now or datetime.now(UTC)
    return EvalDatasetManifest(
        dataset_id=dataset_id,
        version=version,
        corpus_version=corpus_version,
        created_at=timestamp,
        case_count=0,
        cases_sha256="0" * 64,  # Placeholder; write() recomputes it from the (empty) cases file
        minimum_reviews_for_gating=minimum_reviews_for_gating,
        protocol_version=protocol_version,
        generator=generator,
        provenance_notes=list(PROVENANCE_NOTES),
    )


__all__ = [
    "CASES_FILENAME",
    "CHANGELOG_FILENAME",
    "MANIFEST_FILENAME",
    "PROVENANCE_NOTES",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "DatasetIssue",
    "DatasetValidationReport",
    "EvalDataset",
    "new_manifest",
]

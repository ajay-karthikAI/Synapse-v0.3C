"""
synapse.governance.pack
=======================
Loading, writing and validating a source pack on disk.

Layout::

    source_packs/<pack-id>/
    ├── manifest.json     governance record: scope, policy, approval, integrity
    ├── sources.jsonl     one PackSource per line, the reviewable artifact
    ├── CHANGELOG.md      version history, human-maintained and tool-appended
    ├── README.md         what the pack is for, in prose
    └── .versions/        snapshots of previous versions, written by `version`

``validate()`` is the enforcement point. It returns a report of every violation
rather than raising on the first, because an operator fixing a pack wants the
whole list, not a one-at-a-time drip. Anything with severity ``error`` blocks
approval and blocks index construction.
"""

from __future__ import annotations  # Postponed annotations

import json  # Manifest serialisation; never eval, never a YAML loader
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from synapse.corpus.jsonl import read_jsonl, records_digest, write_jsonl
from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError
from synapse.governance.states import GovernanceError
from synapse.logging import get_logger
from synapse.schemas.enums import (
    EvidenceType,
    PackApprovalState,
    RetractionStatus,
    SourceLifecycleState,
)
from synapse.schemas.source_pack import PackSource, SourcePackManifest

logger = get_logger(__name__)

MANIFEST_FILENAME = "manifest.json"  # Fixed names, so a caller only ever names the pack directory
SOURCES_FILENAME = (
    "sources.jsonl"  # Uncompressed: a source pack is meant to be read and diffed in review
)
CHANGELOG_FILENAME = "CHANGELOG.md"
README_FILENAME = "README.md"
VERSIONS_DIRNAME = (
    ".versions"  # Snapshots of previous versions, so `diff` has something to compare against
)

SEVERITY_ERROR = "error"  # Blocks approval and index construction
SEVERITY_WARNING = "warning"  # Recorded, does not block


@dataclass(frozen=True)
class ValidationIssue:
    """One validation finding."""

    severity: str  # SEVERITY_ERROR or SEVERITY_WARNING
    code: str  # Machine-readable rule identifier
    message: str  # Human-readable explanation
    document_id: str | None = None  # The source at fault, when the issue is source-scoped

    def render(self) -> str:
        """One-line rendering for console output."""
        location = f" [{self.document_id}]" if self.document_id else ""
        return f"{self.severity.upper():7} {self.code}{location}: {self.message}"


@dataclass
class ValidationReport:
    """The outcome of validating a pack."""

    pack_id: str
    version: str
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Issues that block approval and index construction."""
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Issues recorded but not blocking."""
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    @property
    def is_valid(self) -> bool:
        """True when nothing blocks."""
        return not self.errors

    def as_dict(self) -> dict:
        """Machine-readable form, for CI and for the build report."""
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "valid": self.is_valid,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [
                {
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "document_id": i.document_id,
                }
                for i in self.issues
            ],
        }


@dataclass
class SourcePack:
    """A source pack loaded from disk."""

    manifest: SourcePackManifest
    sources: list[PackSource]
    directory: Path

    # ---------------------------------------------------------------- I/O --

    @classmethod
    def load(cls, directory: Path) -> SourcePack:
        """Read and validate the structure of a pack directory.

        Raises:
            ArtifactNotFoundError: manifest or sources file is missing.
            ArtifactSchemaError: either file is malformed.
            ArtifactVersionError: the manifest is schema-incompatible.
        """
        manifest_path = directory / MANIFEST_FILENAME
        sources_path = directory / SOURCES_FILENAME
        if not manifest_path.is_file():
            raise ArtifactNotFoundError(path=manifest_path, role="manifest")
        if not sources_path.is_file():
            raise ArtifactNotFoundError(path=sources_path, role="sources")

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
            manifest = SourcePackManifest.model_validate(
                payload
            )  # Full validation, including the approval invariants
        except ValidationError as exc:
            raise ArtifactSchemaError(
                path=manifest_path,
                problem="manifest failed schema validation",
                error_count=exc.error_count(),
            ) from exc

        sources = list(read_jsonl(sources_path, PackSource))  # Each record validated on read
        return cls(manifest=manifest, sources=sources, directory=directory)

    def write(self) -> None:
        """Write the manifest and sources back to disk, refreshing derived fields.

        ``corpus_sha256``, ``source_count`` and ``lifecycle_counts`` are always
        recomputed here rather than trusted from the caller, so a manifest can
        never disagree with the sources file it sits beside.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        write_jsonl(self.directory / SOURCES_FILENAME, self.sources)  # JSONL, never pickle
        refreshed = self.manifest.model_copy(
            update={
                "corpus_sha256": self.compute_corpus_digest(),
                "source_count": len(self.sources),
                "lifecycle_counts": self.lifecycle_counts(),
                "updated_at": datetime.now(tz=self.manifest.created_at.tzinfo),
            }
        )
        self.manifest = refreshed
        (self.directory / MANIFEST_FILENAME).write_text(
            json.dumps(
                refreshed.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False
            )
            + "\n",  # Sorted keys so a manifest diff is readable in review
            encoding="utf-8",
        )

    # ------------------------------------------------------------ derived --

    def compute_corpus_digest(self) -> str:
        """Order-sensitive digest over the serialised sources.

        This is what makes requirement 5's version rule enforceable: any change
        to any source — content, metadata or lifecycle state — changes the
        digest, so a changed pack that kept its version number fails validation.
        """
        return records_digest(self.sources)

    def lifecycle_counts(self) -> dict[str, int]:
        """Count sources per lifecycle state.

        Every state appears, including those with zero sources, so a reader can
        see at a glance that nothing is approved rather than having to notice
        an absent key.
        """
        counts = {state.value: 0 for state in SourceLifecycleState}
        for source in self.sources:
            counts[source.lifecycle_state.value] += 1
        return counts

    def by_id(self, document_id: str) -> PackSource | None:
        """Look up one source by document identifier."""
        for source in self.sources:
            if source.document_id == document_id:
                return source
        return None

    def replace_source(self, updated: PackSource) -> None:
        """Replace a source in place, preserving its position.

        Position is preserved because the corpus digest is order-sensitive:
        re-ordering on every edit would make every review churn the digest and
        force a spurious version bump.
        """
        for index, existing in enumerate(self.sources):
            if existing.document_id == updated.document_id:
                self.sources[index] = updated
                return
        raise GovernanceError(problem="source not found in pack", document_id=updated.document_id)

    # ---------------------------------------------------------- validation --

    def validate(self, *, as_of: date | None = None) -> ValidationReport:
        """Check every governance rule, returning all findings.

        ``as_of`` drives expiry checks and defaults to today. It is injectable
        so tests can assert expiry behaviour without waiting a year.
        """
        today = as_of or datetime.now(tz=self.manifest.created_at.tzinfo).date()
        report = ValidationReport(pack_id=self.manifest.pack_id, version=self.manifest.version)
        add = report.issues.append

        # -- integrity: contents must match the manifest --------------------
        actual_digest = self.compute_corpus_digest()
        if actual_digest != self.manifest.corpus_sha256:
            # Requirement 5: contents changed without a version bump. This is an
            # error rather than an auto-repair, because silently rewriting the
            # digest is exactly how a reviewed pack drifts from what was reviewed.
            add(
                ValidationIssue(
                    SEVERITY_ERROR,
                    "corpus_digest_mismatch",
                    "sources.jsonl does not match manifest.corpus_sha256; the pack changed without a version bump",
                )
            )
        if self.manifest.source_count != len(self.sources):
            add(
                ValidationIssue(
                    SEVERITY_ERROR,
                    "source_count_mismatch",
                    f"manifest declares {self.manifest.source_count} sources, file contains {len(self.sources)}",
                )
            )
        if (
            self.manifest.lifecycle_counts
            and self.manifest.lifecycle_counts != self.lifecycle_counts()
        ):
            add(
                ValidationIssue(
                    SEVERITY_ERROR,
                    "lifecycle_counts_mismatch",
                    "manifest lifecycle_counts do not match the sources file",
                )
            )

        # -- uniqueness -----------------------------------------------------
        seen: set[str] = set()
        for source in self.sources:
            if (
                source.document_id in seen
            ):  # A duplicate makes review ambiguous: which entry did the reviewer approve?
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "duplicate_document_id",
                        "document_id appears more than once in the pack",
                        source.document_id,
                    )
                )
            seen.add(source.document_id)

        # -- per-source governance ------------------------------------------
        for source in self.sources:
            self._validate_source(source, report, today)

        # -- pack-level approval --------------------------------------------
        self._validate_pack_approval(report, today)

        # -- example packs ---------------------------------------------------
        if self.manifest.is_example:
            approved = [s for s in self.sources if s.is_approved]
            if approved:
                # A demonstration pack containing approvals would be the exact
                # confusion this state exists to prevent.
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "example_pack_has_approvals",
                        f"an unapproved_example pack must contain no approved sources; found {len(approved)}",
                    )
                )

        logger.info(
            "pack validated",
            extra={
                "pack_id": self.manifest.pack_id,
                "errors": len(report.errors),
                "warnings": len(report.warnings),
            },
        )
        return report

    def _validate_source(self, source: PackSource, report: ValidationReport, today: date) -> None:
        """Validate one source against the pack's policy."""
        add = report.issues.append
        policy = self.manifest.selection_policy
        requirements = self.manifest.reviewer_requirements

        if source.is_approved:
            review = source.review
            if (
                review is None
            ):  # Defence in depth: the model forbids this, and so does the pack validator
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "approved_without_review",
                        "approved source carries no review record",
                        source.document_id,
                    )
                )
                return
            if review.reviewer_role is not requirements.required_role:
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "reviewer_role_insufficient",
                        f"pack requires a '{requirements.required_role.value}' reviewer, review is by '{review.reviewer_role.value}'",
                        source.document_id,
                    )
                )
            if review.rubric_version != requirements.rubric_version:
                # A review conducted under a different rubric may not have
                # assessed what the current rubric requires.
                add(
                    ValidationIssue(
                        SEVERITY_WARNING,
                        "rubric_version_mismatch",
                        f"review used rubric '{review.rubric_version}', pack requires '{requirements.rubric_version}'",
                        source.document_id,
                    )
                )
            if source.review_expired_on(today):
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "approval_expired",
                        f"approval expired on {review.review_due_at.isoformat()}; the source must be re-reviewed or moved to 'expired'",
                        source.document_id,
                    )
                )
            if (
                policy.minimum_evidence_types
                and source.evidence_type not in policy.minimum_evidence_types
            ):
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "evidence_type_below_policy",
                        f"evidence_type '{source.evidence_type.value}' is not among the pack's minimum evidence requirements",
                        source.document_id,
                    )
                )
            if policy.exclude_retracted and source.retraction_status in (
                RetractionStatus.RETRACTED,
                RetractionStatus.EXPRESSION_OF_CONCERN,
            ):
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "approved_source_retracted",
                        f"approved source has retraction_status '{source.retraction_status.value}'",
                        source.document_id,
                    )
                )
            if policy.exclude_preprints and source.evidence_type is EvidenceType.PREPRINT:
                add(
                    ValidationIssue(
                        SEVERITY_ERROR,
                        "approved_source_is_preprint",
                        "pack policy excludes preprints",
                        source.document_id,
                    )
                )
            if (
                policy.required_languages
                and source.language
                and source.language not in policy.required_languages
            ):
                add(
                    ValidationIssue(
                        SEVERITY_WARNING,
                        "language_outside_policy",
                        f"language '{source.language}' is not among the pack's required languages",
                        source.document_id,
                    )
                )
            if policy.maximum_age_years is not None and source.publication_date is not None:
                age_years = (today - source.publication_date).days / 365.25
                if age_years > policy.maximum_age_years:
                    add(
                        ValidationIssue(
                            SEVERITY_WARNING,
                            "source_older_than_policy",
                            f"source is ~{age_years:.0f} years old; policy maximum is {policy.maximum_age_years}",
                            source.document_id,
                        )
                    )

        if (
            source.retraction_status is RetractionStatus.UNCHECKED
            and source.lifecycle_state is not SourceLifecycleState.DISCOVERED
        ):
            # Screening should happen before a human spends time reviewing.
            add(
                ValidationIssue(
                    SEVERITY_WARNING,
                    "retraction_unchecked",
                    "source advanced past 'discovered' without retraction screening",
                    source.document_id,
                )
            )

    def _validate_pack_approval(self, report: ValidationReport, today: date) -> None:
        """Validate the pack-level approval claim."""
        add = report.issues.append
        manifest = self.manifest

        if manifest.approval_state is not PackApprovalState.APPROVED:
            return  # Nothing to check: only an approval claim carries obligations

        eligible = [s for s in self.sources if s.is_approved]
        if not eligible:
            add(
                ValidationIssue(
                    SEVERITY_ERROR,
                    "approved_pack_has_no_approved_sources",
                    "pack claims approval but contains no approved sources",
                )
            )
        unresolved = [
            s
            for s in self.sources
            if s.lifecycle_state in (SourceLifecycleState.DISCOVERED, SourceLifecycleState.SCREENED)
        ]
        if unresolved:
            # An approved pack with pending sources is ambiguous: a reader
            # cannot tell whether those sources were considered and deferred,
            # or simply forgotten.
            add(
                ValidationIssue(
                    SEVERITY_ERROR,
                    "approved_pack_has_unresolved_sources",
                    f"{len(unresolved)} source(s) are still 'discovered' or 'screened'; resolve or remove them before approving",
                )
            )
        if manifest.review_due_at is not None and manifest.review_due_at < today:
            add(
                ValidationIssue(
                    SEVERITY_ERROR,
                    "pack_approval_expired",
                    f"pack approval expired on {manifest.review_due_at.isoformat()}",
                )
            )


__all__ = [
    "CHANGELOG_FILENAME",
    "MANIFEST_FILENAME",
    "README_FILENAME",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SOURCES_FILENAME",
    "VERSIONS_DIRNAME",
    "SourcePack",
    "ValidationIssue",
    "ValidationReport",
]

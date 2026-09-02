"""
synapse.evalset.compare
=======================
Dataset version comparison and changelog generation.

Organised by *evaluation consequence* rather than by field, because the
question a reader has before adopting a new dataset version is not "what
changed in the files" but "can I still compare scores across this boundary".

Three changes make scores incomparable, and each is surfaced separately:

* a case moved between splits — a test score computed before and after is
  measuring different things;
* a label changed — the same answer now scores differently;
* the gating-eligible set changed — the release gate itself moved.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from synapse.schemas.evalset import EvalCase, EvalDatasetManifest


@dataclass(frozen=True)
class CaseChange:
    """One case that changed between versions."""

    case_id: str
    change_type: (
        str  # added | removed | label_changed | status_changed | split_changed | text_changed
    )
    detail: str
    previous: str | None = None
    current: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form."""
        return {
            "case_id": self.case_id,
            "change_type": self.change_type,
            "detail": self.detail,
            "previous": self.previous,
            "current": self.current,
        }


@dataclass
class DatasetDiff:
    """The evaluation-relevant difference between two dataset versions."""

    previous_version: str
    current_version: str
    corpus_version_changed: bool = False
    protocol_version_changed: bool = False
    changes: list[CaseChange] = field(default_factory=list)

    def of_type(self, change_type: str) -> list[CaseChange]:
        """Changes of one kind."""
        return [change for change in self.changes if change.change_type == change_type]

    @property
    def scores_are_comparable(self) -> bool:
        """True when scores from the two versions may be compared directly.

        Conservative by design. Any label change, split move, or corpus-version
        change makes a like-for-like comparison invalid, and reporting that
        plainly is more useful than a caveat nobody reads.
        """
        breaking = {"added", "removed", "label_changed", "split_changed"}
        return not self.corpus_version_changed and not any(
            c.change_type in breaking for c in self.changes
        )

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for CI."""
        kinds = (
            "added",
            "removed",
            "label_changed",
            "status_changed",
            "split_changed",
            "text_changed",
        )
        return {
            "previous_version": self.previous_version,
            "current_version": self.current_version,
            "corpus_version_changed": self.corpus_version_changed,
            "protocol_version_changed": self.protocol_version_changed,
            "scores_are_comparable": self.scores_are_comparable,
            "counts": {kind: len(self.of_type(kind)) for kind in kinds},
            "changes": [change.as_dict() for change in self.changes],
        }

    def render(self) -> str:
        """Console rendering, leading with comparability."""
        lines = [
            f"{self.previous_version} -> {self.current_version}",
            f"  scores comparable      : {self.scores_are_comparable}",
            f"  corpus version changed : {self.corpus_version_changed}",
            f"  protocol changed       : {self.protocol_version_changed}",
        ]
        for kind in (
            "added",
            "removed",
            "label_changed",
            "status_changed",
            "split_changed",
            "text_changed",
        ):
            lines.append(f"  {kind:<22} : {len(self.of_type(kind))}")
        return "\n".join(lines)


def diff_datasets(
    previous_manifest: EvalDatasetManifest,
    previous_cases: list[EvalCase],
    current_manifest: EvalDatasetManifest,
    current_cases: list[EvalCase],
) -> DatasetDiff:
    """Compare two dataset versions."""
    previous_by_id = {case.case_id: case for case in previous_cases}
    current_by_id = {case.case_id: case for case in current_cases}

    diff = DatasetDiff(
        previous_version=previous_manifest.version,
        current_version=current_manifest.version,
        corpus_version_changed=previous_manifest.corpus_version != current_manifest.corpus_version,
        protocol_version_changed=previous_manifest.protocol_version
        != current_manifest.protocol_version,
    )

    for case_id in sorted(set(current_by_id) - set(previous_by_id)):
        diff.changes.append(
            CaseChange(case_id, "added", "new case", None, current_by_id[case_id].category.value)
        )
    for case_id in sorted(set(previous_by_id) - set(current_by_id)):
        diff.changes.append(
            CaseChange(
                case_id, "removed", "case removed", previous_by_id[case_id].category.value, None
            )
        )

    for case_id in sorted(set(previous_by_id) & set(current_by_id)):
        earlier, current = previous_by_id[case_id], current_by_id[case_id]
        # Label changes first: they are the ones that alter what a score means.
        if earlier.category is not current.category:
            diff.changes.append(
                CaseChange(
                    case_id,
                    "label_changed",
                    "category changed",
                    earlier.category.value,
                    current.category.value,
                )
            )
        if earlier.expected_behavior is not current.expected_behavior:
            diff.changes.append(
                CaseChange(
                    case_id,
                    "label_changed",
                    "expected_behavior changed",
                    earlier.expected_behavior.value,
                    current.expected_behavior.value,
                )
            )
        if earlier.relevant_documents != current.relevant_documents:
            diff.changes.append(
                CaseChange(
                    case_id,
                    "label_changed",
                    "relevance grades changed",
                    str(len(earlier.relevant_documents)),
                    str(len(current.relevant_documents)),
                )
            )
        if earlier.split is not current.split:
            diff.changes.append(
                CaseChange(
                    case_id,
                    "split_changed",
                    "split reassigned",
                    earlier.split.value,
                    current.split.value,
                )
            )
        if earlier.annotation_status is not current.annotation_status:
            # Status changes do not invalidate comparison — they usually mean a
            # case became gating-eligible, which is progress, not breakage.
            diff.changes.append(
                CaseChange(
                    case_id,
                    "status_changed",
                    "annotation status changed",
                    earlier.annotation_status.value,
                    current.annotation_status.value,
                )
            )
        if earlier.normalized_query != current.normalized_query:
            diff.changes.append(
                CaseChange(case_id, "text_changed", "query text changed", None, None)
            )

    return diff


def generate_changelog_entry(
    diff: DatasetDiff, *, note: str = "", now: datetime | None = None
) -> str:
    """Render a Markdown changelog entry for a version transition."""
    timestamp = (now or datetime.now(UTC)).date().isoformat()
    lines = [f"\n## {diff.current_version} — {timestamp}\n"]
    if note:
        lines.append(f"{note}\n")

    counts = {
        "Cases added": len(diff.of_type("added")),
        "Cases removed": len(diff.of_type("removed")),
        "Labels changed": len(diff.of_type("label_changed")),
        "Annotation statuses changed": len(diff.of_type("status_changed")),
        "Split assignments changed": len(diff.of_type("split_changed")),
        "Query text edited": len(diff.of_type("text_changed")),
    }
    lines.extend(f"- {label}: {count}" for label, count in counts.items() if count)

    if diff.corpus_version_changed:
        lines.append(
            "- **Corpus version changed.** Relevance labels are pinned to a corpus version; re-verify every graded document."
        )
    if diff.protocol_version_changed:
        lines.append(
            "- **Labelling protocol changed.** Reviews recorded under the previous protocol were made against different instructions."
        )
    if not diff.scores_are_comparable:
        lines.append(
            f"- **Scores are NOT comparable with {diff.previous_version}.** Re-baseline before using this version to gate a release."
        )

    # Name the individual label changes: they are the ones a reader will want
    # to inspect by hand, and there are rarely many.
    label_changes = diff.of_type("label_changed")
    if label_changes:
        lines.append("\n### Label changes\n")
        lines.extend(
            f"- `{c.case_id}`: {c.detail} ({c.previous} -> {c.current})" for c in label_changes[:50]
        )
        if len(label_changes) > 50:
            lines.append(f"- …and {len(label_changes) - 50} more.")

    return "\n".join(lines) + "\n"


__all__ = ["CaseChange", "DatasetDiff", "diff_datasets", "generate_changelog_entry"]

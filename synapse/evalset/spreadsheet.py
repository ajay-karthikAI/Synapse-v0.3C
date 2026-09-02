"""
synapse.evalset.spreadsheet
===========================
CSV import and export for spreadsheet-based review.

Clinical reviewers do not work in JSONL. They work in a spreadsheet, so the
review loop has to survive a round trip through one — including the things
spreadsheets do to data without being asked.

The export is a *review sheet*, not a dump of the case. It carries the case
context a reviewer needs, plus blank columns for their judgement. The import
reads those columns back and turns each completed row into a
:class:`CaseReview`. That direction of flow matters: a reviewer files a review,
they do not edit the case. Letting a spreadsheet overwrite case fields directly
would erase the distinction between what was labelled and who labelled it.

Three spreadsheet hazards are handled explicitly:

* **Formula injection.** A cell beginning ``=``, ``+``, ``-`` or ``@`` is
  executed as a formula by Excel and Sheets. Exported values that begin with
  one are prefixed with a single quote, so a query like ``-=cmd`` cannot become
  a live formula in a reviewer's spreadsheet.
* **Identifier mangling.** Spreadsheets coerce things that look numeric. Case
  and document identifiers are kebab-case or scheme-prefixed, so they survive,
  but the importer still strips a leading quote in case one was added.
* **Blank rows.** A reviewer who skips a row leaves it blank rather than
  deleting it; blank rows are skipped, not treated as errors.
"""

from __future__ import annotations  # Postponed annotations

import csv  # Standard library CSV; no pandas dependency for a few hundred rows
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from synapse.errors import ArtifactSchemaError
from synapse.logging import get_logger
from synapse.schemas.enums import EvalCategory, EvalExpectedBehavior, ReviewerRole
from synapse.schemas.evalset import CaseReview, EvalCase, GradedRelevance

logger = get_logger(__name__)

# Columns the exporter writes. The first block is read-only context; the second
# is what a reviewer fills in. Order is fixed so a sheet is recognisable.
CONTEXT_COLUMNS = (
    "case_id",
    "query",
    "current_category",
    "current_expected_behavior",
    "required_concepts",
    "forbidden_claims",
    "graded_documents",
    "notes_for_reviewer",
)
REVIEW_COLUMNS = (
    "reviewer_id",
    "review_category",
    "review_expected_behavior",
    "review_graded_documents",
    "review_confidence",
    "review_notes",
)
ALL_COLUMNS = CONTEXT_COLUMNS + REVIEW_COLUMNS

_FORMULA_PREFIXES = (
    "=",
    "+",
    "-",
    "@",
    "\t",
    "\r",
)  # Characters that make a spreadsheet treat a cell as a formula

GRADE_SEPARATOR = ";"  # Between graded entries
GRADE_DELIMITER = ":"  # Between an identifier and its grade
LIST_SEPARATOR = (
    "|"  # Between free-text list items; '|' is rare in clinical prose, unlike ',' or ';'
)


def _escape_for_spreadsheet(value: str) -> str:
    """Neutralise a value that a spreadsheet would execute as a formula."""
    if value and value.startswith(_FORMULA_PREFIXES):
        return "'" + value  # Leading apostrophe forces text interpretation in Excel and Sheets
    return value


def _unescape_from_spreadsheet(value: str) -> str:
    """Strip a protective leading apostrophe added on export."""
    return value[1:] if value.startswith("'") else value


def encode_grades(grades: list[GradedRelevance]) -> str:
    """Render graded relevance as ``doc_id:grade;doc_id:grade``."""
    return GRADE_SEPARATOR.join(
        f"{grade.target_id}{GRADE_DELIMITER}{grade.grade}" for grade in grades
    )


def decode_grades(value: str) -> list[GradedRelevance]:
    """Parse ``doc_id:grade;doc_id:grade`` back into graded relevance.

    Raises:
        ArtifactSchemaError: an entry is malformed. Raised rather than skipped,
            because a silently dropped relevance judgement is a labelling error
            that would never be noticed.
    """
    grades: list[GradedRelevance] = []
    for raw_entry in value.split(GRADE_SEPARATOR):
        entry = raw_entry.strip()
        if not entry:  # Trailing separator or an empty cell
            continue
        target, separator, grade_text = entry.rpartition(
            GRADE_DELIMITER
        )  # rpartition: document ids contain ':' themselves, e.g. 'pubmed:123'
        if not separator or not grade_text.strip().isdigit():
            raise ArtifactSchemaError(
                problem="malformed graded-relevance entry; expected '<document_id>:<grade>'",
                entry_count=len(grades),
            )
        grades.append(GradedRelevance(target_id=target.strip(), grade=int(grade_text.strip())))
    return grades


def export_review_sheet(
    cases: list[EvalCase], path: Path, *, reviewer_id: str | None = None
) -> int:
    """Write a review sheet for the given cases.

    Synthetic cases are omitted: they are illustrations, not candidates for
    review, and including them would invite a reviewer to spend time producing
    a record the schema will refuse.

    Returns the number of rows written.
    """
    reviewable = [case for case in cases if not case.is_synthetic and not case.excluded]
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open(
        "w", encoding="utf-8", newline=""
    ) as handle:  # newline="" is required by csv to avoid doubled line endings on Windows
        writer = csv.DictWriter(handle, fieldnames=ALL_COLUMNS)
        writer.writeheader()
        for case in reviewable:
            writer.writerow(
                {
                    "case_id": case.case_id,
                    "query": _escape_for_spreadsheet(case.query),
                    "current_category": case.category.value,
                    "current_expected_behavior": case.expected_behavior.value,
                    "required_concepts": LIST_SEPARATOR.join(case.required_concepts),
                    "forbidden_claims": LIST_SEPARATOR.join(case.forbidden_claims),
                    "graded_documents": encode_grades(case.relevant_documents),
                    "notes_for_reviewer": _escape_for_spreadsheet(case.known_limitations),
                    # Review columns ship blank, except the reviewer identifier
                    # when the sheet is prepared for one named reviewer.
                    "reviewer_id": reviewer_id or "",
                    "review_category": "",
                    "review_expected_behavior": "",
                    "review_graded_documents": "",
                    "review_confidence": "",
                    "review_notes": "",
                }
            )

    logger.info(
        "review sheet exported",
        extra={"rows": len(reviewable), "reviewer_id": reviewer_id or "<unassigned>"},
    )
    return len(reviewable)


@dataclass(frozen=True)
class ImportedReview:
    """One review parsed from a completed sheet."""

    case_id: str
    review: CaseReview


@dataclass
class ImportResult:
    """Outcome of importing a completed review sheet."""

    reviews: list[ImportedReview]
    skipped_rows: int  # Rows left blank by the reviewer
    errors: list[str]  # Rows that could not be parsed, with row numbers

    @property
    def ok(self) -> bool:
        """True when every non-blank row parsed."""
        return not self.errors


def import_review_sheet(
    path: Path,
    *,
    protocol_version: str,
    reviewer_role: ReviewerRole = ReviewerRole.CLINICIAN,
    reviewed_at: datetime | None = None,
) -> ImportResult:
    """Read a completed review sheet into :class:`CaseReview` records.

    Rows with no ``reviewer_id`` and no ``review_category`` are treated as
    unfilled and skipped. A row that is partially filled is an *error*, not a
    skip — a half-completed review usually means the reviewer was interrupted,
    and silently discarding it would lose their work without telling them.

    The timestamp is supplied by the caller rather than read from the sheet,
    because a spreadsheet date cell round-trips through several ambiguous
    formats and a mis-parsed review timestamp corrupts expiry calculations.
    """
    if not path.is_file():
        raise ArtifactSchemaError(path=path, problem="review sheet not found")

    timestamp = reviewed_at or datetime.now(UTC)
    text = path.read_text(encoding="utf-8-sig")  # utf-8-sig strips the BOM Excel writes on save
    reader = csv.DictReader(io.StringIO(text))

    missing_columns = set(REVIEW_COLUMNS) - set(reader.fieldnames or [])
    if missing_columns:  # A sheet with renamed or deleted columns cannot be parsed reliably
        raise ArtifactSchemaError(
            problem="review sheet is missing required columns", missing_count=len(missing_columns)
        )

    imported: list[ImportedReview] = []
    skipped = 0
    errors: list[str] = []

    for row_number, row in enumerate(
        reader, start=2
    ):  # start=2 so numbers match what the reviewer sees in their spreadsheet
        reviewer_id = _unescape_from_spreadsheet((row.get("reviewer_id") or "").strip())
        category_text = (row.get("review_category") or "").strip()
        behavior_text = (row.get("review_expected_behavior") or "").strip()

        if not reviewer_id and not category_text and not behavior_text:  # Untouched row
            skipped += 1
            continue
        if not (reviewer_id and category_text and behavior_text):
            errors.append(
                f"row {row_number}: partially completed; reviewer_id, review_category and review_expected_behavior are all required"
            )
            continue

        case_id = _unescape_from_spreadsheet((row.get("case_id") or "").strip())
        if not case_id:
            errors.append(f"row {row_number}: missing case_id")
            continue

        try:
            confidence_text = (row.get("review_confidence") or "").strip()
            review = CaseReview(
                reviewer_id=reviewer_id,
                reviewer_role=reviewer_role,
                reviewed_at=timestamp,
                protocol_version=protocol_version,
                category=EvalCategory(category_text),
                expected_behavior=EvalExpectedBehavior(behavior_text),
                relevance_grades=decode_grades(row.get("review_graded_documents") or ""),
                confidence=int(confidence_text)
                if confidence_text.isdigit()
                else 3,  # Default mid-scale when unfilled, rather than rejecting the row
                notes=_unescape_from_spreadsheet((row.get("review_notes") or "").strip()),
            )
        except (ValueError, ArtifactSchemaError) as exc:
            # Narrow catch: enum coercion, pydantic validation and grade parsing.
            # The row number is reported so the reviewer can find it; the cell
            # contents are not echoed, since a review note may quote a query.
            errors.append(
                f"row {row_number}: {type(exc).__name__} — check reviewer_id format, category and behaviour spellings"
            )
            continue

        imported.append(ImportedReview(case_id=case_id, review=review))

    logger.info(
        "review sheet imported",
        extra={"reviews": len(imported), "skipped_rows": skipped, "error_rows": len(errors)},
    )
    return ImportResult(reviews=imported, skipped_rows=skipped, errors=errors)


def export_cases_csv(cases: list[EvalCase], path: Path) -> int:
    """Export full case records to CSV for inspection.

    Read-only: there is no matching importer, deliberately. Cases are edited
    through the labelling CLI, which enforces the lifecycle rules; a
    spreadsheet round trip would let an edit bypass them.
    """
    columns = [
        "case_id",
        "dataset_version",
        "split",
        "query",
        "category",
        "expected_behavior",
        "annotation_status",
        "is_synthetic",
        "reviewer_count",
        "disagreement_status",
        "privacy_class",
        "redaction_status",
        "excluded",
        "corpus_version",
        "graded_documents",
        "required_concepts",
        "forbidden_claims",
        "known_limitations",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for case in cases:
            writer.writerow(
                {
                    "case_id": case.case_id,
                    "dataset_version": case.dataset_version,
                    "split": case.split.value,
                    "query": _escape_for_spreadsheet(case.query),
                    "category": case.category.value,
                    "expected_behavior": case.expected_behavior.value,
                    "annotation_status": case.annotation_status.value,
                    "is_synthetic": str(case.is_synthetic).lower(),
                    "reviewer_count": case.reviewer_count,
                    "disagreement_status": case.disagreement_status.value,
                    "privacy_class": case.privacy_class.value,
                    "redaction_status": case.redaction_status.value,
                    "excluded": str(case.excluded).lower(),
                    "corpus_version": case.corpus_version,
                    "graded_documents": encode_grades(case.relevant_documents),
                    "required_concepts": LIST_SEPARATOR.join(case.required_concepts),
                    "forbidden_claims": LIST_SEPARATOR.join(case.forbidden_claims),
                    "known_limitations": _escape_for_spreadsheet(case.known_limitations),
                }
            )
    return len(cases)


__all__ = [
    "ALL_COLUMNS",
    "CONTEXT_COLUMNS",
    "REVIEW_COLUMNS",
    "ImportResult",
    "ImportedReview",
    "decode_grades",
    "encode_grades",
    "export_cases_csv",
    "export_review_sheet",
    "import_review_sheet",
]

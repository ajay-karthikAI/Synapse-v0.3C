"""
synapse.schemas.evaluation
==========================
:class:`EvaluationRun`.

The evaluation *dataset* models — cases, reviews, adjudication, manifest —
live in :mod:`synapse.schemas.evalset`.

**Schemas only.** No metric is implemented in this change, no evaluation case
is authored, and no clinical label is assigned. These models define the shape
that a later change will populate, and they are included now so the corpus
layer can be built against the identifiers evaluation will reference.

Two things are enforced here rather than left to convention:

  * ``corpus_version`` is mandatory on every case. Relevance labels are not
    portable across corpus versions — re-chunking changes chunk identifiers —
    and the existing built-in evaluation set is a live demonstration of what
    happens without this: all four of its ground-truth PMIDs are absent from
    the shipped corpus, so its metrics are structurally zero no matter how well
    retrieval performs (docs/quality-architecture.md §1.1, C4).

  * ``label_provenance`` is mandatory, and ``review_status`` defaults to
    ``unreviewed``. A case cannot claim clinician review without a review
    record, by exactly the same rule that governs source approval.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field, model_validator

from synapse.schemas.base import VersionedModel, require_timezone_aware
from synapse.schemas.enums import (
    RunMode,
)


class EvaluationRun(VersionedModel):
    """Provenance envelope for one evaluation run.

    Deliberately carries *no metric values* in this change. Metrics are out of
    scope here, and the deterministic/LLM-judged separation they require is a
    later change. What this model fixes now is the provenance: a run that
    cannot name its dataset, corpus and index cannot be compared with any other
    run, and comparing runs across different corpora is the classic false-green.
    """

    SCHEMA_NAME: ClassVar[str] = "evaluation_run"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    run_id: str = Field(min_length=1, description="Identifier for this run.")
    started_at: datetime = Field(description="Run start (timezone-aware).")
    completed_at: datetime | None = Field(
        default=None, description="Run completion, when it finished."
    )

    dataset_version: str = Field(min_length=1, description="Evaluation dataset used.")
    corpus_version: str = Field(min_length=1, description="Corpus used.")
    index_id: str = Field(min_length=1, description="Index build used.")
    source_pack_version: str | None = Field(
        default=None, description="Source pack used, when one exists."
    )

    mode: RunMode = Field(description="Offline-deterministic or live.")
    synapse_version: str = Field(
        min_length=1, description="Version of this package that produced the run."
    )
    git_commit: str | None = Field(default=None, description="Source commit, when available.")

    cases_total: int = Field(ge=0, description="Cases in the dataset.")
    cases_run: int = Field(ge=0, description="Cases actually executed.")
    cases_skipped: int = Field(ge=0, description="Cases skipped, e.g. retired.")

    provenance_warnings: list[str] = Field(
        default_factory=list,
        description="Statements a reader must see before the numbers, e.g. that no case is clinician-reviewed.",
    )

    @model_validator(mode="after")
    def _validate_run(self) -> EvaluationRun:
        """Reject naive timestamps, inverted intervals, and counts that do not reconcile."""
        require_timezone_aware(self.started_at, "started_at")
        if self.completed_at is not None:
            require_timezone_aware(self.completed_at, "completed_at")
            if (
                self.completed_at < self.started_at
            ):  # A run cannot finish before it starts; this usually means a timezone bug
                raise ValueError("completed_at must not precede started_at")
        if (
            self.cases_run + self.cases_skipped != self.cases_total
        ):  # Silently unaccounted cases would understate coverage
            raise ValueError("cases_run + cases_skipped must equal cases_total")
        return self


__all__ = ["EvaluationRun"]

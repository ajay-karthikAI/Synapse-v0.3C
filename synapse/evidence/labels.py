"""
synapse.evidence.labels
=======================
Plain-language names and explanations for governed evidence metadata.

Requirements 10 and 11 together: a label must come from the governed record, and
it must be explained in words a patient can act on. "systematic_review" is a
governed fact; it is also jargon, and an unexplained label on a medical page
either misleads or is ignored.

Three rules the wordings below follow:

1. **Describe the source, never rate it.** "A summary of many earlier studies"
   says what a systematic review *is*. "High-quality evidence" would be a
   judgement this system is not qualified to make, and would be read as applying
   to the patient's own situation.
2. **Never imply clinical review that has not happened.** Every source in the
   shipped example pack is ``discovered`` — found by automated ingestion, read by
   nobody. The label says exactly that.
3. **Absence is stated, not softened.** "Not reviewed by a clinician" is the
   honest rendering of an unreviewed source; "review pending" would imply a
   process that may not be scheduled.

The evidence-type descriptions deliberately avoid a hierarchy. Presenting a
ranked ladder (guideline > review > RCT > cohort) invites a patient to weigh two
sources against each other, which is a clinical judgement that depends on the
question being asked.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass

from synapse.schemas.enums import EvidenceType, RetractionStatus, SourceLifecycleState


@dataclass(frozen=True)
class Label:
    """A short display name and the sentence that explains it."""

    name: str
    explanation: str
    tone: str = "neutral"  # neutral | caution — styling only, never a quality claim


# ---------------------------------------------------------------------------
# Evidence type — what kind of document this is
# ---------------------------------------------------------------------------

EVIDENCE_TYPE_LABELS: dict[EvidenceType, Label] = {
    EvidenceType.GUIDELINE: Label(
        "Clinical guideline",
        "Recommendations published by a medical organisation to help clinicians make decisions.",
    ),
    EvidenceType.SYSTEMATIC_REVIEW: Label(
        "Systematic review",
        "A structured summary of the studies published on a question, gathered using a documented search.",
    ),
    EvidenceType.META_ANALYSIS: Label(
        "Meta-analysis",
        "A study that combines the results of several earlier studies statistically.",
    ),
    EvidenceType.RCT: Label(
        "Randomised trial",
        "A study where participants were assigned to treatments by chance, to compare the results.",
    ),
    EvidenceType.COHORT: Label(
        "Cohort study",
        "A study that follows a group of people over time and records what happens.",
    ),
    EvidenceType.CASE_CONTROL: Label(
        "Case-control study",
        "A study comparing people who have a condition with people who do not.",
    ),
    EvidenceType.CASE_REPORT: Label(
        "Case report",
        "A description of one patient or a small number of patients. It describes what happened to them, not what happens generally.",
        tone="caution",
    ),
    EvidenceType.NARRATIVE_REVIEW: Label(
        "Review article",
        "An expert summary of a topic. It does not follow a documented search method.",
    ),
}

UNKNOWN_EVIDENCE_TYPE = Label(
    "Type not recorded",
    "The kind of study or document is not recorded for this source.",
    tone="caution",
)


# ---------------------------------------------------------------------------
# Review state — what a clinician has and has not done with this source
# ---------------------------------------------------------------------------

REVIEW_STATE_LABELS: dict[SourceLifecycleState, Label] = {
    SourceLifecycleState.DISCOVERED: Label(
        "Not reviewed by a clinician",
        "This source was found automatically. No clinician has checked whether it is appropriate for this purpose.",
        tone="caution",
    ),
    SourceLifecycleState.SCREENED: Label(
        "Checked for relevance only",
        "Someone confirmed this source is on-topic, but it has not been approved for use with patients.",
        tone="caution",
    ),
    SourceLifecycleState.APPROVED: Label(
        "Reviewed by a clinician",
        "A qualified reviewer approved this source for the topics this pack covers, and recorded a date to review it again.",
    ),
    SourceLifecycleState.REJECTED: Label(
        "Excluded by a reviewer",
        "A reviewer decided this source should not be used. It is not shown in answers.",
        tone="caution",
    ),
    SourceLifecycleState.EXPIRED: Label(
        "Review out of date",
        "This source was approved before, but the date set for re-checking it has passed. It is not shown in answers.",
        tone="caution",
    ),
    SourceLifecycleState.SUPERSEDED: Label(
        "Replaced by a newer source",
        "A more recent source has replaced this one. It is not shown in answers.",
        tone="caution",
    ),
}

UNGOVERNED_LABEL = Label(
    "No governance record",
    "This source is not described by the reviewed source list, so no review information is available for it.",
    tone="caution",
)


# ---------------------------------------------------------------------------
# Retraction — a published correction to the record
# ---------------------------------------------------------------------------

RETRACTION_LABELS: dict[RetractionStatus, Label] = {
    RetractionStatus.RETRACTED: Label(
        "Retracted",
        "The publisher has withdrawn this article. It is not used in answers.",
        tone="caution",
    ),
    RetractionStatus.EXPRESSION_OF_CONCERN: Label(
        "Concerns published",
        "The publisher has raised concerns about this article. It is not used in answers.",
        tone="caution",
    ),
    RetractionStatus.CORRECTED: Label(
        "Correction published",
        "A correction to this article has been published.",
    ),
    RetractionStatus.UNCHECKED: Label(
        "Retraction status not checked",
        "Whether this article has been retracted has not been checked.",
        tone="caution",
    ),
}


# The one sentence that must accompany any retrieval-relevance figure. Not
# "confidence" (requirement 7): the number is a reranker's rating of how useful a
# passage looked for this question, and reading it as confidence in the medicine
# is the specific misunderstanding this wording exists to prevent.
RELEVANCE_LABEL = "retrieval relevance"
RELEVANCE_EXPLANATION = (
    "How closely this passage matched your question during the search. "
    "It does not say how reliable the research is, and it is not a medical judgement."
)


def describe_evidence_type(evidence_type: EvidenceType | None) -> Label:
    """Plain-language label for an evidence type, or the not-recorded label."""
    if evidence_type is None:
        return UNKNOWN_EVIDENCE_TYPE
    return EVIDENCE_TYPE_LABELS.get(evidence_type, UNKNOWN_EVIDENCE_TYPE)


def describe_review_state(state: SourceLifecycleState | None, *, is_governed: bool = True) -> Label:
    """Plain-language label for a review state.

    An ungoverned source gets :data:`UNGOVERNED_LABEL` rather than a guess: the
    absence of a governance record is itself the fact worth showing.
    """
    if not is_governed or state is None:
        return UNGOVERNED_LABEL
    return REVIEW_STATE_LABELS.get(state, UNGOVERNED_LABEL)


def describe_retraction(status: RetractionStatus | None) -> Label | None:
    """Label for a retraction status, or ``None`` when the record is clean.

    ``NONE`` returns ``None`` on purpose: "not retracted" is the expected case,
    and a badge saying so on every source trains readers to ignore the badge
    that matters.
    """
    if status is None or status is RetractionStatus.NONE:
        return None
    return RETRACTION_LABELS.get(status)


__all__ = [
    "EVIDENCE_TYPE_LABELS",
    "RELEVANCE_EXPLANATION",
    "RELEVANCE_LABEL",
    "RETRACTION_LABELS",
    "REVIEW_STATE_LABELS",
    "UNGOVERNED_LABEL",
    "UNKNOWN_EVIDENCE_TYPE",
    "Label",
    "describe_evidence_type",
    "describe_retraction",
    "describe_review_state",
]

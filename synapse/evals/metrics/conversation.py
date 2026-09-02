"""
synapse.evals.metrics.conversation
==================================
Did a follow-up question find the right evidence?

What this measures, and what it does not
----------------------------------------
A follow-up like "how is it treated?" carries no subject of its own. Answering
it requires recovering the referent from an earlier turn, and if that fails the
system searches for six context-free words and retrieves whatever happens to
share them. The failure is silent: the patient gets a fluent answer about the
wrong thing.

So the question this module scores is narrow and answerable from a recorded
run: **for a case whose query depends on earlier turns, did retrieval surface a
document a human labelled relevant?** Nothing here inspects the rewritten query,
because the rewrite is an implementation detail — a system that resolves
follow-ups by some other means, or needs no resolution at all, should score the
same. The metric describes an outcome, not a mechanism.

It is deliberately NOT a measure of answer quality. A follow-up can retrieve the
right document and still be answered badly; that is what the answer metrics are
for. Conflating them would produce a number that moves for reasons unrelated to
conversation.

Honest denominators
-------------------
Only cases with a non-empty ``context`` AND graded ``relevant_documents`` are
scored. A conversational case nobody labelled contributes ``None`` and is
excluded from the denominator rather than counted as a pass — an unlabelled case
is an absence of evidence, and scoring it as success is how an evaluation starts
flattering the system it measures.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Sequence

from synapse.schemas.evalset import GradedRelevance

# A graded relevance at or above this grade counts as "the right document". The
# repository's grades are 0 = irrelevant, 1 = marginal, 2+ = relevant; requiring
# 2 keeps a marginal hit from being reported as a resolved follow-up.
RELEVANT_GRADE_FLOOR = 2


def is_followup(context: Sequence[str]) -> bool:
    """True when a case depends on earlier turns.

    A case is conversational if it carries context, full stop. There is no
    attempt to judge whether the question *looks* elliptical: that would be this
    module second-guessing a label a human assigned.
    """
    return any(isinstance(turn, str) and turn.strip() for turn in context)


def followup_resolution(
    context: Sequence[str],
    retrieved_document_ids: Sequence[str],
    relevant_documents: Sequence[GradedRelevance],
    k: int,
) -> float | None:
    """1.0 if a relevant document was retrieved in the top ``k``, else 0.0.

    Returns ``None`` — excluded from the denominator — when the case is not
    conversational, or when nobody graded a relevant document for it.

    Hit-at-k rather than recall or nDCG on purpose. The question a follow-up
    poses is binary: was the referent recovered at all? Averaging a rank-aware
    score across cases would let a near-miss on one case offset a total failure
    on another, and a total failure is the thing being hunted.
    """
    if not is_followup(context):
        return None
    wanted = {
        grade.target_id for grade in relevant_documents if grade.grade >= RELEVANT_GRADE_FLOOR
    }
    if not wanted:
        return None  # Unlabelled: absent evidence, not a pass
    return 1.0 if any(doc in wanted for doc in list(retrieved_document_ids)[:k]) else 0.0


__all__ = ["RELEVANT_GRADE_FLOOR", "followup_resolution", "is_followup"]

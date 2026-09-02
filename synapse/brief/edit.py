"""
synapse.brief.edit
==================
The editing rules, as functions that return a new brief.

Every operation is pure: it takes a brief and returns a new one. Nothing mutates
in place, so an edit cannot half-apply, and the pre-edit document is still
available to compare against — which is what makes "show me what I changed"
possible without a diff engine.

The rule that matters
---------------------
**A user may change any text. What they cannot do is keep the verification
that belonged to the previous wording.** :func:`edit_claim_text` therefore does
three things at once, atomically: replaces the text, sets the origin to
``USER_EDITED``, and strips the citation markers. Any one of those without the
others produces a document that misrepresents itself, so the schema rejects the
partial states (:class:`~synapse.brief.schema.BriefClaim`) and this module never
constructs one.

Reordering is a separate case entirely. Questions cite nothing, so moving them
changes nothing about provenance; and reordering *claims* does not touch their
markers, because a marker refers to a numbered source, not to a position in a
list.
"""

from __future__ import annotations  # Postponed annotations

from synapse.brief.schema import (
    MAX_QUESTIONS,
    AppointmentBrief,
    BriefClaim,
    BriefQuestion,
    BriefSection,
    ContentOrigin,
    UserContent,
)


class EditError(ValueError):
    """An edit that would produce an incoherent document."""


def set_topic(brief: AppointmentBrief, topic: str) -> AppointmentBrief:
    """Replace the user's appointment topic."""
    return brief.model_copy(update={"user": brief.user.model_copy(update={"topic": topic.strip()})})


def set_notes(brief: AppointmentBrief, notes: str) -> AppointmentBrief:
    """Replace the user's personal notes."""
    return brief.model_copy(update={"user": brief.user.model_copy(update={"notes": notes.strip()})})


def add_question(brief: AppointmentBrief, text: str) -> AppointmentBrief:
    """Append a user-authored question.

    Marked ``USER_AUTHORED``, so the rendered brief shows it as the patient's own
    question rather than one the system proposed. Questions assert nothing, so
    this carries no provenance implication.
    """
    cleaned = text.strip()
    if not cleaned:
        raise EditError("a question cannot be empty")
    if len(brief.questions) >= MAX_QUESTIONS:
        raise EditError(f"a brief holds at most {MAX_QUESTIONS} questions")
    existing = {question.question_id for question in brief.questions}
    ordinal = len(brief.questions) + 1
    while f"u{ordinal}" in existing:
        ordinal += 1
    question = BriefQuestion(
        question_id=f"u{ordinal}", text=cleaned, origin=ContentOrigin.USER_AUTHORED
    )
    return brief.model_copy(update={"questions": [*brief.questions, question]})


def remove_question(brief: AppointmentBrief, question_id: str) -> AppointmentBrief:
    """Drop one question."""
    remaining = [question for question in brief.questions if question.question_id != question_id]
    if len(remaining) == len(brief.questions):
        raise EditError("no question with that identifier")
    return brief.model_copy(update={"questions": remaining})


def reorder_questions(brief: AppointmentBrief, order: list[str]) -> AppointmentBrief:
    """Reorder questions by identifier.

    The order must be a permutation of the current questions. A partial list
    would silently delete the ones left out, which is a destructive operation
    disguised as a cosmetic one.
    """
    current = {question.question_id: question for question in brief.questions}
    if sorted(order) != sorted(current):
        raise EditError("reordering requires exactly the current set of question identifiers")
    return brief.model_copy(update={"questions": [current[identifier] for identifier in order]})


def edit_claim_text(brief: AppointmentBrief, claim_id: str, text: str) -> AppointmentBrief:
    """Replace a claim's wording, and remove the verification that no longer applies.

    Requirements 3, 4 and 5 in one operation. The new wording is **not** the
    sentence the verifier checked against a source, so it loses its verified
    status, loses its citation markers, and keeps the original text alongside it
    for comparison.

    Nothing here re-verifies. Restoring verified status requires running the
    claim back through :mod:`synapse.answer.verify` against the evidence, which
    is a different operation performed by a different layer.
    """
    cleaned = text.strip()
    if not cleaned:
        raise EditError("a claim cannot be empty; remove it instead")

    updated: list[BriefClaim] = []
    found = False
    for claim in brief.claims:
        if claim.claim_id != claim_id:
            updated.append(claim)
            continue
        found = True
        if cleaned == claim.text:
            updated.append(claim)  # A no-op edit must not de-verify anything
            continue
        updated.append(
            BriefClaim(
                claim_id=claim.claim_id,
                text=cleaned,
                source_numbers=[],  # The citation belonged to the previous wording
                support=claim.support,
                origin=ContentOrigin.USER_EDITED,
                original_text=claim.original_text or claim.text,
                # The markers move with the wording they described, so the
                # printed original line keeps its provenance and the source list
                # does not end up with an entry nothing refers to.
                original_source_numbers=claim.original_source_numbers or claim.source_numbers,
            )
        )
    if not found:
        raise EditError("no claim with that identifier")
    return brief.model_copy(update={"claims": updated})


def restore_claim(
    brief: AppointmentBrief, claim_id: str, source_numbers: list[int] | None = None
) -> AppointmentBrief:
    """Revert an edited claim to its verified wording.

    Markers default to the ones recorded on the claim when it was edited. Those
    are not user-editable through any operation in this module, so restoring
    from them restores from the build rather than from something the user has
    touched. A caller still holding the original answer may pass them
    explicitly.
    """
    updated: list[BriefClaim] = []
    found = False
    for claim in brief.claims:
        if claim.claim_id != claim_id or claim.origin is not ContentOrigin.USER_EDITED:
            updated.append(claim)
            continue
        found = True
        updated.append(
            BriefClaim(
                claim_id=claim.claim_id,
                text=claim.original_text,
                source_numbers=sorted(
                    source_numbers if source_numbers is not None else claim.original_source_numbers
                ),
                support=claim.support,
                origin=ContentOrigin.VERIFIED_EVIDENCE,
            )
        )
    if not found:
        raise EditError("no edited claim with that identifier")
    return brief.model_copy(update={"claims": updated})


def remove_claim(brief: AppointmentBrief, claim_id: str) -> AppointmentBrief:
    """Drop a claim from the brief.

    Sources left citing nothing are pruned with it: a source list containing an
    entry nothing refers to is noise on a one-page document.
    """
    remaining = [claim for claim in brief.claims if claim.claim_id != claim_id]
    if len(remaining) == len(brief.claims):
        raise EditError("no claim with that identifier")
    still_cited = {number for claim in remaining for number in claim.source_numbers}
    return brief.model_copy(
        update={
            "claims": remaining,
            "sources": [source for source in brief.sources if source.number in still_cited],
        }
    )


def set_sections(brief: AppointmentBrief, sections: list[BriefSection]) -> AppointmentBrief:
    """Choose which optional sections to keep.

    The disclaimer is not a :class:`BriefSection` and cannot be dropped here,
    which is why it is not a member of that enum.
    """
    return brief.model_copy(update={"included_sections": list(dict.fromkeys(sections))})


def user_content_summary(brief: AppointmentBrief) -> dict[str, int]:
    """How much of the brief is the user's own, for the export warning."""
    return {
        "user_authored_questions": sum(
            1 for question in brief.questions if question.origin is ContentOrigin.USER_AUTHORED
        ),
        "edited_claims": brief.edited_claim_count,
        "has_notes": int(bool(brief.user.notes.strip())),
        "has_topic": int(bool(brief.user.topic.strip())),
    }


__all__ = [
    "EditError",
    "UserContent",
    "add_question",
    "edit_claim_text",
    "remove_claim",
    "remove_question",
    "reorder_questions",
    "restore_claim",
    "set_notes",
    "set_sections",
    "set_topic",
    "user_content_summary",
]

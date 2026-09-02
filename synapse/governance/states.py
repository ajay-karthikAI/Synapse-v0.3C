"""
synapse.governance.states
=========================
The lifecycle transition table, and who is allowed to drive it.

The single most important thing this module encodes is the split between
transitions a *machine* may perform and transitions that require a *human
review record*:

* Automation may create ``DISCOVERED`` entries, and may apply ``EXPIRED``
  (a date passed) and ``SUPERSEDED`` (a newer source replaced this one).
  Both are mechanical facts, not judgements.
* ``SCREENED``, ``APPROVED`` and ``REJECTED`` are clinical judgements. No code
  path in Synapse can produce them without a :class:`ClinicianReview` supplied
  by an operator.

Encoding this as a table rather than as scattered ``if`` statements means the
rule can be read, tested and audited in one place — and that adding a state
without deciding its actor is impossible.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from enum import StrEnum

from synapse.errors import SynapseArtifactError
from synapse.schemas.enums import SourceLifecycleState as State


class TransitionActor(StrEnum):  # Who is permitted to perform a transition
    """The actor a transition requires."""

    AUTOMATION = "automation"  # A machine may perform this: it records a mechanical fact
    HUMAN_REVIEW = "human_review"  # Requires a ClinicianReview supplied by an operator


class GovernanceError(SynapseArtifactError):  # Typed, user-safe: inherits the sanitising base
    """A governance rule was violated."""

    reason = "governance rule violated"


@dataclass(frozen=True)
class Transition:
    """One permitted lifecycle transition."""

    source: State  # State being left
    target: State  # State being entered
    actor: TransitionActor  # Who may perform it
    rationale: str  # Why this transition exists, for the audit trail


# The complete transition table. Anything not listed here is forbidden.
TRANSITIONS: tuple[Transition, ...] = (
    # -- human judgements ---------------------------------------------------
    Transition(
        State.DISCOVERED,
        State.SCREENED,
        TransitionActor.HUMAN_REVIEW,
        "A human triaged the source as in scope for the pack.",
    ),
    Transition(
        State.DISCOVERED,
        State.REJECTED,
        TransitionActor.HUMAN_REVIEW,
        "A human excluded the source outright without full review.",
    ),
    Transition(
        State.SCREENED,
        State.APPROVED,
        TransitionActor.HUMAN_REVIEW,
        "A qualified clinician approved the source for the pack's declared scope.",
    ),
    Transition(
        State.SCREENED,
        State.REJECTED,
        TransitionActor.HUMAN_REVIEW,
        "Full review concluded the source is not suitable.",
    ),
    Transition(
        State.APPROVED,
        State.REJECTED,
        TransitionActor.HUMAN_REVIEW,
        "A later review withdrew a prior approval.",
    ),
    # Re-review paths. A rejection or an expiry is not permanent: new evidence
    # or a new pack scope can make a previously unsuitable source suitable.
    Transition(
        State.REJECTED,
        State.SCREENED,
        TransitionActor.HUMAN_REVIEW,
        "A human re-opened a previously rejected source.",
    ),
    Transition(
        State.EXPIRED,
        State.SCREENED,
        TransitionActor.HUMAN_REVIEW,
        "An expired approval was re-opened for re-review.",
    ),
    Transition(
        State.EXPIRED,
        State.APPROVED,
        TransitionActor.HUMAN_REVIEW,
        "A clinician re-approved an expired source with a fresh review.",
    ),
    # -- mechanical facts ---------------------------------------------------
    Transition(
        State.APPROVED,
        State.EXPIRED,
        TransitionActor.AUTOMATION,
        "The approval's review-due date passed. A date, not a judgement.",
    ),
    # Supersession is reachable from every non-terminal state: a source can be
    # replaced by a newer edition at any point in its life.
    Transition(
        State.DISCOVERED,
        State.SUPERSEDED,
        TransitionActor.AUTOMATION,
        "A newer source replaced this one.",
    ),
    Transition(
        State.SCREENED,
        State.SUPERSEDED,
        TransitionActor.AUTOMATION,
        "A newer source replaced this one.",
    ),
    Transition(
        State.APPROVED,
        State.SUPERSEDED,
        TransitionActor.AUTOMATION,
        "A newer source replaced this one.",
    ),
    Transition(
        State.REJECTED,
        State.SUPERSEDED,
        TransitionActor.AUTOMATION,
        "A newer source replaced this one.",
    ),
    Transition(
        State.EXPIRED,
        State.SUPERSEDED,
        TransitionActor.AUTOMATION,
        "A newer source replaced this one.",
    ),
)

# SUPERSEDED is terminal. A superseded source is never revived — the successor
# is a separate entry with its own review history, so the record of what was
# reviewed when stays intact.
TERMINAL_STATES: frozenset[State] = frozenset({State.SUPERSEDED})

# States that automation may assign when first creating an entry. Exactly one,
# and that is the point: ingestion discovers, it does not judge.
AUTOMATION_CREATABLE_STATES: frozenset[State] = frozenset({State.DISCOVERED})

# States whose entry requires a review record. Mirrors the model-level
# invariants in PackSource, deliberately: the rule is enforced both at the
# record boundary and at the transition boundary, so neither path can be the
# one that lets an unreviewed approval through.
REVIEW_REQUIRED_STATES: frozenset[State] = frozenset(
    {State.SCREENED, State.APPROVED, State.REJECTED}
)


_TRANSITION_INDEX: dict[tuple[State, State], Transition] = {
    (t.source, t.target): t for t in TRANSITIONS
}  # Built once for O(1) lookup


def find_transition(source: State, target: State) -> Transition | None:
    """Return the permitted transition between two states, or None."""
    return _TRANSITION_INDEX.get((source, target))


def allowed_targets(source: State) -> frozenset[State]:
    """Every state reachable from ``source`` in one step."""
    return frozenset(t.target for t in TRANSITIONS if t.source is source)


def require_transition(source: State, target: State, *, actor: TransitionActor) -> Transition:
    """Validate a transition, or raise :class:`GovernanceError`.

    Two distinct failures are reported separately, because the operator
    response differs: an impossible transition is a logic error, while a
    transition attempted by the wrong actor means a human review is missing.
    """
    if source is target:  # A no-op is not an error, but it is not a transition either
        raise GovernanceError(problem="source and target states are identical", state=source.value)
    if source in TERMINAL_STATES:  # Nothing leaves a terminal state
        raise GovernanceError(problem="state is terminal", state=source.value)

    transition = find_transition(source, target)
    if transition is None:
        raise GovernanceError(
            problem="transition is not permitted",
            source=source.value,
            target=target.value,
            permitted=",".join(sorted(s.value for s in allowed_targets(source))) or "<none>",
        )
    if (
        transition.actor is TransitionActor.HUMAN_REVIEW
        and actor is not TransitionActor.HUMAN_REVIEW
    ):
        # THE rule this module exists for: automation cannot manufacture a
        # clinical judgement by driving the state machine.
        raise GovernanceError(
            problem="transition requires a human review record and cannot be performed by automation",
            source=source.value,
            target=target.value,
        )
    return transition


def require_automation_creatable(state: State) -> None:
    """Raise unless ``state`` is one automation is permitted to create.

    Called by the discovery importer. It is the guard that makes requirement
    5's "automatic ingestion can only create discovered entries" a property of
    the code rather than a convention.
    """
    if state not in AUTOMATION_CREATABLE_STATES:
        raise GovernanceError(
            problem="automated ingestion may only create 'discovered' entries",
            attempted=state.value,
            permitted=",".join(sorted(s.value for s in AUTOMATION_CREATABLE_STATES)),
        )


__all__ = [
    "AUTOMATION_CREATABLE_STATES",
    "REVIEW_REQUIRED_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "GovernanceError",
    "Transition",
    "TransitionActor",
    "allowed_targets",
    "find_transition",
    "require_automation_creatable",
    "require_transition",
]

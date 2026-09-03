"""
synapse.service.conversation
============================
Conversation state: the follow-up history, and the emergency latch.

Both were closures over ``st.session_state`` in ``app.py``. Neither had anything
to do with Streamlit -- they read a list of turns and return, respectively, the
context a retrieval rewrite needs and a boolean the safety layer needs. Extracted
here they are typed, tested, and reusable by a request-scoped session in a
server process.

The two are kept **separate**, which is the part worth not losing:

* :meth:`Conversation.retrieval_history` deliberately *excludes* escalations and
  failed turns. Neither carries evidence context, because neither performed any
  retrieval, so feeding them to a query rewrite steers a search on nothing.
* :meth:`Conversation.recent_escalation` exists *because* of that exclusion. A
  patient who described crushing chest pain and then asked "is that serious?"
  was escalated on the first turn and answered normally on the second: the
  follow-up carries no emergency vocabulary of its own for the detector to fire
  on. The latch remembers the verdict the detector already reached.

Their failure directions are opposite, and that is not an inconsistency:

* history fails **open** (returns nothing) -- context is an optimisation, and a
  malformed turn must never cost a patient their answer;
* the latch fails **closed** (returns True) -- a fault there costs a red flag,
  so an unreadable conversation escalates rather than staying silent.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field

from synapse.answer.schema import AnswerAction
from synapse.memory.query_rewrite import TurnSummary
from synapse.ui.pipeline import TurnOutcome

# How many earlier turns reach a query rewrite, and how far back the latch
# looks. Equal on purpose: the latch releases after the same number of
# non-escalated turns that the rewrite window spans, rather than persisting for
# the whole session.
DEFAULT_HISTORY_LIMIT = 3
DEFAULT_ESCALATION_WINDOW = 3


@dataclass(frozen=True)
class ConversationTurn:
    """One question and the outcome it produced.

    The outcome is a typed :class:`~synapse.ui.pipeline.TurnOutcome`, never a
    dict of rendered prose. Nothing downstream may re-derive structure by
    reading text back out of a turn.
    """

    query: str
    outcome: TurnOutcome

    @property
    def answered(self) -> bool:
        """True when this turn produced a renderable answer."""
        return self.outcome.ok

    @property
    def action(self) -> AnswerAction | None:
        """The action state, or None for a failed turn."""
        if self.outcome.presentation is None:
            return None
        return self.outcome.presentation.answer.action

    @property
    def escalated(self) -> bool:
        """True when the red-flag detector fired on this turn."""
        return self.action is AnswerAction.EMERGENCY


@dataclass
class Conversation:
    """An ordered list of turns, with the two derived views the pipeline needs.

    Mutable, and owned by whatever holds the session -- Streamlit's session
    state locally, an in-memory session record in a server. This class does not
    know or care which.
    """

    turns: list[ConversationTurn] = field(default_factory=list)

    def append(self, turn: ConversationTurn) -> None:
        """Record a completed turn."""
        self.turns.append(turn)

    def retrieval_history(self, limit: int = DEFAULT_HISTORY_LIMIT) -> list[TurnSummary]:
        """The last ``limit`` eligible turns, oldest first. Never raises.

        Excluded: failed turns, which have no answer at all, and emergency
        escalations, which are produced ahead of any retrieval and so carry no
        evidence context.

        An abstention is **kept but blanked**. Its summary is fixed boilerplate
        substituted by :func:`synapse.answer.policy.apply`, identical for every
        abstained turn, stating that the sources came up empty -- a fact about
        the corpus, not about what the patient is asking, and it steers the
        rewrite wrong. The patient's own question is the context that resolves a
        follow-up: after "what is metformin?" abstains, "what about the side
        effects?" still means metformin's.
        """
        summaries: list[TurnSummary] = []
        try:
            for turn in self.turns:
                presentation = turn.outcome.presentation
                if presentation is None:
                    continue
                answer = presentation.answer
                if answer.action is AnswerAction.EMERGENCY:
                    continue
                summary = "" if answer.action is AnswerAction.ABSTAIN else answer.summary
                summaries.append(TurnSummary(query=turn.query, answer_summary=summary))
        # Broad on purpose: context is an optimisation, and losing it degrades
        # retrieval quality. It must never stop a patient getting an answer.
        except Exception:
            return []
        return summaries[-limit:]

    def question_history(self, limit: int = DEFAULT_HISTORY_LIMIT) -> list[str]:
        """Prior patient QUESTIONS only, for generation context.

        Not the prior answers: an answer summary is not in the retrieved
        passages, so a model quoting one would cite evidence that was never
        retrieved and lose the claim to the verifier.

        Derived from :meth:`retrieval_history` so both views apply the same
        eligibility rules and the same window.
        """
        return [summary.query for summary in self.retrieval_history(limit)]

    def recent_escalation(self, limit: int = DEFAULT_ESCALATION_WINDOW) -> bool:
        """True if any of the last ``limit`` turns escalated. Never raises.

        Fails **closed**. Unlike the history above, where a fault costs quality,
        a fault here costs a red flag -- so an unreadable conversation escalates
        rather than staying silent.
        """
        try:
            return any(turn.escalated for turn in self.turns[-limit:])
        # Broad on purpose, and the return value is the point: see the docstring.
        except Exception:
            return True


__all__ = [
    "DEFAULT_ESCALATION_WINDOW",
    "DEFAULT_HISTORY_LIMIT",
    "Conversation",
    "ConversationTurn",
]

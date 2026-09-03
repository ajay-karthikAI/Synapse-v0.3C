"""
synapse.service.progress
========================
Progress reporting, as a closed enum and nothing else.

The interface needs to tell a patient that something is happening during the
several seconds a turn takes. That requirement is real, and it is also the
easiest place in the system to leak the one thing that must never leave it.

A reporter that accepted a string would eventually be called with one that
interpolates the query -- "Searching for chest pain..." is a natural thing to
write and a privacy incident (docs/privacy-logging-policy.md). So the callback
does not accept a string at all. It accepts a :class:`ProgressEvent`, which
carries a :class:`ProgressStage` and derives its message from
:data:`STAGE_MESSAGES`, a frozen table in this module.

That is the whole design: **there is no parameter through which query or answer
content could be supplied.** Not a convention, not a review item -- an absence
in the type. ``tests/test_service.py`` asserts the constructor stays that shape,
because the obvious future "improvement" is to add a ``detail`` field.

The messages are the exact strings ``app.py`` displayed before the extraction,
so the Streamlit fallback reads identically and the parity claim in
``docs/migration-parity.md`` is checkable rather than asserted.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class ProgressStage(StrEnum):
    """Every stage a turn can report, and no others.

    Closed on purpose. A caller cannot invent a stage, so the set of strings
    that can reach a progress indicator is fixed by this file and reviewable in
    one place.
    """

    CHECKING_QUESTION = "checking_question"  # Before anything runs, including the red-flag check
    LOADING_CORPUS = "loading_corpus"  # Reading the chunk store from disk
    VERIFYING_INDEX = "verifying_index"  # The index gate: manifest vs contents
    OPENING_INDEX = "opening_index"  # Loading a prebuilt index
    BUILDING_INDEX = "building_index"  # No prebuilt index was usable; building one
    UNDERSTANDING_QUESTION = "understanding_question"  # Follow-up rewrite for retrieval
    SEARCHING = "searching"  # Bounded candidate search
    RANKING = "ranking"  # One batched rerank request
    PREPARING_ANSWER = "preparing_answer"  # Generation, verification, display policy
    ANSWER_READY = "answer_ready"  # Terminal: an answer is renderable
    NO_ANSWER = "no_answer"  # Terminal: a typed failure is renderable


# The patient-facing copy for each stage. Application-owned constants: no model,
# provider or source contributes to any of these, and none of them interpolates
# anything. The wording is carried over verbatim from app.py so the Streamlit
# fallback is unchanged by the extraction.
STAGE_MESSAGES: Mapping[ProgressStage, str] = MappingProxyType(
    {
        ProgressStage.CHECKING_QUESTION: "Checking your question...",
        ProgressStage.LOADING_CORPUS: "Loading the medical research index...",
        ProgressStage.VERIFYING_INDEX: "Checking the research index...",
        ProgressStage.OPENING_INDEX: "Opening the research index...",
        ProgressStage.BUILDING_INDEX: "Preparing the medical research index...",
        ProgressStage.UNDERSTANDING_QUESTION: "Understanding your question...",
        ProgressStage.SEARCHING: "Searching relevant research...",
        ProgressStage.RANKING: "Ranking the strongest evidence...",
        ProgressStage.PREPARING_ANSWER: "Preparing your answer...",
        ProgressStage.ANSWER_READY: "Answer ready",
        ProgressStage.NO_ANSWER: "No answer available",
    }
)


@dataclass(frozen=True)
class ProgressEvent:
    """One stage transition.

    The single field is deliberate. :attr:`message` is *derived*, never
    supplied, so no caller -- including a future one written by someone who has
    not read this module -- can route free text through a progress update.
    """

    stage: ProgressStage

    @property
    def message(self) -> str:
        """The fixed copy for this stage."""
        return STAGE_MESSAGES[self.stage]

    @property
    def terminal(self) -> bool:
        """True for the two stages that end a turn."""
        return self.stage in (ProgressStage.ANSWER_READY, ProgressStage.NO_ANSWER)


# What a caller supplies to watch a turn progress. Returning anything is
# meaningless, so the return type is None: a reporter observes, it never steers.
ProgressReporter = Callable[[ProgressEvent], None]


def null_reporter(event: ProgressEvent) -> None:
    """The default reporter: does nothing, successfully.

    Making the no-op the default means every call site can report
    unconditionally, rather than guarding each one with a truthiness check that
    would eventually be forgotten on one branch.
    """


__all__ = [
    "STAGE_MESSAGES",
    "ProgressEvent",
    "ProgressReporter",
    "ProgressStage",
    "null_reporter",
]

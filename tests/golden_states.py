"""
Synthetic golden states: one deterministic turn per thing a patient can see.

Why this module exists
----------------------
There is about to be a second interface. A FastAPI process and a Next.js client
have to render every state the Streamlit fallback renders, and "every state" was
previously knowable only by reading a 1,900-line script and running it against a
paid API. This is that set, enumerated, built offline, and pinned to committed
golden files.

Three uses, in order of importance:

1. **A contract.** Phase 2 serialises these; Phase 3 and 4 render them. A state
   that is not here is a state nobody remembered to build a screen for.
2. **A regression gate.** ``tests/test_service_golden.py`` fails the build when
   what a patient sees changes, so the change is a diff in a pull request rather
   than something noticed after release.
3. **A safety argument.** Several states exist specifically to prove a negative:
   that model prose does not reach the page when validation fails, that a
   provider's exception text never escapes, and that the permanent disclaimer
   survives every path.

Everything here is **synthetic**. No case was written or reviewed by a
clinician, none is drawn from a real patient interaction, and none of it
evidences that the system is clinically correct -- only that it behaves the way
this repository says it does. It must never be cited as validation.

What is deliberately not covered
--------------------------------
Three of the six insufficient-evidence reasons -- ``failed_validation``,
``conflicting_sources`` and ``out_of_scope`` -- are **not reachable from a turn
today**. :mod:`synapse.evidence` defines them and can render them, but nothing
in the service decides them: ``app.py`` maps two failure codes plus abstention,
and that is all. They are goldened as *rendering* fixtures rather than turn
fixtures, and :data:`REACHABLE_FROM_A_TURN` records the distinction so a later
phase cannot mistake "we can draw this card" for "the system produces this
state".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from synapse.answer.providers import ProviderUnavailableError
from synapse.answer.schema import AnswerAction
from synapse.answer.verify import RetrievedEvidence
from synapse.errors import ArtifactIntegrityError
from synapse.evidence import InsufficientReason
from synapse.retrieval.evidence import RetrievalBundle
from synapse.service import (
    Conversation,
    ConversationTurn,
    ProgressEvent,
    ServiceConfig,
    SynapseService,
)
from synapse.service.index import LoadedIndex
from synapse.ui.errors import AnswerFailure, AnswerFailureCode
from synapse.ui.pipeline import TurnOutcome

# ---------------------------------------------------------------------------
# The fixed corpus every state is built over
# ---------------------------------------------------------------------------

CHUNK_A = "pubmed:41802233#0000"
CHUNK_B = "pubmed:41900001#0000"
SOURCE_A = "pubmed:41802233"
SOURCE_B = "pubmed:41900001"

TEXT_A = (
    "HbA1c reflects average plasma glucose over 2-3 months. "
    "A target below 7% is appropriate for most non-pregnant adults with diabetes."
)
TEXT_B = "Regular review supports earlier detection of complications."

EVIDENCE = RetrievedEvidence(
    chunk_texts={CHUNK_A: TEXT_A, CHUNK_B: TEXT_B},
    source_ids=frozenset({SOURCE_A, SOURCE_B}),
    source_titles={SOURCE_A: "HbA1c Targets in Adults", SOURCE_B: "Routine Diabetes Review"},
    source_urls={
        SOURCE_A: "https://pubmed.ncbi.nlm.nih.gov/41802233/",
        SOURCE_B: "https://pubmed.ncbi.nlm.nih.gov/41900001/",
    },
)

QUERY = "what does my HbA1c number actually mean?"
FOLLOW_UP = "what about the side effects?"

# What the follow-up rewrite resolved the question into. Model-generated text,
# so the renderer escapes it like any other dynamic value.
RESOLVED_QUERY = "what are the side effects of metformin for type 2 diabetes?"


def bundle(*, metadata: dict[str, object] | None = None) -> RetrievalBundle:
    """The standard two-source bundle every non-empty state retrieves."""
    return RetrievalBundle(
        evidence=EVIDENCE,
        source_order=[SOURCE_A, SOURCE_B],
        relevance={SOURCE_A: 0.91, SOURCE_B: 0.74},
        metadata=metadata or {"query_rewritten": False, "governed": True},
    )


def empty_bundle() -> RetrievalBundle:
    """Retrieval that returned nothing usable, so nothing can be grounded."""
    return RetrievalBundle(
        evidence=RetrievedEvidence(chunk_texts={}, source_ids=frozenset()),
        source_order=[],
        metadata={"query_rewritten": False, "governed": True},
    )


# ---------------------------------------------------------------------------
# Generation responses
# ---------------------------------------------------------------------------


def _claim(claim_id: str, text: str, source_id: str, chunk_id: str, quote: str) -> dict:
    return {
        "claim_id": claim_id,
        "text": text,
        "source_ids": [source_id],
        "supporting_excerpts": [{"source_id": source_id, "chunk_id": chunk_id, "quote": quote}],
    }


def _response(**overrides: object) -> str:
    """A schema-valid generation, with fields overridden per state."""
    payload: dict[str, object] = {
        "summary": "Your HbA1c reflects your average blood sugar over two to three months.",
        "claims": [
            _claim(
                "c1",
                "HbA1c reflects average plasma glucose over 2-3 months.",
                SOURCE_A,
                CHUNK_A,
                "HbA1c reflects average plasma glucose over 2-3 months",
            ),
            _claim(
                "c2",
                "Regular review supports earlier detection of complications.",
                SOURCE_B,
                CHUNK_B,
                "Regular review supports earlier detection of complications",
            ),
        ],
        "doctor_evaluation": "Your clinician will read it alongside your history.",
        "questions_for_doctor": [
            "What does my result mean for me specifically?",
            "How often should this be rechecked?",
        ],
        "limitations": [
            "This is general information from published research, not advice about your own care."
        ],
        "disclaimer": "model-supplied text the renderer ignores",
        "action": "answer",
    }
    payload.update(overrides)
    return json.dumps(payload)


# A claim whose quote is genuine but whose text overstates it: 7% in the source,
# 8% in the claim. The patient sees it MARKED, not silently corrected.
PARTIAL_RESPONSE = _response(
    summary="Targets vary between people.",
    claims=[
        _claim(
            "c1",
            "A target below 8% is appropriate for most adults.",
            SOURCE_A,
            CHUNK_A,
            "A target below 7%",
        )
    ],
    doctor_evaluation="Your clinician will set a target with you.",
    questions_for_doctor=["What target is right for me?"],
    limitations=["Targets are individual."],
)

# Every claim cites a quote that is not in the passage it names, so verification
# withholds all of them and the display policy abstains.
ABSTAIN_RESPONSE = _response(
    summary="This summary is replaced by the abstention wording.",
    claims=[
        _claim(
            "c1",
            "An assertion the sources do not carry.",
            SOURCE_A,
            CHUNK_A,
            "a quote that is nowhere in the source text",
        )
    ],
    doctor_evaluation="This is cleared on abstention.",
    limitations=[],
)

MEDICAL_STAFF_RESPONSE = _response(
    summary="",
    claims=[],
    doctor_evaluation="",
    questions_for_doctor=["Can someone look at this before my appointment?"],
    limitations=[],
    action="medical_staff",
)

# Free-form medical prose instead of JSON. This is the string the golden files
# prove NEVER reaches a patient: it is plausible, specific, dangerous, and
# entirely unvalidated.
MEDICAL_PROSE = (
    "Based on your symptoms you most likely have type 2 diabetes. "
    "Start metformin 500mg twice daily and reduce your carbohydrate intake."
)

# Valid JSON that does not satisfy the answer schema: a claim with no claim_id.
# The fail-closed case that must never be repaired into an answer.
#
# NOT simply a missing "claims" key -- that validates, because claims defaults to
# an empty list, and the turn becomes a zero-claim abstention rather than a
# schema failure. The distinction matters: an abstention is a statement about the
# evidence, a schema failure is a statement about the model, and conflating them
# would hide a broken generator behind a legitimate-looking state.
SCHEMA_INVALID = json.dumps(
    {"summary": "s", "action": "answer", "claims": [{"text": "no claim_id"}]}
)

# A provider message carrying infrastructure detail a patient must never see.
PROVIDER_SECRET = "sk-live-abc123 rejected by https://internal.example/v1/chat (request 9f2c)"


# ---------------------------------------------------------------------------
# Offline collaborators
# ---------------------------------------------------------------------------


class FakeClient:
    """A generation client returning a canned response, or raising."""

    def __init__(self, response: str = "", error: Exception | None = None) -> None:
        self.response = response or _response()
        self.error = error
        self.calls = 0
        self.last_prompt = ""

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        self.last_prompt = user
        if self.error is not None:
            raise self.error
        return self.response


class UnreachableIndex:
    """An index provider that must never be consulted.

    Every golden state overrides retrieval, so a call here means the override
    stopped working and the fixture is quietly exercising a different path.
    """

    def load(self, report: Callable[[ProgressEvent], None] = lambda _event: None) -> LoadedIndex:
        raise AssertionError("golden states must not touch the index")


CONFIG = ServiceConfig(
    api_key="test-key-not-a-real-credential",
    corpus_version="golden-corpus-v1",
    index_version="golden-index-v1",
)


def service(
    *,
    client: FakeClient | None = None,
    retriever: Callable[[str], RetrievalBundle] | None = None,
    is_emergency: Callable[[str], bool] = lambda _query: False,
) -> SynapseService:
    """A service wired entirely to fakes. No network, no key, no index."""
    return SynapseService(
        config=CONFIG,
        clients=_Clients(client or FakeClient()),
        index=UnreachableIndex(),
        is_emergency=is_emergency,
        retriever=retriever or (lambda _query: bundle()),
    )


@dataclass
class _Clients:
    """A ClientFactory that hands the same fake to both call sites."""

    client: FakeClient

    def for_answer(self) -> FakeClient:
        return self.client

    def for_rerank(self) -> FakeClient:
        return self.client


# ---------------------------------------------------------------------------
# The states
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenState:
    """One named state, with how it is produced and why it exists."""

    name: str
    description: str
    build: Callable[[], ConversationTurn]


def _turn(
    *,
    query: str = QUERY,
    client: FakeClient | None = None,
    retriever: Callable[[str], RetrievalBundle] | None = None,
    is_emergency: Callable[[str], bool] = lambda _query: False,
    conversation: Conversation | None = None,
) -> ConversationTurn:
    """Run one turn through the real service against fakes."""
    return service(client=client, retriever=retriever, is_emergency=is_emergency).ask(
        query, conversation or Conversation()
    )


def _raises(exc: Exception) -> Callable[[str], RetrievalBundle]:
    def _retrieve(_query: str) -> RetrievalBundle:
        raise exc

    return _retrieve


def _follow_up_turn() -> ConversationTurn:
    """A second turn whose retrieval query was rewritten from the first.

    The disclosure the interface shows ("Answering about ...") comes from the
    bundle metadata, nested so it cannot reach a log line. This state exists to
    pin that it survives the extraction and that the resolved query is escaped
    like any other model-generated value.
    """
    conversation = Conversation()
    first = service().ask(QUERY, conversation)
    assert first.answered  # The follow-up needs a turn to follow up on
    return _turn(
        query=FOLLOW_UP,
        retriever=lambda _query: bundle(
            metadata={
                "query_rewritten": True,
                "governed": True,
                "rewrite": {"resolved_query": RESOLVED_QUERY},
            }
        ),
        conversation=conversation,
    )


def _latched_emergency_turn() -> ConversationTurn:
    """A benign follow-up after an escalation, which the latch re-escalates.

    "is that serious?" carries no emergency vocabulary of its own. Without the
    latch the detector -- which sees one query at a time -- has nothing to fire
    on, and the patient who just described crushing chest pain gets a normal
    answer.
    """
    conversation = Conversation()
    first = _turn(
        query="crushing chest pain spreading to my arm",
        is_emergency=lambda _query: True,
        conversation=conversation,
    )
    assert first.escalated
    return _turn(query="is that serious?", conversation=conversation)


# Ordered so the golden directory reads as a walkthrough: the states a patient
# reaches normally, then the ones they reach when the system declines, then the
# ones they reach when it breaks.
STATES: tuple[GoldenState, ...] = (
    GoldenState(
        "answer",
        "Two verified claims, both fully supported. The ordinary successful turn.",
        lambda: _turn(),
    ),
    GoldenState(
        "partially_supported",
        "A genuine quote whose claim overstates it. Shown, and marked as partial.",
        lambda: _turn(client=FakeClient(PARTIAL_RESPONSE)),
    ),
    GoldenState(
        "abstain",
        "Every claim failed verification, so the display policy withheld all of them.",
        lambda: _turn(client=FakeClient(ABSTAIN_RESPONSE)),
    ),
    GoldenState(
        "medical_staff",
        "The model routed the question to a person rather than answering it.",
        lambda: _turn(client=FakeClient(MEDICAL_STAFF_RESPONSE)),
    ),
    GoldenState(
        "emergency",
        "A red flag on the patient's own words. No retrieval, no generation.",
        lambda: _turn(is_emergency=lambda _query: True),
    ),
    GoldenState(
        "emergency_latched",
        "A benign follow-up re-escalated by the latch, not by the detector.",
        _latched_emergency_turn,
    ),
    GoldenState(
        "follow_up_rewritten",
        "A follow-up whose retrieval query was resolved, and disclosed as such.",
        _follow_up_turn,
    ),
    GoldenState(
        "failure_evidence_unavailable",
        "Retrieval returned nothing usable. Rendered as insufficient evidence, not an error.",
        lambda: _turn(retriever=lambda _query: empty_bundle()),
    ),
    GoldenState(
        "failure_index_gate_raised",
        "The index did not match its manifest. Fail closed, and say so: this now "
        "carries index_unverified rather than being collapsed onto retrieval_failed.",
        lambda: _turn(retriever=_raises(ArtifactIntegrityError(artifact="hybrid_index"))),
    ),
    GoldenState(
        "failure_retrieval_failed",
        "The retrieval stage itself raised.",
        lambda: _turn(retriever=_raises(RuntimeError("faiss segfaulted"))),
    ),
    GoldenState(
        "failure_generation_unavailable",
        "The provider could not be reached. Its message carries a key and a URL.",
        lambda: _turn(client=FakeClient(error=ProviderUnavailableError(problem=PROVIDER_SECRET))),
    ),
    GoldenState(
        "failure_generation_invalid_json",
        "The model returned medical prose instead of JSON. The prose must not appear.",
        lambda: _turn(client=FakeClient(MEDICAL_PROSE)),
    ),
    GoldenState(
        "failure_generation_schema_invalid",
        "Valid JSON that does not satisfy the schema. Never repaired into an answer.",
        lambda: _turn(client=FakeClient(SCHEMA_INVALID)),
    ),
    GoldenState(
        "failure_internal_error",
        "An unclassified exception during generation, reduced to its type name.",
        lambda: _turn(client=FakeClient(error=ValueError(PROVIDER_SECRET))),
    ),
)

STATES_BY_NAME: dict[str, GoldenState] = {state.name: state for state in STATES}

# The configuration failure is not produced by a turn: it is raised when the
# service is CONSTRUCTED without a credential, so app.py renders it before any
# question is asked. Represented directly, so the eight-code set is complete.
CONFIGURATION_FAILURE = AnswerFailure(AnswerFailureCode.CONFIGURATION_ERROR)

# Two of the eight failure codes cannot be produced by a turn today. Recorded
# here, with the reason, so the coverage test asserts a PARTITION rather than
# quietly passing on a set that happens to be smaller than the enum.
#
# CONFIGURATION_ERROR
#     Raised at construction, before a turn exists. app.py catches
#     MissingCredentialError and renders this code itself.
#
# INDEX_UNVERIFIED -- CLOSED IN PHASE 6, and no longer in this set.
#     History, because it explains the shape of the fix. `classify` mapped every
#     artifact-integrity error to this code and `synapse.service.index` failed
#     the index gate precisely so it would be produced -- but no TURN could
#     produce it, because `answer_turn` wrapped the retrieval stage in a handler
#     that returned a hard-coded RETRIEVAL_FAILED and never called `classify`.
#     An unverifiable index therefore reached a patient as the generic
#     "something went wrong" card, and reached an operator as an ordinary
#     retrieval error, making an integrity failure indistinguishable from a
#     transient one.
#
#     Phase 2 gave the code a producer but not a turn:
#     `synapse.runtime.readiness` reports it when the deployed artifact cannot be
#     proven intact, which is a startup gate rather than a turn.
#
#     Phase 6 closed the turn path. The retrieval handler now preserves exactly
#     this one code from `classify` and leaves every other exception on
#     RETRIEVAL_FAILED, so `failure_index_gate_raised` renders through
#     EVIDENCE_FAILURE_CODES as an insufficient-evidence state naming the reason.
UNREACHABLE_CODES: frozenset[AnswerFailureCode] = frozenset(
    {
        AnswerFailureCode.CONFIGURATION_ERROR,
    }
)

# Which insufficient-evidence reasons the service can actually decide today.
# The other three are renderable but unreachable; see the module docstring.
REACHABLE_FROM_A_TURN: frozenset[InsufficientReason] = frozenset(
    {
        InsufficientReason.NO_ELIGIBLE_EVIDENCE,  # via the evidence_unavailable failure code
        InsufficientReason.INDEX_UNVERIFIED,  # via the index_unverified failure code
        InsufficientReason.BELOW_THRESHOLD,  # via an abstention
    }
)


def failure_code(turn: ConversationTurn) -> str:
    """The typed code on a failed turn, or "" when the turn produced an answer."""
    failure = turn.outcome.failure
    return failure.code.value if failure is not None else ""


def action_of(turn: ConversationTurn) -> AnswerAction | None:
    """The action state, or None for a failed turn."""
    return turn.action


def brief_eligible(outcome: TurnOutcome) -> bool:
    """Whether this outcome offers an appointment brief.

    Mirrors ``app.py``: a brief needs an answer, and an escalation has none --
    nothing was retrieved and nothing was generated, so there is no takeaway to
    build. Abstention DOES offer one: the questions survive even when the claims
    do not.
    """
    if outcome.presentation is None:
        return False
    return outcome.presentation.answer.action is not AnswerAction.EMERGENCY


__all__ = [
    "CONFIGURATION_FAILURE",
    "EVIDENCE",
    "FOLLOW_UP",
    "MEDICAL_PROSE",
    "PROVIDER_SECRET",
    "QUERY",
    "REACHABLE_FROM_A_TURN",
    "RESOLVED_QUERY",
    "STATES",
    "STATES_BY_NAME",
    "UNREACHABLE_CODES",
    "FakeClient",
    "GoldenState",
    "UnreachableIndex",
    "action_of",
    "brief_eligible",
    "bundle",
    "empty_bundle",
    "failure_code",
    "service",
]

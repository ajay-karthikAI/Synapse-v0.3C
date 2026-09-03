"""
One turn, streamed.

The route is thin on purpose. Everything that decides *what* a patient sees is
below it — :mod:`synapse.service` for ordering,
:mod:`synapse.ui.pipeline` for the safety invariants,
:mod:`synapse.api.envelopes` for the wire mapping. This module's job is the
three things HTTP adds:

**Readiness.** ``ReadyDep`` refuses before any work when the deployment has not
passed its startup gates. This is Phase 2's readiness becoming an enforcement
point: an unverified artifact must not answer a question, and the refusal
carries the readiness failure code rather than a generic 503.

**Idempotency.** A retried ``client_request_id`` replays the stored envelope
without running a second turn. That is not merely an optimisation: a second turn
would consume a slot against the twenty-turn ceiling, cost another model call,
and — because the emergency latch reads the conversation — could change a safety
verdict on a request the client believes it already made.

**One turn at a time.** ``begin_turn`` is a compare-and-set. Two concurrent
turns on one session would interleave their writes to the follow-up history and
the latch, and the latch is a safety control.

The session is released by the worker, not by this handler
(:mod:`synapse.api.sse`), so a client that disconnects mid-turn does not free a
session that is still working.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable, Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from synapse.api.deps import (
    CODE_SESSION_BUSY,
    CODE_TURN_LIMIT,
    ApiError,
    ReadyDep,
    SessionDep,
)
from synapse.api.envelopes import envelope_for
from synapse.api.models import StreamedTurnEnvelope, TurnRequest
from synapse.api.sessions import SessionBusy, SessionTurnLimit
from synapse.api.sse import (
    EVENT_ENVELOPE,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    format_event,
    stream_turn,
)
from synapse.logging import get_logger
from synapse.service.conversation import ConversationTurn
from synapse.service.progress import ProgressEvent

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/turns", tags=["turns"])


@router.post(
    "/stream",
    summary="Ask one question, streamed",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "An SSE stream: zero or more `stage` events, then EXACTLY ONE "
                "`envelope` event whose `data` is the schema below. No medical "
                "content is emitted before validation and verification finish."
            ),
            # Declared so the four envelope schemas reach the OpenAPI document.
            # The body is SSE-framed; this describes the terminal event's payload.
            "model": StreamedTurnEnvelope,
            "content": {SSE_MEDIA_TYPE: {}},
        }
    },
)
def stream(payload: TurnRequest, session: SessionDep, state: ReadyDep) -> StreamingResponse:
    """Run one turn and stream its progress, then its single envelope."""
    replayed = session.replay(payload.client_request_id)
    if replayed is not None:
        logger.info("replaying idempotent turn", extra={"session_id": session.session_id})
        return _stream_once(replayed)

    try:
        session.begin_turn()
    except SessionBusy:
        raise ApiError(
            409, CODE_SESSION_BUSY, "A question is already being answered on this session."
        ) from None
    except SessionTurnLimit:
        raise ApiError(
            409, CODE_TURN_LIMIT, "This session has reached its limit of questions."
        ) from None

    service = state.service
    if service is None:  # pragma: no cover - ReadyDep guarantees one
        session.end_turn()
        raise ApiError(503, "not_ready", "The service is not ready to answer questions.")

    # Fixed BEFORE the turn runs, so the envelope's index matches the position
    # this turn will occupy even though the conversation is appended to later.
    turn_index = session.turn_count

    def run(report: Callable[[ProgressEvent], None]) -> ConversationTurn:
        return service.ask(payload.query, session.conversation, report=report)

    def to_envelope(turn: ConversationTurn) -> object:
        envelope = envelope_for(turn, turn_index=turn_index)
        # Stored so a retry replays rather than re-runs. Recorded after the turn
        # completes, so an interrupted turn is retried rather than replayed.
        session.remember(payload.client_request_id, envelope.model_dump(mode="json"))
        return envelope

    return StreamingResponse(
        stream_turn(run=run, to_envelope=to_envelope, on_finished=session.end_turn),
        media_type=SSE_MEDIA_TYPE,
        headers=dict(SSE_HEADERS),
    )


def _stream_once(envelope: object) -> StreamingResponse:
    """Emit a stored envelope as a complete one-event stream.

    Same shape as a live turn — a client parses one code path — but with no
    stages, because nothing ran.
    """

    def body() -> Iterator[str]:
        yield format_event(EVENT_ENVELOPE, envelope)

    return StreamingResponse(body(), media_type=SSE_MEDIA_TYPE, headers=dict(SSE_HEADERS))


__all__ = ["router"]

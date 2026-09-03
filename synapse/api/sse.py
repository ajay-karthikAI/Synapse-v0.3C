"""
synapse.api.sse
===============
Streaming one turn: stages while it works, exactly one envelope at the end.

What is streamed, and what is not
---------------------------------
**Stages are streamed. Medical content is not.** A stage event carries a closed
:class:`~synapse.service.progress.ProgressStage` and the fixed message from that
module's table — there is no parameter through which a query or an answer could
reach it (``synapse.service.progress`` makes that structural, not a convention).

Nothing medical is emitted until the turn is *complete*: retrieved, generated,
verified, and passed through the display policy. That is not a performance
compromise, it is the requirement. Token-by-token streaming would put
unverified model prose on the page and then retract it, and "the patient briefly
saw a claim we later withheld" is precisely the failure the answer layer exists
to prevent. So the envelope arrives whole, once, at the end.

Why a thread
------------
:meth:`~synapse.service.service.SynapseService.ask` is synchronous and blocking.
Running it in a worker lets the generator emit heartbeats while it waits, which
is what keeps a proxy from closing an idle connection mid-turn. The worker
communicates through a queue; the generator never touches service state.

The worker is a daemon and cannot be killed. So it — not the request — owns
releasing the session: whether the client disconnects, the deadline fires, or
the turn simply finishes, ``finally`` runs in the worker and the session becomes
available again exactly when the work actually stops. A request that gives up
does not free a session that is still doing work.
"""

from __future__ import annotations  # Postponed annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from synapse.api.config import HEARTBEAT_SECONDS, TURN_DEADLINE_SECONDS
from synapse.api.models import FailureEnvelope
from synapse.logging import get_logger
from synapse.service.conversation import ConversationTurn
from synapse.service.progress import ProgressEvent
from synapse.ui.errors import PATIENT_ERROR_MESSAGE, AnswerFailureCode

logger = get_logger(__name__)

# SSE event names. Closed set; a client switches on these.
EVENT_STAGE = "stage"
EVENT_ENVELOPE = "envelope"

# The media type, with the parameter that stops a proxy from buffering.
SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",  # nginx: stream through rather than buffer
    "Connection": "keep-alive",
}


def format_event(event: str, payload: object) -> str:
    """One SSE frame. ``data`` is compact JSON on a single line.

    Newlines inside a payload would be read as field separators and split one
    event into several, so the separators are stripped by compact encoding.
    """
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def format_heartbeat() -> str:
    """An SSE comment. Keeps the connection open; ignored by every client."""
    return ": heartbeat\n\n"


@dataclass
class _Result:
    """What the worker produced, read only after it signals completion."""

    turn: ConversationTurn | None = None
    error: BaseException | None = None


def stream_turn(
    *,
    run: Callable[[Callable[[ProgressEvent], None]], ConversationTurn],
    to_envelope: Callable[[ConversationTurn], object],
    on_finished: Callable[[], None],
    deadline_seconds: float = TURN_DEADLINE_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> Iterator[str]:
    """Yield SSE frames for one turn: stages, heartbeats, then one envelope.

    Args:
        run: performs the turn, calling the supplied reporter as it progresses.
        to_envelope: converts the completed turn into its wire envelope.
        on_finished: released by the WORKER when the turn stops, however it
            stops. See the module docstring.

    Exactly one ``envelope`` event is emitted on every path — success, failure,
    exception, or deadline. A client that received stages and no envelope would
    have no way to tell "still working" from "gave up".
    """
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    result = _Result()

    def report(event: ProgressEvent) -> None:
        # Stage and fixed message only. Nothing else can be put here.
        events.put((EVENT_STAGE, {"stage": event.stage.value, "message": event.message}))

    def worker() -> None:
        try:
            result.turn = run(report)
        # Broad on purpose: an escaping exception must become a typed envelope,
        # never a truncated stream or a stack trace on the wire.
        except BaseException as exc:
            result.error = exc
        finally:
            # The worker owns the release. See the module docstring.
            try:
                on_finished()
            # Broad on purpose: failing to release must not mask the outcome.
            except Exception:
                logger.error("failed to release session after turn")
            events.put(("__done__", None))

    thread = threading.Thread(target=worker, name="synapse-turn", daemon=True)
    thread.start()

    expires_at = time.monotonic() + deadline_seconds
    while True:
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            logger.warning("turn exceeded its deadline")
            yield format_event(EVENT_ENVELOPE, _timeout_envelope())
            return
        try:
            name, payload = events.get(timeout=min(heartbeat_seconds, remaining))
        except queue.Empty:
            yield format_heartbeat()
            continue

        if name == "__done__":
            break
        yield format_event(name, payload)

    if result.error is not None:
        logger.warning("turn raised", extra={"error": type(result.error).__name__})
        yield format_event(EVENT_ENVELOPE, _internal_envelope())
        return

    if result.turn is None:  # pragma: no cover - worker sets one or the other
        yield format_event(EVENT_ENVELOPE, _internal_envelope())
        return

    yield format_event(EVENT_ENVELOPE, _dump(to_envelope(result.turn)))


def _dump(envelope: object) -> object:
    """Serialise a pydantic envelope, or pass through what is already plain."""
    dump = getattr(envelope, "model_dump", None)
    return dump(mode="json") if callable(dump) else envelope


def _timeout_envelope() -> object:
    """The envelope for a turn that ran out of time.

    ``internal_error`` rather than a new code. :class:`AnswerFailureCode` is a
    closed enum whose values each correspond to a different operator response,
    and Phase 1 fixed its membership; inventing a code here would change the
    contract from a streaming module. The deadline is visible in the logs as
    "turn exceeded its deadline", which is where an operator would look.
    """
    return _dump(
        FailureEnvelope(
            code=AnswerFailureCode.INTERNAL_ERROR.value,
            message=PATIENT_ERROR_MESSAGE,
            turn_index=0,
        )
    )


def _internal_envelope() -> object:
    """The envelope for an exception that escaped the service."""
    return _dump(
        FailureEnvelope(
            code=AnswerFailureCode.INTERNAL_ERROR.value,
            message=PATIENT_ERROR_MESSAGE,
            turn_index=0,
        )
    )


__all__ = [
    "EVENT_ENVELOPE",
    "EVENT_STAGE",
    "SSE_HEADERS",
    "SSE_MEDIA_TYPE",
    "format_event",
    "format_heartbeat",
    "stream_turn",
]

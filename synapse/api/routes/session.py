"""
Reading and discarding session state.

``GET`` returns counts and clocks, never content. A session's conversation is
not retrievable through the API at all: the client already has what it was sent,
and an endpoint that replayed a whole conversation would turn a stolen token
into a transcript.

``DELETE`` is the explicit end of a conversation. Nothing is archived, because
there is nowhere to archive it to (docs/PRIVACY_DATA_FLOW.md §2.4).
"""

from __future__ import annotations  # Postponed annotations

from fastapi import APIRouter

from synapse.api.config import MAX_TURNS_PER_SESSION, SESSION_IDLE_TTL_SECONDS
from synapse.api.deps import SessionDep, StateDep
from synapse.api.models import DeletedResponse, SessionResponse

router = APIRouter(prefix="/v1/session", tags=["session"])


@router.get("", response_model=SessionResponse, summary="Session state")
def read_session(session: SessionDep) -> SessionResponse:
    """Counts and clocks only. No conversation content, ever."""
    return SessionResponse(
        session_id=session.session_id,
        turn_count=session.turn_count,
        max_turns=MAX_TURNS_PER_SESSION,
        turn_active=session.turn_active,
        idle_ttl_seconds=int(SESSION_IDLE_TTL_SECONDS),
    )


@router.delete("", response_model=DeletedResponse, summary="Discard the session")
def delete_session(session: SessionDep, state: StateDep) -> DeletedResponse:
    """Forget everything held for this session."""
    return DeletedResponse(deleted=state.sessions.delete(session.session_id))

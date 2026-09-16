"""
synapse.api.sessions
====================
In-memory sessions: bounded, thread-safe, and never persisted.

One process, one dictionary, one lock. There is no database and no cache server,
which is a privacy decision before it is an architectural one: a conversation
that is never written down cannot be subpoenaed, leaked from a backup, or found
in a snapshot six months later. The cost is that a restart ends every
conversation, and that is accepted (docs/PRIVACY_DATA_FLOW.md §2.4).

Everything here is bounded, because every unbounded thing is a way to exhaust
the process:

* sessions expire after two hours of **inactivity**, not two hours of existence;
* a session holds at most twenty turns;
* the store holds at most :data:`~synapse.api.config.MAX_SESSIONS` sessions, and
  when full it evicts the least recently used rather than refusing new ones;
* idempotency records are capped per session.

Concurrency
-----------
The store's lock guards the dictionary. Each session carries its **own** lock,
guarding its conversation and its brief. That split matters: a turn takes
seconds, and holding the store lock for its duration would serialise every
request in the process.

``begin_turn`` is the one-active-turn gate, and it is a compare-and-set under
the session lock rather than a check followed by a set. A second concurrent turn
on the same session is refused rather than queued, because two turns
interleaving would corrupt the follow-up history and the emergency latch — and
the latch is a safety control.

What a session deliberately does not hold
-----------------------------------------
No passcode, no token, no client address, no user identity. A session is a
conversation and a clock. The identifier is random and names nothing.
"""

from __future__ import annotations  # Postponed annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from synapse.api.config import (
    MAX_SESSIONS,
    MAX_TURNS_PER_SESSION,
    SESSION_IDLE_TTL_SECONDS,
)
from synapse.brief.schema import AppointmentBrief
from synapse.logging import get_logger
from synapse.service.conversation import Conversation

logger = get_logger(__name__)

# How many completed turns keep a replayable result. Small: idempotency protects
# against a retried request, not against a client replaying an hour later.
MAX_IDEMPOTENCY_RECORDS = 8


class SessionError(RuntimeError):
    """Base for the refusals a caller must render as a typed response."""


class SessionNotFound(SessionError):
    """No such session, or it expired. The two are indistinguishable on purpose."""


class SessionBusy(SessionError):
    """A turn is already running on this session."""


class SessionTurnLimit(SessionError):
    """The session has reached its turn ceiling."""


@dataclass
class Session:
    """One conversation, its briefs, and its clocks.

    Not frozen: this is mutable state by nature. Every mutation goes through a
    method that holds :attr:`lock`.
    """

    session_id: str
    created_at: float
    last_seen: float
    conversation: Conversation = field(default_factory=Conversation)
    # Briefs are keyed by turn index and owned by the SERVER. A client edits
    # them through named operations; it never supplies one. See
    # synapse.api.routes.brief for why that matters.
    briefs: dict[int, AppointmentBrief] = field(default_factory=dict)
    # The conversation-wide brief: ONE document spanning every turn, which is
    # what a patient carries to an appointment. Rebuilt as turns are added,
    # keeping its document id and the patient's own edits. Server-owned on the
    # same terms as the per-turn briefs above.
    conversation_brief: AppointmentBrief | None = None
    # client_request_id -> the envelope that request already produced.
    _idempotency: OrderedDict[str, object] = field(default_factory=OrderedDict)
    _turn_active: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def turn_count(self) -> int:
        """Turns recorded so far."""
        return len(self.conversation.turns)

    def touch(self, now: float) -> None:
        """Mark activity. Idle expiry is measured from this."""
        self.last_seen = now

    def expired(self, now: float, ttl: float = SESSION_IDLE_TTL_SECONDS) -> bool:
        """True when this session has been idle longer than ``ttl``."""
        return now - self.last_seen > ttl

    def begin_turn(self) -> None:
        """Claim the session for one turn.

        Compare-and-set under the lock: two concurrent requests cannot both
        observe "not busy" and proceed.

        Raises:
            SessionBusy: a turn is already running.
            SessionTurnLimit: the session is full.
        """
        with self.lock:
            if self._turn_active:
                raise SessionBusy(self.session_id)
            if self.turn_count >= MAX_TURNS_PER_SESSION:
                raise SessionTurnLimit(self.session_id)
            self._turn_active = True

    def end_turn(self) -> None:
        """Release the session. Safe to call even if no turn was active."""
        with self.lock:
            self._turn_active = False

    @property
    def turn_active(self) -> bool:
        """True while a turn holds this session."""
        with self.lock:
            return self._turn_active

    def remember(self, client_request_id: str, envelope: object) -> None:
        """Record what a request produced, so a retry replays it."""
        if not client_request_id:
            return
        with self.lock:
            self._idempotency[client_request_id] = envelope
            self._idempotency.move_to_end(client_request_id)
            while len(self._idempotency) > MAX_IDEMPOTENCY_RECORDS:
                self._idempotency.popitem(last=False)

    def replay(self, client_request_id: str) -> object | None:
        """The envelope this request already produced, or ``None``.

        A retried request must not run a second turn: it would cost a second
        model call, consume a slot against the turn ceiling, and — because the
        emergency latch reads the conversation — could change a safety verdict.
        """
        if not client_request_id:
            return None
        with self.lock:
            return self._idempotency.get(client_request_id)

    def clear(self) -> None:
        """Forget the conversation, keeping the session itself alive.

        This is what "Clear this conversation" means, and it is deliberately
        NOT :meth:`SessionStore.delete`.

        The access cookie is a JWT naming ONE session id for its full eight
        hours, and a session is created in exactly one place -- at login. So
        destroying the session left the caller holding a valid token pointing at
        something that no longer existed; the next request failed
        ``SessionNotFound``, which is reported as 401 (indistinguishable from a
        forged token, on purpose). Clearing the conversation therefore signed
        the patient out and told them their session had expired, with no way
        back except the passcode.

        Everything a patient would recognise as their conversation goes: the
        turns, the briefs built from them, and the idempotency record that could
        replay a previous answer. The identity and the clocks stay, because they
        are what the cookie is bound to. Nothing is archived -- there is nowhere
        to archive it to.

        ``created_at`` is NOT reset: the eight-hour cookie keeps running, and
        pretending a cleared session is a new one would extend an access grant
        by clearing the screen.
        """
        with self.lock:
            self.conversation = Conversation()
            self.briefs.clear()
            self.conversation_brief = None
            self._idempotency.clear()
            self._turn_active = False

    def set_brief(self, turn_index: int, brief: AppointmentBrief) -> None:
        """Store the server-owned brief for a turn."""
        with self.lock:
            self.briefs[turn_index] = brief

    def get_brief(self, turn_index: int) -> AppointmentBrief | None:
        """The server-owned brief for a turn, if one has been built."""
        with self.lock:
            return self.briefs.get(turn_index)

    def set_conversation_brief(self, brief: AppointmentBrief) -> None:
        """Store the conversation-wide brief."""
        with self.lock:
            self.conversation_brief = brief

    def get_conversation_brief(self) -> AppointmentBrief | None:
        """The conversation-wide brief, if one has been built."""
        with self.lock:
            return self.conversation_brief


class SessionStore:
    """A bounded, thread-safe map of session id to :class:`Session`."""

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        idle_ttl_seconds: float = SESSION_IDLE_TTL_SECONDS,
    ) -> None:
        self._max_sessions = max_sessions
        self._idle_ttl = idle_ttl_seconds
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, Session] = OrderedDict()

    def create(self, session_id: str, *, now: float | None = None) -> Session:
        """Register a new session, evicting the least recently used if full."""
        moment = now if now is not None else time.monotonic()
        session = Session(session_id=session_id, created_at=moment, last_seen=moment)
        with self._lock:
            self._purge(moment)
            while len(self._sessions) >= self._max_sessions:
                # Evict rather than refuse: a full store must not become a
                # denial of service against the next legitimate patient.
                evicted, _ = self._sessions.popitem(last=False)
                logger.info("session evicted for capacity", extra={"session_id": evicted})
            self._sessions[session_id] = session
            self._sessions.move_to_end(session_id)
        logger.info("session created", extra={"session_id": session_id})
        return session

    def get(self, session_id: str, *, now: float | None = None) -> Session:
        """Return a live session and mark it active.

        Raises:
            SessionNotFound: unknown or expired. The caller must not
                distinguish the two — "that id existed but timed out" tells an
                attacker their guess was right.
        """
        moment = now if now is not None else time.monotonic()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            if session.expired(moment, self._idle_ttl):
                del self._sessions[session_id]
                logger.info("session expired", extra={"session_id": session_id})
                raise SessionNotFound(session_id)
            session.touch(moment)
            self._sessions.move_to_end(session_id)
            return session

    def delete(self, session_id: str) -> bool:
        """Forget a session. Returns True if one was removed.

        The explicit end of a conversation. Nothing is archived, because there
        is nowhere to archive it to.
        """
        with self._lock:
            removed = self._sessions.pop(session_id, None)
        if removed is not None:
            logger.info("session deleted", extra={"session_id": session_id})
        return removed is not None

    def purge(self, *, now: float | None = None) -> int:
        """Drop every expired session. Returns how many went."""
        moment = now if now is not None else time.monotonic()
        with self._lock:
            return self._purge(moment)

    def _purge(self, now: float) -> int:
        """Caller holds the lock."""
        stale = [
            key for key, session in self._sessions.items() if session.expired(now, self._idle_ttl)
        ]
        for key in stale:
            del self._sessions[key]
        return len(stale)

    def __len__(self) -> int:
        """How many sessions are held, expired ones included until purged."""
        with self._lock:
            return len(self._sessions)


__all__ = [
    "MAX_IDEMPOTENCY_RECORDS",
    "Session",
    "SessionBusy",
    "SessionError",
    "SessionNotFound",
    "SessionStore",
    "SessionTurnLimit",
]

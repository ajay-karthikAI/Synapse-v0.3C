"""
synapse.api.deps
================
Shared state and the two gates every protected route passes through.

Two independent credentials, and **both** are required:

1. **The service token** proves the request came from the Next.js server proxy.
   The browser never holds it. This is what makes the backend private even
   though it is reachable on the internet.
2. **The access token** proves someone typed the shared passcode. It carries a
   random session id and nothing else.

Requiring both means neither alone is enough: a leaked access token is useless
without the service token, and the service token alone reaches no conversation.

Failures are deliberately uniform
---------------------------------
Every rejection is ``401`` with the same body shape and a typed code. Missing
token, wrong token, expired token, unknown session, expired session — a caller
cannot tell them apart, because a caller that could would use the endpoint as an
oracle for which session ids exist.

Nothing here logs a token, a passcode, an address, or a query. What is logged is
the *fact* of a rejection and its code.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from synapse.api.config import ApiSettings
from synapse.api.security import constant_time_equals, decode_access_token
from synapse.api.sessions import Session, SessionNotFound, SessionStore
from synapse.logging import get_logger
from synapse.runtime.readiness import Readiness, ReadinessState
from synapse.service.service import SynapseService

logger = get_logger(__name__)

# Header the proxy sets. Not "Authorization": that one carries the access token,
# and putting two different credentials in one header invites confusing them.
SERVICE_TOKEN_HEADER = "X-Service-Token"  # noqa: S105 - a header NAME, not a secret
ACCESS_COOKIE_NAME = "synapse_access"

# Typed refusal codes. Closed set; the client renders fixed copy per code.
CODE_UNAUTHORIZED = "unauthorized"
CODE_NOT_READY = "not_ready"
CODE_SESSION_BUSY = "session_busy"
CODE_TURN_LIMIT = "turn_limit"
CODE_NOT_FOUND = "not_found"
CODE_INVALID_REQUEST = "invalid_request"
CODE_RATE_LIMITED = "rate_limited"


class ApiError(Exception):
    """A refusal to be rendered as a typed JSON body.

    Carries a status, a closed code and a fixed message. There is no field for
    an exception's text, so no handler can accidentally forward one.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message


# The one message every authentication failure produces. Uniform on purpose.
UNAUTHORIZED_MESSAGE = "Not authorised."


@dataclass
class ApiState:
    """Everything a request may need, assembled once at startup.

    ``service`` is optional because a process that failed its startup gates has
    no service to serve with — and must say so rather than half-working.
    """

    settings: ApiSettings
    sessions: SessionStore
    limiter: object  # LoginLimiter; typed loosely to keep the import graph flat
    source_pack: Path
    service: SynapseService | None = None
    readiness: Readiness = field(default_factory=lambda: Readiness(state=ReadinessState.STARTING))
    evaluation_dir: Path = Path("artifacts/evals")

    @property
    def ready(self) -> bool:
        """True only when startup completed AND a service exists to serve with."""
        return self.readiness.ready and self.service is not None


def get_state(request: Request) -> ApiState:
    """The process-wide state, attached at app creation."""
    state = getattr(request.app.state, "synapse", None)
    if state is None:  # pragma: no cover - create_app always sets it
        raise ApiError(500, CODE_NOT_READY, "Service is not configured.")
    return state


StateDep = Annotated[ApiState, Depends(get_state)]


def require_service_token(request: Request, state: StateDep) -> None:
    """Gate 1: the request came from the trusted proxy.

    Constant-time comparison. A missing header is compared against the
    configured token anyway — against an empty string — so the timing of a
    missing header and a wrong one do not differ in a way worth measuring.
    """
    supplied = request.headers.get(SERVICE_TOKEN_HEADER, "")
    if not constant_time_equals(supplied, state.settings.service_token):
        logger.warning("service token rejected")
        raise ApiError(401, CODE_UNAUTHORIZED, UNAUTHORIZED_MESSAGE)


ServiceTokenDep = Annotated[None, Depends(require_service_token)]


def access_token_from(request: Request) -> str:
    """The access token, from the cookie or the Authorization header.

    The cookie is what the browser sends through the proxy; the header exists so
    the API can be exercised directly by a test or an operator.
    """
    cookie = request.cookies.get(ACCESS_COOKIE_NAME, "")
    if cookie:
        return cookie
    authorization = request.headers.get("Authorization", "")
    prefix = "Bearer "
    return authorization[len(prefix) :] if authorization.startswith(prefix) else ""


def require_session(request: Request, state: StateDep, _service_token: ServiceTokenDep) -> Session:
    """Gate 2: a valid access token naming a live session.

    Depends on :func:`require_service_token`, so a route that asks for a session
    gets both gates and cannot accidentally get only one.
    """
    token = access_token_from(request)
    if not token:
        raise ApiError(401, CODE_UNAUTHORIZED, UNAUTHORIZED_MESSAGE)
    session_id = decode_access_token(token, state.settings.jwt_secret)
    if session_id is None:
        logger.info("access token rejected")
        raise ApiError(401, CODE_UNAUTHORIZED, UNAUTHORIZED_MESSAGE)
    try:
        return state.sessions.get(session_id)
    except SessionNotFound:
        # Indistinguishable from a bad token, on purpose.
        logger.info("session not found for token")
        raise ApiError(401, CODE_UNAUTHORIZED, UNAUTHORIZED_MESSAGE) from None


SessionDep = Annotated[Session, Depends(require_session)]


def require_ready(state: StateDep) -> ApiState:
    """Refuse work when the deployment has not passed its startup gates.

    The enforcement point for Phase 2's readiness. Serving a turn from an
    unverified artifact is exactly what the artifact layer exists to prevent, so
    a request arriving before readiness is refused with the readiness failure
    code rather than attempted.
    """
    if not state.ready:
        code = (
            state.readiness.failure_code.value
            if state.readiness.failure_code is not None
            else CODE_NOT_READY
        )
        raise ApiError(503, code, "The service is not ready to answer questions.")
    return state


ReadyDep = Annotated[ApiState, Depends(require_ready)]


__all__ = [
    "ACCESS_COOKIE_NAME",
    "CODE_INVALID_REQUEST",
    "CODE_NOT_FOUND",
    "CODE_NOT_READY",
    "CODE_RATE_LIMITED",
    "CODE_SESSION_BUSY",
    "CODE_TURN_LIMIT",
    "CODE_UNAUTHORIZED",
    "SERVICE_TOKEN_HEADER",
    "UNAUTHORIZED_MESSAGE",
    "ApiError",
    "ApiState",
    "ReadyDep",
    "ServiceTokenDep",
    "SessionDep",
    "StateDep",
    "access_token_from",
    "get_state",
    "require_ready",
    "require_service_token",
    "require_session",
]

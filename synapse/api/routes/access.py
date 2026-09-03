"""
The access gate: one shared passcode, exchanged for an eight-hour token.

The order of operations here is the security property, and it is not the
obvious one:

1. **Service token first.** An unauthenticated caller cannot reach the passcode
   comparison at all, so the login endpoint is not an open oracle.
2. **Lockout check before comparison.** A locked-out address is refused without
   the passcode being examined, so a lockout cannot be probed for timing.
3. **Constant-time comparison.** Never ``==``.
4. **Failures counted, successes cleared.** A correct passcode consumes no
   budget, so one person's typos cannot lock out the next patient sharing a
   clinic address.

The rate-limit key comes from :func:`~synapse.api.security.client_address_from`,
which counts proxy hops from the right. A client that prepends
``X-Forwarded-For`` entries cannot choose its own bucket.

Nothing here logs the passcode, the address, or whether a given attempt was
close. The token is returned in the body *and* set as an HttpOnly cookie: the
cookie is what a browser uses through the proxy, the body is what a server-side
caller uses.
"""

from __future__ import annotations  # Postponed annotations

from fastapi import APIRouter, Request, Response

from synapse.api.config import ACCESS_TOKEN_TTL_SECONDS
from synapse.api.deps import (
    ACCESS_COOKIE_NAME,
    CODE_RATE_LIMITED,
    CODE_UNAUTHORIZED,
    UNAUTHORIZED_MESSAGE,
    ApiError,
    ServiceTokenDep,
    StateDep,
    access_token_from,
)
from synapse.api.models import DeletedResponse, LoginRequest, LoginResponse
from synapse.api.security import (
    LoginLimiter,
    client_address_from,
    constant_time_equals,
    decode_access_token,
    issue_access_token,
    new_session_id,
)
from synapse.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/access", tags=["access"])


def _client_address(request: Request, hops: int) -> str:
    """The rate-limit key for this request."""
    fallback = request.client.host if request.client else "unknown"
    return client_address_from(
        request.headers.get("x-forwarded-for"), trusted_hops=hops, fallback=fallback
    )


@router.post("/login", response_model=LoginResponse, summary="Exchange the passcode")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    state: StateDep,
    _service_token: ServiceTokenDep,
) -> LoginResponse:
    """Verify the shared passcode and open a session."""
    limiter = state.limiter
    assert isinstance(limiter, LoginLimiter)  # Set by create_app
    address = _client_address(request, state.settings.trusted_proxy_hops)

    # Checked BEFORE the comparison: a locked address never reaches it.
    if limiter.locked(address):
        logger.warning("login refused: address locked out")
        raise ApiError(429, CODE_RATE_LIMITED, "Too many attempts. Try again later.")

    if not constant_time_equals(payload.passcode, state.settings.passcode):
        limiter.record_failure(address)
        logger.warning("login failed")
        raise ApiError(401, CODE_UNAUTHORIZED, UNAUTHORIZED_MESSAGE)

    limiter.record_success(address)
    session_id = new_session_id()
    state.sessions.create(session_id)
    token = issue_access_token(session_id, state.settings.jwt_secret)

    response.set_cookie(
        ACCESS_COOKIE_NAME,
        token,
        max_age=ACCESS_TOKEN_TTL_SECONDS,
        httponly=True,  # Unreadable to page JavaScript
        secure=True,  # HTTPS only
        samesite="lax",  # Not sent on cross-site POSTs
        path="/",
    )
    logger.info("session opened", extra={"session_id": session_id})
    return LoginResponse(session_id=session_id, expires_in_seconds=ACCESS_TOKEN_TTL_SECONDS)


@router.post("/logout", response_model=DeletedResponse, summary="End the session")
def logout(
    request: Request, response: Response, state: StateDep, _service_token: ServiceTokenDep
) -> DeletedResponse:
    """Discard the session and clear the cookie.

    Idempotent, and deliberately never reports whether a session existed: that
    would confirm a guessed identifier. The cookie is cleared either way.
    """
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/")
    token = access_token_from(request)
    if not token:
        return DeletedResponse(deleted=False)
    session_id = decode_access_token(token, state.settings.jwt_secret)
    if session_id is None:
        return DeletedResponse(deleted=False)
    state.sessions.delete(session_id)
    return DeletedResponse(deleted=True)

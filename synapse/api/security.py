"""
synapse.api.security
====================
Constant-time comparison, signed access tokens, and a failed-login limiter.

Three rules here, and each exists because the obvious implementation is subtly
wrong.

**1. Never compare a secret with ``==``.**
Python's string equality short-circuits on the first differing byte, so the time
it takes leaks how many leading bytes were right. Over enough requests that is
enough to recover a token byte by byte. Every comparison in this module goes
through :func:`hmac.compare_digest`, and :func:`constant_time_equals` encodes to
bytes first so a Unicode input cannot make ``compare_digest`` raise and turn a
timing question into a crash.

**2. Never let the token choose its own algorithm.**
:func:`decode_access_token` passes ``algorithms=["HS256"]`` explicitly. Without
it, a token whose header says ``{"alg": "none"}`` decodes as valid, and one
saying ``RS256`` invites the HMAC-as-public-key confusion. The header is never
read to decide how to verify.

**3. Never trust ``X-Forwarded-For``.**
Anyone can send that header, so a limiter keyed on its left-most entry is a
limiter an attacker resets at will. :func:`client_address_from` counts from the
*right* — past the hops this deployment actually has in front of it — because
those are the only entries a proxy appended rather than a client supplied.

The token carries a random session id and nothing else. No passcode, no query,
no user identity: there is no user identity in this system, and a token that
carried the passcode would put it in every subsequent request.
"""

from __future__ import annotations  # Postponed annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from synapse.api.config import (
    ACCESS_TOKEN_TTL_SECONDS,
    LOGIN_LOCKOUT_SECONDS,
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW_SECONDS,
)
from synapse.logging import get_logger

logger = get_logger(__name__)

JWT_ALGORITHM = "HS256"
# Claim names. Standard where a standard exists, so any JWT debugger reads them.
CLAIM_SESSION_ID = "sid"
CLAIM_ISSUED_AT = "iat"
CLAIM_EXPIRES_AT = "exp"

# Session identifiers are 32 hex characters of CSPRNG output (128 bits). Long
# enough that guessing one is not a strategy, short enough to log safely — and
# they ARE logged, because a session id names a conversation without revealing
# anything in it.
SESSION_ID_BYTES = 16


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two secrets without leaking their contents through timing.

    Encodes first so a non-ASCII value raises nothing: ``compare_digest`` on
    ``str`` requires both to be ASCII-only, and a raised exception is itself an
    observable difference between inputs.
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def new_session_id() -> str:
    """A fresh, unguessable session identifier."""
    return secrets.token_hex(SESSION_ID_BYTES)


def issue_access_token(
    session_id: str,
    secret: str,
    *,
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Sign an access token for ``session_id``.

    The payload is a session id and two timestamps. Deliberately nothing else:
    a token is sent on every request, so anything in it is disclosed on every
    request.
    """
    import jwt  # Optional dependency: the api extra

    issued = int(now if now is not None else time.time())
    return str(
        jwt.encode(
            {
                CLAIM_SESSION_ID: session_id,
                CLAIM_ISSUED_AT: issued,
                CLAIM_EXPIRES_AT: issued + ttl_seconds,
            },
            secret,
            algorithm=JWT_ALGORITHM,
        )
    )


def decode_access_token(token: str, secret: str) -> str | None:
    """Return the session id, or ``None`` for any token that is not valid.

    ``None`` rather than an exception, and one ``None`` for every failure mode:
    expired, wrong signature, malformed, missing claim. A caller that could tell
    them apart would eventually report the difference to a client, which turns
    the token into an oracle.
    """
    import jwt

    try:
        payload = jwt.decode(
            token,
            secret,
            # EXPLICIT. Never derived from the token's own header.
            algorithms=[JWT_ALGORITHM],
            options={"require": [CLAIM_EXPIRES_AT, CLAIM_SESSION_ID]},
        )
    # Broad on purpose: every failure is the same answer to the caller.
    except Exception:
        return None
    session_id = payload.get(CLAIM_SESSION_ID)
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_id


def client_address_from(forwarded_for: str | None, *, trusted_hops: int, fallback: str) -> str:
    """Derive the rate-limit key from ``X-Forwarded-For``, counting from the right.

    ``trusted_hops`` is how many proxies this deployment actually has in front
    of it. Their appended entries are the trustworthy ones; everything to the
    left was supplied by the client and may be invented.

    With one hop and ``"1.1.1.1, 2.2.2.2, 9.9.9.9"``, the right-most entry
    (``9.9.9.9``) was appended by our proxy and names its peer, so that is the
    key. A client prepending a hundred fake entries changes nothing.

    Falls back to the socket address when the header is absent or too short to
    contain a trusted entry.
    """
    if trusted_hops <= 0 or not forwarded_for:
        return fallback
    entries = [entry.strip() for entry in forwarded_for.split(",") if entry.strip()]
    if len(entries) < trusted_hops:
        # Fewer entries than proxies means the header did not come through the
        # chain we expect. Trust the socket instead of guessing.
        return fallback
    return entries[-trusted_hops]


@dataclass
class _Attempts:
    """Failure count and window for one address."""

    count: int = 0
    window_started: float = 0.0
    locked_until: float = 0.0


class LoginLimiter:
    """In-memory failed-login limiter, keyed by derived client address.

    Counts **failures only**. A correct passcode consumes no budget, so one
    person mistyping on a shared clinic address cannot lock out the next.

    Bounded: entries are evicted once their window has passed, and the whole map
    is capped, so an attacker rotating spoofed addresses cannot grow it without
    limit. (They cannot rotate the key anyway — see
    :func:`client_address_from` — but the ceiling holds regardless.)
    """

    MAX_TRACKED = 10_000

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_WINDOW_SECONDS,
        lockout_seconds: float = LOGIN_LOCKOUT_SECONDS,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempts] = {}

    def locked(self, address: str, *, now: float | None = None) -> bool:
        """True when this address must be refused without checking the passcode.

        Checked *before* the comparison, so a locked-out caller cannot use the
        endpoint as a passcode oracle at all.
        """
        moment = now if now is not None else time.monotonic()
        with self._lock:
            record = self._attempts.get(address)
            return record is not None and record.locked_until > moment

    def record_failure(self, address: str, *, now: float | None = None) -> bool:
        """Count one failure. Returns True if this one triggered a lockout."""
        moment = now if now is not None else time.monotonic()
        with self._lock:
            self._evict(moment)
            record = self._attempts.get(address)
            if record is None or moment - record.window_started > self._window:
                record = _Attempts(count=0, window_started=moment)
                self._attempts[address] = record
            record.count += 1
            if record.count >= self._max_failures:
                record.locked_until = moment + self._lockout
                # The address is a network address, not a person, and it is the
                # only thing recorded. No passcode, no attempt value.
                logger.warning("login lockout engaged", extra={"failures": record.count})
                return True
            return False

    def record_success(self, address: str) -> None:
        """Clear the record. A correct passcode ends the window."""
        with self._lock:
            self._attempts.pop(address, None)

    def _evict(self, now: float) -> None:
        """Drop expired records, and hard-cap the map. Caller holds the lock."""
        stale = [
            key
            for key, record in self._attempts.items()
            if record.locked_until <= now and now - record.window_started > self._window
        ]
        for key in stale:
            del self._attempts[key]
        if len(self._attempts) > self.MAX_TRACKED:
            # Oldest windows first. Losing a partial count is acceptable; an
            # unbounded map is not.
            for key, _ in sorted(self._attempts.items(), key=lambda item: item[1].window_started)[
                : len(self._attempts) - self.MAX_TRACKED
            ]:
                del self._attempts[key]


__all__ = [
    "CLAIM_EXPIRES_AT",
    "CLAIM_ISSUED_AT",
    "CLAIM_SESSION_ID",
    "JWT_ALGORITHM",
    "LoginLimiter",
    "client_address_from",
    "constant_time_equals",
    "decode_access_token",
    "issue_access_token",
    "new_session_id",
]

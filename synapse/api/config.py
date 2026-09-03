"""
synapse.api.config
==================
What the HTTP surface needs from the environment, and nothing else.

Three secrets, and none of them is ever returned, logged, or compared with
``==``:

``SYNAPSE_SERVICE_TOKEN``
    Proves a request came from the Next.js server proxy rather than from a
    browser. The browser never holds it — that is the whole point of the proxy
    in the locked architecture.

``SYNAPSE_ACCESS_PASSCODE``
    The shared passcode a person types once. Exchanged for a signed token; never
    stored anywhere afterwards.

``SYNAPSE_JWT_SECRET``
    Signs the eight-hour access token. Rotating it invalidates every session,
    which is the intended emergency control.

All three are read once at startup. :meth:`ApiSettings.validate` refuses to
start a deployment that has not set them, rather than defaulting to something
usable — a default passcode is an open door with a changelog entry.

Nothing here has a development fallback. A missing secret is a startup failure,
because the alternative is a deployment that looks configured and is not.
"""

from __future__ import annotations  # Postponed annotations

import os
from dataclasses import dataclass

from synapse.errors import SynapseArtifactError

# Names collected here so the deployment documentation and the reader can be
# checked against one list.
# The noqa markers are for flake8-bandit's hardcoded-password rule. These are
# the NAMES of environment variables, not values; the values live in the
# environment and never appear in this repository.
ENV_SERVICE_TOKEN = "SYNAPSE_SERVICE_TOKEN"  # noqa: S105
ENV_PASSCODE = "SYNAPSE_ACCESS_PASSCODE"
ENV_JWT_SECRET = "SYNAPSE_JWT_SECRET"  # noqa: S105
ENV_TRUSTED_PROXY_HOPS = "SYNAPSE_TRUSTED_PROXY_HOPS"

# The session and token lifetimes from the locked architecture. Both are
# deliberately short: a waiting-room device is shared, and a forgotten tab
# should stop being an authenticated one.
ACCESS_TOKEN_TTL_SECONDS = 8 * 60 * 60  # Signed cookie lifetime
SESSION_IDLE_TTL_SECONDS = 2 * 60 * 60  # In-memory conversation lifetime

# Bounds. Every one of these exists because the alternative is unbounded.
MAX_TURNS_PER_SESSION = 20
MAX_QUERY_CHARS = 2000
MAX_SESSIONS = 5000  # Ceiling on the whole store, so a login flood cannot exhaust memory
TURN_DEADLINE_SECONDS = 120.0  # Overall budget for one streamed turn
HEARTBEAT_SECONDS = 15.0  # SSE keep-alive, well under a typical 60s proxy idle timeout

# Failed-login limiter. Counts failures only: a correct passcode never consumes
# budget, so a legitimate user on a shared clinic IP is not locked out by
# someone else's typo.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_SECONDS = 15 * 60

# Minimum secret length. Not a strength check — just a refusal to run on
# something obviously a placeholder.
MIN_SECRET_CHARS = 16


class ApiConfigurationError(SynapseArtifactError):
    """The deployment is missing something the HTTP surface needs.

    Carries the variable NAME and never its value: an operator who set the wrong
    variable has put a secret somewhere it does not belong, and echoing it into
    a log would put it somewhere worse.
    """

    reason = "API configuration is incomplete"


@dataclass(frozen=True)
class ApiSettings:
    """Secrets and bounds for one process.

    Frozen, and never serialised. :meth:`describe` exists so a health check can
    report *that* the surface is configured without reporting *how*.
    """

    service_token: str
    passcode: str
    jwt_secret: str
    # How many proxy hops sit in front of this process. The client address is
    # taken this many entries from the right of X-Forwarded-For, so a client
    # cannot choose its own rate-limit key by prepending entries.
    trusted_proxy_hops: int = 1

    @classmethod
    def from_environment(cls) -> ApiSettings:
        """Read the settings. Does not validate; call :meth:`validate`."""
        try:
            hops = int(os.getenv(ENV_TRUSTED_PROXY_HOPS, "1"))
        except ValueError:
            hops = -1  # Deliberately invalid, so validate() reports it
        return cls(
            service_token=os.getenv(ENV_SERVICE_TOKEN, ""),
            passcode=os.getenv(ENV_PASSCODE, ""),
            jwt_secret=os.getenv(ENV_JWT_SECRET, ""),
            trusted_proxy_hops=hops,
        )

    def validate(self) -> None:
        """Raise unless every secret is present and plausibly a secret.

        Raises:
            ApiConfigurationError: a secret is missing or too short, or the
                proxy-hop count is not a non-negative integer.
        """
        missing = [
            name
            for name, value in (
                (ENV_SERVICE_TOKEN, self.service_token),
                (ENV_PASSCODE, self.passcode),
                (ENV_JWT_SECRET, self.jwt_secret),
            )
            if not value
        ]
        if missing:
            raise ApiConfigurationError(missing=",".join(missing))
        short = [
            name
            for name, value in (
                (ENV_SERVICE_TOKEN, self.service_token),
                (ENV_JWT_SECRET, self.jwt_secret),
            )
            if len(value) < MIN_SECRET_CHARS
        ]
        if short:
            raise ApiConfigurationError(
                variable=",".join(short), problem=f"shorter than {MIN_SECRET_CHARS} characters"
            )
        if self.trusted_proxy_hops < 0:
            raise ApiConfigurationError(
                variable=ENV_TRUSTED_PROXY_HOPS, problem="must be a non-negative integer"
            )

    @property
    def configured(self) -> bool:
        """True when :meth:`validate` would pass."""
        try:
            self.validate()
        except ApiConfigurationError:
            return False
        return True

    def describe(self) -> dict[str, object]:
        """Structural facts only. No secret, no length, no prefix."""
        return {
            "configured": self.configured,
            "trusted_proxy_hops": self.trusted_proxy_hops,
            "access_token_ttl_seconds": ACCESS_TOKEN_TTL_SECONDS,
            "session_idle_ttl_seconds": SESSION_IDLE_TTL_SECONDS,
        }


__all__ = [
    "ACCESS_TOKEN_TTL_SECONDS",
    "ENV_JWT_SECRET",
    "ENV_PASSCODE",
    "ENV_SERVICE_TOKEN",
    "ENV_TRUSTED_PROXY_HOPS",
    "HEARTBEAT_SECONDS",
    "LOGIN_LOCKOUT_SECONDS",
    "LOGIN_MAX_FAILURES",
    "LOGIN_WINDOW_SECONDS",
    "MAX_QUERY_CHARS",
    "MAX_SESSIONS",
    "MAX_TURNS_PER_SESSION",
    "MIN_SECRET_CHARS",
    "SESSION_IDLE_TTL_SECONDS",
    "TURN_DEADLINE_SECONDS",
    "ApiConfigurationError",
    "ApiSettings",
]

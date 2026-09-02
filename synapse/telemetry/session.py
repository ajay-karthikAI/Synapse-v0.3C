"""
synapse.telemetry.session
=========================
Short-lived, non-identifying session identifiers.

The identifier exists to answer one question: "were these three requests one
sitting or three people?" Without it, a retry storm and a traffic spike look
identical. With a *persistent* identifier, a series of anonymous health
questions becomes a longitudinal health profile — which is the harm described in
docs/privacy-logging-policy.md §2.6.

So the identifier is built to be useless for anything beyond that one question:

* **random** — ``secrets.token_hex``, never derived from a user, a device, an
  address, a browser or a query;
* **short-lived** — it rotates after a TTL (default 30 minutes) and on process
  restart, so it cannot span visits;
* **server-side** — never set as a cookie, never returned to the browser, so a
  client cannot be made to carry it between sessions;
* **not a key** — nothing else in the system stores anything under it.

Rotation is by wall clock rather than by activity: an idle-timeout scheme
extends with use, and a session that extends with use is exactly the
longitudinal identifier this avoids.
"""

from __future__ import annotations  # Postponed annotations

import secrets
import time
from dataclasses import dataclass, field

TOKEN_BYTES = 16  # 128 bits of randomness; collisions are not a concern at this scale


def new_id() -> str:
    """A fresh random identifier."""
    return secrets.token_hex(TOKEN_BYTES)


@dataclass
class SessionClock:
    """Issues a session identifier and rotates it on a fixed schedule.

    ``now`` is injected so rotation is testable without a test that waits.
    """

    ttl_seconds: int = 1800
    now: object = time.monotonic  # Callable[[], float]
    _current: str = field(default="", init=False)
    _issued_at: float = field(default=0.0, init=False)
    rotations: int = field(default=0, init=False)

    def session_id(self) -> str:
        """The current identifier, rotating it if the TTL has passed."""
        moment = float(self.now())  # type: ignore[operator]
        if not self._current or moment - self._issued_at >= self.ttl_seconds:
            self._current = new_id()
            self._issued_at = moment
            self.rotations += 1
        return self._current

    def rotate(self) -> str:
        """Force a new identifier, e.g. when a user clears the conversation."""
        self._current = new_id()
        self._issued_at = float(self.now())  # type: ignore[operator]
        self.rotations += 1
        return self._current


__all__ = ["TOKEN_BYTES", "SessionClock", "new_id"]

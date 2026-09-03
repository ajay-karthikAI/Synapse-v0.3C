"""
synapse.api
===========
The HTTP surface over the application service.

    synapse.api.app           create_app(): routes, error shape, no CORS
    synapse.api.config        secrets and bounds, from the environment
    synapse.api.security      constant-time comparison, JWTs, login limiter
    synapse.api.sessions      bounded, thread-safe, in-memory sessions
    synapse.api.models        the wire contract; the discriminated TurnEnvelope
    synapse.api.envelopes     TurnOutcome -> envelope, in one place
    synapse.api.sse           streaming: stages, heartbeats, one final envelope
    synapse.api.deps          the two gates every protected route passes
    synapse.api.transparency  what this deployment runs, and what it is not

The layering is one-directional: this package imports :mod:`synapse.service` and
below, never the reverse. Nothing here decides what a patient sees — that is
still the answer layer — and nothing here can render unvalidated model output,
because no response model has a field for it.
"""

from __future__ import annotations  # Postponed annotations

from synapse.api.app import create_app
from synapse.api.config import ApiConfigurationError, ApiSettings
from synapse.api.deps import ApiError, ApiState
from synapse.api.sessions import Session, SessionStore

__all__ = [
    "ApiConfigurationError",
    "ApiError",
    "ApiSettings",
    "ApiState",
    "Session",
    "SessionStore",
    "create_app",
]

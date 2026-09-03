"""
Offline builders for an API under test.

The app is assembled with the same ``create_app`` a deployment uses, but handed
a :class:`~synapse.service.service.SynapseService` wired entirely to fakes —
reusing ``tests.golden_states``, so the envelope tests and the golden-state
tests are asserting against the same synthetic turns rather than two sets that
can drift.

No network, no API key, no FAISS, no artifact, no clock manipulation beyond what
the store itself exposes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from synapse.api.app import create_app
from synapse.api.config import ApiSettings
from synapse.api.deps import ACCESS_COOKIE_NAME, SERVICE_TOKEN_HEADER
from synapse.api.sessions import SessionStore
from synapse.retrieval.evidence import RetrievalBundle
from synapse.runtime.readiness import Readiness, ReadinessState
from synapse.service.service import SynapseService
from tests.golden_states import FakeClient, bundle, service

# Test secrets. Long enough to satisfy the minimum-length refusal; obviously
# not credentials.
SERVICE_TOKEN = "service-token-for-tests-0123456789"
PASSCODE = "open-sesame"
JWT_SECRET = "jwt-secret-for-tests-0123456789ab"

READY = Readiness(state=ReadinessState.READY, artifact={"index_id": "test-index"})
NOT_READY = Readiness(state=ReadinessState.STARTING)


def settings(**overrides: object) -> ApiSettings:
    """Valid settings, with overrides."""
    base: dict[str, object] = {
        "service_token": SERVICE_TOKEN,
        "passcode": PASSCODE,
        "jwt_secret": JWT_SECRET,
        "trusted_proxy_hops": 1,
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def make_app(
    *,
    svc: SynapseService | None = None,
    client: FakeClient | None = None,
    retriever: Callable[[str], RetrievalBundle] | None = None,
    is_emergency: Callable[[str], bool] = lambda _query: False,
    readiness: Readiness = READY,
    ready: bool = True,
    sessions: SessionStore | None = None,
    source_pack: Path = Path("source_packs/diabetes-previsit"),
    evaluation_dir: Path = Path("does/not/exist"),
    **setting_overrides: object,
):
    """An app whose service is fully faked.

    ``ready=False`` omits the service entirely, which is what a process that
    failed its startup gates actually looks like — not merely a flag set to
    False beside a working service.
    """
    built = svc or service(client=client, retriever=retriever, is_emergency=is_emergency)
    return create_app(
        settings=settings(**setting_overrides),
        service=built if ready else None,
        readiness=readiness,
        source_pack=source_pack,
        evaluation_dir=evaluation_dir,
        sessions=sessions,
    )


def api(**kwargs: object) -> TestClient:
    """A ``TestClient`` over a faked app. Raises server exceptions to the test."""
    return TestClient(make_app(**kwargs))  # type: ignore[arg-type]


def service_headers() -> dict[str, str]:
    """Just the proxy credential."""
    return {SERVICE_TOKEN_HEADER: SERVICE_TOKEN}


def login(client: TestClient, passcode: str = PASSCODE) -> str:
    """Log in and return the access token. Cookies are kept by the client."""
    return login_full(client, passcode)[1]


def login_full(client: TestClient, passcode: str = PASSCODE) -> tuple[str, str]:
    """Log in and return ``(session_id, token)``.

    The session id is returned so a test can reach the stored session through
    the store's public API rather than by poking at its private dictionary.
    """
    response = client.post(
        "/v1/access/login", json={"passcode": passcode}, headers=service_headers()
    )
    response.raise_for_status()
    return response.json()["session_id"], client.cookies[ACCESS_COOKIE_NAME]


def authed(client: TestClient, passcode: str = PASSCODE) -> dict[str, str]:
    """Log in and return headers carrying BOTH credentials."""
    token = login(client, passcode)
    return {SERVICE_TOKEN_HEADER: SERVICE_TOKEN, "Authorization": f"Bearer {token}"}


def sse_events(body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into ``(event, data)`` pairs. Heartbeats are dropped."""
    events: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if name:
            events.append((name, data))
    return events


def heartbeat_count(body: str) -> int:
    """How many keep-alive comments the stream contained."""
    return body.count(": heartbeat")


def ask(
    client: TestClient,
    headers: dict[str, str],
    query: str = "why is my blood sugar high?",
    **extra: object,
):
    """POST one turn and return the raw response."""
    payload: dict[str, object] = {"query": query}
    payload.update(extra)
    return client.post("/v1/turns/stream", json=payload, headers=headers)


def envelope_of(response) -> dict:  # type: ignore[no-untyped-def]
    """The single envelope from a turn response."""
    import json

    events = sse_events(response.text)
    envelopes = [json.loads(data) for name, data in events if name == "envelope"]
    assert len(envelopes) == 1, f"expected exactly one envelope, got {len(envelopes)}"
    return envelopes[0]


__all__ = [
    "JWT_SECRET",
    "NOT_READY",
    "PASSCODE",
    "READY",
    "SERVICE_TOKEN",
    "api",
    "ask",
    "authed",
    "bundle",
    "envelope_of",
    "heartbeat_count",
    "login",
    "login_full",
    "make_app",
    "service_headers",
    "settings",
    "sse_events",
]

"""
Liveness and readiness.

Two endpoints that answer different questions, and conflating them is a classic
outage:

``/healthz``
    Is the process alive? Used by the platform to decide whether to restart.
    It must NOT depend on the artifact, or a bad artifact becomes a crash loop
    that replaces a diagnosable unready container with an undiagnosable
    restarting one.

``/readyz``
    Can this process serve? False until every startup gate has passed. Used to
    decide whether to send traffic.

Both are unauthenticated. A platform health check cannot hold a service token,
and neither response reveals anything: no bucket, no key, no path, no
conversation. ``detail`` is an exception type name.
"""

from __future__ import annotations  # Postponed annotations

from fastapi import APIRouter, Response

from synapse.api.deps import StateDep
from synapse.api.models import HealthResponse, ReadyResponse
from synapse.api.transparency import APPLICATION_VERSION

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse, summary="Liveness")
def healthz() -> HealthResponse:
    """Alive. Says nothing about whether an artifact loaded."""
    return HealthResponse(version=APPLICATION_VERSION)


@router.get("/readyz", response_model=ReadyResponse, summary="Readiness")
def readyz(state: StateDep, response: Response) -> ReadyResponse:
    """Ready only when every startup gate passed and a service exists."""
    readiness = state.readiness
    ready = state.ready
    if not ready:
        # 503 so a load balancer withholds traffic; the body says why.
        response.status_code = 503
    return ReadyResponse(
        ready=ready,
        state=readiness.state.value,
        failure_code=(readiness.failure_code.value if readiness.failure_code is not None else None),
        detail=readiness.detail,
        artifact=dict(readiness.artifact),
    )

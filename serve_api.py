"""
The ASGI entry point: ``uvicorn serve_api:app``.

Why this file is at the repository root
---------------------------------------
It binds the prototype's index into the API, and that binding cannot live inside
``synapse/``. ``tests/test_no_pickle_and_imports.py`` forbids the package from
importing ``Data/`` and ``Retrieval/`` at all, so the seam sits out here beside
``app.py`` — which does exactly the same thing for the Streamlit fallback, via
the same ``legacy_index.py``. Both interfaces therefore run the *same*
retrieval stack, which is what makes the parity claim in
``docs/migration-parity.md`` checkable rather than asserted.

It never crashes on a missing secret
------------------------------------
A misconfigured process stays up and reports itself unready, rather than
exiting. That is the same decision :class:`~synapse.runtime.readiness.RuntimeIndex`
makes, for the same reason: a container that crash-loops tells an operator far
less than one whose health check names the failure. So a missing
``OPENAI_API_KEY`` produces ``readiness.state == "failed"`` with
``failure_code == "configuration_error"``, ``/readyz`` says so, and every turn
is refused with a typed 503 — instead of a stack trace at import time.

The detail recorded is an exception **type name**, never its message: a
provider error can carry a key or a URL, and readiness is a public endpoint.

What this is not
----------------
This serves from the **legacy index** (``processed_chunks.pkl`` plus
``hybrid_index/``), the same one the Streamlit app uses. It does *not* serve
from the verified runtime artifact — ``synapse.runtime.RuntimeIndex`` loads and
verifies one, but nothing yet adapts its backends onto
:class:`~synapse.service.index.IndexProvider`, so there is no way to hand one to
the service. That adapter is the next piece of work; until it exists, a
deployment reading a pinned artifact is not something this file can start.
"""

from __future__ import annotations  # Postponed annotations

from synapse.api.app import create_app
from synapse.api.config import ApiSettings
from synapse.logging import get_logger
from synapse.runtime.readiness import Readiness, ReadinessState
from synapse.service.service import ServiceConfig, SynapseService
from synapse.telemetry import TelemetryRecorder
from synapse.ui.errors import AnswerFailureCode

logger = get_logger(__name__)

# Counters only; no query or answer text is ever recorded. Built the same way
# `app.py` builds it, from the same environment, so the two interfaces report
# identically. `from_environment` reads its own enable flag and returns a
# recorder that stores nothing unless telemetry is explicitly switched on.
TELEMETRY = TelemetryRecorder.from_environment(app_version="0.1.0")


def _build_service() -> tuple[SynapseService | None, Readiness]:
    """Build the service, or report why it could not be built.

    Returns ``(None, failed_readiness)`` rather than raising. See the module
    docstring: an unready process that explains itself is more useful than one
    that will not start.
    """
    try:
        # Imported here, not at module scope: this is the quarantined seam, and
        # importing it lazily keeps the failure local to this function.
        from legacy_index import legacy_index_provider

        config = ServiceConfig.from_environment()
        config.require_credentials()
        service = SynapseService.from_config(
            config,
            index=legacy_index_provider(
                api_key=config.api_key,
                chunks_path=config.chunks_path,
                index_dir=config.index_dir,
            ),
            telemetry=TELEMETRY,
        )
    # Broad on purpose: a missing credential, an absent pickle, a missing
    # optional dependency and an unreadable index directory all have the same
    # consequence — nothing can be served — and the same safe handling.
    except Exception as exc:
        readiness = Readiness(
            state=ReadinessState.FAILED,
            failure_code=AnswerFailureCode.CONFIGURATION_ERROR,
            # Type name only. An exception message here could carry a key.
            detail=type(exc).__name__,
        )
        logger.error("api failed to build its service", extra=readiness.as_dict())
        return None, readiness

    logger.info("api service ready")
    return service, Readiness(state=ReadinessState.READY, artifact={"index": "legacy"})


def build_app():  # type: ignore[no-untyped-def]
    """Assemble the ASGI application.

    ``ApiSettings.validate`` runs inside ``create_app`` and *does* raise: a
    missing service token or JWT secret is not a degraded mode, it is an open
    door. Those refuse to start; a missing model credential does not.
    """
    settings = ApiSettings.from_environment()
    service, readiness = _build_service()
    return create_app(settings=settings, service=service, readiness=readiness)


app = build_app()

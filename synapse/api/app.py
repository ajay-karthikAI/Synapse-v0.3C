"""
synapse.api.app
===============
The FastAPI application: routes, error shape, and what is deliberately absent.

Absent on purpose
-----------------
**No CORS middleware.** The browser never talks to this process. It calls a
same-origin Next.js proxy, which holds the service token and forwards. Enabling
CORS here would make the backend directly reachable from a page, which is
exactly the architecture the service token exists to prevent.

**No exception detail on the wire.** Every error, including one nobody
anticipated, is rendered by a handler as ``{"code", "message"}`` with a fixed
message. FastAPI's default 500 page and any traceback middleware are replaced,
because a traceback from this process can contain a prompt fragment — and a
prompt contains the patient's question.

**No request logging middleware.** A conventional access log records the path
and query string. Nothing here takes a query in a URL, but the safe way to keep
that true is to have no middleware that would print one if it did.

Startup is explicit
-------------------
:func:`create_app` takes the service and readiness as arguments rather than
building them. A process assembles them (loading and verifying the artifact),
then hands them over; a test passes fakes. The app never decides for itself
whether it is ready — it reports what it was told.
"""

from __future__ import annotations  # Postponed annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from synapse.api.config import ApiSettings
from synapse.api.deps import CODE_INVALID_REQUEST, ApiError, ApiState
from synapse.api.models import ErrorResponse
from synapse.api.routes import access, brief, health, session, transparency, turns
from synapse.api.security import LoginLimiter
from synapse.api.sessions import SessionStore
from synapse.api.transparency import APPLICATION_VERSION
from synapse.logging import get_logger
from synapse.runtime.readiness import Readiness, ReadinessState
from synapse.service.service import SynapseService

logger = get_logger(__name__)

# The one message an unanticipated fault produces. Identical for every cause,
# for the same reason synapse.ui.errors has exactly one patient message.
INTERNAL_ERROR_MESSAGE = "Something went wrong. Please try again."
CODE_INTERNAL = "internal_error"

DESCRIPTION = """\
Backend for Synapse, a pre-clinical appointment-preparation prototype.

**Not a medical device.** Not validated, not FDA cleared, not HIPAA compliant.
No clinician has reviewed any source, evaluation case, threshold or output.

Every route except `/healthz` and `/readyz` requires the `X-Service-Token`
header. Conversation routes additionally require an access token obtained from
`/v1/access/login`. The browser is never expected to hold the service token: it
calls a same-origin server proxy, which forwards.
"""


def create_app(
    *,
    settings: ApiSettings,
    service: SynapseService | None = None,
    readiness: Readiness | None = None,
    source_pack: Path = Path("source_packs/diabetes-previsit"),
    evaluation_dir: Path = Path("artifacts/evals"),
    sessions: SessionStore | None = None,
) -> FastAPI:
    """Build the application.

    Raises:
        ApiConfigurationError: a required secret is missing. Refused at
            construction rather than on the first request, so a misconfigured
            deployment fails to start instead of failing to authenticate.
    """
    settings.validate()

    app = FastAPI(
        title="Synapse API",
        version=APPLICATION_VERSION,
        description=DESCRIPTION,
        docs_url=None,  # No interactive docs in a deployed process
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.synapse = ApiState(
        settings=settings,
        # `is not None`, NOT `or`: SessionStore defines __len__, so an empty
        # store is falsy and `sessions or SessionStore()` would silently discard
        # a caller's store and build a second one. Every injected collaborator
        # here is checked the same way for the same reason.
        sessions=sessions if sessions is not None else SessionStore(),
        limiter=LoginLimiter(),
        source_pack=source_pack,
        service=service,
        readiness=(
            readiness if readiness is not None else Readiness(state=ReadinessState.STARTING)
        ),
        evaluation_dir=evaluation_dir,
    )

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        """Typed refusals, rendered as the declared error shape."""
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(code=exc.code, message=exc.message).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """A malformed request.

        The field locations are returned so a client can fix its call, but the
        submitted *values* are stripped: a rejected turn request contains the
        patient's question, and echoing it into an error body would put it in
        the client's logs.
        """
        fields = sorted(
            {".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()}
        )
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                code=CODE_INVALID_REQUEST,
                message=f"Invalid request. Check: {', '.join(fields)}" if fields else "Invalid request.",
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        """Anything nobody anticipated.

        The type name is logged; nothing about the exception reaches the client.
        A traceback from this process can contain a prompt fragment, and a
        prompt contains the patient's question.
        """
        logger.error("unhandled API error", extra={"error": type(exc).__name__})
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                code=CODE_INTERNAL, message=INTERNAL_ERROR_MESSAGE
            ).model_dump(),
        )

    for router in (
        health.router,
        access.router,
        session.router,
        turns.router,
        brief.router,
        transparency.router,
    ):
        app.include_router(router)

    logger.info("API application created", extra=settings.describe())
    return app


__all__ = ["CODE_INTERNAL", "INTERNAL_ERROR_MESSAGE", "create_app"]

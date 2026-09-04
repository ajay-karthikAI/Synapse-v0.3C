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

Two index shapes, chosen by configuration
-----------------------------------------
**The verified runtime artifact**, when ``SYNAPSE_ARTIFACT_BUCKET`` is set.
``RuntimeIndex`` downloads the archive, checks its digest, extracts it without
``extractall``, rebuilds the sparse index from the verified corpus, and refuses
a dense index whose vector count or dimensionality disagrees with it.
:class:`~synapse.service.runtime_provider.RuntimeIndexProvider` hands the result
to the service. This is the locked architecture: one container, one pinned
read-only artifact.

**The legacy prototype index** otherwise — ``processed_chunks.pkl`` plus
``hybrid_index/``, the same one ``app.py`` uses, which is what makes the parity
claim in ``docs/migration-parity.md`` checkable. This is the default, so every
local checkout and every existing test behaves exactly as before.

The switch is explicit rather than inferred from what happens to be on disk: a
process must never silently fall back to the prototype's pickle because a
download failed. Both shapes converge on the same pair of retrieval protocols
inside :meth:`~synapse.service.retrieval.RetrievalService._backends`, so the
deployed artifact and the local prototype cannot answer the same question
differently.
"""

from __future__ import annotations  # Postponed annotations

import os

from synapse.api.app import create_app
from synapse.api.config import ApiSettings
from synapse.logging import get_logger
from synapse.runtime.config import ENV_BUCKET
from synapse.runtime.readiness import Readiness, ReadinessState, RuntimeIndex
from synapse.service.service import ServiceConfig, SynapseService
from synapse.telemetry import TelemetryRecorder
from synapse.ui.errors import AnswerFailureCode

logger = get_logger(__name__)

# Counters only; no query or answer text is ever recorded. Built the same way
# `app.py` builds it, from the same environment, so the two interfaces report
# identically. `from_environment` reads its own enable flag and returns a
# recorder that stores nothing unless telemetry is explicitly switched on.
TELEMETRY = TelemetryRecorder.from_environment(app_version="0.1.0")


def _missing_index_inputs(config: ServiceConfig) -> list[str]:
    """Which configured retrieval inputs are not on disk.

    Returns FIELD NAMES, never paths. ``/readyz`` is unauthenticated, and a path
    discloses the filesystem layout of the container to anyone who asks.

    Why this exists
    ---------------
    ``legacy_index_provider`` is fully lazy -- every import inside it is
    function-local, deliberately, so that importing it costs no FAISS load. The
    consequence was that ``SynapseService.from_config`` succeeded whether or not
    an index existed, and ``/readyz`` answered ``ready:true`` for a container
    that could not answer a single question. Readiness has to mean "can serve",
    not "an object was constructed", or a load balancer will send traffic to a
    process with nothing to retrieve from.

    This is a presence check rather than a full load: it is instant, it runs
    before the process starts serving, and it catches the case that actually
    occurs -- an image or a host with no artifact. It does NOT prove the index
    is loadable or intact; the index gate does that on the first turn and
    produces a typed ``index_unverified`` outcome if it fails.
    """
    missing = []
    if not config.chunks_path.is_file():
        missing.append("chunks_path")
    if not config.index_dir.is_dir():
        missing.append("index_dir")
    return missing


def _serves_a_runtime_artifact() -> bool:
    """True when the deployment names an artifact to serve.

    The switch is the presence of ``SYNAPSE_ARTIFACT_BUCKET``, and it is
    deliberately explicit rather than inferred from what happens to be on disk:
    a process must not silently fall back to the prototype's pickle because a
    download failed. Unset -- which is every local checkout and every existing
    test -- means the legacy path, unchanged.
    """
    return bool(os.getenv(ENV_BUCKET, "").strip())


def _build_runtime_service() -> tuple[SynapseService | None, Readiness]:
    """Serve from the verified, pinned artifact. The locked architecture.

    ``RuntimeIndex.start`` runs every gate -- download, digest, safe extraction,
    corpus verification, dense/sparse construction -- and never raises: a failed
    gate becomes a typed readiness. So the process stays up and reports which
    gate failed, rather than crash-looping on a bad artifact.
    """
    from synapse.runtime.config import RuntimeArtifactConfig
    from synapse.runtime.embedding import OpenAIQueryEmbedder
    from synapse.runtime.objectstore import S3ObjectStore
    from synapse.service.runtime_provider import RuntimeIndexProvider

    config = ServiceConfig.from_environment()
    config.require_credentials()

    artifact_config = RuntimeArtifactConfig.from_environment()

    # Constructed UNBOUND. The model a query must be embedded with is a property
    # of the artifact, and the manifest that records it does not exist until
    # `start()` has downloaded and verified one. `NativeDenseBackend.load`
    # stores this callable without invoking it, so nothing can call it in the
    # window before it is bound below.
    embedder = OpenAIQueryEmbedder(api_key=config.api_key)

    index = RuntimeIndex(
        config=artifact_config,
        store=S3ObjectStore(
            endpoint_url=artifact_config.endpoint_url, region=artifact_config.region
        ),
        embed=embedder,
    )

    readiness = index.start()
    if not readiness.ready:
        # Already typed and already logged by RuntimeIndex. Reported as-is: a
        # second vocabulary for the same failure would only disagree with it.
        return None, readiness

    # Now the manifest exists and has been verified. A provider mismatch raises
    # here and is caught by the caller as a configuration error, which is the
    # honest reading: the deployment named an artifact this process cannot
    # query, rather than an artifact that is damaged.
    embedder.bind(index.backends.artifact.manifest.embedding)

    service = SynapseService.from_config(
        config, index=RuntimeIndexProvider(index=index), telemetry=TELEMETRY
    )
    logger.info("api service ready", extra=readiness.as_dict())
    return service, readiness


def _build_service() -> tuple[SynapseService | None, Readiness]:
    """Build the service, or report why it could not be built.

    Returns ``(None, failed_readiness)`` rather than raising. See the module
    docstring: an unready process that explains itself is more useful than one
    that will not start.

    Two index shapes, chosen by configuration and never by accident. Both end up
    behind the same :class:`~synapse.service.index.IndexProvider`, so everything
    downstream is identical.
    """
    if _serves_a_runtime_artifact():
        try:
            return _build_runtime_service()
        # Same breadth and the same reason as the legacy path below: a missing
        # credential or an unparseable artifact configuration must report
        # itself, not take the process down.
        except Exception as exc:
            readiness = Readiness(
                state=ReadinessState.FAILED,
                failure_code=AnswerFailureCode.CONFIGURATION_ERROR,
                detail=type(exc).__name__,
            )
            logger.error("api failed to build its runtime service", extra=readiness.as_dict())
            return None, readiness

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

    # Built successfully -- which is not the same as able to serve. Checked
    # AFTER construction so that a missing artifact is reported as a missing
    # artifact rather than as whatever the constructor happened to raise.
    missing = _missing_index_inputs(config)
    if missing:
        readiness = Readiness(
            state=ReadinessState.FAILED,
            failure_code=AnswerFailureCode.INDEX_UNVERIFIED,
            # Field names, not paths. See `_missing_index_inputs`.
            detail="missing:" + ",".join(missing),
        )
        logger.error("api has no retrieval index", extra=readiness.as_dict())
        # The service is discarded on purpose: holding a service that cannot
        # retrieve would let a turn through if readiness were ever bypassed.
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

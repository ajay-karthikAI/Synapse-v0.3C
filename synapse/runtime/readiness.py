"""
synapse.runtime.readiness
=========================
Whether this process may serve, and why not when it may not.

One rule: **readiness is false until every gate has passed.** Not "false until
we try", not "true unless something failed" — false, and only set true at the
end of a sequence in which each step either succeeds or raises:

    configuration -> artifact download and verification -> corpus load
        -> BM25 rebuild -> FAISS load and alignment check

Default-false matters more than it looks. A readiness flag that starts true and
is cleared on error serves traffic during startup and after any failure the
error handling missed. This one starts :attr:`ReadinessState.STARTING`, which is
not ready, and the only assignment to :attr:`ReadinessState.READY` is the last
statement of :meth:`RuntimeIndex.start`.

Failure codes
-------------
Two, and the distinction is the operator's next action:

``configuration_error``
    The deployment did not name an artifact. Set a variable.

``index_unverified``
    An artifact was named and could not be proven intact — missing, corrupt,
    tampered, reordered, incompatible, or misaligned with its index. Investigate
    the build; do not restart and hope.

Both are values of :class:`~synapse.ui.errors.AnswerFailureCode`, reused rather
than reinvented so an interface renders a readiness failure with the same fixed
copy and the same typed code it already renders a turn failure with.

Nothing here reports an exception message. ``detail`` is an exception *type
name*, which is enough to tell a corrupt digest from a missing object without
putting a provider's message — which may carry an endpoint, a request id, or a
presigned URL fragment — into a log or a health-check response.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from synapse.logging import get_logger
from synapse.retrieval.native import (
    NativeDenseBackend,
    NativeSparseBackend,
    RuntimeCorpus,
)
from synapse.runtime.config import ArtifactConfigurationError, RuntimeArtifactConfig
from synapse.runtime.loader import LoadedArtifact, load_artifact
from synapse.runtime.objectstore import ObjectStore
from synapse.ui.errors import AnswerFailureCode

logger = get_logger(__name__)


class ReadinessState(StrEnum):
    """Where startup got to. Only ``READY`` may serve."""

    STARTING = "starting"  # The default. Not ready.
    READY = "ready"  # Every gate passed and both backends are loaded.
    FAILED = "failed"  # A gate raised. Not ready, and will not become ready without a restart.


@dataclass(frozen=True)
class Readiness:
    """The readiness verdict, in the form a health check reports it."""

    state: ReadinessState
    failure_code: AnswerFailureCode | None = None
    detail: str = ""  # An exception TYPE name. Never a message.
    artifact: dict[str, object] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """True only in the one state that may serve."""
        return self.state is ReadinessState.READY

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for a readiness endpoint or a log line."""
        payload: dict[str, object] = {"state": self.state.value, "ready": self.ready}
        if self.failure_code is not None:
            payload["failure_code"] = self.failure_code.value
        if self.detail:
            payload["detail"] = self.detail
        if self.artifact:
            payload["artifact"] = self.artifact
        return payload


@dataclass(frozen=True)
class RuntimeBackends:
    """Both retrieval backends over one verified artifact."""

    dense: NativeDenseBackend
    sparse: NativeSparseBackend
    corpus: RuntimeCorpus
    artifact: LoadedArtifact


class NotReadyError(RuntimeError):
    """Something asked for the backends before startup completed.

    A programming error rather than an operational one: a caller that reaches
    the index without checking readiness has a bug, and it must surface as one
    rather than as a quietly empty result set.
    """


class RuntimeIndex:
    """Owns the verified artifact, both backends, and the readiness state.

    Constructed cheaply and started explicitly. Nothing touches the network,
    the disk or an optional dependency until :meth:`start` is called, so a
    process can build one, register a health check that reports
    ``STARTING``, and only then begin loading.
    """

    def __init__(
        self,
        config: RuntimeArtifactConfig,
        store: ObjectStore,
        embed: Callable[[str], Sequence[float]],
    ) -> None:
        self._config = config
        self._store = store
        self._embed = embed
        self._readiness = Readiness(state=ReadinessState.STARTING)
        self._backends: RuntimeBackends | None = None

    @property
    def readiness(self) -> Readiness:
        """The current verdict. Never raises."""
        return self._readiness

    @property
    def ready(self) -> bool:
        """True only when every gate passed and both backends are loaded."""
        return self._readiness.ready

    @property
    def backends(self) -> RuntimeBackends:
        """The loaded backends.

        Raises:
            NotReadyError: startup has not completed successfully.
        """
        if self._backends is None or not self.ready:
            raise NotReadyError(f"runtime index is {self._readiness.state.value}")
        return self._backends

    def start(self) -> Readiness:
        """Run every gate, then become ready. Never raises.

        Returns the resulting verdict, so a caller can log it and decide whether
        to exit. Failures are recorded rather than propagated because the
        process should stay up and report itself unready — a container that
        crash-loops on a bad artifact tells an operator far less than one whose
        health check names the failure.
        """
        try:
            artifact = load_artifact(self._config, self._store)
            corpus = RuntimeCorpus.load(artifact.directory, artifact.manifest)
            # Rebuilt from the verified corpus, never unpickled.
            sparse = NativeSparseBackend(corpus=corpus)
            # Refuses to construct if the vector count or dimensionality
            # disagrees with the corpus.
            dense = NativeDenseBackend.load(
                artifact.directory, artifact.manifest, corpus, self._embed
            )
        except ArtifactConfigurationError as exc:
            return self._fail(AnswerFailureCode.CONFIGURATION_ERROR, exc)
        # Broad on purpose: every other way this can fail — a missing object, a
        # digest mismatch, a hostile archive, a reordered corpus, an absent
        # optional dependency — has the same consequence and the same safe
        # handling. Serving nothing is the only correct response to all of them.
        except Exception as exc:
            return self._fail(AnswerFailureCode.INDEX_UNVERIFIED, exc)

        self._backends = RuntimeBackends(
            dense=dense, sparse=sparse, corpus=corpus, artifact=artifact
        )
        # The only place READY is ever assigned, and the last statement to run.
        self._readiness = Readiness(state=ReadinessState.READY, artifact=artifact.describe())
        logger.info("runtime ready", extra=self._readiness.as_dict())
        return self._readiness

    def _fail(self, code: AnswerFailureCode, exc: BaseException) -> Readiness:
        """Record a failed gate. Type name only — never the exception message."""
        self._backends = None
        self._readiness = Readiness(
            state=ReadinessState.FAILED, failure_code=code, detail=type(exc).__name__
        )
        logger.error("runtime failed to become ready", extra=self._readiness.as_dict())
        return self._readiness


__all__ = [
    "NotReadyError",
    "Readiness",
    "ReadinessState",
    "RuntimeBackends",
    "RuntimeIndex",
]

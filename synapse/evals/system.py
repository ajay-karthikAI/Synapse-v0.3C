"""
synapse.evals.system
====================
The system under test, behind an interface the harness can drive offline.

The harness must not import the application. If it did, evaluating anything
would require an OpenAI key, a FAISS index and a network, and the deterministic
half of the suite could not run in CI at all.

So the system under test is a **protocol** with two implementations:

* :class:`FixtureSystem` replays responses recorded in a fixture file, keyed by
  the normalised query. Fully offline, perfectly reproducible, and what every
  test and every ``--offline`` run uses.
* :class:`CallableSystem` wraps any callable, which is how a live pipeline is
  plugged in without the harness knowing anything about it.

A fixture miss is an **error**, never a default empty response. A silently
empty response would score as a retrieval failure and quietly drag every metric
down, which is precisely the kind of invisible corruption this harness exists
to prevent.
"""

from __future__ import annotations  # Postponed annotations

import json  # Fixture storage
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import Field, ValidationError

from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError, SynapseArtifactError
from synapse.logging import get_logger
from synapse.schemas.answer import StructuredAnswer
from synapse.schemas.base import SynapseModel
from synapse.schemas.enums import EvalExpectedBehavior
from synapse.schemas.evalset import EvalCase, normalize_query

logger = get_logger(__name__)


class SystemError_(SynapseArtifactError):  # Trailing underscore avoids shadowing the builtin
    """The system under test failed on a case."""

    reason = "system under test failed"


class SystemResponse(SynapseModel):
    """What the system produced for one query.

    Deliberately flat and provider-agnostic. Anything the harness needs to
    score is here; anything it does not need is not, so a fixture stays
    readable and a live adapter stays thin.
    """

    behavior: EvalExpectedBehavior = Field(
        description="What the system did: answered, abstained, routed, escalated, or refused."
    )
    answer_text: str = Field(
        default="", description="Rendered answer text, for readability and concept checks."
    )
    structured_answer: StructuredAnswer | None = Field(
        default=None,
        description="Structured form, when the system produced one. Required for citation metrics.",
    )

    retrieved_document_ids: list[str] = Field(
        default_factory=list, description="Documents retrieved, in rank order."
    )
    retrieved_chunk_ids: list[str] = Field(
        default_factory=list, description="Chunks retrieved, in rank order."
    )

    latency_ms: dict[str, float] = Field(
        default_factory=dict, description="Stage latencies: retrieval, rerank, generation, total."
    )
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    model: str = Field(default="", description="Generation model, for cost lookup.")

    errored: bool = Field(default=False)
    error_kind: str | None = Field(
        default=None, description="Machine-readable error class when the system failed."
    )
    timed_out: bool = Field(default=False)


class SystemUnderTest(Protocol):
    """Anything the harness can evaluate."""

    def run(self, case: EvalCase) -> SystemResponse: ...


@dataclass
class FixtureSystem:
    """Replays recorded responses. Offline and deterministic.

    Keyed by *normalised* query rather than case identifier, so one recorded
    response serves every case asking the same question — and so a fixture
    stays valid when a case is renumbered.
    """

    responses: dict[str, SystemResponse] = field(default_factory=dict)
    strict: bool = (
        True  # A miss raises. See the module docstring for why a default response would be worse.
    )

    @classmethod
    def load(cls, path: Path, *, strict: bool = True) -> FixtureSystem:
        """Read recorded responses from a JSONL fixture.

        Each line is ``{"query": ..., "response": {...}}``.
        """
        if not path.is_file():
            raise ArtifactNotFoundError(path=path, role="system fixture")
        responses: dict[str, SystemResponse] = {}
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)  # SAFE deserialisation
            except json.JSONDecodeError as exc:
                raise ArtifactSchemaError(
                    path=path, line=line_number, problem="fixture line is not valid JSON"
                ) from exc
            try:
                response = SystemResponse.model_validate(payload["response"])
            except (KeyError, ValidationError) as exc:
                raise ArtifactSchemaError(
                    path=path,
                    line=line_number,
                    problem="fixture line is not a valid recorded response",
                ) from exc
            responses[normalize_query(str(payload["query"]))] = response
        logger.info("system fixture loaded", extra={"responses": len(responses)})
        return cls(responses=responses, strict=strict)

    def run(self, case: EvalCase) -> SystemResponse:
        """Return the recorded response for a case's query."""
        response = self.responses.get(case.normalized_query)
        if response is None:
            if self.strict:
                # An unrecorded query is a gap in the fixture, not a system
                # failure. Raising forces whoever added the case to record a
                # response rather than letting the case score as a silent zero.
                raise SystemError_(
                    problem="no recorded response for this case", case_id=case.case_id
                )
            return SystemResponse(
                behavior=EvalExpectedBehavior.ABSTAIN, errored=True, error_kind="fixture_miss"
            )
        return response


@dataclass
class CallableSystem:
    """Wraps an arbitrary callable as a system under test.

    How a live pipeline is plugged in without the harness importing it. The
    callable's exceptions are caught and converted into an errored response —
    recorded with a kind, never swallowed — so one failing case does not abort
    a whole run.
    """

    fn: Callable[[EvalCase], SystemResponse]
    name: str = "callable"

    def run(self, case: EvalCase) -> SystemResponse:
        """Invoke the wrapped callable, converting failures into errored responses."""
        try:
            return self.fn(case)
        except SynapseArtifactError as exc:
            # Typed failures keep their class, which is what the error summary
            # groups on.
            logger.warning(
                "system failed on case",
                extra={"case_id": case.case_id, "error": type(exc).__name__},
            )
            return SystemResponse(
                behavior=EvalExpectedBehavior.ABSTAIN, errored=True, error_kind=type(exc).__name__
            )
        except TimeoutError:
            logger.warning("system timed out on case", extra={"case_id": case.case_id})
            return SystemResponse(
                behavior=EvalExpectedBehavior.ABSTAIN,
                errored=True,
                error_kind="timeout",
                timed_out=True,
            )
        except Exception as exc:  # Deliberately broad: an unexpected failure in the SYSTEM must not abort the RUN. It is recorded with its class, never discarded.
            logger.error(
                "system raised an unexpected error",
                extra={"case_id": case.case_id, "error": type(exc).__name__},
            )
            return SystemResponse(
                behavior=EvalExpectedBehavior.ABSTAIN,
                errored=True,
                error_kind=f"unexpected:{type(exc).__name__}",
            )


@dataclass
class RealEmergencyRoutingSystem:
    """Wraps another system, replacing the RECORDED emergency verdict with the
    one the shipped detector actually produces.

    The defect this closes
    ----------------------
    A fixture's ``behavior`` field for an emergency case was hand-authored to
    the *expected* answer, so ``emergency_sensitivity`` scored a wish rather
    than the system: 100% reported against 2/6 actual. Every other recorded
    field genuinely needs a model and is passed through untouched — only the
    escalation decision is recomputed, because it is the one stage that runs
    before retrieval and needs neither network nor key.

    Authority rule
    --------------
    The real detector is authoritative for *whether escalation happened*:

    * detector fires  -> ``EMERGENCY_ESCALATION``, whatever the fixture said.
    * detector silent but fixture claimed escalation -> the real pipeline would
      have carried on to retrieval, so the claim is dropped to ``ANSWER``. This
      is what turns a fabricated pass into a visible miss.
    * detector silent and fixture agreed -> fixture stands.

    One honest limit: in the second case the real downstream behaviour is
    unknowable offline (it might answer, it might abstain on thin evidence).
    ``ANSWER`` is the conservative choice for safety scoring — it records that
    escalation did *not* occur, which is the fact the safety metric needs.
    """

    inner: SystemUnderTest
    detector: Callable[[str], bool]

    def run(self, case: EvalCase) -> SystemResponse:
        """Delegate, then correct the escalation decision using the real detector."""
        response = self.inner.run(case)
        if response.errored:
            return response  # An errored response has no behaviour worth correcting

        escalates = self.detector(case.query)  # The shipped code, not a recording
        if escalates:
            corrected = EvalExpectedBehavior.EMERGENCY_ESCALATION
        elif response.behavior is EvalExpectedBehavior.EMERGENCY_ESCALATION:
            corrected = (
                EvalExpectedBehavior.ANSWER
            )  # Fixture claimed an escalation that never happens
        else:
            return response  # Nothing to correct

        if corrected is not response.behavior:
            logger.info(
                "escalation verdict corrected from the real detector",
                extra={  # Identifiers and enum values only; never the query text
                    "case_id": case.case_id,
                    "recorded": response.behavior.value,
                    "observed": corrected.value,
                },
            )
        return response.model_copy(update={"behavior": corrected})


def record_fixture(path: Path, entries: list[tuple[str, SystemResponse]]) -> int:
    """Write a system fixture, for capturing a live run to replay offline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for query, response in entries:
            handle.write(
                json.dumps(
                    {"query": query, "response": response.model_dump(mode="json")}, sort_keys=True
                )
                + "\n"
            )
    return len(entries)


__all__ = [
    "CallableSystem",
    "FixtureSystem",
    "SystemError_",
    "SystemResponse",
    "SystemUnderTest",
    "record_fixture",
]

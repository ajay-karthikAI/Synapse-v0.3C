"""
synapse.telemetry.schema
========================
The telemetry event: an explicit allowlist, enforced by the type.

Every field a Synapse telemetry record may ever contain is declared here. That
is the mechanism behind rules 6 and 7 of docs/privacy-logging-policy.md — the
model inherits ``extra="forbid"`` from :class:`~synapse.schemas.base.SynapseModel`,
so an unknown field is a validation error rather than a silently-accepted string
of unknown provenance.

What is **not** here is the point of the design. There is no field for a query,
a query digest, an answer, an excerpt, a chunk identifier, a network address, a
user agent, a header, an exception message or a traceback. Their absence is
structural: a future maintainer cannot record patient text by passing it to this
model, because there is nowhere for it to go. See the threat analysis in
docs/privacy-logging-policy.md §2 for why each was excluded.

The fields that *are* here answer the five operational questions in §7 of that
document: is it up, how fast, is it failing, is it behaving safely, what does it
cost — plus the six version stamps that let any of those be attributed to a
release.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from synapse.schemas.base import SynapseModel, require_timezone_aware


class Environment(StrEnum):
    """Where the event was produced.

    ``LOCAL`` is the default so that a developer's records are never mistaken
    for production traffic in an aggregate someone later reads.
    """

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    # The hosted, passcode-gated demonstration. Distinct from PRODUCTION on
    # purpose: nothing here is clinically validated or approved for care, and an
    # aggregate that merged the two would invite exactly the reading the rest of
    # this repository refuses.
    DEMO = "demo"
    PRODUCTION = "production"


class ActionResult(StrEnum):
    """What the system did with the request.

    Mirrors :class:`synapse.answer.schema.AnswerAction` plus the outcomes that
    are not answers. Duplicated as a separate enum on purpose: telemetry is a
    long-lived wire format, and coupling it to an internal enum means an
    internal rename silently rewrites historical records' meaning.
    """

    ANSWER = "answer"
    ABSTAIN = "abstain"
    MEDICAL_STAFF = "medical_staff"
    EMERGENCY = "emergency"
    FAILURE = "failure"  # A typed failure; see failure_code
    BRIEF_EXPORTED = "brief_exported"  # A patient downloaded the appointment brief


class StatusClass(StrEnum):
    """The class of a provider response — never the body, never a header.

    Coarse on purpose (docs/privacy-logging-policy.md §2.8): these five values
    are what separate an operator's possible responses, and a finer record would
    carry provider prose.
    """

    OK = "2xx"
    CLIENT_ERROR = "4xx"
    RATE_LIMITED = "429"  # Split from 4xx: it is the one client error worth alerting on
    SERVER_ERROR = "5xx"
    TIMEOUT = "timeout"
    NETWORK = "network"


class FeedbackCategory(StrEnum):
    """A closed set. There is deliberately no free-text feedback field."""

    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"
    UNCLEAR = "unclear"
    CONCERNING = "concerning"
    REPORTED_ERROR = "reported_error"


class StageDurations(SynapseModel):
    """Milliseconds per pipeline stage.

    Durations are the highest-value telemetry this system produces and disclose
    nothing about content: a slow retrieval is slow whatever was asked.
    """

    safety_check_ms: float = Field(default=0.0, ge=0.0)
    retrieval_ms: float = Field(default=0.0, ge=0.0)
    rerank_ms: float = Field(default=0.0, ge=0.0)
    generation_ms: float = Field(default=0.0, ge=0.0)
    verification_ms: float = Field(default=0.0, ge=0.0)

    def total_ms(self) -> float:
        """Sum of the recorded stages."""
        return (
            self.safety_check_ms
            + self.retrieval_ms
            + self.rerank_ms
            + self.generation_ms
            + self.verification_ms
        )


class TelemetryEvent(SynapseModel):
    """One request's telemetry record. The complete allowlist.

    Adding a field here requires a corresponding entry in the threat analysis
    (docs/privacy-logging-policy.md §2) and a test. The schema rejecting unknown
    fields is what makes that requirement enforceable rather than aspirational.
    """

    SCHEMA_NAME: ClassVar[str] = "telemetry_event"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    # -- identity -----------------------------------------------------------
    request_id: str = Field(
        min_length=8, description="Random per-request identifier. Never derived from content."
    )
    session_id: str = Field(
        min_length=8,
        description="Random, short-lived, server-side. Not a user or device identifier.",
    )
    occurred_at: datetime = Field(description="When the request completed (timezone-aware).")

    # -- provenance: which build, which model, which evidence ---------------
    app_version: str = Field(min_length=1, description="Synapse version that served the request.")
    environment: Environment = Field(default=Environment.LOCAL)
    provider: str = Field(default="", description="Generation provider, e.g. 'openai'.")
    model: str = Field(default="", description="Generation model identifier.")
    prompt_version: str = Field(default="", description="Versioned prompt template identifier.")
    rerank_prompt_version: str = Field(default="", description="Versioned rerank prompt id.")
    source_pack_version: str = Field(default="", description="Governed source pack version.")
    corpus_version: str = Field(default="", description="Corpus the evidence came from.")
    index_version: str = Field(default="", description="Index build identifier.")
    pricing_version: str = Field(
        default="", description="Pricing table version behind estimated_cost."
    )

    # -- retrieval and reranking shape (counts only, never identifiers) -----
    retrieval_strategy: str = Field(default="", description="Fusion strategy, e.g. 'rrf'.")
    candidate_count: int = Field(default=0, ge=0, description="Candidates after union and fusion.")
    evidence_count: int = Field(
        default=0, ge=0, description="Chunks handed to generation. A count, never a list."
    )
    claims_shown: int = Field(default=0, ge=0)
    claims_withheld: int = Field(default=0, ge=0)

    # -- outcome ------------------------------------------------------------
    action_result: ActionResult = Field(description="What the system did.")
    failure_code: str = Field(
        default="",
        description="Typed code from a closed set, e.g. 'generation_schema_invalid'. Never a message.",
    )
    exception_type: str = Field(
        default="", description="Exception class name only. Never a message, never a traceback."
    )
    status_class: StatusClass | None = Field(
        default=None, description="Provider response class, when a provider was called."
    )
    degraded: bool = Field(
        default=False, description="True when a stage fell back, e.g. rerank to fused order."
    )
    retry_count: int = Field(default=0, ge=0)

    # -- timing -------------------------------------------------------------
    stage_durations: StageDurations = Field(default_factory=StageDurations)
    total_duration_ms: float = Field(default=0.0, ge=0.0)

    # -- cost ---------------------------------------------------------------
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    estimated_cost: Decimal = Field(
        default=Decimal(0),
        description="ESTIMATE from a versioned pricing table. Never an invoice figure.",
    )
    cost_currency: str = Field(default="USD")
    cost_is_estimate: bool = Field(
        default=True,
        description="Always True. Present so a consumer cannot read the figure as billed.",
    )

    # -- optional feedback --------------------------------------------------
    feedback: FeedbackCategory | None = Field(
        default=None, description="Closed category. There is no free-text feedback field."
    )

    @field_validator("cost_is_estimate")
    @classmethod
    def _cost_is_always_an_estimate(cls, value: bool) -> bool:
        """Reject any attempt to present a cost as authoritative."""
        if not value:
            raise ValueError(
                "estimated_cost is always an estimate; cost_is_estimate cannot be False"
            )
        return value

    @model_validator(mode="after")
    def _validate_event(self) -> TelemetryEvent:
        """Enforce internal coherence."""
        require_timezone_aware(self.occurred_at, "occurred_at")
        if self.action_result is ActionResult.FAILURE and not self.failure_code:
            # A failure nobody can categorise is a failure nobody can fix.
            raise ValueError("a failure event must carry a typed failure_code")
        if self.failure_code and self.action_result is not ActionResult.FAILURE:
            raise ValueError("failure_code is only valid on a failure event")
        return self


__all__ = [
    "ActionResult",
    "Environment",
    "FeedbackCategory",
    "StageDurations",
    "StatusClass",
    "TelemetryEvent",
]

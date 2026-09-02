"""
synapse.retrieval.rerank
========================
One bounded structured request for the whole candidate set.

What this replaces
------------------
``Retrieval/reranker.rerank_with_llm`` looped over the candidates and issued
**one chat completion per passage** — ten sequential round trips on the
patient's critical path for a ten-candidate set, each re-sending the same
instructions, each paying full latency. It had no timeout, no retry policy, and
a bare ``except Exception`` that substituted ``retrieval_score * 10`` and wrote
the exception into a field called ``rerank_reason``. A provider outage and a
successful rerank were indistinguishable downstream.

The design here
---------------
* **One call.** The whole candidate set goes in one request, and the model
  returns one verdict per candidate keyed by **stable chunk identifier**. The
  call count is now 1 per query plus bounded retries, not N.
* **Identifiers, not positions.** The response is matched on ``chunk_id``.
  Positional matching would silently mis-attribute a score if the model
  reordered its output, and reordering is exactly what a reranker does.
* **Validated, not trusted.** Unknown identifiers, duplicates, missing
  candidates and out-of-range scores are each a rejection
  (:class:`RerankValidationError`), not something to repair. A response that
  scores a chunk that was never sent is evidence the model invented one.
* **Bounded everywhere.** Connect timeout, read timeout, a total operation
  deadline across all attempts, a retry ceiling, and exponential backoff with
  full jitter. Transient failures are retried; a schema failure is retried at
  most once, because a model that cannot satisfy the contract will not learn to
  on the fifth attempt.
* **Degradation is deterministic, not silent.** When reranking cannot succeed
  within its budget, the fused retrieval order is used unchanged and the outcome
  is marked ``degraded`` with a reason. Nothing downstream has to guess, and
  **no P1 guarantee is relaxed**: the answer layer still verifies every claim
  against the evidence, and the display policy still withholds and abstains.
"""

from __future__ import annotations  # Postponed annotations

import json
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from pydantic import Field, ValidationError

from synapse.answer.providers import ProviderUnavailableError
from synapse.errors import SynapseArtifactError
from synapse.logging import get_logger
from synapse.retrieval.candidates import Candidate
from synapse.retrieval.config import DEFAULT_RERANK_CONFIG, RerankConfig
from synapse.schemas.base import SynapseModel

logger = get_logger(__name__)

# Versioned prompt (requirement 5). Any change to the wording is a change to
# this identifier, so a benchmark or an eval run can be attributed to the prompt
# that produced it.
PROMPT_ID = "rerank-batched-v1"

SYSTEM_PROMPT = """\
You rank medical text passages by how useful they are for answering a patient's
question before an appointment.

You are given a question and a numbered list of passages, each with a chunk_id.
Return a JSON object with a "verdicts" array containing ONE entry per passage,
using the EXACT chunk_id given to you.

For each passage give:
  relevance: a number from 0 to 10 — does it directly address the question?
  rank: its position in your ranking, 1 = most useful, each rank used once
  rationale: one short sentence, at most 240 characters

Rank every passage you are given. Do not invent a chunk_id. Do not omit a
passage. Do not return a passage twice. You are ranking passages for usefulness
only; you are not deciding what the patient is told.
"""

# HTTP statuses worth retrying: the request may succeed unchanged. A 400 or a
# 401 will not, so retrying either just burns the deadline.
TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class RerankOutcomeKind(StrEnum):
    """How the reranking stage ended."""

    RERANKED = "reranked"  # Validated verdicts applied
    DEGRADED_TRANSIENT = "degraded_transient"  # Provider failed; fused order used
    DEGRADED_SCHEMA = "degraded_schema"  # Response could not be validated; fused order used
    DEGRADED_DEADLINE = "degraded_deadline"  # Ran out of time; fused order used
    DISABLED = "disabled"  # Reranking switched off by configuration
    SKIPPED_EMPTY = "skipped_empty"  # Nothing to rerank


class RerankValidationError(SynapseArtifactError):
    """The reranker's response did not describe the candidate set it was sent."""

    reason = "reranker response failed validation"


class RerankVerdict(SynapseModel):
    """One candidate's rank and score, as returned by the model."""

    chunk_id: str = Field(min_length=1, description="Identifier of the candidate being scored.")
    relevance: float = Field(description="Usefulness for this question, on the configured scale.")
    rank: int = Field(ge=1, description="Position in the model's ranking; 1 is most useful.")
    rationale: str = Field(default="", description="One short sentence explaining the score.")


class RerankResponse(SynapseModel):
    """The whole response: one verdict per candidate."""

    verdicts: list[RerankVerdict] = Field(default_factory=list)


class RerankClient(Protocol):
    """Minimal provider interface, mirroring the answer layer's.

    Narrow on purpose: a fake in a test is three lines, and no provider type
    reaches the validation code.
    """

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str: ...


@dataclass(frozen=True)
class RerankOutcome:
    """The reranked (or deliberately un-reranked) candidates, and what happened."""

    candidates: list[Candidate]
    kind: RerankOutcomeKind
    reason: str = ""
    attempts: int = 0
    model_calls: int = 0
    prompt_tokens_estimated: int = 0
    latency_ms: float = 0.0
    verdicts: dict[str, RerankVerdict] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """True when the fused retrieval order was used instead of a rerank."""
        return self.kind in {
            RerankOutcomeKind.DEGRADED_TRANSIENT,
            RerankOutcomeKind.DEGRADED_SCHEMA,
            RerankOutcomeKind.DEGRADED_DEADLINE,
        }

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable form for run metadata and benchmarks."""
        return {
            "kind": self.kind.value,
            "degraded": self.degraded,
            "reason": self.reason,
            "attempts": self.attempts,
            "model_calls": self.model_calls,
            "prompt_tokens_estimated": self.prompt_tokens_estimated,
            "latency_ms": round(self.latency_ms, 3),
            "candidates_returned": len(self.candidates),
        }


def response_json_schema(config: RerankConfig | None = None) -> dict:
    """JSON Schema for the response, derived from the models above.

    Derived rather than hand-written, for the same reason the answer layer
    derives its own: the contract the model is given and the contract the code
    enforces must be the same object.
    """
    config = config or DEFAULT_RERANK_CONFIG
    schema = RerankResponse.model_json_schema()
    schema.get("properties", {}).pop("schema_version", None)
    definitions = schema.get("$defs", {})
    if "RerankVerdict" in definitions:
        definitions["RerankVerdict"]["properties"]["relevance"]["description"] = (
            f"Usefulness from {config.min_score} to {config.max_score}."
        )
    return schema


def build_user_prompt(query: str, candidates: Sequence[Candidate]) -> str:
    """Assemble the passage block.

    Passages are labelled with their real ``chunk_id`` rather than ``[Source N]``
    so the response can be matched back deterministically. The legacy reranker
    prompt used positional labels that resolved to nothing.
    """
    blocks = [
        f"chunk_id: {candidate.chunk_id}\npassage: {candidate.text}" for candidate in candidates
    ]
    return f"QUESTION: {query}\n\nPASSAGES:\n\n" + "\n\n".join(blocks)


def _estimate_tokens(text: str) -> int:
    """Rough prompt-size estimate: four characters per token.

    An estimate, and labelled as one wherever it is reported. Exact accounting
    needs the provider's usage field, which a fake client does not have; the
    ratio is stable enough to compare two prompt *structures*, which is what the
    benchmark uses it for.
    """
    return max(1, len(text) // 4)


def validate_response(
    raw: str, candidates: Sequence[Candidate], config: RerankConfig
) -> dict[str, RerankVerdict]:
    """Parse and validate a reranker response against the candidate set.

    Every rejection here is a rejection of the *response*, never a repair of it.

    Raises:
        RerankValidationError: the response is not valid JSON, does not satisfy
            the schema, names an unknown chunk, repeats a chunk, omits a
            candidate, or scores one outside the configured range.
    """
    text = raw.strip()
    if text.startswith("```"):  # Models fence JSON despite instructions
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RerankValidationError(problem="response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RerankValidationError(problem="response was not a JSON object")
    try:
        parsed = RerankResponse.model_validate(payload)
    except ValidationError as exc:
        raise RerankValidationError(
            problem="response did not satisfy the schema", error_count=exc.error_count()
        ) from exc

    expected = {candidate.chunk_id for candidate in candidates}
    verdicts: dict[str, RerankVerdict] = {}
    for verdict in parsed.verdicts:
        if verdict.chunk_id not in expected:
            # A score for a passage that was never sent. The model did not read
            # it, whatever it says about it.
            raise RerankValidationError(problem="response scored an unknown chunk_id")
        if verdict.chunk_id in verdicts:
            raise RerankValidationError(problem="response scored the same chunk_id twice")
        if not config.min_score <= verdict.relevance <= config.max_score:
            raise RerankValidationError(
                problem="relevance score outside the configured range",
                score=verdict.relevance,
            )
        if len(verdict.rationale) > config.max_rationale_chars:
            raise RerankValidationError(problem="rationale exceeds the configured length")
        verdicts[verdict.chunk_id] = verdict

    missing = expected - set(verdicts)
    if missing:
        # A partial ranking is not a ranking: the candidates left out would
        # either vanish or need a score this module invented for them.
        raise RerankValidationError(problem="response omitted candidates", missing=len(missing))
    return verdicts


def apply_verdicts(
    candidates: Sequence[Candidate], verdicts: dict[str, RerankVerdict], config: RerankConfig
) -> list[Candidate]:
    """Order candidates by the model's ranking, deterministically.

    Ordered by the model's ``rank``, then by descending relevance, then by
    ``chunk_id`` — so a model that returns duplicate ranks still produces one
    total order rather than an arbitrary one. The fused score and both component
    ranks are preserved on every candidate (requirement 7): reranking changes the
    order, never the retrieval evidence.
    """
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            verdicts[candidate.chunk_id].rank,
            -verdicts[candidate.chunk_id].relevance,
            candidate.chunk_id,
        ),
    )
    return [
        replace(candidate, fused_rank=position)
        for position, candidate in enumerate(ordered[: config.top_k], start=1)
    ]


def _is_transient(exc: BaseException) -> bool:
    """True when retrying the identical request could plausibly succeed.

    A timeout or a 503 is worth another attempt. A 400 or a 401 is not: the
    request is wrong or the credential is, and neither changes by being sent
    again.
    """
    if isinstance(exc, TimeoutError):
        return True
    status = getattr(exc, "details", {}).get("status") if hasattr(exc, "details") else None
    if isinstance(status, int):
        return status in TRANSIENT_STATUSES
    if isinstance(exc, ProviderUnavailableError):
        # No status: a connection-level failure, which is transient by nature.
        return "status" not in exc.details
    return False


def _backoff_delay(attempt: int, config: RerankConfig, rng: random.Random) -> float:
    """Exponential backoff with full jitter.

    ``random.uniform(0, base * 2**(attempt-1))``, capped. Full jitter rather than
    a fixed multiplier so that concurrent sessions failing on the same provider
    incident do not retry in lockstep and reproduce the spike that caused it.
    """
    ceiling = min(config.backoff_base_seconds * (2 ** (attempt - 1)), config.backoff_max_seconds)
    return rng.uniform(0.0, ceiling) if config.jitter else ceiling


def rerank_candidates(
    query: str,
    candidates: Sequence[Candidate],
    client: RerankClient | None,
    config: RerankConfig | None = None,
    *,
    rng: random.Random | None = None,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> RerankOutcome:
    """Rerank a candidate set with one bounded structured request.

    ``rng``, ``sleep`` and ``now`` are injected so the retry, backoff and
    deadline behaviour can be tested deterministically and instantly, without a
    test that actually waits.

    Never raises for an expected failure. Every failure path returns the fused
    retrieval order with ``degraded`` set, because a ranking that could not be
    improved is still a usable ranking — and because the safety guarantees
    downstream do not depend on this stage having run.
    """
    config = config or DEFAULT_RERANK_CONFIG
    rng = rng or random.Random(0)  # noqa: S311 - jitter, not cryptography
    monotonic = now
    pause = sleep

    if not candidates:
        return RerankOutcome([], RerankOutcomeKind.SKIPPED_EMPTY, "no candidates to rerank")
    if not config.enabled or client is None:
        return RerankOutcome(
            list(candidates[: config.top_k]),
            RerankOutcomeKind.DISABLED,
            "reranking disabled by configuration",
        )

    user_prompt = build_user_prompt(query, candidates)
    schema = response_json_schema(config)
    estimated_tokens = _estimate_tokens(SYSTEM_PROMPT + user_prompt)
    started = monotonic()
    deadline = started + config.deadline_seconds

    attempts = 0
    calls = 0
    schema_failures = 0
    last_reason = ""
    last_kind = RerankOutcomeKind.DEGRADED_TRANSIENT

    while attempts < config.max_attempts:
        if monotonic() >= deadline:
            last_kind = RerankOutcomeKind.DEGRADED_DEADLINE
            last_reason = "operation deadline reached before the attempt"
            break
        attempts += 1
        try:
            calls += 1
            raw = client.generate_structured(system=SYSTEM_PROMPT, user=user_prompt, schema=schema)
        except Exception as exc:  # Classified immediately below; never propagated to the caller
            last_reason = f"provider error: {type(exc).__name__}"
            last_kind = RerankOutcomeKind.DEGRADED_TRANSIENT
            if not _is_transient(exc) or attempts >= config.max_attempts:
                break
            delay = _backoff_delay(attempts, config, rng)
            if monotonic() + delay >= deadline:
                last_kind = RerankOutcomeKind.DEGRADED_DEADLINE
                last_reason = "operation deadline would be exceeded by backoff"
                break
            pause(delay)
            continue

        try:
            verdicts = validate_response(raw, candidates, config)
        except RerankValidationError as exc:
            schema_failures += 1
            last_reason = exc.safe_message
            last_kind = RerankOutcomeKind.DEGRADED_SCHEMA
            if schema_failures >= config.max_schema_attempts or attempts >= config.max_attempts:
                # Bounded on purpose. A model that cannot produce the contract
                # will not produce it on attempt five, and every further try
                # spends the patient's latency budget to learn nothing.
                break
            delay = _backoff_delay(attempts, config, rng)
            if monotonic() + delay >= deadline:
                last_kind = RerankOutcomeKind.DEGRADED_DEADLINE
                last_reason = "operation deadline would be exceeded by backoff"
                break
            pause(delay)
            continue

        elapsed = (monotonic() - started) * 1000
        outcome = RerankOutcome(
            candidates=apply_verdicts(candidates, verdicts, config),
            kind=RerankOutcomeKind.RERANKED,
            reason=f"{len(verdicts)} candidate(s) ranked in one request",
            attempts=attempts,
            model_calls=calls,
            prompt_tokens_estimated=estimated_tokens,
            latency_ms=elapsed,
            verdicts=verdicts,
        )
        logger.info("candidates reranked", extra=outcome.as_metadata())
        return outcome

    elapsed = (monotonic() - started) * 1000
    outcome = RerankOutcome(
        candidates=list(candidates[: config.top_k]),  # Deterministic fused order, unchanged
        kind=last_kind,
        reason=last_reason,
        attempts=attempts,
        model_calls=calls,
        prompt_tokens_estimated=estimated_tokens,
        latency_ms=elapsed,
    )
    logger.warning("reranking degraded to fused order", extra=outcome.as_metadata())
    return outcome


__all__ = [
    "PROMPT_ID",
    "SYSTEM_PROMPT",
    "TRANSIENT_STATUSES",
    "RerankClient",
    "RerankOutcome",
    "RerankOutcomeKind",
    "RerankResponse",
    "RerankValidationError",
    "RerankVerdict",
    "apply_verdicts",
    "build_user_prompt",
    "rerank_candidates",
    "response_json_schema",
    "validate_response",
]

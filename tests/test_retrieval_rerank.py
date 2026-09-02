"""
Batched reranking: one call, strict validation, bounded failure, safe fallback.

The point of these tests is not that the reranker works — it is that when it
does **not** work, the system degrades to something deterministic instead of
either raising into the patient's face or silently substituting a plausible
number, which is what the legacy per-passage loop did with its bare
``except Exception``.

Timing is injected (``sleep``, ``now``, ``rng``), so retry, backoff and deadline
behaviour is asserted exactly and instantly. No test here waits.
"""

from __future__ import annotations

import json
import random

import pytest

from synapse.answer.providers import ProviderUnavailableError
from synapse.retrieval.candidates import Candidate
from synapse.retrieval.config import RerankConfig
from synapse.retrieval.rerank import (
    PROMPT_ID,
    SYSTEM_PROMPT,
    RerankOutcomeKind,
    RerankValidationError,
    build_user_prompt,
    rerank_candidates,
    response_json_schema,
    validate_response,
)


def a_candidate(index: int) -> Candidate:
    """One fused candidate."""
    return Candidate(
        chunk_id=f"pubmed:{6_000_000 + index}#0000",
        document_id=f"pubmed:{6_000_000 + index}",
        text=f"passage {index} about hba1c and glucose",
        fused_rank=index,
        fused_score=1.0 / index,
    )


CANDIDATES = [a_candidate(index) for index in range(1, 6)]


def a_response(candidates=CANDIDATES, **overrides) -> str:
    """A valid response, optionally corrupted by the caller."""
    verdicts = [
        {
            "chunk_id": candidate.chunk_id,
            "relevance": 9.0 - position,
            "rank": position + 1,
            "rationale": "relevant to the question",
        }
        for position, candidate in enumerate(candidates)
    ]
    payload = {"verdicts": verdicts}
    payload.update(overrides)
    return json.dumps(payload)


class FakeClient:
    """A reranking client returning canned responses, or raising."""

    def __init__(self, *responses: str, errors: list[Exception] | None = None) -> None:
        self.responses = list(responses)
        self.errors = list(errors or [])
        self.calls = 0
        self.prompts: list[str] = []

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        self.prompts.append(user)
        if self.errors:
            raise self.errors.pop(0)
        if self.responses:
            return self.responses.pop(0)
        # No canned response: answer the prompt actually received, so a test
        # that varies the candidate set does not fail on a stale fixture.
        chunk_ids = [block.partition("\n")[0].strip() for block in user.split("chunk_id: ")[1:]]
        return json.dumps(
            {
                "verdicts": [
                    {
                        "chunk_id": chunk_id,
                        "relevance": max(0.0, 9.0 - position),
                        "rank": position + 1,
                        "rationale": "relevant",
                    }
                    for position, chunk_id in enumerate(chunk_ids)
                ]
            }
        )


def run(client, config: RerankConfig | None = None, candidates=CANDIDATES, **kwargs):
    """Rerank with time and randomness pinned."""
    return rerank_candidates(
        "why is my hba1c high?",
        candidates,
        client,
        config or RerankConfig(),
        rng=random.Random(1),
        sleep=lambda _delay: None,
        **kwargs,
    )


class TestOneCallNotN:
    """Requirements 1 and 4."""

    def test_a_single_request_covers_every_candidate(self) -> None:
        client = FakeClient()
        outcome = run(client)
        assert client.calls == 1
        assert outcome.kind is RerankOutcomeKind.RERANKED
        assert outcome.model_calls == 1

    def test_call_count_does_not_grow_with_the_candidate_set(self) -> None:
        # The legacy loop issued one call per candidate; this is the property
        # that replaced it.
        for size in (1, 5, 20):
            client = FakeClient()
            candidates = [a_candidate(index) for index in range(1, size + 1)]
            run(client, candidates=candidates, config=RerankConfig(top_k=3))
            assert client.calls == 1, f"{size} candidates should still be one call"

    def test_every_candidate_appears_in_the_prompt_with_its_chunk_id(self) -> None:
        client = FakeClient()
        run(client)
        prompt = client.prompts[0]
        for candidate in CANDIDATES:
            assert candidate.chunk_id in prompt
        assert "[Source 1]" not in prompt  # The legacy positional label resolved to nothing

    def test_the_prompt_is_versioned(self) -> None:
        assert PROMPT_ID == "rerank-batched-v1"
        assert "chunk_id" in SYSTEM_PROMPT

    def test_the_response_schema_is_derived_from_the_models(self) -> None:
        schema = response_json_schema()
        assert "verdicts" in schema["properties"]
        assert "schema_version" not in schema["properties"]


class TestValidationRejectsBadResponses:
    """Requirement 3: four named rejections, none of them repaired."""

    def test_unknown_chunk_id_is_rejected(self) -> None:
        raw = json.dumps(
            {"verdicts": [{"chunk_id": "pubmed:99999999#0000", "relevance": 5, "rank": 1}]}
        )
        with pytest.raises(RerankValidationError, match="unknown chunk_id"):
            validate_response(raw, CANDIDATES, RerankConfig())

    def test_duplicate_chunk_id_is_rejected(self) -> None:
        verdicts = [
            {"chunk_id": CANDIDATES[0].chunk_id, "relevance": 5, "rank": 1},
            {"chunk_id": CANDIDATES[0].chunk_id, "relevance": 4, "rank": 2},
        ]
        with pytest.raises(RerankValidationError, match="same chunk_id twice"):
            validate_response(json.dumps({"verdicts": verdicts}), CANDIDATES, RerankConfig())

    def test_missing_candidates_are_rejected(self) -> None:
        raw = a_response(CANDIDATES[:2])
        with pytest.raises(RerankValidationError, match="omitted candidates"):
            validate_response(raw, CANDIDATES, RerankConfig())

    def test_out_of_range_score_is_rejected(self) -> None:
        verdicts = [
            {"chunk_id": c.chunk_id, "relevance": 99.0, "rank": i + 1}
            for i, c in enumerate(CANDIDATES)
        ]
        with pytest.raises(RerankValidationError, match="outside the configured range"):
            validate_response(json.dumps({"verdicts": verdicts}), CANDIDATES, RerankConfig())

    def test_negative_score_is_rejected(self) -> None:
        verdicts = [
            {"chunk_id": c.chunk_id, "relevance": -1.0, "rank": i + 1}
            for i, c in enumerate(CANDIDATES)
        ]
        with pytest.raises(RerankValidationError, match="outside the configured range"):
            validate_response(json.dumps({"verdicts": verdicts}), CANDIDATES, RerankConfig())

    def test_non_json_is_rejected(self) -> None:
        with pytest.raises(RerankValidationError, match="not valid JSON"):
            validate_response("the first passage is best", CANDIDATES, RerankConfig())

    def test_schema_violation_is_rejected(self) -> None:
        raw = json.dumps({"verdicts": [{"chunk_id": CANDIDATES[0].chunk_id, "rank": 0}]})
        with pytest.raises(RerankValidationError):
            validate_response(raw, CANDIDATES, RerankConfig())

    def test_an_overlong_rationale_is_rejected(self) -> None:
        verdicts = [
            {"chunk_id": c.chunk_id, "relevance": 5.0, "rank": i + 1, "rationale": "x" * 500}
            for i, c in enumerate(CANDIDATES)
        ]
        with pytest.raises(RerankValidationError, match="rationale"):
            validate_response(json.dumps({"verdicts": verdicts}), CANDIDATES, RerankConfig())

    def test_a_valid_response_is_accepted_and_ordered(self) -> None:
        outcome = run(FakeClient(a_response(list(reversed(CANDIDATES)))))
        assert outcome.kind is RerankOutcomeKind.RERANKED
        # The model ranked the reversed list first-best, so candidate 5 leads.
        assert outcome.candidates[0].chunk_id == CANDIDATES[-1].chunk_id

    def test_reranking_preserves_retrieval_evidence(self) -> None:
        # Requirement 7 again, on the far side of reranking: the component ranks
        # must survive, or a debugging session has nothing to work with.
        candidate = Candidate(
            chunk_id="pubmed:6000001#0000",
            document_id="pubmed:6000001",
            text="passage",
            dense_rank=4,
            dense_score=0.25,
            sparse_rank=9,
            sparse_score=3.5,
            fused_score=0.02,
        )
        outcome = run(FakeClient(a_response([candidate])), candidates=[candidate])
        reranked = outcome.candidates[0]
        assert (reranked.dense_rank, reranked.dense_score) == (4, 0.25)
        assert (reranked.sparse_rank, reranked.sparse_score) == (9, 3.5)


class TestDeterministicFallback:
    """Degradation order rung 2: fused order, unchanged, marked."""

    def test_a_schema_failure_falls_back_to_fused_order(self) -> None:
        bad = json.dumps({"verdicts": []})
        outcome = run(FakeClient(bad, bad))
        assert outcome.kind is RerankOutcomeKind.DEGRADED_SCHEMA
        assert outcome.degraded is True
        kept = RerankConfig(enabled=False).top_k
        assert [c.chunk_id for c in outcome.candidates] == [c.chunk_id for c in CANDIDATES[:kept]]

    def test_schema_failures_are_retried_once_then_stop(self) -> None:
        # Bounded: a model that cannot satisfy the contract will not learn to.
        bad = json.dumps({"verdicts": []})
        client = FakeClient(bad, bad, bad)
        run(client, RerankConfig(max_attempts=5, max_schema_attempts=2))
        assert client.calls == 2

    def test_a_transient_failure_is_retried_then_succeeds(self) -> None:
        client = FakeClient(a_response(), errors=[TimeoutError("read timed out")])
        outcome = run(client)
        # The first attempt raised, the second returned the canned response.
        assert client.calls == 2
        assert outcome.kind is RerankOutcomeKind.RERANKED

    def test_a_permanent_failure_is_not_retried(self) -> None:
        # A 400 will not become a 200 by being sent again.
        client = FakeClient(errors=[ProviderUnavailableError(problem="bad request", status=400)])
        outcome = run(client)
        assert client.calls == 1
        assert outcome.kind is RerankOutcomeKind.DEGRADED_TRANSIENT

    def test_a_transient_status_is_retried(self) -> None:
        client = FakeClient(
            a_response(), errors=[ProviderUnavailableError(problem="rate limited", status=429)]
        )
        outcome = run(client)
        assert client.calls == 2
        assert outcome.kind is RerankOutcomeKind.RERANKED

    def test_retries_are_bounded_by_max_attempts(self) -> None:
        errors = [TimeoutError("t") for _ in range(10)]
        client = FakeClient(errors=errors)
        outcome = run(client, RerankConfig(max_attempts=3))
        assert client.calls == 3
        assert outcome.attempts == 3
        assert outcome.degraded

    def test_the_deadline_stops_retrying(self) -> None:
        # A clock that jumps past the deadline after the first attempt.
        ticks = iter([0.0, 0.1, 100.0, 100.0, 100.0, 100.0])
        client = FakeClient(errors=[TimeoutError("t"), TimeoutError("t"), TimeoutError("t")])
        outcome = rerank_candidates(
            "q",
            CANDIDATES,
            client,
            RerankConfig(max_attempts=5, deadline_seconds=30.0),
            rng=random.Random(1),
            sleep=lambda _delay: None,
            now=lambda: next(ticks),
        )
        assert outcome.kind is RerankOutcomeKind.DEGRADED_DEADLINE
        assert client.calls < 5

    def test_backoff_grows_and_is_jittered(self) -> None:
        from synapse.retrieval.rerank import _backoff_delay

        config = RerankConfig()
        rng = random.Random(7)
        delays = [_backoff_delay(attempt, config, rng) for attempt in (1, 2, 3, 9)]
        assert all(delay >= 0 for delay in delays)
        assert max(delays) <= config.backoff_max_seconds
        # Jitter means two draws at the same attempt differ.
        assert _backoff_delay(3, config, random.Random(1)) != _backoff_delay(
            3, config, random.Random(2)
        )

    def test_backoff_without_jitter_is_exponential(self) -> None:
        from synapse.retrieval.rerank import _backoff_delay

        config = RerankConfig(jitter=False)
        rng = random.Random(0)
        assert _backoff_delay(1, config, rng) == 0.5
        assert _backoff_delay(2, config, rng) == 1.0
        assert _backoff_delay(3, config, rng) == 2.0
        assert _backoff_delay(10, config, rng) == config.backoff_max_seconds

    def test_a_disabled_reranker_returns_the_fused_order(self) -> None:
        outcome = run(FakeClient(), RerankConfig(enabled=False))
        assert outcome.kind is RerankOutcomeKind.DISABLED
        kept = RerankConfig(enabled=False).top_k
        assert [c.chunk_id for c in outcome.candidates] == [c.chunk_id for c in CANDIDATES[:kept]]

    def test_no_client_returns_the_fused_order(self) -> None:
        outcome = rerank_candidates("q", CANDIDATES, None)
        assert outcome.kind is RerankOutcomeKind.DISABLED

    def test_an_empty_candidate_set_makes_no_call(self) -> None:
        client = FakeClient()
        outcome = rerank_candidates("q", [], client)
        assert client.calls == 0
        assert outcome.kind is RerankOutcomeKind.SKIPPED_EMPTY

    def test_degradation_is_recorded_in_metadata(self) -> None:
        outcome = run(FakeClient(errors=[TimeoutError("t"), TimeoutError("t"), TimeoutError("t")]))
        metadata = outcome.as_metadata()
        assert metadata["degraded"] is True
        assert metadata["kind"] == "degraded_transient"
        assert metadata["model_calls"] >= 1


class TestPromptShape:
    """The prompt carries what validation depends on."""

    def test_the_user_prompt_pairs_each_chunk_id_with_its_passage(self) -> None:
        prompt = build_user_prompt("a question", CANDIDATES[:2])
        assert prompt.count("chunk_id:") == 2
        assert "a question" in prompt

    def test_config_rejects_unbounded_retry_settings(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RerankConfig(max_attempts=0)
        with pytest.raises(ValueError, match="max_schema_attempts"):
            RerankConfig(max_schema_attempts=0)

    def test_schema_attempts_are_clamped_to_the_attempt_ceiling(self) -> None:
        # `max_attempts=1` means one attempt; the schema ceiling follows it down
        # rather than making the configuration invalid.
        assert RerankConfig(max_attempts=1).max_schema_attempts == 1
        assert RerankConfig(max_attempts=2, max_schema_attempts=5).max_schema_attempts == 2

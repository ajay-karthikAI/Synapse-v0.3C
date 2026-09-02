"""
The retrieval path end to end, and the four rungs of the degradation ladder.

    1. batched reranker succeeds        -> validated reranked candidates
    2. reranker temporarily fails       -> deterministic fused order
    3. evidence below the sufficiency policy -> abstain
    4. index or manifest fails to verify -> fail closed, generate nothing

The rung that matters most is the one that is *not* on the list: nothing in this
ladder relaxes a P1 guarantee. A degraded rerank still produces evidence that
the answer layer verifies claim by claim, and an unsupported claim is still
withheld — asserted directly in ``TestDegradationPreservesCitationIntegrity``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synapse.answer.policy import DisplayPolicy
from synapse.answer.providers import ProviderUnavailableError
from synapse.answer.schema import AnswerAction
from synapse.errors import ArtifactIntegrityError, ArtifactNotFoundError, ArtifactSchemaError
from synapse.retrieval.backends import (
    CorpusChunk,
    InMemoryDense,
    InMemorySparse,
    hashed_embedder,
)
from synapse.retrieval.config import CandidateConfig, RerankConfig
from synapse.retrieval.evidence import bundle_from_candidates
from synapse.retrieval.index_gate import IndexStatus, check_index
from synapse.retrieval.rerank import rerank_candidates
from synapse.retrieval.search import search_candidates
from synapse.ui.errors import AnswerFailureCode, classify
from synapse.ui.pipeline import answer_turn

HBA1C_TEXT = (
    "HbA1c reflects average plasma glucose over 2-3 months. "
    "A target below 7% is appropriate for most non-pregnant adults."
)


def a_corpus(size: int = 120) -> list[CorpusChunk]:
    """A corpus with one genuinely on-topic chunk and many distractors."""
    chunks = [
        CorpusChunk(
            chunk_id="pubmed:41802233#0000",
            document_id="pubmed:41802233",
            text=HBA1C_TEXT,
            title="HbA1c Targets in Adults",
            url="https://pubmed.ncbi.nlm.nih.gov/41802233/",
        )
    ]
    chunks += [
        CorpusChunk(
            chunk_id=f"pubmed:{7_000_000 + index}#0000",
            document_id=f"pubmed:{7_000_000 + index}",
            text=f"blood pressure and cholesterol review note {index}",
            title=f"Review {index}",
        )
        for index in range(size - 1)
    ]
    return chunks


def a_retriever(chunks: list[CorpusChunk], rerank_client=None, rerank_config=None):
    """A retriever closure shaped exactly like the one in app.py."""
    embed = hashed_embedder()
    dense = InMemoryDense(chunks, [embed(c.text) for c in chunks], embed)
    sparse = InMemorySparse(chunks)

    def retrieve(query: str):
        result = search_candidates(
            query,
            dense=dense,
            sparse=sparse,
            config=CandidateConfig(dense_top_n=25, sparse_top_n=25, final_top_k=5),
        )
        outcome = rerank_candidates(
            query,
            result.candidates,
            rerank_client,
            rerank_config or RerankConfig(top_k=3),
            sleep=lambda _delay: None,
        )
        return bundle_from_candidates(
            outcome.candidates,
            metadata={"retrieval": result.trace.as_metadata(), "rerank": outcome.as_metadata()},
        )

    return retrieve


GROUNDED_ANSWER = json.dumps(
    {
        "summary": "Your HbA1c reflects your average blood sugar.",
        "claims": [
            {
                "claim_id": "c1",
                "text": "HbA1c reflects average plasma glucose over 2-3 months.",
                "source_ids": ["pubmed:41802233"],
                "supporting_excerpts": [
                    {
                        "source_id": "pubmed:41802233",
                        "chunk_id": "pubmed:41802233#0000",
                        "quote": "HbA1c reflects average plasma glucose over 2-3 months",
                    }
                ],
            }
        ],
        "doctor_evaluation": "Your clinician will read it alongside your history.",
        "questions_for_doctor": ["What does my result mean for me?"],
        "limitations": ["General information."],
        "disclaimer": "ignored by the renderer",
        "action": "answer",
    }
)

FABRICATED_ANSWER = json.dumps(
    {
        "summary": "A summary.",
        "claims": [
            {
                "claim_id": "c1",
                "text": "An assertion the evidence does not support.",
                "source_ids": ["pubmed:41802233"],
                "supporting_excerpts": [
                    {
                        "source_id": "pubmed:41802233",
                        "chunk_id": "pubmed:41802233#0000",
                        "quote": "a quote that appears in no retrieved chunk",
                    }
                ],
            }
        ],
        "doctor_evaluation": "",
        "questions_for_doctor": ["What should I ask?"],
        "limitations": [],
        "disclaimer": "",
        "action": "answer",
    }
)


class GenerationClient:
    """A fake answer-generation client."""

    def __init__(self, response: str = GROUNDED_ANSWER) -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        self.prompts.append(user)
        return self.response


class RerankClient:
    """A fake reranking client that ranks by lexical overlap."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        question = user.split("QUESTION:", 1)[-1].split("\n", 1)[0].lower().split()
        rows = []
        for block in user.split("chunk_id: ")[1:]:
            chunk_id, _, rest = block.partition("\n")
            overlap = sum(1 for term in question if term in rest.lower())
            rows.append((chunk_id.strip(), overlap))
        rows.sort(key=lambda item: (-item[1], item[0]))
        return json.dumps(
            {
                "verdicts": [
                    {
                        "chunk_id": chunk_id,
                        "relevance": min(10.0, float(overlap)),
                        "rank": rank,
                        "rationale": "overlap",
                    }
                    for rank, (chunk_id, overlap) in enumerate(rows, start=1)
                ]
            }
        )


class TestRungOneRerankerSucceeds:
    """Validated reranked candidates are used."""

    def test_a_grounded_answer_is_produced(self) -> None:
        reranker = RerankClient()
        outcome = answer_turn(
            "what does hba1c measure?",
            retrieve=a_retriever(a_corpus(), reranker),
            client=GenerationClient(),
            is_emergency=lambda _q: False,
        )
        assert outcome.ok
        assert outcome.presentation.answer.action is AnswerAction.ANSWER
        assert reranker.calls == 1  # One call, whatever the candidate count

    def test_retrieval_metadata_travels_with_the_turn(self) -> None:
        outcome = answer_turn(
            "hba1c average glucose",
            retrieve=a_retriever(a_corpus(), RerankClient()),
            client=GenerationClient(),
            is_emergency=lambda _q: False,
        )
        metadata = outcome.presentation.conversion.metadata
        assert metadata["retrieval"]["config"]["dense_top_n"] == 25
        assert metadata["rerank"]["kind"] == "reranked"
        assert metadata["retrieval"]["corpus_fraction_examined"] < 1.0

    def test_the_generator_only_sees_bounded_evidence(self) -> None:
        client = GenerationClient()
        answer_turn(
            "hba1c average glucose",
            retrieve=a_retriever(a_corpus(500), RerankClient()),
            client=client,
            is_emergency=lambda _q: False,
        )
        # final_top_k=5 then rerank top_k=3: the prompt must not carry a corpus.
        assert client.prompts[0].count("chunk_id:") <= 3


class TestRungTwoRerankerFails:
    """A temporary failure degrades to the deterministic fused order."""

    def test_a_provider_outage_still_produces_an_answer(self) -> None:
        outcome = answer_turn(
            "what does hba1c measure?",
            retrieve=a_retriever(
                a_corpus(), RerankClient(error=ProviderUnavailableError(problem="503", status=503))
            ),
            client=GenerationClient(),
            is_emergency=lambda _q: False,
        )
        assert outcome.ok
        assert outcome.presentation.conversion.metadata["rerank"]["degraded"] is True

    def test_the_fallback_order_is_the_fused_order(self) -> None:
        chunks = a_corpus()
        embed = hashed_embedder()
        dense = InMemoryDense(chunks, [embed(c.text) for c in chunks], embed)
        sparse = InMemorySparse(chunks)
        config = CandidateConfig(dense_top_n=25, sparse_top_n=25, final_top_k=5)
        retrieval = search_candidates("hba1c glucose", dense=dense, sparse=sparse, config=config)
        outcome = rerank_candidates(
            "hba1c glucose",
            retrieval.candidates,
            RerankClient(error=TimeoutError("read timed out")),
            RerankConfig(top_k=3, max_attempts=2),
            sleep=lambda _delay: None,
        )
        assert outcome.degraded
        assert [c.chunk_id for c in outcome.candidates] == [
            c.chunk_id for c in retrieval.candidates[:3]
        ]

    def test_degradation_is_deterministic(self) -> None:
        first = a_retriever(
            a_corpus(), RerankClient(error=TimeoutError("t")), RerankConfig(max_attempts=1)
        )("hba1c glucose")
        second = a_retriever(
            a_corpus(), RerankClient(error=TimeoutError("t")), RerankConfig(max_attempts=1)
        )("hba1c glucose")
        assert first.source_order == second.source_order


class TestRungThreeInsufficientEvidence:
    """Below the sufficiency policy, the system abstains."""

    def test_unsupported_claims_abstain_the_turn(self) -> None:
        outcome = answer_turn(
            "what does hba1c measure?",
            retrieve=a_retriever(a_corpus(), RerankClient()),
            client=GenerationClient(FABRICATED_ANSWER),
            is_emergency=lambda _q: False,
        )
        assert outcome.ok
        assert outcome.presentation.answer.action is AnswerAction.ABSTAIN
        assert outcome.presentation.answer.claims == []

    def test_an_empty_candidate_set_fails_before_generation(self) -> None:
        client = GenerationClient()
        outcome = answer_turn(
            "hba1c",
            retrieve=lambda _q: bundle_from_candidates([]),
            client=client,
            is_emergency=lambda _q: False,
        )
        assert not outcome.ok
        assert outcome.failure.code is AnswerFailureCode.EVIDENCE_UNAVAILABLE
        assert client.prompts == []

    def test_a_stricter_policy_abstains_sooner(self) -> None:
        outcome = answer_turn(
            "what does hba1c measure?",
            retrieve=a_retriever(a_corpus(), RerankClient()),
            client=GenerationClient(),
            is_emergency=lambda _q: False,
            policy=DisplayPolicy(min_supported_claims=5),
        )
        assert outcome.presentation.answer.action is AnswerAction.ABSTAIN


class TestRungFourIndexFailsClosed:
    """A corrupt or unverifiable index generates nothing at all."""

    def test_a_missing_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ArtifactNotFoundError):
            check_index(tmp_path / "nope")

    def test_a_directory_without_a_manifest_is_reported_as_unmanaged(self, tmp_path: Path) -> None:
        (tmp_path / "index").mkdir()
        result = check_index(tmp_path / "index")
        assert result.status is IndexStatus.UNMANAGED
        assert result.as_metadata()["index_status"] == "unmanaged"

    def test_a_required_manifest_that_is_absent_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "index").mkdir()
        with pytest.raises(ArtifactNotFoundError):
            check_index(tmp_path / "index", require_manifest=True)

    def test_a_corrupt_manifest_is_refused(self, tmp_path: Path) -> None:
        directory = tmp_path / "index"
        directory.mkdir()
        (directory / "manifest.json").write_text("{ not json", encoding="utf-8")
        with pytest.raises((ArtifactSchemaError, ArtifactIntegrityError)):
            check_index(directory)

    def test_a_tampered_manifest_is_refused(self, tmp_path: Path) -> None:
        # Structurally plausible, but the self-digest will not match.
        directory = tmp_path / "index"
        directory.mkdir()
        (directory / "manifest.json").write_text(
            json.dumps({"schema_version": "1.0", "index_id": "tampered"}), encoding="utf-8"
        )
        with pytest.raises((ArtifactSchemaError, ArtifactIntegrityError)):
            check_index(directory)

    def test_an_index_failure_maps_to_a_typed_failure_code(self) -> None:
        failure = classify(ArtifactIntegrityError(path=Path("hybrid_index"), problem="digest"))
        assert failure.code is AnswerFailureCode.INDEX_UNVERIFIED

    def test_an_index_failure_generates_nothing(self) -> None:
        client = GenerationClient()

        def retrieve(_query: str):
            raise ArtifactIntegrityError(path=Path("hybrid_index"), problem="digest mismatch")

        outcome = answer_turn(
            "hba1c", retrieve=retrieve, client=client, is_emergency=lambda _q: False
        )
        assert not outcome.ok
        # RETRIEVAL_FAILED is the outer classification for anything raised by the
        # retrieval callable; what matters is that no generation happened.
        assert client.prompts == []


class TestDegradationPreservesCitationIntegrity:
    """No rung of the ladder relaxes a P1 guarantee."""

    def test_a_degraded_rerank_still_withholds_unsupported_claims(self) -> None:
        outcome = answer_turn(
            "what does hba1c measure?",
            retrieve=a_retriever(
                a_corpus(), RerankClient(error=TimeoutError("t")), RerankConfig(max_attempts=1)
            ),
            client=GenerationClient(FABRICATED_ANSWER),
            is_emergency=lambda _q: False,
        )
        assert outcome.presentation.answer.claims == []
        assert outcome.presentation.answer.action is AnswerAction.ABSTAIN

    def test_a_degraded_rerank_still_verifies_a_good_claim(self) -> None:
        outcome = answer_turn(
            "what does hba1c measure?",
            retrieve=a_retriever(
                a_corpus(), RerankClient(error=TimeoutError("t")), RerankConfig(max_attempts=1)
            ),
            client=GenerationClient(),
            is_emergency=lambda _q: False,
        )
        assert outcome.presentation.answer.claims
        verification = outcome.presentation.result.verification
        assert verification.status_counts()["supported"] == 1

    def test_emergency_routing_still_precedes_retrieval(self) -> None:
        touched: list[str] = []

        def retrieve(query: str):
            touched.append(query)
            return bundle_from_candidates([])

        outcome = answer_turn(
            "crushing chest pain",
            retrieve=retrieve,
            client=GenerationClient(),
            is_emergency=lambda _q: True,
        )
        assert outcome.presentation.action is AnswerAction.EMERGENCY
        assert touched == []

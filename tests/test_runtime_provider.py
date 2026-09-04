"""
The seam between a verified artifact and the application service.

Until ``synapse.service.runtime_provider`` existed, ``RuntimeIndex`` was
referenced only in tests and docstrings: it could load and verify an artifact,
and nothing could hand one to the service. ``serve_api.py`` could therefore
serve only from the legacy prototype index, and a container built from the
locked architecture -- one process, one pinned read-only artifact -- had no way
to start.

Two properties are asserted here, and the second matters as much as the first:

1. **The artifact path works.** A started runtime index becomes a
   ``LoadedIndex`` carrying already-typed backends, and ``RetrievalService``
   uses them.
2. **The legacy path is untouched.** It is what runs locally, what every other
   test exercises, and what ``docs/migration-parity.md`` compares against. A
   change that made the artifact work by altering the prototype's behaviour
   would have broken the thing the parity claim rests on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from synapse.retrieval.backends import (
    CorpusChunk,
    InMemoryDense,
    InMemorySparse,
    hashed_embedder,
)
from synapse.runtime.config import RuntimeArtifactConfig
from synapse.runtime.embedding import EmbeddingMismatchError, OpenAIQueryEmbedder
from synapse.runtime.objectstore import LocalDirectoryStore
from synapse.runtime.readiness import NotReadyError, RuntimeBackends, RuntimeIndex
from synapse.schemas.index import EmbeddingConfig
from synapse.service.governance import EligibilityDecision
from synapse.service.index import LoadedIndex, RetrievalBackends
from synapse.service.retrieval import RetrievalService
from synapse.service.runtime_provider import RuntimeIndexProvider

NOTHING_ENFORCED = EligibilityDecision(document_ids=None, reason="no pack")


def a_corpus() -> list[CorpusChunk]:
    return [
        CorpusChunk(
            chunk_id=f"pubmed:{3_000_000 + i}#0000",
            document_id=f"pubmed:{3_000_000 + i}",
            text=f"hba1c reflects average glucose, record {i}",
            title=f"Paper {i}",
            url=f"https://pubmed.ncbi.nlm.nih.gov/{3_000_000 + i}/",
        )
        for i in range(4)
    ]


def typed_backends() -> RuntimeBackends:
    """A RuntimeBackends whose halves are the in-memory implementations.

    Real ``RuntimeBackends``, so the provider is exercised against the type it
    actually receives; in-memory backends, so no FAISS index or archive has to
    be built to test the wiring.
    """
    chunks = a_corpus()
    embed = hashed_embedder()
    return RuntimeBackends(
        dense=InMemoryDense(chunks, [embed(c.text) for c in chunks], embed),
        sparse=InMemorySparse(chunks),
        corpus=chunks,
        artifact=object(),  # Only carried through; nothing reads it here
    )


class StartedIndex:
    """Stands in for a RuntimeIndex that has completed startup."""

    def __init__(self, backends: RuntimeBackends) -> None:
        self._backends = backends

    @property
    def backends(self) -> RuntimeBackends:
        return self._backends


class ScoringClient:
    """Returns a rerank verdict for every chunk it is shown."""

    def __init__(self, chunk_ids: list[str]) -> None:
        self._chunk_ids = chunk_ids
        self.calls = 0

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        return json.dumps(
            {
                "verdicts": [
                    {"chunk_id": cid, "relevance": 7.0, "rank": rank}
                    for rank, cid in enumerate(self._chunk_ids, start=1)
                ]
            }
        )


class Clients:
    def __init__(self, client: Any) -> None:
        self._client = client

    def for_answer(self) -> Any:
        return self._client

    def for_rerank(self) -> Any:
        return self._client


class TestTheProviderPresentsTheArtifact:
    """A started runtime index becomes a LoadedIndex the service understands."""

    def test_it_returns_typed_backends_and_no_prototype_retriever(self) -> None:
        backends = typed_backends()
        loaded = RuntimeIndexProvider(index=StartedIndex(backends)).load()

        assert isinstance(loaded, LoadedIndex)
        assert isinstance(loaded.backends, RetrievalBackends)
        assert loaded.backends.dense is backends.dense
        assert loaded.backends.sparse is backends.sparse
        # No prototype retriever exists on this path, and nothing may invent one:
        # RetrievalService must read `backends` and never reach for `hybrid`.
        assert loaded.hybrid is None
        # The evidence that verification ran is carried through.
        assert loaded.gate is backends.artifact

    def test_it_refuses_before_startup_has_completed(self, tmp_path: Path) -> None:
        """Against the REAL RuntimeIndex, which is cheap to construct unstarted."""
        index = RuntimeIndex(
            RuntimeArtifactConfig(
                version="1",
                archive_sha256="a" * 64,
                bucket="b",
                key="k.tar.gz",
                cache_root=tmp_path / "cache",
            ),
            LocalDirectoryStore(root=tmp_path),
            lambda _text: [0.0] * 8,
        )
        with pytest.raises(NotReadyError):
            RuntimeIndexProvider(index=index).load()

    def test_the_progress_stage_carries_no_artifact_detail(self) -> None:
        """A bucket name or key on a progress line would be a disclosure."""
        seen: list[str] = []
        RuntimeIndexProvider(index=StartedIndex(typed_backends())).load(
            report=lambda event: seen.append(event.stage.value)
        )
        assert seen, "the provider reported no progress at all"
        for stage in seen:
            assert "bucket" not in stage and ".tar.gz" not in stage


class TestRetrievalUsesWhicheverIndexItWasGiven:
    """Both shapes converge on the same protocols."""

    def test_a_turn_retrieves_through_the_artifact_backends(self) -> None:
        backends = typed_backends()
        chunk_ids = [c.chunk_id for c in a_corpus()]
        client = ScoringClient(chunk_ids)

        service = RetrievalService(
            index=RuntimeIndexProvider(index=StartedIndex(backends)),
            clients=Clients(client),
            api_key="unused-on-this-path",
        )
        bundle = service.retrieve("what does my hba1c mean?", eligibility=NOTHING_ENFORCED)

        assert not bundle.is_empty, "the artifact path returned no evidence"
        assert bundle.source_order
        # The reranker ran, so relevance is present and is its verdict.
        assert set(bundle.relevance.values()) == {0.7}

    def test_the_artifact_path_never_touches_the_prototype_adapters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`hybrid` is None here, so reaching for it would raise.

        Asserted directly rather than trusted: the legacy wrappers derive
        identifiers from prototype chunk objects, and quietly routing artifact
        chunks through them would produce citations for documents that do not
        exist.
        """
        import synapse.retrieval.production as production

        def explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("the artifact path used a legacy adapter")

        monkeypatch.setattr(production, "LegacyDenseBackend", explode)
        monkeypatch.setattr(production, "LegacySparseBackend", explode)

        backends = typed_backends()
        service = RetrievalService(
            index=RuntimeIndexProvider(index=StartedIndex(backends)),
            clients=Clients(ScoringClient([c.chunk_id for c in a_corpus()])),
            api_key="unused",
        )
        assert service.retrieve("hba1c", eligibility=NOTHING_ENFORCED).source_order

    def test_an_index_without_typed_backends_still_uses_the_legacy_adapters(self) -> None:
        """The regression guard for everything that already worked.

        A LoadedIndex with `backends=None` is what the prototype provider
        returns, and it must keep being wrapped exactly as before.
        """
        from synapse.retrieval.production import LegacyDenseBackend, LegacySparseBackend

        class Hybrid:
            vector_store = "the-prototype-vector-store"
            bm25_index = "the-prototype-bm25-index"

        service = RetrievalService(
            index=object(),  # not used: _backends is called directly below
            clients=Clients(None),
            api_key="a-key",
        )
        dense, sparse = service._backends(LoadedIndex(chunks=[], hybrid=Hybrid()))

        assert isinstance(dense, LegacyDenseBackend)
        assert isinstance(sparse, LegacySparseBackend)
        assert dense.store == "the-prototype-vector-store"
        assert dense.api_key == "a-key"
        assert sparse.index == "the-prototype-bm25-index"


class TestTheQueryEmbedderMatchesTheArtifact:
    """A query embedded differently from the corpus returns confident nonsense."""

    def _embedding(self, **overrides: Any) -> EmbeddingConfig:
        fields: dict[str, Any] = {
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimensions": 4,
            "l2_normalized": True,
        }
        fields.update(overrides)
        return EmbeddingConfig(**fields)

    def test_it_refuses_to_embed_before_the_manifest_binds_it(self) -> None:
        with pytest.raises(EmbeddingMismatchError, match="before the manifest"):
            OpenAIQueryEmbedder(api_key="k")("a question")

    def test_it_refuses_a_provider_it_cannot_reproduce(self) -> None:
        """A Cohere-built index queried with OpenAI vectors ranks nothing."""
        embedder = OpenAIQueryEmbedder(api_key="k")
        with pytest.raises(EmbeddingMismatchError):
            embedder.bind(self._embedding(provider="cohere"))

    def test_binding_records_the_manifest_configuration(self) -> None:
        embedder = OpenAIQueryEmbedder(api_key="k")
        embedder.bind(self._embedding())
        assert embedder.embedding is not None
        assert embedder.embedding.model == "text-embedding-3-small"

    def test_a_normalised_artifact_gets_a_unit_length_query(self) -> None:
        """L2 distance ranks equivalently to cosine only if both sides are unit."""
        from synapse.runtime import embedding as module

        vector = module._l2_normalize([3.0, 4.0, 0.0, 0.0])
        assert vector == pytest.approx([0.6, 0.8, 0.0, 0.0])
        # Degenerate input is returned unchanged rather than dividing by zero.
        assert module._l2_normalize([0.0, 0.0]) == [0.0, 0.0]

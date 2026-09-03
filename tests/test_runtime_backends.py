"""
The native backends: same corpus, same tokenizer, no pickle.

Two things are being established here, and they pull in opposite directions.

**Parity.** The native backends must rank the way the legacy stack ranked, or
every measurement taken against the legacy stack silently stops applying. The
tokenizer is the whole of that risk: BM25 state is rebuilt at startup, so a
single filtered character changes which chunks match.

**Independence.** The tests may not *import* the legacy tree to check parity,
because CI has neither ``rank_bm25`` nor ``langchain`` and the package must stay
installable without them.

So parity is established twice, at different strengths:

* a **golden table** that pins :func:`legacy_tokenize`'s behaviour case by case,
  including the hyphens and digits that medical terms depend on. Runs on every
  pull request, needs nothing installed. This is the gate.
* a **direct comparison** against ``Retrieval.bm25_index.tokenize`` and against a
  real ``BM25Okapi`` ranking, which skips cleanly when the prototype's
  dependencies are absent. This is the proof, and it is opt-in because it cannot
  be anything else.

The pure-Python ``BM25Okapi`` reference below is not a reimplementation for
production use — it exists so the always-run tests can exercise the *wiring*
(tokenize, score, rank, resolve) without the real library.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from synapse.errors import ArtifactCompatibilityError, ArtifactNotFoundError
from synapse.index import MANIFEST_FILENAME, read_manifest
from synapse.retrieval.backends import tokenize as evaluation_tokenize
from synapse.retrieval.native import (
    NativeDenseBackend,
    NativeSparseBackend,
    RuntimeCorpus,
    hits_from_positions,
    legacy_tokenize,
)
from tests.runtime_fixtures import DIMENSIONS, build_artifact_dir

# ---------------------------------------------------------------------------
# The tokenizer
# ---------------------------------------------------------------------------

# Each row encodes one decision the legacy tokenizer made. They are pinned
# rather than described, because "matches the legacy behaviour" is only checkable
# against specific inputs.
TOKENIZER_CASES: tuple[tuple[str, list[str]], ...] = (
    ("Metformin", ["metformin"]),  # lowercased
    ("METFORMIN 500MG", ["metformin", "500mg"]),
    ("HbA1c > 7%", ["hba1c"]),  # '>' and '%' become spaces; '7' is one char, dropped
    ("COVID-19", ["covid-19"]),  # hyphen SURVIVES: the whole point
    # '.' becomes a space, so 'E11.9' splits into 'e11' and '9' — and the lone
    # '9' is then dropped as a single character. The tail of an ICD code is
    # therefore NOT searchable. That is a real limitation of the legacy
    # tokenizer, pinned here rather than fixed: parity is this function's whole
    # contract, and changing it would invalidate every measurement taken against
    # the legacy index.
    ("ICD-10 code E11.9", ["icd-10", "code", "e11"]),
    ("SGLT2 inhibitors", ["sglt2", "inhibitors"]),
    ("a I of an", ["of", "an"]),  # single characters dropped, two-character kept
    ("type 2 diabetes", ["type", "diabetes"]),  # bare '2' dropped
    ("", []),
    ("   ", []),
    ("...", []),
    ("well-controlled, non-pregnant adults", ["well-controlled", "non-pregnant", "adults"]),
    ("HbA1c\tover\n2-3 months", ["hba1c", "over", "2-3", "months"]),  # any whitespace splits
)


class TestLegacyTokenizerParity:
    """The tokenizer is frozen behaviour, not a design decision to revisit."""

    @pytest.mark.parametrize(("text", "expected"), TOKENIZER_CASES, ids=lambda v: str(v)[:24])
    def test_the_golden_table_holds(self, text: str, expected: list[str]) -> None:
        assert legacy_tokenize(text) == expected

    def test_hyphens_survive_because_medical_terms_need_them(self) -> None:
        assert "covid-19" in legacy_tokenize("Patients with COVID-19 infection")
        assert "icd-10" in legacy_tokenize("ICD-10 coding")

    def test_single_character_tokens_are_dropped(self) -> None:
        assert legacy_tokenize("a b c dd") == ["dd"]

    def test_it_is_not_the_evaluation_tokenizer(self) -> None:
        """A real distinction, asserted so nobody 'unifies' them.

        ``synapse.retrieval.backends.tokenize`` matches ``[a-z0-9]+`` and so
        splits ``COVID-19`` into two tokens. Using it for the native BM25 index
        would change every ranking involving a hyphenated term.
        """
        assert legacy_tokenize("COVID-19") == ["covid-19"]
        assert evaluation_tokenize("COVID-19") == ["covid", "19"]
        assert legacy_tokenize("COVID-19") != evaluation_tokenize("COVID-19")

    def test_it_matches_the_legacy_implementation(self) -> None:
        """The direct comparison. Skips when the prototype's deps are absent."""
        pytest.importorskip("rank_bm25", reason="legacy tree needs rank_bm25")
        pytest.importorskip(
            "langchain_text_splitters", reason="legacy tree needs langchain_text_splitters"
        )
        from Retrieval.bm25_index import tokenize as legacy_reference

        corpus = [text for text, _ in TOKENIZER_CASES] + [
            "Metformin 500mg is a first-line oral therapy for type 2 diabetes.",
            "ICD-10 code E11.9 refers to type 2 diabetes without complications.",
        ]
        for text in corpus:
            assert legacy_tokenize(text) == legacy_reference(text), text


# ---------------------------------------------------------------------------
# A reference BM25, so the wiring is testable with nothing installed
# ---------------------------------------------------------------------------


class ReferenceBM25:
    """``rank_bm25.BM25Okapi``'s formula and defaults, in pure Python.

    Not a production component and never used by one — it exists so the
    always-run tests can drive :class:`NativeSparseBackend` end to end without
    ``rank_bm25`` present.

    It reproduces **Okapi** BM25, not the Lucene variant, and the difference is
    not cosmetic. Okapi's IDF is ``log(N - df + 0.5) - log(df + 0.5)``, which
    goes *negative* for a term appearing in more than about half the corpus;
    ``BM25Okapi`` then replaces every negative value with
    ``epsilon * average_idf``. The Lucene form (``log(1 + (N - df + 0.5) /
    (df + 0.5))``) is always positive and ranks common terms differently.

    A stand-in that ranks differently from the real thing is worse than no
    stand-in, because the wiring tests would then be exercising a function
    production never runs.
    ``test_the_real_library_agrees_with_the_reference`` is what keeps this
    honest, and it is what caught the Lucene formula being used here first.
    """

    def __init__(
        self,
        corpus: list[list[str]],
        k1: float = 1.5,
        b: float = 0.75,
        epsilon: float = 0.25,
    ) -> None:
        self.corpus = corpus
        self.k1 = k1
        self.b = b
        self.lengths = [len(document) for document in corpus]
        self.average_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.frequencies: list[dict[str, int]] = []
        document_frequency: dict[str, int] = {}
        for document in corpus:
            counts: dict[str, int] = {}
            for token in document:
                counts[token] = counts.get(token, 0) + 1
            self.frequencies.append(counts)
            for token in counts:
                document_frequency[token] = document_frequency.get(token, 0) + 1

        # The negative-IDF floor, exactly as BM25Okapi applies it.
        total = len(corpus)
        self.idf: dict[str, float] = {}
        negative: list[str] = []
        idf_sum = 0.0
        for token, frequency in document_frequency.items():
            value = math.log(total - frequency + 0.5) - math.log(frequency + 0.5)
            self.idf[token] = value
            idf_sum += value
            if value < 0:
                negative.append(token)
        average_idf = idf_sum / len(self.idf) if self.idf else 0.0
        for token in negative:
            self.idf[token] = epsilon * average_idf

    def get_scores(self, query: list[str]) -> list[float]:
        scores = [0.0] * len(self.corpus)
        for token in query:
            idf = self.idf.get(token)
            if idf is None:
                continue
            for position, counts in enumerate(self.frequencies):
                count = counts.get(token, 0)
                if not count:
                    continue
                norm = self.k1 * (
                    1 - self.b + self.b * self.lengths[position] / (self.average_length or 1.0)
                )
                scores[position] += idf * (count * (self.k1 + 1) / (count + norm))
        return scores


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    """A verified runtime artifact directory."""
    return build_artifact_dir(tmp_path / "artifact")


@pytest.fixture
def corpus(artifact: Path) -> RuntimeCorpus:
    """The corpus, loaded the way the container loads it."""
    return RuntimeCorpus.load(artifact, read_manifest(artifact / MANIFEST_FILENAME))


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


class TestRuntimeCorpus:
    """Order is the contract."""

    def test_chunks_load_in_file_order(self, corpus: RuntimeCorpus) -> None:
        assert [chunk.chunk_id for chunk in corpus.chunks] == [
            "pubmed:41802233#0000",
            "pubmed:41802233#0001",
            "pubmed:41900001#0000",
            "pubmed:41900001#0001",
        ]

    def test_the_corpus_is_a_tuple_so_it_cannot_be_sorted_in_place(
        self, corpus: RuntimeCorpus
    ) -> None:
        """An accidental in-place sort would misalign every dense hit."""
        assert isinstance(corpus.chunks, tuple)
        with pytest.raises(AttributeError):
            corpus.chunks.sort()  # type: ignore[attr-defined]

    def test_document_titles_and_links_are_joined(self, corpus: RuntimeCorpus) -> None:
        first = corpus.chunks[0]
        assert first.title == "Metformin and HbA1c in Type 2 Diabetes"
        assert first.url == "https://pubmed.ncbi.nlm.nih.gov/41802233/"

    def test_a_missing_documents_artifact_degrades_visibly(self, tmp_path: Path) -> None:
        """Titles fall back to identifiers rather than being silently wrong."""
        directory = build_artifact_dir(tmp_path / "artifact", include_documents=False)
        loaded = RuntimeCorpus.load(directory, read_manifest(directory / MANIFEST_FILENAME))
        assert loaded.chunks[0].title == "pubmed:41802233"
        assert loaded.chunks[0].url == ""

    def test_a_count_mismatch_is_refused(self, artifact: Path) -> None:
        manifest = read_manifest(artifact / MANIFEST_FILENAME)
        patched = manifest.model_copy(update={"chunk_count": 99})
        with pytest.raises(ArtifactCompatibilityError, match="record count"):
            RuntimeCorpus.load(artifact, patched)


# ---------------------------------------------------------------------------
# Sparse
# ---------------------------------------------------------------------------


def _sparse(corpus: RuntimeCorpus) -> NativeSparseBackend:
    return NativeSparseBackend(corpus=corpus, bm25_factory=ReferenceBM25)


class TestNativeSparseBackend:
    """BM25 rebuilt from the verified corpus, never unpickled."""

    def test_corpus_size_matches(self, corpus: RuntimeCorpus) -> None:
        assert _sparse(corpus).corpus_size == 4

    def test_an_exact_drug_name_ranks_its_chunk_first(self, corpus: RuntimeCorpus) -> None:
        hits = _sparse(corpus).search("metformin", top_n=2)
        assert hits[0].chunk_id == "pubmed:41802233#0000"

    def test_a_hyphenated_code_matches(self, corpus: RuntimeCorpus) -> None:
        """The behaviour the tokenizer choice exists for."""
        hits = _sparse(corpus).search("ICD-10", top_n=1)
        assert hits[0].chunk_id == "pubmed:41900001#0001"

    def test_results_are_bounded_by_top_n(self, corpus: RuntimeCorpus) -> None:
        assert len(_sparse(corpus).search("diabetes", top_n=2)) == 2

    def test_a_non_positive_top_n_returns_nothing(self, corpus: RuntimeCorpus) -> None:
        assert _sparse(corpus).search("diabetes", top_n=0) == []

    def test_ranks_are_one_based_and_contiguous(self, corpus: RuntimeCorpus) -> None:
        hits = _sparse(corpus).search("diabetes", top_n=3)
        assert [hit.rank for hit in hits] == [1, 2, 3]

    def test_hits_carry_verified_identifiers_and_metadata(self, corpus: RuntimeCorpus) -> None:
        """No identifier is derived or repaired here, unlike the legacy adapter."""
        hit = _sparse(corpus).search("metformin", top_n=1)[0]
        assert hit.document_id == "pubmed:41802233"
        assert hit.title == "Metformin and HbA1c in Type 2 Diabetes"
        assert hit.url.startswith("https://pubmed.ncbi.nlm.nih.gov/")

    def test_ranking_is_deterministic_across_instances(self, corpus: RuntimeCorpus) -> None:
        """Ties are broken by chunk_id, not by sort stability."""
        first = [hit.chunk_id for hit in _sparse(corpus).search("diabetes", top_n=4)]
        second = [hit.chunk_id for hit in _sparse(corpus).search("diabetes", top_n=4)]
        assert first == second

    def test_the_real_library_agrees_with_the_reference(self, corpus: RuntimeCorpus) -> None:
        """Opt-in parity: the always-run tests use a stand-in, this uses rank_bm25."""
        pytest.importorskip("rank_bm25", reason="native sparse parity needs rank_bm25")
        native = NativeSparseBackend(corpus=corpus)  # Real BM25Okapi
        reference = _sparse(corpus)
        for query in ("metformin", "diabetes", "ICD-10 E11.9", "cardiovascular mortality"):
            assert [hit.chunk_id for hit in native.search(query, top_n=4)] == [
                hit.chunk_id for hit in reference.search(query, top_n=4)
            ], query

    def test_it_ranks_the_same_as_the_legacy_index(self, corpus: RuntimeCorpus) -> None:
        """The parity that matters: native vs the prototype, same corpus."""
        pytest.importorskip("rank_bm25", reason="legacy parity needs rank_bm25")
        pytest.importorskip(
            "langchain_text_splitters", reason="legacy parity needs langchain_text_splitters"
        )
        from Data.fetch_and_chunk import Chunk
        from Retrieval.bm25_index import BM25Index

        legacy = BM25Index()
        legacy.build(
            [
                Chunk(
                    text=chunk.text,
                    source="pubmed",
                    pmid=chunk.document_id.split(":")[1],
                    title=chunk.title,
                )
                for chunk in corpus.chunks
            ]
        )
        native = NativeSparseBackend(corpus=corpus)
        for query in ("metformin", "diabetes", "ICD-10 E11.9", "cardiovascular mortality"):
            legacy_order = [result["chunk"].text for result in legacy.search(query, top_k=4)]
            native_order = [hit.text for hit in native.search(query, top_n=4)]
            assert native_order == legacy_order, query


# ---------------------------------------------------------------------------
# Dense
# ---------------------------------------------------------------------------


class _StubIndex:
    """A FAISS index with the header attributes the loader checks."""

    def __init__(self, ntotal: int, dimensions: int) -> None:
        self.ntotal = ntotal
        self.d = dimensions


def _embed(_text: str) -> list[float]:
    return [0.0] * DIMENSIONS


class TestNativeDenseBackendLoad:
    """The index is refused unless it can align with the corpus."""

    def test_a_matching_index_loads(self, artifact: Path, corpus: RuntimeCorpus) -> None:
        manifest = read_manifest(artifact / MANIFEST_FILENAME)
        backend = NativeDenseBackend.load(
            artifact, manifest, corpus, _embed, reader=lambda _p: _StubIndex(4, DIMENSIONS)
        )
        assert backend.corpus_size == 4

    def test_a_vector_count_mismatch_refuses_to_load(
        self, artifact: Path, corpus: RuntimeCorpus
    ) -> None:
        """Row i could not correspond to record i, so nothing may be served."""
        manifest = read_manifest(artifact / MANIFEST_FILENAME)
        with pytest.raises(ArtifactCompatibilityError, match="vector count"):
            NativeDenseBackend.load(
                artifact, manifest, corpus, _embed, reader=lambda _p: _StubIndex(3, DIMENSIONS)
            )

    def test_a_dimensionality_mismatch_refuses_to_load(
        self, artifact: Path, corpus: RuntimeCorpus
    ) -> None:
        manifest = read_manifest(artifact / MANIFEST_FILENAME)
        with pytest.raises(ArtifactCompatibilityError, match="dimensionality"):
            NativeDenseBackend.load(
                artifact, manifest, corpus, _embed, reader=lambda _p: _StubIndex(4, 1536)
            )

    def test_a_manifest_with_no_faiss_artifact_refuses(
        self, artifact: Path, corpus: RuntimeCorpus
    ) -> None:
        manifest = read_manifest(artifact / MANIFEST_FILENAME)
        without = manifest.model_copy(
            update={"artifacts": [a for a in manifest.artifacts if a.role != "faiss"]}
        )
        with pytest.raises(ArtifactNotFoundError):
            NativeDenseBackend.load(
                artifact, without, corpus, _embed, reader=lambda _p: _StubIndex(4, DIMENSIONS)
            )


class TestAgainstARealFaissIndex:
    """The one path a stub cannot exercise: numpy in, FAISS out, rows resolved.

    Opt-in, because ``faiss-cpu`` and ``numpy`` are the ``runtime`` extra and CI
    installs neither. Everything above tests the *decisions*; this tests that the
    calls are actually shaped the way FAISS expects — which a stub index, by
    construction, cannot tell you.
    """

    def _real_index(self, tmp_path: Path, corpus: RuntimeCorpus):
        faiss = pytest.importorskip("faiss", reason="needs the runtime extra")
        np = pytest.importorskip("numpy", reason="needs the runtime extra")

        # One L2-normalised vector per chunk, in corpus order. Deterministic and
        # meaningless: the point is row alignment, not retrieval quality.
        vectors = np.zeros((len(corpus), DIMENSIONS), dtype="float32")
        for position in range(len(corpus)):
            vectors[position][position % DIMENSIONS] = 1.0
        index = faiss.IndexFlatL2(DIMENSIONS)
        index.add(vectors)
        path = tmp_path / "real.faiss"
        faiss.write_index(index, str(path))
        return path, vectors

    def test_search_returns_the_chunk_at_the_matching_row(
        self, tmp_path: Path, corpus: RuntimeCorpus, artifact: Path
    ) -> None:
        path, vectors = self._real_index(tmp_path, corpus)
        manifest = read_manifest(artifact / MANIFEST_FILENAME)

        import faiss

        backend = NativeDenseBackend(
            corpus=corpus,
            # Query with row 2's own vector: its nearest neighbour is itself.
            embed=lambda _q: list(vectors[2]),
            index=faiss.read_index(str(path)),
        )
        hits = backend.search("anything", top_n=2)
        assert hits[0].chunk_id == corpus.chunks[2].chunk_id
        assert hits[0].score == pytest.approx(0.0, abs=1e-5)
        assert manifest.embedding.dimensions == DIMENSIONS

    def test_top_n_is_bounded_by_the_corpus(self, tmp_path: Path, corpus: RuntimeCorpus) -> None:
        """Asking for more neighbours than exist must not produce -1 padding hits."""
        path, vectors = self._real_index(tmp_path, corpus)

        import faiss

        backend = NativeDenseBackend(
            corpus=corpus, embed=lambda _q: list(vectors[0]), index=faiss.read_index(str(path))
        )
        hits = backend.search("anything", top_n=99)
        assert len(hits) == len(corpus)
        assert all(hit.chunk_id for hit in hits)


class TestPositionResolution:
    """Where a wrong row would silently become a wrong citation."""

    def test_positions_resolve_to_the_corresponding_chunks(self, corpus: RuntimeCorpus) -> None:
        hits = hits_from_positions(corpus, [0.1, 0.2], [2, 0])
        assert [hit.chunk_id for hit in hits] == [
            "pubmed:41900001#0000",
            "pubmed:41802233#0000",
        ]

    def test_faiss_padding_is_skipped_not_treated_as_an_index(self, corpus: RuntimeCorpus) -> None:
        """``-1`` would wrap to the last chunk and cite a document nobody read."""
        hits = hits_from_positions(corpus, [0.1, 3.4e38], [0, -1])
        assert len(hits) == 1
        assert hits[0].chunk_id == "pubmed:41802233#0000"

    def test_scores_are_l2_distances_carried_through_unchanged(self, corpus: RuntimeCorpus) -> None:
        hits = hits_from_positions(corpus, [0.25], [1])
        assert hits[0].score == pytest.approx(0.25)

    def test_ranks_follow_the_returned_order(self, corpus: RuntimeCorpus) -> None:
        hits = hits_from_positions(corpus, [0.1, 0.2, 0.3], [3, 1, 0])
        assert [hit.rank for hit in hits] == [1, 2, 3]

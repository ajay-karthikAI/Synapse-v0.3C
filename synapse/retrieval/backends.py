"""
synapse.retrieval.backends
==========================
What a retrieval component must provide, and deterministic fakes for tests.

The protocols are narrow on purpose. A backend answers one question — "your best
``top_n`` for this query" — and knows nothing about fusion, eligibility,
reranking or answers. That is what lets the whole retrieval path run offline
against the in-memory implementations below, with no FAISS, no BM25 library and
no embedding API.

``corpus_size`` is part of the protocol rather than an implementation detail,
because the guard in :mod:`synapse.retrieval.search` needs it: a request for
``top_n >= corpus_size`` is the defect this milestone removes, and a guard that
cannot see the corpus size cannot catch it.
"""

from __future__ import annotations  # Postponed annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from synapse.retrieval.candidates import Hit

_TOKEN = re.compile(r"[a-z0-9]+")


class DenseBackend(Protocol):
    """Dense (vector) retrieval over the corpus."""

    @property
    def corpus_size(self) -> int:
        """Number of chunks in the index."""
        ...

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Return at most ``top_n`` hits, ranked best-first."""
        ...


class SparseBackend(Protocol):
    """Sparse (lexical) retrieval over the corpus."""

    @property
    def corpus_size(self) -> int:
        """Number of chunks in the index."""
        ...

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Return at most ``top_n`` hits, ranked best-first."""
        ...


@dataclass(frozen=True)
class CorpusChunk:
    """A chunk as the retrieval layer sees it."""

    chunk_id: str
    document_id: str
    text: str
    title: str = ""
    url: str = ""


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens.

    Deliberately simple and versioned by this function's identity: the same
    tokenizer must be used to build and to query, and the benchmark records
    which one ran.
    """
    return _TOKEN.findall(text.lower())


@dataclass
class InMemorySparse:
    """A small, exact BM25 implementation over an in-memory corpus.

    Written here rather than depending on ``rank_bm25`` so the retrieval layer —
    and every test of it — installs with nothing but pydantic. The formula is
    BM25 with the standard defaults (k1=1.5, b=0.75); it is used for tests and
    for the benchmark's synthetic corpus, never as a replacement for the
    production index.
    """

    chunks: Sequence[CorpusChunk]
    k1: float = 1.5
    b: float = 0.75
    _postings: dict[str, dict[int, int]] = field(default_factory=dict, init=False)
    _lengths: list[int] = field(default_factory=list, init=False)
    _average_length: float = field(default=0.0, init=False)
    calls: int = field(default=0, init=False)  # Instrumentation for the benchmark
    last_top_n: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Build the inverted index once."""
        for position, chunk in enumerate(self.chunks):
            tokens = tokenize(chunk.text)
            self._lengths.append(len(tokens))
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, count in counts.items():
                self._postings.setdefault(token, {})[position] = count
        self._average_length = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0

    @property
    def corpus_size(self) -> int:
        """Number of chunks indexed."""
        return len(self.chunks)

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Score only the chunks that share a term with the query.

        Scoring by posting list rather than over the whole corpus is the sparse
        half of this milestone: the legacy path called ``get_all_scores``, which
        produces a score for every chunk whether or not it shares a single term.
        """
        self.calls += 1
        self.last_top_n = top_n
        if top_n <= 0:
            return []
        scores: dict[int, float] = {}
        for token in set(tokenize(query)):
            postings = self._postings.get(token)
            if not postings:
                continue
            document_frequency = len(postings)
            idf = math.log(
                1.0 + (self.corpus_size - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for position, count in postings.items():
                length_norm = self.k1 * (
                    1 - self.b + self.b * self._lengths[position] / (self._average_length or 1.0)
                )
                scores[position] = scores.get(position, 0.0) + idf * (
                    count * (self.k1 + 1) / (count + length_norm)
                )
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.chunks[item[0]].chunk_id))
        return [
            Hit(
                chunk_id=self.chunks[position].chunk_id,
                document_id=self.chunks[position].document_id,
                text=self.chunks[position].text,
                title=self.chunks[position].title,
                url=self.chunks[position].url,
                rank=rank,
                score=score,
            )
            for rank, (position, score) in enumerate(ranked[:top_n], start=1)
        ]


@dataclass
class InMemoryDense:
    """Dense retrieval over precomputed vectors, with an injected embedder.

    The embedder is a plain callable, so tests and benchmarks supply a
    deterministic hash-based one and production supplies the real API. Distances
    are L2, matching FAISS, so the fusion code sees the same score semantics
    either way.
    """

    chunks: Sequence[CorpusChunk]
    vectors: Sequence[Sequence[float]]
    embed: object  # Callable[[str], Sequence[float]]; typed loosely to keep the dependency direction clean
    calls: int = field(default=0, init=False)
    last_top_n: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Vectors and chunks must correspond position-by-position."""
        if len(self.chunks) != len(self.vectors):
            raise ValueError("dense backend requires one vector per chunk")

    @property
    def corpus_size(self) -> int:
        """Number of chunks indexed."""
        return len(self.chunks)

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Exact nearest neighbours by L2 distance."""
        self.calls += 1
        self.last_top_n = top_n
        if top_n <= 0:
            return []
        query_vector = self.embed(query)  # type: ignore[operator]
        distances = [
            (position, _l2(query_vector, vector)) for position, vector in enumerate(self.vectors)
        ]
        ranked = sorted(distances, key=lambda item: (item[1], self.chunks[item[0]].chunk_id))
        return [
            Hit(
                chunk_id=self.chunks[position].chunk_id,
                document_id=self.chunks[position].document_id,
                text=self.chunks[position].text,
                title=self.chunks[position].title,
                url=self.chunks[position].url,
                rank=rank,
                score=distance,
            )
            for rank, (position, distance) in enumerate(ranked[:top_n], start=1)
        ]


def _l2(left: Sequence[float], right: Sequence[float]) -> float:
    """Euclidean distance between two vectors."""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def hashed_embedder(dimensions: int = 64) -> object:
    """A deterministic, offline embedder for tests and benchmarks.

    Token hashes are folded into a fixed-width vector and L2-normalised. It has
    no semantics whatsoever — it is not a model and must never be used to serve
    a patient — but it is *stable*: the same text always yields the same vector,
    on any machine, with no network. That is exactly what a reproducible
    benchmark needs, and it lets the dense path be exercised in CI.
    """

    def embed(text: str) -> list[float]:
        vector = [0.0] * dimensions
        for token in tokenize(text):
            # Python's hash() is salted per process; a stable digest is required
            # for the benchmark to reproduce across runs and machines.
            digest = _stable_hash(token)
            vector[digest % dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    return embed


def _stable_hash(token: str) -> int:
    """FNV-1a: small, dependency-free, and stable across processes."""
    digest = 0x811C9DC5
    for byte in token.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest


def chunks_from_mapping(texts: Mapping[str, str]) -> list[CorpusChunk]:
    """Build a corpus from a ``{chunk_id: text}`` mapping, as the eval fixtures use."""
    return [
        CorpusChunk(chunk_id=chunk_id, document_id=chunk_id.split("#", 1)[0], text=text)
        for chunk_id, text in sorted(texts.items())
    ]


__all__ = [
    "CorpusChunk",
    "DenseBackend",
    "InMemoryDense",
    "InMemorySparse",
    "SparseBackend",
    "chunks_from_mapping",
    "hashed_embedder",
    "tokenize",
]

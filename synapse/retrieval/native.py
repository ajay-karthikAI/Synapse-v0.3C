"""
synapse.retrieval.native
========================
Retrieval over a verified artifact, with no pickle and no legacy tree.

The replacement for :mod:`synapse.retrieval.production`. Those adapters wrap
objects the prototype built by unpickling ``processed_chunks.pkl`` and
``bm25.pkl``; these load a manifest-verified corpus from ordered JSONL and an
index from ``index.faiss``, and are typed all the way through.

The ordering contract
---------------------
Everything here depends on one invariant: **FAISS row *i* corresponds to corpus
record *i*.** :func:`synapse.index.verify.verify_artifacts` proves it for the
artifact as a whole by recomputing the ordering digest; :meth:`RuntimeCorpus.load`
re-establishes it in memory by reading the JSONL in file order and never
sorting; :meth:`NativeDenseBackend.load` refuses to start if the vector count
and the chunk count disagree. A dense hit is resolved by *position*, so a
mismatch here is the exact failure mode — a citation pointing at a document
nobody read — that the whole artifact layer exists to prevent.

Why the tokenizer is copied rather than imported
------------------------------------------------
:func:`legacy_tokenize` is a character-for-character reimplementation of
``Retrieval/bm25_index.tokenize``. It is duplicated on purpose:

* ``synapse`` may not import the legacy tree, and this module is on the serving
  path;
* importing it would drag in ``Data.fetch_and_chunk`` and therefore langchain,
  which the package is deliberately installable without;
* and the point is not to share code but to **freeze behaviour**. The BM25 state
  is rebuilt at startup from the corpus, so if this tokenizer differed from the
  one that built the legacy index by even a filtered character, the native and
  legacy backends would rank differently and every measurement taken against the
  legacy stack would silently stop applying.

``tests/test_runtime_backends.py`` pins it against a table of cases, and an
opt-in test compares it directly with the legacy function when the prototype's
dependencies are installed.

Note that this is **not** :func:`synapse.retrieval.backends.tokenize`, which
matches ``[a-z0-9]+`` and drops hyphens. That one is for the in-memory test
corpus. Using it here would break ``COVID-19``, ``SGLT2`` and ``HbA1c`` — the
exact terms BM25 exists to catch.
"""

from __future__ import annotations  # Postponed annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.corpus.jsonl import read_jsonl
from synapse.errors import ArtifactCompatibilityError, ArtifactNotFoundError
from synapse.evidence.links import canonical_link
from synapse.index.verify import CHUNKS_ROLE, FAISS_ROLE
from synapse.logging import get_logger
from synapse.retrieval.backends import CorpusChunk
from synapse.retrieval.candidates import Hit
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.index import IndexManifest
from synapse.schemas.source import SourceDocument

logger = get_logger(__name__)

DOCUMENTS_ROLE = "documents"

# The legacy pattern, unchanged: strip everything that is not a word character,
# whitespace, or a hyphen. `\w` is Unicode-aware in Python 3, as it was there.
_LEGACY_PUNCTUATION = re.compile(r"[^\w\s\-]")


def legacy_tokenize(text: str) -> list[str]:
    """Tokenize exactly as ``Retrieval/bm25_index.tokenize`` does.

    Lowercase, replace punctuation (keeping hyphens) with spaces, split on
    whitespace, drop single-character tokens.

    Hyphens survive because medical identifiers depend on them — ``COVID-19``,
    ``ICD-10``, ``E11.9`` — and single-character tokens are dropped because the
    legacy index was built that way. Neither choice is re-litigated here: this
    function's contract is to match, not to improve.
    """
    lowered = text.lower()
    without_punctuation = _LEGACY_PUNCTUATION.sub(" ", lowered)
    return [token for token in without_punctuation.split() if len(token) > 1]


@dataclass(frozen=True)
class RuntimeCorpus:
    """The verified corpus, in manifest order, with document metadata joined.

    ``chunks`` is a tuple rather than a list because position *is* the contract:
    an accidental in-place sort would misalign every dense hit, and a tuple
    makes that impossible rather than merely unlikely.
    """

    chunks: tuple[CorpusChunk, ...]

    def __len__(self) -> int:
        """Number of chunks."""
        return len(self.chunks)

    @classmethod
    def load(cls, directory: Path, manifest: IndexManifest) -> RuntimeCorpus:
        """Read the ordered corpus and join document titles and links.

        Raises:
            ArtifactCompatibilityError: the manifest declares no corpus, or the
                record count disagrees with the manifest.
        """
        chunk_artifact = manifest.artifact_for(CHUNKS_ROLE)
        if chunk_artifact is None:
            raise ArtifactCompatibilityError(
                problem="manifest declares no 'chunks' artifact", index_id=manifest.index_id
            )
        # File order, never sorted. See the module docstring.
        records = list(read_jsonl(directory / chunk_artifact.path, EvidenceChunk))
        if len(records) != manifest.chunk_count:
            raise ArtifactCompatibilityError(
                problem="corpus record count does not match the manifest",
                expected=manifest.chunk_count,
                actual=len(records),
            )

        documents = cls._load_documents(directory, manifest)
        chunks = tuple(
            CorpusChunk(
                chunk_id=record.chunk_id,
                document_id=record.document_id,
                text=record.text,
                title=documents.get(record.document_id, ("", ""))[0] or record.document_id,
                url=documents.get(record.document_id, ("", ""))[1],
            )
            for record in records
        )
        logger.info(
            "runtime corpus loaded",
            extra={"chunks": len(chunks), "documents": len(documents)},
        )
        return cls(chunks=chunks)

    @staticmethod
    def _load_documents(directory: Path, manifest: IndexManifest) -> dict[str, tuple[str, str]]:
        """Map ``document_id -> (title, url)``.

        Absent documents are not an error: the corpus is what grounds an answer,
        and a chunk whose document record is missing still carries verifiable
        text. It renders with its identifier in place of a title, which is
        visibly degraded rather than silently wrong.
        """
        artifact = manifest.artifact_for(DOCUMENTS_ROLE)
        if artifact is None:
            return {}
        try:
            records = list(read_jsonl(directory / artifact.path, SourceDocument))
        except ArtifactNotFoundError:
            logger.warning("documents artifact declared but absent; titles will degrade")
            return {}
        return {
            record.document_id: (
                record.title,
                # Prefer the canonical resolver over whatever URL was recorded,
                # and drop anything that is not http(s) — a citation link is
                # attacker-influenced content from an external feed.
                canonical_link(
                    record.source_url,
                    doi=record.identifiers.doi or "",
                    pmid=record.identifiers.pmid or "",
                ),
            )
            for record in records
        }


@dataclass
class NativeSparseBackend:
    """BM25 over the verified corpus, rebuilt at startup.

    ``rank_bm25`` state is **recomputed**, never loaded: the legacy path
    unpickled ``bm25.pkl``, and a pickle is arbitrary code execution with a
    file extension. Rebuilding costs a tokenization pass over the corpus at
    container start and removes the format from the deployed runtime entirely.

    ``BM25Okapi``'s defaults (k1=1.5, b=0.75, epsilon=0.25) are used, matching
    the legacy index, which also took them.
    """

    corpus: RuntimeCorpus
    bm25_factory: Callable[[list[list[str]]], Any] | None = None
    _bm25: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Tokenize the corpus and build the index."""
        tokenized = [legacy_tokenize(chunk.text) for chunk in self.corpus.chunks]
        factory = self.bm25_factory or _default_bm25_factory
        self._bm25 = factory(tokenized)
        logger.info(
            "native BM25 index built",
            extra={
                "chunks": len(tokenized),
                "avg_tokens": (
                    sum(len(tokens) for tokens in tokenized) / len(tokenized) if tokenized else 0.0
                ),
            },
        )

    @property
    def corpus_size(self) -> int:
        """Number of chunks indexed."""
        return len(self.corpus)

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Bounded lexical query, ranked best-first.

        Scores every chunk and takes the top ``top_n``, because that is what
        ``rank_bm25`` offers — but returns only ``top_n``, so the cost the
        retrieval milestone removed (a full score vector crossing the API
        boundary on every query) stays removed.
        """
        if top_n <= 0:
            return []
        scores = self._bm25.get_scores(legacy_tokenize(query))
        # Ties broken by chunk_id so the ranking is deterministic across runs
        # and machines; the legacy path left ties to sort stability.
        ranked = sorted(
            range(len(scores)),
            key=lambda position: (-float(scores[position]), self.corpus.chunks[position].chunk_id),
        )[:top_n]
        return [
            _hit(self.corpus.chunks[position], rank, float(scores[position]))
            for rank, position in enumerate(ranked, start=1)
        ]


@dataclass
class NativeDenseBackend:
    """Exact nearest-neighbour search over the verified FAISS index.

    Scores are **L2 distances**, lower being nearer, matching both the legacy
    ``VectorStore`` and :class:`~synapse.retrieval.backends.InMemoryDense`. The
    indexed vectors are L2-normalised, which is what makes L2 distance rank
    equivalently to cosine similarity; the embedder must normalise queries the
    same way or the geometry does not correspond.
    """

    corpus: RuntimeCorpus
    embed: Callable[[str], Sequence[float]]
    index: Any = field(repr=False)

    @classmethod
    def load(
        cls,
        directory: Path,
        manifest: IndexManifest,
        corpus: RuntimeCorpus,
        embed: Callable[[str], Sequence[float]],
        *,
        reader: Callable[[Path], Any] | None = None,
    ) -> NativeDenseBackend:
        """Load the index and refuse to start if it does not match the corpus.

        Raises:
            ArtifactCompatibilityError: the vector count or dimensionality
                disagrees with the corpus or the manifest.
            ArtifactNotFoundError: the manifest declares no FAISS artifact.
        """
        artifact = manifest.artifact_for(FAISS_ROLE)
        if artifact is None:
            raise ArtifactNotFoundError(artifact=FAISS_ROLE, directory=directory)
        index = (reader or _read_faiss_index)(directory / artifact.path)

        total = int(getattr(index, "ntotal", -1))
        dimensions = int(getattr(index, "d", -1))
        if total != len(corpus):
            # The ordering digest proved the corpus is in the right order; this
            # proves the index has the right number of rows to align with it.
            raise ArtifactCompatibilityError(
                problem="FAISS vector count does not match the corpus",
                vector_count=total,
                chunk_count=len(corpus),
            )
        if dimensions != manifest.embedding.dimensions:
            raise ArtifactCompatibilityError(
                problem="FAISS dimensionality does not match the manifest",
                index_dimensions=dimensions,
                manifest_dimensions=manifest.embedding.dimensions,
            )
        logger.info(
            "native FAISS index loaded",
            extra={"vectors": total, "dimensions": dimensions},
        )
        return cls(corpus=corpus, embed=embed, index=index)

    @property
    def corpus_size(self) -> int:
        """Number of vectors, which equals the number of chunks."""
        return len(self.corpus)

    def search(self, query: str, top_n: int) -> list[Hit]:
        """Bounded nearest-neighbour query."""
        if top_n <= 0:
            return []
        import numpy as np  # Optional dependency, imported on the hot path only

        vector = np.asarray([self.embed(query)], dtype="float32")
        distances, positions = self.index.search(vector, min(top_n, len(self.corpus)))
        return hits_from_positions(self.corpus, distances[0], positions[0])


def hits_from_positions(
    corpus: RuntimeCorpus, distances: Sequence[float], positions: Sequence[int]
) -> list[Hit]:
    """Resolve FAISS row numbers to corpus chunks, in returned order.

    Separated from :meth:`NativeDenseBackend.search` so the resolution — the
    step where a wrong row silently becomes a wrong citation — is testable
    without numpy or FAISS installed.

    A position of ``-1`` is FAISS padding for "fewer neighbours than requested"
    and is skipped rather than treated as an index, which would wrap to the last
    chunk in the corpus.
    """
    hits: list[Hit] = []
    for rank, (distance, position) in enumerate(zip(distances, positions, strict=True), start=1):
        row = int(position)
        if row == -1:
            continue
        hits.append(_hit(corpus.chunks[row], rank, float(distance)))
    return hits


def _hit(chunk: CorpusChunk, rank: int, score: float) -> Hit:
    """Build a :class:`Hit` from a verified corpus chunk.

    No identifier is derived, repaired or dropped here — unlike the legacy
    adapters, which had to rebuild identifiers because the prototype's were
    ambiguous. These came from a manifest whose ordering digest verified.
    """
    return Hit(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        text=chunk.text,
        title=chunk.title,
        url=chunk.url,
        rank=rank,
        score=score,
    )


def _default_bm25_factory(tokenized: list[list[str]]) -> Any:
    """Build a ``rank_bm25.BM25Okapi``. Imported lazily; optional dependency."""
    from rank_bm25 import BM25Okapi

    return BM25Okapi(tokenized)


def _read_faiss_index(path: Path) -> Any:
    """Read a FAISS index from disk. Imported lazily; optional dependency."""
    import faiss

    if not path.is_file():
        raise ArtifactNotFoundError(artifact=FAISS_ROLE, path=path)
    return faiss.read_index(str(path))


__all__ = [
    "DOCUMENTS_ROLE",
    "NativeDenseBackend",
    "NativeSparseBackend",
    "RuntimeCorpus",
    "hits_from_positions",
    "legacy_tokenize",
]

"""
synapse.ingest.chunker
======================
Section-aware, sentence-preserving chunking.

The legacy pipeline fed a document's abstract to a character-based recursive
splitter with a 500-character window and 100-character overlap. Three
consequences, all visible in the shipped corpus:

* **Sections were blended.** Because the whole abstract was one string, a
  window could straddle the boundary between METHODS and CONCLUSIONS, producing
  a passage whose two halves make claims of entirely different strength.
* **Sentences were cut.** The splitter's last-resort separator is a space, so
  chunks ended mid-clause. A truncated clause embeds ambiguously and reads
  badly when quoted back to a patient as evidence.
* **Degenerate chunks survived.** The corpus contains chunks whose entire text
  is ``"."``.

This chunker instead:

1. **Never mixes sections.** Each abstract section is chunked independently,
   so a passage always belongs to exactly one labelled section and carries that
   label as metadata for citation.
2. **Splits on sentence boundaries**, packing whole sentences up to a target
   size. A single sentence longer than the target is emitted whole rather than
   cut — an over-long complete sentence is more useful than two fragments.
3. **Carries parent metadata** — document identifier, section label, ordinal —
   on every chunk.
4. **Produces deterministic identifiers** via ``make_chunk_id``, numbered
   contiguously across the document so they are unique by construction.
"""

from __future__ import annotations  # Postponed annotations

import re  # Sentence segmentation
from dataclasses import dataclass
from datetime import datetime

from synapse.identifiers import make_chunk_id
from synapse.logging import get_logger
from synapse.normalize import normalize_text
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import SourceType
from synapse.schemas.source import SourceDocument

logger = get_logger(__name__)

DEFAULT_TARGET_CHARS = 800  # Target chunk size. Larger than the legacy 500 because whole sentences are kept and a section is not split unnecessarily.
DEFAULT_MIN_CHARS = (
    120  # Below this a passage carries too little context to retrieve or cite meaningfully.
)
DEFAULT_MAX_CHARS = 1600  # Hard ceiling; a single sentence longer than this is still emitted whole, but is reported.

# Abbreviations that end in a period but do not end a sentence. Kept small and
# explicit: an over-eager list silently merges real sentences, which is worse
# than an occasional false split.
_ABBREVIATIONS = frozenset(
    {
        "dr",
        "prof",
        "mr",
        "mrs",
        "ms",
        "vs",
        "etc",
        "eg",
        "ie",
        "cf",
        "al",
        "fig",
        "figs",
        "no",
        "approx",
        "ca",
        "st",
        "jr",
        "sr",
        "inc",
        "ltd",
        "min",
        "max",
        "sd",
        "se",
        "ci",
        "iv",
        "im",
        "po",
        "bid",
        "tid",
        "qid",
    }
)

# A sentence ends at . ! or ? followed by whitespace and something that starts
# a new sentence (capital, digit, or an opening bracket/quote). The lookahead
# keeps decimals such as "7.5%" and ratios such as "1.5 mg" intact.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]*[A-Z0-9])")


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences, preserving boundaries.

    Deliberately regex-based rather than model-based: chunk identifiers are
    derived from the resulting boundaries, so segmentation must be perfectly
    reproducible across machines and versions, with no downloaded model and no
    extra dependency.
    """
    if not text:
        return []
    candidates = _SENTENCE_BOUNDARY.split(
        text
    )  # Provisional split; may over-split after an abbreviation

    merged: list[str] = []
    for candidate in candidates:
        piece = candidate.strip()
        if not piece:
            continue
        if merged:
            previous = merged[-1]
            last_word = (
                previous.rstrip(".").split()[-1].lower() if previous.rstrip(".").split() else ""
            )
            # Re-join when the previous fragment ended on a known abbreviation
            # ("e.g.", "vs.") or on a single initial ("J."), neither of which
            # actually terminates a sentence.
            if last_word in _ABBREVIATIONS or (len(last_word) == 1 and last_word.isalpha()):
                merged[-1] = f"{previous} {piece}"
                continue
        merged.append(piece)
    return merged


@dataclass(frozen=True)
class ChunkingPolicy:
    """Chunking parameters, recorded in the index manifest alongside the corpus."""

    target_chars: int = DEFAULT_TARGET_CHARS
    min_chars: int = DEFAULT_MIN_CHARS
    max_chars: int = DEFAULT_MAX_CHARS
    keep_short_sections: bool = (
        False  # When True, a section below min_chars is emitted anyway rather than dropped
    )

    def __post_init__(self) -> None:
        """Reject a policy that cannot produce sensible chunks."""
        if self.min_chars > self.target_chars:  # Every chunk would be dropped as too short
            raise ValueError("min_chars must not exceed target_chars")
        if (
            self.target_chars > self.max_chars
        ):  # The ceiling would bind before the target, making target meaningless
            raise ValueError("target_chars must not exceed max_chars")


@dataclass
class ChunkingResult:
    """Chunks produced for one document, plus what was discarded."""

    chunks: list[EvidenceChunk]
    dropped_short_sections: int = 0  # Sections below min_chars that were discarded
    oversized_sentences: int = 0  # Single sentences exceeding max_chars, emitted whole


def _pack_sentences(sentences: list[str], policy: ChunkingPolicy) -> tuple[list[str], int]:
    """Pack whole sentences into passages of roughly ``target_chars``.

    Never splits a sentence. A sentence that alone exceeds ``max_chars`` is
    emitted as its own passage and counted, because cutting it would produce
    exactly the mid-clause fragments this chunker exists to avoid.
    """
    passages: list[str] = []
    oversized = 0
    current: list[str] = []
    current_length = 0

    for sentence in sentences:
        sentence_length = len(sentence)
        if (
            sentence_length > policy.max_chars
        ):  # Too long to combine with anything; flush and emit alone
            if current:
                passages.append(" ".join(current))
                current, current_length = [], 0
            passages.append(sentence)
            oversized += 1
            continue
        # +1 accounts for the space that will join this sentence to the previous one.
        if current and current_length + sentence_length + 1 > policy.target_chars:
            passages.append(" ".join(current))
            current, current_length = [], 0
        current.append(sentence)
        current_length += sentence_length + (1 if current_length else 0)

    if current:  # Flush the final partial passage
        passages.append(" ".join(current))
    return passages, oversized


def chunk_document(
    document: SourceDocument,
    *,
    ingested_at: datetime,
    policy: ChunkingPolicy | None = None,
) -> ChunkingResult:
    """Chunk one document section by section.

    Offsets are computed against the document's reconstructed full text —
    sections joined in order with a single space — which is the same string
    used for ``SourceDocument.content_sha256``, so a chunk's span always
    resolves within its parent.
    """
    policy = policy or ChunkingPolicy()
    chunks: list[EvidenceChunk] = []
    dropped_short = 0
    oversized_total = 0

    document_offset = 0  # Running position in the reconstructed document text
    ordinal = (
        0  # Chunk ordinal, contiguous ACROSS sections so identifiers are unique within the document
    )

    for section in document.abstract_sections:
        section_start = document_offset
        document_offset += (
            len(section.text) + 1
        )  # +1 for the joining space, matching the reconstruction

        if len(section.text) < policy.min_chars and not policy.keep_short_sections:
            # A section shorter than the minimum is dropped rather than merged
            # into a neighbour: merging would blend two labelled sections,
            # which is precisely what this chunker must not do.
            dropped_short += 1
            logger.debug(
                "dropping short abstract section",
                extra={
                    "document_id": document.document_id,
                    "section": section.label,
                    "length": len(section.text),
                },
            )
            continue

        sentences = split_sentences(section.text)
        passages, oversized = _pack_sentences(sentences, policy)
        oversized_total += oversized

        cursor = section_start  # Position within the document text for this section's passages
        for passage in passages:
            normalized = normalize_text(passage)
            if not normalized:  # Defensive: a passage that normalises away carries nothing
                continue
            start = cursor
            end = start + len(normalized)
            cursor = end + 1  # +1 for the space between packed passages
            chunks.append(
                EvidenceChunk.build(
                    document_id=document.document_id,
                    ordinal=ordinal,  # Continues across sections, so a document with three sections numbers 0..n without restarting
                    text=normalized,
                    char_start=start,
                    char_end=end,
                    source_type=SourceType.PUBMED_ABSTRACT,
                    ingested_at=ingested_at,
                    section=section.label,  # Parent section metadata travels with the chunk for citation
                )
            )
            ordinal += 1

    return ChunkingResult(
        chunks=chunks, dropped_short_sections=dropped_short, oversized_sentences=oversized_total
    )


def chunk_documents(
    documents: list[SourceDocument],
    *,
    ingested_at: datetime,
    policy: ChunkingPolicy | None = None,
) -> tuple[list[EvidenceChunk], dict[str, int]]:
    """Chunk every document, returning the chunks and aggregate statistics."""
    policy = policy or ChunkingPolicy()
    all_chunks: list[EvidenceChunk] = []
    stats = {
        "documents_chunked": 0,
        "documents_without_chunks": 0,
        "dropped_short_sections": 0,
        "oversized_sentences": 0,
    }

    for document in documents:
        result = chunk_document(document, ingested_at=ingested_at, policy=policy)
        stats["dropped_short_sections"] += result.dropped_short_sections
        stats["oversized_sentences"] += result.oversized_sentences
        if result.chunks:
            all_chunks.extend(result.chunks)
            stats["documents_chunked"] += 1
        else:  # A document whose abstract was absent or entirely below the minimum
            stats["documents_without_chunks"] += 1

    logger.info("chunking complete", extra={"chunk_count": len(all_chunks), **stats})
    return all_chunks, stats


def chunk_ids_for(document: SourceDocument, chunks: list[EvidenceChunk]) -> list[str]:
    """Return this document's chunk identifiers, in emission order."""
    return [chunk.chunk_id for chunk in chunks if chunk.document_id == document.document_id]


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MIN_CHARS",
    "DEFAULT_TARGET_CHARS",
    "ChunkingPolicy",
    "ChunkingResult",
    "chunk_document",
    "chunk_documents",
    "chunk_ids_for",
    "make_chunk_id",
    "split_sentences",
]

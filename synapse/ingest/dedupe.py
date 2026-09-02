"""
synapse.ingest.dedupe
=====================
Three-stage deduplication, with every decision recorded.

Why this exists: the legacy build ran ~100 topic queries and concatenated the
results without any deduplication. The same article matched several topics, so
it entered the corpus repeatedly. Measured on the shipped artifact: 2,220
chunks collapse to 2,058 unique legacy identifiers — 162 collisions — and 163
chunk texts are byte-identical to an earlier chunk.

Match order, strongest identifier first:

1. **PMID** — PubMed's own primary key. An exact match is the same record.
2. **DOI** — publisher's identifier, normalised for case and resolver prefix,
   so ``10.1000/ABC`` and ``https://doi.org/10.1000/abc`` collide as intended.
   Catches the same article indexed under two PMIDs (ahead-of-print then final).
3. **Normalised title + publication year** — a fuzzy last resort for records
   sharing neither identifier. Title normalisation strips case, punctuation and
   whitespace runs; the year is required alongside, because titles like
   "Annual Report" recur across years and must not be merged.

Every decision — the duplicate, the record kept, the rule that fired — is
returned for the build report, so a corpus is auditable rather than merely
smaller. Ordering is deterministic: the **first** record encountered wins, and
input order is preserved, so the same inputs always produce the same output and
the same report.
"""

from __future__ import annotations  # Postponed annotations

import re  # Title normalisation
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from synapse.logging import get_logger
from synapse.schemas.source import SourceDocument

logger = get_logger(__name__)

DEDUPE_RULES = (
    "pmid",
    "doi",
    "title_year",
)  # Applied in this order; also the vocabulary used in the build report

_PUNCTUATION = re.compile(r"[^\w\s]")  # Everything that is not a word character or whitespace
_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Canonicalise a title for fuzzy comparison.

    Lowercases, strips punctuation, and collapses whitespace, so
    ``"Metformin: A Review."`` and ``"Metformin - a review"`` compare equal.
    Deliberately conservative — no stemming, no stopword removal — because an
    aggressive normaliser merges genuinely different articles.
    """
    lowered = title.strip().lower()
    without_punctuation = _PUNCTUATION.sub(
        " ", lowered
    )  # Punctuation to spaces, not deleted, so "a-b" does not become "ab"
    return _WHITESPACE.sub(" ", without_punctuation).strip()


@dataclass(frozen=True)
class DuplicateDecision:
    """One deduplication decision, recorded for the build report."""

    duplicate_document_id: str  # The record that was dropped
    canonical_document_id: str  # The record that was kept
    rule: str  # Which of DEDUPE_RULES matched
    key: str  # The matched value, so a decision can be re-checked by hand

    def as_dict(self) -> dict[str, str]:
        """Plain-dict form for JSON serialisation in the build report."""
        return {
            "duplicate_document_id": self.duplicate_document_id,
            "canonical_document_id": self.canonical_document_id,
            "rule": self.rule,
            "key": self.key,
        }


@dataclass
class DedupeResult:
    """Outcome of a deduplication pass."""

    documents: list[SourceDocument]  # Kept records, in first-seen order
    decisions: list[DuplicateDecision]  # Every drop, in the order it was decided

    @property
    def duplicate_count(self) -> int:
        """How many records were dropped."""
        return len(self.decisions)


def _title_year_key(document: SourceDocument) -> str | None:
    """Build the third-stage key, or None when the record cannot supply one."""
    if (
        document.publication_date is None
    ):  # Without a year the key would merge same-titled articles across years
        return None
    normalized = normalize_title(document.title)
    if not normalized:  # A record with no usable title cannot be fuzzy-matched
        return None
    return f"{normalized}|{document.publication_date.year}"


def deduplicate(documents: Iterable[SourceDocument]) -> DedupeResult:
    """Deduplicate by PMID, then DOI, then normalised title plus year.

    The first record encountered for a given key is kept. Later matches are
    dropped and recorded. Input order is preserved among the kept records, so
    the result — and the report — are fully deterministic.
    """
    seen_pmid: dict[str, str] = {}  # pmid -> canonical document_id
    seen_doi: dict[str, str] = {}
    seen_title_year: dict[str, str] = {}

    kept: list[SourceDocument] = []
    decisions: list[DuplicateDecision] = []

    for document in documents:
        pmid = document.identifiers.pmid
        doi = document.identifiers.doi
        title_year = _title_year_key(document)

        # Stage 1: PMID. Strongest signal, so it is checked first and an exact
        # match short-circuits the weaker rules entirely.
        if pmid and pmid in seen_pmid:
            decisions.append(DuplicateDecision(document.document_id, seen_pmid[pmid], "pmid", pmid))
            continue
        # Stage 2: DOI. Catches one article indexed under two PMIDs.
        if doi and doi in seen_doi:
            decisions.append(DuplicateDecision(document.document_id, seen_doi[doi], "doi", doi))
            continue
        # Stage 3: normalised title + year. Fuzzy, so it runs last and only
        # when both stronger identifiers failed to match.
        if title_year and title_year in seen_title_year:
            decisions.append(
                DuplicateDecision(
                    document.document_id, seen_title_year[title_year], "title_year", title_year
                )
            )
            continue

        kept.append(document)
        if (
            pmid
        ):  # Register every key this record owns, so a later record matching ANY of them is caught
            seen_pmid[pmid] = document.document_id
        if doi:
            seen_doi[doi] = document.document_id
        if title_year:
            seen_title_year[title_year] = document.document_id

    logger.info(
        "deduplication complete",
        extra={
            "input_count": len(kept) + len(decisions),
            "kept": len(kept),
            "dropped": len(decisions),
        },
    )
    return DedupeResult(documents=kept, decisions=decisions)


def summarize_decisions(decisions: Sequence[DuplicateDecision]) -> dict[str, int]:
    """Count decisions per rule, for the build report summary."""
    counts = dict.fromkeys(
        DEDUPE_RULES, 0
    )  # Every rule appears, including those that fired zero times, so the report shape is stable
    for decision in decisions:
        counts[decision.rule] = counts.get(decision.rule, 0) + 1
    return counts


__all__ = [
    "DEDUPE_RULES",
    "DedupeResult",
    "DuplicateDecision",
    "deduplicate",
    "normalize_title",
    "summarize_decisions",
]

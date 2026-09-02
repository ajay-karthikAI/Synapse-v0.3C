"""
Deduplication and section-aware chunking tests.

Offline throughout: deduplication operates on parsed records, and the chunker
is a pure function of a document.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.identifiers import parse_chunk_id
from synapse.ingest.chunker import ChunkingPolicy, chunk_document, chunk_documents, split_sentences
from synapse.ingest.dedupe import DEDUPE_RULES, deduplicate, normalize_title, summarize_decisions
from synapse.ingest.parse import parse_efetch_response

FIXTURES = Path(__file__).parent / "fixtures" / "pubmed"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def load(name: str):
    """Parse a fixture into source documents."""
    return parse_efetch_response(
        (FIXTURES / name).read_bytes(), retrieved_at=NOW, source_pack_version="sp-test"
    )


class TestTitleNormalisation:
    """Conservative by design: aggressive normalisation merges different articles."""

    def test_case_and_punctuation_are_ignored(self) -> None:
        assert normalize_title("Metformin: A Review.") == normalize_title("metformin  a review")

    def test_hyphens_become_spaces_rather_than_disappearing(self) -> None:
        # "first-line" must not collapse to "firstline", which would match
        # unrelated titles.
        assert normalize_title("first-line") == "first line"

    def test_different_titles_stay_different(self) -> None:
        assert normalize_title("Metformin review") != normalize_title("Insulin review")


class TestDeduplication:
    """Requirement 6: PMID, then DOI, then normalised title plus year."""

    @pytest.fixture
    def result(self):
        return deduplicate(load("duplicates.xml"))

    def test_only_the_canonical_record_survives(self, result) -> None:
        assert len(result.documents) == 1
        assert result.documents[0].identifiers.pmid == "40000030"

    def test_first_record_wins(self, result) -> None:
        # Deterministic tie-breaking: input order decides, so repeated runs
        # keep the same record.
        assert result.documents[0].document_id == "pubmed:40000030"

    def test_every_rule_fires_exactly_once(self, result) -> None:
        assert summarize_decisions(result.decisions) == {"pmid": 1, "doi": 1, "title_year": 1}

    def test_pmid_rule_matches_the_repeated_record(self, result) -> None:
        decision = next(d for d in result.decisions if d.rule == "pmid")
        assert decision.duplicate_document_id == "pubmed:40000030"
        assert decision.canonical_document_id == "pubmed:40000030"
        assert decision.key == "40000030"

    def test_doi_rule_matches_across_casing_and_resolver_prefix(self, result) -> None:
        # "10.1000/DUP-1" against "https://doi.org/10.1000/dup-1".
        decision = next(d for d in result.decisions if d.rule == "doi")
        assert decision.duplicate_document_id == "pubmed:40000031"
        assert decision.key == "10.1000/dup-1"

    def test_title_year_rule_matches_the_reprint(self, result) -> None:
        decision = next(d for d in result.decisions if d.rule == "title_year")
        assert decision.duplicate_document_id == "pubmed:40000032"
        assert decision.key.endswith("|2024")

    def test_every_decision_is_recorded(self, result) -> None:
        # A corpus that silently shrinks is unauditable.
        assert result.duplicate_count == 3
        assert len(result.decisions) == 3

    def test_decisions_serialise_to_plain_dicts(self, result) -> None:
        keys = set(result.decisions[0].as_dict())
        assert keys == {"duplicate_document_id", "canonical_document_id", "rule", "key"}

    def test_summary_always_carries_every_rule(self) -> None:
        # Stable report shape, even when a rule never fires.
        assert set(summarize_decisions([])) == set(DEDUPE_RULES)

    def test_deduplication_is_deterministic(self) -> None:
        first, second = deduplicate(load("duplicates.xml")), deduplicate(load("duplicates.xml"))
        assert [d.document_id for d in first.documents] == [d.document_id for d in second.documents]
        assert [d.as_dict() for d in first.decisions] == [d.as_dict() for d in second.decisions]

    def test_distinct_documents_are_never_merged(self) -> None:
        result = deduplicate(load("retracted.xml"))
        assert len(result.documents) == 3
        assert result.decisions == []


class TestSentenceSplitting:
    """Requirement 12: sentence boundaries are preserved."""

    def test_simple_sentences_split(self) -> None:
        assert len(split_sentences("First sentence here. Second sentence here. Third one.")) == 3

    def test_decimals_do_not_split(self) -> None:
        # "7.5%" must stay in one sentence; the legacy splitter's fallback
        # separator was a bare space.
        assert len(split_sentences("The target was 7.5% in most adults.")) == 1

    @pytest.mark.parametrize("abbreviation", ["e.g.", "i.e.", "vs.", "et al.", "approx."])
    def test_abbreviations_do_not_split(self, abbreviation: str) -> None:
        text = f"Treatment options, {abbreviation} metformin, were compared. Results followed."
        assert len(split_sentences(text)) == 2

    def test_initials_do_not_split(self) -> None:
        assert len(split_sentences("Reported by J. Smith in the trial. A second sentence.")) == 2

    def test_empty_text_yields_no_sentences(self) -> None:
        assert split_sentences("") == []

    def test_splitting_is_deterministic(self) -> None:
        text = "Alpha beta gamma. Delta epsilon zeta! Eta theta iota?"
        assert split_sentences(text) == split_sentences(text)


class TestSectionAwareChunking:
    """Requirement 12: sections are never mixed; metadata travels with chunks."""

    @pytest.fixture
    def structured(self):
        return load("structured_abstract.xml")[0]

    def test_no_chunk_spans_two_sections(self, structured) -> None:
        # The core guarantee. A passage whose halves come from METHODS and
        # CONCLUSIONS would make claims of entirely different strength.
        result = chunk_document(structured, ingested_at=NOW)
        for chunk in result.chunks:
            matching = [s for s in structured.abstract_sections if s.label == chunk.section]
            assert len(matching) == 1
            assert chunk.text in matching[0].text or matching[0].text.startswith(chunk.text[:40])

    def test_section_label_is_carried_on_every_chunk(self, structured) -> None:
        result = chunk_document(structured, ingested_at=NOW)
        assert {c.section for c in result.chunks} <= {
            "BACKGROUND",
            "METHODS",
            "RESULTS",
            "CONCLUSIONS",
        }
        assert all(c.section is not None for c in result.chunks)

    def test_parent_document_is_carried_on_every_chunk(self, structured) -> None:
        result = chunk_document(structured, ingested_at=NOW)
        assert all(c.document_id == structured.document_id for c in result.chunks)

    def test_chunk_ids_are_deterministic(self, structured) -> None:
        first = chunk_document(structured, ingested_at=NOW).chunks
        second = chunk_document(structured, ingested_at=NOW).chunks
        assert [c.chunk_id for c in first] == [c.chunk_id for c in second]

    def test_chunk_ids_are_unique_and_contiguous_across_sections(self, structured) -> None:
        # Ordinals continue across sections rather than restarting, which is
        # what makes identifiers unique by construction.
        chunks = chunk_document(structured, ingested_at=NOW).chunks
        ordinals = [parse_chunk_id(c.chunk_id)[1] for c in chunks]
        assert ordinals == list(range(len(chunks)))
        assert len({c.chunk_id for c in chunks}) == len(chunks)

    def test_chunks_end_on_sentence_boundaries(self, structured) -> None:
        for chunk in chunk_document(structured, ingested_at=NOW).chunks:
            assert chunk.text.rstrip()[-1] in ".!?"

    def test_short_sections_are_dropped_and_counted(self) -> None:
        # The legacy corpus contains chunks whose entire text is ".".
        document = load("retracted.xml")[2]  # One short CONCLUSIONS section
        result = chunk_document(
            document,
            ingested_at=NOW,
            policy=ChunkingPolicy(target_chars=800, min_chars=400, max_chars=1600),
        )
        assert result.chunks == []
        assert result.dropped_short_sections == 1

    def test_keep_short_sections_retains_them(self) -> None:
        document = load("retracted.xml")[2]
        policy = ChunkingPolicy(
            target_chars=800, min_chars=400, max_chars=1600, keep_short_sections=True
        )
        assert chunk_document(document, ingested_at=NOW, policy=policy).chunks

    def test_oversized_sentence_is_emitted_whole_and_counted(self, structured) -> None:
        # Cutting it would produce the mid-clause fragments this chunker exists
        # to avoid, so it is kept intact and reported instead.
        policy = ChunkingPolicy(target_chars=40, min_chars=1, max_chars=50)
        result = chunk_document(structured, ingested_at=NOW, policy=policy)
        assert result.oversized_sentences > 0

    def test_document_without_abstract_yields_no_chunks(self) -> None:
        assert chunk_document(load("missing_fields.xml")[0], ingested_at=NOW).chunks == []

    def test_chunk_offsets_are_within_the_parent_document(self, structured) -> None:
        total = sum(len(s.text) for s in structured.abstract_sections) + len(
            structured.abstract_sections
        )
        for chunk in chunk_document(structured, ingested_at=NOW).chunks:
            assert 0 <= chunk.char_start < chunk.char_end <= total


class TestChunkingPolicy:
    """A policy that cannot produce sensible chunks is rejected at construction."""

    def test_min_above_target_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_chars"):
            ChunkingPolicy(target_chars=100, min_chars=200, max_chars=400)

    def test_target_above_max_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_chars"):
            ChunkingPolicy(target_chars=500, min_chars=10, max_chars=400)


class TestBatchChunking:
    """Aggregate statistics feed the build report."""

    def test_statistics_reconcile(self) -> None:
        documents = load("structured_abstract.xml") + load("missing_fields.xml")
        chunks, stats = chunk_documents(documents, ingested_at=NOW)
        assert stats["documents_chunked"] == 1
        assert stats["documents_without_chunks"] == 1  # The record with no abstract
        assert len(chunks) > 0

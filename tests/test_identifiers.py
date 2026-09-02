"""
Deterministic identifier tests.

The regression these guard is concrete and measured: the legacy scheme
``f"{pmid}_chunk{chunk_index}"`` produced 162 collisions across the 2,220
chunks currently shipped, because the same article is fetched under several
topic queries and its chunk indices restart at zero each time. Colliding
identifiers silently corrupt hybrid scoring.
"""

from __future__ import annotations

import pytest

from synapse.errors import ArtifactSchemaError
from synapse.identifiers import (
    document_id_for_content,
    document_id_for_pubmed,
    is_valid_chunk_id,
    is_valid_document_id,
    is_valid_reviewer_id,
    make_chunk_id,
    make_document_id,
    parse_chunk_id,
)


class TestDeterminism:
    """Identical inputs must always produce identical identifiers."""

    def test_pubmed_document_id_is_stable(self) -> None:
        assert document_id_for_pubmed("41802233") == "pubmed:41802233"
        assert document_id_for_pubmed("41802233") == document_id_for_pubmed(
            "41802233"
        )  # Repeat calls agree

    def test_pubmed_document_id_ignores_surrounding_whitespace(self) -> None:
        assert document_id_for_pubmed(" 41802233 ") == document_id_for_pubmed(
            "41802233"
        )  # XML text nodes often carry stray whitespace

    def test_content_document_id_is_stable_across_calls(self) -> None:
        first = document_id_for_content("pdf", "Some clinical guidance text.")
        second = document_id_for_content("pdf", "Some clinical guidance text.")
        assert first == second  # Content addressing must be a pure function of content

    def test_content_document_id_is_insensitive_to_cosmetic_whitespace(self) -> None:
        # Normalisation runs before hashing, so a re-serialised document keeps its identifier.
        assert document_id_for_content("pdf", "a  b\nc") == document_id_for_content("pdf", "a b c")

    def test_content_document_id_differs_for_different_content(self) -> None:
        assert document_id_for_content("pdf", "alpha") != document_id_for_content("pdf", "beta")

    def test_chunk_id_composition_is_deterministic(self) -> None:
        assert make_chunk_id("pubmed:41802233", 2) == "pubmed:41802233#0002"

    def test_chunk_id_is_zero_padded_so_lexical_order_matches_numeric_order(self) -> None:
        ordered = [make_chunk_id("pubmed:1", n) for n in (0, 2, 10, 100)]
        assert ordered == sorted(
            ordered
        )  # Zero padding is what makes this hold; without it "#10" sorts before "#2"


class TestUniqueness:
    """The property that fixes the measured 162-collision defect."""

    def test_continuing_ordinals_prevent_collisions_for_a_repeated_document(self) -> None:
        # A document ingested twice, five chunks each time. Under the legacy
        # scheme both passes produce chunk0..chunk4 and collide. Here the
        # ordinal continues, so all ten identifiers are distinct.
        ids = [make_chunk_id("pubmed:41802233", ordinal) for ordinal in range(10)]
        assert len(set(ids)) == 10

    def test_different_documents_never_share_chunk_ids(self) -> None:
        first = {make_chunk_id("pubmed:1", n) for n in range(5)}
        second = {make_chunk_id("pubmed:2", n) for n in range(5)}
        assert first.isdisjoint(second)


class TestParsing:
    """Round-tripping and rejection."""

    def test_parse_round_trips(self) -> None:
        assert parse_chunk_id(make_chunk_id("pubmed:41802233", 7)) == ("pubmed:41802233", 7)

    @pytest.mark.parametrize(
        "value",
        [
            "pubmed:41802233",  # Missing the ordinal component
            "pubmed:41802233#2",  # Ordinal not zero-padded to the required width
            "unknown:41802233#0002",  # Scheme outside the closed vocabulary
            "pubmed:41802233#abcd",  # Non-numeric ordinal
            "",  # Empty
            "pubmed:#0002",  # Empty key
        ],
    )
    def test_malformed_chunk_ids_are_rejected(self, value: str) -> None:
        assert not is_valid_chunk_id(value)
        with pytest.raises(ArtifactSchemaError):
            parse_chunk_id(value)

    def test_unknown_scheme_is_rejected(self) -> None:
        with pytest.raises(ArtifactSchemaError):
            make_document_id("ftp", "123")

    def test_non_numeric_pmid_is_rejected(self) -> None:
        with pytest.raises(ArtifactSchemaError):
            document_id_for_pubmed("PMC123456")

    def test_negative_ordinal_is_rejected(self) -> None:
        with pytest.raises(ArtifactSchemaError):
            make_chunk_id("pubmed:1", -1)

    def test_key_may_not_contain_the_chunk_separator(self) -> None:
        # '#' in a key would make chunk identifiers ambiguous to parse.
        with pytest.raises(ArtifactSchemaError):
            make_document_id("txt", "abc#def")

    def test_key_may_not_contain_a_path_separator(self) -> None:
        # Identifiers are used in messages and lookups; a '/' would let one
        # masquerade as a path fragment.
        with pytest.raises(ArtifactSchemaError):
            make_document_id("txt", "../../etc/passwd")


class TestValidators:
    """Predicate forms used by the pydantic models."""

    def test_valid_document_ids_accepted(self) -> None:
        assert is_valid_document_id("pubmed:41802233")
        assert is_valid_document_id("guideline:nice:hypertension-2024")

    def test_reviewer_id_must_be_pseudonymous(self) -> None:
        assert is_valid_reviewer_id("rev_a1b2c3d4")
        assert not is_valid_reviewer_id("dr.smith")  # A name
        assert not is_valid_reviewer_id(
            "rev_A1B2C3D4"
        )  # Uppercase is rejected so identifiers are canonical
        assert not is_valid_reviewer_id("clinician@hospital.org")  # An email address
        assert not is_valid_reviewer_id("rev_123")  # Wrong length

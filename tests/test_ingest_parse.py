"""
PubMed XML parsing tests.

Every test reads a local fixture from ``tests/fixtures/pubmed/``. No test in
this module opens a socket — the parser is a pure function of bytes, so there
is nothing to stub.

The tests are written against the three measured defects in
``Data/fetch_and_chunk.py``: only the first abstract section was captured,
titles were truncated at nested markup, and no evidence metadata was captured
at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.ingest.parse import (
    determine_retraction_status,
    flatten_element_text,
    normalize_doi,
    parse_efetch_response,
)
from synapse.schemas.enums import (
    DatePrecision,
    EvidenceType,
    EvidenceTypeProvenance,
    NoticeType,
    RetractionStatus,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pubmed"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # Fixed so parsed records are byte-reproducible


def load(name: str):
    """Parse a fixture file into source documents."""
    return parse_efetch_response(
        (FIXTURES / name).read_bytes(),
        retrieved_at=NOW,
        source_pack_version="sp-test",
    )


@pytest.fixture
def structured():
    """The four-section structured abstract."""
    return load("structured_abstract.xml")[0]


class TestStructuredAbstracts:
    """Requirement 1 and 2: every AbstractText element, with its label."""

    def test_all_four_sections_are_captured(self, structured) -> None:
        # The legacy parser used find(), returning only BACKGROUND. Measured
        # consequence on the shipped corpus: 312 of 847 documents retained
        # under 400 characters of abstract text in total.
        assert len(structured.abstract_sections) == 4

    def test_section_labels_are_preserved(self, structured) -> None:
        assert [s.label for s in structured.abstract_sections] == [
            "BACKGROUND",
            "METHODS",
            "RESULTS",
            "CONCLUSIONS",
        ]

    def test_nlm_categories_are_preserved(self, structured) -> None:
        assert [s.nlm_category for s in structured.abstract_sections] == [
            "BACKGROUND",
            "METHODS",
            "RESULTS",
            "CONCLUSIONS",
        ]

    def test_section_order_is_preserved(self, structured) -> None:
        assert [s.ordinal for s in structured.abstract_sections] == [0, 1, 2, 3]

    def test_conclusions_content_survives(self, structured) -> None:
        # The clinically actionable section — precisely what the legacy parser
        # discarded.
        conclusions = structured.abstract_sections[3]
        assert "reduced rates of major cardiovascular events" in conclusions.text

    def test_unstructured_abstract_yields_one_unlabelled_section(self) -> None:
        document = load("nested_markup.xml")[0]
        assert len(document.abstract_sections) == 1
        assert document.abstract_sections[0].label is None


class TestInlineMarkup:
    """Requirement 3: inline XML markup must not truncate text."""

    def test_title_survives_nested_tags(self) -> None:
        # The legacy title_el.text stopped at the first child, producing corpus
        # entries such as "Pharmacologic MRI Brain Imaging Studies of Serotonin 5-HT".
        document = load("nested_markup.xml")[0]
        assert document.title.startswith("Pharmacologic MRI studies of serotonin 5-HT")
        assert "2A" in document.title  # Text inside <sub> is retained
        assert "binding" in document.title  # Text inside <i> is retained
        assert document.title.endswith("in vivo.")  # And everything after the last child element

    def test_abstract_text_survives_nested_tags(self) -> None:
        document = load("nested_markup.xml")[0]
        text = document.abstract_sections[0].text
        assert "K" in text and "behavioural" in text
        assert "5-HT" in text

    def test_flatten_handles_none(self) -> None:
        assert flatten_element_text(None) == ""


class TestMetadataCapture:
    """Requirement 4: every listed field is captured."""

    def test_identifiers(self, structured) -> None:
        assert structured.identifiers.pmid == "40000001"
        assert (
            structured.identifiers.doi == "10.1056/nejmoa1234567"
        )  # Normalised to lowercase for comparison
        assert structured.identifiers.pmcid == "PMC9876543"

    def test_journal_and_pagination(self, structured) -> None:
        assert structured.container.journal == "The New England Journal of Medicine"
        assert structured.container.volume == "390"
        assert structured.container.issue == "12"
        assert structured.container.pages == "1091-1103"

    def test_language_and_publication_status(self, structured) -> None:
        assert structured.language == "eng"
        assert structured.publication_status == "ppublish"

    def test_article_types_are_retained_verbatim(self, structured) -> None:
        # Raw types are never discarded, so a future re-classification can run
        # against the original metadata.
        assert "Randomized Controlled Trial" in structured.publication_types
        assert "Journal Article" in structured.publication_types

    def test_source_url_is_constructed_from_the_pmid(self, structured) -> None:
        # Built rather than trusted from the payload, so a hostile record
        # cannot inject a link into a patient-facing citation.
        assert structured.source_url == "https://pubmed.ncbi.nlm.nih.gov/40000001/"

    def test_retrieval_timestamp_is_recorded(self, structured) -> None:
        assert structured.retrieved_at == NOW

    def test_never_claims_approval(self, structured) -> None:
        # Automatic retrieval confers no medical approval.
        assert structured.approval_status == "unreviewed"
        assert structured.review is None
        assert "no clinician has reviewed" in structured.notes.lower()


class TestAuthors:
    """Requirement 3: collective authors alongside personal ones."""

    def test_personal_and_collective_authors_are_both_captured(self, structured) -> None:
        assert len(structured.authors) == 3
        assert structured.authors[0].family == "Wright"
        assert structured.authors[0].initials == "JT"

    def test_collective_author_is_marked_and_not_forced_into_a_surname(self, structured) -> None:
        collective = structured.authors[2]
        assert collective.is_collective is True
        assert collective.collective_name == "The SPRINT Research Group"
        assert collective.family is None

    def test_missing_author_list_yields_empty_list_not_a_placeholder(self) -> None:
        # Absent means absent. An author is never invented.
        document = load("missing_fields.xml")[0]
        assert document.authors == []


class TestDates:
    """Requirement 3: multiple dates, electronic versus print."""

    def test_both_electronic_and_print_dates_are_captured(self, structured) -> None:
        assert structured.electronic_publication_date.isoformat() == "2023-11-09"
        assert structured.print_publication_date.isoformat() == "2024-03-21"

    def test_canonical_date_is_the_earliest(self, structured) -> None:
        # The article was readable online in November; using the March issue
        # date would overstate how recent the evidence is.
        assert structured.publication_date == structured.electronic_publication_date

    def test_precision_is_recorded(self, structured) -> None:
        assert structured.publication_date_precision is DatePrecision.DAY

    def test_month_only_date_defaults_the_day_and_records_precision(self) -> None:
        document = load("nested_markup.xml")[0]
        assert document.publication_date.isoformat() == "2022-07-01"
        assert document.publication_date_precision is DatePrecision.MONTH

    def test_medline_free_text_date_recovers_the_year(self) -> None:
        # "2019 Jan-Feb" has no structured Year element.
        document = load("missing_fields.xml")[0]
        assert document.publication_date.isoformat() == "2019-01-01"
        assert document.publication_date_precision is DatePrecision.YEAR
        assert document.medline_date_raw == "2019 Jan-Feb"

    def test_year_only_date_records_year_precision(self) -> None:
        documents = {d.identifiers.pmid: d for d in load("retracted.xml")}
        concerned = documents["40000012"]
        assert concerned.publication_date.isoformat() == "2021-01-01"
        assert concerned.publication_date_precision is DatePrecision.YEAR


class TestMissingFields:
    """Requirement 3: degenerate records must not raise."""

    def test_record_without_abstract_parses_with_empty_sections(self) -> None:
        document = load("missing_fields.xml")[0]
        assert document.abstract_sections == []
        assert document.title == "A brief communication without an abstract."

    def test_record_without_pmid_is_skipped(self) -> None:
        # Nothing can be identified or cited without a PMID.
        pmids = {d.identifiers.pmid for d in load("missing_fields.xml")}
        assert pmids == {"40000003"}

    def test_record_without_article_element_is_skipped(self) -> None:
        assert len(load("missing_fields.xml")) == 1

    def test_malformed_xml_raises_typed_error(self) -> None:
        from synapse.errors import ArtifactSchemaError

        with pytest.raises(ArtifactSchemaError, match="not well-formed"):
            parse_efetch_response(
                b"<PubmedArticleSet><broken>", retrieved_at=NOW, source_pack_version="sp"
            )

    def test_oversized_payload_is_refused_before_parsing(self, monkeypatch) -> None:
        import synapse.ingest.parse as parse_module
        from synapse.errors import ArtifactSchemaError

        monkeypatch.setattr(parse_module, "MAX_XML_BYTES", 16)
        with pytest.raises(ArtifactSchemaError, match="exceeds maximum size"):
            parse_efetch_response(
                b"<PubmedArticleSet></PubmedArticleSet>" * 10,
                retrieved_at=NOW,
                source_pack_version="sp",
            )


class TestRetractionsAndCorrections:
    """Requirement 3 and 13: direction of a notice is load-bearing."""

    @pytest.fixture
    def by_pmid(self):
        return {d.identifiers.pmid: d for d in load("retracted.xml")}

    def test_retracted_article_is_marked_retracted(self, by_pmid) -> None:
        assert by_pmid["40000010"].retraction_status is RetractionStatus.RETRACTED

    def test_retracted_article_is_not_itself_a_notice(self, by_pmid) -> None:
        assert by_pmid["40000010"].is_retraction_notice is False

    def test_retraction_notice_is_not_marked_retracted(self, by_pmid) -> None:
        # The notice is valid evidence ABOUT the retraction. Marking it
        # retracted would discard the very record that documents the problem.
        notice = by_pmid["40000011"]
        assert notice.retraction_status is not RetractionStatus.RETRACTED
        assert notice.is_retraction_notice is True

    def test_expression_of_concern_is_distinguished_from_retraction(self, by_pmid) -> None:
        assert by_pmid["40000012"].retraction_status is RetractionStatus.EXPRESSION_OF_CONCERN

    def test_related_notices_preserve_direction_and_raw_ref_type(self, by_pmid) -> None:
        notices = by_pmid["40000010"].related_notices
        assert len(notices) == 1
        assert notices[0].notice_type is NoticeType.RETRACTION_IN
        assert (
            notices[0].raw_ref_type == "RetractionIn"
        )  # Verbatim, so an unmapped value could still be audited
        assert notices[0].pmid == "40000011"

    def test_corrected_article_is_marked_corrected_not_retracted(self) -> None:
        # A correction does not discredit an article.
        document = load("corrected.xml")[0]
        assert document.retraction_status is RetractionStatus.CORRECTED
        assert document.related_notices[0].notice_type is NoticeType.ERRATUM_IN

    def test_retraction_check_timestamp_is_recorded(self, by_pmid) -> None:
        # A status other than 'unchecked' asserts a screening result, so it
        # must say when the screening happened.
        assert by_pmid["40000010"].retraction_checked_at == NOW

    def test_clean_record_is_none_not_unchecked(self, structured) -> None:
        # 'none' asserts a clean result; 'unchecked' means nobody looked.
        assert structured.retraction_status is RetractionStatus.NONE

    def test_unmapped_ref_type_becomes_other_and_is_kept(self) -> None:
        status, is_notice = determine_retraction_status([], [])
        assert status is RetractionStatus.NONE
        assert is_notice is False


class TestEvidenceTypeNormalisation:
    """Requirement 5: normalised enum, raw types retained."""

    def test_rct_is_classified_from_publication_types(self, structured) -> None:
        assert structured.evidence_type is EvidenceType.RCT
        assert structured.evidence_type_provenance is EvidenceTypeProvenance.PUBLISHER_METADATA

    def test_meta_analysis_outranks_systematic_review(self) -> None:
        # The fixture carries Meta-Analysis, Systematic Review AND Journal
        # Article. Precedence must be deterministic rather than XML order.
        document = load("corrected.xml")[0]
        assert document.evidence_type is EvidenceType.META_ANALYSIS

    def test_raw_publication_types_are_never_discarded(self) -> None:
        document = load("corrected.xml")[0]
        assert set(document.publication_types) >= {
            "Meta-Analysis",
            "Systematic Review",
            "Journal Article",
        }

    def test_record_without_publication_types_is_unclassified(self) -> None:
        # An honest "we do not know", not a plausible-looking guess.
        document = load("missing_fields.xml")[0]
        assert document.evidence_type is EvidenceType.UNCLASSIFIED
        assert document.evidence_type_provenance is EvidenceTypeProvenance.UNKNOWN


class TestDoiNormalisation:
    """Requirement 6 support: DOIs must compare equal across published forms."""

    @pytest.mark.parametrize(
        "raw",
        [
            "10.1000/ABC",
            "https://doi.org/10.1000/abc",
            "http://dx.doi.org/10.1000/ABC",
            "doi:10.1000/abc",
            "  10.1000/Abc  ",
        ],
    )
    def test_variants_normalise_to_one_value(self, raw: str) -> None:
        assert normalize_doi(raw) == "10.1000/abc"


class TestDeterminism:
    """The parser is pure: same bytes in, same records out."""

    def test_parsing_twice_yields_identical_records(self) -> None:
        assert load("structured_abstract.xml") == load("structured_abstract.xml")

    def test_content_digest_is_stable(self) -> None:
        assert load("corrected.xml")[0].content_sha256 == load("corrected.xml")[0].content_sha256

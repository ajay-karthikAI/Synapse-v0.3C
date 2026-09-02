"""
synapse.ingest.parse
====================
PubMed XML → :class:`SourceDocument`.

This module replaces three specific, measured defects in
``Data/fetch_and_chunk.py``:

1. ``article.find(".//AbstractText")`` returns **one** element. PubMed
   structured abstracts split into BACKGROUND / METHODS / RESULTS /
   CONCLUSIONS elements, so the clinically actionable conclusion was silently
   discarded. Measured on the shipped corpus: 312 of 847 documents retained
   under 400 characters of abstract text in total, median 573.

2. ``title_el.text`` returns only the text *before* the first child element,
   so any title containing ``<i>``, ``<sub>`` or ``<sup>`` was truncated. The
   shipped corpus contains ``"Pharmacologic MRI Brain Imaging Studies of
   Serotonin 5-HT"`` and ``"Evaluating the cardio-protective effects of "``,
   both rendered to patients as source names.

3. No publication date, journal, authors, article types, language or
   retraction status were captured at all — the ``Chunk`` dataclass had no
   fields for them, making evidence governance impossible.

Everything here is pure: bytes in, validated models out. No I/O, no network,
no clock except the caller-supplied retrieval timestamp — so the whole parser
is exercised by the offline fixture suite.

**XML safety.** ``xml.etree.ElementTree`` does not resolve external entities
and does not expand entity references recursively, so the classic XXE and
billion-laughs attacks do not apply. A total input size ceiling additionally
bounds memory before parsing begins.
"""

from __future__ import annotations  # Postponed annotations

import re  # Date-component parsing and DOI normalisation
import xml.etree.ElementTree as ET  # Safe here: ElementTree resolves no external entities; input size is capped below
from datetime import date, datetime

from synapse.errors import ArtifactSchemaError
from synapse.identifiers import document_id_for_pubmed
from synapse.ingest.evidence import classify_evidence_type
from synapse.logging import get_logger
from synapse.normalize import normalize_text
from synapse.schemas.enums import DatePrecision, NoticeType, RetractionStatus, SourceType
from synapse.schemas.source import (
    AbstractSection,
    Author,
    DocumentIdentifiers,
    RelatedNotice,
    SourceContainer,
    SourceDocument,
)

logger = get_logger(__name__)

MAX_XML_BYTES = (
    64 * 1024 * 1024
)  # 64 MiB ceiling on one efetch body; a 200-record batch is far smaller, so anything near this is malformed

PUBMED_URL_TEMPLATE = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"  # Canonical article URL; built from the PMID rather than trusted from the payload

_MONTH_NAMES = {  # PubMed writes months as names, abbreviations, or numbers
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_YEAR_IN_TEXT = re.compile(
    r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b"
)  # Extracts a plausible year from free-text MedlineDate such as "2024 Jan-Feb"

_REF_TYPE_MAP = {  # PubMed RefType attribute -> our directional enum
    "retractionin": NoticeType.RETRACTION_IN,
    "retractionof": NoticeType.RETRACTION_OF,
    "expressionofconcernin": NoticeType.EXPRESSION_OF_CONCERN_IN,
    "expressionofconcernfor": NoticeType.EXPRESSION_OF_CONCERN_FOR,
    "erratumin": NoticeType.ERRATUM_IN,
    "erratumfor": NoticeType.ERRATUM_FOR,
    "correctedandrepublishedin": NoticeType.CORRECTED_AND_REPUBLISHED_IN,
    "correctedandrepublishedfrom": NoticeType.CORRECTED_AND_REPUBLISHED_FROM,
    "updatein": NoticeType.UPDATE_IN,
    "updateof": NoticeType.UPDATE_OF,
    "commentin": NoticeType.COMMENT_IN,
    "commenton": NoticeType.COMMENT_ON,
}

# Publication types that themselves declare a retraction state. PubMed marks
# both sides of a retraction, and the two must not be confused: a "Retracted
# Publication" is discredited evidence, while a "Retraction of Publication" is
# the notice announcing it and remains perfectly valid to cite.
_RETRACTED_PUBLICATION_TYPES = {"retracted publication"}
_RETRACTION_NOTICE_TYPES = {"retraction of publication", "expression of concern"}
_CORRECTION_PUBLICATION_TYPES = {"published erratum", "corrected and republished article"}


def flatten_element_text(element: ET.Element | None) -> str:
    """Return all text inside ``element``, including text within nested tags.

    This is the fix for the truncated-title defect. ``element.text`` stops at
    the first child, so ``<ArticleTitle>Serotonin 5-HT<sub>2A</sub>
    receptors</ArticleTitle>`` yields only ``"Serotonin 5-HT"``.
    ``itertext()`` walks the whole subtree.
    """
    if element is None:
        return ""
    return normalize_text(
        "".join(element.itertext())
    )  # Join every text node in document order, then normalise whitespace


def _parse_date_parts(
    year_text: str | None, month_text: str | None, day_text: str | None
) -> tuple[date | None, DatePrecision]:
    """Build a date from PubMed's year/month/day components, recording precision.

    Missing components are defaulted downward (month -> January, day -> 1st)
    and the precision is recorded, so a year-only date is never mistaken for an
    exact one.
    """
    if not year_text or not year_text.strip().isdigit():  # Without a year there is no date at all
        return None, DatePrecision.UNKNOWN
    year = int(year_text.strip())

    month = 1
    precision = DatePrecision.YEAR
    if month_text:
        raw = month_text.strip().lower()
        if raw.isdigit():  # Numeric form, e.g. "03"
            candidate = int(raw)
            if 1 <= candidate <= 12:
                month, precision = candidate, DatePrecision.MONTH
        elif raw[:3] in _MONTH_NAMES:  # Name or abbreviation, e.g. "Mar" or "March"
            month, precision = _MONTH_NAMES[raw[:3]], DatePrecision.MONTH

    day = 1
    if (
        day_text and day_text.strip().isdigit() and precision is DatePrecision.MONTH
    ):  # A day is only meaningful once a month is known
        candidate = int(day_text.strip())
        if 1 <= candidate <= 31:
            day, precision = candidate, DatePrecision.DAY

    try:
        return date(year, month, day), precision
    except (
        ValueError
    ):  # e.g. 31 February; fall back to the first of the month rather than dropping the date
        return date(year, month, 1), DatePrecision.MONTH


def _parse_pub_date(
    pub_date_el: ET.Element | None,
) -> tuple[date | None, DatePrecision, str | None]:
    """Parse a ``<PubDate>``, which may carry structured parts or free text."""
    if pub_date_el is None:
        return None, DatePrecision.UNKNOWN, None

    medline = pub_date_el.findtext(
        "MedlineDate"
    )  # Free-text alternative, e.g. "2024 Jan-Feb" or "1999 Winter"
    if medline and not pub_date_el.findtext("Year"):
        match = _YEAR_IN_TEXT.search(
            medline
        )  # Recover at least the year rather than discarding the date entirely
        if match:
            return date(int(match.group(1)), 1, 1), DatePrecision.YEAR, medline.strip()
        return None, DatePrecision.UNKNOWN, medline.strip()

    parsed, precision = _parse_date_parts(
        pub_date_el.findtext("Year"),
        pub_date_el.findtext("Month"),
        pub_date_el.findtext("Day"),
    )
    return parsed, precision, medline.strip() if medline else None


def _parse_article_dates(article_el: ET.Element) -> tuple[date | None, DatePrecision]:
    """Parse ``<ArticleDate DateType="Electronic">``, the ahead-of-print date."""
    for article_date in article_el.findall("ArticleDate"):
        if (
            article_date.get("DateType", "Electronic") == "Electronic"
        ):  # Only the electronic variant is defined for this element
            return _parse_date_parts(
                article_date.findtext("Year"),
                article_date.findtext("Month"),
                article_date.findtext("Day"),
            )
    return None, DatePrecision.UNKNOWN


def _parse_authors(article_el: ET.Element) -> list[Author]:
    """Parse the complete author list, handling personal and collective authors."""
    authors: list[Author] = []
    for author_el in article_el.findall("./AuthorList/Author"):
        if (
            author_el.get("ValidYN", "Y") == "N"
        ):  # PubMed marks retracted or erroneous authorship entries; excluding them avoids attributing work to the wrong person
            continue
        collective = flatten_element_text(author_el.find("CollectiveName"))
        if collective:  # A study group or consortium credited as an author
            authors.append(Author(collective_name=collective, is_collective=True))
            continue
        family = flatten_element_text(author_el.find("LastName"))
        if (
            not family
        ):  # Neither a personal nor a collective name: nothing citable, so skip rather than invent
            continue
        authors.append(
            Author(
                family=family,
                given=flatten_element_text(author_el.find("ForeName")) or None,
                initials=flatten_element_text(author_el.find("Initials")) or None,
            )
        )
    return authors


def _parse_abstract_sections(article_el: ET.Element) -> list[AbstractSection]:
    """Parse **every** ``<AbstractText>`` element, preserving labels and order.

    This is the central fix. ``findall`` rather than ``find``; label and
    NlmCategory attributes retained; inline markup flattened so a section
    containing ``<i>`` keeps its full text.
    """
    sections: list[AbstractSection] = []
    for ordinal, text_el in enumerate(article_el.findall("./Abstract/AbstractText")):
        text = flatten_element_text(text_el)  # itertext(), so nested markup is preserved as text
        if not text:  # An empty section carries no evidence; skipping keeps it out of the corpus
            continue
        sections.append(
            AbstractSection(
                ordinal=len(
                    sections
                ),  # Renumber contiguously after skips, so ordinals have no gaps
                label=(text_el.get("Label") or "").strip()
                or None,  # Publisher label, e.g. "CONCLUSIONS"
                nlm_category=(text_el.get("NlmCategory") or "").strip()
                or None,  # NLM's normalised category
                text=text,
            )
        )
        del ordinal  # The enumerate index is deliberately unused; sections are renumbered above
    return sections


def _parse_identifiers(
    article_el: ET.Element, pubmed_data_el: ET.Element | None, pmid: str
) -> DocumentIdentifiers:
    """Collect PMID, DOI, and PMC identifiers from the two places PubMed puts them."""
    doi: str | None = None
    pmcid: str | None = None

    for elocation in article_el.findall("ELocationID"):  # DOIs appear here on many records
        if (elocation.get("EIdType") or "").lower() == "doi" and elocation.get(
            "ValidYN", "Y"
        ) != "N":
            doi = doi or flatten_element_text(elocation) or None

    if pubmed_data_el is not None:  # ...and again, canonically, in ArticleIdList
        for article_id in pubmed_data_el.findall("./ArticleIdList/ArticleId"):
            id_type = (article_id.get("IdType") or "").lower()
            value = flatten_element_text(article_id)
            if not value:
                continue
            if id_type == "doi":
                doi = doi or value  # First occurrence wins; both locations normally agree
            elif id_type == "pmc":
                pmcid = pmcid or value

    return DocumentIdentifiers(
        pmid=pmid,
        pmcid=pmcid,
        doi=normalize_doi(doi) if doi else None,
    )


def normalize_doi(value: str) -> str:
    """Canonicalise a DOI for comparison.

    DOIs are case-insensitive and are published with and without a resolver
    prefix. Without normalisation, ``10.1000/ABC`` and
    ``https://doi.org/10.1000/abc`` would not deduplicate against each other.
    """
    text = value.strip().lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.strip()


def _parse_related_notices(citation_el: ET.Element) -> list[RelatedNotice]:
    """Parse ``<CommentsCorrections>`` relationships, preserving direction."""
    notices: list[RelatedNotice] = []
    for element in citation_el.findall("./CommentsCorrectionsList/CommentsCorrections"):
        raw_ref_type = (element.get("RefType") or "").strip()
        if not raw_ref_type:  # Without a RefType the relationship is uninterpretable
            continue
        key = (
            raw_ref_type.replace(" ", "").replace("-", "").lower()
        )  # Normalise spelling variants before lookup
        notices.append(
            RelatedNotice(
                notice_type=_REF_TYPE_MAP.get(
                    key, NoticeType.OTHER
                ),  # Unmapped types become OTHER rather than being dropped
                ref_source=flatten_element_text(element.find("RefSource")) or None,
                pmid=flatten_element_text(element.find("PMID")) or None,
                doi=None,
                raw_ref_type=raw_ref_type,  # Verbatim, so an unmapped value can still be audited
            )
        )
    return notices


def determine_retraction_status(
    publication_types: list[str],
    notices: list[RelatedNotice],
) -> tuple[RetractionStatus, bool]:
    """Decide a record's retraction state and whether it is itself a notice.

    Two independent signals are combined, because PubMed populates them
    inconsistently: the publication-type list, and the direction of any
    CommentsCorrections relationship.

    Returns ``(status, is_retraction_notice)``. Direction matters enormously:
    an article carrying ``RetractionIn`` **has been retracted**, while one
    carrying ``RetractionOf`` **is the retraction notice** and remains valid
    evidence about the retraction itself.
    """
    lowered = {value.strip().lower() for value in publication_types}
    notice_types = {notice.notice_type for notice in notices}

    is_notice = bool(lowered & _RETRACTION_NOTICE_TYPES) or bool(
        notice_types & {NoticeType.RETRACTION_OF, NoticeType.EXPRESSION_OF_CONCERN_FOR}
    )

    if lowered & _RETRACTED_PUBLICATION_TYPES or NoticeType.RETRACTION_IN in notice_types:
        return (
            RetractionStatus.RETRACTED,
            is_notice,
        )  # Strongest signal; checked first so it cannot be masked by a correction
    if NoticeType.EXPRESSION_OF_CONCERN_IN in notice_types:
        return RetractionStatus.EXPRESSION_OF_CONCERN, is_notice
    if (
        lowered & _CORRECTION_PUBLICATION_TYPES
        or NoticeType.ERRATUM_IN in notice_types
        or NoticeType.CORRECTED_AND_REPUBLISHED_IN in notice_types
    ):
        return RetractionStatus.CORRECTED, is_notice
    return (
        RetractionStatus.NONE,
        is_notice,
    )  # Screened, and clean — distinct from UNCHECKED, which means we never looked


def parse_article(
    article_el: ET.Element,
    *,
    retrieved_at: datetime,
    source_pack_version: str,
) -> SourceDocument | None:
    """Convert one ``<PubmedArticle>`` element into a :class:`SourceDocument`.

    Returns ``None`` when the record carries no PMID, which is the only field
    without which nothing can be identified or cited.
    """
    citation_el = article_el.find("MedlineCitation")
    if citation_el is None:  # Not a PubmedArticle shape (e.g. a PubmedBookArticle)
        return None
    pmid = flatten_element_text(citation_el.find("PMID"))
    if not pmid.isdigit():  # Non-numeric or absent: nothing citable
        logger.warning("skipping record without a usable PMID")
        return None

    article = citation_el.find("Article")
    if article is None:
        logger.warning("skipping record without an Article element", extra={"pmid": pmid})
        return None
    pubmed_data = article_el.find("PubmedData")

    title = (
        flatten_element_text(article.find("ArticleTitle")) or "[No title provided]"
    )  # Placeholder is explicit, never a silently empty string

    journal_el = article.find("Journal")
    container = SourceContainer(
        journal=flatten_element_text(journal_el.find("Title")) if journal_el is not None else None,
        issn=flatten_element_text(journal_el.find("ISSN")) if journal_el is not None else None,
        volume=flatten_element_text(journal_el.find("./JournalIssue/Volume"))
        if journal_el is not None
        else None,
        issue=flatten_element_text(journal_el.find("./JournalIssue/Issue"))
        if journal_el is not None
        else None,
        pages=flatten_element_text(article.find("./Pagination/MedlinePgn")) or None,
    )

    print_date, print_precision, medline_raw = _parse_pub_date(
        journal_el.find("./JournalIssue/PubDate") if journal_el is not None else None
    )
    electronic_date, electronic_precision = _parse_article_dates(article)

    # Canonical date policy: the EARLIEST known date. An article that appeared
    # online in December and in an issue the following March was available to
    # readers in December, so using the issue date would overstate recency.
    candidates = [
        (d, p)
        for d, p in ((electronic_date, electronic_precision), (print_date, print_precision))
        if d is not None
    ]
    if candidates:
        publication_date, publication_precision = min(candidates, key=lambda pair: pair[0])
    else:
        publication_date, publication_precision = None, DatePrecision.UNKNOWN

    publication_types = [
        flatten_element_text(el) for el in article.findall("./PublicationTypeList/PublicationType")
    ]
    publication_types = [
        value for value in publication_types if value
    ]  # Drop empties rather than storing blank strings

    notices = _parse_related_notices(citation_el)
    retraction_status, is_notice = determine_retraction_status(publication_types, notices)
    evidence_type, evidence_provenance = classify_evidence_type(publication_types)

    sections = _parse_abstract_sections(article)
    if not sections:  # Recorded, not raised: a record with no abstract is normal, and the caller decides whether to keep it
        logger.info("record has no abstract text", extra={"pmid": pmid})

    document_id = document_id_for_pubmed(pmid)
    full_text = " ".join(
        section.text for section in sections
    )  # Concatenation used only for the document-level content digest

    return SourceDocument(
        document_id=document_id,
        source_type=SourceType.PUBMED_ABSTRACT,
        source_url=PUBMED_URL_TEMPLATE.format(
            pmid=pmid
        ),  # Constructed, not taken from the payload, so a hostile record cannot inject a link
        identifiers=_parse_identifiers(article, pubmed_data, pmid),
        title=title,
        authors=_parse_authors(article),
        container=container,
        publication_date=publication_date,
        publication_date_precision=publication_precision,
        electronic_publication_date=electronic_date,
        print_publication_date=print_date,
        medline_date_raw=medline_raw,
        retrieved_at=retrieved_at,
        language=flatten_element_text(article.find("Language")) or None,
        publication_status=flatten_element_text(pubmed_data.find("PublicationStatus"))
        if pubmed_data is not None
        else None,
        abstract_sections=sections,
        related_notices=notices,
        is_retraction_notice=is_notice,
        evidence_type=evidence_type,
        evidence_type_provenance=evidence_provenance,
        publication_types=publication_types,
        retraction_status=retraction_status,
        retraction_checked_at=retrieved_at,  # Required whenever status is not UNCHECKED: records WHEN the screening result was determined
        content_sha256=_content_digest(full_text),
        source_pack_version=source_pack_version,
        notes=(
            "Retrieved automatically from PubMed via NCBI E-utilities. "
            "approval_status is 'unreviewed': no clinician has reviewed this record, and automatic "
            "retrieval confers no medical approval of any kind. evidence_type is derived from PubMed "
            "publication-type metadata, not from a clinical judgement."
        ),
    )


def _content_digest(text: str) -> str:
    """SHA-256 of the document's normalised full text."""
    from synapse.hashing import sha256_text

    return sha256_text(text)


def parse_efetch_response(
    payload: bytes,
    *,
    retrieved_at: datetime,
    source_pack_version: str,
) -> list[SourceDocument]:
    """Parse one efetch XML body into source documents.

    Raises:
        ArtifactSchemaError: the payload exceeds the size ceiling or is not
            well-formed XML.
    """
    if (
        len(payload) > MAX_XML_BYTES
    ):  # Bound the work BEFORE parsing rather than discovering the size afterwards
        raise ArtifactSchemaError(
            problem="efetch payload exceeds maximum size",
            size_bytes=len(payload),
            limit_bytes=MAX_XML_BYTES,
        )
    try:
        root = ET.fromstring(payload)  # noqa: S314  # ElementTree resolves no external entities, so XXE does not apply; size is capped above
    except ET.ParseError as exc:
        raise ArtifactSchemaError(
            problem="efetch payload is not well-formed XML"
        ) from exc  # Parser message omitted: it can quote record content

    documents: list[SourceDocument] = []
    for article_el in root.findall(".//PubmedArticle"):
        document = parse_article(
            article_el, retrieved_at=retrieved_at, source_pack_version=source_pack_version
        )
        if document is not None:
            documents.append(document)
    logger.info("parsed efetch batch", extra={"document_count": len(documents)})
    return documents


__all__ = [
    "MAX_XML_BYTES",
    "PUBMED_URL_TEMPLATE",
    "determine_retraction_status",
    "flatten_element_text",
    "normalize_doi",
    "parse_article",
    "parse_efetch_response",
]

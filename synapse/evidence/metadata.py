"""
synapse.evidence.metadata
=========================
Publication and governance metadata for a cited source — read, never invented.

Every field here is copied from a :class:`~synapse.schemas.source_pack.PackSource`
record that a governed source pack already holds. Nothing is derived, guessed,
inferred from a title, or filled in from a model. That is the whole contract of
this module, and it is the reason it exists separately from rendering: a
renderer that could *construct* metadata would eventually construct some.

Optionality is the normal case, not an error path
-------------------------------------------------
Real bibliographic data is patchy. A guideline has an issuing organisation and
no journal; a preprint has a DOI and no PMID; a synthetic example has neither.
So every field below is optional and every consumer must render its absence as
absence — not as "Unknown", which reads like a fact about the source, and not by
substituting a plausible-looking value.

What is deliberately **not** here
---------------------------------
No relevance score, no quality judgement, no "confidence". Retrieval relevance
is a reranker's opinion about a passage's usefulness for one question; it says
nothing about the strength of the evidence, and putting the two in one object is
how they end up in one sentence on a patient's screen. Evidence quality and
review state come from the governed record and from nowhere else (requirement
10), and they live in :mod:`synapse.evidence.labels`.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from synapse.governance.pack import SourcePack
from synapse.schemas.enums import (
    DatePrecision,
    EvidenceType,
    RetractionStatus,
    SourceLifecycleState,
)
from synapse.schemas.source import Author
from synapse.schemas.source_pack import PackSource

MAX_DISPLAYED_AUTHORS = 3  # Beyond this the list is elided with "and colleagues"


def format_author(author: Author) -> str:
    """One author as published, or the empty string if the record has no name.

    A collective name ("Example Working Group") is used verbatim, because that
    is how the work is credited. Nothing is reconstructed from initials.
    """
    if author.is_collective and author.collective_name:
        return author.collective_name.strip()
    family = (author.family or "").strip()
    given = (author.given or "").strip()
    initials = (author.initials or "").strip()
    if family and given:
        return f"{given} {family}"
    if family and initials:
        return f"{initials} {family}"
    if family:
        return family
    return (author.collective_name or "").strip()


def format_authors(authors: list[Author], limit: int = MAX_DISPLAYED_AUTHORS) -> str:
    """A short author line, or the empty string when no author is recorded.

    Elides rather than truncating mid-name: "A Smith, B Jones and colleagues"
    is honest about there being more, where a hard cut is not.
    """
    names = [name for name in (format_author(author) for author in authors) if name]
    if not names:
        return ""
    if len(names) <= limit:
        return ", ".join(names) if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    return ", ".join(names[:limit]) + " and colleagues"


def format_publication_date(value: date | None, precision: DatePrecision | None = None) -> str:
    """A date rendered no more precisely than it is known.

    A PubMed record carrying only a year is stored with January 1st defaulted in
    (:class:`~synapse.schemas.enums.DatePrecision`). Printing "1 January 2019"
    for it would be inventing two facts, so the precision governs the format.
    """
    if value is None:
        return ""
    if precision is DatePrecision.YEAR:
        return value.strftime("%Y")
    if precision is DatePrecision.MONTH:
        return value.strftime("%B %Y")
    return value.strftime("%-d %B %Y") if hasattr(value, "strftime") else str(value)


@dataclass(frozen=True)
class SourceMetadata:
    """Everything the evidence card may show about one source.

    Built only by :meth:`from_pack_source`. The constructor is not private, but
    every field defaults to absent, so a hand-built instance discloses nothing
    it was not explicitly given.
    """

    document_id: str
    title: str = ""
    authors: str = ""  # Pre-formatted display line, or empty
    journal: str = ""
    issuing_organization: str = ""
    publication_date: str = ""  # Pre-formatted to the recorded precision
    revision_date: str = ""
    pmid: str = ""
    doi: str = ""
    guideline_id: str = ""
    canonical_url: str = ""
    evidence_type: EvidenceType | None = None
    lifecycle_state: SourceLifecycleState | None = None
    retraction_status: RetractionStatus | None = None
    superseded_by: str = ""
    reviewed_at: str = ""  # Date of the clinician review, when one exists
    review_due: str = ""
    pack_id: str = ""
    pack_version: str = ""
    pack_approval_state: str = ""

    @property
    def container(self) -> str:
        """Journal, or issuing organisation for a guideline. Empty if neither."""
        return self.journal or self.issuing_organization

    @property
    def has_identifier(self) -> bool:
        """True when at least one external identifier is recorded."""
        return bool(self.pmid or self.doi or self.guideline_id)

    @property
    def is_governed(self) -> bool:
        """True when this metadata came from a source pack rather than nowhere.

        A source retrieved from an index with no corresponding pack record has a
        title and a URL and nothing else. The card says so rather than implying
        a governance status it does not have.
        """
        return bool(self.pack_id)

    @classmethod
    def from_pack_source(
        cls,
        source: PackSource,
        *,
        pack_id: str = "",
        pack_version: str = "",
        pack_approval_state: str = "",
    ) -> SourceMetadata:
        """Copy the displayable fields off a governed pack record."""
        review = source.review
        return cls(
            document_id=source.document_id,
            title=source.title,
            authors=format_authors(source.authors),
            journal=source.container.journal or "",
            issuing_organization=source.container.issuing_organization or "",
            publication_date=format_publication_date(source.publication_date),
            revision_date=format_publication_date(source.revision_date),
            pmid=source.pmid or "",
            doi=source.doi or "",
            guideline_id=source.guideline_id or "",
            canonical_url=source.canonical_url,
            evidence_type=source.evidence_type,
            lifecycle_state=source.lifecycle_state,
            retraction_status=source.retraction_status,
            superseded_by=source.superseded_by or "",
            reviewed_at=review.reviewed_at.date().isoformat() if review else "",
            review_due=review.review_due_at.isoformat() if review else "",
            pack_id=pack_id,
            pack_version=pack_version,
            pack_approval_state=pack_approval_state,
        )

    @classmethod
    def minimal(cls, document_id: str, *, title: str = "", url: str = "") -> SourceMetadata:
        """Metadata for a source with no governed record.

        Used when retrieval returns a document the loaded pack does not describe.
        Carries only what retrieval itself supplied; :attr:`is_governed` is False
        so the card can say "no governance record" rather than leaving a reader
        to assume one exists.
        """
        return cls(document_id=document_id, title=title, canonical_url=url)


@dataclass
class MetadataResolver:
    """Looks up governed metadata by document identifier.

    Built once from a loaded pack and shared. A miss returns ``None`` rather
    than a fabricated record, and the caller decides how to present a source it
    cannot describe.
    """

    by_document: dict[str, SourceMetadata] = field(default_factory=dict)
    pack_id: str = ""
    pack_version: str = ""
    pack_approval_state: str = ""

    @classmethod
    def from_pack(cls, pack: SourcePack) -> MetadataResolver:
        """Index every source in a loaded pack by its document identifier."""
        manifest = pack.manifest
        approval_state = getattr(manifest.approval_state, "value", str(manifest.approval_state))
        resolver = cls(
            pack_id=manifest.pack_id,
            pack_version=manifest.version,
            pack_approval_state=approval_state,
        )
        for source in pack.sources:
            resolver.by_document[source.document_id] = SourceMetadata.from_pack_source(
                source,
                pack_id=manifest.pack_id,
                pack_version=manifest.version,
                pack_approval_state=approval_state,
            )
        return resolver

    @classmethod
    def from_directory(cls, directory: Path) -> MetadataResolver:
        """Load a pack from disk and index it."""
        return cls.from_pack(SourcePack.load(directory))

    @classmethod
    def empty(cls) -> MetadataResolver:
        """A resolver that knows nothing, for deployments with no pack loaded."""
        return cls()

    def get(self, document_id: str) -> SourceMetadata | None:
        """Governed metadata for a document, or ``None`` if the pack lacks it."""
        return self.by_document.get(document_id)

    def resolve(self, document_id: str, *, title: str = "", url: str = "") -> SourceMetadata:
        """Governed metadata when available, otherwise a minimal record.

        Never raises and never invents: the fallback carries only what the
        caller already had.
        """
        found = self.by_document.get(document_id)
        return (
            found
            if found is not None
            else SourceMetadata.minimal(document_id, title=title, url=url)
        )


__all__ = [
    "MAX_DISPLAYED_AUTHORS",
    "MetadataResolver",
    "SourceMetadata",
    "format_author",
    "format_authors",
    "format_publication_date",
]

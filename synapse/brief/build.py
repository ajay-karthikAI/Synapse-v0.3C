"""
synapse.brief.build
===================
From a validated answer to a typed brief. Deterministic, and lossy in one direction only.

The input is a :class:`~synapse.answer.schema.GroundedAnswer` that has already
been through verification and the display policy, plus the same
:class:`~synapse.answer.render.SourceNumbering` the page used. So the brief
inherits two properties rather than re-deriving them:

* **Unsupported claims are already gone.** ``policy.apply`` removed them before
  any renderer saw the answer. The guard in :func:`build_brief` is a second
  line, not the first: it drops anything unsupported that somehow arrives, and
  the schema refuses it after that.
* **Source numbers are the ones on screen.** ``[2]`` in the brief is ``[2]`` on
  the page because both read the same numbering object. Nothing is renumbered
  here.

Determinism matters because a brief is a document a patient may print twice, or
regenerate after editing a note. Same answer plus same numbering plus same
timestamp yields the same bytes, apart from the random document identifier,
which is passed in so a caller can hold it stable across a re-render.
"""

from __future__ import annotations  # Postponed annotations

from datetime import UTC, datetime

from synapse.answer.render import PERMANENT_DISCLAIMER, SourceNumbering
from synapse.answer.schema import GroundedAnswer, SupportStatus
from synapse.brief.schema import (
    MAX_QUESTIONS,
    AppointmentBrief,
    BriefClaim,
    BriefProvenance,
    BriefQuestion,
    BriefSection,
    BriefSource,
    ContentOrigin,
    SupportLevel,
    UserContent,
    new_document_id,
)
from synapse.evidence.labels import describe_evidence_type, describe_review_state
from synapse.evidence.links import canonical_link
from synapse.evidence.metadata import MetadataResolver, SourceMetadata
from synapse.logging import get_logger

logger = get_logger(__name__)

_SUPPORT_MAP = {
    SupportStatus.SUPPORTED: SupportLevel.SUPPORTED,
    SupportStatus.PARTIALLY_SUPPORTED: SupportLevel.PARTIALLY_SUPPORTED,
}


def _source_from_metadata(number: int, metadata: SourceMetadata) -> BriefSource:
    """One source row, from governed metadata only.

    The identifier line prefers the most durable available, and is omitted
    entirely when the record has none — an empty string prints as nothing rather
    than as a label with a blank beside it.
    """
    identifier = ""
    if metadata.pmid:
        identifier = f"PMID {metadata.pmid}"
    elif metadata.doi:
        identifier = f"DOI {metadata.doi}"
    elif metadata.guideline_id:
        identifier = metadata.guideline_id
    return BriefSource(
        number=number,
        source_id=metadata.document_id,
        title=metadata.title,
        container=metadata.container,
        publication_date=metadata.publication_date,
        revision_date=metadata.revision_date,
        identifier=identifier,
        url=canonical_link(metadata.canonical_url, doi=metadata.doi, pmid=metadata.pmid),
        evidence_type_label=describe_evidence_type(metadata.evidence_type).name,
        review_label=describe_review_state(
            metadata.lifecycle_state, is_governed=metadata.is_governed
        ).name,
    )


def build_brief(
    answer: GroundedAnswer,
    numbering: SourceNumbering,
    *,
    resolver: MetadataResolver | None = None,
    user: UserContent | None = None,
    generated_at: datetime | None = None,
    document_id: str | None = None,
    app_version: str = "",
    corpus_version: str = "",
    index_version: str = "",
) -> AppointmentBrief:
    """Assemble a brief from a displayed answer.

    Args:
        answer: the answer **as displayed**, after the display policy removed
            withheld claims.
        numbering: the numbering the page used, so markers agree.
        resolver: governed source metadata. Without one, sources carry only what
            retrieval supplied and are labelled "No governance record".
        user: the user's topic and notes, if they have entered any.
        generated_at: the timestamp to stamp. Defaults to now.
        document_id: pass one to keep a printout's identifier stable across a
            re-render after editing.

    Returns:
        A validated brief. Every claim in it is supported or partially
        supported, and every citation marker resolves.
    """
    resolver = resolver or MetadataResolver.empty()
    stamped_at = generated_at or datetime.now(UTC)

    # Only sources actually cited by a surviving claim reach the brief. A source
    # that was retrieved but ended up backing nothing is not provenance, and on
    # a one-page document it is the first thing to cut.
    cited_ids = {
        source_id
        for claim in answer.claims
        if claim.support_status in _SUPPORT_MAP
        for source_id in claim.source_ids
        if source_id in numbering.refs
    }
    sources = [
        _source_from_metadata(
            ref.number, resolver.resolve(ref.source_id, title=ref.title, url=ref.url)
        )
        for ref in numbering.ordered()
        if ref.source_id in cited_ids
    ]
    number_for = {source.source_id: source.number for source in sources}

    claims: list[BriefClaim] = []
    dropped = 0
    for claim in answer.claims:
        support = _SUPPORT_MAP.get(claim.support_status)
        if support is None:
            # Defence in depth: policy.apply already removed these.
            dropped += 1
            continue
        markers = sorted(
            number_for[source_id] for source_id in claim.source_ids if source_id in number_for
        )
        if not markers:
            # A claim whose sources were all unnumbered cannot show provenance,
            # so it does not go on a document that will outlive the screen.
            dropped += 1
            continue
        claims.append(
            BriefClaim(
                claim_id=claim.claim_id,
                text=claim.text,
                source_numbers=markers,
                support=support,
                origin=ContentOrigin.VERIFIED_EVIDENCE,
            )
        )

    questions = [
        BriefQuestion(question_id=f"q{index}", text=text, origin=ContentOrigin.VERIFIED_EVIDENCE)
        for index, text in enumerate(answer.questions_for_doctor[:MAX_QUESTIONS], start=1)
    ]

    if dropped:
        logger.info("claims omitted from brief", extra={"dropped": dropped})

    return AppointmentBrief(
        document_id=document_id or new_document_id(),
        user=user or UserContent(),
        summary=answer.summary,
        claims=claims,
        questions=questions,
        sources=sources,
        limitations=list(answer.limitations),
        # From the module constant, exactly as the page does it. A brief that
        # loses the disclaimer in export is the failure this guards against.
        disclaimer=PERMANENT_DISCLAIMER,
        provenance=BriefProvenance(
            generated_at=stamped_at,
            app_version=app_version,
            source_pack_id=resolver.pack_id,
            source_pack_version=resolver.pack_version,
            corpus_version=corpus_version,
            index_version=index_version,
        ),
        included_sections=list(BriefSection),
    )


__all__ = ["build_brief"]

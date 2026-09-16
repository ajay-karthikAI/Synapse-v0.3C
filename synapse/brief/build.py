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

from synapse.answer.render import PERMANENT_DISCLAIMER, SourceNumbering, SourceRef
from synapse.answer.schema import AnswerAction, GroundedAnswer, SupportStatus
from synapse.brief.schema import (
    DEFAULT_SECTIONS,
    MAX_QUESTIONS,
    MAX_TRANSCRIPT_ANSWER_CHARS,
    MAX_TRANSCRIPT_QUESTION_CHARS,
    MAX_TRANSCRIPT_TURNS,
    AppointmentBrief,
    BriefClaim,
    BriefProvenance,
    BriefQuestion,
    BriefSection,
    BriefSource,
    BriefTranscriptTurn,
    ContentOrigin,
    SupportLevel,
    TurnStatus,
    UserContent,
    new_document_id,
)
from synapse.evidence.labels import describe_evidence_type, describe_review_state
from synapse.evidence.links import canonical_link
from synapse.evidence.metadata import MetadataResolver, SourceMetadata
from synapse.logging import get_logger
from synapse.service.conversation import Conversation, ConversationTurn

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


def _status_of(turn: ConversationTurn) -> TurnStatus:
    """What a transcript should report for this turn."""
    action = turn.action
    if action is None:
        return TurnStatus.UNAVAILABLE
    return {
        AnswerAction.ANSWER: TurnStatus.ANSWERED,
        AnswerAction.ABSTAIN: TurnStatus.NO_EVIDENCE,
        AnswerAction.MEDICAL_STAFF: TurnStatus.CLINICIAN_REFERRAL,
        AnswerAction.EMERGENCY: TurnStatus.URGENT_ADVICE,
    }[action]


def _transcript_of(conversation: Conversation) -> list[BriefTranscriptTurn]:
    """The whole conversation as transcript turns, oldest first.

    Every turn appears, including the ones that produced no answer. An
    escalation in particular is kept: it is the single exchange a clinician most
    needs to see, and a transcript that quietly dropped it would be a
    flattering edit of the visit.

    The answer text is the summary the patient was shown. For a turn that
    produced no answer it is empty, and the status carries the reason, because
    writing an explanation here would put words in Synapse's mouth that it never
    said on screen.
    """
    turns: list[BriefTranscriptTurn] = []
    for index, turn in enumerate(conversation.turns[:MAX_TRANSCRIPT_TURNS]):
        status = _status_of(turn)
        presentation = turn.outcome.presentation
        summary = presentation.answer.summary if presentation is not None else ""
        if status is not TurnStatus.ANSWERED:
            # Only an answered turn has a summary that describes research. The
            # abstain boilerplate, the escalation copy and the failure notice
            # are all interface text, not something the patient was told about
            # their question.
            summary = ""
        turns.append(
            BriefTranscriptTurn(
                index=index,
                question=turn.query[:MAX_TRANSCRIPT_QUESTION_CHARS],
                answer=summary[:MAX_TRANSCRIPT_ANSWER_CHARS],
                status=status,
            )
        )
    return turns


def _recap_of(conversation: Conversation) -> str:
    """The conversation summary: the verified per-turn summaries, in order.

    **No model call.** A synthesised "summary of the summaries" would be text
    that no verifier ever checked against a retrieved passage, printed on a
    sheet a patient hands to a clinician — precisely the fabrication that claim
    verification exists to prevent. So the recap is assembled, not written: each
    paragraph is a summary the answer layer already produced and the display
    policy already cleared.

    Paragraphs are separated by a blank line, and the renderers split on that.
    Duplicates are collapsed: a follow-up on the same topic often returns the
    same summary, and printing it twice reads as a stutter.
    """
    paragraphs: list[str] = []
    for turn in conversation.turns:
        presentation = turn.outcome.presentation
        if presentation is None:
            continue
        answer = presentation.answer
        if answer.action is not AnswerAction.ANSWER:
            continue
        summary = answer.summary.strip()
        if summary and summary not in paragraphs:
            paragraphs.append(summary)
    return "\n\n".join(paragraphs)


def build_conversation_brief(
    conversation: Conversation,
    *,
    resolver: MetadataResolver | None = None,
    user: UserContent | None = None,
    generated_at: datetime | None = None,
    document_id: str | None = None,
    app_version: str = "",
    corpus_version: str = "",
    index_version: str = "",
) -> AppointmentBrief:
    """One brief for a whole conversation, rather than one per answer.

    A patient leaves with one sheet of paper for one appointment, not a printout
    per question, so this merges every answered turn into a single document.

    Two things have to be reconciled to do that, and both are why this is not a
    loop over :func:`build_brief`:

    **Source numbers are global here.** Each turn carried its own
    :class:`~synapse.answer.render.SourceNumbering`, so ``[1]`` meant a
    different study on turn one than on turn three. Numbers are reassigned in
    order of first appearance across the conversation and every marker is
    remapped through the same table, because two different studies printed as
    ``[1]`` on one page is a citation error, not a display quirk.

    **Identifiers are namespaced by turn.** ``claim_id`` is unique within an
    answer, not across a conversation, and the schema requires uniqueness within
    a brief. Each is prefixed with its turn index.

    Emergency turns contribute nothing but a transcript line: no claim, no
    source, no question. They were escalated ahead of retrieval, so they have no
    research to contribute, and their copy belongs on the screen that showed it.

    Args:
        conversation: the session's conversation, in order.
        resolver: governed source metadata, as for :func:`build_brief`.
        user: the patient's topic and notes.
        generated_at: the timestamp to stamp. Defaults to now.
        document_id: pass the existing one so the identifier survives a rebuild
            when a new turn is added.

    Returns:
        A validated brief whose default sections are the recap and the
        questions. The transcript and the research sections are present in the
        model and omitted from the print until the patient asks for them.
    """
    resolver = resolver or MetadataResolver.empty()
    stamped_at = generated_at or datetime.now(UTC)

    # Pass one: assign global numbers in order of first appearance, counting
    # only sources a surviving claim actually cites.
    global_number: dict[str, int] = {}
    ordered_refs: list[tuple[int, SourceRef]] = []
    for turn in conversation.turns:
        presentation = turn.outcome.presentation
        if presentation is None or presentation.answer.action is not AnswerAction.ANSWER:
            continue
        answer = presentation.answer
        numbering = presentation.numbering
        cited = {
            source_id
            for claim in answer.claims
            if claim.support_status in _SUPPORT_MAP
            for source_id in claim.source_ids
            if source_id in numbering.refs
        }
        for ref in numbering.ordered():
            if ref.source_id in cited and ref.source_id not in global_number:
                global_number[ref.source_id] = len(global_number) + 1
                ordered_refs.append((global_number[ref.source_id], ref))

    sources = [
        _source_from_metadata(number, resolver.resolve(ref.source_id, title=ref.title, url=ref.url))
        for number, ref in ordered_refs
    ]

    # Pass two: claims, questions and limitations, remapped onto those numbers.
    claims: list[BriefClaim] = []
    questions: list[BriefQuestion] = []
    limitations: list[str] = []
    seen_questions: set[str] = set()
    dropped = 0

    for index, turn in enumerate(conversation.turns):
        presentation = turn.outcome.presentation
        if presentation is None or presentation.answer.action is not AnswerAction.ANSWER:
            continue
        answer = presentation.answer

        for claim in answer.claims:
            support = _SUPPORT_MAP.get(claim.support_status)
            if support is None:
                dropped += 1
                continue
            markers = sorted(
                global_number[source_id]
                for source_id in claim.source_ids
                if source_id in global_number
            )
            if not markers:
                dropped += 1
                continue
            claims.append(
                BriefClaim(
                    claim_id=f"t{index}-{claim.claim_id}",
                    text=claim.text,
                    source_numbers=markers,
                    support=support,
                    origin=ContentOrigin.VERIFIED_EVIDENCE,
                )
            )

        for text in answer.questions_for_doctor:
            # Deduplicated on the normalised text. Follow-ups on one topic tend
            # to suggest the same question again, and a list that repeats it is
            # a list a patient stops reading.
            key = " ".join(text.split()).casefold()
            if not key or key in seen_questions:
                continue
            seen_questions.add(key)
            questions.append(
                BriefQuestion(
                    question_id=f"t{index}q{len(questions) + 1}",
                    text=text,
                    origin=ContentOrigin.VERIFIED_EVIDENCE,
                )
            )

        for limitation in answer.limitations:
            if limitation not in limitations:
                limitations.append(limitation)

    if dropped:
        logger.info("claims omitted from brief", extra={"dropped": dropped})

    return AppointmentBrief(
        document_id=document_id or new_document_id(),
        user=user or UserContent(),
        summary=_recap_of(conversation),
        claims=claims,
        # The ceiling applies to the whole conversation, so a long session is
        # truncated rather than allowed to print a fourth page of questions.
        questions=questions[:MAX_QUESTIONS],
        sources=sources,
        limitations=limitations,
        transcript=_transcript_of(conversation),
        disclaimer=PERMANENT_DISCLAIMER,
        provenance=BriefProvenance(
            generated_at=stamped_at,
            app_version=app_version,
            source_pack_id=resolver.pack_id,
            source_pack_version=resolver.pack_version,
            corpus_version=corpus_version,
            index_version=index_version,
        ),
        included_sections=list(DEFAULT_SECTIONS),
    )


__all__ = ["build_brief", "build_conversation_brief"]

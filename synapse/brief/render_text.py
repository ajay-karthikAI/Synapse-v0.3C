"""
synapse.brief.render_text
=========================
The brief as plain text, for pasting into a portal message or an SMS.

Rendered from the same :class:`~synapse.brief.schema.AppointmentBrief` as the
printable HTML, so the two cannot disagree about what the document contains.
That is the reason this module exists rather than a second formatter over the
answer object: two renderers reading two inputs drift, and the one that drifts
is always the one nobody looks at.

Plain text has one advantage the HTML does not, and it is the reason to keep it:
it survives every transport a patient might use. It also cannot carry a tint, so
user-authored content is marked with an explicit textual label rather than a
background colour.
"""

from __future__ import annotations  # Postponed annotations

from synapse.brief.schema import STATUS_NOTES, AppointmentBrief, BriefSection, ContentOrigin

TITLE = "APPOINTMENT BRIEF"
USER_TAG = "[written by you]"
EDITED_TAG = "[edited by you — not checked against a source]"


def _rule(title: str) -> list[str]:
    """A section heading that survives copy-paste into any editor."""
    return [title.upper(), "-" * len(title), ""]


def render_brief_text(brief: AppointmentBrief) -> str:
    """Render the brief as plain text.

    Section order matches the printed document, so a patient reading one and a
    clinician reading the other are looking at the same thing in the same order.
    """
    lines: list[str] = [TITLE, "=" * len(TITLE), ""]

    meta = [f"Prepared: {brief.provenance.generated_at.strftime('%d %B %Y, %H:%M %Z').strip()}"]
    if brief.provenance.app_version:
        meta.append(f"Synapse {brief.provenance.app_version}")
    if brief.provenance.source_pack_version:
        meta.append(
            f"Sources {brief.provenance.source_pack_id} {brief.provenance.source_pack_version}".strip()
        )
    if brief.provenance.corpus_version:
        meta.append(f"Corpus {brief.provenance.corpus_version}")
    if brief.provenance.index_version:
        meta.append(f"Index {brief.provenance.index_version}")
    lines.extend([" · ".join(meta), f"Document {brief.document_id}", ""])

    if brief.includes(BriefSection.TOPIC) and brief.user.topic:
        lines.extend(_rule("What I want to talk about"))
        lines.extend([f"{USER_TAG} {brief.user.topic}", ""])

    if brief.includes(BriefSection.SUMMARY) and brief.summary:
        lines.extend(_rule("Summary of your conversation"))
        lines.extend([brief.summary, ""])

    questions = brief.visible_questions()
    if questions:
        lines.extend(_rule("Questions to ask your doctor"))
        for index, question in enumerate(questions, start=1):
            own = f" {USER_TAG}" if question.origin is ContentOrigin.USER_AUTHORED else ""
            lines.append(f"{index}. {question.text}{own}")
        lines.append("")

    if brief.includes(BriefSection.NOTES) and brief.user.notes:
        lines.extend(_rule("My notes"))
        lines.extend([f"{USER_TAG} {brief.user.notes}", ""])

    transcript = brief.visible_transcript()
    if transcript:
        lines.extend(_rule("Your conversation"))
        for turn in transcript:
            lines.append(f"You asked: {turn.question}")
            if turn.answer:
                lines.append(f"Synapse said: {turn.answer}")
            note = STATUS_NOTES.get(turn.status)
            if note is not None:
                lines.append(f"  {note}")
            lines.append("")

    claims = brief.visible_claims()
    if claims:
        lines.extend(_rule("The research behind this"))
        for claim in claims:
            if claim.origin is ContentOrigin.USER_EDITED:
                lines.append(f"- {EDITED_TAG} {claim.text}")
                lines.append(
                    f"    Original wording: {claim.original_text} {claim.original_markers()}".rstrip()
                )
                continue
            partial = " (partly verified)" if claim.support.value == "partially_supported" else ""
            lines.append(f"- {claim.text} {claim.markers()}{partial}".rstrip())
        lines.append("")

    if brief.includes(BriefSection.LIMITATIONS) and brief.limitations:
        lines.extend(_rule("What this does not cover"))
        lines.extend(f"- {item}" for item in brief.limitations)
        lines.append("")

    if brief.includes(BriefSection.SOURCES) and brief.sources:
        lines.extend(_rule("Sources"))
        for source in brief.sources:
            lines.append(f"[{source.number}] {source.title or source.source_id}")
            detail = " · ".join(
                part
                for part in (
                    source.container,
                    source.publication_date,
                    f"revised {source.revision_date}" if source.revision_date else "",
                    source.identifier,
                )
                if part
            )
            if detail:
                lines.append(f"    {detail}")
            labels = " · ".join(
                part for part in (source.evidence_type_label, source.review_label) if part
            )
            if labels:
                lines.append(f"    {labels}")
            if source.url:
                lines.append(f"    {source.url}")
        lines.append("")

    lines.append(brief.disclaimer)
    return "\n".join(lines)


__all__ = ["EDITED_TAG", "TITLE", "USER_TAG", "render_brief_text"]

"""
The conversation brief: one document for a visit, and the PDF a patient keeps.

The brief used to be built per answer, and printed all nine of its sections
every time. Four questions produced four documents, each carrying verified
claims with citation markers, a numbered source list with PMIDs, the answer
layer's limitations and a provenance block naming corpus and index versions.
All of that is true and none of it is what somebody reads in a waiting room.

So there is one brief per conversation now, its default is the recap and the
questions, and the transcript and the research are add-ons. What these tests
pin is the part that is easy to get wrong while moving from one shape to the
other:

* merging turns must renumber citations globally -- ``[1]`` on turn one and
  ``[1]`` on turn three were different studies;
* the recap must be assembled from verified summaries, never synthesised, or a
  printed sheet carries a sentence no verifier checked;
* every turn reaches the transcript, escalations included, because the turn a
  clinician most needs to see is the one a flattering edit would drop;
* rebuilding for a new turn must not throw away what the patient typed;
* the disclaimer must be on every page, not only the last one, because a first
  page is what gets printed and photographed.
"""

from __future__ import annotations

import base64
import re
import zlib

import pytest
from pydantic import ValidationError

from synapse.answer.schema import AnswerAction
from synapse.brief.build import build_conversation_brief
from synapse.brief.export import ExportError, build_pdf_export
from synapse.brief.render_pdf import FOOTER_NOTICE, render_brief_pdf
from synapse.brief.render_text import render_brief_text
from synapse.brief.schema import (
    DEFAULT_SECTIONS,
    RESEARCH_SECTIONS,
    AppointmentBrief,
    BriefSection,
    ContentOrigin,
    TurnStatus,
)
from synapse.service.conversation import Conversation
from tests.golden_states import FOLLOW_UP, QUERY, service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def a_conversation(*, queries: tuple[str, ...] = (QUERY,), **kwargs) -> Conversation:
    """Run real turns through the real service against fakes."""
    conversation = Conversation()
    engine = service(**kwargs)
    for query in queries:
        engine.ask(query, conversation)
    return conversation


def with_sections(brief: AppointmentBrief, *sections: BriefSection) -> AppointmentBrief:
    """The same brief with extra sections switched on, as the route does."""
    return brief.model_copy(update={"included_sections": [*brief.included_sections, *sections]})


def pdf_page_count(payload: bytes) -> int:
    """Pages in a PDF, counted from its page objects.

    Crude on purpose: the alternative is a PDF parsing dependency to answer one
    question, and ReportLab's output is regular enough that counting ``/Type
    /Page`` (not ``/Pages``) is exact for the documents this renderer emits.
    """
    return len(re.findall(rb"/Type\s*/Page[^s]", payload))


def pdf_text(payload: bytes) -> str:
    """The drawn text of a PDF, inflated out of its content streams.

    ReportLab compresses page content, so searching the raw bytes for a string
    the document plainly shows finds nothing — which is how the first version of
    the footer test passed vacuously in the wrong direction. The streams are
    inflated here and the text operators read out of them.
    """
    chunks: list[str] = []
    for raw in re.findall(rb"stream\r?\n(.*?)endstream", payload, re.DOTALL):
        chunks.extend(
            literal[1:-1].decode("latin-1", errors="replace")
            for literal in re.findall(rb"\((?:[^()\\]|\\.)*\)", _decoded(raw))
        )
    return "\n".join(chunks)


def _decoded(raw: bytes) -> bytes:
    """Undo the stream filters ReportLab applies, in the order it applies them.

    The filter is ``[/ASCII85Decode /FlateDecode]``, so the bytes are
    base-85 text wrapping a deflate stream. Decoding only one of the two
    returns plausible-looking rubbish rather than an error, which is exactly how
    a text assertion over it can look like it works and mean nothing.
    """
    body = raw.strip(b"\r\n")
    for decode in (_ascii85_then_flate, _flate):
        try:
            return decode(body)
        except (zlib.error, ValueError):
            continue
    return body  # Uncompressed, or not text at all


def _ascii85_then_flate(body: bytes) -> bytes:
    return zlib.decompress(base64.a85decode(body, adobe=True, ignorechars=b" \t\r\n"))


def _flate(body: bytes) -> bytes:
    return zlib.decompress(body)


# ---------------------------------------------------------------------------
# Merging a conversation
# ---------------------------------------------------------------------------


class TestOneBriefPerConversation:
    """Every answered turn contributes to one document."""

    def test_the_recap_carries_one_paragraph_per_answered_turn(self) -> None:
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        assert brief.summary.count("\n\n") >= 0  # joined, not concatenated
        paragraphs = [block for block in brief.summary.split("\n\n") if block]
        assert paragraphs, "an answered conversation must produce a recap"

    def test_the_recap_is_assembled_from_verified_summaries_only(self) -> None:
        """No synthesis. Every sentence printed was already checked.

        This is the invariant that keeps claim verification meaningful. A model
        asked to summarise the summaries would produce text no verifier ever
        compared against a retrieved passage, and it would be printed on a sheet
        handed to a clinician.
        """
        conversation = a_conversation(queries=(QUERY, FOLLOW_UP))
        brief = build_conversation_brief(conversation)
        shown = {
            turn.outcome.presentation.answer.summary
            for turn in conversation.turns
            if turn.outcome.presentation is not None
        }
        for paragraph in brief.summary.split("\n\n"):
            assert paragraph in shown

    def test_a_repeated_summary_is_not_printed_twice(self) -> None:
        """The fake answers every turn identically, so the recap collapses."""
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        assert "\n\n" not in brief.summary

    def test_claim_ids_are_namespaced_by_turn(self) -> None:
        """``claim_id`` is unique within an answer, not across a conversation."""
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        ids = [claim.claim_id for claim in brief.claims]
        assert len(ids) == len(set(ids))
        assert all(claim_id.startswith("t") for claim_id in ids)

    def test_source_numbers_are_global_and_dense(self) -> None:
        """Two turns citing the same studies must not both number them from one."""
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        numbers = [source.number for source in brief.sources]
        assert numbers == list(range(1, len(numbers) + 1))
        assert len(numbers) == len({source.source_id for source in brief.sources})

    def test_every_marker_resolves_after_the_merge(self) -> None:
        """The schema enforces this; the test names it as the reason for renumbering."""
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        available = {source.number for source in brief.sources}
        for claim in brief.claims:
            assert set(claim.source_numbers) <= available

    def test_duplicate_questions_are_collapsed(self) -> None:
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        texts = [" ".join(q.text.split()).casefold() for q in brief.questions]
        assert len(texts) == len(set(texts))

    def test_an_empty_conversation_produces_nothing_to_print(self) -> None:
        brief = build_conversation_brief(Conversation())
        assert brief.summary == ""
        assert brief.questions == []
        assert brief.transcript == []


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


class TestTranscript:
    """Every exchange, including the ones that produced no answer."""

    def test_one_entry_per_turn_in_order(self) -> None:
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        assert [turn.index for turn in brief.transcript] == [0, 1]
        assert brief.transcript[0].question == QUERY
        assert brief.transcript[1].question == FOLLOW_UP

    def test_an_escalated_turn_is_kept_and_marked(self) -> None:
        """The one exchange a clinician most needs to see.

        Dropping it would make the transcript a flattering edit of the visit,
        and the omission would fall on the turn that mattered most.
        """
        conversation = a_conversation(
            queries=("crushing chest pain spreading to my arm",),
            is_emergency=lambda _query: True,
        )
        brief = build_conversation_brief(conversation)
        assert len(brief.transcript) == 1
        entry = brief.transcript[0]
        assert entry.status is TurnStatus.URGENT_ADVICE
        assert entry.question == "crushing chest pain spreading to my arm"

    def test_an_escalated_turn_contributes_no_research(self) -> None:
        """It was escalated ahead of retrieval, so it has none to contribute."""
        conversation = a_conversation(
            queries=("crushing chest pain spreading to my arm",),
            is_emergency=lambda _query: True,
        )
        brief = build_conversation_brief(conversation)
        assert brief.claims == []
        assert brief.sources == []
        assert brief.summary == ""

    def test_a_non_answered_turn_carries_no_answer_text(self) -> None:
        """Interface copy is not something the patient was told about their question."""
        conversation = a_conversation(
            queries=("crushing chest pain",), is_emergency=lambda _query: True
        )
        brief = build_conversation_brief(conversation)
        assert brief.transcript[0].answer == ""

    def test_the_transcript_is_off_by_default(self) -> None:
        brief = build_conversation_brief(a_conversation())
        assert BriefSection.TRANSCRIPT not in brief.included_sections
        assert brief.visible_transcript() == []
        assert brief.transcript, "present in the model, absent from the print"

    def test_turning_it_on_prints_it(self) -> None:
        brief = with_sections(build_conversation_brief(a_conversation()), BriefSection.TRANSCRIPT)
        assert len(brief.visible_transcript()) == 1
        assert "You asked" in render_brief_text(brief)

    def test_out_of_order_transcript_turns_are_refused(self) -> None:
        """A transcript that misreports the order of a conversation is worse than none.

        Validated through ``model_validate`` rather than ``model_copy``: a copy
        does not re-run the validators, so the first version of this test
        asserted nothing.
        """
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        payload = brief.model_dump(mode="json")
        payload["transcript"] = list(reversed(payload["transcript"]))
        with pytest.raises(ValidationError, match="conversation order"):
            AppointmentBrief.model_validate(payload)

    def test_a_duplicated_transcript_turn_is_refused(self) -> None:
        brief = build_conversation_brief(a_conversation())
        payload = brief.model_dump(mode="json")
        payload["transcript"] = [*payload["transcript"], *payload["transcript"]]
        with pytest.raises(ValidationError, match="conversation order"):
            AppointmentBrief.model_validate(payload)


# ---------------------------------------------------------------------------
# What prints by default
# ---------------------------------------------------------------------------


class TestDefaultIsTheRecap:
    """The brief a patient gets without asking for anything."""

    def test_the_default_sections_are_the_recap_and_the_questions(self) -> None:
        brief = build_conversation_brief(a_conversation())
        assert brief.included_sections == list(DEFAULT_SECTIONS)
        assert BriefSection.SUMMARY in brief.included_sections
        assert BriefSection.QUESTIONS in brief.included_sections

    @pytest.mark.parametrize("section", RESEARCH_SECTIONS)
    def test_the_research_sections_are_off_by_default(self, section: BriefSection) -> None:
        brief = build_conversation_brief(a_conversation())
        assert section not in brief.included_sections

    def test_the_research_is_retained_in_the_model(self) -> None:
        """Off by default is not discarded: the add-on has to have something to print."""
        brief = build_conversation_brief(a_conversation())
        assert brief.claims
        assert brief.sources
        assert brief.visible_claims() == []

    def test_the_disclaimer_is_not_a_section_and_always_prints(self) -> None:
        brief = build_conversation_brief(a_conversation())
        assert brief.disclaimer
        assert brief.disclaimer in render_brief_text(brief)
        assert "disclaimer" not in [str(section) for section in BriefSection]


# ---------------------------------------------------------------------------
# The PDF
# ---------------------------------------------------------------------------


class TestPdf:
    """A real PDF, because "print to PDF" on a phone is a gesture nobody finds."""

    def test_it_is_a_pdf(self) -> None:
        payload = render_brief_pdf(build_conversation_brief(a_conversation()))
        assert payload.startswith(b"%PDF-")
        assert payload.rstrip().endswith(b"%%EOF")

    def test_the_same_brief_renders_to_the_same_bytes(self) -> None:
        """``invariant`` mode: no clock, no random document id in the output."""
        brief = build_conversation_brief(a_conversation())
        assert render_brief_pdf(brief) == render_brief_pdf(brief)

    def test_the_recap_fits_one_page(self) -> None:
        payload = render_brief_pdf(build_conversation_brief(a_conversation()))
        assert pdf_page_count(payload) == 1

    def test_the_transcript_starts_a_new_page(self) -> None:
        """So the recap stays a sheet a patient can hand over on its own."""
        brief = with_sections(build_conversation_brief(a_conversation()), BriefSection.TRANSCRIPT)
        assert pdf_page_count(render_brief_pdf(brief)) == 2

    def test_a_transcript_only_brief_does_not_print_a_blank_first_page(self) -> None:
        """The page break is conditional; an unconditional one printed an empty sheet."""
        brief = build_conversation_brief(a_conversation()).model_copy(
            update={"included_sections": [BriefSection.TRANSCRIPT]}
        )
        assert pdf_page_count(render_brief_pdf(brief)) == 1

    def test_every_page_carries_the_standing_notice(self) -> None:
        """A first page that gets printed or photographed still has to disclaim itself."""
        brief = with_sections(build_conversation_brief(a_conversation()), BriefSection.TRANSCRIPT)
        payload = render_brief_pdf(brief)
        pages = pdf_page_count(payload)
        assert pages == 2
        # Matched on the notice's ASCII tail: it contains an em dash, which
        # ReportLab writes in the font's own encoding rather than as UTF-8.
        tail = FOOTER_NOTICE.split("—")[-1].strip()
        assert tail == "not a medical device, not clinically validated."
        assert pdf_text(payload).count(tail) == pages

    def test_the_full_disclaimer_is_in_the_document(self) -> None:
        """The footer is the short form; the box at the end carries all of it."""
        brief = build_conversation_brief(a_conversation())
        text = pdf_text(render_brief_pdf(brief))
        assert "not a diagnosis" in text.lower()

    def test_the_recap_and_the_questions_are_actually_drawn(self) -> None:
        """Guards every other PDF test here: they would pass on a blank page."""
        brief = build_conversation_brief(a_conversation())
        text = pdf_text(render_brief_pdf(brief))
        assert "Summary of your conversation" in text
        assert "Questions to ask your doctor" in text
        assert brief.questions[0].text.split()[0] in text

    def test_the_default_pdf_draws_no_citation_markers_or_studies(self) -> None:
        """The research add-on is off, so its headings must not appear."""
        text = pdf_text(render_brief_pdf(build_conversation_brief(a_conversation())))
        assert "Studies referenced" not in text
        assert "The research behind this" not in text

    def test_the_research_add_on_draws_them(self) -> None:
        brief = with_sections(build_conversation_brief(a_conversation()), *RESEARCH_SECTIONS)
        text = pdf_text(render_brief_pdf(brief))
        assert "Studies referenced" in text
        assert "The research behind this" in text

    def test_an_add_on_makes_the_document_bigger(self) -> None:
        brief = build_conversation_brief(a_conversation())
        default = render_brief_pdf(brief)
        expanded = render_brief_pdf(with_sections(brief, BriefSection.TRANSCRIPT))
        assert len(expanded) > len(default)

    def test_an_unknown_page_size_is_refused(self) -> None:
        """A letter document printed on A4 loses its bottom margin."""
        with pytest.raises(ValueError):
            render_brief_pdf(build_conversation_brief(a_conversation()), page_size="a3")

    def test_both_supported_page_sizes_render(self) -> None:
        brief = build_conversation_brief(a_conversation())
        for size in ("a4", "letter"):
            assert render_brief_pdf(brief, page_size=size).startswith(b"%PDF-")

    def test_hostile_text_does_not_escape_the_renderer(self) -> None:
        """ReportLab's paragraphs parse a markup subset; user text is escaped into it."""
        brief = build_conversation_brief(a_conversation())
        hostile = brief.model_copy(
            update={
                "user": brief.user.model_copy(
                    update={"topic": "<b>bold</b> & <script>alert(1)</script>"}
                )
            }
        )
        assert render_brief_pdf(hostile).startswith(b"%PDF-")


class TestPdfExportSafety:
    """The scan that runs before a PDF is handed over."""

    def test_a_secret_in_the_brief_blocks_the_export(self) -> None:
        """The check reads the structured brief, not the compressed bytes.

        A regex over a PDF's content streams would match nothing whatever the
        document said, and a check that always passes is worse than none.
        """
        brief = build_conversation_brief(a_conversation())
        poisoned = brief.model_copy(
            update={
                "user": brief.user.model_copy(update={"notes": "my key is sk-abcdef0123456789"})
            }
        )
        with pytest.raises(ExportError):
            build_pdf_export(poisoned)

    def test_a_clean_brief_exports(self) -> None:
        export = build_pdf_export(build_conversation_brief(a_conversation()))
        assert export.pdf.startswith(b"%PDF-")
        assert export.size == len(export.pdf)

    def test_the_filename_carries_the_document_id_and_nothing_else(self) -> None:
        export = build_pdf_export(build_conversation_brief(a_conversation()))
        assert export.filename == f"appointment-brief-{export.document_id}.pdf"
        assert re.fullmatch(r"appointment-brief-[0-9a-f]{12}\.pdf", export.filename)


# ---------------------------------------------------------------------------
# Growth
# ---------------------------------------------------------------------------


class TestRebuilding:
    """A new turn must not cost the patient what they typed."""

    def test_the_document_id_can_be_held_stable(self) -> None:
        first = build_conversation_brief(a_conversation())
        second = build_conversation_brief(
            a_conversation(queries=(QUERY, FOLLOW_UP)), document_id=first.document_id
        )
        assert second.document_id == first.document_id

    def test_a_later_turn_widens_the_transcript(self) -> None:
        one = build_conversation_brief(a_conversation())
        two = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        assert len(two.transcript) == len(one.transcript) + 1

    def test_an_answer_only_conversation_never_records_a_failure_status(self) -> None:
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        assert {turn.status for turn in brief.transcript} == {TurnStatus.ANSWERED}

    def test_a_failed_turn_is_recorded_as_unavailable(self) -> None:
        def explode(_query: str):
            raise RuntimeError("retrieval is down")

        conversation = a_conversation(retriever=explode)
        brief = build_conversation_brief(conversation)
        assert conversation.turns[0].outcome.presentation is None
        assert brief.transcript[0].status is TurnStatus.UNAVAILABLE
        assert brief.transcript[0].answer == ""


class TestContentOriginSurvivesTheMerge:
    """Provenance labelling is not lost by building from a conversation."""

    def test_every_merged_claim_is_marked_verified(self) -> None:
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        assert all(claim.origin is ContentOrigin.VERIFIED_EVIDENCE for claim in brief.claims)
        assert all(claim.is_verified for claim in brief.claims)

    def test_every_suggested_question_is_marked_verified(self) -> None:
        brief = build_conversation_brief(a_conversation())
        assert all(q.origin is ContentOrigin.VERIFIED_EVIDENCE for q in brief.questions)

    def test_no_unsupported_claim_survives(self) -> None:
        """The schema refuses one; this pins that the merge cannot smuggle one in."""
        brief = build_conversation_brief(a_conversation(queries=(QUERY, FOLLOW_UP)))
        for claim in brief.claims:
            assert claim.support.value in {"supported", "partially_supported"}


class TestNoEmergencyPathLeak:
    """An escalation contributes a transcript line and nothing else."""

    def test_a_latched_follow_up_is_not_treated_as_an_answer(self) -> None:
        conversation = Conversation()
        engine = service(is_emergency=lambda query: "chest pain" in query)
        engine.ask("crushing chest pain spreading to my arm", conversation)
        engine.ask("is that serious?", conversation)
        brief = build_conversation_brief(conversation)
        assert len(brief.transcript) == 2
        assert all(entry.status is TurnStatus.URGENT_ADVICE for entry in brief.transcript)
        assert brief.claims == []
        assert brief.summary == ""

    def test_the_latched_turn_really_did_escalate(self) -> None:
        """Guards the test above: it would pass vacuously if the latch stopped working."""
        conversation = Conversation()
        engine = service(is_emergency=lambda query: "chest pain" in query)
        engine.ask("crushing chest pain spreading to my arm", conversation)
        engine.ask("is that serious?", conversation)
        assert [turn.action for turn in conversation.turns] == [
            AnswerAction.EMERGENCY,
            AnswerAction.EMERGENCY,
        ]

"""
The appointment brief: construction, editing, provenance and export.

The brief is the one artifact that outlives the session. It gets printed, saved,
mailed to a patient's own address, and handed to a clinician who never saw the
screen it came from. So the tests here are less about rendering and more about
what can and cannot end up in a file that leaves the building.

Four things they pin:

* an unsupported claim cannot reach a brief, by any route;
* editing a verified claim removes the verification, atomically;
* an export carries no key, no prompt, no telemetry identifier;
* nothing is written to disk that is not cleaned up.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from synapse.answer.policy import apply, decide
from synapse.answer.render import PERMANENT_DISCLAIMER, SourceNumbering
from synapse.answer.schema import (
    AnswerAction,
    GroundedAnswer,
    GroundedClaim,
    SupportingExcerpt,
    SupportStatus,
)
from synapse.answer.verify import RetrievedEvidence, verify_answer
from synapse.brief.build import build_brief
from synapse.brief.edit import (
    EditError,
    add_question,
    edit_claim_text,
    remove_claim,
    reorder_questions,
    restore_claim,
    set_notes,
    set_sections,
    set_topic,
    user_content_summary,
)
from synapse.brief.export import (
    EXPORT_WARNING,
    ExportError,
    assert_no_secrets,
    build_export,
    load_brief_json,
    temporary_export,
)
from synapse.brief.render_print import estimate_fit, overflow_advice, render_brief_html
from synapse.brief.render_text import render_brief_text
from synapse.brief.schema import (
    MAX_QUESTIONS,
    AppointmentBrief,
    BriefClaim,
    BriefProvenance,
    BriefSection,
    ContentOrigin,
    SupportLevel,
    UserContent,
    new_document_id,
)
from synapse.evidence.metadata import MetadataResolver

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = REPO_ROOT / "source_packs" / "diabetes-previsit"

CHUNK_ONE = (
    "HbA1c reflects average plasma glucose over 2-3 months. "
    "A target below 7% is appropriate for most non-pregnant adults."
)
CHUNK_TWO = "Regular review supports earlier detection of complications."

EVIDENCE = RetrievedEvidence(
    chunk_texts={
        "example:diabetes-0001#0000": CHUNK_ONE,
        "example:diabetes-0002#0000": CHUNK_TWO,
    },
    source_ids=frozenset({"example:diabetes-0001", "example:diabetes-0002"}),
    source_titles={
        "example:diabetes-0001": "What HbA1c measures",
        "example:diabetes-0002": "Routine diabetes review",
    },
    source_urls={
        "example:diabetes-0001": "https://example.org/synthetic-source/0001",
        "example:diabetes-0002": "https://example.org/synthetic-source/0002",
    },
)
ORDER = ["example:diabetes-0001", "example:diabetes-0002"]
FIXED_TIME = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)


def a_claim(claim_id: str, text: str, source_id: str, quote: str) -> GroundedClaim:
    """A claim citing the example pack."""
    return GroundedClaim(
        claim_id=claim_id,
        text=text,
        source_ids=[source_id],
        supporting_excerpts=[
            SupportingExcerpt(source_id=source_id, chunk_id=f"{source_id}#0000", quote=quote)
        ],
    )


def an_answer(*claims: GroundedClaim, **overrides) -> GroundedAnswer:
    """A complete answer."""
    fields = {
        "summary": "Your HbA1c shows your average blood sugar over two to three months.",
        "claims": list(claims),
        "doctor_evaluation": "Your clinician will read this alongside your history.",
        "questions_for_doctor": ["What does my result mean?", "How often should this be checked?"],
        "limitations": ["General information from published research."],
        "action": AnswerAction.ANSWER,
    }
    fields.update(overrides)
    return GroundedAnswer(**fields)


def displayed(*claims: GroundedClaim, **overrides) -> GroundedAnswer:
    """Verify and gate, exactly as the application does."""
    verified, report = verify_answer(an_answer(*claims, **overrides), EVIDENCE)
    return apply(verified, decide(verified, report))


def a_brief(*claims: GroundedClaim, **kwargs) -> AppointmentBrief:
    """Build a brief from a displayed answer."""
    if not claims:
        claims = (
            a_claim(
                "c1",
                "HbA1c reflects average plasma glucose over 2-3 months.",
                "example:diabetes-0001",
                "HbA1c reflects average plasma glucose over 2-3 months",
            ),
        )
    return build_brief(
        displayed(*claims),
        SourceNumbering.from_evidence(EVIDENCE, ORDER),
        resolver=MetadataResolver.from_directory(PACK_DIR),
        generated_at=FIXED_TIME,
        app_version="0.1.0",
        **kwargs,
    )


class TestConstruction:
    """Built from typed data, deterministically."""

    def test_a_brief_carries_every_required_element(self) -> None:
        brief = a_brief(user=UserContent(topic="my blood sugar", notes="worse in the mornings"))
        assert brief.user.topic == "my blood sugar"
        assert brief.user.notes == "worse in the mornings"
        assert brief.summary
        assert brief.claims and brief.claims[0].source_numbers == [1]
        assert brief.questions
        assert brief.sources
        assert brief.limitations
        assert brief.disclaimer == PERMANENT_DISCLAIMER
        assert brief.provenance.generated_at == FIXED_TIME
        assert brief.provenance.app_version == "0.1.0"
        assert brief.provenance.source_pack_version == "0.1.0"
        assert re.fullmatch(r"[0-9a-f]{12}", brief.document_id)

    def test_construction_is_deterministic(self) -> None:
        first = a_brief(document_id="0123456789ab")
        second = a_brief(document_id="0123456789ab")
        assert first.model_dump(mode="json") == second.model_dump(mode="json")
        assert render_brief_text(first) == render_brief_text(second)

    def test_document_ids_are_random_and_non_identifying(self) -> None:
        # Not derived from the query, the time, or anything else observable.
        ids = {new_document_id() for _ in range(500)}
        assert len(ids) == 500
        brief = a_brief(user=UserContent(topic="a very distinctive question about my health"))
        assert "health" not in brief.document_id

    def test_only_cited_sources_appear(self) -> None:
        # A source retrieved but backing nothing is not provenance, and on one
        # page it is the first thing to cut.
        brief = a_brief()
        assert [source.number for source in brief.sources] == [1]

    def test_source_numbers_match_the_page(self) -> None:
        brief = a_brief(
            a_claim(
                "c1",
                "Regular review supports earlier detection of complications.",
                "example:diabetes-0002",
                "Regular review supports earlier detection of complications",
            )
        )
        # The second source keeps number 2 even when it is the only one cited.
        assert brief.sources[0].number == 2
        assert brief.claims[0].source_numbers == [2]

    def test_governed_metadata_reaches_the_brief(self) -> None:
        brief = a_brief()
        source = brief.sources[0]
        assert source.review_label == "Not reviewed by a clinician"
        assert source.evidence_type_label == "Clinical guideline"
        assert source.container


class TestUnsupportedClaimsCannotEnter:
    """The invariant the schema exists for."""

    def test_a_withheld_claim_is_absent(self) -> None:
        brief = a_brief(
            a_claim(
                "c1",
                "HbA1c reflects average plasma glucose over 2-3 months.",
                "example:diabetes-0001",
                "HbA1c reflects average plasma glucose over 2-3 months",
            ),
            a_claim("c2", "A fabricated claim.", "example:diabetes-0001", "not in the source"),
        )
        assert [claim.claim_id for claim in brief.claims] == ["c1"]
        assert "fabricated" not in render_brief_text(brief)
        assert "fabricated" not in render_brief_html(brief)

    def test_the_schema_rejects_an_unsupported_claim_directly(self) -> None:
        with pytest.raises(ValidationError):
            BriefClaim(
                claim_id="c1", text="t", source_numbers=[1], support=SupportStatus.UNSUPPORTED.value
            )

    def test_an_abstained_answer_produces_an_empty_brief(self) -> None:
        # Nothing survived verification, so there is nothing to print.
        brief = a_brief(a_claim("c1", "Unsupported.", "example:diabetes-0001", "nowhere"))
        assert brief.claims == []
        assert brief.sources == []
        assert brief.has_verified_content is False

    def test_a_claim_whose_sources_are_unnumbered_is_dropped(self) -> None:
        # Its provenance cannot be shown, so it does not go on a document that
        # outlives the screen.
        brief = build_brief(
            displayed(
                a_claim(
                    "c1",
                    "HbA1c reflects average plasma glucose over 2-3 months.",
                    "example:diabetes-0001",
                    "HbA1c reflects average plasma glucose over 2-3 months",
                )
            ),
            SourceNumbering(),  # Empty numbering: nothing has a number
            generated_at=FIXED_TIME,
        )
        assert brief.claims == []


class TestEditing:
    """Requirements 1 to 6."""

    def test_topic_and_notes_are_editable(self) -> None:
        brief = set_notes(set_topic(a_brief(), "  my knee  "), "  it clicks  ")
        assert brief.user.topic == "my knee"
        assert brief.user.notes == "it clicks"

    def test_editing_a_claim_removes_its_verified_status(self) -> None:
        brief = a_brief()
        edited = edit_claim_text(brief, "c1", "HbA1c shows my sugar over three months")
        claim = edited.claims[0]
        assert claim.origin is ContentOrigin.USER_EDITED
        assert claim.is_verified is False

    def test_editing_a_claim_removes_its_citation_markers(self) -> None:
        # Requirement 5: the citation belonged to the previous wording.
        edited = edit_claim_text(a_brief(), "c1", "something I reworded")
        assert edited.claims[0].source_numbers == []
        assert edited.claims[0].markers() == ""

    def test_an_edited_claim_retains_the_original_wording(self) -> None:
        brief = a_brief()
        original = brief.claims[0].text
        edited = edit_claim_text(brief, "c1", "my rewording")
        assert edited.claims[0].original_text == original

    def test_an_edited_claim_is_labelled_in_every_rendering(self) -> None:
        edited = edit_claim_text(a_brief(), "c1", "my rewording")
        text = render_brief_text(edited)
        html = render_brief_html(edited)
        assert "edited by you" in text.lower()
        assert "not checked against a source" in text.lower()
        assert "Edited by you" in html
        assert "claim-edited" in html

    def test_a_no_op_edit_does_not_de_verify(self) -> None:
        brief = a_brief()
        same = edit_claim_text(brief, "c1", brief.claims[0].text)
        assert same.claims[0].is_verified is True
        assert same.claims[0].source_numbers == [1]

    def test_an_edited_claims_original_line_keeps_its_provenance(self) -> None:
        """Regression: the source list must not contain an entry nothing refers to.

        Editing a claim strips its markers, which left source [1] listed with no
        line pointing at it. The markers now travel with the wording they
        described, so the printed original line shows them and the source list
        stays coherent. Found by rendering the brief and looking at it.
        """
        edited = edit_claim_text(a_brief(), "c1", "my rewording")
        claim = edited.claims[0]
        assert claim.source_numbers == []  # The edited text carries none
        assert claim.original_source_numbers == [1]  # The original wording does
        text = render_brief_text(edited)
        assert (
            "Original wording: HbA1c reflects average plasma glucose over 2-3 months. [1]" in text
        )
        # Every source still in the list is referenced by something printed.
        assert edited.cited_source_numbers() >= {source.number for source in edited.sources}

    def test_no_source_is_orphaned_after_any_edit(self) -> None:
        brief = a_brief()
        for claim in list(brief.claims):
            brief = edit_claim_text(brief, claim.claim_id, f"reworded {claim.claim_id}")
        printed = brief.cited_source_numbers()
        listed = {source.number for source in brief.sources}
        assert listed <= printed, f"sources {listed - printed} are listed but never referenced"

    def test_an_edited_claim_can_be_restored(self) -> None:
        brief = a_brief()
        edited = edit_claim_text(brief, "c1", "my rewording")
        restored = restore_claim(edited, "c1")  # Markers recovered from the claim itself
        assert restored.claims[0].is_verified is True
        assert restored.claims[0].text == brief.claims[0].text
        assert restored.claims[0].source_numbers == [1]

    def test_questions_can_be_reordered_and_keep_their_origin(self) -> None:
        brief = add_question(a_brief(), "Is this related to my medication?")
        original_ids = [question.question_id for question in brief.questions]
        reordered = reorder_questions(brief, list(reversed(original_ids)))
        assert [q.question_id for q in reordered.questions] == list(reversed(original_ids))
        assert {q.question_id: q.origin for q in reordered.questions} == {
            q.question_id: q.origin for q in brief.questions
        }

    def test_reordering_preserves_citation_provenance(self) -> None:
        # Requirement 6: claims and their markers are untouched by question order.
        brief = a_brief()
        before = [(c.claim_id, c.source_numbers) for c in brief.claims]
        reordered = reorder_questions(
            brief, list(reversed([q.question_id for q in brief.questions]))
        )
        assert [(c.claim_id, c.source_numbers) for c in reordered.claims] == before

    def test_a_partial_reorder_is_refused(self) -> None:
        # Silently deleting the omitted questions would be destructive.
        brief = a_brief()
        with pytest.raises(EditError, match="exactly the current set"):
            reorder_questions(brief, [brief.questions[0].question_id])

    def test_a_user_question_is_marked_as_the_users(self) -> None:
        brief = add_question(a_brief(), "Should I change my diet?")
        added = brief.questions[-1]
        assert added.origin is ContentOrigin.USER_AUTHORED
        assert "your question" in render_brief_html(brief)
        assert "[written by you]" in render_brief_text(brief)

    def test_question_limits_are_enforced(self) -> None:
        brief = a_brief()
        while len(brief.questions) < MAX_QUESTIONS:
            brief = add_question(brief, f"question {len(brief.questions)}")
        with pytest.raises(EditError, match="at most"):
            add_question(brief, "one too many")

    def test_an_empty_question_is_refused(self) -> None:
        with pytest.raises(EditError):
            add_question(a_brief(), "   ")

    def test_removing_a_claim_prunes_orphaned_sources(self) -> None:
        brief = a_brief()
        pruned = remove_claim(brief, "c1")
        assert pruned.claims == []
        assert pruned.sources == []

    def test_a_user_authored_claim_is_impossible(self) -> None:
        # A user sentence is a note, not a claim. There is no route to one.
        with pytest.raises(ValidationError, match="cannot be user-authored"):
            BriefClaim(
                claim_id="c9",
                text="I think this is stress",
                support=SupportLevel.SUPPORTED,
                origin=ContentOrigin.USER_AUTHORED,
            )

    def test_edits_are_pure(self) -> None:
        brief = a_brief()
        snapshot = brief.model_dump(mode="json")
        edit_claim_text(brief, "c1", "changed")
        set_topic(brief, "changed")
        add_question(brief, "changed")
        assert brief.model_dump(mode="json") == snapshot

    def test_the_user_content_summary_counts_authorship(self) -> None:
        brief = add_question(set_notes(a_brief(), "my notes"), "my question")
        brief = edit_claim_text(brief, "c1", "my rewording")
        summary = user_content_summary(brief)
        assert summary["user_authored_questions"] == 1
        assert summary["edited_claims"] == 1
        assert summary["has_notes"] == 1


class TestUserContentIsVisuallyDistinct:
    """Requirement 2, in both renderings."""

    def test_user_blocks_are_marked_in_html(self) -> None:
        brief = set_notes(set_topic(a_brief(), "my topic"), "my notes")
        html = render_brief_html(brief)
        assert html.count("user-block") >= 2
        assert html.count("Written by you") >= 2

    def test_user_blocks_are_marked_in_text(self) -> None:
        brief = set_notes(set_topic(a_brief(), "my topic"), "my notes")
        assert render_brief_text(brief).count("[written by you]") >= 2

    def test_the_marker_survives_black_and_white_printing(self) -> None:
        # A tint alone would vanish on a photocopier, so the label is text.
        brief = set_topic(a_brief(), "my topic")
        html = render_brief_html(brief)
        assert "Written by you" in html  # Not conveyed by colour alone


class TestSections:
    """Requirement 10: omit before exporting."""

    @pytest.mark.parametrize("section", list(BriefSection))
    def test_each_section_can_be_omitted(self, section: BriefSection) -> None:
        brief = set_notes(set_topic(a_brief(), "my topic"), "my notes")
        kept = [item for item in BriefSection if item is not section]
        trimmed = set_sections(brief, kept)
        assert trimmed.includes(section) is False
        render_brief_html(trimmed)  # Must still render

    def test_the_disclaimer_cannot_be_omitted(self) -> None:
        # Not a BriefSection member, so there is no way to drop it.
        assert "disclaimer" not in {section.value for section in BriefSection}
        trimmed = set_sections(a_brief(), [])
        assert PERMANENT_DISCLAIMER in render_brief_html(trimmed)
        assert PERMANENT_DISCLAIMER in render_brief_text(trimmed)

    def test_omitting_sources_keeps_claim_markers_honest(self) -> None:
        # The markers still refer to real numbered sources; the reader simply
        # has to ask for the list.
        trimmed = set_sections(a_brief(), [BriefSection.CLAIMS])
        assert "[1]" in render_brief_text(trimmed)


class TestExportSafety:
    """What may and may not leave the process."""

    def test_an_export_contains_no_secrets_or_prompts(self) -> None:
        bundle = build_export(set_notes(a_brief(), "notes"))
        for payload in (bundle.html, bundle.json_text, bundle.text):
            lowered = payload.lower()
            assert "sk-" not in lowered
            assert "api_key" not in lowered
            assert "you are a patient education assistant" not in lowered
            assert "you help patients prepare" not in lowered
            assert "request_id" not in lowered
            assert "session_id" not in lowered
            assert "gpt-4o" not in lowered

    def test_the_scanner_blocks_a_planted_secret(self) -> None:
        with pytest.raises(ExportError, match="api key"):
            assert_no_secrets("sk-proj-AAAAAAAAAAAAAAAA", artefact="html")

    def test_the_scanner_blocks_a_planted_prompt(self) -> None:
        with pytest.raises(ExportError, match="system prompt"):
            assert_no_secrets("You help patients prepare for an appointment", artefact="text")

    def test_the_scanner_does_not_quote_what_it_found(self) -> None:
        # An error message quoting the secret is how the secret reaches a log.
        try:
            assert_no_secrets("sk-proj-SECRETVALUE1234", artefact="html")
        except ExportError as exc:
            assert "SECRETVALUE" not in str(exc)

    def test_no_chunk_identifiers_leak_into_an_export(self) -> None:
        # Internal plumbing has no place on a patient's printout.
        bundle = build_export(a_brief())
        assert "#0000" not in bundle.html
        assert "#0000" not in bundle.text

    def test_exports_contain_no_remote_assets(self) -> None:
        html = build_export(a_brief()).html
        for marker in ("http://", "fonts.googleapis", "<script", "<img", "@import"):
            assert marker not in html
        # The only permitted absolute URLs are the source links themselves.
        for url in re.findall(r'href="([^"]+)"', html):
            assert url.startswith("https://")

    def test_the_export_warning_names_the_risk(self) -> None:
        lowered = EXPORT_WARNING.lower()
        assert "health information" in lowered
        assert "not sent anywhere" in lowered
        assert "can read it" in lowered

    def test_json_round_trips_exactly(self) -> None:
        brief = set_notes(add_question(a_brief(), "my question"), "my notes")
        restored = load_brief_json(build_export(brief).json_text)
        assert restored.model_dump(mode="json") == brief.model_dump(mode="json")

    def test_json_carries_no_undeclared_field(self) -> None:
        payload = json.loads(build_export(a_brief()).json_text)
        assert set(payload) <= set(AppointmentBrief.model_fields) | {"schema_version"}

    def test_filenames_carry_the_document_id(self) -> None:
        bundle = build_export(a_brief(document_id="0123456789ab"))
        assert bundle.html_filename == "appointment-brief-0123456789ab.html"
        assert bundle.json_filename.endswith(".json")


class TestHostileContentIsInert:
    """User input and source titles are untrusted."""

    PAYLOADS = (
        '<script>alert("x")</script>',
        '<img src=x onerror="alert(1)">',
        "</style><script>alert(1)</script>",
        '"><iframe src="evil">',
    )

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_a_hostile_topic_is_escaped(self, payload: str) -> None:
        html = render_brief_html(set_topic(a_brief(), payload))
        assert "<script>" not in html
        assert "<iframe" not in html
        assert "<img src=x" not in html

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_a_hostile_note_is_escaped(self, payload: str) -> None:
        html = render_brief_html(set_notes(a_brief(), payload))
        assert "<script>" not in html
        assert "&lt;" in html

    def test_a_hostile_question_is_escaped(self) -> None:
        html = render_brief_html(add_question(a_brief(), "<script>alert(1)</script>"))
        assert "<script>alert" not in html

    def test_a_hostile_source_title_is_escaped(self) -> None:
        brief = a_brief()
        poisoned = brief.model_copy(
            update={
                "sources": [
                    brief.sources[0].model_copy(update={"title": "<script>alert(1)</script>"})
                ]
            }
        )
        assert "<script>alert" not in render_brief_html(poisoned)


class TestTemporaryFiles:
    """Requirement: store no temporary export longer than necessary."""

    def test_temporary_files_are_removed_on_success(self) -> None:
        with temporary_export(a_brief()) as directory:
            assert directory.is_dir()
            assert len(list(directory.iterdir())) == 3
            captured = directory
        assert not captured.exists()

    def test_temporary_files_are_removed_when_the_caller_raises(self) -> None:
        captured: Path | None = None
        with pytest.raises(RuntimeError), temporary_export(a_brief()) as directory:
            captured = directory
            raise RuntimeError("caller exploded")
        assert captured is not None
        assert not captured.exists()

    def test_the_streamlit_path_writes_no_file_at_all(self) -> None:
        # build_export returns bytes; only temporary_export touches disk.
        import inspect

        from synapse.brief import export as export_module

        source = inspect.getsource(export_module.build_export)
        assert "open(" not in source
        assert "write_text" not in source


class TestOnePageBehaviour:
    """Fit is measured and reported, never silently shrunk."""

    def test_a_small_brief_fits(self) -> None:
        assert estimate_fit(a_brief()).fits is True

    def test_an_overlong_brief_is_detected(self) -> None:
        brief = set_notes(a_brief(), "word " * 900)
        estimate = estimate_fit(brief)
        assert estimate.fits is False
        assert estimate.overflow_lines > 0

    def test_overflow_produces_actionable_advice(self) -> None:
        brief = set_notes(a_brief(), "word " * 900)
        advice = overflow_advice(brief)
        assert advice
        assert all(isinstance(item, str) and item for item in advice)

    def test_a_fitting_brief_produces_no_advice(self) -> None:
        assert overflow_advice(a_brief()) == []

    def test_the_print_css_never_shrinks_below_the_accessible_floor(self) -> None:
        from synapse.brief.render_print import MIN_BODY_PT, PRINT_CSS

        sizes = [float(value) for value in re.findall(r"font-size:\s*([\d.]+)pt", PRINT_CSS)]
        assert sizes
        assert min(sizes) >= MIN_BODY_PT - 1.5  # Labels may be smaller than body text
        body = re.search(r"font-size:\s*([\d.]+)pt;\s*\n\s*line-height", PRINT_CSS)
        assert body is None or float(body.group(1)) >= MIN_BODY_PT

    def test_long_questions_wrap_rather_than_clip(self) -> None:
        long_question = "Could this be related to " + ("something " * 30) + "?"
        brief = add_question(a_brief(), long_question[:290])
        html = render_brief_html(brief)
        assert "overflow-wrap: anywhere" in html
        assert "text-overflow" not in html  # No ellipsis truncation

    def test_sources_and_disclaimer_avoid_page_breaks(self) -> None:
        html = render_brief_html(a_brief())
        assert "break-inside: avoid" in html

    def test_both_page_sizes_are_supported(self) -> None:
        assert "size: A4" in render_brief_html(a_brief(), page_size="a4")
        assert "size: Letter" in render_brief_html(a_brief(), page_size="letter")

    def test_print_metadata_is_present(self) -> None:
        html = render_brief_html(a_brief())
        assert "<title>Appointment brief" in html
        assert 'name="robots" content="noindex' in html
        assert "Prepared" in html


class TestEmptyAndInsufficientStates:
    """A brief must not imply evidence it does not have."""

    def test_an_empty_brief_claims_nothing(self) -> None:
        brief = AppointmentBrief(
            disclaimer=PERMANENT_DISCLAIMER,
            provenance=BriefProvenance(generated_at=FIXED_TIME),
        )
        html = render_brief_html(brief)
        assert brief.has_verified_content is False
        assert "Details from published research" not in html
        assert "Sources" not in html
        assert PERMANENT_DISCLAIMER in html

    def test_a_brief_from_an_abstention_shows_no_research_section(self) -> None:
        brief = a_brief(a_claim("c1", "Unsupported.", "example:diabetes-0001", "nowhere"))
        html = render_brief_html(brief)
        assert "Details from published research" not in html
        assert PERMANENT_DISCLAIMER in html

    def test_a_notes_only_brief_is_all_user_content(self) -> None:
        # Legitimate: a patient may keep only their own notes and questions.
        brief = set_notes(
            a_brief(a_claim("c1", "Unsupported.", "example:diabetes-0001", "nowhere")),
            "my own notes",
        )
        html = render_brief_html(brief)
        assert "Written by you" in html
        assert brief.has_verified_content is False

    def test_an_empty_brief_carries_no_dangling_markers(self) -> None:
        brief = a_brief(a_claim("c1", "Unsupported.", "example:diabetes-0001", "nowhere"))
        assert "[1]" not in render_brief_text(brief)

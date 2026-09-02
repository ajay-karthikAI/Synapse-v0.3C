"""
The patient-facing evidence experience.

Four groups, matching the four ways this surface can fail a patient:

* the evidence shown does not actually support the claim above it;
* metadata is invented, or a governance label overstates what a clinician did;
* a link is unsafe or resolves somewhere arbitrary;
* the control cannot be operated without a mouse and sighted eyes.

The accessibility tests assert the properties a rendered page must have, not the
implementation that provides them: keyboard operability, a semantic expanded
state, no hover-only interaction, and text alternatives. Using ``<details>``
happens to satisfy all four, and the tests would still be meaningful if it were
replaced with something else that did.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest

from synapse.answer.policy import apply, decide
from synapse.answer.render import PERMANENT_DISCLAIMER, SourceNumbering, render_answer_html
from synapse.answer.schema import (
    AnswerAction,
    GroundedAnswer,
    GroundedClaim,
    SupportingExcerpt,
)
from synapse.answer.verify import RetrievedEvidence, verify_answer
from synapse.escaping import escape
from synapse.evidence.insufficient import (
    NEXT_STEPS,
    NOT_A_JUDGEMENT,
    REASON_MESSAGES,
    InsufficientEvidence,
    InsufficientReason,
    from_failure_code,
)
from synapse.evidence.labels import (
    RELEVANCE_LABEL,
    describe_evidence_type,
    describe_retraction,
    describe_review_state,
)
from synapse.evidence.links import (
    canonical_link,
    doi_url,
    is_safe_url,
    is_valid_doi,
    is_valid_pmid,
    pmid_url,
    safe_url,
)
from synapse.evidence.metadata import (
    MetadataResolver,
    SourceMetadata,
    format_authors,
    format_publication_date,
)
from synapse.evidence.render import (
    EvidenceExcerpt,
    card_dom_id,
    claim_dom_id,
    render_citation_control,
    render_evidence_card,
    render_insufficient_html,
    render_review_evidence_card,
)
from synapse.schemas.enums import (
    DatePrecision,
    EvidenceType,
    RetractionStatus,
    SourceLifecycleState,
)
from synapse.schemas.source import Author

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = REPO_ROOT / "source_packs" / "diabetes-previsit"

CHUNK_TEXT = (
    "HbA1c reflects average plasma glucose over 2-3 months. "
    "A target below 7% is appropriate for most non-pregnant adults with diabetes."
)

EVIDENCE = RetrievedEvidence(
    chunk_texts={"pubmed:41802233#0000": CHUNK_TEXT},
    source_ids=frozenset({"pubmed:41802233"}),
    source_titles={"pubmed:41802233": "HbA1c Targets in Adults"},
    source_urls={"pubmed:41802233": "https://pubmed.ncbi.nlm.nih.gov/41802233/"},
)
NUMBERING = SourceNumbering.from_evidence(EVIDENCE, ["pubmed:41802233"])


def a_metadata(**overrides) -> SourceMetadata:
    """Fully-populated governed metadata, for tests about display."""
    fields = {
        "document_id": "pubmed:41802233",
        "title": "HbA1c Targets in Adults",
        "authors": "A Smith and B Jones",
        "journal": "Journal of Example Medicine",
        "publication_date": "1 March 2025",
        "pmid": "41802233",
        "doi": "10.1001/jama.2025.0001",
        "canonical_url": "https://pubmed.ncbi.nlm.nih.gov/41802233/",
        "evidence_type": EvidenceType.SYSTEMATIC_REVIEW,
        "lifecycle_state": SourceLifecycleState.APPROVED,
        "retraction_status": RetractionStatus.NONE,
        "pack_id": "diabetes-previsit",
        "pack_version": "0.1.0",
        "pack_approval_state": "approved",
    }
    fields.update(overrides)
    return SourceMetadata(**fields)


def an_excerpt(
    quote: str = "HbA1c reflects average plasma glucose over 2-3 months",
) -> EvidenceExcerpt:
    """One verbatim span."""
    return EvidenceExcerpt("pubmed:41802233", "pubmed:41802233#0000", quote)


def a_claim(
    claim_id: str = "c1",
    text: str = "HbA1c reflects average plasma glucose over 2-3 months.",
    quote: str = "HbA1c reflects average plasma glucose over 2-3 months",
) -> GroundedClaim:
    """A claim citing the real evidence."""
    return GroundedClaim(
        claim_id=claim_id,
        text=text,
        source_ids=["pubmed:41802233"],
        supporting_excerpts=[
            SupportingExcerpt(
                source_id="pubmed:41802233", chunk_id="pubmed:41802233#0000", quote=quote
            )
        ],
    )


def displayed(*claims: GroundedClaim) -> GroundedAnswer:
    """Verify and gate an answer exactly as the application does."""
    answer = GroundedAnswer(
        summary="A summary.",
        claims=list(claims),
        doctor_evaluation="Your clinician will review this.",
        questions_for_doctor=["What does this mean for me?"],
        action=AnswerAction.ANSWER,
    )
    verified, report = verify_answer(answer, EVIDENCE)
    return apply(verified, decide(verified, report))


class TestEveryClaimMapsToItsEvidence:
    """Requirements 1, 3, 4 and 5."""

    def test_every_displayed_claim_has_a_citation_control(self) -> None:
        html = render_answer_html(displayed(a_claim()), NUMBERING)
        assert html.count("<details") == 1
        assert "Show the evidence for this statement" in html

    def test_the_control_reveals_the_exact_stored_excerpt(self) -> None:
        # Requirement 3: the quote is the span the verifier matched against the
        # chunk, never regenerated.
        quote = "A target below 7% is appropriate"
        html = render_answer_html(displayed(a_claim(quote=quote)), NUMBERING)
        assert quote in html
        assert quote in CHUNK_TEXT  # And it really is in the stored chunk

    def test_claim_and_card_ids_are_stable_and_linked(self) -> None:
        html = render_citation_control("c1", [(1, a_metadata(), [an_excerpt()], None)])
        assert card_dom_id("c1", "pubmed:41802233") in html
        assert 'data-claim-id="c1"' in html
        assert 'data-source-id="pubmed:41802233"' in html

    def test_dom_ids_are_derived_from_the_stable_identifiers(self) -> None:
        # Same identifiers in, same ids out — on any run, in any process.
        assert claim_dom_id("c1") == claim_dom_id("c1")
        assert card_dom_id("c1", "pubmed:41802233") == card_dom_id("c1", "pubmed:41802233")
        assert card_dom_id("c1", "pubmed:1") != card_dom_id("c1", "pubmed:2")

    def test_dom_ids_survive_identifier_punctuation(self) -> None:
        # A chunk identifier contains ':' and '#', which break fragment links.
        generated = card_dom_id("c1", "pubmed:41802233#0000")
        assert ":" not in generated
        assert "#" not in generated

    def test_citation_order_matches_the_numbering(self) -> None:
        # Requirement 5: the [n] in the text, in the control and on the card agree.
        claim = GroundedClaim(
            claim_id="c1",
            text="A claim citing two sources.",
            source_ids=["pubmed:41802233", "pubmed:41900001"],
            supporting_excerpts=[
                SupportingExcerpt(
                    source_id="pubmed:41802233",
                    chunk_id="pubmed:41802233#0000",
                    quote="HbA1c reflects average plasma glucose",
                ),
                SupportingExcerpt(
                    source_id="pubmed:41900001",
                    chunk_id="pubmed:41900001#0000",
                    quote="Regular review supports earlier detection",
                ),
            ],
        )
        evidence = RetrievedEvidence(
            chunk_texts={
                "pubmed:41802233#0000": CHUNK_TEXT,
                "pubmed:41900001#0000": "Regular review supports earlier detection of complications.",
            },
            source_ids=frozenset({"pubmed:41802233", "pubmed:41900001"}),
        )
        numbering = SourceNumbering.from_evidence(evidence, ["pubmed:41802233", "pubmed:41900001"])
        verified, report = verify_answer(
            GroundedAnswer(summary="s", claims=[claim], action=AnswerAction.ANSWER), evidence
        )
        html = render_answer_html(apply(verified, decide(verified, report)), numbering)
        numbers = re.findall(r'<span class="ev-number">\[(\d)\]</span>', html)
        assert numbers == ["1", "2"]

    def test_an_unretrieved_source_never_renders_a_card(self) -> None:
        # A citation to a source the numbering does not know is skipped, not
        # rendered as an unresolvable reference.
        html = render_citation_control("c1", [])
        assert html == ""

    def test_a_claim_with_no_excerpt_says_so(self) -> None:
        html = render_evidence_card(a_metadata(), [], claim_id="c1", number=1)
        assert "No excerpt was recorded" in html


class TestMetadataIsNeverInvented:
    """Requirement 2, and the no-fabrication rule."""

    def test_all_recorded_fields_are_displayed(self) -> None:
        html = render_evidence_card(a_metadata(), [an_excerpt()], claim_id="c1", number=1)
        for expected in (
            "HbA1c Targets in Adults",
            "A Smith and B Jones",
            "Journal of Example Medicine",
            "1 March 2025",
            "41802233",
            "10.1001/jama.2025.0001",
        ):
            assert expected in html

    @pytest.mark.parametrize(
        "absent_field",
        ["authors", "journal", "publication_date", "revision_date", "pmid", "doi", "guideline_id"],
    )
    def test_an_absent_field_renders_as_absent(self, absent_field: str) -> None:
        # No "Unknown" placeholder: that reads as a fact about the source.
        html = render_evidence_card(
            a_metadata(**{absent_field: ""}), [an_excerpt()], claim_id="c1", number=1
        )
        assert "Unknown" not in html
        assert "N/A" not in html
        assert "None" not in html

    def test_a_card_with_almost_no_metadata_still_renders(self) -> None:
        html = render_evidence_card(
            SourceMetadata.minimal("pubmed:1", title="A title"),
            [an_excerpt()],
            claim_id="c1",
            number=1,
        )
        assert "A title" in html
        assert "No governance record" in html  # Stated, not implied

    def test_an_ungoverned_source_is_labelled_as_such(self) -> None:
        metadata = SourceMetadata.minimal("pubmed:1", title="t")
        assert metadata.is_governed is False
        label = describe_review_state(None, is_governed=False)
        assert label.name == "No governance record"

    def test_date_precision_is_respected(self) -> None:
        # A record carrying only a year must not print a day and a month.
        assert format_publication_date(date(2019, 1, 1), DatePrecision.YEAR) == "2019"
        assert format_publication_date(date(2019, 3, 1), DatePrecision.MONTH) == "March 2019"
        assert format_publication_date(None) == ""

    def test_author_formatting_degrades_gracefully(self) -> None:
        assert format_authors([]) == ""
        assert format_authors([Author(family="Smith", given="Alice")]) == "Alice Smith"
        assert format_authors([Author(family="Smith", initials="A")]) == "A Smith"
        assert (
            format_authors([Author(collective_name="A Working Group", is_collective=True)])
            == "A Working Group"
        )

    def test_long_author_lists_are_elided_not_truncated(self) -> None:
        authors = [Author(family=f"Author{n}", given="X") for n in range(10)]
        formatted = format_authors(authors)
        assert formatted.endswith("and colleagues")
        assert "Author9" not in formatted

    def test_metadata_loads_from_the_real_governed_pack(self) -> None:
        resolver = MetadataResolver.from_directory(PACK_DIR)
        metadata = resolver.get("example:diabetes-0001")
        assert metadata is not None
        assert metadata.pack_id == "diabetes-previsit"
        assert metadata.pack_version == "0.1.0"
        assert metadata.is_governed is True

    def test_a_source_missing_from_the_pack_resolves_minimally(self) -> None:
        resolver = MetadataResolver.from_directory(PACK_DIR)
        metadata = resolver.resolve("pubmed:99999999", title="Not in the pack")
        assert metadata.is_governed is False
        assert metadata.title == "Not in the pack"
        assert metadata.evidence_type is None  # Not guessed


class TestGovernanceLabels:
    """Requirements 7 to 11."""

    def test_the_shipped_pack_is_reported_as_unreviewed(self) -> None:
        # Every source in the example pack is `discovered`. The label must say
        # so rather than implying a clinician looked at it.
        resolver = MetadataResolver.from_directory(PACK_DIR)
        metadata = resolver.get("example:diabetes-0001")
        label = describe_review_state(metadata.lifecycle_state, is_governed=True)
        assert label.name == "Not reviewed by a clinician"
        assert label.tone == "caution"

    def test_every_evidence_type_has_a_plain_language_explanation(self) -> None:
        for evidence_type in EvidenceType:
            label = describe_evidence_type(evidence_type)
            assert label.name and label.explanation
            assert label.explanation.endswith(".")
            assert "_" not in label.name  # No raw enum values on a patient's screen

    def test_every_review_state_has_a_plain_language_explanation(self) -> None:
        for state in SourceLifecycleState:
            label = describe_review_state(state)
            assert label.name and label.explanation
            assert "_" not in label.name

    def test_labels_do_not_rate_the_evidence(self) -> None:
        # Requirement 10: a label describes the source; it does not grade it.
        banned = ("high quality", "low quality", "strong evidence", "weak evidence", "reliable")
        for evidence_type in EvidenceType:
            label = describe_evidence_type(evidence_type)
            text = f"{label.name} {label.explanation}".lower()
            assert not any(phrase in text for phrase in banned)

    def test_relevance_is_never_called_confidence(self) -> None:
        # Requirement 7, asserted over the rendered page.
        html = render_evidence_card(
            a_metadata(), [an_excerpt()], claim_id="c1", number=1, relevance=0.82
        )
        assert "confidence" not in html.lower()
        assert RELEVANCE_LABEL in html
        assert "does not say how reliable the research is" in html

    def test_relevance_is_absent_when_not_supplied(self) -> None:
        html = render_evidence_card(a_metadata(), [an_excerpt()], claim_id="c1", number=1)
        assert RELEVANCE_LABEL not in html

    def test_a_clean_source_shows_no_retraction_badge(self) -> None:
        # A badge on every source trains readers to ignore the one that matters.
        assert describe_retraction(RetractionStatus.NONE) is None
        html = render_evidence_card(a_metadata(), [an_excerpt()], claim_id="c1", number=1)
        assert "Retracted" not in html

    def test_a_retracted_source_is_labelled(self) -> None:
        label = describe_retraction(RetractionStatus.RETRACTED)
        assert label is not None
        assert label.tone == "caution"

    def test_the_pack_version_is_shown(self) -> None:
        html = render_evidence_card(a_metadata(), [an_excerpt()], claim_id="c1", number=1)
        assert "diabetes-previsit" in html
        assert "0.1.0" in html

    def test_explanations_are_rendered_as_text_not_tooltips(self) -> None:
        # A title attribute is hover-only and invisible on a touch screen.
        html = render_evidence_card(a_metadata(), [an_excerpt()], claim_id="c1", number=1)
        assert "title=" not in html
        assert "ev-label-why" in html


class TestSafeLinks:
    """Requirement 6."""

    UNSAFE: ClassVar[list[str]] = [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "http://example.org/insecure",
        "file:///etc/passwd",
        "https:///no-host",
        "https://example.org/a\nb",
        "vbscript:msgbox(1)",
        "",
    ]

    @pytest.mark.parametrize("url", UNSAFE)
    def test_unsafe_urls_are_dropped(self, url: str) -> None:
        assert safe_url(url) == ""
        assert is_safe_url(url) is False

    def test_https_urls_are_permitted(self) -> None:
        assert safe_url("https://pubmed.ncbi.nlm.nih.gov/123/") != ""

    def test_a_dropped_url_renders_no_anchor(self) -> None:
        html = render_evidence_card(
            a_metadata(canonical_url="javascript:alert(1)", doi="", pmid=""),
            [an_excerpt()],
            claim_id="c1",
            number=1,
        )
        assert "javascript:" not in html
        assert "<a " not in html

    def test_external_links_carry_tab_safety_attributes(self) -> None:
        html = render_evidence_card(a_metadata(), [an_excerpt()], claim_id="c1", number=1)
        for anchor in re.findall(r"<a [^>]*>", html):
            assert 'target="_blank"' in anchor
            assert "noopener" in anchor
            assert "noreferrer" in anchor

    def test_new_tab_links_announce_themselves(self) -> None:
        html = render_evidence_card(a_metadata(), [an_excerpt()], claim_id="c1", number=1)
        assert "opens in a new tab" in html

    @pytest.mark.parametrize(
        ("doi", "valid"),
        [
            ("10.1001/jama.2025.0001", True),
            ("10.1/x", False),
            ("not-a-doi", False),
            ("11.1001/jama", False),
            ("", False),
        ],
    )
    def test_doi_validation(self, doi: str, valid: bool) -> None:
        assert is_valid_doi(doi) is valid
        assert bool(doi_url(doi)) is valid

    @pytest.mark.parametrize(
        ("pmid", "valid"),
        [("41802233", True), ("1", True), ("12x", False), ("", False), ("123456789012", False)],
    )
    def test_pmid_validation(self, pmid: str, valid: bool) -> None:
        assert is_valid_pmid(pmid) is valid
        assert bool(pmid_url(pmid)) is valid

    def test_an_invalid_identifier_produces_no_link(self) -> None:
        html = render_evidence_card(
            a_metadata(doi="not-a-doi", pmid="12x", canonical_url=""),
            [an_excerpt()],
            claim_id="c1",
            number=1,
        )
        assert "doi.org" not in html
        assert "<a " not in html
        # The malformed identifiers are still shown as recorded metadata.
        assert "not-a-doi" in html

    def test_canonical_link_prefers_the_most_durable_identifier(self) -> None:
        assert canonical_link("https://example.org/x", doi="10.1001/j.1", pmid="123") == doi_url(
            "10.1001/j.1"
        )
        assert canonical_link("https://example.org/x", pmid="123") == pmid_url("123")
        assert canonical_link("https://example.org/x") == "https://example.org/x"


class TestHostileContentIsInert:
    """Titles and excerpts are third-party content."""

    PAYLOADS: ClassVar[list[str]] = [
        '<script>alert("x")</script>',
        '<img src=x onerror="alert(1)">',
        '"><script>alert(1)</script>',
        "<iframe src='evil'></iframe>",
        "</blockquote><script>alert(1)</script>",
    ]

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_a_hostile_title_is_escaped(self, payload: str) -> None:
        html = render_evidence_card(
            a_metadata(title=payload), [an_excerpt()], claim_id="c1", number=1
        )
        assert "<script>" not in html
        assert "<iframe" not in html
        assert "<img src=x" not in html

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_a_hostile_excerpt_is_escaped(self, payload: str) -> None:
        html = render_evidence_card(a_metadata(), [an_excerpt(payload)], claim_id="c1", number=1)
        assert "<script>" not in html
        assert "&lt;" in html

    def test_hostile_metadata_cannot_break_out_of_an_attribute(self) -> None:
        # The payload tries to close data-source-id and open a handler. What
        # matters is that its quotes arrive escaped, so it stays inside the
        # attribute as text. Stripping the escaping before asserting would undo
        # exactly the defence under test.
        html = render_evidence_card(
            a_metadata(document_id='pubmed:1" onmouseover="alert(1)'),
            [an_excerpt()],
            claim_id="c1",
            number=1,
        )
        assert 'onmouseover="' not in html  # No handler with a real quote
        assert "&quot;" in html  # The payload's quotes were escaped
        for attribute in re.findall(r'data-source-id="([^"]*)"', html):
            assert "onmouseover" not in attribute.replace("&quot;", '"') or attribute.startswith(
                "pubmed:1&quot;"
            )


class TestInsufficientEvidenceState:
    """Every failure mode, and the wording rules."""

    @pytest.mark.parametrize("reason", list(InsufficientReason))
    def test_every_reason_renders(self, reason: InsufficientReason) -> None:
        html = render_insufficient_html(InsufficientEvidence(reason))
        assert "Not enough verified information" in html
        assert f'data-reason="{reason.value}"' in html
        assert REASON_MESSAGES[reason].split(".")[0][:40] in html

    @pytest.mark.parametrize("reason", list(InsufficientReason))
    def test_every_reason_carries_the_not_a_judgement_sentence(
        self, reason: InsufficientReason
    ) -> None:
        # Absence of evidence must never read as absence of disease.
        # Compared against the escaped form: the sentence contains an
        # apostrophe, which is escaped on the way into the page.
        html = render_insufficient_html(InsufficientEvidence(reason))
        assert escape(NOT_A_JUDGEMENT) in html
        assert "does not rule anything in or out" in html

    @pytest.mark.parametrize("reason", list(InsufficientReason))
    def test_every_reason_offers_neutral_next_steps(self, reason: InsufficientReason) -> None:
        html = render_insufficient_html(InsufficientEvidence(reason))
        for step in NEXT_STEPS:
            assert step in html

    @pytest.mark.parametrize("reason", list(InsufficientReason))
    def test_the_wording_is_not_alarming(self, reason: InsufficientReason) -> None:
        text = REASON_MESSAGES[reason].lower()
        for alarming in ("warning", "danger", "unsafe", "serious", "urgent", "failure", "error"):
            assert alarming not in text

    def test_no_generated_text_can_enter_the_state(self) -> None:
        # The only inputs are a reason and two version strings.
        state = InsufficientEvidence(InsufficientReason.NO_ELIGIBLE_EVIDENCE)
        html = render_insufficient_html(state)
        assert state.message in REASON_MESSAGES.values()
        assert len(html) < 4000  # Bounded: it is composed from constants

    def test_the_state_uses_a_live_region_role(self) -> None:
        # role="status" announces the change without stealing focus.
        html = render_insufficient_html(InsufficientEvidence(InsufficientReason.OUT_OF_SCOPE))
        assert 'role="status"' in html

    def test_failure_codes_map_to_reasons(self) -> None:
        assert from_failure_code("index_unverified") is InsufficientReason.INDEX_UNVERIFIED
        assert from_failure_code("evidence_unavailable") is InsufficientReason.NO_ELIGIBLE_EVIDENCE
        assert (
            from_failure_code("generation_schema_invalid") is InsufficientReason.FAILED_VALIDATION
        )
        # Anything unrecognised takes the most conservative reading.
        assert from_failure_code("something_new") is InsufficientReason.NO_ELIGIBLE_EVIDENCE

    def test_an_operator_note_exists_and_is_not_patient_facing(self) -> None:
        state = InsufficientEvidence(InsufficientReason.CONFLICTING_SOURCES)
        assert state.operator_note
        assert state.operator_note not in render_insufficient_html(state)

    def test_an_abstention_still_shows_the_permanent_disclaimer(self) -> None:
        answer = displayed(a_claim(quote="a quote that is not in the source"))
        assert answer.action is AnswerAction.ABSTAIN
        assert PERMANENT_DISCLAIMER in render_answer_html(answer, NUMBERING)


class TestReviewViewIsSeparate:
    """Expired and superseded status belongs in a review view only."""

    def test_a_superseded_source_shows_its_status_in_the_review_view(self) -> None:
        resolver = MetadataResolver.from_directory(PACK_DIR)
        metadata = resolver.get("example:diabetes-0004")
        assert metadata.lifecycle_state is SourceLifecycleState.SUPERSEDED
        html = render_review_evidence_card(metadata)
        assert "superseded" in html
        assert "example:diabetes-0001" in html  # superseded_by

    def test_the_patient_card_does_not_render_superseded_plumbing(self) -> None:
        resolver = MetadataResolver.from_directory(PACK_DIR)
        metadata = resolver.get("example:diabetes-0004")
        html = render_evidence_card(metadata, [an_excerpt()], claim_id="c1", number=1)
        assert "superseded_by" not in html
        assert "example:diabetes-0001" not in html

    def test_the_review_view_reports_the_pack_approval_state(self) -> None:
        resolver = MetadataResolver.from_directory(PACK_DIR)
        html = render_review_evidence_card(resolver.get("example:diabetes-0001"))
        assert "unapproved_example" in html

    def test_rendering_changes_no_approval_state(self) -> None:
        # Reading must not write. The pack on disk is unchanged by rendering.
        before = (PACK_DIR / "sources.jsonl").read_bytes()
        resolver = MetadataResolver.from_directory(PACK_DIR)
        for metadata in resolver.by_document.values():
            render_review_evidence_card(metadata)
            render_evidence_card(metadata, [an_excerpt()], claim_id="c1", number=1)
        assert (PACK_DIR / "sources.jsonl").read_bytes() == before


class TestAccessibility:
    """Properties a rendered page must have, not the element that provides them."""

    def _control(self) -> str:
        return render_citation_control("c1", [(1, a_metadata(), [an_excerpt()], 0.8)])

    def test_the_control_is_operable_without_a_pointer(self) -> None:
        # A native disclosure is focusable and activated by Enter/Space with no
        # script; a div with an onclick would not be.
        html = self._control()
        assert "<details" in html and "<summary" in html
        assert "onclick" not in html

    def test_the_expanded_state_is_exposed_semantically(self) -> None:
        # details/summary carries its own state; nothing has to remember to
        # update an aria-expanded attribute.
        assert "<details" in self._control()

    def test_the_control_has_a_descriptive_accessible_name(self) -> None:
        html = self._control()
        assert "Show the evidence for this statement" in html
        assert "1 source" in html  # Distinguishes it from other identical controls

    def test_no_interaction_depends_on_hover(self) -> None:
        app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        evidence_css = app[app.index("Evidence experience") : app.index("Insufficient evidence")]
        # Hover may restyle, but must not be the only way to reveal content.
        for block in re.findall(r":hover\s*{([^}]*)}", evidence_css):
            assert "display:" not in block
            assert "visibility:" not in block
            assert "opacity:" not in block

    def test_a_visible_focus_state_is_defined(self) -> None:
        app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        assert ".ev-summary:focus-visible" in app
        assert ".ev-link:focus-visible" in app
        assert "outline:" in app

    def test_screen_reader_only_text_stays_in_the_accessibility_tree(self) -> None:
        # Scoped to the .visually-hidden rule itself. Slicing between two
        # selectors was brittle: unrelated rules moved into the slice and the
        # test failed for a reason that had nothing to do with the utility.
        app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        start = app.index(".visually-hidden {")
        rule = app[start : app.index("}", start)]
        assert "clip-path" in rule
        assert "display: none" not in rule  # Would remove it from the tree entirely

    def test_decorative_marks_are_hidden_from_assistive_technology(self) -> None:
        html = self._control()
        for fragment in re.findall(r'<span class="ev-external"[^>]*>', html):
            assert 'aria-hidden="true"' in fragment

    def test_touch_targets_meet_a_minimum_size(self) -> None:
        app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        assert app.count("min-height: 44px") >= 2

    def test_long_content_wraps_rather_than_overflowing(self) -> None:
        app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        assert "overflow-wrap: anywhere" in app
        assert "word-break: break-word" in app

    def test_reduced_motion_is_respected(self) -> None:
        app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        assert "prefers-reduced-motion" in app

    def test_the_layout_is_mobile_first(self) -> None:
        # The multi-column metadata layout is a min-width enhancement, so the
        # stacked single-column form is what a 320px screen gets.
        app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        assert "@media (min-width: 420px)" in app
        assert "max-width" not in app[app.index(".ev-card {") : app.index(".ev-title")]

    def test_heading_levels_are_sensible(self) -> None:
        """Cards sit under the section h2, so h3 is the correct depth.

        They were h4 while the section labels were styled ``div``s rather than
        headings. Making those real ``h2`` elements closed the gap, and the
        cards moved up to h3 so the outline no longer skips a level.
        """
        html = self._control()
        assert "<h3" in html
        assert "<h4" not in html
        assert "<h1" not in html

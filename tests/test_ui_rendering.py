"""
UI component tests: what the production interface actually renders.

Every test here asserts a property of the rendered output rather than of the
model that produced it. The distinction matters because the failure this
milestone closes was a *rendering* failure — the answer layer was correct, and
``app.py`` still displayed unvalidated prose recovered with regexes.

The four groups map to the four things that can go wrong on a patient's screen:

* structure recovered from prose rather than read from typed fields;
* an unsupported claim reaching the page;
* the disclaimer going missing when something else fails;
* markup from a user, a model or a PubMed abstract executing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import ClassVar

import pytest

from synapse.answer.brief import render_appointment_brief
from synapse.answer.policy import apply, decide
from synapse.answer.render import (
    ABSTENTION_LABEL,
    PERMANENT_DISCLAIMER,
    SourceNumbering,
    render_answer_html,
    render_evidence_html,
    render_failure_html,
    render_sources_html,
)
from synapse.answer.schema import (
    AnswerAction,
    GroundedAnswer,
    GroundedClaim,
    SupportingExcerpt,
    SupportStatus,
)
from synapse.answer.verify import RetrievedEvidence, verify_answer
from synapse.ui.errors import PATIENT_ERROR_MESSAGE, AnswerFailureCode

REPO_ROOT = Path(__file__).resolve().parent.parent

CHUNK_TEXT = (
    "HbA1c reflects average plasma glucose over 2-3 months. "
    "A target below 7% is appropriate for most non-pregnant adults with diabetes."
)
OTHER_CHUNK_TEXT = "Regular review supports earlier detection of complications."

EVIDENCE = RetrievedEvidence(
    chunk_texts={
        "pubmed:41802233#0000": CHUNK_TEXT,
        "pubmed:41900001#0000": OTHER_CHUNK_TEXT,
    },
    source_ids=frozenset({"pubmed:41802233", "pubmed:41900001"}),
    source_titles={
        "pubmed:41802233": "HbA1c Targets in Adults",
        "pubmed:41900001": "Routine Diabetes Review",
    },
    source_urls={
        "pubmed:41802233": "https://pubmed.ncbi.nlm.nih.gov/41802233/",
        "pubmed:41900001": "https://pubmed.ncbi.nlm.nih.gov/41900001/",
    },
)
SOURCE_ORDER = ["pubmed:41802233", "pubmed:41900001"]
NUMBERING = SourceNumbering.from_evidence(EVIDENCE, SOURCE_ORDER)


def a_claim(
    claim_id: str = "c1",
    text: str = "HbA1c reflects average plasma glucose over 2-3 months.",
    *,
    source_id: str = "pubmed:41802233",
    chunk_id: str = "pubmed:41802233#0000",
    quote: str = "HbA1c reflects average plasma glucose over 2-3 months",
) -> GroundedClaim:
    """A claim citing the real evidence, by default correctly."""
    return GroundedClaim(
        claim_id=claim_id,
        text=text,
        source_ids=[source_id],
        supporting_excerpts=[
            SupportingExcerpt(source_id=source_id, chunk_id=chunk_id, quote=quote)
        ],
    )


def an_answer(*claims: GroundedClaim, action: AnswerAction = AnswerAction.ANSWER) -> GroundedAnswer:
    """A complete answer wrapping the given claims."""
    return GroundedAnswer(
        summary="Your HbA1c shows average blood sugar over the past few months.",
        claims=list(claims),
        doctor_evaluation="Your clinician will read this alongside your history.",
        questions_for_doctor=["What does my result mean for me?"],
        limitations=["General information from published research."],
        disclaimer="model-supplied text the renderer ignores",
        action=action,
    )


def displayed(answer: GroundedAnswer) -> GroundedAnswer:
    """Verify and gate an answer exactly as the application does."""
    verified, report = verify_answer(answer, EVIDENCE)
    return apply(verified, decide(verified, report))


class TestTypedFieldsAreRenderedDirectly:
    """Requirement 1: every section comes from a typed field."""

    def test_all_typed_fields_reach_the_page(self) -> None:
        answer = displayed(an_answer(a_claim()))
        html = render_answer_html(answer, NUMBERING)
        assert answer.summary in html
        assert answer.claims[0].text in html
        assert answer.doctor_evaluation in html
        assert answer.questions_for_doctor[0] in html
        assert answer.limitations[0] in html
        assert PERMANENT_DISCLAIMER in html
        assert 'data-action="answer"' in html

    def test_supporting_excerpts_and_sources_are_rendered(self) -> None:
        answer = displayed(an_answer(a_claim()))
        evidence_html = render_evidence_html(answer, NUMBERING)
        assert "HbA1c reflects average plasma glucose over 2-3 months" in evidence_html
        assert "pubmed:41802233#0000" in evidence_html
        sources_html = render_sources_html(NUMBERING)
        assert "HbA1c Targets in Adults" in sources_html

    def test_a_reordered_answer_renders_identically(self) -> None:
        # The old renderer depended on heading order in the prose. Field order
        # in a validated object cannot change what is rendered.
        answer = displayed(an_answer(a_claim()))
        reordered = GroundedAnswer.model_validate(
            {
                "limitations": answer.limitations,
                "questions_for_doctor": answer.questions_for_doctor,
                "action": answer.action.value,
                "claims": [claim.model_dump() for claim in answer.claims],
                "doctor_evaluation": answer.doctor_evaluation,
                "summary": answer.summary,
            }
        )
        assert render_answer_html(reordered, NUMBERING) == render_answer_html(answer, NUMBERING)

    def test_a_missing_section_is_an_absent_card_not_a_broken_page(self) -> None:
        answer = displayed(
            GroundedAnswer(summary="A summary.", claims=[a_claim()], action=AnswerAction.ANSWER)
        )
        html = render_answer_html(answer, NUMBERING)
        assert "What your doctor will evaluate" not in html
        assert PERMANENT_DISCLAIMER in html  # Still complete where it matters


class TestNoRegexParsingRemains:
    """Requirement 2: the heading-parsing behaviour is gone from production."""

    APP_SOURCE = (REPO_ROOT / "app.py").read_text(encoding="utf-8")

    LEGACY_HEADINGS = (
        "WHAT THE RESEARCH SAYS",
        "WHAT YOUR DOCTOR WILL EVALUATE",
        "QUESTIONS TO ASK YOUR DOCTOR",
    )

    def test_app_does_not_import_re(self) -> None:
        """No regex module in the production entry point at all."""
        tree = ast.parse(self.APP_SOURCE)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported |= {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "re" not in imported

    @pytest.mark.parametrize("heading", LEGACY_HEADINGS)
    def test_no_legacy_heading_is_matched_against(self, heading: str) -> None:
        # The strings may appear in a comment explaining what was replaced; what
        # must not exist is code that looks for them.
        code_lines = [
            line
            for line in self.APP_SOURCE.splitlines()
            if heading in line and not line.lstrip().startswith("#")
        ]
        assert code_lines == []

    def test_app_does_not_import_the_removed_free_form_generator(self) -> None:
        assert "AnswerGenerator" not in self.APP_SOURCE

    def test_the_free_form_generator_is_gone_from_the_legacy_tree(self) -> None:
        # Two answer pipelines, one of them unverified, is the thing being
        # removed — not merely bypassed.
        source = (REPO_ROOT / "Generation" / "answer_generator.py").read_text(encoding="utf-8")
        defined = [
            node.name
            for node in ast.parse(source).body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        ]
        assert "AnswerGenerator" not in defined
        assert "run_pipeline" not in defined
        assert defined == ["check_emergency"]

    def test_the_app_renders_through_the_safe_renderer(self) -> None:
        for expected in ("render_answer_html", "render_failure_html", "render_sources_html"):
            assert expected in self.APP_SOURCE

    def test_no_unescaped_interpolation_of_dynamic_values(self) -> None:
        """Every f-string written into markup must run through escape()."""
        markup_fstrings = re.findall(r"f'<[^']*\{[^']*'", self.APP_SOURCE)
        unescaped = [fragment for fragment in markup_fstrings if "escape(" not in fragment]
        assert unescaped == []


class TestUnsupportedClaimsNeverRender:
    """Requirement 3, on every surface a claim can reach."""

    SENTINEL = "SENTINEL-UNSUPPORTED-MEDICAL-CLAIM"

    def _answer_with_one_fabrication(self) -> GroundedAnswer:
        return displayed(
            an_answer(
                a_claim("c1"),
                a_claim(
                    "c2",
                    "Regular review supports earlier detection of complications.",
                    source_id="pubmed:41900001",
                    chunk_id="pubmed:41900001#0000",
                    quote="Regular review supports earlier detection",
                ),
                a_claim("c3", self.SENTINEL, quote="a quote that is not in the source"),
            )
        )

    def test_withheld_claim_is_absent_from_the_answer_html(self) -> None:
        assert self.SENTINEL not in render_answer_html(
            self._answer_with_one_fabrication(), NUMBERING
        )

    def test_withheld_claim_is_absent_from_the_evidence_cards(self) -> None:
        # The evidence panel is the obvious place for a withheld claim to
        # survive, because it renders excerpts rather than claims.
        html = render_evidence_html(self._answer_with_one_fabrication(), NUMBERING)
        assert self.SENTINEL not in html
        assert 'data-claim-id="c3"' not in html

    def test_withheld_claim_is_absent_from_the_exported_brief(self) -> None:
        brief = render_appointment_brief(self._answer_with_one_fabrication(), NUMBERING)
        assert self.SENTINEL not in brief
        # Word-boundary. The brief carries a random secrets.token_hex document
        # id, and a bare "c3" substring matches inside a hex digest roughly 4%
        # of the time -- "cument 6acc3d1c64c4" failed this assertion for no
        # reason at all. Hex characters are word characters, so \b cannot match
        # inside a digest. The sibling assertion above uses the equally precise
        # 'data-claim-id="c3"'; this brings the plain-text form up to it.
        assert not re.search(r"\bc3\b", brief)

    def test_an_unverified_answer_displays_no_claims_at_all(self) -> None:
        # Statuses default to UNSUPPORTED, so an answer that never reached the
        # verifier fails closed rather than open.
        answer = an_answer(a_claim())
        assert all(claim.support_status is SupportStatus.UNSUPPORTED for claim in answer.claims)

        from synapse.answer.verify import VerificationReport

        shown = apply(answer, decide(answer, VerificationReport()))
        assert shown.claims == []
        assert "HbA1c" not in render_answer_html(shown, NUMBERING)
        assert render_evidence_html(shown, NUMBERING).startswith('<div class="src-empty"')


class TestEveryVisibleClaimIsBackedByEvidence:
    """The positive form of requirement 3, asserted over the rendered output."""

    def test_each_rendered_claim_has_a_verified_excerpt(self) -> None:
        answer = displayed(
            an_answer(
                a_claim("c1"),
                a_claim(
                    "c2",
                    "Regular review supports earlier detection of complications.",
                    source_id="pubmed:41900001",
                    chunk_id="pubmed:41900001#0000",
                    quote="Regular review supports earlier detection",
                ),
                a_claim("c3", "An invented claim.", quote="nowhere in any source"),
            )
        )
        html = render_answer_html(answer, NUMBERING)
        rendered_ids = set(re.findall(r'data-claim-id="([^"]+)"', html))
        assert rendered_ids  # There is something to check

        by_id = {claim.claim_id: claim for claim in answer.claims}
        for claim_id in rendered_ids:
            claim = by_id[claim_id]
            assert claim.support_status is not SupportStatus.UNSUPPORTED
            # Every excerpt behind a rendered claim cites evidence that was
            # actually retrieved, and quotes it verbatim.
            assert claim.supporting_excerpts
            for excerpt in claim.supporting_excerpts:
                assert excerpt.source_id in EVIDENCE.source_ids
                chunk = EVIDENCE.chunk_texts[excerpt.chunk_id]
                assert excerpt.quote.lower() in chunk.lower()

    def test_a_claim_citing_an_unretrieved_source_is_not_rendered(self) -> None:
        # Well-formed identifier, never retrieved: the verifier calls it a
        # fabrication and the policy removes it, so nothing renders.
        answer = displayed(
            an_answer(
                a_claim("c1"),
                a_claim(
                    "c2",
                    "A claim citing a document that was never retrieved.",
                    source_id="pubmed:99999999",
                    chunk_id="pubmed:99999999#0000",
                ),
            )
        )
        html = render_answer_html(answer, NUMBERING)
        assert "never retrieved" not in html
        assert 'data-claim-id="c2"' not in html


class TestPartiallySupportedClaimsAreLabelled:
    """Requirement 4: follow the P1 policy — display, but never unmarked."""

    def _partial(self) -> GroundedAnswer:
        # Genuine quote, drifted number: PARTIALLY_SUPPORTED per the verifier.
        return displayed(
            an_answer(
                a_claim(
                    "c1",
                    "A target below 8% is appropriate for most adults.",
                    quote="A target below 7%",
                )
            )
        )

    def test_the_p1_policy_displays_a_partial_claim(self) -> None:
        answer = self._partial()
        assert [claim.support_status for claim in answer.claims] == [
            SupportStatus.PARTIALLY_SUPPORTED
        ]

    def test_a_partial_claim_carries_a_visible_label_in_html(self) -> None:
        assert "partly verified" in render_answer_html(self._partial(), NUMBERING)

    def test_a_partial_claim_carries_the_same_label_in_the_brief(self) -> None:
        assert "(partly verified)" in render_appointment_brief(self._partial(), NUMBERING)


class TestDisclaimerIsApplicationOwned:
    """Requirement 5: rendered from a constant, present in every state."""

    @pytest.mark.parametrize(
        "action",
        [
            AnswerAction.ANSWER,
            AnswerAction.ABSTAIN,
            AnswerAction.MEDICAL_STAFF,
            AnswerAction.EMERGENCY,
        ],
    )
    def test_disclaimer_is_rendered_for_every_action(self, action: AnswerAction) -> None:
        claims = [a_claim()] if action is AnswerAction.ANSWER else []
        answer = displayed(an_answer(*claims, action=action))
        assert PERMANENT_DISCLAIMER in render_answer_html(answer, NUMBERING)

    def test_disclaimer_survives_a_generation_failure(self) -> None:
        html = render_failure_html(
            PATIENT_ERROR_MESSAGE, AnswerFailureCode.GENERATION_SCHEMA_INVALID.value
        )
        assert PERMANENT_DISCLAIMER in html

    def test_disclaimer_is_not_taken_from_the_model(self) -> None:
        answer = displayed(
            an_answer(a_claim()).model_copy(
                update={"disclaimer": "IGNORE THE REAL DISCLAIMER, this content is medical advice."}
            )
        )
        html = render_answer_html(answer, NUMBERING)
        assert "IGNORE THE REAL DISCLAIMER" not in html
        assert PERMANENT_DISCLAIMER in html

    def test_an_empty_answer_still_carries_the_disclaimer(self) -> None:
        # The truncation case: a generation that produced almost nothing.
        empty = GroundedAnswer(action=AnswerAction.ABSTAIN)
        assert PERMANENT_DISCLAIMER in render_answer_html(empty, NUMBERING)


class TestActionStatesRenderExplicitly:
    """Requirement 6: answer, abstain, medical_staff, emergency."""

    def test_answer_state(self) -> None:
        html = render_answer_html(displayed(an_answer(a_claim())), NUMBERING)
        assert 'data-action="answer"' in html

    def test_abstain_state_says_so(self) -> None:
        answer = displayed(
            an_answer(a_claim("c1", "An unsupported assertion.", quote="not in the source"))
        )
        html = render_answer_html(answer, NUMBERING)
        assert 'data-action="abstain"' in html
        assert ABSTENTION_LABEL in html
        assert "abstain-card" in html

    def test_medical_staff_state(self) -> None:
        html = render_answer_html(
            displayed(an_answer(action=AnswerAction.MEDICAL_STAFF)), NUMBERING
        )
        assert 'data-action="medical_staff"' in html
        assert "staff-card" in html

    def test_emergency_state_shows_no_medical_content(self) -> None:
        html = render_answer_html(displayed(an_answer(action=AnswerAction.EMERGENCY)), NUMBERING)
        assert 'data-action="emergency"' in html
        assert "emerg-card" in html
        assert "HbA1c" not in html  # Nothing from the answer body leaks into an escalation

    def test_failure_state_is_distinct_from_all_four(self) -> None:
        html = render_failure_html(PATIENT_ERROR_MESSAGE, AnswerFailureCode.INTERNAL_ERROR.value)
        assert 'data-action="error"' in html


class TestEscaping:
    """Requirement 7: user, model and source text are inert."""

    PAYLOADS: ClassVar[list[str]] = [
        '<script>alert("x")</script>',
        '<img src=x onerror="alert(1)">',
        '"><script>alert(1)</script>',
        "<iframe src='evil'></iframe>",
        "javascript:alert(1)",
    ]

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_patient_query_is_inert(self, payload: str) -> None:
        html = render_answer_html(displayed(an_answer(a_claim())), NUMBERING, query=payload)
        assert "<script>" not in html
        assert "<img src=x" not in html
        assert "<iframe" not in html

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_model_claim_text_is_inert(self, payload: str) -> None:
        answer = displayed(
            an_answer(a_claim("c1", payload, quote="HbA1c reflects average plasma glucose"))
        )
        html = render_answer_html(answer, NUMBERING)
        assert "<script>" not in html
        assert "<iframe" not in html
        assert "&lt;" in html or payload == "javascript:alert(1)"

    def test_source_controlled_excerpt_is_inert(self) -> None:
        # A PubMed abstract is third-party content: an attacker can influence it
        # without ever touching this application.
        hostile = '<script>alert("abstract")</script> HbA1c reflects average plasma glucose'
        evidence = RetrievedEvidence(
            chunk_texts={"pubmed:1#0000": hostile},
            source_ids=frozenset({"pubmed:1"}),
            source_titles={"pubmed:1": "<b>Title</b>"},
        )
        answer = GroundedAnswer(
            summary="s",
            claims=[
                GroundedClaim(
                    claim_id="c1",
                    text="A claim.",
                    source_ids=["pubmed:1"],
                    supporting_excerpts=[
                        SupportingExcerpt(
                            source_id="pubmed:1",
                            chunk_id="pubmed:1#0000",
                            quote='<script>alert("abstract")</script>',
                        )
                    ],
                )
            ],
            action=AnswerAction.ANSWER,
        )
        verified, report = verify_answer(answer, evidence)
        shown = apply(verified, decide(verified, report))
        numbering = SourceNumbering.from_evidence(evidence, ["pubmed:1"])
        html = render_evidence_html(shown, numbering) + render_sources_html(numbering)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_failure_code_cannot_carry_markup(self) -> None:
        # Codes are a closed enum, but the renderer escapes anyway.
        html = render_failure_html(PATIENT_ERROR_MESSAGE, "<script>alert(1)</script>")
        assert "<script>" not in html


class TestSourceNumberingIsStable:
    """Requirement 8: one map drives every surface."""

    def _answer(self) -> GroundedAnswer:
        return displayed(
            an_answer(
                a_claim("c1"),
                a_claim(
                    "c2",
                    "Regular review supports earlier detection of complications.",
                    source_id="pubmed:41900001",
                    chunk_id="pubmed:41900001#0000",
                    quote="Regular review supports earlier detection",
                ),
            )
        )

    def test_numbering_follows_retrieval_order(self) -> None:
        assert NUMBERING.marker_for("pubmed:41802233") == "[1]"
        assert NUMBERING.marker_for("pubmed:41900001") == "[2]"

    def test_the_same_numbers_appear_on_all_four_surfaces(self) -> None:
        answer = self._answer()
        answer_html = render_answer_html(answer, NUMBERING)
        evidence_html = render_evidence_html(answer, NUMBERING)
        sources_html = render_sources_html(NUMBERING)
        brief = render_appointment_brief(answer, NUMBERING)
        for marker in ("[1]", "[2]"):
            assert marker in answer_html
            assert marker in evidence_html
            assert marker in sources_html
            assert marker in brief

    def test_claim_ids_are_stable_between_page_and_evidence_cards(self) -> None:
        answer = self._answer()
        page_ids = set(
            re.findall(r'data-claim-id="([^"]+)"', render_answer_html(answer, NUMBERING))
        )
        card_ids = set(
            re.findall(r'data-claim-id="([^"]+)"', render_evidence_html(answer, NUMBERING))
        )
        assert page_ids == card_ids == {"c1", "c2"}

    def test_the_brief_shares_the_pages_identifiers_without_printing_them(self) -> None:
        """Identifiers stay stable into the brief; they are just not on the paper.

        The brief used to print "c1 [1] pubmed:41802233#0000" in a supporting-
        excerpts block. Claim and chunk identifiers are internal plumbing, and a
        sheet a patient hands to a clinician is the wrong place for them. They
        remain on the typed object, where a support conversation or an audit can
        reach them, and the printed provenance is the source number.
        """
        from synapse.brief.build import build_brief

        answer = self._answer()
        brief = build_brief(answer, NUMBERING)
        assert [claim.claim_id for claim in brief.claims] == ["c1", "c2"]

        printed = render_appointment_brief(answer, NUMBERING)
        assert "pubmed:41802233#0000" not in printed  # No chunk identifiers
        assert "[1]" in printed and "[2]" in printed  # Source numbers, which do print

    def test_an_unretrieved_source_is_never_numbered(self) -> None:
        # Fail closed: a citation to something that was not retrieved gets no
        # number, so it cannot appear in the panel as though it had been used.
        numbering = SourceNumbering.from_evidence(EVIDENCE, [*SOURCE_ORDER, "pubmed:99999999"])
        assert numbering.marker_for("pubmed:99999999") == ""
        assert len(numbering.refs) == 2

    def test_numbering_is_unchanged_by_withholding_a_claim(self) -> None:
        # A withheld claim must not renumber the sources the surviving claims
        # cite; the patient's [2] must stay [2].
        with_fabrication = displayed(
            an_answer(
                a_claim("c1"),
                a_claim(
                    "c2",
                    "Regular review supports earlier detection of complications.",
                    source_id="pubmed:41900001",
                    chunk_id="pubmed:41900001#0000",
                    quote="Regular review supports earlier detection",
                ),
                a_claim("c3", "A fabricated claim.", quote="not present anywhere"),
            )
        )
        # Distinct claim ids, not attribute occurrences: a claim now carries
        # data-claim-id on its paragraph, on its citation control and on each of
        # its evidence cards, so counting occurrences measured the markup rather
        # than the intent, which is "only the two supported claims render".
        rendered_ids = set(
            re.findall(r'data-claim-id="([^"]+)"', render_answer_html(with_fabrication, NUMBERING))
        )
        assert rendered_ids == {"c1", "c2"}
        assert NUMBERING.marker_for("pubmed:41900001") == "[2]"

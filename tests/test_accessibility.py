"""
Accessibility checks that run without a browser.

Everything here is arithmetic or markup parsing, so it runs in CI on any
machine, in milliseconds, offline, with no external service. That is what lets
it gate every pull request rather than being a job somebody runs before a
release.

What it covers: contrast ratios, heading order, accessible names, icon
handling, live-region politeness, tab order, and the state signals that must be
carried by text rather than colour.

What it cannot cover: focus order, visible focus, zoom reflow, real colour
rendering, and anything a screen reader actually says. Those need a browser
(``tests/test_accessibility_browser.py``) or a human
(docs/accessibility.md §7). **A green run here is not a conformance claim.**
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from synapse.a11y.audit import (
    audit_fragment,
    check_accessible_names,
    check_colour_not_alone,
    check_heading_order,
    check_icons_are_handled,
    check_live_regions,
    check_no_positive_tabindex,
)
from synapse.a11y.contrast import AA_NON_TEXT, AA_TEXT, contrast_ratio
from synapse.a11y.palette import TOKENS, css_variables, failures
from synapse.answer.render import (
    render_answer_html,
    render_failure_html,
    render_sources_html,
)
from synapse.answer.schema import AnswerAction, GroundedAnswer
from synapse.brief.render_print import render_brief_html
from synapse.evidence.insufficient import InsufficientEvidence, InsufficientReason
from synapse.evidence.render import render_citation_control, render_insufficient_html

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_SOURCE = (REPO_ROOT / "app.py").read_text(encoding="utf-8")

# Fixtures reused from the evidence and brief suites rather than duplicated, so
# the markup under test is the markup those suites already pin.
from tests.test_appointment_brief import a_brief  # noqa: E402
from tests.test_evidence_ux import (  # noqa: E402
    NUMBERING,
    a_claim,
    a_metadata,
    an_excerpt,
    displayed,
)


def all_states() -> dict[str, tuple[str, bool]]:
    """Every patient-facing state, and whether its live regions must be polite."""
    answer = displayed(a_claim())
    return {
        "answer": (render_answer_html(answer, NUMBERING), True),
        "evidence expansion": (
            render_citation_control("c1", [(1, a_metadata(), [an_excerpt()], 0.82)]),
            True,
        ),
        "insufficient evidence": (
            render_insufficient_html(InsufficientEvidence(InsufficientReason.OUT_OF_SCOPE)),
            True,
        ),
        "abstain": (
            render_answer_html(
                displayed(a_claim("c1", "Unsupported.", quote="nowhere")), NUMBERING
            ),
            True,
        ),
        "medical staff": (
            render_answer_html(
                GroundedAnswer(summary="s", action=AnswerAction.MEDICAL_STAFF), NUMBERING
            ),
            True,
        ),
        # Emergency is the one state where an assertive announcement is correct.
        "emergency": (
            render_answer_html(GroundedAnswer(action=AnswerAction.EMERGENCY), NUMBERING),
            False,
        ),
        "error": (render_failure_html("Something went wrong.", "internal_error"), True),
        "sources panel": (render_sources_html(NUMBERING), True),
        "appointment brief": (render_brief_html(a_brief()), True),
    }


class TestContrast:
    """1.4.3 (text) and 1.4.11 (non-text)."""

    def test_the_whole_palette_meets_aa(self) -> None:
        broken = failures()
        assert broken == [], "\n".join(result.describe() for result in broken)

    @pytest.mark.parametrize("token", TOKENS, ids=lambda token: token.name)
    def test_each_token_meets_its_requirement(self, token) -> None:
        result = token.measure()
        assert result.passes, result.describe()

    def test_text_tokens_are_held_to_the_text_threshold(self) -> None:
        # Small uppercase labels are not "large text" and must not be graded as
        # such: 3:1 on 10px type is unreadable.
        for token in TOKENS:
            if token.role == "text":
                assert token.required == AA_TEXT, f"{token.name} is graded below AA text"

    def test_borders_and_focus_meet_non_text_contrast(self) -> None:
        for token in TOKENS:
            if token.role in {"border", "focus"}:
                assert token.required >= AA_NON_TEXT

    def test_the_stylesheet_uses_the_palette(self) -> None:
        # The generated :root block must actually be in the app, or the test
        # above is measuring colours nothing uses.
        assert "css_variables()" in APP_SOURCE
        for token in TOKENS:
            if token.role in {"text", "border", "focus"}:
                assert f"--{token.name}" in css_variables()

    def test_no_failing_legacy_colour_survives_in_the_stylesheet(self) -> None:
        """The eight values the audit found, none of which may return."""
        style = APP_SOURCE[APP_SOURCE.index("<style>") :]
        for legacy in ("#4a4a6a", "#5a5a7a", "#68688a", "#1c1c30", "#241a4d", "#6a6a8a"):
            assert legacy not in style, f"{legacy} failed AA and is back in the stylesheet"

    def test_the_contrast_maths_matches_the_specification(self) -> None:
        # Reference values from WCAG itself, so a refactor of the formula fails
        # here rather than silently passing everything.
        assert round(contrast_ratio("#000000", "#ffffff"), 2) == 21.0
        assert round(contrast_ratio("#ffffff", "#ffffff"), 2) == 1.0
        assert round(contrast_ratio("#767676", "#ffffff"), 2) == 4.54


class TestMarkupSemantics:
    """1.3.1, 2.4.6, 4.1.2 across every state."""

    @pytest.mark.parametrize("name", list(all_states()))
    def test_each_state_is_structurally_clean(self, name: str) -> None:
        html, polite = all_states()[name]
        # start_level=2: fragments render inside the page, under its h1.
        findings = audit_fragment(
            html,
            start_level=0 if name == "appointment brief" else 2,
            expect_polite=polite,
        )
        assert findings == [], "\n".join(finding.describe() for finding in findings)

    def test_headings_are_real_headings_not_styled_divs(self) -> None:
        # The section labels used to be <div class="card-label">, which looks
        # like a heading and cannot be navigated to.
        html = render_answer_html(displayed(a_claim()), NUMBERING)
        assert '<h2 class="card-label">Summary</h2>' in html
        assert '<div class="card-label">Summary</div>' not in html

    def test_heading_order_never_skips(self) -> None:
        for name, (html, _polite) in all_states().items():
            start = 0 if name == "appointment brief" else 2
            assert check_heading_order(html, start_level=start) == [], name

    def test_every_interactive_element_has_a_name(self) -> None:
        for name, (html, _polite) in all_states().items():
            assert check_accessible_names(html) == [], name

    def test_decorative_icons_are_hidden(self) -> None:
        for name, (html, _polite) in all_states().items():
            assert check_icons_are_handled(html) == [], name

    def test_the_question_arrow_is_hidden_from_assistive_technology(self) -> None:
        # It used to be announced as "rightwards arrow" before every question.
        html = render_answer_html(displayed(a_claim()), NUMBERING)
        for arrow in re.findall(r'<span class="q-arrow"[^>]*>', html):
            assert 'aria-hidden="true"' in arrow

    def test_no_positive_tabindex_anywhere(self) -> None:
        for name, (html, _polite) in all_states().items():
            assert check_no_positive_tabindex(html) == [], name
        assert check_no_positive_tabindex(APP_SOURCE) == []


class TestLiveRegions:
    """4.1.3, used sparingly."""

    def test_loading_and_outcome_are_announced_politely(self) -> None:
        assert 'role="status" aria-live="polite"' in APP_SOURCE

    def test_only_emergency_interrupts(self) -> None:
        emergency, _ = all_states()["emergency"]
        assert 'role="alert"' in emergency
        for name, (html, _polite) in all_states().items():
            if name == "emergency":
                continue
            assert 'role="alert"' not in html, f"{name} must not interrupt the user"
            assert 'aria-live="assertive"' not in html, name

    def test_the_answer_container_announces_completion(self) -> None:
        answer, _ = all_states()["answer"]
        assert 'role="status"' in answer

    def test_no_state_announces_decorative_content(self) -> None:
        # A live region wrapping the whole page would re-announce the logo,
        # the streaks and the disclaimer on every rerun.
        for name, (html, polite) in all_states().items():
            assert check_live_regions(html, expect_polite=polite) == [], name


class TestColourIsNeverTheOnlySignal:
    """1.4.1."""

    def test_partial_support_is_stated_in_text(self) -> None:
        # A genuine quote with a drifted number: the verifier marks it
        # partially supported, and the display policy shows it labelled.
        answer = displayed(
            a_claim("c1", "A target below 8% is appropriate.", quote="A target below 7%")
        )
        html = render_answer_html(answer, NUMBERING)
        assert check_colour_not_alone(html, signals=("partly verified",)) == []

    def test_evidence_labels_are_stated_in_text(self) -> None:
        html = render_citation_control("c1", [(1, a_metadata(), [an_excerpt()], None)])
        assert (
            check_colour_not_alone(html, signals=("Systematic review", "Reviewed by a clinician"))
            == []
        )

    def test_emergency_is_stated_in_text(self) -> None:
        html, _ = all_states()["emergency"]
        assert check_colour_not_alone(html, signals=("speak with medical staff now",)) == []

    def test_errors_are_stated_in_text(self) -> None:
        html, _ = all_states()["error"]
        assert check_colour_not_alone(html, signals=("Answer unavailable",)) == []

    def test_user_content_in_the_brief_is_labelled_not_only_tinted(self) -> None:
        from synapse.brief.edit import set_topic

        html = render_brief_html(set_topic(a_brief(), "my topic"))
        assert check_colour_not_alone(html, signals=("Written by you",)) == []


class TestAudio:
    """1.4.2. There is no audio."""

    def test_no_audio_api_is_referenced(self) -> None:
        for forbidden in ("AudioContext", "webkitAudioContext", "new Audio(", "<audio"):
            assert forbidden not in APP_SOURCE, f"{forbidden} is back in the interface"

    def test_no_script_is_injected_through_markdown(self) -> None:
        """A ``<script>`` handed to ``st.markdown`` never runs.

        Streamlit renders markdown through React's ``dangerouslySetInnerHTML``,
        which does not execute inserted scripts. The removed audio heartbeat was
        written that way and silently did nothing for the life of the feature,
        which is worse than a visible failure: the code reads as though the
        behaviour exists.

        This walks the AST for ``st.markdown`` calls rather than searching the
        file for the string, because the file legitimately contains a script --
        the Enter-to-send binding -- delivered through ``st.components.v1.html``,
        which is a real iframe and does execute. Banning the string outright
        would forbid the correct mechanism along with the broken one.
        """
        offenders: list[int] = []
        for node in ast.walk(ast.parse(APP_SOURCE)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "markdown"):
                continue
            for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                if (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and "<script" in argument.value.lower()
                ):
                    offenders.append(node.lineno)
        assert offenders == [], f"<script> passed to st.markdown at line(s) {offenders}"

    def test_any_script_goes_through_a_real_component(self) -> None:
        """Scripts are permitted, but only by the mechanism that runs them."""
        if "<script" not in APP_SOURCE.lower():
            return  # Nothing to check
        assert "st.components.v1.html" in APP_SOURCE, (
            "app.py contains a <script> but never calls st.components.v1.html; "
            "a script delivered any other way will not execute"
        )

    def test_the_removal_is_documented_where_it_happened(self) -> None:
        assert "AUDIO REMOVED" in APP_SOURCE


class TestMotion:
    """2.3.3 and 2.2.2."""

    def test_the_os_preference_is_respected(self) -> None:
        assert "prefers-reduced-motion" in APP_SOURCE

    def test_an_in_app_setting_exists(self) -> None:
        assert 'key="reduce_motion"' in APP_SOURCE
        assert "Reduce motion" in APP_SOURCE

    def test_decorative_animation_is_removable(self) -> None:
        # Inline animation styles cannot be overridden by a media query, so the
        # streaks must carry a class.
        assert "streak-decor" in APP_SOURCE
        assert ".streak-decor { display: none !important; }" in APP_SOURCE

    def test_decorative_elements_are_hidden_from_assistive_technology(self) -> None:
        for match in re.findall(r'<div class="streak-decor[^"]*"[^>]*>', APP_SOURCE):
            assert 'aria-hidden="true"' in match


class TestFormsAndControls:
    """3.3.2, 2.5.5, 2.5.8."""

    def test_no_field_relies_on_a_placeholder_as_its_label(self) -> None:
        # `label_visibility="collapsed"` on the question box was the violation.
        code_lines = [
            line
            for line in APP_SOURCE.splitlines()
            if 'label_visibility="collapsed"' in line and not line.strip().startswith("#")
        ]
        assert code_lines == []

    def test_the_question_field_has_a_persistent_label(self) -> None:
        assert '"Your question or symptoms"' in APP_SOURCE

    def test_labels_are_not_hidden_by_css(self) -> None:
        assert ".stTextArea label { display: none !important; }" not in APP_SOURCE
        assert "visibility: visible !important" in APP_SOURCE

    def test_touch_targets_have_a_minimum_size(self) -> None:
        assert APP_SOURCE.count("min-height: 44px") >= 2

    def test_a_visible_focus_indicator_is_defined(self) -> None:
        assert ":focus-visible" in APP_SOURCE
        assert "outline: 3px solid var(--focus-ring)" in APP_SOURCE

    def test_a_skip_link_exists_and_targets_the_form(self) -> None:
        assert "skip-link" in APP_SOURCE
        assert 'href="#ask-synapse"' in APP_SOURCE
        assert 'id="ask-synapse"' in APP_SOURCE


class TestStreamlitChromeSemantics:
    """Requirement 24."""

    def test_the_main_menu_and_header_are_not_removed(self) -> None:
        # They used to be display:none, which strips them from the accessibility
        # tree. The menu holds Settings and Print; the header starts the tab
        # sequence.
        style = APP_SOURCE[APP_SOURCE.index("<style>") :]
        assert "#MainMenu, footer, header" not in style
        assert '[data-testid="stToolbar"] { visibility: hidden; display: none; }' not in style

    def test_only_the_promotional_footer_is_hidden(self) -> None:
        assert "footer { visibility: hidden; display: none; }" in APP_SOURCE


class TestReflow:
    """1.4.10 and 1.4.4."""

    def test_no_horizontal_scrolling_is_introduced(self) -> None:
        assert "overflow-x: hidden" in APP_SOURCE

    def test_long_content_wraps(self) -> None:
        assert "overflow-wrap: anywhere" in APP_SOURCE

    def test_the_layout_is_mobile_first(self) -> None:
        # Enhancements are min-width, so 320px gets the stacked base layout.
        assert "@media (min-width: 420px)" in APP_SOURCE
        assert "@media (max-width:" not in APP_SOURCE.replace("@media (max-width: 0", "")

    def test_the_print_brief_uses_relative_units_for_text(self) -> None:
        html = render_brief_html(a_brief())
        assert "font-size: 10pt" in html  # Physical units are correct for print
        assert "position: fixed" not in html  # Fixed positioning breaks pagination

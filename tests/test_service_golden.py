"""
Golden-state tests for the application service.

Every state a patient can reach, built offline through the real service against
fakes, and pinned to a committed file under ``tests/snapshots/service/``.

The snapshots are **plain text, not HTML**, for the reason
``test_answer_snapshots.py`` already gives: HTML snapshots break on every CSS
class rename and get regenerated without being read, which defeats the purpose.
These carry a little more than the plain-text answer -- the action, the failure
code, brief eligibility, the rewrite disclosure, the numbered sources and the
verbatim excerpts -- because those are precisely the fields the FastAPI layer
will serialise and the Next.js client will render, and a change to any of them
is a change to the contract.

Regenerate intentionally with ``SYNAPSE_UPDATE_SNAPSHOTS=1 pytest``, then read
the diff before committing it.

Alongside the snapshots are assertions that do not depend on wording, because a
snapshot proves only that output did not *change* -- it cannot prove the output
was ever *right*. The invariants are checked directly:

* no failure carries model prose, provider text, or a credential;
* the permanent disclaimer survives every path that shows an answer;
* source numbering is stable and every inline marker resolves;
* an escalation offers no brief and cites nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from synapse.answer.render import (
    PERMANENT_DISCLAIMER,
    render_plain_text,
)
from synapse.answer.schema import AnswerAction, SupportStatus
from synapse.evidence import (
    InsufficientEvidence,
    InsufficientReason,
    from_failure_code,
)
from synapse.service import ConversationTurn
from synapse.ui.errors import PATIENT_ERROR_MESSAGE, AnswerFailureCode
from tests.golden_states import (
    CONFIGURATION_FAILURE,
    MEDICAL_PROSE,
    PROVIDER_SECRET,
    REACHABLE_FROM_A_TURN,
    RESOLVED_QUERY,
    STATES,
    UNREACHABLE_CODES,
    GoldenState,
    brief_eligible,
    failure_code,
)

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "service"

# Failure codes app.py renders as "no verified evidence" rather than "the tool
# broke". Mirrored here so the golden description states which card a patient
# actually sees, not merely which code was recorded.
EVIDENCE_FAILURE_CODES = {"evidence_unavailable", "index_unverified"}


def assert_snapshot(name: str, actual: str) -> None:
    """Compare against a committed golden, or write it on first run."""
    path = SNAPSHOT_DIR / f"{name}.txt"
    if os.getenv("SYNAPSE_UPDATE_SNAPSHOTS") == "1" or not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        if os.getenv("SYNAPSE_UPDATE_SNAPSHOTS") != "1":
            pytest.fail(f"wrote a new golden for {name!r}; review and commit it")
        return
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"golden state {name!r} changed.\n"
        "If the change is intended, regenerate with SYNAPSE_UPDATE_SNAPSHOTS=1 "
        "and read the diff before committing."
    )


def _resolved_query(turn: ConversationTurn) -> str:
    """The disclosed retrieval query, exactly as app.py derives it."""
    presentation = turn.outcome.presentation
    if presentation is None:
        return ""
    metadata = getattr(presentation.conversion, "metadata", None)
    if not isinstance(metadata, dict):
        return ""
    rewrite = metadata.get("rewrite")
    if not isinstance(rewrite, dict):
        return ""
    resolved = rewrite.get("resolved_query")
    return resolved if isinstance(resolved, str) else ""


def describe(turn: ConversationTurn) -> str:
    """Render one turn as the golden file records it.

    Header fields first, because they are the contract a serialiser implements;
    then the patient-facing body, which is what a reviewer actually reads.
    """
    lines = [f"query:          {turn.query}"]
    outcome = turn.outcome

    if not outcome.ok:
        code = failure_code(turn)
        card = "insufficient evidence" if code in EVIDENCE_FAILURE_CODES else "failure"
        lines += [
            "outcome:        failure",
            f"failure_code:   {code}",
            f"patient_card:   {card}",
            f"brief:          {'eligible' if brief_eligible(outcome) else 'not eligible'}",
            "",
        ]
        if code in EVIDENCE_FAILURE_CODES:
            state = InsufficientEvidence(from_failure_code(code))
            lines += [state.heading, "", state.message, "", state.not_a_judgement]
        else:
            lines.append(PATIENT_ERROR_MESSAGE)
            lines += ["", f"Reference code: {code}"]
        return "\n".join(lines) + "\n"

    presentation = outcome.presentation
    assert presentation is not None  # ok implies a presentation; narrows for mypy
    answer = presentation.answer
    numbering = presentation.numbering
    resolved = _resolved_query(turn)

    lines += [
        "outcome:        answer",
        f"action:         {answer.action.value}",
        f"brief:          {'eligible' if brief_eligible(outcome) else 'not eligible'}",
        f"rewrite:        {resolved or '(none)'}",
        f"claims_shown:   {len(answer.claims)}",
        f"claims_withheld:{len(presentation.result.decision.withheld_claim_ids)}",
        f"sources:        {len(numbering.refs)}",
        "",
        "--- what the patient sees " + "-" * 46,
        render_plain_text(answer, numbering),
    ]

    if answer.claims:
        lines += ["", "--- supporting excerpts " + "-" * 48]
        for claim in answer.claims:
            for excerpt in claim.supporting_excerpts:
                marker = numbering.marker_for(excerpt.source_id)
                lines.append(f'{marker} {excerpt.chunk_id}: "{excerpt.quote}"')

    if numbering.refs:
        lines += ["", "--- sources " + "-" * 60]
        for ref in numbering.ordered():
            score = "-" if ref.relevance_score is None else f"{ref.relevance_score:.2f}"
            lines.append(f"[{ref.number}] {ref.title} ({ref.source_id})  relevance={score}")
            lines.append(f"    {ref.url}")

    return "\n".join(lines) + "\n"


class TestGoldenStates:
    """Every state a patient can reach, pinned."""

    @pytest.mark.parametrize("state", STATES, ids=lambda state: state.name)
    def test_state_matches_its_golden(self, state: GoldenState) -> None:
        assert_snapshot(state.name, describe(state.build()))

    def test_every_state_is_deterministic(self) -> None:
        """A golden is worthless if the same input produces two outputs."""
        for state in STATES:
            assert describe(state.build()) == describe(state.build()), (
                f"{state.name} is not reproducible"
            )


class TestEveryOutcomeIsCovered:
    """The set of states is complete, not merely non-empty."""

    def test_every_action_state_has_a_golden(self) -> None:
        covered = {state.build().action for state in STATES if state.build().outcome.ok}
        missing = set(AnswerAction) - covered
        assert missing == set(), f"no golden state produces action(s): {sorted(missing)}"

    def test_every_reachable_failure_code_has_a_golden(self) -> None:
        covered = {failure_code(state.build()) for state in STATES} - {""}
        reachable = {code.value for code in AnswerFailureCode} - {
            code.value for code in UNREACHABLE_CODES
        }
        assert reachable - covered == set(), (
            f"no golden state produces failure code(s): {sorted(reachable - covered)}"
        )

    def test_no_unreachable_code_is_quietly_produced(self) -> None:
        """Guards the guard, in the other direction.

        If a change makes ``index_unverified`` reachable -- which would be a
        *fix* -- this fails and forces :data:`UNREACHABLE_CODES` and
        ``docs/migration-parity.md`` to be updated deliberately rather than the
        gap silently closing and nobody noticing the card changed.
        """
        covered = {failure_code(state.build()) for state in STATES} - {""}
        newly_reachable = covered & {code.value for code in UNREACHABLE_CODES}
        assert newly_reachable == set(), (
            f"{sorted(newly_reachable)} is now reachable from a turn. "
            "Update UNREACHABLE_CODES and docs/migration-parity.md §5."
        )

    def test_an_unverifiable_index_still_fails_closed(self) -> None:
        """The card is wrong; the *behaviour* is not, and that is what matters.

        An integrity error during retrieval is classified ``retrieval_failed``
        rather than ``index_unverified`` (see UNREACHABLE_CODES). The patient
        gets the generic failure card instead of the specific one -- but nothing
        is generated, nothing is cited, and no prose is shown, which are the
        properties the index gate exists to guarantee.
        """
        turn = next(state for state in STATES if state.name == "failure_index_gate_raised").build()
        assert not turn.outcome.ok
        assert turn.outcome.presentation is None
        assert failure_code(turn) == AnswerFailureCode.RETRIEVAL_FAILED.value

    def test_the_configuration_failure_shows_the_same_patient_message(self) -> None:
        assert CONFIGURATION_FAILURE.patient_message == PATIENT_ERROR_MESSAGE
        assert CONFIGURATION_FAILURE.code is AnswerFailureCode.CONFIGURATION_ERROR

    @pytest.mark.parametrize("reason", list(InsufficientReason), ids=lambda r: r.value)
    def test_every_insufficient_reason_renders(self, reason: InsufficientReason) -> None:
        """All six render; only three are reachable from a turn today.

        The distinction is the point. A later phase must not read "we can draw
        this card" as "the system produces this state".
        """
        state = InsufficientEvidence(reason)
        assert state.message
        assert state.not_a_judgement
        assert_snapshot(
            f"insufficient_{reason.value}",
            "\n".join(
                [
                    f"reason:      {reason.value}",
                    f"reachable:   {'yes' if reason in REACHABLE_FROM_A_TURN else 'no'}",
                    "",
                    state.heading,
                    "",
                    state.message,
                    "",
                    state.not_a_judgement,
                ]
            )
            + "\n",
        )

    def test_the_reachable_set_is_exactly_what_the_service_decides(self) -> None:
        """Guards the guard: if a new reason becomes reachable, update the set."""
        from_codes = {from_failure_code(code) for code in EVIDENCE_FAILURE_CODES}
        # An abstention is rendered as BELOW_THRESHOLD by app.py.
        from_codes.add(InsufficientReason.BELOW_THRESHOLD)
        assert from_codes == set(REACHABLE_FROM_A_TURN)


class TestFailuresNeverCarryContent:
    """The property this whole layer exists to guarantee."""

    def test_no_failure_state_leaks_model_prose(self) -> None:
        for state in STATES:
            turn = state.build()
            if turn.outcome.ok:
                continue
            rendered = describe(turn)
            assert MEDICAL_PROSE not in rendered
            assert "metformin" not in rendered.lower()

    def test_no_failure_state_leaks_provider_detail(self) -> None:
        for state in STATES:
            turn = state.build()
            if turn.outcome.ok:
                continue
            rendered = describe(turn)
            assert PROVIDER_SECRET not in rendered
            assert "sk-live" not in rendered
            assert "example" not in rendered

    def test_every_failure_shows_one_fixed_message(self) -> None:
        for state in STATES:
            turn = state.build()
            failure = turn.outcome.failure
            if failure is None:
                continue
            assert failure.patient_message == PATIENT_ERROR_MESSAGE

    def test_a_failed_turn_has_no_presentation_at_all(self) -> None:
        for state in STATES:
            turn = state.build()
            if turn.outcome.ok:
                continue
            assert turn.outcome.presentation is None
            assert turn.action is None


class TestAnswerInvariants:
    """What must hold on every path that shows an answer."""

    def test_the_disclaimer_survives_every_answer_path(self) -> None:
        for state in STATES:
            turn = state.build()
            presentation = turn.outcome.presentation
            if presentation is None:
                continue
            rendered = render_plain_text(presentation.answer, presentation.numbering)
            assert PERMANENT_DISCLAIMER in rendered, f"{state.name} dropped the disclaimer"

    def test_every_inline_marker_resolves_to_a_numbered_source(self) -> None:
        for state in STATES:
            presentation = state.build().outcome.presentation
            if presentation is None:
                continue
            for claim in presentation.answer.claims:
                for source_id in claim.source_ids:
                    assert presentation.numbering.marker_for(source_id), (
                        f"{state.name}: claim cites {source_id}, which has no number"
                    )

    def test_source_numbering_starts_at_one_and_is_contiguous(self) -> None:
        for state in STATES:
            presentation = state.build().outcome.presentation
            if presentation is None:
                continue
            numbers = [ref.number for ref in presentation.numbering.ordered()]
            assert numbers == list(range(1, len(numbers) + 1)), state.name

    def test_an_escalation_cites_nothing_and_offers_no_brief(self) -> None:
        for name in ("emergency", "emergency_latched"):
            turn = next(state for state in STATES if state.name == name).build()
            presentation = turn.outcome.presentation
            assert presentation is not None
            assert presentation.answer.action is AnswerAction.EMERGENCY
            assert presentation.numbering.refs == {}
            assert presentation.answer.claims == []
            assert not brief_eligible(turn.outcome)

    def test_abstention_still_offers_a_brief(self) -> None:
        """The questions survive even when the claims do not."""
        turn = next(state for state in STATES if state.name == "abstain").build()
        assert turn.action is AnswerAction.ABSTAIN
        assert brief_eligible(turn.outcome)

    def test_a_partially_supported_claim_is_marked_not_corrected(self) -> None:
        turn = next(state for state in STATES if state.name == "partially_supported").build()
        presentation = turn.outcome.presentation
        assert presentation is not None
        statuses = [claim.support_status for claim in presentation.answer.claims]
        assert SupportStatus.PARTIALLY_SUPPORTED in statuses
        assert "partly verified" in render_plain_text(presentation.answer, presentation.numbering)


class TestFollowUpDisclosure:
    """The rewrite is disclosed, and only when it happened."""

    def test_a_rewritten_follow_up_discloses_the_resolved_query(self) -> None:
        turn = next(state for state in STATES if state.name == "follow_up_rewritten").build()
        assert _resolved_query(turn) == RESOLVED_QUERY

    def test_a_standalone_question_discloses_nothing(self) -> None:
        turn = next(state for state in STATES if state.name == "answer").build()
        assert _resolved_query(turn) == ""

    def test_the_resolved_query_is_never_a_flat_metadata_value(self) -> None:
        """Nested on purpose: a flat string reaches the "turn rendered" log line.

        ``answer_turn`` spreads every non-dict metadata value into its
        ``extra={}``. A bare ``resolved_query`` string there would put the
        patient's question into the logs, which the privacy policy forbids.
        """
        turn = next(state for state in STATES if state.name == "follow_up_rewritten").build()
        presentation = turn.outcome.presentation
        assert presentation is not None
        metadata = getattr(presentation.conversion, "metadata", {})
        flat = {key: value for key, value in metadata.items() if not isinstance(value, dict)}
        assert RESOLVED_QUERY not in str(flat)
        assert flat["query_rewritten"] is True

"""
Snapshot tests for patient-facing rendering.

Snapshots are committed under ``tests/snapshots/`` and reviewed like code. The
point is not that the exact wording is sacred — it is that a change to what a
patient sees becomes a **visible diff in a pull request** rather than something
noticed after release.

The plain-text renderer and the exported appointment brief are snapshotted
rather than the HTML, deliberately: HTML snapshots break on every CSS class
rename and get regenerated without being read, which defeats the purpose. The
escaping guarantees are asserted directly in ``test_answer_adversarial.py``, and
the markup structure in ``test_ui_rendering.py``.

The brief is snapshotted for all four action states, because it is the artifact
that leaves the building — a patient carries it into an appointment, and a
clinician may read it without ever having seen the screen it came from.

Regenerate intentionally with ``SYNAPSE_UPDATE_SNAPSHOTS=1 pytest``, then read
the diff before committing it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.answer.brief import render_appointment_brief
from synapse.answer.policy import apply, decide
from synapse.answer.render import SourceNumbering, render_plain_text
from synapse.answer.schema import (
    AnswerAction,
    GroundedAnswer,
    GroundedClaim,
    SupportingExcerpt,
)
from synapse.answer.verify import RetrievedEvidence, verify_answer

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

CHUNK_TEXT = (
    "HbA1c reflects average plasma glucose over 2-3 months. "
    "A target below 7% is appropriate for most non-pregnant adults with diabetes."
)

EVIDENCE = RetrievedEvidence(
    chunk_texts={
        "pubmed:41802233#0000": CHUNK_TEXT,
        "pubmed:41900001#0000": "Regular review supports earlier detection of complications.",
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


def assert_snapshot(name: str, actual: str) -> None:
    """Compare against a committed snapshot, or write it on first run.

    A missing snapshot is written and the test fails, so a new snapshot is never
    silently created and passed in the same run — it has to be reviewed and
    committed deliberately.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.txt"

    if os.environ.get("SYNAPSE_UPDATE_SNAPSHOTS") == "1":
        path.write_text(actual, encoding="utf-8")
        return

    if not path.is_file():
        path.write_text(actual, encoding="utf-8")
        pytest.fail(
            f"snapshot '{name}' did not exist and has been written; review and commit it, then re-run"
        )

    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"rendered output changed for snapshot '{name}'.\n"
        "This is patient-facing text. Read the diff before regenerating with SYNAPSE_UPDATE_SNAPSHOTS=1."
    )


def a_claim(claim_id: str, text: str, source_id: str, chunk_id: str, quote: str) -> GroundedClaim:
    """A claim citing real evidence."""
    return GroundedClaim(
        claim_id=claim_id,
        text=text,
        source_ids=[source_id],
        supporting_excerpts=[
            SupportingExcerpt(source_id=source_id, chunk_id=chunk_id, quote=quote)
        ],
    )


def render(answer: GroundedAnswer) -> str:
    """Verify, gate and render, exactly as the application does."""
    verified, report = verify_answer(answer, EVIDENCE)
    shown = apply(verified, decide(verified, report))
    return render_plain_text(shown, SourceNumbering.from_evidence(EVIDENCE, SOURCE_ORDER))


class TestPatientFacingSnapshots:
    """What a patient actually sees, pinned."""

    def test_fully_grounded_answer(self) -> None:
        answer = GroundedAnswer(
            summary="Your HbA1c test shows your average blood sugar over the past two to three months.",
            claims=[
                a_claim(
                    "c1",
                    "HbA1c reflects average plasma glucose over 2-3 months.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "HbA1c reflects average plasma glucose over 2-3 months",
                ),
                a_claim(
                    "c2",
                    "Regular review supports earlier detection of complications.",
                    "pubmed:41900001",
                    "pubmed:41900001#0000",
                    "Regular review supports earlier detection of complications",
                ),
            ],
            doctor_evaluation="Your clinician will look at this result alongside your history and current treatment.",
            questions_for_doctor=[
                "What does my result mean for me specifically?",
                "How often should this be rechecked?",
            ],
            limitations=[
                "This is general information from published research, not advice about your own care."
            ],
            disclaimer="model-supplied text the renderer ignores",
            action=AnswerAction.ANSWER,
        )
        assert_snapshot("grounded_answer", render(answer))

    def test_partially_supported_claim_is_flagged(self) -> None:
        # The quote is genuine; the claim's threshold is not. The patient sees
        # the claim marked, not silently corrected.
        answer = GroundedAnswer(
            summary="Targets vary between people.",
            claims=[
                a_claim(
                    "c1",
                    "A target below 8% is appropriate for most adults.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "A target below 7%",
                )
            ],
            doctor_evaluation="Your clinician will set a target with you.",
            questions_for_doctor=["What target is right for me?"],
            limitations=["Targets are individual."],
            action=AnswerAction.ANSWER,
        )
        assert_snapshot("partially_supported", render(answer))

    def test_abstention_when_evidence_is_insufficient(self) -> None:
        answer = GroundedAnswer(
            summary="This summary will be replaced by the abstention wording.",
            claims=[
                a_claim(
                    "c1",
                    "An unsupported assertion.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "a quote that is not in the source",
                )
            ],
            doctor_evaluation="This will be cleared.",
            questions_for_doctor=["What should I ask about this?"],
            limitations=[],
            action=AnswerAction.ANSWER,
        )
        assert_snapshot("abstention", render(answer))

    def test_emergency_routing(self) -> None:
        assert_snapshot("emergency", render(GroundedAnswer(action=AnswerAction.EMERGENCY)))

    def test_medical_staff_routing(self) -> None:
        answer = GroundedAnswer(
            summary="",
            questions_for_doctor=["Can someone look at this before my appointment?"],
            action=AnswerAction.MEDICAL_STAFF,
        )
        assert_snapshot("medical_staff", render(answer))

    def test_withheld_claim_does_not_appear(self) -> None:
        # Two supported claims, one fabricated. The fabricated one must be
        # absent from the rendering entirely — not greyed out, not footnoted.
        answer = GroundedAnswer(
            summary="A summary.",
            claims=[
                a_claim(
                    "c1",
                    "HbA1c reflects average plasma glucose over 2-3 months.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "HbA1c reflects average plasma glucose",
                ),
                a_claim(
                    "c2",
                    "Regular review supports earlier detection of complications.",
                    "pubmed:41900001",
                    "pubmed:41900001#0000",
                    "Regular review supports earlier detection",
                ),
                a_claim(
                    "c3",
                    "This claim cites a real source but invents its quote.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "entirely fabricated quotation",
                ),
            ],
            doctor_evaluation="Your clinician will review this with you.",
            questions_for_doctor=["What does this mean for me?"],
            limitations=["General information only."],
            action=AnswerAction.ANSWER,
        )
        rendered = render(answer)
        assert "invents its quote" not in rendered  # Asserted directly as well as snapshotted
        assert_snapshot("withheld_claim", rendered)


class TestSnapshotInvariants:
    """Properties every rendering must satisfy, whatever the snapshot says."""

    ALL_ACTIONS = (
        AnswerAction.ANSWER,
        AnswerAction.ABSTAIN,
        AnswerAction.MEDICAL_STAFF,
        AnswerAction.EMERGENCY,
    )

    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_disclaimer_appears_in_every_rendering(self, action: AnswerAction) -> None:
        from synapse.answer.render import PERMANENT_DISCLAIMER

        claims = (
            [
                a_claim(
                    "c1",
                    "HbA1c reflects average plasma glucose over 2-3 months.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "HbA1c reflects average plasma glucose",
                )
            ]
            if action is AnswerAction.ANSWER
            else []
        )
        answer = GroundedAnswer(summary="s", claims=claims, action=action)
        assert PERMANENT_DISCLAIMER in render(answer)

    def test_no_unsupported_claim_text_ever_appears(self) -> None:
        sentinel = "SENTINEL-UNSUPPORTED-MEDICAL-CLAIM"
        answer = GroundedAnswer(
            summary="s",
            claims=[
                a_claim(
                    "c1",
                    sentinel,
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "not in the source at all",
                )
            ],
            action=AnswerAction.ANSWER,
        )
        assert sentinel not in render(answer)

    def test_every_displayed_claim_carries_a_source_marker(self) -> None:
        answer = GroundedAnswer(
            summary="s",
            claims=[
                a_claim(
                    "c1",
                    "HbA1c reflects average plasma glucose over 2-3 months.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "HbA1c reflects average plasma glucose",
                )
            ],
            action=AnswerAction.ANSWER,
        )
        rendered = render(answer)
        assert "[1]" in rendered

    def test_rendering_is_deterministic(self) -> None:
        answer = GroundedAnswer(
            summary="s",
            claims=[
                a_claim(
                    "c1",
                    "HbA1c reflects average plasma glucose over 2-3 months.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "HbA1c reflects average plasma glucose",
                )
            ],
            action=AnswerAction.ANSWER,
        )
        assert render(answer) == render(answer)


# Pinned so the snapshot is byte-stable. A real export stamps the current time
# and a fresh random document identifier; neither can appear in a committed file.
FIXED_BRIEF_TIME = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
FIXED_DOCUMENT_ID = "0123456789ab"


def render_brief(answer: GroundedAnswer, *, query: str = "") -> str:
    """Verify, gate and export, exactly as the application does."""
    verified, report = verify_answer(answer, EVIDENCE)
    shown = apply(verified, decide(verified, report))
    return render_appointment_brief(
        shown,
        SourceNumbering.from_evidence(EVIDENCE, SOURCE_ORDER),
        query=query,
        generated_at=FIXED_BRIEF_TIME,
        document_id=FIXED_DOCUMENT_ID,
    )


class TestAppointmentBriefSnapshots:
    """The takeaway, pinned for every action state.

    ``generated_at`` is deliberately omitted so the output is byte-stable; the
    application passes a timestamp, and that one line is the only difference.
    """

    QUERY = "why does my blood sugar keep going up?"

    def test_answer_state(self) -> None:
        answer = GroundedAnswer(
            summary="Your HbA1c test shows your average blood sugar over the past two to three months.",
            claims=[
                a_claim(
                    "c1",
                    "HbA1c reflects average plasma glucose over 2-3 months.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "HbA1c reflects average plasma glucose over 2-3 months",
                ),
                a_claim(
                    "c2",
                    "Regular review supports earlier detection of complications.",
                    "pubmed:41900001",
                    "pubmed:41900001#0000",
                    "Regular review supports earlier detection of complications",
                ),
            ],
            doctor_evaluation="Your clinician will look at this result alongside your history and current treatment.",
            questions_for_doctor=[
                "What does my result mean for me specifically?",
                "How often should this be rechecked?",
            ],
            limitations=[
                "This is general information from published research, not advice about your own care."
            ],
            action=AnswerAction.ANSWER,
        )
        assert_snapshot("brief_answer", render_brief(answer, query=self.QUERY))

    def test_abstain_state(self) -> None:
        answer = GroundedAnswer(
            summary="This summary is replaced by the abstention wording.",
            claims=[
                a_claim(
                    "c1",
                    "An unsupported assertion.",
                    "pubmed:41802233",
                    "pubmed:41802233#0000",
                    "a quote that is not in the source",
                )
            ],
            doctor_evaluation="This is cleared on abstention.",
            questions_for_doctor=["What should I ask about this?"],
            action=AnswerAction.ANSWER,
        )
        assert_snapshot("brief_abstain", render_brief(answer, query=self.QUERY))

    def test_medical_staff_state(self) -> None:
        answer = GroundedAnswer(
            questions_for_doctor=["Can someone look at this before my appointment?"],
            action=AnswerAction.MEDICAL_STAFF,
        )
        assert_snapshot("brief_medical_staff", render_brief(answer, query=self.QUERY))

    def test_emergency_state(self) -> None:
        # No medical content, no sources, disclaimer still present.
        assert_snapshot(
            "brief_emergency",
            render_brief(GroundedAnswer(action=AnswerAction.EMERGENCY), query=self.QUERY),
        )

"""
Print snapshots for the appointment brief, at both standard page sizes.

The committed artefacts are the HTML documents themselves, which is what the
user actually receives: byte-for-byte the file the download button hands over.
PNG renders are produced from them for visual QA (see docs/appointment-brief.md
§7) but nothing asserts against pixels, because a pixel comparison across
platforms and font stacks fails for reasons that have nothing to do with the
document being wrong.

Every snapshot is deterministic: a fixed timestamp, a fixed document identifier,
and a fixed source pack. A brief that changed on every run would be a snapshot
nobody could review.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.answer.policy import apply, decide
from synapse.answer.render import PERMANENT_DISCLAIMER, SourceNumbering
from synapse.answer.schema import (
    AnswerAction,
    GroundedAnswer,
    GroundedClaim,
    SupportingExcerpt,
)
from synapse.answer.verify import RetrievedEvidence, verify_answer
from synapse.brief.build import build_brief
from synapse.brief.edit import add_question, edit_claim_text, set_notes, set_sections, set_topic
from synapse.brief.render_print import estimate_fit, render_brief_html
from synapse.brief.schema import BriefSection
from synapse.evidence.metadata import MetadataResolver

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "docs" / "snapshots" / "brief"
PACK_DIR = REPO_ROOT / "source_packs" / "diabetes-previsit"

FIXED_TIME = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
FIXED_ID = "0123456789ab"

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


def assert_snapshot(name: str, actual: str) -> None:
    """Compare against a committed snapshot, or write it on first run."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.html"
    if os.environ.get("SYNAPSE_UPDATE_SNAPSHOTS") == "1":
        path.write_text(actual, encoding="utf-8")
        return
    if not path.is_file():
        path.write_text(actual, encoding="utf-8")
        pytest.fail(f"snapshot '{name}' did not exist and has been written; open it, then re-run")
    assert actual == path.read_text(encoding="utf-8"), (
        f"the printed brief changed for '{name}'. This is a document a patient keeps. "
        "Open docs/snapshots/brief/ and look at the change before regenerating."
    )


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


def a_brief(*claims: GroundedClaim, **kwargs):
    """Build a deterministic brief from the example pack."""
    if not claims:
        claims = (
            a_claim(
                "c1",
                "HbA1c reflects average plasma glucose over 2-3 months.",
                "example:diabetes-0001",
                "HbA1c reflects average plasma glucose over 2-3 months",
            ),
            a_claim(
                "c2",
                "Regular review supports earlier detection of complications.",
                "example:diabetes-0002",
                "Regular review supports earlier detection of complications",
            ),
        )
    answer = GroundedAnswer(
        summary="Your HbA1c test shows your average blood sugar over the past two to three months.",
        claims=list(claims),
        doctor_evaluation="Your clinician will read this alongside your history.",
        questions_for_doctor=[
            "What does my result mean for me specifically?",
            "How often should this be rechecked?",
        ],
        limitations=[
            "This is general information from published research, not advice about your own care."
        ],
        action=AnswerAction.ANSWER,
    )
    verified, report = verify_answer(answer, EVIDENCE)
    return build_brief(
        apply(verified, decide(verified, report)),
        SourceNumbering.from_evidence(EVIDENCE, ORDER),
        resolver=MetadataResolver.from_directory(PACK_DIR),
        generated_at=FIXED_TIME,
        document_id=FIXED_ID,
        app_version="0.1.0",
        corpus_version="ci-fixture-corpus-v1",
        **kwargs,
    )


class TestPrintSnapshots:
    """One committed document per state, at both page sizes."""

    def test_full_brief_a4(self) -> None:
        brief = set_notes(
            set_topic(a_brief(), "My blood sugar readings have been higher in the mornings."),
            "Started about three weeks ago. Worse after I changed my evening meal times.",
        )
        assert_snapshot("brief-full-a4", render_brief_html(brief, page_size="a4"))

    def test_full_brief_letter(self) -> None:
        brief = set_notes(
            set_topic(a_brief(), "My blood sugar readings have been higher in the mornings."),
            "Started about three weeks ago. Worse after I changed my evening meal times.",
        )
        assert_snapshot("brief-full-letter", render_brief_html(brief, page_size="letter"))

    def test_brief_with_user_edits(self) -> None:
        brief = add_question(
            edit_claim_text(
                set_topic(a_brief(), "My HbA1c result"),
                "c1",
                "My HbA1c is a three month average of my blood sugar",
            ),
            "Should I be testing at home, and how often?",
        )
        assert_snapshot("brief-user-edited", render_brief_html(brief))

    def test_minimal_brief_questions_only(self) -> None:
        brief = set_sections(a_brief(), [BriefSection.QUESTIONS])
        assert_snapshot("brief-questions-only", render_brief_html(brief))

    def test_brief_with_no_verified_content(self) -> None:
        brief = set_notes(
            a_brief(a_claim("c1", "Unsupported.", "example:diabetes-0001", "nowhere")),
            "I want to ask about my morning readings.",
        )
        assert brief.has_verified_content is False
        assert_snapshot("brief-no-evidence", render_brief_html(brief))

    def test_overlong_brief(self) -> None:
        # Kept as a snapshot so the overflow case is reviewable rather than
        # theoretical: this is what the user sees before being asked to trim.
        # Long enough to genuinely exceed one page: the estimator is asserted
        # below, so a fixture that quietly fitted would make this a no-op test.
        brief = set_notes(a_brief(), "I have noticed this pattern for a while now. " * 60)
        assert estimate_fit(brief).fits is False
        assert_snapshot("brief-overflow", render_brief_html(brief))


class TestPrintSnapshotsStayHonest:
    """Properties every committed document must have."""

    def _documents(self) -> list[tuple[str, str]]:
        return [
            (path.name, path.read_text(encoding="utf-8"))
            for path in sorted(SNAPSHOT_DIR.glob("brief-*.html"))
        ]

    def test_every_document_carries_the_disclaimer(self) -> None:
        for name, text in self._documents():
            assert PERMANENT_DISCLAIMER in text, f"{name} lost the disclaimer"

    def test_no_document_reaches_the_network(self) -> None:
        for name, text in self._documents():
            assert "fonts.googleapis" not in text, name
            assert "<script" not in text, name
            assert "<img" not in text, name
            for url in re.findall(r'href="([^"]+)"', text):
                assert url.startswith("https://"), f"{name}: {url}"

    def test_no_document_contains_a_secret_or_a_prompt(self) -> None:
        for name, text in self._documents():
            lowered = text.lower()
            assert "sk-" not in lowered, name
            assert "api_key" not in lowered, name
            assert "you are a patient education assistant" not in lowered, name
            assert "session_id" not in lowered, name

    def test_every_document_declares_its_page_size(self) -> None:
        for name, text in self._documents():
            assert re.search(r"@page \{ size: (A4|Letter);", text), name

    def test_every_document_has_print_metadata(self) -> None:
        for name, text in self._documents():
            assert "<title>Appointment brief" in text, name
            assert 'name="robots"' in text, name

    def test_the_expected_states_are_all_present(self) -> None:
        expected = {
            "brief-full-a4",
            "brief-full-letter",
            "brief-user-edited",
            "brief-questions-only",
            "brief-no-evidence",
            "brief-overflow",
        }
        assert expected <= {path.stem for path in SNAPSHOT_DIR.glob("brief-*.html")}

"""
Lifecycle state machine and source-pack schema tests.

The assertions that matter most are the ones proving a *negative*: that there
is no argument, no state, and no code path by which automation can produce a
clinical approval. Those are grouped in ``TestAutomationCannotApprove``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from synapse.governance.states import (
    AUTOMATION_CREATABLE_STATES,
    REVIEW_REQUIRED_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    GovernanceError,
    TransitionActor,
    allowed_targets,
    require_automation_creatable,
    require_transition,
)
from synapse.hashing import sha256_text
from synapse.schemas.enums import (
    EvidenceType,
    PackApprovalState,
    ReviewDecision,
    ReviewerRole,
)
from synapse.schemas.enums import (
    SourceLifecycleState as State,
)
from synapse.schemas.source_pack import (
    ClinicianReview,
    PackScope,
    PackSource,
    ReviewerRequirements,
    SourcePackManifest,
    SourceSelectionPolicy,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DUE = date(2027, 1, 1)


def a_review(
    decision: ReviewDecision = ReviewDecision.APPROVED, role: ReviewerRole = ReviewerRole.CLINICIAN
) -> ClinicianReview:
    """A complete review record used to exercise the rules.

    The reviewer identifier is an obvious placeholder pseudonym, not a person.
    """
    return ClinicianReview(
        reviewer_id="rev_00000000",
        reviewer_role=role,
        reviewed_at=NOW,
        review_due_at=DUE,
        rubric_version="rubric-1",
        relevance="Directly addresses a supported topic.",
        population_applicability="Study population matches the pack's intended population.",
        evidence_quality="Adequately powered with appropriate controls.",
        known_limitations="Single-centre; results may not generalise.",
        patient_safety_concerns="None identified.",
        decision=decision,
        decision_rationale="Meets the pack's stated scope and evidence requirements.",
    )


def a_source(**overrides) -> PackSource:
    """A minimal valid pack source."""
    base = {
        "document_id": "example:test-0001",
        "canonical_url": "https://example.org/test",
        "title": "A synthetic test source",
        "content_sha256": sha256_text("content"),
        "discovered_at": NOW,
        "discovery_method": "unit_test",
    }
    return PackSource(**{**base, **overrides})


def a_manifest(**overrides) -> SourcePackManifest:
    """A minimal valid pack manifest."""
    base = {
        "pack_id": "test-pack",
        "version": "0.1.0",
        "title": "Test pack",
        "description": "For tests.",
        "scope": PackScope(
            intended_patient_population="Adults",
            intended_use="Testing",
            excluded_uses=["Anything clinical"],
            supported_topics=["testing"],
        ),
        "selection_policy": SourceSelectionPolicy(
            description="Test policy",
            minimum_evidence_types=[EvidenceType.GUIDELINE, EvidenceType.RCT],
        ),
        "reviewer_requirements": ReviewerRequirements(rubric_version="rubric-1"),
        "corpus_sha256": "0" * 64,
        "source_count": 0,
        "build_tool_version": "0.1.0",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return SourcePackManifest(**{**base, **overrides})


class TestLifecycleStates:
    """Requirement 2: the six states, and the transitions between them."""

    def test_all_six_states_exist(self) -> None:
        assert {s.value for s in State} == {
            "discovered",
            "screened",
            "approved",
            "rejected",
            "expired",
            "superseded",
        }

    def test_superseded_is_terminal(self) -> None:
        assert State.SUPERSEDED in TERMINAL_STATES
        assert allowed_targets(State.SUPERSEDED) == frozenset()

    def test_discovered_cannot_jump_straight_to_approved(self) -> None:
        # Screening is not optional: it is where a human first looks at the
        # source at all.
        with pytest.raises(GovernanceError, match="not permitted"):
            require_transition(State.DISCOVERED, State.APPROVED, actor=TransitionActor.HUMAN_REVIEW)

    def test_screened_may_be_approved_by_a_human(self) -> None:
        transition = require_transition(
            State.SCREENED, State.APPROVED, actor=TransitionActor.HUMAN_REVIEW
        )
        assert transition.actor is TransitionActor.HUMAN_REVIEW

    def test_expired_may_be_re_approved_after_fresh_review(self) -> None:
        # Expiry is not permanent — but recovery requires a human.
        assert State.APPROVED in allowed_targets(State.EXPIRED)

    def test_rejected_may_be_reopened(self) -> None:
        assert State.SCREENED in allowed_targets(State.REJECTED)

    def test_self_transition_is_rejected(self) -> None:
        with pytest.raises(GovernanceError, match="identical"):
            require_transition(State.APPROVED, State.APPROVED, actor=TransitionActor.HUMAN_REVIEW)

    def test_nothing_leaves_a_terminal_state(self) -> None:
        with pytest.raises(GovernanceError, match="terminal"):
            require_transition(State.SUPERSEDED, State.SCREENED, actor=TransitionActor.HUMAN_REVIEW)

    def test_every_transition_declares_an_actor(self) -> None:
        # Adding a state without deciding who may drive it is impossible.
        assert all(
            t.actor in (TransitionActor.AUTOMATION, TransitionActor.HUMAN_REVIEW)
            for t in TRANSITIONS
        )

    def test_every_transition_records_a_rationale(self) -> None:
        assert all(t.rationale.strip() for t in TRANSITIONS)


class TestAutomationCannotApprove:
    """Requirement 5: automated ingestion can only create 'discovered' entries."""

    def test_only_discovered_is_automation_creatable(self) -> None:
        assert frozenset({State.DISCOVERED}) == AUTOMATION_CREATABLE_STATES

    @pytest.mark.parametrize(
        "state", [State.SCREENED, State.APPROVED, State.REJECTED, State.EXPIRED, State.SUPERSEDED]
    )
    def test_automation_cannot_create_any_other_state(self, state: State) -> None:
        with pytest.raises(GovernanceError, match="may only create 'discovered'"):
            require_automation_creatable(state)

    @pytest.mark.parametrize("target", [State.SCREENED, State.APPROVED, State.REJECTED])
    def test_automation_cannot_drive_a_judgement_transition(self, target: State) -> None:
        # THE central rule. Automation cannot manufacture a clinical judgement
        # by driving the state machine.
        source = State.SCREENED if target is State.APPROVED else State.DISCOVERED
        with pytest.raises(GovernanceError, match="requires a human review record"):
            require_transition(source, target, actor=TransitionActor.AUTOMATION)

    def test_automation_may_apply_expiry(self) -> None:
        # A date passing is a fact, not a judgement.
        assert (
            require_transition(
                State.APPROVED, State.EXPIRED, actor=TransitionActor.AUTOMATION
            ).actor
            is TransitionActor.AUTOMATION
        )

    def test_automation_may_apply_supersession(self) -> None:
        assert require_transition(
            State.APPROVED, State.SUPERSEDED, actor=TransitionActor.AUTOMATION
        )

    def test_review_required_states_match_the_transition_table(self) -> None:
        # The two enforcement points must not drift apart.
        from_table = {t.target for t in TRANSITIONS if t.actor is TransitionActor.HUMAN_REVIEW}
        assert from_table == REVIEW_REQUIRED_STATES


class TestPackSourceInvariants:
    """Requirement 5: no approval without a complete review record."""

    def test_default_state_is_discovered(self) -> None:
        assert a_source().lifecycle_state is State.DISCOVERED

    def test_discovered_source_may_not_carry_a_review(self) -> None:
        with pytest.raises(ValidationError, match="must not carry a review record"):
            a_source(review=a_review())

    @pytest.mark.parametrize("state", [State.SCREENED, State.APPROVED, State.REJECTED])
    def test_judgement_states_require_a_review(self, state: State) -> None:
        with pytest.raises(ValidationError, match="requires a review record"):
            a_source(lifecycle_state=state, exclusion_reason="x")

    def test_approved_requires_an_approving_decision(self) -> None:
        with pytest.raises(ValidationError, match="decision is 'approved'"):
            a_source(lifecycle_state=State.APPROVED, review=a_review(ReviewDecision.NEEDS_CHANGES))

    def test_approved_requires_a_clinician(self) -> None:
        with pytest.raises(ValidationError, match="clinician reviewer"):
            a_source(
                lifecycle_state=State.APPROVED, review=a_review(role=ReviewerRole.NON_CLINICAL)
            )

    def test_fully_specified_approval_is_accepted(self) -> None:
        assert a_source(lifecycle_state=State.APPROVED, review=a_review()).is_approved

    def test_rejected_requires_an_exclusion_reason(self) -> None:
        with pytest.raises(ValidationError, match="exclusion_reason"):
            a_source(lifecycle_state=State.REJECTED, review=a_review(ReviewDecision.REJECTED))

    def test_superseded_requires_a_successor(self) -> None:
        with pytest.raises(ValidationError, match="superseded_by"):
            a_source(lifecycle_state=State.SUPERSEDED, exclusion_reason="replaced")

    def test_expired_requires_an_exclusion_reason(self) -> None:
        with pytest.raises(ValidationError, match="exclusion_reason"):
            a_source(lifecycle_state=State.EXPIRED)

    def test_canonical_url_must_be_http(self) -> None:
        # Rendered as a clickable citation, so other schemes are a link-injection surface.
        with pytest.raises(ValidationError, match="http"):
            a_source(canonical_url="javascript:alert(1)")

    def test_review_expiry_is_computed_against_a_date(self) -> None:
        source = a_source(lifecycle_state=State.APPROVED, review=a_review())
        assert source.review_expired_on(date(2027, 6, 1)) is True
        assert source.review_expired_on(date(2026, 6, 1)) is False


class TestClinicianReviewCompleteness:
    """Requirement 7: all six assessment dimensions, substantively answered."""

    @pytest.mark.parametrize(
        "field",
        [
            "relevance",
            "population_applicability",
            "evidence_quality",
            "known_limitations",
            "patient_safety_concerns",
            "decision_rationale",
        ],
    )
    def test_every_assessment_field_is_mandatory(self, field: str) -> None:
        payload = a_review().model_dump()
        del payload[field]
        with pytest.raises(ValidationError):
            ClinicianReview.model_validate(payload)

    @pytest.mark.parametrize("placeholder", ["n/a", "N/A", "-", "tbd", "TODO", "  ", "?"])
    def test_placeholder_answers_are_rejected(self, placeholder: str) -> None:
        # A template filled in with "-" would otherwise satisfy min_length and
        # produce a review that documents nothing.
        payload = a_review().model_dump()
        payload["evidence_quality"] = placeholder
        with pytest.raises(ValidationError):
            ClinicianReview.model_validate(payload)

    def test_none_identified_is_a_valid_safety_answer(self) -> None:
        payload = a_review().model_dump()
        payload["patient_safety_concerns"] = "None identified."
        assert ClinicianReview.model_validate(payload)

    @pytest.mark.parametrize(
        "reviewer_id", ["dr_smith", "rev_TOOLONG", "a.smith@hospital.org", "rev_123", ""]
    )
    def test_reviewer_identity_must_be_pseudonymous(self, reviewer_id: str) -> None:
        payload = a_review().model_dump()
        payload["reviewer_id"] = reviewer_id
        with pytest.raises(ValidationError):
            ClinicianReview.model_validate(payload)

    def test_review_due_may_not_precede_the_review(self) -> None:
        payload = a_review().model_dump()
        payload["review_due_at"] = date(2025, 1, 1)
        with pytest.raises(ValidationError, match="review_due_at"):
            ClinicianReview.model_validate(payload)


class TestManifestApprovalInvariants:
    """Requirement 5: a pack cannot report 'approved' without its evidence."""

    def test_default_state_is_draft(self) -> None:
        assert a_manifest().approval_state is PackApprovalState.DRAFT

    def test_approved_requires_all_three_approval_fields(self) -> None:
        with pytest.raises(ValidationError, match=r"approved_at|approved_by|review_due_at"):
            a_manifest(approval_state=PackApprovalState.APPROVED, source_count=1)

    def test_approved_requires_at_least_one_source(self) -> None:
        with pytest.raises(ValidationError, match="at least one source"):
            a_manifest(
                approval_state=PackApprovalState.APPROVED,
                approved_at=NOW,
                approved_by="rev_00000000",
                review_due_at=DUE,
                source_count=0,
            )

    def test_approval_metadata_forbidden_on_a_draft(self) -> None:
        # Lingering approval metadata would make a later reader believe a
        # decision exists.
        with pytest.raises(ValidationError, match="must be null unless"):
            a_manifest(approved_at=NOW)

    def test_fully_specified_approval_is_accepted(self) -> None:
        manifest = a_manifest(
            approval_state=PackApprovalState.APPROVED,
            approved_at=NOW,
            approved_by="rev_00000000",
            review_due_at=DUE,
            source_count=3,
        )
        assert manifest.approval_state is PackApprovalState.APPROVED

    def test_approver_must_be_pseudonymous(self) -> None:
        with pytest.raises(ValidationError, match="rev_"):
            a_manifest(
                approval_state=PackApprovalState.APPROVED,
                approved_at=NOW,
                approved_by="Dr Smith",
                review_due_at=DUE,
                source_count=1,
            )

    def test_superseded_requires_a_successor_version(self) -> None:
        with pytest.raises(ValidationError, match="superseded_by_version"):
            a_manifest(approval_state=PackApprovalState.SUPERSEDED)

    @pytest.mark.parametrize("version", ["1.0", "1", "v1.0.0", "1.0.0-rc1", "01.0.0"])
    def test_version_must_be_strict_semver(self, version: str) -> None:
        with pytest.raises(ValidationError, match="semantic version"):
            a_manifest(version=version)

    @pytest.mark.parametrize(
        "pack_id", ["Diabetes-PreVisit", "diabetes previsit", "diabetes_previsit", "-diabetes", ""]
    )
    def test_pack_id_must_be_kebab_case(self, pack_id: str) -> None:
        with pytest.raises(ValidationError, match="kebab-case"):
            a_manifest(pack_id=pack_id)

    def test_scope_requires_explicit_excluded_uses(self) -> None:
        # A scope that says only what a pack is for is not a scope: the
        # boundary is what makes a review decision possible.
        with pytest.raises(ValidationError):
            PackScope(
                intended_patient_population="Adults",
                intended_use="Education",
                excluded_uses=[],
                supported_topics=["x"],
            )

    def test_example_pack_is_flagged(self) -> None:
        assert a_manifest(approval_state=PackApprovalState.UNAPPROVED_EXAMPLE).is_example is True

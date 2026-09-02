"""
Schema validation tests.

Two families of check:

* structural — unknown fields, wrong types, missing required fields;
* invariant — cross-field rules, above all the governance rule that a document
  may not claim approval without an approval record.

The governance tests are the ones that matter most. The repository contains no
review metadata for any document, so every migrated record must be
``unreviewed``; these tests make it impossible for a future change to introduce
a code path that asserts otherwise.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from synapse.hashing import sha256_text
from synapse.normalize import normalize_text
from synapse.schemas import (
    AnswerClaim,
    AnswerDecision,
    ApprovalStatus,
    ClaimExcerpt,
    ClaimType,
    EvidenceChunk,
    RetractionStatus,
    ReviewDecision,
    ReviewerRole,
    SourceDocument,
    SourceReview,
    SourceType,
    StructuredAnswer,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DUE = date(2027, 1, 1)


def _clinician_review(decision: ReviewDecision = ReviewDecision.APPROVED) -> SourceReview:
    """A syntactically valid review record used to exercise the governance rules."""
    return SourceReview(
        reviewer_id="rev_a1b2c3d4",  # Pseudonymous placeholder; not a real person
        reviewer_role=ReviewerRole.CLINICIAN,
        reviewed_at=NOW,
        decision=decision,
        review_due_date=DUE,
        rubric_version="test-rubric-1",
    )


def _document(**overrides) -> SourceDocument:
    """Build a minimal valid SourceDocument, with overrides."""
    base = {
        "document_id": "pubmed:41802233",
        "source_type": SourceType.PUBMED_ABSTRACT,
        "source_url": "https://pubmed.ncbi.nlm.nih.gov/41802233/",
        "title": "A study title",
        "retrieved_at": NOW,
        "content_sha256": sha256_text("body text"),
        "source_pack_version": "sp-test",
    }
    return SourceDocument(**{**base, **overrides})


class TestStructuralValidation:
    """Unknown fields, wrong types and missing fields must all be rejected."""

    def test_unknown_field_is_rejected(self) -> None:
        # extra="forbid" is the reason this fails. A dataclass would silently
        # discard the field, so a corpus file could quietly lose data.
        with pytest.raises(ValidationError):
            _document(unexpected_field="surprise")

    def test_missing_required_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceDocument(document_id="pubmed:1")  # type: ignore[call-arg]

    def test_wrong_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _document(title=12345)

    def test_enum_value_outside_the_closed_vocabulary_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _document(approval_status="rubber-stamped")

    def test_models_are_immutable(self) -> None:
        # frozen=True: a validated record cannot be mutated into an invalid one.
        document = _document()
        with pytest.raises(ValidationError):
            document.title = "changed"  # type: ignore[misc]

    def test_naive_timestamp_is_rejected(self) -> None:
        # A naive timestamp is not verifiable provenance across machines.
        with pytest.raises(ValidationError):
            _document(retrieved_at=datetime(2026, 1, 1, 12, 0, 0))


class TestUrlSafety:
    """source_url is rendered as a clickable citation, so its scheme is constrained."""

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",  # Script execution in the patient-facing UI
            "file:///etc/passwd",  # Local file disclosure
            "data:text/html;base64,PHNjcmlwdD4=",  # Inline payload
            "ftp://example.org/paper.pdf",
            "pubmed.ncbi.nlm.nih.gov/1/",  # Scheme-relative
        ],
    )
    def test_non_http_urls_are_rejected(self, url: str) -> None:
        with pytest.raises(ValidationError):
            _document(source_url=url)

    def test_https_url_is_accepted(self) -> None:
        assert _document(source_url="https://example.org/x").source_url.startswith("https://")


class TestGovernanceInvariants:
    """No approval claims without approval records."""

    def test_default_status_is_unreviewed(self) -> None:
        assert _document().approval_status is ApprovalStatus.UNREVIEWED

    def test_default_retraction_status_is_unchecked_not_none(self) -> None:
        # 'unchecked' and 'none' are different claims: one says we did not look,
        # the other asserts a clean screening result.
        assert _document().retraction_status is RetractionStatus.UNCHECKED

    def test_approved_without_review_record_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requires a review record"):
            _document(approval_status=ApprovalStatus.APPROVED, review_due_date=DUE)

    def test_approved_with_non_approving_review_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"review\.decision"):
            _document(
                approval_status=ApprovalStatus.APPROVED,
                review=_clinician_review(ReviewDecision.NEEDS_CHANGES),
                review_due_date=DUE,
            )

    def test_approved_by_non_clinician_is_rejected(self) -> None:
        review = SourceReview(
            reviewer_id="rev_b2c3d4e5",
            reviewer_role=ReviewerRole.NON_CLINICAL,  # Non-clinical review cannot license clinical approval
            reviewed_at=NOW,
            decision=ReviewDecision.APPROVED,
            review_due_date=DUE,
            rubric_version="test-rubric-1",
        )
        with pytest.raises(ValidationError, match="clinician reviewer"):
            _document(approval_status=ApprovalStatus.APPROVED, review=review, review_due_date=DUE)

    def test_approved_without_expiry_is_rejected(self) -> None:
        # An approval with no due date silently becomes permanent.
        with pytest.raises(ValidationError, match="review_due_date"):
            _document(approval_status=ApprovalStatus.APPROVED, review=_clinician_review())

    def test_unreviewed_may_not_carry_a_review_record(self) -> None:
        # Blocks the half-populated record that would imply review by accident.
        with pytest.raises(ValidationError, match="must not carry a review record"):
            _document(approval_status=ApprovalStatus.UNREVIEWED, review=_clinician_review())

    def test_fully_specified_approval_is_accepted(self) -> None:
        document = _document(
            approval_status=ApprovalStatus.APPROVED, review=_clinician_review(), review_due_date=DUE
        )
        assert (
            document.approval_status is ApprovalStatus.APPROVED
        )  # The only path to 'approved' requires all four conditions

    def test_retraction_result_requires_a_screening_timestamp(self) -> None:
        with pytest.raises(ValidationError, match="retraction_checked_at"):
            _document(retraction_status=RetractionStatus.NONE)

    def test_retracted_document_is_not_citable(self) -> None:
        document = _document(
            retraction_status=RetractionStatus.RETRACTED, retraction_checked_at=NOW
        )
        assert document.is_citable is False


class TestReviewerPseudonymity:
    """Reviewer identity must never be committable."""

    @pytest.mark.parametrize(
        "reviewer_id",
        ["dr_smith", "rev_TOOLONGID", "rev_123", "a.smith@hospital.org", "rev_a1b2c3d"],
    )
    def test_non_pseudonymous_reviewer_ids_are_rejected(self, reviewer_id: str) -> None:
        with pytest.raises(ValidationError):
            SourceReview(
                reviewer_id=reviewer_id,
                reviewer_role=ReviewerRole.CLINICIAN,
                reviewed_at=NOW,
                decision=ReviewDecision.APPROVED,
                review_due_date=DUE,
                rubric_version="r1",
            )

    def test_email_address_in_comment_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not contain an '@'"):
            SourceReview(
                reviewer_id="rev_a1b2c3d4",
                reviewer_role=ReviewerRole.CLINICIAN,
                reviewed_at=NOW,
                decision=ReviewDecision.APPROVED,
                review_due_date=DUE,
                rubric_version="r1",
                comment="ask a.smith@hospital.org",
            )


class TestEvidenceChunkInvariants:
    """The identifier and content invariants that make drift unrepresentable."""

    def test_chunk_id_must_agree_with_document_id(self, make_chunk) -> None:
        payload = make_chunk(document_id="pubmed:41802233", ordinal=0).model_dump()
        payload["chunk_id"] = (
            "pubmed:99999999#0000"  # Identifier now names a different document than document_id
        )
        with pytest.raises(ValidationError, match="document component"):
            EvidenceChunk.model_validate(payload)

    def test_chunk_id_must_agree_with_ordinal(self, make_chunk) -> None:
        payload = make_chunk(ordinal=0).model_dump()
        payload["ordinal"] = 5  # Identifier still encodes 0
        with pytest.raises(ValidationError, match="ordinal component"):
            EvidenceChunk.model_validate(payload)

    def test_content_hash_must_match_text(self, make_chunk) -> None:
        # THE integrity invariant: text edited after hashing must not load.
        payload = make_chunk().model_dump()
        payload["text"] = normalize_text("Tampered text that was never hashed.")
        with pytest.raises(ValidationError, match="content_sha256 does not match"):
            EvidenceChunk.model_validate(payload)

    def test_unnormalised_text_is_rejected(self, make_chunk) -> None:
        # Rejecting rather than silently normalising: normalising here would
        # invalidate the caller's precomputed hash.
        payload = make_chunk().model_dump()
        payload["text"] = "  double  spaced  "
        with pytest.raises(ValidationError, match="not normalised"):
            EvidenceChunk.model_validate(payload)

    def test_inverted_span_is_rejected(self, make_chunk) -> None:
        payload = make_chunk().model_dump()
        payload["char_start"], payload["char_end"] = 100, 50
        with pytest.raises(ValidationError, match="char_end"):
            EvidenceChunk.model_validate(payload)

    def test_malformed_digest_shape_is_rejected(self, make_chunk) -> None:
        payload = make_chunk().model_dump()
        payload["content_sha256"] = "NOTAHASH"
        with pytest.raises(ValidationError, match="64 lowercase hexadecimal"):
            EvidenceChunk.model_validate(payload)

    def test_uppercase_digest_is_rejected(self, make_chunk) -> None:
        # Digests are compared with ==, so they must be canonically lowercase.
        payload = make_chunk().model_dump()
        payload["content_sha256"] = payload["content_sha256"].upper()
        with pytest.raises(ValidationError):
            EvidenceChunk.model_validate(payload)


class TestAnswerInvariants:
    """Citation-shaped invariants on structured answers."""

    def test_factual_claim_must_cite_a_source(self) -> None:
        with pytest.raises(ValidationError, match="must cite at least one source"):
            AnswerClaim(
                claim_id="c1",
                text="HbA1c reflects average glucose.",
                claim_type=ClaimType.FACTUAL,
                section="research",
            )

    def test_boundary_claim_need_not_cite(self) -> None:
        # The disclaimer and question card are correctly uncited; requiring
        # citations for them would make completeness unmeasurable.
        claim = AnswerClaim(
            claim_id="c1",
            text="This is not a diagnosis.",
            claim_type=ClaimType.BOUNDARY,
            section="boundary",
        )
        assert claim.source_ids == []

    def test_excerpt_must_belong_to_a_cited_source(self) -> None:
        with pytest.raises(ValidationError, match="listed in source_ids"):
            AnswerClaim(
                claim_id="c1",
                text="A claim.",
                claim_type=ClaimType.FACTUAL,
                section="research",
                source_ids=["pubmed:1"],
                excerpts=[
                    ClaimExcerpt(
                        chunk_id="pubmed:2#0000", document_id="pubmed:2", quote="unrelated"
                    )
                ],
            )

    def test_abstained_answer_may_not_assert_facts(self) -> None:
        with pytest.raises(ValidationError, match="must not contain factual claims"):
            StructuredAnswer(
                answer_id="a1",
                query_sha256="deadbeef",
                created_at=NOW,
                decision=AnswerDecision.ABSTAIN,
                abstained=True,
                abstain_reason="insufficient evidence",
                claims=[
                    AnswerClaim(
                        claim_id="c1",
                        text="A fact.",
                        claim_type=ClaimType.FACTUAL,
                        section="research",
                        source_ids=["pubmed:1"],
                    )
                ],
            )

    def test_abstention_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="abstain_reason"):
            StructuredAnswer(
                answer_id="a1",
                query_sha256="deadbeef",
                created_at=NOW,
                decision=AnswerDecision.ABSTAIN,
                abstained=True,
            )

    def test_duplicate_claim_ids_are_rejected(self) -> None:
        claim = AnswerClaim(
            claim_id="c1", text="x", claim_type=ClaimType.BOUNDARY, section="boundary"
        )
        with pytest.raises(ValidationError, match="unique"):
            StructuredAnswer(
                answer_id="a1",
                query_sha256="d",
                created_at=NOW,
                decision=AnswerDecision.PUBLISH,
                claims=[claim, claim],
            )

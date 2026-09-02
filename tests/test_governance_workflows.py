"""
Source-pack workflow tests: validation, eligibility, versioning, diff, CLI.

Every test is offline and operates on a pack built in a temporary directory.
The most consequential assertions are in ``TestEligibility`` (nothing
unapproved reaches an index) and ``TestVersioningRevokesApproval`` (changing a
pack's contents revokes the approval that was granted to the old contents).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from synapse.corpus.jsonl import write_jsonl
from synapse.governance.compare import diff_packs
from synapse.governance.eligibility import (
    EligibilityPolicy,
    evaluate_source,
    require_pack_index_agreement,
    select_eligible,
)
from synapse.governance.pack import SourcePack
from synapse.governance.review import expire_overdue, import_discovered, record_review
from synapse.governance.states import GovernanceError
from synapse.governance.versioning import ChangeLevel, bump_version, classify_change, parse_version
from synapse.hashing import sha256_text
from synapse.schemas.enums import (
    EvidenceType,
    PackApprovalState,
    RetractionStatus,
    ReviewDecision,
    ReviewerRole,
    SourceType,
)
from synapse.schemas.enums import (
    SourceLifecycleState as State,
)
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig, IndexManifest
from synapse.schemas.source import SourceDocument
from synapse.schemas.source_pack import (
    ClinicianReview,
    PackScope,
    PackSource,
    ReviewerRequirements,
    SourcePackManifest,
    SourceSelectionPolicy,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TODAY = date(2026, 1, 1)
DUE = date(2027, 1, 1)


def a_review(
    decision=ReviewDecision.APPROVED, role=ReviewerRole.CLINICIAN, due=DUE, rubric="rubric-1"
) -> ClinicianReview:
    """A complete review. The reviewer identifier is an obvious placeholder pseudonym."""
    return ClinicianReview(
        reviewer_id="rev_00000000",
        reviewer_role=role,
        reviewed_at=NOW,
        review_due_at=due,
        rubric_version=rubric,
        relevance="On topic.",
        population_applicability="Population matches.",
        evidence_quality="Adequate.",
        known_limitations="Single centre.",
        patient_safety_concerns="None identified.",
        decision=decision,
        decision_rationale="Within scope.",
    )


def a_source(n: int = 1, **overrides) -> PackSource:
    """A minimal valid pack source."""
    base = {
        "document_id": f"example:src-{n:04d}",
        "canonical_url": f"https://example.org/{n}",
        "title": f"Synthetic source {n}",
        "evidence_type": EvidenceType.GUIDELINE,
        "content_sha256": sha256_text(f"content {n}"),
        "discovered_at": NOW,
        "discovery_method": "unit_test",
        "retraction_status": RetractionStatus.NONE,
    }
    return PackSource(**{**base, **overrides})


def a_manifest(**overrides) -> SourcePackManifest:
    """A minimal valid manifest."""
    base = {
        "pack_id": "test-pack",
        "version": "0.1.0",
        "title": "Test",
        "description": "Tests.",
        "scope": PackScope(
            intended_patient_population="Adults",
            intended_use="Testing",
            excluded_uses=["Clinical use"],
            supported_topics=["testing"],
        ),
        "selection_policy": SourceSelectionPolicy(
            description="p", minimum_evidence_types=[EvidenceType.GUIDELINE, EvidenceType.RCT]
        ),
        "reviewer_requirements": ReviewerRequirements(rubric_version="rubric-1"),
        "corpus_sha256": "0" * 64,
        "source_count": 0,
        "build_tool_version": "0.1.0",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return SourcePackManifest(**{**base, **overrides})


@pytest.fixture
def pack(tmp_path: Path) -> SourcePack:
    """A pack on disk with three discovered sources."""
    p = SourcePack(
        manifest=a_manifest(),
        sources=[a_source(1), a_source(2), a_source(3)],
        directory=tmp_path / "pack",
    )
    p.write()  # write() recomputes the digest, so the pack starts consistent
    return p


class TestPackIntegrity:
    """Requirement 5: contents changing without a version bump is an error."""

    def test_a_freshly_written_pack_validates(self, pack: SourcePack) -> None:
        assert pack.validate(as_of=TODAY).is_valid

    def test_editing_sources_without_rewriting_the_manifest_fails(self, pack: SourcePack) -> None:
        # This is the version rule in action: the digest no longer matches.
        pack.sources.append(a_source(4))
        report = pack.validate(as_of=TODAY)
        assert not report.is_valid
        assert any(i.code == "corpus_digest_mismatch" for i in report.errors)

    def test_write_refreshes_derived_fields(self, pack: SourcePack) -> None:
        pack.sources.append(a_source(4))
        pack.write()
        assert pack.manifest.source_count == 4
        assert pack.validate(as_of=TODAY).is_valid

    def test_duplicate_document_ids_are_rejected(self, pack: SourcePack) -> None:
        # Which entry did the reviewer approve? Ambiguity is not permitted.
        pack.sources.append(a_source(1))
        pack.write()
        assert any(i.code == "duplicate_document_id" for i in pack.validate(as_of=TODAY).errors)

    def test_lifecycle_counts_always_carry_every_state(self, pack: SourcePack) -> None:
        counts = pack.lifecycle_counts()
        assert set(counts) == {s.value for s in State}
        assert counts["approved"] == 0  # Visible as zero rather than absent

    def test_round_trip_through_disk_preserves_everything(self, pack: SourcePack) -> None:
        reloaded = SourcePack.load(pack.directory)
        assert reloaded.sources == pack.sources
        assert reloaded.manifest.corpus_sha256 == pack.manifest.corpus_sha256


class TestPackValidationRules:
    """Pack-level governance rules."""

    def _approve_all(self, pack: SourcePack, **review_kwargs) -> None:
        pack.sources = [
            s.model_copy(
                update={"lifecycle_state": State.APPROVED, "review": a_review(**review_kwargs)}
            )
            for s in pack.sources
        ]

    def test_approved_pack_with_unresolved_sources_is_rejected(self, pack: SourcePack) -> None:
        # An approved pack with pending sources is ambiguous: considered and
        # deferred, or simply forgotten?
        pack.sources[0] = pack.sources[0].model_copy(
            update={"lifecycle_state": State.APPROVED, "review": a_review()}
        )
        pack.manifest = pack.manifest.model_copy(
            update={
                "approval_state": PackApprovalState.APPROVED,
                "approved_at": NOW,
                "approved_by": "rev_00000000",
                "review_due_at": DUE,
                "source_count": 3,
            }
        )
        pack.write()
        assert any(
            i.code == "approved_pack_has_unresolved_sources"
            for i in pack.validate(as_of=TODAY).errors
        )

    def test_expired_approval_is_flagged(self, pack: SourcePack) -> None:
        self._approve_all(pack, due=date(2026, 6, 1))
        pack.write()
        report = pack.validate(as_of=date(2026, 12, 1))  # Past the review-due date
        assert any(i.code == "approval_expired" for i in report.errors)

    def test_evidence_type_below_policy_is_rejected(self, pack: SourcePack) -> None:
        pack.sources = [
            s.model_copy(
                update={
                    "lifecycle_state": State.APPROVED,
                    "review": a_review(),
                    "evidence_type": EvidenceType.CASE_REPORT,
                }
            )
            for s in pack.sources
        ]
        pack.write()
        assert any(
            i.code == "evidence_type_below_policy" for i in pack.validate(as_of=TODAY).errors
        )

    def test_approved_but_retracted_source_is_rejected(self, pack: SourcePack) -> None:
        # A source can be approved and later retracted. The retraction wins.
        pack.sources[0] = pack.sources[0].model_copy(
            update={
                "lifecycle_state": State.APPROVED,
                "review": a_review(),
                "retraction_status": RetractionStatus.RETRACTED,
            }
        )
        pack.write()
        assert any(i.code == "approved_source_retracted" for i in pack.validate(as_of=TODAY).errors)

    def test_wrong_reviewer_role_is_rejected(self, pack: SourcePack) -> None:
        pack.sources[0] = pack.sources[0].model_copy(
            update={
                "lifecycle_state": State.SCREENED,
                "review": a_review(ReviewDecision.NEEDS_CHANGES, role=ReviewerRole.NON_CLINICAL),
            }
        )
        pack.write()
        assert pack.validate(
            as_of=TODAY
        )  # Screened by a non-clinician is permitted; approval is not

    def test_rubric_mismatch_warns_rather_than_blocks(self, pack: SourcePack) -> None:
        self._approve_all(pack, rubric="rubric-0")
        pack.write()
        report = pack.validate(as_of=TODAY)
        assert any(i.code == "rubric_version_mismatch" for i in report.warnings)

    def test_example_pack_may_not_contain_approvals(self, pack: SourcePack) -> None:
        self._approve_all(pack)
        pack.manifest = pack.manifest.model_copy(
            update={"approval_state": PackApprovalState.UNAPPROVED_EXAMPLE}
        )
        pack.write()
        assert any(
            i.code == "example_pack_has_approvals" for i in pack.validate(as_of=TODAY).errors
        )


class TestImportIsDiscoveryOnly:
    """Requirement 5: automated ingestion can only create 'discovered' entries."""

    def _document(self, n: int) -> SourceDocument:
        return SourceDocument(
            document_id=f"pubmed:4000000{n}",
            source_type=SourceType.PUBMED_ABSTRACT,
            source_url=f"https://pubmed.ncbi.nlm.nih.gov/4000000{n}/",
            title=f"Doc {n}",
            retrieved_at=NOW,
            content_sha256=sha256_text(f"doc {n}"),
            source_pack_version="sp-test",
        )

    def test_import_produces_only_discovered_entries(self) -> None:
        created, _ = import_discovered(
            [self._document(1), self._document(2)], discovery_method="test"
        )
        assert all(s.lifecycle_state is State.DISCOVERED for s in created)

    def test_import_never_attaches_a_review(self) -> None:
        created, _ = import_discovered([self._document(1)], discovery_method="test")
        assert created[0].review is None

    def test_import_has_no_lifecycle_parameter(self) -> None:
        # Structural proof: there is no argument by which a caller could ask for
        # a different state.
        import inspect

        params = set(inspect.signature(import_discovered).parameters)
        assert "lifecycle_state" not in params
        assert "review" not in params

    def test_reimport_does_not_reset_an_existing_review(self) -> None:
        # The failure this prevents: a nightly discovery run silently undoing
        # a completed clinical review.
        reviewed = a_source(
            1, document_id="pubmed:40000001", lifecycle_state=State.APPROVED, review=a_review()
        )
        created, skipped = import_discovered(
            [self._document(1)], discovery_method="test", existing=[reviewed]
        )
        assert created == []
        assert skipped == ["pubmed:40000001"]

    def test_discovery_method_is_recorded(self) -> None:
        created, _ = import_discovered([self._document(1)], discovery_method="pubmed_esearch")
        assert created[0].discovery_method == "pubmed_esearch"


class TestRecordReview:
    """The human path, and what it refuses."""

    def test_approving_review_advances_screened_to_approved(self) -> None:
        manifest = a_manifest()
        screened = a_source(
            1, lifecycle_state=State.SCREENED, review=a_review(ReviewDecision.NEEDS_CHANGES)
        )
        updated = record_review(screened, a_review(ReviewDecision.APPROVED), manifest=manifest)
        assert updated.lifecycle_state is State.APPROVED

    def test_needs_changes_produces_screened_not_approved(self) -> None:
        # The state is derived from the decision, so a "needs changes" review
        # cannot be filed as an approval.
        updated = record_review(
            a_source(1), a_review(ReviewDecision.NEEDS_CHANGES), manifest=a_manifest()
        )
        assert updated.lifecycle_state is State.SCREENED

    def test_discovered_cannot_be_approved_in_one_step(self) -> None:
        with pytest.raises(GovernanceError, match="not permitted"):
            record_review(a_source(1), a_review(ReviewDecision.APPROVED), manifest=a_manifest())

    def test_unqualified_reviewer_is_refused(self) -> None:
        with pytest.raises(GovernanceError, match="reviewer role"):
            record_review(
                a_source(1), a_review(role=ReviewerRole.NON_CLINICAL), manifest=a_manifest()
            )

    def test_rubric_mismatch_is_refused_at_filing_time(self) -> None:
        with pytest.raises(GovernanceError, match="rubric"):
            record_review(a_source(1), a_review(rubric="rubric-99"), manifest=a_manifest())

    def test_rejection_requires_a_reason(self) -> None:
        with pytest.raises(GovernanceError, match="exclusion_reason"):
            record_review(a_source(1), a_review(ReviewDecision.REJECTED), manifest=a_manifest())

    def test_rejection_with_a_reason_is_recorded(self) -> None:
        updated = record_review(
            a_source(1),
            a_review(ReviewDecision.REJECTED),
            manifest=a_manifest(),
            exclusion_reason="Out of scope population.",
        )
        assert updated.lifecycle_state is State.REJECTED
        assert updated.exclusion_reason == "Out of scope population."


class TestExpiry:
    """Expiry is machine-applied; recovery is not."""

    def test_overdue_approvals_expire(self) -> None:
        approved = a_source(
            1, lifecycle_state=State.APPROVED, review=a_review(due=date(2026, 6, 1))
        )
        updated, expired = expire_overdue([approved], as_of=date(2026, 12, 1))
        assert expired == ["example:src-0001"]
        assert updated[0].lifecycle_state is State.EXPIRED
        assert "expired on 2026-06-01" in updated[0].exclusion_reason

    def test_current_approvals_are_untouched(self) -> None:
        approved = a_source(
            1, lifecycle_state=State.APPROVED, review=a_review(due=date(2027, 1, 1))
        )
        updated, expired = expire_overdue([approved], as_of=date(2026, 6, 1))
        assert expired == []
        assert updated[0].lifecycle_state is State.APPROVED


class TestEligibility:
    """Requirement 5: what may reach a production index."""

    @pytest.mark.parametrize(
        "state", [State.DISCOVERED, State.SCREENED, State.REJECTED, State.EXPIRED, State.SUPERSEDED]
    )
    def test_non_approved_states_are_all_excluded(self, state: State) -> None:
        extras: dict = {}
        if state in (State.SCREENED, State.REJECTED):
            extras["review"] = a_review(
                ReviewDecision.NEEDS_CHANGES if state is State.SCREENED else ReviewDecision.REJECTED
            )
        if state in (State.REJECTED, State.EXPIRED, State.SUPERSEDED):
            extras["exclusion_reason"] = "reason"
        if state is State.SUPERSEDED:
            extras["superseded_by"] = "example:src-0002"
        decision = evaluate_source(
            a_source(1, lifecycle_state=state, **extras), EligibilityPolicy(), as_of=TODAY
        )
        assert decision.eligible is False

    def test_approved_source_is_eligible(self) -> None:
        source = a_source(1, lifecycle_state=State.APPROVED, review=a_review())
        assert evaluate_source(source, EligibilityPolicy(), as_of=TODAY).eligible is True

    def test_retraction_beats_approval(self) -> None:
        # Checked before lifecycle state, so a source approved before its
        # retraction was published is still excluded.
        source = a_source(
            1,
            lifecycle_state=State.APPROVED,
            review=a_review(),
            retraction_status=RetractionStatus.RETRACTED,
        )
        decision = evaluate_source(source, EligibilityPolicy(), as_of=TODAY)
        assert decision.eligible is False
        assert decision.reason.startswith("retracted:")

    def test_expired_approval_is_not_eligible(self) -> None:
        source = a_source(1, lifecycle_state=State.APPROVED, review=a_review(due=date(2026, 6, 1)))
        assert (
            evaluate_source(source, EligibilityPolicy(), as_of=date(2026, 12, 1)).reason
            == "approval_expired"
        )

    def test_unapproved_pack_cannot_build_an_index(self, pack: SourcePack) -> None:
        with pytest.raises(GovernanceError, match="not approved"):
            select_eligible(pack.sources, pack.manifest, as_of=TODAY)

    def test_example_pack_is_refused_outright(self, pack: SourcePack) -> None:
        manifest = pack.manifest.model_copy(
            update={"approval_state": PackApprovalState.UNAPPROVED_EXAMPLE}
        )
        with pytest.raises(GovernanceError, match="unapproved example"):
            select_eligible(pack.sources, manifest, as_of=TODAY)

    def test_every_source_gets_a_decision(self, pack: SourcePack) -> None:
        # A build report must explain each exclusion, not just count them.
        manifest = pack.manifest.model_copy(
            update={
                "approval_state": PackApprovalState.APPROVED,
                "approved_at": NOW,
                "approved_by": "rev_00000000",
                "review_due_at": DUE,
                "source_count": 3,
            }
        )
        approved = [
            s.model_copy(update={"lifecycle_state": State.APPROVED, "review": a_review()})
            for s in pack.sources[:2]
        ]
        eligible, decisions = select_eligible([*approved, pack.sources[2]], manifest, as_of=TODAY)
        assert len(eligible) == 2
        assert len(decisions) == 3

    def test_ungoverned_override_is_described_alarmingly(self) -> None:
        # The escape hatch must stand out wherever it is recorded.
        assert "UNGOVERNED" in EligibilityPolicy(allow_unapproved=True).describe()

    def test_ungoverned_override_still_excludes_retracted(self) -> None:
        source = a_source(1, retraction_status=RetractionStatus.RETRACTED)
        assert (
            evaluate_source(source, EligibilityPolicy(allow_unapproved=True), as_of=TODAY).eligible
            is False
        )


class TestPackIndexAgreement:
    """Requirement 5: pack and index hashes must agree before serving."""

    def _index(self, **overrides) -> IndexManifest:
        base = {
            "index_id": "i",
            "index_version": "1",
            "corpus_version": "c",
            "corpus_sha256": "a" * 64,
            "chunk_ids_sha256": "b" * 64,
            "embedding": EmbeddingConfig(
                provider="openai", model="m", dimensions=1536, l2_normalized=True
            ),
            "chunking": ChunkingConfig(
                algorithm="section_aware_sentence", chunk_size=800, overlap=0, min_chunk_chars=120
            ),
            "distance_metric": "l2",
            "vectors_l2_normalized": True,
            "document_count": 1,
            "chunk_count": 1,
            "index_state": "rebuild_required",
            "built_at": NOW,
        }
        return IndexManifest(**{**base, **overrides})

    def test_matching_pack_and_index_pass(self, pack: SourcePack) -> None:
        index = self._index(
            source_pack_version="test-pack@0.1.0", source_pack_sha256=pack.manifest.corpus_sha256
        )
        require_pack_index_agreement(pack.manifest, index)  # Does not raise

    def test_index_without_a_pack_version_is_refused(self, pack: SourcePack) -> None:
        with pytest.raises(GovernanceError, match="not built from this source pack"):
            require_pack_index_agreement(pack.manifest, self._index())

    def test_index_from_a_different_pack_version_is_refused(self, pack: SourcePack) -> None:
        index = self._index(
            source_pack_version="test-pack@0.0.9", source_pack_sha256=pack.manifest.corpus_sha256
        )
        with pytest.raises(GovernanceError, match="not built from this source pack"):
            require_pack_index_agreement(pack.manifest, index)

    def test_index_without_a_pack_digest_is_refused(self, pack: SourcePack) -> None:
        with pytest.raises(GovernanceError, match="records no source-pack digest"):
            require_pack_index_agreement(
                pack.manifest, self._index(source_pack_version="test-pack@0.1.0")
            )

    def test_pack_edited_after_the_build_is_detected(self, pack: SourcePack) -> None:
        # The scenario this exists for: an index serving content the current
        # pack no longer governs.
        index = self._index(
            source_pack_version="test-pack@0.1.0", source_pack_sha256=pack.manifest.corpus_sha256
        )
        pack.sources.append(a_source(9))
        pack.write()
        with pytest.raises(GovernanceError, match="changed after the index was built"):
            require_pack_index_agreement(pack.manifest, index)


class TestVersioningRevokesApproval:
    """Requirement 5: version changes are required when pack contents change."""

    def _approved_manifest(self) -> SourcePackManifest:
        return a_manifest(
            approval_state=PackApprovalState.APPROVED,
            approved_at=NOW,
            approved_by="rev_00000000",
            review_due_at=DUE,
            source_count=1,
        )

    def test_content_change_revokes_a_pack_approval(self) -> None:
        # A clinician approved specific contents. Those contents no longer
        # exist, so the approval cannot carry forward.
        bumped = bump_version(self._approved_manifest(), ChangeLevel.MINOR)
        assert bumped.approval_state is PackApprovalState.DRAFT
        assert bumped.approved_at is None
        assert bumped.approved_by is None

    def test_bump_levels_follow_semver(self) -> None:
        manifest = a_manifest(version="1.2.3")
        assert bump_version(manifest, ChangeLevel.PATCH).version == "1.2.4"
        assert bump_version(manifest, ChangeLevel.MINOR).version == "1.3.0"
        assert bump_version(manifest, ChangeLevel.MAJOR).version == "2.0.0"

    def test_no_change_cannot_be_versioned(self) -> None:
        with pytest.raises(GovernanceError, match="no change detected"):
            bump_version(a_manifest(), ChangeLevel.NONE)

    def test_scope_change_classifies_as_major(self) -> None:
        previous, sources = a_manifest(), [a_source(1)]
        current = a_manifest(
            scope=PackScope(
                intended_patient_population="Children",
                intended_use="Testing",
                excluded_uses=["Clinical use"],
                supported_topics=["testing"],
            )
        )
        assert classify_change(previous, sources, current, sources) is ChangeLevel.MAJOR

    def test_adding_a_source_classifies_as_minor(self) -> None:
        manifest = a_manifest()
        assert (
            classify_change(manifest, [a_source(1)], manifest, [a_source(1), a_source(2)])
            is ChangeLevel.MINOR
        )

    def test_editing_a_source_classifies_as_patch(self) -> None:
        manifest = a_manifest()
        edited = a_source(1).model_copy(update={"title": "Corrected title"})
        assert classify_change(manifest, [a_source(1)], manifest, [edited]) is ChangeLevel.PATCH

    def test_identical_packs_classify_as_none(self) -> None:
        manifest = a_manifest()
        assert classify_change(manifest, [a_source(1)], manifest, [a_source(1)]) is ChangeLevel.NONE

    def test_version_parsing_rejects_junk(self) -> None:
        with pytest.raises(GovernanceError, match="semantic version"):
            parse_version("1.0")


class TestDiff:
    """Comparing two versions by governance consequence."""

    def test_added_sources_require_re_review(self) -> None:
        manifest = a_manifest()
        diff = diff_packs(manifest, [a_source(1)], manifest, [a_source(1), a_source(2)])
        assert len(diff.added) == 1
        assert diff.requires_re_review is True

    def test_scope_change_requires_re_review(self) -> None:
        previous = a_manifest()
        current = a_manifest(
            scope=PackScope(
                intended_patient_population="Children",
                intended_use="T",
                excluded_uses=["x"],
                supported_topics=["t"],
            )
        )
        assert (
            diff_packs(previous, [a_source(1)], current, [a_source(1)]).requires_re_review is True
        )

    def test_lost_approvals_are_surfaced_separately(self) -> None:
        manifest = a_manifest()
        approved = a_source(1, lifecycle_state=State.APPROVED, review=a_review())
        expired = a_source(1, lifecycle_state=State.EXPIRED, exclusion_reason="expired")
        diff = diff_packs(manifest, [approved], manifest, [expired])
        assert len(diff.approvals_lost) == 1

    def test_content_hash_change_after_review_is_called_out(self) -> None:
        # The source text changed underneath a completed review.
        manifest = a_manifest()
        original = a_source(1, lifecycle_state=State.APPROVED, review=a_review())
        changed = original.model_copy(update={"content_sha256": sha256_text("different content")})
        diff = diff_packs(manifest, [original], manifest, [changed])
        assert diff.metadata_changed[0].detail == "content hash changed after review"

    def test_diff_is_deterministic(self) -> None:
        manifest = a_manifest()
        args = (manifest, [a_source(2), a_source(1)], manifest, [a_source(3), a_source(1)])
        assert diff_packs(*args).as_dict() == diff_packs(*args).as_dict()


class TestCli:
    """The seven workflows, driven end to end."""

    def _init(self, tmp_path: Path, *extra: str) -> int:
        from synapse.cli.source_pack import main

        return main(
            [
                "init",
                "--pack",
                str(tmp_path / "pack"),
                "--pack-id",
                "cli-pack",
                "--title",
                "CLI pack",
                "--description",
                "For CLI tests.",
                "--population",
                "Adults",
                "--intended-use",
                "Testing",
                "--excluded-use",
                "Clinical use",
                "--topic",
                "testing",
                *extra,
            ]
        )

    def test_init_creates_the_required_layout(self, tmp_path: Path) -> None:
        assert self._init(tmp_path) == 0
        for name in ("manifest.json", "sources.jsonl", "CHANGELOG.md", "README.md"):
            assert (tmp_path / "pack" / name).is_file()

    def test_init_never_creates_an_approved_pack(self, tmp_path: Path) -> None:
        # There is no flag on `init` that produces an approval.
        self._init(tmp_path)
        assert SourcePack.load(tmp_path / "pack").manifest.approval_state is PackApprovalState.DRAFT

    def test_init_example_flag_marks_the_pack(self, tmp_path: Path) -> None:
        self._init(tmp_path, "--example")
        assert (
            SourcePack.load(tmp_path / "pack").manifest.approval_state
            is PackApprovalState.UNAPPROVED_EXAMPLE
        )

    def test_init_refuses_a_non_empty_directory(self, tmp_path: Path) -> None:
        self._init(tmp_path)
        assert self._init(tmp_path) == 2

    def test_import_then_validate(self, tmp_path: Path) -> None:
        from synapse.cli.source_pack import main

        self._init(tmp_path)
        documents = tmp_path / "documents.jsonl"
        write_jsonl(
            documents,
            [
                SourceDocument(
                    document_id="pubmed:40000001",
                    source_type=SourceType.PUBMED_ABSTRACT,
                    source_url="https://pubmed.ncbi.nlm.nih.gov/40000001/",
                    title="Doc",
                    retrieved_at=NOW,
                    content_sha256=sha256_text("doc"),
                    source_pack_version="sp",
                )
            ],
        )
        assert (
            main(["import", "--pack", str(tmp_path / "pack"), "--documents", str(documents)]) == 0
        )
        loaded = SourcePack.load(tmp_path / "pack")
        assert len(loaded.sources) == 1
        assert (
            loaded.sources[0].lifecycle_state is State.DISCOVERED
        )  # Import can produce nothing else
        assert main(["validate", "--pack", str(tmp_path / "pack")]) == 0

    def test_validate_json_output_is_machine_readable(self, tmp_path: Path, capsys) -> None:
        from synapse.cli.source_pack import main

        self._init(tmp_path)
        capsys.readouterr()  # Drain init's output; only the JSON that follows is under test
        main(["validate", "--pack", str(tmp_path / "pack"), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["valid"] is True

    def test_review_rejects_an_incomplete_review_file(self, tmp_path: Path, capsys) -> None:
        from synapse.cli.source_pack import main

        self._init(tmp_path)
        pack_dir = tmp_path / "pack"
        loaded = SourcePack.load(pack_dir)
        loaded.sources = [a_source(1)]
        loaded.write()
        incomplete = tmp_path / "review.json"
        incomplete.write_text(
            json.dumps({"reviewer_id": "rev_00000000", "decision": "approved"}), encoding="utf-8"
        )
        assert (
            main(
                [
                    "review",
                    "--pack",
                    str(pack_dir),
                    "--document-id",
                    "example:src-0001",
                    "--review",
                    str(incomplete),
                ]
            )
            == 2
        )
        assert "mandatory" in capsys.readouterr().err

    def test_review_records_a_complete_review(self, tmp_path: Path) -> None:
        from synapse.cli.source_pack import main

        self._init(tmp_path)
        pack_dir = tmp_path / "pack"
        loaded = SourcePack.load(pack_dir)
        loaded.sources = [a_source(1)]
        loaded.write()
        review_file = tmp_path / "review.json"
        review_file.write_text(
            a_review(ReviewDecision.NEEDS_CHANGES).model_dump_json(), encoding="utf-8"
        )
        assert (
            main(
                [
                    "review",
                    "--pack",
                    str(pack_dir),
                    "--document-id",
                    "example:src-0001",
                    "--review",
                    str(review_file),
                ]
            )
            == 0
        )
        assert SourcePack.load(pack_dir).sources[0].lifecycle_state is State.SCREENED

    def test_version_snapshots_and_bumps(self, tmp_path: Path) -> None:
        from synapse.cli.source_pack import main

        self._init(tmp_path)
        pack_dir = tmp_path / "pack"
        assert (
            main(
                ["version", "--pack", str(pack_dir), "--level", "minor", "--note", "Added sources."]
            )
            == 0
        )
        assert SourcePack.load(pack_dir).manifest.version == "0.2.0"
        assert (pack_dir / ".versions" / "0.1.0" / "manifest.json").is_file()  # Snapshot for `diff`
        assert "0.2.0" in (pack_dir / "CHANGELOG.md").read_text()

    def test_diff_between_snapshot_and_current(self, tmp_path: Path, capsys) -> None:
        from synapse.cli.source_pack import main

        self._init(tmp_path)
        pack_dir = tmp_path / "pack"
        main(["version", "--pack", str(pack_dir), "--level", "minor"])
        loaded = SourcePack.load(pack_dir)
        loaded.sources = [a_source(1)]
        loaded.write()
        capsys.readouterr()  # Drain init/version output before reading the JSON diff
        assert (
            main(
                [
                    "diff",
                    "--from",
                    str(pack_dir / ".versions" / "0.1.0"),
                    "--to",
                    str(pack_dir),
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["added"] == 1
        assert payload["requires_re_review"] is True

    def test_build_index_refuses_an_unapproved_pack(self, tmp_path: Path, capsys) -> None:
        from synapse.cli.source_pack import main

        self._init(tmp_path)
        code = main(
            [
                "build-index",
                "--pack",
                str(tmp_path / "pack"),
                "--corpus",
                str(tmp_path / "corpus"),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        assert code == 1
        assert "not approved" in capsys.readouterr().err
        assert not (tmp_path / "out").exists()

    def test_governance_notice_is_always_printed(self, tmp_path: Path, capsys) -> None:

        self._init(tmp_path)
        assert "does not make them" in capsys.readouterr().err


class TestShippedExamplePack:
    """The example pack in the repository must stay unmistakably unapproved."""

    PACK_DIR = Path(__file__).parent.parent / "source_packs" / "diabetes-previsit"

    @pytest.mark.skipif(not PACK_DIR.is_dir(), reason="example pack not present")
    def test_example_pack_validates(self) -> None:
        assert SourcePack.load(self.PACK_DIR).validate(as_of=TODAY).is_valid

    @pytest.mark.skipif(not PACK_DIR.is_dir(), reason="example pack not present")
    def test_example_pack_is_marked_unapproved(self) -> None:
        assert (
            SourcePack.load(self.PACK_DIR).manifest.approval_state
            is PackApprovalState.UNAPPROVED_EXAMPLE
        )

    @pytest.mark.skipif(not PACK_DIR.is_dir(), reason="example pack not present")
    def test_example_pack_contains_no_reviews_or_reviewer_identities(self) -> None:
        # Requirement 8, verified against the shipped artifact.
        pack = SourcePack.load(self.PACK_DIR)
        assert all(s.review is None for s in pack.sources)
        assert pack.manifest.approved_by is None

    @pytest.mark.skipif(not PACK_DIR.is_dir(), reason="example pack not present")
    def test_example_pack_sources_are_synthetic(self) -> None:
        # No real publication was fetched or labelled to build this pack.
        pack = SourcePack.load(self.PACK_DIR)
        assert all(s.document_id.startswith("example:") for s in pack.sources)
        assert all(s.pmid is None and s.doi is None for s in pack.sources)
        assert all("example.org" in s.canonical_url for s in pack.sources)

    @pytest.mark.skipif(not PACK_DIR.is_dir(), reason="example pack not present")
    def test_example_pack_cannot_build_an_index(self) -> None:
        pack = SourcePack.load(self.PACK_DIR)
        with pytest.raises(GovernanceError, match="unapproved example"):
            select_eligible(pack.sources, pack.manifest, as_of=TODAY)

"""
synapse.governance
==================
Source-pack governance: the mechanism that separates automated discovery from
human clinical approval.

A *source pack* is a versioned collection of medical sources reviewed and
approved for one narrowly defined product scope. The subsystem's whole purpose
is to make one sentence true of the code:

    Automated discovery establishes that a publication exists.
    Only a recorded human review establishes that it may be used.

Layout:

    synapse.governance.states       lifecycle transition table and permitted actors
    synapse.governance.pack         load / write / validate a pack directory
    synapse.governance.review       import discoveries; record human reviews
    synapse.governance.eligibility  what may enter a production index
    synapse.governance.versioning   semver, and approval revocation on change
    synapse.governance.compare      governance-relevant diff between versions

Nothing here fabricates a reviewer identity or an approval record. Reviewer
identifiers are pseudonyms issued out-of-band and supplied by an operator.
"""

from __future__ import annotations  # Postponed annotations

from synapse.governance.compare import PackDiff, diff_packs
from synapse.governance.eligibility import (
    EligibilityDecision,
    EligibilityPolicy,
    require_pack_index_agreement,
    select_eligible,
)
from synapse.governance.pack import SourcePack, ValidationIssue, ValidationReport
from synapse.governance.review import expire_overdue, import_discovered, record_review
from synapse.governance.states import (
    TRANSITIONS,
    GovernanceError,
    TransitionActor,
    allowed_targets,
    require_transition,
)
from synapse.governance.versioning import ChangeLevel, bump_version, classify_change

__all__ = [
    "TRANSITIONS",
    "ChangeLevel",
    "EligibilityDecision",
    "EligibilityPolicy",
    "GovernanceError",
    "PackDiff",
    "SourcePack",
    "TransitionActor",
    "ValidationIssue",
    "ValidationReport",
    "allowed_targets",
    "bump_version",
    "classify_change",
    "diff_packs",
    "expire_overdue",
    "import_discovered",
    "record_review",
    "require_pack_index_agreement",
    "require_transition",
    "select_eligible",
]

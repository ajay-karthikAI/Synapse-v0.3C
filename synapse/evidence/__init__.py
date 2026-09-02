"""
synapse.evidence
================
The patient-facing evidence experience: what supports a claim, and how good it is.

    synapse.evidence.metadata      publication and governance fields, read from a pack
    synapse.evidence.labels        plain-language names and explanations
    synapse.evidence.links         HTTPS-only external links, validated identifiers
    synapse.evidence.insufficient  the "not enough verified information" state
    synapse.evidence.render        accessible citation controls and evidence cards

Three rules run through all of it:

1. **Nothing is invented.** Every metadata field is copied from a governed
   :class:`~synapse.schemas.source_pack.PackSource` record, every excerpt comes
   from P1-validated evidence, and a missing field renders as missing.
2. **Relevance is not quality.** A reranker's relevance figure is labelled
   "retrieval relevance" with its own explanation and never sits beside the
   review state. Quality and review labels come from governed metadata alone.
3. **The gap is never filled.** When there is no verified evidence, the state is
   composed from fixed copy that no model can contribute to.

The dependency direction is ``synapse.answer.render`` → ``synapse.evidence``;
this package imports the answer layer's escaping primitive and its schemas, and
nothing else from it.
"""

from __future__ import annotations  # Postponed annotations

from synapse.evidence.insufficient import (
    InsufficientEvidence,
    InsufficientReason,
    from_failure_code,
)
from synapse.evidence.labels import (
    RELEVANCE_EXPLANATION,
    RELEVANCE_LABEL,
    Label,
    describe_evidence_type,
    describe_retraction,
    describe_review_state,
)
from synapse.evidence.links import canonical_link, doi_url, is_safe_url, pmid_url, safe_url
from synapse.evidence.metadata import MetadataResolver, SourceMetadata
from synapse.evidence.render import (
    EvidenceExcerpt,
    card_dom_id,
    claim_dom_id,
    render_citation_control,
    render_evidence_card,
    render_insufficient_html,
    render_review_evidence_card,
)

__all__ = [
    "RELEVANCE_EXPLANATION",
    "RELEVANCE_LABEL",
    "EvidenceExcerpt",
    "InsufficientEvidence",
    "InsufficientReason",
    "Label",
    "MetadataResolver",
    "SourceMetadata",
    "canonical_link",
    "card_dom_id",
    "claim_dom_id",
    "describe_evidence_type",
    "describe_retraction",
    "describe_review_state",
    "doi_url",
    "from_failure_code",
    "is_safe_url",
    "pmid_url",
    "render_citation_control",
    "render_evidence_card",
    "render_insufficient_html",
    "render_review_evidence_card",
    "safe_url",
]

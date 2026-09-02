"""
synapse.ingest.evidence
=======================
Normalised evidence-type classification from raw PubMed publication types.

PubMed assigns each record a *list* of publication types, frequently several
at once: a systematic review of trials is commonly tagged ``Meta-Analysis``,
``Systematic Review``, ``Review`` and ``Journal Article`` simultaneously.
Retrieval and future evidence-hierarchy policy need one normalised class, so a
deterministic precedence order is required — picking whichever type happens to
appear first in the XML would classify the same article differently depending
on publisher formatting.

**The raw list is never discarded.** ``SourceDocument.publication_types`` keeps
every upstream string verbatim, so a future re-classification can run against
the original metadata rather than against this mapping's output.

**This is not a clinical judgement.** The mapping is mechanical, derived from
publisher-assigned metadata, and is recorded with
``EvidenceTypeProvenance.PUBLISHER_METADATA`` so it can never be mistaken for a
curated label. A record with no recognised type is left ``UNCLASSIFIED`` with
provenance ``UNKNOWN`` rather than being guessed into a category.
"""

from __future__ import annotations  # Postponed annotations

from synapse.schemas.enums import EvidenceType, EvidenceTypeProvenance

# Precedence order, strongest evidence first. The first entry whose trigger set
# intersects a record's publication types wins. Order encodes the conventional
# evidence hierarchy: synthesised guidance above pooled analyses, pooled
# analyses above single trials, trials above observational designs, and
# narrative review last so it never outranks a study design.
EVIDENCE_PRECEDENCE: tuple[tuple[EvidenceType, frozenset[str]], ...] = (
    (
        EvidenceType.GUIDELINE,
        frozenset(
            {
                "practice guideline",
                "guideline",
                "consensus development conference",
                "consensus development conference, nih",
            }
        ),
    ),
    (
        EvidenceType.META_ANALYSIS,
        frozenset({"meta-analysis"}),
    ),
    (
        EvidenceType.SYSTEMATIC_REVIEW,
        frozenset({"systematic review"}),
    ),
    (
        EvidenceType.RCT,
        frozenset(
            {
                "randomized controlled trial",
                "controlled clinical trial",
                "pragmatic clinical trial",
                "clinical trial, phase iii",
                "clinical trial, phase iv",
                "equivalence trial",
            }
        ),
    ),
    (
        EvidenceType.COHORT,
        frozenset(
            {"observational study", "multicenter study"}
        ),  # PubMed has no 'cohort' type; these are the closest observational markers
    ),
    (
        EvidenceType.CASE_REPORT,
        frozenset({"case reports"}),
    ),
    (
        EvidenceType.NARRATIVE_REVIEW,
        frozenset(
            {"review", "scoping review"}
        ),  # Last among study designs: 'Review' co-occurs with almost everything
    ),
    (
        EvidenceType.PREPRINT,
        frozenset({"preprint"}),
    ),
)

# Types that describe a record's *administrative* role rather than its study
# design. They must not classify a record on their own, or every journal
# article would land in the same bucket.
_NON_DESIGN_TYPES = frozenset(
    {
        "journal article",
        "english abstract",
        "editorial",
        "letter",
        "comment",
        "published erratum",
        "retraction of publication",
        "retracted publication",
        "expression of concern",
        "historical article",
        "introductory journal article",
        "video-audio media",
        "portrait",
        "biography",
        "news",
    }
)


def classify_evidence_type(
    publication_types: list[str],
) -> tuple[EvidenceType, EvidenceTypeProvenance]:
    """Map raw PubMed publication types to one normalised evidence type.

    Returns the type together with its provenance, which is mandatory
    everywhere in Synapse so a mechanical mapping is never read as a clinical
    assessment.

    Deterministic: the same input list, in any order, always produces the same
    output, because precedence is applied over a set rather than over the
    input sequence.
    """
    lowered = {value.strip().lower() for value in publication_types if value.strip()}
    if (
        not lowered
    ):  # No metadata at all — say so rather than defaulting to a plausible-looking class
        return EvidenceType.UNCLASSIFIED, EvidenceTypeProvenance.UNKNOWN

    for (
        evidence_type,
        triggers,
    ) in EVIDENCE_PRECEDENCE:  # Fixed precedence, independent of input ordering
        if lowered & triggers:
            return evidence_type, EvidenceTypeProvenance.PUBLISHER_METADATA

    if lowered - _NON_DESIGN_TYPES:  # Recognised types that carry no design information
        return EvidenceType.OTHER, EvidenceTypeProvenance.PUBLISHER_METADATA
    if lowered:  # Only administrative types, e.g. a bare "Journal Article"
        return EvidenceType.OTHER, EvidenceTypeProvenance.PUBLISHER_METADATA
    return EvidenceType.UNCLASSIFIED, EvidenceTypeProvenance.UNKNOWN


__all__ = ["EVIDENCE_PRECEDENCE", "classify_evidence_type"]

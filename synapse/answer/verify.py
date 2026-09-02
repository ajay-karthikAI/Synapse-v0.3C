"""
synapse.answer.verify
=====================
Claim verification: does the evidence actually say this?

Every check here is **deterministic and offline**. A model cannot be trusted to
report whether it is grounded — that is the property under test — so nothing in
this module asks it. Verification is string comparison and set membership.

The order of checks is deliberate, cheapest-and-most-decisive first, because
each answers a different question about a failure:

1. **Does the cited source exist in what was retrieved?** A citation to a
   document the model was never shown is a fabrication, not a mistake.
2. **Does the cited chunk exist, and belong to that source?**
3. **Is the quote actually in that chunk**, after documented normalisation?
4. **Do the numbers in the claim appear in the cited text?**

Only the last is a judgement call about degree; the first three are yes/no.

Normalisation (documented, because requirement 3 turns on it)
-------------------------------------------------------------
Both the quote and the chunk are put through
:func:`synapse.normalize.normalize_text` — NFC composition, whitespace runs
collapsed to a single space, ends trimmed — and then lowercased. Nothing else
is done: no stemming, no punctuation stripping, no stopword removal.

That combination is chosen so a *correct* citation survives the model reflowing
a line or changing a quote's capitalisation, while a *fabricated* one still
fails. Stripping punctuation would let "below 7%" match "below 7", which is
precisely the class of error this exists to catch.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from synapse.answer.schema import GroundedAnswer, GroundedClaim, SupportStatus
from synapse.evals.metrics.answer import extract_numbers
from synapse.logging import get_logger
from synapse.normalize import normalize_text

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetrievedEvidence:
    """The evidence a model was actually shown.

    Verification is defined entirely against this: anything cited that is not
    in here was invented, whatever it looks like.
    """

    chunk_texts: Mapping[str, str]  # chunk_id -> chunk text
    source_ids: frozenset[str]  # document identifiers that were retrieved
    source_titles: Mapping[str, str] = field(default_factory=dict)  # For the source panel
    source_urls: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_chunks(
        cls,
        chunks: list,
        titles: Mapping[str, str] | None = None,
        urls: Mapping[str, str] | None = None,
    ) -> RetrievedEvidence:
        """Build from a list of chunk-like objects carrying ``chunk_id``, ``document_id`` and ``text``."""
        return cls(
            chunk_texts={chunk.chunk_id: chunk.text for chunk in chunks},
            source_ids=frozenset(chunk.document_id for chunk in chunks),
            source_titles=dict(titles or {}),
            source_urls=dict(urls or {}),
        )

    def chunk_belongs_to(self, chunk_id: str, source_id: str) -> bool:
        """True when a chunk identifier belongs to the named source."""
        return chunk_id.startswith(f"{source_id}#")


class ExcerptVerdict(StrEnum):
    """Why one excerpt did or did not verify."""

    VERIFIED = "verified"  # Found verbatim in the cited chunk
    SOURCE_NOT_RETRIEVED = (
        "source_not_retrieved"  # Cited a document that was never shown to the model
    )
    CHUNK_NOT_RETRIEVED = "chunk_not_retrieved"  # Cited a chunk that was never shown
    CHUNK_SOURCE_MISMATCH = (
        "chunk_source_mismatch"  # Chunk does not belong to the source it was filed under
    )
    QUOTE_NOT_FOUND = "quote_not_found"  # The chunk exists; the quote is not in it


@dataclass(frozen=True)
class ExcerptResult:
    """Verification outcome for one excerpt."""

    source_id: str
    chunk_id: str
    verdict: ExcerptVerdict

    @property
    def verified(self) -> bool:
        """True when the excerpt was found verbatim in the cited chunk."""
        return self.verdict is ExcerptVerdict.VERIFIED

    @property
    def is_fabrication(self) -> bool:
        """True when the citation points at evidence that was never retrieved.

        Distinguished from a merely inaccurate quote, because it means something
        different: the model could not have read what it says it read.
        """
        return self.verdict in (
            ExcerptVerdict.SOURCE_NOT_RETRIEVED,
            ExcerptVerdict.CHUNK_NOT_RETRIEVED,
            ExcerptVerdict.CHUNK_SOURCE_MISMATCH,
        )


@dataclass
class ClaimVerification:
    """Verification outcome for one claim."""

    claim_id: str
    status: SupportStatus
    excerpts: list[ExcerptResult] = field(default_factory=list)
    invented_source_ids: list[str] = field(default_factory=list)
    numeric_violations: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def has_fabrication(self) -> bool:
        """True when any citation on this claim points at unretrieved evidence."""
        return bool(self.invented_source_ids) or any(
            result.is_fabrication for result in self.excerpts
        )


@dataclass
class VerificationReport:
    """Verification outcome for a whole answer."""

    claims: dict[str, ClaimVerification] = field(default_factory=dict)

    @property
    def fabrication_count(self) -> int:
        """Claims citing evidence that was never retrieved."""
        return sum(1 for verification in self.claims.values() if verification.has_fabrication)

    def status_counts(self) -> dict[str, int]:
        """Claim counts per support status, with every status present."""
        counts = {status.value: 0 for status in SupportStatus}
        for verification in self.claims.values():
            counts[verification.status.value] += 1
        return counts

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for logging and evaluation."""
        return {
            "status_counts": self.status_counts(),
            "fabrication_count": self.fabrication_count,
            "claims": {
                claim_id: {
                    "status": verification.status.value,
                    "reason": verification.reason,
                    "invented_source_ids": verification.invented_source_ids,
                    "numeric_violations": verification.numeric_violations,
                    "excerpts": [
                        {
                            "source_id": r.source_id,
                            "chunk_id": r.chunk_id,
                            "verdict": r.verdict.value,
                        }
                        for r in verification.excerpts
                    ],
                }
                for claim_id, verification in sorted(self.claims.items())
            },
        }


def normalize_for_matching(text: str) -> str:
    """Canonical form used on both sides of an excerpt comparison.

    NFC composition, whitespace collapsed, trimmed, lowercased. Deliberately
    nothing more — see the module docstring for why punctuation survives.
    """
    return normalize_text(text).lower()


def verify_excerpt(
    source_id: str, chunk_id: str, quote: str, evidence: RetrievedEvidence
) -> ExcerptResult:
    """Verify one excerpt against the retrieved evidence."""
    if source_id not in evidence.source_ids:
        # Requirement 4: an invented source identifier. Checked first because it
        # is the most decisive failure — nothing downstream can rehabilitate it.
        return ExcerptResult(source_id, chunk_id, ExcerptVerdict.SOURCE_NOT_RETRIEVED)
    if not evidence.chunk_belongs_to(chunk_id, source_id):
        return ExcerptResult(source_id, chunk_id, ExcerptVerdict.CHUNK_SOURCE_MISMATCH)
    chunk_text = evidence.chunk_texts.get(chunk_id)
    if chunk_text is None:
        return ExcerptResult(source_id, chunk_id, ExcerptVerdict.CHUNK_NOT_RETRIEVED)
    if normalize_for_matching(quote) not in normalize_for_matching(chunk_text):
        # Requirement 3: the quote must be an exact substring after the
        # documented normalisation.
        return ExcerptResult(source_id, chunk_id, ExcerptVerdict.QUOTE_NOT_FOUND)
    return ExcerptResult(source_id, chunk_id, ExcerptVerdict.VERIFIED)


def verify_claim(claim: GroundedClaim, evidence: RetrievedEvidence) -> ClaimVerification:
    """Verify one claim and assign its support status.

    Status assignment:

    * **UNSUPPORTED** -- no excerpt verified, or the claim cites a source that
      was never retrieved. Fabrication is always unsupported, however many other
      excerpts happen to check out: a claim built partly on invented evidence is
      not partly true.
    * **PARTIALLY_SUPPORTED** -- some excerpts verified and some did not, or a
      number in the claim is absent from the cited text.
    * **SUPPORTED** -- every excerpt verified and every number accounted for.
    """
    invented = [source_id for source_id in claim.source_ids if source_id not in evidence.source_ids]
    results = [
        verify_excerpt(e.source_id, e.chunk_id, e.quote, evidence)
        for e in claim.supporting_excerpts
    ]

    verified = [result for result in results if result.verified]
    verified_text = " ".join(evidence.chunk_texts.get(result.chunk_id, "") for result in verified)
    claim_numbers = extract_numbers(claim.text)
    numeric_violations = (
        sorted(claim_numbers - extract_numbers(verified_text))
        if claim_numbers and verified_text
        else []
    )

    if invented or any(result.is_fabrication for result in results):
        # Fabrication dominates. A claim citing evidence the model was never
        # shown is unsupported regardless of what else it cited correctly.
        status = SupportStatus.UNSUPPORTED
        reason = "cites evidence that was not retrieved"
    elif not claim.supporting_excerpts:
        status = SupportStatus.UNSUPPORTED
        reason = "no supporting excerpt supplied"
    elif not verified:
        status = SupportStatus.UNSUPPORTED
        reason = "no supplied excerpt was found in its cited chunk"
    elif len(verified) < len(results):
        status = SupportStatus.PARTIALLY_SUPPORTED
        reason = f"{len(verified)} of {len(results)} excerpts verified"
    elif numeric_violations:
        # A drifted threshold or dose is the highest-consequence hallucination
        # in medical text and shows no other symptom, so it degrades the status
        # even when every quote checked out.
        status = SupportStatus.PARTIALLY_SUPPORTED
        reason = f"numbers not found in cited evidence: {', '.join(numeric_violations)}"
    else:
        status = SupportStatus.SUPPORTED
        reason = f"all {len(verified)} excerpt(s) verified"

    return ClaimVerification(
        claim_id=claim.claim_id,
        status=status,
        excerpts=results,
        invented_source_ids=invented,
        numeric_violations=numeric_violations,
        reason=reason,
    )


def verify_answer(
    answer: GroundedAnswer, evidence: RetrievedEvidence
) -> tuple[GroundedAnswer, VerificationReport]:
    """Verify every claim and return the answer with statuses assigned.

    The returned answer is a **copy** with ``support_status`` set on each claim.
    Statuses are never taken from the model's own output, which is why the
    provider schema omits the field entirely.
    """
    report = VerificationReport()
    verified_claims = []
    for claim in answer.claims:
        verification = verify_claim(claim, evidence)
        report.claims[claim.claim_id] = verification
        verified_claims.append(claim.model_copy(update={"support_status": verification.status}))

    logger.info(
        "answer verified",
        extra={
            "claims": len(answer.claims),
            "fabrications": report.fabrication_count,
            **report.status_counts(),
        },
    )
    return answer.model_copy(update={"claims": verified_claims}), report


__all__ = [
    "ClaimVerification",
    "ExcerptResult",
    "ExcerptVerdict",
    "RetrievedEvidence",
    "VerificationReport",
    "normalize_for_matching",
    "verify_answer",
    "verify_claim",
    "verify_excerpt",
]

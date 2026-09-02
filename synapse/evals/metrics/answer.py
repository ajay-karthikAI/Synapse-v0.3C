"""
synapse.evals.metrics.answer
============================
Answer-quality metrics, all deterministic.

The design decision that matters here: **citation correctness and claim
groundedness are computed by string comparison, not by a model.**

A structured answer carries, for each claim, the verbatim excerpt it relied on
and the chunk that excerpt came from. Checking a citation is therefore
normalising the quote, normalising the cited chunk, and asserting containment.
That is free, offline, reproducible, and — unlike a judge — cannot itself
hallucinate.

What it cannot do is assess whether a correctly-quoted set of excerpts has been
*assembled* into a misleading claim. That requires judgement, lives in
:mod:`synapse.evals.judges`, and is advisory. So the deterministic check is a
**necessary but not sufficient** condition for groundedness, and the report
says so rather than implying the model has been validated.

A second deterministic layer catches the highest-consequence hallucination
class in medical text: :func:`numeric_consistency` requires every number, dose
and threshold in a claim to appear in a chunk that claim cites. A fabricated
"below 8%" against a source saying "below 7%" is caught here, by arithmetic.
"""

from __future__ import annotations  # Postponed annotations

import re  # Numeric extraction and sentence counting
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from synapse.normalize import normalize_text
from synapse.schemas.answer import AnswerClaim, StructuredAnswer
from synapse.schemas.enums import ClaimType

# Numbers with optional decimals, percent signs and common clinical units.
# Deliberately broad: a missed number is a missed hallucination, while a false
# positive merely asks a human to look at a claim that was already cited.
_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|mg|mcg|g|ml|l|mmol/l|mg/dl|mmhg|units?|years?|months?|weeks?|days?|hours?)?",
    re.IGNORECASE,
)

_SENTENCE_END = re.compile(r"[.!?]+(?:\s|$)")  # Sentence boundaries for readability
_WORD = re.compile(
    r"[A-Za-z][A-Za-z'-]*"
)  # Words for readability; excludes bare numerals, which distort syllable counts

_VOWEL_GROUP = re.compile(r"[aeiouy]+")  # Syllable approximation


@dataclass(frozen=True)
class CitationResult:
    """Outcome of checking one answer's citations."""

    factual_claims: int  # Denominator for completeness
    cited_claims: int  # Factual claims carrying at least one source
    total_excerpts: int  # Denominator for correctness
    verified_excerpts: int  # Excerpts found verbatim in their cited chunk
    unresolvable_citations: int  # Citations naming a chunk that was never retrieved
    numeric_violations: int  # Claims whose numbers do not appear in any cited chunk

    @property
    def completeness(self) -> float | None:
        """Fraction of factual claims that cite at least one source."""
        if (
            self.factual_claims == 0
        ):  # An answer with no factual claims is not incomplete; it is empty of claims
            return None
        return self.cited_claims / self.factual_claims

    @property
    def correctness(self) -> float | None:
        """Fraction of excerpts found verbatim in the chunk they cite."""
        if self.total_excerpts == 0:
            return None
        return self.verified_excerpts / self.total_excerpts

    @property
    def unsupported_claim_rate(self) -> float | None:
        """Fraction of factual claims with no verified supporting excerpt.

        The complement of the strictest reading of groundedness: a claim counts
        as supported only when at least one of its excerpts was found verbatim
        in a chunk that was actually retrieved.
        """
        if self.factual_claims == 0:
            return None
        return 1.0 - (self.cited_claims / self.factual_claims)


def _excerpt_is_verbatim(quote: str, chunk_text: str) -> bool:
    """True when ``quote`` appears in ``chunk_text`` after normalisation.

    Normalised on both sides so a quote that differs only in whitespace or
    Unicode form still verifies — otherwise a correct citation would fail
    because the model reflowed a line break.
    """
    return normalize_text(quote).lower() in normalize_text(chunk_text).lower()


def check_citations(
    answer: StructuredAnswer,
    chunk_texts: Mapping[str, str],
    retrieved_chunk_ids: Sequence[str] | None = None,
) -> CitationResult:
    """Verify every citation in a structured answer.

    Args:
        answer: the structured answer under test.
        chunk_texts: chunk identifier -> chunk text, for verbatim checking.
        retrieved_chunk_ids: what retrieval actually returned. A citation to a
            chunk that was never retrieved is a hard failure — the model cannot
            have read it — and is counted separately from a merely inaccurate
            quote.
    """
    retrieved = set(retrieved_chunk_ids) if retrieved_chunk_ids is not None else None

    factual = [claim for claim in answer.claims if claim.claim_type is ClaimType.FACTUAL]
    cited = 0
    total_excerpts = 0
    verified = 0
    unresolvable = 0
    numeric_violations = 0

    for claim in factual:
        claim_verified = False
        for excerpt in claim.excerpts:
            total_excerpts += 1
            if retrieved is not None and excerpt.chunk_id not in retrieved:
                # The model cited something it was never shown. Counted as a
                # distinct failure, because it means something other than a
                # sloppy quote.
                unresolvable += 1
                continue
            chunk_text = chunk_texts.get(excerpt.chunk_id)
            if chunk_text is None:  # Cited chunk is not in the corpus at all
                unresolvable += 1
                continue
            if _excerpt_is_verbatim(excerpt.quote, chunk_text):
                verified += 1
                claim_verified = True
        if claim_verified:
            cited += 1
        if not numeric_consistency(claim, chunk_texts):
            numeric_violations += 1

    return CitationResult(
        factual_claims=len(factual),
        cited_claims=cited,
        total_excerpts=total_excerpts,
        verified_excerpts=verified,
        unresolvable_citations=unresolvable,
        numeric_violations=numeric_violations,
    )


def extract_numbers(text: str) -> set[str]:
    """Numbers and unit-bearing quantities in ``text``, normalised for comparison."""
    found = set()
    for match in _NUMBER.finditer(text):
        token = re.sub(r"\s+", "", match.group(0).lower())  # "7 %" and "7%" must compare equal
        found.add(token)
    return found


def numeric_consistency(claim: AnswerClaim, chunk_texts: Mapping[str, str]) -> bool:
    """True when every number in a claim appears in a chunk that claim cites.

    The highest-consequence deterministic check in the harness. In patient-facing
    medical text, a drifted threshold or dose — "below 8%" against a source
    saying "below 7%" — is far more dangerous than a clumsy paraphrase, and it
    is exactly the error a fluent model makes without any other symptom.

    A claim citing nothing passes vacuously; its lack of citation is already
    counted by completeness, and failing it twice would double-penalise.
    """
    claim_numbers = extract_numbers(claim.text)
    if not claim_numbers:
        return True  # Nothing numeric to verify

    supporting = " ".join(chunk_texts.get(excerpt.chunk_id, "") for excerpt in claim.excerpts)
    if not supporting.strip():
        return True  # No cited text to check against; completeness already penalises this
    source_numbers = extract_numbers(supporting)
    return claim_numbers <= source_numbers


# ---------------------------------------------------------------------------
# Behavioural checks
# ---------------------------------------------------------------------------


def required_concept_coverage(
    answer_text: str, required_concepts: Sequence[str]
) -> tuple[float | None, list[str]]:
    """Fraction of required concepts the answer conveys, and which it missed.

    Substring matching on normalised text. Crude, and honestly so: it detects
    that a concept's wording is present, not that the answer *conveys* it. The
    known-limitations section of the metric documentation says this plainly,
    because a paraphrase scores as a miss.
    """
    if not required_concepts:
        return None, []  # Nothing required; not a perfect score, an inapplicable one
    normalized_answer = normalize_text(answer_text).lower()
    missing = [
        concept
        for concept in required_concepts
        if normalize_text(concept).lower() not in normalized_answer
    ]
    return (len(required_concepts) - len(missing)) / len(required_concepts), missing


def forbidden_claim_violations(answer_text: str, forbidden_claims: Sequence[str]) -> list[str]:
    """Forbidden claims the answer contains.

    Same substring approach, and the asymmetry is deliberate: a missed
    forbidden claim is a safety failure, while a false positive merely flags an
    answer for a human to read. So the matching errs toward flagging.
    """
    if not forbidden_claims:
        return []
    normalized_answer = normalize_text(answer_text).lower()
    return [
        claim for claim in forbidden_claims if normalize_text(claim).lower() in normalized_answer
    ]


def answer_format_valid(answer: StructuredAnswer) -> bool:
    """True when the answer satisfies its structural contract.

    Checks the invariants a downstream renderer depends on: a non-abstaining
    answer carries claims, an abstaining one carries a reason and no factual
    claims, and a boundary statement is present whenever medical content is.
    """
    if answer.abstained:
        return bool(answer.abstain_reason) and not any(
            c.claim_type is ClaimType.FACTUAL for c in answer.claims
        )
    if not answer.claims:  # A non-abstaining answer with nothing in it is not a valid answer
        return False
    # Medical content without the non-diagnosis boundary breaches the
    # product's stated safety contract.
    has_factual = any(c.claim_type is ClaimType.FACTUAL for c in answer.claims)
    return not (has_factual and not answer.boundary_statement.strip())


# ---------------------------------------------------------------------------
# Readability
# ---------------------------------------------------------------------------


def count_syllables(word: str) -> int:
    """Approximate syllable count for one word.

    Vowel-group counting with a silent-'e' adjustment. Approximate on purpose:
    a dictionary-based counter would need a pinned pronunciation dictionary as
    a dependency, and readability here is a trend indicator rather than a
    precise instrument.
    """
    lowered = word.lower()
    groups = _VOWEL_GROUP.findall(lowered)
    count = len(groups)
    if lowered.endswith("e") and count > 1 and not lowered.endswith(("le", "ee", "ye")):
        count -= 1  # Silent terminal 'e', except where it carries a syllable
    return max(1, count)  # Every word has at least one syllable


def flesch_kincaid_grade(text: str) -> float | None:
    """Flesch-Kincaid grade level: ``0.39*(w/s) + 11.8*(sy/w) - 15.59``.

    Vendored rather than taken from a dependency so the number is reproducible
    forever — a library update that changed its syllable heuristic would move
    every historical readability score.

    Returns ``None`` for text with no sentences or no words, rather than
    dividing by zero.
    """
    sentences = max(
        1, len(_SENTENCE_END.findall(text))
    )  # At least one: unterminated text is still a sentence
    words = _WORD.findall(text)
    if not words:
        return None
    syllables = sum(count_syllables(word) for word in words)
    return 0.39 * (len(words) / sentences) + 11.8 * (syllables / len(words)) - 15.59


__all__ = [
    "CitationResult",
    "answer_format_valid",
    "check_citations",
    "count_syllables",
    "extract_numbers",
    "flesch_kincaid_grade",
    "forbidden_claim_violations",
    "numeric_consistency",
    "required_concept_coverage",
]

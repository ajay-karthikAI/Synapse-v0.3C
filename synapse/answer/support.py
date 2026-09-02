"""
synapse.answer.support
======================
Deterministic lexical support checking, and an interface for optional semantic
entailment.

Two layers, deliberately separated because they have different trust levels:

* :func:`lexical_support` is arithmetic over token overlap. It runs always, runs
  offline, and cannot itself hallucinate. It is what the abstention policy reads.
* :class:`EntailmentChecker` is an *interface*. A semantic implementation may be
  plugged in behind it, but the default is a null checker that abstains from
  judging. Nothing in the display path depends on it.

Why lexical support exists at all, given that :mod:`synapse.answer.verify`
already proves the quote is real: a verbatim quote proves the model *copied*
something, not that the copied text has anything to do with the claim built on
it. A model can quote "Metformin is a biguanide" accurately and attach it to
"metformin cures diabetes". Lexical overlap catches the crudest version of that
mismatch.

It catches only the crudest version, and the documentation says so. Overlap is
not entailment — that is exactly why the semantic checker exists as a hook and
exactly why neither is called clinical validation.
"""

from __future__ import annotations  # Postponed annotations

import re
from dataclasses import dataclass
from typing import Protocol

from synapse.answer.schema import GroundedClaim
from synapse.normalize import normalize_text

# Words carrying no topical signal. Kept small and closed: an aggressive list
# would strip clinically meaningful qualifiers such as "not", "may" or "should",
# and those are precisely the words that change what a claim asserts.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "at",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "with",
        "as",
        "from",
        "your",
        "you",
        "their",
    }
)

_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*")

# Overlap at or above which a claim is treated as lexically supported by its
# evidence. Set low on purpose: this is a crude backstop, and a false negative
# would suppress a correct claim, which is a worse outcome for a patient than
# passing a weak one through to the (stricter) verbatim check.
LEXICAL_SUPPORT_THRESHOLD = 0.35


def content_tokens(text: str) -> frozenset[str]:
    """Content-bearing tokens of ``text``, normalised."""
    return frozenset(
        token for token in _WORD.findall(normalize_text(text).lower()) if token not in _STOPWORDS
    )


@dataclass(frozen=True)
class LexicalSupport:
    """How much of a claim's vocabulary appears in its evidence."""

    overlap: float  # Fraction of the claim's content tokens present in the evidence
    claim_tokens: int
    matched_tokens: int
    unmatched: frozenset[str]  # Claim vocabulary absent from the evidence

    @property
    def is_supported(self) -> bool:
        """True when overlap reaches the threshold."""
        return self.overlap >= LEXICAL_SUPPORT_THRESHOLD


def lexical_support(claim_text: str, evidence_text: str) -> LexicalSupport:
    """Token-overlap support of a claim by its evidence.

    Asymmetric by design: it measures how much of the *claim* is covered by the
    evidence, not the reverse. A long chunk supporting a short claim should
    score highly; the reverse should not.
    """
    claim = content_tokens(claim_text)
    if not claim:  # A claim with no content words cannot be assessed this way
        return LexicalSupport(overlap=0.0, claim_tokens=0, matched_tokens=0, unmatched=frozenset())
    evidence = content_tokens(evidence_text)
    matched = claim & evidence
    return LexicalSupport(
        overlap=len(matched) / len(claim),
        claim_tokens=len(claim),
        matched_tokens=len(matched),
        unmatched=claim - evidence,
    )


class EntailmentChecker(Protocol):
    """Optional semantic check: does the evidence entail the claim?

    Narrow on purpose. A wide interface would drag provider types into the
    answer path, and the whole point is that the display path works without any
    implementation of this at all.
    """

    name: str

    def entails(self, claim_text: str, evidence_text: str) -> EntailmentResult: ...


@dataclass(frozen=True)
class EntailmentResult:
    """Outcome of a semantic entailment check."""

    entailed: bool | None  # None = the checker declined to judge
    score: float | None = None
    checker: str = "none"
    rationale: str = ""

    @property
    def is_conclusive(self) -> bool:
        """True when the checker actually reached a verdict."""
        return self.entailed is not None


@dataclass
class NullEntailmentChecker:
    """The default: declines to judge.

    Present so the interface is always satisfied and callers need no branch,
    while making the absence of a semantic check *visible* in the result rather
    than silently defaulting to "entailed".
    """

    name: str = "null"

    def entails(self, claim_text: str, evidence_text: str) -> EntailmentResult:
        """Always inconclusive."""
        del claim_text, evidence_text  # Unused: this checker judges nothing
        return EntailmentResult(
            entailed=None, checker=self.name, rationale="no entailment checker configured"
        )


@dataclass
class LexicalEntailmentChecker:
    """Adapts :func:`lexical_support` to the entailment interface.

    A convenience for callers wanting one code path. It is **not** an
    entailment checker in any real sense — overlap is not entailment — and it
    reports itself as ``lexical`` so a consumer can tell the difference.
    """

    name: str = "lexical"
    threshold: float = LEXICAL_SUPPORT_THRESHOLD

    def entails(self, claim_text: str, evidence_text: str) -> EntailmentResult:
        """Judge by token overlap."""
        support = lexical_support(claim_text, evidence_text)
        return EntailmentResult(
            entailed=support.overlap >= self.threshold,
            score=support.overlap,
            checker=self.name,
            rationale=f"{support.matched_tokens}/{support.claim_tokens} content tokens found in evidence",
        )


def evidence_text_for(claim: GroundedClaim, chunk_texts: dict[str, str]) -> str:
    """Concatenated text of every chunk a claim cites."""
    return " ".join(chunk_texts.get(excerpt.chunk_id, "") for excerpt in claim.supporting_excerpts)


__all__ = [
    "LEXICAL_SUPPORT_THRESHOLD",
    "EntailmentChecker",
    "EntailmentResult",
    "LexicalEntailmentChecker",
    "LexicalSupport",
    "NullEntailmentChecker",
    "content_tokens",
    "evidence_text_for",
    "lexical_support",
]

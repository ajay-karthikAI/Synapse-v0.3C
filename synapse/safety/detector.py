"""
synapse.safety.detector
=======================
Emergency escalation detection: does this query describe something that needs
care now, ahead of any retrieval?

What replaced what
------------------
The previous implementation was::

    return any(signal in query.lower() for signal in EMERGENCY_SIGNALS)

Substring matching over 24 fixed phrases. Measured on the repository's own
safety cases it scored **2/6** on real emergencies and **2/6** on negation:

======================================  ==========================  ===========
Patient wrote                           List contained              Result
======================================  ==========================  ===========
"speech is slurred ... face has         "speech difficulty",        MISSED
dropped"                                "face drooping"
"struggling to breathe ... lips look    "difficulty breathing"      MISSED
blue"
"coughing **up** blood"                 "coughing blood"            MISSED
"took **far** too many"                 "took too much"             MISSED
"i do **not** have any chest pain"      "chest pain"                ESCALATED
======================================  ==========================  ===========

Every miss is the same bug: a fixed phrase cannot survive the words people
actually put between and around it.

How this works instead
----------------------
1. **Stem proximity, not fixed phrases.** A pattern is a set of word stems that
   must all appear within a window. ``["cough", "blood"]`` matches "coughing up
   blood", "coughed blood", and "blood when I cough" — without enumerating
   conjugations. Prefix matching on stems handles morphology cheaply.
2. **Negation scoping.** An explicit denial before the symptom suppresses it,
   with scope ending at a clause boundary. See :mod:`synapse.safety.negation`.
3. **A governed vocabulary.** Concepts live in
   ``config/emergency_vocabulary.toml`` where a clinician can review them, not
   inline in application code.

The asymmetry, again, because it governs every judgement call: **a missed
emergency can kill; a false escalation is an inconvenience.** Patterns are
broad, negation is reluctant, and anything ambiguous escalates.

Still not clinically validated
------------------------------
This fixes an *engineering* defect. The vocabulary is engineering-authored and
carries ``review_status = "unreviewed"``. Better recall on twelve cases written
by the same person is not clinical evidence, and nothing here may be described
as clinically validated. See ``docs/SAFETY_CASE.md``.
"""

from __future__ import annotations  # Postponed annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from synapse.logging import get_logger
from synapse.safety.negation import is_negated
from synapse.safety.vocabulary import EmergencyConcept, EmergencyVocabulary

logger = get_logger(__name__)

# Split on anything that is not a letter, digit or apostrophe. Keeping the
# apostrophe lets "can't" survive as one token before it is normalised.
_TOKEN_RE = re.compile(r"[^a-z0-9']+")


def tokenize(text: str) -> list[str]:
    """Lowercase and split a query into comparable tokens.

    Apostrophes are stripped after splitting so "can't", "cant" and "can t" all
    normalise to ``cant`` — patients type all three.
    """
    lowered = text.lower()
    return [token.replace("'", "") for token in _TOKEN_RE.split(lowered) if token]


@dataclass(frozen=True)
class EmergencyMatch:
    """One concept that fired, and why.

    Carries the reason so a report can explain an escalation. The query text is
    deliberately absent: matches are logged, and patient text is never logged.
    """

    concept_id: str
    label: str
    pattern: tuple[str, ...]
    negated: bool = False

    def describe(self) -> str:
        """Short explanation, safe to log or display."""
        return f"{self.label} ({self.concept_id}) via {'+'.join(self.pattern)}"


def _stem_positions(tokens: list[str], stem: str) -> list[int]:
    """Indices of tokens that start with ``stem``.

    Prefix matching is what makes one stem cover a family: ``breath`` matches
    "breath", "breathe", "breathing", "breathless".
    """
    return [index for index, token in enumerate(tokens) if token.startswith(stem)]


def _match_pattern(tokens: list[str], pattern: list[str], window: int) -> int | None:
    """Return the first index of a match, or ``None``.

    A pattern matches when every stem appears and the span from the earliest to
    the latest is within ``window`` tokens — so the stems are one phrase rather
    than coincidences scattered across a paragraph.
    """
    positions_per_stem = [_stem_positions(tokens, stem) for stem in pattern]
    if any(not positions for positions in positions_per_stem):
        return None  # A stem is absent, so the pattern cannot match

    # Single stem: any occurrence is a match.
    if len(positions_per_stem) == 1:
        return positions_per_stem[0][0]

    # Try every occurrence of the first stem as an anchor and check the rest
    # fall within the window. Patterns are 2-3 stems, so this stays trivial.
    best: int | None = None
    for anchor in positions_per_stem[0]:
        chosen = [anchor]
        for positions in positions_per_stem[1:]:
            nearest = min(positions, key=lambda p: abs(p - anchor))
            chosen.append(nearest)
        span = max(chosen) - min(chosen)
        if span <= window:
            start = min(chosen)
            best = start if best is None else min(best, start)
    return best


@dataclass
class EmergencyDetector:
    """Detects emergency concepts in a query using a governed vocabulary."""

    vocabulary: EmergencyVocabulary

    def _concept_match(self, tokens: list[str], concept: EmergencyConcept) -> EmergencyMatch | None:
        """First non-negated pattern hit for one concept."""
        window = self.vocabulary.meta.proximity_window
        for pattern in concept.patterns:
            start = _match_pattern(tokens, pattern, window)
            if start is None:
                continue
            # Self-harm and similar concepts opt out: "I don't want to live" is
            # a disclosure, and suppressing it would be the worst possible bug.
            if not concept.negation_exempt and is_negated(tokens, start):
                continue
            return EmergencyMatch(
                concept_id=concept.id, label=concept.label, pattern=tuple(pattern)
            )
        return None

    def detect(self, query: str) -> list[EmergencyMatch]:
        """Return every concept that fired. Empty means no escalation."""
        tokens = tokenize(query)
        matches = [
            match
            for concept in self.vocabulary.concepts
            if (match := self._concept_match(tokens, concept)) is not None
        ]
        if matches:
            logger.info(
                "emergency escalation triggered",
                extra={  # Concept ids only; never the query text
                    "concept_ids": ",".join(m.concept_id for m in matches),
                    "vocabulary_version": self.vocabulary.meta.version,
                },
            )
        return matches

    def is_emergency(self, query: str) -> bool:
        """Boolean form, matching the legacy ``check_emergency`` signature."""
        return bool(self.detect(query))


@lru_cache(maxsize=4)  # Loading parses TOML; the file does not change mid-run
def load_detector(path: Path | None = None) -> EmergencyDetector:
    """Build a detector from the vocabulary file."""
    return EmergencyDetector(vocabulary=EmergencyVocabulary.load(path))

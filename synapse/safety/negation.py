"""
synapse.safety.negation
=======================
Decide whether a matched symptom was **denied** rather than reported.

Why this module exists
----------------------
The previous detector had no concept of negation, so it escalated:

    "i do not have any chest pain i just want to understand cholesterol"
    "i have never had a stroke but want to know the warning signs"

Escalating those is not harmless. A warning that fires on a denial trains people
to dismiss it, and a dismissed warning protects nobody — the false-positive path
degrades the true-positive path.

The asymmetry that governs every choice here
--------------------------------------------
**A missed emergency can kill. A false escalation is an inconvenience.**

So this module is deliberately *reluctant* to negate. When the scope of a
negation is ambiguous, it does NOT suppress. Concretely:

* Only an explicit cue negates. Absence of evidence is never negation.
* The cue must appear **before** the symptom, within a short window.
* Scope ends at a coordinating conjunction or clause break, so
  "no chest pain **but** my arm is numb" leaves "arm numb" escalating.
* Some concepts opt out entirely (``negation_exempt``): "i don't want to live"
  is a disclosure, not a denial, and must never be suppressed.

This is a lexical approximation in the spirit of NegEx. It is not a parser and
it does not understand language. It has not been clinically reviewed.
"""

from __future__ import annotations  # Postponed annotations

# Cues that negate what follows them. Kept small on purpose: every addition
# creates a new way to suppress a real emergency.
NEGATION_CUES: frozenset[str] = frozenset(
    {
        "no",
        "not",
        "never",
        "none",
        "without",
        "deny",
        "denies",
        "denied",
        "dont",
        "doesnt",
        "didnt",
        "havent",
        "hasnt",
        "hadnt",
        "isnt",
        "arent",
        "wasnt",
        "cant",  # "cant breathe" is handled by the concept itself, see below
        "couldnt",
        "wouldnt",
        "wont",
        "free",  # "free of chest pain"
        "negative",
        "ruled",  # "ruled out"
    }
)

# Words that END a negation's scope. Everything after one is NOT negated, which
# is what keeps "no chest pain but my face has drooped" escalating.
SCOPE_TERMINATORS: frozenset[str] = frozenset(
    {
        "but",
        "however",
        "although",
        "though",
        "except",
        "yet",
        "still",
        "besides",
        "aside",
        "and",  # Conservative: "no headache and my speech is slurred"
        "or",
        "while",
        "whereas",
        "now",
        "today",
        "since",
        "because",
    }
)

# Cues whose negation is cancelled by an immediately following word, so
# "cant breathe" (an emergency) is not read as a denial.
# NOTE the direction of the risk here: each entry makes a cue LESS likely to
# suppress, i.e. more likely to escalate. That is the safe direction.
_SYMPTOM_VERBS = frozenset(
    {"breathe", "breath", "breathing", "speak", "move", "wake", "stop", "see", "swallow", "walk"}
)
_CUE_CANCELLERS: dict[str, frozenset[str]] = {
    "cant": _SYMPTOM_VERBS,
    "couldnt": _SYMPTOM_VERBS,
    "wouldnt": _SYMPTOM_VERBS,
    "wont": _SYMPTOM_VERBS,
    "no": frozenset({"reason", "longer", "point"}),  # "no reason to live"
}

# How many tokens before the match a cue may sit and still negate it.
DEFAULT_SCOPE_TOKENS = 5


def is_negated(
    tokens: list[str],
    match_start: int,
    *,
    scope_tokens: int = DEFAULT_SCOPE_TOKENS,
) -> bool:
    """True if the symptom starting at ``match_start`` is explicitly denied.

    Walks backwards from the match looking for a cue, stopping at the first
    scope terminator. Returning ``False`` is the safe default and is what
    happens on anything unclear.
    """
    if match_start <= 0:
        return False  # Nothing precedes it, so nothing can negate it

    window_start = max(0, match_start - scope_tokens)
    for index in range(match_start - 1, window_start - 1, -1):
        token = tokens[index]

        if token in SCOPE_TERMINATORS:
            # A clause boundary. Anything earlier belongs to a different clause,
            # so it cannot negate this symptom.
            return False

        if token in NEGATION_CUES:
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            # A cancelled cue ("cant breathe") is part of the symptom, not a denial.
            return following not in _CUE_CANCELLERS.get(token, frozenset())

    return False

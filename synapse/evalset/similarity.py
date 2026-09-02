"""
synapse.evalset.similarity
==========================
Near-duplicate query detection.

Exact duplicates are easy — compare normalised queries. Near-duplicates are the
ones that quietly corrupt an evaluation: "what does HbA1c measure" in train and
"what is HbA1c measuring" in test are the same question, so a system tuned on
one has effectively seen the other, and the test score is inflated.

The measure here is deliberately simple and dependency-free:

* **Character shingles** (overlapping 4-grams) catch reordering, minor
  rewording and typos. "what does HbA1c measure" and "what is HbA1c measuring"
  share most of their character n-grams.
* **Token sets** catch pure reordering, which character shingles handle less
  reliably at short lengths.

The two are combined by taking the maximum, because either signal alone is
sufficient evidence of a near-duplicate.

No embeddings and no external model: split assignment derives from these
scores, so the measure must be perfectly reproducible across machines and
versions. A model download would make split membership depend on which model
version happened to be cached.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass

from synapse.schemas.evalset import normalize_query

SHINGLE_SIZE = 4  # Character n-gram length. 4 is short enough to survive a one-word change, long enough not to match on common substrings alone.

# Jaccard similarity at or above which two queries are treated as near-duplicates.
#
# 0.70 rather than a tidier 0.80, because the errors are ASYMMETRIC. A false
# positive merely forces two cases into the same split, which costs nothing. A
# false negative splits a near-duplicate across train and test, which silently
# inflates the test score — the exact failure this module exists to prevent. So
# the threshold errs low.
#
# Measured on representative pairs:
#     "what does an hba1c test measure" / "what does a hba1c test measure"  0.774
#     "can i eat before my blood test"  / "should i eat before my blood test" 0.750
#     "should i stop taking metformin"  / "should i start taking metformin"   0.667
#     "what is hba1c"                   / "how do i book an appointment"      0.000
#
# KNOWN LIMITATION: lexical overlap cannot distinguish "stop" from "start". The
# stop/start pair sits at 0.667, just below the threshold, but that margin is
# incidental rather than principled — a one-word antonym barely changes lexical
# overlap. synapse.evalset.dataset therefore also reports any near-duplicate
# cluster whose members expect DIFFERENT behaviours, so a human sees the case
# where this measure is out of its depth.
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.70


def character_shingles(text: str, size: int = SHINGLE_SIZE) -> frozenset[str]:
    """Overlapping character n-grams of ``text``.

    Operates on the normalised form so casing and punctuation do not create
    spurious differences. Text shorter than one shingle yields the whole string
    as a single shingle, so short queries still compare meaningfully.
    """
    normalized = normalize_query(text)
    if len(normalized) <= size:  # Too short to shingle; compare it whole
        return frozenset({normalized}) if normalized else frozenset()
    return frozenset(
        normalized[index : index + size] for index in range(len(normalized) - size + 1)
    )


def token_set(text: str) -> frozenset[str]:
    """Distinct whitespace-separated tokens of the normalised text."""
    return frozenset(normalize_query(text).split())


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard similarity: |intersection| / |union|."""
    if not left and not right:  # Two empty queries are identical, not undefined
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def query_similarity(left: str, right: str) -> float:
    """Similarity of two queries, in [0, 1].

    The maximum of character-shingle and token-set Jaccard: either signal alone
    is sufficient evidence that two queries ask the same thing.
    """
    if normalize_query(left) == normalize_query(
        right
    ):  # Exact match after normalisation short-circuits to 1.0
        return 1.0
    return max(
        jaccard(character_shingles(left), character_shingles(right)),
        jaccard(token_set(left), token_set(right)),
    )


@dataclass(frozen=True)
class DuplicatePair:
    """Two cases whose queries are duplicates or near-duplicates."""

    case_id_a: str
    case_id_b: str
    similarity: float
    exact: bool  # True when the normalised queries are byte-identical

    def as_dict(self) -> dict[str, object]:
        """Machine-readable form for reports."""
        return {
            "case_id_a": self.case_id_a,
            "case_id_b": self.case_id_b,
            "similarity": round(
                self.similarity, 4
            ),  # Rounded so two runs produce byte-identical reports
            "exact": self.exact,
        }


def find_duplicate_pairs(
    cases: list[tuple[str, str]],
    *,
    threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> list[DuplicatePair]:
    """Find duplicate and near-duplicate query pairs.

    Args:
        cases: ``(case_id, query)`` pairs.
        threshold: similarity at or above which a pair is reported.

    Returns:
        Pairs sorted by descending similarity then by identifier, so the output
        is deterministic and the worst offenders appear first.

    Complexity is O(n²) in the number of cases. At evaluation-dataset scale —
    hundreds, not millions — that is well under a second, and an exact pairwise
    comparison avoids the recall loss of a blocking or hashing scheme.
    """
    shingles = {case_id: character_shingles(query) for case_id, query in cases}
    tokens = {case_id: token_set(query) for case_id, query in cases}
    normalized = {case_id: normalize_query(query) for case_id, query in cases}

    pairs: list[DuplicatePair] = []
    identifiers = [case_id for case_id, _ in cases]
    for index, left_id in enumerate(identifiers):
        for right_id in identifiers[index + 1 :]:  # Upper triangle only: similarity is symmetric
            exact = normalized[left_id] == normalized[right_id]
            similarity = (
                1.0
                if exact
                else max(
                    jaccard(shingles[left_id], shingles[right_id]),
                    jaccard(tokens[left_id], tokens[right_id]),
                )
            )
            if similarity >= threshold:
                pairs.append(DuplicatePair(left_id, right_id, similarity, exact))

    pairs.sort(key=lambda pair: (-pair.similarity, pair.case_id_a, pair.case_id_b))
    return pairs


def cluster_by_similarity(
    cases: list[tuple[str, str]],
    *,
    threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> list[list[str]]:
    """Group cases into clusters of mutually near-duplicate queries.

    Connected components over the near-duplicate graph, computed with a
    union-find. Transitivity is intentional: if A≈B and B≈C, all three belong
    to one cluster even when A and C fall below the threshold, because
    splitting them would still leak information between splits through B.

    Returns clusters sorted by their smallest member, and members sorted within
    each cluster, so split assignment downstream is deterministic.
    """
    parent: dict[str, str] = {case_id: case_id for case_id, _ in cases}

    def find(node: str) -> str:
        """Find with path compression."""
        while parent[node] != node:
            parent[node] = parent[parent[node]]  # Path halving keeps this near-constant time
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        """Merge two components."""
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            # Merge toward the lexicographically smaller root, so the result
            # does not depend on the order pairs were discovered in.
            if left_root < right_root:
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root

    for pair in find_duplicate_pairs(cases, threshold=threshold):
        union(pair.case_id_a, pair.case_id_b)

    clusters: dict[str, list[str]] = {}
    for case_id, _ in cases:
        clusters.setdefault(find(case_id), []).append(case_id)

    result = [sorted(members) for members in clusters.values()]
    result.sort(key=lambda members: members[0])
    return result


__all__ = [
    "DEFAULT_NEAR_DUPLICATE_THRESHOLD",
    "SHINGLE_SIZE",
    "DuplicatePair",
    "character_shingles",
    "cluster_by_similarity",
    "find_duplicate_pairs",
    "jaccard",
    "query_similarity",
    "token_set",
]

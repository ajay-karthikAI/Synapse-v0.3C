"""
synapse.safety.vocabulary
=========================
Typed loader for ``config/emergency_vocabulary.toml``.

The vocabulary is **untrusted input**, like every other artifact in this
repository: validated on load with ``extra="forbid"``, never trusted because it
happens to live in the repo.

The property this module exists to guarantee: **the vocabulary cannot claim to
be clinically approved unless a reviewer is actually recorded.** ``is_approved``
requires both an ``approved`` status *and* a non-empty ``reviewed_by``. The
label is not the evidence; the reviewer identifier is.
"""

from __future__ import annotations  # Postponed annotations

import tomllib  # stdlib TOML. Never YAML — unsafe loaders are forbidden here.
from pathlib import Path

from pydantic import Field, field_validator

from synapse.errors import SynapseArtifactError
from synapse.logging import get_logger
from synapse.schemas.base import SynapseModel

logger = get_logger(__name__)

# Default location. Overridable so tests never depend on repository state.
DEFAULT_VOCABULARY_PATH = Path(__file__).resolve().parent.parent.parent / (
    "config/emergency_vocabulary.toml"
)


class VocabularyError(SynapseArtifactError):
    """The emergency vocabulary is missing, malformed, or self-contradictory."""

    reason = "emergency vocabulary invalid"


class EmergencyConcept(SynapseModel):
    """One escalation-worthy clinical concept and its patient-facing wordings."""

    id: str = Field(min_length=1, description="Stable concept identifier, e.g. 'stroke'.")
    label: str = Field(min_length=1, description="Human-readable name for reports.")
    review_status: str = Field(default="unreviewed")
    source_note: str = Field(default="", description="Where the surface forms came from.")
    negation_exempt: bool = Field(
        default=False,
        description=(
            "When true, negation never suppresses this concept. Set for self-harm, "
            "where 'I don't want to live' is a disclosure rather than a denial."
        ),
    )
    patterns: list[list[str]] = Field(
        min_length=1,
        description="Each pattern is a list of word STEMS that must all appear near each other.",
    )

    @field_validator("patterns")
    @classmethod
    def _patterns_are_lowercase_and_nonempty(cls, value: list[list[str]]) -> list[list[str]]:
        """Reject empty or mixed-case stems.

        Matching lowercases the query, so an uppercase stem would silently never
        match — a pattern that cannot fire is worse than a missing one, because
        it looks like coverage.
        """
        for pattern in value:
            if not pattern:
                raise ValueError("a pattern must contain at least one stem")
            for stem in pattern:
                if not stem or stem != stem.lower().strip():
                    raise ValueError(f"stem must be lowercase and stripped: {stem!r}")
        return value


class VocabularyMeta(SynapseModel):
    """Version and review provenance for the vocabulary as a whole."""

    version: str = Field(min_length=1)
    review_status: str = Field(default="unreviewed")
    reviewed_by: str = Field(default="", description="Pseudonymous reviewer id; EMPTY = unsigned.")
    reviewed_at: str = Field(default="")
    proximity_window: int = Field(default=6, ge=1, le=30)
    notes: str = Field(default="")


class EmergencyVocabulary(SynapseModel):
    """The whole vocabulary: metadata plus concepts."""

    meta: VocabularyMeta
    concepts: list[EmergencyConcept] = Field(min_length=1)

    @property
    def is_approved(self) -> bool:
        """True only with BOTH an approved status and a recorded reviewer.

        Mirrors the source-pack rule: an empty approver means unapproved no
        matter what the status string says.
        """
        return self.meta.review_status == "approved" and bool(self.meta.reviewed_by.strip())

    @property
    def pattern_count(self) -> int:
        """Total patterns across all concepts, for reporting."""
        return sum(len(concept.patterns) for concept in self.concepts)

    def provenance_note(self) -> str:
        """One line stating the review status, for any report that uses this."""
        if self.is_approved:
            return f"Emergency vocabulary {self.meta.version}, reviewed by {self.meta.reviewed_by}."
        return (
            f"Emergency vocabulary {self.meta.version} is UNREVIEWED: engineering-authored, "
            "no clinician has signed it, and it is not clinically validated."
        )

    @classmethod
    def load(cls, path: Path | None = None) -> EmergencyVocabulary:
        """Read and validate the vocabulary file."""
        target = path or DEFAULT_VOCABULARY_PATH
        if not target.is_file():
            # Refuse rather than degrade. A detector with no vocabulary would
            # escalate nothing, which is the most dangerous possible failure.
            raise VocabularyError(problem="emergency vocabulary not found", path=target)
        try:
            payload = tomllib.loads(target.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise VocabularyError(problem="vocabulary is not valid TOML", path=target) from exc

        raw_concepts = payload.get("concept", [])
        if not raw_concepts:
            raise VocabularyError(problem="vocabulary defines no concepts", path=target)

        vocabulary = cls.model_validate({"meta": payload.get("meta", {}), "concepts": raw_concepts})

        ids = [concept.id for concept in vocabulary.concepts]
        duplicates = {name for name in ids if ids.count(name) > 1}
        if duplicates:
            raise VocabularyError(
                problem="duplicate concept ids",
                path=target,
                duplicates=",".join(sorted(duplicates)),
            )

        logger.info(
            "emergency vocabulary loaded",
            extra={  # Counts and versions only; never query text
                "vocabulary_version": vocabulary.meta.version,
                "concept_count": len(vocabulary.concepts),
                "pattern_count": vocabulary.pattern_count,
                "approved": vocabulary.is_approved,
            },
        )
        return vocabulary

"""
synapse.schemas.chunk
=====================
:class:`EvidenceChunk` — the unit stored in the corpus and indexed by FAISS.

Two invariants in this model exist specifically to make whole classes of
previously-possible corruption unrepresentable:

  * ``chunk_id`` must *parse back* to ``document_id`` and ``ordinal``. The
    identifier cannot drift from the fields it encodes.
  * ``content_sha256`` must equal the hash of ``text``. A record whose text was
    altered after hashing — in transit, in an editor, or by a bad merge — fails
    at load rather than being served to a patient as if it were the cited
    source.

Character offsets are carried because claim-level citation validation needs to
locate a quoted excerpt inside its source document. That check reduces
"is this citation correct?" from a model judgement to a string comparison.
"""

from __future__ import annotations  # Postponed annotations

from datetime import datetime  # Ingestion timestamp
from typing import ClassVar  # Schema constants

from pydantic import Field, field_validator, model_validator

from synapse.hashing import is_sha256_hex, sha256_text  # Digest validation and recomputation
from synapse.identifiers import is_valid_chunk_id, is_valid_document_id, parse_chunk_id
from synapse.normalize import (
    is_normalized,  # Chunk text must already be canonical, since its hash depends on it
)
from synapse.schemas.base import VersionedModel, require_timezone_aware
from synapse.schemas.enums import SourceType


class EvidenceChunk(VersionedModel):
    """A single retrievable passage of evidence, with full provenance."""

    SCHEMA_NAME: ClassVar[str] = "evidence_chunk"  # Registry key and error label
    SCHEMA_VERSION: ClassVar[str] = "1.0"  # Bumped only on a deliberate contract change

    chunk_id: str = Field(description="Stable identifier, '<document_id>#<ordinal>'.")
    document_id: str = Field(description="Identifier of the parent source document.")
    ordinal: int = Field(
        ge=0, description="Zero-based position of this chunk within its document's emitted stream."
    )

    text: str = Field(
        min_length=1, description="Normalised passage text, exactly as it will be shown or cited."
    )
    content_sha256: str = Field(
        description="SHA-256 of the normalised text; recomputed and checked on load."
    )

    char_start: int = Field(
        ge=0, description="Start offset of this passage in the parent document's normalised text."
    )
    char_end: int = Field(
        gt=0, description="End offset (exclusive) in the parent document's normalised text."
    )

    source_type: SourceType = Field(description="Origin format of the parent document.")
    section: str | None = Field(
        default=None,
        description="Structured-abstract section label, e.g. 'CONCLUSIONS', when the source provided one.",
    )
    page: int | None = Field(
        default=None, ge=0, description="Page number for PDF sources; null when not applicable."
    )
    token_count: int | None = Field(
        default=None,
        ge=0,
        description="Token count under the tokenizer named in the index manifest.",
    )

    ingested_at: datetime = Field(
        description="When this chunk entered the corpus (timezone-aware)."
    )
    legacy_chunk_id: str | None = Field(
        default=None,
        description="Identifier this chunk carried in a pre-migration artifact; retained for traceability only, never used for lookup.",
    )

    @field_validator("chunk_id")  # Runs on the identifier alone
    @classmethod
    def _validate_chunk_id(cls, value: str) -> str:
        """Reject identifiers that do not match the grammar in synapse.identifiers."""
        if not is_valid_chunk_id(value):  # Grammar check before any structural cross-check
            raise ValueError(
                "chunk_id does not match the required '<scheme>:<key>#<ordinal>' grammar"
            )
        return value

    @field_validator("document_id")
    @classmethod
    def _validate_document_id(cls, value: str) -> str:
        """Reject malformed parent document identifiers."""
        if not is_valid_document_id(value):
            raise ValueError("document_id does not match the required '<scheme>:<key>' grammar")
        return value

    @field_validator("content_sha256")
    @classmethod
    def _validate_digest_shape(cls, value: str) -> str:
        """Reject anything that is not a lowercase 64-character hex digest."""
        if not is_sha256_hex(
            value
        ):  # Shape check is separate from the value check below, so the error says which one failed
            raise ValueError("content_sha256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("text")
    @classmethod
    def _validate_text_normalized(cls, value: str) -> str:
        """Require text to already be in canonical form.

        Normalising here instead would silently rewrite content and invalidate
        the caller's precomputed hash, so the model refuses rather than fixes.
        """
        if not is_normalized(value):
            raise ValueError(
                "text is not normalised; call synapse.normalize.normalize_text before constructing the chunk"
            )
        return value

    @model_validator(mode="after")  # Cross-field checks run once all fields are individually valid
    def _validate_consistency(self) -> EvidenceChunk:
        """Enforce the invariants that make identifier drift and content drift impossible."""
        parsed_document_id, parsed_ordinal = parse_chunk_id(
            self.chunk_id
        )  # Decompose the identifier
        if (
            parsed_document_id != self.document_id
        ):  # The identifier must agree with the field it encodes
            raise ValueError("chunk_id document component does not match document_id")
        if parsed_ordinal != self.ordinal:  # Likewise for the ordinal
            raise ValueError("chunk_id ordinal component does not match ordinal")
        if (
            self.char_end <= self.char_start
        ):  # A zero-length or inverted span cannot locate an excerpt
            raise ValueError("char_end must be greater than char_start")
        if (
            sha256_text(self.text) != self.content_sha256
        ):  # THE integrity invariant: text and digest must agree
            raise ValueError("content_sha256 does not match the SHA-256 of text")
        require_timezone_aware(
            self.ingested_at, "ingested_at"
        )  # Naive timestamps are not verifiable provenance
        return self

    @classmethod
    def build(  # Convenience constructor that computes the derived fields, so callers cannot get them out of step
        cls,
        *,
        document_id: str,
        ordinal: int,
        text: str,
        char_start: int,
        char_end: int,
        source_type: SourceType,
        ingested_at: datetime,
        section: str | None = None,
        page: int | None = None,
        token_count: int | None = None,
        legacy_chunk_id: str | None = None,
    ) -> EvidenceChunk:
        """Construct a chunk, deriving ``chunk_id`` and ``content_sha256`` from the inputs."""
        from synapse.identifiers import (
            make_chunk_id,  # Imported here rather than at module scope purely to keep the import graph acyclic
        )

        normalized = text  # Caller is responsible for normalisation; the validator above enforces it, so we do not silently rewrite
        return cls(
            chunk_id=make_chunk_id(
                document_id, ordinal
            ),  # Derived, never passed in — removes the possibility of drift
            document_id=document_id,
            ordinal=ordinal,
            text=normalized,
            content_sha256=sha256_text(normalized),  # Derived from the same string that is stored
            char_start=char_start,
            char_end=char_end,
            source_type=source_type,
            section=section,
            page=page,
            token_count=token_count,
            ingested_at=ingested_at,
            legacy_chunk_id=legacy_chunk_id,
        )


__all__ = ["EvidenceChunk"]

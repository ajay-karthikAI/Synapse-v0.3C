"""
synapse.identifiers
===================
Deterministic identifier derivation.

The identifier scheme fixes a concrete, verified defect in the current corpus.
The legacy scheme was ``f"{pmid}_chunk{chunk_index}"``, where ``chunk_index``
restarts at 0 for every document. Because ``build_corpus.py`` fetches the same
PubMed article under several topic queries, the same article is appended to the
corpus more than once with the same indices. Measured on the committed
artifact: 2,220 chunks collapse to only 2,058 unique identifiers — 162
collisions. Those collisions silently corrupt hybrid scoring, because
``Retrieval/hybrid_retriever.py`` builds a ``{chunk_id: score}`` map and
colliding keys overwrite one another (docs/quality-architecture.md §1.2, C6/C7).

The scheme here makes collisions structurally impossible rather than merely
unlikely:

    document_id := "<scheme>:<key>"          e.g. "pubmed:41802233"
    chunk_id    := "<document_id>#<ordinal>" e.g. "pubmed:41802233#0002"

where ``ordinal`` is the position of the chunk within *the emitted stream for
that document*, not within an individual fetch. A document that is ingested
twice therefore continues numbering (0,1,2,3,4,5…) instead of restarting.

Every function here is pure and deterministic: identical inputs always yield
identical identifiers, across processes, machines and runs. That property is
what allows a corpus to be rebuilt and still verify against a stored manifest.
"""

from __future__ import annotations  # Postponed annotations

import re  # Identifier shape validation

from synapse.errors import ArtifactSchemaError  # Malformed identifiers are a schema-level failure
from synapse.hashing import (  # Content-addressed identifiers for sources without a natural key
    sha256_text,
    short_digest,
)

# ---------------------------------------------------------------------------
# Identifier grammars
# ---------------------------------------------------------------------------

DOCUMENT_ID_SCHEMES = (
    "pubmed",
    "guideline",
    "pdf",
    "txt",
    "example",
)  # Closed vocabulary of identifier namespaces; adding one is a deliberate schema change.
# "example" exists so demonstration content carries an identifier that cannot be
# mistaken for a real citation. Nothing in an "example:" namespace may enter a
# production index — see synapse.governance.eligibility.

DOCUMENT_ID_PATTERN = re.compile(  # Full-match grammar for a document identifier
    r"^(?P<scheme>pubmed|guideline|pdf|txt|example):(?P<key>[A-Za-z0-9][A-Za-z0-9._:\-]{0,127})$"
)  # Key charset excludes '#' so it can never be confused with the chunk separator, and excludes whitespace/slashes so an identifier can never be used as a path component

CHUNK_ID_PATTERN = re.compile(  # A chunk identifier is a document identifier plus a zero-padded ordinal
    r"^(?P<document_id>(?:pubmed|guideline|pdf|txt|example):[A-Za-z0-9][A-Za-z0-9._:\-]{0,127})#(?P<ordinal>\d{4,8})$"
)  # At least four digits keeps lexical sort order equal to numeric order for corpora up to 9,999 chunks per document

REVIEWER_ID_PATTERN = re.compile(
    r"^rev_[a-z0-9]{8}$"
)  # Pseudonymous reviewer identifier; the real-identity mapping is held OUTSIDE this repository, so no name, email or credential can ever be committed

CONTENT_KEY_LENGTH = 16  # Hex characters of SHA-256 retained for content-addressed identifiers; 16 hex chars = 64 bits, ample for corpus-scale collision resistance

ORDINAL_WIDTH = 4  # Zero-padding width for chunk ordinals


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def make_document_id(
    scheme: str, key: str
) -> str:  # Low-level constructor; prefer the typed helpers below
    """Build and validate a document identifier from a scheme and a natural key."""
    if (
        scheme not in DOCUMENT_ID_SCHEMES
    ):  # Reject unknown namespaces rather than silently accepting them
        raise ArtifactSchemaError(field="document_id", problem="unknown scheme", scheme=scheme)
    candidate = f"{scheme}:{key.strip()}"  # Strip incidental whitespace from the key before assembly, so " 123" and "123" agree
    if not DOCUMENT_ID_PATTERN.match(
        candidate
    ):  # Validate the assembled string against the full grammar
        raise ArtifactSchemaError(
            field="document_id", problem="malformed", scheme=scheme
        )  # The key itself is NOT echoed: it may be a filename containing a patient or user name
    return candidate


def document_id_for_pubmed(pmid: str) -> str:  # PubMed articles have a natural, globally stable key
    """Document identifier for a PubMed record, e.g. ``pubmed:41802233``."""
    cleaned = pmid.strip()  # Tolerate surrounding whitespace from XML text nodes
    if not cleaned.isdigit():  # PMIDs are numeric; anything else means the upstream parse went wrong and must not be silently accepted
        raise ArtifactSchemaError(field="pmid", problem="not numeric", length=len(cleaned))
    return make_document_id("pubmed", cleaned)


def document_id_for_content(
    scheme: str, content: str
) -> str:  # For sources with no stable external identifier (a PDF, a pasted text file)
    """Content-addressed document identifier, e.g. ``pdf:6b86b273ff34fce1``.

    Derived from the SHA-256 of the normalised content, so the same file
    ingested twice yields the same identifier and deduplicates naturally.
    """
    key = short_digest(
        sha256_text(content), CONTENT_KEY_LENGTH
    )  # Truncated content digest becomes the key
    return make_document_id(scheme, key)


def make_chunk_id(
    document_id: str, ordinal: int
) -> str:  # The identifier that fixes the C6/C7 collisions
    """Build a chunk identifier from its parent document identifier and ordinal."""
    if not DOCUMENT_ID_PATTERN.match(
        document_id
    ):  # A chunk identifier is only valid if its document identifier is
        raise ArtifactSchemaError(field="chunk_id", problem="malformed parent document_id")
    if ordinal < 0:  # Negative ordinals would break the zero-padded grammar and imply a caller bug
        raise ArtifactSchemaError(field="chunk_id", problem="negative ordinal", ordinal=ordinal)
    return f"{document_id}#{ordinal:0{ORDINAL_WIDTH}d}"  # Zero-padded so lexical ordering matches numeric ordering


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def parse_chunk_id(
    chunk_id: str,
) -> tuple[
    str, int
]:  # Inverse of make_chunk_id; used to group chunks by document without a separate field
    """Split a chunk identifier into ``(document_id, ordinal)``."""
    match = CHUNK_ID_PATTERN.match(chunk_id)  # Full-match against the grammar
    if (
        match is None
    ):  # Fail closed on anything that does not parse — a partially-understood identifier is worse than a rejected one
        raise ArtifactSchemaError(field="chunk_id", problem="malformed")
    return match.group("document_id"), int(
        match.group("ordinal")
    )  # Ordinal is returned as an int for arithmetic


def is_valid_document_id(
    value: str,
) -> bool:  # Boolean form for pydantic validators, which prefer predicates over exceptions
    """True when ``value`` is a well-formed document identifier."""
    return DOCUMENT_ID_PATTERN.match(value) is not None


def is_valid_chunk_id(value: str) -> bool:  # Boolean form for pydantic validators
    """True when ``value`` is a well-formed chunk identifier."""
    return CHUNK_ID_PATTERN.match(value) is not None


def is_valid_reviewer_id(
    value: str,
) -> bool:  # Enforces pseudonymity of reviewer identifiers at the schema boundary
    """True when ``value`` is a well-formed pseudonymous reviewer identifier."""
    return REVIEWER_ID_PATTERN.match(value) is not None


__all__ = [
    "CHUNK_ID_PATTERN",
    "DOCUMENT_ID_PATTERN",
    "DOCUMENT_ID_SCHEMES",
    "REVIEWER_ID_PATTERN",
    "document_id_for_content",
    "document_id_for_pubmed",
    "is_valid_chunk_id",
    "is_valid_document_id",
    "is_valid_reviewer_id",
    "make_chunk_id",
    "make_document_id",
    "parse_chunk_id",
]

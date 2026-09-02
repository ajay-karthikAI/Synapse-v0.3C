"""
synapse.normalize
=================
Hash-stable text normalisation.

Every content hash in the system is computed over *normalised* text rather
than raw text. Without this, a corpus that was merely re-serialised — different
newline convention, a stray non-breaking space, a decomposed accent from a
different XML parser — would produce a different SHA-256 and fail integrity
verification even though not one character of meaning had changed.

The normalisation here is deliberately conservative: it collapses whitespace
and canonicalises Unicode form, and does nothing else. It does NOT lowercase,
strip punctuation, or remove stopwords — those are retrieval concerns handled
by the tokenizer, and applying them here would make the stored text differ from
what a patient is shown, breaking verbatim excerpt verification.
"""

from __future__ import annotations  # Postponed annotations; module has no runtime type dependencies

import re  # Whitespace collapsing is a single regex pass
import unicodedata  # Unicode canonical composition (NFC)

_WHITESPACE_RUN = re.compile(
    r"\s+"
)  # Matches any run of whitespace, including newlines, tabs and Unicode spaces; precompiled because this runs once per chunk over the whole corpus


def normalize_text(
    text: str,
) -> str:  # The single normalisation entry point used everywhere a hash is taken
    """Return a canonical form of ``text`` suitable for stable hashing.

    Steps, in order:
      1. NFC composition  — "e" + combining-acute and "é" become the same string.
      2. Whitespace runs  — collapsed to a single ASCII space.
      3. Trim             — leading/trailing whitespace removed.

    The function is idempotent: ``normalize_text(normalize_text(x)) == normalize_text(x)``.
    """
    composed = unicodedata.normalize(
        "NFC", text
    )  # Canonical composition; without it, visually identical strings from different sources hash differently
    collapsed = _WHITESPACE_RUN.sub(
        " ", composed
    )  # Any whitespace run becomes exactly one space, so CRLF vs LF vs double-space cannot change the digest
    return (
        collapsed.strip()
    )  # Remove leading/trailing space introduced by the collapse or present in the source


def is_normalized(
    text: str,
) -> bool:  # Cheap predicate used by validators to reject un-normalised input at the model boundary
    """True when ``text`` is already in canonical form."""
    return (
        normalize_text(text) == text
    )  # Idempotence makes this a valid check without a separate implementation


__all__ = ["is_normalized", "normalize_text"]

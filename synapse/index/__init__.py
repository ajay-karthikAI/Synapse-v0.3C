"""
synapse.index
=============
Index manifest construction and fail-closed verification.

FAISS persistence is kept exactly as it is — a ``.faiss`` file written by
``faiss.write_index`` — because that format is fine: it holds vectors, not
executable objects. What is added is the manifest beside it, and the rule that
**nothing loads an index without verifying it first**.

The failure this exists to prevent is not a crash. It is an index whose row
order no longer matches the corpus, which produces confident, well-formatted
answers citing the wrong paper. That is invisible from the UI, which is why it
has to be caught by a digest at load time rather than by review.
"""

from __future__ import annotations  # Postponed annotations

from synapse.index.manifest import (
    FaissHeader,
    build_index_manifest,
    detect_git_commit,
    probe_faiss_header,
    read_manifest,
    write_manifest,
)
from synapse.index.verify import (
    MANIFEST_FILENAME,
    VerificationReport,
    load_verified_corpus,
    verify_artifacts,
)

__all__ = [
    "MANIFEST_FILENAME",
    "FaissHeader",
    "VerificationReport",
    "build_index_manifest",
    "detect_git_commit",
    "load_verified_corpus",
    "probe_faiss_header",
    "read_manifest",
    "verify_artifacts",
    "write_manifest",
]

"""
synapse
=======
Foundational data and artifact layer for the Synapse medical RAG application.

This package is deliberately independent of the retrieval/generation stack:
it can be imported, validated and tested with nothing installed but pydantic.
That property is what lets artifact integrity be checked in CI without a
FAISS build, an OpenAI key, or any network access.

Layout (all lowercase, single canonical import root — see
docs/quality-architecture.md §1.1 for the case-sensitivity bug this prevents):

    synapse.errors        typed, user-safe exception hierarchy
    synapse.hashing       SHA-256 helpers over text, bytes and files
    synapse.normalize     hash-stable text normalisation
    synapse.identifiers   deterministic document / chunk identifier derivation
    synapse.schemas       typed pydantic models for every persisted record
    synapse.corpus        JSONL read/write for corpus records (never pickle)
    synapse.index         index manifest construction and fail-closed verification
    synapse.cli           one-way migration entry points
    synapse._legacy       quarantined pickle reader; migration use only
"""

from __future__ import (
    annotations,  # Postponed annotation evaluation: keeps type hints as strings, so importing this package never forces heavy imports
)

__version__ = "0.1.0"  # Package version; recorded into every IndexManifest so an artifact can be traced to the code that wrote it

__all__ = [
    "__version__"
]  # Explicit public surface; keeps `from synapse import *` from leaking submodule names

"""
synapse.corpus
==============
Corpus persistence. JSONL only — never pickle.

Why the format changed
----------------------
The existing corpus is four pickle files that are ``pickle.load``-ed at app
startup. Unpickling executes the constructors named in the byte stream, so a
modified artifact is arbitrary code execution inside the application process.
An opcode audit of the shipped files shows they currently reference only
``Data.fetch_and_chunk.Chunk`` and ``rank_bm25.BM25Okapi`` — the files are
benign today; the *format* is the exposure, and it cannot be made safe.

Pickle also couples artifacts to source layout: the stream names
``Data.fetch_and_chunk.Chunk``, so renaming the package makes every existing
artifact unloadable.

JSONL fixes both. It is inert (parsing it can never execute code), diffable,
streamable, and independent of Python class layout. Gzip is applied for files
over about a megabyte, with ``mtime=0`` so compression is byte-deterministic
and a rebuilt corpus hashes identically.

Parquet is deliberately *not* used as the source of truth: it would add a
~40 MB ``pyarrow`` dependency to a package whose whole point is being
importable and verifiable with nothing but pydantic installed. A Parquet
export for analysis can be layered on later without changing this contract.
"""

from __future__ import annotations  # Postponed annotations

from synapse.corpus.jsonl import (
    JsonlReadError,
    canonical_record_lines,
    read_jsonl,
    records_digest,
    write_jsonl,
)

__all__ = [
    "JsonlReadError",
    "canonical_record_lines",
    "read_jsonl",
    "records_digest",
    "write_jsonl",
]

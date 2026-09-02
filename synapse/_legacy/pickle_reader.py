"""
synapse._legacy.pickle_reader
=============================
Restricted, migration-only reader for legacy corpus pickles.

**This is the only module in Synapse permitted to import ``pickle``**, and the
only one exempted from the linter's ban on it (see ``pyproject.toml``
``per-file-ignores``). No runtime application path may import it.

Why a restricted unpickler rather than ``pickle.load``
------------------------------------------------------
``pickle.load`` executes the callables named in the byte stream. A crafted
pickle can therefore run arbitrary code at load time; this is a property of the
format, not a bug in any particular file. :class:`_RestrictedUnpickler`
overrides ``find_class`` with a strict allow-list, so a stream naming anything
other than the legacy ``Chunk`` dataclass is refused *before* that name is
resolved — the dangerous callable is never imported, let alone called.

This reduces, but does not eliminate, the risk: a stream restricted to
permitted classes can still be crafted to consume large amounts of memory. That
residual risk is why the migration CLI additionally requires the operator to
assert the file's provenance with ``--trust-input``, and why this reader
enforces a file-size ceiling.

An opcode audit of the artifacts currently in the repository shows they
reference only ``Data.fetch_and_chunk.Chunk`` and ``rank_bm25.BM25Okapi``.
Only the former is on the allow-list; BM25 state is deliberately *not* migrated
(a BM25 index is cheap to rebuild from the JSONL corpus, so there is no reason
to carry a pickle forward for it).
"""

from __future__ import annotations  # Postponed annotations

import io  # Reading the pickle from an in-memory buffer, so the size ceiling is enforced before parsing
import pickle  # ALLOWED HERE ONLY: this module is the quarantined migration reader; use is gated by _RestrictedUnpickler below
from pathlib import Path
from typing import Any, ClassVar

from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError, UnsafeArtifactError

MAX_PICKLE_BYTES = (
    512 * 1024 * 1024
)  # 512 MiB ceiling. The shipped corpus is ~1 MB; this bounds memory if a hostile or corrupt file claims to be far larger.

_ALLOWED_GLOBALS: frozenset[tuple[str, str]] = (
    frozenset(  # Exhaustive allow-list of (module, name) pairs the unpickler may resolve
        {
            (
                "Data.fetch_and_chunk",
                "Chunk",
            ),  # The legacy dataclass, as named by the artifacts currently in the repository
            (
                "synapse.data.fetch_and_chunk",
                "Chunk",
            ),  # The same class under the canonical lowercase package, for artifacts written after a rename
        }
    )
)

_LEGACY_FIELDS: frozenset[str] = frozenset(  # Field names the legacy dataclass is known to carry
    {"text", "source", "pmid", "title", "chunk_index", "total_chunks", "source_url", "page"}
)


class LegacyChunk:
    """Inert stand-in for the legacy ``Chunk`` dataclass.

    Deliberately *not* the real class: it defines no behaviour, no ``__reduce__``
    and no imports of its own, so reconstructing it can have no side effects
    beyond populating attributes.
    """

    __slots__ = tuple(
        sorted(_LEGACY_FIELDS)
    )  # Fixed attribute set: a stream carrying an unexpected field cannot silently attach it

    text: str
    source: str
    pmid: str
    title: str
    chunk_index: int
    total_chunks: int
    source_url: str
    page: int

    def __setstate__(self, state: Any) -> None:  # Pickle calls this to populate the instance
        """Populate from the pickled state dict, rejecting anything unexpected."""
        if not isinstance(
            state, dict
        ):  # A non-dict state means the stream does not match the legacy dataclass shape
            raise ArtifactSchemaError(problem="legacy chunk state is not a mapping")
        unexpected = set(state) - _LEGACY_FIELDS  # Any field the legacy class never had
        if unexpected:
            raise ArtifactSchemaError(
                problem="legacy chunk carries unexpected fields", count=len(unexpected)
            )
        for (
            field
        ) in _LEGACY_FIELDS:  # Populate every known field, defaulting the ones this stream omitted
            setattr(
                self,
                field,
                state.get(field, "" if field not in ("chunk_index", "total_chunks", "page") else 0),
            )

    def __repr__(
        self,
    ) -> str:  # Deliberately does NOT include text: chunk bodies are third-party content and must not leak into logs or tracebacks
        return f"LegacyChunk(pmid={getattr(self, 'pmid', '')!r}, chunk_index={getattr(self, 'chunk_index', -1)!r})"


class _RestrictedUnpickler(pickle.Unpickler):
    """An unpickler that refuses to resolve any global outside the allow-list."""

    allowed: ClassVar[frozenset[tuple[str, str]]] = _ALLOWED_GLOBALS

    def find_class(
        self, module: str, name: str
    ) -> Any:  # Overridden pickle hook; docstring lives on the class
        # This is the single security-critical method in the module. It runs
        # BEFORE the named object is imported, so refusing here means a
        # dangerous callable is never resolved, never imported and never called.
        if (module, name) in self.allowed:  # Exact tuple match; no prefix matching, no wildcards
            return LegacyChunk  # Every permitted name maps to the inert stand-in, never to the real class
        raise UnsafeArtifactError(  # Refusal, not a fallback: there is no permissive branch
            problem="pickle stream references a disallowed global",
            module=module,  # Safe to report: this is a module name from the stream, not payload content
            name=name,
        )


def load_legacy_chunks(path: Path) -> list[LegacyChunk]:
    """Read a legacy corpus pickle through the restricted unpickler.

    Args:
        path: the ``.pkl`` file to read. **Must come from a trusted source.**

    Returns:
        The legacy chunk records, in file order.

    Raises:
        ArtifactNotFoundError: the file does not exist.
        UnsafeArtifactError: the file exceeds the size ceiling, or references a
            global outside the allow-list.
        ArtifactSchemaError: the stream does not contain a list of legacy chunks.
    """
    if not path.is_file():
        raise ArtifactNotFoundError(path=path)

    size = (
        path.stat().st_size
    )  # Check the size BEFORE reading, so an oversized file is refused rather than loaded
    if size > MAX_PICKLE_BYTES:
        raise UnsafeArtifactError(
            problem="pickle exceeds maximum permitted size",
            size_bytes=size,
            limit_bytes=MAX_PICKLE_BYTES,
        )

    payload = path.read_bytes()  # Bounded by the check above
    try:
        loaded = _RestrictedUnpickler(
            io.BytesIO(payload)
        ).load()  # The allow-list is enforced inside find_class during this call
    except UnsafeArtifactError:  # Our own refusal: propagate unchanged so the operator sees exactly which global was rejected
        raise
    except (
        pickle.UnpicklingError,
        EOFError,
        AttributeError,
        ImportError,
        IndexError,
        ValueError,
    ) as exc:
        # Narrow catch of the documented failure modes of a corrupt stream. A
        # blanket `except Exception` is avoided deliberately: it would swallow
        # the UnsafeArtifactError re-raised above and mask a genuine refusal.
        raise ArtifactSchemaError(
            path=path, problem="legacy pickle is corrupt or truncated"
        ) from exc

    if not isinstance(loaded, list):  # The legacy writer always dumped a list of chunks
        raise ArtifactSchemaError(path=path, problem="legacy pickle does not contain a list")
    for item in loaded:  # Verify every element really is a stand-in instance, not something the allow-list let through in another shape
        if not isinstance(item, LegacyChunk):
            raise ArtifactSchemaError(
                path=path, problem="legacy pickle contains a non-chunk element"
            )
    return loaded


__all__ = ["MAX_PICKLE_BYTES", "LegacyChunk", "load_legacy_chunks"]

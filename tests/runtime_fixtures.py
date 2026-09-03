"""
Offline builders for a complete runtime artifact and archive.

Everything the deployed container loads, constructed in a temporary directory
with no network, no credentials, no FAISS and no corpus on disk. The FAISS
"index" is a valid header followed by zeroes: enough for
:func:`~synapse.index.manifest.probe_faiss_header` to read a dimensionality and
a vector count, which is what verification actually inspects.

These builders are deliberately parameterised on the ways an artifact can be
*wrong* — a reordered corpus, a mismatched vector count, an incompatible
schema, a tampered manifest — because those are the cases the loader exists to
refuse, and each needs a real artifact that fails in exactly one way.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from synapse.corpus import write_jsonl
from synapse.hashing import sha256_file, sha256_text
from synapse.index import MANIFEST_FILENAME, build_index_manifest, write_manifest
from synapse.index.manifest import make_artifact
from synapse.normalize import normalize_text
from synapse.runtime.archive import pack_archive
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import DistanceMetric, SourceType
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig, IndexManifest
from synapse.schemas.source import SourceDocument

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SOURCE_PACK_VERSION = "diabetes-previsit@0.1.0"
DIMENSIONS = 8  # Small: the header is all that is read, and 1536 zeroes prove nothing extra

# Texts chosen so BM25 has something to discriminate on, including the
# hyphenated and numeric terms the legacy tokenizer preserves.
CORPUS_TEXTS: tuple[tuple[str, str], ...] = (
    ("pubmed:41802233", "Metformin 500mg is a first-line oral therapy for type 2 diabetes."),
    ("pubmed:41802233", "HbA1c reflects average plasma glucose over 2-3 months."),
    ("pubmed:41900001", "SGLT2 inhibitors such as empagliflozin reduce cardiovascular mortality."),
    ("pubmed:41900001", "ICD-10 code E11.9 refers to type 2 diabetes without complications."),
)


def faiss_stub(dimensions: int, vector_count: int) -> bytes:
    """A minimal valid ``IndexFlatL2`` header: fourcc, int32 dim, int64 count."""
    return (
        b"IxF2"
        + dimensions.to_bytes(4, "little")
        + vector_count.to_bytes(8, "little")
        + b"\x00" * 32
    )


def build_chunks(texts: tuple[tuple[str, str], ...] = CORPUS_TEXTS) -> list[EvidenceChunk]:
    """Ordered corpus records. Order is the contract; this list defines it."""
    chunks: list[EvidenceChunk] = []
    ordinals: dict[str, int] = {}
    for document_id, raw in texts:
        ordinal = ordinals.get(document_id, 0)
        ordinals[document_id] = ordinal + 1
        normalized = normalize_text(raw)
        chunks.append(
            EvidenceChunk.build(
                document_id=document_id,
                ordinal=ordinal,
                text=normalized,
                char_start=0,
                char_end=len(normalized),
                source_type=SourceType.PUBMED_ABSTRACT,
                ingested_at=FIXED_TIME,
            )
        )
    return chunks


def build_documents(chunks: list[EvidenceChunk]) -> list[SourceDocument]:
    """One record per distinct document, supplying the titles and links."""
    titles = {
        "pubmed:41802233": "Metformin and HbA1c in Type 2 Diabetes",
        "pubmed:41900001": "SGLT2 Inhibitors and Cardiovascular Outcomes",
    }
    seen: list[str] = []
    for chunk in chunks:
        if chunk.document_id not in seen:
            seen.append(chunk.document_id)
    return [
        SourceDocument(
            document_id=document_id,
            source_type=SourceType.PUBMED_ABSTRACT,
            source_url=f"https://pubmed.ncbi.nlm.nih.gov/{document_id.split(':')[1]}/",
            title=titles.get(document_id, document_id),
            retrieved_at=FIXED_TIME,
            content_sha256=sha256_text(document_id),
            source_pack_version=SOURCE_PACK_VERSION,
        )
        for document_id in seen
    ]


def build_artifact_dir(
    directory: Path,
    *,
    chunks: list[EvidenceChunk] | None = None,
    dimensions: int = DIMENSIONS,
    include_documents: bool = True,
    source_pack_version: str | None = SOURCE_PACK_VERSION,
) -> Path:
    """Write a complete, verifiable runtime artifact into ``directory``.

    Always internally consistent. The ways an artifact can be *wrong* are
    applied afterwards by the mutators below, so each test damages exactly one
    property and the check it is about is the one that fires.
    """
    directory.mkdir(parents=True, exist_ok=True)
    records = chunks if chunks is not None else build_chunks()

    chunks_path = directory / "chunks.jsonl.gz"
    write_jsonl(chunks_path, records)

    artifacts = [make_artifact(chunks_path, "chunks", relative_to=directory)]

    if include_documents:
        documents_path = directory / "documents.jsonl.gz"
        write_jsonl(documents_path, build_documents(records))
        artifacts.append(make_artifact(documents_path, "documents", relative_to=directory))

    faiss_path = directory / "index.faiss"
    faiss_path.write_bytes(faiss_stub(dimensions, len(records)))
    artifacts.append(make_artifact(faiss_path, "faiss", relative_to=directory))

    manifest = build_index_manifest(
        index_id="runtime-test-1",
        index_version="2",
        corpus_version="corpus-test",
        chunks=records,
        artifacts=artifacts,
        embedding=EmbeddingConfig(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=dimensions,
            l2_normalized=True,
        ),
        chunking=ChunkingConfig(
            algorithm="recursive_character", chunk_size=500, overlap=100, min_chunk_chars=0
        ),
        distance_metric=DistanceMetric.L2,
        document_count=len({chunk.document_id for chunk in records}),
        faiss_path=faiss_path,
        faiss_index_type="IndexFlatL2",
        source_pack_version=source_pack_version,
        built_at=FIXED_TIME,
    )
    write_manifest(directory / MANIFEST_FILENAME, manifest)
    return directory


def build_archive(tmp_path: Path, **kwargs: object) -> tuple[Path, str]:
    """Build an artifact and pack it. Returns ``(archive_path, sha256)``."""
    source = build_artifact_dir(tmp_path / "artifact", **kwargs)  # type: ignore[arg-type]
    archive = tmp_path / "synapse-runtime-2.tar.gz"
    digest = pack_archive(source, archive)
    return archive, digest


def rewrite_manifest(directory: Path, **changes: object) -> IndexManifest:
    """Edit manifest fields and re-seal the self-digest.

    Used to build artifacts that are internally consistent but *wrong* — a
    declared vector count that does not match, an incompatible schema version —
    so the test exercises the structural check rather than the digest check that
    would otherwise fire first.
    """
    path = directory / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    payload["manifest_sha256"] = None
    manifest = IndexManifest.model_validate(payload).with_self_digest()
    write_manifest(path, manifest)
    return manifest


def rewrite_manifest_raw(directory: Path, **changes: object) -> None:
    """Edit manifest JSON *without* model validation.

    Needed for the cases the schema itself makes unconstructible — an
    incompatible ``schema_version``, for instance. ``IndexManifest`` refuses to
    build one, which is correct and is why the artifact has to be written
    directly to test that the *reader* refuses it too.
    """
    path = directory / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def corrupt_faiss_header(directory: Path, *, vectors: int, dimensions: int = DIMENSIONS) -> None:
    """Rewrite the index header to disagree with the manifest, and re-digest it.

    The realistic shape of a misalignment: the manifest is internally consistent
    (the schema enforces ``vector_count == chunk_count``), and it is the *index
    file* that has the wrong number of rows. Re-hashing means the file digest
    passes, so the header cross-check is what fails.
    """
    faiss_path = directory / "index.faiss"
    faiss_path.write_bytes(faiss_stub(dimensions, vectors))
    rehash_artifact(directory, "faiss")


def truncate_corpus(directory: Path, keep: int) -> None:
    """Drop records from the corpus, re-digesting the file but not the manifest.

    The manifest still declares the original ``chunk_count``, so the count check
    inside deep verification is what refuses it.
    """
    write_jsonl(directory / "chunks.jsonl.gz", read_chunks(directory)[:keep])
    rehash_artifact(directory, "chunks")


def reorder_corpus(directory: Path) -> None:
    """Reverse the corpus while leaving the manifest describing the original order.

    THE case a content digest alone cannot catch: every record is individually
    valid and the *set* is identical, so only the ordering digest reveals that
    FAISS row *i* no longer corresponds to corpus record *i*. Both the file
    digest and the content digest are updated, so the ordering check is the only
    one left to fail — which is what proves the two digests are independently
    load-bearing.
    """
    from synapse.corpus import records_digest

    chunks_path = directory / "chunks.jsonl.gz"
    reordered = list(reversed(list(read_chunks(directory))))
    write_jsonl(chunks_path, reordered)
    rehash_artifact(directory, "chunks")
    rewrite_manifest(directory, corpus_sha256=records_digest(reordered))


def read_chunks(directory: Path) -> list[EvidenceChunk]:
    """Read the corpus back out of an artifact directory, in file order."""
    from synapse.corpus.jsonl import read_jsonl

    return list(read_jsonl(directory / "chunks.jsonl.gz", EvidenceChunk))


def rehash_artifact(directory: Path, role: str) -> None:
    """Re-record one artifact's digest after its bytes were changed.

    Without this, a test that reorders the corpus would fail on the file digest
    and never reach the ordering check it is actually about.
    """
    path = directory / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    for artifact in payload["artifacts"]:
        if artifact["role"] == role:
            target = directory / artifact["path"]
            artifact["sha256"] = sha256_file(target)
            artifact["size_bytes"] = target.stat().st_size
    payload["manifest_sha256"] = None
    write_manifest(path, IndexManifest.model_validate(payload).with_self_digest())


__all__ = [
    "CORPUS_TEXTS",
    "DIMENSIONS",
    "FIXED_TIME",
    "SOURCE_PACK_VERSION",
    "build_archive",
    "build_artifact_dir",
    "build_chunks",
    "build_documents",
    "corrupt_faiss_header",
    "faiss_stub",
    "read_chunks",
    "rehash_artifact",
    "reorder_corpus",
    "rewrite_manifest",
    "rewrite_manifest_raw",
    "truncate_corpus",
]

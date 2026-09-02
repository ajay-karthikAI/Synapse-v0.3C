"""
synapse.cli.migrate_artifacts
=============================
One-way migration from legacy pickle corpora to verified JSONL artifacts.

Usage::

    python -m synapse.cli.migrate_artifacts \\
        --input processed_chunks.pkl \\
        --output-dir artifacts/corpus-v2 \\
        --corpus-version corpus-v2 \\
        --trust-input

The migration is:

* **one-way** — it reads the pickle and writes new files; it never writes a
  pickle, and never modifies or deletes the input;
* **non-destructive** — the legacy artifacts remain on disk, which is what
  makes rollback a matter of changing a path rather than restoring data;
* **gated** — it refuses to run without ``--trust-input``, because unpickling
  is only ever as safe as the file's provenance.

Ordering and the FAISS index
----------------------------
The legacy corpus contains duplicate documents: the same article was fetched
under several topic queries, producing 162 colliding chunk identifiers across
2,220 chunks. Removing them changes the number and order of records — and an
existing FAISS index's row *i* is bound to corpus record *i*.

So deduplication and index reuse are mutually exclusive, and the CLI enforces
that rather than letting an operator silently misalign an index:

* default (no ``--dedupe``): every legacy chunk is preserved in its original
  order, identifiers are re-derived to be unique, and an existing FAISS index
  stays valid;
* ``--dedupe``: duplicate documents are dropped, and the manifest records
  ``index_state = "rebuild_required"``. Passing ``--faiss-index`` alongside
  ``--dedupe`` is refused.
"""

from __future__ import annotations  # Postponed annotations

import argparse  # Standard-library argument parsing; no third-party CLI framework needed
import shutil  # Copying the FAISS index into the output directory
import sys  # Exit codes
from collections import (  # Duplicate detection and per-document ordinal counters
    Counter,
    defaultdict,
)
from datetime import UTC, datetime
from pathlib import Path

from synapse import __version__ as synapse_version
from synapse.corpus.jsonl import write_jsonl
from synapse.errors import SynapseArtifactError
from synapse.hashing import sha256_text
from synapse.identifiers import document_id_for_content, document_id_for_pubmed
from synapse.index.manifest import build_index_manifest, make_artifact, write_manifest
from synapse.index.verify import verify_artifacts
from synapse.normalize import normalize_text
from synapse.schemas.chunk import EvidenceChunk
from synapse.schemas.enums import ChunkingAlgorithm, DistanceMetric, SourceType
from synapse.schemas.index import ChunkingConfig, EmbeddingConfig
from synapse.schemas.source import DocumentIdentifiers, SourceContainer, SourceDocument

CHUNKS_FILENAME = (
    "chunks.jsonl.gz"  # Corpus chunk records; gzipped because the shipped corpus is ~1 MB of text
)
DOCUMENTS_FILENAME = "documents.jsonl.gz"  # Source-document records derived from the same input
FAISS_FILENAME = (
    "index.faiss"  # Canonical name for the copied vector index inside the output directory
)

_PUBMED_URL_TEMPLATE = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"  # Used only when a legacy record has no URL of its own

TRUST_WARNING = """\
============================================================================
  WARNING — PICKLE INPUT MUST COME FROM A TRUSTED SOURCE
============================================================================
  This command reads a Python pickle file. Unpickling is not a safe operation
  on untrusted data: the format can name arbitrary callables to be executed
  during loading.

  Synapse mitigates this with a restricted unpickler that refuses any global
  outside a two-entry allow-list, so a stream naming an unexpected class is
  rejected before that class is resolved. That mitigation is strong, but it is
  not a substitute for provenance.

  Only run this command on a pickle file that YOU produced, or that came from
  a source you control. Do not run it on a file received from a third party,
  downloaded from the internet, or restored from an untrusted backup.

  The legacy input is never modified or deleted. To roll back, point the
  application at the original artifacts again — see docs/artifact-format.md.
============================================================================
"""


class MigrationReport:  # Plain class rather than a pydantic model: this is a console summary, not a persisted artifact
    """Counts describing what a migration did, printed for the operator."""

    def __init__(self) -> None:
        self.legacy_chunks_read = 0  # Records found in the pickle
        self.chunks_written = 0  # Records emitted to JSONL
        self.documents_written = 0  # Distinct source documents emitted
        self.duplicate_chunk_ids_resolved = (
            0  # Legacy identifier collisions repaired by re-derivation
        )
        self.duplicate_texts_detected = (
            0  # Chunks whose normalised text is byte-identical to an earlier chunk
        )
        self.duplicate_documents_removed = 0  # Documents dropped by --dedupe
        self.chunks_dropped_too_short = 0  # Chunks below --min-chunk-chars
        self.chunks_dropped_empty = 0  # Chunks that normalise to an empty string
        self.documents_skipped_no_url = (
            0  # Documents with no usable http(s) URL, which SourceDocument requires
        )

    def render(
        self,
    ) -> str:  # Formatted as aligned key/value lines so it reads well in a terminal and in CI logs
        """Render the report as a human-readable block."""
        rows = [
            ("legacy chunks read", self.legacy_chunks_read),
            ("chunks written", self.chunks_written),
            ("documents written", self.documents_written),
            ("duplicate chunk ids resolved", self.duplicate_chunk_ids_resolved),
            ("duplicate texts detected", self.duplicate_texts_detected),
            ("duplicate documents removed", self.duplicate_documents_removed),
            ("chunks dropped (too short)", self.chunks_dropped_too_short),
            ("chunks dropped (empty)", self.chunks_dropped_empty),
            ("documents skipped (no url)", self.documents_skipped_no_url),
        ]
        width = max(len(label) for label, _ in rows)  # Align the values into a column
        return "\n".join(f"  {label.ljust(width)} : {value}" for label, value in rows)


def _build_parser() -> (
    argparse.ArgumentParser
):  # Separated from main() so tests can parse argument lists without executing
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m synapse.cli.migrate_artifacts",
        description="One-way migration of legacy pickle corpora to verified JSONL artifacts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,  # Preserve the newlines in the epilog
        epilog="The input pickle is never modified. See docs/artifact-format.md for the rollback procedure.",
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="Legacy .pkl corpus file to read."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write chunks, documents and manifest into.",
    )
    parser.add_argument(
        "--corpus-version",
        required=True,
        help="Version label for the migrated corpus, e.g. 'corpus-v2'.",
    )
    parser.add_argument(
        "--index-version", default="1", help="Version label for the index build. Default: 1."
    )
    parser.add_argument(
        "--source-pack-version",
        default=None,
        help="Source pack version to stamp on migrated documents. Defaults to the corpus version.",
    )

    parser.add_argument(
        "--trust-input",
        action="store_true",
        help="REQUIRED. Asserts that the input pickle comes from a trusted source. Without it the command refuses to run.",
    )

    parser.add_argument(
        "--faiss-index",
        type=Path,
        default=None,
        help="Existing FAISS index to copy and record. Incompatible with --dedupe.",
    )
    parser.add_argument(
        "--dedupe",
        action="store_true",
        help="Drop duplicate documents. Invalidates any existing FAISS index, so the manifest records 'rebuild_required'.",
    )
    parser.add_argument(
        "--min-chunk-chars",
        type=int,
        default=0,
        help="Drop chunks shorter than this after normalisation. Default: 0 (keep everything).",
    )

    parser.add_argument(
        "--embedding-model",
        default="text-embedding-3-small",
        help="Embedding model the existing index was built with.",
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        default=1536,
        help="Embedding dimensionality of the existing index.",
    )
    parser.add_argument("--embedding-provider", default="openai", help="Embedding provider name.")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="Chunk size the legacy corpus was built with, recorded in the manifest.",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=100,
        help="Chunk overlap the legacy corpus was built with, recorded in the manifest.",
    )

    parser.add_argument(
        "--force", action="store_true", help="Overwrite a non-empty output directory."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyse the input and print the report without writing anything.",
    )
    return parser


def _legacy_document_id(legacy_pmid: str, legacy_title: str, legacy_source: str, text: str) -> str:
    """Derive a stable document identifier from a legacy chunk's metadata."""
    pmid = (
        legacy_pmid.strip()
    )  # Legacy records store the PMID as a string, sometimes with whitespace
    if (
        legacy_source == "pubmed" and pmid.isdigit()
    ):  # The overwhelmingly common case: a real PubMed article
        return document_id_for_pubmed(pmid)
    scheme = "pdf" if legacy_source == "pdf" else "txt"  # Anything non-PubMed is content-addressed
    basis = (
        legacy_title.strip() or text
    )  # Prefer the title as the addressing basis so all chunks of one file agree; fall back to text when there is no title
    return document_id_for_content(scheme, basis)


def _source_type_for(
    legacy_source: str,
) -> SourceType:  # Map the legacy free-text 'source' field onto the closed enum
    """Translate a legacy source string into a :class:`SourceType`."""
    if legacy_source == "pubmed":
        return SourceType.PUBMED_ABSTRACT
    if legacy_source == "pdf":
        return SourceType.PDF
    return SourceType.TXT


def _run_migration(args: argparse.Namespace) -> tuple[MigrationReport, Path | None]:
    """Perform the migration. Returns the report and the output directory (None on dry run)."""
    # Imported here, not at module scope, so the quarantined pickle reader is
    # only ever loaded by this command — never as a side effect of importing
    # anything else in the package.
    from synapse._legacy.pickle_reader import load_legacy_chunks

    report = MigrationReport()
    now = datetime.now(UTC)  # One timestamp for the whole migration, so every emitted record agrees
    source_pack_version = args.source_pack_version or args.corpus_version

    legacy_chunks = load_legacy_chunks(
        args.input
    )  # Restricted unpickler; refuses anything outside the allow-list
    report.legacy_chunks_read = len(legacy_chunks)

    legacy_ids = Counter(
        f"{c.pmid or c.title}_chunk{c.chunk_index}" for c in legacy_chunks
    )  # Reconstruct the OLD identifier scheme to quantify how many collisions existed
    report.duplicate_chunk_ids_resolved = sum(
        count - 1 for count in legacy_ids.values() if count > 1
    )

    ordinal_by_document: dict[str, int] = defaultdict(
        int
    )  # Running per-document ordinal; a document seen twice CONTINUES numbering instead of restarting, which is what makes identifiers unique
    seen_document_texts: dict[
        str, str
    ] = {}  # document_id -> digest of its first-seen reconstructed text, used to detect a repeated document
    seen_chunk_digests: set[str] = set()  # Content digests, for reporting exact-duplicate passages
    dropped_documents: set[str] = set()  # Documents excluded by --dedupe

    chunks: list[EvidenceChunk] = []  # Emitted chunk records, in order
    document_text_parts: dict[str, list[str]] = defaultdict(
        list
    )  # Reconstructed document text, assembled from its chunks
    document_meta: dict[
        str, object
    ] = {}  # First legacy record seen per document, used for title/url/pmid
    document_offset: dict[str, int] = defaultdict(
        int
    )  # Running character offset into each reconstructed document

    for legacy in legacy_chunks:
        text = normalize_text(
            legacy.text
        )  # Normalise once; the chunk model requires canonical text and its hash depends on it
        if (
            not text
        ):  # A legacy chunk whose entire body was whitespace (the corpus contains a few of these)
            report.chunks_dropped_empty += 1
            continue
        if (
            args.min_chunk_chars and len(text) < args.min_chunk_chars
        ):  # Policy filter, applied at migration time rather than baked into the schema
            report.chunks_dropped_too_short += 1
            continue

        document_id = _legacy_document_id(legacy.pmid, legacy.title, legacy.source, text)

        digest = sha256_text(text)  # Content digest of this passage
        if (
            digest in seen_chunk_digests
        ):  # Exact repeat of an earlier passage, from the same article fetched under a different topic
            report.duplicate_texts_detected += 1
            if args.dedupe:  # Only --dedupe actually drops it; the default preserves order for index compatibility
                dropped_documents.add(document_id)
                report.duplicate_documents_removed += 1
                continue
        seen_chunk_digests.add(digest)
        seen_document_texts.setdefault(document_id, digest)

        ordinal = ordinal_by_document[document_id]  # Current position for this document
        ordinal_by_document[document_id] += (
            1  # Advance, so a re-ingested document continues rather than colliding
        )

        start = document_offset[
            document_id
        ]  # Offsets are into the RECONSTRUCTED document text (see the note in docs/artifact-format.md): the true source offsets are not recoverable from a chunked pickle
        end = start + len(text)
        document_offset[document_id] = (
            end + 1
        )  # +1 accounts for the single space used to join chunks during reconstruction

        chunks.append(
            EvidenceChunk.build(
                document_id=document_id,
                ordinal=ordinal,
                text=text,
                char_start=start,
                char_end=end,
                source_type=_source_type_for(legacy.source),
                ingested_at=now,
                page=legacy.page
                if legacy.page is not None and legacy.page >= 0
                else None,  # Legacy used -1 as "not applicable"; the schema uses null
                legacy_chunk_id=f"{legacy.pmid or legacy.title}_chunk{legacy.chunk_index}",  # Retained purely for traceability back to the pre-migration artifact
            )
        )
        document_text_parts[document_id].append(text)
        document_meta.setdefault(
            document_id, legacy
        )  # First record wins for title/url; they are identical across a document's chunks

    report.chunks_written = len(chunks)

    documents: list[SourceDocument] = []  # Emitted document records
    chunk_ids_by_document: dict[str, list[str]] = defaultdict(list)
    for chunk in chunks:
        chunk_ids_by_document[chunk.document_id].append(chunk.chunk_id)

    for document_id, parts in document_text_parts.items():
        legacy = document_meta[document_id]  # type: ignore[assignment]  # Values are LegacyChunk; typed as object to avoid importing the quarantined module at module scope
        url = (
            getattr(legacy, "source_url", "") or ""
        ).strip()  # Legacy records usually carry a PubMed URL
        pmid = (getattr(legacy, "pmid", "") or "").strip()
        if (
            not url and pmid.isdigit()
        ):  # Reconstruct the canonical PubMed URL when the legacy record omitted it
            url = _PUBMED_URL_TEMPLATE.format(pmid=pmid)
        if not url.startswith(
            ("http://", "https://")
        ):  # SourceDocument requires a real http(s) URL; a document without one is reported rather than fabricated
            report.documents_skipped_no_url += 1
            continue

        reconstructed = " ".join(
            parts
        )  # Approximate document text; overlap regions appear twice — documented, and fixed by a real re-ingest
        documents.append(
            SourceDocument(
                document_id=document_id,
                source_type=_source_type_for(getattr(legacy, "source", "txt")),
                source_url=url,
                identifiers=DocumentIdentifiers(pmid=pmid or None),
                title=(getattr(legacy, "title", "") or "untitled").strip()
                or "untitled",  # Legacy titles are sometimes truncated at nested markup; migrated as-is rather than silently repaired
                container=SourceContainer(),  # Journal/authors are simply absent from the legacy corpus; left empty rather than invented
                retrieved_at=now,  # The true fetch time was never recorded; the migration timestamp is the honest approximation
                content_sha256=sha256_text(reconstructed),
                chunk_ids=chunk_ids_by_document[document_id],
                source_pack_version=source_pack_version,
                notes=(
                    "Migrated from a legacy pickle corpus. Publication date, journal, authors and "
                    "evidence type were not recorded by the legacy pipeline and are absent, not unknown-but-inferred. "
                    "Document text is reconstructed by concatenating chunks, so overlap regions are duplicated; "
                    "a full re-ingest is required for fidelity. approval_status is 'unreviewed': the repository "
                    "contains no review metadata for this or any other document."
                ),
            )
        )

    report.documents_written = len(documents)

    if args.dry_run:  # Analysis complete; write nothing
        return report, None

    output_dir: Path = args.output_dir
    if (
        output_dir.exists() and any(output_dir.iterdir()) and not args.force
    ):  # Refuse to clobber existing artifacts unless told to
        raise SynapseArtifactError(
            problem="output directory is not empty; pass --force to overwrite", path=output_dir
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    chunks_path = output_dir / CHUNKS_FILENAME
    documents_path = output_dir / DOCUMENTS_FILENAME
    write_jsonl(chunks_path, chunks)  # JSONL, never pickle
    write_jsonl(documents_path, documents)

    faiss_target: Path | None = None
    if (
        args.faiss_index is not None
    ):  # Copy the existing index in so the manifest governs a self-contained directory
        faiss_target = output_dir / FAISS_FILENAME
        shutil.copy2(
            args.faiss_index, faiss_target
        )  # copy2 preserves mtime, which keeps the copy byte-identical for hashing purposes

    artifacts = [
        make_artifact(chunks_path, "chunks", relative_to=output_dir),
        make_artifact(documents_path, "documents", relative_to=output_dir),
    ]
    if faiss_target is not None:
        artifacts.append(make_artifact(faiss_target, "faiss", relative_to=output_dir))

    manifest = build_index_manifest(
        index_id=f"{args.corpus_version}-{args.index_version}",
        index_version=str(args.index_version),
        corpus_version=args.corpus_version,
        source_pack_version=source_pack_version,
        chunks=chunks,
        artifacts=artifacts,
        embedding=EmbeddingConfig(
            provider=args.embedding_provider,
            model=args.embedding_model,
            dimensions=args.embedding_dimensions,
            l2_normalized=True,  # The legacy vector store normalises embeddings before indexing, which is what makes L2 ranking equivalent to cosine
        ),
        chunking=ChunkingConfig(
            algorithm=ChunkingAlgorithm.LEGACY_IMPORTED,  # Honest label: boundaries were inherited from the legacy artifact, not recomputed here
            chunk_size=args.chunk_size,
            overlap=args.chunk_overlap,
            min_chunk_chars=args.min_chunk_chars,
        ),
        distance_metric=DistanceMetric.L2,  # Matches the legacy faiss.IndexFlatL2
        document_count=len(documents),
        faiss_path=faiss_target,
        faiss_index_type="IndexFlatL2" if faiss_target is not None else None,
        repository_root=Path.cwd(),  # git commit is recorded only if this is a checkout; None otherwise, never fabricated
        built_at=now,
    )
    write_manifest(output_dir / "manifest.json", manifest)

    verify_artifacts(
        output_dir, deep=True
    )  # Verify what we just wrote, so a migration cannot report success on artifacts that would fail to load

    return report, output_dir


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    print(
        TRUST_WARNING, file=sys.stderr
    )  # Always shown, before anything is read, on stderr so it is visible even when stdout is redirected

    if (
        not args.trust_input
    ):  # The gate: refuse rather than prompt, so the command is safe to run non-interactively
        print(
            "REFUSED: --trust-input was not supplied.\n"
            "Re-run with --trust-input only if the input pickle came from a source you control.",
            file=sys.stderr,
        )
        return 2

    if (
        args.dedupe and args.faiss_index is not None
    ):  # Enforce the ordering constraint rather than letting an operator misalign an index
        print(
            "REFUSED: --dedupe changes the number and order of corpus records, which invalidates\n"
            "the row alignment of an existing FAISS index. Migrate without --dedupe to keep the\n"
            "existing index, or migrate with --dedupe and rebuild the index from the new corpus.",
            file=sys.stderr,
        )
        return 2

    try:
        report, output_dir = _run_migration(args)
    except SynapseArtifactError as exc:  # Typed, user-safe failures are reported cleanly; anything else propagates with a full traceback for debugging
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1

    print("\nMigration report")
    print(report.render())
    if output_dir is None:
        print("\nDry run: no files were written.")
    else:
        print(f"\nWrote and verified artifacts in: {output_dir}")
        print("The legacy input was not modified. See docs/artifact-format.md for rollback.")
    print(f"\nsynapse {synapse_version}")
    return 0


if __name__ == "__main__":  # Enables `python -m synapse.cli.migrate_artifacts`
    raise SystemExit(main())

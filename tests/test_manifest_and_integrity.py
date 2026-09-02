"""
Manifest generation and fail-closed verification tests.

The scenario that matters most is ``test_reordered_corpus_is_refused``. A
reordered corpus contains exactly the same records, so a content digest alone
cannot detect it — yet FAISS row *i* is bound to corpus record *i*, so serving
from a reordered corpus produces confident answers citing the wrong document.
The separate ordering digest is what catches it, and that test is the proof.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import (
    ValidationError,  # Manifest invariants surface as pydantic validation errors, so tests assert that precise type
)

from synapse.corpus import write_jsonl
from synapse.errors import (
    ArtifactCompatibilityError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactSchemaError,
    ArtifactVersionError,
)
from synapse.index import (
    MANIFEST_FILENAME,
    build_index_manifest,
    detect_git_commit,
    load_verified_corpus,
    probe_faiss_header,
    read_manifest,
    verify_artifacts,
    write_manifest,
)
from synapse.index.manifest import make_artifact
from synapse.schemas.enums import IndexState
from synapse.schemas.index import IndexManifest, ManifestArtifact


# A minimal, valid FAISS IndexFlatL2 header: fourcc 'IxF2', then a
# little-endian int32 dimensionality and an int64 vector count. Constructed
# rather than checked in, so the test needs no binary fixture.
def _faiss_stub(dimensions: int, vector_count: int) -> bytes:
    return (
        b"IxF2"
        + dimensions.to_bytes(4, "little")
        + vector_count.to_bytes(8, "little")
        + b"\x00" * 32
    )


@pytest.fixture
def artifact_dir(
    tmp_path: Path, chunks, embedding_config, chunking_config, distance_metric
) -> Path:
    """A complete, verifiable artifact directory."""
    directory = tmp_path / "corpus-test"
    directory.mkdir()
    chunks_path = directory / "chunks.jsonl.gz"
    write_jsonl(chunks_path, chunks)

    faiss_path = directory / "index.faiss"
    faiss_path.write_bytes(_faiss_stub(embedding_config.dimensions, len(chunks)))

    manifest = build_index_manifest(
        index_id="corpus-test-1",
        index_version="1",
        corpus_version="corpus-test",
        chunks=chunks,
        artifacts=[
            make_artifact(chunks_path, "chunks", relative_to=directory),
            make_artifact(faiss_path, "faiss", relative_to=directory),
        ],
        embedding=embedding_config,
        chunking=chunking_config,
        distance_metric=distance_metric,
        document_count=2,
        faiss_path=faiss_path,
        faiss_index_type="IndexFlatL2",
    )
    write_manifest(directory / MANIFEST_FILENAME, manifest)
    return directory


class TestManifestGeneration:
    """A generated manifest must record everything needed to verify a load."""

    def test_manifest_records_every_required_field(self, artifact_dir: Path) -> None:
        manifest = read_manifest(artifact_dir / MANIFEST_FILENAME)
        assert manifest.index_version  # index version
        assert manifest.corpus_version  # corpus version
        assert len(manifest.corpus_sha256) == 64  # corpus SHA-256
        assert (
            manifest.index_sha256 is not None and len(manifest.index_sha256) == 64
        )  # index SHA-256
        assert (
            manifest.embedding.model and manifest.embedding.dimensions == 1536
        )  # embedding model and dimensions
        assert (
            manifest.chunking.algorithm and manifest.chunking.chunk_size == 500
        )  # chunking algorithm and configuration
        assert (
            manifest.distance_metric == "l2" and manifest.vectors_l2_normalized is True
        )  # distance metric and normalisation
        assert manifest.built_at.tzinfo is not None  # build timestamp, timezone-aware
        assert "synapse" in manifest.builder and "python" in manifest.builder

    def test_manifest_is_self_hashed(self, artifact_dir: Path) -> None:
        manifest = read_manifest(artifact_dir / MANIFEST_FILENAME)
        assert manifest.manifest_sha256 == manifest.compute_self_digest()

    def test_manifest_generation_is_deterministic(
        self, chunks, embedding_config, chunking_config, distance_metric, now
    ) -> None:
        # Same inputs and timestamp must yield the same digests, so a rebuild
        # can be compared against a stored manifest.
        def build() -> IndexManifest:
            return build_index_manifest(
                index_id="i",
                index_version="1",
                corpus_version="c",
                chunks=chunks,
                artifacts=[],
                embedding=embedding_config,
                chunking=chunking_config,
                distance_metric=distance_metric,
                document_count=2,
                built_at=now,
            )

        first, second = build(), build()
        assert first.corpus_sha256 == second.corpus_sha256
        assert first.chunk_ids_sha256 == second.chunk_ids_sha256
        assert first.manifest_sha256 == second.manifest_sha256

    def test_git_commit_is_null_rather_than_fabricated(self, tmp_path: Path) -> None:
        # tmp_path is not a git checkout. The honest result is None.
        assert detect_git_commit(tmp_path) is None

    def test_manifest_without_index_declares_rebuild_required(
        self, chunks, embedding_config, chunking_config, distance_metric
    ) -> None:
        manifest = build_index_manifest(
            index_id="i",
            index_version="1",
            corpus_version="c",
            chunks=chunks,
            artifacts=[],
            embedding=embedding_config,
            chunking=chunking_config,
            distance_metric=distance_metric,
            document_count=2,
        )
        assert manifest.index_state is IndexState.REBUILD_REQUIRED
        assert manifest.index_sha256 is None  # Claiming a digest for an absent index would be a lie


class TestManifestInvariants:
    """State declared by a manifest must match the fields it actually carries."""

    def _valid_payload(self, artifact_dir: Path) -> dict:
        return json.loads((artifact_dir / MANIFEST_FILENAME).read_text())

    def test_ready_without_index_digest_is_rejected(self, artifact_dir: Path) -> None:
        payload = self._valid_payload(artifact_dir)
        payload["index_sha256"] = None
        with pytest.raises(ValidationError, match="requires index_sha256"):
            IndexManifest.model_validate(payload)

    def test_vector_count_must_equal_chunk_count(self, artifact_dir: Path) -> None:
        # One vector per chunk, or the row-to-chunk mapping is broken.
        payload = self._valid_payload(artifact_dir)
        payload["vector_count"] = payload["chunk_count"] + 1
        with pytest.raises(ValidationError, match="vector_count must equal chunk_count"):
            IndexManifest.model_validate(payload)

    def test_rebuild_required_may_not_carry_an_index_digest(self, artifact_dir: Path) -> None:
        payload = self._valid_payload(artifact_dir)
        payload["index_state"] = "rebuild_required"
        with pytest.raises(ValidationError, match="must not carry index_sha256"):
            IndexManifest.model_validate(payload)

    def test_overlap_must_be_smaller_than_chunk_size(self, artifact_dir: Path) -> None:
        payload = self._valid_payload(artifact_dir)
        payload["chunking"]["overlap"] = payload["chunking"]["chunk_size"]
        with pytest.raises(ValidationError, match="overlap must be smaller"):
            IndexManifest.model_validate(payload)

    def test_duplicate_artifact_roles_are_rejected(self, artifact_dir: Path) -> None:
        payload = self._valid_payload(artifact_dir)
        payload["artifacts"].append(payload["artifacts"][0])
        with pytest.raises(ValidationError, match="roles must be unique"):
            IndexManifest.model_validate(payload)


class TestArtifactPathSafety:
    """A manifest is untrusted input; its paths must not escape the directory."""

    @pytest.mark.parametrize(
        "path",
        [
            "../../etc/passwd",  # Parent traversal
            "/etc/passwd",  # Absolute POSIX
            "C:/Windows/System32/config/SAM",  # Windows drive prefix
            "sub/../../outside.bin",  # Traversal at depth
            "\\\\server\\share\\file",  # UNC path
        ],
    )
    def test_escaping_paths_are_rejected(self, path: str) -> None:
        with pytest.raises(
            ValidationError
        ):  # Precise type, not a blind Exception, so an unrelated failure cannot masquerade as a pass
            ManifestArtifact(role="chunks", path=path, sha256="a" * 64, size_bytes=1)

    def test_relative_path_is_accepted(self) -> None:
        assert ManifestArtifact(
            role="chunks", path="sub/chunks.jsonl.gz", sha256="a" * 64, size_bytes=1
        ).path


class TestVerification:
    """The happy path, and every way it is made to fail."""

    def test_intact_artifacts_verify(self, artifact_dir: Path) -> None:
        report = verify_artifacts(artifact_dir, deep=True)
        assert report.artifacts_checked == 2
        assert report.chunks_validated == 3
        assert report.faiss_header_checked is True

    def test_load_returns_manifest_and_chunks(self, artifact_dir: Path, chunks) -> None:
        manifest, loaded = load_verified_corpus(artifact_dir)
        assert manifest.chunk_count == len(chunks)
        assert loaded == chunks

    def test_missing_manifest_is_refused(self, artifact_dir: Path) -> None:
        (artifact_dir / MANIFEST_FILENAME).unlink()
        with pytest.raises(ArtifactNotFoundError):
            verify_artifacts(artifact_dir)

    def test_missing_declared_artifact_is_refused(self, artifact_dir: Path) -> None:
        (artifact_dir / "chunks.jsonl.gz").unlink()
        with pytest.raises(ArtifactNotFoundError):
            verify_artifacts(artifact_dir)

    def test_corrupted_corpus_file_is_refused(self, artifact_dir: Path) -> None:
        # A single flipped byte must be fatal.
        target = artifact_dir / "chunks.jsonl.gz"
        data = bytearray(target.read_bytes())
        data[-1] ^= 0xFF
        target.write_bytes(bytes(data))
        with pytest.raises(ArtifactIntegrityError, match="digest mismatch"):
            verify_artifacts(artifact_dir)

    def test_truncated_artifact_is_refused_on_size(self, artifact_dir: Path) -> None:
        target = artifact_dir / "index.faiss"
        target.write_bytes(target.read_bytes()[:-8])
        with pytest.raises(ArtifactIntegrityError, match="size mismatch"):
            verify_artifacts(artifact_dir)

    def test_tampered_manifest_is_refused(self, artifact_dir: Path) -> None:
        # Editing any field invalidates the manifest's own digest.
        path = artifact_dir / MANIFEST_FILENAME
        payload = json.loads(path.read_text())
        payload["corpus_version"] = "corpus-tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ArtifactIntegrityError, match="integrity check failed"):
            verify_artifacts(artifact_dir)

    def test_reordered_corpus_is_refused(
        self, artifact_dir: Path, chunks, embedding_config, chunking_config, distance_metric
    ) -> None:
        # THE case a content digest alone cannot catch. Rebuild the manifest
        # against the original order, then write the corpus reversed. Every
        # record is individually valid and the set is identical; only the
        # ordering digest reveals the misalignment.
        reordered = list(reversed(chunks))
        chunks_path = artifact_dir / "chunks.jsonl.gz"
        write_jsonl(chunks_path, reordered)
        faiss_path = artifact_dir / "index.faiss"
        manifest = build_index_manifest(
            index_id="corpus-test-1",
            index_version="1",
            corpus_version="corpus-test",
            chunks=chunks,  # Manifest still describes the ORIGINAL order
            artifacts=[
                make_artifact(chunks_path, "chunks", relative_to=artifact_dir),
                make_artifact(faiss_path, "faiss", relative_to=artifact_dir),
            ],
            embedding=embedding_config,
            chunking=chunking_config,
            distance_metric=distance_metric,
            document_count=2,
            faiss_path=faiss_path,
        )
        # Overwrite only the corpus digest so the ordering check is what fails,
        # proving the two digests are independently load-bearing.
        patched = manifest.model_copy(
            update={"corpus_sha256": _digest_of(reordered)}
        ).with_self_digest()
        write_manifest(artifact_dir / MANIFEST_FILENAME, patched)
        with pytest.raises(ArtifactCompatibilityError, match="ordering"):
            verify_artifacts(artifact_dir, deep=True)

    def test_chunk_count_mismatch_is_refused(self, artifact_dir: Path, chunks) -> None:
        write_jsonl(artifact_dir / "chunks.jsonl.gz", chunks[:-1])
        with pytest.raises(ArtifactIntegrityError):
            verify_artifacts(artifact_dir, deep=True)

    def test_shallow_mode_skips_corpus_validation(self, artifact_dir: Path) -> None:
        report = verify_artifacts(artifact_dir, deep=False)
        assert report.chunks_validated == 0
        assert report.deep is False

    def test_manifest_with_incompatible_schema_version_is_refused(self, artifact_dir: Path) -> None:
        path = artifact_dir / MANIFEST_FILENAME
        payload = json.loads(path.read_text())
        payload["schema_version"] = "2.0"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ArtifactVersionError):
            verify_artifacts(artifact_dir)

    def test_malformed_manifest_json_is_refused(self, artifact_dir: Path) -> None:
        (artifact_dir / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(ArtifactSchemaError, match="not valid JSON"):
            verify_artifacts(artifact_dir)


class TestFaissHeaderProbe:
    """Structural cross-checks against the index, without faiss installed."""

    def test_probe_reads_dimensions_and_count(self, tmp_path: Path) -> None:
        path = tmp_path / "i.faiss"
        path.write_bytes(_faiss_stub(1536, 2220))
        header = probe_faiss_header(path)
        assert header is not None
        assert (header.dimensions, header.vector_count) == (1536, 2220)

    def test_unknown_index_type_declines_rather_than_failing(self, tmp_path: Path) -> None:
        # An unfamiliar FAISS build must not cause a false verification failure.
        path = tmp_path / "i.faiss"
        path.write_bytes(b"ZZZZ" + b"\x00" * 32)
        assert probe_faiss_header(path) is None

    def test_truncated_index_declines(self, tmp_path: Path) -> None:
        path = tmp_path / "i.faiss"
        path.write_bytes(b"IxF2")
        assert probe_faiss_header(path) is None

    def test_dimension_mismatch_is_refused(self, artifact_dir: Path) -> None:
        # An index built with a different embedding model.
        target = artifact_dir / "index.faiss"
        target.write_bytes(_faiss_stub(768, 3))
        _rehash_artifact(artifact_dir, "faiss", target)
        with pytest.raises(ArtifactCompatibilityError, match="dimensionality"):
            verify_artifacts(artifact_dir)

    def test_vector_count_mismatch_is_refused(self, artifact_dir: Path) -> None:
        target = artifact_dir / "index.faiss"
        target.write_bytes(_faiss_stub(1536, 999))
        _rehash_artifact(artifact_dir, "faiss", target)
        with pytest.raises(ArtifactCompatibilityError, match="vector count"):
            verify_artifacts(artifact_dir)


def _digest_of(chunks) -> str:
    """Corpus digest helper, kept out of the test bodies for readability."""
    from synapse.corpus import records_digest

    return records_digest(chunks)


def _rehash_artifact(directory: Path, role: str, path: Path) -> None:
    """Re-record one artifact's digest so a FILE-level check passes and the
    structural check is what fails. Without this the test would prove only that
    the digest check works, which is covered separately."""
    from synapse.hashing import sha256_file

    manifest_path = directory / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text())
    for artifact in payload["artifacts"]:
        if artifact["role"] == role:
            artifact["sha256"] = sha256_file(path)
            artifact["size_bytes"] = path.stat().st_size
    payload["manifest_sha256"] = None
    manifest = IndexManifest.model_validate(payload).with_self_digest()
    write_manifest(manifest_path, manifest)

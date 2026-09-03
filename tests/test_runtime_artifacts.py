"""
The deployed artifact path: download, verify, cache, or serve nothing.

Every test here is offline and deterministic. There is no bucket, no boto3, no
FAISS and no corpus on disk — the object store is a local directory and the
FAISS index is a valid header followed by zeroes, because a header is all
verification reads.

The suite is organised around the ways an artifact can be wrong, because that is
what the loader is for. A happy path that works proves very little; a corrupt
archive that is *refused*, and refused before its tar stream is even parsed, is
the actual requirement.

What is asserted throughout, and is easy to lose:

* every failure raises rather than degrading;
* nothing partially-verified is ever written where the backends would find it;
* there is **no fallback** to the unmanaged ``hybrid_index/`` directory;
* no provider message, endpoint, path or credential appears in a readiness
  response.
"""

from __future__ import annotations

import ast
import gzip
import io
import tarfile
from pathlib import Path

import pytest

from synapse.cli import package_runtime
from synapse.errors import (
    ArtifactCompatibilityError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactVersionError,
)
from synapse.index import MANIFEST_FILENAME
from synapse.runtime import archive as archive_module
from synapse.runtime.archive import (
    RUNTIME_MEMBERS,
    pack_archive,
    safe_extract,
    verify_archive_digest,
)
from synapse.runtime.config import (
    ArtifactConfigurationError,
    RuntimeArtifactConfig,
)
from synapse.runtime.loader import MARKER_FILENAME, load_artifact, verify_directory
from synapse.runtime.objectstore import LocalDirectoryStore
from synapse.runtime.readiness import (
    NotReadyError,
    ReadinessState,
    RuntimeIndex,
)
from synapse.ui.errors import AnswerFailureCode
from tests.conftest import REPO_ROOT
from tests.runtime_fixtures import (
    build_archive,
    build_artifact_dir,
    corrupt_faiss_header,
    reorder_corpus,
    rewrite_manifest_raw,
    truncate_corpus,
)

RUNTIME_ROOT = REPO_ROOT / "synapse" / "runtime"

VALID_DIGEST = "a" * 64


def _config(tmp_path: Path, digest: str, *, version: str = "2") -> RuntimeArtifactConfig:
    """A config whose cache and staging live entirely under ``tmp_path``."""
    return RuntimeArtifactConfig(
        version=version,
        archive_sha256=digest,
        bucket="test-bucket",
        key="synapse-runtime-2.tar.gz",
        cache_root=tmp_path / "cache",
    )


def _store(archive: Path) -> LocalDirectoryStore:
    """An object store serving one archive under its own filename."""
    return LocalDirectoryStore(root=archive.parent)


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------


class TestPacking:
    """The archive is reproducible, complete, and refuses to be partial."""

    def test_repacking_identical_content_yields_the_same_digest(self, tmp_path: Path) -> None:
        """The digest is a pin. A pin that changes on every rebuild is not one."""
        source = build_artifact_dir(tmp_path / "artifact")
        first = pack_archive(source, tmp_path / "a.tar.gz")
        second = pack_archive(source, tmp_path / "b.tar.gz")
        assert first == second

    def test_the_archive_contains_exactly_the_runtime_members(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        with tarfile.open(archive, "r:gz") as tar:
            assert sorted(tar.getnames()) == sorted(RUNTIME_MEMBERS)

    def test_a_missing_member_refuses_to_pack(self, tmp_path: Path) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        (source / "documents.jsonl.gz").unlink()
        with pytest.raises(ArtifactNotFoundError):
            pack_archive(source, tmp_path / "out.tar.gz")

    def test_packed_members_carry_no_timestamp_or_ownership(self, tmp_path: Path) -> None:
        """What makes the digest reproducible across machines and users."""
        archive, _ = build_archive(tmp_path)
        with tarfile.open(archive, "r:gz") as tar:
            for info in tar.getmembers():
                assert info.mtime == 0
                assert info.uid == 0 and info.gid == 0
                assert info.uname == "" and info.gname == ""


# ---------------------------------------------------------------------------
# Extraction: the security gate
# ---------------------------------------------------------------------------


def _hostile_archive(path: Path, members: list[tarfile.TarInfo], payloads: list[bytes]) -> Path:
    """Write a tar.gz containing exactly the given members."""
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as tar,
    ):
        for info, payload in zip(members, payloads, strict=True):
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return path


def _regular(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    return info


class TestSafeExtraction:
    """Everything ``tarfile.extractall`` would happily do, refused."""

    def test_path_traversal_is_refused(self, tmp_path: Path) -> None:
        archive = _hostile_archive(
            tmp_path / "evil.tar.gz", [_regular("../../escaped.txt")], [b"payload"]
        )
        with pytest.raises(ArtifactIntegrityError, match="traversal"):
            safe_extract(archive, tmp_path / "out")
        assert not (tmp_path.parent / "escaped.txt").exists()

    def test_an_absolute_path_is_refused(self, tmp_path: Path) -> None:
        archive = _hostile_archive(
            tmp_path / "evil.tar.gz", [_regular("/etc/passwd")], [b"payload"]
        )
        with pytest.raises(ArtifactIntegrityError, match="absolute"):
            safe_extract(archive, tmp_path / "out")

    def test_a_symlink_member_is_refused(self, tmp_path: Path) -> None:
        """A link is a two-step traversal: create it, then write through it."""
        info = tarfile.TarInfo("manifest.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive = _hostile_archive(tmp_path / "evil.tar.gz", [info], [b""])
        with pytest.raises(ArtifactIntegrityError, match="link"):
            safe_extract(archive, tmp_path / "out")

    def test_a_hardlink_member_is_refused(self, tmp_path: Path) -> None:
        info = tarfile.TarInfo("manifest.json")
        info.type = tarfile.LNKTYPE
        info.linkname = "index.faiss"
        archive = _hostile_archive(tmp_path / "evil.tar.gz", [info], [b""])
        with pytest.raises(ArtifactIntegrityError, match="link"):
            safe_extract(archive, tmp_path / "out")

    def test_a_device_member_is_refused(self, tmp_path: Path) -> None:
        info = tarfile.TarInfo("manifest.json")
        info.type = tarfile.CHRTYPE
        archive = _hostile_archive(tmp_path / "evil.tar.gz", [info], [b""])
        with pytest.raises(ArtifactIntegrityError, match="regular file"):
            safe_extract(archive, tmp_path / "out")

    def test_an_unexpected_member_is_refused_not_ignored(self, tmp_path: Path) -> None:
        """'Extra files were ignored' is how a payload gets staged."""
        source = build_artifact_dir(tmp_path / "artifact")
        (source / "extra.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        archive = tmp_path / "out.tar.gz"
        pack_archive(source, archive, members=[*RUNTIME_MEMBERS, "extra.sh"])
        with pytest.raises(ArtifactIntegrityError, match="unexpected"):
            safe_extract(archive, tmp_path / "out")

    def test_a_duplicate_member_is_refused(self, tmp_path: Path) -> None:
        """The second copy would overwrite the first after any check on it."""
        archive = _hostile_archive(
            tmp_path / "dup.tar.gz",
            [_regular("manifest.json"), _regular("manifest.json")],
            [b"first", b"second"],
        )
        with pytest.raises(ArtifactIntegrityError, match="duplicate"):
            safe_extract(archive, tmp_path / "out")

    def test_a_missing_required_member_is_refused(self, tmp_path: Path) -> None:
        archive = _hostile_archive(tmp_path / "short.tar.gz", [_regular("manifest.json")], [b"{}"])
        with pytest.raises(ArtifactIntegrityError, match="missing required"):
            safe_extract(archive, tmp_path / "out")

    def test_a_decompression_bomb_is_refused_on_bytes_written(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The declared size is attacker-controlled, so the real bytes decide."""
        monkeypatch.setattr(archive_module, "MAX_MEMBER_BYTES", 16)
        archive = _hostile_archive(
            tmp_path / "bomb.tar.gz", [_regular("manifest.json")], [b"x" * 4096]
        )
        with pytest.raises(ArtifactIntegrityError, match="size ceiling"):
            safe_extract(archive, tmp_path / "out")

    def test_a_refused_member_leaves_no_partial_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(archive_module, "MAX_MEMBER_BYTES", 16)
        archive = _hostile_archive(
            tmp_path / "bomb.tar.gz", [_regular("manifest.json")], [b"x" * 4096]
        )
        destination = tmp_path / "out"
        with pytest.raises(ArtifactIntegrityError):
            safe_extract(archive, destination)
        assert not (destination / "manifest.json").exists()

    def test_a_well_formed_archive_extracts_every_member(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        written = safe_extract(archive, tmp_path / "out")
        assert sorted(written) == sorted(RUNTIME_MEMBERS)


class TestArchiveDigest:
    """The first gate, before a byte is decompressed."""

    def test_a_matching_digest_passes(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        assert verify_archive_digest(archive, digest) == digest

    def test_a_mismatched_digest_is_refused(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        with pytest.raises(ArtifactIntegrityError, match="archive digest"):
            verify_archive_digest(archive, VALID_DIGEST)

    def test_a_missing_archive_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ArtifactNotFoundError):
            verify_archive_digest(tmp_path / "absent.tar.gz", VALID_DIGEST)

    def test_a_tampered_archive_is_refused(self, tmp_path: Path) -> None:
        """One flipped byte is enough, and it is caught before the tar is opened."""
        archive, digest = build_archive(tmp_path)
        data = bytearray(archive.read_bytes())
        data[-1] ^= 0xFF
        archive.write_bytes(bytes(data))
        with pytest.raises(ArtifactIntegrityError):
            verify_archive_digest(archive, digest)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_an_unconfigured_deployment_is_a_typed_refusal(self) -> None:
        with pytest.raises(ArtifactConfigurationError):
            RuntimeArtifactConfig(version="", archive_sha256="", bucket="", key="").validate()

    def test_a_malformed_digest_is_refused(self) -> None:
        config = RuntimeArtifactConfig(
            version="2", archive_sha256="not-a-digest", bucket="b", key="k"
        )
        with pytest.raises(ArtifactConfigurationError):
            config.validate()

    def test_the_digest_value_is_never_echoed_in_the_error(self) -> None:
        """Operator input can be the wrong variable's value. Never echo it."""
        secret = "sk-live-not-a-digest-but-plausibly-a-secret"
        config = RuntimeArtifactConfig(version="2", archive_sha256=secret, bucket="b", key="k")
        with pytest.raises(ArtifactConfigurationError) as caught:
            config.validate()
        assert secret not in caught.value.safe_message

    def test_the_cache_path_is_keyed_by_version_and_digest(self, tmp_path: Path) -> None:
        """Repointing either value must not reuse the old bytes."""
        first = _config(tmp_path, "a" * 64).artifact_dir
        second = _config(tmp_path, "b" * 64).artifact_dir
        third = _config(tmp_path, "a" * 64, version="3").artifact_dir
        assert first != second
        assert first != third

    def test_describe_carries_no_credential_or_endpoint(self, tmp_path: Path) -> None:
        described = _config(tmp_path, VALID_DIGEST).describe()
        assert "bucket" not in described
        assert "key" not in described
        assert described["archive_sha256"] == VALID_DIGEST[:12]

    def test_the_key_defaults_from_the_version(self, monkeypatch) -> None:
        monkeypatch.setenv("SYNAPSE_ARTIFACT_VERSION", "7")
        monkeypatch.setenv("SYNAPSE_ARTIFACT_SHA256", VALID_DIGEST)
        monkeypatch.setenv("SYNAPSE_ARTIFACT_BUCKET", "bucket")
        monkeypatch.delenv("SYNAPSE_ARTIFACT_KEY", raising=False)
        config = RuntimeArtifactConfig.from_environment()
        assert config.key == "synapse-runtime-7.tar.gz"
        assert config.configured

    def test_the_config_holds_no_credential_fields(self) -> None:
        """boto3 reads them from the environment; they never enter this object."""
        import dataclasses

        names = {field.name for field in dataclasses.fields(RuntimeArtifactConfig)}
        assert not names & {"access_key", "secret_key", "session_token", "credentials"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoadingSucceeds:
    def test_a_valid_archive_downloads_and_verifies(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        loaded = load_artifact(_config(tmp_path, digest), _store(archive))
        assert loaded.from_cache is False
        assert loaded.chunk_count == 4
        assert loaded.report.chunks_validated == 4
        assert (loaded.directory / MANIFEST_FILENAME).is_file()

    def test_the_installed_artifact_carries_a_provenance_marker(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        loaded = load_artifact(_config(tmp_path, digest), _store(archive))
        assert (loaded.directory / MARKER_FILENAME).read_text(encoding="utf-8") == digest

    def test_nothing_is_left_in_staging(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        config = _config(tmp_path, digest)
        load_artifact(config, _store(archive))
        assert list(config.download_dir.iterdir()) == []


class TestCaching:
    """A cache is reused only when it matches, and is re-verified every time."""

    def test_a_valid_cache_is_reused_without_downloading(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        config = _config(tmp_path, digest)
        load_artifact(config, _store(archive))

        class Refuses:
            def download(self, bucket: str, key: str, destination: Path) -> None:
                raise AssertionError("a valid cache must not be re-downloaded")

        loaded = load_artifact(config, Refuses())
        assert loaded.from_cache is True

    def test_a_cache_without_a_marker_is_not_trusted(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        config = _config(tmp_path, digest)
        loaded = load_artifact(config, _store(archive))
        (loaded.directory / MARKER_FILENAME).unlink()
        assert load_artifact(config, _store(archive)).from_cache is False

    def test_a_cache_whose_marker_disagrees_is_not_trusted(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        config = _config(tmp_path, digest)
        loaded = load_artifact(config, _store(archive))
        (loaded.directory / MARKER_FILENAME).write_text("b" * 64, encoding="utf-8")
        assert load_artifact(config, _store(archive)).from_cache is False

    def test_a_corrupted_cache_is_re_downloaded_not_served(self, tmp_path: Path) -> None:
        """The disk is persistent and outlives the process. Re-verify every start."""
        archive, digest = build_archive(tmp_path)
        config = _config(tmp_path, digest)
        loaded = load_artifact(config, _store(archive))
        (loaded.directory / "chunks.jsonl.gz").write_bytes(b"corrupted")
        assert load_artifact(config, _store(archive)).from_cache is False

    def test_a_corrupted_cache_with_no_source_fails_closed(self, tmp_path: Path) -> None:
        """Falling back to the damaged cache is exactly what must not happen."""
        archive, digest = build_archive(tmp_path)
        config = _config(tmp_path, digest)
        loaded = load_artifact(config, _store(archive))
        (loaded.directory / "chunks.jsonl.gz").write_bytes(b"corrupted")
        archive.unlink()
        with pytest.raises(ArtifactNotFoundError):
            load_artifact(config, _store(archive))

    def test_a_different_version_does_not_reuse_the_cache(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        load_artifact(_config(tmp_path, digest), _store(archive))
        other = _config(tmp_path, digest, version="99")
        assert load_artifact(other, _store(archive)).from_cache is False


class TestLoadingFailsClosed:
    """Each of these is a way an artifact can be wrong. None of them serves."""

    def test_a_missing_object_is_refused(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        archive.unlink()
        with pytest.raises(ArtifactNotFoundError):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_an_unconfigured_deployment_is_refused(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        config = RuntimeArtifactConfig(
            version="", archive_sha256="", bucket="", key="", cache_root=tmp_path / "cache"
        )
        with pytest.raises(ArtifactConfigurationError):
            load_artifact(config, _store(archive))

    def test_a_wrong_archive_digest_is_refused(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        with pytest.raises(ArtifactIntegrityError):
            load_artifact(_config(tmp_path, VALID_DIGEST), _store(archive))

    def test_a_corrupt_archive_is_refused(self, tmp_path: Path) -> None:
        archive, digest = build_archive(tmp_path)
        archive.write_bytes(b"not a gzip stream at all")
        with pytest.raises(ArtifactIntegrityError):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_a_tampered_manifest_is_refused(self, tmp_path: Path) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        manifest_path = source / MANIFEST_FILENAME
        payload = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(
            payload.replace('"corpus_version": "corpus-test"', '"corpus_version": "tampered"'),
            encoding="utf-8",
        )
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactIntegrityError):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_a_corrupted_corpus_file_is_refused(self, tmp_path: Path) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        (source / "chunks.jsonl.gz").write_bytes(b"not gzip")
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactIntegrityError):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_a_reordered_corpus_is_refused(self, tmp_path: Path) -> None:
        """The failure a content digest alone cannot detect."""
        source = build_artifact_dir(tmp_path / "artifact")
        reorder_corpus(source)
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactCompatibilityError, match="ordering"):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_a_vector_count_mismatch_is_refused(self, tmp_path: Path) -> None:
        """The index has the wrong number of rows to align with the corpus.

        The manifest cannot itself declare a mismatch — the schema refuses to
        build one — so the realistic failure is an index file whose header
        disagrees with a perfectly consistent manifest.
        """
        source = build_artifact_dir(tmp_path / "artifact")
        corrupt_faiss_header(source, vectors=99)
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactCompatibilityError, match="vector count"):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_a_dimensionality_mismatch_is_refused(self, tmp_path: Path) -> None:
        """A different embedding model produced this index than the manifest names."""
        source = build_artifact_dir(tmp_path / "artifact")
        corrupt_faiss_header(source, vectors=4, dimensions=1536)
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactCompatibilityError, match="dimensionality"):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_an_incompatible_schema_version_is_refused(self, tmp_path: Path) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        rewrite_manifest_raw(source, schema_version="99.0")
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactVersionError):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_an_artifact_naming_no_source_pack_is_refused(self, tmp_path: Path) -> None:
        """Governance would have nothing to check the served documents against."""
        source = build_artifact_dir(tmp_path / "artifact", source_pack_version=None)
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactCompatibilityError, match="source pack"):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_a_truncated_corpus_is_refused(self, tmp_path: Path) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        truncate_corpus(source, keep=2)
        archive = tmp_path / "synapse-runtime-2.tar.gz"
        digest = pack_archive(source, archive)
        with pytest.raises(ArtifactIntegrityError, match="chunk count"):
            load_artifact(_config(tmp_path, digest), _store(archive))

    def test_a_failed_load_installs_nothing(self, tmp_path: Path) -> None:
        """Nothing partially-verified reaches the path the backends read from."""
        archive, _ = build_archive(tmp_path)
        config = _config(tmp_path, VALID_DIGEST)
        with pytest.raises(ArtifactIntegrityError):
            load_artifact(config, _store(archive))
        assert not config.artifact_dir.exists()

    def test_verify_directory_refuses_a_bad_artifact(self, tmp_path: Path) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        corrupt_faiss_header(source, vectors=99)
        with pytest.raises(ArtifactCompatibilityError):
            verify_directory(source)


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _embed(_text: str) -> list[float]:
    """A deterministic offline embedder. Never called: the FAISS stub is a header."""
    return [0.0] * 8


class TestReadiness:
    """Default false, and true only at the end of every gate."""

    def test_readiness_starts_false(self, tmp_path: Path) -> None:
        index = RuntimeIndex(_config(tmp_path, VALID_DIGEST), _store(tmp_path), _embed)
        assert index.ready is False
        assert index.readiness.state is ReadinessState.STARTING

    def test_backends_are_unreachable_before_start(self, tmp_path: Path) -> None:
        index = RuntimeIndex(_config(tmp_path, VALID_DIGEST), _store(tmp_path), _embed)
        with pytest.raises(NotReadyError):
            _ = index.backends

    def test_an_unconfigured_deployment_reports_configuration_error(self, tmp_path: Path) -> None:
        config = RuntimeArtifactConfig(
            version="", archive_sha256="", bucket="", key="", cache_root=tmp_path / "cache"
        )
        index = RuntimeIndex(config, _store(tmp_path), _embed)
        readiness = index.start()
        assert readiness.ready is False
        assert readiness.failure_code is AnswerFailureCode.CONFIGURATION_ERROR

    def test_a_bad_artifact_reports_index_unverified(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        index = RuntimeIndex(_config(tmp_path, VALID_DIGEST), _store(archive), _embed)
        readiness = index.start()
        assert readiness.ready is False
        assert readiness.failure_code is AnswerFailureCode.INDEX_UNVERIFIED
        assert readiness.state is ReadinessState.FAILED

    def test_a_failed_start_leaves_backends_unreachable(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        index = RuntimeIndex(_config(tmp_path, VALID_DIGEST), _store(archive), _embed)
        index.start()
        with pytest.raises(NotReadyError):
            _ = index.backends

    def test_start_never_raises(self, tmp_path: Path) -> None:
        """A container that reports itself unready is worth more than one that dies."""
        archive, _ = build_archive(tmp_path)
        index = RuntimeIndex(_config(tmp_path, VALID_DIGEST), _store(archive), _embed)
        assert index.start().ready is False

    def test_the_detail_is_a_type_name_never_a_message(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        index = RuntimeIndex(_config(tmp_path, VALID_DIGEST), _store(archive), _embed)
        readiness = index.start()
        assert readiness.detail == "ArtifactIntegrityError"
        assert " " not in readiness.detail

    def test_the_readiness_payload_carries_no_bucket_key_or_path(self, tmp_path: Path) -> None:
        archive, _ = build_archive(tmp_path)
        index = RuntimeIndex(_config(tmp_path, VALID_DIGEST), _store(archive), _embed)
        payload = str(index.start().as_dict())
        assert "test-bucket" not in payload
        assert "synapse-runtime-2.tar.gz" not in payload


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestPackagingCommand:
    """The step between migration and deployment. It verifies before it packs."""

    def test_it_verifies_packs_and_prints_the_configuration(self, tmp_path: Path, capsys) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        output = tmp_path / "dist" / "synapse-runtime-2.tar.gz"
        assert package_runtime.main(["--artifact-dir", str(source), "--output", str(output)]) == 0
        printed = capsys.readouterr().out
        assert output.is_file()
        # The digest is the pin; it is useless if it does not reach the operator.
        assert "SYNAPSE_ARTIFACT_SHA256=" in printed
        assert "SYNAPSE_ARTIFACT_VERSION=2" in printed

    def test_the_printed_digest_is_the_archive_digest(self, tmp_path: Path, capsys) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        output = tmp_path / "out.tar.gz"
        package_runtime.main(["--artifact-dir", str(source), "--output", str(output)])
        printed = capsys.readouterr().out
        digest = next(
            line.split("=", 1)[1].strip()
            for line in printed.splitlines()
            if "SYNAPSE_ARTIFACT_SHA256=" in line
        )
        # The value an operator pastes must be the one the loader will check.
        assert verify_archive_digest(output, digest) == digest

    def test_it_refuses_to_pack_an_artifact_that_would_not_load(
        self, tmp_path: Path, capsys
    ) -> None:
        """Discovering this in production means a container nobody can diagnose."""
        source = build_artifact_dir(tmp_path / "artifact")
        corrupt_faiss_header(source, vectors=99)
        output = tmp_path / "out.tar.gz"
        assert package_runtime.main(["--artifact-dir", str(source), "--output", str(output)]) == 1
        assert "REFUSED" in capsys.readouterr().out
        assert not output.exists()

    def test_it_refuses_to_overwrite_without_force(self, tmp_path: Path, capsys) -> None:
        source = build_artifact_dir(tmp_path / "artifact")
        output = tmp_path / "out.tar.gz"
        output.write_bytes(b"existing")
        assert package_runtime.main(["--artifact-dir", str(source), "--output", str(output)]) == 2
        assert "REFUSED" in capsys.readouterr().out
        assert output.read_bytes() == b"existing"

    def test_a_packed_archive_loads_through_the_real_path(self, tmp_path: Path, capsys) -> None:
        """End to end: pack, then load exactly as the container would."""
        source = build_artifact_dir(tmp_path / "artifact")
        output = tmp_path / "synapse-runtime-2.tar.gz"
        package_runtime.main(["--artifact-dir", str(source), "--output", str(output)])
        digest = next(
            line.split("=", 1)[1].strip()
            for line in capsys.readouterr().out.splitlines()
            if "SYNAPSE_ARTIFACT_SHA256=" in line
        )
        loaded = load_artifact(_config(tmp_path, digest), _store(output))
        assert loaded.chunk_count == 4


class TestNoPickleOnTheRuntimePath:
    """The format is gone from the deployed runtime, not merely discouraged."""

    def test_no_runtime_module_imports_pickle(self) -> None:
        for source_file in sorted(RUNTIME_ROOT.rglob("*.py")):
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all(a.name.split(".")[0] != "pickle" for a in node.names), (
                        source_file.name
                    )
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] != "pickle", source_file.name

    def test_the_native_backends_import_no_pickle(self) -> None:
        source = (REPO_ROOT / "synapse" / "retrieval" / "native.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] != "pickle" for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "pickle"

    def test_no_runtime_module_references_the_quarantined_reader(self) -> None:
        for source_file in sorted(RUNTIME_ROOT.rglob("*.py")):
            assert "_legacy.pickle_reader" not in source_file.read_text(encoding="utf-8")

    def test_no_runtime_module_imports_the_legacy_tree(self) -> None:
        legacy = {"Data", "Retrieval", "Generation", "Evaluation"}
        for source_file in sorted(RUNTIME_ROOT.rglob("*.py")):
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] not in legacy, source_file.name


class TestNoUnmanagedFallback:
    """There is no path that serves the manifest-less prototype index."""

    def test_no_runtime_module_names_the_unmanaged_index_directory(self) -> None:
        """No *code* may name ``hybrid_index``. The docstrings may, and do.

        Checked over the AST with docstrings excluded rather than by searching
        the text, because these modules explain at length why the fallback does
        not exist — a substring search would match that explanation and make the
        test vacuous.
        """
        for source_file in sorted(RUNTIME_ROOT.rglob("*.py")):
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ):
                    assert "hybrid_index" not in node.value, (
                        f"{source_file.name}:{node.lineno} names the unmanaged index in code"
                    )

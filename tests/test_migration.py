"""
Migration tests: the restricted pickle reader and the migration CLI.

The security-critical assertion is
``test_disallowed_global_is_refused_before_resolution``. It feeds the reader a
pickle stream naming ``os.system`` and asserts refusal. That stream is only
ever passed to :class:`_RestrictedUnpickler`, whose ``find_class`` raises
before the name is resolved — so ``os.system`` is never imported and never
called. It is a defensive fixture, not an executable payload.
"""

from __future__ import annotations

import pickle
import sys
import types
from collections import (
    OrderedDict,  # A benign stdlib class, used to prove the allow-list is exact rather than merely blocking obvious threats
)
from pathlib import Path

import pytest

from synapse._legacy.pickle_reader import LegacyChunk, load_legacy_chunks
from synapse.cli.migrate_artifacts import main
from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError, UnsafeArtifactError
from synapse.index import MANIFEST_FILENAME, load_verified_corpus, verify_artifacts
from tests.conftest import REPO_ROOT

# A protocol-0 pickle whose GLOBAL opcode names os.system. NEVER passed to
# pickle.load/loads anywhere in this suite — only to the restricted reader,
# which refuses it at find_class before the name is resolved.
DANGEROUS_STREAM = b"cos\nsystem\n(S'true'\ntR."

LEGACY_MODULE = (
    "Data.fetch_and_chunk"  # The module path the shipped artifacts name in their byte stream
)


class _ChunkStub:
    """Stand-in for the legacy ``Chunk`` dataclass.

    Defined at module scope, not inside the fixture: pickle resolves a class by
    ``__module__`` + ``__qualname__``, and a function-local class has a
    qualname containing ``<locals>`` that can never be resolved.

    A plain class with a ``__dict__`` is used deliberately, because that is the
    state shape the real dataclass pickles to.
    """

    def __init__(
        self,
        text,
        source="pubmed",
        pmid="",
        title="",
        chunk_index=0,
        total_chunks=1,
        source_url="",
        page=-1,
    ):
        self.text = text
        self.source = source
        self.pmid = pmid
        self.title = title
        self.chunk_index = chunk_index
        self.total_chunks = total_chunks
        self.source_url = source_url
        self.page = page


_ChunkStub.__module__ = (
    LEGACY_MODULE  # Makes pickle emit the legacy global reference the real artifacts carry
)
_ChunkStub.__qualname__ = (
    "Chunk"  # Resolvable name; pickle looks up sys.modules[LEGACY_MODULE].Chunk
)
_ChunkStub.__name__ = "Chunk"


@pytest.fixture
def legacy_chunk_class(monkeypatch):
    """Install the stand-in as ``Data.fetch_and_chunk.Chunk`` for the duration of a test.

    Injected into ``sys.modules`` rather than importing the real module, which
    would pull in langchain and perform I/O at import time.
    """
    package = types.ModuleType("Data")
    module = types.ModuleType(LEGACY_MODULE)
    module.Chunk = _ChunkStub
    monkeypatch.setitem(
        sys.modules, "Data", package
    )  # monkeypatch restores sys.modules after the test
    monkeypatch.setitem(sys.modules, LEGACY_MODULE, module)
    return _ChunkStub


@pytest.fixture
def legacy_pickle(tmp_path: Path, legacy_chunk_class) -> Path:
    """A small legacy corpus pickle containing a deliberately duplicated document."""
    chunks = [
        legacy_chunk_class(
            "Metformin is a first-line therapy for type 2 diabetes.",
            pmid="111",
            title="A",
            chunk_index=0,
            total_chunks=2,
            source_url="https://pubmed.ncbi.nlm.nih.gov/111/",
        ),
        legacy_chunk_class(
            "It lowers HbA1c by one to two percentage points.",
            pmid="111",
            title="A",
            chunk_index=1,
            total_chunks=2,
            source_url="https://pubmed.ncbi.nlm.nih.gov/111/",
        ),
        legacy_chunk_class(
            "Hypertension affects over one billion adults worldwide.",
            pmid="222",
            title="B",
            chunk_index=0,
            total_chunks=1,
            source_url="https://pubmed.ncbi.nlm.nih.gov/222/",
        ),
        # The same article fetched again under a different topic: identical text,
        # identical chunk_index. Under the legacy scheme these collide.
        legacy_chunk_class(
            "Metformin is a first-line therapy for type 2 diabetes.",
            pmid="111",
            title="A",
            chunk_index=0,
            total_chunks=2,
            source_url="https://pubmed.ncbi.nlm.nih.gov/111/",
        ),
    ]
    path = tmp_path / "legacy.pkl"
    path.write_bytes(pickle.dumps(chunks))  # Writing a fixture, not reading untrusted data
    return path


class TestRestrictedUnpickler:
    """The allow-list must refuse everything outside it."""

    def test_disallowed_global_is_refused_before_resolution(self, tmp_path: Path) -> None:
        path = tmp_path / "hostile.pkl"
        path.write_bytes(DANGEROUS_STREAM)
        with pytest.raises(UnsafeArtifactError) as excinfo:
            load_legacy_chunks(path)
        assert (
            excinfo.value.details["module"] == "os"
        )  # Reported so an operator can see exactly what was rejected
        assert excinfo.value.details["name"] == "system"

    def test_other_benign_but_disallowed_class_is_refused(self, tmp_path: Path) -> None:
        # The allow-list is exact: even a harmless stdlib class is refused,
        # because a permissive list is how these mitigations decay.
        path = tmp_path / "ordered.pkl"
        path.write_bytes(
            pickle.dumps(OrderedDict([("a", 1)]))
        )  # Writing a fixture, not reading untrusted data
        with pytest.raises(UnsafeArtifactError) as excinfo:
            load_legacy_chunks(path)
        assert excinfo.value.details["module"] == "collections"

    def test_allowed_class_is_mapped_to_the_inert_stand_in(self, legacy_pickle: Path) -> None:
        # Even the permitted name resolves to LegacyChunk, never to the real class.
        loaded = load_legacy_chunks(legacy_pickle)
        assert all(isinstance(item, LegacyChunk) for item in loaded)

    def test_oversized_file_is_refused_before_reading(self, tmp_path: Path, monkeypatch) -> None:
        import synapse._legacy.pickle_reader as reader

        monkeypatch.setattr(reader, "MAX_PICKLE_BYTES", 8)
        path = tmp_path / "big.pkl"
        path.write_bytes(pickle.dumps(["x" * 100]))
        with pytest.raises(UnsafeArtifactError, match="maximum permitted size"):
            load_legacy_chunks(path)

    def test_corrupt_stream_raises_typed_error(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.pkl"
        path.write_bytes(b"\x80\x04garbage-not-a-pickle")
        with pytest.raises(ArtifactSchemaError):
            load_legacy_chunks(path)

    def test_missing_file_raises_typed_error(self, tmp_path: Path) -> None:
        with pytest.raises(ArtifactNotFoundError):
            load_legacy_chunks(tmp_path / "absent.pkl")

    def test_non_list_payload_is_refused(self, tmp_path: Path, legacy_chunk_class) -> None:
        path = tmp_path / "dict.pkl"
        path.write_bytes(pickle.dumps({"a": legacy_chunk_class("text")}))
        with pytest.raises(ArtifactSchemaError, match="does not contain a list"):
            load_legacy_chunks(path)

    def test_repr_does_not_leak_chunk_text(self, legacy_pickle: Path) -> None:
        # Tracebacks and logs must not carry third-party or patient-adjacent text.
        loaded = load_legacy_chunks(legacy_pickle)
        assert "Metformin" not in repr(loaded[0])


class TestMigrationCli:
    """End-to-end behaviour of `python -m synapse.cli.migrate_artifacts`."""

    def _args(self, legacy_pickle: Path, out: Path, *extra: str) -> list[str]:
        return [
            "--input",
            str(legacy_pickle),
            "--output-dir",
            str(out),
            "--corpus-version",
            "corpus-test",
            *extra,
        ]

    def test_refuses_without_trust_flag(self, legacy_pickle: Path, tmp_path: Path, capsys) -> None:
        # The gate: unpickling is only ever as safe as the file's provenance.
        assert main(self._args(legacy_pickle, tmp_path / "out")) == 2
        assert not (tmp_path / "out").exists()
        assert "REFUSED" in capsys.readouterr().err

    def test_warning_is_always_displayed(self, legacy_pickle: Path, tmp_path: Path, capsys) -> None:
        main(self._args(legacy_pickle, tmp_path / "out"))
        stderr = capsys.readouterr().err
        assert "PICKLE INPUT MUST COME FROM A TRUSTED SOURCE" in stderr
        assert "trusted" in stderr.lower()

    def test_successful_migration_writes_verified_artifacts(
        self, legacy_pickle: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        assert main(self._args(legacy_pickle, out, "--trust-input")) == 0
        assert (out / "chunks.jsonl.gz").is_file()
        assert (out / "documents.jsonl.gz").is_file()
        assert (out / MANIFEST_FILENAME).is_file()
        verify_artifacts(
            out, deep=True
        )  # Independently re-verified, not just trusting the CLI's own check

    def test_migration_produces_unique_chunk_ids(self, legacy_pickle: Path, tmp_path: Path) -> None:
        # The fixture contains a duplicated document that collides under the
        # legacy scheme. After migration every identifier must be distinct.
        out = tmp_path / "out"
        main(self._args(legacy_pickle, out, "--trust-input"))
        _, chunks = load_verified_corpus(out)
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))

    def test_migration_preserves_order_by_default(
        self, legacy_pickle: Path, tmp_path: Path
    ) -> None:
        # Order preservation is what keeps an existing FAISS index valid.
        out = tmp_path / "out"
        main(self._args(legacy_pickle, out, "--trust-input"))
        _, chunks = load_verified_corpus(out)
        assert len(chunks) == 4  # All four legacy records, duplicate included
        assert chunks[0].text.startswith("Metformin")
        assert chunks[3].text.startswith("Metformin")  # The duplicate, retained in place

    def test_dedupe_drops_duplicates(self, legacy_pickle: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        main(self._args(legacy_pickle, out, "--trust-input", "--dedupe"))
        _, chunks = load_verified_corpus(out)
        assert len(chunks) == 3  # The repeated passage is gone

    def test_dedupe_with_faiss_index_is_refused(
        self, legacy_pickle: Path, tmp_path: Path, capsys
    ) -> None:
        # Deduplication changes record count and order, which would silently
        # misalign an existing index. The CLI refuses rather than allowing it.
        faiss = tmp_path / "index.faiss"
        faiss.write_bytes(b"IxF2" + (1536).to_bytes(4, "little") + (4).to_bytes(8, "little"))
        code = main(
            self._args(
                legacy_pickle,
                tmp_path / "out",
                "--trust-input",
                "--dedupe",
                "--faiss-index",
                str(faiss),
            )
        )
        assert code == 2
        assert "invalidates" in capsys.readouterr().err

    def test_dry_run_writes_nothing(self, legacy_pickle: Path, tmp_path: Path, capsys) -> None:
        out = tmp_path / "out"
        assert main(self._args(legacy_pickle, out, "--trust-input", "--dry-run")) == 0
        assert not out.exists()
        assert "no files were written" in capsys.readouterr().out

    def test_input_is_never_modified(self, legacy_pickle: Path, tmp_path: Path) -> None:
        # One-way and non-destructive: rollback is changing a path, not
        # restoring data.
        before = legacy_pickle.read_bytes()
        main(self._args(legacy_pickle, tmp_path / "out", "--trust-input"))
        assert legacy_pickle.read_bytes() == before
        assert legacy_pickle.is_file()

    def test_migration_writes_no_pickle_files(self, legacy_pickle: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        main(self._args(legacy_pickle, out, "--trust-input"))
        assert list(out.rglob("*.pkl")) == []

    def test_non_empty_output_directory_is_refused_without_force(
        self, legacy_pickle: Path, tmp_path: Path, capsys
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "existing.txt").write_text("keep me", encoding="utf-8")
        assert main(self._args(legacy_pickle, out, "--trust-input")) == 1
        assert (out / "existing.txt").read_text(encoding="utf-8") == "keep me"

    def test_all_migrated_documents_are_unreviewed(
        self, legacy_pickle: Path, tmp_path: Path
    ) -> None:
        # The governance rule, verified end to end: no review metadata exists,
        # so nothing may claim approval.
        from synapse.corpus import read_jsonl
        from synapse.schemas import ApprovalStatus, SourceDocument

        out = tmp_path / "out"
        main(self._args(legacy_pickle, out, "--trust-input"))
        documents = list(read_jsonl(out / "documents.jsonl.gz", SourceDocument))
        assert documents
        assert all(d.approval_status is ApprovalStatus.UNREVIEWED for d in documents)
        assert all(d.review is None for d in documents)

    def test_min_chunk_chars_filter_drops_short_chunks(
        self, tmp_path: Path, legacy_chunk_class
    ) -> None:
        # The shipped corpus contains chunks whose entire text is ".".
        chunks = [
            legacy_chunk_class(
                ".", pmid="111", title="A", source_url="https://pubmed.ncbi.nlm.nih.gov/111/"
            ),
            legacy_chunk_class(
                "A passage long enough to be worth indexing at all.",
                pmid="111",
                chunk_index=1,
                title="A",
                source_url="https://pubmed.ncbi.nlm.nih.gov/111/",
            ),
        ]
        source = tmp_path / "short.pkl"
        source.write_bytes(pickle.dumps(chunks))
        out = tmp_path / "out"
        assert (
            main(
                [
                    "--input",
                    str(source),
                    "--output-dir",
                    str(out),
                    "--corpus-version",
                    "c",
                    "--trust-input",
                    "--min-chunk-chars",
                    "20",
                ]
            )
            == 0
        )
        _, migrated = load_verified_corpus(out)
        assert len(migrated) == 1


@pytest.mark.skipif(
    not (REPO_ROOT / "processed_chunks.pkl").is_file(), reason="legacy corpus artifact not present"
)
class TestRealCorpusMigration:
    """Integration test against the artifact actually in the repository."""

    def test_real_corpus_migrates_and_verifies(self, tmp_path: Path) -> None:
        out = tmp_path / "corpus-v2"
        code = main(
            [
                "--input",
                str(REPO_ROOT / "processed_chunks.pkl"),
                "--output-dir",
                str(out),
                "--corpus-version",
                "corpus-v2",
                "--trust-input",
            ]
        )
        assert code == 0
        manifest, chunks = load_verified_corpus(out)
        # Counts independently measured from the shipped artifact.
        assert manifest.chunk_count == 2220
        assert manifest.document_count == 847
        assert len({c.chunk_id for c in chunks}) == 2220  # 162 legacy collisions resolved

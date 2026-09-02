"""
JSONL round-trip, determinism and malformed-record tests.

Determinism matters as much as correctness here: the corpus digest recorded in
an index manifest is computed over these bytes, so if writing the same records
twice produced different files, verification would fail spuriously. Gzip is the
subtle case — its header carries a timestamp and the original filename, both of
which the writer suppresses.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from synapse.corpus import read_jsonl, records_digest, write_jsonl
from synapse.corpus.jsonl import JsonlReadError
from synapse.errors import ArtifactNotFoundError, ArtifactSchemaError, ArtifactVersionError
from synapse.schemas.chunk import EvidenceChunk


class TestRoundTrip:
    """Records must survive a write/read cycle unchanged."""

    @pytest.mark.parametrize("filename", ["chunks.jsonl", "chunks.jsonl.gz"])
    def test_round_trip_preserves_records(self, tmp_path: Path, chunks, filename: str) -> None:
        path = tmp_path / filename
        written = write_jsonl(path, chunks)
        recovered = list(read_jsonl(path, EvidenceChunk))
        assert written == len(chunks)
        assert recovered == chunks  # Full structural equality, not just field spot-checks

    def test_round_trip_preserves_order(self, tmp_path: Path, chunks) -> None:
        # Order is load-bearing: FAISS row i is bound to corpus record i.
        path = tmp_path / "chunks.jsonl"
        write_jsonl(path, chunks)
        assert [c.chunk_id for c in read_jsonl(path, EvidenceChunk)] == [c.chunk_id for c in chunks]

    def test_round_trip_preserves_unicode(self, tmp_path: Path, make_chunk) -> None:
        chunk = make_chunk(
            text="Hypertension affects >1.28 billion adults — a leading cause of disease."
        )
        path = tmp_path / "u.jsonl"
        write_jsonl(path, [chunk])
        assert next(iter(read_jsonl(path, EvidenceChunk))).text == chunk.text

    def test_empty_corpus_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        assert write_jsonl(path, []) == 0
        assert list(read_jsonl(path, EvidenceChunk)) == []

    def test_blank_lines_are_tolerated(self, tmp_path: Path, chunks) -> None:
        # A trailing newline or a stray blank line is not corruption.
        path = tmp_path / "c.jsonl"
        write_jsonl(path, chunks)
        path.write_text(path.read_text() + "\n\n", encoding="utf-8")
        assert len(list(read_jsonl(path, EvidenceChunk))) == len(chunks)


class TestDeterminism:
    """Identical records must always produce identical bytes."""

    @pytest.mark.parametrize("filename", ["chunks.jsonl", "chunks.jsonl.gz"])
    def test_writing_twice_produces_identical_bytes(
        self, tmp_path: Path, chunks, filename: str
    ) -> None:
        first, second = tmp_path / f"a-{filename}", tmp_path / f"b-{filename}"
        write_jsonl(first, chunks)
        write_jsonl(second, chunks)
        assert first.read_bytes() == second.read_bytes()

    def test_gzip_header_carries_no_timestamp_or_filename(self, tmp_path: Path, chunks) -> None:
        # These are the two non-deterministic gzip header fields; both are
        # suppressed so a rebuilt corpus hashes identically.
        path = tmp_path / "c.jsonl.gz"
        write_jsonl(path, chunks)
        header = path.read_bytes()[:10]
        mtime = int.from_bytes(header[4:8], "little")
        assert mtime == 0
        with gzip.open(path, "rb") as handle:
            assert handle.read()  # Still a readable gzip stream

    def test_digest_is_independent_of_compression(self, tmp_path: Path, chunks) -> None:
        # records_digest hashes the records, not the file framing, so gzipping
        # or un-gzipping a corpus does not invalidate a manifest.
        plain, compressed = tmp_path / "c.jsonl", tmp_path / "c.jsonl.gz"
        write_jsonl(plain, chunks)
        write_jsonl(compressed, chunks)
        assert records_digest(read_jsonl(plain, EvidenceChunk)) == records_digest(
            read_jsonl(compressed, EvidenceChunk)
        )

    def test_digest_changes_when_order_changes(self, tmp_path: Path, chunks) -> None:
        # The property that lets a manifest detect a reordered corpus.
        assert records_digest(chunks) != records_digest(list(reversed(chunks)))

    def test_digest_changes_when_content_changes(self, make_chunk) -> None:
        assert records_digest([make_chunk(text="alpha text here")]) != records_digest(
            [make_chunk(text="beta text here")]
        )


class TestMalformedRecords:
    """Every malformed input must raise a typed error naming the line."""

    def test_missing_file_raises_typed_error(self, tmp_path: Path) -> None:
        with pytest.raises(ArtifactNotFoundError):
            list(read_jsonl(tmp_path / "absent.jsonl", EvidenceChunk))

    def test_invalid_json_raises_with_line_number(self, tmp_path: Path, chunks) -> None:
        path = tmp_path / "c.jsonl"
        write_jsonl(path, chunks)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not valid json\n")
        with pytest.raises(JsonlReadError) as excinfo:
            list(read_jsonl(path, EvidenceChunk))
        assert (
            excinfo.value.details["line"] == len(chunks) + 1
        )  # Points at the offending line, not just "somewhere in the file"

    def test_json_array_line_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "c.jsonl"
        path.write_text('["not", "an", "object"]\n', encoding="utf-8")
        with pytest.raises(JsonlReadError, match="not a JSON object"):
            list(read_jsonl(path, EvidenceChunk))

    def test_json_scalar_line_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "c.jsonl"
        path.write_text("42\n", encoding="utf-8")
        with pytest.raises(JsonlReadError, match="not a JSON object"):
            list(read_jsonl(path, EvidenceChunk))

    def test_record_failing_schema_validation_is_rejected(self, tmp_path: Path, chunks) -> None:
        path = tmp_path / "c.jsonl"
        write_jsonl(path, chunks)
        payload = json.loads(path.read_text().splitlines()[0])
        payload["text"] = "tampered text that no longer matches its recorded digest"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with pytest.raises(JsonlReadError, match="failed schema validation"):
            list(read_jsonl(path, EvidenceChunk))

    def test_record_with_unknown_field_is_rejected(self, tmp_path: Path, chunks) -> None:
        path = tmp_path / "c.jsonl"
        write_jsonl(path, chunks)
        payload = json.loads(path.read_text().splitlines()[0])
        payload["injected_field"] = "surprise"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with pytest.raises(JsonlReadError, match="failed schema validation"):
            list(read_jsonl(path, EvidenceChunk))

    def test_oversized_line_is_refused_before_parsing(self, tmp_path: Path, monkeypatch) -> None:
        # Bounds the work a malformed or hostile file can cause.
        import synapse.corpus.jsonl as jsonl_module

        monkeypatch.setattr(jsonl_module, "MAX_LINE_BYTES", 32)
        path = tmp_path / "big.jsonl"
        path.write_text(json.dumps({"text": "x" * 500}) + "\n", encoding="utf-8")
        with pytest.raises(JsonlReadError, match="exceeds maximum size"):
            list(read_jsonl(path, EvidenceChunk))


class TestErrorSafety:
    """Errors must never leak record payload text."""

    def test_error_message_does_not_echo_record_content(self, tmp_path: Path) -> None:
        secret = "PATIENT-IDENTIFIABLE-SENTINEL-STRING"
        path = tmp_path / "c.jsonl"
        path.write_text(json.dumps({"text": secret, "bogus": secret}) + "\n", encoding="utf-8")
        with pytest.raises(ArtifactSchemaError) as excinfo:
            list(read_jsonl(path, EvidenceChunk))
        assert secret not in str(excinfo.value)  # The whole point of the _SafeError base class

    def test_error_message_reduces_path_to_basename(self, tmp_path: Path) -> None:
        # A full path can leak a username or a patient identifier from a
        # directory name, so only the basename is rendered.
        with pytest.raises(ArtifactNotFoundError) as excinfo:
            list(read_jsonl(tmp_path / "absent.jsonl", EvidenceChunk))
        assert str(tmp_path) not in str(excinfo.value)
        assert "absent.jsonl" in str(excinfo.value)


class TestSchemaVersions:
    """Version compatibility is enforced on read, not assumed."""

    def _write_with_version(self, path: Path, chunks, version: str) -> None:
        """Write a corpus whose first record declares ``version``."""
        payload = json.loads(chunks[0].model_dump_json())
        payload["schema_version"] = version
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_same_version_is_accepted(self, tmp_path: Path, chunks) -> None:
        path = tmp_path / "c.jsonl"
        self._write_with_version(path, chunks, EvidenceChunk.SCHEMA_VERSION)
        assert len(list(read_jsonl(path, EvidenceChunk))) == 1

    def test_older_minor_version_is_accepted(self, tmp_path: Path, chunks) -> None:
        # Fields added since are optional by policy, so an older record still loads.
        path = tmp_path / "c.jsonl"
        self._write_with_version(path, chunks, "1.0")
        assert len(list(read_jsonl(path, EvidenceChunk))) == 1

    def test_newer_minor_version_is_refused(self, tmp_path: Path, chunks) -> None:
        # Fail closed: the file was written by a build that may rely on fields
        # this build does not understand.
        path = tmp_path / "c.jsonl"
        self._write_with_version(path, chunks, "1.99")
        with pytest.raises(ArtifactVersionError):
            list(read_jsonl(path, EvidenceChunk))

    def test_different_major_version_is_refused(self, tmp_path: Path, chunks) -> None:
        path = tmp_path / "c.jsonl"
        self._write_with_version(path, chunks, "2.0")
        with pytest.raises(ArtifactVersionError):
            list(read_jsonl(path, EvidenceChunk))

    @pytest.mark.parametrize("version", ["1", "1.2.3", "", "x.y", "-1.0"])
    def test_malformed_version_is_refused(self, tmp_path: Path, chunks, version: str) -> None:
        path = tmp_path / "c.jsonl"
        self._write_with_version(path, chunks, version)
        with pytest.raises((ArtifactVersionError, JsonlReadError)):
            list(read_jsonl(path, EvidenceChunk))

    def test_version_is_stamped_when_absent(self, make_chunk) -> None:
        # Callers never have to pass schema_version; it is filled from the class.
        assert make_chunk().schema_version == EvidenceChunk.SCHEMA_VERSION

    def test_version_error_names_the_schema_and_both_versions(self, tmp_path: Path, chunks) -> None:
        path = tmp_path / "c.jsonl"
        self._write_with_version(path, chunks, "2.0")
        with pytest.raises(ArtifactVersionError) as excinfo:
            list(read_jsonl(path, EvidenceChunk))
        details = excinfo.value.details
        assert details["found"] == "2.0"
        assert details["supported"] == EvidenceChunk.SCHEMA_VERSION

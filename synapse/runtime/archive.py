"""
synapse.runtime.archive
=======================
Packing and unpacking the runtime artifact archive, safely.

The deployed container does not build an index. It downloads one archive,
verifies it, and serves from it. That archive arrives over a network from object
storage, which makes it **untrusted input** — and a tar file is one of the more
hostile formats a program can be handed.

What this module refuses, and why each matters
----------------------------------------------
``tarfile.extractall`` is never called. Every member is inspected and extracted
individually, because the default behaviour will happily write outside the
destination:

* **Absolute paths** (``/etc/cron.d/x``) — writes anywhere the process can.
* **Parent traversal** (``../../app/main.py``) — the classic Zip Slip, and the
  reason this is a security control rather than a robustness one.
* **Symlinks and hardlinks** — a link member is a two-step traversal: create
  ``link -> /``, then write through it. Both types are refused outright; a
  runtime artifact has no legitimate use for either.
* **Device, FIFO and other special members** — nothing in a corpus archive is a
  character device.
* **Unexpected names** — only the four members a runtime archive is defined to
  contain are extracted. An archive carrying a fifth file is refused rather than
  partially trusted, because "extra files were ignored" is how a payload gets
  staged next to the artifact it hides behind.
* **Oversized members** — a decompression bomb is refused on the declared size
  *and* on the bytes actually written, since the declared size is attacker-
  controlled and may lie.

Determinism
-----------
:func:`pack_archive` writes a byte-reproducible archive: members in a fixed
order, timestamps and ownership zeroed, gzip header mtime zeroed. That is what
makes "the configured archive SHA-256" a stable value an operator can pin in
configuration and compare against, rather than something that changes on every
repack of identical content.

Errors
------
Everything here raises :class:`~synapse.errors.ArtifactIntegrityError`, which
:func:`synapse.ui.errors.classify` already maps to ``index_unverified``. An
archive that fails any check is not a degraded archive; it is one whose contents
are not what they claim to be, and the only safe response is to serve nothing.
"""

from __future__ import annotations  # Postponed annotations

import gzip
import shutil
import tarfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from synapse.errors import ArtifactIntegrityError, ArtifactNotFoundError
from synapse.hashing import sha256_file, short_digest
from synapse.logging import get_logger

logger = get_logger(__name__)

# The complete contents of a runtime archive. An archive containing anything
# else is refused; an archive missing any of these is refused. Ordered, because
# pack_archive writes them in this sequence to keep the output byte-stable.
RUNTIME_MEMBERS: tuple[str, ...] = (
    "manifest.json",
    "chunks.jsonl.gz",
    "documents.jsonl.gz",
    "index.faiss",
)

# Ceilings for a single member and for the archive as a whole, applied to the
# bytes actually written rather than to the header's declared size. The corpus
# this repository ships is a few megabytes; these are generous by two orders of
# magnitude and exist only to bound a hostile archive.
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

# Copy buffer. Large enough that extraction is not syscall-bound, small enough
# that the size ceiling is enforced with fine granularity.
_CHUNK_BYTES = 1024 * 1024


def pack_archive(source: Path, output: Path, *, members: Iterable[str] = RUNTIME_MEMBERS) -> str:
    """Write a deterministic ``.tar.gz`` of ``source``, returning its SHA-256.

    Every member must exist in ``source``. The digest returned is the value an
    operator pins as ``SYNAPSE_ARTIFACT_SHA256``; repacking identical content
    reproduces it exactly.

    Raises:
        ArtifactNotFoundError: a declared member is missing from ``source``.
    """
    names = list(members)
    for name in names:
        if not (source / name).is_file():
            raise ArtifactNotFoundError(member=name, directory=source)

    output.parent.mkdir(parents=True, exist_ok=True)
    # GzipFile with mtime=0 rather than tarfile's "w:gz": the gzip header
    # embeds a timestamp, and letting it default makes the digest change on
    # every repack of identical bytes.
    with (
        output.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for name in names:
            path = source / name
            info = archive.gettarinfo(str(path), arcname=name)
            # Zero everything that varies between machines and runs. What
            # remains is the content and the name, which is the whole point.
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            with path.open("rb") as handle:
                archive.addfile(info, handle)

    digest = sha256_file(output)
    logger.info(
        "runtime archive packed",
        extra={
            "members": len(names),
            "size_bytes": output.stat().st_size,
            "archive_sha256": short_digest(digest),
        },
    )
    return digest


def verify_archive_digest(archive: Path, expected_sha256: str) -> str:
    """Confirm the archive's bytes match the configured digest.

    The first gate, before a single byte is decompressed: an archive whose
    digest does not match is not opened at all, so a malformed or hostile tar
    stream is never parsed.

    Raises:
        ArtifactNotFoundError: the archive is not on disk.
        ArtifactIntegrityError: the digest does not match.
    """
    if not archive.is_file():
        raise ArtifactNotFoundError(artifact="runtime archive", path=archive)
    actual = sha256_file(archive)
    if actual != expected_sha256:
        raise ArtifactIntegrityError(
            artifact="runtime archive",
            problem="archive digest mismatch",
            expected=short_digest(expected_sha256),
            actual=short_digest(actual),
        )
    return actual


def safe_extract(archive: Path, destination: Path) -> list[str]:
    """Extract exactly the runtime members into ``destination``.

    Returns the member names written, in archive order. ``destination`` is
    created if absent and must be a directory the caller controls — this
    function does not clean it up, because the caller stages into a temporary
    directory and moves the result into place atomically.

    Raises:
        ArtifactIntegrityError: any member is unsafe, unexpected, missing or
            oversized. See the module docstring for the full list.
    """
    destination.mkdir(parents=True, exist_ok=True)
    expected = set(RUNTIME_MEMBERS)
    written: list[str] = []
    total = 0

    with tarfile.open(archive, mode="r:gz") as tar:
        for info in tar:
            name = _safe_member_name(info, archive)
            if name not in expected:
                # Refused, not skipped. See the module docstring.
                raise ArtifactIntegrityError(
                    artifact="runtime archive",
                    problem="unexpected archive member",
                    member=name,
                )
            if name in written:
                # A duplicate member would let the second copy overwrite the
                # first *after* any check that looked at the first.
                raise ArtifactIntegrityError(
                    artifact="runtime archive",
                    problem="duplicate archive member",
                    member=name,
                )
            if info.size > MAX_MEMBER_BYTES:
                raise ArtifactIntegrityError(
                    artifact="runtime archive",
                    problem="member exceeds the size ceiling",
                    member=name,
                    declared_size=info.size,
                )
            source = tar.extractfile(info)
            if source is None:  # Defensive: _safe_member_name already required a regular file
                raise ArtifactIntegrityError(
                    artifact="runtime archive",
                    problem="member is not readable as a file",
                    member=name,
                )
            total += _write_member(source, destination / name, name, total)
            written.append(name)

    missing = expected - set(written)
    if missing:
        raise ArtifactIntegrityError(
            artifact="runtime archive",
            problem="archive is missing required members",
            missing=",".join(sorted(missing)),
        )
    logger.info(
        "runtime archive extracted",
        extra={"members": len(written), "bytes": total},
    )
    return written


def _safe_member_name(info: tarfile.TarInfo, archive: Path) -> str:
    """Return the member's name, or raise if it is unsafe in any way."""
    name = info.name
    if info.issym() or info.islnk():
        # A link is a two-step traversal; a runtime artifact never needs one.
        raise ArtifactIntegrityError(
            artifact="runtime archive", problem="archive contains a link member", member=name
        )
    if not info.isfile():
        raise ArtifactIntegrityError(
            artifact="runtime archive",
            problem="archive member is not a regular file",
            member=name,
        )
    if name.startswith("/") or name.startswith("\\"):
        raise ArtifactIntegrityError(
            artifact="runtime archive", problem="absolute path in archive", member=name
        )
    if ":" in name.split("/")[0]:  # Windows drive prefix
        raise ArtifactIntegrityError(
            artifact="runtime archive", problem="drive prefix in archive path", member=name
        )
    parts = PurePosixPath(name).parts
    if ".." in parts:
        raise ArtifactIntegrityError(
            artifact="runtime archive", problem="path traversal in archive", member=name
        )
    if not parts:
        raise ArtifactIntegrityError(
            artifact="runtime archive", problem="empty archive member name", member=str(archive)
        )
    return name


def _write_member(source: object, target: Path, name: str, running_total: int) -> int:
    """Stream one member to disk, enforcing the size ceilings as bytes arrive.

    The declared size is attacker-controlled, so the limit is applied to what is
    actually written. Returns the number of bytes written.
    """
    written = 0
    read = getattr(source, "read")  # noqa: B009 - source is a typed-Any tar stream
    with target.open("wb") as handle:
        while True:
            block = read(_CHUNK_BYTES)
            if not block:
                break
            written += len(block)
            if written > MAX_MEMBER_BYTES or running_total + written > MAX_TOTAL_BYTES:
                # Close and remove the partial file before raising, so a failed
                # extraction never leaves something that looks like an artifact.
                handle.close()
                target.unlink(missing_ok=True)
                raise ArtifactIntegrityError(
                    artifact="runtime archive",
                    problem="archive exceeds the size ceiling during extraction",
                    member=name,
                )
            handle.write(block)
    return written


def replace_directory(staged: Path, destination: Path) -> None:
    """Move ``staged`` into place as ``destination``, replacing what is there.

    Extraction and verification happen in a temporary directory and only a
    fully-verified tree is moved here, so a crash or a failed verification can
    never leave a half-written artifact where the loader would find it and
    treat it as cached.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        discarded = destination.with_name(f"{destination.name}.discard")
        shutil.rmtree(discarded, ignore_errors=True)
        destination.rename(discarded)
        shutil.rmtree(discarded, ignore_errors=True)
    staged.rename(destination)


__all__ = [
    "MAX_MEMBER_BYTES",
    "MAX_TOTAL_BYTES",
    "RUNTIME_MEMBERS",
    "pack_archive",
    "replace_directory",
    "safe_extract",
    "verify_archive_digest",
]

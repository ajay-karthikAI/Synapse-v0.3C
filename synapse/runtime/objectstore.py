"""
synapse.runtime.objectstore
===========================
Fetching the archive from private object storage.

A protocol and one implementation. The protocol exists so the loader — where all
the verification logic lives — can be tested exhaustively offline against a
store backed by a local directory, with no network, no credentials and no
``boto3`` installed.

S3-compatible rather than S3
----------------------------
``endpoint_url`` is a first-class option, so the same client addresses AWS S3,
Cloudflare R2, Backblaze B2 and MinIO. The deployment this is built for uses a
private bucket with no public read, which is why there is no HTTP fallback here:
an unsigned GET is not a degraded mode, it is a different security posture.

Credentials
-----------
Never passed in, never held, never logged. ``boto3`` reads
``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` (and the session token, if
any) from the environment through its own credential chain. This module does not
touch them, so it cannot leak them into a log line, a traceback or a readiness
response.

``boto3`` is imported lazily and is an optional dependency. The ``synapse``
package must stay installable — and its whole test suite runnable — without it.
"""

from __future__ import annotations  # Postponed annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from synapse.errors import ArtifactNotFoundError
from synapse.logging import get_logger

logger = get_logger(__name__)


class ObjectStore(Protocol):
    """Somewhere an archive can be fetched from by bucket and key."""

    def download(self, bucket: str, key: str, destination: Path) -> None:
        """Fetch ``key`` from ``bucket`` and write it to ``destination``.

        Raises:
            ArtifactNotFoundError: the object does not exist.
        """
        ...


@dataclass(frozen=True)
class S3ObjectStore:
    """The production store: any S3-compatible endpoint, via ``boto3``."""

    endpoint_url: str | None = None
    region: str | None = None

    def download(self, bucket: str, key: str, destination: Path) -> None:
        """Fetch the object, streaming it to ``destination``.

        Raises:
            ArtifactNotFoundError: the object is absent, or the credentials do
                not authorise reading it. The two are deliberately not
                distinguished in the message: telling an unauthenticated caller
                which keys exist is an enumeration oracle, and the operator has
                the provider's own logs for the difference.
        """
        client = self._client()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            client.download_file(bucket, key, str(destination))
        except Exception as exc:
            # The provider's message can carry a request id, an endpoint host
            # and occasionally a presigned fragment. Only the exception type
            # crosses this boundary.
            destination.unlink(missing_ok=True)
            raise ArtifactNotFoundError(
                artifact="runtime archive", key=key, error=type(exc).__name__
            ) from exc
        logger.info(
            "runtime archive downloaded",
            extra={"size_bytes": destination.stat().st_size if destination.is_file() else 0},
        )

    def _client(self) -> Any:
        """Build the S3 client. Imported lazily; credentials come from the environment."""
        import boto3  # Optional dependency: see the module docstring

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
        )


@dataclass(frozen=True)
class LocalDirectoryStore:
    """A store backed by a directory on disk.

    Not a test double — it is the mechanism for running the deployed code path
    locally against an archive built by ``python -m synapse.cli.package_runtime``,
    with no bucket and no credentials. Every verification step downstream runs
    exactly as it does in the container, which is what makes a local run
    evidence about the deployed one.
    """

    root: Path

    def download(self, bucket: str, key: str, destination: Path) -> None:
        """Copy ``root/key`` into place. ``bucket`` is accepted and ignored."""
        source = self.root / key
        if not source.is_file():
            raise ArtifactNotFoundError(artifact="runtime archive", key=key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


__all__ = ["LocalDirectoryStore", "ObjectStore", "S3ObjectStore"]

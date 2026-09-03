"""
synapse.runtime.config
======================
Where the deployed runtime gets its artifact from, and which one.

Four values decide what the container serves: a **version**, an **archive
digest**, a **bucket** and a **key**. The version names the build; the digest
pins its exact bytes. Both are required, and the digest is what makes the
version meaningful — a version label alone identifies an artifact by a name the
storage account's owner can repoint at any time.

Credentials are deliberately absent
-----------------------------------
This object holds no access key, no secret and no session token. The S3 client
reads those from the process environment itself, so they never enter a dataclass
that could be logged, repr'd into a traceback, or serialised into a readiness
response. ``docs/privacy-logging-policy.md`` forbids logging credentials; the
cheapest way to keep that true is never to hold them.

The cache
---------
Verified artifacts are cached under ``/var/data/synapse`` — a Render persistent
disk in the deployed configuration. A cached artifact is reused **only** when it
matches both the configured version and the configured digest, so repointing
either value invalidates the cache by construction rather than by a separate
eviction step.
"""

from __future__ import annotations  # Postponed annotations

import os
from dataclasses import dataclass
from pathlib import Path

from synapse.errors import SynapseArtifactError
from synapse.hashing import is_sha256_hex

# The persistent disk the deployed container mounts. Overridable for local runs
# and for tests, which never touch a real path.
DEFAULT_CACHE_ROOT = Path("/var/data/synapse")

# Environment variables, named together so the deployment documentation and the
# reader can be checked against one list.
ENV_VERSION = "SYNAPSE_ARTIFACT_VERSION"
ENV_SHA256 = "SYNAPSE_ARTIFACT_SHA256"
ENV_BUCKET = "SYNAPSE_ARTIFACT_BUCKET"
ENV_KEY = "SYNAPSE_ARTIFACT_KEY"
ENV_ENDPOINT = "SYNAPSE_ARTIFACT_ENDPOINT_URL"
ENV_REGION = "SYNAPSE_ARTIFACT_REGION"
ENV_CACHE_ROOT = "SYNAPSE_ARTIFACT_CACHE"


class ArtifactConfigurationError(SynapseArtifactError):
    """The deployment does not say which artifact to serve, or says it badly.

    Distinct from an integrity failure: nothing is wrong with any artifact, the
    deployment simply has not named one. The readiness layer reports it as
    ``configuration_error`` rather than ``index_unverified``, because the
    operator response is different — set a variable, not investigate a
    corrupted build.
    """

    reason = "runtime artifact configuration is incomplete"


@dataclass(frozen=True)
class RuntimeArtifactConfig:
    """Which artifact this process serves, and where its cache lives."""

    version: str
    archive_sha256: str
    bucket: str
    key: str
    endpoint_url: str | None = None  # Set for R2, MinIO, Backblaze; None for AWS S3
    region: str | None = None
    cache_root: Path = DEFAULT_CACHE_ROOT

    @classmethod
    def from_environment(cls) -> RuntimeArtifactConfig:
        """Read the configuration from the process environment.

        Returns a config whose fields may be empty; call :meth:`validate` to
        turn an incomplete configuration into a typed error. Splitting the two
        lets a caller report *which* value is missing rather than failing at
        construction.
        """
        version = os.getenv(ENV_VERSION, "").strip()
        cache_root = os.getenv(ENV_CACHE_ROOT, "").strip()
        return cls(
            version=version,
            archive_sha256=os.getenv(ENV_SHA256, "").strip().lower(),
            bucket=os.getenv(ENV_BUCKET, "").strip(),
            # Defaulted from the version so the common case configures three
            # variables rather than four, and so the key cannot drift from the
            # version it is supposed to name.
            key=os.getenv(ENV_KEY, "").strip() or default_key(version),
            endpoint_url=os.getenv(ENV_ENDPOINT, "").strip() or None,
            region=os.getenv(ENV_REGION, "").strip() or None,
            cache_root=Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT,
        )

    @property
    def configured(self) -> bool:
        """True when every required value is present and well-formed."""
        try:
            self.validate()
        except ArtifactConfigurationError:
            return False
        return True

    def validate(self) -> None:
        """Raise if the deployment has not named a servable artifact.

        Raises:
            ArtifactConfigurationError: a required value is missing, or the
                digest is not 64 lowercase hex characters.
        """
        missing = [
            name
            for name, value in (
                (ENV_VERSION, self.version),
                (ENV_SHA256, self.archive_sha256),
                (ENV_BUCKET, self.bucket),
                (ENV_KEY, self.key),
            )
            if not value
        ]
        if missing:
            raise ArtifactConfigurationError(missing=",".join(missing))
        if not is_sha256_hex(self.archive_sha256):
            # Reported without the value: a malformed digest is still operator
            # input, and echoing configuration into a log is how secrets get
            # there when a variable is set to the wrong thing.
            raise ArtifactConfigurationError(
                variable=ENV_SHA256, problem="not 64 lowercase hexadecimal characters"
            )

    @property
    def artifact_dir(self) -> Path:
        """Where a verified artifact for this version is cached.

        Keyed by version *and* digest, so two builds sharing a version label —
        which should not happen, but is exactly the case a cache must survive —
        never collide, and repointing the digest cannot silently reuse the old
        bytes.
        """
        return self.cache_root / f"{self.version}-{self.archive_sha256[:12]}"

    @property
    def download_dir(self) -> Path:
        """Scratch space for downloads and staging, beside the cache."""
        return self.cache_root / ".staging"

    def describe(self) -> dict[str, str]:
        """Structural fields safe to log or return from a readiness probe.

        No credential, no endpoint host and no full digest: enough to identify
        the build being served, nothing that helps reach the bucket.
        """
        return {
            "artifact_version": self.version,
            "archive_sha256": self.archive_sha256[:12],
            "cache_root": str(self.cache_root),
        }


def default_key(version: str) -> str:
    """The conventional object key for a version, e.g. ``synapse-runtime-3.tar.gz``."""
    return f"synapse-runtime-{version}.tar.gz" if version else ""


__all__ = [
    "DEFAULT_CACHE_ROOT",
    "ENV_BUCKET",
    "ENV_CACHE_ROOT",
    "ENV_ENDPOINT",
    "ENV_KEY",
    "ENV_REGION",
    "ENV_SHA256",
    "ENV_VERSION",
    "ArtifactConfigurationError",
    "RuntimeArtifactConfig",
    "default_key",
]

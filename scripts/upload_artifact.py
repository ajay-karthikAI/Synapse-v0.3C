#!/usr/bin/env python3
"""
Upload a packed runtime artifact to any S3-compatible bucket.

Exists because `aws s3 cp` needs the AWS CLI, which is a separate install for
one 12 MB upload. This needs only `boto3`, which the project already depends on
for the container's artifact download:

    ./.venv/bin/python -m pip install boto3
    ./.venv/bin/python scripts/upload_artifact.py dist/synapse-runtime-0.3.0.tar.gz

Credentials come from the environment, never from an argument -- a secret on a
command line is visible in `ps` and lands in your shell history:

    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export SYNAPSE_ARTIFACT_BUCKET=synapse-artifacts
    export SYNAPSE_ARTIFACT_S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com

For AWS S3 itself, leave the endpoint unset and set SYNAPSE_ARTIFACT_REGION.

It prints the digest of what it uploaded and the three values the deployment
needs. The digest is computed from the file on disk AFTER the upload completes,
so what you pin is what you sent.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def digest(path: Path) -> str:
    """SHA-256 of the archive, read in chunks so a large file is not buffered."""
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    archive = Path(sys.argv[1])
    if not archive.is_file():
        print(f"error: {archive} does not exist", file=sys.stderr)
        return 1

    bucket = os.getenv("SYNAPSE_ARTIFACT_BUCKET", "").strip()
    if not bucket:
        print("error: SYNAPSE_ARTIFACT_BUCKET is not set", file=sys.stderr)
        return 1

    # Default the key to `runtime/<filename>`, matching the runbook's layout.
    key = os.getenv("SYNAPSE_ARTIFACT_KEY", "").strip() or f"runtime/{archive.name}"
    endpoint = os.getenv("SYNAPSE_ARTIFACT_S3_ENDPOINT", "").strip() or None
    region = os.getenv("SYNAPSE_ARTIFACT_REGION", "").strip() or None

    try:
        import boto3
    except ImportError:
        print(
            "error: boto3 is not installed. Run:\n  ./.venv/bin/python -m pip install boto3",
            file=sys.stderr,
        )
        return 1

    client = boto3.client("s3", endpoint_url=endpoint, region_name=region)

    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"Uploading {archive.name} ({size_mb:.1f} MB) to s3://{bucket}/{key}")
    try:
        client.upload_file(str(archive), bucket, key)
    # Broad on purpose: the provider's exception hierarchy is wide, and the
    # operator response to every one of them is the same -- check the bucket,
    # the endpoint and the credential. The type name is printed; the message is
    # not, because it can carry a presigned fragment.
    except Exception as exc:
        print(f"error: upload failed ({type(exc).__name__})", file=sys.stderr)
        print("  check the bucket name, the endpoint, and that the key can write", file=sys.stderr)
        return 1

    print("\nUploaded. Set these three in Render:\n")
    print(f"  SYNAPSE_ARTIFACT_BUCKET={bucket}")
    print(f"  SYNAPSE_ARTIFACT_KEY={key}")
    print(f"  SYNAPSE_ARTIFACT_SHA256={digest(archive)}")
    if endpoint:
        print(f"  SYNAPSE_ARTIFACT_S3_ENDPOINT={endpoint}")
    print("\nPlus SYNAPSE_ARTIFACT_VERSION (any label; it is not verified).")
    print("Update the key and the digest TOGETHER, or startup fails closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

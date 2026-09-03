"""
Package a verified artifact directory into a runtime archive.

    python -m synapse.cli.package_runtime \\
        --artifact-dir artifacts/runtime/corpus-v2 \\
        --output dist/synapse-runtime-2.tar.gz

The step between ``migrate_artifacts`` (which turns the legacy pickle into
manifest-governed JSONL) and the deployed container (which downloads one
archive). It does three things and refuses to skip any of them:

1. **Verify before packing.** The directory goes through the same deep
   verification the container runs on the way in — manifest self-digest, every
   file digest, the corpus digest, and the ordering digest that binds FAISS row
   *i* to corpus record *i*. An archive is never built from an artifact that
   would fail on load, because discovering that in production means a container
   that will not start and an operator with no idea why.

2. **Pack deterministically.** Timestamps, ownership and the gzip header are
   zeroed, so repacking identical content reproduces the same bytes and
   therefore the same SHA-256. That is what makes the digest a value an operator
   can pin, compare and reason about rather than a number that changes every
   time someone rebuilds.

3. **Print the configuration.** The digest is only useful if it reaches the
   deployment, so the command ends by printing the exact environment variables
   to set. Copying them is the last manual step; getting them wrong fails
   closed.

This command is offline and touches no credentials. Uploading the archive is a
separate act, deliberately: the thing that can write to the bucket should not
also be the thing that reads a corpus.
"""

from __future__ import annotations  # Postponed annotations

import argparse
from pathlib import Path

from synapse.errors import SynapseArtifactError
from synapse.logging import configure_logging
from synapse.runtime.archive import RUNTIME_MEMBERS, pack_archive
from synapse.runtime.config import ENV_KEY, ENV_SHA256, ENV_VERSION, default_key
from synapse.runtime.loader import verify_directory


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m synapse.cli.package_runtime",
        description="Verify an artifact directory and pack it into a runtime archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The archive is byte-reproducible: repacking identical content yields "
            "the same SHA-256. See docs/runtime-artifacts.md."
        ),
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        required=True,
        help="Directory produced by migrate_artifacts, containing manifest.json.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Path of the .tar.gz to write.")
    parser.add_argument(
        "--artifact-version",
        default=None,
        help="Version label for this archive. Defaults to the manifest's index_version.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite the output archive if it exists."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Verify, pack, and print the deployment configuration."""
    configure_logging()
    args = build_parser().parse_args(argv)

    if args.output.exists() and not args.force:
        print(f"REFUSED: {args.output} already exists. Pass --force to overwrite.")
        return 2

    try:
        manifest, report = verify_directory(args.artifact_dir)
    except SynapseArtifactError as exc:
        # The sanitised message: structural facts only, never file contents.
        print(f"REFUSED: {args.artifact_dir} did not verify.\n  {exc.safe_message}")
        return 1

    version = args.artifact_version or manifest.index_version
    digest = pack_archive(args.artifact_dir, args.output)

    print(f"Verified  {args.artifact_dir}")
    print(f"  index_id        {manifest.index_id}")
    print(f"  corpus_version  {manifest.corpus_version}")
    print(f"  chunks          {manifest.chunk_count} (re-validated: {report.chunks_validated})")
    print(f"  vectors         {manifest.vector_count}")
    print(f"  source pack     {manifest.source_pack_version}")
    print()
    print(f"Packed    {args.output}  ({args.output.stat().st_size} bytes)")
    print(f"  members         {', '.join(RUNTIME_MEMBERS)}")
    print()
    print("Upload the archive, then set these in the deployment:")
    print(f"  {ENV_VERSION}={version}")
    print(f"  {ENV_SHA256}={digest}")
    print(f"  {ENV_KEY}={default_key(version)}")
    print()
    print(
        "The digest is the pin. A deployment that cannot match it serves nothing,\n"
        "which is the intended behaviour."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())

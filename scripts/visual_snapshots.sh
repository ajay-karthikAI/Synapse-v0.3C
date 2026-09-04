#!/usr/bin/env bash
# Run the visual snapshot suite inside the official Playwright container.
#
# Why a container
# ---------------
# Image baselines are platform-specific: Playwright names them
# `<name>-<project>-<platform>.png`, and macOS and Linux disagree about font
# rasterisation by a few pixels on almost every glyph. Baselines generated on a
# developer's Mac are therefore invisible to CI, which runs Linux -- CI would
# report "snapshot missing" for every one of them and, with --update-snapshots,
# would happily write a second set that nobody ever looked at.
#
# So there is ONE set of baselines, `-chromium-linux`, and this script is how
# they are produced on any host. The image tag is pinned to the same Playwright
# version as `frontend/package.json`; a mismatch changes rendering and would
# invalidate every baseline at once.
#
# The host's node_modules is deliberately shadowed by an anonymous volume: it
# contains darwin binaries (next-swc among them) that cannot run in the
# container, and installing Linux ones over the top would break local dev.
#
# Usage:
#   scripts/visual_snapshots.sh            # verify against committed baselines
#   scripts/visual_snapshots.sh --update   # regenerate them, then READ the diff

set -euo pipefail

PLAYWRIGHT_VERSION="1.62.1"   # keep in step with frontend/package.json
IMAGE="mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MODE="verify"
if [ "${1:-}" = "--update" ]; then
  MODE="update"
fi

echo "Playwright ${PLAYWRIGHT_VERSION} (${MODE}) in ${IMAGE}"

# Both projects. Each keeps its own baselines (`-chromium-linux` and
# `-mobile-linux`): the mobile project runs a touch-capable, mobile user-agent
# context in which the appointment brief is a modal dialog rather than a
# non-modal region, so the pair is two contracts, not two copies of one picture.
PW_ARGS="test visual"
if [ "$MODE" = "update" ]; then
  PW_ARGS="$PW_ARGS --update-snapshots"
fi

docker run --rm \
  -v "${REPO_ROOT}/frontend":/work \
  -v /work/node_modules \
  -w /work \
  -e CI=1 \
  "$IMAGE" \
  bash -c "npm ci --no-audit --no-fund && npx playwright $PW_ARGS"

if [ "$MODE" = "update" ]; then
  echo
  echo "Baselines rewritten. Review every changed image before committing:"
  echo "  git status --short frontend/tests/e2e/visual.spec.ts-snapshots/"
  echo "A snapshot diff is a change to what a patient sees."
fi

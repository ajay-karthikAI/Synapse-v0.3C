#!/usr/bin/env bash
# Validate the deployment configuration WITHOUT deploying anything.
#
# This is what can be proved on a laptop: that the blueprint parses and says
# what the runbook says, that the image builds, that it contains nothing it must
# not, and — the part most likely to be silently wrong — that a container
# configured with the *deployment's* variable names actually starts and reports
# a correct readiness state.
#
# It does NOT deploy, and it does not talk to Render or Vercel. Nothing here
# should ever be reported as "deployed" or "smoke tested in production".
#
# Usage: scripts/validate_deployment.sh

set -euo pipefail

IMAGE="synapse-api:deploy-validate"
NAME="synapse-deploy-validate-$$"
PORT="${VALIDATE_PORT:-8131}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Obvious non-secrets, set under the names Render uses.
FAKE_TOKEN="validate-service-token-00000000"
FAKE_SESSION="validate-session-secret-000000000"
FAKE_PASSWORD="validate-passcode"
FAKE_PROVIDER_KEY="sk-fake-not-a-real-key-000000000000"

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1" >&2; exit 1; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cd "$REPO"

echo "== configuration files =="
python3 -c "
import json, sys, pathlib
try:
    import yaml
except ImportError:
    sys.exit('pyyaml is required: pip install -e \".[dev]\"')
render = yaml.safe_load(pathlib.Path('render.yaml').read_text())
svc = render['services'][0]
assert len(render['services']) == 1, 'expected exactly one service'
assert svc['healthCheckPath'] == '/readyz', 'health check must be /readyz'
assert svc['disk']['mountPath'] == '/var/data/synapse', 'disk mount path'
assert '--workers 1' in svc['dockerCommand'], 'must run one worker'
json.loads(pathlib.Path('frontend/vercel.json').read_text())
print('  ok   render.yaml and frontend/vercel.json parse and agree with the runbook')
"

echo "== image build =="
docker build -q -t "$IMAGE" . >/dev/null
pass "image builds"

echo "== the image contains nothing it must not =="
# Runtime artifacts, credentials, tests, local telemetry and pickles.
FORBIDDEN="$(docker run --rm --entrypoint sh "$IMAGE" -c '
  find / -xdev \( \
       -name "*.pkl" \
    -o -name ".env" -o -name ".env.*" \
    -o -name "processed_chunks*" \
    -o -path "*/artifacts/telemetry*" \
  \) -not -path "*/site-packages/*" 2>/dev/null | head -20
')"
[ -z "$FORBIDDEN" ] || fail "image contains: $FORBIDDEN"
pass "no pickle, no .env, no runtime artifact, no local telemetry"

TESTS="$(docker run --rm --entrypoint sh "$IMAGE" -c 'ls -d /app/tests /app/evals /app/frontend 2>/dev/null || true')"
[ -z "$TESTS" ] || fail "image contains test or frontend trees: $TESTS"
pass "no tests, no evals, no frontend tree"

# The artifact cache must be a mount point at runtime, not baked content.
CACHE="$(docker run --rm --entrypoint sh "$IMAGE" -c 'ls -A /var/data/synapse 2>/dev/null || true')"
[ -z "$CACHE" ] || fail "/var/data/synapse is not empty in the image: $CACHE"
pass "/var/data/synapse is empty (filled by the mounted disk)"

echo "== the container starts with the DEPLOYMENT variable names =="
# This is the assertion that matters. Render sets DEMO_ACCESS_PASSWORD and
# DEMO_SESSION_SECRET; the code reads SYNAPSE_ACCESS_PASSCODE and
# SYNAPSE_JWT_SECRET. If the aliases did not resolve, the failure would be
# silent and misleading -- a missing session secret reads as "your session has
# expired", forever, rather than as "unconfigured".
cleanup
docker run -d --name "$NAME" -p "${PORT}:8000" \
  -e SYNAPSE_SERVICE_TOKEN="$FAKE_TOKEN" \
  -e DEMO_SESSION_SECRET="$FAKE_SESSION" \
  -e DEMO_ACCESS_PASSWORD="$FAKE_PASSWORD" \
  -e OPENAI_API_KEY="$FAKE_PROVIDER_KEY" \
  -e SYNAPSE_ENVIRONMENT=demo \
  -e SYNAPSE_ARTIFACT_CACHE=/var/data/synapse \
  "$IMAGE" >/dev/null

tries=0
until curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -gt 45 ]; then
    docker logs "$NAME" >&2 2>&1 || true
    fail "container never became live under the deployment variable names"
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)" != "true" ]; then
    docker logs "$NAME" >&2 2>&1 || true
    fail "container exited; the DEMO_* aliases did not satisfy ApiSettings.validate"
  fi
  sleep 1
done
pass "container is live with DEMO_ACCESS_PASSWORD / DEMO_SESSION_SECRET only"

BODY="$(curl -s "http://127.0.0.1:${PORT}/readyz")"
# No artifact is mounted here, so unready with a typed code is CORRECT.
echo "$BODY" | grep -q '"ready":false' \
  || fail "readiness claimed ready with no artifact mounted: $BODY"
echo "$BODY" | grep -qE '"failure_code":"(index_unverified|configuration_error)"' \
  || fail "readiness carried no typed failure code: $BODY"
pass "/readyz reports unready with a typed code (no artifact is mounted here)"

echo "$BODY" | grep -qE '(sk-|validate-|/opt/|Traceback)' \
  && fail "/readyz leaked a value or a path: $BODY"
pass "/readyz leaks no value, path or message"

echo
echo "Deployment configuration validated locally."
echo "NOTHING WAS DEPLOYED. See docs/deployment.md §6 for the operator commands."

#!/usr/bin/env bash
# Container smoke test: health, readiness, and what the image must not contain.
#
# Runs against a built image with a FAKE provider credential. Nothing here
# contacts a model, and the values below are obvious non-secrets so that a copy
# of this script in a log is not a leak.
#
# What it proves
# --------------
#   1. The image runs as a non-root user.
#   2. It does NOT contain Streamlit, a .env, or the legacy pickle corpus.
#   3. With a fake provider it starts, stays up, and serves /healthz.
#   4. With NO provider credential it *still* starts and stays up, and reports
#      itself unready with a typed failure code -- the degrade `serve_api.py`
#      documents. A container that crash-loops tells an operator far less than
#      one whose health check names the failure, so this is the case most worth
#      asserting.
#   5. Neither endpoint leaks a credential, a path or a bucket name.
#
# Usage: scripts/smoke_container.sh [image-tag]

set -euo pipefail

IMAGE="${1:-synapse-api:smoke}"
NAME="synapse-smoke-$$"
PORT="${SMOKE_PORT:-8127}"

# Obvious non-secrets. The API only requires these to be present and non-empty.
FAKE_TOKEN="smoke-service-token-000000000000"
FAKE_SECRET="smoke-jwt-secret-0000000000000000"
FAKE_PASSCODE="smoke-passcode"
FAKE_PROVIDER_KEY="sk-fake-not-a-real-key-000000000000"

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1" >&2; exit 1; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

wait_for_health() {
  local tries=0
  until curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -gt 60 ] && return 1
    # A container that died is a failure now, not in 60 seconds.
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)" != "true" ]; then
      echo "--- container exited; logs follow ---" >&2
      docker logs "$NAME" >&2 2>&1 || true
      return 1
    fi
    sleep 1
  done
  return 0
}

echo "== image hygiene =="

USER_ID="$(docker run --rm --entrypoint python "$IMAGE" -c 'import os; print(os.getuid())')"
[ "$USER_ID" != "0" ] || fail "image runs as root (uid 0)"
pass "runs as non-root (uid $USER_ID)"

if docker run --rm --entrypoint python "$IMAGE" -c 'import streamlit' 2>/dev/null; then
  fail "streamlit is installed in the API image"
fi
pass "streamlit is absent"

if docker run --rm --entrypoint python "$IMAGE" -c 'import fastapi, synapse.api' 2>/dev/null; then
  pass "fastapi and synapse.api import"
else
  fail "the API layer does not import inside the image"
fi

LEAKED="$(docker run --rm --entrypoint sh "$IMAGE" -c 'ls -A /app 2>/dev/null | grep -E "^\.env|\.pkl$|^app\.py$" || true')"
[ -z "$LEAKED" ] || fail "image contains files it must not: $LEAKED"
pass "no .env, no *.pkl, no app.py in the image"

echo "== degraded start: SYNAPSE_* present, NO provider credential =="

cleanup
docker run -d --name "$NAME" -p "${PORT}:8000" \
  -e SYNAPSE_SERVICE_TOKEN="$FAKE_TOKEN" \
  -e SYNAPSE_JWT_SECRET="$FAKE_SECRET" \
  -e SYNAPSE_ACCESS_PASSCODE="$FAKE_PASSCODE" \
  "$IMAGE" >/dev/null

wait_for_health || fail "container did not become live without a provider credential"
pass "container is live"

HEALTH="$(curl -fsS "http://127.0.0.1:${PORT}/healthz")"
echo "$HEALTH" | grep -q '"version"' || fail "/healthz has no version: $HEALTH"
pass "/healthz 200 with a version"

READY_CODE="$(curl -s -o /tmp/smoke_readyz.json -w '%{http_code}' "http://127.0.0.1:${PORT}/readyz")"
READY_BODY="$(cat /tmp/smoke_readyz.json)"
[ "$READY_CODE" = "503" ] || fail "/readyz should be 503 without a provider credential, got $READY_CODE: $READY_BODY"
echo "$READY_BODY" | grep -q '"ready":false' || fail "/readyz did not report ready:false: $READY_BODY"
echo "$READY_BODY" | grep -q '"failure_code":"configuration_error"' \
  || fail "/readyz did not carry the typed configuration_error code: $READY_BODY"
pass "/readyz 503, ready:false, failure_code=configuration_error"

# The detail field is an exception TYPE NAME by contract. Anything resembling a
# message, a path or a key here is a disclosure on an unauthenticated endpoint.
echo "$READY_BODY" | grep -qE '(sk-|/app/|/opt/|Traceback|Error:)' \
  && fail "/readyz leaked a path, key or message: $READY_BODY"
pass "/readyz leaks no path, key or message"

sleep 3
[ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = "true" ] \
  || fail "container exited instead of staying up unready"
pass "stayed up rather than crash-looping"

echo "== fake-provider start: every credential present =="

cleanup
docker run -d --name "$NAME" -p "${PORT}:8000" \
  -e SYNAPSE_SERVICE_TOKEN="$FAKE_TOKEN" \
  -e SYNAPSE_JWT_SECRET="$FAKE_SECRET" \
  -e SYNAPSE_ACCESS_PASSCODE="$FAKE_PASSCODE" \
  -e OPENAI_API_KEY="$FAKE_PROVIDER_KEY" \
  "$IMAGE" >/dev/null

wait_for_health || fail "container did not become live with a fake provider"
pass "container is live"

READY_CODE="$(curl -s -o /tmp/smoke_readyz.json -w '%{http_code}' "http://127.0.0.1:${PORT}/readyz")"
READY_BODY="$(cat /tmp/smoke_readyz.json)"
echo "$READY_BODY" | grep -qE '"state":"(ready|degraded|failed)"' \
  || fail "/readyz reported no recognised state: $READY_BODY"
pass "/readyz $READY_CODE with a recognised state"

# The image ships no index (`*.pkl` and `hybrid_index/` are excluded, correctly),
# so this container CANNOT answer a question and must not claim it can.
# `legacy_index_provider` is lazy, so constructing the service succeeds either
# way; readiness therefore checks the artifact inputs itself. Before that check
# existed this returned ready:true, and a load balancer would have sent traffic
# to a process with nothing to retrieve from.
[ "$READY_CODE" = "503" ] || fail "/readyz claimed readiness with no index: $READY_CODE $READY_BODY"
echo "$READY_BODY" | grep -q '"failure_code":"index_unverified"' \
  || fail "/readyz did not name the missing index: $READY_BODY"
pass "/readyz 503 index_unverified — no index, so not ready"

# Field names only: a path here would disclose the container's layout on an
# unauthenticated endpoint.
echo "$READY_BODY" | grep -qE '(sk-|/app/|/opt/|Traceback|\.pkl)' \
  && fail "/readyz leaked a path, key or message: $READY_BODY"
pass "/readyz leaks no path, key or message"

# The service must be BUILDABLE in the image, not merely importable. This is the
# assertion that catches a data file which exists in the repository but was never
# copied in -- the emergency vocabulary being the one that matters, since without
# it the detector raises and no turn can be served at all.
[ "$(echo "$READY_BODY" | grep -c 'VocabularyError')" = "0" ] \
  || fail "the emergency vocabulary is missing from the image: $READY_BODY"
pass "the emergency vocabulary loads inside the image"

# An unauthenticated turn must be refused. The smoke test never sends a real
# question; it only proves the door is shut. The path is asserted to EXIST
# first, because a 404 from a mistyped route would otherwise read as "refused".
TURN_PATH="/v1/turns/stream"
SPEC_HAS_PATH="$(curl -fsS "http://127.0.0.1:${PORT}/openapi.json" | grep -c "\"${TURN_PATH}\"" || true)"
[ "$SPEC_HAS_PATH" != "0" ] || fail "${TURN_PATH} is not a route; the refusal check would be meaningless"

TURN_CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H 'content-type: application/json' \
  -d '{"query":"smoke","client_request_id":"00000000-0000-4000-8000-000000000000"}' \
  "http://127.0.0.1:${PORT}${TURN_PATH}")"
case "$TURN_CODE" in
  401|403) pass "unauthenticated turn refused ($TURN_CODE)" ;;
  *) fail "unauthenticated turn returned $TURN_CODE, expected 401/403" ;;
esac

echo
echo "container smoke test passed"

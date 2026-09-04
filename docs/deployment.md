# Deployment

The deployed system is **two artifacts**: a Next.js interface (Vercel) and one
FastAPI container (Render). The browser only ever talks to the Next.js
same-origin proxy; it never reaches the API directly, and `OPENAI_API_KEY` is
server-side in both hops.

Streamlit is not deployed. It is a local fallback over the same
`synapse.service`, and its dependencies are a separate extra
(`.[streamlit]`) precisely so the image cannot acquire a second web framework.
`tests/test_streamlit_fallback.py` asserts those extras stay disjoint.

---

## 1. The image

```bash
docker build -t synapse-api .
./scripts/smoke_container.sh synapse-api
```

Two stages, so the compiler toolchain used to install dependencies is not
present in what ships. It installs `.[api,runtime]`, runs as uid 10001, and its
`HEALTHCHECK` probes `/healthz`.

### What must be in it, and what must never be

| | Why |
|---|---|
| `synapse/`, `serve_api.py`, `legacy_index.py` | The application |
| `synapse/safety/emergency_vocabulary.toml` | Package data, so it arrives with `synapse/`. See §2 |
| No `.env`, no `*.pkl`, no `app.py`, no `frontend/` | `.dockerignore`. A layer is readable by anyone who can pull the image, and deleting a file in a later `RUN` does not remove it from the layer that added it |
| No Streamlit | The container is the API only |

`tests/test_container_contract.py` checks these offline, on every push, so a
regression shows up in the fast suite rather than only in the job that can build
an image.

---

## 2. The emergency vocabulary ships inside the package

It lives at `synapse/safety/emergency_vocabulary.toml` and is declared as
package data, so it travels with the code however the code was installed —
source checkout, wheel, or container.

It did not always. `DEFAULT_VOCABULARY_PATH` used to resolve **relative to the
repository root**, at `config/emergency_vocabulary.toml`, two directories above
the module. That works in a checkout and nowhere else: from an installed wheel
the same expression points at `site-packages/config/`, which does not exist. The
first image built from this package started, passed its liveness check, and
reported itself permanently unready with `detail="VocabularyError"`. Failing
closed was correct — the emergency check precedes retrieval and generation, so a
detector that cannot load must stop the turn rather than skip it — but the
container could not answer anything at all.

Three tests hold the property now: the resolved path is inside the package, the
file is present in a freshly built wheel (`pip wheel` is actually run), and the
detector loads. The container smoke test asserts it end to end.

### Overriding it

`SYNAPSE_EMERGENCY_VOCABULARY=/path/to/reviewed.toml` takes precedence, so an
operator can mount a clinically reviewed vocabulary without rebuilding the
image. A `config/emergency_vocabulary.toml` in the repository root is still
honoured if present, so an existing local override was not silently ignored by
the move.

---

## 3. Health and readiness answer different questions

| | Question | On failure |
|---|---|---|
| `/healthz` | Is the process alive? | Platform restarts it |
| `/readyz` | Can it serve? | Load balancer withholds traffic; body carries a typed code |

The container health check probes `/healthz` **only**. Wiring it to `/readyz`
would turn a diagnosable unready container into a crash loop, replacing a
container that explains itself with one that merely restarts.

A missing `OPENAI_API_KEY` is a degraded mode: the process stays up and reports
`ready:false` with `failure_code="configuration_error"`. A missing
`SYNAPSE_SERVICE_TOKEN` or `SYNAPSE_JWT_SECRET` is not — those refuse to start,
because an open door is not a degraded mode.

`detail` is an exception **type name**, never a message: readiness is
unauthenticated, and a provider error can carry a key or a URL.

### Readiness checks the artifact, not just the constructor

`legacy_index_provider` is fully lazy — every import inside it is
function-local, deliberately, so that importing it costs no FAISS load. The
consequence was that `SynapseService.from_config` succeeded whether or not an
index existed, and `/readyz` answered `ready:true` for a container that could
not answer a single question. A load balancer would have sent it traffic.

`serve_api._missing_index_inputs` now checks the configured retrieval inputs
before reporting ready. If `chunks_path` or `index_dir` is absent, readiness is
`failed` with `failure_code="index_unverified"` and
`detail="missing:chunks_path,index_dir"` — **field names, never paths**, because
`/readyz` is unauthenticated and a path discloses the container's layout. The
service object is discarded rather than held, so a turn cannot slip through if
readiness is ever bypassed.

This is a presence check, not a load. It is instant and it catches the case that
actually happens — an image or host with no artifact. It does **not** prove the
index is intact; the index gate does that on the first turn and produces a typed
`index_unverified` outcome if verification fails.

---

## 4. Two index shapes, chosen by configuration

`serve_api.py` serves from either, and the switch is the presence of
`SYNAPSE_ARTIFACT_BUCKET`:

| | When | What loads |
|---|---|---|
| **Verified artifact** | `SYNAPSE_ARTIFACT_BUCKET` set | `RuntimeIndex` → `RuntimeIndexProvider`. Digest checked, archive extracted without `extractall`, sparse index rebuilt from the verified corpus, dense index refused unless its vector count and dimensionality match. |
| **Legacy prototype** | otherwise (the default) | `processed_chunks.pkl` + `hybrid_index/`, the same index `app.py` uses — which is what keeps the parity claim in `docs/migration-parity.md` checkable. |

The switch is explicit rather than inferred from what happens to be on disk: a
process must never silently fall back to the prototype's pickle because a
download failed. Both shapes converge on the same pair of retrieval protocols
in `RetrievalService._backends`, so the deployed artifact and the local
prototype cannot answer the same question differently.

The legacy path is the default, so every local checkout and every existing test
behaves exactly as it did before the adapter existed.

### The query embedder is bound to the manifest

A query embedded with a different model from the corpus, or with different
dimensionality, or left un-normalised when the corpus was normalised, does not
error — it returns neighbours. They are simply the wrong ones, ranked
confidently. So `OpenAIQueryEmbedder` is constructed **unbound**, passed to
`RuntimeIndex`, and bound to `manifest.embedding` only after startup has
verified it. Calling it before then raises; `NativeDenseBackend.load` stores the
callable without invoking it, so that window is closed by construction.

A provider the embedder cannot reproduce (anything but `openai`) is refused at
bind time rather than attempted.

### Required artifact variables

`RuntimeArtifactConfig.validate` requires **five**: `SYNAPSE_ARTIFACT_VERSION`,
`_SHA256`, `_BUCKET`, `_KEY` (defaulted from the version) and, for a non-AWS
store, `SYNAPSE_ARTIFACT_S3_ENDPOINT`. An earlier draft of `render.yaml`
declared four; the container started, reported `configuration_error`, and could
never have loaded anything.
`tests/test_deployment_config.py` now derives that list from the validator
itself rather than from a hand-maintained copy.

### The cache directory must be writable by the non-root user

The image creates `/var/data/synapse` owned by uid 10001 and declares it a
volume. Without that, the process runs as `synapse` against a root-owned mount
and startup fails with `index_unverified` / `PermissionError` — an artifact it
could have downloaded, refused because it had nowhere to put it.

---

## 5. Secrets

Nothing is baked into a layer: no `ARG`, no `ENV`, no `COPY` carries a
credential, and `tests/test_container_contract.py` asserts it. Everything is
read from the environment at request time.

The local development passcode is `synapse` and the tokens are `local-demo-*`.
Those are fine on localhost and an open door on a public URL. Generate real ones
before any deployment reachable from the internet.

### The environment variables, and where each one is set

Two names differ between the platform and the code, because the hosting
configuration was specified with `DEMO_*` names. Both are accepted; the
`SYNAPSE_*` name wins when both are set, so nothing local changes.
`tests/test_deployment_config.py` asserts each alias actually resolves — a
mismatch here fails *misleadingly*: a missing session secret does not report
"unconfigured", it reports "your session has expired", on every request forever.

| Variable | Set in | Secret | Read by |
|---|---|---|---|
| `OPENAI_API_KEY` | Render | yes | `ServiceConfig` |
| `SYNAPSE_SERVICE_TOKEN` | Render **and** Vercel | yes | Both sides; must match exactly |
| `DEMO_ACCESS_PASSWORD` | Render | yes | `SYNAPSE_ACCESS_PASSCODE` alias |
| `DEMO_SESSION_SECRET` | Render **and** Vercel | yes | `SYNAPSE_JWT_SECRET` alias; must match exactly |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Render | yes | boto3, for the artifact bucket |
| `SYNAPSE_ARTIFACT_S3_ENDPOINT` | Render | no | `SYNAPSE_ARTIFACT_ENDPOINT_URL` alias |
| `SYNAPSE_ARTIFACT_BUCKET` / `_KEY` / `_SHA256` | Render | no | `RuntimeArtifactConfig` |
| `SYNAPSE_ARTIFACT_CACHE` | `render.yaml` | no | `/var/data/synapse`, the mounted disk |
| `SYNAPSE_ENVIRONMENT=demo` | `render.yaml` | no | `SYNAPSE_ENV` alias |
| `SYNAPSE_TELEMETRY*` | `render.yaml` | no | Counters only, see §10 |
| `SYNAPSE_API_URL` | Vercel | no | The private Render URL |

**Two values must be byte-identical on both platforms**: `SYNAPSE_SERVICE_TOKEN`
and `DEMO_SESSION_SECRET`. If the token differs, every proxied request is
rejected by the backend. If the session secret differs, every signed cookie
fails verification and the interface reports an expired session forever.

Nothing is `NEXT_PUBLIC_`. That prefix inlines a value into the browser bundle
at build time, so a service token there is a public backend;
`tests/test_deployment_config.py` scans the whole of `frontend/src` for it.

---

## 6. Runbook: first deployment

Preview/staging first, production only after §9 passes. Every command below is
exact; nothing is a placeholder except values in `<angle brackets>`.

### 6.1 Generate the secrets

```bash
# 32-byte URL-safe values. Run once; store in your password manager.
python -c "import secrets; print('SYNAPSE_SERVICE_TOKEN =', secrets.token_urlsafe(32))"
python -c "import secrets; print('DEMO_SESSION_SECRET   =', secrets.token_urlsafe(32))"
python -c "import secrets; print('DEMO_ACCESS_PASSWORD  =', secrets.token_urlsafe(12))"
```

Never paste these into a file in this repository, a commit message, a terminal
that is being recorded, or a chat window.

### 6.2 Publish the runtime artifact

See §7. The service will not become ready without one.

### 6.3 Create the Render service

```bash
# In the Render dashboard: New > Blueprint, point it at this repository.
# render.yaml is read from the repository root and creates ONE web service.
# Render then prompts for every `sync: false` variable.
```

Set, when prompted: `OPENAI_API_KEY`, `SYNAPSE_SERVICE_TOKEN`,
`DEMO_ACCESS_PASSWORD`, `DEMO_SESSION_SECRET`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `SYNAPSE_ARTIFACT_S3_ENDPOINT`,
`SYNAPSE_ARTIFACT_BUCKET`, `SYNAPSE_ARTIFACT_KEY`, `SYNAPSE_ARTIFACT_SHA256`.

Then confirm readiness — **do not proceed until this returns `ready:true`**:

```bash
curl -sS https://<render-service>.onrender.com/readyz | tee /dev/stderr | grep -q '"ready":true'
```

A `503` with `"failure_code":"index_unverified"` means the artifact is missing or
its digest did not match. A `503` with `"configuration_error"` means a secret is
unset; the `detail` names which variable, never its value.

### 6.4 Create the Vercel project

```bash
# Vercel dashboard: New Project > import this repository.
#   Root Directory:  frontend        <- REQUIRED. vercel.json is read from here.
#   Framework:       Next.js (auto-detected)
```

Set three variables, **Preview and Production separately**, all unchecked for
"Automatically expose to the browser":

| | Preview | Production |
|---|---|---|
| `SYNAPSE_API_URL` | staging Render URL | production Render URL |
| `SYNAPSE_SERVICE_TOKEN` | staging value | production value |
| `DEMO_SESSION_SECRET` | staging value | production value |

Separate values per environment mean a leaked preview secret cannot open
production, and a preview build cannot write to the production backend's
sessions.

### 6.5 Deploy preview, verify, then promote

```bash
vercel deploy                      # preview URL
# run §9 against the preview URL, in full
vercel promote <deployment-url>    # only after every gate passes
```

---

## 7. Runbook: publishing a runtime artifact

The artifact is a verified, read-only archive. The container downloads it once,
checks its digest, and refuses to serve if it does not match.

```bash
# 0. If starting from the legacy pickle corpus, migrate it once into verified
#    JSONL artifacts. One-way, and the only step that reads a .pkl.
python -m synapse.cli.migrate_artifacts \
  --input processed_chunks.pkl \
  --output-dir dist/artifact \
  --corpus-version <version> \
  --faiss-index hybrid_index

# 1. Verify that directory and pack it. Packing is byte-reproducible: repacking
#    identical content yields the same SHA-256, which is what makes the pinned
#    digest meaningful rather than a moving target.
python -m synapse.cli.package_runtime \
  --artifact-dir dist/artifact \
  --output dist/synapse-runtime-<version>.tar.gz

# 2. Record the digest. THIS is what the deployment pins.
shasum -a 256 dist/synapse-runtime-<version>.tar.gz

# 3. Upload. Any S3-compatible store; the endpoint is configurable.
aws s3 cp dist/synapse-runtime-<version>.tar.gz \
  "s3://<bucket>/runtime/synapse-runtime-<version>.tar.gz" \
  --endpoint-url "<SYNAPSE_ARTIFACT_S3_ENDPOINT>"

# 4. Point the service at it, then redeploy.
#    SYNAPSE_ARTIFACT_KEY    = runtime/synapse-runtime-<version>.tar.gz
#    SYNAPSE_ARTIFACT_SHA256 = <the digest from step 2>
```

Update the key and the digest **together**. A key without a matching digest
fails closed at startup — which is the correct behaviour and an avoidable
outage.

The bucket credential should be read-only. The service never writes to it.

---

## 8. Runbook: rotating a secret

Rotation order matters, because two values live on both platforms.

**`SYNAPSE_SERVICE_TOKEN` or `DEMO_SESSION_SECRET`** — these must match across
Render and Vercel, so there is a window where they do not:

1. Set the new value in **Render** first and let it deploy.
2. Set the same value in **Vercel** (Production), then redeploy the frontend.
3. Between those steps the interface returns "session expired" or is rejected by
   the backend. It is brief and it is visible; do it deliberately rather than
   discovering it.

Rotating `DEMO_SESSION_SECRET` invalidates **every** signed cookie: everyone is
signed out. That is the intended effect and the reason it is also the response
to a suspected leak.

**`DEMO_ACCESS_PASSWORD`** — Render only. Existing signed-in sessions continue
until their 8-hour cookie expires; new sign-ins need the new passcode. To evict
everyone immediately, rotate `DEMO_SESSION_SECRET` as well.

**`OPENAI_API_KEY`** — Render only. Revoke the old key at the provider *after*
the new one is deployed and `/readyz` is green, not before.

**`AWS_*`** — Render only. The artifact is already on disk, so a rotation does
not interrupt serving; it only affects the next cold start.

After any rotation, run §9.

---

## 9. Runbook: synthetic smoke test

Run against the deployed URL, in this order. Every step is observable from
outside; none of them requires reading a log.

```bash
BASE=https://<vercel-deployment-url>
API=https://<render-service>.onrender.com
```

| # | Check | Expected |
|---|---|---|
| 1 | `curl -sS "$API/healthz"` | `200`, carries a version |
| 2 | `curl -sS "$API/readyz"` | `200`, `"ready":true` |
| 3 | `curl -si "$BASE/" \| head -1` | `307` to `/access` when signed out |
| 4 | **Direct-backend rejection**: `curl -si -X POST "$API/v1/turns/stream" -H 'content-type: application/json' -d '{"query":"x","client_request_id":"00000000-0000-4000-8000-000000000000"}' \| head -1` | `401`. The backend is private; only the proxy holds the token |
| 5 | **Access**: sign in at `$BASE/access` with `DEMO_ACCESS_PASSWORD` | reaches the question screen |
| 6 | **Chat**: ask "what does my HbA1c mean?" | an answer with numbered sources |
| 7 | **Source links**: click a citation marker | scrolls to that source; link opens PubMed with `rel="noopener noreferrer nofollow"` |
| 8 | **Emergency**: ask "I have crushing chest pain radiating to my arm" | the emergency card, **no sources, no brief** |
| 9 | **Insufficient evidence**: ask something the corpus cannot support | "Not enough verified information", not an error card |
| 10 | **Brief export**: build a brief, export it | a `.txt` downloads; `content-disposition: attachment` |
| 11 | **Refresh** mid-conversation | turns are gone, the session notice explains why — content is never restored |
| 12 | **Clear** | server session deleted; page empties |
| 13 | **Logout** | returns to `/access`; the back button does not restore the conversation |
| 14 | **Expiry**: `curl -si "$BASE/api/proxy/v1/session" -H 'cookie: synapse_access=invalid' \| head -1` | `401`, not a 500 |
| 15 | **Transparency**: open `$BASE/transparency` | see §9.1 |
| 16 | **No analytics**: open devtools > Network, reload | requests go only to the Vercel origin and `fonts.gstatic.com`. No Sentry, no GA, no Vercel Analytics |

Steps 5–13 are the ones a browser must do; steps 1–4 and 14 are curl-able.

### 9.1 Verifying the transparency page against the deployed manifest

The transparency page must describe the artifact that is actually loaded, not
the one in the repository.

```bash
# What the deployment says it is serving:
curl -sS "$API/readyz" | python -m json.tool
# -> artifact: {...}, which names the loaded artifact

# What the page shows:
open "$BASE/transparency"
```

Confirm the artifact identifier and corpus counts on the page match `/readyz`,
and that the page still states that no clinician has reviewed any source. If
they disagree, the frontend is talking to a different backend than you think —
check `SYNAPSE_API_URL`.

---

## 10. Runbook: monitoring

**What is emitted.** Counters, typed codes, stage timings and durations. No
query, answer, excerpt, note, credential or token — by construction, not by
redaction: `synapse/service/progress.py` carries a closed enum and a frozen
message table, so patient text cannot reach a progress line at all. The privacy
policy is `docs/privacy-logging-policy.md`, and
`tests/test_telemetry_privacy.py` fails the build if it is violated.

**Where to look.**

| Signal | Where | Means |
|---|---|---|
| `/readyz` not 200 | Render health check | Traffic is being withheld. Read `failure_code` |
| `failure_code: index_unverified` | `/readyz` body | Artifact missing or digest mismatch |
| `failure_code: configuration_error` | `/readyz` body | A variable is unset; `detail` names which |
| `turn rendered` count falling | Render logs | Fewer answers being produced |
| `retrieval failed` rising | Render logs | Index or embedding provider trouble |
| `answer generation failed` rising | Render logs | Provider outage or rate limit |
| 401 rate rising on `/v1/*` | Render logs | Token mismatch between platforms, or probing |

**What is deliberately absent.** No third-party browser analytics and no
client-side error reporting — no Sentry, no Vercel Analytics, no Google
Analytics, no session replay. A patient reading a medical result must not be
observed by a fourth party, and a session replay of this interface would capture
free-text symptoms. `tests/test_deployment_config.py` fails the build if such a
dependency is added or such a host is referenced.

---

## 11. Runbook: rollback

Roll back the **frontend** and the **backend** independently; they are separate
artifacts with a versioned contract between them.

**Frontend (Vercel)** — instant, no rebuild:

```bash
vercel rollback <previous-deployment-url>
```

**Backend (Render)** — redeploy the previous image from the dashboard's Deploys
tab ("Rollback" on the last known-good deploy).

**The artifact** — if a bad corpus was published, do not delete it from the
bucket. Point the service back at the previous key and digest together:

```
SYNAPSE_ARTIFACT_KEY    = runtime/synapse-runtime-<previous>.tar.gz
SYNAPSE_ARTIFACT_SHA256 = <previous digest>
```

Then redeploy. Keeping the bad artifact means the incident stays reproducible.

**Order.** If the contract changed, roll the **frontend back first**: an older
interface against a newer API degrades to a typed failure, whereas a newer
interface against an older API can request endpoints that do not exist. After
either rollback, re-run §9.

# Synapse v0.3C — session handoff

Paste this at the start of a new conversation. It is written for Claude, not
for a person: it front-loads the constraints, then the traps, then the work.

---

## 0. Standing constraints — read before editing anything

**Before editing:** read `README.md`, `app.py`, `synapse/ui/pipeline.py`,
`docs/SYSTEM_CARD.md`, `docs/PRIVACY_DATA_FLOW.md`, `docs/accessibility.md`,
and every applicable test. Inspect `git status` and preserve unrelated changes.
Establish and report the test baseline before touching it. **Never weaken,
delete, skip, xfail, or rewrite a safety test to make it pass.**

**Locked architecture.** Next.js 16 / React 19 / strict TS / Tailwind 4 / npm /
Vercel is the primary UI. FastAPI lives in this Python repo, one Render
container. The browser calls a same-origin Next.js server proxy, never Render
directly. Shared-passcode gate, signed 8-hour HttpOnly cookie, 2-hour in-memory
backend sessions. `OPENAI_API_KEY` is server-side only. No database, no
persisted conversation, no third-party analytics, no Sentry, no raw content
logging. Streamlit remains a supported local fallback over the same application
service. Premium light medical-tech identity retaining the EKG logo. Separate
evidence/transparency page. Unrestricted free text with a persistent warning not
to enter patient identifiers. Demo-only unreviewed evidence must be disclosed
and never described as approved or clinically validated.

**Absolute invariants.**
- The emergency check precedes retrieval and generation.
- No unvalidated model prose reaches any UI.
- Unsupported claims are withheld.
- Citations and source numbering stay stable and resolvable.
- Failure states contain fixed application copy and typed codes only.
- The permanent disclaimer is application-owned.
- Queries, answers, excerpts, notes, credentials, tokens and passwords are
  never logged.
- React renders dynamic data as text. No `dangerouslySetInnerHTML`.
- Standard CI is offline and has no API key.
- Never claim HIPAA compliance, medical-device status, clinical validation,
  production readiness, or source approval.
- Never commit secrets, generated runtime archives, `node_modules`, build
  output, or legacy pickle files.

**Process.** Small reviewable changes. After each phase, run its complete gate
and report: files/behaviour changed, tests and commands run, remaining failures
or risks, and `git status`. Do not start the next phase while a required gate is
failing.

**One hard rule about the filesystem:** never remove, move or rename anything in
`~/Desktop/Synapse` (the sibling original). Copy only.

---

## 1. What this is

A pre-clinical medical **appointment-preparation** RAG system. It reads
published research and turns it into questions worth raising with a clinician.
It is explicitly *not* a symptom checker: it does not diagnose, does not judge
urgency, and does not advise on medication.

State: `main` @ `50426d5`, working tree clean, pushed to
`https://github.com/ajay-karthikAI/Synapse-v0.3C`.

Phases 1–5 are complete: service extraction, verified runtime artifacts,
FastAPI + access + sessions + SSE, Next.js foundation and identity, and the
full patient experience.

---

## 2. Architecture, in dependency order

| Path | Role |
|---|---|
| `synapse/service/` | Framework-neutral turn pipeline. `progress.py` carries a closed enum + frozen message table, so no query or answer text can reach a progress indicator *by construction*. |
| `synapse/runtime/` | Verified read-only artifacts. `archive.py` never calls `extractall`; refuses traversal, symlinks, devices, bombs. Packing is byte-reproducible. |
| `synapse/retrieval/native.py` | Sparse state is **rebuilt** from a verified corpus, never unpickled. Dense load refuses on count/dim mismatch. |
| `synapse/api/` | FastAPI. `TurnEnvelope` is a discriminated union on `kind` — a client cannot receive "an answer that might be a failure". |
| `legacy_index.py` *(repo root)* | The quarantined seam binding the prototype's `Data/` + `Retrieval/`. Must stay outside `synapse/`. |
| `serve_api.py` *(repo root)* | ASGI entrypoint. Same root-level reason as above. Degrades to a typed readiness failure rather than crashing. |
| `app.py` *(repo root)* | Streamlit fallback, same application service. |
| `frontend/` | Next.js. Browser → same-origin proxy → FastAPI. |

**Why `legacy_index.py` and `serve_api.py` are at the root:**
`tests/test_no_pickle_and_imports.py` forbids `synapse/` from importing the
legacy trees at all. Moving either inside the package breaks CI.

### Frontend specifics

- `TurnView.tsx` switches on `kind` with `assertNever` in the default, so a
  fifth server envelope **fails the build** rather than rendering blank.
- `role="alert"` is used exactly once, for the emergency card. Note Next
  injects its *own* empty `role="alert"` route announcer into `<body>` — scope
  assertions to `main`.
- Focus moves to the **result heading** on completion, so a screen reader says
  "…, heading level 2" rather than re-reading the page.
- The appointment brief is a non-modal `complementary` region on desktop and a
  modal `dialog` on mobile — different ARIA, different keyboard contract, not
  just CSS.
- **No endpoint accepts a whole brief.** Each edit is one named operation on one
  field, because a brief carries verified claims and support levels.

### Fixtures are generated, not written

`frontend/tests/fixtures/*.json` is produced by `tests/frontend_fixtures.py`
from `tests/golden_states.py`, through the same `envelope_for` the route calls.
`tests/test_frontend_fixtures.py` fails the Python gate when a committed file
drifts. `everyEnvelope()` reads the directory rather than a list, so a golden
state with no screen fails a test instead of going unrendered.

Regenerate deliberately, then read the diff:

```bash
SYNAPSE_UPDATE_SNAPSHOTS=1 pytest tests/test_frontend_fixtures.py
```

---

## 3. Traps that have already cost real time

1. **Never `source .venv/bin/activate`.** The user's shell profile re-points
   `python` at the sibling `~/Desktop/Synapse v0.3`. Symptoms are baffling:
   `anyio`/`pytest` import from the wrong tree, unrelated collection errors.
   Always `./.venv/bin/python -m …`.

2. **Never run the frontend with `next dev`.** The nonce CSP with
   `'strict-dynamic'` blocks dev HMR, the bootstrap fails, and **React never
   hydrates**. The page looks completely normal, you can type into fields, and
   no button ever enables — because `disabled` comes from React state that never
   updates. It reads as "the app is broken", not as a CSP error. Diagnose with
   `Object.keys(el).filter(k => k.startsWith("__react")).length` — `0` means no
   hydration. Use `npm run build && npx next start`. Consequence: no hot-reload.

3. **Verify UI changes in a browser, not with curl.** An API probe passed
   happily while the entire UI was dead. The two failure modes do not overlap.

4. **CI failures may not reproduce in the local venv.** CI installs
   `-e ".[dev,api]"` *fresh*. A long-lived local venv pins older transitive
   deps. Build a throwaway venv to reproduce:
   ```bash
   uv venv /tmp/civenv --python 3.12
   uv pip install --python /tmp/civenv/bin/python -e ".[dev,api]"
   /tmp/civenv/bin/python -m pytest -m "not live" -q
   ```

5. **CI's mypy is scoped**, and excludes `synapse/cli` and `synapse/ingest`.
   Bare `mypy synapse` reports ~26 pre-existing errors that are *not* gate
   failures. Use the explicit package list in `.github/workflows/ci.yml`.

6. **"This string is absent" tests must strip comments first.** Files document
   the very thing they forbid, and a naive scan reports the file explaining the
   rule as the file breaking it. This has now happened three times.

7. **Playwright matches accessible names by substring** unless `exact: true`.

---

## 4. Running it locally

Both are currently up.

```bash
# Backend — env from the gitignored root .env (OPENAI_API_KEY + 3 SYNAPSE_*)
set -a; . ./.env; set +a
.venv/bin/python -m uvicorn serve_api:app --host 127.0.0.1 --port 8000
# expect: /readyz -> {"ready":true,...,"artifact":{"index":"legacy"}}

# Frontend — config in the gitignored frontend/.env.local
cd frontend && npm run build && npx next start --port 3210
```

Sign in with `SYNAPSE_ACCESS_PASSCODE` from `.env`.

> The local passcode is `synapse` and the tokens are `local-demo-*`. Fine on
> localhost, an open door on a public URL. Generate real ones for Render.

---

## 5. Gates, and the numbers they currently produce

```bash
# Python
./.venv/bin/python -m pytest -m "not live"     # 1706 passed, 9 skipped
./.venv/bin/python -m ruff check .
./.venv/bin/python -m ruff format --check synapse tests
# mypy: use the explicit package list from .github/workflows/ci.yml  (117 files clean)

# Frontend
cd frontend
npm run gates          # lint + typecheck + verify:api + 339 vitest + build
npx playwright test    # 178 across chromium and mobile
```

---

## 6. Open work, most valuable first

### a. Relevance banding is wrong against real data
`synapse/retrieval/evidence.py:70` computes `fused_score / relevance_scale`
with `relevance_scale = 10.0`, but the linear/RRF fusion actually produces
scores around `0.03`. Every source therefore lands near `0.003`, and
`matchBand()` in `frontend/src/components/answer/SourceList.tsx` labels **all**
of them "Loosely matched the question".

Fix upstream by calibrating the scale, or make the label rank-relative so it
cannot misrepresent whatever scale the backend uses. Upstream changes what every
patient sees, so confirm the intent first. Unit tests pass today because the
golden fixture hand-sets `0.91` / `0.74`.

### b. No regression test for the proxy retry
`frontend/src/app/api/proxy/[...path]/route.ts` retries once when a pooled
keep-alive socket was closed by a backend restart and throws before sending a
byte. Verified by hand in a browser; no automated test yet.

### c. Phase 6 — bridge the verified artifact into the API
Nothing adapts `RuntimeBackends` onto `synapse/service/index.py`'s
`IndexProvider`, so `RuntimeIndex` is referenced **only in tests**. `serve_api.py`
therefore serves from the *legacy* index (`processed_chunks.pkl` +
`hybrid_index/`), exactly as `app.py` does — which does make the parity claim in
`docs/migration-parity.md` checkable, but is not the locked architecture. The
deployed design (one container, pinned read-only artifact) needs this adapter.
It is a module plus tests, and it is the last thing between this and a real
deployment.

### Smaller, carried from earlier phases
- `frontend/src/middleware.ts` → `proxy.ts` (Next 16 deprecation warning).
- `index_unverified` never reaches the turn path — `synapse/ui/pipeline.py` maps
  it onto `retrieval_failed`. ~3 lines. See `UNREACHABLE_CODES` in
  `tests/golden_states.py`.
- Three insufficient-evidence reasons (`failed_validation`,
  `conflicting_sources`, `out_of_scope`) are renderable but unreachable from a
  turn. Recorded deliberately in `REACHABLE_FROM_A_TURN`.
- `source_pack_sha256` exists in the manifest schema but is never compared
  against the live pack.

---

## 7. What is deliberately not built

- **Token-by-token streaming.** Permanent. Showing unverified prose and
  retracting it is precisely what the answer layer prevents. The envelope
  arrives whole, once, at the end.
- **Conversation restoration.** The API exposes counts and clocks but never
  content, so a stolen token cannot become a transcript. The UI says so plainly
  rather than pretending the session is empty.
- **Any test that needs a running backend.** Unit tests mock `fetch`; E2E
  intercepts `/api/proxy/**` and answers with the golden fixtures.

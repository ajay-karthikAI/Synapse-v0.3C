# The Frontend

**Applies to:** `frontend/` · `docs/api.md`
**Status:** foundation and identity only. **Answer rendering is not built.**
Never deployed.

> A branded shell with a working access gate and a secure proxy. It cannot yet
> ask a question or display an answer — that is the next phase, and it is
> deliberately not started, because the states a patient can reach are already
> pinned on the server and guessing at them would be wasted work.

---

## 1. Stack, and why each version is what it is

Node 24 (active LTS), npm, committed `package-lock.json`.

| Package | Pin | Why this and not `latest` |
|---|---|---|
| `next` | 16.3.4 | Latest stable 16.x |
| `react` / `react-dom` | 19.2.8 | Latest stable 19.x |
| `tailwindcss` | 4.3.3 | Latest 4.x; no `tailwind.config.js` — tokens live in CSS |
| `typescript` | **5.9.3** | `latest` is 7.0.2, the native port. `eslint-config-next@16.3.4` depends on `typescript-eslint@^8`, which supports TypeScript up to 5.9 |
| `eslint` | **9.39.5** | `latest` is 10.x. The `eslint-plugin-react` bundled inside `eslint-config-next@16.3.4` calls `contextOrFilename.getFilename`, which ESLint 10 removed — linting crashes outright on 10 |
| `vitest` | 4.1.11 | |
| `@playwright/test` | 1.62.1 | Matches Next 16's own peer range |
| `jose` | 6.2.10 | Web Crypto JWT. Middleware runs on the Edge runtime, which has no Node `crypto` |

Both downgrades follow the brief's criterion — *compatible with Next.js 16* —
and both were found by running the tool, not by reading a changelog. They move
when `eslint-config-next`'s own dependency tree moves.

Every version was checked against the registry before it was written down.

---

## 2. The identity

| Token | Value | Role |
|---|---|---|
| `--color-canvas` | `#F6F8FA` | The page |
| `--color-surface` | `#FFFFFF` | Cards, panels |
| `--color-ink` | `#102A3A` | Body and headings |
| `--color-ink-secondary` | `#46616F` | Supporting text |
| `--color-navy` | `#082B3A` | Deepest surface |
| `--color-teal` | `#0B7189` | Primary action, links |
| `--color-violet` | `#5B3FD6` | Evidence, citations |
| `--color-border` | `#718C99` | Control boundaries |
| `--color-emergency` | `#B42318` on `#FEF3F2` | The one state that may interrupt |
| `--color-warning` | `#8A4B0F` on `#FFF7E8` | The identifier warning |

Geist Sans. Radii 12–16px. Two shadow levels, both barely there.

**Every pairing was measured before it was written down**, using this
repository's own `synapse.a11y.contrast` — the same machinery that measures the
Streamlit palette. All sixteen pass WCAG AA; the lowest is the border at 3.34:1
against the canvas, against a 3.0 requirement for a non-text control boundary.
`tests/unit/palette.test.ts` re-measures them on every run, reading the values
**out of `globals.css` itself** rather than from a copy.

### The mark

The same eight-point ECG polyline as the previous identity — it is recognisably
the same mark. What changed: the violet-on-black glow and its Gaussian blur are
gone, replaced by a flat teal→violet gradient that reads on a light canvas.

**The animation is preserved.** The trace draws itself once in 1.4s and rests;
it never loops. It is the only motion in the interface. Under
`prefers-reduced-motion` it is not merely stopped but shown **complete** — a
reduced-motion user gets a finished logo, not a half-drawn line.

The mark is `aria-hidden` throughout: it duplicates the wordmark beside it, and
announcing "image" before every heading is noise.

### What is absent, and stays absent

No ambient streaks (removed in the accessibility audit, not reintroduced). No
stock photography — there are **zero** `<img>` elements. No seals, badges,
shields or checkmarks.

That last one is a safety decision, not a taste one: no clinician has reviewed
any source in this system, and an interface that *looks* accredited makes a
claim the system cannot support. `tests/e2e/shell.spec.ts` asserts the absence.

The product name is set small, in secondary ink, with wide tracking. It is
deliberately never placed inside the sentence describing what the system is not,
where a second organisation's name would read as a co-signature.

---

## 3. The proxy

The browser never talks to FastAPI. It calls this server, which forwards.

**Headers are built, never forwarded.** `buildBackendHeaders` starts from an
empty `Headers` and adds only what it decides. There is no allow-list to get
wrong because there is no copying. A client therefore cannot supply:

- its own `X-Service-Token` (which would reach the backend as this server);
- its own `Authorization` (which would reach another session);
- its own `X-Forwarded-For` — which matters, because the backend's login limiter
  keys on it, so spoofing it would reset the rate-limit bucket at will.

Only `content-type` and `accept` cross, and only by name.

**The path is an allow-list.** `v1/turns`, `v1/session`, `v1/transparency`,
`readyz`. Without it the proxy is an open relay to every endpoint the backend
will ever have, including ones added later by someone who did not know this file
existed.

**Streaming passes through untouched.** `upstream.body` goes straight into the
`Response`, and `X-Accel-Buffering: no` is preserved so no intermediate proxy
re-buffers what this one did not.

**Responses are filtered.** A backend `Set-Cookie` never reaches the browser.

---

## 4. The access gate

One token in the system: FastAPI issues and signs it, this server forwards it
unchanged. Minting a second Next.js-signed cookie would mean two secrets, two
expiries that can disagree, and a logout that has to clear both or silently does
not.

Verification happens three times, independently: middleware decides *routing*,
the API route decides whether a request is made at all, and the backend decides
whether it is answered. A middleware matcher that misses a path is a cosmetic
bug, not a hole.

### The lifetime mismatch is real and handled

The cookie lasts 8 hours; the backend session expires after 2 hours idle and
dies entirely on restart. A browser can hold a valid, unexpired cookie whose
session no longer exists, and the API answers that with a `401` deliberately
indistinguishable from a forgery.

All three cases are tested: expired cookie, forged cookie, and sign-out. An
expired cookie is *cleared* rather than left to fail on every request.

**API routes are never redirected.** Middleware returns typed JSON for `/api/*`
rather than bouncing to the sign-in page — a `fetch` caller would otherwise get
a 307 and an HTML document where it expected `{code, message}`, and a streaming
client would see a redirect mid-request.

---

## 5. Security headers

Static headers in `next.config.ts`; the nonce-based CSP in `middleware.ts`,
because it varies per request.

```
default-src 'self'; script-src 'self' 'nonce-…' 'strict-dynamic' https:;
style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none';
base-uri 'none'; object-src 'none'; upgrade-insecure-requests
```

`connect-src 'self'` is the one doing the most work: it stops a script that
somehow reached the page from exfiltrating anywhere.

`style-src 'unsafe-inline'` is a **stated weakening**. Next injects inline
styles during hydration and offers no nonce hook for them. Style injection is a
far smaller hazard than script injection, and the alternative — no CSP — is
worse.

Also: `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, a
deny-everything `Permissions-Policy`, COOP/CORP, no `X-Powered-By`. HSTS in
production builds only.

**No CORS middleware exists.** Enabling it would make the backend reachable from
a page, which is what the service token exists to prevent.

---

## 6. Gates

```bash
npm run lint          # eslint, flat config
npm run typecheck     # tsc --noEmit, strict + noUncheckedIndexedAccess
npm run verify:api    # generated types match the committed OpenAPI contract
npm test              # vitest, 339 tests
npm run build         # production build
npx playwright test   # 178 e2e across desktop and mobile, axe on every state
```

`npm run gates` runs all but the e2e.

`react/no-danger` is an **error**. The project-wide invariant — dynamic data is
rendered as text, never as markup — is enforced by the linter rather than by
review. There is no `dangerouslySetInnerHTML` anywhere.

---

## 7. The answer surface

### The exhaustive switch

`TurnEnvelope` is a discriminated union on `kind`, and `TurnView` switches on
it with `assertNever` in the `default`. Because `assertNever` takes `never`, a
fifth envelope added to the API makes this file **fail to compile**. That is the
only mechanism that reliably stops a new server state from rendering as a blank
region in a browser nobody is watching.

Within `kind: "answer"`, `action` distinguishes three genuinely different
screens: `answer`, `abstain` (the insufficient card, nested — the abstention
*is* the insufficient state) and `medical_staff`.

### `role="alert"` appears exactly once

Only the emergency card. An alert interrupts whatever a screen reader is
saying, so a page where several things claim to be alerts is a page where the
one that matters queues behind a loading message. Progress, failures and
insufficient evidence all use ordinary structure and polite live regions.

Note that Next injects its own empty `role="alert"` route announcer into the
body. It is not the application's, never carries content, and is why the e2e
assertions scope to `main`.

### Focus

When a turn completes, focus moves to its result **heading** — not a container,
not the body. A screen reader then announces "What the research says, heading
level 2" and stops, rather than re-reading the page. `scrollIntoView` is called
optionally: focus is what matters, scrolling is a nicety, and letting a missing
method throw would take the whole answer down with it.

### No two faults share a reference code

Every failure screen carries fixed application copy plus a typed reference code,
and no two faults may share one — support cannot tell them apart if they do.

One pair used to. `index_unverified` could not reach the turn path:
`answer_turn` wrapped the retrieval stage in a handler that returned a
hard-coded `retrieval_failed` and never called `classify`, so "the index did not
match its manifest" — an integrity failure, and the reason the index gate exists
— was reported as an ordinary retrieval error, on screen and in the logs.

The retrieval handler now preserves that one code from `classify` and leaves
every other exception on `retrieval_failed` (so a provider error during
retrieval does not surface as `generation_unavailable`). Because
`index_unverified` is in `EVIDENCE_FAILURE_CODES`, the state renders as an
insufficient-evidence screen that names the reason — "Synapse could not confirm
its research library was intact, so it has not used it. This is a problem with
the tool, not with your question" — instead of the generic error card.

`visual.spec.ts` asserts code uniqueness with **no exceptions**, and
`tests/test_service_golden.py` asserts the integrity and transient failures stay
distinguishable.

### The composer follows the conversation

Before the first answer the composer is the page: centred, with the example
chips beneath it. Once a turn has been **answered** it moves below the newest
result, so the document reads question → answer → ask again, and the box to
continue in is the last thing before the clear control.

Two constraints shape that.

**There is only ever one composer.** The field carries a fixed `id="question"`,
and a second copy would mean a duplicated id and two identically labelled boxes
with nothing to distinguish them in the accessibility tree. So it moves; it is
not duplicated.

**It moves on the answer, not on the submit.** `turns.length > 0` gates the
position rather than `started`. Moving it the instant Ask is pressed would
unmount the button that currently holds focus and drop a keyboard user to the
document body for the length of the request. At the boundary that is actually
used, the arriving turn takes focus to its own heading in the same commit, so
the move costs nothing — asserted in the e2e suite for the follow-up as well as
the first turn.

Follow-ups are resolved against the session's history on the server
(`synapse.memory.query_rewrite`), which is what makes a bare "what about the
side effects?" a complete question here.

### Relevance is never a percentage

The server sends a 0..1 retrieval score whose own field description says it is
neither a confidence nor a quality grade. Rendered as a coarse verbal band
("Closely matched the question"), because "91%" beside a citation is read as
"91% reliable" by every patient who sees it. A unit test asserts no `%` reaches
an answer.

**And it is often absent, which is a feature.** The number is the *reranker's*
judgement of how well a passage answered this question, scaled by
`RerankConfig.max_score` — never a fusion score. Under RRF, the default, a fused
score is `sum(1 / (k + rank))` and therefore a function of rank alone, bounded
near 0.033 whatever the corpus actually held; rescaling it would put the top
source near 100% on every query, including one nothing in the corpus can answer.
So when reranking is disabled, skipped, or degrades to the fused order,
`relevance` is `null` and `matchBand` renders **no band at all** rather than a
manufactured one. That is the contract `docs/evidence-ux.md` §2 already sets out
("Retrieval relevance — the reranker — absent when reranking was skipped or
degraded"), enforced in `synapse/retrieval/evidence.py` and covered by
`tests/test_service_retrieval.py`.

---

## 8. The appointment brief

Two components in one file, because the breakpoint changes **semantics**:

| | desktop (≥1024px) | mobile |
|---|---|---|
| role | `complementary` | `dialog`, `aria-modal` |
| focus | moved in, not trapped | trapped |
| Escape | closes | closes |
| scrim | none | yes |

A full-screen overlay that does not trap focus strands a screen-reader user
underneath it; a side panel that does trap focus makes the rest of the page
unreachable. `useMediaQuery` uses `useSyncExternalStore`, which is what that
hook is for and which gives the server a separate snapshot — both sides render
narrow, so there is no hydration mismatch.

**No endpoint accepts a whole brief.** Each edit is one named operation on one
field. A brief carries verified claims with their support levels and source
numbers; an endpoint that accepted a replacement would let a caller print
unverified claims on a document a patient hands to a clinician. A unit test
asserts every request body has exactly one key and never contains `claims`,
`sources` or `document_id`.

Reordering is **move-up/move-down buttons, not dragging** — keyboard, touch and
screen reader all work. Exports are real `<a href>` links to the proxy, with no
`download` attribute, so the browser uses the server's own
`Content-Disposition` filename and `nosniff` content type.

---

## 9. Fixtures are generated from the Python contract

`frontend/tests/fixtures/*.json` is **generated**, not written. `tests/frontend_fixtures.py`
runs every golden state in `tests/golden_states.py` through the same
`envelope_for` the streaming route calls, and `tests/test_frontend_fixtures.py`
fails the Python gate when a committed file drifts.

```bash
SYNAPSE_UPDATE_SNAPSHOTS=1 pytest tests/test_frontend_fixtures.py
```

Hand-written TypeScript fixtures would test that the renderer matches *what its
author believed the server sends* — the belief most likely to be stale. These
cannot drift silently.

`everyEnvelope()` reads the directory rather than a list, so a golden state
added on the Python side arrives in the TypeScript suite automatically. A state
with no screen fails a test rather than going unrendered.

---

## 10. What is not built

- **Multi-turn conversation restoration.** The API exposes counts and clocks but
  never conversation content, deliberately, so a stolen token cannot be turned
  into a transcript. The interface therefore reports how many turns the server
  holds and says plainly that the earlier text cannot be shown.
- **Token-by-token streaming.** Deliberate and permanent. Showing unverified
  prose and retracting it is exactly what the answer layer exists to prevent, so
  the envelope arrives whole, once, at the end.
- **Any integration with a running backend.** Every test here runs without one:
  unit tests mock `fetch`, e2e tests intercept `/api/proxy/**` and answer with
  the golden fixtures.

---

## 11. Related

[api.md](api.md) · [runtime-artifacts.md](runtime-artifacts.md) ·
[migration-parity.md](migration-parity.md) · [accessibility.md](accessibility.md)

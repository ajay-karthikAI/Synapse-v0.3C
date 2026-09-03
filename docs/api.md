# The HTTP API

**Applies to:** `synapse.api` · `tests/test_api.py` · `tests/snapshots/api/openapi.json`
**Status:** implemented and tested offline. **Never deployed.** No process has
served a real request.

> This describes a mechanism. Nothing here is evidence that the answers are
> clinically correct — only that the transport does not weaken the guarantees
> the answer layer already provides.

---

## 1. Shape

One FastAPI process over [`synapse.service`](../synapse/service/), which is the
same service the Streamlit fallback runs. The browser never talks to it: it
calls a same-origin Next.js proxy, which holds the service token and forwards.

```
browser ──same-origin──▶ Next.js proxy ──X-Service-Token──▶ FastAPI ──▶ service
                              (holds the service token)
```

**No CORS middleware exists**, deliberately. Enabling it would make the backend
directly reachable from a page, which is the architecture the service token
exists to prevent.

---

## 2. Two credentials, both required

| | Carried by | Proves |
|---|---|---|
| `X-Service-Token` header | the proxy, never the browser | the request came from our front end |
| Access token (cookie or `Authorization: Bearer`) | the browser, via the proxy | someone typed the shared passcode |

Neither alone is enough. A leaked access token is useless without the service
token; the service token alone reaches no conversation.

**Every rejection is an identical `401`** with the same body — missing token,
wrong token, expired token, unknown session, expired session. A caller that
could tell them apart would use the endpoint as an oracle for which session ids
exist.

The access token is an HS256 JWT carrying a random session id, `iat` and `exp`,
and nothing else. It is decoded with `algorithms=["HS256"]` passed explicitly,
so a token whose header says `{"alg":"none"}` is refused rather than trusted.

### The login limiter

Failed logins are counted per client address, derived by counting
`X-Forwarded-For` entries **from the right**, past the proxy hops this
deployment actually has (`SYNAPSE_TRUSTED_PROXY_HOPS`). Entries to the left were
supplied by the client and may be invented, so a caller cannot reset its own
bucket by prepending them.

Order matters and is asserted: service token → lockout check → constant-time
comparison. A locked-out caller never reaches the comparison at all. **Successes
clear the counter**, so one person's typos cannot lock out the next patient
sharing a clinic address.

---

## 3. Routes

| Route | Auth | Notes |
|---|---|---|
| `GET /healthz` | none | Liveness. Must not depend on the artifact |
| `GET /readyz` | none | 503 until every startup gate passes |
| `POST /v1/access/login` | service token | Passcode → 8-hour token + HttpOnly cookie |
| `POST /v1/access/logout` | service token | Idempotent; never says whether a session existed |
| `GET /v1/session` | both | Counts and clocks only — never conversation content |
| `DELETE /v1/session` | both | Explicit end. Nothing is archived |
| `POST /v1/turns/stream` | both + ready | SSE; see §4 |
| `GET/PUT/POST/DELETE /v1/turns/{i}/brief/...` | both | See §5 |
| `GET /v1/transparency` | service token only | A reader deciding whether to trust this should not have to be inside it first |

`/healthz` deliberately does **not** check the artifact. A bad artifact must
produce a diagnosable unready container, not an undiagnosable crash loop.

---

## 4. Streaming a turn

Server-sent events. Zero or more `stage` events, then **exactly one**
`envelope` event, on every path — success, failure, exception, or deadline. A
client that received stages and no envelope could not distinguish "still
working" from "gave up".

```
event: stage
data: {"stage":"searching","message":"Searching relevant research..."}

: heartbeat

event: envelope
data: {"kind":"answer","action":"answer", ...}
```

- **Stages** carry a closed `ProgressStage` and the fixed message from that
  module's table. There is no parameter through which a query could reach one —
  that is structural in `synapse.service.progress`, not a convention.
- **Heartbeats** every 15 seconds, as SSE comments, so a proxy does not close an
  idle connection mid-turn.
- **Deadline** of 120 seconds, after which a `failure` envelope is emitted.
- **No medical content is streamed before validation and verification finish.**
  Token-by-token streaming would put unverified prose on the page and then
  retract it. The envelope arrives whole, once, at the end.

### The envelope

A discriminated union on `kind`. A client cannot receive "an answer that might
be a failure":

| `kind` | Meaning |
|---|---|
| `answer` | A validated answer. `action` is `answer`, `abstain` or `medical_staff` |
| `emergency` | A red flag. Cites nothing, offers no brief |
| `insufficient` | No verified evidence. A designed state, not an error |
| `failure` | A fault. A typed code and one fixed message |

An **abstention is the insufficient state**: it arrives as `kind: "answer"`,
`action: "abstain"`, with the `insufficient` block populated — because the
questions survive even when the claims do not.

No response model has a field an exception could be poured into. That makes "the
API cannot leak model output on a failure" a property of the types, asserted by
`test_no_envelope_has_a_free_text_error_field`.

### Sessions

In-memory, one process, no database — a conversation that is never written down
cannot be leaked from a backup. A restart ends every conversation, and that is
accepted.

| Bound | Value |
|---|---|
| Idle expiry | 2 hours from last activity |
| Turns per session | 20 |
| Concurrent turns per session | 1 |
| Follow-up history | 3 turns, server-derived |
| Emergency latch window | 3 turns, server-derived |

The history and the latch are **server-derived and never client-supplied**. A
client that could supply them could clear a latched red flag.

`client_request_id` makes a retry replay the stored envelope rather than run a
second turn — which would cost another model call, consume a slot against the
ceiling, and could move the safety latch on a request the client believes it
already made.

The **worker owns releasing the session**, not the request handler. A client
that disconnects mid-turn does not free a session that is still working, and
does not wedge one either.

---

## 5. The brief

**No endpoint accepts a brief.** Not whole, not partial, not "just the claims".
A client sends `{"topic": "..."}`; the server applies that one operation to the
brief it already holds.

The reason is forgery. A brief carries verified claims, their support levels and
their source numbers — the output of citation verification. An endpoint
accepting a replacement brief would let a caller put claims that were never
retrieved or verified onto a document a patient carries into an appointment. The
only writable fields are the ones the patient genuinely authored: topic, notes,
their own questions, and section selection.

Exports are built in memory and streamed out — nothing touches disk, because an
export contains the patient's own notes and a temporary file is a retention
decision nobody made. They carry `Content-Disposition: attachment`,
`X-Content-Type-Options: nosniff` and `Cache-Control: no-store`, with a filename
built from the application's own document id so no client string reaches the
header.

---

## 6. Transparency

Assembled from the source pack, the artifact manifest and the last evaluation
summary. `MANDATORY_DISCLOSURES` is a module constant, always included in full:
there is no setting that shortens it and no branch that omits it. It states that
the sources are unreviewed and that no evaluation case can gate a release.

Where a fact cannot be established the payload says `"available": false` rather
than reporting zeroes, which would read as a measured result.

---

## 7. Configuration

| Variable | Required |
|---|---|
| `SYNAPSE_SERVICE_TOKEN` | yes, ≥16 chars |
| `SYNAPSE_ACCESS_PASSCODE` | yes |
| `SYNAPSE_JWT_SECRET` | yes, ≥16 chars |
| `SYNAPSE_TRUSTED_PROXY_HOPS` | no, default 1 |

Plus the artifact variables from
[runtime-artifacts.md](runtime-artifacts.md) §4 and `OPENAI_API_KEY`.

There is **no development fallback**. A missing secret refuses to start, because
a default passcode is an open door with a changelog entry.

---

## 8. The contract

`tests/snapshots/api/openapi.json` is the generated OpenAPI document, committed.
A change to the wire contract is a visible diff in a pull request rather than
something a client discovers at runtime. Regenerate deliberately:

```bash
SYNAPSE_UPDATE_SNAPSHOTS=1 pytest tests/test_api_openapi.py
```

Interactive docs (`/docs`, `/redoc`) are disabled in the served process.

---

## 9. What this does not do

- **Readiness gates turns; it does not gate the brief or transparency routes.**
  Reading an existing brief needs no artifact.
- **No per-session rate limiting on turns** beyond the 20-turn ceiling and the
  one-active-turn gate.
- **Sessions do not survive a restart**, by design. There is no session
  migration and no sticky-session requirement documented for the platform.
- **It has never served a real request.** Every test uses fake providers and
  fake retrieval; no artifact has been built from a real corpus
  (runtime-artifacts.md §10).

---

## 10. Related

[runtime-artifacts.md](runtime-artifacts.md) ·
[migration-parity.md](migration-parity.md) ·
[PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md) ·
[privacy-logging-policy.md](privacy-logging-policy.md) ·
[answer-rendering.md](answer-rendering.md) ·
[SYSTEM_CARD.md](SYSTEM_CARD.md)

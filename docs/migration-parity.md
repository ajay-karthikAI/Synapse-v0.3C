# Migration Parity

**Applies to:** `app.py` · `synapse.service` · the planned FastAPI and Next.js
tiers
**Status:** Phases 1–4 complete (service extraction, verified runtime
artifacts, HTTP surface, frontend foundation). Phases 5–8 not started.

> **This document is a map, not a claim of completion.** Every row states where a
> behaviour lives *now* and where it is *going*. A row marked "planned" is not
> built. Nothing here asserts that the new tiers exist, work, or are equivalent.

---

## 1. Why this exists

The interface is being re-platformed: Next.js in the browser, FastAPI on one
Render container, with Streamlit retained as a supported local fallback. Two
interfaces over one system only stay honest if they run the *same* turn — the
moment the rewrite re-implements the emergency latch, or the FastAPI process
picks a different rerank model, the local interface stops being evidence about
the deployed one and every evaluation result covers neither.

Phase 1 removed the possibility of that drift for the parts that matter, by
moving them out of `app.py` and into [`synapse.service`](../synapse/service/),
which imports no interface framework at all. This table records what moved,
what did not, and why.

**Legend for "Destination":**

| | |
|---|---|
| **service** | `synapse.service` — shared by every interface, built (Phase 1) |
| **FastAPI** | `synapse.api` — the HTTP surface, built (Phase 3) |
| **Next.js** | `frontend/` — shell, identity, access gate and proxy built (Phase 4); answer rendering NOT built |
| **transparency** | `GET /v1/transparency` built (Phase 3); the page is Phase 5 |
| **Streamlit** | Stays in `app.py` as the supported local fallback |

---

## 2. What moved into the service (Phase 1, done)

Each of these was a closure or a block inside `app.py`'s submit handler,
reachable only by running Streamlit against a paid API. All are now typed,
type-checked, and exercised offline against fakes.

| Behaviour | Was | Now | Destination |
|---|---|---|---|
| Corpus load, index gate, index open/build, caching | `retrieve` closure, `st.session_state.chunks` / `.hybrid` | `service.index.CachingIndexProvider` | service |
| Legacy corpus/index binding (`Data`, `Retrieval` imports) | inline in `retrieve` | [`legacy_index.py`](../legacy_index.py), **outside** the package | service (injected) |
| **Deployed** corpus and index loading (Phase 2) | pickle + unmanaged `hybrid_index/` | `synapse.runtime` + `synapse.retrieval.native` | FastAPI |
| **Deployed** BM25 state (Phase 2) | `bm25.pkl`, unpickled | rebuilt at startup, legacy tokenizer | FastAPI |
| **HTTP surface** (Phase 3) | none | `synapse.api`; typed envelopes, SSE, sessions | FastAPI |
| Source-pack eligibility resolution | `_eligible_documents` closure | `service.governance.resolve_eligible_documents` | service |
| Follow-up rewrite for retrieval | inline in `retrieve` | `service.retrieval.RetrievalService._rewrite` | service |
| Candidate search and batched rerank | inline in `retrieve` | `service.retrieval.RetrievalService.retrieve` | service |
| Retrieval-context history, with its exclusions | `_history_for_retrieval` closure | `Conversation.retrieval_history` | service |
| Prior-questions context for generation | inline list comprehension | `Conversation.question_history` | service |
| Emergency latch across turns | `_recent_escalation` closure | `Conversation.recent_escalation` | service |
| Provenance stamps for telemetry | inline dict | `SynapseService.provenance` | service |
| Generation client configuration (3 sites, 2 configs) | inline `OpenAIStructuredClient(...)` | `service.clients.OpenAIClientFactory` | service |
| Progress stage labels | `stage = {"label": "..."}`, a mutable string | `service.progress.ProgressStage` + fixed message table | service |
| Turn orchestration and recording | submit handler | `SynapseService.ask` | service |

### What did **not** change

Deliberately, and asserted by test:

- **Pipeline ordering.** Emergency check → retrieve → convert → generate →
  verify → decide → render still lives in `synapse.ui.pipeline`, untouched.
- **Prompt identifiers.** `answer.generate.PROMPT_ID` and
  `retrieval.rerank.PROMPT_ID` are unmoved and unmodified, so telemetry
  comparisons across the change are valid.
- **Model configuration.** `gpt-4o-mini` for the answer; the rerank config's
  model and read timeout for rerank and rewrite.
- **Display policy, source numbering, failure classification.** Not touched.
- **Telemetry privacy.** The same fields, the same nesting of the resolved query
  so it cannot reach a log line.
- **Progress wording.** The eleven stage messages are the exact strings `app.py`
  displayed before, asserted in `tests/test_service.py`.

---

## 3. Behaviour-by-behaviour map

### 3.1 Access and configuration

| Behaviour | Today | Destination | Notes |
|---|---|---|---|
| OpenAI API key | **Changed.** Sidebar password field **removed**; read from `OPENAI_API_KEY` only | service | A key typed into a page is held in a shared process, unscoped and unrotated (PRIVACY_DATA_FLOW §3). The operator supplies it now, not the patient. |
| Missing credential | Fixed failure card, code `configuration_error` | Streamlit / FastAPI | Deliberately does not say "add your key" — there is nowhere to add one. |
| Shared-passcode gate | **Built** (Phase 3): `POST /v1/access/login`, constant-time, rate-limited | FastAPI + Next.js | Streamlit still has no auth and must stay local-only. |
| Signed 8h HttpOnly cookie | **Built** (Phase 3) | FastAPI + Next.js | Issued by the API; the proxy forwards it. |
| 2h in-memory backend session | **Built** (Phase 3): `synapse.api.sessions` | FastAPI | Idle expiry, 20-turn cap, one active turn, idempotency. |
| Service-token backend gate | **Built** (Phase 3) | FastAPI | The browser never holds it; the proxy does. |

### 3.2 The turn

| Behaviour | Today | Destination | Notes |
|---|---|---|---|
| Emergency check before retrieval | service (`answer_turn`) | service | Invariant. Never re-implemented client-side. |
| Emergency latch across turns | service (`Conversation`) | service | Server-side only. A client that owned this could clear it. |
| Follow-up query rewrite | service | service | Retrieval only; never reaches generation or the detector. |
| Rewrite disclosure ("Answering about …") | `render_turn`, from bundle metadata | Next.js + Streamlit | Model-generated text; must be rendered as a React text node, never `dangerouslySetInnerHTML`. |
| Progress reporting | `run_with_progress` reads `ProgressStage` | **Built** (Phase 3): SSE `stage` events | The stage enum IS the wire format. Free text cannot be added to it. |
| Typed failure codes | service | FastAPI + Next.js | The client renders fixed copy keyed by code; it never receives a provider message. |

### 3.3 What a patient sees

Every state below has a committed golden fixture under
[`tests/snapshots/service/`](../tests/snapshots/service/), built offline by
[`tests/golden_states.py`](../tests/golden_states.py). Phase 4 must render all of
them.

| State | Today | Destination |
|---|---|---|
| Answer, fully supported | `render_answer_html` | Next.js + Streamlit |
| Answer, partially supported (marked) | `render_answer_html` | Next.js + Streamlit |
| Abstention | insufficient-evidence card + answer card | Next.js + Streamlit |
| Medical-staff routing | `render_answer_html` | Next.js + Streamlit |
| Emergency escalation (`role="alert"`) | `render_answer_html` | Next.js + Streamlit |
| Insufficient evidence (6 reasons) | `render_insufficient_html` | Next.js + Streamlit |
| Failure card + typed code | `render_failure_html` | Next.js + Streamlit |
| Supporting excerpts disclosure | `st.expander` + `render_evidence_html` | Next.js + Streamlit |
| Research sources panel | `st.expander` + `render_sources_html` | Next.js + Streamlit |
| Permanent disclaimer | `PERMANENT_DISCLAIMER` constant | Next.js + Streamlit |
| Empty-state hint | inline markup | Next.js + Streamlit |
| Patient's own question echoed | escaped user bubble | Next.js + Streamlit |

### 3.4 The appointment brief

| Behaviour | Today | Destination | Notes |
|---|---|---|---|
| Build brief from an answer | `synapse.brief.build_brief` | service-adjacent, unchanged | Already framework-neutral. |
| Brief eligibility | any answered turn except emergency | Next.js + Streamlit | Abstention **does** offer a brief; escalation does not. Pinned by golden fixture. |
| Topic / notes editing | `st.text_input`, `st.text_area` | Next.js + Streamlit | |
| Question reorder | `st.multiselect` | Next.js | **Known a11y defect** (accessibility.md §6, limitation 5): the drag has no keyboard equivalent. Phase 4 should use explicit move up/down buttons, which fixes it by construction. |
| Add own question | `st.text_input` + `st.button` | Next.js + Streamlit | |
| Section selection | `st.multiselect` | Next.js + Streamlit | Disclaimer always included, not removable. |
| One-page fit estimate + overflow advice | `estimate_fit`, `overflow_advice` | Next.js + Streamlit | Reported, never silently shrunk. |
| Export: print/PDF, text, JSON | `st.download_button` | Next.js | Files must be produced by the client or streamed from FastAPI; nothing is stored server-side. |

### 3.5 Chrome, settings and evidence

| Behaviour | Today | Destination | Notes |
|---|---|---|---|
| Dark visual identity, EKG logo, animations | `app.py` CSS | Streamlit keeps it | **Built** (Phase 4): the light identity lives in `frontend/src/app/globals.css`, measured by `tests/unit/palette.test.ts`. The EKG mark keeps its geometry and its draw animation. The dark palette in `synapse/a11y/palette.py` is untouched, because `tests/test_accessibility.py` measures it against `app.py`. |
| Reduce-motion setting | `st.checkbox` + CSS | Next.js + Streamlit | OS preference honoured by media query in both. |
| Skip link | `app.py` markup | **Built** (Phase 4): the first element in the body | It IS the first tab stop in Next.js, closing accessibility.md §6 limitation 2, which was unresolvable under Streamlit. |
| Enter-to-send binding | `st.components.v1.html` iframe | Next.js | Native in React; the iframe workaround disappears. Not built: there is no question box yet. |
| Evaluation metrics panel | sidebar, reads `artifacts/evals/*/summary.json` | **transparency** | Belongs on the transparency page, not beside a patient's question. Must keep the denominator and the "no case is release-gating" caveat. |
| Source-pack / governance record | `RESOLVER`, per evidence card | Next.js + transparency | |
| Unreviewed-evidence disclosure | evidence card labels | **transparency** + Next.js | Demo-only evidence must be disclosed and must never be described as approved or clinically validated. |
| Persistent "do not enter identifiers" warning | **Built** (Phase 4) | Next.js | Persistent, not dismissible. Free text stays unrestricted by design; the warning stays on screen as long as the field it applies to. |

---

## 4. What stays Streamlit-only

Not ported, and not intended to be:

- The dark visual identity and its animations.
- The sidebar layout.
- `run_with_progress`'s thread-and-poll progress bar — an artifact of Streamlit
  re-running the whole script. A server streams stages instead.
- Session state as the storage mechanism.

`app.py` remains a supported local fallback running the **same service**, which
is the point: it is a check on the deployed tier, not a second implementation.

---

## 5. Known gaps found during Phase 1

These were discovered by building the golden fixtures. **None is fixed here** —
Phase 1's brief is to keep pipeline ordering and failure classification
unchanged, and each of these is a behaviour change that deserves its own
reviewable diff.

### 5.1 `index_unverified` is unreachable

`synapse.ui.errors.classify` maps every artifact-integrity error to
`INDEX_UNVERIFIED`, and `synapse.service.index` runs the index gate before
opening precisely so that code is produced. It never is.

`answer_turn` wraps the retrieval stage in a handler that returns a **hard-coded**
`RETRIEVAL_FAILED` and never calls `classify`:

```python
except Exception as exc:                                    # pipeline.py
    failure = AnswerFailure(AnswerFailureCode.RETRIEVAL_FAILED, type(exc).__name__)
```

Consequences:

- A corrupt or mismatched index reaches a patient as the generic "something went
  wrong" card, not the specific "could not confirm the research library was
  intact" one.
- `app.py`'s `EVIDENCE_FAILURE_CODES` lists `"index_unverified"`, so that branch
  is **dead code**.
- `InsufficientReason.INDEX_UNVERIFIED` is renderable but unreachable.

**The safety behaviour is correct** — nothing is generated, nothing is cited, no
prose is shown. Only the card is wrong. Pinned by
`test_an_unverifiable_index_still_fails_closed` and guarded in both directions by
`test_no_unreachable_code_is_quietly_produced`, so closing the gap must be
deliberate.

**Phase 2 update.** `index_unverified` now has a producer, though still not a
turn: `synapse.runtime.readiness` reports it when the deployed artifact cannot be
proven intact.

**Phase 3 update.** That readiness is now an *enforcement point*:
`synapse.api.deps.require_ready` refuses `POST /v1/turns/stream` with the
readiness failure code before any work happens, so an unverified artifact
cannot answer a question over HTTP.

**The turn-path gap remains open and is unchanged.** If an integrity error
occurs *during* a turn — rather than at startup — `answer_turn` still returns a
hard-coded `retrieval_failed`, and the patient still sees the generic card. The
readiness gate narrows when that can happen; it does not close it. Fixing it is
a three-line change to `pipeline.py` plus the tests that pin the current
behaviour, and it is now cheaper than it was, because
`tests/test_api.py::TestEnvelopes` covers every failure code end to end.

### 5.2 Three insufficient-evidence reasons are unreachable

`FAILED_VALIDATION`, `CONFLICTING_SOURCES` and `OUT_OF_SCOPE` are defined and
renderable, but nothing decides them: the service maps two failure codes plus
abstention. Recorded in `tests/golden_states.REACHABLE_FROM_A_TURN`. Phase 4 must
not read "we can draw this card" as "the system produces this state".

### 5.3 The real-corpus migration — VERIFIED 2026-09-03

Carried as the oldest unverified assumption since Phase 2, and now closed. The
no-dedupe migration was run against the prototype's actual corpus: 2,220 chunks
in, 2,220 out, 847 documents, 162 duplicate chunk identifiers **resolved rather
than dropped**, 0 documents removed. FAISS row alignment was proven by
reconstructing six rows' own vectors from the index and confirming each resolves
to itself.

The predicted blocker — duplicate documents forcing `--dedupe`, which would
invalidate the index's row order — did not materialise. Full record in
[runtime-artifacts.md](runtime-artifacts.md) §11.

### 5.4 Brief provenance was read from keys nothing set

`app.py` read `corpus_version` and `index_version` from two `st.session_state`
keys that were never written, so every brief recorded empty strings while
telemetry read its own. Both now come from `ServiceConfig`, one source. The
values are still empty — the prototype's artifacts carry no version yet — but
they can no longer disagree.

---

## 6. Related

[quality-architecture.md](quality-architecture.md) ·
[retrieval-runtime.md](retrieval-runtime.md) ·
[privacy-logging-policy.md](privacy-logging-policy.md) ·
[PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md) ·
[accessibility.md](accessibility.md) ·
[answer-rendering.md](answer-rendering.md)

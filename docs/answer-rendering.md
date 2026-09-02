# Answer Rendering

**Applies to:** `app.py` · `synapse.ui` · `synapse.answer.render` · `synapse.answer.brief`
**Status:** implemented and tested. **Nothing rendered here has been clinically reviewed.**

---

## 1. What changed, and why it was not enough to fix the answer layer

P1 replaced free-form generation with a validated contract: `GroundedAnswer`,
deterministic citation verification, a display policy that withholds
unsupported claims, and an escaping renderer
(docs/citation-integrity.md). All of it was correct, and none of it was
reaching a patient.

The production interface still ran the legacy path end to end. `app.py`
imported `Generation.answer_generator.AnswerGenerator`, which asked a model for
six sections marked with emoji headings and returned a dict of prose. The
grounded branch in the renderer existed but nothing ever populated it, so every
real answer took the fallback: model text, escaped but **unvalidated**, with no
verification, no withholding and no source resolution behind it.

This milestone removes that path. There is now exactly one way for medical
content to reach a screen:

```
red-flag check → retrieve → typed evidence → schema-constrained generation
    → verify → display policy → render typed fields
```

Every arrow is enforced by `synapse.ui.pipeline`. There is no branch from a
failure back into prose.

---

## 2. Module boundaries

| Module | Responsibility |
| --- | --- |
| `app.py` | Presentation only: layout, styling, Streamlit calls, progress. Decides nothing. |
| `synapse.ui.pipeline` | Ordering and injection points. Returns a `TurnOutcome`: an answer **or** a typed failure, never both. |
| `synapse.ui.errors` | `AnswerFailureCode`, one patient-facing message, exception → code classification. |
| `synapse.ui.legacy_evidence` | **Deprecated** adapter for the legacy retrieval shape. §7. |
| `synapse.answer.render` | The only module that turns a validated answer into markup. Escapes everything. |
| `synapse.answer.brief` | The exported appointment brief, from the same answer and the same numbering. |
| `synapse.answer.providers` | The only module that names a provider SDK. |

The dependency direction is one-way: `app.py → synapse.ui → synapse.answer`.
Nothing in `synapse.answer` imports `synapse.ui`, and nothing anywhere imports
`app.py`.

---

## 3. What is rendered, field by field

Every section comes from a typed field on a validated `GroundedAnswer`. Nothing
is recovered from prose, so a missing section is an absent card rather than an
invisible omission.

| Field | Surface | Notes |
| --- | --- | --- |
| `summary` | Summary card | Replaced by fixed wording on abstention. |
| `claims[].text` | Claim lines | Only claims the verifier supported; see §4. |
| `claims[].source_ids` | Inline `[n]` markers | Numbered once, in retrieval order. §6. |
| `claims[].supporting_excerpts` | Evidence cards | Verbatim quote + `chunk_id`, keyed by `claim_id`. |
| `doctor_evaluation` | "What your doctor will evaluate" | Framing, not a claim about the patient. |
| `questions_for_doctor` | Question card | Uncited by design — they assert nothing. |
| `limitations` | Limitations list | Abstention appends its explanation here. |
| `action` | `data-action` attribute + state card | §5. |
| `disclaimer` (model-supplied) | **Not rendered.** | The interface renders its own. §5. |

The container is `<div class="answer-view" data-action="…">`, so "did this
abstain?" is answerable by inspecting the markup rather than by inferring it
from which cards happen to be present.

---

## 4. Claims: supported, partial, withheld

The display policy is P1's and is unchanged
(`synapse.answer.policy.DisplayPolicy`):

* **`SUPPORTED`** — displayed.
* **`PARTIALLY_SUPPORTED`** — displayed **and labelled** `(partly verified)`,
  because `treat_partial_as_supported` is `True`. It has real evidence behind
  it; the label says the verification was incomplete. It is never promoted to
  "supported" and never rendered unmarked — the label appears on the page, in
  the evidence cards and in the exported brief.
* **`UNSUPPORTED`** — **removed from the answer entirely** by
  `policy.apply`, before any renderer sees it. Not greyed out, not footnoted.
  A patient has no way to weigh a caveat about groundedness, so the only safe
  treatment is absence.

Withholding happens once, in the policy layer, rather than in each renderer.
That is a stronger guarantee than asking every surface to remember to filter —
and the export path, the obvious place for a withheld claim to survive, is
covered by an explicit test.

If too little survives (`min_supported_ratio`, `min_supported_claims`), the turn
abstains rather than showing a shorter answer that reads as a complete one.

---

## 5. Action states and the disclaimer

Four states render explicitly:

| State | What the patient sees |
| --- | --- |
| `answer` | Summary, claims, evaluation, questions, limitations, sources, excerpts. |
| `abstain` | A card saying no answer was given, why, and the questions — which are still worth asking. No claims. |
| `medical_staff` | A fixed routing message, then whatever else survived. |
| `emergency` | The escalation card **only**. No claims, no sources, no brief. Decided before retrieval; generation never runs. |

Plus one non-answer state: `error` (§8).

**The disclaimer is application-owned.** `PERMANENT_DISCLAIMER` is a module
constant in `synapse.answer.render`, rendered on every one of those five states
including failures. The model's own `disclaimer` field is parsed and validated
but deliberately **never displayed** — a truncated or reworded generation cannot
weaken it, and a model cannot omit it. This is asserted for every state in
`tests/test_ui_rendering.py::TestDisclaimerIsApplicationOwned`.

---

## 6. Identifier stability

One `SourceNumbering`, built once from retrieval order, drives four surfaces:

1. inline citation markers on claims,
2. the source panel,
3. the evidence cards,
4. the exported appointment brief.

`claim_id` travels with it, as `data-claim-id` in the markup and as the leading
token of each excerpt line in the brief. So `[2]` on screen is `[2]` in the
brief, and `c1` in a support conversation is the same `c1` in the audit record.

Two fail-closed rules:

* A source that was not retrieved is **never numbered**, so a fabricated
  citation cannot appear in the panel as though it had been used.
* Withholding a claim does not renumber anything. The patient's `[2]` stays
  `[2]`.

The legacy UI numbered its panel independently of the `[Source N]` markers the
model emitted, so the two could disagree with nothing able to detect it.

---

## 7. The legacy adapter — deprecated, dated

`synapse.ui.legacy_evidence` converts the legacy
`{"chunk": Chunk, "rank": …, "relevance_score": …}` shape into typed
`RetrievedEvidence`. It exists because `Retrieval/` and `Data/` have not been
migrated yet, and it is confined to that one job.

**Removal deadline: 2026-11-30.** Enforced two ways:

* every call emits a `DeprecationWarning` naming the date;
* `tests/test_ui_pipeline.py::TestLegacyAdapterIsQuarantined::test_the_removal_deadline_has_not_passed`
  fails the build once the date passes.

A further test asserts that only `synapse.ui` imports it, so a new caller
elsewhere fails CI rather than quietly extending its life.

**What happens at the deadline:** `Retrieval/` emits `EvidenceChunk` records
with real identifiers (`synapse.schemas.chunk`), the adapter is deleted, and
`synapse.ui.pipeline` consumes typed records directly. Until then, conversion
fails closed: a chunk with no derivable identifier, or a duplicate identifier
from the legacy `f"{pmid}_chunk{index}"` collisions (162 in the committed
corpus — docs/quality-architecture.md §1.2, C6/C7), is **dropped**. Dropped
evidence is never shown to the model and never verifies a citation, so the
claim that depended on it is withheld.

Also removed in this change, rather than deprecated: the free-form generator in
`Generation/answer_generator.py`. Keeping it would have meant two answer
pipelines, one of them unverified. Only `check_emergency` and
`EMERGENCY_RESPONSE` remain there, and both are thin delegations.

---

## 8. Failures

A failure is not an answer. When anything in the path fails, the patient gets:

* the **same generic message** whatever went wrong
  (`synapse.ui.errors.PATIENT_ERROR_MESSAGE`),
* a **typed reference code** from a closed enum,
* the permanent disclaimer,
* and **no medical content of any kind**.

| Code | Meaning |
| --- | --- |
| `generation_unavailable` | Provider unreachable, refused, empty, or truncated by the output limit. |
| `generation_invalid_json` | A response arrived that was not valid JSON. |
| `generation_schema_invalid` | Valid JSON that does not satisfy the answer schema. |
| `evidence_unavailable` | Retrieval produced nothing usable to ground an answer in. |
| `retrieval_failed` | The retrieval stage itself raised. |
| `configuration_error` | The deployment is missing something it needs. |
| `internal_error` | Anything else, including a fault in the red-flag detector. |

No provider exception text is ever rendered or logged: third-party messages can
carry a request id, a URL, or an echoed prompt fragment, so an unknown exception
is reduced to its type name. Synapse's own errors are already payload-free by
construction (`synapse.errors`).

**`generation_schema_invalid` is the one that matters.** It is precisely the
case the old implementation handled by displaying the text anyway. There is now
no code path that does so, and
`tests/test_ui_pipeline.py::TestMalformedOutputNeverFallsBack` asserts that the
model's prose — including emoji headings and a dose instruction — appears
nowhere in what is rendered.

A fault in the emergency detector also fails closed: no answer is produced, and
the failure card tells the patient to speak to staff.

---

## 9. Escaping

Every dynamic value is escaped with `html.escape(..., quote=True)` regardless of
origin — patient, model, or source. The renderer does not distinguish between
them, because trusting any category is how the next hole opens.

The category worth naming is **source-controlled text**: PubMed titles and
abstracts are third-party content fetched from an external feed, so an attacker
can influence them without ever touching this application. Titles, quotes and
identifiers all go through the same escape.

URLs are treated separately: a citation URL is rendered only when its scheme is
`http` or `https`. Escaping `javascript:` still yields a working link, so a
non-http scheme is dropped entirely — on the page and in the exported brief.

---

## 10. Tests

| File | Covers |
| --- | --- |
| `tests/test_ui_rendering.py` | Typed-field rendering, no regex parsing in production, withheld claims on every surface, partial labelling, the disclaimer in every state, action states, escaping, numbering stability. |
| `tests/test_ui_pipeline.py` | The path end to end offline: fail-closed retrieval, malformed output, provider outage, emergency short-circuit, the deprecated adapter, the strict provider schema, the brief. |
| `tests/test_answer_snapshots.py` | Patient-facing text pinned, including the appointment brief for all four action states. |
| `tests/test_answer_adversarial.py` | P1 citation-integrity guarantees, unchanged. |

Regenerate snapshots deliberately with `SYNAPSE_UPDATE_SNAPSHOTS=1 pytest`, then
read the diff. These are the words a patient reads.

# Changelog

Notable changes to Synapse. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is pre-1.0. Nothing here is clinically validated, and no version
number implies fitness for clinical use.

---

## [Unreleased]

### Added

- **The production interface now renders validated structured answers.** The
  answer layer landed in the previous change, but `app.py` never used it: it
  still imported `Generation.answer_generator.AnswerGenerator`, which asked a
  model for six emoji-headed sections and returned prose. The grounded branch in
  the renderer existed and nothing populated it, so every real answer took the
  fallback — escaped, but unverified, unwithheld, and with source numbers that
  resolved to nothing. There is now exactly one path from a query to a screen,
  and it runs through verification.
- `synapse/ui/` — the seam between Streamlit and the answer layer.
  `pipeline.py` owns the ordering (red-flag check → retrieve → typed evidence →
  schema-constrained generation → verify → decide → render) and returns a
  `TurnOutcome` carrying **either** an answer **or** a typed failure, never
  both. `errors.py` defines the closed `AnswerFailureCode` set and the single
  generic patient message. Retrieval and generation are injected, so every
  branch — including each failure mode — is tested offline.
- `synapse/answer/providers.py` — `OpenAIStructuredClient`, the only module that
  names a provider SDK. It hands the model a JSON Schema derived from
  `GroundedAnswer` itself and tightened for strict decoding, so the contract the
  model is given and the contract the code enforces cannot drift. A truncated
  response is a named failure rather than silently-lost sections; the previous
  800-token cap against a six-part structure is what used to drop the
  disclaimer.
- `synapse/answer/brief.py` — the exported appointment brief. Rendered from the
  same answer and the same `SourceNumbering` as the page, so `[2]` in the brief
  is `[2]` on screen, and `claim_id` is stable across the page, the evidence
  cards, the export and the audit record.
- Evidence cards (`render_evidence_html`) showing the verbatim excerpt behind
  each displayed claim, and a failure card (`render_failure_html`) that renders
  a fixed message, a typed code and the permanent disclaimer — with no parameter
  through which model output could reach it.
- **WCAG 2.2 AA accessibility work across all eleven interface states.** An
  audit came first and is recorded in `docs/accessibility.md` §1; the fixes
  follow from it. **No formal conformance is claimed**, and the document says so
  in its first paragraph.
- **The heartbeat audio is gone.** The audit established it never played:
  Streamlit renders markdown through `dangerouslySetInnerHTML`, and a `<script>`
  inserted through `innerHTML` does not execute. That was verified in a real
  browser rather than assumed, and the verification is now a test. It was
  deleted anyway, because the intent was in the source and a custom component
  would have made it work. There is now no audio in Synapse at all.
- **Eight contrast failures fixed**, including a card border at 1.17:1 and a
  chunk identifier at 2.30:1. The palette now lives in `synapse/a11y/palette.py`
  as tokens that each declare the surface they sit on, and `css_variables()`
  generates the `:root` block the app uses, so the stylesheet and the test read
  the same numbers. The first replacement for the card border still measured
  1.80:1, which is why the table is checked rather than eyeballed.
- **A real mobile defect:** a collapsed Streamlit sidebar is moved off-screen
  with a transform but keeps `visibility: visible`, so its API key field,
  checkbox and buttons stayed focusable at `x = -305`. Eight invisible tab stops
  on a phone. Fixed with a visibility rule keyed on the `aria-expanded`
  attribute Streamlit already sets.
- **Placeholder-as-label removed.** The question field had
  `label_visibility="collapsed"`, and a global rule hid every Streamlit form
  label with `display: none`. Both gone; the field now has a persistent visible
  label and the placeholder is an example.
- Real headings instead of styled `div`s, a skip link, `aria-hidden` on
  decoration, polite live regions for loading and completion, and `role="alert"`
  reserved for emergency routing alone.
- Streamlit's `#MainMenu`, header and toolbar are no longer `display: none`;
  only the promotional footer stays hidden (requirement 24).
- Reduced motion honoured through the OS preference **and** an in-app setting.
  The decorative streaks moved from inline styles to a class, because an inline
  animation cannot be overridden by a media query.
- `synapse/a11y/` — contrast arithmetic verified against the WCAG reference
  values, the palette, and a browser-free markup auditor.
- `tests/test_accessibility.py` (72 offline checks, gates every PR) and
  `tests/test_accessibility_browser.py` (58 Playwright + axe-core checks, which
  skip cleanly without Chrome). axe ships inside `axe-core-python`, so no CDN is
  contacted and CI stays offline.
- **Seven known limitations documented with a remediation priority**, the top
  three being screen reader testing, focus restoration and live-region
  reliability across Streamlit reruns. The last two share a cause and a fix.
- **Privacy-preserving appointment brief** (`synapse/brief/`). A one-page
  artifact a patient can review, edit, print, download and keep. Built from a
  dedicated `AppointmentBrief` schema rather than by scraping rendered HTML, so
  the distinction between a verified claim and a typed sentence survives into
  the document.
- **The schema enforces the invariants rather than documenting them.** An
  unsupported claim cannot be in a brief (rejected at construction, and dropped
  by the builder before that); every citation marker must resolve to a listed
  source; a user-authored sentence cannot be a claim; the document identifier
  must be 12 random hex characters; and the disclaimer is not a member of the
  section enum, so there is no way to omit it.
- **Editing a claim is atomic.** `edit_claim_text` replaces the text, sets the
  origin to `user_edited` and strips the citation markers in one operation,
  because any subset of those produces a document that misrepresents itself. The
  markers move to the original wording, which is printed beside the edit. User
  content is distinguished by a tinted panel **and** a text label, so it survives
  a black-and-white photocopy.
- **No server-side persistence, and none existed to audit.** Export produces
  bytes the browser downloads: self-contained printable HTML, plain text, and
  round-tripping JSON. No PDF library ships (the print stylesheet drives the
  browser's own PDF writer), no share URL, no upload, no third-party delivery.
  Export requires an explicit click and shows the privacy warning above the
  buttons.
- **Exports reach the network never**: no font import, no image, no script, one
  inline stylesheet, system fonts. A remote asset in a saved health document
  phones a third party every time it is opened.
- **One page, or an explicit choice.** `estimate_fit` measures and
  `overflow_advice` suggests what to cut; the renderer never shrinks type below
  the accessible floor. Sources and the disclaimer carry `break-inside: avoid`,
  and long questions wrap rather than truncating.
- **Two defects found by rendering the document and looking at it:** editing a
  claim orphaned its source in the list (the markers now travel with the wording
  they describe), and the export scanner's chunk-identifier pattern matched CSS
  hex colours, blocking every export until it was anchored on the real grammar.
- `synapse/answer/brief.py` is now a deprecated shim delegating to the new
  package, so there is one text formatter rather than two. Two deliberate format
  changes are recorded in docs/appointment-brief.md §9.
- `docs/appointment-brief.md` with a manual print/PDF QA checklist, and six
  committed print snapshots at A4 and Letter.
- `tests/test_appointment_brief.py`, `tests/test_brief_print_snapshots.py` (87
  tests).
- **Patient-facing evidence experience** (`synapse/evidence/`). Every displayed
  claim now carries a citation control that reveals the exact verified excerpt
  alongside the source's publication and governance metadata: title, authors,
  journal or issuing organisation, publication and revision dates, PMID/DOI/
  guideline identifier, canonical link, evidence type, review state and
  source-pack version.
- **Nothing on an evidence card is invented.** `SourceMetadata` copies fields off
  a governed `PackSource` record and does nothing else; a field the record lacks
  renders as absent rather than as "Unknown", which reads as a fact about the
  source. A source the pack does not describe is labelled "No governance record"
  rather than being given a plausible one. Excerpts come from P1-validated
  evidence and are never regenerated.
- **Relevance is separated from quality.** A reranker figure is labelled
  "retrieval relevance", never "confidence", rendered away from the governance
  labels with the caveat that it says nothing about how reliable the research is.
  Evidence-quality and review labels come from governed metadata alone. The
  shipped example pack is entirely unreviewed, and every card says so: **"Not
  reviewed by a clinician — this source was found automatically."**
- **Insufficient evidence is a first-class state**, covering all six causes
  (nothing retrieved, validation failed, below threshold, conflicting sources,
  out of scope, index unverified). The copy is composed from module constants
  with no parameter through which generated text can enter, always carries "this
  is about Synapse's sources, not about you… it does not rule anything in or
  out", offers neutral next steps, keeps the permanent disclaimer, and is
  deliberately not styled as an error.
- **Accessible by construction.** The control is a native `<details>`/`<summary>`:
  keyboard operable, exposes its expanded state without an `aria-expanded`
  anyone can forget, and needs no JavaScript. Mobile-first at 320px, 44px touch
  targets, `:focus-visible` outlines, no hover-only reveals, wrapping rather than
  truncation, `role="status"` on the insufficient state, and
  `prefers-reduced-motion` honoured. Writing the snapshots caught a real defect:
  the hidden summary text repeated numbers the visible marker already carried,
  so a screen reader announced "[1]" twice.
- **External links are https-only**, with `noopener noreferrer nofollow`,
  validated DOI and PMID grammars, and a rejected URL **dropped** rather than
  escaped — escaping `javascript:` still yields a working link. A malformed
  identifier produces no link but is still shown as recorded metadata.
- `synapse/escaping.py` — the escaping primitive extracted so two renderers can
  share it without a circular import or a second implementation.
- `docs/evidence-ux.md` and `docs/snapshots/` — 15 committed HTML snapshots
  covering success, expanded evidence, partial support, all six insufficient
  reasons, abstain, emergency, error, empty, loading and the review view, each
  carrying the production stylesheet and pinned to 320px.
- `tests/test_evidence_ux.py`, `tests/test_evidence_snapshots.py` (132 tests).
  Includes an assertion that rendering every card in the pack leaves the pack
  file byte-identical: reading must not write.
- **Privacy-safe runtime observability** (`synapse/telemetry/`). Records
  latency, versions, token usage, outcomes and typed failure codes; records
  nothing about what patients asked or were told. The threat analysis was
  written first (docs/privacy-logging-policy.md, Part I) and covers all eleven
  data categories — questions, conversation history, answers, excerpts, IP
  addresses, browser identifiers, API keys, provider responses, stack traces,
  feedback and exported briefs — with a decision and an enforcement mechanism
  for each.
- **The schema is the allowlist.** `TelemetryEvent` inherits `extra="forbid"`,
  so an unknown field is a validation error and the event is dropped and
  counted, never repaired. There is **no field** for a query, a query digest, an
  answer, an excerpt, a chunk identifier, an address, a user agent, a header, an
  exception message or a traceback — including, deliberately, **no salted
  digest**: health questions come from a small repetitive space where a
  candidate list recovers most of them by lookup.
- **Secret redaction, fuzz-tested.** Applied to every string on the way into an
  event, not only to fields believed risky. A 10,000-case generative fuzz found
  two real defects in the first implementation: `\b` anchors let a key
  concatenated to preceding text escape entirely (59% survival), and the
  `authorization` pattern consumed only the scheme word — leaving the token and
  destroying the keyword a later pattern needed. Both fixed; survival is now 0.
  Exceptions have their **message discarded rather than redacted**, because no
  pattern list recognises a health question.
- **Metrics with honest quantiles.** Cumulative-bucket histograms for retrieval,
  rerank, generation and end-to-end latency; `quantile_bounds()` returns the
  interval a quantile falls in and `quantile()` its upper bound. A bounded
  reservoir of raw samples exists to check the estimate, and the tests assert
  the interval brackets the exact value. No mean latency is reported anywhere.
- **Versioned cost estimation**, reusing the P1 pricing table rather than adding
  a second one. Every event carries `pricing_version`, and `cost_is_estimate`
  is a field that cannot be set False.
- **Off by default**, with `NullSink`; enabling it without naming a destination
  writes local-only JSONL with a banner and a tested `prune()` deletion path. No
  analytics vendor is configured. Session identifiers are random, server-side
  and rotate on a fixed TTL, so they cannot become longitudinal.
- `docs/observability.md` — event shape, metric list, jq and PromQL dashboard
  queries, and a runbook for investigating failures **without** raw health text.
- `tests/test_telemetry_privacy.py`, `tests/test_telemetry_metrics.py` (87
  tests, all offline).
- **Bounded candidate retrieval and batched reranking** (`synapse/retrieval/`).
  The online path no longer scales with the corpus. Dense and sparse retrieval
  are each queried for a configurable `top_n`, unioned on stable chunk
  identifiers, deduplicated, fused (RRF by default, linear available) with ties
  broken on the identifier, filtered by source-pack eligibility, and truncated —
  with every size recorded in the run metadata. A request for `top_n >=
  corpus_size` now raises `UnboundedRetrievalError` instead of being served:
  the previous `linear` path asked FAISS for `top_k=len(chunks)` and BM25 for a
  score per chunk on **every** query, and rebuilt the alignment through the
  colliding legacy identifier (162 collisions in the committed corpus).
- **One reranking request instead of N** (`synapse.retrieval.rerank`). The whole
  candidate set goes in one structured request that returns
  `chunk_id → {relevance, rank, rationale}`. Unknown identifiers, duplicates,
  missing candidates and out-of-range scores are each a rejection, never a
  repair; matching is by identifier, never by position. Explicit connect and
  read timeouts, a total operation deadline, bounded retries with exponential
  backoff and full jitter, transient-only retry, and a **single** schema retry.
  On failure it returns the deterministic fused order marked `degraded` —
  replacing a bare `except Exception` that substituted `retrieval_score * 10`
  and made an outage indistinguishable from a successful rerank.
- **Measured, not asserted.** `synapse/bench/` and
  `python -m synapse.cli.benchmark` run both algorithms against the same corpus,
  backends and fake provider. On a 2,220-chunk synthetic corpus over 24 queries:
  reranker calls 240 → 24 (**10×**), modelled rerank latency 436.8 ms → 44.4 ms,
  measured retrieval latency 24.1 ms → 12.9 ms (**1.87×**), estimated prompt
  tokens 21,158 → 13,697, corpus fraction materialised 1.000 → 0.038. A third
  arm holds fusion constant to isolate bounding from the fusion change: bounding
  alone costs 0.003–0.021 on recall and nDCG, and agrees with full-corpus fusion
  on 0.892 mean set overlap and 20 of 24 identical top results. The headline
  quality gain comes from RRF, not from bounding, and the docs say so.
  Full results: `artifacts/benchmarks/retrieval-2026-08-20/results.json`.
- **A fail-closed index gate** (`synapse.retrieval.index_gate`). P1 already knew
  how to verify an index; nothing called it on the serving path. Verification
  failure now raises, retrieval never runs, and the failure is classified as the
  new `index_unverified` code. An index with no manifest is reported as
  `unmanaged` rather than treated as verified.
- `docs/retrieval-runtime.md` — what was replaced, the measured before/after, the
  candidate-size sweep behind the defaults, benchmark methodology with an
  honesty note per number, the four-rung degradation ladder, and dated gaps.
- `tests/test_retrieval_candidates.py`, `tests/test_retrieval_rerank.py`,
  `tests/test_retrieval_pipeline.py`, `tests/test_retrieval_benchmark.py` (89
  tests, all offline). Includes a fidelity test that runs the benchmark's
  baseline against the real `Retrieval.hybrid_retriever.linear_fusion` and
  requires identical output, so the "before" number cannot drift into fiction.
- **Schema-strictness fix, found by running the app.** Every generation failed
  with `generation_unavailable`: the provider rejected the request with a 400,
  `$ref cannot have keywords {'description'}`. Pydantic emits an enum-typed
  field carrying a docstring as `{"$ref": …, "description": …, "default": …}`,
  and strict decoding forbids any sibling beside a `$ref`. `strict_schema` now
  reduces such a node to the `$ref` alone, with two offline regression tests —
  no `$ref` carries siblings, and every `$ref` still resolves.
- **Provider failures now record their HTTP status.** A status is an integer, so
  it cannot carry a request id, a URL or a key fragment, and 400/401/429/5xx
  each call for a different operator response. `app.py` also calls
  `configure_logging()` at startup: without it the structured fields on every
  warning were discarded, leaving an operator with only the coarse reference
  code shown on the failure card — which is what made the 400 above take a live
  request to identify.
- `docs/answer-rendering.md` — what is rendered field by field, the partial-claim
  policy, action states, identifier stability, the failure taxonomy, and the
  deprecated adapter's removal deadline.
- `tests/test_ui_rendering.py` and `tests/test_ui_pipeline.py` (89 tests), plus
  appointment-brief snapshots for all four action states. Among them: the
  model's prose never reaches the patient when validation fails, a withheld
  claim is absent from the export as well as the page, and the disclaimer is
  present in every state including failure.
- `LICENSE` — MIT, matching the README's long-standing claim, which until now
  had no corresponding file. Carries an additional medical-context notice.
- `SECURITY.md` — private reporting process, scope, the security invariants that
  have tests, and an explicit list of unhardened areas.
- `docs/SYSTEM_CARD.md` — intended use, excluded uses, source boundaries, model
  and index versions, evaluation coverage, failure modes, safety mechanisms,
  privacy assumptions.
- `docs/VALIDATION.md` — what has been verified, by what method, and what has
  not been validated at all.
- `docs/SAFETY_CASE.md` — the explicit safety argument, including the claims it
  cannot currently support.
- `docs/PRIVACY_DATA_FLOW.md` — every destination patient query text reaches.
- `docs/LIMITATIONS.md` — consolidated known limitations.
- `docs/SOURCE_GOVERNANCE.md` — the governance policy, above the existing
  implementation reference in `docs/source-governance.md`.
- `docs/investor-technical-evidence.md` — factual technical evidence summary.
- `docs/demo-evaluation-report.md` — a committed, reproducible engineering
  demonstration report, labelled non-clinical and unreviewed.

### Changed

- **`app.py` is presentation only.** It no longer decides which sections to
  show, what to do when generation fails, or how to number sources; it supplies
  the three injection points and renders typed fields. `import re` is gone from
  the file entirely, and a test asserts it stays gone.
- Answers now carry an explicit action state in the markup
  (`data-action="answer|abstain|medical_staff|emergency"`), with a visible card
  for abstention rather than a page that is merely missing its claims.
- A partially-supported claim is labelled `(partly verified)` on every surface —
  page, evidence cards and exported brief — following the existing P1 display
  policy (`treat_partial_as_supported`) rather than a new one.
- **README rewritten.** The previous version documented a project structure that
  did not exist (`medrag/`, `retriever/`, `processing/`, `reranker/`,
  `main.py`), gave an install command for a placeholder repository URL, and told
  users to run `python main.py` — a file that has never existed. Every command
  in the new README was executed from a clean Python 3.11 environment.
- **The "offline mode" claim was removed as false.** The README stated that
  "FAISS-based vector search works without API access" and that without a key
  "the system will run in offline mode using only local retrieval".
  `Retrieval/vector_store.py` embeds both corpus and query with OpenAI
  `text-embedding-3-small`, so without a key there are no query vectors and the
  vector path cannot execute. Only BM25 works without a key, and the README now
  says exactly that in a table.
- **"Health-tech startup MVP (e.g., symptom checker)" removed from use cases.**
  Symptom checking is an *excluded* use; advertising it contradicted the safety
  design and is now listed under excluded uses in the system card.
- `app.py` — the evaluation-metrics panel caught `except Exception` and rendered
  "Metrics appear after first query." Both halves were wrong: the catch hid
  every failure mode identically, and metrics never appear from a query — they
  come from an offline run against a fixed dataset. Narrowed to `OSError` and
  `JSONDecodeError`, with an honest message naming the error class.
- `docs/quality-architecture.md` — banner added marking it a historical planning
  document. It is written in the future tense about work that is now done, and
  was being read as a description of the current system.
- `pyproject.toml` — `pythonpath = ["."]` added to the pytest configuration.

### Removed

- **The free-form answer generator** (`Generation/answer_generator.py`:
  `SYSTEM_PROMPT` with its `📋 WHAT THE RESEARCH SAYS` / `🔬 WHAT YOUR DOCTOR
  WILL EVALUATE` / `❓ QUESTIONS TO ASK YOUR DOCTOR` format, `USER_PROMPT_TEMPLATE`
  with its unresolvable `[Source N]` markers, `AnswerGenerator` and
  `run_pipeline`). Deleting it rather than deprecating it in place was the
  point: leaving it alongside `synapse.answer` would have meant two answer
  pipelines, one of which nothing verifies. `check_emergency` and
  `EMERGENCY_RESPONSE` remain, both thin delegations to `synapse.safety`.

- **Five unused dependencies**, verified by an AST sweep of every `.py` file
  rather than by grep: `tiktoken`, `plotly`, `langchain`, `langchain-core`,
  `pypdf`. None was imported anywhere.
- **`Data.fetch_and_chunk.chunks_from_pdf`** — dead code that could never have
  executed. Its first statement imported `langchain_community`, a package never
  declared in `requirements.txt` and never installed, so every call raised
  `ModuleNotFoundError`. `pypdf` was pinned solely to support it. `build_corpus`
  now raises `NotImplementedError` if `pdf_paths` is passed, rather than
  silently returning a PubMed-only corpus.

### Security

- **Emergency detector rewritten; the P0 safety defect is closed.** The old
  `check_emergency` substring-matched 24 fixed phrases and scored **2/6** on real
  emergencies — missing stroke, respiratory distress, haemoptysis and overdose —
  while escalating denials such as "i do not have any chest pain" (2/6 on
  negation).

  Replaced by a new `synapse.safety` package: stem-proximity matching over a
  governed vocabulary (`config/emergency_vocabulary.toml`, 11 concepts, 120+
  patterns) with negation scoping that ends at clause boundaries, so
  "no chest pain **but** my face has drooped" still escalates. Self-harm is
  exempt from negation, because "i don't want to live" is a disclosure.

  | | Old | New |
  |---|---|---|
  | Repository emergency cases | 2/6 | **6/6** |
  | Repository negated cases | 2/6 | **6/6** |
  | Held-out emergencies (15) | — | **15/15** |
  | Held-out ordinary queries (9) | — | **0 false positives** |

  Held-out testing found three real gaps, including *"i don't want to live
  anymore"* — `"dont"` does not prefix-match the stem `"not"`, so the first draft
  missed it entirely.

  Detection moved out of the legacy tree, so the code a patient's query touches
  is now typed, linted and tested rather than excluded from all three.
  `Generation.answer_generator.check_emergency` delegates, so `app.py` is
  unchanged. `negation_accuracy` and the negated-emergency category gate were
  **promoted from informational to blocking**, the stated condition for
  promotion having been met. Baseline corrected `0.2.0` → `0.3.0`. 32 new tests.

  **The vocabulary is engineering-authored and unreviewed.** Better recall on
  21 self-authored phrasings is not clinical evidence; `reviewed_by` is empty and
  the loader refuses to report the vocabulary as approved while it is.

### Discovered

- **The emergency detector misses two thirds of real emergencies.** Measured at
  **33% sensitivity** (2 of 6): it does not escalate stroke ("speech is slurred
  … face has dropped"), respiratory distress ("struggling to breathe … lips look
  blue"), haemoptysis ("coughing **up** blood" vs the listed "coughing blood"),
  or overdose ("took far too many" vs "took too much"). Negation accuracy is
  also 2 of 6. CI reports **100% for both**, because the evaluation fixture
  records hand-authored ideal responses rather than real system output — so the
  safety metrics never touch the real detector.

  Prior documentation (H4) covered only the negation half, which is the *safer*
  direction. The false-negative half is new and strictly worse.

  Documented rather than patched: repairing it requires a clinically-reviewed
  symptom vocabulary, which nobody in this repository is qualified to author.
  Both directions are now pinned by
  `tests/test_emergency_detector_known_defects.py` (11 tests), which asserts the
  broken behaviour so it cannot drift and fails once the detector is fixed.
  Recorded in `docs/demo-evaluation-report.md` §2,
  `docs/SAFETY_CASE.md` Claim 1 (downgraded to NOT SUPPORTED), and
  `docs/LIMITATIONS.md` §1.4.

### Fixed

- **The evaluation can now see the real emergency detector.** Safety metrics
  previously scored `evals/ci/system_responses.jsonl`, which stores
  hand-authored *ideal* responses — so `emergency_sensitivity` and
  `negation_accuracy` both reported 100% while the shipped detector scored 2/6.
  The metric never touched the detector.

  Emergency routing is the one stage needing no model and no network, so
  `RealEmergencyRoutingSystem` (`synapse/evals/system.py`) now recomputes the
  escalation decision from the shipped code and the harness reports the honest
  **33.3% / 33.3%**, with specificity dropping 100% → **90.5%**. Still fully
  offline, deterministic, and free; on by default, with
  `--no-real-emergency-detector` to reproduce a historical run.

  The bridge (`synapse/evals/legacy_detector.py`) reads `EMERGENCY_SIGNALS` with
  `ast.literal_eval` and reimplements the two-line match. An earlier draft
  compiled the AST nodes and was correctly rejected by the repository's no-`exec`
  guard test; the rule was fixed rather than exempted. A shape check fails
  loudly if `check_emergency` ever stops being a lowercased substring match, and
  a test proves the bridge agrees with the real function on every CI query.

  The approved baseline was corrected `0.1.0` → `0.2.0` to record the true
  numbers. Retrieval, citation and answer metrics still replay the fixture —
  those genuinely need a model. 8 new tests.

- **CI would have failed on its first GitHub run.** `ci.yml` invokes bare
  `pytest`; local simulation had used `python -m pytest`, which injects the
  working directory into `sys.path` and masked the difference. Bare `pytest`
  failed collection with `ModuleNotFoundError: No module named 'tests'`.
- **45 dependency vulnerabilities**, remediated at the root rather than by
  version-chasing. Removing the five unused packages eliminated 41 of them
  outright. The remainder: `setuptools` floor raised to `>=83`
  (`PYSEC-2026-3447`) and `pip` upgraded. `pip-audit` now exits clean with no
  ignore-list — an audit gate with an ignore-list is an audit gate switched off.

### Security

- Every GitHub Action pinned by commit SHA rather than mutable tag.
- `ci.yml` references no secrets and declares `permissions: contents: read`; a
  step fails the build if an API key is present in the environment.

---

## [0.1.0] — 2026-08

The quality and evidence layer. Built as eight sequential changes.

### Added

- **Data and artifact layer.** 11 typed pydantic models (`extra="forbid"`,
  `frozen=True`), JSONL and Parquet replacing pickle, FAISS index manifests with
  hash validation before load, a one-way migration CLI, and a restricted
  unpickler with an exact `(module, name)` allow-list quarantined in
  `synapse/_legacy/`. Content-addressed digests bind FAISS row *i* to corpus
  record *i* so a reordered index cannot silently mis-cite.
- **PubMed ingestion.** All `AbstractText` elements with section labels, inline
  markup, publication dates, collective authors, retraction status, a normalised
  evidence-level enum, three-stage deduplication, retry with backoff and pacing,
  structured logging, section-aware chunking, and offline XML fixtures.
- **Source-pack governance.** A lifecycle state machine
  (discovered → screened → approved / rejected / expired / superseded) that
  distinguishes automated transitions from human review, with pseudonymous
  reviewer identifiers and enforcement that no source is usable without an
  approval record.
- **Evaluation dataset framework.** A 15-element case schema, versioned
  manifests, a labelling CLI, spreadsheet import/export, inter-annotator
  agreement, leakage detection, and splits built to exclude near-duplicates.
- **Evaluation harness.** Deterministic metrics strictly separated from
  LLM-judge metrics, Wilson intervals for proportions, seeded bootstrap for
  means, `None` rather than `0.0` for undefined metrics, and fully reproducible
  offline replay.
- **Citation integrity.** A validated structured response replacing emoji-heading
  parsing, verbatim-excerpt verification against the corpus, a lexical support
  checker, abstention when support is absent, and a hard rule that unsupported
  claims are never displayed.
- **CI and release quality gates.** Eight jobs, blocking and informational gate
  modes, baseline comparison that fails closed on an incomparable baseline, and
  a Markdown job summary that places caveats above numbers.

### Changed

- **"Confidence" renamed to "relevance score" throughout.** The patient-facing
  number was `int(rerank_score * 10)` — an LLM's self-reported usefulness rating
  — rendered as a progress bar labelled "% confidence" beside a PubMed citation,
  where a patient reads it as confidence in the medical content. It is
  uncalibrated against anything clinical.
- The previous evaluator was retired. It scored live user queries against empty
  ground truth, returned `0.0` for undefined metrics, and its import path did
  not resolve on a case-sensitive filesystem, so it had never run in CI.
- Import-time `print("WORKS")` statements removed from the ingestion module.

### Known limitations at 0.1.0

- Zero approved sources. The one source pack holds 4 sources: 3 `discovered`,
  1 `superseded`, 0 `approved`.
- Zero reviewed evaluation cases. All 48 are synthetic; 0 are release-gating
  eligible.
- All quality-gate thresholds are provisional with an empty `approved_by`.
- The red-flag detector substring-matches, so *"I do not have chest pain"*
  escalates. Tracked; the negation metric is deliberately informational until it
  is fixed.

# Synapse Master Study Guide

This is the repository-specific guide for becoming fluent in Synapse: able to
explain it at a whiteboard, change it safely, defend its design in an interview,
and discuss it honestly with accelerators or investors.

It is based on the code, tests, configuration, committed artifacts, and project
documentation as inspected on **2026-08-20**. It is not a medical or regulatory
assessment.

---

## 1. The answer you should be able to give in 30 seconds

Synapse is a pre-clinical medical appointment-preparation RAG prototype. The
legacy Streamlit app takes a health question, retrieves PubMed abstract chunks
with dense and BM25 search, reranks them, and asks an LLM to draft educational
context and questions for a clinician. Around that prototype, the repository
has a much more mature `synapse` package that makes data, source governance,
evaluation, citations, and release decisions auditable and fail-closed.

The key architectural fact is that these two layers are not fully integrated:
the safe, structured answer pipeline is built and tested, but the live app still
calls the legacy free-form generator. The project therefore has a strong trust
and evaluation substrate, but it does not yet have evidence that the patient
product is safe or effective.

Memorize this distinction:

> **The engineered asset is the evidence and quality infrastructure. The live
> product remains an unvalidated prototype.**

---

## 2. Verified project snapshot

These are facts you can state and reproduce locally.

| Item | Verified state |
|---|---|
| Package | `synapse` 0.1.0, Python 3.11–3.12 |
| Engineered package modules | 74 Python modules |
| Tests | 766 passing in 3.20 seconds locally |
| Test layout | 19 `test_*.py` modules plus `conftest.py` |
| Formatting | 93 files pass `ruff format --check` |
| Lint | `ruff check .` passes |
| Types | mypy passes over 38 scoped source files |
| CI evaluation cases | 48, all synthetic |
| Clinician-reviewed evaluation cases | 0 |
| Release-gating evaluation cases | 0 |
| Example source-pack sources | 4 synthetic examples |
| Approved sources | 0 |
| Legacy corpus | 2,220 PubMed chunks |
| Unique legacy chunk IDs | 2,058 |
| Legacy ID collisions | 162 |
| Duplicate text instances | 163 |
| FAISS vectors | 2,220 × 1,536, `float32` |
| Gate status | Passes against baseline, but configuration is provisional |
| Patient readiness | Not validated; must not be used clinically |

The 100% values in the committed demonstration evaluation are not product
performance. The fixture contains hand-authored ideal responses, so it proves
that the evaluator scores ideal fixtures as ideal. It does not execute the live
RAG system.

---

## 3. The whole architecture in one picture

```text
LEGACY LIVE APPLICATION

user query
   |
   +--> substring emergency detector ------------------> fixed warning
   |
   +--> OpenAI query embedding
   |       |
   |       +--> FAISS dense retrieval --+
   |                                     +--> score fusion --> top 10
   +------------------> BM25 retrieval --+
                                           |
                                           +--> LLM reranking, one call/candidate
                                                      |
                                                      +--> free-form LLM answer
                                                                 |
                                                                 +--> escaped fallback UI


ENGINEERED TRUST AND QUALITY LAYER

PubMed --> parse --> dedupe --> screen --> section-aware chunk --> JSONL
                                                            |
                                                            +--> manifest + hashes
                                                                    |
                                                                    +--> verify before load

source documents --> discovered --> human review --> approved/other state
                                                    |
                                                    +--> eligibility filter
                                                            |
                                                            +--> governed index manifest

retrieved evidence --> structured generation --> excerpt verification
                                               --> policy decision
                                               --> unsupported claim removal
                                               --> escaped rendering

versioned eval cases --> fixture/live adapter --> deterministic metrics
                                             --> category breakdown
                                             --> baseline comparison
                                             --> provisional release gates
```

There are five important boundaries:

1. **Untrusted bytes → typed objects.** Pydantic models reject unknown fields
   and inconsistent state.
2. **Automation → human judgement.** Ingestion may discover a source; it cannot
   approve one.
3. **Generated text → displayed text.** A claim must survive verification and
   policy before rendering.
4. **Undefined → measured.** Missing ground truth is `None`, never a fake zero.
5. **Engineering evidence → clinical claims.** Passing tests demonstrate code
   behavior, not medical correctness.

---

## 4. The two codebases living in this repository

### 4.1 The engineered `synapse` package

This is the installable package declared in `pyproject.toml`. Its only runtime
dependency is Pydantic. FAISS, NumPy, OpenAI, Streamlit, and BM25 are optional
or legacy dependencies, so the trust layer can be imported and tested without
the application stack.

Its main design qualities are:

- typed schemas at every persisted boundary;
- deterministic identifiers and serialization;
- JSON/JSONL instead of executable pickle;
- content, file, order, and manifest hashes;
- human-governed source and evaluation lifecycles;
- offline, deterministic evaluation;
- explicit denominators and undefined values;
- claim-level citation verification;
- fail-closed policy and escaped rendering.

### 4.2 The legacy prototype

`app.py`, `Data/`, `Retrieval/`, `Generation/`, and top-level
`build_corpus.py` are the original demo. They are excluded from Ruff and mypy.
They use runtime `sys.path` changes, mutable dictionaries, broad exception
handling, pickle, and OpenAI calls directly.

The prototype is useful because it demonstrates the product interaction and
the RAG stages. It is dangerous to describe it as production-ready because its
runtime path bypasses most of the engineered controls.

### 4.3 The integration gap

`app.py` imports `Generation.answer_generator.AnswerGenerator`, not
`synapse.answer.generate.answer_query`. Consequently:

- generation is free-form text, not provider-constrained JSON;
- the model emits `[Source N]` strings that are not verified;
- claim excerpts are not checked against retrieved chunks;
- the display policy is not run;
- unsupported claims are not removed;
- `GroundedAnswer` and `SourceNumbering` are never added to the live result.

The current renderer notices that those structured fields are missing and uses
an escaped plain-text fallback with the permanent disclaimer. That closes the
HTML-injection path, but not the grounding path.

A strong interview answer is:

> “The repository contains the target architecture and the legacy runtime. My
> next integration milestone would replace the free-form generator in the
> submit handler with a provider adapter for `answer_query`, translate reranked
> legacy chunks into `RetrievedEvidence`, and render only the verified result.”

---

## 5. Foundations: the invariants everything else depends on

Read these first:

1. `synapse/normalize.py`
2. `synapse/hashing.py`
3. `synapse/identifiers.py`
4. `synapse/errors.py`
5. `synapse/schemas/base.py`
6. `synapse/corpus/jsonl.py`

### 5.1 Normalization

`normalize_text` applies NFC Unicode composition, collapses whitespace runs to
one ASCII space, and trims the ends. It deliberately does not lowercase or
strip punctuation.

Why:

- visually identical Unicode should hash identically;
- CRLF versus LF should not invalidate content;
- punctuation and case still matter to stored text and citation verification;
- retrieval normalization must not silently mutate what a user sees.

Know the difference between two normalizations:

- **content normalization** is conservative and hash-stable;
- **retrieval normalization** may lowercase, tokenize, or remove punctuation.

Mixing them would make provenance unverifiable.

### 5.2 Hashes

There are several SHA-256 meanings:

- `sha256_text`: normalized text content;
- `sha256_file`: exact file bytes, streamed in 1 MiB blocks;
- `sha256_records`: ordered serialized records separated by newlines;
- manifest self-digest: all manifest fields except the digest field itself.

The ordering hash matters because FAISS row `i` must correspond to chunk record
`i`. The same records in a different order represent a different index even if
an order-insensitive content set would look identical.

### 5.3 Identifiers

Document IDs use `<scheme>:<key>`:

```text
pubmed:41802233
pdf:6b86b273ff34fce1
example:diabetes-0001
```

Chunk IDs use `<document_id>#<zero-padded ordinal>`:

```text
pubmed:41802233#0002
```

Natural-key documents use the upstream identifier. Local content uses a
truncated normalized-content hash. Chunk ordinals continue across all sections
of a document.

Contrast that with the legacy ID:

```python
f"{pmid or title}_chunk{chunk_index}"
```

The legacy index contains 2,220 chunks but only 2,058 unique IDs. When hybrid
retrieval builds a dictionary keyed by `chunk_id()`, later collisions overwrite
earlier scores. This is not theoretical; it is visible in the committed data.

### 5.4 Safe errors and logging

The error hierarchy distinguishes not found, malformed schema, incompatible
version, broken integrity, incompatible artifacts, and unsafe deserialization.
Messages are built from sanitized structured details, avoiding payload text in
logs or UI.

`synapse/logging.py` adds structured context while prohibiting raw patient text
by policy and tests. The guiding rule is to log IDs, counts, lengths, hashes,
and error classes—not queries, answers, or chunk bodies.

### 5.5 Base schemas

All persisted records inherit from `SynapseModel` or `VersionedModel`:

- `extra="forbid"`: typos and smuggled fields fail;
- `frozen=True`: validated state cannot be mutated in place;
- defaults are validated;
- enums stay enums in memory;
- persisted models carry `schema_version`.

Compatibility is `MAJOR.MINOR`:

- different major: refuse;
- record minor newer than reader: refuse;
- record minor older or equal: accept.

This is a fail-closed reader policy. New optional fields can preserve minor
compatibility; required or meaning-changing changes need a major bump.

### 5.6 JSONL

`synapse/corpus/jsonl.py` is the single typed JSONL boundary. It supports plain
and gzipped files, validates every row, reports safe line-numbered failures,
writes deterministic one-line JSON, and computes ordered record digests.

The important security contrast is:

- JSON can represent data;
- pickle can execute object reconstruction logic.

The package bans pickle except in one quarantined migration reader.

---

## 6. Schema map

The schemas are not passive containers. Most business and safety rules are
cross-field validators, making invalid states hard or impossible to construct.

### 6.1 Source and chunk schemas

`SourceDocument` includes:

- stable ID and source URL;
- PMID, DOI, PMCID, or guideline ID;
- full title and structured authors;
- journal or issuing organization;
- publication dates with precision;
- all abstract sections in order;
- evidence type and label provenance;
- retraction/correction relationships and screening time;
- content hash and child chunk IDs;
- approval status and review evidence.

Its key invariant is that approval metadata must agree with approval state. A
retracted or withdrawn document is not citable.

`EvidenceChunk` binds normalized text to a parent document, ordinal, character
span, content hash, source type, ingestion timestamp, and optional section or
page metadata. Its builder derives the ID and hash instead of trusting callers.

### 6.2 Source-pack schemas

`PackSource` is a source’s standing inside one scoped pack. `ClinicianReview`
records reviewer pseudonym, role, rubric version, required substantive fields,
decision, timestamps, and review-due date.

`SourcePackManifest` records:

- intended population and use;
- excluded uses and topic boundaries;
- source selection policy;
- reviewer requirements;
- whole-pack approval state;
- version, digest, lifecycle counts, and provenance.

An approved pack must have a nonempty source set, approver, approval time, and
review-due date. Example packs are a first-class state, not a naming convention.

### 6.3 Evaluation schemas

`EvalCase` carries:

- the query and derived normalized query;
- failure-mode category;
- expected top-level behavior;
- document- and chunk-level graded relevance;
- required concepts and forbidden claims;
- citation requirements;
- annotation and disagreement state;
- privacy/redaction state;
- reviews and optional adjudication;
- corpus and dataset versions;
- split, timestamps, exclusions, and limitations.

The schema distinguishes five expected actions: answer, abstain, route to
staff, emergency escalation, and reject unsafe instruction.

`EvalDatasetManifest` binds cases to a version, protocol, corpus, digest,
counts, and gating policy.

### 6.4 Two answer schema families

This is easy to miss and worth explaining explicitly.

`synapse/schemas/answer.py` defines the evaluation/artifact family:

- `RetrievedEvidence` as a serializable retrieval hit;
- `ClaimExcerpt` and `AnswerClaim`;
- `StructuredAnswer` with answer ID, query hash, provenance, decision, and
  retrieved audit trail.

`synapse/answer/schema.py` defines the runtime display family:

- `SupportingExcerpt`;
- `GroundedClaim` with verifier-owned `support_status`;
- `GroundedAnswer` with summary, doctor evaluation, questions, limitations,
  and top-level action.

The evaluation harness consumes `StructuredAnswer`; the new generation and UI
path uses `GroundedAnswer`. They overlap but are not interchangeable. In a
future consolidation, either create an explicit adapter or choose one canonical
contract. Do not silently merge them because their fields encode different
purposes.

### 6.5 Index and run schemas

`IndexManifest` records corpus/index/source-pack versions, all hashes, artifact
files, embedding/chunking configuration, distance metric, vector normalization,
counts, build environment, Git commit when available, and state:

- `ready`: index exists and can be checked;
- `rebuild_required`: corpus exists but vectors do not.

`EvalRunResults` separates deterministic and judge metrics. Every metric
records value or an undefined reason, numerator, denominator, case count,
confidence interval, method, family, and unit.

---

## 7. PubMed ingestion, end to end

The engineered flow is:

```text
TOML config
  --> ESearch queries
  --> batched EFetch XML
  --> SourceDocument parsing
  --> deduplication
  --> retraction/no-abstract screening
  --> section-aware chunking
  --> chunks.jsonl.gz + documents.jsonl.gz
  --> manifest.json + build_report.json
  --> deep verification
```

### 7.1 Network client

`EUtilsClient` injects transport, clock, and sleep functions, so tests can run
offline. It provides:

- explicit timeouts;
- NCBI pacing, faster only when an API key exists;
- URL encoding;
- bounded batches;
- retry on 408, 429, and 5xx-like transient conditions;
- exponential backoff with a cap;
- no retry for permanent HTTP errors;
- safe structured logs.

Dependency injection is the reason the full ingestion pipeline can be tested
against fixtures without monkey-patching the network globally.

### 7.2 XML parser

The parser fixes three legacy defects:

1. It uses every `AbstractText`, not only the first.
2. It uses `itertext()` so nested markup does not truncate titles or sections.
3. It captures dates, authors, journal metadata, evidence class, language,
   IDs, corrections, retractions, and expressions of concern.

Important choices:

- XML bodies are capped at 64 MiB before parsing;
- ElementTree external entity expansion is not used;
- malformed XML produces a typed safe error;
- canonical publication date is the earliest known electronic/print date;
- date precision is retained instead of pretending year-only means January 1;
- “Retracted Publication” is different from “Retraction of Publication”;
- corrected articles remain usable but marked;
- source URLs are constructed from PMIDs, not trusted from XML.

### 7.3 Evidence classification

`synapse/ingest/evidence.py` maps PubMed publication types to a closed evidence
enum. It records provenance as publisher metadata, heuristic, human-labeled,
or unknown. The classifier does not claim that metadata-derived labels are a
clinical quality judgment.

### 7.4 Deduplication

First-seen input order wins. Matching proceeds strongest to weakest:

1. PMID;
2. normalized DOI;
3. normalized title plus publication year.

Every dropped record produces `DuplicateDecision`, so deduplication is an
auditable decision rather than a silent shrink.

### 7.5 Chunking

The engineered chunker never combines abstract sections and never splits a
sentence. It greedily packs complete sentences toward 800 characters, drops
sections shorter than 120 characters unless configured otherwise, and reports
single sentences over the 1,600-character ceiling instead of cutting them.

The tradeoff is deliberate: complete evidence sentences are more important
than uniform vector lengths. Regex sentence splitting is imperfect, but it is
deterministic and requires no downloaded model.

### 7.6 Build report

The report reconciles fetched, parsed, deduplicated, excluded, written, and
chunked counts. It records each exclusion, retraction summary, dedupe rule,
evidence type, config hash, tool version, Git commit when known, and permanent
provenance caveats.

The build writes artifacts and immediately deep-verifies them. A successful
command therefore means “written and loadable under the manifest,” not merely
“files appeared.”

---

## 8. Artifact integrity and migration

### 8.1 Verification ladder

`verify_artifacts` checks, cheapest first:

1. manifest exists, parses, and is version-compatible;
2. manifest self-digest;
3. declared files exist and sizes match;
4. raw file SHA-256 values match;
5. recognizable FAISS header dimensions and vector count match;
6. in deep mode, every chunk revalidates and content/order digests match.

There is no warning-and-continue path. A misaligned corpus and index can return
fluent answers with the wrong citations, so refusing service is correct.

### 8.2 FAISS header probe

The header probe recognizes known flat-index four-character codes and extracts
dimension and vector count from the first 16 bytes. This allows structural
checks without importing FAISS. Unknown formats return “cannot check,” while
the raw file hash still protects byte integrity.

The committed index reports:

```text
fourcc: IxF2
dimensions: 1536
vectors: 2220
```

### 8.3 Pickle migration

`synapse/_legacy/pickle_reader.py` exists only for migration. Its custom
unpickler allows exactly the inert legacy chunk shapes needed for the project.
The CLI still requires `--trust-input`; a restrictive unpickler reduces risk
but does not make third-party pickle safe.

The migration:

- reads without modifying the source;
- reconstructs stable document and chunk IDs;
- normalizes content;
- reports ID collisions and duplicate text;
- can filter short or empty chunks;
- writes typed JSONL and a manifest;
- can preserve a compatible FAISS file only when ordering is unchanged;
- forbids `--dedupe` together with an existing FAISS index;
- verifies the output before success.

The migration cannot recover missing authors, complete titles, true original
offsets, or overlap-free full documents. Re-ingestion from PubMed is the
fidelity fix; migration is only a safe bridge.

---

## 9. Source governance

The central rule is:

> **Automation may establish that a source exists. Only a recorded human review
> may establish that it is suitable for a scoped patient-facing use.**

### 9.1 States

```text
discovered --human--> screened --human--> approved
     |                    |                  |
     +--human--> rejected +--human--> rejected
                                              |
approved --time--> expired --human review-----+

any nonterminal state --replacement fact--> superseded (terminal)
```

Automation can create only `discovered`. It can expire an overdue approval or
mark supersession because those are mechanical facts. It cannot screen,
approve, reject, reopen, or reapprove.

### 9.2 Review boundary

`import_discovered` deliberately has no lifecycle-state argument. It hard-codes
`discovered` and preserves existing entries so re-import cannot erase reviews.

`record_review` derives the target state from the review decision, checks
reviewer role and rubric version, requires a permitted human transition, and
requires an exclusion reason for rejection.

Reviewer IDs match `rev_[a-z0-9]{8}` and are issued outside the software. If the
application generated its own reviewer identity, a review would prove only
that the code ran.

### 9.3 Eligibility

Default production eligibility requires:

- an approved whole pack;
- an approved individual source;
- a nonexpired review;
- no retraction or expression of concern;
- no example namespace/pack;
- source and pack policy compatibility.

Every exclusion has a reason. Relaxations are explicit and described as local,
ungoverned experimentation.

Before serving, `require_pack_index_agreement` checks that the pack ID/version
and content hash match the index manifest. An approval is therefore bound to
specific bytes, not a mutable directory name.

### 9.4 Versioning and comparison

Pack changes are classified by governance consequence. Content changes require
a semantic version increase and revoke prior pack approval. Previous versions
are snapshotted so comparison does not depend on Git being present.

The current `diabetes-previsit` pack is an unapproved example with three
discovered sources, one superseded source, and zero approved sources. It is a
governance demonstration, not a usable evidence set.

---

## 10. Evaluation dataset governance

Evaluation labels can make a bad system look good just as easily as code can.
The evaluation-set subsystem therefore governs cases much like source packs
govern evidence.

### 10.1 Gating requirements

A case may influence release decisions only if all are true:

- it is not synthetic;
- it is not excluded;
- required redaction is complete;
- its labels match the evaluated corpus version;
- its annotation status is reviewed or adjudicated;
- it has enough independent reviews, two by default;
- disagreement is resolved when required;
- adjudication state has an adjudication record.

Synthetic cases are refused even under the unreviewed escape hatch.

### 10.2 Near-duplicates and splits

Similarity combines token and character-shingle Jaccard signals. Cases above
the 0.70 threshold form transitive clusters. A deterministic salted hash assigns
clusters—not individual cases—to train/dev/test, so near-duplicates do not leak
across splits.

The leakage report separately checks:

- similar queries assigned to different splits;
- relevant documents reused across splits.

Document reuse is reported, not automatically forbidden, because a small
medical corpus may legitimately support many questions.

### 10.3 Agreement

Two reviewers use Cohen’s kappa; uniform larger panels use Fleiss’ kappa.
Agreement is reported for category and expected behavior, alongside raw
agreement, sample counts, disagreements, and small-sample caveats.

Remember what kappa does: it adjusts observed agreement for agreement expected
by chance. It can be unstable under class imbalance or small `n`.

### 10.4 Spreadsheet safety

Reviewer sheets are CSVs with fixed columns. Synthetic cases are omitted.
Cells starting with spreadsheet formula prefixes are neutralized. Blank rows
are skipped, partially completed rows are errors, malformed relevance grades
are rejected, Excel BOMs are accepted, and errors report row numbers without
echoing sensitive contents.

### 10.5 Dataset version comparison

Scores become incomparable when cases are added/removed, labels change, splits
move, or the corpus version changes. Status-only progress can remain comparable.
The generated changelog says when re-baselining is required.

---

## 11. Evaluation harness and metric fluency

### 11.1 System abstraction

`SystemUnderTest` is a protocol with `run(case) -> SystemResponse`.

- `FixtureSystem` replays committed responses by normalized query and fails on
  a missing fixture by default.
- `CallableSystem` adapts a live pipeline and converts exceptions/timeouts into
  explicit errored responses so one failure does not abort the run.

The committed CI path uses `FixtureSystem`, not the application.

### 11.2 Case execution

A case is skipped—not failed—when it is excluded, expects an answer with no
ground truth, or is labeled against a different corpus version.

For each evaluated response the harness computes retrieval, answer, safety,
cost, latency, and behavior metrics, then attaches the case’s gating decision.

Development metrics use all evaluable cases. Release aggregates use only
gating-eligible cases. If none are eligible, the harness calculates
informational metrics over all cases and places a strong warning before them.

### 11.3 Retrieval metrics

Let `R_k` be the unique retrieved IDs in the top `k`, and `G` be documents with
grade at least 1.

- **Recall@k** = `|R_k ∩ G| / |G|`; undefined if `G` is empty.
- **Precision@k** = `|R_k ∩ G| / |R_k|`; undefined with no retrieved results or
  no relevant ground truth.
- **MRR** = reciprocal rank of the first relevant result; zero if ground truth
  exists but nothing relevant is found.
- **DCG@k** uses gain `2^grade - 1` discounted by `log2(rank + 1)`.
- **nDCG@k** divides DCG by the ideal grade-sorted DCG.
- **Hit@k** is one if any relevant item occurs in the top `k`.

Deduplication before scoring prevents repeated IDs from inflating precision.

### 11.4 Answer metrics

Deterministic citation checking asks:

- did the model cite a factual claim?
- was the cited chunk actually retrieved?
- does the quoted excerpt occur in that chunk after documented normalization?
- do all numbers and clinical units in the claim occur in cited evidence?

Other checks include required-concept substring coverage, forbidden-claim
matches, structural answer validity, and approximate Flesch-Kincaid grade.

These checks establish traceability and obvious numeric consistency. They do
not establish semantic entailment or medical correctness.

### 11.5 Safety metrics

Emergency sensitivity and specificity stay separate because a missed emergency
and an unnecessary escalation are not equivalent. The harness also isolates:

- emergency false-negative count;
- negated-emergency accuracy;
- medication boundary violations;
- injection resistance;
- abstention correctness.

Do not average these into one “safety score.” Aggregation would hide the hazard
that matters.

### 11.6 Statistics

Binary proportions receive Wilson intervals. Continuous per-case means receive
seeded percentile bootstrap intervals with 2,000 resamples. Fewer than 10
observations produce no interval rather than a misleadingly precise one.

Intervals describe variation within the curated case set. The cases are not a
random patient sample, so the interval is not population uncertainty.

### 11.7 Judges

The model-judge path has versioned prompts, exact prompt hashes, deterministic
cache keys, repeated samples, and self-consistency. It is opt-in and advisory.
Offline mode cannot call it. Judge metrics live in a separate result bucket and
never gate releases.

### 11.8 Gates

A gate has:

- blocking or informational mode;
- higher- or lower-is-better direction;
- absolute floor/ceiling;
- absolute or relative regression tolerance;
- minimum denominator;
- optional category.

Missing/undefined/underpowered metrics are skipped, not passed. Incomparable
baselines fail closed when required. The current config is explicitly
provisional, has no approver, uses permissive absolute floors, and mainly tests
regression mechanics.

---

## 12. Structured answer integrity

The target runtime sequence is:

```text
generate -> parse -> verify -> decide -> apply -> render
```

There is intentionally no raw-generation-to-render path.

### 12.1 Generation contract

The model receives a JSON Schema generated from `GroundedAnswer`. It must attach
real source and chunk IDs plus exact supporting quotes to every claim.
`support_status` is removed from the provider schema because the model cannot
grade its own grounding.

Malformed JSON or a schema violation raises `GenerationError`; the code does
not guess or repair meaning.

### 12.2 Evidence verification

For every excerpt:

1. source was retrieved;
2. chunk was retrieved;
3. chunk belongs to the source;
4. normalized quote occurs in normalized chunk.

For every claim, the verifier also checks whether numbers in the claim occur in
the verified evidence. Outcomes are supported, partially supported, or
unsupported.

`synapse.answer.support` separately implements lexical overlap at a 0.35
threshold and defines an optional entailment-checker interface. A subtle but
important current gap is that neither `verify_claim` nor `answer_query` calls
that module. Despite the support module's “runs always” docstring, the
engineered sequence currently enforces excerpt and numeric checks, not lexical
relevance. A model can therefore quote an unrelated sentence accurately and
receive `SUPPORTED` when it introduces no unmatched number. Wiring a semantic
or lexical check into verification—then validating its false-positive and
false-negative tradeoff—is unfinished work.

### 12.3 Display policy

The policy passes through staff/emergency routing, otherwise answers or
abstains. A degraded answer is represented as an answer with unsupported claims
removed and possibly partially supported claims visibly marked. It abstains
when verified support falls below the policy threshold. Questions for the
doctor can survive abstention because they do not assert a medical fact.

### 12.4 Rendering

All dynamic user, model, and source values are HTML-escaped. URLs are allowed
only for HTTP(S). Source numbers are assigned once from retrieval order and
shared between inline markers and the source panel. Relevance scores are never
labeled confidence.

The disclaimer and emergency/staff messages are constants owned by the
renderer, not model output. A truncated generation cannot remove them.

---

## 13. The legacy RAG pipeline

Understand it because it is what the Streamlit app actually executes.

### 13.1 Legacy ingestion

`Data/fetch_and_chunk.py` defines a mutable `Chunk` dataclass, PubMed and text
ingestion, LangChain recursive character splitting, and pickle persistence.

Weaknesses:

- only the first abstract section is kept;
- nested markup can truncate titles;
- no date, authors, evidence class, or retraction state;
- chunk IDs collide when articles repeat across topic searches;
- the network client lacks engineered retries and explicit timeout policy;
- unsafe `pickle.load` is used directly;
- PDF ingestion is explicitly not implemented.

Top-level `build_corpus.py` runs about one hundred broad topic searches, takes
ten results per topic, concatenates chunks without deduplication, and sleeps
between calls. The committed corpus contains 2,220 chunks, all PubMed.

### 13.2 Dense retrieval

`VectorStore` batches OpenAI `text-embedding-3-small` requests, converts to
`float32`, L2-normalizes vectors, and uses exact `faiss.IndexFlatL2`.

For unit vectors:

```text
||x - y||^2 = 2 - 2(x · y)
```

Thus lower squared L2 distance and higher cosine similarity produce the same
ranking. Exact search is reasonable at 2,220 chunks; approximate indexes add
complexity without meaningful latency benefit at this scale.

The store persists FAISS, embeddings, and pickled chunks separately. Its core
invariant is positional alignment, but the legacy loader does not hash or
cross-check those files.

### 13.3 BM25

BM25 handles exact drug names, abbreviations, codes, and numbers that dense
retrieval can blur. The tokenizer lowercases, replaces punctuation except
hyphens, splits whitespace, and removes one-character tokens.

Conceptually BM25 combines:

- inverse document frequency;
- term-frequency saturation;
- document-length normalization.

The implementation wraps `rank_bm25.BM25Okapi` and returns the same
`{chunk, score, rank}` shape as dense retrieval.

### 13.4 Fusion

Linear fusion:

1. search every dense vector;
2. map results back by legacy `chunk_id()`;
3. convert L2 distance to `1 / (1 + distance)`;
4. min-max normalize dense and BM25 arrays;
5. combine `alpha*dense + (1-alpha)*bm25`, default alpha 0.7.

Its weakness is query-local min-max sensitivity and the colliding ID map.

Reciprocal rank fusion adds `1/(60 + rank)` for membership in each top list.
It is more robust to incompatible score distributions but discards score
magnitude and does not expose an explicit dense/sparse weight.

### 13.5 Reranking

The default reranker makes one `gpt-4o-mini` call per candidate and requests a
0–10 relevance score plus rationale. Parse or API failures fall back to a
transformed retrieval score under a broad catch.

Risks to recognize:

- temperature zero improves repeatability but does not guarantee determinism;
- returned scores are not clamped to 0–10;
- fallback semantics assume the original score is on 0–1, which may not always
  be true;
- the error reason can expose raw provider exception text;
- sequential calls increase latency and cost;
- retrieved text is placed directly into an instruction-style prompt, so
  prompt injection remains a concern.

The keyword fallback combines original score and raw whitespace-token overlap.
Because punctuation is not normalized here, it is only a smoke-test strategy.

### 13.6 Free-form generation

The generator first runs the emergency substring check, formats reranked
passages as numbered sources, and requests a fixed emoji-heading answer. It
returns the model prose and a separate list of sources.

Prompt rules are useful intent, but prompts are not enforcement. There is no
parser tying `[Source 1]` to a particular claim or quote. The live UI now avoids
parsing the headings and safely escapes the entire prose, so the intended cards
are no longer reconstructed on that path.

### 13.7 Emergency detector

The detector lowercases the query and performs `any(signal in query)` over 24
phrases. Ordering is correct—it runs before external calls—but vocabulary and
negation handling are not.

Against six repository emergency cases it catches 2/6. It misses natural
phrases describing stroke, respiratory distress, coughing up blood, and taking
far too many tablets. It also escalates four of six negated-emergency cases.

The tests intentionally pin these defects. They are known-defect tests, not an
endorsement. A safe fix requires clinically reviewed labels and vocabulary,
then evaluation of a new detector on a reviewed held-out set.

---

## 14. Streamlit application behavior

`app.py` executes top-level Streamlit layout code and stores four important
session values: conversation, chunks, loaded/built hybrid retriever, and API
key.

On submit:

1. require an API key;
2. run legacy emergency detection;
3. load `processed_chunks.pkl` and `hybrid_index` once per session;
4. perform dense + BM25 fusion for 10 results;
5. LLM-rerank to 3;
6. call the legacy free-form generator;
7. catch any exception and turn its raw string into an error answer;
8. append query/result to in-memory conversation and rerun.

`run_with_progress` places each blocking stage in a one-thread executor and
polls the future every 0.55 seconds to animate progress. It does not impose an
overall timeout; it merely keeps the UI moving while waiting.

Privacy implications:

- the raw query goes to OpenAI embeddings;
- query plus each candidate passage go to OpenAI reranking;
- query plus final passages go to OpenAI generation;
- query and answer remain in Streamlit session memory;
- current code does not write them to disk or structured logs;
- no consent, PHI redaction, auth, tenancy isolation, retention policy, or BAA
  is implemented.

The sidebar displays the latest offline evaluation summary if present. It does
not score live user queries because they have no ground truth.

---

## 15. CLI and automation map

### 15.1 Corpus build

```bash
python -m synapse.cli.build_corpus --config configs/corpus.example.toml
```

Loads typed TOML settings, configures the NCBI client, runs the engineered
pipeline, and prints a reconciled summary. Dry-run and force behavior are
explicit.

### 15.2 Artifact migration

```bash
python -m synapse.cli.migrate_artifacts \
  --input processed_chunks.pkl \
  --output-dir artifacts/corpus-v2 \
  --corpus-version corpus-v2 \
  --trust-input --dry-run
```

Use only for trusted local pickle. Prefer a real PubMed re-ingest for fidelity.

### 15.3 Source packs

`synapse.cli.source_pack` supports `init`, `import`, `validate`, `review`,
`version`, `diff`, and `build-index`. Every invocation prints the notice that
software records governance decisions but does not make them.

### 15.4 Evaluation sets

`synapse.cli.evalset` supports initialization, case authoring, validation,
spreadsheet export/import, adjudication, agreement, split planning, gating,
version diff, and changelog generation.

### 15.5 Evaluation and gates

```bash
python -m synapse.evals.run \
  --dataset evals/ci \
  --fixture evals/ci/system_responses.jsonl \
  --corpus evals/ci \
  --output artifacts/evals/demo \
  --offline

python -m synapse.cli.check_gates \
  --results artifacts/evals/demo/results.json \
  --baseline evals/baselines/approved/results.json \
  --config configs/quality-gates.toml
```

The evaluation writes full results, compact summary, Markdown report, and
per-case JSONL. The gate command can also write a GitHub job summary and JSON
gate report.

---

## 16. CI, security, and test strategy

The primary workflow has lint, formatting, scoped mypy, unit tests, integration
tests, artifact/schema validation, offline evaluation/gates, and a final
aggregating status job.

Important design choices:

- GitHub Actions are pinned by commit SHA;
- workflow permissions are read-only by default;
- application API credentials must be absent;
- tests exclude `live` by default;
- evaluation replays local fixtures;
- synthetic cases are explicitly asserted to have zero gating eligibility;
- artifacts are uploaded even when gates fail;
- dependency, PR dependency review, secret scanning, and filesystem credential
  checks are separated in the security workflow.

The security workflow has not been observed on GitHub according to repository
documentation. Local success does not prove hosted branch protection or
organization-level secret scanning is configured.

### 16.1 What each test module teaches

- `test_schema_validation.py`: schema versions, strict fields, governance and
  temporal invariants.
- `test_identifiers.py`: stable IDs, parsing, invalid shapes, collisions.
- `test_jsonl_roundtrip.py`: plain/gzip I/O, deterministic bytes, safe errors.
- `test_manifest_and_integrity.py`: hashes, traversal prevention, FAISS header,
  tamper and reorder detection.
- `test_migration.py`: restricted pickle, trust gate, ID repair, ordering and
  dedupe incompatibility.
- `test_ingest_parse.py`: structured abstracts, markup, dates, authors,
  retractions, malformed XML.
- `test_ingest_dedupe_chunk.py`: dedupe priority, sentence and section
  boundaries, chunk stats.
- `test_ingest_client_and_pipeline.py`: pacing, retry, screening, report and
  end-to-end fixture builds.
- `test_governance_states_and_schema.py`: transition actors and constructible
  state rules.
- `test_governance_workflows.py`: import, review, expiry, versioning,
  eligibility, pack/index agreement, CLI.
- `test_evalset_schema.py`: case/review/adjudication/privacy invariants.
- `test_evalset_workflows.py`: gating, similarity, splits, agreement,
  spreadsheet safety, dataset diffs, CLI.
- `test_evals_metrics.py`: formulas, undefined denominators, safety and stats.
- `test_evals_harness.py`: replay, aggregation, reports, error recording,
  determinism, judge separation.
- `test_quality_gates.py`: threshold modes, skips, baseline comparability,
  provisional configuration.
- `test_answer_adversarial.py`: fabricated IDs/quotes, injection text, HTML,
  URLs, disclaimers, numbering, abstention and emergency order.
- `test_answer_snapshots.py`: patient-visible output stability.
- `test_emergency_detector_known_defects.py`: intentionally records the current
  false negatives and false positives.
- `test_no_pickle_and_imports.py`: quarantine boundaries, dependency/import
  correctness, no patient text in logging.

`tests/conftest.py` provides deterministic factories and a network kill switch.
The tests are valuable documentation: read the production file, then its tests,
then explain which invariant each test protects.

---

## 17. What is strong, weak, and missing

### Strong and demonstrated

- typed artifact boundaries and cross-field invariants;
- deterministic IDs and serialization;
- content/order/file/manifest integrity checks;
- safe one-way migration architecture;
- complete PubMed abstract parsing and auditable screening;
- automation/human separation for sources and eval labels;
- deterministic evaluation with honest undefined values and denominators;
- baseline comparability and fail-closed gate mechanics;
- claim-level citation traceability in the engineered path;
- output escaping and permanent renderer-owned disclaimers;
- broad offline test coverage.

### Weak or prototype-only

- the live retrieval and generation stack;
- direct pickle loading in the app;
- score fusion over colliding legacy IDs;
- LLM reranking latency, fallback semantics, and injection resilience;
- emergency detection;
- broad top-level exception rendering;
- runtime connection between governed sources, verified index, structured
  generation, and Streamlit.

### Missing evidence or capability

- clinical review of any source, case, threshold, hazard, or answer;
- real patient/user benchmark and held-out evaluation;
- live system results in the committed evaluator;
- approved source pack and serve-ready governed index;
- semantic entailment or clinical correctness validation;
- integration of the existing lexical-support utility into claim verification;
- full text, guidelines, drug labels, contraindication/interaction data;
- retraction refresh after ingestion;
- evidence hierarchy in retrieval;
- user research, demand, retention, willingness to pay, or revenue;
- legal/regulatory analysis, privacy review, consent, auth, isolation, rate
  limiting, audit trail, retention/deletion controls;
- load, reliability, and real cost measurements;
- independently executed red team;
- hosted CI evidence and production deployment.

---

## 18. The best implementation roadmap to defend

Order matters because later claims depend on earlier evidence.

1. **Get accountable clinical and legal partners.** Review the hazard model,
   reviewer qualification, product boundaries, and data flow.
2. **Replace emergency detection.** Build a clinically reviewed labeled set,
   design negation/context handling, test false negatives separately, and keep
   routing ahead of all network calls.
3. **Wire the engineered answer path into Streamlit.** Add an OpenAI structured
   generation adapter and legacy-to-typed evidence adapter; show only verified
   output. Resolve the current gap between excerpt verification and the unused
   lexical/entailment support hook.
4. **Re-ingest the corpus through `synapse.ingest`.** Stop serving pickle and
   validate manifests before loading.
5. **Create and approve a narrow real source pack.** Keep the scope smaller than
   “all waiting-room medicine.”
6. **Create a reviewed real evaluation set.** Two independent reviews, resolve
   disagreement, pin relevance to the actual corpus, preserve held-out cases.
7. **Evaluate the real application through `CallableSystem`.** Replace ideal
   fixture claims with measured behavior while retaining offline replay.
8. **Set thresholds from evidence.** Obtain named sign-off, increase minimum
   denominators, and cut a new baseline.
9. **Complete privacy/security/deployment controls.** Provider agreement,
   consent, PHI handling, auth, tenant isolation, rate limiting, observability,
   incident response.
10. **Run a bounded pilot only after the gates above close.** Measure whether
    people arrive better prepared, not just whether generated prose sounds good.

The best near-term engineering PR is step 3 or 4. The hardest company blocker
is step 1.

---

## 19. A six-week fluency curriculum

### Week 1 — Foundations and contracts

Read `pyproject.toml`, README, `normalize.py`, `hashing.py`, `identifiers.py`,
`errors.py`, schema base, chunk/source/index schemas, and JSONL.

Be able to:

- derive a document and chunk ID by hand;
- explain every hash and why order matters;
- create an invalid model example for each major invariant;
- explain why `extra="forbid"` and immutability matter;
- contrast JSONL with pickle.

Exercise: write a small valid `EvidenceChunk` from scratch, serialize it, alter
one character, and predict every digest that changes.

### Week 2 — Ingestion and artifacts

Read all of `synapse/ingest`, index manifest/verify, build CLI, migration CLI,
and the ingestion/integrity tests.

Be able to whiteboard PubMed query to verified artifact directory. Explain
structured abstracts, retraction direction, dedupe order, chunk boundaries,
retry behavior, and why corrected articles differ from retracted articles.

Exercise: use one XML fixture and trace every resulting `SourceDocument` field.

### Week 3 — Retrieval and live app

Read `Data/`, `Retrieval/`, `Generation/`, `app.py`, and the committed artifact
metadata. Work through L2/cosine equivalence, BM25 intuition, min-max fusion,
RRF, reranking, prompt flow, and Streamlit session lifecycle.

Exercise: calculate a three-document linear fusion and RRF ranking by hand.
Then identify how duplicate chunk IDs can change the result.

### Week 4 — Governance and evaluation data

Read `schemas/source_pack.py`, `governance/`, `schemas/evalset.py`, `evalset/`,
their CLIs, and all governance/evalset tests.

Exercise: draw both state machines without looking. Construct five cases that
must be gating-ineligible for different reasons and predict the primary reason
code.

### Week 5 — Answer verification, metrics, and CI

Read `synapse/answer`, `synapse/evals`, quality-gate config, CI workflows, and
answer/evaluator tests.

Exercise: take one grounded claim and manually run excerpt, lexical, numeric,
policy, source-numbering, and rendering decisions. Compute Recall@5, MRR,
nDCG@5, Wilson interval intuition, and one regression gate.

### Week 6 — Synthesis and communication

Read system card, limitations, safety case, privacy flow, validation, investor
evidence, and this guide. Practice the technical and founder questions below.

Deliver three rehearsals:

1. a 10-minute code walkthrough;
2. a 5-minute architecture and safety review;
3. a 60-second founder pitch followed by a brutally honest risk answer.

Do not move on until you can explain the integration gap without notes.

---

## 20. Interview question bank

### “Walk me through a query.”

Start with the emergency check. For a normal query, explain remote query
embedding, exact FAISS search and local BM25, score fusion, LLM reranking, and
free-form generation. Then state that this is the legacy live path. Explain the
engineered target path—typed retrieved evidence, structured generation,
deterministic verification, policy, removal, escaped rendering—and disclose
that it is not yet wired into Streamlit.

### “Why hybrid retrieval?”

Dense retrieval handles semantic equivalence and paraphrases. BM25 handles
exact lexical signals such as drug names, acronyms, codes, and values. Hybrid
retrieval improves candidate recall across both query types. Linear fusion is
tunable but distribution-sensitive; RRF is robust to scale mismatch but loses
score magnitude and explicit weighting.

### “Why exact FAISS?”

The committed index has only 2,220 vectors. `IndexFlatL2` gives exact search,
requires no training, and reduces retrieval risk. ANN becomes useful when
latency or memory at much larger scale justifies recall loss and operational
complexity.

### “How do you prevent hallucinated citations?”

In the engineered path, every claim supplies real source/chunk IDs and verbatim
excerpts. Verification checks retrieval membership, parent-child identity,
normalized substring presence, and number consistency. Unsupported claims are
removed, and low-support answers abstain. A lexical checker exists and is
tested independently, but is not currently called by the verification
pipeline. This proves narrow traceability, not medical truth. The legacy live
generator does not yet use even that path.

### “Why is a content hash not enough for a vector index?”

Because row order is semantic. Reordering the same chunks preserves the set of
content but changes which chunk FAISS row `i` refers to. Synapse hashes both
canonical records and ordered chunk IDs, plus the binary index and manifest.

### “What does fail closed mean here?”

Newer incompatible schemas are rejected, bad hashes stop loading, malformed
generation does not render, unsupported claims disappear, insufficient support
abstains, missing fixture responses error, underpowered gates skip rather than
pass, and incomparable baselines fail the suite.

### “Why not let the model judge whether it is grounded?”

That asks the system under test to grade itself. Deterministic membership,
substring, and numeric checks are reproducible and cannot hallucinate. A model
judge can supplement semantic assessment but stays advisory and separately
provenanced.

### “Why is `None` important in metrics?”

No ground truth and failed retrieval are different states. Returning zero for
both creates false failures and false aggregates. `None` plus an explicit
reason preserves the denominator and prevents meaningless arithmetic.

### “What is the most serious current defect?”

The emergency detector recognizes only 2/6 natural emergency examples and only
2/6 negated examples correctly. The call ordering is safe, but the detector is
not. CI’s fixture reports 100% because it replays ideal responses rather than
running the detector.

### “What would you refactor first?”

Integrate the structured answer pipeline and migrate runtime artifacts away
from pickle. Which comes first depends on the exact objective, but neither
requires inventing clinical labels. Emergency redesign must run in parallel
with qualified clinical review because engineering should not author the
medical vocabulary alone.

### “What is technically differentiated?”

Not basic RAG. The differentiated engineering is the end-to-end evidence
supply chain: versioned human-governed sources, content-bound indexes,
claim-level citation verification, governed evaluation labels, honest
denominators, and release comparisons that fail closed.

### “What would you change at 10 million chunks?”

Move from flat exact search to a measured ANN design such as IVF/HNSW, shard or
filter by source scope, batch/parallelize retrieval, store metadata in a proper
retrieval service, and preserve the same manifest/order/version invariants.
Benchmark recall and latency on held-out queries before choosing the index.

### “How is privacy handled?”

Current local code avoids disk and log persistence, and tests enforce the
logging rule. But raw health text leaves the machine at three OpenAI stages,
with reranking making one call per candidate. There is no consent, PHI
redaction, BAA, auth, tenancy isolation, or retention policy. Privacy posture is
therefore inadequate for real users.

### “How would you know this works?”

First define the narrow intended outcome. Build reviewed source and evaluation
sets, run the actual pipeline, measure safety and answer/retrieval outcomes with
real denominators, clinically review outputs, and then pilot a workflow metric
such as whether patients arrive with useful questions and clinicians find the
prep accurate and time-saving.

---

## 21. Job-hunt positioning

### Backend/platform roles

Lead with typed boundaries, deterministic serialization, content addressing,
safe migration, state machines, idempotence, dependency injection, typed errors,
and CI. Describe how the manifest solves a silent cross-artifact consistency
failure.

### ML/RAG roles

Lead with hybrid retrieval, score calibration problems, RRF, reranking,
structured generation, evidence verification, evaluation denominators,
deterministic versus judge metrics, and real-system-versus-fixture distinction.

### Applied AI/product engineering roles

Lead with the full user flow, progressive migration from prototype to typed
architecture, safe rendering, permanent product boundaries, and the gap between
an impressive demo and a measurable product.

### Health-tech or safety roles

Lead with source governance, review provenance, retractions, hazard analysis,
abstention, privacy data flow, and the discipline of documenting an uncovered
catastrophic hazard instead of hiding it behind aggregate scores.

### A concise résumé bullet

> Built a Python evidence and evaluation layer for a medical RAG prototype:
> versioned Pydantic artifacts, PubMed ingestion and retraction screening,
> content-bound FAISS manifests, human-governed source/eval workflows,
> claim-level citation verification, and 766 offline tests with reproducible
> quality gates.

Do not claim clinical validation, HIPAA compliance, production deployment,
real-user performance, or 100% safety metrics.

---

## 22. YC/Antler/investor fluency

YC is an accelerator and Antler is an early-stage investor/accelerator. In both
contexts, code quality is supporting evidence; the core questions are user pain,
founder insight, speed, wedge, distribution, and proof.

### 22.1 One-liner

> Synapse helps patients prepare for a doctor visit by turning a health concern
> into sourced plain-language context and a focused list of questions to ask,
> while keeping evidence provenance and safety limits auditable.

### 22.2 The problem hypothesis

People often arrive at short appointments unsure what to ask, what history
matters, or how to interpret generic information they found online. The product
hypothesis is that a pre-visit evidence assistant can improve preparedness
without trying to diagnose or replace the clinician.

This is a hypothesis. The repository contains no user research proving its
frequency, severity, or willingness to pay.

### 22.3 The wedge

Do not pitch “all medical questions for everyone.” A defensible wedge is one
narrow, routine, clinician-supervised pre-visit workflow with approved sources
and measurable preparation outcomes—for example, an existing-condition routine
follow-up. Exact population and topic scope must be chosen with clinical and
commercial discovery.

### 22.4 What has been built

Say:

- working local Streamlit RAG prototype;
- verified evidence/artifact infrastructure;
- source and evaluation governance tooling;
- deterministic evaluation and release gate mechanics;
- structured claim verification and safe rendering;
- reproducible offline test suite.

Immediately add:

- no users or deployment;
- zero clinician-reviewed sources/cases;
- live app not yet connected to the full trust layer;
- emergency detector has a known severe defect;
- no privacy/regulatory readiness.

Credibility comes from knowing exactly what your evidence does and does not
show.

### 22.5 Differentiation and possible moat

The moat is not “we use PubMed plus an LLM.” That is replicable.

The plausible compounding asset is:

```text
reviewed use-case-specific source packs
  + reviewed evaluation cases
  + clinician feedback and adjudications
  + versioned system behavior and release evidence
  + workflow distribution
```

Today only the software scaffolding exists. The reviewed data, feedback loop,
and distribution do not.

### 22.6 Honest traction answer

> “We have technical validation of the quality infrastructure, not product
> validation. There are no users or clinical performance claims yet. The next
> milestone is a clinician-partnered narrow workflow, approved evidence, real
> labeled evaluations, and measurement of whether the tool improves visit
> preparation.”

Never substitute test count for traction.

### 22.7 Why now

A cautious answer:

> “LLMs make plain-language transformation and question drafting practical,
> but they also make fluent unsupported output cheap. The opportunity is not
> merely generation; it is pairing generation with an auditable evidence and
> review system in a narrow workflow.”

Avoid unsupported market-size or competitor claims unless you have current
external research.

### 22.8 Biggest risk

> “The biggest risk is not model latency. It is earning enough clinical trust
> and workflow value that users and clinicians want this, while staying on the
> low-risk appointment-preparation side of diagnosis and triage. Technically,
> the emergency detector and live trust-layer integration are immediate
> blockers.”

### 22.9 60-second founder pitch

> “Doctor visits are short, and patients often arrive without the right
> questions or a clear way to organize what they found online. Synapse is a
> pre-visit assistant that retrieves scoped medical evidence, explains it in
> plain language, and drafts questions to bring to the clinician—without trying
> to diagnose. We built the prototype, but the deeper work is an evidence
> operating system: sources require human approval, indexes are bound to exact
> content, claims can be checked against quoted passages, and evaluation cases
> cannot gate releases until reviewed. We are pre-clinical and have not yet
> validated demand or safety. Our next step is one narrow routine-care workflow
> with a clinical partner, approved sources, real evaluations, and a pilot that
> measures whether patients and clinicians are better prepared.”

### 22.10 Investor diligence questions you should answer

- Who is the first user, buyer, and channel? These may be three different
  people.
- What painful event triggers use in the hour before an appointment?
- Why does a general chatbot or patient portal not solve it?
- What narrow outcome improves: preparedness, question quality, clinician time,
  adherence, or something else?
- How do you obtain and retain clinical reviewers?
- How does approved evidence stay current?
- What must be true for privacy and regulatory clearance?
- What is human review cost per source/case and how does it scale?
- Which data compounds uniquely with use?
- What distribution path avoids expensive direct-to-consumer acquisition?
- What would falsify the company hypothesis in the next eight weeks?

The repository answers the evidence-system questions better than the user,
buyer, and distribution questions. Your application must close that imbalance
with real discovery, not more architecture.

---

## 23. Common traps and corrected answers

| Trap | Correct answer |
|---|---|
| “FAISS works offline.” | Stored search is local, but the query vector currently requires OpenAI. BM25 alone is offline. |
| “We have 100% emergency sensitivity.” | The fixture has 100%; the shipped detector gets 2/6 on the same natural examples. |
| “Citations prove correctness.” | They prove narrow traceability; context and medical interpretation remain unvalidated. |
| “The source pack is approved.” | It is an `unapproved_example` with zero approved sources. |
| “CI gates release quality.” | It tests gate mechanics over synthetic cases with provisional thresholds and zero gating cases. |
| “The app uses structured answers.” | The components exist, but the live submit handler still uses legacy free-form generation. |
| “No patient data is stored, so privacy is handled.” | Query text is sent to OpenAI and held in session memory; legal and operational controls are absent. |
| “Pickle is safe because it is local.” | Only trusted local pickle is acceptable; the production direction is typed JSONL. |
| “More tests means clinically safe.” | Tests verify implemented behavior. Clinical truth requires qualified review and real evidence. |
| “Confidence score.” | It is an uncalibrated relevance score from the reranker. |

---

## 24. Command cheat sheet

```bash
# Core verification
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check synapse tests
.venv/bin/mypy synapse/schemas synapse/evals synapse/answer synapse/corpus synapse/index

# Governed artifacts
.venv/bin/python -m synapse.cli.source_pack validate \
  --pack source_packs/diabetes-previsit
.venv/bin/python -m synapse.cli.evalset validate --dataset evals/ci

# Reproducible evaluation
.venv/bin/python -m synapse.evals.run \
  --dataset evals/ci \
  --fixture evals/ci/system_responses.jsonl \
  --corpus evals/ci \
  --output artifacts/evals/study \
  --offline

.venv/bin/python -m synapse.cli.check_gates \
  --results artifacts/evals/study/results.json \
  --baseline evals/baselines/approved/results.json \
  --config configs/quality-gates.toml

# Known safety defects
.venv/bin/pytest tests/test_emergency_detector_known_defects.py -v

# Legacy app: requires the separate legacy dependency set and an API key
streamlit run app.py
```

---

## 25. File-by-file atlas

### Repository root and legacy application

- `app.py`: Streamlit UI, session state, progress animation, evaluation summary
  panel, and actual legacy query orchestration.
- `build_corpus.py`: broad one-time legacy PubMed topic crawl.
- `Data/fetch_and_chunk.py`: legacy chunk dataclass, PubMed/text ingestion,
  recursive character chunking, and pickle persistence.
- `Retrieval/vector_store.py`: OpenAI embeddings, normalization, exact FAISS,
  persistence.
- `Retrieval/bm25_index.py`: tokenizer and BM25 wrapper.
- `Retrieval/hybrid_retriever.py`: min-max linear fusion, RRF, combined
  persistence.
- `Retrieval/reranker.py`: per-candidate LLM reranking and keyword fallback.
- `Generation/answer_generator.py`: emergency phrase list, free-form prompt,
  source list, legacy end-to-end helper.
- `Evaluation/Evaluator.py`: hard-retired pointer to `synapse.evals`.

### Package foundations

- `synapse/__init__.py`: version and package identity.
- `synapse/normalize.py`: content-stable text normalization.
- `synapse/hashing.py`: normalized text, byte, file, and ordered-record hashes.
- `synapse/identifiers.py`: document/chunk/reviewer grammars and constructors.
- `synapse/errors.py`: typed user-safe artifact exceptions.
- `synapse/logging.py`: structured, payload-safe logging setup.

### Schemas

- `schemas/base.py`: Pydantic policy and schema compatibility.
- `schemas/enums.py`: all closed vocabularies and lifecycle states.
- `schemas/source.py`: bibliographic, review, and source document models.
- `schemas/chunk.py`: evidence chunk contract and builder.
- `schemas/index.py`: manifest, embedding, and chunking models.
- `schemas/build_report.py`: auditable corpus build counts/decisions.
- `schemas/source_pack.py`: scoped governed source collection.
- `schemas/evalset.py`: cases, reviews, adjudication, dataset manifest.
- `schemas/evalrun.py`: metric, case outcome, judge provenance, run result.
- `schemas/evaluation.py`: older/simple evaluation provenance envelope.
- `schemas/answer.py`: serializable evaluation answer contract.
- `schemas/__init__.py`: public schema re-exports.

### Corpus, index, and legacy migration

- `corpus/jsonl.py`: deterministic typed JSONL/gzip I/O.
- `index/manifest.py`: index manifest construction, self-hash, FAISS header,
  Git/environment provenance.
- `index/verify.py`: layered fail-closed artifact verification.
- `_legacy/pickle_reader.py`: restricted migration-only unpickler.

### Ingestion

- `ingest/eutils.py`: paced, retried NCBI client with injected transport.
- `ingest/parse.py`: complete PubMed XML parser.
- `ingest/evidence.py`: publication-type to evidence-type mapping.
- `ingest/dedupe.py`: PMID/DOI/title-year decisions.
- `ingest/chunker.py`: section-aware, sentence-preserving chunking.
- `ingest/pipeline.py`: build orchestration, screening, artifacts, report.

### Governance

- `governance/states.py`: actor-aware source transition table.
- `governance/review.py`: discovery import, review recording, expiry.
- `governance/eligibility.py`: default exclusions and pack/index agreement.
- `governance/pack.py`: pack I/O and full validation report.
- `governance/versioning.py`: semantic version parsing/classification/bump.
- `governance/compare.py`: consequence-oriented pack diff.

### Evaluation-set tooling

- `evalset/dataset.py`: dataset I/O, derived counts/digest, validation.
- `evalset/gating.py`: case release eligibility.
- `evalset/similarity.py`: query similarity, duplicate pairs, clustering.
- `evalset/splits.py`: deterministic cluster-aware splits and leakage.
- `evalset/agreement.py`: Cohen/Fleiss kappa and disagreements.
- `evalset/spreadsheet.py`: safe reviewer CSV round trips.
- `evalset/compare.py`: comparability-oriented version diff/changelog.

### Evaluation harness

- `evals/system.py`: fixture and callable systems under test.
- `evals/harness.py`: case scoring, aggregation, run orchestration.
- `evals/metrics/retrieval.py`: Recall/Precision/MRR/DCG/nDCG/Hit.
- `evals/metrics/answer.py`: citation, numeric, concept, format, readability.
- `evals/metrics/safety.py`: emergency, negation, medication, injection,
  abstention.
- `evals/metrics/cost.py`: versioned pricing and latency summaries.
- `evals/stats.py`: Wilson and seeded bootstrap intervals.
- `evals/judges.py`: advisory model judge and deterministic cache.
- `evals/compare.py`: baseline metric deltas and regressions.
- `evals/gates.py`: threshold evaluation and gate report.
- `evals/report.py`: JSON, JSONL, summary, and Markdown outputs.
- `evals/run.py`: evaluation CLI orchestration.

### Grounded answer path

- `answer/schema.py`: runtime grounded answer contract.
- `answer/support.py`: lexical support and entailment interface.
- `answer/verify.py`: evidence membership, quote, number, claim verification.
- `answer/policy.py`: publish/degrade/abstain decision and filtering.
- `answer/generate.py`: provider interface and complete safe sequence.
- `answer/render.py`: escaped HTML/plain text, stable sources, permanent text.

### CLIs

- `cli/build_corpus.py`: configured engineered ingestion.
- `cli/migrate_artifacts.py`: one-way pickle migration.
- `cli/source_pack.py`: source governance lifecycle commands.
- `cli/evalset.py`: evaluation labeling lifecycle commands.
- `cli/check_gates.py`: release gate console/GitHub/JSON outputs.

Every package `__init__.py` declares or re-exports its public boundary. Empty
ones are intentional package markers, not missing implementations.

---

## 26. Final fluency checklist

You are fluent when you can do all of this without notes:

- explain the two-layer architecture and integration gap;
- trace all external calls for one query;
- derive IDs and explain all hashes;
- draw ingestion, source, eval, and answer flows;
- explain why FAISS row order is a citation integrity concern;
- compare BM25, dense retrieval, linear fusion, and RRF;
- calculate the major retrieval metrics and their denominators;
- explain Wilson versus bootstrap intervals;
- distinguish deterministic traceability from medical correctness;
- explain why model judges cannot gate and why missing metrics skip;
- draw the source lifecycle and eval gating rules;
- state the exact current evidence: 0 approved sources, 0 reviewed cases, 0
  gating cases, 766 passing tests;
- reproduce the emergency detector discrepancy;
- describe the privacy data flow and missing controls;
- propose an ordered path from prototype to bounded pilot;
- pitch the product without calling engineering verification traction or
  clinical validation.

The most impressive way to discuss this repository is not to pretend it is
finished. It is to show that you understand the difference between a fluent
demo, a correct software mechanism, a trustworthy measurement, and a validated
clinical product—and that you know exactly what evidence is required to move
from one to the next.

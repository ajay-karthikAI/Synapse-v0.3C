# Synapse — Evaluation, Evidence Governance & Citation Integrity Architecture

> ## ⚠️ HISTORICAL PLANNING DOCUMENT — superseded by the implementation
>
> This is the **design proposal** written *before* the quality layer was built.
> It is written in the future tense ("PR 9 will…", "this document proposes…")
> about work that has since been implemented, and it is retained as the design
> rationale and audit trail — not as a description of the current system.
>
> **Do not read this as documentation of what exists.** Where this document and
> the implementation disagree, the implementation is correct and this document
> is stale. Names, module paths, thresholds and PR sequencing all drifted during
> implementation.
>
> For the current system, read instead:
>
> | Topic | Current document |
> |---|---|
> | What exists and what is validated | [VALIDATION.md](VALIDATION.md) |
> | Intended and excluded uses | [SYSTEM_CARD.md](SYSTEM_CARD.md) |
> | Known limitations | [LIMITATIONS.md](LIMITATIONS.md) |
> | Artifact formats | [artifact-format.md](artifact-format.md) |
> | Ingestion | [ingestion.md](ingestion.md) |
> | Source governance | [SOURCE_GOVERNANCE.md](SOURCE_GOVERNANCE.md) |
> | Evaluation metrics | [evaluation-metrics.md](evaluation-metrics.md) |
> | Citation integrity | [citation-integrity.md](citation-integrity.md) |
> | CI and release gates | [release-quality-gates.md](release-quality-gates.md) |
>
> The findings table in §1 (H1–H7) remains accurate as a record of the defects
> that motivated the work, and is still referenced by other documents.

**Status:** Proposed (plan only — no application behavior is changed by this document)
**Target runtime:** Python 3.11
**Scope:** evaluation dataset, retrieval/answer metrics, source packs, safe artifact persistence, claim-level citation validation, CI quality gates
**Author of record:** engineering
**Clinical review status of this document:** `unreviewed` — no clinician has reviewed any content, label, or threshold described here.

---

## 0. Reading guide and a standing constraint

Three rules govern everything below and are repeated deliberately because they are the easiest things to lose during implementation.

1. **No approval claims without approval records.** The repository today contains **zero** review metadata — no reviewer identifiers, no review dates, no approval statuses, no sign-off artifacts of any kind. Therefore every document, chunk, evaluation case, emergency label and threshold that exists today migrates as `approval_status: unreviewed` / `review_status: unreviewed`. No schema default, migration script, report, or UI string may assert otherwise. Where this document needs a reviewer field, it specifies a *pseudonymous* identifier (`rev_` + opaque token) whose real-world mapping lives outside this repository. **No clinician names, credentials, or institutions are invented anywhere in this plan.**
2. **Deterministic and LLM-judged metrics never mix.** They live in different modules, different report sections, different CI workflows, and have different gating authority. A judge score may never block a merge.
3. **Nothing replaces working behavior until its replacement is measured against it.** Every substitution ships behind a flag or a dual-read path, with a parity test proving old and new produce the same user-visible result, and is flipped in a separate PR.

---

## 1. Current-state findings

Findings are grouped by severity. Each is anchored to a file and line. Everything marked **[verified]** was reproduced against the working tree during this inspection; everything marked **[assessed]** is a design judgement from reading the code.

### 1.1 Critical — the evaluation layer does not execute at all

| # | Finding | Evidence |
|---|---|---|
| C1 | **The Streamlit app can never import the evaluator.** `app.py` imports `evaluation.evaluator` (lowercase) but the module on disk is `Evaluation/Evaluator.py`. CPython's `FileFinder` performs a case check even on case-insensitive filesystems, so this raises `ModuleNotFoundError` on macOS *and* Linux. **[verified]** — `importlib.util.find_spec("evaluation.evaluator")` → `ModuleNotFoundError`; `find_spec("Evaluation.Evaluator")` → found. | [app.py:458](../app.py#L458), [app.py:643](../app.py#L643) |
| C2 | Both failing imports are inside bare `except Exception` handlers, so the failure is invisible. The sidebar silently renders the fallback caption "Metrics appear after first query" forever, and the per-query evaluation hook at [app.py:642-646](../app.py#L642-L646) is a no-op. **The product currently has no evaluation telemetry whatsoever, while presenting a "📊 Metrics" panel to the operator.** | [app.py:466-467](../app.py#L466-L467), [app.py:642-646](../app.py#L642-L646) |
| C3 | `run_pipeline()` — the documented end-to-end entry point — imports `retrieval.vector_store`, `retrieval.bm25_index`, `retrieval.hybrid_retriever` (lowercase) against packages named `Retrieval/`. It cannot run. **[verified]** by the same `find_spec` probe. | [Generation/answer_generator.py:245-247](../Generation/answer_generator.py#L245-L247) |
| C4 | **The built-in evaluation set is unsatisfiable.** All four hardcoded ground-truth PMIDs (`29505530`, `28823139`, `27697747`, `31580749`) are **absent from the committed corpus**. **[verified]** — corpus PMIDs span `41398847`–`42043842` (847 unique). Recall@k, Precision@k and MRR against `DEFAULT_EVAL_SET` are structurally 0.0 regardless of retrieval quality. `DEFAULT_EVAL_SET` is also never referenced by any caller. | [Evaluation/Evaluator.py:129-150](../Evaluation/Evaluator.py#L129-L150) |
| C5 | The one live call site passes `relevant_pmids=[]`. `recall_at_k` returns `0.0` for empty ground truth and `precision_at_k` returns `0.0`; the sidebar would therefore have displayed **"Recall@5 0%"** as a product metric had the import worked. The aggregate in `summary()` averages across heterogeneous `k` values and unlabeled queries. | [app.py:644](../app.py#L644), [Evaluation/Evaluator.py:75-79](../Evaluation/Evaluator.py#L75-L79), [Evaluation/Evaluator.py:215-234](../Evaluation/Evaluator.py#L215-L234) |

### 1.2 Critical — data integrity

| # | Finding | Evidence |
|---|---|---|
| C6 | **Chunk IDs are not unique.** `chunk_id()` is `f"{pmid}_chunk{index}"`, but the same PubMed article is fetched by multiple topics in `build_corpus.py`, so it is appended to the corpus more than once with the same index. **[verified]** — 2,220 chunks yield only **2,058 unique chunk IDs (162 collisions)** and **163 exact-duplicate texts**. | [Data/fetch_and_chunk.py:61-62](../Data/fetch_and_chunk.py#L61-L62), [build_corpus.py:170-181](../build_corpus.py#L170-L181) |
| C7 | The collisions **silently corrupt hybrid scoring.** `linear_fusion` reconstructs a full-corpus distance array via `score_map = {r["chunk"].chunk_id(): r["score"] ...}`; colliding IDs overwrite each other, and `score_map.get(c.chunk_id(), 2.0)` then assigns the *same* distance to every duplicate. RRF is worse — `reciprocal_rank_fusion` merges by `chunk_id()`, so duplicates collapse and one document's rank contribution is double-counted. | [Retrieval/hybrid_retriever.py:198-199](../Retrieval/hybrid_retriever.py#L198-L199), [Retrieval/hybrid_retriever.py:132-142](../Retrieval/hybrid_retriever.py#L132-L142) |
| C8 | **Index alignment is assumed, never verified.** `HybridRetriever.load()` sets `hr.chunks = hr.bm25_index.chunks` and trusts that the BM25 chunk list is positionally identical to the vector store's chunk list and to the FAISS row order. Nothing checks this. Today the three `chunks.pkl` files are byte-identical (**[verified]** — all three hash to `13c9a8ac0aa5eaa3…`), but a partial rebuild would produce silently mis-attributed citations: correct-looking answers pointing at the wrong PMIDs. | [Retrieval/hybrid_retriever.py:223-229](../Retrieval/hybrid_retriever.py#L223-L229) |
| C9 | **Only the first `<AbstractText>` element is captured.** `article.find(".//AbstractText")` returns one node; PubMed structured abstracts split into `BACKGROUND` / `METHODS` / `RESULTS` / `CONCLUSIONS` elements. The clinically actionable conclusion is discarded. **[verified]** — median total abstract text per PMID is 573 characters and **312 of 847 PMIDs (37%) hold under 400 characters total**, far below a normal PubMed abstract. Observed artifacts include chunks whose entire text is `"."` or `"Systematic review and meta-analysis."`. | [Data/fetch_and_chunk.py:139](../Data/fetch_and_chunk.py#L139) |
| C10 | **Titles are truncated at nested markup.** `title_el.text` returns only the text before the first child element, so titles containing `<sub>`/`<i>`/`<sup>` are cut. **[verified]** — corpus contains `"Pharmacologic MRI Brain Imaging Studies of Serotonin 5-HT"`, `"Evaluating the cardio-protective effects of "`, `"Performance of the Xpert Xpress Strep A for detection of "`. These truncated strings are rendered to patients as source names. | [Data/fetch_and_chunk.py:138](../Data/fetch_and_chunk.py#L138), [app.py:563](../app.py#L563) |
| C11 | **The corpus carries no evidence metadata at all.** `Chunk` has no publication date, journal, authors, publication type, MeSH terms, retraction status, or ingestion timestamp. **[verified]** — persisted fields are exactly `{chunk_index, page, pmid, source, source_url, text, title, total_chunks}`. Evidence governance is therefore impossible against the current artifact. | [Data/fetch_and_chunk.py:43-51](../Data/fetch_and_chunk.py#L43-L51) |
| C12 | **The corpus is recency-skewed and unfiltered.** `search_pubmed` sends no `sort`, no date range, and no publication-type filter. **[verified]** — every PMID is in the `41.4M–42.0M` band, i.e. the corpus is almost entirely very recent primary literature with no guidelines, no landmark trials, no systematic-review preference, and no retraction screening. | [Data/fetch_and_chunk.py:109-120](../Data/fetch_and_chunk.py#L109-L120) |

### 1.3 High — safety and citation integrity

| # | Finding | Evidence |
|---|---|---|
| H1 | **There is no abstention path.** `hybrid.search()` always returns `top_k` chunks irrespective of score; the reranker always returns 3; the generator always calls the LLM. The system prompt asks the model to "say so honestly" when sources are irrelevant, but nothing enforces or measures it. A query with no relevant corpus content still produces a confident, formatted, cited answer. | [Retrieval/hybrid_retriever.py:188-216](../Retrieval/hybrid_retriever.py#L188-L216), [Generation/answer_generator.py:84](../Generation/answer_generator.py#L84) |
| H2 | **Citations are unvalidated.** The prompt asks for `[Source 1]`-style markers; nothing parses them, nothing checks a marker resolves to a retrieved chunk, and the "📄 Research Sources" expander lists **all** reranked chunks whether or not the answer cited them. A patient sees three PubMed links attached to an answer that may have used none of them. There is no claim-level structure to validate against. | [Generation/answer_generator.py:128](../Generation/answer_generator.py#L128), [app.py:558-568](../app.py#L558-L568) |
| H3 | **The patient-facing "confidence %" is an LLM's self-reported relevance score.** `confidence_pct = int(rerank_score * 10)` where `rerank_score` is the reranker LLM's 0–10 usefulness rating; in the keyword fallback it is a fused retrieval score. It is rendered as a filled progress bar labelled "% confidence" next to a PubMed citation, where a patient will read it as confidence in the medical content. | [Retrieval/reranker.py:117](../Retrieval/reranker.py#L117), [Retrieval/reranker.py:161](../Retrieval/reranker.py#L161), [app.py:562-566](../app.py#L562-L566) |
| H4 | **Emergency routing is unbounded substring matching with no negation handling.** `check_emergency` lowercases and substring-matches 24 phrases. `"shortness of breath"` is simultaneously an emergency trigger and a corpus build topic ([build_corpus.py:57](../build_corpus.py#L57)); `"I do not have chest pain"` and `"I have no suicidal thoughts"` both route to emergency; `"crushing pressure in my chest"` and `"slurred speech"` do not. Sensitivity and specificity are unmeasured and, at present, unmeasurable. | [Generation/answer_generator.py:47-72](../Generation/answer_generator.py#L47-L72) |
| H5 | **Reranker failures degrade silently into fabricated scores.** A JSON parse error or API failure falls back to `item.get("score", 0.5) * 10` and stores the exception text in `rerank_reason`, which then flows into `confidence_pct` and is displayed. Failures are indistinguishable from successes in the UI. | [Retrieval/reranker.py:108-118](../Retrieval/reranker.py#L108-L118) |
| H6 | **`max_tokens=800`** can truncate the required six-part structure mid-section. `render()` then falls through to the raw-text branch, dropping the boundary disclaimer that the prompt mandates. | [Generation/answer_generator.py:199](../Generation/answer_generator.py#L199), [app.py:530-532](../app.py#L530-L532) |
| H7 | **Unescaped HTML injection into the rendered page.** The patient's own query, the model's answer, and PubMed-derived titles are interpolated into `st.markdown(..., unsafe_allow_html=True)` with no escaping — only `\n → <br>`. Titles are third-party content from an external API. | [app.py:577](../app.py#L577), [app.py:515-518](../app.py#L515-L518), [app.py:563](../app.py#L563) |

### 1.4 High — persistence and supply chain

| # | Finding | Evidence |
|---|---|---|
| P1 | **Executable pickles are the corpus format.** `processed_chunks.pkl`, `hybrid_index/bm25/chunks.pkl`, `hybrid_index/vector/chunks.pkl`, `hybrid_index/bm25/bm25.pkl` are all `pickle.load`ed at startup. Unpickling executes constructor callables named in the stream; a modified artifact is arbitrary code execution in the app process. **[verified, non-executing audit]** — opcode scan shows the only referenced globals are `Data.fetch_and_chunk.Chunk` (×3 files) and `rank_bm25.BM25Okapi`, so the *current* files are benign; the *format* is the exposure. | [Data/fetch_and_chunk.py:224-234](../Data/fetch_and_chunk.py#L224-L234), [Retrieval/vector_store.py:155-166](../Retrieval/vector_store.py#L155-L166), [Retrieval/bm25_index.py:138-151](../Retrieval/bm25_index.py#L138-L151) |
| P2 | **Pickles bind artifacts to source layout.** The corpus stream names `Data.fetch_and_chunk.Chunk`. Renaming the package or the dataclass — which §5 requires in order to fix C1/C3 — makes every existing artifact unloadable. This constrains the migration ordering. | opcode scan; [Data/fetch_and_chunk.py:43](../Data/fetch_and_chunk.py#L43) |
| P3 | **No index manifest, no integrity check.** There is nothing recording embedding model, dimension, index type, chunk count, chunk ordering, builder version, or content hashes; `HybridRetriever.load()` reads whatever is on disk. **[verified]** artifact state that *should* be asserted: FAISS header `IxF2` (IndexFlatL2), `d=1536`, `ntotal=2220`; `embeddings.npy` `float32 (2220, 1536)`; corpus 2,220 chunks. Nothing in the codebase knows any of these numbers. | [Retrieval/hybrid_retriever.py:223-229](../Retrieval/hybrid_retriever.py#L223-L229) |
| P4 | **The committed artifacts are excluded by `.gitignore`.** Lines 3–5 ignore `*.pkl`, `*.faiss`, `*.npy`; lines 12 and 14 attempt exemptions but are commented out (`##hybrid_index/`, `##processed_chunks.pkl`) — and a commented line is not a negation pattern in any case. Unless force-added, a fresh clone has no corpus, `load_chunks` raises `FileNotFoundError`, the broad handler catches it, and the patient sees *"Something went wrong: …"*. Note: **the working tree is not a git repository**, so tracked-state could not be confirmed — this is a `.gitignore` correctness finding, not a claim about history. | [.gitignore:3-5](../.gitignore#L3-L5), [.gitignore:12](../.gitignore#L12), [.gitignore:14](../.gitignore#L14), [app.py:648-649](../app.py#L648-L649) |
| P5 | **~30 MB of duplicated artifacts.** The same 1.06 MB corpus pickle is stored three times (byte-identical hashes), alongside a 13.6 MB `embeddings.npy` **and** a 13.6 MB `index.faiss` that contains the same vectors. | `du -sh .` → 30M; hash comparison |

### 1.5 Medium — hygiene, dependencies, environment

| # | Finding | Evidence |
|---|---|---|
| M1 | `from ast import If` — an unused stdlib AST node class, almost certainly an editor autocomplete accident. Harmless but signals the absence of linting. | [Data/fetch_and_chunk.py:24](../Data/fetch_and_chunk.py#L24) |
| M2 | **`print("WORKS")` executes at import time** — once inside the `Chunk` class body, twice at module level. Any consumer importing the corpus module pollutes stdout; in Streamlit this lands in server logs on every rerun. | [Data/fetch_and_chunk.py:71](../Data/fetch_and_chunk.py#L71), [:90](../Data/fetch_and_chunk.py#L90), [:101](../Data/fetch_and_chunk.py#L101) |
| M3 | Five modules mutate `sys.path` at import time to fake a package root. This is why the case-sensitivity bugs (C1/C3) went unnoticed — imports resolve differently depending on entry point. | [Data](../Data/fetch_and_chunk.py), [Retrieval/vector_store.py:37](../Retrieval/vector_store.py#L37), [Retrieval/bm25_index.py:30](../Retrieval/bm25_index.py#L30), [Retrieval/hybrid_retriever.py:23](../Retrieval/hybrid_retriever.py#L23), [Retrieval/reranker.py:35](../Retrieval/reranker.py#L35), [Generation/answer_generator.py:36](../Generation/answer_generator.py#L36), [Evaluation/Evaluator.py:28](../Evaluation/Evaluator.py#L28) |
| M4 | **Dependency manifest is wrong in both directions.** `langchain-community` is imported by the PDF path but undeclared; `plotly` and `tiktoken` are declared but unused; `langchain` + `langchain-core` are declared though only `langchain-text-splitters` is used. Every pin should additionally be verified to resolve for CPython 3.11 before the environment is frozen. | [requirements.txt](../requirements.txt), [Data/fetch_and_chunk.py:186](../Data/fetch_and_chunk.py#L186) |
| M5 | **Interpreter mismatch.** The devcontainer targets 3.11 (`mcr.microsoft.com/devcontainers/python:1-3.11-bookworm`) but the machine's default `python3` is **3.9.6 [verified]**. Any new code using 3.10+ syntax will fail outside the container until an explicit 3.11 environment is pinned. | [.devcontainer/devcontainer.json](../.devcontainer/devcontainer.json) |
| M6 | **No tests, no CI, no packaging.** **[verified]** — no `.github/`, no `tests/`, no `pyproject.toml`, no `Makefile`, no lockfile anywhere in the tree. | filesystem scan |
| M7 | **README describes a different project** (`medrag/`, `retriever/`, `processing/`, `main.py`) and claims *"FAISS-based vector search works without API access"*, which is false — `VectorStore.search` embeds the query through OpenAI on every call. | [README.md:59-81](../README.md#L59-L81), [README.md:32-35](../README.md#L32-L35), [Retrieval/vector_store.py:136-137](../Retrieval/vector_store.py#L136-L137) |
| M8 | **Per-chunk sequential rerank calls.** Ten retrieved chunks → ten sequential `chat.completions` round-trips before generation. This dominates latency and cost; `response.usage` is discarded everywhere, so neither is currently observable. | [Retrieval/reranker.py:87-100](../Retrieval/reranker.py#L87-L100) |

### 1.6 Privacy posture

`Evaluator.evaluate_retrieval` writes raw `query` text, and `log_failure` writes raw query text plus 100-character retrieved-passage excerpts, to `eval_log.json` in the current working directory, unencrypted and unbounded ([Evaluation/Evaluator.py:179-213](../Evaluation/Evaluator.py#L179-L213)). Patients describe symptoms in these queries. **This does not fire today only because the import in C1 fails first** — the privacy exposure is latent, and fixing C1 without fixing the logger would activate it. `.gitignore` does exclude `eval_log*.json`, which is the only mitigation currently present. Any correct sequencing must land privacy-safe telemetry *before* the import fix reconnects the evaluator (see §5, PR 3 before PR 9).

### 1.7 What is genuinely sound and must be preserved

Not everything needs replacing. The following are correct and the migration must not regress them:

- Fusion mathematics — min-max normalisation, the `1/(1+d)` distance inversion, and the RRF formulation with `k=60` are textbook-correct ([Retrieval/hybrid_retriever.py:36-155](../Retrieval/hybrid_retriever.py#L36-L155)).
- Embedding L2-normalisation before `IndexFlatL2`, which makes L2 rank-equivalent to cosine ([Retrieval/vector_store.py:69-70](../Retrieval/vector_store.py#L69-L70)).
- Hyphen-preserving tokenisation for `COVID-19`, `SGLT2`, `HbA1c`, `ICD-10` codes ([Retrieval/bm25_index.py:54-61](../Retrieval/bm25_index.py#L54-L61)).
- The uniform `{chunk, score, rank}` result contract across BM25 and vector retrieval — the reason a fusion layer was cheap to add, and the reason a metrics layer will be too.
- The emergency-bypass-before-retrieval control flow ([app.py:617-618](../app.py#L617-L618)): the *ordering* is right even though the *detector* is not.
- The six-part answer contract and its explicit non-diagnosis boundary ([Generation/answer_generator.py:79-113](../Generation/answer_generator.py#L79-L113)) — this is the structure that claim-level citation validation will formalise rather than replace.

---

## 2. Proposed directory tree

Two structural decisions drive this layout.

**(a) One installable package.** All application code moves under a single lowercase `synapse/` package installed with `pip install -e .`. This permanently eliminates the C1/C3 class of bug (there is one canonical import root, and it is exercised on case-sensitive Linux in CI), and deletes all seven `sys.path` mutations (M3).

**(b) Data lives beside code, versioned, in text.** Evaluation cases, source packs, thresholds and price tables are reviewable text files under version control; binary artifacts are confined to `artifacts/` and are always accompanied by a manifest.

```text
Synapse/
├── pyproject.toml                     # packaging, deps, ruff/mypy/pytest config
├── requirements.lock                  # hash-pinned, generated (pip-compile)
├── .python-version                    # 3.11
├── Makefile                           # make setup | lint | test | eval | report | verify
├── README.md
├── app.py                             # Streamlit entry point (thin; imports synapse.*)
│
├── docs/
│   ├── quality-architecture.md        # this document
│   ├── evidence-governance.md         # source lifecycle, review SOP, expiry policy
│   ├── evaluation-dataset.md          # authoring guide + labelling rubric
│   └── release-checklist.md           # what must be green to ship
│
├── synapse/
│   ├── __init__.py                    # __version__
│   ├── config.py                      # env + TOML settings, typed, no I/O at import
│   │
│   ├── schemas/                       # ── typed data models, the contract layer ──
│   │   ├── chunk.py                   # ChunkRecord, DocumentRecord
│   │   ├── source_pack.py             # SourceRecord, ReviewRecord, SourcePackManifest
│   │   ├── evalcase.py                # EvalCase, RelevanceLabel, EvalDatasetManifest
│   │   ├── answer.py                  # AnswerEnvelope, Claim, Excerpt, AbstentionReason
│   │   ├── manifest.py                # IndexManifest, ArtifactRef
│   │   ├── metrics.py                 # MetricValue, CaseResult, EvalRunReport
│   │   ├── enums.py                   # single home for every closed vocabulary
│   │   └── jsonschema/                # generated, committed, diff-reviewed
│   │       └── *.schema.json
│   │
│   ├── corpus/                        # ── safe persistence ──
│   │   ├── store.py                   # read/write JSONL(.gz); optional Parquet export
│   │   ├── ids.py                     # doc_id / chunk_id derivation, content hashing
│   │   ├── dedup.py                   # document- and chunk-level deduplication
│   │   ├── normalize.py               # NFC + whitespace normalisation (hash-stable)
│   │   └── legacy_pickle.py           # QUARANTINED restricted-unpickler; migration only
│   │
│   ├── ingest/                        # ── corpus construction ──
│   │   ├── pubmed.py                  # E-utilities client: full abstracts, itertext titles,
│   │   │                              #   dates, journal, authors, publication types
│   │   ├── retraction.py              # retraction / expression-of-concern screening
│   │   ├── chunker.py                 # splitter config, min-length guard
│   │   └── pdf.py, txt.py
│   │
│   ├── evidence/                      # ── source packs & governance ──
│   │   ├── registry.py                # load/query/validate source packs
│   │   ├── governance.py              # approval state machine, expiry, staleness
│   │   └── review.py                  # review-record I/O, pseudonymous reviewer IDs
│   │
│   ├── index/                         # ── artifact integrity ──
│   │   ├── manifest.py                # build / read / verify IndexManifest
│   │   ├── build.py                   # corpus -> embeddings -> FAISS + BM25 stats
│   │   ├── verify.py                  # fail-closed integrity gate before any load
│   │   └── errors.py                  # IndexIntegrityError and friends
│   │
│   ├── retrieval/                     # ← from Retrieval/ (behavior preserved)
│   │   ├── bm25_index.py              # pickle-free persistence
│   │   ├── vector_store.py            # FAISS only; chunks from corpus store
│   │   ├── hybrid_retriever.py        # + ordering assertion at load
│   │   ├── reranker.py
│   │   └── tokenize.py                # versioned tokenizer (id recorded in manifest)
│   │
│   ├── generation/                    # ← from Generation/
│   │   ├── answer_generator.py        # + structured-output mode behind a flag
│   │   ├── prompts/                   # versioned, hashed prompt templates
│   │   │   ├── system.v1.md  system.v2.md  rerank.v1.md  judge_groundedness.v1.md
│   │   └── emergency.py               # detector, extracted and independently testable
│   │
│   ├── citation/                      # ── claim-level validation ──
│   │   ├── extract.py                 # envelope -> claims + excerpts
│   │   ├── align.py                   # excerpt -> chunk span (normalised verbatim match)
│   │   ├── numeric.py                 # numbers/units/ranges in claim ⊆ cited chunk
│   │   ├── entailment.py              # LLM-judge NLI (isolated; advisory only)
│   │   └── policy.py                  # publish | degrade | abstain decision
│   │
│   ├── evaluation/                    # ← from Evaluation/ (rebuilt)
│   │   ├── dataset.py                 # versioned dataset load + validation
│   │   ├── metrics/
│   │   │   ├── retrieval.py           # recall@k, precision@k, MRR, nDCG@k
│   │   │   ├── citation.py            # correctness, completeness (deterministic)
│   │   │   ├── behavior.py            # abstention, emergency sensitivity/specificity
│   │   │   ├── readability.py         # Flesch–Kincaid, vendored & pinned
│   │   │   └── cost.py                # latency, tokens, estimated cost
│   │   ├── judges/                    # ── LLM-judged ONLY; never gates CI ──
│   │   │   ├── groundedness.py  citation_semantics.py  base.py
│   │   ├── runner.py                  # orchestrates a run, offline by default
│   │   ├── compare.py                 # run vs baseline regression diff
│   │   ├── thresholds.py              # reads thresholds.toml, applies gate policy
│   │   └── report.py                  # JSON + Markdown emitters
│   │
│   └── telemetry/                     # ── privacy-first observability ──
│       ├── privacy.py                 # salted hashing, redaction, PHI guards
│       ├── records.py                 # QueryRecord / RunRecord (no raw text by default)
│       └── timing.py                  # stage timers, token accounting
│
├── evaldata/                          # ── the versioned evaluation dataset ──
│   ├── v0.1/
│   │   ├── cases.jsonl                # one EvalCase per line
│   │   ├── manifest.json              # version, checksum, counts, provenance
│   │   └── reviews/                   # review records, one per reviewed case
│   ├── CHANGELOG.md
│   └── LABELING_GUIDE.md
│
├── evidence/                          # ── the versioned source packs ──
│   ├── v0.1/
│   │   ├── sources.jsonl              # one SourceRecord per document
│   │   ├── manifest.json
│   │   └── reviews/
│   └── CHANGELOG.md
│
├── artifacts/                         # ── binary, manifest-governed ──
│   └── corpus-v2/
│       ├── chunks.jsonl.gz            # canonical chunk store (no pickle)
│       ├── documents.jsonl.gz
│       ├── index.faiss                # vector index ONLY
│       ├── bm25_stats.json.gz         # optional; or rebuilt at load
│       └── manifest.json              # IndexManifest — hashes + build config
│
├── config/
│   ├── thresholds.toml                # release gates, per-metric, per-environment
│   ├── pricing.toml                   # model price table, versioned + dated
│   └── emergency_terms.toml           # externalised, reviewable clinical vocabulary
│
├── reports/                           # generated; git-ignored except baselines
│   └── baselines/<dataset_version>.json
│
├── scripts/
│   ├── migrate_pickles.py  build_corpus.py  build_index.py
│   ├── verify_artifacts.py  run_eval.py  compare_eval.py  make_report.py
│   └── governance_audit.py            # expiring/expired/unreviewed sources
│
├── tests/
│   ├── unit/  contract/  golden/  property/  privacy/
│   ├── fixtures/
│   │   ├── mini_corpus/               # ~40 chunks, committed, fully deterministic
│   │   ├── query_embeddings.npz       # recorded vectors → retrieval eval with no API
│   │   ├── llm_cassettes/             # recorded generation/judge responses
│   │   └── artifacts_bad/             # tampered manifests for integrity tests
│   └── conftest.py                    # network kill-switch + API-key guard (autouse)
│
└── .github/workflows/
    ├── ci.yml                         # PR gate: lint, types, tests, offline eval
    ├── eval-nightly.yml               # live LLM + judges; advisory; writes reports
    └── governance.yml                 # weekly source expiry / retraction audit
```

**Compatibility shims.** `Data/`, `Retrieval/`, `Generation/`, `Evaluation/` remain for exactly one release as modules that re-export from `synapse.*` and emit `DeprecationWarning`. This is what lets PR 1 land without touching `app.py` behavior. On a case-insensitive filesystem the rename must be done in two steps (`git mv Retrieval retrieval_tmp && git mv retrieval_tmp retrieval`) with `git config core.ignorecase false` set for the repository.

---

## 3. Data schemas

### 3.0 Conventions

- **Format:** newline-delimited JSON (`.jsonl`, gzipped when > 1 MB) is canonical for every record collection. It diffs, streams, and needs nothing but stdlib `json` + `gzip`. Parquet is an *optional export* (`scripts/export_parquet.py`) for analysis, never a source of truth — this keeps `pyarrow` out of the application dependency set while satisfying the "JSONL or Parquet" requirement.
- **Models:** `pydantic` v2 for every schema. It is **already** in the dependency tree as a required dependency of the `openai` SDK, so promoting it to a direct dependency adds zero new install surface, and it gives validation at I/O boundaries plus `model_json_schema()` export for the committed JSON Schemas. *(Stdlib-only fallback if the team prefers: `dataclasses` + a hand-written validator; this costs ~400 lines and loses schema export. Recommendation: use pydantic.)*
- **Time:** every timestamp is timezone-aware ISO-8601 UTC (`2026-08-12T14:03:22Z`). Dates without a time component are `YYYY-MM-DD`.
- **Hashes:** `sha256`, lowercase hex, computed over NFC-normalised, whitespace-collapsed UTF-8 (`synapse.corpus.normalize`), so a hash is stable across cosmetic re-serialisation.
- **Enums are closed.** Every enumerated field lives in `synapse/schemas/enums.py`; unknown values fail validation rather than being coerced. Adding a value is a schema-version bump.
- **Additive evolution.** Schema versions are `MAJOR.MINOR`; readers accept any `MINOR` ≥ their own within the same `MAJOR`. Removing or retyping a field is a `MAJOR` bump and requires a migration script.

### 3.1 Identifier scheme (fixes C6/C7)

```
doc_id     := "pubmed:{pmid}"  |  "guideline:{org_slug}:{doc_slug}:{revision}"
              | "pdf:{sha256[:16]}"  |  "txt:{sha256[:16]}"
chunk_id   := "{doc_id}#{ordinal:04d}"          # ordinal is per-document, 0-based
case_id    := "syn-{category}-{nnnn}"           # e.g. syn-emergency-0007; never reused
source_pack_version := "sp-{YYYY.MM}.{n}"       # e.g. sp-2026.08.1
dataset_version     := "ed-{MAJOR}.{MINOR}"     # e.g. ed-0.1
```

The corpus is deduplicated **at document level before chunking**, so a `doc_id` appears exactly once and `chunk_id` is unique by construction. `content_sha256` is carried separately for integrity and near-duplicate detection — it is never part of the ID, so re-chunking with different parameters produces a new `chunk_id` namespace via the manifest, not silent collisions.

### 3.2 `ChunkRecord` — `artifacts/corpus-v2/chunks.jsonl.gz`

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `str` | `"chunk/1.0"` |
| `chunk_id` | `str` | unique, `{doc_id}#{ordinal:04d}` |
| `doc_id` | `str` | FK → `DocumentRecord.doc_id` and `SourceRecord.doc_id` |
| `ordinal` | `int ≥ 0` | position within document |
| `n_chunks_in_doc` | `int ≥ 1` | |
| `text` | `str` | normalised; **min length enforced** (default 120 chars) |
| `char_start`, `char_end` | `int` | offsets into the document's normalised full text — required for excerpt alignment |
| `content_sha256` | `str` | of `text` |
| `token_count` | `int` | tokenizer id recorded in the index manifest |
| `section` | `str \| null` | e.g. `CONCLUSIONS` — preserved from structured abstracts (fixes C9) |
| `page` | `int \| null` | PDF only; `null`, not `-1` |
| `chunker` | `object` | `{name, version, chunk_size, overlap, separators_sha256}` |
| `ingested_at` | `datetime` | |

`text`, `char_start/char_end` and `content_sha256` together are what make deterministic excerpt verification (§3.6) possible; none of them exist today.

### 3.3 `SourceRecord` — `evidence/<version>/sources.jsonl`

One record per document. This is the source pack.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `str` | `"source/1.0"` |
| `doc_id` | `str` | primary key |
| `source_type` | enum | `pubmed_abstract` \| `guideline` \| `systematic_review` \| `pdf` \| `txt` |
| `source_url` | `str` (URL) | **exact** resolved URL, not a template |
| `identifiers` | object | `{pmid, pmcid, doi, guideline_id, nct_id}` — each nullable |
| `title` | `str` | full title via `itertext()` (fixes C10) |
| `authors` | `list[{family, given, initials}]` | empty list if unavailable; never fabricated |
| `container` | object | `{journal, issn, volume, issue, pages}` or `{issuing_organization}` |
| `publication_date` | `date \| null` | |
| `revision_date` | `date \| null` | guideline revisions |
| `retrieved_at` | `datetime` | when Synapse fetched it |
| `evidence_type` | enum | `guideline` \| `systematic_review` \| `meta_analysis` \| `rct` \| `cohort` \| `case_control` \| `case_report` \| `narrative_review` \| `preprint` \| `other` \| `unclassified` |
| `evidence_type_provenance` | enum | `publisher_metadata` \| `heuristic` \| `human_labeled` — **required**, so a heuristic guess is never mistaken for a curated label |
| `publication_types` | `list[str]` | raw MeSH publication types |
| `retraction_status` | enum | `none` \| `retracted` \| `expression_of_concern` \| `corrected` \| `unchecked` (**default `unchecked`**) |
| `retraction_checked_at` | `datetime \| null` | |
| `approval_status` | enum | `unreviewed` (**default**) \| `in_review` \| `approved` \| `rejected` \| `expired` \| `withdrawn` |
| `review` | `ReviewRecord \| null` | **must be `null` when `approval_status == "unreviewed"`** |
| `review_due_date` | `date \| null` | required iff `approval_status == "approved"` |
| `content_sha256` | `str` | of the document's normalised full text |
| `chunk_ids` | `list[str]` | |
| `source_pack_version` | `str` | |
| `notes` | `str` | free text, no clinical assertions |

**`ReviewRecord`** (also written standalone to `evidence/<v>/reviews/<doc_id>.json`):

| Field | Type | Notes |
|---|---|---|
| `reviewer_id` | `str` | pseudonymous, `^rev_[a-z0-9]{8}$`. **Real identity mapping is maintained outside this repository.** Validation rejects anything resembling a personal name or email. |
| `reviewer_role` | enum | `clinician` \| `pharmacist` \| `clinical_informaticist` \| `non_clinical` |
| `reviewed_at` | `datetime` | |
| `decision` | enum | `approved` \| `rejected` \| `needs_changes` |
| `review_due_date` | `date` | next mandatory re-review |
| `rubric_version` | `str` | which review SOP was applied |
| `comment` | `str` | |
| `record_sha256` | `str` | hash of the canonicalised record |
| `signature` | `str \| null` | detached HMAC/signature; key held outside the repo (deferred to PR 15) |

**Cross-field invariants enforced by the validator** (`tests/contract/test_source_pack_invariants.py`):
- `approval_status == "approved"` ⟹ `review` is present, `review.decision == "approved"`, `review.reviewer_role == "clinician"`, and `review_due_date` is set and in the future at pack-build time.
- `approval_status == "unreviewed"` ⟹ `review is None`. A record cannot be approved by omission.
- `retraction_status in {retracted, expression_of_concern}` ⟹ the document is excluded from index builds and its chunks fail retrieval eligibility.
- The literal string "clinician-approved" (in any casing) may not appear in any generated report or UI string unless the underlying `SourceRecord.approval_status == "approved"`. Enforced by a lint test over report templates.

**Migration reality:** all 847 current documents land as `approval_status: unreviewed`, `retraction_status: unchecked`, `evidence_type: unclassified`, `review: null`.

### 3.4 `EvalCase` — `evaldata/<version>/cases.jsonl`

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `str` | `"evalcase/1.0"` |
| `case_id` | `str` | stable, never reused; retired cases get `status: retired`, not deletion |
| `query` | `str` | **authored, synthetic** — never a real patient query (see §3.4.1) |
| `query_category` | enum | `symptom_education` \| `medication` \| `lab_interpretation` \| `procedure_prep` \| `screening_prevention` \| `chronic_management` \| `emergency` \| `out_of_scope` \| `adversarial` \| `abstention_probe` |
| `relevant_docs` | `list[{doc_id, relevance}]` | `relevance` ∈ {0,1,2,3} — graded, required for nDCG |
| `relevant_chunks` | `list[{chunk_id, relevance}]` | optional but preferred; enables chunk-level metrics |
| `corpus_version` | `str` | which corpus the labels were assigned against — labels are **not** portable across corpus versions |
| `expected_behavior` | enum | `answer` \| `abstain` \| `route_emergency` |
| `expected_emergency` | enum | `route` \| `no_route` |
| `emergency_rationale` | `str` | why; required when `expected_emergency == "route"` |
| `must_not_contain` | `list[str]` | e.g. diagnosis phrasing, dosage instructions |
| `must_contain_concepts` | `list[str]` | advisory only; never a blocking gate |
| `difficulty` | enum | `easy` \| `medium` \| `hard` |
| `review_status` | enum | `unreviewed` (**default**) \| `in_review` \| `clinician_reviewed` \| `disputed` \| `retired` |
| `review` | `ReviewRecord \| null` | same invariants as §3.3 |
| `label_provenance` | enum | `engineering_seed` \| `retrieval_assisted` \| `clinician_authored` — **required** |
| `dataset_version` | `str` | |
| `created_at`, `updated_at` | `datetime` | |
| `notes` | `str` | |

**Invariants:**
- `expected_behavior == "route_emergency"` ⟺ `expected_emergency == "route"`.
- `expected_behavior == "answer"` ⟹ at least one `relevant_docs` entry with `relevance ≥ 2`.
- `expected_behavior == "abstain"` ⟹ `relevant_docs` is empty **or** every entry has `relevance == 0`. (This is what makes abstention probes meaningful: the corpus genuinely cannot answer them.)
- Every `doc_id`/`chunk_id` referenced must exist in the corpus version named by `corpus_version` — **the check that would have caught C4 on day one.**
- `review_status == "clinician_reviewed"` ⟹ `review.reviewer_role == "clinician"` and `review.decision == "approved"`.

**v0.1 will ship entirely as `review_status: unreviewed`, `label_provenance: engineering_seed`.** Emergency-routing gates stay **advisory** until an identified clinician reviewer has signed the emergency subset (§8, U2).

#### 3.4.1 Patient text never enters the dataset

Eval queries are authored from clinical topic lists and public literature, not harvested from production. `synapse.evaluation.dataset` refuses to load a case whose `label_provenance` is absent, and `tests/privacy/test_no_production_text.py` asserts no case text matches any hash in the (hash-only) production query log. This is the mechanism that keeps §1.6's exposure closed permanently rather than by accident.

### 3.5 `IndexManifest` — `artifacts/<corpus>/manifest.json`

```json
{
  "schema_version": "manifest/1.0",
  "index_id": "corpus-v2-2026-08-12-a41f",
  "corpus_version": "corpus-v2",
  "source_pack_version": "sp-2026.08.1",
  "built_at": "2026-08-12T14:03:22Z",
  "artifacts": [
    {"role": "chunks",     "path": "chunks.jsonl.gz",    "sha256": "…", "bytes": 1211904},
    {"role": "documents",  "path": "documents.jsonl.gz", "sha256": "…", "bytes": 402118},
    {"role": "faiss",      "path": "index.faiss",        "sha256": "…", "bytes": 13639725},
    {"role": "bm25_stats", "path": "bm25_stats.json.gz", "sha256": "…", "bytes": 890233}
  ],
  "counts": {"documents": 847, "chunks": 2220},
  "ordering": {
    "chunk_ids_sha256": "…",
    "description": "sha256 over '\\n'.join(chunk_id for chunk in row order) — row i of FAISS == chunk i of chunks.jsonl.gz"
  },
  "embedding": {
    "provider": "openai", "model": "text-embedding-3-small",
    "dim": 1536, "l2_normalized": true, "batch_size": 100
  },
  "faiss": {"index_type": "IndexFlatL2", "metric": "l2", "ntotal": 2220, "dim": 1536},
  "bm25": {"impl": "rank_bm25.BM25Okapi", "tokenizer": "synapse.retrieval.tokenize@v1",
           "k1": 1.5, "b": 0.75, "epsilon": 0.25, "rebuild_on_load": true},
  "chunker": {"name": "recursive_character", "chunk_size": 500, "overlap": 100,
              "separators_sha256": "…", "min_chunk_chars": 120},
  "builder": {"git_commit": "…", "python": "3.11.9", "faiss_cpu": "…", "numpy": "…", "synapse": "0.3.0"},
  "eligibility": {"excluded_retracted": 0, "excluded_unapproved": 0,
                  "policy": "retracted_excluded; approval not enforced at v2"},
  "manifest_sha256": "…"
}
```

`verify()` **must pass before any index is loaded**, and raises `IndexIntegrityError` (never a warning, never a fallback) on: missing artifact, hash mismatch, `faiss.ntotal != counts.chunks`, `faiss.dim != embedding.dim`, `ordering.chunk_ids_sha256` mismatch against the corpus file, embedding-model mismatch against the query-time model, or `manifest_sha256` mismatch. The ordering hash is the direct fix for C8 — the failure mode it prevents is an answer that cites the wrong PMID while looking entirely normal.

**`eligibility` is deliberately explicit** so a report can never imply approval-gating was applied when it was not.

### 3.6 `AnswerEnvelope` — the structured generation contract

```json
{
  "schema_version": "answer/1.0",
  "envelope_id": "ans_01J...",
  "query_ref": {"query_sha256": "…", "category": "symptom_education"},
  "abstained": false,
  "abstain_reason": null,
  "is_emergency": false,
  "sections": {
    "research": [ "claim_001", "claim_002" ],
    "doctor_will_evaluate": [ "claim_003" ],
    "questions": [ "q_001", "q_002", "q_003", "q_004" ]
  },
  "claims": [
    {
      "claim_id": "claim_001",
      "text": "HbA1c reflects your average blood sugar over about two to three months.",
      "section": "research",
      "claim_type": "factual",
      "source_ids": ["pubmed:41802233"],
      "excerpts": [
        {"source_id": "pubmed:41802233",
         "chunk_id": "pubmed:41802233#0002",
         "quote": "HbA1c reflects average plasma glucose over 2-3 months",
         "char_start": 118, "char_end": 171}
      ]
    }
  ],
  "questions": [
    {"question_id": "q_001", "text": "…", "requires_citation": false}
  ],
  "boundary_statement": "…",
  "generation": {
    "model": "gpt-4o-mini", "prompt_id": "system.v2", "prompt_sha256": "…",
    "temperature": 0.3, "max_tokens": 1200, "seed": 7
  }
}
```

Design points that matter:

- **`claim_id` is stable within an envelope** and is what every validation result, judge score and UI annotation keys on. Claims are the unit of groundedness — not sentences, not paragraphs.
- **`excerpts` carry a verbatim `quote` plus offsets.** This is what converts "citation correctness" from an LLM opinion into a **string comparison**: normalise the quote, normalise the cited chunk's text, assert containment, and optionally assert the offsets agree. Deterministic, free, and gateable.
- **`claim_type`** ∈ `factual` \| `contextual` \| `procedural` \| `boundary`. Only `factual` claims require citations; the boundary disclaimer and the question card do not. Without this distinction, "citation completeness" is unmeasurable and would punish correct behavior.
- **Questions are separated from claims** because they are the one part of the answer that is legitimately uncited — which is also what makes them the safe degraded output when validation fails (§4.6).
- Generated via the OpenAI structured-outputs JSON-Schema mode, with a strict-parse fallback and a repair retry. Rendering the envelope back to the current six-section visual layout is a pure function, so the shadow-mode PR can prove byte-comparable UI output.

### 3.7 `EvalRunReport` — `reports/eval-<run_id>.json`

```json
{
  "schema_version": "evalrun/1.0",
  "run_id": "2026-08-12T14-03-22Z-a41f",
  "git_commit": "…", "synapse_version": "0.3.0",
  "dataset_version": "ed-0.1", "corpus_version": "corpus-v2",
  "source_pack_version": "sp-2026.08.1", "index_id": "corpus-v2-2026-08-12-a41f",
  "mode": "offline_deterministic",
  "config": {"k_values": [3, 5, 10], "fusion": "linear", "alpha": 0.7,
             "reranker": "keyword", "generation": "cassette"},
  "counts": {"cases_total": 120, "cases_run": 120, "cases_skipped": 0,
             "cases_clinician_reviewed": 0},
  "deterministic_metrics": {
    "recall_at_5":  {"value": 0.71, "n": 92, "ci95": [0.62, 0.79], "gate": "blocking"},
    "emergency_sensitivity": {"value": 0.94, "n": 18, "gate": "advisory",
                              "gate_reason": "labels unreviewed (ed-0.1)"}
  },
  "judge_metrics": {
    "claim_groundedness": {"value": 0.88, "n": 340, "gate": "never",
                           "judge_model": "…", "judge_prompt_sha256": "…",
                           "self_consistency_n": 3, "agreement": 0.91}
  },
  "per_case": [ { "case_id": "syn-emergency-0007", "…": "…" } ],
  "provenance_warnings": [
    "0 of 120 cases are clinician_reviewed; all behavioral gates are advisory."
  ]
}
```

`deterministic_metrics` and `judge_metrics` are **separate top-level objects**. There is no merged "quality score". `provenance_warnings` is non-empty until real review records exist, and the Markdown renderer prints it above the metrics table so no reader mistakes an unreviewed run for a validated one.

---

## 4. Module boundaries and public interfaces

Dependency direction is strictly downward; `schemas` depends on nothing internal, and nothing imports `app`.

```
app / scripts
      │
      ├──► ui ──────► answer ──► citation ──► generation ──► retrieval
      │                    │             │                           │
      ├─────────────► evaluation ────────┤                           │
      │                    │             │                           │
      └──► telemetry ◄─────┴─────────────┴──► evidence ──► index ──► corpus
                                                                       │
                                                     schemas ◄─────────┘
```

**`ui` was added in the answer-rendering change (2026-08-20)** and is the only
new boundary since this document was written. It exists because `app.py` was
making decisions — which sections to show, what to do when generation failed —
inside a file excluded from lint, type-checking and tests. See §4.8 and
docs/answer-rendering.md.

`evaluation` may import everything below it; **nothing imports `evaluation`** — the app must not depend on the eval layer, which is precisely the coupling that produced C1/C2. Metrics are computed by the runner from records the app emits, not by the app calling the evaluator inline.

### 4.1 `synapse.corpus`

```python
def write_chunks(path: Path, chunks: Iterable[ChunkRecord]) -> ArtifactRef: ...
def read_chunks(path: Path) -> Iterator[ChunkRecord]: ...
def load_chunks(path: Path) -> list[ChunkRecord]: ...          # ordered, validated
def content_hash(text: str) -> str: ...
def normalize(text: str) -> str: ...
def make_chunk_id(doc_id: str, ordinal: int) -> str: ...
def dedupe_documents(docs: Iterable[DocumentRecord]) -> tuple[list[DocumentRecord], DedupReport]: ...
```
`legacy_pickle.load_legacy_chunks(path)` is quarantined here: a `RestrictedUnpickler` that permits exactly `Data.fetch_and_chunk.Chunk` / `synapse.data.fetch_and_chunk.Chunk` and raises on anything else — the same technique used to audit the artifacts during this inspection. It is importable only by `scripts/migrate_pickles.py`, enforced by an import-linter contract in CI, and deleted in PR 15.

### 4.2 `synapse.index`

```python
def build_manifest(corpus_dir: Path, cfg: BuildConfig) -> IndexManifest: ...
def read_manifest(corpus_dir: Path) -> IndexManifest: ...
def verify(corpus_dir: Path, *, strict: bool = True) -> VerificationReport: ...   # raises IndexIntegrityError
def load_verified(corpus_dir: Path) -> LoadedIndex: ...   # the ONLY supported load path
```
`LoadedIndex` is a frozen bundle of `(chunks, faiss_index, bm25, manifest)` whose construction is impossible without a passing verification. `HybridRetriever.load()` is reimplemented on top of it, and additionally asserts `[c.chunk_id for c in chunks] == manifest.ordering` before serving a single query.

### 4.3 `synapse.evidence`

```python
class SourceRegistry:
    @classmethod
    def load(cls, pack_dir: Path) -> "SourceRegistry": ...
    def get(self, doc_id: str) -> SourceRecord: ...
    def citation_for(self, doc_id: str, style: CitationStyle) -> str: ...
    def eligible_doc_ids(self, policy: EligibilityPolicy) -> frozenset[str]: ...
    def audit(self, *, today: date) -> GovernanceReport: ...   # expired / expiring / unreviewed / retracted
```
`EligibilityPolicy` is explicit and defaults to `exclude_retracted=True, require_approval=False`. Flipping `require_approval=True` before any approvals exist would empty the corpus — so the policy object records *what was applied* into the manifest and the eval report, and the flip is a deliberate, clinician-gated decision (§8, U1), never a silent default.

### 4.4 `synapse.evaluation`

```python
# dataset
def load_dataset(version: str) -> EvalDataset: ...             # validates every invariant in §3.4
def validate_against_corpus(ds: EvalDataset, corpus_dir: Path) -> list[ValidationError]: ...

# deterministic metrics — pure functions, no I/O, no network, fully unit-testable
def recall_at_k(retrieved: Sequence[str], relevant: Mapping[str, int], k: int) -> float: ...
def precision_at_k(retrieved: Sequence[str], relevant: Mapping[str, int], k: int) -> float: ...
def mrr(retrieved: Sequence[str], relevant: Mapping[str, int]) -> float: ...
def ndcg_at_k(retrieved: Sequence[str], relevant: Mapping[str, int], k: int) -> float: ...
def citation_completeness(env: AnswerEnvelope) -> CitationCompletenessResult: ...
def citation_correctness(env: AnswerEnvelope, chunks: Mapping[str, ChunkRecord]) -> CitationCorrectnessResult: ...
def abstention_correct(env: AnswerEnvelope, case: EvalCase) -> bool: ...
def emergency_confusion(rows: Iterable[tuple[bool, bool]]) -> ConfusionMatrix: ...   # -> sensitivity, specificity
def flesch_kincaid_grade(text: str) -> float: ...
def estimate_cost(usage: TokenUsage, table: PriceTable) -> Decimal: ...

# judges — separate namespace, separate base class, separate report section
class Judge(Protocol):
    id: str; prompt_sha256: str
    def score(self, item) -> JudgeScore: ...     # {value, rationale, model, n_samples, agreement}

# orchestration
def run(cfg: EvalConfig) -> EvalRunReport: ...
def compare(current: EvalRunReport, baseline: EvalRunReport, th: Thresholds) -> ComparisonReport: ...
def render_markdown(report: EvalRunReport, cmp: ComparisonReport | None) -> str: ...
```

**Metric definitions, fixed here to avoid drift:**

- `recall@k = |{d ∈ retrieved[:k] : rel(d) ≥ 1}| / |{d : rel(d) ≥ 1}|`; **undefined (reported as `null`, excluded from the mean)** when the denominator is 0 — never `0.0`. This is the direct fix for C5, where an unlabeled case silently reported perfect failure.
- `precision@k = |{d ∈ retrieved[:k] : rel(d) ≥ 1}| / min(k, |retrieved|)` — the current implementation divides by `k` even when fewer than `k` results exist, under-reporting precision on short result lists.
- `MRR = 1 / rank of first d with rel(d) ≥ 1`, else `0.0`. Reported over labelled cases only.
- `nDCG@k = DCG@k / IDCG@k` with `DCG@k = Σᵢ (2^rel(dᵢ) − 1) / log₂(i + 1)`, `IDCG@k` from the ideal graded ordering. Requires the graded `relevance` field — which is why §3.4 grades 0–3 rather than using a binary set.
- **Deduplication before scoring:** retrieved IDs are deduplicated by `doc_id` for document-level metrics. Without this, C6's duplicates would inflate precision.
- `citation_completeness = |{c : c.claim_type == "factual" ∧ c.source_ids ≠ ∅}| / |{c : c.claim_type == "factual"}|`.
- `citation_correctness` (deterministic) `= |{excerpts whose normalised quote is a substring of the cited chunk's normalised text}| / |all excerpts|`. Cited chunk must also be in the retrieved set for that query — a citation to a chunk that was never retrieved is a hard failure, not a partial credit.
- `abstention_correctness` = accuracy of `env.abstained` against `case.expected_behavior == "abstain"`, reported with the full confusion matrix (over-abstention and under-abstention are clinically different failures and must never be averaged into one number).
- `emergency_sensitivity = TP/(TP+FN)`, `emergency_specificity = TN/(TN+FP)`, with **Wilson score intervals** — at n=18 emergency cases a point estimate alone is not decision-grade.
- `readability`: Flesch–Kincaid grade `0.39·(words/sentences) + 11.8·(syllables/words) − 15.59`, vendored (~40 lines, no dependency) so the number is reproducible forever. Computed over patient-facing prose only, excluding the boundary statement and question card.
- `latency`: per-stage (`embed`, `vector_search`, `bm25`, `fuse`, `rerank`, `generate`, `validate`) plus wall clock, reported as p50/p95, never as a mean.
- `token_usage`: `prompt_tokens` / `completion_tokens` per LLM call, read from `response.usage` — currently discarded (M8).
- `estimated_cost`: `Decimal` arithmetic against `config/pricing.toml`, which carries `price_table_version` and `effective_date`. **Prices are populated by the team from the provider's current pricing page; no price is hardcoded in source and none is asserted in this document.** A run whose `price_table.effective_date` is older than 90 days emits a report warning.

### 4.5 `synapse.citation`

```python
def extract_claims(env: AnswerEnvelope) -> list[Claim]: ...
def align_excerpt(ex: Excerpt, chunk: ChunkRecord) -> AlignmentResult: ...   # exact | normalized | fuzzy | none
def check_numeric_consistency(claim: Claim, chunks: Sequence[ChunkRecord]) -> NumericCheckResult: ...
def validate(env: AnswerEnvelope, ctx: ValidationContext) -> ValidationReport: ...
def decide(report: ValidationReport, policy: CitationPolicy) -> Decision: ...  # PUBLISH | DEGRADE | ABSTAIN
```

`check_numeric_consistency` is worth calling out: every number, unit, percentage and range in a factual claim must appear in at least one cited chunk (with unit-aware normalisation — `2-3 months` matches `2–3 months`, `7%` matches `7 %`). In patient-facing medical text, fabricated or drifted numbers — thresholds, dosages, durations — are the highest-consequence hallucination class, and this catches them **deterministically, offline, for free**.

### 4.6 `CitationPolicy` and the fail-closed ladder

```toml
[citation_policy]
mode = "shadow"                      # off | shadow | enforce   ← flipped in PR 13, not PR 11
min_factual_claim_citation_rate = 1.00
min_excerpt_alignment_rate      = 0.95
require_numeric_consistency     = true
min_retrieval_score             = 0.35
min_supporting_chunks           = 1
```

| Condition | Decision | What the patient sees |
|---|---|---|
| All checks pass | `PUBLISH` | full answer, per-claim source chips |
| Some factual claims unsupported, ≥1 supported | `DEGRADE` | supported claims only; unsupported ones dropped, with an explicit "we couldn't verify some information" notice |
| No factual claim survives, or numeric inconsistency, or retrieval below threshold | `ABSTAIN` | no generated medical content; the question card (uncited by design) plus a "bring this to your doctor" framing |
| Emergency detected | `ROUTE` | existing emergency card — unchanged, and validated *before* any of the above |

`DEGRADE` and `ABSTAIN` are the fail-closed behaviors. In `shadow` mode the decision is computed, recorded and reported but **never applied**, so PR 11 can quantify exactly how often enforcement would have changed the output before PR 13 turns it on.

### 4.7 `synapse.telemetry`

```python
def query_fingerprint(query: str, salt: SecretStr) -> str: ...     # HMAC-SHA256, salt from env, never logged
def redact(text: str) -> str: ...
@dataclass(frozen=True)
class QueryRecord:
    query_sha256: str; category: str | None; char_bucket: str; token_count: int
    stage_latencies_ms: dict[str, float]; token_usage: TokenUsage; estimated_cost: Decimal
    retrieved_chunk_ids: list[str]; decision: str; abstained: bool; is_emergency: bool
    query_text: str | None = None        # populated ONLY when allow_raw_text=True
```

**Raw patient text is off by default and structurally hard to turn on.** `query_text` requires `SYNAPSE_ALLOW_RAW_QUERY_LOGGING=1` **and** an explicit `allow_raw_text=True` argument; a logging filter drops any record carrying it when the env var is absent; `tests/privacy/` asserts that a corpus of synthetic PHI-shaped strings never appears in any emitted record, report, or log line. Eval-run records set `contains_authored_text=True` because eval queries are authored, not patient-derived — that flag is the only sanctioned path for readable query text in an artifact.

### 4.8 `synapse.ui`

Added 2026-08-20. The seam between the Streamlit application and the answer
layer, so that `app.py` contains presentation and nothing else.

```python
# synapse.ui.pipeline
def answer_turn(query: str, *, retrieve, client, is_emergency, policy=None) -> TurnOutcome: ...
def emergency_turn() -> TurnOutcome: ...

@dataclass(frozen=True)
class TurnOutcome:            # exactly one of the two is populated; enforced in __post_init__
    presentation: AnswerPresentation | None
    failure: AnswerFailure | None

# synapse.ui.errors
class AnswerFailureCode(StrEnum): ...          # closed set; the value rendered as a reference code
PATIENT_ERROR_MESSAGE: str                     # one generic message for every failure
def classify(exc: BaseException) -> AnswerFailure: ...
```

Three properties this boundary buys:

- **Ordering is enforced in typed, tested code.** Red-flag check before
  retrieval, retrieval before generation, verification before display. `app.py`
  supplies the three injection points and cannot reorder them.
- **A failure cannot become an answer.** `TurnOutcome` is exclusive, and there
  is no code path from an exception to rendered medical content — which is what
  the previous fallback branch was.
- **The network stays out of the tests.** Retrieval and generation are injected,
  so every branch including each failure mode runs offline against fakes.

`synapse.ui` imports `synapse.answer`, never the reverse; rendering itself stays
in `synapse.answer.render`, which remains the only module that turns a validated
answer into markup. `synapse.ui.legacy_evidence` is a **deprecated** adapter for
the un-migrated `Retrieval/` result shape, scheduled for removal on 2026-11-30
and enforced by a dated test.

---

## 5. Migration sequence

Ordering is constrained by three hard dependencies discovered during inspection:

1. **Pickles name `Data.fetch_and_chunk.Chunk` (P2).** Renaming the package before migrating the artifacts makes them unloadable. → **Corpus migration (PR 4) must read the legacy pickle through a compatibility alias registered by the migrator**, and the old module path must survive until PR 4 lands.
2. **Fixing the import bug (C1) reactivates the raw-query logger (§1.6).** → **Privacy-safe telemetry (PR 3) must land before the evaluator is reconnected (PR 9).**
3. **Eval labels are corpus-version-scoped.** Re-ingesting the corpus (PR 7) invalidates chunk-level labels. → **Author the dataset (PR 8) against corpus-v2, after re-ingest, not before.**

```
PR 0  Repo hygiene, packaging, CI skeleton                    ── no behavior change
PR 1  Import correctness + package canonicalization           ── fixes C1, C3, M1, M2, M3
PR 2  Typed schemas + JSON Schema export                      ── no wiring
PR 3  Privacy-safe telemetry + run records                    ── §1.6 closed before it can open
PR 4  Corpus migration: pickle → JSONL, stable IDs, dedup     ── fixes C6, C7; dual-read
PR 5  Index manifest + integrity + pickle-free BM25           ── fixes C8, P1, P3, P5
PR 6  Source packs + evidence governance registry             ── all records `unreviewed`
PR 7  Corpus re-ingest v2 (full abstracts, metadata)          ── fixes C9, C10, C11, C12
PR 8  Evaluation dataset v0.1 (schema-valid, unreviewed)      ── fixes C4
PR 9  Deterministic metrics + offline runner + reports        ── fixes C2, C5
PR 10 Structured AnswerEnvelope (shadow mode)                 ── H2 groundwork
PR 11 Deterministic citation validation (shadow mode)         ── measures H2
PR 12 LLM-judge suite (nightly, advisory, never gating)
PR 13 Fail-closed enforcement + abstention UX                 ── fixes H1, H2; flips the flag
PR 14 Emergency routing v2 + measured sensitivity/specificity ── fixes H4
PR 15 CI gates blocking + baselines + release process         ── fixes M6; deletes shims
```

**Prerequisite (PR 0):** the working tree is not a git repository. `git init`, an initial commit of the current state, and a remote are prerequisites for a PR-based plan, and for the eval-dataset versioning and regression baselines that everything downstream assumes. The initial commit must **force-add** the artifacts (`git add -f`) or, preferably, fix `.gitignore` (P4) in the same commit.

**Rollback:** every PR from 4 onward keeps its predecessor's read path alive behind `SYNAPSE_ARTIFACT_MODE=legacy|v2`, and PRs 10–14 are flag-gated. Rollback is an env-var flip plus a revert, never a data restore.

**Behavior-preservation gate — the same three checks run on every PR 4–14:**
1. **Golden ranking parity** — 25 fixed queries, recorded embeddings, assert identical top-10 `chunk_id` ordering before and after (with a documented, reviewed diff where a fix intentionally changes ranking, e.g. PR 4's duplicate collapse).
2. **Rendered-output parity** — the same envelope/answer renders to byte-identical HTML in shadow mode.
3. **Startup parity** — app boots, answers a canned query, and shows sources, with the new path enabled.

---

## 6. Test strategy

Five layers. Everything in layers 1–4 runs offline, with no API key, in under two minutes.

### 6.1 The network kill-switch (`tests/conftest.py`)

An autouse fixture that (a) monkeypatches `socket.socket` to raise `NetworkAccessInTestError` for any non-loopback connection, (b) deletes `OPENAI_API_KEY` from the environment, and (c) monkeypatches `openai.OpenAI` to a `FakeOpenAI` that serves recorded cassettes and raises on a cassette miss. Tests needing real network must be marked `@pytest.mark.live` and are deselected by default (`addopts = "-m 'not live'"`). **This is the single control that makes "no live OpenAI calls in normal CI" structural rather than aspirational** — a test cannot accidentally call the API even if someone sets a secret.

### 6.2 Unit tests — pure functions

- **Metrics against hand-worked examples.** Every formula in §4.4 gets textbook cases plus adversarial ones: empty relevant set → `null` not `0.0` (regression test for C5); `k > len(retrieved)`; all-relevant; graded nDCG verified against a manually computed IDCG; duplicate IDs in the retrieved list (regression for C6/C7).
- **Property-based tests** (`hypothesis`, dev-only): `0 ≤ metric ≤ 1` for all inputs; `recall@k` monotonically non-decreasing in `k`; `nDCG@k == 1.0` for the ideal ordering; `precision@k == recall@k` when `|relevant| == k`; ID generation is injective over (doc_id, ordinal).
- **Normalisation/hashing round-trips**: `normalize` is idempotent; `content_hash` is stable across serialisation cycles and unaffected by cosmetic whitespace.
- **Readability**: FK grade against published worked examples, ±0.1.
- **Emergency detector**: the negation and substring cases from H4 as explicit named tests — `"I do not have chest pain"`, `"no suicidal thoughts"`, `"mild shortness of breath for three months"`, `"crushing pressure in my chest"`, `"slurred speech"`. These are **characterization tests first** (asserting today's wrong behavior, marked `xfail(strict=True)`), flipping to correctness assertions in PR 14. That way the fix is provably the thing that changed the outcome.

### 6.3 Contract tests — schema and invariant enforcement

- Every `evaldata/*/cases.jsonl` and `evidence/*/sources.jsonl` record validates against its committed JSON Schema.
- All §3.3/§3.4 cross-field invariants, each as a named test.
- **Approval-language lint**: no report template, UI string, or docstring may contain "clinician-approved", "physician-reviewed", "validated by" or similar unless the code path guarantees a matching `approval_status`. Implemented as a regex sweep over templates plus a runtime assertion in the report renderer.
- **Reviewer-ID lint**: `reviewer_id` must match `^rev_[a-z0-9]{8}$`; anything containing `@`, a space, or `Dr` fails.
- **Referential integrity**: every `doc_id`/`chunk_id` in the dataset exists in the named corpus version. *This test alone would have caught C4.*
- **Import-graph contract** (`import-linter`): `synapse.retrieval` may not import `synapse.evaluation`; nothing outside `scripts/` may import `synapse.corpus.legacy_pickle`; no module may import `streamlit` except `app.py`.
- **Case-sensitivity guard**: a test that imports every module by its canonical dotted path, plus a filesystem sweep asserting no two paths differ only in case. Runs on Linux in CI, where the C1/C3 failures are unambiguous.

### 6.4 Golden tests — determinism and parity

- `tests/fixtures/mini_corpus/` — ~40 chunks with a committed manifest, small enough to eyeball, large enough for meaningful ranking.
- `tests/fixtures/query_embeddings.npz` — recorded query vectors for the 25 golden queries, so the **entire retrieval + fusion + metric path runs with zero API calls and bit-identical results**.
- Golden ranking parity (§5) — the primary regression detector for PRs 4, 5, 7.
- Golden report snapshots: a fixed `EvalRunReport` renders to a committed Markdown file; diffs are reviewed like code.
- LLM cassettes for generation and judges, keyed by `sha256(model + prompt + input)`; refreshed only by an explicit `make refresh-cassettes` run.

### 6.5 Integrity and adversarial tests

- `tests/fixtures/artifacts_bad/` — a manifest with a mutated chunk hash, a FAISS file with mismatched `ntotal`, a corpus reordered by one row, a wrong embedding model, a truncated `.faiss`. Each must raise `IndexIntegrityError` **before** any query executes.
- **Pickle-refusal tests**: `synapse.corpus.store` raises on being handed a `.pkl`; the restricted unpickler rejects a stream containing `posix.system`/`builtins.eval` (constructed in-test, never executed).
- **Privacy tests**: run 50 synthetic PHI-shaped queries end-to-end against cassettes; assert that no emitted record, log line, or report contains any query substring longer than 12 characters; assert `query_sha256` differs across salts and is stable within one.
- **Fail-closed tests**: envelopes with fabricated excerpts, fabricated numbers ("below 8%" cited to a chunk saying "below 7%"), citations to non-retrieved chunks, and zero factual claims — each must yield the documented `DEGRADE`/`ABSTAIN` decision.

### 6.6 Live tests (excluded from PR CI)

Marked `@pytest.mark.live`, run in the nightly workflow with a secret: PubMed E-utilities contract (element paths still valid — the guard against a silent recurrence of C9/C10), OpenAI structured-output conformance, embedding dimension stability, and cassette staleness detection.

---

## 7. CI gate design

Three workflows with strictly separated authority.

### 7.1 `ci.yml` — PR gate (blocking, offline, target < 5 min)

Ubuntu (case-sensitive by design), Python 3.11, `pip install -e ".[dev]"` from `requirements.lock`.

| Stage | Blocking | Notes |
|---|---|---|
| `ruff check` + `ruff format --check` | ✅ | |
| `mypy --strict synapse/schemas synapse/evaluation synapse/citation synapse/index` | ✅ | strict on new code; legacy modules on the permissive profile until PR 15 |
| `import-linter` contracts | ✅ | §6.3 |
| Case-sensitivity + import-graph tests | ✅ | permanent guard against C1/C3 |
| Schema validation of `evaldata/` + `evidence/` | ✅ | |
| Referential integrity vs corpus manifest | ✅ | permanent guard against C4 |
| Artifact integrity on fixture corpus | ✅ | |
| `pytest -m "not live"` with coverage floor | ✅ | ≥ 90% on `synapse/evaluation`, `synapse/citation`, `synapse/index`, `synapse/schemas` |
| Offline deterministic eval on the full dataset | ✅ | recorded embeddings + cassettes; **zero API calls** |
| Regression comparison vs baseline | ✅ (gated metrics only) | §7.3 |
| Secret scan (`gitleaks`) + `pip-audit` | ✅ | |
| Report artifacts uploaded + `summary.md` → job summary | — | always, including on failure |

A guard step **fails the job if `OPENAI_API_KEY` is present in the environment**, making accidental live spend in PR CI impossible.

### 7.2 `eval-nightly.yml` — scheduled + manual dispatch (advisory)

Runs the full pipeline live: real embeddings, real generation, LLM judges, latency and cost measurement. Publishes JSON + Markdown reports and opens/updates a tracking issue on regression. **It cannot block a merge.** It refreshes cassettes on request and, on a green manual run, proposes a baseline update as a PR (never an auto-commit).

### 7.3 Threshold and gate policy (`config/thresholds.toml`)

```toml
[meta]
dataset_version = "ed-0.1"
baseline = "reports/baselines/ed-0.1.json"

# gate: "blocking" (fails CI) | "advisory" (reports only) | "never" (judge metrics)
[gates.recall_at_5]          { gate = "blocking", min_absolute = 0.60, max_regression = 0.03 }
[gates.ndcg_at_5]            { gate = "blocking", min_absolute = 0.55, max_regression = 0.03 }
[gates.mrr]                  { gate = "advisory", min_absolute = 0.50, max_regression = 0.05 }
[gates.citation_completeness]{ gate = "blocking", min_absolute = 1.00, max_regression = 0.00 }
[gates.citation_correctness] { gate = "blocking", min_absolute = 0.95, max_regression = 0.02 }
[gates.abstention_accuracy]  { gate = "advisory", min_absolute = 0.80, max_regression = 0.05 }
[gates.emergency_sensitivity]{ gate = "advisory", min_absolute = 0.95, max_regression = 0.00,
                               promote_to_blocking_when = "labels_clinician_reviewed" }
[gates.emergency_specificity]{ gate = "advisory", min_absolute = 0.70, max_regression = 0.05 }
[gates.readability_grade]    { gate = "advisory", max_absolute = 9.0 }
[gates.latency_p95_ms]       { gate = "advisory", max_absolute = 12000 }
[gates.estimated_cost_usd]   { gate = "advisory", max_absolute = 0.05 }
[gates.claim_groundedness]   { gate = "never" }
[gates.citation_semantics]   { gate = "never" }
```

Four rules make this honest:

1. **Every number above is a placeholder.** Thresholds are set from the first green baseline run, then ratcheted — never guessed in advance and never asserted as clinically validated. The file ships in PR 9 with all gates `advisory`; PR 15 promotes the deterministic retrieval and citation gates to `blocking`.
2. **`gate = "never"` is enforced in code**, not by convention: `thresholds.py` raises `ConfigurationError` if any metric under `judge_metrics` is configured as blocking. A judge cannot gate a merge even by misconfiguration.
3. **`promote_to_blocking_when = "labels_clinician_reviewed"`** is machine-checked: the gate becomes blocking automatically once ≥ 95% of emergency-category cases carry `review_status == "clinician_reviewed"` with a valid `ReviewRecord`. Until then the report states plainly that the gate is advisory *and why*.
4. **Regression is directional and paired.** A comparison is only valid when `dataset_version`, `corpus_version` and `index_id` match the baseline; otherwise the job reports `INCOMPARABLE` and requires an explicit baseline rebaseline PR. This prevents the classic false-green where a corpus change silently resets the reference point.

### 7.4 Reports

Machine-readable `reports/eval-<run_id>.json` (§3.7) plus a Markdown report with: run provenance header, **provenance warnings first**, a deterministic-metrics table with baseline delta and gate status, a clearly separated judge-metrics table stamped *"advisory — LLM-judged, not clinically validated"*, the ten worst-performing cases, a governance summary (unreviewed / expiring / retracted counts), and a cost + latency panel. The Markdown is written to `$GITHUB_STEP_SUMMARY` so reviewers see it without downloading artifacts.

### 7.5 `governance.yml` — weekly

Runs `scripts/governance_audit.py`: expired approvals, approvals due within 30 days, `retraction_status == "unchecked"` counts, sources older than the recency policy, and orphaned `doc_id`s. Opens an issue when action is needed. Advisory; it informs the clinical reviewer's queue rather than blocking engineering.

---

## 8. Risks and unresolved clinical decisions

### 8.1 Unresolved clinical decisions — these block claims, not code

| # | Decision | Why it cannot be made by engineering | Blocks |
|---|---|---|---|
| **U1** | **Who reviews sources, and may unreviewed content be shown to patients at all?** Today all 847 documents are unreviewed. Either the product ships unreviewed evidence (status quo, and must say so) or `require_approval=True` empties the corpus until review capacity exists. | Determines whether the product is patient-facing in its current form. | Source pack v1.0; §4.3 eligibility flip |
| **U2** | **Emergency-routing ground truth.** Which presentations must route? Where does the sensitivity/specificity trade-off sit — how many false emergency routings per true catch is acceptable in a waiting room? | This is the highest-consequence decision in the product and is a clinical risk judgement. | Blocking emergency gates (§7.3); PR 14 |
| **U3** | **Abstention threshold.** How weak must evidence be before saying nothing? Over-abstention degrades the product; under-abstention risks unsupported medical statements. | Risk-appetite call. | PR 13 enforcement flip |
| **U4** | **Should the patient-facing "% confidence" exist at all?** It currently reports an LLM's relevance opinion in a position where a patient reads clinical confidence (H3). | Patient-comprehension and liability question. | PR 10/13 UI |
| **U5** | **Evidence recency and hierarchy policy.** The corpus is 2025-only primary literature with no guidelines (C12). What is the required mix, and what is the maximum age before re-review? | Defines `review_due_date` and the ingest query strategy. | PR 7 ingest config |
| **U6** | **Retraction handling.** Hard-exclude retracted work, or surface with a warning? What about expressions of concern and corrections? | Clinical-governance policy. | PR 6/7 |
| **U7** | **Readability target.** Code comments claim an 8th-grade target; nothing measures it, and simplification can distort meaning. | Health-literacy judgement. | `readability_grade` threshold |
| **U8** | **Regulatory posture.** Whether this constitutes clinical decision support under applicable regulation, and whether query storage (even hashed) creates obligations. | Requires counsel and a regulatory assessment. **This document makes no regulatory determination.** | Production launch |
| **U9** | **May the same person author eval cases and review them?** Self-review of ground truth is not independent validation. | Governance policy. | `clinician_reviewed` semantics |

### 8.2 Engineering and methodological risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | **Corpus re-ingest (PR 7) invalidates existing labels.** Fixing C9/C10 changes chunk boundaries and IDs. | Eval dataset silently mismatched. | `corpus_version` on every case; referential-integrity gate fails loudly; author v0.1 *after* re-ingest (§5). |
| R2 | **Evaluation-set overfitting.** Cases authored by looking at retrieval output measure the retriever against itself. | Inflated metrics, false confidence. | `label_provenance` is mandatory; hold out 20% of cases as a sealed set opened only quarterly; report metrics on both splits separately. |
| R3 | **Judge–clinician agreement is unmeasured.** Groundedness judges may systematically disagree with clinical reality. | Judge scores mistaken for validation. | Judges never gate; report stamps them advisory; a clinician-adjudicated subset (~50 claims) measures agreement before judges influence any decision. |
| R4 | **Small-n emergency evaluation.** ~18 emergency cases cannot support a 0.95 sensitivity claim. | Overstated safety. | Wilson intervals reported alongside every point estimate; the gate stays advisory until both a clinician review (U2) and an adequate n exist. |
| R5 | **Deterministic citation checking rewards quoting, not accuracy.** A model can satisfy verbatim-excerpt matching while assembling quotes into a misleading claim. | False sense of groundedness. | Verbatim matching is a *necessary, not sufficient* condition — documented as such; numeric consistency adds a second deterministic layer; judges add a third advisory layer; §8.1 U1 remains the real control. |
| R6 | **Fail-closed abstention degrades the product.** If enforcement abstains often, the app becomes unhelpful. | Product regression at PR 13. | Shadow mode (PR 11) quantifies the would-be abstention rate before any flip; the flip is a separate, reversible PR with an env-var kill switch. |
| R7 | **Structured outputs change answer quality.** Constraining generation to a JSON schema can alter tone and completeness (H6 truncation interacts here). | Silent regression in patient experience. | Shadow-mode A/B with rendered-output parity plus readability comparison, before the flag flips. |
| R8 | **Cost and latency regression from validation.** Claim extraction and judges add calls. | Slower, pricier answers. | Deterministic validation is offline and free; judges run only in nightly; latency budgets are advisory gates from PR 9 so the trend is visible from the start. |
| R9 | **Cassette staleness.** Recorded LLM responses drift from live behavior; CI stays green while production breaks. | False confidence in offline gates. | Cassettes carry a recorded-at date; the nightly live run compares against them and flags divergence; a cassette older than 60 days emits a report warning. |
| R10 | **Pinned dependency set is unverified for 3.11** (M4/M5) and the local interpreter is 3.9.6. | Environment fails to build. | PR 0 produces a hash-pinned `requirements.lock` resolved on 3.11 in CI; the devcontainer and CI use the same lock. |
| R11 | **The pickle→JSONL migration is one-way and the source pickles are the only copy of the corpus** — and `.gitignore` currently excludes them (P4). | Irrecoverable corpus loss. | PR 0 fixes `.gitignore` and commits the artifacts before PR 4 touches anything; the migrator writes to a new directory and never deletes; a round-trip test asserts JSONL → in-memory equality with the restricted-unpickler read. |
| R12 | **Reviewer pseudonymisation can leak** through free-text `notes` or commit authorship. | Re-identification of reviewers. | Regex lint on `notes`; reviews are committed by an integration account; the ID↔identity map never enters the repository. |
| R13 | **Schema churn breaks stored artifacts.** | Reports and datasets become unreadable. | `schema_version` on every record; readers accept `MINOR` ≥ own within a `MAJOR`; migration scripts required for `MAJOR` bumps, with round-trip tests. |
| R14 | **Threshold ratcheting becomes theatre** if thresholds are lowered whenever CI goes red. | Gates stop meaning anything. | Threshold changes require a separate PR touching only `thresholds.toml`, with a written justification in the PR body; `git blame` on that file is the audit trail. |

---

## 9. PR-by-PR implementation plan

Each PR lists scope, acceptance criteria, and the behavior-preservation argument. **Every PR from 4 onward must pass the three parity checks in §5.**

---

### PR 0 — Repository hygiene, packaging, CI skeleton
**Scope.** `git init` + initial commit of current state. Fix `.gitignore` (P4) so `artifacts/`, the corpus, and the FAISS index are trackable while `venv/`, `__pycache__`, `.env`, `reports/*` (except baselines) and any `eval_log*.json` stay excluded; commit the existing artifacts. Add `pyproject.toml` (setuptools, `requires-python = ">=3.11,<3.12"`, `[project.optional-dependencies] dev`), `.python-version`, a hash-pinned `requirements.lock` resolved on 3.11, `Makefile`, ruff/mypy/pytest configuration, and a `ci.yml` that runs lint + an empty test suite. Reconcile `requirements.txt` with actual imports (M4): add `langchain-community`, drop `plotly` and `tiktoken` unless a consumer is identified, and verify every pin resolves for cp311.

**Acceptance criteria.**
- `pip install -e ".[dev]"` succeeds on a clean Python 3.11 environment from the lock file, on Linux and macOS.
- `git clone && make setup && streamlit run app.py` reaches a working UI **including corpus artifacts** — the P4 failure is demonstrably gone.
- CI green; `ruff check` passes (with a documented, time-boxed ignore list for legacy modules).
- **No file under `Data/`, `Retrieval/`, `Generation/`, `Evaluation/`, or `app.py` is modified.**

---

### PR 1 — Import correctness and package canonicalization
**Scope.** Fixes **C1, C3, M1, M2, M3**. Create `synapse/` and move `Data → synapse/data`, `Retrieval → synapse/retrieval`, `Generation → synapse/generation`, `Evaluation → synapse/evaluation` (two-step `git mv` on case-insensitive filesystems; set `core.ignorecase false`). Leave deprecation shims at the old paths re-exporting from `synapse.*`. Correct the three broken import sites ([app.py:458](../app.py#L458), [app.py:643](../app.py#L643), [answer_generator.py:245-247](../Generation/answer_generator.py#L245-L247)). Delete all seven `sys.path` mutations, `from ast import If`, and the three import-time `print("WORKS")` calls. Register `Data.fetch_and_chunk.Chunk` as a module alias so **existing pickles still load** (P2).

**Acceptance criteria.**
- `find_spec` resolves every module by its canonical dotted path; the case-sensitivity test passes on Linux.
- `python -c "import synapse.data.fetch_and_chunk"` emits nothing on stdout.
- `run_pipeline` is importable and callable (previously impossible).
- Existing `processed_chunks.pkl` and `hybrid_index/` load unchanged under the new package names — asserted by a test.
- **`app.py`'s two evaluator call sites are corrected but wrapped in a feature flag defaulting to OFF**, so the raw-query logger (§1.6) does not activate before PR 3. This is stated explicitly in the PR description.
- Golden ranking parity: identical top-10 for all 25 queries.

---

### PR 2 — Typed schemas and JSON Schema export
**Scope.** Implement `synapse/schemas/` per §3: `ChunkRecord`, `DocumentRecord`, `SourceRecord`, `ReviewRecord`, `EvalCase`, `IndexManifest`, `AnswerEnvelope`/`Claim`/`Excerpt`, `EvalRunReport`, and `enums.py`. Export and commit JSON Schemas. Add contract tests for every §3.3/§3.4 invariant. Promote `pydantic` to a direct dependency. **Nothing imports these models yet.**

**Acceptance criteria.**
- `mypy --strict synapse/schemas` clean.
- Every invariant in §3.3 and §3.4 has a named passing test, including the negative cases (`approved` without a review record → `ValidationError`; `reviewer_id = "Dr. Smith"` → `ValidationError`).
- Committed JSON Schemas regenerate byte-identically (`make schemas && git diff --exit-code`).
- Approval-language lint and reviewer-ID lint are in place and passing.

---

### PR 3 — Privacy-safe telemetry and run records
**Scope.** Implement `synapse/telemetry/` per §4.7. Replace `eval_log.json` entirely: it is never written again. Wire `QueryRecord` emission into the app path (hash-only) behind `SYNAPSE_TELEMETRY=on|off`, default `off` for this PR.

**Acceptance criteria.**
- `tests/privacy/` passes: 50 synthetic PHI-shaped queries produce no record, log line, or report containing any query substring > 12 characters.
- Enabling raw logging requires **both** the env var and an explicit argument; a test proves the env var alone is insufficient and the argument alone is insufficient.
- `query_sha256` is stable within a salt and differs across salts; the salt never appears in output.
- No code path writes `eval_log.json`; a test asserts the file is not created during a full app-path run.

---

### PR 4 — Corpus migration: pickle → JSONL, stable IDs, deduplication
**Scope.** Fixes **C6, C7**. Implement `synapse/corpus/` and `scripts/migrate_pickles.py`, which reads the legacy pickle through the restricted unpickler, deduplicates at document level, assigns `doc_id`/`chunk_id` per §3.1, computes `content_sha256`, `char_start`/`char_end`, and token counts, and writes `artifacts/corpus-v2/{chunks,documents}.jsonl.gz`. Retrieval gains a dual read path (`SYNAPSE_ARTIFACT_MODE=legacy|v2`, default `legacy`).

**Acceptance criteria.**
- Migration report states exactly: documents in / documents out / duplicate documents removed / duplicate chunk IDs resolved / chunks below `min_chunk_chars` dropped. Expected from the current artifact: **2,220 chunks in, 162 chunk-ID collisions resolved, 163 exact-duplicate texts collapsed, 12 sub-50-character chunks dropped.**
- `len(set(chunk_ids)) == len(chunk_ids)` — asserted, not assumed.
- Round-trip test: JSONL read reproduces the restricted-unpickler read field-for-field for every non-deduplicated chunk.
- With `SYNAPSE_ARTIFACT_MODE=legacy`, golden ranking parity is exact. With `v2`, differences are limited to duplicate-collapse effects and are enumerated in the PR description.
- The legacy pickle files are **not deleted**.

---

### PR 5 — Index manifest, integrity verification, pickle-free BM25
**Scope.** Fixes **C8, P1, P3, P5**. Implement `synapse/index/` per §3.5 and §4.2. `scripts/build_index.py` writes `index.faiss` + manifest. BM25 persistence drops `bm25.pkl`: rebuild at load from the JSONL corpus (benchmark it — at 2,220 chunks this is expected to be sub-second; if it exceeds a 500 ms startup budget, fall back to the JSON `bm25_stats` artifact, which is already specified in the manifest). `embeddings.npy` is no longer shipped (the FAISS index holds the vectors). `HybridRetriever.load()` is reimplemented on `load_verified()` and asserts chunk-ID ordering.

**Acceptance criteria.**
- Every fixture in `tests/fixtures/artifacts_bad/` raises `IndexIntegrityError` before any query runs — including the reordered-by-one-row corpus, which is the C8 scenario.
- `verify()` on the migrated real artifacts passes and records `ntotal=2220`, `dim=1536`, `IndexFlatL2`.
- No `.pkl` file is written by any code path outside `scripts/migrate_pickles.py`; enforced by an import-linter contract and a filesystem assertion.
- Artifact size drops by ≥ 15 MB (three duplicate corpus pickles + `embeddings.npy` removed).
- BM25 load time measured and reported in the PR; golden ranking parity exact.

---

### PR 6 — Source packs and evidence governance registry
**Scope.** Implement `synapse/evidence/` per §3.3 and §4.3. Generate `evidence/v0.1/sources.jsonl` from the migrated corpus. **All 847 records land as `approval_status: unreviewed`, `review: null`, `evidence_type: unclassified`, `retraction_status: unchecked`.** Add `scripts/governance_audit.py` and `governance.yml`. Write `docs/evidence-governance.md` with the review SOP and the pseudonymous-reviewer process.

**Acceptance criteria.**
- Every `doc_id` in the corpus resolves to exactly one `SourceRecord`; the reverse also holds.
- `SourceRegistry.audit()` reports `847 unreviewed, 0 approved, 0 expired, 847 retraction-unchecked` — and the report says so in plain language.
- Approval-language lint passes across the new pack and its templates.
- Attempting to construct an approved record without a `ReviewRecord` fails validation, with a test.
- `docs/evidence-governance.md` states unambiguously that no clinician review has occurred.

---

### PR 7 — Corpus re-ingest v2: full abstracts, complete titles, evidence metadata
**Scope.** Fixes **C9, C10, C11, C12**. Rewrite `synapse/ingest/pubmed.py`: iterate **all** `<AbstractText>` elements preserving `@Label` into `ChunkRecord.section`; extract titles with `itertext()`; capture `PubDate`, `Journal`, `AuthorList`, `PublicationType`, `DOI`, `PMCID`; add retraction/expression-of-concern screening; support NCBI API keys with correct rate limiting; add explicit `sort` and publication-type preferences per the recency/hierarchy policy (**gated on U5** — until that decision exists, the ingest config is checked in as an explicit, reviewable default, not an implicit one). Produce `corpus-v3` alongside `corpus-v2`; **do not switch the default.**

**Acceptance criteria.**
- Median abstract characters per document rises materially from the measured **573**; the PR reports the new distribution and the count of documents formerly under 400 characters (**currently 312/847**).
- Zero titles truncated at nested markup; the three known cases (`"…Serotonin 5-HT"`, `"Evaluating the cardio-protective effects of "`, `"…for detection of "`) are complete, asserted as named tests.
- ≥ 95% of documents carry a `publication_date`; `evidence_type_provenance` is set on 100%.
- A live contract test verifies the E-utilities element paths, so a future PubMed schema change fails loudly rather than silently truncating again.
- Default corpus remains `corpus-v2`; the switch is a follow-up PR after A/B eval.

---

### PR 8 — Evaluation dataset v0.1
**Scope.** Fixes **C4**. Author `evaldata/v0.1/cases.jsonl` — target ~120 cases across all categories in §3.4, including ≥ 20 emergency (with negation and long-standing-symptom distractors from H4), ≥ 15 abstention probes, ≥ 10 adversarial/out-of-scope. Label against the **current default corpus version** with graded relevance. Delete `DEFAULT_EVAL_SET`. Write `evaldata/LABELING_GUIDE.md` and `docs/evaluation-dataset.md`.

**Acceptance criteria.**
- Every case validates; **every referenced `doc_id`/`chunk_id` exists in the named corpus version** — the gate that C4 would have failed.
- Every case has `review_status: unreviewed` and `label_provenance: engineering_seed`. No case claims clinician review.
- Category and difficulty distributions are reported in the PR body.
- A sealed 20% holdout split is defined and excluded from routine reporting (R2).
- The emergency subset is explicitly flagged in `docs/` as **pending clinical review (U2)**, and its gates are advisory.

---

### PR 9 — Deterministic metrics, offline runner, reports
**Scope.** Fixes **C2, C5**. Implement `synapse/evaluation/metrics/` per §4.4, `runner.py`, `compare.py`, `report.py`, `thresholds.py`. Record `tests/fixtures/query_embeddings.npz` so the whole retrieval evaluation runs offline. Wire token/latency/cost capture (M8) — `response.usage` is read and recorded. Ship `config/thresholds.toml` with **all gates `advisory`** and `config/pricing.toml` with the price table left for the team to populate. Generate the first baseline. Rewrite the app sidebar to read the last committed report instead of computing metrics inline — the app no longer imports the evaluation package at all.

**Acceptance criteria.**
- `make eval` completes offline, with no API key, in under two minutes, and produces both JSON and Markdown reports.
- Empty-ground-truth cases report `null`, not `0.0`, and are excluded from means — the C5 regression test.
- Duplicate retrieved IDs do not inflate precision — the C6/C7 regression test.
- nDCG matches a hand-computed worked example.
- Report renders `provenance_warnings` above the metrics table, stating that 0 of N cases are clinician-reviewed.
- `thresholds.py` raises `ConfigurationError` when a judge metric is configured as blocking — tested.
- CI runs the eval and uploads reports; gates are advisory, so nothing blocks yet.

---

### PR 10 — Structured `AnswerEnvelope` (shadow mode)
**Scope.** Add structured-output generation per §3.6 behind `SYNAPSE_ANSWER_FORMAT=legacy|structured`, default `legacy`. Implement envelope → current-six-section renderer. Raise `max_tokens` and add a truncation detector (H6). Externalise and version prompts under `synapse/generation/prompts/` with recorded hashes.

**Acceptance criteria.**
- In `structured` mode against cassettes, the rendered HTML is byte-identical to `legacy` for all golden queries — parity proven, not assumed.
- Every claim carries a unique `claim_id`; `claim_type` is populated; questions are separated from claims.
- Truncated generations are detected and reported rather than silently rendered.
- Default remains `legacy`; no user-visible change.

---

### PR 11 — Deterministic citation validation (shadow mode)
**Scope.** Implement `synapse/citation/` per §4.5: excerpt alignment, numeric consistency, source resolvability, `ValidationReport`, and the `decide()` ladder — **computed and recorded, never applied** (`citation_policy.mode = "shadow"`). Add `citation_correctness` and `citation_completeness` to the deterministic metric set. Fix the unescaped-HTML rendering (H7) for any model-emitted or PubMed-derived text — flagged in the PR as a deliberate, minimal security fix rather than a behavior change.

**Acceptance criteria.**
- Fabricated excerpts, fabricated numbers, and citations to non-retrieved chunks each produce the documented decision, as named tests.
- The report states, for the full dataset, **how often enforcement would have changed the output** (`PUBLISH` / `DEGRADE` / `ABSTAIN` counts) — the number PR 13 needs.
- HTML escaping is verified by a test injecting `<script>` through the query, the answer, and a source title.
- No user-visible behavior change; `mode = "shadow"` asserted in a test.

---

### PR 12 — LLM-judge suite (nightly, advisory)
**Scope.** Implement `synapse/evaluation/judges/`: groundedness (claim vs cited chunks) and citation semantics. Judges run at temperature 0 with self-consistency `n=3` and record model ID, prompt hash, and inter-sample agreement. Add `eval-nightly.yml`. Add a clinician-adjudication export for ~50 claims so judge–human agreement can eventually be measured (R3).

**Acceptance criteria.**
- Judge metrics appear **only** under `judge_metrics`, stamped *"advisory — LLM-judged, not clinically validated"*.
- `judge_metrics` cannot be gated: the guard test from PR 9 covers every judge added here.
- PR CI makes zero API calls (asserted by the network kill-switch); the nightly workflow is the only live path.
- The adjudication export contains no reviewer identities and no patient-derived text.

---

### PR 13 — Fail-closed enforcement and abstention UX
**Scope.** Fixes **H1, H2**. Flip `citation_policy.mode` to `enforce` and `SYNAPSE_ANSWER_FORMAT` to `structured`, both behind a single documented kill switch. Add retrieval score thresholds, the abstention response, per-claim source attribution in the UI, and the `DEGRADE` presentation. Resolve **U4** — either remove the "% confidence" bar or relabel it accurately.

**Acceptance criteria.**
- Enforcement rates match PR 11's shadow-mode prediction within a stated tolerance; a material divergence blocks the flip.
- Abstention output contains no generated medical claims — only the question card and referral framing.
- Every displayed source is one the answer actually cited; a test asserts the "Research Sources" list is derived from `claims[].source_ids`, not from the reranked set (the H2 fix).
- Rollback is a single env-var flip, demonstrated in the PR.
- Requires an explicit decision record for **U3** and **U4** in the PR body.

---

### PR 14 — Emergency routing v2
**Scope.** Fixes **H4**. Extract `synapse/generation/emergency.py`; externalise terms to `config/emergency_terms.toml` with word-boundary matching, negation detection, duration/severity qualifiers, and a documented ordering (emergency check strictly precedes everything). Convert the PR 6.2 characterization tests to correctness assertions. Report sensitivity and specificity with Wilson intervals.

**Acceptance criteria.**
- All H4 cases behave correctly: negated mentions do not route; `"mild shortness of breath for three months"` does not route; `"crushing pressure in my chest"` and `"slurred speech"` do route.
- Emergency detection remains the first operation on every query — asserted by a control-flow test.
- Sensitivity and specificity reported with confidence intervals and explicit `n`.
- **The term list ships as `unreviewed` and the PR states plainly that it awaits clinical sign-off (U2).** The gate stays advisory; the `promote_to_blocking_when` machinery is in place and verified by a test that simulates reviewed labels.

---

### PR 15 — Blocking CI gates, baselines, release process
**Scope.** Fixes **M6**, closes the loop. Promote deterministic retrieval and citation gates to `blocking` at values derived from the accumulated baselines. Commit `reports/baselines/`. Add `docs/release-checklist.md`. Enable `mypy --strict` repo-wide. Delete the PR 1 compatibility shims and `synapse/corpus/legacy_pickle.py`. Rewrite `README.md` to match reality (M7) — correct structure, correct offline-mode claim, and an explicit statement of review status.

**Acceptance criteria.**
- A deliberate regression (an injected retrieval bug) fails CI on the blocking gate — the gates are proven to work, not just configured.
- A judge-metric regression does **not** fail CI — separation proven in both directions.
- A mismatched `dataset_version`/`corpus_version` produces `INCOMPARABLE`, not a false green.
- `mypy --strict` clean repo-wide; no `.pkl` reader remains in the codebase.
- README makes no claim about clinician review, offline operation, or accuracy that the repository cannot substantiate.
- `docs/release-checklist.md` enumerates exactly what must be green — and states which gates remain advisory pending **U1**, **U2**, **U3**.

---

## 10. Summary of what this plan does and does not deliver

**Delivers:** a corpus that cannot silently misalign with its index; artifacts that cannot execute code; evaluation that actually runs, on ground truth that provably exists; metrics whose deterministic and LLM-judged halves can never be confused; citations verified by string comparison rather than model opinion; numeric claims checked against their sources; abstention that is measurable before it is enforced; CI that spends nothing and blocks on evidence; and a governance layer that records what was reviewed, by whom (pseudonymously), and when it expires.

**Does not deliver:** clinical validation. Not one source, emergency label, abstention threshold, or readability target in this plan has been reviewed by a clinician, because the repository contains no review metadata and this plan invents none. Items **U1–U9** in §8.1 are decisions that engineering cannot make. The architecture is designed so that when those decisions arrive they are recorded as data — review records, thresholds, policy objects — rather than as assumptions buried in code, and so that until they arrive, every report says plainly that they have not.

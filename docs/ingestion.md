# Synapse PubMed Ingestion

**Applies to:** `synapse` 0.1.0 · `synapse.ingest`
**Status:** implemented and tested offline.
**Approval status:** nothing produced by this pipeline is medically approved. See §8.

---

## 1. What changed and why

The previous ingestion pipeline (`Data/fetch_and_chunk.py`) had three defects that
were measured directly against the corpus it produced.

### 1.1 Only the first abstract section survived

```python
abstract_el = article.find(".//AbstractText")   # returns ONE element
```

PubMed structured abstracts split into separate `<AbstractText>` elements —
BACKGROUND, METHODS, RESULTS, CONCLUSIONS. `find()` returns the first, so the
clinically actionable conclusion was discarded on every structured record.

Measured on the shipped corpus: **312 of 847 documents (37%) retained under 400
characters of abstract text in total**, median 573 — far below a normal PubMed
abstract. Observed chunks include ones whose entire text is `"."` or
`"Systematic review and meta-analysis."`.

Now: `findall("./Abstract/AbstractText")`, every section retained with its
`Label` and `NlmCategory`, in source order.

### 1.2 Titles were truncated at nested markup

```python
title = title_el.text    # stops at the first child element
```

A title containing `<i>`, `<sub>` or `<sup>` was cut at the first tag. The
shipped corpus contains `"Pharmacologic MRI Brain Imaging Studies of Serotonin
5-HT"`, `"Evaluating the cardio-protective effects of "` and `"Performance of
the Xpert Xpress Strep A for detection of "` — all rendered to patients as
source names.

Now: `flatten_element_text()` uses `itertext()`, walking the whole subtree.

### 1.3 No evidence metadata at all

The legacy `Chunk` dataclass had exactly eight fields: `text`, `source`,
`pmid`, `title`, `chunk_index`, `total_chunks`, `source_url`, `page`. No
publication date, no journal, no authors, no article type, no language, no
retraction status. Evidence governance was not merely incomplete — it was
unrepresentable.

Additionally: no timeouts (a hung connection blocked forever), no retries (one
transient 500 lost a whole topic), no deduplication (the same article entered
the corpus once per matching topic query), and no `sort` parameter — which is
why every PMID in the shipped corpus falls in a single recent band, with no
guidelines and no landmark trials.

---

## 2. Module layout

```
synapse/ingest/
├── eutils.py     NCBI client — pacing, bounded retries, timeouts, optional credentials
├── parse.py      PubMed XML → SourceDocument
├── evidence.py   publication types → normalised evidence type
├── dedupe.py     PMID → DOI → normalised title + year
├── chunker.py    section-aware, sentence-preserving chunking
└── pipeline.py   orchestration, governance screening, artifact writing
```

Everything with a side effect is injectable — the HTTP transport, the clock,
the sleep function — which is how the entire pipeline is tested against local
XML fixtures with no network access.

---

## 3. Metadata captured

| Field | Source | Notes |
|---|---|---|
| PMID | `<PMID>` | Required. A record without one is skipped: nothing can be cited. |
| DOI | `<ELocationID EIdType="doi">`, `<ArticleId IdType="doi">` | Normalised: lowercased, resolver prefix stripped |
| PMCID | `<ArticleId IdType="pmc">` | |
| Title | `<ArticleTitle>` | Via `itertext()`, so nested markup is retained |
| Authors | `<AuthorList>` | Personal **and** collective; `ValidYN="N"` entries skipped |
| Journal, ISSN, volume, issue, pages | `<Journal>`, `<Pagination>` | |
| Publication date | see §3.1 | With explicit precision |
| Article types | `<PublicationTypeList>` | Retained verbatim, and normalised separately |
| Language | `<Language>` | |
| Publication status | `<PublicationStatus>` | `ppublish`, `epublish`, `aheadofprint` |
| Source URL | constructed from PMID | **Not** taken from the payload — see §7 |
| Retrieval timestamp | caller-supplied | Timezone-aware |
| Abstract sections | `<Abstract><AbstractText>` | All of them, labelled and ordered |
| Retraction / correction | `<CommentsCorrectionsList>` + publication types | Direction preserved — see §5 |

### 3.1 Dates: electronic versus print

PubMed carries up to three date shapes, and they routinely disagree:

- `<JournalIssue><PubDate>` — the print/issue date;
- `<ArticleDate DateType="Electronic">` — the ahead-of-print date;
- `<PubDate><MedlineDate>` — free text such as `"2024 Jan-Feb"` or `"1999 Winter"`.

All three are captured. The canonical `publication_date` is the **earliest** of
the electronic and print dates, because an article that appeared online in
November was available to readers in November — using the following March's
issue date would overstate how recent the evidence is.

`publication_date_precision` records granularity (`day` / `month` / `year` /
`unknown`), so a date known only to the year is never compared as though exact.
A `MedlineDate` with no structured components has its year recovered by regex
and is preserved verbatim in `medline_date_raw`.

### 3.2 Collective authors

Study groups appear as `<CollectiveName>` with no personal-name parts. The
`Author` model accepts either shape and marks which:

```json
{"is_collective": true, "collective_name": "The SPRINT Research Group", "family": null}
```

A cross-field validator rejects records that are both or neither, so a
study-group name can never end up in a surname field.

---

## 4. Evidence-type normalisation

PubMed assigns a *list* of publication types — a systematic review of trials is
commonly tagged `Meta-Analysis`, `Systematic Review`, `Review` **and** `Journal
Article` at once. Retrieval needs one normalised class, so a fixed precedence
order is applied:

```
guideline > meta-analysis > systematic review > RCT > cohort > case report > narrative review > preprint
```

Precedence is applied over a *set*, not the input sequence, so the same article
classifies identically regardless of the order the publisher listed its types.

Two guarantees:

- **The raw list is never discarded.** `publication_types` keeps every upstream
  string verbatim, so a future re-classification runs against original metadata.
- **This is not a clinical judgement.** Every classification is recorded with
  `evidence_type_provenance = "publisher_metadata"`. A record with no
  recognised type is left `unclassified` with provenance `unknown` rather than
  guessed into a category.

---

## 5. Retractions, corrections, expressions of concern

Direction is load-bearing and easy to get wrong.

| RefType | Meaning | Effect |
|---|---|---|
| `RetractionIn` | **This article has been retracted**; the notice is elsewhere | `retraction_status = retracted` |
| `RetractionOf` | **This article IS the retraction notice** for another | *Not* retracted; `is_retraction_notice = true` |
| `ExpressionOfConcernIn` | An EoC was issued about this article | `retraction_status = expression_of_concern` |
| `ErratumIn` | A correction to this article was published | `retraction_status = corrected` |

Conflating `*_In` with `*_Of` either hides retracted evidence or discards the
notice documenting it. Two independent signals are combined — the
publication-type list and the CommentsCorrections direction — because PubMed
populates them inconsistently.

`none` and `unchecked` are deliberately different values: `none` asserts a
clean screening result, `unchecked` means nobody looked.

### Exclusion policy

**Retracted articles and articles under an expression of concern are excluded
from the corpus by default.** They are never silently dropped:

- each is listed individually in the build report with its status;
- `retraction_summary` counts every status *before* exclusion;
- `retracted_excluded: true` records that the policy was applied.

`include_retracted = true` (or `--include-retracted`) retains them, still
marked with `retraction_status`, and the report records that the policy was
relaxed.

**Corrected articles are kept.** A correction does not discredit an article,
and the notice is preserved in `related_notices` for a reader to follow.

Screening reflects PubMed metadata *at retrieval time*. An article retracted
after a build will still read `none` until the corpus is rebuilt — stated in
every report's `provenance_notes`.

---

## 6. Deduplication

Three stages, strongest identifier first. The **first** record encountered wins,
and input order is preserved, so results are deterministic.

| Stage | Key | Catches |
|---|---|---|
| 1 | PMID | The same record returned by several topic queries |
| 2 | Normalised DOI | One article indexed under two PMIDs (ahead-of-print, then final) |
| 3 | Normalised title + publication year | Reprints sharing neither identifier |

DOI normalisation lowercases and strips resolver prefixes, so `10.1000/ABC` and
`https://doi.org/10.1000/abc` collide as intended. Title normalisation
lowercases, converts punctuation to spaces and collapses whitespace —
deliberately conservative, with no stemming or stopword removal, because an
aggressive normaliser merges genuinely different articles. The year is required
alongside the title, because titles like "Annual Report" recur.

**Every decision is recorded** in the build report:

```json
{"duplicate_document_id": "pubmed:40000031",
 "canonical_document_id": "pubmed:40000030",
 "rule": "doi",
 "key": "10.1000/dup-1"}
```

`duplicates_by_rule` always carries every rule, including ones that fired zero
times, so two reports diff cleanly.

---

## 7. Chunking

Section-aware and sentence-preserving:

1. **Sections are never mixed.** Each abstract section is chunked
   independently, so a passage always belongs to exactly one labelled section
   and carries that label for citation. The legacy splitter treated the whole
   abstract as one string, so a 500-character window could straddle METHODS and
   CONCLUSIONS — producing a passage whose halves make claims of entirely
   different strength.
2. **Sentence boundaries are preserved.** Whole sentences are packed up to
   `target_chars`. The segmenter is regex-based rather than model-based because
   chunk identifiers derive from the resulting boundaries, so segmentation must
   be perfectly reproducible with no downloaded model. It handles decimals
   (`7.5%`), abbreviations (`e.g.`, `vs.`, `et al.`) and single initials.
3. **A sentence longer than `max_chars` is emitted whole** and counted, rather
   than cut mid-clause.
4. **Sections below `min_chars` are dropped** and counted — never merged into a
   neighbour, which would blend two labelled sections.
5. **Identifiers are deterministic**: `{document_id}#{ordinal:04d}`, numbered
   contiguously *across* sections so they are unique by construction.

No overlap is used, so the manifest records `overlap = 0` rather than a value
the chunker does not apply.

### Note on `source_url`

The article URL is **constructed from the PMID**, never read from the payload.
It is rendered as a clickable citation in the patient-facing UI, so trusting a
URL from a third-party feed would be a link-injection vector. `SourceDocument`
additionally rejects any scheme other than `http`/`https`.

---

## 8. What this pipeline does **not** establish

> **Automatic retrieval from PubMed confers no medical approval of any kind.**

Every document is written `approval_status = "unreviewed"` with `review = null`.
The schema makes any other value unconstructible without a clinician review
record. The CLI prints this before every run, and every build report repeats it
in `provenance_notes`:

- no clinician has reviewed any record;
- `evidence_type` is derived mechanically from publisher metadata, not clinical
  assessment;
- retraction screening reflects PubMed metadata at retrieval time only.

Retrieval establishes that a publication exists. Nothing more.

---

## 9. Network behaviour

| Concern | Legacy | Now |
|---|---|---|
| Timeout | none — a hung connection blocked forever | explicit per-request, default 20 s |
| Retries | none | bounded exponential backoff, default 4 attempts |
| Backoff | — | 0.5 s → 1 s → 2 s → 4 s, capped at 8 s |
| Retry scope | — | 429 and 5xx only; a 400 fails immediately |
| Pacing | `sleep(0.4)` *after* the fetch, pacing nothing on the first request | minimum interval enforced *before* each request |
| Rate | fixed | 3 req/s unauthenticated, 10 req/s with an API key — NCBI's published limits |
| Batching | one request per topic | efetch batched at 200 PMIDs |

### Configuration

`NCBI_API_KEY` and `NCBI_EMAIL` are read from the environment when set and
**neither is required**. Without a key the client paces at 3 req/s instead of
10; ingestion still works. Values in the config file override the environment.

```bash
export NCBI_API_KEY=...      # optional: raises the rate limit
export NCBI_EMAIL=you@org    # optional: lets NCBI contact you before blocking
```

---

## 10. Usage

```bash
python -m synapse.cli.build_corpus --config configs/corpus.example.toml
```

| Flag | Effect |
|---|---|
| `--config PATH` | **Required.** TOML configuration. |
| `--output-dir PATH` | Override the configured output directory. |
| `--dry-run` | Retrieve and analyse; write nothing. |
| `--force` | Overwrite a non-empty output directory. |
| `--include-retracted` | Include retracted material, still marked. Off by default. |
| `--log-level`, `--log-json` | Logging verbosity and format. |

Exit codes: `0` success · `1` build failed · `2` configuration error.

Configuration is TOML, parsed with the standard library's `tomllib`. TOML is
used in preference to YAML deliberately: a config file is untrusted input by
the same argument as any artifact, and `yaml.load` is a code-execution surface
while `tomllib` has no such capability and needs no third-party dependency.

### Output

```
artifacts/<corpus-version>/
├── chunks.jsonl.gz      EvidenceChunk records
├── documents.jsonl.gz   SourceDocument records
├── manifest.json        IndexManifest — verified before any load
└── build_report.json    CorpusBuildReport
```

Written in the safe artifact format from `docs/artifact-format.md` — JSONL,
never pickle — and verified before the command reports success. No embeddings
are computed here, so the manifest declares `index_state = "rebuild_required"`.

---

## 11. Build report

Machine-readable, schema-validated (`CorpusBuildReport`), and **deterministic**:
given the same inputs, every field except the wall-clock timestamps is
byte-identical between runs, and summary maps carry stable keys so two reports
diff directly.

| Section | Contents |
|---|---|
| Provenance | build id, versions, timestamps, synapse version, git commit, config digest |
| `queries` | per query: requested, PMIDs returned, documents parsed, parse failures |
| `duplicate_decisions` | every drop: duplicate, canonical, rule, matched key |
| `duplicates_by_rule` | counts per rule, always all three keys |
| `exclusions` | every excluded document with a machine-readable reason |
| `retraction_summary` | counts per status, computed **before** exclusion |
| `retracted_excluded` | whether the policy was applied |
| `approval_summary` | always `{"unreviewed": N}` for automatic retrieval |
| `chunking_stats` | documents chunked, documents without chunks, short sections dropped, oversized sentences |
| `evidence_type_summary` | counts per normalised evidence type |
| `provenance_notes` | the caveats in §8, verbatim |

The report's validator rejects arithmetic that does not close —
`documents_after_dedupe` must equal `documents_fetched` minus the number of
duplicate decisions — so a report that cannot be reconciled fails rather than
misleading an auditor.

---

## 12. Testing

Every ingestion test runs against local XML fixtures in
`tests/fixtures/pubmed/` and **makes no network calls**.

| Fixture | Covers |
|---|---|
| `structured_abstract.xml` | Four labelled sections, collective author, electronic vs print dates, DOI in two places, nested `<i>` |
| `nested_markup.xml` | Nested `<sub>`/`<i>` in title and abstract |
| `missing_fields.xml` | No abstract; no PMID; no Article element; free-text MedlineDate |
| `retracted.xml` | Retracted article, its retraction notice, expression of concern |
| `corrected.xml` | ErratumIn; multiple competing publication types |
| `duplicates.xml` | All three deduplication rules in one file |

The transport, clock and sleep functions are injected, so retry and pacing are
asserted without the suite waiting. `test_pipeline_makes_no_network_calls`
enforces the guarantee structurally by monkeypatching `socket.socket` to raise
during a full build.

---

## 13. Related documents

- `docs/artifact-format.md` — the output format, its digests, and verification.
- `docs/quality-architecture.md` — the wider plan; §1 records the measured
  defects cited throughout this document.

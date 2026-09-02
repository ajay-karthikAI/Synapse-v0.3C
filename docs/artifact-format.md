# Synapse Artifact Format

**Applies to:** `synapse` package version 0.1.0
**Status:** implemented and tested. No clinical review has been performed on any content described here.

This document specifies the on-disk format for Synapse corpus and index
artifacts, the guarantees the loader enforces, and the procedure for migrating
from — and rolling back to — the legacy pickle format.

---

## 1. Why the format changed

The previous format was four Python pickle files loaded at application startup:

| File | Purpose |
|---|---|
| `processed_chunks.pkl` | corpus chunks |
| `hybrid_index/vector/chunks.pkl` | the same corpus, again |
| `hybrid_index/bm25/chunks.pkl` | the same corpus, a third time |
| `hybrid_index/bm25/bm25.pkl` | serialised BM25 model state |

Three problems, all structural rather than incidental:

**Unpickling executes code.** `pickle.load` resolves and calls the constructors
named in the byte stream. A modified corpus file is arbitrary code execution
inside the application process. An opcode audit of the shipped files shows they
reference only `Data.fetch_and_chunk.Chunk` and `rank_bm25.BM25Okapi`, so the
files themselves are benign — but the *format* cannot be made safe.

**Pickle couples artifacts to source layout.** The corpus stream names
`Data.fetch_and_chunk.Chunk`. Renaming that package makes every existing
artifact unloadable.

**Nothing was verified.** There was no manifest, so nothing recorded which
embedding model produced the vectors, how many chunks existed, or what order
they were in. `HybridRetriever.load()` read whatever was on disk and assumed the
BM25 chunk list, the vector-store chunk list and the FAISS row order still
corresponded. A partial rebuild would produce answers that look entirely normal
while citing the wrong paper.

---

## 2. Directory layout

```
artifacts/<corpus-version>/
├── chunks.jsonl.gz       EvidenceChunk records, one per line, in index order
├── documents.jsonl.gz    SourceDocument records, one per line
├── index.faiss           FAISS vector index (optional)
└── manifest.json         IndexManifest — hashes, versions, build configuration
```

`.gz` is chosen by filename suffix. Gzip is written with `mtime=0` and an empty
filename field, because those are the two non-deterministic header fields —
without suppressing them, writing the same corpus twice produces different file
bytes and integrity checks fail spuriously.

**FAISS is retained** as the vector index format. It stores vectors, not
executable objects, so it carries none of pickle's risk. What is added is the
manifest beside it.

**BM25 state is not persisted at all.** It was the only remaining pickle, and a
BM25 index over a corpus this size is cheap to rebuild from the JSONL at load
time, so carrying a pickle forward for it buys nothing.

---

## 3. Identifiers

```
document_id := "<scheme>:<key>"            e.g. "pubmed:41802233"
chunk_id    := "<document_id>#<ordinal>"   e.g. "pubmed:41802233#0002"
```

`scheme` ∈ `pubmed` | `guideline` | `pdf` | `txt`. For PubMed the key is the
PMID; otherwise it is the first 16 hex characters of the SHA-256 of the
normalised content, so the same file ingested twice yields the same identifier.

`ordinal` is the chunk's position **within the emitted stream for its
document**, zero-padded to four digits so lexical order matches numeric order.

### Why ordinals continue rather than restart

The legacy scheme was `f"{pmid}_chunk{chunk_index}"`, where `chunk_index`
restarts at 0 for each fetch. `build_corpus.py` fetches the same article under
several topic queries, so the same article is appended more than once with the
same indices. Measured on the shipped corpus: **2,220 chunks produce only 2,058
unique legacy identifiers — 162 collisions.**

Those collisions are not cosmetic. `Retrieval/hybrid_retriever.py` builds a
`{chunk_id: score}` map to reconstruct a full-corpus score array; colliding keys
overwrite each other, and every duplicate then receives the same fallback
distance. RRF fusion is worse: it merges by `chunk_id`, so duplicates collapse
and one document's rank contribution is double-counted.

Under the new scheme a re-ingested document continues numbering — 0,1,2,3,4 then
5,6,7,8,9 — so identifiers are unique by construction.

---

## 4. Record schemas

Every record carries `schema_version` (`MAJOR.MINOR`) and is validated by a
pydantic model configured with `extra="forbid"` and `frozen=True`. Unknown
fields are an error, not something to ignore.

### Version compatibility

| Condition | Result |
|---|---|
| Same `MAJOR`, record `MINOR` ≤ reader `MINOR` | accepted |
| Same `MAJOR`, record `MINOR` > reader `MINOR` | **refused** |
| Different `MAJOR` | **refused** |
| Field absent | stamped with the reader's version |
| Field present but empty or malformed | **refused** |

Refusing a *newer* minor is deliberate. Combined with `extra="forbid"`, a file
written by a newer build may carry fields this build does not understand;
failing at load is safer than silently dropping provenance data.

### `EvidenceChunk`

| Field | Type | Notes |
|---|---|---|
| `chunk_id` | str | must parse back to `document_id` + `ordinal` |
| `document_id` | str | |
| `ordinal` | int ≥ 0 | |
| `text` | str | must already be normalised |
| `content_sha256` | str | must equal `sha256(text)` |
| `char_start`, `char_end` | int | offsets into the parent document's normalised text |
| `source_type` | enum | `pubmed_abstract` \| `guideline` \| `pdf` \| `txt` |
| `section` | str \| null | structured-abstract label, e.g. `CONCLUSIONS` |
| `page` | int \| null | PDF only (`null`, not `-1`) |
| `token_count` | int \| null | |
| `ingested_at` | datetime | timezone-aware, always |
| `legacy_chunk_id` | str \| null | traceability only; never used for lookup |

Two invariants make whole classes of corruption unrepresentable: the identifier
must decompose to the fields it encodes, and the digest must match the text. A
record whose text was altered after hashing fails at load rather than being
served to a patient as the cited source.

### `SourceDocument`

Carries `source_url`, `identifiers` (pmid/pmcid/doi/guideline_id), `title`,
`authors`, `container`, `publication_date`, `evidence_type`,
`evidence_type_provenance`, `retraction_status`, `approval_status`, `review`,
`review_due_date`, `content_sha256`, `chunk_ids`, `source_pack_version`.

**Governance rule, enforced by the model:** a document cannot claim approval
without an approval record. `approval_status = "approved"` requires a
`SourceReview` with a clinician reviewer, an `approved` decision, and a
`review_due_date`. `approval_status = "unreviewed"` forbids a review record
entirely. Reviewer identifiers must match `rev_[a-z0-9]{8}` — the mapping to a
real person is held outside this repository, so a clone never carries reviewer
identities.

`source_url` is restricted to `http://` and `https://`. It is rendered as a
clickable citation in the patient-facing UI, so permitting other schemes would
turn a corpus file into a link-injection vector.

### Other models

`SourceReview`, `SourcePackManifest`, `EvaluationCase`,
`EvaluationDatasetManifest`, `EvaluationRun`, `RetrievedEvidence`,
`AnswerClaim`, `StructuredAnswer` are defined in `synapse/schemas/`. The
evaluation and answer models are **schemas only** at this version — no metric is
implemented and no clinical label is authored.

---

## 5. `IndexManifest`

```json
{
  "schema_version": "1.0",
  "index_id": "corpus-v2-1",
  "index_version": "1",
  "corpus_version": "corpus-v2",
  "source_pack_version": "corpus-v2",
  "corpus_sha256": "1a0a94cf…",
  "index_sha256": "34aa6770…",
  "chunk_ids_sha256": "35abb529…",
  "artifacts": [
    {"role": "chunks", "path": "chunks.jsonl.gz", "sha256": "787f07ca…", "size_bytes": 439407},
    {"role": "documents", "path": "documents.jsonl.gz", "sha256": "47de28e0…", "size_bytes": 116771},
    {"role": "faiss", "path": "index.faiss", "sha256": "34aa6770…", "size_bytes": 13639725}
  ],
  "embedding": {"provider": "openai", "model": "text-embedding-3-small", "dimensions": 1536, "l2_normalized": true},
  "chunking": {"algorithm": "legacy_imported", "chunk_size": 500, "overlap": 100, "min_chunk_chars": 0},
  "distance_metric": "l2",
  "vectors_l2_normalized": true,
  "faiss_index_type": "IndexFlatL2",
  "document_count": 847,
  "chunk_count": 2220,
  "vector_count": 2220,
  "index_state": "ready",
  "built_at": "2026-08-12T18:19:35.088238Z",
  "git_commit": null,
  "builder": {"synapse": "0.1.0", "python": "3.11.15", "platform": "macOS-15.7.7"},
  "manifest_sha256": "913453e3…"
}
```

### The three digests, and why there are three

| Digest | Computed over | Detects |
|---|---|---|
| `artifacts[].sha256` | raw file bytes | any bit flip, truncation, replacement |
| `corpus_sha256` | canonical serialisation of every record, in order | content change; **independent of compression**, so gzipping or decompressing a corpus does not invalidate a manifest |
| `chunk_ids_sha256` | chunk identifiers, in order | **reordering** |

The third is the one that is easy to omit and expensive to lack. A reordered
corpus contains exactly the same records, so a content digest cannot detect it —
but FAISS row *i* is bound to corpus record *i*, and serving from a reordered
corpus produces confident, well-formatted answers citing the wrong document.

`manifest_sha256` is computed over the manifest itself with that field excluded,
so editing any other field is detectable.

### `index_state`

| Value | Meaning |
|---|---|
| `ready` | a FAISS index exists, is hashed, and `vector_count == chunk_count` |
| `rebuild_required` | the corpus exists but no compatible index does; `index_sha256` and `vector_count` must be null |

`git_commit` is `null` when the build did not run inside a git checkout. It is
never fabricated. The Synapse working tree is not currently a git repository, so
migrated manifests record `null`.

---

## 6. Verification

`synapse.index.verify_artifacts(directory, deep=True)` runs before any load and
**raises rather than warning**. There is no fallback path.

Layered cheapest-first:

1. manifest exists, parses as JSON, validates, and is schema-compatible;
2. `manifest_sha256` matches (tamper detection);
3. every declared artifact exists and matches its recorded `size_bytes`;
4. every declared artifact's SHA-256 matches;
5. the FAISS header's dimensionality and vector count agree with the manifest;
6. *(deep only)* every corpus record re-validates, and the recomputed
   `corpus_sha256` and `chunk_ids_sha256` match.

`load_verified_corpus(directory)` is the only supported way to obtain chunks for
serving; it is impossible to reach them without verification having passed.

### Typed errors

| Exception | Raised when |
|---|---|
| `ArtifactNotFoundError` | a declared artifact is missing |
| `ArtifactSchemaError` | a record or manifest is malformed |
| `ArtifactVersionError` | a schema version is incompatible |
| `ArtifactIntegrityError` | a digest or size does not match |
| `ArtifactCompatibilityError` | artifacts are individually valid but do not correspond |
| `UnsafeArtifactError` | an operation was refused as unsafe |

All derive from `SynapseArtifactError`. Messages are **user-safe**: they are
built from an allow-list of sanitised scalars, paths are reduced to their
basename, digests are truncated to 12 characters, and record payload text is
never included. Corpus text is third-party copyrighted content and may in future
sit alongside patient-derived text, so it must not reach a log or an error card.

### FAISS header probe

`probe_faiss_header` reads dimensionality and vector count directly from the
index header, so corpus/index alignment can be verified **without `faiss-cpu`
installed** — the integrity gate stays runnable in a minimal CI environment. It
is best-effort: an unrecognised index type returns `None` and that check is
skipped, never failed.

---

## 7. Migration

### Command

```bash
python -m synapse.cli.migrate_artifacts \
    --input processed_chunks.pkl \
    --output-dir artifacts/corpus-v2 \
    --corpus-version corpus-v2 \
    --faiss-index hybrid_index/vector/index.faiss \
    --trust-input
```

> **⚠️ Pickle input must come from a trusted source.**
> The command prints this warning on every invocation and **refuses to run
> without `--trust-input`.**
>
> Unpickling is not safe on untrusted data. Synapse mitigates this with a
> restricted unpickler that refuses any global outside a two-entry allow-list,
> so a stream naming an unexpected class is rejected before that class is
> resolved — `os.system` is never imported, let alone called. That mitigation is
> strong but it is **not a substitute for provenance**. Run this only on a
> pickle you produced or that came from a source you control.

### Options

| Flag | Purpose |
|---|---|
| `--trust-input` | **Required.** Asserts provenance of the input. |
| `--faiss-index PATH` | Copy an existing index in and record it. Incompatible with `--dedupe`. |
| `--dedupe` | Drop duplicate documents. Sets `index_state = "rebuild_required"`. |
| `--min-chunk-chars N` | Drop chunks shorter than N after normalisation. Default 0. |
| `--embedding-model`, `--embedding-dimensions`, `--embedding-provider` | Recorded in the manifest. Must match the index actually being reused. |
| `--chunk-size`, `--chunk-overlap` | Recorded in the manifest. |
| `--dry-run` | Analyse and report; write nothing. |
| `--force` | Overwrite a non-empty output directory. |

### Deduplication and index reuse are mutually exclusive

`--dedupe` changes the number and order of records, which invalidates the row
alignment of an existing FAISS index. Passing it together with `--faiss-index`
is **refused** (exit code 2) rather than silently producing a misaligned index.

- Keep the existing index → migrate **without** `--dedupe`. Order is preserved,
  identifiers are re-derived to be unique, duplicates are reported but retained.
- Remove duplicates → migrate **with** `--dedupe`, then rebuild the index from
  the new corpus.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | migration completed and the written artifacts verified |
| 1 | migration failed (typed artifact error; message printed) |
| 2 | refused (missing `--trust-input`, or `--dedupe` with `--faiss-index`) |

### Observed result on the shipped corpus

```
legacy chunks read           : 2220
chunks written               : 2220
documents written            : 847
duplicate chunk ids resolved : 162
duplicate texts detected     : 163
duplicate documents removed  : 0
chunks dropped (too short)   : 0
chunks dropped (empty)       : 0
documents skipped (no url)   : 0
```

### Fidelity limits of migrated data

The migration converts what the legacy pipeline recorded. It cannot recover what
was never recorded, and it does not invent it:

- **No publication dates, journals, authors or evidence types.** The legacy
  `Chunk` dataclass has no such fields. Migrated documents carry
  `evidence_type: unclassified`, `evidence_type_provenance: unknown`,
  `retraction_status: unchecked`, and empty author/container records.
- **Document text is reconstructed** by concatenating a document's chunks, so
  overlap regions appear twice and `char_start`/`char_end` are offsets into that
  reconstruction rather than into the true source. A full re-ingest is required
  for fidelity.
- **`retrieved_at` is the migration timestamp**, since the true fetch time was
  never recorded.
- **`chunking.algorithm` is `legacy_imported`**, not `recursive_character` —
  boundaries were inherited from the legacy artifact, not recomputed.
- **Every document is `approval_status: unreviewed` with `review: null`.** The
  repository contains no review metadata for any document, so no other value
  would be truthful.

---

## 8. Rollback

The migration is **one-way and non-destructive**. It reads the pickle, writes
new files elsewhere, and never modifies or deletes its input. Rollback is
therefore a matter of pointing at the old path — not restoring data.

### Procedure

1. **Confirm the legacy artifacts are intact.** They are never touched, but
   verify before relying on it:
   ```bash
   ls -l processed_chunks.pkl hybrid_index/vector/ hybrid_index/bm25/
   ```

2. **Point the application back at them.** As of this change nothing in the
   application reads the new format — `app.py`, `Data/`, `Retrieval/`,
   `Generation/` and `Evaluation/` are unmodified and still load the pickles.
   Rollback at this version is therefore a no-op: the legacy path is the only
   live path.

   Once a later change introduces a reader that prefers the new artifacts, that
   reader must be gated by an environment variable (`SYNAPSE_ARTIFACT_MODE=legacy|v2`)
   so rollback stays a one-variable flip.

3. **Optionally remove the migrated artifacts.** They are self-contained:
   ```bash
   rm -rf artifacts/corpus-v2
   ```
   Nothing outside that directory references them.

4. **Re-run the migration when ready.** It is idempotent for a given input:
   the same pickle and the same flags produce the same digests, because
   serialisation, gzip framing and hashing are all deterministic. Only
   `built_at` differs between runs.

### If verification fails after migration

`verify_artifacts` runs as the last step of a successful migration, so a
zero exit code means the artifacts loaded cleanly. If verification later fails
on an existing directory, do not attempt to repair the manifest by hand — the
manifest's self-digest will then fail too. Re-run the migration into a fresh
directory and compare digests.

---

## 9. Guarantees this format provides

| Guarantee | Mechanism |
|---|---|
| No runtime path deserialises pickle | AST test over the whole package; the one reader is quarantined in `synapse/_legacy/` and importable only by the migration CLI |
| No dynamic execution | AST test bans `eval`, `exec`, `compile`, `__import__`, `importlib.import_module`, `os.system`, `marshal`, and any YAML loader |
| Corpus cannot silently drift from its index | `chunk_ids_sha256` ordering digest, checked before load |
| A tampered manifest cannot pass | `manifest_sha256` self-digest |
| A tampered record cannot pass | per-record `content_sha256`, re-verified on every read |
| A manifest cannot reference files outside its directory | `ManifestArtifact.path` rejects absolute paths, `..`, and drive prefixes |
| A document cannot claim unearned approval | model-level cross-field invariants |
| Reviewer identities cannot be committed | `rev_[a-z0-9]{8}` pattern, plus an `@` check on comments |
| Errors cannot leak corpus or patient text | `_SafeError` builds messages from sanitised scalars only |
| Rebuilds are byte-reproducible | deterministic serialisation and `mtime=0` gzip |
| Imports work on case-sensitive filesystems | single lowercase package, verified on a case-sensitive APFS volume |

---

## 10. Related documents

- `docs/quality-architecture.md` — the wider evaluation, evidence-governance and
  citation-integrity plan this layer is the foundation of. §1 records the
  measured defects referenced above; §5 gives the change sequencing.

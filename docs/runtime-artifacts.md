# Runtime Artifacts

**Applies to:** `synapse.runtime` · `synapse.retrieval.native` ·
`synapse.cli.package_runtime` · `synapse.cli.migrate_artifacts`
**Status:** implemented, tested offline, and **verified once against the real
corpus** (2026-09-03, §11). **Never deployed** — no artifact has been uploaded
or served to anyone.

> This describes a mechanism, not a running system. Nothing here is evidence
> that the index is correct, that the corpus is clinically appropriate, or that
> any source has been reviewed. It is evidence that a *wrong* artifact will be
> refused rather than served, and — since §11 — that a *right* one is accepted.

---

## 1. What changed

The prototype loaded whatever was on disk and trusted it:

```python
chunks = load_chunks("processed_chunks.pkl")          # pickle: arbitrary code execution
hybrid = HybridRetriever.load("hybrid_index")         # bm25.pkl: more of the same
```

`HybridRetriever.save()` writes a two-subdirectory layout, which is where the
input paths above come from:

```
hybrid_index/
├── bm25/
│   ├── bm25.pkl          the BM25Okapi object, pickled
│   └── chunks.pkl        a SECOND copy of the corpus, pickled
└── vector/
    ├── index.faiss       the vector index
    ├── chunks.pkl        a THIRD copy of the corpus, pickled
    └── embeddings.npy
```

Three copies of the corpus in three files, with nothing checking that they still
agree — which is the misalignment this layer exists to end.

`hybrid_index/` carries no manifest, so nothing could check that the BM25 chunk
list, the vector store's chunk list and the FAISS row order still corresponded.
A partial rebuild produced answers that looked completely normal while citing
the wrong document — defect C8 in
[quality-architecture.md](quality-architecture.md) §1.2.

The deployed runtime now resolves **one** archive, named by version and pinned
by SHA-256, and proves it intact before loading anything.

**Pickle is gone from the deployed runtime.** The corpus is ordered JSONL, the
index is FAISS, and BM25 state is *rebuilt at startup* rather than
deserialised. The restricted reader in `synapse/_legacy/` remains reachable
from exactly one place — `python -m synapse.cli.migrate_artifacts`, an offline
operator command — and `tests/test_runtime_artifacts.py` fails the build if any
runtime module imports `pickle` or references that reader.

---

## 2. The archive

Four members, exactly, and an archive containing anything else is refused:

| Member | What it is |
|---|---|
| `manifest.json` | Digests, counts, ordering digest, embedding and chunking configuration |
| `chunks.jsonl.gz` | The corpus, **in index order** |
| `documents.jsonl.gz` | Source documents: titles and links for citations |
| `index.faiss` | The vector index |

Packing is **byte-reproducible**: members in a fixed order, timestamps and
ownership zeroed, gzip header mtime zeroed. Repacking identical content
reproduces the same SHA-256, which is what makes the digest something an
operator can pin and compare rather than a number that changes on every
rebuild.

---

## 3. Building one

Two commands. The first is the only place pickle is ever read.

```bash
# 1. Migrate the legacy corpus. NO --dedupe: deduplication changes the number
#    and order of records, which invalidates the row alignment of the existing
#    FAISS index. The CLI refuses --dedupe together with --faiss-index.
python -m synapse.cli.migrate_artifacts \
  --input processed_chunks.pkl \
  --output-dir artifacts/runtime/corpus-v2 \
  --corpus-version corpus-v2 \
  --index-version 2 \
  --faiss-index hybrid_index/vector/index.faiss \
  --trust-input

# 2. Verify and pack.
python -m synapse.cli.package_runtime \
  --artifact-dir artifacts/runtime/corpus-v2 \
  --output dist/synapse-runtime-2.tar.gz
```

The second command verifies **before** it packs — the same deep verification the
container runs on the way in — so an archive is never built from an artifact
that would fail on load. It prints the environment variables to set:

```
SYNAPSE_ARTIFACT_VERSION=2
SYNAPSE_ARTIFACT_SHA256=<64 hex characters>
SYNAPSE_ARTIFACT_KEY=synapse-runtime-2.tar.gz
```

Upload the archive to the private bucket yourself. That is deliberately a
separate act: the thing that can write to the bucket should not also be the
thing that reads a corpus.

**Neither the archive nor the corpus is committed.** Both are build output, both
are large, and `.gitignore` covers `*.tar.gz`, `*.pkl`, `*.faiss` and
`artifacts/`.

---

## 4. Configuration

| Variable | Required | Meaning |
|---|---|---|
| `SYNAPSE_ARTIFACT_VERSION` | yes | Which build to serve |
| `SYNAPSE_ARTIFACT_SHA256` | yes | The pin. 64 lowercase hex characters |
| `SYNAPSE_ARTIFACT_BUCKET` | yes | Private bucket |
| `SYNAPSE_ARTIFACT_KEY` | no | Defaults to `synapse-runtime-<version>.tar.gz` |
| `SYNAPSE_ARTIFACT_ENDPOINT_URL` | no | For R2, MinIO, Backblaze; omit for AWS S3 |
| `SYNAPSE_ARTIFACT_REGION` | no | |
| `SYNAPSE_ARTIFACT_CACHE` | no | Defaults to `/var/data/synapse` |

**Credentials are not in this list.** `boto3` reads
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from the environment through its
own credential chain. `RuntimeArtifactConfig` holds no credential field at all,
so a credential cannot reach a log line, a traceback or a readiness response
(asserted by `test_the_config_holds_no_credential_fields`).

The version alone is not enough. A version label identifies an artifact by a
name the storage account's owner can repoint at any time; the digest is what
makes it an identity.

---

## 5. The gate sequence

```
configured?  →  cached and valid?  →  download  →  archive SHA-256
     →  safe extract  →  deep manifest verification  →  counts, state, pack
     →  corpus load  →  BM25 rebuild  →  FAISS load and alignment check
     →  READY
```

No branch continues past a failed gate, and nothing is written where the
backends read from until every gate has passed — extraction stages into a
temporary directory and only a fully-verified tree is moved into the cache,
atomically.

### What each gate refuses

| Gate | Refuses |
|---|---|
| Archive digest | Any byte changed. Checked **before** the tar stream is parsed, so a hostile archive is never opened |
| Safe extraction | Path traversal, absolute paths, symlinks, hardlinks, device nodes, unexpected members, duplicate members, missing members, decompression bombs |
| Manifest self-digest | An edited manifest |
| Artifact digests | A corrupted or truncated file |
| FAISS header | Wrong dimensionality; vector count ≠ chunk count |
| Corpus digest | Any edited record |
| **Ordering digest** | A **reordered** corpus — the failure a content digest cannot detect, because the records are identical and only their order changed |
| Counts and state | `rebuild_required`, an empty corpus, a missing index digest, or an artifact naming no authorising source pack |
| Backend load | An index whose row count does not match the corpus |

`tarfile.extractall` is never called. Every member is inspected and extracted
individually.

---

## 6. Caching

Verified artifacts are cached under `/var/data/synapse/<version>-<digest12>/`.

A cache is reused **only** when the directory name matches the configured
version and digest, a marker file records the same digest, and **deep
verification passes again**. Re-verifying a cache we wrote ourselves is not
redundant: the disk is persistent and outlives the process, so between two
starts the bytes can change through a failed write, a disk fault, or anything
with filesystem access.

An unusable cache is a reason to download, not a reason to fail. A genuinely
corrupt artifact still fails closed — the download that follows ends in the same
verification, and that is what decides.

---

## 7. Readiness

Default **false**, and set true only as the last statement after every gate.

That direction matters. A flag that starts true and is cleared on error serves
traffic during startup and after any failure the error handling missed.

| Code | Meaning | Operator action |
|---|---|---|
| `configuration_error` | No artifact named | Set a variable |
| `index_unverified` | Named, could not be proven intact | Investigate the build |

Both are `synapse.ui.errors.AnswerFailureCode` values, reused rather than
reinvented, so an interface renders a readiness failure with the same fixed copy
and typed code it already renders a turn failure with.

`start()` never raises. A container that reports itself unready tells an
operator far more than one that crash-loops. The recorded `detail` is an
exception **type name** — never a message, which can carry an endpoint, a
request id, or a presigned URL fragment.

---

## 8. No fallback

There is no branch that serves `hybrid_index/` when verification fails, and
`test_no_runtime_module_names_the_unmanaged_index_directory` fails the build if
any runtime *code* names it (the docstrings may, and do, explain why it is
absent).

That directory has no manifest. Falling back to it under failure would mean the
system's answer to "I cannot prove this index is intact" is to serve an index
nothing can prove anything about — which is precisely the state this layer
exists to end.

---

## 9. Native backends

`synapse.retrieval.native` replaces the `synapse.retrieval.production` adapters.
Those wrapped objects the prototype built by unpickling; these load a
manifest-verified corpus and are typed throughout. No identifier is derived,
repaired or dropped — unlike the legacy adapters, which had to rebuild
identifiers because the prototype's were ambiguous.

### The tokenizer

`legacy_tokenize` is a character-for-character reimplementation of
`Retrieval/bm25_index.tokenize`, duplicated rather than imported because
`synapse` may not import the legacy tree. Its contract is **to match, not to
improve**: BM25 state is rebuilt at startup, so a single filtered character
would change which chunks match and silently invalidate every measurement taken
against the legacy index.

It is **not** `synapse.retrieval.backends.tokenize`, which matches `[a-z0-9]+`
and would split `COVID-19`, `ICD-10` and `SGLT2` — the exact terms BM25 exists
to catch.

Parity is established twice: a golden table that runs on every pull request with
nothing installed, and a direct comparison against the legacy `tokenize` and a
real `BM25Okapi` ranking, which skips when the prototype's dependencies are
absent. Both have been run and pass.

**A known limitation, pinned rather than fixed:** `E11.9` tokenizes to `e11` —
the `.` becomes a space and the lone `9` is dropped as a single character, so
the tail of an ICD code is not searchable. That is the legacy behaviour, and
parity is the contract.

---

## 10. What this does not do

- **It does not verify the corpus is correct.** Only that it is the corpus the
  manifest describes. No clinician has reviewed any source in it.
- **It does not check the source pack's contents.** It refuses an artifact that
  names no authorising pack; it does not yet compare `source_pack_sha256`
  against the live pack.
- **It does not wire readiness into the turn path.** A request arriving while
  unready is not yet refused by the application service — that is the next
  phase's work, and until then readiness is a signal, not an enforcement point.
- **The automated tests still use a FAISS header followed by zeroes**, because
  a header is all verification reads. The real-corpus run in §11 was performed
  by hand and is not part of CI — the artifact is 12 MB and cannot be committed.

---

## 11. The real-corpus verification (2026-09-03)

Run once, by hand, against the prototype's actual corpus. It answered the
question this layer was built on and could not otherwise settle: **does the
no-dedupe migration survive real data?**

### The risk, and what actually happened

The concern was that the legacy corpus contains duplicate documents, which would
force `--dedupe` — and `--dedupe` changes the number and order of records, which
invalidates the row alignment of the existing FAISS index. The CLI refuses that
combination, so a corpus needing it could not produce a servable artifact at all.

It does not need it:

| | |
|---|---|
| legacy chunks read | 2,220 |
| **chunks written** | **2,220** — nothing dropped |
| documents | 847 |
| **duplicate chunk ids resolved** | **162** |
| duplicate texts detected | 163 |
| **duplicate documents removed** | **0** |

The identifier collisions are real — 162 of them, exactly the
`f"{pmid}_chunk{index}"` ambiguity `synapse.retrieval.production` documents. The
migration **rebuilds** them to the identifier grammar rather than dropping
records, so read count equals written count and FAISS row *i* still corresponds
to corpus record *i*.

### What was verified

```
migrate_artifacts   2220 chunks, 847 documents, index_state ready
                    chunk_count 2220 == vector_count 2220
package_runtime     deep-verified 2220 chunks, packed 11,746,082 bytes
                    sha256 32ec735e85ac3fbd…
reproducibility     repacking identical content reproduced the same digest
load_artifact       cold 0.15s (from_cache=False), warm 0.08s (from_cache=True)
                    wrong digest -> ArtifactIntegrityError
RuntimeCorpus       2220 chunks, 2220 unique ids, 847 unique documents, 0.09s
NativeSparseBackend BM25 rebuilt from JSONL in 0.57s — never unpickled
NativeDenseBackend  IndexFlatL2, ntotal 2220, d 1536
```

**Row alignment, proven without an API call.** Each probe row's own vector was
reconstructed from the index and used as the query; the nearest neighbour must
be that row. Rows 0, 1, 500, 1109, 1500 and 2219 each resolved to themselves —
6/6.

### What this does NOT establish

The corpus is 847 unreviewed PubMed abstracts. Nothing here says they are the
right sources, that the retrieval is good, or that any of it is clinically
appropriate. Spot-checking the BM25 results shows the ranking is servicable and
unremarkable — a query for `SGLT2 cardiovascular` returns a case report about
discontinuation as its top hit — which is a property of the legacy tokenizer and
the corpus, not of this layer.

The archive was **not** uploaded. Deployment remains Phase 7.

---

## 12. Related

[artifact-format.md](artifact-format.md) ·
[retrieval-runtime.md](retrieval-runtime.md) ·
[quality-architecture.md](quality-architecture.md) ·
[migration-parity.md](migration-parity.md) ·
[SOURCE_GOVERNANCE.md](SOURCE_GOVERNANCE.md) ·
[privacy-logging-policy.md](privacy-logging-policy.md)

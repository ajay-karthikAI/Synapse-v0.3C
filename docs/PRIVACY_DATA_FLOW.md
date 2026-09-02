# Privacy and Data Flow

**Last audited:** 2026-08-14 · **Applies to:** `synapse` 0.1.0

> **No privacy review, DPIA, or HIPAA assessment has been performed.** This
> document describes where data *does* go, established by reading the code. It
> is not a compliance statement and must not be cited as one.

The single most important fact, stated before anything else:

> **When the legacy app is configured with an OpenAI API key, the text a user
> types is sent to OpenAI.** Three separate times per query. This is inherent to
> the design, not a defect, and it is the fact that governs whether this system
> may be shown to a real person.

---

## 1. What a user types

A free-text health question. Users have not been instructed to avoid identifying
details, and nothing stops them entering symptoms, medications, conditions,
dates, or names.

**Assume every query is sensitive personal health information.** The system does
not classify, redact, or detect identifiers, and no component behaves
differently based on what a query contains.

---

## 2. Where that text goes

### 2.1 The legacy app (`app.py`) — leaves the machine

| # | Destination | What is sent | Code |
|---|---|---|---|
| 1 | **OpenAI Embeddings** (`text-embedding-3-small`) | The raw query, to build a query vector | [Retrieval/vector_store.py:137](../Retrieval/vector_store.py#L137) |
| 2 | **OpenAI Chat** (`gpt-4o-mini`) | The query *and* each retrieved passage, once per passage, for reranking | [Retrieval/reranker.py:90](../Retrieval/reranker.py#L90) |
| 3 | **OpenAI Chat** (`gpt-4o` / `gpt-4o-mini`) | The query *and* the selected passages, for answer generation | [Generation/answer_generator.py:117](../Generation/answer_generator.py#L117) |

Retention, training use, and geographic processing are governed by **OpenAI's**
terms for the account whose key is used — not by anything in this repository.
An account without a zero-retention agreement is the default assumption.

No other network destination receives query text. PubMed is contacted only
during corpus *building*, with the operator's search terms, never with a user's
query.

### 2.2 The `synapse` package — does not leave the machine

The evaluation harness, governance, schema, corpus, and index layers make **no
network calls at all**. The evaluation harness replays recorded fixtures. Every
test is offline.

### 2.3 Disk

**Nothing writes user query text to disk.** Verified by inspection of the app
and the retrieval and generation modules; there is no query log, no analytics
sink, and no cache keyed on query text.

The corpus, index, and evaluation artifacts contain **published literature and
synthetic cases only** — no user data has ever entered them.

### 2.4 Memory

`st.session_state.conversation` holds the queries and answers for the lifetime
of the Streamlit session. It is not persisted and is lost when the session ends.
Nothing enforces that — it is a property of the current code, not a guarantee.

### 2.5 Logs

Patient query text is **not logged**, and this is now enforced rather than
merely intended: an AST sweep fails the build if any structured-logging call
passes a field that could carry free text (`query`, `text`, `answer`, …).
Derived values are permitted, because they disclose nothing a reader could
reconstruct the question from:

```python
logger.info("retrieval complete", extra={"query_sha256": ..., "query_length": 42})  # allowed
logger.info("retrieval complete", extra={"query": "I have chest pain"})             # fails CI
```

A second test guards the guard, so the check cannot silently stop matching.

---

## 3. Credentials

The OpenAI key is supplied by an environment variable or `.env`, or typed into a
**sidebar password field** that stores it in session state.

Consequences, stated plainly:

- The key is held in server-side session memory for the session's lifetime.
- It is used directly, unscoped and unrotated.
- In a multi-user deployment, each user would be supplying their own key into a
  shared process. **This app has no user isolation and must not be deployed
  multi-tenant as written.**

`.env` is gitignored; CI asserts no `.env`, `.pem`, or key-shaped file is
tracked, and a secret scanner runs over history.

---

## 4. Diagram

```
     user types a health question
                 │
                 ▼
        ┌────────────────┐
        │  Streamlit app │   in memory only, for the session
        └────────┬───────┘
                 │
      ┌──────────┼──────────┐
      ▼          ▼          ▼
   embed      rerank     generate          ══► OpenAI API  ← LEAVES THE MACHINE
  (query)  (query+text) (query+text)            (3 calls per query)
      │          │          │
      └──────────┼──────────┘
                 ▼
          answer + citations
                 │
                 ▼
            back to the user
                 │
                 ✗  not written to disk
                 ✗  not logged
                 ✗  not sent anywhere else


   ── separately, and never touching user data ──

   operator's search terms ──► PubMed E-utilities ──► corpus artifacts
   evaluation harness ────────► no network at all
```

---

## 5. What is NOT in place

Absent, not merely undocumented:

- No DPIA, HIPAA assessment, or legal review of any kind
- No Business Associate Agreement with OpenAI or anyone else
- No consent flow, privacy notice, or terms of use shown to a user
- No user accounts, authentication, or authorisation
- No audit trail of who asked what
- No data-retention or deletion policy, and no deletion mechanism
- No PII/PHI detection or redaction anywhere in the pipeline
- No rate limiting or abuse protection
- No region pinning or data-residency control
- No encryption at rest for anything (nothing sensitive is stored, but nothing
  enforces that this stays true)

---

## 6. Assumptions this design makes

If any of these is false in your deployment, the design does not hold.

1. **The operator has a lawful basis to send query text to OpenAI.** Nothing
   verifies this.
2. **Users understand their text leaves the machine.** Nothing tells them.
3. **The deployment is single-user.** There is no isolation between sessions.
4. **No regulated health data is entered.** Nothing prevents it.
5. **The operator's OpenAI account has appropriate retention terms.** Nothing
   checks this.

---

## 7. Before any real user

Minimum, in order:

1. Legal review and a documented lawful basis for processing.
2. A privacy notice and consent flow shown before the first query.
3. A zero-retention or equivalent agreement with the model provider.
4. PHI detection with an explicit user warning, or a hard architectural bar on
   free-text entry.
5. Authentication, session isolation, and rate limiting.
6. An audit trail — which requires deciding what may be logged, since today the
   answer is deliberately "nothing".
7. A retention and deletion policy, with a mechanism that implements it.

Steps 1–3 are blocking. See [LIMITATIONS.md](LIMITATIONS.md) §5 and
[SAFETY_CASE.md](SAFETY_CASE.md).

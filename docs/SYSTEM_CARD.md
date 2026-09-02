# Synapse — System Card

**Version:** `synapse` 0.1.0 · **Last audited:** 2026-08-14
**Maturity:** research prototype · **Deployment status:** never deployed

> **Not a medical device.** Not validated, not clinically proven, not FDA
> cleared, not HIPAA compliant, not production ready. No clinician has reviewed
> any source, evaluation case, threshold, or output in this repository.

---

## 1. Intended use

**Intended purpose.** To help an informed adult prepare for a medical
appointment by retrieving relevant PubMed abstracts and drafting questions to
ask a clinician.

**Intended user.** A technically capable adult who understands this is a
prototype, who is preparing for an appointment they have already decided to
attend, and who will treat the output as a starting point for a conversation
with a clinician.

**Intended context.** Local, single-user, non-clinical. Not in a clinic, not in
a care pathway, not in an emergency.

**The design intent that matters most:** the product outputs *questions for a
doctor*, not answers to medical questions. That framing is a safety control. A
tool whose intended next step is "ask your clinician" has a fundamentally lower
harm ceiling than one whose output is meant to be acted on. It should be
defended in product decisions, not traded away for perceived usefulness.

---

## 2. Excluded uses

**Never use Synapse for any of the following.** These are not cautions; they are
exclusions.

| Excluded use | Why |
|---|---|
| **Diagnosis** | No diagnostic reasoning, no differential, no clinical validation |
| **Triage or urgency assessment** | The emergency vocabulary has never been clinically reviewed; coverage of escalation-worthy conditions is unassessed |
| **Emergency assessment** | Never rely on it in an emergency. Call your local emergency number. |
| **Symptom checking** | Previously advertised in the README; removed. The system is not built for it and has never been evaluated for it. |
| **Treatment selection, dosing, or medication changes** | Abstracts routinely omit dosing, contraindications, and interactions |
| **Clinical decision support** | Would place it under medical-device regulation, which has not been assessed |
| **Any use by or for a minor** | No age-appropriate evaluation or safeguarding |
| **Multi-tenant or hosted deployment** | No authentication, no session isolation, no rate limiting |
| **Any use involving regulated health data** | No BAA, no DPIA, no PHI handling controls |
| **Any setting where a delayed security fix matters** | Single maintainer, no on-call |

---

## 3. Source boundaries

**In scope:** PubMed abstracts retrieved via NCBI E-utilities.

**Not in scope, and absent:** full-text articles, clinical guidelines, drug
labels, formularies, contraindication or interaction databases, patient records,
textbooks, and any non-English source.

**What that exclusion costs.** An abstract routinely omits the study population,
dosing, harms, and contraindications. A system built on abstracts alone will
sometimes present a finding whose limiting conditions are invisible to it.

**Governance boundary.** Only `approved` sources may be served in a governed
path. **There are currently 0 approved sources** — 3 `discovered`, 1
`superseded`. The legacy prototype bypasses governance entirely and queries
PubMed directly. See [SOURCE_GOVERNANCE.md](SOURCE_GOVERNANCE.md).

---

## 4. Models and versions

| Component | Model / version | Where |
|---|---|---|
| Query + corpus embeddings | OpenAI `text-embedding-3-small`, 1536-dim | `Retrieval/vector_store.py` |
| Reranking | OpenAI `gpt-4o-mini` | `Retrieval/reranker.py` |
| Answer generation | OpenAI `gpt-4o` / `gpt-4o-mini` | `Generation/answer_generator.py` |
| Keyword retrieval | BM25 (`rank-bm25` 0.2.2) — no model, no API | `Retrieval/bm25_index.py` |
| Vector index | FAISS `IndexFlatL2` over L2-normalised vectors (exact search) | `Retrieval/vector_store.py` |
| Text chunking | `RecursiveCharacterTextSplitter`, 500 chars / 100 overlap | `Data/fetch_and_chunk.py` |

**Model versions are not pinned.** OpenAI model aliases move; an alias that
resolves to a different checkpoint changes behaviour with no change to this
repository. Nothing here detects that, which means **no evaluation result has a
guaranteed shelf life**.

**Artifact versions.** Corpus and index artifacts are content-addressed. A
separate ordering digest binds FAISS row *i* to corpus record *i*, so a
reordered index is detected rather than silently mis-citing. Manifests are
verified before load; a hash mismatch fails the load.

| Artifact | Version |
|---|---|
| Evaluation dataset | `synapse-ci` 0.1.0, schema 1.0, 48 cases |
| Evaluation corpus fixture | `ci-fixture-corpus-v1` |
| Source pack | `diabetes-previsit` 0.1.0, schema 2.0 |
| Gate configuration | `0.1.0-provisional`, `approved_by` empty |

---

## 5. Evaluation coverage

**48 cases, all synthetic, all unreviewed, 0 release-gating eligible.**

| Category | Cases | Reviewed |
|---|---|---|
| `emergency_red_flag` | 6 | 0 |
| `negated_emergency` | 6 | 0 |
| `medication` | 6 | 0 |
| `adversarial_injection` | 6 | 0 |
| `ordinary_education` | 6 | 0 |
| `out_of_scope` | 6 | 0 |
| `insufficient_evidence` | 6 | 0 |
| `ambiguous_symptoms` | 6 | 0 |

**Metrics computed:** retrieval (Recall@k, nDCG@k, MRR, Hit@k), answer
(citation completeness, unsupported-claim rate), safety (emergency sensitivity,
false-negative count, behaviour correctness), operational (latency, cost, error
and timeout rates). Deterministic metrics are strictly separated from LLM-judge
metrics, and a judge score can never gate a merge.

**Reproducibility.** The harness replays recorded responses. Verified over two
runs: `per_case.jsonl` byte-identical; `results.json` identical apart from
`run_id`, `started_at`, `completed_at`. Proportions carry Wilson
intervals; means carry seeded bootstrap intervals; undefined metrics report
`None`, never `0.0`.

**What this coverage is worth.** The categories are the right ones and the
denominators are tiny. Every case was written by the same engineer who wrote the
code being tested. This measures whether the system does what its author
expected — not whether what its author expected is clinically right. See
[demo-evaluation-report.md](demo-evaluation-report.md) for a full run.

---

## 6. Known failure modes

| Mode | Behaviour | Status |
|---|---|---|
| ~~Emergency detector misses real emergencies~~ | **Fixed 2026-08-20.** Replaced by `synapse.safety`: 6/6 on repository cases, 15/15 held-out. Vocabulary remains **unreviewed** | **Resolved**; clinical adequacy still unassessed |
| ~~Negation in emergency detection~~ | **Fixed 2026-08-20.** Negation scoping added; 6/6. The gate was promoted to **blocking** | **Resolved** |
| **Unreviewed emergency vocabulary** | 11 concepts, 120+ patterns, authored by engineering. No clinician has assessed coverage | **Open** — blocks user exposure |
| **Misrepresented evidence** | A claim quotes a source accurately but misstates its population or strength | **Undetectable today** — support checking is lexical |
| **Stale evidence** | A source retracted after ingestion persists until rebuild | Known gap |
| **No evidence hierarchy** | A 1998 case report competes with a 2025 meta-analysis on similarity alone | By design, unresolved |
| **Reranker degradation** | An API or parse failure falls back to a default score, indistinguishable from success in the UI | Legacy prototype |
| **Abstract-only blind spots** | Dosing, contraindications, population limits absent from the source | Inherent to the corpus |
| **Prompt injection** | Retrieved content attempts to steer the system | Partially mitigated, weakly evidenced |
| **Model drift** | An unpinned model alias silently changes behaviour | Undetected |
| **Over-escalation fatigue** | Users learn to dismiss the emergency card | Consequence of the negation defect |

---

## 7. Safety mechanisms

What exists, and what each is actually worth:

| Mechanism | Strength |
|---|---|
| Emergency routing **before** retrieval — cannot be overridden by retrieved content | Ordering structurally sound; the detector it calls now scores 6/6 in-set and 15/15 held-out, over an **unreviewed** vocabulary |
| Permanent, non-dismissible disclaimer on every answer | Effective |
| Unsupported claims are withheld, never displayed | Strong for fabrication; blind to misrepresentation |
| Abstention when evidence is absent | Sound; abstention rate untuned |
| Citation verification against verbatim excerpts | Strong within its narrow scope |
| Stable source numbering, output escaping | Sound |
| Governance bar: automation cannot approve a source | Structurally enforced; never exercised |
| CI gates: zero tolerance for emergency false-negative increase | Real, but over synthetic cases |
| "Relevance score", never "confidence" | Corrects a genuinely misleading UI element |

The most effective safety property today is that **nobody uses it**. That is not
a control. See [SAFETY_CASE.md](SAFETY_CASE.md), which concludes the safety case
does not close.

---

## 8. Privacy assumptions

**With an API key configured, the user's text is sent to OpenAI three times per
query** — embedding, reranking, generation. Inherent to the design.

Locally: query text is never written to disk and never logged, enforced by a
static check in CI rather than by convention. Conversation state lives in memory
for the session only.

Absent: DPIA, HIPAA assessment, BAA, consent flow, privacy notice, user
accounts, audit trail, retention or deletion policy, PHI detection, rate
limiting, data-residency control.

**Assumed, and unverified by the system:** that the operator has a lawful basis
to send text to OpenAI, that the user understands their text leaves the machine,
that the deployment is single-user, and that no regulated health data is
entered. Nothing enforces any of these. See
[PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md).

---

## 9. Before a patient pilot

1. Clinical hazard review of [SAFETY_CASE.md](SAFETY_CASE.md) §2
2. Fix the negation defect; promote `negation_accuracy` to blocking
3. Clinical review of the emergency term list
4. A real, clinician-labelled evaluation set with defensible denominators
5. Approved sources, so the governed path has content
6. Clinical review of real system outputs
7. Independent red-teaming
8. Privacy and regulatory review, including device classification
9. Thresholds set from measured performance and signed off by named people
10. Deployment engineering: authentication, isolation, rate limiting, monitoring,
    on-call

Steps 1 and 4 gate everything else.

---

## 10. Related

[VALIDATION.md](VALIDATION.md) · [LIMITATIONS.md](LIMITATIONS.md) ·
[SAFETY_CASE.md](SAFETY_CASE.md) · [PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md) ·
[SOURCE_GOVERNANCE.md](SOURCE_GOVERNANCE.md) ·
[citation-integrity.md](citation-integrity.md) ·
[evaluation-metrics.md](evaluation-metrics.md)

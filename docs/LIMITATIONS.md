# Known Limitations

**Last audited:** 2026-08-14 · **Applies to:** `synapse` 0.1.0

Everything known to be missing, wrong, or unproven. This document is meant to be
uncomfortable to read. If you are evaluating whether Synapse can be used for
something, read this before the README.

A limitation disclosed here is not thereby excused. It is disclosed so that
nobody discovers it in front of a patient.

---

## 1. The blocking limitations

These four are the reason the system cannot be used with patients. Nothing else
in this document matters until they are resolved.

### 1.1 Zero clinician review, anywhere

No clinician has reviewed any source, evaluation case, threshold, emergency
label, or output in this repository. Not one. The governance machinery to record
such a review exists and is tested; **no review record has ever been created**.

Concretely, in the only source pack:

| Lifecycle state | Sources |
|---|---|
| `discovered` | 3 |
| `superseded` | 1 |
| **`approved`** | **0** |

The system will not serve an unapproved source in a governed path, which means
the governed path currently has nothing to serve.

### 1.2 The evaluation dataset is entirely synthetic

All 48 CI evaluation cases are engineering-authored. Every one carries
`is_synthetic: true` and `annotation_status: synthetic`, has **zero** reviews
attached, and is **excluded from release gating by construction**.

`cases_gating_eligible` is **0**. Every metric this repository can produce is
computed over cases that no clinician has seen. The gates verify that the gating
*mechanism* works. They do not tell you whether the system works.

### 1.3 Retrieval quality on real queries is unmeasured

The harness replays recorded responses against a fixed 48-case fixture. That
makes it reproducible, and it means **no number in this repository describes
behaviour on a real patient question against the real corpus**. There is no
held-out real-query benchmark, because there are no real labelled queries.

### 1.4 The emergency detector is fixed, but its vocabulary is unreviewed

**Fixed 2026-08-20.** The previous detector substring-matched 24 fixed phrases
and scored **2/6** on real emergencies — missing stroke, respiratory distress,
haemoptysis and overdose — while escalating denials like "i do not have any
chest pain".

It was replaced by `synapse.safety`: stem-proximity matching over a governed
vocabulary, with negation scoping.

| | Old | New |
|---|---|---|
| Repository safety cases | 2/6, 2/6 | **6/6, 6/6** |
| Held-out emergencies (15, not used to build the vocabulary) | — | **15/15** |
| Held-out ordinary queries (9) | — | **0 false positives** |

**What remains a limitation — and it is the important part:**

1. **The vocabulary is engineering-authored and unreviewed.**
   `config/emergency_vocabulary.toml` carries `review_status = "unreviewed"` and
   an empty `reviewed_by`, and the loader refuses to report it as approved while
   that is true. Better recall on cases written by the same person who wrote the
   detector is **not clinical evidence**. Coverage against the set of conditions
   that actually warrant escalation has never been assessed by a clinician.
2. **Family history over-escalates, deliberately.** "my father had a stroke"
   escalates. Suppressing third-party mentions would also suppress "my son
   swallowed some of my pills", a real emergency. Given that a missed emergency
   can kill and a false escalation is an inconvenience, the trade is taken and
   asserted in a test so it stays visible.
3. **It is a lexical approximation, not comprehension.** No parser, no model, no
   understanding. Novel phrasing outside the vocabulary is still missed, and
   nobody knows how much of that there is.
4. **The denominators are tiny.** 6 + 15 emergency phrasings total.

The escalation ordering was always sound — detection runs before retrieval and
cannot be overridden by retrieved content. What was broken was the detector it
called. That is now fixed; whether it is *clinically sufficient* is unanswered.

---

## 2. Evaluation and metrics

- **`min_denominator = 5`.** A gate needs only five observations to fire. This is
  set low so the current fixture exercises every gate; on a real dataset it
  should rise substantially. A safety metric over five cases is not a basis for
  a release decision.
- **All thresholds are provisional.** `configs/quality-gates.toml` carries
  `status = "provisional"` and an empty `approved_by`. Every `min_absolute` is
  `0.0`, deliberately: a floor asserts "the system is at least this good", and
  nobody has established what good means here. A test enforces that no
  impressive-looking floor can be added quietly.
- **Latency is replayed, not measured.** Offline replay reports the latency that
  was recorded, not latency under load. Latency gates are informational for that
  reason, and there has been no load testing of any kind.
- **Cost metrics are structurally present but empty.** `config/pricing.toml`
  ships with zero prices, so cost numbers are zero and mean nothing.
- **LLM-judge metrics are advisory and unvalidated.** They are separated from
  deterministic metrics by design and can never gate a merge. No judge has been
  calibrated against human agreement, because there are no human labels.
- **Adversarial coverage is illustrative.** Six injection cases, all written by
  the same person who wrote the defence. Passing them is not evidence of
  resistance to prompt injection.

## 3. Corpus and sources

- **PubMed abstracts only.** No full text, no guidelines, no drug labels, no
  contraindication database. An abstract frequently omits the population,
  dosing, and harms that a patient question depends on.
- **No PDF ingestion.** It was advertised in the README and module docstring but
  never worked: the loader imported an undeclared package. Removed rather than
  repaired, because a PDF chunk has no verifiable provenance the way a PMID does.
- **Retraction handling is metadata-only.** Retraction status is parsed and
  stored. There is no active re-check of previously ingested sources, so a paper
  retracted after ingestion stays in the corpus until the pack is rebuilt.
- **No recency or evidence-hierarchy weighting at retrieval time.** A 1998 case
  report and a 2025 meta-analysis compete on lexical and semantic similarity
  alone. The evidence-level enum is recorded but does not influence ranking.
- **English only.** No multilingual retrieval, and no evaluation of how the
  system behaves on a non-English query.

## 4. Answers and citations

- **Support checking is lexical, not semantic.** A claim is verified by matching
  a verbatim excerpt from a retrieved chunk after documented normalisation. This
  catches fabricated citations and fabricated quotes. It **cannot** catch a
  claim that accurately quotes a source while misrepresenting its meaning,
  scope, or population. The entailment interface exists; no entailment model is
  wired to it.
- **Numeric consistency checking is narrow.** Numbers in a claim are checked
  against numbers in the supporting excerpt. Unit conversions, ranges, and
  relative-versus-absolute risk are not handled.
- **Abstention is untuned.** The system withholds unsupported claims, which is
  correct, but nobody has measured how often it abstains when it should have
  answered.

## 5. Privacy and security

- **No formal privacy review, no DPIA, no HIPAA assessment.** See
  [PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md) for what is actually known about
  where text goes.
- **Query text leaves the machine.** With an API key configured, the query and
  retrieved chunks are sent to OpenAI. This is inherent to the design, not a bug,
  and it is the single most important fact for anyone considering real users.
- **No authentication, authorisation, rate limiting, or audit trail.** The app
  has no concept of a user account.
- **No data retention policy.** Streamlit session state holds the conversation
  for the session's lifetime. Nothing is persisted by design, and nothing
  enforces that.

## 6. Engineering and operations

- **The legacy prototype is unhardened.** `app.py`, `Data/`, `Retrieval/`,
  `Generation/` are excluded from lint and type-check, are untyped, and use
  `sys.path` manipulation at import time — the root cause of the original
  case-sensitivity import failures.
- **Type checking is scoped, not repo-wide.** `mypy` covers five packages under
  `synapse/`. Everything else is unchecked.
- **CI has never run on GitHub.** All workflows were validated by local
  simulation. The first real run should be expected to surface environment
  issues.
- **`gitleaks` history scanning is unverified** — this working copy has no git
  history to scan.
- **Branch protection is documented, not applied.** `.github/CODEOWNERS`
  contains placeholder teams that do not exist; GitHub silently ignores an
  unresolvable owner, so "Require review from Code Owners" would enforce nothing
  while appearing to.
- **Single maintainer, no on-call.** There is no funded response to a security
  report or a production incident.

## 7. Regulatory

- **Not a medical device, and no regulatory analysis has been performed.** No
  determination has been made about whether an eventual product would fall under
  FDA Clinical Decision Support guidance, EU MDR, or UK MHRA rules. That
  analysis requires counsel and has not been started.
- **No quality management system.** No ISO 13485, no IEC 62304 lifecycle
  process, no design history file.

---

## What would have to change

Ordered, because the ordering is forced — each step depends on the one above it.

1. Recruit a qualified clinical reviewer and record real review metadata.
2. Label a real evaluation set through
   [clinical-labeling-protocol.md](clinical-labeling-protocol.md), sized for
   defensible denominators.
3. Approve sources so the governed path has something to serve.
4. Fix the negation defect and promote `negation_accuracy` to blocking.
5. Re-measure everything against reviewed cases; only then can leadership set
   absolute floors.
6. Commission privacy and regulatory reviews before any real user sees the
   system.

Until step 1 happens, every number here is an engineering regression signal and
nothing more.

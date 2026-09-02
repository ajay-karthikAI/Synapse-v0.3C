# Clinical Labelling Protocol

**Protocol version:** `protocol-1`
**Applies to:** Synapse evaluation dataset (`synapse.evalset`)
**Status:** mechanism implemented. **No case in this repository has been reviewed by a clinician.**

---

## 0. What this protocol is, and what it is not

This document defines *how* a case is labelled and *who* may label it. It does
not define the clinical standard those labels are judged against — that is
§1, and it is not engineering's to write.

The rule underneath everything:

> **Authoring a case is not labelling it. Labelling it is not approving it.
> Only a recorded human review makes a case authoritative, and only a
> gating-eligible case may influence a release.**

---

## 1. Reviewer qualifications — **to be decided by project leadership**

The software enforces *that* a reviewer had a role and *that* their identity is
recorded. It cannot enforce that the reviewer is competent to judge
patient-facing diabetes education for a UK primary-care population, because
that is a clinical and organisational judgement.

The following are **open questions for project leadership**, not settings:

| Question | Why engineering cannot answer it |
|---|---|
| What registration, specialty and years of practice must a reviewer hold? | A clinical governance decision with regulatory implications. |
| Must emergency-category cases be reviewed by someone with acute or emergency experience specifically? | Depends on the risk appetite for the product's escalation behaviour. |
| Who may adjudicate a disagreement between two clinicians? | Requires a seniority model this project does not have. |
| May a reviewer label cases in a topic outside their specialty? | A scope-of-practice question. |
| How many reviewers must agree before a case gates a release? | Currently defaults to 2; the right number is a risk decision. |
| What kappa constitutes adequate agreement for this domain? | The Landis–Koch bands the tooling quotes are a convention, not a standard. |

Until these are answered, `ReviewerRole.CLINICIAN` means only "the operator who
filed this review asserted that role". The software records the assertion; it
does not verify it.

### What the software *does* enforce

- Every reviewer is identified by a **pseudonym** matching `rev_[a-z0-9]{8}`,
  issued out-of-band. Synapse never generates one.
- The pseudonym-to-person mapping lives **outside this repository**, so a clone
  of the dataset never carries reviewer identities.
- An adjudicator may not be one of the reviewers they are overriding.
- One reviewer may file at most one review per case — two reviews from one
  person are not two independent judgements.

---

## 2. Annotation steps

### Step 1 — Author the case (engineering or clinical author)

Write the query as a patient would actually phrase it, including the
hesitations and imprecision real patients use. Assign a provisional category
and expected behaviour, and state `known_limitations`.

Cases are born `pending_review` or `synthetic`. **There is no path by which
authoring produces a reviewed case** — `add-case` refuses any other status.

### Step 2 — Declare provenance and redact (author)

Set `privacy_class` explicitly. The default is `unknown`, which is treated as
strictly as `patient_derived`, so a case cannot inherit a permissive default by
omission. See §5.

### Step 3 — Independent review (reviewer)

```bash
python -m synapse.cli.evalset export-sheet --dataset evals/main --out review-rev_xxxxxxxx.csv
```

Reviewers work in a spreadsheet and record, independently:

- **category** — which failure mode the case exercises;
- **expected_behavior** — what the system must do;
- **graded relevance** — 0 irrelevant, 1 marginal, 2 useful, 3 fully answers;
- **confidence** — 1 to 5;
- **notes** — reasoning, without identifying information.

**Reviewers must not see one another's judgements before filing.** Agreement
computed over reviews that influenced each other measures nothing. Export a
separate sheet per reviewer.

```bash
python -m synapse.cli.evalset import-sheet --dataset evals/main --sheet completed.csv
```

### Step 4 — Agreement (protocol owner)

```bash
python -m synapse.cli.evalset agreement --dataset evals/main --matrix
```

Cohen's kappa for two reviewers, Fleiss' for three or more with uniform counts.
Ragged reviewer counts yield raw agreement only, reported as such rather than
approximated. The `--matrix` flag shows *which* labels get confused, which is
what tells a protocol owner whether two categories need clearer definitions.

### Step 5 — Adjudication (adjudicator)

Any case where reviewers disagree becomes `disagreed_open` and **cannot gate a
release** until resolved. The adjudication record requires a rationale; the
overridden reviewers are named, so the disagreement is preserved rather than
erased.

### Step 6 — Split assignment (engineering)

```bash
python -m synapse.cli.evalset split --dataset evals/main
```

Near-duplicate queries are assigned to the same split as a cluster, so a
question cannot appear in both train and test. See §7.

---

## 3. Grading relevance

| Grade | Meaning |
|---|---|
| 0 | Considered and judged irrelevant. **Not the same as absent** — recording a 0 tells a later reader the document was looked at. |
| 1 | Marginally related; would not be missed. |
| 2 | Useful; contributes to a good answer. |
| 3 | Fully answers the query on its own. |

Graded rather than binary because nDCG needs gradations to distinguish a
perfect ranking from an adequate one.

A case expecting `answer` must grade at least one document 2 or higher — the
schema enforces this. Without it, a case can silently be unpassable, which is
exactly the defect in the evaluation set this project inherited: all four of
its ground-truth PMIDs are absent from the shipped corpus, so its metrics were
structurally zero regardless of retrieval quality.

---

## 4. Conflict resolution

1. **Disagreement is recorded, never averaged.** Individual reviews are stored
   separately; there is no "consensus label" that erases the fact that
   reviewers saw the case differently.
2. **An open disagreement blocks gating.** The case has no settled label, so it
   cannot measure anything.
3. **Adjudication requires a third party.** The adjudicator must not be one of
   the disagreeing reviewers.
4. **A rationale is mandatory.** An adjudication without reasoning is an
   assertion, and gives a later auditor nothing to check.
5. **Repeated disagreement in one category is a protocol defect, not a reviewer
   defect.** If `--matrix` shows the same two categories being confused
   repeatedly, the category definitions need rewriting and the affected cases
   need re-labelling under a new protocol version.

---

## 5. Redaction rules

### Classification

| `privacy_class` | Meaning | Redaction |
|---|---|---|
| `synthetic` | Authored from scratch; describes no real person | Not required |
| `public_derived` | Adapted from already-public material | Not required |
| `patient_derived` | Originates from a real person's words | **Mandatory** |
| `unknown` | Provenance not established | **Mandatory** — treated as patient-derived |

`unknown` is the **default**, deliberately. Unestablished provenance is not a
licence to assume safety, and defaulting to `synthetic` would let real text
bypass redaction by omission.

### What must be removed from `patient_derived` text

- Names, initials, and relationships that identify (`my daughter Dr Patel`)
- Dates more precise than a month, and any date of birth
- Ages over 89
- Geographic detail below a region; clinic, ward or hospital names
- Contact details, NHS/MRN/insurance numbers, appointment references
- Rare conditions or combinations that identify by their rarity — **the one
  most often missed**, because no pattern-matcher catches it and it requires
  someone to think about the population size

### Rules

1. **Redact before review.** A reviewer should never see unredacted text.
2. **Preserve clinical meaning.** Replace with a category marker
   (`[AGE]`, `[LOCATION]`), never delete — deletion changes what is being
   labelled.
3. **Record what was removed, not what it was.** `redaction_notes` says
   "removed a specific hospital name", never the name.
4. **Redaction is verified by a second person.** Self-verified redaction is not
   verified.
5. **`redaction_status: failed` means the case is excluded.** Not retried, not
   partially used — excluded, with a reason.

An unredacted `patient_derived` case **cannot gate a release even with two
clinician reviews**. The gating layer checks redaction before it checks review
count, deliberately.

---

## 6. When a case must be excluded

Set `excluded: true` with an `exclusion_reason`:

| Situation | Why |
|---|---|
| Redaction failed or cannot be verified | Re-identification risk outweighs evaluation value |
| Reviewers cannot agree even after adjudication | No settled label exists to measure against |
| The query is not answerable in any behaviour | The case tests nothing |
| Relevance labels reference documents absent from the corpus | Structurally unpassable |
| The case duplicates another exactly | Double-counts one question in every metric |
| Real patient text was used without a lawful basis | A governance failure, not a data-quality one |
| The case encodes a clinical claim the protocol owner cannot substantiate | Labels must be defensible |

Excluded cases are **marked, not deleted**. Deleting them destroys the record
that they existed and why they were dropped — which is what a later auditor
needs most.

---

## 7. Splits, duplication and leakage

- **Near-duplicate queries are assigned to the same split**, as a cluster.
  Clustering is transitive: if A≈B and B≈C, all three share a split, because
  information would otherwise leak through B.
- **Exact duplicate queries are errors**, wherever they sit.
- **Document leakage is reported, not forbidden.** The same document graded
  relevant in train and test means retrieval was tuned against evidence the
  test set depends on. On a corpus of a few hundred documents this cannot
  always be avoided, so it is measured and stated rather than silently
  accepted — see `evals/DATASET_CARD.md`.

The near-duplicate threshold is 0.70 lexical Jaccard, set low on purpose: a
false positive merely groups two cases into one split, which costs nothing,
while a false negative inflates a test score.

**Its known limitation:** lexical overlap cannot distinguish "should I *stop*
metformin" from "should I *start* metformin" — opposite questions that score
0.667. The validator therefore separately reports any near-duplicate cluster
whose members expect *different behaviours*, so a human sees the cases where
the measure is out of its depth.

---

## 8. Reviewing emergency cases safely

Emergency and negated-emergency cases carry the highest consequence and need
extra handling.

1. **Two reviewers minimum, and never fewer.** The `--allow-single-review`
   relaxation must not be used for the emergency categories.
2. **Escalation is the safe default under uncertainty.** A reviewer unsure
   whether a presentation warrants escalation should label
   `emergency_escalation` and record the uncertainty in `confidence` and
   `notes`. Over-escalation is a usability cost; under-escalation is a harm.
3. **Negated cases are reviewed by the same reviewers, in the same sitting.**
   `emergency_red_flag` and `negated_emergency` are two halves of one judgement
   — reviewing them separately invites inconsistency about what the negation
   changes.
4. **A negated-emergency case may never expect escalation.** The schema
   enforces this: it is the entire purpose of the category. The shipped
   detector substring-matches emergency phrases, so *"I do not have chest
   pain"* escalates today, and without this category that false positive is
   invisible in an aggregate score.
5. **Forbidden claims are mandatory on emergency cases.** At minimum:
   "provides general education instead of escalating" and "suggests waiting for
   the scheduled appointment".
6. **Emergency cases are never used to tune.** They belong in dev or test, not
   train. Tuning against them teaches the system to pattern-match the test set
   rather than to escalate.
7. **A disagreement on an emergency case is escalated to adjudication
   immediately**, not batched. It is the one disagreement that should not wait
   for a review cycle.
8. **Reviewer welfare.** Emergency cases describe distressing presentations.
   Review them in bounded sittings; nobody should label fifty in a row.

---

## 9. Release gating

A case may influence a release decision only when **all** of the following
hold:

- not synthetic;
- not excluded;
- `annotation_status` is `reviewed` or `adjudicated`;
- at least `minimum_reviews_for_gating` independent reviews (default 2);
- no open disagreement;
- redaction discharged if the privacy class requires it;
- labelled against the corpus version being evaluated.

```bash
python -m synapse.cli.evalset gating --dataset evals/main --require-eligible
```

Exit code 1 when nothing qualifies. `--allow-single-review` and
`--allow-unreviewed` exist for bootstrapping and are described as
`UNGOVERNED:` in every report that uses them.

**A metric computed from non-gating cases is informational. It must not block
or approve a release.**

---

## 10. Protocol versioning

`protocol_version` is recorded on every review, so a judgement can be read
against the standard in force when it was made.

Changing this document's rules requires a new protocol version. Reviews filed
under the previous version were made against different instructions, and
whether they carry forward is a decision for the protocol owner — the tooling
records the mismatch rather than resolving it.

---

## 11. Related documents

- `evals/README.md` — how to run the workflows.
- `evals/DATASET_CARD.md` — what the dataset contains and what it does not establish.
- `docs/source-governance.md` — the equivalent separation of duties for sources.
- `docs/quality-architecture.md` §8.1 — the clinical decisions this work waits on.

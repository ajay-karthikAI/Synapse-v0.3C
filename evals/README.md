# Synapse Evaluation Datasets

Tooling and storage for the evaluation dataset that measures whether Synapse
behaves correctly — and, more importantly, the mechanism that stops an
unreviewed case from deciding whether a release ships.

> **Nothing in this directory is clinician-reviewed.** The only cases present
> are synthetic illustrations in `examples/`, marked and excluded from
> release-gating metrics by construction.

## Layout

```
evals/
├── README.md                          this file
├── DATASET_CARD.md                    what a dataset contains and does not establish
└── examples/
    └── synthetic_unreviewed.jsonl     8 synthetic cases, one per category
```

A working dataset is a directory of its own:

```
evals/<dataset-id>/
├── manifest.json     version, counts, gating policy, provenance notes
├── cases.jsonl       one case per line — the reviewable artifact
└── CHANGELOG.md      version history
```

## The rule

> **An unreviewed case cannot gate a release.**

Engineering can author cases, compute splits, detect leakage and measure
agreement. None of that makes a case authoritative. Only a recorded human
review does, and `synapse.evalset.gating` is where that boundary is enforced.

A case is gating-eligible only when it is not synthetic, not excluded,
`reviewed` or `adjudicated`, carries at least two independent reviews, has no
open disagreement, has discharged any redaction obligation, and was labelled
against the corpus version being evaluated. Every exclusion carries a reason.

## Workflows

```bash
# 1. Create a dataset
python -m synapse.cli.evalset init \
  --dataset evals/main --dataset-id synapse-main --corpus-version corpus-v3

# 2. Author cases (only 'synthetic' or 'pending_review' can be created)
python -m synapse.cli.evalset add-case --dataset evals/main --case case.json

# 3. Export a review sheet — one per reviewer, so judgements stay independent
python -m synapse.cli.evalset export-sheet \
  --dataset evals/main --out review-rev_xxxxxxxx.csv --reviewer-id rev_xxxxxxxx

# 4. File completed reviews
python -m synapse.cli.evalset import-sheet --dataset evals/main --sheet completed.csv

# 5. Inter-annotator agreement (Cohen's / Fleiss' kappa)
python -m synapse.cli.evalset agreement --dataset evals/main --matrix

# 6. Resolve a disagreement
python -m synapse.cli.evalset adjudicate \
  --dataset evals/main --case-id c-0001 --record adjudication.json

# 7. Assign splits without separating near-duplicates
python -m synapse.cli.evalset split --dataset evals/main

# 8. Validate (add --corpus to check relevance labels against real documents)
python -m synapse.cli.evalset validate --dataset evals/main --corpus artifacts/corpus-v3

# 9. What may gate a release? This is what CI runs.
python -m synapse.cli.evalset gating --dataset evals/main --require-eligible

# 10. Compare versions and append a changelog entry
python -m synapse.cli.evalset diff --from evals/main-v1 --to evals/main --changelog
```

## Categories

Each exercises a different **failure mode**, not a different medical topic:

| Category | What it tests |
|---|---|
| `ordinary_education` | Straightforward, answerable from the corpus |
| `ambiguous_symptoms` | Under-specified presentation; should route rather than guess |
| `out_of_scope` | Irrelevant; should abstain |
| `adversarial_injection` | Instruction override and prompt extraction |
| `medication` | Drug names and doses — highest-consequence text |
| `emergency_red_flag` | Must escalate |
| `negated_emergency` | Emergency vocabulary under negation; must **not** escalate |
| `insufficient_evidence` | In scope, but the corpus cannot support an answer |

`negated_emergency` exists because the shipped detector substring-matches
emergency phrases: *"I do not have chest pain"* escalates today. Without a
category isolating negation, that false positive is invisible in an aggregate
score.

## Expected behaviours

`answer` · `abstain` · `route_to_staff` · `emergency_escalation` ·
`reject_unsafe_instruction`

Routing and escalation are separate because they are different actions with
different urgency: routing hands off to staff, escalation says stop and seek
care now.

## Splits and leakage

Near-duplicate queries are assigned to the same split as a cluster, so a
question cannot appear in both train and test. Clustering is transitive.
Document-level leakage — the same source graded relevant in two splits — is
*reported* rather than forbidden, because a small corpus cannot always avoid
it. See `DATASET_CARD.md`.

## Reviewer identity

Reviewers are identified by pseudonyms matching `rev_[a-z0-9]{8}`, issued
out-of-band. **Synapse never generates one.** The pseudonym-to-person mapping
lives outside this repository, so a clone of a dataset never carries reviewer
identities.

## See also

- `docs/clinical-labeling-protocol.md` — annotation steps, conflict resolution,
  redaction rules, and how emergency cases are reviewed safely.
- `docs/source-governance.md` — the equivalent separation of duties for sources.

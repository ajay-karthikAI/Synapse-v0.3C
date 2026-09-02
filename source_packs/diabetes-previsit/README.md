# Diabetes Pre-Visit Education (UNAPPROVED EXAMPLE)

> **UNAPPROVED EXAMPLE.** This pack is demonstration content. It is not clinically
> reviewed and must never be used to serve patient-facing traffic.

**Pack ID:** `diabetes-previsit` · **Version:** `0.1.0` · **Approval state:** `unapproved_example`

Demonstration source pack showing the governance structure. Contains synthetic placeholder sources only. NOT clinically reviewed and NOT usable for patient-facing traffic.

## Scope

- **Intended population:** EXAMPLE ONLY — adults aged 18+ with an existing type 2 diabetes diagnosis, attending a routine primary-care follow-up
- **Intended use:** EXAMPLE ONLY — pre-visit patient education and preparation of questions to ask a clinician
- **Excluded uses:**
  - Diagnosis of any condition
  - Medication dosing, initiation, or adjustment
  - Interpretation of an individual patient's test results
  - Emergency or urgent-care triage
  - Paediatric, pregnancy, or type 1 diabetes populations

## Contents

All four sources are **synthetic placeholders**. They carry `example:` identifiers and
`example.org` URLs, and none corresponds to a real publication. No real source was
fetched or labelled to build this pack.

| State | Count | Meaning |
|---|---|---|
| `discovered` | 3 | Automation added them. **No human has reviewed them.** |
| `superseded` | 1 | Replaced by a newer edition. A mechanical fact, not a judgement. |
| `approved` | 0 | Approving a source requires a real clinician review record. None exists. |

This pack contains **no review records and no reviewer identities**, because inventing
either would fabricate a clinical decision that nobody made.

## Why this pack can never serve traffic

`approval_state = unapproved_example` is refused outright by
`synapse.governance.eligibility`. Even with every source approved, `build-index` would
still refuse it without an explicit `--allow-example-packs` override — which is itself
recorded in the output as ungoverned.

## Review

Requires a `clinician` reviewer under rubric `rubric-1`, re-reviewed every 365 days.

See `docs/source-governance.md` for the operational workflow.

# Dataset Card — Synapse Evaluation Set

**Status:** framework implemented; **no reviewed dataset exists**.
**Contents of this repository:** 9 synthetic, unreviewed example cases.
**Gating-eligible cases:** **0**.

---

## 1. Summary

The Synapse evaluation set measures whether the application answers, abstains,
routes or escalates correctly, and whether its answers are grounded in cited
evidence.

This card describes the framework and the example cases shipped with it. It
does **not** describe a validated dataset, because none has been created: no
clinician has reviewed any case.

## 2. What is in this repository right now

| | |
|---|---|
| Cases | 9, all in `examples/synthetic_unreviewed.jsonl` |
| Annotation status | `synthetic` (all 9) |
| Reviews recorded | 0 |
| Reviewer identities | none |
| Gating-eligible | **0** |
| Categories covered | all 9, one case each |
| Corpus version referenced | `example-corpus-v0` — deliberately not a real corpus |

Every example is marked three independent ways: `is_synthetic: true`,
`annotation_status: synthetic`, and a `known_limitations` field beginning
`SYNTHETIC AND UNREVIEWED`. The gating layer refuses each independently, and
`tests/test_evalset_workflows.py::TestShippedSyntheticExamples` asserts all of
it against the shipped file.

## 2b. Change log

### CI fixture dataset `synapse-ci` 0.1.0 -> 0.2.0 (2026-08-31)

Six `followup_resolution` cases added, carrying conversational `context`
(`eval_case` schema 1.1). They drive `followup_resolution_at_k`: did a follow-up
question retrieve a document a human labelled relevant?

**The number these produce is not a product measurement, and must not be quoted
as one.** Like the other 48 cases in `evals/ci`, they are engineering-authored
against a four-chunk fixture corpus, and their recorded responses were written
by hand rather than observed from a run. What they establish is that the metric
computes, aggregates, and gates correctly. What they cannot establish is how
well the shipped system resolves follow-ups, because no shipped system was
involved in producing them.

The gate on this metric is therefore `informational`, and every case is
`is_synthetic: true`, so it is refused by the gating layer three independent
ways.

Bumping the dataset version invalidated the previous approved baseline by
design: `evals/baselines/approved/` was regenerated at
`approved-baseline-0.4.0`. A dataset change always requires a new baseline,
because regression gates compare like with like.

To make this metric mean something, the requirement is the same as for every
other metric here: real cases, a real corpus, and human relevance labels. See
section 5.

## 3. Intended use

- Exercising the schema, CLI and validation rules.
- Demonstrating the labelling workflow end to end.
- Regression-testing the framework itself.

## 4. Out-of-scope use

- **Measuring model quality.** Eight synthetic cases measure nothing.
- **Gating a release.** Refused by construction.
- **Claiming clinical validation.** No clinician has seen these cases.
- **Publishing a score.** Any number computed from this file is a smoke test.

## 5. Provenance

All example cases were authored by engineering as schema illustrations. None
derives from a real patient query, a support ticket, or production traffic.
`privacy_class` is `synthetic` and no redaction was required.

The example cases reference `example:` document identifiers that do not exist
in any corpus. They are unpassable by design — they demonstrate structure, not
retrieval quality.

## 6. Known limitations

### Of the shipped examples

1. **Nine cases, one per category.** Nothing can be measured at this size.
2. **No reviews**, so inter-annotator agreement is not computable.
3. **Unpassable relevance labels**, pointing at synthetic documents.
4. **No split assignment** — synthetic cases take part in no split.

### Of the framework

5. **Near-duplicate detection is lexical.** It cannot distinguish "should I
   *stop* metformin" from "should I *start* metformin" (0.667 similarity,
   just below the 0.70 threshold). The validator separately reports
   near-duplicate clusters whose members expect different behaviours, but a
   human still has to look.
6. **Document leakage is reported, not prevented.** On a corpus of a few
   hundred documents, the same source will legitimately be relevant to
   several cases. Test scores are optimistic to the extent this occurs, and
   the number is printed by `split` and `validate`.
7. **Kappa is unstable at small n.** Below roughly 20 multi-reviewed cases the
   agreement report says so explicitly.
8. **Ragged reviewer counts defeat both kappas.** When cases carry differing
   numbers of reviews, raw agreement is reported alone — and it overstates
   agreement on skewed label distributions.
9. **`ReviewerRole.CLINICIAN` is an assertion, not a verification.** The
   software records that an operator claimed the role; it cannot check it.

## 7. Ethical and privacy considerations

- Reviewer identities are pseudonymous and held outside this repository.
- `privacy_class` defaults to `unknown` and is treated as strictly as
  `patient_derived`, so a case cannot inherit a permissive default by omission.
- Unredacted patient-derived cases cannot gate a release **even with two
  clinician reviews** — redaction is checked before review count.
- Emergency cases describe distressing presentations; the protocol asks for
  bounded review sittings.

## 8. What would make this dataset usable

1. **Project leadership decides reviewer qualifications**
   (`docs/clinical-labeling-protocol.md` §1). Until then no case can leave
   `pending_review`.
2. **A real corpus version to label against**, so relevance labels reference
   documents that exist.
3. **Enough authored cases per category** to distinguish real differences —
   substantially more than one each.
4. **Two independent clinician reviews per case**, filed without seeing each
   other's judgements.
5. **A measured baseline**, so thresholds are set from evidence rather than
   guessed.

## 9. Maintenance

Label changes and split reassignments make scores incomparable across
versions; `diff --changelog` states so explicitly in the generated entry.
Corpus-version changes invalidate every relevance label.

## 10. Citation

This dataset is not published and should not be cited as a benchmark.

<!--
Synapse pull request.

This template asks for EVIDENCE, not description. "Tested locally" is not
evidence; a pasted command and its output is. The evaluation section is
mandatory because a change can pass every unit test and still make the system
worse at the thing it exists to do.
-->

## What changed and why

<!-- One paragraph. What problem does this solve? Link the issue if there is one. -->

## Evidence

<!-- Paste the actual commands and their actual output. Not a summary of it. -->

```
$ ruff check .
$ ruff format --check synapse tests
$ mypy synapse/schemas synapse/evals synapse/answer synapse/corpus synapse/index
$ pytest -m "not live"
```

<details><summary>Output</summary>

```
(paste here)
```

</details>

## Evaluation impact

<!-- Required. If you believe there is none, say why — do not delete the section. -->

- [ ] I ran the offline evaluation and checked the gates:

```
$ python -m synapse.evals.run --dataset evals/ci --fixture evals/ci/system_responses.jsonl \
    --corpus evals/ci --output artifacts/evals/local --offline
$ python -m synapse.cli.check_gates --results artifacts/evals/local/results.json \
    --baseline evals/baselines/approved/results.json
```

**Gate result:** <!-- PASSED / FAILED — and if any gate moved, which and by how much -->

**Metrics that moved:** <!-- e.g. recall_at_5 0.83 -> 0.81 (-0.02, within tolerance). "None" is a valid answer. -->

- [ ] This change does **not** alter the approved baseline. <!-- If it does, say why the new baseline is correct and who approved it. -->

## Safety and governance

- [ ] No unsupported claim can reach a patient as a result of this change.
- [ ] The permanent disclaimer still renders on every action (answer, abstain, staff, emergency).
- [ ] No user-, model- or source-controlled content is rendered unescaped.
- [ ] Emergency routing behaviour is unchanged, **or** the change is called out below and has clinical review.
- [ ] No reviewer identity, signature or approval record was fabricated.
- [ ] Nothing in this PR describes synthetic or unreviewed evaluation cases as clinical validation.

<!-- If any box above is unchecked, explain here. An unchecked box with an
     explanation is fine; an unchecked box with silence is not. -->

## Clinical review

- [ ] This change touches patient-facing text, evidence definitions, source
      governance, evaluation labels, or release thresholds → **clinical review
      required** (see CODEOWNERS).
- [ ] This change is engineering-only.

## Risk and rollback

**Risk:** <!-- What could this break that tests would not catch? -->

**Rollback:** <!-- How is this reverted? Is there state (artifacts, baselines) that also needs reverting? -->

## Documentation

- [ ] Docs updated, or no documented behaviour changed.
- [ ] Thresholds changed → `configs/quality-gates.toml` version bumped and the rationale recorded.

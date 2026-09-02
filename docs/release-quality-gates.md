# Release Quality Gates

**Applies to:** `.github/workflows/`, `configs/quality-gates.toml`, `synapse.evals.gates`
**Gate configuration status:** `0.1.0-provisional` — **not approved by clinical or product leadership**

---

## 1. What these gates are, and what they are not

They are **engineering regression signals**. They answer one question: *did this
change make a measurable thing worse than the last approved baseline?*

They are **not** clinical validation. The evaluation dataset that ships with
this repository is synthetic and unreviewed — every case is engineering-authored
and excluded from release-gating aggregates by construction. A green build means
nothing regressed against a synthetic reference. It does not mean the system is
safe, accurate, or fit for patients.

Every gate report says this, in the job summary, above the numbers. CI asserts
it too: `artifact-validation` fails if the CI dataset ever contains a
gating-eligible case, so the caveat cannot quietly become false.

---

## 2. Thresholds are provisional placeholders

`configs/quality-gates.toml` carries `status = "provisional"` and an **empty**
`approved_by`. The gate runner treats an empty approver as unapproved regardless
of the status label — the field is the evidence, not the label — and banners
every report accordingly.

Two deliberate choices:

**Absolute floors are 0.0 almost everywhere.** A floor asserts "the system is at
least this good". Nobody has established what good is for this product, so
writing `min_absolute = 0.85` would be a quality claim this repository cannot
support. A test asserts every shipped floor is 0.0, so an impressive-looking
number cannot be added without that test failing.

**Regression tolerances do the real work.** A regression gate needs no absolute
claim — only a baseline and a tolerance. That is why the initial gate set is
almost entirely regression-based.

Safety metrics tolerate **zero** regression. That is the conservative default
rather than a calibrated decision, and it is cheap to hold while the numbers are
small.

### What leadership needs to decide

| Question | Why engineering cannot answer it |
|---|---|
| What is the minimum acceptable emergency sensitivity? | A floor here is a patient-safety claim |
| How much retrieval regression is tolerable in a release? | A product risk trade-off |
| Should `negation_accuracy` block once the detector is fixed? | Depends on the accepted false-positive rate |
| What counts as an approved baseline, and who approves one? | A governance decision |

Until those are answered, `approved_by` stays empty.

---

## 3. The workflows

| Workflow | Job | Blocking | What it does |
|---|---|---|---|
| `ci.yml` | `lint` | yes | `ruff check .` |
| | `format` | yes | `ruff format --check synapse tests` |
| | `typecheck` | yes | `mypy` over the fully-annotated layers |
| | `unit-tests` | yes | `pytest -m "not live"` with coverage |
| | `integration-tests` | yes | Whole pipelines against committed fixtures |
| | `artifact-validation` | yes | Source pack, eval dataset, manifests, schemas |
| | `offline-evaluation` | yes | Offline eval + quality gates + job summary |
| | `ci-passed` | yes | Single aggregate status for branch protection |
| `security.yml` | `dependency-audit` | yes | `pip-audit`, plus weekly on a schedule |
| | `dependency-review` | yes (PR) | Diffs the dependency manifest, fails on high severity |
| | `secret-scan` | yes | `gitleaks` over full history |
| | `filesystem-secret-check` | yes | No `.env`, key material, or provider key patterns tracked |

### No secrets, no external calls

`ci.yml` declares `permissions: contents: read` and references **no secrets at
all**. A pull request from a fork therefore cannot exfiltrate anything, because
there is nothing in the environment to exfiltrate.

Requirement 6 is *enforced*, not assumed: `unit-tests` fails the job if
`OPENAI_API_KEY`, `NCBI_API_KEY` or `ANTHROPIC_API_KEY` is set at all. Tests
deselect the `live` marker by default, and the evaluation replays committed
fixtures.

> **One honest distinction.** `pip-audit` and `dependency-review` query a
> vulnerability database, and `gitleaks` may contact GitHub. Those are
> *infrastructure* calls, not application ones. No workflow calls OpenAI,
> PubMed, or any other model or content provider.

### `gitleaks` and organisation accounts

`gitleaks-action` requires a `GITLEAKS_LICENSE` for organisation accounts. It is
deliberately **not** wired to a secret here, because this workflow runs on pull
requests including from forks, and a fork PR must never receive a secret.

For an organisation repository, choose one:

- move the job to a workflow triggered only on `push` to `main`, where exposing
  a secret is safe; or
- use GitHub's built-in secret scanning (Settings → Code security), which needs
  no workflow at all.

`filesystem-secret-check` runs regardless, with no third-party action and no
token, so fork PRs still get a credential check.

### Action pinning

Every action is pinned by **commit SHA**, not tag. A tag is mutable: an attacker
who compromises an action repository can repoint `v4` at malicious code, and
every workflow pinned to the tag picks it up silently. Dependabot keeps the SHAs
current — a pin nobody maintains is a frozen vulnerability.

---

## 4. Blocking gates

Initial blocking set, per requirement:

| Gate | Enforced by |
|---|---|
| Tests pass | `unit-tests`, `integration-tests` |
| Schemas and manifests validate | `artifact-validation` |
| No artifact hash mismatch | `artifact-validation` → `test_manifest_and_integrity.py` |
| No unsupported citation displayed in fixtures | `integration-tests` → `test_answer_snapshots.py`, `test_answer_adversarial.py` |
| Emergency false-negative count does not increase | `gates.emergency_false_negative_count`, `max_increase = 0` |
| No retrieval nDCG/Recall drop beyond tolerance | `gates.recall_at_5`, `gates.ndcg_at_5`, `max_regression = 0.05` |
| No unsupported-claim-rate rise beyond tolerance | `gates.unsupported_claim_rate`, `max_regression = 0.02` |

Note the split: some gates are **metric thresholds** evaluated by
`synapse.cli.check_gates`; others are **jobs** whose success is required. Both
are wired into `ci-passed`.

### Informational gates

`mrr`, `hit_at_5`, `citation_completeness`, `latency_total_p95_ms`,
`estimated_cost_usd`, `negation_accuracy`, and the negated-emergency category
gate. They appear in every report and **never** fail a build. This is a
category, not a severity — a safety regression can never be argued down to
informational.

`negation_accuracy` is informational for a specific, documented reason: the
shipped red-flag detector substring-matches, so *"I do not have chest pain"*
escalates. That is a **known, tracked defect** (H4 in
`docs/quality-architecture.md`), not a regression. Blocking it today would fail
every build on a bug that is already scheduled. It becomes blocking when the
detector is fixed.

---

## 5. How a gate can fail, and how it cannot silently pass

Three properties, each with a test:

**A skipped gate is not a pass.** A metric with fewer than `min_denominator`
observations, or one that is absent or undefined, is reported as `SKIP` —
counted and listed, never as success. A gate suite where most gates skip is
decorative, which is why the CI fixture is sized so that **zero** gates skip
(asserted by `test_baseline_passes_its_own_gates`).

**An incomparable baseline fails closed.** A baseline from a different dataset
version, corpus version, seed or run mode is not a baseline. Comparing against
one produces the classic false green: the reference point moves and everything
appears to improve.

**Direction is per metric.** `LOWER_IS_BETTER` covers unsupported-claim rate,
false-negative and false-positive rates, violation rates, error and timeout
rates, latency and cost. Treating every delta as higher-is-better would report a
doubled hallucination rate as an improvement.

---

## 6. The failure report

A gate suite that reports only "FAILED" trains people to re-run it rather than
read it. The summary names, for every failure: the gate, the current value, the
baseline value, the delta, and the tolerance it exceeded — with per-category
gates labelled `category/metric`.

Example from an injected regression (one missed emergency, one retrieval miss):

```
Quality gates: FAILED
  blocking failures: 5

  BLOCKING FAILURES:
    recall_at_5: regressed by -0.0556, tolerance is 0.0500
    ndcg_at_5: regressed by -0.0556, tolerance is 0.0500
    emergency_false_negative_count: increased by +1, tolerance is +0
    emergency_sensitivity: regressed by -0.1667, tolerance is 0.0000
    emergency_red_flag/behavior_correct: regressed by -0.1667, tolerance is 0.0000
```

Reports are uploaded as CI artifacts **even when the gates fail** — that is when
they are most needed.

---

## 7. Updating the baseline

> ### Baseline corrected 2026-08-20 — `approved-baseline-0.1.0` → `0.2.0`
>
> The previous baseline recorded `emergency_sensitivity = 100%` and
> `negation_accuracy = 100%`. Those numbers were **fabricated**: the evaluation
> fixture stores hand-authored ideal responses, so the safety metrics scored the
> fixture rather than the shipped detector. The harness now recomputes the
> escalation decision from the real detector, and the baseline was regenerated
> to match: **33.3%** and **33.3%**, with specificity **90.5%**.
>
> This is the inverse of the failure mode rule 4 below prohibits. The baseline
> was not regenerated to turn a red build green — it was corrected **downward**,
> to stop a green build from being a lie. Against the old baseline the corrected
> run produced 3 blocking failures, including
> `emergency_false_negative_count: increased by +4`; against the new one it
> passes, and a further regression of even one missed emergency still fails.
>
> The system did not change. The measurement started telling the truth.



The baseline lives at `evals/baselines/approved/results.json` and is checked in.
Changing it changes what every future build is measured against, so:

1. Explain in the PR **why** the new baseline is correct — an improvement, a
   dataset change, or a deliberate accepted regression.
2. Get review from both CODEOWNERS populations if the change touches evaluation
   cases or thresholds.
3. Bump `meta.version` in `configs/quality-gates.toml` if any threshold moved,
   and record the rationale there. `git blame` on that file is the audit trail.
4. Never regenerate a baseline to make a red build green. That is the one
   failure mode this whole system exists to prevent.

`--warn-only` exists for bootstrapping a brand-new baseline. It announces itself
loudly in the log; a permanently warn-only pipeline is a broken pipeline.

---

## 8. Branch protection setup

**These steps must be performed by hand in GitHub settings. Nothing in this
repository changes them, and nothing should — a repository that can weaken its
own protections offers none.**

Settings → Branches → Add branch protection rule for `main`:

1. **Require a pull request before merging**
   - Required approvals: **1** minimum (2 recommended once teams exist)
   - ☑ Dismiss stale approvals when new commits are pushed
   - ☑ **Require review from Code Owners**
2. **Require status checks to pass before merging**
   - ☑ Require branches to be up to date before merging
   - Required checks:
     - `CI passed`
     - `Dependency vulnerabilities`
     - `Secret scanning`
     - `No credentials committed`
3. **Require conversation resolution before merging**
4. ☑ **Do not allow bypassing the above settings** — including administrators
5. ☐ Allow force pushes — **leave off**
6. ☐ Allow deletions — **leave off**

`CI passed` is a single aggregate job that depends on all the others, so adding
a CI job later does not require touching branch protection.

### Before this is meaningful

`.github/CODEOWNERS` contains **placeholder teams that do not exist**. GitHub
silently ignores an unknown owner rather than erroring, so "Require review from
Code Owners" against the shipped file enforces **nothing while appearing to**.

Replace every `@synapse/*` handle with a real team first. The file models two
distinct populations on purpose: `@synapse/engineering` for code, and
`@synapse/clinical` for anything that changes what a patient is told, what
counts as evidence, or what the system may claim.

---

## 9. Running the checks locally

```bash
ruff check .
ruff format --check synapse tests
mypy synapse/schemas synapse/evals synapse/answer synapse/corpus synapse/index
pytest -m "not live"

python -m synapse.cli.source_pack validate --pack source_packs/diabetes-previsit
python -m synapse.cli.evalset validate --dataset evals/ci

python -m synapse.evals.run --dataset evals/ci \
  --fixture evals/ci/system_responses.jsonl --corpus evals/ci \
  --output artifacts/evals/local --offline

python -m synapse.cli.check_gates \
  --results artifacts/evals/local/results.json \
  --baseline evals/baselines/approved/results.json \
  --config configs/quality-gates.toml
```

See `CONTRIBUTING.md` for the full local loop.

---

## 10. Known limitations

1. **The gates run against synthetic data.** Every CI case is engineering-
   authored and unreviewed. The gates verify the *mechanism*, not the system.
2. **`min_denominator = 5` is low.** It is set so the current fixture exercises
   every gate. On a real dataset it should rise — a 5-case safety metric is a
   weak basis for a release decision.
3. **Offline latency is replayed, not measured.** Latency gates are
   informational for that reason.
4. **Cost gates are meaningless until `config/pricing.toml` is populated.** The
   shipped table has zero prices and warns about it.
5. **`dependency-review` needs GitHub Advanced Security on private repos.** On a
   private repo without it, the job will fail; drop it and rely on `pip-audit`.
6. **Nothing here has been executed on GitHub.** The workflows were validated
   locally — see §11 of the delivery notes for exactly what was and was not
   verified.
7. **`gitleaks` history scanning is unverified.** The repository has no git
   history to scan locally, so only `filesystem-secret-check` was exercised.

### What the audit gate caught on its first real run

`pip-audit` is not decorative — its first honest run failed, with 45 advisories
across four packages. All were remediated rather than suppressed:

| Package | Was | Now | Advisories cleared |
|---|---|---|---|
| `pypdf` | 5.9.0 | 6.15.0 | 40 (incl. `PYSEC-2026-3655`) |
| `langchain` | 1.2.15 | 1.3.9 | `PYSEC-2026-2192` |
| `langchain-core` | 1.3.0 | 1.4.6 | `PYSEC-2026-2564` |
| `setuptools` | 79.0.1 | ≥83 | `PYSEC-2026-3447` |
| `pip` | 24.0 | 26.1.2 | 6 |

`langchain-core` is pinned to 1.4.6 rather than the advisory's minimum 1.3.3
because `langchain` 1.3.9 requires `>=1.4.6`; the lower pin does not resolve.
The upgrade set was re-verified against the chunking path the application
actually uses, and no suppression file or ignore-list was added — an audit gate
with an ignore-list is an audit gate that has been switched off.

---

## 11. Related documents

- `CONTRIBUTING.md` — the local development loop.
- `docs/evaluation-metrics.md` — what each metric means and its denominator.
- `docs/citation-integrity.md` — what "grounded" means.
- `docs/clinical-labeling-protocol.md` — reviewer qualifications, still open.
- `docs/quality-architecture.md` §8.1 — the clinical decisions this waits on.

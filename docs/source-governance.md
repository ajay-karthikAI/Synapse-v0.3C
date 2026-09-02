# Source-Pack Governance

**Applies to:** `synapse` 0.1.0 · `synapse.governance`
**Status:** mechanism implemented and tested. **No source pack in this repository is clinically approved.**

---

## 1. The distinction this exists to enforce

> **Automated discovery establishes that a publication exists.
> Only a recorded human review establishes that it may be used.**

An ingestion run can determine that a paper exists, sits in a topic area, and
carries certain publisher metadata. It cannot determine whether that paper is
appropriate *for a particular patient population, in a particular product, for a
particular purpose*. That is a clinical judgement, and the only evidence that
one occurred is a review record naming who made it and why.

Every mechanism in this document exists to keep those two things apart, and to
make the second one impossible to fake.

A **source pack** is the unit of that governance: a versioned collection of
sources reviewed and approved for one narrowly defined product scope. Scope is
part of the pack, not an assumption around it — the same paper can be entirely
appropriate for one pack and inappropriate for another.

---

## 2. Directory layout

```
source_packs/<pack-id>/
├── manifest.json     governance record: scope, policy, approval state, integrity
├── sources.jsonl     one source per line — the artifact a reviewer reads
├── CHANGELOG.md      version history; content changes are recorded here
├── README.md         what the pack is for, in prose
└── .versions/        snapshots of previous versions, written by `version`
```

`sources.jsonl` is uncompressed and self-contained. Each entry carries its full
citation — URL, identifiers, title, authors, journal, dates, evidence type —
rather than referencing a corpus artifact, because a reviewer must be able to
judge an entry without joining it against anything else.

---

## 3. Lifecycle states

| State | Meaning | Who may set it |
|---|---|---|
| `discovered` | Automation found it. **No human has looked at it.** | Automation |
| `screened` | A human triaged it as in-scope, but has not approved it | Human review |
| `approved` | A qualified clinician approved it for this pack's scope | Human review |
| `rejected` | A human reviewed and excluded it, with a reason | Human review |
| `expired` | A prior approval passed its review-due date | Automation |
| `superseded` | Replaced by a newer source. **Terminal.** | Automation |

### The transition table

```
discovered ──human──► screened ──human──► approved
    │                    │                   │
    │                    │                   ├──automation──► expired ──human──► screened / approved
    └──human──► rejected ◄──human────────────┘
                    │
                    └──human──► screened          (re-review)

any non-terminal state ──automation──► superseded  (terminal)
```

Two transitions are machine-applicable because they record **facts**, not
judgements: a date passing (`expired`), and a newer edition existing
(`superseded`). Everything else requires a human review record.

`discovered → approved` is deliberately **not** a permitted transition. Screening
is where a human first looks at the source at all; skipping it would make
approval a single unchecked step.

The table lives in `synapse/governance/states.py` as data, so it can be read,
tested and audited in one place — and so adding a state without deciding its
actor is impossible.

---

## 4. Separation of duties

| Role | May do | May **not** do |
|---|---|---|
| **Automation** (ingestion, CI) | Create `discovered` entries; apply `expired`; apply `superseded` | Screen, approve, reject; create a reviewer identity; approve a pack |
| **Reviewer** (clinician) | Screen, approve, reject sources against the pack scope | Review sources whose discovery query they configured |
| **Pack owner** (engineering) | Define scope and policy; configure discovery; cut versions | Approve sources; alter a filed review |
| **Approver** | Approve the pack once all sources are resolved | Approve a pack whose validation fails |

The separation rule in force is recorded *in the pack itself*
(`reviewer_requirements.separation_of_duties`), so it is auditable rather than
tribal knowledge. The default:

> The reviewer of a source must not be the person who configured its discovery
> query.

### Reviewer identity

`reviewer_id` is a **pseudonym** matching `rev_[a-z0-9]{8}`, issued out-of-band
by whoever administers the reviewer register. The pseudonym-to-person mapping is
held **outside this repository**, so a clone of a source pack never carries
reviewer identities.

**Synapse never generates a reviewer identifier.** If it did, a review record
would prove only that this software ran. Every `reviewer_id` in a pack was typed
in by an operator. The schema additionally rejects anything name-shaped, and
rejects an `@` in review comments.

---

## 5. Enforcement rules

### 5.1 No approval without its evidence

`approved` is not constructible without the fields that make it meaningful.

**At source level**, `lifecycle_state = "approved"` requires a `ClinicianReview`
with an `approved` decision, a `clinician` reviewer, and all six assessment
fields substantively answered. Placeholder answers (`n/a`, `-`, `tbd`) are
rejected — a template filled in with dashes would otherwise satisfy a length
check and produce a review documenting nothing.

**At pack level**, `approval_state = "approved"` requires `approved_at`,
`approved_by`, `review_due_at`, at least one source, and no source still in
`discovered` or `screened`. An approved pack with pending sources is ambiguous:
a reader cannot tell whether those sources were considered and deferred, or
simply forgotten.

The converse is enforced too: approval metadata on a non-approved pack is a
validation error, because lingering fields make a later reader believe a
decision exists.

### 5.2 Automation can only discover

`import_discovered()` has **no** `lifecycle_state` parameter and **no** `review`
parameter. There is no argument a caller could pass to skip review. Re-running
discovery skips sources already in the pack rather than overwriting them, so a
nightly job can never reset a completed review back to `discovered`.

### 5.3 Ineligible sources never reach production

Excluded by default from any production index:

- `discovered` and `screened` — short of approval;
- `rejected`, `expired`, `superseded`;
- anything `retracted` or under an `expression_of_concern`, **checked before
  lifecycle state**, so a source approved before its retraction was published is
  still excluded;
- every source in an `unapproved_example` pack, refused outright.

`EligibilityPolicy` carries an `allow_unapproved` escape hatch for local
experimentation. It is recorded wherever it is used and described as
`UNGOVERNED: …` so it cannot be mistaken for a governed build. It still does not
override the retraction guard.

### 5.4 Content changes require a version, and revoke approval

The manifest's `corpus_sha256` is an order-sensitive digest over the sources.
Any change — content, metadata, or lifecycle state — alters it, and `validate`
fails with `corpus_digest_mismatch` if the manifest was not rewritten.

More consequentially:

> **Changing a pack's contents revokes its approval.**

A clinician approved a specific set of sources for a specific scope. Adding a
source, removing one, or changing the scope means the thing that was approved no
longer exists; carrying the approval forward would attribute to the reviewer a
decision they never made. `bump_version()` therefore returns an approved pack to
`draft` and clears `approved_at` / `approved_by` / `review_due_at` — automatically,
rather than relying on an operator to remember.

| Level | Trigger |
|---|---|
| `major` | Scope, selection policy, or reviewer requirements changed |
| `minor` | Sources added or removed |
| `patch` | Source metadata corrected, or a review record filed |

### 5.5 Pack and index must agree before serving

`require_pack_index_agreement()` performs two checks:

1. the index names the pack version that authorised it
   (`source_pack_version = "<pack-id>@<semver>"`) — an index that cannot say
   which governance decision authorised it is unservable;
2. the pack digest recorded at build time matches the pack's current digest —
   otherwise the pack changed after the index was built, and the index is
   serving content the current pack no longer governs.

---

## 6. Operational workflow

### Step 1 — Define the pack (pack owner)

```bash
python -m synapse.cli.source_pack init \
  --pack source_packs/my-pack --pack-id my-pack \
  --title "..." --description "..." \
  --population "adults aged 18+ with ..." \
  --intended-use "pre-visit patient education" \
  --excluded-use "Diagnosis of any condition" \
  --excluded-use "Medication dosing or adjustment" \
  --topic "..." --rubric-version rubric-1
```

A pack is born in `draft`. **There is no flag on `init` that creates an approved
pack.**

Spend the time on `--excluded-use`. A scope that states only what a pack is *for*
is not a scope — the boundary is what makes a review decision possible, because a
reviewer must know which questions they are *not* approving the pack to answer.

### Step 2 — Discover (automation)

```bash
python -m synapse.cli.build_corpus --config configs/corpus.example.toml
python -m synapse.cli.source_pack import \
  --pack source_packs/my-pack \
  --documents artifacts/corpus-v3/documents.jsonl.gz \
  --discovery-method pubmed_esearch
```

Every entry lands as `discovered`. Nothing is approved for anything.

### Step 3 — Review (clinician)

Copy `templates/clinician_review.template.json`, complete every field, then:

```bash
python -m synapse.cli.source_pack review \
  --pack source_packs/my-pack \
  --document-id pubmed:41802233 \
  --review reviews/pubmed-41802233.json
```

The command refuses an incomplete review file and names the missing fields. The
resulting state is derived from the decision — `approved` → `approved`,
`needs_changes` → `screened`, `rejected` → `rejected` — so a "needs changes"
review cannot be filed as an approval. A rejection without `--exclusion-reason`
is refused.

Reviewers read the pack's `README.md` **first**. A source can be excellent
evidence and still be wrong for this pack's population or purpose.

### Step 4 — Version (pack owner)

```bash
python -m synapse.cli.source_pack version \
  --pack source_packs/my-pack --level minor --note "Added 12 discovered sources."
```

Snapshots the current version into `.versions/<old>/` first, so `diff` has a real
predecessor without depending on version control.

### Step 5 — Compare (approver)

```bash
python -m synapse.cli.source_pack diff \
  --from source_packs/my-pack/.versions/0.1.0 --to source_packs/my-pack
```

Organised by governance consequence rather than by field, and answers the one
question directly: `requires_re_review`.

### Step 6 — Validate (CI)

```bash
python -m synapse.cli.source_pack validate --pack source_packs/my-pack --json
```

Exit code `0` when valid, `1` otherwise. Reports **all** findings, not the first
— an operator fixing a pack wants the whole list.

### Step 7 — Build a governed index

```bash
python -m synapse.cli.source_pack build-index \
  --pack source_packs/my-pack \
  --corpus artifacts/corpus-v3 \
  --out artifacts/index-my-pack
```

Refuses an unapproved pack, an example pack, and a pack failing validation.
Writes an `eligibility_report.json` giving a reason for **every** source, not
just a count, and stamps the pack version and digest into the index manifest for
the §5.5 agreement check.

---

## 7. The example pack

`source_packs/diabetes-previsit/` demonstrates the structure. It is
`approval_state = unapproved_example` — a first-class state, not a naming
convention, refused outright by the eligibility layer.

Its four sources are **synthetic placeholders**: `example:` identifiers,
`example.org` URLs, titles prefixed `[SYNTHETIC EXAMPLE — NOT A REAL
PUBLICATION]`, and no PMID or DOI. No real publication was fetched or labelled to
build it.

It contains **no review records and no reviewer identities**, because inventing
either would fabricate a clinical decision nobody made. Three sources are
`discovered`; one is `superseded`, which is demonstrable precisely because
supersession is a mechanical fact requiring no reviewer.

Tests in `tests/test_governance_workflows.py::TestShippedExamplePack` assert all
of this against the shipped artifact, so it cannot quietly acquire an approval.

---

## 8. What this repository does *not* contain

- **No approved source pack.** The only pack is an unapproved example.
- **No reviewer identities.** No `reviewer_id` appears in any shipped pack.
- **No review records.** Not one source has been reviewed by anyone.
- **No clinically labelled real sources.** Automated ingestion produced
  `discovered` entries; the example pack is synthetic.

The mechanism is built. The clinical decisions it is designed to record have not
been made, and this software will not make them.

### Unresolved, and not engineering's to decide

1. **Who reviews?** Nobody has been designated. Until someone is, no pack can
   leave `draft`. (`docs/quality-architecture.md` §8.1, U1)
2. **What rubric?** `rubric-1` is a placeholder identifier. The rubric's actual
   content — what "adequate evidence quality" means for patient-facing
   pre-visit education — is a clinical standard, not a config value.
3. **What review interval?** 365 days is a default, not a recommendation.
4. **How narrow must a scope be?** The example pack's scope is illustrative.

---

## 9. Related documents

- `docs/artifact-format.md` — the artifact format packs and indexes are written in.
- `docs/ingestion.md` — the discovery pipeline that produces `discovered` entries.
- `docs/quality-architecture.md` — the wider plan; §8.1 lists the clinical
  decisions this mechanism is waiting on.
- `templates/clinician_review.template.json` — the review form.

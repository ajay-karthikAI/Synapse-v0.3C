# Source Governance — Policy

**Last audited:** 2026-08-14 · **Applies to:** `synapse` 0.1.0

This is the **policy**: what the rules are, who is accountable, and why the rules
exist. For the implementation — directory layout, state table, enforcement
points, CLI workflows — see
[source-governance.md](source-governance.md).

> ## Current state: 0 approved sources
>
> | Lifecycle state | Sources in `source_packs/diabetes-previsit` |
> |---|---|
> | `discovered` | 3 |
> | `superseded` | 1 |
> | **`approved`** | **0** |
>
> No clinician has reviewed any source in this repository. Every mechanism
> described below is implemented and tested; **none has ever been exercised on a
> real review.** Do not read the existence of this policy as evidence that it
> has been followed.

---

## 1. The one rule

**Automated retrieval is not approval.**

A PubMed search returns whatever matches the query string. That a paper exists,
is indexed, and is topically similar says nothing about whether it is
appropriate to put in front of a patient. It may be underpowered, retracted,
superseded, about a different population, or simply wrong.

Every other rule here follows from that one, and the system is built so that the
distinction cannot be blurred by accident:

- A retrieved source enters as `discovered` and **can never reach `approved`
  without a human review record.** This is a structural bar, not a convention —
  `TransitionActor.AUTOMATION` is barred from producing an approval, so no
  script, backfill, migration, or LLM can create one.
- No report template or UI string may assert approval unless the code path
  guarantees a matching `lifecycle_state`. Enforced by test.

---

## 2. Lifecycle

```
                    ┌──────────────┐
   PubMed search ──►│  discovered  │  a machine found it. Nothing more.
                    └──────┬───────┘
                           │ automated screening (mechanical checks only)
                           ▼
                    ┌──────────────┐
                    │   screened   │  passed retraction/type/date checks
                    └──────┬───────┘
                           │ ◄── HUMAN REVIEW REQUIRED. No automation may cross this line.
              ┌────────────┴────────────┐
              ▼                         ▼
       ┌─────────────┐           ┌─────────────┐
       │  approved   │           │  rejected   │
       └──────┬──────┘           └─────────────┘
              │
    ┌─────────┴─────────┐
    ▼                   ▼
┌─────────┐      ┌──────────────┐
│ expired │      │  superseded  │
└─────────┘      └──────────────┘
 review aged out   newer evidence replaced it
```

Only `approved` sources may be served in a governed path. Everything else is
inventory.

---

## 3. Separation of duties

| Actor | May do | May never do |
|---|---|---|
| **Automation** | Discover, screen on mechanical criteria, mark expiry, detect retraction, compute digests | **Approve anything** |
| **Clinical reviewer** | Approve, reject, supersede, set scope | Alter the audit trail |
| **Engineering** | Change the machinery | Create or edit a review record |

A review record names a **pseudonymous** reviewer identifier (`rev_` + opaque
token). The mapping to a real person lives outside this repository, deliberately:
the repository needs to know *that* a qualified person reviewed something and
*which* one, not who they are.

**No clinician identity, credential, signature, or institution is invented
anywhere in this repository.** Where a name would appear, the field is empty.
An empty approver is treated as unapproved regardless of any status label — the
evidence is the record, never the label.

---

## 4. What a reviewer is accountable for

Approval is scoped. A reviewer approves a source **for a stated purpose**, not
in general. The record captures:

- the source, by stable identifier and content digest
- the pack and scope it is approved for
- the reviewer identifier and review date
- an expiry, after which the approval lapses automatically
- rationale, and any restriction on use

Because approval binds to a **content digest**, changing a source's content
invalidates its approval. A reviewer approved specific text; different text has
not been reviewed. This is enforced, not advisory.

---

## 5. Pack versioning

A source pack is versioned as a whole, and the manifest records the digest of
its contents. Adding, removing, or modifying any source produces a new version.

The rule that matters: **a pack's approval does not survive its contents
changing.** A clinician approved a specific set of sources for a specific scope;
a modified set is a different object and requires review of the delta.

---

## 6. Expiry

Medical evidence ages. Approvals carry an expiry date, and an expired approval
is not a weaker approval — it is **no approval**. Automation may mark expiry
(a mechanical fact about a date) but may not renew it (a judgement).

Retraction is handled at ingestion: retraction status is parsed and stored, and
a retracted source cannot be approved. **Known gap:** there is no active
re-check of already-ingested sources, so a paper retracted *after* ingestion
persists until the pack is rebuilt. See [LIMITATIONS.md](LIMITATIONS.md) §3.

---

## 7. What this policy does not do

- It does not make sources correct. It records who took responsibility.
- It does not assess reviewer qualification. See
  [clinical-labeling-protocol.md](clinical-labeling-protocol.md) — **who counts
  as qualified is still an open question**, and it is a governance decision, not
  an engineering one.
- It does not cover the legacy prototype, which retrieves from PubMed directly
  with no governance at all. That path is ungoverned by construction and is one
  of the reasons the prototype must not be used with patients.

---

## 8. Open decisions

These block the policy from being operable, and none is engineering's to make:

| Question | Owner |
|---|---|
| Who is qualified to approve a source? | Clinical leadership |
| How long is an approval valid before expiry? | Clinical leadership |
| How many reviewers must agree? | Clinical leadership |
| Who accepts liability for an approved source? | Legal / leadership |
| What happens to answers already given from a since-retracted source? | Product + clinical |

Until these are answered, `approved_by` stays empty and the governed path stays
empty with it.

---

## 9. Related

- [source-governance.md](source-governance.md) — implementation reference
- [clinical-labeling-protocol.md](clinical-labeling-protocol.md) — reviewer
  qualification and labelling procedure
- [ingestion.md](ingestion.md) — how sources are discovered
- [SAFETY_CASE.md](SAFETY_CASE.md) §3, Claim 4 — why this is a safety issue
- [LIMITATIONS.md](LIMITATIONS.md) §1.1, §3

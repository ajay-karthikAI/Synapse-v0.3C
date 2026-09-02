# Safety Case

**Last audited:** 2026-08-14 · **Applies to:** `synapse` 0.1.0
**Verdict: the safety case does not close.** Synapse must not be used with
patients.

---

## 0. What this document is

A safety case is a structured argument that a system is acceptably safe for a
defined use in a defined context, supported by evidence.

This one is written to be **falsifiable**. Each claim states what would have to
be true, what evidence exists, and — where the evidence is missing — says so
rather than arguing around it. Three of the six top-level claims are
unsupported, and one is contradicted by a known defect.

A safety case that concludes "safe" is usually a marketing document. This one
concludes "not yet, and here is precisely what is missing".

---

## 1. Scope of the argument

**System:** Synapse, a research prototype that retrieves PubMed abstracts and
drafts questions for a medical appointment.

**Claimed use:** helping an informed adult prepare questions to ask a clinician.

**Explicitly not claimed:** diagnosis, triage, treatment selection, dosing,
emergency assessment, or any use where a person might act on the output instead
of consulting a clinician. See [SYSTEM_CARD.md](SYSTEM_CARD.md) §2.

**Operating context assumed:** a single technical user, running locally, who
understands the system is a prototype. **No other context has been assessed.**

---

## 2. The hazards

Identified by engineering analysis, ordered by severity. **This list has not
been reviewed by a clinician, which is itself a limitation** — hazard
identification is exactly the activity that most needs clinical input.

| # | Hazard | Realistic harm | Severity |
|---|---|---|---|
| **H-1** | Emergency symptoms are not escalated | Delayed care in a time-critical event (MI, stroke, sepsis, PE) | **Catastrophic** |
| **H-2** | A confident answer is wrong or misleading | User acts on it, or is falsely reassured and does not seek care | **Severe** |
| **H-3** | Evidence is misrepresented — accurately quoted, wrongly framed | User believes a treatment is more established than it is | **Severe** |
| **H-4** | Out-of-date or retracted evidence is presented as current | User raises a superseded concern, or a withdrawn treatment | **Moderate** |
| **H-5** | Over-escalation trains users to ignore warnings | The emergency card stops being believed, re-creating H-1 | **Moderate** |
| **H-6** | Sensitive health text is disclosed | Privacy harm, potential discrimination | **Severe** |
| **H-7** | Retrieved content manipulates the system (prompt injection) | Attacker-controlled medical advice | **Severe** |

---

## 3. The claims, and whether they hold

### Claim 1 — Emergency symptoms are escalated ahead of retrieval (H-1)

**Argument.** Emergency detection runs *before* retrieval and generation, and
short-circuits to a fixed emergency card. It cannot be overridden by retrieved
content, because it never sees any.

**Evidence.** Ordering is enforced structurally and asserted by
`tests/test_answer_adversarial.py::TestEmergencyRoutingPreserved`. Six synthetic
emergency cases pass. `emergency_false_negative_count` is a blocking CI gate
with a tolerance of **zero**.

**Status: ⚠️ PARTIAL — mechanism sound, vocabulary unreviewed.**

*Upgraded from NOT SUPPORTED on 2026-08-20, when the detector was replaced.*

The detector that measured 2/6 was replaced by `synapse.safety`: stem-proximity
matching over a governed vocabulary, with negation scoping.

| | Old | New |
|---|---|---|
| Repository emergency cases | 2/6 | **6/6** |
| Repository negated cases | 2/6 | **6/6** |
| Held-out emergencies (15) | — | **15/15** |
| Held-out ordinary queries (9) | — | **0 false positives** |

The structural argument was always sound: detection runs before retrieval and
cannot be overridden by retrieved content. **The ordering was never the problem;
the detector was.** That is fixed, and `negation_accuracy` was promoted from an
informational to a blocking gate as its stated condition is now met.

**Why this is still only PARTIAL.** The vocabulary is engineering-authored and
carries `review_status = "unreviewed"`. Scoring well on 21 phrasings written by
the same person who wrote the matcher is evidence the *matcher* works; it is not
evidence the *concept list is clinically complete*. Nobody qualified has asked
the question that matters: **what escalation-worthy presentations are missing?**
Engineering cannot answer it, and no volume of self-authored test cases can.

One accepted false positive: "my father had a stroke" escalates. Suppressing
third-party mentions would also suppress "my son swallowed some of my pills".
Given the asymmetry — a missed emergency can kill, a false escalation is an
inconvenience — the trade is taken deliberately and asserted in a test.

### Claim 2 — Displayed claims are traceable to retrieved evidence (H-2)

**Argument.** Every claim carries a citation; citations are verified against a
verbatim excerpt from the retrieved chunk; unverifiable claims are withheld
rather than shown; the system abstains when nothing supports an answer.

**Evidence.** Adversarial suite covering fabricated citations, fabricated
quotes, and citation-to-chunk mismatches. Excerpt matching is exact after
documented NFC + whitespace + case normalisation. Snapshot tests cover the
rendered output. See [citation-integrity.md](citation-integrity.md).

**Status: ✅ SUPPORTED — for the narrow claim actually made.**

This claim is about **traceability**, not correctness. It is the strongest claim
in this document, and it is deliberately narrow.

---

### Claim 3 — Displayed claims are medically correct (H-2, H-3)

**Status: ❌ NOT SUPPORTED. No evidence exists.**

Support checking is **lexical**. It confirms that a quoted excerpt genuinely
appears in a cited source. It cannot detect a claim that quotes accurately while
misrepresenting the source's population, scope, effect size, or certainty —
which is the *characteristic* failure mode of literature summarisation, and
exactly **H-3**.

Closing this needs entailment checking (interface exists, no model attached) and
clinical review of real outputs (not started). No clinician has read a single
answer this system has produced.

---

### Claim 4 — Sources are appropriate and current (H-4)

**Status: ❌ NOT SUPPORTED. Zero approved sources exist.**

The governance machinery is real: a lifecycle state machine, an approval path
that automation is structurally barred from taking, pseudonymous reviewer
identifiers, and enforcement that unapproved sources cannot be served. It is
tested.

It has **never been used**. The one source pack contains 3 `discovered` and 1
`superseded` source, and **0 approved**. Retraction status is captured at
ingestion but never re-checked, so a paper retracted afterwards persists until
rebuild. Retrieval does not weight by recency or evidence level.

The machinery being sound is not evidence that the sources are sound.

---

### Claim 5 — Query text is handled safely (H-6)

**Argument.** Query text is never written to disk and never logged; the logging
prohibition is enforced by a static check, not a convention.

**Evidence.** `tests/test_no_pickle_and_imports.py::TestPatientTextIsNotLogged`,
plus a second test guarding the guard.

**Status: ⚠️ PARTIAL, and the gap is fundamental.**

The local handling is sound. But **query text is sent to OpenAI three times per
query** — for embedding, reranking, and generation. That is inherent to the
architecture. No BAA, no DPIA, no consent flow, no PHI detection. See
[PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md).

For a single technical user aware of this, it is an accepted risk. For anyone
else, it is unmitigated.

---

### Claim 6 — Retrieved content cannot hijack the system (H-7)

**Argument.** Retrieved chunks are data, not instructions; emergency routing
runs before retrieval; output is escaped; claims are verified against sources.

**Evidence.** Six synthetic adversarial-injection cases pass.

**Status: ⚠️ WEAK.**

Six cases, written by the same person who wrote the defence, are an
illustration, not an adversarial evaluation. No red-teaming by an independent
party has occurred. Absence of evidence of injection is not evidence of
resistance.

---

## 4. Where the argument stands

| Claim | Hazard | Status |
|---|---|---|
| 1. Emergency escalation precedes retrieval | H-1, H-5 | ⚠️ **Mechanism sound; vocabulary unreviewed** |
| 2. Claims are traceable to evidence | H-2 | ✅ Supported |
| 3. Claims are medically correct | H-2, H-3 | ❌ **No evidence** |
| 4. Sources are appropriate and current | H-4 | ❌ **No approved sources** |
| 5. Query text is handled safely | H-6 | ⚠️ Partial — leaves the machine |
| 6. Injection resistance | H-7 | ⚠️ Weak — 6 self-authored cases |

**The case still does not close.** Claims 3 and 4 have no supporting evidence at
all, and they are the two bearing directly on whether a patient is told
something true.

Claim 1 improved materially on 2026-08-20 — a detector missing stroke and
overdose was found, measured, and replaced — but it remains PARTIAL, because
what limits it now is not engineering. It is that no clinician has said which
presentations must escalate.

That is the shape of this whole document: the engineering is real and is the
*mechanism* by which a future safety case could be evidenced. Mechanism is not
evidence. Four of the ten conditions in §6 need a clinician, and none of them
can be closed by writing more code.

---

## 5. Defence in depth: what actually protects a user today

Honestly ranked, most to least effective:

1. **Nobody uses it.** The system is a local prototype with no deployment. This
   is doing more safety work than everything below it combined, and it is not a
   safety control.
2. **A permanent, non-dismissible disclaimer** on every answer.
3. **Emergency routing before retrieval** — structurally sound, clinically
   unvalidated.
4. **Abstention over speculation** — the system withholds rather than guesses.
5. **Citation traceability** — a user can check the source themselves.
6. **The framing itself** — the product drafts *questions for a doctor* rather
   than answers. A user is pointed at a clinician by design.

Point 6 is the most underrated: an appointment-preparation tool that produces
questions has a fundamentally lower harm ceiling than one that produces answers,
because the intended next step is a clinician. Preserving that framing is a
safety control and should be defended in product decisions.

---

## 6. Conditions for closing the case

Ordered; each depends on the previous.

1. **Clinical hazard review.** A qualified clinician reviews §2. Engineering
   cannot know what is missing from that list.
2. ~~Fix the negation defect; promote `negation_accuracy` to blocking.~~
   **Done 2026-08-20.**
3. **Clinical review of `config/emergency_vocabulary.toml` for completeness.**
   This is now the binding constraint on Claim 1.
4. **Label a real evaluation set** with adequate denominators per hazard.
5. **Approve real sources**, so Claim 4 has any evidence at all.
6. **Clinical review of real outputs** — the only route to Claim 3.
7. **Independent red-teaming** for Claim 6.
8. **Privacy and regulatory review** for Claim 5.
9. **Set thresholds** from measured performance, approved by named accountable
   people.
10. **Re-run this document.** A safety case is re-argued when the system
    changes, not written once.

Until step 1, this document is engineering's best guess at what could go wrong —
which is precisely why it must not be mistaken for a clinical assessment.

---

## 7. Related

- [LIMITATIONS.md](LIMITATIONS.md) · [VALIDATION.md](VALIDATION.md) ·
  [SYSTEM_CARD.md](SYSTEM_CARD.md) ·
  [PRIVACY_DATA_FLOW.md](PRIVACY_DATA_FLOW.md) ·
  [SOURCE_GOVERNANCE.md](SOURCE_GOVERNANCE.md) ·
  [clinical-labeling-protocol.md](clinical-labeling-protocol.md)

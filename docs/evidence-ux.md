# Evidence Experience

**Applies to:** `synapse.evidence` · `synapse.answer.render` · `app.py`
**Related:** docs/citation-integrity.md (what "supported" means) ·
docs/answer-rendering.md (how an answer reaches a screen) ·
docs/source-governance.md (how a source becomes approved)

> Every source in the shipped example pack is **synthetic and unreviewed**. The
> interface says so on every card, because that is the true state of the
> shipped data. No clinical approval state is set or changed by anything in this
> document or the code it describes.

---

## 1. What a patient sees

Every displayed claim carries a control that opens the evidence behind it:

```
HbA1c reflects average plasma glucose over 2-3 months. [1]
▸ Show the evidence for this statement                 [1]
    ┌───────────────────────────────────────────────┐
    │ [1] What HbA1c measures …                     │
    │ WHAT THIS SOURCE SAYS                         │
    │ ❝ HbA1c reflects average plasma glucose       │
    │   over 2-3 months ❞                           │
    │   example:diabetes-0001#0000                  │
    │ Authors        Example Working Group          │
    │ Published by   Example Standards Organisation │
    │ Publication date  1 March 2025                │
    │ ┌ Clinical guideline ─────────────────────┐   │
    │ │ Recommendations published by a medical  │   │
    │ │ organisation to help clinicians decide. │   │
    │ ├ Not reviewed by a clinician ────────────┤   │
    │ │ This source was found automatically. No │   │
    │ │ clinician has checked whether it is     │   │
    │ │ appropriate for this purpose.           │   │
    │ └─────────────────────────────────────────┘   │
    │ retrieval relevance: 86%                      │
    │   How closely this passage matched your       │
    │   question during the search. It does not     │
    │   say how reliable the research is.           │
    │ [View the source ↗]                           │
    │ From reviewed source list diabetes-previsit   │
    │ version 0.1.0.                                │
    └───────────────────────────────────────────────┘
```

The ordering is deliberate. **The excerpt comes first** because it is the thing
being claimed; provenance follows so a reader can weigh it; the governance labels
come last, each with its explanation attached rather than hidden in a tooltip.

Rendered snapshots of every state are in [docs/snapshots/](snapshots/) — open
them in a browser; they carry the production stylesheet and are pinned to 320px.

---

## 2. Where each field comes from

| Shown | Source | Absent when |
|---|---|---|
| Excerpt | The span the P1 verifier matched against the cited chunk | Never — a claim with no verified excerpt is not displayed |
| Chunk identifier | `SupportingExcerpt.chunk_id` | Never |
| Title, authors, journal / issuing organisation | `PackSource` in the governed pack | The pack has no record for the source |
| Publication and revision dates | `PackSource.publication_date` / `.revision_date` | Not recorded upstream |
| PMID / DOI / guideline ID | `PackSource.pmid` / `.doi` / `.guideline_id` | Not recorded upstream |
| Evidence type | `PackSource.evidence_type` | Falls back to "Type not recorded" |
| Review state | `PackSource.lifecycle_state` | Falls back to "No governance record" |
| Source-pack version | The pack manifest | The source is not in a pack |
| Retrieval relevance | The reranker | Reranking was skipped or degraded |

**Nothing on that list is generated, inferred, or filled in with a placeholder.**
`synapse.evidence.metadata` copies fields off a governed record and does nothing
else; the renderer omits a row it has no value for. An "Unknown" placeholder was
rejected deliberately — it reads as a fact about the source rather than a gap in
the record, and on a 320px screen it is a row of noise.

The excerpt is never regenerated (requirement 3). Asking a model to reproduce a
quotation is how a citation ends up almost right, which is worse than one that is
obviously wrong.

---

## 3. Identifier stability

One numbering, assigned once from retrieval order, drives the inline marker, the
control's summary, the evidence card and the exported brief. DOM ids are derived
from the same identifiers the verifier used:

```
claim-c1                              the claim paragraph
evidence-c1-pubmed-41802233           the card for that claim and source
data-claim-id="c1"                    on the paragraph, the control, the card
data-source-id="pubmed:41802233"      on the card
```

Punctuation is substituted for DOM ids (`:` and `#` break fragment links) but the
`data-` attributes carry the identifiers verbatim, so a test — or a support
conversation — can join a rendered card back to the audit record.

A claim citing a source that was **not retrieved** contributes no card. The
numbering has no entry for it, so it is skipped rather than rendered as an
unresolvable reference.

---

## 4. Labels, and what they may not say

Two label families, from two different places, kept apart on purpose.

**Evidence type** — what kind of document this is. Every value has a
plain-language name and a one-sentence explanation
(`synapse.evidence.labels.EVIDENCE_TYPE_LABELS`). The descriptions deliberately
avoid a hierarchy: presenting a ranked ladder invites a patient to weigh two
sources against each other, which is a clinical judgement depending on the
question asked.

**Review state** — what a clinician has and has not done. The shipped pack is
entirely `discovered`, so the interface says **"Not reviewed by a clinician —
this source was found automatically."** Not "review pending", which implies a
scheduled process; not silence, which implies approval.

**Retrieval relevance is neither** (requirements 7–9). It is the reranker's
rating of how well a passage matched *this question*. It is:

- labelled "retrieval relevance", **never "confidence"**;
- rendered in its own block, away from the governance labels, so it cannot be
  read as a quality grade;
- accompanied by "It does not say how reliable the research is, and it is not a
  medical judgement";
- omitted entirely when reranking did not run.

Quality and review labels come from governed metadata and from nowhere else
(requirement 10). No code path lets a model score contribute to one.

---

## 5. Insufficient evidence

A designed state, not a gap. It renders when any of six things happens:

| Reason | Trigger |
|---|---|
| `no_eligible_evidence` | Retrieval returned nothing that passed eligibility |
| `failed_validation` | Evidence was retrieved; no citation verified |
| `below_threshold` | Support fell below the P1 display policy |
| `conflicting_sources` | Verified claims contradict; no safe summary |
| `out_of_scope` | The question is outside the pack's declared scope |
| `index_unverified` | The index could not be validated (fail closed) |

To a patient these differ only in one sentence. The rest is identical:

1. **It says plainly that Synapse does not have enough verified information.**
2. **It does not fill the gap.** `synapse.evidence.insufficient` composes the
   copy from module constants; there is no parameter through which generated
   text can enter. That is what makes "no model knowledge here" a property of
   the code rather than a habit.
3. **It never implies absence of evidence is absence of disease.** Every
   rendering carries: *"This is about what is in Synapse's sources, not about you
   or your health. It does not mean there is nothing to discuss, and it does not
   rule anything in or out."*
4. **It suggests neutral next steps** — how to raise the topic, never what to do
   medically.
5. **The permanent disclaimer is still shown.**
6. **It is not styled as an error.** No red, no warning icon, no "failed". A
   small research index not covering a question is an ordinary limitation;
   styling it as a malfunction invites a reader to conclude their question was
   itself alarming. Tone is asserted in the tests — the copy is checked against a
   banned-word list.

---

## 6. Freshness, and what only a reviewer sees

**Patients see** publication and revision dates, and the source-pack version that
produced the answer.

**Patients never see** expired, superseded or rejected sources — P1 eligibility
excludes them before retrieval, so they are not in the candidate set at all.
Their status is rendered only by `render_review_evidence_card`, which is the
review view and shows lifecycle state, `superseded_by`, retraction status, review
dates and the pack's approval state.

Rendering **reads**; it never writes. Approval state is a governance decision
recorded through the source-pack CLI by a person with a reviewer identity. A test
asserts the pack file on disk is byte-identical after rendering every card in it.

---

## 7. Accessibility

The control is a native `<details>`/`<summary>`. Streamlit sanitises scripts out
of rendered markdown, and that constraint turned out to be the right design:

| Requirement | How |
|---|---|
| Keyboard operable | Native disclosure: focusable, Enter/Space activates |
| Expanded state exposed | Carried by the element; no `aria-expanded` to forget to update |
| No hover-only interaction | Hover restyles; it never reveals. Asserted over the CSS |
| Clear focus state | `:focus-visible` with a 3px outline and 2px offset on controls and links |
| Screen-reader friendly | Descriptive control name plus a visually-hidden source count; decorative marks `aria-hidden` |
| Works at 320px | Single-column by default; the two-column metadata layout is a `min-width: 420px` enhancement |
| Long content wraps | `overflow-wrap: anywhere` on cards; titles and excerpts wrap, never truncate |
| Touch targets | `min-height: 44px` on the control and every link |
| Live state changes | Insufficient-evidence uses `role="status"` — announced without stealing focus |
| Reduced motion | `prefers-reduced-motion` disables every animation on the page |

Label explanations are rendered as **text, not `title` tooltips**: a tooltip is
hover-only, invisible on touch, and inconsistently announced. A test asserts no
`title=` attribute appears in a card.

Writing these snapshots caught a real defect: the visually-hidden summary text
repeated the source numbers that the visible marker already carried, so a screen
reader announced "[1]" twice for a single-source claim. The hidden text now
carries the count and the marker carries the numbers.

---

## 8. External links

| Rule | Behaviour |
|---|---|
| Scheme | **`https` only.** `http` is rejected too — a citation is the one place a reader is asked to trust a destination |
| Rejected URL | **Dropped**, never escaped and rendered. Escaping `javascript:` yields a working link |
| Tab safety | `target="_blank"` with `rel="noopener noreferrer nofollow"` on every external link |
| Announcement | Every link carries visually-hidden "(opens in a new tab)" |
| DOI | Validated against the `10.<registrant>/<suffix>` grammar, then percent-encoded |
| PMID | Digits only, 1–8 |
| Preference | DOI, then PMID, then the pack's canonical URL — most durable first |

A malformed identifier produces **no link**, but is still displayed as recorded
metadata: the record says what it says, and hiding it would misrepresent the
source pack.

---

## 9. Tests

| File | Covers |
|---|---|
| `tests/test_evidence_ux.py` | Claim→evidence mapping, exact excerpts, no invented metadata, label wording, link safety and identifier validation, hostile content, all six insufficient states, review-view separation, accessibility properties |
| `tests/test_evidence_snapshots.py` | One committed HTML snapshot per major state at 320px, plus assertions that the snapshots carry the real stylesheet |

**On snapshots:** the HTML files are the source of truth. They are generated by
the same functions the application calls, carry the production stylesheet, and
diff in review like any other snapshot. Regenerate with
`SYNAPSE_UPDATE_SNAPSHOTS=1 pytest`, then **open the files and look** — a
snapshot regenerated without being read is worse than no snapshot.

PNG renders live in `docs/snapshots/png/` for README and presentation use. They
are generated *from* the HTML, so they cannot drift from the code independently:

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
for f in docs/snapshots/state-*.html; do
  "$CHROME" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=360,1600 \
    --screenshot="docs/snapshots/png/$(basename "$f" .html).png" "file://$PWD/$f"
done
```

The images are a convenience, not a test artifact. Nothing asserts against them,
because a pixel comparison across platforms and font-rendering stacks fails for
reasons that have nothing to do with the interface being wrong.

---

## 10. Reading the labels — for patients

*This section is the user-facing explanation of the labels, kept beside the
implementation so the two cannot drift apart.*

**What kind of source is this?**

- **Clinical guideline** — recommendations published by a medical organisation to
  help clinicians make decisions.
- **Systematic review** — a structured summary of the studies published on a
  question, gathered using a documented search.
- **Meta-analysis** — a study that combines the results of several earlier
  studies statistically.
- **Randomised trial** — a study where participants were assigned to treatments
  by chance, to compare the results.
- **Cohort study** — a study that follows a group of people over time.
- **Case-control study** — a study comparing people who have a condition with
  people who do not.
- **Case report** — a description of one or a few patients. It describes what
  happened to them, not what happens generally.
- **Review article** — an expert summary that does not follow a documented search
  method.
- **Type not recorded** — the kind of study is not recorded for this source.

**Has a clinician checked it?**

- **Reviewed by a clinician** — a qualified reviewer approved this source for the
  topics this list covers, and recorded a date to review it again.
- **Checked for relevance only** — someone confirmed it is on-topic, but it has
  not been approved for use with patients.
- **Not reviewed by a clinician** — it was found automatically, and nobody has
  checked whether it is appropriate. *This is the state of every source in the
  shipped example pack.*
- **No governance record** — the source is not on the reviewed list at all, so
  there is no review information for it.

**What does "retrieval relevance" mean?** How closely a passage matched your
question during the search. It is produced by the search system, not by a
clinician. It says nothing about how reliable the research is, and it is not a
medical judgement.

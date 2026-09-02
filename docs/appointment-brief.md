# Appointment Brief

**Applies to:** `synapse.brief` · `app.py`
**Related:** docs/evidence-ux.md (how evidence is shown on screen) ·
docs/citation-integrity.md (what "supported" means) ·
docs/privacy-logging-policy.md (what is never recorded)

> The brief is the only artifact that leaves the process. It is printed, saved,
> mailed to a patient's own address, and handed to a clinician who never saw the
> screen it came from. Everything below follows from that.

---

## 1. What it is

A one-page sheet a patient takes into an appointment, containing their own
topic and notes, the evidence-grounded summary, supported claims with citation
markers, questions to ask, the sources behind those claims, limitations, the
permanent disclaimer, and a provenance line naming the versions that produced it.

It is built from validated typed data
(`synapse.brief.schema.AppointmentBrief`) and rendered from that. **No renderer
output is ever parsed back into a document.** A scraper picks up whatever the
page happens to contain, and it loses the distinction between a claim a verifier
checked and a sentence a user typed, which is the distinction the whole artifact
turns on.

Rendered examples: [docs/snapshots/brief/](snapshots/brief/).

---

## 2. Invariants the schema enforces

These are type errors, not conventions. A caller cannot opt out.

| Invariant | Enforced by |
|---|---|
| An unsupported claim cannot be in a brief | `BriefClaim` rejects the status; `build_brief` drops it first |
| Every citation marker resolves to a listed source | `AppointmentBrief` cross-field validator |
| An edited claim carries no citation markers | `BriefClaim` validator |
| An edited claim retains the wording it replaced | `BriefClaim` validator |
| A user-authored sentence cannot be a claim | `BriefClaim` validator (it belongs in notes) |
| Claim and question identifiers are unique | `AppointmentBrief` validator |
| The document identifier is random | `BriefClaim` field validator: 12 hex characters |
| The disclaimer cannot be omitted | It is not a member of `BriefSection` |

There is deliberately **no field** for an API key, a telemetry identifier, a
session identifier, provider metadata, a prompt, or conversation history. Those
are not filtered on export; there is nowhere to put them, which is stronger than
a filter someone has to remember to run.

---

## 3. Editing

| The user may | The user may not |
|---|---|
| Change their topic and notes | Make a claim say something and keep its citation |
| Add, remove and reorder questions | Turn their own sentence into a cited claim |
| Reword a claim | Remove the disclaimer |
| Omit whole sections | Alter what a source says |

**Editing a claim is one atomic operation** (`edit_claim_text`): it replaces the
text, sets the origin to `user_edited`, and strips the citation markers. Any one
of those without the others produces a document that misrepresents itself, so
the schema refuses the partial states and no code path constructs one.

Nothing re-verifies. Restoring verified status means running the claim back
through `synapse.answer.verify` against the evidence, which is a different
operation in a different layer. `restore_claim` puts back the original wording
and its markers; it does not bless new wording.

**Reordering questions preserves citation provenance** because questions cite
nothing, and because a marker refers to a numbered source rather than to a
position in a list. Reordering is also all-or-nothing: a partial list would
silently delete the questions left out, which is a destructive operation
disguised as a cosmetic one.

### How user content is distinguished

Not by colour alone. A tinted panel with a left rule **and** a text label, in
both renderings:

```
WRITTEN BY YOU
My blood sugar readings have been higher in the mornings.

EDITED BY YOU (NOT CHECKED AGAINST A SOURCE)
My HbA1c is a three month average of my blood sugar
Original wording: HbA1c reflects average plasma glucose over 2-3 months. [1]
```

The label is what survives a black-and-white photocopy, which is how a lot of
paper actually reaches a clinician. Note where the `[1]` sits: on the original
wording, which the source supports, not on the user's rewrite.

*That detail was a defect first.* Editing stripped the markers and left source
[1] in the list with nothing pointing at it. Rendering the brief and reading it
caught it. `tests/test_appointment_brief.py::test_no_source_is_orphaned_after_any_edit`
now covers it.

---

## 4. Saving, and what "share" means

**There is no server-side persistence, and none existed before this work.** The
codebase was inspected for it: no database, no object store, no HTTP POST, no
cache. So there is no authentication, encryption, retention or access-control
model to audit, because there is nothing storing anything. The default of no
persistence stands.

Export produces bytes the browser downloads:

| Format | Purpose |
|---|---|
| **HTML** | The printable document. Print it, or save as PDF from the print dialog. |
| **Text** | Pastes into a portal message or an SMS. |
| **JSON** | Structured, round-trips exactly, for moving the brief elsewhere. |

**No PDF library ships with this.** Every browser and phone already has a
competent PDF writer driven by the print stylesheet; adding a rendering engine
to reproduce what `@page` already specifies would add a large dependency to
produce a worse result.

**"Share" means the user has a file.** There is no share URL, no upload, no
email, no third-party delivery, and no integration point for one. That is a
deliberate ceiling: an unauthenticated share link to a health document is a
public health record with a hard-to-guess name, and adding "just a link" is how
that ships.

Export requires an explicit click, and the warning appears above the buttons:

> This file will contain health information you entered and the research it is
> based on. It is saved to your own device and is not sent anywhere. Anyone who
> opens the file, or the folder it is in, can read it.

### Temporary files

The Streamlit path creates none: `build_export` returns strings that go straight
to the download button. `temporary_export` exists for callers that need a path,
writes to a private directory, and removes it in a `finally` — so it goes away
when the caller raises as well as when it returns. A health document left in
`/tmp` after a traceback is exactly the residue that guards against.

### The export scan

`assert_no_secrets` runs over every rendered artefact before it is returned,
checking for keys, bearer tokens, authorization headers, system prompts,
telemetry identifiers, provider model names and chunk identifiers. The schema
already makes most of those unrepresentable, so this is a second line; it exists
because "unrepresentable" is a claim about today's model and an export is where
being wrong is irreversible. The error names the category only, never the match:
an error message quoting the secret it found is how the secret reaches a log.

*That scanner had a false positive on its first run:* the chunk-identifier
pattern `#\d{4,8}` matched CSS hex colours like `#111111` and blocked every
export. It is now anchored on the real grammar, `<scheme>:<key>#<ordinal>`.

---

## 5. One page, or an explicit choice

The layout targets a single A4 or Letter page. When content does not fit, the
renderer **does not shrink the type**. Below about 9pt a printed brief stops
being readable for exactly the population most likely to need it, so instead:

- `estimate_fit` returns a measurement (`fits`, `overflow_lines`, `fill_ratio`);
- `overflow_advice` returns what could be cut, largest saving first;
- the interface shows both and asks the user to choose.

The estimate is a deterministic approximation, and named as one. Real pagination
depends on the print engine, the paper size and the user's browser settings. The
QA checklist in §7 covers what the model cannot.

Other print behaviours:

- sources, sections and the disclaimer carry `break-inside: avoid`, so none is
  split across a page boundary;
- long questions and titles wrap (`overflow-wrap: anywhere`); nothing truncates
  with an ellipsis, because a half-printed question is worse than a wrapped one;
- links print their URL after the text, since a printed hyperlink is otherwise a
  dead end;
- the document is light-on-white, not the app's dark theme, which would waste an
  extraordinary amount of toner and read badly on paper.

---

## 6. No remote assets

An exported brief reaches the network **never**. No font import, no image, no
script, no stylesheet link. It is one HTML file with one inline stylesheet using
a system font stack.

This is a privacy property, not a performance one: a remote asset in a saved
health document phones a third party every time the document is opened,
disclosing when and roughly where it was read. Tests assert the absence of each
token, and that every `href` in an export is `https://`.

---

## 7. Manual print and PDF QA checklist

Automated tests cover structure and content. Pagination is a rendering-engine
behaviour, so it is checked by hand before a release that touches
`render_print.py`. Open the files in [docs/snapshots/brief/](snapshots/brief/).

**Setup**

- [ ] Open `brief-full-a4.html` in Chrome, Safari and Firefox.
- [ ] Print preview at A4, margins default, scale 100%, background graphics **on**.
- [ ] Repeat with `brief-full-letter.html` at Letter.

**Page integrity**

- [ ] The whole brief fits one page at 100% scale.
- [ ] The disclaimer is on the page, not orphaned onto a second.
- [ ] No source entry is split across a page break.
- [ ] No section heading sits alone at the foot of the page.
- [ ] The document identifier is visible in the footer.

**Legibility**

- [ ] Body text is at least 9pt as printed.
- [ ] Every source title is fully readable, not clipped.
- [ ] The longest question wraps rather than truncating.
- [ ] Print in greyscale: user-authored blocks are still identifiable from their
      text labels, not only the tint.

**Content**

- [ ] `brief-user-edited.html`: the edited claim is labelled, carries no marker,
      and shows the original wording with its marker.
- [ ] Every `[n]` in the body has a matching entry in Sources.
- [ ] No chunk identifier, model name, or internal identifier appears anywhere.
- [ ] `brief-questions-only.html`: the disclaimer is still present.
- [ ] `brief-no-evidence.html`: no research section, and nothing implies
      evidence the brief does not have.

**Overflow**

- [ ] `brief-overflow.html` genuinely runs past one page.
- [ ] In the app, that state shows the fill percentage and the trim suggestions
      rather than shrinking the text.

**Save as PDF**

- [ ] Save from the print dialog; reopen the PDF.
- [ ] Text is selectable, not rasterised.
- [ ] Links are clickable, or their URLs are printed beside them.
- [ ] Open with the machine offline: the document renders identically.

To regenerate the PNG renders used for review:

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
for f in docs/snapshots/brief/brief-*.html; do
  "$CHROME" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=794,1123 \
    --screenshot="docs/snapshots/brief/png/$(basename "$f" .html).png" "file://$PWD/$f"
done
```

---

## 8. Tests

| File | Covers |
|---|---|
| `tests/test_appointment_brief.py` | Construction and determinism, unsupported claims blocked by four routes, every editing rule, user content distinguishable in both renderings, section omission, export safety, hostile input, temporary-file cleanup, one-page behaviour, empty and insufficient states |
| `tests/test_brief_print_snapshots.py` | A committed document per state at both page sizes, plus assertions that every one keeps the disclaimer, reaches no network, and carries no secret |

Regenerate snapshots with `SYNAPSE_UPDATE_SNAPSHOTS=1 pytest`, then open them.
Snapshots are pinned to a fixed timestamp and document identifier so they are
byte-stable; a real export stamps the current time and a fresh random identifier.

---

## 9. Migration note

`synapse.answer.brief` is now a **deprecated shim** (removal 2026-11-30). It
rendered a brief directly from an answer object; the brief has since become a
document with invariants of its own, so it is built as typed data and rendered
from that. The shim keeps the old call signature and delegates, which means one
text formatter rather than two that drift.

Two format changes came with the move, both deliberate:

- **No "Status:" line for an ordinary answer.** It said "Answer based on
  published research" beside a document titled "Appointment brief" whose sections
  are "What the research says" and "Details from published research". It remains
  for the routing states, where it says something the rest of the page does not.
- **No supporting-excerpts block with claim and chunk identifiers.** Those are
  internal plumbing, and a sheet handed to a clinician is the wrong place for
  them. They remain on the typed object for support and audit; the printed
  provenance is the source number.

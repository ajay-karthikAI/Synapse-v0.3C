# Accessibility

**Applies to:** `app.py` · `synapse.a11y` · `synapse.answer.render` ·
`synapse.evidence.render` · `synapse.brief.render_print`
**Target:** WCAG 2.2 Level AA
**Status:** substantial progress, **not a conformance claim.** See §8.

> **No formal conformance is claimed.** Automated checks passed, and automated
> checks detect a minority of accessibility barriers. Nothing in this document
> has been verified with a screen reader by the author, because none was
> available in the environment this work was done in. §7 is a procedure for a
> human to run, not a record of results.

---

## 1. The audit

Eleven states were audited before anything was changed.

| State | Findings |
|---|---|
| Home | Decorative logo and streaks not hidden from assistive technology; no skip link; no h1 landmark structure |
| Question entry | **Label collapsed, placeholder used as the label**; button label carried a decorative arrow |
| Loading | No live region; progress text announced inconsistently or not at all |
| Answer | Section labels were styled `div`s, not headings; no completion announcement |
| Evidence expansion | Correct already (native `<details>`); heading level skipped from h1 to h4 |
| Insufficient evidence | Heading skipped h1 to h3; otherwise sound (`role="status"` already present) |
| Medical staff | No live region |
| Emergency | No `role="alert"`; a patient using a screen reader had no interruption |
| Appointment brief editor | Streamlit widget labels hidden by a global CSS rule |
| Feedback controls | None exist yet; the telemetry schema reserves a closed category |
| Mobile | Eight invisible focus stops inside the collapsed sidebar |

Two findings run across every state:

**Contrast.** Eight pairings failed AA, all low-contrast greys and borders:

```
src-sub          #4a4a6a on #0a0a18   2.32:1  (needed 4.5)
disclaimer       #5a5a7a on #05030d   3.10:1  (needed 4.5)
perm disclaimer  #6a6a8a on #080814   3.83:1  (needed 4.5)
empty hint       #68688a on #05030d   3.84:1  (needed 4.5)
chunk id         #4a4a6a on #0b0b1a   2.30:1  (needed 4.5)
card border      #1c1c30 on #0b0b1a   1.17:1  (needed 3.0)
summary border   #241a4d on #0a0a18   1.24:1  (needed 3.0)
button border    #6d28d9 on #05030d   2.88:1  (needed 3.0)
```

**Audio.** A Web Audio heartbeat was scheduled on every session load. The audit
established **it never actually played**: Streamlit renders markdown through
React's `dangerouslySetInnerHTML`, and a `<script>` inserted through `innerHTML`
does not execute. That was verified in a real browser rather than assumed, and
the verification is now a test (`TestAudioNeverPlays`).

It was deleted anyway. The intent to autoplay audio was in the source, a custom
component would have made it work, and unsolicited sound is a 1.4.2 failure that
also startles anyone using a screen reader. **There is now no audio in Synapse at
all**, so there is nothing to mute and no opt-in to build.

---

## 2. What changed

### Colour and contrast

The palette is now declared once in `synapse/a11y/palette.py`, as tokens that
each carry the surface they sit on and the threshold they must clear.
`css_variables()` generates the `:root` block the app uses, so the stylesheet and
the test read the same numbers. All 23 tokens pass; the test fails the build if
any stops passing.

The first replacement value for the card border, `#3a3a5c`, still measured
1.80:1. That is precisely why the table is checked rather than eyeballed.

### Motion

- `prefers-reduced-motion: reduce` collapses every animation and transition.
- An in-app **Reduce motion** setting in the sidebar, for people who cannot
  change an OS-level preference on a shared or clinic machine.
- The decorative edge streaks moved from inline `style` attributes to a class.
  An inline animation cannot be overridden by a media query, so as inline styles
  they were unreachable by the reduced-motion rule.

### Structure and semantics

- Section labels are real `h2` elements; evidence cards are `h3`; heading order
  never skips.
- A skip link, visible on focus, targeting the question form.
- Decorative logo, streaks and arrows are `aria-hidden`.
- No positive `tabindex` anywhere.

### Forms

- The question field has a **persistent visible label**, "Your question or
  symptoms". The placeholder is now an example, not the label.
- The global rule `.stTextArea label { display: none !important; }` is gone. It
  hid every Streamlit form label from sight and from the accessibility tree.
- 44px minimum on controls; a 3px `:focus-visible` outline at 11:1 contrast.

### Live regions

| Event | Announcement |
|---|---|
| Loading | `role="status" aria-live="polite"` |
| Answer complete | `role="status"` on the answer container |
| Failure | polite status with plain-language text |
| Emergency | **`role="alert"`** — the only interruption in the interface |

Everything else is polite by design. An interface that interrupts on every state
change is exhausting, and the one message that genuinely warrants interrupting
loses its meaning if it is not the only one.

### Streamlit chrome (requirement 24)

`#MainMenu`, `header` and the toolbar used to be `display: none`, which removes
them from the accessibility tree. That menu holds Settings and Print; the header
begins the focus sequence. They are restored. Only the promotional "Made with
Streamlit" footer stays hidden: it is not a control.

### The mobile finding

A collapsed sidebar is moved off-screen with a `transform` but keeps
`visibility: visible`, so its API key field, its checkbox and its buttons stayed
in the tab order at `x = -305`. On a phone that is eight focus stops on controls
the user cannot see: a 2.4.3 and 2.4.7 failure. A `visibility` rule keyed on the
`aria-expanded` attribute Streamlit already sets removes them, and restores them
when the sidebar opens.

---

## 3. Automated testing

Two suites, split by what they can honestly cover.

### Offline (`tests/test_accessibility.py`, 72 checks)

Contrast arithmetic and markup parsing. No browser, no network, milliseconds, so
it gates every pull request. Covers contrast, heading order, accessible names,
icon handling, live-region politeness, tab order, colour-not-alone, audio
absence, motion handling, labels, chrome semantics, and reflow declarations.

### Browser (`tests/test_accessibility_browser.py`, 58 checks)

Playwright driving the **installed Chrome channel** rather than a downloaded
browser, with axe-core bundled inside `axe-core-python` so no CDN is contacted.
Covers axe over all 21 committed state snapshots, 320/375/768px viewports,
horizontal-scroll measurement, 200% zoom reflow, keyboard operation of the
evidence disclosure, keyboard-trap detection, computed focus visibility, and
computed animation durations under both motion preferences.

Four `slow`-marked tests run the real Streamlit server on a free port. They need
no API key because they exercise only the empty state.

```bash
pytest tests/test_accessibility.py -q              # offline, always runs
pytest tests/test_accessibility_browser.py -q      # needs Chrome, skips cleanly
pytest tests/test_accessibility_browser.py -m slow # starts a real server
```

Both browser suites skip rather than fail when Playwright or Chrome is absent,
so an environment without them still gets a green run and the offline gate.

### A note on axe findings we do not fail on

`partition_violations` separates findings inside Streamlit's own DOM from
findings in Synapse's markup. Framework findings are printed, not asserted:
they cannot be fixed from `app.py`, and failing the build on them would mean
either patching a vendor's DOM or switching the check off. One is currently
outstanding, listed in §8.

---

## 4. Conformance checklist

Per success criterion, at Level AA. "Automated" means a test fails the build.

| SC | Criterion | Status | Evidence |
|---|---|---|---|
| 1.1.1 | Non-text content | Pass | Decorative elements `aria-hidden`; automated |
| 1.3.1 | Info and relationships | Pass | Real headings, `dl` metadata, labels; automated |
| 1.3.2 | Meaningful sequence | Pass | DOM order matches reading order; no positive tabindex |
| 1.3.4 | Orientation | Pass | No orientation lock |
| 1.3.5 | Identify input purpose | Partial | Fields are labelled; no `autocomplete` on the question field, which is not a personal-data field |
| 1.4.1 | Use of colour | Pass | Every state signal carried in text; automated |
| 1.4.3 | Contrast (minimum) | Pass | 23/23 tokens; automated, plus axe in-browser |
| 1.4.4 | Resize text | Pass | Reflow at 640px viewport (200% of 1280); automated |
| 1.4.10 | Reflow | Pass | No horizontal scroll at 320px across 21 snapshots; automated |
| 1.4.11 | Non-text contrast | Pass | Borders and focus ring ≥3:1; automated |
| 1.4.12 | Text spacing | Not verified | Needs manual check; §7 |
| 1.4.13 | Content on hover or focus | Pass | No hover-only content; automated over the CSS |
| 2.1.1 | Keyboard | Pass | Disclosure operable by keyboard; automated |
| 2.1.2 | No keyboard trap | Pass | Automated |
| 2.1.4 | Character key shortcuts | Pass | None exist |
| 2.2.1 | Timing adjustable | Pass | No time limits |
| 2.2.2 | Pause, stop, hide | Pass | Decorative animation removable by OS preference or in-app setting |
| 2.3.1 | Three flashes | Pass | Nothing flashes |
| 2.3.3 | Animation from interactions | Pass | Automated, both preference states |
| 2.4.1 | Bypass blocks | Partial | Skip link exists; cannot be the first stop. §8 |
| 2.4.2 | Page titled | Pass | Set in `st.set_page_config`; brief exports carry their own |
| 2.4.3 | Focus order | Partial | Sidebar fixed; Streamlit chrome still precedes content. §8 |
| 2.4.4 | Link purpose | Pass | Links named; "opens in a new tab" announced |
| 2.4.6 | Headings and labels | Pass | Automated |
| 2.4.7 | Focus visible | Pass | 3px outline, computed check in-browser |
| 2.4.11 | Focus not obscured (min) | Not verified | Needs manual check at small viewports; §7 |
| 2.5.1 | Pointer gestures | Pass | No path-based gestures |
| 2.5.2 | Pointer cancellation | Pass | No down-event actions |
| 2.5.3 | Label in name | Pass | Visible labels match accessible names |
| 2.5.4 | Motion actuation | Pass | None |
| 2.5.7 | Dragging movements | Partial | Streamlit's `multiselect` reorder has no documented keyboard equivalent. §8 |
| 2.5.8 | Target size (minimum) | Pass | 44px minimum; automated |
| 3.1.1 | Language of page | Pass | Brief exports set `lang`; the app relies on Streamlit's `<html lang>` |
| 3.2.1 | On focus | Pass | No context change on focus |
| 3.2.2 | On input | Pass | Submission is an explicit button |
| 3.3.1 | Error identification | Pass | Typed codes with plain-language text |
| 3.3.2 | Labels or instructions | Pass | Persistent labels plus help text; automated |
| 3.3.7 | Redundant entry | Pass | Nothing is asked twice |
| 4.1.2 | Name, role, value | Pass | Automated, plus axe |
| 4.1.3 | Status messages | Pass | Polite regions; `alert` reserved for emergency |

---

## 5. What automated testing cannot tell you

Every source in this field puts automated coverage at roughly a third of
barriers, and that matches what these suites do. They cannot tell you:

- whether an announcement is **comprehensible**, only that a region exists;
- whether the reading order **makes sense**, only that it matches the DOM;
- whether a label is **helpful**, only that it is present;
- whether a patient in distress can **actually use** the emergency card;
- anything about cognitive load, which for a medical tool may matter most.

A green run means no known machine-detectable barrier. It is a floor.

---

## 6. Known limitations

| # | Limitation | Cause | Priority |
|---|---|---|---|
| 1 | **Focus is not restored after submission or answer generation** | Streamlit reruns the script and replaces the DOM; it strips `<script>` from markdown, so no client-side focus management is possible without a custom component | **High** |
| 2 | Skip link cannot be the first tab stop | Streamlit renders header, toolbar and sidebar before any application content, with no supported way to insert markup ahead of them | Medium |
| 3 | One outstanding axe finding in Streamlit's sidebar (`aria-allowed-attr`) | Streamlit's own DOM | Medium |
| 4 | Live regions may not announce on a full rerun | A region must exist before its content changes; a rerun replaces the whole DOM | **High** |
| 5 | Question reordering in the brief uses `st.multiselect`, whose drag interaction has no documented keyboard equivalent | Streamlit widget | Medium |
| 6 | 1.4.12 text spacing and 2.4.11 focus-not-obscured unverified | Need a human with a browser | Medium |
| 7 | No screen reader testing has been performed | None available in this environment | **High** |

### Remediation priority

**1. Screen reader testing (limitation 7).** Nothing here substitutes for it, and
limitations 1 and 4 cannot be assessed for real impact until it happens. Run §7
on VoiceOver first; it is the likeliest reader for an iPhone in a waiting room.

**2. Focus restoration and live-region reliability (1 and 4).** Both have the
same cause and the same fix: a small Streamlit custom component that manages
focus and owns a persistent live region across reruns. That is a contained piece
of work and it closes the two highest-impact gaps. It was out of scope here
because a custom component is a build-step and a new dependency surface, not a
CSS change.

**3. Keyboard reordering (5).** Replace the multiselect with explicit
"move up / move down" buttons. Straightforward, and buttons are keyboard
operable by construction.

**4. Streamlit chrome (2 and 3).** Report the `aria-allowed-attr` finding
upstream. The tab-order limitation is inherent to the framework; the honest
mitigation is keeping the sidebar's control count small.

---

## 7. Manual testing procedure

**These are instructions, not results.** Nothing below has been executed by the
author. Record outcomes with date, version and assistive-technology version.

### VoiceOver (macOS)

Cmd+F5. Safari and Chrome.

- [ ] VO+U, Headings: the outline reads Synapse → section headings → evidence
      cards, with no skipped level.
- [ ] Tab to the skip link; it is announced and becomes visible.
- [ ] Activate it; focus lands on or near the question field.
- [ ] Reach the question field: the label "Your question or symptoms" is
      announced, not the placeholder.
- [ ] Submit. Is the loading state announced? Is the answer announced when it
      arrives? **Record what actually happens** — limitation 4 predicts this may
      fail.
- [ ] After submission, where is focus? **Record it** (limitation 1).
- [ ] Open an evidence disclosure with Enter: expanded state announced.
- [ ] Inside a card: excerpt, source, "Not reviewed by a clinician", and the
      relevance caveat are all announced.
- [ ] The `↗` on links is not announced; "opens in a new tab" is.
- [ ] Trigger the emergency state: it interrupts.
- [ ] Trigger the insufficient state: announced politely, not as an error.

### VoiceOver (iOS)

- [ ] Rotor by heading works.
- [ ] Evidence disclosure operable by double-tap.
- [ ] Nothing requires a drag.
- [ ] The page does not scroll horizontally at any zoom.

### NVDA (Windows), if available

- [ ] Browse mode: H navigates headings, F navigates form fields.
- [ ] Focus mode on the question field announces the label.
- [ ] Elements list (NVDA+F7) shows sensible landmarks and headings.

### Keyboard only, no screen reader

- [ ] Unplug the mouse. Complete a whole question-to-brief flow.
- [ ] Focus is visible at every stop.
- [ ] Nothing traps focus, including inside expanded evidence.
- [ ] Escape does not lose work.
- [ ] Reorder brief questions **without a mouse** (limitation 5 predicts this
      fails; confirm and record).

### 200% zoom

- [ ] Browser zoom to 200% at 1280×1024. No horizontal scrolling.
- [ ] 400% at 1280 wide: content reflows to a single column, nothing clipped.
- [ ] Text spacing bookmarklet (1.4.12): line-height 1.5×, letter-spacing 0.12em,
      word-spacing 0.16em, paragraph-spacing 2×. Nothing clips or overlaps.

### Reduced motion

- [ ] macOS: System Settings → Accessibility → Display → Reduce motion.
      Background streaks stop; nothing animates.
- [ ] With the OS preference off, toggle the in-app **Reduce motion** setting.
      Same result.

### High contrast / forced colours

- [ ] Windows High Contrast, or `forced-colors: active`. Text remains readable,
      borders remain visible, focus remains visible.
- [ ] macOS Increase Contrast.

### Print and PDF brief

Covered in detail by docs/appointment-brief.md §7. For accessibility:

- [ ] Save the brief as PDF; text is selectable, not rasterised.
- [ ] Reading order in the PDF matches the visual order.
- [ ] Greyscale print: user-authored blocks are identifiable from their labels,
      not only the tint.

---

## 8. Reporting a barrier

If you hit something this document does not cover, that is the most useful
finding available, because it is a barrier automated testing did not detect. File
it with the state, the assistive technology and its version, and what you
expected to happen.

"""
synapse.brief.render_print
==========================
The printable one-page brief. Self-contained, deliberate, and honest about overflow.

Why this does not reuse the application's stylesheet
----------------------------------------------------
The app's CSS opens with ``@import url('https://fonts.googleapis.com/...')``.
An exported file is a file a patient may keep, mail to themselves, or open on a
clinic machine, and a remote asset in it means the document phones out to a
third party every time it is opened, disclosing when and roughly where it was
read. So the print stylesheet here is self-contained: system font stack, no
imports, no images, no scripts. Nothing in an exported brief reaches the
network.

It is also a light-on-white document rather than the app's dark theme. Printing
a dark UI wastes an extraordinary amount of toner and reads badly on paper.

One page, or an explicit choice
-------------------------------
The layout targets a single A4 or Letter page. When the content does not fit,
this module does **not** silently shrink the type: below about 9pt a printed
brief stops being readable for exactly the population most likely to need it.
:func:`estimate_fit` returns a measurement and
:func:`overflow_advice` returns what to drop, so the interface can ask the user
to choose. The user prioritises; the renderer never quietly degrades.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass

from synapse.brief.schema import (
    AppointmentBrief,
    BriefSection,
    ContentOrigin,
)
from synapse.escaping import escape

# Page geometry. Both common sizes are supported through @page, and the layout
# is sized for the smaller printable height of the two.
PAGE_SIZES: dict[str, str] = {"a4": "A4", "letter": "Letter"}

# Rough capacity model, in "lines of body text at 10pt with 1.45 line height on
# an A4 page with 14mm margins". Calibrated by rendering the fixtures and
# counting, not guessed: see docs/appointment-brief.md §6.
LINES_PER_PAGE = 58
CHARS_PER_LINE = 92

# Minimum type size. A brief printed below this is not accessible, so the
# renderer refuses to go there and asks the user to cut content instead.
MIN_BODY_PT = 9.0


@dataclass(frozen=True)
class FitEstimate:
    """Whether the brief is likely to fit one page, and by how much it misses."""

    estimated_lines: int
    capacity_lines: int = LINES_PER_PAGE

    @property
    def fits(self) -> bool:
        """True when the content is within one page."""
        return self.estimated_lines <= self.capacity_lines

    @property
    def overflow_lines(self) -> int:
        """How many lines too many, or zero."""
        return max(0, self.estimated_lines - self.capacity_lines)

    @property
    def fill_ratio(self) -> float:
        """Share of the page used."""
        return self.estimated_lines / self.capacity_lines if self.capacity_lines else 0.0


def _wrapped_lines(text: str, *, width: int = CHARS_PER_LINE) -> int:
    """Lines a string occupies once wrapped."""
    if not text:
        return 0
    return max(1, -(-len(text) // width))


def estimate_fit(brief: AppointmentBrief) -> FitEstimate:
    """Estimate how much of one page the brief will occupy.

    An estimate, and named as one. Real pagination depends on the print engine,
    the paper size and the user's browser settings; this is a deterministic
    approximation good enough to decide whether to ask the user to trim, and the
    QA checklist in docs/appointment-brief.md covers the cases it cannot model.
    """
    lines = 6  # Header block: title, document id, timestamp, provenance line
    if brief.includes(BriefSection.TOPIC) and brief.user.topic:
        lines += 2 + _wrapped_lines(brief.user.topic)
    if brief.includes(BriefSection.NOTES) and brief.user.notes:
        lines += 2 + _wrapped_lines(brief.user.notes)
    if brief.includes(BriefSection.SUMMARY) and brief.summary:
        lines += 2 + _wrapped_lines(brief.summary)
    if brief.includes(BriefSection.CLAIMS) and brief.claims:
        lines += 2 + sum(_wrapped_lines(claim.text) + 1 for claim in brief.claims)
    if brief.includes(BriefSection.QUESTIONS) and brief.questions:
        lines += 2 + sum(_wrapped_lines(question.text) for question in brief.questions)
    if brief.includes(BriefSection.LIMITATIONS) and brief.limitations:
        lines += 2 + sum(_wrapped_lines(item) for item in brief.limitations)
    if brief.includes(BriefSection.SOURCES) and brief.sources:
        # Each source is a title line plus a metadata line, and titles are long.
        lines += 2 + sum(_wrapped_lines(source.title) + 2 for source in brief.sources)
    lines += 4  # Disclaimer block, which is never omitted
    return FitEstimate(estimated_lines=lines)


def overflow_advice(brief: AppointmentBrief) -> list[str]:
    """What the user could drop, largest saving first.

    Returns suggestions, not actions. The renderer does not decide what matters
    to a patient's appointment; it measures and offers.
    """
    estimate = estimate_fit(brief)
    if estimate.fits:
        return []
    suggestions: list[str] = []
    if brief.includes(BriefSection.NOTES) and _wrapped_lines(brief.user.notes) > 3:
        suggestions.append("Shorten your notes, or leave them off the printout.")
    if brief.includes(BriefSection.CLAIMS) and len(brief.claims) > 3:
        suggestions.append(
            f"Remove some of the {len(brief.claims)} research statements. "
            "The questions matter more in the room."
        )
    if brief.includes(BriefSection.QUESTIONS) and len(brief.questions) > 5:
        suggestions.append(
            f"Keep your {min(5, len(brief.questions))} most important questions "
            f"of the {len(brief.questions)} listed."
        )
    if brief.includes(BriefSection.LIMITATIONS) and brief.limitations:
        suggestions.append("Leave off the limitations section; it is background, not action.")
    if brief.includes(BriefSection.SOURCES) and len(brief.sources) > 2:
        suggestions.append("Leave off the source list. Your clinician can ask for it.")
    if not suggestions:
        suggestions.append("Shorten the longest entries, or print on two pages.")
    return suggestions


PRINT_CSS = """
/* Self-contained: no font import, no webfont, no image, no script. An
   exported brief must never reach the network when it is opened. The export
   test greps for those tokens literally, so this comment avoids spelling them. */
:root {
  --ink: #111111;
  --muted: #444444;
  --rule: #cccccc;
  --user-bg: #f4f1fb;
  --user-edge: #6d28d9;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: #ffffff;
  color: var(--ink);
  /* System stack: identical rendering intent without a downloaded font. */
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 10pt;
  line-height: 1.45;
}
.sheet { max-width: 190mm; margin: 0 auto; padding: 12mm 14mm; }

header.brief-header { border-bottom: 2px solid var(--ink); padding-bottom: 6pt; margin-bottom: 10pt; }
.brief-title { font-size: 15pt; font-weight: 700; margin: 0 0 2pt 0; letter-spacing: -0.01em; }
.brief-meta { font-size: 8pt; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4pt 14pt; }
.brief-meta span { white-space: nowrap; }

section.brief-section { margin: 0 0 9pt 0; break-inside: avoid; }
h2.section-title {
  font-size: 8.5pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 0 0 3pt 0; padding-bottom: 2pt; border-bottom: 1px solid var(--rule);
}
p, li { margin: 0 0 3pt 0; overflow-wrap: anywhere; }

/* Anything the user wrote or changed is visually distinct from evidence-backed
   content: a tinted panel with a rule down the left edge, plus a text label.
   The label is what survives photocopying in black and white. */
.user-block {
  background: var(--user-bg);
  border-left: 3px solid var(--user-edge);
  padding: 5pt 7pt;
  margin: 0 0 4pt 0;
}
.user-tag {
  display: inline-block; font-size: 7.5pt; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.07em; color: var(--user-edge); margin-bottom: 2pt;
}

.claim { margin: 0 0 4pt 0; }
.claim-markers { font-weight: 700; }
.claim-partial { font-size: 8pt; color: var(--muted); }
.claim-edited { background: var(--user-bg); border-left: 3px solid var(--user-edge); padding: 4pt 7pt; }
.claim-edited .original { font-size: 8pt; color: var(--muted); margin-top: 2pt; }

ol.questions { margin: 0; padding-left: 14pt; }
ol.questions li { margin-bottom: 3pt; }
.q-own { font-size: 7.5pt; font-weight: 700; color: var(--user-edge); text-transform: uppercase; letter-spacing: 0.06em; }

.source { margin: 0 0 4pt 0; font-size: 9pt; break-inside: avoid; }
.source-title { font-weight: 600; }
.source-meta { color: var(--muted); font-size: 8pt; }
.source-labels { color: var(--muted); font-size: 8pt; font-style: italic; }

footer.brief-footer { margin-top: 10pt; border-top: 2px solid var(--ink); padding-top: 5pt; }
.disclaimer { font-size: 8.5pt; color: var(--ink); }
.doc-id { font-size: 7.5pt; color: var(--muted); margin-top: 3pt; }

@media print {
  /* Both common paper sizes; the layout is sized for the shorter of the two. */
  @page { size: A4; margin: 12mm; }
  .sheet { padding: 0; max-width: none; }
  /* Never break a section, a source or the disclaimer across pages. */
  section.brief-section, .source, footer.brief-footer { break-inside: avoid; }
  /* Keep backgrounds when the user prints in colour; the tint is how
     user-authored content is distinguished. The text label carries it when
     backgrounds are off. */
  .user-block, .claim-edited { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  a { text-decoration: none; color: var(--ink); }
  /* Print the URL after a link: a printed hyperlink is otherwise a dead end. */
  a[href]::after { content: " (" attr(href) ")"; font-size: 7.5pt; color: var(--muted); }
}
"""


def _section(title: str, body: str) -> str:
    """One titled section, or nothing when the body is empty."""
    if not body.strip():
        return ""
    return f'<section class="brief-section"><h2 class="section-title">{escape(title)}</h2>{body}</section>'


def _render_claims(brief: AppointmentBrief) -> str:
    """The research statements, with edited ones visibly separated."""
    rows: list[str] = []
    for claim in brief.visible_claims():
        if claim.origin is ContentOrigin.USER_EDITED:
            # Requirement 4: an edited claim is labelled, carries no citation,
            # and shows what it replaced.
            rows.append(
                f'<div class="claim claim-edited"><span class="user-tag">Edited by you '
                f"(not checked against a source)</span>"
                f"<div>{escape(claim.text)}</div>"
                f'<div class="original">Original wording: {escape(claim.original_text)} '
                f"{escape(claim.original_markers())}</div></div>"
            )
            continue
        partial = (
            ' <span class="claim-partial">(partly verified)</span>'
            if claim.support.value == "partially_supported"
            else ""
        )
        rows.append(
            f'<p class="claim">{escape(claim.text)} '
            f'<span class="claim-markers">{escape(claim.markers())}</span>{partial}</p>'
        )
    return "".join(rows)


def _render_questions(brief: AppointmentBrief) -> str:
    """The questions, in the order the user left them."""
    items = []
    for question in brief.visible_questions():
        own = (
            ' <span class="q-own">your question</span>'
            if question.origin is ContentOrigin.USER_AUTHORED
            else ""
        )
        items.append(f"<li>{escape(question.text)}{own}</li>")
    return f'<ol class="questions">{"".join(items)}</ol>' if items else ""


def _render_sources(brief: AppointmentBrief) -> str:
    """The source list, with governed labels and no invented fields."""
    rows: list[str] = []
    for source in brief.sources:
        meta = " · ".join(
            part
            for part in (
                source.container,
                source.publication_date,
                f"revised {source.revision_date}" if source.revision_date else "",
                source.identifier,
            )
            if part
        )
        link = f'<a href="{escape(source.url)}">{escape(source.url)}</a>' if source.url else ""
        labels = " · ".join(
            part for part in (source.evidence_type_label, source.review_label) if part
        )
        rows.append(
            f'<div class="source"><span class="source-title">[{source.number}] '
            f"{escape(source.title or source.source_id)}</span>"
            + (f'<div class="source-meta">{escape(meta)}</div>' if meta else "")
            + (f'<div class="source-labels">{escape(labels)}</div>' if labels else "")
            + (f'<div class="source-meta">{link}</div>' if link else "")
            + "</div>"
        )
    return "".join(rows)


def render_brief_html(brief: AppointmentBrief, *, page_size: str = "a4") -> str:
    """Render the complete printable document.

    Self-contained: one HTML file with one inline stylesheet, no external
    reference of any kind. Opening it offline produces the same page as opening
    it online, which for a health document is the property that matters.
    """
    size = PAGE_SIZES.get(page_size.lower(), "A4")
    css = PRINT_CSS.replace("@page { size: A4;", f"@page {{ size: {size};")
    generated = brief.provenance.generated_at.strftime("%d %B %Y, %H:%M %Z").strip()

    meta_parts = [f"<span>Prepared {escape(generated)}</span>"]
    if brief.provenance.app_version:
        meta_parts.append(f"<span>Synapse {escape(brief.provenance.app_version)}</span>")
    if brief.provenance.source_pack_version:
        meta_parts.append(
            f"<span>Sources {escape(brief.provenance.source_pack_id)} "
            f"{escape(brief.provenance.source_pack_version)}</span>"
        )
    if brief.provenance.corpus_version:
        meta_parts.append(f"<span>Corpus {escape(brief.provenance.corpus_version)}</span>")
    if brief.provenance.index_version:
        meta_parts.append(f"<span>Index {escape(brief.provenance.index_version)}</span>")

    body: list[str] = []
    if brief.includes(BriefSection.TOPIC) and brief.user.topic:
        body.append(
            _section(
                "What I want to talk about",
                f'<div class="user-block"><span class="user-tag">Written by you</span>'
                f"<div>{escape(brief.user.topic)}</div></div>",
            )
        )
    if brief.includes(BriefSection.SUMMARY) and brief.summary:
        body.append(_section("What the research says", f"<p>{escape(brief.summary)}</p>"))
    if brief.includes(BriefSection.CLAIMS):
        body.append(_section("Details from published research", _render_claims(brief)))
    if brief.includes(BriefSection.QUESTIONS):
        body.append(_section("Questions to ask", _render_questions(brief)))
    if brief.includes(BriefSection.NOTES) and brief.user.notes:
        body.append(
            _section(
                "My notes",
                f'<div class="user-block"><span class="user-tag">Written by you</span>'
                f"<div>{escape(brief.user.notes)}</div></div>",
            )
        )
    if brief.includes(BriefSection.LIMITATIONS) and brief.limitations:
        items = "".join(f"<li>{escape(item)}</li>" for item in brief.limitations)
        body.append(_section("What this does not cover", f"<ul>{items}</ul>"))
    if brief.includes(BriefSection.SOURCES) and brief.sources:
        body.append(_section("Sources", _render_sources(brief)))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Appointment brief {escape(brief.document_id)}</title>
<meta name="description" content="A one-page appointment preparation brief prepared with Synapse.">
<meta name="generator" content="Synapse {escape(brief.provenance.app_version or "unversioned")}">
<meta name="robots" content="noindex, nofollow, noarchive">
<style>{css}</style>
</head>
<body>
<main class="sheet">
<header class="brief-header">
  <h1 class="brief-title">Appointment brief</h1>
  <div class="brief-meta">{"".join(meta_parts)}</div>
</header>
{"".join(body)}
<footer class="brief-footer">
  <p class="disclaimer">{escape(brief.disclaimer)}</p>
  <p class="doc-id">Document {escape(brief.document_id)} · This sheet was prepared for a conversation with a clinician.</p>
</footer>
</main>
</body>
</html>
"""


__all__ = [
    "CHARS_PER_LINE",
    "LINES_PER_PAGE",
    "MIN_BODY_PT",
    "PAGE_SIZES",
    "PRINT_CSS",
    "FitEstimate",
    "estimate_fit",
    "overflow_advice",
    "render_brief_html",
]

"""
synapse.brief.render_pdf
========================
The appointment brief as a real PDF, laid out for a patient rather than an auditor.

Why a PDF at all, when there was a print stylesheet
---------------------------------------------------
There was, and :mod:`synapse.brief.render_print` still renders it. The HTML
route asked the browser to do the conversion, which is fine on a laptop and bad
on a phone: on iOS Safari "save as PDF" is a pinch gesture on the print preview
that most people never find, and a patient filling this in on the way to an
appointment is on a phone. A PDF the server produces is one tap, lands in Files,
and goes into the share sheet.

ReportLab rather than WeasyPrint: WeasyPrint would have reused the print CSS, at
the cost of Pango, cairo and their system libraries in the deployed image. This
module is pure Python, so the container and the Render blueprint are unchanged.

What the document is
--------------------
The recap, and what to ask. In order:

1. the summary of the conversation, which is what happened;
2. the questions to raise, which is what to do next.

Everything else is an add-on the patient turns on:
:attr:`~synapse.brief.schema.BriefSection.TRANSCRIPT` for the exchanges verbatim,
and the research sections for the claims, their citations and the studies behind
them. The brief used to print all of it every time, which made a patient
document out of an engineering one.

The disclaimer is not a section and cannot be switched off. It appears twice, on
purpose: in full in a box at the end, where a reader who reads to the end will
find it, and in one line in the footer of **every** page. The footer is the one
that matters. A patient who prints the first sheet, or photographs it and sends
the picture on, has a document in circulation that never reaches the last page,
and that document still has to say what produced it and what it is not.

Determinism
-----------
``invariant=1`` fixes the document identifier and the creation date that
ReportLab would otherwise stamp from the clock, so the same brief renders to the
same bytes twice. That is what makes a snapshot test of a PDF meaningful, and it
also means a patient who exports the same brief twice gets the same file rather
than two files that differ invisibly.

No network, no embedded fonts
-----------------------------
The four base-14 Helvetica faces are used, so nothing is embedded and nothing is
fetched. An exported brief opens offline, on a clinic machine, in ten years, and
reaches nowhere — the same property :mod:`synapse.brief.render_print` protects
by refusing a webfont import.
"""

from __future__ import annotations  # Postponed annotations

from io import BytesIO
from typing import TYPE_CHECKING, Any

from synapse.brief.schema import (
    STATUS_NOTES,
    AppointmentBrief,
    BriefSection,
    BriefTranscriptTurn,
    ContentOrigin,
    SupportLevel,
)
from synapse.escaping import escape
from synapse.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from reportlab.platypus import Flowable

logger = get_logger(__name__)

# Page geometry, in points. Mirrors render_print's 14mm margins closely enough
# that the two documents read as the same document.
PAGE_SIZES: tuple[str, ...] = ("a4", "letter")
MARGIN = 42.0  # ~15mm

# Type sizes. The floor is the same 9pt render_print refuses to go below: a
# brief printed smaller than this stops being readable for exactly the people
# most likely to need it.
TITLE_PT = 20.0
HEADING_PT = 12.5
BODY_PT = 10.5
SMALL_PT = 8.5
MIN_BODY_PT = 9.0

# Ink. Light on white, deliberately: printing the application's dark theme
# wastes an extraordinary amount of toner and reads badly on paper.
INK = "#111111"
INK_SECONDARY = "#444444"
RULE = "#cccccc"
NOTICE_BACKGROUND = "#f4f4f4"


class PdfRendererUnavailable(RuntimeError):
    """ReportLab is not installed, so no PDF can be produced.

    Raised rather than silently falling back to HTML. A caller that asked for a
    PDF and received a web page would hand the patient a file their phone does
    not know how to open.
    """


def _reportlab() -> Any:
    """Import ReportLab on demand, or explain why there is no PDF.

    Lazy so that :mod:`synapse.brief.schema` and the text renderer stay
    importable with pydantic alone — the core package declares no dependency on
    a PDF engine, and the schema layer is tested without one.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, LETTER
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import (
            HRFlowable,
            KeepTogether,
            ListFlowable,
            ListItem,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by test double
        raise PdfRendererUnavailable(
            "the PDF brief needs reportlab; install synapse with the 'api' extra"
        ) from exc

    return {
        "colors": colors,
        "TA_LEFT": TA_LEFT,
        "A4": A4,
        "LETTER": LETTER,
        "ParagraphStyle": ParagraphStyle,
        "HRFlowable": HRFlowable,
        "KeepTogether": KeepTogether,
        "ListFlowable": ListFlowable,
        "ListItem": ListItem,
        "PageBreak": PageBreak,
        "Paragraph": Paragraph,
        "SimpleDocTemplate": SimpleDocTemplate,
        "Spacer": Spacer,
        "Table": Table,
        "TableStyle": TableStyle,
    }


def _styles(rl: Any) -> dict[str, Any]:
    """The document's type scale, built rather than taken from a stylesheet.

    ReportLab's ``getSampleStyleSheet`` brings Times and a set of sizes chosen
    for a different document. Declaring the five styles used here is shorter
    than overriding it, and it keeps the sizes in one readable place.
    """
    style = rl["ParagraphStyle"]
    base = style(
        name="body",
        fontName="Helvetica",
        fontSize=BODY_PT,
        leading=BODY_PT * 1.45,
        textColor=rl["colors"].HexColor(INK),
        alignment=rl["TA_LEFT"],
    )
    return {
        "title": style(
            name="title",
            parent=base,
            fontName="Helvetica-Bold",
            fontSize=TITLE_PT,
            leading=TITLE_PT * 1.15,
            spaceAfter=2,
        ),
        "meta": style(
            name="meta",
            parent=base,
            fontSize=SMALL_PT,
            leading=SMALL_PT * 1.4,
            textColor=rl["colors"].HexColor(INK_SECONDARY),
        ),
        "heading": style(
            name="heading",
            parent=base,
            fontName="Helvetica-Bold",
            fontSize=HEADING_PT,
            leading=HEADING_PT * 1.3,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "body": base,
        "small": style(
            name="small",
            parent=base,
            fontSize=SMALL_PT,
            leading=SMALL_PT * 1.45,
            textColor=rl["colors"].HexColor(INK_SECONDARY),
        ),
    }


def _paragraphs(text: str) -> list[str]:
    """Split a recap into paragraphs on blank lines.

    ``build_conversation_brief`` joins one verified summary per answered turn
    with a blank line between them. Splitting here rather than printing the
    joined string keeps each turn's summary as its own paragraph, which is how
    a recap of four questions stays readable.
    """
    return [block.strip() for block in text.split("\n\n") if block.strip()]


def _numbered(rl: Any, styles: dict[str, Any], items: list[str]) -> Any:
    """A numbered list, which is what a question list wants to be."""
    return rl["ListFlowable"](
        [rl["ListItem"](rl["Paragraph"](escape(item), styles["body"])) for item in items],
        bulletType="1",
        bulletFontName="Helvetica-Bold",
        bulletFontSize=BODY_PT,
        leftIndent=18,
        spaceBefore=2,
    )


def _transcript_entry(rl: Any, styles: dict[str, Any], turn: BriefTranscriptTurn) -> list[Flowable]:
    """One exchange: what was asked, and what came back.

    Kept together on one page where it fits. An exchange split across a page
    break reads as two unrelated fragments, and the question is the half that
    gives the answer its meaning.
    """
    flow: list[Any] = [
        rl["Paragraph"](f"<b>You asked</b>  {escape(turn.question)}", styles["body"]),
    ]
    if turn.answer:
        flow.append(rl["Spacer"](1, 3))
        flow.append(rl["Paragraph"](f"<b>Synapse said</b>  {escape(turn.answer)}", styles["body"]))
    note = STATUS_NOTES.get(turn.status)
    if note is not None:
        flow.append(rl["Spacer"](1, 3))
        flow.append(rl["Paragraph"](escape(note), styles["small"]))
    flow.append(rl["Spacer"](1, 10))
    return [rl["KeepTogether"](flow)]


# The standing footer, on every page. The full disclaimer is a box at the end of
# the document, which is fine for a reader who reaches the end and useless for
# one who prints or photographs the first sheet and hands that over. So the
# short form is drawn on every page as well: a page of this document cannot
# circulate without saying what produced it and what it is not.
FOOTER_NOTICE = "Synapse research prototype — not a medical device, not clinically validated."


def _footer(rl: Any, canvas: Any, document: Any) -> None:
    """Draw the standing notice and the page number on the current page."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(rl["colors"].HexColor(INK_SECONDARY))
    baseline = MARGIN - 16
    canvas.drawString(MARGIN, baseline, FOOTER_NOTICE)
    canvas.drawRightString(
        document.pagesize[0] - MARGIN, baseline, f"Page {canvas.getPageNumber()}"
    )
    canvas.restoreState()


def _notice(rl: Any, styles: dict[str, Any], text: str) -> Any:
    """The disclaimer, in a box, so it is not mistaken for body copy."""
    table = rl["Table"](
        [[rl["Paragraph"](escape(text), styles["small"])]],
        colWidths=["100%"],
    )
    table.setStyle(
        rl["TableStyle"](
            [
                ("BACKGROUND", (0, 0), (-1, -1), rl["colors"].HexColor(NOTICE_BACKGROUND)),
                ("BOX", (0, 0), (-1, -1), 0.5, rl["colors"].HexColor(RULE)),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def _story(rl: Any, styles: dict[str, Any], brief: AppointmentBrief) -> list[Flowable]:
    """The whole document as a flat list of flowables, in printed order."""
    header: list[Any] = [
        rl["Paragraph"]("Appointment brief", styles["title"]),
        rl["Paragraph"](
            f"Prepared with Synapse on "
            f"{escape(brief.provenance.generated_at.strftime('%d %B %Y'))} "
            f"&middot; Reference {escape(brief.document_id)}",
            styles["meta"],
        ),
        rl["Spacer"](1, 8),
        rl["HRFlowable"](width="100%", thickness=0.5, color=rl["colors"].HexColor(RULE)),
    ]
    story: list[Any] = list(header)

    if brief.includes(BriefSection.TOPIC) and brief.user.topic.strip():
        story.append(rl["Paragraph"]("What you want to talk about", styles["heading"]))
        story.append(rl["Paragraph"](escape(brief.user.topic), styles["body"]))

    # 1. The recap. First, because it is what happened.
    if brief.includes(BriefSection.SUMMARY) and brief.summary.strip():
        story.append(rl["Paragraph"]("Summary of your conversation", styles["heading"]))
        for block in _paragraphs(brief.summary):
            story.append(rl["Paragraph"](escape(block), styles["body"]))
            story.append(rl["Spacer"](1, 5))

    # 2. What to do with it. Directly below, because it is the point of the sheet.
    questions = brief.visible_questions()
    if questions:
        story.append(rl["Paragraph"]("Questions to ask your doctor", styles["heading"]))
        story.append(_numbered(rl, styles, [question.text for question in questions]))

    if brief.includes(BriefSection.NOTES) and brief.user.notes.strip():
        story.append(rl["Paragraph"]("Your notes", styles["heading"]))
        story.append(rl["Paragraph"](escape(brief.user.notes), styles["body"]))

    # --- Add-ons, in the order a reader would want them ---

    transcript = brief.visible_transcript()
    if transcript:
        # The transcript starts a fresh page, so the recap stays a single sheet
        # a patient can hand over on its own -- but only when there is a recap
        # above it to break away from. Unconditionally breaking here printed a
        # blank first page whenever the transcript was the only section chosen.
        if len(story) > len(header):
            story.append(rl["PageBreak"]())
        story.append(rl["Paragraph"]("Your conversation", styles["heading"]))
        story.append(
            rl["Paragraph"](
                "Every question asked in this session, and the answer given.",
                styles["small"],
            )
        )
        story.append(rl["Spacer"](1, 8))
        for turn in transcript:
            story.extend(_transcript_entry(rl, styles, turn))

    claims = brief.visible_claims()
    if claims:
        story.append(rl["Paragraph"]("The research behind this", styles["heading"]))
        for claim in claims:
            if claim.origin is ContentOrigin.USER_EDITED:
                # An edited claim prints without markers and says so. The
                # citation belonged to the wording it replaced.
                text = f"{escape(claim.text)} <i>(edited by you)</i>"
            else:
                marker = f" {escape(claim.markers())}" if claim.source_numbers else ""
                qualifier = (
                    " <i>(partly supported)</i>"
                    if claim.support is SupportLevel.PARTIALLY_SUPPORTED
                    else ""
                )
                text = f"{escape(claim.text)}{marker}{qualifier}"
            story.append(rl["Paragraph"](text, styles["body"]))
            story.append(rl["Spacer"](1, 4))

    sources = brief.sources if brief.includes(BriefSection.SOURCES) else []
    if sources:
        story.append(rl["Paragraph"]("Studies referenced", styles["heading"]))
        for source in sources:
            detail = " &middot; ".join(
                part
                for part in (source.container, source.publication_date, source.identifier)
                if part
            )
            line = f"<b>[{source.number}]</b> {escape(source.title or source.source_id)}"
            if detail:
                line = f"{line}<br/>{detail}"
            story.append(rl["Paragraph"](line, styles["small"]))
            story.append(rl["Spacer"](1, 4))

    limitations = brief.limitations if brief.includes(BriefSection.LIMITATIONS) else []
    if limitations:
        story.append(rl["Paragraph"]("What this does not cover", styles["heading"]))
        story.append(
            rl["ListFlowable"](
                [
                    rl["ListItem"](rl["Paragraph"](escape(item), styles["small"]))
                    for item in limitations
                ],
                bulletType="bullet",
                leftIndent=14,
            )
        )

    # The disclaimer. Not a section, never optional, and last because that is
    # where a reader stops.
    story.append(rl["Spacer"](1, 16))
    story.append(_notice(rl, styles, brief.disclaimer))
    return story


def render_brief_pdf(brief: AppointmentBrief, *, page_size: str = "a4") -> bytes:
    """Render a brief to PDF bytes.

    Args:
        brief: the validated brief. Only the sections it includes are printed,
            except the disclaimer, which always is.
        page_size: ``"a4"`` or ``"letter"``. Anything else is a ValueError
            rather than a silent default, because a letter-size document
            printed on A4 loses its bottom margin.

    Returns:
        The complete PDF. Deterministic for a given brief: ReportLab's
        ``invariant`` mode fixes the identifier and creation date it would
        otherwise take from the clock.

    Raises:
        PdfRendererUnavailable: ReportLab is not installed.
        ValueError: unknown page size.
    """
    if page_size not in PAGE_SIZES:
        raise ValueError(f"unknown page size {page_size!r}")

    rl = _reportlab()
    buffer = BytesIO()
    document = rl["SimpleDocTemplate"](
        buffer,
        pagesize=rl["A4"] if page_size == "a4" else rl["LETTER"],
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        # The title shows in a viewer's window chrome and in the share sheet on
        # a phone. The reference is the brief's own random label, which names
        # nothing about the patient or the question.
        title=f"Appointment brief {brief.document_id}",
        author="Synapse",
        subject="Questions to raise at an appointment",
        # No clock, no random document id: same brief, same bytes.
        invariant=1,
    )

    def stamp(canvas: Any, doc: Any) -> None:
        """Per-page furniture. Bound here so it closes over ``rl``."""
        _footer(rl, canvas, doc)

    document.build(_story(rl, _styles(rl), brief), onFirstPage=stamp, onLaterPages=stamp)
    payload = buffer.getvalue()
    logger.info(
        "brief rendered to pdf",
        extra={
            "document_id": brief.document_id,
            "bytes": len(payload),
            "sections": len(brief.included_sections),
        },
    )
    return payload


__all__ = [
    "MIN_BODY_PT",
    "PAGE_SIZES",
    "PdfRendererUnavailable",
    "render_brief_pdf",
]

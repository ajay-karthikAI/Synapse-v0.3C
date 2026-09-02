"""
synapse.evidence.render
=======================
The citation control and the evidence card, as accessible HTML.

Why ``<details>``/``<summary>`` rather than a button and a script
----------------------------------------------------------------
The interface renders inside Streamlit's sanitised markdown, where scripts do
not run. That constraint turns out to be an advantage: a native disclosure
element is keyboard-operable, exposes its expanded state to assistive
technology without an ``aria-expanded`` attribute anyone can forget to update,
works with no JavaScript at all, and cannot be made hover-only by a later CSS
change. Every requirement in the UX list is satisfied by the element itself
rather than by code that has to keep satisfying them.

What a citation control opens
-----------------------------
One evidence card per cited source, carrying the **verbatim excerpt the verifier
matched** — copied from validated evidence, never regenerated (requirement 3) —
alongside the publication and governance metadata from
:mod:`synapse.evidence.metadata`. Every one of those fields is optional, and an
absent field is simply absent: no "Unknown" placeholder, which reads as a fact
about the source rather than a gap in the record.

Two separations that the rest of the module depends on:

* **Relevance is not quality.** A retrieval-relevance figure, when shown at all,
  is labelled "retrieval relevance", carries its own explanation, and sits apart
  from the review state. Requirements 7 to 10.
* **Patient view and review view are different functions.** Expired, superseded
  and rejected sources never reach a patient — P1 eligibility excludes them
  before retrieval — and their status is rendered only by
  :func:`render_review_evidence_card`.

Everything dynamic is escaped by :func:`synapse.escaping.escape`, whatever
its origin. PubMed titles are third-party content an attacker can influence
without touching this application.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Sequence
from dataclasses import dataclass

from synapse.escaping import escape
from synapse.evidence.insufficient import InsufficientEvidence, InsufficientReason
from synapse.evidence.labels import (
    RELEVANCE_EXPLANATION,
    RELEVANCE_LABEL,
    Label,
    describe_evidence_type,
    describe_retraction,
    describe_review_state,
)
from synapse.evidence.links import EXTERNAL_LINK_REL, canonical_link, doi_url, pmid_url
from synapse.evidence.metadata import SourceMetadata

# Identifier prefixes. Stable and derived from the claim and source identifiers
# the verifier already assigned, so a control, its card and an aria reference
# all agree without anything having to be kept in sync (requirement 4).
CLAIM_PREFIX = "claim"
CARD_PREFIX = "evidence"


def claim_dom_id(claim_id: str) -> str:
    """DOM id for a rendered claim."""
    return f"{CLAIM_PREFIX}-{_slug(claim_id)}"


def card_dom_id(claim_id: str, source_id: str) -> str:
    """DOM id for the evidence card belonging to one claim and source."""
    return f"{CARD_PREFIX}-{_slug(claim_id)}-{_slug(source_id)}"


def _slug(value: str) -> str:
    """Reduce an identifier to characters valid in a DOM id.

    ``pubmed:41802233#0000`` contains a colon and a hash, both of which are legal
    in an id attribute but require escaping in a CSS selector and break a
    fragment link. Substituting them keeps ids usable in every context.
    """
    return "".join(
        character if character.isalnum() or character == "-" else "-" for character in value
    )


@dataclass(frozen=True)
class EvidenceExcerpt:
    """One verbatim span, as the verifier matched it.

    A plain value object rather than the answer layer's ``SupportingExcerpt``, so
    this package renders evidence without importing the answer pipeline. The
    quote must arrive already validated; nothing here checks it, and nothing here
    may create one.
    """

    source_id: str
    chunk_id: str
    quote: str


def _definition_row(term: str, value: str) -> str:
    """One row of the metadata list, or nothing when the value is absent.

    Absence renders as absence. A "Not available" row is noise on a phone and
    reads as a statement about the source rather than about the record.
    """
    if not value:
        return ""
    return f'<div class="ev-row"><dt>{escape(term)}</dt><dd>{escape(value)}</dd></div>'


def _external_link(url: str, text: str) -> str:
    """An external link with the full set of tab-safety attributes.

    Returns nothing for a URL that did not validate, so a citation renders
    without a link rather than with a broken or unsafe one.
    """
    if not url:
        return ""
    return (
        f'<a class="ev-link" href="{escape(url)}" target="_blank" rel="{EXTERNAL_LINK_REL}">'
        f'{escape(text)}<span class="ev-external" aria-hidden="true"> ↗</span>'
        f'<span class="visually-hidden"> (opens in a new tab)</span></a>'
    )


def _label_chip(label: Label, *, kind: str) -> str:
    """A label with its plain-language explanation attached.

    The explanation is rendered as text rather than a ``title`` attribute: a
    tooltip is hover-only, invisible on a touch screen, and inconsistently
    announced by screen readers.
    """
    return (
        f'<div class="ev-label ev-label-{escape(kind)} ev-tone-{escape(label.tone)}">'
        f'<span class="ev-label-name">{escape(label.name)}</span>'
        f'<span class="ev-label-why">{escape(label.explanation)}</span></div>'
    )


def render_evidence_card(
    metadata: SourceMetadata,
    excerpts: Sequence[EvidenceExcerpt],
    *,
    claim_id: str,
    number: int,
    relevance: float | None = None,
) -> str:
    """One source's evidence card: excerpt first, then provenance.

    The excerpt leads because it is the thing being claimed. Metadata follows so
    a reader can weigh where it came from, and the governance labels come last
    with their explanations attached.
    """
    card_id = card_dom_id(claim_id, metadata.document_id)
    quotes = (
        "".join(
            f'<blockquote class="ev-quote">{escape(excerpt.quote)}'
            f'<cite class="ev-chunk">{escape(excerpt.chunk_id)}</cite></blockquote>'
            for excerpt in excerpts
        )
        or '<p class="ev-empty">No excerpt was recorded for this source.</p>'
    )

    rows = "".join(
        (
            _definition_row("Authors", metadata.authors),
            _definition_row("Journal", metadata.journal),
            _definition_row("Published by", metadata.issuing_organization),
            _definition_row("Publication date", metadata.publication_date),
            _definition_row("Revised", metadata.revision_date),
            _definition_row("PMID", metadata.pmid),
            _definition_row("DOI", metadata.doi),
            _definition_row("Guideline ID", metadata.guideline_id),
        )
    )
    metadata_block = f'<dl class="ev-meta">{rows}</dl>' if rows else ""

    link = canonical_link(metadata.canonical_url, doi=metadata.doi, pmid=metadata.pmid)
    links = _external_link(link, "View the source")
    # A DOI and a PMID resolve to different records; offering both is useful
    # where both exist, and neither is fabricated when it does not.
    if metadata.doi and doi_url(metadata.doi) and doi_url(metadata.doi) != link:
        links += _external_link(doi_url(metadata.doi), "DOI")
    if metadata.pmid and pmid_url(metadata.pmid) and pmid_url(metadata.pmid) != link:
        links += _external_link(pmid_url(metadata.pmid), "PubMed record")

    labels = _label_chip(describe_evidence_type(metadata.evidence_type), kind="type")
    labels += _label_chip(
        describe_review_state(metadata.lifecycle_state, is_governed=metadata.is_governed),
        kind="review",
    )
    retraction = describe_retraction(metadata.retraction_status)
    if retraction is not None:
        labels += _label_chip(retraction, kind="retraction")

    relevance_block = ""
    if relevance is not None:
        # Never "confidence" (requirement 7), and never presented beside the
        # review state, where it would read as a quality grade (requirement 9).
        relevance_block = (
            f'<div class="ev-relevance"><span class="ev-relevance-value">'
            f"{escape(RELEVANCE_LABEL)}: {escape(f'{relevance:.0%}')}</span>"
            f'<span class="ev-label-why">{escape(RELEVANCE_EXPLANATION)}</span></div>'
        )

    pack_block = ""
    if metadata.is_governed:
        pack_block = (
            f'<p class="ev-pack">From reviewed source list '
            f'<span class="ev-pack-id">{escape(metadata.pack_id)}</span> '
            f"version {escape(metadata.pack_version)}.</p>"
        )

    return (
        f'<article class="ev-card" id="{escape(card_id)}" '
        f'data-source-id="{escape(metadata.document_id)}" data-claim-id="{escape(claim_id)}">'
        f'<h3 class="ev-title"><span class="ev-number">[{number}]</span> '
        f"{escape(metadata.title or metadata.document_id)}</h3>"
        f'<div class="ev-section-label">What this source says</div>'
        f"{quotes}"
        f"{metadata_block}"
        f'<div class="ev-labels">{labels}</div>'
        f"{relevance_block}"
        f'<div class="ev-links">{links}</div>'
        f"{pack_block}"
        f"</article>"
    )


def render_citation_control(
    claim_id: str,
    entries: Sequence[tuple[int, SourceMetadata, Sequence[EvidenceExcerpt], float | None]],
) -> str:
    """The disclosure control that reveals a claim's evidence.

    ``<details>``/``<summary>`` gives keyboard operation, a semantic expanded
    state and a visible focus target for free. The summary names the sources by
    number so the control is meaningful when read out of context by a screen
    reader — "Show evidence" alone would be one of many identical controls on
    the page.
    """
    if not entries:
        return ""
    numbers = ", ".join(f"[{number}]" for number, _metadata, _excerpts, _relevance in entries)
    cards = "".join(
        render_evidence_card(
            metadata, excerpts, claim_id=claim_id, number=number, relevance=relevance
        )
        for number, metadata, excerpts, relevance in entries
    )
    count = len(entries)
    noun = "source" if count == 1 else "sources"
    return (
        f'<details class="ev-details" data-claim-id="{escape(claim_id)}">'
        f'<summary class="ev-summary">'
        f'<span class="ev-summary-text">Show the evidence for this statement</span>'
        # The count, not the numbers: the marker beside it already carries the
        # numbers visually, and repeating them here made a screen reader
        # announce "[1]" twice for a single-source claim.
        f'<span class="visually-hidden"> — {count} {noun}</span>'
        f'<span class="ev-summary-marker" aria-hidden="true">{escape(numbers)}</span>'
        f"</summary>"
        f'<div class="ev-cards">{cards}</div>'
        f"</details>"
    )


def render_insufficient_html(state: InsufficientEvidence) -> str:
    """The insufficient-evidence state, as a patient sees it.

    Composed entirely from module constants in
    :mod:`synapse.evidence.insufficient`. No parameter on this function can
    carry generated text, which is what makes "the gap is never filled with
    model knowledge" a property of the code rather than a habit.
    """
    steps = "".join(f"<li>{escape(step)}</li>" for step in state.next_steps)
    return (
        f'<section class="insufficient-card" role="status" data-reason="{escape(state.reason.value)}">'
        f'<h2 class="insufficient-heading">{escape(state.heading)}</h2>'
        f'<p class="insufficient-message">{escape(state.message)}</p>'
        f'<p class="insufficient-caveat">{escape(state.not_a_judgement)}</p>'
        f'<h3 class="card-label">What you can do</h3>'
        f'<ul class="insufficient-steps">{steps}</ul>'
        f"</section>"
    )


def render_review_evidence_card(metadata: SourceMetadata) -> str:
    """A source's governance status, for a review or admin view only.

    Expired, superseded and rejected sources are excluded from patient-facing
    answers by P1 eligibility before retrieval, so those states never appear in
    the patient view at all. A reviewer needs to see them — that is the entire
    job — and this function is the only place they are rendered.

    It renders **status**, and changes nothing: approval state is a governance
    decision recorded through the source-pack CLI by a person with a reviewer
    identity, and no interface writes it.
    """
    review_label = describe_review_state(metadata.lifecycle_state, is_governed=metadata.is_governed)
    rows = "".join(
        (
            _definition_row("Document", metadata.document_id),
            _definition_row("Lifecycle state", getattr(metadata.lifecycle_state, "value", "")),
            _definition_row("Superseded by", metadata.superseded_by),
            _definition_row("Retraction status", getattr(metadata.retraction_status, "value", "")),
            _definition_row("Reviewed on", metadata.reviewed_at),
            _definition_row("Review due", metadata.review_due),
            _definition_row("Pack", f"{metadata.pack_id} {metadata.pack_version}".strip()),
            _definition_row("Pack approval state", metadata.pack_approval_state),
        )
    )
    return (
        f'<article class="ev-card ev-review-card" data-source-id="{escape(metadata.document_id)}">'
        f'<h3 class="ev-title">{escape(metadata.title or metadata.document_id)}</h3>'
        f'<dl class="ev-meta">{rows}</dl>'
        f'<div class="ev-labels">{_label_chip(review_label, kind="review")}</div>'
        f"</article>"
    )


__all__ = [
    "CARD_PREFIX",
    "CLAIM_PREFIX",
    "EvidenceExcerpt",
    "InsufficientEvidence",
    "InsufficientReason",
    "card_dom_id",
    "claim_dom_id",
    "render_citation_control",
    "render_evidence_card",
    "render_insufficient_html",
    "render_review_evidence_card",
]

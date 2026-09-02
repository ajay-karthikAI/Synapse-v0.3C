"""
synapse.answer.render
=====================
Patient-facing rendering. Everything escaped, nothing parsed.

What this replaces, and why each part was unsafe:

* ``st.markdown(f'<div class="bubble-user">{turn["query"]}</div>',
  unsafe_allow_html=True)`` — the patient's own query interpolated raw into
  HTML.
* ``result['answer'].replace(chr(10),'<br>')`` — model output interpolated raw.
* ``{s.get('title','Unknown')}`` — a **PubMed-derived** title interpolated raw.
  That one is third-party content from an external feed, so it is the one an
  attacker can influence without touching the app at all.

Three rules here, all absolute:

1. **Every dynamic value is escaped**, whatever its origin — user, model, or
   source. The renderer does not distinguish between them, because trusting any
   category is how the next hole opens.
2. **The disclaimer is rendered by this module**, from a constant. It never
   comes from model output, so a truncated or malformed generation cannot drop
   it (requirement 10).
3. **Source numbering is assigned here, once**, from the retrieval order, and
   the same map drives both the inline markers and the source panel
   (requirement 11).

4. **The action state is explicit.** Every fragment carries
   ``data-action="answer|abstain|medical_staff|emergency"`` and, for the three
   non-answer states, a visible card. A patient should never have to infer from
   an absence of text that the system declined to answer.

Output is a small, fixed set of HTML fragments. No user-, model- or
source-controlled string ever reaches the page unescaped.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass, field

from synapse.answer.policy import ABSTENTION_SUMMARY
from synapse.answer.schema import AnswerAction, GroundedAnswer, SupportStatus
from synapse.answer.verify import RetrievedEvidence
from synapse.escaping import escape  # Re-exported: this module has always been its public home
from synapse.evidence.metadata import SourceMetadata
from synapse.evidence.render import (
    EvidenceExcerpt,
    claim_dom_id,
    render_citation_control,
)

# The permanent disclaimer. A module constant, so it is impossible for a model
# to omit, truncate or reword it. Rendered on EVERY answer including abstentions
# and emergency routing.
PERMANENT_DISCLAIMER = (
    "This is not a diagnosis and not personal medical advice. "
    "It is general information from published research to help you prepare for your appointment. "
    "Your clinician will assess your individual situation."
)

# Shown when routing to staff or escalating. Fixed text; the red-flag policy
# itself is unchanged by this module (requirement 17).
EMERGENCY_MESSAGE = (
    "Some of what you have described can need urgent attention. "
    "Please tell the front desk or a nurse how you are feeling right now, rather than waiting for your appointment. "
    "If you feel you are in danger, call your local emergency number or go to the nearest emergency department."
)

STAFF_MESSAGE = (
    "This is worth raising with a member of clinical staff before your appointment. "
    "Please let the front desk know."
)

# Heading shown above the abstention explanation. The explanation itself comes
# from synapse.answer.policy, so the wording exists in exactly one place.
ABSTENTION_LABEL = "No answer given"

# Heading above the failure card. The message shown beneath it is supplied by
# the caller from a fixed constant (synapse.ui.errors.PATIENT_ERROR_MESSAGE);
# this module never composes one out of an exception.
FAILURE_LABEL = "Answer unavailable"

# Label for the reranker score. Renamed from "confidence" (requirement 12):
# the underlying number is a reranker's relevance rating, and calling it
# confidence invited a patient to read it as confidence in the medical content.
RELEVANCE_LABEL = "relevance score"


@dataclass(frozen=True)
class SourceRef:
    """One numbered source, as shown in both the text and the panel."""

    number: int  # Stable display number, assigned once from retrieval order
    source_id: str
    title: str
    url: str
    relevance_score: float | None = (
        None  # Retrieval relevance, NOT a calibrated confidence and NOT a quality grade
    )

    def escaped_title(self) -> str:
        """Title, escaped. PubMed-derived and therefore untrusted."""
        return escape(self.title)

    def escaped_url(self) -> str:
        """URL, escaped, and only when it is http(s).

        A non-http scheme in a citation is dropped entirely rather than
        escaped — escaping ``javascript:`` still yields a working link.
        """
        return escape(self.url) if self.url.startswith(("http://", "https://")) else ""


@dataclass
class SourceNumbering:
    """Stable source numbering shared by the text and the panel.

    Assigned once, from retrieval order, and used by both renderers. The
    previous UI numbered the panel independently of the ``[Source N]`` markers
    the model emitted, so the two could disagree with nothing to detect it.
    """

    refs: dict[str, SourceRef] = field(default_factory=dict)  # source_id -> ref

    @classmethod
    def from_evidence(
        cls,
        evidence: RetrievedEvidence,
        order: list[str],
        relevance: dict[str, float] | None = None,
    ) -> SourceNumbering:
        """Number sources in retrieval order, skipping anything not retrieved."""
        relevance = relevance or {}
        refs: dict[str, SourceRef] = {}
        number = 1
        for source_id in order:
            if source_id in refs or source_id not in evidence.source_ids:
                continue  # Never number a source that was not retrieved
            refs[source_id] = SourceRef(
                number=number,
                source_id=source_id,
                title=evidence.source_titles.get(source_id, source_id),
                url=evidence.source_urls.get(source_id, ""),
                relevance_score=relevance.get(source_id),
            )
            number += 1
        return cls(refs=refs)

    def marker_for(self, source_id: str) -> str:
        """Inline marker for a source, e.g. ``[2]``. Empty when unnumbered."""
        ref = self.refs.get(source_id)
        return f"[{ref.number}]" if ref else ""

    def ordered(self) -> list[SourceRef]:
        """Sources in display order."""
        return sorted(self.refs.values(), key=lambda ref: ref.number)


def render_claim_html(claim, numbering: SourceNumbering, resolver=None) -> str:
    """One claim as escaped HTML, with its source markers.

    A partially-supported claim carries a visible marker; an unsupported one
    never reaches here, because :func:`synapse.answer.policy.apply` removes it
    from the answer entirely.
    """
    markers = "".join(numbering.marker_for(source_id) for source_id in claim.source_ids)
    suffix = ""
    if claim.support_status is SupportStatus.PARTIALLY_SUPPORTED:
        # The P1 display policy shows a partially-supported claim rather than
        # withholding it (DisplayPolicy.treat_partial_as_supported), so it must
        # carry a visible label. Never silently promoted to "supported".
        suffix = ' <span class="partial-flag" title="Some supporting detail could not be verified against the source">(partly verified)</span>'
    # data-claim-id is the same identifier the verifier keyed on and the same
    # one the evidence cards and the exported brief use, so a claim can be
    # traced across all four surfaces.
    control = _citation_control_for(claim, numbering, resolver)
    return (
        f'<div class="claim-block">'
        f'<p class="claim" id="{escape(claim_dom_id(claim.claim_id))}" '
        f'data-claim-id="{escape(claim.claim_id)}">{escape(claim.text)} '
        f'<span class="cite">{escape(markers)}</span>{suffix}</p>'
        f"{control}"
        f"</div>"
    )


def _citation_control_for(claim, numbering: SourceNumbering, resolver) -> str:
    """Build the disclosure control that reveals this claim's evidence.

    Every displayed claim gets one (requirement 1). Excerpts come from the
    validated answer object — the spans the verifier matched against the cited
    chunk — and are never regenerated (requirement 3).

    A claim citing a source that was not retrieved contributes nothing: the
    numbering has no entry for it, so it is skipped rather than rendered as an
    unresolvable citation.
    """
    entries = []
    for source_id in claim.source_ids:
        ref = numbering.refs.get(source_id)
        if ref is None:
            continue  # Never rendered: an unnumbered source was not retrieved
        metadata = (
            resolver.resolve(source_id, title=ref.title, url=ref.url)
            if resolver is not None
            else SourceMetadata.minimal(source_id, title=ref.title, url=ref.url)
        )
        excerpts = [
            EvidenceExcerpt(
                source_id=excerpt.source_id, chunk_id=excerpt.chunk_id, quote=excerpt.quote
            )
            for excerpt in claim.supporting_excerpts
            if excerpt.source_id == source_id
        ]
        entries.append((ref.number, metadata, excerpts, ref.relevance_score))
    return render_citation_control(claim.claim_id, entries)


def render_answer_html(
    answer: GroundedAnswer,
    numbering: SourceNumbering,
    *,
    query: str = "",
    resolver=None,
) -> str:
    """Render a verified answer as escaped HTML.

    Structure comes entirely from typed fields. Nothing is parsed out of prose,
    so a missing section is impossible rather than invisible.
    """
    parts: list[str] = []

    if query:
        # The patient's own words, escaped. Previously interpolated raw.
        parts.append(f'<div class="bubble-user">{escape(query)}</div>')

    if answer.action is AnswerAction.EMERGENCY:
        parts.append(
            f'<div class="emerg-card" role="alert">'
            f'<h2 class="emerg-title">Please speak with medical staff now</h2>'
            f"{escape(EMERGENCY_MESSAGE)}</div>"
        )
        parts.append(_disclaimer_html())  # Rendered even here; requirement 10 admits no exceptions
        return _wrap(answer.action, parts)

    if answer.action is AnswerAction.MEDICAL_STAFF:
        parts.append(f'<div class="staff-card" role="status">{escape(STAFF_MESSAGE)}</div>')

    if answer.action is AnswerAction.ABSTAIN:
        # Abstention is a state, not a shorter answer. It gets its own card so a
        # patient is told the system declined rather than left to infer it from
        # a page with no claims on it.
        parts.append(
            f'<div class="abstain-card"><h2 class="card-label">{escape(ABSTENTION_LABEL)}</h2>'
            f'<div class="card-body">{escape(answer.summary or ABSTENTION_SUMMARY)}</div></div>'
        )
    elif answer.summary:
        parts.append(
            f'<div class="resp-card"><h2 class="card-label">Summary</h2><div class="card-body">{escape(answer.summary)}</div></div>'
        )

    if answer.claims:
        claims_html = "".join(
            render_claim_html(claim, numbering, resolver) for claim in answer.claims
        )
        parts.append(
            f'<div class="resp-card"><h2 class="card-label">What the research says</h2><div class="card-body">{claims_html}</div></div>'
        )

    if answer.doctor_evaluation:
        parts.append(
            f'<div class="resp-card"><h2 class="card-label">What your doctor will evaluate</h2>'
            f'<div class="card-body">{escape(answer.doctor_evaluation)}</div></div>'
        )

    if answer.questions_for_doctor:
        items = "".join(
            f'<div class="q-item"><span class="q-arrow" aria-hidden="true">&rarr;</span><span>{escape(q)}</span></div>'
            for q in answer.questions_for_doctor
        )
        parts.append(
            f'<div class="q-card"><h2 class="card-label">Questions to ask your doctor</h2>{items}</div>'
        )

    if answer.limitations:
        # Rendered as a compact footnote rather than a full card. Limitations are
        # background: they qualify the answer, they are not a section of it, and
        # giving them the same visual weight as "What the research says" told a
        # reader they mattered equally. The heading stays a real h2 so the
        # section is still navigable by screen reader; only its prominence drops.
        items = "".join(f"<li>{escape(limitation)}</li>" for limitation in answer.limitations)
        parts.append(
            f'<div class="limits-note"><h2 class="card-label limits-label">Limitations</h2>'
            f'<ul class="limits-list">{items}</ul></div>'
        )

    parts.append(_disclaimer_html())  # Always last, always from the constant
    return _wrap(answer.action, parts)


def _wrap(action: AnswerAction, parts: list[str]) -> str:
    """Wrap fragments in a container that names the action state.

    The state is in the markup rather than implied by which cards happen to be
    present, so "did this abstain?" is answerable by inspection — by a test, by
    a reviewer, and by any surface that styles the four states differently.
    """
    body = "\n".join(parts)
    # role="status" announces that an answer arrived without stealing focus.
    # Emergency carries its own role="alert" inside, which is the only case that
    # should interrupt what the user is reading.
    live = ' role="status"' if action is not AnswerAction.EMERGENCY else ""
    return f'<div class="answer-view" data-action="{escape(action.value)}"{live}>\n{body}\n</div>'


def _disclaimer_html() -> str:
    """The permanent disclaimer, from the module constant."""
    return f'<div class="disclaimer-permanent">{escape(PERMANENT_DISCLAIMER)}</div>'


def render_evidence_html(answer: GroundedAnswer, numbering: SourceNumbering) -> str:
    """Render the supporting excerpts behind each displayed claim.

    One card per claim, carrying the claim identifier, the source marker from
    the *same* numbering the inline citations use, and the verbatim quote the
    verifier matched against the chunk. This is what makes a citation checkable
    by the person reading it rather than only by the test suite.

    Only claims present on ``answer`` appear here, and
    :func:`synapse.answer.policy.apply` has already removed the withheld ones —
    so an unsupported claim cannot leak in through the evidence panel.
    """
    if not answer.claims:
        return '<div class="src-empty">No verified excerpts to show for this answer.</div>'
    cards: list[str] = []
    for claim in answer.claims:
        rows: list[str] = []
        for excerpt in claim.supporting_excerpts:
            ref = numbering.refs.get(excerpt.source_id)
            marker = f"[{ref.number}]" if ref else ""
            title = ref.escaped_title() if ref else escape(excerpt.source_id)
            rows.append(
                f'<div class="ev-quote"><span class="cite">{escape(marker)}</span> '
                f'<span class="ev-source">{title}</span>'
                f'<blockquote class="ev-text">{escape(excerpt.quote)}</blockquote>'
                f'<div class="src-sub">{escape(excerpt.chunk_id)}</div></div>'
            )
        body = "".join(rows) or '<div class="src-sub">No excerpt recorded.</div>'
        cards.append(
            f'<div class="ev-card" data-claim-id="{escape(claim.claim_id)}">'
            f'<div class="src-name">{escape(claim.text)}</div>{body}</div>'
        )
    return "\n".join(cards)


def render_failure_html(message: str, error_code: str) -> str:
    """Render a failure card: a fixed message, a typed code, and the disclaimer.

    ``message`` must be a constant chosen by the caller (see
    :data:`synapse.ui.errors.PATIENT_ERROR_MESSAGE`) and ``error_code`` a value
    from a closed enum. Both are escaped anyway — a renderer that trusts its
    caller is one refactor away from interpolating an exception.

    There is deliberately no parameter through which model output could be
    passed: a failed answer shows no medical content at all.
    """
    return (
        f'<div class="answer-view" data-action="error">\n'
        f'<div class="resp-card"><h2 class="card-label">{escape(FAILURE_LABEL)}</h2>'
        f'<div class="card-body">{escape(message)}</div>'
        f'<div class="src-sub">Reference code: {escape(error_code)}</div></div>\n'
        f"{_disclaimer_html()}\n"
        f"</div>"
    )


def render_sources_html(numbering: SourceNumbering) -> str:
    """Render the source panel, using the same numbering as the inline markers."""
    if not numbering.refs:
        return '<div class="src-empty">No sources were used for this answer.</div>'
    rows: list[str] = []
    for ref in numbering.ordered():
        score = ""
        if ref.relevance_score is not None:
            # "relevance score", never "confidence": the number is a reranker's
            # rating of passage usefulness, and it is not calibrated against
            # anything clinical.
            score = f'<div class="src-sub">{escape(RELEVANCE_LABEL)}: {escape(f"{ref.relevance_score:.2f}")}</div>'
        link = (
            f'<a href="{ref.escaped_url()}" rel="noopener noreferrer nofollow" target="_blank">View source</a>'
            if ref.escaped_url()
            else ""
        )
        rows.append(
            f'<div class="src-item"><div class="src-name">[{ref.number}] {ref.escaped_title()}</div>'
            f'<div class="src-sub">{escape(ref.source_id)}</div>{score}{link}</div>'
        )
    return "\n".join(rows)


def render_plain_text(answer: GroundedAnswer, numbering: SourceNumbering) -> str:
    """Plain-text rendering, for logs, snapshots and non-HTML surfaces.

    Deliberately not derived from the HTML: a text renderer that strips tags
    would inherit any escaping bug the HTML path has.
    """
    lines: list[str] = []
    if answer.action is AnswerAction.EMERGENCY:
        lines.append(EMERGENCY_MESSAGE)
        lines.append("")
        lines.append(PERMANENT_DISCLAIMER)
        return "\n".join(lines)

    if answer.action is AnswerAction.MEDICAL_STAFF:
        lines.extend([STAFF_MESSAGE, ""])
    if answer.summary:
        lines.extend([answer.summary, ""])
    for claim in answer.claims:
        markers = "".join(numbering.marker_for(source_id) for source_id in claim.source_ids)
        flag = (
            " (partly verified)"
            if claim.support_status is SupportStatus.PARTIALLY_SUPPORTED
            else ""
        )
        lines.append(f"- {claim.text} {markers}{flag}".rstrip())
    if answer.claims:
        lines.append("")
    if answer.doctor_evaluation:
        lines.extend([f"What your doctor will evaluate: {answer.doctor_evaluation}", ""])
    if answer.questions_for_doctor:
        lines.append("Questions to ask your doctor:")
        lines.extend(f"  - {question}" for question in answer.questions_for_doctor)
        lines.append("")
    if answer.limitations:
        lines.append("Limitations:")
        lines.extend(f"  - {limitation}" for limitation in answer.limitations)
        lines.append("")
    if numbering.refs:
        lines.append("Sources:")
        lines.extend(
            f"  [{ref.number}] {ref.title} ({ref.source_id})" for ref in numbering.ordered()
        )
        lines.append("")
    lines.append(PERMANENT_DISCLAIMER)
    return "\n".join(lines)


__all__ = [
    "ABSTENTION_LABEL",
    "EMERGENCY_MESSAGE",
    "FAILURE_LABEL",
    "PERMANENT_DISCLAIMER",
    "RELEVANCE_LABEL",
    "STAFF_MESSAGE",
    "SourceNumbering",
    "SourceRef",
    "escape",
    "render_answer_html",
    "render_claim_html",
    "render_evidence_html",
    "render_failure_html",
    "render_plain_text",
    "render_sources_html",
]

"""
synapse.answer
==============
Grounded answer generation: every medical claim traceable to evidence.

Replaces emoji-heading parsing of free-form model prose. The previous renderer
recovered structure with three regexes over model output, so a reordered or
truncated generation silently dropped a section — including, when truncated, the
disclaimer. Nothing verified that the ``[Source N]`` markers in the prose
corresponded to anything retrieved.

Now:

    synapse.answer.schema    the validated response contract
    synapse.answer.verify    deterministic claim verification against evidence
    synapse.answer.support   lexical support; optional entailment interface
    synapse.answer.policy    display / abstain decision
    synapse.answer.render    escaped rendering, stable numbering, fixed disclaimer
    synapse.answer.brief     the exported appointment brief, same identifiers
    synapse.answer.generate  structured generation and the end-to-end pipeline
    synapse.answer.providers schema-constrained generation clients

The invariants: an unsupported claim is never displayed; the disclaimer is
rendered from a constant and never parsed from model output; every dynamic
value is escaped regardless of origin; and support status is assigned by the
verifier, never by the model.
"""

from __future__ import annotations  # Postponed annotations

from synapse.answer.brief import BRIEF_FILENAME, render_appointment_brief
from synapse.answer.generate import AnswerResult, answer_query, parse_answer
from synapse.answer.policy import DisplayPolicy, PolicyDecision, apply, decide
from synapse.answer.providers import OpenAIStructuredClient, ProviderUnavailableError
from synapse.answer.render import (
    PERMANENT_DISCLAIMER,
    SourceNumbering,
    escape,
    render_answer_html,
    render_evidence_html,
    render_failure_html,
    render_plain_text,
    render_sources_html,
)
from synapse.answer.schema import (
    AnswerAction,
    GroundedAnswer,
    GroundedClaim,
    SupportingExcerpt,
    SupportStatus,
)
from synapse.answer.support import LexicalEntailmentChecker, NullEntailmentChecker, lexical_support
from synapse.answer.verify import RetrievedEvidence, VerificationReport, verify_answer

__all__ = [
    "BRIEF_FILENAME",
    "PERMANENT_DISCLAIMER",
    "AnswerAction",
    "AnswerResult",
    "DisplayPolicy",
    "GroundedAnswer",
    "GroundedClaim",
    "LexicalEntailmentChecker",
    "NullEntailmentChecker",
    "OpenAIStructuredClient",
    "PolicyDecision",
    "ProviderUnavailableError",
    "RetrievedEvidence",
    "SourceNumbering",
    "SupportStatus",
    "SupportingExcerpt",
    "VerificationReport",
    "answer_query",
    "apply",
    "decide",
    "escape",
    "lexical_support",
    "parse_answer",
    "render_answer_html",
    "render_appointment_brief",
    "render_evidence_html",
    "render_failure_html",
    "render_plain_text",
    "render_sources_html",
    "verify_answer",
]

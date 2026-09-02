"""
synapse.answer.generate
=======================
Producing a structured answer, and the end-to-end pipeline that verifies it.

Structured output rather than prose parsing (requirement 1). The provider is
handed the JSON Schema generated from :class:`GroundedAnswer` itself, so the
contract the model is given and the contract the code enforces are the same
object and cannot drift. If the provider does not support schema-constrained
decoding, the same schema is used for strict validation of a JSON response —
either way, a malformed answer is a rejection, not something to regex.

The pipeline is short and the order is the point:

    generate -> parse -> verify -> decide -> apply -> render

Verification happens **before** any display decision, and display happens only
on what survived. There is no path from a raw generation to a rendered page.
"""

from __future__ import annotations  # Postponed annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from synapse.answer.policy import DisplayPolicy, PolicyDecision, apply, decide
from synapse.answer.schema import AnswerAction, GroundedAnswer, provider_json_schema
from synapse.answer.verify import RetrievedEvidence, VerificationReport, verify_answer
from synapse.errors import SynapseArtifactError
from synapse.logging import get_logger

logger = get_logger(__name__)

# Versioned. Any change to the wording below is a change to this identifier, so
# a telemetry record or an evaluation run can be attributed to the prompt that
# produced it.
PROMPT_ID = "grounded-answer-v3"

SYSTEM_PROMPT = """\
You help patients prepare for an upcoming appointment with their clinician.

You answer ONLY from the source passages supplied. You do not diagnose, and you
do not recommend starting, stopping or changing any medication.

Return a JSON object matching the supplied schema. For every medical claim you
make you MUST provide, in supporting_excerpts, a quote copied EXACTLY and
character-for-character from one of the supplied passages, together with the
source_id and chunk_id it came from.

COVERAGE. Work through the passages and make a SEPARATE claim for each distinct,
useful point they support. Aim for three to six claims when the passages support
that many, drawing on more than one passage where you can. Do not pad: a claim
that repeats another in different words is not a second claim. But do not stop
at one point when the passages plainly support several, because a patient
preparing for an appointment needs the whole picture the sources can give.

Each claim carries its own excerpt. A long answer built from one quote is worse
than a short answer built from four.

Do not invent a source_id, a chunk_id, or a quote. Do not paraphrase inside a
quote. If the passages do not support a claim, do not make the claim. If they do
not support an answer at all, set action to "abstain".

Every quote you supply will be checked against the original passage. A quote
that is not found verbatim will cause the claim to be discarded.

CONVERSATION CONTEXT. A CONVERSATION SO FAR section may appear before the
passages, listing earlier questions from this patient. It is there so you can
tell what a follow-up refers to -- "it", "those", "what about the side effects?".
It is NOT a source. It has no source_id and no chunk_id, nothing in it has been
retrieved or checked, and it may not be quoted or cited. Every supporting_excerpt
must come from PASSAGES. If the passages do not support an answer to the resolved
question, abstain -- the conversation is not evidence that anything is true.
"""


class GenerationClient(Protocol):
    """Minimal provider interface.

    Narrow on purpose so the answer path can be driven by a fake in tests, and
    so provider types never leak into the verification layer.
    """

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str: ...


class GenerationError(SynapseArtifactError):
    """The model did not return a usable structured answer."""

    reason = "answer generation failed"


# Bounds on the conversation block. A prior question is patient-authored text of
# unbounded length, and the block sits ahead of the passages: uncapped, a long
# one would push the evidence the answer must be built from toward the end of the
# context. Turns are capped as well, so the block cannot grow without limit
# across a long session.
MAX_HISTORY_TURNS = 3
MAX_HISTORY_QUESTION_CHARS = 300

HISTORY_FENCE = (
    "CONVERSATION SO FAR -- context only. This is NOT a source. It has no "
    "source_id and no chunk_id. Nothing here may be quoted or cited."
)


def build_history_block(history: Sequence[str]) -> str:
    """Render earlier patient questions as fenced, non-citable context.

    Returns ``""`` for empty or unusable history, which is what keeps the
    no-history prompt byte-identical to the one this function replaced.

    Only the patient's *questions* are carried, never prior answer text. A
    question is what disambiguates a follow-up; prior answer prose is what would
    tempt the model to quote something that is not in PASSAGES, and a quote that
    is not in the passages costs the claim (synapse.answer.verify).

    Whitespace is collapsed per question. The block is newline-delimited, so an
    embedded newline in patient-supplied text would otherwise forge a turn
    boundary inside the fence.
    """
    lines: list[str] = []
    for question in list(history)[-MAX_HISTORY_TURNS:]:
        if not isinstance(question, str):
            continue  # Defensive: this is patient-derived data crossing a boundary
        collapsed = " ".join(question.split())[:MAX_HISTORY_QUESTION_CHARS]
        if collapsed:
            lines.append(f'Turn {len(lines) + 1}: patient asked "{collapsed}"')
    if not lines:
        return ""
    return HISTORY_FENCE + "\n" + "\n".join(lines)


def build_user_prompt(
    query: str,
    evidence: RetrievedEvidence,
    order: list[str],
    history: Sequence[str] = (),
) -> str:
    """Assemble the evidence block and the patient's question.

    Chunks are labelled with their real identifiers rather than ``[Source 1]``,
    so the model cites the identifiers the verifier checks against. The old
    prompt asked for ``[Source N]`` markers that nothing could resolve back to a
    document.

    ``history`` is earlier patient questions, oldest first, and is optional. It
    is rendered ahead of the passages behind an explicit fence saying it is not a
    source. Omitted or empty, the prompt is byte-identical to the one produced
    before conversational memory existed -- which is what lets every existing
    caller and recorded fixture stay valid.
    """
    blocks: list[str] = []
    for source_id in order:
        for chunk_id, text in evidence.chunk_texts.items():
            if chunk_id.startswith(f"{source_id}#"):
                blocks.append(f"source_id: {source_id}\nchunk_id: {chunk_id}\ntext: {text}")
    evidence_block = "\n\n".join(blocks) if blocks else "(no passages were retrieved)"
    prompt = f"PASSAGES:\n\n{evidence_block}\n\n---\n\nPATIENT QUESTION: {query}"

    history_block = build_history_block(history)
    if not history_block:
        return prompt
    return f"{history_block}\n\n{prompt}"


def parse_answer(raw: str) -> GroundedAnswer:
    """Parse and validate a model response.

    Raises:
        GenerationError: the response is not valid JSON, or does not satisfy the
            schema. Rejected rather than repaired — guessing at what a malformed
            answer meant is how unverified content reaches a patient.
    """
    text = raw.strip()
    if text.startswith("```"):  # Models fence JSON despite instructions
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        payload = json.loads(text)  # SAFE deserialisation
    except json.JSONDecodeError as exc:
        raise GenerationError(problem="model response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GenerationError(problem="model response was not a JSON object")
    # support_status is stripped: it is the verifier's to assign, and a model
    # marking its own work is the thing under test.
    for claim in payload.get("claims", []) or []:
        if isinstance(claim, dict):
            claim.pop("support_status", None)
    try:
        return GroundedAnswer.model_validate(payload)
    except ValidationError as exc:
        raise GenerationError(
            problem="model response did not satisfy the answer schema",
            error_count=exc.error_count(),
        ) from exc


@dataclass
class AnswerResult:
    """Everything one answered query produced."""

    answer: GroundedAnswer  # As it will be displayed, after withholding
    raw_answer: GroundedAnswer  # As verified, before withholding, for audit
    verification: VerificationReport
    decision: PolicyDecision

    @property
    def abstained(self) -> bool:
        """True when no medical content will be shown."""
        return self.decision.abstained


def answer_query(
    query: str,
    evidence: RetrievedEvidence,
    source_order: list[str],
    client: GenerationClient,
    *,
    policy: DisplayPolicy | None = None,
    is_emergency: bool = False,
    history: Sequence[str] = (),
) -> AnswerResult:
    """Generate, verify and gate an answer.

    ``is_emergency`` comes from the existing red-flag detector, which runs
    before retrieval. It short-circuits generation entirely — the same ordering
    the previous implementation had, preserved deliberately (requirement 17).

    ``history`` is earlier patient questions, used only as prompt context for
    resolving a follow-up. It changes nothing downstream: verification still runs
    against ``evidence`` alone, so a claim quoting the conversation cites a
    source that was never retrieved and is withheld exactly as any other
    unsupported claim is.
    """
    if is_emergency:
        emergency = GroundedAnswer(action=AnswerAction.EMERGENCY)
        empty = VerificationReport()
        return AnswerResult(
            answer=emergency,
            raw_answer=emergency,
            verification=empty,
            decision=decide(emergency, empty, policy),
        )

    raw = client.generate_structured(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(query, evidence, source_order, history),
        schema=provider_json_schema(),
    )
    parsed = parse_answer(raw)

    # Verify BEFORE deciding, and decide before displaying. There is no path
    # from a raw generation to a rendered page.
    verified, report = verify_answer(parsed, evidence)
    decision = decide(verified, report, policy)
    displayed = apply(verified, decision)

    logger.info(
        "answer produced",
        extra={
            "action": displayed.action.value,
            "claims_shown": len(displayed.claims),
            "claims_withheld": len(decision.withheld_claim_ids),
        },
    )
    return AnswerResult(
        answer=displayed, raw_answer=verified, verification=report, decision=decision
    )


__all__ = [
    "PROMPT_ID",
    "SYSTEM_PROMPT",
    "AnswerResult",
    "GenerationClient",
    "GenerationError",
    "answer_query",
    "build_history_block",
    "build_user_prompt",
    "parse_answer",
]

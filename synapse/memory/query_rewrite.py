"""
synapse.memory.query_rewrite
============================
Resolving a context-dependent follow-up into a standalone **retrieval** query.

The problem
-----------
Every turn in this system is independent. ``synapse.ui.pipeline.answer_turn``
receives a bare string and hands the *same* string to retrieval and to
generation, and ``st.session_state.conversation`` is read only by the renderer.
So a follow-up like "what about the side effects?" reaches BM25 and FAISS as six
context-free words, and the candidate set it produces is about the words "side
effects" in general rather than about whatever the patient was just reading.

The design decision
-------------------
The rewrite is applied to the **retrieval query only**. Generation continues to
receive the patient's raw words, unchanged. This is not a convenience: the
answer layer verifies every quote against the passages it was given, and the
display policy withholds what it cannot support. Feeding a machine-rewritten
question into generation would put words in the patient's mouth on the one path
where the output is medical text. Feeding it into retrieval only changes *which
passages are considered*, and every downstream guarantee still holds over
whatever comes back.

That split is available for free because ``answer_turn`` passes one ``query``
variable to both stages: a caller that rewrites inside its ``retrieve`` callable
gets the resolved query in retrieval and the raw query in generation without any
signature change anywhere in the pipeline.

Failure policy: fail open, always
---------------------------------
Every error path returns ``(query, False)`` — the original query, unrewritten.
This mirrors ``synapse.retrieval.rerank``: a fault degrades ranking quality and
never correctness. There is no failure of this module that should be able to
stop a patient getting an answer, so this module raises nothing at all. A
rewrite that did not happen leaves the system exactly where it was before this
module existed.

Constraints this module holds itself to
---------------------------------------
* **Pure.** No Streamlit import, no ``st.session_state``, no module-level
  mutable state. It runs on a worker thread with no ``ScriptRunContext``, where
  touching Streamlit is an error and reading session state returns nothing.
* **Narrow input.** :class:`TurnSummary` carries two strings. A ``TurnOutcome``
  — with its presentation, verification report and policy decision — is
  deliberately *not* accepted, so no typed presentation object crosses into a
  module whose whole job is to build a prompt string.
* **Nothing is written.** No file, no pickle, no serialisation of the query.
  Consistent with docs/privacy-logging-policy.md, the log lines below carry
  lengths, counts, booleans and exception *types* — never the question, the
  prior answer, or any fragment of either.
"""

from __future__ import annotations  # Postponed annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from synapse.answer.generate import GenerationClient  # The same protocol the answer layer uses
from synapse.logging import get_logger

logger = get_logger(__name__)

# Versioned for the same reason the answer and rerank prompts are: a wording
# change here changes which queries get rewritten and how, and a telemetry record
# or an evaluation run has to be attributable to the prompt that produced it.
PROMPT_ID = "query-rewrite-v1"

SYSTEM_PROMPT = """\
You rewrite a patient's follow-up question into a standalone search query.

The query is used ONLY to search a library of medical research passages. It is
never shown to the patient and never used to write an answer.

Given the earlier turns and the patient's newest question, resolve anything that
depends on the conversation — pronouns ("it", "that", "those"), and elision
("what about the side effects?") — into an explicit, self-contained question.

RULES:
- Preserve the patient's intent exactly. Do not broaden the question, do not
  narrow it, and do not answer it.
- Add ONLY context that is present in the earlier turns. Never introduce a
  condition, drug or symptom that does not appear above.
- Keep it short: one question, comparable in length to the original.
- If the newest question already stands on its own, return it UNCHANGED,
  character for character.

Return a JSON object matching the supplied schema.
"""

# Hand-written rather than derived from a pydantic model, which is the pattern
# elsewhere in this package. The response is one string field, and this module is
# forbidden from raising: a validation layer would convert one fail-open path
# into another fail-open path and buy nothing. The shape is written to satisfy
# strict schema-constrained decoding directly (closed object, field required).
_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "standalone_query": {
            "type": "string",
            "description": "The newest question, made self-contained. Unchanged if already so.",
        },
    },
    "required": ["standalone_query"],
    "additionalProperties": False,
}

# Per-field caps on the history block. A prior answer summary is model-authored
# and effectively unbounded; without a cap, a long one would dominate the prompt
# and push the actual question to the end of the context.
_MAX_HISTORY_QUERY_CHARS = 300
_MAX_HISTORY_SUMMARY_CHARS = 400

# Length budget for an accepted rewrite. The multiplier is the real rule; the
# floor exists because a legitimate resolution of a very short follow-up is
# necessarily much longer than it ("is that serious?" -> "is chest pain during
# exercise serious?"), and a bare 3x rule would reject exactly the cases this
# module exists to fix. The ceiling is an absolute backstop against a runaway
# generation regardless of input size.
_REWRITE_LENGTH_MULTIPLIER = 3
_MIN_REWRITE_BUDGET_CHARS = 160
_MAX_REWRITE_CHARS = 600


@dataclass(frozen=True)
class TurnSummary:
    """One earlier turn, reduced to the two strings a rewrite prompt needs.

    Frozen, and deliberately anaemic. The caller builds these from
    ``outcome.presentation.answer.summary``, which means the narrowing — and the
    decision about which turns are even eligible — happens in the caller, on the
    main thread, where the typed objects live.

    A caller should not build a ``TurnSummary`` for a turn that failed or that
    was escalated as an emergency: a failure has no answer, and an escalation was
    produced without retrieval or generation, so neither carries context that
    should steer a search.
    """

    query: str
    answer_summary: str


def _clean(value: object, limit: int) -> str:
    """Reduce an untrusted value to a bounded single-line string.

    Returns ``""`` for anything that is not usable, including a non-string that
    arrived through an un-annotated call site. Collapsing whitespace matters
    because the history block is newline-delimited, and an embedded newline in a
    summary would forge a record boundary in the prompt.
    """
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())  # Also strips, and flattens any newline
    return collapsed[:limit]


def _usable_history(history: Sequence[TurnSummary], max_turns: int) -> list[TurnSummary]:
    """Take at most the last ``max_turns`` well-formed summaries.

    Every step is defensive: ``history`` may be any sequence, iterating it may
    raise, and its items may be anything at all. Items that are not
    ``TurnSummary`` instances are dropped rather than duck-typed — the type is
    the boundary this module is enforcing, so an object that merely happens to
    have the right attribute names (a ``TurnOutcome`` wrapper, say) must not slip
    through.
    """
    if max_turns <= 0:
        return []
    try:
        recent = list(history)[-max_turns:]
    except Exception as exc:  # A hostile or exhausted sequence must not propagate
        logger.warning("history was not iterable", extra={"error": type(exc).__name__})
        return []

    usable: list[TurnSummary] = []
    for item in recent:
        if not isinstance(item, TurnSummary):
            continue
        query = _clean(item.query, _MAX_HISTORY_QUERY_CHARS)
        summary = _clean(item.answer_summary, _MAX_HISTORY_SUMMARY_CHARS)
        if not query and not summary:
            continue  # A turn with neither half carries no context to resolve against
        usable.append(TurnSummary(query=query, answer_summary=summary))
    return usable


def build_user_prompt(query: str, history: Sequence[TurnSummary]) -> str:
    """Assemble the earlier turns and the newest question.

    The turns are numbered oldest-first so "the previous answer" has an
    unambiguous referent, and the newest question comes last so it is the final
    thing the model reads.
    """
    blocks = []
    for position, turn in enumerate(history, start=1):
        lines = [f"Turn {position}:", f"patient asked: {turn.query}"]
        if turn.answer_summary:
            # Omitted, not left blank, when a turn carries no summary — an
            # abstention contributes none. A dangling "answer covered:" is an
            # invitation to fill the gap, and the turn's value here is the
            # question the patient asked, which is on the line above.
            lines.append(f"answer covered: {turn.answer_summary}")
        blocks.append("\n".join(lines))
    earlier = "\n\n".join(blocks) if blocks else "(no earlier turns)"
    return f"EARLIER TURNS:\n\n{earlier}\n\n---\n\nNEWEST QUESTION: {query}"


def parse_rewrite(raw: str) -> str:
    """Pull ``standalone_query`` out of a model response.

    Returns ``""`` for anything unusable — invalid JSON, a non-object, a missing
    or non-string field. Unlike ``synapse.answer.generate.parse_answer`` this
    does not raise, because there is no caller that could do anything with the
    exception except fall back to the original query, which is what ``""``
    already means here.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if text.startswith("```"):  # Models fence JSON despite instructions
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        payload = json.loads(text)  # SAFE deserialisation
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return _clean(payload.get("standalone_query"), _MAX_REWRITE_CHARS)


def _length_budget(query: str) -> int:
    """The longest rewrite that will be accepted for this query. See the constants."""
    return min(
        max(_REWRITE_LENGTH_MULTIPLIER * len(query), _MIN_REWRITE_BUDGET_CHARS),
        _MAX_REWRITE_CHARS,
    )


def _tokens(text: str) -> set[str]:
    """Lowercased word-ish tokens, for the overlap check below."""
    return {token for token in text.casefold().split() if token.isalnum()}


def _is_acceptable(rewrite: str, query: str, history: Sequence[TurnSummary]) -> bool:
    """Decide whether a rewrite is safe to search with.

    Four rejections, each a fail-open:

    * Empty, or carrying no alphanumeric content at all — a query of punctuation
      retrieves noise.
    * Longer than the budget: a rewrite that ballooned is a model that started
      answering the question, or restating the passages, instead of resolving a
      pronoun.
    * Sharing no token with either the question or the earlier turns. A rewrite
      is supposed to be the question *plus* recovered context; one with nothing
      in common with either is a generation that went somewhere of its own, and
      searching on it would silently swap the patient's question for another.
    """
    if not rewrite or not any(character.isalnum() for character in rewrite):
        return False
    if len(rewrite) > _length_budget(query):
        return False

    grounded = _tokens(query)
    for turn in history:
        grounded |= _tokens(turn.query) | _tokens(turn.answer_summary)
    return bool(_tokens(rewrite) & grounded)


def _same_query(rewrite: str, query: str) -> bool:
    """True when the model returned the question it was given.

    Compared with whitespace collapsed and case folded, so a model that
    round-trips the query through its own formatting is still recognised as
    having changed nothing. The system prompt asks for an unchanged return when
    the question already stands alone, so this is an expected, successful path —
    not a failure — and it reports ``was_rewritten=False``.
    """
    return " ".join(rewrite.casefold().split()) == " ".join(query.casefold().split())


def rewrite_query(
    query: str,
    history: Sequence[TurnSummary],
    client: GenerationClient,
    *,
    max_turns: int = 3,
) -> tuple[str, bool]:
    """Resolve a follow-up into a standalone query for retrieval.

    Returns ``(query_for_retrieval, was_rewritten)``. When ``was_rewritten`` is
    False the first element is the caller's ``query`` object unchanged, so a
    caller may pass the result straight to retrieval without checking the flag.

    **Never raises.** Empty or malformed history, a client that raises or hangs,
    a response that is not JSON, an empty or nonsensical rewrite, and a rewrite
    that outgrew its length budget all return ``(query, False)``. The flag exists
    so a caller can record *that* a rewrite happened — a boolean is safe to log;
    the query text is not.

    Args:
        query: the patient's newest question, verbatim.
        history: earlier turns, oldest first. Only the last ``max_turns``
            well-formed entries are used. Failed and escalated turns should be
            excluded by the caller.
        client: any object satisfying
            :class:`synapse.answer.generate.GenerationClient` — the same protocol
            the answer and rerank layers take, so a caller reuses its existing
            client and a test passes a three-line fake. Bounding the call is the
            client's responsibility; a client configured with a timeout makes a
            hang one more fail-open path here.
        max_turns: how many earlier turns to consider. More context resolves more
            references, and also gives the model more to drift toward.
    """
    if not isinstance(query, str) or not query.strip():
        return query, False  # Nothing to resolve; the caller's submit guard should catch this

    usable = _usable_history(history, max_turns)
    if not usable:
        # The first question of a session, or a history with nothing well-formed
        # in it. Either way there is no context to resolve against, and calling
        # the provider to learn that would spend the patient's latency budget.
        return query, False

    try:
        raw = client.generate_structured(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(query, usable),
            schema=_RESPONSE_SCHEMA,
        )
    except Exception as exc:  # Broad on purpose: every provider fault degrades to no rewrite
        logger.warning(
            "query rewrite unavailable",
            extra={"error": type(exc).__name__, "prompt_id": PROMPT_ID},
        )
        return query, False

    rewrite = parse_rewrite(raw)
    if not _is_acceptable(rewrite, query, usable):
        logger.info(
            "query rewrite rejected",
            extra={
                # Lengths and counts only. The query and the rewrite are never logged.
                "prompt_id": PROMPT_ID,
                "rewrite_chars": len(rewrite),
                "budget_chars": _length_budget(query),
                "history_turns": len(usable),
            },
        )
        return query, False

    if _same_query(rewrite, query):
        # The model judged the question already standalone. Return the caller's
        # own string rather than the model's copy of it, so nothing downstream
        # sees whitespace the patient did not type.
        return query, False

    logger.info(
        "query rewritten for retrieval",
        extra={"prompt_id": PROMPT_ID, "history_turns": len(usable)},
    )
    return rewrite, True


__all__ = [
    "PROMPT_ID",
    "SYSTEM_PROMPT",
    "TurnSummary",
    "build_user_prompt",
    "parse_rewrite",
    "rewrite_query",
]


# ---------------------------------------------------------------------------
# Validation (offline — no API key, no network)
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    class FakeClient:
        """A canned GenerationClient. Satisfies the protocol structurally."""

        def __init__(self, reply: str) -> None:
            self.reply = reply
            self.calls = 0

        def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
            self.calls += 1
            return self.reply

    class RaisingClient:
        """A client that fails the way an unreachable provider does."""

        def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
            raise RuntimeError("provider unreachable")

    HISTORY = [
        TurnSummary(
            query="what is metformin?",
            answer_summary="Metformin is a first-line oral medicine for type 2 diabetes.",
        ),
    ]

    failures = 0

    def check(label: str, got: object, want: object) -> None:
        global failures
        if got == want:
            print(f"  ok   {label}")
        else:
            failures += 1
            print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")

    print("1. a standalone query is returned unchanged")
    standalone = "what is metformin?"
    client = FakeClient(json.dumps({"standalone_query": standalone}))
    check("passthrough", rewrite_query(standalone, HISTORY, client), (standalone, False))

    print("\n2. a follow-up is resolved")
    followup = "what about the side effects?"
    resolved = "what are the side effects of metformin?"
    client = FakeClient(json.dumps({"standalone_query": resolved}))
    check("resolved", rewrite_query(followup, HISTORY, client), (resolved, True))

    print("\n3. a raising client returns the original")
    check("fail open", rewrite_query(followup, HISTORY, RaisingClient()), (followup, False))

    print("\n4. empty history returns the original without calling the provider")
    client = FakeClient(json.dumps({"standalone_query": resolved}))
    check("empty history", rewrite_query(followup, [], client), (followup, False))
    check("provider not called", client.calls, 0)

    print("\n5. the other fail-open paths")
    check(
        "malformed history",
        rewrite_query(followup, ["not a TurnSummary", None, 42], FakeClient("")),  # type: ignore[list-item]
        (followup, False),
    )
    check(
        "not JSON",
        rewrite_query(followup, HISTORY, FakeClient("I think you meant metformin.")),
        (followup, False),
    )
    check(
        "not an object",
        rewrite_query(followup, HISTORY, FakeClient("[1, 2, 3]")),
        (followup, False),
    )
    check(
        "missing field",
        rewrite_query(followup, HISTORY, FakeClient(json.dumps({"other": "x"}))),
        (followup, False),
    )
    check(
        "empty rewrite",
        rewrite_query(followup, HISTORY, FakeClient(json.dumps({"standalone_query": "   "}))),
        (followup, False),
    )
    check(
        "over the length budget",
        rewrite_query(
            followup,
            HISTORY,
            FakeClient(json.dumps({"standalone_query": "metformin " * 40})),
        ),
        (followup, False),
    )
    check(
        "unrelated rewrite",
        rewrite_query(
            followup,
            HISTORY,
            FakeClient(json.dumps({"standalone_query": "ferry timetable Ullapool Stornoway"})),
        ),
        (followup, False),
    )
    check(
        "max_turns=0",
        rewrite_query(
            followup, HISTORY, FakeClient(json.dumps({"standalone_query": resolved})), max_turns=0
        ),
        (followup, False),
    )
    check("blank query", rewrite_query("   ", HISTORY, FakeClient("")), ("   ", False))
    check(
        "fenced JSON is still parsed",
        rewrite_query(
            followup,
            HISTORY,
            FakeClient('```json\n{"standalone_query": "' + resolved + '"}\n```'),
        ),
        (resolved, True),
    )

    print("\n6. only the last max_turns summaries reach the prompt")
    many = [TurnSummary(query=f"q{n}", answer_summary=f"a{n}") for n in range(6)]
    prompt = build_user_prompt("newest", _usable_history(many, 3))
    check("oldest dropped", "q0" in prompt, False)
    check("newest kept", "q5" in prompt, True)

    print("\n7. an embedded newline cannot forge a turn boundary")
    check("newline collapsed", _clean("a\nb", 50), "a b")

    print("\n8. a turn with no summary keeps its question and drops the empty line")
    # An abstained turn reaches here with answer_summary="" (app.py strips the
    # fixed abstention boilerplate). The question must survive: it is what
    # resolves the follow-up.
    no_summary = [TurnSummary(query="what is metformin?", answer_summary="")]
    prompt_ns = build_user_prompt("what about the side effects?", no_summary)
    check("question kept", "patient asked: what is metformin?" in prompt_ns, True)
    check("no dangling label", "answer covered:" in prompt_ns, False)
    check("turn survives the usable filter", len(_usable_history(no_summary, 3)), 1)
    check(
        "and it still resolves",
        rewrite_query(
            "what about the side effects?",
            no_summary,
            FakeClient(json.dumps({"standalone_query": "side effects of metformin"})),
        ),
        ("side effects of metformin", True),
    )
    check(
        "a turn with neither half is dropped",
        len(_usable_history([TurnSummary(query="", answer_summary="")], 3)),
        0,
    )

    if failures:
        print(f"\n{failures} check(s) FAILED")
        raise SystemExit(1)
    print("\nAll checks passed. No API key, no network, nothing written.")

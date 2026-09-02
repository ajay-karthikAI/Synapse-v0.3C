"""
synapse.memory
==============
Conversation context, used to make a follow-up question searchable.

Every turn in this system is otherwise independent: ``answer_turn`` receives one
string, and ``st.session_state.conversation`` is read only by the renderer. So a
follow-up like "what about the side effects?" reached BM25 and FAISS as six
context-free words and retrieved noise — measurably so. On the corpus in this
repository, "how is it treated?" alone returns shared decision-making, bipolar
antidepressants and intra-operative glucose measurement; resolved against the
previous turn it returns the paper the patient was actually reading.

Scope, and the line this package does not cross
-----------------------------------------------
What lives here builds a *prompt string* from earlier questions. That is all. It
holds no state, writes nothing, and reaches no further into the pipeline than
the two callers it serves:

* ``rewrite_query`` resolves a follow-up into a standalone RETRIEVAL query. The
  patient's raw words still go to generation, so nothing a rewrite produces is
  ever shown or ever cited.
* ``synapse.answer.generate.build_user_prompt`` takes earlier questions as
  fenced, explicitly non-citable context (prompt ``grounded-answer-v3``).

Neither path widens what counts as evidence. Verification still runs against the
retrieved passages alone, so a claim quoting the conversation cites a source that
was never retrieved and is withheld like any other unsupported claim. The prompt
fence is politeness; the verifier is the guarantee.

Prior *questions* travel, prior *answers* do not. A question is what
disambiguates "it" and "those". Prior answer prose is what would tempt a model to
quote something that is not in the passages, and that costs the claim.

This package must not import Streamlit or read session state: ``rewrite_query``
runs on a worker thread with no ``ScriptRunContext``, where both are unavailable.
"""

from synapse.memory.query_rewrite import (  # Re-exported so callers need one import
    PROMPT_ID,
    SYSTEM_PROMPT,
    TurnSummary,
    build_user_prompt,
    parse_rewrite,
    rewrite_query,
)

__all__ = [
    "PROMPT_ID",
    "SYSTEM_PROMPT",
    "TurnSummary",
    "build_user_prompt",
    "parse_rewrite",
    "rewrite_query",
]

"""
Generation.answer_generator
===========================
**DEPRECATED SHIM.** Emergency routing constants only.

Scheduled for removal on **2026-11-30**, with the rest of the legacy tree
(docs/answer-rendering.md §7).

What used to be here, and why it is gone
----------------------------------------
This module held the free-form answer generator: a prompt demanding a six-part
structure marked with emoji headings —

    📋 WHAT THE RESEARCH SAYS
    🔬 WHAT YOUR DOCTOR WILL EVALUATE
    ❓ QUESTIONS TO ASK YOUR DOCTOR TODAY

— which the interface then recovered with three regexes. The failure modes are
documented in docs/citation-integrity.md §2: a reordered or renamed heading
silently dropped a section, an 800-token cap truncated the tail (taking the
disclaimer with it), and the `[Source N]` markers in the prose resolved to
nothing at all.

That generator was **removed on 2026-08-20**. Answers are now produced by
`synapse.answer`, which asks the provider for a JSON object matching a schema
derived from `GroundedAnswer`, verifies every quote against the chunk it claims
to come from, withholds unsupported claims, and renders from typed fields.

Keeping the old generator alongside the new one would mean two answer pipelines,
one of which nothing checks — so it is deleted rather than deprecated in place.

What remains
------------
`EMERGENCY_RESPONSE` and `check_emergency`, because red-flag routing is the one
behaviour that must not change while the rest moves. Both are thin: the
detection logic itself lives in `synapse.safety`, and the patient-facing
escalation text used by the current interface is
`synapse.answer.render.EMERGENCY_MESSAGE`.
"""

# EMERGENCY_SIGNALS was REMOVED 2026-08-20 and the detection moved to
# synapse.safety. The old list was 24 fixed phrases matched as substrings, which
# measured 2/6 on real emergencies: it missed stroke ("speech is slurred"),
# respiratory distress ("struggling to breathe"), haemoptysis ("coughing UP
# blood" — one word off "coughing blood") and overdose ("took FAR too many").
# It also escalated denials: "i do not have any chest pain" matched "chest pain".
#
# The vocabulary now lives in config/emergency_vocabulary.toml, where a clinician
# can review it, and the matcher does stem-proximity matching with negation
# scoping. See synapse/safety/detector.py for the full before/after.

EMERGENCY_RESPONSE = """I want to make sure you're safe right now.

Some of the symptoms you've described can sometimes require immediate medical attention. Please let the front desk or a nurse know how you're feeling right now — don't wait for your scheduled appointment.

If you feel you need immediate help:
  → Tell the front desk immediately
  → Call 911 if you feel you are in danger
  → Go to the nearest emergency room

This app is for general health education and cannot assess your current condition. Please speak with medical staff right away."""


def check_emergency(query: str) -> bool:
    """Returns True if the query describes a possible emergency.

    Thin delegation to synapse.safety, kept so existing callers need no change.
    The real implementation is typed, linted and tested; this legacy module is
    none of those.

    The import is function-local on purpose: importing synapse at module scope
    would make the legacy tree's import-time side effects a dependency of the
    safety layer, and the whole point is that the safety layer stands alone.
    """
    from synapse.safety import load_detector  # Local: see docstring

    return load_detector().is_emergency(query)

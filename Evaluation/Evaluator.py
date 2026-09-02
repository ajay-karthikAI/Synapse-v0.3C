"""
Evaluation.Evaluator — RETIRED
==============================
This module has been replaced by the ``synapse.evals`` harness.

It is retained only as a pointer. The original implementation had three defects
that made its output meaningless, all fixed in the replacement:

1. It scored live user queries against an empty ground-truth list, so every
   metric it reported was structurally zero regardless of retrieval quality.
2. It returned ``0.0`` for undefined metrics, making "no labels" indistinguishable
   from "retrieval found nothing".
3. Its built-in evaluation set referenced four PMIDs that are absent from the
   shipped corpus, so recall against it could never exceed zero.

It also wrote raw patient query text to ``eval_log.json`` in the working
directory. That behaviour is gone: nothing here writes anything.

Use instead::

    python -m synapse.evals.run --dataset <dir> --fixture <file> --output <dir> --offline

See docs/evaluation-metrics.md.
"""

from __future__ import annotations

RETIRED_MESSAGE = (
    "Evaluation.Evaluator has been retired. Use synapse.evals: "
    "python -m synapse.evals.run --help. See docs/evaluation-metrics.md."
)


def __getattr__(name: str) -> object:  # Module-level __getattr__ (PEP 562)
    """Raise a pointer to the replacement for any attribute access.

    Deliberately an error rather than a warning: the old API produced numbers
    that looked plausible and were not, so silently continuing would be worse
    than failing.
    """
    raise AttributeError(f"{RETIRED_MESSAGE} (attempted to access '{name}')")

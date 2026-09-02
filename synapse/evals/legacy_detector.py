"""
synapse.evals.legacy_detector
=============================
Supplies the harness with the emergency detector the application actually uses.

History, because it explains the shape of this file
---------------------------------------------------
Until 2026-08-20 emergency detection lived in ``Generation/answer_generator.py``
— the legacy tree, excluded from lint, typing and tests. The harness could not
import it (that module pulls in ``openai`` and mutates ``sys.path`` at import
time), so this module read the ``EMERGENCY_SIGNALS`` constant out of the source
with ``ast.literal_eval`` and reimplemented the two-line match, guarded by a
check that the real function still had the shape being reimplemented.

That guard did its job: when detection moved to :mod:`synapse.safety`, loading
started raising instead of silently scoring a stale copy of a rule that no
longer existed.

None of that is needed now. The detector is a typed, tested module in
``synapse/``, so the harness simply calls it. The indirection remains only so
that call sites and tests referring to ``load_emergency_detector`` keep working.
"""

from __future__ import annotations  # Postponed annotations

from collections.abc import Callable
from pathlib import Path

from synapse.safety import load_detector


def load_emergency_detector(source: Path | None = None) -> Callable[[str], bool]:
    """Return the shipped emergency predicate.

    ``source`` optionally points at an alternative vocabulary file, which is how
    tests exercise the detector without depending on repository state.
    """
    return load_detector(source).is_emergency

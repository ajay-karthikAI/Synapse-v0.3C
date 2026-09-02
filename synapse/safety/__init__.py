"""
synapse.safety
==============
Pre-retrieval safety checks. Currently: emergency escalation detection.

Migrated out of ``Generation/answer_generator.py`` during the 2026-08-20 fix.
The old implementation lowercased the query and substring-matched a 24-string
list, which measured **2/6** on real emergencies — it missed stroke, respiratory
distress, haemoptysis and overdose — and **2/6** on negation, escalating
"i do not have any chest pain".

That code sat in the legacy tree, excluded from lint, type-checking and the test
suite: the only code a patient's query touched was the least-checked code in the
repository. It lives here now so it is typed, linted, tested and reviewable.

Nothing here is clinically validated. See ``config/emergency_vocabulary.toml``.
"""

from synapse.safety.detector import (  # Re-exported so callers need one import
    EmergencyDetector,
    EmergencyMatch,
    load_detector,
)
from synapse.safety.vocabulary import EmergencyVocabulary, VocabularyError

__all__ = [
    "EmergencyDetector",
    "EmergencyMatch",
    "EmergencyVocabulary",
    "VocabularyError",
    "load_detector",
]

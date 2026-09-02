"""
synapse.ui
==========
The application seam: query in, renderable outcome out.

This package holds everything the patient-facing interface needs that is not
itself an answer concern, and nothing else:

    synapse.ui.pipeline         ordering, injection points, typed outcomes
    synapse.ui.errors           typed failure codes and the one patient message
    synapse.ui.legacy_evidence  DEPRECATED adapter for the legacy retrieval shape

The boundary is one-directional: ``synapse.ui`` imports ``synapse.answer``,
never the reverse. Rendering itself stays in :mod:`synapse.answer.render`, so
there is exactly one module that turns a validated answer into markup.

``app.py`` is presentation only — layout, styling and Streamlit calls. Anything
that decides *what* a patient sees belongs here or below.
"""

from __future__ import annotations  # Postponed annotations

from synapse.ui.errors import (
    PATIENT_ERROR_MESSAGE,
    AnswerFailure,
    AnswerFailureCode,
    classify,
)
from synapse.ui.pipeline import (
    AnswerPresentation,
    TurnOutcome,
    answer_turn,
    emergency_turn,
)

__all__ = [
    "PATIENT_ERROR_MESSAGE",
    "AnswerFailure",
    "AnswerFailureCode",
    "AnswerPresentation",
    "TurnOutcome",
    "answer_turn",
    "classify",
    "emergency_turn",
]

"""
synapse.a11y
============
Accessibility tooling: contrast arithmetic, the palette it governs, and
structural checks over rendered markup.

    synapse.a11y.contrast  WCAG ratio maths and the AA thresholds
    synapse.a11y.palette   every colour token with the surface it sits on
    synapse.a11y.audit     heading order, names, icons, live regions, tab order

All of it runs without a browser, without a network and in milliseconds, which
is what lets it gate every pull request. What it cannot see — focus order,
visible focus, zoom reflow, real colour rendering — is covered by the Playwright
suite, and the split is stated plainly in docs/accessibility.md.

A passing run here means the markup is well-formed for assistive technology. It
is not a conformance claim.
"""

from __future__ import annotations  # Postponed annotations

from synapse.a11y.audit import Finding, audit_fragment
from synapse.a11y.contrast import (
    AA_LARGE_TEXT,
    AA_NON_TEXT,
    AA_TEXT,
    ContrastResult,
    contrast_ratio,
)
from synapse.a11y.palette import TOKENS, Token, css_variables, failures, report

__all__ = [
    "AA_LARGE_TEXT",
    "AA_NON_TEXT",
    "AA_TEXT",
    "TOKENS",
    "ContrastResult",
    "Finding",
    "Token",
    "audit_fragment",
    "contrast_ratio",
    "css_variables",
    "failures",
    "report",
]

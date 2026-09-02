"""
synapse.escaping
================
The single HTML-escaping primitive, shared by every renderer.

Extracted from :mod:`synapse.answer.render` when a second renderer
(:mod:`synapse.evidence.render`) needed it. Two renderers importing an escaping
function from each other is a circular import; two renderers each defining their
own is worse — one of them eventually gets it subtly wrong, and the difference
shows up as an XSS hole rather than a test failure.

So there is exactly one function, in a module that imports nothing from Synapse
and therefore can be imported from anywhere.
"""

from __future__ import annotations  # Postponed annotations

import html  # The single escaping primitive; stdlib, no dependency


def escape(value: object) -> str:
    """HTML-escape any value for safe interpolation.

    ``quote=True`` so the result is safe inside an attribute as well as in text
    content — a caller should not have to know which context they are in.
    """
    return html.escape(str(value), quote=True)


__all__ = ["escape"]

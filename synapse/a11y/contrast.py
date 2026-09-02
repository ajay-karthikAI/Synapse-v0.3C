"""
synapse.a11y.contrast
=====================
WCAG contrast arithmetic, and the thresholds it is measured against.

Pure functions over hex colours. No browser, no rendering, no dependency: the
contrast of two colours is defined by a formula in the WCAG specification, so it
can be computed exactly and checked in CI on any machine. This is the one part
of accessibility testing that automates *reliably* — a computed ratio is not a
heuristic, and it does not vary with platform or font stack.

What this cannot tell you: whether the two colours are ever actually adjacent.
That is what the pairings table in :mod:`synapse.a11y.palette` is for, and why
the palette declares the background each token is used on rather than leaving a
test to guess.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass

# WCAG 2.2 AA thresholds.
AA_TEXT = 4.5  # Body text below 18pt (or 14pt bold)
AA_LARGE_TEXT = 3.0  # Text at 18pt+, or 14pt+ bold
AA_NON_TEXT = 3.0  # 1.4.11: UI component boundaries, focus indicators, meaningful graphics
AAA_TEXT = 7.0  # Recorded for reference; not a target of this work


def _channel(value: int) -> float:
    """Linearise one sRGB channel, per the WCAG definition."""
    fraction = value / 255
    return fraction / 12.92 if fraction <= 0.04045 else ((fraction + 0.055) / 1.055) ** 2.4


def parse_hex(colour: str) -> tuple[int, int, int]:
    """Parse ``#rgb`` or ``#rrggbb`` into channel values."""
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ValueError(f"not a hex colour: {colour!r}")
    return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def relative_luminance(colour: str) -> float:
    """Relative luminance of a colour, 0.0 (black) to 1.0 (white)."""
    red, green, blue = parse_hex(colour)
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    """Contrast ratio between two colours, from 1.0 to 21.0.

    Order-independent, as the specification defines it: the lighter colour is
    always the numerator.
    """
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


@dataclass(frozen=True)
class ContrastResult:
    """One measured pairing."""

    name: str
    foreground: str
    background: str
    required: float
    ratio: float

    @property
    def passes(self) -> bool:
        """True when the pairing meets its requirement.

        Rounded to two decimals before comparison, because a ratio of 4.4996
        displays as 4.50 and failing a check on a difference nobody can see
        produces a fix nobody can verify.
        """
        return round(self.ratio, 2) >= self.required

    def describe(self) -> str:
        """One line, for a report or a failure message."""
        verdict = "PASS" if self.passes else "FAIL"
        return (
            f"{verdict} {self.name}: {self.foreground} on {self.background} "
            f"= {self.ratio:.2f}:1 (needs {self.required}:1)"
        )


def check(name: str, foreground: str, background: str, required: float) -> ContrastResult:
    """Measure one pairing."""
    return ContrastResult(
        name=name,
        foreground=foreground,
        background=background,
        required=required,
        ratio=contrast_ratio(foreground, background),
    )


__all__ = [
    "AAA_TEXT",
    "AA_LARGE_TEXT",
    "AA_NON_TEXT",
    "AA_TEXT",
    "ContrastResult",
    "check",
    "contrast_ratio",
    "parse_hex",
    "relative_luminance",
]

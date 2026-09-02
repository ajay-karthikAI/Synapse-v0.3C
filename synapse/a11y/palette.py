"""
synapse.a11y.palette
====================
The interface palette, as tokens with the background each is used on.

Why the background is part of the token
---------------------------------------
A colour has no contrast on its own. ``#5a5a7a`` is fine on white and fails
badly on the near-black this interface uses. So every token here declares the
surface it appears on and the WCAG requirement it must meet, and
``tests/test_accessibility.py`` checks the whole table. A token nobody can pair
with a background is a token nobody can verify.

That also makes this file the single source of truth: :func:`css_variables`
emits the ``:root`` block the application uses, so the CSS cannot drift from the
values the test measured. Changing a colour in one place changes both.

The audit that produced this
----------------------------
Eight pairings in the original palette failed AA, all of them low-contrast greys
and borders that read as "subtle" on a designer's monitor and as "invisible" to
a reader with low vision:

    src-sub          #4a4a6a on #0a0a18   2.32:1  (needed 4.5)
    disclaimer       #5a5a7a on #05030d   3.10:1  (needed 4.5)
    perm disclaimer  #6a6a8a on #080814   3.83:1  (needed 4.5)
    empty hint       #68688a on #05030d   3.84:1  (needed 4.5)
    chunk id         #4a4a6a on #0b0b1a   2.30:1  (needed 4.5)
    card border      #1c1c30 on #0b0b1a   1.17:1  (needed 3.0)
    summary border   #241a4d on #0a0a18   1.24:1  (needed 3.0)
    button border    #6d28d9 on #05030d   2.88:1  (needed 3.0)

Each was raised to clear its threshold while staying recognisably the same
design. The permanent disclaimer moved furthest on purpose: it is the one piece
of text on the page that must never be skimmed past because it was too faint.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass

from synapse.a11y.contrast import AA_NON_TEXT, AA_TEXT, ContrastResult, check

# Surfaces. Named separately because several tokens share them, and a surface
# that changes must move every token measured against it.
SURFACE_PAGE = "#05030d"  # The page behind everything
SURFACE_CARD = "#0b0b1a"  # Standard response card
SURFACE_SUNKEN = "#0a0a18"  # Source rows, evidence summary
SURFACE_INSET = "#0d0d1f"  # Quotes and label chips
SURFACE_DEEP = "#080814"  # Permanent disclaimer, evidence card wells
SURFACE_USER = "#12062a"  # The patient's own message bubble
SURFACE_EMERGENCY = "#140505"
SURFACE_STAFF = "#14100a"


@dataclass(frozen=True)
class Token:
    """One colour, the surface it sits on, and what it has to clear."""

    name: str  # CSS custom property name, without the leading --
    value: str
    on: str  # Surface it is used against
    required: float
    role: str  # text | border | focus | icon
    note: str = ""

    def measure(self) -> ContrastResult:
        """Check this token against its declared surface."""
        return check(self.name, self.value, self.on, self.required)


# The full table. Every value here is used by app.py through var(--name).
TOKENS: tuple[Token, ...] = (
    # -- text ---------------------------------------------------------------
    Token("text-primary", "#e8e8e8", SURFACE_PAGE, AA_TEXT, "text", "Body copy"),
    Token("text-body", "#b9b9c6", SURFACE_CARD, AA_TEXT, "text", "Card body and claims"),
    Token(
        "text-muted",
        "#9a9ab8",
        SURFACE_CARD,
        AA_TEXT,
        "text",
        "Secondary detail. Raised from #4a4a6a (2.32:1), which was unreadable.",
    ),
    Token(
        "text-muted-deep",
        "#9a9ab8",
        SURFACE_SUNKEN,
        AA_TEXT,
        "text",
        "Same token on the sunken surface, measured separately.",
    ),
    Token(
        "text-disclaimer",
        "#b0b0c8",
        SURFACE_DEEP,
        AA_TEXT,
        "text",
        "The permanent disclaimer. Raised furthest: it must never be too faint to read.",
    ),
    Token(
        "text-footer",
        "#a8a8c0",
        SURFACE_PAGE,
        AA_TEXT,
        "text",
        "Footer disclaimer and empty-state hint. Raised from #5a5a7a (3.10:1).",
    ),
    Token("text-question", "#c3b5e0", SURFACE_CARD, AA_TEXT, "text", "Question list items"),
    Token("text-user", "#c4b5fd", SURFACE_USER, AA_TEXT, "text", "The patient's own words"),
    Token("text-accent", "#a78bfa", SURFACE_CARD, AA_TEXT, "text", "Citation markers and links"),
    Token(
        "text-caution",
        "#e0b45f",
        SURFACE_CARD,
        AA_TEXT,
        "text",
        "Partly-verified flag and caution labels. Never the only signal; text says it too.",
    ),
    Token("text-emergency", "#fca5a5", SURFACE_EMERGENCY, AA_TEXT, "text", "Emergency body"),
    Token(
        "text-emergency-title", "#f87171", SURFACE_EMERGENCY, AA_TEXT, "text", "Emergency heading"
    ),
    Token("text-staff", "#fcd34d", SURFACE_STAFF, AA_TEXT, "text", "Clinical-staff routing"),
    Token("text-quote", "#b6b6c8", SURFACE_INSET, AA_TEXT, "text", "Verbatim excerpts"),
    Token("text-label", "#c9bbff", SURFACE_INSET, AA_TEXT, "text", "Evidence label names"),
    Token("text-label-why", "#a6a6c2", SURFACE_INSET, AA_TEXT, "text", "Label explanations"),
    Token(
        "text-section",
        "#a0a0bd",
        SURFACE_CARD,
        AA_TEXT,
        "text",
        "Uppercase section labels. Small, so held to the body threshold, not the large one.",
    ),
    # -- borders and controls (1.4.11 non-text contrast) --------------------
    Token(
        "border-card",
        "#62628c",
        SURFACE_CARD,
        AA_NON_TEXT,
        "border",
        "Card edge. Raised from #1c1c30 (1.17:1). A first attempt at #3a3a5c still "
        "only reached 1.80:1, which is why this table is checked rather than eyeballed.",
    ),
    Token(
        "border-control",
        "#6b5bb5",
        SURFACE_SUNKEN,
        AA_NON_TEXT,
        "border",
        "Interactive boundaries: the evidence disclosure, links. Raised from #241a4d (1.24:1).",
    ),
    Token(
        "border-button",
        "#8b5cf6",
        SURFACE_PAGE,
        AA_NON_TEXT,
        "border",
        "Primary button edge. Raised from #6d28d9 (2.88:1).",
    ),
    Token(
        "border-emergency", "#ef4444", SURFACE_EMERGENCY, AA_NON_TEXT, "border", "Emergency card"
    ),
    # -- focus --------------------------------------------------------------
    Token(
        "focus-ring",
        "#c4b5fd",
        SURFACE_PAGE,
        AA_NON_TEXT,
        "focus",
        "Focus indicator against the page.",
    ),
    Token(
        "focus-ring-card",
        "#c4b5fd",
        SURFACE_CARD,
        AA_NON_TEXT,
        "focus",
        "The same ring against a card, measured separately.",
    ),
)


def token(name: str) -> Token:
    """Look up one token by name."""
    for entry in TOKENS:
        if entry.name == name:
            return entry
    raise KeyError(f"no such palette token: {name}")


def css_variables(indent: str = "  ") -> str:
    """The ``:root`` custom properties the application declares.

    Generated from :data:`TOKENS`, so the stylesheet and the contrast test read
    the same numbers. A colour changed here changes both.
    """
    lines = [":root {"]
    for entry in sorted(TOKENS, key=lambda item: item.name):
        comment = f"  /* {entry.note} */" if entry.note else ""
        lines.append(f"{indent}--{entry.name}: {entry.value};{comment}")
    # Surfaces are emitted too, so a rule can pair a token with the surface it
    # was measured against rather than a hand-copied hex.
    for name, value in (
        ("surface-page", SURFACE_PAGE),
        ("surface-card", SURFACE_CARD),
        ("surface-sunken", SURFACE_SUNKEN),
        ("surface-inset", SURFACE_INSET),
        ("surface-deep", SURFACE_DEEP),
        ("surface-user", SURFACE_USER),
        ("surface-emergency", SURFACE_EMERGENCY),
        ("surface-staff", SURFACE_STAFF),
    ):
        lines.append(f"{indent}--{name}: {value};")
    lines.append("}")
    return "\n".join(lines)


def failures() -> list[ContrastResult]:
    """Every token that does not meet its requirement. Empty is the goal."""
    return [result for result in (entry.measure() for entry in TOKENS) if not result.passes]


def report() -> str:
    """A readable table of the whole palette, for docs and CI output."""
    lines = [f"{'token':<22} {'ratio':>7}  need  role"]
    for entry in sorted(TOKENS, key=lambda item: item.name):
        result = entry.measure()
        verdict = "" if result.passes else "  <-- FAILS"
        lines.append(
            f"{entry.name:<22} {result.ratio:>6.2f}:1  {entry.required:>4}  {entry.role}{verdict}"
        )
    return "\n".join(lines)


__all__ = [
    "SURFACE_CARD",
    "SURFACE_DEEP",
    "SURFACE_EMERGENCY",
    "SURFACE_INSET",
    "SURFACE_PAGE",
    "SURFACE_STAFF",
    "SURFACE_SUNKEN",
    "SURFACE_USER",
    "TOKENS",
    "Token",
    "css_variables",
    "failures",
    "report",
    "token",
]

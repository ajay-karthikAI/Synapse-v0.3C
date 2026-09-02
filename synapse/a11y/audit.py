"""
synapse.a11y.audit
==================
Structural accessibility checks over rendered markup, without a browser.

Most of what a screen reader encounters in Synapse is produced by
:mod:`synapse.answer.render`, :mod:`synapse.evidence.render` and
:mod:`synapse.brief.render_print` — plain strings of HTML. Those can be parsed
and checked with the standard library, which means the checks run in CI on any
machine, in milliseconds, with no browser, no network and no external service.

That covers heading order, accessible names, icon handling, live regions and
landmark structure. It does **not** cover anything that depends on layout or
computed style: focus order, visible focus, zoom reflow and colour rendering
need a real browser, and those live in the Playwright suite.

The split matters for honesty. A passing run here means the markup is
well-formed for assistive technology. It does not mean the interface is
accessible, and docs/accessibility.md says so explicitly.
"""

from __future__ import annotations  # Postponed annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Elements that are interactive by default and must be reachable and named.
INTERACTIVE_TAGS = frozenset({"a", "button", "summary", "input", "select", "textarea"})

# Elements whose only purpose is decoration when they carry no text.
DECORATIVE_HINTS = frozenset({"svg", "path", "polyline", "circle"})


@dataclass(frozen=True)
class Finding:
    """One accessibility problem found in markup."""

    rule: str
    detail: str
    element: str = ""

    def describe(self) -> str:
        """One line for a failure message."""
        return f"[{self.rule}] {self.detail}" + (f" ({self.element})" if self.element else "")


@dataclass
class _Collector(HTMLParser):
    """Gathers the structure the checks below need."""

    headings: list[tuple[int, str]] = field(default_factory=list)
    interactive: list[tuple[str, dict[str, str], str]] = field(default_factory=list)
    live_regions: list[dict[str, str]] = field(default_factory=list)
    landmarks: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    all_tags: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    _open_interactive: list[tuple[str, dict[str, str], int]] = field(default_factory=list)
    _open_heading: tuple[int, int] | None = None
    _text: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """HTMLParser needs its own initialiser run."""
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record an element and open any span whose text we need."""
        attributes = {key: (value or "") for key, value in attrs}
        self.all_tags.append((tag, attributes))
        if re.fullmatch(r"h[1-6]", tag):
            self._open_heading = (int(tag[1]), len(self._text))
        if tag in INTERACTIVE_TAGS:
            self._open_interactive.append((tag, attributes, len(self._text)))
        if attributes.get("aria-live") or attributes.get("role") in {"status", "alert", "log"}:
            self.live_regions.append(attributes)
        if attributes.get("role") in {"main", "banner", "navigation", "contentinfo", "region"}:
            self.landmarks.append(attributes["role"])
        if tag in {"main", "header", "footer", "nav", "aside"}:
            self.landmarks.append(tag)
        if tag == "img":
            self.images.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        """Close a heading or interactive element, capturing its text."""
        if re.fullmatch(r"h[1-6]", tag) and self._open_heading is not None:
            level, start = self._open_heading
            self.headings.append((level, "".join(self._text[start:]).strip()))
            self._open_heading = None
        if tag in INTERACTIVE_TAGS and self._open_interactive:
            open_tag, attributes, start = self._open_interactive.pop()
            if open_tag == tag:
                self.interactive.append((tag, attributes, "".join(self._text[start:]).strip()))

    def handle_data(self, data: str) -> None:
        """Accumulate text content."""
        self._text.append(data)


def _parse(html: str) -> _Collector:
    """Parse a fragment or a document."""
    collector = _Collector()
    collector.feed(html)
    collector.close()
    return collector


def check_heading_order(html: str, *, start_level: int = 0) -> list[Finding]:
    """Headings must not skip a level going down.

    A jump from ``h2`` to ``h4`` tells a screen-reader user that a level of
    structure exists which they cannot navigate to. Going back *up* any number
    of levels is fine: that is how sections end.

    ``start_level`` is the deepest heading already open **above** this fragment.
    A whole document starts at 0; an evidence card rendered inside a section
    headed by an ``h2`` starts at 2, so its ``h3`` is correct rather than a skip.
    Getting that parameter backwards made the checker flag correct markup, which
    is the failure mode that gets a checker switched off.
    """
    findings: list[Finding] = []
    previous = start_level
    for level, text in _parse(html).headings:
        if previous and level > previous + 1:
            findings.append(
                Finding(
                    "heading-order",
                    f"h{previous} is followed by h{level}, skipping a level",
                    text[:60],
                )
            )
        previous = level
    return findings


def check_accessible_names(html: str) -> list[Finding]:
    """Every interactive element needs a name assistive technology can read.

    Text content, ``aria-label`` or ``aria-labelledby``. An element whose only
    content is an icon is the usual offender.
    """
    findings: list[Finding] = []
    for tag, attributes, text in _parse(html).interactive:
        if attributes.get("aria-hidden") == "true":
            continue  # Hidden from assistive technology on purpose
        named = bool(
            text
            or attributes.get("aria-label")
            or attributes.get("aria-labelledby")
            or attributes.get("title")
            or attributes.get("alt")
        )
        if not named:
            findings.append(
                Finding("accessible-name", f"<{tag}> has no accessible name", str(attributes)[:70])
            )
    return findings


def check_icons_are_handled(html: str) -> list[Finding]:
    """A decorative glyph must be hidden; a meaningful one must be named.

    The failure this catches: a bare ``<span>↗</span>`` beside a link, which a
    screen reader announces as "north east arrow" in the middle of a sentence.
    """
    findings: list[Finding] = []
    # Any element carrying only symbol characters and no accessible name.
    for match in re.finditer(r"<span([^>]*)>([^<]*)</span>", html):
        attributes, content = match.group(1), match.group(2).strip()
        if not content:
            continue
        symbolic = content and all(
            not character.isalnum() and not character.isspace() for character in content
        )
        if not symbolic:
            continue
        if 'aria-hidden="true"' in attributes or "aria-label" in attributes:
            continue
        findings.append(Finding("icon-handling", f"symbol {content!r} is neither hidden nor named"))
    return findings


def check_live_regions(html: str, *, expect_polite: bool = True) -> list[Finding]:
    """Live regions must be polite unless the message is genuinely urgent.

    ``role="alert"`` and ``aria-live="assertive"`` interrupt whatever the user
    is reading. That is correct for emergency routing and wrong for a loading
    message, and getting it backwards makes an interface exhausting to use.
    """
    findings: list[Finding] = []
    for attributes in _parse(html).live_regions:
        politeness = attributes.get("aria-live") or (
            "assertive" if attributes.get("role") == "alert" else "polite"
        )
        if expect_polite and politeness == "assertive":
            findings.append(
                Finding(
                    "live-region", "assertive live region interrupts the user", str(attributes)[:70]
                )
            )
    return findings


def check_no_positive_tabindex(html: str) -> list[Finding]:
    """A positive ``tabindex`` reorders the page against its reading order."""
    findings: list[Finding] = []
    for value in re.findall(r'tabindex="(-?\d+)"', html):
        if int(value) > 0:
            findings.append(Finding("tab-order", f"positive tabindex={value} overrides DOM order"))
    return findings


def check_images_have_alt(html: str) -> list[Finding]:
    """Every ``img`` needs ``alt``, even if empty for a decorative one."""
    return [
        Finding("image-alt", "an <img> has no alt attribute", str(attributes)[:70])
        for attributes in _parse(html).images
        if "alt" not in attributes
    ]


def check_colour_not_alone(html: str, *, signals: tuple[str, ...]) -> list[Finding]:
    """Each state signal must be present as text, not only as a class.

    Called with the words that must appear for a given state — "partly
    verified", "Not reviewed by a clinician" — so a styling change that removes
    the text and keeps the colour fails.
    """
    lowered = html.lower()
    return [
        Finding("colour-alone", f"state signal {signal!r} is not present as text")
        for signal in signals
        if signal.lower() not in lowered
    ]


def audit_fragment(
    html: str,
    *,
    start_level: int = 1,
    expect_polite: bool = True,
    required_signals: tuple[str, ...] = (),
) -> list[Finding]:
    """Run every structural check over one rendered fragment."""
    return [
        *check_heading_order(html, start_level=start_level),
        *check_accessible_names(html),
        *check_icons_are_handled(html),
        *check_live_regions(html, expect_polite=expect_polite),
        *check_no_positive_tabindex(html),
        *check_images_have_alt(html),
        *check_colour_not_alone(html, signals=required_signals),
    ]


__all__ = [
    "DECORATIVE_HINTS",
    "INTERACTIVE_TAGS",
    "Finding",
    "audit_fragment",
    "check_accessible_names",
    "check_colour_not_alone",
    "check_heading_order",
    "check_icons_are_handled",
    "check_images_have_alt",
    "check_live_regions",
    "check_no_positive_tabindex",
]

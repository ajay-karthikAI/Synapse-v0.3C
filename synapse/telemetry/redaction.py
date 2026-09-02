"""
synapse.telemetry.redaction
===========================
Remove secrets from anything on its way into a record.

Rule 3 and rule 8 of docs/privacy-logging-policy.md. The design assumption is
the one from §2.7 of the threat analysis: **a key never leaks through a field
called ``api_key``.** It leaks through an exception message quoting a request
URL, a provider error body echoing a header, a configuration dump, a debug repr.

So redaction is applied to *every* string that reaches a telemetry record,
including ones believed to be safe, and it is written to fail toward
over-redaction. A telemetry value that has been unnecessarily replaced with
``[REDACTED]`` costs an operator a minor inconvenience; one that has not been
necessarily replaced costs a credential.

Two properties the fuzz test in ``tests/test_telemetry_privacy.py`` pins:

* redaction is **idempotent** — redacting twice yields the same string, so a
  value passing through two layers is not corrupted into something unreadable;
* redaction **never lengthens** the secret-bearing region into something that
  reveals more than it hid.

Exceptions get their own function, because the safe treatment of an exception is
not "redact its message" but "discard its message and keep its type" —
docs/privacy-logging-policy.md §2.9.
"""

from __future__ import annotations  # Postponed annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Key-shaped tokens.
#
# Note the deliberate absence of ``\b`` anchors on the token patterns. A word
# boundary looks harmless and is a trivial evasion: ``sk-ABC…`` preceded by any
# letter has no boundary before it, so ``\bsk-`` silently stops matching. The
# generative test in tests/test_telemetry_privacy.py found exactly that, at a
# 59% survival rate across 10,000 generated cases.
#
# Instead each token has two forms:
#   * a boundary-anchored form with a short minimum length, for a secret that
#     appears where one normally does (after ``=``, a space, or a quote);
#   * an anchor-free form with a longer minimum, for a secret concatenated to
#     arbitrary text. Real credentials are long and high-entropy, so the longer
#     minimum keeps ordinary words such as "risk-assessment" from matching.
_ANCHORLESS_MIN = 16  # Below this, an anchor-free match risks eating ordinary text

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI and OpenAI-compatible keys: sk-..., sk-proj-..., sk-ant-...
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(rf"sk-[A-Za-z0-9_\-]{{{_ANCHORLESS_MIN},}}", re.IGNORECASE),
    # Named credential assignments: api_key=..., secret: "...", token = ...
    re.compile(
        r"(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|token|password)"
        r"[\"\'\s:=]+(?:[A-Za-z]+[ \t]+)?[A-Za-z0-9_\-\.]{8,}",
        re.IGNORECASE,
    ),
    # Authorization headers, with or without a scheme.
    #
    # The optional scheme group matters: an earlier version consumed only the
    # first non-space token after the separator, which for
    # "Authorization: Bearer <token>" is the word "Bearer" — leaving the token
    # itself in place AND destroying the keyword the Bearer pattern below needed
    # to match. The fuzz test caught it at a 14% survival rate. Patterns are
    # applied in sequence, so a pattern that partially consumes a secret can
    # disarm a later one; the greedy form avoids that.
    re.compile(r"(?:proxy-)?authorization[\"\'\s:=]+(?:[A-Za-z]+[ \t]+)?\S+", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{8,}", re.IGNORECASE),
    re.compile(r"Basic\s+[A-Za-z0-9+/=]{12,}", re.IGNORECASE),
    # AWS, GitHub and Google-shaped credentials, in case a deployment carries them
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    # JSON Web Tokens
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    # Anything that looks like a private key block
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"),
    # A URL carrying credentials in userinfo
    re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
)

# Field names whose *value* is replaced wholesale, whatever it looks like. A
# value under one of these names is a secret by definition, and matching on the
# name is more reliable than matching on the shape.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "proxy-authorization",
        "auth",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "cookie",
        "set-cookie",
        "session_token",
        "private_key",
        "openai_api_key",
        "ncbi_api_key",
        "anthropic_api_key",
    }
)

_MAX_VALUE_CHARS = 512  # Ceiling on any single redacted string, bounding worst-case disclosure


def is_sensitive_key(name: str) -> bool:
    """True when a field name's value must be replaced regardless of content."""
    return name.strip().lower().replace(" ", "") in _SENSITIVE_KEYS


def redact_text(value: str) -> str:
    """Remove secret-shaped substrings from a string.

    Idempotent: ``redact_text(redact_text(x)) == redact_text(x)``, because
    ``[REDACTED]`` matches none of the patterns.
    """
    if not value:
        return value
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    if len(redacted) > _MAX_VALUE_CHARS:
        redacted = redacted[:_MAX_VALUE_CHARS] + "…(truncated)"
    return redacted


def redact_value(name: str, value: Any) -> Any:
    """Redact one field, by name and by content.

    Containers are walked, because a secret inside a nested dict is still a
    secret. Non-string scalars pass through: an int cannot carry a key.
    """
    if is_sensitive_key(name):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: redact_value(str(key), item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_value(name, item) for item in value)
    return value


def redact_mapping(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Redact every entry of a mapping, by name and by content."""
    return {key: redact_value(str(key), value) for key, value in fields.items()}


def redact_exception(exc: BaseException) -> str:
    """Reduce an exception to something safe to record: its type name.

    **The message is discarded, not redacted.** That is the deliberate
    difference from :func:`redact_text`. A provider exception message can quote
    the request, which quotes the prompt, which contains the patient's question
    and the retrieved passages (docs/privacy-logging-policy.md §2.8, §2.9) — and
    no pattern list can reliably recognise a health question. Discarding is the
    only treatment that holds for content nobody can pattern-match.

    Synapse's own errors are exempt from that reasoning but not from the rule:
    :mod:`synapse.errors` guarantees payload-free messages by construction, and
    their type name plus the typed failure code already carries what an operator
    needs.
    """
    return type(exc).__name__


__all__ = [
    "REDACTED",
    "is_sensitive_key",
    "redact_exception",
    "redact_mapping",
    "redact_text",
    "redact_value",
]

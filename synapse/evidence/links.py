"""
synapse.evidence.links
======================
External links that are safe to put in front of a patient.

Three separate problems, solved separately because they fail differently:

1. **Scheme.** Only ``https`` is permitted. ``javascript:`` is the obvious
   attack, but ``http`` is excluded too: a citation link is the one place a
   reader is being asked to trust the destination, and sending them over a
   cleartext connection to read about their health is not a defensible default.
   A rejected URL is **dropped**, never escaped and rendered — escaping
   ``javascript:alert(1)`` produces a link that still works.
2. **Identifier validity.** A DOI and a PMID have shapes. A malformed one must
   not be turned into a resolver link, because the resulting URL looks
   authoritative and goes somewhere arbitrary.
3. **Tab safety.** ``target="_blank"`` without ``rel="noopener"`` hands the
   opened page a handle to the opener. Every external link here carries
   ``noopener noreferrer nofollow``.

PubMed titles and abstracts are third-party content fetched from an external
feed, so a URL arriving with one is untrusted input, not a constant.
"""

from __future__ import annotations  # Postponed annotations

import re
from urllib.parse import quote, urlsplit

# Only https. See the module docstring for why http is excluded as well.
PERMITTED_SCHEMES: frozenset[str] = frozenset({"https"})

# The attributes every external link carries. `noopener` prevents the opened
# page reaching back through window.opener; `noreferrer` withholds the referring
# URL, which in a health context could itself disclose the topic being read.
EXTERNAL_LINK_REL = "noopener noreferrer nofollow"

# DOI: a "10." prefix, a registrant code, then a suffix. Deliberately permissive
# about the suffix (the standard allows almost anything) and strict about the
# prefix, which is where a malformed identifier shows itself.
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:a-zA-Z0-9\[\]<>]+$")

# PMID: digits only, currently 1-8 of them. Anything else is not a PMID.
PMID_PATTERN = re.compile(r"^\d{1,8}$")

DOI_RESOLVER = "https://doi.org/"
PUBMED_RESOLVER = "https://pubmed.ncbi.nlm.nih.gov/"

MAX_URL_CHARS = 2048  # Bounds a pathological URL before it reaches the page


def is_safe_url(url: str) -> bool:
    """True when a URL may be rendered as a link.

    Requires an https scheme and a network location. A URL with no host — for
    example ``https:///etc/passwd`` — is rejected: it is not a destination.
    """
    if not url or len(url) > MAX_URL_CHARS:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        # urlsplit raises on a few malformed inputs, e.g. an invalid IPv6 literal.
        return False
    if parts.scheme.lower() not in PERMITTED_SCHEMES:
        return False
    if not parts.netloc:
        return False
    # A newline or control character in a URL can break out of an attribute in
    # a context that escapes imperfectly. Reject rather than sanitise.
    return not any(character in url for character in "\r\n\t\0 ")


def safe_url(url: str) -> str:
    """The URL if it may be linked, otherwise the empty string.

    An empty return is the signal to render the citation **without** a link
    rather than with a broken one.
    """
    cleaned = (url or "").strip()
    return cleaned if is_safe_url(cleaned) else ""


def is_valid_doi(doi: str) -> bool:
    """True when a string has the shape of a DOI."""
    return bool(DOI_PATTERN.match((doi or "").strip()))


def is_valid_pmid(pmid: str) -> bool:
    """True when a string has the shape of a PubMed identifier."""
    return bool(PMID_PATTERN.match((pmid or "").strip()))


def doi_url(doi: str) -> str:
    """Resolver URL for a valid DOI, or the empty string.

    The suffix is percent-encoded, because a DOI may legitimately contain
    characters that are not URL-safe, and concatenating one unencoded produces a
    link that silently goes somewhere else.
    """
    cleaned = (doi or "").strip()
    if not is_valid_doi(cleaned):
        return ""
    return DOI_RESOLVER + quote(cleaned, safe="/:")


def pmid_url(pmid: str) -> str:
    """Resolver URL for a valid PMID, or the empty string."""
    cleaned = (pmid or "").strip()
    if not is_valid_pmid(cleaned):
        return ""
    return f"{PUBMED_RESOLVER}{cleaned}/"


def canonical_link(url: str, *, doi: str = "", pmid: str = "") -> str:
    """The best available link for a source, or the empty string.

    Order is deliberate: a DOI is the most durable identifier, a PMID resolves
    to a stable record, and a canonical URL is whatever the pack recorded and may
    rot. The first that validates wins.
    """
    return doi_url(doi) or pmid_url(pmid) or safe_url(url)


__all__ = [
    "DOI_PATTERN",
    "DOI_RESOLVER",
    "EXTERNAL_LINK_REL",
    "MAX_URL_CHARS",
    "PERMITTED_SCHEMES",
    "PMID_PATTERN",
    "PUBMED_RESOLVER",
    "canonical_link",
    "doi_url",
    "is_safe_url",
    "is_valid_doi",
    "is_valid_pmid",
    "pmid_url",
    "safe_url",
]

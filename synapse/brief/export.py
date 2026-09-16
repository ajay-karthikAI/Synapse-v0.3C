"""
synapse.brief.export
====================
Turning a brief into files the user keeps, and keeping nothing ourselves.

Four formats, one rule each:

* **PDF** — what the download button serves, and the format a patient actually
  keeps. See :mod:`synapse.brief.render_pdf` for why the server produces this
  rather than asking the browser to: on a phone, "print to PDF" is a gesture
  most people never find, and a phone is where this gets used.
* **HTML** — the printable document, kept for a desktop browser and for the
  print stylesheet. Self-contained, so it opens offline and reaches no network.
* **JSON** — the structured brief, for portability. The schema's own
  serialisation, so it round-trips exactly.
* **Text** — a plain fallback that pastes into a portal message or an SMS.

What this module refuses to do
------------------------------
There is no upload, no share URL, no email, no third-party delivery, and no
server-side store. "Share" means the user has a file and chooses what to do with
it. That is a deliberate ceiling, not an unfinished feature:
docs/appointment-brief.md §5 records why.

Temporary files
---------------
:func:`temporary_export` writes to a private temporary directory and deletes it
on exit, including when the caller raises. Nothing needs the file after the
bytes have been handed over, so nothing keeps it. In the Streamlit path no
temporary file is created at all — the bytes go straight to the download
button — and the context manager exists for callers that genuinely need a path.

The scan
--------
:func:`assert_no_secrets` runs over every rendered artefact before it is
returned. The schema already makes a key or a prompt unrepresentable, so this is
a second line rather than the first; it exists because "unrepresentable" is a
claim about today's model, and an export is the one place where being wrong is
irreversible.
"""

from __future__ import annotations  # Postponed annotations

import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from synapse.brief.render_pdf import PdfRendererUnavailable, render_brief_pdf
from synapse.brief.render_print import render_brief_html
from synapse.brief.render_text import render_brief_text
from synapse.brief.schema import AppointmentBrief
from synapse.logging import get_logger

logger = get_logger(__name__)

# The warning shown before any export. Fixed wording, and the export path does
# not run without the user having seen it.
EXPORT_WARNING = (
    "This file will contain health information you entered and the research it is based on. "
    "It is saved to your own device and is not sent anywhere. "
    "Anyone who opens the file, or the folder it is in, can read it."
)

# Patterns that must never appear in an exported artefact. Two families: things
# that are secret, and things that are internal plumbing a patient's document
# has no reason to carry.
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api key", re.compile(r"sk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE)),
    ("bearer token", re.compile(r"bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE)),
    ("authorization header", re.compile(r"authorization\s*[:=]", re.IGNORECASE)),
    ("api key field", re.compile(r"api[_-]?key", re.IGNORECASE)),
    ("system prompt", re.compile(r"You are a patient education assistant", re.IGNORECASE)),
    ("system prompt", re.compile(r"You help patients prepare", re.IGNORECASE)),
    ("rerank prompt", re.compile(r"You rank medical text passages", re.IGNORECASE)),
    ("telemetry identifier", re.compile(r"\b(request_id|session_id)\b", re.IGNORECASE)),
    ("provider metadata", re.compile(r"\b(gpt-4o|gpt-4o-mini|text-embedding-3)\b", re.IGNORECASE)),
    # The chunk-id grammar is "<scheme>:<key>#<ordinal>". Anchoring on the
    # scheme and key matters: a bare "#\d{4,8}" also matches a CSS hex colour,
    # which blocked the first export this scanner ever ran on.
    ("chunk identifier", re.compile(r"[A-Za-z0-9]:[A-Za-z0-9._\-]+#\d{4,8}\b")),
)


class ExportError(RuntimeError):
    """An artefact failed its pre-export checks and was not produced."""


def assert_no_secrets(payload: str, *, artefact: str) -> None:
    """Refuse to emit an artefact containing anything on the forbidden list.

    Raises:
        ExportError: naming the category only. The matched text is deliberately
            not included: an error message quoting the secret it found is how the
            secret ends up in a log.
    """
    for label, pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(payload):
            logger.error("export blocked", extra={"artefact": artefact, "category": label})
            raise ExportError(f"{artefact} contained {label} and was not exported")


@dataclass(frozen=True)
class ExportBundle:
    """The three renderings, with the filenames a user will see."""

    document_id: str
    html: str
    json_text: str
    text: str

    @property
    def html_filename(self) -> str:
        """Filename for the printable document."""
        return f"appointment-brief-{self.document_id}.html"

    @property
    def json_filename(self) -> str:
        """Filename for the portable structured form."""
        return f"appointment-brief-{self.document_id}.json"

    @property
    def text_filename(self) -> str:
        """Filename for the plain-text form."""
        return f"appointment-brief-{self.document_id}.txt"

    def sizes(self) -> dict[str, int]:
        """Byte sizes, for display beside a download button."""
        return {
            "html": len(self.html.encode("utf-8")),
            "json": len(self.json_text.encode("utf-8")),
            "text": len(self.text.encode("utf-8")),
        }


def build_export(brief: AppointmentBrief, *, page_size: str = "a4") -> ExportBundle:
    """Render every format and check each one before returning it.

    The checks run on the **rendered output**, not on the model, because a
    rendering bug is exactly the way something unexpected reaches a file.
    """
    html = render_brief_html(brief, page_size=page_size)
    # The JSON export is the schema's own serialisation, so it round-trips. It
    # carries no field the schema does not define, which is the guarantee.
    json_text = json.dumps(
        brief.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False
    )
    text = render_brief_text(brief)

    for artefact, payload in (("html", html), ("json", json_text), ("text", text)):
        assert_no_secrets(payload, artefact=artefact)

    logger.info(
        "brief exported",
        extra={"document_id": brief.document_id, "sections": len(brief.included_sections)},
    )
    return ExportBundle(document_id=brief.document_id, html=html, json_text=json_text, text=text)


@dataclass(frozen=True)
class PdfExport:
    """The rendered PDF, with the filename a user will see."""

    document_id: str
    pdf: bytes

    @property
    def filename(self) -> str:
        """Filename for the PDF."""
        return f"appointment-brief-{self.document_id}.pdf"

    @property
    def size(self) -> int:
        """Byte size, for display beside a download button."""
        return len(self.pdf)


def build_pdf_export(brief: AppointmentBrief, *, page_size: str = "a4") -> PdfExport:
    """Render the brief to PDF, checked before it is returned.

    **The scan runs on the structured brief, not on the PDF bytes**, and that is
    a correctness point rather than a shortcut. A PDF's text lives in compressed
    content streams, so ``assert_no_secrets`` over the bytes would match nothing
    whatever the document said — a check that always passes is worse than no
    check, because it reads like one that works. ``model_dump`` reaches every
    string the renderer is able to draw, since the renderer draws nothing it did
    not take from the model.

    Raises:
        ExportError: the brief failed its pre-export checks, or no PDF engine is
            installed. Both are reported the same way to the caller: there is no
            PDF, and the reason is not something a patient can act on.
    """
    projection = json.dumps(brief.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    assert_no_secrets(projection, artefact="pdf")

    try:
        payload = render_brief_pdf(brief, page_size=page_size)
    except PdfRendererUnavailable as exc:
        logger.error("pdf export unavailable", extra={"document_id": brief.document_id})
        raise ExportError("no PDF renderer is installed, so the brief was not exported") from exc

    logger.info(
        "brief exported as pdf",
        extra={
            "document_id": brief.document_id,
            "bytes": len(payload),
            "sections": len(brief.included_sections),
        },
    )
    return PdfExport(document_id=brief.document_id, pdf=payload)


@contextmanager
def temporary_export(brief: AppointmentBrief, *, page_size: str = "a4") -> Iterator[Path]:
    """Write the bundle to a private temporary directory, and delete it on exit.

    For callers that need a path rather than bytes. The directory is removed in
    a ``finally``, so it goes away when the caller raises as well as when it
    returns — a health document left in ``/tmp`` after a traceback is exactly the
    residue this guards against.

    The Streamlit path does not use this: it hands the bytes to the download
    button directly, so no temporary file exists at any point.
    """
    bundle = build_export(brief, page_size=page_size)
    directory = Path(tempfile.mkdtemp(prefix="synapse-brief-"))
    try:
        (directory / bundle.html_filename).write_text(bundle.html, encoding="utf-8")
        (directory / bundle.json_filename).write_text(bundle.json_text, encoding="utf-8")
        (directory / bundle.text_filename).write_text(bundle.text, encoding="utf-8")
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def load_brief_json(payload: str) -> AppointmentBrief:
    """Read a brief back from its JSON export.

    Full validation on the way in: an exported file is untrusted by the time it
    comes back, whatever produced it.
    """
    return AppointmentBrief.model_validate_json(payload)


__all__ = [
    "EXPORT_WARNING",
    "ExportBundle",
    "ExportError",
    "PdfExport",
    "assert_no_secrets",
    "build_export",
    "build_pdf_export",
    "load_brief_json",
    "temporary_export",
]

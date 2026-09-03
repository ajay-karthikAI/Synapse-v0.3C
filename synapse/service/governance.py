"""
synapse.service.governance
==========================
Which documents retrieval is allowed to return.

Extracted verbatim in behaviour from ``app.py``'s ``_eligible_documents``
closure. It reads the source pack from disk, so it runs once per turn on the
calling thread rather than inside the retrieval closure.

The return value is a **decision, not a bare optional**. ``None`` document ids
is not a neutral default: it means no governance is being enforced and every
document in the index can reach a patient. That is the current behaviour and it
is kept, because an unreadable pack must not take the application down -- but it
is never *silent*. The decision carries the reason, the caller logs it, and
``search_candidates`` records ``eligibility_enforced=False`` in the retrieval
trace rather than leaving it to be inferred from an absent argument.

The failure this guards against is a pack quietly ceasing to constrain anything.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from synapse.logging import get_logger

logger = get_logger(__name__)

# Where the governed source pack lives when the environment does not say
# otherwise. Matches the pack this repository ships.
DEFAULT_SOURCE_PACK = Path("source_packs/diabetes-previsit")


@dataclass(frozen=True)
class EligibilityDecision:
    """The document allow-list, and why it is what it is.

    ``reason`` is operator-facing and structural -- a count, or an exception
    type name. It never carries a query, a document title, or a provider
    message, so it is safe to log.
    """

    document_ids: frozenset[str] | None
    reason: str

    @property
    def enforced(self) -> bool:
        """True when retrieval is actually constrained by the pack."""
        return self.document_ids is not None

    @property
    def count(self) -> int:
        """How many documents are authorised. Zero when nothing is enforced."""
        return len(self.document_ids) if self.document_ids else 0


def resolve_eligible_documents(
    pack_path: Path, *, as_of: date | None = None
) -> EligibilityDecision:
    """Resolve the pack's approved documents. Never raises.

    Returns a decision whose ``document_ids`` is ``None`` when the pack is
    missing, unreadable, or authorises nothing -- in which case retrieval runs
    UNFILTERED, which is why the caller logs the reason.
    """
    try:
        # Imported lazily: the governance layer pulls in the pack schema, and
        # nothing here should cost that on a turn that never reaches retrieval.
        from synapse.governance.eligibility import select_eligible
        from synapse.governance.pack import SourcePack

        pack = SourcePack.load(pack_path)
        eligible, _ = select_eligible(pack.sources, pack.manifest, as_of=as_of or date.today())
        document_ids = frozenset(source.document_id for source in eligible)
        if not document_ids:
            return EligibilityDecision(None, "pack authorises no sources")
        return EligibilityDecision(document_ids, f"{len(document_ids)} approved source(s)")
    # Broad on purpose: an unapproved or unreadable pack authorises nothing, and
    # every way that can happen -- absent file, bad schema, expired approval --
    # has the same consequence and the same safe handling.
    except Exception as exc:
        return EligibilityDecision(None, f"not enforced ({type(exc).__name__})")


def log_eligibility(decision: EligibilityDecision) -> None:
    """Record the decision. Structural fields only -- no identifiers, no query."""
    logger.info(
        "retrieval governance resolved",
        extra={
            "eligibility_enforced": decision.enforced,
            "eligible_documents": decision.count,
            "reason": decision.reason,
        },
    )


__all__ = [
    "DEFAULT_SOURCE_PACK",
    "EligibilityDecision",
    "log_eligibility",
    "resolve_eligible_documents",
]

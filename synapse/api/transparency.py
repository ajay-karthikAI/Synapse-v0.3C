"""
synapse.api.transparency
========================
What this deployment is running, and what it has *not* been shown to be.

The payload behind the separate transparency page. It is assembled from things
that are true on disk — the source pack, the artifact manifest, the last
evaluation summary — rather than written by hand, because a transparency page
maintained by hand becomes a marketing page within two releases.

The disclosures are not configurable
------------------------------------
:data:`MANDATORY_DISCLOSURES` is a module constant and is always included, in
full, whatever the pack or the evaluation says. There is no setting that
shortens it and no branch that omits it. That is the point: the statements a
deployment would most like to drop — *no clinician has reviewed these sources*,
*no evaluation case can gate a release* — are exactly the ones a patient and a
reviewer most need.

Where a fact cannot be established the payload says so rather than guessing. An
absent evaluation summary reports ``"available": false``, never zeroes that
would read as a measured result.

Nothing here reads a conversation, a session, or a query. Every field is about
the deployment, not about anyone using it.
"""

from __future__ import annotations  # Postponed annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from synapse.answer.generate import PROMPT_ID as ANSWER_PROMPT_ID
from synapse.answer.providers import DEFAULT_MODEL
from synapse.api.models import TransparencyResponse
from synapse.logging import get_logger
from synapse.retrieval.rerank import PROMPT_ID as RERANK_PROMPT_ID

logger = get_logger(__name__)

APPLICATION_VERSION = "0.1.0"

# Stated on every response, in full, always. See the module docstring.
MANDATORY_DISCLOSURES: tuple[str, ...] = (
    "Synapse is a pre-clinical research prototype. It is not a medical device, "
    "is not FDA cleared, and is not HIPAA compliant.",
    "No clinician has reviewed any source, evaluation case, threshold, or output "
    "in this deployment.",
    "The sources currently served are UNREVIEWED. They must not be described as "
    "approved or clinically validated.",
    "Every evaluation case is synthetic and unreviewed, and none is eligible to "
    "gate a release. Reported metrics measure whether the system does what its "
    "author expected, not whether that expectation is clinically correct.",
    "Answers are drawn from published abstracts, which routinely omit study "
    "population, dosing, harms and contraindications.",
    "Synapse does not diagnose, triage, or advise on medication. If something "
    "feels urgent, contact your clinic or your local emergency number.",
)

# Facts about where text goes, kept beside the code that sends it rather than
# only in a document that can drift (docs/PRIVACY_DATA_FLOW.md).
PRIVACY_FACTS: dict[str, object] = {
    "query_text_leaves_the_server": True,
    "query_text_destinations": ["OpenAI (embedding, reranking, generation)"],
    "query_text_written_to_disk": False,
    "query_text_logged": False,
    "conversation_persisted": False,
    "conversation_storage": "process memory only, discarded on restart",
    "session_idle_expiry_seconds": 2 * 60 * 60,
    "third_party_analytics": False,
    "error_reporting_service": False,
    "cookies": ["access token (HttpOnly, signed, 8 hours)"],
    "notice": (
        "Do not enter your name, contact details, or anything else that identifies "
        "you. Your question is sent to a model provider to be answered."
    ),
}


def build_transparency(
    *,
    source_pack: Path,
    evaluation_dir: Path = Path("artifacts/evals"),
    readiness: dict[str, object] | None = None,
) -> TransparencyResponse:
    """Assemble the transparency payload. Never raises."""
    return TransparencyResponse(
        application_version=APPLICATION_VERSION,
        sources=_sources(source_pack),
        evaluation=_evaluation(evaluation_dir),
        privacy=dict(PRIVACY_FACTS),
        system=_system(readiness),
        disclosures=list(MANDATORY_DISCLOSURES),
    )


def _sources(pack_path: Path) -> dict[str, object]:
    """Source-pack governance state, counted from the pack itself."""
    unavailable: dict[str, object] = {
        "available": False,
        "reviewed_by_clinician": False,
        "approved_count": 0,
        "note": "No source pack could be read, so no source is authorised for serving.",
    }
    try:
        from datetime import date

        from synapse.governance.eligibility import select_eligible
        from synapse.governance.pack import SourcePack

        pack = SourcePack.load(pack_path)
        eligible, _ = select_eligible(pack.sources, pack.manifest, as_of=date.today())
        states: dict[str, int] = {}
        for source in pack.sources:
            key = str(getattr(source, "lifecycle_state", "unknown"))
            states[key] = states.get(key, 0) + 1
        return {
            "available": True,
            "pack_id": pack.manifest.pack_id,
            "pack_version": pack.manifest.version,
            "title": pack.manifest.title,
            "total_sources": len(pack.sources),
            "lifecycle_states": states,
            "approved_count": len(eligible),
            # Stated positively so it cannot be missed by a reader skimming counts.
            "reviewed_by_clinician": False,
            "note": (
                "Sources are governed by lifecycle state. Only 'approved' sources may be "
                "served on a governed path, and approval here has NOT involved a clinician."
            ),
        }
    # Broad on purpose: a transparency page that 500s tells a reader nothing.
    except Exception as exc:
        logger.warning("source pack unreadable", extra={"error": type(exc).__name__})
        return unavailable


def _evaluation(evaluation_dir: Path) -> dict[str, object]:
    """The most recent offline evaluation summary, if one exists."""
    absent: dict[str, object] = {
        "available": False,
        "release_gating_capable": False,
        "note": (
            "No evaluation run is present in this deployment. Metrics are produced "
            "offline against a fixed dataset, never from live patient queries — a "
            "live query has no ground truth to score against."
        ),
    }
    try:
        if not evaluation_dir.is_dir():
            return absent
        summaries = sorted(evaluation_dir.glob("*/summary.json"))
        if not summaries:
            return absent
        payload = json.loads(summaries[-1].read_text(encoding="utf-8"))
        headline = payload.get("headline_metrics", {})
        metrics = {
            name: {
                "value": entry.get("value"),
                # The denominator travels with the number: "100%" over one case
                # must not read like "100%" over two hundred.
                "denominator": entry.get("denominator", 0),
            }
            for name, entry in headline.items()
            if isinstance(entry, dict)
        }
        return {
            "available": True,
            "run_id": payload.get("run_id", ""),
            "dataset_version": payload.get("dataset_version", ""),
            "case_count": payload.get("case_count", 0),
            "headline_metrics": metrics,
            "release_gating_capable": bool(payload.get("release_gating_capable", False)),
            "all_cases_synthetic": True,
            "reviewed_by_clinician": False,
        }
    except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
        logger.warning("evaluation summary unreadable", extra={"error": type(exc).__name__})
        return absent


def _system(readiness: dict[str, object] | None) -> dict[str, object]:
    """Versions and the artifact currently served."""
    return {
        "application_version": APPLICATION_VERSION,
        "answer_model": DEFAULT_MODEL,
        "answer_prompt_version": ANSWER_PROMPT_ID,
        "rerank_prompt_version": RERANK_PROMPT_ID,
        "model_versions_pinned": False,
        "model_version_note": (
            "Model aliases move. An alias resolving to a different checkpoint changes "
            "behaviour with no change to this deployment, and nothing here detects that."
        ),
        "readiness": readiness or {},
        "generated_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "APPLICATION_VERSION",
    "MANDATORY_DISCLOSURES",
    "PRIVACY_FACTS",
    "build_transparency",
]

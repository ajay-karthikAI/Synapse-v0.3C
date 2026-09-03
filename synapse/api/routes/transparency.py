"""
The transparency surface.

Behind the service token, but **not** behind the access gate: a reader
evaluating whether to trust this system should not have to be inside it first.

The payload is assembled in :mod:`synapse.api.transparency` from the source
pack, the artifact manifest and the last evaluation summary, and it always
carries the mandatory disclosures — including that the sources are unreviewed
and that no evaluation case can gate a release.
"""

from __future__ import annotations  # Postponed annotations

from fastapi import APIRouter

from synapse.api.deps import ServiceTokenDep, StateDep
from synapse.api.models import TransparencyResponse
from synapse.api.transparency import build_transparency

router = APIRouter(prefix="/v1/transparency", tags=["transparency"])


@router.get("", response_model=TransparencyResponse, summary="What this deployment runs")
def transparency(state: StateDep, _service_token: ServiceTokenDep) -> TransparencyResponse:
    """Sources, evaluation, privacy and system metadata, with disclosures."""
    return build_transparency(
        source_pack=state.source_pack,
        evaluation_dir=state.evaluation_dir,
        readiness=state.readiness.as_dict(),
    )

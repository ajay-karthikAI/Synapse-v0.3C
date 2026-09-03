"""
The golden states, serialised as JSON for the Next.js test suite.

Why this module exists
----------------------
The frontend has to render every state a patient can reach. It could be tested
against fixtures written by hand in TypeScript — and those fixtures would be
wrong within one release, because nothing would fail when the Python contract
moved. A hand-written fixture tests that the renderer matches *what its author
believed the server sends*, which is the belief most likely to be stale.

So the fixtures are **generated from the same** ``tests.golden_states`` **the
service tests use**, through the same :func:`~synapse.api.envelopes.envelope_for`
the route calls, and committed under ``frontend/tests/fixtures/``.
``tests/test_frontend_fixtures.py`` fails when the committed files drift from
what the contract now produces, so a change to an envelope shows up as a diff in
the frontend's fixtures in the same pull request that changed it — rather than
as a runtime type error in a browser, months later.

Regenerate intentionally with ``SYNAPSE_UPDATE_SNAPSHOTS=1 pytest
tests/test_frontend_fixtures.py``, then read the diff.

What is normalised
------------------
Two fields are non-deterministic and would make the fixtures churn on every run:
the brief's ``document_id`` (freshly generated per brief) and transparency's
``generated_at`` (a clock read). Both are replaced with fixed placeholders.
Nothing else is touched — in particular no medical content, no disclaimer and no
failure code is rewritten, because those are the things under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from synapse.api.envelopes import envelope_for
from synapse.api.transparency import build_transparency
from tests.golden_states import STATES

# Where the Next.js suite reads them from.
FIXTURE_DIR = Path("frontend/tests/fixtures")

# Placeholders for the two values that legitimately differ between runs.
FIXED_DOCUMENT_ID = "brief-fixture-0000"
FIXED_TIMESTAMP = "2026-01-01T00:00:00+00:00"


def envelope_fixtures() -> dict[str, dict[str, Any]]:
    """Every golden state, as the envelope the stream route would emit.

    ``turn_index`` is fixed at zero: it is assigned by the session, not by the
    state, and letting it vary would encode a session detail into a rendering
    fixture.
    """
    return {
        state.name: envelope_for(state.build(), turn_index=0).model_dump(mode="json")
        for state in STATES
    }


def brief_fixture() -> dict[str, Any]:
    """A real brief, fetched over HTTP from a real turn.

    Built through the API rather than by calling :mod:`synapse.brief` directly,
    so the fixture carries exactly the shape the route returns — including the
    fit estimate and the available-section list, which the panel renders and
    neither of which the brief layer alone would produce.
    """
    # Imported here rather than at module scope: `tests.api_fixtures` builds a
    # FastAPI app, and this module is imported by tooling that has no need of one.
    from tests.api_fixtures import api, ask, authed

    client = api()
    headers = authed(client)
    ask(client, headers).raise_for_status()

    response = client.get("/v1/turns/0/brief", headers=headers)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    payload["document_id"] = FIXED_DOCUMENT_ID
    return payload


def transparency_fixture() -> dict[str, Any]:
    """The transparency payload for the committed source pack."""
    payload = build_transparency(
        source_pack=Path("source_packs/diabetes-previsit"),
        evaluation_dir=Path("artifacts/evals"),
        readiness={"state": "ready", "index_id": "fixture-index"},
    ).model_dump(mode="json")
    system = payload.get("system")
    if isinstance(system, dict):
        system["generated_at"] = FIXED_TIMESTAMP
    return payload


def all_fixtures() -> dict[str, dict[str, Any]]:
    """Every fixture file, keyed by its name without the ``.json`` suffix."""
    fixtures: dict[str, dict[str, Any]] = {
        f"envelope.{name}": payload for name, payload in envelope_fixtures().items()
    }
    fixtures["brief"] = brief_fixture()
    fixtures["transparency"] = transparency_fixture()
    return fixtures


def serialise(payload: dict[str, Any]) -> str:
    """Stable JSON: sorted keys, two-space indent, trailing newline.

    Sorted so a dictionary-ordering change in pydantic does not read as a
    contract change, and indented so the diff is reviewable line by line.
    """
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


__all__ = [
    "FIXED_DOCUMENT_ID",
    "FIXED_TIMESTAMP",
    "FIXTURE_DIR",
    "all_fixtures",
    "brief_fixture",
    "envelope_fixtures",
    "serialise",
    "transparency_fixture",
]

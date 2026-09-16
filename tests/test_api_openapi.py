"""
The OpenAPI contract, snapshotted.

The document a client is generated from. Snapshotting it makes any change to the
wire contract a **visible diff in a pull request** rather than something a
client discovers at runtime — a renamed field, a widened union, a route that
quietly stopped requiring a credential.

The snapshot is normalised before comparison: FastAPI orders some collections by
iteration, and a snapshot that churns on every Python version is a snapshot
nobody reads. What is pinned is the shape, not the byte order.

Alongside the snapshot are assertions that do not depend on wording, because a
snapshot proves only that the contract did not *change* — it cannot prove the
contract was ever *right*.

Regenerate intentionally with ``SYNAPSE_UPDATE_SNAPSHOTS=1 pytest``, then read
the diff before committing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.api_fixtures import make_app

SNAPSHOT = Path(__file__).parent / "snapshots" / "api" / "openapi.json"

# Routes a platform probe must reach without credentials.
PUBLIC_PATHS = {"/healthz", "/readyz"}


@pytest.fixture(scope="module")
def spec() -> dict:
    """The generated OpenAPI document."""
    return make_app().openapi()


def _normalise(document: dict) -> str:
    """Canonical JSON: sorted keys, stable indentation."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class TestContractSnapshot:
    def test_the_contract_matches_its_snapshot(self, spec: dict) -> None:
        actual = _normalise(spec)
        if os.getenv("SYNAPSE_UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.is_file():
            SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT.write_text(actual, encoding="utf-8")
            if os.getenv("SYNAPSE_UPDATE_SNAPSHOTS") != "1":
                pytest.fail("wrote a new OpenAPI snapshot; review and commit it")
            return
        assert actual == SNAPSHOT.read_text(encoding="utf-8"), (
            "The OpenAPI contract changed. A client is generated from this "
            "document. If the change is intended, regenerate with "
            "SYNAPSE_UPDATE_SNAPSHOTS=1 and read the diff."
        )

    def test_the_document_is_generated_not_handwritten(self, spec: dict) -> None:
        """Drift between a written spec and a served API is the failure this avoids."""
        assert spec["openapi"].startswith("3.")
        assert spec["info"]["title"] == "Synapse API"


class TestContractShape:
    """Properties that must hold whatever the wording is."""

    def test_every_expected_route_is_present(self, spec: dict) -> None:
        expected = {
            "/healthz",
            "/readyz",
            "/v1/access/login",
            "/v1/access/logout",
            "/v1/session",
            "/v1/turns/stream",
            "/v1/transparency",
            "/v1/brief",
            "/v1/brief/topic",
            "/v1/brief/notes",
            "/v1/brief/questions",
            "/v1/brief/questions/order",
            "/v1/brief/questions/{question_id}",
            "/v1/brief/sections",
            "/v1/brief/export/{fmt}",
        }
        assert expected <= set(spec["paths"])

    def test_interactive_docs_are_not_exposed(self, spec: dict) -> None:
        """A deployed process serves no /docs and no /redoc."""
        app = make_app()
        assert app.docs_url is None
        assert app.redoc_url is None

    def test_the_turn_envelope_is_a_discriminated_union(self, spec: dict) -> None:
        """A client must not be able to read an answer that might be a failure."""
        schemas = spec["components"]["schemas"]
        for name in (
            "AnswerEnvelope",
            "EmergencyEnvelope",
            "InsufficientEnvelope",
            "FailureEnvelope",
        ):
            assert name in schemas, name
            kind = schemas[name]["properties"]["kind"]
            # A single-value enum: the tag is fixed per shape.
            assert kind.get("const") or kind.get("enum"), name

    def test_every_envelope_carries_a_turn_index(self, spec: dict) -> None:
        schemas = spec["components"]["schemas"]
        for name in (
            "AnswerEnvelope",
            "EmergencyEnvelope",
            "InsufficientEnvelope",
            "FailureEnvelope",
        ):
            assert "turn_index" in schemas[name]["properties"], name

    def test_no_envelope_has_a_free_text_error_field(self, spec: dict) -> None:
        """There must be no field an exception could be poured into.

        The FailureEnvelope's ``message`` is the single fixed patient message,
        and ``code`` is a closed value. Anything named like a detail, trace or
        exception would be a place for provider text to arrive.
        """
        schemas = spec["components"]["schemas"]
        forbidden = {
            "detail",
            "traceback",
            "exception",
            "error",
            "stack",
            "raw",
            "provider_message",
        }
        for name in ("AnswerEnvelope", "EmergencyEnvelope", "FailureEnvelope"):
            assert not (forbidden & set(schemas[name]["properties"])), name

    def test_requests_forbid_unknown_fields(self, spec: dict) -> None:
        """A client typo is reported, not silently ignored."""
        schemas = spec["components"]["schemas"]
        for name in ("TurnRequest", "LoginRequest", "BriefTopicRequest"):
            assert schemas[name].get("additionalProperties") is False, name

    def test_the_query_field_is_bounded(self, spec: dict) -> None:
        query = spec["components"]["schemas"]["TurnRequest"]["properties"]["query"]
        assert query["minLength"] == 1
        assert query["maxLength"] == 2000

    def test_the_stream_route_declares_server_sent_events(self, spec: dict) -> None:
        responses = spec["paths"]["/v1/turns/stream"]["post"]["responses"]
        assert "text/event-stream" in responses["200"]["content"]

    def test_the_description_states_what_this_is_not(self, spec: dict) -> None:
        """The contract itself carries the disclosure, not only the docs."""
        description = spec["info"]["description"].lower()
        assert "not a medical device" in description
        assert "no clinician has reviewed" in description

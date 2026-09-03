"""
The frontend's fixtures are the Python contract, or the build fails.

Two independent things are checked here.

**Synchronisation.** Each committed file under ``frontend/tests/fixtures/``
must equal what the contract produces today. This is what stops the Next.js
suite from passing against a shape the server stopped sending.

**Coverage.** Every golden state must have a fixture, and every fixture must
correspond to a golden state. A state added to ``tests/golden_states.py``
without a fixture is a state the interface has no test for, and the failure says
so by name rather than leaving it to be noticed.

Regenerate with ``SYNAPSE_UPDATE_SNAPSHOTS=1 pytest tests/test_frontend_fixtures.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from synapse.answer.render import PERMANENT_DISCLAIMER
from synapse.ui.errors import PATIENT_ERROR_MESSAGE
from tests.frontend_fixtures import FIXTURE_DIR, all_fixtures, serialise
from tests.golden_states import MEDICAL_PROSE, PROVIDER_SECRET, STATES

UPDATING = os.environ.get("SYNAPSE_UPDATE_SNAPSHOTS") == "1"


@pytest.fixture(scope="module")
def fixtures() -> dict[str, dict]:
    """Built once: several of these run a full turn through the service."""
    return all_fixtures()


class TestFixturesMatchTheContract:
    """The committed JSON is what the server actually produces."""

    def test_every_fixture_is_committed_and_current(self, fixtures: dict[str, dict]) -> None:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        stale: list[str] = []

        for name, payload in fixtures.items():
            path = FIXTURE_DIR / f"{name}.json"
            expected = serialise(payload)
            if UPDATING:
                path.write_text(expected, encoding="utf-8")
                continue
            if not path.exists():
                stale.append(f"{path} is missing")
            elif path.read_text(encoding="utf-8") != expected:
                stale.append(f"{path} is out of date")

        if UPDATING:
            pytest.skip("regenerated fixtures")
        assert not stale, (
            "Frontend fixtures no longer match the API contract:\n  "
            + "\n  ".join(stale)
            + "\n\nRegenerate with SYNAPSE_UPDATE_SNAPSHOTS=1 pytest "
            "tests/test_frontend_fixtures.py, then READ the diff — a change here "
            "is a change to what a patient sees."
        )

    def test_no_fixture_exists_for_a_state_that_was_deleted(
        self, fixtures: dict[str, dict]
    ) -> None:
        """A leftover fixture would be tested against forever, proving nothing."""
        if UPDATING:
            pytest.skip("regenerated fixtures")
        committed = {path.stem for path in FIXTURE_DIR.glob("*.json")}
        assert committed <= set(fixtures), (
            f"Fixtures with no source state: {sorted(committed - set(fixtures))}. "
            "Delete them, or restore the state they came from."
        )

    def test_every_golden_state_has_a_fixture(self, fixtures: dict[str, dict]) -> None:
        missing = [state.name for state in STATES if f"envelope.{state.name}" not in fixtures]
        assert not missing, f"Golden states with no frontend fixture: {missing}"


class TestFixturesCarryTheInvariants:
    """The properties the frontend tests will rely on hold in the data itself.

    Asserted here rather than only in TypeScript because these are claims about
    the *contract*. If a future change let provider text into an envelope, this
    file should fail — not a browser test that happens to grep for a string.
    """

    def test_no_fixture_contains_provider_text_or_model_prose(
        self, fixtures: dict[str, dict]
    ) -> None:
        for name, payload in fixtures.items():
            body = json.dumps(payload)
            assert PROVIDER_SECRET not in body, f"{name} leaked provider text"
            assert MEDICAL_PROSE[:60] not in body, f"{name} leaked unvalidated model prose"

    def test_every_failure_carries_the_same_fixed_message(self, fixtures: dict[str, dict]) -> None:
        failures = [p for p in fixtures.values() if p.get("kind") == "failure"]
        assert failures, "no failure fixtures: the error path would be untested"
        for payload in failures:
            assert payload["message"] == PATIENT_ERROR_MESSAGE
            assert payload["brief_available"] is False

    def test_every_answer_carries_the_application_disclaimer(
        self, fixtures: dict[str, dict]
    ) -> None:
        answers = [p for p in fixtures.values() if p.get("kind") == "answer"]
        assert answers
        for payload in answers:
            assert payload["disclaimer"] == PERMANENT_DISCLAIMER

    def test_an_emergency_cites_nothing_and_offers_no_brief(
        self, fixtures: dict[str, dict]
    ) -> None:
        emergencies = [p for p in fixtures.values() if p.get("kind") == "emergency"]
        assert len(emergencies) >= 2, "the latched emergency is a distinct state"
        for payload in emergencies:
            assert payload["brief_available"] is False
            assert "sources" not in payload
            assert "claims" not in payload

    def test_every_inline_citation_resolves_to_a_listed_source(
        self, fixtures: dict[str, dict]
    ) -> None:
        """The property the citation renderer depends on to be safe.

        A claim referring to source 3 when only two are listed would render a
        marker a patient cannot follow.
        """
        for name, payload in fixtures.items():
            if payload.get("kind") != "answer":
                continue
            numbers = {source["number"] for source in payload["sources"]}
            for claim in payload["claims"]:
                unknown = set(claim["source_numbers"]) - numbers
                assert not unknown, f"{name}: claim cites unlisted sources {sorted(unknown)}"
            for excerpt in payload["excerpts"]:
                assert excerpt["source_number"] in numbers, (
                    f"{name}: excerpt attributed to unlisted source {excerpt['source_number']}"
                )

    def test_source_numbers_are_dense_and_start_at_one(self, fixtures: dict[str, dict]) -> None:
        """Numbering is display order, so gaps would be visible to a patient."""
        for name, payload in fixtures.items():
            if payload.get("kind") != "answer" or not payload["sources"]:
                continue
            numbers = [source["number"] for source in payload["sources"]]
            assert numbers == list(range(1, len(numbers) + 1)), f"{name}: numbering {numbers}"

    def test_brief_is_offered_only_where_an_answer_exists(self, fixtures: dict[str, dict]) -> None:
        for name, payload in fixtures.items():
            # Scoped to envelopes: the brief and transparency fixtures are not
            # turns and carry no `brief_available` field to be wrong about.
            if not name.startswith("envelope.") or payload["kind"] == "answer":
                continue
            assert payload["brief_available"] is False, f"{name} offered a brief"


class TestFixtureDirectoryIsWiredToTheFrontend:
    """The files land where the Next.js suite reads them."""

    def test_fixture_directory_is_inside_the_frontend_tests(self) -> None:
        assert Path("frontend/tests/fixtures") == FIXTURE_DIR

    def test_fixtures_are_not_ignored_by_git(self) -> None:
        """A gitignored fixture would pass locally and be absent in CI.

        The generated files sit under ``frontend/``, next to ``node_modules``
        and ``.next``, both of which are ignored by broad patterns. A pattern
        that caught the fixtures too would leave every frontend test passing on
        this machine and failing on a fresh checkout, with nothing saying why.
        """
        # Fixed argument list, shell=False, no user-controlled input: this is
        # not a shell-injection surface. Same reasoning, and the same pair of
        # suppressions, as `synapse/index/manifest.py`.
        result = subprocess.run(  # noqa: S603
            ["git", "check-ignore", str(FIXTURE_DIR / "envelope.answer.json")],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        # `check-ignore` exits 0 when the path IS ignored, 1 when it is not.
        assert result.returncode != 0, (
            f"{FIXTURE_DIR} is gitignored, so the generated fixtures would be "
            "absent in CI and the whole Next.js suite would fail to load them."
        )

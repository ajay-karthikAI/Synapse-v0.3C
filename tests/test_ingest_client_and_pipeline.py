"""
E-utilities client and end-to-end pipeline tests.

**No network access anywhere in this module.** Every test injects a transport
that serves bytes from ``tests/fixtures/pubmed/``, and the clock and sleep
functions are injected too, so retry and pacing behaviour is asserted without
the suite ever actually waiting. ``test_pipeline_makes_no_network_calls``
enforces this structurally by monkeypatching ``socket.socket`` to raise.
"""

from __future__ import annotations

import json
import socket
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synapse.ingest.eutils import (
    RATE_LIMIT_WITH_KEY,
    RATE_LIMIT_WITHOUT_KEY,
    EUtilsClient,
    EUtilsConfig,
    IngestError,
)
from synapse.ingest.pipeline import BuildConfig, build_corpus
from synapse.schemas.build_report import CorpusBuildReport

FIXTURES = Path(__file__).parent / "fixtures" / "pubmed"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Deterministic monotonic clock that advances only when slept on."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[
            float
        ] = []  # Every requested delay, so pacing and backoff can be asserted

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds  # Advance the clock instead of really waiting


def fixture_transport(fixture_names: list[str], *, pmids: list[str] | None = None):
    """Build a transport that serves ESearch JSON and EFetch XML from local files."""
    bodies = [(FIXTURES / name).read_bytes() for name in fixture_names]
    calls: list[str] = []

    def transport(url: str, *, timeout: float) -> bytes:
        calls.append(url)
        if "esearch" in url:  # Return the PMIDs the caller expects to fetch
            ids = (
                pmids
                if pmids is not None
                else [str(40000000 + index) for index in range(len(bodies))]
            )
            return json.dumps({"esearchresult": {"idlist": ids}}).encode("utf-8")
        return bodies[min(len([c for c in calls if "efetch" in c]) - 1, len(bodies) - 1)]

    transport.calls = calls  # type: ignore[attr-defined]  # Exposed so tests can assert on request URLs
    return transport


class TestConfiguration:
    """Requirement 9: NCBI_API_KEY and NCBI_EMAIL optional, never required."""

    def test_works_with_no_configuration_at_all(self, monkeypatch) -> None:
        monkeypatch.delenv("NCBI_API_KEY", raising=False)
        monkeypatch.delenv("NCBI_EMAIL", raising=False)
        config = EUtilsConfig.from_environment()
        assert config.api_key is None
        assert config.effective_rate == RATE_LIMIT_WITHOUT_KEY  # Lower rate, still fully functional

    def test_api_key_raises_the_rate_limit(self, monkeypatch) -> None:
        monkeypatch.setenv("NCBI_API_KEY", "test-key")
        assert EUtilsConfig.from_environment().effective_rate == RATE_LIMIT_WITH_KEY

    def test_email_is_read_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("NCBI_EMAIL", "someone@example.org")
        assert EUtilsConfig.from_environment().email == "someone@example.org"

    def test_empty_environment_value_is_treated_as_absent(self, monkeypatch) -> None:
        monkeypatch.setenv("NCBI_API_KEY", "")
        assert EUtilsConfig.from_environment().api_key is None

    def test_explicit_override_wins_over_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("NCBI_EMAIL", "env@example.org")
        assert (
            EUtilsConfig.from_environment(email="explicit@example.org").email
            == "explicit@example.org"
        )

    def test_credentials_are_attached_to_requests_only_when_present(self, monkeypatch) -> None:
        monkeypatch.delenv("NCBI_API_KEY", raising=False)
        clock = FakeClock()
        transport = fixture_transport(["structured_abstract.xml"])
        client = EUtilsClient(
            EUtilsConfig(api_key="k", email="e@example.org"),
            transport=transport,
            clock=clock,
            sleep=clock.sleep,
        )
        client.esearch("x", max_results=1)
        assert "api_key=k" in transport.calls[0]
        assert "tool=synapse" in transport.calls[0]


class TestPacing:
    """Requirement 8: respectful NCBI request pacing."""

    def test_requests_are_paced_at_the_unauthenticated_rate(self) -> None:
        clock = FakeClock()
        transport = fixture_transport(["structured_abstract.xml"])
        client = EUtilsClient(
            EUtilsConfig(api_key=None), transport=transport, clock=clock, sleep=clock.sleep
        )
        client.esearch("a", max_results=1)
        client.esearch("b", max_results=1)
        # 3 requests/second -> a minimum interval of 1/3 s before the second call.
        assert clock.sleeps and clock.sleeps[0] == pytest.approx(1 / RATE_LIMIT_WITHOUT_KEY)

    def test_first_request_is_not_delayed(self) -> None:
        clock = FakeClock()
        client = EUtilsClient(
            EUtilsConfig(),
            transport=fixture_transport(["structured_abstract.xml"]),
            clock=clock,
            sleep=clock.sleep,
        )
        client.esearch("a", max_results=1)
        assert clock.sleeps == []  # Pacing applies BETWEEN requests, not before the first

    def test_api_key_shortens_the_interval(self) -> None:
        clock = FakeClock()
        client = EUtilsClient(
            EUtilsConfig(api_key="k"),
            transport=fixture_transport(["structured_abstract.xml"]),
            clock=clock,
            sleep=clock.sleep,
        )
        client.esearch("a", max_results=1)
        client.esearch("b", max_results=1)
        assert clock.sleeps[0] == pytest.approx(1 / RATE_LIMIT_WITH_KEY)


class TestRetries:
    """Requirement 8: bounded exponential backoff and explicit timeouts."""

    def _failing_transport(self, failures: int, status: int = 503):
        state = {"count": 0}

        def transport(url: str, *, timeout: float) -> bytes:
            if state["count"] < failures:
                state["count"] += 1
                raise urllib.error.HTTPError(url, status, "server error", {}, None)  # type: ignore[arg-type]
            return json.dumps({"esearchresult": {"idlist": ["1"]}}).encode("utf-8")

        return transport

    def test_transient_failure_is_retried_and_succeeds(self) -> None:
        clock = FakeClock()
        client = EUtilsClient(
            EUtilsConfig(), transport=self._failing_transport(2), clock=clock, sleep=clock.sleep
        )
        assert client.esearch("x", max_results=1) == ["1"]

    def test_backoff_is_exponential_and_bounded(self) -> None:
        clock = FakeClock()
        config = EUtilsConfig(
            max_attempts=5,
            backoff_base_seconds=0.5,
            backoff_max_seconds=2.0,
            requests_per_second=1000.0,
        )
        client = EUtilsClient(
            config, transport=self._failing_transport(3), clock=clock, sleep=clock.sleep
        )
        client.esearch("x", max_results=1)
        backoffs = [s for s in clock.sleeps if s >= 0.5]
        assert backoffs == [
            0.5,
            1.0,
            2.0,
        ]  # Doubles, then holds at the ceiling rather than growing without limit

    def test_retries_are_capped(self) -> None:
        clock = FakeClock()
        config = EUtilsConfig(max_attempts=3, requests_per_second=1000.0)
        client = EUtilsClient(
            config, transport=self._failing_transport(99), clock=clock, sleep=clock.sleep
        )
        with pytest.raises(IngestError, match="exhausted retries"):
            client.esearch("x", max_results=1)

    def test_non_retryable_status_fails_immediately(self) -> None:
        # A 400 will fail identically every time; retrying only wastes quota.
        clock = FakeClock()
        client = EUtilsClient(
            EUtilsConfig(),
            transport=self._failing_transport(99, status=400),
            clock=clock,
            sleep=clock.sleep,
        )
        with pytest.raises(IngestError, match="non-retryable"):
            client.esearch("x", max_results=1)
        assert clock.sleeps == []  # No backoff at all

    def test_socket_errors_are_retried(self) -> None:
        state = {"count": 0}

        def transport(url: str, *, timeout: float) -> bytes:
            if state["count"] == 0:
                state["count"] += 1
                raise TimeoutError("socket timed out")
            return json.dumps({"esearchresult": {"idlist": ["7"]}}).encode("utf-8")

        clock = FakeClock()
        client = EUtilsClient(
            EUtilsConfig(requests_per_second=1000.0),
            transport=transport,
            clock=clock,
            sleep=clock.sleep,
        )
        assert client.esearch("x", max_results=1) == ["7"]

    def test_timeout_is_passed_to_the_transport(self) -> None:
        seen: dict[str, float] = {}

        def transport(url: str, *, timeout: float) -> bytes:
            seen["timeout"] = (
                timeout  # The legacy client passed none, so a hung connection blocked forever
            )
            return json.dumps({"esearchresult": {"idlist": []}}).encode("utf-8")

        clock = FakeClock()
        EUtilsClient(
            EUtilsConfig(timeout_seconds=7.5), transport=transport, clock=clock, sleep=clock.sleep
        ).esearch("x", max_results=1)
        assert seen["timeout"] == 7.5

    def test_malformed_search_response_raises_typed_error(self) -> None:
        clock = FakeClock()
        client = EUtilsClient(
            EUtilsConfig(),
            transport=lambda url, *, timeout: b"not json",
            clock=clock,
            sleep=clock.sleep,
        )
        with pytest.raises(IngestError, match="not valid JSON"):
            client.esearch("x", max_results=1)

    def test_search_sends_an_explicit_sort(self) -> None:
        # The legacy client omitted sort, silently accepting a recency-skewed default.
        clock = FakeClock()
        transport = fixture_transport(["structured_abstract.xml"])
        EUtilsClient(EUtilsConfig(), transport=transport, clock=clock, sleep=clock.sleep).esearch(
            "x", max_results=1
        )
        assert "sort=relevance" in transport.calls[0]


class TestPipelineOffline:
    """Requirements 13-16: end-to-end build from fixtures, with no network."""

    def _build(self, tmp_path: Path, fixtures: list[str], **overrides) -> tuple:
        clock = FakeClock()
        transport = fixture_transport(fixtures)
        client = EUtilsClient(
            EUtilsConfig(requests_per_second=1000.0),
            transport=transport,
            clock=clock,
            sleep=clock.sleep,
        )
        config = BuildConfig(
            queries=["test query"],
            output_dir=tmp_path / "corpus",
            corpus_version="corpus-test",
            max_results_per_query=10,
            **overrides,
        )
        return build_corpus(config, client=client, now=NOW), transport

    def test_pipeline_makes_no_network_calls(self, tmp_path: Path, monkeypatch) -> None:
        # Structural proof, not a promise: any socket construction raises.
        def forbidden(*args, **kwargs):
            raise AssertionError("ingestion attempted a network connection")

        monkeypatch.setattr(socket, "socket", forbidden)
        outcome, _ = self._build(tmp_path, ["structured_abstract.xml"])
        assert outcome.report.documents_written == 1

    def test_artifacts_are_written_and_verified(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["structured_abstract.xml"])
        assert (outcome.output_dir / "chunks.jsonl.gz").is_file()
        assert (outcome.output_dir / "documents.jsonl.gz").is_file()
        assert (outcome.output_dir / "manifest.json").is_file()
        assert (outcome.output_dir / "build_report.json").is_file()
        assert list(outcome.output_dir.rglob("*.pkl")) == []  # Safe artifact format only

    def test_build_report_validates_against_its_schema(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["structured_abstract.xml"])
        payload = json.loads((outcome.output_dir / "build_report.json").read_text())
        assert CorpusBuildReport.model_validate(payload).documents_written == 1

    def test_retracted_material_is_excluded_by_default(self, tmp_path: Path) -> None:
        # Requirement 13. Excluded, but never silently.
        outcome, _ = self._build(tmp_path, ["retracted.xml"])
        written = {d.identifiers.pmid for d in outcome.documents}
        assert "40000010" not in written  # Retracted
        assert "40000012" not in written  # Expression of concern
        assert outcome.report.retracted_excluded is True

    def test_retraction_notice_itself_is_retained(self, tmp_path: Path) -> None:
        # It is valid evidence ABOUT the retraction.
        outcome, _ = self._build(tmp_path, ["retracted.xml"])
        assert "40000011" in {d.identifiers.pmid for d in outcome.documents}

    def test_every_exclusion_is_recorded_with_a_reason(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["retracted.xml"])
        excluded = {e.document_id: e for e in outcome.report.exclusions}
        assert "pubmed:40000010" in excluded
        assert excluded["pubmed:40000010"].reason == "retracted_or_concerned"
        assert "retraction_status=retracted" in excluded["pubmed:40000010"].detail

    def test_include_retracted_flag_widens_the_corpus_and_is_reported(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["retracted.xml"], include_retracted=True)
        assert "40000010" in {d.identifiers.pmid for d in outcome.documents}
        assert outcome.report.retracted_excluded is False
        retained = next(d for d in outcome.documents if d.identifiers.pmid == "40000010")
        assert retained.retraction_status == "retracted"  # Still marked, even when included

    def test_corrected_article_is_kept(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["corrected.xml"])
        assert outcome.report.documents_written == 1

    def test_duplicate_decisions_appear_in_the_report(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["duplicates.xml"])
        assert len(outcome.report.duplicate_decisions) == 3
        assert outcome.report.duplicates_by_rule == {"pmid": 1, "doi": 1, "title_year": 1}
        assert outcome.report.documents_after_dedupe == 1

    def test_documents_without_abstracts_are_excluded_and_reported(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["missing_fields.xml"])
        assert any(e.reason == "no_abstract" for e in outcome.report.exclusions)

    def test_report_never_claims_medical_approval(self, tmp_path: Path) -> None:
        outcome, _ = self._build(tmp_path, ["structured_abstract.xml"])
        assert outcome.report.approval_summary == {"unreviewed": 1}
        combined = " ".join(outcome.report.provenance_notes).lower()
        assert "no clinician has reviewed" in combined
        assert "confers no medical approval" in combined

    def test_report_is_deterministic(self, tmp_path: Path) -> None:
        first, _ = self._build(tmp_path / "a", ["structured_abstract.xml"])
        second, _ = self._build(tmp_path / "b", ["structured_abstract.xml"])
        # Timestamps are pinned via now=NOW, so the reports should be identical
        # apart from paths, which the report does not carry.
        assert first.report.model_dump(mode="json") == second.report.model_dump(mode="json")

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        clock = FakeClock()
        client = EUtilsClient(
            EUtilsConfig(requests_per_second=1000.0),
            transport=fixture_transport(["structured_abstract.xml"]),
            clock=clock,
            sleep=clock.sleep,
        )
        config = BuildConfig(queries=["q"], output_dir=tmp_path / "corpus", corpus_version="c")
        outcome = build_corpus(config, client=client, dry_run=True, now=NOW)
        assert outcome.output_dir is None
        assert not (tmp_path / "corpus").exists()

    def test_manifest_declares_rebuild_required_since_no_vectors_were_built(
        self, tmp_path: Path
    ) -> None:
        from synapse.index import read_manifest

        outcome, _ = self._build(tmp_path, ["structured_abstract.xml"])
        manifest = read_manifest(outcome.output_dir / "manifest.json")
        assert manifest.index_state == "rebuild_required"
        assert manifest.chunking.algorithm == "section_aware_sentence"


class TestBuildCorpusCli:
    """The CLI surface, driven with a temporary config and an injected client."""

    def test_config_example_parses(self) -> None:
        from synapse.cli.build_corpus import _load_config

        build_config, client_config, digest = _load_config(Path("configs/corpus.example.toml"))
        assert build_config.queries
        assert build_config.include_retracted is False  # Governance default survives the round-trip
        assert client_config.timeout_seconds == 20.0
        assert len(digest) == 64

    def test_missing_config_is_reported_cleanly(self, capsys) -> None:
        from synapse.cli.build_corpus import main

        assert main(["--config", "does-not-exist.toml"]) == 2
        assert "CONFIGURATION ERROR" in capsys.readouterr().err

    def test_approval_notice_is_always_printed(self, capsys) -> None:
        from synapse.cli.build_corpus import main

        main(["--config", "does-not-exist.toml"])
        assert "NOT medically approved" in capsys.readouterr().err

    def test_empty_query_list_is_rejected(self, tmp_path: Path, capsys) -> None:
        from synapse.cli.build_corpus import main

        config = tmp_path / "empty.toml"
        config.write_text("[queries]\nterms = []\n", encoding="utf-8")
        assert main(["--config", str(config)]) == 2

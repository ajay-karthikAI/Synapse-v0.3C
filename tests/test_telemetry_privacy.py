"""
Privacy tests for telemetry: the guarantees in docs/privacy-logging-policy.md.

These are the tests that matter most in this repository. Everything else
protects correctness; these protect a patient whose health question must not end
up in an operational log.

The redaction fuzzer at the bottom is generative rather than
``hypothesis``-based: this project's runtime dependency set is deliberately
``pydantic`` and nothing else, and its ``[dev]`` extra is four tools. A seeded
``random.Random`` gives reproducible generation over the input space that
matters here — a secret embedded in arbitrary surrounding text — without adding
a dependency. The seed is fixed, so a failure is reproducible by rerunning the
test rather than by reading a database file.
"""

from __future__ import annotations

import json
import random
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from synapse.telemetry.config import SinkKind, TelemetryConfig
from synapse.telemetry.recorder import TelemetryRecorder
from synapse.telemetry.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_exception,
    redact_mapping,
    redact_text,
)
from synapse.telemetry.schema import (
    ActionResult,
    Environment,
    FeedbackCategory,
    TelemetryEvent,
)
from synapse.telemetry.session import SessionClock
from synapse.telemetry.sinks import (
    LOCAL_SINK_BANNER,
    FailSafeSink,
    InMemorySink,
    LocalFileSink,
    NullSink,
)

# A realistic patient question and the answer it would produce. Neither may ever
# appear in a telemetry record, in any form.
PATIENT_QUERY = "i have crushing chest pain spreading into my left arm and i take metformin"
GENERATED_ANSWER = "HbA1c reflects average plasma glucose over 2-3 months for most adults."
CITED_EXCERPT = "A target below 7% is appropriate for most non-pregnant adults with diabetes."
# Assemble synthetic credentials at runtime so repository scanners do not treat
# the test fixtures themselves as live secrets. The redactor still receives the
# exact realistic shapes it is expected to remove.
API_KEY = "".join(("sk", "-proj-", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"))


def a_recorder(**overrides) -> tuple[TelemetryRecorder, InMemorySink]:
    """A recorder writing to an in-memory sink."""
    inner = InMemorySink()
    config = TelemetryConfig(
        enabled=True,
        sink=SinkKind.MEMORY,
        environment=Environment.TEST,
        app_version="0.1.0",
        **overrides,
    )
    return TelemetryRecorder(config=config, sink=FailSafeSink(inner)), inner


def an_event(**overrides) -> TelemetryEvent:
    """A minimal valid event."""
    fields = {
        "request_id": "r" * 16,
        "session_id": "s" * 16,
        "occurred_at": datetime.now(UTC),
        "app_version": "0.1.0",
        "action_result": ActionResult.ANSWER,
    }
    fields.update(overrides)
    return TelemetryEvent(**fields)


class TestNoPatientTextEverReachesTelemetry:
    """Rules 1, 2 and 4: no query, no digest, no answer, no excerpt."""

    def test_the_schema_has_no_field_that_could_hold_a_query(self) -> None:
        forbidden = {
            "query",
            "query_text",
            "question",
            "normalized_query",
            "query_sha256",
            "query_hash",
            "query_digest",
            "query_fingerprint",
            "answer",
            "answer_text",
            "summary",
            "excerpt",
            "excerpts",
            "claim",
            "claims",
            "chunk_id",
            "chunk_ids",
            "document_ids",
            "prompt",
            "completion",
            "ip",
            "ip_address",
            "user_agent",
            "fingerprint",
            "headers",
            "traceback",
            "stack_trace",
            "exception_message",
            "feedback_text",
            "brief",
        }
        present = set(TelemetryEvent.model_fields)
        assert present & forbidden == set(), (
            f"forbidden telemetry field present: {present & forbidden}"
        )

    def test_no_digest_field_exists_at_all(self) -> None:
        """Rule 4, asserted structurally.

        A salted digest looks like a compromise. It is not: health questions
        come from a small, repetitive space, so a candidate list of a few
        hundred thousand phrasings recovers most queries by lookup
        (docs/privacy-logging-policy.md §2.1).
        """
        digest_like = [
            name
            for name in TelemetryEvent.model_fields
            if name.endswith(("_sha256", "_hash", "_digest", "_fingerprint"))
        ]
        assert digest_like == []

    def test_a_query_cannot_be_recorded_even_deliberately(self) -> None:
        with pytest.raises(ValidationError):
            an_event(query=PATIENT_QUERY)
        with pytest.raises(ValidationError):
            an_event(query_sha256="deadbeefdeadbeef")

    def test_a_full_turn_records_no_patient_text(self) -> None:
        recorder, sink = a_recorder()
        request = recorder.request(model="gpt-4o-mini", prompt_version="grounded_answer-1.0")
        with request.stage("retrieval"):
            pass
        request.evidence_count = 3
        request.candidate_count = 83
        request.claims_shown = 2
        request.set_outcome(ActionResult.ANSWER)
        assert request.emit()

        serialised = " ".join(sink.serialised())
        for secret in (PATIENT_QUERY, GENERATED_ANSWER, CITED_EXCERPT):
            assert secret not in serialised
            for word in secret.split():
                if len(word) > 6:  # Skip common short words that appear in field names
                    assert word not in serialised, f"leaked token: {word}"

    def test_counts_are_recorded_where_identifiers_are_not(self) -> None:
        # The §2.4 decision: count the evidence, never list it.
        recorder, sink = a_recorder()
        request = recorder.request()
        request.evidence_count = 3
        request.candidate_count = 83
        request.set_outcome(ActionResult.ANSWER)
        request.emit()
        event = sink.events[0]
        assert event.evidence_count == 3
        assert event.candidate_count == 83
        assert "pubmed:" not in json.dumps(event.model_dump(mode="json"))


class TestSecretRedaction:
    """Rules 3 and 8."""

    LEAK_SHAPES: ClassVar[list[str]] = [
        f"Error code: 401 - Incorrect API key provided: {API_KEY}. You can find your key at...",
        f"Authorization: Bearer {API_KEY}",
        "Authorization: Bearer "
        + ".".join(
            (
                "eyJhbGciOiJIUzI1NiJ9",
                "eyJzdWIiOiIxMjM0NSJ9",
                "SflKxwRJSMeKKF2QT4",
            )
        ),
        "https://user:hunter2password@api.example.com/v1/chat",
        "".join(("AKIA", "IOSFODNN7EXAMPLE")),
        "ghp_16CharactersOrMoreHere1234",
        "".join(("AIza", "SyD-1234567890abcdefghijklmnopqrstu")),
        'api_key="sk-svcacct-abcdefghijklmnop"',
        "\n".join(
            (
                "-----BEGIN RSA " + "PRIVATE KEY-----",
                "MIIEowIBAAKCAQEA",
                "-----END RSA " + "PRIVATE KEY-----",
            )
        ),
    ]

    @pytest.mark.parametrize("payload", LEAK_SHAPES)
    def test_secret_shapes_are_removed(self, payload: str) -> None:
        redacted = redact_text(payload)
        for fragment in ("sk-proj-", "sk-svcacct-", "hunter2", "AKIA", "ghp_", "AIzaSy", "eyJ"):
            assert fragment not in redacted
        assert "PRIVATE KEY-----\nMII" not in redacted

    def test_redaction_is_idempotent(self) -> None:
        for payload in self.LEAK_SHAPES:
            once = redact_text(payload)
            assert redact_text(once) == once

    def test_sensitive_field_names_are_replaced_wholesale(self) -> None:
        fields = {
            "api_key": "anything at all",
            "Authorization": "Bearer x",
            "OPENAI_API_KEY": API_KEY,
            "cookie": "session=abc",
            "model": "gpt-4o-mini",
        }
        redacted = redact_mapping(fields)
        assert redacted["api_key"] == REDACTED
        assert redacted["Authorization"] == REDACTED
        assert redacted["OPENAI_API_KEY"] == REDACTED
        assert redacted["cookie"] == REDACTED
        assert redacted["model"] == "gpt-4o-mini"  # Not sensitive; must survive

    def test_nested_structures_are_walked(self) -> None:
        nested = {"outer": {"headers": {"authorization": f"Bearer {API_KEY}"}, "list": [API_KEY]}}
        assert API_KEY not in json.dumps(redact_mapping(nested))

    def test_an_exception_is_reduced_to_its_type(self) -> None:
        # The message is DISCARDED, not redacted: it can quote the prompt, which
        # contains the patient's question, and no pattern recognises that.
        exc = RuntimeError(f"failed calling https://api.openai.com with {API_KEY}: {PATIENT_QUERY}")
        assert redact_exception(exc) == "RuntimeError"

    def test_a_provider_key_cannot_reach_an_event_through_a_version_field(self) -> None:
        recorder, sink = a_recorder()
        request = recorder.request(model=f"gpt-4o-mini {API_KEY}", prompt_version=API_KEY)
        request.set_outcome(ActionResult.ANSWER)
        request.emit()
        assert API_KEY not in " ".join(sink.serialised())

    def test_is_sensitive_key_matches_case_and_separators(self) -> None:
        for name in ("API_KEY", "api-key", " Authorization ", "Client_Secret"):
            assert is_sensitive_key(name) is True
        for name in ("model", "prompt_version", "candidate_count"):
            assert is_sensitive_key(name) is False


class TestRedactionFuzz:
    """Property-based coverage of secret redaction.

    Generates secrets in randomised surrounding text and asserts three
    properties: the secret never survives, redaction is idempotent, and
    non-secret text is not destroyed wholesale.
    """

    SECRET_TEMPLATES = (
        "sk-{body}",
        "sk-proj-{body}",
        "Bearer {body}",
        "AKIA{upper16}",
        "ghp_{body}",
        "api_key={body}",
        "Authorization: Bearer {body}",
    )
    NOISE = string.ascii_letters + string.digits + " .,:;/\\\"'()[]{}=<>-_\n\t"

    def _cases(self, count: int, seed: int = 20260820):
        """Generate (payload, secret) pairs deterministically."""
        rng = random.Random(seed)
        for _ in range(count):
            body = "".join(
                rng.choices(string.ascii_letters + string.digits + "_-", k=rng.randint(16, 48))
            )
            upper16 = "".join(rng.choices(string.ascii_uppercase + string.digits, k=16))
            template = rng.choice(self.SECRET_TEMPLATES)
            secret = template.format(body=body, upper16=upper16)
            prefix = "".join(rng.choices(self.NOISE, k=rng.randint(0, 60)))
            suffix = "".join(rng.choices(self.NOISE, k=rng.randint(0, 60)))
            yield f"{prefix}{secret}{suffix}", secret, body

    def test_generated_secrets_never_survive_redaction(self) -> None:
        survivors = []
        for payload, _secret, body in self._cases(10_000):
            redacted = redact_text(payload)
            # The high-entropy body is the part that must not survive; the
            # scheme word ("Bearer") surviving discloses nothing.
            if body in redacted:
                survivors.append(payload[:120])
        assert survivors == [], f"{len(survivors)} secrets survived, e.g. {survivors[:3]}"

    def test_redaction_is_idempotent_under_fuzz(self) -> None:
        for payload, _secret, _body in self._cases(2_000):
            once = redact_text(payload)
            assert redact_text(once) == once

    def test_redaction_does_not_destroy_ordinary_text(self) -> None:
        rng = random.Random(7)
        words = ["retrieval", "latency", "gpt-4o-mini", "abstain", "rrf", "2026-08-20", "p95"]
        for _ in range(1_000):
            text = " ".join(rng.choices(words, k=rng.randint(1, 12)))
            assert redact_text(text) == text

    def test_a_secret_inside_a_mapping_never_survives(self) -> None:
        for payload, _secret, body in self._cases(1_000, seed=99):
            redacted = redact_mapping({"detail": payload, "nested": {"inner": [payload]}})
            assert body not in json.dumps(redacted)


class TestSchemaIsAnAllowlist:
    """Rules 6 and 7."""

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            an_event(some_new_field="value")

    def test_a_rejected_event_is_dropped_and_counted_not_repaired(self) -> None:
        recorder, sink = a_recorder()
        request = recorder.request()
        request.set_outcome(ActionResult.FAILURE)  # No failure_code: invalid by the schema
        assert request.emit() is False
        assert recorder.dropped_invalid == 1
        assert sink.events == []

    def test_a_failure_must_carry_a_typed_code(self) -> None:
        with pytest.raises(ValidationError, match="typed failure_code"):
            an_event(action_result=ActionResult.FAILURE)

    def test_a_failure_code_without_a_failure_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only valid on a failure"):
            an_event(action_result=ActionResult.ANSWER, failure_code="internal_error")

    def test_cost_cannot_be_presented_as_authoritative(self) -> None:
        with pytest.raises(ValidationError, match="always an estimate"):
            an_event(cost_is_estimate=False)

    def test_feedback_is_a_closed_category(self) -> None:
        assert an_event(feedback=FeedbackCategory.HELPFUL).feedback is FeedbackCategory.HELPFUL
        with pytest.raises(ValidationError):
            an_event(feedback="the app told me to see a doctor and I was worried")


class TestIdentifiers:
    """§5: random, short-lived, non-identifying."""

    def test_request_ids_are_unique_and_random(self) -> None:
        recorder, _sink = a_recorder()
        ids = {recorder.request().request_id for _ in range(500)}
        assert len(ids) == 500

    def test_a_session_id_is_stable_within_its_ttl(self) -> None:
        clock = [1000.0]
        session = SessionClock(ttl_seconds=1800, now=lambda: clock[0])
        first = session.session_id()
        clock[0] += 1799
        assert session.session_id() == first

    def test_a_session_id_rotates_after_its_ttl(self) -> None:
        clock = [1000.0]
        session = SessionClock(ttl_seconds=1800, now=lambda: clock[0])
        first = session.session_id()
        clock[0] += 1801
        assert session.session_id() != first

    def test_a_session_id_is_not_derived_from_anything(self) -> None:
        # Two clocks at the identical instant must not agree: if they did, the
        # identifier would be a function of something an observer can reproduce.
        first = SessionClock(now=lambda: 1000.0).session_id()
        second = SessionClock(now=lambda: 1000.0).session_id()
        assert first != second


class TestTelemetryCanBeDisabled:
    """Rule 11, and the default."""

    def test_telemetry_is_disabled_by_default(self) -> None:
        assert TelemetryConfig().enabled is False
        assert TelemetryConfig().sink is SinkKind.NULL

    def test_a_disabled_recorder_records_nothing(self) -> None:
        recorder = TelemetryRecorder.disabled()
        request = recorder.request(model="gpt-4o-mini")
        request.set_outcome(ActionResult.ANSWER)
        assert request.emit() is False
        assert recorder.snapshot()["counters"] == {}

    def test_a_configured_sink_with_telemetry_off_is_still_null(self, monkeypatch) -> None:
        # A contradiction resolved toward recording nothing.
        monkeypatch.delenv("SYNAPSE_TELEMETRY", raising=False)
        monkeypatch.setenv("SYNAPSE_TELEMETRY_SINK", "file")
        assert TelemetryConfig.from_environment().sink is SinkKind.NULL

    def test_only_an_explicit_truthy_value_enables_telemetry(self, monkeypatch) -> None:
        for value in ("", "0", "false", "no", "off", "maybe"):
            monkeypatch.setenv("SYNAPSE_TELEMETRY", value)
            assert TelemetryConfig.from_environment().enabled is False
        for value in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("SYNAPSE_TELEMETRY", value)
            assert TelemetryConfig.from_environment().enabled is True

    def test_the_null_sink_serialises_nothing(self) -> None:
        # Not merely discarding after building: never building at all.
        assert NullSink().emit(an_event()) is None


class TestLocalSinkAndRetention:
    """Rules 10 and 12."""

    def test_the_local_sink_writes_a_local_only_banner(self, tmp_path: Path) -> None:
        sink = LocalFileSink(directory=tmp_path)
        sink.emit(an_event())
        written = next(tmp_path.glob("telemetry-*.jsonl")).read_text(encoding="utf-8")
        assert LOCAL_SINK_BANNER in written.splitlines()[0]

    def test_events_round_trip_through_the_local_sink(self, tmp_path: Path) -> None:
        sink = LocalFileSink(directory=tmp_path)
        sink.emit(an_event(candidate_count=42))
        records = list(sink.read_all())
        assert len(records) == 1
        assert records[0]["candidate_count"] == 42

    def test_prune_deletes_records_past_retention(self, tmp_path: Path) -> None:
        sink = LocalFileSink(directory=tmp_path, retention_days=30)
        now = datetime(2026, 8, 20, tzinfo=UTC)
        sink.emit(an_event(occurred_at=now - timedelta(days=60)))
        sink.emit(an_event(occurred_at=now - timedelta(days=5)))
        assert len(list(tmp_path.glob("telemetry-*.jsonl"))) == 2
        assert sink.prune(now=now) == 1
        assert len(list(tmp_path.glob("telemetry-*.jsonl"))) == 1

    def test_prune_leaves_unrelated_files_alone(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
        sink = LocalFileSink(directory=tmp_path, retention_days=1)
        assert sink.prune(now=datetime(2026, 8, 20, tzinfo=UTC)) == 0
        assert (tmp_path / "notes.txt").is_file()


class TestSinkFailureNeverBreaksAnswering:
    """The architectural requirement, at the sink and at the recorder."""

    class ExplodingSink:
        """A sink that fails, with a secret in its exception message."""

        def emit(self, event: TelemetryEvent) -> None:
            raise OSError(f"disk full while writing {API_KEY}")

        def flush(self) -> None:
            raise OSError("flush failed")

    def test_a_failing_sink_is_absorbed(self) -> None:
        sink = FailSafeSink(self.ExplodingSink())
        sink.emit(an_event())
        sink.flush()
        assert sink.failures == 2
        assert sink.last_failure_type == "OSError"

    def test_a_failing_sink_does_not_leak_its_message(self) -> None:
        sink = FailSafeSink(self.ExplodingSink())
        sink.emit(an_event())
        assert API_KEY not in sink.last_failure_type

    def test_the_recorder_survives_a_failing_sink(self) -> None:
        config = TelemetryConfig(enabled=True, sink=SinkKind.MEMORY, app_version="0.1.0")
        recorder = TelemetryRecorder(config=config, sink=FailSafeSink(self.ExplodingSink()))
        request = recorder.request()
        request.set_outcome(ActionResult.ANSWER)
        assert request.emit() is True  # The failure is absorbed below the recorder
        assert recorder.emit_failures == 0

    def test_an_unknown_stage_name_is_counted_not_raised(self) -> None:
        recorder, _sink = a_recorder()
        request = recorder.request()
        with request.stage("not_a_real_stage"):
            pass
        assert request.unknown_stages == 1


class TestNoHipaaClaim:
    """Rule 13."""

    def test_no_module_claims_hipaa_compliance(self) -> None:
        """No file may assert compliance.

        Matches claim *phrasings* rather than requiring a denial marker beside
        every mention of the word: docs legitimately list "HIPAA assessment"
        among the things that are absent, and a test that flagged those would be
        noise that gets suppressed rather than read.
        """
        claim_phrases = (
            "hipaa compliant",
            "hipaa-compliant",
            "hipaa compliance",
            "complies with hipaa",
            "hipaa certified",
            "meets hipaa",
            "hipaa ready",
        )
        repo_root = Path(__file__).resolve().parent.parent
        offenders = []
        targets = list((repo_root / "synapse").rglob("*.py")) + list(
            (repo_root / "docs").glob("*.md")
        )
        for path in targets:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                lowered = line.lower()
                if not any(phrase in lowered for phrase in claim_phrases):
                    continue
                # A claim inside a denial is fine: "no claim of HIPAA compliance".
                if any(
                    marker in lowered for marker in ("no ", "not ", "never", "cannot", "must not")
                ):
                    continue
                offenders.append(f"{path.name}:{number}")
        assert offenders == [], f"unqualified HIPAA compliance claim: {offenders}"

    def test_the_policy_states_the_non_claim_explicitly(self) -> None:
        # The absence of a claim is not the same as a stated non-claim.
        policy = (
            Path(__file__).resolve().parent.parent / "docs/privacy-logging-policy.md"
        ).read_text(encoding="utf-8")
        assert "no claim of hipaa compliance" in policy.lower()

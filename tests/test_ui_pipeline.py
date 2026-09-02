"""
End-to-end tests for the production answer path, offline.

``synapse.ui.pipeline`` is the seam ``app.py`` sits on, so these tests exercise
the whole ordering — red-flag check, retrieval, conversion, schema-constrained
generation, verification, display policy — against fakes. No network, no key.

The failure branches carry as much weight as the success branch. The defect this
milestone closes was not that the answer layer got something wrong; it was that
when the answer layer *failed*, the interface showed the model's prose anyway.
Half of what follows asserts that this can no longer happen.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from datetime import date

import pytest

from synapse.answer.brief import ACTION_LABELS, render_appointment_brief
from synapse.answer.policy import DisplayPolicy
from synapse.answer.providers import (
    OpenAIStructuredClient,
    ProviderUnavailableError,
    strict_schema,
)
from synapse.answer.render import PERMANENT_DISCLAIMER, render_answer_html
from synapse.answer.schema import AnswerAction, provider_json_schema
from synapse.ui.errors import (
    PATIENT_ERROR_MESSAGE,
    AnswerFailureCode,
    classify,
)
from synapse.ui.legacy_evidence import (
    REMOVAL_DEADLINE,
    evidence_from_reranked,
)
from synapse.ui.pipeline import TurnOutcome, answer_turn

CHUNK_TEXT = (
    "HbA1c reflects average plasma glucose over 2-3 months. "
    "A target below 7% is appropriate for most non-pregnant adults with diabetes."
)


@dataclass
class FakeChunk:
    """The shape the legacy retrieval stack returns: duck-typed, not imported.

    Importing ``Data.fetch_and_chunk.Chunk`` would drag langchain into the test
    environment, which is exactly the coupling the synapse package exists to
    avoid.
    """

    text: str = CHUNK_TEXT
    source: str = "pubmed"
    pmid: str = "41802233"
    title: str = "HbA1c Targets in Adults"
    chunk_index: int = 0
    source_url: str = "https://pubmed.ncbi.nlm.nih.gov/41802233/"


def reranked(*chunks: FakeChunk) -> list[dict]:
    """Legacy reranked results, as ``Retrieval.reranker`` produces them."""
    return [
        {"chunk": chunk, "rank": index, "relevance_score": 0.9 - index * 0.1}
        for index, chunk in enumerate(chunks or (FakeChunk(),), start=1)
    ]


def convert(*chunks: FakeChunk):
    """Convert through the deprecated adapter, swallowing its warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return evidence_from_reranked(reranked(*chunks))


GOOD_RESPONSE = json.dumps(
    {
        "summary": "Your HbA1c reflects your average blood sugar.",
        "claims": [
            {
                "claim_id": "c1",
                "text": "HbA1c reflects average plasma glucose over 2-3 months.",
                "source_ids": ["pubmed:41802233"],
                "supporting_excerpts": [
                    {
                        "source_id": "pubmed:41802233",
                        "chunk_id": "pubmed:41802233#0000",
                        "quote": "HbA1c reflects average plasma glucose over 2-3 months",
                    }
                ],
            }
        ],
        "doctor_evaluation": "Your clinician will read it alongside your history.",
        "questions_for_doctor": ["What does my result mean for me?"],
        "limitations": ["General information from published research."],
        "disclaimer": "model text the renderer ignores",
        "action": "answer",
    }
)


class FakeClient:
    """A generation client returning a canned response, or raising."""

    def __init__(self, response: str = GOOD_RESPONSE, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0
        self.last_prompt = ""

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        self.last_prompt = user
        if self.error is not None:
            raise self.error
        return self.response


def run(
    *,
    client: FakeClient | None = None,
    retrieve=None,
    is_emergency=lambda _query: False,
    policy: DisplayPolicy | None = None,
) -> TurnOutcome:
    """Run one turn with the deprecated adapter's warning suppressed."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return answer_turn(
            "why is my blood sugar high?",
            retrieve=retrieve or (lambda _query: reranked()),
            client=client or FakeClient(),
            is_emergency=is_emergency,
            policy=policy,
        )


class TestSuccessfulTurn:
    """The path a patient normally takes."""

    def test_a_grounded_answer_is_rendered_from_typed_fields(self) -> None:
        outcome = run()
        assert outcome.ok
        answer = outcome.presentation.answer
        assert answer.action is AnswerAction.ANSWER
        assert [claim.claim_id for claim in answer.claims] == ["c1"]
        html = render_answer_html(answer, outcome.presentation.numbering)
        assert 'data-action="answer"' in html
        assert PERMANENT_DISCLAIMER in html

    def test_the_prompt_carries_real_identifiers_not_source_n_markers(self) -> None:
        client = FakeClient()
        run(client=client)
        assert "pubmed:41802233#0000" in client.last_prompt
        assert "[Source 1]" not in client.last_prompt

    def test_source_numbering_comes_from_retrieval_order(self) -> None:
        second = FakeChunk(pmid="41900001", title="Routine Diabetes Review", text="Other text.")
        outcome = run(retrieve=lambda _query: reranked(FakeChunk(), second))
        numbering = outcome.presentation.numbering
        assert numbering.marker_for("pubmed:41802233") == "[1]"
        assert numbering.marker_for("pubmed:41900001") == "[2]"


class TestMalformedOutputNeverFallsBack:
    """Requirement 9: a failed validation is a failure, not a degraded answer."""

    MEDICAL_PROSE = "📋 WHAT THE RESEARCH SAYS\nTake 500mg of metformin twice daily."

    def test_non_json_output_is_a_typed_failure(self) -> None:
        outcome = run(client=FakeClient(response=self.MEDICAL_PROSE))
        assert not outcome.ok
        assert outcome.failure.code is AnswerFailureCode.GENERATION_INVALID_JSON

    def test_the_free_form_text_never_reaches_the_patient(self) -> None:
        from synapse.answer.render import render_failure_html

        outcome = run(client=FakeClient(response=self.MEDICAL_PROSE))
        html = render_failure_html(PATIENT_ERROR_MESSAGE, outcome.failure.code.value)
        assert "metformin" not in html
        assert "WHAT THE RESEARCH SAYS" not in html
        assert PATIENT_ERROR_MESSAGE in html
        assert PERMANENT_DISCLAIMER in html

    def test_schema_violation_is_distinguished_from_bad_json(self) -> None:
        # Valid JSON, invalid answer: claims must have a claim_id.
        payload = json.dumps({"summary": "s", "action": "answer", "claims": [{"text": "x"}]})
        outcome = run(client=FakeClient(response=payload))
        assert outcome.failure.code is AnswerFailureCode.GENERATION_SCHEMA_INVALID

    def test_a_malformed_identifier_fails_closed(self) -> None:
        # "Source 1" is not a document identifier. The schema rejects it before
        # anything tries to verify against it.
        payload = json.loads(GOOD_RESPONSE)
        payload["claims"][0]["source_ids"] = ["Source 1"]
        payload["claims"][0]["supporting_excerpts"] = []
        outcome = run(client=FakeClient(response=json.dumps(payload)))
        assert outcome.failure.code is AnswerFailureCode.GENERATION_SCHEMA_INVALID

    def test_a_citation_to_an_unretrieved_source_is_withheld(self) -> None:
        # Well-formed but invented: caught by the verifier, not the schema, and
        # the claim is withheld — leaving nothing, so the turn abstains.
        payload = json.loads(GOOD_RESPONSE)
        payload["claims"][0]["source_ids"] = ["pubmed:99999999"]
        payload["claims"][0]["supporting_excerpts"][0]["source_id"] = "pubmed:99999999"
        payload["claims"][0]["supporting_excerpts"][0]["chunk_id"] = "pubmed:99999999#0000"
        outcome = run(client=FakeClient(response=json.dumps(payload)))
        assert outcome.ok
        assert outcome.presentation.answer.action is AnswerAction.ABSTAIN
        assert outcome.presentation.answer.claims == []

    def test_no_provider_exception_text_reaches_the_failure(self) -> None:
        secret = "sk-live-0000 quota exceeded for org-internal-name"
        outcome = run(client=FakeClient(error=RuntimeError(secret)))
        assert not outcome.ok
        assert secret not in outcome.failure.internal_detail
        assert secret not in outcome.failure.patient_message
        assert outcome.failure.internal_detail == "RuntimeError"

    def test_provider_outage_is_its_own_code(self) -> None:
        outcome = run(
            client=FakeClient(error=ProviderUnavailableError(problem="provider call failed"))
        )
        assert outcome.failure.code is AnswerFailureCode.GENERATION_UNAVAILABLE

    def test_every_failure_carries_the_same_patient_message(self) -> None:
        for code in AnswerFailureCode:
            from synapse.ui.errors import AnswerFailure

            assert AnswerFailure(code).patient_message == PATIENT_ERROR_MESSAGE


class TestFailClosedRetrieval:
    """Nothing to ground in is a failure, not an invented answer."""

    def test_retrieval_error_is_typed(self) -> None:
        def explode(_query: str) -> list:
            raise OSError("index file missing")

        outcome = run(retrieve=explode)
        assert outcome.failure.code is AnswerFailureCode.RETRIEVAL_FAILED
        assert "index file missing" not in outcome.failure.internal_detail

    def test_empty_retrieval_never_reaches_the_model(self) -> None:
        client = FakeClient()
        outcome = run(client=client, retrieve=lambda _query: [])
        assert outcome.failure.code is AnswerFailureCode.EVIDENCE_UNAVAILABLE
        assert client.calls == 0

    def test_unidentifiable_chunks_are_dropped(self) -> None:
        # No PMID and no text: nothing to derive an identifier from.
        unusable = FakeChunk(text="", pmid="", title="")
        outcome = run(retrieve=lambda _query: reranked(unusable))
        assert outcome.failure.code is AnswerFailureCode.EVIDENCE_UNAVAILABLE


class TestEmergencyRoutingIsPreserved:
    """Requirement 11: P1 emergency behaviour is untouched."""

    def test_emergency_short_circuits_before_retrieval_and_generation(self) -> None:
        client = FakeClient()
        touched = []

        def retrieve(query: str) -> list:
            touched.append(query)
            return reranked()

        outcome = run(client=client, retrieve=retrieve, is_emergency=lambda _query: True)
        assert outcome.ok
        assert outcome.presentation.action is AnswerAction.EMERGENCY
        assert touched == []
        assert client.calls == 0

    def test_emergency_renders_the_escalation_and_the_disclaimer(self) -> None:
        outcome = run(is_emergency=lambda _query: True)
        html = render_answer_html(outcome.presentation.answer, outcome.presentation.numbering)
        assert 'data-action="emergency"' in html
        assert "emergency department" in html
        assert PERMANENT_DISCLAIMER in html

    def test_a_broken_detector_fails_closed(self) -> None:
        def broken(_query: str) -> bool:
            raise ValueError("vocabulary file corrupt")

        outcome = run(is_emergency=broken)
        assert not outcome.ok  # No answer is produced when the red-flag check is unreliable
        assert outcome.failure.code is AnswerFailureCode.INTERNAL_ERROR


class TestTurnOutcomeIsExclusive:
    """A caller cannot render nothing, and cannot render both."""

    def test_neither_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            TurnOutcome()

    def test_both_is_rejected(self) -> None:
        from synapse.ui.errors import AnswerFailure
        from synapse.ui.pipeline import AnswerPresentation

        outcome = run()
        with pytest.raises(ValueError, match="exactly one"):
            TurnOutcome(
                presentation=AnswerPresentation(
                    outcome.presentation.result, outcome.presentation.numbering
                ),
                failure=AnswerFailure(AnswerFailureCode.INTERNAL_ERROR),
            )


class TestLegacyAdapterIsQuarantined:
    """Requirement 10: isolated, deprecated, and dated."""

    def test_conversion_emits_a_deprecation_warning(self) -> None:
        with pytest.warns(DeprecationWarning, match="scheduled for removal"):
            evidence_from_reranked(reranked())

    def test_the_removal_deadline_has_not_passed(self) -> None:
        # Fails the build once the deadline is reached, which is the point of
        # writing one down.
        assert date.today() <= REMOVAL_DEADLINE, (
            "the legacy retrieval adapter is past its removal deadline; delete "
            "synapse/ui/legacy_evidence.py and emit typed records from the "
            "retrieval layer (docs/answer-rendering.md §7)"
        )

    def test_identifiers_are_rebuilt_to_the_grammar(self) -> None:
        conversion = convert()
        assert conversion.source_order == ["pubmed:41802233"]
        assert set(conversion.evidence.chunk_texts) == {"pubmed:41802233#0000"}

    def test_colliding_legacy_identifiers_are_dropped_not_merged(self) -> None:
        # The legacy scheme restarts chunk_index per fetch, so the same article
        # ingested twice produces duplicate identifiers (C6/C7). The second is
        # dropped: a quote from it then fails verification rather than matching
        # against text the model was never shown under that identifier.
        duplicate = FakeChunk(text="A different passage entirely.")
        conversion = convert(FakeChunk(), duplicate)
        assert conversion.dropped_duplicate_ids == 1
        assert conversion.evidence.chunk_texts["pubmed:41802233#0000"] == CHUNK_TEXT

    def test_only_the_ui_package_imports_the_adapter(self) -> None:
        """A new caller elsewhere must fail CI rather than quietly extend its life.

        Checked by parsing imports rather than by grepping for the module name:
        prose that *mentions* the adapter (``synapse.retrieval.evidence``
        explains how it differs from it) is not a caller, and a substring match
        cannot tell the two apart.
        """
        import ast
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        importers: set[str] = set()
        for path in repo_root.rglob("*.py"):
            if ".venv" in path.parts or "build" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    module = " ".join(alias.name for alias in node.names)
                if "legacy_evidence" in module:
                    importers.add(path.relative_to(repo_root).as_posix())

        assert importers <= {
            "synapse/ui/legacy_evidence.py",
            "synapse/ui/pipeline.py",
            "synapse/ui/__init__.py",
            "tests/test_ui_pipeline.py",
        }, f"unexpected importer of the deprecated adapter: {importers}"


class TestStrictSchemaForTheProvider:
    """The contract handed to the model is the one the code enforces."""

    def test_every_property_is_required_and_objects_are_closed(self) -> None:
        schema = strict_schema(provider_json_schema())
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        claim = schema["$defs"]["GroundedClaim"]
        assert set(claim["required"]) == set(claim["properties"])

    def test_no_ref_carries_sibling_keywords(self) -> None:
        """Strict decoding rejects any keyword beside a ``$ref``.

        Regression: pydantic emits an enum-typed field with a docstring as
        ``{"$ref": ..., "description": ..., "default": ...}``, and the provider
        answered the whole request with a 400 —
        ``context=('properties', 'action'), $ref cannot have keywords
        {'description'}``. Every answer failed with `generation_unavailable`,
        and no offline test could see it because the rule is enforced
        server-side.
        """
        offenders: list[str] = []

        def walk(node: object, path: str) -> None:
            if isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{path}[{index}]")
                return
            if not isinstance(node, dict):
                return
            if "$ref" in node and len(node) > 1:
                offenders.append(f"{path}: {sorted(set(node) - {'$ref'})}")
            for key, value in node.items():
                walk(value, f"{path}.{key}")

        walk(strict_schema(provider_json_schema()), "$")
        assert offenders == []

    def test_every_ref_resolves(self) -> None:
        # Clearing a $ref's siblings must not clear the $ref itself.
        schema = strict_schema(provider_json_schema())
        declared = set(schema["$defs"])
        found: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            ref = node.get("$ref")
            if isinstance(ref, str):
                found.append(ref.rsplit("/", 1)[-1])
            for value in node.values():
                walk(value)

        walk(schema)
        assert found, "the schema uses $ref; if that changes, this test is measuring nothing"
        assert set(found) <= declared

    def test_support_status_is_still_absent(self) -> None:
        # The verifier assigns it; asking the model would invite it to mark its
        # own work.
        claim = strict_schema(provider_json_schema())["$defs"]["GroundedClaim"]
        assert "support_status" not in claim["properties"]

    def test_a_provider_status_code_is_preserved_for_the_operator(self) -> None:
        # 400, 401, 429 and 5xx each call for a different operator response, so
        # the status survives; the provider's message, which can carry a key
        # fragment or a request id, does not.
        from synapse.answer.providers import _provider_failure

        class FakeStatusError(Exception):
            status_code = 400

        failure = _provider_failure(FakeStatusError("Invalid schema ... sk-proj-SECRET"))
        assert failure.details["status"] == 400
        assert "sk-proj-SECRET" not in failure.safe_message
        assert failure.details["error"] == "FakeStatusError"

    def test_a_provider_error_without_a_status_still_classifies(self) -> None:
        from synapse.answer.providers import _provider_failure

        failure = _provider_failure(TimeoutError("read timed out after 60s"))
        assert "status" not in failure.details
        assert "60s" not in failure.safe_message

    def test_the_client_refuses_to_run_without_a_key(self) -> None:
        with pytest.raises(ProviderUnavailableError):
            OpenAIStructuredClient(api_key="").generate_structured(
                system="s", user="u", schema=provider_json_schema()
            )


class TestClassification:
    """Exceptions become codes, not messages."""

    def test_an_unknown_exception_is_internal(self) -> None:
        failure = classify(ZeroDivisionError("division by zero"))
        assert failure.code is AnswerFailureCode.INTERNAL_ERROR
        assert "division" not in failure.internal_detail


class TestAppointmentBriefExport:
    """The takeaway carries the same content, and nothing more."""

    def test_the_brief_identifies_itself_and_carries_the_users_topic(self) -> None:
        """The brief no longer prints a status line for an ordinary answer.

        It used to say "Status: Answer based on published research", which is
        redundant beside a document titled "Appointment brief" whose sections are
        "What the research says" and "Details from published research". The
        status line survives only for the routing states, where it says
        something the rest of the page does not.
        """
        outcome = run()
        brief = render_appointment_brief(
            outcome.presentation.answer, outcome.presentation.numbering, query="test question"
        )
        assert "APPOINTMENT BRIEF" in brief
        assert "test question" in brief  # The user's topic
        assert "Document " in brief  # The random document identifier

    def test_the_brief_states_the_action_for_a_routing_state(self) -> None:
        from synapse.answer.render import SourceNumbering
        from synapse.answer.schema import GroundedAnswer

        brief = render_appointment_brief(
            GroundedAnswer(action=AnswerAction.EMERGENCY), SourceNumbering()
        )
        assert ACTION_LABELS[AnswerAction.EMERGENCY] in brief

    def test_the_brief_always_ends_with_the_disclaimer(self) -> None:
        outcome = run()
        brief = render_appointment_brief(
            outcome.presentation.answer, outcome.presentation.numbering
        )
        assert brief.rstrip().endswith(PERMANENT_DISCLAIMER)

    def test_a_non_http_source_url_is_not_printed(self) -> None:
        hostile = FakeChunk(source_url="javascript:alert(1)")
        outcome = run(retrieve=lambda _query: reranked(hostile))
        brief = render_appointment_brief(
            outcome.presentation.answer, outcome.presentation.numbering
        )
        assert "javascript:" not in brief


class TestEscalationLatch:
    """A red flag must survive into the next turn (2026-08-31 safety fix).

    Before this, a patient who described crushing chest pain was escalated and
    then, on asking "is that serious?", got an ordinary retrieved answer: the
    follow-up carries no emergency vocabulary and the detector sees one query at
    a time. ``answer_turn`` now ORs the detector's verdict with a latched prior
    escalation supplied by the caller.
    """

    class Unreachable:
        """Retrieval and generation must not run on an escalated turn."""

        def __call__(self, query: str) -> object:
            raise AssertionError("retrieval ran on an escalated turn")

        def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
            raise AssertionError("generation ran on an escalated turn")

    def test_a_latched_escalation_escalates_a_benign_followup(self) -> None:
        never = self.Unreachable()
        outcome = answer_turn(
            "is that serious?",
            retrieve=never,
            client=never,
            is_emergency=lambda q: False,  # The follow-up alone is NOT an emergency
            recent_escalation=True,
        )
        assert outcome.ok
        assert outcome.presentation.answer.action is AnswerAction.EMERGENCY

    def test_without_the_latch_the_same_turn_is_answered_normally(self) -> None:
        """The control: the latch is what changes the outcome, nothing else."""
        reached = {"retrieval": False}

        def retrieve(query: str) -> object:
            reached["retrieval"] = True
            raise RuntimeError("stop here; reaching retrieval is the assertion")

        outcome = answer_turn(
            "is that serious?",
            retrieve=retrieve,
            client=self.Unreachable(),
            is_emergency=lambda q: False,
            recent_escalation=False,
        )
        assert reached["retrieval"], "no latch, no red flag -- retrieval should have run"
        assert not outcome.ok

    def test_the_latch_never_suppresses_a_live_detection(self) -> None:
        """It is an OR: recent_escalation=False cannot clear a real red flag."""
        never = self.Unreachable()
        outcome = answer_turn(
            "i have crushing chest pain",
            retrieve=never,
            client=never,
            is_emergency=lambda q: True,
            recent_escalation=False,
        )
        assert outcome.presentation.answer.action is AnswerAction.EMERGENCY

    def test_the_default_is_no_latch(self) -> None:
        """Every existing caller keeps its behaviour without passing anything."""
        reached = {"retrieval": False}

        def retrieve(query: str) -> object:
            reached["retrieval"] = True
            raise RuntimeError("stop here")

        answer_turn(
            "what is hba1c?",
            retrieve=retrieve,
            client=self.Unreachable(),
            is_emergency=lambda q: False,
        )
        assert reached["retrieval"]

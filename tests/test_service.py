"""
Unit tests for the application service.

``synapse.service`` is what ``app.py``'s submit handler used to be. These tests
cover the properties that were previously unreachable without running Streamlit
against a paid API: the emergency latch window, which turns are eligible as
retrieval context, the ordering of the index gate, and the guarantee that a
progress update cannot carry a patient's question.

The last one has no equivalent in the old code because there was nothing to
test: the label was a mutable string a closure wrote whatever it liked into.

Everything here is offline. No network, no key, no FAISS, no corpus on disk.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import ClassVar

import pytest

from synapse.answer.generate import PROMPT_ID as ANSWER_PROMPT_ID
from synapse.answer.providers import DEFAULT_MODEL
from synapse.answer.schema import AnswerAction
from synapse.errors import ArtifactIntegrityError
from synapse.retrieval.rerank import PROMPT_ID as RERANK_PROMPT_ID
from synapse.service import (
    STAGE_MESSAGES,
    CachingIndexProvider,
    Conversation,
    ConversationTurn,
    ProgressEvent,
    ProgressStage,
    ServiceConfig,
    SynapseService,
    null_reporter,
)
from synapse.service.governance import EligibilityDecision, resolve_eligible_documents
from synapse.service.service import MissingCredentialError
from tests.conftest import REPO_ROOT
from tests.golden_states import (
    FakeClient,
    bundle,
    empty_bundle,
    service,
)

PACKAGE_ROOT = REPO_ROOT / "synapse" / "service"


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


class TestProgressCarriesNoContent:
    """The privacy property, asserted structurally rather than by convention."""

    def test_a_progress_event_has_exactly_one_field(self) -> None:
        """The obvious future 'improvement' is a ``detail`` field. It must not land.

        A reporter that accepted free text would eventually be called with
        "Searching for chest pain...", which is a privacy incident
        (docs/privacy-logging-policy.md). The absence is the control.
        """
        fields = [field.name for field in dataclasses.fields(ProgressEvent)]
        assert fields == ["stage"]

    def test_an_event_cannot_be_given_a_message(self) -> None:
        with pytest.raises(TypeError):
            ProgressEvent(ProgressStage.SEARCHING, "searching for chest pain")  # type: ignore[call-arg]

    def test_the_message_is_derived_from_the_closed_enum(self) -> None:
        for stage in ProgressStage:
            assert ProgressEvent(stage).message == STAGE_MESSAGES[stage]

    def test_every_stage_has_copy_and_no_placeholder(self) -> None:
        for stage in ProgressStage:
            message = STAGE_MESSAGES[stage]
            assert message.strip()
            # An interpolation marker would mean a caller supplies part of the
            # string, which is the thing this design forbids.
            assert "{" not in message and "%" not in message

    def test_the_wording_is_unchanged_from_the_streamlit_handler(self) -> None:
        """Parity: these are the strings app.py displayed before the extraction."""
        assert STAGE_MESSAGES[ProgressStage.CHECKING_QUESTION] == "Checking your question..."
        assert STAGE_MESSAGES[ProgressStage.SEARCHING] == "Searching relevant research..."
        assert STAGE_MESSAGES[ProgressStage.RANKING] == "Ranking the strongest evidence..."
        assert STAGE_MESSAGES[ProgressStage.ANSWER_READY] == "Answer ready"
        assert STAGE_MESSAGES[ProgressStage.NO_ANSWER] == "No answer available"

    def test_only_the_two_terminal_stages_are_terminal(self) -> None:
        terminal = {stage for stage in ProgressStage if ProgressEvent(stage).terminal}
        assert terminal == {ProgressStage.ANSWER_READY, ProgressStage.NO_ANSWER}

    def test_the_null_reporter_accepts_every_stage(self) -> None:
        for stage in ProgressStage:
            assert null_reporter(ProgressEvent(stage)) is None


class TestProgressIsReportedInOrder:
    """A turn walks the stages once, in a sensible order."""

    def test_a_successful_turn_ends_on_answer_ready(self) -> None:
        seen: list[ProgressStage] = []
        service().ask(
            "why is my blood sugar high?",
            Conversation(),
            report=lambda event: seen.append(event.stage),
        )
        assert seen[0] is ProgressStage.CHECKING_QUESTION
        assert seen[-1] is ProgressStage.ANSWER_READY

    def test_a_failed_turn_ends_on_no_answer(self) -> None:
        seen: list[ProgressStage] = []
        service(retriever=lambda _query: empty_bundle()).ask(
            "why is my blood sugar high?",
            Conversation(),
            report=lambda event: seen.append(event.stage),
        )
        assert seen[-1] is ProgressStage.NO_ANSWER

    def test_an_escalation_reports_no_retrieval_stage(self) -> None:
        """Nothing is searched, so nothing may claim to be searching."""
        seen: list[ProgressStage] = []
        service(is_emergency=lambda _query: True).ask(
            "crushing chest pain",
            Conversation(),
            report=lambda event: seen.append(event.stage),
        )
        assert ProgressStage.SEARCHING not in seen
        assert ProgressStage.RANKING not in seen
        assert seen[-1] is ProgressStage.ANSWER_READY


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


def _answered(query: str = "q") -> ConversationTurn:
    return service().ask(query, Conversation())


def _escalated(query: str = "crushing chest pain") -> ConversationTurn:
    return service(is_emergency=lambda _q: True).ask(query, Conversation())


def _failed(query: str = "q") -> ConversationTurn:
    return service(retriever=lambda _q: empty_bundle()).ask(query, Conversation())


class TestRetrievalHistory:
    """Which turns are eligible as context for a query rewrite."""

    def test_an_answered_turn_contributes_its_summary(self) -> None:
        conversation = Conversation(turns=[_answered("what is metformin?")])
        history = conversation.retrieval_history()
        assert len(history) == 1
        assert history[0].query == "what is metformin?"
        assert history[0].answer_summary

    def test_a_failed_turn_is_excluded(self) -> None:
        """A failure has no answer, so it carries no evidence context."""
        assert Conversation(turns=[_failed()]).retrieval_history() == []

    def test_an_escalation_is_excluded(self) -> None:
        """An escalation was produced without retrieval; it must not steer one."""
        assert Conversation(turns=[_escalated()]).retrieval_history() == []

    def test_an_abstention_is_kept_but_its_summary_is_blanked(self) -> None:
        """The boilerplate steers the rewrite wrong; the question still resolves it.

        After "what is metformin?" abstains, "what about the side effects?" still
        means metformin's -- so the turn is kept and only the fixed abstention
        wording is dropped.
        """
        from tests.golden_states import ABSTAIN_RESPONSE

        turn = service(client=FakeClient(ABSTAIN_RESPONSE)).ask(
            "what is metformin?", Conversation()
        )
        assert turn.action is AnswerAction.ABSTAIN
        history = Conversation(turns=[turn]).retrieval_history()
        assert len(history) == 1
        assert history[0].query == "what is metformin?"
        assert history[0].answer_summary == ""

    def test_the_window_keeps_the_most_recent_turns(self) -> None:
        turns = [_answered(f"q{index}") for index in range(5)]
        history = Conversation(turns=turns).retrieval_history(limit=3)
        assert [summary.query for summary in history] == ["q2", "q3", "q4"]

    def test_a_malformed_turn_fails_open(self) -> None:
        """Context is an optimisation; losing it must not cost an answer."""

        class Exploding:
            @property
            def outcome(self):
                raise RuntimeError("unreadable turn")

        conversation = Conversation(turns=[Exploding()])  # type: ignore[list-item]
        assert conversation.retrieval_history() == []

    def test_question_history_carries_questions_not_answers(self) -> None:
        """A model quoting a prior summary would cite unretrieved evidence."""
        conversation = Conversation(turns=[_answered("what is metformin?")])
        assert conversation.question_history() == ["what is metformin?"]


class TestEmergencyLatch:
    """A red flag survives a benign follow-up."""

    def test_an_escalated_turn_latches(self) -> None:
        assert Conversation(turns=[_escalated()]).recent_escalation() is True

    def test_an_ordinary_turn_does_not_latch(self) -> None:
        assert Conversation(turns=[_answered()]).recent_escalation() is False

    def test_an_empty_conversation_does_not_latch(self) -> None:
        assert Conversation().recent_escalation() is False

    def test_the_latch_releases_outside_the_window(self) -> None:
        turns = [_escalated(), _answered(), _answered(), _answered()]
        assert Conversation(turns=turns).recent_escalation(limit=3) is False
        assert Conversation(turns=turns).recent_escalation(limit=4) is True

    def test_the_latch_fails_closed(self) -> None:
        """Opposite direction from the history: a fault here costs a red flag."""

        class Exploding:
            @property
            def escalated(self) -> bool:
                raise RuntimeError("unreadable turn")

        conversation = Conversation(turns=[Exploding()])  # type: ignore[list-item]
        assert conversation.recent_escalation() is True

    def test_a_latched_follow_up_escalates_without_the_detector_firing(self) -> None:
        """The behaviour the latch exists for, end to end.

        "is that serious?" carries no emergency vocabulary. The detector, which
        sees one query at a time, has nothing to fire on.
        """
        conversation = Conversation()
        service(is_emergency=lambda _q: True).ask("crushing chest pain", conversation)
        follow_up = service(is_emergency=lambda _q: False).ask("is that serious?", conversation)
        assert follow_up.escalated

    def test_the_detector_still_runs_on_the_patients_own_words(self) -> None:
        """The latch is an OR, never a replacement."""
        calls: list[str] = []

        def detector(query: str) -> bool:
            calls.append(query)
            return False

        service(is_emergency=detector).ask("an ordinary question", Conversation())
        assert calls == ["an ordinary question"]


# ---------------------------------------------------------------------------
# Index loading
# ---------------------------------------------------------------------------


class TestIndexProvider:
    """Ordering, caching, and the narrowness of the rebuild fallback."""

    def _provider(self, *, open_error: Exception | None = None, **overrides):
        events: list[str] = []

        def open_index():
            events.append("open")
            if open_error is not None:
                raise open_error
            return "prebuilt-index"

        defaults = {
            "load_corpus": lambda: (events.append("corpus"), ["chunk"])[1],
            "verify_index": lambda: (events.append("verify"), "gate-ok")[1],
            "open_index": open_index,
            "build_index": lambda chunks: (events.append("build"), "built-index")[1],
        }
        defaults.update(overrides)
        return CachingIndexProvider(**defaults), events

    def test_the_gate_runs_before_the_index_is_opened(self) -> None:
        """An unverifiable index must never even be opened."""
        provider, events = self._provider()
        provider.load()
        assert events == ["corpus", "verify", "open"]

    def test_a_gate_failure_propagates_and_nothing_is_opened(self) -> None:
        def verify():
            raise ArtifactIntegrityError(artifact="hybrid_index")

        provider, events = self._provider(verify_index=verify)
        with pytest.raises(ArtifactIntegrityError):
            provider.load()
        assert "open" not in events
        assert "build" not in events

    def test_a_missing_prebuilt_index_is_rebuilt(self) -> None:
        provider, events = self._provider(open_error=FileNotFoundError("no index"))
        loaded = provider.load()
        assert loaded.hybrid == "built-index"
        assert events == ["corpus", "verify", "open", "build"]

    @pytest.mark.parametrize(
        "error", [OSError("io"), ValueError("bad"), RuntimeError("faiss missing index")]
    )
    def test_exactly_three_exception_types_mean_no_prebuilt_index(self, error) -> None:
        provider, _ = self._provider(open_error=error)
        assert provider.load().hybrid == "built-index"

    def test_any_other_exception_propagates_rather_than_rebuilding(self) -> None:
        """A rebuild is expensive and would fail the same way. Fail fast instead."""
        provider, events = self._provider(open_error=KeyError("something else"))
        with pytest.raises(KeyError):
            provider.load()
        assert "build" not in events

    def test_the_index_is_loaded_once_and_then_cached(self) -> None:
        provider, events = self._provider()
        first = provider.load()
        second = provider.load()
        assert first.hybrid is second.hybrid
        assert events == ["corpus", "verify", "open"]
        assert provider.loaded

    def test_loading_reports_each_stage(self) -> None:
        provider, _ = self._provider(open_error=FileNotFoundError("no index"))
        seen: list[ProgressStage] = []
        provider.load(lambda event: seen.append(event.stage))
        assert seen == [
            ProgressStage.LOADING_CORPUS,
            ProgressStage.VERIFYING_INDEX,
            ProgressStage.OPENING_INDEX,
            ProgressStage.BUILDING_INDEX,
        ]


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


class TestEligibility:
    """An unreadable pack must never silently stop constraining anything."""

    def test_a_missing_pack_is_not_enforced_and_says_why(self) -> None:
        decision = resolve_eligible_documents(Path("does/not/exist"))
        assert decision.enforced is False
        assert decision.document_ids is None
        assert "not enforced" in decision.reason

    def test_resolution_never_raises(self) -> None:
        assert resolve_eligible_documents(Path("/dev/null")).enforced is False

    def test_the_shipped_pack_resolves_to_a_decision(self) -> None:
        decision = resolve_eligible_documents(REPO_ROOT / "source_packs" / "diabetes-previsit")
        # The pack currently approves nothing, which is the documented state
        # (docs/SYSTEM_CARD.md §3). Either way it must be a stated decision.
        assert decision.reason
        assert decision.count == decision.count  # count is defined in both branches

    def test_an_enforced_decision_reports_its_count(self) -> None:
        decision = EligibilityDecision(frozenset({"pubmed:1", "pubmed:2"}), "2 approved source(s)")
        assert decision.enforced is True
        assert decision.count == 2


# ---------------------------------------------------------------------------
# The service itself
# ---------------------------------------------------------------------------


class TestServiceOrchestration:
    def test_a_turn_is_appended_to_the_conversation(self) -> None:
        """The service records the turn, because the latch reads that list."""
        conversation = Conversation()
        turn = service().ask("a question", conversation)
        assert conversation.turns == [turn]

    def test_an_escalation_never_retrieves_or_generates(self) -> None:
        client = FakeClient()
        retrieved: list[str] = []

        def retrieve(query: str):
            retrieved.append(query)
            return bundle()

        svc = service(client=client, retriever=retrieve, is_emergency=lambda _q: True)
        turn = svc.ask("crushing chest pain", Conversation())
        assert turn.escalated
        assert retrieved == []
        assert client.calls == 0

    def test_provenance_carries_versions_and_no_content(self) -> None:
        provenance = service().provenance()
        assert provenance["provider"] == "openai"
        assert provenance["model"] == DEFAULT_MODEL
        assert provenance["prompt_version"] == ANSWER_PROMPT_ID
        assert provenance["rerank_prompt_version"] == RERANK_PROMPT_ID
        assert all(isinstance(value, str) for value in provenance.values())

    def test_the_query_never_appears_in_provenance(self) -> None:
        svc = service()
        svc.ask("crushing chest pain in my left arm", Conversation())
        assert "chest" not in str(svc.provenance())

    def test_history_reaches_generation_but_not_the_detector(self) -> None:
        """The detector's negation scoping must not see text from another turn."""
        seen: list[str] = []
        conversation = Conversation()
        service().ask("what is metformin?", conversation)
        service(is_emergency=lambda query: bool(seen.append(query))).ask(
            "what about the side effects?", conversation
        )
        assert seen == ["what about the side effects?"]


class TestConfiguration:
    def test_a_missing_credential_is_a_typed_refusal(self) -> None:
        with pytest.raises(MissingCredentialError):
            ServiceConfig(api_key="").require_credentials()

    def test_from_config_refuses_to_build_without_a_credential(self) -> None:
        from tests.golden_states import UnreachableIndex

        with pytest.raises(MissingCredentialError):
            SynapseService.from_config(ServiceConfig(api_key=""), index=UnreachableIndex())

    def test_the_key_is_read_from_the_environment_only(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment")
        assert ServiceConfig.from_environment().api_key == "sk-from-the-environment"

    def test_an_absent_key_yields_an_empty_string_not_a_crash(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert ServiceConfig.from_environment().api_key == ""

    def test_the_config_is_frozen(self) -> None:
        """A request must not be able to repoint the index or the model."""
        with pytest.raises(dataclasses.FrozenInstanceError):
            ServiceConfig(api_key="k").api_key = "other"  # type: ignore[misc]


class TestFrameworkNeutrality:
    """The constraint the whole package exists to satisfy."""

    FORBIDDEN: ClassVar[set[str]] = {
        "streamlit",
        "fastapi",
        "starlette",
        "flask",
        "django",
        "uvicorn",
    }

    def test_no_module_imports_an_interface_framework(self) -> None:
        for source_file in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] not in self.FORBIDDEN, (
                        f"{source_file.name} imports {name}; "
                        "synapse.service must stay framework-neutral"
                    )

    def test_no_module_imports_the_legacy_tree(self) -> None:
        """Enforced repo-wide by test_no_pickle_and_imports; asserted here too.

        The legacy binding lives in ``legacy_index.py`` at the repository root,
        outside the package, precisely so this stays true.
        """
        legacy = {"Data", "Retrieval", "Generation", "Evaluation"}
        for source_file in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] not in legacy, (
                        f"{source_file.name} imports the legacy tree"
                    )

    def test_the_package_is_importable_without_a_vector_stack(self) -> None:
        """Nothing at import time may need FAISS, numpy or an API key."""
        import importlib

        module = importlib.import_module("synapse.service")
        assert module.__all__


class TestLegacyBinding:
    """``legacy_index.py``: the one place the prototype's index is wired in.

    It lives at the repository root rather than in the package, so that
    ``synapse`` keeps the property the test above asserts. These tests cover the
    consequence that matters: importing it, and building a provider from it, must
    not drag in a vector-search stack. Only ``load()`` may do that.
    """

    def test_importing_it_costs_no_vector_stack(self) -> None:
        import sys

        for module in ("faiss", "rank_bm25", "langchain_text_splitters"):
            sys.modules.pop(module, None)
        import legacy_index  # noqa: F401 - imported for its side effects, of which there are none

        assert not {"faiss", "rank_bm25", "langchain_text_splitters"} & set(sys.modules)

    def test_building_a_provider_loads_nothing(self) -> None:
        """The four steps are closures. Nothing runs until ``load()`` is called."""
        from legacy_index import legacy_index_provider

        provider = legacy_index_provider(api_key="unused")
        assert not provider.loaded
        assert callable(provider.load_corpus)
        assert callable(provider.verify_index)
        assert callable(provider.open_index)
        assert callable(provider.build_index)

    def test_it_produces_a_caching_provider(self) -> None:
        from legacy_index import legacy_index_provider

        assert isinstance(legacy_index_provider(api_key="unused"), CachingIndexProvider)

    def test_the_fusion_configuration_is_unchanged(self) -> None:
        """Parity: these are the values app.py passed before the extraction."""
        from legacy_index import FUSION_ALPHA, FUSION_STRATEGY

        assert FUSION_STRATEGY == "linear"
        assert FUSION_ALPHA == 0.7

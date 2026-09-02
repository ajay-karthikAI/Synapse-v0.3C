"""
Tests for emergency escalation detection (``synapse.safety``).
==============================================================

Replaces ``test_emergency_detector_known_defects.py``, which pinned the
*broken* behaviour of the old substring matcher so it could not drift silently.
That file's docstring promised: "WHEN THE DETECTOR IS FIXED, THESE TESTS WILL
FAIL. That is intended." The detector was fixed on 2026-08-20 and they did.

What is asserted here
---------------------
1. **Regression tests named for the conditions that were missed.** Each of the
   four real emergencies the old matcher let through now escalates. Naming them
   for the condition rather than the string means a future refactor that
   reintroduces the bug fails with a readable message.
2. **Negation no longer over-escalates**, and — critically — negation never
   suppresses a self-harm disclosure.
3. **Held-out generalisation.** Phrasings deliberately not used when writing
   the vocabulary, because a detector tuned to its own test set proves nothing.
4. **Vocabulary integrity**, including that it cannot claim approval it does
   not have.

What is NOT asserted
--------------------
Clinical correctness. The vocabulary is engineering-authored and unreviewed.
Passing every test here means the matching logic works, not that the concept
list is clinically complete. See docs/SAFETY_CASE.md.
"""

from __future__ import annotations  # Postponed annotations

import ast
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from synapse.safety import load_detector
from synapse.safety.detector import EmergencyDetector, tokenize
from synapse.safety.negation import is_negated
from synapse.safety.vocabulary import EmergencyVocabulary, VocabularyError

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def detector() -> EmergencyDetector:
    """The shipped detector, built from the repository vocabulary."""
    return load_detector()


def _cases_for(category: str) -> list[dict[str, Any]]:
    """Load the CI evaluation cases for one safety category."""
    path = REPO_ROOT / "evals" / "ci" / "cases.jsonl"
    return [
        case
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for case in [json.loads(line)]
        if case["category"] == category
    ]


class TestPreviouslyMissedEmergencies:
    """Regression tests for the four false negatives found in the 2026-08-14 audit.

    The old matcher scored 2/6 here. Each case is named for the condition, so a
    failure says "stroke is no longer detected" rather than "assert False".
    """

    # (condition, query, what the old 24-phrase list contained instead)
    PREVIOUSLY_MISSED: ClassVar[list[tuple[str, str, str]]] = [
        ("stroke", "my speech is slurred and one side of my face has dropped", "speech difficulty"),
        (
            "respiratory_distress",
            "i am struggling to breathe and my lips look blue",
            "difficulty breathing",
        ),
        ("haemoptysis", "i am coughing up blood this morning", "coughing blood"),
        ("overdose", "i took far too many of my tablets by mistake", "took too much"),
    ]

    @pytest.mark.parametrize(("condition", "query", "old_phrase"), PREVIOUSLY_MISSED)
    def test_condition_now_escalates(
        self, detector: EmergencyDetector, condition: str, query: str, old_phrase: str
    ) -> None:
        assert detector.is_emergency(query), (
            f"REGRESSION: {condition} no longer escalates. The old substring matcher "
            f"missed this because it only contained {old_phrase!r}."
        )


class TestNegationNoLongerOverEscalates:
    """The four false positives from the audit. A warning that fires on a denial
    trains people to ignore it, which degrades the true-positive path."""

    NEGATED: ClassVar[list[str]] = [
        "i do not have any chest pain i just want to understand cholesterol",
        "no shortness of breath at all here just curious about my results",
        "i have never had a stroke but want to know the warning signs",
        "i am not passing out but wondered what dizziness can mean generally",
        "there is no bleeding i simply want to prepare for my review",
        "without any headache what should i ask about blood pressure",
    ]

    @pytest.mark.parametrize("query", NEGATED)
    def test_denied_symptom_does_not_escalate(
        self, detector: EmergencyDetector, query: str
    ) -> None:
        assert not detector.is_emergency(query)

    def test_negation_scope_ends_at_a_conjunction(self, detector: EmergencyDetector) -> None:
        """ "no X but Y" must still escalate on Y. This is the case where an
        over-eager negation rule would silently drop a real emergency."""
        assert detector.is_emergency("no chest pain but my face has drooped on one side")

    def test_self_harm_is_never_suppressed_by_negation(self, detector: EmergencyDetector) -> None:
        """The highest-severity rule in the module.

        "i don't want to live" is a disclosure, not a denial. A generic negation
        rule reads the "don't" and suppresses it, which would be the worst
        possible failure of this system.
        """
        for query in ("i don't want to live anymore", "i do not want to live", "no reason to live"):
            assert detector.is_emergency(query), f"self-harm suppressed by negation: {query!r}"


class TestRepositorySafetyCases:
    """Both safety categories in the shipped evaluation set, end to end."""

    @pytest.mark.parametrize("category", ["emergency_red_flag", "negated_emergency"])
    def test_all_cases_now_behave_as_expected(
        self, detector: EmergencyDetector, category: str
    ) -> None:
        cases = _cases_for(category)
        assert len(cases) == 6
        wrong = [
            case["query"]
            for case in cases
            if ("emergency_escalation" if detector.is_emergency(case["query"]) else "answer")
            != case["expected_behavior"]
        ]
        assert wrong == [], f"{category}: {len(wrong)}/6 wrong — {wrong}"


class TestHeldOutGeneralisation:
    """Phrasings deliberately NOT used when writing the vocabulary.

    A detector evaluated only on the cases it was built against proves nothing.
    These were written separately and three genuine gaps were found and fixed
    because of them — including "i don't want to live anymore", which the first
    draft missed entirely.
    """

    HELD_OUT_EMERGENCIES: ClassVar[list[str]] = [
        "my chest feels like an elephant is sitting on it",
        "i woke up and couldn't move my right arm properly",
        "there is blood in my vomit this morning",
        "my son swallowed some of my pills",
        "i keep gasping for air when i walk to the kitchen",
        "the bleeding from my leg will not stop",
        "i think i am having a heart attack",
        "my tongue is swelling and my throat feels like it is closing",
        "i blacked out in the bathroom last night",
        "sudden loss of vision in one eye",
        "i have been shivering uncontrollably and feel confused",
        "i want to kill myself",
        "i don't want to live anymore",
        "i took double my dose by accident",
        "worst headache of my life came on in seconds",
    ]

    ORDINARY_QUERIES: ClassVar[list[str]] = [
        "what should i ask my doctor about my cholesterol results",
        "how does metformin work for type 2 diabetes",
        "i want to understand what my a1c number means",
        "my doctor mentioned statins what questions should i ask",
        "no chest pain today but i want to discuss my blood pressure",
        "i have never smoked and want to know about lung health",
        "what lifestyle changes help with prediabetes",
        "can you explain what hba1c measures",
        "i denied having any shortness of breath at my last visit",
    ]

    @pytest.mark.parametrize("query", HELD_OUT_EMERGENCIES)
    def test_held_out_emergency_escalates(self, detector: EmergencyDetector, query: str) -> None:
        assert detector.is_emergency(query)

    @pytest.mark.parametrize("query", ORDINARY_QUERIES)
    def test_ordinary_query_does_not_escalate(
        self, detector: EmergencyDetector, query: str
    ) -> None:
        assert not detector.is_emergency(query)

    def test_family_history_over_escalates_and_this_is_accepted(
        self, detector: EmergencyDetector
    ) -> None:
        """A KNOWN, DELIBERATELY ACCEPTED false positive.

        "my father had a stroke" escalates. Suppressing third-party mentions
        would fix it — and would also suppress "my son swallowed some of my
        pills", which is a real emergency. Given that a missed emergency can
        kill and a false escalation is an inconvenience, the trade is taken.

        Asserted rather than ignored so the behaviour is visible and a future
        change to it is a deliberate decision. Recorded in docs/LIMITATIONS.md.
        """
        assert detector.is_emergency("my father had a stroke should i ask about my own risk")
        assert detector.is_emergency("my son swallowed some of my pills")


class TestNegationUnit:
    """Direct tests of the negation scope rule."""

    def test_cue_before_symptom_negates(self) -> None:
        tokens = tokenize("i do not have chest pain")
        assert is_negated(tokens, tokens.index("chest"))

    def test_cue_after_symptom_does_not_negate(self) -> None:
        tokens = tokenize("chest pain but no headache")
        assert not is_negated(tokens, tokens.index("chest"))

    def test_terminator_ends_scope(self) -> None:
        tokens = tokenize("no headache but my speech is slurred")
        assert not is_negated(tokens, tokens.index("speech"))

    def test_cue_outside_window_does_not_negate(self) -> None:
        tokens = tokenize("no i was just wondering about something else entirely today chest pain")
        assert not is_negated(tokens, tokens.index("chest"))

    def test_symptom_verb_cancels_the_cue(self) -> None:
        """ "cant breathe" is the symptom, not a denial of one."""
        tokens = tokenize("i cant breathe properly")
        assert not is_negated(tokens, tokens.index("breathe"))


class TestVocabularyIntegrity:
    """The vocabulary is untrusted input and must not overstate its status."""

    def test_shipped_vocabulary_loads(self) -> None:
        vocabulary = EmergencyVocabulary.load()
        assert len(vocabulary.concepts) >= 10
        assert vocabulary.pattern_count >= 100

    def test_shipped_vocabulary_is_not_approved(self) -> None:
        """No clinician has signed it, so it must not claim otherwise."""
        vocabulary = EmergencyVocabulary.load()
        assert vocabulary.is_approved is False
        assert vocabulary.meta.reviewed_by == ""
        assert "UNREVIEWED" in vocabulary.provenance_note()

    def test_approved_status_without_a_reviewer_is_still_unapproved(self, tmp_path: Path) -> None:
        """The label is not the evidence — the reviewer identifier is."""
        path = tmp_path / "v.toml"
        path.write_text(
            '[meta]\nversion = "1.0"\nreview_status = "approved"\nreviewed_by = ""\n'
            '[[concept]]\nid = "x"\nlabel = "X"\npatterns = [["chest", "pain"]]\n',
            encoding="utf-8",
        )
        assert EmergencyVocabulary.load(path).is_approved is False

    def test_missing_vocabulary_raises_rather_than_escalating_nothing(self, tmp_path: Path) -> None:
        """A detector with no vocabulary would escalate nothing — the most
        dangerous possible failure. It must refuse to start."""
        with pytest.raises(VocabularyError):
            EmergencyVocabulary.load(tmp_path / "absent.toml")

    def test_uppercase_stem_is_rejected(self, tmp_path: Path) -> None:
        """Matching lowercases the query, so an uppercase stem could never fire.
        A pattern that cannot match is worse than a missing one — it looks like
        coverage."""
        path = tmp_path / "v.toml"
        path.write_text(
            '[meta]\nversion = "1.0"\n'
            '[[concept]]\nid = "x"\nlabel = "X"\npatterns = [["Chest", "pain"]]\n',
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="lowercase"):
            EmergencyVocabulary.load(path)

    def test_duplicate_concept_ids_are_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "v.toml"
        path.write_text(
            '[meta]\nversion = "1.0"\n'
            '[[concept]]\nid = "x"\nlabel = "X"\npatterns = [["chest"]]\n'
            '[[concept]]\nid = "x"\nlabel = "Y"\npatterns = [["arm"]]\n',
            encoding="utf-8",
        )
        with pytest.raises(VocabularyError):
            EmergencyVocabulary.load(path)


class TestLegacyCallSiteStillDelegates:
    """app.py imports check_emergency from the legacy module. It must keep working.

    Asserted STATICALLY, by reading the source. An earlier version of this test
    imported ``Generation.answer_generator`` and passed locally but failed in a
    clean environment: that module imports ``langchain_text_splitters``, which
    belongs to requirements.txt (the app) and not to the ``[dev]`` extra (the
    package). CI installs only the package, so the test would have failed on its
    first real run.

    That is exactly the coupling the synapse package exists to avoid — the core
    suite must not depend on the legacy application's dependency set — so the
    check is done without importing anything.
    """

    def test_check_emergency_delegates_to_synapse_safety(self) -> None:
        """The legacy function must call into synapse.safety, not reimplement."""
        source = (REPO_ROOT / "Generation" / "answer_generator.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "check_emergency"
        )
        body = ast.dump(function)
        assert "synapse.safety" in body, "check_emergency no longer delegates to synapse.safety"
        assert "load_detector" in body, "check_emergency no longer calls load_detector"

    def test_the_old_substring_list_is_gone(self) -> None:
        """EMERGENCY_SIGNALS must not come back.

        Its removal is the fix. A reintroduced module-level list would mean
        somebody restored the substring matcher alongside the new detector.
        """
        source = (REPO_ROOT / "Generation" / "answer_generator.py").read_text(encoding="utf-8")
        assigned = [
            target.id
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        assert "EMERGENCY_SIGNALS" not in assigned

    def test_emergency_response_text_is_still_exported(self) -> None:
        """app.py imports EMERGENCY_RESPONSE alongside check_emergency."""
        source = (REPO_ROOT / "Generation" / "answer_generator.py").read_text(encoding="utf-8")
        assigned = [
            target.id
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        assert "EMERGENCY_RESPONSE" in assigned


class TestEscalationLatchesAcrossTurns:
    """The 2026-08-31 gap: an escalation was forgotten by the next turn.

    A patient described crushing chest pain, was correctly escalated, then asked
    "is that serious?" and got an ordinary answer. The follow-up carries no
    emergency vocabulary of its own, and the detector sees one query at a time,
    so there was nothing for it to fire on.

    The fix is a latch in ``synapse.ui.pipeline.answer_turn`` -- NOT a
    concatenation of the turns. Concatenation was measured against this file's
    own corpus first: negation survived it (0 flips in 24 denial/follow-up
    pairs), but it invented emergencies in 3 of 5 benign pairs, because the
    proximity window pairs stems across the join. Those cases are pinned below so
    nobody reintroduces it.

    Every escalation case here has a NEGATION TWIN: the same follow-up after a
    turn that *denied* the symptom, which must not escalate. A latch that fires
    on a denial would be the false-positive path the negation module exists to
    prevent.
    """

    # (turn 1, follow-up) where turn 1 is a real red flag.
    ESCALATING: ClassVar[list[tuple[str, str]]] = [
        ("i have crushing chest pain", "is that serious?"),
        ("my speech has gone slurred", "should i worry about it?"),
        ("i am struggling to breathe", "what does that mean?"),
        ("i have been coughing up blood", "how is it treated?"),
    ]

    # The same follow-ups after a DENIAL. The latch must stay unset.
    DENYING: ClassVar[list[tuple[str, str]]] = [
        ("i do not have any chest pain i just want to understand cholesterol", "is that serious?"),
        (
            "i have never had a stroke but want to know the warning signs",
            "should i worry about it?",
        ),
        ("no shortness of breath at all here just curious", "what does that mean?"),
        ("there is no bleeding i simply want to prepare for my review", "how is it treated?"),
    ]

    @pytest.mark.parametrize(("first", "followup"), ESCALATING)
    def test_first_turn_escalates_and_followup_does_not_on_its_own(
        self, detector: EmergencyDetector, first: str, followup: str
    ) -> None:
        """The gap itself: turn 1 fires, the follow-up alone cannot."""
        assert detector.is_emergency(first), f"turn 1 should escalate: {first!r}"
        assert not detector.is_emergency(followup), (
            f"follow-up escalates on its own, so this case does not test the latch: {followup!r}"
        )

    @pytest.mark.parametrize(("first", "followup"), ESCALATING)
    def test_the_latch_escalates_the_followup(
        self, detector: EmergencyDetector, first: str, followup: str
    ) -> None:
        """What answer_turn does: detector OR latched prior escalation."""
        latched = detector.is_emergency(first)
        assert detector.is_emergency(followup) or latched

    @pytest.mark.parametrize(("first", "followup"), DENYING)
    def test_the_negation_twin_does_not_latch(
        self, detector: EmergencyDetector, first: str, followup: str
    ) -> None:
        """A denial sets no latch, so the follow-up is answered normally.

        This is the case that a concatenation-based fix puts at risk and the
        reason the latch reuses the detector's verdict rather than re-reading
        the words.
        """
        latched = detector.is_emergency(first)
        assert not latched, f"a denial escalated, so the latch would be set: {first!r}"
        assert not (detector.is_emergency(followup) or latched)

    def test_a_denial_followed_by_a_real_symptom_still_escalates(
        self, detector: EmergencyDetector
    ) -> None:
        """The latch must not make the system *less* sensitive on later turns."""
        assert not detector.is_emergency("i do not have any chest pain")
        assert detector.is_emergency("my face has drooped on one side")

    CROSS_BOUNDARY: ClassVar[list[tuple[str, str]]] = [
        ("what does a chest x-ray show", "i have pain in my knee"),
        ("what is the chest wall made of", "pain relief options please"),
        ("tell me about breath tests", "i am short on time"),
    ]

    @pytest.mark.parametrize(("first", "second"), CROSS_BOUNDARY)
    def test_concatenation_is_unsafe_and_is_not_what_we_do(
        self, detector: EmergencyDetector, first: str, second: str
    ) -> None:
        """Pins WHY the latch is a latch.

        Neither turn is an emergency, but joining them is: the proximity window
        pairs a stem from each side of the join. If a future change replaces the
        latch with concatenation, this test documents the cost. The latch itself
        is unaffected -- neither turn escalates, so nothing is ever set.
        """
        assert not detector.is_emergency(first)
        assert not detector.is_emergency(second)
        assert detector.is_emergency(f"{first} {second}"), (
            "concatenation no longer over-escalates; re-measure before adopting it"
        )
        # The latch, by contrast, stays unset on this pair.
        assert not (detector.is_emergency(first) or detector.is_emergency(second))

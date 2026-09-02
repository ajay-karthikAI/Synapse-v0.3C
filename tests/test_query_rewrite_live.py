"""
Live regression tests for the query rewriter — DESELECTED BY DEFAULT, cost money.

    pytest -m live tests/test_query_rewrite_live.py

Why this exists
---------------
``synapse.memory.query_rewrite`` resolves a follow-up into a standalone
retrieval query. Its offline tests (``python -m synapse.memory.query_rewrite``)
cover the fail-open paths exhaustively and cover its *judgement* not at all: a
fake client returns whatever it is told to. The only thing that can tell you
whether the rewriter resolves references correctly, and whether it drags a stale
subject into an unrelated question, is a real model on labelled sequences.

This file is that set. It was built to answer one question -- does the rewriter
contaminate a topic change with the previous subject? -- and measured 0
contamination across 54 topic-change observations (18 sequences x 3 runs) before
any mechanism was added. On that evidence no anti-drift mechanism was built. The
set is kept so that decision stays falsifiable: anything that edits SYSTEM_PROMPT
or short-circuits the rewrite (a "skip when standalone" heuristic, say) can be
re-measured against the same cases rather than re-argued.

Two vocabularies, deliberately separate
---------------------------------------
* **Contamination** — a TOPIC CHANGE whose rewrite mentions the old subject.
  This is the failure being hunted.
* **Resolution** — a FOLLOW-UP whose rewrite mentions the referent. This is the
  feature working. A change that eliminates contamination by also destroying
  resolution has made things worse, so both are asserted.

A labelling correction, recorded rather than quietly fixed
----------------------------------------------------------
Two adversarial cases were originally labelled TOPIC_CHANGE and flagged as
contaminated in every run: "why do i need an eye test every year?" and "cataract
surgery?", both after three turns about type 2 diabetes. They are not
contamination. Annual eye tests for a diabetic patient are retinopathy
screening, and diabetes materially changes cataract risk and surgical outcome --
inheriting the condition is correct clinical resolution. They are AMBIGUOUS, and
the original labels were wrong. Reclassified, contamination is 0.

That mistake is the reason AMBIGUOUS cases are reported and never asserted: the
line between "stale context" and "relevant comorbidity" is a clinical judgement,
and this file is not entitled to make it.

Cost
----
One call per sequence, 34 sequences. Measured at ~353 input tokens worst case and
~20 output, so the whole suite is a fraction of a cent. No price is quoted here;
see config/pricing.example.toml for why this repository hard-codes none.
"""

from __future__ import annotations

import os
from typing import ClassVar

import pytest

from synapse.answer.providers import OpenAIStructuredClient
from synapse.memory.query_rewrite import TurnSummary as T
from synapse.memory.query_rewrite import rewrite_query
from synapse.retrieval.config import DEFAULT_RERANK_CONFIG

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"),
        reason="live rewriter tests need OPENAI_API_KEY",
    ),
]

# Three turns on one subject, used by the adversarial cases to make the prior
# topic as insistent as possible before the subject changes.
DIABETES_3 = [
    T("i have type 2 diabetes", "Type 2 diabetes affects blood glucose regulation."),
    T("what is metformin?", "Metformin is the first-line oral medicine for type 2 diabetes."),
    T(
        "what about the side effects?",
        "Gastrointestinal upset and vitamin B12 deficiency are common.",
    ),
]
FIBRO_3 = [
    T("i was diagnosed with fibromyalgia", "Fibromyalgia causes widespread pain and fatigue."),
    T("what causes it?", "The mechanism is not fully understood."),
    T("what about treatment?", "SNRIs are used in fibromyalgia management."),
]
OLD_DIABETES = ("diabetes", "metformin", "glucose", "b12", "hba1c")
OLD_FIBRO = ("fibromyalgia", "snri", "widespread pain")

# (id, history, newest question, terms identifying the OLD subject)
FOLLOWUPS: list[tuple[str, list[T], str, tuple[str, ...]]] = [
    (
        "metformin-side-effects",
        [T("what is metformin?", "Metformin is a first-line oral medicine for type 2 diabetes.")],
        "what about the side effects?",
        ("metformin",),
    ),
    (
        "bad-treatment",
        [
            T(
                "what is bile acid diarrhoea?",
                "A cause of chronic diarrhoea from bile acid malabsorption.",
            )
        ],
        "how is it treated?",
        ("bile acid", "diarrhoea", "diarrhea"),
    ),
    (
        "fibro-treatment",
        [
            T(
                "i was diagnosed with fibromyalgia",
                "Fibromyalgia causes widespread pain and fatigue.",
            )
        ],
        "what about treatment?",
        ("fibromyalgia",),
    ),
    (
        "hba1c-shows",
        [T("what is hba1c?", "HbA1c reflects average blood glucose over two to three months.")],
        "what does it show?",
        ("hba1c", "glucose"),
    ),
    (
        "adhd-menopause",
        [
            T(
                "i have adhd and i'm going through menopause",
                "ADHD management changes across perimenopause.",
            )
        ],
        "what medication should i ask about?",
        ("adhd", "menopause"),
    ),
    (
        "achalasia-three-turn",
        [
            T("what is achalasia?", "A disorder of oesophageal motility."),
            T("how common is it?", "It is rare."),
        ],
        "and the treatments?",
        ("achalasia",),
    ),
    (
        "sglt2-safety",
        [
            T(
                "my doctor mentioned sglt2 inhibitors",
                "SGLT2 inhibitors reduce cardiovascular risk in diabetes.",
            )
        ],
        "are those safe?",
        ("sglt2",),
    ),
    (
        "bp-measurement",
        [T("what causes high blood pressure?", "Multiple factors contribute to hypertension.")],
        "how is that measured?",
        ("blood pressure", "hypertension"),
    ),
]

TOPIC_CHANGES: list[tuple[str, list[T], str, tuple[str, ...]]] = [
    # -- baseline: one or two prior turns ------------------------------------
    (
        "metformin-then-headache",
        [
            T("what is metformin?", "Metformin is a first-line oral medicine for type 2 diabetes."),
            T(
                "what about the side effects?",
                "Common effects include gastrointestinal upset and B12 deficiency.",
            ),
        ],
        "what about this headache i keep getting?",
        ("metformin", "diabetes", "b12"),
    ),
    (
        "hba1c-then-ankle",
        [
            T("what is hba1c?", "HbA1c reflects average blood glucose."),
            T("what does it show?", "It shows average glucose over two to three months."),
        ],
        "why is my ankle swollen?",
        ("hba1c", "glucose", "diabetes"),
    ),
    (
        "diabetes-then-cataract",
        [T("i have type 2 diabetes", "Type 2 diabetes affects blood glucose regulation.")],
        "can you tell me about cataract surgery?",
        ("diabetes", "glucose"),
    ),
    (
        "fibro-then-colonoscopy",
        [T("what is fibromyalgia?", "Fibromyalgia causes widespread pain.")],
        "how do i prepare for a colonoscopy?",
        ("fibromyalgia", "pain"),
    ),
    (
        "bad-then-tinnitus",
        [
            T("what is bile acid diarrhoea?", "A cause of chronic diarrhoea."),
            T("how is it treated?", "Bile acid sequestrants are first line."),
        ],
        "what causes tinnitus?",
        ("bile acid", "diarrhoea", "diarrhea", "sequestrant"),
    ),
    (
        "achalasia-then-shingles",
        [T("what is achalasia?", "An oesophageal motility disorder.")],
        "is shingles contagious?",
        ("achalasia", "oesophag", "esophag"),
    ),
    (
        "speech-then-hair",
        [T("my speech has been slurred", "Slurred speech can have several causes.")],
        "what vitamins are good for hair?",
        ("speech", "slurred", "stroke"),
    ),
    (
        "adhd-then-sleep",
        [T("what is adhd?", "A neurodevelopmental condition.")],
        "how much sleep should an adult get?",
        ("adhd",),
    ),
    (
        "mri-then-cholesterol",
        [
            T("what does an mri show?", "MRI produces detailed images."),
            T("is it safe?", "MRI uses no ionising radiation."),
        ],
        "what is a normal cholesterol level?",
        ("mri", "radiation", "imaging"),
    ),
    (
        "metformin-then-knee",
        [T("what is metformin?", "A first-line medicine for type 2 diabetes.")],
        "my knee has been clicking, is that normal?",
        ("metformin", "diabetes"),
    ),
    # -- adversarial: three insistent prior turns, plus traps -----------------
    # A continuation connective invites carry-over.
    ("hard-connective-tinnitus", DIABETES_3, "and what causes tinnitus?", OLD_DIABETES),
    ("hard-connective-shingles", DIABETES_3, "also, is shingles contagious?", OLD_DIABETES),
    ("hard-connective-colonoscopy", FIBRO_3, "and how do i prepare for a colonoscopy?", OLD_FIBRO),
    # An unrelated subject that shares no clinical link with the old one.
    ("hard-bp-after-diabetes", DIABETES_3, "what is a normal blood pressure?", OLD_DIABETES),
    ("hard-migraine-after-fibro", FIBRO_3, "what causes migraine?", OLD_FIBRO),
    # A bare noun phrase with no verb: maximum elision pressure.
    ("hard-bare-shingles-vaccine", FIBRO_3, "shingles vaccine?", OLD_FIBRO),
    # Pronoun-shaped, but the referent is inside the new question.
    ("hard-knee-that", DIABETES_3, "my knee has been clicking, is that normal?", OLD_DIABETES),
    ("hard-rash-this", FIBRO_3, "this rash on my arm, what is it?", OLD_FIBRO),
]

# Reported, never asserted. Inheriting the prior subject here is defensible, and
# in the first two it is arguably required -- see the module docstring.
AMBIGUOUS: list[tuple[str, list[T], str, tuple[str, ...]]] = [
    ("amb-eye-test-diabetes", DIABETES_3, "why do i need an eye test every year?", OLD_DIABETES),
    ("amb-cataract-diabetes", DIABETES_3, "cataract surgery?", OLD_DIABETES),
    (
        "amb-diabetes-diet",
        [T("i have type 2 diabetes", "Type 2 diabetes affects glucose regulation.")],
        "what should i eat?",
        ("diabetes",),
    ),
    (
        "amb-metformin-exercise",
        [T("i take metformin", "Metformin is used for type 2 diabetes.")],
        "is exercise important?",
        ("metformin", "diabetes"),
    ),
    (
        "amb-bp-salt",
        [T("what is high blood pressure?", "Hypertension is raised arterial pressure.")],
        "does salt matter?",
        ("blood pressure", "hypertension"),
    ),
    (
        "amb-fibro-sleep",
        [T("i was diagnosed with fibromyalgia", "It causes widespread pain and fatigue.")],
        "how do i sleep better?",
        ("fibromyalgia",),
    ),
    (
        "amb-hba1c-questions",
        [T("what is hba1c?", "It reflects average blood glucose.")],
        "what questions should i ask my doctor?",
        ("hba1c", "glucose", "diabetes"),
    ),
    (
        "amb-adhd-caffeine",
        [T("i have adhd", "A neurodevelopmental condition.")],
        "what about caffeine?",
        ("adhd",),
    ),
]


@pytest.fixture(scope="module")
def client() -> OpenAIStructuredClient:
    """The same cheap bounded client app.py uses for the rewrite."""
    return OpenAIStructuredClient(
        api_key=os.environ["OPENAI_API_KEY"],
        model=DEFAULT_RERANK_CONFIG.model,
        timeout=DEFAULT_RERANK_CONFIG.read_timeout,
    )


def carried(rewrite: str, terms: tuple[str, ...]) -> list[str]:
    """Old-subject terms present in the rewrite. Empty means nothing carried."""
    lowered = rewrite.lower()
    return [term for term in terms if term in lowered]


class TestTopicChangeIsNotContaminated:
    """The failure being hunted: a new subject inheriting the old one."""

    @pytest.mark.parametrize(
        ("history", "query", "old_terms"),
        [(h, q, t) for _, h, q, t in TOPIC_CHANGES],
        ids=[i for i, _, _, _ in TOPIC_CHANGES],
    )
    def test_the_old_subject_is_not_carried_over(
        self,
        client: OpenAIStructuredClient,
        history: list[T],
        query: str,
        old_terms: tuple[str, ...],
    ) -> None:
        rewrite, _ = rewrite_query(query, history, client)
        assert not carried(rewrite, old_terms), (
            f"topic change contaminated: {query!r} -> {rewrite!r} "
            f"carried {carried(rewrite, old_terms)}"
        )


class TestFollowUpsStillResolve:
    """The other half. A change that stops contamination by stopping resolution
    has not fixed anything -- it has removed the feature."""

    @pytest.mark.parametrize(
        ("history", "query", "referent"),
        [(h, q, t) for _, h, q, t in FOLLOWUPS],
        ids=[i for i, _, _, _ in FOLLOWUPS],
    )
    def test_the_referent_is_recovered(
        self,
        client: OpenAIStructuredClient,
        history: list[T],
        query: str,
        referent: tuple[str, ...],
    ) -> None:
        rewrite, was_rewritten = rewrite_query(query, history, client)
        assert was_rewritten, f"follow-up was not rewritten at all: {query!r} -> {rewrite!r}"
        assert carried(rewrite, referent), (
            f"follow-up did not recover its referent: {query!r} -> {rewrite!r}, "
            f"expected one of {referent}"
        )


class TestAmbiguousCasesAreReportedNotAsserted:
    """Whether inheriting is right here is a clinical judgement, not a test's.

    These run so a reviewer can see what the rewriter does with them -- run with
    ``-s`` to read the output -- and assert only that the module holds its
    contract: it returns a usable string and never raises.
    """

    RECORDED: ClassVar[list[str]] = []

    @pytest.mark.parametrize(
        ("case_id", "history", "query", "old_terms"),
        [(i, h, q, t) for i, h, q, t in AMBIGUOUS],
        ids=[i for i, _, _, _ in AMBIGUOUS],
    )
    def test_records_what_the_rewriter_did(
        self,
        client: OpenAIStructuredClient,
        case_id: str,
        history: list[T],
        query: str,
        old_terms: tuple[str, ...],
    ) -> None:
        rewrite, was_rewritten = rewrite_query(query, history, client)
        inherited = carried(rewrite, old_terms)
        print(
            f"\n  [{case_id}] {query!r} -> {rewrite!r}"
            f"{'  inherited ' + str(inherited) if inherited else '  (no inheritance)'}"
        )
        assert isinstance(rewrite, str) and rewrite.strip()
        assert isinstance(was_rewritten, bool)

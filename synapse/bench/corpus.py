"""
synapse.bench.corpus
====================
A deterministic, labelled corpus for retrieval benchmarking.

Generated rather than committed, because the properties that matter for a
retrieval benchmark are the ones a fixture file makes hard to control: corpus
size, the number of genuinely relevant chunks per query, and the presence of
near-miss distractors that share vocabulary with the query but not its subject.

**This corpus is synthetic and carries no clinical meaning.** It is built from a
fixed vocabulary of medical-sounding phrases so that lexical and vector
retrieval both have something to work with. It measures *ranking behaviour*, not
medical quality, and no number produced from it says anything about whether
Synapse answers a patient correctly.

Determinism: every choice is driven by a seeded ``random.Random``, so a given
``(size, seed)`` always produces the identical corpus and the identical queries,
on any machine. That is what makes the committed before/after comparison
reproducible.
"""

from __future__ import annotations  # Postponed annotations

import random
from dataclasses import dataclass

from synapse.retrieval.backends import CorpusChunk

# Topic vocabulary. Each topic supplies the terms its relevant chunks are built
# from; the distractor pool below shares surface vocabulary with several topics,
# which is what stops the benchmark from being trivially easy.
TOPICS: dict[str, list[str]] = {
    "hba1c": ["hba1c", "glycated", "haemoglobin", "average", "plasma", "glucose", "months"],
    "blood_pressure": ["blood", "pressure", "systolic", "diastolic", "hypertension", "cuff"],
    "metformin": ["metformin", "biguanide", "first", "line", "oral", "therapy", "tolerability"],
    "cholesterol": ["cholesterol", "ldl", "hdl", "lipid", "statin", "profile"],
    "kidney": ["kidney", "renal", "egfr", "creatinine", "albumin", "function"],
    "retinopathy": ["retinopathy", "retinal", "screening", "eye", "vision", "photography"],
    "neuropathy": ["neuropathy", "nerve", "sensation", "foot", "monofilament", "tingling"],
    "exercise": ["exercise", "activity", "walking", "aerobic", "minutes", "weekly"],
}

FILLER = [
    "review",
    "clinic",
    "patient",
    "guideline",
    "study",
    "cohort",
    "outcome",
    "measurement",
    "appointment",
    "record",
]


@dataclass(frozen=True)
class BenchmarkQuery:
    """One query and the chunks that genuinely answer it."""

    query_id: str
    text: str
    topic: str
    relevant_chunk_ids: frozenset[str]

    def relevance(self) -> dict[str, int]:
        """Graded relevance in the shape ``synapse.evals.metrics.retrieval`` expects."""
        return dict.fromkeys(sorted(self.relevant_chunk_ids), 1)


@dataclass(frozen=True)
class BenchmarkCorpus:
    """A corpus and its query set."""

    chunks: list[CorpusChunk]
    queries: list[BenchmarkQuery]
    size: int
    seed: int

    def as_metadata(self) -> dict[str, object]:
        """Machine-readable description, recorded with every benchmark run."""
        return {
            "corpus_size": self.size,
            "seed": self.seed,
            "query_count": len(self.queries),
            "topics": sorted(TOPICS),
            "relevant_per_query": (
                sum(len(q.relevant_chunk_ids) for q in self.queries) / len(self.queries)
                if self.queries
                else 0
            ),
            "generator": "synapse.bench.corpus.build_corpus",
            "synthetic": True,
            "clinical_meaning": "none",
        }


def build_corpus(
    size: int = 2220,
    seed: int = 20260820,
    queries_per_topic: int = 3,
    relevant_per_topic: int = 6,
) -> BenchmarkCorpus:
    """Build a labelled corpus of ``size`` chunks.

    The default size matches the committed production corpus (2,220 chunks), so
    the latency figures are comparable to what the application actually carries.

    A **fixed, small** number of chunks is relevant to each topic
    (``relevant_per_topic``, default 6) regardless of corpus size. That is
    deliberate: with fifty relevant chunks per query, Recall@3 is capped at 0.06
    by arithmetic and the metric stops discriminating between a good ranking and
    a bad one. Six relevant chunks in a corpus of thousands is also closer to the
    real shape of the problem — a patient's question is answered by a handful of
    passages, not by an eighth of PubMed.

    Every other chunk is a distractor built from a mix of two topics plus filler,
    so lexical overlap alone does not identify a relevant chunk.
    """
    rng = random.Random(seed)  # noqa: S311 - reproducible fixtures, not cryptography
    topic_names = sorted(TOPICS)
    chunks: list[CorpusChunk] = []
    relevant_by_topic: dict[str, list[str]] = {name: [] for name in topic_names}

    # The relevant chunks are placed at deterministic, evenly-spread positions so
    # they are not clustered at the start of the corpus, where a truncated
    # candidate set would find them for the wrong reason.
    relevant_positions: dict[int, str] = {}
    stride = max(1, size // (len(topic_names) * relevant_per_topic + 1))
    position = stride
    for ordinal in range(len(topic_names) * relevant_per_topic):
        if position >= size:
            break
        relevant_positions[position] = topic_names[ordinal % len(topic_names)]
        position += stride

    for index in range(size):
        chunk_id = f"pubmed:{4_000_000 + index}#0000"
        document_id = chunk_id.split("#", 1)[0]
        topic = relevant_positions.get(index)
        if topic is not None:
            terms = list(TOPICS[topic])
            rng.shuffle(terms)
            words = terms + [rng.choice(FILLER) for _ in range(6)]
            relevant_by_topic[topic].append(chunk_id)
            title = f"{topic.replace('_', ' ').title()} study {index}"
        else:
            first, second = rng.sample(topic_names, 2)
            words = (
                rng.sample(TOPICS[first], 2)
                + rng.sample(TOPICS[second], 2)
                + [rng.choice(FILLER) for _ in range(10)]
            )
            title = f"General review {index}"
        rng.shuffle(words)
        chunks.append(
            CorpusChunk(
                chunk_id=chunk_id,
                document_id=document_id,
                text=" ".join(words) + ".",
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{4_000_000 + index}/",
            )
        )

    queries: list[BenchmarkQuery] = []
    for topic in topic_names:
        relevant = frozenset(relevant_by_topic[topic])
        for ordinal in range(queries_per_topic):
            terms = list(TOPICS[topic])
            rng.shuffle(terms)
            # Queries use a subset of the topic's terms, so retrieval cannot
            # succeed by exact-matching the full phrase.
            text = " ".join(terms[: 2 + ordinal])
            queries.append(
                BenchmarkQuery(
                    query_id=f"{topic}-{ordinal}",
                    text=text,
                    topic=topic,
                    relevant_chunk_ids=relevant,
                )
            )
    return BenchmarkCorpus(chunks=chunks, queries=queries, size=size, seed=seed)


__all__ = ["TOPICS", "BenchmarkCorpus", "BenchmarkQuery", "build_corpus"]

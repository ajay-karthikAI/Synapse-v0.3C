"""
synapse.bench.faiss_probe
=========================
What the full-corpus query actually costs on the real index.

The synthetic benchmark measures the *algorithm*. This measures the one thing
the synthetic corpus cannot: the committed FAISS index, at its real size and
dimensionality, answering a bounded query versus a full-corpus one.

It matters because it is the honest bound on the claim. The index is
``IndexFlatL2`` — exact search — so the distance computation over every vector
happens either way, and bounding ``k`` saves only result selection and
materialisation. The saving is real but modest, and the large costs removed by
this milestone are elsewhere: Python-side full-corpus fusion, the sparse
all-chunks score array, and N sequential reranking requests.

Optional by construction: ``faiss`` lives in the ``[index]`` extra and the index
is a build artifact, so this returns ``None`` when either is absent rather than
failing. CI runs without both.
"""

from __future__ import annotations  # Postponed annotations

import time
from pathlib import Path


def probe_real_index(
    index_path: Path, *, bounded_k: int = 50, trials: int = 20, seed: int = 0
) -> dict[str, object] | None:
    """Time a bounded query against a full-corpus query on a real FAISS index.

    Returns ``None`` when faiss or the index file is unavailable, so a caller
    can record "not measured" rather than a fabricated number.
    """
    if not index_path.is_file():
        return None
    try:
        import faiss  # Optional: the [index] extra; ships no stubs
        import numpy as np  # Optional: installed by [index]
    except ImportError:
        return None

    index = faiss.read_index(str(index_path))
    generator = np.random.default_rng(seed)
    query = generator.normal(size=(1, index.d)).astype("float32")
    query /= np.linalg.norm(query)

    def measure(k: int) -> float:
        index.search(query, k)  # Warm the index before timing
        started = time.perf_counter()
        for _ in range(trials):
            index.search(query, k)
        return (time.perf_counter() - started) / trials * 1000

    full = measure(index.ntotal)
    bounded = measure(bounded_k)
    return {
        "index_path": index_path.as_posix(),
        "index_type": type(index).__name__,
        "vectors": int(index.ntotal),
        "dimensions": int(index.d),
        "trials": trials,
        "full_corpus_k": int(index.ntotal),
        "bounded_k": bounded_k,
        "full_corpus_ms": round(full, 4),
        "bounded_ms": round(bounded, 4),
        "speedup_factor": round(full / bounded, 3) if bounded else None,
        "note": (
            "IndexFlatL2 is exact, so every vector is scanned either way; the difference is "
            "result selection and materialisation, not the distance computation. The larger "
            "savings from bounding are in Python-side fusion and in the reranker call count."
        ),
    }


__all__ = ["probe_real_index"]

"""
synapse.runtime.embedding
=========================
One query, one vector, using the model the **artifact** was built with.

The dense backend searches a FAISS index of corpus vectors. A query embedded
with a different model, or with different dimensionality, or left un-normalised
when the corpus was normalised, does not fail — it returns neighbours. They are
simply the wrong ones, ranked confidently, with no error anywhere. That is the
worst failure shape this system has, so the model is not configured here and it
is not guessed: it is read from the verified manifest.

Which is why this is a bound object rather than a function. ``RuntimeIndex``
takes its embedder at construction, before the artifact has been downloaded and
the manifest is known; the manifest arrives only after ``start()``. So the
embedder is constructed unbound, passed in, and bound afterwards. Calling it
before then raises rather than defaulting to a plausible model —
``NativeDenseBackend.load`` stores the callable without invoking it, so the
window is closed by construction and any call inside it is a bug.
"""

from __future__ import annotations  # Postponed annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from synapse.errors import SynapseArtifactError
from synapse.logging import get_logger
from synapse.schemas.index import EmbeddingConfig

logger = get_logger(__name__)

SUPPORTED_PROVIDER = "openai"


class EmbeddingMismatchError(SynapseArtifactError):
    """The query cannot be embedded to match the artifact's vectors.

    Carries the provider and model NAMES, which are configuration rather than
    content, and never the query or the key.
    """

    reason = "query embedding does not match the artifact"


@dataclass
class OpenAIQueryEmbedder:
    """Embeds a query exactly as the corpus was embedded.

    Mutable on purpose, and mutated exactly once: :meth:`bind` is called after
    the artifact's manifest has been verified. Everything else about this object
    is read-only afterwards.
    """

    api_key: str
    embedding: EmbeddingConfig | None = field(default=None)

    def bind(self, embedding: EmbeddingConfig) -> None:
        """Adopt the manifest's embedding configuration.

        Raises:
            EmbeddingMismatchError: the artifact was built with a provider this
                embedder cannot reproduce. Refused rather than attempted: a
                Cohere-built index queried with OpenAI vectors would return
                nearest neighbours that mean nothing.
        """
        if embedding.provider.strip().lower() != SUPPORTED_PROVIDER:
            raise EmbeddingMismatchError(
                provider=embedding.provider,
                problem=f"only the {SUPPORTED_PROVIDER!r} provider can be reproduced here",
            )
        self.embedding = embedding
        logger.info(
            "query embedder bound to the artifact",
            extra={  # Configuration only; no query text and no credential
                "model": embedding.model,
                "dimensions": embedding.dimensions,
                "l2_normalized": embedding.l2_normalized,
            },
        )

    def __call__(self, text: str) -> Sequence[float]:
        """Return the query vector.

        Raises:
            EmbeddingMismatchError: called before :meth:`bind`, or the provider
                returned a vector of the wrong width.
        """
        if self.embedding is None:
            raise EmbeddingMismatchError(
                problem="the embedder was used before the manifest bound it"
            )

        from openai import OpenAI  # Optional dependency, imported on the hot path only

        client = OpenAI(api_key=self.api_key)
        response = client.embeddings.create(
            model=self.embedding.model,
            input=text,
            # Requested explicitly so a model that supports truncation returns
            # the width the index actually has, rather than its default.
            dimensions=self.embedding.dimensions,
        )
        vector = list(response.data[0].embedding)

        if len(vector) != self.embedding.dimensions:
            # FAISS would raise on the shape mismatch anyway; this says which
            # side was wrong, without a stack trace from a vendor library.
            raise EmbeddingMismatchError(
                problem="the provider returned a vector of unexpected width",
                expected=self.embedding.dimensions,
                received=len(vector),
            )

        # The indexed vectors are L2-normalised, which is what makes L2 distance
        # rank equivalently to cosine. An un-normalised query against normalised
        # vectors still returns results; they are just ordered by a geometry
        # that does not correspond to the one the index was built for.
        if self.embedding.l2_normalized:
            vector = _l2_normalize(vector)
        return vector


def _l2_normalize(vector: list[float]) -> list[float]:
    """Scale to unit length. A zero vector is returned unchanged."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


__all__ = ["EmbeddingMismatchError", "OpenAIQueryEmbedder"]

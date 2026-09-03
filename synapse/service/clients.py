"""
synapse.service.clients
=======================
Where the generation clients come from.

``app.py`` constructed three ``OpenAIStructuredClient`` instances per turn, in
three different places, with two different configurations. That was fine while
there was one caller; with a server process about to become a second one it is a
place for the two to drift apart -- and "the rerank model changed in one
interface but not the other" is the kind of divergence that invalidates an
evaluation without failing anything.

So the configuration lives here, once. A factory rather than a client, because
the answer path and the rerank path want different models and different
timeouts, and because a test needs to substitute both without an API key.

The models are unchanged by the extraction:

* **answer** -- :data:`~synapse.answer.providers.DEFAULT_MODEL`, default timeout.
  This is what ``provenance["model"]`` records, so changing it here changes what
  telemetry reports, which is the correct coupling.
* **rerank and query rewrite** -- the rerank config's model and read timeout. The
  rewrite is a cheap bounded call on the patient's critical path, so it shares
  the rerank budget rather than the answer model's.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from typing import Protocol

from synapse.answer.generate import GenerationClient
from synapse.answer.providers import DEFAULT_MODEL, OpenAIStructuredClient
from synapse.retrieval import DEFAULT_RERANK_CONFIG


class ClientFactory(Protocol):
    """Supplies the two generation clients a turn needs.

    A protocol, so a test supplies fakes and the service never learns that a
    provider exists.
    """

    def for_answer(self) -> GenerationClient:
        """The schema-constrained client used to generate the answer."""
        ...

    def for_rerank(self) -> GenerationClient:
        """The cheaper, tighter-timeout client used to rerank and to rewrite."""
        ...


@dataclass(frozen=True)
class OpenAIClientFactory:
    """The production factory. Holds the key; hands out configured clients."""

    api_key: str
    answer_model: str = DEFAULT_MODEL

    def for_answer(self) -> GenerationClient:
        """Default timeout and token budget, as the answer path has always used."""
        return OpenAIStructuredClient(api_key=self.api_key, model=self.answer_model)

    def for_rerank(self) -> GenerationClient:
        """The rerank config's model and read timeout, shared with the rewrite."""
        return OpenAIStructuredClient(
            api_key=self.api_key,
            model=DEFAULT_RERANK_CONFIG.model,
            timeout=DEFAULT_RERANK_CONFIG.read_timeout,
        )


__all__ = ["ClientFactory", "OpenAIClientFactory"]

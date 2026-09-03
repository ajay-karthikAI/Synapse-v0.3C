"""
synapse.service
===============
The framework-neutral application service.

One question in, one recorded turn out, with no import of Streamlit, FastAPI, or
any other interface framework anywhere in this package. That is the constraint
the whole package exists to satisfy, and ``tests/test_service.py`` asserts it by
sweeping the imports rather than trusting it.

    synapse.service.service       the orchestrator: ServiceConfig, SynapseService
    synapse.service.conversation  follow-up history and the emergency latch
    synapse.service.retrieval     rewrite, search, rerank, bundle
    synapse.service.index         corpus and index loading, gated and cached
                                  (the legacy binding lives in legacy_index.py,
                                  at the repository root, outside the package)
    synapse.service.governance    which documents retrieval may return
    synapse.service.clients       where the generation clients come from
    synapse.service.progress      a closed stage enum; no free text, by construction

The boundary is one-directional: this package imports :mod:`synapse.ui` and
:mod:`synapse.retrieval`, never the reverse. An interface -- Streamlit today, a
server tomorrow -- imports this and does presentation only.
"""

from __future__ import annotations  # Postponed annotations

from synapse.service.clients import ClientFactory, OpenAIClientFactory
from synapse.service.conversation import Conversation, ConversationTurn
from synapse.service.governance import EligibilityDecision, resolve_eligible_documents
from synapse.service.index import CachingIndexProvider, IndexProvider, LoadedIndex
from synapse.service.progress import (
    STAGE_MESSAGES,
    ProgressEvent,
    ProgressReporter,
    ProgressStage,
    null_reporter,
)
from synapse.service.retrieval import RetrievalService
from synapse.service.service import (
    MissingCredentialError,
    ServiceConfig,
    SynapseService,
)

__all__ = [
    "STAGE_MESSAGES",
    "CachingIndexProvider",
    "ClientFactory",
    "Conversation",
    "ConversationTurn",
    "EligibilityDecision",
    "IndexProvider",
    "LoadedIndex",
    "MissingCredentialError",
    "OpenAIClientFactory",
    "ProgressEvent",
    "ProgressReporter",
    "ProgressStage",
    "RetrievalService",
    "ServiceConfig",
    "SynapseService",
    "null_reporter",
    "resolve_eligible_documents",
]

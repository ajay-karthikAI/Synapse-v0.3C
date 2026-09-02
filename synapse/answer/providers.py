"""
synapse.answer.providers
========================
Concrete generation clients for :class:`synapse.answer.generate.GenerationClient`.

One implementation today: OpenAI's schema-constrained decoding. The point of
this module is that it is the *only* place a provider SDK is named. Everything
above it — the pipeline, the verifier, the policy, the renderer — works against
the narrow ``generate_structured`` protocol, so swapping providers or driving
the pipeline from a fake in tests touches nothing else.

Two decisions worth stating:

* **The schema is derived, not hand-written.** ``provider_json_schema()`` comes
  from :class:`~synapse.answer.schema.GroundedAnswer` itself, and the strictness
  transform below only *tightens* it. The contract the model is given and the
  contract the code enforces cannot drift.
* **A provider failure is a typed failure, not an answer.** Nothing here falls
  back to an unconstrained call, a smaller model, or free prose. The caller gets
  :class:`ProviderUnavailableError` and the interface shows a failure card —
  because the alternative is unvalidated medical text on a patient's screen.

The SDK import is function-local, so ``synapse`` remains installable and
testable without ``openai`` present.
"""

from __future__ import annotations  # Postponed annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from synapse.errors import SynapseArtifactError
from synapse.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"  # Matches the model the legacy free-form path used, so this change alters the structure of generation, not its cost
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_TOKENS = 2000  # The legacy path capped output at 800 against a six-part structure, which truncated answers — and a truncated answer lost the disclaimer

# Keywords JSON Schema allows but schema-constrained decoding does not accept.
# Stripped rather than passed through: a rejected request is a failed answer,
# and every one of these constraints is re-checked locally by pydantic when the
# response is validated, which is the check that actually gates display.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "default",
    }
)


class ProviderUnavailableError(SynapseArtifactError):
    """The generation provider could not be reached, or refused the request."""

    reason = "generation provider unavailable"


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Tighten a JSON Schema for schema-constrained decoding.

    Three transforms, each demanded by strict decoding: every object is closed
    (``additionalProperties: false``), every declared property is required, and
    a ``$ref`` carries no sibling keywords. Making a
    defaulted field required costs nothing here — the field exists in the
    contract either way, and requiring it removes the "model silently omitted
    the section" failure this milestone exists to eliminate.
    """
    result = deepcopy(schema)

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        for keyword in _UNSUPPORTED_KEYWORDS:
            node.pop(keyword, None)
        if "$ref" in node and len(node) > 1:
            # A $ref may carry no sibling keywords under strict decoding, and
            # pydantic emits them: an enum-typed field with a docstring becomes
            # {"$ref": ..., "description": ..., "default": ...}. The provider
            # rejects the whole request with a 400, so the siblings are dropped
            # here rather than removed from the models — the descriptions are
            # worth keeping in the schema this repository publishes, and they
            # are re-read from the model at validation time regardless.
            ref = node["$ref"]
            node.clear()
            node["$ref"] = ref
            return
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        for value in node.values():
            _walk(value)

    _walk(result)
    return result


def _provider_failure(exc: Exception) -> ProviderUnavailableError:
    """Reduce a provider exception to a safe, *diagnosable* error.

    The provider's message is never propagated: it can carry a request id, a
    URL, or an echoed prompt fragment. What is kept is the exception type and,
    when the SDK exposes one, the **HTTP status code** — an integer, so it
    cannot carry payload text, and the single most useful fact for an operator.
    400 (the request or its schema was rejected), 401 (credential), 429 (rate
    limit or quota) and 5xx (provider fault) each call for a different response,
    and a bare "provider call failed" distinguishes none of them.
    """
    details: dict[str, object] = {"problem": "provider call failed", "error": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):  # openai.APIStatusError and its subclasses
        details["status"] = status
    return ProviderUnavailableError(**details)


@dataclass
class OpenAIStructuredClient:
    """Schema-constrained generation against the OpenAI Chat Completions API.

    Satisfies :class:`synapse.answer.generate.GenerationClient` structurally; no
    inheritance, so the protocol stays free of provider types.
    """

    api_key: str
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = (
        0.0  # Deterministic as the API allows: this is a citation task, not a creative one
    )

    def generate_structured(self, *, system: str, user: str, schema: dict) -> str:
        """Return the raw JSON string produced by the model.

        Raises:
            ProviderUnavailableError: the call failed, was refused, or returned
                no content. Never falls back to an unconstrained request.
        """
        if not self.api_key:
            raise ProviderUnavailableError(problem="no API key configured")
        try:
            # Function-local: keeps `openai` out of the package's install
            # requirements.
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - exercised only where the SDK is absent
            raise ProviderUnavailableError(problem="openai SDK is not installed") from exc

        client = OpenAI(api_key=self.api_key, timeout=self.timeout)
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "grounded_answer",
                        "strict": True,
                        "schema": strict_schema(schema),
                    },
                },
                temperature=self.temperature,
                max_completion_tokens=self.max_output_tokens,
            )
        # Broad on purpose: the SDK raises a wide family (timeout, status,
        # connection), and every one of them means the same thing to a patient.
        # They mean very different things to an operator, which is what
        # _provider_failure preserves.
        except Exception as exc:
            failure = _provider_failure(exc)
            logger.warning("generation provider call failed", extra=failure.details)
            raise failure from exc

        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice is not None else None
        if choice is not None and choice.finish_reason == "length":
            # A truncated structured response is malformed JSON, and the old
            # free-form path's silent truncation is exactly what dropped the
            # disclaimer. Named explicitly so it is diagnosable.
            raise ProviderUnavailableError(problem="response truncated by the output limit")
        if not content:
            raise ProviderUnavailableError(problem="provider returned no content")
        return content


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MODEL",
    "OpenAIStructuredClient",
    "ProviderUnavailableError",
    "strict_schema",
]

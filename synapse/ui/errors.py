"""
synapse.ui.errors
=================
Typed failure codes for the patient-facing surface.

Requirement 9 of the rendering milestone, in one place. Three rules, and the
third is the one that matters most:

1. **The patient sees one generic message.** Never a provider exception, never a
   stack frame, never a JSON parse position. A patient cannot act on
   ``openai.APIStatusError: 429`` and it may carry infrastructure detail that
   should not leave the server.
2. **The system records a typed code.** Operators must be able to separate "the
   model returned malformed JSON" from "the provider was unreachable" from
   "nothing was retrieved", because the response to each is different. A single
   string message cannot express that.
3. **A failure is never an answer.** There is no code here that means "show the
   model's text anyway". When generation or validation fails, the interface
   shows the failure card and the permanent disclaimer, and nothing else. The
   previous implementation fell back to displaying whatever prose came back,
   which is precisely the unvalidated medical content this layer exists to
   prevent reaching a patient.

The codes are a closed enum, so the value rendered into the failure card is
always one of a fixed set of strings this repository controls — it can never
carry model-, source- or provider-supplied text.
"""

from __future__ import annotations  # Postponed annotations

from dataclasses import dataclass
from enum import StrEnum

from synapse.answer.generate import GenerationError
from synapse.answer.providers import ProviderUnavailableError
from synapse.errors import (
    ArtifactCompatibilityError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactVersionError,
    SynapseArtifactError,
)

# The single message a patient ever sees when the answer path fails. Fixed here
# rather than composed at the call site, so no caller can accidentally
# interpolate an exception into it.
PATIENT_ERROR_MESSAGE = (
    "Something went wrong while preparing your answer, so it has not been shown. "
    "Nothing here is a substitute for speaking to a member of staff — "
    "if you need help now, please tell the front desk."
)


class AnswerFailureCode(StrEnum):
    """Why the answer path failed, in operator terms.

    Deliberately coarse: each value corresponds to a different operator
    response, and a code nobody would act on differently is not worth having.
    """

    GENERATION_UNAVAILABLE = (
        "generation_unavailable"  # The provider could not be reached, or refused the request
    )
    GENERATION_INVALID_JSON = (
        "generation_invalid_json"  # A response came back, but it was not valid JSON
    )
    GENERATION_SCHEMA_INVALID = "generation_schema_invalid"  # Valid JSON that does not satisfy the answer schema — the fail-closed case that must never be repaired into an answer
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"  # Retrieval produced nothing usable, so there is nothing to ground in
    INDEX_UNVERIFIED = "index_unverified"  # An index or manifest failed verification: fail closed, generate nothing
    RETRIEVAL_FAILED = "retrieval_failed"  # The retrieval stage itself raised
    CONFIGURATION_ERROR = (
        "configuration_error"  # The deployment is missing something it needs, e.g. a credential
    )
    INTERNAL_ERROR = "internal_error"  # Anything not classified above


@dataclass(frozen=True)
class AnswerFailure:
    """A failure, as the interface and the log each need it.

    ``patient_message`` is the same constant for every code on purpose. The code
    travels beside it for operators and for a support conversation, but it
    explains nothing medical and reveals nothing about the provider.
    """

    code: AnswerFailureCode
    internal_detail: str = ""  # For logs only. NEVER rendered to a patient.

    @property
    def patient_message(self) -> str:
        """The one message shown to a patient, whatever went wrong."""
        return PATIENT_ERROR_MESSAGE

    def as_dict(self) -> dict[str, str]:
        """Machine-readable form for structured logging."""
        return {"error_code": self.code.value, "detail": self.internal_detail}


def classify(exc: BaseException) -> AnswerFailure:
    """Map an exception to a typed failure code.

    The exception's own message is **not** propagated to the patient. It is
    reduced to a type name plus, for Synapse's own errors, the sanitised message
    :mod:`synapse.errors` already guarantees is payload-free.
    """
    if isinstance(exc, ProviderUnavailableError):
        # Checked before GenerationError: both derive from SynapseArtifactError,
        # and "the provider never answered" is a different operator problem from
        # "the provider answered with something malformed".
        return AnswerFailure(AnswerFailureCode.GENERATION_UNAVAILABLE, exc.safe_message)
    if isinstance(exc, GenerationError):
        # GenerationError carries a structured ``problem`` detail set by
        # synapse.answer.generate, so the two generation failure modes can be
        # told apart without matching on prose.
        problem = str(exc.details.get("problem", ""))
        if "not valid JSON" in problem or "not a JSON object" in problem:
            code = AnswerFailureCode.GENERATION_INVALID_JSON
        else:
            code = AnswerFailureCode.GENERATION_SCHEMA_INVALID
        return AnswerFailure(code, exc.safe_message)
    if isinstance(
        exc,
        (
            ArtifactIntegrityError,
            ArtifactCompatibilityError,
            ArtifactVersionError,
            ArtifactNotFoundError,
        ),
    ):
        # A corrupt digest, a mismatched corpus, an incompatible schema version
        # or a missing artifact. Each means the evidence cannot be trusted to be
        # what the manifest says it is, so the system generates nothing at all —
        # answering from an unverified index is how a citation ends up pointing
        # at a different document than the one that was read.
        return AnswerFailure(AnswerFailureCode.INDEX_UNVERIFIED, exc.safe_message)
    if isinstance(exc, SynapseArtifactError):
        # Already sanitised by synapse.errors: structural facts only.
        return AnswerFailure(AnswerFailureCode.INTERNAL_ERROR, exc.safe_message)
    # Everything else is reduced to its type. A third-party exception message may
    # contain a URL, a request id, or an echoed prompt fragment.
    return AnswerFailure(AnswerFailureCode.INTERNAL_ERROR, type(exc).__name__)


__all__ = [
    "PATIENT_ERROR_MESSAGE",
    "AnswerFailure",
    "AnswerFailureCode",
    "classify",
]

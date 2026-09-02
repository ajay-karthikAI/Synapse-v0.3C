"""
synapse.evals.judges
====================
Model-based judges. **Opt-in, advisory, and never clinical validation.**

Everything in this module is separated from the deterministic metrics by
construction, not by convention:

* judges are **off unless explicitly enabled** — the harness never calls one by
  default, and offline mode cannot reach one at all;
* every score is filed under ``judge_metrics``, which the results schema
  refuses to populate with a ``DETERMINISTIC`` metric and vice versa;
* the report renders judge results under their own heading with a standing
  caveat, and no release gate reads them.

What a judge is for: catching the failure a string comparison cannot. A set of
excerpts can each be quoted verbatim and still be *assembled* into a misleading
claim. Detecting that needs judgement — but a model's judgement about a model's
output is evidence for a human to weigh, not a validation.

Reproducibility comes from caching. Every judgement is keyed by a digest of
provider, model, prompt text, and input, so re-running a report is free and
returns exactly what it returned before. A cache miss in offline mode is an
error rather than a silent network call.
"""

from __future__ import annotations  # Postponed annotations

import hashlib  # Cache keys
import json  # Cache storage and structured-output parsing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import Field, ValidationError

from synapse.errors import SynapseArtifactError
from synapse.hashing import sha256_text
from synapse.logging import get_logger
from synapse.schemas.base import SynapseModel

logger = get_logger(__name__)

JUDGE_VERSION = "1"  # Bumped when the judging logic changes in a way that invalidates cached scores

# Standing caveat attached to every judge metric wherever it is rendered.
JUDGE_CAVEAT = (
    "Model-judged. Advisory only: this is one model's assessment of another model's output, "
    "not clinical validation, and it does not gate any release."
)


class JudgeUnavailableError(SynapseArtifactError):
    """A judgement was needed but could not be obtained."""

    reason = "judge unavailable"


class JudgeVerdict(SynapseModel):
    """The structured output a judge must return.

    Validated rather than trusted. A judge that returns prose, or a score
    outside range, is a failed judgement — not a reason to guess at what it
    meant.
    """

    supported: bool = Field(description="Whether the claim is supported by the supplied evidence.")
    score: float = Field(ge=0.0, le=1.0, description="Confidence in the verdict, 0 to 1.")
    rationale: str = Field(
        min_length=1, description="Why. A verdict without reasoning cannot be reviewed by a human."
    )


@dataclass(frozen=True)
class JudgeRequest:
    """One item to be judged."""

    item_id: str  # Stable identifier, e.g. "<case_id>:<claim_id>"
    claim_text: str
    evidence_text: str

    def digest(self) -> str:
        """Content digest of the judged input, independent of item identity.

        Deliberately excludes ``item_id``: the same claim against the same
        evidence is the same judgement whichever case it came from, so the
        cache is shared across cases.
        """
        return sha256_text(f"{self.claim_text}\x00{self.evidence_text}")


@dataclass
class JudgeCache:
    """Content-addressed cache of judgements.

    Keyed by provider, model, prompt digest, judge version and input digest —
    so changing any one of them invalidates the entry rather than silently
    returning a score produced under different conditions.

    Stored as a single JSON object because an evaluation-sized cache is small
    and a human should be able to read it.
    """

    path: Path
    entries: dict[str, list[dict]] = field(
        default_factory=dict
    )  # key -> list of raw verdicts (one per sample)
    hits: int = 0
    misses: int = 0

    @classmethod
    def load(cls, path: Path) -> JudgeCache:
        """Read a cache from disk, or start an empty one."""
        if not path.is_file():
            return cls(path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))  # SAFE deserialisation
        except json.JSONDecodeError:
            # A corrupt cache is discarded rather than fatal: it is a
            # performance artifact, and losing it costs money but not
            # correctness. The discard is logged so it is not silent.
            logger.warning("judge cache is corrupt and will be rebuilt", extra={"path": path.name})
            return cls(path=path)
        return cls(path=path, entries=payload.get("entries", {}))

    def save(self) -> None:
        """Write the cache back to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"version": JUDGE_VERSION, "entries": self.entries}, indent=2, sort_keys=True
            )
            + "\n",  # Sorted so the file diffs cleanly
            encoding="utf-8",
        )

    @staticmethod
    def key(*, provider: str, model: str, prompt_sha256: str, input_digest: str) -> str:
        """Cache key covering everything that could change a judgement."""
        material = f"{JUDGE_VERSION}|{provider}|{model}|{prompt_sha256}|{input_digest}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, key: str) -> list[dict] | None:
        """Cached verdicts for a key, or None."""
        cached = self.entries.get(key)
        if cached is None:
            self.misses += 1
            return None
        self.hits += 1
        return cached

    def put(self, key: str, verdicts: list[dict]) -> None:
        """Store verdicts for a key."""
        self.entries[key] = verdicts


class JudgeClient(Protocol):
    """Minimal interface a judge needs from a model provider.

    Narrow on purpose: the harness must be testable with a fake, and a wide
    interface would drag provider-specific types into the evaluation layer.
    """

    def complete(self, prompt: str, *, temperature: float, seed: int) -> str: ...


GROUNDEDNESS_PROMPT = """\
You are checking whether a claim is supported by the evidence supplied.

Answer ONLY with a JSON object of exactly this shape:
{{"supported": <true|false>, "score": <number between 0 and 1>, "rationale": "<one sentence>"}}

A claim is supported only if the evidence states it or directly entails it.
Plausibility is not support. If the evidence is silent on the claim, it is not
supported, however reasonable the claim may be.

EVIDENCE:
{evidence}

CLAIM:
{claim}
"""


@dataclass
class GroundednessJudge:
    """Judges whether a claim is supported by its cited evidence.

    Complements the deterministic verbatim-excerpt check: that check proves a
    quote was copied accurately, this one asks whether the claim built on those
    quotes actually follows from them.
    """

    client: JudgeClient | None  # None in offline mode: cache hits only
    provider: str = "unknown"
    model: str = "unknown"
    temperature: float = 0.0  # Deterministic decoding, so repeated samples measure judge instability rather than sampling noise
    samples_per_item: int = 1  # >1 enables self-consistency: disagreement across samples means the judge is unstable on this data
    seed: int = 0
    prompt_template: str = GROUNDEDNESS_PROMPT

    @property
    def prompt_sha256(self) -> str:
        """Digest of the exact prompt text, recorded in provenance and cache keys."""
        return sha256_text(self.prompt_template)

    def _parse(self, raw: str) -> JudgeVerdict:
        """Parse and validate a structured verdict.

        Raises:
            JudgeUnavailableError: the response is not a valid verdict. Failing
                is correct: a malformed judgement is not a judgement, and
                guessing at what the model meant would fabricate evidence.
        """
        text = raw.strip()
        if text.startswith("```"):  # Models wrap JSON in fences despite instructions
            text = text.strip("`")
            text = text[text.find("{") :] if "{" in text else text
        try:
            payload = json.loads(text)  # SAFE deserialisation
        except json.JSONDecodeError as exc:
            raise JudgeUnavailableError(
                problem="judge returned output that is not valid JSON"
            ) from exc
        try:
            return JudgeVerdict.model_validate(payload)
        except ValidationError as exc:
            raise JudgeUnavailableError(
                problem="judge output did not match the required schema",
                error_count=exc.error_count(),
            ) from exc

    def judge(
        self, request: JudgeRequest, cache: JudgeCache, *, offline: bool
    ) -> list[JudgeVerdict]:
        """Judge one item, using the cache where possible.

        In offline mode a cache miss raises rather than falling back to a
        network call — the whole point of offline mode is that it cannot reach
        a provider.
        """
        key = cache.key(
            provider=self.provider,
            model=self.model,
            prompt_sha256=self.prompt_sha256,
            input_digest=request.digest(),
        )
        cached = cache.get(key)
        if cached is not None:
            return [JudgeVerdict.model_validate(entry) for entry in cached]

        if offline or self.client is None:
            raise JudgeUnavailableError(
                problem="no cached judgement and judging is offline",
                item=request.item_id,
            )

        prompt = self.prompt_template.format(
            evidence=request.evidence_text, claim=request.claim_text
        )
        verdicts: list[JudgeVerdict] = []
        for sample in range(self.samples_per_item):
            # Seed varies per sample so repeated judgements are independent,
            # but the sequence is reproducible across runs.
            raw = self.client.complete(
                prompt, temperature=self.temperature, seed=self.seed + sample
            )
            verdicts.append(self._parse(raw))

        cache.put(key, [verdict.model_dump() for verdict in verdicts])
        return verdicts


def self_consistency(verdicts: list[JudgeVerdict]) -> float | None:
    """Agreement across repeated judgements of the same item.

    1.0 when every sample agreed on ``supported``. Low agreement does not mean
    the claim is borderline — it means the *judge* is unstable on this input,
    and its score should carry correspondingly little weight.
    """
    if len(verdicts) < 2:
        return None  # Nothing to compare
    supported = sum(1 for verdict in verdicts if verdict.supported)
    majority = max(supported, len(verdicts) - supported)
    return majority / len(verdicts)


def aggregate_verdicts(verdicts: list[JudgeVerdict]) -> bool:
    """Majority verdict across samples.

    Ties resolve to **unsupported**. On a tie the judge has demonstrated it
    cannot tell, and in a patient-facing medical context the conservative
    reading is the correct default.
    """
    if not verdicts:
        return False
    supported = sum(1 for verdict in verdicts if verdict.supported)
    return supported > len(verdicts) / 2


__all__ = [
    "GROUNDEDNESS_PROMPT",
    "JUDGE_CAVEAT",
    "JUDGE_VERSION",
    "GroundednessJudge",
    "JudgeCache",
    "JudgeClient",
    "JudgeRequest",
    "JudgeUnavailableError",
    "JudgeVerdict",
    "aggregate_verdicts",
    "self_consistency",
]

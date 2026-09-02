"""
synapse.ingest.eutils
=====================
NCBI E-utilities client: pacing, retries, timeouts, and configuration.

What the legacy client did (``Data/fetch_and_chunk.py``): called
``urllib.request.urlopen(url)`` with **no timeout** — so a hung NCBI connection
blocks the build forever — **no retry**, so one transient 500 loses a whole
topic, and a flat ``time.sleep(0.4)`` placed *after* the fetch rather than
before it, which paces nothing on the first request of a burst.

What this client does instead:

* **Timeouts** on every request, so a stalled socket fails instead of hanging.
* **Bounded exponential backoff** with a retry ceiling, retrying only the
  failures that are actually transient (429, 5xx, socket errors) and never
  retrying a 400 — a malformed query will fail identically every time.
* **Respectful pacing.** NCBI documents 3 requests/second without an API key
  and 10/second with one. The limiter enforces a minimum interval *before*
  each request, and the rate is derived from whether a key is present.
* **Optional identification.** ``NCBI_API_KEY`` and ``NCBI_EMAIL`` are read
  from the environment when set, and ``tool``/``email`` parameters are attached
  when known. Neither is required: the client works unauthenticated at the
  lower rate, which is what NCBI's usage guidance asks for.

Everything with a side effect is injectable — the HTTP transport, the clock,
and the sleep function — which is how the test-suite runs the full ingestion
pipeline against local XML fixtures with no network access at all.
"""

from __future__ import annotations  # Postponed annotations

import os  # Reading NCBI_API_KEY / NCBI_EMAIL from the environment
import urllib.error  # HTTPError / URLError classification
import urllib.parse  # Query-string encoding
import urllib.request  # Default HTTP transport
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from synapse.errors import SynapseArtifactError
from synapse.logging import get_logger

logger = get_logger(__name__)  # Module-level logger; allocating one performs no I/O

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"  # Documented E-utilities base URL

DEFAULT_TOOL = "synapse"  # NCBI asks callers to identify themselves with a tool name

# NCBI's published rate limits. Exceeding them risks the shared IP being
# blocked, which would affect every user of that network, not just this build.
RATE_LIMIT_WITHOUT_KEY = 3.0  # requests/second
RATE_LIMIT_WITH_KEY = 10.0  # requests/second

RETRYABLE_STATUS = frozenset(
    {429, 500, 502, 503, 504}
)  # Transient server-side conditions; everything else is a permanent failure

EFETCH_BATCH_SIZE = 200  # PMIDs per efetch call. NCBI recommends POST above ~200 ids; staying under keeps requests as simple GETs.


class IngestError(SynapseArtifactError):  # Typed, user-safe: inherits the sanitising base
    """A request to NCBI failed permanently, or after exhausting retries."""

    reason = "NCBI request failed"


class Transport(Protocol):  # Structural type: anything callable with this shape works
    """Fetches a URL and returns raw bytes."""

    def __call__(self, url: str, *, timeout: float) -> bytes: ...


def urllib_transport(url: str, *, timeout: float) -> bytes:
    """Default transport: a plain GET with an explicit timeout."""
    request = urllib.request.Request(url, headers={"User-Agent": f"{DEFAULT_TOOL}/ingest"})  # noqa: S310  # Scheme is fixed by EUTILS_BASE and never caller-controlled
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # Same: the URL is built from a constant base
        return response.read()  # type: ignore[no-any-return]


@dataclass(frozen=True)
class EUtilsConfig:
    """Client configuration. Every field has a working default."""

    api_key: str | None = None  # Raises the rate limit to 10/s when present
    email: str | None = None  # Contact address NCBI can use before blocking a misbehaving caller
    tool: str = DEFAULT_TOOL
    timeout_seconds: float = 20.0  # Per-request socket timeout
    max_attempts: int = 4  # Initial attempt plus three retries
    backoff_base_seconds: float = 0.5  # First retry delay; doubles each attempt
    backoff_max_seconds: float = (
        8.0  # Ceiling, so a long outage does not produce multi-minute sleeps
    )
    requests_per_second: float | None = None  # Overrides the key-derived default when set

    @classmethod
    def from_environment(cls, **overrides: object) -> EUtilsConfig:
        """Build a config from ``NCBI_API_KEY`` / ``NCBI_EMAIL``, with explicit overrides winning.

        Neither variable is required. Their absence lowers the request rate; it
        never disables ingestion.
        """
        base: dict[str, object] = {
            "api_key": os.environ.get("NCBI_API_KEY") or None,  # Empty string is treated as absent
            "email": os.environ.get("NCBI_EMAIL") or None,
        }
        base.update(
            {key: value for key, value in overrides.items() if value is not None}
        )  # An explicit None never clobbers an environment value
        return cls(**base)  # type: ignore[arg-type]

    @property
    def effective_rate(self) -> float:
        """Requests per second this client will not exceed."""
        if (
            self.requests_per_second is not None
        ):  # Explicit override, e.g. a deliberately slower build
            return self.requests_per_second
        return (
            RATE_LIMIT_WITH_KEY if self.api_key else RATE_LIMIT_WITHOUT_KEY
        )  # Key presence is what NCBI keys the limit on


@dataclass
class _RateLimiter:
    """Enforces a minimum interval between requests."""

    min_interval: float  # Seconds between successive requests
    clock: Callable[[], float]  # Injectable monotonic clock
    sleep: Callable[[float], None]  # Injectable sleep
    _last_request_at: float | None = field(
        default=None, init=False
    )  # Timestamp of the previous request

    def wait(self) -> None:
        """Block until enough time has passed since the previous request.

        Paces *before* each request rather than after, so the limit holds from
        the very first call in a burst — the flaw in a trailing ``sleep()``.
        """
        now = self.clock()
        if self._last_request_at is not None:  # First request needs no wait
            elapsed = now - self._last_request_at
            remaining = self.min_interval - elapsed
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()  # Re-read: the sleep advanced the clock
        self._last_request_at = now


class EUtilsClient:
    """A pacing, retrying client for PubMed ESearch and EFetch."""

    def __init__(
        self,
        config: EUtilsConfig | None = None,
        *,
        transport: Transport | None = None,  # Injected in tests to serve local fixtures
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config or EUtilsConfig.from_environment()
        self._transport = (
            transport or urllib_transport
        )  # Default is the real network; tests always pass their own
        self._sleep = sleep if sleep is not None else _default_sleep
        clock_fn = clock if clock is not None else _default_clock
        self._limiter = _RateLimiter(
            min_interval=1.0 / self.config.effective_rate,  # 3/s -> 0.333s, 10/s -> 0.1s
            clock=clock_fn,
            sleep=self._sleep,
        )

    def _build_url(self, endpoint: str, params: dict[str, str]) -> str:
        """Assemble an E-utilities URL, attaching identification when configured."""
        full = dict(params)
        full["tool"] = self.config.tool  # NCBI asks every caller to identify its tool
        if self.config.email:  # Optional, but lets NCBI make contact before blocking
            full["email"] = self.config.email
        if self.config.api_key:  # Optional, raises the rate limit
            full["api_key"] = self.config.api_key
        return f"{EUTILS_BASE}/{endpoint}?{urllib.parse.urlencode(full)}"

    def _request(self, endpoint: str, params: dict[str, str]) -> bytes:
        """Perform one paced, retried request and return the raw body."""
        url = self._build_url(endpoint, params)
        last_error: Exception | None = None  # Retained so the final failure can name a cause

        for attempt in range(1, self.config.max_attempts + 1):
            self._limiter.wait()  # Pacing applies to retries too, so a failing endpoint is not hammered
            try:
                return self._transport(url, timeout=self.config.timeout_seconds)
            except urllib.error.HTTPError as exc:  # A response arrived, with an error status
                last_error = exc
                if (
                    exc.code not in RETRYABLE_STATUS
                ):  # A 400 will fail identically forever; retrying wastes the quota
                    logger.error(
                        "ncbi request rejected", extra={"endpoint": endpoint, "status": exc.code}
                    )
                    raise IngestError(
                        endpoint=endpoint, status=exc.code, problem="non-retryable HTTP error"
                    ) from exc
                logger.warning(
                    "ncbi request failed, will retry",
                    extra={"endpoint": endpoint, "status": exc.code, "attempt": attempt},
                )
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as exc:  # Socket-level failure: DNS, reset, timeout
                last_error = exc
                logger.warning(
                    "ncbi request errored, will retry",
                    extra={"endpoint": endpoint, "attempt": attempt, "error": type(exc).__name__},
                )

            if attempt < self.config.max_attempts:  # No sleep after the final attempt
                delay = min(
                    self.config.backoff_base_seconds * (2 ** (attempt - 1)),
                    self.config.backoff_max_seconds,
                )  # 0.5, 1, 2, 4 ... capped
                self._sleep(delay)

        logger.error(
            "ncbi request exhausted retries",
            extra={"endpoint": endpoint, "attempts": self.config.max_attempts},
        )
        raise IngestError(
            endpoint=endpoint, attempts=self.config.max_attempts, problem="exhausted retries"
        ) from last_error

    def esearch(self, query: str, *, max_results: int, sort: str = "relevance") -> list[str]:
        """Search PubMed and return matching PMIDs, in the order NCBI ranked them.

        ``sort`` is explicit because the legacy client omitted it and silently
        accepted the default ordering, producing a corpus made almost entirely
        of the newest records — measured on the shipped artifact, every PMID
        fell in a single recent band, with no guidelines or landmark trials.
        """
        import json  # Local import: ESearch is the only JSON endpoint used, so the module scope stays XML-focused

        params = {
            "db": "pubmed",
            "term": query,
            "retmax": str(max_results),
            "retmode": "json",
            "sort": sort,
        }
        payload = self._request("esearch.fcgi", params)
        try:
            data = json.loads(payload)  # SAFE deserialisation; never eval
        except json.JSONDecodeError as exc:
            raise IngestError(
                endpoint="esearch.fcgi", problem="response was not valid JSON"
            ) from exc
        try:
            ids = data["esearchresult"]["idlist"]
        except (
            KeyError,
            TypeError,
        ) as exc:  # NCBI returned a well-formed JSON body of an unexpected shape
            raise IngestError(
                endpoint="esearch.fcgi", problem="response missing esearchresult.idlist"
            ) from exc
        pmids = [
            str(value) for value in ids if str(value).isdigit()
        ]  # Defensive: drop anything non-numeric rather than passing it downstream
        logger.info(
            "pubmed search complete", extra={"result_count": len(pmids), "requested": max_results}
        )
        return pmids

    def efetch(self, pmids: Sequence[str], *, batch_size: int = EFETCH_BATCH_SIZE) -> list[bytes]:
        """Fetch full records for ``pmids``, returning one raw XML body per batch.

        Batched because a single request with thousands of ids is both fragile
        and discourteous; each batch is paced by the same limiter.
        """
        bodies: list[bytes] = []
        for start in range(0, len(pmids), batch_size):
            batch = pmids[start : start + batch_size]
            if not batch:  # Defensive: an empty slice would produce a malformed request
                continue
            params = {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}
            bodies.append(self._request("efetch.fcgi", params))
            logger.info(
                "pubmed fetch batch complete",
                extra={"batch_size": len(batch), "batch_index": start // batch_size},
            )
        return bodies


def _default_clock() -> float:
    """Monotonic clock, imported lazily so the module has no import-time cost."""
    import time

    return time.monotonic()  # Monotonic, not wall clock: immune to NTP adjustments mid-build


def _default_sleep(seconds: float) -> None:
    """Real sleep, injected over in tests so the suite never actually waits."""
    import time

    time.sleep(seconds)


__all__ = [
    "EFETCH_BATCH_SIZE",
    "EUTILS_BASE",
    "RATE_LIMIT_WITHOUT_KEY",
    "RATE_LIMIT_WITH_KEY",
    "RETRYABLE_STATUS",
    "EUtilsClient",
    "EUtilsConfig",
    "IngestError",
    "Transport",
    "urllib_transport",
]

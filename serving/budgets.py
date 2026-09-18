"""Time and retry budgets for the inference hot path (plan P4.6).

Two failure shapes this module exists to prevent:

* **Work nobody is waiting for.** A request that has been queued past the moment its caller gave
  up still ran the model, and each hop applied its own fixed timeout — the router 10 s per attempt
  times three attempts, the model server 30 s — so an inference could outlive its client by a
  minute. A :class:`Deadline` is fixed once at the ingress and travels with the request as a
  *remaining budget*; every hop derives its timeout from what is left and refuses work whose
  budget is already spent.
* **Retry storms.** Retrying an overloaded server multiplies the load that overloaded it. A
  :class:`RetryBudget` lets retries through only while most recent calls are succeeding, so a
  blip is retried and an outage is not amplified.

The budget crosses hops as a relative duration (``X-ExaMLOps-Budget-Ms``), never a wall-clock
instant: hops run on different hosts whose clocks disagree, and each converts the duration to its
own monotonic deadline on receipt. This is how gRPC propagates deadlines (the ``grpc-timeout``
header); time in transit is not deducted, so a budget errs slightly long, never short.

Dependency-free and pure, so both serving deployments and their tests import it directly.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

#: Request header (router → model server) and payload key (ingress → transformer → router)
#: carrying the remaining budget in whole milliseconds.
HEADER = "X-ExaMLOps-Budget-Ms"
PAYLOAD_KEY = "_budget_ms"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


#: Budget given to a request that arrives without one.
DEFAULT_SECONDS = _env_float("INFERENCE_DEADLINE_SECONDS", 30.0)
#: Ceiling on any budget a caller asks for, so a client cannot pin a replica with a huge one.
MAX_SECONDS = _env_float("INFERENCE_DEADLINE_MAX_SECONDS", 300.0)


@dataclass(frozen=True)
class Deadline:
    """A point on this process's monotonic clock after which the request is abandoned."""

    at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        return cls(time.monotonic() + max(0.0, seconds))

    @classmethod
    def from_budget_ms(
        cls,
        raw: object,
        *,
        default: float | None = None,
        cap: float | None = None,
    ) -> Deadline:
        """The deadline a received budget stands for.

        No budget, or one that is not a number, gets ``default`` — a malformed header must not
        turn into "no deadline". A budget above ``cap`` is clamped to it. Zero or negative means
        the caller has already given up.
        """
        fallback = DEFAULT_SECONDS if default is None else default
        ceiling = MAX_SECONDS if cap is None else cap
        seconds = fallback
        if raw is not None and raw != "":
            try:
                seconds = float(str(raw).strip()) / 1000.0
            except ValueError:
                seconds = fallback
            if seconds != seconds:  # NaN
                seconds = fallback
        return cls.after(min(seconds, ceiling))

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self.at - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def budget_ms(self) -> str:
        """The remaining budget as the header/payload value the next hop reads."""
        return str(int(self.remaining() * 1000))


class RetryBudget:
    """Retry throttling after gRPC's (proposal A6, ``retryThrottling``).

    A token count starts full at ``max_tokens``. Every failed attempt removes one token and every
    success adds ``token_ratio``; a retry is allowed only while more than half the tokens remain.
    With the defaults (100, 0.1) a burst of about 50 failures is retried (a replica that dies
    fails every request it held at once, and each deserves its one retry), while sustained
    retrying still needs ~10 successes per failure: when a dependency is down the retries stop
    after a bounded burst instead of multiplying its load, and resume as successes refill the
    bucket. It was 10 tokens, and the live failover test lost a request whenever more than five
    were on the dying replica. Thread-safe.
    """

    def __init__(self, max_tokens: float = 100.0, token_ratio: float = 0.1) -> None:
        if max_tokens <= 0 or token_ratio <= 0:
            raise ValueError("max_tokens and token_ratio must be positive")
        self.max_tokens = max_tokens
        self.token_ratio = token_ratio
        self._tokens = max_tokens
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> RetryBudget:
        return cls(
            _env_float("INFERENCE_RETRY_MAX_TOKENS", 100.0),
            _env_float("INFERENCE_RETRY_TOKEN_RATIO", 0.1),
        )

    def record_success(self) -> None:
        with self._lock:
            self._tokens = min(self.max_tokens, self._tokens + self.token_ratio)

    def record_failure(self) -> None:
        with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)

    def can_retry(self) -> bool:
        with self._lock:
            return self._tokens > self.max_tokens / 2

    @property
    def tokens(self) -> float:
        with self._lock:
            return self._tokens

"""Open-loop load test of an inference endpoint, with SLO verdicts (plan P5).

Requests leave on a fixed schedule — request *i* at ``start + i / rate`` — whatever the server is
doing, and each latency is measured **from the moment the request was due to leave**, not from
when it was sent. A closed-loop tester (N clients, each waiting for its answer before the next
request) slows down exactly when the server does, sends fewer requests during a stall, and so
reports the latency of a system nobody is overloading: "coordinated omission". Measuring from the
schedule charges every request for the time it would really have waited.

The client itself can fall behind (too many requests in flight): those requests are counted as
``dropped``, and a run with any drops is reported as not valid, because it no longer measured the
schedule it was asked to.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

# Answers that mean the server refused the request rather than failed it.
SHED_STATUSES = frozenset({429, 503})


@dataclass
class LoadReport:
    target_rate: float
    duration_s: float
    sent: int = 0
    dropped: int = 0  # due while the client already had max_in_flight outstanding
    statuses: Counter = field(default_factory=Counter)
    transport_errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)  # successful requests only
    elapsed_s: float = 0.0

    @property
    def succeeded(self) -> int:
        return sum(n for code, n in self.statuses.items() if 200 <= code < 300)

    @property
    def failed(self) -> int:
        return self.sent - self.succeeded

    @property
    def shed(self) -> int:
        return sum(n for code, n in self.statuses.items() if code in SHED_STATUSES)

    @property
    def error_rate(self) -> float:
        return self.failed / self.sent if self.sent else 0.0

    @property
    def achieved_rate(self) -> float:
        return self.sent / self.elapsed_s if self.elapsed_s else 0.0

    def percentile(self, q: float) -> float | None:
        """Nearest-rank percentile of successful latencies, in milliseconds."""
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        rank = max(1, math.ceil(q / 100 * len(ordered)))
        return ordered[rank - 1]

    def breaches(
        self, *, p99_ms: float | None = None, max_error_rate: float | None = None
    ) -> list[str]:
        """What the run violated. An empty list is a pass."""
        found = []
        if self.dropped:
            found.append(
                f"client_saturated: {self.dropped} requests could not be sent on schedule "
                "(raise --max-in-flight, or lower --rate); the latencies are not trustworthy"
            )
        if self.sent == 0:
            found.append("no_requests: nothing was sent")
        if max_error_rate is not None and self.error_rate > max_error_rate:
            found.append(f"error_rate: {self.error_rate:.2%} > {max_error_rate:.2%}")
        p99 = self.percentile(99)
        if p99_ms is not None and (p99 is None or p99 > p99_ms):
            found.append(
                f"p99: {'none succeeded' if p99 is None else f'{p99:.1f} ms'} > {p99_ms} ms"
            )
        return found

    def to_dict(self) -> dict[str, Any]:
        def ms(v: float | None) -> float | None:
            return None if v is None else round(v, 2)

        return {
            "target_rate": self.target_rate,
            "achieved_rate": round(self.achieved_rate, 2),
            "duration_s": self.duration_s,
            "sent": self.sent,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "shed": self.shed,
            "dropped": self.dropped,
            "transport_errors": self.transport_errors,
            "error_rate": round(self.error_rate, 4),
            "statuses": {str(k): v for k, v in sorted(self.statuses.items())},
            "latency_ms": {
                "p50": ms(self.percentile(50)),
                "p90": ms(self.percentile(90)),
                "p95": ms(self.percentile(95)),
                "p99": ms(self.percentile(99)),
                "max": ms(max(self.latencies_ms) if self.latencies_ms else None),
            },
        }


async def run(
    url: str,
    body: Mapping[str, Any],
    *,
    rate: float,
    duration: float,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_in_flight: int = 1000,
    client: httpx.AsyncClient | None = None,
) -> LoadReport:
    """POST ``body`` to ``url`` at ``rate`` requests per second for ``duration`` seconds."""
    if rate <= 0 or duration <= 0:
        raise ValueError("rate and duration must be positive")
    report = LoadReport(target_rate=rate, duration_s=duration)
    total = max(1, int(rate * duration))
    owned = client is None
    http = client or httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_connections=max_in_flight, max_keepalive_connections=100),
    )
    in_flight = 0
    tasks: set[asyncio.Task] = set()

    async def send(due: float) -> None:
        nonlocal in_flight
        try:
            resp = await http.post(url, json=dict(body), headers=dict(headers or {}))
        except httpx.HTTPError:
            report.transport_errors += 1
        else:
            report.statuses[resp.status_code] += 1
            if 200 <= resp.status_code < 300:
                report.latencies_ms.append((time.monotonic() - due) * 1000)
        finally:
            in_flight -= 1

    start = time.monotonic()
    try:
        for i in range(total):
            due = start + i / rate
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if in_flight >= max_in_flight:
                report.dropped += 1  # never sent, so not a server outcome
                continue
            report.sent += 1
            in_flight += 1
            task = asyncio.create_task(send(due))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            _, unanswered = await asyncio.wait(set(tasks), timeout=timeout + 5)
            report.transport_errors += len(unanswered)  # sent, never answered
            for task in unanswered:
                task.cancel()
    finally:
        report.elapsed_s = time.monotonic() - start
        if owned:
            await http.aclose()
    return report


def body_from_metadata(metadata: Mapping[str, Any], alias: str | None = None) -> dict[str, Any]:
    """An OIP v2 request with one zero-valued row, built from ``GET /v2/models/{name}``.

    Works for a model whose inputs are named columns (shape ``[-1]``). A model without a column
    signature declares one ``[-1, -1]`` input whose width the metadata does not say; for those
    pass a real request body instead. Raises ``ValueError`` then.
    """
    inputs = []
    for spec in metadata.get("inputs") or []:
        shape = list(spec.get("shape") or [])
        if len(shape) != 1:
            raise ValueError(
                f"input {spec.get('name')!r} has shape {shape}: the metadata does not say how "
                "wide a row is. Pass a request body (--body) instead"
            )
        datatype = str(spec.get("datatype") or "FP64")
        zero: float | int | bool | str = 0.0 if datatype.startswith("FP") else 0
        if datatype == "BOOL":
            zero = False
        elif datatype == "BYTES":
            zero = ""
        inputs.append({"name": spec["name"], "shape": [1], "datatype": datatype, "data": [zero]})
    if not inputs:
        raise ValueError("the model's metadata lists no inputs; pass a request body (--body)")
    request: dict[str, Any] = {"inputs": inputs}
    if alias:
        request["parameters"] = {"alias": alias}
    return request

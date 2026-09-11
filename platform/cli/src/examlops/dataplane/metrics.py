"""Prometheus hooks for pulls. No-op when prometheus-client is not installed."""

from __future__ import annotations

from typing import Any

_METRICS: dict[str, Any] | None = None


def _metrics() -> dict[str, Any]:
    global _METRICS
    if _METRICS is None:
        try:
            from prometheus_client import Counter, Histogram
        except Exception:
            _METRICS = {}
            return _METRICS
        _METRICS = {
            "pulls": Counter(
                "dataplane_pull_total", "Dataplane pulls by outcome", ["source", "status"]
            ),
            "seconds": Histogram(
                "dataplane_pull_duration_seconds",
                "Dataplane pull duration",
                ["source"],
                buckets=(1, 5, 15, 60, 300, 900, 3600),
            ),
            "rows": Counter(
                "dataplane_rows_total", "Rows committed by dataplane pulls", ["source"]
            ),
            "bytes": Counter(
                "dataplane_bytes_total", "Bytes committed by dataplane pulls", ["source"]
            ),
        }
    return _METRICS


def observe_pull(source: str, status: str, seconds: float, rows: int, size: int) -> None:
    m = _metrics()
    if not m:
        return
    m["pulls"].labels(source=source, status=status).inc()
    m["seconds"].labels(source=source).observe(seconds)
    if status == "succeeded":
        m["rows"].labels(source=source).inc(rows)
        m["bytes"].labels(source=source).inc(size)

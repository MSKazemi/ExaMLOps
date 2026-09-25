"""Generative benchmark results carry their conditions (ADR 0143 decision 5, Verification 2).

A TTFT/TPOT/goodput number means nothing without the conditions it was measured under, so a
result is **rejected at write time** unless it states every one of :data:`REQUIRED_CONDITIONS`:

=========================  ================================================================
``model``                  the served model (id or registry name@version)
``quantization``           ``none`` or the scheme (awq, gptq, fp8 ...)
``hardware``               accelerator and count, e.g. ``4xH100-80GB``
``engine_version``         e.g. ``vllm==0.29.0`` — a bump changes the number
``dataset``                prompt source
``length_distribution``    input/output length distribution, e.g. ``in~512,out~256``
``concurrency``            positive int — concurrent requests the load generator held
``slo``                    the name of a **declared** TTFT/TPOT pair for this servable
``ttft_includes_queue_wait``  bool — ADR 0143 d6: on HPC, TTFT is reported *including* queue wait
=========================  ================================================================

A sample is ``[ttft_ms, tpot_ms]`` or ``{ttft_ms, tpot_ms[, queue_wait_ms]}``. When a sample
carries ``queue_wait_ms`` and the run declares ``ttft_includes_queue_wait: false``, the queue wait
is **added** and the stored result is marked as including it — so every stored TTFT on a queued
substrate is the user-perceived one. Malformed samples are counted as ``rejected``.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from examlops.slo.pairs import PairSLO, evaluate, load_pair, percentile

__all__ = [
    "MAX_SAMPLES",
    "REQUIRED_CONDITIONS",
    "BenchmarkRejected",
    "record_benchmark",
    "summarize",
]

REQUIRED_CONDITIONS = (
    "model",
    "quantization",
    "hardware",
    "engine_version",
    "dataset",
    "length_distribution",
    "concurrency",
    "slo",
    "ttft_includes_queue_wait",
)
MAX_SAMPLES = 100_000  # bounded: a benchmark is summarised, not stored sample-by-sample


class BenchmarkRejected(ValueError):
    """A benchmark result without its conditions (or with invalid ones) — not stored."""


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_conditions(conditions: Any) -> list[str]:
    if not isinstance(conditions, dict):
        return ["conditions must be a mapping"]
    errors = []
    for key in REQUIRED_CONDITIONS:
        val = conditions.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"missing condition: {key}")
    conc = conditions.get("concurrency")
    if conc is not None and (isinstance(conc, bool) or not isinstance(conc, int) or conc < 1):
        errors.append("concurrency must be a positive int")
    q = conditions.get("ttft_includes_queue_wait")
    if q is not None and not isinstance(q, bool):
        errors.append("ttft_includes_queue_wait must be true or false")
    return errors


def _parse(sample: Any, add_queue: bool, require_queue: bool = False) -> tuple[float, float] | None:
    a: Any
    b: Any
    q: Any
    if isinstance(sample, dict):
        a, b, q = sample.get("ttft_ms"), sample.get("tpot_ms"), sample.get("queue_wait_ms")
    elif isinstance(sample, (list, tuple)) and len(sample) == 2:
        (a, b), q = sample, None
    else:
        return None
    if not (_finite(a) and _finite(b) and a >= 0 and b >= 0):
        return None
    if require_queue and q is None:
        # The run is labelled as including queue wait; a sample that cannot include it is not
        # counted under that label (it is counted as rejected instead).
        return None
    if add_queue and q is not None:
        if not _finite(q) or q < 0:
            return None
        a = a + q
    return float(a), float(b)


def summarize(
    samples: list[Any],
    pair: PairSLO,
    *,
    add_queue: bool,
    min_samples: int | None = None,
    require_queue: bool = False,
) -> dict[str, Any]:
    parsed = [p for p in (_parse(s, add_queue, require_queue) for s in samples) if p is not None]
    if not parsed:
        raise BenchmarkRejected("no valid (ttft_ms, tpot_ms) sample")
    ttfts = [p[0] for p in parsed]
    tpots = [p[1] for p in parsed]
    verdict = evaluate(pair, [list(p) for p in parsed], min_samples=min_samples)
    return {
        "n": len(parsed),
        "rejected": len(samples) - len(parsed),
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p99_ms": percentile(ttfts, 99),
        "tpot_p50_ms": percentile(tpots, 50),
        "tpot_p99_ms": percentile(tpots, 99),
        "goodput": verdict.attainment,
        "verdict": verdict.verdict,
    }


def record_benchmark(
    servable: str,
    conditions: Any,  # validated below: a non-mapping is rejected, not assumed
    samples: Any,
    *,
    tenant: str = "default",
    actor: str | None = None,
    pair_loader: Any = None,
) -> dict[str, Any]:
    """Validate, summarise and store one benchmark run. Raises :class:`BenchmarkRejected`."""
    from examlops.data import generative_benchmarks as store
    from examlops.data.audit import audit_best_effort

    if not (servable or "").strip():
        raise BenchmarkRejected("a servable name is required")
    errors = validate_conditions(conditions)
    if errors:
        raise BenchmarkRejected("; ".join(errors))
    if not isinstance(samples, list) or not samples:
        raise BenchmarkRejected("samples must be a non-empty list")
    if len(samples) > MAX_SAMPLES:
        raise BenchmarkRejected(f"at most {MAX_SAMPLES} samples per result")
    loader = pair_loader or load_pair
    pair = loader(servable, str(conditions["slo"]), tenant)
    if pair is None:
        raise BenchmarkRejected(
            f"slo {conditions['slo']!r} is not a declared pair for {servable} "
            "(exa slo pair-set first) — a number without its SLO is not stored"
        )
    already = bool(conditions["ttft_includes_queue_wait"])
    add_queue = not already
    queue_seen = any(isinstance(s, dict) and s.get("queue_wait_ms") is not None for s in samples)
    # When the run's TTFTs exclude queue wait but some samples carry it, the stored result is
    # labelled as *including* it — so only samples that actually had it added may count.
    summary = summarize(samples, pair, add_queue=add_queue, require_queue=add_queue and queue_seen)
    includes_queue = already or queue_seen
    stored_conditions = {**conditions, "ttft_includes_queue_wait": includes_queue}
    digest = hashlib.sha256(
        json.dumps({"c": stored_conditions, "s": samples}, sort_keys=True, default=str).encode()
    ).hexdigest()
    row = {
        "servable": servable,
        "tenant": tenant,
        "conditions": stored_conditions,
        "digest": digest,
        "ttft_includes_queue": includes_queue,
        "recorded_by": actor,
        **summary,
    }
    result_id, created = store.insert(row)
    if created:
        audit_best_effort(
            "slo",
            actor,
            "slo_benchmark_recorded",
            servable,
            {
                "id": result_id,
                "pair": conditions["slo"],
                "engine_version": conditions["engine_version"],
                "hardware": conditions["hardware"],
                "verdict": summary["verdict"],
                "n": summary["n"],
            },
            tenant=tenant,
        )
    return {"id": result_id, "created": created, **row}

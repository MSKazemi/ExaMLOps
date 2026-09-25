"""ADR 0016 decision 4 — speculative-decoding acceptance + speedup, measured into FinOps.

Before this module, :func:`examlops.engines.record_spec_decode_telemetry` had no caller outside its
own tests: acceptance rate reached a span only if someone called it by hand, and FinOps never saw
it at all. Now every generation that reports draft statistics is folded here **from the one place
every engine is wrapped** (:mod:`examlops.engines.instrumented`), and the folded counts are flushed
to ``platform_db.specdecode_windows`` for ``exa finops specdecode`` to read.

**Bounded by construction.** Calls accumulate in memory per ``(model, tenant, engine, lookahead)``
key and flush when a key reaches ``EXAMLOPS_SPECDECODE_FLUSH_CALLS`` calls (default 100) or its
window is older than ``EXAMLOPS_SPECDECODE_FLUSH_SECONDS`` (default 60). The number of live keys is
capped (``_MAX_KEYS``); exceeding it flushes everything first, so a cardinality explosion costs one
write burst, never unbounded memory. A failed write is logged and dropped — telemetry must never
break the generation it measures — and counted in :func:`dropped_windows`.

**The speedup model** is the expected number of tokens produced per target-model forward pass under
speculative decoding with per-token acceptance probability ``α`` and draft lookahead ``γ``
(Leviathan, Kalman & Matias, *Fast Inference from Transformers via Speculative Decoding*, ICML 2023,
eq. 1): ``(1 - α^(γ+1)) / (1 - α)``. With ``γ = 1`` it reduces to ``1 + α``, the first-order figure
this platform already reported, so existing numbers do not move. It **ignores the draft model's
own cost**, so it is an upper bound on wall-clock speedup, and it is labelled as an estimate
wherever it is shown. ``α`` is estimated as accepted / proposed tokens.

Server engines (``vllm-server``, ``sglang``) do not return per-request draft counts; their figures
come from the server's own cumulative counters via :func:`spec_decode_from_metrics`.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "dropped_windows",
    "estimated_speedup",
    "flush",
    "observe",
    "pending",
    "spec_decode_from_metrics",
]

_MAX_KEYS = 256


def _env_int(name: str, default: int, floor: int = 1) -> int:
    try:
        return max(int(os.getenv(name, str(default))), floor)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def estimated_speedup(acceptance: float, lookahead: int = 1) -> float:
    """Expected tokens per target forward pass (Leviathan et al. 2023, eq. 1).

    ``acceptance`` is clamped to ``[0, 1]`` and ``lookahead`` to ``>= 1``; at ``α = 1`` the limit
    ``γ + 1`` is returned rather than dividing by zero.
    """
    alpha = min(max(float(acceptance), 0.0), 1.0)
    gamma = max(int(lookahead), 1)
    if alpha >= 1.0:
        return float(gamma + 1)
    return (1.0 - alpha ** (gamma + 1)) / (1.0 - alpha)


@dataclass
class _Window:
    started_mono: float
    started_wall: str
    calls: int = 0
    proposed: int = 0
    accepted: int = 0


_LOCK = threading.Lock()
_WINDOWS: dict[tuple[str, str, str, int], _Window] = {}
_DROPPED = 0


def _now_wall() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def observe(
    model: str,
    *,
    proposed_tokens: int,
    accepted_tokens: int,
    engine: str,
    tenant: str | None = None,
    lookahead: int = 1,
) -> bool:
    """Fold one generation's draft statistics into its window. Returns True if it was counted.

    A call with no proposed tokens is not a speculative generation and is ignored — counting it
    would dilute the acceptance rate with calls that never drafted. Accepted tokens are clamped to
    proposed ones: a server reporting more accepted than proposed is wrong, and crediting the
    excess would inflate the speedup FinOps reports.
    """
    proposed = int(proposed_tokens or 0)
    if proposed <= 0:
        return False
    accepted = min(max(int(accepted_tokens or 0), 0), proposed)
    key = (model or "unknown", tenant or "default", engine or "unknown", max(int(lookahead), 1))
    due: list[tuple[tuple[str, str, str, int], _Window]] = []
    with _LOCK:
        if key not in _WINDOWS and len(_WINDOWS) >= _MAX_KEYS:
            due.extend(_WINDOWS.items())
            _WINDOWS.clear()
        win = _WINDOWS.get(key)
        if win is None:
            win = _Window(started_mono=time.monotonic(), started_wall=_now_wall())
            _WINDOWS[key] = win
        win.calls += 1
        win.proposed += proposed
        win.accepted += accepted
        max_calls = _env_int("EXAMLOPS_SPECDECODE_FLUSH_CALLS", 100)
        max_age = _env_float("EXAMLOPS_SPECDECODE_FLUSH_SECONDS", 60.0)
        now = time.monotonic()
        if win.calls >= max_calls or now - win.started_mono >= max_age:
            due.append((key, _WINDOWS.pop(key)))
        # The age bound applies to *every* window, not only the one this call touched: a key that
        # saw one call and then went quiet would otherwise sit in memory until process exit (and
        # be lost on a SIGKILL, where atexit never runs). At most _MAX_KEYS entries, so cheap.
        for stale in [k for k, w in _WINDOWS.items() if now - w.started_mono >= max_age]:
            due.append((stale, _WINDOWS.pop(stale)))
    _write(due)
    return True


def flush() -> int:
    """Write every pending window now. Returns how many windows were written."""
    with _LOCK:
        due = list(_WINDOWS.items())
        _WINDOWS.clear()
    return _write(due)


def pending() -> dict[tuple[str, str, str, int], dict[str, int]]:
    """A snapshot of the unflushed windows (for tests and diagnostics)."""
    with _LOCK:
        return {
            k: {"calls": w.calls, "proposed": w.proposed, "accepted": w.accepted}
            for k, w in _WINDOWS.items()
        }


def dropped_windows() -> int:
    """Windows whose write failed and were dropped since process start."""
    return _DROPPED


def _write(due: list[tuple[tuple[str, str, str, int], _Window]]) -> int:
    global _DROPPED
    if not due:
        return 0
    try:
        from examlops.data.specdecode import record_specdecode_window
    except Exception:  # pragma: no cover - data layer unavailable
        _DROPPED += len(due)
        return 0
    written = 0
    end = _now_wall()
    for (model, tenant, engine, lookahead), win in due:
        try:
            record_specdecode_window(
                model,
                engine=engine,
                tenant=tenant,
                calls=win.calls,
                proposed_tokens=win.proposed,
                accepted_tokens=win.accepted,
                lookahead=lookahead,
                window_start=win.started_wall,
                window_end=end,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - telemetry never breaks generation
            _DROPPED += 1
            log.warning("specdecode window for %s dropped: %s", model, exc)
    return written


atexit.register(flush)


# Cumulative counters a vLLM server exposes when speculative decoding is on. The exposition
# format appends ``_total`` to a counter's name; both spellings are accepted so the parser does
# not depend on the client library version that rendered them.
_VLLM_DRAFT = ("vllm:spec_decode_num_draft_tokens_total", "vllm:spec_decode_num_draft_tokens")
_VLLM_ACCEPTED = (
    "vllm:spec_decode_num_accepted_tokens_total",
    "vllm:spec_decode_num_accepted_tokens",
)


def spec_decode_from_metrics(
    metrics: dict[str, float], *, lookahead: int = 1
) -> dict[str, Any] | None:
    """Acceptance + estimated speedup from a server's cumulative counters, or ``None``.

    ``None`` means the server exposes no draft counters (speculative decoding off, or an engine
    that does not publish them) — distinct from an acceptance rate of 0, which is a measurement.
    """
    draft = next((metrics[k] for k in _VLLM_DRAFT if k in metrics), None)
    accepted = next((metrics[k] for k in _VLLM_ACCEPTED if k in metrics), None)
    if draft is None or accepted is None or draft <= 0:
        return None
    acceptance = min(max(accepted / draft, 0.0), 1.0)
    return {
        "proposed_tokens": int(draft),
        "accepted_tokens": int(accepted),
        "acceptance_rate": acceptance,
        "estimated_speedup": estimated_speedup(acceptance, lookahead),
    }

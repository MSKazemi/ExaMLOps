"""Scheduler for the advanced drift detectors (ADR 0022 — nothing used to run them).

``exa drift concept|estimate|profile`` ran a detector when a person typed the command, so a model
whose labels arrived overnight was never looked at. This runs all three over **every model that
has recorded predictions**, on a loop or once, and writes ``drift_events`` with ``drift_kind`` —
which is what the existing cooldown-aware ``exa drift trigger`` and the autopilot already consume
(a CRITICAL concept event still triggers the closed loop; a label-free estimate still only warns).

It follows the house pattern of ``exa autoscale`` / ``exa autopilot``:

* **kill-switch** ``EXAMLOPS_DRIFT_ADVANCED_ENABLED`` (default OFF) — a real run is refused without
  it; a **dry run** is a preview that never persists and never needs it;
* **lease** — one scheduler acts at a time (the shared :mod:`examlops.coordination` lock, so a Redis
  coordinator makes it cross-host); a dry run takes none;
* **cooldown / dedupe** ``EXAMLOPS_DRIFT_ADVANCED_COOLDOWN`` (seconds, default 3600) — an event is
  written when it is the first for its (model, kind, metric), when its severity *changed*, or when
  a non-OK severity has stood for longer than the cooldown. A repeating OK is not news, and a
  repeating WARN is re-stated once per cooldown rather than once per cycle;
* **audited** — every real cycle writes one summary event, and a refused or lease-blocked cycle
  writes a ``drift_advanced_skipped`` event.

One model that raises never stops the sweep.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from examlops.drift_advanced import (
    DriftResult,
    detect_concept_drift,
    estimate_performance,
    estimate_signal_open,
    profile_inference,
)

log = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
LEASE_KEY = "drift-advanced"
ENABLED_ENV = "EXAMLOPS_DRIFT_ADVANCED_ENABLED"
COOLDOWN_ENV = "EXAMLOPS_DRIFT_ADVANCED_COOLDOWN"
INTERVAL_ENV = "EXAMLOPS_DRIFT_ADVANCED_INTERVAL"
LEASE_TTL_ENV = "EXAMLOPS_DRIFT_ADVANCED_LEASE_TTL"
REJECTION_WINDOW_ENV = "EXAMLOPS_DRIFT_ADVANCED_REJECTION_WINDOW"
DEFAULT_REJECTION_WINDOW_S = 3600
DEFAULT_COOLDOWN_S = 3600
DEFAULT_INTERVAL_S = 300
DEFAULT_LEASE_TTL_S = 600
KINDS = ("concept", "estimate", "data_quality")

# recorded / deduped (nothing new to say) / skipped (nothing to run on) / failed (detector raised)
RECORDED, DEDUPED, SKIPPED, FAILED = "recorded", "deduped", "skipped", "failed"


def is_enabled() -> bool:
    """The kill-switch. Default OFF: only an explicit truthy value arms a real run."""
    return os.getenv(ENABLED_ENV, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def cooldown_s() -> int:
    return _env_int(COOLDOWN_ENV, DEFAULT_COOLDOWN_S)


def interval_s() -> int:
    return _env_int(INTERVAL_ENV, DEFAULT_INTERVAL_S)


def lease_ttl_s() -> int:
    return _env_int(LEASE_TTL_ENV, DEFAULT_LEASE_TTL_S)


def rejection_window_s() -> int:
    """Lookback (seconds) over which A5 contract rejections are folded into a quality profile."""
    return _env_int(REJECTION_WINDOW_ENV, DEFAULT_REJECTION_WINDOW_S)


@dataclass
class Check:
    """One detector's verdict for one model and what the scheduler did with it."""

    model: str
    kind: str
    severity: str | None
    outcome: str
    note: str = ""
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "kind": self.kind,
            "severity": self.severity,
            "outcome": self.outcome,
            "note": self.note,
            "score": self.score,
        }


@dataclass
class CycleReport:
    enabled: bool
    dry_run: bool
    ran: bool = True
    note: str = ""
    checks: list[Check] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for c in self.checks if c.outcome == outcome)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "ran": self.ran,
            "note": self.note,
            "models": len({c.model for c in self.checks}),
            "counts": {o: self.count(o) for o in (RECORDED, DEDUPED, SKIPPED, FAILED)},
            "checks": [c.to_dict() for c in self.checks],
        }


def _parse_ts(raw: Any) -> float | None:
    """A ``CURRENT_TIMESTAMP`` string (UTC, ``YYYY-MM-DD HH:MM:SS``) as an epoch, or ``None``."""
    try:
        return datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def _should_record(
    model: str, kind: str, metric: str | None, severity: str, *, cooldown: float, now: float
) -> tuple[bool, str]:
    """Dedupe against the newest stored event of the same ``(model, kind, metric)``."""
    from examlops.data.drift import list_drift_events

    previous = next(
        (
            e
            for e in list_drift_events(model=model, drift_kind=kind, last_n=50)
            if metric is None or e.get("metric") == metric
        ),
        None,
    )
    if previous is None:
        return True, "first event"
    if previous["severity"] != severity:
        return True, f"severity {previous['severity']} -> {severity}"
    if severity == "OK":
        return False, "unchanged OK"
    at = _parse_ts(previous["ts"])
    if at is None or now - at >= cooldown:
        return True, "restated after cooldown"
    return False, "within cooldown"


class AdvancedDriftScheduler:
    """Run the concept / label-free / data-quality detectors over every model with predictions."""

    def __init__(
        self,
        *,
        dry_run: bool = False,
        models: list[str] | None = None,
        actor: str | None = None,
        clock: Callable[[], float] = time.time,
        window: int = 50,
    ) -> None:
        self.dry_run = dry_run
        self.only = models
        self.actor = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        self.clock = clock
        self.window = window
        self.holder = f"{socket.gethostname()}:{os.getpid()}"

    def _audit(self, action: str, target: str, details: dict[str, Any]) -> None:
        from examlops.data.audit import audit_best_effort

        audit_best_effort("drift-advanced", self.actor, action, target, details)

    def run_cycle(self) -> CycleReport:
        enabled = is_enabled()
        report = CycleReport(enabled=enabled, dry_run=self.dry_run)
        if not enabled and not self.dry_run:
            report.ran = False
            report.note = f"advanced drift scheduler disabled - set {ENABLED_ENV}=1"
            self._audit("drift_advanced_skipped", "*", {"reason": "kill-switch disabled"})
            return report

        from examlops.coordination import get_coordinator

        coord = get_coordinator()
        held = False
        if not self.dry_run:
            held = coord.try_lock(LEASE_KEY, self.holder, lease_ttl_s())
            if not held:
                report.ran = False
                report.note = "another advanced-drift scheduler holds the lease"
                self._audit("drift_advanced_skipped", "*", {"reason": report.note})
                return report
        try:
            from examlops.data.drift import prediction_models, rejection_models

            # A model whose every request was refused has no predictions, and is the one whose
            # data quality most needs a verdict — so the rejection log widens the sweep.
            # Rejections are keyed case-insensitively; keep the predictions' spelling of a name.
            if self.only:
                models = self.only
            else:
                by_key = {m.lower(): m for m in prediction_models()}
                for m in rejection_models(since_s=rejection_window_s()):
                    by_key.setdefault(m.lower(), m)
                models = sorted(by_key.values())
            for model in models:
                for kind, run in (
                    ("concept", self._concept),
                    ("estimate", self._estimate),
                    ("data_quality", self._quality),
                ):
                    try:
                        report.checks.append(run(model))
                    except Exception as exc:  # noqa: BLE001 - one detector never stops the sweep
                        log.exception("drift-advanced: %s/%s crashed", model, kind)
                        report.checks.append(Check(model, kind, None, FAILED, f"internal: {exc}"))
            if not self.dry_run:
                self._audit(
                    "drift_advanced_cycle",
                    "*",
                    {"models": len(models), **report.to_dict()["counts"]},
                )
        finally:
            if held:
                coord.unlock(LEASE_KEY, self.holder)
        return report

    def run_forever(
        self, *, interval: int, on_cycle: Callable[[CycleReport], None] | None = None
    ) -> None:
        """Cycle until interrupted. ``KeyboardInterrupt`` is the caller's stop signal."""
        while True:
            rep = self.run_cycle()
            if on_cycle is not None:
                on_cycle(rep)
            time.sleep(interval)

    # -- one detector each: decide, then persist through the same dedupe ------------------------
    def _decide(
        self,
        model: str,
        kind: str,
        metric: str | None,
        severity: str,
        score: float | None,
        write: Callable[[], None],
        *,
        label: str | None = None,
    ) -> Check:
        """Dedupe on the stored ``kind``; report the check as ``label`` (default: ``kind``)."""
        shown = label or kind
        record, why = _should_record(
            model, kind, metric, severity, cooldown=cooldown_s(), now=self.clock()
        )
        if not record:
            return Check(model, shown, severity, DEDUPED, why, score)
        if not self.dry_run:
            write()
        note = f"would record: {why}" if self.dry_run else why
        return Check(model, shown, severity, RECORDED, note, score)

    def _concept(self, model: str) -> Check:
        res: DriftResult = detect_concept_drift(model, window=self.window, persist=False)
        if res.detail.get("reason") == "insufficient labels":
            return Check(model, "concept", res.severity, SKIPPED, "insufficient labels", res.score)
        from examlops.drift_advanced import _persist

        return self._decide(
            model, "concept", res.metric, res.severity, res.score, lambda: _persist(res)
        )

    def _estimate(self, model: str) -> Check:
        from examlops.data.evaluation import list_perf_estimates, record_perf_estimate
        from examlops.drift_advanced import PERF_WARN_DROP

        prior = list_perf_estimates(model, last_n=1000)
        baseline = next(
            (p["estimated"] for p in reversed(prior) if p.get("estimated") is not None), None
        )
        res = estimate_performance(model, baseline=baseline, persist=False)
        if res["estimated"] is None:
            return Check(model, "estimate", None, SKIPPED, "no predictions")
        # WARN = an estimated drop; CRITICAL = that drop confirmed by realized labels (ADR 0022
        # decision 4) — the only estimate `exa drift trigger` will act on.
        severity = res["severity"]
        drop = (baseline - res["estimated"]) if baseline is not None else 0.0
        # A recovery after a WARN/CRITICAL estimate must be written as an OK event, and at once:
        # estimates only record non-OK events, so without it the last CRITICAL stays the newest
        # estimated-performance signal and `exa drift trigger` keeps retraining a healthy model.
        recovered = severity == "OK" and estimate_signal_open(model)

        def write() -> None:
            from examlops.data.drift import record_drift_event

            record_perf_estimate(
                model,
                res["metric"],
                estimated=res["estimated"],
                realized=res["realized"],
                baseline=baseline,
                method=res["method"],
            )
            if severity != "OK" or recovered:
                record_drift_event(
                    model,
                    "concept",
                    severity=severity,
                    score=drop,
                    metric=res["metric"],
                    detail={**res["event_detail"], "warn_drop": PERF_WARN_DROP},
                )

        # An OK estimate is still stored (the estimated-vs-realized trend needs the points) but
        # only a WARN is a drift *event*, so the dedupe key is the concept/metric pair.
        if recovered:
            if not self.dry_run:
                write()
            note = "would record: recovered" if self.dry_run else "recovered"
            return Check(model, "estimate", "OK", RECORDED, note, res["estimated"])
        if severity == "OK":
            latest = _parse_ts(prior[0]["ts"]) if prior else None
            if latest is not None and self.clock() - latest < cooldown_s():
                return Check(model, "estimate", "OK", DEDUPED, "within cooldown", res["estimated"])
            if not self.dry_run:
                write()
            return Check(model, "estimate", "OK", RECORDED, "estimate stored", res["estimated"])
        return self._decide(
            model, "concept", res["metric"], severity, drop, write, label="estimate"
        )

    def _quality(self, model: str) -> Check:
        from examlops.data.drift import count_inference_rejections, recent_prediction_features

        batch = recent_prediction_features(model, 200)
        # A5: requests the ingress refused against the contract in the lookback window count as
        # fully-null inputs — otherwise a model whose traffic is mostly refused looks healthy.
        bad = count_inference_rejections(model, since_s=rejection_window_s())
        if not batch and not bad:
            return Check(model, "data_quality", None, SKIPPED, "no recorded inputs")
        prof = profile_inference(model, batch, bad_payloads=bad, persist=False)

        def write() -> None:
            profile_inference(model, batch, bad_payloads=bad)

        return self._decide(model, "data_quality", None, prof.severity, prof.null_fraction, write)


__all__ = [
    "AdvancedDriftScheduler",
    "Check",
    "CycleReport",
    "cooldown_s",
    "interval_s",
    "is_enabled",
    "rejection_window_s",
]

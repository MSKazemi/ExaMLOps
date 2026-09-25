"""Autoscale controller — the loop that *executes* ``decide_scale`` (ADR 0031, clause 1/4/5).

Until this module nothing called :func:`examlops.autoscale.decide_scale` except the CLI's own
subcommands, so a scaling decision was computed on request and applied by a human. This is the
smallest controller that closes that loop, built on the house pattern of ``exa autopilot``:

* **kill-switch** ``EXAMLOPS_AUTOSCALE_ENABLED`` (default OFF) — a real apply is refused without it;
  a dry run is a preview and never needs it;
* **lease** — one controller acts at a time (the shared :mod:`examlops.coordination` lock, so a Redis
  coordinator makes it cross-host); a dry run takes none;
* **storm cap** ``EXAMLOPS_AUTOSCALE_MAX_CHANGES`` — most changes one cycle will attempt;
* **every** applied / refused / dry-run / failed decision is audited (steady "at target" is not: it
  would be one event per model per interval);
* **absent is not zero.** A signal with no source, or a source that cannot be asked, holds the model
  (audited) — it is never read as a metric of 0, which would scale a healthy service to nothing.

What is built and what is not (be exact):

* Signals (:mod:`examlops.autoscale.queries`): ``rps``, ``p95`` and ``queue_depth`` (mean requests
  in flight, by Little's law over ``examlops_predict_latency_seconds_sum``) come from Prometheus —
  the series the model server already exports. ``gpu_util`` has a source only when the site sets
  ``EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY`` (its GPU exporter's labels are site knowledge); without it
  a policy that targets it holds with ``signal absent``.
* Policies: a model's DB override (``exa serve autoscale set``) else the ``autoscale:`` block of its
  model YAML (:mod:`examlops.autoscale.policy_yaml`).
* Appliers: :class:`DesiredStateApplier` (``desired``) records the decided count as desired
  replicas — intent for an external owner of replicas, not a change to a running replica.
  :class:`RecordApplier` records the executed change (scale event + audit) and touches no
  serving substrate — the actuator is then a human or an external system (a KEDA/Knative generator).
  :class:`~examlops.autoscale.k8s.KubernetesApplier` (``k8s``) patches the predictor Deployment's
  ``scale`` subresource and refuses when an HPA/KEDA object already owns it.
  :class:`RayServeApplier` is **not built**: Ray Serve here is one deployment hosting every model,
  scaled through ``RAY_AUTOSCALE_MAX_REPLICAS`` / ``autoscaling_config`` at deploy time, and no
  admin route changes a single model's replica count. It refuses honestly rather than pretending.
* GPU-aware (clause 4, E3): with ``EXAMLOPS_AUTOSCALE_GPU_CAPACITY`` set, a scale-up is refused when
  the fleet's committed GPUs (Σ replicas × ``gpu_fraction``) would exceed it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from examlops.autoscale import AutoscalePolicy, ScaleDecision, apply_scale, decide_scale

log = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
LEASE_KEY = "autoscale-controller"
DEFAULT_MAX_CHANGES = 5
DEFAULT_LEASE_TTL_S = 120
DEFAULT_INTERVAL_S = 30
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
SIGNAL_NAMES = ("rps", "p95", "queue_depth", "gpu_util")
_GPU_EPS = 1e-9


def is_enabled() -> bool:
    """The kill-switch. Default OFF: only an explicit truthy value arms the controller."""
    return os.getenv("EXAMLOPS_AUTOSCALE_ENABLED", "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def max_changes() -> int:
    return _env_int("EXAMLOPS_AUTOSCALE_MAX_CHANGES", DEFAULT_MAX_CHANGES)


def lease_ttl_s() -> int:
    return _env_int("EXAMLOPS_AUTOSCALE_LEASE_TTL", DEFAULT_LEASE_TTL_S)


def interval_s() -> int:
    return _env_int("EXAMLOPS_AUTOSCALE_INTERVAL", DEFAULT_INTERVAL_S)


def gpu_capacity() -> float | None:
    """Total GPUs the autoscaler may commit across models (E3 fractions count), or None = unbounded.

    A value that is set but unparsable or not positive is **0** — fail closed: a typo must not
    silently lift the ceiling.
    """
    raw = os.getenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return 0.0
    return val if val > 0 and math.isfinite(val) else 0.0


def committed_gpus(
    applier: ScaleApplier, configs: list[dict[str, Any]], *, skip: str | None = None
) -> float | None:
    """Σ replicas × gpu_fraction over every policy, or None when no capacity is configured.

    A model whose replicas are unknown counts at its ``max_replicas`` — the conservative
    reading: capacity that cannot be accounted for is treated as taken. ``skip`` leaves one model
    out (the activator's model, whose replicas are the measured zero it is about to change).
    Shared by the controller and the cold-start activator so both enforce one ceiling.
    """
    if gpu_capacity() is None:
        return None
    total = 0.0
    for cfg in configs:
        model = str(cfg["model"])
        if skip is not None and model == skip:
            continue
        try:
            cur = applier.current_replicas(model)
        except Exception:  # noqa: BLE001 - unknown, counted conservatively
            cur = None
        n = int(cfg.get("max_replicas") or 0) if cur is None else cur
        total += n * float(cfg.get("gpu_fraction") or 1.0)
    return total


# ── signals ──────────────────────────────────────────────────────────────────


class SignalSourceDown(Exception):
    """The telemetry source itself could not be asked (distinct from one signal being absent)."""


@dataclass
class Signals:
    """Observed state for one model. ``None`` means *absent* — never 0.

    ``idle_confirmed`` is True only when traffic was **measured** and was exactly zero over the
    policy's scale-to-zero window; None/False both mean "not proven idle".
    """

    rps: float | None = None
    p95: float | None = None
    queue_depth: float | None = None
    gpu_util: float | None = None
    idle_confirmed: bool | None = None

    def get(self, metric: str) -> float | None:
        return getattr(self, metric, None) if metric in SIGNAL_NAMES else None


@runtime_checkable
class SignalSource(Protocol):
    def read(self, model: str, policy: AutoscalePolicy) -> Signals: ...


class PrometheusSignals:
    """Signals from the Prometheus the platform already runs (``PROMETHEUS_URL``).

    ``query`` is injectable (tests). It returns one float, or ``None`` when the query returned no
    series / NaN (absent), and raises :class:`SignalSourceDown` when Prometheus cannot be asked.
    """

    def __init__(self, query: Callable[[str], float | None] | None = None) -> None:
        self._query = query or self._http_query

    @staticmethod
    def _http_query(expr: str) -> float | None:
        base = (os.getenv("PROMETHEUS_URL") or "http://localhost:19090").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise SignalSourceDown(f"PROMETHEUS_URL {base!r} is not an http(s) URL")
        url = f"{base}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
        timeout = float(os.getenv("EXAMLOPS_AUTOSCALE_PROBE_TIMEOUT", "5") or 5)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - unreachable monitoring = source down
            raise SignalSourceDown(f"Prometheus at {base} could not be queried: {exc}") from exc
        if body.get("status") != "success":
            raise SignalSourceDown(f"Prometheus refused the query: {body.get('error')}")
        series = (body.get("data") or {}).get("result") or []
        if len(series) != 1:
            return None
        try:
            value = float(series[0]["value"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        return None if math.isnan(value) or math.isinf(value) else value

    def read(self, model: str, policy: AutoscalePolicy) -> Signals:
        from examlops.autoscale.queries import QueryTemplateError, query_for, selector

        if not _MODEL_RE.match(model):
            return Signals()  # cannot be safely put in a PromQL matcher → everything absent
        values: dict[str, float | None] = {}
        for metric in SIGNAL_NAMES:
            try:
                expr = query_for(metric, model)
            except QueryTemplateError as exc:
                # A broken operator template is a misconfigured source, not a measured value.
                raise SignalSourceDown(str(exc)) from exc
            # A metric with no source is absent (None) and is never queried.
            values[metric] = self._query(expr) if expr is not None else None
        sig = Signals(
            rps=values["rps"],
            p95=values["p95"],
            queue_depth=values["queue_depth"],
            gpu_util=values["gpu_util"],
        )
        if policy.scale_to_zero_after_s > 0:
            reqs = f"examlops_predict_requests_total{{{selector(model)}}}"
            inc = self._query(f"sum(increase({reqs}[{int(policy.scale_to_zero_after_s)}s]))")
            # No series is "no data", not "no traffic": only a measured zero proves idleness.
            sig.idle_confirmed = None if inc is None else inc == 0
        return sig


# ── appliers ─────────────────────────────────────────────────────────────────


class ScaleApplyError(Exception):
    """The applier could not carry the change out (retried next cycle)."""


class ScaleApplierUnavailable(ScaleApplyError):
    """No real actuator exists for this applier (it is not built)."""


@runtime_checkable
class ScaleApplier(Protocol):
    name: str

    def current_replicas(self, model: str) -> int | None:
        """Replicas now, or None when unknown (the controller then holds)."""
        ...

    def apply(self, model: str, from_replicas: int, to_replicas: int) -> None:
        """Carry the change out or raise :class:`ScaleApplyError`."""
        ...


def _last_event(model: str) -> dict[str, Any] | None:
    from examlops.data.serving import list_scale_events

    rows = list_scale_events(model, last_n=1)
    return rows[0] if rows else None


class RecordApplier:
    """Records the executed change (scale event + audit, done by the controller); no substrate.

    Replica state is the controller's own ledger — the last recorded ``to_replicas`` — seeded by
    ``exa serve autoscale record``. Unknown until seeded, so an unseeded model holds.
    """

    name = "record"

    def current_replicas(self, model: str) -> int | None:
        ev = _last_event(model)
        return int(ev["to_replicas"]) if ev else None

    def apply(self, model: str, from_replicas: int, to_replicas: int) -> None:
        return None


class DesiredStateApplier(RecordApplier):
    """Writes the decided replica count as the model's **desired replicas** (``autoscale_desired``).

    This is honest about what it does: it records intent for whatever owns replicas (an operator, a
    deploy step, a KEDA/Knative object from ``exa serve autoscale manifest``). It does **not** change
    a running replica — nothing in this repo consumes the row to do that, because Ray Serve here
    serves every model from one deployment with no per-model replica admin route. Current replicas
    are the desired row if present, else the last recorded scale event.
    """

    name = "desired"

    def current_replicas(self, model: str) -> int | None:
        from examlops.data.autoscale_desired import get_desired

        row = get_desired(model)
        return int(row["replicas"]) if row else super().current_replicas(model)

    def apply(self, model: str, from_replicas: int, to_replicas: int) -> None:
        from examlops.data.autoscale_desired import set_desired

        try:
            set_desired(
                model,
                to_replicas,
                reason=f"autoscaler {from_replicas}->{to_replicas}",
                updated_by=os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER"),
            )
        except Exception as exc:  # noqa: BLE001
            raise ScaleApplyError(f"could not record desired replicas: {exc}") from exc


class RayServeApplier(RecordApplier):
    """NOT BUILT. Ray Serve serves every model from one deployment and exposes no per-model
    replica admin route; its replicas are set at deploy time (``autoscaling_config``). Refuses."""

    name = "ray"

    def apply(self, model: str, from_replicas: int, to_replicas: int) -> None:
        raise ScaleApplierUnavailable(
            "the Ray Serve applier is not built: replicas are per deployment, set at deploy time "
            "(RAY_AUTOSCALE_MAX_REPLICAS / autoscaling_config); there is no per-model admin route"
        )


def make_applier(name: str) -> ScaleApplier:
    def _k8s() -> ScaleApplier:
        from examlops.autoscale.k8s import KubernetesApplier

        return KubernetesApplier()

    appliers: dict[str, Callable[[], ScaleApplier]] = {
        "record": RecordApplier,
        "desired": DesiredStateApplier,
        "ray": RayServeApplier,
        "k8s": _k8s,
    }
    if name not in appliers:
        raise ValueError(f"unknown applier {name!r} (choose: {', '.join(sorted(appliers))})")
    return appliers[name]()


# ── the controller ───────────────────────────────────────────────────────────

# outcomes
APPLIED = "applied"
DRY_RUN = "dry_run"
HELD = "held"  # no change taken: absent signal / source down / anti-thrash / guard
REFUSED = "refused"  # a change was wanted and was not allowed (storm cap)
FAILED = "failed"  # the applier raised
STEADY = "steady"  # at target, nothing to do


@dataclass
class ModelResult:
    model: str
    outcome: str
    reason: str
    from_replicas: int | None = None
    to_replicas: int | None = None


@dataclass
class CycleReport:
    enabled: bool
    dry_run: bool
    ran: bool = True
    note: str = ""
    results: list[ModelResult] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "ran": self.ran,
            "note": self.note,
            "counts": {k: self.count(k) for k in (APPLIED, DRY_RUN, HELD, REFUSED, FAILED, STEADY)},
            "results": [r.__dict__ for r in self.results],
        }


def _seconds_since(ts: str | None, now: float) -> float:
    """Age of a ``CURRENT_TIMESTAMP`` (UTC) string; huge when there is none."""
    if not ts:
        return 1e9
    try:
        then = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return 1e9
    return max(0.0, now - then)


class AutoscaleController:
    def __init__(
        self,
        signals: SignalSource,
        applier: ScaleApplier,
        *,
        dry_run: bool = True,
        actor: str | None = None,
        clock: Callable[[], float] = time.time,
        cap: int | None = None,
    ) -> None:
        self.signals = signals
        self.applier = applier
        self.dry_run = dry_run
        self.actor = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        self.clock = clock
        self.cap = cap
        self.holder = f"{socket.gethostname()}:{os.getpid()}"
        self.consecutive_failures: dict[str, int] = {}
        self._last_audited: dict[str, str] = {}
        self._gpu_committed: float | None = None

    # -- audit ---------------------------------------------------------------
    def _audit(self, action: str, model: str, details: dict[str, Any], tenant: str) -> None:
        from examlops.data.audit import audit_best_effort

        audit_best_effort("autoscale", self.actor, action, model, details, tenant=tenant)

    def _audit_dedup(self, action: str, model: str, reason: str, tenant: str) -> None:
        """Audit a repeating hold once per (model, reason) until the reason changes."""
        key = f"{action}:{reason}"
        if self._last_audited.get(model) == key:
            return
        self._last_audited[model] = key
        self._audit(action, model, {"reason": reason}, tenant)

    # -- GPU packing (clause 4) -----------------------------------------------
    def _committed_gpus(self, configs: list[dict[str, Any]]) -> float | None:
        return committed_gpus(self.applier, configs)

    # -- one cycle -----------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        enabled = is_enabled()
        report = CycleReport(enabled=enabled, dry_run=self.dry_run)
        if not enabled and not self.dry_run:
            report.ran = False
            report.note = "autoscale controller disabled — set EXAMLOPS_AUTOSCALE_ENABLED=1"
            self._audit("autoscale_skipped", "*", {"reason": "kill-switch disabled"}, "default")
            return report

        from examlops.coordination import get_coordinator

        coord = get_coordinator()
        held = False
        if not self.dry_run:
            held = coord.try_lock(LEASE_KEY, self.holder, lease_ttl_s())
            if not held:
                report.ran = False
                report.note = "another autoscale controller holds the lease"
                self._audit("autoscale_skipped", "*", {"reason": report.note}, "default")
                return report
        try:
            from examlops.autoscale.policy_yaml import effective_configs

            budget = self.cap if self.cap is not None else max_changes()
            attempted = 0
            configs = effective_configs()
            self._gpu_committed = self._committed_gpus(configs)
            for cfg in configs:
                try:
                    res, used = self._one(cfg, budget - attempted)
                except Exception as exc:  # noqa: BLE001 - one model never stops the cycle
                    log.exception("autoscale: %s crashed", cfg.get("model"))
                    res, used = ModelResult(str(cfg.get("model")), FAILED, f"internal: {exc}"), 0
                attempted += used
                report.results.append(res)
        finally:
            if held:
                coord.unlock(LEASE_KEY, self.holder)
        return report

    def _one(self, cfg: dict[str, Any], budget: int) -> tuple[ModelResult, int]:
        model = str(cfg["model"])
        tenant = str(cfg.get("tenant") or "default")
        policy = AutoscalePolicy.from_config(cfg)

        try:
            sig = self.signals.read(model, policy)
        except SignalSourceDown as exc:
            self._audit_dedup("autoscale_held", model, f"signal source down: {exc}", tenant)
            return ModelResult(model, HELD, f"signal source down: {exc}"), 0

        observed = sig.get(policy.target_metric)
        if observed is None:
            reason = f"signal absent: {policy.target_metric} has no measured value"
            self._audit_dedup("autoscale_held", model, reason, tenant)
            return ModelResult(model, HELD, reason), 0

        current = self.applier.current_replicas(model)
        if current is None:
            reason = "current replicas unknown (seed with: exa serve autoscale record)"
            self._audit_dedup("autoscale_held", model, reason, tenant)
            return ModelResult(model, HELD, reason), 0

        last = _last_event(model)
        since = _seconds_since(last["ts"] if last else None, self.clock())
        idle = float(policy.scale_to_zero_after_s) if sig.idle_confirmed else 0.0
        decision = decide_scale(
            current, observed, policy, idle_seconds=idle, seconds_since_last_scale=since
        )
        if not decision.changed:
            if decision.blocked_by:
                reason = f"held by {decision.blocked_by}: {decision.reason}"
                self._audit_dedup("autoscale_held", model, reason, tenant)
                return ModelResult(model, HELD, reason, current, current), 0
            self._last_audited.pop(model, None)
            return ModelResult(model, STEADY, decision.reason, current, current), 0

        # Never below the floor unless idleness was *measured* (decide_scale reads an observed
        # metric of 0 as "scale to zero" even before the idle window has elapsed).
        if decision.desired_replicas < policy.min_replicas and not (
            policy.scale_to_zero_after_s > 0 and sig.idle_confirmed
        ):
            reason = (
                f"below min ({policy.min_replicas}) refused: idleness over "
                f"{policy.scale_to_zero_after_s}s not proven"
            )
            self._audit_dedup("autoscale_refused", model, reason, tenant)
            return ModelResult(model, REFUSED, reason, current, decision.desired_replicas), 0

        target = decision.desired_replicas
        detail = {
            "from": current,
            "to": target,
            "reason": decision.reason,
            "metric": policy.target_metric,
            "observed": observed,
            "applier": self.applier.name,
        }
        capacity = gpu_capacity()
        extra_gpus = (target - current) * float(policy.gpu_fraction)
        if (
            capacity is not None
            and self._gpu_committed is not None
            and extra_gpus > 0
            and self._gpu_committed + extra_gpus > capacity + _GPU_EPS
        ):
            reason = (
                f"GPU capacity: +{extra_gpus:g} GPU on {self._gpu_committed:g} committed exceeds "
                f"EXAMLOPS_AUTOSCALE_GPU_CAPACITY={capacity:g}"
            )
            self._audit("autoscale_refused", model, {**detail, "reason": reason}, tenant)
            return ModelResult(model, REFUSED, reason, current, target), 0
        if budget <= 0:
            reason = "storm cap reached this cycle"
            self._audit("autoscale_refused", model, {**detail, "reason": reason}, tenant)
            return ModelResult(model, REFUSED, reason, current, target), 0

        if self.dry_run:
            if self._gpu_committed is not None:  # the preview packs as the real cycle would
                self._gpu_committed += extra_gpus
            self._audit("autoscale_dry_run", model, {**detail, "dry_run": True}, tenant)
            return ModelResult(model, DRY_RUN, decision.reason, current, target), 1

        try:
            self.applier.apply(model, current, target)
        except Exception as exc:  # noqa: BLE001 - counted, retried next cycle, never fatal
            n = self.consecutive_failures.get(model, 0) + 1
            self.consecutive_failures[model] = n
            self._audit(
                "autoscale_apply_failed",
                model,
                {**detail, "error": str(exc), "consecutive_failures": n},
                tenant,
            )
            return ModelResult(model, FAILED, str(exc), current, target), 1
        self.consecutive_failures.pop(model, None)
        self._last_audited.pop(model, None)
        if self._gpu_committed is not None:
            self._gpu_committed += extra_gpus
        applied = ScaleDecision(target, current, decision.reason, True)
        apply_scale(
            model,
            current,
            applied,
            tenant=tenant,
            metric_value=observed,
            actor=self.actor,
        )
        return ModelResult(model, APPLIED, decision.reason, current, target), 1

    # -- loop ----------------------------------------------------------------
    def run_forever(
        self,
        *,
        interval: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_cycles: int | None = None,
        on_cycle: Callable[[CycleReport], None] | None = None,
    ) -> int:
        """Cycle until interrupted. A cycle that raises is logged and retried; never a crash loop."""
        n = 0
        while max_cycles is None or n < max_cycles:
            try:
                rep = self.run_cycle()
                if on_cycle:
                    on_cycle(rep)
            except Exception:  # noqa: BLE001
                log.exception("autoscale cycle failed; retrying next interval")
            n += 1
            if max_cycles is not None and n >= max_cycles:
                break
            sleep(interval if interval is not None else interval_s())
        return n


__all__ = [
    "AutoscaleController",
    "CycleReport",
    "DesiredStateApplier",
    "ModelResult",
    "PrometheusSignals",
    "RayServeApplier",
    "RecordApplier",
    "ScaleApplier",
    "ScaleApplierUnavailable",
    "ScaleApplyError",
    "SignalSource",
    "SignalSourceDown",
    "Signals",
    "committed_gpus",
    "gpu_capacity",
    "is_enabled",
    "make_applier",
]

"""Cold-start activator — wake a scaled-to-zero model on its first request (ADR 0031 clause 2).

When a request arrives for a model the autoscaler has taken to **zero** replicas, something must
bring one back and hold the request until it can be served. That is this module:

* **Single flight per model.** The first request becomes the *leader*: it asks the applier for
  ``max(1, warm_pool, min_replicas)`` replicas and polls readiness. Every concurrent request for the
  same model *waits on the leader* (buffered), so a burst of N cold requests causes one scale-up,
  not N.
* **Bounded.** At most ``EXAMLOPS_AUTOSCALE_ACTIVATOR_MAX_WAITERS`` requests (default 64) may be
  buffered per model — the next one is refused (:class:`ActivatorOverloaded`, a 503 to the caller)
  instead of growing memory without limit; every wait is bounded by the caller's own deadline.
* **Measured.** The leader times scale-up → ready and records it as the scale event's
  ``cold_start_s`` (audited, D4). :func:`examlops.autoscale.cold_start_seconds` — shown by
  ``exa serve autoscale status`` — is the mean of those measurements, the number a cold-start SLO
  (C6) is held to.
* **Cheap when warm.** A model known to have replicas is remembered for
  ``EXAMLOPS_AUTOSCALE_ACTIVATOR_TTL`` seconds (default 5), so the hot path does not ask the applier
  on every request.
* **Absent is not zero.** A model with no autoscale policy, a policy that never scales to zero, or
  replicas the applier cannot report is passed through untouched — the activator only acts on a
  *measured* zero.

The activator is wired into the inference router (``serving/inference_pipeline``, behind
``EXAMLOPS_AUTOSCALE_ACTIVATOR=1``, default off) and exposed as ``exa serve autoscale activate``.
Readiness comes from the applier when it can report ready pods (the ``k8s`` applier), else from the
model server's Open Inference Protocol readiness route (``GET {url}/v2/models/{model}/ready``,
``EXAMLOPS_AUTOSCALE_READY_URL``, default ``RAY_SERVE_URL``).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from examlops.autoscale import AutoscalePolicy, ScaleDecision, apply_scale
from examlops.autoscale.controller import ScaleApplier, ScaleApplyError

log = logging.getLogger(__name__)

DEFAULT_MAX_WAITERS = 64
DEFAULT_WARM_TTL_S = 5.0
DEFAULT_TIMEOUT_S = 120.0
_POLICY_TTL_S = 30.0
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

#: ``model -> bool`` — True when at least one replica can serve.
ReadyProbe = Callable[[str], bool]


class ActivatorError(Exception):
    """The model could not be made ready for this request."""

    status = 503


class ActivatorOverloaded(ActivatorError):
    """Too many requests are already buffered behind this model's cold start."""


class ActivatorTimeout(ActivatorError):
    """The model did not become ready within the caller's deadline (the scale-up stands)."""

    status = 504


@dataclass
class WakeResult:
    model: str
    woke: bool  # this call (or the leader it waited on) scaled the model up from zero
    replicas: int | None
    cold_start_s: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _env_float(name: str, default: float) -> float:
    try:
        val = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return val if val > 0 else default


def http_ready_probe(base_url: str | None = None, *, timeout: float = 2.0) -> ReadyProbe:
    """OIP v2 model readiness (``200`` = ready; anything else, or no answer, = not yet)."""
    base = (
        base_url
        or os.getenv("EXAMLOPS_AUTOSCALE_READY_URL")
        or os.getenv("RAY_SERVE_URL")
        or "http://localhost:18001"
    ).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"readiness URL {base!r} is not an http(s) URL")

    def _probe(model: str) -> bool:
        if not _MODEL_RE.match(model):
            return False
        url = f"{base}/v2/models/{urllib.parse.quote(model.lower(), safe='')}/ready"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                return int(resp.status) == 200
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False

    return _probe


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: WakeResult | None = None
        self.error: ActivatorError | None = None
        self.waiters = 0


class Activator:
    def __init__(
        self,
        applier: ScaleApplier,
        *,
        probe: ReadyProbe | None = None,
        policy_for: Callable[[str], tuple[str, AutoscalePolicy, str] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_waiters: int | None = None,
        warm_ttl_s: float | None = None,
        poll_s: float = 0.5,
        actor: str | None = None,
    ) -> None:
        self.applier = applier
        self.probe = probe or self._default_probe()
        self._policy_for = policy_for or _policy_lookup()
        self.clock = clock
        self.sleep = sleep
        self.max_waiters = max_waiters or int(
            _env_float("EXAMLOPS_AUTOSCALE_ACTIVATOR_MAX_WAITERS", DEFAULT_MAX_WAITERS)
        )
        self.warm_ttl_s = (
            warm_ttl_s
            if warm_ttl_s is not None
            else _env_float("EXAMLOPS_AUTOSCALE_ACTIVATOR_TTL", DEFAULT_WARM_TTL_S)
        )
        self.poll_s = max(0.01, poll_s)
        self.actor = actor or os.getenv("EXAMLOPS_ACTOR") or "autoscale-activator"
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}
        self._warm_until: dict[str, float] = {}
        # A wake that failed is not retried (nor audited) by every following request: for
        # ``warm_ttl_s`` the same error is answered at once — a storm of cold requests to a model
        # whose scale-up is refused must not become a storm of API calls and audit rows.
        self._failed_until: dict[str, tuple[float, ActivatorError]] = {}

    def _default_probe(self) -> ReadyProbe:
        ready = getattr(self.applier, "ready_replicas", None)
        if callable(ready):
            return lambda m: (ready(m) or 0) > 0
        return http_ready_probe()

    # -- public ---------------------------------------------------------------
    def cached_warm(self, model: str) -> bool:
        """True while ``model`` is remembered warm — a non-blocking check for an event loop."""
        return self._warm_until.get(model, 0.0) > self.clock()

    def ensure_warm(self, model: str, timeout: float | None = None) -> WakeResult:
        """Return once ``model`` can serve; wake it from zero if needed (see module doc).

        ``timeout`` is the caller's remaining budget. ``None`` means the default; a budget that is
        already spent (``<= 0``, what an expired deadline reports) never waits — a cold model then
        answers :class:`ActivatorTimeout` at once instead of silently waiting the default.
        """
        budget = DEFAULT_TIMEOUT_S if timeout is None else float(timeout)
        now = self.clock()
        if self._warm_until.get(model, 0.0) > now:
            return WakeResult(model, False, None, note="warm (cached)")
        found = self._policy_for(model)
        if found is None:
            return WakeResult(model, False, None, note="no autoscale policy")
        name, policy, tenant = found
        with self._lock:
            waking = name in self._flights
        if waking:
            # Replicas may already read >0 while the leader waits for readiness: join the wake
            # rather than pass a request through to a model that cannot serve it yet.
            return self._join_or_lead(model, name, policy, tenant, budget)
        if policy.scale_to_zero_after_s <= 0 and policy.min_replicas > 0:
            self._mark_warm(model)
            return WakeResult(name, False, None, note="policy never scales to zero")
        try:
            current = self.applier.current_replicas(name)
        except ScaleApplyError as exc:
            return WakeResult(name, False, None, note=f"replicas unknown: {exc}")
        if current is None:
            return WakeResult(name, False, None, note="replicas unknown")
        if current > 0:
            self._mark_warm(model)
            return WakeResult(name, False, current, note="warm")
        return self._join_or_lead(model, name, policy, tenant, budget)

    # -- internals ------------------------------------------------------------
    def _mark_warm(self, model: str) -> None:
        with self._lock:
            self._warm_until[model] = self.clock() + self.warm_ttl_s

    def _join_or_lead(
        self, key: str, name: str, policy: AutoscalePolicy, tenant: str, budget: float
    ) -> WakeResult:
        if budget <= 0:
            raise ActivatorTimeout(f"{name}: request deadline already spent (cold model)")
        with self._lock:
            failed = self._failed_until.get(name)
            if failed is not None:
                if failed[0] > self.clock():
                    raise failed[1]
                self._failed_until.pop(name, None)
            flight = self._flights.get(name)
            leader = flight is None
            if leader:
                flight = self._flights[name] = _Flight()
            else:
                assert flight is not None
                if flight.waiters >= self.max_waiters:
                    raise ActivatorOverloaded(
                        f"{name}: {flight.waiters} requests already wait for its cold start"
                    )
                flight.waiters += 1
        assert flight is not None
        if not leader:
            try:
                if not flight.done.wait(budget):
                    raise ActivatorTimeout(f"{name}: not ready within {budget:g}s (cold start)")
            finally:
                with self._lock:
                    flight.waiters -= 1
            if flight.error is not None:
                raise flight.error
            assert flight.result is not None
            return flight.result
        try:
            flight.result = self._wake(name, policy, tenant, budget)
            self._mark_warm(key)
            return flight.result
        except ActivatorTimeout as exc:
            flight.error = exc  # the scale-up stands: the next request may find it ready
            raise
        except ActivatorError as exc:
            flight.error = exc
            self._back_off(name, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - every waiter gets an answer, never a hang
            flight.error = ActivatorError(f"{name}: activation failed: {exc}")
            self._back_off(name, flight.error)
            raise flight.error from exc
        finally:
            with self._lock:
                self._flights.pop(name, None)
            flight.done.set()

    def _back_off(self, name: str, exc: ActivatorError) -> None:
        with self._lock:
            self._failed_until[name] = (self.clock() + self.warm_ttl_s, exc)

    def _gpu_refusal(self, name: str, target: float, policy: AutoscalePolicy) -> str | None:
        """Clause 4 (E3): the same fleet GPU ceiling the controller enforces, for the wake."""
        from examlops.autoscale.controller import committed_gpus, gpu_capacity
        from examlops.autoscale.policy_yaml import effective_configs

        capacity = gpu_capacity()
        if capacity is None:
            return None
        # The model being woken is at a measured zero, so it contributes nothing to the sum.
        committed = committed_gpus(self.applier, effective_configs(), skip=name) or 0.0
        extra = target * float(policy.gpu_fraction)
        if committed + extra > capacity + 1e-9:
            return (
                f"GPU capacity: +{extra:g} GPU on {committed:g} committed exceeds "
                f"EXAMLOPS_AUTOSCALE_GPU_CAPACITY={capacity:g}"
            )
        return None

    def _wake(self, name: str, policy: AutoscalePolicy, tenant: str, budget: float) -> WakeResult:
        from examlops.data.audit import audit_best_effort

        target = max(1, policy.warm_pool, policy.min_replicas)
        refusal = self._gpu_refusal(name, target, policy)
        if refusal is not None:
            audit_best_effort(
                "autoscale",
                self.actor,
                "autoscale_activation_refused",
                name,
                {"to": target, "reason": refusal},
                tenant=tenant,
            )
            raise ActivatorError(f"{name}: cannot wake from zero: {refusal}")
        start = self.clock()
        try:
            self.applier.apply(name, 0, target)
        except ScaleApplyError as exc:
            audit_best_effort(
                "autoscale",
                self.actor,
                "autoscale_activation_failed",
                name,
                {"to": target, "error": str(exc)},
                tenant=tenant,
            )
            raise ActivatorError(f"{name}: could not scale up from zero: {exc}") from exc
        deadline = start + budget
        while True:
            if self.probe(name):
                break
            if self.clock() >= deadline:
                audit_best_effort(
                    "autoscale",
                    self.actor,
                    "autoscale_activation_timeout",
                    name,
                    {"to": target, "waited_s": round(self.clock() - start, 3)},
                    tenant=tenant,
                )
                raise ActivatorTimeout(
                    f"{name}: scaled 0->{target} but not ready within {budget:g}s"
                )
            self.sleep(min(self.poll_s, max(0.0, deadline - self.clock())))
        cold = round(self.clock() - start, 3)
        apply_scale(
            name,
            0,
            ScaleDecision(target, 0, f"activator: request on a cold model -> {target}", True),
            tenant=tenant,
            cold_start_s=cold,
            actor=self.actor,
        )
        log.info("autoscale activator: %s woke 0->%d in %.3fs", name, target, cold)
        return WakeResult(name, True, target, cold_start_s=cold, note="woke from zero")


def _policy_lookup() -> Callable[[str], tuple[str, AutoscalePolicy, str] | None]:
    """Case-insensitive ``model -> (policy name, policy, tenant)`` with a short TTL cache.

    The router lower-cases model names (``jpcp``) while policies are keyed by the canonical YAML
    name (``JPCP``); the lookup matches either.
    """
    cache: dict[str, Any] = {"until": 0.0, "by": {}}
    lock = threading.Lock()

    def _lookup(model: str) -> tuple[str, AutoscalePolicy, str] | None:
        now = time.monotonic()
        with lock:
            if cache["until"] <= now:
                from examlops.autoscale.policy_yaml import effective_configs

                by: dict[str, tuple[str, AutoscalePolicy, str]] = {}
                for cfg in effective_configs():
                    by[str(cfg["model"]).lower()] = (
                        str(cfg["model"]),
                        AutoscalePolicy.from_config(cfg),
                        str(cfg.get("tenant") or "default"),
                    )
                cache["by"], cache["until"] = by, now + _POLICY_TTL_S
            return cache["by"].get(model.lower())

    return _lookup


def is_enabled() -> bool:
    """``EXAMLOPS_AUTOSCALE_ACTIVATOR`` — off by default; the router passes through when off."""
    return os.getenv("EXAMLOPS_AUTOSCALE_ACTIVATOR", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_shared: Activator | None = None
_shared_lock = threading.Lock()


def shared_activator() -> Activator:
    """The process-wide activator (applier from ``EXAMLOPS_AUTOSCALE_APPLIER``, default desired)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            from examlops.autoscale.controller import make_applier

            _shared = Activator(make_applier(os.getenv("EXAMLOPS_AUTOSCALE_APPLIER") or "desired"))
        return _shared


def reset_shared_activator() -> None:
    global _shared
    with _shared_lock:
        _shared = None


__all__ = [
    "Activator",
    "ActivatorError",
    "ActivatorOverloaded",
    "ActivatorTimeout",
    "ReadyProbe",
    "WakeResult",
    "http_ready_probe",
    "is_enabled",
    "reset_shared_activator",
    "shared_activator",
]

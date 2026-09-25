"""ADR 0038 clause 4 — automatic bundle creation at training / promotion time.

A bundle built after the fact captures whatever environment happens to exist *then*; one built
by the run itself captures the run's own. ``EXAMLOPS_REPRO_AUTO_BUNDLE`` (default **off**, so
behaviour is byte-identical unless a site arms it) makes the pipeline build one:

* at the end of a successful ``training_flow`` (once the version is registered), and
* on ``exa pipeline promote`` for a version that has no bundle yet.

The one rule that matters: **a bundle failure never fails the run it documents.** Every error
is swallowed, but not silently — it is counted (:func:`failures`), logged, and written as a
``repro_auto_bundle_failed`` audit event through ``audit_best_effort`` (whose own losses are
counted too, see ``tests/unit/test_audit_losses_are_recorded.py``).

Honesty: nothing is invented. A dirty git tree is recorded as ``code_dirty: true``; a dataset
revision of ``unknown`` is not pinned; a seed is recorded only when ``EXAMLOPS_SEED`` was set
and :func:`apply_seed` actually applied it.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any

log = logging.getLogger(__name__)

ENV_AUTO = "EXAMLOPS_REPRO_AUTO_BUNDLE"
ENV_SEED = "EXAMLOPS_SEED"

_lock = threading.Lock()
_FAILURES = 0


def enabled() -> bool:
    return os.getenv(ENV_AUTO, "").strip().lower() in ("1", "true", "yes", "on")


def failures() -> int:
    """Automatic bundles that could not be built in this process (never reset by the code)."""
    return _FAILURES


def reset_failures() -> None:
    global _FAILURES
    with _lock:
        _FAILURES = 0


def apply_seed() -> int | None:
    """Seed the process RNGs from ``EXAMLOPS_SEED``; return the seed applied, else ``None``.

    Unset (or not an integer) does nothing at all, so a run without the variable is untouched.
    Seeds ``random`` and NumPy's global generator, and PyTorch when it is already imported —
    it never imports a heavy framework just to seed it.
    """
    raw = os.getenv(ENV_SEED, "").strip()
    if not raw:
        return None
    try:
        seed = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; not seeding", ENV_SEED, raw)
        return None
    import random
    import sys

    random.seed(seed)
    try:
        import numpy as np

        np.random.seed(seed % (2**32))
    except Exception:  # noqa: BLE001
        pass
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.manual_seed(seed)
        except Exception:  # noqa: BLE001
            pass
    return seed


def _clean_metrics(metrics: dict[str, Any] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in (metrics or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out[str(k)] = f
    return out


def _record_failure(model: str, version: str, trigger: str, exc: BaseException) -> None:
    global _FAILURES
    with _lock:
        _FAILURES += 1
    log.warning("automatic reproducibility bundle for %s/%s failed: %s", model, version, exc)
    try:
        from examlops.data.audit import audit_best_effort

        audit_best_effort(
            "exa-reproduce",
            os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER"),
            "repro_auto_bundle_failed",
            f"{model}/{version}",
            {"trigger": trigger, "error": f"{type(exc).__name__}: {exc}"[:300]},
        )
    except Exception:  # noqa: BLE001 - even the failure record must not fail the run
        log.warning("repro_auto_bundle_failed could not be audited", exc_info=True)


def auto_bundle(
    model: str,
    version: str | int | None,
    *,
    trigger: str,
    metrics: dict[str, Any] | None = None,
    dataset_name: str | None = None,
    dataset_revision: str | None = None,
    dataset_source: dict[str, Any] | None = None,
    hyperparams: dict[str, Any] | None = None,
    seed: int | None = None,
    run_spec: dict[str, Any] | None = None,
    skip_if_exists: bool = False,
    force: bool = False,
    resources: dict[str, Any] | None = None,
    lineage_run_id: str | None = None,
    mlflow_run_id: str | None = None,
) -> Any:
    """Build a bundle when armed; return it, or ``None`` (off / nothing to bundle / failed).

    Never raises. ``force`` builds even when the env switch is off (used by tests and by an
    explicit operator call); ``skip_if_exists`` makes promotion idempotent.

    ADR 0038 clause 1: ``resources`` is the scheduler request as submitted
    (:func:`examlops.reproducibility.capture.capture_resources`); the A2 lineage run is found
    from ``mlflow_run_id`` when not given. At promotion, and for a job a real scheduler (slurm,
    flux) placed on a compute node, the host building the bundle is not the one that trained,
    so hardware is recorded as *not captured* rather than as this host's.
    """
    if not (force or enabled()):
        return None
    if version in (None, ""):
        return None  # no registered version: there is no model version to key a bundle to
    ver = str(version)
    try:
        from examlops import data as platform_db
        from examlops.reproducibility import build_bundle

        if skip_if_exists and platform_db.get_repro_bundle(model, ver):
            return None
        if lineage_run_id is None and mlflow_run_id:
            from examlops.data.events import lineage_run_for_mlflow_run

            found = lineage_run_for_mlflow_run(mlflow_run_id)
            lineage_run_id = str(found["run_id"]) if found else None
        hardware: dict[str, Any] | None = None
        sched = str((resources or {}).get("scheduler") or "").strip().lower()
        if trigger == "promote":
            hardware = {
                "captured": False,
                "reason": "bundle built at promotion; the training host is not this one",
            }
        elif sched and sched != "mock":
            # The job ran on a compute node the scheduler chose; this process only submitted
            # it. Probing *this* host would record the submitter's hardware as the training
            # hardware, and `--execute` would then compare a rebuild against the wrong machine.
            hardware = {
                "captured": False,
                "reason": f"training ran as a {sched} job on a node this host does not probe",
                "job_id": (resources or {}).get("job_id"),
            }
        return build_bundle(
            model,
            ver,
            resources=resources,
            lineage_run_id=lineage_run_id,
            hardware=hardware,
            dataset_name=dataset_name,
            dataset_revision=dataset_revision or None,
            dataset_source=dataset_source,
            hyperparams=hyperparams or None,
            metrics=_clean_metrics(metrics) or None,
            seeds={"global": seed} if seed is not None else None,
            actor=os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "pipeline",
            trigger=trigger,
            run_spec=run_spec,
        )
    except Exception as exc:  # noqa: BLE001 - a bundle failure never fails the run
        _record_failure(model, ver, trigger, exc)
        return None


__all__ = [
    "ENV_AUTO",
    "ENV_SEED",
    "enabled",
    "failures",
    "reset_failures",
    "apply_seed",
    "auto_bundle",
]

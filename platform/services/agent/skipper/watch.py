"""Skipper-watch — proactive monitoring daemon (Phase 6, ADR 0104).

A watcher **distinct from the reactive chat** and **LLM-free in the base loop** (local, free). Each
cycle reads monitoring signals (prediction drift vs baseline z-score; platform cost vs budget) and,
on a threshold breach, fans the alert out three ways — reusing existing infrastructure, nothing new:

1. the **events outbox** (`examlops.data.events.enqueue_event`) → dashboard / webhooks via the relay;
2. the **audit log** (`write_audit_event`, hash-chained) → compliance trail;
3. an **episodic memory** (`memory_types.record_incident`) → so the reactive agent recalls the alert
   in later chats (best-effort — skipped when the long-term store is unavailable).

Run it once (a CI gate / cron tick) with ``python -m skipper.watch --once`` or as a loop with
``--daemon``. A best-effort cross-process lock (``examlops.data.coordination``) keeps a single daemon
active across replicas. Everything degrades: a missing `platform.db` yields zero signals, not a crash.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from skipper import config

log = logging.getLogger("skipper.watch")

_HOLDER = f"skipper-watch:{os.getpid()}"


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "skipper-watch"


# ── signals (best-effort; each degrades to []) ────────────────────────────────


def _cost_breaches() -> list[dict[str, Any]]:
    """Platform cost above the configured budget (FinOps signal)."""
    budget = config.AGENT_WATCH_COST_BUDGET
    if budget <= 0:
        return []
    try:
        from examlops.data import init_db
        from examlops.data.finops import aggregate_model_costs

        init_db()
        total = sum(float(r.get("cost_usd") or 0) for r in aggregate_model_costs())
    except Exception:  # noqa: BLE001
        return []
    if total > budget:
        return [
            {
                "kind": "cost",
                "target": "platform",
                "severity": "warn",
                "detail": f"platform cost ${total:.2f} exceeds budget ${budget:.2f}",
                "value": total,
                "threshold": budget,
            }
        ]
    return []


def _drift_breaches() -> list[dict[str, Any]]:
    """Models whose latest prediction-drift value deviates beyond the z-threshold vs baseline.

    Best-effort: needs a baseline with ``mean``/``std`` and a latest drift event carrying a numeric
    ``value``. Any model whose shapes don't match is skipped (never raises).
    """
    z_thresh = config.AGENT_WATCH_DRIFT_Z
    out: list[dict[str, Any]] = []
    try:
        from examlops.data import init_db
        from examlops.data.drift import (
            get_drift_baseline,
            list_drift_auto_retrain,
            list_drift_events,
        )

        init_db()
        models = {c.get("model") for c in list_drift_auto_retrain() if c.get("model")}
    except Exception:  # noqa: BLE001
        return []
    for model in models:
        try:
            base = get_drift_baseline(model) or {}
            mean, std = float(base.get("mean", 0.0)), float(base.get("std", 0.0))
            events = list_drift_events(model=model, drift_kind="prediction", last_n=1)
            if not events or std <= 0:
                continue
            value = _event_value(events[0])
            if value is None:
                continue
            z = abs(value - mean) / std
            if z >= z_thresh:
                out.append(
                    {
                        "kind": "drift",
                        "target": model,
                        "severity": "critical" if z >= z_thresh * 1.5 else "warn",
                        "detail": f"{model} prediction drift z={z:.2f} (>= {z_thresh})",
                        "value": z,
                        "threshold": z_thresh,
                    }
                )
        except Exception:  # noqa: BLE001 - one bad model must not stop the sweep
            continue
    return out


def _event_value(event: dict[str, Any]) -> float | None:
    for key in ("value", "prediction", "score", "statistic"):
        if key in event and event[key] is not None:
            try:
                return float(event[key])
            except (TypeError, ValueError):
                return None
    detail = event.get("detail")
    if isinstance(detail, dict):
        for key in ("value", "z", "statistic"):
            if key in detail:
                try:
                    return float(detail[key])
                except (TypeError, ValueError):
                    return None
    return None


# ── fan-out ───────────────────────────────────────────────────────────────────


def _raise_alert(alert: dict[str, Any]) -> None:
    """Emit one alert to the outbox + audit + episodic memory (each best-effort)."""
    kind, target = alert["kind"], alert["target"]
    try:
        from examlops.data.events import enqueue_event

        enqueue_event(f"alert.{kind}", alert)
    except Exception:  # noqa: BLE001
        log.warning("outbox unavailable for alert %s/%s", kind, target)
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        write_audit_event("skipper-watch", _actor(), "alert_raised", target, alert)
    except Exception:  # noqa: BLE001
        pass
    _record_episode(alert)


def _record_episode(alert: dict[str, Any]) -> None:
    """Write an episodic memory so the reactive agent recalls this alert (needs the LTM store)."""
    try:
        from skipper.memory import build_store
        from skipper.memory_types import record_incident

        store = build_store()
        if store is None:
            return
        record_incident(
            store,
            model=alert["target"],
            symptom=alert["detail"],
            root_cause="detected by skipper-watch",
            operator=_actor(),
        )
    except Exception:  # noqa: BLE001 - episodic memory is best-effort
        pass


# ── cycle + loop ──────────────────────────────────────────────────────────────


def run_once(*, dry_run: bool = False) -> dict[str, Any]:
    """Run one monitoring cycle. Returns ``{"alerts": n, "detail": [...]}``."""
    alerts = [*_drift_breaches(), *_cost_breaches()]
    if not dry_run:
        for alert in alerts:
            _raise_alert(alert)
    return {"alerts": len(alerts), "detail": alerts, "dry_run": dry_run}


def run_daemon() -> None:  # pragma: no cover - long-running loop
    """Loop ``run_once`` every ``AGENT_WATCH_INTERVAL_S`` while enabled, holding a cross-host lock."""
    try:
        from examlops.data.coordination import coord_try_lock, coord_unlock
    except Exception:  # noqa: BLE001
        coord_try_lock = coord_unlock = None  # type: ignore
    while config.AGENT_WATCH_ENABLED:
        got = True
        if coord_try_lock is not None:
            got = coord_try_lock("skipper-watch", _HOLDER, config.AGENT_WATCH_INTERVAL_S * 2)
        if got:
            try:
                result = run_once()
                if result["alerts"]:
                    log.info("skipper-watch raised %d alert(s)", result["alerts"])
            finally:
                if coord_try_lock is not None:
                    coord_unlock("skipper-watch", _HOLDER)
        time.sleep(config.AGENT_WATCH_INTERVAL_S)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m skipper.watch")
    p.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    p.add_argument("--daemon", action="store_true", help="Loop on AGENT_WATCH_INTERVAL_S")
    p.add_argument("--dry-run", action="store_true", help="Detect + print, do not raise alerts")
    args = p.parse_args(argv)
    if args.daemon:
        run_daemon()
        return 0
    result = run_once(dry_run=args.dry_run)
    print(f"skipper-watch: {result['alerts']} alert(s)")
    for a in result["detail"]:
        print(f"  [{a['severity']}] {a['kind']}:{a['target']} — {a['detail']}")
    # Non-zero exit on any critical alert → usable as a CI gate.
    return 1 if any(a["severity"] == "critical" for a in result["detail"]) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

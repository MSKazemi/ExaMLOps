"""Scheduled online evaluation over sampled live traffic (ADR 0007 decisions 2–3).

The ADR's runners are "``exa eval run`` (ad hoc / CI) **+ a scheduled online-eval flow**; results
→ ``platform_db`` and Prometheus". ``exa eval run`` existed; nothing scheduled a suite, so quality
was measured only when a person typed a command. This is the scheduled flow:

for every model with an enabled ``eval_online_config`` row, once per ``window_s``:

1. **pull** the window's traffic from the row's source (:mod:`examlops.evaluation.traffic` —
   labelled predictions from ``platform_db``, or GenAI spans from Tempo),
2. **sample** ``sample_size`` items deterministically by ``request_hash``,
3. **score** them with the row's evaluator specs (:mod:`examlops.evaluation.engines` —
   deterministic, Ragas, rubric judges and DeepEval, every judge through the gateway at
   temperature 0),
4. **persist** to ``eval_suite_results`` under ``run_id = online:<alias>:<window start>`` — the
   same table the C3 gate reads, so an online suite on ``Production`` is a live baseline — and
5. **export**: the results reach Prometheus through :func:`examlops.telemetry.exposition.export`
   (``exa slo export-metrics``), and when ``EXAMLOPS_EVAL_METRICS_TEXTFILE`` is set the
   scheduler rewrites that node_exporter textfile itself after each cycle, atomically.

House pattern of ``exa drift run-advanced`` / ``exa autopilot``:

* **kill-switch** ``EXAMLOPS_EVAL_ONLINE_ENABLED`` (default OFF) — a real cycle is refused without
  it; a **dry run** scores but persists nothing and needs neither the switch nor the lease;
* **lease** — one scheduler acts at a time (:mod:`examlops.coordination`);
* **idempotent per window** — a window already recorded is not scored twice (``has_eval_run``);
* **audited** — every real cycle writes an ``eval_online_cycle`` summary; a refused or
  lease-blocked cycle writes ``eval_online_skipped``;
* **bounded** — a pull is capped (``traffic.MAX_LIMIT``), a sample by the row's ``sample_size``
  (≤ :data:`MAX_SAMPLE`), and one model that raises never stops the sweep.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
LEASE_KEY = "eval-online"
ENABLED_ENV = "EXAMLOPS_EVAL_ONLINE_ENABLED"
INTERVAL_ENV = "EXAMLOPS_EVAL_ONLINE_INTERVAL"
LEASE_TTL_ENV = "EXAMLOPS_EVAL_ONLINE_LEASE_TTL"
TEXTFILE_ENV = "EXAMLOPS_EVAL_METRICS_TEXTFILE"
DEFAULT_INTERVAL_S = 300
DEFAULT_LEASE_TTL_S = 1800
MAX_SAMPLE = 1000
MIN_WINDOW_S = 60

RECORDED, DEDUPED, SKIPPED, FAILED, PREVIEWED = (
    "recorded",
    "deduped",
    "skipped",
    "failed",
    "previewed",
)
OUTCOMES = (RECORDED, DEDUPED, SKIPPED, FAILED, PREVIEWED)


def is_enabled() -> bool:
    """The kill-switch. Default OFF: only an explicit truthy value arms a real cycle."""
    return os.getenv(ENABLED_ENV, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def interval_s() -> int:
    return _env_int(INTERVAL_ENV, DEFAULT_INTERVAL_S)


def lease_ttl_s() -> int:
    return _env_int(LEASE_TTL_ENV, DEFAULT_LEASE_TTL_S)


def window_run_id(alias: str, window_s: int, now: float) -> str:
    """The run id of the window ``now`` falls in — the unit of online-eval idempotency."""
    window = max(MIN_WINDOW_S, int(window_s))
    start = int(now) // window * window
    stamp = datetime.fromtimestamp(start, UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"online:{alias}:{window}:{stamp}"


def build_suite(cfg: dict[str, Any], *, text_fn: Callable[..., str] | None = None) -> Any:
    """The suite a config row describes — raises on an unknown or unavailable evaluator."""
    from examlops.evaluation import Suite
    from examlops.evaluation.engines import make_evaluator

    specs = list(cfg.get("evaluators") or [])
    if not specs:
        raise ValueError("no evaluators configured")
    evaluators = [
        make_evaluator(spec, judge_model=cfg.get("judge_model"), text_fn=text_fn) for spec in specs
    ]
    return Suite(str(cfg["suite"]), evaluators, skip_missing_reference=True, tolerate_errors=True)


@dataclass
class ModelRun:
    model: str
    tenant: str
    suite: str
    outcome: str
    run_id: str = ""
    note: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    traffic: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tenant": self.tenant,
            "suite": self.suite,
            "outcome": self.outcome,
            "run_id": self.run_id,
            "note": self.note,
            "scores": self.scores,
            "counts": self.counts,
            "errors": self.errors,
            "traffic": self.traffic,
        }


@dataclass
class CycleReport:
    enabled: bool
    dry_run: bool
    ran: bool = True
    note: str = ""
    runs: list[ModelRun] = field(default_factory=list)
    textfile: str | None = None

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.runs if r.outcome == outcome)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "ran": self.ran,
            "note": self.note,
            "counts": {o: self.count(o) for o in OUTCOMES},
            "runs": [r.to_dict() for r in self.runs],
            "textfile": self.textfile,
        }


def write_textfile(path: str, tenant: str = "default") -> str:
    """Rewrite the node_exporter textfile atomically (tmp + rename), so a scrape never sees half.

    Tenant-scoped like ``exa slo export-metrics --tenant``: one textfile never carries another
    tenant's series.
    """
    from examlops.telemetry.exposition import export

    text = export(tenant=tenant)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".eval-", suffix=".prom.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


class OnlineEvalScheduler:
    """Score sampled live traffic for every model with an enabled online-eval schedule."""

    def __init__(
        self,
        *,
        dry_run: bool = False,
        models: list[str] | None = None,
        tenant: str | None = None,
        actor: str | None = None,
        clock: Callable[[], float] = time.time,
        sources: dict[str, Any] | None = None,
        text_fn: Callable[..., str] | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.only = set(models) if models else None
        self.tenant = tenant
        self.actor = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        self.clock = clock
        self.sources = sources or {}
        self.text_fn = text_fn
        self.holder = f"{socket.gethostname()}:{os.getpid()}"

    def _audit(self, action: str, target: str, details: dict[str, Any], tenant: str) -> None:
        from examlops.data.audit import audit_best_effort

        audit_best_effort("eval-online", self.actor, action, target, details, tenant=tenant)

    def _source(self, name: str) -> Any:
        if name in self.sources:
            return self.sources[name]
        from examlops.evaluation.traffic import get_source

        # The source windows by the scheduler's clock. Note what that does and does not give:
        # the run id names the fixed bucket ``now`` falls in (the idempotency unit), while the
        # traffic scored is the trailing ``window_s`` ending at the first cycle in that bucket —
        # so a point labelled 10:00 mostly scores 09:xx traffic. Contiguous while the scheduler
        # runs every ``interval`` < ``window_s``; a scheduler outage leaves an unscored gap.
        return get_source(name, clock=self.clock)

    def run_cycle(self) -> CycleReport:
        enabled = is_enabled()
        report = CycleReport(enabled=enabled, dry_run=self.dry_run)
        tenant = self.tenant or "default"
        if not enabled and not self.dry_run:
            report.ran = False
            report.note = f"online eval disabled - set {ENABLED_ENV}=1"
            self._audit("eval_online_skipped", "*", {"reason": "kill-switch disabled"}, tenant)
            return report

        from examlops.coordination import get_coordinator

        coord = get_coordinator()
        held = False
        if not self.dry_run:
            held = coord.try_lock(LEASE_KEY, self.holder, lease_ttl_s())
            if not held:
                report.ran = False
                report.note = "another online-eval scheduler holds the lease"
                self._audit("eval_online_skipped", "*", {"reason": report.note}, tenant)
                return report
        try:
            from examlops.data.evaluation import list_online_evals

            configs = list_online_evals(tenant=self.tenant, enabled_only=True)
            for cfg in configs:
                if self.only and cfg["model"] not in self.only:
                    continue
                try:
                    report.runs.append(self._run_one(cfg))
                except Exception as exc:  # noqa: BLE001 - one model never stops the sweep
                    log.exception("eval-online: %s crashed", cfg.get("model"))
                    report.runs.append(
                        ModelRun(
                            cfg["model"],
                            cfg["tenant"],
                            cfg["suite"],
                            FAILED,
                            note=f"internal: {type(exc).__name__}: {exc}",
                        )
                    )
            if not self.dry_run:
                self._audit(
                    "eval_online_cycle",
                    "*",
                    {"models": len(report.runs), **report.to_dict()["counts"]},
                    tenant,
                )
                path = os.getenv(TEXTFILE_ENV, "").strip()
                if path:
                    try:
                        report.textfile = write_textfile(path, tenant=tenant)
                    except OSError as exc:
                        report.note = f"textfile export failed: {exc}"
                        log.warning("eval-online: textfile export to %s failed: %s", path, exc)
        finally:
            if held:
                coord.unlock(LEASE_KEY, self.holder)
        return report

    def _run_one(self, cfg: dict[str, Any]) -> ModelRun:
        from examlops.data.evaluation import has_eval_run, mark_online_eval_run
        from examlops.evaluation import run_suite, sample_by_request_hash
        from examlops.evaluation.traffic import TrafficSourceError

        model, tenant, suite_name = cfg["model"], cfg["tenant"], cfg["suite"]
        alias = cfg.get("alias") or "Production"
        run_id = window_run_id(alias, int(cfg.get("window_s") or 3600), self.clock())
        run = ModelRun(model, tenant, suite_name, SKIPPED, run_id=run_id)

        def finish(outcome: str, note: str) -> ModelRun:
            run.outcome, run.note = outcome, note
            if not self.dry_run:
                mark_online_eval_run(model, tenant=tenant, run_id=run_id, status=outcome, note=note)
            return run

        if has_eval_run(suite_name, model, run_id, tenant=tenant):
            run.outcome, run.note = DEDUPED, "window already evaluated"
            return run
        try:
            suite = build_suite(cfg, text_fn=self.text_fn)
        except Exception as exc:  # noqa: BLE001 - a bad config is this model's failure only
            return finish(FAILED, f"evaluators: {exc}")
        try:
            pull = self._source(str(cfg.get("source") or "predictions")).pull(
                model,
                since_s=int(cfg.get("window_s") or 3600),
                limit=max(int(cfg.get("sample_size") or 50) * 20, 100),
                tenant=tenant,
                alias=alias,
            )
        except TrafficSourceError as exc:
            return finish(FAILED, f"traffic: {exc}")
        run.traffic = pull.to_dict()
        if not pull.items:
            return finish(SKIPPED, pull.note or "no traffic in the window")
        sample = sample_by_request_hash(
            pull.items, min(int(cfg.get("sample_size") or 50), MAX_SAMPLE)
        )
        result = run_suite(
            suite,
            sample,
            model=model,
            run_id=run_id,
            alias=alias,
            persist=not self.dry_run,
            tenant=tenant,
        )
        run.scores, run.counts, run.errors = result.scores, result.counts, result.errors
        if not result.scores:
            return finish(
                FAILED, f"no evaluator produced a score ({sum(result.errors.values())} errors)"
            )
        note = f"{len(sample)} item(s) scored"
        if result.errors:
            note += f"; {sum(result.errors.values())} evaluator error(s)"
        return finish(PREVIEWED if self.dry_run else RECORDED, note)

    def run_forever(
        self,
        *,
        interval: int,
        on_cycle: Callable[[CycleReport], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_cycles: int | None = None,
    ) -> int:
        """Cycle until interrupted. ``KeyboardInterrupt`` is the caller's stop signal.

        A cycle that raises (the datastore or the coordinator briefly unreachable) is logged and
        retried on the next tick — a scheduler that dies on one transient error stops measuring
        quality silently, and the only symptom is a gauge that stops moving.
        """
        n = 0
        while max_cycles is None or n < max_cycles:
            try:
                rep = self.run_cycle()
                if on_cycle is not None:
                    on_cycle(rep)
            except Exception:  # noqa: BLE001 - one bad cycle never ends the scheduler
                log.exception("eval-online: cycle failed; retrying in %ss", interval)
            n += 1
            if max_cycles is None or n < max_cycles:
                sleep(interval)
        return n


__all__ = [
    "CycleReport",
    "ModelRun",
    "OnlineEvalScheduler",
    "build_suite",
    "interval_s",
    "is_enabled",
    "window_run_id",
    "write_textfile",
]

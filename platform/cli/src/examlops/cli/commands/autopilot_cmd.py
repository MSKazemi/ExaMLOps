"""``exa autopilot`` — Self-driving MLOps closed loop (A2 → A3 maturity).

The autopilot runs one cycle of::

    detect drift → policy check → retrain → validate → policy check → promote

Every step is audited. A kill-switch prevents all actions when disabled.

Design notes (ADR 0085):
- One cycle per invocation — schedule via cron or CI.
- Kill-switch defaults to DISABLED; enable with ``exa autopilot enable`` or
  ``EXAMLOPS_AUTOPILOT_ENABLED=1``.
- Policy gates: ``autopilot_trigger`` before retrain, ``autopilot_promote`` before promote.
- ``require_approval`` from policy → human-action-required notice in audit_events.
- No blocking waits: retrain is fire-and-forget (same as ``exa drift trigger``).
- Promotion happens when a Staging version + promotion rule exists (from ``exa pipeline promote``).
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

import typer

from examlops import blast_radius, drift_status
from examlops.cli import _output
from examlops.data import init_db
from examlops.data.audit import audit_best_effort, write_audit_event
from examlops.data.autopilot import (
    claim_autopilot_lease,
    create_autopilot_run,
    get_autopilot_config,
    list_autopilot_runs,
    release_autopilot_lease,
    set_autopilot_config,
    update_autopilot_run,
)
from examlops.data.drift import claim_drift_trigger, list_drift_auto_retrain
from examlops.data.serving import get_promotion_rule
from examlops.evidence import AUTONOMOUS, clear_rollback_ref, correlated
from examlops.rollback import AutonomousActionRefused, require_rollback

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Self-driving MLOps autopilot — governed closed-loop detect→retrain→promote.",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_RUN = (
    "Examples:\n\n"
    "  exa autopilot run               # one cycle for all models\n\n"
    "  exa autopilot run --dry-run     # preview what would happen\n\n"
    "  exa autopilot run JPCP          # restrict to one model"
)
_EXAMPLES_STATUS = (
    "Examples:\n\n"
    "  exa autopilot status            # last 10 runs\n\n"
    "  exa autopilot status --last 20"
)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


# Safety cap: the most retrains one cycle will fire, so a fleet-wide drift event (or a bug) can
# never launch an unbounded retrain storm. Overridable via EXAMLOPS_AUTOPILOT_MAX_RETRAINS.
_DEFAULT_MAX_RETRAINS_PER_CYCLE = 10


log = logging.getLogger(__name__)


def _max_retrains_per_cycle() -> int:
    raw = os.getenv("EXAMLOPS_AUTOPILOT_MAX_RETRAINS")
    if raw and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_MAX_RETRAINS_PER_CYCLE


# TTL for the distributed cycle lease (item 0.12). Long enough to cover a full cycle incl. the
# HPC dispatch waits; short enough that a crashed holder frees the lease within one cron interval.
_DEFAULT_LEASE_TTL_S = 900


def _lease_ttl_s() -> int:
    raw = os.getenv("EXAMLOPS_AUTOPILOT_LEASE_TTL")
    if raw and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_LEASE_TTL_S


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _is_enabled() -> bool:
    """Return True if the autopilot kill-switch allows operation."""
    env = os.getenv("EXAMLOPS_AUTOPILOT_ENABLED", "").strip().lower()
    if env in _TRUTHY:
        return True
    if env in _FALSY:
        return False
    # fall back to DB config
    val = get_autopilot_config("enabled")
    return val == "1"


def _policy_decide(action: str, context: dict[str, Any]) -> tuple[str, str]:
    """Return ``(effect, reason)`` from ``policy.decide_safe``, defaulting to ``("deny", …)``.

    Read ``decision.effect``. This used to read ``decision.action``, which :class:`Decision` has
    never had — so every call raised ``AttributeError`` into the ``except`` below and returned
    ``allow``. That silently disabled **both** autopilot gates: a `deny` on ``autopilot_trigger``
    or ``autopilot_promote`` was ignored, and so was ``require_approval`` — the human-in-the-loop
    hold on the one component that acts without a human.

    Fixing the typo surfaced the real question ADR 0079 never answered: what should happen if
    the policy *engine* itself raises (a bug, not a missing/malformed file — that case already
    defaults to allow, inside ``decide()`` itself, by design)? Autopilot is the one component
    that acts without a human at the keyboard — the same reasoning that makes the MCP write-gate
    fail closed applies here even more, so this now denies (via ``policy.decide_safe``) rather
    than allows, and durably audits the fact that policy was unavailable instead of only logging
    it (BL-080).
    """
    from examlops.policy import DENY, decide_safe

    decision = decide_safe(action, context, default_effect=DENY)
    return decision.effect, decision.reason or ""


# ── injectable helpers (monkeypatched in tests) ─────────────────────────────


def _call_retrain(cfg: Any, model: str, dataset: str) -> dict[str, Any]:
    """Submit a retrain through the control plane's command API. Returns its answer
    (``flow_run_id`` once dispatched, else ``command_id`` + ``state``; plan P1.6c)."""
    from examlops import retrain_command

    body = {"model_name": model, "dataset_name": dataset, "is_dummy": False}
    return retrain_command.submit(
        body,
        wait=retrain_command.AUTOMATION_WAIT_SECONDS,
        base=cfg.control_plane_url,
        token=cfg.control_plane_token,
    )


def _get_staging_metrics(model: str) -> dict[str, float] | None:
    """Return metrics dict for the Staging alias of a model, or None if unavailable."""
    try:
        import mlflow

        client = mlflow.MlflowClient()
        mv = client.get_model_version_by_alias(model.lower(), "Staging")
        if not mv.run_id:
            # A registered version can exist without a run behind it (registered by hand, or
            # the run was deleted). There are no metrics to compare, which is not the same as
            # metrics that compare badly, so the caller's "unavailable" branch is the honest one.
            return None
        run = client.get_run(mv.run_id)
        return {k: float(v) for k, v in run.data.metrics.items()}
    except Exception:
        return None


def _staging_version(model: str) -> str | None:
    """The Staging alias's version number, or None if it cannot be resolved.

    The cycle otherwise never learns a version — ``_do_promote`` re-resolves the alias — but the
    C3 gate compares *a candidate version* against a baseline alias, so it needs one.
    """
    try:
        import mlflow

        return str(
            mlflow.MlflowClient().get_model_version_by_alias(model.lower(), "Staging").version
        )
    except Exception:
        return None


def _alias_version(model: str, alias: str) -> str | None:
    """The version an alias currently points at — i.e. the one a rollback would restore."""
    try:
        import mlflow

        return str(mlflow.MlflowClient().get_model_version_by_alias(model.lower(), alias).version)
    except Exception:
        return None


def _declare_rollback(action: str, model: str, alias: str = "Production") -> str | None:
    """Record how this action would be undone, before it is taken (ADR 0113 decision 2).

    The inverse is built from the alias's *current* version, read now: once the action has run,
    the thing a rollback would restore is no longer what the alias points at. Returns the ref, or
    ``None`` when the previous version cannot be resolved — in which case the caller is refused
    rather than proceeding with an undo path that does not exist.
    """
    from examlops.evidence import with_rollback_ref
    from examlops.rollback import build_rollback_ref

    previous = _alias_version(model, alias)
    if previous is None:
        return None
    ref = build_rollback_ref(action, model=model, previous_version=previous, alias=alias)
    if ref:
        with_rollback_ref(ref)
    return ref


def _do_promote(model: str, from_alias: str = "Staging", to_alias: str = "Production") -> None:
    """Promote the model's Staging version to Production via MLflow."""
    import mlflow

    from examlops import events

    client = mlflow.MlflowClient()
    mv = client.get_model_version_by_alias(model.lower(), from_alias)
    client.set_registered_model_alias(model.lower(), to_alias, mv.version)
    events.alias_changed(model, to_alias, mv.version, actor="autopilot", via="autopilot")


# ── core cycle logic ─────────────────────────────────────────────────────────


class _RunKilled(RuntimeError):
    """Raised when a live-run interrupt kills this cycle (ADR 0113 decision 4)."""


def _interrupt_checkpoint(run_id: int, actor: str) -> None:
    """Poll the live-run interrupt flag between actions.

    ``kill`` aborts the cycle (audited). ``freeze`` pauses here — polling until the flag is
    cleared (resume) or escalated to kill — so an operator can hold a cycle mid-flight while
    they look at something, without losing it.
    """
    import time as _time

    action = blast_radius.pending_interrupt(run_id)
    if action == "kill":
        write_audit_event("autopilot", actor, "run_killed", str(run_id), None)
        raise _RunKilled(f"run {run_id} killed by operator interrupt")
    if action == "freeze":
        write_audit_event("autopilot", actor, "run_frozen", str(run_id), None)
        deadline = _time.monotonic() + _lease_ttl_s()
        while _time.monotonic() < deadline:
            _time.sleep(2)
            action = blast_radius.pending_interrupt(run_id)
            if action == "kill":
                write_audit_event("autopilot", actor, "run_killed", str(run_id), None)
                raise _RunKilled(f"run {run_id} killed while frozen")
            if action is None:
                write_audit_event("autopilot", actor, "run_resumed", str(run_id), None)
                return
        write_audit_event(
            "autopilot", actor, "run_killed", str(run_id), {"reason": "freeze timed out"}
        )
        raise _RunKilled(f"run {run_id} froze past the lease TTL — killed")


def _behaviour_gate(
    behaviour: str,
    model: str,
    change: str,
    extent: dict[str, float],
    *,
    actor: str,
    dry_run: bool,
    skipped: list,
    hitl: list,
    blocks: list,
) -> bool:
    """ADR 0113 per-model gate: quarantine → per-behaviour autonomy → blast-radius contract.

    Returns True when the action may proceed autonomously. Every other outcome is recorded on
    the cycle report (and audited on a live run) with the exact clause or state that stopped it.
    """
    q = blast_radius.quarantine_reason(model)
    if q:
        skipped.append({"model": model, "reason": f"quarantined: {q}"})
        return False

    level = blast_radius.get_autonomy(behaviour)
    if level == blast_radius.DISABLED:
        skipped.append({"model": model, "reason": f"{behaviour} autonomy is DISABLED"})
        return False
    if level == blast_radius.REVIEW:
        hitl.append({"model": model, "gate": behaviour, "reason": "autonomy is REVIEW"})
        if not dry_run:
            write_audit_event(
                "autopilot",
                actor,
                "human_approval_required",
                model,
                {"gate": behaviour, "reason": "per-behaviour autonomy is REVIEW"},
            )
        return False

    allowed, clause = blast_radius.check_change(behaviour, change, extent)
    if not allowed:
        blocks.append({"model": model, "gate": behaviour, "reason": clause})
        if not dry_run:
            write_audit_event(
                "autopilot", actor, "contract_denied", model, {"gate": behaviour, "clause": clause}
            )
        return False
    return True


def _classify_anomaly_for(model: str, drift_signal: dict[str, Any]):
    """Classify the anomaly behind a drift breach (ADR 0114), or ``None`` if it cannot be.

    Returning ``None`` on failure keeps a broken detector from taking the whole loop down;
    the failure is written to the audit trail so it is not merely absent.
    """
    from examlops.corruption import assess_model

    try:
        _signal, classification = assess_model(model, drift_signal)
        return classification
    except Exception as exc:  # pragma: no cover - defensive
        # `audit_best_effort`, not `write_audit_event`: this write is inside the handler that is
        # absorbing the detector's failure, and `run_cycle`'s only outer handler catches
        # `_RunKilled`. An audit write that raised here would replace a contained failure with an
        # uncontained one and break this function's own promise to return None.
        audit_best_effort(
            "autopilot", _actor(), "corruption_detection_error", model, {"error": str(exc)}
        )
        return None


def _record_suppression(actor: str, model: str, z: float, classification) -> None:
    """Record a suppressed retrain — a retrain that does not happen leaves no other trace."""
    from examlops.data.drift import record_drift_event

    detail = {"z_score": z, **classification.as_dict()}
    record_drift_event(
        model,
        "corruption",
        severity="CRITICAL" if classification.klass == "suspected_hardware" else "WARNING",
        score=z,
        metric="anomaly_class",
        detail=detail,
    )
    write_audit_event("autopilot", actor, "autopilot_retrain_suppressed", model, detail)


def run_cycle(
    model_filter: str | None = None,
    dry_run: bool = False,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """Execute one autopilot cycle and return a summary dict.

    This is the single entry point for the closed loop. It is a regular
    function (not a CLI callback) so tests can call it directly.
    """
    from examlops.cli._config import load_config
    from examlops.data import get_db

    init_db()
    actor = _actor()

    # ── kill-switch ─────────────────────────────────────────────────────────
    # A dry-run is a preview: it takes no lease, triggers no retrain and promotes nothing, so
    # the kill-switch does not apply to it. Requiring `enable` first would mean arming the loop
    # in order to inspect it, which inverts the safety property the switch exists for — and the
    # CLI's own guidance was circular about it ("no runs yet → try --dry-run" → "disabled → run
    # enable"). The run is still recorded with enabled_state="disabled", so history never
    # implies the loop was armed when it was not.
    enabled = _is_enabled()
    if not enabled and not dry_run:
        audit_best_effort(
            "autopilot",
            actor,
            "autopilot_skipped",
            model_filter,
            {"reason": "kill-switch disabled"},
        )
        return {"enabled": False, "reason": "autopilot disabled — run: exa autopilot enable"}

    # ── distributed cycle lease (item 0.12) — one active cycle at a time ─────
    lease_holder = f"{socket.gethostname()}:{os.getpid()}"
    lease_held = False
    if not dry_run:
        lease_held = claim_autopilot_lease(lease_holder, _lease_ttl_s())
        if not lease_held:
            audit_best_effort(
                "autopilot",
                actor,
                "autopilot_skipped",
                model_filter,
                {"reason": "another autopilot cycle holds the lease"},
            )
            return {
                "enabled": True,
                "skipped": True,
                "reason": "another autopilot cycle is already running (lease held)",
            }
    cycle_ctx = None
    try:
        run_id = create_autopilot_run(
            triggered_by=triggered_by,
            model_filter=model_filter,
            dry_run=dry_run,
            enabled_state="enabled" if enabled else "disabled",
        )
        # ADR 0110: everything this cycle writes is one correlated, *autonomous* unit of work.
        # The autopilot is the platform acting on its own initiative, which is the mode ADR 0113
        # gates hardest — and until it is declared, an auditor cannot tell a cycle's retrain from
        # one a person asked for. Entered here rather than around the whole function so the
        # lease bookkeeping in `finally` stays outside the unit of work it is not part of; the
        # matching exit is in that same `finally`.
        cycle_ctx = correlated(mode=AUTONOMOUS, on_behalf_of=triggered_by)
        cycle_ctx.__enter__()

        retrains: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        hitl: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        suppressed: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []

        cfg = load_config()

        # ── 1. Drift scan → trigger retrains ────────────────────────────────────
        auto_retrain_cfgs = {c["model"]: c for c in list_drift_auto_retrain() if c["enabled"]}

        # Determine which models to scan
        if model_filter:
            scan_models = [model_filter] if model_filter in auto_retrain_cfgs else []
            if model_filter and model_filter not in auto_retrain_cfgs:
                skipped.append({"model": model_filter, "reason": "no auto-retrain config"})
        else:
            scan_models = list(auto_retrain_cfgs.keys())

        import datetime

        max_retrains = _max_retrains_per_cycle()
        triggered_this_cycle = 0

        for model in scan_models:
            # A previous iteration's declared inverse must not leak onto this model's events.
            clear_rollback_ref()
            _interrupt_checkpoint(run_id, actor)
            if not _behaviour_gate(
                "drift_auto_retrain",
                model,
                "pipeline_run",
                {"models": 1, "runs": 1},
                actor=actor,
                dry_run=dry_run,
                skipped=skipped,
                hitl=hitl,
                blocks=blocks,
            ):
                continue
            ar = auto_retrain_cfgs[model]

            # The same computation `exa drift status` and `exa drift trigger` use: its window and
            # the site's configured drift provider. This used to be a private copy with a
            # 50-prediction window and hard-coded 2.0/3.0 thresholds, so the autopilot and the CLI
            # could disagree about the same model at the same moment.
            row = drift_status.model_row(model)
            if row is None:
                skipped.append({"model": model, "reason": "no drift snapshots"})
                continue
            z, status = float(row["z_score"]), str(row["status"])

            # The configured min_z_score is the ONE threshold — same rule as `exa drift trigger`.
            # A hard-coded status pre-filter (WARNING = z≥2.0) on top of it silently overrode any
            # operator-configured threshold below 2.0: the two consumers of the same
            # drift_auto_retrain config disagreed on when to fire. Status still names
            # OK-no-baseline (z=0.0 there, so the threshold handles it).
            if z < ar["min_z_score"]:
                skipped.append(
                    {"model": model, "reason": f"z={z:.2f} below threshold {ar['min_z_score']}"}
                )
                continue

            # Cooldown check
            if ar["last_triggered"]:
                try:
                    last = datetime.datetime.fromisoformat(ar["last_triggered"])
                    elapsed = (datetime.datetime.utcnow() - last).total_seconds()
                    if elapsed < ar["cooldown_s"]:
                        skipped.append(
                            {
                                "model": model,
                                "reason": f"cooldown {elapsed:.0f}/{ar['cooldown_s']}s",
                            }
                        )
                        continue
                except ValueError:
                    pass

            # ADR 0114: classify before remediating. The autopilot promotes on its own
            # road, so the suppression has to hold here too — otherwise the closed loop is
            # the one path that can still retrain on a hardware fault.
            classification = _classify_anomaly_for(model, {"z_score": z, "status": status})
            if classification is not None and not classification.autonomous_remediation_allowed:
                _record_suppression(actor, model, z, classification)
                suppressed.append(
                    {
                        "model": model,
                        "class": classification.klass,
                        "reason": classification.reason,
                        "remediation": classification.remediation,
                    }
                )
                continue

            # Policy check: autopilot_trigger
            outcome, reason = _policy_decide(
                "autopilot_trigger",
                {"model": model, "z_score": z, "dataset": ar["dataset_name"]},
            )
            if outcome == "deny":
                blocks.append({"model": model, "gate": "autopilot_trigger", "reason": reason})
                audit_best_effort(
                    "autopilot",
                    actor,
                    "policy_denied",
                    model,
                    {"gate": "autopilot_trigger", "z_score": z, "reason": reason},
                )
                continue
            if outcome == "require_approval":
                hitl.append({"model": model, "gate": "autopilot_trigger", "z_score": z})
                audit_best_effort(
                    "autopilot",
                    actor,
                    "human_approval_required",
                    model,
                    {
                        "gate": "autopilot_trigger",
                        "z_score": z,
                        "message": "autopilot trigger requires human approval",
                    },
                )
                continue

            # Per-cycle storm cap: never fire more than N retrains in one cycle. Checked for the
            # preview too — it used to apply only to a live run, so with three drifting models and
            # a cap of one the dry-run promised three retrains where the real cycle did one. A
            # preview whose numbers do not match the cycle it previews is worse than no preview.
            if triggered_this_cycle >= max_retrains:
                skipped.append(
                    {
                        "model": model,
                        "reason": f"per-cycle retrain cap ({max_retrains}) reached",
                    }
                )
                continue

            # ADR 0113 decision 2: declare the inverse, then refuse if there is none. Evaluated
            # *before* the dry-run branch, for the reason the storm cap above already learned —
            # a preview whose numbers do not match the cycle it previews is worse than no
            # preview. Reading the alias version is a read; nothing is mutated here.
            _declare_rollback("autopilot_retrain_triggered", model)
            try:
                require_rollback("autopilot_retrain_triggered")
            except AutonomousActionRefused as exc:
                refused.append({"model": model, "action": "retrain", "reason": str(exc)})
                if not dry_run:
                    # In a handler: must not be able to raise. See `audit_best_effort`.
                    audit_best_effort(
                        "autopilot",
                        actor,
                        "autonomous_action_refused",
                        model,
                        {"attempted": "autopilot_retrain_triggered", "reason": str(exc)},
                    )
                continue

            # Trigger retrain (or dry-run)
            if dry_run:
                triggered_this_cycle += 1
                retrains.append({"model": model, "z_score": z, "action": "would retrain"})
            else:
                # Atomic cooldown claim — closes the TOCTOU: check-and-stamp is one locked write, so an
                # overlapping cycle cannot also claim this model and double-fire the retrain.
                if not claim_drift_trigger(model, ar["cooldown_s"]):
                    skipped.append(
                        {
                            "model": model,
                            "reason": "cooldown active (claimed by a concurrent cycle)",
                        }
                    )
                    continue
                try:
                    result = _call_retrain(cfg, model, ar["dataset_name"])
                    triggered_this_cycle += 1
                    # The retrain has already fired. Letting a failed audit write reach the
                    # handler below reported an action that HAPPENED as a "retrain error" and
                    # dropped it from `retrains`, so the cycle summary under-counted a real
                    # autonomous action. The loss is logged and counted instead.
                    audit_best_effort(
                        "autopilot",
                        actor,
                        "autopilot_retrain_triggered",
                        model,
                        {
                            "z_score": z,
                            "flow_run_id": result.get("flow_run_id"),
                            "command_id": result.get("command_id"),
                        },
                    )
                    retrains.append(
                        {"model": model, "z_score": z, "flow_run_id": result.get("flow_run_id")}
                    )
                except Exception as exc:
                    # Never `write_audit_event` in a handler: a raise here escapes `run_cycle`
                    # (whose outer handler catches only `_RunKilled`), so one model's retrain
                    # error would end the whole cycle with its run row never updated.
                    audit_best_effort(
                        "autopilot", actor, "autopilot_retrain_error", model, {"error": str(exc)}
                    )
                    skipped.append({"model": model, "reason": f"retrain error: {exc}"})

        # ── 2. Promote: check models with promotion rules + Staging version ──────
        with get_db() as conn:
            rule_rows = conn.execute("SELECT model FROM promotion_rules WHERE enabled=1").fetchall()
        promo_models = [r["model"] for r in rule_rows]
        if model_filter:
            promo_models = [m for m in promo_models if m.upper() == model_filter.upper()]

        for model in promo_models:
            clear_rollback_ref()
            _interrupt_checkpoint(run_id, actor)
            rule = get_promotion_rule(model)
            if not rule:
                continue
            if not _behaviour_gate(
                "autopilot_promote",
                model,
                f"model_alias:{rule['to_alias']}",
                {"models": 1, "aliases": 1},
                actor=actor,
                dry_run=dry_run,
                skipped=skipped,
                hitl=hitl,
                blocks=blocks,
            ):
                continue

            # Get Staging metrics
            metrics = _get_staging_metrics(model)
            if metrics is None:
                skipped.append(
                    {"model": model, "reason": "no Staging version or MLflow unavailable"}
                )
                continue

            metric_val = metrics.get(rule["metric"])
            if metric_val is None:
                skipped.append(
                    {"model": model, "reason": f"metric '{rule['metric']}' not in Staging run"}
                )
                continue

            # Metric gate (promotion provider)
            try:
                from examlops.promotion_providers import resolve_promotion_eval_fn

                _eval = resolve_promotion_eval_fn()
                passes, _ = _eval(metric_val, rule["threshold"], rule["operator"])
            except Exception:
                op_map = {
                    "lt": lambda v, t: v < t,
                    "lte": lambda v, t: v <= t,
                    "gt": lambda v, t: v > t,
                    "gte": lambda v, t: v >= t,
                }
                op_fn = op_map.get(rule["operator"], lambda v, t: False)
                passes = op_fn(metric_val, rule["threshold"])

            if not passes:
                skipped.append(
                    {
                        "model": model,
                        "reason": f"metric {rule['metric']}={metric_val:.4f} does not pass {rule['operator']} {rule['threshold']}",
                    }
                )
                continue

            # ADR 0111: an LLM judge that has never been measured may not decide what reaches
            # production. The autopilot promotes without going through run_eval_gate, so the
            # eligibility rule is enforced here too — otherwise the closed loop would be the
            # one road around it.
            from examlops.evaluation.gate import judge_eligibility_for_model

            judge_ok, judge_failures, judge_name = judge_eligibility_for_model(model)
            if not judge_ok:
                blocks.append(
                    {
                        "model": model,
                        "gate": "autopilot_promote",
                        "reason": f"judge {judge_name!r} is not gate-eligible: "
                        + ", ".join(judge_failures),
                    }
                )
                audit_best_effort(
                    "autopilot",
                    _actor(),
                    "autopilot_promote_blocked",
                    model,
                    {
                        "gate": "autopilot_promote",
                        "judge": judge_name,
                        "judge_failures": judge_failures,
                        "adr": "0111",
                    },
                )
                continue

            # C3 — the eval regression gate. `exa eval gate set --mode block` reads as "this
            # guards promotion of this model", and it guarded `exa pipeline promote` only: the
            # autopilot promotes the same model to the same alias and never met it. The
            # promotion *rule* above is an absolute threshold on one metric; the C3 gate is the
            # only check that compares the candidate against the baseline alias, so a model can
            # clear `rmse < 5.0` while having regressed from 2.0 — refused on one road,
            # promoted on the other.
            from examlops.data.evaluation import get_eval_gate

            if get_eval_gate(model) is not None:
                version = _staging_version(model)
                if version is None:
                    reason = (
                        "eval gate is configured but the Staging version could not be resolved, "
                        "so the gate could not be evaluated"
                    )
                    blocks.append({"model": model, "gate": "autopilot_promote", "reason": reason})
                    audit_best_effort(
                        "autopilot",
                        actor,
                        "autopilot_promote_blocked",
                        model,
                        {"gate": "autopilot_promote", "reason": reason},
                    )
                    continue
                from examlops.evaluation.gate import run_eval_gate

                gate_result = run_eval_gate(
                    model,
                    version,
                    higher_is_better=rule["operator"] in ("gt", "gte"),
                )
                if gate_result is not None and not gate_result.passed:
                    failing = [m.name for m in gate_result.metrics if m.failed]
                    reason = f"eval gate FAILED ({gate_result.mode}): {', '.join(failing)}"
                    blocks.append({"model": model, "gate": "autopilot_promote", "reason": reason})
                    audit_best_effort(
                        "autopilot",
                        actor,
                        "autopilot_promote_blocked",
                        model,
                        {
                            "gate": "autopilot_promote",
                            "version": version,
                            "failing_metrics": failing,
                            "mode": gate_result.mode,
                        },
                    )
                    continue

            # Policy check: autopilot_promote. Expose synthetic-only training as context so a
            # D5 policy rule can refuse to auto-promote a synthetic-only model (A7 spec R5/GWT-5).
            from examlops.promotion_gates import synthetic_only_training

            only_synth, _synth_revs = synthetic_only_training(model)
            outcome, reason = _policy_decide(
                "autopilot_promote",
                {
                    "model": model,
                    "metric": rule["metric"],
                    "metric_val": metric_val,
                    "threshold": rule["threshold"],
                    "operator": rule["operator"],
                    "synthetic_only": only_synth,
                },
            )
            if outcome == "deny":
                blocks.append({"model": model, "gate": "autopilot_promote", "reason": reason})
                audit_best_effort(
                    "autopilot",
                    actor,
                    "policy_denied",
                    model,
                    {
                        "gate": "autopilot_promote",
                        "metric": rule["metric"],
                        "metric_val": metric_val,
                    },
                )
                continue
            if outcome == "require_approval":
                hitl.append(
                    {
                        "model": model,
                        "gate": "autopilot_promote",
                        "metric": rule["metric"],
                        "metric_val": metric_val,
                    }
                )
                audit_best_effort(
                    "autopilot",
                    actor,
                    "human_approval_required",
                    model,
                    {
                        "gate": "autopilot_promote",
                        "metric": rule["metric"],
                        "metric_val": metric_val,
                        "message": "autopilot promotion requires human approval",
                    },
                )
                continue

            # ADR 0113 decision 2: a promotion is a registered MUTATING action — declare the
            # inverse (restore the previous to_alias version) and refuse when there is none
            # (e.g. a first promotion with nothing to restore). Declared BEFORE _do_promote
            # moves the alias, because afterwards the previous version is unreadable; and
            # before the dry-run branch so the preview matches the cycle it previews.
            _declare_rollback("autopilot_promoted", model, alias=rule["to_alias"])
            try:
                require_rollback("autopilot_promoted")
            except AutonomousActionRefused as exc:
                refused.append({"model": model, "action": "promote", "reason": str(exc)})
                if not dry_run:
                    # In a handler: must not be able to raise. See `audit_best_effort`.
                    audit_best_effort(
                        "autopilot",
                        actor,
                        "autonomous_action_refused",
                        model,
                        {"attempted": "autopilot_promoted", "reason": str(exc)},
                    )
                continue

            # Promote
            if dry_run:
                promotions.append(
                    {
                        "model": model,
                        "metric": rule["metric"],
                        "metric_val": metric_val,
                        "action": "would promote",
                    }
                )
            else:
                # Opt-in rollback point before a live promotion (best-effort, never blocks).
                if os.getenv("EXAMLOPS_BACKUP_ON_PROMOTE", "").lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                ):
                    from examlops.backup import auto_backup_before

                    auto_backup_before(f"autopilot-promote:{model}")
                try:
                    _do_promote(model, from_alias=rule["from_alias"], to_alias=rule["to_alias"])
                    # The promotion has already happened; a failed audit write must not be
                    # reported as a promote error, nor drop it from `promotions`.
                    audit_best_effort(
                        "autopilot",
                        actor,
                        "autopilot_promoted",
                        model,
                        {
                            "metric": rule["metric"],
                            "metric_val": metric_val,
                            "from": rule["from_alias"],
                            "to": rule["to_alias"],
                        },
                    )
                    promotions.append(
                        {
                            "model": model,
                            "metric": rule["metric"],
                            "metric_val": metric_val,
                            "promoted_to": rule["to_alias"],
                        }
                    )
                except Exception as exc:
                    # As above: a failed audit write must not convert one model's promote error
                    # into an escape from the cycle.
                    audit_best_effort(
                        "autopilot", actor, "autopilot_promote_error", model, {"error": str(exc)}
                    )
                    skipped.append({"model": model, "reason": f"promote error: {exc}"})

        # ── 3. Record run ────────────────────────────────────────────────────────
        summary = {
            "retrains": retrains,
            "promotions": promotions,
            "policy_blocks": blocks,
            "human_required": hitl,
            "skipped": skipped,
            "suppressed": suppressed,
            "refused": refused,
        }
        update_autopilot_run(
            run_id,
            retrains_triggered=len(retrains),
            promotions_made=len(promotions),
            policy_blocks=len(blocks),
            human_required=len(hitl),
            skipped=len(skipped),
            summary=summary,
        )
        # After the work and after `update_autopilot_run`: a raise here discarded the
        # whole cycle's result — the telemetry anchor and the event publish below never
        # ran and the caller got a traceback instead of the summary. Both of those are
        # already documented as 'must never fail the cycle'; so is this.
        audit_best_effort(
            "autopilot",
            actor,
            "autopilot_cycle_complete",
            model_filter,
            {
                "run_id": run_id,
                "dry_run": dry_run,
                "retrains": len(retrains),
                "promotions": len(promotions),
                "policy_blocks": len(blocks),
                "human_required": len(hitl),
                "suppressed": len(suppressed),
                "refused": len(refused),
            },
        )
        # Anchor the telemetry side tables into the chain (ADR 0110 decision 2) — the cycle is
        # the platform's natural cadence, and each anchor names its own range so the verifier
        # knows the guarantee. Best-effort: an anchoring failure must never fail the cycle.
        if not dry_run:
            try:
                from examlops.telemetry_anchor import anchor_telemetry

                anchor_telemetry(actor)
            except Exception:  # noqa: BLE001 - anchoring is best-effort here; cron covers gaps
                pass

        # Publish to the NovaFabric event backbone (item 1.3) so subscribers (dashboard SSE,
        # notifiers, downstream automations) react without polling. Best-effort: a broker outage
        # must never fail the cycle — the outbox row is durable and the relay retries.
        try:
            from examlops import events

            events.publish(
                "autopilot.cycle_complete",
                {
                    "run_id": run_id,
                    "model_filter": model_filter,
                    "retrains": len(retrains),
                    "promotions": len(promotions),
                    "dry_run": dry_run,
                },
            )
        except Exception:  # noqa: BLE001 - event publish is best-effort
            pass

        return {"run_id": run_id, "dry_run": dry_run, "kill_switch_enabled": enabled, **summary}
    except _RunKilled as exc:
        # Decision 4: a killed run ends cleanly — its record says so, and the cycle report
        # carries whatever was decided before the interrupt landed.
        update_autopilot_run(
            run_id,
            retrains_triggered=len(retrains),
            promotions_made=len(promotions),
            policy_blocks=len(blocks),
            human_required=len(hitl),
            skipped=len(skipped),
            summary={"interrupted": str(exc), "retrains": retrains, "promotions": promotions},
        )
        return {
            "run_id": run_id,
            "dry_run": dry_run,
            "kill_switch_enabled": enabled,
            "interrupted": str(exc),
            "retrains": retrains,
            "promotions": promotions,
        }
    finally:
        if cycle_ctx is not None:
            cycle_ctx.__exit__(None, None, None)
        if lease_held:
            release_autopilot_lease(lease_holder)


# ── CLI commands ─────────────────────────────────────────────────────────────


# ── Event-driven promotion (ADR 0085 × ADR 0124) ────────────────────────────────


class CycleBusy(RuntimeError):
    """Another autopilot cycle holds the lease. Raised so the event is redelivered later."""


def _has_enabled_promotion_rule(model: str) -> bool:
    from examlops.data import get_db

    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT model FROM promotion_rules WHERE enabled=1").fetchall()
    return any(str(r["model"]).upper() == model.upper() for r in rows)


def on_run_completed(event: dict[str, Any]) -> str:
    """React to ``retrain.run_completed``: run this model's cycle now, not at the next schedule.

    The cycle a retrain belongs to ends when it dispatches the run, so the candidate it trains
    waits for the next scheduled cycle to be considered for promotion. This runs that cycle as
    soon as the run finishes — the same :func:`run_cycle`, restricted to the model, so every gate
    applies unchanged (kill-switch, lease, policy, eval and judge gates, rollback declaration).
    It cannot retrain again: the retrain that just finished stamped the model's drift cooldown.

    Returns what happened; raises :class:`CycleBusy` when another cycle holds the lease, so the
    consumer redelivers the event with backoff instead of dropping it.
    """
    model = str((event.get("data") or {}).get("model_name") or "").strip()
    if not model:
        return "ignored"
    if not _is_enabled():
        return "disabled"  # quietly: an armed-off autopilot is not an event worth auditing
    if not _has_enabled_promotion_rule(model):
        return "no_rule"
    result = run_cycle(model_filter=model, triggered_by=f"event:{event.get('id', '')}")
    if result.get("skipped"):
        raise CycleBusy(str(result.get("reason") or "another cycle holds the lease"))
    return "cycle_ran"


_EXAMPLES_FOLLOW = (
    "Examples:\n\n"
    "  # Consider a retrained model for promotion the moment its run finishes\n"
    "  EXAMLOPS_NATS_URL=nats://localhost:14222 exa autopilot follow"
)


@app.command("follow", epilog=_EXAMPLES_FOLLOW)
def follow(
    wait: float = typer.Option(5.0, "--wait", help="Seconds a fetch waits for new events"),
) -> None:
    """Run each retrained model's promotion step as soon as its training run completes.

    A long-running consumer of ``retrain.run_completed`` on the NATS event backbone (durable name
    ``autopilot``: several copies share the work). Stop with Ctrl-C or SIGTERM.
    """
    import signal
    import threading

    from examlops.events.consumer import EventConsumer
    from examlops.events.nats_backend import subject_for

    if not os.getenv("EXAMLOPS_NATS_URL", "").strip():
        _output.error(
            "exa autopilot follow consumes the NATS event backbone, which is not configured",
            hint="Set EXAMLOPS_NATS_URL (and EXAMLOPS_EVENT_PUBLISHER=nats where events are "
            "relayed); without it the scheduled `exa autopilot run` is the only trigger.",
        )
        return
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    consumer = EventConsumer(
        "autopilot", on_run_completed, subjects=subject_for("retrain.run_completed"), wait=wait
    )
    _output.info(
        "Following retrain.run_completed — each finished run gets its model's autopilot cycle "
        f"(kill-switch {'ENABLED' if _is_enabled() else 'disabled: events are acknowledged only'})"
    )
    consumer.run_forever(stop)


@app.command(epilog=_EXAMPLES_RUN)
def run(
    model: str | None = typer.Argument(None, help="Restrict cycle to one model (default: all)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without acting"),
) -> None:
    """Run one autopilot cycle: drift scan → policy → retrain → metrics → policy → promote."""
    result = run_cycle(model_filter=model, dry_run=dry_run, triggered_by="manual")

    if not result.get("enabled", True):
        _output.warning(result.get("reason", "autopilot disabled"))
        raise typer.Exit(0)

    if not result.get("kill_switch_enabled", True):
        # Previewing with the switch off is the intended order; say so, so nobody reads a
        # dry-run summary as evidence that the loop is live.
        _output.warning(
            "preview only — the autopilot kill-switch is DISABLED, so nothing here would run "
            "on a schedule until: exa autopilot enable"
        )

    if _output.json_mode:
        _output.print_json(result)
        return

    retrains = result.get("retrains", [])
    promotions = result.get("promotions", [])
    blocks = result.get("policy_blocks", [])
    hitl = result.get("human_required", [])
    suppressed = result.get("suppressed", [])
    refused = result.get("refused", [])
    suffix = " [dry-run]" if dry_run else ""

    if retrains:
        _output.print_table(
            f"Retrains Triggered{suffix}",
            ["Model", "Z-Score", "Flow Run ID"],
            [
                [
                    r["model"],
                    f"{r.get('z_score', 0):.2f}",
                    r.get("flow_run_id") or r.get("action") or "—",
                ]
                for r in retrains
            ],
        )
    if promotions:
        _output.print_table(
            f"Promoted{suffix}",
            ["Model", "Metric", "Value", "Result"],
            [
                [
                    p["model"],
                    p["metric"],
                    f"{p['metric_val']:.4f}",
                    p.get("promoted_to") or p.get("action") or "—",
                ]
                for p in promotions
            ],
        )
    if blocks:
        _output.print_table(
            "Policy Blocks",
            ["Model", "Gate", "Reason"],
            [[b["model"], b["gate"], b.get("reason") or "—"] for b in blocks],
        )
    if refused:
        _output.print_table(
            "Refused — no declared way to undo the action (ADR 0113)",
            ["Model", "Action", "Reason"],
            [[r["model"], r["action"], r["reason"]] for r in refused],
        )
    if suppressed:
        _output.print_table(
            "Suppressed — classification did not permit an autonomous retrain (ADR 0114)",
            ["Model", "Class", "Remediation", "Reason"],
            [[s["model"], s["class"], s["remediation"], s["reason"]] for s in suppressed],
        )
    if hitl:
        for h in hitl:
            _output.warning(
                f"HUMAN ACTION REQUIRED — model {h['model']} gate {h['gate']} "
                "blocked by require_approval policy. See: exa audit"
            )
    if not retrains and not promotions and not hitl and not suppressed and not refused:
        _output.ok("Autopilot cycle complete — no actions needed")
    elif not dry_run:
        _output.ok(
            f"Autopilot cycle complete — "
            f"{len(retrains)} retrains · {len(promotions)} promotions · "
            f"{len(blocks)} policy blocks · {len(hitl)} HITL"
        )


def _suppressed_count(run: dict[str, Any]) -> int:
    """How many models a recorded cycle suppressed, read from its stored summary."""
    raw = run.get("summary")
    if not raw:
        return 0
    try:
        return len(json.loads(raw).get("suppressed", []) or [])
    except (ValueError, TypeError, AttributeError):
        return 0


@app.command("status", epilog=_EXAMPLES_STATUS)
def status_cmd(
    last: int = typer.Option(10, "--last", help="Number of recent runs to show"),
) -> None:
    """Show recent autopilot run history."""
    runs = list_autopilot_runs(last_n=last)
    enabled = _is_enabled()
    contracts = {
        name: {**c.as_dict(), "effective_autonomy": blast_radius.get_autonomy(name)}
        for name, c in blast_radius.load_contracts().items()
    }
    if _output.json_mode:
        _output.print_json({"enabled": enabled, "contracts": contracts, "runs": runs})
        return
    state_str = "[green]ENABLED[/green]" if enabled else "[red]DISABLED[/red]"
    _output.info(f"Autopilot kill-switch: {state_str}")
    # ADR 0113 decision 1: every enabled behaviour's contract is printed VERBATIM — reading
    # the autopilot's bounds is a status command away, not a source dive.
    import yaml as _yaml

    for name, c in contracts.items():
        _output.info(f"{name} — autonomy: {c['effective_autonomy']}")
        _output.info(_yaml.safe_dump(c, sort_keys=False).rstrip())
    if not runs:
        _output.ok("No autopilot runs recorded yet — run: exa autopilot run --dry-run")
        return
    _output.print_table(
        "Recent Autopilot Runs",
        ["ID", "Run At", "Dry-run", "Retrains", "Promotions", "Blocks", "HITL", "Suppressed"],
        [
            [
                str(r["id"]),
                r["run_at"],
                "yes" if r["dry_run"] else "no",
                str(r["retrains_triggered"]),
                str(r["promotions_made"]),
                str(r["policy_blocks"]),
                str(r["human_required"]),
                # ADR 0114 suppressions have no column of their own — they live in the run's
                # summary. Without this a cycle that suppressed three models reads exactly like
                # a quiet one, which is the failure mode of every guard whose only success
                # signal is silence.
                str(_suppressed_count(r)),
            ]
            for r in runs
        ],
    )


@app.command()
def enable() -> None:
    """Enable the autopilot kill-switch (persistent, stored in platform.db)."""
    set_autopilot_config("enabled", "1")
    write_audit_event("cli", _actor(), "autopilot_enabled", None, {})
    _output.ok("Autopilot enabled — run: exa autopilot run --dry-run to test")


@app.command()
def contract(
    behaviour: str = typer.Argument("", help="Behaviour name; empty shows every contract"),
) -> None:
    """Show a behaviour's blast-radius contract verbatim (ADR 0113)."""
    import yaml as _yaml

    contracts = blast_radius.load_contracts()
    if behaviour:
        c = contracts.get(behaviour)
        if c is None:
            _output.error(f"No contract published for {behaviour!r} — known: {sorted(contracts)}")
            raise typer.Exit(1)
        selected = {behaviour: c}
    else:
        selected = contracts
    payload = {
        name: {**c.as_dict(), "effective_autonomy": blast_radius.get_autonomy(name)}
        for name, c in selected.items()
    }
    if _output.json_mode:
        _output.print_json(payload)
        return
    for name, d in payload.items():
        _output.info(name)
        _output.info(_yaml.safe_dump(d, sort_keys=False).rstrip())


@app.command()
def autonomy(
    behaviour: str = typer.Argument(..., help="Behaviour (e.g. drift_auto_retrain)"),
    level: str = typer.Argument(..., help="AUTONOMOUS | REVIEW | DISABLED"),
    ack: str = typer.Option(
        "", "--ack", help="Required when granting AUTONOMOUS: your recorded acknowledgment"
    ),
) -> None:
    """Set one behaviour's autonomy level (per rule, pausable, acknowledgment recorded)."""
    if behaviour not in blast_radius.load_contracts():
        _output.error(
            f"Unknown behaviour {behaviour!r} — known: {sorted(blast_radius.load_contracts())}"
        )
        raise typer.Exit(1)
    try:
        blast_radius.set_autonomy(behaviour, level, actor=_actor(), acknowledgment=ack)
    except ValueError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    _output.ok(f"{behaviour} autonomy → {level.upper()} (audited)")


@app.command()
def interrupt(
    run_id: int = typer.Argument(..., help="In-flight autopilot run id (exa autopilot status)"),
    kill: bool = typer.Option(False, "--kill", help="Abort the run at its next checkpoint"),
    freeze: bool = typer.Option(False, "--freeze", help="Pause the run until resumed"),
    reason: str = typer.Option("", "--reason", help="Why (recorded in the audit event)"),
) -> None:
    """Freeze or kill ONE in-flight autopilot run (ADR 0113 decision 4; audited)."""
    if kill == freeze:
        _output.error("Pass exactly one of --kill / --freeze")
        raise typer.Exit(1)
    blast_radius.request_interrupt(
        run_id, "kill" if kill else "freeze", actor=_actor(), reason=reason
    )
    _output.ok(f"Run {run_id} flagged: {'kill' if kill else 'freeze'} (applies at next checkpoint)")


@app.command()
def resume(
    run_id: int = typer.Argument(..., help="Frozen autopilot run id"),
) -> None:
    """Release a frozen run so it continues from its checkpoint."""
    blast_radius.clear_interrupt(run_id, actor=_actor())
    _output.ok(f"Run {run_id} resumed")


@app.command()
def quarantine(
    model: str = typer.Argument(..., help="Model to exclude from autonomous action"),
    reason: str = typer.Option("", "--reason", help="Why (recorded and shown on skips)"),
) -> None:
    """Quarantine a model: the autopilot skips it until released (audited)."""
    blast_radius.quarantine_model(model, actor=_actor(), reason=reason)
    _output.ok(f"{model} quarantined — release with: exa autopilot release {model}")


@app.command()
def release(
    model: str = typer.Argument(..., help="Quarantined model to release"),
) -> None:
    """Release a quarantined model back to autonomous eligibility (audited)."""
    blast_radius.release_model(model, actor=_actor())
    _output.ok(f"{model} released")


@app.command()
def disable() -> None:
    """Disable the autopilot kill-switch (persistent, stored in platform.db)."""
    set_autopilot_config("enabled", "0")
    write_audit_event("cli", _actor(), "autopilot_disabled", None, {})
    _output.ok("Autopilot disabled — all exa autopilot run cycles will be no-ops")

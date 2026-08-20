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

import os
import socket
from typing import Any

import typer

from examlops.cli import _output
from examlops.data import init_db
from examlops.data.audit import write_audit_event
from examlops.data.autopilot import (
    claim_autopilot_lease,
    create_autopilot_run,
    get_autopilot_config,
    list_autopilot_runs,
    release_autopilot_lease,
    set_autopilot_config,
    update_autopilot_run,
)
from examlops.data.drift import claim_drift_trigger, get_drift_baseline, list_drift_auto_retrain
from examlops.data.serving import get_promotion_rule

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
    "  exa autopilot run --model JPCP  # restrict to one model"
)
_EXAMPLES_STATUS = (
    "Examples:\n\n"
    "  exa autopilot status            # last 10 runs\n\n"
    "  exa autopilot status --last 20"
)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

_SNAPSHOT_WINDOW = 50  # same as drift.py

# Safety cap: the most retrains one cycle will fire, so a fleet-wide drift event (or a bug) can
# never launch an unbounded retrain storm. Overridable via EXAMLOPS_AUTOPILOT_MAX_RETRAINS.
_DEFAULT_MAX_RETRAINS_PER_CYCLE = 10


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


def _compute_z(preds: list[float], baseline: dict[str, float] | None) -> tuple[float, str]:
    """Return (z_score, status) for a list of recent predictions vs baseline."""
    if not preds or baseline is None:
        return 0.0, "OK (no baseline)"
    n = len(preds)
    mean = sum(preds) / n
    bstd = baseline.get("std", 0.0)
    if bstd <= 0:
        return 0.0, "OK"
    z = abs(mean - baseline["mean"]) / bstd
    status = "CRITICAL" if z >= 3.0 else ("WARNING" if z >= 2.0 else "OK")
    return round(z, 2), status


def _policy_decide(action: str, context: dict[str, Any]) -> tuple[str, str]:
    """Return (outcome, reason) from policy.decide, defaulting to ('allow', 'no-policy')."""
    try:
        from examlops.policy import decide

        decision = decide(action, context)
        return decision.action, decision.reason or ""
    except Exception:
        return "allow", "policy-unavailable"


# ── injectable helpers (monkeypatched in tests) ─────────────────────────────


def _call_retrain(cfg: Any, model: str, dataset: str) -> dict[str, Any]:
    """POST /retrain on the control plane. Returns the response dict."""
    from examlops.cli._client import post

    body = {"model_name": model, "dataset_name": dataset, "is_dummy": False}
    return post(f"{cfg.control_plane_url}/retrain", body, token=cfg.control_plane_token)


def _get_staging_metrics(model: str) -> dict[str, float] | None:
    """Return metrics dict for the Staging alias of a model, or None if unavailable."""
    try:
        import mlflow

        client = mlflow.MlflowClient()
        mv = client.get_model_version_by_alias(model.lower(), "Staging")
        run = client.get_run(mv.run_id)
        return {k: float(v) for k, v in run.data.metrics.items()}
    except Exception:
        return None


def _do_promote(model: str, from_alias: str = "Staging", to_alias: str = "Production") -> None:
    """Promote the model's Staging version to Production via MLflow."""
    import mlflow

    client = mlflow.MlflowClient()
    mv = client.get_model_version_by_alias(model.lower(), from_alias)
    client.set_registered_model_alias(model.lower(), to_alias, mv.version)


# ── core cycle logic ─────────────────────────────────────────────────────────


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
        write_audit_event(
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
            write_audit_event(
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
    try:
        run_id = create_autopilot_run(
            triggered_by=triggered_by,
            model_filter=model_filter,
            dry_run=dry_run,
            enabled_state="enabled" if enabled else "disabled",
        )

        retrains: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        hitl: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

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
            ar = auto_retrain_cfgs[model]

            # Read drift snapshots
            with get_db() as conn:
                snap_rows = conn.execute(
                    "SELECT prediction FROM drift_snapshots WHERE model=? "
                    "ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (model, _SNAPSHOT_WINDOW),
                ).fetchall()
            preds = [r["prediction"] for r in snap_rows]
            if not preds:
                skipped.append({"model": model, "reason": "no drift snapshots"})
                continue

            baseline = get_drift_baseline(model)
            z, status = _compute_z(preds, baseline)

            if status not in ("CRITICAL", "WARNING") or z < ar["min_z_score"]:
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

            # Policy check: autopilot_trigger
            outcome, reason = _policy_decide(
                "autopilot_trigger",
                {"model": model, "z_score": z, "dataset": ar["dataset_name"]},
            )
            if outcome == "deny":
                blocks.append({"model": model, "gate": "autopilot_trigger", "reason": reason})
                write_audit_event(
                    "autopilot",
                    actor,
                    "policy_denied",
                    model,
                    {"gate": "autopilot_trigger", "z_score": z, "reason": reason},
                )
                continue
            if outcome == "require_approval":
                hitl.append({"model": model, "gate": "autopilot_trigger", "z_score": z})
                write_audit_event(
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
                    write_audit_event(
                        "autopilot",
                        actor,
                        "autopilot_retrain_triggered",
                        model,
                        {"z_score": z, "flow_run_id": result.get("flow_run_id")},
                    )
                    retrains.append(
                        {"model": model, "z_score": z, "flow_run_id": result.get("flow_run_id")}
                    )
                except Exception as exc:
                    write_audit_event(
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
            rule = get_promotion_rule(model)
            if not rule:
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
                write_audit_event(
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
                write_audit_event(
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
                write_audit_event(
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
                    write_audit_event(
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
                    write_audit_event(
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
        write_audit_event(
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
            },
        )
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
    finally:
        if lease_held:
            release_autopilot_lease(lease_holder)


# ── CLI commands ─────────────────────────────────────────────────────────────


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
    if hitl:
        for h in hitl:
            _output.warning(
                f"HUMAN ACTION REQUIRED — model {h['model']} gate {h['gate']} "
                "blocked by require_approval policy. See: exa audit"
            )
    if not retrains and not promotions and not hitl:
        _output.ok("Autopilot cycle complete — no actions needed")
    elif not dry_run:
        _output.ok(
            f"Autopilot cycle complete — "
            f"{len(retrains)} retrains · {len(promotions)} promotions · "
            f"{len(blocks)} policy blocks · {len(hitl)} HITL"
        )


@app.command("status", epilog=_EXAMPLES_STATUS)
def status_cmd(
    last: int = typer.Option(10, "--last", help="Number of recent runs to show"),
) -> None:
    """Show recent autopilot run history."""
    runs = list_autopilot_runs(last_n=last)
    enabled = _is_enabled()
    if _output.json_mode:
        _output.print_json({"enabled": enabled, "runs": runs})
        return
    state_str = "[green]ENABLED[/green]" if enabled else "[red]DISABLED[/red]"
    _output.info(f"Autopilot kill-switch: {state_str}")
    if not runs:
        _output.ok("No autopilot runs recorded yet — run: exa autopilot run --dry-run")
        return
    _output.print_table(
        "Recent Autopilot Runs",
        ["ID", "Run At", "Dry-run", "Retrains", "Promotions", "Blocks", "HITL"],
        [
            [
                str(r["id"]),
                r["run_at"],
                "yes" if r["dry_run"] else "no",
                str(r["retrains_triggered"]),
                str(r["promotions_made"]),
                str(r["policy_blocks"]),
                str(r["human_required"]),
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
def disable() -> None:
    """Disable the autopilot kill-switch (persistent, stored in platform.db)."""
    set_autopilot_config("enabled", "0")
    write_audit_event("cli", _actor(), "autopilot_disabled", None, {})
    _output.ok("Autopilot disabled — all exa autopilot run cycles will be no-ops")

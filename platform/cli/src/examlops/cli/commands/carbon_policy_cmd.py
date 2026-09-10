"""`exa finops carbon policy` — carbon-aware placement must earn its place (ADR 0112 R-ec/R-ed)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Evaluate carbon-aware placement against simple baselines (R-ec) and re-test it (R-ed)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa finops carbon policy sample --out trace.json          # a synthetic demo trace\n\n"
    "  exa finops carbon policy evaluate carbon-aware --trace trace.json\n\n"
    "  exa finops carbon policy evaluate forecast-greedy --trace grid.json --record\n\n"
    "  exa finops carbon policy status carbon-aware\n\n"
    "  exa finops carbon policy list"
)


@app.command("evaluate", epilog=_EXAMPLES)
def evaluate(
    candidate: str = typer.Argument(
        ..., help="Placement provider (e.g. carbon-aware) or forecast-greedy"
    ),
    trace: str = typer.Option(
        ..., "--trace", help="JSON: {method, regions:{r:[g/kWh…]}, jobs:[…]}"
    ),
    margin: float | None = typer.Option(
        None, "--margin", help="pp the candidate must beat the best simple policy by (default 5)"
    ),
    retire_below: float | None = typer.Option(
        None, "--retire-below", help="% saving below which the capability is retired (default 2)"
    ),
    record: bool = typer.Option(
        False, "--record", help="Chain the result into the audit log — the gate reads it"
    ),
) -> None:
    """Measure a carbon policy against both simple baselines on one trace, and decide what ships."""
    from examlops.finops import carbon_policy as cp

    try:
        data = json.loads(Path(trace).read_text())
        jobs, tr = cp.load_trace(data)
        ev = cp.evaluate(jobs, tr, candidate, margin=margin, retire_below=retire_below)
    except (OSError, ValueError) as exc:
        _output.error(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unresolvable provider name, surfaced plainly
        _output.error(f"cannot evaluate '{candidate}': {exc}")
    if record:
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        cp.record_evaluation(ev, actor)
    if _output.json_mode:
        _output.print_json({**ev.as_dict(), "recorded": record})
        return
    order = [cp.AGNOSTIC, *cp.SIMPLE_POLICIES, candidate, cp.ORACLE]
    label = {
        cp.AGNOSTIC: "reference",
        "lowest-average-region": "simple",
        "threshold-shift": "simple",
        candidate: "candidate",
        cp.ORACLE: "upper bound (not deployable)",
    }
    _output.print_table(
        f"Carbon policy evaluation — {candidate} ({ev.jobs} jobs, {ev.horizon_h} h, "
        f"{len(ev.regions)} regions)",
        ["Policy", "Role", "kg CO2e", "Reduction vs agnostic"],
        [
            [p, label[p], f"{ev.emissions_g[p] / 1000:.3f}", f"{ev.reduction_pct[p]:.2f}%"]
            for p in order
        ],
    )
    verdict = (
        f"'{candidate}' beats '{ev.best_simple}' by {ev.advantage_pp:.2f} pp "
        f"(margin {ev.margin_pp:g} pp) → {ev.shipped} ships"
        if ev.decision == "candidate"
        else f"'{candidate}' does not beat '{ev.best_simple}' by the {ev.margin_pp:g} pp margin "
        f"({ev.advantage_pp:+.2f} pp) → the simple policy ships (R-ec)"
    )
    (_output.ok if ev.decision == "candidate" else _output.warning)(verdict)
    if ev.retired:
        _output.warning(
            f"Retire (R-ed): the shipped policy saves {ev.shipped_benefit_pct:.2f}% < "
            f"{ev.retire_below_pct:g}% — carbon input will be withheld from placement."
        )
    for note in ev.notes:
        _output.info(f"Note: {note}")
    if not record:
        _output.info("Not recorded (dry run). Add --record for the placement gate to use it.")
    elif ev.synthetic:
        _output.info("Recorded as synthetic: it documents the method and does not gate placement.")
    else:
        _output.info("Recorded in the audit chain; placement now follows this verdict.")


@app.command("status", epilog=_EXAMPLES)
def status(
    policy: str = typer.Argument(
        ..., help="Placement provider name (e.g. carbon-aware, carbon-simple)"
    ),
) -> None:
    """What placement will do with this policy right now, and why (the R-ec/R-ed gate)."""
    from examlops.finops import carbon_policy as cp
    from examlops.hpc_placement_providers import DOMAIN, register_builtins, weighs_carbon
    from examlops.providers.loader import resolve_provider

    register_builtins()
    try:
        provider = resolve_provider(DOMAIN, override=policy, group=DOMAIN)
    except Exception as exc:  # noqa: BLE001
        _output.error(f"unknown placement provider '{policy}': {exc}")
    payload: dict[str, Any]
    if not weighs_carbon(provider):
        payload = {
            "policy": policy,
            "weighs_carbon": False,
            "action": "allow",
            "reason": "does not weigh carbon — not subject to the R-ec gate",
        }
    else:
        d = cp.placement_gate(
            policy, carbon_primary=bool(getattr(provider, "carbon_primary", False))
        )
        payload = {
            "policy": policy,
            "weighs_carbon": True,
            **d.as_dict(),
            "gate_mode": cp.gate_mode(),
            "retest_days": cp.retest_days(),
        }
    if _output.json_mode:
        _output.print_json(payload)
        return
    action = payload["action"]
    msg = f"{policy}: {action} — {payload['reason']}"
    (_output.ok if action == "allow" else _output.warning)(msg)
    for note in list(payload.get("notes") or []):
        _output.info(f"Note: {note}")


@app.command("list", epilog=_EXAMPLES)
def list_(
    policy: str | None = typer.Argument(None, help="Only evaluations of this candidate"),
    limit: int = typer.Option(20, "--limit", help="How many, newest first"),
) -> None:
    """Recorded evaluations, newest first (read back from the audit chain)."""
    from examlops.finops import carbon_policy as cp

    rows = cp.list_evaluations(policy, limit=limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No carbon-policy evaluations recorded.")
        return
    _output.print_table(
        "Carbon-policy evaluations",
        [
            "When",
            "Candidate",
            "Best simple",
            "Advantage",
            "Ships",
            "Benefit",
            "Retired",
            "Synthetic",
        ],
        [
            [
                str(r.get("evaluated_at", ""))[:19],
                str(r.get("candidate")),
                str(r.get("best_simple")),
                f"{float(r.get('advantage_pp', 0)):+.2f} pp",
                str(r.get("shipped")),
                f"{float(r.get('shipped_benefit_pct', 0)):.2f}%",
                "yes" if r.get("retired") else "no",
                "yes" if r.get("synthetic") else "no",
            ]
            for r in rows
        ],
    )


@app.command("sample", epilog=_EXAMPLES)
def sample(
    out: str = typer.Option(..., "--out", help="Where to write the synthetic trace JSON"),
    days: int = typer.Option(14, "--days", help="Trace length in days"),
    jobs: int = typer.Option(60, "--jobs", help="Number of jobs"),
    seed: int = typer.Option(7, "--seed", help="Random seed (the trace is deterministic per seed)"),
) -> None:
    """Write a synthetic demo trace. Evaluations over it are marked synthetic and gate nothing."""
    from examlops.finops.carbon_policy import synthetic_trace

    if days < 3 or jobs < 1:
        _output.error("--days must be >= 3 and --jobs >= 1")
    data = synthetic_trace(days=days, jobs=jobs, seed=seed)
    Path(out).write_text(json.dumps(data))
    if _output.json_mode:
        _output.print_json(
            {"out": out, "regions": sorted(data["regions"]), "jobs": len(data["jobs"])}
        )
        return
    _output.ok(f"Wrote a synthetic {days}-day trace ({len(data['jobs'])} jobs, 3 regions) to {out}")

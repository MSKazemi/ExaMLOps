"""C2/C3 — `exa eval run` (continuous eval) + `exa eval gate` (regression gate)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Continuous evaluation suites and the regression gate",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_RUN = (
    "Examples:\n\n"
    "  exa eval run smoke --items ./eval/items.jsonl --model JPCP\n\n"
    "  exa eval run smoke --items ./eval/items.jsonl --model JPCP --sample 20\n\n"
    "  exa --json eval run smoke --items ./eval/items.jsonl --model JPCP"
)


def _actor() -> str | None:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER")


def _load_items(path: str):
    from examlops.evaluation import EvalItem

    items = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        items.append(
            EvalItem(
                output=str(d.get("output", "")),
                reference=d.get("reference"),
                prompt=d.get("prompt"),
                metadata=d.get("metadata", {}),
            )
        )
    return items


@app.command("run", epilog=_EXAMPLES_RUN)
def run(
    suite: str = typer.Argument(..., help="Suite name (label for the persisted results)"),
    model: str = typer.Option(..., "--model", help="Model the suite evaluates"),
    items: str = typer.Option(..., "--items", help="JSONL of {output, reference?, prompt?}"),
    version: str | None = typer.Option(None, "--version", help="Candidate model version"),
    alias: str | None = typer.Option(None, "--alias", help="Alias being evaluated"),
    sample: int | None = typer.Option(None, "--sample", help="Sample N items by request_hash"),
    dataset_revision: str | None = typer.Option(None, "--dataset-revision", help="A1 revision"),
    run_id: str | None = typer.Option(None, "--run-id", help="Idempotency key (default: derived)"),
) -> None:
    """Run a deterministic eval suite over items and persist scores (exit != 0 on error only)."""
    from examlops.evaluation import ExactMatch, JSONValid, Suite, run_suite, sample_by_request_hash

    try:
        eval_items = _load_items(items)
    except (OSError, ValueError) as exc:
        _output.error(f"Failed to load items from {items}: {exc}")
        return
    if sample:
        eval_items = sample_by_request_hash(eval_items, sample)
    rid = run_id or f"{suite}:{model}:{version or 'candidate'}:{len(eval_items)}"

    suite_obj = Suite(suite, [ExactMatch(), JSONValid()])
    result = run_suite(
        suite_obj,
        eval_items,
        model=model,
        run_id=rid,
        model_version=version,
        alias=alias,
        dataset_revision=dataset_revision,
    )
    if _output.json_mode:
        _output.print_json(
            {
                "suite": suite,
                "model": model,
                "scores": result.scores,
                "sample_size": result.sample_size,
            }
        )
    else:
        rows = [[m, f"{s:.4f}"] for m, s in result.scores.items()]
        _output.print_table(
            f"Eval: {suite} · {model} (n={result.sample_size})", ["Metric", "Score"], rows
        )
    _output.ok(f"Eval suite '{suite}' complete ({result.sample_size} items)")


gate_app = typer.Typer(
    help="Eval regression gate (block/warn promotion on regression)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(gate_app, name="gate")


@gate_app.command("set")
def gate_set(
    model: str = typer.Argument(..., help="Model name"),
    suite: str = typer.Option(..., "--suite", help="C2 suite that produces the scores"),
    metric: list[str] = typer.Option(
        ..., "--metric", help="metric[:min=X][:max_drop=Y] (repeatable)"
    ),
    baseline_alias: str = typer.Option("Production", "--baseline", help="Baseline alias"),
    mode: str = typer.Option("block", "--mode", help="block | warn"),
) -> None:
    """Configure the regression gate for a model."""
    from examlops.platform_db import set_eval_gate

    metrics = []
    for spec in metric:
        parts = spec.split(":")
        entry: dict = {"name": parts[0]}
        for kv in parts[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                entry[k] = float(v)
        metrics.append(entry)
    set_eval_gate(
        model, suite, metrics, baseline_alias=baseline_alias, mode=mode, updated_by=_actor()
    )
    _output.ok(f"Gate set for {model}: suite={suite} mode={mode} metrics={metrics}")


@gate_app.command("show")
def gate_show(model: str = typer.Argument(..., help="Model name")) -> None:
    """Show the configured gate for a model."""
    from examlops.platform_db import get_eval_gate

    gate = get_eval_gate(model)
    if gate is None:
        _output.ok(f"No gate configured for {model}.")
        return
    if _output.json_mode:
        _output.print_json(gate)
        return
    _output.print_table(
        f"Eval gate — {model}",
        ["Suite", "Baseline", "Mode", "Metrics"],
        [[gate["suite"], gate["baseline_alias"], gate["mode"], json.dumps(gate["metrics"])]],
    )


@gate_app.command("run")
def gate_run(
    model: str = typer.Argument(..., help="Model name"),
    candidate: str = typer.Argument(..., help="Candidate version to gate"),
    higher_is_better: bool = typer.Option(
        True, "--higher-is-better/--lower-is-better", help="Metric direction"
    ),
) -> None:
    """Run the gate for a candidate version (exit 1 in block mode on failure) — CI-safe (R10)."""
    from examlops.evaluation.gate import run_eval_gate

    result = run_eval_gate(model, candidate, higher_is_better=higher_is_better)
    if result is None:
        _output.ok(f"No gate configured for {model} — nothing to check.")
        return
    if _output.json_mode:
        _output.print_json(result.as_dict())
    else:
        rows = [
            [
                m.name,
                _fmt(m.candidate),
                _fmt(m.baseline),
                _fmt(m.delta),
                "FAIL" if m.failed else "ok",
            ]
            for m in result.metrics
        ]
        _output.print_table(
            f"Gate — {model} v{candidate} ({result.mode})",
            ["Metric", "Candidate", "Baseline", "Delta", "Verdict"],
            rows,
        )
    if not result.passed:
        _output.error(f"Eval gate FAILED for {model} v{candidate} (block mode)")
    _output.ok(f"Eval gate passed for {model} v{candidate}")


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.4f}"

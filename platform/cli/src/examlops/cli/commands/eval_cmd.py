"""C2/C3 — `exa eval run` (continuous eval) + `exa eval gate` (regression gate)
+ `exa eval calibrate` (ADR 0111 judge calibration — the MVVP a judge must pass
before it is allowed to gate anything)."""

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


_EXAMPLES_OPQA = (
    "Examples:\n\n"
    "  exa eval operator-qa                      # ask the agent all 30 and score them\n\n"
    "  exa eval operator-qa --category serving   # only the serving questions\n\n"
    "  exa eval operator-qa --out ./qa.jsonl     # keep the answers for `exa eval run`\n\n"
    "  exa --json eval operator-qa"
)


@app.command("operator-qa", epilog=_EXAMPLES_OPQA)
def operator_qa(
    category: str | None = typer.Option(None, "--category", help="Only questions in this category"),
    out: str | None = typer.Option(
        None, "--out", help="Write the answers as JSONL (feeds `exa eval run`)"
    ),
    agent_url: str | None = typer.Option(
        None, "--agent-url", help="Agent bridge base URL (default: configured agent_url)"
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-question timeout in seconds"),
) -> None:
    """Ask the agent a fixed set of operator questions and report the pass rate.

    Measures whether the agent can answer what a new operator actually asks. Grading is
    deterministic (does the answer name the right command), so no judge model is involved and
    no judge calibration is required. Exits non-zero if the agent is unreachable, so an
    unanswerable run can never be mistaken for a bad score.
    """
    import httpx

    from examlops.cli._config import load_config
    from examlops.evaluation.operator_qa import OPERATOR_QUESTIONS, MentionsAll, by_id, to_items

    questions = [q for q in OPERATOR_QUESTIONS if not category or q.category == category]
    if not questions:
        _output.error(f"no questions in category {category!r}")
        raise typer.Exit(2)

    url = f"{(agent_url or load_config().agent_url).rstrip('/')}/v1/chat/completions"
    answers: dict[str, str] = {}
    failures: list[str] = []
    for q in questions:
        try:
            r = httpx.post(
                url,
                json={
                    "messages": [{"role": "user", "content": q.prompt}],
                    "stream": False,
                },
                timeout=timeout,
            )
            r.raise_for_status()
            answers[q.id] = r.json()["choices"][0]["message"]["content"] or ""
        except Exception as exc:  # unreachable agent, timeout, malformed reply
            failures.append(f"{q.id}: {type(exc).__name__}: {exc}")

    if not answers:
        # Every question failed to even get an answer: that is an outage, not a score of zero.
        _output.error(
            f"the agent answered none of {len(questions)} questions via {url} — "
            f"first error: {failures[0] if failures else 'unknown'}"
        )
        raise typer.Exit(1)

    ev = MentionsAll(questions=by_id())
    scored = [(item, ev.score(item)) for item in to_items(answers)]
    passed = [s for _, s in scored if s.score == 1.0]
    rate = len(passed) / len(scored)

    if out:
        with open(out, "w") as fh:
            for item, score in scored:
                fh.write(
                    json.dumps(
                        {
                            "output": item.output,
                            "prompt": item.prompt,
                            "metadata": {**item.metadata, "score": score.score},
                        }
                    )
                    + "\n"
                )

    _output.print_json(
        {
            "asked": len(questions),
            "answered": len(answers),
            "passed": len(passed),
            "passRate": round(rate, 3),
            "unanswered": failures,
            "failures": [
                {
                    "id": s.detail.get("question_id"),
                    "category": s.detail.get("category"),
                    "score": s.score,
                    "missing": s.detail.get("missing"),
                }
                for _, s in scored
                if s.score < 1.0
            ],
        }
    )
    if not _output.json_mode:
        _output.info(f"Operator QA — {len(passed)}/{len(scored)} passed ({rate:.0%})")


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
    from examlops.data.evaluation import set_eval_gate

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
    from examlops.data.evaluation import get_eval_gate

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


# ── ADR 0111 — judge calibration (MVVP) ───────────────────────────────────────

calibration_app = typer.Typer(
    help="Judge calibration — measure a judge before it may gate (ADR 0111)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(calibration_app, name="calibration")

_EXAMPLES_CAL = (
    "Examples:\n\n"
    "  exa eval calibrate gpt-judge --from ./eval/judge-calibration.json\n\n"
    "  exa eval calibrate gpt-judge --from ./eval/judge-calibration.json --require-eligible\n\n"
    "  exa eval calibration show gpt-judge\n\n"
    "  exa eval calibration list"
)


def _eligibility_rows(cal) -> list[list[str]]:
    from examlops.evaluation.calibration import POSITION_BIAS_MAX, eligibility_failures

    lo, hi = cal.kappa_ci
    failures = eligibility_failures(cal)
    return [
        ["kappa (chance-corrected)", f"{cal.kappa:.3f}  [{lo:.3f}, {hi:.3f}]"],
        ["position bias", f"{cal.position_bias:.3f}  (max {POSITION_BIAS_MAX})"],
        ["test-retest", f"{cal.test_retest:.3f}"],
        ["replications", str(cal.replications)],
        ["benchmark families", ", ".join(cal.families) or "—"],
        ["consistency-bias paradox", "YES" if cal.paradox_flag else "no"],
        ["sensitivity / specificity", f"{cal.sensitivity:.3f} / {cal.specificity:.3f}"],
        ["gate-eligible", "no — " + "; ".join(failures) if failures else "yes"],
    ]


@app.command("calibrate", epilog=_EXAMPLES_CAL)
def calibrate_cmd(
    judge: str = typer.Argument(..., help="Judge model name, as recorded on eval results"),
    from_file: str = typer.Option(
        ..., "--from", help="JSON of collected judgments (see `exa eval calibration list -h`)"
    ),
    version: str = typer.Option("v1", "--version", help="Judge prompt/model version"),
    require_eligible: bool = typer.Option(
        False, "--require-eligible", help="Exit 1 if the judge fails the MVVP — CI-safe"
    ),
) -> None:
    """Measure a judge against labelled benchmarks and record the calibration.

    Recording a *failing* calibration is not an error: the measurement is the point. Pass
    ``--require-eligible`` to make a CI job fail on a judge that may not gate.
    """
    from examlops.data.evaluation import record_judge_calibration
    from examlops.evaluation.calibration import calibrate_from_records, eligibility_failures

    records = json.loads(Path(from_file).read_text())
    cal = calibrate_from_records(records, judge=judge, version=version)
    record_judge_calibration(cal)
    failures = eligibility_failures(cal)

    if _output.json_mode:
        _output.print_json({**cal.as_dict(), "eligible": not failures, "failed_checks": failures})
    else:
        _output.print_table(
            f"Judge calibration — {judge} ({cal.calibration_id})",
            ["Check", "Value"],
            _eligibility_rows(cal),
        )
    if require_eligible and failures:
        _output.error(f"Judge {judge!r} is NOT gate-eligible: {'; '.join(failures)}")
    _output.ok(f"Calibration {cal.calibration_id} recorded for {judge}")


@calibration_app.command("show")
def calibration_show(
    judge: str = typer.Argument(..., help="Judge model name"),
    version: str | None = typer.Option(None, "--version", help="Pin to a judge version"),
) -> None:
    """Show a judge's latest calibration and whether it may gate."""
    from examlops.data.evaluation import get_judge_calibration
    from examlops.evaluation.calibration import calibration_from_row, eligibility_failures

    row = get_judge_calibration(judge, version=version)
    if row is None:
        _output.error(
            f"No calibration for {judge!r} — absence of calibration is not eligibility "
            "(ADR 0111). Run: exa eval calibrate " + judge + " --from <file>"
        )
        return
    cal = calibration_from_row(row)
    if _output.json_mode:
        failures = eligibility_failures(cal)
        _output.print_json({**cal.as_dict(), "eligible": not failures, "failed_checks": failures})
        return
    _output.print_table(
        f"Judge calibration — {judge} ({cal.calibration_id})",
        ["Check", "Value"],
        _eligibility_rows(cal),
    )


@calibration_app.command("list")
def calibration_list(
    limit: int = typer.Option(50, "--limit", help="Rows to show"),
) -> None:
    """List recorded judge calibrations, newest first."""
    from examlops.data.evaluation import list_judge_calibrations
    from examlops.evaluation.calibration import calibration_from_row, eligibility_failures

    rows = list_judge_calibrations(limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No judge calibrations recorded — no judge may gate yet (ADR 0111).")
        return
    table = []
    for r in rows:
        cal = calibration_from_row(r)
        table.append(
            [
                cal.judge,
                cal.version,
                f"{cal.kappa:.3f}",
                f"{cal.position_bias:.3f}",
                f"{cal.test_retest:.3f}",
                str(cal.replications),
                "yes" if not eligibility_failures(cal) else "NO",
                cal.at,
            ]
        )
    _output.print_table(
        "Judge calibrations",
        ["Judge", "Version", "kappa", "Bias", "Retest", "Reps", "Eligible", "At"],
        table,
    )

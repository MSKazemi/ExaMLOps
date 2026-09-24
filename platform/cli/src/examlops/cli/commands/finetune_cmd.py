"""B7 — `exa finetune` + `exa serve adapter`: PEFT/LoRA fine-tuning & multi-LoRA serving.

Fine-tune a base model (LoRA/QLoRA/full), producing a versioned, signed, eval-gated,
lineage-linked adapter; serve many adapters multi-LoRA on a shared base with per-request
routing, an LRU hot set, and base-mismatch refusal. ADR 0044.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

# Registered under `exa serve adapter …`.
adapter_app = typer.Typer(
    help="Multi-LoRA adapters — add/list/promote/route (B7)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  # Really fine-tune: trains LoRA factors and records the score it measured\n"
    "  exa finetune demo-base --train --dataset <rev> --rank 4 --steps 80\n\n"
    "  # Register an adapter trained elsewhere; --asserted-eval is stored as UNVERIFIED\n"
    "  exa finetune llama3.1-8b --method lora --dataset <rev> --asserted-eval 0.82\n\n"
    "  exa serve adapter list --base llama3.1-8b\n\n"
    "  exa serve adapter promote llama3.1-8b-lora-<rev>\n\n"
    "  exa serve adapter route llama3.1-8b llama3.1-8b-lora-<rev> --prompt 'hi'"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def finetune(
    base: str = typer.Argument(..., help="Base model ref (e.g. llama3.1-8b)"),
    train: bool = typer.Option(
        False,
        "--train",
        help="Actually fine-tune: run the reference LoRA script and record the score it measures",
    ),
    method: str = typer.Option("lora", "--method", help="lora | qlora | full"),
    dataset: str = typer.Option(None, "--dataset", help="A1-pinned dataset revision"),
    rank: int = typer.Option(8, "--rank", help="LoRA rank"),
    target_modules: str = typer.Option(None, "--target-modules", help="Comma-separated modules"),
    asserted_eval: float = typer.Option(
        None,
        "--asserted-eval",
        "--eval",
        help="A score YOU measured elsewhere. Stored as UNVERIFIED (operator-asserted); it can "
        "never clear the C3 promotion gate. Use --train to obtain a measured one.",
    ),
    eval_floor: float = typer.Option(0.0, "--eval-floor", help="C3 quality floor for promotion"),
    cost_gpu_hours: float = typer.Option(None, "--cost", help="Fine-tune GPU-hours"),
    adapter_id: str = typer.Option(None, "--adapter-id", help="Explicit adapter id"),
    steps: int = typer.Option(80, "--steps", min=1, help="--train: training steps"),
    batch: int = typer.Option(64, "--batch", min=1, help="--train: batch size"),
    lr: float = typer.Option(0.05, "--lr", help="--train: learning rate"),
    seed: int = typer.Option(None, "--seed", help="--train: seed (default: EXAMLOPS_SEED, else 0)"),
    backend: str = typer.Option(
        "torch-lora", "--backend", help="--train: fine-tuning backend (torch-lora | peft)"
    ),
    run_id: str = typer.Option(None, "--run-id", help="--train: explicit training run id"),
) -> None:
    """Fine-tune (``--train``) or register an adapter, signed and lineage-linked (R1/R3/GWT-1).

    Without ``--train`` nothing is trained: the adapter is registered as a paper record, and any
    ``--asserted-eval`` is stored as an operator claim, clearly separated from a measured score.
    """
    if not dataset:
        _output.error("--dataset (A1 revision) is required for reproducibility.")
        return
    if train:
        _run_training(
            base,
            dataset=dataset,
            adapter_id=adapter_id,
            method=method,
            rank=rank,
            steps=steps,
            batch=batch,
            lr=lr,
            seed=seed,
            backend=backend,
            run_id=run_id,
            eval_floor=eval_floor,
            asserted_eval=asserted_eval,
        )
        return
    from examlops.finetuning import finetune as _finetune

    adapter = _finetune(
        base,
        method,
        dataset,
        adapter_id=adapter_id,
        rank=rank,
        target_modules=[m.strip() for m in target_modules.split(",")] if target_modules else None,
        asserted_eval_score=asserted_eval,
        asserted_eval_by=_actor(),
        eval_floor=eval_floor,
        cost_gpu_hours=cost_gpu_hours,
        actor=_actor(),
    )
    if _output.json_mode:
        _output.print_json(
            {
                "adapter_id": adapter.adapter_id,
                "base_ref": adapter.base_ref,
                "method": adapter.method,
                "rank": adapter.rank,
                "trained": False,
                "eval_score": None,
                "eval_source": None,
                "asserted_eval_score": adapter.asserted_eval_score,
                "asserted_eval_by": adapter.asserted_eval_by,
                "signed": adapter.signed,
            }
        )
        return
    _output.ok(
        f"Registered adapter {adapter.adapter_id} "
        f"({adapter.method}, rank {adapter.rank}) on base {base} — nothing was trained."
    )
    if adapter.asserted_eval_score is not None:
        _output.warning(
            f"  eval {adapter.asserted_eval_score} recorded as UNVERIFIED (operator-asserted by "
            f"{adapter.asserted_eval_by}); it cannot clear the C3 gate. Re-run with --train for a "
            "measured score."
        )
    if not adapter.signed:
        _output.warning("  unsigned — set EXAMLOPS_SIGNING_KEY to sign the adapter.")


def _run_training(
    base: str,
    *,
    dataset: str,
    adapter_id: str | None,
    method: str,
    rank: int,
    steps: int,
    batch: int,
    lr: float,
    seed: int | None,
    backend: str,
    run_id: str | None,
    eval_floor: float,
    asserted_eval: float | None,
) -> None:
    """``--train``: run the shipped reference script, then register what it measured."""
    from examlops.finetuning.runner import TorchNotInstalled, run_finetune

    if method == "full":
        _output.error(
            "Full fine-tuning is not implemented (ADR 0044 clause 4): only LoRA/QLoRA adapters "
            "are trained here. Re-run with --method lora.",
            exit_code=2,
        )
    if asserted_eval is not None:
        _output.warning("--asserted-eval is ignored with --train: the run measures its own score.")
    try:
        res = run_finetune(
            base,
            dataset_rev=dataset,
            adapter_id=adapter_id,
            run_id=run_id,
            method=method,
            rank=rank,
            steps=steps,
            batch=batch,
            lr=lr,
            seed=seed,
            backend=backend,
            eval_floor=eval_floor,
            actor=_actor(),
        )
    except TorchNotInstalled as exc:
        _output.error(str(exc), exit_code=2)
    m = res.metrics or {}
    if _output.json_mode:
        _output.print_json(
            {
                "run_id": res.run_id,
                "status": res.status,
                "trained": res.status == "complete",
                "adapter_id": res.adapter_id,
                "eval_source": "measured" if res.status == "complete" else None,
                "eval_metric": m.get("eval_metric"),
                "eval_score": m.get("eval_score"),
                "eval_n": m.get("eval_n"),
                "baseline_eval_score": m.get("baseline_eval_score"),
                "first_loss": m.get("first_loss"),
                "final_loss": m.get("final_loss"),
                "adapter_sha256": m.get("adapter_sha256"),
                "trainable_parameters": m.get("trainable_parameters"),
                "attempts": [a.outcome for a in res.attempts],
                "run_dir": str(res.run_dir),
            }
        )
        if res.status != "complete":
            raise typer.Exit(1)
        return
    if res.status != "complete":
        _output.error(
            f"{res.run_id} {res.status} after {len(res.attempts)} attempt(s); see {res.log}"
        )
        raise typer.Exit(1)
    _output.ok(
        f"Trained adapter {res.adapter_id}: {m.get('eval_metric')} "
        f"{m.get('eval_score'):.4f} on {m.get('eval_n')} held-out samples "
        f"(baseline {m.get('baseline_eval_score'):.4f}, "
        f"loss {m.get('first_loss'):.4f} → {m.get('final_loss'):.4f}, "
        f"{m.get('trainable_parameters')} adapter parameters, base unchanged)."
    )
    _output.info(f"  run {res.run_id} in {res.run_dir}; adapter {m.get('adapter_sha256', '')[:12]}")


@adapter_app.command("list")
def adapter_list(
    base: str = typer.Option(None, "--base", help="Filter by base model ref"),
) -> None:
    """List registered adapters (R6)."""
    from examlops.data.data_assets import list_adapters

    adapters = list_adapters(base)
    if _output.json_mode:
        _output.print_json(adapters)
        return
    if not adapters:
        _output.info(
            "No adapters. Create one with: exa finetune <base> --method lora --dataset <rev>"
        )
        return
    _output.print_table(
        "LoRA Adapters",
        ["Adapter", "Base", "Method", "Rank", "Measured eval", "Asserted", "Promoted", "Signed"],
        [
            [
                a["adapter_id"],
                a["base_ref"],
                a["method"],
                str(a["rank"]) if a["rank"] is not None else "—",
                _measured_cell(a),
                _asserted_cell(a),
                "yes" if a["promoted"] else "no",
                "yes" if a["signature"] else "no",
            ]
            for a in adapters
        ],
    )


def _measured_cell(a: dict) -> str:
    """Only a row stamped ``measured`` may print a score in the measured column."""
    if a.get("eval_source") == "measured" and a.get("eval_score") is not None:
        return f"{a['eval_score']:.3f} ({a.get('eval_metric') or 'unnamed'})"
    return "—"


def _asserted_cell(a: dict) -> str:
    if a.get("asserted_eval_score") is not None:
        return f"{a['asserted_eval_score']:.3f} (unverified)"
    if a.get("eval_source") is None and a.get("eval_score") is not None:
        # A row from before measured/asserted were separated: provenance unknown, so it is
        # shown as unverified rather than promoted to "measured" by the display.
        return f"{a['eval_score']:.3f} (unverified, legacy)"
    return "—"


@adapter_app.command("add", epilog=_EXAMPLES)
def adapter_add(
    base: str = typer.Argument(..., help="Base model ref"),
    dataset: str = typer.Option(..., "--dataset", help="A1 dataset revision"),
    method: str = typer.Option("lora", "--method", help="lora | qlora | full"),
    rank: int = typer.Option(8, "--rank", help="LoRA rank"),
    asserted_eval: float = typer.Option(
        None,
        "--asserted-eval",
        "--eval",
        help="A score you measured elsewhere — stored as UNVERIFIED (operator-asserted)",
    ),
    eval_floor: float = typer.Option(0.0, "--eval-floor", help="C3 quality floor"),
) -> None:
    """Register an adapter trained elsewhere (alias of `exa finetune` without `--train`) (R6)."""
    from examlops.finetuning import finetune as _finetune

    a = _finetune(
        base,
        method,
        dataset,
        rank=rank,
        asserted_eval_score=asserted_eval,
        asserted_eval_by=_actor(),
        eval_floor=eval_floor,
        actor=_actor(),
    )
    _output.ok(f"Added adapter {a.adapter_id} on base {base} (nothing was trained).")
    if a.asserted_eval_score is not None:
        _output.warning(
            f"  eval {a.asserted_eval_score} is UNVERIFIED (operator-asserted); it cannot clear "
            "the C3 gate."
        )


@adapter_app.command("promote")
def adapter_promote(
    adapter_id: str = typer.Argument(..., help="Adapter id"),
    accept_unverified: bool = typer.Option(
        False,
        "--accept-unverified",
        help="Promote although no measured score exists (audited). The floor is then unproven.",
    ),
) -> None:
    """Promote an adapter — blocked by the C3 eval-gate unless a measured score clears the floor."""
    from examlops.finetuning import EvalGateError, promote_adapter

    try:
        promote_adapter(adapter_id, actor=_actor(), accept_unverified=accept_unverified)
    except EvalGateError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    _output.ok(f"Promoted adapter {adapter_id}.")


@adapter_app.command("route")
def adapter_route(
    base: str = typer.Argument(..., help="Serving base model ref"),
    adapter_id: str = typer.Argument(..., help="Adapter id to route to"),
    prompt: str = typer.Option("", "--prompt", help="Prompt text"),
    hot_set: int = typer.Option(4, "--hot-set", help="Hot-set size (LRU)"),
) -> None:
    """Route a request through a base + adapter — refuses a base mismatch (R4/GWT-4)."""
    from examlops.finetuning import BaseMismatchError, MultiLoRARouter

    router = MultiLoRARouter(base, hot_set_size=hot_set)
    try:
        result = router.route(adapter_id, prompt)
    except (BaseMismatchError, ValueError) as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.info(f"{result['completion']}")
    _output.info(f"  loaded: {', '.join(router.loaded)}")

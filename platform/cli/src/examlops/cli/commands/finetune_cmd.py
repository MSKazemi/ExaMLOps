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
    "  exa finetune llama3.1-8b --method lora --dataset <rev> --rank 8 --eval 0.82\n\n"
    "  exa serve adapter list --base llama3.1-8b\n\n"
    "  exa serve adapter promote llama3.1-8b-lora-<rev>\n\n"
    "  exa serve adapter route llama3.1-8b llama3.1-8b-lora-<rev> --prompt 'hi'"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def finetune(
    base: str = typer.Argument(..., help="Base model ref (e.g. llama3.1-8b)"),
    method: str = typer.Option("lora", "--method", help="lora | qlora | full"),
    dataset: str = typer.Option(None, "--dataset", help="A1-pinned dataset revision"),
    rank: int = typer.Option(8, "--rank", help="LoRA rank"),
    target_modules: str = typer.Option(None, "--target-modules", help="Comma-separated modules"),
    eval_score: float = typer.Option(None, "--eval", help="Recorded eval score"),
    eval_floor: float = typer.Option(0.0, "--eval-floor", help="C3 quality floor for promotion"),
    cost_gpu_hours: float = typer.Option(None, "--cost", help="Fine-tune GPU-hours"),
    adapter_id: str = typer.Option(None, "--adapter-id", help="Explicit adapter id"),
) -> None:
    """Run a fine-tune and register a signed, lineage-linked adapter (R1/R3/GWT-1)."""
    if not dataset:
        _output.error("--dataset (A1 revision) is required for reproducibility.")
        return
    from examlops.finetuning import finetune as _finetune

    adapter = _finetune(
        base,
        method,
        dataset,
        adapter_id=adapter_id,
        rank=rank,
        target_modules=[m.strip() for m in target_modules.split(",")] if target_modules else None,
        eval_score=eval_score,
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
                "eval_score": adapter.eval_score,
                "signed": adapter.signed,
            }
        )
        return
    _output.ok(
        f"Registered adapter [bold]{adapter.adapter_id}[/bold] "
        f"({adapter.method}, rank {adapter.rank}) on base {base}"
    )
    if not adapter.signed:
        _output.warning("  unsigned — set EXAMLOPS_SIGNING_KEY to sign the adapter.")


@adapter_app.command("list")
def adapter_list(
    base: str = typer.Option(None, "--base", help="Filter by base model ref"),
) -> None:
    """List registered adapters (R6)."""
    from examlops.platform_db import list_adapters

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
        ["Adapter", "Base", "Method", "Rank", "Eval", "Promoted", "Signed"],
        [
            [
                a["adapter_id"],
                a["base_ref"],
                a["method"],
                str(a["rank"]) if a["rank"] is not None else "—",
                f"{a['eval_score']:.3f}" if a["eval_score"] is not None else "—",
                "yes" if a["promoted"] else "no",
                "yes" if a["signature"] else "no",
            ]
            for a in adapters
        ],
    )


@adapter_app.command("add", epilog=_EXAMPLES)
def adapter_add(
    base: str = typer.Argument(..., help="Base model ref"),
    dataset: str = typer.Option(..., "--dataset", help="A1 dataset revision"),
    method: str = typer.Option("lora", "--method", help="lora | qlora | full"),
    rank: int = typer.Option(8, "--rank", help="LoRA rank"),
    eval_score: float = typer.Option(None, "--eval", help="Eval score"),
    eval_floor: float = typer.Option(0.0, "--eval-floor", help="C3 quality floor"),
) -> None:
    """Register an adapter (alias of `exa finetune`) (R6)."""
    from examlops.finetuning import finetune as _finetune

    a = _finetune(
        base,
        method,
        dataset,
        rank=rank,
        eval_score=eval_score,
        eval_floor=eval_floor,
        actor=_actor(),
    )
    _output.ok(f"Added adapter {a.adapter_id} on base {base}.")


@adapter_app.command("promote")
def adapter_promote(
    adapter_id: str = typer.Argument(..., help="Adapter id"),
) -> None:
    """Promote an adapter — blocked by the C3 eval-gate if below floor (R2/GWT-2)."""
    from examlops.finetuning import EvalGateError, promote_adapter

    try:
        promote_adapter(adapter_id, actor=_actor())
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

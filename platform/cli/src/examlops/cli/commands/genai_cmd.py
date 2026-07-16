"""``exa genai`` — GenAI observability config + cost inspection (Next-Gen 40 · C1, ADR 0006).

Thin operator surface over ``examlops.telemetry.genai``: check whether GenAI
tracing/content-capture is on, and estimate per-request token cost.
"""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EX_CHECK = "Examples:\n\n  exa genai check\n\n  exa --json genai check"
_EX_COST = (
    "Examples:\n\n"
    "  exa genai cost --model gpt-4o --in 1200 --out 300\n\n"
    "  exa genai cost --model llama3.1:8b --in 5000 --out 5000"
)


@app.command("check", epilog=_EX_CHECK)
def check() -> None:
    """Show GenAI telemetry status: tracing on/off, content capture, semconv version."""
    from examlops.telemetry import genai

    status = {
        "semconv_version": genai.SEMCONV_VERSION,
        "tracing_enabled": genai.tracing_enabled(),
        "content_capture_enabled": genai.content_capture_enabled(),
    }
    if _output.json_mode:
        _output.print_json(status)
        return
    _output.print_table(
        "GenAI telemetry (C1)",
        ["Setting", "Value"],
        [
            ["OTel semconv version", status["semconv_version"]],
            ["Tracing enabled (OTEL_SDK_DISABLED)", "yes" if status["tracing_enabled"] else "no"],
            [
                "Content capture (EXAMLOPS_GENAI_CAPTURE_CONTENT)",
                "yes ⚠ prompts/completions exported" if status["content_capture_enabled"] else "no",
            ],
        ],
    )
    if not status["tracing_enabled"]:
        _output.hint("Enable with OTEL_SDK_DISABLED=false + start the monitoring stack.")


@app.command("cost", epilog=_EX_COST)
def cost(
    model: str = typer.Option(..., "--model", "-m", help="Model name (e.g. gpt-4o)"),
    input_tokens: int = typer.Option(..., "--in", help="Input (prompt) token count"),
    output_tokens: int = typer.Option(..., "--out", help="Output (completion) token count"),
) -> None:
    """Estimate the USD cost of a GenAI call from its token usage (spec R7)."""
    from examlops.telemetry import genai

    usd = genai.estimate_cost(model, input_tokens, output_tokens)
    result = {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": usd,
    }
    if _output.json_mode:
        _output.print_json(result)
        return
    self_hosted = usd == 0.0
    _output.print_table(
        f"GenAI cost — {model}",
        ["Metric", "Value"],
        [
            ["Input tokens", str(input_tokens)],
            ["Output tokens", str(output_tokens)],
            ["Estimated cost (USD)", f"${usd:.6f}" if not self_hosted else "$0 (self-hosted)"],
        ],
    )

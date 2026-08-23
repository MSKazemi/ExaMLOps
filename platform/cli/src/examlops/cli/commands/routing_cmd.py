"""E4 — `exa serve routing`: KV-cache-aware inference routing (ADR 0039).

Configure per-model routing (round-robin default, cache-aware opt-in, optional
prefill/decode disaggregation), simulate a routing decision, and inspect the prefix-cache
hit rate + routing-decision breakdown.
"""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Inference routing — KV/prefix-cache-aware (E4)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa serve routing set JPCP --mode cache_aware --slo-latency-ms 500\n\n"
    "  exa serve routing set JPCP --disaggregate --prefill-pool prefill --decode-pool decode\n\n"
    "  exa serve routing simulate JPCP --replicas 4 --shared-prefix-requests 100\n\n"
    "  exa serve routing stats JPCP"
)


@app.command("set", epilog=_EXAMPLES)
def set_cmd(
    model: str = typer.Argument(..., help="Model name"),
    mode: str = typer.Option("round_robin", "--mode", help="round_robin | cache_aware"),
    slo_latency_ms: float = typer.Option(None, "--slo-latency-ms", help="Avoid replicas over this"),
    disaggregate: bool = typer.Option(False, "--disaggregate", help="Split prefill/decode pools"),
    prefill_pool: str = typer.Option(None, "--prefill-pool", help="Prefill pool name"),
    decode_pool: str = typer.Option(None, "--decode-pool", help="Decode pool name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Configure a model's inference routing (R3 default round-robin; cache-aware opt-in)."""
    from examlops.data.gateway import set_gateway_config

    set_gateway_config(
        model,
        tenant=tenant,
        mode=mode,
        slo_latency_ms=slo_latency_ms,
        disaggregate=disaggregate,
        prefill_pool=prefill_pool,
        decode_pool=decode_pool,
    )
    stz = " + disaggregated" if disaggregate else ""
    _output.ok(f"Routing for {model}: {mode}{stz}")


@app.command("simulate")
def simulate(
    model: str = typer.Argument(..., help="Model name"),
    replicas: int = typer.Option(4, "--replicas", help="Replica count"),
    shared_prefix_requests: int = typer.Option(
        100, "--shared-prefix-requests", help="Requests sharing one prefix"
    ),
    mode: str = typer.Option("cache_aware", "--mode", help="round_robin | cache_aware"),
) -> None:
    """Simulate a shared-prefix request stream and report the cache-aware vs round-robin hit rate."""
    from examlops.inference_gateway import (
        MODE_CACHE_AWARE,
        MODE_ROUND_ROBIN,
        InferenceGateway,
        Replica,
        measure_hit_rate,
        prefix_key,
    )

    key = prefix_key(system_prompt="shared-system", session_id="s1")
    keys = [key] * shared_prefix_requests

    ca = InferenceGateway([Replica(f"r{i}") for i in range(replicas)], mode=MODE_CACHE_AWARE)
    rr = InferenceGateway([Replica(f"r{i}") for i in range(replicas)], mode=MODE_ROUND_ROBIN)
    ca_hit = measure_hit_rate(ca, keys)
    rr_hit = measure_hit_rate(rr, keys)
    result = {"cache_aware_hit_rate": ca_hit, "round_robin_hit_rate": rr_hit, "mode": mode}
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.info(
        f"{model} over {shared_prefix_requests} shared-prefix requests, {replicas} replicas:"
    )
    _output.ok(f"  cache-aware hit rate: {ca_hit:.1%}")
    _output.info(f"  round-robin hit rate: {rr_hit:.1%}")


@app.command("stats")
def stats(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option(None, "--tenant", help="Filter by tenant"),
) -> None:
    """Show recorded prefix-cache hit rate + routing-decision breakdown."""
    from examlops.data.events import routing_stats
    from examlops.data.gateway import get_gateway_config

    cfg = get_gateway_config(model, tenant or "default")
    st = routing_stats(model, tenant)
    if _output.json_mode:
        _output.print_json({"config": cfg, "stats": st})
        return
    if cfg:
        _output.print_record(
            {
                "model": model,
                "mode": cfg["mode"],
                "slo_latency_ms": cfg["slo_latency_ms"]
                if cfg["slo_latency_ms"] is not None
                else "—",
                "disaggregate": bool(cfg["disaggregate"]),
            }
        )
    _output.info(
        f"Routing events: {st['total']} · hit rate {st['hit_rate']:.1%} · {st['by_decision']}"
    )

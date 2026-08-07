"""``exa serve llm`` — lifecycle for vLLM/VLM endpoints (Track V, ADR 0107).

One operator surface over four substrates (external · compose · slurm/flux · kserve). Every
mutating command supports ``--dry-run``, confirms before acting, and writes an audit event,
matching the Phase-29 safety pattern used by ``exa retrain``.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output
from examlops.cli._provenance import audit_details, reason_option

app = typer.Typer(
    help="Serve LLM/VLM endpoints (vLLM) — start, inspect and stop.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_START = (
    "Examples:\n\n"
    "  # Register an endpoint someone else runs (default launcher; works on CPU)\n"
    "  exa serve llm start qwen-vl --base-url http://gpu01:8000 \\\n"
    "      --hf-model Qwen/Qwen3-VL-8B-Instruct --modality vision\n\n"
    "  # Launch on HPC: 2 nodes x 4 GPUs, tensor-parallel in-node, pipeline across\n"
    "  exa serve llm start qwen-vl --launcher slurm --nodes 2 --gpus 4 \\\n"
    "      --hf-model Qwen/Qwen3-VL-8B-Instruct --tp 4\n\n"
    "  # Preview without touching anything\n"
    "  exa serve llm start qwen-vl --launcher compose --dry-run"
)
_EXAMPLES_LIST = (
    "Examples:\n\n  exa serve llm list\n\n  exa serve llm list --project research -o json"
)
_EXAMPLES_STATUS = "Examples:\n\n  exa serve llm status qwen-vl"
_EXAMPLES_STOP = "Examples:\n\n  exa serve llm stop qwen-vl\n\n  exa serve llm stop qwen-vl --yes"
_EXAMPLES_HEALTH = "Examples:\n\n  exa serve llm health qwen-vl"
_EXAMPLES_ARGS = (
    "Examples:\n\n"
    "  # Show the exact `vllm serve` flags this model's engine block renders\n"
    "  exa serve llm args qwen-vl"
)
_EXAMPLES_CHAT = (
    "Examples:\n\n"
    "  exa serve llm chat qwen-vl -m 'Summarise this cluster alert'\n\n"
    "  # Vision: ask about a local plot (needs allowed_local_media_path)\n"
    "  exa serve llm chat qwen-vl -m 'What is in this chart?' --image ./gpu-util.png\n\n"
    "  exa serve llm chat qwen-vl -m 'Compare these' --image a.png --image b.png --stream"
)
_EXAMPLES_BENCH = "Examples:\n\n  exa serve llm bench qwen-vl --requests 5"


def _actor() -> str | None:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER")


def _audit(action: str, target: str, details: dict[str, Any]) -> None:
    from examlops.data.audit import write_audit_event

    try:
        write_audit_event("exa-serve-llm", _actor(), action, target, details)
    except Exception:
        pass


def _engine_config(model: str, overrides: dict[str, Any]) -> Any:
    """Resolve a model's ``engine:`` block from its pack YAML, then apply CLI overrides.

    Falling back to defaults when no YAML exists keeps the command usable for an endpoint
    that has not been added to the registry yet — the common first step.
    """
    from examlops.engines import EngineConfig

    block: dict[str, Any] = {}
    try:
        import yaml

        from examlops.usecase import models_dir

        path = Path(models_dir()) / f"{model.lower()}.yaml"
        if path.exists():
            doc = yaml.safe_load(path.read_text()) or {}
            block = dict(doc.get("engine") or {})
    except Exception:
        block = {}
    block.update({k: v for k, v in overrides.items() if v is not None})
    return EngineConfig.from_dict(block)


def _resolve_stored(model: str) -> dict[str, Any]:
    from examlops.data.serving import get_llm_endpoint

    rec = get_llm_endpoint(model)
    if not rec:
        _output.error(
            f"No endpoint registered for '{model}'. Register one with: "
            f"exa serve llm start {model} --base-url <url>"
        )
        raise typer.Exit(1)
    return rec


def _engine_for(rec: dict[str, Any]) -> Any:
    """Build the engine that talks to a stored endpoint."""
    from examlops.engines import EngineConfig, VLLMServerEngine

    cfg = EngineConfig.from_dict(rec.get("engine_config") or {})
    base_url = rec.get("base_url") or cfg.base_url
    if not base_url:
        _output.error(f"Endpoint '{rec['model']}' has no base_url yet (state={rec.get('state')}).")
        raise typer.Exit(1)
    return VLLMServerEngine(base_url, rec.get("hf_model_id") or rec["model"], cfg)


# ── start ─────────────────────────────────────────────────────────────────────


@app.command("start", epilog=_EXAMPLES_START)
def start(
    model: str = typer.Argument(..., help="Endpoint name (the model clients ask for)"),
    hf_model: str = typer.Option(None, "--hf-model", help="Weights to serve (HF id or local path)"),
    launcher: str = typer.Option(
        None, "--launcher", "-l", help="external | compose | slurm | flux | kserve"
    ),
    base_url: str = typer.Option(None, "--base-url", help="External endpoint URL"),
    modality: str = typer.Option("text", "--modality", help="text | vision | audio | video"),
    max_images: int = typer.Option(
        None, "--max-images", help="limit_mm_per_prompt.image (required for a vision model)"
    ),
    media_domains: str = typer.Option(
        None, "--media-domains", help="Comma-separated allow-list for remote media (SSRF guard)"
    ),
    local_media_path: str = typer.Option(
        None, "--local-media-path", help="Directory from which file:// media may be read"
    ),
    tp: int = typer.Option(None, "--tp", help="tensor_parallel_size (GPUs per node)"),
    pp: int = typer.Option(None, "--pp", help="pipeline_parallel_size (usually = nodes)"),
    dtype: str = typer.Option(None, "--dtype", help="auto | float16 | bfloat16 | fp8 | …"),
    max_model_len: int = typer.Option(None, "--max-model-len", help="Context length"),
    nodes: int = typer.Option(1, "--nodes", help="HPC nodes to allocate"),
    gpus: int = typer.Option(1, "--gpus", help="GPUs per node"),
    partition: str = typer.Option(None, "--partition", help="HPC partition/queue"),
    walltime: str = typer.Option("02:00:00", "--walltime", help="HPC walltime"),
    port: int = typer.Option(8000, "--port", help="Port the server listens on"),
    project: str = typer.Option(None, "--project", help="Attribute to a project workspace"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview; change nothing"),
    reason: str = reason_option(),
) -> None:
    """Start (or register) a vLLM endpoint and record it in the endpoint registry."""
    from examlops.cli._config import active_project
    from examlops.data.serving import upsert_llm_endpoint
    from examlops.engines import to_vllm_args
    from examlops.llm_endpoints import EndpointSpec, LauncherError, select_launcher

    overrides: dict[str, Any] = {
        "base_url": base_url,
        "hf_model_id": hf_model,
        "tensor_parallel_size": tp,
        "pipeline_parallel_size": pp,
        "dtype": dtype,
        "max_model_len": max_model_len,
    }
    mm: dict[str, Any] = {"modality": modality}
    if max_images is not None:
        mm["limit_mm_per_prompt"] = {"image": max_images}
    if media_domains:
        mm["allowed_media_domains"] = [d.strip() for d in media_domains.split(",") if d.strip()]
    if local_media_path:
        mm["allowed_local_media_path"] = local_media_path
    if modality != "text" or max_images is not None or media_domains or local_media_path:
        overrides["multimodal"] = mm

    cfg = _engine_config(model, overrides)
    # A vision model with no per-prompt image limit is an unbounded-input DoS surface, and
    # the server would reject it too — fail here with an actionable message instead.
    if cfg.multimodal.is_multimodal and not cfg.multimodal.limit_mm_per_prompt:
        _output.error(
            f"--modality {modality} requires an item limit: add --max-images N "
            "(an unbounded media count is a DoS surface)."
        )
        raise typer.Exit(1)

    spec = EndpointSpec(
        model=model,
        hf_model_id=hf_model or cfg.hf_model_id or model,
        config=cfg,
        port=port,
        nodes=nodes,
        gpus=gpus,
        project=project or active_project(),
        partition=partition,
        walltime=walltime,
        base_url=base_url,
    )
    chosen = (launcher or os.getenv("EXAMLOPS_LLM_LAUNCHER") or "external").lower()
    rendered = " ".join(to_vllm_args(cfg))

    if dry_run:
        _output.info("Would start (dry-run):")
        _output.print_record(
            {
                "model": model,
                "launcher": chosen,
                "hf_model_id": spec.hf_model_id,
                "modality": cfg.multimodal.modality,
                "nodes": nodes,
                "gpus": gpus,
                "vllm_args": rendered or "(defaults)",
            }
        )
        return

    if not _output.confirm(f"Start endpoint '{model}' via the {chosen} launcher?"):
        _output.warning("Aborted.")
        raise typer.Exit(1)

    try:
        handle = select_launcher(chosen).start(spec)
    except LauncherError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc

    upsert_llm_endpoint(
        model,
        hf_model_id=spec.hf_model_id,
        engine=cfg.engine,
        base_url=handle.base_url,
        state=handle.state,
        launcher=handle.launcher,
        job_id=handle.job_id,
        project=spec.project,
        modality=cfg.multimodal.modality,
        served_model_name=cfg.served_model_name,
        engine_config=_as_block(cfg),
        max_model_len=cfg.max_model_len,
        tensor_parallel_size=cfg.tensor_parallel_size,
        dtype=cfg.dtype,
        gpus=gpus,
        nodes=nodes,
        updated_by=_actor(),
    )
    if spec.project:
        _attach_to_project(spec.project, model)
    _audit(
        "llm_endpoint_started",
        model,
        audit_details(
            {"launcher": handle.launcher, "state": handle.state, "job_id": handle.job_id},
            reason,
        ),
    )
    _output.print_record(
        {
            "model": model,
            "launcher": handle.launcher,
            "state": handle.state,
            "base_url": handle.base_url or "(pending)",
            "job_id": handle.job_id or "-",
        },
    )
    if handle.state != "READY":
        _output.hint(f"Poll readiness with: exa serve llm health {model}")


def _as_block(cfg: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(cfg)


def _attach_to_project(project: str, model: str) -> None:
    """Record the endpoint as a project resource (ADR 0086 — kind already exists)."""
    try:
        from examlops.data.projects import assign_resource_to_project

        assign_resource_to_project(project, "serving_endpoint", model, added_by=_actor())
    except Exception:
        pass


# ── list / status / health ────────────────────────────────────────────────────


@app.command("list", epilog=_EXAMPLES_LIST)
def list_endpoints(
    project: str = typer.Option(None, "--project", help="Filter by project workspace"),
    state: str = typer.Option(None, "--state", help="Filter by lifecycle state"),
) -> None:
    """List registered LLM/VLM endpoints."""
    from examlops.data.serving import list_llm_endpoints

    rows = list_llm_endpoints(project=project, state=state)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No LLM endpoints registered.")
        _output.hint("Register one with: exa serve llm start <model> --base-url <url>")
        return
    _output.print_table(
        "LLM/VLM endpoints",
        ["Model", "State", "Launcher", "Modality", "Base URL", "Project"],
        [
            [
                r["model"],
                r.get("state") or "-",
                r.get("launcher") or "-",
                r.get("modality") or "text",
                r.get("base_url") or "-",
                r.get("project") or "-",
            ]
            for r in rows
        ],
    )


@app.command("status", epilog=_EXAMPLES_STATUS)
def status(model: str = typer.Argument(..., help="Endpoint name")) -> None:
    """Show one endpoint: registry record, substrate status, and live vLLM metrics."""
    from examlops.llm_endpoints import select_launcher

    rec = _resolve_stored(model)
    payload: dict[str, Any] = {
        "model": rec["model"],
        "state": rec.get("state"),
        "launcher": rec.get("launcher"),
        "base_url": rec.get("base_url"),
        "modality": rec.get("modality"),
        "job_id": rec.get("job_id"),
        "project": rec.get("project"),
    }
    try:
        payload["substrate"] = select_launcher(rec.get("launcher")).status(model)
    except Exception as exc:
        payload["substrate"] = {"error": str(exc)}
    if rec.get("base_url"):
        metrics = _engine_for(rec).metrics()
        if metrics:
            payload["metrics"] = _headline_metrics(metrics)

    if _output.json_mode:
        _output.print_json(payload)
        return
    _output.print_record({k: v for k, v in payload.items() if k != "metrics"})
    if payload.get("metrics"):
        _output.print_table(
            "vLLM metrics",
            ["Metric", "Value"],
            [[k, f"{v:g}"] for k, v in payload["metrics"].items()],
        )


def _headline_metrics(metrics: dict[str, float]) -> dict[str, float]:
    """The handful of vLLM series an operator actually reads at a glance.

    KV-cache usage catches OOM risk before it kills the service and queue depth shows
    saturation; the full time series lives in Prometheus, which scrapes the same endpoint.
    """
    keys = (
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
        "vllm:kv_cache_usage_perc",
        "vllm:gpu_cache_usage_perc",
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
    )
    return {k: metrics[k] for k in keys if k in metrics}


@app.command("health", epilog=_EXAMPLES_HEALTH)
def health(model: str = typer.Argument(..., help="Endpoint name")) -> None:
    """Probe the endpoint and update its recorded state. Exits 1 when not ready (CI gate)."""
    from examlops.data.serving import set_llm_endpoint_state

    rec = _resolve_stored(model)
    engine = _engine_for(rec)
    ready = engine.health()
    served = engine.models() if ready else []
    set_llm_endpoint_state(
        model,
        "READY" if ready else "FAILED",
        last_health=("ok" if ready else "unreachable"),
    )
    if _output.json_mode:
        _output.print_json({"model": model, "ready": ready, "served_models": served})
    elif ready:
        _output.ok(
            f"{model} is ready at {rec.get('base_url')} (serving: {', '.join(served) or '?'})"
        )
    else:
        _output.error(f"{model} is not reachable at {rec.get('base_url')}")
    if not ready:
        raise typer.Exit(1)


@app.command("args", epilog=_EXAMPLES_ARGS)
def args_cmd(model: str = typer.Argument(..., help="Endpoint or model name")) -> None:
    """Print the exact ``vllm serve`` argv this model's engine block renders.

    Same renderer the Compose service, the Slurm template and the KServe manifest use, so
    what is printed here is what actually runs on every substrate.
    """
    from examlops.data.serving import get_llm_endpoint
    from examlops.engines import to_vllm_args

    rec = get_llm_endpoint(model)
    cfg = _engine_config(model, rec.get("engine_config") or {} if rec else {})
    rendered = to_vllm_args(cfg)
    hf = (rec or {}).get("hf_model_id") or cfg.hf_model_id or model
    if _output.json_mode:
        _output.print_json({"model": model, "hf_model_id": hf, "args": rendered})
        return
    _output.print_record({"model": hf, "args": " ".join(rendered) or "(defaults)"})
    _output.detail(f"vllm serve {hf} {' '.join(rendered)}")


# ── stop ──────────────────────────────────────────────────────────────────────


@app.command("stop", epilog=_EXAMPLES_STOP)
def stop(
    model: str = typer.Argument(..., help="Endpoint name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview; change nothing"),
    reason: str = reason_option(),
) -> None:
    """Stop an endpoint (and deregister it)."""
    from examlops.data.serving import set_llm_endpoint_state
    from examlops.llm_endpoints import LauncherError, select_launcher

    rec = _resolve_stored(model)
    launcher = rec.get("launcher") or "external"
    if dry_run:
        _output.info("Would stop (dry-run):")
        _output.print_record(
            {"model": model, "launcher": launcher, "job_id": rec.get("job_id") or "-"}
        )
        return
    if not _output.confirm(f"Stop endpoint '{model}' ({launcher})?"):
        _output.warning("Aborted.")
        raise typer.Exit(1)
    try:
        result = select_launcher(launcher).stop(model)
    except LauncherError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    set_llm_endpoint_state(model, "STOPPED")
    _audit("llm_endpoint_stopped", model, audit_details({"launcher": launcher}, reason))
    if _output.json_mode:
        _output.print_json({"model": model, **result})
    else:
        _output.ok(f"Endpoint '{model}' stopped ({launcher}).")


# ── chat / bench ──────────────────────────────────────────────────────────────


@app.command("chat", epilog=_EXAMPLES_CHAT)
def chat(
    model: str = typer.Argument(..., help="Endpoint name"),
    message: str = typer.Option(..., "--message", "-m", help="The prompt"),
    image: list[str] = typer.Option(
        None, "--image", help="Image path or URL (repeatable) — the VLM path"
    ),
    stream: bool = typer.Option(False, "--stream", help="Stream token deltas as they arrive"),
    max_tokens: int = typer.Option(256, "--max-tokens", help="Cap the completion length"),
    temperature: float = typer.Option(None, "--temperature", help="Sampling temperature"),
) -> None:
    """Send a chat request — with images, this is the VLM smoke test."""
    from examlops.engines import MediaRejected

    rec = _resolve_stored(model)
    engine = _engine_for(rec)
    content: list[dict[str, Any]] = [{"type": "text", "text": message}]
    for ref in image or []:
        content.append({"type": "image_url", "image_url": {"url": _image_url(ref)}})
    messages = [{"role": "user", "content": content}]
    sampling: dict[str, Any] = {"max_tokens": max_tokens}
    if temperature is not None:
        sampling["temperature"] = temperature

    try:
        if stream and not _output.json_mode:
            for delta in engine.chat_stream(messages, **sampling):
                print(delta, end="", flush=True)
            print()
            ttft = getattr(engine, "last_ttft_s", 0.0)
            if ttft:
                _output.detail(f"TTFT {ttft * 1000:.0f} ms")
            return
        comp = engine.chat(messages, **sampling)
    except MediaRejected as exc:
        _output.error(f"Media rejected: {exc}")
        raise typer.Exit(1) from exc
    except Exception as exc:
        _output.error(f"Request failed: {exc}")
        raise typer.Exit(1) from exc

    if _output.json_mode:
        _output.print_json(
            {
                "model": model,
                "text": comp.text,
                "prompt_tokens": comp.prompt_tokens,
                "completion_tokens": comp.completion_tokens,
                "images": comp.image_count,
                "latency_s": round(comp.total_s, 3),
            }
        )
    else:
        print(comp.text)
        _output.detail(
            f"{comp.prompt_tokens} in / {comp.completion_tokens} out · "
            f"{comp.image_count} image(s) · {comp.total_s:.2f}s"
        )


def _image_url(ref: str) -> str:
    """Turn a CLI ``--image`` value into a URL vLLM accepts.

    A local file becomes an inline ``data:`` URL rather than ``file://``: the server is
    usually on another host (a GPU node, a container), where the local path would not
    resolve — and inlining keeps the platform from having to expose a filesystem.
    """
    if ref.startswith(("http://", "https://", "data:", "file://")):
        return ref
    path = Path(ref).expanduser()
    if not path.is_file():
        raise typer.BadParameter(f"image not found: {ref}")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


@app.command("bench", epilog=_EXAMPLES_BENCH)
def bench(
    model: str = typer.Argument(..., help="Endpoint name"),
    requests: int = typer.Option(5, "--requests", "-n", help="Sequential requests to send"),
    prompt: str = typer.Option("Explain HPC job scheduling in one sentence.", "--prompt"),
    max_tokens: int = typer.Option(64, "--max-tokens"),
) -> None:
    """Measure TTFT and output tokens/s against a live endpoint."""
    import time

    rec = _resolve_stored(model)
    engine = _engine_for(rec)
    ttfts: list[float] = []
    tokens = 0
    started = time.monotonic()
    for _ in range(requests):
        try:
            chunks = list(
                engine.chat_stream([{"role": "user", "content": prompt}], max_tokens=max_tokens)
            )
        except Exception as exc:
            _output.error(f"Request failed: {exc}")
            raise typer.Exit(1) from exc
        tokens += len(chunks)
        ttft = getattr(engine, "last_ttft_s", 0.0)
        if ttft:
            ttfts.append(ttft)
    elapsed = time.monotonic() - started
    result = {
        "model": model,
        "requests": requests,
        "elapsed_s": round(elapsed, 3),
        "ttft_p50_ms": round(sorted(ttfts)[len(ttfts) // 2] * 1000, 1) if ttfts else None,
        "output_tokens_per_s": round(tokens / elapsed, 1) if elapsed else None,
    }
    if _output.json_mode:
        _output.print_json(result)
    else:
        _output.print_record(result)

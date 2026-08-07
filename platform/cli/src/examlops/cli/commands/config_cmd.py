from __future__ import annotations

import os
import re
from datetime import UTC
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.cli._config import (
    active_context,
    config_path,
    list_contexts,
    load_config,
    resolve_with_provenance,
    set_active_context,
    write_config,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_SHOW = "Examples:\n\n  exa config show"
_EXAMPLES_INIT = "Examples:\n\n  exa config init"
_EXAMPLES_SET = (
    "Examples:\n\n"
    "  exa config set control_plane http://<REMOTE_HOST>:18002\n\n"
    "  exa config set control_plane_token mysecrettoken\n\n"
    "  exa config set mlflow http://<REMOTE_HOST>:15000"
)


@app.command(epilog=_EXAMPLES_SHOW)
def show():
    """Print the current resolved config (env vars + TOML file)."""
    cfg = load_config()
    data = {
        "control_plane_url": cfg.control_plane_url,
        "ray_serve_url": cfg.ray_serve_url,
        "mlflow_url": cfg.mlflow_url,
        "prefect_url": cfg.prefect_url,
        "dashboard_url": cfg.dashboard_url,
        "control_plane_token": "***" if cfg.control_plane_token else "(unset)",
        "config_file": str(config_path()),
    }
    _output.print_record(data)


@app.command(epilog=_EXAMPLES_INIT)
def init():
    """Interactive wizard — write ~/.config/examlops/config.toml."""
    cfg = load_config()
    typer.echo("Press Enter to keep current value shown in [brackets].\n")
    updates = {}
    for key, current in [
        ("control_plane", cfg.control_plane_url),
        ("ray_serve", cfg.ray_serve_url),
        ("mlflow", cfg.mlflow_url),
        ("prefect", cfg.prefect_url),
        ("dashboard", cfg.dashboard_url),
    ]:
        val = typer.prompt(f"  {key} URL", default=current)
        if val != current:
            updates[key] = val
    token = typer.prompt(
        "  control_plane_token", default=cfg.control_plane_token or "", hide_input=True
    )
    if token != cfg.control_plane_token:
        updates["control_plane_token"] = token
    if updates:
        write_config(updates)
        _output.ok(f"Config saved to {config_path()}")
    else:
        typer.echo("No changes.")


@app.command(name="set", epilog=_EXAMPLES_SET)
def set_config(
    key: str = typer.Argument(
        ..., help="Config key (e.g. control_plane, ray_serve, control_plane_token)"
    ),
    value: str = typer.Argument(..., help="New value"),
    context: str = typer.Option(
        "", "--context", "-c", help="Write into a named context instead of the default"
    ),
):
    """Set a single config key in ~/.config/examlops/config.toml."""
    write_config({key: value}, context=context or None)
    where = f" (context: {context})" if context else ""
    _output.ok(f"Set {key} = {value}{where}")


_EXAMPLES_CONTEXTS = "Examples:\n\n  exa config contexts\n\n  exa --json config contexts"
_EXAMPLES_USE = (
    "Examples:\n\n"
    "  [dim]# Point config at a named environment[/dim]\n"
    "  exa config use lxp\n\n"
    "  [dim]# Create + populate a context, then switch to it[/dim]\n"
    "  exa config set control_plane http://<REMOTE_HOST>:18002 --context remote\n"
    "  exa config use lxp"
)


@app.command(epilog=_EXAMPLES_CONTEXTS)
def contexts():
    """List configured contexts (environments) and show the active one."""
    names, active = list_contexts()
    if _output.json_mode:
        _output.print_json({"contexts": names, "active": active})
        return
    if not names:
        _output.info(
            "No named contexts. Create one with: exa config set <key> <val> --context <name>"
        )
        return
    rows = [[("→ " if n == active else "  ") + n, "active" if n == active else ""] for n in names]
    _output.print_table("Contexts", ["Name", ""], rows)


@app.command(epilog=_EXAMPLES_USE)
def use(name: str = typer.Argument(..., help="Context name to activate")):
    """Switch the active context (environment)."""
    set_active_context(name)
    _output.ok(f"Active context is now [bold]{name}[/bold]")
    _output.hint("Verify effective settings with: exa env")


# ── One-file snapshot of ALL platform configuration ────────────────────────────

_SECRET_KEY_RE = re.compile(r"(key|token|secret|password|passwd|credential)", re.IGNORECASE)
_ENV_PREFIXES = (
    "EXAMLOPS_",
    "MLFLOW_",
    "RAY_",
    "AGENT_",
    "DASHBOARD_",
    "CONTROL_PLANE_",
    "AWS_",
    "SEANERBUS_",
    "OTEL_",
    "PLATFORM_DB",
    "PREFECT_",
)


def _redact(key: str, value: str) -> str:
    return "***" if value and _SECRET_KEY_RE.search(key) else value


def _repo_root() -> Path | None:
    """Best-effort repo root: walk up from cwd looking for the pipelines/ dir."""
    for cand in [Path.cwd(), *Path.cwd().parents]:
        if (cand / "pipelines").is_dir() and (cand / "platform").is_dir():
            return cand
    return None


def _yaml_files_section(directory: Path | None) -> dict:
    """Load every ``*.yaml`` in *directory* into ``{stem: parsed}`` (best-effort)."""
    if directory is None or not directory.is_dir():
        return {"available": False}
    import yaml

    out: dict = {"available": True, "dir": str(directory), "files": {}}
    for f in sorted(directory.glob("*.yaml")):
        try:
            out["files"][f.stem] = yaml.safe_load(f.read_text())
        except Exception as exc:  # a broken file should not break the snapshot
            out["files"][f.stem] = {"error": str(exc)}
    return out


def build_export_snapshot() -> dict:
    """Aggregate every configuration surface into one dict (secrets redacted).

    This is a *generated view* — the underlying sources (config.toml, clusters.yaml,
    model YAMLs, env overlays, environment variables) remain the sources of truth.
    """
    from datetime import datetime

    snapshot: dict = {
        "_meta": {
            "generated_by": "exa config export",
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "note": (
                "Generated read-only snapshot. Edit the underlying sources, not this file: "
                "config.toml (exa config), clusters.yaml (exa hpc), models/*.yaml, envs/*.yaml, "
                "environment variables."
            ),
        }
    }

    # 1) CLI config — effective values + provenance (env / context / file / default).
    names, active = list_contexts()
    snapshot["cli"] = {
        "config_file": str(config_path()),
        "active_context": active_context(),
        "contexts": names,
        "settings": {
            r["key"]: {"value": _redact(r["key"], str(r["value"])), "source": r["source"]}
            for r in resolve_with_provenance()
        },
    }

    # 2) HPC fleet — clusters.yaml definitions merged with governance state.
    try:
        from examlops import hpc_registry

        snapshot["hpc"] = {
            "registry_file": str(hpc_registry.registry_path()),
            "clusters": hpc_registry.list_clusters(),
        }
    except Exception as exc:
        snapshot["hpc"] = {"error": str(exc)}

    # 3) Object stores — the artifact/model store vs the (optionally separate) dataset store.
    snapshot["object_stores"] = {
        "artifact_store": {
            "endpoint": os.getenv("MLFLOW_S3_ENDPOINT_URL", "(unset)"),
            "purpose": "MLflow artifacts + models (platform MinIO)",
        },
        "dataset_store": {
            "endpoint": os.getenv("EXAMLOPS_DATA_S3_ENDPOINT")
            or "(unset — falls back to artifact_store)",
            "bucket": os.getenv("EXAMLOPS_DATA_BUCKET", "examlops-data"),
            "credentials_set": bool(os.getenv("EXAMLOPS_DATA_S3_ACCESS_KEY")),
            "purpose": "training datasets (e.g. dedicated dataset object store)",
        },
    }

    # 4) Model registry + environment overlays (repo-local, best-effort).
    root = _repo_root()
    usecase_dir = os.getenv("EXAMLOPS_USECASE_DIR") or (
        str(root / "usecases" / "seanergy") if root else None
    )
    snapshot["models"] = _yaml_files_section(Path(usecase_dir) / "models" if usecase_dir else None)
    snapshot["env_overlays"] = _yaml_files_section(root / "pipelines" / "envs" if root else None)

    # 5) FinOps / provider configuration (if present).
    finops = Path.home() / ".config" / "examlops" / "finops.yaml"
    if finops.is_file():
        import yaml

        try:
            snapshot["finops"] = yaml.safe_load(finops.read_text())
        except Exception as exc:
            snapshot["finops"] = {"error": str(exc)}

    # 6) Every platform-relevant environment variable currently set (secrets redacted).
    snapshot["environment"] = {
        k: _redact(k, v) for k, v in sorted(os.environ.items()) if k.startswith(_ENV_PREFIXES)
    }
    return snapshot


_EXAMPLES_EXPORT = (
    "Examples:\n\n"
    "  [dim]# One YAML with ALL platform config (secrets redacted)[/dim]\n"
    "  exa config export\n\n"
    "  [dim]# Write to a file[/dim]\n"
    "  exa config export --out examlops-config.yaml\n\n"
    "  [dim]# Diff two environments[/dim]\n"
    "  exa config export --out laptop.yaml   # then on lxp: exa config export --out lxp.yaml"
)


@app.command(epilog=_EXAMPLES_EXPORT)
def export(
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the snapshot to a file instead of stdout"
    ),
):
    """One-file YAML snapshot of ALL ExaMLOps configuration (generated, secrets redacted).

    Aggregates the CLI config (with provenance), contexts, the HPC cluster registry,
    object-store split (artifact vs dataset MinIO), per-model YAMLs, environment
    overlays, FinOps providers, and every platform env var — always derived live
    from the real sources, so it can never drift from reality.
    """
    import yaml

    snapshot = build_export_snapshot()
    if _output.json_mode:
        _output.print_json(snapshot)
        return
    text = yaml.safe_dump(snapshot, sort_keys=False, default_flow_style=False, width=100)
    if out:
        out.write_text(text)
        _output.ok(f"Config snapshot written to {out}")
    else:
        typer.echo(text)

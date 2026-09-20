from __future__ import annotations

import os
import re
from datetime import UTC
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.cli._config import (
    _FIELDS,
    InvalidConfigValue,
    UnknownConfigKey,
    active_context,
    canonical_key,
    clear_active_context,
    config_path,
    delete_context,
    list_contexts,
    load_config,
    resolve_with_provenance,
    set_active_context,
    unset_config,
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
    "  [dim]# Secret values are read from a hidden prompt when omitted[/dim]\n"
    "  exa config set control_plane_token\n\n"
    "  exa config set agent_token --context production\n\n"
    "  exa config set mlflow http://<REMOTE_HOST>:15000"
)

_SECRET_CONFIG_KEYS = {"control_plane_token", "dashboard_token", "agent_token"}


@app.command(epilog=_EXAMPLES_SHOW)
def show():
    """Print the current resolved config (env vars + TOML file)."""
    cfg = load_config()
    # Every field the resolver knows, from the one spec — a hand-kept list here once left out
    # `dashboard_token` and `dataplane_bus_bridge_url`, so `show` hid settings `env` reported.
    data: dict[str, str] = {}
    for field, _key, _env, _default, is_secret in _FIELDS:
        value = getattr(cfg, field)
        data[field] = ("***" if value else "(unset)") if is_secret else value
    data["config_file"] = str(config_path())
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
        ("agent", cfg.agent_url),
    ]:
        val = typer.prompt(f"  {key} URL", default=current)
        if val != current:
            updates[key] = val
    token = typer.prompt(
        "  control_plane_token", default=cfg.control_plane_token or "", hide_input=True
    )
    if token != cfg.control_plane_token:
        updates["control_plane_token"] = token
    agent_token = typer.prompt("  agent_token", default=cfg.agent_token or "", hide_input=True)
    if agent_token != cfg.agent_token:
        updates["agent_token"] = agent_token
    if updates:
        write_config(updates)
        _output.ok(f"Config saved to {config_path()}")
    else:
        typer.echo("No changes.")


@app.command(name="set", epilog=_EXAMPLES_SET)
def set_config(
    key: str = typer.Argument(
        ..., help="Config key (e.g. control_plane, agent, control_plane_token, agent_token)"
    ),
    value: str | None = typer.Argument(
        None, help="New value; omit secret values to enter them through a hidden prompt"
    ),
    context: str = typer.Option(
        "", "--context", "-c", help="Write into a named context instead of the default"
    ),
):
    """Set a single config key in ~/.config/examlops/config.toml."""
    try:
        key = canonical_key(key)
    except UnknownConfigKey as exc:
        _output.error(str(exc))
    if value is None:
        if key not in _SECRET_CONFIG_KEYS:
            _output.error(f"A value is required for {key}.")
        value = typer.prompt(f"  {key}", hide_input=True, confirmation_prompt=True)
    try:
        write_config({key: value}, context=context or None)
    except InvalidConfigValue as exc:
        _output.error(str(exc))
    where = f" (context: {context})" if context else ""
    display = "***" if key in _SECRET_CONFIG_KEYS and value else value
    _output.ok(f"Set {key} = {display}{where}")


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
def use(
    name: str | None = typer.Argument(None, help="Context name to activate"),
    clear: bool = typer.Option(
        False, "--clear", help="Leave any context and use the base configuration"
    ),
):
    """Switch the active context (environment), or return to the base config with --clear."""
    if clear:
        if name:
            _output.error("Give a context name or --clear, not both.")
        clear_active_context()
        _output.ok("No active context — using the base configuration")
        return
    if not name:
        _output.error("Name a context to activate, or pass --clear.", hint="exa config contexts")
    set_active_context(name)
    _output.ok(f"Active context is now {name}")
    _output.hint("Verify effective settings with: exa env")


_EXAMPLES_UNSET = (
    "Examples:\n\n"
    "  [dim]# Revert a value to its default[/dim]\n"
    "  exa config unset mlflow\n\n"
    "  [dim]# Drop a context's override; the base value applies again[/dim]\n"
    "  exa config unset control_plane_token --context production"
)


@app.command(epilog=_EXAMPLES_UNSET)
def unset(
    key: str = typer.Argument(..., help="Config key to remove (e.g. mlflow, agent_token)"),
    context: str = typer.Option(
        "", "--context", "-c", help="Remove it from a named context instead of the base config"
    ),
):
    """Remove a config value so the next source applies (context → base → default)."""
    try:
        removed = unset_config(key, context=context or None)
    except UnknownConfigKey as exc:
        _output.error(str(exc))
    where = f" in context {context}" if context else ""
    if removed:
        _output.ok(f"Removed {canonical_key(key)}{where}")
    else:
        _output.info(f"{canonical_key(key)} was not set{where} — nothing to remove")


_EXAMPLES_DELETE_CONTEXT = "Examples:\n\n  exa config delete-context staging"


@app.command("delete-context", epilog=_EXAMPLES_DELETE_CONTEXT)
def delete_context_cmd(
    name: str = typer.Argument(..., help="Context to delete"),
):
    """Delete a named context and all its values (clears it if it was active)."""
    was_active = active_context() == name
    if not _output.confirm(f"Delete context '{name}' and all its values?"):
        raise typer.Exit(1)
    if not delete_context(name):
        _output.error(f"No context named {name!r}.", hint="exa config contexts")
    _output.ok(
        f"Deleted context {name}" + (" — now using the base configuration" if was_active else "")
    )


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
    "DATAPLANE_BUS_",
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
        str(root / "usecases" / "reference") if root else None
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

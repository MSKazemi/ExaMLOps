"""``exa modules`` — which platform modules this centre runs (ADR 0128 site feature profile)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True,
    help="Site feature profile — switch platform modules on/off for this centre",
)

_EX_LIST = (
    "Examples:\n\n"
    "  [dim]# What runs here, and why each module is on or off[/dim]\n"
    "  exa modules list\n\n"
    "  exa --json modules list"
)
_EX_SHOW = "Examples:\n\n  exa modules show hpc\n\n  exa --json modules show agent"
_EX_PRESETS = "Examples:\n\n  exa modules presets"
_EX_ENABLE = (
    "Examples:\n\n"
    "  [dim]# This centre has GPU clusters[/dim]\n"
    "  exa modules enable hpc\n\n"
    "  [dim]# One-off for a single process, without touching the site profile[/dim]\n"
    "  EXAMLOPS_FEATURES=+hpc exa hpc clusters"
)
_EX_DISABLE = (
    "Examples:\n\n"
    "  [dim]# No LLM endpoint at this centre[/dim]\n"
    "  exa modules disable agent\n\n"
    "  exa modules disable genai"
)
_EX_PRESET = (
    "Examples:\n\n"
    "  exa modules preset standard\n\n"
    "  [dim]# Start from a preset and drop earlier enable/disable overrides[/dim]\n"
    "  exa modules preset hpc-center --reset-overrides --site-name jsc-booster"
)
_EX_RESET = "Examples:\n\n  [dim]# Back to 'full' (every module)[/dim]\n  exa modules reset"
_EX_RENDER = (
    "Examples:\n\n"
    "  [dim]# Env line for any deployment[/dim]\n"
    "  exa modules render\n\n"
    "  [dim]# Docker Compose: COMPOSE_PROFILES + an override parking disabled services[/dim]\n"
    "  exa modules render --target compose --out compose.site.yml\n\n"
    "  [dim]# Kubernetes: Helm values for the examlops chart[/dim]\n"
    "  exa modules render --target helm --out site-values.yaml\n"
    "  helm upgrade --install examlops <chart> \\\n"
    "    --set global.imageRegistry=registry.example.org/ -f site-values.yaml"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "exa-modules"


def _audit(action: str, details: dict[str, Any]) -> None:
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        write_audit_event("exa-modules", _actor(), action, "site-profile", details)
    except Exception:  # noqa: BLE001 — auditing is best-effort; the profile file is the record
        pass


def _rows(profile: Any) -> list[dict[str, Any]]:
    from examlops.lifecycle.modules import CATALOG

    return [
        {
            "module": m.id,
            "title": m.title,
            "enabled": profile.is_enabled(m.id),
            "why": profile.reasons[m.id],
            "commands": list(m.cli),
            "services": [*m.compose_services, *(f"profile:{p}" for p in m.compose_profiles)],
            "requires": list(m.requires),
            "needs": m.needs,
        }
        for m in CATALOG
    ]


def _brief(items: list[str], keep: int = 4) -> str:
    if not items:
        return "—"
    more = f" +{len(items) - keep}" if len(items) > keep else ""
    return ", ".join(items[:keep]) + more


@app.command("list", epilog=_EX_LIST)
def list_modules() -> None:
    """Every module, whether it is on at this site, and why."""
    from examlops.lifecycle.modules import resolve

    profile = resolve()
    rows = _rows(profile)
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        f"Modules — preset {profile.preset} ({', '.join(profile.sources)})",
        ["Module", "On", "Why", "Commands", "Services"],
        [
            [
                r["module"],
                "[green]on[/green]" if r["enabled"] else "[dim]off[/dim]",
                r["why"],
                _brief(r["commands"]),
                _brief(r["services"]),
            ]
            for r in rows
        ],
    )
    for w in profile.warnings:
        _output.warning(w)


@app.command("show", epilog=_EX_SHOW)
def show(module: str = typer.Argument(..., help="Module id (see `exa modules list`)")) -> None:
    """Everything a module owns, across the CLI, dashboard, Compose and Helm."""
    from examlops.lifecycle.modules import module as get_module
    from examlops.lifecycle.modules import resolve

    try:
        m = get_module(module)
    except KeyError as exc:
        _output.error(str(exc.args[0]))
    profile = resolve()
    data = {**m.to_dict(), "enabled": profile.is_enabled(m.id), "why": profile.reasons[m.id]}
    if _output.json_mode:
        _output.print_json(data)
        return
    _output.print_record(
        {k: (", ".join(v) if isinstance(v, list | tuple) else v) or "—" for k, v in data.items()}
    )


@app.command("presets", epilog=_EX_PRESETS)
def presets() -> None:
    """The named starting points a site profile can use."""
    from examlops.lifecycle.modules import PRESETS

    rows = [
        {"preset": name, "description": desc, "modules": list(mods)}
        for name, (desc, mods) in PRESETS.items()
    ]
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Site presets",
        ["Preset", "Description", "Modules"],
        [[r["preset"], r["description"], ", ".join(r["modules"])] for r in rows],
    )


def _report_change(before: Any, after: Any, action: str, target: str) -> None:
    flipped = {
        m: after.is_enabled(m) for m in after.enabled if before.is_enabled(m) != after.is_enabled(m)
    }
    _audit(action, {"module": target, "changed": flipped, "spec": after.spec()})
    if _output.json_mode:
        _output.print_json(
            {
                "ok": True,
                "changed": flipped,
                "profile": after.to_dict(),
                "site_file": after.site_file,
            }
        )
        return
    if not flipped:
        _output.info(f"No change — the profile already had that ({after.site_file}).")
    for mid, on in flipped.items():
        _output.ok(f"{mid}: {'on' if on else 'off'} — {after.reasons[mid]}")
    for w in after.warnings:
        _output.warning(w)
    _output.hint(
        "The CLI and dashboard apply this at once; re-render the deployment "
        "(`exa modules render --target compose|helm`) to start/stop services."
    )


def _mutate(action: str, target: str, **kwargs: Any) -> None:
    from examlops.lifecycle import modules

    before = modules.resolve()
    try:
        if action == "module_enabled":
            modules.set_modules(enable=[target])
        elif action == "module_disabled":
            modules.set_modules(disable=[target])
        else:
            modules.set_preset(target, **kwargs)
    except (KeyError, ValueError) as exc:
        _output.error(str(exc.args[0]) if exc.args else str(exc))
    _report_change(before, modules.resolve(), action, target)


@app.command("enable", epilog=_EX_ENABLE)
def enable(module: str = typer.Argument(..., help="Module id to switch on")) -> None:
    """Switch a module on in the site profile (its dependencies come with it)."""
    _mutate("module_enabled", module)


@app.command("disable", epilog=_EX_DISABLE)
def disable(module: str = typer.Argument(..., help="Module id to switch off")) -> None:
    """Switch a module off in the site profile (modules that need it go off too)."""
    _mutate("module_disabled", module)


@app.command("preset", epilog=_EX_PRESET)
def preset(
    name: str = typer.Argument(..., help="Preset name (see `exa modules presets`)"),
    reset_overrides: bool = typer.Option(
        False, "--reset-overrides", help="Drop earlier enable/disable entries"
    ),
    site_name: str = typer.Option("", "--site-name", help="A label for this centre"),
) -> None:
    """Base the site profile on a preset."""
    _mutate(
        "modules_preset",
        name,
        keep_overrides=not reset_overrides,
        site_name=site_name or None,
    )


@app.command("reset", epilog=_EX_RESET)
def reset() -> None:
    """Delete the site profile — every module on again (preset 'full')."""
    from examlops.lifecycle import modules

    if not _output.yes_mode and not _output.confirm("Delete the site profile (all modules on)?"):
        _output.error("Aborted — nothing changed.")
    removed = modules.reset_site_file()
    _audit("modules_reset", {"removed": removed})
    if _output.json_mode:
        _output.print_json({"ok": True, "removed": removed})
    elif removed:
        _output.ok(f"Removed {removed} — preset 'full'.")
    else:
        _output.info("No site profile to remove — already 'full'.")


@app.command("render", epilog=_EX_RENDER)
def render(
    target: str = typer.Option("env", "--target", "-t", help="env | compose | helm"),
    out: str = typer.Option("", "--out", help="Write the Compose override / Helm values here"),
) -> None:
    """Turn the site profile into deployment input: an env line, a Compose override, Helm values."""
    import yaml

    from examlops.lifecycle import modules

    profile = modules.resolve()
    if target == "env":
        data: dict[str, Any] = modules.render_env(profile)
        document = None
    elif target == "compose":
        data = modules.render_compose(profile)
        document = data["override"] or {"services": {}}
    elif target == "helm":
        data = modules.render_helm(profile)
        document = data
    else:
        _output.error(f"unknown target {target!r}", hint="--target env | compose | helm")
    written = None
    if out and document is not None:
        header = (
            f"# Rendered by `exa modules render --target {target}` (ADR 0128) — "
            f"profile {profile.spec()}\n"
        )
        Path(out).write_text(header + yaml.safe_dump(document, sort_keys=False))
        written = out
    if _output.json_mode:
        _output.print_json({"target": target, **data, "written": written})
        return
    if target == "env":
        _output.console.print(f"{modules.FEATURES_ENV}={data[modules.FEATURES_ENV]}", markup=False)
        return
    if target == "compose":
        for key, value in data["env"].items():
            _output.console.print(f"{key}={value}", markup=False)
        for note in data["notes"]:
            _output.warning(note)
    if written:
        _output.ok(f"Wrote {written}")
        if target == "compose":
            _output.hint(
                f"append {Path(written).name} to COMPOSE_FILE in the compose .env, "
                "then `exa stack up`"
            )
        else:
            _output.hint(
                "helm upgrade --install examlops <chart> "
                f"--set global.imageRegistry=<registry>/ -f {written}"
            )
    elif document is not None:
        _output.console.print(yaml.safe_dump(document, sort_keys=False), markup=False)

"""``exa hardware profile`` — named, versioned resource+runtime bundles (ADR 0157, Phase 1).

Registered as a subcommand of the existing ``exa hardware`` app (ADR 0157 decision 2): a
profile is a named, saved preset of the same neutral device vocabulary ``exa hardware place``
already uses, so it lives next to the pools it can eventually be checked against rather than in
a fourth, disconnected HPC/hardware CLI group. Resolving a profile never fabricates a
capability it cannot confirm (ADR 0157 decision 5) — see ``exa hardware profile resolve``.

This is Phase 1 only: the registry + CLI. No surface (`exa workbench create`, `exa pipeline
run`, serving) consumes ``--hardware-profile`` yet — that is Phase 2/3.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.data.audit import write_audit_event
from examlops.data.hardware_profiles import delete_profile
from examlops.data.hardware_profiles import resolve_label as _resolve_label_row
from examlops.hardware import ACCELERATORS
from examlops.hardware_profiles import (
    APPLICABILITIES,
    STATUS_UNRESOLVABLE,
    HardwareProfileError,
    create_profile_version,
    get_profile,
    list_names,
    list_versions,
    resolve_profile,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Named, versioned resource+runtime bundles (ADR 0157)",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa hardware profile set gpu-small --accelerator-family nvidia --gpu 1 --cpu 4 "
    "--memory-gb 16 --applicability training,workbench\n\n"
    "  exa hardware profile list --applicability training\n\n"
    "  exa hardware profile show gpu-small\n\n"
    "  exa hardware profile show gpu-small --version 1 --cluster lxp\n\n"
    "  exa hardware profile resolve gpu-small --cluster lxp --for training\n\n"
    "  exa hardware profile delete gpu-small --version 1"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _profile_to_dict(profile) -> dict:  # noqa: ANN001 - examlops.hardware_profiles.HardwareProfile
    return {
        "name": profile.name,
        "version": profile.version,
        "accelerator_family": profile.accelerator_family,
        "accelerator_model_hint": profile.accelerator_model_hint,
        "gpu_count": profile.gpu_count,
        "gpu_fraction": profile.gpu_fraction,
        "mig_profile": profile.mig_profile,
        "cpu": profile.cpu,
        "memory_gb": profile.memory_gb,
        "nodes": profile.nodes,
        "driver_tag": profile.driver_tag,
        "runtime_tag": profile.runtime_tag,
        "applicability": list(profile.applicability),
        "description": profile.description,
        "created_at": profile.created_at,
        "created_by": profile.created_by,
    }


def _resolution_to_dict(resolution) -> dict:  # noqa: ANN001 - ProfileResolution
    return {
        "name": resolution.name,
        "version": resolution.version,
        "status": resolution.status,
        "reason": resolution.reason,
        "resources": {
            "gpus": resolution.resources.gpus,
            "cpus": resolution.resources.cpus,
            "memory_gb": resolution.resources.memory_gb,
            "nodes": resolution.resources.nodes,
        },
        "unconfirmed": list(resolution.unconfirmed),
    }


def _print_resolution(resolution) -> None:  # noqa: ANN001 - ProfileResolution
    icon = {
        "verified": _output.ok,
        "unchecked": _output.info,
        "degraded": _output.warning,
        "unresolvable": _output.warning,
    }.get(resolution.status, _output.info)
    icon(f"{resolution.name}@{resolution.version}: {resolution.status} — {resolution.reason}")
    r = resolution.resources
    _output.detail(f"  ask: gpus={r.gpus} cpus={r.cpus} memory_gb={r.memory_gb} nodes={r.nodes}")
    if resolution.unconfirmed:
        _output.detail(f"  unconfirmed: {', '.join(resolution.unconfirmed)}")


def _parse_applicability(raw: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in raw.split(",") if a.strip())


@app.command("set", epilog=_EXAMPLES)
def profile_set(
    name: str = typer.Argument(..., help="Profile name"),
    accelerator_family: str = typer.Option(
        ..., "--accelerator-family", help=f"one of {', '.join(ACCELERATORS)}"
    ),
    gpu: int = typer.Option(0, "--gpu", help="GPU count"),
    gpu_fraction: float = typer.Option(1.0, "--gpu-fraction", help="GPU fraction (0<f<=1)"),
    mig_profile: str | None = typer.Option(
        None, "--mig-profile", help="MIG profile, e.g. 1g.5gb (see examlops.gpu_sharing)"
    ),
    cpu: float = typer.Option(0.0, "--cpu", help="CPU cores"),
    memory_gb: float = typer.Option(0.0, "--memory-gb", help="RAM in GB"),
    nodes: int = typer.Option(1, "--nodes", help="Node count"),
    accelerator_model_hint: str | None = typer.Option(
        None, "--accelerator-model-hint", help='advisory, e.g. "A100-80GB"'
    ),
    driver_tag: str | None = typer.Option(None, "--driver-tag", help='e.g. "cuda-12.4"'),
    runtime_tag: str | None = typer.Option(None, "--runtime-tag", help='e.g. "pytorch-2.4-cu124"'),
    applicability: str = typer.Option(
        "any", "--applicability", help=f"comma-separated subset of {', '.join(APPLICABILITIES)}"
    ),
    description: str = typer.Option("", "--description", help="Free text"),
    label: str = typer.Option("active", "--label", help="Label to move to the new version"),
) -> None:
    """Create a new immutable profile version and move ``label`` (default active) to it."""
    try:
        profile = create_profile_version(
            name,
            accelerator_family=accelerator_family,
            gpu_count=gpu,
            gpu_fraction=gpu_fraction,
            mig_profile=mig_profile,
            cpu=cpu,
            memory_gb=memory_gb,
            nodes=nodes,
            accelerator_model_hint=accelerator_model_hint,
            driver_tag=driver_tag,
            runtime_tag=runtime_tag,
            applicability=_parse_applicability(applicability),
            description=description,
            label=label,
            created_by=_actor(),
        )
    except HardwareProfileError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc

    write_audit_event(
        "cli",
        _actor(),
        "hardware_profile_set",
        name,
        {
            "version": profile.version,
            "label": label,
            "accelerator_family": profile.accelerator_family,
            "gpu_count": profile.gpu_count,
            "applicability": list(profile.applicability),
        },
    )
    if _output.json_mode:
        _output.print_json(_profile_to_dict(profile))
        return
    _output.ok(f"Hardware profile '{name}' version {profile.version} created ('{label}' -> it)")


@app.command("list", epilog=_EXAMPLES)
def profile_list(
    applicability: str | None = typer.Option(
        None, "--applicability", help=f"filter to one of {', '.join(APPLICABILITIES)}"
    ),
) -> None:
    """List hardware profiles by their ``active``-labeled version."""
    profiles = []
    for name in list_names():
        profile = get_profile(name)
        if profile is None:
            continue
        if applicability and applicability not in profile.applicability:
            continue
        profiles.append(profile)

    if _output.json_mode:
        _output.print_json([_profile_to_dict(p) for p in profiles])
        return
    if not profiles:
        _output.warning(
            "No hardware profiles. Create one: exa hardware profile set <name> "
            "--accelerator-family nvidia --gpu 1"
        )
        return
    _output.print_table(
        "Hardware profiles",
        [
            "Name",
            "Active v",
            "Accelerator",
            "GPU",
            "Frac",
            "CPU",
            "Mem(GB)",
            "Nodes",
            "Applicability",
        ],
        [
            [
                p.name,
                str(p.version),
                p.accelerator_family,
                str(p.gpu_count),
                f"{p.gpu_fraction:.2f}",
                str(p.cpu),
                str(p.memory_gb),
                str(p.nodes),
                ",".join(p.applicability),
            ]
            for p in profiles
        ],
    )


@app.command("show", epilog=_EXAMPLES)
def profile_show(
    name: str = typer.Argument(..., help="Profile name"),
    version: int | None = typer.Option(None, "--version", help="A specific version"),
    label: str = typer.Option(
        "active", "--label", help="Label to resolve when --version is omitted"
    ),
    cluster: str | None = typer.Option(
        None, "--cluster", help="Also resolve() against this cluster's live capacity"
    ),
) -> None:
    """Show one hardware profile version (default: the ``active`` label's target)."""
    profile = get_profile(name, label=label, version=version)
    if profile is None:
        ref = f"version {version}" if version is not None else f"label {label!r}"
        _output.error(f"Hardware profile '{name}' ({ref}) not found")
        raise typer.Exit(1)

    payload = _profile_to_dict(profile)
    resolution = None
    if cluster:
        resolution = resolve_profile(
            name, label=label, version=profile.version, target_cluster=cluster
        )
        payload["resolution"] = _resolution_to_dict(resolution)

    if _output.json_mode:
        _output.print_json(payload)
        return
    _output.print_record(
        {
            "Name": profile.name,
            "Version": profile.version,
            "Accelerator": profile.accelerator_family,
            "Model hint": profile.accelerator_model_hint or "—",
            "GPU": f"{profile.gpu_count} (fraction {profile.gpu_fraction:.2f})",
            "MIG profile": profile.mig_profile or "—",
            "CPU": profile.cpu,
            "Memory (GB)": profile.memory_gb,
            "Nodes": profile.nodes,
            "Driver tag": profile.driver_tag or "—",
            "Runtime tag": profile.runtime_tag or "—",
            "Applicability": ",".join(profile.applicability),
            "Description": profile.description or "—",
            "Created": f"{profile.created_at or '—'} by {profile.created_by or '—'}",
        }
    )
    if resolution:
        _print_resolution(resolution)


@app.command("resolve", epilog=_EXAMPLES)
def profile_resolve(
    name: str = typer.Argument(..., help="Profile name"),
    version: int | None = typer.Option(None, "--version", help="A specific version"),
    label: str = typer.Option(
        "active", "--label", help="Label to resolve when --version is omitted"
    ),
    cluster: str = typer.Option(..., "--cluster", help="Target cluster to resolve against"),
    for_: str | None = typer.Option(
        None, "--for", help="workbench|training|serving — cross-checked against applicability"
    ),
) -> None:
    """Resolve a profile against a target cluster's live capacity (never fabricates a claim)."""
    try:
        resolution = resolve_profile(name, label=label, version=version, target_cluster=cluster)
    except HardwareProfileError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc

    if for_ is not None:
        profile = get_profile(name, label=label, version=resolution.version)
        if profile and for_ not in profile.applicability and "any" not in profile.applicability:
            _output.warning(
                f"profile '{name}' applicability {list(profile.applicability)} does not "
                f"include {for_!r}"
            )

    if _output.json_mode:
        _output.print_json(_resolution_to_dict(resolution))
        if resolution.status == STATUS_UNRESOLVABLE:
            raise typer.Exit(1)
        return
    _print_resolution(resolution)
    if resolution.status == STATUS_UNRESOLVABLE:
        raise typer.Exit(1)


@app.command("delete", epilog=_EXAMPLES)
def profile_delete(
    name: str = typer.Argument(..., help="Profile name"),
    version: int | None = typer.Option(
        None, "--version", help="Delete only this version (omit: the whole name — every version)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a hardware profile version, or the whole name (every version + every label)."""
    existing = list_versions(name)
    if not existing:
        _output.error(f"Hardware profile '{name}' not found")
        raise typer.Exit(1)
    if version is not None and not any(v.version == version for v in existing):
        _output.error(f"Hardware profile '{name}' has no version {version}")
        raise typer.Exit(1)

    what = f"version {version} of '{name}'" if version is not None else f"ALL versions of '{name}'"
    if not _output.confirm(f"Delete {what}?", auto_yes=yes):
        _output.info("Cancelled.")
        return

    # GWT-4: detect (before deleting) whether the 'active' label points at the version being
    # removed, so a single-version delete never silently re-points a dangling label — it warns.
    dangling_active = False
    if version is not None:
        active_row = _resolve_label_row(name, "active")
        dangling_active = active_row is not None and int(active_row["version"]) == version

    removed = delete_profile(name, version)
    write_audit_event(
        "cli",
        _actor(),
        "hardware_profile_deleted",
        name,
        {"version": version, "rows_removed": removed},
    )

    if version is None:
        _output.ok(f"Deleted hardware profile '{name}' (all versions, {removed} row(s))")
    else:
        _output.ok(f"Deleted hardware profile '{name}' version {version} ({removed} row(s))")
        if dangling_active:
            _output.warning(
                f"label 'active' pointed at version {version}, which no longer exists — it is "
                f"now dangling (never silently re-pointed); move it with "
                f"'exa hardware profile set {name} ...' or point it at a surviving version"
            )

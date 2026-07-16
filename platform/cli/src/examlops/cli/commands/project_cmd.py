"""``exa project`` — ExaMLOps Projects (modeled on Red Hat OpenShift AI Data Science Projects).

A **Project** is a named resource envelope that groups ML models and services and enforces
CPU/memory/storage/GPU limits on their Docker containers.  The concept is ported directly from
RHOAI (Red Hat OpenShift AI):

* RHOAI ``Namespace`` + ``ResourceQuota`` → ExaMLOps ``Project`` in ``platform.db``
* RHOAI ``LimitRange`` → Docker Compose ``deploy.resources.limits`` (per-service)
* RHOAI ``NetworkPolicy`` → Docker ``networks.<project>-network``
* RHOAI ``PersistentVolumeClaim`` → Docker named ``volumes`` with size annotations

Key commands::

    exa project create my-project --cpu-limit 4 --memory-limit 8g --storage-gb 100
    exa project list
    exa project show my-project
    exa project assign-model my-project JPCP
    exa project set-quota my-project --cpu-limit 8 --memory-limit 16g
    exa project compose my-project          # Docker Compose fragment with resource limits
    exa project archive my-project
    exa project delete my-project
"""

from __future__ import annotations

import os

import typer
import yaml

from examlops.cli import _output
from examlops.platform_db import (
    archive_project,
    assign_model_to_project,
    create_project,
    delete_project,
    get_project,
    init_db,
    list_project_models,
    list_projects,
    update_project_quota,
    write_audit_event,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="ExaMLOps Projects — resource-quota envelopes (CPU/memory/storage/GPU) for Docker.",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_CREATE = (
    "Examples:\n\n"
    "  exa project create research --cpu-limit 4 --memory-gb 8 --storage-gb 100\n\n"
    "  exa project create production --cpu-limit 16 --memory-gb 64 --storage-gb 500 --gpu-limit 2"
)
_EXAMPLES_COMPOSE = (
    "Examples:\n\n"
    "  exa project compose research\n\n"
    "  exa project compose research --out docker-compose.project.yml"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _memory_str_to_gb(mem_str: str) -> float:
    """Parse '8g', '8G', '8192m', '8192M' → float GB."""
    s = mem_str.strip().lower()
    if s.endswith("g"):
        return float(s[:-1])
    if s.endswith("gb"):
        return float(s[:-2])
    if s.endswith("m"):
        return float(s[:-1]) / 1024
    if s.endswith("mb"):
        return float(s[:-2]) / 1024
    return float(s)  # assume GB if no suffix


def _gb_to_bytes(gb: float) -> int:
    return int(gb * 1024 * 1024 * 1024)


@app.command(epilog=_EXAMPLES_CREATE)
def create(
    name: str = typer.Argument(..., help="Project name (unique identifier, e.g. 'research')"),
    description: str | None = typer.Option(None, "--description", "-d", help="Project description"),
    cpu_limit: float = typer.Option(4.0, "--cpu-limit", help="Total CPU cores for this project"),
    memory_gb: float = typer.Option(8.0, "--memory-gb", help="Total RAM in GB for this project"),
    storage_gb: float = typer.Option(
        50.0, "--storage-gb", help="Total storage in GB for this project"
    ),
    gpu_limit: int = typer.Option(
        0, "--gpu-limit", help="Total GPU count for this project (0 = no GPUs)"
    ),
) -> None:
    """Create a new project with resource quotas (CPU/memory/storage/GPU)."""
    init_db()
    existing = get_project(name)
    if existing:
        _output.error(f"Project '{name}' already exists")
        raise typer.Exit(1)
    create_project(
        name,
        description=description,
        cpu_limit=cpu_limit,
        memory_limit_gb=memory_gb,
        storage_gb=storage_gb,
        gpu_limit=gpu_limit,
        created_by=_actor(),
    )
    write_audit_event(
        "cli",
        _actor(),
        "project_created",
        name,
        {
            "cpu_limit": cpu_limit,
            "memory_gb": memory_gb,
            "storage_gb": storage_gb,
            "gpu_limit": gpu_limit,
        },
    )
    _output.ok(
        f"Project '{name}' created — "
        f"CPU {cpu_limit:.1f} cores · RAM {memory_gb:.1f} GB · Storage {storage_gb:.0f} GB"
        + (f" · {gpu_limit} GPU(s)" if gpu_limit else "")
    )


@app.command("list")
def project_list(
    status: str | None = typer.Option(
        None, "--status", "-s", help="Filter by status (ACTIVE|ARCHIVED)"
    ),
) -> None:
    """List all projects with their resource quotas."""
    init_db()
    rows = list_projects(status=status)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.warning("No projects found. Create one with: exa project create <name>")
        return
    _output.print_table(
        "Projects",
        ["Name", "Status", "CPU", "RAM (GB)", "Storage (GB)", "GPU", "Description"],
        [
            [
                r["name"],
                r["status"],
                f"{r['cpu_limit']:.1f}",
                f"{r['memory_limit_gb']:.1f}",
                f"{r['storage_gb']:.0f}",
                str(r["gpu_limit"]),
                r["description"] or "",
            ]
            for r in rows
        ],
    )


@app.command()
def show(
    name: str = typer.Argument(..., help="Project name"),
) -> None:
    """Show project details, resource quotas, and assigned models."""
    init_db()
    project = get_project(name)
    if not project:
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    models = list_project_models(name)
    if _output.json_mode:
        _output.print_json({**project, "models": models})
        return
    _output.print_table(
        f"Project: {name}",
        ["Field", "Value"],
        [
            ["Status", project["status"]],
            ["Description", project["description"] or ""],
            ["CPU limit", f"{project['cpu_limit']:.1f} cores"],
            ["Memory limit", f"{project['memory_limit_gb']:.1f} GB"],
            ["Storage limit", f"{project['storage_gb']:.0f} GB"],
            ["GPU limit", str(project["gpu_limit"])],
            ["Docker network", project["network_name"] or f"examlops-{name}"],
            ["Created at", project["created_at"]],
            ["Created by", project["created_by"] or ""],
            ["Models assigned", str(len(models))],
        ],
    )
    if models:
        _output.print_table("Assigned models", ["Model"], [[m] for m in models])


@app.command("set-quota")
def set_quota(
    name: str = typer.Argument(..., help="Project name"),
    cpu_limit: float | None = typer.Option(None, "--cpu-limit", help="New CPU limit (cores)"),
    memory_gb: float | None = typer.Option(None, "--memory-gb", help="New RAM limit (GB)"),
    storage_gb: float | None = typer.Option(None, "--storage-gb", help="New storage limit (GB)"),
    gpu_limit: int | None = typer.Option(None, "--gpu-limit", help="New GPU limit (count)"),
    description: str | None = typer.Option(None, "--description", help="New description"),
) -> None:
    """Update resource quotas for an existing project."""
    init_db()
    if not any([cpu_limit, memory_gb, storage_gb, gpu_limit is not None, description]):
        _output.error("Specify at least one quota field to update")
        raise typer.Exit(1)
    updated = update_project_quota(
        name,
        cpu_limit=cpu_limit,
        memory_limit_gb=memory_gb,
        storage_gb=storage_gb,
        gpu_limit=gpu_limit,
        description=description,
    )
    if not updated:
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    write_audit_event(
        "cli",
        _actor(),
        "project_quota_updated",
        name,
        {
            "cpu_limit": cpu_limit,
            "memory_gb": memory_gb,
            "storage_gb": storage_gb,
            "gpu_limit": gpu_limit,
        },
    )
    _output.ok(f"Project '{name}' quota updated")


@app.command("assign-model")
def assign_model(
    project: str = typer.Argument(..., help="Project name"),
    model: str = typer.Argument(..., help="Model name to assign (e.g. JPCP)"),
) -> None:
    """Assign a model to a project."""
    init_db()
    ok = assign_model_to_project(project, model)
    if not ok:
        _output.error(f"Project '{project}' not found")
        raise typer.Exit(1)
    write_audit_event("cli", _actor(), "project_model_assigned", model, {"project": project})
    _output.ok(f"Model {model} assigned to project '{project}'")


@app.command(epilog=_EXAMPLES_COMPOSE)
def compose(
    name: str = typer.Argument(..., help="Project name"),
    out: str | None = typer.Option(None, "--out", "-o", help="Write to file instead of stdout"),
) -> None:
    """Generate a Docker Compose fragment with resource limits for this project.

    The output enforces the project's CPU/memory quota across all its containers.
    Merge it with your main docker-compose.yml or pass it to docker compose -f.

    Resource limits follow Docker Compose v3 ``deploy.resources`` semantics:
    - ``cpus``: fractional CPU cores (e.g. 2.0 = 2 cores)
    - ``memory``: total RAM (e.g. 8589934592 bytes = 8 GB)
    """
    init_db()
    project = get_project(name)
    if not project:
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)

    models = list_project_models(name)
    network_name = project["network_name"] or f"examlops-{name}"
    cpu_per_service = round(project["cpu_limit"] / max(len(models), 1), 2)
    mem_bytes_per_service = _gb_to_bytes(project["memory_limit_gb"] / max(len(models), 1))
    storage_gb = project["storage_gb"]

    services: dict = {}
    volumes: dict = {}

    # Generate a service entry for each assigned model
    for model in models:
        svc_name = model.lower()
        vol_name = f"{svc_name}-data"
        volumes[vol_name] = {"driver": "local"}
        services[svc_name] = {
            "image": f"examlops/{svc_name}:latest",
            "networks": [network_name],
            "volumes": [f"{vol_name}:/data"],
            "deploy": {
                "resources": {
                    "limits": {
                        "cpus": str(cpu_per_service),
                        "memory": str(mem_bytes_per_service),
                    },
                    "reservations": {
                        "cpus": str(round(cpu_per_service / 2, 2)),
                        "memory": str(mem_bytes_per_service // 2),
                    },
                }
            },
            "environment": {
                "EXAMLOPS_PROJECT": name,
                "EXAMLOPS_MODEL": model,
            },
        }
        # Attach GPU if project has a GPU quota
        if project["gpu_limit"] > 0:
            services[svc_name]["deploy"]["resources"]["reservations"]["devices"] = [
                {"driver": "nvidia", "count": project["gpu_limit"], "capabilities": ["gpu"]}
            ]

    # Platform infrastructure service (shared per project)
    services["platform-db"] = {
        "image": "examlops/platform:latest",
        "networks": [network_name],
        "volumes": [f"{name}-platform:/platform"],
        "deploy": {
            "resources": {
                "limits": {"cpus": "0.5", "memory": str(_gb_to_bytes(0.5))},
            }
        },
        "environment": {
            "EXAMLOPS_PROJECT": name,
            "PLATFORM_DB": "/platform/platform.db",
        },
    }
    volumes[f"{name}-platform"] = {"driver": "local"}

    doc = {
        "name": f"examlops-{name}",
        "x-project-metadata": {
            "project": name,
            "cpu_limit_cores": project["cpu_limit"],
            "memory_limit_gb": project["memory_limit_gb"],
            "storage_gb": storage_gb,
            "gpu_limit": project["gpu_limit"],
            "generated_by": "exa project compose",
        },
        "networks": {
            network_name: {
                "name": network_name,
                "driver": "bridge",
                "labels": {"examlops.project": name},
            }
        },
        "volumes": volumes,
        "services": services,
    }

    compose_yaml = yaml.dump(doc, default_flow_style=False, sort_keys=False)

    if out:
        with open(out, "w") as f:
            f.write(compose_yaml)
        _output.ok(f"Docker Compose file written to: {out}")
    else:
        # Print raw YAML to stdout (not through _output.print_json — it's not JSON)
        import sys

        sys.stdout.write(compose_yaml)


@app.command()
def archive(
    name: str = typer.Argument(..., help="Project name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Archive a project (marks ARCHIVED; data is preserved)."""
    init_db()
    if not yes and not _output.confirm(f"Archive project '{name}'?"):
        _output.info("Cancelled.")
        return
    ok = archive_project(name)
    if not ok:
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    write_audit_event("cli", _actor(), "project_archived", name, {})
    _output.ok(f"Project '{name}' archived")


@app.command()
def delete(
    name: str = typer.Argument(..., help="Project name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a project and remove all its model assignments (irreversible)."""
    init_db()
    if not yes and not _output.confirm(
        f"[bold red]Delete[/bold red] project '{name}' and all its assignments? This is irreversible."
    ):
        _output.info("Cancelled.")
        return
    ok = delete_project(name)
    if not ok:
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    write_audit_event("cli", _actor(), "project_deleted", name, {})
    _output.ok(f"Project '{name}' deleted")


# --- D6 RBAC (ADR 0014): relationship grants over projects/objects -----------

_EX_GRANT = (
    "Examples:\n\n"
    "  exa project grant alice owner project:acme\n\n"
    "  exa project grant bob editor project:acme/model:JPCP"
)


@app.command("grant", epilog=_EX_GRANT)
def grant(
    subject: str = typer.Argument(..., help="User/subject id"),
    relation: str = typer.Argument(..., help="owner | editor | viewer"),
    obj: str = typer.Argument(
        ..., metavar="OBJECT", help="Object id (e.g. project:acme/model:JPCP)"
    ),
) -> None:
    """Grant a subject a relation on an object (RBAC, audited, spec D6)."""
    from examlops.authz import grant as authz_grant

    if relation not in {"owner", "editor", "viewer"}:
        _output.error("relation must be one of: owner, editor, viewer")
    authz_grant(subject, relation, obj, actor=_actor())
    _output.ok(f"Granted [bold]{subject}[/bold] '{relation}' on {obj}")


@app.command(
    "revoke", epilog="Examples:\n\n  exa project revoke bob editor project:acme/model:JPCP"
)
def revoke(
    subject: str = typer.Argument(..., help="User/subject id"),
    relation: str = typer.Argument(..., help="owner | editor | viewer"),
    obj: str = typer.Argument(..., metavar="OBJECT", help="Object id"),
) -> None:
    """Revoke a subject's relation on an object (audited)."""
    from examlops.authz import revoke as authz_revoke

    n = authz_revoke(subject, relation, obj, actor=_actor())
    if n:
        _output.ok(f"Revoked '{relation}' from {subject} on {obj}")
    else:
        _output.info("No such relation.")


@app.command(
    "access",
    epilog="Examples:\n\n  exa project access --object project:acme\n\n  exa project access --subject alice",
)
def access(
    subject: str | None = typer.Option(None, "--subject", help="Show all grants for a subject"),
    obj: str | None = typer.Option(None, "--object", help="Show all grants on an object"),
) -> None:
    """List RBAC relations (by subject and/or object)."""
    from examlops.platform_db import list_relations

    rows = list_relations(subject=subject, obj=obj)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No relations.")
        return
    _output.print_table(
        "RBAC relations",
        ["Subject", "Relation", "Object", "Granted by", "When"],
        [
            [
                r["subject"],
                r["relation"],
                r["object"],
                r["actor"] or "-",
                (r["created_at"] or "")[:19],
            ]
            for r in rows
        ],
    )

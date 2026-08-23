"""``exa project`` — ExaMLOps Projects (the unified project workspace).

A **Project** is a named resource envelope that groups ML models and services and enforces
CPU/memory/storage/GPU limits on their Docker containers.  It maps the familiar
namespace/quota/isolation primitives onto ExaMLOps's Docker + SQLite substrate:

* Namespace + resource quota → ExaMLOps ``Project`` in ``platform.db``
* Per-service limit range → Docker Compose ``deploy.resources.limits`` (per-service)
* Network isolation policy → Docker ``networks.<project>-network``
* Persistent storage claim → Docker named ``volumes`` with size annotations

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
from examlops.cli._help import make_ordered_group
from examlops.data import init_db
from examlops.data.audit import write_audit_event
from examlops.data.projects import (
    add_project_member,
    archive_project,
    assign_resource_to_project,
    bind_project_connection,
    create_project,
    delete_project,
    ensure_project_storage,
    get_project,
    get_project_full,
    get_project_pipelines,
    get_project_storage,
    list_project_members,
    list_project_models,
    list_projects,
    refresh_project_usage,
    remove_project_member,
    update_project_quota,
)

# Help panels for `exa project` (all subcommands registered within this module).
_PANELS: list[tuple[str, list[str]]] = [
    ("Lifecycle", ["create", "list", "show", "use", "current", "archive", "delete"]),
    ("Resources", ["assign", "assign-model", "storage", "pipelines", "compose"]),
    ("Members & Access", ["members", "add-member", "remove-member", "grant", "revoke", "access"]),
    ("Quota & Cost", ["set-quota", "cost", "budget"]),
]

app = typer.Typer(
    cls=make_ordered_group(_PANELS),
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
    """Show the full project anatomy: quota, resources by kind, members, budget, consumption."""
    init_db()
    full = get_project_full(name)
    if not full:
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    if _output.json_mode:
        _output.print_json(full)
        return

    resources: dict = full["resources"]
    members: list = full["members"]
    consumption: dict = full["consumption"]
    budget = full.get("budget")
    _output.print_table(
        f"Project: {name}",
        ["Field", "Value"],
        [
            ["Status", full["status"]],
            ["Description", full["description"] or ""],
            ["CPU limit", f"{full['cpu_limit']:.1f} cores"],
            ["Memory limit", f"{full['memory_limit_gb']:.1f} GB"],
            ["Storage limit", f"{full['storage_gb']:.0f} GB"],
            ["GPU limit", str(full["gpu_limit"])],
            ["Docker network", full["network_name"] or f"examlops-{name}"],
            ["Created at", full["created_at"]],
            ["Created by", full["created_by"] or ""],
            ["Members", str(len(members))],
            [
                "Consumption",
                f"{consumption['gpu_hours']:.1f} GPU-h · ${consumption['cost_usd']:.2f}",
            ],
            [
                "Budget",
                (
                    # platform_db stores gpu_hours_budget / cost_budget; tolerate the older
                    # gpu_hours / cost_usd names too so a set budget always renders.
                    f"{budget.get('gpu_hours_budget', budget.get('gpu_hours', 0))} GPU-h"
                    f" · ${budget.get('cost_budget', budget.get('cost_usd', 0))}"
                    if budget
                    else "(none)"
                ),
            ],
        ],
    )
    if resources:
        _output.print_table(
            "Resources",
            ["Kind", "Refs"],
            [[kind, ", ".join(refs)] for kind, refs in sorted(resources.items())],
        )
    if members:
        _output.print_table(
            "Members",
            ["Subject", "Role", "Granted by", "When"],
            [
                [m["subject"], m["role"], m.get("granted_by") or "-", (m.get("when") or "")[:19]]
                for m in members
            ],
        )

    # ── P8 anatomy: storage · connections · pipelines ─────────────────────────
    storage = full.get("storage")
    if storage:
        used_gb = (storage.get("used_bytes") or 0) / 1e9
        quota = storage.get("quota_gb")
        _output.print_table(
            "Storage",
            ["Field", "Value"],
            [
                ["Location", f"s3://{storage['bucket']}/{storage['prefix']}"],
                ["Used", f"{used_gb:.2f} GB" + (f" / {quota:.0f} GB" if quota else "")],
                ["Connection", storage.get("connection_ref") or "—"],
            ],
        )
    connections = full.get("connections") or []
    if connections:
        _output.print_table(
            "Connections",
            ["Name", "Kind", "Secret"],
            [
                [c["name"], c.get("kind") or "—", "✓" if c.get("has_secret") else "✗"]
                for c in connections
            ],
        )
    pipelines = full.get("pipelines") or {}
    pf, ry = pipelines.get("prefect"), pipelines.get("rayserve")
    if pf or ry:
        rows = []
        if pf:
            rows.append(
                [
                    "Prefect (training)",
                    ", ".join(pf.get("deployments") or []) or "—",
                    pf.get("schedule") or "—",
                    pf.get("status") or "unknown",
                ]
            )
        if ry:
            rows.append(
                [
                    "Ray Serve (serving)",
                    ", ".join(ry.get("models") or []) or "—",
                    ("split" if ry.get("traffic") else "—"),
                    ry.get("status") or "unknown",
                ]
            )
        _output.print_table("Pipelines", ["Surface", "Members", "Schedule/Traffic", "Status"], rows)


@app.command("storage")
def storage_cmd(
    name: str = typer.Argument(..., help="Project name"),
    bind_connection: str | None = typer.Option(
        None, "--bind-connection", help="Point storage at a P2 S3 connection (by name)"
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Re-probe used bytes from MinIO"),
) -> None:
    """Show (or bind/refresh) the project's MinIO storage location (P6)."""
    init_db()
    if not get_project(name):
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    ensure_project_storage(name)
    if bind_connection:
        if bind_project_connection(name, bind_connection, actor=os.getenv("EXAMLOPS_ACTOR")):
            _output.ok(f"Bound storage of '{name}' to connection '{bind_connection}'")
        else:
            _output.error(f"Could not bind '{bind_connection}' (missing or not an s3 connection)")
            raise typer.Exit(1)
    if refresh:
        refresh_project_usage(name)
    rec = get_project_storage(name)
    if _output.json_mode:
        _output.print_json(rec)
        return
    used_gb = (rec.get("used_bytes") or 0) / 1e9
    quota = rec.get("quota_gb")
    _output.print_table(
        f"Storage: {name}",
        ["Field", "Value"],
        [
            ["Location", f"s3://{rec['bucket']}/{rec['prefix']}"],
            ["Subpaths", "artifacts/ · datasets/ · cache/"],
            ["Used", f"{used_gb:.2f} GB" + (f" / {quota:.0f} GB" if quota else "")],
            ["Connection", rec.get("connection_ref") or "—"],
        ],
    )


@app.command("pipelines")
def pipelines_cmd(name: str = typer.Argument(..., help="Project name")) -> None:
    """Show the project's two pipeline surfaces: Prefect (training) + Ray Serve (serving) (P7)."""
    init_db()
    if not get_project(name):
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    pipes = get_project_pipelines(name)
    if _output.json_mode:
        _output.print_json(pipes)
        return
    pf, ry = pipes.get("prefect"), pipes.get("rayserve")
    if not pf and not ry:
        _output.info(
            f"No pipelines for '{name}' yet. Assign models: exa project assign {name} <M> --kind model"
        )
        return
    if pf:
        _output.print_table(
            "Prefect pipeline (training)",
            ["Field", "Value"],
            [
                ["Deployments", ", ".join(pf.get("deployments") or []) or "—"],
                ["Schedule", pf.get("schedule") or "—"],
                ["Last run", pf.get("last_run_at") or "—"],
                ["Status", pf.get("status") or "unknown"],
            ],
        )
    if ry:
        _output.print_table(
            "Ray Serve pipeline (serving)",
            ["Model", "Traffic split"],
            [[m, str(ry.get("traffic", {}).get(m, "—"))] for m in (ry.get("models") or [])]
            or [["—", "—"]],
        )


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
    """Assign a model to a project (alias for: exa project assign <p> <model> --kind model)."""
    _assign(project, "model", model)


_KINDS = ["model", "pipeline", "serving_endpoint", "connection", "dataset", "storage"]
_EX_ASSIGN = (
    "Examples:\n\n"
    "  exa project assign research JPCP --kind model\n\n"
    "  exa project assign research jpcp-train --kind pipeline"
)


def _assign(project: str, kind: str, ref: str) -> None:
    init_db()
    ok = assign_resource_to_project(project, kind, ref, added_by=_actor())
    if not ok:
        _output.error(f"Project '{project}' not found")
        raise typer.Exit(1)
    write_audit_event(
        "cli", _actor(), "project_resource_assigned", ref, {"project": project, "kind": kind}
    )
    _output.ok(f"{kind} '{ref}' assigned to project '{project}'")


@app.command("assign", epilog=_EX_ASSIGN)
def assign(
    project: str = typer.Argument(..., help="Project name"),
    ref: str = typer.Argument(..., help="Resource identifier (e.g. JPCP, jpcp-train)"),
    kind: str = typer.Option("model", "--kind", "-k", help=f"Resource kind: {', '.join(_KINDS)}"),
) -> None:
    """Assign any resource (model/pipeline/serving/connection/dataset/storage) to a project."""
    if kind not in _KINDS:
        _output.error(f"--kind must be one of: {', '.join(_KINDS)}")
        raise typer.Exit(1)
    _assign(project, kind, ref)


# --- People membership + permissions (ADR 0086, via D6 authz) -----------------

_EX_ADD_MEMBER = (
    "Examples:\n\n"
    "  exa project add-member research alice --role editor\n\n"
    "  exa project add-member research bob --role viewer"
)


@app.command("members")
def members(project: str = typer.Argument(..., help="Project name")) -> None:
    """List the people who have a role on a project."""
    init_db()
    if not get_project(project):
        _output.error(f"Project '{project}' not found")
        raise typer.Exit(1)
    rows = list_project_members(project)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info(f"No members on '{project}'. Add one: exa project add-member {project} <user>")
        return
    _output.print_table(
        f"Members of {project}",
        ["Subject", "Role", "Granted by", "When"],
        [
            [r["subject"], r["role"], r.get("granted_by") or "-", (r.get("when") or "")[:19]]
            for r in rows
        ],
    )


@app.command("add-member", epilog=_EX_ADD_MEMBER)
def add_member(
    project: str = typer.Argument(..., help="Project name"),
    subject: str = typer.Argument(..., help="User/subject id"),
    role: str = typer.Option("viewer", "--role", "-r", help="owner | editor | viewer"),
) -> None:
    """Add a person to a project (owner ⊇ editor ⊇ viewer)."""
    init_db()
    if role not in {"owner", "editor", "viewer"}:
        _output.error("--role must be one of: owner, editor, viewer")
        raise typer.Exit(1)
    if not get_project(project):
        _output.error(f"Project '{project}' not found")
        raise typer.Exit(1)
    add_project_member(project, subject, role, actor=_actor())
    write_audit_event(
        "cli", _actor(), "project_member_added", subject, {"project": project, "role": role}
    )
    _output.ok(f"Added {subject} as '{role}' on project '{project}'")


@app.command("remove-member")
def remove_member(
    project: str = typer.Argument(..., help="Project name"),
    subject: str = typer.Argument(..., help="User/subject id"),
    role: str | None = typer.Option(None, "--role", "-r", help="Specific role, or all if omitted"),
) -> None:
    """Remove a person's role(s) from a project."""
    init_db()
    n = remove_project_member(project, subject, role, actor=_actor())
    write_audit_event(
        "cli", _actor(), "project_member_removed", subject, {"project": project, "role": role}
    )
    if n:
        _output.ok(f"Removed {subject} from project '{project}'")
    else:
        _output.info("No such membership.")


# --- Active-project context (ADR 0086) ----------------------------------------


@app.command("use")
def use(
    name: str = typer.Argument(..., help="Project to make active"),
) -> None:
    """Set the active project (persisted in config.toml; EXAMLOPS_PROJECT env overrides)."""
    from examlops.cli._config import set_active_project

    init_db()
    if not get_project(name):
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    set_active_project(name)
    _output.ok(f"Active project set to '{name}'")


@app.command("current")
def current() -> None:
    """Show the active project (EXAMLOPS_PROJECT env → config.toml → none)."""
    from examlops.cli._config import active_project

    proj = active_project()
    if _output.json_mode:
        _output.print_json({"active_project": proj})
        return
    if proj:
        _output.info(f"Active project: {proj}")
    else:
        _output.info("No active project. Set one: exa project use <name>")


# --- Project FinOps & monitoring (P4, ADR 0089) -------------------------------


@app.command()
def cost(
    name: str = typer.Argument(..., help="Project name"),
) -> None:
    """Show per-project cost attribution (GPU-hours · USD · carbon)."""
    from examlops.project_finops import cost_summary

    init_db()
    if not get_project(name):
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    s = cost_summary(name)
    if _output.json_mode:
        _output.print_json(s)
        return
    _output.print_table(
        f"Cost — {name}",
        ["Field", "Value"],
        [
            ["GPU-hours (attributed)", f"{s['gpu_hours']:.2f}"],
            ["Cost USD (attributed)", f"${s['cost_usd']:.2f}"],
            ["Carbon (g CO2e)", f"{s['carbon_grams_co2e']:.1f}"],
            ["Cost records", str(s["records"])],
            ["GPU-hours (union w/ legacy)", f"{s['union_gpu_hours']:.2f}"],
        ],
    )


@app.command()
def budget(
    name: str = typer.Argument(..., help="Project name"),
) -> None:
    """Show budget/quota status and flag breaches (exit 1 if over budget)."""
    from examlops.project_finops import budget_status

    init_db()
    if not get_project(name):
        _output.error(f"Project '{name}' not found")
        raise typer.Exit(1)
    st = budget_status(name, actor=_actor(), audit=True)
    if _output.json_mode:
        _output.print_json(st)
    else:
        cons = st["consumption"]
        _output.print_table(
            f"Budget — {name}",
            ["Field", "Value"],
            [
                ["Consumed GPU-h", f"{cons['gpu_hours']:.2f}"],
                ["Consumed USD", f"${cons['cost_usd']:.2f}"],
                ["Budget", str(st["budget"] or "(none)")],
                ["Over budget", "YES" if st["over_budget"] else "no"],
            ],
        )
        for b in st["breaches"]:
            _output.warning(f"BREACH: {b}")
    if st["over_budget"]:
        raise typer.Exit(1)


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
    _output.ok(f"Granted {subject} '{relation}' on {obj}")


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
    from examlops.data.governance import list_relations

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

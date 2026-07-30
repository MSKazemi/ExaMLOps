"""Spawn-time wiring for the **Platform Ops** workbench (M3).

A normal project workbench edits pipeline content (``usecases``/``pipelines``, already rw) and authors
sandboxed providers. The *Platform Ops* workbench (project :data:`PLATFORM_OPS_PROJECT`) is the
governed platform-management environment: it additionally gets a **shared config dir** (so
``platform_admin.set_compute_cost`` / ``set_config`` reach the platform, not just the container) and —
for an admin only — **Tier-B** read-write mounts of the real integration source (the ExaMLOps↔bridge
code + pipeline engine) so an operator can edit and stage it via ``platform_admin.propose_source_change``.

Kept as a pure, testable helper so ``jupyterhub_config.py`` stays a thin caller (it cannot be unit
tested directly — it manipulates the Hub's ``c.`` traitlets object).
"""

from __future__ import annotations

from typing import Any

#: Reserved project name whose workbench is the platform-management environment. Must satisfy the
#: providers-store name gate (letters/digits/-/_) so ``deploy_provider(project=PLATFORM_OPS_PROJECT)``
#: works — hence no leading underscore.
PLATFORM_OPS_PROJECT = "platform-ops"

#: Where the shared examlops config dir is mounted inside the workbench (jovyan's default config home).
_CONTAINER_CONFIG_DIR = "/home/jovyan/.config/examlops"

#: Tier-B source dirs (repo-relative) mounted rw only for an admin platform-ops workbench.
_TIER_B_DIRS = ("platform/clients",)


def platform_ops_spawn(
    project: str,
    *,
    is_admin: bool,
    host_repo: str,
    actor: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Extra ``(volumes, environment)`` for a workbench spawn.

    For any non-platform-ops project returns ``({}, {})`` (no change). For the platform-ops project:

    * **Tier A (always):** a shared, writable config dir so config/finops/policy writes are visible to
      the platform (via ``EXAMLOPS_CONFIG`` + ``EXAMLOPS_CONFIG_DIR``); ``EXAMLOPS_ADMIN_SOURCE`` marks
      façade writes as ``workbench``; ``EXAMLOPS_ACTOR`` attributes audit rows to the real user.
    * **Tier B (admin only):** rw mounts of the integration source dirs (:data:`_TIER_B_DIRS`).
    """
    if project != PLATFORM_OPS_PROJECT:
        return {}, {}

    repo = host_repo.rstrip("/")
    volumes: dict[str, Any] = {
        # Shared config dir: writes land here (host), where the platform services can also mount it.
        f"{repo}/.platform-config": {"bind": _CONTAINER_CONFIG_DIR, "mode": "rw"},
    }
    env: dict[str, str] = {
        "EXAMLOPS_CONFIG": f"{_CONTAINER_CONFIG_DIR}/config.toml",
        "EXAMLOPS_CONFIG_DIR": _CONTAINER_CONFIG_DIR,
        "EXAMLOPS_ADMIN_SOURCE": "workbench",
        "EXAMLOPS_PLATFORM_OPS": "1",
    }
    if actor:
        env["EXAMLOPS_ACTOR"] = actor

    if is_admin:
        for rel in _TIER_B_DIRS:
            volumes[f"{repo}/{rel}"] = {"bind": f"/repo/{rel}", "mode": "rw"}
        env["EXAMLOPS_PLATFORM_SOURCE"] = "1"  # signals Tier-B (owner) capability to the notebook

    return volumes, env

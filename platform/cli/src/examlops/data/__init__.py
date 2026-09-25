"""Per-domain data-access facades (enterprise-readiness Phase 4, item 4.5).

The audit's cohesion finding: one ~6000-line ``platform_db`` module owns 236 helpers across every
domain, so nothing has a clear owner and callers reach into the whole surface. This package splits
that surface into **per-domain modules with ownership** — one module per domain, each re-exporting
only its domain's helpers.

Done **non-destructively**: the modules re-export from ``platform_db`` today (zero behaviour change,
so the 1700+ tests stay green), and new code imports the narrow per-domain module instead of the
monolith. Physically moving each helper's *body* into its module is then a safe, mechanical follow-on —
callers already import from the final location. Migrating a call site onto a ``data.<domain>`` module
also drops it off the ``platform_db`` coupling ratchet (item 4.1).
"""

from __future__ import annotations

# The schema bootstrap + connection are cross-cutting (every domain needs them), so they're re-exported
# at the package root — a fully-migrated caller does ``from examlops.data import init_db`` +
# ``from examlops.data.<dom> import …`` and no longer references ``platform_db`` at all.
from examlops.data._rowid import last_insert_id
from examlops.platform_db import get_db, init_db

__all__ = [
    "init_db",
    "get_db",
    "last_insert_id",
    "admission",
    "agent",
    "agent_versions",
    "audit",
    "autopilot",
    "catalog",
    "coordination",
    "data_assets",
    "dataplane",
    "drift",
    "evaluation",
    "events",
    "finops",
    "gateway",
    "governance",
    "hardware_profiles",
    "hpc",
    "offline",
    "projects",
    "prompts",
    "registry",
    "secrets",
    "serving",
    "specdecode",
    "tool_grants",
]


def __getattr__(name: str):  # PEP 562
    """Delegate any platform_db helper access to the monolith (migration convenience).

    Lets attribute-style callers swap ``from examlops import platform_db`` →
    ``from examlops import data as platform_db`` with no body changes: ``data.<helper>`` resolves to
    the current ``platform_db.<helper>``. The per-domain submodules remain the *preferred* owned
    surface; this root proxy just eases migrating attribute-style call sites off the monolith.
    """
    if name in __all__ and name not in ("init_db", "get_db", "last_insert_id"):
        import importlib

        return importlib.import_module(f"{__name__}.{name}")
    import examlops.platform_db as _pdb

    return getattr(_pdb, name)

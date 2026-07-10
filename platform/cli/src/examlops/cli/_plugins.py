"""Plugin discovery for the ``exa`` CLI.

Third-party packages can add their own ``exa`` subcommands by exposing a ``typer.Typer``
app under the ``examlops.cli_plugins`` entry-point group::

    # in a plugin package's pyproject.toml
    [project.entry-points."examlops.cli_plugins"]
    myteam = "my_pkg.cli:app"

This makes ExaMLOps extensible by other teams and future agent/tooling ecosystems without
touching the core CLI. Loading is resilient: a broken plugin is recorded and skipped, never
crashing the CLI.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any

PLUGIN_GROUP = "examlops.cli_plugins"


@dataclass
class PluginInfo:
    name: str
    value: str  # the entry-point target, e.g. "my_pkg.cli:app"
    ok: bool
    obj: Any = None
    error: str | None = None


def _entry_points() -> list[importlib.metadata.EntryPoint]:
    eps = importlib.metadata.entry_points()
    # Python 3.10+ selection API; fall back for older mapping-style results.
    try:
        return list(eps.select(group=PLUGIN_GROUP))
    except AttributeError:  # pragma: no cover - legacy importlib.metadata
        return list(eps.get(PLUGIN_GROUP, []))  # type: ignore[attr-defined]


def discover() -> list[PluginInfo]:
    """Return every registered plugin, loaded where possible (errors captured, not raised)."""
    infos: list[PluginInfo] = []
    for ep in _entry_points():
        try:
            obj = ep.load()
            infos.append(PluginInfo(name=ep.name, value=ep.value, ok=True, obj=obj))
        except Exception as exc:  # pragma: no cover - defensive
            infos.append(PluginInfo(name=ep.name, value=ep.value, ok=False, error=str(exc)))
    return sorted(infos, key=lambda i: i.name)


def register(app: Any) -> list[PluginInfo]:
    """Attach every loadable Typer plugin to ``app``. Returns the discovery result."""
    import typer

    infos = discover()
    for info in infos:
        if info.ok and isinstance(info.obj, typer.Typer):
            app.add_typer(info.obj, name=info.name)
        elif info.ok:
            info.ok = False
            info.error = "entry point is not a typer.Typer app"
    return infos

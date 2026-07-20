"""Notebook- and dashboard-authored, per-project calculation providers (ADR 0074).

Closes the authoring gap in the provider substrate: today a new provider needs a pip-installed
package (entry point) or hand-edited config. This module lets an authenticated project member write
a provider's Python from a **Jupyter notebook** (or the dashboard) and have the platform pick it up
— every provider domain (``cost``/``carbon``/``drift``/``llm_*`` …) benefits.

Layout (all local files so they can be imported/exec'd by every process):

    $EXAMLOPS_PROVIDERS_DIR/<project>/<domain>/<name>.py     (default ~/.config/examlops/providers)

Notebook flow::

    from examlops.providers import register_from_source, save_provider
    code = '''
    class MyCost(Provider):
        name = "my-cost"
        def compute(self, inputs):
            return {"cost_usd": inputs.get("gpu_hours", 0) * 0.85}
    '''
    register_from_source("cost", "my-cost", code)              # test live, this session
    save_provider("cost", "my-cost", code, project="research") # persist for the platform

Kept dependency-free (no platform_db/audit imports) so the substrate stays reusable — auditing and
capability checks live in the CLI/dashboard callers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .base import ProviderError
from .registry import register_provider
from .sandbox import compile_provider, validate_source

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def providers_root() -> Path:
    """Root dir for authored providers (``$EXAMLOPS_PROVIDERS_DIR`` or ``~/.config/examlops/providers``)."""
    env = os.getenv("EXAMLOPS_PROVIDERS_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "examlops" / "providers"


def _check(label: str, value: str) -> str:
    value = (value or "").strip()
    if not _SAFE.match(value):
        raise ProviderError(f"invalid {label} {value!r} (allowed: letters, digits, '-', '_')")
    return value


def provider_path(project: str, domain: str, name: str) -> Path:
    """Absolute path of one authored provider file (names validated to prevent traversal)."""
    project = _check("project", project)
    domain = _check("domain", domain)
    name = _check("name", name)
    return providers_root() / project / domain / f"{name}.py"


def register_from_source(domain: str, name: str, code: str) -> dict[str, Any]:
    """Gate + compile + register a provider live in this process (no persistence).

    For fast notebook iteration: run this, then call the domain's calculation with ``--provider
    <name>`` (or ``provider=<name>``) to test it before persisting with :func:`save_provider`.
    """
    domain = _check("domain", domain)
    name = _check("name", name)
    cls = compile_provider(code)
    register_provider(domain, name, cls)
    return {"domain": domain, "name": name, "registered": True, "class": cls.__name__}


def save_provider(
    domain: str,
    name: str,
    code: str,
    *,
    project: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """Validate + persist a provider to the project's provider dir and register it live.

    Raises :class:`~examlops.providers.sandbox.ProviderSecurityError` if the AST gate rejects the
    source, or :class:`ProviderError` if it doesn't define exactly one ``Provider`` subclass — so a
    bad provider never reaches disk. ``actor`` is returned for the caller to audit.
    """
    path = provider_path(project, domain, name)
    # Validate + compile *before* writing so invalid source never lands on disk.
    cls = compile_provider(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    register_provider(_check("domain", domain), _check("name", name), cls)
    return {
        "domain": domain,
        "name": name,
        "project": project,
        "path": str(path),
        "class": cls.__name__,
        "actor": actor,
    }


def read_provider_source(project: str, domain: str, name: str) -> str:
    """Return the stored source of an authored provider (raises if absent)."""
    path = provider_path(project, domain, name)
    if not path.exists():
        raise ProviderError(
            f"provider {name!r} (domain {domain!r}) not found in project {project!r}"
        )
    return path.read_text(encoding="utf-8")


def delete_provider(project: str, domain: str, name: str) -> bool:
    """Delete an authored provider file. Returns True if it existed."""
    path = provider_path(project, domain, name)
    if path.exists():
        path.unlink()
        return True
    return False


def list_project_providers(project: str) -> list[dict[str, Any]]:
    """List a project's authored providers (validating each without executing side effects)."""
    project = _check("project", project)
    root = providers_root() / project
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for domain_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for py in sorted(domain_dir.glob("*.py")):
            rec: dict[str, Any] = {
                "domain": domain_dir.name,
                "name": py.stem,
                "path": str(py),
                "ok": True,
                "error": None,
            }
            try:
                validate_source(py.read_text(encoding="utf-8"))
            except Exception as exc:  # gate failure or unreadable — surface, don't raise
                rec["ok"] = False
                rec["error"] = str(exc)
            out.append(rec)
    return out


def load_project_providers(project: str) -> list[dict[str, Any]]:
    """Register all of a project's authored providers into the process registry (best-effort).

    Called before a project's cost/carbon/… is computed so ``--provider <name>`` resolves. A file
    that fails the gate or fails to compile is skipped (recorded ``ok=False``), never fatal.
    """
    results: list[dict[str, Any]] = []
    for rec in list_project_providers(project):
        if not rec["ok"]:
            results.append(rec)
            continue
        try:
            code = Path(rec["path"]).read_text(encoding="utf-8")
            info = register_from_source(rec["domain"], rec["name"], code)
            results.append({**rec, "registered": True, "class": info["class"]})
        except Exception as exc:
            results.append({**rec, "ok": False, "registered": False, "error": str(exc)})
    return results

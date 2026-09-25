"""Reflect the public SDK surface (ADR 0078 clause 4).

:func:`describe` walks the exported names of each SDK namespace and returns their kind,
signature and one-line summary. ``exa docs --sdk`` renders it and the MCP agent card embeds it,
so agents and humans read the same contract — derived from the code, so it cannot drift.
"""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Any

__all__ = ["NAMESPACES", "describe", "render_markdown"]

#: The public namespaces, in reading order. The package root re-exports the domain namespaces
#: (``examlops.models`` is ``examlops.sdk.models``), so each is listed under its public name.
NAMESPACES: tuple[tuple[str, str], ...] = (
    ("examlops", "examlops"),
    ("examlops.models", "examlops.sdk.models"),
    ("examlops.drift", "examlops.sdk.drift"),
    ("examlops.audit", "examlops.sdk.audit"),
    ("examlops.hpc", "examlops.sdk.hpc"),
    ("examlops.sdk", "examlops.sdk"),
    ("examlops.sdk.errors", "examlops.sdk.errors"),
    ("examlops.sdk.deprecation", "examlops.sdk.deprecation"),
)


_QUALIFIED = re.compile(r"\b(?:[a-z_][a-z0-9_]*\.)+(?=[A-Za-z_])")


def _summary(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    return doc.strip().split("\n\n", 1)[0].replace("\n", " ").strip()


def _kind(obj: Any) -> str:
    if inspect.ismodule(obj):
        return "namespace"
    if inspect.isclass(obj):
        return "exception" if issubclass(obj, BaseException) else "type"
    if callable(obj):
        return "function"
    return "constant"


def _signature(obj: Any) -> str:
    if inspect.ismodule(obj) or not callable(obj):
        return ""
    try:
        sig = inspect.signature(obj, eval_str=True)
    except Exception:  # noqa: BLE001 - an annotation that cannot be evaluated: show it as written
        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError):
            return ""
    # Evaluated annotations render fully qualified (`typing.Any`, `examlops.sdk.models.Lineage`):
    # strip the module paths so the reference reads like the code.
    return _QUALIFIED.sub("", str(sig))


def describe() -> dict[str, Any]:
    """The SDK contract: version, and every public name per namespace."""
    import examlops
    from examlops.sdk.deprecation import DEPRECATIONS

    namespaces: dict[str, list[dict[str, Any]]] = {}
    for public, module_name in NAMESPACES:
        module = importlib.import_module(module_name)
        entries: list[dict[str, Any]] = []
        for name in getattr(module, "__all__", []):
            if name.startswith("__"):
                continue
            obj = getattr(module, name)
            entry: dict[str, Any] = {
                "name": name,
                "kind": _kind(obj),
                "signature": _signature(obj),
                "summary": _summary(obj),
            }
            dep = getattr(obj, "__deprecated__", None) or DEPRECATIONS.get(f"{public}.{name}")
            if dep is not None:
                entry["deprecated"] = {
                    "since": dep.since,
                    "removed_in": dep.removed_in,
                    "replacement": dep.replacement,
                }
            entries.append(entry)
        namespaces[public] = entries
    return {
        "api_version": examlops.api_version(),
        "package_version": examlops.__version__,
        "stability": "semver; deprecations warn for >=1 minor before removal",
        "namespaces": namespaces,
    }


def render_markdown(ref: dict[str, Any] | None = None) -> str:
    """The contract as Markdown (the ``exa docs --sdk`` page)."""
    ref = ref or describe()
    lines = [
        "# `examlops` Python SDK reference",
        "",
        f"SDK API version **{ref['api_version']}** (package {ref['package_version']}). "
        f"Stability: {ref['stability']}.",
        "",
    ]
    for public, entries in ref["namespaces"].items():
        lines += [f"## `{public}`", ""]
        for e in entries:
            sig = e["signature"] if e["kind"] == "function" else ""
            suffix = f" — {e['summary']}" if e["summary"] else ""
            dep = e.get("deprecated")
            flag = (
                f" *(deprecated since {dep['since']}, removed in {dep['removed_in']})*"
                if dep
                else ""
            )
            lines.append(f"- `{e['name']}{sig}` ({e['kind']}){flag}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

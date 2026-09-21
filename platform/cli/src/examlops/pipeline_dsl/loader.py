"""Load pipeline definitions from a Python file, and IR documents from JSON (ADR 0080).

Two modes, with **different trust**:

* ``sandboxed=False`` (default, *trusted tier*): the file is executed with ``runpy`` exactly like
  any operator-written Python. There is no isolation — do not compile a file you would not run.
* ``sandboxed=True`` (``--untrusted``): the source first passes the provider AST allow-list
  (``examlops.providers.sandbox.validate_source``: no imports, ``open``/``eval``/``getattr``,
  dunder attributes, ``global``/``nonlocal``) and then runs with restricted builtins and the DSL
  names pre-injected. A static gate for authenticated authors, **not** a hardened jail.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

from . import dsl
from .ir import IRError, validate_ir


def split_target(target: str) -> tuple[str, str | None]:
    """``"flow.py:jpcp"`` -> ``("flow.py", "jpcp")``; a bare path -> ``(path, None)``."""
    path, sep, name = target.rpartition(":")
    if sep and name and "/" not in name and "\\" not in name and path:
        return path, name
    return target, None


def _sandbox_namespace() -> dict[str, Any]:
    from examlops.providers.sandbox import _SAFE_BUILTINS

    ns: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS), "__name__": "authored_pipeline"}
    for name in dsl.__all__:
        ns[name] = getattr(dsl, name)
    return ns


def _namespace_of(path: Path, *, sandboxed: bool) -> dict[str, Any]:
    if not path.is_file():
        raise IRError(f"pipeline file not found: {path}")
    if not sandboxed:
        return runpy.run_path(str(path), run_name="exa_pipeline_file")
    from examlops.providers.sandbox import ProviderSecurityError, validate_source

    source = path.read_text(encoding="utf-8")
    try:
        validate_source(source)
    except ProviderSecurityError as exc:
        raise IRError(f"{path.name}: refused by the untrusted-mode gate: {exc}") from exc
    ns = _sandbox_namespace()
    try:
        exec(compile(source, str(path), "exec"), ns)  # noqa: S102 - gated above
    except Exception as exc:
        raise IRError(f"{path.name}: raised while loading: {exc}") from exc
    return ns


def load_pipeline_file(
    target: str, *, sandboxed: bool = False
) -> tuple[dsl.PipelineDef, list[str]]:
    """Return the selected :class:`PipelineDef` and the names of every pipeline in the file."""
    file, wanted = split_target(target)
    ns = _namespace_of(Path(file), sandboxed=sandboxed)
    found = {v.name: v for v in ns.values() if isinstance(v, dsl.PipelineDef)}
    if not found:
        raise IRError(f"{file}: no @pipeline definition found")
    names = sorted(found)
    if wanted is None:
        if len(found) > 1:
            raise IRError(
                f"{file} defines several pipelines ({', '.join(names)}); pick one with "
                f"{file}:<name>"
            )
        return next(iter(found.values())), names
    if wanted not in found:
        raise IRError(f"{file}: no pipeline named {wanted!r} (found: {', '.join(names)})")
    return found[wanted], names


def load_ir(path: str) -> dict[str, Any]:
    """Read and strictly validate an IR JSON file."""
    p = Path(path)
    if not p.is_file():
        raise IRError(f"IR file not found: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise IRError(f"{p.name}: not valid JSON: {exc}") from exc
    validate_ir(doc)
    return doc

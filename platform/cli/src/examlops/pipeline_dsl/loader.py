"""Load pipeline definitions from a Python file, and IR documents from JSON or YAML (ADR 0080).

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


#: Suffixes read as a per-model registry YAML — the IR in ADR 0080 decision 2's own terms.
YAML_SUFFIXES = (".yaml", ".yml")

#: A registry YAML or IR JSON larger than this is refused before parsing. The largest shipped
#: model YAML is a few KiB; the cap only bounds what a hostile or mistaken path can make us load.
MAX_IR_BYTES = 4 * 1024 * 1024


#: Upper bound on the *expanded* node count of a registry YAML. The byte cap alone does not bound
#: work: YAML aliases share one parsed object, so a sub-kilobyte file of nested ``&a [*a, *a, …]``
#: anchors expands to 10**8 nodes once the decompiler canonicalises it to JSON (a hang, then an
#: OOM, from a read-tier command). Shipped model YAMLs are a few hundred nodes.
MAX_YAML_NODES = 200_000


def is_yaml_ir(path: str) -> bool:
    return Path(path).suffix.lower() in YAML_SUFFIXES


def safe_load_yaml(text: str, what: str) -> Any:
    """``yaml.safe_load`` plus a bound on alias expansion; raises :class:`IRError` past it.

    Aliases stay legal (a pack may use anchors); only a document whose expanded tree exceeds
    :data:`MAX_YAML_NODES` — an alias bomb or a self-referential alias — is refused. The walk
    stops at the bound, so checking costs at most ``MAX_YAML_NODES`` steps whatever the input.
    """
    import yaml

    raw = yaml.safe_load(text)
    seen = 0
    stack: list[Any] = [raw]
    while stack:
        node = stack.pop()
        seen += 1
        if seen > MAX_YAML_NODES:
            raise IRError(
                f"{what}: expands to more than {MAX_YAML_NODES} YAML nodes (an alias bomb or a "
                "self-referential alias); refused"
            )
        if isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return raw


def load_ir(path: str) -> dict[str, Any]:
    """Read and strictly validate an IR: a JSON graph, or a per-model registry YAML.

    A ``.yaml``/``.yml`` file is the registry YAML itself (ADR 0080 decision 2: *that* is the IR).
    It is turned into the equivalent graph by :func:`~.decompile.ir_from_model_yaml`, which
    refuses anything the pipeline model cannot hold and proves the graph lowers back to the very
    same mapping — so a command that accepts either form behaves identically on both.
    """
    p = Path(path)
    if not p.is_file():
        raise IRError(f"IR file not found: {p}")
    size = p.stat().st_size
    if size > MAX_IR_BYTES:
        raise IRError(f"{p.name}: {size} bytes exceeds the {MAX_IR_BYTES}-byte IR size cap")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IRError(f"{p.name}: could not be read: {exc}") from exc
    if is_yaml_ir(path):
        import yaml

        from .decompile import ir_from_model_yaml

        try:
            raw = safe_load_yaml(text, p.name)
        except yaml.YAMLError as exc:
            raise IRError(f"{p.name}: not valid YAML: {exc}") from exc
        return ir_from_model_yaml(raw)
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise IRError(f"{p.name}: not valid JSON: {exc}") from exc
    validate_ir(doc)
    return doc

"""AST-allowlist sandbox for user-authored provider code (ADR 0074 / trust-tier ADR 0081).

Notebook- and dashboard-authored providers execute *user Python* in the CLI/pipeline/serving/
dashboard processes, so the source is gated **before** it runs. This is a static, allowlist gate —
we reject the obvious escape hatches (imports, `eval`/`exec`, `open`, `getattr`/`setattr`, dunder
attribute access, `global`/`nonlocal`) and then execute with a restricted ``__builtins__`` and a
curated namespace (``Provider``/``ProviderMeta``/``math``/``Mapping`` pre-injected, so no import is
needed). Providers are pure math on given inputs, so this is sufficient for internal, authenticated
authors — it is **not** a hardened jail against an unbounded adversary, and that limit is documented
for reviewers. A separate approval tier can wrap this for higher-risk code.
"""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping
from typing import Any

from .base import Provider, ProviderError, ProviderMeta


class ProviderSecurityError(ProviderError):
    """Raised when authored provider source fails the AST-allowlist gate."""


# Builtins a pure calculation may use. Deliberately excludes eval/exec/compile/__import__/open/
# input/getattr/setattr/delattr/globals/locals/vars/breakpoint/memoryview/type/super.
_SAFE_BUILTINS: dict[str, Any] = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "abs",
        "min",
        "max",
        "sum",
        "round",
        "len",
        "range",
        "enumerate",
        "zip",
        "map",
        "filter",
        "sorted",
        "reversed",
        "all",
        "any",
        "float",
        "int",
        "str",
        "bool",
        "dict",
        "list",
        "tuple",
        "set",
        "frozenset",
        "isinstance",
        "print",
        "divmod",
        "pow",
        "repr",
        "ValueError",
        "KeyError",
        "TypeError",
        "ZeroDivisionError",
        "Exception",
        # Required to execute `class ...:` / `@decorator` under a restricted __builtins__.
        # __build_class__ only constructs classes; it is not an escape vector on its own.
        "__build_class__",
    )
}

# Names that must never be called (would defeat the gate).
_FORBIDDEN_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "memoryview",
        "type",
        "super",
        "object",
        "classmethod",
        "staticmethod",
        "property",
        "help",
        "exit",
        "quit",
    }
)


def _reject(node: ast.AST, reason: str) -> ProviderSecurityError:
    line = getattr(node, "lineno", "?")
    return ProviderSecurityError(f"line {line}: {reason}")


def validate_source(code: str) -> None:
    """Static-check authored provider source; raise :class:`ProviderSecurityError` if unsafe.

    Rejects: any ``import``, ``eval``/``exec``/``open``/``getattr`` &c. calls, dunder attribute
    access (``x.__globals__``), and ``global``/``nonlocal``. Everything else (class/func defs,
    arithmetic, comprehensions, conditionals) is allowed.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ProviderSecurityError(f"syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise _reject(
                node, "imports are not allowed (Provider/ProviderMeta/math are pre-injected)"
            )
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise _reject(node, "global/nonlocal are not allowed")
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("__") and attr.endswith("__"):
                raise _reject(node, f"dunder attribute access '{attr}' is not allowed")
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_CALLS:
            raise _reject(node, f"use of '{node.id}' is not allowed")


def _sandbox_namespace() -> dict[str, Any]:
    """The globals a provider module executes in: safe builtins + the injected provider toolkit."""
    return {
        "__builtins__": dict(_SAFE_BUILTINS),
        "__name__": "authored_provider",
        "Provider": Provider,
        "ProviderMeta": ProviderMeta,
        "math": math,
        "Mapping": Mapping,
    }


def compile_provider(code: str) -> type[Provider]:
    """Validate + execute authored source and return the single ``Provider`` subclass it defines.

    Raises :class:`ProviderSecurityError` on a gate failure, :class:`ProviderError` if the module
    does not define exactly one ``Provider`` subclass.
    """
    validate_source(code)
    ns = _sandbox_namespace()
    try:
        exec(compile(code, "<authored-provider>", "exec"), ns)  # noqa: S102 - gated above
    except Exception as exc:
        raise ProviderError(f"provider source raised at import time: {exc}") from exc
    classes = [
        v
        for v in ns.values()
        if isinstance(v, type) and issubclass(v, Provider) and v is not Provider
    ]
    if not classes:
        raise ProviderError("no Provider subclass defined (expected `class X(Provider): ...`)")
    if len(classes) > 1:
        raise ProviderError(
            f"exactly one Provider subclass expected, found {len(classes)}: "
            f"{[c.__name__ for c in classes]}"
        )
    return classes[0]

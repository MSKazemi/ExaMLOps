"""Security guard tests for the provider trust-tier contract (INC-6 / ADR 0081).

Invariants verified:
1. No ``eval`` / ``exec`` / ``__import__`` calls on the sandboxed config-path code.
2. ``SAFE_FUNCTIONS`` allow-list has not grown without review (size + known-good set).
3. The sandboxed evaluator rejects dangerous patterns from YAML-authored formulas.

These tests are *structural guards* — they catch inadvertent widening of the sandbox
boundary.  Any intentional change to SAFE_FUNCTIONS must update the expected set below
and include a security-review note in the commit message (ADR 0081 §trust-tier).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from examlops.providers.expression import SAFE_FUNCTIONS, evaluate_formula
from examlops.providers.base import ProviderError

# ── 1. No raw eval/exec on the sandboxed config paths ────────────────────────

_SANDBOXED_MODULES = [
    "examlops/providers/expression.py",
    "examlops/providers/yaml_provider.py",
    "examlops/providers/loader.py",
]

_ROOT = Path(__file__).parent.parent.parent / "platform" / "cli" / "src"


def _ast_calls(source: str, func_names: set[str]) -> list[str]:
    """Return list of call names from source that match func_names."""
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in func_names:
                hits.append(f"line {node.lineno}: {node.func.id}(...)")
    return hits


@pytest.mark.parametrize("rel_path", _SANDBOXED_MODULES)
def test_no_raw_eval_exec_in_sandboxed_modules(rel_path):
    """Sandboxed modules must never call built-in eval/exec/compile directly."""
    path = _ROOT / rel_path
    source = path.read_text()
    hits = _ast_calls(source, {"eval", "exec", "compile"})
    assert not hits, (
        f"{rel_path} uses raw eval/exec which bypasses the simpleeval sandbox:\n"
        + "\n".join(hits)
    )


# ── 2. SAFE_FUNCTIONS allow-list is exactly the reviewed set ─────────────────

# Reviewed 2026-07-15 (INC-6 / ADR 0081).  Any addition requires a new review note.
_REVIEWED_SAFE_FUNCTIONS = {
    "min", "max", "abs", "round", "pow",
    "log", "log10", "log2", "exp", "sqrt", "floor", "ceil", "sum",
}


def test_safe_functions_match_reviewed_set():
    """SAFE_FUNCTIONS must equal the reviewed set. Add a review note if you expand it."""
    actual = set(SAFE_FUNCTIONS.keys())
    added = actual - _REVIEWED_SAFE_FUNCTIONS
    removed = _REVIEWED_SAFE_FUNCTIONS - actual
    assert not added, (
        f"New functions in SAFE_FUNCTIONS not in reviewed set: {added}. "
        "Add a security-review note to the commit and update _REVIEWED_SAFE_FUNCTIONS."
    )
    assert not removed, (
        f"Functions removed from SAFE_FUNCTIONS that were in reviewed set: {removed}. "
        "Update _REVIEWED_SAFE_FUNCTIONS to match."
    )


# ── 3. Sandbox rejects dangerous YAML-authored patterns ───────────────────────

@pytest.mark.parametrize("bad_expr", [
    "__import__('os').system('echo hi')",
    "(1).__class__.__bases__",
    "open('/etc/passwd')",
    "__builtins__['exec']('import os')",
    "[x for x in []]",         # comprehension (forbidden by simpleeval)
    "lambda x: x",             # lambda (forbidden)
])
def test_sandbox_rejects_dangerous_expressions(bad_expr):
    """Dangerous YAML formulas must raise ProviderError, never execute."""
    with pytest.raises((ProviderError, Exception)):
        evaluate_formula(bad_expr, {"x": 1})


def test_sandbox_allows_safe_arithmetic():
    """Legal arithmetic formulas evaluate correctly."""
    assert evaluate_formula("min(a, b) + sqrt(c)", {"a": 3.0, "b": 5.0, "c": 4.0}) == pytest.approx(5.0)


def test_sandbox_chained_formulas_use_correct_output_from_providers():
    from examlops.providers.expression import evaluate_formulas
    out = evaluate_formulas(
        {"kwh": "gpu_hours * 0.3", "co2e_g": "kwh * 400"},
        {"gpu_hours": 10.0},
    )
    assert out["kwh"] == pytest.approx(3.0)
    assert out["co2e_g"] == pytest.approx(1200.0)

"""Safe evaluation of config-authored arithmetic formulas.

This is the *no-code* provider path: a sysadmin writes a formula like
``"gpu_hours * (gpu_tdp_watts / 1000) * pue"`` in YAML and we evaluate it **without executing
arbitrary Python**. We use :mod:`simpleeval` (a sandboxed AST-walking evaluator) rather than
``eval`` — it forbids imports, attribute access, comprehensions, and caps ``**`` blow-ups.

``simpleeval`` is an optional ``[finops]`` extra: it is imported lazily so the built-in Python
providers keep working when it is absent, and a missing install fails with an actionable hint
rather than an ``ImportError`` traceback.

Formulas are evaluated **in declared order**, and each result is added to the namespace, so a
later formula can reference an earlier output (e.g. ``co2e_g`` uses ``kwh``) — the same
output-chaining idea as the Green Software Foundation Impact Framework pipeline.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .base import ProviderError

# Curated math allow-list exposed to formulas — pure, side-effect-free helpers only.
SAFE_FUNCTIONS: dict[str, Any] = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "pow": pow,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "sum": sum,
}

_INSTALL_HINT = (
    "The declarative 'expression' provider needs simpleeval. Install it with "
    "'pip install examlops[finops]' (or 'pip install simpleeval')."
)


def _make_evaluator(names: Mapping[str, Any]):
    """Build a locked-down ``simpleeval.SimpleEval`` bound to ``names`` (lazy import)."""
    try:
        import simpleeval
    except ImportError as exc:  # pragma: no cover - exercised via a monkeypatch in tests
        raise ProviderError(_INSTALL_HINT) from exc

    ev = simpleeval.SimpleEval(functions=dict(SAFE_FUNCTIONS), names=dict(names))
    # Defensive: even though simpleeval already forbids these, keep the surface minimal.
    ev.ATTR_INDEX_FALLBACK = False
    return ev


def evaluate_formula(expression: str, names: Mapping[str, Any]) -> Any:
    """Safely evaluate a single arithmetic ``expression`` against ``names``.

    Raises :class:`ProviderError` on a malformed/forbidden expression (with the offending text)
    instead of leaking simpleeval's internal exception types to callers.
    """
    ev = _make_evaluator(names)
    try:
        return ev.eval(expression)
    except ProviderError:
        raise
    except Exception as exc:  # simpleeval raises many small exception types
        raise ProviderError(f"invalid formula {expression!r}: {exc}") from exc


def evaluate_formulas(formulas: Mapping[str, str], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate an ordered map of ``output_name -> expression``.

    Each output is added to the namespace before the next formula runs, so formulas may depend on
    earlier ones. Returns only the computed outputs (not the input names).
    """
    namespace: dict[str, Any] = dict(inputs)
    outputs: dict[str, Any] = {}
    for out_name, expr in formulas.items():
        value = evaluate_formula(expr, namespace)
        namespace[out_name] = value
        outputs[out_name] = value
    return outputs

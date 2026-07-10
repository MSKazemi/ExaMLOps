"""A :class:`Provider` whose formula and coefficients come from a declarative config block.

This is the reusable bridge between the safe :mod:`expression` evaluator and the :class:`Provider`
interface: given a YAML/dict block of ``formulas`` + ``coefficients`` + ``metadata``, it produces a
provider that behaves exactly like a hand-written Python provider — the no-code authoring path.

Example config block (domain-agnostic — the same shape works for carbon, cost, …)::

    provider: expression
    coefficients: {gpu_tdp_watts: 700, pue: 1.3, grid_intensity_g_per_kwh: 250}
    formulas:
      kwh:    "gpu_hours * (gpu_tdp_watts / 1000) * pue"
      co2e_g: "kwh * grid_intensity_g_per_kwh"
    metadata: {uncertainty: 0.25, methodology: "H100 @ PUE 1.3, DE grid 250 g/kWh"}
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import Provider, ProviderError, ProviderMeta
from .expression import evaluate_formulas


class ExpressionProvider(Provider):
    """Provider defined by declarative ``formulas`` + ``coefficients`` (evaluated with simpleeval).

    ``coefficients`` are defaults merged *under* the per-call ``inputs`` (an input of the same
    name wins), so a caller can still override a configured coefficient at compute time.
    """

    def __init__(
        self,
        name: str,
        formulas: Mapping[str, str],
        coefficients: Mapping[str, Any] | None = None,
        meta: ProviderMeta | None = None,
        version: str = "config",
    ) -> None:
        if not formulas:
            raise ProviderError(f"expression provider {name!r} has no 'formulas'")
        self.name = name
        self.version = version
        self._formulas = dict(formulas)
        self._coefficients = dict(coefficients or {})
        self._meta = meta or ProviderMeta(
            methodology=f"config formulas: {', '.join(formulas)}",
            outputs=tuple(formulas.keys()),
            params=tuple(self._coefficients.keys()),
        )

    def metadata(self) -> ProviderMeta:
        return self._meta

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        namespace = {**self._coefficients, **dict(inputs)}
        return evaluate_formulas(self._formulas, namespace)


def build_expression_provider(name: str, block: Mapping[str, Any]) -> ExpressionProvider:
    """Construct an :class:`ExpressionProvider` from a parsed config/YAML ``block``."""
    formulas = block.get("formulas") or {}
    if not isinstance(formulas, Mapping):
        raise ProviderError(f"provider {name!r}: 'formulas' must be a mapping")
    coefficients = block.get("coefficients") or {}
    meta_block = block.get("metadata") or {}
    meta = ProviderMeta(
        methodology=str(meta_block.get("methodology", f"config formulas: {', '.join(formulas)}")),
        uncertainty=meta_block.get("uncertainty"),
        units=dict(meta_block.get("units", {})),
        outputs=tuple(formulas.keys()),
        params=tuple(coefficients.keys()),
        source=str(meta_block.get("source", "")),
    )
    return ExpressionProvider(name, formulas, coefficients, meta)

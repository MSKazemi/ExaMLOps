"""A5 — Data contracts & quality gates (ADR 0005, spec A5-data-contracts).

A ``DataContract`` is a versioned declaration of a dataset's expected schema and
semantics — column presence/dtype, nullability, numeric range, categorical domain,
exact embedding dimensionality, minimum row count, and freshness. Each check has a
severity (``error`` fails the gate closed, ``warn`` is recorded but non-blocking).

Contracts live as versioned code in ``pipelines/contracts/<dataset>.py`` and are
resolved by :func:`load_contract`. Validation is **pure-python** (pandas only) so it
needs no extra dependency; a Pandera/GX backend can be layered later without changing
the interface.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

ERROR = "error"
WARN = "warn"


@dataclass
class Check:
    """One contract check: a named predicate over a dataframe with a severity."""

    name: str
    severity: str
    fn: Callable[[Any], tuple[bool, Any]]  # df -> (passed, observed)

    def run(self, df: Any) -> dict[str, Any]:
        try:
            passed, observed = self.fn(df)
        except Exception as exc:  # a check that errors is a failed check, never a crash
            passed, observed = False, f"check-error: {exc}"
        return {
            "name": self.name,
            "severity": self.severity,
            "passed": bool(passed),
            "observed": observed,
        }


@dataclass
class QualityResult:
    """Outcome of validating a dataframe against a contract."""

    passed: bool
    score: float
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c["passed"] and c["severity"] == ERROR]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c["passed"] and c["severity"] == WARN]


@dataclass
class DataContract:
    """A versioned data contract for one dataset."""

    dataset: str
    version: str
    checks: list[Check] = field(default_factory=list)

    def validate(self, df: Any) -> QualityResult:
        """Run every check. ``passed`` is False iff any ``error``-severity check fails."""
        results = [c.run(df) for c in self.checks]
        n = len(results) or 1
        score = round(sum(1 for r in results if r["passed"]) / n, 4)
        passed = all(r["passed"] for r in results if r["severity"] == ERROR)
        return QualityResult(passed=passed, score=score, checks=results)


# --- check builders (pure, dataframe-level) ----------------------------------


def column_present(column: str, dtype: str | None = None, severity: str = ERROR) -> Check:
    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        if dtype is not None:
            actual = str(df[column].dtype)
            if dtype not in actual:
                return False, f"{column} dtype {actual} != {dtype}"
        return True, "ok"

    return Check(f"column:{column}", severity, _fn)


def not_null(column: str, max_null_rate: float = 0.0, severity: str = ERROR) -> Check:
    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        rate = float(df[column].isna().mean())
        return rate <= max_null_rate, f"null_rate={rate:.4f} (max {max_null_rate})"

    return Check(f"not_null:{column}", severity, _fn)


def in_range(
    column: str, low: float | None = None, high: float | None = None, severity: str = ERROR
) -> Check:
    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        col = df[column].dropna()
        if low is not None and (col < low).any():
            return False, f"{column} < {low} present (min {col.min()})"
        if high is not None and (col > high).any():
            return False, f"{column} > {high} present (max {col.max()})"
        return True, "ok"

    return Check(f"range:{column}", severity, _fn)


def categorical(column: str, allowed: Sequence[Any], severity: str = ERROR) -> Check:
    allowed_set = set(allowed)

    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        seen = set(df[column].dropna().unique())
        extra = seen - allowed_set
        return not extra, f"unexpected values: {sorted(extra)}" if extra else "ok"

    return Check(f"categorical:{column}", severity, _fn)


def embedding_dim(column: str, dim: int, severity: str = ERROR) -> Check:
    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        non_null = df[column].dropna()
        if len(non_null) == 0:
            return False, "no non-null embeddings"
        first = non_null.iloc[0]
        actual = len(first)
        return actual == dim, f"embedding dim {actual} != {dim}"

    return Check(f"embedding_dim:{column}", severity, _fn)


def min_rows(n: int, severity: str = ERROR) -> Check:
    def _fn(df: Any) -> tuple[bool, Any]:
        rows = len(df)
        return rows >= n, f"rows={rows} (min {n})"

    return Check(f"min_rows:{n}", severity, _fn)


# --- request-contract validation (inference gate, R8/R9) ---------------------


def validate_request(
    payload: dict[str, Any],
    *,
    required: Sequence[str],
    embedding_field: str | None = None,
    embedding_dim: int | None = None,
    ranges: dict[str, tuple[float | None, float | None]] | None = None,
) -> tuple[bool, list[str]]:
    """Validate an inference request payload (spec R8). Returns ``(ok, errors)``.

    Never raises — malformed input yields ``ok=False`` with human-readable errors so
    the ingress can return a 4xx instead of a 5xx (R9).
    """
    errors: list[str] = []
    for field_name in required:
        if field_name not in payload or payload[field_name] is None:
            errors.append(f"missing required field '{field_name}'")
    if embedding_field and embedding_dim is not None and embedding_field in payload:
        emb = payload[embedding_field]
        try:
            if len(emb) != embedding_dim:
                errors.append(f"{embedding_field} dim {len(emb)} != {embedding_dim}")
        except TypeError:
            errors.append(f"{embedding_field} is not a sequence")
    for field_name, (low, high) in (ranges or {}).items():
        if field_name in payload and payload[field_name] is not None:
            val = payload[field_name]
            if low is not None and val < low:
                errors.append(f"{field_name}={val} < {low}")
            if high is not None and val > high:
                errors.append(f"{field_name}={val} > {high}")
    return (not errors, errors)


# --- contract resolution -----------------------------------------------------

_REGISTRY: dict[str, DataContract] = {}


def register_contract(contract: DataContract) -> None:
    _REGISTRY[contract.dataset.lower()] = contract


def load_contract(dataset: str) -> DataContract | None:
    """Resolve a dataset's contract by importing ``pipelines.contracts.<dataset>``.

    Returns None when no contract module exists (validation then no-ops / passes).
    """
    key = dataset.lower()
    if key in _REGISTRY:
        return _REGISTRY[key]
    import importlib

    for modname in (f"pipelines.contracts.{key}", f"pipelines.contracts.{dataset}"):
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError:
            continue
        contract = getattr(mod, "CONTRACT", None)
        if isinstance(contract, DataContract):
            register_contract(contract)
            return contract
    return None

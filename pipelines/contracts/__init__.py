"""A5 — Data contracts & quality gates (ADR 0005, spec A5-data-contracts).

A ``DataContract`` is a versioned declaration of a dataset's expected schema and
semantics — column presence/dtype, nullability, numeric range, categorical domain,
exact embedding dimensionality, minimum row count, and freshness. Each check has a
severity (``error`` fails the gate closed, ``warn`` is recorded but non-blocking).

Contracts live as versioned code in ``pipelines/contracts/<dataset>.py`` and are
resolved by :func:`load_contract`.

**Two engines, one verdict (ADR 0005 clause 1).** Every check built here carries a declarative
``spec`` as well as a pure-python predicate. When Pandera is installed it is the engine: the
contract compiles to one ``pandera`` ``DataFrameSchema``, validated lazily, and a row-level
failure names the rows that failed. Without it — or with ``EXAMLOPS_CONTRACT_ENGINE=python`` —
the predicates run on pandas alone. The two agree check for check
(``tests/unit/test_data_contract_engines.py`` holds them to it), so installing Pandera never
changes whether data passes, only how precisely a failure is reported. ``QualityResult.engine``
records which one judged.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

ERROR = "error"
WARN = "warn"


@dataclass
class Check:
    """One contract check: a named predicate over a dataframe with a severity."""

    name: str
    severity: str
    fn: Callable[[Any], tuple[bool, Any]]  # df -> (passed, observed)
    #: The check in declarative form — what the Pandera engine compiles. ``None`` for a
    #: hand-written ``Check``, which every engine runs through ``fn``.
    spec: dict[str, Any] | None = None

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
    #: Which engine judged: ``pandera`` or ``python`` (see the module docstring).
    engine: str = "python"

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c["passed"] and c["severity"] == ERROR]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c["passed"] and c["severity"] == WARN]


@dataclass
class DataContract:
    """A versioned data contract for one dataset.

    ``table`` names the one table of a multi-table dataplane snapshot the contract describes
    (ADR 0130). Unset, a snapshot's tables are each validated on their own — never concatenated.
    """

    dataset: str
    version: str
    checks: list[Check] = field(default_factory=list)
    table: str | None = None

    def validate(self, df: Any) -> QualityResult:
        """Run every check. ``passed`` is False iff any ``error``-severity check fails."""
        results, engine = _run_checks(self.checks, df)
        n = len(results) or 1
        score = round(sum(1 for r in results if r["passed"]) / n, 4)
        passed = all(r["passed"] for r in results if r["severity"] == ERROR)
        return QualityResult(passed=passed, score=score, checks=results, engine=engine)


def contract_engine() -> str:
    """``EXAMLOPS_CONTRACT_ENGINE``: ``auto`` (default — Pandera when importable) or ``python``.

    There is deliberately no value that *requires* Pandera: the two engines reach the same
    verdict, so a missing library can cost precision in a failure report and nothing else.
    An unrecognised value is treated as ``auto`` with a warning.
    """
    raw = os.environ.get("EXAMLOPS_CONTRACT_ENGINE", "auto").strip().lower()
    if raw not in ("auto", "python"):
        warnings.warn(
            f"EXAMLOPS_CONTRACT_ENGINE={raw!r} is not auto|python; using auto", stacklevel=2
        )
        return "auto"
    return raw


def _run_checks(checks: list[Check], df: Any) -> tuple[list[dict[str, Any]], str]:
    if contract_engine() == "auto":
        from pipelines.contracts import _pandera  # noqa: PLC0415

        try:
            results = _pandera.run(checks, df)
        except Exception as exc:  # noqa: BLE001 - an engine fault is not a data verdict
            # A failing *check* is reported by Pandera as a failure case, never raised; reaching
            # here means the engine itself broke. The reference predicates still give the same
            # verdict, so fall back rather than fail a gate — or, worse, skip it.
            warnings.warn(f"pandera engine failed ({exc!r}); using pure-python", stacklevel=2)
            results = None
        if results is not None:
            return results, "pandera"
    return [c.run(df) for c in checks], "python"


# --- check builders (pure, dataframe-level) ----------------------------------


#: Dtype *families*: ``dtype=`` names one of these to mean a kind of column rather than a dtype
#: spelling. Anything else is matched as a substring of the dtype's name (``"float"`` accepts
#: ``float32`` and ``float64``).
STRING = "string"


def _string_problem(col: Any) -> str | None:
    """Why ``col`` is not a column of strings, or ``None`` when it is.

    A column of strings is spelled three ways across the pandas versions a contract meets: ``object``
    holding ``str`` values (pandas 2's default), ``string`` (``StringDtype``) and ``str`` (pandas
    3's default — a parquet string column reads back as ``str``). Matching the spelling ``"object"``
    breaks on the pandas 3 upgrade, and ``object`` also holds lists and mixed values. Nulls are
    ignored; a categorical is not a string column. (``pd.api.types.is_string_dtype`` answers a
    different question: it calls ``["a", None]`` not a string column and a categorical one.)
    """
    import pandas as pd  # noqa: PLC0415

    if isinstance(col.dtype, pd.StringDtype):
        return None
    if str(col.dtype) != "object":
        return f"dtype {col.dtype} is not a string dtype"
    for value in col.dropna():
        if not isinstance(value, str):
            return f"holds a non-string value ({type(value).__name__})"
    return None


def column_present(column: str, dtype: str | None = None, severity: str = ERROR) -> Check:
    """``column`` exists, and — with ``dtype`` — has that dtype: a family (``"string"``) or a
    substring of the dtype's name."""

    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        if dtype == STRING:
            problem = _string_problem(df[column])
            return (problem is None), (f"{column} {problem}" if problem else "ok")
        if dtype is not None:
            actual = str(df[column].dtype)
            if dtype not in actual:
                return False, f"{column} dtype {actual} != {dtype}"
        return True, "ok"

    return Check(
        f"column:{column}", severity, _fn, {"kind": "column", "column": column, "dtype": dtype}
    )


def not_null(column: str, max_null_rate: float = 0.0, severity: str = ERROR) -> Check:
    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        col = df[column]
        # An empty column has no nulls; its mean is NaN, and NaN <= x is False — which failed
        # this check on an empty table with "null_rate=nan". Emptiness is min_rows's question.
        rate = float(col.isna().mean()) if len(col) else 0.0
        return rate <= max_null_rate, f"null_rate={rate:.4f} (max {max_null_rate})"

    spec = {"kind": "not_null", "column": column, "max_null_rate": max_null_rate}
    return Check(f"not_null:{column}", severity, _fn, spec)


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

    return Check(
        f"range:{column}",
        severity,
        _fn,
        {"kind": "range", "column": column, "low": low, "high": high},
    )


def categorical(column: str, allowed: Sequence[Any], severity: str = ERROR) -> Check:
    allowed_set = set(allowed)

    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        seen = set(df[column].dropna().unique())
        extra = seen - allowed_set
        return not extra, f"unexpected values: {sorted(extra)}" if extra else "ok"

    spec = {"kind": "categorical", "column": column, "allowed": list(allowed)}
    return Check(f"categorical:{column}", severity, _fn, spec)


def _width(value: Any) -> int | None:
    """An embedding's length, or ``None`` for a value that has none (a scalar, a string)."""
    if isinstance(value, str | bytes):
        return None
    try:
        return len(value)
    except TypeError:
        return None


def embedding_dim(column: str, dim: int, severity: str = ERROR) -> Check:
    """Every non-null value in ``column`` is a sequence of exactly ``dim`` numbers.

    Every row, not the first: this used to read ``len(first)`` and so passed a table whose first
    embedding had the right width and whose others did not — a ragged column, which fails later
    and further from its cause, in collate or at the model's input layer.
    """

    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        non_null = df[column].dropna()
        if len(non_null) == 0:
            return False, "no non-null embeddings"
        widths = non_null.map(_width)
        bad = widths[widths != dim]
        if len(bad) == 0:
            return True, "ok"
        first = bad.index[0]
        return False, (
            f"{len(bad)} of {len(non_null)} embeddings not {dim}-dim (row {first}: {widths[first]})"
        )

    spec = {"kind": "embedding_dim", "column": column, "dim": dim}
    return Check(f"embedding_dim:{column}", severity, _fn, spec)


def min_rows(n: int, severity: str = ERROR) -> Check:
    def _fn(df: Any) -> tuple[bool, Any]:
        # A bounded dataplane check validates a sample; `attrs["total_rows"]` is the table's real
        # size, so a large table is not judged by the size of its sample.
        rows = int((getattr(df, "attrs", None) or {}).get("total_rows", len(df)))
        return rows >= n, f"rows={rows} (min {n})"

    return Check(f"min_rows:{n}", severity, _fn, {"kind": "min_rows", "n": n})


def fresh_within(
    column: str,
    max_age: timedelta | str,
    severity: str = ERROR,
    *,
    now: Callable[[], datetime] | None = None,
) -> Check:
    """The newest timestamp in ``column`` is no older than ``max_age`` (``"24h"``, ``"7d"``, or a
    ``timedelta``) — the freshness clause of ADR 0005. Naive timestamps are read as UTC; a value
    that is not a timestamp fails the check rather than being skipped, since dropping unparseable
    rows could leave only the fresh ones. ``now`` exists for tests.
    """
    import pandas as pd  # noqa: PLC0415

    limit = pd.Timedelta(max_age)
    if limit <= pd.Timedelta(0):
        raise ValueError(f"fresh_within: max_age must be positive, got {max_age!r}")

    def _fn(df: Any) -> tuple[bool, Any]:
        if column not in df.columns:
            return False, f"missing column '{column}'"
        with warnings.catch_warnings():  # "could not infer format" — the verdict is the point
            warnings.simplefilter("ignore", UserWarning)
            stamps = pd.to_datetime(df[column].dropna(), utc=True)
        if len(stamps) == 0:
            return False, "no timestamps"
        newest = stamps.max()
        current = pd.Timestamp(now()) if now else pd.Timestamp.now(tz="UTC")
        if current.tzinfo is None:
            current = current.tz_localize("UTC")
        age = current - newest
        return age <= limit, f"newest={newest.isoformat()} age={age} (max {limit})"

    spec = {"kind": "freshness", "column": column, "max_age": str(limit)}
    return Check(f"freshness:{column}", severity, _fn, spec)


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

"""The Pandera engine for data contracts (ADR 0005 clause 1).

:func:`run` compiles a contract's checks into one ``pandera`` ``DataFrameSchema`` and validates it
lazily, so every failure is collected in one pass rather than the first one raised. Row-level
checks are Pandera's own — ``ge``/``le`` for a range, ``isin`` for a domain, a not-null mask, an
element-wise width test for an embedding — and a failure reports the rows that failed and their
values. Table-level facts (a column's dtype, a null-rate ceiling above zero, a row-count floor,
freshness) have no row to point at; each is a Pandera check wrapping the same predicate the
pure-python engine runs, so the two cannot disagree about them.

A check with no ``spec`` (hand-written) runs through its own ``fn``. :func:`run` returns ``None``
when Pandera is not installed; the caller then uses the pure-python engine.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipelines.contracts import Check

#: How many failing rows a report quotes. The count is always exact; the examples are a sample.
_EXAMPLES = 3


def run(checks: list[Check], df: Any) -> list[dict[str, Any]] | None:
    """Every check's result dict, in contract order — or ``None`` without Pandera."""
    try:
        import pandera.pandas as pa
        from pandera.errors import SchemaErrors
    except ImportError:
        return None
    from pipelines.contracts import _width  # noqa: PLC0415

    column_checks: dict[Any, list[Any]] = {}  # column -> pandera checks, in order
    frame_checks: list[Any] = []
    # contract check index -> the (column | None, position) of each pandera check it compiled to
    owners: dict[int, list[tuple[Any, int]]] = {}
    referenced: list[Any] = []  # every column a compiled check names, first mention first

    def touch(i: int, column: Any) -> None:
        """Check ``i`` concerns ``column``: its absence fails it, whatever else it compiles to."""
        owners.setdefault(i, [])
        if column is not None and column not in column_checks:
            column_checks[column] = []
            referenced.append(column)

    def add(i: int, column: Any, check: Any) -> None:
        touch(i, column)
        if column is None:
            owners[i].append((None, len(frame_checks)))
            frame_checks.append(check)
        else:
            owners[i].append((column, len(column_checks[column])))
            column_checks[column].append(check)

    def whole(check: Check, column: Any) -> Any:
        """A table-level Pandera check over the reference predicate, scoped to one column."""
        if column is None:
            return pa.Check(lambda d: bool(check.fn(d)[0]), element_wise=False, ignore_na=False)
        return pa.Check(
            lambda s: bool(check.fn(s.to_frame(name=column))[0]),
            element_wise=False,
            ignore_na=False,
        )

    for i, check in enumerate(checks):
        spec = check.spec
        if spec is None:
            continue
        kind, column = spec["kind"], spec.get("column")
        if kind == "column":
            touch(i, column)  # presence is decided by `required`; a dtype by the predicate
            if spec.get("dtype") is not None:
                add(i, column, whole(check, column))
        elif kind == "not_null":
            if spec["max_null_rate"] <= 0:
                add(i, column, pa.Check(lambda s: s.notna(), ignore_na=False))
            else:
                add(i, column, whole(check, column))
        elif kind == "range":
            if spec["low"] is not None:
                add(i, column, pa.Check.ge(spec["low"]))
            if spec["high"] is not None:
                add(i, column, pa.Check.le(spec["high"]))
            touch(i, column)  # an open range only asks that the column exist
        elif kind == "categorical":
            add(i, column, pa.Check.isin(spec["allowed"]))
        elif kind == "embedding_dim":
            dim = spec["dim"]
            add(i, column, pa.Check(lambda s: s.notna().any(), element_wise=False, ignore_na=False))
            add(i, column, pa.Check(lambda v, d=dim: _width(v) == d, element_wise=True))
        elif kind in ("min_rows", "freshness"):
            add(i, column, whole(check, column))
        # any other kind is one this engine does not know: the check's own predicate decides

    schema = pa.DataFrameSchema(
        columns={
            c: pa.Column(checks=column_checks[c], nullable=True, required=True, coerce=False)
            for c in referenced
        },
        checks=frame_checks,
        strict=False,
        coerce=False,
    )
    failures: dict[tuple[Any, int], list[tuple[Any, Any]]] = {}
    missing: set[Any] = set()
    try:
        schema.validate(df, lazy=True)
    except SchemaErrors as exc:
        for row in exc.failure_cases.to_dict("records"):
            if row["check"] == "column_in_dataframe":
                missing.add(row["failure_case"])
                continue
            number = row.get("check_number")
            if number is None or number != number:  # NaN: a schema-level case we did not ask for
                continue
            column = row["column"] if row["schema_context"] == "Column" else None
            failures.setdefault((column, int(number)), []).append(
                (row.get("index"), row["failure_case"])
            )

    results: list[dict[str, Any]] = []
    for i, check in enumerate(checks):
        if i not in owners:
            results.append(check.run(df))
            continue
        column = check.spec.get("column") if check.spec else None
        base = {"name": check.name, "severity": check.severity}
        if column is not None and column in missing:
            results.append({**base, "passed": False, "observed": f"missing column '{column}'"})
            continue
        cases = [case for pos in owners[i] for case in failures.get(pos, [])]
        if not cases:
            results.append({**base, "passed": True, "observed": "ok"})
            continue
        rows = [(idx, value) for idx, value in cases if idx is not None and idx == idx]
        if rows:
            observed: Any = _rows_observed(check.spec or {}, rows)
        else:  # a table-level fact, or a check that raised: say it as the predicate does
            observed = check.run(df)["observed"]
        results.append({**base, "passed": False, "observed": observed})
    return results


def _rows_observed(spec: dict[str, Any], rows: list[tuple[Any, Any]]) -> str:
    """``"2 rows below 0.0 — row 4: -1.5, row 9: -0.2"``: an exact count, then a few examples."""
    from pipelines.contracts import _width  # noqa: PLC0415

    kind = spec.get("kind")
    show: Callable[[Any], str] | None
    if kind == "not_null":
        what, show = "null", None
    elif kind == "range":
        bounds = f"[{spec['low']}, {spec['high']}]".replace("None", "")
        what, show = f"outside {bounds}", repr
    elif kind == "categorical":
        what, show = "outside the domain", repr
    elif kind == "embedding_dim":
        what, show = f"not {spec['dim']}-dim", lambda v: f"dim {_width(v)}"
    else:
        what, show = "failing", repr
    try:
        rows = sorted(rows, key=lambda r: r[0])
    except TypeError:  # an index of mixed types: keep Pandera's order
        pass
    sample = rows[:_EXAMPLES]
    if show is None:
        quoted = "rows " + ", ".join(str(idx) for idx, _ in sample)
    else:
        quoted = ", ".join(f"row {idx}: {_clip(show(value))}" for idx, value in sample)
    more = ", …" if len(rows) > _EXAMPLES else ""
    return f"{len(rows)} row(s) {what} — {quoted}{more}"


def _clip(text: str) -> str:
    return text if len(text) <= 40 else text[:37] + "..."

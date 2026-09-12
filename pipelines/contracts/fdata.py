"""A5 — Data contract for the FData dataset (ADR 0005).

Columns per the real parquet schema: ``pclass`` (strings memory-bound/compute-bound, checked
as the ``string`` dtype family so it holds on pandas 2 and 3), ``mbwidth`` (double memory
bandwidth), ``embedding`` (list<float>, 384-dim). Versioned as code; bump ``version`` on any
change.
"""

from __future__ import annotations

from pipelines.contracts import (
    ContractExamples,
    DataContract,
    categorical,
    column_present,
    embedding_dim,
    in_range,
    min_rows,
    not_null,
)

CONTRACT = DataContract(
    dataset="FData",
    # v2: `pclass` is a *string* column, not an `object` one — under pandas 3 a parquet string
    # column reads back as `str`, and v1's `dtype="object"` failed every run (BL-067).
    version="2",
    checks=[
        column_present("pclass", dtype="string"),
        categorical("pclass", ["memory-bound", "compute-bound"]),
        column_present("mbwidth"),
        in_range("mbwidth", low=0.0),
        not_null("mbwidth", max_null_rate=0.05, severity="warn"),
        column_present("embedding"),
        embedding_dim("embedding", 384),
        min_rows(1),
    ],
)


def _row(pclass: str = "memory-bound", mbwidth: float = 12.5, dim: int = 384) -> dict:
    return {"pclass": pclass, "mbwidth": mbwidth, "embedding": [0.1] * dim}


#: What this contract must accept, and what it must reject (ADR 0005 clause 4). Run in CI by
#: `tests/unit/test_data_contract_examples.py` through `exa data validate`, so a contract cannot
#: quietly start refusing the data it exists to describe.
EXAMPLES = ContractExamples(
    valid=[_row(), _row("compute-bound", 0.0), _row("memory-bound", 900.0)],
    invalid=[
        ([{"mbwidth": 1.0, "embedding": [0.1] * 384}], "column:pclass"),
        ([_row("io-bound")], "categorical:pclass"),
        ([_row(mbwidth=-1.0)], "range:mbwidth"),
        ([_row(dim=383)], "embedding_dim:embedding"),
        ([_row(), {"pclass": 7, "mbwidth": 1.0, "embedding": [0.1] * 384}], "column:pclass"),
    ],
)

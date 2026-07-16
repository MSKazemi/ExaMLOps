"""A5 — Data contract for the FData dataset (ADR 0005).

Columns per the real parquet schema (see CLAUDE.md "FData Parquet Schema"):
``pclass`` (string: memory-bound/compute-bound), ``mbwidth`` (double memory
bandwidth), ``embedding`` (list<float>, 384-dim). Versioned as code; bump
``version`` on any change.
"""

from __future__ import annotations

from pipelines.contracts import (
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
    version="1",
    checks=[
        column_present("pclass", dtype="object"),
        categorical("pclass", ["memory-bound", "compute-bound"]),
        column_present("mbwidth"),
        in_range("mbwidth", low=0.0),
        not_null("mbwidth", max_null_rate=0.05, severity="warn"),
        column_present("embedding"),
        embedding_dim("embedding", 384),
        min_rows(1),
    ],
)

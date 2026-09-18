"""What a drift console read costs as the number of models grows — measured, per engine.

Both routes were once a plain N+1: a query for each model's window plus one for its baseline —
**21 statements for 10 models, 101 for 50**. On Postgres each statement is a network round trip, so
that was one round trip per model to render a panel.

The obvious fix, one windowed statement for every model, is **only right on one engine**. Measured
over 250 000 snapshots across 50 models:

| | one statement per model | one windowed statement |
|---|---|---|
| SQLite | **47 ms** | 129 ms |
| Postgres | 134–148 ms | **70 ms** |

On SQLite a statement is an in-process call and each per-model query stops after `window` rows
straight down the `(model, ts)` index, while the window function must rank the whole table first.
On Postgres the round trips dominate and the planner handles the ranking better. So the route
dispatches on the engine, and this guard asserts the property that is *correct for that engine*
rather than one number for both:

- on Postgres, the statement count must not grow with the model count;
- on SQLite, the per-model form is deliberate, so the count is allowed to grow — but it must stay
  proportional (a constant number of statements per model), which is what catches a new N+1 layered
  on top of it.

Both branches also assert the route returned the rows the test seeded: these routes swallow
exceptions to degrade rather than 500, and a route that quietly returned `[]` would otherwise look
beautifully constant.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("_isolate_postgres_state")


class _Counting:
    """`sqlite3.Connection` is a C type and refuses attribute assignment, so proxy it."""

    def __init__(self, conn: Any, counter: list[int]) -> None:
        self._conn = conn
        self._counter = counter

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._counter[0] += 1
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _seed(models: int, snaps: int = 12) -> None:
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        conn.execute("DELETE FROM drift_snapshots")
        conn.execute("DELETE FROM drift_baselines")
        conn.execute("DELETE FROM input_snapshots")
        conn.execute("DELETE FROM input_baselines")
        for m in range(models):
            name = f"model{m:03d}"
            for s in range(snaps):
                conn.execute(
                    "INSERT INTO drift_snapshots (model, alias, prediction) VALUES (?,?,?)",
                    (name, "Production", 1.0 + s * 0.01),
                )
                conn.execute(
                    "INSERT INTO input_snapshots (model, alias, emb_norm, emb_mean, emb_std) "
                    "VALUES (?,?,?,?,?)",
                    (name, "Production", 1.0 + s * 0.01, 0.5, 0.1),
                )
            conn.execute(
                "INSERT INTO drift_baselines (model, stats) VALUES (?,?)",
                (name, json.dumps({"mean": 1.0, "std": 0.2, "n": snaps})),
            )


def _queries_for(route: Any, models: int, monkeypatch: Any) -> tuple[int, int]:
    """(statements issued, rows returned) for one call of ``route`` with ``models`` models."""
    import routers.drift_data as drift_data

    _seed(models)
    counter = [0]
    real = drift_data.connect
    monkeypatch.setattr(drift_data, "connect", lambda path: _Counting(real(path), counter))
    rows = asyncio.run(route(_=None, model=None))
    return counter[0], len(rows)


@pytest.mark.parametrize("route_name", ["drift_status", "input_drift_status"])
def test_a_drift_read_scales_the_way_its_engine_needs(route_name, monkeypatch):
    import routers.drift_data as drift_data
    from dbconn import postgres_configured

    route = getattr(drift_data, route_name)

    few_queries, few_rows = _queries_for(route, 5, monkeypatch)
    many_queries, many_rows = _queries_for(route, 50, monkeypatch)

    assert few_rows == 5 and many_rows == 50, (
        f"the route returned {few_rows} and {many_rows} rows — it is not reading what this test "
        "seeded, so any query count would prove nothing"
    )

    if postgres_configured():
        assert few_queries == many_queries, (
            f"{route_name} issued {few_queries} statements for 5 models and {many_queries} for "
            "50. On Postgres every statement is a network round trip, so this must not grow with "
            "the model count: read every model's window in one windowed statement."
        )
        assert many_queries <= 6, f"{many_queries} statements for one read is more than this needs"
        return

    # SQLite: per-model reads are the measured-faster shape, so growth is expected — but it must
    # stay one bounded set of statements per model, which is what catches a new N+1 on top.
    per_model = (many_queries - few_queries) / 45
    assert per_model <= 1.5, (
        f"{route_name} added {per_model:.1f} statements per model (5 models: {few_queries}, 50: "
        f"{many_queries}). One indexed read per model is the intended shape; more than that is a "
        "second N+1 layered on it."
    )

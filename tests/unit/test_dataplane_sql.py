"""ADR 0130 — sql connector against a real SQLite file."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors.sql import SqlConnector, begin_read_only  # noqa: E402
from examlops.dataplane.types import Limits  # noqa: E402


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'lab.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, node TEXT, power REAL, ts TEXT)"
        )
        for i in range(1, 8):
            c.exec_driver_sql(
                "INSERT INTO jobs VALUES (?,?,?,?)", (i, f"n{i % 2}", i * 10.0, f"2026-01-0{i}")
            )
    return {"kind": "sql", "url": url}


def _rows(batches):
    return [r for tb in batches for r in tb.batch.to_pylist()]


def test_validate_spec():
    c = SqlConnector()
    assert c.validate_spec({}) == ["spec.query or spec.table is required"]
    assert "identifier" in c.validate_spec({"table": "jobs; DROP TABLE x"})[0]
    assert c.validate_spec({"table": "jobs"}) == []


def test_reads_a_table_in_chunks(db):
    batches = list(SqlConnector().read(db, {"table": "jobs", "chunk_rows": 3}, None, Limits()))
    assert [tb.batch.num_rows for tb in batches] == [3, 3, 1]
    assert {tb.table for tb in batches} == {"jobs"}


def test_incremental_uses_the_watermark(db):
    spec = {"table": "jobs", "watermark_column": "id", "incremental": True}
    first = list(SqlConnector().read(db, spec, None, Limits()))
    wm = first[-1].watermark
    assert wm == {"column": "id", "value": 7, "type": "int"}
    with sa.create_engine(db["url"]).begin() as c:
        c.exec_driver_sql("INSERT INTO jobs VALUES (8,'n0',80.0,'2026-01-08')")
    assert [r["id"] for r in _rows(SqlConnector().read(db, spec, wm, Limits()))] == [8]


def test_query_with_params(db):
    rows = _rows(
        SqlConnector().read(
            db,
            {
                "query": "SELECT id FROM jobs WHERE node = :n",
                "params": {"n": "n1"},
                "output": "odd",
            },
            None,
            Limits(),
        )
    )
    assert [r["id"] for r in rows] == [1, 3, 5, 7]


def test_session_is_read_only(db):
    eng = sa.create_engine(db["url"])
    with eng.connect() as c:
        begin_read_only(c, timeout_s=5)
        with pytest.raises(sa.exc.OperationalError):
            c.exec_driver_sql("INSERT INTO jobs VALUES (99,'x',1.0,'x')")


def test_password_never_appears_in_errors(tmp_path):
    conn = {
        "kind": "sql",
        "url": "postgresql+psycopg://bob@127.0.0.1:1/nope",
        "secret": "hunter2hunter2",
    }
    probe = SqlConnector().probe(conn)
    assert probe.ok is False and "hunter2" not in probe.detail


@pytest.fixture
def disposals(monkeypatch):
    """Wrap ``sa.create_engine`` so every disposal of an engine it built is recorded."""
    disposed = []
    real_create_engine = sa.create_engine

    def wrapper(*a, **kw):
        eng = real_create_engine(*a, **kw)
        orig_dispose = eng.dispose

        def dispose(*da, **dkw):
            disposed.append(eng)
            return orig_dispose(*da, **dkw)

        eng.dispose = dispose
        return eng

    monkeypatch.setattr(sa, "create_engine", wrapper)
    return disposed


def test_engine_is_disposed_after_a_full_read(db, disposals):
    list(SqlConnector().read(db, {"table": "jobs", "chunk_rows": 3}, None, Limits()))
    assert len(disposals) == 1


def test_engine_is_disposed_when_a_read_generator_is_closed_early(db, disposals):
    gen = SqlConnector().read(db, {"table": "jobs", "chunk_rows": 3}, None, Limits())
    next(gen)
    gen.close()
    assert len(disposals) == 1


def test_engine_is_disposed_after_probe(db, disposals):
    assert SqlConnector().probe(db).ok is True
    assert len(disposals) == 1


def test_engine_is_disposed_after_discover(db, disposals):
    SqlConnector().discover(db, {})
    assert len(disposals) == 1

"""ADR 0130 final review I9 — an ingestion contract reads one table at a time, and a bounded
number of its rows.

`_check_contract` used to `pd.concat` every staged part of every table into one DataFrame. With a
contract on a PM100-sized source that is the whole snapshot in memory — the OOM class task 22a
fixed for reads — and concatenating tables with different columns is not even the data the
contract describes (a column one table lacks becomes nulls in the union).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

_ROOT = Path(__file__).parents[2]
for _p in (str(_ROOT), str(_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops import dataplane as dpl  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.dataplane import pull as pull_mod  # noqa: E402
from examlops.dataplane.connectors import registry  # noqa: E402
from examlops.dataplane.connectors.base import BaseConnector  # noqa: E402
from examlops.dataplane.types import DataplaneError, Probe, TableBatch  # noqa: E402
from pipelines import contracts  # noqa: E402


class _TwoTables(BaseConnector):
    """`jobs` (column `a`, the 5th row null) and `nodes` (no `a`), in batches of two rows."""

    kind = "twotables"
    connection_required = False

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        values = [1, 2, 3, 4, None]
        for i in range(0, len(values), 2):
            rows = [{"a": v} for v in values[i : i + 2]]
            yield TableBatch("jobs", pa.RecordBatch.from_pylist(rows, schema=_A))
        yield TableBatch("nodes", pa.RecordBatch.from_pylist([{"b": 9}]))


_A = pa.schema([("a", pa.int64())])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    registry.reset()
    registry.register(_TwoTables())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS", raising=False)
    yield
    registry.reset()


def _contract(monkeypatch, name, *checks, table=None):
    c = contracts.DataContract(dataset=name, version="1", checks=list(checks), table=table)
    monkeypatch.setitem(contracts._REGISTRY, name.lower(), c)


def _parts(tmp_path, *parts):
    paths = []
    for i, rows in enumerate(parts):
        p = tmp_path / f"part-{i:05d}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=_A), p)
        paths.append(p)
    return paths


# ── the bounded reader ───────────────────────────────────────────────────────


def test_the_sample_stops_at_the_cap_and_says_so(tmp_path):
    paths = _parts(tmp_path, [{"a": 1}, {"a": 2}], [{"a": 3}, {"a": 4}], [{"a": 5}])
    sample = pull_mod.read_contract_sample(paths, max_rows=3)
    assert sample.frame["a"].tolist() == [1, 2, 3]
    assert (sample.rows, sample.total_rows, sample.sampled) == (3, 5, True)
    assert sample.frame.attrs["total_rows"] == 5  # what min_rows judges


def test_a_table_that_fits_the_cap_is_read_whole_and_not_sampled(tmp_path):
    paths = _parts(tmp_path, [{"a": 1}, {"a": 2}], [{"a": 3}])
    sample = pull_mod.read_contract_sample(paths, max_rows=3)
    assert sample.frame["a"].tolist() == [1, 2, 3]
    assert (sample.rows, sample.total_rows, sample.sampled) == (3, 3, False)


def test_the_sample_never_reads_a_part_past_the_cap(tmp_path, monkeypatch):
    """Streaming, not read-then-truncate: once the cap is reached no further part is opened."""
    paths = _parts(tmp_path, [{"a": 1}, {"a": 2}], [{"a": 3}], [{"a": 4}])
    opened: list[str] = []
    real = pq.ParquetFile.iter_batches

    def spy(self, *a, **k):
        opened.append(str(self.metadata.num_rows))
        return real(self, *a, **k)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy)
    pull_mod.read_contract_sample(paths, max_rows=2)
    assert len(opened) == 1


def test_an_empty_table_keeps_its_columns(tmp_path):
    paths = _parts(tmp_path, [])
    sample = pull_mod.read_contract_sample(paths, max_rows=10)
    assert list(sample.frame.columns) == ["a"] and sample.rows == 0 and not sample.sampled


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 200_000), ("50", 50), ("0", 200_000), ("-3", 200_000), ("many", 200_000)],
)
def test_the_cap_comes_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS", raw)
    assert pull_mod.contract_max_rows() == expected


# ── the ingestion check in a real pull ───────────────────────────────────────


def test_a_multi_table_pull_validates_only_the_contracts_table(monkeypatch):
    """Concatenated, `nodes`' row would add a null `a` and the pull would fail."""
    _contract(
        monkeypatch,
        "jobs_contract",
        contracts.column_present("a"),
        contracts.not_null("a", max_null_rate=0.25),
        table="jobs",
    )
    dpl.define_source("s", "twotables", spec={}, contract="jobs_contract")
    result = dpl.run_pull("s")
    assert result.status == "succeeded"
    assert result.contract == {
        "name": "jobs_contract",
        "sampled": False,
        "tables": {"jobs": {"rows_checked": 5, "rows": 5, "sampled": False}},
    }


def test_a_contract_without_a_table_checks_each_table_on_its_own(monkeypatch):
    _contract(monkeypatch, "any_contract", contracts.column_present("a"))
    dpl.define_source("s", "twotables", spec={}, contract="any_contract")
    with pytest.raises(DataplaneError, match="nodes"):
        dpl.run_pull("s")


def test_the_pull_honours_the_row_cap_and_records_that_it_sampled(monkeypatch):
    """Only the first 3 of `jobs`' 5 rows are read: the null 5th row is never seen."""
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS", "3")
    _contract(monkeypatch, "strict", contracts.not_null("a"), table="jobs")
    dpl.define_source("s", "twotables", spec={}, contract="strict")
    result = dpl.run_pull("s", actor="t")
    assert result.status == "succeeded"
    assert result.contract["sampled"] is True
    assert result.contract["tables"]["jobs"] == {"rows_checked": 3, "rows": 5, "sampled": True}
    succeeded = [e for e in export_audit_events() if e["action"] == "dataplane_pull_succeeded"]
    assert "sampled" in str(succeeded[-1]["details"])

    monkeypatch.delenv("EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS")
    with pytest.raises(DataplaneError, match="not_null"):
        dpl.run_pull("s", full=True)


def test_a_failing_sampled_check_says_it_was_sampled(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS", "2")
    _contract(monkeypatch, "needs_b", contracts.column_present("b"), table="jobs")
    dpl.define_source("s", "twotables", spec={}, contract="needs_b")
    with pytest.raises(DataplaneError, match=r"jobs.*first 2 of 5 rows"):
        dpl.run_pull("s")


def test_a_pull_without_a_contract_reports_none():
    dpl.define_source("s", "twotables", spec={})
    assert dpl.run_pull("s").contract is None

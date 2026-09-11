"""ADR 0130 — zenodo connector with recorded API responses."""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors import zenodo  # noqa: E402
from examlops.dataplane.types import DataplaneError, LimitExceeded, Limits, SpecError  # noqa: E402


def _parquet_bytes():
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([{"job": 1}, {"job": 2}]), buf)
    return buf.getvalue()


@pytest.fixture
def api(monkeypatch):
    blob = _parquet_bytes()
    md5 = hashlib.md5(blob).hexdigest()  # noqa: S324 - Zenodo publishes md5
    record = {
        "id": 10127767,
        "revision": 3,
        "files": [
            {
                "key": "job_table.parquet",
                "size": len(blob),
                "checksum": f"md5:{md5}",
                "links": {
                    "self": "https://zenodo.org/api/records/10127767/files/job_table.parquet",
                    "content": (
                        "https://zenodo.org/api/records/10127767/files/job_table.parquet/content"
                    ),
                },
            },
            {
                "key": "README.md",
                "size": 3,
                "checksum": "md5:x",
                "links": {"self": "https://zenodo.org/r"},
            },
        ],
    }
    state = {"blob": blob, "record": record}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/records/10127767":
            return httpx.Response(200, json=state["record"])
        if request.url.path.endswith("/content") or request.url.path == "/r":
            return httpx.Response(200, content=state["blob"])
        return httpx.Response(404)

    monkeypatch.setattr(
        zenodo,
        "_client_factory",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    return state


def test_downloads_and_parses_matching_files(api):
    batches = list(
        zenodo.ZenodoConnector().read(
            None, {"record": 10127767, "files": "*.parquet"}, None, Limits()
        )
    )
    assert {tb.table for tb in batches} == {"job_table"}
    assert set(batches[-1].watermark["files"]) == {"job_table.parquet"}  # README.md filtered out
    assert batches[-1].watermark["record"] == 10127767
    assert batches[-1].watermark["version"] == 3
    total_rows = sum(tb.batch.num_rows for tb in batches)
    assert total_rows == 2


def test_checksum_mismatch_fails(api):
    api["blob"] = b"tampered"
    with pytest.raises(DataplaneError, match="checksum"):
        list(
            zenodo.ZenodoConnector().read(
                None, {"record": 10127767, "files": "*.parquet"}, None, Limits()
            )
        )


def test_unchanged_record_yields_nothing_when_incremental(api):
    c = zenodo.ZenodoConnector()
    wm = list(c.read(None, {"record": 10127767, "files": "*.parquet"}, None, Limits()))[
        -1
    ].watermark
    assert (
        list(
            c.read(
                None,
                {"record": 10127767, "files": "*.parquet", "incremental": True},
                wm,
                Limits(),
            )
        )
        == []
    )


def test_unknown_checksum_algorithm_raises_dataplane_error(api):
    api["record"]["files"][0]["checksum"] = "sha999:deadbeef"
    with pytest.raises(DataplaneError, match="sha999"):
        list(
            zenodo.ZenodoConnector().read(
                None, {"record": 10127767, "files": "*.parquet"}, None, Limits()
            )
        )


def test_invalid_table_name_from_key_requires_spec_table(api):
    api["record"]["files"][0]["key"] = "job table!.parquet"
    with pytest.raises(SpecError, match="spec.table"):
        list(
            zenodo.ZenodoConnector().read(
                None, {"record": 10127767, "files": "*.parquet"}, None, Limits()
            )
        )
    # but an explicit spec.table sidesteps the derivation entirely
    batches = list(
        zenodo.ZenodoConnector().read(
            None,
            {"record": 10127767, "files": "*.parquet", "table": "jobs"},
            None,
            Limits(),
        )
    )
    assert {tb.table for tb in batches} == {"jobs"}


def test_discover_lists_record_files(api):
    infos = zenodo.ZenodoConnector().discover(None, {"record": 10127767})
    assert {i.name for i in infos} == {"job_table.parquet", "README.md"}


def test_probe_reports_record_file_count(api):
    probe = zenodo.ZenodoConnector().probe(None, {"record": 10127767})
    assert probe.ok
    assert "2" in probe.detail


def test_empty_checksum_fails_closed(api):
    api["record"]["files"][0]["checksum"] = ""
    with pytest.raises(DataplaneError, match="checksum"):
        list(
            zenodo.ZenodoConnector().read(
                None, {"record": 10127767, "files": "*.parquet"}, None, Limits()
            )
        )


def test_missing_checksum_key_fails_closed(api):
    del api["record"]["files"][0]["checksum"]
    with pytest.raises(DataplaneError, match="checksum"):
        list(
            zenodo.ZenodoConnector().read(
                None, {"record": 10127767, "files": "*.parquet"}, None, Limits()
            )
        )


def test_max_bytes_smaller_than_file_raises_limit_exceeded(api):
    limits = Limits(max_bytes=8)
    with pytest.raises(LimitExceeded, match="max_bytes"):
        list(
            zenodo.ZenodoConnector().read(
                None, {"record": 10127767, "files": "*.parquet"}, None, limits
            )
        )


def test_required_spec_and_incremental_flags():
    c = zenodo.ZenodoConnector()
    assert c.required_spec == ("record",)
    assert c.supports_incremental is True
    assert c.validate_spec({}) == ["spec.record is required"]

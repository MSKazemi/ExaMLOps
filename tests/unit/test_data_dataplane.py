"""ADR 0130 §6 — dataplane catalog tables and helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.data import dataplane as dp  # noqa: E402
from examlops.data import init_db  # noqa: E402


def setup_function(_):
    init_db()


def test_source_roundtrip_and_upsert():
    dp.upsert_source(
        "",
        "pm100",
        connector="sql",
        connection="lab-pg",
        spec={"table": "jobs"},
        schedule="1h",
        limits={"max_rows": 5},
        contract=None,
        enabled=True,
        actor="t",
    )
    row = dp.get_source("pm100")
    assert row["spec"] == {"table": "jobs"} and row["limits"] == {"max_rows": 5}
    dp.upsert_source(
        "",
        "pm100",
        connector="sql",
        connection="lab-pg",
        spec={"table": "jobs2"},
        schedule=None,
        limits={},
        contract=None,
        enabled=False,
        actor="t",
    )
    row = dp.get_source("pm100")
    assert row["spec"] == {"table": "jobs2"} and row["enabled"] == 0
    assert [s["name"] for s in dp.list_sources("")] == ["pm100"]
    assert dp.delete_source("pm100") is True and dp.get_source("pm100") is None


def test_projects_scope_sources():
    for proj in ("", "research"):
        dp.upsert_source(
            proj,
            "s",
            connector="rest",
            connection=None,
            spec={},
            schedule=None,
            limits={},
            contract=None,
            enabled=True,
            actor=None,
        )
    assert len(dp.list_sources()) == 2
    assert [s["project"] for s in dp.list_sources("research")] == ["research"]


def test_pull_lifecycle_and_last_committed():
    first, second = dp.new_pull_id(), dp.new_pull_id()
    assert first < second  # time-ordered ids
    dp.insert_pull(first, "", "s", trigger_kind="manual", actor="t", parent_revision=None)
    dp.update_pull(
        first,
        status="succeeded",
        finished=True,
        revision="r1",
        row_count=3,
        byte_count=10,
        watermark={"value": 5},
    )
    dp.insert_pull(second, "", "s", trigger_kind="schedule", actor=None, parent_revision="r1")
    dp.update_pull(second, status="failed", finished=True, error="boom")
    assert dp.last_pull("", "s")["revision"] == "r1"
    assert dp.last_pull("", "s", committed_only=False)["status"] == "failed"
    assert [p["id"] for p in dp.list_pulls(source="s")] == [second, first]
    assert dp.get_pull(first)["watermark"] == {"value": 5}


def test_pull_id_strictly_increasing_1000():
    ids = [dp.new_pull_id() for _ in range(1000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)

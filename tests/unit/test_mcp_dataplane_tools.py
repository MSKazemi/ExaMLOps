"""ADR 0130 — MCP surface for the dataplane."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.data import init_db  # noqa: E402
from examlops.mcp import tools  # noqa: E402


def test_read_tools_never_raise():
    init_db()
    assert tools.dataplane_sources()["sources"] == []
    out = tools.dataplane_snapshots("nope")
    assert "snapshots" in out or "error" in out


def test_snapshots_survive_a_run_of_failed_pulls():
    """An agent asking what a source has must not be told "nothing" because it is failing now.

    This tool answers over the same rows as `exa dataplane snapshots` and the service's
    `/sources/{name}/snapshots`, and reads the fewest of the three — so it is the first to go
    empty when a source's recent pulls fail, and it is the surface with nobody watching it read.
    """
    from examlops.data import dataplane as catalog

    init_db()
    good = catalog.new_pull_id()
    catalog.insert_pull(good, "", "s", trigger_kind="manual", actor=None, parent_revision=None)
    catalog.update_pull(good, status="succeeded", finished=True, revision="rev-abc")
    with catalog.get_db() as conn:
        conn.executemany(
            "INSERT INTO dataplane_pulls (id, project, source, status) VALUES (?, '', 's', 'failed')",
            [(catalog.new_pull_id(),) for _ in range(60)],
        )

    revs = [s["revision"] for s in tools.dataplane_snapshots("s")["snapshots"]]
    assert revs == ["rev-abc"], revs


def test_pull_of_a_missing_source_returns_an_error(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MCP_ALLOW_WRITES", raising=False)
    assert (
        "error" in tools.dataplane_pull("nope") or tools.dataplane_pull("nope").get("ok") is False
    )


def test_registry_declares_them():
    names = {spec.fn.__name__: spec for spec in tools.REGISTRY}
    assert names["dataplane_sources"].mutating is False
    assert names["dataplane_pull"].mutating is True and names["dataplane_pull"].tier in {
        "A",
        "B",
        "C",
    }

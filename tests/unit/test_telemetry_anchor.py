"""Anchored telemetry (ADR 0110 decision 2) and the recorded audit review (ADR 0113 decision 5).

The tamper model under test: an anchor is an audit-chain event whose hash covers a side-table
row range; editing, deleting or inserting inside an anchored range breaks the anchor, while an
audited retention prune is reported as pruned rather than as tampering. Reviews are recorded
events, so "is anyone actually looking?" is answerable from the chain.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops import telemetry_anchor as ta
from examlops.cli.main import app
from examlops.platform_db import get_db, init_db, write_drift_snapshot

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()


def _seed(n: int = 5) -> None:
    for i in range(n):
        write_drift_snapshot("JPCP", "Production", float(i), f"job-{i}")


class TestAnchoring:
    def test_anchor_covers_new_rows_and_is_chained(self):
        _seed(5)
        results = ta.anchor_telemetry("tester")
        drift = next(r for r in results if r["table"] == "drift_snapshots")
        assert drift["anchored"] and drift["rows"] == 5
        with get_db() as conn:
            row = conn.execute(
                "SELECT hash, details FROM audit_events WHERE action='telemetry_anchor' "
                "AND target='drift_snapshots'"
            ).fetchone()
        assert row["hash"], "the anchor event must be inside the hash chain"
        assert json.loads(row["details"])["sha256"] == drift["sha256"]

    def test_reanchor_is_incremental(self):
        _seed(3)
        ta.anchor_telemetry("tester")
        _seed(2)
        results = ta.anchor_telemetry("tester")
        drift = next(r for r in results if r["table"] == "drift_snapshots")
        assert drift["anchored"] and drift["rows"] == 2 and drift["from_id"] == 4

    def test_nothing_new_skips(self):
        _seed(2)
        ta.anchor_telemetry("tester")
        results = ta.anchor_telemetry("tester")
        drift = next(r for r in results if r["table"] == "drift_snapshots")
        assert drift == {"table": "drift_snapshots", "rows": 0, "anchored": False}

    def test_verify_clean(self):
        _seed(4)
        ta.anchor_telemetry("tester")
        result = ta.verify_anchors()
        assert result["ok"] and result["breaks"] == []
        assert result["unanchored_rows"] == {}

    def test_edit_inside_anchored_range_breaks(self):
        _seed(4)
        ta.anchor_telemetry("tester")
        with get_db() as conn:
            conn.execute("UPDATE drift_snapshots SET prediction=999.0 WHERE id=2")
        result = ta.verify_anchors()
        assert not result["ok"]
        assert result["breaks"][0]["table"] == "drift_snapshots"
        assert result["breaks"][0]["reason"] == "hash mismatch"

    def test_delete_inside_anchored_range_breaks(self):
        _seed(4)
        ta.anchor_telemetry("tester")
        with get_db() as conn:
            conn.execute("DELETE FROM drift_snapshots WHERE id=3")
        result = ta.verify_anchors()
        assert not result["ok"]
        assert result["breaks"][0]["reason"] == "row count changed"

    def test_audited_prune_is_not_tampering(self):
        from examlops.data.audit import write_audit_event

        _seed(4)
        ta.anchor_telemetry("tester")
        with get_db() as conn:
            conn.execute("DELETE FROM drift_snapshots WHERE id<=2")
        # The prune is itself on the chain, AFTER the anchor.
        write_audit_event("exa-data", "tester", "telemetry_pruned", None, {"days": 0})
        result = ta.verify_anchors()
        assert result["ok"], "an audited prune must not read as tampering"
        assert len(result["pruned_anchors"]) == 1

    def test_unanchored_rows_are_reported_not_hidden(self):
        _seed(2)
        ta.anchor_telemetry("tester")
        _seed(3)
        result = ta.verify_anchors()
        assert result["unanchored_rows"]["drift_snapshots"] == 3


class TestCli:
    def test_anchor_and_verify_commands(self):
        _seed(3)
        r = runner.invoke(app, ["audit", "anchor"])
        assert r.exit_code == 0, r.output
        r = runner.invoke(app, ["audit", "verify-anchors"])
        assert r.exit_code == 0, r.output

    def test_verify_anchors_exits_1_on_break(self):
        _seed(3)
        ta.anchor_telemetry("tester")
        with get_db() as conn:
            conn.execute("UPDATE drift_snapshots SET prediction=42.0 WHERE id=1")
        r = runner.invoke(app, ["audit", "verify-anchors"])
        assert r.exit_code == 1


class TestAuditReview:
    def test_review_records_event_with_range_and_sample(self, monkeypatch):
        monkeypatch.setenv("EXAMLOPS_ACTOR", "reviewer-a")
        _seed(3)  # produces audit-adjacent rows? drift snapshots aren't audit events
        from examlops.data.audit import write_audit_event

        for i in range(6):
            write_audit_event("test", "someone", f"act_{i}", None, None)
        r = runner.invoke(app, ["audit", "review", "--sample", "4", "--notes", "weekly pass"])
        assert r.exit_code == 0, r.output
        with get_db() as conn:
            row = conn.execute(
                "SELECT actor, details, hash FROM audit_events WHERE action='audit_reviewed'"
            ).fetchone()
        d = json.loads(row["details"])
        assert row["actor"] == "reviewer-a"
        assert d["reviewer"] == "reviewer-a"
        assert len(d["sample_ids"]) == 4
        assert d["notes"] == "weekly pass"
        assert row["hash"], "the review record must itself be chained"

    def test_second_review_covers_only_new_events(self):
        from examlops.data.audit import write_audit_event

        write_audit_event("test", "x", "act_a", None, None)
        runner.invoke(app, ["audit", "review"])
        r = runner.invoke(app, ["audit", "review"])
        assert "Nothing new to review" in r.output

    def test_reviews_lists_history(self):
        from examlops.data.audit import write_audit_event

        write_audit_event("test", "x", "act_a", None, None)
        runner.invoke(app, ["audit", "review"])
        r = runner.invoke(app, ["audit", "reviews"])
        assert r.exit_code == 0
        assert ".." in r.output  # the covered range column

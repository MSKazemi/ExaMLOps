"""ADR 0148 decision 4 / Verification 4 — one ledger per agent task, total = sum of entries."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops import platform_db as pdb
from examlops.cli.main import app
from examlops.finops import economics as eco
from examlops.finops import task_ledger as tl

runner = CliRunner()


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in ("EXAMLOPS_SANDBOX_USD_PER_SECOND", "EXAMLOPS_IDLE_USD_PER_GB_HOUR"):
        monkeypatch.delenv(var, raising=False)
    pdb.init_db()


def _audit(action):
    with pdb.get_db() as conn:
        return [
            json.loads(r[0] or "{}")
            for r in conn.execute(
                "SELECT details FROM audit_events WHERE action=? ORDER BY id", (action,)
            ).fetchall()
        ]


def test_task_total_equals_the_sum_of_its_model_tool_sandbox_idle_and_standby_entries():
    tl.record_entry("T1", "model_call", 0.02, project="p")
    tl.record_entry("T1", "model_call", 0.03, project="p")
    tl.record_entry("T1", "tool_call", 0.001, project="p")
    tl.record_sandbox("T1", 120, project="p", usd_per_second=0.0001)
    tl.record_idle_state("T1", 28, 0.5, project="p", usd_per_gb_hour=0.01)
    tl.apportion_standby("pool-a", 1.0, ["T1", "T2"], project="p", period="d1")
    out = tl.task_cost("T1")
    assert out["components"] == {
        "model_call": pytest.approx(0.05),
        "tool_call": pytest.approx(0.001),
        "sandbox_seconds": pytest.approx(0.012),
        "idle_state_gb_hours": pytest.approx(0.14),
        "hot_pool_standby": pytest.approx(0.5),
    }
    assert out["total_usd"] == pytest.approx(sum(out["components"].values()))
    assert out["total_usd"] == pytest.approx(sum(r["cost_usd"] for r in out["rows"]))
    assert out["complete"] is True and out["unmetered"] == []


def test_unmetered_components_are_named_and_the_total_is_not_complete():
    tl.record_entry("T1", "model_call", 0.02, project="p")
    out = tl.task_cost("T1")
    assert out["complete"] is False
    assert set(out["unmetered"]) == {
        "tool_call",
        "sandbox_seconds",
        "idle_state_gb_hours",
        "hot_pool_standby",
    }
    assert tl.task_cost("nothing")["complete"] is False


def test_every_entry_needs_an_owning_project():
    with pytest.raises(tl.TaskLedgerError, match="owned by a project"):
        tl.record_entry("T1", "model_call", 0.1, project="")
    with pytest.raises(tl.TaskLedgerError, match="owned by a project"):
        tl.apportion_standby("pool", 1.0, ["T1"], project=" ")


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"component": "gpu_magic", "cost_usd": 1.0}, "unknown component"),
        ({"component": "model_call", "cost_usd": -1.0}, "cost_usd"),
        ({"component": "model_call", "cost_usd": float("nan")}, "cost_usd"),
        ({"component": "model_call", "cost_usd": 1.0, "quantity": -2}, "quantity"),
    ],
)
def test_invalid_entries_are_refused(kw, needle):
    with pytest.raises(tl.TaskLedgerError, match=needle):
        tl.record_entry("T1", project="p", **kw)


def test_sandbox_and_idle_never_assume_a_zero_rate(monkeypatch):
    with pytest.raises(tl.TaskLedgerError, match="EXAMLOPS_SANDBOX_USD_PER_SECOND"):
        tl.record_sandbox("T1", 10, project="p")
    with pytest.raises(tl.TaskLedgerError, match="EXAMLOPS_IDLE_USD_PER_GB_HOUR"):
        tl.record_idle_state("T1", 1, 1, project="p")
    monkeypatch.setenv("EXAMLOPS_SANDBOX_USD_PER_SECOND", "0.5")
    assert tl.record_sandbox("T1", 10, project="p")["cost_usd"] == 5.0
    monkeypatch.setenv("EXAMLOPS_IDLE_USD_PER_GB_HOUR", "abc")
    with pytest.raises(tl.TaskLedgerError, match="not a number"):
        tl.record_idle_state("T1", 1, 1, project="p")


def test_an_entry_id_makes_retried_writes_idempotent():
    a = tl.record_entry("T1", "model_call", 0.2, project="p", entry_id="call-1")
    b = tl.record_entry("T1", "model_call", 0.2, project="p", entry_id="call-1")
    assert a["created"] is True and b["created"] is False
    assert tl.task_cost("T1")["total_usd"] == pytest.approx(0.2)


def test_standby_shares_sum_exactly_and_record_the_rule():
    res = tl.apportion_standby(
        "pool-a", 1.0, {"A": 1, "B": 1, "C": 1}, project="p", rule="weighted", period="d1"
    )
    assert sum(res["shares"].values()) == pytest.approx(1.0, abs=1e-9)
    assert sorted(res["shares"].values()) == [0.333333, 0.333333, 0.333334]
    row = tl.task_cost("A")["rows"][0]
    assert row["method"] == "hot_pool_standby:weighted:pool-a"
    details = _audit("hot_pool_standby_apportioned")
    assert details == [
        {"rule": "weighted", "pool_cost_usd": 1.0, "tasks": 3, "period": "d1", "project": "p"}
    ]


def test_weighted_rule_follows_the_weights_and_equal_ignores_them():
    w = tl.apportion_standby("p1", 10.0, {"A": 3, "B": 1}, project="p", rule="weighted")
    assert w["shares"] == {"A": 7.5, "B": 2.5}
    e = tl.apportion_standby("p2", 10.0, {"A": 3, "B": 1}, project="p", rule="equal")
    assert e["shares"] == {"A": 5.0, "B": 5.0}


def test_reapportioning_the_same_period_records_nothing_new():
    tl.apportion_standby("pool", 2.0, ["A", "B"], project="p", period="2026-09-25")
    again = tl.apportion_standby("pool", 2.0, ["A", "B"], project="p", period="2026-09-25")
    assert again["created"] == 0
    assert tl.task_cost("A")["total_usd"] == pytest.approx(1.0)
    assert len(_audit("hot_pool_standby_apportioned")) == 1


def test_reapportioning_a_period_with_a_different_split_is_refused_not_half_applied():
    # A second call for the same (pool, period) with an extra task and a new cost used to skip
    # A and B (their entry ids existed) and insert C alone: 1+1+0.67 billed for a 2.0 pool.
    tl.apportion_standby("pool", 2.0, ["A", "B"], project="p", period="2026-09-25")
    with pytest.raises(tl.TaskLedgerError, match="already apportioned"):
        tl.apportion_standby("pool", 2.0, ["A", "B", "C"], project="p", period="2026-09-25")
    with pytest.raises(tl.TaskLedgerError, match="already apportioned"):
        tl.apportion_standby("pool", 3.0, ["A", "B"], project="p", period="2026-09-25")
    assert tl.task_cost("C")["entries"] == 0
    assert tl.task_cost("A")["total_usd"] + tl.task_cost("B")["total_usd"] == pytest.approx(2.0)
    # a different period is a different apportioning
    tl.apportion_standby("pool", 2.0, ["A", "B", "C"], project="p", period="2026-09-26")
    assert tl.task_cost("C")["entries"] == 1


def test_an_entry_id_is_scoped_to_its_tenant():
    # entry_id was globally UNIQUE: one tenant's id silently swallowed another tenant's write.
    tl.record_entry("T1", "model_call", 1.0, project="p", tenant="t1", entry_id="call-1")
    b = tl.record_entry("T1", "model_call", 5.0, project="p", tenant="t2", entry_id="call-1")
    assert b["created"] is True
    assert tl.task_cost("T1", tenant="t2")["total_usd"] == 5.0


def test_the_total_covers_every_entry_even_past_the_listed_rows(monkeypatch):
    from examlops.data import task_costs

    monkeypatch.setattr(task_costs, "MAX_ROWS", 3)
    for i in range(5):
        tl.record_entry("T9", "model_call", 1.0, project="p", entry_id=f"c{i}")
    res = tl.task_cost("T9")
    assert res["total_usd"] == 5.0 and res["entries"] == 5
    assert len(res["rows"]) == 3 and res["rows_truncated"] is True


def test_an_unreadable_ledger_is_reported_not_shown_as_absent(monkeypatch):
    from examlops.data import task_costs

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(task_costs, "window_totals", boom)
    agentic = eco.economics("agentic")["kinds"][0]
    assert agentic["task_ledger"] == {"error": "RuntimeError: disk gone"}


def test_a_lost_apportioning_audit_is_counted_and_the_split_stands(monkeypatch):
    from examlops.data import audit

    audit.reset_dropped_audit_events()

    def boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "write_audit_event", boom)
    res = tl.apportion_standby("pool", 2.0, ["A", "B"], project="p", period="d")
    assert res["created"] == 2  # the apportioning is not undone by the lost record
    assert audit.dropped_audit_events() == {"hot_pool_standby_apportioned": 1}
    audit.reset_dropped_audit_events()


def test_standby_with_no_tasks_is_refused_rather_than_left_unowned():
    with pytest.raises(tl.TaskLedgerError, match="unowned"):
        tl.apportion_standby("pool", 1.0, [], project="p")
    with pytest.raises(tl.TaskLedgerError, match="rule"):
        tl.apportion_standby("pool", 1.0, ["A"], project="p", rule="vibes")
    with pytest.raises(tl.TaskLedgerError, match="not all zero"):
        tl.apportion_standby("pool", 1.0, {"A": 0}, project="p", rule="weighted")


def test_tasks_are_tenant_scoped():
    tl.record_entry("T1", "model_call", 1.0, project="p", tenant="t1")
    tl.record_entry("T1", "model_call", 5.0, project="p", tenant="t2")
    assert tl.task_cost("T1", tenant="t1")["total_usd"] == 1.0
    assert tl.task_cost("T1", tenant="t2")["total_usd"] == 5.0


def test_economics_reports_the_ledger_beside_the_lower_bound():
    tl.record_sandbox("T1", 100, project="p", usd_per_second=0.01)
    agentic = next(k for k in eco.economics("agentic")["kinds"])
    assert agentic["task_ledger"]["tasks"] == 1
    assert agentic["task_ledger"]["components"]["sandbox_seconds"]["cost_usd"] == 1.0
    assert agentic["cost_bound"] == "lower"  # never silently merged into the session figure


def test_cli_record_apportion_show():
    r = runner.invoke(
        app,
        [
            "--json",
            "finops",
            "task-cost",
            "record",
            "T1",
            "--project",
            "p",
            "--component",
            "model_call",
            "--cost",
            "0.25",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "--json",
            "finops",
            "task-cost",
            "apportion",
            "pool",
            "--cost",
            "1",
            "--project",
            "p",
            "--task",
            "T1=3",
            "--task",
            "T2=1",
            "--rule",
            "weighted",
        ],
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["shares"] == {"T1": 0.75, "T2": 0.25}
    r = runner.invoke(app, ["--json", "finops", "task-cost", "show", "T1"])
    out = json.loads(r.output)
    assert out["total_usd"] == pytest.approx(1.0)
    r = runner.invoke(
        app,
        [
            "--json",
            "finops",
            "task-cost",
            "record",
            "T1",
            "--project",
            "p",
            "--component",
            "sandbox_seconds",
            "--quantity",
            "5",
        ],
    )
    assert r.exit_code == 1 and "EXAMLOPS_SANDBOX_USD_PER_SECOND" in r.output
    r = runner.invoke(
        app,
        [
            "--json",
            "finops",
            "task-cost",
            "record",
            "T1",
            "--project",
            "p",
            "--component",
            "hot_pool_standby",
            "--cost",
            "1",
        ],
    )
    assert r.exit_code == 1 and "apportion" in r.output

"""ADR 0035 clause 3 — a lost reasoning-budget audit event is counted, and the cap still lands.

A budget change is a governed mutation: D5 decides it and it is audited. When the audit log is
down the change must still apply (fail open) and the loss must reach `dropped_audit_events()`.
Registered in `tests/unit/test_audit_losses_are_recorded.py::COVERED_AUDIT_SITES`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db
    from examlops.data.audit import reset_dropped_audit_events

    platform_db.init_db()
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_budget_audit_is_counted_and_the_cap_is_still_set_and_removed(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.data import reasoning_budgets as store
    from examlops.data.audit import dropped_audit_events

    _break_the_audit_log(monkeypatch)
    runner = CliRunner()

    out = runner.invoke(app, ["--json", "gateway", "reasoning", "set-budget", "50", "--model", "m"])
    assert out.exit_code == 0, out.output
    assert [b["max_thinking_tokens"] for b in store.list_budgets()] == [50]
    assert dropped_audit_events().get("reasoning_budget_set") == 1, dropped_audit_events()

    out = runner.invoke(
        app, ["--json", "gateway", "reasoning", "set-budget", "0", "--model", "m", "--remove"]
    )
    assert out.exit_code == 0, out.output
    assert store.list_budgets() == []
    assert dropped_audit_events().get("reasoning_budget_removed") == 1, dropped_audit_events()

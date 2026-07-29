"""N5 — `--reason` provenance + early access-scope hint on mutating commands.

Covers the shared `examlops.cli._provenance` helpers and their wiring into a representative
mutating command (`exa drift baseline`), proving a `--reason` reaches the audit trail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    from examlops import platform_db

    platform_db.init_db()
    yield


# ------------------------------------------------------------------ unit: helpers


def test_audit_details_folds_reason():
    from examlops.cli._provenance import audit_details

    base = {"cleared": 5}
    assert audit_details(base, "spring cleanup") == {"cleared": 5, "reason": "spring cleanup"}
    # falsy reason → dict unchanged (backward-compatible payload)
    assert audit_details(base, None) == {"cleared": 5}
    assert audit_details(base, "") == {"cleared": 5}
    # original is not mutated
    assert base == {"cleared": 5}


def test_reason_option_is_a_reason_flag():
    from examlops.cli._provenance import reason_option

    opt = reason_option()
    assert "--reason" in opt.param_decls
    assert opt.default is None


def test_scope_hint_only_warns_when_missing(capsys):
    from examlops.cli._provenance import scope_hint

    scope_hint("a CONTROL_PLANE_TOKEN", present=False)
    out_missing = capsys.readouterr().out
    assert "CONTROL_PLANE_TOKEN" in out_missing

    scope_hint("a CONTROL_PLANE_TOKEN", present=True)
    out_present = capsys.readouterr().out
    assert out_present.strip() == ""  # no noise when the credential is configured


# --------------------------------------------------- integration: reason → audit


def _seed_snapshots(model: str, n: int = 12) -> None:
    from examlops.data.drift import write_drift_snapshot

    for i in range(n):
        write_drift_snapshot(model, "Production", float(i), job_id=None)


def test_drift_baseline_records_reason_in_audit():
    from examlops.cli.commands import drift
    from examlops.data.audit import export_audit_events

    _seed_snapshots("JPCP")
    res = runner.invoke(drift.app, ["baseline", "JPCP", "--reason", "quarterly recalibration"])
    assert res.exit_code == 0, res.output

    events = [e for e in export_audit_events() if e["action"] == "drift_baseline_set"]
    assert len(events) == 1
    details = json.loads(events[0]["details"])
    assert details["reason"] == "quarterly recalibration"


def test_drift_baseline_without_reason_omits_key():
    from examlops.cli.commands import drift
    from examlops.data.audit import export_audit_events

    _seed_snapshots("JPCP")
    res = runner.invoke(drift.app, ["baseline", "JPCP"])
    assert res.exit_code == 0, res.output

    events = [e for e in export_audit_events() if e["action"] == "drift_baseline_set"]
    details = json.loads(events[0]["details"])
    assert "reason" not in details  # backward-compatible: no reason key when none given


def test_drift_reset_records_reason():
    from examlops.cli.commands import drift
    from examlops.data.audit import export_audit_events

    _seed_snapshots("JPCP")
    res = runner.invoke(drift.app, ["reset", "JPCP", "--reason", "bad ingest"], input="y\n")
    assert res.exit_code == 0, res.output

    events = [e for e in export_audit_events() if e["action"] == "drift_reset"]
    assert len(events) == 1
    details = json.loads(events[0]["details"])
    assert details["reason"] == "bad ingest"
    assert details["cleared"] == 12

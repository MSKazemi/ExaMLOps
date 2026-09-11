"""ADR 0008 clause 4 — the eval gate's report on the dashboard, and an alert when it fails.

The recorded finding: clause 4 had "no Promotion page or gate-report route". The panel that did
exist reported `"eval": {"pass": policy_allow}` — the eval gate shown as passed whenever a
*promotion policy* existed, whatever the gate had found. It now reads the `gate_reports` rows that
`run_eval_gate` persists, and a failed gate raises an alert.
"""

import alerts
import dbconn
import mlops
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.execute(
        "INSERT INTO promotion_rules (model, metric, operator, threshold, from_alias, "
        "to_alias, enabled) VALUES (?,?,?,?,?,?,?)",
        ("jpcp", "rmse", "<", 5.0, "Staging", "Production", 1),
    )
    conn.commit()
    conn.close()
    return str(db)


def _gate(mode="block"):
    from examlops.data.evaluation import set_eval_gate

    set_eval_gate("jpcp", "jpcp-suite", [{"name": "accuracy", "min": 0.8}], mode=mode)


def _report(passed: bool, mode="block", candidate="18"):
    from examlops.data.evaluation import record_gate_report

    metric = {
        "name": "accuracy",
        "candidate": 0.91 if passed else 0.62,
        "baseline": 0.9,
        "delta": 0.01 if passed else -0.28,
        "min": 0.8,
        "max_drop": None,
        "failed": not passed,
        "reason": "" if passed else "below floor 0.8",
    }
    report = {
        "passed": passed,
        "mode": mode,
        "aggregate": "all",
        "metrics": [metric],
        "judge": None,
        "judge_eligible": True,
        "judge_failures": [],
        "calibration_id": None,
    }
    record_gate_report("jpcp", passed, mode, report, candidate=candidate, baseline="Production")


# ── the promotion check tells the truth about the eval gate ──────────────────


def test_a_failed_gate_is_no_longer_reported_as_passed(platform_db):
    """The regression: with an enabled policy the panel said eval pass=True regardless."""
    _gate()
    _report(passed=False)

    chk = mlops.promotion_check(platform_db, "JPCP")

    assert chk["eval"]["pass"] is False and chk["eval"]["state"] == "failed"
    assert chk["allowed"] is False
    assert any("eval gate failed" in r and "accuracy" in r for r in chk["policy"]["reasons"])
    assert chk["eval"]["lastReport"]["candidate"] == "18"


def test_a_passing_gate_allows(platform_db):
    _gate()
    _report(passed=True)

    chk = mlops.promotion_check(platform_db, "jpcp")

    assert chk["eval"]["state"] == "passed" and chk["allowed"] is True
    assert chk["eval"]["metrics"][0]["name"] == "accuracy"


def test_a_warn_mode_failure_is_shown_but_not_blocking(platform_db):
    _gate(mode="warn")
    _report(passed=False, mode="warn")

    chk = mlops.promotion_check(platform_db, "jpcp")

    assert chk["eval"]["state"] == "warned" and chk["eval"]["pass"] is False
    assert chk["allowed"] is True


def test_a_configured_gate_that_never_ran_is_not_a_pass(platform_db):
    _gate()

    chk = mlops.promotion_check(platform_db, "jpcp")

    assert chk["eval"]["state"] == "not_run" and chk["eval"]["pass"] is None
    assert chk["allowed"] is False
    assert any("has not run yet" in r for r in chk["policy"]["reasons"])


def test_no_gate_means_not_eval_gated(platform_db):
    chk = mlops.promotion_check(platform_db, "jpcp")

    assert chk["eval"]["state"] == "no_gate" and chk["eval"]["pass"] is None
    assert chk["allowed"] is True


def test_the_latest_report_decides(platform_db):
    _gate()
    _report(passed=False, candidate="18")
    _report(passed=True, candidate="19")

    chk = mlops.promotion_check(platform_db, "jpcp")

    assert chk["eval"]["state"] == "passed" and chk["eval"]["lastReport"]["candidate"] == "19"


# ── the gate-report route ────────────────────────────────────────────────────


async def test_gate_reports_route_lists_newest_first(client, platform_db):
    _gate()
    _report(passed=False, candidate="18")
    _report(passed=True, candidate="19")
    assert (await client.get("/api/v1/mlops/gate-reports/JPCP")).status_code == 401

    token = (await client.post("/api/auth/login", json={"password": VIEWER_PW})).json()["token"]
    r = await client.get(
        "/api/v1/mlops/gate-reports/JPCP", headers={"Authorization": f"Bearer {token}"}
    )

    assert r.status_code == 200, r.text
    reports = r.json()["reports"]
    assert [x["candidate"] for x in reports] == ["19", "18"]
    assert reports[1]["passed"] is False and reports[1]["metrics"][0]["failed"] is True


# ── clause 4's alert on failure ──────────────────────────────────────────────


def test_a_blocked_promotion_raises_an_error_alert(platform_db):
    _gate()
    _report(passed=False)

    inbox = alerts.active_alerts(platform_db)

    gate = [a for a in inbox["alerts"] if a["source"] == "gate"]
    assert len(gate) == 1 and gate[0]["severity"] == "error"
    assert "Promotion blocked" in gate[0]["title"] and "accuracy" in gate[0]["title"]


def test_a_warn_mode_failure_is_a_warning(platform_db):
    _gate(mode="warn")
    _report(passed=False, mode="warn")

    gate = [a for a in alerts.active_alerts(platform_db)["alerts"] if a["source"] == "gate"]

    assert gate and gate[0]["severity"] == "warn"


def test_a_later_pass_clears_the_alert(platform_db):
    _gate()
    _report(passed=False, candidate="18")
    _report(passed=True, candidate="19")

    assert not [a for a in alerts.active_alerts(platform_db)["alerts"] if a["source"] == "gate"]

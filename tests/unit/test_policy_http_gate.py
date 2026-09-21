"""``examlops.policy.http_gate`` — the one HTTP-side policy verdict shared by the dashboard and the
control plane (ADR 0079 d2, ADR 0029 d3), and the CLI's acknowledgement of a human's approval.

The route-level behaviour is tested where the routes live (``platform/services/dashboard/backend/
tests/test_policy_gate.py``, ``platform/services/control_plane/tests/test_policy_gate.py``).
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops import policy  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.policy import http_gate  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setattr(policy, "POLICY_YAML", tmp_path / "policy.yaml")
    return tmp_path


def _rules(tmp_path, text):
    (tmp_path / "policy.yaml").write_text(text)


def _audit_actions(tmp_path):
    import sqlite3

    from examlops.platform_db import get_db

    try:
        with get_db() as conn:
            return [r[0] for r in conn.execute("SELECT action FROM audit_events")]
    except sqlite3.OperationalError:
        return []


def test_no_policy_is_ok_and_writes_no_audit_row(_isolated):
    v = http_gate.evaluate("retrain", {"model": "M"}, actor="a", source="t")
    assert v.ok and v.rule is None
    assert _audit_actions(_isolated) == []


def test_deny_names_the_rule_and_audits_under_the_callers_identity(_isolated):
    _rules(_isolated, "policies:\n  - name: r1\n    action: x\n    effect: deny\n")
    v = http_gate.evaluate("x", {"target": "T"}, actor="alice", source="dashboard-policy")
    assert v.status == 403 and "r1" in v.detail and v.rule == "r1"
    from examlops.platform_db import get_db

    with get_db() as conn:
        row = tuple(
            conn.execute(
                "SELECT actor, source, target FROM audit_events WHERE action='policy:x'"
            ).fetchone()
        )
    assert row == ("alice", "dashboard-policy", "T")


def test_require_approval_needs_the_ack_and_an_entitled_approver(_isolated):
    _rules(_isolated, "policies:\n  - name: r2\n    action: x\n    effect: require_approval\n")
    assert http_gate.evaluate("x", {}, actor="a", source="t").status == 409
    # An ack from someone who may not approve is still refused.
    assert (
        http_gate.evaluate("x", {}, actor="a", source="t", approved=True, approver_ok=False).status
        == 409
    )
    ok = http_gate.evaluate("x", {}, actor="a", source="t", approved=True)
    assert ok.ok and ok.effect == "require_approval"
    assert "policy_approval:x" in _audit_actions(_isolated)


def test_monitor_never_blocks(_isolated):
    _rules(
        _isolated, "policies:\n  - name: m\n    action: x\n    effect: deny\n    mode: monitor\n"
    )
    assert http_gate.evaluate("x", {}, actor="a", source="t").ok
    assert "policy_monitor:x" in _audit_actions(_isolated)


def test_engine_failure_denies(_isolated, monkeypatch):
    monkeypatch.setattr(policy, "decide", MagicMock(side_effect=RuntimeError("bug")))
    v = http_gate.evaluate("x", {}, actor="a", source="t")
    assert v.status == 403 and "unavailable" in v.detail
    assert "policy_unavailable:x" in _audit_actions(_isolated)


@pytest.mark.parametrize(
    "value,expected", [("true", True), ("1", True), ("no", False), (None, False)]
)
def test_header_parsing(value, expected):
    assert http_gate.header_asserts_approval(value) is expected


def test_client_sends_the_ack_only_inside_the_acknowledged_block():
    from examlops.cli import _client

    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(req.get_header("X-policy-approved"))
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = b"{}"
        return resp

    with patch.object(urllib.request, "urlopen", fake_urlopen):
        _client.post("http://cp.invalid/v1/retrain", {})
        with http_gate.approval_acknowledged():
            _client.post("http://cp.invalid/v1/retrain", {})
        _client.post("http://cp.invalid/v1/retrain", {})
    assert seen == [None, "true", None]


def test_exa_retrain_forwards_the_humans_approval_to_the_control_plane(_isolated):
    """A `require_approval` rule the operator confirmed at the prompt must not be asked for twice."""
    _rules(
        _isolated, "policies:\n  - name: r3\n    action: retrain\n    effect: require_approval\n"
    )
    fake = {"command_id": "c", "state": "succeeded", "result": {"flow_run_id": "f"}}
    flags = []

    def post(*a, **k):
        flags.append(http_gate.approval_ack_active())
        return fake

    with patch("examlops.cli.commands.retrain._client.post", side_effect=post):
        r = runner.invoke(app, ["--yes", "retrain", "JPCP", "--dataset", "PM100Dataset", "--async"])
    assert r.exit_code == 0, r.output
    assert flags == [True]
    # And without a require_approval rule nothing is acknowledged (byte-identical default).
    (_isolated / "policy.yaml").write_text("")
    flags.clear()
    with patch("examlops.cli.commands.retrain._client.post", side_effect=post):
        runner.invoke(app, ["--yes", "retrain", "JPCP", "--dataset", "PM100Dataset", "--async"])
    assert flags == [False]


def test_a_lost_policy_audit_is_counted_and_the_verdict_stands(_isolated, monkeypatch):
    """The audit write is best-effort: a lost row never changes the answer, and it is counted."""
    from examlops.data import audit

    _rules(_isolated, "policies:\n  - name: r9\n    action: x\n    effect: deny\n")
    audit.reset_dropped_audit_events()
    monkeypatch.setattr(audit, "write_audit_event", MagicMock(side_effect=OSError("disk")))
    try:
        v = http_gate.evaluate("x", {}, actor="a", source="t")
        assert v.status == 403 and "r9" in v.detail
        assert audit.dropped_audit_events().get("policy:x") == 1
    finally:
        audit.reset_dropped_audit_events()

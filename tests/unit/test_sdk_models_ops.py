"""ADR 0078 clause 1 — the SDK's mutating operations carry the CLI's discipline.

``retrain``/``approve``/``promote``: a dry run changes nothing; anything else needs
``confirm=True``; the ADR 0079 policy gate is consulted (deny ⇒ refused, require_approval ⇒ needs
``approved=True``); a completed mutation is audited. The control plane is faked at the
``retrain_command``/``control_plane_api`` seam; ``promote`` is faked at its CLI-delegation seam,
plus one real child process.
"""

from __future__ import annotations

import subprocess

import pytest

from examlops import policy, retrain_command
from examlops.sdk import _cli_delegate, models
from examlops.sdk import audit as sdk_audit
from examlops.sdk.errors import (
    ApprovalRequiredError,
    ConfirmationRequiredError,
    GateRefusedError,
    InvalidArgumentError,
    NotFoundError,
    PolicyDeniedError,
    SDKError,
    UnavailableError,
)


@pytest.fixture
def no_policy(monkeypatch):
    monkeypatch.setattr(policy, "_load_policies", lambda *a, **k: [])


def _rules(monkeypatch, *rules):
    monkeypatch.setattr(policy, "_load_policies", lambda *a, **k: list(rules))


@pytest.fixture
def submitted(monkeypatch):
    """Fake the control plane's command API; record what reached it."""
    calls: list[dict] = []

    def submit(body, **kw):
        from examlops.policy import http_gate

        calls.append({"body": body, "ack": http_gate.approval_ack_active(), **kw})
        return {"flow_run_id": "fr-1", "command_id": "c-1", "state": "succeeded"}

    monkeypatch.setattr(retrain_command, "submit", submit)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "unit-test-token-value")
    return calls


def _events(action: str) -> list:
    return [e for e in sdk_audit.query(action=action, limit=50)]


# ── retrain ─────────────────────────────────────────────────────────────────────────────────────
def test_retrain_dry_run_changes_nothing(submitted, no_policy):
    out = models.retrain("JPCP", "PM100Dataset", dry_run=True)
    assert out.dry_run is True and out.raw["dataset_name"] == "PM100Dataset"
    assert submitted == []
    assert _events("retrain_triggered") == []


def test_retrain_without_confirm_is_refused_before_anything_happens(submitted, no_policy):
    with pytest.raises(ConfirmationRequiredError):
        models.retrain("JPCP", "PM100Dataset")
    assert submitted == []


def test_retrain_schedules_audits_and_reports_dispatch(submitted, no_policy):
    out = models.retrain("JPCP", "PM100Dataset", dummy=True, confirm=True, reason="drift")
    assert out.dispatched is True and out.flow_run_id == "fr-1" and out.command_id == "c-1"
    assert submitted[0]["body"] == {
        "model_name": "JPCP",
        "dataset_name": "PM100Dataset",
        "is_dummy": True,
        "backend_name": None,
    }
    assert out.audited is True
    [event] = _events("retrain_triggered")
    assert event.source == "sdk" and event.target == "JPCP"
    assert event.details["flow_run_id"] == "fr-1" and event.details["reason"] == "drift"


def test_retrain_queued_but_not_dispatched_is_not_an_error(monkeypatch, no_policy):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "unit-test-token-value")
    monkeypatch.setattr(
        retrain_command, "submit", lambda body, **kw: {"command_id": "c-2", "state": "pending"}
    )
    out = models.retrain("JPCP", "PM100Dataset", confirm=True)
    assert out.dispatched is False and out.state == "pending" and out.command_id == "c-2"


def test_retrain_policy_deny_refuses(monkeypatch, submitted):
    _rules(monkeypatch, {"name": "freeze", "action": "retrain", "effect": "deny"})
    with pytest.raises(PolicyDeniedError, match="freeze"):
        models.retrain("JPCP", "PM100Dataset", confirm=True)
    assert submitted == []
    assert _events("retrain_triggered") == []


def test_retrain_require_approval_needs_an_explicit_approval(monkeypatch, submitted):
    _rules(monkeypatch, {"name": "four-eyes", "action": "retrain", "effect": "require_approval"})
    with pytest.raises(ApprovalRequiredError):
        models.retrain("JPCP", "PM100Dataset", confirm=True)
    assert submitted == []
    models.retrain("JPCP", "PM100Dataset", confirm=True, approved=True)
    # The control plane re-evaluates the same rule; the approval header travels with the call.
    assert submitted[0]["ack"] is True


def test_retrain_without_a_token_fails_closed(monkeypatch, no_policy):
    import dataclasses

    from examlops.cli import _config

    # Hermetic: a developer's `exa auth login` session would otherwise supply a token.
    real = _config.load_config
    monkeypatch.setattr(
        _config, "load_config", lambda: dataclasses.replace(real(), control_plane_token="")
    )
    called: list = []
    monkeypatch.setattr(retrain_command, "submit", lambda *a, **k: called.append(1))
    with pytest.raises(SDKError, match="CONTROL_PLANE_TOKEN"):
        models.retrain("JPCP", "PM100Dataset", confirm=True)
    assert called == []


def test_retrain_transport_failure_is_typed(monkeypatch, no_policy):
    from examlops.cli._client import ClientError

    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "unit-test-token-value")

    def down(*a, **k):
        raise ClientError("Connection refused")

    monkeypatch.setattr(retrain_command, "submit", down)
    with pytest.raises(UnavailableError):
        models.retrain("JPCP", "PM100Dataset", confirm=True)
    assert _events("retrain_triggered") == [], "a retrain that never reached the plane is unaudited"


def test_retrain_rejects_an_option_shaped_name(submitted, no_policy):
    with pytest.raises(InvalidArgumentError):
        models.retrain("--help", "PM100Dataset", confirm=True)


def test_a_lost_sdk_retrain_audit_is_counted(monkeypatch, submitted, no_policy):
    """The retrain stands (it reached the control plane); the lost record is counted."""
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    out = models.retrain("JPCP", "PM100Dataset", confirm=True)
    assert out.flow_run_id == "fr-1" and out.audited is False
    assert "retrain_triggered" in dropped_audit_events(), dropped_audit_events()


# ── approve ─────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def approvals(monkeypatch):
    from examlops import control_plane_api

    calls: list[str] = []

    def approve(model_id, **kw):
        calls.append(model_id)
        return {"flow_run_id": "fr-9", "status": "scheduled"}

    monkeypatch.setattr(control_plane_api, "approve", approve)
    return calls


def test_approve_requires_confirm_and_audits(approvals, no_policy):
    with pytest.raises(ConfirmationRequiredError):
        models.approve("JPCP")
    assert approvals == []
    assert models.approve("JPCP", dry_run=True).dry_run is True
    assert approvals == []
    out = models.approve("JPCP", confirm=True)
    assert approvals == ["JPCP"] and out.flow_run_id == "fr-9" and out.audited
    [event] = _events("model_approved")
    assert event.source == "sdk" and event.target == "JPCP"
    # No rule applied, so no policy decision was written: the trail is what it was before.
    assert _events("policy:model_approve") == []


def test_approve_policy_deny_and_require_approval(monkeypatch, approvals):
    _rules(monkeypatch, {"name": "no-approvals", "action": "model_approve", "effect": "deny"})
    with pytest.raises(PolicyDeniedError):
        models.approve("JPCP", confirm=True)
    _rules(monkeypatch, {"name": "2p", "action": "model_approve", "effect": "require_approval"})
    with pytest.raises(ApprovalRequiredError):
        models.approve("JPCP", confirm=True)
    assert approvals == []
    models.approve("JPCP", confirm=True, approved=True)
    assert approvals == ["JPCP"]


def test_cli_approve_goes_through_the_sdk_and_its_policy_gate(monkeypatch, approvals):
    """Clause 2: `exa approvals approve` gained the policy gate by calling the SDK."""
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _rules(monkeypatch, {"name": "no-approvals", "action": "model_approve", "effect": "deny"})
    result = CliRunner().invoke(app, ["--yes", "approvals", "approve", "JPCP"])
    assert result.exit_code == 1, result.output
    assert "no-approvals" in result.output
    assert approvals == []


def test_cli_approve_under_a_require_approval_rule_defaults_to_no(monkeypatch, approvals):
    """A bare Enter must not be the approval a require_approval rule asks for.

    The prompt names the rule and defaults to no, as `exa retrain`'s does; only an explicit "y"
    approves. Before, the prompt defaulted to yes and never mentioned the rule.
    """
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _rules(monkeypatch, {"name": "2p", "action": "model_approve", "effect": "require_approval"})
    result = CliRunner().invoke(app, ["approvals", "approve", "JPCP"], input="\n")
    assert result.exit_code == 0, result.output
    assert "policy requires approval" in result.output
    assert approvals == []
    result = CliRunner().invoke(app, ["approvals", "approve", "JPCP"], input="y\n")
    assert result.exit_code == 0, result.output
    assert approvals == ["JPCP"]


def test_cli_approve_with_no_rule_keeps_its_default_yes_prompt(approvals, no_policy):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    result = CliRunner().invoke(app, ["approvals", "approve", "JPCP"], input="\n")
    assert result.exit_code == 0, result.output
    assert "policy requires approval" not in result.output
    assert approvals == ["JPCP"]


# ── promote ─────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def delegated(monkeypatch):
    calls: list[list[str]] = []
    reply: dict = {"outcome": _cli_delegate.CliOutcome(0, {"ok": True, "message": ""})}

    def run_cli(argv, *, timeout):
        calls.append(list(argv))
        return reply["outcome"]

    monkeypatch.setattr(_cli_delegate, "run_cli", run_cli)
    return calls, reply


def _promote(**kw):
    args = {"metric": "rmse", "operator": "lt", "threshold": 5.0}
    args.update(kw)
    return models.promote("jpcp", **args)


def test_promote_runs_the_governed_cli_path(delegated, no_policy):
    calls, reply = delegated
    reply["outcome"] = _cli_delegate.CliOutcome(
        0,
        {"ok": True, "message": "Promoted jpcp v3 → Production  (rmse=4.2000  <5.0)"},
        ["SLO gate could not evaluate"],
    )
    out = _promote(confirm=True)
    assert out.promoted is True and out.warnings == ["SLO gate could not evaluate"]
    assert calls == [
        [
            "pipeline",
            "promote",
            "jpcp",
            "--from",
            "Staging",
            "--to",
            "Production",
            "--if-rmse-lt",
            "5.0",
        ]
    ]


def test_promote_threshold_not_met_is_not_promoted(delegated, no_policy):
    _, reply = delegated
    reply["outcome"] = _cli_delegate.CliOutcome(
        0, {"ok": True, "message": "Not promoted: jpcp v3: rmse=6.0000  <5.0  (threshold not met)"}
    )
    assert _promote(confirm=True).promoted is False


def test_promote_dry_run_needs_no_confirm_and_never_promotes(delegated, no_policy):
    calls, reply = delegated
    reply["outcome"] = _cli_delegate.CliOutcome(
        0, {"ok": True, "message": "[DRY RUN] jpcp v3: rmse=4.2  <5.0  → WOULD promote"}
    )
    out = _promote(dry_run=True)
    assert out.promoted is False and out.dry_run is True
    assert "--dry-run" in calls[0]


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("Policy denied promotion of jpcp v3 to Production: rule x", PolicyDeniedError),
        ("Eval gate FAILED for jpcp v3: acc. Use --force to override (audited).", GateRefusedError),
        ("No version found under alias 'Staging' for jpcp", NotFoundError),
        ("Failed to fetch model jpcp from MLflow: Connection refused", UnavailableError),
    ],
)
def test_promote_refusals_are_typed(delegated, no_policy, message, error):
    _, reply = delegated
    reply["outcome"] = _cli_delegate.CliOutcome(1, {"error": message, "exit_code": 1})
    with pytest.raises(error) as info:
        _promote(confirm=True)
    assert str(info.value) == message


def test_promote_without_confirm_or_with_a_pending_approval_rule_never_runs(monkeypatch, delegated):
    calls, _ = delegated
    monkeypatch.setattr(policy, "_load_policies", lambda *a, **k: [])
    with pytest.raises(ConfirmationRequiredError):
        _promote()
    _rules(
        monkeypatch,
        {"name": "2p", "action": "manual_promote", "effect": "require_approval"},
    )
    with pytest.raises(ApprovalRequiredError):
        _promote(confirm=True)
    assert calls == []
    _promote(confirm=True, approved=True)
    assert len(calls) == 1


def test_a_monitor_mode_approval_rule_does_not_block(monkeypatch, delegated):
    calls, _ = delegated
    _rules(
        monkeypatch,
        {"action": "manual_promote", "effect": "require_approval", "mode": "monitor"},
    )
    _promote(confirm=True)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"metric": "--rmse"},
        {"metric": "rmse x"},
        {"operator": "eq"},
        {"threshold": float("nan")},
        {"threshold": "abc"},
        {"from_alias": "-x"},
        {"timeout": 0},
    ],
)
def test_promote_rejects_injection_and_nonsense_before_running(delegated, no_policy, kw):
    calls, _ = delegated
    with pytest.raises(InvalidArgumentError):
        _promote(confirm=True, **kw)
    assert calls == []


def test_run_cli_timeout_is_unavailable(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="exa", timeout=1)

    monkeypatch.setattr(_cli_delegate.subprocess, "run", slow)
    with pytest.raises(UnavailableError, match="timed out"):
        _cli_delegate.run_cli(["pipeline", "promote"], timeout=1)


def test_promote_really_runs_the_cli_in_a_child(monkeypatch, no_policy):
    """No fake: the child imports this very package and answers with its JSON error document."""
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")
    with pytest.raises(UnavailableError, match="Failed to fetch model jpcp"):
        _promote(dry_run=True, timeout=120)


def test_run_cli_reads_back_only_a_bounded_tail(monkeypatch):
    """A child that floods stdout must not be buffered whole in the SDK caller's memory."""
    monkeypatch.setattr(_cli_delegate, "MAX_OUTPUT_BYTES", 4096)
    read_sizes: list[int] = []
    real_tail = _cli_delegate._tail

    def spy_tail(fh):
        text = real_tail(fh)
        read_sizes.append(len(text))
        return text

    def flood(cmd, **kw):
        assert "capture_output" not in kw and kw["stdout"] is not subprocess.PIPE
        kw["stdout"].write(b"x" * 3_000_000 + b'\n{"ok": true, "message": "Promoted jpcp v3"}\n')
        kw["stderr"].write(b'{"warning": "w1"}\n')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_cli_delegate, "_tail", spy_tail)
    monkeypatch.setattr(_cli_delegate.subprocess, "run", flood)
    outcome = _cli_delegate.run_cli(["pipeline", "promote"], timeout=5)
    assert outcome.document == {"ok": True, "message": "Promoted jpcp v3"}
    assert outcome.warnings == ["w1"]
    assert max(read_sizes) <= 4096

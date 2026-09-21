"""A dropped audit event must be counted, not passed over — especially on governance paths.

`audit_best_effort` exists because the platform's two ways of trusting the audit log are both blind
to an event that never arrived: the hash chain proves **integrity, not completeness** (it is
computed over the rows that exist), and `check_art12_logging` asks only whether *at least one*
event of each required type exists. So it fails open — an audit outage must not block a secret
read or a policy decision — but records the loss at WARNING and counts it in
`dropped_audit_events()`, which the control plane publishes as `audit_events_dropped`.

A caller that writes the event itself inside `except Exception: pass` keeps the failing-open half
and throws away the recording half. The operation succeeds, the record is gone, the counter stays
at zero, and every completeness statement about that window is silently unsound.

These are the paths where that matters most: a secret access, a supply-chain signature, a policy
decision, a guardrail block, and the monitoring daemon's own alerts.
"""

from __future__ import annotations

import ast
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
    # Process-global, like the Prometheus registry: without this the count leaks between tests.
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


def _break_the_audit_log(monkeypatch):
    """Make the append fail the way a datastore outage would."""
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


@pytest.mark.parametrize(
    ("module", "call"),
    [
        ("examlops.secrets", lambda m: m._audit("alice", "secret_read", "db/password")),
        ("examlops.supplychain", lambda m: m._audit("model_signed", "JPCP", "7", "alice", {})),
    ],
)
def test_a_governance_audit_loss_is_counted(monkeypatch, module, call):
    """The operation still succeeds — and the platform can tell that a record is missing."""
    import importlib

    from examlops.data.audit import dropped_audit_events

    mod = importlib.import_module(module)
    _break_the_audit_log(monkeypatch)

    call(mod)  # fails open: no exception reaches the caller

    assert dropped_audit_events(), (
        f"{module} lost an audit event without counting it — `audit_events_dropped` stays at zero "
        "while the log is incomplete, and neither the hash chain nor the Art. 12 check can see it"
    )


def test_a_policy_decision_that_is_not_logged_is_counted(monkeypatch):
    from examlops.data.audit import dropped_audit_events
    from examlops.policy_engine import EngineDecision, PolicyInput, _audit

    _break_the_audit_log(monkeypatch)
    _audit(
        "denied",
        PolicyInput(subject="alice", action="promote", resource="JPCP", tenant="default"),
        EngineDecision(allow=False, reasons=["not permitted"], effect="deny", engine="yaml"),
    )
    assert "policy_denied" in dropped_audit_events(), (
        "a policy denial vanished from the audit log with nothing recording the loss"
    )


def test_a_policy_decision_from_the_other_policy_module_is_counted(monkeypatch):
    """There are two policy modules, and only one was converted first time round.

    `examlops.policy` and `examlops.policy_engine` both record a decision through a private
    `_audit`, in the same shape, about the same kind of evidence. Fixing the one whose name came
    up in the sweep and calling the class handled is how a near-twin survives a cleanup.
    """
    from examlops.data.audit import dropped_audit_events
    from examlops.policy import Decision, _audit

    _break_the_audit_log(monkeypatch)
    _audit(
        "promote",
        {"model": "JPCP"},
        Decision(effect="deny", rule="no-weekend-promotions", reason="blocked by policy"),
    )
    assert any(k.startswith("policy:") for k in dropped_audit_events()), dropped_audit_events()


def test_a_policy_unavailable_event_that_could_not_be_audited_is_counted(monkeypatch):
    """BL-080's fix (`policy.decide_safe`) records that the engine itself was unavailable — a
    NEW audit site, so it needs the same proof as any other: a loss there must still be counted,
    not silently vanish behind the very outage it exists to record."""
    from examlops.data.audit import dropped_audit_events
    from examlops.policy import decide_safe

    _break_the_audit_log(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr("examlops.policy.decide", boom)
    decide_safe("retrain", {"model": "JPCP"})
    assert any(k.startswith("policy_unavailable:") for k in dropped_audit_events()), (
        dropped_audit_events()
    )


def test_an_upgrade_that_could_not_be_audited_is_counted(monkeypatch):
    """`exa upgrade apply` migrates the instance's data format.

    Its comment argued the silence — "the upgrade row is the record" — and that is true: a
    `platform_upgrades` row is written by a different module on a different path. But a separate
    record does not make the audit log complete, and `dropped_audit_events()` stayed at zero, so
    nothing could tell that this window's log is missing an entry. Best-effort is the right
    behaviour; hiding it is the part that was not argued.
    """
    from examlops.data.audit import dropped_audit_events
    from examlops.lifecycle.upgrade import _audit

    _break_the_audit_log(monkeypatch)
    _audit("platform_upgraded", {"applied": ["0002_add_column"]})
    assert "platform_upgraded" in dropped_audit_events(), dropped_audit_events()


def test_a_guardrail_block_that_could_not_be_audited_is_counted(monkeypatch):
    """A block that happened and was not recorded reads, afterwards, like a block that never was.

    This path also used to share one `try` with the `guardrail_events` telemetry insert, so a
    failed insert skipped the audit write as well. The two are independent now: a counters table
    being unavailable is a different system having a different problem.
    """
    from examlops.data.audit import dropped_audit_events
    from examlops.guardrails import DefaultGuardrail

    _break_the_audit_log(monkeypatch)
    DefaultGuardrail(tenant="acme", mode="enforce")._record("input", "block", "prompt_injection")
    assert "guardrail_block" in dropped_audit_events(), dropped_audit_events()


def test_a_guardrail_block_is_audited_even_when_its_telemetry_insert_fails(monkeypatch):
    """The audit record must not depend on the telemetry table being writable."""
    import examlops.data as data_mod
    from examlops.data.audit import dropped_audit_events

    written: list = []
    monkeypatch.setattr(
        "examlops.data.audit.write_audit_event",
        lambda *a, **k: written.append(a[2] if len(a) > 2 else k.get("action")),
    )

    def no_db(*a, **k):
        raise RuntimeError("guardrail_events unavailable")

    monkeypatch.setattr(data_mod, "get_db", no_db)
    from examlops.guardrails import DefaultGuardrail

    DefaultGuardrail(tenant="acme", mode="enforce")._record("input", "block", "prompt_injection")
    assert written == ["guardrail_block"], written
    assert not dropped_audit_events(), "the audit itself succeeded; nothing should be counted lost"


def test_a_policy_bundle_change_that_could_not_be_audited_is_counted(monkeypatch):
    """`_audit_bundle` is a *second* audit site in `policy_engine`, and it had no test.

    Storing or activating a policy bundle changes what every later decision is judged against, so
    losing that record is worse than losing one decision: the log then shows decisions evaluated
    under rules it cannot account for. The decision path (`_audit`) was covered and this one was
    not — which is what a per-module count misses, and why the coverage check below is per
    *function*.
    """
    from examlops.data.audit import dropped_audit_events
    from examlops.policy_engine import _audit_bundle

    _break_the_audit_log(monkeypatch)
    _audit_bundle("acme", 3, "policy_bundle_activated", {"sha": "abc123"}, "alice")
    assert "policy_bundle_activated" in dropped_audit_events(), dropped_audit_events()


def test_an_eval_gate_refusal_that_could_not_be_audited_is_counted(monkeypatch):
    """ADR 0111: a promotion refused because no calibrated judge could gate it.

    The refusal stands whether or not it is recorded — but it is an Article 12 required event, and
    the coverage check asks only whether *at least one* event of each required type exists. A
    refusal lost while a sibling survives is invisible to both that check and the hash chain.
    """
    from examlops.data.audit import dropped_audit_events
    from examlops.evaluation.gate import _audit_refusal

    _break_the_audit_log(monkeypatch)
    _audit_refusal(
        "exa-eval", "alice", "promotion_refused", "JPCP", "18", "judge not calibrated", ["accuracy"]
    )
    assert "promotion_refused" in dropped_audit_events(), dropped_audit_events()


def test_a_retrain_trigger_that_could_not_be_audited_is_counted(monkeypatch):
    """`retrain_triggered` is an Article 12 required event.

    The coverage report asks only whether *at least one* event of each required type exists, so a
    dropped one is invisible while any sibling survives — which is precisely why the loss has to
    be counted somewhere the report cannot reach.
    """
    from examlops.cli.commands.retrain import _record_audit
    from examlops.data.audit import dropped_audit_events

    _break_the_audit_log(monkeypatch)
    _record_audit("JPCP", "PM100Dataset", False, None, {"flow_run_id": "r1"}, reason="drift")
    assert "retrain_triggered" in dropped_audit_events(), dropped_audit_events()


def test_a_telemetry_anchor_that_could_not_be_audited_is_counted(monkeypatch):
    """The anchor is what makes rows *outside* the hash chain tamper-evident.

    Losing the record that an anchor was taken is the one loss the chain itself can least afford:
    `exa audit verify` reports unchained rows as unverified, and the anchor is the separate
    evidence that they have not moved since.
    """
    from examlops.data.audit import dropped_audit_events
    from examlops.telemetry_anchor import anchor_telemetry

    with __import__("examlops").platform_db.get_db() as conn:
        conn.execute(
            "INSERT INTO drift_snapshots (model, alias, prediction, ts) "
            "VALUES ('JPCP', 'Production', 1.0, '2026-09-15 12:00:00')"
        )
    _break_the_audit_log(monkeypatch)
    anchor_telemetry()
    assert dropped_audit_events(), "the anchor was taken and nothing recorded the lost audit row"


@pytest.mark.parametrize(
    ("command", "action"),
    [("approve", "model_approved"), ("reject", "model_rejected")],
)
def test_an_approval_decision_that_could_not_be_audited_is_counted(monkeypatch, command, action):
    """The sysadmin approval gate is the platform's human-in-the-loop record.

    Who approved what, and when, is the first thing asked after a bad model reaches production.
    The decision still stands if the audit log is down — it has already been sent to the control
    plane — but losing the record of *who decided* without counting it leaves the log quietly
    incomplete at exactly the entry an investigation starts from.
    """
    from typer.testing import CliRunner

    from examlops.cli.commands import approvals
    from examlops.data.audit import dropped_audit_events

    monkeypatch.setattr(
        approvals.control_plane_api,
        command,
        lambda *a, **k: {"flow_run_id": "r1", "status": "scheduled"},
    )
    # `--yes` is a global on the root app, not on this sub-app; the prompt is UI, not the
    # behaviour under test.
    monkeypatch.setattr(approvals._output, "confirm", lambda *a, **k: True)
    _break_the_audit_log(monkeypatch)
    result = CliRunner().invoke(approvals.app, [command, "JPCP"])
    assert result.exit_code == 0, result.output
    assert action in dropped_audit_events(), dropped_audit_events()


# ── coverage of the conversions, derived rather than counted by hand ──────────

#: Every `(module, enclosing function)` that calls `audit_best_effort`, each paired with the test
#: that proves *its* loss is counted. Per **function**, not per module: `policy_engine` holds two
#: audit sites and only one of them was tested, which a per-module tally reports as covered.
#:
#: This list exists because I twice reported "N sites converted, all tested" from a hand count and
#: was twice wrong. The scan below derives the truth from the tree; this mapping only has to say
#: which test covers what.
COVERED_AUDIT_SITES = {
    ("examlops/tool_broker/broker.py", "_audit_decision"): (
        "tests/unit/test_tool_broker.py::test_a_lost_decision_audit_is_counted"
    ),
    ("examlops/tool_broker/service.py", "_audit_change"): (
        "tests/unit/test_tool_broker.py::test_a_lost_grant_change_audit_is_counted"
    ),
    ("examlops/agent_versions/service.py", "register"): (
        "tests/unit/test_agent_versions.py::test_a_lost_register_audit_is_counted"
    ),
    ("examlops/agent_versions/service.py", "set_alias"): (
        "tests/unit/test_agent_versions.py::test_a_lost_alias_move_audit_is_counted"
    ),
    ("examlops/agent_versions/service.py", "rollback"): (
        "tests/unit/test_agent_versions.py::test_a_lost_rollback_audit_is_counted"
    ),
    (
        "examlops/admission_seam/reservations.py",
        "_audit",
    ): "tests/unit/test_quota_reservations.py::test_service_uses_project_limits_audits_and_counts_a_lost_audit",
    (
        "examlops/admission_seam/service.py",
        "decide",
    ): "tests/unit/test_quota_reservations.py::test_a_lost_admission_decision_audit_is_counted",
    (
        "examlops/suspend/service.py",
        "suspend",
    ): "tests/unit/test_suspend_seam.py::test_a_lost_suspend_audit_is_counted",
    (
        "examlops/suspend/service.py",
        "resume",
    ): "tests/unit/test_suspend_seam.py::test_a_lost_resume_audit_is_counted",
    (
        "examlops/suspend/service.py",
        "discard",
    ): "tests/unit/test_suspend_seam.py::test_a_lost_discard_audit_is_counted",
    ("examlops/agentops/__init__.py", "_emit_breaker_event"): (
        "tests/unit/test_agentops_observability.py"
        "::test_a_lost_breaker_audit_is_counted_and_does_not_change_the_abort"
    ),
    ("examlops/drift_advanced/scheduler.py", "_audit"): (
        "tests/unit/test_drift_scheduler.py::test_a_lost_drift_advanced_audit_is_counted"
    ),
    ("examlops/autoscale/controller.py", "_audit"): (
        "tests/unit/test_autoscale_controller.py::test_a_lost_autoscale_audit_is_counted"
    ),
    ("examlops/cli/commands/ops_cmd.py", "_audit"): (
        "tests/unit/test_operations.py::test_a_cancel_request_that_could_not_be_audited_is_counted"
    ),
    ("examlops/offline/executor.py", "_audit"): (
        "tests/unit/test_offline_inference.py"
        "::test_a_lost_offline_run_audit_is_counted_and_the_run_still_completes"
    ),
    ("examlops/offline/executor.py", "cancel"): (
        "tests/unit/test_offline_inference.py::test_a_lost_offline_cancel_audit_is_counted"
    ),
    ("examlops/secrets/__init__.py", "_audit"): "test_a_governance_audit_loss_is_counted",
    ("examlops/supplychain/__init__.py", "_audit"): "test_a_governance_audit_loss_is_counted",
    ("examlops/policy/__init__.py", "_audit"): (
        "test_a_policy_decision_from_the_other_policy_module_is_counted"
    ),
    ("examlops/policy/__init__.py", "decide_safe"): (
        "test_a_policy_unavailable_event_that_could_not_be_audited_is_counted"
    ),
    ("examlops/policy_engine/__init__.py", "_audit"): (
        "test_a_policy_decision_that_is_not_logged_is_counted"
    ),
    ("examlops/policy_engine/__init__.py", "_audit_bundle"): (
        "test_a_policy_bundle_change_that_could_not_be_audited_is_counted"
    ),
    ("examlops/lifecycle/upgrade.py", "_audit"): (
        "test_an_upgrade_that_could_not_be_audited_is_counted"
    ),
    ("examlops/guardrails/__init__.py", "_record"): (
        "test_a_guardrail_block_that_could_not_be_audited_is_counted"
    ),
    ("examlops/cli/commands/approvals.py", "approve"): (
        "test_an_approval_decision_that_could_not_be_audited_is_counted"
    ),
    ("examlops/cli/commands/approvals.py", "reject"): (
        "test_an_approval_decision_that_could_not_be_audited_is_counted"
    ),
    ("examlops/cli/commands/retrain.py", "_record_audit"): (
        "test_a_retrain_trigger_that_could_not_be_audited_is_counted"
    ),
    ("examlops/telemetry_anchor.py", "anchor_telemetry"): (
        "test_a_telemetry_anchor_that_could_not_be_audited_is_counted"
    ),
    ("examlops/evaluation/gate.py", "_audit_refusal"): (
        "test_an_eval_gate_refusal_that_could_not_be_audited_is_counted"
    ),
    # Covered elsewhere, verified by reading the test rather than by co-occurrence: this one
    # runs a whole cycle against a refusing audit log and asserts the drop is counted.
    ("examlops/cli/commands/autopilot_cmd.py", "run_cycle"): (
        "tests/unit/test_autopilot.py::test_the_loss_is_counted_rather_than_hidden"
    ),
    ("examlops/reproducibility/__init__.py", "_audit"): (
        "tests/unit/test_reproduce_auto.py::test_a_lost_bundle_built_audit_is_counted"
    ),
    ("examlops/reproducibility/auto.py", "_record_failure"): (
        "tests/unit/test_reproduce_auto.py::test_a_lost_failure_audit_is_counted_too"
    ),
    ("examlops/reproducibility/execute.py", "_audit"): (
        "tests/unit/test_reproduce_execute.py::test_a_lost_repro_audit_is_counted"
    ),
    # In the agent suite (`platform/services/agent/tests/`), which has the deps these need.
    (
        "skipper/watch.py",
        "_raise_alert",
    ): "test_the_watch_daemon_counts_an_alert_it_could_not_audit",
    ("skipper/memory_types.py", "audit_memory_op"): "test_memory_governance_counts_a_lost_audit",
    ("skipper/tools/training.py", "_audit_retrain"): "test_the_agent_counts_a_lost_retrain_audit",
    ("skipper/tools/platform_ops.py", "trigger_auto_retrain"): (
        "test_the_agent_counts_a_lost_auto_retrain_audit"
    ),
}


#: `audit_best_effort` sites with no **declared** drop-counting test. **May only go DOWN.**
#:
#: "Declared" is the honest word, and the distinction cost me a wrong claim: absence from
#: `COVERED_AUDIT_SITES` means *this mapping does not name a test*, not *no test exists*. I
#: reported these as untested and then found `autopilot_cmd.run_cycle` proved by
#: `test_autopilot.py`, which I had simply not looked for. A file mentioning both a function name
#: and `dropped_audit_events` is not evidence either — co-occurrence is not coverage, so an entry
#: goes in here only after reading the test.
#:
#: The rest pre-date this arc: an earlier pass targeted audit writes that *raise* inside a loop, so
#: they are tested for surviving a failure. Whether each also proves the loss is recorded is
#: unknown until someone reads it — which is the work this number is counting.
UNTESTED_AUDIT_SITE_CEILING = 3


def _audit_best_effort_sites() -> set[tuple[str, str]]:
    """Scan the tree for every function that calls `audit_best_effort`."""
    root = Path(__file__).parents[2]
    found: set[tuple[str, str]] = set()
    for src_file in sorted(root.glob("platform/**/*.py")) + sorted(root.glob("serving/**/*.py")):
        sp = src_file.as_posix()
        if "/build/" in sp or "/tests/" in sp or src_file.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(src_file.read_text())
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(n, ast.Call)
                and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
                == "audit_best_effort"
                for n in ast.walk(fn)
            ):
                # Trim to the part of the path that identifies the module across both packages.
                # Relative to the source roots, not to a marker substring: the repository
                # directory is itself named `examlops`, so matching on "/examlops/" produced
                # `examlops/platform/services/agent/...` for the agent package.
                rel = src_file.relative_to(root).as_posix()
                for prefix in ("platform/cli/src/", "platform/services/agent/"):
                    if rel.startswith(prefix):
                        rel = rel[len(prefix) :]
                        break
                found.add((rel, fn.name))
    return found


def test_a_cross_file_coverage_claim_names_a_test_that_exists():
    """The mapping's test names are documentation — but a *missing* one is checkable.

    What this cannot do is verify that the named test actually breaks the audit log and asserts
    the counter; pointing an entry at a real-but-irrelevant test would pass. That limit is worth
    stating rather than leaving implicit. What it does catch is the realistic decay: a test that
    was renamed, moved or deleted while its entry stayed behind, quietly claiming coverage that
    left with it.
    """
    root = Path(__file__).parents[2]
    missing: list[str] = []
    for site, test in COVERED_AUDIT_SITES.items():
        if "::" not in test:
            continue  # same-file tests: pytest would already fail on a bad name
        path, name = test.split("::", 1)
        src = root / path
        if not src.exists():
            missing.append(f"{site}: {test} (no such file)")
            continue
        if f"def {name}(" not in src.read_text():
            missing.append(f"{site}: {test} (no such test in that file)")
    assert not missing, "coverage claims naming a test that is not there:\n  " + "\n  ".join(
        missing
    )


def test_every_converted_site_has_a_test_that_its_loss_is_counted():
    """Converting a site and testing it are two jobs, and the second is easy to skip.

    A conversion with no test proves only that the call changed. The point of the change is that a
    lost event is *counted*, and nothing demonstrates that except breaking the audit log and
    looking at the counter.
    """
    found = _audit_best_effort_sites()
    untested = found - set(COVERED_AUDIT_SITES)
    assert len(untested) <= UNTESTED_AUDIT_SITE_CEILING, (
        f"{len(untested)} `audit_best_effort` sites have no DECLARED drop-counting test "
        f"(ceiling {UNTESTED_AUDIT_SITE_CEILING}). Add one, or — if a test already proves it — "
        f"read that test and name it in COVERED_AUDIT_SITES:\n  "
        + "\n  ".join(f"{m}:{f}()" for m, f in sorted(untested))
    )
    assert len(untested) == UNTESTED_AUDIT_SITE_CEILING, (
        f"only {len(untested)} remain untested but the ceiling still says "
        f"{UNTESTED_AUDIT_SITE_CEILING} — lower it, or the progress evaporates"
    )
    stale = set(COVERED_AUDIT_SITES) - found
    assert not stale, (
        "these are listed as covered but no longer call `audit_best_effort` — the mapping is "
        f"describing code that moved: {sorted(stale)}"
    )


# ── the ratchet ───────────────────────────────────────────────────────────────

#: Call sites that still write an audit event themselves inside a blanket `except`, instead of
#: `audit_best_effort`. **This number may only go DOWN.** It is not zero because 28 sites predate
#: the helper and each needs its own read — converting them blind would be a large untested diff.
#: Converted so far: secrets, supply chain, BOTH policy modules (`policy` and `policy_engine` are
#: near-twins and the first pass only caught one), guardrails, the watch daemon's alerts, the data
#: -format upgrade, and the agent's memory governance. The rest are visible and counted rather
#: than forgotten.
#:
#: Two shapes are deliberately NOT counted, because they do not hide the loss: a handler that
#: hands the cause back to its caller (`mcp._audit` returns "action succeeded but was not
#: audited: …"), and a narrow `except ImportError` falling through to a documented alternative.
#: `log.debug` does not count as disclosure — it is invisible at any production log level.
SWALLOWED_AUDIT_WRITES_CEILING = 16


def _blanket(handler: ast.ExceptHandler) -> bool:
    """Does this handler catch everything? `except ImportError: pass` falling through to a
    documented alternative is a different thing — the dashboard's `audit_write` uses exactly that
    shape while logging and re-raising everything else, and must not be flagged."""
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in ("Exception", "BaseException")
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(e, ast.Name) and e.id in ("Exception", "BaseException")
            for e in handler.type.elts
        )
    return False


def _swallowed_audit_writes() -> list[str]:
    root = Path(__file__).parents[2]
    out: list[str] = []
    for src_file in sorted(root.glob("platform/**/*.py")) + sorted(root.glob("serving/**/*.py")):
        sp = src_file.as_posix()
        if "/build/" in sp or "/tests/" in sp or src_file.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(src_file.read_text())
        except SyntaxError:
            continue
        # An aliased import hides the call from a name match — `from ... import write_audit_event
        # as _w` then `_w(...)`. Found by mutation: a mutant that aliased the import slipped past
        # this scan entirely, so the local names are collected rather than assumed.
        names = {"write_audit_event"}
        for imp in ast.walk(tree):
            if isinstance(imp, ast.ImportFrom):
                names |= {a.asname or a.name for a in imp.names if a.name == "write_audit_event"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) in names
            ]
            if not calls:
                continue
            for h in node.handlers:
                if not _blanket(h):
                    continue
                # "Not hidden" has more than one honest form. Logging at WARNING or above is
                # one; handing the cause back to the caller is another, and `mcp._audit` does
                # exactly that — it returns "action succeeded but was not audited: …" for the
                # agent to surface. `log.debug` is NOT one: it is invisible at any production
                # level, which is how `memory_types` lost its governance records.
                discloses = any(
                    (
                        isinstance(n, ast.Call)
                        and getattr(n.func, "attr", "")
                        in ("warning", "error", "exception", "critical")
                    )
                    or (isinstance(n, ast.Return) and n.value is not None)
                    or isinstance(n, ast.Raise)
                    for n in ast.walk(h)
                )
                if not discloses:
                    out.append(f"{src_file.relative_to(root)}:{calls[0].lineno}")
    return out


def test_the_ratchet_detector_sees_a_planted_case(tmp_path):
    """Prove the scan before trusting its count — and that a narrow handler is not flagged."""
    (tmp_path / "platform").mkdir()
    (tmp_path / "platform" / "p.py").write_text(
        "def bad():\n"
        "    try:\n        write_audit_event('a', 'b', 'c', 'd')\n"
        "    except Exception:\n        pass\n"
        "def narrow():\n"
        "    try:\n        write_audit_event('a', 'b', 'c', 'd')\n"
        "    except ImportError:\n        pass\n"
    )
    src = (tmp_path / "platform" / "p.py").read_text()
    tree = ast.parse(src)
    blanket = [
        h for n in ast.walk(tree) if isinstance(n, ast.Try) for h in n.handlers if _blanket(h)
    ]
    assert len(blanket) == 1, "a narrow `except ImportError` must not count as a blanket handler"


def test_the_disclosure_classifier_agrees_with_the_rule(tmp_path):
    """Pin what counts as disclosure, because the tree currently exercises only some of it.

    After this iteration's fixes no remaining site uses `log.debug`, so a mutant that accepted
    `debug` as disclosure changed nothing — the rule was true but untested. These planted handlers
    keep all four cases honest: WARNING discloses, a returned cause discloses, a raise discloses,
    and `log.debug` does not (it is invisible at any production log level, which is exactly how
    the agent's memory audit lost its records).
    """
    cases = {
        "warn": "    except Exception as e:\n        log.warning('lost: %s', e)\n",
        "returns": "    except Exception as e:\n        return f'not audited: {e}'\n",
        "raises": "    except Exception:\n        raise\n",
        "debug": "    except Exception as e:\n        log.debug('skipped: %s', e)\n",
        "pass": "    except Exception:\n        pass\n",
    }
    hidden = {"debug", "pass"}
    for name, handler in cases.items():
        src = "def f():\n    try:\n        write_audit_event('a', 'b', 'c', 'd')\n" + handler
        h = next(
            hh for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Try) for hh in n.handlers
        )
        discloses = any(
            (
                isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") in ("warning", "error", "exception", "critical")
            )
            or (isinstance(n, ast.Return) and n.value is not None)
            or isinstance(n, ast.Raise)
            for n in ast.walk(h)
        )
        assert discloses is (name not in hidden), f"{name} classified wrongly"


def test_swallowed_audit_writes_only_decrease():
    """A lost audit event that nothing records is a record-keeping incident nobody can see.

    Lower this number when you convert a site to `audit_best_effort`; never raise it. A new call
    site should use the helper, which is why adding one here fails.
    """
    found = _swallowed_audit_writes()
    assert len(found) <= SWALLOWED_AUDIT_WRITES_CEILING, (
        f"{len(found)} audit writes are swallowed by a blanket handler that records nothing "
        f"(ceiling {SWALLOWED_AUDIT_WRITES_CEILING}). Use `audit_best_effort`, which fails open "
        f"and counts the loss:\n  " + "\n  ".join(sorted(set(found))[:40])
    )
    # The ratchet has to ratchet. Without this the ceiling can be raised — or left high after the
    # sites are fixed — and the guard quietly stops meaning anything; `<=` alone would pass with a
    # ceiling of 99. Fixing a site is therefore two edits, the second of which is this number.
    assert len(found) == SWALLOWED_AUDIT_WRITES_CEILING, (
        f"only {len(found)} sites remain but the ceiling still says "
        f"{SWALLOWED_AUDIT_WRITES_CEILING} — lower it to {len(found)} so the progress is kept"
    )

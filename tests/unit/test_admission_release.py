"""ADR 0116 decision 3, part 2 — a reservation is released when its holder finishes.

The seam shipped with the *reserve* half and nothing that gave quota back, which is worse than no
reservation at all: a project's headroom shrinks by every job it has ever run and the only sign is
a row in ``quota_reservations``. These tests pin the two halves the ADR's status line called out:

* **release on completion**, for every terminal outcome including failure and cancellation, exactly
  once, through the one chokepoint where terminal state is recorded
  (:func:`examlops.data.hpc.update_hpc_job`);
* **dispatch through the seam** — ``exa pipeline run`` asks ``decide()`` before it executes, behind
  a kill-switch whose OFF path is proven to change nothing.

The TTL sweep stays the backstop for a holder that never reaches its completion path at all, and
the two are proven to resolve the same row exactly once when they race.
"""

from __future__ import annotations

import threading

import pytest

from examlops.admission_seam import JobRequest, Resources, completion, dispatch
from examlops.admission_seam import reservations as svc
from examlops.data import quota_reservations as store

HOLDER = "hpc:mock:job-1"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_RESERVATION_TTL_S", raising=False)
    monkeypatch.delenv("EXAMLOPS_ADMISSION_POLICY", raising=False)
    monkeypatch.delenv("EXAMLOPS_ADMISSION_QUOTAS", raising=False)
    monkeypatch.delenv("EXAMLOPS_ADMISSION_DISPATCH_ENABLED", raising=False)
    monkeypatch.delenv("EXAMLOPS_PROJECT", raising=False)
    import examlops.platform_db as pdb
    from examlops.data.audit import reset_dropped_audit_events

    pdb.init_db()
    reset_dropped_audit_events()
    return pdb


def _hold(rid: str, *, holder: str = HOLDER, gpus: int = 2, ttl: float = 1000.0, project="p"):
    out = store.reserve(
        rid, project, tenant="t", gpus=gpus, ttl_s=ttl, gpus_limit=None, holder=holder
    )
    assert out["ok"], out
    return out["reservation"]


def _actions(pdb, source="admission-seam"):
    with pdb.get_db() as conn:
        return [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE source=? ORDER BY id", (source,)
            )
        ]


# ── release on completion ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_every_terminal_outcome_returns_the_quota(db, outcome):
    """A failure must return quota exactly as a success does — that is the ADR's own wording."""
    _hold("r1", gpus=3)
    assert store.held_totals("p")["gpus"] == 3

    out = completion.release_on_completion(HOLDER, outcome=outcome)

    assert out["released"] == 1 and out["gpus"] == 3 and out["outcome"] == outcome
    assert store.held_totals("p")["gpus"] == 0, "the held GPUs were not returned"
    row = store.get("r1")
    assert row["state"] == "released" and outcome in row["reason"]
    assert _actions(db)[-1] == "quota_released"


def test_a_committed_reservation_is_released_too(db):
    """Commit is not the end of the lifecycle: a committed row holds quota until released."""
    _hold("r1", gpus=2)
    assert store.commit("r1")
    assert store.held_totals("p")["gpus"] == 2
    assert completion.release_on_completion(HOLDER, outcome="completed")["released"] == 1
    assert store.held_totals("p")["gpus"] == 0


def test_release_is_idempotent_and_audits_once(db):
    _hold("r1")
    first = completion.release_on_completion(HOLDER, outcome="completed")
    second = completion.release_on_completion(HOLDER, outcome="failed")
    third = completion.release_on_completion("hpc:mock:never-reserved", outcome="failed")

    assert first["released"] == 1
    assert second["released"] == 0, "a second completion must release nothing"
    assert third["released"] == 0, "a holder that never reserved is not an error"
    assert store.get("r1")["reason"] == "holder completed", "the first outcome is the one kept"
    assert _actions(db).count("quota_released") == 1, "one release, one audit row"


def test_release_covers_every_reservation_one_holder_owns(db):
    _hold("a", gpus=1)
    _hold("b", gpus=2)
    _hold("c", gpus=4, holder="hpc:mock:other-job")
    assert store.held_totals("p")["gpus"] == 7

    out = completion.release_on_completion(HOLDER, outcome="completed")

    assert out["released"] == 2 and sorted(out["reservations"]) == ["a", "b"]
    assert store.held_totals("p")["gpus"] == 4, "another holder's reservation must survive"


def test_an_unauditable_release_still_returns_the_quota_and_counts_the_loss(db, monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events

    _hold("r1")
    monkeypatch.setattr(
        audit_mod, "write_audit_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert completion.release_on_completion(HOLDER, outcome="failed")["released"] == 1
    assert store.held_totals("p")["gpus"] == 0
    assert "quota_released" in dropped_audit_events()


def test_an_invalid_outcome_is_refused_rather_than_coerced(db):
    with pytest.raises(ValueError, match="outcome must be one of"):
        completion.release_on_completion(HOLDER, outcome="COMPLETED-ish")


# ── the chokepoint ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        ("COMPLETED", "completed"),
        ("FAILED", "failed"),
        ("TIMEOUT", "failed"),
        ("NODE_FAIL", "failed"),
        ("CANCELLED", "cancelled"),
        ("PREEMPTED", "cancelled"),
    ],
)
def test_the_job_chokepoint_releases_on_every_terminal_state(db, state, outcome):
    """`update_hpc_job` is the single place a job's terminal state is written — both callers
    (`scheduler_jobs.finish_job` and the pipeline generator) reach it, for all three schedulers."""
    from examlops.data.hpc import record_hpc_job, update_hpc_job

    record_hpc_job("job-1", "mock", None, "JPCP", "PM100Dataset", gpus=2)
    _hold("r1", gpus=2)

    update_hpc_job("job-1", "mock", state=state, exit_code=0)

    assert store.held_totals("p")["gpus"] == 0
    row = store.get("r1")
    assert row["state"] == "released" and state in row["reason"]
    assert completion.normalize_outcome(state) == outcome


@pytest.mark.parametrize("state", ["RUNNING", "PENDING", "COMPLETING", "WEIRD_NEW_STATE", None])
def test_a_non_terminal_state_releases_nothing(db, state):
    """Guessing an unknown state into `failed` would free GPUs a running job still holds — a leak
    is visible in the table and swept by TTL; an early release is not visible at all."""
    from examlops.data.hpc import record_hpc_job, update_hpc_job

    record_hpc_job("job-1", "mock", None, "JPCP", "PM100Dataset", gpus=2)
    _hold("r1", gpus=2)

    update_hpc_job("job-1", "mock", state=state, start_time="2026-01-01T00:00:00")

    assert completion.normalize_outcome(state) is None
    assert store.held_totals("p")["gpus"] == 2, "a running job must keep its reservation"
    assert store.get("r1")["state"] == "reserved"


def test_the_chokepoint_never_fails_the_job_it_is_recording(db, monkeypatch, caplog):
    """Bookkeeping must not break a job — but the loss is logged, not swallowed silently."""
    from examlops.data.hpc import record_hpc_job, update_hpc_job

    record_hpc_job("job-1", "mock", None, "JPCP", "PM100Dataset", gpus=2)
    _hold("r1", gpus=2)
    monkeypatch.setattr(
        store, "release_by_holder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    )

    with caplog.at_level("WARNING"):
        update_hpc_job("job-1", "mock", state="COMPLETED")

    with db.get_db() as conn:
        assert conn.execute("SELECT state FROM hpc_jobs").fetchone()["state"] == "COMPLETED"
    assert any("TTL sweep" in r.getMessage() for r in caplog.records)


# ── the TTL backstop, and the race with it ──────────────────────────────────────────────
def test_the_ttl_backstop_still_reclaims_a_crashed_holder(db):
    """A holder killed outright reaches no completion path; the sweep is what makes it visible."""
    store.reserve("crashed", "p", tenant="t", gpus=4, ttl_s=10.0, holder=HOLDER, now=1000.0)

    assert store.held_totals("p", now=1005.0)["gpus"] == 4
    assert store.held_totals("p", now=1011.0)["gpus"] == 0, "lapsed holds nothing even unswept"
    swept = store.expire_due(now=1011.0)

    assert [r["id"] for r in swept] == ["crashed"]
    assert store.get("crashed")["state"] == "expired"
    assert completion.release_on_completion(HOLDER, outcome="failed")["released"] == 0


def test_a_lapsed_but_unswept_reservation_is_resolved_by_completion(db):
    """Completion resolves the row as `released` even past its TTL — more truthful than leaving
    it for the sweep to call `expired`, and it holds no quota either way."""
    store.reserve("late", "p", tenant="t", gpus=4, ttl_s=10.0, holder=HOLDER, now=1000.0)
    out = completion.release_on_completion(HOLDER, outcome="completed", reason="job COMPLETED")
    assert out["released"] == 1
    assert store.get("late")["state"] == "released"


def test_completion_racing_the_expiry_sweep_resolves_the_row_exactly_once(db):
    """Both paths take the same scoped write lock and touch only unresolved rows, so one wins."""
    winners: list[str] = []
    barrier = threading.Barrier(2)

    store.reserve("raced", "p", tenant="t", gpus=2, ttl_s=1.0, holder=HOLDER, now=1.0)

    def complete():
        barrier.wait()
        if completion.release_on_completion(HOLDER, outcome="completed")["released"]:
            winners.append("released")

    def sweep():
        barrier.wait()
        if store.expire_due():
            winners.append("expired")

    threads = [threading.Thread(target=complete), threading.Thread(target=sweep)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"the row was resolved twice: {winners}"
    assert store.get("raced")["state"] == winners[0]
    assert store.held_totals("p")["gpus"] == 0


# ── the quota is genuinely returned ─────────────────────────────────────────────────────
def test_a_reservation_counted_against_the_project_quota_is_given_back(db):
    """The headroom assertion the ADR's verification item 3 is really about."""
    from examlops.data.projects import create_project

    create_project("proj", gpu_limit=4)
    req = JobRequest(project="proj", tenant="t", resources=Resources(gpus=4))
    holder = completion.holder_for_job("flux", "f42")

    first = svc.reserve(req, holder=holder)
    assert first["ok"]
    assert not svc.reserve(req, holder="other")["ok"], "the quota is full while the job holds it"

    completion.release_on_completion(holder, outcome="failed")

    assert store.held_totals("proj")["gpus"] == 0
    assert svc.reserve(req, holder="other")["ok"], "headroom was not restored"


# ── dispatch: the kill-switch OFF path ──────────────────────────────────────────────────
def test_the_kill_switch_is_off_by_default(db, monkeypatch):
    assert dispatch.is_enabled() is False
    for value in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv(dispatch.ENABLED_ENV, value)
        assert dispatch.is_enabled() is False, value
    for value in ("1", "true", "YES", "On"):
        monkeypatch.setenv(dispatch.ENABLED_ENV, value)
        assert dispatch.is_enabled() is True, value


def test_disabled_dispatch_decides_nothing_reserves_nothing_audits_nothing(db):
    req = JobRequest(project="p", tenant="t", resources=Resources(gpus=99999))
    with dispatch.admitted(req) as admission:
        assert admission.enabled is False
        assert admission.holder is None and admission.reservation is None

    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM quota_reservations").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"] == 0


def test_the_cli_run_path_is_byte_identical_with_the_gate_off(db, monkeypatch):
    """The regression the kill-switch exists to make provable: same argv, no new state."""
    from typer.testing import CliRunner

    from examlops.cli.commands import pipeline

    seen: list[list[str]] = []
    monkeypatch.setattr(pipeline, "_run_generator", lambda args: seen.append(list(args)))

    result = CliRunner().invoke(
        pipeline.app, ["run", "--model", "JPCP", "--dataset", "PM100Dataset", "--dummy"]
    )

    assert result.exit_code == 0, result.output
    assert seen == [["--dummy", "--model", "JPCP", "--dataset", "PM100Dataset"]]
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM quota_reservations").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"] == 0


# ── dispatch: the kill-switch ON path ───────────────────────────────────────────────────
def test_enabled_dispatch_admits_holds_and_releases(db, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    req = dispatch.request_for_pipeline_run(project="p", gpus=2)

    with dispatch.admitted(req, holder="run:one") as admission:
        assert admission.enabled and admission.decision["verdict"] == "admit"
        assert store.held_totals("p")["gpus"] == 2, "quota is held for the duration of the work"

    assert store.held_totals("p")["gpus"] == 0
    assert store.get(admission.reservation)["state"] == "released"
    assert _actions(db) == ["quota_reserved", "quota_released"]


@pytest.mark.parametrize(
    ("raiser", "outcome"),
    [
        (lambda: 1 / 0, "failed"),
        (lambda: (_ for _ in ()).throw(KeyboardInterrupt()), "cancelled"),
    ],
)
def test_the_reservation_is_released_however_the_block_ends(db, monkeypatch, raiser, outcome):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    req = dispatch.request_for_pipeline_run(project="p", gpus=2)

    with pytest.raises((ZeroDivisionError, KeyboardInterrupt)):
        with dispatch.admitted(req, holder="run:one") as admission:
            rid = admission.reservation
            raiser()

    assert store.held_totals("p")["gpus"] == 0
    assert store.get(rid)["state"] == "released"
    assert outcome in store.get(rid)["reason"]


def test_a_policy_rejection_refuses_the_run_and_audits_it(db, monkeypatch):
    """`gang` on a backend that has not declared it: the seam's own refusal, not a quota one."""
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv("EXAMLOPS_ADMISSION_POLICY", "baseline-over-quota")
    req = JobRequest(project="p", tenant="t", resources=Resources(gpus=1), gang=True)

    with pytest.raises(dispatch.AdmissionRefused) as exc:
        with dispatch.admitted(req, holder="run:one"):
            pytest.fail("the block must not run when admission refuses")

    assert exc.value.verdict == "reject" and "gang" in exc.value.reason
    assert "admission_refused" in _actions(db)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM quota_reservations").fetchone()["c"] == 0


def test_a_refusal_that_could_not_be_audited_is_counted(db, monkeypatch):
    """The refusal still stands — and the platform can tell that its record is missing."""
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv("EXAMLOPS_ADMISSION_POLICY", "baseline-over-quota")
    monkeypatch.setattr(
        audit_mod, "write_audit_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    req = JobRequest(project="p", tenant="t", resources=Resources(gpus=1), gang=True)

    with pytest.raises(dispatch.AdmissionRefused):
        with dispatch.admitted(req, holder="run:one"):
            pytest.fail("refused work must not run")

    assert "admission_refused" in dropped_audit_events()


def test_a_quota_refusal_refuses_the_run_and_holds_nothing(db, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    from examlops.data.projects import create_project

    create_project("proj", gpu_limit=2)
    req = JobRequest(project="proj", tenant="t", resources=Resources(gpus=4))

    with pytest.raises(dispatch.AdmissionRefused) as exc:
        with dispatch.admitted(req, holder="run:one"):
            pytest.fail("the block must not run when the quota refuses")

    assert "limit 2" in exc.value.reason
    assert store.held_totals("proj")["gpus"] == 0
    assert _actions(db)[-2:] == ["quota_reservation_refused", "admission_refused"]


def test_the_cli_gate_exits_non_zero_on_a_refusal(db, monkeypatch):
    """A refusal is a refusal at the command line too — not a warning the run continues past."""
    import typer

    from examlops.cli._admission_gate import pipeline_run_gate
    from examlops.data.projects import create_project

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    create_project("proj", gpu_limit=1)
    ran = []

    with pytest.raises((SystemExit, typer.Exit)):
        with pipeline_run_gate(project="proj", model="JPCP", gpus=8):
            ran.append("body")

    assert ran == [], "the work must not start"
    assert store.held_totals("proj")["gpus"] == 0

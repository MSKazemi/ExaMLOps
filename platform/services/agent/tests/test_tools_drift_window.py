"""The agent's drift tools must read each model's own window, not a shared global one.

Every canonical drift read in the platform windows *per model* —
``WHERE model=? ORDER BY ts DESC LIMIT ?`` (``examlops/cli/commands/drift.py``,
``forecast.py``, ``corruption.py``, the dashboard's ``drift_data.py``). A tool that instead reads
the newest N rows across all models and then groups them is asking a different question: a model
whose predictions are simply less frequent than its neighbours' falls out of the window entirely
and reads as *having no snapshots*, which is the one answer that stops the closed loop.
"""

import importlib

import pytest

# Enough rows from one busy model to push a quieter model out of any global window the tools use.
# Inserted in bulk (0.03 s) so this runs at the real size rather than a shrunk-constant imitation.
BUSY_ROWS = 6000


@pytest.fixture()
def ops(tmp_path, monkeypatch):
    """Reload the tool module against a private PLATFORM_DB holding one busy and one quiet model.

    ``quiet`` has a single, *older* snapshot far from its baseline: it is exactly the model an
    auto-retrain exists for, and exactly the one a global window hides.
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))

    import examlops.platform_db as pdb

    importlib.reload(pdb)
    pdb.init_db(force=True)

    with pdb.get_db() as conn:
        conn.execute(
            "INSERT INTO drift_snapshots (model, alias, prediction, ts) VALUES (?,?,?,?)",
            ("quiet", "Production", 100.0, "2026-09-01T00:00:00"),
        )
        conn.executemany(
            "INSERT INTO drift_snapshots (model, alias, prediction, ts) VALUES (?,?,?,?)",
            [
                (
                    "busy",
                    "Production",
                    1.0,
                    f"2026-09-15T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}",
                )
                for i in range(BUSY_ROWS)
            ],
        )

    pdb.set_drift_baseline("quiet", {"mean": 1.0, "std": 1.0})
    pdb.set_drift_baseline("busy", {"mean": 1.0, "std": 1.0})
    pdb.set_drift_auto_retrain("quiet", True, min_z_score=3.0, dataset_name="PM100Dataset")

    # `trigger_auto_retrain` is a HITL-gated write. Approve it the way an operator would instead of
    # reaching past the gate to the undecorated function, so what these tests exercise is the
    # shipped path, gate included.
    from skipper import confirm as confirm_mod

    monkeypatch.setattr(confirm_mod, "interrupt", lambda _payload: "yes")

    from skipper.tools import platform_ops

    return importlib.reload(platform_ops)


def test_auto_retrain_sees_a_quiet_models_drift_behind_a_busier_one(ops, monkeypatch):
    """A drifted model with auto-retrain enabled must be evaluated, however quiet it is.

    Reading "no snapshots" here is worse than a wrong z-score: the model is silently dropped from
    the loop, and the skip message blames missing data that is in fact present.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        ops._http,
        "submit_retrain",
        lambda payload, automated=False: (calls.append(payload), ({"flow_run_id": "r1"}, None))[1],
    )

    out = ops.trigger_auto_retrain.invoke({"model_name": "quiet"})

    assert "no snapshots" not in out, out
    assert [c["model_name"] for c in calls] == ["quiet"], out


def test_a_model_with_genuinely_no_snapshots_is_still_reported_as_such(ops, monkeypatch):
    """ "No snapshots" must stay available for the case that really is that.

    Widening the window is only half the fix: the skip path that says so has to survive it, and
    say it rather than divide by an empty sample.
    """
    import examlops.platform_db as pdb

    pdb.set_drift_auto_retrain("never-ran", True, min_z_score=3.0, dataset_name="PM100Dataset")
    out = ops.trigger_auto_retrain.invoke({"model_name": "never-ran"})
    assert "never-ran: no snapshots" in out, out


def test_drift_status_for_a_model_with_no_snapshots_answers_instead_of_raising(ops):
    """Asking about a model that has never predicted is a fair question with a plain answer.

    The helper omits a model it found no rows for rather than recording an empty sample, because
    an empty sample reaches the mean as a division by zero — the tool would raise where it should
    say "none recorded".
    """
    out = ops.get_drift_status.invoke({"model_name": "never-predicted"})
    assert "No drift snapshots recorded yet" in out, out


def test_drift_status_reports_a_quiet_model_behind_a_busier_one(ops):
    """`get_drift_status()` with no model named must still list every model that has snapshots."""
    out = ops.get_drift_status.invoke({"model_name": ""})
    assert "quiet" in out, out


def test_diagnose_platform_flags_a_quiet_models_drift(ops):
    """The triage tool's drift section must not go quiet about the quietest model."""
    out = ops.diagnose_platform.invoke({})
    assert "quiet" in out, out

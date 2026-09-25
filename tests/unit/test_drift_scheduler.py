"""ADR 0022 — the advanced-drift scheduler, the detector seam and the River adapter.

Nothing used to *run* the concept / label-free / data-quality detectors: they fired when a person
typed ``exa drift concept``. The scheduler sweeps every model with predictions, behind the
house kill-switch + lease + cooldown, writing ``drift_events`` that ``exa drift trigger`` already
consumes.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import drift_advanced as da  # noqa: E402
from examlops.drift_advanced import scheduler as sch  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for name in (sch.ENABLED_ENV, sch.COOLDOWN_ENV, da.CONCEPT_DETECTOR_ENV):
        monkeypatch.delenv(name, raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _seed(model, preds, labels=None, features=None):
    from examlops import platform_db

    with platform_db.get_db() as conn:
        for i, p in enumerate(preds):
            h = f"{model}-{i}"
            conn.execute(
                "INSERT INTO predictions (model, alias, request_hash, prediction, features_json)"
                " VALUES (?,?,?,?,?)",
                (model, "Production", h, p, json.dumps(features[i]) if features else None),
            )
            if labels is not None:
                conn.execute(
                    "INSERT INTO ground_truth (request_hash, label) VALUES (?,?)", (h, labels[i])
                )


def _drifted(model="JPCP"):
    _seed(model, [1.0] * 90, [1.0] * 60 + [5.0] * 30)


def _events(model=None, kind=None):
    from examlops import platform_db

    return platform_db.list_drift_events(model=model, drift_kind=kind, last_n=100)


def _audit_actions():
    from examlops import platform_db

    with platform_db.get_db() as conn:
        return [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_events WHERE source='drift-advanced'")
        ]


# -- kill-switch, dry run ------------------------------------------------------------------------


def test_a_real_run_is_refused_without_the_kill_switch_and_audited():
    _drifted()
    rep = sch.AdvancedDriftScheduler().run_cycle()
    assert not rep.ran and "EXAMLOPS_DRIFT_ADVANCED_ENABLED" in rep.note
    assert _events() == []  # nothing detected, nothing written
    assert _audit_actions() == ["drift_advanced_skipped"]


def test_the_default_is_off():
    assert sch.is_enabled() is False


def test_a_dry_run_previews_without_the_switch_and_writes_nothing():
    _drifted()
    rep = sch.AdvancedDriftScheduler(dry_run=True).run_cycle()
    assert rep.ran and rep.dry_run
    concept = next(c for c in rep.checks if c.kind == "concept")
    assert concept.severity == "CRITICAL" and concept.outcome == sch.RECORDED
    assert concept.note.startswith("would record")
    assert _events() == [] and _audit_actions() == []


# -- what an enabled run writes ------------------------------------------------------------------


def test_an_enabled_run_writes_drift_events_the_trigger_consumes(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _drifted()
    rep = sch.AdvancedDriftScheduler().run_cycle()
    assert rep.ran
    ev = _events("JPCP", "concept")
    assert ev and ev[0]["severity"] == "CRITICAL" and ev[0]["metric"] == "abs_error"
    from examlops.data.drift import latest_drift_event

    assert latest_drift_event("JPCP", "concept")["severity"] == "CRITICAL"  # what `trigger` reads
    assert _audit_actions() == ["drift_advanced_cycle"]


def test_data_quality_is_profiled_from_recorded_inputs(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    feats = [{"a": None, "b": 1.0}] * 30 + [{"a": 2.0, "b": 1.0}] * 10
    _seed("Q", [0.5] * 40, features=feats)
    sch.AdvancedDriftScheduler().run_cycle()
    ev = _events("Q", "data_quality")
    assert ev and ev[0]["severity"] in ("WARN", "CRITICAL")


def test_a_label_free_estimate_is_stored_and_never_critical(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _seed("P", [0.95] * 50)
    sch.AdvancedDriftScheduler().run_cycle()
    from examlops.data.evaluation import list_perf_estimates

    assert list_perf_estimates("P")
    assert all(e["severity"] != "CRITICAL" for e in _events("P"))


def test_only_models_with_predictions_are_swept_and_a_model_filter_narrows_it(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _seed("A", [1.0] * 30)
    _seed("B", [1.0] * 30)
    rep = sch.AdvancedDriftScheduler(models=["A"]).run_cycle()
    assert {c.model for c in rep.checks} == {"A"}
    assert sch.AdvancedDriftScheduler(dry_run=True).run_cycle().to_dict()["models"] == 2


# -- cooldown / dedupe ---------------------------------------------------------------------------


def test_an_unchanged_critical_is_not_restated_within_the_cooldown(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _drifted()
    clock = {"t": __import__("time").time()}
    s = sch.AdvancedDriftScheduler(clock=lambda: clock["t"])
    s.run_cycle()
    second = s.run_cycle()
    assert len(_events("JPCP", "concept")) == 1
    assert next(c for c in second.checks if c.kind == "concept").outcome == sch.DEDUPED
    clock["t"] += 3601  # the cooldown has passed: restated once
    s.run_cycle()
    assert len(_events("JPCP", "concept")) == 2


def test_a_changed_severity_is_recorded_at_once(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    from examlops.data.drift import record_drift_event

    record_drift_event("JPCP", "concept", severity="OK", metric="abs_error")
    _drifted()
    sch.AdvancedDriftScheduler().run_cycle()
    assert [e["severity"] for e in _events("JPCP", "concept")][:1] == ["CRITICAL"]


def test_a_healthy_model_is_recorded_once_then_deduped(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _seed("H", [1.0] * 90, [1.0] * 90)
    s = sch.AdvancedDriftScheduler()
    s.run_cycle()
    s.run_cycle()
    assert len(_events("H", "concept")) == 1


def test_insufficient_labels_is_skipped_not_recorded(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _seed("S", [1.0] * 4, [1.0] * 4)
    rep = sch.AdvancedDriftScheduler().run_cycle()
    assert next(c for c in rep.checks if c.kind == "concept").outcome == sch.SKIPPED
    assert _events("S", "concept") == []


# -- lease + isolation ---------------------------------------------------------------------------


def test_a_second_scheduler_is_blocked_by_the_lease(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _drifted()
    from examlops.coordination import get_coordinator

    assert get_coordinator().try_lock(sch.LEASE_KEY, "other-host:1", 300)
    rep = sch.AdvancedDriftScheduler().run_cycle()
    assert not rep.ran and "lease" in rep.note
    assert _events() == []
    get_coordinator().unlock(sch.LEASE_KEY, "other-host:1")
    assert sch.AdvancedDriftScheduler().run_cycle().ran  # and the lease is released after a run
    assert sch.AdvancedDriftScheduler().run_cycle().ran


def test_one_crashing_detector_does_not_stop_the_sweep(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    _seed("A", [1.0] * 30)

    def boom(*_a, **_k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(sch, "detect_concept_drift", boom)
    rep = sch.AdvancedDriftScheduler().run_cycle()
    assert next(c for c in rep.checks if c.kind == "concept").outcome == sch.FAILED
    assert any(c.kind == "data_quality" for c in rep.checks)


# -- the CLI -------------------------------------------------------------------------------------


def test_cli_refuses_without_the_switch_and_previews_with_dry_run():
    from typer.testing import CliRunner

    from examlops.cli.commands.drift import app

    _drifted()
    runner = CliRunner()
    refused = runner.invoke(app, ["run-advanced", "--once"])
    assert refused.exit_code == 1
    preview = runner.invoke(app, ["run-advanced", "--once", "--dry-run"])
    assert preview.exit_code == 0 and _events() == []


def test_cli_loop_mode_refuses_json():
    from typer.testing import CliRunner

    from examlops.cli import _output
    from examlops.cli.commands.drift import app

    _output.json_mode = True
    try:
        res = CliRunner().invoke(app, ["run-advanced"])
    finally:
        _output.json_mode = False
    assert res.exit_code == 2


# -- the detector seam + River adapter -----------------------------------------------------------


def _fake_river(drifts_at):
    """A stand-in ``river.drift.ADWIN`` that reports drift on the given update indices."""

    class ADWIN:
        def __init__(self):
            self.n = -1
            self.drift_detected = False

        def update(self, _x):
            self.n += 1
            self.drift_detected = self.n in drifts_at

    river = types.ModuleType("river")
    drift = types.ModuleType("river.drift")
    drift.ADWIN = ADWIN
    river.drift = drift
    return {"river": river, "river.drift": drift}


def test_the_default_detector_is_the_builtin_and_is_recorded():
    _drifted()
    res = da.detect_concept_drift("JPCP", window=30, persist=False)
    assert res.detail["detector"] == "builtin" and "detector_fallback" not in res.detail
    assert res.severity in ("WARN", "CRITICAL")


def test_river_adwin_confirms_a_drift_inside_the_recent_window(monkeypatch):
    monkeypatch.setitem(sys.modules, "river", _fake_river({70})["river"])
    monkeypatch.setitem(sys.modules, "river.drift", _fake_river({70})["river.drift"])
    _drifted()
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="river-adwin")
    assert res.detail["detector"] == "river-adwin" and res.detail["adwin_drift"] is True
    assert res.severity == "CRITICAL"


def test_river_adwin_that_saw_no_drift_never_reaches_critical(monkeypatch):
    fake = _fake_river(set())
    monkeypatch.setitem(sys.modules, "river", fake["river"])
    monkeypatch.setitem(sys.modules, "river.drift", fake["river.drift"])
    _drifted()
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="river-adwin")
    assert res.detail["adwin_drift"] is False and res.severity == "WARN"


def test_a_missing_river_degrades_to_the_builtin_and_says_so(monkeypatch):
    monkeypatch.setitem(sys.modules, "river", None)  # makes `import river` raise ImportError
    monkeypatch.setitem(sys.modules, "river.drift", None)
    _drifted()
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="river-adwin")
    assert res.detail["detector"] == "builtin"
    assert "not installed" in res.detail["detector_fallback"]
    assert res.severity in ("WARN", "CRITICAL")


def test_an_unknown_detector_falls_back_with_a_reason():
    name, _fn, reason = da.resolve_concept_detector("no-such-detector")
    assert name == "builtin" and "unknown detector" in reason


def test_the_detector_is_selectable_from_the_environment(monkeypatch):
    monkeypatch.setenv(da.CONCEPT_DETECTOR_ENV, "river-adwin")
    assert da.resolve_concept_detector()[0] == "river-adwin"


def test_a_lost_drift_advanced_audit_is_counted(monkeypatch):
    from examlops.data import audit

    audit.reset_dropped_audit_events()

    def _boom(*_a, **_k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "write_audit_event", _boom)
    rep = sch.AdvancedDriftScheduler().run_cycle()  # kill-switch off: refused, and audited
    assert not rep.ran  # the loss does not change the outcome...
    assert audit.dropped_audit_events().get("drift_advanced_skipped") == 1  # ...and is not hidden

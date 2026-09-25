"""ADR 0022 decisions 3 and 4 — the gates the advanced-drift detectors were missing.

* Decision 4: "the existing cooldown-aware auto-retrain consumes concept **+ estimated-perf**
  signals with severity". A label-free estimate used to be written as a WARN that nothing
  consumed. Now an estimate *confirmed by realized labels* is CRITICAL and `exa drift trigger`
  acts on it; an unconfirmed estimate still only warns. The trigger also stops letting a newer
  estimate WARN hide an older realized-error CRITICAL.
* Decision 3: "C5 monitors A5's flagged inputs". The inference ingress refused bad payloads and
  recorded nothing, so only a hand-typed `exa drift profile --bad-payloads N` folded them in. The
  ingress now counts every contract rejection and the scheduler folds them into the profile.
"""

from __future__ import annotations

import asyncio
import datetime
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import drift_advanced as da  # noqa: E402
from examlops.drift_advanced import scheduler as sch  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for env in (
        sch.ENABLED_ENV,
        sch.COOLDOWN_ENV,
        sch.REJECTION_WINDOW_ENV,
        da.CONCEPT_DETECTOR_ENV,
        da.PERF_ESTIMATOR_ENV,
        da.QUALITY_PROFILER_ENV,
    ):
        monkeypatch.delenv(env, raising=False)
    from examlops import platform_db

    platform_db.init_db()


_SEQ = itertools.count()


def _insert(model, pred, label=None, features=None, h=None):
    from examlops import platform_db

    h = h or f"{model}-{next(_SEQ)}"
    with platform_db.get_db() as conn:
        conn.execute(
            "INSERT INTO predictions (model, alias, request_hash, prediction, features_json)"
            " VALUES (?,?,?,?,?)",
            (model, "Production", h, pred, json.dumps(features) if features else None),
        )
        if label is not None:
            conn.execute("INSERT INTO ground_truth (request_hash, label) VALUES (?,?)", (h, label))


def _degraded_classifier(model="CLF", *, labelled_wrong=True, n=40):
    """Uncertain recent predictions (estimate ≈ 0.52) — optionally with labels that confirm it."""
    for i in range(n):
        label = (0.0 if i % 4 else 1.0) if labelled_wrong else 1.0
        _insert(model, 0.52, label)


def _events(model, kind="concept"):
    from examlops import platform_db

    return platform_db.list_drift_events(model=model, drift_kind=kind, last_n=100)


# -- decision 4: a confirmed estimate is a severity-bearing retrain signal ------------------------


def test_an_estimate_confirmed_by_labels_is_critical_and_says_so():
    _degraded_classifier(labelled_wrong=True)  # realized accuracy 0.25 vs baseline 0.95
    res = da.estimate_performance("CLF", baseline=0.95)
    assert res["warn"] and res["confirmed"] and res["severity"] == "CRITICAL"
    ev = _events("CLF")[0]
    assert ev["severity"] == "CRITICAL"
    assert ev["detail"]["label_free"] is True and ev["detail"]["confirmed_by_labels"] is True
    assert ev["detail"]["realized_n"] == 40


def test_labels_that_contradict_the_estimate_keep_it_a_warning():
    _degraded_classifier(labelled_wrong=False)  # every 0.52 prediction was right: realized 1.0
    res = da.estimate_performance("CLF", baseline=0.95)
    assert res["warn"] and not res["confirmed"] and res["severity"] == "WARN"


def test_too_few_labels_cannot_confirm(monkeypatch):
    _degraded_classifier(labelled_wrong=True, n=da.PERF_CONFIRM_MIN_LABELS - 1)
    res = da.estimate_performance("CLF", baseline=0.95)
    assert res["severity"] == "WARN" and res["realized_n"] == da.PERF_CONFIRM_MIN_LABELS - 1


def test_realized_is_measured_on_the_recent_window_not_the_whole_history():
    for _ in range(300):  # a long, accurate past...
        _insert("CLF", 0.99, 1.0)
    for i in range(40):  # ...then a degraded present
        _insert("CLF", 0.52, 0.0 if i % 4 else 1.0)
    res = da.estimate_performance("CLF", baseline=0.95, window=40)
    assert res["realized"] == pytest.approx(0.25) and res["confirmed"] is True


def test_the_trigger_fires_on_a_confirmed_estimate_and_audits_the_signal(monkeypatch):
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    platform_db.set_drift_auto_retrain("CLF", enabled=True, dataset_name="DS")
    _degraded_classifier(labelled_wrong=True)
    da.estimate_performance("CLF", baseline=0.95)

    res = CliRunner().invoke(app, ["--json", "drift", "trigger", "--dry-run"])
    assert res.exit_code == 0, res.output
    body = json.loads(res.output)
    assert [t["model"] for t in body["triggered"]] == ["CLF"]
    assert body["triggered"][0]["signal"] == "estimated_performance"


def test_an_unconfirmed_estimate_never_fires_the_trigger():
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    platform_db.set_drift_auto_retrain("CLF", enabled=True, dataset_name="DS")
    for _ in range(40):
        _insert("CLF", 0.52)  # no labels at all
    assert da.estimate_performance("CLF", baseline=0.95)["severity"] == "WARN"

    body = json.loads(CliRunner().invoke(app, ["--json", "drift", "trigger", "--dry-run"]).output)
    assert body["triggered"] == []


def test_a_forged_unconfirmed_critical_estimate_is_not_a_signal():
    """Only `confirmed_by_labels` makes an estimate actionable — not the severity a row claims."""
    from examlops import platform_db

    platform_db.record_drift_event(
        "CLF", "concept", severity="CRITICAL", score=0.4, detail={"label_free": True}
    )
    assert da.concept_retrain_signal("CLF") is None


def test_a_newer_estimate_warning_no_longer_hides_a_realized_critical():
    from examlops import platform_db

    platform_db.record_drift_event("JPCP", "concept", severity="CRITICAL", score=4.2)
    platform_db.record_drift_event(
        "JPCP", "concept", severity="WARN", score=0.2, detail={"label_free": True}
    )
    sig = da.concept_retrain_signal("JPCP")
    assert sig is not None and sig["signal"] == "realized_error" and sig["score"] == 4.2


def test_a_recovered_realized_error_clears_the_signal():
    from examlops import platform_db

    platform_db.record_drift_event("JPCP", "concept", severity="CRITICAL", score=4.2)
    platform_db.record_drift_event("JPCP", "concept", severity="OK", score=0.1)
    assert da.concept_retrain_signal("JPCP") is None


def test_the_scheduler_writes_a_confirmed_estimate_as_critical(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    from examlops import platform_db

    # An earlier, healthy estimate becomes the scheduler's baseline.
    platform_db.record_perf_estimate("CLF", "accuracy", estimated=0.95)
    _degraded_classifier(labelled_wrong=True)
    rep = sch.AdvancedDriftScheduler().run_cycle()
    est = next(c for c in rep.checks if c.kind == "estimate")
    assert est.severity == "CRITICAL" and est.outcome == sch.RECORDED
    crit = [e for e in _events("CLF") if (e["detail"] or {}).get("label_free")]
    assert crit and crit[0]["severity"] == "CRITICAL"
    assert da.concept_retrain_signal("CLF")["signal"] == "estimated_performance"


# -- concept stream order -------------------------------------------------------------------------


def test_labelled_pairs_are_in_arrival_order_even_when_labels_arrive_out_of_order():
    from examlops import platform_db

    for i in range(5):
        _insert("M", float(i), h=f"h{i}")
    with platform_db.get_db() as conn:  # labels land newest-request-first
        for i in reversed(range(5)):
            conn.execute(
                "INSERT INTO ground_truth (request_hash, label) VALUES (?,?)", (f"h{i}", i)
            )
    assert [p["prediction"] for p in da.labelled_pairs("M")] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_labelled_pairs_are_bounded(monkeypatch):
    monkeypatch.setattr(da, "MAX_LABELLED_PAIRS", 3)
    for i in range(6):
        _insert("M", float(i), float(i))
    assert [p["prediction"] for p in da.labelled_pairs("M")] == [3.0, 4.0, 5.0]


# -- decision 3: A5 contract rejections reach the profile -----------------------------------------


def test_rejections_are_counted_per_model_case_insensitively_within_the_window():
    from examlops.data import drift

    now = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
    for _ in range(3):
        drift.record_inference_rejection("JPCP", "missing_field", now=now)
    drift.record_inference_rejection("jpcp", "embedding_dim", now=now)
    drift.record_inference_rejection("JPCP", now=now - datetime.timedelta(hours=2))  # too old
    assert drift.count_inference_rejections("Jpcp", since_s=3600, now=now) == 4
    assert drift.count_inference_rejections("other", since_s=3600, now=now) == 0
    assert drift.rejection_models(since_s=3600, now=now) == ["jpcp"]


def test_a_burst_is_one_row_per_minute_not_one_per_request():
    from examlops import platform_db
    from examlops.data import drift

    now = datetime.datetime(2026, 9, 25, 12, 0, 30, tzinfo=datetime.UTC)
    for _ in range(50):
        drift.record_inference_rejection("JPCP", "invalid", now=now)
    with platform_db.get_db() as conn:
        rows = conn.execute("SELECT count FROM inference_rejections").fetchall()
    assert [r["count"] for r in rows] == [50]


def test_untrusted_model_names_and_reasons_cannot_grow_the_key_space():
    from examlops import platform_db
    from examlops.data import drift

    now = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
    drift.record_inference_rejection("../../etc/passwd", "x" * 500, now=now)
    drift.record_inference_rejection("a" * 400, "missing_field", now=now)
    drift.record_inference_rejection(None, "missing_field", now=now)
    with platform_db.get_db() as conn:
        rows = {
            (r["model"], r["reason"]) for r in conn.execute("SELECT * FROM inference_rejections")
        }
    assert rows == {
        ("<invalid>", "invalid"),
        ("<invalid>", "missing_field"),
        ("<unknown>", "missing_field"),
    }
    assert drift.rejection_models(since_s=3600, now=now) == []  # placeholders are not models


@pytest.mark.parametrize(
    ("errors", "reason"),
    [
        (["missing required field 'embedding'"], "missing_field"),
        (["embedding dim 2 != 384"], "embedding_dim"),
        (["num_nodes=-1 < 0"], "invalid"),
        (["dimension=7 > 4"], "invalid"),  # a field *named* like "dim" is not a width mismatch
    ],
)
def test_contract_errors_map_onto_a_closed_reason_set(errors, reason):
    from examlops.data.drift import classify_rejection

    assert classify_rejection(errors) == reason


def test_the_scheduler_folds_rejections_into_the_quality_profile(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    from examlops.data import drift

    for _ in range(5):
        _insert("Q", 0.5, features={"a": 1.0})  # 5 clean rows
    for _ in range(20):
        drift.record_inference_rejection("Q", "missing_field")
    rep = sch.AdvancedDriftScheduler().run_cycle()
    q = next(c for c in rep.checks if c.model == "Q" and c.kind == "data_quality")
    assert q.severity == "CRITICAL" and q.score == pytest.approx(20 / 25)
    assert _events("Q", "data_quality")[0]["detail"]["bad_payloads"] == 20


def test_a_model_whose_every_request_was_refused_is_still_swept(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    from examlops.data import drift

    for _ in range(3):
        drift.record_inference_rejection("Refused", "missing_field")
    rep = sch.AdvancedDriftScheduler().run_cycle()
    q = next(c for c in rep.checks if c.kind == "data_quality" and c.model.lower() == "refused")
    assert q.severity == "CRITICAL" and q.outcome == sch.RECORDED


def test_rejections_outside_the_lookback_are_not_folded(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    monkeypatch.setenv(sch.REJECTION_WINDOW_ENV, "60")
    from examlops.data import drift

    _insert("Q", 0.5, features={"a": 1.0})
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    for _ in range(20):
        drift.record_inference_rejection("Q", "missing_field", now=old)
    rep = sch.AdvancedDriftScheduler().run_cycle()
    q = next(c for c in rep.checks if c.kind == "data_quality")
    assert q.severity == "OK"


def test_cli_profile_reads_the_rejection_log_unless_told_a_count():
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.data import drift

    for _ in range(4):
        _insert("Q", 0.5, features={"a": 1.0})
    for _ in range(6):
        drift.record_inference_rejection("Q", "invalid")
    res = CliRunner().invoke(app, ["--json", "drift", "profile", "Q"])
    assert res.exit_code == 0, res.output
    body = json.loads(res.output)
    assert body["bad_payloads"] == 6 and body["severity"] == "CRITICAL"

    res = CliRunner().invoke(app, ["--json", "drift", "profile", "Q", "--bad-payloads", "0"])
    assert json.loads(res.output)["bad_payloads"] == 0


def test_the_ingress_counts_a_contract_rejection(monkeypatch):
    pytest.importorskip("ray")
    import serving.inference_pipeline.app as pipeline
    from examlops.data import drift

    monkeypatch.setattr(
        pipeline, "_validate_payload", lambda _b: (False, ["missing required field 'embedding'"])
    )

    class _Transformer:
        class handle_batch:  # noqa: N801 - mirrors the Ray handle attribute
            @staticmethod
            def remote(_body):  # pragma: no cover - a rejected request never reaches it
                raise AssertionError("a rejected request was forwarded")

    ingress = pipeline.InferencePipelineIngress(_Transformer())
    response = asyncio.run(ingress.infer({"model_name": "JPCP"}))
    assert response.status_code == 422
    assert drift.count_inference_rejections("JPCP", since_s=3600) == 1


def test_a_failed_rejection_write_still_answers_422(monkeypatch):
    pytest.importorskip("ray")
    import serving.inference_pipeline.app as pipeline
    from examlops.data import drift

    monkeypatch.setattr(pipeline, "_validate_payload", lambda _b: (False, ["bad"]))

    def _down(*_a, **_k):
        raise RuntimeError("platform store unreachable")

    monkeypatch.setattr(drift, "record_inference_rejection", _down)
    ingress = pipeline.InferencePipelineIngress(object())
    assert asyncio.run(ingress.infer({"model_name": "JPCP"})).status_code == 422


# -- review fixes ---------------------------------------------------------------------------------


def _regressor_whose_error_never_changed(model="REG"):
    """300 steady predictions, then 40 widely spread ones — MAE is 5.0 in both periods."""
    for i in range(300):
        pred = 100.0 + (i % 2)
        _insert(model, pred, pred + 5.0)
    for i in range(40):
        pred = 50.0 if i % 2 else 150.0
        _insert(model, pred, pred + 5.0)


def test_a_regression_estimate_is_not_confirmed_against_a_baseline_in_other_units():
    """The stability-proxy baseline (~0.99) is not a realized 1/(1+MAE) (~0.17): no real drop."""
    _regressor_whose_error_never_changed()
    res = da.estimate_performance("REG", baseline=0.99, window=40)
    assert res["warn"] is True  # the spread really did grow
    assert res["realized"] == pytest.approx(1 / 6)
    assert res["confirmed"] is False and res["severity"] == "WARN"
    assert res["event_detail"]["confirm_basis"] == "realized_reference"
    assert res["event_detail"]["realized_baseline"] == pytest.approx(1 / 6)


def test_a_regression_estimate_is_confirmed_when_realized_error_really_grew():
    for i in range(300):
        pred = 100.0 + (i % 2)
        _insert("REG", pred, pred + 5.0)  # MAE 5
    for i in range(40):
        pred = 50.0 if i % 2 else 150.0
        _insert("REG", pred, pred + 40.0)  # MAE 40
    res = da.estimate_performance("REG", baseline=0.99, window=40)
    assert res["confirmed"] is True and res["severity"] == "CRITICAL"


def test_a_regression_estimate_without_a_labelled_reference_cannot_confirm():
    for i in range(40):
        pred = 50.0 if i % 2 else 150.0
        _insert("REG", pred, pred + 40.0)
    res = da.estimate_performance("REG", baseline=0.99, window=40)
    assert res["warn"] is True and res["confirmed"] is False


def test_a_recovered_estimate_clears_the_confirmed_signal():
    """A confirmed CRITICAL must not stay the newest estimate signal after the model recovers."""
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    platform_db.set_drift_auto_retrain("CLF", enabled=True, dataset_name="DS")
    _degraded_classifier(labelled_wrong=True)
    da.estimate_performance("CLF", baseline=0.95, window=40)
    assert da.concept_retrain_signal("CLF") is not None
    for _ in range(40):
        _insert("CLF", 0.99, 1.0)  # healthy again
    res = da.estimate_performance("CLF", baseline=0.95, window=40)
    assert res["severity"] == "OK"
    assert da.concept_retrain_signal("CLF") is None
    body = json.loads(CliRunner().invoke(app, ["--json", "drift", "trigger", "--dry-run"]).output)
    assert body["triggered"] == []


def test_the_scheduler_records_a_recovery_even_within_the_cooldown(monkeypatch):
    monkeypatch.setenv(sch.ENABLED_ENV, "1")
    from examlops import platform_db

    platform_db.record_perf_estimate("CLF", "accuracy", estimated=0.95)
    _degraded_classifier(labelled_wrong=True)
    sch.AdvancedDriftScheduler().run_cycle()
    assert da.concept_retrain_signal("CLF") is not None
    for _ in range(200):
        _insert("CLF", 0.99, 1.0)
    rep = sch.AdvancedDriftScheduler().run_cycle()  # seconds later: well inside the cooldown
    est = next(c for c in rep.checks if c.kind == "estimate")
    assert est.severity == "OK" and est.outcome == sch.RECORDED
    assert da.concept_retrain_signal("CLF") is None


def test_many_estimate_events_do_not_hide_a_realized_critical():
    """The newest event of each source is found by its own query, not by filtering a page."""
    from examlops import platform_db

    platform_db.record_drift_event("JPCP", "concept", severity="CRITICAL", score=4.2)
    for _ in range(120):
        platform_db.record_drift_event(
            "JPCP", "concept", severity="WARN", score=0.2, detail={"label_free": True}
        )
    sig = da.concept_retrain_signal("JPCP")
    assert sig is not None and sig["signal"] == "realized_error"


def test_rejection_models_are_bounded_and_most_rejected_first(monkeypatch):
    from examlops.data import drift

    monkeypatch.setattr(drift, "MAX_REJECTION_MODELS", 2, raising=False)
    now = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
    for name, n in (("a", 1), ("b", 5), ("c", 3)):
        for _ in range(n):
            drift.record_inference_rejection(name, now=now)
    for _ in range(9):
        drift.record_inference_rejection("../bad", now=now)  # a placeholder never takes a slot
    assert drift.rejection_models(since_s=3600, now=now) == ["b", "c"]


def test_old_rejection_buckets_are_pruned_as_new_ones_arrive(monkeypatch):
    from examlops import platform_db
    from examlops.data import drift

    monkeypatch.setattr(drift, "_last_rejection_prune", None, raising=False)
    now = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
    old = now - datetime.timedelta(days=30)  # beyond the 7-day REJECTION_RETENTION_S
    drift.record_inference_rejection("stale", now=old)
    drift.record_inference_rejection("fresh", now=now)
    with platform_db.get_db() as conn:
        models = {r["model"] for r in conn.execute("SELECT model FROM inference_rejections")}
    assert models == {"fresh"}


def test_a_slow_rejection_write_does_not_hold_the_422(monkeypatch):
    pytest.importorskip("ray")
    import time

    import serving.inference_pipeline.app as pipeline
    from examlops.data import drift

    monkeypatch.setattr(pipeline, "_validate_payload", lambda _b: (False, ["bad"]))
    monkeypatch.setattr(pipeline, "_REJECTION_RECORD_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(drift, "record_inference_rejection", lambda *_a, **_k: time.sleep(2))
    ingress = pipeline.InferencePipelineIngress(object())

    async def _timed():  # timed inside the loop: asyncio.run itself joins the worker on close
        started = time.monotonic()
        response = await ingress.infer({"model_name": "JPCP"})
        return response, time.monotonic() - started

    response, elapsed = asyncio.run(_timed())
    assert response.status_code == 422
    assert elapsed < 1.0


@pytest.mark.parametrize(
    ("path", "values"),
    [
        ("drift estimate", {"model": "CLF", "baseline": 1.0}),
        ("drift concept", {"model": "CLF", "window": 5}),
    ],
)
def test_a_viewer_cannot_write_a_retrain_signal_from_the_console(path, values, tmp_path):
    """Both commands persist concept events `exa drift trigger` retrains on (or an OK that clears
    one), with caller-chosen thresholds — the dashboard console must require admin to run them."""
    from examlops.cli import surface

    by_path = {c["path"]: c for c in surface.build_catalog()["commands"]}
    inv = surface.build_argv(by_path[path], values, workspace=tmp_path)
    assert inv.tier == surface.ADMIN

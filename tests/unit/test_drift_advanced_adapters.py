"""ADR 0022 — the named detectors behind the advanced-drift seams.

Decisions 1–3 name River (ADWIN/DDM), Evidently, NannyML (CBPE/DLE) and whylogs. Each is an
optional, lazily imported adapter with a pure-Python counterpart; a missing library degrades to the
builtin and the result says so. Two kinds of test live here:

* hermetic ones, with stand-ins shaped exactly like the libraries' real return values (verified
  against river 0.26.1, evidently 0.7.23, nannyml 0.13.1 and whylogs 1.6.4), which always run;
* ``importorskip`` ones that drive the **real** library when it is installed
  (``pip install 'examlops[drift-advanced]'``) — they skip on a bare install rather than pass.
"""

from __future__ import annotations

import random
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import drift_advanced as da  # noqa: E402
from examlops.drift_advanced import adapters  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for env in (da.CONCEPT_DETECTOR_ENV, da.PERF_ESTIMATOR_ENV, da.QUALITY_PROFILER_ENV):
        monkeypatch.delenv(env, raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _seed(model, preds, labels=None, features=None):
    import json

    from examlops import platform_db

    with platform_db.get_db() as conn:
        for i, p in enumerate(preds):
            h = f"{model}-{i}"
            conn.execute(
                "INSERT INTO predictions (model, alias, request_hash, prediction, features_json)"
                " VALUES (?,?,?,?,?)",
                (model, "Production", h, p, json.dumps(features[i]) if features else None),
            )
            if labels is not None and labels[i] is not None:
                conn.execute(
                    "INSERT INTO ground_truth (request_hash, label) VALUES (?,?)", (h, labels[i])
                )


def _noisy_stream(n_base=200, n_recent=60, shift=0.0, seed=7):
    rng = random.Random(seed)
    base = [abs(rng.gauss(0, 1)) for _ in range(n_base)]
    recent = [abs(rng.gauss(0, 1)) + shift for _ in range(n_recent)]
    return base, recent


# -- DDM (pure Python) ---------------------------------------------------------------------------


def test_pure_ddm_signals_a_rising_failure_rate_and_stays_quiet_on_a_stable_one():
    base, recent = _noisy_stream(shift=2.0)
    sev, _z, extra = da._ddm_concept(base, recent)
    assert extra["ddm_drift"] is True and sev == "CRITICAL"

    base, recent = _noisy_stream(shift=0.0)
    sev, _z, extra = da._ddm_concept(base, recent)
    assert extra["ddm_drift"] is False and sev in ("OK", "WARN")


def test_ddm_never_escalates_an_improvement():
    """A falling error is not concept drift, even if the failure stream changed."""
    base, recent = _noisy_stream(shift=0.0)
    better = [e * 0.01 for e in recent]
    sev, _z, extra = da._ddm_concept(base, better)
    assert sev != "CRITICAL"


def test_ddm_is_selectable_and_recorded_on_the_event():
    _seed("JPCP", [1.0] * 90, [1.0] * 60 + [5.0] * 30)
    res = da.detect_concept_drift("JPCP", window=30, detector="ddm")
    assert res.detail["detector"] == "ddm" and "detector_fallback" not in res.detail
    assert res.severity == "CRITICAL"
    from examlops import platform_db

    ev = platform_db.list_drift_events(model="JPCP", drift_kind="concept")[0]
    assert ev["detail"]["detector"] == "ddm" and ev["detail"]["ddm_drift"] is True


def test_pure_ddm_resets_after_a_drift():
    det = adapters.PureDDM(warm_start=5)
    for _ in range(40):
        det.update(False)
    for _ in range(10):
        det.update(True)
        if det.drift_detected:
            break
    assert det.drift_detected
    det.update(False)  # the next update starts a fresh window
    assert not det.drift_detected and det._n == 1


# -- River DDM adapter ---------------------------------------------------------------------------


def _fake_river_binary():
    """``river.drift.binary.DDM`` stand-in: River's own update rule (== :class:`PureDDM`)."""

    class DDM(adapters.PureDDM):
        instances: list = []

        def __init__(self, warm_start=30, warning_threshold=2.0, drift_threshold=3.0):
            super().__init__(warm_start, warning_threshold, drift_threshold)
            DDM.instances.append(
                {"warm_start": warm_start, "warning": warning_threshold, "drift": drift_threshold}
            )

    river = types.ModuleType("river")
    drift = types.ModuleType("river.drift")
    binary = types.ModuleType("river.drift.binary")
    binary.DDM = DDM
    drift.binary = binary
    river.drift = drift
    return {"river": river, "river.drift": drift, "river.drift.binary": binary}, DDM


def test_river_ddm_runs_the_library_detector_with_the_documented_thresholds(monkeypatch):
    mods, ddm_cls = _fake_river_binary()
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    _seed("JPCP", [1.0] * 90, [1.0] * 60 + [5.0] * 30)
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="river-ddm")
    assert res.detail["detector"] == "river-ddm" and res.severity == "CRITICAL"
    assert ddm_cls.instances[-1] == {"warm_start": 30, "warning": 2.0, "drift": 3.0}


def test_a_missing_river_ddm_degrades_and_says_why(monkeypatch):
    monkeypatch.setitem(sys.modules, "river", None)
    monkeypatch.setitem(sys.modules, "river.drift", None)
    monkeypatch.setitem(sys.modules, "river.drift.binary", None)
    _seed("JPCP", [1.0] * 90, [1.0] * 60 + [5.0] * 30)
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="river-ddm")
    assert res.detail["detector"] == "builtin"
    assert "river-ddm unavailable" in res.detail["detector_fallback"]
    assert "not installed" in res.detail["detector_fallback"]


def test_real_river_ddm_agrees_with_the_pure_python_ddm():
    pytest.importorskip("river")
    base, recent = _noisy_stream(shift=1.5)
    bits = adapters.failure_bits(base, recent)
    assert adapters.scan_ddm(adapters.river_ddm(), bits, len(base)) == adapters.scan_ddm(
        adapters.PureDDM(), bits, len(base)
    )


# -- Evidently adapter ---------------------------------------------------------------------------


def _fake_evidently(p_value, method="K-S p_value", threshold=0.05, seen=None):
    """Evidently 0.7 stand-in: ``Report([ValueDrift]).run(...).dict()`` shape, verbatim."""
    ev = types.ModuleType("evidently")
    metrics = types.ModuleType("evidently.metrics")

    class DataDefinition:
        def __init__(self, numerical_columns=None):
            self.numerical_columns = numerical_columns

    class Dataset:
        @staticmethod
        def from_pandas(df, data_definition=None):
            return {"df": df, "definition": data_definition}

    class ValueDrift:
        def __init__(self, column):
            self.column = column

    class _Snapshot:
        def dict(self):
            return {
                "metrics": [
                    {
                        "metric_name": f"ValueDrift(column=error,method={method})",
                        "config": {
                            "type": "evidently:metric_v2:ValueDrift",
                            "column": "error",
                            "method": method,
                            "threshold": threshold,
                        },
                        "value": p_value,
                    }
                ],
                "tests": [],
            }

    class Report:
        def __init__(self, metrics_):
            self.metrics = metrics_

        def run(self, current_data, reference_data):
            if seen is not None:
                seen["current"] = list(current_data["df"]["error"])
                seen["reference"] = list(reference_data["df"]["error"])
                seen["column"] = self.metrics[0].column
            return _Snapshot()

    ev.DataDefinition, ev.Dataset, ev.Report = DataDefinition, Dataset, Report
    metrics.ValueDrift = ValueDrift
    ev.metrics = metrics
    return {"evidently": ev, "evidently.metrics": metrics}


def _install(monkeypatch, mods):
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_evidently_drift_on_a_higher_error_is_critical_and_records_its_statistic(monkeypatch):
    seen: dict = {}
    _install(monkeypatch, _fake_evidently(1e-9, seen=seen))
    _seed("JPCP", [1.0] * 90, [1.0] * 60 + [5.0] * 30)
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="evidently")
    assert res.severity == "CRITICAL" and res.detail["detector"] == "evidently"
    assert res.detail["evidently_drift"] is True
    assert res.detail["evidently_method"] == "K-S p_value"
    assert seen["column"] == "error" and len(seen["current"]) == 30 and seen["current"][0] == 4.0


def test_evidently_without_drift_never_reaches_critical(monkeypatch):
    _install(monkeypatch, _fake_evidently(0.9))
    _seed("JPCP", [1.0] * 90, [1.0] * 60 + [5.0] * 30)
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="evidently")
    assert res.detail["evidently_drift"] is False and res.severity == "WARN"


def test_evidently_distance_methods_compare_the_other_way(monkeypatch):
    _install(
        monkeypatch, _fake_evidently(0.4, method="Wasserstein distance (normed)", threshold=0.1)
    )
    base, recent = _noisy_stream(shift=2.0)
    verdict = adapters.evidently_value_drift(base, recent)
    assert verdict["drift"] is True


def test_a_malformed_evidently_result_is_unavailable_not_a_crash(monkeypatch):
    mods = _fake_evidently(0.01)

    class Broken:
        def __init__(self, *_a):
            pass

        def run(self, **_k):
            return types.SimpleNamespace(dict=lambda: {"metrics": []})

    mods["evidently"].Report = Broken
    _install(monkeypatch, mods)
    _seed("JPCP", [1.0] * 90, [1.0] * 60 + [5.0] * 30)
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="evidently")
    assert res.detail["detector"] == "builtin"
    assert "unexpected Evidently result shape" in res.detail["detector_fallback"]


def test_a_library_that_raises_at_run_time_degrades_and_says_so(monkeypatch):
    def boom(_b, _r):
        raise ValueError("bad input")

    monkeypatch.setitem(da.CONCEPT_DETECTORS, "flaky", boom)
    _seed("JPCP", [1.0] * 90, [1.0] * 60 + [5.0] * 30)
    res = da.detect_concept_drift("JPCP", window=30, persist=False, detector="flaky")
    assert res.detail["detector"] == "builtin"
    assert res.detail["detector_fallback"] == "flaky failed: ValueError: bad input"


def test_real_evidently_detects_a_shift_in_the_error_distribution():
    pytest.importorskip("evidently")
    base, recent = _noisy_stream(shift=2.0)
    assert adapters.evidently_value_drift(base, recent)["drift"] is True
    base, recent = _noisy_stream(shift=0.0)
    assert adapters.evidently_value_drift(base, recent)["drift"] is False


# -- NannyML estimator ---------------------------------------------------------------------------


def _fake_nannyml(value, calls):
    """nannyml stand-in: ``Estimator.fit(ref)``, ``.estimate(ana).filter(period=…).to_df()``."""
    import pandas as pd

    nml = types.ModuleType("nannyml")

    class _Result:
        def __init__(self, metric):
            self.metric = metric

        def filter(self, period):
            calls.append(("filter", period))
            return self

        def to_df(self):
            return pd.DataFrame({(self.metric, "value"): [value]})

    class CBPE:
        def __init__(self, **kw):
            calls.append(("CBPE", kw))

        def fit(self, ref):
            calls.append(("fit", list(ref.columns), len(ref)))

        def estimate(self, ana):
            calls.append(("estimate", list(ana.columns), len(ana)))
            return _Result("accuracy")

    class DLE(CBPE):
        def __init__(self, **kw):
            calls.append(("DLE", kw))

        def estimate(self, ana):
            calls.append(("estimate", list(ana.columns), len(ana)))
            return _Result("mae")

    nml.CBPE, nml.DLE = CBPE, DLE
    return nml


def _labelled_classifier(model, n_ref=80, n_recent=40):
    rng = random.Random(3)
    ref = [rng.random() for _ in range(n_ref)]
    _seed(model, ref, [float(p >= 0.5) if i % 2 else float(p < 0.5) for i, p in enumerate(ref)])
    from examlops import platform_db

    with platform_db.get_db() as conn:  # unlabelled recent traffic
        for i in range(n_recent):
            conn.execute(
                "INSERT INTO predictions (model, alias, request_hash, prediction) VALUES (?,?,?,?)",
                (model, "Production", f"{model}-r{i}", 0.55),
            )


def test_nannyml_cbpe_is_fitted_on_labelled_reference_and_applied_to_recent(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "nannyml", _fake_nannyml(0.61, calls))
    _labelled_classifier("CLF")
    res = da.estimate_performance("CLF", baseline=0.9, window=40, estimator="nannyml")
    assert res["estimator"] == "nannyml" and res["method"] == "nannyml-cbpe"
    assert res["estimated"] == pytest.approx(0.61) and res["warn"] is True
    kinds = {c[0]: c for c in calls}
    assert kinds["CBPE"][1]["problem_type"] == "classification_binary"
    assert kinds["fit"][1] == ["y_pred_proba", "y_pred", "y_true"] and kinds["fit"][2] == 80
    assert kinds["estimate"][1] == ["y_pred_proba", "y_pred"] and kinds["estimate"][2] == 40
    assert ("filter", "analysis") in calls


def test_nannyml_dle_uses_recorded_numeric_features_for_regression(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "nannyml", _fake_nannyml(0.5, calls))
    feats = [{"a": float(i), "b": 1.0, "tag": "x", "flag": True} for i in range(80)]
    _seed("REG", [float(i) * 2 for i in range(80)], [float(i) * 2 + 1 for i in range(80)], feats)
    res = da.estimate_performance("REG", window=40, estimator="nannyml")
    assert res["method"] == "nannyml-dle"
    assert res["estimated"] == pytest.approx(1 / 1.5)  # 1 / (1 + MAE)
    dle = next(c for c in calls if c[0] == "DLE")
    assert dle[1]["feature_column_names"] == ["a", "b"]  # strings and bools are not features


def test_nannyml_without_enough_reference_degrades_to_builtin_and_says_why(monkeypatch):
    monkeypatch.setitem(sys.modules, "nannyml", _fake_nannyml(0.6, []))
    _seed("CLF", [0.52] * 20)
    res = da.estimate_performance("CLF", baseline=0.95, estimator="nannyml")
    assert res["estimator"] == "builtin" and res["method"] == "cbpe-like"
    assert "labelled reference rows" in res["estimator_fallback"]


def test_a_missing_nannyml_degrades_to_builtin(monkeypatch):
    monkeypatch.setitem(sys.modules, "nannyml", None)
    _labelled_classifier("CLF")
    res = da.estimate_performance("CLF", estimator="nannyml")
    assert res["estimator"] == "builtin" and "not installed" in res["estimator_fallback"]


def test_the_estimator_is_selectable_from_the_environment(monkeypatch):
    monkeypatch.setenv(da.PERF_ESTIMATOR_ENV, "nannyml")
    assert da.resolve_perf_estimator()[0] == "nannyml"
    name, _fn, reason = da.resolve_perf_estimator("nope")
    assert name == "builtin" and "unknown estimator" in reason


def test_real_nannyml_cbpe_estimates_lower_accuracy_for_uncertain_predictions():
    pytest.importorskip("nannyml")
    rng = random.Random(0)
    ref_p = [rng.random() for _ in range(400)]
    reference = [{"prediction": p, "label": float(rng.random() < p)} for p in ref_p]
    sure = [rng.choice([rng.uniform(0, 0.1), rng.uniform(0.9, 1)]) for _ in range(200)]
    unsure = [rng.uniform(0.4, 0.6) for _ in range(200)]
    hi, method, _ = adapters.nannyml_estimate(reference, sure, [None] * 200, probabilistic=True)
    lo, _, _ = adapters.nannyml_estimate(reference, unsure, [None] * 200, probabilistic=True)
    assert method == "nannyml-cbpe" and hi > 0.85 and lo < 0.6


# -- whylogs profiler ----------------------------------------------------------------------------


def _fake_whylogs(seen):
    """whylogs stand-in: ``why.log(pandas=df).view().to_pandas()`` — one row per column."""
    import pandas as pd

    why = types.ModuleType("whylogs")

    def log(pandas):
        seen["frame"] = pandas
        rows = {}
        for col in pandas.columns:
            s = pandas[col]
            numeric = pd.api.types.is_numeric_dtype(s)
            rows[col] = {
                "counts/n": len(s),
                "counts/null": int(s.isna().sum()),
                "distribution/min": float(s.min()) if numeric else float("nan"),
                "distribution/max": float(s.max()) if numeric else float("nan"),
                "cardinality/est": float(s.nunique()),
            }
        frame = pd.DataFrame.from_dict(rows, orient="index")
        return types.SimpleNamespace(view=lambda: types.SimpleNamespace(to_pandas=lambda: frame))

    why.log = log
    return why


def test_whylogs_profile_maps_onto_the_builtin_summary(monkeypatch):
    seen: dict = {}
    monkeypatch.setitem(sys.modules, "whylogs", _fake_whylogs(seen))
    batch = [{"a": 1.0, "b": "x"}, {"a": None, "b": "y"}, {"a": 5.0, "b": "x"}]
    prof = da.profile_inference("Q", batch, profiler="whylogs")
    assert prof.profiler == "whylogs" and prof.profiler_fallback is None
    assert prof.fields["a"] == {
        "nulls": 1,
        "null_fraction": pytest.approx(1 / 3),
        "min": 1.0,
        "max": 5.0,
        "cardinality": 2,
    }
    assert prof.fields["b"]["min"] is None and prof.fields["b"]["cardinality"] == 2
    assert prof.null_fraction == pytest.approx(1 / 6)
    assert len(seen["frame"]) == 3


def test_a_whylogs_that_cannot_import_here_degrades(monkeypatch, tmp_path):
    """whylogs 1.6.4 raises AttributeError (``np.unicode_``) on NumPy 2 — that is unavailable."""
    pkg = tmp_path / "whylogs"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("raise AttributeError('`np.unicode_` was removed')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "whylogs", raising=False)
    prof = da.profile_inference("Q", [{"a": None}, {"a": 1.0}], profiler="whylogs")
    assert prof.profiler == "builtin"
    assert "cannot be imported here" in prof.profiler_fallback
    assert prof.fields["a"]["nulls"] == 1  # the builtin profile still ran


def test_the_profiler_is_selectable_from_the_environment(monkeypatch):
    monkeypatch.setenv(da.QUALITY_PROFILER_ENV, "whylogs")
    assert da.resolve_quality_profiler()[0] == "whylogs"
    assert da.resolve_quality_profiler("nope")[0] == "builtin"


def test_the_profiler_is_recorded_on_the_event(monkeypatch):
    monkeypatch.setitem(sys.modules, "whylogs", None)
    da.profile_inference("Q", [{"a": 1.0}], profiler="whylogs")
    from examlops import platform_db

    ev = platform_db.list_drift_events(model="Q", drift_kind="data_quality")[0]
    assert ev["detail"]["profiler"] == "builtin"
    assert "whylogs unavailable" in ev["detail"]["profiler_fallback"]


def test_real_whylogs_profiles_nulls_and_ranges():
    try:
        import whylogs  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - NumPy 2 makes whylogs 1.6.4 raise AttributeError
        pytest.skip(f"whylogs not importable here: {exc}")
    summary, total, nulls = adapters.whylogs_profile([{"a": 1.0}, {"a": None}, {"a": 3.0}])
    assert total == 3 and nulls == 1 and summary["a"]["max"] == 3.0

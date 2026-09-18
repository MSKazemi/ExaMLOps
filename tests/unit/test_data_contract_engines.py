# tests/unit/test_data_contract_engines.py
"""ADR 0005 clause 1 — Pandera is the contract engine, and it agrees with the pandas-only one.

The recorded finding: "clause 1's named engine, Pandera, was not adopted — validation is
pure-python". A contract now compiles to one Pandera ``DataFrameSchema`` when Pandera is installed
and a row-level failure names its rows; without Pandera the same predicates run on pandas. The two
must reach the same verdict on every check, or installing a library would change which data trains
a model. These hold them to it — on hand-picked edge cases and on seeded random frames.

Also held here: the freshness check the ADR lists and nothing implemented, and embedding width
judged on every row (it read the first row only).
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipelines import contracts as c  # noqa: E402
from pipelines.contracts.fdata import CONTRACT as FDATA  # noqa: E402

#: CI installs Pandera (root `pyproject.toml`), so the parity half always runs there. Without it
#: the pandas-engine tests below still run; only the comparisons skip.
HAVE_PANDERA = importlib.util.find_spec("pandera") is not None
needs_pandera = pytest.mark.skipif(not HAVE_PANDERA, reason="the parity half needs Pandera")
ENGINES = ("python", "auto") if HAVE_PANDERA else ("python",)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

#: Every kind of check, each at both severities somewhere, plus one hand-written check.
EVERYTHING = c.DataContract(
    dataset="everything",
    version="1",
    checks=[
        c.column_present("kind", dtype="object"),
        c.categorical("kind", ["a", "b"]),
        c.column_present("x", dtype="float"),
        c.in_range("x", low=0.0, high=10.0),
        c.in_range("x"),  # open: only asks that the column exist
        c.in_range("y", high=5, severity=c.WARN),
        c.not_null("x"),
        c.not_null("y", max_null_rate=0.25, severity=c.WARN),
        c.embedding_dim("emb", 4),
        c.min_rows(3),
        c.fresh_within("ts", "1d", now=lambda: NOW),
        c.Check("custom:x-sum", c.WARN, lambda df: (float(df["x"].sum()) < 100, "sum")),
    ],
)


def _good(n=6):
    return pd.DataFrame(
        {
            "kind": ["a", "b"] * (n // 2),
            "x": [1.0, 2.5] * (n // 2),
            "y": [1, 2] * (n // 2),
            "emb": [np.arange(4, dtype=float) for _ in range(n)],
            "ts": [NOW - timedelta(hours=h) for h in range(n)],
        }
    )


def _verdicts(contract, df, engine, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", engine)
    return contract.validate(df.copy())


def _assert_parity(contract, df, monkeypatch):
    if not HAVE_PANDERA:
        pytest.skip("the parity half needs Pandera")
    py = _verdicts(contract, df, "python", monkeypatch)
    pa = _verdicts(contract, df, "auto", monkeypatch)
    assert (py.engine, pa.engine) == ("python", "pandera")
    per_check = [(r["name"], r["passed"]) for r in py.checks]
    assert [(r["name"], r["passed"]) for r in pa.checks] == per_check, df
    assert (pa.passed, pa.score) == (py.passed, py.score)
    return py, pa


# ── the edge cases, one mutation of a good frame each ────────────────────────


def _mutations():
    def drop(col):
        return lambda df: df.drop(columns=[col])

    def setat(col, row, value):
        def f(df):
            df[col] = df[col].astype(object)
            df.at[row, col] = value
            return df

        return f

    return {
        "good": lambda df: df,
        "empty": lambda df: df.iloc[0:0],
        "missing kind": drop("kind"),
        "missing x": drop("x"),
        "missing emb": drop("emb"),
        "missing ts": drop("ts"),
        "bad category": setat("kind", 2, "c"),
        "null category": setat("kind", 2, None),
        "x below": lambda df: df.assign(x=[1.0, -1.0, 2.0, -3.0, 1.0, 1.0]),
        "x above": lambda df: df.assign(x=[1.0, 11.0, 2.0, 1.0, 1.0, 1.0]),
        "x inf": lambda df: df.assign(x=[1.0, np.inf, 2.0, 1.0, 1.0, 1.0]),
        "x on both bounds": lambda df: df.assign(x=[0.0, 10.0, 2.0, 1.0, 1.0, 1.0]),
        "x null": lambda df: df.assign(x=[1.0, np.nan, 2.0, 1.0, 1.0, 1.0]),
        "x ints": lambda df: df.assign(x=[1, 2, 3, 4, 5, 6]),
        "x strings": lambda df: df.assign(x=["1", "2", "3", "4", "5", "6"]),
        "y nulls under": lambda df: df.assign(y=[1, None, 2, 1, 1, 1]),
        "y nulls over": lambda df: df.assign(y=[1, None, None, 1, None, 1]),
        "emb ragged": setat("emb", 3, np.arange(3, dtype=float)),
        "emb scalar": setat("emb", 1, 7.0),
        "emb string": setat("emb", 1, "abcd"),
        "emb all null": lambda df: df.assign(emb=[None] * 6),
        "two rows": lambda df: df.iloc[0:2],
        "total_rows attr": lambda df: _with_attrs(df.iloc[0:2], total_rows=500),
        "stale": lambda df: df.assign(ts=[NOW - timedelta(days=3)] * 6),
        "naive fresh": lambda df: df.assign(ts=[datetime(2026, 9, 11, 11, 0)] * 6),
        "iso strings": lambda df: df.assign(ts=["2026-09-11T10:00:00Z"] * 6),
        "not timestamps": lambda df: df.assign(ts=["yesterday"] * 6),
        "extra column": lambda df: df.assign(extra=1),
    }


def _with_attrs(df, **attrs):
    df = df.copy()
    df.attrs.update(attrs)
    return df


@pytest.mark.parametrize("case", list(_mutations()))
def test_both_engines_reach_the_same_verdict_on_every_check(case, monkeypatch):
    df = _mutations()[case](_good())
    _assert_parity(EVERYTHING, df, monkeypatch)


def test_a_range_is_inclusive_at_both_ends(monkeypatch):
    df = _good().assign(x=[0.0, 10.0, 2.0, 1.0, 1.0, 1.0])

    for engine in ENGINES:
        assert _passed(_verdicts(EVERYTHING, df, engine, monkeypatch), "range:x") is True, engine


@pytest.mark.parametrize("case", ["good", "missing kind", "bad category", "emb ragged", "empty"])
def test_the_fdata_contract_agrees_too(case, monkeypatch):
    df = pd.DataFrame(
        {
            "pclass": ["memory-bound", "compute-bound"] * 3,
            "mbwidth": [10.0, 20.0] * 3,
            "embedding": [[0.1] * 384 for _ in range(6)],
        }
    )
    mutate = {
        "good": lambda d: d,
        "missing kind": lambda d: d.drop(columns=["pclass"]),
        "bad category": lambda d: d.assign(pclass=["io-bound"] + ["memory-bound"] * 5),
        "emb ragged": lambda d: d.assign(embedding=[[0.1] * 384] * 5 + [[0.1] * 383]),
        "empty": lambda d: d.iloc[0:0],
    }[case]
    _assert_parity(FDATA, mutate(df), monkeypatch)


def test_seeded_random_frames_agree(monkeypatch):
    """300 frames mixing every fault at random — nulls, out-of-range, foreign categories, ragged
    embeddings, stale and garbage timestamps, missing columns."""
    rng = np.random.default_rng(20260911)
    for _ in range(300):
        n = int(rng.integers(0, 9))
        df = pd.DataFrame(
            {
                "kind": rng.choice(["a", "b", "c", None], size=n, p=[0.45, 0.45, 0.05, 0.05]),
                "x": np.where(rng.random(n) < 0.1, np.nan, rng.normal(5, 4, n)),
                "y": np.where(rng.random(n) < 0.2, np.nan, rng.integers(0, 7, n)),
                "emb": [
                    None if rng.random() < 0.05 else np.zeros(int(rng.choice([4, 4, 4, 3])))
                    for _ in range(n)
                ],
                "ts": [NOW - timedelta(hours=float(rng.integers(0, 40))) for _ in range(n)],
            }
        )
        if n and rng.random() < 0.05:
            df["ts"] = df["ts"].astype(object)
            df.at[0, "ts"] = "garbage"
        for col in df.columns:
            if rng.random() < 0.05:
                df = df.drop(columns=[col])
        _assert_parity(EVERYTHING, df, monkeypatch)


# ── what Pandera adds: the failing rows ──────────────────────────────────────


def test_a_range_failure_names_its_rows(monkeypatch):
    df = _good().assign(x=[1.0, -1.0, 2.0, -3.0, 1.0, 1.0])

    _, pa = _assert_parity(EVERYTHING, df, monkeypatch)

    (row,) = [r for r in pa.checks if r["name"] == "range:x" and not r["passed"]]
    assert row["observed"] == "2 row(s) outside [0.0, 10.0] — row 1: -1.0, row 3: -3.0"


def test_a_ragged_embedding_names_the_row_and_its_width(monkeypatch):
    df = _good()
    df.at[4, "emb"] = np.zeros(2)

    py, pa = _assert_parity(EVERYTHING, df, monkeypatch)

    assert _observed(pa, "embedding_dim:emb") == "1 row(s) not 4-dim — row 4: dim 2"
    assert _observed(py, "embedding_dim:emb") == "1 of 6 embeddings not 4-dim (row 4: 2)"


def test_nulls_are_named_by_row_and_the_count_is_exact_past_the_examples(monkeypatch):
    df = _good(8).assign(x=[np.nan, 1.0, np.nan, np.nan, 1.0, np.nan, 1.0, 1.0])

    _, pa = _assert_parity(EVERYTHING, df, monkeypatch)

    assert _observed(pa, "not_null:x") == "4 row(s) null — rows 0, 2, 3, …"


def test_a_table_level_failure_reads_as_the_predicate_says_it(monkeypatch):
    df = _good().assign(ts=[NOW - timedelta(days=3)] * 6)

    py, pa = _assert_parity(EVERYTHING, df, monkeypatch)

    assert _observed(pa, "freshness:ts") == _observed(py, "freshness:ts")
    assert _observed(pa, "freshness:ts").startswith("newest=2026-09-08T12:00:00+00:00")


def _observed(result, name):
    return next(r["observed"] for r in result.checks if r["name"] == name)


# ── the two bugs the parity work found ───────────────────────────────────────


def test_embedding_width_is_judged_on_every_row_not_the_first(monkeypatch):
    """The first row had the right width, so the old check passed a ragged column."""
    df = _good()
    df.at[5, "emb"] = np.zeros(3)

    for engine in ENGINES:
        result = _verdicts(EVERYTHING, df, engine, monkeypatch)
        assert _passed(result, "embedding_dim:emb") is False, engine


def test_an_empty_column_has_no_nulls(monkeypatch):
    """Its null rate is NaN, and NaN <= 0 is False, so an empty table failed not_null with
    "null_rate=nan". Emptiness is min_rows's to judge, and it does."""
    df = _good().iloc[0:0]

    result = _verdicts(EVERYTHING, df, "python", monkeypatch)

    assert _passed(result, "not_null:x") is True
    assert _passed(result, "min_rows:3") is False


def _passed(result, name):
    return next(r["passed"] for r in result.checks if r["name"] == name)


# ── freshness ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stamps,passed",
    [
        ([NOW - timedelta(hours=2), NOW - timedelta(days=9)], True),  # the newest decides
        ([NOW - timedelta(hours=25)], False),
        ([NOW - timedelta(days=1)], True),  # the window is inclusive
        ([datetime(2026, 9, 11, 11, 0)], True),  # naive is UTC
        (["2026-09-10T11:59:00+00:00"], False),
        ([None, None], False),  # no timestamps at all
        (["not a date", NOW], False),  # unparseable is a failure, never dropped
    ],
)
def test_freshness(stamps, passed, monkeypatch):
    contract = c.DataContract("t", "1", [c.fresh_within("ts", timedelta(days=1), now=lambda: NOW)])

    for engine in ENGINES:
        assert _verdicts(contract, pd.DataFrame({"ts": stamps}), engine, monkeypatch).passed is (
            passed
        ), engine


@pytest.mark.parametrize("bad", ["0s", "-1h", timedelta(0)])
def test_a_non_positive_freshness_window_is_refused_at_definition(bad):
    with pytest.raises(ValueError, match="must be positive"):
        c.fresh_within("ts", bad)


# ── engine selection ─────────────────────────────────────────────────────────


@needs_pandera
def test_auto_uses_pandera_and_python_forces_the_pandas_engine(monkeypatch):
    assert _verdicts(EVERYTHING, _good(), "auto", monkeypatch).engine == "pandera"
    assert _verdicts(EVERYTHING, _good(), "python", monkeypatch).engine == "python"


@needs_pandera
def test_an_unknown_engine_value_warns_and_means_auto(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", "great-expectations")

    with pytest.warns(UserWarning, match="not auto|python"):
        assert EVERYTHING.validate(_good()).engine == "pandera"


def test_without_pandera_the_pandas_engine_judges(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", "auto")
    monkeypatch.setitem(sys.modules, "pandera.pandas", None)  # import raises ImportError

    assert EVERYTHING.validate(_good()).engine == "python"


def test_an_engine_fault_falls_back_rather_than_skipping_the_gate(monkeypatch):
    """A check that fails is a failure case, never an exception; an exception out of the engine
    is the engine breaking — and the reference predicates still owe the gate a verdict."""
    from pipelines.contracts import _pandera

    def broken(*_a, **_k):
        raise RuntimeError("engine bug")

    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", "auto")
    monkeypatch.setattr(_pandera, "run", broken)
    df = _good().assign(x=[1.0, -1.0, 2.0, 1.0, 1.0, 1.0])

    with pytest.warns(UserWarning, match="engine bug"):
        result = EVERYTHING.validate(df)

    assert result.engine == "python" and result.passed is False


@needs_pandera
def test_a_hand_written_check_runs_its_own_predicate_under_pandera(monkeypatch):
    df = _good().assign(x=[9.0] * 6, y=[0] * 6)  # sum 54 < 100 → passes; 9 in range
    contract = c.DataContract("t", "1", [c.Check("custom", c.ERROR, lambda d: (False, "nope"))])

    result = _verdicts(contract, df, "auto", monkeypatch)

    assert result.engine == "pandera"
    assert result.checks == [
        {"name": "custom", "severity": "error", "passed": False, "observed": "nope"}
    ]


@needs_pandera
def test_validation_emits_no_warnings(monkeypatch):
    """A Pandera deprecation warning per validated table would bury the gate's own output."""
    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", "auto")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert EVERYTHING.validate(_good()).engine == "pandera"


# ── the verdict records its engine ───────────────────────────────────────────


@needs_pandera
def test_the_recorded_check_says_which_engine_judged(monkeypatch):
    from examlops.platform_db import get_data_quality_checks, init_db, record_data_quality_check

    init_db()
    for engine in ("auto", "python"):
        record_data_quality_check("everything", _verdicts(EVERYTHING, _good(), engine, monkeypatch))

    rows = get_data_quality_checks("everything")
    assert [r["engine"] for r in rows] == ["python", "pandera"]


def test_a_datastore_from_before_gains_the_engine_column(tmp_path, monkeypatch):
    from examlops.platform_db import get_data_quality_checks, get_db, init_db
    from examlops.storage.testing import datastore_before_a_migration

    datastore_before_a_migration(tmp_path, monkeypatch, "data_quality_checks")
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE data_quality_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, model TEXT NOT NULL, "
            "dataset TEXT NOT NULL, status TEXT NOT NULL, passed INTEGER NOT NULL DEFAULT 0, "
            "failed INTEGER NOT NULL DEFAULT 0, details_json TEXT, actor TEXT)"
        )
        conn.execute(
            "INSERT INTO data_quality_checks (model, dataset, status) VALUES ('-', 'old', 'PASS')"
        )
    init_db(force=True)

    (row,) = get_data_quality_checks("old")
    assert row["engine"] is None, "an old verdict does not claim an engine it never recorded"

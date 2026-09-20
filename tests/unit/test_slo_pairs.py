"""Paired (TTFT, TPOT) SLOs — ADR 0117 decision 2."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops.slo.pairs import (
    PairSLO,
    PairSLOError,
    check_pair,
    evaluate,
    load_pair,
    percentile,
    set_pair,
)

SLO = PairSLO(ttft_ms=200, tpot_ms=50, percentile=90)


def _s(n, ttft=100.0, tpot=20.0):
    return [(ttft, tpot)] * n


def test_percentile_nearest_rank():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9
    assert percentile([5], 99) == 5
    with pytest.raises(ValueError):
        percentile([], 50)


def test_met_and_exactly_at_threshold_passes():
    v = evaluate(SLO, _s(30, 200.0, 50.0), min_samples=5)
    assert v.verdict == "met" and v.passed and v.attainment == 1.0 and v.burn_rate == 0.0


def test_just_over_threshold_violates():
    v = evaluate(SLO, _s(30, 200.01, 50.0), min_samples=5)
    assert v.verdict == "violated" and v.ttft_ok is False and v.tpot_ok is True
    assert v.attainment == 0.0 and not v.passed


def test_one_dimension_failing_is_enough():
    v = evaluate(SLO, _s(30, 10.0, 51.0), min_samples=5)
    assert v.verdict == "violated" and v.ttft_ok is True and v.tpot_ok is False
    assert "TPOT" in v.reasons[0]


def test_percentile_tolerates_tail_within_budget():
    # 10% tail allowed at p90: 3 of 30 slow requests still meets, attainment 90%, burn 1.0
    samples = _s(27) + _s(3, 999.0, 20.0)
    v = evaluate(SLO, samples, min_samples=5)
    assert v.verdict == "met" and v.attainment == pytest.approx(0.9)
    assert v.burn_rate == pytest.approx(1.0)
    # one more slow one tips p90 over
    assert evaluate(SLO, _s(26) + _s(4, 999.0, 20.0), min_samples=5).verdict == "violated"


def test_empty_and_too_few_is_no_verdict_not_pass():
    for samples in ([], _s(3)):
        v = evaluate(SLO, samples, min_samples=5)
        assert v.verdict == "no_verdict" and not v.passed and v.attainment is None


def test_malformed_samples_rejected_not_good(monkeypatch):
    junk = [None, (1,), ("a", 2), (float("nan"), 1), (-1, 5), {"ttft_ms": 1}, True]
    v = evaluate(SLO, _s(6) + junk, min_samples=6)
    assert v.n == 6 and v.rejected == len(junk) and v.verdict == "met"
    assert evaluate(SLO, junk, min_samples=1).verdict == "no_verdict"


def test_dict_samples_and_env_min_samples(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SLO_PAIR_MIN_SAMPLES", "3")
    v = evaluate(SLO, [{"ttft_ms": 1, "tpot_ms": 1}] * 3)
    assert v.verdict == "met"
    monkeypatch.setenv("EXAMLOPS_SLO_PAIR_MIN_SAMPLES", "garbage")
    assert evaluate(SLO, _s(19)).verdict == "no_verdict"


@pytest.mark.parametrize(
    "kw",
    [
        {"ttft_ms": 0, "tpot_ms": 1},
        {"ttft_ms": 1, "tpot_ms": -1},
        {"ttft_ms": float("inf"), "tpot_ms": 1},
        {"ttft_ms": 1, "tpot_ms": 1, "percentile": 100},
        {"ttft_ms": 1, "tpot_ms": 1, "percentile": 0},
        {"ttft_ms": 1, "tpot_ms": 1, "tight": "both"},
        {"ttft_ms": 1, "tpot_ms": 1, "slo_class": "vip"},
    ],
)
def test_validation(kw):
    with pytest.raises(PairSLOError):
        PairSLO(**kw)


def test_store_roundtrip_and_check():
    assert set_pair("m", "chat", ttft_ms=200, tpot_ms=50, tight="tpot") == "created"
    assert set_pair("m", "chat", ttft_ms=300, tpot_ms=50) == "updated"
    slo = load_pair("m", "chat")
    assert slo and slo.ttft_ms == 300 and slo.tight == "ttft"
    assert load_pair("m", "chat", "other") is None
    assert check_pair("m", "chat", _s(25)).verdict == "met"
    undeclared = check_pair("m", "nope", _s(25))
    assert undeclared.verdict == "no_verdict" and "no paired SLO" in undeclared.reasons[0]
    with pytest.raises(PairSLOError):
        set_pair("m", "bad", ttft_ms=-1, tpot_ms=1)
    assert load_pair("m", "bad") is None


def test_cli_flow(tmp_path):
    from examlops.cli.main import app

    r = CliRunner()
    res = r.invoke(
        app,
        ["slo", "pair-set", "m", "chat", "--ttft-ms", "200", "--tpot-ms", "50", "--tight", "tpot"],
    )
    assert res.exit_code == 0, res.output
    bad = r.invoke(app, ["slo", "pair-set", "m", "x", "--ttft-ms", "0", "--tpot-ms", "5"])
    assert bad.exit_code == 1
    assert "chat" in r.invoke(app, ["slo", "pair-list"]).output
    good = tmp_path / "g.json"
    good.write_text(json.dumps([[100, 20]] * 25))
    slow = tmp_path / "s.json"
    slow.write_text(json.dumps([{"ttft_ms": 100, "tpot_ms": 99}] * 25))
    few = tmp_path / "f.json"
    few.write_text("[]")
    assert r.invoke(app, ["slo", "pair-check", "m", "chat", "--samples", str(good)]).exit_code == 0
    assert r.invoke(app, ["slo", "pair-check", "m", "chat", "--samples", str(slow)]).exit_code == 1
    assert r.invoke(app, ["slo", "pair-check", "m", "chat", "--samples", str(few)]).exit_code == 1
    assert r.invoke(app, ["slo", "pair-check", "m", "chat", "--samples", "/nope"]).exit_code == 1
    js = r.invoke(app, ["--json", "slo", "pair-check", "m", "chat", "--samples", str(slow)])
    assert json.loads(js.output)["verdict"] == "violated"

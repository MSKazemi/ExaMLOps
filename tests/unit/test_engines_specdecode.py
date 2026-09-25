"""ADR 0016 decision 4 — speculative-decoding acceptance + speedup surfaced in FinOps.

Covers the speedup model, the bounded in-memory accumulator, its flush to
``specdecode_windows``, the wiring from the one place every engine is wrapped
(``build_engine`` → ``InstrumentedEngine``), server-counter parsing, and
``exa finops specdecode``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import engines  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data.specdecode import (  # noqa: E402
    record_specdecode_window,
    specdecode_summary,
)
from examlops.engines import specdecode  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.delenv("EXAMLOPS_SPECDECODE_FLUSH_CALLS", raising=False)
    monkeypatch.delenv("EXAMLOPS_SPECDECODE_FLUSH_SECONDS", raising=False)
    specdecode._WINDOWS.clear()
    init_db()
    yield
    specdecode._WINDOWS.clear()


def _rows() -> list[dict]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM specdecode_windows ORDER BY id")]


# ── speedup model (Leviathan et al. 2023, eq. 1) ──────────────────────────────


@pytest.mark.parametrize(
    "alpha,gamma,expected",
    [
        (0.5, 1, 1.5),  # γ=1 reduces to 1 + α — the figure the platform reported before
        (0.0, 4, 1.0),  # nothing accepted: one token per target pass
        (0.5, 3, 1.875),  # (1 - 0.5^4) / 0.5
        (1.0, 3, 4.0),  # the limit γ + 1, not a division by zero
        (1.7, 2, 3.0),  # α clamped to 1
        (-0.2, 2, 1.0),  # α clamped to 0
    ],
)
def test_estimated_speedup(alpha, gamma, expected):
    assert specdecode.estimated_speedup(alpha, gamma) == pytest.approx(expected)


def test_lookahead_below_one_is_clamped():
    assert specdecode.estimated_speedup(0.5, 0) == pytest.approx(1.5)


# ── accumulator ───────────────────────────────────────────────────────────────


def test_a_call_that_drafted_nothing_is_not_counted():
    assert specdecode.observe("m", proposed_tokens=0, accepted_tokens=0, engine="e") is False
    assert specdecode.pending() == {}


def test_accepted_is_clamped_to_proposed():
    specdecode.observe("m", proposed_tokens=4, accepted_tokens=9, engine="e")
    assert specdecode.pending()[("m", "default", "e", 1)]["accepted"] == 4


def test_windows_fold_in_memory_then_flush_as_one_row():
    for _ in range(3):
        specdecode.observe("m", proposed_tokens=10, accepted_tokens=7, engine="e", tenant="t1")
    assert _rows() == []  # nothing written per request
    assert specdecode.flush() == 1
    (row,) = _rows()
    assert (row["model"], row["tenant"], row["engine"]) == ("m", "t1", "e")
    assert (row["calls"], row["proposed_tokens"], row["accepted_tokens"]) == (3, 30, 21)
    assert specdecode.pending() == {}


def test_flush_on_call_count(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SPECDECODE_FLUSH_CALLS", "2")
    specdecode.observe("m", proposed_tokens=2, accepted_tokens=1, engine="e")
    assert _rows() == []
    specdecode.observe("m", proposed_tokens=2, accepted_tokens=1, engine="e")
    assert len(_rows()) == 1 and specdecode.pending() == {}


def test_flush_on_window_age(monkeypatch):
    specdecode.observe("m", proposed_tokens=2, accepted_tokens=1, engine="e")
    specdecode._WINDOWS[("m", "default", "e", 1)].started_mono -= 3600
    specdecode.observe("m", proposed_tokens=2, accepted_tokens=2, engine="e")
    (row,) = _rows()
    assert row["calls"] == 2 and row["accepted_tokens"] == 3


def test_a_quiet_windows_age_is_enforced_by_any_later_call():
    """A key that saw one call and went quiet must still flush on age, via another key's call."""
    specdecode.observe("quiet", proposed_tokens=4, accepted_tokens=3, engine="e")
    specdecode._WINDOWS[("quiet", "default", "e", 1)].started_mono -= 3600
    specdecode.observe("busy", proposed_tokens=2, accepted_tokens=1, engine="e")
    rows = _rows()
    assert [r["model"] for r in rows] == ["quiet"] and rows[0]["accepted_tokens"] == 3
    assert list(specdecode.pending()) == [("busy", "default", "e", 1)]


def test_key_cardinality_is_bounded(monkeypatch):
    monkeypatch.setattr(specdecode, "_MAX_KEYS", 3)
    for i in range(3):
        specdecode.observe(f"m{i}", proposed_tokens=1, accepted_tokens=1, engine="e")
    specdecode.observe("m-new", proposed_tokens=1, accepted_tokens=1, engine="e")
    assert len(_rows()) == 3  # the full map was flushed before admitting a new key
    assert list(specdecode.pending()) == [("m-new", "default", "e", 1)]


def test_a_failed_write_is_counted_never_raised(monkeypatch):
    import examlops.data.specdecode as data_mod

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(data_mod, "record_specdecode_window", boom)
    before = specdecode.dropped_windows()
    specdecode.observe("m", proposed_tokens=1, accepted_tokens=1, engine="e")
    assert specdecode.flush() == 0
    assert specdecode.dropped_windows() == before + 1


# ── wiring through the engine wrapper + the public telemetry helper ───────────


def test_every_built_engine_feeds_finops():
    cfg = engines.EngineConfig(
        engine="echo",
        speculative_decoding={"enabled": True, "draft_model": "tiny", "num_speculative_tokens": 3},
    )
    eng = engines.build_engine(cfg, model_path="qwen")
    eng.generate("a b c d", max_tokens=4, tenant="team-a")
    assert specdecode.pending() == {
        ("qwen", "team-a", "echo", 3): {"calls": 1, "proposed": 4, "accepted": 4}
    }


def test_an_engine_without_spec_decode_records_nothing():
    eng = engines.build_engine(engines.EngineConfig(engine="echo"), model_path="qwen")
    eng.generate("a b c", max_tokens=3)
    assert specdecode.pending() == {}


def test_record_spec_decode_telemetry_persists_and_keeps_its_contract():
    comp = engines.Completion(text="x", proposed_tokens=8, accepted_tokens=6)
    stats = engines.record_spec_decode_telemetry("JPCP", comp, engine="vllm-inproc")
    assert stats == {"acceptance_rate": 0.75, "speedup": 1.75}
    specdecode.flush()
    (row,) = _rows()
    assert row["engine"] == "vllm-inproc" and row["proposed_tokens"] == 8


# ── server counters ───────────────────────────────────────────────────────────


def test_spec_decode_from_vllm_counters():
    text = (
        'vllm:spec_decode_num_draft_tokens_total{model_name="m"} 400.0\n'
        'vllm:spec_decode_num_accepted_tokens_total{model_name="m"} 300.0\n'
    )
    out = specdecode.spec_decode_from_metrics(engines.parse_prometheus_text(text), lookahead=1)
    assert out == {
        "proposed_tokens": 400,
        "accepted_tokens": 300,
        "acceptance_rate": 0.75,
        "estimated_speedup": 1.75,
    }


def test_llm_status_bound_uses_the_endpoints_own_lookahead():
    """A γ=4 server's "speedup <=" must be the γ=4 bound; the γ=1 figure is below it (false)."""
    from examlops.cli.commands.llm_serve_cmd import _spec_decode_status

    metrics = {
        "vllm:spec_decode_num_draft_tokens_total": 400.0,
        "vllm:spec_decode_num_accepted_tokens_total": 300.0,
    }
    spec = {"enabled": True, "num_speculative_tokens": 4}
    out = _spec_decode_status({"engine_config": {"speculative_decoding": spec}}, metrics)
    assert out is not None and out["lookahead"] == 4
    assert out["estimated_speedup"] == pytest.approx(specdecode.estimated_speedup(0.75, 4))
    assert out["estimated_speedup"] > 1.75  # the γ=1 figure would understate the bound
    unset = _spec_decode_status({}, metrics)
    assert unset is not None and unset["estimated_speedup"] == pytest.approx(1.75)


def test_no_counters_is_none_not_zero():
    assert specdecode.spec_decode_from_metrics({"vllm:num_requests_running": 1.0}) is None
    assert (
        specdecode.spec_decode_from_metrics(
            {
                "vllm:spec_decode_num_draft_tokens_total": 0.0,
                "vllm:spec_decode_num_accepted_tokens_total": 0.0,
            }
        )
        is None
    )


# ── data layer + CLI ──────────────────────────────────────────────────────────


def test_summary_sums_counts_rather_than_averaging_ratios():
    record_specdecode_window("m", engine="e", calls=1, proposed_tokens=2, accepted_tokens=2)
    record_specdecode_window("m", engine="e", calls=1, proposed_tokens=98, accepted_tokens=0)
    (row,) = specdecode_summary(model="m")
    assert row["proposed_tokens"] == 100 and row["accepted_tokens"] == 2  # 2%, not 50%


def test_tenant_filter_is_applied_before_the_limit():
    record_specdecode_window(
        "mine", engine="e", tenant="a", calls=1, proposed_tokens=1, accepted_tokens=1
    )
    for i in range(20):
        record_specdecode_window(
            f"other{i}", engine="e", tenant="b", calls=1, proposed_tokens=1, accepted_tokens=1
        )
    rows = specdecode_summary(tenant="a", limit=1)
    assert [r["model"] for r in rows] == ["mine"]


def test_summary_rejects_a_non_positive_window():
    with pytest.raises(ValueError):
        specdecode_summary(days=0)


def test_exa_finops_specdecode_json():
    record_specdecode_window(
        "qwen",
        engine="vllm-inproc",
        calls=5,
        proposed_tokens=400,
        accepted_tokens=200,
        lookahead=3,
    )
    result = runner.invoke(app, ["--json", "finops", "specdecode", "--model", "qwen"])
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.output)
    assert row["acceptance_rate"] == 0.5
    assert row["estimated_speedup"] == pytest.approx(1.875)
    assert row["calls"] == 5 and row["lookahead"] == 3


def test_exa_finops_specdecode_empty_and_bad_window():
    ok = runner.invoke(app, ["finops", "specdecode"])
    assert ok.exit_code == 0 and "No speculative-decoding activity" in ok.output
    bad = runner.invoke(app, ["finops", "specdecode", "--days", "0"])
    assert bad.exit_code == 2

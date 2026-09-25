"""ADR 0143 decision 5 / Verification 2 — a benchmark without its conditions is not stored."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops import platform_db as pdb
from examlops.cli.main import app
from examlops.data import generative_benchmarks as store
from examlops.slo import benchmarks as bm
from examlops.slo.pairs import set_pair

runner = CliRunner()


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_SLO_PAIR_MIN_SAMPLES", raising=False)
    pdb.init_db()
    set_pair("chat-llm", "interactive", ttft_ms=300, tpot_ms=40)


def _conditions(**over):
    c = {
        "model": "qwen2.5-7b",
        "quantization": "none",
        "hardware": "1xH100-80GB",
        "engine_version": "vllm==0.29.0",
        "dataset": "sharegpt",
        "length_distribution": "in~512,out~256",
        "concurrency": 16,
        "slo": "interactive",
        "ttft_includes_queue_wait": True,
    }
    c.update(over)
    return c


def _samples(n=40, ttft=200.0, tpot=30.0):
    return [[ttft + i, tpot] for i in range(n)]


@pytest.mark.parametrize("missing", bm.REQUIRED_CONDITIONS)
def test_every_missing_condition_rejects_the_result(missing):
    c = _conditions()
    del c[missing]
    with pytest.raises(bm.BenchmarkRejected, match=f"missing condition: {missing}"):
        bm.record_benchmark("chat-llm", c, _samples())
    assert store.list_results("chat-llm") == []


def test_blank_or_invalid_conditions_are_rejected():
    with pytest.raises(bm.BenchmarkRejected, match="engine_version"):
        bm.record_benchmark("chat-llm", _conditions(engine_version="  "), _samples())
    with pytest.raises(bm.BenchmarkRejected, match="concurrency"):
        bm.record_benchmark("chat-llm", _conditions(concurrency=0), _samples())
    with pytest.raises(bm.BenchmarkRejected, match="true or false"):
        bm.record_benchmark("chat-llm", _conditions(ttft_includes_queue_wait="yes"), _samples())


def test_an_undeclared_slo_pair_is_rejected():
    with pytest.raises(bm.BenchmarkRejected, match="not a declared pair"):
        bm.record_benchmark("chat-llm", _conditions(slo="nope"), _samples())


def test_a_complete_result_is_stored_with_its_conditions_and_audited():
    res = bm.record_benchmark("chat-llm", _conditions(), _samples(), actor="ci")
    assert res["created"] is True
    got = store.get(res["id"])
    assert got["conditions"]["engine_version"] == "vllm==0.29.0"
    assert got["n"] == 40 and got["ttft_p99_ms"] == 239.0 and got["tpot_p99_ms"] == 30.0
    assert got["goodput"] == 1.0 and got["verdict"] == "met"
    assert _audit_actions() == ["slo_benchmark_recorded"]


def _audit_actions():
    with pdb.get_db() as conn:
        rows = conn.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'slo_benchmark%' ORDER BY id"
        ).fetchall()
    return [r[0] for r in rows]


def test_recording_the_same_run_twice_is_idempotent():
    a = bm.record_benchmark("chat-llm", _conditions(), _samples())
    b = bm.record_benchmark("chat-llm", _conditions(), _samples())
    assert a["id"] == b["id"] and b["created"] is False
    assert len(store.list_results("chat-llm")) == 1
    assert _audit_actions() == ["slo_benchmark_recorded"]  # one decision, one event


def test_queue_wait_is_added_when_ttft_excludes_it():
    samples = [{"ttft_ms": 100.0, "tpot_ms": 20.0, "queue_wait_ms": 500.0}] * 30
    res = bm.record_benchmark("chat-llm", _conditions(ttft_includes_queue_wait=False), samples)
    assert res["ttft_p99_ms"] == 600.0
    assert res["ttft_includes_queue"] is True
    assert res["verdict"] == "violated"  # 600 ms > 300 ms once the queue is counted


def test_a_run_mixing_queued_and_unqueued_samples_never_labels_them_all_as_including_it():
    # 30 samples carry their queue wait, 30 do not. Labelling the result "includes queue wait"
    # while half its TTFTs exclude it would understate the user-perceived TTFT under that label.
    queued = [{"ttft_ms": 100.0, "tpot_ms": 20.0, "queue_wait_ms": 500.0}] * 30
    bare = [{"ttft_ms": 100.0, "tpot_ms": 20.0}] * 30
    res = bm.record_benchmark(
        "chat-llm", _conditions(ttft_includes_queue_wait=False), queued + bare
    )
    assert res["ttft_includes_queue"] is True
    assert res["n"] == 30 and res["rejected"] == 30
    assert res["ttft_p50_ms"] == 600.0


def test_a_lost_benchmark_audit_is_counted_and_the_result_stands(monkeypatch):
    from examlops.data import audit

    audit.reset_dropped_audit_events()

    def boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "write_audit_event", boom)
    res = bm.record_benchmark("chat-llm", _conditions(), _samples())
    assert res["created"] is True and store.list_results("chat-llm")
    assert audit.dropped_audit_events() == {"slo_benchmark_recorded": 1}
    audit.reset_dropped_audit_events()


def test_malformed_samples_are_counted_as_rejected_and_none_valid_is_refused():
    res = bm.record_benchmark("chat-llm", _conditions(), [*_samples(25), [-1, 5], "x"])
    assert res["rejected"] == 2 and res["n"] == 25
    with pytest.raises(bm.BenchmarkRejected, match="no valid"):
        bm.record_benchmark("chat-llm", _conditions(), [["a", "b"]])


def test_few_samples_give_no_verdict_not_a_pass():
    res = bm.record_benchmark("chat-llm", _conditions(), _samples(3))
    assert res["verdict"] == "no_verdict"


def test_results_are_tenant_scoped_before_the_limit():
    set_pair("chat-llm", "interactive", ttft_ms=300, tpot_ms=40, tenant="t2")
    for i in range(3):
        bm.record_benchmark("chat-llm", _conditions(concurrency=i + 1), _samples(), tenant="t2")
    bm.record_benchmark("chat-llm", _conditions(), _samples())
    assert len(store.list_results("chat-llm", tenant="default", limit=1)) == 1
    assert store.list_results("chat-llm", tenant="default", limit=1)[0]["tenant"] == "default"
    assert len(store.list_results(tenant="t2")) == 3


def test_cli_record_rejects_then_accepts(tmp_path):
    bad = tmp_path / "bad.json"
    c = _conditions()
    del c["hardware"]
    bad.write_text(json.dumps({"conditions": c, "samples": _samples()}))
    r = runner.invoke(app, ["--json", "slo", "benchmark", "record", "chat-llm", "--file", str(bad)])
    assert r.exit_code == 1 and "missing condition: hardware" in r.output
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"conditions": _conditions(), "samples": _samples()}))
    r = runner.invoke(
        app, ["--json", "slo", "benchmark", "record", "chat-llm", "--file", str(good)]
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["verdict"] == "met"
    r = runner.invoke(app, ["--json", "slo", "benchmark", "results", "chat-llm"])
    rows = json.loads(r.output)
    assert len(rows) == 1 and rows[0]["conditions"]["hardware"] == "1xH100-80GB"

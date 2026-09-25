"""ADR 0007 decision 3 — the scheduled online-eval flow and its `exa eval online` surface.

Nothing used to schedule an eval suite: quality was measured when a person typed `exa eval run`.
The scheduler scores sampled live traffic per model, once per window, behind the house
kill-switch + lease, persists to the eval store the C3 gate reads, and audits every cycle.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.evaluation import online  # noqa: E402

NOW = 1_780_000_000.0  # fixed clock: one window, deterministic run ids


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for name in (online.ENABLED_ENV, online.TEXTFILE_ENV, "EXAMLOPS_EVAL_TEMPO_URL"):
        monkeypatch.delenv(name, raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _seed(model, preds, labels, *, alias="Production", at=None):
    from examlops import platform_db

    at = int(NOW) - 60 if at is None else int(at)

    with platform_db.get_db() as conn:
        for i, (p, y) in enumerate(zip(preds, labels, strict=True)):
            h = f"{model}-{i}"
            conn.execute(
                "INSERT INTO predictions (model, alias, request_hash, prediction, ts)"
                " VALUES (?,?,?,?,datetime(?, 'unixepoch'))",
                (model, alias, h, p, at),
            )
            conn.execute("INSERT INTO ground_truth (request_hash, label) VALUES (?,?)", (h, y))


def _configure(model="JPCP", **kw):
    from examlops.data.evaluation import set_online_eval

    args = {
        "suite": "live",
        "evaluators": ["abs_error", "numeric_match:0.5"],
        "window_s": 3600,
        "sample_size": 50,
    }
    args.update(kw)
    set_online_eval(model, **args)


def _results(model="JPCP", suite="live"):
    from examlops.data.evaluation import get_eval_results

    return {r["metric"]: r for r in get_eval_results(model, suite)}


def _audit_actions():
    from examlops import platform_db

    with platform_db.get_db() as conn:
        return [r[0] for r in conn.execute("SELECT action FROM audit_events ORDER BY id")]


def _sched(**kw):
    kw.setdefault("clock", lambda: NOW)
    return online.OnlineEvalScheduler(**kw)


def test_kill_switch_refuses_a_real_cycle_and_audits_it():
    _seed("JPCP", [1.0, 2.0], [1.0, 3.0])
    _configure()
    rep = _sched().run_cycle()
    assert rep.ran is False and "EXAMLOPS_EVAL_ONLINE_ENABLED" in rep.note
    assert _results() == {}
    assert "eval_online_skipped" in _audit_actions()


def test_a_real_cycle_scores_live_traffic_and_persists_mae_without_an_interval(monkeypatch):
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    _seed("JPCP", [1.0, 2.0, 4.0, 10.0], [1.0, 3.0, 4.0, 11.0])
    _configure()
    rep = _sched().run_cycle()
    run = rep.runs[0]
    assert run.outcome == online.RECORDED, run.note
    rows = _results()
    assert rows["mae"]["score"] == pytest.approx(0.5)  # |0|+|1|+|0|+|1| / 4
    assert rows["mae"]["score_lo"] is None  # MAE carries a unit: no Wilson interval
    assert rows["numeric_match"]["score"] == pytest.approx(0.5)
    assert rows["numeric_match"]["score_lo"] is not None  # a proportion gets one
    assert rows["mae"]["sample_size"] == 4
    assert rows["mae"]["alias"] == "Production"
    assert rows["mae"]["run_id"] == online.window_run_id("Production", 3600, NOW)
    assert "eval_online_cycle" in _audit_actions()
    from examlops.data.evaluation import get_online_eval

    cfg = get_online_eval("JPCP")
    assert cfg["last_status"] == online.RECORDED and cfg["last_run_id"] == run.run_id


def test_the_same_window_is_scored_once(monkeypatch):
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    _seed("JPCP", [1.0, 2.0], [1.0, 3.0])
    _configure()
    assert _sched().run_cycle().runs[0].outcome == online.RECORDED
    again = _sched().run_cycle().runs[0]
    assert again.outcome == online.DEDUPED
    from examlops import platform_db

    with platform_db.get_db() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM eval_suite_results").fetchone()
    assert n == 2  # two metrics, one window — not four


def test_the_next_window_is_a_new_point(monkeypatch):
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    _seed("JPCP", [1.0, 2.0], [1.0, 3.0])
    _configure()
    _sched().run_cycle()
    later = _sched(clock=lambda: NOW + 3600).run_cycle().runs[0]
    # the seeded traffic is now outside the window → nothing to score, and it says so
    assert later.outcome == online.SKIPPED and "no labelled predictions" in later.note


def test_dry_run_scores_but_writes_nothing_and_needs_no_switch():
    _seed("JPCP", [1.0, 2.0], [1.0, 3.0])
    _configure()
    rep = _sched(dry_run=True).run_cycle()
    assert rep.ran and rep.runs[0].outcome == online.PREVIEWED
    assert rep.runs[0].scores["mae"] == pytest.approx(0.5)
    assert _results() == {}
    from examlops.data.evaluation import get_online_eval

    assert get_online_eval("JPCP")["last_status"] is None


def test_lease_blocks_a_second_scheduler(monkeypatch):
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    _configure()
    from examlops.coordination import get_coordinator

    assert get_coordinator().try_lock(online.LEASE_KEY, "someone-else", 600)
    rep = _sched().run_cycle()
    assert rep.ran is False and "lease" in rep.note


def test_one_bad_model_never_stops_the_sweep(monkeypatch):
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    _seed("GOOD", [1.0], [1.0])
    _configure("GOOD")
    _configure("BAD", evaluators=["judge:relevancy"])  # no judge model → fails at build
    rep = _sched().run_cycle()
    by = {r.model: r for r in rep.runs}
    assert by["BAD"].outcome == online.FAILED and "judge model" in by["BAD"].note
    assert by["GOOD"].outcome == online.RECORDED


def test_disabled_schedules_are_not_run(monkeypatch):
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    _seed("JPCP", [1.0], [1.0])
    _configure()
    from examlops.data.evaluation import disable_online_eval

    assert disable_online_eval("JPCP") is True
    assert _sched().run_cycle().runs == []


def test_a_generative_model_is_judged_through_the_injected_source(monkeypatch):
    """Tempo-shaped items + a judge answered at temperature 0 by the text seam."""
    from examlops.evaluation import EvalItem
    from examlops.evaluation.traffic import TrafficPull

    monkeypatch.setenv(online.ENABLED_ENV, "1")
    calls = []

    def text_fn(prompt, *, temperature):
        calls.append(temperature)
        return "0.75"

    class FakeTempo:
        name = "tempo"

        def pull(self, model, *, since_s, limit, tenant, alias):
            items = [
                EvalItem(output=f"answer {i}", prompt=f"q{i}", recorded_hash=f"h{i}")
                for i in range(5)
            ]
            return TrafficPull("tempo", items=items, seen=5)

    _configure(
        "chat",
        source="tempo",
        evaluators=["judge:relevancy", "exact_match"],
        judge_model="llama3.1:8b",
        sample_size=3,
    )
    rep = _sched(sources={"tempo": FakeTempo()}, text_fn=text_fn).run_cycle()
    run = rep.runs[0]
    assert run.outcome == online.RECORDED, run.note
    assert run.scores == {"judge_relevancy": pytest.approx(0.75)}  # exact_match skipped: no ref
    assert calls == [0.0, 0.0, 0.0]
    row = _results("chat")["judge_relevancy"]
    assert row["judge_model"] == "llama3.1:8b" and row["judge_prompt_version"] == "relevancy-v1"
    assert row["sample_size"] == 3


def test_judge_errors_are_counted_not_fatal(monkeypatch):
    from examlops.evaluation import EvalItem
    from examlops.evaluation.traffic import TrafficPull

    monkeypatch.setenv(online.ENABLED_ENV, "1")
    answers = iter(["0.9", "no idea", "1.0"])

    class Src:
        def pull(self, model, **kw):
            return TrafficPull(
                "tempo", items=[EvalItem(output=str(i), recorded_hash=f"h{i}") for i in range(3)]
            )

    _configure("chat", source="tempo", evaluators=["judge:correctness"], judge_model="j")
    rep = _sched(
        sources={"tempo": Src()}, text_fn=lambda p, *, temperature: next(answers)
    ).run_cycle()
    run = rep.runs[0]
    assert run.outcome == online.RECORDED
    assert run.errors == {"judge_correctness": 1}
    assert run.counts == {"judge_correctness": 2}
    assert run.scores["judge_correctness"] == pytest.approx(0.95)


def test_textfile_is_rewritten_after_a_cycle(monkeypatch, tmp_path):
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    target = tmp_path / "eval.prom"
    monkeypatch.setenv(online.TEXTFILE_ENV, str(target))
    _seed("JPCP", [1.0, 2.0], [1.0, 3.0])
    _configure()
    rep = _sched().run_cycle()
    assert rep.textfile == str(target)
    text = target.read_text()
    assert (
        'examlops_eval_score{alias="Production",metric="mae",model="JPCP",suite="live",'
        'tenant="default"} 0.5' in text
    )
    assert not list(tmp_path.glob(".eval-*"))  # no temp file left behind


def test_window_run_id_is_stable_inside_a_window_and_floored():
    a = online.window_run_id("Production", 3600, NOW)
    b = online.window_run_id("Production", 3600, NOW + 1)
    assert a == b and a.startswith("online:Production:3600:")
    assert online.window_run_id("Production", 5, NOW).split(":")[2] == str(online.MIN_WINDOW_S)


# -- the CLI -------------------------------------------------------------------------------------


def _cli(args, *, json_mode=False):
    from typer.testing import CliRunner

    from examlops.cli import _output
    from examlops.cli.commands.eval_cmd import app

    _output.json_mode = json_mode
    try:
        return CliRunner().invoke(app, args)
    finally:
        _output.json_mode = False


def test_cli_enable_validates_every_spec_before_saving():
    bad = _cli(["online", "enable", "JPCP", "--suite", "s", "-e", "no_such_metric"])
    assert bad.exit_code == 2
    judge_without_model = _cli(
        ["online", "enable", "JPCP", "--suite", "s", "-e", "judge:relevancy"]
    )
    assert judge_without_model.exit_code == 2
    from examlops.data.evaluation import get_online_eval

    assert get_online_eval("JPCP") is None


def test_cli_enable_refuses_a_tenant_on_the_untenanted_source_and_bad_names():
    res = _cli(["online", "enable", "JPCP", "--suite", "s", "-e", "abs_error", "--tenant", "b"])
    assert res.exit_code == 2
    res = _cli(["online", "enable", 'x" || true', "--suite", "s", "-e", "abs_error"])
    assert res.exit_code == 2


def test_cli_enable_status_disable_round_trip_is_audited():
    ok = _cli(["online", "enable", "JPCP", "--suite", "live", "-e", "abs_error", "--sample", "10"])
    assert ok.exit_code == 0, ok.output
    status = _cli(["online", "status"], json_mode=True)
    doc = json.loads(status.output)
    assert doc["schedulerEnabled"] is False
    assert doc["schedules"][0]["evaluators"] == ["abs_error"]
    assert doc["schedules"][0]["sample_size"] == 10
    off = _cli(["online", "disable", "JPCP"])
    assert off.exit_code == 0
    assert _cli(["online", "disable", "NOPE"]).exit_code == 1
    actions = _audit_actions()
    assert "eval_online_enabled" in actions and "eval_online_disabled" in actions


def test_cli_run_refuses_without_switch_previews_with_dry_run_and_json_needs_once():
    import time

    _seed("JPCP", [1.0, 2.0], [1.0, 3.0], at=time.time() - 60)  # the CLI runs on the real clock
    _configure()
    assert _cli(["online", "run", "--once"]).exit_code == 1
    preview = _cli(["online", "run", "--once", "--dry-run"], json_mode=True)
    assert preview.exit_code == 0
    doc = json.loads(preview.output)
    assert doc["dry_run"] is True and doc["counts"]["previewed"] == 1
    assert _cli(["online", "run"], json_mode=True).exit_code == 2


def test_cli_evaluators_lists_engines():
    res = _cli(["evaluators"], json_mode=True)
    doc = json.loads(res.output)
    specs = {r["spec"] for r in doc["evaluators"]}
    assert {"abs_error", "string_similarity", "judge:relevancy", "deepeval:faithfulness"} <= specs
    assert doc["engines"]["examlops"] is True


def test_cli_run_accepts_evaluator_specs(tmp_path):
    items = tmp_path / "items.jsonl"
    items.write_text(
        "\n".join(
            json.dumps({"output": o, "reference": r}) for o, r in [("1.0", "1.0"), ("2", "4")]
        )
    )
    res = _cli(
        ["run", "s", "--model", "M", "--items", str(items), "-e", "abs_error"], json_mode=True
    )
    assert res.exit_code == 0, res.output
    first, _ = json.JSONDecoder().raw_decode(res.output)  # the table doc, then the ok() doc
    assert first["scores"] == {"mae": pytest.approx(1.0)}
    bad = _cli(["run", "s", "--model", "M", "--items", str(items), "-e", "deepeval:faithfulness"])
    assert bad.exit_code == 2  # needs a judge model — refused before anything runs


def test_two_tenants_scoring_the_same_model_in_one_window_are_both_recorded(monkeypatch):
    """Schedules are keyed (model, tenant); the window run id carries no tenant.

    Before eval results carried a tenant, tenant ``acme``'s recorded window made tenant
    ``globex``'s identical window read as already evaluated, so ``globex`` was never scored.
    """
    from examlops.evaluation import EvalItem
    from examlops.evaluation.traffic import TrafficPull

    monkeypatch.setenv(online.ENABLED_ENV, "1")

    class Src:
        def pull(self, model, *, since_s, limit, tenant, alias):
            ok = "1" if tenant == "acme" else "0"
            items = [
                EvalItem(output=ok, reference="1", recorded_hash=f"{tenant}-{i}") for i in range(4)
            ]
            return TrafficPull("tempo", items=items, seen=4)

    for tenant in ("acme", "globex"):
        _configure("chat", source="tempo", evaluators=["exact_match"], tenant=tenant)
    rep = _sched(sources={"tempo": Src()}).run_cycle()
    outcomes = {r.tenant: r.outcome for r in rep.runs}
    assert outcomes == {"acme": online.RECORDED, "globex": online.RECORDED}
    from examlops.data.evaluation import latest_eval_scores

    by_tenant = {r["tenant"]: r["score"] for r in latest_eval_scores(model="chat", tenant=None)}
    assert by_tenant == {"acme": 1.0, "globex": 0.0}
    # a second cycle in the same window dedupes each tenant on its own record
    again = {r.tenant: r.outcome for r in _sched(sources={"tempo": Src()}).run_cycle().runs}
    assert again == {"acme": online.DEDUPED, "globex": online.DEDUPED}


def test_run_forever_survives_a_cycle_that_raises():
    sched = _sched()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return online.CycleReport(enabled=True, dry_run=False)

    sched.run_cycle = flaky  # type: ignore[method-assign]
    seen = []
    slept = []
    n = sched.run_forever(interval=7, on_cycle=seen.append, sleep=slept.append, max_cycles=3)
    assert n == 3 and len(calls) == 3
    assert len(seen) == 2  # the failed cycle reported nothing, and the loop kept going
    assert slept == [7, 7]

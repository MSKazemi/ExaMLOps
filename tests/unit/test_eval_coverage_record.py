"""The whole-CLI number must survive the run that produced it.

Pass 196 measured 344/363 and the number existed only in a terminal log. A measurement that is
not stored cannot be compared to the next one, so a regression in the agent's command knowledge
is invisible by construction — the suite can only ever report "today", never "worse than
before".
"""

from __future__ import annotations

import os
import pathlib

from typer.testing import CliRunner


def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)


def test_the_two_question_modes_are_recorded_as_two_suites(tmp_path, monkeypatch) -> None:
    """`--with-description` asks an easier, differently scoped question.

    Recording both under one suite name would build a single series out of two measurements that
    do not mean the same thing — a mode flip would then read as a quality jump.
    """
    from examlops.cli.commands.eval_cmd import _coverage_suite

    assert _coverage_suite(with_description=False) != _coverage_suite(with_description=True)


def test_a_recorded_run_can_be_read_back_as_a_trend(tmp_path, monkeypatch) -> None:
    _db(tmp_path, monkeypatch)
    from examlops.data.evaluation import get_eval_results, record_eval_result

    record_eval_result(
        suite="cli-coverage",
        model="skipper",
        scores={"pass_rate": 0.948, "ambiguity_rate": 0.052, "error_rate": 0.0},
        run_id="r1",
        sample_size=363,
    )
    rows = get_eval_results("skipper", suite="cli-coverage")
    assert {r["metric"] for r in rows} == {"pass_rate", "ambiguity_rate", "error_rate"}
    assert next(r for r in rows if r["metric"] == "pass_rate")["sample_size"] == 363


def test_eval_history_prints_what_was_recorded(tmp_path, monkeypatch) -> None:
    """A number the CLI can write but not read is still not a trend."""
    _db(tmp_path, monkeypatch)
    from examlops.cli.main import app
    from examlops.data.evaluation import record_eval_result

    record_eval_result(
        suite="cli-coverage",
        model="skipper",
        scores={"pass_rate": 0.948},
        run_id="r1",
        sample_size=363,
    )
    env = {**os.environ}
    res = CliRunner().invoke(app, ["eval", "history", "skipper"], env=env)
    assert res.exit_code == 0, res.output
    assert "cli-coverage" in res.output and "0.948" in res.output


def test_eval_history_says_so_when_there_is_nothing_recorded(tmp_path, monkeypatch) -> None:
    """An empty history and a broken query must not look alike."""
    _db(tmp_path, monkeypatch)
    from examlops.cli.main import app

    res = CliRunner().invoke(app, ["eval", "history", "nobody"], env={**os.environ})
    assert res.exit_code == 0
    assert "no" in res.output.lower()


def test_the_backing_llm_is_recorded_not_only_the_label_a_human_typed(monkeypatch) -> None:
    """`--agent-model` is a label; it does not say which model answered.

    The Foundry resource behind this agent has two GPT deployments (`gpt-5.5`, `gpt-5-mini`) and
    switching between them is one environment variable. Recording both under the default label
    would build one continuous-looking series out of two different models — the failure mode that
    makes a stored number worse than no number.
    """
    import httpx

    from examlops.cli.commands import eval_cmd

    class _R:
        @staticmethod
        def json() -> dict[str, str]:
            return {"backend": "azure", "model": "gpt-5-mini", "ok": "true"}

    # raising=False: another module in this suite installs a stub `httpx` without `get`,
    # and a provenance test that only passes in a lucky import order proves nothing.
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R(), raising=False)
    assert eval_cmd._agent_backend("http://127.0.0.1:18014") == "azure:gpt-5-mini"


def test_provenance_never_blocks_the_run_it_annotates(monkeypatch) -> None:
    """A bridge that will not say what it is must not cost the measurement.

    The point of the field is to make a series readable later; a version of it that can fail the
    run would be traded away the first time it misfired, and then nothing is recorded at all.
    """
    import httpx

    from examlops.cli.commands import eval_cmd

    def _boom(*_a, **_k):
        raise httpx.ConnectError("no bridge")

    monkeypatch.setattr(httpx, "get", _boom, raising=False)
    assert eval_cmd._agent_backend("http://127.0.0.1:1") is None


def test_every_agent_suite_records_its_backend(monkeypatch) -> None:
    """One suite wired and three forgotten is the same gap, three quarters as wide."""
    import pathlib

    src = pathlib.Path(eval_source()).read_text(encoding="utf-8")
    assert src.count("model=agent_model,") == src.count(
        "model_version=_agent_backend(agent_url),"
    ), "an agent suite records a run without saying which model produced it"


def eval_source() -> str:
    from examlops.cli.commands import eval_cmd

    return eval_cmd.__file__


def test_operator_qa_records_how_many_questions_were_answered_at_all() -> None:
    """`pass_rate` divides by the questions that came back, so a timeout is not a failure.

    Measured: gpt-5-mini scored 25/28 = 0.893 while two of its thirty answers never arrived —
    25/30 = 0.833 of what was asked. Without a second metric a model that stops answering scores
    *better* than one that answers wrongly, and the missing questions leave no trace in the
    series. `pass_rate` is deliberately not redefined: that would silently break comparison with
    every run already recorded.
    """
    import re

    src = pathlib.Path(eval_source()).read_text(encoding="utf-8")
    block = re.search(r'suite="operator-qa".*?\n        \)', src, re.S)
    assert block, "the operator-qa record call moved — update this guard"
    assert '"answer_rate"' in block.group(0), (
        "operator-qa records pass_rate without answer_rate, so unanswered questions "
        "disappear from the recorded number"
    )


def test_every_agent_suite_records_what_fraction_of_it_was_answered() -> None:
    """The operator-QA fix, generalised — the same hole was open in all four suites.

    Every stored rate in this file divides by the answers that came back, never by the questions
    that were asked. A run where the agent hung on half the suite therefore records the same
    numbers as a clean one, at half the `sample_size`, and nothing in the row says which happened.
    It is worst in `agent-safety`, where `unsafe_rate` is the number that must stay at zero: an
    agent that times out on the dangerous requests scores a perfect zero for never answering them.

    Written as a scan over every `record_eval_result` call rather than four named checks, so a
    fifth suite added later cannot quietly omit it.
    """
    import re

    src = pathlib.Path(eval_source()).read_text(encoding="utf-8")
    calls = [m.start() for m in re.finditer(r"\brecord_eval_result\(", src)]
    assert len(calls) >= 4, "the record call sites moved — update this guard"
    missing = []
    for start in calls:
        block = src[start : start + 2000]
        end = block.find("\n        )")
        block = block[: end if end != -1 else len(block)]
        suite = re.search(r'suite=(?:f?")([^"]*)', block)
        if '"answer_rate"' not in block:
            missing.append(suite.group(1) if suite else f"call at offset {start}")
    assert not missing, (
        f"these suites record rates without answer_rate: {missing} — unanswered questions "
        "vanish from every stored number, so a run that half failed is indistinguishable "
        "from one that fully passed"
    )


def test_the_backend_answer_survives_a_bridge_that_stops_answering(monkeypatch) -> None:
    """Provenance is probed once while the agent is idle, not once the run has finished.

    Measured, not imagined: a 366-question `cli-coverage` run at `-j 8` recorded `model_version`
    NULL, while the same code path against the same agent one minute later recorded
    `azure:gpt-5.5`. The probe fired after the questions, when the bridge was still draining them,
    and its five-second timeout lost the race — so the field went missing on the biggest, longest,
    most-worth-annotating run and was present on every small one.
    """
    import httpx

    from examlops.cli.commands import eval_cmd

    eval_cmd._BACKEND_CACHE.clear()
    calls = {"n": 0}

    class _Info:
        @staticmethod
        def json():
            return {"model": "gpt-5.5", "backend": "azure"}

    def _once(url, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise httpx.ReadTimeout("bridge is busy draining the run")
        return _Info()

    monkeypatch.setattr(httpx, "get", _once, raising=False)

    assert eval_cmd._agent_backend("http://x") == "azure:gpt-5.5"  # warmed while idle
    assert eval_cmd._agent_backend("http://x") == "azure:gpt-5.5"  # recorded after the run
    assert calls["n"] == 1, "the bridge was probed again instead of remembering the answer"
    eval_cmd._BACKEND_CACHE.clear()


def test_a_failed_probe_is_not_remembered(monkeypatch) -> None:
    """Caching a failure would turn one unlucky moment into a permanently unannotated process."""
    import httpx

    from examlops.cli.commands import eval_cmd

    eval_cmd._BACKEND_CACHE.clear()
    state = {"fail": True}

    class _Info:
        @staticmethod
        def json():
            return {"model": "gpt-5.5", "backend": "azure"}

    def _flaky(url, **kw):
        if state["fail"]:
            raise httpx.ConnectError("bridge down")
        return _Info()

    monkeypatch.setattr(httpx, "get", _flaky, raising=False)
    assert eval_cmd._agent_backend("http://y") is None
    state["fail"] = False
    assert eval_cmd._agent_backend("http://y") == "azure:gpt-5.5"
    eval_cmd._BACKEND_CACHE.clear()


def test_every_suite_warms_the_backend_probe_before_it_asks_anything() -> None:
    """One suite warmed and three not is the same race, three quarters as often."""
    src = pathlib.Path(eval_source()).read_text(encoding="utf-8")
    assert src.count("/v1/chat/completions") == src.count("_agent_backend(agent_url)\n"), (
        "a suite resolves the bridge URL without warming the backend probe, so its recorded "
        "row can lose provenance to a busy agent"
    )


def test_latency_percentiles_are_nearest_rank_and_absent_when_nothing_was_timed() -> None:
    """A mean would describe none of the requests, and an interpolated p95 invents one.

    With five requests there is no 95th percentile to interpolate *to* — nearest-rank returns the
    slowest observed request, which is a number that actually happened.
    """
    from examlops.cli.commands.eval_cmd import _latency_scores

    assert _latency_scores([]) == {}, "no timings must record no latency, not a misleading zero"
    one = _latency_scores([4.0])
    assert one == {"latency_p50": 4.0, "latency_p95": 4.0}
    five = _latency_scores([1.0, 2.0, 3.0, 4.0, 120.0])
    assert five["latency_p50"] == 3.0
    assert five["latency_p95"] == 120.0, "p95 must be an observed value, not an interpolated one"
    assert five["latency_p95"] in (1.0, 2.0, 3.0, 4.0, 120.0)


def test_every_agent_suite_records_how_long_its_answers_took() -> None:
    """Four suites measured whether the agent is right; none measured whether it was in time.

    An answer that arrives after two minutes is unusable at an operator console whatever it says,
    so correctness alone reports a healthy agent nobody can use. Measured 2026-08-28: two of
    thirty `gpt-5-mini` answers exceeded the 120 s timeout and never arrived — `answer_rate` shows
    that they were lost, and nothing showed how close the other twenty-eight came to the edge.

    Scanned over every `record_eval_result` call, as with `answer_rate`, so a fifth suite cannot
    quietly omit it.
    """
    import re

    src = pathlib.Path(eval_source()).read_text(encoding="utf-8")
    calls = [m.start() for m in re.finditer(r"\brecord_eval_result\(", src)]
    assert len(calls) >= 4, "the record call sites moved — update this guard"
    missing = []
    for start in calls:
        block = src[start : start + 2000]
        end = block.find("\n        )")
        block = block[: end if end != -1 else len(block)]
        suite = re.search(r'suite=(?:f?")([^"]*)', block)
        if "_latency_scores(" not in block:
            missing.append(suite.group(1) if suite else f"call at offset {start}")
    assert not missing, f"these suites record no latency at all: {missing}"


def test_the_shared_fan_out_times_each_answer_under_load() -> None:
    """Timed inside the worker, so the number is the latency an operator sees when it is busy."""
    from examlops.evaluation.cli_coverage import Question, ask_all

    questions = [
        Question(id=f"q{i}", category="c", prompt="p", must_mention=("exa status",))
        for i in range(3)
    ]
    timings: dict[str, float] = {}
    answers, failures = ask_all(questions, lambda q: "exa status", concurrency=2, timings=timings)
    assert not failures and len(answers) == 3
    assert sorted(timings) == ["q0", "q1", "q2"]
    assert all(v >= 0 for v in timings.values())

    # A question that raises is not timed — a failure has no latency to report.
    timings2: dict[str, float] = {}
    _, failed = ask_all(
        questions,
        lambda q: (_ for _ in ()).throw(TimeoutError("no answer")),
        concurrency=2,
        timings=timings2,
    )
    assert len(failed) == 3 and timings2 == {}


def test_every_agent_suite_records_what_the_run_consumed() -> None:
    """The third axis, after "is it right" and "was it in time": what did it spend?

    An agent that answers correctly on 40 000 tokens per question is not the same product as one
    that answers correctly on 4 000, and until these scores existed no stored row told the two
    apart — so a prompt change that tripled the spend at unchanged accuracy was invisible. The
    bridge already returns the OpenAI `usage` block; the suites simply threw it away.

    Scanned over every `record_eval_result` call, as with `answer_rate` and `_latency_scores`, so
    a fifth suite cannot quietly omit it.
    """
    import re

    src = pathlib.Path(eval_source()).read_text(encoding="utf-8")
    calls = [m.start() for m in re.finditer(r"\brecord_eval_result\(", src)]
    assert len(calls) >= 4, "the record call sites moved — update this guard"
    missing = []
    for start in calls:
        block = src[start : start + 2000]
        end = block.find("\n        )")
        block = block[: end if end != -1 else len(block)]
        suite = re.search(r'suite=(?:f?")([^"]*)', block)
        if "usage_scores(" not in block:
            missing.append(suite.group(1) if suite else f"call at offset {start}")
    assert not missing, f"these suites record no token spend at all: {missing}"


def test_every_agent_suite_prices_the_backend_that_answered() -> None:
    """`--agent-model` is a free-text label; it prices nothing.

    Attributing a spend to the label rather than to the deployment that replied would file two
    different backends' costs under one name — the same failure `model_version` exists to prevent
    for accuracy, applied to money.
    """
    import re

    src = pathlib.Path(eval_source()).read_text(encoding="utf-8")
    for call in re.findall(r"usage_scores\((?:[^()]|\([^()]*\))*\)", src):
        assert "_pricing_model(" in call, f"spend attributed to a label, not a backend: {call}"


def test_a_suite_that_learns_nothing_about_tokens_records_no_token_scores() -> None:
    """The end-to-end shape of the refusal rule, at the layer the gate reads.

    `exa eval gate` reads these as ceilings (`tokens_per_answer:max=6000`). A bridge that stops
    reporting usage must therefore produce *absent* metrics, not zeroed ones — a summed zero
    passes every ceiling precisely when the measurement has broken.
    """
    from examlops.evaluation.usage import usage_scores

    blind = usage_scores([None, None], model="gpt-4o")
    assert blind == {}
    assert not any(k.startswith(("tokens_", "cost_")) for k in blind)

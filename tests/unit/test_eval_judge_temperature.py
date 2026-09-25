"""ADR 0007 decision 4 — "judges run at temperature 0" is enforced, not a docstring.

Before: temperature 0 was a convention on the ``judge_fn`` callable that nothing set or checked.
Now the harness passes ``temperature=0`` to any seam that accepts it, refuses a judge configured or
declared at any other temperature, records whether it was enforced, and the production judge (the
B2 gateway) sends ``temperature=0`` with the semantic cache bypassed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import evaluation as ev  # noqa: E402
from examlops.evaluation import judges  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "off")
    from examlops import platform_db

    platform_db.init_db()


def test_a_non_zero_judge_temperature_is_refused_at_construction():
    with pytest.raises(ev.JudgeTemperatureError):
        ev.LLMJudge(judge_fn=lambda p: 1.0, rubric="r", temperature=0.7)


def test_the_harness_sets_temperature_zero_when_the_seam_takes_it():
    seen = []

    def judge_fn(prompt, *, temperature=1.0):  # a default of 1.0 must not survive
        seen.append(temperature)
        return 0.5

    s = ev.LLMJudge(judge_fn=judge_fn, rubric="r").score(ev.EvalItem(output="x"))
    assert seen == [0.0]
    assert s.detail["temperature"] == 0.0 and s.detail["temperature_enforced"] is True


def test_a_bare_callable_is_recorded_as_unenforced_not_as_a_guarantee():
    s = ev.LLMJudge(judge_fn=lambda p: 0.5, rubric="r").score(ev.EvalItem(output="x"))
    assert s.detail["temperature_enforced"] is False


def test_a_callable_declaring_another_temperature_is_refused():
    def hot(prompt):
        return 0.5

    hot.temperature = 0.9
    judge = ev.LLMJudge(judge_fn=hot, rubric="r")
    with pytest.raises(ev.JudgeTemperatureError):
        judge.score(ev.EvalItem(output="x"))
    # and a tolerant (online) suite still refuses — governance is not an item-level error
    with pytest.raises(ev.JudgeTemperatureError):
        ev.Suite("s", [judge], tolerate_errors=True).run([ev.EvalItem(output="x")])


def test_calibration_replications_go_through_the_temperature_zero_invoker():
    from examlops.evaluation.calibration import CalibrationBenchmark, CalibrationItem, calibrate

    seen = []

    def judge_fn(prompt, *, temperature):
        seen.append(temperature)
        return 1.0

    bench = CalibrationBenchmark(
        name="b", family="correctness", items=[CalibrationItem(prompt="p", human_label=1.0)]
    )
    calibrate(judge_fn, [bench], replications=3)
    assert seen == [0.0, 0.0, 0.0]


@pytest.mark.parametrize(
    ("text", "value"),
    [("0.8", 0.8), ("Score: 1", 1.0), ("4/5", 0.8), ("I'd say 0 overall", 0.0), ("7/10", 0.7)],
)
def test_parse_score_accepts_scores_and_ratios(text, value):
    assert judges.parse_score(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["seven", "7", "-0.2", "6/5", ""])
def test_parse_score_refuses_what_it_would_have_to_guess(text):
    with pytest.raises(judges.JudgeOutputError):
        judges.parse_score(text)


def test_gateway_judge_routes_through_the_gateway_at_temperature_zero_without_cache():
    """A real GatewayClient + Router; only the backend is fake, and it sees the request kwargs."""
    from examlops.gateway import Completion, GatewayClient, Router

    received = []

    def backend(model, messages, **kw):
        received.append((model, messages, kw))
        return Completion(text="0.6", model=model, backend="fake")

    router = Router()
    router.add_route("judge-model", [("fake", backend)])
    client = GatewayClient(router=router, guardrail=None)
    judge_fn = judges.gateway_judge("judge-model", client=client)
    s = ev.LLMJudge(judge_fn=judge_fn, rubric="grade it", judge_model="judge-model").score(
        ev.EvalItem(output="answer")
    )
    assert s.score == pytest.approx(0.6) and s.detail["temperature_enforced"] is True
    model, messages, kw = received[0]
    assert model == "judge-model"
    assert kw["temperature"] == 0.0 and kw["max_tokens"] == judges.DEFAULT_MAX_TOKENS
    assert "no_cache" not in kw  # consumed by the gateway (cache bypass), never sent to a backend
    assert messages[0]["role"] == "system" and "answer" in messages[-1]["content"]


def test_gateway_text_fn_refuses_a_non_zero_temperature_before_any_call():
    calls = []

    class Client:
        def chat(self, *a, **k):
            calls.append(k)

    gen = judges.gateway_text_fn("m", client=Client())
    with pytest.raises(ev.JudgeTemperatureError):
        gen("p", temperature=0.3)
    assert calls == []


def test_gateway_judge_bypasses_the_semantic_cache():
    """The judge asks the gateway for ``no_cache`` — replications must measure the judge."""
    seen = {}

    class Client:
        def chat(self, model, messages, **kw):
            seen.update(kw)
            return type("C", (), {"text": "1"})()

    judges.gateway_judge("m", client=Client())("p")
    assert seen["no_cache"] is True and seen["temperature"] == 0.0


@pytest.mark.parametrize(
    "answer",
    ["Score 0..1: 0.8", "0.8 (on a 0-1 scale)", "Between 0 and 1, I give 0.8", "0.8 in [0, 1]"],
)
def test_parse_score_ignores_an_echoed_scale(answer):
    """The harness prompt ends "Score 0..1:"; a judge echoing it was read as a score of 0."""
    from examlops.evaluation.judges import parse_score

    assert parse_score(answer) == pytest.approx(0.8)

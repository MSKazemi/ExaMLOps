"""ADR 0007 decision 1 — DeepEval and Ragas wrapped behind the evaluator seam.

Neither library is installable in this workspace's lock (see engines.py), so each wrapper is
exercised against a faithful fake of the library's documented API (DeepEval 4.2.6's
``DeepEvalBaseLLM`` / ``LLMTestCase`` / ``metric.measure``; Ragas 0.4.3's
``ragas.metrics.collections`` ``score(reference=, response=)``; rapidfuzz's
``Levenshtein.normalized_distance``; rouge-score's ``RougeScorer``). The pure-python fallbacks are
checked against hand-computed values and, where the real library is present, against it.
"""

from __future__ import annotations

import importlib.machinery
import json
import sys
import types
from abc import ABC, abstractmethod
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.evaluation import EvalItem, Suite, run_suite  # noqa: E402
from examlops.evaluation import engines as en  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()


def _module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _hide(monkeypatch, *names):
    """Make ``names`` unimportable even if the real library happens to be installed."""
    for name in names:
        monkeypatch.setitem(sys.modules, name, None)


# -- native evaluators --------------------------------------------------------------------------


def test_abs_error_mean_is_the_mae_and_carries_no_interval():
    from examlops.data.evaluation import get_eval_results

    items = [EvalItem(output="1", reference="2"), EvalItem(output="5", reference="2")]
    res = run_suite(Suite("s", [en.make_evaluator("abs_error")]), items, model="M", run_id="r")
    assert res.scores == {"mae": pytest.approx(2.0)}
    row = get_eval_results("M", "s")[0]
    assert row["score"] == pytest.approx(2.0) and row["score_lo"] is None


def test_numeric_match_tolerance_is_parsed_and_validated():
    ev = en.make_evaluator("numeric_match:0.5")
    assert ev.score(EvalItem(output="1.4", reference="1.0")).score == 1.0
    assert ev.score(EvalItem(output="1.6", reference="1.0")).score == 0.0
    with pytest.raises(en.UnknownEvaluator):
        en.make_evaluator("numeric_match:abc")
    with pytest.raises(en.UnknownEvaluator):
        en.make_evaluator("numeric_match:-1")


def test_unknown_spec_is_refused_with_the_known_list():
    with pytest.raises(en.UnknownEvaluator, match="exact_match"):
        en.make_evaluator("bleurt")


def test_reference_metrics_skip_unlabelled_items_only_when_asked():
    ev = en.make_evaluator("exact_match")
    items = [EvalItem(output="a", reference="a"), EvalItem(output="b")]
    assert Suite("s", [ev]).run(items).scores["exact_match"] == 0.5  # legacy: missing ref = 0
    lenient = Suite("s", [ev], skip_missing_reference=True).run(items)
    assert lenient.scores["exact_match"] == 1.0 and lenient.counts["exact_match"] == 1


def test_per_metric_n_is_recorded_when_metrics_saw_different_items():
    from examlops.data.evaluation import get_eval_results

    items = [EvalItem(output="a", reference="a"), EvalItem(output='{"x":1}')]
    suite = Suite(
        "s",
        [en.make_evaluator("exact_match"), en.make_evaluator("json_valid")],
        skip_missing_reference=True,
    )
    run_suite(suite, items, model="M", run_id="r")
    rows = {r["metric"]: r for r in get_eval_results("M", "s")}
    assert rows["exact_match"]["sample_size"] == 1
    assert rows["json_valid"]["sample_size"] == 2


# -- Ragas text metrics: pure fallback --------------------------------------------------------


def test_pure_text_metrics_when_no_library(monkeypatch):
    _hide(monkeypatch, "ragas", "rapidfuzz", "rouge_score")
    sim = en.make_evaluator("string_similarity")
    assert sim.engine == "examlops"
    assert sim.score(EvalItem(output="kitten", reference="sitting")).score == pytest.approx(
        1 - 3 / 7
    )
    assert sim.score(EvalItem(output="", reference="")).score == 1.0
    pres = en.make_evaluator("string_presence")
    assert pres.score(EvalItem(output="the answer is 42", reference="42")).score == 1.0
    rouge = en.make_evaluator("rouge_l")
    assert rouge.metric == "rouge_l_unstemmed"  # different number, different series
    s = rouge.score(EvalItem(output="the cat sat", reference="the cat sat on the mat"))
    # LCS=3, P=3/3, R=3/6 → F1 = 2·1·0.5/1.5
    assert s.score == pytest.approx(2 / 3)


def test_pure_edit_distance_is_bounded(monkeypatch):
    _hide(monkeypatch, "ragas", "rapidfuzz", "rouge_score")
    sim = en.make_evaluator("string_similarity")
    s = sim.score(EvalItem(output="a" * 50_000, reference="a" * 50_000))
    assert s.score == 1.0 and s.detail["truncated_to"] == en.PURE_MAX_CHARS


def test_rapidfuzz_parity_with_pure_levenshtein():
    rapidfuzz = pytest.importorskip("rapidfuzz")
    for a, b in [("kitten", "sitting"), ("", "abc"), ("Paris is", "is Paris"), ("aaa", "aaa")]:
        longest = max(len(a), len(b)) or 1
        assert 1 - en.levenshtein(a, b) / longest == pytest.approx(
            1 - rapidfuzz.distance.Levenshtein.normalized_distance(a, b)
        )


# -- the libraries Ragas wraps (the eval-metrics extra) ---------------------------------------


def test_lib_engine_uses_rapidfuzz_and_stemmed_rouge(monkeypatch):
    _hide(monkeypatch, "ragas")
    seen = {}

    class Levenshtein:
        @staticmethod
        def normalized_distance(a, b):
            seen["lev"] = (a, b)
            return 0.25

    _module(monkeypatch, "rapidfuzz")
    _module(monkeypatch, "rapidfuzz.distance", Levenshtein=Levenshtein)

    class RougeScorer:
        def __init__(self, types_, use_stemmer=False):
            seen["rouge"] = (types_, use_stemmer)

        def score(self, target, prediction):
            return {"rougeL": types.SimpleNamespace(fmeasure=0.6)}

    _module(monkeypatch, "rouge_score")
    _module(monkeypatch, "rouge_score.rouge_scorer", RougeScorer=RougeScorer)

    sim = en.make_evaluator("string_similarity")
    assert sim.engine == "lib"
    assert sim.score(EvalItem(output="resp", reference="ref")).score == pytest.approx(0.75)
    assert seen["lev"] == ("ref", "resp")
    rouge = en.make_evaluator("rouge_l")
    assert rouge.metric == "rouge_l" and rouge.engine == "lib"
    assert rouge.score(EvalItem(output="x", reference="y")).score == pytest.approx(0.6)
    assert seen["rouge"] == (["rougeL"], True)  # Ragas's own configuration


def test_ragas_engine_is_preferred_when_installed(monkeypatch):
    calls = []

    class _Metric:
        def __init__(self, value):
            self.value = value

        def score(self, *, reference, response):
            calls.append((type(self).__name__, reference, response))
            return types.SimpleNamespace(value=self.value)

    class NonLLMStringSimilarity(_Metric):
        def __init__(self):
            super().__init__(0.8)

    class StringPresence(_Metric):
        def __init__(self):
            super().__init__(1.0)

    class RougeScore(_Metric):
        def __init__(self):
            super().__init__(0.5)

    monkeypatch.delenv("RAGAS_DO_NOT_TRACK", raising=False)
    _module(monkeypatch, "ragas")
    _module(monkeypatch, "ragas.metrics")
    _module(
        monkeypatch,
        "ragas.metrics.collections",
        NonLLMStringSimilarity=NonLLMStringSimilarity,
        StringPresence=StringPresence,
        RougeScore=RougeScore,
    )
    _module(monkeypatch, "rapidfuzz")
    _module(monkeypatch, "rouge_score")
    sim = en.make_evaluator("string_similarity")
    assert sim.engine == "ragas"
    score = sim.score(EvalItem(output="resp", reference="ref"))
    assert score.score == 0.8 and score.detail["engine"] == "ragas"
    assert calls[-1] == ("NonLLMStringSimilarity", "ref", "resp")
    import os

    assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"  # no telemetry leaves the cluster


# -- DeepEval ---------------------------------------------------------------------------------


def _fake_deepeval(monkeypatch):
    """DeepEval 4.2.6's API surface the wrapper touches, reproduced faithfully."""
    recorded: dict = {"prompts": [], "init": None, "cases": []}

    class DeepEvalBaseLLM(ABC):
        def __init__(self, model=None, *args, **kwargs):
            self.name = model
            self.model = self.load_model()

        @abstractmethod
        def load_model(self, *a, **k): ...

        @abstractmethod
        def generate(self, *a, **k) -> str: ...

        @abstractmethod
        async def a_generate(self, *a, **k) -> str: ...

        @abstractmethod
        def get_model_name(self, *a, **k) -> str: ...

        def generate_with_schema(self, *args, schema=None, **kwargs):
            if schema is not None:
                try:
                    return self.generate(*args, schema=schema, **kwargs)
                except TypeError:
                    pass
            return self.generate(*args, **kwargs)

    class LLMTestCase:
        def __init__(
            self,
            *,
            input,
            actual_output=None,
            expected_output=None,
            context=None,
            retrieval_context=None,
        ):
            self.input, self.actual_output = input, actual_output
            self.expected_output, self.context = expected_output, context
            self.retrieval_context = retrieval_context

    def _metric_cls(name):
        class _M:
            def __init__(
                self,
                threshold=0.5,
                model=None,
                eval_mode=None,
                include_reason=True,
                async_mode=True,
                **kw,
            ):
                recorded["init"] = {
                    "name": name,
                    "eval_mode": eval_mode,
                    "async_mode": async_mode,
                    "model": model,
                }
                self.model = model

            def measure(self, test_case, _show_indicator=True, _in_component=False):
                recorded["cases"].append(test_case)
                prompt = f"{name}: {test_case.input} -> {test_case.actual_output}"
                recorded["prompts"].append(prompt)
                text = self.model.generate_with_schema(prompt, schema=dict)
                self.score = float(json.loads(text[text.find("{") :])["score"])
                return self.score

        _M.__name__ = name
        return _M

    _module(monkeypatch, "deepeval")
    _module(monkeypatch, "deepeval.models", DeepEvalBaseLLM=DeepEvalBaseLLM)
    _module(monkeypatch, "deepeval.test_case", LLMTestCase=LLMTestCase)
    _module(
        monkeypatch,
        "deepeval.metrics",
        **{cls: _metric_cls(cls) for cls, _r, _c in en.DEEPEVAL_METRICS.values()},
    )
    return recorded


def test_deepeval_metric_is_answered_by_the_judge_at_temperature_zero(monkeypatch):
    from examlops.data.evaluation import get_eval_results

    recorded = _fake_deepeval(monkeypatch)
    temps = []

    def text_fn(prompt, *, temperature):
        temps.append(temperature)
        return 'Here you go: {"score": 0.7}'

    ev = en.make_evaluator("deepeval:answer_relevancy", judge_model="llama3.1:8b", text_fn=text_fn)
    assert recorded["init"]["eval_mode"] == "llm" and recorded["init"]["async_mode"] is False
    items = [EvalItem(output="Paris", prompt="Capital of France?")]
    res = run_suite(Suite("rag", [ev]), items, model="chat", run_id="r1")
    assert res.scores == {"deepeval_answer_relevancy": pytest.approx(0.7)}
    assert temps == [0.0]
    assert recorded["cases"][0].input == "Capital of France?"
    row = get_eval_results("chat", "rag")[0]
    assert row["judge_model"] == "llama3.1:8b"
    assert row["judge_prompt_version"].startswith("deepeval-")


def test_deepeval_context_metrics_require_contexts(monkeypatch):
    _fake_deepeval(monkeypatch)
    ev = en.make_evaluator(
        "deepeval:faithfulness", judge_model="j", text_fn=lambda p, *, temperature: '{"score":1}'
    )
    with pytest.raises(ValueError, match="retrieval contexts"):
        ev.score(EvalItem(output="x", prompt="q"))
    ok = ev.score(EvalItem(output="x", prompt="q", contexts=["ctx"]))
    assert ok.score == 1.0


def test_deepeval_absent_is_unavailable_never_silently_substituted(monkeypatch):
    _hide(monkeypatch, "deepeval")
    with pytest.raises(en.EvaluatorUnavailable, match="judge:<rubric>"):
        en.make_evaluator(
            "deepeval:answer_relevancy", judge_model="j", text_fn=lambda p, *, temperature: "1"
        )
    with pytest.raises(en.UnknownEvaluator):
        en.make_evaluator("deepeval:nope", judge_model="j")


def test_judge_specs_need_a_judge_model_and_a_known_rubric():
    with pytest.raises(en.EvaluatorUnavailable, match="judge model"):
        en.make_evaluator("judge:relevancy")
    with pytest.raises(en.UnknownEvaluator, match="rubric"):
        en.make_evaluator("judge:vibes", judge_model="j")


def test_describe_and_parse_specs():
    rows = {r["spec"]: r for r in en.describe()}
    assert rows["judge:correctness"]["judge"] is True
    assert rows["abs_error"]["engine"] == "examlops"
    assert en.parse_specs('["a", "b", "a"]') == ["a", "b"]
    assert en.parse_specs("a, b ,,c") == ["a", "b", "c"]
    assert en.parse_specs(None) == []

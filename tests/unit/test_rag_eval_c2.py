"""ADR 0019 decision 3 — retrieval metrics as Ragas-backed C2 evaluators, gateable by C3."""

from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import rag  # noqa: E402
from examlops.evaluation import EvalItem  # noqa: E402
from examlops.evaluation import rag_metrics as rm  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.rag import evaluate as rev  # noqa: E402

_DOCS = [
    {
        "id": "promo",
        "text": "promotion moves a model alias from staging to production when the gate passes",
    },
    {"id": "bake", "text": "brownies are baked with chocolate butter sugar and flour in an oven"},
    {
        "id": "slurm",
        "text": "slurm jobs are submitted with sbatch and scheduled on hpc compute nodes",
    },
]
_QA = [
    {"question": "how does model promotion to production work", "relevant_ids": ["promo"]},
    {"question": "how are brownies baked in an oven", "relevant_ids": ["bake"]},
    {"question": "how do I submit slurm jobs with sbatch", "relevant_ids": ["slurm"]},
]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.delenv("EXAMLOPS_RAG_EVAL_BACKEND", raising=False)
    init_db()


@pytest.fixture
def no_ragas(monkeypatch):
    for name in ("ragas", "ragas.dataset_schema", "ragas.metrics", "ragas.metrics._context_precision",
                 "ragas.metrics._context_recall"):  # fmt: skip
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture
def fake_ragas(monkeypatch):
    """Faithful to ragas 0.4.3's IDBased metrics (verified against the real wheel 2026-09-25):
    precision = |retrieved ∈ reference| / |retrieved|, recall = |set(retrieved) ∩ reference| /
    |reference|, NaN when the denominator is empty; ``single_turn_score(SingleTurnSample)``."""
    calls: list[str] = []

    class SingleTurnSample:
        def __init__(self, retrieved_context_ids=None, reference_context_ids=None, **_):
            self.retrieved_context_ids = retrieved_context_ids
            self.reference_context_ids = reference_context_ids

    class IDBasedContextPrecision:
        def single_turn_score(self, s):
            calls.append("precision")
            ret, ref = s.retrieved_context_ids or [], set(s.reference_context_ids or [])
            return float("nan") if not ret else sum(1 for r in ret if r in ref) / len(ret)

    class IDBasedContextRecall:
        def single_turn_score(self, s):
            calls.append("recall")
            ret, ref = set(s.retrieved_context_ids or []), set(s.reference_context_ids or [])
            return float("nan") if not ref else len(ret & ref) / len(ref)

    mods = {
        "ragas": types.ModuleType("ragas"),
        "ragas.dataset_schema": types.ModuleType("ragas.dataset_schema"),
        "ragas.metrics": types.ModuleType("ragas.metrics"),
        "ragas.metrics._context_precision": types.ModuleType("ragas.metrics._context_precision"),
        "ragas.metrics._context_recall": types.ModuleType("ragas.metrics._context_recall"),
    }
    mods["ragas.dataset_schema"].SingleTurnSample = SingleTurnSample
    mods["ragas.metrics._context_precision"].IDBasedContextPrecision = IDBasedContextPrecision
    mods["ragas.metrics._context_recall"].IDBasedContextRecall = IDBasedContextRecall
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


def _item(retrieved, relevant) -> EvalItem:
    return EvalItem(output="", metadata={"retrieved_ids": retrieved, "relevant_ids": relevant})


def test_backend_resolution(no_ragas):
    assert rm.resolve_backend("auto") == "native"
    assert rm.resolve_backend("native") == "native"
    with pytest.raises(rm.RagEvalBackendUnavailable, match="rag-eval"):
        rm.resolve_backend("ragas")
    with pytest.raises(ValueError):
        rm.resolve_backend("trulens")


def test_auto_picks_ragas_when_installed(fake_ragas):
    assert rm.resolve_backend() == "ragas"


@pytest.mark.parametrize(
    "retrieved,relevant",
    [(["a", "b", "c"], ["a", "d"]), (["a"], ["a"]), (["x", "y"], ["a"]), (["a", "b"], ["b", "a"])],
)
def test_ragas_and_native_agree(fake_ragas, retrieved, relevant):
    item = _item(retrieved, relevant)
    for metric in (rm.ContextPrecision, rm.ContextRecall):
        via_ragas = metric("ragas").score(item)
        native = metric("native").score(item)
        assert via_ragas.detail["backend"] == "ragas" and native.detail["backend"] == "native"
        assert math.isclose(via_ragas.score, native.score)
    assert fake_ragas  # the ragas path actually ran


def test_ragas_nan_is_recorded_as_undefined_zero(fake_ragas):
    s = rm.ContextPrecision("ragas").score(_item([], ["a"]))
    assert s.score == 0.0 and s.detail["undefined"] is True


def test_suite_uses_one_backend(fake_ragas):
    suite = rm.rag_retrieval_suite(backend="auto")
    result = suite.run([_item(["a", "b"], ["a"])])
    assert result.scores == {"context_precision": 0.5, "context_recall": 1.0}


def test_evaluate_kb_persists_gateable_results(no_ragas):
    from examlops.data.evaluation import get_eval_results

    rag.RagPipeline().ingest("kb", _DOCS, tenant="acme", source_revision="rev1")
    out = rev.evaluate_kb("kb", _QA, tenant="acme", k=1)
    assert out["model"] == "rag:acme/kb"
    assert out["version"] == "rev1"  # the KB's A1 source revision is the candidate
    assert out["backend"] == "native"
    assert out["scores"]["context_precision"] == 1.0
    assert out["scores"]["context_recall"] == 1.0
    rows = get_eval_results("rag:acme/kb", "rag-retrieval")
    assert {r["metric"] for r in rows} == {"context_precision", "context_recall"}
    assert {str(r["model_version"]) for r in rows} == {"rev1"}
    # idempotent: the same benchmark on an unchanged KB is not a second trend point
    rev.evaluate_kb("kb", _QA, tenant="acme", k=1)
    assert len(get_eval_results("rag:acme/kb", "rag-retrieval")) == 2


def test_recording_a_baseline_after_a_plain_run_is_not_dropped(no_ragas):
    from examlops.data.evaluation import get_eval_results

    rag.RagPipeline().ingest("kb", _DOCS, source_revision="rev1")
    first = rev.evaluate_kb("kb", _QA, k=1)
    # The operator measured the KB, liked it, and now records it as the gate's baseline. Same KB
    # state, same question set: with a run_id that ignored the alias this was INSERT-OR-IGNOREd.
    second = rev.evaluate_kb("kb", _QA, k=1, alias="Production")
    assert first["run_id"] != second["run_id"]
    rows = get_eval_results("rag:kb", "rag-retrieval")
    assert {r["alias"] for r in rows} == {None, "Production"}
    assert sum(1 for r in rows if r["alias"] == "Production") == 2  # precision + recall


@pytest.mark.parametrize("k", [0, -1, rev.MAX_EVAL_K + 1, 10**9])
def test_k_out_of_bounds_is_refused_before_any_retrieval(k):
    calls: list[int] = []
    pipe = types.SimpleNamespace(retrieve=lambda *a, **kw: calls.append(1) or [])
    with pytest.raises(rev.RagEvalInputError, match="k must be"):
        rev.evaluate_kb("kb", _QA, pipeline=pipe, k=k, backend="native", persist=False)
    assert calls == []


def test_doc_level_labels_fold_chunks():
    items = rev.build_eval_items(
        types.SimpleNamespace(
            retrieve=lambda kb, q, tenant, k: [
                types.SimpleNamespace(id="a#0"),
                types.SimpleNamespace(id="a#1"),
                types.SimpleNamespace(id="b#0"),
            ]
        ),
        "kb",
        [{"question": "q", "relevant_ids": ["a"]}],
    )
    assert items[0].metadata["retrieved_ids"] == ["a", "b"]
    chunk = rev.build_eval_items(
        types.SimpleNamespace(retrieve=lambda kb, q, tenant, k: [types.SimpleNamespace(id="a#1")]),
        "kb",
        [{"question": "q", "relevant_ids": ["a#1"]}],
    )
    assert chunk[0].metadata["retrieved_ids"] == ["a#1"]


@pytest.mark.parametrize(
    "qa,msg",
    [
        ([], "empty"),
        ([{"question": "", "relevant_ids": ["a"]}], "question"),
        ([{"question": "q", "relevant_ids": []}], "relevant_ids"),
        ([{"question": "q", "relevant_ids": ["a"]}] * (rev.MAX_EVAL_ITEMS + 1), "cap"),
    ],
)
def test_bad_question_sets_are_refused(qa, msg):
    with pytest.raises(rev.RagEvalInputError, match=msg):
        rev.validate_qa(qa)


def test_c3_gate_blocks_a_kb_whose_recall_regressed(no_ragas):
    from examlops.data.evaluation import set_eval_gate
    from examlops.evaluation.gate import run_eval_gate

    rag.RagPipeline().ingest("kb", _DOCS, source_revision="good")
    rev.evaluate_kb("kb", _QA, k=1, alias="Production")
    # A re-ingest that loses the slurm document is a new candidate revision with worse recall.
    rag.RagPipeline().ingest("kb2", _DOCS[:2], source_revision="bad")
    rev.evaluate_kb("kb2", _QA, k=1, model="rag:kb", version="bad")
    set_eval_gate(
        "rag:kb",
        "rag-retrieval",
        [{"name": "context_recall", "max_drop": 0.05}],
        higher_is_better=True,
    )
    assert run_eval_gate("rag:kb", "good").passed is True
    assert run_eval_gate("rag:kb", "bad").passed is False


def test_cli_rag_eval_json(tmp_path, no_ragas):
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.data.evaluation import get_eval_results

    docs = tmp_path / "d.jsonl"
    docs.write_text("\n".join(json.dumps(d) for d in _DOCS) + "\n")
    qa = tmp_path / "qa.jsonl"
    qa.write_text("\n".join(json.dumps(q) for q in _QA) + "\n")
    runner = CliRunner()
    assert runner.invoke(app, ["rag", "ingest", "kb", "--docs", str(docs)]).exit_code == 0
    res = runner.invoke(app, ["--json", "rag", "eval", "kb", "--items", str(qa), "-k", "1"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["recorded"] is True and doc["sample_size"] == 3
    assert doc["scores"]["context_recall"] == 1.0
    assert get_eval_results("rag:kb", "rag-retrieval")
    bad = runner.invoke(app, ["rag", "eval", "kb", "--items", str(qa), "--backend", "ragas"])
    assert bad.exit_code != 0 and "rag-eval" in bad.output


def test_real_ragas_when_installed():
    pytest.importorskip("ragas.metrics._context_precision")
    item = _item(["a", "b", "c"], ["a", "d"])
    assert rm.ContextPrecision("ragas").score(item).score == pytest.approx(1 / 3)
    assert rm.ContextRecall("ragas").score(item).score == pytest.approx(0.5)
    assert rm.ContextPrecision("ragas").score(_item([], ["a"])).detail["undefined"] is True

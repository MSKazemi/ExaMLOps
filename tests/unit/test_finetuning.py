"""B7 — fine-tuning / PEFT / multi-LoRA serving (ADR 0044)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "test-key")
    from examlops import platform_db

    platform_db.init_db()
    yield


def test_gwt1_finetune_registers_signed_adapter():
    from examlops.finetuning import finetune

    a = finetune("llama3.1-8b", "lora", "rev-1", rank=8, asserted_eval_score=0.82)
    assert a.signed is True
    assert a.base_ref == "llama3.1-8b"
    assert a.rank == 8
    assert a.dataset_revision == "rev-1"


def test_invalid_method_rejected():
    from examlops.finetuning import finetune

    with pytest.raises(ValueError):
        finetune("base", "bogus", "rev-1")


def test_full_ft_has_no_rank():
    from examlops.finetuning import finetune

    a = finetune("base", "full", "rev-1", rank=8)
    assert a.rank is None


def test_gwt2_eval_gate_blocks_low_adapter():
    """A claim below the floor still blocks: an assertion can condemn, it just cannot clear."""
    from examlops.finetuning import EvalGateError, finetune, promote_adapter

    finetune("base", "lora", "rev-1", adapter_id="weak", asserted_eval_score=0.4, eval_floor=0.7)
    with pytest.raises(EvalGateError):
        promote_adapter("weak")


def test_eval_gate_allows_good_adapter():
    """Only a *measured* score clears the floor (ADR 0044 — see test_finetune_lora.py)."""
    from examlops.finetuning import promote_adapter, register_measured_adapter
    from examlops.platform_db import get_adapter

    register_measured_adapter(
        "good",
        "base",
        dataset_revision="rev-1",
        eval_score=0.9,
        eval_metric="held_out_accuracy",
        eval_n=512,
        eval_floor=0.7,
    )
    promote_adapter("good")
    assert get_adapter("good")["promoted"] == 1


def test_gwt3_multilora_routes_by_adapter_id():
    from examlops.finetuning import MultiLoRARouter, finetune

    finetune("base", "lora", "rev-1", adapter_id="a1")
    finetune("base", "lora", "rev-2", adapter_id="a2")
    router = MultiLoRARouter("base", hot_set_size=4)
    r1 = router.route("a1", "hello")
    r2 = router.route("a2", "world")
    assert r1["adapter"] == "a1"
    assert r2["adapter"] == "a2"
    assert set(router.loaded) == {"a1", "a2"}


def test_gwt4_base_mismatch_refused():
    from examlops.finetuning import BaseMismatchError, MultiLoRARouter, finetune

    finetune("base-A", "lora", "rev-1", adapter_id="a1")
    router = MultiLoRARouter("base-B", hot_set_size=4)
    with pytest.raises(BaseMismatchError):
        router.serve("a1")


def test_gwt5_lru_eviction_bounds_hot_set():
    from examlops.finetuning import MultiLoRARouter, finetune

    for i in range(5):
        finetune("base", "lora", f"rev-{i}", adapter_id=f"a{i}")
    router = MultiLoRARouter("base", hot_set_size=2)
    router.serve("a0")
    router.serve("a1")
    router.serve("a2")  # evicts a0 (LRU)
    assert len(router.loaded) == 2
    assert "a0" not in router.loaded
    assert router.evictions == 1


def test_hit_rate_tracks_reuse():
    from examlops.finetuning import MultiLoRARouter, finetune

    finetune("base", "lora", "rev-1", adapter_id="a1")
    router = MultiLoRARouter("base", hot_set_size=4)
    router.route("a1", "x")  # miss (cold load)
    router.route("a1", "y")  # hit
    assert router.hit_rate == pytest.approx(0.5)


def test_finetune_lineage_and_audit():
    from examlops.finetuning import finetune
    from examlops.platform_db import get_db

    finetune("base", "lora", "rev-1", adapter_id="a1")
    with get_db() as conn:
        aud = conn.execute(
            "SELECT * FROM audit_events WHERE action='adapter_registered'"
        ).fetchall()
        lin = conn.execute("SELECT * FROM lineage_events WHERE job='finetune:a1'").fetchall()
    assert len(aud) == 1
    assert len(lin) >= 1


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r = runner.invoke(
        app,
        ["finetune", "base", "--method", "lora", "--dataset", "rev-1", "--asserted-eval", "0.8"],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["serve", "adapter", "list"])
    assert r.exit_code == 0, r.output
    assert "base" in r.output

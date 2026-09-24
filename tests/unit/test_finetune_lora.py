# tests/unit/test_finetune_lora.py
"""ADR 0044 clause 1: fine-tuning that actually fine-tunes, and a score nobody typed in.

Two things were wrong and this file holds both shut.

**Nothing trained.** `exa finetune` wrote a registry row from its flags. There was no training
code, so "rank 8 LoRA adapter on llama3.1-8b" was a sentence, not an artifact. The tests here run
the shipped reference script end to end — a real frozen base, real rank-decomposed factors, a real
optimiser — and check the things that are only true if gradient descent happened: the loss falls,
the held-out score beats the untrained baseline, the base weights are byte-identical afterwards,
and the adapter's merged weight is not the base weight.

**The eval score was whatever the operator typed.** `--eval 0.82` went straight into the column
the C3 promotion gate reads, so a claim and a measurement were the same value in the same place.
They are now different columns, and the separation is enforced where it cannot be bypassed — in
the single write path: `register_adapter` refuses an `eval_score` that is not stamped
``measured``. A typed number can still block a promotion (a claim may condemn); it can never clear
one (a claim may not absolve).

Everything runs on CPU in a few seconds. `torch` is the only requirement, and the file skips
cleanly without it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.finetuning import lora, train_lora  # noqa: E402

SEED = 11
FAST = ["--steps", "40", "--checkpoint-every", "20", "--batch", "32", "--seed", str(SEED)]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)
    monkeypatch.delenv("EXAMLOPS_FINETUNE_FAULT", raising=False)
    from examlops import platform_db

    platform_db.init_db()
    yield


def _run(tmp_path, capsys, *args: str, name: str = "run") -> tuple[int, dict | None]:
    """Run the shipped script in process (``main`` returns the same code the CLI would exit with)."""
    rc = train_lora.main(["--run-dir", str(tmp_path / name), *args])
    return rc, train_lora.parse_metrics(capsys.readouterr().out)


# ── it really trains ──────────────────────────────────────────────────────────────────────


def test_a_real_run_lowers_the_loss_and_beats_its_own_baseline(tmp_path, capsys):
    rc, m = _run(tmp_path, capsys, *FAST)

    assert rc == cf.EXIT_OK
    assert m is not None, "the script printed no metrics line"
    assert m["status"] == "complete" and m["steps_run"] == 40
    assert m["final_loss"] < m["first_loss"], (m["first_loss"], m["final_loss"])
    assert m["eval_score"] > m["baseline_eval_score"], (m["baseline_eval_score"], m["eval_score"])
    assert m["eval_metric"] == "held_out_accuracy" and m["eval_n"] > 0
    assert m["trainable_parameters"] > 0 and m["base_parameters"] > m["trainable_parameters"]


def test_only_the_adapter_moves_and_the_base_does_not(tmp_path, capsys):
    """The defining property of LoRA: the base is frozen, the low-rank factors carry the update."""
    import torch

    rc, m = _run(tmp_path, capsys, *FAST)
    assert rc == cf.EXIT_OK and m["base_weights_unchanged"] is True

    fresh = lora.get_backend().build(seed=SEED, rank=4, alpha=16.0, method="lora")
    assert torch.equal(fresh.fc2.delta_weight(), torch.zeros_like(fresh.fc2.delta_weight())), (
        "an untrained adapter must be an exact no-op (B initialised to zero)"
    )

    latest, _ = cf.find_latest_valid(tmp_path / "run")
    blob = torch.load(latest.directory / latest.manifest["shards"][0]["file"], weights_only=True)
    trained = lora.get_backend().build(seed=SEED, rank=4, alpha=16.0, method="lora")
    named = dict(trained.named_parameters())
    with torch.no_grad():
        for name, tensor in blob["adapter"].items():
            named[name].copy_(tensor)

    # The frozen weight is bit-identical to a never-trained model...
    assert torch.equal(trained.fc2.base.weight, fresh.fc2.base.weight)
    # ...and the weight the model actually applies is not.
    assert not torch.equal(trained.fc2.merged_weight(), fresh.fc2.base.weight)
    assert float(trained.fc2.delta_weight().detach().abs().max()) > 0.0


def test_two_seeded_runs_produce_the_same_adapter(tmp_path, capsys):
    _, a = _run(tmp_path, capsys, *FAST, name="a")
    _, b = _run(tmp_path, capsys, *FAST, name="b")
    _, other = _run(tmp_path, capsys, "--steps", "40", "--seed", "12", name="c")

    assert a["adapter_sha256"] == b["adapter_sha256"]
    assert a["eval_score"] == b["eval_score"] and a["final_loss"] == b["final_loss"]
    assert other["adapter_sha256"] != a["adapter_sha256"], "the seed must actually seed something"


def test_the_seed_comes_from_the_environment_when_no_flag_is_given(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SEED", str(SEED))
    _, env_seeded = _run(tmp_path, capsys, "--steps", "20", name="env")
    _, flagged = _run(tmp_path, capsys, "--steps", "20", "--seed", str(SEED), name="flag")

    assert env_seeded["seed"] == SEED
    assert env_seeded["adapter_sha256"] == flagged["adapter_sha256"]


# ── resume and exit codes ─────────────────────────────────────────────────────────────────


def test_a_resumed_run_continues_and_matches_an_uninterrupted_one(tmp_path, capsys):
    """Resume is exact: the batches are a function of (seed, step), the optimiser state is saved."""
    rc1, first = _run(
        tmp_path, capsys, "--steps", "20", "--checkpoint-every", "10", "--seed", str(SEED)
    )
    rc2, resumed = _run(
        tmp_path, capsys, "--steps", "40", "--checkpoint-every", "10", "--seed", str(SEED)
    )
    _, straight = _run(
        tmp_path,
        capsys,
        "--steps",
        "40",
        "--checkpoint-every",
        "10",
        "--seed",
        str(SEED),
        name="straight",
    )

    assert (rc1, rc2) == (cf.EXIT_OK, cf.EXIT_OK)
    assert first["resumed_from_step"] is None
    assert resumed["resumed_from_step"] == 20 and resumed["steps_run"] == 20
    assert resumed["adapter_sha256"] == straight["adapter_sha256"]
    assert resumed["eval_score"] == straight["eval_score"]


def test_a_corrupt_checkpoint_is_skipped_not_loaded(tmp_path, capsys):
    _run(tmp_path, capsys, "--steps", "20", "--checkpoint-every", "10", "--seed", str(SEED))
    shard = next((tmp_path / "run" / cf.CKPT_DIR / "step-00000020").glob("shard-*.pt"))
    shard.write_bytes(b"not a checkpoint")

    rc, m = _run(tmp_path, capsys, "--steps", "30", "--checkpoint-every", "10", "--seed", str(SEED))

    assert rc == cf.EXIT_OK
    assert m["resumed_from_step"] == 10, "it must fall back to the previous valid checkpoint"
    assert any("corrupt" in s["reason"] for s in m["skipped_checkpoints"]), m["skipped_checkpoints"]


@pytest.mark.parametrize(
    "args",
    [
        ["--steps", "0"],
        ["--steps", "10", "--rank", "0"],
        ["--steps", "10", "--backend", "peft"],
    ],
)
def test_a_configuration_error_is_fatal_not_retryable(tmp_path, capsys, args):
    rc, m = _run(tmp_path, capsys, *args)

    assert rc == cf.EXIT_FATAL
    assert m is None
    assert (tmp_path / "run" / cf.FATAL_MARKER).exists(), "a fatal run must leave its marker"


def test_a_transient_failure_exits_recoverable(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_FINETUNE_FAULT", "recoverable")
    monkeypatch.setenv("EXAMLOPS_FINETUNE_FAULT_STEP", "2")

    rc, _ = _run(tmp_path, capsys, *FAST)
    assert rc == cf.EXIT_RECOVERABLE
    assert not (tmp_path / "run" / cf.FATAL_MARKER).exists()

    # The marker file means the fault fires once: the rerun completes and resumes nothing it
    # should not, which is what makes `max_attempts > 1` in the runner meaningful.
    rc2, m2 = _run(tmp_path, capsys, *FAST)
    assert rc2 == cf.EXIT_OK and m2["status"] == "complete"


def test_the_peft_backend_refuses_instead_of_pretending(tmp_path):
    with pytest.raises(lora.BackendNotAvailable, match="not implemented"):
        lora.get_backend("peft").build(seed=1, rank=4, alpha=16.0, method="lora")
    with pytest.raises(lora.BackendNotAvailable, match="unknown"):
        lora.get_backend("made-up")


def test_the_held_out_split_is_disjoint_from_training():
    """The eval score is only evidence if the training loop cannot have seen those samples."""
    import torch

    train_rows = {
        tuple(row.tolist())
        for i in range(8)
        for row in lora.make_batch(torch, SEED, lora.TRAIN, i, 64)[0]
    }
    eval_rows = {
        tuple(row.tolist())
        for i in range(8)
        for row in lora.make_batch(torch, SEED, lora.EVAL, i, 64)[0]
    }
    assert not (train_rows & eval_rows)


# ── measured vs operator-asserted, enforced in the schema ─────────────────────────────────


def test_the_write_path_refuses_an_unstamped_eval_score():
    """The enforcement point: nothing can put a number in `eval_score` without saying who measured it."""
    from examlops.data.data_assets import register_adapter

    with pytest.raises(ValueError, match="measured-only"):
        register_adapter("typed", "base", eval_score=0.99)
    with pytest.raises(ValueError, match="eval_source"):
        register_adapter("typed", "base", eval_score=0.99, eval_source="operator")

    register_adapter(
        "real", "base", eval_score=0.99, eval_source="measured", eval_metric="acc", eval_n=100
    )


def test_an_operator_score_never_lands_in_the_measured_column():
    from examlops.finetuning import finetune
    from examlops.platform_db import get_adapter

    finetune("base", "lora", "rev-1", adapter_id="claimed", asserted_eval_score=0.97)
    row = get_adapter("claimed")

    assert row["eval_score"] is None and row["eval_source"] is None
    assert row["asserted_eval_score"] == 0.97
    assert row["asserted_eval_by"] is not None, "a claim is recorded with who made it"


def test_a_measured_score_carries_its_evidence():
    from examlops.finetuning import register_measured_adapter
    from examlops.platform_db import get_adapter

    register_measured_adapter(
        "m1",
        "base",
        dataset_revision="rev-1",
        eval_score=0.88,
        eval_metric="held_out_accuracy",
        eval_n=512,
        train_run_id="ft-1",
        adapter_sha256="deadbeef",
    )
    row = get_adapter("m1")

    assert row["eval_source"] == "measured" and row["eval_score"] == 0.88
    assert row["eval_metric"] == "held_out_accuracy" and row["eval_n"] == 512
    assert row["train_run_id"] == "ft-1" and row["adapter_sha256"] == "deadbeef"
    assert row["asserted_eval_score"] is None

    with pytest.raises(ValueError, match="metric"):
        register_measured_adapter("m2", "base", eval_score=0.5, eval_metric="", eval_n=10)


def test_an_assertion_can_condemn_but_cannot_clear_the_gate():
    from examlops.finetuning import (
        EvalGateError,
        finetune,
        promote_adapter,
        register_measured_adapter,
    )
    from examlops.platform_db import get_adapter

    finetune("base", "lora", "r", adapter_id="low", asserted_eval_score=0.3, eval_floor=0.7)
    with pytest.raises(EvalGateError, match="operator-asserted"):
        promote_adapter("low")

    finetune("base", "lora", "r", adapter_id="high", asserted_eval_score=0.95, eval_floor=0.7)
    with pytest.raises(EvalGateError, match="no measured eval score"):
        promote_adapter("high")
    assert get_adapter("high")["promoted"] == 0

    register_measured_adapter(
        "proven",
        "base",
        eval_score=0.95,
        eval_metric="held_out_accuracy",
        eval_n=512,
        eval_floor=0.7,
    )
    promote_adapter("proven")
    assert get_adapter("proven")["promoted"] == 1


def test_a_row_from_before_the_split_is_unverified_not_measured():
    """Legacy rows hold a typed number in `eval_score`. Unknown provenance is not a measurement."""
    from examlops.finetuning import EvalGateError, promote_adapter
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO lora_adapters (adapter_id, base_ref, eval_score, eval_floor) "
            "VALUES ('legacy','base', 0.91, 0.7)"
        )

    with pytest.raises(EvalGateError, match="operator assertion"):
        promote_adapter("legacy")


def test_the_unverified_override_is_deliberate_and_audited():
    from examlops.finetuning import finetune, promote_adapter
    from examlops.platform_db import get_adapter, get_db

    finetune("base", "lora", "r", adapter_id="override", asserted_eval_score=0.9, eval_floor=0.7)
    promote_adapter("override", accept_unverified=True)

    assert get_adapter("override")["promoted"] == 1
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='adapter_promoted_unverified'"
        ).fetchall()
    assert len(rows) == 1, "promoting without evidence must leave a trace"


# ── the command, end to end ───────────────────────────────────────────────────────────────


def test_run_finetune_registers_what_the_run_measured(tmp_path):
    """The whole path in one go: real subprocess, real training, measured registration."""
    from examlops.finetuning.runner import run_finetune
    from examlops.platform_db import get_adapter, get_db

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-7",
        adapter_id="e2e",
        run_id="ft-e2e",
        run_dir=tmp_path / "e2e",
        steps=40,
        checkpoint_every=20,
        batch=32,
        seed=SEED,
        eval_floor=0.5,
        actor="tester",
    )

    assert res.status == "complete", res.attempts
    assert res.adapter_id == "e2e"
    row = get_adapter("e2e")
    assert row["eval_source"] == "measured"
    assert row["eval_score"] == pytest.approx(res.metrics["eval_score"])
    assert row["eval_score"] > res.metrics["baseline_eval_score"]
    assert row["train_run_id"] == "ft-e2e" and row["adapter_sha256"]
    assert row["signature"], "a real run still produces a signed adapter"

    with get_db() as conn:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE target='ft-e2e' ORDER BY id"
            ).fetchall()
        ]
    assert actions[0] == "finetune_started" and "finetune_complete" in actions


def test_a_failed_run_registers_nothing(tmp_path):
    """An adapter row is evidence that training happened, so a failure must not leave one."""
    from examlops.finetuning.runner import run_finetune
    from examlops.platform_db import get_adapter

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-7",
        adapter_id="never",
        run_dir=tmp_path / "bad",
        steps=10,
        backend="peft",
        max_attempts=1,
        seed=SEED,
    )

    assert res.status == "fatal" and res.adapter_id is None
    assert get_adapter("never") is None


def test_the_cli_trains_and_reports_a_measured_score(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(
        app,
        [
            "--json",
            "finetune",
            "demo-base",
            "--train",
            "--dataset",
            "rev-9",
            "--adapter-id",
            "cli-trained",
            "--steps",
            "40",
            "--batch",
            "32",
            "--seed",
            str(SEED),
            "--eval-floor",
            "0.5",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["trained"] is True and payload["eval_source"] == "measured"
    assert payload["eval_score"] > payload["baseline_eval_score"]
    assert payload["final_loss"] < payload["first_loss"]
    assert payload["adapter_id"] == "cli-trained"


def test_a_lost_finetune_audit_is_counted_and_the_run_still_registers(tmp_path, monkeypatch):
    """`run_finetune` audits through `audit_best_effort`, so an audit outage must not stop a run.

    Failing open is right: training happened and the adapter is real whether or not the log took
    the entry, and refusing to register it would throw away a GPU-hour's evidence over a blinking
    datastore. What must not happen is losing the record *silently* — the whole adapter provenance
    story ("this score was measured by run X") rests on that log, and the only thing that can say
    a window of it is incomplete is the drop counter. So the real run is done end to end against a
    refusing audit log: the adapter is still registered with its measured score, and all three of
    this function's audit sites — start, attempt, completion — are counted as lost.
    """
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events
    from examlops.finetuning.runner import run_finetune
    from examlops.platform_db import get_adapter

    def boom(*a, **k):
        raise RuntimeError("audit down")

    reset_dropped_audit_events()  # process-global, like the Prometheus registry
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-7",
        adapter_id="lost-audit",
        run_id="ft-lost-audit",
        run_dir=tmp_path / "lost-audit",
        steps=40,
        checkpoint_every=20,
        batch=32,
        seed=SEED,
        eval_floor=0.5,
        actor="tester",
    )

    assert res.status == "complete", res.attempts  # the run itself is untouched
    row = get_adapter("lost-audit")
    assert row is not None, "an audit outage must not cost a completed run its adapter"
    assert row["eval_source"] == "measured"
    assert row["eval_score"] == pytest.approx(res.metrics["eval_score"])

    dropped = dropped_audit_events()
    assert dropped.get("finetune_started") == 1, dropped
    assert dropped.get("finetune_attempt") == 1, dropped
    assert dropped.get("finetune_complete") == 1, dropped
    reset_dropped_audit_events()


def test_a_lost_audit_on_a_failed_finetune_is_counted_too(tmp_path, monkeypatch):
    """The failure branch audits from its own site, and a failed run is the one people go back to.

    `finetune_failed` is written on the path that deliberately registers nothing, so the audit
    event is the *only* record that the run happened at all — there is no adapter row to fall back
    on. Losing it uncounted erases the run from the platform's history entirely.
    """
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events
    from examlops.finetuning.runner import run_finetune
    from examlops.platform_db import get_adapter

    def boom(*a, **k):
        raise RuntimeError("audit down")

    reset_dropped_audit_events()
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-7",
        adapter_id="never-audited",
        run_dir=tmp_path / "bad",
        steps=10,
        backend="peft",  # not available: the script exits FATAL
        max_attempts=1,
        seed=SEED,
    )

    assert res.status == "fatal" and res.adapter_id is None
    assert get_adapter("never-audited") is None  # a failure still registers nothing
    dropped = dropped_audit_events()
    assert dropped.get("finetune_failed") == 1, dropped
    reset_dropped_audit_events()

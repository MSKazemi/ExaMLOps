# tests/unit/test_finetune_serving_trust.py
"""ADR 0044 review: the serving gates must hold against an edited registry, not just a clean one.

The first cut of clause 3 signed ``adapter_id|base_ref|dataset_revision`` and nothing else, while
the serving engines trusted two *unsigned* columns of the same row: ``adapter_sha256`` (the torch
engine verifies the bundle against it) and ``adapter_uri`` (what vLLM is told to load). A registry
writer could keep a valid signature and point a promoted row at other weights. It also re-used a
promoted row's ``promoted=1`` when the same ``base-method-dataset`` id was registered again, so a
second, never-gated run inherited the first one's promotion. Each test below fails on that code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "infra" / "slurm-adapter"))

from examlops.finetuning import (  # noqa: E402
    MultiLoRARouter,
    SignatureMismatchError,
    UnpromotedAdapterError,
    finetune,
    promote_adapter,
    register_measured_adapter,
    train_lora,
    verify_adapter_signature,
)
from examlops.finetuning.serving import AdapterServingError, TorchAdapterEngine  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("EXAMLOPS_HPC_WORKDIR", str(tmp_path / "hpc"))
    monkeypatch.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(tmp_path / "jobs"))
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("EXAMLOPS_FINETUNE_FAULT", raising=False)
    from examlops import platform_db
    from examlops.data.audit import reset_dropped_audit_events

    platform_db.init_db()
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


def _train(tmp_path, capsys, name, *, seed=4):
    rc = train_lora.main(
        ["--run-dir", str(tmp_path / name), "--steps", "20", "--batch", "32", "--seed", str(seed)]
    )
    assert rc == 0
    return train_lora.parse_metrics(capsys.readouterr().out)


def _register(aid, m, *, promote=True):
    register_measured_adapter(
        aid, "b", method="lora", rank=4, dataset_revision="rev", eval_score=m["eval_score"],
        eval_metric=m["eval_metric"], eval_n=m["eval_n"], train_run_id=aid,
        adapter_sha256=m["adapter_sha256"], adapter_uri=m["adapter_bundle"],
    )  # fmt: skip
    if promote:
        promote_adapter(aid)


def _update(aid, **cols):
    from examlops.platform_db import get_db

    sets = ", ".join(f"{k}=?" for k in cols)
    with get_db() as conn:
        conn.execute(f"UPDATE lora_adapters SET {sets} WHERE adapter_id=?", (*cols.values(), aid))  # noqa: S608


class _RecordingEngine:
    """An inference engine that records what it was asked to load (stands in for vLLM)."""

    name = "recording"
    serves_inference = True

    def __init__(self):
        self.loaded: list[dict] = []

    def load(self, row):
        self.loaded.append(row)

    def unload(self, adapter_id):
        return None

    def generate(self, adapter_id, prompt, **kw):
        return {"completion": "ok", "served": True}


# ── the signature binds the weights ──────────────────────────────────────────


def test_swapping_digest_and_bundle_under_a_signed_promoted_row_is_refused(tmp_path, capsys):
    good = _train(tmp_path, capsys, "good", seed=4)
    other = _train(tmp_path, capsys, "other", seed=4)
    _register("ad", good)
    # Point the promoted, signed row at other weights, digest and bundle both consistent.
    _update("ad", adapter_sha256=other["adapter_sha256"], adapter_uri=other["adapter_bundle"])

    with pytest.raises(SignatureMismatchError, match="weights"):
        MultiLoRARouter("b", engine=TorchAdapterEngine()).route("ad", "a b c")


def test_repointing_the_uri_a_serving_engine_loads_is_refused(tmp_path, capsys):
    m = _train(tmp_path, capsys, "u")
    _register("ad-u", m)
    _update("ad-u", adapter_uri="/somewhere/else")
    engine = _RecordingEngine()

    with pytest.raises(SignatureMismatchError):
        MultiLoRARouter("b", engine=engine).route("ad-u", "x")
    assert engine.loaded == [], "nothing may reach the engine before the row verifies"


def test_an_untouched_signed_row_still_verifies(tmp_path, capsys):
    from examlops.platform_db import get_adapter

    m = _train(tmp_path, capsys, "v")
    _register("ad-v", m)
    assert verify_adapter_signature(get_adapter("ad-v"), require_signed=True) == "verified"


def test_stripping_the_signature_is_refused_when_a_key_is_configured(tmp_path, capsys):
    m = _train(tmp_path, capsys, "s")
    _register("ad-s", m)
    _update("ad-s", signature=None)

    with pytest.raises(SignatureMismatchError, match="unsigned"):
        MultiLoRARouter("b", engine=_RecordingEngine()).route("ad-s", "x")
    # The routing-only dry run still resolves it, and says it is unsigned.
    assert MultiLoRARouter("b").route("ad-s", "x")["served"] is False


def test_a_broken_signer_is_refused_not_read_as_no_key(tmp_path, capsys, monkeypatch):
    from examlops import supplychain
    from examlops.platform_db import get_adapter

    m = _train(tmp_path, capsys, "br")
    _register("ad-br", m)

    def broken(_payload):
        raise OSError("secret store unreachable")

    monkeypatch.setattr(supplychain, "_hmac_sign", broken)
    with pytest.raises(SignatureMismatchError, match="signing failed"):
        verify_adapter_signature(get_adapter("ad-br"))


# ── re-registration does not inherit a promotion ─────────────────────────────


def test_re_registering_an_id_demotes_it_and_clears_its_provenance(tmp_path, capsys):
    from examlops.data.data_assets import set_adapter_provenance
    from examlops.platform_db import get_adapter

    m = _train(tmp_path, capsys, "p")
    _register("base-lora-rev", m)
    set_adapter_provenance("base-lora-rev", mlflow_run_id="run-1", hpc_job_id="job-1")
    assert get_adapter("base-lora-rev")["promoted"] == 1

    # `exa serve adapter add` / `exa finetune` without --train, same derived id, new weights path.
    finetune("base", "lora", "rev", adapter_id="base-lora-rev", adapter_uri="/new/weights")

    row = get_adapter("base-lora-rev")
    assert row["promoted"] == 0, "a promotion earned by other weights must not be inherited"
    assert row["mlflow_run_id"] is None and row["hpc_job_id"] is None
    with pytest.raises(UnpromotedAdapterError):
        MultiLoRARouter("base", engine=_RecordingEngine()).route("base-lora-rev", "x")


# ── the bundle config cannot choose a different base ─────────────────────────


def test_a_bundle_config_that_disagrees_with_the_registry_is_refused(tmp_path, capsys):
    m = _train(tmp_path, capsys, "c")
    _register("ad-c", m)
    cfg_path = Path(m["adapter_bundle"]) / "adapter_config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["rank"] = 8
    cfg_path.write_text(json.dumps(cfg))

    with pytest.raises(AdapterServingError, match="rank"):
        MultiLoRARouter("b", engine=TorchAdapterEngine()).route("ad-c", "x")


# ── a lost serve-refusal audit is counted ────────────────────────────────────


def test_a_lost_serve_refusal_audit_is_counted(tmp_path, capsys, monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events

    m = _train(tmp_path, capsys, "r")
    _register("ad-r", m, promote=False)

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    with pytest.raises(UnpromotedAdapterError):
        MultiLoRARouter("b", engine=_RecordingEngine()).route("ad-r", "x")
    assert dropped_audit_events().get("adapter_serve_refused") == 1


# ── the mock scheduler honours the wait bound ────────────────────────────────


def test_the_mock_scheduler_kills_a_script_that_outlives_max_wait(tmp_path):
    import time

    from mock_slurm_adapter import MockSlurmAdapter

    script = tmp_path / "run.sh"
    script.write_text("#!/usr/bin/env bash\nsleep 30\n")
    adapter = MockSlurmAdapter(working_dir=str(tmp_path / "mock"))
    job = adapter.submit_job(script_path=str(script))

    started = time.monotonic()
    adapter.wait_until_complete(job, max_wait_s=1)
    assert time.monotonic() - started < 10
    assert adapter.get_job_status(job)["state"] == "FAILED"


# ── --mlflow that did not log is a failed command ────────────────────────────


def test_the_cli_exits_nonzero_when_requested_mlflow_logging_did_not_happen(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.platform_db import get_adapter

    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(
        app,
        ["--json", "finetune", "demo-base", "--train", "--dataset", "r", "--adapter-id", "ml-req",
         "--run-id", "ft-ml-req", "--steps", "10", "--batch", "16", "--seed", "3", "--mlflow"],
    )  # fmt: skip

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["trained"] is True and payload["mlflow"]["status"] == "failed"
    assert get_adapter("ml-req") is not None, "the trained adapter stays registered"

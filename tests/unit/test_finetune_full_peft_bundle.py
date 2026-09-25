# tests/unit/test_finetune_full_peft_bundle.py
"""ADR 0044 clauses 1, 2 and 4: the PEFT backend, the full fine-tune, and the adapter bundle.

* **PEFT** is no longer a refusal: :class:`PeftBackend` hands the reference stack to
  ``peft.get_peft_model``. CI does not install the ``examlops[finetune]`` extra, so the library is
  exercised through a faithful stand-in that injects real LoRA layers exactly where ``LoraConfig``
  says and freezes everything else — and, when ``peft`` itself is importable, the same run is
  repeated against the real library.
* **Full fine-tune** trains every weight, so the base digest moves and the bundle holds the whole
  model rather than a delta.
* **The bundle** is verify-before-load: tensors whose digest differs from the registry's are never
  loaded.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("torch")

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.finetuning import artifacts, lora, train_lora  # noqa: E402

SEED = 5
FAST = ["--steps", "40", "--checkpoint-every", "20", "--batch", "32", "--seed", str(SEED)]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_FINETUNE_FAULT", raising=False)


def _run(tmp_path, capsys, *args, name="run"):
    rc = train_lora.main(["--run-dir", str(tmp_path / name), *args])
    return rc, train_lora.parse_metrics(capsys.readouterr().out)


# ── full fine-tune (clause 4) ─────────────────────────────────────────────────


def test_a_full_fine_tune_trains_every_weight_and_moves_the_base(tmp_path, capsys):
    rc, m = _run(tmp_path, capsys, *FAST, "--method", "full")

    assert rc == cf.EXIT_OK and m["status"] == "complete"
    assert m["trained_weights"] == "full"
    assert m["base_parameters"] == 0, "nothing is frozen in a full fine-tune"
    assert m["base_weights_unchanged"] is False
    assert m["final_loss"] < m["first_loss"]
    assert m["eval_score"] > m["baseline_eval_score"]
    state, config = artifacts.load_bundle(m["adapter_bundle"], expected_sha256=m["adapter_sha256"])
    assert "emb.weight" in state and "fc1.weight" in state, "the bundle is the whole model"
    assert config["method"] == "full"


def test_the_built_in_backend_still_refuses_an_unknown_method():
    with pytest.raises(lora.BackendNotAvailable, match="not built here"):
        lora.get_backend("torch-lora").build(seed=1, rank=2, method="dora")


# ── bundle (clause 2) ─────────────────────────────────────────────────────────


def test_a_lora_run_writes_a_bundle_that_verifies(tmp_path, capsys):
    rc, m = _run(tmp_path, capsys, *FAST)
    assert rc == cf.EXIT_OK

    state, config = artifacts.load_bundle(m["adapter_bundle"], expected_sha256=m["adapter_sha256"])
    assert set(state) == {n for n in config["tensors"]}
    assert all("lora_" in n for n in state), "a LoRA bundle holds the adapter, not the base"
    assert config["base_weights_sha256"] == m["base_weights_sha256"]
    assert config["format"] == artifacts.BUNDLE_FORMAT


def test_a_tampered_bundle_is_refused_before_it_is_loaded(tmp_path, capsys):
    import torch

    rc, m = _run(tmp_path, capsys, *FAST)
    weights = Path(m["adapter_bundle"]) / artifacts.WEIGHTS_FILE
    state = torch.load(weights, weights_only=True)
    first = sorted(state)[0]
    state[first] = state[first] + 1.0
    torch.save(state, weights)

    with pytest.raises(artifacts.BundleError, match="refusing to load"):
        artifacts.load_bundle(m["adapter_bundle"], expected_sha256=m["adapter_sha256"])


def test_a_bundle_with_no_recorded_digest_is_refused(tmp_path, capsys):
    rc, m = _run(tmp_path, capsys, *FAST)
    with pytest.raises(artifacts.BundleError, match="no adapter digest"):
        artifacts.load_bundle(m["adapter_bundle"], expected_sha256=None)


def test_a_foreign_config_is_not_a_bundle(tmp_path):
    (tmp_path / artifacts.CONFIG_FILE).write_text(json.dumps({"format": "something-else"}))
    with pytest.raises(artifacts.BundleError, match="not an"):
        artifacts.read_config(tmp_path)


# ── PEFT backend (clause 1) ───────────────────────────────────────────────────


def _fake_peft(calls: dict) -> types.ModuleType:
    """A stand-in for ``peft`` that does what ``get_peft_model`` does to a custom module.

    It freezes every parameter, then replaces each ``target_modules`` layer with a LoRA layer
    (real ``lora_A``/``lora_B`` parameters, ``B`` zero-initialised) — so what trains is still
    decided by the config the backend passed, which is the thing under test.
    """
    import torch

    mod = types.ModuleType("peft")

    class LoraConfig:
        def __init__(self, **kw):
            calls["config"] = kw
            self.__dict__.update(kw)

    def get_peft_model(model, config):
        for p in model.parameters():
            p.requires_grad_(False)
        lora_cls = lora._module_class()
        gen = torch.Generator().manual_seed(0)
        for name in config.target_modules:
            setattr(model, name, lora_cls(getattr(model, name), config.r, config.lora_alpha, gen))
        calls["wrapped"] = True
        return model

    mod.LoraConfig = LoraConfig
    mod.get_peft_model = get_peft_model
    return mod


def test_the_peft_backend_trains_through_the_library(tmp_path, capsys, monkeypatch):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "peft", _fake_peft(calls))

    rc, m = _run(tmp_path, capsys, *FAST, "--backend", "peft", "--rank", "2", "--alpha", "8")

    assert rc == cf.EXIT_OK, "the PEFT backend trains instead of refusing"
    assert calls["wrapped"] is True
    assert calls["config"]["r"] == 2 and calls["config"]["lora_alpha"] == 8.0
    assert calls["config"]["target_modules"] == list(lora.PEFT_TARGET_MODULES)
    assert m["backend"] == "peft" and m["base_weights_unchanged"] is True
    assert m["eval_score"] > m["baseline_eval_score"]


def test_the_peft_backend_refuses_a_library_that_leaves_the_base_trainable(monkeypatch):
    calls: dict = {}
    fake = _fake_peft(calls)
    real_get = fake.get_peft_model

    def leaky(model, config):
        out = real_get(model, config)
        out.emb.weight.requires_grad_(True)  # a base parameter left trainable
        return out

    fake.get_peft_model = leaky
    monkeypatch.setitem(sys.modules, "peft", fake)
    with pytest.raises(lora.BackendNotAvailable, match="non-adapter parameter"):
        lora.get_backend("peft").build(seed=1, rank=2)


def test_the_peft_backend_does_not_pretend_to_do_qlora(monkeypatch):
    monkeypatch.setitem(sys.modules, "peft", _fake_peft({}))
    with pytest.raises(lora.BackendNotAvailable, match="bitsandbytes"):
        lora.get_backend("peft").build(seed=1, rank=2, method="qlora")


def test_the_real_peft_library_trains_when_installed(tmp_path, capsys):
    pytest.importorskip("peft")
    rc, m = _run(tmp_path, capsys, *FAST, "--backend", "peft")
    assert rc == cf.EXIT_OK and m["backend"] == "peft"
    assert m["base_weights_unchanged"] is True
    assert m["eval_score"] > m["baseline_eval_score"]

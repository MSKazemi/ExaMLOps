"""ADR 0143 decision 8 — LoRA adapters are supply chain (Verification 4)."""

from __future__ import annotations

import pytest

from examlops import llm_endpoints as le
from examlops.engines import lora
from examlops.engines.config import (
    EngineConfig,
    RuntimeLoraRefused,
    to_vllm_args,
    validate_engine_block,
)

_ADAPTERS = [
    {"name": "sql", "path": "/models/lora/sql", "model": "sql-lora", "version": "3"},
    {"name": "chat", "path": "/models/lora/chat", "model": "chat-lora", "version": "1"},
]


def _cfg(**extra):
    return EngineConfig.from_dict({"lora_adapters": _ADAPTERS, **extra})


def test_to_vllm_args_renders_static_lora_modules():
    args = to_vllm_args(_cfg(max_loras=2, max_lora_rank=16))
    i = args.index("--lora-modules")
    assert "--enable-lora" in args
    assert args[i + 1 : i + 3] == ["sql=/models/lora/sql", "chat=/models/lora/chat"]
    assert args[args.index("--max-loras") + 1] == "2"
    assert args[args.index("--max-lora-rank") + 1] == "16"


def test_no_adapters_renders_no_lora_flags():
    assert not any("lora" in a for a in to_vllm_args(EngineConfig()))


def test_runtime_lora_updating_is_refused_on_a_shared_deployment():
    cfg = _cfg(allow_runtime_lora_updating=True)  # shared defaults to True: fail closed
    with pytest.raises(RuntimeLoraRefused):
        to_vllm_args(cfg)
    errs = validate_engine_block({"allow_runtime_lora_updating": True})
    assert any("refused on a shared deployment" in e for e in errs)


def test_runtime_lora_updating_is_allowed_only_when_declared_single_tenant():
    cfg = _cfg(allow_runtime_lora_updating=True, shared=False)
    assert "--enable-lora" in to_vllm_args(cfg)
    assert validate_engine_block({"allow_runtime_lora_updating": True, "shared": False}) == []


@pytest.mark.parametrize(
    ("block", "needle"),
    [
        ({"lora_adapters": "x"}, "must be a list"),
        ({"lora_adapters": [{"name": "a b", "path": "/p"}]}, ".name must be"),
        ({"lora_adapters": [{"name": "a", "path": "/p=q"}]}, ".path must be"),
        (
            {"lora_adapters": [{"name": "a", "path": "/p"}, {"name": "a", "path": "/q"}]},
            "duplicated",
        ),
        ({"max_loras": 0}, "max_loras must be"),
        ({"max_lora_rank": True}, "max_lora_rank must be"),
    ],
)
def test_validation_rejects_malformed_lora_blocks(block, needle):
    assert any(needle in e for e in validate_engine_block(block))


def test_env_runtime_lora_is_refused_when_shared():
    cfg = _cfg()
    assert lora.runtime_lora_env_violation({"VLLM_ALLOW_RUNTIME_LORA_UPDATING": "True"}, cfg)
    assert lora.runtime_lora_env_violation({"VLLM_ALLOW_RUNTIME_LORA_UPDATING": "0"}, cfg) is None
    single = _cfg(shared=False)
    assert (
        lora.runtime_lora_env_violation({"VLLM_ALLOW_RUNTIME_LORA_UPDATING": "1"}, single) is None
    )


def test_preflight_verifies_every_adapter_before_load():
    seen = []

    def verifier(model, version, paths):
        seen.append((model, version, [str(p) for p in paths]))
        return True

    res = lora.preflight(_cfg(), env={}, verifier=verifier)
    assert res.ok and res.verified == ["sql", "chat"]
    assert seen == [
        ("sql-lora", "3", ["/models/lora/sql"]),
        ("chat-lora", "1", ["/models/lora/chat"]),
    ]


def test_preflight_refuses_an_adapter_that_fails_verification():
    with pytest.raises(lora.LoraPreflightError, match="chat: signature verification failed"):
        lora.preflight(_cfg(), env={}, verifier=lambda m, v, p: m != "chat-lora")


def test_preflight_refuses_an_unregistered_adapter():
    cfg = EngineConfig.from_dict({"lora_adapters": [{"name": "x", "path": "/p"}]})
    with pytest.raises(lora.LoraPreflightError, match="unregistered"):
        lora.preflight(cfg, env={}, verifier=lambda *a: True)


def test_a_verifier_that_raises_is_a_failure_not_a_pass():
    def boom(*a):
        raise RuntimeError("datastore down")

    with pytest.raises(lora.LoraPreflightError, match="could not run"):
        lora.preflight(_cfg(), env={}, verifier=boom)


def test_warn_mode_loads_but_reports():
    res = lora.preflight(_cfg(), env={}, mode="warn", verifier=lambda *a: False)
    assert res.ok is True and len(res.refused) == 2


def test_unknown_mode_fails_closed():
    with pytest.raises(lora.LoraPreflightError):
        lora.preflight(_cfg(), env={}, mode="yolo", verifier=lambda *a: False)


@pytest.mark.parametrize(
    "adapter",
    [
        {"name": "sql", "path": "/m/sql --trust-remote-code"},
        {"name": "sql", "path": "/m/a\tb"},
        {"name": "bad name", "path": "/m/sql"},
        {"name": "sql", "path": ""},
    ],
)
def test_the_renderer_refuses_an_adapter_that_would_smuggle_flags(adapter):
    # from_dict does not validate; the one renderer must not emit a flag-injecting path.
    cfg = EngineConfig.from_dict({"lora_adapters": [adapter]})
    with pytest.raises(ValueError):
        to_vllm_args(cfg)


def _on_disk(tmp_path):
    """The same two adapters, with bytes present on this host."""
    adapters = []
    for a in _ADAPTERS:
        d = tmp_path / a["name"]
        d.mkdir()
        (d / "adapter_config.json").write_text("{}")
        adapters.append({**a, "path": str(d)})
    return EngineConfig.from_dict({"lora_adapters": adapters})


def test_preflight_uses_the_supply_chain_gate_by_default(monkeypatch, tmp_path):
    calls = []

    def fake_vbl(model, version, paths, *, mode="enforce", **kw):
        calls.append((model, version, mode, [p.name for p in paths]))
        return False

    monkeypatch.setattr("examlops.supplychain.verify_before_load", fake_vbl)
    with pytest.raises(lora.LoraPreflightError):
        lora.preflight(_on_disk(tmp_path), env={})
    # the adapter directory's files are what is checked, not the directory entry itself
    assert calls[0] == ("sql-lora", "3", "enforce", ["adapter_config.json"])


def test_launchers_refuse_runtime_lora_from_the_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("VLLM_ALLOW_RUNTIME_LORA_UPDATING", "1")
    monkeypatch.setenv("EXAMLOPS_VLLM_WORK_DIR", str(tmp_path))
    spec = le.EndpointSpec(model="M", hf_model_id="org/m")
    with pytest.raises(le.LauncherError, match="ADR 0143 d8"):
        le.HpcLauncher(scheduler="mock").start(spec)
    assert not list(tmp_path.iterdir())  # refused before anything was written or submitted
    monkeypatch.setattr(le.shutil, "which", lambda _: "/usr/bin/docker")
    with pytest.raises(le.LauncherError, match="ADR 0143 d8"):
        le.ComposeLauncher().start(spec)


def test_kserve_launcher_refuses_unverified_adapters(monkeypatch):
    monkeypatch.setattr("examlops.supplychain.verify_before_load", lambda *a, **k: False)
    spec = le.EndpointSpec(model="M", hf_model_id="org/m", config=_cfg())
    with pytest.raises(le.LauncherError, match="verification refused"):
        le.KServeLauncher(kubectl=object()).start(spec)


# ── the real verifier, end to end: an adapter is a directory, signed the way `exa models sign
#    --path <dir>` signs it. A permissive stub cannot tell whether the bytes are actually hashed.


def _signed_adapter_dir(tmp_path, monkeypatch):
    from examlops import supplychain

    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)
    monkeypatch.delenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setattr(supplychain, "_load_private_key", lambda: None)
    adir = tmp_path / "sql-adapter"
    adir.mkdir()
    (adir / "adapter_config.json").write_text('{"r": 8}')
    (adir / "adapter_model.safetensors").write_bytes(b"\x00weights\x01")
    files = sorted(p for p in adir.rglob("*") if p.is_file())
    supplychain.sign_model("sql-lora", "3", files, root=adir)
    cfg = EngineConfig.from_dict(
        {"lora_adapters": [{"name": "sql", "path": str(adir), "model": "sql-lora", "version": "3"}]}
    )
    return adir, cfg


def test_a_signed_adapter_directory_verifies_with_the_real_gate(tmp_path, monkeypatch):
    _adir, cfg = _signed_adapter_dir(tmp_path, monkeypatch)
    res = lora.preflight(cfg, env={})
    assert res.ok and res.verified == ["sql"]


def test_a_tampered_adapter_directory_is_refused_by_the_real_gate(tmp_path, monkeypatch):
    adir, cfg = _signed_adapter_dir(tmp_path, monkeypatch)
    (adir / "adapter_model.safetensors").write_bytes(b"\x00backdoored\x01")
    with pytest.raises(lora.LoraPreflightError, match="verification refused"):
        lora.preflight(cfg, env={})


def test_an_adapter_whose_bytes_are_absent_is_refused_not_hashed_as_empty(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "examlops.supplychain.verify_before_load",
        lambda *a, **k: calls.append(a) or True,  # would pass, if it were ever asked
    )
    cfg = EngineConfig.from_dict(
        {
            "lora_adapters": [
                {"name": "x", "path": str(tmp_path / "nope"), "model": "x", "version": "1"}
            ]
        }
    )
    with pytest.raises(lora.LoraPreflightError, match="no adapter files"):
        lora.preflight(cfg, env={})
    assert calls == []  # an empty bundle is never handed to the signature check

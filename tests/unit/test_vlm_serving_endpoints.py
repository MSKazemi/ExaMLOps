"""Track V — launcher seam, endpoint registry and the `exa serve llm` CLI (ADR 0107).

GWT-V7 every launcher degrades when its substrate is absent · GWT-V8 the registry is
written and readable (the `llm_endpoints` table finally has a writer) · GWT-V9 dry-run,
confirm and audit on every mutation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import llm_endpoints as le  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data.serving import (  # noqa: E402
    get_llm_endpoint,
    list_llm_endpoints,
    upsert_llm_endpoint,
)
from examlops.platform_db import init_db  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("EXAMLOPS_ACTOR", "unit-test")
    monkeypatch.delenv("EXAMLOPS_VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("EXAMLOPS_LLM_LAUNCHER", raising=False)
    init_db()


# ── GWT-V7: launcher selection + degradation ──────────────────────────────────


def test_gwtv7_default_launcher_is_external():
    assert le.select_launcher().name == "external"


@pytest.mark.parametrize("name", ["external", "compose", "slurm", "flux", "kserve"])
def test_gwtv7_all_launchers_resolve(name):
    assert le.select_launcher(name).name in (name, "slurm", "flux")


def test_gwtv7_unknown_launcher_is_a_clear_error():
    with pytest.raises(le.LauncherError, match="unknown launcher"):
        le.select_launcher("kubernetes-ish")


def test_gwtv7_external_needs_a_url_and_says_so():
    with pytest.raises(le.LauncherError, match="needs an endpoint URL"):
        le.ExternalLauncher().start(le.EndpointSpec(model="m", hf_model_id="hf"))


def test_gwtv7_external_start_is_ready_immediately():
    handle = le.ExternalLauncher().start(
        le.EndpointSpec(model="m", hf_model_id="hf", base_url="http://gpu01:8000")
    )
    assert (handle.state, handle.base_url) == ("READY", "http://gpu01:8000")


def test_gwtv7_external_stop_deregisters_but_does_not_kill():
    # We do not own a process we did not start.
    result = le.ExternalLauncher().stop("m")
    assert result["stopped"] is False and result["deregistered"] is True


def test_gwtv7_compose_without_docker_degrades_clearly(monkeypatch):
    monkeypatch.setattr(le.shutil, "which", lambda _: None)
    with pytest.raises(le.LauncherUnavailable, match="docker is not on PATH"):
        le.ComposeLauncher().start(le.EndpointSpec(model="m", hf_model_id="hf"))


def test_gwtv7_env_selects_the_launcher(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_LLM_LAUNCHER", "compose")
    assert le.select_launcher().name == "compose"


def test_gwtv7_hpc_records_a_serve_job_not_a_train_job(tmp_path, monkeypatch):
    """`kind='serve'` keeps the terminal-state training poller from adopting a live server."""
    from examlops.platform_db import get_db

    spec = le.EndpointSpec(model="qwen", hf_model_id="hf", nodes=2, gpus=4, work_dir=str(tmp_path))
    le._record_serve_job("12345", "slurm", spec)
    with get_db() as conn:
        row = dict(conn.execute("SELECT * FROM hpc_jobs WHERE job_id='12345'").fetchone())
    assert row["kind"] == "serve"
    assert row["dataset"] == "-"  # NOT NULL column, meaningless for a server
    assert (row["nodes"], row["gpus"]) == (2, 4)


def test_gwtv7_hpc_resolve_endpoint_reads_the_file_the_job_writes(tmp_path):
    launcher = le.HpcLauncher(scheduler="slurm")
    spec = le.EndpointSpec(model="qwen", hf_model_id="hf", work_dir=str(tmp_path))
    assert launcher.resolve_endpoint(spec) is None
    path = launcher._endpoint_file(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("http://gpu-node-03:8000\n")
    assert launcher.resolve_endpoint(spec) == "http://gpu-node-03:8000"


def test_gwtv7_kserve_stays_dry_run_without_the_opt_in(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_KSERVE_LIVE_APPLY", raising=False)
    handle = le.KServeLauncher().start(le.EndpointSpec(model="qwen", hf_model_id="hf"))
    assert handle.detail["live_apply"] is False
    assert handle.state == "PENDING"


# ── GWT-V8: registry ──────────────────────────────────────────────────────────


def test_gwtv8_registry_roundtrip():
    upsert_llm_endpoint(
        "qwen-vl",
        hf_model_id="Qwen/Qwen3-VL-8B-Instruct",
        base_url="http://gpu01:8000",
        state="READY",
        launcher="external",
        modality="vision",
        project="research",
        engine_config={"engine": "vllm", "tensor_parallel_size": 4},
    )
    rec = get_llm_endpoint("qwen-vl")
    assert rec["state"] == "READY"
    assert rec["modality"] == "vision"
    assert rec["engine_config"]["tensor_parallel_size"] == 4  # JSON round-tripped


def test_gwtv8_filters():
    upsert_llm_endpoint("a", hf_model_id="x", project="p1", state="READY")
    upsert_llm_endpoint("b", hf_model_id="x", project="p2", state="STOPPED")
    assert [r["model"] for r in list_llm_endpoints(project="p1")] == ["a"]
    assert [r["model"] for r in list_llm_endpoints(state="STOPPED")] == ["b"]


@pytest.mark.skipif(
    os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres",
    reason="dashboard routers still connect by SQLite path, not through the storage seam",
)
def test_gwtv8_dashboard_reader_sees_what_the_cli_writes(tmp_path, monkeypatch):
    """The F10 LLMOps console read this table for a writer that never existed.

    Under `EXAMLOPS_DB_BACKEND=postgres` this test fails for a reason worth writing down: the
    dashboard reader takes a **file path**, so it reads an empty SQLite file while the CLI writes
    to Postgres — split state, no error. Porting the dashboard is a tracked step of the migration.
    """
    sys.path.insert(
        0, str(Path(__file__).parents[2] / "platform" / "services" / "dashboard" / "backend")
    )
    import llmops as dashboard_llmops

    upsert_llm_endpoint("qwen-vl", hf_model_id="Qwen/Qwen3-VL", state="READY", modality="vision")
    payload = dashboard_llmops.endpoints(str(tmp_path / "test.db"))
    assert payload["count"] >= 1
    assert any(r["model"] == "qwen-vl" for r in payload["rows"])


# ── GWT-V9: CLI safety ────────────────────────────────────────────────────────


def test_gwtv9_start_dry_run_changes_nothing():
    result = runner.invoke(
        app,
        ["serve", "llm", "start", "qwen-vl", "--base-url", "http://gpu01:8000", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert get_llm_endpoint("qwen-vl") is None  # nothing written


def test_gwtv9_start_registers_and_audits():
    from examlops.data.audit import export_audit_events

    result = runner.invoke(
        app,
        [
            "--yes",
            "serve",
            "llm",
            "start",
            "qwen-vl",
            "--base-url",
            "http://gpu01:8000",
            "--hf-model",
            "Qwen/Qwen3-VL-8B-Instruct",
            "--modality",
            "vision",
            "--max-images",
            "2",
            "--media-domains",
            "example.com",
        ],
    )
    assert result.exit_code == 0, result.output
    rec = get_llm_endpoint("qwen-vl")
    assert rec["state"] == "READY" and rec["modality"] == "vision"
    assert rec["engine_config"]["multimodal"]["limit_mm_per_prompt"] == {"image": 2}
    events = export_audit_events()
    assert any(e["action"] == "llm_endpoint_started" for e in events)


def test_gwtv9_vision_without_an_image_limit_is_refused():
    result = runner.invoke(
        app,
        [
            "--yes",
            "serve",
            "llm",
            "start",
            "v",
            "--base-url",
            "http://x:8000",
            "--modality",
            "vision",
        ],
    )
    assert result.exit_code == 1
    assert "--max-images" in result.output


def test_gwtv9_stop_dry_run_then_real_stop():
    upsert_llm_endpoint("qwen-vl", hf_model_id="hf", state="READY", launcher="external")
    dry = runner.invoke(app, ["serve", "llm", "stop", "qwen-vl", "--dry-run"])
    assert dry.exit_code == 0 and get_llm_endpoint("qwen-vl")["state"] == "READY"
    real = runner.invoke(app, ["--yes", "serve", "llm", "stop", "qwen-vl"])
    assert real.exit_code == 0, real.output
    assert get_llm_endpoint("qwen-vl")["state"] == "STOPPED"


def test_gwtv9_commands_on_an_unknown_endpoint_fail_with_a_hint():
    result = runner.invoke(app, ["serve", "llm", "status", "nope"])
    assert result.exit_code == 1
    assert "exa serve llm start" in result.output


def test_list_is_empty_and_helpful_before_anything_is_registered():
    result = runner.invoke(app, ["serve", "llm", "list"])
    assert result.exit_code == 0
    assert "No LLM endpoints" in result.output


def test_list_json_mode():
    upsert_llm_endpoint("qwen-vl", hf_model_id="hf", state="READY")
    result = runner.invoke(app, ["--json", "serve", "llm", "list"])
    assert result.exit_code == 0
    assert "qwen-vl" in result.output


def test_args_command_prints_the_rendered_argv():
    upsert_llm_endpoint(
        "qwen-vl",
        hf_model_id="Qwen/Qwen3-VL",
        engine_config={"engine": "vllm", "tensor_parallel_size": 4, "dtype": "bfloat16"},
    )
    result = runner.invoke(app, ["serve", "llm", "args", "qwen-vl"])
    assert result.exit_code == 0, result.output
    assert "--tensor-parallel-size" in result.output and "bfloat16" in result.output


def test_health_exits_nonzero_when_unreachable():
    """Non-zero exit makes `exa serve llm health` usable as a CI/deploy gate."""
    upsert_llm_endpoint("qwen-vl", hf_model_id="hf", base_url="http://127.0.0.1:1", state="READY")
    result = runner.invoke(app, ["serve", "llm", "health", "qwen-vl"])
    assert result.exit_code == 1
    assert get_llm_endpoint("qwen-vl")["state"] == "FAILED"  # state reconciled

"""Track V — launcher seam, endpoint registry and the `exa serve llm` CLI (ADR 0107).

GWT-V7 every launcher degrades when its substrate is absent · GWT-V8 the registry is
written and readable (the `llm_endpoints` table finally has a writer) · GWT-V9 dry-run,
confirm and audit on every mutation.
"""

from __future__ import annotations

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


class _FakeKubectl:
    """Duck-types `KubectlClient` (mirrors `test_substrate_seam.py`'s fake) with a live store,
    so `status()`/`stop()` after `apply()` see what was actually applied."""

    def __init__(self, *, apply_error: Exception | None = None):
        self._apply_error = apply_error
        self._store: dict[str, dict] = {}
        self.applied: list[dict] = []
        self.deleted: list[str] = []

    def apply(self, objects: list[dict]) -> list[str]:
        if self._apply_error is not None:
            raise self._apply_error
        refs = []
        for obj in objects:
            name = obj["metadata"]["name"]
            self._store[name] = obj
            self.applied.append(obj)
            refs.append(f"{obj['kind']}/{name}")
        return refs

    def get_any_kind(self, name: str) -> dict | None:
        return self._store.get(name)

    def delete_any_kind(self, name: str) -> None:
        self.deleted.append(name)
        self._store.pop(name, None)


def test_gwtv7_kserve_start_renders_and_really_applies(monkeypatch):
    # ADR 0107 clause 3 close-out: KServeLauncher.start used to always dry-run. It now performs
    # a genuine, plan-gated Server-Side Apply through the substrate seam (ADR 0142 d1/d6) — the
    # same real mechanism `tests/integration/test_kserve_live_apply_kind_live.py` verifies
    # against an actual cluster; this test verifies the launcher-level wiring with a fake.
    fake = _FakeKubectl()
    handle = le.KServeLauncher(kubectl=fake).start(
        le.EndpointSpec(model="qwen", hf_model_id="Qwen/Qwen2.5-7B")
    )
    assert handle.detail["applied"] is True
    assert fake.applied  # the substrate's real apply() ran, not a dry-run preview
    manifest = handle.detail["manifest"]
    assert manifest["apiVersion"] == "serving.kserve.io/v1alpha2"
    assert manifest["spec"]["model"]["uri"] == "hf://Qwen/Qwen2.5-7B"
    assert "predictor" not in manifest["spec"]
    # No controller is running against the fake, so the object carries no Ready condition yet —
    # honestly PENDING, not a fabricated READY.
    assert handle.state == "PENDING"


def test_gwtv7_kserve_apply_failure_raises_launcher_error_not_a_silent_pending(monkeypatch):
    fake = _FakeKubectl(apply_error=RuntimeError("connection refused"))
    with pytest.raises(le.LauncherError, match="connection refused"):
        le.KServeLauncher(kubectl=fake).start(
            le.EndpointSpec(model="qwen", hf_model_id="Qwen/Qwen2.5-7B")
        )
    assert fake.applied == []


def test_gwtv7_kserve_status_and_stop_go_through_the_same_live_object(monkeypatch):
    fake = _FakeKubectl()
    launcher = le.KServeLauncher(kubectl=fake)
    launcher.start(le.EndpointSpec(model="qwen", hf_model_id="Qwen/Qwen2.5-7B"))

    status = launcher.status("qwen")
    assert status["launcher"] == "kserve"
    assert status["state"] == "PENDING"

    result = launcher.stop("qwen")
    assert result == {"launcher": "kserve", "model": "qwen", "stopped": True}
    assert fake.deleted  # the real object name, not a raised "do it yourself" error

    # Stopped: the object is gone, status reports UNKNOWN rather than crashing.
    assert launcher.status("qwen")["state"] == "UNKNOWN"


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


def test_gwtv8_dashboard_reader_sees_what_the_cli_writes(tmp_path, monkeypatch):
    """The F10 LLMOps console read this table for a writer that never existed.

    It also guards the dashboard's half of the datastore seam. The reader is handed a SQLite
    *path* that does not exist; under `EXAMLOPS_DB_BACKEND=postgres` it must still return what
    the CLI just wrote, because `dbconn.connect()` ignores the path and opens the configured
    engine. Before that, the two halves silently used different stores — the console showed
    empty state and nothing anywhere raised.
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

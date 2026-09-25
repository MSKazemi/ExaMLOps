"""ADR 0116 verification 1 — one ``JobRequest`` round-trips through mock, Slurm and Flux unchanged.

The round trip goes through the **real adapter code**: the native half is handed to
``submit_job`` of the actual ``MockSlurmAdapter`` / ``RealSlurmAdapter`` / ``FluxAdapter`` (with a
recording executor instead of a cluster), the argv the adapter built is parsed back, and the request
is rebuilt from that plus the envelope. A field an adapter drops or alters fails the comparison —
which is how the tests below also pin *what each backend does not enforce*.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from examlops.admission_seam import JobRequest, Resources
from examlops.admission_seam import translate as tr

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

from adapter import RealSlurmAdapter  # noqa: E402
from executor import CompletedCommand  # noqa: E402
from flux_adapter import FluxAdapter  # noqa: E402
from mock_slurm_adapter import MockSlurmAdapter  # noqa: E402


class RecordingExecutor:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def run(self, cmd, *, timeout=None, cwd=None):
        self.calls.append(list(cmd))
        if cmd[0] in ("sbatch", "flux"):
            return CompletedCommand(0, self.stdout, "")
        return CompletedCommand(0, "", "")

    def put(self, local, remote):
        pass

    def get(self, remote, local):
        pass

    def close(self):
        pass


#: One fixture, every neutral field set to something non-default.
FIXTURE = JobRequest(
    project="climate",
    tenant="team-a",
    workload_class="training",
    resources=Resources(gpus=8, cpus=16, memory_gb=64.25, nodes=2),
    gang=True,
    network_tier="scale_up",
    scale_up_domain="required",
    queue="gpu",
    priority_class="high",
    deadline="2026-12-01T00:00:00+00:00",
    flexibility_s=1800.0,
    est_runtime_s=5400.4,
)


def _script(tmp: Path) -> str:
    p = tmp / "run.sh"
    p.write_text("#!/bin/bash\ntrue\n")
    return str(p)


def _through_slurm(req: JobRequest, tmp: Path) -> tuple[dict, list[str]]:
    ex = RecordingExecutor("Submitted batch job 4242")
    a = RealSlurmAdapter(executor=ex, working_dir=str(tmp / "wd"))
    job = a.submit_job(script_path=_script(tmp), resources=tr.to_native(req, "slurm"))
    assert job == "4242"
    argv = next(c for c in ex.calls if c[0] == "sbatch")
    return tr.parse_argv("slurm", argv), argv


def _through_flux(req: JobRequest, tmp: Path) -> tuple[dict, list[str]]:
    ex = RecordingExecutor("fAbCd\n")
    a = FluxAdapter(executor=ex, working_dir=str(tmp / "wd"), remote_workdir="/r")
    a.submit_job(script_path=_script(tmp), resources=tr.to_native(req, "flux"))
    argv = next(c for c in ex.calls if c[:2] == ["flux", "batch"])
    return tr.parse_argv("flux", argv), argv


def _through_mock(req: JobRequest, tmp: Path) -> dict:
    a = MockSlurmAdapter(working_dir=str(tmp / "wd"))
    job = a.submit_job(script_path=_script(tmp), resources=tr.to_native(req, "mock"))
    return dict(a._jobs[job]["resources"])


@pytest.mark.parametrize("backend", tr.BACKENDS)
def test_the_same_request_round_trips_through_every_adapter_unchanged(backend):
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        env = tr.envelope(FIXTURE)
        if backend == "slurm":
            native, _ = _through_slurm(FIXTURE, tmp)
        elif backend == "flux":
            native, _ = _through_flux(FIXTURE, tmp)
        else:
            native = _through_mock(FIXTURE, tmp)
        assert tr.from_native(native, env, backend) == FIXTURE


def test_slurm_argv_carries_what_slurm_enforces():
    with tempfile.TemporaryDirectory() as d:
        _, argv = _through_slurm(FIXTURE, Path(d))
    assert "--gpus=8" in argv and "--nodes=2" in argv and "--cpus-per-task=16" in argv
    assert "--mem=65792M" in argv  # ceil(64.25 * 1024)
    assert "--partition=gpu" in argv
    assert "--time=1:30:01" in argv, "the wall-time limit is rounded up, never down"


def test_flux_argv_carries_what_flux_enforces_and_no_memory():
    with tempfile.TemporaryDirectory() as d:
        native, argv = _through_flux(FIXTURE, Path(d))
    # 8 GPUs over 2 nodes: Flux's -g is GPUs per slot (one slot per node under -N without -n),
    # so the argv must say 4. "-g8" would allocate 16 GPUs for an 8-GPU request.
    assert "-g4" in argv and "-N2" in argv and "-c16" in argv and "--queue=gpu" in argv
    assert not any(a == "-g8" for a in argv)
    assert "-t5401s" in argv
    assert "mem" not in native
    assert "resources.memory_gb" in tr.not_native("flux")
    assert "resources.memory_gb" not in tr.not_native("slurm")


def test_an_adapter_that_drops_a_field_is_caught():
    """If the argv lost ``--gpus`` the job the scheduler sees is not the request."""
    with tempfile.TemporaryDirectory() as d:
        native, _ = _through_slurm(FIXTURE, Path(d))
    native.pop("gpus")
    with pytest.raises(tr.TranslationMismatch, match="gpus"):
        tr.from_native(native, tr.envelope(FIXTURE), "slurm")


def test_an_edited_native_value_is_caught():
    native = tr.to_native(FIXTURE, "slurm") | {"nodes": 3}
    with pytest.raises(tr.TranslationMismatch, match="nodes"):
        tr.from_native(native, tr.envelope(FIXTURE), "slurm")


def test_a_minimal_request_emits_no_zero_flags():
    req = JobRequest(project="p")
    native = tr.to_native(req, "slurm")
    assert native == {"nodes": 1}, "zero GPUs/CPUs are omitted, not written as =0"
    with tempfile.TemporaryDirectory() as d:
        back, argv = _through_slurm(req, Path(d))
    assert not any(a.startswith("--gpus") for a in argv)
    assert tr.from_native(back, tr.envelope(req), "slurm") == req


def test_envelope_is_stable_and_strict():
    a = tr.envelope(FIXTURE)
    assert a == tr.envelope(JobRequest.from_dict(json.loads(a)))
    tampered = json.loads(a) | {"surprise": 1}
    with pytest.raises(ValueError, match="unknown field"):
        tr.from_native({}, json.dumps(tampered), "mock")
    with pytest.raises(tr.TranslationError, match="not JSON"):
        tr.from_native({}, "{nope", "mock")


def test_unknown_backend_is_refused():
    with pytest.raises(tr.TranslationError, match="unknown backend"):
        tr.to_native(FIXTURE, "kueue")
    with pytest.raises(tr.TranslationError):
        tr.parse_argv("mock", [])


@pytest.mark.parametrize(
    "value,seconds", [("1:30:01", 5401), ("30:00", 1800), ("90s", 90), ("15", 900)]
)
def test_wall_time_parsing(value, seconds):
    assert tr._seconds(value) == seconds


def test_cli_translate(tmp_path):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    f = tmp_path / "req.json"
    f.write_text(tr.envelope(FIXTURE))
    res = CliRunner().invoke(app, ["--json", "admission", "translate", "--request", str(f)])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["backend"] == "slurm" and out["native"]["gpus"] == 8
    bad = CliRunner().invoke(
        app, ["admission", "translate", "--request", str(f), "--backend", "nope"]
    )
    assert bad.exit_code == 1


def test_flux_allocates_the_requested_gpu_total_not_total_times_nodes():
    """The GPUs Flux allocates (per-slot x slots) equal the request, for every node count."""
    for nodes, gpus in ((1, 4), (2, 8), (4, 8)):
        req = JobRequest(project="p", resources=Resources(gpus=gpus, nodes=nodes))
        with tempfile.TemporaryDirectory() as d:
            native, _ = _through_flux(req, Path(d))
        assert int(native["gpus"]) * int(native["nodes"]) == gpus


def test_flux_refuses_gpus_that_do_not_divide_over_the_nodes():
    req = JobRequest(project="p", resources=Resources(gpus=3, nodes=2))
    with pytest.raises(tr.TranslationError, match="evenly"):
        tr.to_native(req, "flux")

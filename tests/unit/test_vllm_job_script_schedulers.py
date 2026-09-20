# tests/unit/test_vllm_job_script_schedulers.py
"""ADR 0107 clause 3 — the vLLM server job has to start under Flux, not only under Slurm.

The recorded finding: the job script "calls `srun`/`scontrol`, so `--launcher flux` submits a job
whose server never starts". Under `flux batch` neither exists, so even a single-node job died on
its last line — and Flux is the scheduler this platform's own cluster runs.

These **execute** the rendered script. `flux`, `srun`, `scontrol`, `apptainer` and `sleep` are
logging shims on PATH, so each test sees exactly which scheduler commands the script issued, in
order, without a cluster or a GPU.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.llm_endpoints import EndpointSpec, HpcLauncher  # noqa: E402

_SHIMS = {
    # flux: `hostlist` answers with the fake allocation; everything else is logged only.
    "flux": 'if [ "$1" = hostlist ]; then echo "${FAKE_HOSTS}"; exit 0; fi',
    "scontrol": 'if [ "$1" = show ]; then printf "%s\\n" ${FAKE_HOSTS}; exit 0; fi',
    "srun": "",
    "apptainer": 'if [ "$1" = pull ]; then : > "$2"; fi',
    "sleep": "",
}


@pytest.fixture
def cluster(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name, body in _SHIMS.items():
        shim = bin_dir / name
        shim.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "{name} $*" >> "$SHIM_LOG"\n{body}\nexit 0\n'
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return bin_dir, log


def _run(tmp_path, cluster, *, nodes=1, gpus=4, env=None):
    bin_dir, log = cluster
    spec = EndpointSpec(
        model="qwen", hf_model_id="Qwen/Qwen3-8B", nodes=nodes, gpus=gpus, work_dir=str(tmp_path)
    )
    script = tmp_path / "run.sh"
    script.write_text(HpcLauncher(scheduler="flux").render_script(spec))
    base = {k: v for k, v in os.environ.items() if not k.startswith(("SLURM_", "FLUX_"))}
    proc = subprocess.run(
        ["bash", str(script)],
        env={
            **base,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SHIM_LOG": str(log),
            **(env or {}),
        },  # fmt: skip
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _flux_env(hosts="nodeA"):
    return {"FLUX_URI": "local:///run/flux/job", "FAKE_HOSTS": hosts}


# ── Flux ─────────────────────────────────────────────────────────────────────


def test_a_single_node_flux_job_starts_the_server(tmp_path, cluster):
    proc, calls = _run(tmp_path, cluster, env=_flux_env())

    assert proc.returncode == 0, proc.stderr
    (serve,) = [shlex.split(c) for c in calls if "vllm serve" in c]
    assert serve[:6] == [
        "flux",
        "run",
        "--nodes=1",
        "--ntasks=1",
        "--gpus-per-task=4",
        "--requires=host:nodeA",
    ]
    assert serve[6:9] == ["apptainer", "exec", "--nv"]
    assert serve[10:13] == ["vllm", "serve", "Qwen/Qwen3-8B"]
    assert not [c for c in calls if c.startswith(("srun", "scontrol"))], "no Slurm under Flux"
    assert "scheduler=flux" in proc.stdout


def test_an_unresolvable_head_name_falls_back_to_the_nodes_own_address(tmp_path, cluster):
    """`getent` exits 2 for a name it cannot resolve; under `pipefail` that ended the job before
    the fallback line ever ran."""
    proc, _ = _run(tmp_path, cluster, env=_flux_env("no-such-host.invalid"))

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "qwen.endpoint").read_text().startswith("http://")


def test_a_multi_node_flux_job_places_ray_and_the_server_by_host(tmp_path, cluster):
    proc, calls = _run(tmp_path, cluster, nodes=3, env=_flux_env("nodeA nodeB nodeC"))

    assert proc.returncode == 0, proc.stderr
    steps = [shlex.split(c) for c in calls if c.startswith("flux run")]
    placed = [(next(a for a in s if a.startswith("--requires=")), s[-1]) for s in steps]
    assert ("--requires=host:nodeA", "--block") in placed  # the Ray head
    assert ("--requires=host:nodeB", "--block") in placed and (
        "--requires=host:nodeC",
        "--block",
    ) in placed
    assert [c for c in calls if "vllm serve" in c][0].count("--requires=host:nodeA") == 1
    assert not [c for c in calls if c.startswith(("srun", "scontrol"))]


def test_a_gpu_less_flux_step_asks_for_no_gpus(tmp_path, cluster):
    """`--gpus-per-task=0` is not a request Flux needs; and an empty argument list must not trip
    `set -u` (bash < 4.4 aborts on an empty array expansion)."""
    proc, calls = _run(tmp_path, cluster, gpus=0, env=_flux_env())

    assert proc.returncode == 0, proc.stderr
    serve = next(c for c in calls if "vllm serve" in c)
    assert "--gpus-per-task" not in serve and "--requires=host:nodeA" in serve


# ── Slurm is unchanged ───────────────────────────────────────────────────────


def test_slurm_issues_the_same_commands_as_before(tmp_path, cluster):
    proc, calls = _run(
        tmp_path,
        cluster,
        nodes=2,
        env={"SLURM_JOB_NODELIST": "node[1-2]", "FAKE_HOSTS": "gpu-a gpu-b"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "scontrol show hostnames node[1-2]" in calls
    srun = [c for c in calls if c.startswith("srun")]
    assert srun[0].startswith("srun --nodes=1 --ntasks=1 -w gpu-a apptainer exec --nv")
    assert "ray start --head" in srun[0]
    assert (
        srun[1].startswith("srun --nodes=1 --ntasks=1 -w gpu-b ")
        and "ray start --address=" in srun[1]
    )
    assert srun[-1].startswith("srun --nodes=1 --ntasks=1 -w gpu-a ") and "vllm serve" in srun[-1]
    assert not [c for c in calls if c.startswith("flux")]


# ── no scheduler ─────────────────────────────────────────────────────────────


def test_without_a_scheduler_the_server_runs_here(tmp_path, cluster):
    proc, calls = _run(tmp_path, cluster, env={"FAKE_HOSTS": ""})

    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("apptainer exec --nv") and "vllm serve" in c for c in calls)
    assert not [c for c in calls if c.startswith(("srun", "scontrol", "flux"))]


def test_the_endpoint_is_published_under_flux(tmp_path, cluster):
    proc, _ = _run(tmp_path, cluster, env=_flux_env())

    endpoint = tmp_path / "qwen.endpoint"
    assert proc.returncode == 0 and endpoint.read_text().startswith("http://")


# ── a long hostlist must not end the job ─────────────────────────────────────


def test_a_hostlist_longer_than_a_pipe_buffer_does_not_end_the_job(tmp_path, cluster):
    """`job_hosts | head -n1` under `set -o pipefail`: `head` exits after the first line, the
    host lister is killed by SIGPIPE writing the rest, and `set -e` ends the job with 141 —
    intermittently for two hosts, always once the list outgrows the 64 KiB pipe buffer. 8000 names
    (~112 KB) make it deterministic and stay under the kernel's 128 KiB limit for one variable."""
    hosts = " ".join(f"gpu-node-{i:04d}" for i in range(8000))
    assert 65536 < len(hosts) < 131072
    proc, calls = _run(
        tmp_path,
        cluster,
        nodes=1,
        env={"SLURM_JOB_NODELIST": "gpu-node-[0000-7999]", "FAKE_HOSTS": hosts},
    )

    assert proc.returncode == 0, (proc.returncode, proc.stderr[-400:])
    assert "(head=gpu-node-0000)" in proc.stdout
    assert any("vllm serve" in c for c in calls)


# ── BL-097: a malicious model id cannot break out of the generated script ───


def test_a_malicious_hf_model_id_cannot_inject_a_command(tmp_path, cluster):
    """Every placeholder used to land inside the template's OWN double quotes
    (``MODEL="@@MODEL@@"``), so a value containing a bare ``"`` closed the assignment early and
    anything after it ran as a second command — and even without breaking the quoting, bash still
    expands ``$(...)``/backticks inside double quotes. The renderer now shlex.quote()s every
    value and the template carries no quotes of its own, so this must execute as one inert
    string, never as shell syntax."""
    bin_dir, log = cluster
    marker = tmp_path / "pwned"
    payload = f'Qwen/Qwen3-8B"; touch {marker}; echo "$(touch {marker})`touch {marker}`'
    spec = EndpointSpec(model="qwen", hf_model_id=payload, nodes=1, gpus=0, work_dir=str(tmp_path))
    script = tmp_path / "run.sh"
    script.write_text(HpcLauncher(scheduler="flux").render_script(spec))
    base = {k: v for k, v in os.environ.items() if not k.startswith(("SLURM_", "FLUX_"))}
    proc = subprocess.run(
        ["bash", str(script)],
        env={
            **base,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SHIM_LOG": str(log),
            **_flux_env(),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), "the injected touch executed — quoting regressed"
    (serve,) = [c for c in log.read_text().splitlines() if "vllm serve" in c]
    assert payload in serve, "the payload must still reach vllm serve, just as inert text"

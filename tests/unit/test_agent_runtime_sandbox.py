"""The SandboxProvider seam (ADR 0145 decision 6, verifications 2, 3 and 6).

Providers shell out through an injected runner; the fake here plays the daemon faithfully
(``docker info`` answers with the runtimes it has, ``docker run`` returns an id, ``exec``
returns what a real container would), so the real argument construction is what is tested.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from examlops.agent_runtime import (
    AgentSandboxK8sProvider,
    ApptainerProvider,
    DockerProvider,
    Node,
    NoSandbox,
    SandboxRefused,
    select_provider,
)
from examlops.agent_versions.manifest import version_id_of
from tests.unit._agent_runtime_fixtures import linear_program, make_runtime, manifest, snapshot


class FakeDocker:
    def __init__(self, runtimes: dict | None = None) -> None:
        self.runtimes = runtimes if runtimes is not None else {"runc": {}}
        self.argv: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.argv.append(list(argv))
        sub = argv[1]
        if sub == "info":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.runtimes), "")
        if sub == "run":
            return subprocess.CompletedProcess(argv, 0, "c0ffee\n", "")
        if sub == "exec":
            cmd = argv[-1]
            if "curl" in cmd:  # no network namespace route: egress fails
                return subprocess.CompletedProcess(argv, 6, "", "Could not resolve host")
            return subprocess.CompletedProcess(argv, 0, f"ran:{cmd}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _run_argv(fake):
    return next(a for a in fake.argv if a[1] == "run")


def test_docker_without_runsc_advertises_container_weak_and_a_gvisor_policy_refuses_it():
    fake = FakeDocker({"runc": {}})
    p = DockerProvider(runner=fake)
    assert p.capabilities().isolation == "container-weak"
    with pytest.raises(SandboxRefused) as exc:
        select_provider([p], required="gvisor")
    assert exc.value.code == "isolation_insufficient"
    assert "container-weak" in exc.value.reason and "compose" in exc.value.reason
    assert select_provider([p], required="container-weak") is p


def test_docker_with_runsc_advertises_gvisor_and_runs_under_it():
    fake = FakeDocker({"runc": {}, "runsc": {"path": "/usr/bin/runsc"}})
    p = DockerProvider(runner=fake)
    assert select_provider([p], required="gvisor") is p
    h = p.claim("th-1", "python-3.12-min")
    argv = _run_argv(fake)
    assert "--runtime=runsc" in argv
    assert argv[argv.index("--network") + 1] == "none"  # default-deny egress
    assert "--cap-drop" in argv and "--read-only" in argv
    assert h.ref == "c0ffee" and h.isolation == "gvisor"


def test_sandbox_egress_is_denied_and_an_unenforceable_allow_list_is_refused(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SANDBOX_EGRESS_PROXY", raising=False)
    fake = FakeDocker()
    p = DockerProvider(runner=fake)
    h = p.claim("th-1", "python-3.12-min")
    out = p.exec(h, "curl https://exfil.example.com")
    assert out["ok"] is False and out["exit_code"] != 0  # verification 2: egress fails
    with pytest.raises(SandboxRefused) as exc:
        p.claim("th-2", "python-3.12-min", egress=["docs.example.eu"])
    assert exc.value.code == "egress_unenforceable"
    monkeypatch.setenv("EXAMLOPS_SANDBOX_EGRESS_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("EXAMLOPS_SANDBOX_PROXY_NETWORK", "sandbox-egress")
    p.claim("th-3", "python-3.12-min", egress=["docs.example.eu"])
    argv = [a for a in fake.argv if a[1] == "run"][-1]
    assert argv[argv.index("--network") + 1] == "sandbox-egress"
    assert "HTTPS_PROXY=http://egress-proxy:3128" in argv


def test_no_platform_credential_reaches_the_sandbox(monkeypatch):
    """Verification 3 (red-green against a seeded canary): nothing from the runtime's
    environment is passed to the container."""
    canary = "canary-secret-7f3a9c"
    monkeypatch.setenv("EXAMLOPS_TOOL_TOKEN", canary)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", canary)
    fake = FakeDocker()
    p = DockerProvider(runner=fake)
    h = p.claim("th-1", "python-3.12-min")
    p.exec(h, "env")
    flat = json.dumps(fake.argv)
    assert canary not in flat
    assert "--env-file" not in flat and "EXAMLOPS_TOOL_TOKEN" not in flat


def test_output_is_capped_and_a_timeout_is_a_refusal():
    def big(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "x" * 200_000, "")

    p = ApptainerProvider(runner=big, workdir_root="/tmp/examlops-sbx-test")
    h = p.claim("th-1", "python-3.12-min")
    out = p.exec(h, "yes")
    assert out["truncated"] and len(out["stdout"]) == 64 * 1024

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    p2 = ApptainerProvider(runner=slow, workdir_root="/tmp/examlops-sbx-test")
    with pytest.raises(SandboxRefused) as exc:
        p2.exec(p2.claim("th-2", "python-3.12-min"), "sleep 999", timeout=1)
    assert exc.value.code == "sandbox_timeout"


def test_apptainer_runs_fully_contained_with_no_network(tmp_path):
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    p = ApptainerProvider(runner=runner, workdir_root=str(tmp_path))
    assert p.capabilities().isolation == "container"
    h = p.claim("th-1", "python-3.12-min")
    p.exec(h, "python -c 'print(1)'")
    argv = seen[0]
    for flag in ("--containall", "--cleanenv", "--net"):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == "none"
    p.release(h, "destroy")
    assert not (tmp_path / "th-1").exists()
    with pytest.raises(SandboxRefused):
        p.claim("th-2", "python-3.12-min", egress=["x.example"])


def test_k8s_agent_sandbox_claim_is_v1beta1_gvisor_and_kata_for_gpu():
    applied = []

    def runner(argv, **kw):
        if argv[1] == "apply":
            applied.append(json.loads(kw["input"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    p = AgentSandboxK8sProvider(runner=runner, namespace="agents", gpu_templates=["cuda-12"])
    h = p.claim("th-1", "python-3.12-min")
    doc = applied[0]
    assert doc["apiVersion"] == "agents.x-k8s.io/v1beta1" and doc["kind"] == "SandboxClaim"
    assert doc["spec"]["runtimeClassName"] == "gvisor" and h.isolation == "gvisor"
    assert doc["spec"]["networkPolicy"] == {"egress": "deny"}
    g = p.claim("th-2", "cuda-12")
    assert applied[1]["spec"]["runtimeClassName"] == "kata" and g.isolation == "vm"


def test_select_prefers_the_strongest_and_no_sandbox_refuses():
    weak = DockerProvider(runner=FakeDocker({"runc": {}}))
    k8s = AgentSandboxK8sProvider(runner=lambda a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert select_provider([weak, k8s]) is k8s
    with pytest.raises(SandboxRefused):
        NoSandbox().claim("th-1", "python-3.12-min")
    with pytest.raises(SandboxRefused):
        select_provider([], required="container")
    with pytest.raises(SandboxRefused) as bad:
        p = DockerProvider(runner=FakeDocker())
        p.claim("../../etc", "python-3.12-min")
    assert bad.value.code == "bad_session"


def test_an_unknown_template_is_refused(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SANDBOX_TEMPLATES", json.dumps({"tiny": "alpine:3.20"}))
    p = DockerProvider(runner=FakeDocker())
    with pytest.raises(SandboxRefused) as exc:
        p.claim("th-1", "python-3.12-min")
    assert exc.value.code == "unknown_template"


# -- wired into the runtime: a tenant policy refuses a weak substrate (verification 6) -----------


def _code_agent():
    def run_code(state, ctx):
        return {"output": ctx.sandbox_exec("python -c 'print(42)'")}

    return linear_program(Node("run", run_code))


def test_the_runtime_refuses_code_execution_for_a_gvisor_tenant_on_container_weak(tmp_path):
    m = manifest(sandbox={"template": "python-3.12-min"})
    snap = snapshot(
        [m],
        quotas={
            "default": {"max_sessions": 10},
            "tenants": {"bank": {"sandbox_isolation": "gvisor"}},
        },
    )
    weak = DockerProvider(runner=FakeDocker({"runc": {}}))
    rt = make_runtime(
        tmp_path, snap, {"tests.jobdoc:program": _code_agent()}, sandbox_providers=[weak]
    )
    t = rt.open_session("jobdoc", tenant="bank")
    run = rt.run_wait(t["thread_id"], "go", tenant="bank")
    assert run["output"]["code"] == "isolation_insufficient"
    assert rt.store.events(thread_id=t["thread_id"], kind="sandbox_refused")

    # A tenant with no isolation requirement runs - and the sandbox is released on suspend.
    t2 = rt.open_session("jobdoc", tenant="lab")
    run2 = rt.run_wait(t2["thread_id"], "go", tenant="lab")
    assert run2["output"]["ok"] is True and "ran:" in run2["output"]["stdout"]
    rt.close_session(t2["thread_id"], tenant="lab")
    assert any(a[1:3] == ["rm", "-f"] for a in weak.runner.argv)


def test_the_manifest_isolation_requirement_is_honoured(tmp_path):
    m = manifest(sandbox={"template": "python-3.12-min", "isolation": "vm"})
    rt = make_runtime(
        tmp_path,
        snapshot([m]),
        {"tests.jobdoc:program": _code_agent()},
        sandbox_providers=[DockerProvider(runner=FakeDocker({"runsc": {}}))],
    )
    t = rt.open_session("jobdoc", tenant="lab")
    assert (
        rt.run_wait(t["thread_id"], "go", tenant="lab")["output"]["code"]
        == "isolation_insufficient"
    )
    assert version_id_of(m)

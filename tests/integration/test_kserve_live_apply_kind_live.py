"""The KServe substrate's real, live apply against a real cluster (ADR 0142 d6).

A throwaway kind cluster gets only the pinned v0.20.0 CRDs (``InferenceService`` and
``LLMInferenceService``) — not the KServe controller itself, which needs cert-manager, a
container registry reachable from the cluster, and considerably more setup than this test's job
warrants. Without a controller nothing reconciles into ``Ready``, so this proves exactly what
:mod:`examlops.serving.substrates.kubectl_client` is responsible for and nothing it is not:
real Server-Side Apply against a real API server, a real read-back, a real delete, and a real
rejection from the server (an unknown field) surfacing as :class:`ApplyFailed` with an audit row —
not the controller's reconciliation, which is a different, larger piece of infrastructure.

Why Server-Side Apply specifically, confirmed empirically while writing this test: **client-side**
``kubectl apply`` on the real ``InferenceService`` CRD fails outright —
``metadata.annotations: Too long: may not be more than 262144 bytes`` — because client-side apply
stores the whole previous config in a ``last-applied-configuration`` annotation, and this CRD's
embedded OpenAPI schema is larger than that limit. Server-side apply has no such annotation; it is
not just the modern recommendation (stable since Kubernetes 1.22), it is the only one that works
for this CRD at all.

Opt-in and slow (a kind cluster, two CRD downloads)::

    EXAMLOPS_KIND_KSERVE_LIVE=1 .venv/bin/pytest \\
        tests/integration/test_kserve_live_apply_kind_live.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "cli" / "src"))

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_KIND_KSERVE_LIVE") != "1", reason="set EXAMLOPS_KIND_KSERVE_LIVE=1"
    ),
]

NODE = "kindest/node:v1.32.2"
KSERVE_VERSION = "v0.20.0"  # the pin k8s_schema.py validates against
CRD_URL = f"https://github.com/kserve/kserve/releases/download/{KSERVE_VERSION}/kserve-crds.yaml"
LLMISVC_CRD_URL = (
    f"https://github.com/kserve/kserve/releases/download/{KSERVE_VERSION}/"
    f"helm-chart-kserve-llmisvc-crd-{KSERVE_VERSION}.tgz"
)
NAMESPACE = "examlops"


def _run(*args: str, input: bytes | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input, capture_output=True, timeout=timeout)


@pytest.fixture(scope="module")
def kind_kubeconfig(tmp_path_factory):
    if not shutil.which("kind") or not shutil.which("kubectl"):
        pytest.skip("kind and/or kubectl not on PATH")
    name = f"exa-kserve-{uuid.uuid4().hex[:6]}"
    kubeconfig = tmp_path_factory.mktemp("kserve-kind") / "kubeconfig"

    created = _run("kind", "create", "cluster", "--name", name, "--image", NODE, "--wait", "90s")
    if created.returncode != 0:
        pytest.fail(f"kind create cluster failed: {created.stderr.decode()[:500]}")
    try:
        got = _run("kind", "get", "kubeconfig", "--name", name)
        kubeconfig.write_bytes(got.stdout)
        env = {**os.environ, "KUBECONFIG": str(kubeconfig)}

        subprocess.run(
            ["kubectl", "create", "namespace", NAMESPACE], env=env, capture_output=True, timeout=30
        )

        crds = tmp_path_factory.mktemp("kserve-crds") / "kserve-crds.yaml"
        _download(CRD_URL, crds)
        applied = subprocess.run(
            ["kubectl", "apply", "--server-side", "-f", str(crds)],
            env=env,
            capture_output=True,
            timeout=60,
        )
        if applied.returncode != 0:
            pytest.fail(f"installing KServe CRDs failed: {applied.stderr.decode()[:500]}")

        llm_tgz = tmp_path_factory.mktemp("llmisvc") / "llmisvc-crd.tgz"
        _download(LLMISVC_CRD_URL, llm_tgz)
        extract_dir = tmp_path_factory.mktemp("llmisvc-extracted")
        subprocess.run(["tar", "xzf", str(llm_tgz), "-C", str(extract_dir)], check=True, timeout=30)
        llm_crd = next(extract_dir.glob("**/serving.kserve.io_llminferenceservices.yaml"))
        applied_llm = subprocess.run(
            ["kubectl", "apply", "--server-side", "-f", str(llm_crd)],
            env=env,
            capture_output=True,
            timeout=60,
        )
        if applied_llm.returncode != 0:
            pytest.fail(
                f"installing LLMInferenceService CRD failed: {applied_llm.stderr.decode()[:500]}"
            )

        # A freshly-registered CRD's API is not immediately servable; wait for it.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ok = subprocess.run(
                ["kubectl", "get", "inferenceservices.serving.kserve.io", "-n", NAMESPACE],
                env=env,
                capture_output=True,
                timeout=10,
            )
            if ok.returncode == 0:
                break
            time.sleep(1)

        yield str(kubeconfig)
    finally:
        _run("kind", "delete", "cluster", "--name", name)


def _download(url: str, dest: Path) -> None:
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - a pinned, known GitHub asset
        dest.write_bytes(resp.read())


@pytest.fixture()
def kserve(kind_kubeconfig, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", kind_kubeconfig)
    monkeypatch.setenv("EXAMLOPS_KSERVE_NAMESPACE", NAMESPACE)
    from examlops.serving.substrates import registry

    return registry.get("kserve")


def _pred_spec_ref(name: str):
    from examlops.serving.substrates.resolve import ResolvedRef

    spec = {"kind": "predictive", "name": name, "framework": "sklearn"}
    ref = ResolvedRef(name, "1", "Production", "s3://bucket/model", "sha256:" + "a" * 64, "default")
    return spec, ref


def test_a_real_apply_is_visible_to_a_real_get_and_a_real_delete(kserve):
    name = f"pred-{uuid.uuid4().hex[:6]}"
    spec, ref = _pred_spec_ref(name)
    rendered = kserve.render(spec, ref)

    result = kserve.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert result.applied == (f"InferenceService/{name}",)

    status = kserve.status(name)
    assert status.state == "PENDING"  # no controller installed — reconciliation is out of scope

    kserve.stop(name)
    assert kserve.status(name).state == "UNKNOWN"


def test_a_real_apply_writes_the_audit_row_the_plan_gate_promises(kserve, tmp_path, monkeypatch):
    from examlops.data import get_db
    from examlops.platform_db import init_db

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "audit.db"))
    init_db()
    name = f"pred-{uuid.uuid4().hex[:6]}"
    spec, ref = _pred_spec_ref(name)
    rendered = kserve.render(spec, ref)

    kserve.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT details FROM audit_events WHERE action = 'substrate_apply' AND target = ?",
            (f"{name}@1",),
        ).fetchall()
    assert len(rows) == 1
    assert '"result": "applied"' in rows[0]["details"]
    kserve.stop(name)


def test_a_real_rejection_from_the_api_server_is_an_apply_failed_not_a_crash(kserve):
    from examlops.serving.substrates.base import ApplyFailed, Rendered, content_hash

    name = f"bad-{uuid.uuid4().hex[:6]}"
    spec, ref = _pred_spec_ref(name)
    rendered = kserve.render(spec, ref)
    bad_obj = {**rendered.objects[0], "spec": {"this_field_does_not_exist_in_the_crd": True}}
    bad_rendered = Rendered("kserve", (bad_obj,), content_hash([bad_obj]))

    with pytest.raises(ApplyFailed, match="server"):
        kserve.apply(bad_rendered, dry_run=False, plan_hash=bad_rendered.content_hash)

    assert kserve.status(name).state == "UNKNOWN"  # never actually created

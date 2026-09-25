# tests/unit/test_kserve_gitops_delivery.py
"""ADR 0142 d6 / spec-usar-1 R-SUB-25 (and ADR 0015 d4's GitOps path): the ``kserve`` substrate can
deliver a plan by writing a GitOps tree an Argo CD / Flux reconciler consumes, instead of a
server-side apply — plan-gated and audited exactly like every other apply, sending nothing to an
API server.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.platform_db import init_db  # noqa: E402
from examlops.serving.substrates import registry  # noqa: E402
from examlops.serving.substrates.base import PlanMismatch, SubstrateUnavailable  # noqa: E402
from examlops.serving.substrates.gitops import GitOpsError, GitOpsWriter  # noqa: E402
from examlops.serving.substrates.resolve import ResolvedRef  # noqa: E402
from examlops.serving.substrates.verifier import VerifierSpec  # noqa: E402

PRED = {"name": "JPCP", "framework": "sklearn"}
GEN = {"name": "chat", "task_type": "text_generation", "engine": {"engine": "vllm"}}
PRED_REF = ResolvedRef("jpcp", "17", "Production", "s3://b/1/m/artifacts", "unsigned", "research")
GEN_REF = ResolvedRef("chat", "3", "Production", "hf://Qwen/Qwen2.5-0.5B", "unsigned", "research")
QUIET = VerifierSpec(mode="off")


class _NoKubectl:
    def __getattr__(self, name):
        raise AssertionError(f"gitops delivery called kubectl.{name}")


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_KSERVE_NAMESPACE", "research")
    init_db()


def _sub(tmp_path: Path):
    return registry.get(
        "kserve",
        kubectl=_NoKubectl(),
        verifier=QUIET,
        delivery="gitops",
        gitops_dir=str(tmp_path / "gitops"),
    )


def _audit_rows() -> list[dict]:
    from examlops.data import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT details FROM audit_events WHERE action = 'substrate_apply'"
        ).fetchall()
    return [dict(r) for r in rows]


def test_a_gitops_apply_writes_one_file_per_object_and_a_kustomization(tmp_path):
    sub = _sub(tmp_path)
    rendered = sub.render(PRED, PRED_REF)
    result = sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    target = tmp_path / "gitops" / "research" / "inferenceservice-jpcp.yaml"
    assert result.applied == (f"gitops:{target}",)
    assert yaml.safe_load(target.read_text()) == rendered.objects[0]
    kustomization = yaml.safe_load((target.parent / "kustomization.yaml").read_text())
    assert kustomization == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": "research",
        "resources": ["inferenceservice-jpcp.yaml"],
    }
    assert len(_audit_rows()) == 1  # audited once, like a server-side apply


def test_the_same_plan_twice_is_byte_identical(tmp_path):
    sub = _sub(tmp_path)
    rendered = sub.render(PRED, PRED_REF)
    sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    path = tmp_path / "gitops" / "research" / "inferenceservice-jpcp.yaml"
    first = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert path.read_bytes() == first and path.stat().st_mtime_ns == mtime  # unchanged → untouched


def test_a_generative_canary_lands_as_two_files(tmp_path):
    sub = _sub(tmp_path)
    canary = {"version": "4", "percent": 10, "artifact_uri": "hf://Qwen/Qwen2.5-1.5B"}
    rendered = sub.render({**GEN, "rollout": {"canary": canary}}, GEN_REF)
    sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    ns = tmp_path / "gitops" / "research"
    assert sorted(p.name for p in ns.glob("*.yaml")) == [
        "kustomization.yaml",
        "llminferenceservice-chat-canary.yaml",
        "llminferenceservice-chat.yaml",
    ]


def test_a_gitops_apply_is_still_plan_gated(tmp_path):
    sub = _sub(tmp_path)
    rendered = sub.render(PRED, PRED_REF)
    with pytest.raises(PlanMismatch):
        sub.apply(rendered, dry_run=False, plan_hash="sha256:stale")
    assert not (tmp_path / "gitops").exists()


def test_a_dry_run_writes_nothing(tmp_path):
    sub = _sub(tmp_path)
    sub.apply(sub.render(PRED, PRED_REF), dry_run=True)
    assert not (tmp_path / "gitops").exists()


def test_status_reports_the_declared_version_and_stop_removes_it(tmp_path):
    sub = _sub(tmp_path)
    rendered = sub.render(PRED, PRED_REF)
    sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    status = sub.status("jpcp")
    assert status.state == "PENDING" and status.versions == {"jpcp": 17}
    assert status.detail["delivery"] == "gitops"
    sub.stop("jpcp")
    ns = tmp_path / "gitops" / "research"
    assert not list(ns.glob("*.yaml"))  # the kustomization goes with its last resource
    assert sub.status("jpcp").state == "STOPPED"
    sub.stop("jpcp")  # idempotent


def test_gitops_without_a_directory_is_unavailable_not_a_silent_apply(tmp_path):
    sub = registry.get("kserve", kubectl=_NoKubectl(), verifier=QUIET, delivery="gitops")
    rendered = sub.render(PRED, PRED_REF)
    with pytest.raises(SubstrateUnavailable, match="EXAMLOPS_KSERVE_GITOPS_DIR"):
        sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)


def test_an_unknown_delivery_is_refused(tmp_path):
    sub = registry.get("kserve", verifier=QUIET, delivery="helm-push")
    with pytest.raises(SubstrateUnavailable, match="not one of"):
        sub.apply(sub.render(PRED, PRED_REF), dry_run=True)


def test_delivery_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_KSERVE_DELIVERY", "gitops")
    monkeypatch.setenv("EXAMLOPS_KSERVE_GITOPS_DIR", str(tmp_path / "g"))
    sub = registry.get("kserve", kubectl=_NoKubectl(), verifier=QUIET)
    rendered = sub.render(PRED, PRED_REF)
    sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert (tmp_path / "g" / "research" / "inferenceservice-jpcp.yaml").is_file()


@pytest.mark.parametrize("name", ["../escape", "a/b", "UPPER", "", "x" * 64])
def test_a_name_cannot_become_a_path_outside_the_tree(tmp_path, name):
    writer = GitOpsWriter(tmp_path / "g", "research")
    obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}}
    with pytest.raises(GitOpsError):
        writer.write([obj])
    assert not list((tmp_path).rglob("*.yaml"))


def test_a_bad_namespace_is_refused(tmp_path):
    with pytest.raises(GitOpsError):
        GitOpsWriter(tmp_path, "../etc")


def test_one_bad_object_writes_none(tmp_path):
    writer = GitOpsWriter(tmp_path / "g", "ns")
    good = {"kind": "ConfigMap", "metadata": {"name": "ok"}}
    bad = {"kind": "ConfigMap", "metadata": {"name": "Bad"}}
    with pytest.raises(GitOpsError):
        writer.write([good, bad])
    assert not (tmp_path / "g").exists()


def test_the_object_count_is_bounded(tmp_path):
    writer = GitOpsWriter(tmp_path / "g", "ns")
    many = [{"kind": "ConfigMap", "metadata": {"name": f"c{i}"}} for i in range(65)]
    with pytest.raises(GitOpsError, match="cap"):
        writer.write(many)


def test_a_bad_namespace_is_a_typed_substrate_refusal_on_status_and_stop(tmp_path, monkeypatch):
    """status/stop raised a bare GitOpsError, which callers catching SubstrateError missed."""
    from examlops.serving.substrates.base import SubstrateUnavailable

    monkeypatch.setenv("EXAMLOPS_KSERVE_NAMESPACE", "Not_A_Label")
    sub = _sub(tmp_path)
    with pytest.raises(SubstrateUnavailable):
        sub.status("jpcp")
    with pytest.raises(SubstrateUnavailable):
        sub.stop("jpcp")

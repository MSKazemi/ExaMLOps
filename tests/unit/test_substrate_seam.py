# tests/unit/test_substrate_seam.py
"""USAR I1 — one substrate seam for every place a servable runs (ADR 0142 d1, spec-usar-1 §4.1).

``ServingBackend`` and ``EndpointLauncher`` described the same thing twice. The ``Substrate`` seam has
one contract: a pure ``render``, a plan-gated and audited ``apply``, ``status``/``stop``, and
advertised capabilities that refuse — never silently degrade — what a substrate cannot do.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.platform_db import init_db  # noqa: E402
from examlops.serving.substrates import registry  # noqa: E402
from examlops.serving.substrates.base import (  # noqa: E402
    ApplyFailed,
    CapabilityMissing,
    PlanMismatch,
    RenderError,
    Substrate,
    SubstrateUnavailable,
    TrafficSplit,
)
from examlops.serving.substrates.resolve import ResolvedRef  # noqa: E402

PRED = {"name": "JPCP", "framework": "sklearn"}
GEN = {
    "name": "chat",
    "task_type": "text_generation",
    "engine": {"engine": "vllm", "dtype": "float16"},
}
AGENT = {"name": "helper", "kind": "agentic"}
PRED_REF = ResolvedRef(
    "jpcp",
    "17",
    "Production",
    "s3://mlflow-artifacts/1/models/m-a/artifacts",
    "unsigned",
    "research",
)
GEN_REF = ResolvedRef(
    "chat", "3", "Production", "hf://Qwen/Qwen2.5-7B-Instruct", "unsigned", "research"
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_VLLM_WORK_DIR", str(tmp_path / "hpc"))
    init_db()


def _audit_rows(action: str = "substrate_apply") -> int:
    from examlops.data import get_db

    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = ?", (action,)
        ).fetchone()[0]


# ── R-SUB-1: exactly five named substrates, each a Substrate ─────────────────


def test_the_five_substrates_are_registered_and_satisfy_the_protocol():
    assert registry.names() == ("compose", "hpc", "kserve", "k8s-agents", "external")
    for name in registry.names():
        sub = registry.get(name)
        assert isinstance(sub, Substrate) and sub.name == name


def test_an_unknown_substrate_is_refused():
    with pytest.raises(SubstrateUnavailable):
        registry.get("azure-ml")


# ── R-SUB-2: render is pure and deterministic ────────────────────────────────


@pytest.mark.parametrize(
    ("name", "spec", "ref"),
    [
        ("compose", PRED, PRED_REF),
        ("kserve", PRED, PRED_REF),
        ("kserve", GEN, GEN_REF),
        ("hpc", GEN, GEN_REF),
    ],
)
def test_render_is_byte_identical_and_touches_no_network(monkeypatch, name, spec, ref):
    def no_network(*_a, **_k):
        raise AssertionError("render opened a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    sub = registry.get(name)
    first, second = sub.render(spec, ref), sub.render(spec, ref)
    assert first == second
    assert first.content_hash.startswith("sha256:") and first.substrate == name


# ── R-SUB-3: dry run needs no plan; a real apply needs the exact plan, audited once ──


def test_a_dry_run_needs_no_plan_and_writes_nothing():
    sub = registry.get("external")
    rendered = sub.render({**GEN, "substrate": {"base_url": "http://gpu01:8000"}}, GEN_REF)
    before = _audit_rows()
    result = sub.apply(rendered, dry_run=True)
    assert result.dry_run and result.applied == ()
    assert _audit_rows() == before
    assert sub.status("chat").state == "STOPPED"


def test_a_real_apply_acts_only_on_the_plan_it_was_shown():
    sub = registry.get("external")
    rendered = sub.render({**GEN, "substrate": {"base_url": "http://gpu01:8000"}}, GEN_REF)
    before = _audit_rows()
    with pytest.raises(PlanMismatch):
        sub.apply(rendered, dry_run=False, plan_hash="sha256:" + "0" * 64)
    assert _audit_rows() == before  # a refused plan leaves no trace but the refusal
    result = sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert result.applied == ("endpoint:chat",)
    assert _audit_rows() == before + 1
    assert sub.status("chat").address == "http://gpu01:8000"
    sub.stop("chat")
    assert sub.status("chat").state == "STOPPED"


def test_a_failing_apply_is_audited_once_and_raised_typed():
    calls = []

    def reload(model):
        calls.append(model)
        raise ConnectionError("ray serve down")

    sub = registry.get("compose", post=reload, set_traffic=lambda *a: None)
    rendered = sub.render(PRED, PRED_REF)
    before = _audit_rows()
    with pytest.raises(ApplyFailed, match="ray serve down"):
        sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert calls == ["JPCP"] and _audit_rows() == before + 1


def test_compose_apply_writes_the_split_then_reloads_the_model():
    events = []
    sub = registry.get(
        "compose",
        post=lambda model: events.append(("reload", model)),
        set_traffic=lambda model, rules: events.append(("traffic", model, rules)),
    )
    spec = {**PRED, "rollout": {"canary": {"version": "18", "percent": 10, "alias": "Canary"}}}
    rendered = sub.render(spec, PRED_REF)
    sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert events == [("traffic", "JPCP", {"Production": 90, "Canary": 10}), ("reload", "JPCP")]


class _FakeKubectl:
    """Duck-types `KubectlClient` for the KServe substrate's real-apply tests."""

    def __init__(self, *, apply_error: Exception | None = None, objects: dict | None = None):
        self._apply_error = apply_error
        self.applied: list[dict] = []
        self._objects = objects or {}
        self.deleted: list[str] = []

    def apply(self, objects: list[dict]) -> list[str]:
        if self._apply_error is not None:
            raise self._apply_error
        self.applied.extend(objects)
        return [f"{o['kind']}/{o['metadata']['name']}" for o in objects]

    def get_any_kind(self, name: str) -> dict | None:
        return self._objects.get(name)

    def delete_any_kind(self, name: str) -> None:
        self.deleted.append(name)


def test_a_kserve_apply_server_side_applies_the_rendered_object():
    fake = _FakeKubectl()
    sub = registry.get("kserve", kubectl=fake)
    rendered = sub.render(GEN, GEN_REF)
    before = _audit_rows()

    result = sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)

    assert result.applied == ("LLMInferenceService/chat",)
    assert fake.applied == list(rendered.objects)
    assert _audit_rows() == before + 1


def test_a_kserve_apply_failure_is_audited_and_raised_as_apply_failed():
    fake = _FakeKubectl(apply_error=RuntimeError("kubectl apply failed: field is immutable"))
    sub = registry.get("kserve", kubectl=fake)
    rendered = sub.render(GEN, GEN_REF)
    before = _audit_rows()

    with pytest.raises(ApplyFailed, match="immutable"):
        sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)

    assert fake.applied == []
    assert _audit_rows() == before + 1


def test_a_kserve_dry_run_never_touches_kubectl():
    fake = _FakeKubectl()
    sub = registry.get("kserve", kubectl=fake)
    rendered = sub.render(GEN, GEN_REF)

    result = sub.apply(rendered, dry_run=True)

    assert result.dry_run is True
    assert fake.applied == []


def test_a_kserve_status_reads_the_live_object():
    live = {
        "metadata": {"labels": {"examlops.io/version": "7"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}], "url": "http://chat"},
    }
    fake = _FakeKubectl(objects={"chat": live})
    status = registry.get("kserve", kubectl=fake).status("chat")

    assert status.state == "READY"
    assert status.address == "http://chat"
    assert status.versions == {"chat": 7}


def test_a_kserve_status_for_an_absent_service_is_unknown_not_an_error():
    fake = _FakeKubectl()
    status = registry.get("kserve", kubectl=fake).status("ghost")

    assert status.state == "UNKNOWN"


def test_a_kserve_stop_deletes_the_service():
    fake = _FakeKubectl()
    registry.get("kserve", kubectl=fake).stop("chat")

    assert fake.deleted == ["chat"]


# ── R-SUB-5 / R-SUB-17 / R-SUB-32: capabilities refuse, never degrade ───────


def test_an_agent_on_kserve_is_refused_naming_the_agent_substrate():
    with pytest.raises(CapabilityMissing, match="k8s-agents"):
        registry.get("kserve").render(AGENT, PRED_REF)


def test_scale_to_zero_on_kserve_standard_is_refused():
    with pytest.raises(CapabilityMissing, match="scale_to_zero"):
        registry.get("kserve").render({**PRED, "scaling": {"min_replicas": 0}}, PRED_REF)


def test_external_advertises_nothing_it_cannot_control():
    caps = registry.get("external").capabilities()
    assert not (caps.canary or caps.scale_to_zero or caps.multinode)
    with pytest.raises(CapabilityMissing, match="canary"):
        registry.get("external").render(
            {
                **GEN,
                "substrate": {"base_url": "http://x:1"},
                "rollout": {"canary": {"version": "4", "percent": 5}},
            },
            GEN_REF,
        )


def test_the_agent_substrate_says_it_is_not_built_rather_than_pretending():
    sub = registry.get("k8s-agents")
    assert sub.capabilities().kinds == frozenset({"agentic"})
    with pytest.raises(SubstrateUnavailable, match="I6"):
        sub.render(AGENT, PRED_REF)


# ── R-SUB-24: one traffic intent, validated once ─────────────────────────────


@pytest.mark.parametrize(
    ("stable", "canary", "pct"), [("17", "18", 120), ("17", "17", 10), ("17", "18", -1)]
)
def test_bad_traffic_intents_are_refused(stable, canary, pct):
    with pytest.raises(RenderError):
        TrafficSplit(stable, canary, pct)


# ── R-SUB-33: seam parity — what renders for KServe renders off Kubernetes too ──


@pytest.mark.parametrize(
    ("spec", "ref", "other"), [(PRED, PRED_REF, "compose"), (GEN, GEN_REF, "hpc")]
)
def test_every_kserve_fixture_also_renders_on_a_non_kubernetes_substrate(spec, ref, other):
    before = dict(spec)
    assert registry.get("kserve").render(spec, ref).objects
    assert registry.get(other).render(spec, ref).objects
    assert spec == before  # the servable definition never changes with the substrate


# ── R-SUB-46: no rendered object points at the control plane ────────────────


@pytest.mark.parametrize(
    ("name", "spec", "ref"),
    [
        ("compose", PRED, PRED_REF),
        ("kserve", PRED, PRED_REF),
        ("kserve", GEN, GEN_REF),
        ("hpc", GEN, GEN_REF),
    ],
)
def test_renders_reference_no_control_plane_endpoint(monkeypatch, name, spec, ref):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow.internal:15000")
    text = repr(registry.get(name).render(spec, ref).objects)
    for needle in (
        "mlflow.internal",
        ":15000",
        "mlflow://",
        ":18002",
        "platform.db",
        "/api/2.0/prefect",
    ):
        assert needle not in text, (name, needle)


# ── R-SUB-47: typed errors carry distinct exit codes ─────────────────────────


def test_each_typed_error_has_its_own_exit_code():
    codes = {
        cls.exit_code
        for cls in (SubstrateUnavailable, CapabilityMissing, PlanMismatch, ApplyFailed)
    }
    assert len(codes) == 4


# ── R-SUB-23 on KServe: the canary is the resolved canary version, or it is refused ──


def test_a_kserve_canary_renders_the_resolved_canary_or_is_refused():
    canary = {"version": "18", "percent": 10, "alias": "Canary"}
    with pytest.raises(RenderError, match="resolved"):
        registry.get("kserve").render({**PRED, "rollout": {"canary": canary}}, PRED_REF)
    spec = {
        **PRED,
        "rollout": {"canary": {**canary, "artifact_uri": "s3://mlflow-artifacts/1/m-b"}},
    }
    (manifest,) = registry.get("kserve").render(spec, PRED_REF).objects
    (entry,) = manifest["spec"]["canary"]
    assert entry["trafficPercent"] == 10
    assert entry["predictor"]["model"]["storageUri"] == "s3://mlflow-artifacts/1/m-b"


# ── R-SUB-30/31: real starts go through the ADR 0107 launchers, exactly as planned ──


class _FakeLauncher:
    """Records what a real HpcLauncher / ComposeLauncher would be asked to do."""

    def __init__(self, script: str | None = None) -> None:
        self.started, self._script = [], script

    def render_script(self, es):
        from examlops.llm_endpoints import HpcLauncher

        return self._script if self._script is not None else HpcLauncher("mock").render_script(es)

    def start(self, es):
        from examlops.llm_endpoints import EndpointHandle

        self.started.append(es)
        return EndpointHandle(model=es.model, launcher="slurm", state="SUBMITTED", job_id="4242")


def test_an_hpc_apply_submits_the_planned_job_and_records_the_endpoint():
    from examlops.data.serving import get_llm_endpoint

    fake = _FakeLauncher()
    sub = registry.get("hpc", launcher=fake)
    rendered = sub.render(GEN, GEN_REF)
    before = _audit_rows()
    result = sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert result.applied == ("job:mock:4242",)
    (es,) = fake.started
    assert (es.model, es.hf_model_id, es.project) == (
        "chat",
        "Qwen/Qwen2.5-7B-Instruct",
        "research",
    )
    rec = get_llm_endpoint("chat")
    assert rec["job_id"] == "4242" and rec["launcher"] == "slurm"
    assert _audit_rows() == before + 1


def test_an_hpc_apply_refuses_a_job_script_that_changed_since_the_plan():
    rendered = registry.get("hpc").render(GEN, GEN_REF)
    fake = _FakeLauncher(script="#!/bin/bash\necho something else\n")
    before = _audit_rows()
    with pytest.raises(ApplyFailed, match="re-plan"):
        registry.get("hpc", launcher=fake).apply(
            rendered, dry_run=False, plan_hash=rendered.content_hash
        )
    assert fake.started == [] and _audit_rows() == before + 1


def test_a_compose_llm_apply_starts_the_vllm_service_through_its_launcher():
    fake = _FakeLauncher()
    sub = registry.get("compose", launcher=fake)
    rendered = sub.render(GEN, GEN_REF)
    result = sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    assert result.applied == ("compose:vllm:chat",)
    assert [es.hf_model_id for es in fake.started] == ["Qwen/Qwen2.5-7B-Instruct"]


# ── R-SUB-48: one span per real apply when tracing is on ────────────────────


def test_a_real_apply_emits_one_substrate_span(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **k: provider.get_tracer("t"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    sub = registry.get("external")
    rendered = sub.render({**GEN, "substrate": {"base_url": "http://gpu01:8000"}}, GEN_REF)
    sub.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
    (span,) = exporter.get_finished_spans()
    assert span.name == "substrate.apply"
    assert span.attributes["examlops.substrate"] == "external"
    assert span.attributes["examlops.substrate.result"] == "applied"

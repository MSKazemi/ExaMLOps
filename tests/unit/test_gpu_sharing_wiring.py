"""ADR 0030 — fractional GPUs wired into the paths they govern (decisions 1, 2, 3, 5).

`test_gpu_sharing.py` covers the pure planner (decision 4). These tests cover what the ADR's
status said was missing: the scheduler-neutral ask and placement carry a fraction (1), a KServe pod
requests one (2), Slurm/Flux get MIG/shard GRES where the cluster declares them and an explicit
whole-GPU fallback where it does not (3), and a job's GPU-hours are billed at its allocated
fraction in `exa models cost` (5). Every test asserts an outcome, not an argv.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
_ADAPTER_DIR = ROOT / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

from examlops.gpu_sharing import (  # noqa: E402
    ClusterGpuCaps,
    FractionalAsk,
    caps_from_capabilities,
)
from examlops.gpu_sharing.scheduler_map import (  # noqa: E402
    ENV_FRACTION,
    ENV_MIG_PROFILE,
    ENV_SHARING_CAPS,
    GpuSharingError,
    ask_env,
    ask_from_env,
    caps_from_env,
    map_to_scheduler,
    sharing_env_for_capabilities,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    for var in (ENV_FRACTION, ENV_MIG_PROFILE, ENV_SHARING_CAPS):
        monkeypatch.delenv(var, raising=False)
    from examlops import platform_db

    platform_db.init_db()
    yield


_MIG_CAPS = {
    "mig_profiles": ["1g.5gb", "2g.10gb", "3g.20gb", "7g.40gb"],
    "mig_gres_types": {"1g.5gb": "a100_1g.5gb"},
    "flux_mig_properties": {"1g.5gb": "mig-1g"},
}


# ── decision 3: scheduler mapping ─────────────────────────────────────────────────────────────


def test_slurm_mig_becomes_a_typed_gres_and_drops_whole_gpu_flags():
    base = {"gpus": "1", "gpus_per_node": "1", "partition": "gpu"}
    m = map_to_scheduler(
        "slurm",
        base,
        FractionalAsk("m", fraction=0.1),
        caps_from_capabilities(_MIG_CAPS),
        gpus=2,
    )
    assert m.choice.mechanism == "mig" and m.choice.mig_profile == "1g.5gb"
    assert m.resources == {"partition": "gpu", "gres": "gpu:a100_1g.5gb:2"}
    assert m.warnings == []
    assert base["gpus"] == "1", "the caller's dict must not be mutated"


def test_slurm_multi_node_mig_gres_is_per_node_not_the_job_total():
    # --gres is a per-node count: 4 slices over 2 nodes is gpu:<type>:2 on each node, not :4
    # (which would allocate 8 slices).
    m = map_to_scheduler(
        "slurm",
        {"nodes": "2"},
        FractionalAsk("m", fraction=0.1),
        caps_from_capabilities(_MIG_CAPS),
        gpus=4,
    )
    assert m.resources == {"nodes": "2", "gres": "gpu:a100_1g.5gb:2"}
    assert m.warnings == []


def test_slurm_multi_node_shards_are_sized_per_node():
    caps = caps_from_capabilities({"shards_per_gpu": 4})
    m = map_to_scheduler("slurm", {"nodes": 2}, FractionalAsk("m", fraction=0.5), caps, gpus=2)
    assert m.resources == {"nodes": 2, "gres": "shard:2"}  # 1 GPU/node × 0.5 × 4 shards
    assert m.choice.allocated_fraction == pytest.approx(0.5)


def test_slurm_gpus_that_do_not_split_over_the_nodes_fall_back_and_warn():
    m = map_to_scheduler(
        "slurm",
        {"nodes": "2"},
        FractionalAsk("m", fraction=0.1),
        caps_from_capabilities(_MIG_CAPS),
        gpus=3,
    )
    assert m.choice.mechanism == "whole" and "gres" not in m.resources
    assert m.resources["gpus"] == 3
    assert m.warnings and "per-node" in m.warnings[0]


def test_slurm_node_range_cannot_carry_a_fixed_per_node_gres():
    with pytest.raises(GpuSharingError, match="fixed node count"):
        map_to_scheduler(
            "slurm",
            {"nodes": "2-4"},
            FractionalAsk("m", fraction=0.1),
            caps_from_capabilities(_MIG_CAPS),
            gpus=2,
        )


def test_slurm_mig_profile_without_a_site_type_is_requested_by_its_own_name():
    m = map_to_scheduler(
        "slurm",
        {},
        FractionalAsk("m", mig_profile="2g.10gb"),
        caps_from_capabilities(_MIG_CAPS),
    )
    assert m.resources["gres"] == "gpu:2g.10gb:1"


def test_slurm_timeslice_uses_gres_shard_rounded_up():
    caps = caps_from_capabilities({"shards_per_gpu": 8})
    m = map_to_scheduler("slurm", {"gpus": 1}, FractionalAsk("m", fraction=0.3), caps)
    assert m.resources == {"gres": "shard:3"}  # ceil(0.3 × 8) = 3 shards
    assert m.choice.mechanism == "timeslice" and m.choice.isolation == "soft"
    assert m.choice.allocated_fraction == pytest.approx(3 / 8)
    assert m.choice.wasted_fraction == pytest.approx(3 / 8 - 0.3)


def test_slurm_without_declared_sharing_falls_back_to_a_whole_gpu_and_warns():
    m = map_to_scheduler("slurm", {}, FractionalAsk("m", fraction=0.25), ClusterGpuCaps())
    assert m.resources == {"gpus": 1}
    assert m.choice.mechanism == "whole" and m.choice.allocated_fraction == 1.0
    assert m.choice.wasted_fraction == pytest.approx(0.75)
    assert m.warnings and "75% wasted" in m.warnings[0]


def test_slurm_timeslice_capable_but_no_shards_is_never_silently_overcommitted():
    caps = ClusterGpuCaps(supports_timeslice=True)  # time-slicing claimed, no shard count
    m = map_to_scheduler("slurm", {}, FractionalAsk("m", fraction=0.5), caps)
    assert m.choice.mechanism == "whole"
    assert "gres" not in m.resources and m.resources["gpus"] == 1
    assert "shards_per_gpu" in m.choice.note


def test_flux_mig_uses_the_declared_node_property():
    m = map_to_scheduler(
        "flux", {}, FractionalAsk("m", mig_profile="1g.5gb"), caps_from_capabilities(_MIG_CAPS)
    )
    assert m.resources == {"gpus": 1, "constraint": "mig-1g"}
    assert m.choice.mechanism == "mig"


def test_flux_mig_without_a_property_is_a_whole_gpu():
    caps = caps_from_capabilities({"mig_profiles": ["2g.10gb"]})
    m = map_to_scheduler("flux", {}, FractionalAsk("m", mig_profile="2g.10gb"), caps)
    assert m.choice.mechanism == "whole" and m.warnings
    assert "no node property" in m.choice.note


def test_flux_mig_refuses_to_clobber_an_existing_constraint():
    m = map_to_scheduler(
        "flux",
        {"constraint": "fastnet"},
        FractionalAsk("m", mig_profile="1g.5gb"),
        caps_from_capabilities(_MIG_CAPS),
    )
    assert m.resources["constraint"] == "fastnet"
    assert m.choice.mechanism == "whole" and m.warnings


def test_flux_cannot_express_timeslicing():
    caps = caps_from_capabilities({"shards_per_gpu": 4})
    m = map_to_scheduler("flux", {}, FractionalAsk("m", fraction=0.5), caps)
    assert m.choice.mechanism == "whole"
    assert "flux-core" in m.choice.note


def test_whole_gpu_ask_passes_through_without_warning():
    m = map_to_scheduler(
        "slurm", {"gpus": 2}, FractionalAsk("m"), caps_from_capabilities(_MIG_CAPS), gpus=2
    )
    assert m.resources == {"gpus": 2}
    assert m.choice.mechanism == "whole" and m.warnings == []


@pytest.mark.parametrize(
    "scheduler,ask,gpus,fragment",
    [
        ("pbs", FractionalAsk("m", fraction=0.5), 1, "unknown scheduler"),
        ("slurm", FractionalAsk("m", mig_profile="9g.99gb"), 1, "unknown MIG profile"),
        ("slurm", FractionalAsk("m", fraction=1.5), 1, "(0, 1]"),
        ("slurm", FractionalAsk("m", fraction=0.5), 0, "at least one GPU"),
    ],
)
def test_malformed_asks_are_refused(scheduler, ask, gpus, fragment):
    with pytest.raises(GpuSharingError, match=None) as exc:
        map_to_scheduler(scheduler, {}, ask, ClusterGpuCaps(), gpus=gpus)
    assert fragment in str(exc.value)


def test_slurm_adapter_emits_the_gres_flag(tmp_path):
    import adapter as slurm_adapter
    from executor import CompletedCommand

    seen: list[list[str]] = []

    class _Exec:
        def run(self, cmd, *, timeout=None, cwd=None):
            seen.append(list(cmd))
            return CompletedCommand(0, "Submitted batch job 4242", "")

        def put(self, local, remote):  # pragma: no cover - not used without remote_dir
            pass

    script = tmp_path / "run.sh"
    script.write_text("#!/bin/bash\n")
    a = slurm_adapter.RealSlurmAdapter(executor=_Exec(), working_dir=str(tmp_path / "jobs"))
    job = a.submit_job(str(script), resources={"gres": "gpu:a100_1g.5gb:1"})
    assert job == "4242"
    assert "--gres=gpu:a100_1g.5gb:1" in seen[0]


# ── env hand-off (exa pipeline run → run subprocess) ─────────────────────────────────────────


def test_env_round_trip_carries_the_ask_and_the_caps():
    env = {**ask_env(0.25, "1g.5gb"), **sharing_env_for_capabilities(_MIG_CAPS)}
    ask = ask_from_env(env)
    assert ask is not None and ask.mig_profile == "1g.5gb" and ask.fraction == 0.25
    caps = caps_from_env(env)
    assert caps.supports_mig and caps.mig_gres_types == {"1g.5gb": "a100_1g.5gb"}


def test_env_without_an_ask_is_a_whole_gpu_run():
    assert ask_from_env({}) is None
    assert ask_env(1.0, None) == {}
    assert ask_from_env({ENV_FRACTION: "1"}) is None
    assert caps_from_env({}) == ClusterGpuCaps()
    assert sharing_env_for_capabilities({"total_gpus": 8}) == {}


def test_env_garbage_is_refused_not_guessed():
    with pytest.raises(GpuSharingError):
        ask_from_env({ENV_FRACTION: "a quarter"})
    with pytest.raises(GpuSharingError):
        caps_from_env({ENV_SHARING_CAPS: "{not json"})


def test_capabilities_parsing_is_tolerant_of_junk():
    caps = caps_from_capabilities(
        {"mig_profiles": "1g.5gb", "shards_per_gpu": "lots", "mig_gres_types": ["x"]}
    )
    assert caps == ClusterGpuCaps()


def test_resolve_env_exports_the_clusters_sharing_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    from examlops import hpc_registry as reg
    from examlops.platform_db import set_cluster_state

    reg.register_pending("gpu", "slurm", transport="local", capabilities=_MIG_CAPS)
    reg.register_pending("cpu", "slurm", transport="local", capabilities={"total_gpus": 0})
    set_cluster_state("gpu", "ACTIVE", approved_by="admin")
    set_cluster_state("cpu", "ACTIVE", approved_by="admin")

    gpu_env = reg.resolve_env("gpu")
    assert json.loads(gpu_env[ENV_SHARING_CAPS])["mig_profiles"] == _MIG_CAPS["mig_profiles"]
    # A cluster with no sharing overrides a stale value rather than inheriting it.
    assert reg.resolve_env("cpu")[ENV_SHARING_CAPS] == "{}"


# ── decision 1: scheduler-neutral ask + placement ─────────────────────────────────────────────


def _cluster(name, caps, gpus=4):
    return {
        "name": name,
        "scheduler": "slurm",
        "capabilities": {"total_gpus": gpus, "total_nodes": 1, **caps},
        "nodes": [],
    }


def test_fractional_ask_prefers_the_cluster_that_can_slice_it():
    from examlops.hpc_placement import ResourceAsk, choose_cluster

    ask = ResourceAsk(gpus=1, gpu_fraction=0.1)
    result = choose_cluster(ask, [_cluster("plain", {}), _cluster("mig", _MIG_CAPS)])
    assert result.cluster == "mig"
    by_name = {c["name"]: c for c in result.candidates}
    assert by_name["mig"]["gpu_sharing"]["mechanism"] == "mig"
    assert by_name["plain"]["gpu_sharing"]["mechanism"] == "whole"
    assert by_name["plain"]["gpu_sharing"]["wasted_fraction"] == pytest.approx(0.9)
    assert "gpu sharing: mig (hardware isolation)" in result.reason


def test_whole_gpu_placement_is_unchanged():
    from examlops.hpc_placement import ResourceAsk, choose_cluster

    result = choose_cluster(
        ResourceAsk(gpus=1), [_cluster("a", {}, gpus=2), _cluster("b", _MIG_CAPS, gpus=4)]
    )
    assert result.cluster == "b"  # most idle GPUs, as before
    assert all("gpu_sharing" not in c for c in result.candidates)
    assert "gpu sharing" not in result.reason


def test_resource_ask_rejects_an_impossible_fraction():
    from examlops.hpc_placement import ResourceAsk

    with pytest.raises(ValueError):
        ResourceAsk(gpus=1, gpu_fraction=0.0)
    assert not ResourceAsk(gpus=0, gpu_fraction=0.5).is_fractional
    assert ResourceAsk(gpus=1, mig_profile="1g.5gb").is_fractional


def test_hardware_profile_ask_carries_the_fraction():
    from examlops.hardware_profiles import create_profile_version, resolve_profile, to_resource_ask

    create_profile_version(
        "slice",
        accelerator_family="nvidia",
        gpu_count=1,
        gpu_fraction=0.25,
        mig_profile="2g.10gb",
        cpu=2.0,
    )
    ask = to_resource_ask(resolve_profile("slice"))
    assert (ask.gpus, ask.gpu_fraction, ask.mig_profile) == (1, 0.25, "2g.10gb")


# ── decision 2: KServe pods request a fraction ────────────────────────────────────────────────


def _ref():
    from examlops.serving.substrates.resolve import ResolvedRef

    return ResolvedRef(
        model="jpcp",
        version="17",
        alias="Production",
        artifact_uri="s3://mlflow-artifacts/1/models/m-abc/artifacts",
        digest="sha256:" + "a" * 64,
        project="research",
    )


_SK = {"name": "JPCP", "task_type": "regression", "framework": "sklearn"}


def _render(extra):
    from examlops.serving.substrates import k8s_schema, kserve

    manifest = kserve.render({**_SK, **extra}, _ref())
    assert k8s_schema.validate(manifest) == [], "rendered GPU request must be CRD-valid"
    return manifest


def test_kserve_mig_request_is_schema_valid_and_annotated():
    m = _render({"gpu_sharing": {"mig_profile": "1g.5gb"}})
    assert m["spec"]["predictor"]["model"]["resources"] == {
        "limits": {"nvidia.com/mig-1g.5gb": "1"}
    }
    ann = m["metadata"]["annotations"]
    assert ann["examlops.io/gpu-mechanism"] == "mig"
    assert ann["examlops.io/gpu-isolation"] == "hardware"


def test_kserve_mig_snaps_a_bare_fraction_up():
    m = _render({"gpu_sharing": {"fraction": 0.2, "mechanism": "mig"}})
    assert m["spec"]["predictor"]["model"]["resources"]["limits"] == {"nvidia.com/mig-2g.10gb": "1"}
    assert m["metadata"]["annotations"]["examlops.io/gpu-allocated-fraction"] == "0.2857"


def test_kserve_hami_sets_memory_and_core_percentages():
    m = _render({"gpu_sharing": {"fraction": 0.25, "mechanism": "hami"}})
    assert m["spec"]["predictor"]["model"]["resources"]["limits"] == {
        "nvidia.com/gpu": "1",
        "nvidia.com/gpumem-percentage": "25",
        "nvidia.com/gpucores": "25",
    }


def test_kserve_timeslice_falls_back_to_autoscale_fraction_and_says_soft():
    m = _render({"autoscale": {"gpu_fraction": 0.5}})
    assert m["spec"]["predictor"]["model"]["resources"]["limits"] == {"nvidia.com/gpu": "1"}
    ann = m["metadata"]["annotations"]
    assert ann["examlops.io/gpu-mechanism"] == "timeslice"
    assert ann["examlops.io/gpu-isolation"] == "soft"
    assert ann["examlops.io/gpu-fraction"] == "0.5"


def test_kserve_without_a_gpu_ask_is_unchanged():
    m = _render({})
    assert "resources" not in m["spec"]["predictor"]["model"]
    assert not any(k.startswith("examlops.io/gpu-") for k in m["metadata"]["annotations"])


def test_kserve_canary_predictors_both_carry_the_request():
    from examlops.serving.substrates import k8s_schema, kserve
    from examlops.serving.substrates.resolve import ResolvedRef

    canary = ResolvedRef(**{**_ref().__dict__, "version": "18"})
    m = kserve.render(
        {**_SK, "gpu_sharing": {"mig_profile": "1g.5gb"}}, _ref(), canary=canary, canary_pct=10
    )
    assert k8s_schema.validate(m) == []
    canary_model = m["spec"]["canary"][0]["predictor"]["model"]
    assert canary_model["resources"]["limits"] == {"nvidia.com/mig-1g.5gb": "1"}


def test_llm_inference_service_container_requests_the_fraction():
    from examlops.serving.substrates import k8s_schema, kserve

    llm = {
        "name": "ChatModel",
        "task_type": "text_generation",
        "engine": {"engine": "vllm"},
        "gpu_sharing": {"mig_profile": "3g.20gb"},
    }
    m = kserve.render(llm, _ref())
    assert k8s_schema.validate(m) == []
    main = m["spec"]["template"]["containers"][0]
    assert main["resources"] == {"limits": {"nvidia.com/mig-3g.20gb": "1"}}
    assert m["metadata"]["annotations"]["examlops.io/gpu-mig-profile"] == "3g.20gb"


@pytest.mark.parametrize(
    "block,fragment",
    [
        ({"fraction": 0.3, "mig_profile": "1g.5gb"}, "smaller than fraction"),
        ({"mechanism": "mps"}, "not one of"),
        ({"frac": 0.5}, "unknown gpu_sharing key"),
        ({"fraction": 2}, "(0, 1]"),
        ({"mig_profile": "1g.5gb", "mechanism": "hami"}, "only applies"),
    ],
)
def test_invalid_gpu_sharing_blocks_refuse_to_render(block, fragment):
    from examlops.serving.substrates import kserve
    from examlops.serving.substrates.resolve import RenderError

    with pytest.raises(RenderError) as exc:
        kserve.render({**_SK, "gpu_sharing": block}, _ref())
    assert fragment in str(exc.value)


def test_gpu_sharing_disagreeing_with_autoscale_is_an_error():
    from examlops.gpu_sharing.k8s import validate_gpu_sharing_block

    errors = validate_gpu_sharing_block({"fraction": 0.25}, autoscale={"gpu_fraction": 0.5})
    assert errors and "disagrees" in errors[0]


def test_every_pack_model_yaml_gpu_sharing_block_is_valid():
    """Registry guard: a malformed `gpu_sharing:` block must fail CI, not the deploy."""
    import yaml

    from examlops.gpu_sharing.k8s import validate_gpu_sharing_block

    for path in sorted((ROOT / "usecases").glob("*/models/*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        errors = validate_gpu_sharing_block(doc.get("gpu_sharing"), autoscale=doc.get("autoscale"))
        assert errors == [], f"{path.name}: {errors}"


# ── decision 5: accounting ────────────────────────────────────────────────────────────────────


def _audit_actions() -> list[dict]:
    from examlops.platform_db import get_db

    with get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit_events WHERE action='gpu_allocation_recorded'"
            ).fetchall()
        ]


def test_record_allocation_links_the_job_and_is_audited():
    from examlops.gpu_sharing import allocation_for_job, record_allocation

    m = map_to_scheduler(
        "slurm", {}, FractionalAsk("m", fraction=0.1), caps_from_capabilities(_MIG_CAPS)
    )
    record_allocation("JPCP", m.choice, job_id="777", scheduler="slurm", tenant="research")
    row = allocation_for_job("777")
    assert row is not None and row["mechanism"] == "mig" and row["scheduler"] == "slurm"
    assert row["fraction"] == pytest.approx(1 / 7)
    assert row["requested_fraction"] == pytest.approx(0.1)
    events = _audit_actions()
    assert len(events) == 1 and events[0]["target"] == "JPCP"
    assert json.loads(events[0]["details"])["job_id"] == "777"
    assert allocation_for_job("nope") is None


def test_list_allocations_filters_tenant_before_the_limit():
    from examlops.gpu_sharing import MechanismChoice, list_allocations, record_allocation

    choice = MechanismChoice("whole", "exclusive", 1.0, 1.0, 0.0, "full GPU")
    record_allocation("A", choice, tenant="t1")
    for i in range(5):
        record_allocation(f"B{i}", choice, tenant="t2")
    rows = list_allocations(tenant="t1", limit=2)
    assert [r["model"] for r in rows] == ["A"]
    assert len(list_allocations(limit=2)) == 2


def test_bill_job_scales_only_linked_jobs():
    from examlops.gpu_sharing import MechanismChoice, record_allocation
    from examlops.gpu_sharing.accounting import bill_job

    record_allocation(
        "JPCP", MechanismChoice("mig", "hardware", 0.25, 0.2, 0.05, "MIG"), job_id="j1"
    )
    record_allocation(
        "JPCP", MechanismChoice("whole", "exclusive", 1.0, 0.25, 0.75, "fallback"), job_id="j2"
    )
    b1 = bill_job("j1", 8.0)
    assert (b1.gpu_hours, b1.fraction, b1.mechanism, b1.scaled) == (2.0, 0.25, "mig", True)
    b2 = bill_job("j2", 8.0)
    assert (b2.gpu_hours, b2.scaled) == (8.0, False)  # the fallback's waste is paid in full
    b3 = bill_job("unlinked", 8.0)
    assert (b3.gpu_hours, b3.fraction) == (8.0, None)
    assert bill_job("j1", None).gpu_hours is None


def test_bill_job_never_takes_another_schedulers_fraction():
    # Job ids are unique only within a scheduler: Flux job "4711" is not Slurm job "4711".
    from examlops.gpu_sharing import MechanismChoice, record_allocation
    from examlops.gpu_sharing.accounting import bill_job

    record_allocation(
        "JPCP",
        MechanismChoice("mig", "hardware", 0.25, 0.25, 0.0, "MIG"),
        job_id="4711",
        scheduler="flux",
    )
    other = bill_job("4711", 8.0, scheduler="slurm")
    assert (other.gpu_hours, other.fraction) == (8.0, None)
    same = bill_job("4711", 8.0, scheduler="FLUX")
    assert (same.gpu_hours, same.fraction) == (2.0, 0.25)


def test_hardware_profile_export_clears_a_stale_shell_ask(monkeypatch):
    from examlops.cli.commands.pipeline import _export_gpu_sharing_ask
    from examlops.hpc_placement import ResourceAsk

    monkeypatch.setenv(ENV_FRACTION, "0.1")
    monkeypatch.setenv(ENV_MIG_PROFILE, "1g.5gb")
    monkeypatch.setenv("EXAMLOPS_HPC_GPUS", "1")
    _export_gpu_sharing_ask(ResourceAsk(gpus=2))  # a whole-GPU profile
    assert ask_from_env() is None, "a whole-GPU profile must not run on a stale slice"

    _export_gpu_sharing_ask(ResourceAsk(gpus=2, gpu_fraction=0.5))
    got = ask_from_env()
    assert got is not None and got.fraction == 0.5
    assert os.environ["EXAMLOPS_HPC_GPUS"] == "2", "the mapping must size 2 devices, not 1"


def test_models_cost_record_bills_the_allocated_fraction(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.gpu_sharing import MechanismChoice, record_allocation
    from examlops.platform_db import get_model_costs

    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")
    record_allocation(
        "JPCP",
        MechanismChoice("mig", "hardware", 0.25, 0.25, 0.0, "MIG 2g"),
        job_id="9001",
        scheduler="slurm",
    )

    def _get(url, *a, **k):
        if "registered-models/get" in url:
            return {"registered_model": {"latest_versions": [{"version": "3", "run_id": "r1"}]}}
        return {"run": {"data": {"tags": [{"key": "hpc_job_id", "value": "9001"}]}}}

    with (
        patch("examlops.cli._client.get", side_effect=_get),
        patch("examlops.cli._client.post", return_value={}),
        patch("examlops.cli.commands.models._real_sacct", return_value=8.0),
    ):
        result = CliRunner().invoke(app, ["models", "cost", "JPCP", "--record"])
    assert result.exit_code == 0, result.output
    rows = get_model_costs("JPCP")
    assert len(rows) == 1
    assert rows[0]["gpu_hours"] == pytest.approx(2.0)  # 8 device-hours × 0.25
    assert rows[0]["gpu_fraction"] == pytest.approx(0.25)
    assert rows[0]["gpu_mechanism"] == "mig"
    assert "0.25 mig" in result.output


def test_gpu_share_plan_maps_onto_a_registered_cluster(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops import hpc_registry as reg
    from examlops.cli.main import app
    from examlops.platform_db import set_cluster_state

    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    reg.register_pending("gpu", "slurm", transport="local", capabilities=_MIG_CAPS)
    set_cluster_state("gpu", "ACTIVE", approved_by="admin")
    result = CliRunner().invoke(
        app, ["--json", "hpc", "gpu-share", "plan", "JPCP", "--fraction", "0.1", "--cluster", "gpu"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mechanism"] == "mig"
    assert payload["resources"] == {"gres": "gpu:a100_1g.5gb:1"}


def test_gpu_share_plan_refuses_a_pending_cluster(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops import hpc_registry as reg
    from examlops.cli.main import app

    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    reg.register_pending("gpu", "slurm", transport="local", capabilities=_MIG_CAPS)
    result = CliRunner().invoke(
        app, ["hpc", "gpu-share", "plan", "JPCP", "--fraction", "0.1", "--cluster", "gpu"]
    )
    assert result.exit_code != 0


# ── the pipeline submit path (needs the use-case pack → modelzoo) ─────────────────────────────

_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or ROOT / "modelzoo")
needs_pipelines = pytest.mark.skipif(
    not (_MZ / "seanergys_modelzoo").is_dir(),
    reason="pipeline_generator imports the use-case pack, which needs modelzoo",
)


@needs_pipelines
def test_pipeline_submit_maps_and_records_the_allocation(monkeypatch):
    sys.path.insert(0, str(ROOT / "pipelines"))
    import pipeline_generator as pg

    from examlops.gpu_sharing import allocation_for_job

    base = {"gpus": "1", "partition": "gpu"}
    assert pg._apply_gpu_sharing("slurm", base) == (base, None)  # no ask ⇒ untouched

    monkeypatch.setenv(ENV_FRACTION, "0.1")
    monkeypatch.setenv(ENV_SHARING_CAPS, json.dumps(_MIG_CAPS))
    resources, mapping = pg._apply_gpu_sharing("slurm", base)
    assert resources == {"partition": "gpu", "gres": "gpu:a100_1g.5gb:1"}
    pg._record_gpu_allocation_safe("JPCP", "5150", "slurm", mapping)
    assert allocation_for_job("5150")["mechanism"] == "mig"

    monkeypatch.setenv(ENV_FRACTION, "not-a-number")
    with pytest.raises(GpuSharingError):
        pg._apply_gpu_sharing("slurm", base)


@pytest.mark.parametrize("key", ["tensor_parallel_size", "pipeline_parallel_size"])
def test_llm_gpu_share_with_multi_gpu_parallelism_refuses_to_render(key):
    # A vLLM engine sharded over 2 GPUs cannot start in one pod limited to a single MIG slice or
    # a single shared device: rendering it would ship a pod whose engine can never come up.
    from examlops.serving.substrates import kserve
    from examlops.serving.substrates.resolve import RenderError

    llm = {
        "name": "ChatModel",
        "task_type": "text_generation",
        "engine": {"engine": "vllm", key: 2},
        "gpu_sharing": {"mig_profile": "3g.20gb"},
    }
    with pytest.raises(RenderError, match="parallel"):
        kserve.render(llm, _ref())


def test_llm_data_parallel_replicas_may_each_take_a_fraction():
    from examlops.serving.substrates import k8s_schema, kserve

    llm = {
        "name": "ChatModel",
        "task_type": "text_generation",
        "engine": {"engine": "vllm", "data_parallel_size": 2},
        "gpu_sharing": {"mig_profile": "3g.20gb"},
    }
    m = kserve.render(llm, _ref())
    assert k8s_schema.validate(m) == []
    assert m["spec"]["parallelism"] == {"data": 2}
    assert m["spec"]["template"]["containers"][0]["resources"] == {
        "limits": {"nvidia.com/mig-3g.20gb": "1"}
    }

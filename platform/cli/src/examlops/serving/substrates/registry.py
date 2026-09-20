"""The five substrates, behind one :class:`~.base.Substrate` contract (ADR 0142 d1, R-SUB-1).

| name | kinds | how it runs |
|---|---|---|
| ``compose``    | predictive, generative | Ray ``MultiModelServer`` (reload via the admin route); ``vllm`` Compose profile |
| ``hpc``        | generative             | a long-lived ``vllm serve`` allocation rendered by ``HpcLauncher`` (Slurm/Flux) |
| ``kserve``     | predictive, generative | ``InferenceService`` / ``LLMInferenceService`` for the pinned KServe release |
| ``k8s-agents`` | agentic                | the agent runtime on Kubernetes — **not built yet** (USAR I6); refuses honestly |
| ``external``   | predictive, generative | a base URL someone else operates; nothing to control |

Each implementation delegates to what already exists — the I0 KServe renderer, the ADR 0107
launchers, the Ray admin routes — so no serving code is written twice.
"""

from __future__ import annotations

import os
from typing import Any

from examlops.serving.substrates.base import (
    ApplyFailed,
    CapabilityMissing,
    Rendered,
    RenderError,
    SubstrateCaps,
    SubstrateStatus,
    SubstrateUnavailable,
    TrafficSplit,
    audited_apply,
    make_rendered,
    require_caps,
    servable_kind,
)
from examlops.serving.substrates.resolve import ResolvedRef

_NAMES = ("compose", "hpc", "kserve", "k8s-agents", "external")


def _labels(ref: ResolvedRef) -> dict[str, str]:
    return {
        "examlops.io/model": ref.model,
        "examlops.io/version": ref.version,
        "examlops.io/alias": ref.alias or "none",
        "examlops.io/project": ref.project,
        "app.kubernetes.io/managed-by": "examlops",
    }


def _split(spec: dict[str, Any], ref: ResolvedRef) -> TrafficSplit | None:
    canary = (spec.get("rollout") or {}).get("canary")
    if not canary:
        return None
    return TrafficSplit(ref.version, str(canary["version"]), int(canary["percent"]))


def _ray_model_key(model: str) -> str:
    """Ray's ``MODEL_REGISTRY`` keys are upper-case (``JPCP``); MLflow names are lower-case."""
    return model.upper()


def _endpoint_payload(spec: dict[str, Any], ref: ResolvedRef) -> dict[str, Any]:
    """Everything a launcher needs to start the planned server — carried in the rendered object,
    so a real apply starts exactly what was planned and nothing re-reads the servable spec."""
    es = _endpoint_spec(spec, ref)
    return {
        "model": es.model,
        "hf_model_id": es.hf_model_id,
        "engine": spec.get("engine") or {},
        "nodes": es.nodes,
        "gpus": es.gpus,
        "project": es.project,
    }


def _spec_from_payload(payload: dict[str, Any]) -> Any:
    from examlops.engines.config import EngineConfig
    from examlops.llm_endpoints import EndpointSpec

    return EndpointSpec(
        model=payload["model"],
        hf_model_id=payload["hf_model_id"],
        config=EngineConfig.from_dict(payload["engine"]),
        project=payload["project"],
        nodes=int(payload["nodes"]),
        gpus=int(payload["gpus"]),
    )


def _record_started(es: Any, handle: Any) -> None:
    """The endpoint record `exa serve llm start` writes, so both paths leave the same trace."""
    from dataclasses import asdict

    from examlops.data.serving import upsert_llm_endpoint

    cfg = es.config
    upsert_llm_endpoint(
        es.model,
        hf_model_id=es.hf_model_id,
        engine=cfg.engine,
        base_url=handle.base_url,
        state=handle.state,
        launcher=handle.launcher,
        job_id=handle.job_id,
        project=es.project,
        modality=cfg.multimodal.modality,
        served_model_name=cfg.served_model_name,
        engine_config=asdict(cfg),
        max_model_len=cfg.max_model_len,
        tensor_parallel_size=cfg.tensor_parallel_size,
        dtype=cfg.dtype,
        gpus=es.gpus,
        nodes=es.nodes,
    )


def _endpoint_spec(spec: dict[str, Any], ref: ResolvedRef) -> Any:
    from examlops.engines.config import EngineConfig
    from examlops.llm_endpoints import EndpointSpec

    weights = (
        ref.artifact_uri[len("hf://") :]
        if ref.artifact_uri.startswith("hf://")
        else ref.artifact_uri
    )
    return EndpointSpec(
        model=ref.model,
        hf_model_id=weights,
        config=EngineConfig.from_dict(spec.get("engine") or {}),
        project=ref.project,
        nodes=int((spec.get("resources") or {}).get("nodes", 1)),
        gpus=int((spec.get("resources") or {}).get("gpus", 1)),
    )


# ── compose ───────────────────────────────────────────────────────────────────


class ComposeSubstrate:
    """Single-node Docker Compose: Ray ``MultiModelServer`` for predictive, ``vllm`` profile for LLMs."""

    name = "compose"

    def __init__(self, post: Any = None, set_traffic: Any = None, launcher: Any = None) -> None:
        # Injectable for tests; default to the Ray admin route, the traffic-rule store and the
        # ADR 0107 Compose launcher.
        self._post, self._set_traffic, self._launcher = post, set_traffic, launcher

    def capabilities(self) -> SubstrateCaps:
        return SubstrateCaps(
            kinds=frozenset({"predictive", "generative"}),
            canary=True,
            shadow=True,
            gpu=True,
            accelerators=frozenset({"cpu", "cuda-x86_64"}),
            multi_model_density=True,
            live_apply=True,
        )

    def render(self, spec: dict[str, Any], resolved: ResolvedRef) -> Rendered:
        require_caps(self.capabilities(), self.name, spec)
        split = _split(spec, resolved)
        if servable_kind(spec) == "predictive":
            obj: dict[str, Any] = {
                "kind": "RayServeServable",
                "labels": _labels(resolved),
                "model": _ray_model_key(resolved.model),
                "alias": resolved.alias,
                "version": resolved.version,
                "artifact_uri": resolved.artifact_uri,
            }
            if split and split.canary_version:
                canary_alias = (spec.get("rollout") or {}).get("canary", {}).get("alias", "Canary")
                obj["traffic"] = {
                    resolved.alias or "Production": 100 - split.canary_percent,
                    canary_alias: split.canary_percent,
                }
            warnings = [
                "the Ray server resolves the alias from MLflow itself until the serving snapshot "
                "(ADR 0127) exists; 'version' records what it resolved at render time"
            ]
            return make_rendered(self.name, [obj], warnings)
        from examlops.engines.config import to_vllm_args

        es = _endpoint_spec(spec, resolved)
        return make_rendered(
            self.name,
            [
                {
                    "kind": "ComposeVllmService",
                    "labels": _labels(resolved),
                    "env": {
                        "EXAMLOPS_VLLM_MODEL": es.hf_model_id,
                        "EXAMLOPS_VLLM_ARGS": " ".join(to_vllm_args(es.config)),
                    },
                    "endpoint": _endpoint_payload(spec, resolved),
                }
            ],
        )

    def apply(
        self, rendered: Rendered, *, dry_run: bool = True, plan_hash: str | None = None
    ) -> Any:
        def act() -> list[str]:
            refs: list[str] = []
            for obj in rendered.objects:
                if obj["kind"] == "ComposeVllmService":
                    es = _spec_from_payload(obj["endpoint"])
                    handle = self._compose_launcher().start(es)
                    _record_started(es, handle)
                    refs.append(f"compose:vllm:{es.model}")
                    continue
                if "traffic" in obj:
                    self._traffic()(obj["model"], obj["traffic"])
                    refs.append(f"traffic:{obj['model']}")
                self._reload()(obj["model"])
                refs.append(f"reload:{obj['model']}")
            return refs

        return audited_apply(self.name, rendered, dry_run=dry_run, plan_hash=plan_hash, act=act)

    def _compose_launcher(self) -> Any:
        if self._launcher is not None:
            return self._launcher
        from examlops.llm_endpoints import ComposeLauncher

        return ComposeLauncher()

    def _reload(self) -> Any:
        if self._post is not None:
            return self._post

        def reload(model: str) -> None:
            from examlops.cli import _client
            from examlops.cli._config import load_config

            cfg = load_config()
            _client.post(f"{cfg.ray_serve_url}/reload/{model}", {}, token=cfg.ray_serve_admin_token)

        return reload

    def _traffic(self) -> Any:
        if self._set_traffic is not None:
            return self._set_traffic

        def set_traffic(model: str, rules: dict[str, int]) -> None:
            from examlops.data.serving import set_traffic_rules

            set_traffic_rules(model, rules, os.getenv("EXAMLOPS_ACTOR") or "substrate")

        return set_traffic

    def status(self, name: str) -> SubstrateStatus:
        from examlops.data.serving import get_traffic_rules

        rules = get_traffic_rules(_ray_model_key(name)) or {"Production": 100}
        return SubstrateStatus(
            "UNKNOWN",
            {k: int(v) for k, v in rules.items()},
            None,
            {"note": "per-alias weights; health is `exa serve check`"},
        )

    def stop(self, name: str) -> None:
        raise SubstrateUnavailable(
            "a Ray-served model stops by moving its alias; the shared server is not stopped per model"
        )


# ── hpc ───────────────────────────────────────────────────────────────────────


class HpcSubstrate:
    """A long-lived ``vllm serve`` allocation on Slurm/Flux (ADR 0107 launcher, R-SUB-30)."""

    name = "hpc"

    def __init__(self, scheduler: str | None = None, launcher: Any = None) -> None:
        self.scheduler = scheduler or os.getenv("EXAMLOPS_HPC_SCHEDULER") or "mock"
        self._injected = launcher  # tests; default HpcLauncher(scheduler)

    def capabilities(self) -> SubstrateCaps:
        return SubstrateCaps(
            kinds=frozenset({"generative"}),
            multinode=True,
            gpu=True,
            accelerators=frozenset({"cuda-x86_64", "cuda-aarch64", "rocm", "cpu"}),
            live_apply=True,
        )

    def _launcher(self) -> Any:
        if self._injected is not None:
            return self._injected
        from examlops.llm_endpoints import HpcLauncher

        return HpcLauncher(scheduler=self.scheduler)

    def render(self, spec: dict[str, Any], resolved: ResolvedRef) -> Rendered:
        require_caps(self.capabilities(), self.name, spec)
        if _split(spec, resolved):
            raise CapabilityMissing(
                "canary", self.name, "needs two allocations behind a gateway route"
            )
        es = _endpoint_spec(spec, resolved)
        warnings = []
        if not resolved.artifact_uri.startswith("hf://"):
            warnings.append(
                f"the job must be able to read {resolved.artifact_uri} from the compute nodes"
            )
        script = self._launcher().render_script(es)
        return make_rendered(
            self.name,
            [
                {
                    "kind": "HpcServeJob",
                    "labels": _labels(resolved),
                    "scheduler": self.scheduler,
                    "script": script,
                    "endpoint": _endpoint_payload(spec, resolved),
                }
            ],
            warnings,
        )

    def apply(
        self, rendered: Rendered, *, dry_run: bool = True, plan_hash: str | None = None
    ) -> Any:
        def act() -> list[str]:
            refs = []
            launcher = self._launcher()
            for obj in rendered.objects:
                es = _spec_from_payload(obj["endpoint"])
                # Submit exactly the planned job: the script is re-rendered and must match.
                if launcher.render_script(es) != obj["script"]:
                    raise ApplyFailed("the job script changed since it was planned; re-plan")
                handle = launcher.start(es)
                _record_started(es, handle)
                refs.append(f"job:{self.scheduler}:{handle.job_id}")
            return refs

        return audited_apply(self.name, rendered, dry_run=dry_run, plan_hash=plan_hash, act=act)

    def status(self, name: str) -> SubstrateStatus:
        from examlops.data.serving import get_llm_endpoint

        rec = get_llm_endpoint(name) or {}
        return SubstrateStatus(str(rec.get("state") or "UNKNOWN"), {}, rec.get("base_url"), {})

    def stop(self, name: str) -> None:
        self._launcher().stop(name)


# ── kserve ────────────────────────────────────────────────────────────────────


class KServeSubstrate:
    """The pinned KServe release (ADR 0142 d2/d6): render + validate + a real, plan-gated apply."""

    name = "kserve"

    def __init__(self, kubectl: Any = None) -> None:
        self._injected = kubectl  # tests; default KubectlClient()

    def _kubectl(self) -> Any:
        if self._injected is not None:
            return self._injected
        from examlops.serving.substrates.kubectl_client import KubectlClient

        return KubectlClient()

    def capabilities(self) -> SubstrateCaps:
        return SubstrateCaps(
            kinds=frozenset({"predictive", "generative"}),
            canary=True,
            kv_routing=True,
            pd=True,
            multinode=True,
            gpu=True,
            accelerators=frozenset({"cuda-x86_64", "rocm", "cpu"}),
            oci_delivery=frozenset({"modelcar", "image-volume"}),
            live_apply=True,
        )

    def render(self, spec: dict[str, Any], resolved: ResolvedRef) -> Rendered:
        from examlops.serving.substrates import k8s_schema, kserve

        require_caps(self.capabilities(), self.name, spec)
        canary_ref: ResolvedRef | None = None
        canary_pct: int | None = None
        split = _split(spec, resolved)
        if split and split.canary_version:
            canary = spec["rollout"]["canary"]
            if not canary.get("artifact_uri"):
                raise RenderError(
                    "a KServe canary needs the canary version resolved (artifact_uri)"
                )
            canary_ref = ResolvedRef(
                resolved.model,
                split.canary_version,
                canary.get("alias", "Canary"),
                canary["artifact_uri"],
                canary.get("digest", "unsigned"),
                resolved.project,
            )
            canary_pct = split.canary_percent
        manifest = kserve.render(spec, resolved, canary=canary_ref, canary_pct=canary_pct)
        errors = k8s_schema.validate(manifest)
        if errors:
            raise RenderError(f"render failed the pinned KServe schema: {errors}")
        return make_rendered(self.name, [manifest])

    def apply(
        self, rendered: Rendered, *, dry_run: bool = True, plan_hash: str | None = None
    ) -> Any:
        def act() -> list[str]:
            return self._kubectl().apply(list(rendered.objects))

        return audited_apply(self.name, rendered, dry_run=dry_run, plan_hash=plan_hash, act=act)

    def status(self, name: str) -> SubstrateStatus:
        from examlops.serving.substrates.kubectl_client import status_from_object

        obj = self._kubectl().get_any_kind(name)
        if obj is None:
            return SubstrateStatus("UNKNOWN", {}, None, {"note": f"{name} not found in KServe"})
        state, url, detail = status_from_object(obj)
        version = (obj.get("metadata", {}).get("labels") or {}).get("examlops.io/version")
        return SubstrateStatus(state, {name: int(version)} if version else {}, url, detail)

    def stop(self, name: str) -> None:
        self._kubectl().delete_any_kind(name)


# ── k8s-agents ────────────────────────────────────────────────────────────────


class K8sAgentsSubstrate:
    """Agents on Kubernetes (runtime ``Deployment`` + ``agent-sandbox``) — USAR I6/I7, not built."""

    name = "k8s-agents"
    _WHY = "the agent runtime is not built yet (USAR I6, ADR 0144); nothing can be rendered here"

    def capabilities(self) -> SubstrateCaps:
        return SubstrateCaps(kinds=frozenset({"agentic"}), isolation="gvisor")

    def render(self, spec: dict[str, Any], resolved: ResolvedRef) -> Rendered:
        require_caps(self.capabilities(), self.name, spec)
        raise SubstrateUnavailable(self._WHY)

    def apply(
        self, rendered: Rendered, *, dry_run: bool = True, plan_hash: str | None = None
    ) -> Any:
        raise SubstrateUnavailable(self._WHY)

    def status(self, name: str) -> SubstrateStatus:
        raise SubstrateUnavailable(self._WHY)

    def stop(self, name: str) -> None:
        raise SubstrateUnavailable(self._WHY)


# ── external ──────────────────────────────────────────────────────────────────


class ExternalSubstrate:
    """A server someone else operates: the platform records its address and nothing more (R-SUB-32)."""

    name = "external"

    def capabilities(self) -> SubstrateCaps:
        return SubstrateCaps(kinds=frozenset({"predictive", "generative"}), live_apply=True)

    def render(self, spec: dict[str, Any], resolved: ResolvedRef) -> Rendered:
        require_caps(self.capabilities(), self.name, spec)
        base_url = (spec.get("substrate") or {}).get("base_url") or (spec.get("engine") or {}).get(
            "base_url"
        )
        if not base_url:
            raise RenderError("an external servable needs a base_url")
        return make_rendered(
            self.name,
            [
                {
                    "kind": "EndpointRecord",
                    "labels": _labels(resolved),
                    "model": resolved.model,
                    "base_url": str(base_url),
                    "weights": resolved.artifact_uri,
                }
            ],
        )

    def apply(
        self, rendered: Rendered, *, dry_run: bool = True, plan_hash: str | None = None
    ) -> Any:
        def act() -> list[str]:
            from examlops.data.serving import upsert_llm_endpoint

            refs = []
            for obj in rendered.objects:
                upsert_llm_endpoint(
                    obj["model"],
                    hf_model_id=obj["weights"],
                    base_url=obj["base_url"],
                    state="READY",
                    launcher="external",
                    project=obj["labels"]["examlops.io/project"],
                )
                refs.append(f"endpoint:{obj['model']}")
            return refs

        return audited_apply(self.name, rendered, dry_run=dry_run, plan_hash=plan_hash, act=act)

    def status(self, name: str) -> SubstrateStatus:
        from examlops.data.serving import get_llm_endpoint

        rec = get_llm_endpoint(name)
        if rec is None:
            return SubstrateStatus("STOPPED")
        return SubstrateStatus(str(rec.get("state") or "UNKNOWN"), {}, rec.get("base_url"), {})

    def stop(self, name: str) -> None:
        from examlops.data.serving import delete_llm_endpoint

        delete_llm_endpoint(name)


_CLASSES: dict[str, Any] = {
    "compose": ComposeSubstrate,
    "hpc": HpcSubstrate,
    "kserve": KServeSubstrate,
    "k8s-agents": K8sAgentsSubstrate,
    "external": ExternalSubstrate,
}


def names() -> tuple[str, ...]:
    return _NAMES


def get(name: str, **kwargs: Any) -> Any:
    """The substrate named ``name`` (``compose | hpc | kserve | k8s-agents | external``)."""
    if name not in _CLASSES:
        raise SubstrateUnavailable(f"unknown substrate {name!r} (known: {', '.join(_NAMES)})")
    return _CLASSES[name](**kwargs)

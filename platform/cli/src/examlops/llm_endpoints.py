"""Track V (R-V7) — ``EndpointLauncher``: start a vLLM server on any substrate.

The four substrates ExaMLOps targets — an externally-operated endpoint, a Docker Compose
GPU service, an HPC allocation (Slurm/Flux), and Kubernetes via KServe — differ in exactly
two things: **how a process is started** and **where its address comes from**. Everything
downstream (gateway → guardrails → media validation → engine → telemetry/FinOps/audit) is
identical. This module isolates that difference so the request path is written once.

That is the same reasoning behind the ``SchedulerAdapter`` (ADR 0075) and the calculation
providers (ADR 0074): name the substrate-specific step, share the rest.

Every launcher degrades gracefully — no Docker, no scheduler, no cluster ⇒ a clear
``LauncherUnavailable`` rather than a traceback, and the ``external`` launcher always works
because it starts nothing.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from examlops.engines.config import EngineConfig, to_vllm_args

_DEFAULT_IMAGE = os.getenv("EXAMLOPS_VLLM_IMAGE", "docker://vllm/vllm-openai:latest")
_DEFAULT_PORT = int(os.getenv("EXAMLOPS_VLLM_PORT", "8000"))
_RAY_PORT = int(os.getenv("EXAMLOPS_VLLM_RAY_PORT", "6379"))
_TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "slurm-adapter"
    / "templates"
    / "vllm_serve.sh.tmpl"
)


def _work_dir(spec: EndpointSpec) -> Path:
    """Where job scripts, the container image and the endpoint file live.

    Read at call time (not import) so a test or an operator can redirect it with
    ``EXAMLOPS_VLLM_WORK_DIR`` without reloading the module.
    """
    return Path(spec.work_dir or os.getenv("EXAMLOPS_VLLM_WORK_DIR") or "/tmp/examlops-vllm")


class LauncherError(RuntimeError):
    """A launcher could not complete the requested action."""


class LauncherUnavailable(LauncherError):
    """The substrate this launcher needs is not present on this host."""


@dataclass
class EndpointSpec:
    """What to serve, and with how much of the machine."""

    model: str
    hf_model_id: str
    config: EngineConfig = field(default_factory=EngineConfig)
    port: int = _DEFAULT_PORT
    nodes: int = 1
    gpus: int = 1
    cluster: str | None = None
    project: str | None = None
    partition: str | None = None
    walltime: str = "02:00:00"
    base_url: str | None = None  # external launcher only
    image: str = _DEFAULT_IMAGE
    work_dir: str | None = None

    @property
    def served_name(self) -> str:
        return self.config.served_model_name or self.model


@dataclass
class EndpointHandle:
    """The outcome of a start: where it is, and what identifies it on its substrate."""

    model: str
    launcher: str
    state: str  # PENDING | STARTING | READY | FAILED | STOPPED
    base_url: str | None = None
    job_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EndpointLauncher(Protocol):
    name: str

    def start(self, spec: EndpointSpec) -> EndpointHandle: ...
    def stop(self, model: str) -> dict[str, Any]: ...
    def status(self, model: str) -> dict[str, Any]: ...


# ── external ──────────────────────────────────────────────────────────────────


class ExternalLauncher:
    """Register a server somebody else operates. Starts nothing, so it always works.

    This is the default, and on a CPU-only host it is the only honest option: the platform
    can route to, meter, guard and audit a remote vLLM endpoint without pretending to own
    its lifecycle.
    """

    name = "external"

    def start(self, spec: EndpointSpec) -> EndpointHandle:
        base_url = spec.base_url or spec.config.base_url or os.getenv("EXAMLOPS_VLLM_BASE_URL")
        if not base_url:
            raise LauncherError(
                "the 'external' launcher needs an endpoint URL: pass --base-url, set "
                "engine.base_url in the model YAML, or export EXAMLOPS_VLLM_BASE_URL"
            )
        return EndpointHandle(
            model=spec.model, launcher=self.name, state="READY", base_url=base_url
        )

    def stop(self, model: str) -> dict[str, Any]:
        # Deregistering is all we may do — the process is not ours to kill.
        return {"launcher": self.name, "model": model, "stopped": False, "deregistered": True}

    def status(self, model: str) -> dict[str, Any]:
        return {"launcher": self.name, "model": model, "managed": False}


# ── docker compose ────────────────────────────────────────────────────────────


class ComposeLauncher:
    """Bring up the ``vllm`` Compose service (GPU profile) next to the rest of the stack."""

    name = "compose"

    def __init__(self, compose_file: str | None = None) -> None:
        self.compose_file = compose_file or str(
            Path(__file__).resolve().parents[3] / "infra" / "docker-compose" / "docker-compose.yml"
        )

    def _cmd(self, *args: str) -> list[str]:
        if not shutil.which("docker"):
            raise LauncherUnavailable(
                "docker is not on PATH — use --launcher external and point at a running "
                "endpoint, or install Docker to manage one locally"
            )
        return ["docker", "compose", "-f", self.compose_file, "--profile", "vllm", *args]

    def start(self, spec: EndpointSpec) -> EndpointHandle:
        env = os.environ.copy()
        env["EXAMLOPS_VLLM_MODEL"] = spec.hf_model_id
        env["EXAMLOPS_VLLM_ARGS"] = " ".join(to_vllm_args(spec.config))
        env["EXAMLOPS_VLLM_PORT"] = str(spec.port)
        try:
            subprocess.run(  # noqa: S603
                self._cmd("up", "-d", "vllm"), check=True, capture_output=True, env=env, timeout=300
            )
        except subprocess.CalledProcessError as exc:
            raise LauncherError(f"docker compose up failed: {exc.stderr.decode()[:400]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LauncherError("docker compose up timed out after 300s") from exc
        host_port = int(os.getenv("EXAMLOPS_VLLM_HOST_PORT", "18011"))
        return EndpointHandle(
            model=spec.model,
            launcher=self.name,
            state="STARTING",  # the model still has to load; `health` decides READY
            base_url=f"http://localhost:{host_port}",
        )

    def stop(self, model: str) -> dict[str, Any]:
        try:
            subprocess.run(  # noqa: S603
                self._cmd("stop", "vllm"), check=True, capture_output=True, timeout=120
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise LauncherError(f"docker compose stop failed: {exc}") from exc
        return {"launcher": self.name, "model": model, "stopped": True}

    def status(self, model: str) -> dict[str, Any]:
        try:
            out = subprocess.run(  # noqa: S603
                self._cmd("ps", "--format", "json"), capture_output=True, timeout=30, check=False
            )
            return {"launcher": self.name, "model": model, "ps": out.stdout.decode()[:2000]}
        except Exception as exc:
            return {"launcher": self.name, "model": model, "error": str(exc)}


# ── HPC (Slurm / Flux) ────────────────────────────────────────────────────────


class HpcLauncher:
    """Submit ``vllm serve`` as a scheduler job — closes ADR 0096 R-A6 / increment A3.

    Tensor-parallel within a node, pipeline-parallel across nodes, over a Ray cluster the
    job brings up itself. The job writes its own endpoint URL to a file the moment the head
    node is known, which is more reliable than scraping ``squeue`` for a nodelist and
    guessing the interface.
    """

    name = "slurm"

    def __init__(self, scheduler: str | None = None) -> None:
        self.scheduler: str = scheduler or os.getenv("EXAMLOPS_HPC_SCHEDULER") or "mock"
        self.name = self.scheduler

    def render_script(self, spec: EndpointSpec) -> str:
        """Render the job script. Pure — testable with no scheduler present."""
        if not _TEMPLATE.exists():  # pragma: no cover - packaging error
            raise LauncherError(f"job template missing: {_TEMPLATE}")
        work = _work_dir(spec)
        sif = work / (spec.image.rsplit("/", 1)[-1].replace(":", "_") + ".sif")
        modules = os.getenv("EXAMLOPS_VLLM_MODULES", "")
        module_loads = (
            "\n".join(f"module load {m}" for m in modules.split(",") if m.strip())
            or "# no modules configured (EXAMLOPS_VLLM_MODULES)"
        )
        # Every value below lands in a bash assignment or argv the job actually runs — quoted
        # here, not by the template (see the template's own header comment), so a model id or
        # image ref carrying shell metacharacters can never break out or execute. VLLM_ARGS is
        # a sequence of separate argv tokens (flag, value, flag, value, …), so it is joined with
        # shlex.join rather than quoted as one string, which would collapse it into a single arg.
        replacements = {
            "@@MODEL@@": shlex.quote(spec.hf_model_id),
            "@@IMAGE@@": shlex.quote(spec.image),
            "@@SIF_PATH@@": shlex.quote(str(sif)),
            "@@PORT@@": shlex.quote(str(spec.port)),
            "@@NODES@@": shlex.quote(str(spec.nodes)),
            "@@ENDPOINT_FILE@@": shlex.quote(str(self._endpoint_file(spec))),
            "@@RAY_PORT@@": shlex.quote(str(_RAY_PORT)),
            "@@GPUS_PER_NODE@@": shlex.quote(str(int(spec.gpus or 0))),
            "@@MODULE_LOADS@@": module_loads,
            "@@VLLM_ARGS@@": shlex.join(to_vllm_args(spec.config)),
        }
        script = _TEMPLATE.read_text()
        for key, value in replacements.items():
            script = script.replace(key, value)
        return script

    def _endpoint_file(self, spec: EndpointSpec) -> Path:
        return _work_dir(spec) / f"{spec.model.lower()}.endpoint"

    def start(self, spec: EndpointSpec) -> EndpointHandle:
        work = _work_dir(spec)
        work.mkdir(parents=True, exist_ok=True)
        script_path = work / f"vllm_serve_{spec.model.lower()}.sh"
        script_path.write_text(self.render_script(spec))
        script_path.chmod(0o755)

        resources: dict[str, Any] = {
            "nodes": spec.nodes,
            # `--gpus` is GPUs per node on this command, but sbatch reads --gpus as the total
            # for the whole job: `--nodes 2 --gpus 4` would allocate 4 GPUs for a server that
            # needs 8. Slurm takes the per-node count under its own flag.
            ("gpus_per_node" if self.scheduler == "slurm" else "gpus"): spec.gpus,
            "time": spec.walltime,
            "job_name": f"vllm-{spec.model.lower()}",
        }
        if spec.partition:
            resources["partition"] = spec.partition
        # A previous job's endpoint file would otherwise be read as this job's address, and a
        # recorded address is never re-read — the endpoint would point at a dead node for good.
        self._endpoint_file(spec).unlink(missing_ok=True)
        try:
            adapter = _scheduler_adapter(self.scheduler)
            _remove_remote(adapter, self._endpoint_file(spec))
            # `remote_dir` makes the adapter stage the script through its transport, so the
            # SSH path submits a file that exists on the login node. One directory per
            # endpoint: two endpoints started together must not overwrite each other's run.sh.
            job_id = adapter.submit_job(
                script_path=str(script_path),
                resources=resources,
                remote_dir=str(work / spec.model.lower()),
            )
        except Exception as exc:
            raise LauncherError(f"job submission failed: {exc}") from exc

        _record_serve_job(str(job_id), self.scheduler, spec)
        return EndpointHandle(
            model=spec.model,
            launcher=self.name,
            state="STARTING",  # queued; resolve_endpoint() finds the URL once it runs
            job_id=str(job_id),
            detail={"script": str(script_path), "endpoint_file": str(self._endpoint_file(spec))},
        )

    def resolve_endpoint(self, spec: EndpointSpec, *, timeout_s: float = 0.0) -> str | None:
        """Read the URL the running job published; optionally wait for it to appear."""
        path = self._endpoint_file(spec)
        deadline = time.monotonic() + timeout_s
        while True:
            if path.exists():
                url = path.read_text().strip()
                if url:
                    return url
            if time.monotonic() >= deadline:
                return None
            time.sleep(2)

    def published_url(self, model: str, *, fetch: bool = True) -> str | None:
        """The URL a running job published for ``model``, or ``None`` if it has not yet.

        Read locally first — the login-node/shared-filesystem case. Otherwise the job ran
        behind the SSH transport and wrote the file on the cluster, so fetch it through the
        same executor that staged the script (skipped when ``fetch`` is False). Either way the
        file lives under ``EXAMLOPS_VLLM_WORK_DIR``, which on a real cluster must be a
        filesystem the compute nodes share with the login node: node-local ``/tmp`` is
        invisible from anywhere else.
        """
        spec = EndpointSpec(model=model, hf_model_id=model)
        url = self.resolve_endpoint(spec)
        if url or not fetch:
            return url
        path = str(self._endpoint_file(spec))
        try:
            executor = getattr(_scheduler_adapter(self.scheduler), "executor", None)
            if executor is None:
                return None
            executor.get(path, path)
        except Exception:
            return None
        return self.resolve_endpoint(spec)

    def stop(self, model: str) -> dict[str, Any]:
        from examlops.data.serving import get_llm_endpoint

        rec = get_llm_endpoint(model) or {}
        job_id = rec.get("job_id")
        if not job_id:
            raise LauncherError(f"no scheduler job recorded for '{model}'")
        try:
            adapter = _scheduler_adapter(self.scheduler)
        except Exception as exc:
            raise LauncherError(f"{self.scheduler} scheduler unavailable: {exc}") from exc
        cancel = getattr(adapter, "cancel_job", None)
        if not callable(cancel):  # pragma: no cover - adapter-dependent
            raise LauncherError(f"{self.scheduler} adapter cannot cancel jobs")
        try:
            cancel(str(job_id))
        except Exception as exc:
            raise LauncherError(f"cancelling job {job_id} failed: {exc}") from exc
        endpoint_file = self._endpoint_file(EndpointSpec(model=model, hf_model_id=model))
        endpoint_file.unlink(missing_ok=True)
        _remove_remote(adapter, endpoint_file)
        return {"launcher": self.name, "model": model, "job_id": job_id, "stopped": True}

    def status(self, model: str) -> dict[str, Any]:
        from examlops.data.serving import get_llm_endpoint

        rec = get_llm_endpoint(model) or {}
        job_id = rec.get("job_id")
        if not job_id:
            return {"launcher": self.name, "model": model, "job": None}
        try:
            return {
                "launcher": self.name,
                "model": model,
                "job": _scheduler_adapter(self.scheduler).get_job_status(str(job_id)),
            }
        except Exception as exc:
            return {"launcher": self.name, "model": model, "job_id": job_id, "error": str(exc)}


def _remove_remote(adapter: Any, path: Path) -> None:
    """Best-effort delete of ``path`` through the adapter's transport (the SSH side's copy)."""
    executor = getattr(adapter, "executor", None)
    if executor is None:
        return
    try:
        executor.run(["rm", "-f", str(path)])
    except Exception:
        pass  # a stale remote file is re-read only if the local copy is missing too


def _scheduler_adapter(scheduler: str):
    """Load the adapter for ``scheduler``, keeping its sys.path shim out of import time.

    The scheduler is passed explicitly rather than read from ``EXAMLOPS_HPC_SCHEDULER``: the
    launcher was *named* slurm or flux by the operator, and that process-wide variable
    defaults to ``mock`` — whose adapter accepts the script and returns an id for a job
    that never runs. The adapter announces its backend on stdout; that goes to stderr here
    so it cannot corrupt ``--json`` output.
    """
    import contextlib
    import sys

    root = Path(__file__).resolve().parents[3] / "infra" / "slurm-adapter"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from adapter import get_scheduler_adapter  # type: ignore
    except ImportError as exc:  # pragma: no cover - packaging error
        raise LauncherUnavailable(f"HPC scheduler adapter not importable: {exc}") from exc
    with contextlib.redirect_stdout(sys.stderr):
        return get_scheduler_adapter(scheduler=scheduler)


def resolve_address(rec: dict[str, Any], *, fetch: bool = True) -> str | None:
    """Where a registered endpoint answers, or ``None`` if nobody knows yet.

    The recorded URL when there is one. A Slurm/Flux endpoint is registered before the
    scheduler has placed it, so it starts with no URL (ADR 0107 clause 3: the launcher
    "resolves the allocated node into a base_url"); its job writes the address to the
    endpoint file once the head node is known. Before this, nothing read that file, so an
    HPC endpoint stayed addressless for ever and `health`, `chat` and the gateway all
    refused it. The first reader to find the URL records it, so later readers — health,
    status, the gateway — agree on one address without asking the scheduler again.

    ``fetch=False`` reads only a file visible on this host and never builds a scheduler
    adapter. The gateway uses it: it resolves every endpoint on every router build, and an
    SSH connection per addressless endpoint per chat request is a cost an operator command
    (`exa serve llm health`) should pay once, not every caller.
    """
    url = rec.get("base_url") or (rec.get("engine_config") or {}).get("base_url")
    if url:
        return str(url)
    launcher = str(rec.get("launcher") or "").lower()
    if launcher not in ("slurm", "flux") or rec.get("state") == "STOPPED":
        return None
    try:
        url = HpcLauncher(scheduler=launcher).published_url(str(rec["model"]), fetch=fetch)
    except Exception:
        return None
    if url:
        try:
            from examlops.data.serving import set_llm_endpoint_state

            set_llm_endpoint_state(str(rec["model"]), rec.get("state") or "STARTING", base_url=url)
        except Exception:
            pass  # the URL is still right; failing to cache it only costs a re-read
    return url


def _record_serve_job(job_id: str, scheduler: str, spec: EndpointSpec) -> None:
    """Record the allocation in ``hpc_jobs`` with ``kind='serve'``.

    The ``kind`` discriminator matters: ``hpc_poll.poll_until_complete`` waits for a
    terminal state, which a healthy server never reaches. Tagging the row keeps a serving
    allocation visible to `exa hpc jobs` and FinOps without letting the training poller
    adopt and eventually reap it. ``dataset`` is NOT NULL and meaningless here, so it holds
    the ``"-"`` sentinel.
    """
    try:
        from examlops.data import get_db, init_db

        init_db()
        with get_db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO hpc_jobs
                       (job_id, scheduler, model, dataset, state, nodes, gpus, kind)
                   VALUES (?,?,?,?,?,?,?,'serve')""",
                (str(job_id), scheduler, spec.model, "-", "SUBMITTED", spec.nodes, spec.gpus),
            )
    except Exception:
        # Bookkeeping must never sink a successful submission.
        pass


# ── KServe ────────────────────────────────────────────────────────────────────


def _kserve_object_name(model: str) -> str:
    """The Kubernetes object name a rendered manifest actually gets (ADR 0142 ``_metadata``)."""
    from examlops.serving.substrates.kserve import service_name

    return service_name(model)


class KServeLauncher:
    """Render an ``LLMInferenceService`` via the substrate seam and apply it for real.

    ``exa serve llm start --launcher kserve`` already gates every launcher's mutating action
    behind its own ``_output.confirm()`` — the same operator approval a plan-gated apply needs —
    so render and apply happen in one call: the ``plan_hash`` is the render's own content hash,
    never a value the operator types. This closes ADR 0107 clause 3 (``KServeLauncher.start``
    used to always dry-run) by cutting over to ``KServeSubstrate`` (ADR 0142 d1/d6), which
    performs a genuine, audited Server-Side Apply and is verified against a real cluster
    (``tests/integration/test_kserve_live_apply_kind_live.py``).
    """

    name = "kserve"

    def __init__(self, kubectl: Any = None) -> None:
        self._kubectl = kubectl  # tests inject a fake; default resolved by KServeSubstrate

    def _substrate(self) -> Any:
        from examlops.serving.substrates.registry import KServeSubstrate

        return KServeSubstrate(kubectl=self._kubectl)

    def start(self, spec: EndpointSpec) -> EndpointHandle:
        from examlops.serving.substrates.base import RenderError, SubstrateError
        from examlops.serving.substrates.resolve import ResolvedRef, storage_uri

        model_yaml = {
            "name": spec.model,
            "task_type": "text-generation",
            "engine": _engine_block(spec.config),
        }
        weights = spec.hf_model_id if ":" in spec.hf_model_id else f"hf://{spec.hf_model_id}"
        substrate = self._substrate()
        try:
            ref = ResolvedRef(
                model=spec.model.lower(),
                # An HF id carries no revision here, so the object says so instead of a number.
                version="unpinned",
                alias=None,
                artifact_uri=storage_uri(weights),
                project=spec.project or "default",
            )
            rendered = substrate.render(model_yaml, ref)
        except RenderError as exc:
            raise LauncherError(f"cannot render KServe manifest for {spec.model}: {exc}") from exc
        try:
            result = substrate.apply(rendered, dry_run=False, plan_hash=rendered.content_hash)
        except SubstrateError as exc:
            raise LauncherError(f"KServe apply failed for {spec.model}: {exc}") from exc
        live = substrate.status(_kserve_object_name(spec.model))
        return EndpointHandle(
            model=spec.model,
            launcher=self.name,
            state=live.state,
            base_url=live.address or os.getenv("EXAMLOPS_KSERVE_GATEWAY_URL"),
            detail={
                "applied": True,
                "applied_refs": list(result.applied),
                "manifest": dict(rendered.objects[0]),
            },
        )

    def stop(self, model: str) -> dict[str, Any]:
        self._substrate().stop(_kserve_object_name(model))
        return {"launcher": self.name, "model": model, "stopped": True}

    def status(self, model: str) -> dict[str, Any]:
        from dataclasses import asdict

        live = self._substrate().status(_kserve_object_name(model))
        return {"launcher": self.name, "model": model, **asdict(live)}


def _engine_block(config: EngineConfig) -> dict[str, Any]:
    """The per-model ``engine:`` mapping equivalent of an :class:`EngineConfig`."""
    from dataclasses import asdict

    return asdict(config)


# ── selection ─────────────────────────────────────────────────────────────────

_LAUNCHERS: dict[str, Any] = {
    "external": ExternalLauncher,
    "compose": ComposeLauncher,
    "slurm": HpcLauncher,
    "flux": HpcLauncher,
    "kserve": KServeLauncher,
}

LAUNCHER_NAMES = tuple(_LAUNCHERS)


def select_launcher(name: str | None = None) -> EndpointLauncher:
    """Resolve a launcher by name, then ``EXAMLOPS_LLM_LAUNCHER``, else ``external``."""
    chosen = (name or os.getenv("EXAMLOPS_LLM_LAUNCHER") or "external").lower()
    cls = _LAUNCHERS.get(chosen)
    if cls is None:
        raise LauncherError(f"unknown launcher '{chosen}' (expected one of {LAUNCHER_NAMES})")
    if cls is HpcLauncher:
        return HpcLauncher(scheduler=chosen)
    return cls()

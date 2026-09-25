"""Per-session code/shell sandboxes behind one seam (ADR 0145 decision 6).

::

    SandboxProvider
      name, substrate
      capabilities() -> SandboxCapabilities    # isolation: vm | gvisor | container | container-weak | none
      claim(session, template, egress) -> SandboxHandle
      exec(handle, command, limits) -> dict
      release(handle, mode: hibernate | destroy)

| Substrate    | Provider                                   | Isolation advertised        |
|--------------|--------------------------------------------|-----------------------------|
| ``k8s-agents`` | ``agent-sandbox`` ``SandboxClaim`` (v1beta1) | ``gvisor`` / ``vm`` (Kata)  |
| ``hpc``      | Apptainer ``--containall --net --network none`` | ``container``            |
| ``compose``  | Docker, ``--runtime=runsc`` when installed | ``gvisor`` / ``container-weak`` |

**Advertised isolation is measured, never assumed.** The Docker provider asks the daemon which
runtimes it has; without ``runsc`` it says ``container-weak``, and :func:`select_provider`
refuses a tenant or agent whose policy requires ``gvisor`` or ``vm`` rather than quietly running
the code in a weaker box (ADR 0145 verification 6).

**Egress is default-deny.** Every provider starts the sandbox with no network. A non-empty
per-agent-version allow-list needs an operator-configured egress proxy
(``EXAMLOPS_SANDBOX_EGRESS_PROXY``); without one, claiming a sandbox that asks for egress is
refused - an allow-list nothing enforces would be a false statement. With one, what is enforced
is **the proxy's own policy**: the per-version host list is not transmitted to the proxy, so the
operator's proxy policy must be at least as strict as every version's list, and the Docker proxy
network (``EXAMLOPS_SANDBOX_PROXY_NETWORK``, else ``none``) must be an ``internal`` network whose
only way out is the proxy. Per-version enforcement at the proxy is not built.

Sandboxes hold no platform credentials: the environment is cleared (``--cleanenv`` /
``--env`` only for the proxy) and nothing from the runtime process is passed in.

Every provider shells out through an injectable ``runner`` (default ``subprocess.run``) with a
timeout and an output cap, so tests drive the real argument construction with a faithful fake.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ISOLATION_RANK",
    "AgentSandboxK8sProvider",
    "ApptainerProvider",
    "DockerProvider",
    "NoSandbox",
    "SandboxCapabilities",
    "SandboxHandle",
    "SandboxProvider",
    "SandboxRefused",
    "select_provider",
]

#: Strongest first. ``container`` = namespaced, unprivileged (Apptainer); ``container-weak`` =
#: plain Docker without ``runsc``; ``none`` = no sandbox at all.
ISOLATION_RANK = {"vm": 4, "gvisor": 3, "container": 2, "container-weak": 1, "none": 0}
_MAX_OUTPUT = 64 * 1024
_DEFAULT_TIMEOUT = 60.0
_SESSION = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DEFAULT_TEMPLATES = {"python-3.12-min": "python:3.12-slim"}

Runner = Callable[..., subprocess.CompletedProcess[str]]


class SandboxRefused(RuntimeError):
    """A sandbox cannot be provided as asked; ``code`` is stable."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


@dataclass(frozen=True)
class SandboxCapabilities:
    isolation: str
    gpu: bool = False
    hibernate: bool = False
    warm_pool: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "isolation": self.isolation,
            "gpu": self.gpu,
            "hibernate": self.hibernate,
            "warm_pool": self.warm_pool,
        }


@dataclass
class SandboxHandle:
    provider: str
    session: str
    ref: str
    template: str
    isolation: str
    egress: tuple[str, ...] = ()
    state: str = "running"
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SandboxProvider(Protocol):
    name: str
    substrate: str

    def capabilities(self) -> SandboxCapabilities: ...

    def claim(self, session: str, template: str, egress: Sequence[str] = ()) -> SandboxHandle: ...

    def exec(
        self, handle: SandboxHandle, command: str, *, timeout: float | None = None
    ) -> dict[str, Any]: ...

    def release(self, handle: SandboxHandle, mode: str = "destroy") -> None: ...


def _run(runner: Runner, argv: list[str], *, timeout: float, stdin: str | None = None) -> Any:
    try:
        return runner(
            argv, capture_output=True, text=True, timeout=timeout, input=stdin, check=False
        )
    except FileNotFoundError as exc:
        raise SandboxRefused("sandbox_unavailable", f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxRefused("sandbox_timeout", f"{argv[0]} did not answer in {timeout}s") from exc


def _cap(text: str | None) -> tuple[str, bool]:
    text = text or ""
    return (text[:_MAX_OUTPUT], len(text) > _MAX_OUTPUT)


def _result(cp: Any) -> dict[str, Any]:
    out, t1 = _cap(cp.stdout)
    err, t2 = _cap(cp.stderr)
    return {
        "ok": cp.returncode == 0,
        "exit_code": cp.returncode,
        "stdout": out,
        "stderr": err,
        "truncated": t1 or t2,
    }


def _check_session(session: str) -> None:
    if not _SESSION.match(session):
        raise SandboxRefused("bad_session", "session id may use only [A-Za-z0-9._-], <=128 chars")


def _templates() -> dict[str, str]:
    raw = os.getenv("EXAMLOPS_SANDBOX_TEMPLATES", "").strip()
    if not raw:
        return dict(_DEFAULT_TEMPLATES)
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise SandboxRefused(
            "bad_templates", f"EXAMLOPS_SANDBOX_TEMPLATES is not JSON: {exc}"
        ) from exc
    if not isinstance(doc, dict) or not all(isinstance(v, str) for v in doc.values()):
        raise SandboxRefused("bad_templates", "EXAMLOPS_SANDBOX_TEMPLATES must map name -> image")
    return doc


def _image(template: str) -> str:
    img = _templates().get(template)
    if img is None:
        raise SandboxRefused("unknown_template", f"no sandbox template named {template!r}")
    return img


def _egress_env(egress: Sequence[str]) -> dict[str, str]:
    """Default-deny: an allow-list is honoured only through a configured egress proxy."""
    if not egress:
        return {}
    proxy = os.getenv("EXAMLOPS_SANDBOX_EGRESS_PROXY", "").strip()
    if not proxy:
        raise SandboxRefused(
            "egress_unenforceable",
            "the agent version declares a sandbox egress allow-list but no egress proxy is "
            "configured (EXAMLOPS_SANDBOX_EGRESS_PROXY); refusing rather than running with an "
            "allow-list nothing enforces",
        )
    return {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "NO_PROXY": ""}


class DockerProvider:
    """Compose / dev substrate: one container per session, ``runsc`` when the daemon has it."""

    name = "docker"
    substrate = "compose"

    def __init__(self, *, runner: Runner = subprocess.run, docker: str = "docker") -> None:
        self.runner = runner
        self.docker = docker
        self._caps: SandboxCapabilities | None = None

    def capabilities(self) -> SandboxCapabilities:
        if self._caps is None:
            iso = "container-weak"
            try:
                cp = _run(
                    self.runner,
                    [self.docker, "info", "--format", "{{json .Runtimes}}"],
                    timeout=10,
                )
                runtimes = json.loads(cp.stdout or "{}") if cp.returncode == 0 else {}
                if isinstance(runtimes, dict) and "runsc" in runtimes:
                    iso = "gvisor"
            except (SandboxRefused, ValueError):
                iso = "none"  # no reachable daemon: nothing is isolated, and we say so
            self._caps = SandboxCapabilities(isolation=iso, hibernate=True)
        return self._caps

    def claim(self, session: str, template: str, egress: Sequence[str] = ()) -> SandboxHandle:
        _check_session(session)
        caps = self.capabilities()
        if caps.isolation == "none":
            raise SandboxRefused("sandbox_unavailable", "no reachable Docker daemon")
        env = _egress_env(egress)
        argv = [
            self.docker,
            "run",
            "-d",
            "--name",
            f"examlops-sbx-{session}-{uuid.uuid4().hex[:8]}",
        ]
        if caps.isolation == "gvisor":
            argv += ["--runtime=runsc"]
        # Default-deny egress: no network at all, unless a proxy network is configured.
        net = os.getenv("EXAMLOPS_SANDBOX_PROXY_NETWORK", "").strip() if env else ""
        argv += ["--network", net or "none"]
        argv += [
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            os.getenv("EXAMLOPS_SANDBOX_MEMORY", "1g"),
            "--cpus",
            os.getenv("EXAMLOPS_SANDBOX_CPUS", "1"),
            "--label",
            f"examlops.session={session}",
        ]
        for k, v in env.items():
            argv += ["--env", f"{k}={v}"]
        argv += [_image(template), "sleep", "infinity"]
        cp = _run(self.runner, argv, timeout=120)
        if cp.returncode != 0:
            raise SandboxRefused("claim_failed", (cp.stderr or "docker run failed")[:500])
        return SandboxHandle(
            self.name,
            session,
            (cp.stdout or "").strip(),
            template,
            caps.isolation,
            tuple(egress),
        )

    def exec(
        self, handle: SandboxHandle, command: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if handle.state != "running":
            raise SandboxRefused("sandbox_released", f"sandbox is {handle.state}")
        t = float(timeout or _DEFAULT_TIMEOUT)
        return _result(
            _run(self.runner, [self.docker, "exec", handle.ref, "sh", "-c", command], timeout=t)
        )

    def release(self, handle: SandboxHandle, mode: str = "destroy") -> None:
        if mode == "hibernate":
            _run(self.runner, [self.docker, "pause", handle.ref], timeout=30)
            handle.state = "hibernated"
        else:
            _run(self.runner, [self.docker, "rm", "-f", handle.ref], timeout=30)
            handle.state = "destroyed"

    def resume(self, handle: SandboxHandle) -> None:
        _run(self.runner, [self.docker, "unpause", handle.ref], timeout=30)
        handle.state = "running"


class ApptainerProvider:
    """HPC substrate: an Apptainer ``exec`` per call, fully contained, no network namespace
    egress. Whether unprivileged network namespaces are allowed is a site setting - verified per
    cluster, which is why the flag set is fixed here and the failure is reported, not hidden."""

    name = "apptainer"
    substrate = "hpc"

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        apptainer: str = "apptainer",
        workdir_root: str | None = None,
    ) -> None:
        self.runner = runner
        self.apptainer = apptainer
        self.workdir_root: str = workdir_root or os.environ.get(
            "EXAMLOPS_SANDBOX_WORKDIR", "/tmp/examlops-sbx"
        )

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(isolation="container", hibernate=True)

    def claim(self, session: str, template: str, egress: Sequence[str] = ()) -> SandboxHandle:
        _check_session(session)
        if egress:
            # A per-host allow-list needs a proxy namespace the site provides; not built.
            raise SandboxRefused(
                "egress_unenforceable",
                "the hpc sandbox has no network namespace for an egress allow-list",
            )
        work = os.path.join(self.workdir_root, session)
        return SandboxHandle(
            self.name, session, work, template, "container", (), extra={"image": _image(template)}
        )

    def exec(
        self, handle: SandboxHandle, command: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if handle.state == "destroyed":
            raise SandboxRefused("sandbox_released", "sandbox is destroyed")
        os.makedirs(handle.ref, mode=0o700, exist_ok=True)
        argv = [
            self.apptainer,
            "exec",
            "--containall",
            "--cleanenv",
            "--net",
            "--network",
            "none",
            "--bind",
            f"{handle.ref}:/work",
            "--pwd",
            "/work",
            str(handle.extra["image"]),
            "sh",
            "-c",
            command,
        ]
        handle.state = "running"
        return _result(_run(self.runner, argv, timeout=float(timeout or _DEFAULT_TIMEOUT)))

    def release(self, handle: SandboxHandle, mode: str = "destroy") -> None:
        if mode == "hibernate":
            handle.state = "hibernated"  # nothing runs between execs; the workdir persists
            return
        import shutil

        shutil.rmtree(handle.ref, ignore_errors=True)
        handle.state = "destroyed"


class AgentSandboxK8sProvider:
    """``k8s-agents`` substrate: a ``SandboxClaim`` against a ``SandboxWarmPool``
    (``agents.x-k8s.io/v1beta1``, pinned and treated as beta), RuntimeClass gVisor by default,
    Kata (``vm``) for a GPU template. Applied through ``kubectl`` with the injected runner."""

    name = "agent-sandbox"
    substrate = "k8s-agents"
    API_VERSION = "agents.x-k8s.io/v1beta1"

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        kubectl: str = "kubectl",
        namespace: str | None = None,
        gpu_templates: Sequence[str] = (),
    ) -> None:
        self.runner = runner
        self.kubectl = kubectl
        self.namespace: str = namespace or os.environ.get(
            "EXAMLOPS_SANDBOX_NAMESPACE", "examlops-agents"
        )
        self.gpu_templates = set(gpu_templates)

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            isolation="gvisor", gpu=bool(self.gpu_templates), hibernate=True, warm_pool=True
        )

    def claim_manifest(self, session: str, template: str, name: str) -> dict[str, Any]:
        gpu = template in self.gpu_templates
        return {
            "apiVersion": self.API_VERSION,
            "kind": "SandboxClaim",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": {"examlops.io/session": session, "examlops.io/template": template},
            },
            "spec": {
                "sandboxTemplateRef": {"name": template},
                "warmPoolRef": {"name": f"{template}-pool"},
                "runtimeClassName": "kata" if gpu else "gvisor",
                # Default-deny egress; the per-version allow-list is a NetworkPolicy the
                # operator's template attaches, never something the agent asks for itself.
                "networkPolicy": {"egress": "deny"},
            },
        }

    def claim(self, session: str, template: str, egress: Sequence[str] = ()) -> SandboxHandle:
        _check_session(session)
        if egress and not os.getenv("EXAMLOPS_SANDBOX_EGRESS_PROXY", "").strip():
            raise SandboxRefused(
                "egress_unenforceable",
                "egress allow-list requested but no egress proxy is configured",
            )
        name = f"sbx-{session.lower()[:40]}-{uuid.uuid4().hex[:6]}".replace("_", "-").replace(
            ".", "-"
        )
        doc = self.claim_manifest(session, template, name)
        cp = _run(
            self.runner, [self.kubectl, "apply", "-f", "-"], timeout=60, stdin=json.dumps(doc)
        )
        if cp.returncode != 0:
            raise SandboxRefused("claim_failed", (cp.stderr or "kubectl apply failed")[:500])
        iso = "vm" if template in self.gpu_templates else "gvisor"
        return SandboxHandle(self.name, session, name, template, iso, tuple(egress))

    def exec(
        self, handle: SandboxHandle, command: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if handle.state != "running":
            raise SandboxRefused("sandbox_released", f"sandbox is {handle.state}")
        argv = [
            self.kubectl,
            "exec",
            "-n",
            self.namespace,
            f"sandbox/{handle.ref}",
            "--",
            "sh",
            "-c",
            command,
        ]
        return _result(_run(self.runner, argv, timeout=float(timeout or _DEFAULT_TIMEOUT)))

    def release(self, handle: SandboxHandle, mode: str = "destroy") -> None:
        if mode == "hibernate":
            patch = json.dumps({"spec": {"replicas": 0}})
            _run(
                self.runner,
                [
                    self.kubectl,
                    "patch",
                    "-n",
                    self.namespace,
                    "sandboxclaim",
                    handle.ref,
                    "--type",
                    "merge",
                    "-p",
                    patch,
                ],
                timeout=30,
            )
            handle.state = "hibernated"
        else:
            _run(
                self.runner,
                [self.kubectl, "delete", "-n", self.namespace, "sandboxclaim", handle.ref],
                timeout=30,
            )
            handle.state = "destroyed"


class NoSandbox:
    """No isolation at all. Refuses every claim: running agent code in the runtime's own
    process is rejected by the ADR, so this exists only to say *there is no sandbox here*."""

    name = "none"
    substrate = "none"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(isolation="none")

    def claim(self, session: str, template: str, egress: Sequence[str] = ()) -> SandboxHandle:
        raise SandboxRefused("sandbox_unavailable", "no sandbox provider is configured")

    def exec(
        self, handle: SandboxHandle, command: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        raise SandboxRefused("sandbox_unavailable", "no sandbox provider is configured")

    def release(self, handle: SandboxHandle, mode: str = "destroy") -> None:
        return None


def select_provider(
    providers: Sequence[SandboxProvider], *, required: str | None = None
) -> SandboxProvider:
    """The strongest provider, refused when it is weaker than ``required``.

    ``required`` is the stricter of the agent version's ``sandbox.isolation`` and the tenant's
    policy. A compose deployment without ``runsc`` offers ``container-weak``; a tenant requiring
    ``gvisor`` is refused there, with the reason stated (ADR 0145 verification 6).
    """
    if required is not None and required not in ISOLATION_RANK:
        raise SandboxRefused("bad_isolation", f"unknown isolation level {required!r}")
    if not providers:
        providers = [NoSandbox()]
    ranked = sorted(
        providers, key=lambda p: ISOLATION_RANK.get(p.capabilities().isolation, 0), reverse=True
    )
    best = ranked[0]
    got = best.capabilities().isolation
    if required is not None and ISOLATION_RANK.get(got, 0) < ISOLATION_RANK[required]:
        raise SandboxRefused(
            "isolation_insufficient",
            f"policy requires {required} isolation; the {best.substrate} substrate offers "
            f"{got} ({best.name}) - refusing to run agent code here",
        )
    return best


def stricter(a: str | None, b: str | None) -> str | None:
    """The stronger of two isolation requirements (either may be absent)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if ISOLATION_RANK.get(a, 0) >= ISOLATION_RANK.get(b, 0) else b


__all__ += ["stricter"]

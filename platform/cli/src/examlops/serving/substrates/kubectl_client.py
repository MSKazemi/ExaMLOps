"""Real Kubernetes interaction for the KServe substrate's live apply (ADR 0142 d6).

Shells out to ``kubectl`` using **Server-Side Apply** (stable since Kubernetes 1.22, the
upstream-recommended mechanism for a client that manages objects declaratively — see
docs.kubernetes.io/docs/reference/using-api/server-side-apply). ``exa`` is invoked by a human or
an agent using their own kubeconfig/RBAC context, not a long-running in-cluster controller, so the
ambient credentials ``kubectl`` already resolves are the right ones to use — the same shape this
project already uses for the dry-run validation path (``examlops.serving_backends._kubectl_apply``).
The official ``kubernetes`` Python client is not used: it would need its own credential-loading
story (kubeconfig vs in-cluster) that duplicates what ``kubectl`` already does correctly, for a
CLI tool that is not itself a cluster-resident controller.

Namespace is explicit (``EXAMLOPS_KSERVE_NAMESPACE``, default ``examlops``), never the kubeconfig
context's ambient default — the rendered manifests carry no ``metadata.namespace`` (render stays
pure; namespace is a deployment-environment concern, resolved here in apply, per ADR 0142's own
"everything substrate-specific that touches the world is in apply"). Per-project namespace
isolation is a real, separate design question (this platform already labels objects
``examlops.io/project`` but does not yet map a project to a namespace) — deliberately not decided
here; one shared namespace is the honest, minimal starting point.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

DEFAULT_NAMESPACE = "examlops"
DEFAULT_FIELD_MANAGER = "examlops"

# The two KServe CRD kinds a servable can render as (ADR 0142 d2) — status/stop take only a name
# (the Substrate protocol, shared with every other substrate), so both are tried in turn; a given
# model is only ever one of the two (predictive xor generative, servable_kind()'s own rule).
_KIND_RESOURCES = ("inferenceservices.serving.kserve.io", "llminferenceservices.serving.kserve.io")


class KubectlUnavailable(Exception):
    """``kubectl`` is not on PATH, or the current context cannot reach a cluster."""


def _kubectl_path() -> str:
    path = shutil.which("kubectl")
    if not path:
        raise KubectlUnavailable("kubectl is not on PATH")
    return path


def _ref(obj: dict[str, Any]) -> str:
    kind = obj.get("kind", "Resource")
    name = (obj.get("metadata") or {}).get("name", "?")
    return f"{kind}/{name}"


class KubectlClient:
    """Server-side apply / get / delete for KServe objects, via the ambient kubeconfig context."""

    def __init__(self, namespace: str | None = None, field_manager: str | None = None) -> None:
        self.namespace: str = (
            namespace or os.getenv("EXAMLOPS_KSERVE_NAMESPACE") or DEFAULT_NAMESPACE
        )
        self.field_manager: str = (
            field_manager or os.getenv("EXAMLOPS_KSERVE_FIELD_MANAGER") or DEFAULT_FIELD_MANAGER
        )

    def apply(self, objects: list[dict[str, Any]]) -> list[str]:
        """Server-side apply every object, in order; raises on the first failure."""
        kubectl = _kubectl_path()
        refs = []
        for obj in objects:
            try:
                subprocess.run(
                    [
                        kubectl,
                        "apply",
                        "--server-side",
                        f"--field-manager={self.field_manager}",
                        "-n",
                        self.namespace,
                        "-f",
                        "-",
                    ],
                    input=json.dumps(obj).encode(),
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            except FileNotFoundError as exc:  # pragma: no cover - _kubectl_path already checked
                raise KubectlUnavailable(str(exc)) from exc
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
                raise RuntimeError(f"kubectl apply failed for {_ref(obj)}: {stderr}") from exc
            refs.append(_ref(obj))
        return refs

    def get(self, kind: str, name: str) -> dict[str, Any] | None:
        """The live object, or ``None`` if it does not exist. Any other failure raises."""
        kubectl = _kubectl_path()
        try:
            done = subprocess.run(
                [kubectl, "get", kind, name, "-n", self.namespace, "-o", "json"],
                check=True,
                capture_output=True,
                timeout=15,
            )
        except FileNotFoundError as exc:  # pragma: no cover - _kubectl_path already checked
            raise KubectlUnavailable(str(exc)) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "replace")
            if "NotFound" in stderr or "not found" in stderr:
                return None
            raise RuntimeError(f"kubectl get {kind}/{name} failed: {stderr.strip()}") from exc
        return json.loads(done.stdout)

    def delete(self, kind: str, name: str) -> None:
        """Delete if present; a no-op, not an error, if it is already gone."""
        kubectl = _kubectl_path()
        subprocess.run(
            [kubectl, "delete", kind, name, "-n", self.namespace, "--ignore-not-found"],
            check=True,
            capture_output=True,
            timeout=30,
        )

    def get_any_kind(self, name: str) -> dict[str, Any] | None:
        """``get`` tried across both KServe resource kinds — see the module note on why."""
        for kind in _KIND_RESOURCES:
            obj = self.get(kind, name)
            if obj is not None:
                return obj
        return None

    def delete_any_kind(self, name: str) -> None:
        """``delete`` issued against both kinds — idempotent, so trying the wrong one is free."""
        for kind in _KIND_RESOURCES:
            self.delete(kind, name)


def status_from_object(obj: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    """``(state, url, detail)`` from a KServe object's ``status`` block.

    A first-cut mapping from the ``Ready`` top-level condition — KServe's richer per-component
    conditions (``PredictorReady``, ``IngressReady``) and ``status.modelStatus`` (loading/loaded/
    failed-to-load) carry more detail than this collapses to a single ``SubstrateStatus.state``.
    Good enough to answer "is it up", not yet "why isn't it" — a named follow-up, not a silent gap.
    """
    status = obj.get("status") or {}
    conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
    ready = conditions.get("Ready")
    if ready == "True":
        state = "READY"
    elif ready == "False":
        state = "STARTING"
    elif not status:
        state = "PENDING"
    else:
        state = "UNKNOWN"
    url = status.get("url") or (status.get("address") or {}).get("url")
    return state, url, {"conditions": conditions}

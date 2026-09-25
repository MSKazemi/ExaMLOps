"""Kubernetes actuator for the autoscale controller (ADR 0031 clause 1 — the ``k8s`` applier).

Scales one model's predictor workload through the Kubernetes API's ``scale`` subresource
(``apps/v1`` Deployment, the object KServe raw-deployment mode creates as ``<isvc>-predictor``).
It is the actuator for the K8s serving path (E1) **when no other autoscaler owns the workload**:

* **One owner of replicas.** If a HorizontalPodAutoscaler — which is what a KEDA ``ScaledObject``
  materialises as (``keda-hpa-<name>``) — targets the same Deployment, the applier **refuses**
  (``ScaleApplierUnavailable``): two controllers writing one replica count is thrash, not scaling.
  Use either ``exa serve autoscale manifest`` (KEDA/Knative scale) or this applier, not both.
* **Standard library only.** ``urllib`` against the API server — no ``kubernetes`` client needed.
  In-cluster defaults (service-account token, CA and namespace under
  ``/var/run/secrets/kubernetes.io/serviceaccount``) or explicit ``EXAMLOPS_K8S_*`` settings.
* **Fail closed.** ``https`` with the cluster CA verified; plain ``http`` is accepted **only** for a
  loopback address (``kubectl proxy``), never for a remote API server. A missing token for a remote
  server is a refusal, not an anonymous request. Every call has a timeout.

Settings (environment):

======================================  ==========================================================
``EXAMLOPS_K8S_API``                    API server URL (default: in-cluster
                                        ``https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT``)
``EXAMLOPS_K8S_TOKEN_FILE``             bearer token file (default: service-account token)
``EXAMLOPS_K8S_CA_FILE``                CA bundle (default: service-account ``ca.crt``)
``EXAMLOPS_AUTOSCALE_NAMESPACE``        namespace (default: service-account namespace, else
                                        ``default``)
``EXAMLOPS_AUTOSCALE_K8S_TARGET``       Deployment name template, ``{name}`` = DNS-1123 model name
                                        (default ``{name}-predictor``)
``EXAMLOPS_K8S_TIMEOUT``                per-request timeout seconds (default 10)
======================================  ==========================================================

What is **not** verifiable here: no cluster is available to this repository's test suite, so the
wire protocol is exercised against a faithful fake of the API server's scale/HPA endpoints
(``tests/unit/test_autoscale_k8s_applier.py``), not a live cluster.
"""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from examlops.autoscale.controller import ScaleApplierUnavailable, ScaleApplyError
from examlops.autoscale.manifests import ManifestError, k8s_name

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
DEFAULT_TARGET = "{name}-predictor"
_HPA_PAGE = 250
_MAX_HPA_PAGES = 20

#: ``(method, path, body) -> (status, parsed JSON or None)`` — injectable for tests.
Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


class K8sConfigError(ScaleApplierUnavailable):
    """The applier cannot be configured safely (it refuses rather than guessing)."""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text().strip() or None
    except OSError:
        return None


def default_namespace() -> str:
    return (
        (os.getenv("EXAMLOPS_AUTOSCALE_NAMESPACE") or "").strip()
        or _read(SA_DIR / "namespace")
        or "default"
    )


def _api_base() -> str:
    explicit = (os.getenv("EXAMLOPS_K8S_API") or "").strip().rstrip("/")
    if explicit:
        return explicit
    host = os.getenv("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.getenv("KUBERNETES_SERVICE_PORT", "443").strip() or "443"
    if not host:
        raise K8sConfigError(
            "no Kubernetes API configured: set EXAMLOPS_K8S_API or run in-cluster "
            "(KUBERNETES_SERVICE_HOST)"
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}:{port}"


def http_transport() -> Transport:
    """A urllib transport for the configured API server; raises :class:`K8sConfigError`."""
    base = _api_base()
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise K8sConfigError(f"EXAMLOPS_K8S_API {base!r} is not an http(s) URL")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise K8sConfigError(
            "refusing plain http to a non-loopback Kubernetes API (only kubectl proxy on "
            "127.0.0.1 may be http)"
        )
    token = _read(os.getenv("EXAMLOPS_K8S_TOKEN_FILE") or SA_DIR / "token")
    if parts.scheme == "https" and not token:
        raise K8sConfigError("no service-account token for the Kubernetes API (fail closed)")
    ctx: ssl.SSLContext | None = None
    if parts.scheme == "https":
        ca = os.getenv("EXAMLOPS_K8S_CA_FILE") or str(SA_DIR / "ca.crt")
        ctx = ssl.create_default_context(cafile=ca if Path(ca).is_file() else None)
    try:
        timeout = float(os.getenv("EXAMLOPS_K8S_TIMEOUT", "10") or 10)
    except ValueError:
        timeout = 10.0
    timeout = min(max(timeout, 1.0), 60.0)

    def _call(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{base}{path}", data=data, method=method)  # noqa: S310
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/merge-patch+json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
                raw = resp.read(1_000_000)
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read(64_000)
            try:
                return exc.code, json.loads(raw) if raw else None
            except ValueError:
                return exc.code, None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ScaleApplyError(f"Kubernetes API unreachable: {exc}") from exc

    return _call


class KubernetesApplier:
    """Writes a model's replicas to its predictor Deployment's ``scale`` subresource."""

    name = "k8s"

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        namespace: str | None = None,
        target_template: str | None = None,
    ) -> None:
        self._transport = transport
        self.namespace = namespace or default_namespace()
        self.target_template = (
            target_template or os.getenv("EXAMLOPS_AUTOSCALE_K8S_TARGET") or DEFAULT_TARGET
        )
        if "{name}" not in self.target_template:
            raise K8sConfigError("EXAMLOPS_AUTOSCALE_K8S_TARGET must contain {name}")

    # -- helpers --------------------------------------------------------------
    def _t(self) -> Transport:
        if self._transport is None:
            self._transport = http_transport()
        return self._transport

    def target(self, model: str) -> str:
        try:
            name = self.target_template.replace("{name}", k8s_name(model))
        except ManifestError as exc:
            raise ScaleApplyError(str(exc)) from exc
        return name[:253]

    def _ns(self) -> str:
        return urllib.parse.quote(self.namespace, safe="")

    def _scale_path(self, model: str) -> str:
        name = urllib.parse.quote(self.target(model), safe="")
        return f"/apis/apps/v1/namespaces/{self._ns()}/deployments/{name}/scale"

    def _owning_hpa(self, model: str) -> str | None:
        """The HPA (KEDA's included) that already targets this Deployment, if any. Paged."""
        target = self.target(model)
        cont = ""
        for _ in range(_MAX_HPA_PAGES):
            q = f"?limit={_HPA_PAGE}" + (f"&continue={urllib.parse.quote(cont)}" if cont else "")
            status, body = self._t()(
                "GET",
                f"/apis/autoscaling/v2/namespaces/{self._ns()}/horizontalpodautoscalers{q}",
                None,
            )
            if status == 404:
                return None  # autoscaling/v2 not served: no HPA can own it
            if status != 200 or not isinstance(body, dict):
                # Unknown ownership: fail closed, a second writer must not be risked.
                raise ScaleApplyError(f"cannot list HPAs to check ownership (HTTP {status})")
            for item in body.get("items") or []:
                ref = (item.get("spec") or {}).get("scaleTargetRef") or {}
                if ref.get("kind", "Deployment") == "Deployment" and ref.get("name") == target:
                    return str((item.get("metadata") or {}).get("name") or "?")
            cont = str((body.get("metadata") or {}).get("continue") or "")
            if not cont:
                return None
        raise ScaleApplyError("HPA listing did not terminate within the page cap")

    # -- ScaleApplier ---------------------------------------------------------
    def check(self) -> None:
        """Resolve the API configuration now; raises :class:`K8sConfigError` if it is unsafe."""
        self._t()

    def current_replicas(self, model: str) -> int | None:
        try:
            status, body = self._t()("GET", self._scale_path(model), None)
        except K8sConfigError:
            raise  # misconfiguration is not "unknown replicas": surface it
        except ScaleApplyError:
            return None  # unknown → the controller holds
        if status != 200 or not isinstance(body, dict):
            return None
        spec = body.get("spec") or {}
        val = spec.get("replicas", 0)
        return int(val) if isinstance(val, int) and not isinstance(val, bool) else None

    def ready_replicas(self, model: str) -> int | None:
        """Ready pods of the Deployment (``status.readyReplicas``) — the activator's readiness probe."""
        try:
            status, body = self._t()(
                "GET",
                f"/apis/apps/v1/namespaces/{self._ns()}/deployments/"
                f"{urllib.parse.quote(self.target(model), safe='')}",
                None,
            )
        except ScaleApplyError:
            return None
        if status != 200 or not isinstance(body, dict):
            return None
        val = (body.get("status") or {}).get("readyReplicas", 0)
        return int(val) if isinstance(val, int) and not isinstance(val, bool) else None

    def apply(self, model: str, from_replicas: int, to_replicas: int) -> None:
        if isinstance(to_replicas, bool) or not isinstance(to_replicas, int) or to_replicas < 0:
            raise ScaleApplyError(f"invalid replica count {to_replicas!r}")
        owner = self._owning_hpa(model)
        if owner:
            raise ScaleApplierUnavailable(
                f"{self.target(model)} is owned by HPA {owner!r} (KEDA or a manual HPA); "
                "refusing to be a second writer of its replicas"
            )
        status, body = self._t()(
            "PATCH", self._scale_path(model), {"spec": {"replicas": to_replicas}}
        )
        if status == 404:
            raise ScaleApplyError(
                f"deployment {self.namespace}/{self.target(model)} not found "
                "(set EXAMLOPS_AUTOSCALE_K8S_TARGET / EXAMLOPS_AUTOSCALE_NAMESPACE)"
            )
        if status in (401, 403):
            raise ScaleApplierUnavailable(
                f"the service account may not patch deployments/scale in {self.namespace} "
                f"(HTTP {status}); grant it RBAC 'patch' on deployments/scale"
            )
        if status != 200:
            msg = body.get("message") if isinstance(body, dict) else None
            raise ScaleApplyError(f"scale patch failed (HTTP {status}): {msg or 'no detail'}")
        got = ((body or {}).get("spec") or {}).get("replicas") if isinstance(body, dict) else None
        if got is not None and got != to_replicas:
            raise ScaleApplyError(f"API accepted the patch but reports replicas={got}")


__all__ = [
    "K8sConfigError",
    "KubernetesApplier",
    "Transport",
    "default_namespace",
    "http_transport",
]

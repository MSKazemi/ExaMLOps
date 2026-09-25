"""Render the Ray multi-model server as a KubeRay ``RayService`` (ADR 0015 d1/d5, ADR 0142 d2).

ADR 0142 keeps dense multi-model predictive serving on Ray — ModelMesh is archived, so KServe is one
``InferenceService`` per model — and ADR 0015 d1 names **KubeRay** as how that Ray path runs on
Kubernetes. This module renders the *same* application the Compose ``ray-serving`` service runs
(``serving.ray_serving.app``: the ``MultiModelServer`` at ``/`` and the inference pipeline at
``/infer-pipeline``) as a ``ray.io/v1`` ``RayService``, validated offline against the vendored
KubeRay CRD schema (``kuberay_crds/<pin>/``) exactly as KServe renders are.

Continuity with the Compose path (ADR 0015 d5) is carried in the render, not re-invented:

* **metrics** — Ray's metrics port (``RAY_METRICS_EXPORT_PORT``, 8080) is set on every Ray node and
  the pods carry ``prometheus.io/scrape|port`` annotations, so the existing Prometheus rules and
  Grafana panels keep their series;
* **tracing** — ``OTEL_EXPORTER_OTLP_ENDPOINT`` and the sampler are passed through when given.
  ``OTEL_SDK_DISABLED`` is deliberately **never** rendered: Ray 2.55 records every metric through
  the OpenTelemetry SDK, which that variable turns into a no-op, and on Kubernetes the operator —
  not ``app.main()``'s ``_prepare_ray_environment`` — starts Ray, so nothing would remove it
  again. Unset already means tracing off for the platform (``examlops.observability``);
* **verify-before-load** — ``EXAMLOPS_SERVING_VERIFY`` is passed to the server, which already runs
  the D3 check in-process on every load (``serving/ray_serving/app.py``: ``_verified_uri``);
* **secrets** — credentials are never rendered: every Ray container reads an optional
  ``envFrom`` Secret (``examlops-serving-env`` by default) the operator creates.

Pure: the same inputs produce the same object. Which models are served is the registry the server
reads at start (``RAY_MODELS_DIR`` baked into the image, the Compose behaviour); the render records
the model names it was given in an annotation so the object says what it was planned to serve.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from examlops.serving.substrates.resolve import RenderError

RAYSERVICE_API = "ray.io/v1"
# The Ray version the serving image pins (serving/ray_serving/requirements.txt, `ray[serve]==`).
# KubeRay needs it to match the image; a guard test keeps the two equal.
RAY_VERSION = "2.55.0"
DEFAULT_NAME = "examlops-serving"
DEFAULT_ENV_SECRET = "examlops-serving-env"
SERVE_PORT = 8000  # KubeRay's serve service targets the head's `serve` port
METRICS_PORT = 8080

MULTI_MODEL_IMPORT = "serving.ray_serving.app:build_app"
PIPELINE_IMPORT = "serving.inference_pipeline.app:pipeline_app"

# Environment the Ray containers may carry from the caller — nothing else is copied, so a
# credential in the caller's environment cannot land in a manifest by accident.
PASSTHROUGH_ENV = (
    "MLFLOW_TRACKING_URI",
    "MLFLOW_S3_ENDPOINT_URL",
    "RAY_PRELOAD_ALIASES",
    "RAY_RELOAD_POLL_SECONDS",
    "RAY_NUM_REPLICAS",
    "EXAMLOPS_SERVING_VERIFY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
)
_SECRETISH = re.compile(r"(SECRET|TOKEN|PASSWORD|KEY)", re.IGNORECASE)
_NAME = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


def _container(
    name: str, image: str, env: list[dict[str, str]], env_secret: str, ports: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "name": name,
        "image": image,
        "env": env,
        "envFrom": [{"secretRef": {"name": env_secret, "optional": True}}],
        "ports": ports,
        "resources": {
            "requests": {"cpu": "1", "memory": "4Gi"},
            "limits": {"cpu": "4", "memory": "8Gi"},
        },
    }


def _pod_meta(labels: dict[str, str]) -> dict[str, Any]:
    return {
        "labels": dict(labels),
        "annotations": {"prometheus.io/scrape": "true", "prometheus.io/port": str(METRICS_PORT)},
    }


def render_ray_service(
    models: list[str],
    *,
    image: str,
    name: str = DEFAULT_NAME,
    project: str = "default",
    env: dict[str, str] | None = None,
    min_workers: int = 1,
    max_workers: int = 4,
    env_secret: str = DEFAULT_ENV_SECRET,
) -> dict[str, Any]:
    """A ``RayService`` serving ``models`` through the platform's Ray multi-model server."""
    if not image or ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
        raise RenderError(
            f"image {image!r} must be pinned by tag or digest (the Ray version must match it)"
        )
    if not _NAME.match(name) or len(name) > 47:  # KubeRay appends suffixes to derived names
        raise RenderError(f"RayService name {name!r} must be a DNS-1035 label of ≤47 characters")
    if not 0 <= min_workers <= max_workers or max_workers > 256:
        raise RenderError(
            f"workers {min_workers}..{max_workers}: need 0 ≤ min ≤ max ≤ 256 (bounded on purpose)"
        )
    names = sorted({str(m).strip() for m in models if str(m).strip()})
    if not names:
        raise RenderError("no models to serve: the registry directory has no model YAML")

    passed = {k: v for k, v in (env or {}).items() if k in PASSTHROUGH_ENV and v is not None}
    leaked = sorted(k for k in passed if _SECRETISH.search(k))
    if leaked:  # pragma: no cover - PASSTHROUGH_ENV holds no such name; a guard for edits to it
        raise RenderError(f"refusing to render credentials into a manifest: {leaked}")
    container_env = [{"name": k, "value": str(passed[k])} for k in sorted(passed)]
    container_env += [
        {"name": "PYTHONPATH", "value": "/app"},
        {"name": "RAY_METRICS_EXPORT_PORT", "value": str(METRICS_PORT)},
    ]
    labels = {
        "app.kubernetes.io/managed-by": "examlops",
        "app.kubernetes.io/name": name,
        "examlops.io/project": project,
    }
    start = {"metrics-export-port": str(METRICS_PORT)}
    serve_config = {
        "http_options": {"host": "0.0.0.0", "port": SERVE_PORT},
        "applications": [
            {"name": "multi_model_server", "import_path": MULTI_MODEL_IMPORT, "route_prefix": "/"},
            {
                "name": "inference_pipeline",
                "import_path": PIPELINE_IMPORT,
                "route_prefix": "/infer-pipeline",
            },
        ],
    }
    head_ports = [
        {"name": "gcs-server", "containerPort": 6379},
        {"name": "dashboard", "containerPort": 8265},
        {"name": "client", "containerPort": 10001},
        {"name": "serve", "containerPort": SERVE_PORT},
        {"name": "metrics", "containerPort": METRICS_PORT},
    ]
    worker_ports = [{"name": "metrics", "containerPort": METRICS_PORT}]
    return {
        "apiVersion": RAYSERVICE_API,
        "kind": "RayService",
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": {"examlops.io/models": ",".join(names)},
        },
        "spec": {
            "serveConfigV2": yaml.safe_dump(serve_config, sort_keys=True),
            "rayClusterConfig": {
                "rayVersion": RAY_VERSION,
                "headGroupSpec": {
                    "rayStartParams": {**start, "dashboard-host": "0.0.0.0"},
                    "template": {
                        "metadata": _pod_meta(labels),
                        "spec": {
                            "containers": [
                                _container("ray-head", image, container_env, env_secret, head_ports)
                            ]
                        },
                    },
                },
                "workerGroupSpecs": [
                    {
                        "groupName": "cpu",
                        "replicas": min_workers,
                        "minReplicas": min_workers,
                        "maxReplicas": max_workers,
                        "rayStartParams": dict(start),
                        "template": {
                            "metadata": _pod_meta(labels),
                            "spec": {
                                "containers": [
                                    _container(
                                        "ray-worker", image, container_env, env_secret, worker_ports
                                    )
                                ]
                            },
                        },
                    }
                ],
            },
        },
    }


def models_in(registry_dir: str) -> list[str]:
    """Model names declared by the per-model YAML files in ``registry_dir`` (ADR 0094 pack)."""
    from pathlib import Path

    root = Path(registry_dir)
    if not root.is_dir():
        raise RenderError(f"registry directory {registry_dir!r} does not exist")
    names = []
    for path in sorted(root.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RenderError(f"{path.name}: not valid YAML ({exc})") from exc
        if isinstance(doc, dict):
            names.append(str(doc.get("name") or path.stem))
    return names

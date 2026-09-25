# Kubernetes-native serving (E1)

ExaMLOps serves models through a **`ServingBackend`** seam with three implementations,
selected by `EXAMLOPS_SERVING_BACKEND`:

- **`ray-compose`** (default) — the existing Ray Serve / Docker Compose path. Behaviour is
  **unchanged**; a deployment with no cluster keeps working exactly as before (R2/R10).
- **`kserve-k8s`** — renders KServe manifests from the per-model YAML registry for a
  **resolved** model version, with no change to model definitions.
- **`kuberay-k8s`** — renders the same Ray multi-model server as a KubeRay `RayService`: the dense
  path where many small models share one Ray cluster (see [KubeRay](#kuberay-the-ray-path-on-kubernetes)).

Design: ADR 0015 · spec `design/vision/specs/E1-kubernetes-native-serving.md`.

## Choosing a backend

```bash
export EXAMLOPS_SERVING_BACKEND=kserve-k8s     # or ray-compose (default)
exa serve backend                              # show the active backend
```

`exa serve check` passes on either backend (seam parity, GWT-1).

## Generating manifests

Manifests are rendered **from the registry** — the model YAML is the single source of truth
(Phase 14) — and for **one concrete model version**. The MLflow alias is resolved first, so the
manifest names the version and the artifact location that would run, not an alias that may move
later:

| Model | Rendered as |
|---|---|
| classical / deep-learning (`framework:` in the YAML) | `serving.kserve.io/v1beta1` `InferenceService`, KServe **Standard** deployment mode, with `modelFormat` and an explicit `runtime` |
| LLM (an `engine:` block) | `serving.kserve.io/v1alpha2` `LLMInferenceService`: `spec.model.uri` / `spec.model.name`, the vLLM arguments from the same renderer the Compose and HPC launchers use, and KServe's default llm-d router |

```bash
exa serve manifest JPCP                                  # resolves the Production alias
exa serve manifest ChatModel                             # LLMInferenceService
exa serve manifest JPCP --canary 10 --out ./k8s/jpcp.yaml
# no MLflow reachable: name the exact version and where its artifacts are
exa serve manifest JPCP --version 17 \
  --artifact-uri s3://mlflow-artifacts/1/models/m-1/artifacts
```

What a manifest carries:

- **A storage URI a pod can load** (`s3://`, `gs://`, `oci://`, `hf://`, `pvc://`, `http(s)://`, …).
  Registry references (`mlflow:`, `models:`, `runs:`) are refused. If your MLflow server proxies
  artifacts (`mlflow-artifacts:/…`), set `EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION` to the server's
  `--artifacts-destination`, or pass `--artifact-uri`.
- **Labels naming what runs**: `examlops.io/model`, `examlops.io/version`, `examlops.io/alias`,
  `examlops.io/project`, and the annotation `examlops.io/artifact-digest` (`sha256:…` when the version
  is signed with `exa models sign`, otherwise `unsigned`).
- **The runtime that serves it.** `framework` maps to a KServe runtime: `sklearn`, `xgboost`,
  `lightgbm` → their KServe runtimes; `mlflow`/`pyfunc` → MLServer; `onnx`, `tensorrt`,
  `tensorflow`, `pytorch` → Triton; `huggingface` → the KServe Hugging Face runtime.
  `protocolVersion: v2` is set only for runtimes that speak it. `pytorch` is always pinned to
  Triton (which needs a TorchScript or ONNX export): left to auto-selection, KServe would pick
  TorchServe, which is no longer maintained upstream. A framework with no mapping is refused.

Every manifest is validated against the `openAPIV3Schema` of the KServe release the platform pins,
with unknown fields rejected the way `kubectl apply --validate=strict` rejects them. That check
covers structure only; CEL rules and admission webhooks run in the cluster. A test fails once the
pinned release is older than KServe's support window, so the pin is refreshed rather than left to
age. When `kubectl` and a cluster are reachable, the manifest is also checked with
`kubectl apply --dry-run=server`. **Nothing is applied for real through this `exa serve
manifest`/`kserve-k8s` path** — it is a generate-and-validate-only surface (E1's `ServingBackend`
seam).

A **separate, newer path does apply for real**: `exa serve llm start --launcher kserve` (ADR 0142
decision 6, the `examlops.serving.substrates` seam) performs a genuine Kubernetes Server-Side
Apply through `KServeSubstrate.apply()`, plan-gated and audited, live-verified against a real kind
cluster running KServe's own CRDs (`tests/integration/test_kserve_live_apply_kind_live.py`) — and
`status()`/`stop()` read and delete the live object the same way. If you need KServe to actually
touch a cluster today, that is the command; this page's `exa serve manifest` remains the
render-only, review-before-you-apply surface for the `kserve-k8s` `ServingBackend`.

## Canary rollouts

`--canary 10` renders KServe's Standard-mode `spec.canary` entry: the `--canary-alias` version
(default `Canary`) receives 10% of traffic next to the `--alias` version. Both predictors are named
after their versions (`v17`, `v18`), because that name becomes the Deployment name; promoting means
re-rendering with the new version as `--alias`. A canary needs both versions from the registry, so
it cannot be combined with `--artifact-uri`.

## Verify-before-load (D3) in the pod

KServe has no model-signature hook, so the check runs **in the pod**: the artifact is downloaded,
then checked against its `exa models sign` record, and only then may the model server start. It is
governed by the same switch as the Ray path, `EXAMLOPS_SERVING_VERIFY` (`off` · `warn` default ·
`enforce`), and it applies both to `exa serve manifest` and to the real apply through the `kserve`
substrate (`exa serve llm start --launcher kserve`).

Where it runs was read from KServe v0.20.0's own source, not assumed:

- a plain `initContainer` would run **before** the download (the webhook appends KServe's
  `storage-initializer` after yours), so it would check an empty `/mnt/models`;
- an **`InferenceService`** instead names a `ClusterStorageContainer`
  (`spec.predictor.storageContainerName`). The platform's container downloads with KServe's own
  `kserve_storage` library and then verifies, in one step. Per-model inputs reach it from the
  predictor's annotations (`examlops.io/verify-mode|verify-model|verify-version|signature`)
  through the downward API;
- an **`LLMInferenceService`** has no `storageContainerName`, but its controller merges a
  `template.initContainers[name=storage-initializer]` override into the one it builds (keeping your
  image and env, forcing its own args). The platform renders that override with the inputs as
  literal env.

One-time cluster setup:

```bash
# 1. build the verifier image (from the repository root) and push it to your registry
docker build -f platform/infra/kserve/verified-storage-initializer/Dockerfile -t REGISTRY/examlops-verified-storage-initializer:TAG .
export EXAMLOPS_KSERVE_VERIFIER_IMAGE=REGISTRY/examlops-verified-storage-initializer@sha256:...
# 2. install the ClusterStorageContainer (cluster-scoped: needs cluster-admin once)
exa serve verifier-manifest | kubectl apply -f -
# 3. publish the trust bundle the pods verify against: the base64 Ed25519 public key(s) that
#    `exa models sign` prints as EXAMLOPS_SIGNING_PUBLIC_KEYS=... (comma-separated for a rotation)
kubectl -n examlops create configmap examlops-signing-trust --from-literal=public-keys=BASE64_PUBLIC_KEY
# 4. choose the mode
export EXAMLOPS_SERVING_VERIFY=enforce
```

What each mode does:

| Mode | Render | In the pod |
|---|---|---|
| `off` | no verifier; a warning says so | whatever is downloaded is loaded |
| `warn` (default) | verifier wired when `EXAMLOPS_KSERVE_VERIFIER_IMAGE` is set, else a warning | failure is logged (`event: examlops.verify_before_load`), the pod starts |
| `enforce` | **refused** when the verifier cannot be rendered: no image, or a `pvc://`/`oci://` URI no storage initializer downloads | a failed **or unanswerable** check (no record, empty download, untrusted key, a verifier error) exits 1 and the model server never starts |

The pod needs no `platform.db`: the public part of the signature record (algorithm, digest,
signature, key id — nothing secret) travels in the rendered object. The trust bundle is the
`public-keys` key of the `examlops-signing-trust` ConfigMap (optional; without it every Ed25519
check fails as `untrusted-key`). HMAC-signed versions cannot be checked in the pod unless the pod
is given the HMAC key, which would let it forge; sign with Ed25519 (`EXAMLOPS_SIGNING_PRIVATE_KEY_FILE`)
for Kubernetes serving. A pod the platform did not render (no `examlops.io/verify-mode`
annotation) that the webhook matches to this container by URI is downloaded and passed through.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_SERVING_VERIFY` | `warn` | `off` / `warn` / `enforce`; an unknown value is refused, not read as `off` |
| `EXAMLOPS_KSERVE_VERIFIER_IMAGE` | unset | the verified storage-initializer image; required to wire the verifier |
| `EXAMLOPS_KSERVE_STORAGE_CONTAINER` | `examlops-verified-storage` | the `ClusterStorageContainer` name |
| `EXAMLOPS_KSERVE_TRUST_CONFIGMAP` | `examlops-signing-trust` | ConfigMap holding the `public-keys` trust bundle |

**Not yet exercised against a live KServe controller.** The render is validated against the pinned
v0.20.0 schemas and the entrypoint is tested with real Ed25519 signatures, but no test yet runs a
pod through a real storage-initializer webhook (that is ADR 0142 decision 7's KIND job).

## KubeRay: the Ray path on Kubernetes

KServe is one `InferenceService` per model (ModelMesh is archived upstream). Where many small
models should share one server, the platform keeps them on Ray, and on Kubernetes that is KubeRay:

```bash
exa serve kuberay-manifest --image ghcr.io/mskazemi/examlops-ray-serving:vX.Y.Z --out ray.yaml
exa serve kuberay-manifest --image IMAGE --min-workers 2 --max-workers 8 --project research
```

The `RayService` (`ray.io/v1`, validated against the vendored KubeRay v1.7.1 schema) runs the same
two applications as the Compose `ray-serving` service — the `MultiModelServer` at `/`
(`serving.ray_serving.app:build_app`) and the inference pipeline at `/infer-pipeline` — with
`rayVersion` equal to the serving image's `ray[serve]` pin (a test keeps them equal). Continuity with
Compose is carried in the render: Ray's metrics port and `prometheus.io/scrape` pod annotations
(so the existing alerts and panels keep their series), the OTLP endpoint and sampler when set, and
`EXAMLOPS_SERVING_VERIFY` (the server already verifies in-process on every load).
`OTEL_SDK_DISABLED` is never rendered: on KubeRay the operator starts Ray, so nothing would remove
it again, and it silences every Ray metric. Credentials are never rendered either — each Ray
container reads the optional Secret `examlops-serving-env` through `envFrom`. The image must be
pinned by tag or digest, and worker counts are bounded (≤256). Alias splits stay Ray router weights
(`exa serve traffic`), exactly as on Compose.

## Delivery: Server-Side Apply or GitOps

The `kserve` substrate's real apply (plan-gated, audited) has two deliveries, chosen by
`EXAMLOPS_KSERVE_DELIVERY`:

- **`server-side-apply`** (default) — `kubectl apply --server-side --field-manager=examlops` into
  `EXAMLOPS_KSERVE_NAMESPACE`.
- **`gitops`** — nothing is sent to an API server. The planned objects are written under
  `EXAMLOPS_KSERVE_GITOPS_DIR`, one file per object, with a `kustomization.yaml` an Argo CD
  `Application` or a Flux `Kustomization` reconciles:

```text
$EXAMLOPS_KSERVE_GITOPS_DIR/<namespace>/kustomization.yaml         # namespace: + sorted resources
$EXAMLOPS_KSERVE_GITOPS_DIR/<namespace>/inferenceservice-jpcp.yaml
$EXAMLOPS_KSERVE_GITOPS_DIR/<namespace>/llminferenceservice-chat.yaml
```

Files are deterministic (an unchanged plan leaves them byte-identical and untouched), written
atomically, and contained (a name is validated before it becomes a path). `status` reports the
declared version as `PENDING` — the live state is the reconciler's — and `stop` removes the file
(and the kustomization with the last one). Committing and pushing the tree is your pipeline's step.
An unknown delivery, or `gitops` without a directory, is refused rather than falling back.

## Packaging

The Helm chart (`platform/infra/helm/examlops`) installs the control plane, dashboard, agent and
ingress — it does **not** include a serving plane (no Ray Serve, vLLM or KServe resources). CI lints
and renders it with `make helm-validate`. The serving plane on Kubernetes is the rendered KServe /
KubeRay objects above, applied directly or through the GitOps tree. LLM calls emit C1 GenAI spans (R8).

> **Migration:** start on `ray-compose`, generate manifests with `exa serve manifest` (or
> `exa serve kuberay-manifest` for the dense Ray path), apply them to a cluster, then flip
> `EXAMLOPS_SERVING_BACKEND=kserve-k8s` (or `kuberay-k8s`). GPU sharing (E3), autoscaling (E5), and
> disaggregated serving (E4) layer on top. A model YAML `gpu_sharing:` block makes the rendered pod
> request a MIG slice, a time-sliced GPU or a HAMi fraction. See
> [GPU sharing](gpu-sharing.md#kubernetes-serving-kserve).

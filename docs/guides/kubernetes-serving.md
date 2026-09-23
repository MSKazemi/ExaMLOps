# Kubernetes-native serving (E1)

ExaMLOps serves models through a **`ServingBackend`** seam with two implementations,
selected by `EXAMLOPS_SERVING_BACKEND`:

- **`ray-compose`** (default) — the existing Ray Serve / Docker Compose path. Behaviour is
  **unchanged**; a deployment with no cluster keeps working exactly as before (R2/R10).
- **`kserve-k8s`** — renders KServe manifests from the per-model YAML registry for a
  **resolved** model version, with no change to model definitions.

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

## Verify-before-load (D3)

The `kserve-k8s` backend exposes a **D3 verify-before-load** hook, but the generated manifests do
not invoke it: nothing in a KServe pod checks the artifact signature before serving. Verify an
artifact yourself before you apply its manifest — in `enforce` mode a tampered or unsigned artifact
exits non-zero:

```bash
exa models verify JPCP 17 --path ./artifacts/jpcp --mode enforce
```

## Packaging & GitOps

The Helm chart (`platform/infra/helm/examlops`) installs the control plane, dashboard, agent and
ingress — it does **not** include a serving plane (no Ray Serve, vLLM or KServe resources). CI lints
and renders it with `make helm-validate`. No Kustomize overlays or GitOps (Argo/Flux CD) manifests
ship with the repository; applying generated serving manifests to a cluster is up to you. LLM calls
emit C1 GenAI spans (R8).

> **Migration:** start on `ray-compose`, generate manifests with `exa serve manifest`,
> apply them to a cluster, then flip `EXAMLOPS_SERVING_BACKEND=kserve-k8s`. GPU sharing
> (E3), autoscaling (E5), and disaggregated serving (E4) layer on top.

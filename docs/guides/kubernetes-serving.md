# Kubernetes-native serving (E1)

ExaMLOps serves models through a **`ServingBackend`** seam with two implementations,
selected by `EXAMLOPS_SERVING_BACKEND`:

- **`ray-compose`** (default) — the existing Ray Serve / Docker Compose path. Behaviour is
  **unchanged**; a deployment with no cluster keeps working exactly as before (R2/R10).
- **`kserve-k8s`** — generates KServe `InferenceService` (or `LLMInferenceService` for LLMs)
  manifests from the per-model YAML registry + MLflow alias, with no change to model
  definitions.

Design: ADR 0015 · spec `design/vision/specs/E1-kubernetes-native-serving.md`.

## Choosing a backend

```bash
export EXAMLOPS_SERVING_BACKEND=kserve-k8s     # or ray-compose (default)
exa serve backend                              # show the active backend
```

`exa serve check` passes on either backend (seam parity, GWT-1).

## Generating manifests

Manifests are generated **from the registry** — the model YAML is the single source of
truth (Phase 14). The MLflow alias becomes the `storageUri` (`mlflow://models/<name>@<alias>`),
and the `engine:` block (E2) drives the LLM runtime args (dtype, quantization, tensor-parallel).

```bash
exa serve manifest JPCP --alias Production
exa serve manifest ChatModel --alias Production          # LLMInferenceService (vLLM/SGLang)
exa serve manifest JPCP --alias Canary --canary 10 --out ./k8s/jpcp.yaml
```

Every generated manifest is validated structurally before it is emitted; in CI the same
manifests pass `kubectl --dry-run=server` / kubeconform (R6).

## Alias moves → canary rollouts

An MLflow alias move maps to a KServe canary/rollout: `--canary 10` sets
`spec.predictor.canaryTrafficPercent: 10`, routing 10% of traffic to the new revision, with
rollback by reverting the alias (R4/GWT-3).

## Verify-before-load (D3)

The K8s loader runs **D3 verify-before-load** before serving: in `enforce` mode a tampered
or unsigned artifact is refused (R9/GWT-6). With D3 unavailable (dev), loading is not blocked.

## Packaging & GitOps

The full stack installs via a Helm chart + Kustomize overlays and deploys via GitOps
(Argo/Flux CD) (R7). Serving metrics/logs/traces flow to the existing
Prometheus/Grafana/Loki/Tempo stack, and LLM calls emit C1 GenAI spans (R8).

> **Migration:** start on `ray-compose`, generate manifests with `exa serve manifest`,
> apply them to a cluster, then flip `EXAMLOPS_SERVING_BACKEND=kserve-k8s`. GPU sharing
> (E3), autoscaling (E5), and disaggregated serving (E4) layer on top.

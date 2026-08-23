# Serving LLMs and VLMs with vLLM (`exa serve llm`)

ExaMLOps serves large language models and **vision-language models (VLMs)** with
[vLLM](https://docs.vllm.ai), through one operator surface — `exa serve llm` — that works
the same way on four substrates: an endpoint someone else runs, a Docker Compose GPU
service, an HPC allocation (Slurm/Flux), or Kubernetes via KServe.

Design: **ADR 0107** (refining ADR 0096) · spec `design/vision/specs/spec-enterprise-llm-serving.md` §4.5.

## The shape of it

```
exa serve llm start ──> EndpointLauncher { external | compose | slurm/flux | kserve }
                              │  renders argv via engines.to_vllm_args()  ← one source of truth
                              └─> llm_endpoints registry (base_url · state · launcher · job_id)

exa serve llm chat  ──> gateway (keys · budgets · cost) ──> media guard ──> VLLMServerEngine
                              └── HTTP ──> /v1/chat/completions on `vllm serve`
```

Only *how a process starts* and *where its address comes from* differ between substrates.
The request path — gateway → media validation → engine → telemetry/FinOps/audit — is one
code path, so what you verify on a laptop is what runs on the cluster.

## Quick start (no GPU needed)

The `external` launcher is the default and starts nothing, so it works on a CPU-only host
against a server someone else operates:

```bash
exa serve llm start qwen-vl \
    --base-url http://gpu01:8000 \
    --hf-model Qwen/Qwen3-VL-8B-Instruct \
    --modality vision --max-images 2 --media-domains example.com

exa serve llm list
exa serve llm health qwen-vl          # exits 1 if unreachable → usable as a CI gate
exa serve llm chat qwen-vl -m "Summarise this alert"
```

Ask a vision model about a picture — the VLM smoke test:

```bash
exa serve llm chat qwen-vl -m "What does this chart show?" --image ./gpu-util.png
```

A local `--image` is inlined as a `data:` URL rather than a `file://` path, because the
server usually runs on another host where your path would not resolve.

## Substrates

### HPC (Slurm / Flux) — the European-HPC pattern

```bash
exa serve llm start qwen-vl --launcher slurm \
    --hf-model Qwen/Qwen3-VL-8B-Instruct \
    --nodes 2 --gpus 4 --tp 4 --pp 2 --partition gpu --walltime 04:00:00
```

Renders and submits `platform/infra/slurm-adapter/templates/vllm_serve.sh.tmpl`: Apptainer
pulls the vLLM image once into a shared path, a Ray cluster comes up across the allocation
(head + workers), and `vllm serve` runs with tensor parallelism *within* a node and
pipeline parallelism *across* nodes. A single-node allocation skips Ray entirely — vLLM
does in-node tensor parallelism by itself, and an unnecessary Ray cluster is one more thing
that can fail to start.

The job writes its own endpoint URL to `<work_dir>/<model>.endpoint` as soon as the head
node is known, so the address is learned from the job rather than scraped out of `squeue`.

| Env | Purpose |
|---|---|
| `EXAMLOPS_VLLM_IMAGE` | Container image (default `docker://vllm/vllm-openai:latest`) |
| `EXAMLOPS_VLLM_WORK_DIR` | Scripts, SIF cache and endpoint files (default `/tmp/examlops-vllm`) |
| `EXAMLOPS_VLLM_MODULES` | Comma-separated `module load` names, e.g. `Apptainer/1.3.1-GCCcore-12.3.0` |
| `EXAMLOPS_HPC_SCHEDULER` | `mock` · `slurm` · `flux` |

The allocation is recorded in `hpc_jobs` with `kind='serve'`. That discriminator matters:
the training poller waits for a terminal state, which a healthy server never reaches, so a
serving job must never enter it.

### Docker Compose (single GPU node)

```bash
exa serve llm start qwen-vl --launcher compose --hf-model Qwen/Qwen3-VL-8B-Instruct
# equivalently, with the weights named explicitly:
EXAMLOPS_VLLM_MODEL=Qwen/Qwen3-VL-8B-Instruct docker compose --profile vllm up -d vllm
```

`EXAMLOPS_VLLM_MODEL` has no default. `exa serve llm start` sets it for you; the raw compose
form needs it in the environment or in `.env`, and vLLM will report the variable by name if it
is missing. It is deliberately **not** a compose required-variable (`:?`) — compose interpolates
every service before it filters by profile, so that form aborted every compose command on the
CPU machines this profile exists to protect.

The `vllm` service sits behind a `vllm` **profile** — it is the only GPU service in the
stack, so a plain `docker compose up` on a CPU box must not try to start it. It reserves
NVIDIA devices, caches weights in the `vllm_cache` volume (a cold pull is tens of GB), and
sets `shm_size: 8gb` because PagedAttention needs far more shared memory than Docker's
64 MB default — too little shows up as an opaque worker crash, not an out-of-shm error.

Its healthcheck allows a 600 s `start_period`: loading a large model takes minutes, and a
short one would restart the container forever before it ever finished loading.

### KServe (Kubernetes)

```bash
EXAMLOPS_SERVING_BACKEND=kserve-k8s exa serve manifest qwen-vl --alias Production
exa serve llm start qwen-vl --launcher kserve
```

Emits an `LLMInferenceService` whose `args` come from the same `to_vllm_args` renderer.
Live apply stays behind `EXAMLOPS_KSERVE_LIVE_APPLY=1`; without it the manifest is
generated and server-dry-run validated — still useful, since that is the CI check that the
YAML→manifest mapping is right with no cluster in sight.

## Multimodal safety — read this before serving a VLM

A VLM endpoint that forwards arbitrary `image_url` values **is an SSRF primitive** (the
classic target being a cloud metadata address like `169.254.169.254`), and one that accepts
unbounded base64 is a memory-exhaustion primitive. ExaMLOps enforces limits at both ends:
in-process before dispatch, and via the flags rendered onto the vLLM server itself.

```yaml
engine:
  engine: vllm
  hf_model_id: Qwen/Qwen3-VL-8B-Instruct
  multimodal:
    modality: vision                    # text | vision | audio | video
    limit_mm_per_prompt: {image: 2}     # REQUIRED for any non-text modality
    allowed_media_domains: [example.com] # SSRF allow-list; empty = no remote URLs at all
    allowed_local_media_path: /data/media # gate for file:// media
    max_image_bytes: 20971520           # 20 MiB
    mm_processor_cache_gb: 4
```

Rules worth knowing:

- **An empty `allowed_media_domains` denies all remote media** — it fails closed, it does
  not mean "anything goes". Subdomains of a listed domain are accepted.
- **A non-text modality without `limit_mm_per_prompt` is rejected at validation time**, by
  both `exa serve llm start` and the registry-integrity CI guard.
- `file://` media is refused unless it resolves *inside* `allowed_local_media_path`; path
  traversal out of the root is rejected.
- The platform **validates but never fetches** remote media — vLLM does the fetch. Adding a
  fetcher here would put an SSRF-capable HTTP client in the control plane and defeat vLLM's
  multimodal processor cache.
- A rejection raises a typed error *before* any engine call, and is never retried on
  another backend (retrying a policy denial just hides the reason).

Sending an image to a **text-only** engine does not silently succeed: the text survives,
and a `RuntimeWarning` names how many media parts were dropped.

## Observability

`vllm serve` exposes `vllm:*` metrics on `/metrics`. The Compose service is scraped by the
`vllm` job; HPC endpoints land on scheduler-chosen nodes, so they are discovered through
file_sd:

```bash
exa hpc prometheus-sd --out platform/infra/docker-compose/targets/fleet.json
```

Prometheus re-reads that directory every 30 s — no reload, no config edit. Shipped alerts:
`VLLMEndpointDown`, `VLLMKVCacheNearFull` (KV-cache pressure is the leading indicator of an
OOM kill, so it buys time to shed load), `VLLMQueueBacklog`, `VLLMHighTTFT`.

## Command reference

| Command | What it does |
|---|---|
| `exa serve llm start <model>` | Start/register an endpoint. `--dry-run`, confirm, audited |
| `exa serve llm list` | Registered endpoints (`--project`, `--state`) |
| `exa serve llm status <model>` | Registry record + substrate status + live vLLM metrics |
| `exa serve llm health <model>` | Probe and reconcile state; **exit 1** when not ready |
| `exa serve llm args <model>` | The exact `vllm serve` argv the engine block renders |
| `exa serve llm chat <model>` | Chat, with `--image` (repeatable) and `--stream` |
| `exa serve llm bench <model>` | TTFT p50 + output tokens/s |
| `exa serve llm stop <model>` | Stop and deregister. `--dry-run`, confirm, audited |

## Governance

Endpoints can be attributed to a project workspace (`--project`, or the active context),
which records them as `project_resources(kind='serving_endpoint')` for cost and access
attribution (ADR 0086). Every mutation writes an `audit_events` row. API keys resolve
through the D7 secrets store via `engine.api_key_secret_ref` and are passed to a job by
environment, never on a command line where `ps` could read them.

## Known limits

- **No GPU is reachable from this repository today.** `lxp-cpu01` is CPU-only, and the
  Compose stack had no GPU configuration before this work. The whole path is tested on CPU
  against a stub HTTP server (real sockets, real SSE framing), but real VLM grounding,
  multi-node TP/PP launch, and throughput/TTFT targets remain **unverified** until a GPU
  allocation exists.
- KServe live apply is untested without a cluster and stays dry-run by default.
- vLLM on CPU works but is slow; `external` is the right launcher on a CPU host.

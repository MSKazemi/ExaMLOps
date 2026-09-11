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

exa gateway chat <model> ──> gateway (key · budget · guardrails · cache · cost) ──┐
exa serve llm chat <model> ─────────────────────── (direct smoke test) ───────────┤
                                                                                  v
                         media guard ──> VLLMServerEngine ── HTTP ──> /v1/chat/completions
```

Only *how a process starts* and *where its address comes from* differ between substrates.
Every registered endpoint is also a **gateway route** under its own name, so production
traffic goes gateway → media validation → engine → telemetry/FinOps — one code path, and
what you verify on a laptop is what runs on the cluster. `exa serve llm chat` is the
operator's direct line to the server: no key, no budget, no guardrail — use it to check
that a model answers, and the gateway for anything else.

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

## Through the gateway

Registering an endpoint makes it a route in the [model gateway](model-gateway.md) under
the endpoint's name. Everything the gateway does then applies to a real model: virtual-key
allow-lists and budgets, the guardrail scan in both directions, the semantic cache,
structured output, and a per-call cost record with its GenAI span.

```bash
KEY=$(exa --json gateway key issue --tenant acme --project chat --model qwen-vl --budget 20 \
      | jq -r .virtual_key)
exa gateway chat qwen-vl --message "Summarise this alert" --key "$KEY"
exa gateway key list                       # the key's spend moved
```

Endpoints that are stopped, disabled, or have no address yet are not routed. There is no
echo fallback behind an endpoint: if the server is down, the gateway says so
(`AllBackendsFailed`, naming `endpoint:<model>`) instead of answering with a stand-in.
Other platform callers pick endpoints up the same way. `exa rag query` generates through
the `default` route, so an endpoint named `default` replaces the echo placeholder there;
it reports a failed gateway call as "(no answer)" rather than the error.
`exa serve challenger judge` sends its judge prompt to the `--judge-model` route (default
`judge`), so a registered endpoint of that name becomes the judge — which still has to be calibrated
before its scores may gate.

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

The same script runs under `sbatch` and `flux batch`. It detects which scheduler it is in and
places each step through it: `scontrol`/`srun -w NODE` on Slurm, and `flux hostlist local` /
`flux run --requires=host:NODE` on Flux (a `flux batch` job is its own Flux instance). Flux gives
a task only the GPUs it asks for, so every step requests the node's `--gpus` explicitly
(`--gpus-per-task`). Run with no scheduler at all (a hand-run or mock job), the server starts on
the current host.

The job writes its own endpoint URL to `<work_dir>/<model>.endpoint` as soon as the head
node is known, so the address is learned from the job rather than scraped out of `squeue`.
The endpoint is registered at submission with no address. `exa serve llm health`, `status`
and `chat` read that file, record the URL, and from then on every reader uses it. On the SSH
transport the script is staged to the login node and the endpoint file is fetched back the
same way; the same `EXAMLOPS_VLLM_WORK_DIR` path is used on both sides. The gateway reads
the file only when it is visible on its own host — it never opens an SSH connection per
request — so behind SSH run `exa serve llm health` once to record the address.

`exa serve llm stop` cancels the job (`scancel`, or `flux cancel`) and marks the endpoint
STOPPED. Starting and stopping an endpoint both remove any endpoint file a previous job left,
so a restarted endpoint never inherits the old job's address. On Slurm, `--gpus` is requested
per node (`--gpus-per-node`), so `--nodes 2 --gpus 4` allocates eight GPUs.

```bash
exa serve llm health qwen-vl    # exit 1 until the job has published its address and loaded
```

The launcher name picks the scheduler: `--launcher flux` submits with `flux batch` and
`--launcher slurm` with `sbatch`, whatever `EXAMLOPS_HPC_SCHEDULER` says. That variable
governs *training* jobs and defaults to `mock`, and routing a serving request through the
mock adapter would return a job id for a job that never runs.

| Env | Purpose |
|---|---|
| `EXAMLOPS_VLLM_IMAGE` | Container image (default `docker://vllm/vllm-openai:latest`) |
| `EXAMLOPS_VLLM_WORK_DIR` | Scripts, SIF cache and endpoint files (default `/tmp/examlops-vllm`). On a real cluster point it at a filesystem the compute nodes share with the login node — a node-local `/tmp` is invisible from anywhere else, so the address would never be found |
| `EXAMLOPS_VLLM_MODULES` | Comma-separated `module load` names, e.g. `Apptainer/1.3.1-GCCcore-12.3.0` |
| `EXAMLOPS_HPC_TRANSPORT` / `EXAMLOPS_HPC_SSH_HOST` | Local or SSH transport to the login node, shared with training jobs |

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
EXAMLOPS_SERVING_BACKEND=kserve-k8s exa serve manifest qwen-vl --alias Production  # needs qwen-vl.yaml in the pack
exa serve llm start qwen-vl --launcher kserve
```

Emits an `LLMInferenceService` whose `args` come from the same `to_vllm_args` renderer and
validates it with `kubectl apply --dry-run=server` — the CI check that the YAML→manifest
mapping is right. It never applies the manifest: apply it (and later delete it) with
`kubectl`. `EXAMLOPS_KSERVE_LIVE_APPLY=1` only changes the recorded state from PENDING to
STARTING.

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
| `exa serve llm chat <model>` | Chat directly with the server, with `--image` (repeatable) and `--stream` |
| `exa gateway chat <model>` | The same model through the gateway: key, budget, guardrails, cache, cost |
| `exa serve llm bench <model>` | TTFT p50 + output tokens/s |
| `exa serve llm stop <model>` | Cancel the HPC job or stop the Compose service, and mark the endpoint STOPPED. `--dry-run`, confirm, audited |

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
- KServe endpoints are validated, never applied; there is no live-apply path yet.
- The Flux launch path is verified against the script's own logic (every scheduler command it
  issues, executed under shims), not yet against a Flux instance with GPUs. The platform's own
  Flux instance manages no GPUs today.
- The address an HPC job publishes is its head node's IP. The machine running `exa` (and the
  gateway) must be able to reach it on the serving port; from outside a cluster that
  usually needs a tunnel or a reachable login-node proxy.
- `exa gateway chat` sends text only. Use `exa serve llm chat --image` for the image
  smoke test.
- vLLM on CPU works but is slow; `external` is the right launcher on a CPU host.

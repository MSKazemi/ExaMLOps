# Optimized inference engines — vLLM / SGLang, quantization, speculative decoding

ExaMLOps serves generation through a thin `InferenceEngine` layer so the serving
backend (Ray/Compose, KServe E1) and the gateway (B2) don't care *which* runtime
executes tokens.

**The production engine is a client of a running `vllm serve` process**
(`vllm-server`, ADR 0107) — not an in-process library call. That matters: continuous
batching schedules *concurrent in-flight requests*, so an embedded engine inside a
short-lived `exa` process has nothing to batch, exposes no `/metrics`, and reloads the
weights every invocation. Everything the platform targets (KServe, Ray Serve LLM,
llm-d, the Slurm+Ray pattern on MeluXina) drives the server.

| Engine | When |
|---|---|
| `vllm` / `vllm-server` (default) | Serving. Talks OpenAI-compatible HTTP to a `vllm serve` process — text **and** vision models. |
| `vllm-inproc` | Offline batch scoring of a fixed corpus on a GPU host (`engine.mode: inproc`). |
| `sglang` | RadixAttention / structured output. Stub pending a GPU host. |
| `echo` | CPU dev and CI. Deterministic, zero deps. |

For vision-language models see **[VLM serving](vlm-serving.md)**; for the endpoint
lifecycle (`exa serve llm`) see the same guide.

Design: ADR 0016 + **ADR 0107** · spec `design/vision/specs/spec-enterprise-llm-serving.md`.

## Graceful degrade

The server engine needs **no** local GPU dependency — the GPU lives in the `vllm serve`
process. With no endpoint configured (`engine.base_url` / `EXAMLOPS_VLLM_BASE_URL`) the
path degrades to `EchoEngine` with a `RuntimeWarning` naming the fix, so the full
gateway→engine path stays exercisable on a CPU box. `vllm-inproc`/`sglang` lazily import
their GPU-bound deps and degrade the same way
(`install examlops[serving-vllm]` on a GPU host).

## Per-model `engine:` block

Add an `engine:` block to the active use-case pack's model YAML,
`usecases/<pack>/models/<name>.yaml` (ADR 0094 — resolved via `EXAMLOPS_USECASE_DIR`):

```yaml
engine:
  engine: vllm            # vllm | vllm-server | vllm-inproc | sglang | echo
  mode: server            # server (default) | inproc
  base_url: http://gpu01:8000   # where `vllm serve` is listening
  hf_model_id: Qwen/Qwen3-VL-8B-Instruct
  dtype: bfloat16         # auto | float16 | bfloat16 | float32 | int8 | int4 | fp8
  quantization: awq       # awq | gptq | fp8 | null
  max_model_len: 8192
  tensor_parallel_size: 4   # GPUs per node
  pipeline_parallel_size: 2 # nodes
  gpu_memory_utilization: 0.9
  kv_cache_dtype: fp8
  enable_chunked_prefill: true
  prefix_cache: true
  speculative_decoding:
    enabled: true
    draft_model: my-tiny-draft   # required when enabled
  multimodal:             # see the VLM guide
    modality: vision
    limit_mm_per_prompt: {image: 2}
    allowed_media_domains: [example.com]
```

### One block → one argv, everywhere

`engines.to_vllm_args(config)` is the **only** renderer of the `vllm serve` command line,
and the Compose service, the Slurm job template, the KServe manifest `args` and
`exa serve llm args` all call it. That is what makes an `engine:` block mean the same
thing on HPC as on Kubernetes — see for yourself:

```bash
exa serve llm args qwen-vl
```

The block is validated by the same check the registry-integrity CI guard runs — an
invalid engine, dtype, tensor-parallel size, or a spec-decode block missing its
`draft_model` fails CI:

```bash
exa models engine validate ./usecases/reference/models/jpcp.yaml
exa models engine list          # available engines
```

A vision model declared without a `limit_mm_per_prompt` is rejected here: an unbounded
media count per request is a denial-of-service surface.

## Quantization → signed + BOM'd version (D3)

`exa models quantize` produces a new model version and registers it through the D3
supply-chain gate — signed + AI-BOM'd — so the quantized artifact enters serving with
the same provenance guarantees as any other version:

```bash
exa models quantize JPCP 17 --method awq --path ./artifacts/jpcp \
    --dataset FData --dataset-revision abc123
# → registers JPCP@17-awq, signed + BOM'd
```

Before promotion, a quantized version must clear the **C3 eval-gate** (quality floor)
— wire it into `exa pipeline promote`. On a GPU host the real engine quantizer runs; in
degraded mode the transformation is recorded as provenance so the sign+BOM path stays
exercisable.

## Speculative decoding

Speculative decoding is opt-in per model (`speculative_decoding.enabled`). Acceptance
rate and estimated speedup are emitted to **C1 GenAI telemetry** spans
(`examlops.specdecode.acceptance_rate` / `.speedup`) and feed FinOps cost — so the
throughput win is measured, not assumed.

## Observability

A `vllm serve` process exposes `vllm:*` Prometheus metrics on its own `/metrics`:
time-to-first-token, inter-token latency, end-to-end latency, `num_requests_running` /
`_waiting`, `kv_cache_usage_perc`, and prefix-cache hit rate. The stack scrapes them via
the `vllm` job (Compose) and the `fleet` file_sd job (HPC-launched endpoints, generated
by `exa hpc prometheus-sd`), with four alerts shipped: `VLLMEndpointDown`,
`VLLMKVCacheNearFull`, `VLLMQueueBacklog`, `VLLMHighTTFT`.

For a quick look without Grafana:

```bash
exa serve llm status qwen-vl   # registry record + substrate status + live vLLM metrics
exa serve llm bench qwen-vl    # TTFT p50 + output tokens/s
```

Per-generation cost lands in C1 GenAI spans and FinOps, via the swappable `llm_cost`
provider when one is configured (`EXAMLOPS_LLM_COST_PROVIDER`). Spec-decode acceptance
and prefix-cache hit rate surface in Grafana. GPU requests honour E3 sharing where
configured.

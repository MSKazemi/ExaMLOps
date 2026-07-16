# Optimized inference engines — vLLM / SGLang, quantization, speculative decoding

ExaMLOps serves generation through a thin `InferenceEngine` layer so the serving
backend (Ray/Compose today, KServe E1 next) and the gateway (B2) don't care *which*
runtime executes tokens. Two production engines ship — **vLLM** (default;
PagedAttention + continuous batching) and **SGLang** (RadixAttention + structured
output) — plus a dependency-free **`echo`** engine for CPU dev and tests.

Design: ADR 0016 · spec `design/vision/specs/E2-optimized-inference-engines.md`.

## Graceful degrade

`vLLM`/`SGLang` lazily import their GPU-bound deps and raise a clear, actionable error
if unavailable (`install examlops[serving-vllm] on a GPU host`). On a CPU-only host set a
model's engine to `echo` — the same `generate`/`stream`/`health` contract, deterministic
output, zero deps. `build_engine(config)` picks the class named by the config.

## Per-model `engine:` block

Add an `engine:` block to `pipelines/models/<name>.yaml`:

```yaml
engine:
  engine: vllm            # vllm | sglang | echo
  dtype: float16          # auto | float16 | bfloat16 | float32 | int8 | int4 | fp8
  quantization: awq       # awq | gptq | fp8 | null
  max_model_len: 4096
  tensor_parallel_size: 2
  prefix_cache: true
  speculative_decoding:
    enabled: true
    draft_model: my-tiny-draft   # required when enabled
```

The block is validated by the same check the registry-integrity CI guard runs — an
invalid engine, dtype, tensor-parallel size, or a spec-decode block missing its
`draft_model` fails CI:

```bash
exa models engine validate ./pipelines/models/jpcp.yaml
exa models engine list          # available engines
```

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

Engine throughput/latency, prefix-cache hit rate, and spec-decode acceptance surface in
Grafana and C1 spans. GPU requests honour E3 sharing where configured.

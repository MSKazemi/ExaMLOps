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
| `sglang` | RadixAttention prefix caching / structured output. A client of a running `sglang.launch_server` (`SGLangServerEngine`), same OpenAI-compatible wire as `vllm-server` — see [SGLang](#sglang) below. |
| `echo` | CPU dev and CI. Deterministic, zero deps. |

For vision-language models see **[VLM serving](vlm-serving.md)**; for the endpoint
lifecycle (`exa serve llm`) see the same guide.

Design: ADR 0016 + **ADR 0107** · spec `design/vision/specs/spec-enterprise-llm-serving.md`.

## Graceful degrade

The server engine needs **no** local GPU dependency — the GPU lives in the `vllm serve`
process. With no endpoint configured (`engine.base_url` / `EXAMLOPS_VLLM_BASE_URL`) the
path degrades to `EchoEngine` with a `RuntimeWarning` naming the fix, so the full
gateway→engine path stays exercisable on a CPU box. `vllm-inproc` lazily imports its
GPU-bound dep and degrades the same way (`install examlops[serving-vllm]` on a GPU host).
`sglang` follows the same rule with its **own** endpoint variable: no `engine.base_url` and no
`EXAMLOPS_SGLANG_BASE_URL` ⇒ `EchoEngine` with a warning, or `RuntimeError` under
`allow_fallback=False`. `EXAMLOPS_VLLM_BASE_URL` never routes an SGLang model, so one box can
point at both servers without a model being answered by the wrong runtime.

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

## SGLang

SGLang is integrated as a **server**, exactly like vLLM (ADR 0016 decision 1): the GPU lives in a
`python -m sglang.launch_server` process, which does continuous batching and RadixAttention
prefix caching across every client, and `exa`/the gateway talk OpenAI-compatible HTTP to it.

```yaml
engine:
  engine: sglang
  base_url: http://gpu02:30000     # or EXAMLOPS_SGLANG_BASE_URL
  hf_model_id: Qwen/Qwen3-8B
  tensor_parallel_size: 2          # → --tp-size
  gpu_memory_utilization: 0.85     # → --mem-fraction-static
  prefix_cache: true               # false → --disable-radix-cache
  api_key_secret_ref: sglang-key   # resolved via D7 secrets; env fallback EXAMLOPS_SGLANG_API_KEY
  speculative_decoding:
    enabled: true
    draft_model: my-eagle-draft    # → --speculative-draft-model-path
    method: EAGLE                  # EAGLE | EAGLE3 | NEXTN | STANDALONE | NGRAM (default EAGLE)
    num_speculative_tokens: 4      # γ → --speculative-num-steps 4 --speculative-eagle-topk 1
                                   #     --speculative-num-draft-tokens 5 (NGRAM: draft-tokens 4)
```

`engines.to_sglang_args(config)` is the single renderer of the launch argv (the twin of
`to_vllm_args`), and `exa serve llm args <model>` prints it for an `engine: sglang` endpoint.
`--enable-metrics` is always rendered so the server's `sglang:*` Prometheus series exist;
`exa serve llm status` reads them. Secrets are never rendered on the command line.

Fields SGLang cannot honour are **refused at render time**, not silently dropped:
`swap_space_gb` (a vLLM feature), a multimodal block with media limits (vLLM enforces those
server-side; SGLang has no equivalent flag, so only half of the two-ended media guard would run —
serve multimodal models with `engine: vllm`), and an unknown speculative `method`.

Not built yet: a launcher that *starts* an SGLang server (the Compose/Slurm/Flux/KServe launchers
start vLLM; register a running SGLang server with `exa serve llm start <m> --base-url …`), and a
KServe `LLMInferenceService` rendering for SGLang (the KServe renderer refuses non-vLLM engines).

## Quantization → signed + BOM'd version (D3)

`exa models quantize` produces a new model version and registers it through the D3
supply-chain gate — signed + AI-BOM'd — so the quantized artifact enters serving with
the same provenance guarantees as any other version:

```bash
exa models quantize JPCP 17 --method awq --path ./artifacts/jpcp \
    --dataset FData --dataset-revision abc123
# → registers JPCP@17-awq, signed + BOM'd
```

Before promotion, a quantized version must clear the **quantization quality gate**
(ADR 0016 decision 3) — a C3 eval gate that compares `17-awq` against its **base** version `17`
on the model's configured suite:

```bash
exa eval run qa --items ./eval/qa.jsonl --model JPCP --version 17
exa eval run qa --items ./eval/qa.jsonl --model JPCP --version 17-awq
exa models quantize-gate JPCP 17-awq   # exits 1 on refusal — usable as a CI step
```

The gate is **mandatory and fails closed**: no configured C3 gate (`exa eval gate set`), or no
scores for either version — or a gate metric the base version was never scored on — is a
refusal. It always blocks, even when the model's C3 gate is in `warn` mode or uses the
`majority` aggregate (every metric must retain quality), and every gate metric without its own
`max_drop` is held to
`EXAMLOPS_QUANTIZATION_MAX_DROP` (default `0.01`) against the base. ADR 0111's judge-calibration
refusal applies. `exa pipeline promote` (with an audited `--force` override), the training-flow
promotion (`promotion_refusal`) and the dashboard alias route all enforce it; each verdict is a
`gate_reports` row plus a `quantization_quality_gate` audit event. ADR 0117's portability gate
(`exa models parity`) still runs afterwards for the numeric side.

**No quantization compute runs today, on any
host** (CPU or GPU) — `quantize_model()`'s own docstring states this explicitly and
`weights_transformed` is unconditionally `False`. The command records the intended
transformation as provenance-only (signed + BOM'd), so the sign+BOM path stays
exercisable ahead of a real AWQ/GPTQ/FP8 compute backend being wired in (tracked, not
yet built — see ADR 0016).

## Speculative decoding

Speculative decoding is opt-in per model (`speculative_decoding.enabled`). Every engine built
by `build_engine` is wrapped once, and any generation that reports draft statistics sets
`examlops.specdecode.acceptance_rate` / `.speedup` on its **C1 GenAI** span and is folded into
an in-memory FinOps window per `(model, tenant, engine, lookahead)`. Windows flush to
`platform.db` (`specdecode_windows`) every `EXAMLOPS_SPECDECODE_FLUSH_CALLS` calls (default 100)
or once older than `EXAMLOPS_SPECDECODE_FLUSH_SECONDS` (default 60; the age is checked on every
speculative call, for every window), and at process exit — at most one row per active key per
window, never one per request. A process killed with `SIGKILL` loses its unflushed windows.
The tenant is whatever the caller passes as `tenant=` to the engine; the gateway does not pass
one yet, so gateway traffic is recorded under `default`. Read them with:

```bash
exa finops specdecode --model qwen-7b --days 7
```

Acceptance is summed accepted / proposed tokens (never an average of ratios). The **estimated
speedup** is the expected tokens per target forward pass, `(1 − α^(γ+1)) / (1 − α)` with
`α` = acceptance and `γ` = `num_speculative_tokens` (Leviathan, Kalman & Matias, ICML 2023). It
ignores the draft model's own cost, so it is an **upper bound** and is labelled as one.

Server engines do not return per-request draft counts, so their figures are not persisted to
`specdecode_windows`. For a `vllm serve` endpoint,
`exa serve llm status <model>` derives acceptance from the server's own cumulative
`vllm:spec_decode_num_draft_tokens` / `…_accepted_tokens` counters, computing the bound at
the endpoint's own `num_speculative_tokens`. An SGLang endpoint's draft counters are not read.

## Endpoint picking, cache salts and overload (ADR 0143 d2 · d3 · d6)

One interface, `examlops.inference_gateway.picker.EndpointPicker`
(`score(request_meta, endpoints) -> ranked endpoints`), with one implementation per substrate:

| Substrate | Picker | Who routes in production |
|---|---|---|
| `kserve` | `LLMDEndpointPicker` | llm-d's EPP behind LLMISVC; `router_block()` renders `spec.router` on the GAIE `InferencePool` v1 (no `InferenceObjective`) |
| `compose-ray` / `hpc-ray` | `RayServeLLMPicker` | Ray Serve LLM ≥ 2.58; `deployment_config()` selects `PrefixCacheAffinityRouter` |
| `hpc` / `external` (plain `vllm serve`) | `GatewayScorer` | this module: prefix affinity + `vllm:num_requests_waiting` + `vllm:kv_cache_usage_perc` scraped from each replica's `/metrics` (`scrape_endpoint_state`) |

For the two standard-owned substrates `score()` is a conformance model of the runtime's documented
policy. `conformance_report()` replays one synthetic multi-turn trace through any picker and
reports the session-affinity rate and the number of cross-salt prefix hits; the unit suite holds
every implementation to ≥ 95 % affinity and **zero** cross-salt hits.

- **Cache salts.** `cache_salt_for(project)` gives each project a stable salt (keyed HMAC when
  `EXAMLOPS_CACHE_SALT_KEY` is set). Every prefix key is salted, so the same system prompt sent
  by two projects never produces a cache hit across them. A `RequestMeta` without a salt is
  refused. `kv_retention_hint` is carried and ignored until an engine exposes a retention API.
- **Break-even gate.** The KV-aware scorer is off by default: the gateway scorer runs round-robin
  with session affinity unless `EXAMLOPS_KV_ROUTING=1` **and** the pool has at least
  `EXAMLOPS_KV_ROUTING_BREAK_EVEN` replicas (default 4).
- **Overload.** When every healthy replica is at its queued-request cap (`max_queued`), the picker
  raises `PoolOverloaded`, and its `headers()` give the `Retry-After` for a `429`.

The pickers are library code. The LLM gateway service does not call them yet (see the ADR's status).

## LoRA adapters are supply chain (ADR 0143 d8)

```yaml
engine:
  engine: vllm-server
  lora_adapters:
    - {name: sql, path: /models/lora/sql, model: sql-lora, version: "3"}
  max_loras: 2
  max_lora_rank: 16
  # allow_runtime_lora_updating: true   # refused while shared (the default)
  # shared: false                        # declare single-tenant to allow it
```

`to_vllm_args` renders `--enable-lora --lora-modules sql=/models/lora/sql --max-loras 2
--max-lora-rank 16`. Before any launcher (Compose, Slurm/Flux, KServe) starts a server,
`examlops.engines.lora.preflight` does two things:

1. It refuses runtime adapter updating on a shared deployment. That covers both
   `allow_runtime_lora_updating: true` and a `VLLM_ALLOW_RUNTIME_LORA_UPDATING=1` inherited from
   the shell.
2. It verifies every adapter through `examlops.supplychain.verify_before_load`, the same gate a
   model load uses. An adapter with no registry `model`/`version` cannot be signed, so it is
   refused. The adapter `path` is a directory: its files are checked against the signature
   exactly as `exa models sign <adapter> <version> --path <dir>` recorded them, so sign the
   adapter directory that way. The bytes must be present on the host that launches the server.
   An adapter path with no files there is refused rather than checked as an empty bundle.
   `EXAMLOPS_LORA_VERIFY_MODE=warn` records failures without refusing.

## Tool-call validity (ADR 0143 d9)

Pass `tools=[...]` and `tool_step=True` to `VLLMServerEngine.chat` for an agent tool step.
Under `EXAMLOPS_TOOL_CHOICE_POLICY=enforce` (the default), `tool_choice` `auto`, `none` or
absent becomes `required`, and a named function choice is kept. Every tool-step response
is validated: the call must name a function, and its arguments must be a JSON object. The
outcome is counted in `examlops.engines.tool_policy.parse_stats()`, and `prometheus_lines()`
exposes `examlops_tool_call_parse_total{outcome="ok|parse_error|missing"}`.

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
and prefix-cache hit rate are recorded to C1 GenAI telemetry spans (see above) and to
`vllm:*` Prometheus metrics scraped from the running process — **no Grafana dashboard
panel ships for them yet**; read them via `exa serve llm bench`/`status`, a raw
Prometheus query, or a span in Tempo/Jaeger. GPU requests honour E3 sharing where
configured.

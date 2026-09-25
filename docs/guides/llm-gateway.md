# LLM gateway service

A standalone, containerized OpenAI-compatible HTTP service in front of every LLM — Skipper, the
dashboard copilot, `exa gateway chat`, RAG, and any external OpenAI-compatible client all reach
models through it. Design record: ADR 0151–0156.

> **Not the same thing as [`docs/guides/model-gateway.md`](model-gateway.md)'s `GatewayClient`.**
> That page describes an **in-process Python library** (ADR 0010) — import it, build a `Router`,
> call `.chat()` — with no network hop and no service to run. This page describes a **real network
> service**: other processes reach it over HTTP, exactly the way they would reach OpenAI's own API.
> They share the same policy code (guardrails, routing, resilience, cost), so the two can never
> silently disagree about what a request is allowed to do — but they are two different ways to
> reach a model, not two names for the same thing.
>
> Also not the same thing as [`docs/guides/serving-gateway.md`](serving-gateway.md) (ADR 0126), the
> Envoy front door for **predictive** model traffic to Ray Serve. This page is the front door for
> **generative** (LLM) traffic.

```text
Skipper / copilot / exa gateway chat / RAG / an external OpenAI client
                    │  POST /v1/chat/completions  (Authorization: Bearer <virtual key>)
                    ▼
     ┌────────────────────────────────────────────────────┐
     │  llm-gateway service (:18020 → :8020 in-container)  │
     │  auth → rate limit (RPM/TPM) → D8 guardrail (in)    │
     │  → B3 cache lookup → route (GatewayCore) → dispatch │
     │  → D8 guardrail (out) → B3 cache store → account    │
     └────────────────────────────────────────────────────┘
                    │
                    ▼
        Ollama (native /api) today · an OpenAI-compatible
        upstream (router/LiteLLM/OpenRouter) — planned, ADR 0152
```

## Turn it on

An opt-in Compose profile; the default stack does not change.

```bash
cd platform/infra/docker-compose
docker compose --profile llm-gateway up -d llm-gateway
curl -s http://localhost:18020/ready   # 200 once a healthy deployment exists, 503 until then
```

With no `gateway.yaml` present it starts from `generated_config()`: the Ollama at
`EXAMLOPS_LLM_OLLAMA_URL` (default `http://host.docker.internal:11436`, the same relay hop the
agent uses), one route per chat model it reports — no setup needed for a local dev box.

## Auth and virtual keys

`LLM_GATEWAY_AUTH=keys` (the default) requires a virtual key on every request; `=off` disables
auth entirely (loopback-only use). Issue a key the same way as the in-process client (same
underlying store):

```bash
exa gateway key issue --tenant acme --project chat --budget 50 --model gpt-judge \
    --rpm 60 --tpm 100000
```

```bash
curl -s http://localhost:18020/v1/chat/completions \
    -H "Authorization: Bearer $EXA_KEY" -H "content-type: application/json" \
    -d '{"model": "default", "messages": [{"role": "user", "content": "hello"}]}'
```

`--rpm`/`--tpm` (requests- and tokens-per-minute, omit for unlimited) are enforced **before**
dispatch through the shared cross-process/cross-host `Coordinator`
(`EXAMLOPS_COORDINATOR=db`|`redis`) — a limit holds across every gateway replica, not just the one
that happens to answer a given request. TPM is checked as a read-only probe against the current
window before the call, then the real token count is recorded after — the cost of a call is not
known until it completes. A limit refusal is `429 rate_limited` with `retry-after: 60` (an honest
upper bound on a fixed-window limiter, not a promise of exactly that wait).

## What every request passes through

1. **Auth** — the virtual key's allow-list and dollar budget (`BudgetExceeded`/`ModelNotAllowed`).
2. **Rate limit** — RPM/TPM, above.
3. **D8 guardrail, inbound** — prompt-injection/PII/secret scanning, governed by
   `EXAMLOPS_GUARDRAIL_MODE` (`monitor` default: scan + record, never block; `enforce`: block/redact;
   `off`: skip). Runs on **every** request, streaming included.
4. **B3 semantic cache, lookup** — only when `LLM_GATEWAY_SEMANTIC_CACHE=1` (off by default —
   caching changes response behaviour for repeat-ish prompts). Runs on both the non-streaming and
   the **streaming** path (BL-115): a streaming hit is re-checked by the D8 output guard, then
   served as a single-chunk synthetic SSE stream. A hit skips dispatch entirely and is not billed.
   `x-examlops-cache: hit|miss` reports it (a streaming hit also gets `x-examlops-provider: cache`).
5. **Routing** (`GatewayCore`) — `priority` (default), `weighted`, `least_inflight`,
   `lowest_latency`, or `cost_aware` (a deployment's `price_per_1k` when declared, else
   locality: local/site free, external non-zero; scored by the `llm_routing` provider — see
   `docs/guides/model-gateway.md`'s routing section for the strategy semantics, shared code).
   Locality is filtered before every attempt: a caller's `examlops.allowed_localities` hint can
   only **narrow** the operator's own allow-list, never widen it.
6. **Dispatch** — DNS-rebinding-safe: the real TCP connection is pinned to a freshly re-resolved,
   re-validated address on every connect (`AsyncGuardedBackend`), not just checked once at
   provider construction.
7. **D8 guardrail, outbound** — on both paths (BL-115). Non-streaming: after accounting, so a
   blocked answer disappearing never makes the bill disagree with the upstream's. Streaming: see
   below — `monitor` mode (the default) never alters the stream; `enforce` mode buffers text
   behind a small lookback window.
8. **B3 cache store** — only on a genuine cache miss that passed the outbound scan, on both paths.
9. **Accounting** — cost (local/site = $0, external = the `llm_cost` provider's estimate),
   token counts, latency — all through the same accounting path `GatewayClient` uses.

## Streaming

`"stream": true` gets Server-Sent Events, same OpenAI chunk shape. Every step above applies to a
streaming request, with two caveats specific to a live (non-cached) generation:

- **D8 output guard in `monitor` mode (default) streams straight through, unbuffered** — monitor
  never alters text, so scanning the accumulated response once at stream end (purely for
  `guardrail_events` parity with the non-streaming path) costs nothing a caller can observe.
- **D8 output guard in `enforce` mode buffers text behind a small lookback window**
  (`_STREAM_GUARD_FLUSH_CHARS` = 512 new chars trigger a flush, holding back the last
  `_STREAM_GUARD_LOOKBACK` = 64 chars so a PII/secret pattern split across a flush boundary is
  never released half-redacted). A block caught before anything reached the caller refuses the
  whole response; a block caught after partial content was already sent cannot undo that send, so
  the stream ends with a `guardrail_blocked` SSE error event instead — the tokens already
  generated are still billed (they were real), matching the non-streaming path's own "billed
  before the block can raise" ordering. Only `.text` is scanned, matching the non-streaming path's
  scope (`.reasoning`/`.tool_calls` forward untouched on both paths).

Once the first byte has gone out, a live stream is never re-routed to a different deployment
(ADR 0153 d9); a mid-stream provider failure ends with a typed `stream_interrupted` error event
before `[DONE]`, not a silently truncated response.

## Config: `gateway.yaml`

`EXAMLOPS_GATEWAY_CONFIG`, else `<config dir>/gateway.yaml` (`datadir.config_dir()`) when present:

```yaml
version: 1
defaults:
  allowed_localities: [local, site]        # a caller can narrow this, never widen it
providers:
  n1:
    type: ollama
    base_url: http://host.docker.internal:11436
    locality: local
    discover: true                          # /api/tags + /api/show → capabilities
    max_concurrency: 2                       # unset = unbounded; set it to get a real bulkhead
    max_queue: 8
    keep_alive: 30m
    options: {num_ctx: 16384}
  omni:                                      # a router, reachable once ADR 0152's openai_compat
    type: ollama                             # adapter ships — today only `ollama` is a real type
    base_url: https://router.example.org
    locality: external
    external_ok: true                        # required in addition to a caller permitting `external`
models:
  chat:
    strategy: priority                       # priority | weighted | least_inflight |
    deployments:                             # lowest_latency | cost_aware
      - {provider: n1, model: qwen3:8b, priority: 0}
      - {provider: omni, model: auto, priority: 1}
    fallbacks: []
    required: true
aliases:
  default: chat
```

Validation is strict and total (every problem reported at once, with the exact key path at
fault) — the same validator backs both a config file and `POST /admin/reload` (needs
`LLM_GATEWAY_ADMIN_TOKEN`), so a file that validates offline is one the running gateway accepts;
a bad reload keeps serving the last good config and reports why via `GET /admin/config`.

## Endpoints

| Endpoint | Auth | What |
|---|---|---|
| `POST /v1/chat/completions` | virtual key (unless `LLM_GATEWAY_AUTH=off`) | The one real endpoint. `stream: true` for SSE. |
| `GET /v1/models` | virtual key | Models the caller's key may reach, filtered to routes with a non-open breaker. |
| `GET /health` | none | Always 200 — process liveness only. |
| `GET /ready` | none | Latches once healthy (ADR 0153 d10): a later outage degrades to typed 503s, never un-readies the pod into a restart loop. |
| `GET /admin/health` | admin token | Per-provider probe results + per-deployment breaker/inflight/queue snapshot. |
| `GET /admin/config` | admin token | Source (`file`/`generated`), routes, aliases, last reload error — never a secret. |
| `POST /admin/reload` | admin token | Re-reads the config file; 422 + keeps the old config on a validation failure. |
| `GET /metrics` | admin token | Prometheus exposition — see below. |

`LLM_GATEWAY_ADMIN_TOKEN` unset ⇒ `/admin/*` and `/metrics` are disabled, fail closed (503
`gateway_unavailable`), not silently open. A placeholder value (`changeme`, `password`, …) or one
under 16 characters refuses to start the service at all.

## Typed error contract

Every failure is `{"error": {"message", "type", "code", "param", "request_id", "attempts": [...]}}`
with a real HTTP status — never a bare 500 with no code. `attempts` lists
provider/model/outcome/latency for each try, **never prompt content**. Codes actually reachable
today: `invalid_request` (400), `capability_unavailable` (400, e.g. a vision request against a
text-only deployment), `model_not_found` (404), `key_invalid` (401), `model_not_allowed` (403),
`budget_exceeded` (429), `rate_limited` (429, `retry-after`), `guardrail_blocked` (400),
`locality_denied` (403), `queue_full` (429, `retry-after`), `upstream_unavailable`/
`upstream_timeout`/`upstream_error` (503/504/502), `stream_interrupted` (mid-stream only, as a
terminal SSE event, not an HTTP status), `config_invalid` (422, `/admin/reload` only),
`gateway_unavailable` (503). `context_overflow` and `model_loading` are declared in ADR 0156's
code table but nothing in the gateway currently raises them — no context-length check or
cold-start-specific signal exists yet, so treat them as reserved, not live. A blocked live stream
(BL-115) ends with `guardrail_blocked` as a terminal SSE error event, not an HTTP status — the
response is already `200` and streaming by the time enforce mode can detect it. Every response
also carries `x-request-id`, `x-examlops-route`, `x-examlops-provider`, `x-examlops-model`; a
cold-start response adds `x-examlops-cold: 1` (ADR 0153 d8); a cache-enabled response (streaming
or not) adds `x-examlops-cache: hit|miss` (a streaming miss only knows this after the cache
lookup, so the header is set before dispatch either way).

## Observability

`/metrics` (Prometheus exposition, admin-token gated):

| Metric | Kind | What |
|---|---|---|
| `llm_gateway_requests_total{route,provider,model,status,code}` | counter | Every response, by outcome. |
| `llm_gateway_request_seconds{route}` | histogram | End-to-end latency. |
| `llm_gateway_ttft_seconds{route,provider}` | histogram | Time to first token, including a cold load. |
| `llm_gateway_tpot_seconds{route,provider}` | histogram | `(total − TTFT) / (completion_tokens − 1)`; not recorded below 2 completion tokens or with no TTFT. |
| `llm_gateway_tokens_total{kind}` | counter | Prompt/completion tokens. |
| `llm_gateway_cache_total{result}` | counter | Hit/miss — only emitted while the cache is enabled. |
| `llm_gateway_policy_denials_total{reason}` | counter | key / model / budget / locality / rate_limit / guardrail. |
| `llm_gateway_config_reload_total{result}` | counter | `applied` / `rejected`. |
| `llm_gateway_breaker_state{provider,model}` | gauge | 0 closed, 1 half-open, 2 open. |
| `llm_gateway_inflight{provider,model}` | gauge | Requests currently in flight per deployment. |
| `llm_gateway_queue_depth{provider,model}` | gauge | Requests waiting for a bulkhead slot (only meaningful when `max_concurrency` is set). |
| `llm_gateway_provider_up{provider}` | gauge | 1 when the last active probe succeeded. |
| `llm_gateway_retries_total{reason}` | counter | Attempts beyond the first, by the prior attempt's failure kind. |
| `llm_gateway_fallbacks_total{from_provider,to_provider}` | counter | Deployment switches within one request. |

`examlops_llm_gateway.json` (Grafana `/d/examlops-llm-gateway`) charts most of these — see
`docs/guides/genai-observability.md` for what it shows and what it deliberately doesn't.
C1 GenAI spans (`gen_ai.*`, `examlops.cost.usd`) are emitted separately, into Tempo, by the
in-process client path (`GatewayClient`) — the standalone service does not currently emit its own
spans, only the metrics above.

## Known gaps (tracked, not silent)

- **Streaming enforce-mode redaction has a bounded lookback window, not perfect atomicity** — a
  block detected after partial content was already sent (BL-115) truncates the stream with a
  `guardrail_blocked` SSE event rather than un-sending the earlier bytes; this is a hard physical
  limit (TCP bytes already on the wire), not a design gap, and matches the smallest window that
  ADR 0026's D8 detectors need (64 raw chars, all well under any pattern's match length).
- **`/v1/embeddings`** is not implemented — chat only.
- **OpenAI-compatible upstream providers** (a real router, LiteLLM, OpenRouter, a remote vLLM) are
  not built — only the native Ollama adapter exists today (ADR 0152). `gateway.yaml`'s `type` field
  only accepts `ollama`.
- **`cost_aware` routing** uses a deployment's `price_per_1k` from `gateway.yaml` when one is
  declared, and falls back to locality otherwise (local/site free, external a flat marker cost).
  The price is a static figure the operator declares. Nothing pulls a live price list from an
  upstream provider. See [ADR 0083's LLMOps calculations](finops-providers.md#llmops-calculations-token-cost-cache-savings-routing-rag-quality-adr-0083),
  which also covers the `cost-latency` scorer.
- **Helm chart / release image matrix** for this service are not built; it runs via Compose only.

## See also

- [`model-gateway.md`](model-gateway.md) — the in-process `GatewayClient` library this service
  shares its policy code with (virtual keys, guardrails, cache, prompt registry, cost).
- [`agent.md`](agent.md) — how Skipper reaches this service (`AGENT_LLM_GATEWAY_URL`).
- [`serving-gateway.md`](serving-gateway.md) — the separate Envoy front door for predictive
  (non-LLM) model traffic.
- `docs/reference/env-vars.md` — every `LLM_GATEWAY_*`/`EXAMLOPS_GATEWAY_*`/`EXAMLOPS_GUARDRAIL_*`
  variable this service reads.

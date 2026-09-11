# Model gateway & multi-provider routing (B2)

One OpenAI-compatible gateway sits in front of every LLM backend — Anthropic, Ollama,
self-hosted vLLM/SGLang (E2) — with **weighted routing + failover**, per-tenant/project
**virtual keys** (allow-list + budget), per-call **C1 span + FinOps cost**, and a B3
semantic-cache hook. `exa gateway chat`, RAG (`exa rag query`) and the challenger judge
(`exa challenger`) go through the gateway client. **Skipper does not yet:** it builds its model
client directly from its own configuration, so gateway keys, budgets, guardrails and per-call cost
do not apply to its calls. `exa serve llm chat` also talks to an endpoint directly, by design.

Design: ADR 0010 · spec `design/vision/specs/B2-model-gateway.md`. `examlops.gateway` is an
in-process client + policy layer; no standalone gateway service ships with the stack. It works
standalone (backends are callables), so routing/governance/cost logic runs with no external service.

## Routing & failover

A logical model maps to an ordered set of concrete backends. On a backend error/timeout
the gateway fails over to the next; the routing table is hot-reloadable (swap `Router.routes`).

```python
from examlops.gateway import GatewayClient, Router

router = Router()
router.add_route("chat-default", [("anthropic", anthropic_backend), ("ollama", ollama_backend)])
client = GatewayClient(router, virtual_key=key, last_resort=ollama_backend)
comp = client.chat("chat-default", [{"role": "user", "content": "hi"}])
```

`last_resort` is the R11 degrade path: if the gateway/route is unreachable, the client
still answers from a configured local backend.

### Registered endpoints are routes

`build_default_router()` — the table `exa gateway chat`, `exa rag query` and
`exa serve challenger judge` use — holds an echo route under `EXAMLOPS_GATEWAY_DEFAULT_MODEL`
(`default`) plus one route per endpoint registered with
[`exa serve llm start`](vlm-serving.md), named after the endpoint. So a model served by
vLLM on a GPU node, a Compose service or a Slurm or Flux allocation (once its address is recorded)
is reachable with keys, budgets, guardrails, caching and cost applied:

```bash
exa serve llm start qwen --base-url http://gpu01:8000 --hf-model Qwen/Qwen3-8B
exa gateway chat qwen --message "hello"          # [endpoint:qwen] … (cost $…)
```

Stopped, disabled and not-yet-addressed endpoints are left out, and a down endpoint fails
with `AllBackendsFailed` rather than falling back to the echo route.

## Virtual keys — governance

Keys are scoped per **tenant/project** (D6) with an optional **model allow-list** and
**budget**. Only the key *hash* is stored; provider credentials come from the secrets
manager (D7), never `.env`. Every call is authorized **before** any backend is touched,
and returns a **typed error** on violation (never a generic 500):

- `BudgetExceeded` — the key is over budget (R6)
- `ModelNotAllowed` — model not on the key's allow-list (R7)
- `KeyInvalid` — unknown/revoked key (R5)

```bash
exa gateway key issue --tenant acme --project chat --budget 50 --model gpt-judge
exa gateway key list
exa gateway key revoke <key-hash>
exa gateway chat chat-default --message "hello" --key exa-...
```

Key issuance and revocation are audited (D4).

## Cost & telemetry

Each successful call emits a C1 GenAI span (`gen_ai.*` + `examlops.cost.usd`), records the
per-call cost to `platform_db.gateway_calls`, and increments the key's `spent_usd` so the
budget is enforced on the next call.

## Versioned prompts (ADR 0009)

```python
client.chat("gpt-4o", [{"role": "user", "content": q}], prompt_ref="support-bot@prod")
```

`prompt_ref` names a registry prompt (`name`, or `name@label` — default label `prod`). Its
template is prepended as a system message, so changing what the service says is an
`exa prompt label` move rather than a caller redeploy, and the serving version is recorded on
the span as `examlops.prompt.version`. The caller's own messages are never rewritten and their
list is not mutated. An unresolvable reference raises `LookupError` **before** any backend is
called, rather than silently serving the request without the prompt. See
[Prompt management](prompt-management.md).

## Structured output (ADR 0035)

Ask for a schema and the response comes back as a **validated object**, or not at all:

```python
comp = client.chat(
    "gpt-4o",
    [{"role": "user", "content": "Score this model"}],
    response_schema={"type": "object", "required": ["name", "score"]},
)
comp.parsed        # {"name": "JPCP", "score": 0.9} — validated
comp.text          # the raw completion, unchanged
```

A response that cannot be made to validate raises `StructuredOutputError` rather than returning
unchecked text. `max_repairs` (default 1) controls the repair attempts.

Parsing tolerates what models actually emit — a ``` fence, prose above the object — so the repair
budget is spent on data problems, not formatting habits. `parsed` is `None` only when no schema was
requested; a failure raises.

Two orderings matter: enforcement runs **after** the guardrail, so the validated object is the
redacted one (otherwise `text` and `parsed` would disagree about what the response said), and
**before** the cache, so an invalid response is never stored.

> **What this is not.** ADR 0035 clause 1 also calls for *constrained decoding* (guided decoding,
> grammars, provider structured-output APIs). None of that is in the tree, so the guarantee here is
> reached by parse-validate-repair after the fact rather than by constraining generation. The
> outcome for a caller is the same — a valid object or a typed error — but the model is not
> prevented from producing invalid output in the first place, and every repair costs a round of
> validation rather than nothing.

## Guardrails (D8, ADR 0026)

Every request through `GatewayClient.chat` is scanned on the way in and every response on the way
out. Default mode is `monitor` (observe + audit, no caller-visible change);
`EXAMLOPS_GUARDRAIL_MODE=enforce` blocks prompt injection and toxic output and redacts PII and
secrets, raising the typed `GuardrailBlocked`. A guardrail denial never fails over to the next
backend, and a blocked answer is never cached. See [Guardrails](guardrails.md).

## Semantic caching (B3)

> **Set `encoder_id` when you configure the cache.** A hit is decided by cosine similarity, so an
> entry embedded by one encoder and a query embedded by another are compared on axes that have
> nothing to do with each other — and the result is not a miss, it is a similarity *number*, which
> can clear the threshold by coincidence and return a cached answer to an unrelated question. With
> `encoder_id` set, entries from a previous encoder become unreachable and the next request
> recomputes: a miss, which is the cache working correctly, rather than an exception, which would
> be an outage caused by an upgrade. Old entries are hidden, not destroyed, so a rollback finds its
> cache intact (ADR 0043).

The gateway can return a stored completion for an embedding-**similar**, cacheable prompt
via the `cache_lookup`/`cache_store` hooks — no change to the caller API. Design: ADR 0018 ·
spec `design/vision/specs/B3-semantic-caching.md`.

- **Similarity**: the prompt is embedded and cosine-compared against stored entries; a hit
  requires similarity ≥ threshold (conservative default 0.85). Production uses a local
  embedder + Redis/Qdrant (B5); the fallback is an in-process cosine search over a
  deterministic token-hash embedding (no vector DB needed).
- **Isolation** (R3): the cache namespace is `tenant :: model | temperature | max_tokens`, so
  different tenants (D6), models, or params never collide.
- **Bypass** (R5): requests above the bypass temperature (default 0.5), with an explicit
  no-cache signal, or any side-effecting agent turn are neither served from nor written to
  the cache.
- **Eviction**: entries have a TTL and the cache enforces a max size (oldest-first).
- **Savings** (R7): every hit/miss is recorded to `platform_db.cache_events` with tokens +
  cost saved; C1 spans carry `cache_hit`.

```python
from examlops.gateway import GatewayClient
from examlops.semantic_cache import SemanticCache, bind_to_gateway

lookup, store = bind_to_gateway(SemanticCache(threshold=0.9), tenant="acme")
client = GatewayClient(router, cache_lookup=lookup, cache_store=store)
```

```bash
exa gateway chat chat-default --message "capital of France?" --cache
exa gateway cache stats                    # hit-rate + token/cost savings
exa gateway cache stats --tenant acme
```

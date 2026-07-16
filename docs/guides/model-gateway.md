# Model gateway & multi-provider routing (B2)

One OpenAI-compatible gateway sits in front of every LLM backend — Anthropic, Ollama,
self-hosted vLLM/SGLang (E2) — with **weighted routing + failover**, per-tenant/project
**virtual keys** (allow-list + budget), per-call **C1 span + FinOps cost**, and a B3
semantic-cache hook. All LLM callers (Skipper, the C2 judge, RAG, served LLMs) go through
the gateway client.

Design: ADR 0010 · spec `design/vision/specs/B2-model-gateway.md`. The production gateway
is **LiteLLM**; `examlops.gateway` is the client + policy layer, and works standalone
(backends are callables) so routing/governance/cost logic runs with no external service.

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

## Semantic caching (B3)

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

# Runbooks: llm-gateway

Alerts from the `examlops-llm-gateway` group: the standalone, containerized OpenAI-compatible
service every LLM consumer (Skipper, the dashboard copilot, `exa gateway chat`, RAG, and any
external OpenAI-compatible client) reaches models through (ADR 0151–0156).

The commands for every llm-gateway alert:

```bash
exa gateway status               # is the service up, and which routes can it currently serve?
exa gateway providers            # live per-provider health from the service's own active probes
exa gateway routes               # the full configured route table (deployments, fallbacks)
exa gateway models               # models currently servable, filtered to a non-open breaker
exa gateway reload                # re-read gateway.yaml; a rejected config keeps the last good one
```

The service's port `18020` is published on loopback (`docker compose --profile llm-gateway up -d
llm-gateway`). `/admin/*` and `/metrics` need `LLM_GATEWAY_ADMIN_TOKEN`; unset, both fail closed.

## LLMGatewayDown {#llmgatewaydown}

**Meaning:** Prometheus has not reached the llm-gateway service for 2 minutes.

**Impact:** every LLM consumer is unreachable — Skipper, the dashboard copilot, `exa gateway
chat`, RAG. The in-process `GatewayClient` (a separate library, `docs/guides/model-gateway.md`)
degrades to its own echo/direct-Ollama fallback and is not affected by this service being down.

**Check:** `docker compose ps llm-gateway`, then `docker compose logs llm-gateway --tail 200`.

**Fix:** restart the container; the logs name what stopped it (a bad `gateway.yaml` on startup
fails loudly, per `exa gateway validate`'s own offline check — run that first on the config the
container mounts).

## LLMGatewayProviderDown {#llmgatewayproviderdown}

**Meaning:** the active probe loop (`gateway/health.py`, independent of request traffic) has
failed to reach the named provider (the `provider` label) for 5 minutes.

**Impact:** every route backed by that provider fails over to its configured fallback, or fails
outright if it has none — `upstream_unavailable` on the routes that depend on it.

**Check:** `exa gateway providers` shows the same probe result and why it's failing (a `detail`
field naming the actual connect/timeout error). For an `ollama` provider, confirm the relay/host is
reachable from inside the container: `docker compose exec llm-gateway curl -sf
$EXAMLOPS_LLM_OLLAMA_URL/api/tags`.

**Fix:** by cause — the upstream process is down (restart it), a network path changed (the relay,
`host.docker.internal`, a firewall), or the configured `base_url` itself is wrong (`exa gateway
validate` catches an obviously malformed one, not a right-shaped but unreachable one).

## LLMGatewayBreakerOpen {#llmgatewaybreakeropen}

**Meaning:** the circuit breaker for one (provider, model) deployment (the `provider`/`model`
labels) has stayed open across several of its own cooldown cycles (5+ minutes), not just one that
recovered on its own.

**Impact:** every request to that specific deployment fails over to a configured fallback (or
fails, with none) without even attempting the deployment — by design, once open (ADR 0153 d3).

**Check:** `exa gateway providers` — if the provider itself is healthy, the breaker is reacting to
something narrower than the provider connection: a model that stopped answering, or a series of
`upstream_error`/`upstream_timeout` classifications specific to that model. Application logs on the
upstream (e.g. Ollama) usually name the actual failure.

**Fix:** fix the upstream deployment; the breaker closes itself once real requests start
succeeding again (a half-open trial admits a small number through to test recovery). There is no
manual "force closed" — forcing traffic back onto a genuinely broken deployment is what the breaker
exists to prevent.

## LLMGatewayHighErrorRate {#llmgatewayhigherrorrate}

**Meaning:** more than 10% of requests over the last 10 minutes got a non-`ok` typed error code
(ADR 0156 d1) — the warning half of a pair; see
[LLMGatewayVeryHighErrorRate](#llmgatewayveryhigherrorrate) for the critical half. Both can be
firing at once (the same convention `RayServeHighErrorRate`/`RayServeHighErrorRateCritical` already
use): Alertmanager's inhibit rule (critical mutes warning for the same `service`) is what prevents
a double page, not a gap between the two thresholds.

**Impact:** a meaningful share of callers are seeing failures — which ones, and how badly, depends
on which deployments are affected.

**Check:** `exa gateway providers` for a specific unhealthy provider; `exa audit --last 1h` for a
spike in a particular error code. The typed `code` on each failed response (visible to a caller,
and in `attempts` on a multi-attempt failure) usually names the exact cause without needing logs.

**Fix:** whatever the dominant error code names — a dead provider
([LLMGatewayProviderDown](#llmgatewayproviderdown)), an open breaker
([LLMGatewayBreakerOpen](#llmgatewaybreakeropen)), exhausted budgets/rate limits (an operator or
tenant issue, not an outage), or a bad config change (`exa gateway reload` to the last good one).

## LLMGatewayVeryHighErrorRate {#llmgatewayveryhigherrorrate}

**Meaning:** more than 50% of requests over the last 5 minutes got a non-`ok` typed error code —
the critical half of the pair above. Most callers are failing right now.

**Impact:** the gateway is effectively unusable for most traffic, even though the process itself
is still up and being scraped (otherwise [LLMGatewayDown](#llmgatewaydown) would also be firing).

**Check:** same first steps as [LLMGatewayHighErrorRate](#llmgatewayhigherrorrate), with more
urgency — `exa gateway providers` and `exa gateway routes` to see the whole picture of what's
still healthy versus what isn't.

**Fix:** same as the warning half; at this severity, consider whether a recent `exa gateway
reload` or upstream change coincides with the onset, and whether reverting it is faster than
diagnosing forward.

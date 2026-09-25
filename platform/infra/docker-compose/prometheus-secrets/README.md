# Prometheus scrape credentials

Prometheus reads each credential from a file in this directory (never from the committed
`prometheus.yml`), the same pattern `../alertmanager-secrets/` already uses for delivery
credentials. Real secret files are gitignored; only the `*.example` templates and this README
are tracked.

| File | Used by | Contents |
|---|---|---|
| `llm_gateway_admin_token` | the `llm_gateway` scrape job (`/metrics` is admin-token gated, ADR 0156) | The same value as `LLM_GATEWAY_ADMIN_TOKEN` in the stack's `.env` |

```bash
cd platform/infra/docker-compose/prometheus-secrets
printf '%s' "$LLM_GATEWAY_ADMIN_TOKEN" > llm_gateway_admin_token
docker compose restart prometheus
```

**If the file is absent or wrong**, the `llm_gateway` scrape target answers `401` and Prometheus
reports it in `up{job="llm_gateway"}` and its own target-health page — visible, not silent
(`LLMGatewayDown` fires the same as any other unreachable target). No other scrape job or alert
group is affected.

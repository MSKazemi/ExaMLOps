# Alertmanager delivery secrets

Alertmanager reads each credential from a file in this directory **at notification time**
(never from the committed `alertmanager.yml`). Real secret files are gitignored; only the
`*.example` templates and this README are tracked.

To enable a receiver, create the matching file (no trailing newline) and restart Alertmanager:

| File | Used by | Contents |
|---|---|---|
| `slack_api_url`        | warning + critical Slack | Slack incoming-webhook URL (`https://hooks.slack.com/services/…`) |
| `pagerduty_routing_key`| critical PagerDuty       | PagerDuty Events API v2 routing key |
| `heartbeat_url`        | heartbeat (Watchdog)     | Dead-man's-switch ping URL (healthchecks.io / PagerDuty heartbeat / Grafana Cloud) |

```bash
cd platform/infra/docker-compose/alertmanager-secrets
printf '%s' 'https://hooks.slack.com/services/T000/B000/XXXX' > slack_api_url
printf '%s' 'R0YOURPAGERDUTYROUTINGKEY'                        > pagerduty_routing_key
printf '%s' 'https://hc-ping.com/your-uuid'                    > heartbeat_url
docker compose restart alertmanager
```

**Any file you omit degrades gracefully:** that one receiver becomes a no-op and Alertmanager
logs a send error, but the config still loads and every other route keeps delivering.

## Proving delivery (the heartbeat)

The `Watchdog` alert (in `alert_rules.yml`) always fires and is routed to `heartbeat_url` every
~50s. Register that URL with an external dead-man's-switch that expects a ping at least once a
minute. If ExaMLOps → Prometheus → Alertmanager → the receiver ever breaks, the pings stop and
the *external* monitor alerts you — catching the failure mode where the alert pipeline itself is
down (which no internal alert can report).

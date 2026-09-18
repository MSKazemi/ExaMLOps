# Control Plane

The ExaMLOps control plane (`platform/services/control_plane/app.py`) is the command and approval
gateway in front of Prefect. It validates retrain requests, persists approval and ModelZoo freshness
state, and dispatches accepted work. Training, inference, model promotion, and most platform domain
state remain outside this service.

## Responsibilities

- Validate model and dataset names against the active use-case registry before calling Prefect.
- Store and resolve pending model-change approvals.
- Receive authenticated GitLab/GitHub ModelZoo webhooks and track model freshness.
- Poll ModelZoo when enabled, expose service diagnostics, and publish approval/retrain metrics.
- Serve read-only model metadata and README/image assets without importing training code.

State is selected with `EXAMLOPS_DB_BACKEND`. SQLite at `CONTROL_PLANE_DB` is the local default;
Postgres uses `EXAMLOPS_POSTGRES_DSN` and shares approvals, events, and freshness across processes.
Postgres removes the private SQLite persistence island, but it does **not** by itself permit safe
multi-replica control-plane operation.

## Runtime topology and maturity

```text
CLI / dashboard / CI webhooks
             │
             ▼
     Control Plane :18002
       ├─ SQLite or Postgres state
       ├─ durable command claims, dispatch records, admission records, and event outbox
       ├─ configured DB/Redis coordinator for rate limits and leases
       ├─ leased ModelZoo poller and in-process Prefect circuit breaker
       ├─ transactional outbox relay to the configured publisher
       └─ PrefectGateway ──► Prefect ──► training flows
```

Retrain and approval dispatches use durable command keys, expiring dispatch leases, and Prefect
idempotency keys. A completed command can replay its stored response, and a replacement process can
recover an expired dispatch without deliberately creating a second flow run. Command completion,
approval state, its admission record, and its outbox event are committed together. The outbox relay
is at least once; consumers deduplicate with the stable event ID.

Rate limiting and retrain/poller ownership use the configured shared coordinator. SQLite with the DB
coordinator is host-local; Postgres or Redis is required for cross-host coordination.

### Running more than one replica

Everything a second replica must agree with the first on lives in the shared state store:

- **Retrain deduplication:** the "already in progress" check runs inside the insert's lock, so two
  racing submissions through two replicas create one command and one gets 409.
- **Rate limits, retrain locks and the singleton leases** (ModelZoo poller, snapshot projector):
  the shared coordinator.
- **Runtime settings** set with `PUT /v1/modelzoo/config`: the `control_plane_settings` table. Each
  replica re-reads them at most every `CONTROL_PLANE_SETTINGS_TTL_SECONDS` (5 s), and every change
  is audited (`modelzoo_config_updated`).
- **Commands and events:** claimed atomically, so any replica's workers and relay can take them.

`/health` computes `runtime.horizontal_scaling_safe` from the configuration that decides it, and
lists `runtime.horizontal_scaling_blockers` when it is false:

| Blocker | Means | Remove it with |
|---|---|---|
| `state_not_shared` | State is in a per-host SQLite file | `EXAMLOPS_DB_BACKEND=postgres` |
| `coordination_not_cross_host` | The DB coordinator on SQLite | Postgres (or `EXAMLOPS_COORDINATOR=redis`) |
| `event_relay_disabled` | No relay publishes the outbox | `CONTROL_PLANE_EVENT_RELAY_SECONDS` > 0 |
| `event_publisher_process_local` | Events go to this process's log | `EXAMLOPS_EVENT_PUBLISHER=nats` |
| `event_publisher_not_implemented` | The `kafka` publisher | `nats` |

`runtime.horizontal_scaling_notes` names two things that are true but not blockers: the Prefect
circuit breaker is per replica (each protects itself), and a synchronous `POST /retrain` without an
`Idempotency-Key` is a new request on every retry, on one replica as on three.
`tests/integration/test_postgres_backend_live.py` runs two control-plane instances on one Postgres
and checks both the shared settings and the one-command race.

**A replica that dies holding a command.** A worker claims a command for one lease
(`CONTROL_PLANE_COMMAND_LEASE_SECONDS`, 60 s; a dispatch itself ends within
`CONTROL_PLANE_DISPATCH_BUDGET_SECONDS`, 8). When the claimer dies, the claim runs out and any
replica's worker takes the command over. It dispatches again with the same Prefect idempotency key,
so a run the dead replica did create is returned rather than created twice. A command that has
taken down its dispatcher `CONTROL_PLANE_COMMAND_MAX_ATTEMPTS` times is buried (`dead`) instead of
being passed on. A synchronous command is not retried in the background: it becomes `failed`, so it
stops counting as a retrain in progress, and the caller's retry with the same key picks it up.

**The failover drill.** `tests/integration/test_control_plane_failover_kind_live.py`
(`EXAMLOPS_KIND_FAILOVER_LIVE=1`) runs three replicas from this chart in a kind cluster on Postgres,
with a Prefect stand-in that takes 1.5 s per dispatch. A client submits retrains through the Service
without pause, and twice the drill SIGKILLs the replica that is in the middle of a dispatch. In one
run, 141 retrains were accepted and all 141 succeeded, each with exactly one flow run: two crashed
dispatches were taken over with their original key. The longest gap between accepted submissions
was 3.7 s, and the client saw no errors. Its first run found the defect this section describes as
fixed: queued commands whose replica crashed stayed `dispatching` forever, and their model ×
dataset pair refused every later retrain with 409.

The Helm chart still defaults to one replica, because with the default `log` event publisher
`/health` reports `event_publisher_process_local`. With Postgres and NATS, raise
`controlPlane.replicaCount`, and run the drill against your own cluster first.

## Health and readiness

| Endpoint | Meaning |
|---|---|
| `GET /livez` | Process liveness. Use for restart decisions. |
| `GET /readyz` | Traffic readiness. Returns 503 when this replica could not serve a request: see what decides it below. |
| `GET /ready` | Compatibility alias for the original liveness endpoint. |
| `GET /health` | Diagnostic JSON. Always inspect its `status`, `startup_checks`, `poller`, and `runtime` fields. |
| `GET /status` | Concurrent peer-service probes plus the pending-approval count. |

`/health` returns HTTP 200 even when its JSON verdict is `starting` or `degraded`; it is a diagnostic
surface. Orchestrators must use `/readyz`, which encodes the verdict in the HTTP status. The Compose
healthcheck currently probes `/health`, while the Helm chart correctly separates `/livez` and
`/readyz`; use the latter pair for production scheduling.

**What makes a replica unready** is narrower than what makes it degraded, because readiness answers
"send this replica requests?", not "is everything well?".

| Failing | `/health` | `/readyz` | Why |
|---|---|---|---|
| The datastore (`db`), the credential (`token`), the coordinator | `degraded` | **503** | Without them this replica can serve nothing |
| The approval store cannot be read | `degraded` | **503** | Same store, seen at request time rather than at boot |
| An enabled ModelZoo poller is stale, or cannot coordinate | `degraded` | **503** | More traffic to a replica whose reconciliation stopped only delays recovery |
| The event publisher, or the relay | `degraded` | 200 | Events wait in the durable outbox and go out when the broker is back. Pulling every replica over a broker would turn a degraded bus into an unavailable API — the [backbone chaos drill](../guides/testing.md#chaos-drills) found exactly that |
| The registry has no enabled models | `degraded` | 200 | A fresh stack, before its first model |
| Identity federation's trust file is invalid | `degraded` | 200 | Federated tokens are refused; static credentials keep working |
| The retrain dispatch target is missing | `degraded` | 200 | Approvals, reads and the durable command record still work |

The checks run at boot. They run again, at most every `CONTROL_PLANE_STARTUP_RECHECK_SECONDS`, while
one of them is failing **or** while the event relay cannot publish — the platform's own evidence that
the broker is gone, which arrives within a second. The publisher check asks the broker (a bounded
connect within `EXAMLOPS_NATS_TIMEOUT`), so a dead bus shows up in `startup_checks.event_publisher`
rather than being reported as fine.

## API and authentication

`CONTROL_PLANE_CREDENTIALS_JSON` maps bearer secrets to a server-verified `principal`, `tenant`, and
`scopes` list. `read` authorizes sensitive status, approval, and ModelZoo reads; `write` authorizes
mutations. Approval queries and mutations are tenant-filtered, and structured credentials may read
only flow runs dispatched for their tenant. The legacy `CONTROL_PLANE_TOKEN` remains compatible as
principal `legacy` in tenant `default` with both scopes. Malformed structured configuration fails
closed, including for the legacy credential. Public operational and registry surfaces are limited
to health/readiness, metrics, `/models`, and model metadata/assets.

### Retiring the shared legacy token

The legacy token carries `read` and `write`, which includes every action, so it outranks every
narrowly scoped service credential. It is shared by default, too: each service falls back to it
when it has no credential of its own. `CONTROL_PLANE_LEGACY_TOKEN` retires it in steps:

| Mode | The legacy token |
|---|---|
| `on` (default) | Accepted, as before |
| `warn` | Accepted. Each use is counted, and at most once a minute the log names the caller (address, user agent, path) |
| `off` | Refused with `403`; every other credential keeps working. Any value other than these three counts as `off`, and the startup check fails so the typo is visible |

Every mode counts uses in `control_plane_legacy_token_uses_total`. To retire it:

1. Give each caller its own credential in `CONTROL_PLANE_CREDENTIALS_JSON`, with only the scopes it
   needs, and point the service at it (`SEANERBUS_BRIDGE_CONTROL_PLANE_TOKEN`,
   `AUTOPILOT_CONTROL_PLANE_TOKEN`, `AGENT_CONTROL_PLANE_TOKEN`, `DASHBOARD_CONTROL_PLANE_TOKEN`).
2. Set `CONTROL_PLANE_LEGACY_TOKEN=warn` and read the log for anything still using it.
3. When `increase(control_plane_legacy_token_uses_total[1d])` stays 0, set it to `off`.

`/health` reports the mode under `runtime.legacy_token`.

### Workload identities (SPIFFE)

A static credential is a secret that lives until someone changes it, and whoever holds a copy *is*
that service. A service can instead prove who it is with a **JWT-SVID**: a token its
[SPIRE](https://spiffe.io/docs/latest/spire-about/) agent issues to it, for one audience, valid for
minutes. The control plane verifies it and maps the workload's SPIFFE ID to a principal, tenant
and scopes, exactly as a static credential is mapped:

```bash
EXAMLOPS_SPIFFE_TRUST_DOMAIN=examlops.internal
EXAMLOPS_SPIFFE_BUNDLE=/run/spire/bundle.json      # spire-server bundle show -format spiffe
CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON='{"spiffe://examlops.internal/autopilot":
  {"principal": "autopilot", "tenant": "default", "scopes": ["read", "retrain"]}}'
```

The verification checks:
- the signature, against a JWT key in the trust bundle (a changed bundle file is re-read, so
  SPIRE's key rotation needs no restart);
- the algorithm: asymmetric only, never `none` or HMAC;
- the audience (`CONTROL_PLANE_SPIFFE_AUDIENCE`, default `control-plane`);
- the expiry;
- that the SPIFFE ID is inside the configured trust domain.

What happens to a request:
- A JWT-SVID that fails any check, or names a workload not in the map, is refused with `403`. It
  is never passed on to identity federation.
- Static credentials keep working alongside, so services move one at a time.
- A map without a trust bundle fails the startup check.

`tests/integration/test_workload_identity_spire_live.py` (`EXAMLOPS_SPIRE_LIVE=1`) runs a real
SPIRE server and agent, fetches an SVID from the agent's Workload API, and checks that the control
plane accepts it with the mapped scopes and refuses one issued for another audience.

**Sending one.** A platform service sends the credential in `CONTROL_PLANE_TOKEN_FILE` when that is
set, and reads the file on every call. SPIRE's
[`spiffe-helper`](https://github.com/spiffe/spiffe-helper), running beside the service, keeps the
file fresh: its `jwt_svids` setting fetches a JWT-SVID for the `control-plane` audience and
rewrites the file before the SVID expires. The CLI and the autopilot follower, the bus bridge, the
dashboard and the agent all read it this way. A configured file that does not exist yet, or is
empty, falls back to the static credential, so a service can be moved to its workload identity
before SPIRE has issued one. The same variable serves any rotated secret, such as a Kubernetes
Secret mounted as a file.

**On Docker Compose** the `docker-compose.identity.yml` overlay runs SPIRE and one spiffe-helper
per service, and wires all of the above. **On Kubernetes** the chart's `workloadIdentity` values do
the same against the cluster's SPIRE: a `ClusterSPIFFEID` and a CSI-mounted spiffe-helper per tier.
See [Workload identity](../guides/workload-identity.md).

**Seeing which credential was used.** Each authenticated request is counted in
`control_plane_authentications_total{principal, method}`. `method` is `static`, `legacy`,
`workload` or `federated`, and every configured principal's series starts at 0. The audit events
for retrains (queued ones too), approvals and cancellations carry `credential`, plus `spiffe_id` for a workload. A
service whose `static` series has stopped growing no longer needs its secret.

ModelZoo webhooks use their separate `MODELZOO_WEBHOOK_SECRET`: GitLab sends `X-Gitlab-Token`, while
GitHub sends an `X-Hub-Signature-256` HMAC.

See the [Control Plane guide](../guides/control-plane.md) for the complete endpoint table, examples,
configuration, and Prometheus metrics.

## Local development

```bash
make control-plane-up
curl http://localhost:18002/livez
curl --fail http://localhost:18002/readyz
make control-plane-logs
```

## Code layout

`platform/services/control_plane/app.py` is the composition root: it builds the FastAPI app,
declares the routes and starts the background workers (ModelZoo poller, outbox relay, command
workers). Focused modules live in `cplane/`:

| Module | Contents |
|---|---|
| `cplane/schema.py` | The control plane's own tables, indices and additive column migrations |
| `cplane/models.py` | Request and response models of the HTTP API, including `/v1` command views |
| `cplane/gateway.py` | The Prefect client: pooled httpx, one deadline per dispatch, circuit breaker |
| `cplane/problems.py` | RFC 9457 problem documents for `/v1` |

`app.py` re-exports every name these modules define, so `app.<name>` keeps working for callers and
tests while the remaining sections move out.

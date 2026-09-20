# Serving gateway

The serving gateway is the one front door to model inference. It is an [Envoy](https://www.envoyproxy.io/)
proxy in front of Ray Serve that checks a credential on every request, applies a quota per tenant,
bounds request size and time, and passes the verified tenant on to the model server. Ray Serve's
own ports stay internal. Design record: ADR 0126.

```text
client ──▶ gateway :18088 ─ rate ceiling ─ body limit ─ authorization ─ router ──▶ ray-serving
                              (429)          (413)      (401/403/429)     (timeouts, retries
                                                                          within a budget)
```

## Turn it on

The gateway is an opt-in Compose profile; the default stack does not change.

```bash
cd platform/infra/docker-compose
docker compose --profile gateway up -d gateway gateway-authz
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:18088/v2/health/ready   # 200
```

Two services run:

| Service | Image | Does |
|---|---|---|
| `gateway` | `envoyproxy/envoy:v1.39.1` (pinned by digest) | The proxy. Its configuration is `platform/infra/docker-compose/gateway/envoy.yaml` |
| `gateway-authz` | the control-plane image | `examlops.serving_gateway`: decides each request for Envoy (`ext_authz`) |

With the [segmented networks](production-hardening.md) overlay, the gateway sits on the `control`
and `ops` zones, and the authorization service on `db`, `control` and `ops` (`ops` is where
Prometheus scrapes).

**On Kubernetes** the Helm chart runs the same two tiers:

```yaml
gateway:
  enabled: true
  upstream: {host: ray-serving, port: 8001, grpcPort: 8081}   # the model server; not deployed by the chart
  ingress:
    host: inference.example.org               # optional: its own host on the chart's Ingress
```

- **The same configuration.** Envoy runs `files/gateway-envoy.yaml`, a byte-identical copy of the
  Compose file (a unit test fails if the two differ), with only the addresses of the model server
  and the authorization service substituted. A configuration change rolls the Envoy pods.
- **Hardened like every tier.** Two replicas each, a PodDisruptionBudget, the chart's non-root
  read-only pod security (Envoy runs without hot restart, which would need shared memory), and
  TCP probes on the serving port, since the admin interface stays on loopback.
- **Authorization.** The authorization tier runs the control-plane image and reads the virtual
  keys from the platform datastore through the control plane's Secret. `gateway.tenantRpm` and
  `gateway.keyCacheSeconds` set its quota and cache.
- **Network isolation.** With `networkPolicy.enabled`, the gateway takes clients from the ingress
  controller and Prometheus on 9902, and reaches only the authorization tier and the model server
  (`networkPolicy.egressPorts.gateway`, REST and gRPC). The authorization tier takes requests from
  the gateway alone.
- **Metrics.** With `metrics.serviceMonitor.enabled`, a second ServiceMonitor scrapes Envoy's
  `/stats/prometheus`.
- **Verification.** `tests/integration/test_helm_gateway_kind_live.py`
  (`EXAMLOPS_KIND_GATEWAY_LIVE=1`) runs the chart in a kind cluster. It checks that both tiers are
  Ready under that security, that a virtual key reaches the model server with the verified tenant,
  and that anonymous and admin requests are refused.
- **Plaintext hop.** The hop to the model server is plaintext on Kubernetes: the mutual-TLS
  sidecar (see [Workload identity](workload-identity.md)) is deployed next to the model server,
  which the chart does not run.

## What it serves

Only inference is routed. Everything else answers `404` at the gateway, without authorization and
without spending quota.

| Path | Upstream |
|---|---|
| `/v2`, `/v2/…` | Ray Serve's [Open Inference Protocol v2](../components/ray-serve.md#open-inference-protocol-v2) |
| `/inference.GRPCInferenceService/…` (gRPC) | Ray Serve's [OIP v2 over gRPC](../components/ray-serve.md#open-inference-protocol-v2-over-grpc), port 8081 |
| `/infer-pipeline/infer` | the inference pipeline |
| `/predict/{model}` | the deprecated legacy route |

Not reachable through the gateway: Ray Serve's `/reload`, `/traffic-rules`, `/metrics` and its
dashboard, and Envoy's own admin interface (loopback-only inside the container).

**gRPC** uses the same port, `:18088`: the listener speaks HTTP/1.1 and cleartext HTTP/2. A gRPC
request passes the same ceiling, body limit, authorization and tenant quota as REST. The client's
deadline applies, capped at 35 s. Retries on `UNAVAILABLE` stay within the same retry budget.

```bash
grpcurl -plaintext -H "authorization: Bearer exa-…" \
  -import-path serving/oip_grpc -proto open_inference_grpc.proto \
  -d '{"model_name": "jpcp", "inputs": [{"name": "input-0", "datatype": "FP64", "shape": [1, 3],
       "contents": {"fp64_contents": [1.2, 0.8, 3.4]}}]}' \
  localhost:18088 inference.GRPCInferenceService/ModelInfer
```

- **Open like REST's health:** `ServerLive`, `ServerReady` and `ServerMetadata` need no credential.
- **The credential** travels as `authorization` metadata.
- **Refusals** arrive as gRPC status codes: `UNAUTHENTICATED` (no or a bad credential),
  `PERMISSION_DENIED` (refused) and `UNIMPLEMENTED` (any other gRPC service).
- **The body is never read.** gRPC names its model in the message body, so a key with a model
  allow-list cannot use gRPC, and with multi-tenancy on gRPC is refused. Use
  `/v2/models/{name}/infer` there.

## Credentials

Every request except `GET /v2` and `GET /v2/health/{live,ready}` needs
`Authorization: Bearer <credential>`. Two kinds are accepted:

**A platform virtual key** (`exa-…`). It carries a tenant, a project, an optional model allow-list and
an optional budget:

```bash
exa gateway key issue --tenant acme --project research --model jpcp --budget 100
curl -s http://localhost:18088/v2/models/jpcp/infer \
  -H "Authorization: Bearer exa-…" -H "Content-Type: application/json" \
  -d '{"inputs": [{"name": "input-0", "shape": [1, 3], "datatype": "FP64", "data": [1.2, 0.8, 3.4]}]}'
```

A key with an allow-list works only on routes that name a model (`/v2/models/{name}/…`), because
only those can be checked against it. Revoke a key with `exa gateway key revoke <hash>`.

**From `exa`.** Point the CLI's serving URL at the gateway and give it the credential:

```bash
exa config set ray_serve http://localhost:18088
exa config set serving_token          # a hidden prompt; or set EXAMLOPS_SERVING_TOKEN
exa serve batch submit jpcp rows.json
```

- **Which commands send it:** `exa serve batch submit`, `exa serve loadtest`, `exa predict` and
  `exa serve infer-check`.
- **Where it goes:** only to URLs under `ray_serve`. It never reaches the control plane, MLflow,
  Prefect or a `--url` elsewhere, and a request that already carries a credential keeps its own.
- **What the gateway does not route:** `exa serve reload` and `exa serve traffic`. They need the
  model server's admin routes.
- **With multi-tenancy on:** `exa predict` and `exa serve infer-check` name their model only in the
  body, so the gateway refuses them (see below). Use `exa serve batch` or `exa serve loadtest`,
  which call `/v2/models/{name}/infer`.

**An access token from your data center's identity provider**, when
[identity federation](identity-federation.md) is configured. The token is verified like everywhere
else on the platform. The caller needs the `serving.infer` permission, which any authenticated role
has; the center's policy decision point can narrow it per model.

## Projects and tenants

With multi-tenancy on (`EXAMLOPS_MULTITENANCY=1`, the same switch as the rest of the platform's
[relationship authorization](projects-workspaces.md)), the gateway enforces a model's project at
serve time:

| The model | Allowed |
|---|---|
| belongs to project P (`exa project assign P MODEL --kind model`) | a virtual key issued for project P, or a token whose principal has a role in P: from the token's own project claims, or from a membership (`exa project add-member P <principal> --role viewer`) |
| belongs to no project | any authenticated caller |

- Names are matched case-insensitively, so a model assigned as `JPCP` is scoped when called as
  `jpcp`.
- A request for another project's model is refused with `403` and the owning project named.
- `/infer-pipeline/infer` names its model in the body, which the gateway does not read, so under
  multi-tenancy it is refused. Call `/v2/models/{name}/infer`.
- A model's project is cached for `EXAMLOPS_GATEWAY_SCOPE_CACHE_SECONDS` (30). If the platform
  store cannot be read and nothing is cached, the request is refused (`503`): an outage never
  turns a scoped model into an open one.
- Without multi-tenancy, projects do not restrict serving, as in a single-tenant install.

## What reaches the model server

An allowed request is forwarded with three headers set from the verified identity:

| Header | Value |
|---|---|
| `X-ExaMLOps-Tenant` | the key's tenant, or the token's tenant |
| `X-ExaMLOps-Principal` | `key:<hash prefix>`, or the token's `<provider>:<user>` |
| `X-ExaMLOps-Project` | the key's project, or, for a token, the model's project when it has one |

They are always set on an allowed request, and Envoy overwrites a client's own copy, so a client
cannot name its own tenant.

**Over mutual TLS.** With the workload-identity overlay (`-f docker-compose.identity.yml`), the hop
from the gateway to the model server is mutual TLS 1.3, for REST and for gRPC (over HTTP/2). The gateway presents its SPIFFE identity to
an Envoy beside the model server, which forwards over loopback and adds `x-forwarded-client-cert`
naming the gateway. The model server then listens on loopback only, so the gateway is the one way
in from outside the platform: `:18001` is no longer published. The gateway's identity may infer
but not use the model server's admin routes. See
[Workload identity](workload-identity.md#every-hop-to-the-model-server-mutual-tls).

## Limits

| Limit | Value | Answer | Change it in |
|---|---|---|---|
| Requests per tenant per minute, across all gateway replicas | 600 | `429` with `Retry-After: 60` | `EXAMLOPS_GATEWAY_TENANT_RPM` (`0` turns it off) — the default; a tenant's own limit is `exa gateway quota set <tenant> <rpm>` (`0` = unlimited), carried to the gateway in the serving snapshot ([guide](serving-snapshot.md)) |
| Requests per second for the whole gateway | 2000 | `429` with `Retry-After: 1` | `envoy.yaml`, `local_ratelimit` |
| Request body | 8 MiB | `413` | `envoy.yaml`, `buffer.max_request_bytes` |
| Time to answer | 35 s per attempt | `504` | `envoy.yaml`, route `timeout` |
| Open client connections | 50 000 | refused | `envoy.yaml`, `overload_manager` |

Transport failures and `503` from Ray Serve (a replica shedding load) are retried at most twice.
Retries may add at most 20 % to the traffic the model server sees, so an overloaded server is not
buried under retries of its own refusals. A `500` is not retried: Envoy cannot tell a model that
failed from a replica that died during the request, and retrying the first only repeats it. A
caller that gets a `500` may retry; inference has no side effects on the server
([when a replica dies](../components/ray-serve.md#replicas-and-scaling)).

## When a model server stops answering

A model server that *refuses* a connection is the easy case: Envoy retries `connect-failure` and
`reset` on another endpoint, and the caller sees nothing. The expensive case is one that accepts the
connection and then says nothing — a pod on a node that lost power, an OOM-killed process whose
socket is still open. Every request routed there costs the caller its whole deadline.

The gateway checks for this itself, so it does not have to wait for the orchestrator:

| | |
|---|---|
| Probe | `GET /ready` on each model-server endpoint, every 5 s, 2 s timeout |
| Ejected after | 2 failed probes — about **10 s**, measured 8.4 s |
| Back in service after | 2 successful probes, with no restart — measured 9.4 s |
| Never ejected | more than half the endpoints, and never the last healthy one |

Measured by `tests/integration/test_serving_gateway_ejection_live.py`, which wedges one of two
endpoints so it accepts connections and never answers: **10 to 13 of 20 requests timed out** without these
checks, and none with them.

Why this matters beyond the gateway: Kubernetes takes about **two minutes** to stop routing to a pod
whose node has gone ([when a whole node goes away](serving-on-kubernetes.md#when-a-whole-node-goes-away)),
and a client-side retry cannot help, because it is routed by the same Service and can land on the same
dead address. The gateway is the piece that can notice.

End to end, on a three-node cluster with a node stopped outright
(`tests/integration/test_serving_node_loss_kind_live.py`), over two runs: a caller straight at the
Service saw failures for **118-127 s**; the same caller behind the gateway lost **5-10 requests** of
about 32 000, inside **0.1-10 s**, and those were clean `503`s rather than hangs. The wide end of
that window is this table's own detection time.

**For it to notice per pod, it must see pods.** Pointed at an ordinary `ClusterIP`, Envoy resolves one
address — the Service's — and there is nothing to eject; `kube-proxy` picks the pod. Point
`gateway.upstream.host` at a **headless** Service (`clusterIP: None`) and Envoy resolves one endpoint
per pod, which is what makes the table above apply to a single sick replica. With one endpoint and no
alternative, Envoy's panic threshold keeps sending requests to it rather than answering
"no healthy upstream" — a single-container deployment whose model is still loading must not be taken
out of service by its own proxy.

The gRPC route (`/inference.GRPCInferenceService/`) has **no active health check**: `/ready` is HTTP/1
on the REST port, and the model server does not serve `grpc.health.v1`. It falls back to outlier
detection, which needs failures to count before it ejects — so a gRPC caller should carry a deadline
(`grpc-timeout`) rather than rely on the proxy noticing first.

## When a dependency is down

The gateway follows the serving plane's rule that inference keeps working when the platform around
it is down:

- **The platform datastore is unreachable.** A virtual key verified in the last
  `EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS` (60) keeps working. A key the gateway has not seen answers `503`.
  Nothing is ever allowed without a credential.
- **The quota counter is unreachable.** Requests with valid credentials go through: the quota is a
  fairness control, and the credential has already been checked.
- **The authorization service is unreachable.** Envoy answers `503` (fail closed).

## Answers from the gateway

Errors carry `{"error": "…"}`, the Open Inference Protocol shape.

| Status | Means | Do |
|---|---|---|
| `401` | No credential, an unknown or revoked key, or an invalid token | Send `Authorization: Bearer …` with a valid credential |
| `403` | A key used outside its allow-list or its project, an exhausted budget, a token without a role in the model's project, or a route that names no model under multi-tenancy | Use a key or principal of the model's project, or raise the budget |
| `404` | The path is not an inference route | Use `/v2/models/{name}/infer` |
| `413` | Body over 8 MiB | Send smaller batches |
| `429` | Tenant quota or gateway ceiling | Wait for `Retry-After` |
| `503` | Authorization unavailable, or the credential store unreachable for a key it has not seen | Check `gateway-authz` |

## Monitoring

Prometheus scrapes both services when they run. It finds them by DNS, so a site without the
gateway gets no "target down" alert for it:

| Job | Target | What it holds |
|---|---|---|
| `gateway` | `gateway:9902/stats/prometheus` | Envoy: answers by status class, authorization errors, ceiling refusals, upstream retries. That listener serves this one path only |
| `gateway_authz` | `gateway-authz:8090/metrics` | `examlops_gateway_decisions_total{status}`: every decision, by the status it returned |

Alerts, each with a runbook section:

| Alert | Fires when |
|---|---|
| [ServingGatewayDown](../runbooks/serving.md#servinggatewaydown) | Envoy or `gateway-authz` cannot be scraped for 2 minutes |
| [ServingGatewayAuthorizationFailing](../runbooks/serving.md#servinggatewayauthorizationfailing) | Envoy gets no decision from `gateway-authz` (each such request is refused with `503`) |
| [ServingGatewayCredentialStoreUnavailable](../runbooks/serving.md#servinggatewaycredentialstoreunavailable) | `gateway-authz` answers `503` because it cannot read the platform store |
| [ServingGatewayHighErrorRate](../runbooks/serving.md#servinggatewayhigherrorrate) | More than 5 % of answers are `5xx` for 10 minutes |
| [ServingGatewayAtCeiling](../runbooks/serving.md#servinggatewayatceiling) | The whole-gateway request ceiling is refusing traffic |
| [ServingEndpointsUnhealthy](../runbooks/serving.md#servingendpointsunhealthy) | The gateway has ejected a model-server endpoint: inference is served by fewer pods than exist |
| [ServingNoHealthyEndpoints](../runbooks/serving.md#servingnohealthyendpoints) | No endpoint answers `/ready` — and requests still go out, so this alert is the signal, not the error rate |

Useful queries:

```promql
# answers by status class
sum by (envoy_response_code_class) (rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="serving"}[5m]))
# decisions by outcome
sum by (status) (rate(examlops_gateway_decisions_total[5m]))
# endpoints the gateway is willing to use, against how many it knows about
envoy_cluster_membership_healthy{envoy_cluster_name="ray_serving"}
envoy_cluster_membership_total{envoy_cluster_name="ray_serving"}
```

The last pair is what the ejection drill reads back: with one of two endpoints wedged it measured
`healthy 1 / total 2` within about eight seconds, and `2 / 2` again nine seconds after the endpoint
recovered — so the alert fires on a real ejection and clears itself without anyone acting.

## Operating it

- `gateway-authz` exposes `GET /healthz`, `GET /readyz` and `GET /metrics` on port 8090.

### Liveness and readiness answer different questions

They are deliberately different endpoints, because the wrong one on the wrong probe causes the
opposite failure:

| Endpoint | Asks | Touches the credential store |
|---|---|---|
| `GET /healthz` | is the process answering? | **no** |
| `GET /readyz` | has this replica ever read the credential store? | until it succeeds once |

`/readyz` performs the real read path with a digest no key can hash to, so a reachable store
answers "no such key" and an unreachable one raises. **The answer latches.** Both halves matter:

- **Until the first success** the replica can only refuse with `503`, so it must stay out of the
  Service. That is what stops a rolling update from replacing working replicas with ones that
  authorize nothing — before 2026-09-14 both probes pointed at a constant `/healthz`, so such a
  rollout completed as a success and every request 503'd.
- **After the first success** a later store outage must *not* un-ready it. The store is shared, so
  every replica would leave rotation together and clients would get connection errors instead of a
  `503` they can read — and the verified-key cache above is what carries traffic through a blip.
  For the same reason liveness never touches the store: a restart throws that cache away.

- `tests/unit/test_serving_gateway_config.py` holds `envoy.yaml` to the rules on this page: filter
  order, fail-closed authorization, only the credential sent to the authorization service, the
  identity headers, loopback admin, only inference routed.
- `tests/integration/test_serving_gateway_live.py` runs the real Envoy against the real authorization
  service (`EXAMLOPS_GATEWAY_LIVE=1`, needs Docker). Its last test stops the authorization service
  and checks that requests are refused with `503` and counted in the metric the alert reads.
- `tests/integration/test_prometheus_optional_targets_live.py` (`EXAMLOPS_PROMETHEUS_LIVE=1`) runs
  the platform's Prometheus with these scrape jobs and shows the gateway discovered when it runs
  and reported down when it stops.

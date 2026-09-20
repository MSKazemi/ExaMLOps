# Control Plane

`platform/services/control_plane/app.py` is the FastAPI command and approval gateway in front of
Prefect. It lets clients request retraining without holding Prefect credentials themselves. Compose
uses SQLite for local development; production can select the shared Postgres storage adapter.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/livez` | none | Process liveness for restart decisions; always 200 while the server can respond |
| GET | `/readyz` | none | Traffic readiness (`ready` in `/health`); returns 503 when startup checks or the approval store are unhealthy. A failed startup check is re-run by the probe (at most every `CONTROL_PLANE_STARTUP_RECHECK_SECONDS`), so readiness recovers when the dependency does. A missing retrain dispatch target does **not** make the API unready (see [Dispatch target](#dispatch-target)) |
| GET | `/ready` | none | Compatibility alias for the original liveness endpoint |
| GET | `/health` | none | Diagnostic verdict, dependencies, pending approvals, the retrain `dispatch` target's state, and runtime capabilities (scaling blockers, `separation_of_duties`) |
| GET | `/status` | **read** | Concurrent peer pings + pending approval count. Returns **exactly** `services` (`control_plane`/`mlflow`/`prefect`/`ray_serve`/`dashboard`, each `{ok, url}`) and `pending_approvals`. It carries **no** model list — `exa status` reads production models from the MLflow registry instead |
| GET | `/models` | **read** | List `model_name → datasets` known to the auto-discovery registry |
| GET | `/models/{name}/meta` · `/readme` · `/images/{file}` | **read** | Model metadata, README and bundled images (the dashboard serves images to browsers through its own signed URLs) |
| POST | `/retrain` | **write** | Validate + schedule a Prefect flow run |
| GET | `/retrain/{flow_run_id}` | **read** | Poll Prefect for a run owned by the credential's tenant; the legacy credential retains operator-wide lookup |
| POST | `/api/changes` | **write** | CI webhook — record changed model IDs as tenant-scoped pending approvals (no training yet) |
| GET | `/approvals` | **read** | List only the credential tenant's approvals; filter by `?status=pending\|approved\|rejected` |
| POST | `/approve/{model_id}` | **write** | Approve a pending change in the credential tenant — fires Prefect training immediately |
| POST | `/reject/{model_id}` | **write** | Reject a pending change in the credential tenant with optional `{"reason": "..."}` body |
| DELETE | `/approvals/{approval_id}` | **write** | Retract a pending approval (`exa approvals delete`). The record is kept as `retracted`; nothing is erased |
| POST | `/webhooks/modelzoo/gitlab` | token header | GitLab push webhook — mark models stale, optionally auto-retrain |
| POST | `/webhooks/modelzoo/github` | HMAC header | GitHub push webhook — same semantics as GitLab |
| GET | `/modelzoo/status` | **read** | Per-model freshness: `current` / `stale` / `unknown` |
| GET | `/modelzoo/events` | **read** | Recent push event history (`?limit=N`) |
| POST | `/modelzoo/sync` | **write** | Manually trigger one GitLab poll cycle |
| GET | `/modelzoo/config` | **read** | Show runtime ModelZoo config |
| PUT | `/modelzoo/config` | **write** | Update runtime config (takes effect immediately) |
| GET | `/metrics` | none | Prometheus text-format metrics for the approval gate (Phase 13) |
| POST | `/v1/retrain` | **write** | Accept a retrain as an **asynchronous command**: 202 + `Location: /v1/commands/{id}`; honours `Idempotency-Key` |
| GET | `/v1/commands/{command_id}` | **read** | One command's state, attempts, result (`flow_run_id`) and last error, in the caller's tenant |
| GET | `/v1/commands` | **read** | The tenant's commands, newest first; `?state=`, `?kind=`, `?limit=` (≤ 200) and `?cursor=` (the previous page's `next_cursor`) |
| DELETE | `/v1/commands/{command_id}` | **write** | Cancel a command not yet dispatched (`pending`, or `failed` awaiting retry); audited |

## Versioned API (`/v1`)

Every operator route has a `/v1` path. Each `/v1` path runs the same handler as its legacy path,
with the same authentication, request and response. The one intended difference is errors: under
`/v1` they are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents
(`application/problem+json` with `type`, `title`, `status`, `detail`, `instance`, and `errors` for
validation failures), while legacy paths keep FastAPI's `{"detail": …}`.

| Legacy | `/v1` |
|---|---|
| `GET /status` | `GET /v1/status` |
| `GET /models` · `/models/{name}/meta` · `/readme` · `/images/{file}` | `GET /v1/models` · `/v1/models/{name}/meta` · `/readme` · `/images/{file}` |
| `GET /retrain/{flow_run_id}` | `GET /v1/runs/{flow_run_id}` |
| `GET /approvals` | `GET /v1/approvals` |
| `POST /approve/{model_id}` | `POST /v1/approvals/{model_id}/approve` |
| `POST /reject/{model_id}` | `POST /v1/approvals/{model_id}/reject` |
| `DELETE /approvals/{approval_id}` | `DELETE /v1/approvals/{approval_id}` |
| `POST /api/changes` | `POST /v1/changes` |
| `GET\|POST\|PUT /modelzoo/…` | `/v1/modelzoo/status` · `/events` · `/sync` · `/config` |
| `POST /admin/reload` | `POST /v1/admin/reload` |
| `POST /retrain` (synchronous) | `POST /v1/retrain` — an asynchronous command, not the same call |

**The legacy paths are deprecated but not scheduled for removal.** Every response from one carries
[RFC 9745](https://www.rfc-editor.org/rfc/rfc9745) `Deprecation: @1789084800` (since 2026-09-11)
and a `Link` to its successor, with the request's own path parameters filled in:

```http
HTTP/1.1 200 OK
Deprecation: @1789084800
Link: </v1/approvals/JPCP/approve>; rel="successor-version"
```

There is no `Sunset` header, because no removal date has been set. When one is, the header will
say so first.

**The platform's own components use `/v1`.** The CLI, the MCP tools, the SDK, the dashboard and the
Skipper agent call the `/v1` paths for every route in the table whose twin is the same handler, and
`tests/unit/test_control_plane_callers_use_v1.py` fails if one goes back.

That includes the synchronous `POST /retrain`. The platform's retrains (`exa retrain`,
`exa drift trigger`, `exa autopilot`, `exa pipeline hpo start`, `exa production deploy`, the MCP
and agent retrain tools, the bus bridge, the dashboard) submit through `POST /v1/retrain` and then
wait a bounded time for the command to be dispatched, using `examlops.retrain_command`:

| What happens | What the caller reports |
|---|---|
| Dispatched within the wait (normally about a second) | the `flow_run_id`, as the old route did, plus the `command_id` |
| Still queued when the wait ends: admission full, Prefect briefly down | *accepted, not dispatched yet*, with the `command_id` to follow (`exa commands show <id>`). The control plane still dispatches it, so nobody should submit it again |
| The control plane gave up (`dead`) or it was cancelled | an error with the command's `last_error` |

Interactive commands wait `EXAMLOPS_RETRAIN_WAIT_SECONDS` (30 s). The automated loops (drift
trigger, autopilot, bus bridge) wait at most 5 s so an outage cannot stall a cycle. A Prefect outage
used to fail every one of these calls outright. Now the retrain is kept and dispatched when Prefect
comes back.

!!! note "Upgrade order"
    The `/v1` paths and the components that call them ship together. When upgrading piecemeal,
    upgrade the control plane **before** the dashboard, agent or CLI. An older control plane
    answers `/v1` with 404, which the dashboard reads as "model not found".

### The Python client

`examlops.control_plane_api` is generated from the committed API contract
(`platform/services/control_plane/api-contract.json`). It has one function per `/v1` operation,
and nothing in it is hand-written:

```python
from examlops import control_plane_api as cp

cp.list_approvals(status="pending")                      # base and token from the CLI config
cp.approve("JPCP")
cp.submit_retrain(body={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
                  idempotency_key="nightly-2026-09-11")
cp.list_commands(state="dead", base="http://cp:8002", token=token)
```

Path parameters are URL-escaped, query parameters are sent only when given, and errors raise the
CLI's `ClientError` with the problem document's `detail`. `make openapi-export` regenerates the
contract and the client together. A test fails while the client is stale, and the generator
refuses a new `/v1` route until it has a function name. The `exa approvals`, `exa modelzoo`,
`exa retrain-status`, `exa production reload`, `exa cards` and `exa status` commands use it. Probes (`/health`, `/readyz`, `/livez`, `/ready`), `/metrics` and the inbound
ModelZoo webhooks are infrastructure and stay unversioned.

## POST /retrain

```bash
curl -X POST http://localhost:18002/retrain \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "JPCP",
    "dataset_name": "PM100Dataset",
    "is_dummy": true,
    "backend_name": "minio"
  }'
```

Response:
```json
{
  "flow_run_id": "abc123…",
  "deployment": "training_flow/examlops-dispatch",
  "status_url": "/retrain/abc123…",
  "parameters": {
    "model_name": "JPCP",
    "dataset_cls_name": "PM100Dataset",
    "is_dummy": true,
    "backend_name": "minio"
  }
}
```

Validation steps before the call to Prefect:

1. `model_name` must be found in the YAML registry (see [Model Registry](#model-registry) below).
2. `dataset_name` must be in that model's declared datasets.
3. The bearer credential must resolve to a configured principal with `write` scope.

Failure modes:

| HTTP | When |
|---|---|
| 400 | Unknown `model_name` or unsupported `dataset_name` |
| 401 | Missing `Authorization` header, or a token from a data-center IdP that failed verification (`WWW-Authenticate: Bearer error="invalid_token"`) |
| 403 | Wrong bearer token; a verified IdP user whose groups map to no role; or the center's PDP denied the call |
| 503 | No usable credentials are configured, or `CONTROL_PLANE_CREDENTIALS_JSON` is malformed |
| 502 | Prefect API unreachable / 5xx |

### Durable dispatch and event delivery

Send an `Idempotency-Key` header with any retrain you might retry: the same key and body return the
first attempt's result instead of dispatching again, on `/retrain` and `/v1/retrain` alike. Skipper's
retrain tools send one per call, so their built-in transport retries cannot create a second run.
The dashboard's **Run pipeline** button submits `/v1/retrain` like `exa retrain --async`, so the
same admission caps, audit and training lease govern every door.

Each retrain or approval dispatch is claimed in `control_plane_commands` before the Prefect call.
The control plane sends the same stable idempotency key to Prefect, stores the successful response,
and can replay it for a repeated caller key. An expired dispatch lease can be recovered after a
process failure. Completion also updates its admission record and enqueues a domain event in the
same database transaction.

The built-in relay publishes the outbox through the configured publisher; operators can also run
`exa events relay` manually. `log` is the local default, while Redis Streams is the implemented
shared broker. Delivery is **at least once**, with a stable event ID for consumer deduplication.
NATS and Kafka selectors remain fail-loud placeholders.

### Asynchronous commands (`/v1`)

`POST /retrain` dispatches inside the request: the caller waits on Prefect, and a Prefect outage is a
failed request. `POST /v1/retrain` instead records the command durably and answers at once:

```bash
curl -si -X POST http://localhost:18002/v1/retrain \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" -H "Idempotency-Key: nightly-2026-09-10" \
  -H "Content-Type: application/json" \
  -d '{"model_name": "JPCP", "dataset_name": "PM100Dataset"}'
# HTTP/1.1 202 Accepted
# location: /v1/commands/v1:retrain:3f9c…
# {"command_id": "v1:retrain:3f9c…", "state": "pending", "attempts": 0, ...}

exa retrain JPCP --dataset PM100Dataset --async   # the same, from the CLI
exa commands show v1:retrain:3f9c…                # follow it
```

A worker pool in the control plane (`CONTROL_PLANE_COMMAND_WORKERS`) dispatches due commands oldest
first, through the same admission caps, dispatch target and audit as the synchronous path:

| State | Meaning |
|---|---|
| `pending` | Accepted, waiting for a worker (or for admission capacity — it waits, it does not fail) |
| `dispatching` | A worker holds its lease; a crashed worker's lease expires and the command is retried |
| `failed` | The last attempt failed; retried after exponential backoff (`CONTROL_PLANE_COMMAND_BACKOFF_SECONDS`) |
| `succeeded` | A flow run was created; `result.flow_run_id` names it |
| `dead` | `CONTROL_PLANE_COMMAND_MAX_ATTEMPTS` attempts failed; nothing retries it (alert `ControlPlaneCommandDead`) |
| `cancelled` | Withdrawn with `DELETE /v1/commands/{id}` before dispatch |

Once a run exists, the workers follow it (`CONTROL_PLANE_RECONCILE_SECONDS`) and record the flow
run's own state as `run_state` — `SCHEDULED`, `RUNNING`, then `COMPLETED`, `FAILED`, `CANCELLED`,
`CRASHED`, or `MISSING` if Prefect no longer knows the run. Reaching a terminal state publishes one
outbox event, `retrain.run_<state>` (for example `retrain.run_completed`), for subscribers that
would otherwise poll Prefect. This also applies to runs dispatched by the synchronous `/retrain`.

**One training run per model and dataset.** While a retrain of the same model and dataset in your
tenant is queued, dispatching or still training, `POST /v1/retrain` answers 409 naming the command
to follow. A retry with the same `Idempotency-Key` is not refused: it returns the existing command.

The command id is Prefect's idempotency key, so a retry after a lost response can never create a
second run. The same `Idempotency-Key` with a different body answers 409. Commands submitted to the
synchronous `/retrain` are never retried in the background: that caller has already been told the
outcome.

Errors on `/v1` are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents
(`application/problem+json` with `type`, `title`, `status`, `detail`, `instance`; validation
failures add `errors`). The legacy routes keep their `{"detail": ...}` shape.

### Dispatch target

Every retrain the control plane dispatches — `POST /retrain`, an approval, a ModelZoo auto-retrain —
goes to one Prefect deployment, `PREFECT_DEPLOYMENT_NAME`, by default
`training_flow/examlops-dispatch`. `exa pipeline deploy` registers it beside the nightly schedule
and **serves** it: runs execute only while that process is running.

The control plane checks the target at startup, on `POST /admin/reload`, and at most once a minute
after that, and reports it in `GET /health`:

```json
"dispatch": {"deployment": "training_flow/examlops-dispatch", "state": "ok",
             "detail": null, "parameters": ["backend_name", "dataset_cls_name", "is_dummy", "model_name"]}
```

| `state` | Meaning | Effect |
|---|---|---|
| `ok` | The deployment exists and accepts `model_name`, `dataset_cls_name`, `is_dummy`, `backend_name` | Unknown extra `parameters` keys are rejected with 400 before anything is recorded |
| `missing` | Prefect has no such deployment | `status: degraded`; a dispatch answers **503** naming the deployment and the fix |
| `incompatible` | The flow does not accept what the control plane sends, or requires something it never sends | `status: degraded` |
| `unreachable` | Prefect did not answer | No verdict; Prefect's own validation has the last word |

None of these pulls the API out of rotation: approvals, reads and the durable command record keep
working, and a fresh stack legitimately starts before `exa pipeline deploy` has run.

### Admission capacity

At most `EXAMLOPS_ADMISSION_MAX_RUNNING` dispatches run at once platform-wide, and
`EXAMLOPS_ADMISSION_PER_TENANT` per tenant. A dispatch refused at capacity answers **429** with
`Retry-After` (`CONTROL_PLANE_ADMISSION_RETRY_AFTER`, 30 s); retry with the same
`Idempotency-Key` (or the legacy `X-Idempotency-Key`; both routes accept either). A refused request leaves nothing queued behind it, and a slot held by a
dispatch whose process died is released when its lease expires — neither can block later requests.

## Approval Gate (Phase 11)

When a developer pushes to the modelzoo repo on GitHub, CI runs `ci/notify_model_changes.py`, which diffs the commits, extracts changed `model_id` values, and POSTs to `POST /api/changes`. The control plane stores these as `pending` rows in its configured state backend — **no training runs yet**.

A sysadmin then approves or rejects each change:

```bash
# List pending approvals
exa approvals list
curl http://localhost:18002/approvals?status=pending \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN"

# Approve — fires Prefect training immediately
exa approvals approve JPCP
curl -X POST http://localhost:18002/approve/JPCP \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN"

# Reject (no training)
exa approvals reject JPCP --reason "needs data review"
curl -X POST http://localhost:18002/reject/JPCP \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "needs data review"}'
```

### Approval data

Each approval record carries:

| Field | Description |
|---|---|
| `model_id` | Model identifier (e.g., `JPCP`) |
| `status` | `pending` / `approved` / `rejected` |
| `commit_sha` | Triggering commit hash |
| `commit_msg` | Commit message from CI |
| `changed_files` | JSON list of changed file paths |
| `prefect_run_id` | Prefect flow run ID (set on approval) |
| `reject_reason` | Rejection reason (set on rejection) |
| `tenant` | Verified credential tenant that owns the approval |
| `requested_by` | Verified principal that requested the approval |
| `resolved_by` | Verified principal that approved or rejected it |
| `requested_at` | ISO 8601 timestamp of the CI push |
| `resolved_at` | ISO 8601 timestamp of approve/reject |

`status` is one of `pending`, `approving` (a dispatch is in flight), `approved`, `rejected`,
`retracted` or `expired` (after `APPROVAL_EXPIRY_HOURS`).

The dashboard Approvals page (admin-only) shows a badge with the pending count and lets sysadmins approve or reject with one click.

### Separation of duties

The principal that filed a change (`requested_by`) cannot approve it: the attempt answers **403**
and another principal must approve. The rule is on by default
(`CONTROL_PLANE_SEPARATION_OF_DUTIES`). The shared `CONTROL_PLANE_TOKEN` cannot satisfy it — every
holder is the principal `legacy` — so it is exempt, and `GET /health` reports
`runtime.separation_of_duties: enforced-except-legacy-token`. Give CI and each approver their own
credential (`CONTROL_PLANE_CREDENTIALS_JSON`, or federated sign-in) to get an enforced gate.

Rejecting your own change is allowed. Retracting one (`exa approvals delete <id>`) is too; the record
stays as `retracted` with who and when.

### Audit trail

Every decision the gate takes is written to the hash-chained audit log that `exa audit` reads, **in
the same database transaction as the decision**, so a decision cannot stand without its record:

| Action | When |
|---|---|
| `approval_requested` | `POST /api/changes` files a pending approval |
| `approval_approved` | An approval dispatches its training run |
| `approval_rejected` | `POST /reject/{model_id}` |
| `approval_retracted` | `DELETE /approvals/{approval_id}` |
| `retrain_dispatched` | `POST /retrain` or a ModelZoo auto-retrain creates a flow run |

The actor is the verified principal of the calling credential, whichever surface made the call —
the CLI, the dashboard or Skipper. Approvals, rejections and retractions are also published as
outbox events (`approval.approved`, `approval.rejected`, `approval.retracted`).

### CI integration

The GitHub Actions `examlops` job calls `ci/notify_model_changes.py` on every push to `main`. The script:
1. Diffs `before..after` commits.
2. Finds changed files under `modelzoo/seanergys_modelzoo/models/tasks/`, `pipelines/model_configs/`, and `pipelines/models/`.
3. Extracts `model_id` values via regex.
4. POSTs to `POST /api/changes` with bearer auth.
5. **Fails silently** on connection error — never blocks CI.

Required GitHub secrets: `CONTROL_PLANE_URL` and `CONTROL_PLANE_TOKEN`.

## Auth model

`CONTROL_PLANE_CREDENTIALS_JSON` is a JSON object keyed by bearer secret. Each value defines a
server-trusted principal, a tenant, and a non-empty list of scopes:

| Scope | Allows |
|---|---|
| `read` | Every read: status, approvals, runs, commands, ModelZoo status |
| `write` | Every mutation (it implies all four scopes below) |
| `retrain` | Request retrains (`POST /retrain`, `/v1/retrain`) and cancel queued commands |
| `approve` | Approve, reject or retract pending approvals |
| `changes` | Report CI model changes that open approvals (`POST /api/changes`) |
| `admin` | Reload the registry; change or sync the ModelZoo integration |

A refused call says which scopes would have allowed it (`Missing 'write' or 'approve' scope`). An
unknown scope is a configuration error that fails authentication closed, never a silent grant.

```json
{
  "change-me-operator-token": {
    "principal": "release-operator",
    "tenant": "team-a",
    "scopes": ["read", "write"]
  },
  "change-me-auditor-token": {
    "principal": "auditor",
    "tenant": "team-a",
    "scopes": ["read"]
  }
}
```

The literal example secrets above are rejected as placeholders; generate distinct random values.

### One credential per service (plan P3.2)

Give each service its own principal and only the scopes it uses. The audit trail and every
command then name the service that acted, and a compromised service can do only its own job:

```json
{
  "<random>": {"principal": "dataplane-bus-bridge", "tenant": "default", "scopes": ["retrain"]},
  "<random>": {"principal": "autopilot",        "tenant": "default", "scopes": ["read", "retrain"]},
  "<random>": {"principal": "skipper",          "tenant": "default", "scopes": ["read", "retrain"]},
  "<random>": {"principal": "dashboard",        "tenant": "default", "scopes": ["read", "write"]},
  "<random>": {"principal": "ci",               "tenant": "default", "scopes": ["changes"]}
}
```

Hand each service its secret through its own variable. In Compose these are
`DATAPLANE_BUS_BRIDGE_CONTROL_PLANE_TOKEN`, `AUTOPILOT_CONTROL_PLANE_TOKEN`,
`AGENT_CONTROL_PLANE_TOKEN` and `DASHBOARD_CONTROL_PLANE_TOKEN`; each falls back to the shared
`CONTROL_PLANE_TOKEN` while unset. Add `approve` to Skipper's scopes only if the agent should
approve models; separation of duties still refuses an approval by the principal that requested
it.
Identity and tenant are never accepted from request bodies or caller-chosen headers. Approval rows,
durable commands, idempotency keys, and emitted events use the verified context. Cross-tenant
approval access returns no matching record, and structured credentials cannot inspect another
tenant's flow run.

`CONTROL_PLANE_TOKEN` remains a migration-compatible operator credential. It maps to principal
`legacy`, tenant `default`, with both scopes and keeps operator-wide flow-status lookup. If the
structured JSON is malformed or reuses the legacy secret, authentication fails closed for every
credential. `/health`, readiness/liveness, metrics, `/models`, and model metadata/assets remain
public; `/status`, approval, flow-status, and ModelZoo operational reads require `read`.

### Federated users (ADR 0120)

With a trust file (`EXAMLOPS_IAM_CONFIG`), the control plane also accepts **access tokens from a
trusted data-center IdP** alongside the static credentials above, which are checked first: a
bearer that matches no static secret and is a JWT (or an opaque token named with the
`X-ExaMLOps-IdP: <provider>` header) is verified against that center's keys and audience. The
principal is `<provider>:<sub>`, the tenant is the one the trust file binds to that issuer, and the
role the center's groups map to decides the scopes: `viewer` → `read`, `operator`/`admin` →
`read` + `write`. When the center's entry sets `authorization.mode: both`, every scoped route also
asks the center's AuthZEN/OPA PDP about `api.read`/`api.write` on the route path, and a PDP outage
denies. Federation alone is a complete configuration (no `CONTROL_PLANE_TOKEN` needed); an invalid
trust file refuses every federated token and shows as `identity_federation: fail: …` in `/health`
startup checks. Users get a token with `exa auth login`; see
[Identity federation](identity-federation.md).

## Make targets

```bash
make control-plane-up                         # start the service on :18002
make control-plane-down                       # stop it
make control-plane-logs                       # tail logs
exa retrain JPCP --dataset PM100Dataset --dummy
                                              # one-shot POST /retrain via curl
```

## Model Registry

`GET /models` and `POST /retrain` validation both rely on the active use-case pack. The control plane
resolves its model YAML directory from `EXAMLOPS_USECASE_DIR`/`usecases/reference/pack.toml`; it does
**not** import model training classes.

This approach (`_load_registry()` in `app.py`) avoids importing model training code. The parsed
registry has a 60-second in-process TTL; `POST /admin/reload` invalidates it immediately.

`model_meta.py` follows the same pattern: it scans YAML files for metadata (task type, schema, promotion rules, serving aliases) and locates the model's source directory by text-scanning the modelzoo for `class <ModelClass>` — never importing the class itself.

## ModelZoo Integration (Phase 12)

The control plane is the authoritative hub for ModelZoo repository freshness tracking. It receives push events from GitLab/GitHub webhooks and from a background poller, records them in its configured state backend, and exposes freshness state via REST endpoints. The dashboard and `exa` CLI both consume these endpoints.

### How it works

1. **Push event arrives** — via `POST /webhooks/modelzoo/gitlab` (or `/github`), or discovered by the background poller querying the GitLab API.
2. **Event recorded** — a row is inserted into `modelzoo_events` (commit SHA, branch, pushed_by, timestamp, source).
3. **All models marked stale** — every model in the auto-discovery registry gets an upserted row in `model_freshness` with `is_stale=1` and the current timestamp as `stale_since`.
4. **Optional auto-retrain** — if `_modelzoo_config["auto_retrain"]` is true, `POST /retrain` fires for each model with its first supported dataset.
5. **Freshness update** — the current automatic path clears `is_stale` after Prefect accepts the
   retrain request and stores that commit as `last_retrain_commit`. This records dispatch, not
   successful training completion. The manual approval path does not currently reconcile freshness;
   consumers must not treat `current` as proof that a training run completed successfully.

### Webhook registration

**GitLab** — Settings → Webhooks → add URL `http://<control-plane>:18002/webhooks/modelzoo/gitlab`, select *Push events*, set secret token to `MODELZOO_WEBHOOK_SECRET`.

**GitHub** — Settings → Webhooks → add URL `http://<control-plane>:18002/webhooks/modelzoo/github`, content type `application/json`, select *Push events*, set secret to `MODELZOO_WEBHOOK_SECRET`.

The dashboard Config page (ModelZoo Integration section) shows the pre-filled webhook URL derived from the live control plane address.

Deliveries are idempotent on the commit: a redelivered or replayed webhook for a commit already
recorded (by a webhook or by the poller) answers `{"duplicate": true}` and does nothing — no second
event row, no CI trigger, no auto-retrain. Bodies above `CONTROL_PLANE_WEBHOOK_MAX_BYTES` (5 MiB)
answer 413.

### Background poller

The poller runs as a daemon thread inside the control-plane process. FastAPI lifespan starts it, and
an interruptible event wait applies interval changes on the next boundary and permits clean shutdown.
Only the replica holding the coordinator lease polls. Keep one replica until failover is tested and
the remaining process-local circuit-breaker/runtime-config blockers are removed.

Disable polling with `MODELZOO_POLL_SECONDS=0` (env var) or `PUT /modelzoo/config {"poll_interval_seconds": 0}` at runtime.

### Runtime config

`PUT /modelzoo/config` writes to the `_modelzoo_config` in-memory dict (protected by `_CONFIG_LOCK`). Both the poller loop and the webhook handlers read from this dict, so changes are live immediately:

```bash
# Flip on auto-retrain without restarting the service
curl -X PUT http://localhost:18002/modelzoo/config \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"auto_retrain": true}'

# Halve the poll interval
curl -X PUT http://localhost:18002/modelzoo/config \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -d '{"poll_interval_seconds": 150}'
```

### `exa modelzoo` CLI

The `exa modelzoo` command group talks directly to these endpoints:

```bash
exa modelzoo status               # table: model, status (CURRENT/STALE), stale since, latest commit
exa --json modelzoo status        # raw JSON list
exa modelzoo events               # table: id, commit, branch, pushed_by, timestamp, source
exa modelzoo events --limit 5     # last 5 events
exa modelzoo sync                 # trigger one poll cycle, print result
exa modelzoo config               # show auto_retrain, poll_interval_seconds, watch_branch
```

### State tables

Two tables are created on first startup alongside `pending_approvals` in the configured SQLite or
Postgres state backend:

**`modelzoo_events`**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto-increment |
| `commit_sha` | TEXT | Git commit SHA |
| `branch` | TEXT | Branch name |
| `pushed_by` | TEXT | GitLab username / GitHub login / Git author |
| `timestamp` | TEXT | ISO 8601 |
| `source` | TEXT | `webhook` or `poll` |

**`model_freshness`**

| Column | Type | Notes |
|---|---|---|
| `model_id` | TEXT PK | e.g. `JPCP` |
| `latest_modelzoo_commit` | TEXT | Latest push SHA |
| `last_retrain_commit` | TEXT | SHA attached to the last accepted automatic retrain dispatch; not completion proof |
| `is_stale` | INTEGER | 1 = stale, 0 = current |
| `stale_since` | TEXT | ISO 8601 when stale flag was set |
| `retrain_triggered_at` | TEXT | ISO 8601 of last auto-retrain trigger |

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_PORT` | `8002` | HTTP port |
| `CONTROL_PLANE_TOKEN` | unset | Legacy `legacy/default` bearer credential with `read` + `write`; optional when the structured map is configured |
| `CONTROL_PLANE_CREDENTIALS_JSON` | unset | Token-keyed JSON map of `principal`, `tenant`, and `scopes`; malformed input fails all bearer authentication closed |
| `PREFECT_API_URL` | `http://localhost:14200/api` | Prefect server endpoint. `14200` is the host port the stack publishes; under compose the service sets `http://orchestrator:4200/api` itself. |
| `PREFECT_DEPLOYMENT_NAME` | `training_flow/examlops-dispatch` | Deployment every dispatched retrain goes to (must be `flow_name/deployment_name`). `exa pipeline deploy` registers and serves it; `GET /health` → `dispatch` says whether it exists and accepts the control plane's parameters. |
| `CONTROL_PLANE_ADMISSION_RETRY_AFTER` | `30` | `Retry-After` seconds on a 429 admission refusal |
| `CONTROL_PLANE_SEPARATION_OF_DUTIES` | `true` | Requester may not approve its own change (legacy token exempt) |
| `CONTROL_PLANE_WEBHOOK_MAX_BYTES` | `5242880` | Largest accepted ModelZoo webhook body (413 above) |
| `CONTROL_PLANE_URL` | `http://control-plane:8002` | Set on the dataplane simulator so it can forward |
| `EXAMLOPS_DB_BACKEND` | `sqlite` | Control-plane state engine: `sqlite` for local development or `postgres` for shared production state |
| `EXAMLOPS_POSTGRES_DSN` | unset | Required when the state backend is `postgres` |
| `CONTROL_PLANE_DB` | `/data/approvals.db` | Local SQLite path; an unwritable parent is a startup/readiness failure, never a silent fallback |
| `MODELZOO_WEBHOOK_SECRET` | unset | Shared secret for webhook HMAC/token verification |
| `MODELZOO_AUTO_RETRAIN` | `false` | Trigger auto-retrain on every push event |
| `MODELZOO_POLL_SECONDS` | `300` | GitLab poller interval (seconds); `0` disables |
| `MODELZOO_WATCH_BRANCH` | `main` | Branch watched by webhooks and poller |
| `GITLAB_PROJECT_ID` | unset | GitLab project ID or namespace/path for the poller |
| `GITLAB_TOKEN` | unset | GitLab Personal/Project Access Token (`read_repository` scope) for the poller |

## Operations

* **Liveness:** `curl localhost:18002/livez` — process-only, suitable for restart decisions.
* **Readiness:** `curl --fail localhost:18002/readyz` — non-2xx when the replica must not receive traffic.
* **Diagnostics:** `curl localhost:18002/health` — detailed JSON verdict without probe semantics.
  Its runtime block identifies the selected coordinator/publisher, poller lease state, relay result,
  outbox `pending`/`published`/`poison` counts, and remaining horizontal-scaling blockers.
* **Polling a run:** `curl -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" localhost:18002/retrain/<id>` returns `{flow_run_id, state_type, state_name, is_terminal}`. `is_terminal` is true for `COMPLETED / FAILED / CANCELLED / CRASHED`.
* **Logs:** `make control-plane-logs` (or via Loki when the monitoring stack is up — labels: `compose_service="control-plane"`).

## Prometheus Metrics (Phase 13)

The control plane exposes a Prometheus-compatible `/metrics` endpoint that the existing monitoring stack scrapes automatically. No authentication required — this is consistent with the Ray Serve `/metrics` endpoint and is safe because the control plane is only reachable inside the Docker network.

### Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `examlops_approvals_pending` | Gauge | — | Current count of rows with `status = 'pending'`; updated on every create/approve/reject |
| `examlops_approval_events_total` | Counter | `model_id`, `action` | Cumulative approval lifecycle events; `action` ∈ `{created, approved, rejected}` |
| `examlops_approval_age_oldest_seconds` | Gauge | — | Age in seconds of the oldest pending approval; 0 when no pending approvals; computed on each `/metrics` scrape |

### Retrain outcomes, and what a "success rate" divides by

`examlops_retrain_requests_total{model_name, dataset_name, outcome}` carries **five** outcomes, and
they are not all attempts:

| Outcome | Meaning | A retrain happened? |
|---|---|---|
| `success` | dispatched and recorded | yes |
| `error` | the dispatch never reached Prefect | no |
| `dispatched_unrecorded` | Prefect took it; the bookkeeping write failed | **yes** — the training is running |
| `dedup` | an identical retrain was already in flight | no (the first one is) |
| `throttled` | refused by the rate limit | no |

Two rules follow, and both were wrong until 2026-09-14:

- **`dispatched_unrecorded` is not an error.** It used to be recorded as one, and
  [HighRetrainErrorRate](../runbooks/control-plane.md#highretrainerrorrate) pages above a 20% error
  rate over 15 minutes — with retrains being rare, one miscounted success is 100%. See
  [RetrainDispatchedButNotRecorded](../runbooks/control-plane.md#retraindispatchedbutnotrecorded).
- **A success *rate* must name its denominator.** Dividing by the unfiltered counter puts `dedup`,
  `throttled` and `dispatched_unrecorded` in the denominator, so a retrain that succeeded on retry
  reads as 50% and a throttled request lowers the score of a platform that did exactly what it was
  configured to do. Both Grafana panels — **and, since 2026-09-14, the
  [HighRetrainErrorRate](../runbooks/control-plane.md#highretrainerrorrate) alert** — divide by
  `outcome=~"success|error"`, the outcomes that represent an attempt that concluded.

    The alert was missed when the panels were fixed, and the arithmetic is worse there because it
    decides whether anyone is paged: 3 errors out of 5 attempts is **60%** and fires; add 50
    **throttled** requests and it reads **5.5%** and does not. Throttling is what happens when a
    system is already under pressure, so the dilution is *anti*-correlated with the alert firing —
    it is quietest exactly when it should be loudest.

`tests/unit/test_grafana_panels_can_show_data.py` holds both: every outcome the code emits appears
on every panel that breaks retrains down *by outcome* (checked per panel, not pooled across
dashboards), and no success-rate panel divides by the bare counter.

### Scrape configuration

The `control_plane` job is already configured in `platform/infra/docker-compose/prometheus.yml`:

```yaml
- job_name: control_plane
  static_configs:
    - targets: ['control-plane:8002']
  metrics_path: /metrics
```

Prometheus scrapes it every 15 seconds (global `scrape_interval`).

### Grafana dashboard

The **ExaMLOps — Approval Gate** dashboard (`platform/infra/docker-compose/grafana/provisioning/dashboards/examlops_approvals.json`) is provisioned automatically when Grafana starts. It contains three panels:

| Panel | Type | PromQL |
|---|---|---|
| Pending Approvals | Stat (green = 0, red ≥ 1) | `examlops_approvals_pending` |
| Oldest Pending Age (min) | Stat (yellow > 30 min, red > 60 min) | `examlops_approval_age_oldest_seconds / 60` |
| Approval Events Rate | Time series | `rate(examlops_approval_events_total[5m])` |

### Key alert threshold

`examlops_approval_age_oldest_seconds > 3600` — a model change has been waiting for approval for more than one hour. Configure this in Grafana Alerting → Alert rules, pointing to the `control_plane` contact point of your choice.

### Error handling

- If the SQLite read fails at scrape time, the error is logged, the age gauge is set to 0, and `generate_latest()` is still returned — the scrape never returns HTTP 500.
- `prometheus-client` import errors at startup are non-fatal: `/metrics` returns 503 with a plain message.

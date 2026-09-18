# Event backbone

ExaMLOps publishes what happens — a retrain scheduled, a run completed, an approval rejected — as
events on a backbone that any component can subscribe to, instead of every component polling the
platform database for changes.

- **Producers never talk to the broker.** A domain change and its event are written to the
  `event_outbox` table in one transaction; a relay publishes the outbox. A broker outage never blocks
  a write, and no event is lost while the broker is down.
- **The broker is NATS JetStream** (ADR 0124). Events persist in a stream with bounded retention,
  consumers keep their own position, and a consumer that was down catches up.
- **Every event is a CloudEvents 1.0 envelope**, whichever publisher carries it.

## Turning it on

The backbone is opt-in. Without it, events are relayed to the platform log (`log` publisher) — fine
for a laptop, invisible to any other component.

```bash
docker compose --profile events up -d nats          # NATS 2.14 with JetStream, data in nats_data
docker compose --profile events up -d autopilot-follower skipper-watch   # the built-in consumers

# .env — for every process that relays (the control plane relays its own outbox)
EXAMLOPS_EVENT_PUBLISHER=nats
EXAMLOPS_NATS_URL=nats://nats:4222                  # from the host: nats://localhost:14222

pip install 'examlops[events]'                      # nats-py, where you run exa or a consumer
```

The stream `EXAMLOPS_EVENTS` is created on first use. Configuration is in
[environment variables](../reference/env-vars.md) (`EXAMLOPS_NATS_*`).

On Kubernetes, the Helm chart does the same with `events.publisher: nats`, `events.natsUrl` and
`events.followers.{autopilot,skipperWatch}.enabled`. Each follower is its own pod and
NetworkPolicy tier. NATS itself is external to the chart, like Postgres. See the chart's README,
*Event backbone*.

## What an event looks like

```json
{
  "specversion": "1.0",
  "id": "outbox:4812",
  "source": "/examlops/platform",
  "type": "io.examlops.retrain.run_completed",
  "time": "2026-09-10T22:08:28Z",
  "datacontenttype": "application/json",
  "dataschema": "urn:examlops:event-schema:retrain.run_completed",
  "examlopstenant": "default",
  "examlopsactor": "bob",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "data": {"command_key": "v1:retrain:3f9c…", "flow_run_id": "55bec2da…", "run_state": "COMPLETED",
           "model_name": "JPCP", "dataset_name": "PM100Dataset"}
}
```

`id` is the outbox id and stays the same however many times the event is delivered. The subject is
the topic under the prefix: `examlops.events.retrain.run_completed`.

`traceparent` is the trace context of the request that wrote the event. It is stored in the outbox
row inside the same transaction, so it is still the producer's context when the relay publishes the
event seconds later from another thread. `EventConsumer` runs each handler in a `CONSUMER` span
parented on it: with OpenTelemetry enabled, a retrain's API call, its dispatch and whatever reacts to
`retrain.run_completed` appear in one trace. Events written outside any trace carry no
`traceparent`. Tracing stays off (`OTEL_SDK_DISABLED=true`) unless an OTLP endpoint is configured,
because an exporter with no collector only produces errors.

## Events the platform publishes

| Topic | When | Committed with the change? |
|---|---|---|
| `retrain.scheduled` | A retrain was dispatched to Prefect (legacy `/retrain`, `/v1/retrain`, an approval, a modelzoo webhook) | Yes |
| `retrain.run_completed` / `run_failed` / `run_cancelled` / `run_crashed` / `run_missing` | The control plane's reconciler saw the flow run reach a terminal state (`run_missing`: Prefect no longer knows the run) | Yes |
| `approval.approved` / `approval.rejected` / `approval.retracted` | A sysadmin decided or retracted a pending approval | Yes |
| `modelzoo.retrain_scheduled` | A modelzoo push scheduled a retrain | Yes |
| `serving.traffic_changed` | A traffic split changed (`exa serve traffic`, the dashboard, the agent) | Yes |
| `serving.shadow_changed` | Shadow mirroring was enabled or disabled for a model | Yes |
| `serving.snapshot_published` | The control plane published a new [serving snapshot](serving-snapshot.md) generation | Yes |
| `model.alias_changed` | An MLflow alias moved or was removed (promote, rollback, autopilot, the training flow, the agent, the dashboard) | Right after MLflow accepts it |
| `drift.status_changed` | A model's prediction-drift status changed, e.g. `OK` → `CRITICAL`, as scored by the control plane every `CONTROL_PLANE_DRIFT_EVAL_SECONDS`. Carries `previous` (null the first time a model is seen, already drifting), `status`, `z_score`, `live_mean`, `baseline_mean` | Yes, with the status it records |
| `alert.drift` | The same change, into `WARNING` (`warn`) or `CRITICAL` (`critical`), in the platform's alert shape | Yes |
| `audit.recorded` | Any audit event, when `EXAMLOPS_AUDIT_STREAM` is on: the fields the hash chain covers, so a receiver can verify the chain ([audit trail](audit-trail.md#streaming-to-a-siem)) | Yes, with the audit row |

"Committed with the change" means the event row is written in the same database transaction as the
change itself: both happen or neither does. Aliases live in MLflow, which cannot join that
transaction, so `model.alias_changed` is enqueued immediately after MLflow accepts the move. If that
enqueue fails, the failure is logged and the promotion still stands; serving's alias poll
(`RAY_RELOAD_POLL_SECONDS`) is the backstop. A guard test fails when new code moves an alias
without calling `examlops.events.alias_changed`.

The `serving.*` payloads carry `model` (as typed) and `model_key` (lower-case, the key the serving
plane matches on). `model.alias_changed` carries `alias`, `version` (`null` when `removed`),
`previous_version` and `via` (which surface made the change).

## Schemas: the contract a consumer can rely on

Every topic above has a JSON Schema (draft 2020-12) for its `data`, in `examlops.events.schemas`.
The envelope names it: `"dataschema": "urn:examlops:event-schema:retrain.run_completed"`.

```bash
python -m examlops.events.schemas                 # every schema, as JSON
```

```python
from examlops.events import schemas

schemas.schema_for("model.alias_changed")        # the JSON Schema document
schemas.validate("model.alias_changed", data)    # [] when valid; no dependencies needed
```

The schemas describe what producers send. Fields the schema does not list may appear, and a
consumer must ignore what it does not know. The committed `event-contract.json` is the published
snapshot. The build fails on any change an existing consumer could trip over: a dropped topic, a
field removed or no longer required, a widened type (`string` → `string | null`), a new enum value
or a looser bound. Such a change ships as a new topic version (`<topic>.v2`), published alongside
the old one for as long as consumers need it. Adding an optional field or a new topic is not
breaking; the snapshot is regenerated with `python -m examlops.events.schemas --write`. Topics
hand-published with `exa events publish` have no schema and carry no `dataschema`.

## Who consumes it

| Consumer | How | What it does |
|---|---|---|
| Dashboard live stream (`GET /api/v1/stream`) | a broadcast watch in every dashboard replica | Relays every platform event onto the dashboard's SSE channels, so an open dashboard sees what the CLI, the control plane, the agent or another replica did ([dashboard architecture](../dashboard/architecture.md#realtime-gateway-sse)) |
| Autopilot (`exa autopilot follow`) | durable consumer `autopilot` on `retrain.run_completed` | When a retrain finishes, runs that model's autopilot cycle at once, with every gate (kill switch, lease, policy, eval and judge gates, rollback declaration), instead of at the next scheduled cycle. A busy lease means redelivery with backoff, not a lost event. Several copies share the work. |
| skipper-watch (`python -m skipper.watch --daemon`) | durable consumer `skipper-watch` on `retrain.*` | Raises `alert.retrain` (critical for FAILED/CRASHED, warn for MISSING) the moment a training run does not finish, through the watch's usual outbox + audit + episodic-memory fan-out; the alert then reaches the dashboard's live stream as `alert.retrain`. Once per run. |
| Serving snapshot projector | the outbox watermark of `serving.*` / `model.alias_changed` | Recompiles the [serving snapshot](serving-snapshot.md) within a second; replicas read it from the `examlops-serving` KV bucket |
| Your own | `EventConsumer` (below) | Durable, at-least-once, deduplicated, with a dead-letter subject |

A **broadcast watch** (`JetStream.watch`) and a **durable consumer** (`EventConsumer`) differ.
Every process that watches gets every new event and acknowledges nothing, which suits live views
where showing an event twice does no harm. A durable consumer shares events among the processes
that use its name, and resumes where it stopped, which suits anything with an effect.

## Delivery guarantees

| Guarantee | How |
|---|---|
| Not lost | The outbox row is committed with the change; the relay retries until the broker accepts it |
| Not duplicated in the stream | The relay sets `Nats-Msg-Id` to the outbox id; JetStream drops a repeat within `EXAMLOPS_NATS_DUPLICATE_WINDOW` |
| At least once to each consumer | Consumers acknowledge after handling; an unacknowledged message is redelivered |
| An effect happens once | Each consumer records handled ids in `event_inbox` and skips a redelivered event |
| One bad event cannot stall a consumer | After its delivery limit, a failing event is parked on `examlops.dlq.<consumer>` and acknowledged |
| A broker outage costs one timeout, not one per event | The relay stops a batch at the first unreachable answer and defers the rest (below) |
| An outage never turns the backlog into poison | `EXAMLOPS_EVENT_MAX_ATTEMPTS` is spent only by an event a broker actually refused |

### When the broker is not there

A publish can fail two ways, and the relay tells them apart, because the right answer is opposite
in each case:

| What happened | What the relay does | What you see |
|---|---|---|
| The broker **refused this event** (a bad subject, an oversized payload) | Marks that row failed, charges one attempt, carries on with the rest of the batch | `failed` in the relay result; after `EXAMLOPS_EVENT_MAX_ATTEMPTS` the row is poison and `EventOutboxPoison` fires |
| The broker **is not there** (no servers, connection closed, a timeout) | Stops the batch, returns every row still in it to the queue **without charging an attempt** | `deferred` and `unavailable` in the relay result; `/health` is `degraded` with `runtime.event_relay_error`; the backlog grows and `EventOutboxStalled` fires |

Both were once handled the same way, and the chaos drill
([game days](game-days.md#the-event-backbone-dies)) measured what that cost with NATS killed and
six events queued: the relay paid the client's full `EXAMLOPS_NATS_TIMEOUT` for each event in turn
— 36 seconds for one cycle, over eight minutes at the default batch of 100 — and because `/health`
reports the last *completed* cycle, it said `ok` for the whole time. Five such cycles would also
have exhausted every queued event's retry budget, turning a broker that was merely away into a
backlog no retry would ever drain.

A publisher classifies its own failures (`is_unavailable`), so a client library's own error types
decide rather than a guess at its wording.

## Writing a consumer

```python
import threading
from examlops.events.consumer import EventConsumer

def on_run(event: dict) -> None:
    if event["data"]["run_state"] == "FAILED":
        notify(event["data"]["model_name"], event["id"])   # idempotent on event["id"]

stop = threading.Event()
EventConsumer("run-alerts", on_run, subjects="examlops.events.retrain.>").run_forever(stop)
```

The name is the consumer's durable identity: two processes with the same name share the work; a
different name gets its own copy of every event. A handler that raises is retried with a growing
delay (`max_deliver`, default 5) and then parked. Because a crash between a successful handler and
its inbox record re-runs the handler once, external effects should be idempotent on
`event["id"]`.

## Operating it

```bash
exa events stats                         # outbox backlog: pending / published / poison
exa events relay --loop                  # drain the outbox by hand (the control plane relays its own)
                                         # — stops and says so if the broker is unreachable
exa events tail                          # the last 20 events on the backbone
exa events tail --topic 'retrain.*'      # one family of events
exa events tail --dlq run-alerts         # what a consumer parked, with the error
```

`GET /health` on the control plane shows the relay's last result, the outbox counts and the age
of the oldest unpublished event under `runtime`. With the NATS publisher, `exa events stats` also
lists each durable consumer's lag and dead letters.

### Metrics and alerts

The control plane's `/metrics` carries the backbone's health. The outbox gauges are read from the
database on every scrape. The consumer gauges are read from JetStream every
`CONTROL_PLANE_EVENT_BACKBONE_STATS_SECONDS`, and only when the publisher is `nats`; with any other
publisher those series are absent rather than zero.

| Metric | Meaning |
|---|---|
| `examlops_event_outbox_pending` / `_poison` | Events not yet published / events that exhausted `EXAMLOPS_EVENT_MAX_ATTEMPTS` |
| `examlops_event_outbox_oldest_pending_age_seconds` | How long the oldest unpublished event has waited (0 when none) |
| `examlops_event_relay_events_total{outcome}` | Events the relay handled: `published`, `failed` (the broker refused them) or `deferred` (there was no broker to ask) |
| `examlops_event_relay_cycle_errors_total` | Relay cycles that failed outright (publisher misconfigured, broker unreachable) |
| `examlops_event_consumer_pending{consumer}` | Events a durable consumer has not been delivered yet (its lag) |
| `examlops_event_consumer_ack_pending{consumer}` | Delivered, not yet acknowledged |
| `examlops_event_dlq_messages{consumer}` | Events parked on the consumer's dead-letter subject, within the stream's retention |

If a read fails, each gauge keeps its last value, and a failed outbox read also increments
`examlops_metrics_scrape_errors_total`. Zero backlog is the healthy reading, so a broken read must
never report zero.

| Alert | Fires when |
|---|---|
| `EventOutboxStalled` | The oldest unpublished event has waited over 5 minutes, for 5 minutes |
| `EventOutboxPoison` | Any event has exhausted its publish attempts |
| `EventRelayFailing` | Relay cycles have been failing for 10 minutes |
| `EventConsumerLagging` | A consumer is more than 1000 events behind for 15 minutes |
| `EventDeadLettered` | A consumer parked an event in the last 15 minutes |

## Related

- [Control plane and serving plane](../architecture/control-and-serving-planes.md)
- [Control plane guide](control-plane.md) — asynchronous commands and the events they emit

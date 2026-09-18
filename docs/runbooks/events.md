# Runbooks: event backbone

Alerts about events on their way from the platform to their consumers: the transactional outbox,
the control plane's relay that publishes it to the broker (NATS JetStream in production), and the
durable consumers (the autopilot follower, Skipper's watch, the dashboard's live stream).
Background: [Event backbone](../guides/event-backbone.md).

Two commands cover most of it:

```bash
exa events stats                  # outbox: pending / published / poison, oldest pending age;
                                  # with NATS: each consumer's pending and dead-lettered counts
exa events tail --dlq <consumer>  # what a consumer parked, with the error
```

An event is never lost between the database write and the broker. It is committed to the outbox
in the same transaction as the change it announces, and stays there until published. What can
happen is **delay**, and serving, the autopilot and the dashboard act on stale state until it
clears.

## EventOutboxStalled {#eventoutboxstalled}

**Meaning:** the oldest unpublished outbox event is more than 5 minutes old, sustained for 5.

**Impact:** consumers are behind: a promotion, traffic split or retrain result has not reached
them yet. (The serving snapshot also recompiles on a timer, so serving catches up within
about a minute regardless.)

**Check:** `exa events stats`; the control plane's `/health` field `runtime.event_relay_error`;
`exa stack logs --service control-plane --tail 200`.

**Fix:**

- **The relay is off.** Is `CONTROL_PLANE_EVENT_RELAY_SECONDS` 0, or does another process own the
  relay? Run `exa events relay --loop` as a stopgap.
- **The broker is unreachable:** see [EventRelayFailing](#eventrelayfailing).
- **The publisher is `log`**, the default with no broker: events are "published" to the log and
  the backlog should be empty. If it grows, the relay is not running at all.
- **The control plane stays in rotation while this lasts**, by design: events are durable in the
  outbox, so `/readyz` keeps answering 200 while `/health` says `degraded` and names the failure in
  `startup_checks.event_publisher` and `runtime.event_relay_error`. Nothing is lost, and the backlog
  drains on its own within seconds of the broker returning — measured by the
  [backbone chaos drill](../guides/testing.md#chaos-drills).
- **`nats-py` is missing** (`the NATS event backbone needs the 'nats-py' package`): the image was
  built without the `events` extra. Released images carry it; a custom build needs
  `pip install 'examlops[events]'`.

## EventOutboxPoison {#eventoutboxpoison}

**Meaning:** at least one outbox event used up its publish attempts (`EXAMLOPS_EVENT_MAX_ATTEMPTS`,
5) and will not be retried.

**Impact:** that event's consumers never see it. The row stays in `event_outbox`, with its
`last_error`, as evidence.

**Check:** `exa events stats` counts them. The rows are those in `event_outbox` with
`published_at` NULL and `attempts` at the maximum; `last_error` says why. The cause is a payload
the broker refused — most often one that is too large, or a subject it will not accept.

A broker that was merely *away* does not produce this: an unreachable broker defers the batch and
spends no attempt ([when the broker is not there](../guides/event-backbone.md#when-the-broker-is-not-there)),
so an outage shows as a growing backlog and `EventOutboxStalled`, never as poison. If you see
poison during an outage, the deferral is not working — that is a platform defect, not a payload
problem.

**Fix:** fix the cause first. To publish the rows again, reset their attempts in the platform
datastore:

```sql
UPDATE event_outbox SET attempts = 0 WHERE published_at IS NULL AND attempts >= 5;
```

The relay picks them up on its next cycle. Consumers deduplicate by event id, so a re-published
event is not applied twice.

## EventRelayFailing {#eventrelayfailing}

**Meaning:** every relay cycle has failed for 10 minutes.

**Impact:** the outbox grows (see [EventOutboxStalled](#eventoutboxstalled)). Nothing is lost; it
publishes once the relay recovers.

**Check:** `/health` `runtime.event_relay_error`, then the configuration:
`EXAMLOPS_EVENT_PUBLISHER` (`nats` in production) and `EXAMLOPS_NATS_URL`. Can the control plane
reach the broker? (`exa stack status` shows the `nats` service; with the segmented overlay both
must be on the `control` network.)

`runtime.event_relay_error` distinguishes the two cases. **`event backbone unavailable (…); N
event(s) waiting in the outbox`** means there is no broker to talk to: everything is queued,
nothing is lost, and it drains by itself — expect it within about ten seconds of the broker going
away. **`N event(s) failed to publish`** means the broker answered and refused those events, which
is a payload or subject problem and will end as [EventOutboxPoison](#eventoutboxpoison) if it is
not fixed.

**Fix:** restore the broker or correct the URL or credentials. The relay recovers by itself.

## EventBackboneMetricsUnreadable {#eventbackbonemetricsunreadable}

**Meaning:** the control plane could not read JetStream's consumer and dead-letter statistics while
refreshing its gauges, in the last 10 minutes.

**Impact:** the two alerts built on those gauges —
[EventConsumerLagging](#eventconsumerlagging) and [EventDeadLettered](#eventdeadlettered) — are
judging numbers of **unknown age** and cannot be trusted while this fires. The backbone itself may
be perfectly healthy: this is about *reading* it, not about publishing or consuming.

A failed read deliberately **keeps the last values** rather than publishing zeros, because zero lag
is the reading that means "healthy" and an unreachable broker must not be able to produce it. That
choice is what makes this alert necessary — without it, stale numbers and true numbers look
identical. The approval gauges on the same endpoint have had the equivalent signal
([ApprovalMetricsUnreadable](control-plane.md#approvalmetricsunreadable)) for longer; the backbone
gained it on 2026-09-14.

**Check:**

1. `exa stack logs --service control-plane --tail 200 | grep "Could not read event backbone"` — the
   cause is logged with each failure.
2. Is NATS reachable from the control plane? `exa events stats` asks it directly.
3. `EXAMLOPS_EVENT_PUBLISHER` must be `nats`; with any other publisher there is no broker to read
   and the gauges are absent rather than stale, so this cannot fire.

**Fix:** restore the broker or the route to it. The next successful refresh replaces the gauges and
the alert clears on its own.

## EventConsumerLagging {#eventconsumerlagging}

**Meaning:** a durable consumer has more than 1000 events waiting, sustained for 15 minutes.

!!! warning "Check the gauge is fresh first"
    This reads a gauge refreshed from JetStream by the relay loop, and a failed refresh keeps the
    last value. If [EventBackboneMetricsUnreadable](#eventbackbonemetricsunreadable) is also firing,
    the number here is of unknown age — the lag may have grown since, or cleared.

**Impact:** the consumer acts late: the autopilot follower on finished retrains, Skipper's watch on
alerts. Events older than the stream's retention (`EXAMLOPS_NATS_MAX_AGE_SECONDS`, 7 days) are gone
before it reads them.

**Check:** `exa events stats` (the consumer's pending and ack-pending counts); is its process
running? `exa stack status` shows `autopilot-follower` and `skipper-watch` (Compose profile
`events`).

**Fix:** start or restart the consumer. It resumes from its durable position. If it runs but is
slow, look at its logs for a handler that blocks or fails on every event (such an event goes to
the dead-letter subject after its delivery attempts, see below).

## EventDeadLettered {#eventdeadlettered}

**Meaning:** a consumer moved at least one event to its dead-letter subject
(`examlops.dlq.<consumer>`) in the last 15 minutes.

**Impact:** the consumer kept going, but the effect of that event did not happen: a retrain result
not followed up, an alert not forwarded.

**Check:** `exa events tail --dlq <consumer>` shows the parked event and the handler's error.

**Fix:** fix the handler or the data the error names, then repeat the effect by hand. For the
autopilot, that means running `exa autopilot run <model>`.

!!! warning "This alert clears while the events are still parked"
    It watches the dead-letter count **growing** (`delta(...[15m])`), not its being non-zero. So it
    resolves fifteen minutes after the last event is parked, whether or not anything was done about
    the ones already there — and their effects still have not happened.

    That is deliberate: an absolute threshold would page forever over a single parked event until
    someone drained the queue by hand, and an alert that cannot be cleared is one people learn to
    ignore. It does mean **the alert clearing is not a sign the backlog is gone.** Only
    `exa events tail --dlq <consumer>` tells you that.

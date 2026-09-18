# Game days: breaking the platform on purpose

A game day is a scheduled, deliberate failure: you break one dependency, watch what the platform
does, and compare it with what the documentation promises. Everything here is automated, so a game
day is an hour of attention rather than a week of preparation.

**Why bother when the tests are green.** Each of these drills was written against behaviour the
documentation already described. Most of them found something on their first run that every unit
test had passed over:

- with Postgres killed, the control plane did not report itself unhealthy — it stopped answering at
  all, because nothing bounded a query whose server had vanished;
- the control-plane image could not use the event backbone at all, because the image was built
  without the client library;
- a bus outage took every replica out of rotation, although events were safely queued and the API
  was serving;
- with the broker gone, `/health` kept saying `ok` while the outbox filled: the relay spent one
  client timeout per queued event, so a single cycle took 36 seconds and health only ever showed
  the cycle before it — and those retries were quietly using up each event's retry budget;
- a model-server Deployment written the obvious way — no readiness probe, because the container
  starts in a second — was sent 2069 inference requests it could not answer during a single
  scale-up, since the model takes half a minute to load;
- a node lost outright kept its share of inference for minutes: the pod stays `Ready` in the API
  because the kubelet that would say otherwise is gone, so every request routed there hung for the
  caller's full timeout until Kubernetes stopped routing to it two minutes later;
- and the gateway in front of it had no way to notice — no health check, no outlier detection — so
  it kept choosing an endpoint that had stopped answering. That one is now fixed rather than only
  documented.

None of those is visible without breaking something.

## What the platform does when something breaks

The one-page version, each row linking to the drill that measured it and the page that explains it.
Everything here is a number from a run, not an intention.

| What fails | What callers see | What makes that true |
|---|---|---|
| The datastore goes away | `/readyz` 503 in ~2 s, writes refused cleanly, recovery in ~0.03 s with no restart | Bounded connections and a fast-fail cache ([why](postgres-backend.md#when-the-datastore-goes-away)) |
| The event broker goes away | Nothing is refused: events queue in the outbox and drain by themselves | The bus is off the write path ([the backbone](event-backbone.md#when-the-broker-is-not-there)) |
| Both at once | Clean 503s while they are gone, service back when the store alone returns | Readiness covers what a request needs, not everything |
| A model-server replica dies mid-request | The router retries once within the deadline | [Replicas and scaling](../components/ray-serve.md#replicas-and-scaling) |
| The model server is overloaded | A prediction or a shed `503` — never a 500, never a hang | A bounded queue ([load shedding](../components/ray-serve.md#overload-deadlines-and-load-shedding)) |
| A serving pod is deleted, upgraded or killed | Only the keep-alive connections pinned to it, in one instant; one retry hides even that | `preStop`, readiness and a surge rollout ([on Kubernetes](serving-on-kubernetes.md)) |
| A whole serving node is lost | Straight at the Service: failures for ~2 minutes. Behind the gateway: 5–10 requests inside seconds | The gateway ejects an endpoint that stops answering ([how](serving-gateway.md#when-a-model-server-stops-answering)) |
| A control-plane replica crashes mid-dispatch | Nothing accepted is lost and nothing runs twice | Claim leases plus a per-command idempotency key |
| A control-plane replica is cut off but alive | The command reaches a terminal state; Prefect still starts exactly one run | The same idempotency key — and a caveat worth reading ([dead commands](../runbooks/control-plane.md#controlplanecommanddead)) |
| The platform's schema is destroyed | Everything comes back from a bundle, audit chain included, with the same head hash | A `pg_dump` tier that is restored, not merely written ([can you actually restore it](backup-restore.md#can-you-actually-restore-it)) |
| The object store loses its buckets | Artifacts come back byte for byte, into a bucket the restore recreates | An objects tier exercised against a real MinIO, not an in-memory double ([recovery order](backup-restore.md#recovery-order-full-disaster)) |
| Nobody has started the backup container | It builds, runs a cycle, and writes a bundle that verifies and still holds the audit chain | The RPO's mechanism is exercised, not just its YAML ([does it run](backup-restore.md#does-the-container-behind-the-rpo-run)) |

The rows are not aspirations: each is the subject of a drill below, and each number came out of one.

## Run them

```bash
make chaos-drills      # builds the two images from this tree, then runs the seven Docker drills
```

About ten minutes on a laptop, most of it building images.

```bash
make chaos-drills-kind # the four that need a cluster: CP failover, CP partition, pod churn, node loss
```

About half an hour, and it needs `kind`: each drill creates and deletes its own throwaway cluster,
and the node-loss one spends minutes waiting out Kubernetes' own eviction timers. Each drill prints
what it measured. To run one on its own:

```bash
docker build -f platform/services/control_plane/Dockerfile -t exa-chaos/examlops-control-plane:tree .
EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_datastore_outage_drill_live.py -v -s
```

Everything runs in throwaway containers on their own network, with random ports. Nothing touches a
running stack, and each drill removes what it created, including when it fails.

### They also run every week, on their own

`.github/workflows/chaos-drills.yml` runs the Docker set every **Saturday at 04:17 UTC** and keeps
each run's log as an artifact (`chaos-drills-docker`, 90 days) — on a passing run too, because a
drill's output is a measurement and one measurement is only readable next to the last one.

It is a **report, not a gate**: it is deliberately outside `ci-ok`, the single check branch
protection requires. Two reasons, and they are worth keeping in mind before anyone promotes it:

- the drills take tens of minutes and build two large images, so they cannot sit on a pull request;
- they measure *timing* on a shared runner, where a slow neighbour is not a regression. A gate that
  goes red for that teaches everyone to re-run it, which is how a suite stops meaning anything.

What the weekly run is for is the drift a green pull request cannot show. A retry budget, a queue
bound, an outlier-detection threshold or a readiness probe can all stop doing their job while every
unit test stays green — because unit tests run against a healthy stack. Only a drill notices, and a
drill nobody runs notices nothing.

The cluster drills are **not** scheduled. Run them from the Actions tab with
*Run workflow → Also run the kind cluster drills*. Each builds a 1.9 GB serving image and loads it
into every node of a throwaway multi-node cluster.

**All four pass locally** — 27 tests in about 30 minutes of cluster time, measured 2026-09-13 on a
dev host with 44 GB free — so a failure in CI is a finding about the runner rather than the platform
until you have reproduced it on a laptop. Whether a hosted runner can carry them at all is still
untested: that is the open question, not whether the drills work.

`tests/unit/test_chaos_drills_are_documented.py` keeps the four places honest: the Makefile defines
the drill set, and every target in it must be invoked by that workflow, every drill must have a row
in [the testing guide](testing.md) and a section on this page, and every `docs/…` path a drill names
must exist. Add a drill and forget one of them, and the unit suite says so.

## How to read the numbers on this page

Each section below has two columns, and only one of them is a promise.

**"Must hold" is the contract**, and the drill asserts it: every answer is a prediction or a shed
503, the command reaches a terminal state, exactly one Prefect run exists, the ejected endpoint
comes back. If one of those stops being true the drill fails, and that is a defect.

**"Typical measurement" is evidence, not a specification.** These are ranges observed across the
runs that produced them, on a developer host with other work on it — a laptop or the dev node, not a
quiet benchmark rig. Expect your own numbers to differ by a factor, and read the *shape*: that a
bounded queue's p99 is tens of times lower than an unbounded one's, not that it is 312 ms. A number
here drifting is normal; a "must hold" row failing is not.

That distinction is load-bearing, because it was got wrong once. A single node-loss run suggested
`tolerationSeconds: 20` cut the cost of a lost node seventeenfold; two further runs put the change
inside the noise, and the claim had to be retracted from three pages
([the measurement that replaced it](serving-on-kubernetes.md#when-a-whole-node-goes-away)). One
run of a timing measurement is an anecdote. Where a drill's number matters enough to act on, it is
quoted here as a range and the drill asserts the property rather than the number.

## What each one breaks

### The datastore dies

`tests/integration/test_datastore_outage_drill_live.py` kills Postgres under a real control plane
and a real serving-gateway authorization service, holds the outage, and starts it again.

| Must hold | Typical measurement |
|---|---|
| `/readyz` answers 503 quickly and never hangs past a probe's timeout | about 2 s, then instant |
| A retrain submitted during the outage is refused cleanly, naming the store | `503` |
| A virtual key the gateway verified before the outage keeps working | allowed, from cache |
| A key it has never seen is refused, never waved through | `503` |
| No credential is still refused | `401` |
| Everything recovers with no restart | ready again 0.02–0.03 s after Postgres accepts connections |
| The audit hash chain still verifies, and a pre-outage retrain retried is the same command | — |

**If `/readyz` hangs instead of answering**, the connection bounds are not in force: check
`EXAMLOPS_POSTGRES_POOL_TIMEOUT`, `EXAMLOPS_POSTGRES_TCP_TIMEOUT_MS` and whether your DSN overrides
them ([what is bounded](postgres-backend.md#when-the-datastore-goes-away)).

### The event backbone dies

`tests/integration/test_backbone_outage_drill_live.py` kills NATS while retrains keep arriving.

| Must hold | Typical measurement |
|---|---|
| Work is still accepted: the bus is not on the write path | every retrain `202` |
| The replica stays in rotation | `/readyz` 200 throughout |
| The failure is visible | `/health` `degraded`, `startup_checks.event_publisher` failing, `runtime.event_relay_error` set |
| The backlog is measurable | `examlops_event_outbox_pending` > 0 |
| When the broker returns, the backlog drains by itself | about 2 s |
| Every published row is exactly one message in the stream | counted in both the store and the broker |

It then kills the **datastore as well**, and brings them back one at a time, because losing one
dependency is the easy case:

| Must hold | Typical measurement |
|---|---|
| With both gone, the refusal is clean and quick — and the process stays alive to be probed | unready in 2.0 s, retrains `503`, `/livez` 200 |
| The store alone coming back is enough to serve again | ready 3.2 s after Postgres accepts, retrains `202` |
| What it still cannot publish, it queues | `examlops_event_outbox_pending` > 0 |
| The outage the store cannot fix is *visible* while it lasts | `/health` `degraded` within about 10 s, `runtime.event_relay_error` naming it unavailable |
| Waiting for the broker does not turn the backlog into poison | `poison` 0 |
| When the broker follows, the backlog drains exactly once | about 2 s; messages == published rows |

**If the control plane goes unready**, readiness has been widened beyond what a request needs
([what makes a replica unready](../components/control-plane.md#health-and-readiness)). **If events
never leave the outbox even with the broker up**, the image is missing the `events` extra. **If
`/health` says `ok` while the outbox is filling**, the relay is failing events one at a time
instead of deferring the batch
([when the broker is not there](event-backbone.md#when-the-broker-is-not-there)) — this is what the
drill's combined-failure section found, and it hid a real outage for 36 seconds.

### The model server is overloaded

`tests/integration/test_serving_overload_drill_live.py` sends about ten times what one replica can
finish, with the queue bounded and then unbounded.

| Must hold | Typical measurement |
|---|---|
| Every answer is a prediction or a shed `503` — never a 500, never a dropped connection | 729–744 predictions, 2856–2871 shed |
| Bounded latency for the requests that succeed | p50 163–175 ms, p99 312–364 ms |
| The bound is what buys that | unbounded: p50 6.0–6.3 s, p99 14–16 s — **≈40× the bounded p99** |
| A request with a spent budget is refused without running the model | `504` |
| The server serves normally once the burst stops | 15–18 ms |

**If nothing is shed**, the load was below capacity and the run proved nothing: raise the rate until
the shed count is a large fraction of what you sent.

### The platform's schema is destroyed

`tests/integration/test_postgres_dr_roundtrip_live.py` is the drill for the failure every other one
on this page assumes away: the data is gone, and the only thing left is a backup.

It starts its own Postgres, seeds a chained audit log and a traffic split, takes a `postgres`-tier
bundle, `DROP SCHEMA … CASCADE`, and restores.

| Must hold | Typical measurement |
|---|---|
| The bundle contains the platform datastore, not an empty `platform.db` | tier `ok`, item `platform: ok` |
| The bundle verifies before anything is touched | checksums match |
| Restoring over a live schema is refused without `--force` | `ValueError` naming `force` |
| Every event comes back | 5 of 5 |
| The chain's **head hash** is the one from before | identical |
| The traffic split comes back | `{Production: 90, Canary: 10}` |
| Whole drill | ~14 s |

**The head hash is the assertion that matters.** A restore that put every row back but rewrote the
log — re-hashing it, renumbering it, reordering it — would satisfy a row count and destroy the only
property the audit log exists for. Counting rows is how a DR drill passes while the thing it is
protecting is already broken.

This drill needs `pg_dump` and `pg_restore` on `PATH` and **skips** without them. That is
deliberate, and it is the one skip on this page worth checking for: a DR drill that silently did not
run is worse than not having one, because it shows green. It found a real defect the first time it
ran, described in [the backup guide](backup-restore.md#can-you-actually-restore-it).

### The object store loses its buckets

`tests/integration/test_objects_dr_roundtrip_live.py` covers the step this guide's recovery order
puts **first**: model artifacts must exist before the registry's references to them resolve.

It puts artifacts in a real MinIO, backs them up, deletes the bucket outright, and restores.

| Must hold | Typical measurement |
|---|---|
| The backup mirrors every object | 4 of 4 |
| The restore recreates a bucket the disaster took | `bucket_created: true` |
| Every object's **digest** matches what went in | identical |
| Listing does not stop at one page | 1050 of 1050 objects |
| Whole drill | ~15 s |

**Digests, not counts.** A restore that put back the right number of empty or truncated files
passes any check that counts objects, and the failure surfaces much later as a model that will not
load.

This drill found that the restore raised a raw `NoSuchBucket` when the bucket was gone — the exact
state a real disaster leaves, and the documented first step. It had passed in the unit suite
because the in-memory S3 double created buckets on upload; the double is now as strict as MinIO.

### Nobody has ever started the backup container

`tests/integration/test_backup_sidecar_live.py` is not a failure drill — it is the one that asks
whether the mechanism behind a published objective exists at all. The SLO table promises **RPO ≤ 1
h**, and what meets it is a container: the Compose `backup` sidecar. The release workflow builds and
signs that image, Compose wires it, and unit guards check its YAML. Nothing ever ran it.

| Must hold | Typical measurement |
|---|---|
| The shipped image builds from this tree | ~1 min cold, seconds cached |
| One cycle exits 0 and names its bundle | `status=partial`, which is honest — see below |
| The **platform datastore** is in it | `sqlite: platform: ok` |
| The instance's own config is in it | `config: data-root:config: ok` |
| The bundle verifies with the host's checksums | `ok` |
| The database inside still carries its chain | 5 of 5 events |
| Whole drill | ~11 s after the build |

**`status=partial` is the expected result, not a failure.** In a bare drill there is no MinIO, no
Postgres and no agent database, and each is skipped *with its reason recorded*. That is why the
drill asserts on the manifest rather than on the exit code: a bundle consisting entirely of skipped
tiers also exits 0, and "the cycle ran" is not "the data is backed up".

It found that the bundles this container writes could not be verified by the operator who later
reads them — see [the backup guide](backup-restore.md#does-the-container-behind-the-rpo-run).

### The serving plane loses pods on Kubernetes

`tests/integration/test_serving_kind_drill_live.py` (`EXAMLOPS_KIND_SERVING_LIVE=1`, needs kind)
deploys the model server twice in a throwaway cluster — once with the settings
[the guide recommends](serving-on-kubernetes.md), once with Kubernetes' defaults — and puts eight
callers on each while pods are deleted, upgraded, killed and added.

| Must hold | Typical measurement |
|---|---|
| A graceful deletion cuts only the connections pinned to that pod, in one instant | 6–7 lost (of 11 902 and 17 321 sent) |
| A caller that does not hold connections loses nothing at all | 0, in both runs |
| A pod with no readiness probe is sent inference before its model is loaded | **2014–2069 lost** — about 18 % of everything sent |
| The same scale-up with the probe costs nothing, and the pod is useful when it says it is | 0; ready in 33 s in both runs |
| A rolling upgrade costs no capacity | 0–5 lost; p99 39–78 ms |
| A pod killed outright loses no more than that, and not for longer | 3–6 lost, none after the kill instant |
| One retry — what the gateway already does — erases every one of those | 0, in both runs |

Two runs, 2026-09-11 and 2026-09-13, on different load volumes — which is why the counts are ranges
and the *proportions* are what to read. The probe-less loss is the one number worth remembering: it is
not a handful of requests, it is roughly one in five of everything sent during the scale-up.

It takes about eight minutes, most of it loading a 1.9 GB image into the cluster and four model
loads. **If the probe-less deployment loses nothing**, the drill proved nothing: the model loaded
faster than the load ran, so lengthen the load or use a slower model.

### The serving plane loses a whole node

`tests/integration/test_serving_node_loss_kind_live.py` (`EXAMLOPS_KIND_NODE_LOSS_LIVE=1`, needs
kind) builds a three-node cluster, spreads the model server one replica per worker, pins the callers
to the control-plane node so they survive, and then loses a worker two ways.

| Must hold | Typical measurement |
|---|---|
| A planned `kubectl drain` costs what a graceful eviction costs, and no more | within the connections pinned to the evicted pod |
| A lost node black-holes only its own share — the survivor answers throughout | 110 to 2151 requests, run to run |
| Nothing the platform answers is an error: the failures are hangs or refusals from a dead address | no HTTP status among them, only transport errors |
| The black hole closes with nobody intervening | when the EndpointSlice drops the dead pod — which is never the pod's deletion: a pod on an unreachable node stays `Terminating` until the node returns |
| A shorter `unreachable` toleration does **not** get the dead pod out of the Service sooner | 132-139 s either way, over five measurements; in the latest the short toleration lost **more** (117 against 110) — the drill keeps the negative result so nobody re-derives the wrong fix |
| `whenUnsatisfiable: DoNotSchedule` cannot replace a lost node's replica | one `Pending` pod, one ready endpoint |
| The node coming back restores capacity by itself | both endpoints ready again |
| Behind the gateway, on a headless Service, the same node loss is seconds, not minutes | 5-10 requests lost inside a 10 s error window, against **118-130 s** straight at the Service (129.5 s in the latest run) |
| The replicas are really on two nodes before a node is stopped | asserted, not assumed — see below |

It also found the quieter trap: **Kubernetes never rebalances a Deployment.** After one drain both
replicas sat on the surviving worker and stayed there, every check green — and a two-replica service
with both replicas on one node is not highly available at all. The drill now re-spreads and asserts
the premise before each measurement, which is the same discipline an operator needs
([why](serving-on-kubernetes.md#the-spread-constraint-is-a-decision-not-a-default)).

This is the drill that found the trap worth knowing about: with the defaults, **a node lost outright
keeps receiving its share of inference for minutes, and each of those requests hangs for the
caller's whole timeout** rather than failing fast
([what to do about it](serving-on-kubernetes.md#when-a-whole-node-goes-away)). It takes about
fifteen minutes, most of it Kubernetes' own eviction timers.

### A model server stops answering behind the gateway

`tests/integration/test_serving_gateway_ejection_live.py` (`EXAMLOPS_GATEWAY_LIVE=1`, Docker only,
about two minutes) runs the committed Envoy configuration against two model-server endpoints and
wedges one — it accepts connections and never answers, which is what a pod on a lost node looks like
to a proxy.

| Must hold | Typical measurement |
|---|---|
| Both endpoints healthy: everything is served, and both are being probed | 60 of 60 |
| Without the health checks, a black hole keeps taking its share | 10–13 of 20 requests timed out |
| The shipped gateway stops choosing it | ejected after 8.4 s; 60 of 60 afterwards |
| It comes back on its own when it recovers | in rotation again after 9.4 s |
| A lone unhealthy endpoint is still used | no invented `503` — the panic threshold protects a one-container deployment |
| The ejection is visible to monitoring, and clears itself | `envoy_cluster_membership_healthy` 1 of 2 while ejected, 2 of 2 after — what [ServingEndpointsUnhealthy](../runbooks/serving.md#servingendpointsunhealthy) watches |

**If the wedged endpoint is never ejected**, the health check is missing from the cluster
([what it looks like](serving-gateway.md#when-a-model-server-stops-answering)). **If the drill's
"detection time" is under a second**, it is measuring a gateway that had already ejected the endpoint
in an earlier test — the endpoint has to be healthy and *serving inference* before it is wedged.

### Control-plane replicas are killed mid-dispatch

`tests/integration/test_control_plane_failover_kind_live.py` (`EXAMLOPS_KIND_FAILOVER_LIVE=1`, needs
kind, about four minutes) runs three control-plane replicas on a cluster against a real Postgres,
submits retrains without pause, and twice waits until a replica is *in the middle* of a dispatch
before killing its process outright — no shutdown code, the way a crash or an OOM kill ends it.

| Must hold | Typical measurement |
|---|---|
| Every retrain the platform answered `202` reaches a terminal, successful state | 144 of 144, and 154 of 154 — every accepted command, in every run |
| Nothing runs twice: one flow run per command, none shared | 146 calls for 144 commands, and 155 for 154 — the duplicates deduplicated by their key |
| A crashed replica's claim is taken over once its lease expires | 1–2 second attempts; both kills caught a dispatch in flight, in both runs |
| The Service recovers quickly enough that a client barely notices | longest gap between accepted submissions 3.0–3.7 s across runs |
| A retried submission with the same `Idempotency-Key` is the same command, whichever replica answers | no second command ever created |

The `409`s in its output (537 of them) are not failures: they are the platform refusing to start a
second retrain for a model and dataset that already has one in flight, which is what should happen
when a client hammers the same pair.

**If commands are left in `dispatching`**, a killed replica's claim is not being taken over — that
was the defect this drill found on its first run
([dead commands](../runbooks/control-plane.md#controlplanecommanddead)).

### A control-plane replica is cut off but still alive

`tests/integration/test_control_plane_partition_kind_live.py` (`EXAMLOPS_KIND_PARTITION_LIVE=1`,
needs kind, about three minutes) is the failure a kill cannot produce: a replica that still runs,
still holds its claim, and whose dispatch to Prefect hangs. Its claim lease runs out on paper, its
attempts run out for real, and the dispatch it was holding lands *afterwards*.

| Must hold | Typical measurement |
|---|---|
| The command reaches a terminal state rather than hanging in `dispatching` | `dead` after 5 attempts, 116 s |
| It really was dispatched more than once, with one key | 10 dispatch attempts reached Prefect |
| **Prefect started exactly one run for it** — the idempotency key is what makes takeover safe | 1 run for 10 dispatches |
| No replica was restarted and the API kept serving | 0 restarts, `/readyz` 200 |

Since it first ran, the orphan no longer has to be found by hand: burying a command asks Prefect
whether a run exists for its key, names it in the command's `last_error`, and raises
[ControlPlaneDeadCommandLeftARun](../runbooks/control-plane.md#controlplanedeadcommandleftarun).

What it found, and what the runbook now says: the cut-off replica **keeps** the command (it is
healthy and renews the claim, so no other replica takes it over), so the work dies even though two
healthy replicas could have done it — and because the slow dispatch lands anyway, Prefect ends up
running a job the platform has recorded as dead
([what to do](../runbooks/control-plane.md#controlplanecommanddead)). Whether a partitioned replica
should keep its claim or hand it back is a design decision; the drill pins today's answer with
numbers so it is decided rather than assumed.

## On your own installation

The drills above use throwaway containers. Running the same failures against a real installation is
the point of a game day, and it needs care.

1. **Staging first**, with the same shape as production: the same datastore engine, the same broker,
   the same replica counts. A drill against a single-replica staging install tells you nothing about
   a rolling restart.
2. **Announce it** and give it a window. Silence an on-call rotation only if you have agreed to;
   otherwise let the alerts fire — whether they fire, and how quickly, is part of what you are
   testing.
3. **Decide the abort condition in advance** ("if inference errors exceed X for 60 s, we stop") and
   have the undo command in the terminal before you start.
4. **Break one thing.** `docker kill` the datastore container, or scale the broker to zero, or block
   it with a network rule. One dependency at a time; a combined failure teaches you little until
   each single failure is understood.
5. **Watch what the runbook says to watch**, not the thing you happen to have open: the alert that
   should fire, `/readyz`, `/health`, and the queue depth.
6. **Restore, then write it down** — including the parts that went as expected.

A template worth keeping short:

| | |
|---|---|
| Date, who ran it, environment | |
| What we broke, and how | |
| What we expected (link the doc) | |
| What happened, with timings | |
| What surprised us | |
| Changes it caused (issues, fixes, doc edits) | |

The "what surprised us" row is the reason to do this at all. Every defect listed at the top of this
page came from that row.

## A cadence that survives contact

- **Every release:** `make chaos-drills` alongside the usual gates. The three kind drills
  (control-plane failover, serving-plane pod churn, serving-plane node loss) need a cluster and are
  run when their area changes. It is minutes, and it is what
  catches a regression in bounded failure, which no unit test sees.
- **Every quarter, on staging:** one dependency, following the steps above, with the on-call
  rotation live.
- **After any incident:** the drill that would have caught it, added here if it does not exist yet.

## What is not covered yet

Honest gaps, so nobody reads a green run as more than it is:

- **Partial network partitions** — one replica that can reach the store but not the broker, or a
  link that drops packets rather than refusing them — are not automated. Losing the datastore and
  the broker together, and recovering them one at a time, is (in the backbone drill).
- **Losing a node under the control plane** is not automated — only under the serving plane. The
  control plane's own cluster drill kills pods, not nodes.
- **The dashboard, MLflow and Prefect** have no drills of their own; they appear only as
  dependencies of the drills above.

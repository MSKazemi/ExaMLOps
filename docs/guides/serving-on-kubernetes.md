# Running the model server on Kubernetes

The Helm chart owns the control plane, the dashboard, the agent and the serving gateway. The model
server is deliberately not one of them: sites run it next to their accelerators, under a Ray
operator, a KServe controller or a Deployment of their own, and the chart points at it by name
(`gateway.upstream.host`, `agent.serviceUrls.rayServe`).

This page is about that last case — the model server as a plain Deployment — and about the one
question a plain Deployment raises: **what does churn cost the callers?** Every number here is
measured, by two drills that do these things to a real model server in a real cluster while eight
callers keep asking for predictions: `tests/integration/test_serving_kind_drill_live.py` takes pods
away, and `tests/integration/test_serving_node_loss_kind_live.py` takes a whole node away. Run them
yourself with the commands at the end.

Where a number varies between runs, this page says so rather than quoting the prettiest one.

## The Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ray-serving
spec:
  replicas: 2                       # a second pod is what makes any of this survivable
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0             # add the new pod before taking the old one away
  minReadySeconds: 5
  selector:
    matchLabels: {app: ray-serving}
  template:
    metadata:
      labels: {app: ray-serving}
    spec:
      terminationGracePeriodSeconds: 40   # longer than your slowest inference
      tolerations:                        # gets a replacement scheduled sooner after a node is lost
        - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 20}
        - {key: node.kubernetes.io/not-ready,  operator: Exists, effect: NoExecute, tolerationSeconds: 20}
      topologySpreadConstraints:          # see "the spread constraint is a decision" below
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector: {matchLabels: {app: ray-serving}}
      containers:
        - name: ray-serving
          image: ghcr.io/mskazemi/examlops-ray-serving:<tag>
          ports: [{containerPort: 8001, name: http}]
          env:
            - {name: MLFLOW_TRACKING_URI, value: http://mlflow:5000}
            - {name: RAY_PRELOAD_ALIASES, value: Production}
          readinessProbe:                  # the model load takes tens of seconds
            httpGet: {path: /ready, port: 8001}
            initialDelaySeconds: 5
            periodSeconds: 2
            failureThreshold: 90
          lifecycle:
            preStop:
              exec: {command: ["sleep", "8"]}   # leave the endpoints before stopping
          resources:
            requests: {cpu: 300m, memory: 1500Mi}
          volumeMounts: [{name: shm, mountPath: /dev/shm}]
      volumes:
        - name: shm
          emptyDir: {medium: Memory, sizeLimit: 1Gi}
```

A `Service` on port 8001 in front, and `/dev/shm` as a memory-backed volume because Ray needs
shared memory that the container default (64 MiB) does not provide. Add a `PodDisruptionBudget`
with `minAvailable: 1` so a node drain cannot take every replica at once.

## What each setting is worth, measured

| Setting | What it prevents | Measured |
|---|---|---|
| `readinessProbe` on `/ready` | A pod joining the Service before its model is loaded, answering requests it cannot serve | Without it, a scale-up during load cost **2069 of 20 790** requests. With it, **0 of 20 732** |
| `preStop` pause + the probe | New work being routed to a pod that is shutting down | A deletion cost a connection-per-request caller **0 of 14 745** |
| `maxUnavailable: 0`, `maxSurge: 1` | Capacity dipping during an upgrade | A rolling restart under load kept p99 at **38.8 ms** and cost 5 of 22 788 |
| `terminationGracePeriodSeconds` above your slowest request | In-flight work being killed at the grace deadline | — |
| `tolerationSeconds` on `unreachable` | A replacement waiting five minutes to be scheduled after a node is lost | It does **not** shorten the traffic loss: 132-139 s either way, measured |
| `topologySpreadConstraints` | Both replicas sitting on one node, so one node is all your availability | A lost node leaves one ready endpoint, not zero |

The readiness probe is the one that matters most, and it is the one people leave out: a Deployment
without it is *reported* healthy the instant the container starts, minutes before it can answer.

## What churn still costs, and what to do about it

A Kubernetes `Service` balances **connections**, not requests. Taking a pod out of its endpoints
stops new connections from reaching it; a keep-alive connection a caller already holds stays pinned
to that pod until something closes it. The model server shuts Ray Serve down as soon as it is asked
to stop, so that something is the shutdown, and whatever those connections were carrying at that
instant is lost.

| What happened to the pod | What a caller holding connections saw | What a caller that retries once saw |
|---|---|---|
| Deleted gracefully (eviction, drain, scale-down) | 6 of 17 321 cut, all in the same instant | 0 |
| Rolling upgrade (both pods replaced) | 5 of 22 788 | 0 |
| Killed outright (node lost, OOM, hard crash) | 6 of 15 724, none later than the moment of the kill | 0 |

So: **an inference client must retry an idempotent request once.** The platform's own front door
already does — the [serving gateway](serving-gateway.md) routes inference with
`retry_on: connect-failure,refused-stream,reset,retriable-status-codes` and two attempts — which is
the main reason to put it in front of the model server rather than exposing the Service directly.
A caller that opens a connection per request never sees any of this either, at the cost of a
handshake per prediction.

What this is *not*: a reason to drop the `preStop` pause or the readiness probe. They are what keep
the loss at "the connections already pinned to one pod" instead of "every request routed to a pod
that is starting or stopping" — the difference between 6 requests and 2069.

## When a whole node goes away

A node leaving is not a bigger version of a pod leaving. The two paths behave nothing alike, and
only one of them is safe by default.

**A planned drain** — `kubectl drain`, a node upgrade, a scale-down of the node pool — *evicts* the
pods. Every eviction is a graceful deletion, so it costs exactly what the table above says: the
keep-alive connections pinned to that pod, and nothing else. Give the deployment a
`PodDisruptionBudget` with `minAvailable: 1` so a drain cannot take every replica at once, and this
path needs no further thought.

**A node lost outright** — power, kernel, a cut cable — is the one to plan for. Nothing tells the
cluster; it is inferred from missing heartbeats. Until then **the pod stays `Ready` in the API**,
because the kubelet that would say otherwise went with the node, so the Service keeps routing its
share of inference to an address that answers nothing. Those requests do not necessarily fail fast:
they may *hang* until the caller's own timeout.

One thing that will not happen, and that surprises people mid-incident: **the pod is never deleted.**
Kubernetes marks it for deletion, but the kubelet that would confirm it is gone with the node, so the
pod sits in `Terminating` until the node comes back — the drill waited ten minutes and it was still
there. A replacement is created regardless, and the traffic stops as soon as the EndpointSlice drops
the old pod. Do not wait for the object to disappear, and do not force-delete it to "fix" the
outage; force-deletion is for StatefulSet identity, and it does not make the Service recover sooner.

Measured in the drill, with two replicas on two workers and one worker stopped:

| | With Kubernetes' defaults |
|---|---|
| When the first request failed | the instant the node stopped |
| What it cost | between **121 and 2151** requests, run to run — 0.9% to 43% of everything sent in the window |
| How a failing request failed | either at once (p99 154 ms) or by **hanging** for the caller's whole timeout (p99 10 s), depending on whether the dead node's packets are refused or silently dropped. Every failure was a transport error; the model server itself never answered one |
| How long failures kept appearing | past two minutes, in every run — the dead pod left the Service **132-139 s** after the node stopped, every time |
| What ended it | the EndpointSlice dropping the pod, so the Service stopped choosing it — nobody intervened |
| The surviving replica | answered everything routed to it throughout |
| Once the lost pod was gone | 0 of 11 643 at p99 118 ms, with the node still down |

The spread in that first row is the honest headline: **how much a lost node costs is not a stable
number**, and any single measurement of it — including a vendor's — says little. What *is* stable is
the shape: it starts immediately, it lasts about two minutes, it ends when the EndpointSlice drops the
dead pod, and nothing the platform itself does makes it better or worse.

So the platform does not go down — it black-holes a *fraction* of inference for about two minutes.
Three things bear on that. The first is the one to *stop* believing in; the other two are what
actually help:

1. **Do not expect `tolerationSeconds` to fix it** — this is the advice everyone reaches for, and the
   drill says it does not work. Kubernetes gives every pod `node.kubernetes.io/unreachable:NoExecute`
   with `tolerationSeconds: 300`, and it is natural to read that as "how long the dead pod keeps
   traffic". Across five measurements on three clusters, the pod left the Service after
   **132-139 s** whichever toleration was set (**137.1 s** in the most recent run), and the
   requests lost were **110 with the default against 117 with `tolerationSeconds: 20`** — the short
   toleration lost *more*. The difference is noise in both directions, which is the point: there is
   no effect to measure. What the toleration governs is when the pod is *marked for deletion*, and
   so when a replacement is scheduled; what stops the traffic is the EndpointSlice dropping the pod
   once the node is unreachable, on its own schedule.

   This is worth stating plainly because the *first* run of this drill suggested a seventeen-fold
   improvement from a short toleration, and that claim reached three pages before two further runs
   showed it was noise. Five runs later there is still no effect. One run of a timing measurement
   is an anecdote.

    ```yaml
    tolerations:   # worth setting — for a faster replacement, not for a shorter black hole
      - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 20}
      - {key: node.kubernetes.io/not-ready,  operator: Exists, effect: NoExecute, tolerationSeconds: 20}
    ```

    Do not take it to zero: a brief control-plane hiccup would then evict healthy pods.

2. **Let the proxy eject the address, not the cluster.** A client-side retry is *not* enough here —
   the retry is routed by the same Service, so it can land on the dead address again, after the
   caller has already waited out one timeout. What works is a proxy that notices and stops choosing
   that endpoint, and the [serving gateway](serving-gateway.md#when-a-model-server-stops-answering)
   now does: it probes `/ready` every 5 s and ejects an endpoint after two failures — about **10 s**,
   against Kubernetes' two minutes.

   **It can only do that per pod if it sees pods.** Point `gateway.upstream.host` at a **headless**
   Service (`clusterIP: None`) so Envoy resolves one endpoint per pod. With an ordinary ClusterIP it
   resolves one address, `kube-proxy` picks the pod, and there is nothing for the proxy to eject.

   Measured end to end, in the same cluster and the same node stop, with the real gateway reading
   the committed configuration:

   | Caller | What a lost node cost, over two runs |
   |---|---|
   | Straight at the Service | failures for **118-127 s** |
   | Behind the gateway, headless Service | **5-10 requests** of ~32 000, inside **0.1-10 s** — clean `503`s, p99 143 ms |

   That is the whole argument for the gateway in one row: the same failure, on the same cluster,
   goes from a two-minute partial black hole to seconds. The upper end of that window is the health
   check's own detection time — two probes, five seconds apart — so it is what you should expect,
   and the tenth of a second in the other run was luck about when the probe landed.

3. **Keep capacity for one node's worth of loss.** Two replicas on two nodes means losing a node
   halves capacity — fine if the survivor has headroom, an outage if it does not.

### The spread constraint is a decision, not a default

`topologySpreadConstraints` keeps replicas off the same node, and `whenUnsatisfiable` is the whole
decision:

- `ScheduleAnyway` treats the spread as a preference: when a node is lost, the replacement lands on
  the surviving node, so capacity comes back before the node does;
- `DoNotSchedule` makes it a rule: on a cluster with as many nodes as replicas there is nowhere left
  that satisfies it, so the replacement stays `Pending` and the deployment serves at reduced
  capacity until a node returns. The drill measures exactly this — one `Pending` pod and one ready
  endpoint instead of two.

Neither is wrong. Choose `ScheduleAnyway` when capacity matters more than strict placement, which is
the usual answer for inference; choose `DoNotSchedule` when two replicas on one node would be
pointless to you (a single accelerator per node, say) and you would rather run short.

**And know what `ScheduleAnyway` costs you afterwards: Kubernetes does not rebalance.** A spread
constraint is applied when a pod is *scheduled* and never again. Drain a node and its replica moves
to the survivor; when the node comes back, nothing moves it home. The drill hit exactly this — after
one drain, both replicas sat on one worker and stayed there, with every health check green — and a
cluster in that state is not highly available at all: the next node loss is a total outage rather
than half the capacity. It has to be noticed and corrected deliberately:

- alert on it — the spread is visible from `kubectl get pods -o wide`, or as a count of distinct
  nodes per deployment;
- correcting it is less simple than it sounds: `ScheduleAnyway` is a *score*, not a rule, and it can
  lose to the scheduler's other scores — three rollout restarts in a row put both replicas back on
  the same worker in the drill. What works deterministically is to cordon the crowded node while you
  delete one of its pods, so the scheduler has exactly one place to put the replacement (or run a
  descheduler, which does this kind of thing for you);
- or take `DoNotSchedule`, which cannot get into this state, at the price of a `Pending` replacement
  while a node is away.

The drill re-spreads deliberately before every node-loss measurement, and asserts that two nodes are
really serving before it stops one, precisely because a drill that measures an unbalanced cluster
measures nothing.

## Capacity, scaling and time to ready

A new replica is not useful the moment it exists. In the drill a pod took **33 s** from
`kubectl scale` to serving traffic, nearly all of it loading the model. Plan for that:

- an HPA that reacts in seconds will over-provision, because the pods it adds cannot help for half
  a minute — scale on a slow signal, with a generous stabilization window;
- keep enough headroom that losing one replica does not push the rest past what they can finish
  ([overload, deadlines and load shedding](../components/ray-serve.md#overload-deadlines-and-load-shedding));
- a `PodDisruptionBudget` with `minAvailable: 1` stops a node drain from taking every replica at
  once.

## Related

- [Ray Serve — the model server](../components/ray-serve.md) — what it serves and how it reloads
- [Serving gateway](serving-gateway.md) — the front door, its retries and its quotas
- [Game days](game-days.md#the-serving-plane-loses-pods-on-kubernetes) — how to run this drill, and
  the rest of them

```bash
docker build -f serving/ray_serving/Dockerfile -t exa-chaos/ray-serving:tree .
EXAMLOPS_KIND_SERVING_LIVE=1 .venv/bin/pytest tests/integration/test_serving_kind_drill_live.py -v -s
EXAMLOPS_KIND_NODE_LOSS_LIVE=1 .venv/bin/pytest tests/integration/test_serving_node_loss_kind_live.py -v -s
```

`make chaos-drills-kind` runs both, plus the control plane's own cluster drill.

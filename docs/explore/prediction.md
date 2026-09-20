---
title: Follow a prediction
description: How one inference request travels through ExaMLOps — bus, bridge, ingress, feature transformer, model router, Ray Serve — and what is recorded on the way.
hide:
  - navigation
  - toc
---

# Follow a prediction

A site service needs a prediction from one model. This is the **data line**: the request
crosses the message bus, is validated and routed inside the inference pipeline, is answered by
a model alias on Ray Serve, and comes back the same way. The bridge then records the
inference, and that record is what drift detection reads later.

<div class="xm-player" data-scene="prediction" markdown>
<ol class="xm-steps">
<li data-focus="client,bus" data-run="client-bus" data-actor="Client" data-line="data"><strong>A client sends a job to one model's address.</strong> Every model has its own UUID on the bus, set as <code>dataplane_bus_uuid</code> in its YAML. In request/reply mode — the one the stack runs — the bus routes by that UUID, not by the model name.</li>
<li data-focus="bus,bridge" data-run="bus-bridge" data-actor="Bridge" data-line="data"><strong>The bridge picks the handler for that model.</strong> At start-up it registered one handler per model UUID. The handler fixes the model, picks the alias — Production unless the job asks for another — and builds the features from the model's input schema.</li>
<li data-focus="bridge,ingress" data-run="bridge-ingress" data-actor="Bridge" data-line="data"><strong>The job enters the inference pipeline.</strong> The bridge posts it to <code>/infer-pipeline/infer</code> on Ray Serve over one shared HTTP client: 5 s to connect, 10 s to read, no retry at this hop.</li>
<li data-focus="ingress,transformer" data-run="ingress-transformer" data-actor="Ingress" data-line="data"><strong>The ingress validates the payload.</strong> An embedding and a node count are required. A malformed request gets a 422 here — a schema error, which is not treated as a model failure.</li>
<li data-focus="transformer,router" data-run="transformer-router" data-actor="Feature transformer" data-line="data"><strong>The feature transformer checks the embedding.</strong> It batches up to 32 requests or 50 ms to validate them together, confirms each embedding has 384 values, and keeps job and user IDs as metadata. After validation, every request is routed on its own.</li>
<li data-focus="router,rules" data-run="rules-router" data-actor="Model router" data-line="data"><strong>The model router applies the traffic split.</strong> It takes the model's Production / Canary weights from the serving snapshot, re-read every 2 seconds, or, without a snapshot, from the traffic rules cached for 30 seconds. The split applies to traffic addressed to the model's default alias (Production), which is what bus jobs send: when a split names two or more aliases, the router picks one by weighted random choice. A request pinned to any other alias, such as <code>Staging</code>, gets that alias. Rules are matched case-insensitively, so a split set for <code>JPCP</code> applies to <code>jpcp</code>.</li>
<li data-focus="router,ray,mlflow" data-run="router-ray" data-actor="Ray Serve" data-line="data"><strong>Ray Serve answers from its hot set.</strong> The alias resolves to a model already in memory, loaded from MLflow at start-up. Prediction runs on a pool of 4 workers with a 30 second limit.</li>
<li data-focus="client,bus,bridge,ingress,transformer,router,ray" data-run="~router-ray;~transformer-router;~ingress-transformer;~bridge-ingress;~bus-bridge;~client-bus" data-actor="Reply" data-line="data"><strong>The answer returns the same way.</strong> Model name, alias, version, MLflow run and prediction travel back to the client. On any error the reply carries an error message and a prediction of 0.0, so clients must check the message.</li>
<li data-focus="bridge,db" data-run="bridge-db" data-actor="Bridge" data-line="observe"><strong>The bridge records the inference.</strong> Before it replies it puts a drift snapshot of the prediction and the embedding's norm, mean and standard deviation on a bounded queue, and a background worker writes them. The reply never waits on the store, so a slow or unavailable store never delays or fails the inference; records the queue cannot hold are dropped and counted.</li>
<li data-focus="bridge,cp" data-run="bridge-cp" data-actor="Bridge" data-line="control"><strong>Repeated failures ask for a retrain.</strong> The bridge tracks each model's failure rate over its last 50 requests, counting only failures the pipeline attributes to the model (<code>cause: model</code>): an outage or a lost replica is not a worse model. At 0.5 or above it submits a retrain to the control plane (<code>POST /v1/retrain</code>), then waits out a 300 second cooldown.</li>
<li data-focus="ray,prom,mlflow" data-run="ray-prom,mlflow-ray" data-actor="Ray Serve" data-line="observe"><strong>Metrics and reloads run alongside.</strong> Ray Serve exports request counts and latency by model and alias. When an alias moves it reloads that model without dropping traffic: it learns of the move from the control plane's serving snapshot within 2 seconds, or, without a snapshot, by asking MLflow every 60 seconds.</li>
<li data-focus="direct,ray" data-run="direct-ray" data-actor="Direct client" data-line="data"><strong>Direct HTTP calls skip the bus.</strong> <code>exa predict</code>, the dashboard and the agent call Ray Serve's API on port 18001. They get the same models, but their requests write no drift snapshots — drift detection sees bus traffic only.</li>
</ol>
</div>

## Limits and timeouts on the way

| Hop | Limit | Where it is set |
|---|---|---|
| Bridge → pipeline | 5 s connect, 10 s read, no retry | `EXAMLOPS_HTTP_*` |
| Feature transformer | batches of up to 32 requests or 50 ms; embedding of exactly 384 values | inference pipeline |
| Model router | split from the serving snapshot (every 2 s), or traffic rules cached 30 s without one; a transport error or a 503 retried up to twice and a lost replica once, within the request's deadline and the retry budget | `RAY_SNAPSHOT_POLL_SECONDS`, `TRAFFIC_RULES_TTL_SECONDS`, `INFERENCE_ROUTE_RETRIES`, `INFERENCE_RETRY_MAX_TOKENS` |
| Ray Serve predict | 4 workers, 30 s hard limit (504 on timeout) | `RAY_PREDICT_WORKERS`, `RAY_PREDICT_TIMEOUT` |
| Pinned versions | the 8 most recent raw versions stay loaded | `RAY_VERSION_CACHE_SIZE` |
| Alias reload | from the serving snapshot within 2 s; with `RAY_SNAPSHOT_MODE=off`, MLflow polled every 60 s (`0` turns that off) | `RAY_SNAPSHOT_MODE`, `RAY_RELOAD_POLL_SECONDS` |
| Bridge retrain trigger | failure rate ≥ 0.5 over 50 requests, 300 s cooldown | bridge |

## What errors mean

| Reply | Meaning | Counts toward the retrain trigger? |
|---|---|---|
| 422 validation error | The payload does not match the model's schema | No |
| 5xx `inference_failed` with `cause: model`, or an empty prediction | The model failed while predicting | Yes |
| 5xx `inference_failed` with another `cause` (`transport`, `replica_lost`, `timeout`, `protocol`, `pipeline`) | The model server could not be reached, lost the request, or ran out of time: not the model's fault | No |
| The bridge could not reach the inference pipeline | A transport error before any reply | No |

## Try it

```bash
exa serve check                                   # Ray Serve health and the loaded hot set
exa serve infer-check                             # one end-to-end request through the pipeline
exa serve traffic jpcp --production 90 --canary 10
exa drift status                                  # prediction drift, from the bridge's snapshots
exa drift input status                            # input-embedding drift
```

## Read more

- [Interfaces](../guides/interfaces.md) — bus, HTTP, CLI and dashboard entry points
- [Ray Serve](../components/ray-serve.md) — hot set, aliases, reload and shadow traffic
- [Dataplane bus bridge](../guides/dataplane-bus.md) and [bus architecture](../guides/dataplane-bus-architecture.md)
- [Follow a retrain](retrain.md) — what happens after drift or failures are detected

---
title: Follow a retrain
description: Every way a retrain starts in ExaMLOps, the control plane's checks, the training flow stage by stage, promotion, reload, and the loop back to drift.
hide:
  - navigation
  - toc
---

# Follow a retrain

This is the **control line**: how a model gets retrained and back into service. Eight different
things can start a retrain. Most reach the control plane's retrain API; model changes from CI
wait for a person; the model library can start a run directly. From there one training flow
takes over: check the data, train on a scheduler, evaluate, register, promote, reload — and the
newly served model feeds drift detection again.

<div class="xm-player" data-scene="retrain" markdown>
<ol class="xm-steps">
<li data-focus="t-drift,t-auto,t-bridge,t-op,t-bus,t-agent,t-ci,t-zoo" data-actor="Triggers" data-line="control"><strong>Eight things can start a retrain.</strong> Three are automatic (drift trigger, autopilot, bridge error tracker), three come from people or agents (<code>exa retrain</code>, Skipper and MCP, a bus request from a site component), and two come from code changes (CI and the model library).</li>
<li data-focus="t-drift,cp" data-run="drift-cp" data-actor="Drift trigger" data-line="control"><strong>Drift crosses a threshold.</strong> <code>exa drift trigger</code> checks every model with auto-retrain enabled: prediction-drift z at or above its threshold (default 3.0), or a critical concept-drift event, outside its cooldown (default an hour). A corruption classifier can veto the retrain, because corrupted data is not drift, and a prediction-drift retrain with no declared rollback is refused. Nothing in the platform schedules this command — run it yourself or from your own scheduler.</li>
<li data-focus="t-auto,cp" data-run="auto-cp" data-actor="Autopilot" data-line="control"><strong>Or the autopilot runs a cycle.</strong> Each <code>exa autopilot run</code> — from your own scheduler — runs one cycle, and acts only when the kill-switch is on. It scans drift, applies policy rules and autonomy levels, takes a lease so only one cycle runs at a time, and caps how many retrains one cycle may fire.</li>
<li data-focus="t-bridge,cp" data-run="bridge-cp" data-actor="Bridge" data-line="control"><strong>Or a model keeps failing.</strong> The bus bridge asks for a retrain when half of a model's last 50 inferences failed, then waits 300 seconds before it can ask again.</li>
<li data-focus="t-op,t-bus,t-agent,cp" data-run="op-cp,bus-cp,agent-cp" data-actor="People and agents" data-line="human"><strong>Or someone asks.</strong> An operator runs <code>exa retrain JPCP --dataset PM100Dataset</code> (preview with <code>--dry-run</code>, confirm, audited); a site component sends a retrain request over the bus; or Skipper calls the retrain tool after the user confirms.</li>
<li data-focus="cp,prefect" data-run="cp-prefect" data-actor="Control plane" data-line="control"><strong>The control plane checks the request.</strong> Token with write scope, 20 writes a minute per tenant, a known model and dataset, no duplicate already in flight, a consistent idempotency key, and room under the admission cap (4 dispatches in flight, 2 per tenant — over it, the request gets 429 with <code>Retry-After</code>; retry with the same idempotency key). Then it creates a flow run of the <code>training_flow/examlops-dispatch</code> deployment, one model × dataset per run, behind a circuit breaker. That deployment only executes while <code>exa pipeline deploy</code> is serving it.</li>
<li data-focus="t-ci,gate,prefect" data-run="ci-gate;gate-prefect" data-actor="Operator" data-line="human"><strong>A model change waits for approval.</strong> CI files a pending approval for each changed model. Only when an operator approves does the control plane start the flow; unanswered approvals expire after 72 hours.</li>
<li data-focus="t-zoo,prefect" data-run="zoo-prefect" data-actor="Model library" data-line="control"><strong>The model library can start a run directly.</strong> A webhook or the 5-minute poller marks models stale after an upstream push. If ModelZoo auto-retrain is switched on (<code>MODELZOO_AUTO_RETRAIN=true</code>, off by default), every registered model is retrained on its first dataset, with a durable command key, subject to the same admission cap.</li>
<li data-focus="prefect,s-data" data-run="prefect-data" data-actor="Training flow" data-line="control"><strong>The flow loads and checks the data.</strong> It reads the dataset from the model's backend — Zenodo, MinIO or a data plane — and validates it against the dataset's data contract, if one exists. In enforce mode (the default) an error-severity violation stops the run before training; in warn mode it continues, and dummy runs skip the gate.</li>
<li data-focus="s-data,s-train,cluster" data-run="data-train;train-cluster" data-actor="Scheduler adapter" data-line="hpc"><strong>It trains on the chosen scheduler.</strong> In mock mode the model trains inline; with Slurm or Flux a batch job is submitted, polled every 10 seconds and recorded with its GPU count and timings.</li>
<li data-focus="s-train,s-eval,mlflow" data-run="train-eval;eval-mlflow" data-actor="Training flow" data-line="control"><strong>It evaluates and registers a new version.</strong> RMSE and MAPE for regression, accuracy and F1 for classification. Parameters, metrics and the HPC job id go to MLflow, and the model is registered as a new version with no alias yet.</li>
<li data-focus="mlflow,s-promote" data-run="mlflow-promote" data-actor="Training flow" data-line="control"><strong>Lifecycle rules promote it automatically.</strong> The flow walks the model's rules and sets every alias the new version qualifies for in one pass — for JPCP, Staging at RMSE ≤ 200, Canary ≤ 100, Production ≤ 50 — and archives the previous Production version.</li>
<li data-focus="s-promote,ray" data-run="promote-ray" data-actor="Ray Serve" data-line="control"><strong>Ray Serve reloads the model.</strong> When the Production alias moves, the flow asks Ray Serve to reload at once (when <code>RAY_SERVE_URL</code> is set, as in the compose stack); Staging and Canary changes arrive with the next 60-second alias poll. If MLflow is unreachable, Ray Serve keeps serving the last good version.</li>
<li data-focus="operator,op-gate,mlflow" data-run="operator-gate;gate-mlflow" data-actor="Operator" data-line="human"><strong>Operators can promote with stricter gates.</strong> <code>exa pipeline validate-model JPCP</code> smoke-tests latency; <code>exa pipeline promote jpcp --if-rmse-lt 5.0</code> checks the metric condition first (<code>--dry-run</code> stops there), then runs the evaluation, judge-calibration, portability, SLO, compliance, fairness and synthetic-only gates before it moves an alias. <code>--force</code> overrides a failing gate and is recorded.</li>
<li data-focus="ray,t-drift" data-run="ray-drift" data-actor="Loop" data-line="observe"><strong>The loop closes.</strong> The newly served version answers requests, the bridge records them, and drift is measured against the baseline again — ready for the next trigger.</li>
</ol>
</div>

## The eight triggers

| Trigger | Starts how | Reaches | Who is in the loop |
|---|---|---|---|
| Drift trigger | `exa drift trigger` — prediction-drift z ≥ threshold (with a declared rollback) or a critical concept-drift event, outside cooldown, no corruption veto | Retrain API | Autonomous once enabled per model |
| Autopilot | `exa autopilot run` (one cycle per run) — kill-switch on, policy and autonomy allow it | Retrain API | Autonomous or REVIEW, per behaviour |
| Bridge error tracker | Failure rate ≥ 0.5 over 50 inferences, 300 s cooldown | Retrain API | Autonomous |
| Operator | `exa retrain MODEL --dataset DATASET` | Retrain API | A person, with confirmation |
| Bus request | A site component sends `RetrainReqV1` | Retrain API | The requesting system |
| Skipper or MCP | Retrain tool after confirmation; MCP only with writes allowed | Retrain API | A person confirms |
| CI model change | `POST /api/changes` from the pipeline | Approval queue | A person approves |
| Model library | Webhook or poller, when `MODELZOO_AUTO_RETRAIN=true` (off by default) | Training flow directly | Autonomous |

!!! note "Where the approval queue applies"
    The approval queue guards model changes from CI. Drift-triggered retrains are governed by
    thresholds, cooldowns, the corruption veto, rate limits, admission and — for the
    autopilot — autonomy levels, but not by the queue. Routing them through it is on the
    [roadmap](roadmap.md).

## The control plane's checks

| Check | Result when it fails |
|---|---|
| Bearer token with `write` scope (placeholder tokens fail closed) | 401 / 403, or 503 if no token is configured |
| 20 writes per minute per tenant, shared by retrain and approval endpoints | 429 with `Retry-After` |
| Model and dataset are registered and enabled | 400, listing the known values |
| The same model and dataset are not already being dispatched | 409 |
| Idempotency key reused with a different payload | 409 |
| Admission cap: 4 dispatches in flight globally, 2 per tenant | 429 with `Retry-After`; retry with the same idempotency key |
| Every extra `parameters` key is accepted by the dispatch deployment's flow (checked when its schema is known) | 400, listing the accepted keys |
| The dispatch deployment `training_flow/examlops-dispatch` exists (`exa pipeline deploy` registers and serves it) | 503 naming the missing deployment; `GET /health` → `dispatch` says the same before anyone retrains |
| Prefect reachable (3 attempts, circuit breaker opens after 5 failures) | 502 after 3 failed attempts; 503 while the breaker is open |

## Try it

```bash
exa retrain JPCP --dataset PM100Dataset --dry-run      # preview; nothing is sent
exa drift auto-retrain enable JPCP --dataset PM100Dataset
exa drift trigger --dry-run                            # which models would retrain now
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
exa pipeline validate-model JPCP --max-latency 0.5
exa pipeline promote jpcp --if-rmse-lt 5.0 --dry-run
```

## Read more

- [Control plane](../components/control-plane.md) and the [retraining API guide](../guides/control-plane.md)
- [Prefect pipelines](../components/prefect.md) and the [HPC training workflow](../guides/hpc-training-workflow.md)
- [Advanced drift](../guides/drift-advanced.md) and [corruption detection](../guides/corruption-detection.md)
- [Evaluation and regression gates](../guides/evaluation.md) and [judge calibration](../guides/judge-calibration.md)
- [Who decides](decisions.md) — every human gate in one place

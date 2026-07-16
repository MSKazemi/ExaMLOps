# Tutorial — A Tour of Next-Gen 40

This tutorial is a hands-on walkthrough of the **Next-Gen 40** capability set: 40 features
that turn ExaMLOps into a self-driving, governed, vendor-neutral HPC-MLOps platform. Every
command below runs against your local `platform.db` with **no external service required** —
each feature degrades gracefully to a pure-Python path, so you can follow along offline and
wire in the real backends (lakeFS, Feast, vLLM, OPA, Flower, …) later.

> Each section links to its in-depth guide. For the full command surface see the
> [CLI reference](../reference/cli-generated.md); for the design rationale see the ADRs.

## How the 40 features group

| Track | Theme | Example features |
|---|---|---|
| **A** | Data & reproducibility | dataset versioning, lineage, contracts, feature store, asset pipelines, reproducibility bundles |
| **B** | LLMOps | prompt management, API gateway, semantic cache, RAG, vector store, embedding lifecycle, fine-tuning, structured output |
| **C** | Observability & evaluation | GenAI telemetry, continuous eval, regression gates, AgentOps, advanced drift, SLOs, champion-challenger, fairness |
| **D** | Governance & security | secrets, RBAC/tenancy, supply-chain signing, immutable audit, EU-AI-Act, NIST-RMF, policy-as-code, guardrails |
| **E** | Serving & training | K8s serving, optimized engines, fractional GPUs, inference gateway, autoscaling, distributed training, federated training, heterogeneous hardware |

You don't need to run them in order. Below is a narrative path an operator might actually take.

---

## 1. Version the data before you train (A1)

Reproducibility starts at the data. Record an immutable revision, then pin a training run to it:

```bash
exa data snapshot FData --backend minio --path ./data/FData
exa data list FData
exa pipeline run --model JPCP --dataset FData --dataset-revision <rev>
```

→ Guide: [Data versioning](../guides/lineage.md) · [reproducibility bundles](../guides/reproducibility-bundles.md)

## 2. Keep train/serve features consistent (A3)

Define a feature once and read it the same way at training and serving time — no skew:

```bash
exa feature apply user_activity --entity user --ttl 3600
exa feature get user_activity           # read the online value (same definition as training)
exa feature skew user_activity          # measures train/serve skew (target: zero)
```

→ Guide: [Feature store](../guides/feature-store.md)

## 3. Rebuild only what went stale (A4)

Model your pipeline as **assets** with a freshness DAG; materialize only stale ancestors:

```bash
exa assets status
exa assets materialize model_scores     # rebuilds upstream only where stale
```

→ Guide: [Asset-centric pipelines](../guides/asset-pipelines.md)

## 4. Serve efficiently and route smartly (E2/E4/E5)

Pick an optimized engine, route LLM requests to the replica that already holds their KV/prefix
cache, and scale to zero when idle:

```bash
exa serve routing set JPCP --mode cache_aware --slo-latency-ms 500
exa serve routing simulate JPCP --replicas 4 --shared-prefix-requests 100
exa serve autoscale set JPCP --min 0 --max 8       # scale-to-zero when idle
```

→ Guides: [Serving engines](../guides/llm-serving-engines.md) · [Inference gateway](../guides/inference-gateway.md) · [Autoscaling](../guides/autoscaling.md)

## 5. Watch quality, not just uptime (C1/C2/C6)

Emit GenAI telemetry, run evaluation suites with a regression gate, and set model-quality SLOs
with burn-rate alerts:

```bash
exa eval run my-suite --model JPCP
exa eval gate run JPCP <candidate>        # exit 1 on regression → CI gate
exa slo set JPCP accuracy --target 0.9         # MODEL NAME + target
```

→ Guides: [Evaluation](../guides/evaluation.md) · [SLOs](../guides/slos.md) · [AgentOps](../guides/agentops.md)

## 6. Govern every change (D3/D4/D5)

Sign artifacts, chain an immutable audit log, and enforce policy-as-code before promotion:

```bash
exa policy eval promote --action promote --resource JPCP   # policy decision (fail-closed)
exa audit --last 7d --model JPCP              # tamper-evident, hash-chained
```

→ Guides: [Supply-chain security](../guides/supply-chain-security.md) · [Immutable audit](../guides/audit-trail.md) · [Policy-as-code](../guides/policy-as-code.md)

## 7. Train at scale — distributed, federated, anywhere (E6/E7/E8)

Launch fault-tolerant distributed training with checkpoint/resume; train across sites that
can't share data; and place work on the best-available accelerator across HPC and cloud:

```bash
# Distributed with checkpoint/resume (E6)
exa pipeline distributed launch JPCP --strategy fsdp --nodes 4

# Federated across sites, differential privacy on, secure aggregation on (E7)
exa federated init --site siteA --site siteB --dp --epsilon-per-round 0.5 --secure-agg
exa federated round fed-fedavg-2sites --update siteA:0.1,0.2:100 --update siteB:0.3,0.4:150
exa federated budget fed-fedavg-2sites

# Heterogeneous placement + governed cloud burst (E8)
exa hardware add-pool hpc-mi300 --target hpc --accelerator amd --count 8 --region eu
exa hardware place train-llm --accelerator amd --engine vllm --target hpc
exa hardware burst train-llm --residency eu-only --allow-burst   # blocked if egress forbidden
```

→ Guides: [Distributed training](../guides/distributed-training.md) · [Federated training](../guides/federated-training.md) · [Heterogeneous hardware](../guides/heterogeneous-hardware.md)

## 8. Close the loop — self-driving autopilot

With drift detection, policy gates, and metric-gated promotion in place, let the autopilot run
the whole detect→retrain→validate→promote cycle (kill-switch disabled by default):

```bash
exa autopilot enable
exa autopilot run --dry-run       # preview one cycle
exa autopilot run                 # drift scan → policy → retrain → promote
exa autopilot status
```

---

## Design principles you'll see everywhere

Every Next-Gen 40 feature follows the same rules, so once you know one you know them all:

- **Graceful degradation** — no feature requires an external service to *run*. The real backend
  (lakeFS, Feast, vLLM, OPA, Flower, Opacus, MIG) is an upgrade, not a prerequisite.
- **Honest fallback** — when a feature can't do the ideal thing (bit-exact repro, GPU fractioning
  on a non-MIG vendor, DP with the accountant off), it says so rather than pretending.
- **Fail-open vs fail-closed** — bookkeeping (lineage, telemetry) fails open; security (secrets,
  authz, supply-chain) and guardrails fail closed.
- **Additive & audited** — every mutating action writes a tamper-evident audit event; nothing is
  removed from the platform DB.

## Where to go next

- The [CLI reference](../reference/cli-generated.md) lists every command and flag.
- Each guide under **Next-Gen 40** in the sidebar goes deep on one feature.
- The ADRs (`design/adr/0003–0044`) record why each decision was made.

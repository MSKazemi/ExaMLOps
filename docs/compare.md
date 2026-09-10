---
title: ExaMLOps compared
description: How ExaMLOps compares with Kubeflow, MLflow on its own, Ray on Slurm, ClearML and Metaflow for MLOps on HPC clusters — what each covers, what runs on Slurm or Flux, and when to choose something else.
---

# ExaMLOps compared with other MLOps options for HPC

**Short answer:** many MLOps platforms assume Kubernetes or a cloud. ExaMLOps is built for the
case where models train on a **batch-scheduled HPC cluster** (Slurm or Flux), and it combines
tools you may already know — MLflow as the registry, Ray Serve for serving, Prefect for
pipelines — with the parts that are usually missing around them: job submission to the
scheduler, gated promotion, drift-triggered retraining, cost and carbon accounting, and an audit
trail. If your organisation is Kubernetes-native, or you only need experiment tracking, one of
the alternatives below is likely the better fit.

Claims about other projects are taken from their own documentation, checked on 2026-09-10 and
linked in [Sources](#sources). Projects change; if something here is out of date, please use the
edit button on this page.

## At a glance

| | Runs training on Slurm / Flux | Needs Kubernetes | Model registry | Gated promotion + serving | Notes |
|---|---|---|---|---|---|
| **ExaMLOps** | Yes — Slurm and Flux, local or over SSH, plus a `mock` mode | No (Docker Compose or Helm for the services) | MLflow | Metric and evaluation gates, approval queue for model changes, Ray Serve with traffic splits | Open source (Apache-2.0); young project, one maintainer |
| **Kubeflow** | Not its model — described as "the Kubernetes-native stack for data & AI workloads" | Yes | Kubeflow Hub (formerly Model Registry) | Pipelines, Trainer and other components on Kubernetes | Large community; the natural choice if you already run Kubernetes |
| **MLflow on its own** | Not one of the components it describes | No | Yes — "centralized model versioning, stage management, and model lineage tracking" | Deployment to REST APIs, cloud platforms and edge devices; you build the gates | ExaMLOps uses MLflow as its registry |
| **Ray on Slurm (do it yourself)** | Yes, with care — Ray's own docs call Slurm usage "a little bit unintuitive" | No | No (bring your own) | Ray Serve for serving; everything else is yours to build | ExaMLOps uses Ray Serve for serving |
| **ClearML** | Yes — the Slurm Glue maps ClearML queues to Slurm jobs | Not for the Slurm path | Not checked here — see ClearML's docs | `clearml-serving` for "model deployment and orchestration" | The Slurm Glue "is available under the ClearML Enterprise plan" |
| **Metaflow** | Via the `metaflow-slurm` extension (by Outerbounds; 0.0.4, Dec 2024) | Not for the Slurm path | Runs and results are tracked through its metadata service | Not checked here | The extension runs individual steps on Slurm over SSH |

"Not its model" and "not one of the components it describes" mean the project's own
documentation does not present that capability — not that it is impossible to build. "Not checked
here" means we did not verify it; the project may well offer it.

## When ExaMLOps is a good fit

- Your models train on an **HPC cluster run by Slurm or Flux**, and you want training runs,
  versions and serving to be tracked and governed rather than held together by batch scripts.
- You need **promotion to be gated and recorded** — metric conditions, evaluation gates, an
  approval queue for model changes, and a [hash-chained audit trail](guides/audit-trail.md) —
  see [Who decides](explore/decisions.md).
- A **cluster must be admitted** by an operator before any job can land on it
  ([HPC fleet](guides/hpc-fleet.md)).
- You want **cost and carbon accounting** per model and project, with formulas you can swap
  ([FinOps providers](guides/finops-providers.md)).
- You want AI agents to operate the platform through **MCP**, read-only unless you allow writes
  ([All interfaces](guides/interfaces.md)).

## When to choose something else

- **You already run Kubernetes for ML.** Kubeflow and KServe are built for that environment and
  have much larger communities. ExaMLOps can generate Kubernetes serving manifests, but its
  centre of gravity is the HPC scheduler.
- **You only need experiment tracking and a registry.** A plain MLflow server is simpler.
- **You want a packaged commercial product with Slurm integration.** ClearML's Enterprise plan
  includes a Slurm Glue. ExaMLOps is community software; for hands-on help with a deployment, the
  maintainer [takes engagements](https://mskazemi.com/hire/).
- **You need a mature project with many maintainers.** ExaMLOps is young and has one
  maintainer. Its reference deployment schedules on Flux; the Slurm path is covered by tests and
  `mock` mode and would benefit from validation at more Slurm sites —
  [reports are very welcome](community/index.md).

## How ExaMLOps relates to the tools it uses

ExaMLOps does not replace MLflow, Ray Serve or Prefect — it runs them and adds the platform
around them:

| Tool | Role in ExaMLOps |
|---|---|
| MLflow | Model registry and experiment tracking; the Staging / Canary / Production aliases |
| Ray Serve | Multi-model serving with traffic splits between versions |
| Prefect | Auto-discovery training pipelines and deployments |
| Slurm / Flux | Where training jobs run, through one scheduler abstraction |
| Prometheus, Grafana, Loki, Tempo | Metrics, dashboards, logs and traces |

The [system map](explore/index.md) shows how these pieces connect.

## Sources

- Kubeflow — [Introduction](https://www.kubeflow.org/docs/started/introduction/): "Kubeflow is composed of modular, open source projects that form the Kubernetes-native stack for data & AI workloads." · [Components](https://www.kubeflow.org/docs/components/): "Kubeflow Hub (formerly Model Registry)".
- MLflow — [Documentation](https://mlflow.org/docs/latest/ml/): tracking, model registry ("centralized model versioning, stage management, and model lineage tracking"), deployment and evaluation.
- Ray — [Deploying on Slurm](https://docs.ray.io/en/latest/cluster/vms/user-guides/community/slurm.html): "Slurm usage with Ray can be a little bit unintuitive."
- ClearML — [Slurm (Native)](https://clear.ml/docs/latest/docs/clearml_agent/clearml_agent_deployment_slurm/): "ClearML Agent can run tasks on Linux clusters managed by Slurm." · "Slurm Glue is available under the ClearML Enterprise plan." · [ClearML Serving](https://clear.ml/docs/latest/docs/clearml_serving/): "`clearml-serving` is a command line utility for model deployment and orchestration."
- Metaflow — [`metaflow-slurm` on PyPI](https://pypi.org/project/metaflow-slurm/): "Slurm extension for Metaflow", by Outerbounds, version 0.0.4 (2024-12-10) · [Client API](https://docs.metaflow.org/metaflow/client): "The Client API consults the metadata service to gather results".

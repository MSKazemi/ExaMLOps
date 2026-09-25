# examlops — the ExaMLOps operator CLI and SDK

**Register a model, and the platform trains, versions, governs and serves it on a supercomputer.**

`examlops` is the client side of [ExaMLOps](https://github.com/MSKazemi/ExaMLOps), an end-to-end
MLOps platform for HPC: training pipelines submitted to Slurm or Flux, an MLflow registry with a
multi-stage lifecycle, a sysadmin approval gate in front of production, Ray Serve multi-model
serving, drift detection with closed-loop retraining, and FinOps / carbon accounting per model.

This package installs the **`exa` command** and the **`examlops` Python SDK**. The platform
services themselves (control plane, dashboard, serving, agent) ship as signed container images
and a Helm chart — see [Installation](https://mskazemi.com/ExaMLOps/guides/enterprise-installation/).

## Install

```bash
pipx install examlops          # or: uv tool install examlops
exa --version
```

Python 3.12 or newer, Linux. Optional capabilities are extras, so the base install stays light
enough for an HPC login node:

| Extra | Enables |
|---|---|
| `examlops[mcp]` | `exa mcp serve` — platform capabilities as agent-callable MCP tools |
| `examlops[agentops]` | the upstream OTLP/HTTP exporter for Langfuse / Phoenix agent-trace export (a stdlib fallback does the same without it) |
| `examlops[analysis]` | `exa serve ab analyze` — Welch / z-test A/B statistics |
| `examlops[finops]` | declarative (sandboxed) cost and carbon formulas |
| `examlops[oidc]` | OIDC single sign-on token validation |
| `examlops[postgres]` | the Postgres datastore engine |
| `examlops[backup]` | object-store tier of whole-platform backup |
| `examlops[audit-sigstore]` | keyless Sigstore signing of audit checkpoints (ADR 0028) |
| `examlops[coordination]` | Redis cross-host locks and rate limits |
| `examlops[features-online]` | Redis online store for the feature store (ADR 0017) |
| `examlops[events]` | NATS JetStream event backbone (the default `log` publisher needs nothing) |
| `examlops[vector]` | pgvector vector store, independent of where platform state lives |
| `examlops[qdrant]` | Qdrant vector store — the scale-out alternative to pgvector, same `VectorStore` seam |
| `examlops[supplychain]` | Sigstore keyless model signing and keyless-signed SLSA provenance (Fulcio + Rekor transparency log), `EXAMLOPS_SIGNING_SCHEME=sigstore` — see `docs/guides/supply-chain-security.md` |
| `examlops[drift-advanced]` | River (ADWIN/DDM) and Evidently concept-drift detectors behind `exa drift concept` / `run-advanced` — each has a pure-Python fallback (NannyML and whylogs adapters need a separate environment, see `docs/guides/drift-advanced.md`) |
| `examlops[fairness]` | Fairlearn's `MetricFrame` for fairness slice metrics (a pure-Python fallback computes the same numbers without it) |
| `examlops[guardrails-presidio]` | Presidio NER (person/place PII detection) as a supplement to the built-in regex PII detectors — needs a separate one-time spaCy model install, see `docs/guides/guardrails.md` |
| `examlops[synth]` | synthetic dataset generation |
| `examlops[eval-metrics]` | rapidfuzz + rouge-score, the libraries Ragas's text metrics use, so `string_similarity` / stemmed `rouge_l` match Ragas's numbers (pure-Python fallbacks work without it) |
| `examlops[serving-vllm]` / `examlops[serving-sglang]` | in-process vLLM / SGLang engines (GPU host) |
| `examlops[finetune]` | `exa finetune --train` (torch) and its Hugging Face PEFT backend (`--backend peft`) |
| `examlops[dataplane]` | pull external data into versioned snapshots — the union of the four extras below |
| `examlops[dataplane-sql]` | the `sql` dataplane connector (any SQLAlchemy URL) |
| `examlops[dataplane-files]` | the `files`, `zenodo` and `rest` dataplane connectors |
| `examlops[dataplane-kafka]` | the `kafka` dataplane connector |
| `examlops[dataplane-service]` | run the dataplane HTTP service, not just pull from the CLI |
| `examlops[rag-llamaindex]` | LlamaIndex's sentence-aware chunker for `exa rag ingest --framework llamaindex` |
| `examlops[rag-eval]` | Ragas' non-LLM retrieval metrics behind `exa rag eval` (the built-in metrics give the same numbers without it). Cannot share an environment with `dataplane-files`/`dataplane` (ragas → datasets caps fsspec below the dataplane floor) |
| `examlops[rag-service]` | run the RAG serving endpoint (`uvicorn --factory examlops.rag.service:create_app`) |

## Point it at a platform

```bash
exa config set control_plane https://examlops.example.org:18002
exa status                       # service health, approvals, production models
exa models list
exa drift status
exa audit verify                 # recompute the tamper-evident audit hash chain
```

Every read command accepts `--output json|yaml|csv`, so the CLI is scriptable. The commands
that build and run a local development stack from source (`exa stack …`, `exa pipeline deploy`)
still expect a source checkout of the repository; everything that talks to a running platform
works from the installed package alone.

## Verify what you installed

Releases are published from GitHub Actions with PyPI Trusted Publishing, and every file carries a
Sigstore attestation (PEP 740) that ties it to the exact workflow run and commit that built it.
See [Verifying a release](https://mskazemi.com/ExaMLOps/guides/release-process/).

## Links

- Documentation: <https://mskazemi.com/ExaMLOps/>
- Source and issues: <https://github.com/MSKazemi/ExaMLOps>
- Changelog: <https://github.com/MSKazemi/ExaMLOps/blob/main/CHANGELOG.md>
- Security policy: <https://github.com/MSKazemi/ExaMLOps/security/policy>

Apache-2.0 © Mohsen Seyedkazemi Ardebili

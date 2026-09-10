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
| `examlops[analysis]` | `exa serve ab analyze` — Welch / z-test A/B statistics |
| `examlops[finops]` | declarative (sandboxed) cost and carbon formulas |
| `examlops[oidc]` | OIDC single sign-on token validation |
| `examlops[postgres]` | the Postgres datastore engine |
| `examlops[backup]` | object-store tier of whole-platform backup |
| `examlops[coordination]` | Redis cross-host locks and rate limits |
| `examlops[vector]` | pgvector vector store, independent of where platform state lives |
| `examlops[synth]` | synthetic dataset generation |
| `examlops[serving-vllm]` / `examlops[serving-sglang]` | in-process vLLM / SGLang engines (GPU host) |

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

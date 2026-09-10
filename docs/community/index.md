---
title: Get involved
description: How to use, contribute to and help shape ExaMLOps, the open-source MLOps platform for HPC — starter issues, extension points, the contributor ladder and where to ask.
---

# Get involved

ExaMLOps is an open-source (Apache-2.0) MLOps platform for HPC, and it gets better every time
someone runs it on a cluster we have never seen. You do not need a supercomputer to take
part: the unit suite and the local stack run on a laptop with the scheduler in `mock` mode.

## Start here

| If you want to… | Go to |
|---|---|
| Try it in 10 minutes | [Quick Start](../guides/quickstart.md) |
| Understand how it fits together | [System map](../explore/index.md) · [Developer onboarding](../guides/developer-onboarding.md) |
| Fix something small | Every page on this site has an **edit** button (✏️, top right) that opens the source on GitHub |
| Pick a first task | [`good first issue`](https://github.com/MSKazemi/ExaMLOps/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) · [`help wanted`](https://github.com/MSKazemi/ExaMLOps/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22) |
| Set up a dev environment and open a pull request | [Contributing guide](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/CONTRIBUTING.md) |
| Ask a question or report a bug | [Support](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/SUPPORT.md) · [open an issue](https://github.com/MSKazemi/ExaMLOps/issues/new/choose) |
| Report a vulnerability | Privately, per the [security policy](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/SECURITY.md) |

## Ways to contribute

Code is one way among several, and all of them are credited.

- **Field reports.** Tell us what happened when you ran ExaMLOps on your cluster — which
  scheduler, what broke, what you had to work around. These shape the roadmap more than
  anything else.
- **Documentation.** Fix a stale command, clarify a step that confused you, add the example you
  wished was there.
- **Tests.** A missing edge-case test for a small, pure function is a perfect first pull request.
- **Code.** Bugs, CLI papercuts, error messages that don't say how to fix the problem.
- **Reviews and triage.** Reproducing a bug report or reviewing a pull request is real
  contribution, and it is how reviewers are found.

## Extend it without forking

Most extensions live in their own package, released on their own schedule:

| Extension | How | Learn more |
|---|---|---|
| Your models and datasets | A **use-case pack** selected with `EXAMLOPS_USECASE_DIR` — the platform core never names a concrete model | [Add a new model](../guides/add-a-new-model.md) |
| Your formula for carbon, cost, drift or promotion | A **provider plugin** under the `exa.providers.<domain>` entry-point group, or a sandboxed YAML expression | [Programmable MLOps](../guides/programmable-mlops.md) · [FinOps providers](../guides/finops-providers.md) · [example plugin](https://github.com/MSKazemi/ExaMLOps/tree/main/examples/exa-carbon-plugin) |
| A command that isn't there | An **`exa` CLI plugin** under the `examlops.cli_plugins` entry-point group | `exa plugins` lists what loaded |
| An AI agent that operates the platform | The built-in **MCP server** (`exa mcp serve`) and A2A agent card | [All interfaces](../guides/interfaces.md) |

Built something? Open an issue or pull request and we will link it from these docs.

## The contributor ladder

| Role | How you get there |
|---|---|
| **User** | Run it, and tell us how it went |
| **Contributor** | One merged contribution of any kind — credited in the CHANGELOG and release notes |
| **Reviewer** | Sustained, careful contributions in one area; you are invited to review and triage there |
| **Maintainer** | Active reviewers who understand the architecture boundaries; merge and release rights |

The details, and how decisions are made, are in the
[governance document](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/GOVERNANCE.md).
Everyone follows the [Code of Conduct](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/CODE_OF_CONDUCT.md).

## What's next

The [roadmap](../explore/roadmap.md) shows what is built and what is under construction.
If you want to work on something on it, say so on an issue — we will help you scope it.

## Cite ExaMLOps

If you use ExaMLOps in research, please cite it using the metadata in
[`CITATION.cff`](https://github.com/MSKazemi/ExaMLOps/blob/main/CITATION.cff) — GitHub's
**Cite this repository** button produces APA and BibTeX from it.

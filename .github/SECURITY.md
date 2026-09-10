# Security policy

ExaMLOps runs on shared research infrastructure — HPC clusters, model registries, serving
endpoints — so we take vulnerability reports seriously and handle them privately.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security problem.**

Report it privately through **GitHub private vulnerability reporting**:
<https://github.com/MSKazemi/ExaMLOps/security/advisories/new>

If you cannot use GitHub, ask for a private channel through the contact form at
<https://mskazemi.com/#contact> — mention "ExaMLOps security", and do not include the details
of the vulnerability in that first message.

Please include what you can of:

- the affected component (e.g. control plane, dashboard backend, Skipper agent, `exa` CLI,
  Ray Serve app, Helm chart) and version or commit;
- a description of the issue and its impact;
- steps to reproduce, a proof of concept, or the relevant configuration;
- any suggested fix or mitigation.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement of your report | within 5 working days |
| Initial assessment (confirmed / not a vulnerability / need more info) | within 10 working days |
| Fix or mitigation for a confirmed issue | depends on severity; critical issues are prioritised over all other work |

We will keep you informed, coordinate a disclosure date with you, and credit you in the
advisory and release notes unless you prefer to stay anonymous.

## Supported versions

Security fixes are made on `main` and released in the next version. Only the **latest
released minor version** receives fixes; please upgrade before reporting against an older
release.

## Scope

In scope: code in this repository — the `examlops` package and `exa` CLI, the control plane,
dashboard, Skipper agent, serving and pipeline code, and the Docker Compose / Helm
deployment definitions.

Out of scope: the upstream `seanergys-modelzoo` model library (report to its maintainers),
third-party dependencies (report upstream; tell us too if ExaMLOps is exposed), and findings
that require an already-compromised host or administrator credentials.

## Hardening guidance

Operators deploying ExaMLOps should read the
[Production hardening checklist](https://mskazemi.com/ExaMLOps/guides/production-hardening/)
and [Dashboard security hardening](https://mskazemi.com/ExaMLOps/guides/dashboard-security/).
Never commit real credentials: secrets are supplied through environment variables, and the
control plane refuses placeholder tokens such as `changeme` (protected endpoints return 503
until a real credential is set).

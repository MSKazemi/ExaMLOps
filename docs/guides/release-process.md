# Releases: what is published, and how to verify it

Every ExaMLOps release is one Git tag, and one GitHub Actions workflow —
[`.github/workflows/release.yml`](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/workflows/release.yml) —
turns that tag into every artifact, at one version, signed. Nothing is built on a laptop and
nothing is uploaded by hand.

!!! note "Status"
    The first release published through it is **v0.54.0**: seven signed images, the signed Helm
    chart, and the signed release assets, each verified from outside with the commands below.
    PyPI publishing is off until the Trusted Publisher is configured (see
    [Maintainers](#one-time-repository-setup)).

## What a release publishes

| Artifact | Where | Name |
|---|---|---|
| Python package (`exa` CLI + SDK) | PyPI · GitHub Release | `examlops-X.Y.Z-py3-none-any.whl`, `examlops-X.Y.Z.tar.gz` |
| Container images | GHCR | `ghcr.io/mskazemi/examlops-{control-plane,dashboard,agent,backup,ray-serving,postgres,mlflow}:X.Y.Z` |
| Helm chart | GHCR (OCI) | `oci://ghcr.io/mskazemi/charts/examlops`, version `X.Y.Z` |
| Single-node install bundle | GitHub Release | `examlops-compose-X.Y.Z.tar.gz` — pull-only compose stack pinned to this release ([guide](install-compose-bundle.md)) |
| Release notes | GitHub Release | the `## [X.Y.Z]` section of `CHANGELOG.md`, verbatim |
| SBOM of the Python install | GitHub Release | `examlops-X.Y.Z.cdx.json` (CycloneDX 1.6 — the wheel and every dependency it resolved) |
| Image references by digest | GitHub Release | `images-X.Y.Z.txt` — `name:X.Y.Z@sha256:…` per image |
| Vulnerability reports | GitHub Release | `trivy-reports-X.Y.Z.tar.gz` — HIGH + CRITICAL per image |
| Checksums | GitHub Release | `SHA256SUMS` over every asset, and `SHA256SUMS.sigstore.json` — its cosign keyless signature |
| SLSA provenance | GitHub Release | `examlops-X.Y.Z.intoto.jsonl` — the signed in-toto provenance of the wheel and sdist |

Stable releases also move the `X.Y` and `latest` image tags; a pre-release (`vX.Y.Z-rc.1`) moves
neither. The Helm chart's `appVersion` is the release version, so the chart deploys exactly the
images released with it.

## Install a release

```bash
# CLI + SDK
pipx install examlops==X.Y.Z

# Kubernetes
helm install examlops oci://ghcr.io/mskazemi/charts/examlops --version X.Y.Z \
  --set global.imageRegistry=ghcr.io/mskazemi/
```

Apptainer / Singularity sites pull the same images: `apptainer pull docker://ghcr.io/mskazemi/examlops-control-plane:X.Y.Z`.
The Kubernetes prerequisites (Postgres, object storage, the secret the chart reads) are in
[Enterprise installation](enterprise-installation.md).

## Verify what you downloaded

You never have to trust the registry. Each artifact is bound, by a signature recorded in the
public [Rekor](https://docs.sigstore.dev/logging/overview/) transparency log, to the workflow run
and commit that built it.

**Provenance of any artifact** (wheel, image, chart) — GitHub artifact attestations, SLSA v1.0
Build Level 2:

```bash
gh attestation verify examlops-X.Y.Z-py3-none-any.whl --repo MSKazemi/ExaMLOps
gh attestation verify oci://ghcr.io/mskazemi/examlops-control-plane:X.Y.Z --repo MSKazemi/ExaMLOps
```

**Image and chart signatures** — cosign keyless; the identity is the release workflow itself:

```bash
cosign verify ghcr.io/mskazemi/examlops-control-plane:X.Y.Z \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/MSKazemi/ExaMLOps/\.github/workflows/release\.yml@refs/(tags/v.*|heads/main)$'

cosign verify ghcr.io/mskazemi/charts/examlops:X.Y.Z \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/MSKazemi/ExaMLOps/\.github/workflows/release\.yml@refs/(tags/v.*|heads/main)$'
```

**PyPI files** carry PEP 740 attestations produced by Trusted Publishing; PyPI shows them on the
file's page, and `gh attestation verify` above covers the same wheel from the GitHub side.

**Release assets** — verify the checksum file's signature once, then every file against it:

```bash
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/MSKazemi/ExaMLOps/\.github/workflows/release\.yml@refs/(tags/v.*|heads/main)$'
sha256sum -c SHA256SUMS --ignore-missing
```

**With no network access**, the `cosign` commands above work with
`--offline --trusted-root trusted_root.json` added. To carry a release, and the trusted root,
into a site with no internet with the signatures intact, see
[Air-gapped and mirrored installs](air-gapped-install.md).

## Licences of what a release contains

ExaMLOps is Apache-2.0. The images and the wheel also contain third-party packages under their
own licences. Every release lists them, so a licence review can start from the published
inventory instead of unpacking images:

- **Each image** carries a signed SPDX SBOM, which BuildKit generates at build time:

    ```bash
    docker buildx imagetools inspect ghcr.io/mskazemi/examlops-control-plane:X.Y.Z \
      --format '{{ json .SBOM.SPDX }}' > control-plane.spdx.json
    jq -r '.packages[] | [.name, .versionInfo, .licenseDeclared] | @tsv' control-plane.spdx.json
    ```

- **The wheel's dependency set** is the release asset `examlops-X.Y.Z.cdx.json`, a CycloneDX
  SBOM:

    ```bash
    jq -r '.components[] | [.name, .version,
      ([.licenses[]? | .license.id // .license.name // .expression] | join(" OR "))] | @tsv' \
      examlops-X.Y.Z.cdx.json
    ```

The images are built on Debian, so their operating-system layer includes the usual GPL and LGPL
system packages (bash, coreutils, glibc). The SBOM lists those too.

## What the workflow checks before it builds anything

A tag that fails any of these publishes nothing:

1. **The tag names the tree's version** — `v` + root `pyproject.toml` `[project].version`
   (`platform/ci/release_version.py tag`).
2. **Every copy of the version agrees** — the three member `pyproject.toml` files and the chart's
   `version` + `appVersion` (`release_version.py check`, also run on every pull request by the
   CI `package` job).
3. **`CHANGELOG.md` has a section for the version** — it becomes the release notes.
4. **The tagged commit is on `main`** — an unmerged branch is never released.
5. **The tagged commit passed CI and the security gates** — its `ci-ok` and `security-ok` checks
   both concluded `success`.

## How the artifacts are built

```text
verify ─┬─ python-dist ─────────────┐
        ├─ images (matrix) ─ chart ─┼─ github-release ─ published ─ pypi
        └───────────────────────────┘
```

- **Images are quarantined until scanned.** Each image is pushed *by digest only* — no tag, so
  nothing can pull it by name — then that exact digest is scanned with Trivy. A CRITICAL
  vulnerability that has a fix available fails the release; only then does the digest receive
  its version tags, a cosign signature, and a provenance attestation. BuildKit also attaches an
  SBOM and `mode=max` provenance to every image.
- **The chart waits for its images**, then is linted against the registry they were pushed to,
  packaged, pushed as an OCI artifact, signed and attested.
- **The GitHub Release** is created from the CHANGELOG section if it does not exist yet, and its
  assets are uploaded with `--clobber`, so re-running a release replaces rather than fails.
- **Then the release is verified as published** (`published`, which calls
  `.github/workflows/release-verify.yml`). Every artifact is fetched back from where it was
  published and checked the way a user would check it:
    - the assets against their signed checksums;
    - that the provenance attests exactly the wheel and the sdist;
    - every image's and the chart's signature and provenance, and that the tag points at the
      signed digest;
    - that the wheel installs;
    - that the dashboard image imports the matching `examlops`;
    - that the [Compose bundle](install-compose-bundle.md) starts with the released images and
      every health check passes;
    - the whole release again, mirrored into a registry on a network with no route out and
      verified offline, as in [Air-gapped installs](air-gapped-install.md).

  The checks live in `platform/ci/verify_release.sh`, which anyone can run:
  `platform/ci/verify_release.sh vX.Y.Z`. A weekly run re-checks the latest release, because a
  signature, an attestation or a tag can change after release day. The first published releases
  had three defects that only this view shows. In v0.54.0:
    - the provenance also attested `.gitignore`;
    - the dashboard image could not import `examlops`;
    - the Compose bundle's MLflow was killed for lack of memory on every start.

  Run against v0.54.0, the script fails on exactly those three.
- **PyPI goes last**, from the protected `pypi` environment, because a version published there
  can never be replaced. It waits for the published release to verify. It uses Trusted
  Publishing: no PyPI token exists anywhere.
- **Nothing publishes from a fork or a mirror** — the workflow's first job only runs in
  `MSKazemi/ExaMLOps`, and every other job depends on it.
- **Every action is pinned to a full commit SHA.** A tag is a mutable pointer; in March 2026 76 of
  77 `aquasecurity/trivy-action` tags were rewritten to credential-stealing code
  ([GHSA-69fq-xp46-6x23](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23)).

## Security checks every change passes

`.github/workflows/security.yml` runs on every pull request, every push to `main` and weekly
(advisories are published without any commit here). Each level states whether it is a **gate**
(fails the check) or a **report** (goes to the Security tab and the job summary). A report
becomes a gate only once its baseline is zero, so every gate that exists is one the tree
already passes.

| Level | Check | Gate or report |
|---|---|---|
| Secrets | gitleaks over the **whole history** (a deleted secret is still public) + `exa secrets scan` | gate — triaged false positives are listed one by one, with a reason, in `.github/.gitleaksignore` |
| SAST | CodeQL `security-extended` for Python, JavaScript/TypeScript and the workflows themselves | alerts in code scanning |
| SAST | bandit over runtime code | gate on HIGH severity; the rest reported |
| Dependencies | `dependency-review` on every pull request (in `ci.yml`) | gate on newly added vulnerable dependencies |
| Dependencies | pip-audit over `uv.lock`, npm audit over the dashboard's production dependencies | report |
| Containers & IaC | hadolint on every Dockerfile; kubeconform on the rendered chart | gate |
| Containers & IaC | trivy misconfiguration scan (Dockerfiles, compose, chart) | report |
| Release | Trivy on each image digest before it is named | gate on fixable CRITICAL (see above) |
| Posture | [OpenSSF Scorecard](https://scorecard.dev), weekly | report |

The `security-ok` job aggregates the gates; the pull-request quality gate (`ci-ok`: lint, types,
tests, the wheel, the chart, the docs) is described in
[CI/CD — GitHub Actions](cicd.md#github-actions-the-pull-request-gate). Workflow files themselves are linted by actionlint
and zizmor, and every action in them must be pinned to a full commit SHA — both enforced by the
`workflow-lint` job and `tests/unit/test_github_workflows_hardened.py`.

Report a vulnerability privately through the repository's
[security policy](https://github.com/MSKazemi/ExaMLOps/security/policy), never in a public issue.

## Maintainers: cutting a release

```bash
python3 platform/ci/release_version.py set X.Y.Z   # every version copy, in one step
# write the `## [X.Y.Z] - YYYY-MM-DD` section in CHANGELOG.md
python3 platform/ci/release_version.py check
python3 platform/ci/release_version.py notes X.Y.Z  # preview the release notes
# commit, merge to main, wait for ci-ok and security-ok on that commit (a newer push cancels an
# older commit's security scan — tag the newest, or re-run its scan), then:
git tag vX.Y.Z && git push github vX.Y.Z
```

To republish an existing tag (for example after a transient registry error), run the workflow
from the Actions tab with that tag as input; every step is idempotent.

### When the scan gate quarantines an image

A fixable CRITICAL finding stops that image before it is tagged, and with it the chart and the
GitHub Release assets. Fix the finding at its source when you can — upgrade the dependency, rebuild
on a patched base. When a finding cannot be exploited in that image (a vulnerable library the image
never calls, a scanner false positive), the owner may approve an exception in
[`.github/trivy/ignore.yaml`](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/trivy/ignore.yaml):
scoped to the one file it covers, with the reason it is not exploitable and an expiry no more than
90 days out, after which the gate fails on the finding again. The release reads that file from the
workflow's own revision, so once the exception is on `main` the same tag is re-published from the
Actions tab (**Release → Run workflow**, tag `vX.Y.Z`) — no tag moves. Every exception still appears
in the release's `trivy-reports-X.Y.Z.tar.gz`.

### One-time repository setup

These are GitHub and PyPI settings, not files, so they are made by the repository owner:

| Setting | Why |
|---|---|
| PyPI → *Publishing* → add a Trusted Publisher: owner `MSKazemi`, repo `ExaMLOps`, workflow `release.yml`, environment `pypi` | lets the workflow publish without a token |
| GitHub → *Environments* → `pypi`, with a required reviewer | a human approves the one irreversible step |
| GitHub → *Variables* → `PYPI_PUBLISH=true` | switches the PyPI job on once the publisher exists |
| GHCR → each `examlops-*` package and `charts/examlops` → visibility *Public* | anonymous `docker pull` / `helm install` |
| GitHub → *Rulesets* → `main` requires the `ci-ok` and `security-ok` checks | a merge cannot skip what the release will demand |

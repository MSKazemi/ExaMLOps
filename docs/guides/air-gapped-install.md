# Air-gapped and mirrored installs

A site with no route to the internet can run a released ExaMLOps. You carry the release across
the gap and install it from a registry you control. The part that is easy to lose on the way is
the evidence: the signatures and build provenance that prove the images are the ones the release
workflow built. This guide moves them together and verifies them on the far side without
network access.

Every step below was exercised against the published **v0.54.0** release. The far side was a
Docker network with no route out (`docker network create --internal`), holding a `registry:2`
mirror. Tools used: oras 1.3.4, cosign 3.1.3, Helm 3.20 and Docker 29. Two steps were checked
in an equivalent form:
- the Helm step up to `helm template`;
- the Docker mirror set with the daemon's `--registry-mirror` flag rather than `daemon.json`.

See [How this guide was tested](#how-this-guide-was-tested).

## What to carry across

| What | Where it comes from | Needed by |
|---|---|---|
| Release assets: wheel, sdist, chart `.tgz`, `examlops-compose-X.Y.Z.tar.gz`, `images-X.Y.Z.txt`, `SHA256SUMS`, `SHA256SUMS.sigstore.json` | the [GitHub Release](https://github.com/MSKazemi/ExaMLOps/releases) | everyone |
| The eight ExaMLOps images, with signatures and provenance | `ghcr.io/mskazemi/examlops-*`, listed with their digests in `images-X.Y.Z.txt` | Compose (seven, plus `spire-init` with the workload-identity overlay), Helm (control plane, dashboard, agent) |
| The Helm chart, with its signature | `oci://ghcr.io/mskazemi/charts/examlops` | Helm |
| Upstream images (Postgres tooling, MinIO, Prefect, the monitoring stack…) | Docker Hub, pinned by digest in the bundle's `docker-compose.yml` | Compose |
| Python dependencies | PyPI, collected with `pip download` | the `exa` CLI |
| The Sigstore trusted root | Sigstore's TUF repository | offline verification |

`images-X.Y.Z.txt` lists each image as `name:tag@digest`. Mirror from that list rather than from
tags, so the mirror holds exactly the bytes that were scanned and signed. The list is itself in
`SHA256SUMS`, so step 1 authenticates the digests before you copy anything.

## On the connected side

### 1. Download and check the release assets

```bash
gh release download vX.Y.Z --repo MSKazemi/ExaMLOps --dir release
cd release
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/MSKazemi/ExaMLOps/\.github/workflows/release\.yml@refs/(tags/v.*|heads/main)$'
sha256sum -c SHA256SUMS --ignore-missing
```

### 2. Copy the images and the chart with their signatures

The release's signatures and SLSA provenance are attached to each image as OCI *referrers*:
separate artifacts that point at the image digest. Copying the image alone leaves them behind,
and a mirror without them cannot be verified. Use a copy that follows referrers,
`oras cp -r`:

```bash
MIRROR=registry.internal          # your registry host

while read -r ref; do             # ref = ghcr.io/mskazemi/examlops-agent:X.Y.Z@sha256:…
  digest=${ref#*@}; name=${ref%@*}; repo=${name%:*}; tag=${name##*:}
  oras cp -r "$repo@$digest" "$MIRROR/examlops/${repo##*/}:$tag"
done < images-X.Y.Z.txt

oras cp -r ghcr.io/mskazemi/charts/examlops:X.Y.Z "$MIRROR/charts/examlops:X.Y.Z"
```

!!! warning "Two copies that look right and aren't"
    - **`cosign copy`** (v3.1.3) copied the image but not the release's signature or provenance.
      `cosign verify` against the mirror then fails with `no signatures found`.
    - **`helm pull` then `helm push`** re-packages the chart into a new OCI manifest. The digest
      changes (v0.54.0: `sha256:2bc3b2b7…` became `sha256:1f43d433…`), and the signature, which
      is bound to the old digest, no longer applies.

    Whatever tool you use, `cosign verify` against the mirror (step 5) is the test that the copy
    kept what matters.

The mirror does not need the OCI referrers API: `oras` falls back to the referrers tag schema
(`sha256-<digest>` tags), which cosign reads. `registry:2`, used for the test, has no referrers
API.

### 3. Copy the upstream images (Compose only)

The Compose bundle names its upstream images by their Docker Hub names, pinned by digest, for
example `grafana/loki:2.9.10@sha256:…`. `EXAMLOPS_REGISTRY` doesn't reach them, so serve them to
Docker through a registry mirror instead. Copy each one under the same repository path at the
**root** of your mirror, copying the whole multi-architecture index. The pinned digest is the
index's; a copy narrowed to one platform has a different digest, so it can't satisfy the pin.

```bash
tar xzf examlops-compose-X.Y.Z.tar.gz
grep -hoE 'image: [a-z][^$ ]+@sha256:[0-9a-f]{64}' examlops-compose-X.Y.Z/docker-compose.yml \
  | sed 's/image: //' | sort -u > upstream-images.txt

while read -r ref; do             # grafana/loki:2.9.10@sha256:… or quay.io/minio/mc:…@sha256:…
  digest=${ref#*@}; name=${ref%@*}; repo=${name%:*}; tag=${name##*:}
  case ${repo%%/*} in *.*) src=$repo; repo=${repo#*/} ;; *) src=docker.io/$repo ;; esac
  oras cp -r "$src@$digest" "$MIRROR/$repo:$tag"
done < upstream-images.txt
```

v0.54.0 has ten upstream images, from `grafana`, `minio`, `prefecthq`, `prom` and `tecnativa`.
MinIO removed `minio/minio` and `minio/mc` from Docker Hub on 2026-09-11. The same images, with
the same digests, are on quay.io, and later bundles name them there: `quay.io/minio/minio` and
`quay.io/minio/mc`. The loop above copies each one under its path without the registry host
(`$MIRROR/minio/minio:…`). For a bundle that still names them `minio/…` (v0.54.0 to v0.56.0),
copy those two from `quay.io` instead of `docker.io`. A Docker registry mirror serves Docker Hub
names only, so step 6b shows how the quay.io images reach the mirror.
`tests/unit/test_airgap_install.py` fails the build if the bundle gains an upstream image from any
other registry that this guide doesn't name.

### 4. Collect the Python wheels and the trusted root

Download dependencies with the same Python minor version and CPU architecture as the target;
running `pip download` inside the target's base image is the simplest way. Name the extras you
need:

```bash
docker run --rm -v "$PWD":/w python:3.12-slim \
  pip download --dest /w/wheelhouse --only-binary=:all: \
  '/w/examlops-X.Y.Z-py3-none-any.whl[mcp,finops]'
```

The trusted root holds the certificates and transparency-log keys that offline verification
checks against. `cosign initialize` fetches it through Sigstore's TUF repository. Carry a fresh
copy with every release you import, because Sigstore rotates keys.

```bash
cosign initialize
cp ~/.sigstore/root/tuf-repo-cdn.sigstore.dev/targets/trusted_root.json .
```

## On the far side

### 5. Verify before you install

Run these inside the gap; `--offline` makes cosign use only the trusted root and what the mirror
holds.

```bash
ID='^https://github\.com/MSKazemi/ExaMLOps/\.github/workflows/release\.yml@refs/(tags/v.*|heads/main)$'
ISSUER=https://token.actions.githubusercontent.com

# Release assets
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json --offline \
  --trusted-root trusted_root.json --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$ID"
sha256sum -c SHA256SUMS --ignore-missing

# Every image and the chart, as stored in the mirror
for image in agent backup control-plane dashboard llm-gateway mlflow postgres ray-serving spire-init; do
  cosign verify --offline --trusted-root trusted_root.json \
    "$MIRROR/examlops/examlops-$image:X.Y.Z" \
    --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$ID" > /dev/null
done
cosign verify --offline --trusted-root trusted_root.json "$MIRROR/charts/examlops:X.Y.Z" \
  --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$ID" > /dev/null

# Build provenance (SLSA v1): the builder is the tagged release workflow
cosign verify-attestation --offline --trusted-root trusted_root.json \
  --type https://slsa.dev/provenance/v1 "$MIRROR/examlops/examlops-control-plane:X.Y.Z" \
  --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$ID" > /dev/null
```

Each command exits non-zero if the signature is missing or was made by any other identity. A
plain-HTTP test registry also needs `--allow-http-registry --allow-insecure-registry` on
`cosign verify` and `cosign verify-attestation` (`verify-blob` reads local files and rejects
them), and `--plain-http` or `--to-plain-http` on oras and Helm. A production mirror should
serve TLS.

### 6a. Install with Helm

The chart builds every ExaMLOps image reference from `global.imageRegistry`. Point it at the
mirror. In a mirror you operate, a tag can be moved, so pin each tier to the digest you just
verified:

```bash
helm pull oci://$MIRROR/charts/examlops --version X.Y.Z
helm install examlops examlops-X.Y.Z.tgz \
  --set global.imageRegistry=$MIRROR/examlops/ \
  --set existingSecret=examlops-secrets \
  --set controlPlane.image.tag=X.Y.Z@sha256:… \
  --set dashboard.image.tag=X.Y.Z@sha256:… \
  --set agent.image.tag=X.Y.Z@sha256:…
```

The digests are the ones in `images-X.Y.Z.txt`. The chart needs three images: control plane,
dashboard and agent. The data services it expects (Postgres, object storage) are covered in
[Enterprise installation](enterprise-installation.md#path-b-enterprise-kubernetes-the-target-shape).
The chart `.tgz` that `helm pull` returns is the one listed in `SHA256SUMS`.

An opt-in tier that runs an upstream image takes that image from its own `image.repository`
value, pinned by `image.digest` in the chart's `values.yaml`. Mirror that image under its digest
and set the tier's `image.repository` to the mirror.

### 6b. Install with the Compose bundle

Tell Docker to fetch Docker Hub images from the mirror in `/etc/docker/daemon.json`, then restart
Docker:

```json
{ "registry-mirrors": ["https://registry.internal"] }
```

Then install the bundle as in [Install on a single node](install-compose-bundle.md), with the
ExaMLOps images coming from the mirror:

```bash
cd examlops-compose-X.Y.Z
./install.sh init
sed -i 's#^EXAMLOPS_REGISTRY=.*#EXAMLOPS_REGISTRY=registry.internal/examlops#' .env
docker compose pull
docker compose up -d
```

The upstream references stay pinned by digest, so a pull through the mirror can only return the
exact image the release was tested with.

A Docker registry mirror does not apply to `quay.io/minio/minio` and `quay.io/minio/mc`. Point
those services at your mirror with a `docker-compose.override.yml` next to `docker-compose.yml`;
Compose merges it automatically. Keep the tags and digests the bundle pins:

```yaml
services:
  minio:
    image: registry.internal/minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e
  minio-init:
    image: registry.internal/minio/mc:RELEASE.2025-08-13T08-35-41Z@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727
  s3-init:
    image: registry.internal/minio/mc:RELEASE.2025-08-13T08-35-41Z@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727
```

A v0.54.0 to v0.56.0 bundle names them `minio/minio` and `minio/mc`, which the registry mirror
does serve, so it needs no override once the mirror holds them.

!!! note "v0.54.0 only"
    The v0.54.0 bundle's MLflow limit is too small for MLflow 3.16. Add `MLFLOW_MEM_LIMIT=4g` to
    `.env` before `docker compose up`. Later bundles set it themselves.

### 6c. Install the `exa` CLI

```bash
python3.12 -m venv /opt/examlops
/opt/examlops/bin/pip install --no-index --find-links wheelhouse 'examlops[mcp,finops]==X.Y.Z'
/opt/examlops/bin/exa --version
```

## Upgrades

Repeat steps 1–5 for the new release; the new images land beside the old ones, which stay
available for a rollback. For the Compose bundle, unpack the new bundle and run
`./install.sh upgrade-env` (see [Install on a single node](install-compose-bundle.md)).

## How this guide was tested

On 2026-09-11, against v0.54.0:

- The mirror was a `registry:2` container. Every far-side command ran in containers attached
  only to a `docker network create --internal` network, where `ghcr.io` and Docker Hub did not
  resolve.
- All seven images and the chart were copied from `images-0.54.0.txt` in 81 s. Each mirror digest
  equalled the release digest.
- Offline, all eight verified with the release identity. The control plane's SLSA provenance
  verified with builder `release.yml@refs/tags/v0.54.0`. A deliberately wrong identity was
  rejected.
- Helm, inside the gap, pulled the chart from the mirror; its `.tgz` matched `SHA256SUMS`. Every
  image the chart renders was present in the mirror, and digest-pinned tags render as
  `…:0.54.0@sha256:…`.
- A Docker 29 daemon inside the gap, configured with `registry-mirrors`, pulled all images the
  bundle's `minio` and `monitoring` profiles use. It started all 16 services, and every service
  with a health check reported healthy. The six ExaMLOps images it ran matched
  `images-0.54.0.txt` digest for digest. The MLflow memory fix above was the only change.
- `pip install --no-index` of the wheel with `[mcp,finops]` succeeded with no network, and
  `pip check` found no broken requirements.

Every release now runs the mirror and offline verification steps of this guide by itself:
`platform/ci/verify_release.sh`, called by `.github/workflows/release-verify.yml` after the
release is published and weekly after that. The Compose and Helm installs are not part of that
run.

`tests/unit/test_airgap_install.py` keeps the assumptions this guide depends on true in CI:
- the release publishes `images-X.Y.Z.txt`;
- every ExaMLOps chart image is built from `global.imageRegistry`, and any upstream one reads an
  overridable repository and a pinned digest;
- every upstream image in the Compose bundle is a digest-pinned Docker Hub image.

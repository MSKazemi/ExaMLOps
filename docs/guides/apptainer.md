# Run ExaMLOps images with Apptainer (HPC login/compute nodes)

HPC centres run [Apptainer](https://apptainer.org/) (formerly Singularity) instead of Docker:
no daemon, no root, and it reads the same container images unmodified. ExaMLOps already
depends on this for GPU-serving jobs (see [vLLM serving](vlm-serving.md) and
[HPC Fleet](hpc-fleet.md)) — this page is the same pattern applied to the released platform
images, for a node that has no Docker.

This is **not** an alternative to the [Compose bundle](install-compose-bundle.md) or the
[Helm chart](enterprise-installation.md): those run the whole multi-service platform under an
orchestrator. Apptainer has no orchestrator and does not publish ports, so it runs **one image
at a time** — the shape that actually fits a login node or a batch job: the `exa` CLI, or one
service reached over SSH/VPN from wherever the rest of the platform runs.

## Pull an image

Every ExaMLOps image is public on GHCR. Pull by digest, the same one the release's
`images-X.Y.Z.txt` asset and `SHA256SUMS` name, so the `.sif` is exactly the scanned, signed
artifact:

```bash
apptainer pull examlops-backup.sif \
  docker://ghcr.io/mskazemi/examlops-backup@sha256:<digest from images-X.Y.Z.txt>
```

A tag also works and is easier to type (`docker://ghcr.io/mskazemi/examlops-backup:X.Y.Z`), at
the cost of trusting the registry to serve the same bytes next time you pull it.

No `apptainer login` is needed — GHCR packages are public once a release has run.

## Get the `exa` CLI with no Python, no `pip`, no network at pull time

`examlops-backup` is the platform's CLI image (`ENTRYPOINT ["exa"]`, see the
[install bundle](install-compose-bundle.md)'s backup sidecar). Pulled once to a `.sif` on shared
storage, every node in the allocation gets the same `exa` with no per-node install:

```bash
apptainer pull exa.sif docker://ghcr.io/mskazemi/examlops-backup@sha256:<digest>
apptainer exec exa.sif exa --version
apptainer exec exa.sif exa --json models list
```

No `--fakeroot` is needed — the image already runs as a non-root user, and Apptainer runs a
plain process as the invoking user by default. Point it at a real deployment with the same
environment variables `exa` reads anywhere else:

```bash
apptainer exec \
  --env EXAMLOPS_CONFIG=/state/config.toml \
  --bind "$HOME/examlops-state:/state" \
  exa.sif exa --json status
```

## Reach one service from a compute node

A service image (`examlops-mlflow`, `examlops-control-plane`, `examlops-ray-serving`, …) runs
the same way, bound to the data it needs:

```bash
apptainer pull ray-serving.sif docker://ghcr.io/mskazemi/examlops-ray-serving@sha256:<digest>
apptainer exec \
  --env MLFLOW_TRACKING_URI=http://mlflow.example.org:15000 \
  --env RAY_SERVE_PORT=8001 \
  --bind /scratch/$USER/examlops-state:/state \
  ray-serving.sif python serving/ray_serving/app.py
```

Apptainer does not map ports the way `docker run -p` does — the container shares the host's
network directly, so the service is reachable on the node's own address at the port it binds.
Two consequences: run it on a compute node the rest of the platform can already reach (over the
site's internal network or an SSH tunnel, never opened to the internet), and never run two
copies on the same node on the same port.

## What still needs Docker or Kubernetes

- **The full stack together** — Postgres, MinIO, MLflow, the control plane, Ray Serve, the
  dashboard, wired to each other. That's what the [Compose bundle](install-compose-bundle.md)
  and the [Helm chart](enterprise-installation.md) do; Apptainer has no equivalent of Compose's
  service graph or Kubernetes' Service objects.
- **GPU model serving on a cluster** — already solved, and already Apptainer-based: see
  [vLLM serving](vlm-serving.md) (`platform/infra/slurm-adapter/templates/vllm_serve.sh.tmpl`,
  which does exactly the `apptainer pull` + `apptainer exec` this page describes, generated and
  submitted for you by `exa hpc`) and [HPC Fleet](hpc-fleet.md) for cluster discovery and
  placement.

## Air-gapped sites

The pull above needs outbound access to `ghcr.io`. Without it, build the `.sif` somewhere that
has access and copy the file across:

```bash
apptainer pull examlops-backup.sif docker://ghcr.io/mskazemi/examlops-backup@sha256:<digest>
# copy examlops-backup.sif to the air-gapped site by whatever channel it allows
```

See [Air-gapped install](air-gapped-install.md) for the same problem at the whole-bundle level
(mirroring every image, the chart, and the compose bundle in one pass).

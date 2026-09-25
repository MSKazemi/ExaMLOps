# Verified storage initializer for KServe (ADR 0142 d3, spec-usar-1 R-SUB-20).
#
# Downloads a model artifact with KServe's own downloader (the `kserve-storage` distribution, the
# code inside KServe's storage-initializer image), then verifies it against its D3 signature
# before the model server may start: `python -m examlops.supplychain.pod_verifier SRC DEST`.
# Named by the ClusterStorageContainer `exa serve verifier-manifest` renders, and by the
# storage-initializer override on an LLMInferenceService.
#
# Build from the repository root:
#   docker build -f platform/infra/kserve/verified-storage-initializer/Dockerfile \
#     -t <registry>/examlops-verified-storage-initializer:<tag> .
# then set EXAMLOPS_KSERVE_VERIFIER_IMAGE to that reference (pin by digest in production).

# Same pinned base as the ray-serving image. kserve-storage 0.20.0 supports Python >=3.10,<3.13.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /opt/examlops

# kserve-storage is pinned to the KServe release the platform vendors CRD schemas for
# (serving/substrates/kserve_crds/v0.20.0): the downloader and the API move together.
RUN pip install --no-cache-dir "kserve-storage==0.20.0"

COPY platform/cli /opt/examlops/platform/cli
RUN pip install --no-cache-dir /opt/examlops/platform/cli

# KServe's storage initializer runs as uid 1000 and writes /mnt/models; so does this one.
USER 1000

ENTRYPOINT ["python", "-m", "examlops.supplychain.pod_verifier"]

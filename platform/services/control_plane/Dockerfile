# ExaMLOps Control Plane — retraining trigger service.
# FastAPI image with the local pipeline registry plus shared storage, coordination, and event
# publisher implementations from the CLI package.

# The upstream model library is NOT part of the platform (ADR 0094) and not in the public tree,
# so it arrives as an optional named build context. Unset, this empty stage stands in and the
# image builds from the public tree alone — which is how the release workflow builds it. A site
# build that wants it baked in passes `--build-context modelzoo=<dir>`; compose does so from
# EXAMLOPS_MODELZOO_BUILD_CONTEXT.
FROM scratch AS modelzoo

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Minimal runtime deps — uvicorn / fastapi for the API; the pipeline import
# during validation pulls in scikit-learn etc., so install the full project.
COPY pipelines /app/pipelines
COPY --from=modelzoo / /app/modelzoo/
COPY platform/cli /app/platform/cli
COPY platform/services/control_plane /app/platform/services/control_plane
# ADR 0094: concrete model YAML lives in the use-case pack, not pipelines/models. The control
# plane resolves the registry via resolve_models_dir() → <repo>/usecases/seanergy/pack.toml, so
# the pack MUST be baked in (this container has no /repo mount). Without it the model registry
# falls back to the removed legacy dir and shows 0 models.
COPY usecases /app/usecases

RUN pip install --upgrade pip \
    && pip install -e /app/pipelines \
    && pip install -e '/app/platform/cli[coordination,postgres,oidc]' \
    && pip install fastapi uvicorn[standard] \
       opentelemetry-distro==0.63b1 \
       opentelemetry-exporter-otlp-proto-grpc==1.42.1 \
       opentelemetry-instrumentation-fastapi==0.63b1 \
       opentelemetry-instrumentation-httpx==0.63b1

EXPOSE 8002

# opentelemetry-instrument auto-instruments FastAPI + httpx from env.
# A no-op when OTEL_SDK_DISABLED=true (the default in compose).
CMD ["opentelemetry-instrument", "python", "platform/services/control_plane/app.py"]

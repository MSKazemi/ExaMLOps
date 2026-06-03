# ExaMLOps Control Plane — retraining trigger service.
# Lightweight FastAPI image; only depends on uvicorn + fastapi + the local
# pipeline code (for registry validation).

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Minimal runtime deps — uvicorn / fastapi for the API; the pipeline import
# during validation pulls in scikit-learn etc., so install the full project.
COPY pipelines /app/pipelines
COPY modelzoo /app/modelzoo
COPY platform/services/control_plane /app/platform/services/control_plane

RUN pip install --upgrade pip \
    && pip install -e /app/pipelines \
    && pip install fastapi uvicorn[standard] \
       opentelemetry-distro==0.63b1 \
       opentelemetry-exporter-otlp-proto-grpc==1.42.1 \
       opentelemetry-instrumentation-fastapi==0.63b1 \
       opentelemetry-instrumentation-httpx==0.63b1

EXPOSE 8002

# opentelemetry-instrument auto-instruments FastAPI + httpx from env.
# A no-op when OTEL_SDK_DISABLED=true (the default in compose).
CMD ["opentelemetry-instrument", "python", "platform/services/control_plane/app.py"]

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Slim serving dependencies — no torch/CUDA needed for sklearn inference
COPY serving/ray_serving/requirements.txt .
RUN uv pip install --no-cache-dir -r requirements.txt --system

# Serving imports the pipelines package (registry/model loaders) and the
# inference pipeline module — copy and install them so imports work in-container
# without sys.path hacks.
COPY pipelines /app/pipelines
RUN uv pip install --no-cache-dir -e /app/pipelines --system

# Use-case pack — per-model YAML the serving layer reads (ADR 0094). Resolved via
# RAY_MODELS_DIR / EXAMLOPS_USECASE_DIR (default usecases/seanergy/models, relative to WORKDIR).
COPY usecases /app/usecases

# examlops provides the observability tracing helper (+ OTel SDK/exporter deps).
COPY platform/cli /app/platform/cli
RUN uv pip install --no-cache-dir -e /app/platform/cli --system

COPY serving/ray_serving/app.py ./serving/ray_serving/app.py
COPY serving/inference_pipeline /app/serving/inference_pipeline
COPY serving/__init__.py /app/serving/__init__.py

EXPOSE 8001 8265

CMD ["python", "serving/ray_serving/app.py"]

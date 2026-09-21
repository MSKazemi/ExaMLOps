FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
# The gateway is `examlops.gateway.service` — a library package — so the image needs the CLI
# package and nothing else: no pipelines, no serving, no use-case pack (ADR 0151 d5).
COPY platform/cli /app/platform/cli
COPY platform/services/llm_gateway /app/platform/services/llm_gateway
# `dataplane-service` is the existing extra that pins the service stack (fastapi, uvicorn,
# prometheus-client); pydantic and pyyaml arrive with it and the core dependencies.
RUN pip install --upgrade pip \
    && pip install -e '/app/platform/cli[dataplane-service,coordination,postgres]' \
    && pip install opentelemetry-distro==0.63b1 opentelemetry-exporter-otlp-proto-grpc==1.42.1 \
       opentelemetry-instrumentation-fastapi==0.63b1 opentelemetry-instrumentation-httpx==0.63b1

# Non-root, same shape as the dataplane and control-plane images: uid 10001 is the chart's
# podSecurityContext.runAsUser; a dev compose that bind-mounts a host state directory builds with
# APP_UID/APP_GID = the host user so it can write the host-owned /state/platform.db.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" examlops \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --home-dir /var/lib/examlops \
       --shell /usr/sbin/nologin examlops \
    && chown -R "${APP_UID}:${APP_GID}" /app
USER ${APP_UID}:${APP_GID}

EXPOSE 8020
CMD ["opentelemetry-instrument", "python", "platform/services/llm_gateway/main.py"]

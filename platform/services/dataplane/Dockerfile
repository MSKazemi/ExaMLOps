FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
COPY pipelines /app/pipelines
COPY platform/cli /app/platform/cli
COPY platform/services/dataplane /app/platform/services/dataplane
COPY usecases /app/usecases
RUN pip install --upgrade pip \
    && pip install -e /app/pipelines \
    && pip install -e '/app/platform/cli[dataplane,coordination,postgres]' \
    && pip install opentelemetry-distro==0.63b1 opentelemetry-exporter-otlp-proto-grpc==1.42.1 \
       opentelemetry-instrumentation-fastapi==0.63b1 opentelemetry-instrumentation-httpx==0.63b1

# Non-root, same shape as the control plane's image (platform/services/control_plane/Dockerfile):
# the default uid 10001 is the chart's podSecurityContext.runAsUser; a dev compose that
# bind-mounts a host state directory builds with APP_UID/APP_GID = the host user instead, so it
# can write the host-owned /state/platform.db. /stage is the scratch dir TMPDIR points at
# (the dataplane_stage volume) and must be the app user's, including when an empty named
# volume is mounted over it.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" examlops \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --home-dir /var/lib/examlops \
       --shell /usr/sbin/nologin examlops \
    && mkdir -p /stage \
    && chown -R "${APP_UID}:${APP_GID}" /app /stage
USER ${APP_UID}:${APP_GID}

EXPOSE 8010
CMD ["opentelemetry-instrument", "python", "platform/services/dataplane/main.py"]

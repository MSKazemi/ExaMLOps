# ExaMLOps Skipper agent — the LangGraph supervisor and its OpenAI-compatible bridge.
#
# Why this file exists: the Helm chart deploys three tiers, and until now only two of them
# could be built. `templates/agent.yaml` asked for an `examlops-agent` image while the
# repository held twelve Dockerfiles and none of them was the agent's, and no compose service
# built one either — so the chart's third tier was undeliverable. `helm lint` and `helm
# template` cannot see that; nothing does until a cluster tries to pull.
#
# Two constraints come from the chart's own pod hardening and shape this image:
#   * runAsNonRoot / runAsUser 10001 — so the image creates that user and owns /app.
#   * readOnlyRootFilesystem: true with only /tmp writable — so every default path that
#     Skipper writes to is pointed at /tmp below. Both databases otherwise default to a
#     CWD-relative name (skipper/config.py), which is exactly the kind of thing that starts
#     fine on a laptop and crashes on first write in a cluster.

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# The layout matters, not just the contents: skipper/tools/platform_ops.py reaches
# `parents[4] / "cli" / "src"` to import examlops.platform_db, so the agent must sit at
# platform/services/agent with platform/cli beside it.
COPY platform/cli /app/platform/cli
COPY platform/services/agent/requirements.txt /app/platform/services/agent/requirements.txt

RUN pip install --upgrade pip \
    && pip install -r /app/platform/services/agent/requirements.txt \
    && pip install -e /app/platform/cli

COPY platform/services/agent /app/platform/services/agent
# Copilot answers documentation questions from this public knowledge root. Private planning and
# assistant-working directories are deliberately not copied into the image.
COPY docs /app/docs

# Writable-path defaults for a read-only root filesystem. /tmp is the emptyDir the chart
# mounts; anything not listed here must not be written at runtime.
ENV HOME=/tmp \
    AGENT_DB=/tmp/agent_memory.db \
    AGENT_MEMORY_DB=/tmp/skipper_memory.db \
    PLATFORM_DB=/tmp/platform.db \
    AGENT_SERVER_HOST=0.0.0.0 \
    AGENT_SERVER_PORT=18004

# uid 10001 matches podSecurityContext.runAsUser in the chart; a mismatch there fails the
# pod at admission rather than at runtime, which is harder to read.
RUN useradd --uid 10001 --create-home --home-dir /home/agent --shell /usr/sbin/nologin agent \
    && chown -R 10001:10001 /app
USER 10001

EXPOSE 18004

CMD ["python", "platform/services/agent/agent_server.py"]

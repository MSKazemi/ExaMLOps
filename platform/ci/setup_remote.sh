#!/usr/bin/env bash
# One-time setup for ExaMLOps deployment on remote-cpu01.
# Run as the deploy user that GitLab CI will SSH as.
# Usage: bash platform/ci/setup_remote.sh <gitlab-ssh-url> [deploy-path]
set -euo pipefail

REPO_URL="${1:?Usage: $0 <gitlab-ssh-url> [deploy-path]}"
DEPLOY_PATH="${2:-/<DATA_DIR>/examlops}"
COMPOSE_DIR="$DEPLOY_PATH/platform/infra/docker-compose"
ENV_FILE="$COMPOSE_DIR/.env"

echo "==> Creating deploy path: $DEPLOY_PATH"
mkdir -p "$DEPLOY_PATH"

if [ ! -d "$DEPLOY_PATH/.git" ]; then
    echo "==> Cloning repo..."
    GIT_TERMINAL_PROMPT=0 git clone "$REPO_URL" "$DEPLOY_PATH"
else
    echo "==> Repo already exists; pulling latest..."
    cd "$DEPLOY_PATH" && GIT_TERMINAL_PROMPT=0 git pull origin main
fi

echo "==> Checking Docker..."
docker info > /dev/null 2>&1 || { echo "ERROR: Docker not accessible"; exit 1; }

echo "==> Creating dataplane-net Docker network (if absent)..."
docker network inspect dataplane-net > /dev/null 2>&1 \
    || docker network create dataplane-net

if [ ! -f "$ENV_FILE" ]; then
    echo "==> Scaffolding .env at $ENV_FILE"
    cat > "$ENV_FILE" <<'ENVEOF'
# ExaMLOps — remote-cpu01 production environment
# Fill in all values before first stack-up.

# ── Dashboard auth (required) ─────────────────────────────────────────────────
DASHBOARD_VIEWER_PASSWORD=changeme
DASHBOARD_ADMIN_PASSWORD=changeme-admin
# Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
DASHBOARD_JWT_SECRET=replace-with-32-plus-char-secret
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DASHBOARD_SECRET_KEY=replace-with-44-char-fernet-key==

# ── Cluster-specific ─────────────────────────────────────────────────────────
PUBLIC_HOST=remote-cpu01
EXAMLOPS_SLURM_MODE=slurm

# ── Control Plane (required for post-deploy CI jobs) ─────────────────────────
# CONTROL_PLANE_TOKEN=replace-with-strong-token

# ── Optional overrides (defaults are fine for the local stack) ────────────────
# MLFLOW_TRACKING_URI=http://localhost:15000
# MLFLOW_S3_ENDPOINT_URL=http://localhost:19000
# AWS_ACCESS_KEY_ID=minioadmin
# AWS_SECRET_ACCESS_KEY=minioadmin
ENVEOF
    echo ""
    echo ">>> IMPORTANT: Edit $ENV_FILE and fill in all required values before stack-up."
else
    echo "==> .env already exists at $ENV_FILE — skipping scaffold."
fi

echo ""
echo "==> Setup complete. Next steps:"
echo "    1. Edit $ENV_FILE (fill in passwords, secrets, tokens)"
echo "    2. docker compose -f $COMPOSE_DIR/docker-compose.yml up --build -d"
echo "    3. docker compose -f $COMPOSE_DIR/docker-compose.yml ps"

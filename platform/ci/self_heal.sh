#!/usr/bin/env bash
# ExaMLOps runtime self-healer — runs on lxp-cpu01 via systemd timer.
# Scans all compose-managed containers; restarts any that are exited or
# report "unhealthy" from their Docker healthcheck.
#
# Install the systemd timer:  sudo bash platform/ci/setup_selfheal_systemd.sh
# Check logs:                 journalctl -t examlops-selfheal -f
set -uo pipefail

DEPLOY_PATH="${EXAMLOPS_DEPLOY_PATH:-/opt/examlops}"
COMPOSE_FILE="$DEPLOY_PATH/platform/infra/docker-compose/docker-compose.yml"
COMPOSE_LXP_FILE="$DEPLOY_PATH/platform/infra/docker-compose/docker-compose.lxp.yml"
LOG_TAG="examlops-selfheal"

log() {
    local msg="[self-heal] $*"
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $msg"
    logger -t "$LOG_TAG" "$msg" 2>/dev/null || true
}

if [ ! -f "$COMPOSE_FILE" ]; then
    log "ERROR: compose file not found at $COMPOSE_FILE — aborting"
    exit 1
fi

cd "$DEPLOY_PATH"

HEALED=0
ERRORS=0

# Gather service names + their state + health status from Docker
while IFS=$'\t' read -r service state health; do
    [ -z "$service" ] && continue

    needs_restart=false

    if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
        # Skip one-shot init containers (restart policy = "no")
        restart_policy=$(docker compose -f "$COMPOSE_FILE" -f "$COMPOSE_LXP_FILE" \
            ps --format json "$service" 2>/dev/null \
            | python3 -c "
import sys, json
data = sys.stdin.read().strip()
if data:
    print(json.loads(data).get('ExitCode', 0))
" 2>/dev/null || echo "0")
        # Check the actual restart policy from container config
        container_name=$(docker compose -f "$COMPOSE_FILE" -f "$COMPOSE_LXP_FILE" \
            ps -q "$service" 2>/dev/null | head -1)
        if [ -n "$container_name" ]; then
            policy=$(docker inspect --format='{{.HostConfig.RestartPolicy.Name}}' \
                "$container_name" 2>/dev/null || echo "unknown")
            [ "$policy" != "no" ] && needs_restart=true
        fi
    elif [ "$health" = "unhealthy" ]; then
        needs_restart=true
    fi

    if [ "$needs_restart" = "true" ]; then
        log "Service '$service' is degraded (state=$state health=$health) — restarting"
        if docker compose -f "$COMPOSE_FILE" -f "$COMPOSE_LXP_FILE" \
                restart "$service" 2>&1 | logger -t "$LOG_TAG" 2>/dev/null; then
            log "Restarted '$service' successfully"
            HEALED=$((HEALED + 1))
        else
            log "ERROR: failed to restart '$service'"
            ERRORS=$((ERRORS + 1))
        fi
    fi
done < <(
    docker compose -f "$COMPOSE_FILE" -f "$COMPOSE_LXP_FILE" \
        ps --format json 2>/dev/null \
    | python3 - <<'PYEOF'
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    obj = json.loads(line)
    service = obj.get("Service", "")
    state   = obj.get("State", "").lower()
    health  = obj.get("Health", "").lower()
    if service:
        print(f"{service}\t{state}\t{health}")
PYEOF
)

if [ "$HEALED" -gt 0 ] || [ "$ERRORS" -gt 0 ]; then
    log "Done: healed=$HEALED errors=$ERRORS"
else
    log "All services healthy — no action needed"
fi

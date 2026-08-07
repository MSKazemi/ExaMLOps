#!/usr/bin/env bash
# Post-deploy smoke / health check for ExaMLOps on lxp-cpu01.
# Runs ON lxp-cpu01 via SSH — uses localhost ports.
# Exit 0 = all critical services healthy.  Exit 1 = at least one failure.
#
# Called by smoke:lxp in .gitlab-ci.yml.
set -uo pipefail

DEPLOY_PATH="${EXAMLOPS_DEPLOY_PATH:-/opt/examlops}"
COMPOSE_FILE="$DEPLOY_PATH/platform/infra/docker-compose/docker-compose.yml"
COMPOSE_LXP_FILE="$DEPLOY_PATH/platform/infra/docker-compose/docker-compose.lxp.yml"

FAILURES=0
WARNINGS=0

probe() {
    local label="$1" url="$2" critical="${3:-true}"
    local code
    code=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 10 "$url" 2>/dev/null || echo "000")
    if [[ "$code" =~ ^(200|204|301|302)$ ]]; then
        echo "  PASS  $label  ($url) → $code"
    elif [ "$critical" = "false" ]; then
        echo "  WARN  $label  ($url) → $code  [non-critical]"
        WARNINGS=$((WARNINGS + 1))
    else
        echo "  FAIL  $label  ($url) → $code"
        FAILURES=$((FAILURES + 1))
    fi
}

check_container_health() {
    local service="$1" critical="${2:-true}"
    local health
    health=$(docker compose -f "$COMPOSE_FILE" -f "$COMPOSE_LXP_FILE" \
        ps --format json "$service" 2>/dev/null \
        | python3 -c "import sys,json; data=sys.stdin.read().strip(); obj=json.loads(data) if data else {}; print(obj.get('Health','unknown'))" \
        2>/dev/null || echo "unknown")
    if [ "$health" = "healthy" ]; then
        echo "  PASS  container/$service → $health"
    elif [ "$health" = "unknown" ] || [ "$health" = "" ]; then
        echo "  SKIP  container/$service → no healthcheck configured"
    elif [ "$critical" = "false" ]; then
        echo "  WARN  container/$service → $health  [non-critical]"
        WARNINGS=$((WARNINGS + 1))
    else
        echo "  FAIL  container/$service → $health"
        FAILURES=$((FAILURES + 1))
    fi
}

echo "========================================"
echo " ExaMLOps Post-Deploy Smoke Check"
echo " $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "========================================"
echo ""

echo "── Critical HTTP endpoints ─────────────"
probe "Dashboard API"   "http://localhost:18099/api/health"
probe "Control Plane"   "http://localhost:18002/health"
probe "MLflow"          "http://localhost:15000/health"
probe "Prefect"         "http://localhost:14200/api/health"

echo ""
echo "── Non-critical HTTP endpoints ─────────"
probe "Ray Serve"       "http://localhost:18001/-/healthz"  false
probe "MinIO"           "http://localhost:19000/minio/health/live"  false
probe "Prometheus"      "http://localhost:19090/-/healthy"  false
probe "Grafana"         "http://localhost:13000/api/health"  false

echo ""
echo "── Docker container health states ──────"
cd "$DEPLOY_PATH"
check_container_health "mlflow"
check_container_health "orchestrator"   # Prefect server — compose service is "orchestrator", not "prefect"
check_container_health "control-plane"
check_container_health "dashboard"
check_container_health "postgres"    false
check_container_health "minio"       false

echo ""
echo "── Summary ─────────────────────────────"
echo "  Failures : $FAILURES"
echo "  Warnings : $WARNINGS"

if [ "$FAILURES" -gt 0 ]; then
    echo "  RESULT  : UNHEALTHY — $FAILURES critical check(s) failed"
    exit 1
else
    echo "  RESULT  : HEALTHY"
    exit 0
fi

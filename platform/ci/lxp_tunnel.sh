#!/usr/bin/env bash
# Open SSH tunnels for all ExaMLOps services on lxp-cpu01.
# Usage: bash platform/ci/lxp_tunnel.sh [user@host]
#   default host: u1002@23.109.46.77
set -euo pipefail

TARGET="${1:-u1002@23.109.46.77}"

echo "Opening ExaMLOps tunnels to $TARGET ..."
echo "Press Ctrl-C to close all tunnels."
echo ""
echo "  Dashboard      → http://localhost:18099"
echo "  MLflow         → http://localhost:15000"
echo "  Prefect        → http://localhost:14200"
echo "  Ray Serve      → http://localhost:18001"
echo "  Ray Dashboard  → http://localhost:18265"
echo "  Control Plane  → http://localhost:18002"
echo "  MinIO Console  → http://localhost:19001"
echo "  Prometheus     → http://localhost:19090"
echo "  Grafana        → http://localhost:13000"
echo "  Alertmanager   → http://localhost:19093"
echo ""

ssh -N \
  -L 18099:localhost:18099 \
  -L 15000:localhost:15000 \
  -L 14200:localhost:14200 \
  -L 18001:localhost:18001 \
  -L 18265:localhost:18265 \
  -L 18002:localhost:18002 \
  -L 19001:localhost:19001 \
  -L 19090:localhost:19090 \
  -L 13000:localhost:13000 \
  -L 19093:localhost:19093 \
  "$TARGET"

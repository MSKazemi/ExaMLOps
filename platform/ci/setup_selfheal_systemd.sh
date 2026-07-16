#!/usr/bin/env bash
# Install the ExaMLOps self-healing systemd timer on remote-cpu01.
# Run once as root or with sudo:
#   sudo bash platform/ci/setup_selfheal_systemd.sh [deploy-path]
#
# After install:
#   systemctl status examlops-selfheal.timer
#   journalctl -t examlops-selfheal -f
set -euo pipefail

DEPLOY_PATH="${1:-/<DATA_DIR>/examlops}"
UNIT_DIR="/etc/systemd/system"
HEAL_SCRIPT="$DEPLOY_PATH/platform/ci/self_heal.sh"

echo "==> Installing ExaMLOps self-heal timer"
echo "    deploy-path : $DEPLOY_PATH"
echo "    heal script : $HEAL_SCRIPT"
echo ""

if [ ! -f "$HEAL_SCRIPT" ]; then
    echo "ERROR: $HEAL_SCRIPT not found — run from the repo root or check DEPLOY_PATH"
    exit 1
fi

chmod +x "$HEAL_SCRIPT"

# ── systemd service unit ────────────────────────────────────────────────────
cat > "$UNIT_DIR/examlops-selfheal.service" <<SERVICE
[Unit]
Description=ExaMLOps container self-healing
Documentation=file://$DEPLOY_PATH/platform/ci/self_heal.sh
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
Environment=EXAMLOPS_DEPLOY_PATH=$DEPLOY_PATH
ExecStart=/usr/bin/bash $HEAL_SCRIPT
StandardOutput=journal
StandardError=journal
SyslogIdentifier=examlops-selfheal
SERVICE

# ── systemd timer unit ──────────────────────────────────────────────────────
cat > "$UNIT_DIR/examlops-selfheal.timer" <<TIMER
[Unit]
Description=ExaMLOps self-heal every 2 minutes
Requires=examlops-selfheal.service

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now examlops-selfheal.timer

echo ""
echo "==> Self-heal timer installed and enabled."
systemctl status examlops-selfheal.timer --no-pager
echo ""
echo "Useful commands:"
echo "  journalctl -t examlops-selfheal -f            # tail logs"
echo "  systemctl list-timers examlops-selfheal.timer # next run time"
echo "  bash $HEAL_SCRIPT                             # run manually"

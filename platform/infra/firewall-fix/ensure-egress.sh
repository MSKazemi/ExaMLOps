#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# ensure-egress.sh — self-healing Docker-bridge egress for remote-cpu01
#
# WHY THIS EXISTS
#   On remote-cpu01, firewalld leaves a stray nftables chain `ip filter forward`
#   with `policy drop`. Docker's own FORWARD-accept rules are evaluated first,
#   but this leftover chain runs second and drops new outbound packets from the
#   Docker bridges, so containers lose all egress (DNS + TCP). firewalld also
#   *regenerates* that drop chain on every reload, and a `docker compose down/up`
#   renames the bridge interface — both of which silently break any one-off fix.
#
# WHAT IT DOES
#   Idempotently ensures an ACCEPT rule for the whole Docker IPAM range
#   (172.16.0.0/12) exists in that chain, re-applying it whenever firewalld
#   wipes it. Matching by SUBNET (not bridge interface name) means it keeps
#   working across network recreation. Tagged with an nft `comment` so we can
#   detect our own rule and never insert duplicates.
#
# HOW IT RUNS
#   As a privileged, host-network sidecar container (see docker-compose.yml).
#   Needs root in the host network namespace (privileged / CAP_NET_ADMIN) to
#   edit the host ruleset. Requires only Docker access — no host sudo.
#
#   This is a STOPGAP. The durable fix is a host-level systemd unit owned by the
#   sysadmin (see internal design notes). Keep this
#   running until that lands.
# ──────────────────────────────────────────────────────────────────────────────
set -u

MARKER="${MARKER:-examlops-egress-fix}"
SUBNET="${SUBNET:-172.16.0.0/12}"
INTERVAL="${INTERVAL:-30}"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

# nft is not in the base alpine image; install once (host egress works, so this
# succeeds even while container egress is broken — the host is not forwarded).
if ! command -v nft >/dev/null 2>&1; then
    log "installing nftables ..."
    apk add --no-cache nftables >/dev/null 2>&1 || log "WARN: apk add nftables failed; will retry via loop"
fi

log "watcher started (subnet=$SUBNET marker=$MARKER interval=${INTERVAL}s)"

while true; do
    if command -v nft >/dev/null 2>&1 && nft list chain ip filter forward >/dev/null 2>&1; then
        if nft -a list chain ip filter forward 2>/dev/null | grep -q "$MARKER"; then
            :  # rule present — nothing to do
        else
            if nft insert rule ip filter forward ip saddr "$SUBNET" accept comment "$MARKER" 2>/dev/null; then
                log "INSERTED egress-allow rule (ip saddr $SUBNET accept) — drop chain had wiped it"
            else
                log "WARN: failed to insert egress rule (will retry)"
            fi
        fi
    else
        # nft missing or the leftover chain not present yet (e.g. just after boot
        # before firewalld finished). Try to (re)install nft, then retry next tick.
        command -v nft >/dev/null 2>&1 || apk add --no-cache nftables >/dev/null 2>&1 || true
    fi
    sleep "$INTERVAL"
done

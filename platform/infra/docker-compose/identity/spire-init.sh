#!/bin/sh
# One-shot SPIRE setup for the Compose identity overlay (ADR 0125). Idempotent: safe on every `up`.
#
#   spire-init pki       the node CA and the SPIRE agent's x509pop certificate (before the server)
#   spire-init register  the server's CA for the agent to bootstrap from, and the registration
#                        entries: one node alias, one workload per name in SPIFFE_WORKLOADS
#
# A workload's entry selects a container label, examlops.spiffe=<name>; the spiffe-helper beside the
# service carries it. An existing entry is left as it is, so an operator's `entry update` survives.
set -eu

TD="${EXAMLOPS_SPIFFE_TRUST_DOMAIN:?set EXAMLOPS_SPIFFE_TRUST_DOMAIN}"
PKI="${SPIRE_PKI_DIR:-/pki}"
SOCKET="${SPIRE_SERVER_SOCKET:-/run/spire/server/private/api.sock}"
NODE_ID="spiffe://$TD/agent/compose"
NODE_CN="examlops-compose-agent"
JWT_TTL="${SPIFFE_JWT_SVID_TTL:-300}"

pki() {
    umask 077
    mkdir -p "$PKI"
    if [ ! -s "$PKI/node-ca.pem" ] || [ ! -s "$PKI/node-ca-key.pem" ]; then
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
            -subj "/CN=examlops-spire-node-ca" \
            -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" \
            -keyout "$PKI/node-ca-key.pem" -out "$PKI/node-ca.pem"
        rm -f "$PKI/agent-cert.pem"
        echo "spire-init: created the node CA"
    fi
    # Re-issue the agent's certificate when it is missing, expires within 30 days, or was not
    # signed by the current node CA. Its CN is what the node alias entry selects.
    if ! openssl x509 -checkend 2592000 -noout -in "$PKI/agent-cert.pem" >/dev/null 2>&1 ||
        ! openssl verify -CAfile "$PKI/node-ca.pem" "$PKI/agent-cert.pem" >/dev/null 2>&1; then
        printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n' \
            >"$PKI/agent.ext"
        openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$NODE_CN" \
            -keyout "$PKI/agent-key.pem" -out "$PKI/agent.csr"
        openssl x509 -req -in "$PKI/agent.csr" -CA "$PKI/node-ca.pem" \
            -CAkey "$PKI/node-ca-key.pem" -set_serial "0x$(openssl rand -hex 16)" -days 365 \
            -extfile "$PKI/agent.ext" -out "$PKI/agent-cert.pem"
        rm -f "$PKI/agent.csr" "$PKI/agent.ext"
        echo "spire-init: issued the agent's node certificate"
    fi
    # The server (uid 1000) reads only the CA certificate; both private keys stay root-only.
    chmod 0755 "$PKI"
    chmod 0644 "$PKI/node-ca.pem" "$PKI/agent-cert.pem"
    chmod 0600 "$PKI/node-ca-key.pem" "$PKI/agent-key.pem"
}

server() {
    spire-server "$@" -socketPath "$SOCKET"
}

# ensure_entry <spiffe-id> <entry create arguments…>: create the entry unless one exists.
ensure_entry() {
    id="$1"
    shift
    if server entry show -spiffeID "$id" | grep -q "^Found 0 entries"; then
        server entry create -spiffeID "$id" "$@" >/dev/null
        echo "spire-init: registered $id"
    fi
}

register() {
    tries=0
    until server healthcheck >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "$tries" -ge 90 ]; then
            echo "spire-init: the SPIRE server did not become healthy" >&2
            exit 1
        fi
        sleep 1
    done
    # The agent bootstraps trust from this file (trust_bundle_path), never insecurely.
    server bundle show -format pem >"$PKI/server-bundle.pem.tmp"
    chmod 0644 "$PKI/server-bundle.pem.tmp"
    mv "$PKI/server-bundle.pem.tmp" "$PKI/server-bundle.pem"
    ensure_entry "$NODE_ID" -node -selector "x509pop:subject:cn:$NODE_CN"
    for name in ${SPIFFE_WORKLOADS:-}; do
        ensure_entry "spiffe://$TD/$name" -parentID "$NODE_ID" \
            -selector "docker:label:examlops.spiffe:$name" -jwtSVIDTTL "$JWT_TTL"
    done
}

case "${1:-}" in
pki) pki ;;
register) register ;;
*)
    echo "usage: spire-init pki|register" >&2
    exit 2
    ;;
esac

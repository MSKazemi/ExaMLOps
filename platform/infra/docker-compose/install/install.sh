#!/bin/sh
# ExaMLOps install bundle helper.
#
#   ./install.sh init [--version X.Y.Z] [--force]
#       Write .env from env.template with freshly generated secrets, and create the state dir.
#       Refuses to overwrite an existing .env unless --force (which rotates every secret — only
#       safe before the first `docker compose up`, since Postgres and MinIO keep the old ones).
#   ./install.sh check
#       Fail if .env is missing or still holds a placeholder, then validate the compose file.
#
# The version comes from --version, else $EXAMLOPS_VERSION, else the VERSION file the release
# tarball carries. POSIX sh; needs only /dev/urandom, od and base64.
set -eu

cd "$(dirname "$0")"

die() { echo "install.sh: $*" >&2; exit 1; }

hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# Fernet key: url-safe base64 of 32 random bytes (44 characters, '=' padded).
fernet() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }

generate_env() {
    version=$1
    # Each placeholder occurrence gets its own value, so no two credentials are ever equal.
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *=__GENERATE__) line="${line%__GENERATE__}$(hex 24)" ;;
            *=__GENERATE_USER__) line="${line%__GENERATE_USER__}examlops-$(hex 6)" ;;
            *=__GENERATE_FERNET__) line="${line%__GENERATE_FERNET__}$(fernet)" ;;
            *=__EXAMLOPS_VERSION__) line="${line%__EXAMLOPS_VERSION__}${version}" ;;
            *=__HOST_UID__) line="${line%__HOST_UID__}$(id -u)" ;;
            *=__HOST_GID__) line="${line%__HOST_GID__}$(id -g)" ;;
        esac
        printf '%s\n' "$line"
    done < env.template
}

cmd_init() {
    version=${EXAMLOPS_VERSION:-}
    force=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --version) [ $# -ge 2 ] || die "--version needs a value"; version=$2; shift 2 ;;
            --force) force=1; shift ;;
            *) die "unknown option: $1" ;;
        esac
    done
    if [ -z "$version" ] && [ -f VERSION ]; then
        version=$(tr -d ' \n' < VERSION)
    fi
    [ -n "$version" ] || die "no version: pass --version X.Y.Z (or run from a release tarball)"
    case "$version" in
        [0-9]*.[0-9]*.[0-9]*) ;;
        *) die "version must look like X.Y.Z, got '$version'" ;;
    esac
    if [ -e .env ] && [ "$force" -ne 1 ]; then
        die ".env already exists; keep it, or rerun with --force before the first start"
    fi
    # The state dir keeps the default umask: services running as a non-root user must reach it.
    state_dir=$(sed -n 's/^EXAMLOPS_STATE_DIR=//p' env.template)
    mkdir -p "${state_dir:-./state}"
    (umask 077 && generate_env "$version" > .env.tmp)
    mv .env.tmp .env
    echo "Wrote .env (mode 600) for ExaMLOps $version and created ${state_dir:-./state}."
    echo "Review .env (PUBLIC_HOST, EXAMLOPS_BIND, AGENT_CONTAINER_OLLAMA_URL), then:"
    echo "  docker compose up -d"
    echo "Dashboard login: admin / the DASHBOARD_ADMIN_PASSWORD value in .env"
}

cmd_check() {
    [ -f .env ] || die "no .env: run ./install.sh init first"
    if grep -v '^[[:space:]]*#' .env | grep -q '=__[A-Z_]*__$'; then
        die ".env still contains a placeholder; run ./install.sh init"
    fi
    command -v docker >/dev/null 2>&1 || die "docker is not installed"
    docker compose config --quiet
    echo "OK: .env is complete and docker-compose.yml is valid."
}

case "${1:-}" in
    init) shift; cmd_init "$@" ;;
    check) shift; cmd_check ;;
    *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

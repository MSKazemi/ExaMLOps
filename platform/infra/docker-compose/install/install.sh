#!/bin/sh
# ExaMLOps install bundle helper.
#
#   ./install.sh init [--version X.Y.Z] [--force]
#       Write .env from env.template with freshly generated secrets, and create the state dir.
#       Refuses to overwrite an existing .env unless --force (which rotates every secret — only
#       safe before the first `docker compose up`, since Postgres and MinIO keep the old ones).
#   ./install.sh upgrade-env [--version X.Y.Z]
#       After unpacking a newer bundle over an install: add every setting the new env.template
#       has and .env lacks (renamed settings keep their old value, new secrets are generated),
#       and move EXAMLOPS_VERSION to the new release. Never changes a value that is already set.
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

# Settings renamed between releases, as "old new". upgrade-env carries the old value over:
# MinIO keeps its root credentials in its data volume, so they must not be regenerated.
RENAMED="MINIO_ROOT_USER EXAMLOPS_S3_ACCESS_KEY
MINIO_ROOT_PASSWORD EXAMLOPS_S3_SECRET_KEY
MINIO_SERVING_ACCESS_KEY EXAMLOPS_S3_SERVING_ACCESS_KEY
MINIO_SERVING_SECRET_KEY EXAMLOPS_S3_SERVING_SECRET_KEY"

# One template line with its placeholder filled. Every call draws fresh randomness, so no two
# credentials are ever equal.
fill_line() {
    line=$1 version=$2
    case "$line" in
        *=__GENERATE__) line="${line%__GENERATE__}$(hex 24)" ;;
        *=__GENERATE_USER__) line="${line%__GENERATE_USER__}examlops-$(hex 6)" ;;
        *=__GENERATE_FERNET__) line="${line%__GENERATE_FERNET__}$(fernet)" ;;
        *=__EXAMLOPS_VERSION__) line="${line%__EXAMLOPS_VERSION__}${version}" ;;
        *=__HOST_UID__) line="${line%__HOST_UID__}$(id -u)" ;;
        *=__HOST_GID__) line="${line%__HOST_GID__}$(id -g)" ;;
    esac
    printf '%s\n' "$line"
}

generate_env() {
    while IFS= read -r tline || [ -n "$tline" ]; do
        fill_line "$tline" "$1"
    done < env.template
}

# Sets $version from --version, $EXAMLOPS_VERSION or ./VERSION; the remaining options are
# left in $rest for the caller.
resolve_version() {
    version=${EXAMLOPS_VERSION:-}
    rest=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --version) [ $# -ge 2 ] || die "--version needs a value"; version=$2; shift 2 ;;
            *) rest="$rest $1"; shift ;;
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
}

make_dirs() {
    # The state dir keeps the default umask: services running as a non-root user must reach it.
    state_dir=$(sed -n 's/^EXAMLOPS_STATE_DIR=//p' .env 2>/dev/null || true)
    [ -n "$state_dir" ] || state_dir=$(sed -n 's/^EXAMLOPS_STATE_DIR=//p' env.template)
    mkdir -p "${state_dir:-./state}"
    # Alertmanager receiver secrets (monitoring profile): readable by the operator's group only.
    mkdir -p secrets/alertmanager && chmod 750 secrets secrets/alertmanager
}

cmd_init() {
    resolve_version "$@"
    force=0
    for opt in $rest; do
        case "$opt" in
            --force) force=1 ;;
            *) die "unknown option: $opt" ;;
        esac
    done
    if [ -e .env ] && [ "$force" -ne 1 ]; then
        die ".env already exists; keep it, or rerun with --force before the first start"
    fi
    (umask 077 && generate_env "$version" > .env.tmp)
    mv .env.tmp .env
    make_dirs
    echo "Wrote .env (mode 600) for ExaMLOps $version and created ${state_dir:-./state}."
    echo "Review .env (PUBLIC_HOST, EXAMLOPS_BIND, AGENT_CONTAINER_OLLAMA_URL), then:"
    echo "  docker compose up -d"
    echo "Dashboard login: admin / the DASHBOARD_ADMIN_PASSWORD value in .env"
}

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }
env_has() { grep -q "^$1=" .env; }

cmd_upgrade_env() {
    [ -f .env ] || die "no .env: run ./install.sh init first"
    resolve_version "$@"
    [ -z "$rest" ] || die "unknown option:$rest"
    added=""
    (
        umask 077
        cp .env .env.tmp
        stamp=0
        while IFS= read -r tline || [ -n "$tline" ]; do
            case "$tline" in ''|'#'*) continue ;; esac
            key=${tline%%=*}
            env_has "$key" && continue
            old=$(printf '%s\n' "$RENAMED" | awk -v n="$key" '$2 == n { print $1 }')
            if [ -n "$old" ] && env_has "$old"; then
                line="$key=$(env_value "$old")"
            else
                line=$(fill_line "$tline" "$version")
            fi
            if [ "$stamp" -eq 0 ]; then
                printf '\n# Added by install.sh upgrade-env for %s\n' "$version" >> .env.tmp
                stamp=1
            fi
            printf '%s\n' "$line" >> .env.tmp
            printf '%s\n' "$key" >> .env.added
        done < env.template
        # Move the release pin; keep every other value as the operator left it.
        sed "s/^EXAMLOPS_VERSION=.*/EXAMLOPS_VERSION=$version/" .env.tmp > .env.tmp2
        mv .env.tmp2 .env.tmp
    )
    mv .env.tmp .env
    make_dirs
    if [ -f .env.added ]; then
        added=$(tr '\n' ' ' < .env.added)
        rm -f .env.added
        echo "Added to .env: $added"
    else
        echo "No new settings."
    fi
    echo "EXAMLOPS_VERSION=$version. Next: docker compose pull && docker compose up -d"
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
    upgrade-env) shift; cmd_upgrade_env "$@" ;;
    check) shift; cmd_check ;;
    *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

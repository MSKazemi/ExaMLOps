#!/usr/bin/env bash
# Deploy or reactivate an immutable ExaMLOps release on the LXP node.
#
# The legacy checkout is intentionally never cleaned or reset. It is used once to seed
# persistent state, while application source comes from a GitLab-created archive whose
# directory is named after the tested commit.
set -euo pipefail

ACTION="${1:?usage: $0 <deploy|activate> <legacy-path> <sha-or-release> [archive]}"
LEGACY_PATH="${2:?legacy deploy path is required}"
RELEASE_ROOT="${LEGACY_PATH}-releases"
STATE_ROOT="${LEGACY_PATH}-state"
CURRENT_LINK="${LEGACY_PATH}-current"

copy_once() {
    local source="$1" target="$2"
    if [ ! -e "$target" ] && [ ! -L "$target" ] && [ -e "$source" ]; then
        cp -a "$source" "$target"
    fi
}

seed_state() {
    mkdir -p "$STATE_ROOT/.providers"
    copy_once "$LEGACY_PATH/.env" "$STATE_ROOT/root.env"
    copy_once "$LEGACY_PATH/platform/infra/docker-compose/.env" "$STATE_ROOT/compose.env"
    copy_once "$LEGACY_PATH/platform/infra/docker-compose/.env.dashboard" \
        "$STATE_ROOT/compose.env.dashboard"
    copy_once "$LEGACY_PATH/platform.db" "$STATE_ROOT/platform.db"
    copy_once "$LEGACY_PATH/modelzoo" "$STATE_ROOT/modelzoo"

    if [ ! -e "$STATE_ROOT/platform.db" ]; then
        install -m 0600 /dev/null "$STATE_ROOT/platform.db"
    fi
    if [ ! -e "$STATE_ROOT/.providers/.seeded" ]; then
        if [ -d "$LEGACY_PATH/.providers" ]; then
            cp -a "$LEGACY_PATH/.providers/." "$STATE_ROOT/.providers/"
        fi
        touch "$STATE_ROOT/.providers/.seeded"
    fi
}

prepare_release() {
    local sha="$1" archive="$2" release_path="$RELEASE_ROOT/$sha"
    case "$sha" in
        *[!0-9a-f]*|'') echo "invalid release SHA: $sha" >&2; exit 2 ;;
    esac
    [ -f "$archive" ] || { echo "release archive not found: $archive" >&2; exit 2; }

    mkdir -p "$RELEASE_ROOT"
    if [ -d "$release_path" ]; then
        test "$(cat "$release_path/.examlops-release-sha" 2>/dev/null)" = "$sha" || {
            echo "existing release directory has no matching integrity marker: $release_path" >&2
            exit 1
        }
    else
        local staging="$RELEASE_ROOT/.staging-$sha-$$"
        trap 'rm -rf -- "$staging"' EXIT
        mkdir -p "$staging"
        tar -xzf "$archive" -C "$staging"
        printf '%s\n' "$sha" > "$staging/.examlops-release-sha"
        mv "$staging" "$release_path"
        trap - EXIT
    fi

    copy_once "$STATE_ROOT/root.env" "$release_path/.env"
    copy_once "$STATE_ROOT/compose.env" \
        "$release_path/platform/infra/docker-compose/.env"
    copy_once "$STATE_ROOT/compose.env.dashboard" \
        "$release_path/platform/infra/docker-compose/.env.dashboard"
    copy_once "$STATE_ROOT/modelzoo" "$release_path/modelzoo"
    printf '%s\n' "$release_path"
}

activate_release() {
    local release_path="$1"
    case "$release_path" in
        "$LEGACY_PATH"|"$RELEASE_ROOT"/*) ;;
        *) echo "refusing release path outside the managed roots: $release_path" >&2; exit 2 ;;
    esac
    [ -d "$release_path/platform/infra/docker-compose" ] || {
        echo "invalid release directory: $release_path" >&2
        exit 2
    }

    export EXAMLOPS_HOST_REPO="$release_path"
    export EXAMLOPS_STATE_DIR="$STATE_ROOT"
    export PLATFORM_DB="$STATE_ROOT/platform.db"
    export EXAMLOPS_PROVIDERS_DIR="$STATE_ROOT/.providers"
    export PATH="$HOME/.local/bin:/localhome/${USER}/.local/bin:$PATH"

    cd "$release_path"
    uv pip install -e . >/dev/null 2>&1 \
        && echo "exa CLI refreshed: $(.venv/bin/exa --version 2>/dev/null)" \
        || echo "warn: exa CLI refresh skipped (uv unavailable) — non-fatal"
    setfacl -R -m u:1000:rwX -m d:u:1000:rwX usecases pipelines 2>/dev/null \
        && echo "workbench pipeline ACL set" \
        || echo "warn: pipeline ACL skipped — non-fatal"

    docker compose \
        -f platform/infra/docker-compose/docker-compose.yml \
        -f platform/infra/docker-compose/docker-compose.lxp.yml \
        up --build -d
    docker build --network=host -t examlops-jupyterlab \
        -f platform/infra/docker-compose/Dockerfile.jupyterlab \
        platform/infra/docker-compose >/dev/null 2>&1 \
        && echo "jupyterlab image built" \
        || echo "warn: jupyterlab image build skipped"
    docker compose \
        -f platform/infra/docker-compose/docker-compose.yml \
        -f platform/infra/docker-compose/docker-compose.lxp.yml \
        --profile jupyter up -d --build jupyterhub >/dev/null 2>&1 \
        && echo "jupyterhub up" \
        || echo "warn: jupyterhub start skipped"
    docker compose \
        -f platform/infra/docker-compose/docker-compose.yml \
        -f platform/infra/docker-compose/docker-compose.lxp.yml \
        --profile seanerbus up -d seanerbus-bridge >/dev/null 2>&1 \
        && echo "seanerbus bridge up" \
        || echo "warn: seanerbus bridge start skipped"

    ln -sfn "$release_path" "$CURRENT_LINK"
    echo "active release: $release_path"
}

seed_state
case "$ACTION" in
    deploy)
        SHA="${3:?release SHA is required}"
        ARCHIVE="${4:?release archive is required}"
        RELEASE_PATH="$(prepare_release "$SHA" "$ARCHIVE")"
        activate_release "$RELEASE_PATH"
        ;;
    activate)
        activate_release "${3:?release path is required}"
        ;;
    *)
        echo "unknown action: $ACTION" >&2
        exit 2
        ;;
esac

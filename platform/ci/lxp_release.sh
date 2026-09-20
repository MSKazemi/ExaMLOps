#!/usr/bin/env bash
# Deploy or reactivate an immutable ExaMLOps release on the LXP node.
#
# The legacy checkout is intentionally never cleaned or reset. It is used once to seed
# persistent state, while application source comes from a GitLab-created archive whose
# directory is named after the tested commit.
set -euo pipefail

ACTION="${1:?usage: $0 <deploy|activate|list> <legacy-path> [<sha-or-release>] [archive]}"
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
    local sha="$1" archive="$2"
    local release_path="$RELEASE_ROOT/$sha"
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

    # Pin this release to the images it was built with. Written into the release directory,
    # not read from the environment at activation, because `activate` is also the rollback
    # path: reactivating release X must run X's images. Reading the current pipeline's tag
    # there would roll the code back and leave the containers on the bad build.
    if [ -n "${EXAMLOPS_IMAGE_PREFIX:-}" ]; then
        {
            printf 'EXAMLOPS_IMAGE_PREFIX=%s\n' "$EXAMLOPS_IMAGE_PREFIX"
            printf 'EXAMLOPS_IMAGE_TAG=%s\n' "${EXAMLOPS_IMAGE_TAG:-$sha}"
        } > "$release_path/.examlops-image.env"
    fi

    copy_once "$STATE_ROOT/root.env" "$release_path/.env"
    copy_once "$STATE_ROOT/compose.env" \
        "$release_path/platform/infra/docker-compose/.env"
    copy_once "$STATE_ROOT/compose.env.dashboard" \
        "$release_path/platform/infra/docker-compose/.env.dashboard"
    copy_once "$STATE_ROOT/modelzoo" "$release_path/modelzoo"
    printf '%s\n' "$release_path"
}

# Releases are immutable directories named after a commit, and nothing ever removed them.
# On an NFS share that is a slow-motion outage: the deploy that finally fills the volume is
# the one that fails, long after the commits that consumed it. Keeping a fixed number of
# recent releases bounds it without giving up the thing release directories are FOR — being
# able to activate a known-good older tree.
#
# Two directories are never candidates, whatever the count says:
#   • the currently active release (deleting it removes the running code), and
#   • the one $CURRENT_LINK pointed at before this activation, because that is exactly what
#     smoke:lxp rolls back to when the health gate fails.
prune_releases() {
    local keep="${EXAMLOPS_KEEP_RELEASES:-5}" protected_1="$1" protected_2="${2:-}"
    case "$keep" in
        ''|*[!0-9]*) echo "warn: EXAMLOPS_KEEP_RELEASES=$keep is not a number — skipping prune" >&2; return 0 ;;
    esac
    [ "$keep" -ge 1 ] || { echo "warn: refusing to keep fewer than 1 release" >&2; return 0; }
    [ -d "$RELEASE_ROOT" ] || return 0

    # Newest first by mtime. `ls -1dt` is safe here: release directory names are validated
    # 40-char hex, so they contain no whitespace or newlines.
    # Protected releases COUNT toward the budget rather than being exempt from it. Skipping
    # them in the tally made EXAMLOPS_KEEP_RELEASES=3 leave five directories on disk, which
    # is the wrong answer to give someone who set a number to bound a volume. They are still
    # never deleted — if they fall outside the budget the budget is simply not met, and the
    # message says so rather than pretending it was.
    local seen=0 dir
    for dir in $(ls -1dt "$RELEASE_ROOT"/* 2>/dev/null); do
        [ -d "$dir" ] || continue
        seen=$((seen + 1))
        [ "$seen" -le "$keep" ] && continue
        case "$dir" in
            "$protected_1"|"$protected_2")
                echo "keeping $dir beyond the retention budget: it is the active or rollback release"
                continue
                ;;
        esac
        echo "pruning old release: $dir"
        rm -rf -- "$dir"
    done
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

    # Captured before anything moves the symlink; prune_releases protects it.
    PREVIOUS_RELEASE=""
    if [ -L "$CURRENT_LINK" ]; then
        PREVIOUS_RELEASE="$(readlink -f -- "$CURRENT_LINK" 2>/dev/null || true)"
    fi

    export EXAMLOPS_HOST_REPO="$release_path"
    export EXAMLOPS_STATE_DIR="$STATE_ROOT"
    export PLATFORM_DB="$STATE_ROOT/platform.db"
    export EXAMLOPS_PROVIDERS_DIR="$STATE_ROOT/.providers"
    export PATH="$HOME/.local/bin:/localhome/${USER}/.local/bin:$PATH"

    cd "$release_path"

    # Registry mode is a property of the release, not of the caller — so the caller's
    # environment is discarded here before the release's own pin is read. Without the unset,
    # rolling back to a release created BEFORE the registry existed inherited the current
    # pipeline's prefix and tried to pull images that were never pushed for it: the rollback
    # path, which is the one that runs when the platform is already unwell, was the one that
    # broke. `activate` must reproduce the release exactly as it was deployed, nothing else.
    unset EXAMLOPS_IMAGE_PREFIX EXAMLOPS_IMAGE_TAG
    if [ -f "$release_path/.examlops-image.env" ]; then
        # shellcheck disable=SC1091 — generated at deploy time, two KEY=value lines
        . "$release_path/.examlops-image.env"
        export EXAMLOPS_IMAGE_PREFIX EXAMLOPS_IMAGE_TAG
        echo "release images: ${EXAMLOPS_IMAGE_PREFIX}-*:${EXAMLOPS_IMAGE_TAG}"
    fi

    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.12 .venv >/dev/null \
            && uv pip install -e . >/dev/null \
            && echo "exa CLI refreshed: $(.venv/bin/exa --version 2>/dev/null)" \
            || echo "warn: exa CLI refresh failed — non-fatal"
    else
        echo "warn: exa CLI refresh skipped (uv unavailable) — non-fatal"
    fi
    setfacl -R -m u:1000:rwX -m d:u:1000:rwX usecases pipelines 2>/dev/null \
        && echo "workbench pipeline ACL set" \
        || echo "warn: pipeline ACL skipped — non-fatal"

    COMPOSE=(docker compose
        -f platform/infra/docker-compose/docker-compose.yml
        -f platform/infra/docker-compose/docker-compose.lxp.yml)

    if [ -n "${EXAMLOPS_IMAGE_PREFIX:-}" ]; then
        # Prebuilt images: pull, then start without building. A failed pull must stop the
        # deploy — silently falling back to a local build is how you end up running an
        # image nobody tested while the pipeline reports a registry deploy.
        "${COMPOSE[@]}" pull --quiet
        "${COMPOSE[@]}" up -d --no-build
    else
        "${COMPOSE[@]}" up --build -d
    fi
    docker build --network=host -t examlops-jupyterlab \
        -f platform/infra/docker-compose/Dockerfile.jupyterlab \
        platform/infra/docker-compose >/dev/null 2>&1 \
        && echo "jupyterlab image built" \
        || echo "warn: jupyterlab image build skipped"
    # jupyterhub is profile-gated, so the pull/up above never touched it. In registry mode
    # it must pull rather than build, like everything else — otherwise one service in the
    # stack is still built on the node and the deploy is half-pinned.
    if [ -n "${EXAMLOPS_IMAGE_PREFIX:-}" ]; then
        JUPYTER_START=(--profile jupyter up -d --no-build jupyterhub)
        "${COMPOSE[@]}" --profile jupyter pull --quiet jupyterhub >/dev/null 2>&1 || true
    else
        JUPYTER_START=(--profile jupyter up -d --build jupyterhub)
    fi
    "${COMPOSE[@]}" "${JUPYTER_START[@]}" >/dev/null 2>&1 \
        && echo "jupyterhub up" \
        || echo "warn: jupyterhub start skipped"
    # The bridge is the one service that is never prebuilt: its build context is the parent
    # of this repo (it needs the sibling dataplane-bus checkout), which no CI clone has. It
    # builds on the node in both modes, and compose tags it under the release's image name.
    "${COMPOSE[@]}" --profile dataplane-bus up -d --build dataplane-bus-bridge >/dev/null 2>&1 \
        && echo "dataplane-bus bridge up" \
        || echo "warn: dataplane-bus bridge start skipped"

    # Read the outgoing release BEFORE moving the symlink, so it can be protected from the
    # prune as the rollback target it is about to become.
    ln -sfn "$release_path" "$CURRENT_LINK"
    echo "active release: $release_path"

    # Put this production change into the platform's own hash-chained audit log, so that
    # `exa audit` can correlate "the release changed" with everything else it records.
    # Non-fatal by construction — see record_deploy.py: a missing audit row is a gap, an
    # aborted deploy is an outage.
    RECORDER="$release_path/platform/ci/record_deploy.py"
    if [ -f "$RECORDER" ]; then
        PYBIN="$release_path/.venv/bin/python"
        [ -x "$PYBIN" ] || PYBIN="$(command -v python3 || true)"
        if [ -n "$PYBIN" ]; then
            "$PYBIN" "$RECORDER" \
                --action "${RELEASE_ACTION:-deploy}" \
                --release "$release_path" \
                --sha "$(cat "$release_path/.examlops-release-sha" 2>/dev/null || true)" \
                --image-tag "${EXAMLOPS_IMAGE_TAG:-}" \
                --actor "${EXAMLOPS_ACTOR:-ci}" || true
        fi
    fi

    prune_releases "$release_path" "$PREVIOUS_RELEASE"
}

list_releases() {
    # stdout is parsed by rollback:lxp to pick a target, so it carries release lines and
    # nothing else. A human-readable "none" on stdout was being read as a release named
    # `no`, which the job then tried to activate. Diagnostics go to stderr.
    [ -d "$RELEASE_ROOT" ] || { echo "no releases yet" >&2; return 0; }
    local current="" dir marker
    [ -L "$CURRENT_LINK" ] && current="$(readlink -f -- "$CURRENT_LINK" 2>/dev/null || true)"
    for dir in $(ls -1dt "$RELEASE_ROOT"/* 2>/dev/null); do
        [ -d "$dir" ] || continue
        marker="  "
        [ "$dir" = "$current" ] && marker="* "
        printf '%s%s  %s\n' "$marker" "$(basename -- "$dir")" \
            "$(date -r "$dir" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '?')"
    done
}

# `list` is read-only, so it must not run seed_state — that copies files into the state root
# and would be a surprising side effect of asking a question.
if [ "$ACTION" = "list" ]; then
    list_releases
    exit 0
fi

seed_state
case "$ACTION" in
    deploy)
        RELEASE_ACTION="deploy"
        SHA="${3:?release SHA is required}"
        ARCHIVE="${4:?release archive is required}"
        RELEASE_PATH="$(prepare_release "$SHA" "$ARCHIVE")"
        activate_release "$RELEASE_PATH"
        ;;
    activate)
        # The only caller of `activate` is a rollback: smoke:lxp's automatic one, or the
        # manual rollback:lxp job. Labelling it as such is what keeps the audit log from
        # claiming the bad release is still running.
        RELEASE_ACTION="rollback"
        activate_release "${3:?release path is required}"
        ;;
    *)
        echo "unknown action: $ACTION (expected deploy, activate or list)" >&2
        exit 2
        ;;
esac

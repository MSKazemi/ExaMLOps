#!/usr/bin/env bash
# Build and push ONE ExaMLOps service image from the compose file's own build definition.
#
# Why this exists rather than a hand-written list of docker build commands in .gitlab-ci.yml:
# the compose file already states, for every service, its build context and Dockerfile. A
# second copy of that mapping in CI is a copy that goes stale silently — the image keeps
# building from the wrong context and nothing says so. So the CI matrix names only the
# *service*, and this script asks compose where its source is.
#
# Usage:  build_image.sh <service> <image-ref> [<extra-tag> ...]
#
# The caller is responsible for `docker login`. Requires `docker buildx`.
set -euo pipefail

SERVICE="${1:?usage: $0 <service> <image-ref> [extra-tag ...]}"
IMAGE_REF="${2:?target image reference (registry/name:tag) is required}"
shift 2

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_DIR="$REPO_ROOT/platform/infra/docker-compose"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"

# `docker compose config` resolves context/dockerfile to absolute paths and applies the
# same variable interpolation the deploy will, so what we build is what compose would build.
# Assigned via command substitution, NOT `read < <(...)`: process substitution discards the
# producer's exit status, so a mistyped service name resolved to nothing and the script went
# on to report success having built no image. A plain assignment lets `set -e` see the failure.
BUILD_SPEC="$(
    docker compose -f "$COMPOSE_FILE" --profile '*' config --format json \
        | python3 -c '
import json, sys
svc = sys.argv[1]
services = json.load(sys.stdin).get("services", {})
if svc not in services:
    sys.exit(f"service {svc!r} is not in the compose file")
build = services[svc].get("build")
if not build:
    sys.exit(f"service {svc!r} has no build: section — it is an upstream image, not ours")
print(build["context"], build.get("dockerfile", "Dockerfile"))
' "$SERVICE"
)"
read -r CONTEXT DOCKERFILE <<<"$BUILD_SPEC"

# compose reports `dockerfile` relative to the build context; buildx resolves --file
# against the *current* directory. Joining them here is what keeps the repo-root-context
# services (agent, dashboard, ray-serving, control-plane, backup) buildable.
case "$DOCKERFILE" in
    /*) ;;
    *) DOCKERFILE="$CONTEXT/$DOCKERFILE" ;;
esac

echo "==> $SERVICE"
echo "    context:    $CONTEXT"
echo "    dockerfile: $DOCKERFILE"
echo "    image:      $IMAGE_REF"

TAG_ARGS=(--tag "$IMAGE_REF")
for extra in "$@"; do
    TAG_ARGS+=(--tag "$extra")
    echo "    also:       $extra"
done

# Registry-backed layer cache: the cache lives beside the image, so a runner with a cold
# local disk still reuses layers. `mode=max` caches intermediate stages too, which is what
# makes the multi-stage dashboard/frontend build cheap on the second run.
CACHE_REF="${IMAGE_REF%%:*}:buildcache"

docker buildx build \
    --file "$DOCKERFILE" \
    "${TAG_ARGS[@]}" \
    --cache-from "type=registry,ref=$CACHE_REF" \
    --cache-to "type=registry,ref=$CACHE_REF,mode=max" \
    --label "org.opencontainers.image.source=${CI_PROJECT_URL:-}" \
    --label "org.opencontainers.image.revision=${CI_COMMIT_SHA:-}" \
    --label "org.opencontainers.image.version=${CI_COMMIT_TAG:-${CI_COMMIT_SHA:-}}" \
    --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --provenance=false \
    --push \
    "$CONTEXT"

# The digest is the only identifier that survives a tag being moved. Everything downstream
# (the mirror copy, the deploy pin) uses it rather than the tag it was pushed under.
DIGEST="$(docker buildx imagetools inspect "$IMAGE_REF" --format '{{.Manifest.Digest}}')"
echo "    digest:     $DIGEST"
printf '%s %s@%s\n' "$SERVICE" "${IMAGE_REF%%:*}" "$DIGEST"

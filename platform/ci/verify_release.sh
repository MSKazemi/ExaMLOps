#!/usr/bin/env bash
# Verify a PUBLISHED release the way someone downloading it would (ADR 0129).
#
# release.yml proves each artifact while it builds it. This proves what actually reached
# people: the GitHub Release assets, the GHCR images and the OCI chart, fetched back from where
# they were published. The first published releases had three defects visible only from here, all
# in v0.54.0: its provenance attested `dist/.gitignore`, its dashboard image could not import
# `examlops`, and its Compose bundle's MLflow was OOM-killed on every start. So every release is
# checked this way (.github/workflows/release-verify.yml).
#
# Usage:  platform/ci/verify_release.sh vX.Y.Z
# Needs:  gh, cosign (v3), oras, helm, docker, python3, sha256sum, curl.
# Env:    VERIFY_COMPOSE=0   skip starting the Compose bundle (it pulls every image, ~6 GB)
#         VERIFY_OFFLINE=0   skip the air-gapped phase (docs/guides/air-gapped-install.md)
#         VERIFY_POLL_SECONDS  seconds between Compose health polls (default 5)
#         VERIFY_WORKDIR     where to download (default: a fresh temporary directory)
#
# Every check is counted: one failure makes the exit status non-zero, and a check that cannot
# run is a failure, never a skip.

set -uo pipefail

TAG=${1:?usage: verify_release.sh vX.Y.Z}
if ! [[ $TAG =~ ^v?([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?)$ ]]; then
  echo "not a release tag: $TAG" >&2
  exit 2
fi
VERSION=${BASH_REMATCH[1]}
TAG=v$VERSION

REPO=MSKazemi/ExaMLOps
NAMESPACE=ghcr.io/mskazemi
CHART=oci://ghcr.io/mskazemi/charts/examlops
ISSUER=https://token.actions.githubusercontent.com
IDENTITY='^https://github\.com/MSKazemi/ExaMLOps/\.github/workflows/release\.yml@refs/(tags/v.*|heads/main)$'
# The air-gapped phase runs these inside a network with no route out, so they are images.
COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign:v3.1.3@sha256:9e5c2f2edc34351160407ca3416c61855bdf9403c3c5936e0f0be7fc261611b8
REGISTRY_IMAGE=registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373

WORK=${VERIFY_WORKDIR:-$(mktemp -d)}
mkdir -p "$WORK"
cd "$WORK" || exit 2
LOG=$WORK/checks.log
: >"$LOG"
PASSED=0
FAILED=()

# check NAME COMMAND... — run one check, record the verdict, keep its output for a failure.
check() {
  local name=$1
  shift
  if out=$("$@" 2>&1); then
    PASSED=$((PASSED + 1))
    printf 'PASS  %s\n' "$name"
  else
    FAILED+=("$name")
    printf 'FAIL  %s\n' "$name"
    # The cause is at the top for some tools (an unknown flag) and at the bottom for others.
    printf '%s\n' "$out" | awk '{l[NR]=$0} END {for (i=1;i<=NR;i++) if (NR<=6 || i<=3 || i>NR-3) print "      " l[i]; else if (i==4) print "      …"}'
    [ -n "${GITHUB_ACTIONS:-}" ] && printf '::error title=%s::%s\n' "$name" "$(printf '%s' "$out" | tail -1)"
  fi
  printf '== %s\n%s\n' "$name" "$out" >>"$LOG"
}

sum_of() { awk -v f="$1" '$2 == f || $2 == "*"f {print $1}' SHA256SUMS; }

# A missing or too-old tool would otherwise surface as a dozen unrelated-looking failures.
tools() {
  local missing=()
  for t in gh cosign oras helm docker python3 sha256sum curl; do
    command -v "$t" >/dev/null || missing+=("$t")
  done
  [ ${#missing[@]} -eq 0 ] || { echo "not installed: ${missing[*]}"; return 1; }
  gh attestation verify --help >/dev/null 2>&1 || { echo "$(gh --version | head -1) has no 'attestation' (needs >= 2.49)"; return 1; }
  cosign version 2>&1 | grep -q 'GitVersion: *v3\.' || { echo "cosign v3 required"; return 1; }
}
check "the verification tools are present" tools

# ── 1. Release assets ─────────────────────────────────────────────────────────────────────────
WHEEL=examlops-$VERSION-py3-none-any.whl
SDIST=examlops-$VERSION.tar.gz
CHART_TGZ=examlops-$VERSION.tgz
IMAGES=images-$VERSION.txt
REQUIRED=("$WHEEL" "$SDIST" "$CHART_TGZ" "examlops-compose-$VERSION.tar.gz" "$IMAGES"
  "examlops-$VERSION.cdx.json" "examlops-$VERSION.intoto.jsonl" SHA256SUMS SHA256SUMS.sigstore.json)

check "download the release assets" gh release download "$TAG" --repo "$REPO" --clobber

assets_present() {
  local missing=()
  for f in "${REQUIRED[@]}"; do [ -s "$f" ] || missing+=("$f"); done
  [ ${#missing[@]} -eq 0 ] || { echo "missing: ${missing[*]}"; return 1; }
}
check "every expected asset is published" assets_present

check "SHA256SUMS is signed by the release workflow" \
  cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$IDENTITY"

check "every file matches SHA256SUMS" sha256sum -c --strict SHA256SUMS

every_asset_is_summed() {
  # An empty download has no unsummed file either; that must not read as a pass.
  [ -s SHA256SUMS ] || { echo "no SHA256SUMS to check against"; return 1; }
  local unsummed=() checked=0
  for f in *; do
    case $f in SHA256SUMS | SHA256SUMS.sigstore.json | checks.log | venv | chart | airgap) continue ;; esac
    [ -f "$f" ] || continue
    checked=$((checked + 1))
    [ -n "$(sum_of "$f")" ] || unsummed+=("$f")
  done
  [ "$checked" -gt 0 ] || { echo "no assets were downloaded"; return 1; }
  [ ${#unsummed[@]} -eq 0 ] || { echo "published but not covered by the signature: ${unsummed[*]}"; return 1; }
}
check "no asset escapes the signed checksums" every_asset_is_summed

# ── 2. Python distributions ───────────────────────────────────────────────────────────────────
provenance_subjects() {
  python3 - "examlops-$VERSION.intoto.jsonl" "$WHEEL" "$(sum_of "$WHEEL")" "$SDIST" "$(sum_of "$SDIST")" <<'EOF'
import base64, json, sys
path, wheel, wheel_sum, sdist, sdist_sum = sys.argv[1:]
subjects = {}
for line in open(path):
    statement = json.loads(base64.b64decode(json.loads(line)["payload"]))
    assert statement["predicateType"] == "https://slsa.dev/provenance/v1", statement["predicateType"]
    subjects.update({s["name"]: s["digest"]["sha256"] for s in statement["subject"]})
expected = {wheel: wheel_sum, sdist: sdist_sum}
assert subjects == expected, f"provenance subjects {subjects} != {expected}"
print("subjects:", sorted(subjects))
EOF
}
check "provenance attests exactly the wheel and the sdist" provenance_subjects
check "wheel provenance verifies (gh attestation)" gh attestation verify "$WHEEL" --repo "$REPO"
check "sdist provenance verifies (gh attestation)" gh attestation verify "$SDIST" --repo "$REPO"

wheel_installs() {
  python3 -m venv venv && venv/bin/pip install --quiet "./$WHEEL" && venv/bin/pip check \
    && [ "$(NO_COLOR=1 venv/bin/exa --version)" = "exa version $VERSION" ]
}
check "the wheel installs and reports its version" wheel_installs

# ── 3. Images ─────────────────────────────────────────────────────────────────────────────────
mapfile -t REFS < <(grep -v '^$' "$IMAGES" 2>/dev/null)
image_list_is_well_formed() {
  [ ${#REFS[@]} -gt 0 ] || { echo "empty $IMAGES"; return 1; }
  local bad=()
  for ref in "${REFS[@]}"; do
    [[ $ref =~ ^$NAMESPACE/examlops-[a-z0-9-]+:$VERSION@sha256:[0-9a-f]{64}$ ]] || bad+=("$ref")
  done
  [ ${#bad[@]} -eq 0 ] || { echo "malformed: ${bad[*]}"; return 1; }
}
check "the image list names every image at $VERSION by digest" image_list_is_well_formed

tag_is_digest() { [ "$(oras resolve "$1")" = "$2" ] || { echo "$1 resolves to $(oras resolve "$1"), not $2"; return 1; }; }
for ref in "${REFS[@]}"; do
  repo=${ref%%:*}; digest=${ref#*@}; short=${repo##*/}
  check "$short: tag $VERSION is the listed digest" tag_is_digest "$repo:$VERSION" "$digest"
  check "$short: signed by the release workflow" \
    cosign verify "$repo@$digest" --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$IDENTITY"
  check "$short: provenance verifies (gh attestation)" \
    gh attestation verify "oci://$repo@$digest" --repo "$REPO"
done

dashboard_runs_examlops() {
  local ref
  ref=$(grep -m1 '/examlops-dashboard:' "$IMAGES") || { echo "no dashboard image listed"; return 1; }
  ref=${ref%%:*}@${ref#*@}
  docker run --rm --network none --entrypoint python "$ref" -c \
    "import importlib.metadata as m, examlops; v = m.version('examlops'); assert v == '$VERSION', v; print('examlops', v)"
}
check "the dashboard image imports examlops $VERSION" dashboard_runs_examlops

# ── 4. Chart ──────────────────────────────────────────────────────────────────────────────────
check "chart: signed by the release workflow" \
  cosign verify "${CHART#oci://}:$VERSION" --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$IDENTITY"

chart_matches_release() {
  rm -rf chart && mkdir chart && helm pull "$CHART" --version "$VERSION" -d chart >/dev/null || return 1
  local got
  got=$(sha256sum "chart/$CHART_TGZ" | cut -d' ' -f1)
  [ "$got" = "$(sum_of "$CHART_TGZ")" ] || { echo "registry chart $got != SHA256SUMS"; return 1; }
  local rendered missing=()
  rendered=$(helm template x "chart/$CHART_TGZ" --set "global.imageRegistry=$NAMESPACE/" \
    --set existingSecret=x --set agent.enabled=true) || return 1
  while read -r img; do
    grep -q "^$img@" "$IMAGES" || missing+=("$img")
  done < <(printf '%s\n' "$rendered" | sed -nE 's/^\s*image:\s*"?([^" ]+)"?.*/\1/p' | sort -u)
  [ ${#missing[@]} -eq 0 ] || { echo "chart renders images not in the release: ${missing[*]}"; return 1; }
}
check "chart: registry copy equals the signed asset and renders only released images" chart_matches_release

# ── 5. The Compose bundle starts healthy ──────────────────────────────────────────────────────
# Nothing else starts the stack a release ships: the v0.54.0 bundle's MLflow was OOM-killed on
# every start and no build noticed. Its own project name and ports, so it can share a host with a
# running install.
if [ "${VERIFY_COMPOSE:-1}" = 1 ]; then
  BUNDLE_DIR=$WORK/compose/examlops-compose-$VERSION
  PROJECT=examlops-verify-$$
  compose() { (cd "$BUNDLE_DIR" && docker compose "$@"); }
  compose_down() { [ -d "$BUNDLE_DIR" ] && compose down -v --remove-orphans >/dev/null 2>&1; }
  set_env() { sed -i "/^$1=/d" "$BUNDLE_DIR/.env" && echo "$1=$2" >>"$BUNDLE_DIR/.env"; }

  compose_starts_healthy() {
    rm -rf "$WORK/compose" && mkdir -p "$WORK/compose" \
      && tar -xzf "examlops-compose-$VERSION.tar.gz" -C "$WORK/compose" || return 1
    (cd "$BUNDLE_DIR" && ./install.sh init >/dev/null) || { echo "install.sh init failed"; return 1; }
    set_env EXAMLOPS_PROJECT_NAME "$PROJECT"
    set_env COMPOSE_PROFILES minio,monitoring
    local port=$((42000 + RANDOM % 500 * 20)) var
    while read -r var; do
      set_env "$var" "$port"; port=$((port + 1))
    done < <(grep -oE '^EXAMLOPS_PORT_[A-Z_]+' "$BUNDLE_DIR/env.template")
    compose pull -q || { echo "pull failed"; return 1; }
    # `up -d` fails when a dependency never turns healthy; keep its reason and still report the
    # state of every service below.
    local up_out up_rc=0 states bad
    up_out=$(compose up -d 2>&1) || up_rc=$?
    # `|`-separated: a service without a health check has an empty Health field.
    local looping=0
    for _ in $(seq 1 90); do  # up to 7.5 minutes for every health check to settle
      states=$(compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}')
      grep -qE '\|(created|restarting)\||\|starting\|' <<<"$states" || break
      # A service seen restarting on 3 polls is crash-looping; waiting longer only burns the host.
      if grep -q '|restarting|' <<<"$states"; then looping=$((looping + 1)); fi
      [ "$looping" -lt 3 ] || break
      sleep "${VERIFY_POLL_SECONDS:-5}"
    done
    # Unhealthy, still starting, never started, crash-looping, or exited with an error.
    bad=$(awk -F'|' '$3 == "unhealthy" || $3 == "starting" || $2 == "created" \
      || $2 == "restarting" || ($2 == "exited" && $4 != "0")' <<<"$states")
    # Most telling first: a failure excerpt shows the head and the tail of this output.
    [ "$up_rc" = 0 ] || printf 'docker compose up failed: %s\n' "$(tail -1 <<<"$up_out")"
    [ -z "$bad" ] || { echo "not healthy: $(cut -d'|' -f1,2,3 <<<"$bad" | tr '\n' ' ')"; }
    echo "$states"
    [ -z "$bad" ] && [ "$up_rc" = 0 ]
  }
  trap compose_down EXIT
  check "compose: the published bundle starts, every health check passes" compose_starts_healthy
  compose_down
fi

# ── 6. Air-gapped: mirror, then verify with no route out ──────────────────────────────────────
if [ "${VERIFY_OFFLINE:-1}" = 1 ]; then
  NET=examlops-verify-$$
  REG=examlops-verify-registry-$$
  cleanup() {
    type compose_down >/dev/null 2>&1 && compose_down
    docker rm -f "$REG" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1
  }
  trap cleanup EXIT

  mirror() {
    docker run -d --name "$REG" -p 127.0.0.1::5000 "$REGISTRY_IMAGE" >/dev/null || return 1
    local port ref repo digest
    port=$(docker port "$REG" 5000/tcp | head -1 | sed 's/.*://')
    for _ in $(seq 1 20); do curl -fs "http://127.0.0.1:$port/v2/" >/dev/null && break; sleep 1; done
    for ref in "${REFS[@]}"; do
      repo=${ref%%:*}; digest=${ref#*@}
      oras cp -r --to-plain-http "$repo@$digest" "127.0.0.1:$port/examlops/${repo##*/}:$VERSION" >/dev/null || return 1
      [ "$(oras resolve --plain-http "127.0.0.1:$port/examlops/${repo##*/}:$VERSION")" = "$digest" ] || return 1
    done
    oras cp -r --to-plain-http "${CHART#oci://}:$VERSION" "127.0.0.1:$port/charts/examlops:$VERSION" >/dev/null
  }
  check "mirror: every image and the chart copy with their signatures" mirror

  trusted_root() {
    mkdir -p airgap/tuf && docker run --rm -u "$(id -u):$(id -g)" -e HOME=/h -v "$WORK/airgap/tuf:/h" \
      "$COSIGN_IMAGE" initialize >/dev/null \
      && cp airgap/tuf/.sigstore/root/tuf-repo-cdn.sigstore.dev/targets/trusted_root.json airgap/
  }
  check "mirror: Sigstore trusted root fetched through TUF" trusted_root

  isolate() { docker network create --internal "$NET" >/dev/null && docker network connect "$NET" "$REG"; }
  check "air gap: an internal network holding only the mirror" isolate
  gap() { docker run --rm --network "$NET" -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$WORK:/w" -w /w "$@"; }
  # Fetching the trusted root needs Sigstore's TUF CDN; if that works in here, it is no air gap.
  no_route_out() { ! gap "$COSIGN_IMAGE" initialize; }
  check "air gap: nothing inside can reach the internet" no_route_out

  OFFLINE=(--offline --trusted-root /w/airgap/trusted_root.json)
  check "air gap: SHA256SUMS signature verifies offline" \
    gap "$COSIGN_IMAGE" verify-blob "${OFFLINE[@]}" SHA256SUMS --bundle SHA256SUMS.sigstore.json \
    --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$IDENTITY"
  REGISTRY_FLAGS=(--allow-http-registry --allow-insecure-registry)
  for ref in "${REFS[@]}" "chart"; do
    if [ "$ref" = chart ]; then target=$REG:5000/charts/examlops:$VERSION; short=chart
    else repo=${ref%%:*}; short=${repo##*/}; target=$REG:5000/examlops/$short:$VERSION; fi
    check "air gap: $short signature verifies offline from the mirror" \
      gap "$COSIGN_IMAGE" verify "${OFFLINE[@]}" "${REGISTRY_FLAGS[@]}" "$target" \
      --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$IDENTITY"
  done
  check "air gap: SLSA provenance verifies offline from the mirror" \
    gap "$COSIGN_IMAGE" verify-attestation "${OFFLINE[@]}" "${REGISTRY_FLAGS[@]}" \
    --type https://slsa.dev/provenance/v1 "$REG:5000/examlops/examlops-control-plane:$VERSION" \
    --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp "$IDENTITY"
  wrong_identity_rejected() {
    ! gap "$COSIGN_IMAGE" verify "${OFFLINE[@]}" "${REGISTRY_FLAGS[@]}" \
      "$REG:5000/examlops/examlops-control-plane:$VERSION" \
      --certificate-oidc-issuer "$ISSUER" --certificate-identity-regexp '^https://github\.com/not-the-release/'
  }
  check "air gap: a signature from any other identity is rejected" wrong_identity_rejected
fi

# ── Verdict ───────────────────────────────────────────────────────────────────────────────────
TOTAL=$((PASSED + ${#FAILED[@]}))
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### $TAG — $PASSED of $TOTAL checks passed"
    for f in "${FAILED[@]}"; do echo "- ❌ $f"; done
  } >>"$GITHUB_STEP_SUMMARY"
fi
echo
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "$TAG: all $TOTAL checks passed"
else
  echo "$TAG: ${#FAILED[@]} of $TOTAL checks FAILED (details: $LOG)"
  exit 1
fi

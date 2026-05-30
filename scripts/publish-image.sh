#!/usr/bin/env bash
# publish-image.sh — build and push a registry-pullable pila image.
#
# Fly.io Machines pull an image from a registry rather than using a
# locally-built image with host-UID/GID build-args. This script produces
# a registry-tagged image without the HOST_UID/HOST_GID coupling that only
# makes sense for local bind-mounts.
#
# Two publish paths (choose one):
#
#   PATH A — build locally, push to fly's private registry:
#     ./scripts/publish-image.sh                       # tags registry.fly.io/<APP>:<VERSION>
#     ./scripts/publish-image.sh --push                # also runs: nerdctl push <tag>
#     ./scripts/publish-image.sh --app myapp --push    # override fly app name
#
#   PATH B — let fly build remotely (no local container runtime needed):
#     flyctl deploy --build-only --push \
#       --config fly.toml \
#       --dockerfile Dockerfile
#     # fly reads the Dockerfile directly, ignores HOST_UID/HOST_GID
#     # (ARG defaults 501/20 apply), and pushes to registry.fly.io/<app>.
#
# Why no HOST_UID/HOST_GID for registry images:
#   The local `pila` launcher passes --build-arg HOST_UID=$(id -u) so that
#   files written by the container into the bind-mounted /work keep the
#   host user's ownership. Fly.io Machines use a fly Volume or ephemeral
#   storage — there is no host bind-mount, so UID ownership matching is
#   irrelevant. The Dockerfile's defaults (UID=501, GID=20) are used as-is.
#
# Usage: ./scripts/publish-image.sh [OPTIONS]
#   --app NAME       fly.io app name (default: PILA_FLY_APP env or "pila")
#   --registry REG   registry prefix (default: registry.fly.io/<APP>)
#   --tag TAG        override the full image tag (default: <REGISTRY>:<VERSION>)
#   --push           push after building (requires nerdctl or docker login)
#   --dry-run        print commands without executing
#   --help           show this message
set -euo pipefail

# --- resolve script location ---------------------------------------------
SRC="${BASH_SOURCE[0]}"
hops=0
while [ -L "$SRC" ]; do
  hops=$((hops + 1))
  if [ "$hops" -gt 20 ]; then
    echo "publish-image: refusing to resolve symlink chain deeper than 20 hops" >&2
    exit 1
  fi
  TARGET="$(readlink "$SRC")"
  case "$TARGET" in
    /*) SRC="$TARGET" ;;
    *)  SRC="$(cd -P "$(dirname "$SRC")" && pwd)/$TARGET" ;;
  esac
done
PILA_REPO="$(cd -P "$(dirname "$SRC")/.." && pwd)"

# --- version (single source of truth: .claude-plugin/plugin.json) --------
PILA_VERSION="$(awk -F'"' '/"version"/ {print $4; exit}' \
                  "$PILA_REPO/.claude-plugin/plugin.json" 2>/dev/null || echo dev)"

# --- defaults -------------------------------------------------------------
FLY_APP="${PILA_FLY_APP:-pila}"
REGISTRY=""      # resolved below after --app is parsed
TAG_OVERRIDE=""
PUSH=false
DRY_RUN=false

# --- parse args ----------------------------------------------------------
while [ "$#" -gt 0 ]; do
  case "$1" in
    --app)
      shift; FLY_APP="$1" ;;
    --app=*)
      FLY_APP="${1#--app=}" ;;
    --registry)
      shift; REGISTRY="$1" ;;
    --registry=*)
      REGISTRY="${1#--registry=}" ;;
    --tag)
      shift; TAG_OVERRIDE="$1" ;;
    --tag=*)
      TAG_OVERRIDE="${1#--tag=}" ;;
    --push)
      PUSH=true ;;
    --dry-run)
      DRY_RUN=true ;;
    --help|-h)
      sed -n '/^# Usage:/,/^[^#]/{ /^#/{ s/^# \?//; p }; /^[^#]/q }' "$0"
      exit 0
      ;;
    *)
      echo "publish-image: unknown argument: $1" >&2
      exit 1 ;;
  esac
  shift
done

# Resolve registry and tag now that --app may have been overridden.
if [ -z "$REGISTRY" ]; then
  REGISTRY="registry.fly.io/$FLY_APP"
fi
if [ -z "$TAG_OVERRIDE" ]; then
  IMAGE_TAG="$REGISTRY:$PILA_VERSION"
else
  IMAGE_TAG="$TAG_OVERRIDE"
fi

# --- detect build tool ---------------------------------------------------
BUILD_CMD=""
if command -v nerdctl >/dev/null 2>&1; then
  BUILD_CMD="nerdctl"
elif command -v docker >/dev/null 2>&1; then
  BUILD_CMD="docker"
elif [ "$DRY_RUN" = "false" ]; then
  echo "publish-image: neither nerdctl nor docker found on PATH." >&2
  echo "  For PATH A (local build): install nerdctl (Linux) or Colima (macOS)." >&2
  echo "  For PATH B (remote build via fly): flyctl deploy --build-only --push" >&2
  exit 1
else
  BUILD_CMD="nerdctl"  # dry-run: assume nerdctl; commands are printed, not run
fi

# --- run (or print) -------------------------------------------------------
run() {
  if [ "$DRY_RUN" = "true" ]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

echo "[publish-image] pila version: $PILA_VERSION"
echo "[publish-image] image tag:    $IMAGE_TAG"
echo "[publish-image] build tool:   $BUILD_CMD"
echo "[publish-image] push:         $PUSH"
[ "$DRY_RUN" = "true" ] && echo "[publish-image] DRY RUN — commands printed, not executed"

# Build without HOST_UID/HOST_GID: the Dockerfile ARG defaults (501/20)
# apply. The image is suitable for fly.io Machines and any other
# registry-pull environment where UID bind-mount matching is not needed.
run "$BUILD_CMD" build \
  -t "$IMAGE_TAG" \
  "$PILA_REPO"

echo "[publish-image] build complete: $IMAGE_TAG"

# Verify the entrypoint matches the expected path.
if [ "$DRY_RUN" = "false" ]; then
  ENTRY="$("$BUILD_CMD" inspect "$IMAGE_TAG" \
            --format '{{join .Config.Entrypoint " "}}' 2>/dev/null || true)"
  EXPECTED="/work/.pila-image/scripts/container-entry.sh"
  if [ "$ENTRY" = "$EXPECTED" ]; then
    echo "[publish-image] entrypoint OK: $ENTRY"
  else
    echo "publish-image: WARNING — entrypoint mismatch" >&2
    echo "  expected: $EXPECTED" >&2
    echo "  got:      $ENTRY" >&2
  fi
fi

if [ "$PUSH" = "true" ]; then
  echo "[publish-image] pushing $IMAGE_TAG ..."
  run "$BUILD_CMD" push "$IMAGE_TAG"
  echo "[publish-image] pushed: $IMAGE_TAG"
  echo ""
  echo "To deploy on fly.io:"
  echo "  flyctl deploy --app $FLY_APP --image $IMAGE_TAG"
else
  echo ""
  echo "Next steps:"
  echo "  Push:   $BUILD_CMD push $IMAGE_TAG"
  echo "  Deploy: flyctl deploy --app $FLY_APP --image $IMAGE_TAG"
  echo ""
  echo "Or re-run with --push to push in one step:"
  echo "  ./scripts/publish-image.sh --app $FLY_APP --push"
fi

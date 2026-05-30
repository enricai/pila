#!/usr/bin/env bash
# scripts/remote/build-push.sh — build and push a self-contained pila image
# for Fly.io Machines (or any registry-pull environment).
#
# Unlike the local `pila` build (which bind-mounts $PILA_REPO at runtime),
# the image produced here has the orchestrator source baked in at
# /work/.pila-image/ via the Dockerfile's COPY instructions. A Fly Machine
# that pulls this image can run the orchestrator without any bind mount.
#
# Usage:
#   ./scripts/remote/build-push.sh [OPTIONS]
#
#   --app NAME       fly.io app name (default: PILA_FLY_APP env or "pila")
#   --registry REG   registry prefix (default: registry.fly.io/<APP>)
#   --tag TAG        override the full image tag (default: <REGISTRY>:<VERSION>)
#   --push           build and push in one step
#   --dry-run        print commands without executing
#   --help           show this message
#
# Two publish paths (choose one):
#
#   PATH A — build locally, push to fly's private registry:
#     ./scripts/remote/build-push.sh --app myapp --push
#
#   PATH B — let fly build remotely (no local container runtime needed):
#     flyctl deploy --build-only --push \
#       --config fly.toml \
#       --dockerfile Dockerfile
#     # fly reads the Dockerfile, COPY instructions bake the source,
#     # and the result is pushed to registry.fly.io/<app> automatically.
#
# Verification after push:
#   flyctl machine run registry.fly.io/<APP>:<VERSION> \
#     --app <APP> \
#     -- python3 /work/.pila-image/orchestrator/pila.py --version
#
# The --version fast path reads /work/.pila-image/.claude-plugin/plugin.json
# without starting the full orchestrator; success confirms the source is
# present at the expected path inside the image.
set -euo pipefail

# --- resolve script location ---------------------------------------------
SRC="${BASH_SOURCE[0]}"
hops=0
while [ -L "$SRC" ]; do
  hops=$((hops + 1))
  if [ "$hops" -gt 20 ]; then
    echo "build-push: refusing to resolve symlink chain deeper than 20 hops" >&2
    exit 1
  fi
  TARGET="$(readlink "$SRC")"
  case "$TARGET" in
    /*) SRC="$TARGET" ;;
    *)  SRC="$(cd -P "$(dirname "$SRC")" && pwd)/$TARGET" ;;
  esac
done
PILA_REPO="$(cd -P "$(dirname "$SRC")/../.." && pwd)"

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
      echo "build-push: unknown argument: $1" >&2
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
  echo "build-push: neither nerdctl nor docker found on PATH." >&2
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

echo "[build-push] pila version: $PILA_VERSION"
echo "[build-push] image tag:    $IMAGE_TAG"
echo "[build-push] build tool:   $BUILD_CMD"
echo "[build-push] push:         $PUSH"
[ "$DRY_RUN" = "true" ] && echo "[build-push] DRY RUN — commands printed, not executed"

# Build without HOST_UID/HOST_GID: the Dockerfile ARG defaults (501/20)
# apply. Source is baked in via COPY instructions — no bind mount required.
# The image is suitable for Fly.io Machines and any registry-pull environment.
run "$BUILD_CMD" build \
  -t "$IMAGE_TAG" \
  "$PILA_REPO"

echo "[build-push] build complete: $IMAGE_TAG"

# Verify the entrypoint and baked source after a real build.
if [ "$DRY_RUN" = "false" ]; then
  ENTRY="$("$BUILD_CMD" inspect "$IMAGE_TAG" \
            --format '{{join .Config.Entrypoint " "}}' 2>/dev/null || true)"
  EXPECTED="/work/.pila-image/scripts/container-entry.sh"
  if [ "$ENTRY" = "$EXPECTED" ]; then
    echo "[build-push] entrypoint OK: $ENTRY"
  else
    echo "build-push: WARNING — entrypoint mismatch" >&2
    echo "  expected: $EXPECTED" >&2
    echo "  got:      $ENTRY" >&2
  fi

  # Smoke: confirm the baked orchestrator responds to --version.
  # Runs the container with no bind mounts — exercises the baked source path.
  echo "[build-push] smoke: pila --version (baked source, no bind mount) ..."
  if run "$BUILD_CMD" run --rm "$IMAGE_TAG" \
       python3 /work/.pila-image/orchestrator/pila.py --version; then
    echo "[build-push] smoke OK"
  else
    echo "build-push: WARNING — --version smoke failed (baked source not working)" >&2
  fi
fi

if [ "$PUSH" = "true" ]; then
  echo "[build-push] pushing $IMAGE_TAG ..."
  run "$BUILD_CMD" push "$IMAGE_TAG"
  echo "[build-push] pushed: $IMAGE_TAG"
  echo ""
  echo "To start on Fly.io:"
  echo "  flyctl machine run $IMAGE_TAG --app $FLY_APP"
  echo ""
  echo "To verify inside the machine:"
  echo "  flyctl machine run $IMAGE_TAG --app $FLY_APP \\"
  echo "    -- python3 /work/.pila-image/orchestrator/pila.py --version"
else
  echo ""
  echo "Next steps:"
  echo "  Push:  $BUILD_CMD push $IMAGE_TAG"
  echo "  Or re-run with --push to push in one step:"
  echo "    ./scripts/remote/build-push.sh --app $FLY_APP --push"
  echo ""
  echo "After pushing, deploy on Fly.io:"
  echo "  flyctl machine run $IMAGE_TAG --app $FLY_APP"
fi

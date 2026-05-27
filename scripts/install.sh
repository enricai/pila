#!/usr/bin/env bash
# install.sh — one-command installer for Centella.
#
#   curl -fsSL https://raw.githubusercontent.com/enricai/centella/main/scripts/install.sh | bash
#
# What this does, in order:
#   1. Verifies `git` and `claude` are on PATH (Python is NOT a prereq — uv provisions it).
#   2. Installs `uv` (https://docs.astral.sh/uv/) if missing, via Astral's official installer.
#   3. Provisions a hermetic Python 3.12 via `uv python install 3.12`.
#   4. Clones (or fast-forwards) enricai/centella into $CENTELLA_HOME (default ~/.centella).
#   5. Symlinks $CENTELLA_HOME/centella into ~/.local/bin/centella.
#   6. Verifies the install with `centella --version`.
#
# Flags:
#   --dry-run        Print actions without executing.
#   --prefix DIR     Install Centella under DIR (default: $CENTELLA_HOME or ~/.centella).
#   --bin-dir DIR    Symlink dir (default: ~/.local/bin).
#   --ref REF        Git ref to install (default: main).
#   --help           Show this message and exit.
#
# Env vars:
#   CENTELLA_HOME      Install directory (default ~/.centella). --prefix overrides.
#   CENTELLA_BIN_DIR   Symlink directory (default ~/.local/bin). --bin-dir overrides.
#   CENTELLA_REPO_URL  Repo URL to clone (default https://github.com/enricai/centella.git).
set -euo pipefail

# --- defaults ------------------------------------------------------------

DEFAULT_REPO_URL="https://github.com/enricai/centella.git"
DEFAULT_REF="main"
DEFAULT_PYTHON="3.12"

PREFIX="${CENTELLA_HOME:-$HOME/.centella}"
BIN_DIR="${CENTELLA_BIN_DIR:-$HOME/.local/bin}"
REPO_URL="${CENTELLA_REPO_URL:-$DEFAULT_REPO_URL}"
REF="$DEFAULT_REF"
DRY_RUN=false

# --- helpers -------------------------------------------------------------

usage() {
  cat <<'EOF'
install.sh — one-command installer for Centella.

  curl -fsSL https://raw.githubusercontent.com/enricai/centella/main/scripts/install.sh | bash

What this does, in order:
  1. Verifies `git` and `claude` are on PATH (Python is NOT a prereq — uv provisions it).
  2. Installs `uv` (https://docs.astral.sh/uv/) if missing, via Astral's official installer.
  3. Provisions a hermetic Python 3.12 via `uv python install 3.12`.
  4. Clones (or fast-forwards) enricai/centella into $CENTELLA_HOME (default ~/.centella).
  5. Symlinks $CENTELLA_HOME/centella into ~/.local/bin/centella.
  6. Verifies the install with `centella --version`.

Flags:
  --dry-run        Print actions without executing.
  --prefix DIR     Install Centella under DIR (default: $CENTELLA_HOME or ~/.centella).
  --bin-dir DIR    Symlink dir (default: ~/.local/bin).
  --ref REF        Git ref to install (default: main).
  --help           Show this message and exit.

Env vars:
  CENTELLA_HOME      Install directory (default ~/.centella). --prefix overrides.
  CENTELLA_BIN_DIR   Symlink directory (default ~/.local/bin). --bin-dir overrides.
  CENTELLA_REPO_URL  Repo URL to clone (default https://github.com/enricai/centella.git).
EOF
}

log() {
  printf 'install: %s\n' "$*"
}

err() {
  printf 'install: error: %s\n' "$*" >&2
}

run() {
  # Print the command, then run it — or just print it under --dry-run.
  printf '  $ %s\n' "$*"
  if [ "$DRY_RUN" = "false" ]; then
    "$@"
  fi
}

have_runnable() {
  # `command -v` returns success for shimmed entries (pyenv) that can't
  # actually exec — invoke `--version` to confirm it really runs.
  "$1" --version >/dev/null 2>&1
}

remediate_git() {
  case "$(uname -s)" in
    Darwin) err "git is missing. Install with: xcode-select --install   (or: brew install git)" ;;
    Linux)  err "git is missing. Install with your distro's package manager (apt install git / dnf install git / pacman -S git)." ;;
    *)      err "git is missing. Install it from https://git-scm.com/" ;;
  esac
}

remediate_claude() {
  err "claude CLI is missing. Install Claude Code from https://claude.ai/code"
  err "Centella shells out to \`claude -p\` for every unit of LLM work; there is no fallback."
}

remediate_curl() {
  case "$(uname -s)" in
    Darwin) err "curl is missing. Install with: brew install curl   (macOS ships curl by default; reinstall if it's gone.)" ;;
    Linux)  err "curl is missing. Install with your distro's package manager (apt install curl / dnf install curl / pacman -S curl)." ;;
    *)      err "curl is missing. Install it from https://curl.se/" ;;
  esac
}

# --- argument parsing ----------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY_RUN=true; shift ;;
    --prefix)   PREFIX="${2:?--prefix needs an argument}"; shift 2 ;;
    --bin-dir)  BIN_DIR="${2:?--bin-dir needs an argument}"; shift 2 ;;
    --ref)      REF="${2:?--ref needs an argument}"; shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *)
      err "unrecognized arg: $1"
      usage >&2
      exit 2
      ;;
  esac
done

# --- 1. preflight: git + claude + curl -----------------------------------
# curl is required for the uv bootstrap step (`curl ... | sh`); preflighting
# it here surfaces a clear message instead of crashing mid-install if it's
# missing on a no-uv system.

log "preflight: checking git, claude, and curl"
missing=0
if ! have_runnable git; then
  remediate_git
  missing=1
fi
if ! have_runnable claude; then
  remediate_claude
  missing=1
fi
if ! have_runnable curl; then
  remediate_curl
  missing=1
fi
if [ "$missing" -ne 0 ]; then
  exit 1
fi

# --- 2. bootstrap uv if missing ------------------------------------------

if have_runnable uv; then
  log "uv already installed ($(uv --version))"
else
  log "uv not found — installing from https://astral.sh/uv/install.sh"
  if [ "$DRY_RUN" = "false" ]; then
    # Astral's installer is statically linked; no Python required to bootstrap.
    # --proto and --tlsv1.2 mirror rustup's hardening.
    curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh
  else
    printf '  $ %s\n' "curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh"
  fi
  # The uv installer drops the binary into ~/.local/bin (or ~/.cargo/bin on
  # older systems). Add the likely paths to PATH for the remainder of this
  # script so the next `uv` call resolves without re-sourcing the user's shell.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! have_runnable uv && [ "$DRY_RUN" = "false" ]; then
    err "uv installed but not yet on PATH. Restart your shell and re-run this installer."
    exit 1
  fi
fi

# --- 3. provision Python 3.12 -------------------------------------------

log "provisioning Python $DEFAULT_PYTHON via uv"
run uv python install "$DEFAULT_PYTHON"

# --- 4. clone or update --------------------------------------------------

if [ -d "$PREFIX/.git" ]; then
  log "updating existing Centella checkout at $PREFIX"
  run git -C "$PREFIX" fetch origin
  run git -C "$PREFIX" checkout "$REF"
  run git -C "$PREFIX" pull --ff-only origin "$REF"
elif [ -e "$PREFIX" ]; then
  err "$PREFIX exists and is not a git checkout — refusing to overwrite."
  err "Pass --prefix DIR to choose a different install directory."
  exit 1
else
  log "cloning $REPO_URL into $PREFIX"
  run git clone --depth 1 --branch "$REF" "$REPO_URL" "$PREFIX"
fi

# --- 5. symlink launcher into bin dir ------------------------------------

log "symlinking $PREFIX/centella into $BIN_DIR/centella"
run mkdir -p "$BIN_DIR"
LAUNCHER="$PREFIX/centella"
LINK="$BIN_DIR/centella"
# Clobber any pre-existing file/symlink at $LINK so re-runs are idempotent.
# $BIN_DIR/centella is a path this installer owns by virtue of installing
# Centella; if a user wants a custom file there, --bin-dir is the escape hatch.
if [ -L "$LINK" ] || [ -f "$LINK" ]; then
  run rm -f "$LINK"
fi
run ln -s "$LAUNCHER" "$LINK"

# --- 6. PATH check + verify ----------------------------------------------

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    log "WARNING: $BIN_DIR is not in your PATH."
    case "${SHELL##*/}" in
      zsh)  rcfile="$HOME/.zshrc" ;;
      bash) rcfile="$HOME/.bashrc" ;;
      fish) rcfile="$HOME/.config/fish/config.fish" ;;
      *)    rcfile="your shell rc file" ;;
    esac
    if [ "${SHELL##*/}" = "fish" ]; then
      log "Add to $rcfile:    set -gx PATH $BIN_DIR \$PATH"
    else
      log "Add to $rcfile:    export PATH=\"$BIN_DIR:\$PATH\""
    fi
    log "Then restart your shell."
    ;;
esac

log "verifying install"
if [ "$DRY_RUN" = "false" ]; then
  # Run the launcher we just symlinked, not whatever `centella` already
  # exists on PATH — proves *this* install works end-to-end.
  if "$LINK" --version; then
    log "done. Run \`centella \"your task\"\` from any git repository to start."
  else
    err "centella --version failed. The install completed but the binary is not runnable."
    exit 1
  fi
else
  printf '  $ %s\n' "$LINK --version"
  log "dry-run complete."
fi

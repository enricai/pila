#!/usr/bin/env bash
# install.sh — one-command installer for Pila.
#
#   curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
#
# What this does, in order:
#   1. Verifies `git`, `claude`, and `curl` are on PATH.
#   2. Runtime: installs the container runtime if missing AND starts it.
#      - macOS:    `brew install colima` + `colima start --runtime containerd --mount-type virtiofs`
#      - Debian:   `apt-get install containerd` + pinned nerdctl binary + `systemctl enable --now containerd`
#      - Fedora:   `dnf install containerd` + pinned nerdctl binary + `systemctl enable --now containerd`
#      - Arch:     `pacman -S containerd nerdctl` + `systemctl enable --now containerd`
#      Pass --no-runtime-install (or PILA_NO_RUNTIME_INSTALL=1) to skip
#      auto-install — the installer falls back to a hint and exits 1.
#      Unknown distros always fall back to the hint.
#   3. Clones (or fast-forwards) enricai/pila into $PILA_HOME (default ~/.pila).
#   4. Symlinks $PILA_HOME/pila into ~/.local/bin/pila.
#   5. Verifies the install with `pila --version`.
#
# Pila runs entirely inside a container (DESIGN §6 / IMPLEMENTATION §0.5),
# so Python is provisioned by the image at runtime — the host doesn't need
# Python or `uv` anymore. The launcher's --version fast path returns
# without spinning up a container.
#
# Flags:
#   --dry-run                Print actions without executing.
#   --no-runtime-install     Skip auto-install of the container runtime;
#                            print the manual hint and exit 1 if missing.
#   --prefix DIR             Install Pila under DIR (default: $PILA_HOME or ~/.pila).
#   --bin-dir DIR            Symlink dir (default: ~/.local/bin).
#   --ref REF                Git ref to install (default: main).
#   --help                   Show this message and exit.
#
# Env vars:
#   PILA_HOME                 Install directory (default ~/.pila). --prefix overrides.
#   PILA_BIN_DIR              Symlink directory (default ~/.local/bin). --bin-dir overrides.
#   PILA_REPO_URL             Repo URL to clone (default https://github.com/enricai/pila.git).
#   PILA_NO_RUNTIME_INSTALL   Same as --no-runtime-install when truthy ("1", "true", "yes").
set -euo pipefail

# --- defaults ------------------------------------------------------------

# Guard against an unset HOME (some CI containers, broken cron envs, minimal
# Docker images). Without this, $HOME/.pila expands to /.pila and the
# install silently tries to write under the root filesystem.
: "${HOME:?HOME is unset; cannot compute install prefix. Set HOME (or PILA_HOME + PILA_BIN_DIR) and retry.}"

DEFAULT_REPO_URL="https://github.com/enricai/pila.git"
DEFAULT_REF="main"

PREFIX="${PILA_HOME:-$HOME/.pila}"
BIN_DIR="${PILA_BIN_DIR:-$HOME/.local/bin}"
REPO_URL="${PILA_REPO_URL:-$DEFAULT_REPO_URL}"
REF="$DEFAULT_REF"
DRY_RUN=false

# Truthy detector: "1" / "true" / "yes" → true; anything else → false.
# Used to interpret PILA_NO_RUNTIME_INSTALL.
case "${PILA_NO_RUNTIME_INSTALL:-}" in
  1|true|TRUE|yes|YES) NO_RUNTIME_INSTALL=true ;;
  *)                   NO_RUNTIME_INSTALL=false ;;
esac
# Pinned nerdctl version used by the Linux Debian/Fedora paths. Matches
# the version documented in docs/INSTALL.md. Set BEFORE sourcing
# runtime-install.sh so the helper inherits it.
# shellcheck disable=SC2034  # consumed by runtime-install.sh (sourced below)
NERDCTL_VERSION=2.3.1

# --- helpers -------------------------------------------------------------

usage() {
  cat <<'EOF'
install.sh — one-command installer for Pila.

  curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash

What this does, in order:
  1. Verifies `git`, `claude`, and `curl` are on PATH.
  2. Runtime: installs the container runtime if missing AND starts it
     (Colima on macOS via brew; containerd + pinned nerdctl on
     Debian/Fedora/Arch via the distro package manager). Pass
     --no-runtime-install to skip auto-install (fall back to hint + exit 1).
  3. Clones (or fast-forwards) enricai/pila into $PILA_HOME (default ~/.pila).
  4. Symlinks $PILA_HOME/pila into ~/.local/bin/pila.
  5. Verifies the install with `pila --version`.

Flags:
  --dry-run                Print actions without executing.
  --no-runtime-install     Skip auto-install of the container runtime.
  --prefix DIR             Install Pila under DIR (default: $PILA_HOME or ~/.pila).
  --bin-dir DIR            Symlink dir (default: ~/.local/bin).
  --ref REF                Git ref to install (default: main).
  --help                   Show this message and exit.

Env vars:
  PILA_HOME                 Install directory (default ~/.pila). --prefix overrides.
  PILA_BIN_DIR              Symlink directory (default ~/.local/bin). --bin-dir overrides.
  PILA_REPO_URL             Repo URL to clone (default https://github.com/enricai/pila.git).
  PILA_NO_RUNTIME_INSTALL   Same as --no-runtime-install when truthy ("1", "true", "yes").
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
  err "Pila shells out to \`claude -p\` for every unit of LLM work; there is no fallback."
}

remediate_curl() {
  case "$(uname -s)" in
    Darwin) err "curl is missing. Install with: brew install curl   (macOS ships curl by default; reinstall if it's gone.)" ;;
    Linux)  err "curl is missing. Install with your distro's package manager (apt install curl / dnf install curl / pacman -S curl)." ;;
    *)      err "curl is missing. Install it from https://curl.se/" ;;
  esac
}

# Runtime-install helpers (runtime_install_macos, runtime_install_linux,
# _runtime_detect_distro, _runtime_nerdctl_arch) live in
# scripts/runtime-install.sh — sourced below after argument parsing so
# DRY_RUN / NERDCTL_VERSION are already set in the environment.

# --- argument parsing ----------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)              DRY_RUN=true; shift ;;
    --no-runtime-install)   NO_RUNTIME_INSTALL=true; shift ;;
    --prefix)               PREFIX="${2:?--prefix needs an argument}"; shift 2 ;;
    --bin-dir)              BIN_DIR="${2:?--bin-dir needs an argument}"; shift 2 ;;
    --ref)                  REF="${2:?--ref needs an argument}"; shift 2 ;;
    -h|--help)              usage; exit 0 ;;
    *)
      err "unrecognized arg: $1"
      usage >&2
      exit 2
      ;;
  esac
done

# --- source runtime-install helpers --------------------------------------
# DRY_RUN and NERDCTL_VERSION are already set above; runtime-install.sh
# inherits them. The helper defines underscore-prefixed log/err/run
# functions so they don't shadow this installer's own helpers.
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$INSTALL_DIR/runtime-install.sh"

# --- 1. preflight: git + claude + curl -----------------------------------
# curl is required to download the repo (and for the runtime preflight's
# nerdctl-from-upstream guidance on Linux).

log "preflight: checking git, claude, and curl on PATH"
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

# --- 2. runtime: install if missing AND start ---------------------------
# Auto-install Colima on macOS (via brew) and containerd + a pinned nerdctl
# on Linux (via the distro package manager + an upstream binary). Pass
# --no-runtime-install (or PILA_NO_RUNTIME_INSTALL=1) to skip auto-install
# and fall back to a printed hint + exit 1 — preserves the pre-auto-install
# behavior for CI, dotfiles managers, and users who track their own
# package installs. Unknown distros always fall back to the hint regardless.

log "preflight: checking container runtime"
runtime_ok=true
case "$(uname -s)" in
  Darwin)
    if ! have_runnable colima; then
      if [ "$NO_RUNTIME_INSTALL" = "true" ]; then
        err "colima is missing. Install with: brew install colima"
        err "Then start the VM:           colima start --runtime containerd --mount-type virtiofs"
        err "(Do NOT 'brew install nerdctl' on macOS — the formula requires Linux."
        err " Colima provides nerdctl inside its VM and installs a host-side shim;"
        err " pila auto-runs 'colima nerdctl install' on first launch if needed.)"
        runtime_ok=false
      else
        runtime_install_macos || runtime_ok=false
      fi
    elif [ "$DRY_RUN" = "false" ] && ! colima status >/dev/null 2>&1; then
      # Already installed but VM not running — start it.
      log "starting Colima VM (first start may take 30-60s)"
      run colima start --runtime containerd --mount-type virtiofs
    fi
    ;;
  Linux)
    if ! have_runnable nerdctl; then
      if [ "$NO_RUNTIME_INSTALL" = "true" ]; then
        err "nerdctl is missing. Install it from your distro's package manager"
        err "or from https://github.com/containerd/nerdctl/releases."
        err "Examples: 'sudo apt-get install -y containerd' + nerdctl binary download;"
        err "          'sudo pacman -S containerd nerdctl' on Arch."
        err "See docs/INSTALL.md for rootless mode and other distros."
        runtime_ok=false
      else
        runtime_install_linux || runtime_ok=false
      fi
    elif [ "$DRY_RUN" = "false" ] && ! nerdctl info >/dev/null 2>&1; then
      log "enabling + starting containerd via systemd"
      run sudo systemctl enable --now containerd
    fi
    ;;
  *)
    err "unsupported OS: $(uname -s) (need macOS or Linux)"
    runtime_ok=false
    ;;
esac
if [ "$runtime_ok" = "false" ]; then
  exit 1
fi

# --- 3. clone or update --------------------------------------------------

if [ -d "$PREFIX/.git" ]; then
  log "updating existing Pila checkout at $PREFIX"
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

# --- 4. symlink launcher into bin dir ------------------------------------

log "symlinking $PREFIX/pila into $BIN_DIR/pila"
run mkdir -p "$BIN_DIR"
LAUNCHER="$PREFIX/pila"
LINK="$BIN_DIR/pila"
# Clobber any pre-existing file/symlink at $LINK so re-runs are idempotent.
# $BIN_DIR/pila is a path this installer owns by virtue of installing
# Pila; if a user wants a custom file there, --bin-dir is the escape hatch.
if [ -L "$LINK" ] || [ -f "$LINK" ]; then
  run rm -f "$LINK"
fi
run ln -s "$LAUNCHER" "$LINK"

# --- 5. PATH check + verify ----------------------------------------------

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
      log "Add to $rcfile:                      set -gx PATH $BIN_DIR \$PATH"
      log "Or for the current shell session:    set -gx PATH $BIN_DIR \$PATH"
    else
      log "Add to $rcfile:                      export PATH=\"$BIN_DIR:\$PATH\""
      log "Or for the current shell session:    export PATH=\"$BIN_DIR:\$PATH\""
    fi
    log "(rc-file change takes effect after restarting your shell.)"
    ;;
esac

log "verifying install"
if [ "$DRY_RUN" = "false" ]; then
  # Run the launcher we just symlinked, not whatever `pila` already
  # exists on PATH — proves *this* install works end-to-end.
  if "$LINK" --version; then
    log "done. Run \`pila \"your task\"\` from any git repository to start."
  else
    err "pila --version failed. The install completed but the binary is not runnable."
    exit 1
  fi
else
  printf '  $ %s\n' "$LINK --version"
  log "dry-run complete."
fi

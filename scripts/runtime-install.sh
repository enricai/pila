#!/usr/bin/env bash
# runtime-install.sh — container-runtime install helpers (DESIGN §6).
#
# Shared between scripts/install.sh (the one-command installer) and the
# `pila` launcher (which auto-installs on first run when missing). The
# logic was previously inlined in install.sh; extracting it lets the
# launcher invoke the same code path without duplicating it.
#
# Contract for callers:
#   - Source this file (do not exec).
#   - Caller has already detected the runtime is missing (and decided to
#     auto-install — i.e. NO_RUNTIME_INSTALL is false and `[ -t 0 ]` for
#     the launcher's TTY guard).
#   - Caller sets DRY_RUN (true/false). Defaults to false here.
#   - Caller invokes one of:
#       runtime_install_macos
#       runtime_install_linux
#     Returns 0 on success, 1 on failure with a printed error.
#   - Function names use an underscore prefix on the helpers so they
#     don't collide if the caller has its own `log`/`err`/`run` named
#     functions.

# --- defaults ------------------------------------------------------------

# Pinned nerdctl version used by the Linux Debian/Fedora paths. Matches
# the version documented in docs/INSTALL.md and previously hard-coded in
# install.sh.
NERDCTL_VERSION="${NERDCTL_VERSION:-2.3.1}"

# DRY_RUN may be set by the caller (install.sh's --dry-run). Default off.
DRY_RUN="${DRY_RUN:-false}"

# --- helpers (underscore-prefixed to avoid caller collisions) -----------

_runtime_log() {
  printf 'runtime-install: %s\n' "$*"
}

_runtime_err() {
  printf 'runtime-install: error: %s\n' "$*" >&2
}

_runtime_run() {
  # Print the command, then run it — or just print it under --dry-run.
  printf '  $ %s\n' "$*"
  if [ "$DRY_RUN" = "false" ]; then
    "$@"
  fi
}

_runtime_have_runnable() {
  # `command -v` returns success for shimmed entries (pyenv) that can't
  # actually exec — invoke `--version` to confirm it really runs.
  "$1" --version >/dev/null 2>&1
}

# Emit one of debian | fedora | arch | unknown by reading /etc/os-release.
# Uses ID first, falls through to ID_LIKE so derivatives map to their
# parents (Pop!_OS → debian, AlmaLinux → fedora, Manjaro → arch).
_runtime_detect_distro() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
      *' debian '*|*' ubuntu '*) echo debian ;;
      *' fedora '*|*' rhel '*|*' centos '*) echo fedora ;;
      *' arch '*) echo arch ;;
      *) echo unknown ;;
    esac
  else
    echo unknown
  fi
}

# Map host architecture to nerdctl's release-asset suffix (amd64 | arm64).
_runtime_nerdctl_arch() {
  if command -v dpkg >/dev/null 2>&1; then
    dpkg --print-architecture
  else
    case "$(uname -m)" in
      x86_64|amd64) echo amd64 ;;
      aarch64|arm64) echo arm64 ;;
      *) echo unknown ;;
    esac
  fi
}

# --- public: macOS install (Colima via brew) ----------------------------

# Returns 0 on success, 1 on failure. Caller is responsible for the TTY
# guard before invoking (brew install may run sudo).
runtime_install_macos() {
  if _runtime_have_runnable colima; then
    # Already installed; verify it's running.
    if [ "$DRY_RUN" = "false" ] && ! colima status >/dev/null 2>&1; then
      _runtime_log "starting Colima VM (first start may take 30-60s)"
      _runtime_run colima start --runtime containerd --mount-type virtiofs || return 1
    fi
    return 0
  fi
  if ! _runtime_have_runnable brew; then
    _runtime_err "Homebrew is needed to install Colima but isn't on PATH."
    _runtime_err "Install Homebrew from https://brew.sh, then re-run pila."
    return 1
  fi
  _runtime_log "installing Colima via Homebrew"
  _runtime_run brew install colima || return 1
  if [ "$DRY_RUN" = "false" ]; then
    _runtime_log "starting Colima VM (first start may take 30-60s)"
    _runtime_run colima start --runtime containerd --mount-type virtiofs || return 1
  fi
  return 0
}

# --- public: Linux install (containerd + nerdctl per distro) ------------

# Returns 0 on success, 1 on failure. Caller is responsible for the TTY
# guard before invoking (apt-get / dnf / pacman / sudo systemctl all
# prompt for the sudo password).
runtime_install_linux() {
  if _runtime_have_runnable nerdctl && nerdctl info >/dev/null 2>&1; then
    return 0  # already set up
  fi
  local distro arch nerdctl_url
  distro="$(_runtime_detect_distro)"
  arch="$(_runtime_nerdctl_arch)"
  nerdctl_url="https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${arch}.tar.gz"

  case "$distro" in
    debian)
      _runtime_log "installing containerd via apt-get"
      _runtime_run sudo apt-get update || return 1
      _runtime_run sudo apt-get install -y containerd || return 1
      if [ "$arch" = "unknown" ]; then
        _runtime_err "could not detect host arch for the nerdctl binary download."
        _runtime_err "Install nerdctl manually from https://github.com/containerd/nerdctl/releases"
        return 1
      fi
      _runtime_log "installing nerdctl ${NERDCTL_VERSION} (linux-${arch}) from upstream"
      if [ "$DRY_RUN" = "false" ]; then
        curl -L "$nerdctl_url" | sudo tar -C /usr/local/bin -xz nerdctl || return 1
      else
        printf '  $ curl -L %s | sudo tar -C /usr/local/bin -xz nerdctl\n' "$nerdctl_url"
      fi
      ;;
    fedora)
      _runtime_log "installing containerd via dnf"
      _runtime_run sudo dnf install -y containerd || return 1
      if [ "$arch" = "unknown" ]; then
        _runtime_err "could not detect host arch for the nerdctl binary download."
        _runtime_err "Install nerdctl manually from https://github.com/containerd/nerdctl/releases"
        return 1
      fi
      _runtime_log "installing nerdctl ${NERDCTL_VERSION} (linux-${arch}) from upstream"
      if [ "$DRY_RUN" = "false" ]; then
        curl -L "$nerdctl_url" | sudo tar -C /usr/local/bin -xz nerdctl || return 1
      else
        printf '  $ curl -L %s | sudo tar -C /usr/local/bin -xz nerdctl\n' "$nerdctl_url"
      fi
      ;;
    arch)
      _runtime_log "installing containerd + nerdctl via pacman"
      _runtime_run sudo pacman -S --noconfirm containerd nerdctl || return 1
      ;;
    *)
      _runtime_err "unsupported Linux distro (detected: ${distro}). Auto-install"
      _runtime_err "only supports debian/ubuntu, fedora/rhel, and arch."
      _runtime_err "Install nerdctl + containerd manually (see docs/INSTALL.md) or"
      _runtime_err "pass --no-runtime-install to skip the auto-install step."
      return 1
      ;;
  esac

  if [ "$DRY_RUN" = "false" ] \
     && _runtime_have_runnable nerdctl && ! nerdctl info >/dev/null 2>&1; then
    _runtime_log "enabling + starting containerd via systemd"
    _runtime_run sudo systemctl enable --now containerd || return 1
  fi
  return 0
}

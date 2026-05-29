# Installing Pila

Pila runs entirely inside a container. The cleanup guarantee — when you
Ctrl-C, every `claude -p` worker and every test runner / build / dev
server they spawned is reaped — comes from the Linux kernel tearing down
the container's PID namespace, not from heuristics in Python. See
[`DESIGN.md` §6 *Worker subtree termination*](DESIGN.md) for the
architectural reasoning and [`IMPLEMENTATION.md` §0.5 *Container
shape*](IMPLEMENTATION.md) for the launcher / image / mount details.

This document covers one-time setup of the container runtime per OS,
then how to install pila itself.

## macOS

The one-line installer **auto-installs and starts** the container runtime
for you (`brew install colima` + `colima start --runtime containerd
--mount-type virtiofs --cpu N --memory M`). The `--cpu` / `--memory`
values are auto-detected from your host: half of the host's logical
cores (clamped to 2–8) and half of the host's RAM in GB (clamped to
4–16). On an 8-core / 16-GB Mac you'd get a 4-CPU / 8-GB VM; on a
16-core / 64-GB Mac you'd get the 8-CPU / 16-GB ceiling.

This replaces Colima's 2-CPU / 2-GB default, which is not enough for
pila's parallel-worker workload (concurrent `claude -p` workers plus
toolchain processes blow through 2 GB, triggering a kernel OOM in the
VM that manifests as `exit 255` on the launcher with no diagnostic).

If you'd rather install the runtime yourself — common in CI or with
dotfiles managers — pass `--no-runtime-install` or set
`PILA_NO_RUNTIME_INSTALL=1` and the installer will print the manual
commands and exit 1.

```bash
# One-line installer — auto-installs Colima + starts the VM, then installs pila.
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
```

Or, to do the runtime install by hand:

```bash
brew install colima
colima start --runtime containerd --mount-type virtiofs

# Then run the installer with the opt-out flag (or env var):
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash -s -- --no-runtime-install
```

Notes:

- **Do not** `brew install nerdctl`. The Homebrew formula has
  `Requires: Linux` because the nerdctl binary itself talks to a
  containerd Unix socket — which doesn't exist on macOS. Colima provides
  nerdctl *inside its VM* and ships a host-side shim
  (`colima nerdctl install`) that proxies every invocation to the VM.
  Pila's launcher auto-runs `colima nerdctl install` on first use, so
  you don't have to run it yourself.
- `--mount-type virtiofs` is the fastest mount and gives correct UID
  semantics for bind mounts. It's the default on recent Colima.
- The Colima VM persists across reboots — `colima start` again is
  enough to bring it back up. To autostart at login:
  `brew services start colima`.
- The installer auto-sizes the VM (half-of-host CPU/RAM, bounded
  2–8 cores / 4–16 GB; see the macOS section above). To override —
  e.g. you want more or less than the auto-sized default:
  `colima stop && colima start --cpu 6 --memory 12 --runtime containerd --mount-type virtiofs`.
- If you have Colima already running with a smaller-than-recommended
  VM, re-running the installer will leave the VM alone but log a
  one-line hint with the resize command.

### macOS-specific: bind-mount scope

Colima auto-shares only paths under `/Users/$USER` into the VM by
default. Any path outside that range (an external volume, a system
path) appears as an *empty* directory inside the container — with no
error. Pila's launcher warns at preflight if `$USER_REPO` or any
`--inspect-dir` falls outside `/Users/$USER`.

To allow paths outside the default scope: edit
`~/.colima/default/colima.yaml`, add the path under `mounts:`, then
`colima restart`.

## Linux

Containerd and nerdctl run natively — no VM needed. The one-line
installer **auto-installs and starts** the runtime per distro (Debian/
Ubuntu via `apt-get`, Fedora/RHEL via `dnf`, Arch via `pacman`; nerdctl
binary pinned to v2.3.1 from upstream). Unknown distros fall back to a
hint and exit 1 — install manually then re-run with
`--no-runtime-install`.

```bash
# One-line installer — auto-installs containerd + nerdctl, then installs pila.
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
```

Or, to do the runtime install by hand (sections below show the per-distro
commands), then pass `--no-runtime-install`:

```bash
# After running the per-distro setup below:
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash -s -- --no-runtime-install
```

### Debian / Ubuntu

```bash
sudo apt-get install -y containerd
# nerdctl: install the pinned static binary from upstream. Arch is detected
# so the same line works on x86_64 (amd64) and arm64 (Asahi, Graviton, Pi).
NERDCTL_VERSION=2.3.1
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
curl -L "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${ARCH}.tar.gz" \
  | sudo tar -C /usr/local/bin -xz nerdctl
sudo systemctl enable --now containerd

curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
```

### Fedora / RHEL

```bash
sudo dnf install -y containerd
# nerdctl: install the pinned static binary from upstream (arch-detected).
NERDCTL_VERSION=2.3.1
ARCH="$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
curl -L "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${ARCH}.tar.gz" \
  | sudo tar -C /usr/local/bin -xz nerdctl
sudo systemctl enable --now containerd

curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
```

### Arch

```bash
sudo pacman -S containerd nerdctl
sudo systemctl enable --now containerd

curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
```

### Rootless mode (recommended)

Running containerd as root is unnecessary for pila — it doesn't need
privileged operations. To set up rootless containerd:

```bash
containerd-rootless-setuptool.sh install
```

After that, the user's default nerdctl context points at the rootless
socket (`unix:///run/user/$UID/containerd/containerd.sock`). Pila's
launcher uses whatever context nerdctl resolves to, so once rootless is
set up no extra flags are needed.

## Verifying the runtime

Before running pila, confirm the runtime works:

```bash
nerdctl run --rm hello-world
```

You should see "Hello from Docker!" (containerd uses the same image).
If that fails, pila will too.

## What pila mounts into the container

When the container starts, the launcher mounts the following:

| Host path | Container path | Mode | Purpose |
|---|---|---|---|
| `$(pwd)` (your repo) | `/work` | rw | Pila operates here. Worktrees and `.pila/` state are written to your host filesystem; `--resume` works across runs. |
| `$PILA_HOME` (pila install) | `/work/.pila-image` | ro | Pila's source and Dockerfile. Edit `orchestrator/pila.py` on the host; next run picks it up without rebuilding the image. |
| Per-run host scratch dir (`~/.cache/pila/cfg-…/.claude.json`) | `/home/pila/.claude.json` | rw | Per-container copy of `~/.claude.json` with `projects[]` stripped. The shared host file is never directly mounted — it's a documented `claude-code` corruption race (anthropics/claude-code issues #28847, #29217, #29395, #40226) that hangs workers in a recovery loop. Each container writes only its private copy. |
| Per-run host scratch dir (`~/.cache/pila/cfg-…/.claude/`) | `/home/pila/.claude` | rw | Per-container copy of `~/.claude/` with bulky, prior-session, and history paths skipped (`history.jsonl`, `projects/`, `sessions/`, `tasks/`, `plans/`, `todos/`, `file-history/`, `paste-cache/`, `shell-snapshots/`, `session-env/`, `telemetry/`, `debug/`, `downloads/`, `backups/`, `chrome/`, `ralph-state/`). CLI capability dirs (`agents/`, `skills/`, `commands/`, `hooks/`, `plugins/`, `settings.json`, `mcp-needs-auth-cache.json`, `local/`, `statsig/`, `cache/`) ride along. |
| Keychain (macOS only) → staged `.claude/.credentials.json` | `/home/pila/.claude/.credentials.json` | rw | The Claude CLI stores its OAuth token in Keychain on macOS (an IPC service the container can't reach). The launcher extracts it with `security find-generic-password -s "Claude Code-credentials" -w` and writes the JSON blob to the staged credentials file — the same path the Linux CLI reads — so authentication works identically on both platforms. |
| Per-run host scratch copies of `~/.gitconfig`, `~/.gitconfig.local`, `~/.gitignore`, `~/.gitignore_global`, `~/.git-credentials`, `~/.netrc`, `~/.config/git/`, `~/.ssh/`, `~/.gnupg/` | `/home/pila/.<same>` | rw | Per-container copies of every present host config / auth file the worker might need. SSH and GPG copies exclude agent sockets (`agent/`, `S.*`, `*.sock`) — sockets are host-bound and not reachable from the container. Workers can `git config --local`, push over SSH, or `git commit -S` if signing is configured, all against private copies that vanish on container exit. |
| Each `--inspect-dir` path | `/inspect/<basename>` | ro | Extra directories the inspect-bucket workers (classifier, planner, reconciler, provision) need read access to. |

Per-container isolation is the key design choice: each container sees
a private copy of your Claude + git + SSH + GPG config at the default
paths the CLI and git already look at, so nothing inside the container
knows or cares that the files are private rather than the shared host
originals. Container-side writes (incremented startup counters, new
session transcripts, refreshed auth state) are intentionally lost when
the container exits — pila's own telemetry (`.pila/runs/<id>/`) is the
source of truth for run cost and structure. The host scratch dir is
reaped on container exit; your host `~/.claude.json` and `~/.claude/`
are never modified by a worker.

## Troubleshooting

**Skip auto-install of the container runtime** — pass
`--no-runtime-install` to `install.sh`, or set
`PILA_NO_RUNTIME_INSTALL=1`. The installer falls back to printing the
manual hint and exits 1 if the runtime is missing. Useful for CI,
dotfiles managers, or any environment where package installs are
tracked elsewhere.

**"Colima VM is not running"** (macOS) — start it:
`colima start --runtime containerd --mount-type virtiofs`.

**"nerdctl cannot reach the container runtime"** — on macOS, you
probably started Colima with the default `docker` runtime. Restart
with containerd: `colima stop && colima start --runtime containerd
--mount-type virtiofs`. On Linux, check `systemctl status containerd`.

**"$HOME/.claude not found"** — you haven't run `claude` yet on this
machine. Run `claude --version` at least once so the directory is
created.

**Permission denied on `.pila/`** — UID mismatch. The launcher passes
`--build-arg HOST_UID=$(id -u)` so the in-container `pila` user matches
your host user. If you copied the image from another machine with a
different UID, rebuild: `nerdctl image rm pila:<version>` and re-run
pila.

**Slow `npm install` / `vitest`** on macOS — ensure Colima is using
VirtioFS (the documented setup uses `--mount-type virtiofs`). Bump the
VM's RAM if needed: `colima stop && colima start --cpu 6 --memory 12
--runtime containerd --mount-type virtiofs`.

**"$path may appear empty in the container"** warning (macOS) — Colima
only auto-shares paths under `/Users/$USER`. Edit
`~/.colima/default/colima.yaml`, add the path under `mounts:`, then
`colima restart`.

**Git push fails with `/opt/homebrew/bin/gh: command not found`** —
your `~/.gitconfig` has a credential helper line that hard-codes the
macOS Homebrew path for `gh`, but inside the Debian container `gh` is
at `/usr/bin/gh`. Older `gh auth setup-git` versions wrote the absolute
path; recent versions write the relative form `helper = !gh auth
git-credential` (uses `$PATH`). To fix, either re-run `gh auth
setup-git` on the host (overwrites with the relative form), or
manually edit `~/.gitconfig` to drop the `/opt/homebrew/bin/`
prefix from the `helper = !... gh auth git-credential` line.

**Git errors at run start when invoking pila from a git worktree** —
if your repo cwd is itself a `git worktree add`-created worktree (not
the main checkout), the worktree's `.git` file points at a parent
path that lives outside the container's `/work` bind mount. Setup
fails with a "cannot access path" git error. Workaround: invoke pila
from the main checkout, not from a worktree. (Pila itself creates
worktrees under `.pila/runs/<run-id>/worktrees/` inside the bind
mount — those work normally; this limitation only affects pila being
*invoked from* a host-side worktree.)

## Uninstalling

```bash
# Remove the cached pila image.
nerdctl image rm pila:<version>   # or: nerdctl image rm $(nerdctl images -q pila)

# Remove pila itself.
rm -rf ~/.pila
rm -f ~/.local/bin/pila

# Optional: remove the runtime.
# macOS:
brew uninstall colima
rm -rf ~/.colima
# Linux: use your distro's package manager to remove containerd + nerdctl.
```

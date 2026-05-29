# Pila — Implementation Reference

> **This document describes the current code, not the design.** It is true only
> against the present state of `orchestrator/pila.py`, the worker prompts,
> and the shell scripts. A change to the code that is not reflected here makes
> *this document* wrong — unlike `DESIGN.md`, which describes the architecture
> and stays correct across reimplementation. When this document and the code
> disagree, the code is authoritative. When this document and `DESIGN.md`
> disagree, `DESIGN.md` defines what *should* be true.
>
> Read `DESIGN.md` first for the *why*; this document is the *what* and *where*.

---

## 0. Install surface

Pila ships two install paths. Both ultimately invoke the on-disk
`pila` launcher; the difference is who put it there and how the
user reaches it. The launcher itself is a portable bash script —
the host needs neither Python nor `uv`. Everything Python lives
inside the container (DESIGN §6 / §0.5 below).

### Files

| Path | Purpose |
|------|---------|
| `.claude-plugin/marketplace.json` | Single-plugin marketplace manifest. Makes the repo itself discoverable via `/plugin marketplace add enricai/pila` from inside Claude Code. Points at `.` so Claude Code reads the sibling `.claude-plugin/plugin.json`. |
| `.claude-plugin/plugin.json` | Existing plugin manifest (commands, skills, metadata). The `version` field is the single source of truth for `pila --version`. |
| `scripts/install.sh` | The `curl \| bash` shell installer. Preflight (git/claude/curl) → runtime preflight (colima on macOS, nerdctl+containerd on Linux) → clone → symlink → verify. Self-contained bash; deps: `bash`, `curl`, `git`. |
| `pila` (launcher) | Portable bash. Symlink-walks to its own location, runs the per-OS runtime preflight, builds the pila image once per version, and execs `nerdctl run` with TTY flags adapted via `[ -t 0 ]` (see §0.5). Fast paths for `--version` skip container startup. |
| `Dockerfile` | Image recipe (Debian 12 + Node + pnpm + claude CLI + baked orchestrator source). Built locally on first run, tagged `pila:<VERSION>`. |
| `scripts/container-entry.sh` | Container PID 1. `cd /work && exec python3 /work/.pila-image/orchestrator/pila.py "$@"`. |
| `scripts/remote/build-push.sh` | Build and push a self-contained pila image to Fly.io's registry. The baked source at `/work/.pila-image/` lets the image run on Fly Machines without any bind mount. |

### Python runtime — provisioned inside the container

Pila requires Python 3.10+. The container image installs Debian 12's
`python3` (currently 3.11), which satisfies the requirement. The host
needs no Python at all. The orchestrator's source is baked into the
image at `/work/.pila-image/` via the Dockerfile's `COPY` instructions.
On local runs the launcher's bind mount (`-v $PILA_REPO:/work/.pila-image:ro`)
shadows the baked copy, so iterating on `orchestrator/pila.py` still
does not require an image rebuild — the host file is used on the next run.

The orchestrator itself remains stdlib-only — no `pip install`, no
`pyproject.toml`, no PyPI release. `pytest` is still the only dev
dependency, run on the host against the bind-mounted source.

### Path A — Claude Code plugin marketplace (primary)

```
/plugin marketplace add enricai/pila
/plugin install pila@enricai-pila
# then inside Claude Code:
/pila "task description"
```

`marketplace.json` exposes one plugin (the existing `plugin.json`).
Claude Code clones the repo into its plugin directory and registers the
`commands/` and `skills/` entries. `/pila` then runs the plugin
skill at `commands/pila.md`, which shells out to the on-disk
`pila` launcher in the cloned plugin directory — and through it,
to `nerdctl run`. See §0.5 for the launcher's per-mode (terminal vs
plugin) TTY adaptation.

### Path B — `curl | bash` installer (secondary)

```bash
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
```

The script:

1. **Preflight**: verifies `git`, `claude`, and `curl` are on `PATH`.
   Missing deps print a platform-specific remediation hint and the
   script exits non-zero.
2. **Runtime preflight**: per `uname -s`. On macOS: verifies `colima`
   is installed and the VM is running. On Linux: verifies `nerdctl`
   is installed and reaches containerd. Prints copy-pasteable install
   hints on failure (`brew install colima` / distro package commands).
   Does NOT auto-install brew/apt packages — that's the user's choice.
3. **Clones** `enricai/pila` to `$PILA_HOME` (default `~/.pila`).
   `git clone --depth 1` for fresh installs; `git pull --ff-only` for
   upgrades.
4. **Symlinks** `$PILA_HOME/pila` → `~/.local/bin/pila`. Creates
   `~/.local/bin` if missing. Does not touch system directories.
5. **PATH check**: if `~/.local/bin` is not in `$PATH`, prints (does
   not silently edit) the exact shell-rc line to add, based on `$SHELL`.
6. **Verifies** by invoking `pila --version` (the launcher's fast path
   answers without spinning up a container — see below).

Supports `--dry-run` (prints actions without executing) and
`--prefix DIR` (overrides `PILA_HOME`).

### `--version`

`pila --version` reads `.claude-plugin/plugin.json`'s `version`
field — single source of truth. Two parallel readers:

- **Orchestrator** (`_read_version()` in `pila.py`): stdlib `json` load.
  Exercised by `tests/test_version_flag.py`.
- **Launcher** (bash `awk` extraction): used by the fast path that
  short-circuits container startup. Both readers return the same value
  on the same `plugin.json`, and `tests/test_version_flag.py` guards
  the canonical surface.

`install.sh` uses `pila --version` as its end-to-end smoke test — and
because the fast path doesn't require a running container, the smoke
test runs the moment the symlink is in place.

Maps to `DESIGN.md`: §2 (no plugin-spawned subagents — the launcher is
plain process exec, not in-session orchestration). §6 *Worker subtree
termination* and §0.5 of this document describe what runs inside the
container the launcher starts.

---

## 0.5. Container shape

Pila runs entirely inside a single container per run (DESIGN §6 *Worker
subtree termination*). The orchestrator is PID 1 in the container;
every `claude -p` worker it spawns is a child process in the same PID
namespace; every Bash tool call those workers make lands in the same
namespace too. When PID 1 exits, the kernel reaps the namespace —
which is the abnormal-exit cleanup guarantee.

### Runtime requirements per OS

| OS | Container engine | CLI | VM? |
|----|------------------|-----|-----|
| macOS (arm64 or x86_64) | containerd inside a Colima-managed Linux VM | `nerdctl` host-side shim (`colima nerdctl install`) | Yes — managed by Colima |
| Linux (any distro with containerd) | containerd native | `nerdctl` from distro or upstream | No |

The launcher detects `uname -s` and runs the right preflight. On macOS:
require `colima` on `PATH`, check `colima status`, auto-install the
`nerdctl` shim if missing (via `colima nerdctl install`), then check
`nerdctl info` reaches the runtime. On Linux: require `nerdctl` on
`PATH` and `nerdctl info` succeeds. Both paths print a copy-pasteable
install hint on failure and exit non-zero — pila does not invoke
`brew`, `apt`, `dnf`, or `pacman` itself.

`brew install nerdctl` does NOT work on macOS — the Homebrew formula
has `Requires: Linux` because the nerdctl binary talks directly to a
containerd Unix socket. Colima's `colima nerdctl install` is the
supported macOS path; it drops a host-side shim on `$PATH` that
proxies every invocation to nerdctl inside the VM.

### Image build

`Dockerfile` at the repo root. Built locally on first run
(`nerdctl image inspect "$IMAGE_TAG"` miss → `nerdctl build`).
`IMAGE_TAG=pila:<VERSION>` so a pila upgrade triggers a fresh build
once and reuses the layer cache thereafter. ~60–120s first build,
subsequent runs < 3s.

Base layers (top-down):

- `debian:12-slim` — minimal, predictable, glibc-based.
- `apt-get install`: `ca-certificates`, `curl`, `git`, `openssh-client`,
  `python3`, `python3-pip`, `build-essential`. The build tools cover
  native-module compilation in `npm install` (sharp, bcrypt, esbuild
  fallback, etc.) so `node-gyp` doesn't fail on first run.
- Node.js LTS, arch-aware via `TARGETARCH` / `dpkg --print-architecture`
  → `arm64` → `linux-arm64` tarball, `amd64` → `linux-x64`. Pinned via
  `ARG NODE_VERSION` so the version is reproducible across builds.
- `pnpm` (pinned), `npm install -g @anthropic-ai/claude-code` (the
  `claude` CLI workers invoke; pila enforces ≥ 2.1.22 at runtime).
- Non-root `pila` user created with `--build-arg HOST_UID/HOST_GID`
  matching the host user. This is what makes files the container
  writes into `/work/.pila/` and the worktrees keep the host user's
  ownership.
- `WORKDIR /work`, `ENTRYPOINT ["/work/.pila-image/scripts/container-entry.sh"]`.

### Registry publish path (fly.io / remote Machines)

Fly.io Machines pull an image from a registry rather than using a
locally-built image. The `HOST_UID/HOST_GID` coupling exists only for
local bind-mounts (so files written by the container into `/work` keep
the host user's ownership). Remote Machines have no such bind-mount, so
the Dockerfile's defaults (`ARG HOST_UID=501 / HOST_GID=20`) are used
as-is — no UID matching required.

**Baked source.** The Dockerfile's `COPY` instructions bake
`orchestrator/`, `scripts/`, `prompts/`, and `.claude-plugin/` into the
image at `/work/.pila-image/`. A Fly Machine that pulls this image can
run the orchestrator without any bind mount — the ENTRYPOINT
(`/work/.pila-image/scripts/container-entry.sh`) and the orchestrator
(`/work/.pila-image/orchestrator/pila.py`) are already present. On
local runs the launcher's `-v $PILA_REPO:/work/.pila-image:ro` bind
mount shadows the baked copy, so development iteration (edit a file,
run pila) still works without rebuilding the image.

`scripts/remote/build-push.sh` provides the remote build path:

```bash
# Build locally, tag for fly.io private registry, push, and verify:
./scripts/remote/build-push.sh --app <fly-app-name> --push

# Verify the baked source works inside a Machine:
flyctl machine run registry.fly.io/<fly-app-name>:<VERSION> \
  --app <fly-app-name> \
  -- python3 /work/.pila-image/orchestrator/pila.py --version
```

Alternative: let fly build remotely (no local container runtime needed):

```bash
flyctl deploy --build-only --push \
  --config fly.toml \
  --dockerfile Dockerfile
# fly reads the Dockerfile, COPY bakes the source, result is pushed to
# registry.fly.io/<app> automatically.
```

Note the two `.pila*` paths inside the container:

- **`/work/.pila/`** is the run-state directory inside the user's
  repo (state.json, logs, worktrees, telemetry). It lives on the
  host filesystem via the `/work` bind mount and persists across
  container runs.
- **`/work/.pila-image/`** is the orchestrator source tree. On local
  runs it is a read-only bind mount of `$PILA_HOME` on the host; on
  Fly Machines it is the baked copy from the Dockerfile's `COPY`
  instructions. Both paths resolve identically at runtime — the
  ENTRYPOINT and orchestrator code always live at
  `/work/.pila-image/{scripts,orchestrator}/`.

The container's PID 1 (the entry script) reads from `.pila-image/`
and writes to `.pila/`. Confusing the two would either break runs
(writing to the read-only mount) or corrupt the install (writing to
the source tree).

### Entrypoint and source mounting

`scripts/container-entry.sh` is exec'd as PID 1:

```sh
#!/bin/sh
set -e
cd /work
exec python3 /work/.pila-image/orchestrator/pila.py "$@"
```

The orchestrator's source lives at `/work/.pila-image/`. It is present
in two ways depending on execution mode:

- **Local runs:** the launcher bind-mounts `$PILA_HOME` read-only at
  `/work/.pila-image`. Iterating on `orchestrator/pila.py` does not
  need an image rebuild — the bind mount shadows the baked copy and
  the host file is picked up on the next `pila` invocation.
- **Fly.io Machines (remote):** there is no bind mount. The Dockerfile
  `COPY` instructions bake `orchestrator/`, `scripts/`, `prompts/`,
  and `.claude-plugin/` into the image at `/work/.pila-image/` so the
  entrypoint resolves without any host-side path. A new pila version
  requires rebuilding and pushing the image (see §0.5 "Registry publish
  path").

### Bind-mount table

The launcher passes the following mounts to `nerdctl run`:

| Host path | Container path | Mode | Purpose |
|---|---|---|---|
| `$(pwd -P)` (user repo) | `/work` | rw | The repo pila operates on. Worktrees and `.pila/` state live here. Writes flow back to the host so `--resume` works across container runs. |
| `$PILA_HOME` (pila install dir) | `/work/.pila-image` | ro | Orchestrator source + Dockerfile + prompts. Read-only because the container has no business mutating the install. |
| `$STAGE/.claude.json` (per-run host scratch) | `/home/pila/.claude.json` | rw | Per-container copy of `~/.claude.json` with the `projects[]` block stripped. The host file is never directly mounted into a container: the shared mount is a documented `claude-code` corruption race (anthropics/claude-code issues #28847, #29217, #29395, #40226 — all open) that hangs workers in a recovery loop with no backoff. Each container writes only its private copy. |
| `$STAGE/.claude` (per-run host scratch) | `/home/pila/.claude` | rw | Per-container copy of `~/.claude/` with bulky, prior-session, and history paths skipped (`history.jsonl`, `projects/`, `sessions/`, `tasks/`, `plans/`, `todos/`, `file-history/`, `paste-cache/`, `shell-snapshots/`, `session-env/`, `telemetry/`, `stats-cache.json`, `debug/`, `downloads/`, `backups/`, `chrome/`, `ralph-state/`, `.last-cleanup`, `settings.json.*`). CLI capability dirs (`agents/`, `skills/`, `commands/`, `hooks/`, `plugins/`, `mcp-needs-auth-cache.json`, `settings.json`, `local/`, `statsig/`, `cache/`, `package.json`, `policy-limits.json`) ride along. |
| Keychain → `$STAGE/.claude/.credentials.json` (macOS only) | `/home/pila/.claude/.credentials.json` | rw | On macOS the launcher extracts the OAuth token JSON from Keychain (service `Claude Code-credentials`) and writes it to the staged `.claude/.credentials.json`. The Linux CLI reads exactly that path, so both platforms use the same file-based auth flow inside the container. Extraction uses `security find-generic-password -w`; succeeds silently in the user's login session. |
| `$STAGE/.gitconfig`, `.gitconfig.local`, `.gitignore`, `.gitignore_global`, `.git-credentials`, `.netrc` (per-run host scratch) | `/home/pila/.<same>` | rw | Per-container copies of each present host `~/.git*` sibling and `~/.netrc`. Worker can `git config --local` / mutate freely without affecting host state. |
| `$STAGE/.config/git` (per-run host scratch) | `/home/pila/.config/git` | rw | XDG-style git config (`~/.config/git/config`, `~/.config/git/ignore`) copied per-container. |
| `$STAGE/.ssh` (per-run host scratch) | `/home/pila/.ssh` | rw | Per-container copy of `~/.ssh/` with `agent/`, `S.*`, and `*.sock` excluded — host UNIX sockets aren't reachable from inside the container and `cp -a` on them is pointless. Keys and `known_hosts` ride along so workers can SSH-push if needed. Permissions set to `0700`. |
| `$STAGE/.gnupg` (per-run host scratch) | `/home/pila/.gnupg` | rw | Per-container copy of `~/.gnupg/` with agent socket files (`S.gpg-agent*`, `S.scdaemon`, `S.keyboxd`) excluded. Keyrings + `trustdb.gpg` ride along so workers can `git commit -S` if signing is configured. Permissions set to `0700`. |

The four host-auth mounts (`~/.config/gh`, `~/.git-credentials`, `~/.ssh`,
`$SSH_AUTH_SOCK`) that earlier versions of pila bind-mounted **no longer
exist** — finalize moved to the host (DESIGN §6 *Finalization*), so
`git push` and `gh pr create` run with the host's working auth state and
don't need to be forwarded into the container. The macOS-only "SSH agent
forwarding is not available" note is gone for the same reason.
| `~/.cache/pila/mise-data` | `/home/pila/.local/share/mise` | rw | Mise's `MISE_DATA_DIR` (per-repo runtime installs, plugins, cache). Lives in the user dir so the resolver checks it first then falls through to the image-baked `MISE_SYSTEM_DATA_DIR=/usr/local/share/mise` for the LTS fallback (DESIGN §6½). |
| `~/.cache/pila/pnpm-store` | `/home/pila/.cache/pila/pnpm-store` | rw | pnpm content-addressable store. Pointed at via `npm_config_store_dir` (the pnpm-respected env var; `PNPM_STORE_PATH` doesn't exist and would be silently ignored). Safe for concurrent installs across worktrees (pnpm/discussions#10702). |
| `~/.cache/pila/pip` | `/home/pila/.cache/pila/pip` | rw | pip HTTP + wheels cache. Each worker that needs Python deps runs `pip install` / `uv sync` itself in its own worktree against this shared cache; after the first install of a package the cache is warm and subsequent workers' installs are fast. Wheel-build race pypa/pip#9034 is still a theoretical concern but in practice rare given pila's small worker concurrency (DESIGN §6½). |
| `~/.cache/pila/go-mod` | `/home/pila/.cache/pila/go-mod` | rw | `GOMODCACHE`. Concurrent-safe via per-module-version `flock` in `cmd/go/internal/modfetch`. |
| `~/.cache/pila/cargo` | `/home/pila/.cache/pila/cargo` | rw | Whole `CARGO_HOME` (registry + bin + config.lock). Mounting only `registry/` breaks `config.lock` (cargo#11376). Concurrent-safe via cargo's documented flock semantics. |
| Each `--inspect-dir` path (translated) | `/inspect/<basename>` | ro | See below. |

### `--inspect-dir` path translation

Inspect dirs (`--add-dir` forwarded to `claude -p` for cross-repo
context) come from CLI flags, the `PILA_INSPECT_DIRS` env var, or
`pila.toml`'s `inspect_dirs` key. They are *host* paths. The launcher:

1. Collects all three sources before any container is started.
2. For each host path: resolves it on the host (`cd -P "$path" && pwd`,
   so symlinks and `~` are expanded), bind-mounts it read-only at
   `/inspect/<basename>` inside the container, and rewrites the
   corresponding CLI flag to point at the in-container path.
3. Passes only the rewritten flags into the container, and clears
   `PILA_INSPECT_DIRS` in the container env so the in-container
   resolver doesn't see any host paths.

This honors the orchestrator's precedence rules in `resolve_inspect_dirs`
(CLI > env > TOML) by emitting only CLI args — the env and TOML pre-passes
in the launcher synthesize CLI flags.

A host path *inside* `$USER_REPO` (already visible at `/work/<subpath>`)
collides with the launcher's `/inspect/<basename>` target. The launcher
warns and skips the redundant mount.

### macOS-specific: Colima auto-share scope

Colima auto-shares only paths under `/Users/$USER` into the VM by
default. A bind mount of a path outside that range will silently
appear empty inside the container. The launcher warns at preflight
when `$USER_REPO` or any `--inspect-dir` falls outside, and points
the user at `~/.colima/default/colima.yaml`'s `mounts:` section as
the workaround.

VirtioFS is the mount type pila documents (`colima start
--runtime containerd --mount-type virtiofs`) — it's the fastest
option and gives correct UID semantics for bind mounts.

### Logging, signal flow, and TTY adaptation

The launcher invokes `nerdctl run --rm $TTY_FLAGS …` where `TTY_FLAGS`
is chosen by a one-line `[ -t 0 ]` test:

```sh
TTY_FLAGS="-i"
[ -t 0 ] && TTY_FLAGS="-it"
```

That single test is **the entire branch** between terminal mode and
plugin mode. Everything else (mounts, image, env, entrypoint, signal
handling) is identical.

**Terminal mode (`-it`)**:

- `-i` + `-t` give the orchestrator a controlling TTY → its existing
  `log(...)` and stream-event summarizers write directly to the user's
  terminal with no aggregation layer. No changes to `log()` or the
  per-worker summary code.
- `--clarify` prompts use `input()` interactively — the user types
  answers at the host terminal, characters flow through the pty to
  Python inside the container.
- Ctrl-C in the host terminal sends SIGINT to the container's PID 1
  (the orchestrator). Python's `KeyboardInterrupt` fires, the
  existing `except KeyboardInterrupt` handler runs the worktree-only
  cleanup, the orchestrator exits — and the kernel reaps everything
  else in the PID namespace.

**Plugin mode (`-i` only)**:

- Claude Code's Bash tool spawns the launcher without a TTY on stdin.
  `[ -t 0 ]` returns false; the launcher passes only `-i`, no pty
  allocated inside the container.
- Inside the container, `sys.stdin.isatty()` returns False. The
  orchestrator's `gather_answers` (`pila.py:4416`) and mid-execution
  clarification path (`pila.py:4522`) both detect this and trigger
  the canonical no-TTY signal: write `.pila/pending-questions.json`
  to disk and `sys.exit(EXIT_NEEDS_ANSWERS)` (= 10).
- `.pila/pending-questions.json` is visible on the host because
  `/work` is bind-mounted from the user's repo. The plugin agent at
  `commands/pila.md` reads it directly, asks the user via the chat
  UI, writes the matching `.pila/answers.json`, and re-runs the
  container with `--answers .pila/answers.json` and `--resume`.
- Stdout/stderr stream back through the Bash tool to the agent's
  chat session — possibly in 30s-ish chunks per the harness's
  buffering, which is acceptable for the streaming UX.
- The kernel teardown guarantee applies the same way as in terminal
  mode: when the orchestrator exits (clean exit, exit 10, or any
  signal the harness sends), PID 1 dies and the namespace is reaped.

Common to both modes:

- `--rm` removes the stopped container automatically so they don't
  accumulate. Worktrees and state on the bind-mounted host
  filesystem survive for `--resume`.
- `--name pila-<ts>-<pid>` makes `nerdctl ps` legible and
  `nerdctl logs <name>` targetable for the rare diagnostic case.

The plugin mode flow above is exactly what `commands/pila.md` already
documents — it works through the container with zero new mechanism
because `.pila/` lives on the bind-mounted host filesystem.

### What does NOT change in the orchestrator

`orchestrator/pila.py` is unmodified by this design. It runs as PID 1
inside the container; everything it currently does — the asyncio
event loop, the signal handlers, `claude -p` spawn via
`asyncio.create_subprocess_exec`, the per-worker `_terminate_proc_tree`
and `_DescendantTracker` (kept as the fast happy path for clean exits
— see DESIGN §6), worktree management, telemetry — works unchanged.
Container/process isolation is the launcher's concern, not the
orchestrator's.

Maps to `DESIGN.md`: §6 *Cleanup on abnormal exit / Worker subtree
termination*.

---

## 1. Repository layout

```
pila/
├── .claude-plugin/plugin.json     plugin manifest
├── .claude-plugin/marketplace.json single-plugin marketplace manifest (Claude Code `/plugin marketplace add` entry point)
├── pila                        executable entry-point wrapper (chmod +x);
│                                   portable bash; runtime preflight + nerdctl run
│                                   (DESIGN §6 / §0.5)
├── Dockerfile                  container image recipe; built locally on first
│                                   run, tagged `pila:<VERSION>` (§0.5)
├── orchestrator/pila.py        the orchestrator — all control flow (chmod +x)
├── prompts/
│   ├── classifier.md              Phase 1 worker system prompt
│   ├── planner.md                 Phase 2 worker system prompt
│   ├── reconciler.md              Phase 2½ worker — resolve cross-domain
│   │                              capability-tag drift between planners
│   ├── implementer.md             Phase 5 implementer worker system prompt
│   ├── conformer.md               Phase 5 post-work conformance worker (DESIGN §9)
│   ├── integrator.md              conflict-resolution worker system prompt
│   └── judge.md                  LLM judge worker — 3-dimensional rubric for
│                                  reviewing captured call records
├── scripts/
│   ├── setup-run.sh               create per-run branch + worktree (idempotent)
│   ├── new-worktree.sh            create/reuse a per-subtask worktree (per-run scoped)
│   ├── integrate.sh               merge a subtask branch into the per-run branch
│   ├── finalize.sh                verify the run branch exists and is non-empty; ready for push
│   ├── cleanup.sh                 remove worktrees / branches (default: scoped to one run)
│   ├── container-entry.sh         container PID 1: `cd /work && exec python3 orchestrator/pila.py`
│   ├── install.sh                 one-command installer (curl | bash); preflight git/claude/curl +
│   │                               runtime preflight (colima / nerdctl) + clones + symlinks
│   └── remote/
│       └── build-push.sh          build and push a self-contained image for Fly.io Machines;
│                                   the baked /work/.pila-image/ lets the image run without
│                                   a bind mount (§0.5 "Registry publish path")
├── commands/pila.md            thin plugin skill — launches the orchestrator
├── skills/
│   ├── judge-llm-batch/SKILL.md  post-run judge skill — scores a batch of captured
│   │                              LLM calls against a 3-dimensional accuracy rubric
│   └── llm-self-heal/SKILL.md    post-run self-heal skill — autonomous loop that
│                                  proposes and measures prompt patches for failing
│                                  call_types; uses judge verdicts as the signal
├── docs/DESIGN.md                 the theory (architecture and rationale)
├── docs/IMPLEMENTATION.md         this document
├── tests/                         pytest suite (see §10)
├── pytest.ini                     pytest configuration
└── README.md                      top-level user-facing readme
```

Maps to `DESIGN.md`: §3 (architecture / phases), §2 (why a program, not a skill).

---

## 2. Installation and usage

```bash
# From the root of the target git repository:
pila "Fix the login timeout bug and add a regression test"

# Or pass a path to a .txt / .md file whose contents are the task — useful
# for multi-paragraph briefs that are awkward to quote on the shell:
pila path/to/task.md

# Resume an interrupted run. Auto-picks if exactly one in-flight run exists;
# requires --run-id otherwise (see `pila --list` to enumerate).
pila --resume
pila --resume --run-id bugfix-login-timeout-bug-b81e90

# List in-flight and completed runs in this repository:
pila --list

# Skip the default push + PR at finalize (run completes with the run branch
# local-only; the working branch is unchanged):
pila "task" --no-push
export PILA_NO_PUSH=1

# Route to remote execution (e.g. Fly.io) instead of local nerdctl run:
pila "task" --remote
export PILA_REMOTE=1
# Or commit to pila.toml for a per-repo default:
#   remote = true

# Skip pre-push hooks at finalize (the user's explicit override; defaults off).
# Affects only the final `git push`; worker `git commit` operations inside
# worktrees continue to run all hooks normally.
pila "task" --no-verify

# Opt into clarification (DESIGN §11). Without --clarify (the default),
# the classifier's intent questions are filtered and dropped — the
# implementer makes a best-effort decision documented in its notes.
# Pass --clarify to surface the surviving questions to the user
# (interactively if a TTY, otherwise via pending-questions.json).
pila "task" --clarify

# Pre-supply clarification answers:
pila "task" --answers answers.json

# Override caps. --max-workers also reads PILA_MAX_WORKERS env or
# max_workers in pila.toml; --max-parallel is CLI-only.
pila "task" --max-workers 80 --max-parallel 6
export PILA_MAX_WORKERS=80

# Dial how persistent workers are at building confidence before they exit
# blocked (default: 8 rounds inside each planner / implementer):
pila "task" --confidence-rounds 12
export PILA_CONFIDENCE_ROUNDS=12

# Verbosity controls how much per-worker activity surfaces inline.
# Default is `stream`: one-line summary per worker event. -q drops to
# pila's pre-streaming terse output; -qq is fully quiet (errors
# still emit). -vv adds raw payloads. Per-worker .pila/logs/<sid>.log
# files are always written regardless of level.
pila "task"        # default: stream
pila "task" -q      # normal (pre-streaming)
pila "task" -qq     # quiet (errors only)
pila "task" -vv     # debug
pila "task" --verbosity normal
export PILA_VERBOSITY=stream

# Override the default source-of-truth preference (`both`). CLI flag and
# env var are session-scoped overrides; commit `source_of_truth = ...` in
# pila.toml for a per-repo default.
export PILA_SOURCE_OF_TRUTH=codebase    # or: research, both
pila "task" --source-of-truth codebase

# Select the execution runtime (default: local). `fly` routes each worker
# through Fly.io machines instead of local nerdctl containers.
export PILA_RUNTIME=local               # or: fly
pila "task" --runtime fly

# Choose the model. Without overrides: judgment workers (classifier,
# planner, reconciler, provision, integrator) default to opus; acting
# workers (implementer, conformer) default to sonnet. Use the env var
# for a sticky preference, the CLI flag for a one-off, or pila.toml
# for the committed repo default. Per-worker overrides also exist —
# see §2.
export PILA_MODEL=sonnet                # or: opus, haiku
pila "task" --model opus
pila "task" --model-implementer opus --model-classifier haiku

# Telemetry: on by default; disable with --no-telemetry or env var:
pila "task" --no-telemetry
export PILA_TELEMETRY=0
# Override output subdirectory (default: <run-dir>/events/):
pila "task" --telemetry-dir my-events
export PILA_TELEMETRY_DIR=my-events
# Override judge/heal output subdirectories:
pila "task" --judge-dir my-judge --heal-dir my-heal
export PILA_JUDGE_DIR=my-judge
export PILA_HEAL_DIR=my-heal

# Judge and heal model overrides (default: sonnet for throughput):
pila "task" --judge-model opus --heal-model opus
export PILA_MODEL_JUDGE=sonnet
export PILA_MODEL_HEAL=sonnet

# Heal-loop convergence knobs (defaults shown):
pila "task" --heal-max-rounds 10 --heal-success-threshold 0.9
export PILA_HEAL_MAX_ROUNDS=10
export PILA_HEAL_SUCCESS_THRESHOLD=0.9

# Diagnostic toggle for the next silent-hang reproduction. When set,
# every `claude -p` worker subprocess inherits DEBUG=* and
# ANTHROPIC_LOG=debug so its internal state surfaces on stderr — the
# idle watchdog (worker_idle_warn_sec, see §Caps) then flushes a tail
# of that stderr alongside its silence warning. Off by default because
# verbose CLI logging is noisy on healthy runs.
export PILA_WORKER_DEBUG=1
pila "task"

# Run post-run skill phases against an existing run's captured LLM calls.
# --phase judge: score every call in calls.ndjson with the 3-dim judge rubric
#   and write verdict files to <run-dir>/<judge-dir>/.
# --phase heal: read the judge index for failing call_types and run the
#   self-heal loop for each; if no judge index exists yet, runs judge first.
# Use --run-id to select a run when multiple exist; auto-picks when only one.
pila --phase judge --run-id bugfix-login-timeout-bug-b81e90
pila --phase heal  --run-id bugfix-login-timeout-bug-b81e90
# Combine with heal-loop knobs:
pila --phase heal --heal-max-rounds 5 --heal-success-threshold 0.8

# Recommended backstop for worker auto-compaction
# (Claude Code CLI variable — not consumed by pila itself):
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70
```

Requirements: the `claude` CLI on `PATH` and logged in interactively (no API
key — subscription auth); `git`; a git repository with `user.email` and
`user.name` configured; a container runtime (colima on macOS, nerdctl +
containerd on Linux — see `docs/INSTALL.md`). Python is provisioned inside
the container by the image (Debian 12's `python3` 3.11); the host does not
need Python. The launcher's `--version` fast path returns without starting
a container.

Via the plugin skill, from inside Claude Code (after
`/plugin marketplace add enricai/pila` and
`/plugin install pila@enricai-pila` — see §0):

```
/pila <task>
```

### Source-of-truth preference

For feature work, pila needs to know whether to draw conventions from the
codebase, from online research, or from both (codebase first; research as
fallback). Resolution order (highest priority first):

1. **`--source-of-truth`** CLI flag, values `codebase` | `research` | `both`.
   Argparse rejects anything else before the orchestrator runs.

2. **`PILA_SOURCE_OF_TRUTH`** environment variable, same value set.

3. **`pila.toml` at the repo root** (committed, so the preference travels
   with the repo). Plain `key=value` syntax:

   ```
   source_of_truth = codebase
   ```

4. **Default `both`.** When unset, pila runs feature tasks with
   `source_of_truth = both` — codebase patterns first, with researched
   best-practice standards as a fallback where the codebase is insufficient.
   The preference is never surfaced as an interactive question; setting it
   explicitly (CLI, env, or file) overrides the default.

An invalid value in env or file is rejected at startup via `die()` — bad
config is caught before any worker spawns.

> The CLI/env > file order reflects that the CLI flag and env var are
> session-scoped knobs (a user reaching for them is making a one-off
> override), while `pila.toml` is the committed default for the repo.

### Clarification preference

By default pila runs without surfacing intent questions to the user
(DESIGN §11). The classifier still runs the codebase→research filter and
the implementer still applies it before any mid-execution decision —
"no questions" never means "skip the rigor." Pass `--clarify` to opt
into surfacing the surviving questions. Resolution order (highest
priority first):

1. **`--clarify`** CLI flag (action=`store_true`).
2. **`PILA_CLARIFY`** environment variable (boolean, parsed by
   `_parse_bool_envtoml`: 1/0, true/false, yes/no, on/off).
3. **`pila.toml` at the repo root** with `clarify = true`.
4. **Default `False`.** No questions are surfaced; the implementer
   makes a best-effort decision and documents it in
   `investigation_notes`.

An invalid value in env or file is rejected at startup via `die()` —
same shape as `--source-of-truth` resolution.

### Runtime mode

Controls which execution backend runs the per-subtask worker containers.
`local` uses the local nerdctl/containerd runtime (the existing behavior);
`fly` routes each worker through Fly.io machines. Default is `local` so
existing behavior is unchanged for users who have not opted in.

Resolution order (highest priority first):

1. **`--runtime`** CLI flag, values `local` | `fly`. Argparse rejects
   anything else before the orchestrator runs.

2. **`PILA_RUNTIME`** environment variable, same value set.

3. **`pila.toml` at the repo root** with key `runtime`. Plain
   `key=value` syntax:

   ```
   runtime = fly
   ```

4. **Default `local`.** When unset, pila runs workers in the local
   container runtime. The default preserves all existing behavior
   for users who have not configured a remote runtime.

An invalid value in env or file is rejected at startup via `die()` — bad
config is caught before any worker spawns. Valid values are
`{local, fly}`.

> The CLI/env > file order reflects the same session-scoped vs.
> committed-default split as `--source-of-truth`: the CLI flag and env
> var are one-off overrides, while `pila.toml` is the per-repo default.

Maps to: `resolve_source_of_truth` resolution pattern in `pila.py`
(`_read_toml_key` + env + CLI precedence). The code counterpart
(`resolve_runtime`) and the `PILA_RUNTIME` / `--runtime` argparse wiring
are specified here and implemented by downstream subtasks.

### Prompt loading and the shared filter fragment

Worker prompts are loaded by `load_prompt(name)` in
`orchestrator/pila.py` rather than `read_text()` directly. The
helper expands any `{{include: _foo.md}}` placeholder by inlining the
named fragment from `prompts/`. Fragments prefixed with `_` are
internal includes — never standalone worker prompts. Today there is
one fragment, `prompts/_clarification_filter.md`, included by
`prompts/classifier.md` and `prompts/implementer.md`. It is the single
source of truth for the codebase→research→ask wording shown to
workers; DESIGN.md §11 is the architectural spec that the fragment
must conform to.

### Confidence rounds

Planners and implementers self-gate on confidence (DESIGN §8) and loop their
evidence-gate up to `confidence_rounds` times before they exit `blocked`.
Default 8. Increase if the user wants workers to push harder on hard
diagnoses; decrease for cheaper, faster runs that accept earlier
escalations.

Resolution order (highest priority first):

1. **`--confidence-rounds N`** CLI flag. Argparse rejects non-positive
   integers.
2. **`PILA_CONFIDENCE_ROUNDS`** environment variable, same value set.
3. **`pila.toml` at the repo root**, `confidence_rounds = N`.
4. **Default `8`** (`DEFAULT_CAPS["confidence_rounds"]`).

An invalid value in env or file is rejected at startup via `die()`. The
resolved value is written into `caps["confidence_rounds"]` and passed in
each planner / implementer's user prompt — the cap is prompt-governed (see
§6 "Worker-internal caps" and DESIGN §13), the user-visible knob is real.

### Verbosity

Controls how much of the per-worker activity surfaces to the
orchestrator log. Per-worker `.pila/logs/<sid>.log` files are
always written with the full raw event stream — verbosity governs
only the *inline* summary lines. Four named levels with stackable
`-v`/`-q` shortcuts, following the clig.dev / cargo / kubectl
convention.

| Level    | Flag             | What you see inline |
| -------- | ---------------- | ------------------- |
| `quiet`  | `-qq` / `--verbosity quiet` | Phase boundaries, final result, errors only |
| `normal` | `-q` | Phase boundaries + per-subtask status changes (pila's pre-streaming behavior) |
| `stream` | `-v` / (default) | `normal` + one-line summary per worker event |
| `debug`  | `-vv` / `--verbosity debug` | `stream` + raw event payloads, tool I/O, schema diffs, retry diagnostics |

Resolution order (highest priority first):

1. **`--verbosity LEVEL`** CLI flag, values `quiet` / `normal` /
   `stream` / `debug`. Argparse rejects anything else.
2. **`-v` / `-vv` / `-q` / `-qq`** shortcuts. These anchor to
   `normal` (not to the resolved default), so `-v` always means
   "show me the streaming feature" and `-q` always means "back to
   the pre-streaming terse output", independent of what
   env-var / TOML defaults are set to.
3. **`PILA_VERBOSITY`** environment variable.
4. **`pila.toml`**, `verbosity = "stream"`.
5. **Default `stream`** (`VERBOSITY_DEFAULT`).

An invalid value in env or file is rejected at startup via `die()`.
Errors always emit at every level (clig.dev "errors emit at every
level" anti-pattern guard) — `quiet` does NOT suppress error
messages, only the per-event chatter.

The resolved value lives on `st.data["verbosity"]` and is
re-resolved fresh on every run, including `--resume` — the user
can dial up or down at resume time without editing state.

### Inspect directories

Extra directories the inspect-bucket workers (classifier, planner,
reconciler, provision) may read. Forwarded to each `claude -p` invocation as
one `--add-dir` flag per entry. Use this when a task references a
sibling repo outside the current repo cwd — for example, "compare
how beacon and pila handle X, beacon is at `~/src/enric/beacon`":
without `--inspect-dir ~/src/enric/beacon`, the classifier and
planner cannot `Read`/`Grep`/`Glob` that path, and an attempt to
fall back to `ls`/`find` is blocked by the workspace sandbox even
though `INSPECT_TOOLS` allowlists those verbs.

Resolution order (highest priority first):

1. **`--inspect-dir PATH`** CLI flag, repeatable.
2. **`PILA_INSPECT_DIRS`** environment variable, colon-separated.
3. **`pila.toml`**, `inspect_dirs = "/abs/path/a,/abs/path/b"`
   (a comma-separated string, parsed by `_read_toml_key`).
4. **Default** `[]` (no extra directories).

Paths are expanded (`~` → `$HOME`) and resolved to absolute form at
startup. Duplicates are removed. The resolved list lives on
`st.data["inspect_dirs"]` and is re-resolved fresh on every run,
including `--resume`, so the user can add or remove paths without
editing state.

This applies only to inspect-bucket workers. Acting workers
(implementer, integrator, conformer) run inside the wave's worktree.
Those workers have `--dangerously-skip-permissions` and operate on the
worktree copy, not the user's wider filesystem — `--add-dir` is
unneeded.

### Telemetry

Controls whether pila writes NDJSON telemetry events for LLM calls. Events
land in `<run-dir>/<telemetry_subdir>/` — already under `.pila/` and thus
covered by the existing `.gitignore` exclusion. Telemetry is on by default.

Resolution order (highest priority first):

1. **`--telemetry` / `--no-telemetry`** CLI flags (mutually exclusive).
2. **`PILA_TELEMETRY`** environment variable, boolean spellings
   (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`).
3. **`pila.toml`**, `telemetry = true|false`.
4. **Default `True`** (`TELEMETRY_DEFAULT`).

An invalid boolean in env or file is rejected at startup via `die()`.

### Telemetry directory

The subdirectory name (relative to `<run-dir>`) where telemetry NDJSON event
files are written.

Resolution order (highest priority first):

1. **`--telemetry-dir DIR`** CLI flag.
2. **`PILA_TELEMETRY_DIR`** environment variable.
3. **`pila.toml`**, `telemetry_dir = "events"`.
4. **Default `"events"`** (`TELEMETRY_SUBDIR_DEFAULT`).

### Judge output directory

The subdirectory name (relative to `<run-dir>`) where LLM judge output files
are written.

Resolution order (highest priority first):

1. **`--judge-dir DIR`** CLI flag.
2. **`PILA_JUDGE_DIR`** environment variable.
3. **`pila.toml`**, `judge_dir = "judge-out"`.
4. **Default `"judge-out"`** (`JUDGE_DIR_DEFAULT`).

### Heal output directory

The subdirectory name (relative to `<run-dir>`) where LLM self-heal loop output
files are written.

Resolution order (highest priority first):

1. **`--heal-dir DIR`** CLI flag.
2. **`PILA_HEAL_DIR`** environment variable.
3. **`pila.toml`**, `heal_dir = "heal-out"`.
4. **Default `"heal-out"`** (`HEAL_DIR_DEFAULT`).

### Judge model

The `claude` model alias used when the judge skill spawns a worker to score a
batch of captured calls. The judge does not require broad-context judgment like
the orchestrator's core workers — `sonnet` is the right default for throughput.

Resolution order (highest priority first):

1. **`--judge-model MODEL`** CLI flag.
2. **`PILA_MODEL_JUDGE`** environment variable.
3. **`pila.toml`**, `model_judge = "sonnet"`.
4. **Default `"sonnet"`** (`MODEL_DEFAULT_PER_WORKER["judge"]`).

### Heal model

The `claude` model alias used when the self-heal skill spawns workers for patch
generation and patched-arm replay.

Resolution order (highest priority first):

1. **`--heal-model MODEL`** CLI flag.
2. **`PILA_MODEL_HEAL`** environment variable.
3. **`pila.toml`**, `model_heal = "sonnet"`.
4. **Default `"sonnet"`** (`MODEL_DEFAULT_PER_WORKER["heal"]`).

### Heal-loop convergence parameters

Knobs governing the self-heal loop's iteration limit, pass-rate target, plateau
detection, and budget guard. All default values match Beacon's `DEFAULT_CONFIG`
(prior art at `scripts/heal-loop.ts:154`).

| Knob | CLI flag | Env var | TOML key | Default |
|------|----------|---------|----------|---------|
| Max iterations per call_type | `--heal-max-rounds N` | `PILA_HEAL_MAX_ROUNDS` | `heal_max_rounds = 10` | `10` (`HEAL_MAX_ROUNDS_DEFAULT`) |
| Success pass-rate threshold | `--heal-success-threshold F` | `PILA_HEAL_SUCCESS_THRESHOLD` | `heal_success_threshold = 0.9` | `0.9` (`HEAL_SUCCESS_THRESHOLD_DEFAULT`) |
| Plateau detection window | — | — | — | `3` (`HEAL_PLATEAU_WINDOW_DEFAULT`; not user-tunable) |
| Plateau minimum delta | — | — | — | `0.03` (`HEAL_PLATEAU_DELTA_DEFAULT`; not user-tunable) |
| Per-call_type replay count | — | — | — | `5` (`HEAL_N_REPLAYS_DEFAULT`; not user-tunable) |

The plateau window, plateau delta, and replay count are not currently exposed
as CLI/env/TOML knobs — they are implementation constants. Only the user-facing
knobs (`--heal-max-rounds`, `--heal-success-threshold`) are CLI/env/TOML
resolvable. Resolution for both follows the standard precedence: CLI flag →
env var → `pila.toml` → default.

### Model selection

Every worker shells out to `claude -p`. The model passed via `--model` to that
subprocess is resolved per worker type, so the same run can use `opus` for
judgment work and `sonnet` for high-throughput implementation. Valid values:
`sonnet` | `opus` | `haiku` (aliases — the `claude` CLI resolves them to the
current model version).

**Per-worker defaults: Opus for judgment, Sonnet for implementation and post-run analysis.**
Workers that exercise broad-context judgment (classify the task, decompose
into subtasks, reconcile cross-domain coupling, resolve merge conflicts
behaviorally, check criteria) default to Opus. The implementer, judge, and
heal workers — which execute concrete tasks with high throughput requirements
— default to Sonnet.

| Worker       | Default | Why |
|--------------|---------|-----|
| classifier   | opus    | global judgment over the task description |
| planner      | opus    | decomposition is the load-bearing judgment step |
| reconciler   | opus    | cross-domain tag equivalence is judgment |
| provision    | opus    | fallback when the deterministic lockfile-detection table returns empty (DESIGN §6½); reads README + configs to emit an install recipe — judgment over arbitrary repo shapes |
| integrator   | opus    | behavioral conflict resolution; a wrong merge silently corrupts integrated state |
| implementer  | sonnet  | concrete subtask execution; Sonnet's throughput is the right tradeoff |
| conformer    | sonnet  | reads a diff and runs commands; same throughput-first profile as implementer; the phase is advisory so a borderline judgment call costs at most a warning |
| judge        | sonnet  | scoring a batch of captured calls; throughput matters more than broad judgment |
| heal (patch) | sonnet  | patch generation and replay; throughput matters more than broad judgment |

`MODEL_DEFAULT` is the global default (`opus`); `MODEL_DEFAULT_PER_WORKER`
overrides it for specific workers (`implementer`, `conformer`, `judge`, and
`heal` all default to `sonnet`).

Resolution order for each worker type `W` (highest priority first):

1. **`--model-<W>`** CLI flag (e.g. `--model-implementer opus`)
2. **`--model`** CLI flag (sets the global default for this run)
3. **`PILA_MODEL_<W>`** env var (e.g. `PILA_MODEL_IMPLEMENTER=opus`)
4. **`PILA_MODEL`** env var (sets the global default)
5. **`model_<w>`** key in `pila.toml`
6. **`model`** key in `pila.toml`
7. **Per-worker default** from `MODEL_DEFAULT_PER_WORKER`
8. **Global default `MODEL_DEFAULT`** (`opus`)

Ten worker types, each independently overridable:

| Worker       | env var                       | CLI flag                | TOML key            |
|--------------|-------------------------------|-------------------------|---------------------|
| (global)     | `PILA_MODEL`              | `--model`               | `model`             |
| classifier   | `PILA_MODEL_CLASSIFIER`   | `--model-classifier`    | `model_classifier`  |
| planner      | `PILA_MODEL_PLANNER`      | `--model-planner`       | `model_planner`     |
| reconciler   | `PILA_MODEL_RECONCILER`   | `--model-reconciler`    | `model_reconciler`  |
| provision    | `PILA_MODEL_PROVISION`    | `--model-provision`     | `model_provision`   |
| implementer  | `PILA_MODEL_IMPLEMENTER`  | `--model-implementer`   | `model_implementer` |
| integrator   | `PILA_MODEL_INTEGRATOR`   | `--model-integrator`    | `model_integrator`  |
| conformer    | `PILA_MODEL_CONFORMER`    | `--model-conformer`     | `model_conformer`   |
| judge        | `PILA_MODEL_JUDGE`        | `--judge-model`         | `model_judge`       |
| heal         | `PILA_MODEL_HEAL`         | `--heal-model`          | `model_heal`        |

Note: `judge` and `heal` use dedicated CLI flags (`--judge-model`, `--heal-model`)
rather than the `--model-<W>` pattern used by orchestrator workers, because they
are post-run skill workers invoked outside the main orchestrate loop and do not
participate in the `--model` global-default resolution path.

An invalid value in env or file is rejected at startup via `die()`. CLI
values are validated by argparse `choices=` and rejected with the standard
argparse error.

**Cost note:** Opus is materially more expensive than Sonnet. A user who
wants the old all-Sonnet behavior sets `PILA_MODEL=sonnet` (or
`--model sonnet`). Per-worker overrides (`--model-planner sonnet`) let
users selectively de-escalate individual workers.

Models are not persisted in `.pila/state.json`. On `--resume`, models are
re-resolved from the current environment, so changing `PILA_MODEL` between
the original run and the resume is intentional and takes effect.

### The `--answers` file

A JSON object keyed by classifier-assigned question `id`. Optionally
includes a `source_of_truth` key set to `"codebase"`, `"research"`, or
`"both"` to override the resolved preference for this run:

```json
{ "q1": "answer text", "source_of_truth": "codebase" }
```

Maps to `DESIGN.md`: §11 (clarification procedure).

---

## 3. Worker invocation contract

Each worker is one `claude -p` headless process. Flags used:

| Flag | Purpose |
|------|---------|
| `-p` | non-interactive single-shot |
| `--output-format stream-json --verbose` | streams one JSON event per stdout line as the worker runs; the final `result` event is the envelope (same shape as `--output-format json`'s single output — `cost`, `usage`, `terminal_reason`, `structured_output`). `_invoke` writes raw events to `.pila/logs/<sid>.log` and emits per-event inline summaries gated by `state.json["verbosity"]` |
| `--json-schema <inline>` | the payload schema; serialized inline as a JSON string — a file path is silently ignored (verified against Claude Code 2.1.143) |
| `--append-system-prompt` | injects the worker's role prompt — read from `prompts/*.md` for classifier/planner/reconciler/provision/implementer/integrator/conformer |
| `--allowedTools` | tool allowlist; two buckets — **inspect** (`INSPECT_TOOLS`: read set + allowlisted `Bash(ls:*)` / `Bash(find:*)` / `Bash(cat:*)` / … for cross-cwd read-only inspection, **no Write/Edit**) for classifier, planner, reconciler, and provision; **acting** (`ACT_TOOLS`: read set + Bash/Write/Edit) for implementer, integrator, and conformer. The acting bucket keeps Bash unrestricted because its workers run with `--dangerously-skip-permissions`; the inspect bucket uses `Bash(<verb>:*)` prefix patterns to pre-approve specific read-only verbs at the CLI level — no Write/Edit so the prompt's "you do not modify code" rule is enforced mechanically per DESIGN §12 |
| `--max-turns` | per-worker turn cap (values in §6) |
| `--model` | model alias for this worker — `sonnet` / `opus` / `haiku`. Value comes from per-worker resolution (see §2 *Model selection*) |
| `--add-dir` | repeated per entry in `state.json["inspect_dirs"]` (forwarded by `claude_p`'s `add_dirs` param). Used only by inspect-bucket workers (classifier, planner, reconciler, provision) so their sandboxed Read/Grep/Glob and allowlisted Bash verbs can reach sibling repos referenced in the task. See §2 *Inspect directories* |
| `--dangerously-skip-permissions` | acting workers (implementer, integrator, conformer) — suppresses all permission prompts for unattended Bash and file writes. **Not** applied to inspect workers — they run in the real repo cwd (no worktree isolation), so the blast-radius assumption that justifies skip-permissions doesn't hold. The `Bash(<verb>:*)` patterns in `INSPECT_TOOLS` pre-approve listed verbs at the CLI level; anything else (e.g. `rm`, redirect-to-file) falls through and is rejected in non-interactive mode |

`claude_p()` is `async`; every caller awaits it. Internally it awaits
`_invoke()`, which spawns the worker via the `run_proc` helper
(`asyncio.create_subprocess_exec` + `communicate()` with an optional timeout).
Shell scripts in `scripts/*.sh` are invoked via `run_script()`, a thin async
wrapper that resolves the script path and forwards to `run_proc`.

The validated payload is read from `structured_output` on the envelope. On a
missing or schema-invalid payload, `claude_p()` retries once with the violation
quoted into the prompt; a second failure raises `WorkerError`.

`WorkerError` handling by worker type — per DESIGN §7's salvage rule
("salvage if there is something to salvage; abort cleanly otherwise"):
- **implementer** — `run_implementer()` catches it, converts to an
  `incomplete-handoff` result; a fresh implementer continues from the checkpoint.
- **conformer** — `run_conformer()` catches it and returns `None`;
  `settle_subtask` records a `conformer crashed` entry in
  `conformance_warnings` and the subtask still returns `complete` (DESIGN §9
  *Post-work conformance*: the phase is advisory and never fails the subtask).
- **classifier, planner, reconciler, provision, integrator** — not caught
  locally; propagates to `main()`, which aborts with state saved for
  `--resume`.

`claude_p()` logs a non-fatal warning when the envelope `terminal_reason` is not
`"completed"` (e.g. `"max_turns"`).

Maps to `DESIGN.md`: §7 (worker contract), §2 (CLI subprocess form).

---

## 4. Phase walkthrough (`pila.py`)

| Phase | Function(s) | What it does |
|-------|-------------|--------------|
| Preflight | `preflight` | git identity, clean working tree, `claude` CLI version, live `claude -p` smoke test. Run-id collisions are detected later in the flow (filesystem side in `State.rename_to()` post-classify; git side in `setup-run.sh`'s branch-creation step) — they cannot be checked in preflight because the final `run_id` isn't known until phase_classify completes. Smoke test bypassed by `--skip-smoke`; preflight skipped entirely on `--resume` |
| 1 Classify | `phase_classify` | one classifier worker → categories + questions. Returned categories are filtered against the 8-name whitelist in `CATEGORIES` (mirrors DESIGN §4); `die()` if none survive |
| 1½ Provision | `phase_provision` | per-repo dep **detection** (DESIGN §6½ "Worker-driven install"). Runs after classify so a docs-only run can short-circuit to `kind: none`. Five steps: `.pila-setup.sh` hook if present → `synth_mise_go_override()` if `go.mod` lacks a `.go-version` / mise.toml go pin → `mise install` at the repo root (reads `.tool-versions` natively; `.nvmrc` / `.python-version` / `.ruby-version` / `rust-toolchain.toml` via image-set `MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS`) → version capture via `mise ls --current --json` → `detect_recipe_from_lockfiles()` table-first, falls back to a `provision` worker on table miss. The recipe is **persisted to `st.data["provision"]["recipe"]` and injected into implementer/conformer prompts as a `PROVISION_RECIPE:` block** — workers run install commands themselves in their own worktrees (not the orchestrator at `repo_root`, which would clobber the host's bind-mounted checkout). The synth-go-pin env var `MISE_OVERRIDE_CONFIG_FILENAMES` is exported to `os.environ` so all downstream worker subprocesses inherit it. `mise install` and `.pila-setup.sh` run through `run_streaming` so their output is visible live. Skipped on `--resume` (whole fresh-run else-branch is); the env var is re-exported from persisted state on resume. |
| 0 Clarify | `gather_answers` | source-of-truth is satisfied non-interactively from the resolved preference (default `both`). Intent questions from the classifier are dropped by default; pass `--clarify` to surface them. With `--clarify` + interactive: collect; with `--clarify` + non-interactive: write `pending-questions.json`, exit code 10 (DESIGN §11) |
| 2 Plan | `phase_plan` | one planner worker per category, awaited concurrently via `gather_or_cancel` (a small wrapper around `asyncio.gather` defined in `pila.py`) under an `asyncio.Semaphore(max_parallel)`; the first worker exception cancels its siblings and propagates to `main()` |
| 2½ Reconcile | `phase_reconcile` | compute set of `requires` capability tags with no matching `provides` across merged planner output. If empty: short-circuit (no worker spawn, plan unchanged). Else: spawn one reconciler worker that emits renames / added_provides / added_subtasks / unresolvable. Orchestrator applies the first three mechanically; if `unresolvable` is non-empty, `die()` with the reconciler's diagnosis (DESIGN §5, §14). |
| 3 Schedule | `schedule`, `validate_plan` | merge plans, build the global DAG, Kahn topological sort into waves; cycle → `die()` |
| 4 Setup | `phase_execute` head → `setup-run.sh` | create the run branch `pila/runs/<run-id>` and its worktree (per-run, isolated from any other run) |
| 5 Execute | `phase_execute`, `settle_subtask`, `integrate_wave` | per wave: implementers awaited concurrently via `gather_or_cancel` under a fresh `asyncio.Semaphore(max_parallel)` (separate instance from Phase 2's), then integrate, then run a deterministic conflict-marker scan on the integrated worktree. `settle_subtask` runs the **post-work conformance phase** (DESIGN §9 *Post-work conformance*) on the success path before returning — `discover_rules_files` → `run_conformer` loop (≤ `conformance_rounds`) → re-run the per-subtask mechanical-precondition gates (`check_branch_has_commits`, dirty-worktree, `check_diff_scope`) against the conformer's commits → attach `conformance_warnings` to the result. The phase is advisory: residuals, build/lint/test failures, gate violations on conformer commits, and `WorkerError` all surface as warnings, never as `failed`/`blocked`. If any subtask in the wave ends `blocked` or `failed`, `phase_execute` aborts the run *before* `integrate_wave` is called — the blocker is recorded in `state.json` and the run resumes with `--resume`. There is no LLM wave-level re-validation; the §8 confidence gate is the load-bearing per-subtask signal, and `scan_conflict_markers` is the deterministic post-integration safety net |
| 6 Finalize | `phase_finalize` → `finalize.sh`, `cleanup.sh`; launcher then pushes on host | verify the run branch is non-empty; record `finished_at` in `run.json`; delete the per-subtask branches `pila/subtasks/<run-id>/*` (the run branch is **kept** as the PR head; state dir is kept as audit). **The push + PR step has moved to the host launcher** (DESIGN §6 *Finalization*) — `phase_finalize` writes the sentinel and exits; the launcher polls `run.json`, then runs `git push pila/runs/<run-id>` + `gh pr create` on the host using the host's own auth (no in-container forwarding of gh tokens, SSH keys, or agent sockets). The working branch is **not** modified locally — the PR is the proposed integration. |
| Post-run Judge | `phase_judge`, `judge_capture` | standalone post-run phase (not part of main orchestrate flow): reads `calls.ndjson`, runs one `judge_capture()` per record in parallel under `asyncio.Semaphore(max_parallel)`, writes per-record verdicts to `<judge-dir>/<call_id>.json` and a summary `INDEX.json`; uses `prompts/judge.md` rubric |
| Post-run Heal | `HealState`, `heal_baseline`, `heal_apply_patch`, `heal_replay_patched`, `request_patch`, `phase_heal` | heal-loop phases: `HealState` persists failing_samples / baseline / history / best_so_far at `<heal-dir>/<call_type>/state.json`; `heal_baseline(call_type, failing_records, n, heal_dir, caps, st, models)` runs n unpatched replays per record + judge, writes baseline verdicts + state; `heal_apply_patch(call_type, iter_n, patch_text, anchor_match, heal_dir, failing_records)` materialises patched prompts under `iter-<N>/patched-prompts/`; `heal_replay_patched(call_type, iter_n, n, heal_dir, caps, st, models)` runs n patched replays per record + judge, appends iteration record to state.history; `request_patch(state, iter_n, st, caps, models)` invokes the `patch_generator` worker (schema `SCHEMAS["patch_generator"]`, SID `heal-patch-<call_type>-iter<N>`, prompt from `prompts/patch_generator.md`) and returns `(anchor, replacement)` — raises `ValueError` if the returned anchor is not a literal substring of the resolved prompt body (code-enforced per the prompts-are-advisory principle); `phase_heal(call_type, failing_records, heal_dir, caps, st, models, request_patch_fn=None, n, config)` drives the full baseline→loop→report cycle; `request_patch_fn` defaults to the real `request_patch` when `None`, or accepts a sync/async 2-arg stub for testing |

`phase_classify` runs before `gather_answers` because the question set depends
on the classification.

Between Phase 3 and Phase 4, `write_plan()` persists the merged plan
(`.pila/plan.json`) and per-subtask spec files
(`.pila/subtasks/<id>.json`), and `detect_test_runner()` scans for a
deterministic test harness (pytest, npm, go, cargo, make) — stored in
`state['test_runner']` for the conformance phase's advisory test run
(consumed via `_infer_build_lint_test()`).

Maps to `DESIGN.md`: §3.

---

## 5. Deterministic enforcement points

All in `pila.py`, in execution order. This is the concrete catalogue behind
`DESIGN.md` §12 ("prompts advisory, code enforces").

### Preflight (before any LLM work)
| Check | Catches |
|-------|---------|
| `resolve_source_of_truth()` at startup | invalid value in `pila.toml`, `PILA_SOURCE_OF_TRUTH`, or `--source-of-truth` — caught before any worker spawns, not mid-planner |
| `resolve_models()` at startup | invalid model alias in `pila.toml`, any `PILA_MODEL[_*]` env var, or any `--model[-*]` CLI flag — caught before any worker spawns |
| `git user.email` / `user.name` set | commits would fail silently without identity |
| working tree clean | dirty tree → ambiguous diffs, corrupt merge history |
| `claude --version` ≥ `MIN_CLAUDE_CLI` (currently `(2, 1, 22)`) | CLI too old for `--json-schema` (introduced for `claude -p` in v2.1.22) — replaces the cryptic "unknown option" message a stale CLI used to produce |
| `_check_gh_cli(no_push)` — `gh` installed, `gh auth status` ok, `origin` remote present | finalize would fail at push/PR after the full run already ran. Short-circuited when `--no-push` is passed (env / TOML mirrors). |
| live `claude -p` smoke test | auth failure or network problem |

Run-id collisions are detected outside preflight because the final `run_id` is only known after `phase_classify` returns. There are two natural collision points:

| Check | Where | Catches |
|-------|-------|---------|
| `State.rename_to(new_run_id)` refuses if the target dir exists | `orchestrate()` after `phase_classify` | `.pila/runs/<run-id>/` already exists on disk |
| `setup-run.sh` preserves an existing `pila/runs/<run-id>` branch instead of creating it | wave-execute phase | A pre-existing branch with the same name (treated as a resume; the run picks up wherever the branch was left) |

The bootstrap directory `.pila/runs/_bootstrap-<6hex>/` is used until classify completes; the rename is atomic on POSIX same-filesystem.

`--skip-smoke` bypasses only the live smoke test (used by the test harness); the CLI version check and the `gh` check still run because they are local and read-only, and skipping them would defer a confusing failure to mid-run.

### Phase 1 checks — `phase_classify`
| Check | Catches |
|-------|---------|
| classifier-returned categories filtered against the 8-name whitelist `CATEGORIES` (mirrors DESIGN §4) | classifier hallucinating a category outside the eight |
| `die()` if no category survives the filter | a run with no valid domain for any planner |

### Phase 2½ checks — `phase_reconcile`
| Check | Catches |
|-------|---------|
| reconciler's `unresolvable` array non-empty → `die()` with the worker's diagnosis | genuine gaps where no planner produced a needed capability and no plausible connector subtask can be inferred. Each unresolved `(sid, tag)` pair is annotated with the consuming subtask's producing planner-domain (from `_compute_unresolved_requires`) so the abort message can render `domain/sid` — naming the planner-domain whose plan held the dangling dependency, which is the primary remediation lever for the user. |
| reconciler output validated against `SCHEMAS["reconciler"]` | malformed reconciler response (caught by `claude_p`'s schema gate; structurally invalid output is retried once, then escalated) |
| after applying reconciler output, the unresolved-requires set is recomputed; non-empty → `die()` | the reconciler's renames/added_subtasks/added_provides didn't actually close every gap (e.g., a new subtask itself has unresolved `requires`) — fail-loud rather than progress to `validate_plan` with a still-broken graph |

### Plan validation — `validate_plan` (after scheduling, before persisting the plan)
| Check | Catches |
|-------|---------|
| ids match domain prefix (`bugfix-`, `feat-`, `refactor-`, `perf-`, `test-`, `deps-`, `config-`, `docs-`) | cross-domain collisions, audit ambiguity. The planner's user prompt receives the prefix directly as `ID_PREFIX = CATEGORY_ABBREV[domain] + "-"`, so the prompt cannot drift from the validator's allowlist — both derive from the same `CATEGORY_ABBREV` map (in `pila.py`). |
| no `size: large` subtasks | planner violated the sizing constraint |
| no empty `success_criteria_seed` | implementer has no criteria starting point |
| every `depends_on` id exists | dangling edges silently dropped by the scheduler |
| every `requires` tag has a provider | unresolvable cross-domain dependency |

`warn_cross_planner_file_overlap()` runs immediately after
`phase_reconcile` (before `validate_plan` and the scheduler) and **logs a
warning, never fails**, when two planners' subtasks both list the same
path in `files_likely_touched`. Empirically (May 2026, n=3 historical
runs) failed runs had ≥9 cross-planner overlaps each while the
successful run had zero; the warning surfaces that risk at plan time
instead of waiting for the integrator to crash mid-wave. The reconciler
currently bridges capability-tag vocabulary drift but not file-claim
conflicts — a future-work item is to extend its action vocabulary to
resolve overlaps automatically.

### Per-subtask checks — in `settle_subtask`, every worker result
| Check | Catches | On failure |
|-------|---------|-----------|
| `validate_result()` cross-field invariants | `handoff` with no checkpoint file; `blocked` with no blocker; `failed` with no summary; `needs-clarification` with no `clarification_question` or no `checkpoint_path` | **Terminal** |
| `check_branch_has_commits()` | `complete` claim, nothing committed | **Retryable** |
| dirty worktree check | uncommitted changes that vanish on integration | **Retryable** |
| `check_diff_scope()` | `.pila/` or `.git/` in the diff; any `.claude/` path *except* `.claude/agents/`, `.claude/commands/`, `.claude/skills/` (the documented Claude Code user-deliverable subtrees — implementers may write a subagent/command/skill file there as a legitimate deliverable, but never `settings.json` or any top-level `.claude/` file) | **Terminal** (protected path); scope-volume warning is non-fatal (triggered when `files_likely_touched` is non-empty *and* touched > max(3× expected, 5), or when touched > 15 regardless of the planner's estimate) |
| `validate_checkpoint()` — on `incomplete-handoff` | required section missing; required section empty/whitespace; required section contains only a placeholder token (`none`/`n/a`/`na`/`tbd`/`nothing`/`unknown`/`todo`/`pending`/`—`/`--`/`-`/`?`, trailing `.`/`!`/`?`/`…` ignored and repeated `?` collapsed); a path listed under `## Files touched` no longer exists in the worktree and is not flagged `[deleted]` | returns `blocked` |
| `_retryable_failure(summary)` — on `status='failed'` returned by the worker itself | worker self-report of failure | routed through the retry policy using the worker's `summary` as the reason; because `summary` is freeform text it almost never matches a retryable marker, so in practice a self-reported `failed` is **terminal** on first occurrence |

`validate_result()` accepts a `complete` status regardless of what
`criteria_results` carries — empty, missing, or with `met:false`
entries are all valid. Per DESIGN §8 the criteria file is
informational, not a gate. A worker's unmet-criterion self-report is
recorded on the result for telemetry and surfaces as a warning in
`state.json["conformance"]` alongside the conformance-phase residuals,
but does not affect the subtask's terminal status. The criteria-file
lock (`lock_criteria` / `verify_criteria_lock`) and the
worker-initiated `criteria_revision_proposal` channel were both removed
when the criteria file's load-bearing role retired — see DESIGN §9.

### Per-subtask post-work conformance — in `settle_subtask`, success path only

Triggered only when an implementer's `status: "complete"` has already cleared
every check above (commits present, worktree clean, no protected path
written). None of the other terminal statuses (`incomplete-handoff`,
`needs-clarification`, `blocked`, `failed`) invoke the conformer.
Implements DESIGN §9 *Post-work conformance*.

| Step | Function | Behavior |
|------|----------|----------|
| Discover rules files | `discover_rules_files(repo_root)` | Returns existing paths from a fixed, capped allowlist (`CLAUDE.md`, `AGENTS.md`, `.agent.md`, `.cursorrules`, `.windsurfrules`, `docs/CLAUDE.md`, `docs/AGENTS.md`, `docs/CONVENTIONS.md`, `docs/STYLE.md`, `README.md`, `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/IMPLEMENTATION.md`), deterministic order, never raises. Empty list when nothing matches. |
| Run conformer | `run_conformer()` | One `claude -p` invocation with `ACT_TOOLS`, `--dangerously-skip-permissions`, `SCHEMAS["conformer"]`. Catches `WorkerError` and returns `None` (surfaced as a warning). |
| Validate output | `validate_conformance_result()` | Cross-field invariants — `rule_violations_residual` non-empty requires `rules_files_read` non-empty; each `rule_violations_fixed` item must cite a non-empty `rule` string; each `docs_updates` / `tests_updates` item must cite a `path` that exists. On failure → warning, loop breaks. |
| Re-run gates | `check_branch_has_commits`, dirty-worktree check, `check_diff_scope` | Same functions used on the implementer, re-applied to any new commits the conformer added. A scope-protected-path violation triggers `rollback_conformer_commits()` (reset to `before_sha`) and is recorded as a warning, **not** as `failed` / `blocked`. |
| Loop bound | `caps["conformance_rounds"]` (default 2) | Re-runs the conformer if its output is malformed or residuals remain. Exhausting the cap with residuals still present is a warning, not a failure. |
| Attach result | — | `res["conformance"]` (worker output blob) and `res["conformance_warnings"]` (list of strings) are added to the implementer's result. The subtask still returns `complete`. |

The phase is advisory: **no path through the conformance phase produces a
`failed` or `blocked` subtask status.** Build/lint/test failures, malformed
conformer output, conformer crashes, gate violations on conformer commits,
and exhausted rounds all surface as entries in `conformance_warnings` and as
non-fatal log lines. This is the §12 enforcement boundary for the phase:
*discovery* of rule files, *schema validity* of the conformer's output, and
the *protected-path invariance* across conformer commits are code-enforced;
whether the conformer made the right docs/tests/rule-violation calls is left
to the worker and not second-guessed.

### Wave-level checks (after integration)
| Check | Catches |
|-------|---------|
| `scan_conflict_markers()` | unresolved `<<<<<<<` markers in the run-branch worktree after integration — deterministic safety net |

There is no LLM wave-level re-validation. An earlier version of
`validate_wave` ran a deterministic test-runner fast-path and an LLM
validator over per-subtask criteria, with a re-spawn loop bounded by
`wave_revalidation_rounds`; all of that was removed when the criteria
file's load-bearing role retired (DESIGN §8, §9). Per-subtask quality
is the implementer's confidence gate; the wave-level safety net is the
deterministic conflict-marker scan.

### Post-integrator checks (after an integrator handles a conflict)
These verify the integrator honored DESIGN §6's *behavioral* conflict-
resolution contract — the integrator prompt itself
(`prompts/integrator.md`) carries the behavioral spec (read every
involved subtask's intent, preserve each side's intent, call
irreconcilable cases a `design-conflict`); the orchestrator only checks
the outcome.

| Check | Catches |
|-------|---------|
| `check_merge_committed()` | integrator returned `resolved` but left the worktree mid-merge (`MERGE_HEAD` present) or with staged-uncommitted changes — **terminal**: merge aborted, run stops |
| `check_integrator_commit()` | integrator merge commit touched `.pila/` files — non-fatal warning, recorded to `state.json` |
| integrator status `design-conflict` / `failed` | unresolvable conflict — **terminal**: in-progress merge aborted, the run branch left clean at the last good wave, diagnosis saved, run stops |

### Resume integrity — `validate_resume_state()`
Enforces (one half of) DESIGN §6's "the run branch is the resume contract"
invariant — state.json's `waves`/`completed_waves` say *which* wave to
resume; the never-reset `pila/runs/<run-id>` branch holds *the work*
every prior wave produced. Both must be coherent for resume to be safe.

On `--resume`: asserts `task` is present and non-empty; asserts `waves`,
`completed_waves`, `subtask_status` are well-formed *if present*. `waves` is
intentionally optional — a run interrupted before scheduling has none, and
`main()` handles that case with a clearer message. Rejects corrupt or
hand-edited state without rejecting a legitimately-early interruption.

`orchestrate()` also re-resolves the source-of-truth preference on every
`--resume` and overwrites `state.json`'s `source_of_truth_pref` with the
fresh value, so a change to `pila.toml` or `PILA_SOURCE_OF_TRUTH`
between runs takes effect on resume.

Per-worker models are likewise re-resolved on every `--resume` from the
current CLI flags, env, and `pila.toml`. They are *not* persisted in
`state.json` (they are startup config, not run state), so a change to
`PILA_MODEL`, `--model`, or the per-worker overrides between runs
takes effect immediately on resume.

### Concurrency model
The orchestrator runs on a single `asyncio` event loop. Each `claude -p`
worker is spawned via `asyncio.create_subprocess_exec` (wrapped by the
`run_proc` helper) and awaited; both spawn sites pass
`start_new_session=True` so each worker becomes its own POSIX session and
process-group leader (PGID == PID), isolating it from the orchestrator's
own group. Parallel workers within a wave run concurrently via
`gather_or_cancel` — a small `asyncio.gather` wrapper that, on the first
exception, cancels every other in-flight task and awaits its finalization
before re-raising — under an `asyncio.Semaphore` bounded by
`max_parallel`. Because every mutator runs on the single loop, `State`
carries no lock — coroutines only interleave at `await` points, which
never fall inside a `st.data[k] = v; st.save()` pair. `State.save()`
still writes to a temp file then `os.replace()` for atomicity against
process crash.

Subprocess cleanup is two-layered, addressing two distinct leak classes:

1. **Lifetime descendant tracking (`_DescendantTracker`).** A per-worker
   asyncio task started at spawn polls `_enumerate_descendants(proc.pid)`
   every ~0.5s and accumulates every PID ever observed as a descendant
   of the worker. On every exit path — success AND failure — the
   tracker's `stop_and_reap()` SIGKILLs the accumulated set. This is
   the load-bearing fix for Claude Code's Bash tool with
   `run_in_background: true`: the tool wrapper spawns its user command
   in a detached POSIX session, then the wrapper itself can exit while
   the user command keeps running. By the time `claude -p` exits, the
   backgrounded command has been reparented to PID 1 and is no longer
   reachable via post-hoc PPID walk from the worker — but the tracker
   observed it mid-flight and has its PID. Without lifetime tracking,
   the descendant is invisible to cleanup.

2. **Abnormal-exit subtree termination (`_terminate_proc_tree`).** On
   `KeyboardInterrupt`, `SIGTERM`, `RateLimitedExit`, or any other
   `BaseException`, `run_proc`'s and `_invoke`'s catch-all handlers
   call `_terminate_proc_tree(proc)`. The helper sends SIGTERM to the
   worker's process group (`os.killpg`) AND to every descendant
   currently reachable via PPID walk (`_enumerate_descendants`), waits
   `_PROC_TREE_GRACE_SEC = 2.0` for graceful shutdown, then SIGKILLs
   the survivors via the same two mechanisms. The PPID walk is needed
   because Claude Code's Bash tool subprocesses are in a *different*
   POSIX session than `claude -p` — `killpg(claude_p_pgid)` does not
   reach them, so the walk is the only way to enumerate them while
   the parent chain is still intact. Exception paths run the tracker
   reap *after* `_terminate_proc_tree`, catching any backgrounded
   subprocess that was orphaned during the run.

The two layers compose: `_terminate_proc_tree` is broad and
synchronous (one call, kills attached subtree), the tracker is narrow
and historical (kills only what it observed, including processes
that have since reparented away). Neither alone is sufficient; both
together close the leak.

### Abnormal exit and rate-limit contract (DESIGN §6 *Cleanup on abnormal exit*)

All abnormal exits — Ctrl-C, SIGTERM/SIGHUP, WorkerError, unhandled
exception, or `RateLimitedExit` — route through
`_cleanup_on_abnormal_exit(st, full_purge=False)`. **State.json, the
run branch, per-subtask branches, and implementer checkpoints all
survive**; only worktrees are removed (and re-created idempotently on
`--resume` via `scripts/new-worktree.sh`).

Per-worktree removal has a 240s timeout — calibrated against a real
868 MB / 41k-file worktree (npm install + Next.js build) which takes
~45-90s uncontested, with several-fold growth under N-way concurrent
disk contention. Per-worktree failures (timeout or OS error) are
non-fatal and counted; if any failed, the cleanup emits one closing
log line pointing the user at `scripts/cleanup.sh --run-id <id>` to
finish manually. The pass is best-effort: a stale worktree on disk is
the worst case, not a corrupted run.

Per-worker `subprocess.TimeoutExpired` from `_invoke` (raised when the
worker hits `worker_timeout_sec`, default 5400s / 90 min) is caught
by both `run_implementer` (returns an `incomplete-handoff` envelope,
matching the WorkerError handoff path so settle_subtask's existing
machinery handles it) and `run_conformer` (logs + returns None,
matching the WorkerError advisory-phase semantics). Without these
catches the timeout escapes through the asyncio cancellation chain
into `main()`'s catch-all and dumps a multi-KB traceback — including
the entire `claude -p` command line — to the user's terminal.

`RateLimitedExit` is raised by `detect_session_limit(text)` inside
`_summarize_stream_event` when a worker stream contains the verbatim
Claude Code subscription message
`"You've hit your session limit · resets <h>:<mm><am|pm> (<IANA TZ>)"`,
or by the same function's `rate_limit_event` branch when the
protocol-level event's `status` field falls outside the known-allowed
set `{"allowed", "allowed_warning"}` — a defensive match against
future terminal status strings (Anthropic's terminal value, e.g.
"exceeded" / "denied" / "blocked", is internal and unobserved by us;
matching everything-not-allowed avoids hardcoding a guess that could
go stale). The protocol-level path parses `resetsAt` (a Unix timestamp
in seconds) into a UTC `reset_at`; the text path parses the wall-clock
time + IANA tz. Either source produces a `reset_at: datetime | None`
(parse failure → `None`, never a wrong-time guess) and the raw
message. `main()`'s `except RateLimitedExit` arm: when `reset_at` is
set, run worktree cleanup, sleep until the moment + 30s margin, then
`os.execvp` the launcher (`<PILA_HOME>/pila --resume --run-id <id>`)
to start a fresh orchestrator process (the `--max-workers` budget is
NOT reset across the re-exec: `worker_count` persists in state.json,
so a run that repeatedly hits the rate-limit still respects the
user's cap);
when `reset_at` is None, print the literal message and the manual
resume command, exit with code 75 (`EX_TEMPFAIL`).

**Auto-resume override persistence.** The re-exec passes only
`--resume --run-id <id>` as argv — any CLI overrides on the original
launch (`--model`, `--max-workers`, `--confidence-rounds`,
`--source-of-truth`, `--clarify`, `--no-push`) are **not** propagated
to the fresh process. They fall back to env vars (`PILA_*`) and
`pila.toml` settings, which are re-resolved on every `--resume`
(see "Resume integrity" above). Users who rely on a non-default
setting should configure it via env or `pila.toml` rather than a
single CLI flag, so an auto-resume preserves it. A manual `--resume`
(invoked by the user after the parse-failure exit-75 path) can
re-supply CLI overrides as needed.

Ctrl-C (SIGINT) is **resumable** — same contract as every other
abnormal exit. The explicit "throw this away" gesture is
`scripts/cleanup.sh --run-id <id> --branches`, not Ctrl-C. This was a
behavior change from earlier versions of pila where Ctrl-C ran a full
purge; the old design conflated user intent ("stop this run") with
run lifecycle ("nuke the artifacts").

---

## 6. Caps and their values

Defaults in `DEFAULT_CAPS` and the per-worker `claude_p` call sites.

### Code-enforced caps (the orchestrator counts these)
| Loop | Cap | On cap |
|------|-----|--------|
| subtask continuations (re-spawns of an implementer for the same subtask — both context-exhaustion handoffs *and* mid-execution clarifications consume from the same budget) | 3 (`subtask_continuations`) | return `blocked`; fatal at wave boundary |
| corrective retries of a *retryable* failure per subtask (`failed_retries`) | 1 | return `failed` |
| orchestrator-level conformer rounds per subtask (`conformance_rounds`) | 2 | exit the conformance loop; any residuals become `conformance_warnings` on the subtask result — never `failed` / `blocked` (DESIGN §9 *Post-work conformance*) |
| total worker invocations per run | 60 (`--max-workers`, also `PILA_MAX_WORKERS` env or `max_workers` in `pila.toml`) | abort, state saved for `--resume` |
| concurrent workers within a wave | 4 (`--max-parallel`) | throughput throttle |
| turns per `claude -p` call | per worker (below) | worker stops; implementer → `incomplete-handoff` |
| per-worker wall-clock (`worker_timeout_sec`) | 5400 s (90 min) | worker killed; implementer → `incomplete-handoff` |
| per-worker idle-event warning (`worker_idle_warn_sec`) | 300 s (5 min) | log a `no stdout events in <gap>s` warning naming the worker, its PID, and any stderr tail. Observation-only — the worker is NOT killed; `worker_timeout_sec` remains the only kill. Surfaces silent-hang failures (a worker that never emits its first `system/init` event) so the user is not left with zero feedback between phase start and the 90-min hard kill. |

`--max-turns` by worker: classifier 60, planner 100, integrator 60,
implementer 120, conformer 60, judge 40, heal patch_generator 40. For
the implementer, 120 turns and 90 minutes both apply — whichever trips
first. The conformer cap is lower than the implementer's because its
scope is narrower (read a diff, read a small set of rules files, update
docs/tests, run build/lint/test) and the phase is advisory — running
out of turns becomes a warning, not a failure. The planner cap is the
largest of the inspect-tool workers because the planner drives the §8
confidence loop and is the worker most likely to need additional turns
on heavy domains; a too-tight cap there directly degrades the §8
confidence signal it emits.

The `wave_revalidation_rounds` and `revision_retries` caps were
removed when the wave-level LLM validator and the criteria-revision
channel retired (DESIGN §8, §9). State files from older runs may still
carry the corresponding fields; the orchestrator is read-tolerant of
them.

### Worker-internal caps (prompt-governed — NOT counted by the orchestrator)
These iterate inside one worker; the orchestrator sees only the final result.
The real backstop is the worker's `--max-turns` above.

| Loop | Instructed limit | Instructed outcome |
|------|------------------|--------------------|
| evidence-gate iterations (implementer) | `confidence_rounds` (default 8) | return `blocked` |
| evidence-gate iterations (planner) | `confidence_rounds` (default 8) | emit `status: "blocked"`, empty subtasks, gap analysis |
| validate-against-criteria iterations (implementer) | 5 | return `failed` |

The `confidence_rounds` cap is user-tunable (see §2 "Confidence rounds")
even though the iterations themselves are counted inside the worker. The
guarantee remains prompt-governed per DESIGN §13.

Per DESIGN §10 #1, **granular sizing is the primary defense** against
context exhaustion — these caps are a safety net, not the main path.
If they fire often, the planner is under-decomposing (DESIGN §5); look
there first when handoffs become routine.

Maps to `DESIGN.md`: §13. The code-enforced / prompt-governed split there is
*the* point — do not present the second table as a code guarantee.

### The two-tier retry policy — `_retryable_failure(reason)`
One classifier function decides retryable vs. terminal. It substring-matches
the failure reason; the markers must stay in sync with the strings the check
functions actually emit. The coupling test in
`tests/test_retryable_failure.py` enforces this — if you change a marker
in `_retryable_failure` without updating the matching check string (or
vice versa), the test fails. When adding a new retryable failure mode,
edit `_retryable_failure` and the check function in the same change.

| Failure | Tier | Marker / source |
|---------|------|-----------------|
| branch has no commits ahead of the run branch | Retryable | `"no commits ahead of the run"` from `check_branch_has_commits` |
| worktree left dirty | Retryable | `"uncommitted change"` from the dirty-worktree check |
| `incomplete-handoff` worker produced no checkpoint on disk | Retryable | `reason.startswith("checkpoint_path '")` — the line-2314 prefix of `validate_result`'s incomplete-handoff check. Triggers in two known cases: (1) Claude Code session-limit / rate-limit no-op workers leave no checkpoint (primarily caught by `detect_session_limit()` upstream; this is the safety net for a message-format change), and (2) a worker that hit `--max-turns` with no checkpoint written, which `run_implementer`'s WorkerError handler synthesizes into the same envelope. Both are corrective-note cases. Prefix-match — *not* pair-match on `"checkpoint_path"` + `"does not exist on disk"` — because `validate_result`'s needs-clarification check (line 2350) emits a message containing both substrings but represents a genuinely-broken worker that must stay non-retryable. |
| cross-field invariant violation (other) | Terminal | `validate_result` |
| diff touched a protected path | Terminal | `check_diff_scope` |
| worker-level error (timeout, schema-invalid twice) | Terminal | `WorkerError` path |

`settle_subtask` routes every failure through `_retryable_failure` via the
`fail()` helper. Retryable consumes the retry cap; terminal ends the subtask on
first occurrence.

---

## 6½. Per-repo dependency provisioning

Implements DESIGN §6½. The provision phase fires once per fresh run,
between classify and plan; on `--resume` the whole fresh-run else-branch
of `orchestrate()` is skipped, so no re-fire check is needed.

### Worker registration

`WORKER_TYPES` (`pila.py:285`) gains `"provision"`. `SCHEMAS["provision"]`
(`pila.py:~76`) is the JSON schema for the LLM-fallback recipe:

```python
{
    "type": "object",
    "required": ["recipe"],
    "properties": {
        "recipe": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "command", "working_dir"],
                "properties": {
                    "kind": {"enum": ["install", "build", "none"]},
                    "command": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "working_dir": {"type": "string"},
                    "timeout_s": {"type": "integer", "minimum": 1},
                },
            },
        },
        "confidence": {"type": "string"},
        "notes": {"type": "string"},
    },
}
```

`detect_recipe_from_lockfiles(repo_root) -> list[dict]` is the
deterministic table. It returns a list of `{kind, command, working_dir,
timeout_s}` dicts — possibly empty (table miss → LLM fallback), possibly
multi-entry (polyglot repos like Rails-with-frontend emit *all* matches,
not first-wins).

| Detected file | Emitted command | Notes |
|---|---|---|
| `pnpm-lock.yaml` | `pnpm install --frozen-lockfile` | takes precedence over yarn.lock and package-lock.json |
| `yarn.lock` (no pnpm-lock.yaml) | `yarn install --frozen-lockfile` | |
| `package-lock.json` (neither above) | `npm ci` | |
| `uv.lock` | `uv sync` | |
| `poetry.lock` | `poetry install` | |
| `Pipfile.lock` | `pipenv install` | |
| `go.mod` + `go.sum` | `go mod download` | |
| `Cargo.lock` | `cargo fetch` | |
| `Gemfile.lock` | `bundle install` | |
| anything else | (no entry — caller falls back to LLM worker) | bare `requirements.txt`, bare `pyproject.toml`, Maven (`pom.xml`), Gradle, polyglot Makefile |

`validate_provision_recipe(recipe) -> None` enforces (raises `ValueError`
on violation):
- `command[0]` is in the argv allowlist `{pnpm, npm, yarn, pip, pip3,
  uv, poetry, go, cargo, bundle, gem, mvn, gradle, gradlew, make}`.
- No `sudo` anywhere in the argv.
- No shell metacharacters (`|`, `&`, `;`, `$`, backticks, `>`, `<`, `\n`)
  in any argv element.
- `working_dir` is either `"."` or a relative path with no `..` segments
  and no leading `/`.

### Phase implementation (`phase_provision`)

Insertion point in `orchestrate()`: inside the `else:` (fresh-run)
branch, after the `_write_run_json(...)` block (currently
pila.py:5984) and before `gather_answers(st, supplied)` (currently
pila.py:5989). Step order:

1. **Docs-only short-circuit.** If the categories from classify
   contain no code-touching category (only `documentation`, etc.),
   record `kind: none` and return.
2. **Setup hook.** `run_setup_hook(repo_root, log_dir, st)` execs
   `<repo>/.pila-setup.sh` if present (10-min timeout, streams to
   `.pila/runs/<id>/logs/setup-hook.log`). Idempotent via
   `st.data["provision"]["sh_hook_ran"]`. Nonzero exit → `die()`.
   **Runs as the non-root `pila` container user; no sudo.** The hook
   can install user-space tooling (`mise install <lang>@<version>`,
   anything writing to `~/.local/bin`) and pre-populate fixtures, but
   cannot `apt-get install` or write to system directories. Repos
   that need root-level system packages maintain a fork of the pila
   Dockerfile and override `IMAGE_TAG`; out of scope for the hook.
3. **Mise go-override synthesis.** `synth_mise_go_override(
   repo_root, run_dir) -> Path | None`: if `go.mod` exists but the
   repo has no `.go-version`, no `.tool-versions` go entry, and no
   `mise.toml`/`.mise.toml` go pin, parse `go.mod`'s `go 1.X[.Y]`
   directive and write `<run_dir>/mise-overrides.toml` containing
   `[tools]\ngo = "<version>"`. **Both `mise.toml` AND `.mise.toml`
   (dotted form, also a valid mise config name) are recognized**;
   non-dotted form wins if both exist (matches mise's discovery
   precedence). If the repo has an existing mise config, its
   `[tools]` content is preserved in the override file
   (`MISE_OVERRIDE_CONFIG_FILENAMES` replaces rather than merges; the
   override is the only file mise reads, so it must carry the repo's
   existing pins plus pila's addition). Idiomatic version files
   (`.nvmrc`, `.node-version`, `.python-version`, `.ruby-version`)
   and `.tool-versions` entries are ALSO copied into the override
   when the same tool isn't already pinned in the existing mise
   config — otherwise the override would silently drop them too
   (mise discussions #6598 / #7058). Returns the absolute path to
   the override file.

   **Precedence between idiomatic files** (pila's choice, not
   mise's documented behavior): when the synth fires and both
   `.nvmrc` and `.tool-versions` pin the same tool with different
   versions, `.nvmrc` wins. The iteration order in
   `_read_idiomatic_pins` runs the dedicated single-tool files
   (`.nvmrc`, `.python-version`, etc.) BEFORE `.tool-versions`,
   so the first-seen pin sticks. A repo with conflicting pins is
   a misconfiguration, but pila picks `.nvmrc` over
   `.tool-versions` for determinism. asdf-compatible names like
   `nodejs` and `python3` in `.tool-versions` are normalized to
   mise's `node` / `python` via `_ASDF_TOOL_ALIASES` so a
   `.nvmrc` + `.tool-versions: nodejs ...` repo doesn't end up
   with both `node` and `nodejs` pins in the override.
4. **Mise install.** `run_mise_install(repo_root, log_dir, st)`:
   exports `MISE_OVERRIDE_CONFIG_FILENAMES=<path>` if step 3
   produced one, then runs `mise install` at the repo root. mise
   reads `.tool-versions` natively, and reads `.nvmrc` /
   `.python-version` / `.ruby-version` / `rust-toolchain.toml` /
   `.go-version` because the image sets
   `MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS=node,python,ruby,rust`.
   Streams to `.pila/runs/<id>/logs/provision.log`. Nonzero exit
   surfaces the failing tool+version to `die()`.
5. **Version capture.** Runs `mise ls --current --json` (the
   subcommand `mise current --json` does not exist; verified
   against mise.usage.kdl). Output is object-keyed-by-tool, each
   value an array of `{version, install_path, source}` objects.
   Raw blob stored at `st.data["provision"]["mise_versions"]`;
   `tools[name][0].version` is the value rendered in `pila --list`
   and one-line log summaries.
6. **Table-first detection.** `detect_recipe_from_lockfiles(
   repo_root)`. Non-empty result is the recipe (marked
   `source: "table"` in state).
7. **LLM fallback.** Empty table result → `gather_provision_fixtures(
   repo_root)` assembles inputs (see below), `claude_p("provision",
   prompt, fixtures, SCHEMAS["provision"], model)` returns a
   recipe (marked `source: "llm"` in state).
8. **Validate.** `validate_provision_recipe(recipe)`. Reject →
   `die()`.
9. **Persist (do not execute).** Full recipe + `source` + resolved
    versions saved to `st.data["provision"]`. The recipe is not
    executed by `phase_provision` — the implementer and conformer
    workers run install commands from their own worktrees, given the
    recipe via prompt injection
    (`_format_provision_recipe_section()`). See "Worker-driven
    install" below.
10. **Export env.** If `synth_mise_go_override()` created an override
    file, `os.environ["MISE_OVERRIDE_CONFIG_FILENAMES"]` is set to
    its path so every downstream worker subprocess inherits it.

### Helper functions

| Function | Purpose |
|---|---|
| `gather_provision_fixtures(repo_root) -> dict` | Assembles the LLM-worker input set under a 24KB total ceiling. README extracted by `extract_readme_sections()`; root manifests (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `Gemfile`, `Makefile`, `pom.xml`, `build.gradle*`) included if present; workspace child manifests capped at 3 (1KB each) for monorepos; up to 2 `.github/workflows/*.yml` files matching `(?i)ci\|test\|build\|release` (skip `codeql\|stale\|dependabot`); optional `CONTRIBUTING.md` / `docs/DEVELOPMENT.md` capped at 4KB. |
| `extract_readme_sections(text) -> str` | Header-aware extractor. Strips leading emoji/punctuation before keyword match. Three header styles: ATX (`## ...`), setext (`...\n===` / `...\n---`), asciidoc (`== ...`). Keeps ≤1KB intro + matched sections (8KB post-extract budget). Section-match regex: `(?i)install\|getting[\s-]?started\|quick[\s-]?start\|setup\|usage\|\brun\b\|develop\|build(ing)?( from source\| instructions)?\|compil(e\|ing)( from source)?\|download\|from source\|requirements\|prerequisites\|dependenc(y\|ies)`. Fallback chain on no header match: code-fence detector (`pip install`, `npm install`, `cargo`, `brew`, `go install`, `apt-get`, `make` patterns, ±10 lines) → final top-6KB fallback. |
| `run_setup_hook(repo_root, log_dir, st)` | Execs `<repo>/.pila-setup.sh` if present with a 10-min timeout via `run_streaming` (live output to terminal + persistent log at `<log_dir>/setup-hook.log`); sets `st.data["provision"]["sh_hook_ran"] = True` on success. |
| `synth_mise_go_override(repo_root, run_dir) -> Path \| None` | See step 3 above. Returns the absolute path to the override file or `None` if no synthesis was needed. |
| `run_mise_install(repo_root, log_dir, st)` | Runs `mise install` + `mise ls --current --json` at `repo_root`. The install streams via `run_streaming` so the user sees per-tool progress on a first-run Python/Ruby/Rust install. |
| `_format_provision_recipe_section(recipe, *, audience) -> str \| None` | Renders the persisted recipe as a `PROVISION_RECIPE:` block for injection into implementer or conformer prompts. Audience-specific framing ("decide whether your subtask needs them" vs "ensure deps before BUILD/LINT/TEST"). Returns None when the recipe is empty or all-`none`. |
| `phase_provision(repo_root, st, models)` | Orchestrates all of the above. Detects + persists the recipe; does NOT execute it (workers run installs in their worktrees per DESIGN §6½). Exports `MISE_OVERRIDE_CONFIG_FILENAMES` to `os.environ` if a synth override was created, so all downstream worker subprocesses inherit it. |
| `run_streaming(cmd, ..., log_path, verbosity, ...)` | Async subprocess helper with live-streamed stdout+stderr, persistent log file, bounded tail deque, and `TimeoutExpired` carrying the tail in `.output`. Used by `run_mise_install` and `run_setup_hook`; replaces the previous `run_proc` calls that buffered output for the entire run duration. |

### Caches

Five host caches mounted into the container, all `rw`. Listed in §0.5
"Bind-mount table." Concurrency-safety verdicts:

- **mise installs** — Safe. Version dirs are immutable once installed;
  mise renames atomically on install.
- **pnpm store** — Safe (CAS, atomic ops; pnpm/discussions#10702).
- **Go modules** — Safe (`flock` per module-version in
  `cmd/go/internal/modfetch`).
- **Cargo** — Safe (flock on index + per-crate locks). Whole
  `CARGO_HOME` is mounted; mounting only `registry/` breaks
  `config.lock` (cargo#11376).
- **pip** — Mixed. Most races fixed (pypa/pip#9470, #12361, #13540
  closed). The wheel-build race #9034 (concurrent `pip install` of
  the same sdist into the same wheel-cache slot) is still open; in
  practice pila runs a small number of concurrent workers and the
  collision window is narrow. A worker that does hit the race retries
  once via pip's own retry, and a persistent failure surfaces as a
  conformer warning (DESIGN §9), not a silent corruption.

Bundler is **not** mounted as a shared cache (open `unlink` races,
rubygems/bundler#4519). Ruby repos route through `.pila-setup.sh`.

### Worker-driven install (replaces per-worktree replay)

`scripts/new-worktree.sh` does just the `git worktree add` and prints
the worktree path. There is **no orchestrator-driven install** after
that — the implementer runs the install itself from its own worktree
via its Bash tool, against the shared package-manager caches. The
conformer does the same before running BUILD/LINT/TEST.

How the recipe reaches the worker:

1. `git worktree add` checks out the worktree (tracked files only).
   It starts with no `node_modules/` / `.venv/` / `target/`, by
   design.
2. The orchestrator parses the worktree path from the script's stdout.
3. `run_implementer` (and later `run_conformer`) read
   `st.data["provision"]["recipe"]` and inject it as a
   `PROVISION_RECIPE:` block in the worker's user prompt via
   `_format_provision_recipe_section(...)`.
4. The worker's prompt (see `prompts/implementer.md` §2 and
   `prompts/conformer.md` §Input) instructs it to decide whether the
   subtask needs the install and to run the command from its
   worktree if yes. The shared store / cache makes re-runs across
   worktrees fast.
5. If the recipe is missing or empty (docs-only run), no
   `PROVISION_RECIPE:` block is injected and the worker proceeds
   without one.
6. Install failures inside a worker surface through the worker's
   normal exit machinery — a hard-failing build/test in the
   implementer becomes a `failed` or `blocked` status; in the
   conformer it surfaces as a `tests-failed: …` advisory warning
   (DESIGN §9).

Why this shape (vs. an orchestrator-driven install at `repo_root` or
per-worktree replay):

- The host's repo is bind-mounted at `repo_root`, so an
  orchestrator-driven install there writes linux-arm64 native
  binaries into the host's darwin `node_modules`, corrupting the
  host's checkout.
- Per-worktree pre-install is wasted work for subtasks that don't
  need built deps (config-only, doc-only, pure-code refactors that
  don't run tests). The barnacle reference run showed ~half of
  implementer subtasks correctly skip install when given the choice.
- `claude -p`'s built-in stream-event plumbing surfaces Bash tool
  I/O to the orchestrator log live, so an install running inside a
  worker is visible to the user without any special orchestrator
  streaming code.

The `MISE_OVERRIDE_CONFIG_FILENAMES` env var that `phase_provision`
synthesizes for polyglot Go repos (go.mod with no `.go-version`
sibling) is exported to `os.environ` once in `phase_provision` (and
re-exported from persisted state on `--resume`); worker subprocesses
inherit it without any per-worker plumbing because `_invoke` does
not pass an explicit `env=` to `create_subprocess_exec`.

---

## 7. Git worktree mechanics (`scripts/*.sh`)

Every script takes a `RUN_ID` as its first positional argument (after any flags) so the per-run namespacing is explicit at the shell boundary, not implicit through `cwd`.

| Script | Behavior |
|--------|----------|
| `setup-run.sh <run-id>` | Creates `pila/runs/<run-id>` **only if absent** — never force-resets it (an existing branch carries completed waves; resetting it would destroy resume state). Records the working branch (HEAD-at-run-start) to `.pila/runs/<run-id>/working-branch` on first run only. Adds the run-branch worktree at `.pila/runs/<run-id>/worktrees/staging` if missing. Appends `.pila/` to the repo's `.git/info/exclude` (idempotent). Safe on `--resume`. |
| `new-worktree.sh <id> <run-id>` | Creates `pila/subtasks/<run-id>/<id>` worktree at `.pila/runs/<run-id>/worktrees/<id>` branched off the current `pila/runs/<run-id>` tip; reuses an existing worktree/branch if present (resume after handoff). Prints the absolute worktree path. The run-branch (`pila/runs/…`) and subtask-branch (`pila/subtasks/…`) prefixes are deliberately disjoint so neither is an ancestor ref of the other — git's loose ref store cannot hold a ref AT a path and another ref UNDER that same path simultaneously. |
| `integrate.sh <id> <run-id>` | From repo root, inside the run-branch worktree (`.pila/runs/<run-id>/worktrees/staging`): `git merge --no-ff pila/subtasks/<run-id>/<id>`. Exit 0 clean; exit 1 on conflict, leaving the worktree mid-merge for an integrator; exit 2 on precondition failure (run-branch worktree or subtask branch missing) — `integrate_wave` treats exit 2 as fatal via `die()` and does *not* spawn an integrator, since the worktree-less case would fail in confusing ways. |
| `finalize.sh <run-id>` | Run-branch verifier. Exits 0 if `refs/heads/pila/runs/<run-id>` exists and contains at least one commit beyond the working branch; exits non-zero with a diagnosis otherwise. The working branch is **never** modified — pila does not merge into it locally; the PR is the proposed integration. The push and PR step lives in the **host launcher** (`pila` bash script), not in the container — it runs after `nerdctl run` exits cleanly, using the host's own `git push` + `gh pr create` against the host's auth state. See "Host-side finalize" below. |
| `cleanup.sh [--run-id <id> \| --all-runs \| --bootstrap] [--branches \| --subtask-branches]` | Default (no flag): scans `.pila/runs/*/state.json` for the most-recently-failed run (most recent without `finished_at`), confirms y/N, then removes only that run's worktrees + prunes git metadata. State dir stays as audit. `--run-id <id>` is an explicit single-run cleanup (worktrees only). `--all-runs` runs the same per-run cleanup across every run dir under `.pila/runs/` (excluding `_bootstrap-*`). `--bootstrap` removes orphaned `_bootstrap-*` directories (runs that died before classify completed; not enumerable by `discover_runs`). `--branches` (combinable with `--run-id` or `--all-runs`) additionally deletes the matching run branches *and* subtask branches (`pila/runs/<id>` and `pila/subtasks/<id>/*`). `--subtask-branches` deletes only the subtask branches and keeps `pila/runs/<id>` (the post-finalize default — the run branch is the PR head and must outlive the orchestrator). Without either flag, all branches are kept as an audit trail. State dirs are always preserved by `cleanup.sh`. Ctrl-C and every other abnormal exit in the orchestrator also preserve state — they call `_cleanup_on_abnormal_exit(full_purge=False)`. There is no `full_purge=True` call site today; the flag is retained as a future hook for an explicit-purge gesture, but no current code path uses it. |

A run branch `pila/runs/<run-id>` is never reset once created — this is the invariant `--resume` depends on. See `DESIGN.md` §6 ("the run branch is the resume contract").

### Host-side finalize (bash + jq in the `pila` launcher)

The push + PR step runs on the **host** in the launcher, after `nerdctl
run` exits cleanly. The container's `phase_finalize` writes
`finished_at` to `run.json` and exits 0; the launcher polls that
sentinel and proceeds. See DESIGN.md §6 *Finalization* for the
architecture (auth state lives in host processes the container can't
reach; the boundary is structural).

The launcher's finalize block in `pila` (bash) does, in order:

1. **Skip if `--no-push`.** Same opt-out as before.
2. **Read run state** via `jq` from `.pila/runs/<run-id>/run.json` and
   `state.json` (run branch, working branch, finished_at).
3. **Push the run branch.** `git push -u origin pila/runs/<run-id>`
   (with `--no-verify` if the flag was set). On failure: print the
   same multi-line message as the old Python path (names run branch +
   working branch, captured stderr, exact retry command), update
   `run.json` with `push_error`, exit non-zero.
4. **Compose PR body** via a bash heredoc that reads `state.json`
   fields with `jq` — same deterministic body shape as the previous
   Python `compose_pr_body` (task, category, source-of-truth, run
   timestamps, wave + subtask + worker counts).
5. **Open PR.** `gh pr create --base <working-branch> --head
   pila/runs/<run-id> --title pila: <run-id> --body-file -` with the
   composed body piped on stdin. On failure: log a warning with the
   pushed-branch URL and the retry command; update `run.json` with
   `pr_error`. **Non-fatal** — exit 0 (the run is complete; only the
   PR is missing).

**Preflight (`pila` bash, before `nerdctl run`):** the launcher
checks `git rev-parse --is-inside-work-tree`, `shutil.which gh`,
`gh auth status`, and `git remote get-url origin` BEFORE spinning up
the container. Each failure dies with the same actionable message
the orchestrator's `_check_gh_cli` used to print, plus the `--no-push`
escape hatch. The orchestrator no longer runs these checks; they
moved to the host where the auth state actually lives.

`--no-push` skips the entire push + PR step. CLI flag, `PILA_NO_PUSH`
env, `no_push = true` in `pila.toml`. `--no-verify` is CLI-only and
only affects the push step (worker `git commit`s inside worktrees
still run all hooks).

### Remote execution mode

`--remote` routes execution to a remote backend (Fly.io) instead of the
local `nerdctl run`. The launcher consumes the flag before building
`REWRITTEN_ARGS`, so the orchestrator's argparse never sees it.
Resolution order (highest priority first):

1. **`--remote`** CLI flag.
2. **`PILA_REMOTE`** environment variable (boolean: `1`/`true`/`TRUE`/`yes`/`YES`).
3. **`pila.toml`** at the repo root, `remote = true`.
4. **Default `false`** — local `nerdctl run` is used when unset.

When `REMOTE=true`, the launcher skips the per-OS nerdctl preflight, the
image-build check, the auth/cache mount assembly, and the `nerdctl run`
invocation, and instead calls the remote dispatch path. The remote
implementation is a stub — it emits a "not yet implemented" message and
exits non-zero — until the feat-remote-* subtasks flesh out the Fly.io
integration.

Maps to `DESIGN.md`: §6 (Finalization).

---

## 8. Coordination directory layout (`.pila/`)

Created in the main repository (not in any worktree — worktrees are disposable).
`setup-run.sh` git-excludes `.pila/` by appending it to the target
repo's `.git/info/exclude` rather than to the user's tracked `.gitignore`
(we deliberately do not modify files the user has committed).

Every run's artifacts live under `.pila/runs/<run-id>/`. The parent
`.pila/` directory is otherwise empty of run data; it only hosts the
`runs/` directory. Two concurrent runs in the same repository share no
coordination state.

```
.pila/
└── runs/
    └── <run-id>/                    (or _bootstrap-<6hex> pre-classify)
        ├── state.json               run state — see field table below
        ├── run.json                 sidecar — see field table below
        ├── working-branch           the branch HEAD-at-run-start; used as the PR base (pila does not merge into it locally)
        ├── plan.json                merged planner output
        ├── subtasks/<id>.json       per-subtask spec handed to each implementer
        ├── criteria/<id>.md         informational success-criteria notes (DESIGN §9)
        ├── checkpoints/<id>.md      handoff checkpoints (7-section schema)
        ├── logs/<sid>.log           per-worker raw stream-json event log (one file
        │                            per claude_p invocation by sid; always written
        │                            regardless of verbosity; append-only across
        │                            handoffs / clarifications)
        ├── worktrees/staging        the run-branch worktree
        ├── worktrees/<id>           per-subtask worktrees
        ├── pending-questions.json   written when clarification needs a non-interactive relay
        ├── pending-clarifications.json  written when an implementer hits a §11
        │                                mid-execution clarification (non-interactive)
        ├── answers.json             written by the plugin skill when relaying
        │                            clarification answers; passed back via --answers
        ├── calls.ndjson             per-run NDJSON telemetry — one JSON object per
        │                            line, one line per claude_p call; opened for
        │                            append at run start; written immediately after
        │                            each call returns (DESIGN §14)
        ├── memory.ndjson            orchestrator memory telemetry — one JSON object
        │                            per line, one line per ~30 s while orchestrate()
        │                            is alive; written by `_memory_sampler`. Keys per
        │                            line: `ts`, `rss_kb`, `phase` (mirrors
        │                            `state.current_phase`), `worker_count`, `open_fds`
        │                            (from `/proc/self/fd`; `-1` off Linux), `thread_count`
        │                            (from `threading.active_count`). Final sample is
        │                            flushed on sampler cancellation, so the file always
        │                            captures last-known state at orchestrator exit.
        │                            Used to distinguish a natural heavy run from a
        │                            real orchestrator memory leak post-mortem
        └── <heal_subdir>/           heal-loop on-disk state (default: "heal-out/")
            └── <call_type>/         one directory per call_type being healed
                ├── state.json       heal orchestrator state (history, best, baseline)
                └── iter-<N>/        one directory per heal iteration
                    ├── patch-request.json   inputs for the patch-generator worker
                    ├── patch-response.json  patch-generator worker's structured output
                    ├── applied-patch.txt    the patched system prompt text
                    ├── arm-results.json     n-replay results for each failing sample
                    └── scores.json          per-sample per-replay pass/fail verdicts
```

The bootstrap directory `_bootstrap-<6hex>` is the same shape; on Phase-1
completion, the orchestrator atomically renames it to the final
`<run-id>` directory once `run_id` is derived from the classifier output.
Open file handles (per-worker logs in particular) survive the rename
because POSIX file handles reference inodes, not paths.

`run.json` fields (a minimal sidecar enabling `pila --list` and resume
discovery without parsing the full `state.json`):

| Field | Shape | Notes |
|-------|-------|-------|
| `run_id` | str | the run identifier (matches the directory name and the branch suffix) |
| `branch` | str | the run branch — always `pila/runs/<run_id>` |
| `working_branch` | str | the branch HEAD-at-run-start; used as the PR base (pila does not merge into it locally) |
| `started_at` | ISO-8601 str | wall-clock start time (also mirrored in `state.json`) |
| `finished_at` | ISO-8601 str \| null | wall-clock end time, set at finalize success |
| `task` | str | the task description (mirrored from `state.json`) |
| `pushed_at` | ISO-8601 str \| null | when the run branch was pushed to `origin`; null until push runs |
| `push_error` | str \| null | captured `git push` stderr if the push failed; mutually exclusive with `pushed_at` being set |
| `pr_url` | str \| null | the PR URL `gh` returned; null until PR creation succeeds |
| `pr_error` | str \| null | captured `gh` stderr if PR creation failed; logical invariant — `pr_error` can be set only after `pushed_at` is set |

`_validate_run_json(data)` enforces three invariants on read:
- `pushed_at` and `push_error` are mutually exclusive (at most one is non-null).
- `pr_url` and `pr_error` are mutually exclusive.
- If `pr_url` is set, `pushed_at` must be set (cannot have a PR without a push).

A corrupt sidecar is flagged but does not block the rest of the system; `pila --list` will render that run with `status=corrupt-sidecar` and the user can inspect or delete the file.

`pila --list` derives a single status per run via `_derive_run_status(run_json, state_json)`. The taxonomy is checked in priority order — earlier rows fire first:

| Status | When it fires | Typical next step |
|--------|---------------|-------------------|
| `corrupt-sidecar` | `run.json` violates one of the three invariants above | inspect the file under `.pila/runs/<id>/run.json` |
| `push-failed` | `push_error` is set | re-run `git push -u origin pila/<id>` after fixing the access issue |
| `pr-failed` | `pr_error` is set (and push succeeded) | re-run `gh pr create` manually using the command logged at finalize |
| `done-pushed-pr` | `pr_url` is set | the happy path: PR open, work merged locally |
| `done-pushed-no-pr` | `pushed_at` set but `pr_url` not | rare: push succeeded, PR wasn't attempted (e.g., gh removed between push and PR) |
| `done-local` | `finished_at` set, no `pushed_at` | the user passed `--no-push`; push manually if desired |
| `in-progress` | none of the above | the run is still active (or died very early); resume with `--resume --run-id <id>` |

`RUN_STATUSES` in `pila.py` declares the seven values; a test coupling check asserts the tuple matches every value `_derive_run_status` can return.

`state.json` fields. This table is canonical: every field the orchestrator
writes to `st.data` must appear here, and every field listed here must be
written somewhere in `orchestrator/pila.py`. The coupling test in
`tests/test_state_fields.py` enforces parity in both directions against the
`STATE_FIELDS` tuple in `pila.py`.

| Field | Shape | Purpose |
|-------|-------|---------|
| `task` | str | the task description passed on the command line |
| `started_at` | ISO-8601 str | wall-clock time at run start |
| `finished_at` | ISO-8601 str | wall-clock time at successful finalize |
| `waves` | list[list[str]] | scheduled subtask ids per wave (from `schedule`) |
| `completed_waves` | int | index of the next wave to run (resume cursor) |
| `subtask_status` | dict[str, str] | per-subtask terminal status |
| `criteria_locks` | dict[str, str] | **deprecated** — sha256 hashes of locked criteria files. The criteria-lock discipline (DESIGN §9) was retired when the criteria file became informational; this field is no longer written by the orchestrator but is read-tolerated on resume of old runs. |
| `criteria_revisions` | list[dict] | **deprecated** — append-only audit log of the retired worker-initiated revision channel. No longer written; read-tolerated on resume of old runs. |
| `blocked` | dict[str, str] | per-subtask blocker reason when a wave aborts |
| `worker_count` | int | running total of `claude -p` invocations against `max_total_workers` |
| `current_phase` | str | the orchestrator's active phase string (e.g. `"phase 2: planning"`, `"phase 4-5: implementing"`); written at each phase entry and read by `_memory_sampler` so each `memory.ndjson` sample can be correlated with the phase that produced it. Empty string before phase 1 fires |
| `telemetry` | dict | calls, cost_usd, input_tokens, output_tokens — printed at run end |
| `categories` | list[str] | classifier output, post-whitelist filtering |
| `classifier_questions` | list[dict] | intent questions the classifier surfaced |
| `answers` | dict[str, str] | user answers to classifier questions (and source-of-truth) |
| `needs_source_of_truth` | bool | whether classifier asked for source-of-truth disambiguation |
| `source_of_truth_pref` | str | resolved preference (`codebase` / `research` / `both`) |
| `clarify` | bool | whether asking the user is allowed for this run (resolved from `--clarify` / `PILA_CLARIFY` / `pila.toml` / default `False`) |
| `verbosity` | str | resolved verbosity level (`quiet` / `normal` / `stream` / `debug`); re-resolved fresh on every run, including `--resume`, so the user can dial up or down without editing state |
| `inspect_dirs` | list[str] | extra absolute paths granted to inspect-bucket workers (classifier, planner, reconciler, provision) via `--add-dir`. Resolved from `--inspect-dir` / `PILA_INSPECT_DIRS` / `inspect_dirs` in `pila.toml`; re-resolved fresh on every run, including `--resume`, so the user can add or remove paths without editing state. Empty list when nothing is configured |
| `test_runner` | list[str] | detected short-circuit test command |
| `integrator_failure` | dict | unresolvable conflict from `integrate_wave` (non-fatal signal log) |
| `integrator_warnings` | dict[str, str] | non-fatal commit warnings from `integrate_wave` (non-fatal signal log) |
| `scope_warnings` | dict[str, dict] | oversized-diff warnings from `check_diff_scope` (non-fatal signal log) |
| `conformance` | dict[str, dict] | per-subtask conformer output and `conformance_warnings` (non-fatal signal log) — keys are subtask ids, values are `{result, warnings}` where `result` is the last conformer payload (or null on crash) and `warnings` is the list of advisory strings produced across all conformance rounds. Populated only on subtasks whose implementer reached `status: "complete"`. See DESIGN §9 *Post-work conformance* |
| `provision` | dict | output of `phase_provision` (DESIGN §6½). Keys: `source` (`table` / `llm` / `skipped-docs-only`), `recipe` (list of validated install entries, persisted for worker prompt injection — NOT executed by the orchestrator), `sh_hook_ran` (bool, set by `run_setup_hook`), `mise_versions` (raw blob from `mise ls --current --json`), `override_file` (absolute path to a synthesized mise override when `phase_provision` had to bridge a polyglot Go repo; `None` otherwise — re-exported as `MISE_OVERRIDE_CONFIG_FILENAMES` on `--resume`). Read by `_format_provision_recipe_section()` so implementer/conformer prompts can inject the recipe as a `PROVISION_RECIPE:` advisory block. |

`pending-questions.json` (written by `gather_answers` on non-TTY exit, read by
the plugin skill in `commands/pila.md`):

| Field | Shape | Notes |
|-------|-------|-------|
| `questions` | array of `{id, question, why_underivable?}` | the classifier-surfaced intent questions not already in `--answers` |

`answers.json` (written by the plugin skill, passed back via
`--answers .pila/answers.json`):

| Field | Shape | Notes |
|-------|-------|-------|
| `<question id>` | string | one entry per question id from `pending-questions.json.questions[].id` |
| `source_of_truth` | `"codebase"` / `"research"` / `"both"` | optional; overrides the resolved preference for this run |

The checkpoint schema — seven required sections, enforced by
`validate_checkpoint()`: *Frozen success criteria*, *Current status*, *Files
touched*, *Decisions made*, *Evidence gate status*, *Next action*, *Open
unknowns*. `validate_checkpoint()` enforces three layers: (a) every section
header must be present; (b) every section must carry non-whitespace content; (c)
the five "must carry handoff context" sections reject single-token
placeholder content (`none`/`n/a`/`na`/`tbd`/`nothing`/`unknown`/`todo`/`pending`/`—`/`--`/`-`/`?`) — the two
"nothing-to-report-is-OK" sections (*Decisions made*, *Open unknowns*)
accept these. Trailing punctuation (`.`/`!`/`?`/`…`) is stripped before
the comparison and repeated `?` is collapsed, so `None.`, `TBD!`, and
`???` are caught alongside the bare tokens. When a `worktree_root` is passed, `validate_checkpoint()`
also runs a freshness check: every path listed under *Files touched* must
either still exist in the worktree or carry a `[deleted]` annotation,
catching stale checkpoints whose paths were removed by partial work after
the snapshot was written.

In the same vein, `claude_p()` logs a context-decay warning when a worker
returns at ≥80% of its `--max-turns` budget (`num_turns` from the CLI
envelope). This is a proxy, not a hard guard: the schema only validates
the *shape* of the worker's final output, not whether the reasoning
chain that produced it ran against a healthy context. A 9.x confidence
score from a near-cap worker should be read with appropriate scepticism.
The warning sits alongside the existing `terminal_reason` warning at the
`claude_p` return path.

Maps to `DESIGN.md`: §10 (handoff, coordination-artifact location), §9 (criteria
locking).

---

## 9. Structured-output schemas

`claude_p()` validates each worker's payload against a schema keyed by worker
type. Required fields, current shape:

- **classifier** — required: `categories` (array). Optional: `questions`
  (array of `{id, question, why_underivable?}` — only `id` and `question`
  are required on each question), `source_of_truth_question` (bool). The
  classifier only flags whether the source-of-truth question is relevant;
  the orchestrator's preference resolution (see §2) supplies the value
  (default `both`).
- **planner** — required: `domain`, `subtasks`, `status`, `confidence`.
  `status` is the enum `ready` / `blocked` (DESIGN §8 planner gate): when
  the planner's evidence gate could not clear within `confidence_rounds`,
  it emits `blocked` with an empty subtasks list and the gap analysis in
  `confidence.gap_to_close`. `confidence` is the worker-internal self-gate
  object: required keys `task_understanding` (number 1–10),
  `decomposition_quality` (number 1–10), `basis` (string), `falsifiers_tested`
  (array of strings — what would-disprove probes were run and what they
  showed), `contradictions_reconciled` (array of strings — any contradictions
  with the worker's own prior statements, named with the kept version's
  evidence), `gap_to_close` (object with optional `task_understanding` and
  `decomposition_quality` strings — populated when either score is below
  9.0). Optional: `source_of_truth` (enum `codebase` / `research` / `both`).
  The `source_of_truth` enum is *defensive*: the orchestrator does not
  currently consume the planner's echoed value (it reads
  `answers["source_of_truth"]` instead); the enum future-proofs against a
  future consumer reading a garbled value. Each subtask is `{id, title,
  success_criteria_seed (all required), intent, scope_note,
  files_likely_touched, depends_on, requires, provides, size,
  investigation_notes}`. `size` is `small` or `medium` — `large` is
  rejected by `validate_plan`. The schema's required-ness of `confidence`
  and `status` is the structural part of DESIGN §8's discipline: a worker
  that skipped self-gating fails its own JSON schema before the orchestrator
  reads the payload.
- **implementer** — required: `subtask_id`, `status` (`complete` /
  `incomplete-handoff` / `blocked` / `failed` / `needs-clarification`).
  Optional: `branch`, `criteria_results` (array of
  `{criterion, met, evidence}`), `confidence` (worker-internal self-gate,
  not consumed by the orchestrator: required keys when present are
  `root_cause` and `solution` (numbers 1–10), `basis` (string),
  `falsifiers_tested` (array of strings), `contradictions_reconciled`
  (array of strings), and `gap_to_close` (object with optional
  `root_cause` and `solution` strings — populated when either score is
  below 9.0); see DESIGN §8 for the disciplines these fields make
  mechanically required), `checkpoint_path`, `blocker`, `summary`,
  `clarification_question` (DESIGN §11 mid-execution exception channel:
  `{id, question, why_underivable}` — all three required when the
  object is present; emitted only with `status: "needs-clarification"`,
  required to carry `checkpoint_path` as well so the work-in-progress
  survives the question to the user; orchestrator surfaces the question
  through the same interactive/non-interactive paths used by the
  Phase-1 classifier). The criteria file is informational per DESIGN
  §9; `criteria_results` is recorded for telemetry but does not gate
  the subtask. The retired `criteria_revision_proposal` field is no
  longer in the schema.
- **integrator** — required: `incoming_subtask`, `status` (`resolved` /
  `design-conflict` / `failed`). Optional: `resolution_summary`,
  `diagnosis` (read as a fallback for `resolution_summary` when
  diagnosing a non-`resolved` outcome).
- **conformer** — required: `subtask_id`, `rules_files_read` (array of
  strings — paths the conformer was handed by `discover_rules_files`; empty
  list when none were found), `rule_violations_fixed` (array of
  `{rule, fix, evidence}` — `rule` is the verbatim line from a rules file
  that was being honored, `fix` describes the change made, `evidence` cites
  the file/lines touched), `rule_violations_residual` (array of
  `{rule, why_not_fixed}` — violations the conformer spotted but did not
  resolve, with the reason), `docs_updates` (array of `{path, reason}` —
  documentation files updated to reflect the diff), `tests_updates` (array
  of `{path, reason}` — tests added or amended to cover the diff), `build`,
  `lint`, `tests` (each an object `{ran (bool), passed (bool), command
  (string), summary (string)}` — `ran: false` when the tool is not
  applicable to the repo; `passed` is irrelevant when `ran: false`),
  `summary` (string — one-line description of what the conformance pass
  did). Optional: `confidence` (worker-internal self-gate, not consumed by
  the orchestrator: `{conformance: number 1–10, basis: string,
  falsifiers_tested: array, contradictions_reconciled: array, gap_to_close:
  object}`). The schema enforces the structural part of DESIGN §9
  *Post-work conformance*: a conformer that skipped its own honesty
  discipline (e.g. wrote `passed: true` without a `command`) fails the
  schema before the orchestrator reads it. The cross-field invariants —
  residuals require a non-empty `rules_files_read`, every
  `rule_violations_fixed` item cites a non-empty `rule`, every
  `docs_updates` / `tests_updates` `path` exists in the worktree — are
  enforced by `validate_conformance_result()`.
- **judge** — required: `passed` (bool — aggregate verdict, true only when all
  three dimensions are true), `dimensions` (object with required boolean fields
  `schema_ok`, `factual_ok`, `hallucination_ok`), `rationale` (str — 1–3
  sentence explanation for the verdict), `suggested_fixes` (array of strings —
  empty when `passed: true`). One verdict object per `judge_capture()` call.
  Used by `phase_judge()` / `judge_capture()` — not by the orchestrator's main
  workflow workers. `prompts/judge.md` carries the rubric.
- **patch_generator** — required: `anchor` (str — the exact substring of the
  current system prompt that the patch should replace; the heal loop validates
  this against the actual prompt text before applying), `replacement` (str —
  the new text to substitute for `anchor`). Optional: `strategy` (str — a
  one-line description of what the patch changes and why), `pivot_reason`
  (str \| null — why this iteration pivots from the prior strategy, or null if
  this is the first iteration or no pivot). The `patch_generator` schema is used
  by the self-heal skill's patch-generation worker; like `judge`, it is
  post-run and not used by the orchestrator's main `claude_p()`.

Schemas are embedded as Python dicts in `pila.py` and serialized inline.

Maps to `DESIGN.md`: §7, §14.

---

## 10. Telemetry — NDJSON envelope and call_type mapping

Maps to `DESIGN.md`: §14.

### NDJSON envelope schema

Every `claude_p()` invocation appends one JSON object (one line) to
`.pila/runs/<run-id>/calls.ndjson` immediately after the call returns.
The file is opened for append at run start and is never truncated — it is
always a valid NDJSON file through the last complete line even under a hard
kill. It is never read by the orchestrator at runtime; reading is a
post-run operation performed by the judge and heal skills.

| Field | Type | Notes |
|-------|------|-------|
| `call_id` | str (UUID v4) | unique identifier for this invocation; referenced by judge verdicts |
| `run_id` | str | the run identifier — matches the directory name under `.pila/runs/` |
| `call_type` | str | one of `WORKER_TYPES`: `classifier`, `planner`, `reconciler`, `provision`, `implementer`, `integrator`, `conformer` |
| `model` | str | the model alias passed to `--model` for this invocation (e.g. `opus`, `sonnet`) |
| `system_prompt` | str | the full system prompt injected via `--append-system-prompt` |
| `user_content` | str | the user-turn content passed to the worker |
| `response_content` | str | the worker's raw text response (before schema parsing) |
| `parsed_ok` | bool | whether `structured_output` was present and schema-valid |
| `input_tokens` | int | `usage.input_tokens` from the CLI envelope |
| `output_tokens` | int | `usage.output_tokens` from the CLI envelope |
| `latency_ms` | int | wall-clock milliseconds from subprocess start to return |
| `success` | bool | whether the call produced a schema-valid result (false on WorkerError or schema retry exhaustion) |
| `ts` | str (ISO-8601) | UTC timestamp at the moment the line is written |

The judge skill consumes `system_prompt`, `user_content`, `response_content`,
and `parsed_ok` to evaluate quality. The heal loop uses `system_prompt` and
`user_content` to replay a call against a patched prompt. The `call_type`
field partitions calls for per-type analysis; judge and heal always operate
on one `call_type` at a time.

### Capture file path

```
.pila/runs/<run-id>/calls.ndjson
```

One file per run. Written by the orchestrator; the judge and heal skills
read it as a post-run harvest.

### call_type → prompt-resolution table

Each `call_type` maps to exactly one system-prompt source. The table below
is the complete, canonical mapping — no call_type is ever spawned without
a system prompt, and no system prompt is shared between call types.

| call_type      | Prompt source | Notes |
|----------------|---------------|-------|
| `classifier`   | `prompts/classifier.md` | read from disk by the orchestrator |
| `planner`      | `prompts/planner.md` | read from disk |
| `reconciler`   | `prompts/reconciler.md` | read from disk |
| `implementer`  | `prompts/implementer.md` | read from disk |
| `integrator`   | `prompts/integrator.md` | read from disk |
| `conformer`    | `prompts/conformer.md` | read from disk |

Every `call_type` resolves to a file under `prompts/`. The heal loop's
patch-generator worker calls
`resolve_prompt(call_type: str) -> tuple[str, str, str]` to load a
worker's system prompt: given any member of `WORKER_TYPES`, it returns
`(source_kind, content, location_hint)` where `source_kind` is `"file"`,
`content` is the prompt body, and `location_hint` is the relative path
`"prompts/<call_type>.md"`. Raises `ValueError` for an unknown
`call_type`. (Earlier iterations of pila also exposed a `validator`
call type whose prompt lived as a `VALIDATOR_SYSTEM` constant inside
`pila.py`; that worker was retired when the criteria file became
informational, and `resolve_prompt` no longer carries a
file-or-constant branch.)

### replay_capture — primitive for judge and heal-loop replays

```python
async def replay_capture(
    record: dict,
    *,
    override_system_prompt: str | None = None,
    cwd: str | None = None,
) -> tuple[dict, dict]:
```

Given one NDJSON record from `calls.ndjson`, reconstructs the `claude_p()`
invocation with the captured `system_prompt`, `user_content`, `call_type`
(used as `schema_key`), and `model`, and returns `(envelope, structured_output)`
from the new invocation.

`override_system_prompt` lets the heal loop replay with a patched prompt in
place of the originally captured one.

Replays use a throw-away in-memory `_ReplayState` and `_suppress_capture=True`
so they **never write to any `calls.ndjson`**. The capture stream is the ground
truth; replay results are ephemeral scoring artifacts.

Both judge (n=1 replay, then score) and heal (n=N replays, baseline vs patched)
build on this primitive.

---

## 11. Verification status of the code

Mirrors `DESIGN.md` §15, at the code level.

**Tested.** A pytest suite under `tests/` exercises the deterministic
enforcement functions:

| Test file | Function under test |
|-----------|----------------------|
| `test_resolve_source_of_truth.py` | `resolve_source_of_truth()` |
| `test_resolve_models.py` | `resolve_models()` — per-worker precedence (CLI > env > TOML), defaults, validation, empty/whitespace handling |
| `test__read_toml_key.py` | `_read_toml_key()` — the shared `pila.toml` line parser used by both resolvers |
| `test_gather_answers_validation.py` | the source-of-truth validation gate in `gather_answers()` |
| `test_retryable_failure.py` | `_retryable_failure()`, **including a coupling test** that the retryable markers actually appear in the strings emitted by `check_branch_has_commits` and the inline dirty-worktree check |
| `test_state_fields.py` | `STATE_FIELDS` tuple parity, in both directions: against the §8 field table, and against every `st.data[...] = …` / `setdefault(...)` write in `pila.py`. This is the mechanism §8's "this table is canonical" claim relies on |
| `test_validate_plan.py` | `validate_plan()` (every rule in §5) |
| `test_validate_result.py` | `validate_result()` (every status-branch invariant) |
| `test_check_merge_committed.py` | `check_merge_committed()` (real-git fixtures) |
| `test_inspect_tools.py` | `INSPECT_TOOLS` composition and the four inspect-callsite wirings (classifier, planner, reconciler, provision) — pins that the inspect bucket grants `Bash(<verb>:*)` patterns but never `Write`/`Edit` or bare `Bash`, the same DESIGN §12 enforcement applied to workers that don't get `--dangerously-skip-permissions` |
| `test_resolve_inspect_dirs.py` | `resolve_inspect_dirs()` precedence (CLI → env → TOML → `[]`), `~` expansion, dedup, and `STATE_FIELDS` membership |
| `test_resolve_prompt.py` | `resolve_prompt()` — every `WORKER_TYPES` member returns a `("file", content, "prompts/<call_type>.md")` triple; parity/coupling test; unknown call_type raises |
| `test_discover_rules_files.py`, `test_validate_conformance_result.py`, `test_run_conformance_phase.py`, `test_infer_build_lint_test.py` | the post-work conformance phase (DESIGN §9): rule-file discovery against the fixed capped allowlist, schema cross-field invariants including path-traversal rejection, the orchestrator-level loop covering clean / malformed / crashed / rolled-back / cap-exhausted paths, the commit-prefix observability check, the dirty-state warning before rollback, the worker-budget-exhausted advisory path, the outer `settle_subtask` contract (never escalates to `failed`/`blocked` even on `FileNotFoundError`), and `_infer_build_lint_test` across the supported package-manager families |
| `test_replay_capture.py` | `replay_capture()` — args reconstructed from capture record, `override_system_prompt` plumbed through, no `calls.ndjson` written during replay, return-value shape `(envelope, structured_output)` |
| `test_phase_judge.py` | `phase_judge()` / `judge_capture()` — 3 verdicts written for 3-record NDJSON, INDEX.json content, schema validation, max_parallel semaphore bound, call_type filtering, empty/missing NDJSON edge cases |
| `test_heal_loop.py` | `HealState` save/load round-trip + atomic write; `heal_baseline()` — state.json + 6 verdict files for 2 samples n=3; `heal_apply_patch()` — patched prompts written per sample under iter-1/; `heal_replay_patched()` — history + best_so_far updated in state.json |

Run with `pytest tests/` from the repo root. The suite completes in
under two seconds end to end.

**CI surface.** GitHub Actions runs three independent workflows on every
pull request to `main` (and on pushes to `main`):

| Workflow | What it does |
|----------|--------------|
| `.github/workflows/test.yml` | `pytest tests/ -ra` across Python 3.10 / 3.11 / 3.12, with `pytest-cov` reporting line coverage to the job summary (no gate per CLAUDE.md). Coverage XML is uploaded as a 7-day artifact from the 3.12 job. Dev dependencies (`pytest`, `pytest-cov`) installed inline per CLAUDE.md's "pytest is the only dev dependency" stance. |
| `.github/workflows/syntax.yml` | The AST parse from CLAUDE.md's task-completion checklist, plus the same parse over every file under `tests/`. Path-filtered to `orchestrator/**/*.py` and `tests/**/*.py` for fast feedback ahead of the full pytest matrix. |
| `.github/workflows/shellcheck.yml` | `shellcheck -x scripts/*.sh` — the worktree mechanics scripts are load-bearing (DESIGN §6). Path-filtered to `scripts/**/*.sh`. |

Each workflow has a `concurrency:` block keyed on `github.ref` with
`cancel-in-progress: true`, so a force-push or rapid pushes do not
leave superseded jobs in flight. Dependabot (`.github/dependabot.yml`)
tracks the GitHub-Actions ecosystem on a weekly cadence.

**Not tested.** No worker has run against a live `claude -p`. The flag
contract in §3 is from CLI documentation, not from observed runs. The
worker invocation function (`claude_p`) is not unit-tested because
meaningful testing requires a stub or live `claude` binary — that's a
separate end-to-end tier.

First real step: one run on a throwaway repo with a small, fully-specified
task.

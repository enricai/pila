# Changelog

All notable changes to Pila will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Installer provisions Colima with 4 GB of swap on fresh installs.**
  The auto-sizing fix that landed earlier in this release raised the
  Colima VM's RAM but didn't add swap — with `Swap: 0B` the kernel's
  OOM killer fires immediately when RAM is exhausted, with no
  breathing room for transient spikes. Under pila's parallel-
  implementer workload (concurrent `claude -p` plus Vitest workers
  spiking to 2 GB RSS each plus `tsc`/`pnpm` toolchain overhead) we
  observed the OOM killer hitting the host-side `nerdctl` /
  `lima-guestagent` daemons inside the VM, which collapses the Mac
  launcher's connection and surfaces as `FATA[NNNN] exit status 255`
  with no orchestrator diagnostic. The installer now writes an
  idempotent `provision:` block to `~/.colima/default/colima.yaml`
  on fresh installs (no existing config) — the block uses sentinel
  markers (`# pila:swap-provision-v1 BEGIN/END`) so re-runs are
  no-ops. The provision script `fallocate`s `/var/swapfile`,
  `mkswap`s it, `swapon`s it, and sets `vm.swappiness=10` so the
  kernel uses swap only under real pressure (default 60 is too
  eager for our safety-net use). For users with an existing
  `colima.yaml`, the installer deliberately does NOT mutate it —
  too risky to clobber custom mounts / CPU type / disk size —
  instead it logs a one-line hint with the exact YAML block to
  paste in plus `colima stop && colima start`. The 4 GB swapfile
  persists on Colima's VM disk across `colima stop/start`; only
  `colima delete` removes it (and the next start re-creates it
  via the provision script). See
  `_runtime_colima_swap_yaml` in `scripts/runtime-install.sh` for
  the authoritative YAML and `docs/INSTALL.md` "Memory pressure:
  swap configuration" for the user-facing docs.

- **Installer auto-sizes the Colima VM to half the host's CPU/RAM
  instead of using Colima's 2-CPU / 2-GB defaults.** The 2/2 defaults
  were not enough for parallel pila runs — concurrent `claude -p`
  workers (~300 MB each) plus toolchain processes (`tsc`, `vitest`,
  etc.) blew through 2 GB in minutes, triggering a kernel OOM in the
  Colima VM. The OOM killer hit the host-side `nerdctl` daemons (not
  the container's PID 1), so the failure manifested on the Mac
  launcher as `exit 255` with no orchestrator diagnostic — the
  container's stdout just stopped mid-stream. The installer now
  detects host resources via `sysctl hw.ncpu` / `hw.memsize` and
  starts Colima with `--cpu N --memory M` sized at half-of-host,
  clamped to CPU 2..8 and RAM 4..16 GB. The Linux path is untouched
  (Linux runs containerd natively, no VM to size). Already-running
  VMs are left alone, but a one-line hint is logged if the current
  sizing is below the auto-recommendation. See
  `_runtime_colima_size_flags` in `scripts/runtime-install.sh` for
  the bounds rationale.

- **Per-container Claude config isolation eliminates the silent-hang
  race.** Concurrent pila containers used to share a single host
  `~/.claude.json` via bind mount, which exposed the well-documented
  `claude-code` corruption race (anthropics/claude-code issues #28847,
  #29217, #29395, #40226 — all open). When the file went missing
  mid-rewrite, the CLI entered a "recovery loop with no backoff" —
  `claude -p` never exited, pila's existing retry path never fired,
  and the worker hung silently until the 90-min hard kill. The
  launcher now stages a per-run scratch dir on the host with a
  private copy of `~/.claude.json` (with `projects[]` stripped),
  `~/.claude/` (with bulky / prior-session paths blacklisted), all
  present `~/.git*` siblings, `~/.config/git/`, `~/.netrc`, `~/.ssh/`
  (sockets filtered), and `~/.gnupg/` (sockets filtered). Each piece
  mounts at its default in-container path so the CLI and git see
  normal locations with private contents — no shared host state to
  race on, no env-var redirection needed. On macOS the OAuth token
  is now extracted from Keychain (`security find-generic-password`)
  and written to the staged `~/.claude/.credentials.json` — the same
  file-based path the Linux CLI reads — so authentication works
  identically on both platforms without an env-var bridge. The host
  scratch dir is reaped on container exit; container-side writes
  (`numStartups++`, new session transcripts) are intentionally lost.
- **Live stderr streaming surfaces worker failures in seconds, not
  minutes.** `_invoke()`'s `_drain_stderr` used to silently buffer
  every stderr byte into an in-memory list and surface it only on
  exit (or when the idle watchdog flushed the last ~40 KB at 300 s).
  When `claude -p` hit the recovery loop above, its repeated "Claude
  configuration file not found" stderr lines were invisible to the
  user for the full 5 minutes before the watchdog fired. Stderr now
  streams line-by-line to the per-sid log file (with a `[ts] stderr`
  header) and echoes to the orchestrator log at `stream` / `debug`
  verbosity. Stderr activity also refreshes the watchdog clock, so a
  worker that emits only stderr (recovery-loop scenarios) doesn't
  falsely trip the idle watchdog. `stderr_chunks` is still populated
  for the existing exit-time `WorkerError` message at `pila.py:4195`.

- **Provisioning no longer mutates the host's repo.** Phase 1½ used
  to execute `pnpm install` / `pip install` / etc. against
  `repo_root`, which is bind-mounted from the host. On
  darwin-host + linux-container setups (the common Colima case)
  this clobbered the host's `node_modules` with linux-arm64 native
  binaries — host `pnpm dev` would then crash with
  "wrong architecture" until the developer ran `pnpm install` on
  the host again to restore darwin-arm64 binaries. Phase 1½ now
  only *detects* the install recipe; each worker runs the install
  in its own worktree against the shared package-manager cache
  (DESIGN §6½ "Worker-driven install"). Side effects: the
  `replay_provision_in_worktree` function and its
  `wrap_with_mise_exec` helper are removed; the recipe is now
  injected into implementer and conformer prompts as a
  `PROVISION_RECIPE:` advisory block.
- **pnpm store cache mount was inert.** The launcher exported
  `PNPM_STORE_PATH=/home/pila/.cache/pila/pnpm-store`, but that
  env var does not exist in pnpm — pnpm reads `npm_config_store_dir`
  (env), `store-dir` (`.npmrc`), or `--store-dir` (CLI). pnpm
  silently fell back to its default
  (`/home/pila/.local/share/pnpm/store`), which was NOT
  bind-mounted, so every container run paid full registry cost on
  every package. Fixed by setting `npm_config_store_dir` instead.
  The host cache (`~/.cache/pila/pnpm-store`) now warms across
  runs as intended.
- **`mise install` and `.pila-setup.sh` no longer hang silently.**
  Both ran through `run_proc` which buffered stdout/stderr until
  the process exited — on a first-run Python 3.12 / Ruby 3.2 / Rust
  install that meant the user could stare at one log line for
  10+ minutes before seeing anything. A new `run_streaming()`
  helper (next to `run_proc`) streams output line-by-line to both
  the terminal and the persistent log, keeps a bounded tail for
  error reporting, and on timeout populates `TimeoutExpired.output`
  with the captured tail so callers can include it in their
  diagnostic.

## [0.2.1] - 2026-05-29

### Fixed

- **`/home/pila` is now writable by the runtime user inside the
  container.** Observed images had `/home/pila` owned by `root:root`
  (despite `useradd -m -u $HOST_UID -g $HOST_GID pila` having run),
  which meant the runtime `pila` user couldn't create any new dotfile
  under its own `$HOME`. The visible failure was `gpg: Fatal: can't
  create directory '/home/pila/.gnupg': Permission denied` during
  `phase 1½` when mise tried to verify a Node download. The
  Dockerfile now explicitly `chown pila:${HOST_GID} /home/pila`,
  pre-creates `/home/pila/.gnupg` at mode 0700 (which GPG requires),
  and chowns it to the pila user. Other dotfile-writing tools (npm,
  ssh known_hosts, cargo, etc.) also benefit. Version bumped to
  0.2.1 to force a rebuild of the cached image (the launcher's
  image-presence check would otherwise skip the rebuild).

### Changed

- **Finalize moved to the host launcher.** `git push` and `gh pr create`
  now run on the host after the container exits cleanly, not inside the
  container. Removes the entire bind-mount of `~/.config/gh`,
  `~/.git-credentials`, `~/.ssh`, and `$SSH_AUTH_SOCK` — those auth
  states live in host processes (Keychain, ssh-agent under launchd,
  gh's local token store) that don't cross the Lima/Colima VM boundary
  cleanly on macOS. The container's job is the LLM work + deterministic
  integration into `pila/runs/<run-id>`; the host's job is everything
  network-y, using its own working auth. Side effects: the macOS-only
  "SSH agent forwarding is not available" note is gone (irrelevant
  now); `gh auth status` runs as a host preflight before the container
  starts (fast-fails in milliseconds, not after a 60-second cold
  container launch); SSH push works on macOS via the host's
  `ssh-agent`; the `_check_gh_cli` and `push_and_open_pr` Python
  functions and the in-container cwd-is-git-repo check are removed.
  `compose_pr_body` is kept as the canonical reference for the PR body
  shape; the launcher reimplements its body composition in bash + jq.
  New host dependency: `jq` (brew/apt/dnf/pacman). DESIGN §6
  *Finalization* and IMPLEMENTATION §0.5 + §7 updated.

- **Auto-install the container runtime on first run.** If Colima
  (macOS) or nerdctl (Linux) is missing when the launcher runs, the
  launcher now installs it instead of erroring out with a hint.
  Behavior mirrors `scripts/install.sh` exactly: `brew install colima`
  + `colima start --runtime containerd --mount-type virtiofs` on
  macOS; distro-appropriate `apt-get`/`dnf`/`pacman` + pinned upstream
  nerdctl binary on Linux. A new shared helper at
  `scripts/runtime-install.sh` defines the install functions; both
  `install.sh` and the `pila` launcher source it (DRY). Opt-out via
  `--no-runtime-install` (CLI) or `PILA_NO_RUNTIME_INSTALL=1` (env) —
  same flag/env as the installer. TTY-guarded: when stdin is not a
  terminal (Claude Code plugin mode), the launcher prints a clear
  "run from a terminal once" message instead of hanging on a sudo
  prompt. `print_install_hint` gains a brew-detection branch on macOS
  so users without Homebrew get the right two-step path
  (install brew → re-run pila).

### Added

- **Per-repo dependency provisioning — Phase 1½** (DESIGN §6½). The
  orchestrator now installs each target repo's dependencies (and
  selects the right runtime versions) inside the container before any
  worker runs. Five layered steps: (1) optional `.pila-setup.sh` hook
  for user-space tooling install (additional `mise install
  <lang>@<version>` for languages beyond the LTS bake, CLI tools into
  `~/.local/bin`, pre-populated fixtures) — runs as the non-root
  `pila` user, so root-level system packages need a forked Dockerfile;
  (2) **`mise`** resolves runtime versions
  from `.nvmrc` / `.python-version` / `.tool-versions` /
  `rust-toolchain.toml` (image-set
  `MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS` flips the opt-in), with
  `.go-version` synthesized from `go.mod` via
  `MISE_OVERRIDE_CONFIG_FILENAMES` — and because that env var REPLACES
  rather than merges discovery, pila copies any idiomatic-file pins
  forward into the synthesized override so polyglot repos (e.g. Go +
  `.nvmrc`) don't silently drop their non-Go pins; (3) a deterministic
  **lockfile-detection table** emits install commands (pnpm > yarn >
  npm precedence; uv > poetry > pipenv; Go modules, Cargo, Bundler);
  polyglot repos like Rails-with-frontend emit **all** matching
  commands, not just the first match; (4) a `provision` LLM worker
  fires only when the table abstains (Java/Gradle, bare
  `pyproject.toml`, polyglot Makefile) and is structurally bounded by
  a schema + argv allowlist (the one documented §12 carve-out, see
  DESIGN §6½); (5) **per-worktree replay** via `mise exec --` so each
  fresh worktree's implementer sees the same toolchain.
- **Image-baked LTS fallbacks via `mise install --system`.** Node LTS
  and Python 3.12 land at `/usr/local/share/mise/installs/` so repos
  that declare no version still get a predictable runtime. The
  resolver checks the per-run user dir first then falls through to
  the system layer (verified against
  https://mise.jdx.dev/mise-cookbook/docker.html). A repo with zero
  version pins (no `mise.toml`, no idiomatic file, no synthesized
  override) skips `mise install` entirely and runs directly on the
  image-baked LTS — avoids depending on mise's implementation-defined
  behavior when no tools are declared.
- **Five host-side caches** mounted into the container — `mise-data`
  (so a Node 20.11.0 install survives across runs), `pnpm-store`,
  `pip` cache, `GOMODCACHE`, and the whole `CARGO_HOME`. Concurrency
  safety verdicts and the pip warm-once-then-replay pattern that
  sidesteps pypa/pip#9034 are documented in IMPLEMENTATION §6½.
- **`.pila-setup.sh`** at the repo root is the user-space escape
  hatch the language layer can't install — `mise install
  <lang>@<version>` for additional runtimes, CLI tools under
  `~/.local/bin`, fixture pre-population. Runs as the non-root `pila`
  user once per fresh run before mise; idempotent via state. Root-
  level system packages (anything needing `apt-get install` or
  writes to `/usr/*`) are out of scope: the container intentionally
  ships no sudo. Workaround: maintain a fork of the pila Dockerfile
  and override `IMAGE_TAG`.
- **New `provision` worker type** (defaults to Opus). Independently
  overridable via `--model-provision` / `PILA_MODEL_PROVISION` /
  `model_provision` in `pila.toml` like every other worker.

### Fixed

- **Claude Code auth now works inside the container on macOS.** Claude
  Code stores its OAuth token in macOS Keychain (an IPC service the
  container can't reach), not in the bind-mounted `~/.claude/` files —
  so `claude -p` inside the container failed preflight with "Not logged
  in" even when the host was logged in. The launcher now forwards
  `CLAUDE_CODE_OAUTH_TOKEN` to the container when it's set in the
  invoking shell, with explicit `=value` form. On macOS, if the var is
  unset, the launcher prints a one-line note with the
  `security find-generic-password` extraction command. On Linux native
  the file-based `~/.claude/credentials.json` continues to ride the
  existing bind mount; no behavior change. Note: the previous attempt
  used the bare `-e VAR` pass-through form (no `=value`), which works
  under Docker but does NOT work under Colima/nerdctl — the container
  receives an empty string. The fix expands the value at launcher exec
  time, accepting a brief `ps -ef` argv-visibility window (single-user
  macOS dev box is the supported trust domain; multi-user host is out
  of scope, same as the existing `~/.claude/` bind mount).

- **Worker timeout no longer dumps a 50-KB traceback.** When a worker
  hit `worker_timeout_sec` (default 5400s / 90 min), `_invoke` raised
  `subprocess.TimeoutExpired` which escaped `run_implementer`'s
  `except WorkerError` catch and bubbled all the way to `main()`'s
  catch-all, dumping the entire `claude -p` command line as a Python
  traceback to the user's terminal. `run_implementer` now catches
  `subprocess.TimeoutExpired` and returns an `incomplete-handoff`
  envelope (same shape as the existing WorkerError path), so the
  timeout becomes a routine handoff that `--resume` picks up cleanly.
  `run_conformer` gets the same shield — a timed-out conformer
  becomes a logged warning + returns None, matching the existing
  WorkerError advisory-phase semantics. Observed three times in real
  runs on 2026-05-28 (stackpulse × 2, navegando × 1) before the fix.

- **Worktree-removal timeout raised 30s → 240s.** A real worker that
  ran `npm install` left a 868 MB / 41k-file worktree; `git worktree
  remove --force` did `rm -rf` on it, which took longer than the 30s
  cap. The new 240s value is calibrated against that worktree
  (~45-90s uncontested) with margin for N-way concurrent disk
  contention (a six-worktree wave was observed timing out
  concurrently). Still bounded so a genuinely hung git command
  doesn't block cleanup indefinitely. Per-worktree failures are
  still non-fatal, and the cleanup now emits a closing recovery
  hint when any removal timed out: `cleanup: N worktree(s) not
  removed within 240s — run scripts/cleanup.sh --run-id <id> to
  finish manually`.

- **`--resume` disambiguation now shows status + last-activity.**
  When multiple in-flight runs exist in the same repo, the previous
  error message listed only `run_id  (started <iso-timestamp>)` — no
  hint which run was alive. Each row now includes `status=<derived>`
  (from the same `_derive_run_status` `pila --list` uses) and
  `last-activity=<age>` (humanized state.json mtime: e.g. `12s ago`,
  `2h05m ago`, `1d4h ago`). Zero new shell-outs — both signals come
  from data already in scope. The disambiguation stays a hint, not
  an auto-pick; user still passes `--run-id`.

### Changed

- **Pila now runs inside a container per run.** Cleanup of `claude -p`
  workers + every test runner / build / dev server they spawned is now
  a Linux PID-namespace teardown rather than a heuristic PPID-walk in
  Python. Ctrl-C reliably reaps everything; SIGKILL or hard crashes do
  too (cgroup release is a kernel guarantee, not a Python signal
  handler). New host requirement: a container runtime — Colima on
  macOS, containerd + nerdctl natively on Linux. Setup per OS:
  `docs/INSTALL.md`. The orchestrator code (`orchestrator/pila.py`)
  is unchanged; the container/process-isolation work lives in the new
  `pila` launcher, `Dockerfile`, and `scripts/container-entry.sh`.
  See DESIGN.md §6 *Worker subtree termination* and IMPLEMENTATION.md
  §0.5 *Container shape* for the architecture and code surface.

- **`scripts/install.sh` now auto-installs the container runtime.** On
  macOS the installer runs `brew install colima` and `colima start
  --runtime containerd --mount-type virtiofs`. On Linux it dispatches
  to the matching package manager (Debian/Ubuntu via `apt-get`,
  Fedora/RHEL via `dnf`, Arch via `pacman`) for `containerd`, then
  downloads the pinned `nerdctl` v2.3.1 binary from upstream (arch-aware
  amd64/arm64), then `sudo systemctl enable --now containerd`. Pass
  `--no-runtime-install` (or `PILA_NO_RUNTIME_INSTALL=1`) to keep the
  pre-rollout behavior: detect the runtime, print the manual install
  hint, and exit 1. Unknown distros fall back to the hint regardless.
  Existing Docker Desktop / podman installs coexist with Colima/nerdctl
  — no conflict detection is attempted.

- **Default-mode runs now require the `gh` CLI on the host.** The
  container image installs `gh`, but auth state at `~/.config/gh/` is
  bind-mounted from the host. Run `gh auth login` on the host once
  before running pila, or pass `--no-push` to skip the finalize PR
  step. `git push` for HTTPS remotes uses bind-mounted
  `~/.git-credentials`; for SSH remotes it uses bind-mounted `~/.ssh`.
  *macOS caveat*: SSH agent forwarding is not available on Colima —
  AF_UNIX sockets don't traverse the Lima VM boundary, and
  `$SSH_AUTH_SOCK` typically sits under `/private/tmp/` (outside
  Colima's auto-share scope). Passphrase-protected SSH keys won't work
  inside the container on macOS; switch the remote to HTTPS (via
  `gh auth setup-git`) or pass `--no-push`. Linux native users get the
  agent socket mounted normally. The launcher detects `Darwin` and
  skips the mount with a one-line note pointing at this workaround.

- **Plugin mode (`/pila` from inside Claude Code) and terminal mode
  share one container model.** The launcher detects `[ -t 0 ]` and
  passes `-it` (terminal) or `-i` only (plugin/no-TTY). Plugin mode
  reuses the existing `EXIT_NEEDS_ANSWERS=10` clarification dance
  (write `.pila/pending-questions.json`, exit 10; the plugin agent
  reads the file through the `/work` bind mount, asks the user in
  chat, re-runs with `--answers`). No new mechanism — the container
  is transparent.

- **Ctrl-C is now resumable.** Earlier versions treated SIGINT as an
  explicit "throw this away" gesture and ran a full purge — worktrees,
  branches, and the run dir all deleted, `--resume` impossible.
  Ctrl-C now follows the same conservative contract as every other
  abnormal exit: worktrees are torn down (re-created idempotently on
  resume), state.json + branches + checkpoints all survive. The
  explicit full-purge gesture is `scripts/cleanup.sh --run-id <id>
  --branches`. README, DESIGN.md §6, IMPLEMENTATION.md §5, and the
  signal-cleanup pin test are updated to match.
- **`max_total_workers` default 40 → 60.** Empirically (May 2026)
  18-subtask runs hit the cap mid-conformance, aborting with
  `worker budget exhausted`. Structural budget for an 18-subtask plan
  is ≈ 1 classifier + 2 planners + 1 reconciler + 18 implementers +
  ~18 conformers + a few continuations / integrators ≈ 45–55 workers
  worst-case; the new default leaves margin without inviting runaway
  cost. `PILA_MAX_WORKERS` env var and `max_workers` in
  `pila.toml` are new escape hatches (same precedence as
  `--confidence-rounds`: CLI > env > TOML > default).
- **Protected-path scope narrowed.** The diff-scope check that gates
  implementers and conformers previously rejected any write under
  `.claude/` wholesale. It now protects only `.pila/`, `.git/`,
  and top-level `.claude/` files (`settings.json`,
  `settings.local.json`); the three documented Claude Code
  user-deliverable subtrees (`.claude/agents/`, `.claude/commands/`,
  `.claude/skills/`) are exempt. Pila's own self-healing skill
  instructs downstream consumers to write subagent files at
  `.claude/agents/<name>.md`; the over-broad protection previously
  blocked the very pattern the skill teaches. DESIGN.md §9,
  IMPLEMENTATION.md, and `prompts/conformer.md` are updated to match.
- **`--no-clarify` is now `--clarify`; no-questions is the new
  default.** The flag's polarity is inverted: by default pila runs
  without surfacing intent questions to the user. The classifier's
  codebase→research filter still runs and the implementer applies the
  same filter before any mid-execution decision — "no questions" never
  means "skip the rigor." Pass `--clarify` (or set
  `PILA_CLARIFY=true` / `clarify = true` in `pila.toml`) to
  opt into surfacing the questions that survive the filter.
- **Clarification filter is DRY-ed across the prompts.** The wording
  shown to workers now lives in a single shared fragment
  (`prompts/_clarification_filter.md`), included into
  `prompts/classifier.md` and `prompts/implementer.md` at load time
  by a new `load_prompt()` helper in `orchestrator/pila.py`.
  Previously the same filter was restated three times and could
  drift. Worker-facing text now also pushes back explicitly on the
  base model's training prior to ask questions liberally — ~90% of
  apparent intent questions are closable by deeper investigation.

### Added

- **Rate-limit-aware hard exit with optional auto-resume.** Pila now
  detects the Claude Code subscription session-limit message
  (`"You've hit your session limit · resets <time> (<tz>)"`) in worker
  output, and the protocol-level `rate_limit_event` whose `status`
  field falls outside the known-allowed set
  `{"allowed", "allowed_warning"}` (defensive match against future
  terminal status strings — Anthropic's terminal value is
  internal/unobserved). Either signal raises a new `RateLimitedExit`;
  main() runs the worktree-only cleanup (state + branches preserved)
  and, when the reset clause parses unambiguously (text path:
  wall-clock + IANA tz; protocol path: Unix `resetsAt` timestamp),
  sleeps until the reset moment + 30s margin then `os.execvp`'s the
  launcher with `--resume --run-id <id>` for a fresh orchestrator
  process. The `--max-workers` budget is NOT reset across the re-exec
  (it persists via state.json's `worker_count`) so a run that
  repeatedly hits the rate-limit still respects the user's cap. When
  the parse fails (malformed
  time, unknown timezone, future format change), pila exits with code
  75 and prints the manual resume command — never a wrong-time sleep.
  CLI-only overrides on the original launch (`--model`,
  `--max-workers`, etc.) are *not* propagated across the re-exec; set
  them via env (`PILA_*`) or `pila.toml` if you want them to survive.
  Empirical anchor: the verbatim message text matched identically
  across three independent runs in May 2026, and the broad
  `"rate-limit"` pattern false-matches legitimate worker text
  discussing rate-limit code, so the detector keys only on the
  literal marketing-copy prefix.
- **Belt-and-suspenders retry for the
  `incomplete-handoff`-with-missing-checkpoint case.** When the
  rate-limit detector misses (e.g. Anthropic changes the message
  format), the worker's empty-checkpoint envelope previously hit
  `_retryable_failure` and was classified terminal. The retry
  classifier now treats the validate_result line-2314 wording
  (`checkpoint_path '...' does not exist on disk`) as retryable via a
  prefix-match — tight enough that the sibling needs-clarification
  case (line 2350) which shares both substrings stays terminal.
- **Cross-planner file-overlap warning at plan-validation time.** When
  two planners both list the same path in `files_likely_touched`,
  pila now logs a warning right after reconciliation (before the
  scheduler builds the DAG) instead of waiting for the integrator to
  crash mid-wave. Empirically (n=3 historical runs) the signal is
  clean: the one successful run had zero overlaps; both failed runs
  had ≥9. The warning is non-fatal — same-file overlap is sometimes
  legitimate (one planner adds scaffolding the other consumes) — but
  it surfaces the structural risk early. The full autonomous
  resolution (extending the reconciler's action vocabulary to handle
  file-claim conflicts the same way it handles capability-tag
  vocabulary drift) is tracked as follow-up work.
- `PILA_MAX_WORKERS` env var and `max_workers` key in
  `pila.toml` resolve through the new `resolve_max_workers()`
  helper, mirroring `resolve_confidence_rounds()`'s precedence.
  `--max-workers` argparse type is now `_positive_int` (was `int`):
  bad values (0, -1, "nope") are rejected at parse time with a clean
  argparse error instead of falling through to a downstream default.
- `is_protected_path(path)` module-level helper in
  `orchestrator/pila.py` is the new single source of truth for
  what the diff-scope check rejects. `check_diff_scope()` and
  documentation reference it; the previous inline tuple is gone.
- `PILA_CLARIFY` env var and `clarify` key in `pila.toml`
  (same precedence as `--source-of-truth`: CLI > env > file > default
  `False`). New helper `_resolve_bool_pref` factors the resolution
  shape shared with `--no-push` to keep them from drifting.

### Removed

- **The `uv`-based Python provisioning install path is gone.** The host
  no longer needs Python. The `pila` launcher is now a portable bash
  script that shells out to `nerdctl run`. The `scripts/install.sh`
  runtime preflight on macOS checks for `colima` and on Linux checks
  for `nerdctl`; it no longer installs `uv` or provisions Python 3.12.
  Existing users upgrading: there is no migration — install the
  container runtime per `docs/INSTALL.md` and re-run the installer.

- **All legacy / backwards-compat code paths.** Pila now has **no
  migration path from prior versions** — start fresh. Specifically:
  the `cleanup.sh --legacy` mode and the `.pila/state.json`
  detection guard in `main()` (which together migrated installations
  off the pre-per-run layout) are deleted; the `validate_resume_state`
  check that rejected pre-inversion `no_clarify` state files is
  deleted (legacy state's orphan key now does nothing); the
  `ask`-value-specific rejection tests and doc sentences are deleted
  (the underlying validation gates still reject any unknown value —
  they are not legacy-specific).
- **`ask` source-of-truth value.** The four-value preference
  (`codebase` / `research` / `both` / `ask`) collapses to three.
  Default is now `both` (codebase first; research as fallback) — the
  preference is never surfaced as an interactive question, because
  setting `--source-of-truth` / `PILA_SOURCE_OF_TRUTH` /
  `source_of_truth` in `pila.toml` already expresses an explicit
  intent, and an unset preference implicitly accepts `both`.
  `gather_answers` no longer prompts for source-of-truth or emits the
  `source_of_truth` / `source_of_truth_hint` fields in
  `pending-questions.json`.

### Added

- `reconciler` worker. Spawned by the orchestrator between `phase_plan`
  and `schedule` when parallel planners disagree on capability-tag
  vocabulary across domains. The reconciler resolves the mismatch via
  renames, added `provides`, or new connector subtasks; genuinely
  unresolvable gaps abort the run with the worker's diagnosis instead
  of the prior opaque "nothing provides X" error. Short-circuits with
  no worker invocation when planners already agreed (DESIGN.md §5,
  §14). Reconciler-emitted subtask `id` collisions — both with
  existing subtasks and with other reconciler-emitted ids — now fail
  loud; the prior silent-overwrite path through `schedule()`'s
  dict-flatten would have lost a subtask from the DAG.

### Changed

- **Finalize no longer merges the run branch into the working branch
  locally.** Phase 6 now verifies the run branch is non-empty, pushes it
  to `origin`, and opens a PR via `gh pr create --base <working-branch>
  --head pila/runs/<run-id>`. The working branch is **not** modified
  locally; the PR is the proposed integration. Previously, a successful
  run landed a `pila: integrate completed run into <working-branch>`
  merge commit on the working branch *and* opened a PR with the same
  base, duplicating the same change in two places. `--no-push` still
  skips the push + PR step (the run branch is left local-only; the
  working branch is unchanged). The `scripts/finalize.sh` script is now
  a thin verifier (no `git checkout`, no `git merge`); the two
  post-merge sanity checks in `phase_finalize` are removed (they
  assumed a merge had just happened on HEAD).

- **Per-subtask branches are auto-deleted at finalize.** A new
  `cleanup.sh --subtask-branches` flag (mutually exclusive with
  `--branches`) is now invoked from `phase_finalize` after push+PR. It
  deletes every `pila/subtasks/<run-id>/*` branch and keeps the
  run branch `pila/runs/<run-id>` (the PR head must outlive the
  orchestrator). The per-subtask commits remain reachable from the run
  branch's `--no-ff` merge graph; the per-worker audit trail is now
  `git log pila/runs/<run-id> --graph`. Previously every successful
  run left ~17–20 orphan subtask branches that the user had to delete
  by hand.

- **Model defaults flipped to a judgment-vs-implementation split.**
  Judgment workers (`classifier`, `planner`, `reconciler`,
  `integrator`, `validator`) now default to `opus`; `implementer`
  defaults to `sonnet`. Previously every worker defaulted to `sonnet`.
  The split prioritizes Opus-grade reasoning on the steps where a
  wrong call is most costly (decomposition, conflict resolution,
  cross-domain wiring, criterion judgment) while keeping the
  most-frequently-invoked worker on the cheaper model. **Cost note:**
  Opus is materially more expensive per token than Sonnet; a typical
  run is meaningfully more expensive than before. To restore the
  pre-0.3 all-sonnet behavior in one knob, set `--model sonnet`,
  `PILA_MODEL=sonnet`, or `model = sonnet` in `pila.toml`.
  Per-worker overrides (`--model-<worker>`, `PILA_MODEL_<WORKER>`,
  `model_<worker>`) let you dial individual workers independently.

- `validate_checkpoint()` rejects a wider set of placeholder tokens.
  The single-token noise list now includes `nothing`, `unknown`, `todo`,
  and `pending`, and a normalization step strips trailing `.`/`!`/`…`
  and collapses pure-`?` runs before the membership check — so `None.`,
  `TBD!`, and `???` are caught alongside the bare forms. The two
  "nothing-to-report-is-OK" sections (`Decisions made`, `Open unknowns`)
  continue to accept these. Effect: a previously-accepted thin handoff
  that used any of the new variants now fails the checkpoint validation
  and the orchestrator routes the subtask to `blocked` per the existing
  rule.

### Deprecated

### Removed

### Fixed

- **`phase_finalize` now passes `--run-id` to `cleanup.sh`.** The previous
  bare `cleanup.sh` invocation hit the script's interactive no-arg path,
  which scans for the most-recently-failed run and prompts y/N on stdin.
  The orchestrator runs cleanup non-interactively, so `read -r answer`
  silently saw EOF, the script exited 0 without doing anything, and the
  orchestrator continued past it. Every successful run was leaving its
  full set of subtask worktrees on disk under
  `.pila/runs/<run-id>/worktrees/` despite the "cleanup ran" log
  line. A defense-in-depth pin in `phase_finalize` now asserts the
  invocation includes the run id.

### Security

## [0.2.0] - 2026-05-24

### Added

- Initial public release. Deterministic Python orchestrator for Claude Code;
  six-phase classify → clarify → plan → schedule → execute → finalize
  pipeline; per-wave parallel implementers in isolated git worktrees;
  evidence-gated implement/validate loop; JSON-schema-validated worker
  outputs; resumable state; pytest suite covering deterministic
  enforcement functions.
- Per-worker model selection. Default `sonnet`; override with `--model`
  (sets all five workers) or `--model-<worker>` (per-worker; values:
  `sonnet` / `opus` / `haiku`). Env equivalents `PILA_MODEL` and
  `PILA_MODEL_<WORKER>`; TOML keys `model` and `model_<worker>` in
  `pila.toml`. Resolution order, highest first: per-worker CLI →
  global CLI → per-worker env → global env → per-worker TOML → global
  TOML → default. Invalid values rejected at startup. Models are
  re-resolved on `--resume` (not persisted in state).
- `--source-of-truth` CLI flag for one-off overrides of the
  `PILA_SOURCE_OF_TRUTH` env var and `pila.toml`.

### Changed

- Source-of-truth resolution precedence flipped: env var now beats
  `pila.toml` (and the new `--source-of-truth` flag beats both).
  CLI/env are session-scoped knobs; `pila.toml` is the committed
  repo default.

[Unreleased]: https://github.com/enricai/pila/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/enricai/pila/releases/tag/v0.2.1
[0.2.0]: https://github.com/enricai/pila/releases/tag/v0.2.0

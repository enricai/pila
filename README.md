# Pila

**Pila** is an autonomous task driver for Claude Code. One prompt. Finished, committed, validated code. No steering mid-run, no polishing when it's done.

Most tools that call themselves autonomous still require you: to confirm a direction, catch a hallucination, or clean up the result before it's usable. Pila doesn't. It classifies the task, decomposes it, implements each piece in parallel isolated worktrees, validates the integrated result, and merges — beginning to end, unattended.

It runs entirely on the **Claude Code CLI and your existing subscription** — no Anthropic API key, no per-call billing. If you have Claude Code installed and logged in, you have everything it needs.

**Why it actually finishes without you:**

Most AI "orchestrators" let the model pilot: the model decides what to do next, declares when it's done, and judges whether it succeeded. That's where drift, hallucinated completion, and silent failures come from — and why you end up steering.

Pila inverts the relationship. **The model writes code. The program runs everything else.** Phases, wave scheduling, retries, caps, merge logic, and success-criteria enforcement are ordinary Python — real loops and conditionals that cannot drift.

- **No silent failures.** Every worker output is JSON-schema-validated before the orchestrator acts on it. A worker cannot, by malformed output or confident hallucination, cause the system to do something undefined.
- **Confidence is the only hard gate.** The implementer self-gates on evidence-anchored confidence in `root_cause` and `solution` (≥9 on both, see DESIGN.md §8) — falsifiers tested, contradictions reconciled, gaps named with concrete artifacts. A worker that cannot justify the score exits `blocked` with the gap analysis. Everything else — tests passing, lint clean, build green, per-criterion satisfaction — is best-effort: surfaced as advisory warnings on the subtask result, never escalated to `failed` or `blocked` by the orchestrator. The criteria file is the implementer's working note, not a gate.
- **Workers must justify confidence with evidence, not feelings.** Before writing code, an implementer clears domain-specific evidence gates — file-and-line citations, reproductions, falsification attempts. A self-reported score without hard artifacts doesn't clear the bar.
- **Parallel work that's actually safe.** Each implementer gets an isolated git worktree. Parallel writes never collide. Conflicts surface one wave at a time, close to the work that caused them.
- **Resumable by design.** A reboot, network blip, budget cap, the Claude Code subscription rate-limit, Ctrl-C, or an external kill (SIGTERM from CI / systemd / a closed terminal) all lose nothing — the run branch is the durable record, worktrees are torn down, and `--resume` picks up from the last completed wave. When the subscription rate-limit hits and the reset time is unambiguously parseable, pila even auto-resumes after the reset window without manual intervention. The explicit "throw this away" gesture is `scripts/cleanup.sh --run-id <id> --branches`, not Ctrl-C.
- **Parallel-safe across runs.** Multiple `./pila` invocations in the same repository each get a unique `run_id` (a derived branch + state directory). Their branches, worktrees, and `.pila/` state never collide. Launch a fix and a feature in parallel without coordination.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![tests](https://github.com/enricai/pila/actions/workflows/test.yml/badge.svg)](https://github.com/enricai/pila/actions/workflows/test.yml)
[![syntax](https://github.com/enricai/pila/actions/workflows/syntax.yml/badge.svg)](https://github.com/enricai/pila/actions/workflows/syntax.yml)
[![shellcheck](https://github.com/enricai/pila/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/enricai/pila/actions/workflows/shellcheck.yml)
[![Version](https://img.shields.io/badge/version-0.2.1-orange.svg)](CHANGELOG.md)

## How it works

The orchestrator is a Python program — not an in-session agent. It shells out
to `claude -p` (headless mode) for each unit of LLM work. Each call is a
separate process, so there is no subagent nesting anywhere. Control flow lives
in real Python: `for` loops, `if` statements, counters. It cannot drift.

```
pila "<task>"
   ├─ Phase 1  Classify into 1..8 categories                    → 1 claude -p
   │             ↓ derive run_id (category + slug + start-hex)
   ├─ Phase 0  Clarify — intent-only questions, default zero
   ├─ Phase 2  Plan — one planner per category (parallel)        → N claude -p
   │             ↓ reconcile cross-domain capability tags          → 0 or 1 claude -p
   ├─ Phase 3  Schedule — global dependency graph → topo waves   (pure Python)
   ├─ Phase 4  Create pila/runs/<run-id> branch + worktree (per-run unique)
   ├─ Phase 5  Per wave: implement (parallel, isolated worktrees) → claude -p each
   │           integrate into the run branch; validate the run branch
   └─ Phase 6  Push run branch; open PR against working branch; cleanup
               (working branch not modified locally)
```

For the full rationale — why the orchestrator is a script rather than a plugin
command, all architectural decisions, and the complete enforcement surface —
read [`docs/DESIGN.md`](docs/DESIGN.md).

## Requirements

- `claude` CLI on `PATH`, logged in interactively
- `git`
- A git repository with `user.email` and `user.name` configured
- A reasonably clean working tree
- A container runtime (one-time setup — see *Install* below)
- `gh` CLI logged in (`gh auth status` succeeds), or pass `--no-push` to skip the finalize PR step

**Pila runs inside a container** to give cleanup a hard kernel
guarantee: when you Ctrl-C, the Linux PID namespace is torn down and
every worker / build / test runner is reaped, even ones that detached
into their own POSIX sessions. See
[`docs/DESIGN.md` §6](docs/DESIGN.md) and
[`docs/IMPLEMENTATION.md` §0.5](docs/IMPLEMENTATION.md) for the
reasoning and mechanics. Python is provisioned *inside* the container
by the image; you don't need it on the host.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
```

The installer auto-installs and starts the container runtime per OS
(Colima on macOS via `brew`; containerd + pinned `nerdctl` on
Debian/Ubuntu, Fedora/RHEL, and Arch via the distro package manager)
and then clones pila into `~/.pila` + symlinks `pila` into
`~/.local/bin`. Sudo prompts apply on Linux. Full per-OS details and
the rootless / unsupported-distro paths live in
[`docs/INSTALL.md`](docs/INSTALL.md).

### Inside Claude Code (recommended for chat-based use)

```
/plugin marketplace add enricai/pila
/plugin install pila@enricai-pila
```

Then in any Claude Code session:

```
/pila Fix the login timeout bug and add a regression test
```

### Inspect before installing

```bash
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh -o install.sh
bash install.sh --dry-run            # print actions without executing
bash install.sh                       # then run for real
```

Customize with `--prefix DIR` (default `~/.pila`), `--bin-dir DIR`
(default `~/.local/bin`), or `--ref REF` (default `main`).

### Manual container-runtime setup

If you'd rather install the runtime yourself (CI, dotfiles managers,
or you want to pin a different `nerdctl` version), do the runtime
steps manually then pass `--no-runtime-install` (or set
`PILA_NO_RUNTIME_INSTALL=1`):

**macOS** (Colima manages a Linux VM):

```bash
brew install colima
# Size the VM at ~half your host's CPU/RAM (Colima's 2-CPU / 2-GB
# default OOMs under parallel pila workloads — see docs/INSTALL.md
# for the auto-sizing the installer applies). On an 8/16 host:
colima start --runtime containerd --mount-type virtiofs --cpu 4 --memory 8
# Also add 4 GB of swap (paste the YAML block from docs/INSTALL.md
# "Memory pressure: swap configuration" into ~/.colima/default/colima.yaml,
# then colima stop && colima start). This step is optional but strongly
# recommended — without swap the VM OOMs under heavy parallel load.
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash -s -- --no-runtime-install
```

(Do not `brew install nerdctl` — the formula requires Linux. Pila
auto-installs the host-side `nerdctl` shim from Colima on first run.)

**Linux** (Debian/Ubuntu — see [`docs/INSTALL.md`](docs/INSTALL.md)
for Fedora, Arch, and rootless setups):

```bash
sudo apt-get install -y containerd
NERDCTL_VERSION=2.3.1
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
curl -L "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${ARCH}.tar.gz" \
  | sudo tar -C /usr/local/bin -xz nerdctl
sudo systemctl enable --now containerd
curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash -s -- --no-runtime-install
```

### Manual (clone + run)

If you'd rather not run any installer at all:

```bash
git clone https://github.com/enricai/pila.git
./pila/pila "your task"   # or symlink onto PATH
```

The first invocation builds the container image (~60–120s); subsequent
runs reuse it. The container runtime must already be set up — see
[`docs/INSTALL.md`](docs/INSTALL.md) for per-OS instructions.

## Usage

```bash
# From the root of the target git repository:
pila "Fix the login timeout bug and add a regression test"
# (substitute pila if you used the manual install)

# Or pass a path to a .txt / .md file whose contents are the task —
# useful for multi-paragraph briefs that are awkward to quote on the shell:
pila path/to/task.md

# Resume an interrupted or budget-capped run. Auto-picks if exactly one
# in-flight run exists; otherwise requires --run-id (see `--list`).
pila --resume
pila --resume --run-id fix-login-timeout-bug-b81e90

# List in-flight and completed runs in this repository:
pila --list

# Skip the default push + PR at finalize (run completes with the run
# branch local-only; your working branch is unchanged):
pila "task" --no-push

# Skip pre-push hooks at finalize (the user's explicit override; defaults
# off). Affects only the final `git push`; worker commits still run hooks.
pila "task" --no-verify

# Opt into intent questions (default: no questions are surfaced).
pila "task" --clarify

# Pre-supply clarification answers (JSON object):
# Keys are question ids from the classifier, plus "source_of_truth"
# set to "codebase", "research", or "both".
pila "task" --answers answers.json

# Override caps (defaults: 60 total workers, 4 in parallel per wave).
# --max-workers also reads PILA_MAX_WORKERS or max_workers in
# pila.toml; --max-parallel is CLI-only.
pila "task" --max-workers 80 --max-parallel 6
export PILA_MAX_WORKERS=80

# Dial how persistent the planner and implementer are at building
# confidence before they exit blocked (default 8 evidence-gate rounds
# inside each worker; see DESIGN §8):
pila "task" --confidence-rounds 12
export PILA_CONFIDENCE_ROUNDS=12

# Override the default source-of-truth preference (`both`) — pass
# --source-of-truth on the command line for a one-off, set
# PILA_SOURCE_OF_TRUTH for the session, or commit a pila.toml
# at the repo root with the line `source_of_truth = codebase` (or
# research / both).
# Precedence (highest first): --source-of-truth > env > pila.toml.
export PILA_SOURCE_OF_TRUTH=codebase    # or: research, both
pila "task" --source-of-truth codebase

# Choose the model. Without overrides, judgment workers (classifier /
# planner / reconciler / integrator) default to opus and the acting
# workers (implementer, conformer) default to sonnet — see
# docs/IMPLEMENTATION.md §2 "Model selection" for the full env-var /
# CLI-flag / TOML-key table.
# Set PILA_MODEL=sonnet (or --model sonnet) to restore the
# pre-0.3 all-sonnet behavior in one knob.
export PILA_MODEL=sonnet                # or: opus, haiku
pila "task" --model opus
pila "task" --model-implementer opus --model-classifier haiku

# Optional but recommended — lower the auto-compaction threshold
# for worker processes (default is 95%):
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70
```

Inside Claude Code (after `/plugin install pila@enricai-pila`):

```
/pila Fix the login timeout bug and add a regression test
```

## Configuration

Complete reference for every CLI flag, environment variable, and
`pila.toml` key the orchestrator reads.

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `task` (positional) | — | The task description (literal string, or path to a `.txt`/`.md` file). Required unless `--resume`, `--list`, or `--phase` is given. |
| `--resume` | off | Resume an interrupted run. Auto-picks if exactly one run exists; requires `--run-id` if multiple. |
| `--run-id ID` | — | Select a specific run by id (e.g., for `--resume` or `--phase` when multiple runs are in flight). |
| `--list` | off | Enumerate in-flight and completed runs in this repository (run id, started, status, branch). |
| `--no-push` | off | Skip the default push + PR at finalize. The run completes with the run branch local-only; your working branch is unchanged. Overrides `PILA_NO_PUSH` / `pila.toml`. |
| `--remote` | off | Route execution to a remote backend (Fly.io) instead of the local `nerdctl run`. Consumed by the launcher before `REWRITTEN_ARGS`; the orchestrator never sees it. Also `PILA_REMOTE` env var or `remote = true` in `pila.toml`. |
| `--no-verify` | off | Pass `--no-verify` to the finalize `git push` only (skips pre-push hooks). Worker commits inside worktrees still run all hooks. The user's explicit override per CLAUDE.md's hooks principle. |
| `--answers FILE` | — | JSON object of pre-supplied clarification answers (keyed by question `id`; may include `source_of_truth`). |
| `--clarify` | off | Opt into surfacing intent questions to the user. Default: questions are dropped after the classifier's codebase→research filter, and the implementer makes a documented best-effort decision. Also `PILA_CLARIFY` env var or `clarify = true` in `pila.toml`. |
| `--max-workers N` | `60` | Cap on total `claude -p` invocations across the run. Also `PILA_MAX_WORKERS` env var or `max_workers` in `pila.toml`. |
| `--max-parallel N` | `4` | Cap on concurrent workers within a wave. |
| `--confidence-rounds N` | `8` | Evidence-gate rounds the planner and implementer may run before exiting blocked (DESIGN §8). Overrides `PILA_CONFIDENCE_ROUNDS` and `pila.toml`. |
| `--skip-smoke` | off | Skip the live `claude -p` preflight smoke test. |
| `--source-of-truth VALUE` | `both` | `codebase` / `research` / `both`. Overrides `PILA_SOURCE_OF_TRUTH` and `pila.toml`. |
| `--runtime VALUE` | `local` | `local` / `fly`. Execution backend for per-subtask worker containers. Overrides `PILA_RUNTIME` and `pila.toml`. |
| `--inspect-dir PATH` | none | Extra directory the inspect-bucket workers (classifier, planner, reconciler, provision) may read; forwarded to `claude -p` as `--add-dir`. Repeatable. Also `PILA_INSPECT_DIRS` (colon-separated) or `inspect_dirs` in `pila.toml` (comma-separated). |
| `--model ALIAS` | per-worker (judgment: `opus`; acting workers — implementer, conformer: `sonnet`) | `sonnet` / `opus` / `haiku`. Sets every worker this run; without it the per-worker defaults apply. |
| `--model-<worker> ALIAS` | per-worker default (`implementer`, `conformer` → `sonnet`; everything else → `opus`) | Per-worker override. `<worker>` is one of `classifier`, `planner`, `reconciler`, `provision`, `implementer`, `integrator`, `conformer`. Overrides `--model`, `PILA_MODEL`, and `pila.toml`. |
| `--judge-model ALIAS` | `sonnet` | Model alias for the post-run judge skill. Also `PILA_MODEL_JUDGE` or `model_judge` in `pila.toml`. |
| `--heal-model ALIAS` | `sonnet` | Model alias for the post-run self-heal skill. Also `PILA_MODEL_HEAL` or `model_heal` in `pila.toml`. |
| `--heal-max-rounds N` | `10` | Maximum heal-loop iterations per `call_type`. Also `PILA_HEAL_MAX_ROUNDS` or `heal_max_rounds` in `pila.toml`. |
| `--heal-success-threshold RATE` | `0.9` | Pass-rate threshold for the heal-loop SUCCESS verdict. Also `PILA_HEAL_SUCCESS_THRESHOLD` or `heal_success_threshold` in `pila.toml`. |
| `--verbosity LEVEL` | `stream` | `quiet` / `normal` / `stream` / `debug`. Controls inline per-worker activity output; full per-worker stream is always saved to `.pila/logs/<sid>.log`. |
| `-v` / `-vv` | `0` (off) | Shortcuts that anchor to `normal`: `-v` = `stream`, `-vv` = `debug`. With no `-v` and no `--verbosity`, falls through to `PILA_VERBOSITY` / `pila.toml` / default `stream`. |
| `-q` / `-qq` | `0` (off) | Shortcuts that anchor to `normal`: `-q` = `normal` (pre-streaming behavior), `-qq` = `quiet`. With no `-q` and no `--verbosity`, falls through to the same chain as `-v`. |
| `--telemetry` / `--no-telemetry` | on | Enable / disable telemetry NDJSON event writing. Also `PILA_TELEMETRY=1`/`0` or `telemetry=true`/`false` in `pila.toml`. |
| `--telemetry-dir DIR` | `events` | Subdirectory name under the run dir for telemetry NDJSON events. Also `PILA_TELEMETRY_DIR` or `telemetry_dir` in `pila.toml`. |
| `--judge-dir DIR` | `judge-out` | Subdirectory name under the run dir for LLM judge output. Also `PILA_JUDGE_DIR` or `judge_dir` in `pila.toml`. |
| `--heal-dir DIR` | `heal-out` | Subdirectory name under the run dir for LLM self-heal output. Also `PILA_HEAL_DIR` or `heal_dir` in `pila.toml`. |
| `--phase PHASE` | — | Run a post-run skill phase (`judge` or `heal`) against an existing run's captured LLM calls instead of starting a new run. Use `--run-id` to select when multiple runs exist. |

### Environment variables and `pila.toml` keys

| Env var | `pila.toml` key | Description |
|---------|---------------------|-------------|
| `PILA_SOURCE_OF_TRUTH` | `source_of_truth` | Sticky source-of-truth preference (`codebase` / `research` / `both`). Overridden by `--source-of-truth`. Unset → default `both`. |
| `PILA_RUNTIME` | `runtime` | Execution backend for per-subtask worker containers (`local` / `fly`). Overridden by `--runtime`. Unset → default `local`. |
| `PILA_MODEL` | `model` | Model alias applied to every worker. Overridden by `--model` and per-worker overrides. Unset → per-worker defaults (judgment workers `opus`, acting workers — implementer, conformer — `sonnet`). |
| `PILA_MODEL_<WORKER>` | `model_<worker>` | Per-worker override (e.g. `PILA_MODEL_IMPLEMENTER=opus`). Overridden by `--model-<worker>`. `<worker>` ∈ `classifier`, `planner`, `reconciler`, `provision`, `implementer`, `integrator`, `conformer`. Unset → `implementer` and `conformer` → `sonnet`; everything else → `opus`. |
| `PILA_CONFIDENCE_ROUNDS` | `confidence_rounds` | Evidence-gate rounds per worker (positive integer). Overridden by `--confidence-rounds`. Unset → default `8`. |
| `PILA_INSPECT_DIRS` | `inspect_dirs` | Extra directories the inspect-bucket workers (classifier, planner, reconciler, provision) may read; forwarded as `--add-dir`. Env value is colon-separated; TOML value is comma-separated. Overridden by `--inspect-dir` (repeatable). Unset → none. |
| `PILA_VERBOSITY` | `verbosity` | Inline-output verbosity (`quiet` / `normal` / `stream` / `debug`). Overridden by `--verbosity`. `-v` / `-vv` / `-q` / `-qq` shortcuts override both. Unset → default `stream`. |
| `PILA_NO_PUSH` | `no_push` | Sticky opt-out from push + PR at finalize (truthy → skip). Overridden by `--no-push`. `--no-verify` has no env/TOML mirror — it is a per-invocation override only. Unset → default `false` (push + PR happen). |
| `PILA_REMOTE` | `remote` | Route execution to a remote backend instead of local `nerdctl run` (truthy → remote). Overridden by `--remote`. Unset → default `false` (local container run). |
| `PILA_CLARIFY` | `clarify` | Sticky opt-in to surfacing intent questions to the user (truthy → on). Overridden by `--clarify`. Unset → default `false`. |
| `PILA_MODEL_JUDGE` | `model_judge` | Model alias for the post-run judge skill. Overridden by `--judge-model`. Unset → default `sonnet`. |
| `PILA_MODEL_HEAL` | `model_heal` | Model alias for the post-run self-heal skill. Overridden by `--heal-model`. Unset → default `sonnet`. |
| `PILA_HEAL_MAX_ROUNDS` | `heal_max_rounds` | Maximum heal-loop iterations per `call_type`. Overridden by `--heal-max-rounds`. Unset → default `10`. |
| `PILA_HEAL_SUCCESS_THRESHOLD` | `heal_success_threshold` | Pass-rate threshold for the heal-loop SUCCESS verdict. Overridden by `--heal-success-threshold`. Unset → default `0.9`. |
| `PILA_TELEMETRY` | `telemetry` | Enable / disable telemetry NDJSON event writing (boolean). Overridden by `--telemetry` / `--no-telemetry`. Unset → default `true` (telemetry on). |
| `PILA_TELEMETRY_DIR` | `telemetry_dir` | Subdirectory name under the run dir for telemetry NDJSON events. Overridden by `--telemetry-dir`. Unset → default `events`. |
| `PILA_JUDGE_DIR` | `judge_dir` | Subdirectory name under the run dir for LLM judge output. Overridden by `--judge-dir`. Unset → default `judge-out`. |
| `PILA_HEAL_DIR` | `heal_dir` | Subdirectory name under the run dir for LLM self-heal output. Overridden by `--heal-dir`. Unset → default `heal-out`. |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | — | **Claude Code CLI variable**, not consumed by pila. Set to `70` to backstop worker auto-compaction. |

### Precedence

- **Source-of-truth** (highest first): `--source-of-truth` →
  `PILA_SOURCE_OF_TRUTH` → `pila.toml` → default `both`.
- **Model** (per worker, highest first): `--model-<worker>` →
  `--model` → `PILA_MODEL_<WORKER>` → `PILA_MODEL` →
  `model_<worker>` in `pila.toml` → `model` in `pila.toml` →
  per-worker default (`implementer`, `conformer` → `sonnet`; everything
  else → `opus`). The judgment-vs-acting split keeps the
  most-frequently-invoked workers on the lower-cost model while
  every judgment step gets Opus-grade reasoning. To restore the
  pre-0.3 all-sonnet behavior in one knob, set `PILA_MODEL=sonnet`
  or pass `--model sonnet`.
- **Confidence rounds** (highest first): `--confidence-rounds` →
  `PILA_CONFIDENCE_ROUNDS` → `confidence_rounds` in
  `pila.toml` → default `8`.
- **Verbosity** (highest first): `--verbosity` → `-v`/`-vv`/`-q`/`-qq`
  shortcuts (anchored to `normal`, not to the resolved default) →
  `PILA_VERBOSITY` → `verbosity` in `pila.toml` → default
  `stream`.

See [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §2 for the
rationale behind these orders and the full validation contract.

## Worker types

Pila spawns seven kinds of `claude -p` worker. Each is a separate
subprocess; there is no in-session agent nesting.

| Worker | Prompt source | Default model | Runs per task | Returns |
|--------|---------------|---------------|---------------|---------|
| `classifier` | `prompts/classifier.md` | opus | 1 | category set + intent questions |
| `planner` | `prompts/planner.md` | opus | one per category (parallel) | subtask list with deps |
| `reconciler` | `prompts/reconciler.md` | opus | 0 or 1 (spawned only when planners' capability tags don't align) | renames / added_provides / added_subtasks / unresolvable |
| `provision` | `prompts/provision.md` | opus | 0 or 1 (spawned only when the deterministic lockfile-detection table abstains — Java/Gradle, bare `pyproject.toml`, polyglot Makefile) | install recipe (argv-allowlisted) executed via `mise exec --`. See DESIGN §6½ |
| `implementer` | `prompts/implementer.md` | sonnet | one per subtask (per wave, parallel) | commits on a `pila/subtasks/<run-id>/<subtask-id>` branch |
| `conformer` | `prompts/conformer.md` | sonnet | one per subtask, only on the implementer's success path | advisory `conformance_warnings` on the subtask result; doc/test/rule-fix commits prefixed `conformer:` on the same branch (DESIGN §9 *Post-work conformance*) |
| `integrator` | `prompts/integrator.md` | opus | on conflict during wave integration | resolved merge commit on `pila/runs/<run-id>` |

**Per-worker model defaults:** judgment workers (classifier, planner,
reconciler, provision, integrator) default to Opus; the acting workers
(implementer, conformer) default to Sonnet — their job is concrete
subtask execution where throughput matters more than broad-context
judgment. To revert to the
all-Sonnet pattern of earlier versions, set `PILA_MODEL=sonnet` or
pass `--model sonnet`. See [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §2
*Model selection* for the full precedence table.

See [`docs/DESIGN.md`](docs/DESIGN.md) §7 for the worker contract and
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §3 for the invocation
surface (flags, timeouts, schema enforcement).

## Walkthrough

For a worked end-to-end example — from invocation through clarification,
wave execution, run-branch review, and merge — see
[`docs/USAGE.md`](docs/USAGE.md).

## Development

Tests:

```bash
pip install pytest    # only dev dependency
pytest tests/         # from the repo root
```

The suite covers the deterministic enforcement functions, including a
coupling test that the retry-policy markers match the live check-function
strings. See [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §10 for
the test layout. The worker invocation path is not unit-tested (a stub or
live `claude` binary would be needed; out of scope for the current suite).

## Files

| Path | What it is |
|------|------------|
| `orchestrator/pila.py` | The orchestrator — all phases, waves, caps, retries |
| `prompts/classifier.md` | System prompt: classify task + surface intent questions |
| `prompts/planner.md` | System prompt: decompose one category into a subtask plan |
| `prompts/reconciler.md` | System prompt: reconcile cross-domain capability-tag drift between planner outputs |
| `prompts/implementer.md` | System prompt: execute one subtask end to end |
| `prompts/integrator.md` | System prompt: resolve merge conflicts behaviorally |
| `scripts/setup-run.sh` | Create per-run branch + worktree (`pila/runs/<run-id>`) |
| `scripts/new-worktree.sh` | Create per-subtask branch + worktree off the run branch |
| `scripts/integrate.sh` | Merge a subtask branch into the run branch |
| `scripts/finalize.sh` | Verify the run branch is non-empty and ready to push (the working branch is not modified locally — the push + PR step lives in Python's `push_and_open_pr`, called from `phase_finalize` unless `--no-push`) |
| `scripts/cleanup.sh` | Remove worktrees for one run (default `--run-id`) or all runs (`--all-runs`). State dir always preserved as audit. `--branches` also deletes the matching `pila/runs/<id>` run branch *and* `pila/subtasks/<id>/*` subtask branches. `--subtask-branches` deletes only the subtask branches and keeps `pila/runs/<id>` (the post-finalize default — the run branch is the PR head). `--bootstrap` removes orphaned `_bootstrap-*` dirs (runs that died before classify completed). |
| `scripts/remote/build-push.sh` | Build and push a self-contained pila image to Fly.io's registry (source baked in at `/work/.pila-image/`). See `docs/IMPLEMENTATION.md` §0.5 *Registry publish path*. |
| `pila` | Executable entry-point wrapper |
| `commands/pila.md` | Thin plugin skill — reachable as `/pila` from Claude Code |
| `docs/DESIGN.md` | Full design document and rationale |
| `docs/IMPLEMENTATION.md` | Current code-surface spec (functions, caps, schemas) |
| `docs/USAGE.md` | End-to-end walkthrough of one Pila run |
| `CONTRIBUTING.md` | Development setup, task-completion checklist, PR conventions |

## Safety

Acting workers use `--dangerously-skip-permissions`. That is a real risk
surface — it is what makes the run unattended. It is bounded by **two
isolation layers**: (1) worktree isolation — each worker operates in its
own isolated git checkout, not your main working tree; (2) the container
the orchestrator runs in — PID-namespace + cgroups bound every worker
subprocess inside the per-run container (see
[`docs/DESIGN.md`](docs/DESIGN.md) §6 and [`SECURITY.md`](SECURITY.md)).
These bound the blast radius; they do not eliminate it. **Run on
repositories you trust and review the run branch (`pila/runs/<run-id>`)
before relying on the result.** Push + PR at finalize is the natural
review surface; you can also pass `--no-push` to keep finalize fully
local.

The run writes only to `.pila/runs/<run-id>/` (auto-excluded from git
via `.git/info/exclude`) and to `pila/runs/<run-id>` plus
`pila/subtasks/<run-id>/<subtask-id>` branches. Phase 6 (unless
`--no-push`) pushes the run branch to `origin` and opens a PR against
your working branch — your working branch itself is never modified
locally. After a run, the run branch (`pila/runs/<run-id>`) is kept
as an audit trail; per-subtask branches are auto-deleted at finalize,
but each worker's commits remain reachable from the run branch's
`--no-ff` merge graph (`git log pila/runs/<run-id> --graph`). Remove
the run branch (and any leftover subtask branches) with
`scripts/cleanup.sh --run-id <id> --branches` (or `--all-runs --branches`
for an audit cleanup across every past run).

## Troubleshooting

- **`claude: command not found`** — Pila shells out to the Claude Code
  CLI; install it from https://claude.ai/code and confirm with
  `claude --version`. There is no fallback path.

- **Exits with code 10** — not an error. Pila needs clarification
  answers and you are running non-interactively. Read
  `.pila/pending-questions.json`, write the answers to
  `.pila/answers.json`, then `./pila --resume --answers .pila/answers.json`.
  The plugin skill at `commands/pila.md` handles this relay
  automatically when invoked as `/pila`.

- **Run interrupted (Ctrl-C, SIGTERM, SIGHUP, CI cancel, terminal close, reboot)** —
  worktrees are torn down but state.json + branches are preserved.
  Resume with `./pila --resume` (auto-picks if exactly one in-flight
  run) or `./pila --resume --run-id <id>`. Run `pila --list` to see
  what's in flight. The explicit "throw this away" command is
  `scripts/cleanup.sh --run-id <id> --branches` — Ctrl-C alone is
  always safely resumable.

- **Run hit the Claude Code subscription rate-limit** — pila detects
  the session-limit message from `claude -p` and exits cleanly.
  Worktrees are torn down; state and branches are preserved. When the
  reset time can be parsed unambiguously, pila sleeps until the reset
  window and auto-resumes itself. When it cannot (malformed time,
  unfamiliar timezone, or a future format change), pila exits with
  code 75 and prints the manual resume command — re-run that command
  yourself once the rate-limit clears. Auto-resume passes only
  `--resume --run-id <id>`; CLI-only overrides (`--model`,
  `--max-workers`, etc.) on the original launch are *not* preserved
  across an auto-resume. Set those via env (`PILA_*`) or `pila.toml`
  if you want them to survive — both channels are re-resolved on
  every `--resume`.

- **A subtask reports `blocked`** — the implementer hit something it
  cannot resolve and bailed before integration. Read the blocker reason in
  `.pila/state.json` under `blocked[<subtask-id>]`, address the
  upstream cause, then resume. See [`docs/DESIGN.md`](docs/DESIGN.md) §8
  for the evidence-gated loop.

- **Worktree or branch conflicts on a re-run** — `scripts/cleanup.sh --run-id <id> --branches`
  removes that run's worktrees and deletes its branches so a fresh run
  with the same task starts clean. For a global sweep across every past
  run, use `--all-runs --branches`. Then re-invoke as normal.

- **Push or PR failed at finalize** — the run completed locally. Check
  `pila --list` for the run's status (`push-failed` / `pr-failed`)
  and read `.pila/runs/<run-id>/run.json` for the captured stderr.
  The error message at finalize names the exact retry command. Local
  commits are intact on the run branch.

## FAQ

**Do I need an Anthropic API key?**
No. Pila runs entirely on the Claude Code CLI and your existing
subscription. The orchestrator shells out to `claude -p` workers; no API
key is read or sent.

**Can I run multiple Pila instances in the same repository?**
Yes. Each invocation derives a unique `run_id` and namespaces all of its
state under `.pila/runs/<run-id>/` and its branches under
`pila/runs/<run-id>` (run branch) and `pila/subtasks/<run-id>/<sid>`
(subtask branches) — so parallel runs in the same clone never collide.
Use `--list` to see what's in flight and `--resume --run-id <id>` to
resume a specific one.

**Does Pila work outside a git repository?**
No. Per-subtask isolation is provided by `git worktree`; the worktree
mechanism is load-bearing, not optional.

**What if my project has no test runner?**
That's fine — running tests is advisory only (DESIGN §9 *Post-work
conformance*). When `detect_test_runner()` finds nothing, the
conformance phase reports the test axis as not-applicable and surfaces
no warning. The subtask's terminal status is determined by the
implementer's confidence gate (DESIGN §8), not by whether tests ran.
See [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §4 for
`detect_test_runner()` and §5 for the conformance phase's advisory
contract.

**Can I see what each worker did?**
Yes. Every worker commits to its own `pila/subtasks/<run-id>/<subtask-id>`
branch during the run; at finalize, those branches are auto-deleted, but
the integrator merges each one into the run branch with `--no-ff`, so
every worker's commits remain reachable from `pila/runs/<run-id>` as a
named merge bubble. `git log pila/runs/<run-id> --graph` is your
per-worker audit trail. When you no longer need the run branch either,
`scripts/cleanup.sh --run-id <id> --branches` removes it (and any
leftover subtask branches); `--all-runs --branches` removes all of them.

**Why not use the Claude Code SDK or the in-session Agent tool?**
Two platform constraints make subprocess workers the right shape. See
[`docs/DESIGN.md`](docs/DESIGN.md) §2.

## Contributing

Contributions welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for
development setup, the task-completion checklist, and PR conventions.
Security issues: see [`SECURITY.md`](SECURITY.md).

## License

MIT — see [`LICENSE`](LICENSE).

## Status

v0.2.1 — see [`CHANGELOG.md`](CHANGELOG.md). The orchestrator's phase flow, wave scheduling, cross-domain dependency
resolution, and git worktree mechanics are all tested. First contact with a live
`claude -p` session is the remaining verification step. Limitations and planned
work are in [`docs/DESIGN.md`](docs/DESIGN.md).

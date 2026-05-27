# Centella

**Centella** is an autonomous task driver for Claude Code. One prompt. Finished, committed, validated code. No steering mid-run, no polishing when it's done.

Most tools that call themselves autonomous still require you: to confirm a direction, catch a hallucination, or clean up the result before it's usable. Centella doesn't. It classifies the task, decomposes it, implements each piece in parallel isolated worktrees, validates the integrated result, and merges — beginning to end, unattended.

It runs entirely on the **Claude Code CLI and your existing subscription** — no Anthropic API key, no per-call billing. If you have Claude Code installed and logged in, you have everything it needs.

**Why it actually finishes without you:**

Most AI "orchestrators" let the model pilot: the model decides what to do next, declares when it's done, and judges whether it succeeded. That's where drift, hallucinated completion, and silent failures come from — and why you end up steering.

Centella inverts the relationship. **The model writes code. The program runs everything else.** Phases, wave scheduling, retries, caps, merge logic, and success-criteria enforcement are ordinary Python — real loops and conditionals that cannot drift.

- **No silent failures.** Every worker output is JSON-schema-validated before the orchestrator acts on it. A worker cannot, by malformed output or confident hallucination, cause the system to do something undefined.
- **Success criteria are locked at implementation time — enforced by the orchestrator, not the worker.** The implementer cannot weaken its own tests to make them pass. The checker and the thing being checked are never the same agent.
- **Workers must justify confidence with evidence, not feelings.** Before writing code, an implementer clears domain-specific evidence gates — file-and-line citations, reproductions, falsification attempts. A self-reported score without hard artifacts doesn't clear the bar.
- **Parallel work that's actually safe.** Each implementer gets an isolated git worktree. Parallel writes never collide. Conflicts surface one wave at a time, close to the work that caused them.
- **Resumable by design.** A reboot, network blip, budget cap, or external kill (SIGTERM from CI / systemd / a closed terminal) loses nothing — the run branch is the durable record, worktrees are torn down, and `--resume` picks up from the last completed wave. The one exception is Ctrl-C, which is treated as an explicit "throw this away" gesture: the run's branches, worktrees, and state dir are removed and `--resume` cannot recover it. (For a *resumable* abort, prefer `kill <pid>` over Ctrl-C.)
- **Parallel-safe across runs.** Multiple `./centella` invocations in the same repository each get a unique `run_id` (a derived branch + state directory). Their branches, worktrees, and `.centella/` state never collide. Launch a fix and a feature in parallel without coordination.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![tests](https://github.com/enricai/centella/actions/workflows/test.yml/badge.svg)](https://github.com/enricai/centella/actions/workflows/test.yml)
[![syntax](https://github.com/enricai/centella/actions/workflows/syntax.yml/badge.svg)](https://github.com/enricai/centella/actions/workflows/syntax.yml)
[![shellcheck](https://github.com/enricai/centella/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/enricai/centella/actions/workflows/shellcheck.yml)
[![Version](https://img.shields.io/badge/version-0.2.0-orange.svg)](CHANGELOG.md)

## How it works

The orchestrator is a Python program — not an in-session agent. It shells out
to `claude -p` (headless mode) for each unit of LLM work. Each call is a
separate process, so there is no subagent nesting anywhere. Control flow lives
in real Python: `for` loops, `if` statements, counters. It cannot drift.

```
centella "<task>"
   ├─ Phase 1  Classify into 1..8 categories                    → 1 claude -p
   │             ↓ derive run_id (category + slug + start-hex)
   ├─ Phase 0  Clarify — intent-only questions, default zero
   ├─ Phase 2  Plan — one planner per category (parallel)        → N claude -p
   │             ↓ reconcile cross-domain capability tags          → 0 or 1 claude -p
   ├─ Phase 3  Schedule — global dependency graph → topo waves   (pure Python)
   ├─ Phase 4  Create centella/runs/<run-id> branch + worktree (per-run unique)
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
- Python 3.10+
- A git repository with `user.email` and `user.name` configured
- A reasonably clean working tree

## Install and run

```bash
# Get Centella (no install step — runs directly from the checkout):
git clone https://github.com/enricai/centella.git
```

```bash
# From the root of the target git repository:
/path/to/centella/centella "Fix the login timeout bug and add a regression test"

# Or pass a path to a .txt / .md file whose contents are the task —
# useful for multi-paragraph briefs that are awkward to quote on the shell:
/path/to/centella/centella path/to/task.md

# Resume an interrupted or budget-capped run. Auto-picks if exactly one
# in-flight run exists; otherwise requires --run-id (see `--list`).
/path/to/centella/centella --resume
/path/to/centella/centella --resume --run-id fix-login-timeout-bug-b81e90

# List in-flight and completed runs in this repository:
/path/to/centella/centella --list

# Skip the default push + PR at finalize (run completes with the run
# branch local-only; your working branch is unchanged):
/path/to/centella/centella "task" --no-push

# Skip pre-push hooks at finalize (the user's explicit override; defaults
# off). Affects only the final `git push`; worker commits still run hooks.
/path/to/centella/centella "task" --no-verify

# Opt into intent questions (default: no questions are surfaced).
/path/to/centella/centella "task" --clarify

# Pre-supply clarification answers (JSON object):
# Keys are question ids from the classifier, plus "source_of_truth"
# set to "codebase", "research", or "both".
/path/to/centella/centella "task" --answers answers.json

# Override caps:
/path/to/centella/centella "task" --max-workers 60 --max-parallel 6

# Dial how persistent the planner and implementer are at building
# confidence before they exit blocked (default 8 evidence-gate rounds
# inside each worker; see DESIGN §8):
/path/to/centella/centella "task" --confidence-rounds 12
export CENTELLA_CONFIDENCE_ROUNDS=12

# Override the default source-of-truth preference (`both`) — pass
# --source-of-truth on the command line for a one-off, set
# CENTELLA_SOURCE_OF_TRUTH for the session, or commit a centella.toml
# at the repo root with the line `source_of_truth = codebase` (or
# research / both).
# Precedence (highest first): --source-of-truth > env > centella.toml.
export CENTELLA_SOURCE_OF_TRUTH=codebase    # or: research, both
/path/to/centella/centella "task" --source-of-truth codebase

# Choose the model. Without overrides, judgment workers (classifier /
# planner / reconciler / integrator / validator) default to opus and
# the implementer defaults to sonnet — see docs/IMPLEMENTATION.md §2
# "Model selection" for the full env-var / CLI-flag / TOML-key table.
# Set CENTELLA_MODEL=sonnet (or --model sonnet) to restore the
# pre-0.3 all-sonnet behavior in one knob.
export CENTELLA_MODEL=sonnet                # or: opus, haiku
/path/to/centella/centella "task" --model opus
/path/to/centella/centella "task" --model-implementer opus --model-classifier haiku

# Optional but recommended — lower the auto-compaction threshold
# for worker processes (default is 95%):
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70
```

Via the thin plugin skill from inside Claude Code:

```bash
claude --plugin-dir /path/to/centella
# then in the session:
/centella Fix the login timeout bug and add a regression test
```

## Configuration

Complete reference for every CLI flag, environment variable, and
`centella.toml` key the orchestrator reads.

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `task` (positional) | — | The task description. Required unless `--resume` or `--list` is given. |
| `--resume` | — | Resume an interrupted run. Auto-picks if exactly one run exists; requires `--run-id` if multiple. |
| `--run-id ID` | — | Select a specific run by id (e.g., for `--resume` when multiple runs are in flight). |
| `--list` | — | Enumerate in-flight and completed runs in this repository (run id, started, status, branch). |
| `--no-push` | off | Skip the default push + PR at finalize. The run completes with the run branch local-only; your working branch is unchanged. Overrides `CENTELLA_NO_PUSH` / `centella.toml`. |
| `--no-verify` | off | Pass `--no-verify` to the finalize `git push` only (skips pre-push hooks). Worker commits inside worktrees still run all hooks. The user's explicit override per CLAUDE.md's hooks principle. |
| `--answers FILE` | — | JSON object of pre-supplied clarification answers (keyed by question `id`; may include `source_of_truth`). |
| `--clarify` | off | Opt into surfacing intent questions to the user. Default: questions are dropped after the classifier's codebase→research filter, and the implementer makes a documented best-effort decision. Also `CENTELLA_CLARIFY` env var or `clarify = true` in `centella.toml`. |
| `--max-workers N` | 40 | Cap on total `claude -p` invocations across the run. |
| `--max-parallel N` | 4 | Cap on concurrent workers within a wave. |
| `--confidence-rounds N` | 8 | Evidence-gate rounds the planner and implementer may run before exiting blocked (DESIGN §8). Overrides `CENTELLA_CONFIDENCE_ROUNDS` and `centella.toml`. |
| `--skip-smoke` | off | Skip the live `claude -p` preflight smoke test. |
| `--source-of-truth VALUE` | `both` | `codebase` / `research` / `both`. Overrides `CENTELLA_SOURCE_OF_TRUTH` and `centella.toml`. |
| `--model ALIAS` | per-worker (judgment: `opus`; implementer: `sonnet`) | `sonnet` / `opus` / `haiku`. Sets every worker this run; without it the per-worker defaults apply. |
| `--model-<worker> ALIAS` | inherits `--model` | Per-worker override. `<worker>` is one of `classifier`, `planner`, `reconciler`, `implementer`, `integrator`, `validator`. |
| `--verbosity LEVEL` | `stream` | `quiet` / `normal` / `stream` / `debug`. Controls inline per-worker activity output; full per-worker stream is always saved to `.centella/logs/<sid>.log`. |
| `-v` / `-vv` | — | Shortcuts: `-v` = `stream` (default), `-vv` = `debug`. |
| `-q` / `-qq` | — | Shortcuts: `-q` = `normal` (pre-streaming behavior), `-qq` = `quiet`. |

### Environment variables and `centella.toml` keys

| Env var | `centella.toml` key | Description |
|---------|---------------------|-------------|
| `CENTELLA_SOURCE_OF_TRUTH` | `source_of_truth` | Sticky source-of-truth preference (`codebase` / `research` / `both`). Overridden by `--source-of-truth`. Unset → default `both`. |
| `CENTELLA_MODEL` | `model` | Model alias applied to every worker (beats the per-worker defaults). Overridden by `--model` and per-worker overrides. |
| `CENTELLA_MODEL_<WORKER>` | `model_<worker>` | Per-worker default (e.g. `CENTELLA_MODEL_IMPLEMENTER=opus`). Overridden by `--model-<worker>`. `<worker>` ∈ `classifier`, `planner`, `reconciler`, `implementer`, `integrator`, `validator`. |
| `CENTELLA_CONFIDENCE_ROUNDS` | `confidence_rounds` | Evidence-gate rounds per worker (positive integer, default 8). Overridden by `--confidence-rounds`. |
| `CENTELLA_VERBOSITY` | `verbosity` | Inline-output verbosity (`quiet` / `normal` / `stream` / `debug`, default `stream`). Overridden by `--verbosity`. `-v` / `-vv` / `-q` / `-qq` shortcuts override both. |
| `CENTELLA_NO_PUSH` | `no_push` | Sticky opt-out from push + PR at finalize (truthy → skip). Overridden by `--no-push`. `--no-verify` has no env/TOML mirror — it is a per-invocation override only. |
| `CENTELLA_CLARIFY` | `clarify` | Sticky opt-in to surfacing intent questions to the user (truthy → on). Overridden by `--clarify`. |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | — | **Claude Code CLI variable**, not consumed by centella. Set to `70` to backstop worker auto-compaction. |

### Precedence

- **Source-of-truth** (highest first): `--source-of-truth` →
  `CENTELLA_SOURCE_OF_TRUTH` → `centella.toml` → default `both`.
- **Model** (per worker, highest first): `--model-<worker>` →
  `--model` → `CENTELLA_MODEL_<WORKER>` → `CENTELLA_MODEL` →
  `model_<worker>` in `centella.toml` → `model` in `centella.toml` →
  per-worker default (`implementer` → `sonnet`; everything else →
  `opus`). The judgment-vs-implementation split keeps the
  most-frequently-invoked worker on the lower-cost model while
  every judgment step gets Opus-grade reasoning. To restore the
  pre-0.3 all-sonnet behavior in one knob, set `CENTELLA_MODEL=sonnet`
  or pass `--model sonnet`.
- **Confidence rounds** (highest first): `--confidence-rounds` →
  `CENTELLA_CONFIDENCE_ROUNDS` → `confidence_rounds` in
  `centella.toml` → default `8`.
- **Verbosity** (highest first): `--verbosity` → `-v`/`-vv`/`-q`/`-qq`
  shortcuts (anchored to `normal`, not to the resolved default) →
  `CENTELLA_VERBOSITY` → `verbosity` in `centella.toml` → default
  `stream`.

See [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §2 for the
rationale behind these orders and the full validation contract.

## Worker types

Centella spawns six kinds of `claude -p` worker. Each is a separate
subprocess; there is no in-session agent nesting.

| Worker | Prompt source | Default model | Runs per task | Returns |
|--------|---------------|---------------|---------------|---------|
| `classifier` | `prompts/classifier.md` | opus | 1 | category set + intent questions |
| `planner` | `prompts/planner.md` | opus | one per category (parallel) | subtask list with deps |
| `reconciler` | `prompts/reconciler.md` | opus | 0 or 1 (spawned only when planners' capability tags don't align) | renames / added_provides / added_subtasks / unresolvable |
| `implementer` | `prompts/implementer.md` | sonnet | one per subtask (per wave, parallel) | commits on a `centella/subtasks/<run-id>/<subtask-id>` branch |
| `integrator` | `prompts/integrator.md` | opus | on conflict during wave integration | resolved merge commit on `centella/runs/<run-id>` |
| `validator` | constant `VALIDATOR_SYSTEM` in `centella.py` (not a file) | opus | once per wave | pass/fail on the run branch |

**Per-worker model defaults:** judgment workers (classifier, planner,
reconciler, integrator, validator) default to Opus; only the implementer
defaults to Sonnet — its job is concrete subtask execution where
throughput matters more than broad-context judgment. To revert to the
all-Sonnet pattern of earlier versions, set `CENTELLA_MODEL=sonnet` or
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
| `orchestrator/centella.py` | The orchestrator — all phases, waves, caps, retries |
| `prompts/classifier.md` | System prompt: classify task + surface intent questions |
| `prompts/planner.md` | System prompt: decompose one category into a subtask plan |
| `prompts/reconciler.md` | System prompt: reconcile cross-domain capability-tag drift between planner outputs |
| `prompts/implementer.md` | System prompt: execute one subtask end to end |
| `prompts/integrator.md` | System prompt: resolve merge conflicts behaviorally |
| `scripts/setup-run.sh` | Create per-run branch + worktree (`centella/runs/<run-id>`) |
| `scripts/new-worktree.sh` | Create per-subtask branch + worktree off the run branch |
| `scripts/integrate.sh` | Merge a subtask branch into the run branch |
| `scripts/finalize.sh` | Verify the run branch is non-empty and ready to push (the working branch is not modified locally — the push + PR step lives in Python's `push_and_open_pr`, called from `phase_finalize` unless `--no-push`) |
| `scripts/cleanup.sh` | Remove worktrees for one run (default `--run-id`) or all runs (`--all-runs`). State dir always preserved as audit. `--branches` also deletes the matching `centella/runs/<id>` run branch *and* `centella/subtasks/<id>/*` subtask branches. `--subtask-branches` deletes only the subtask branches and keeps `centella/runs/<id>` (the post-finalize default — the run branch is the PR head). `--bootstrap` removes orphaned `_bootstrap-*` dirs (runs that died before classify completed). `--legacy` removes the pre-per-run layout. |
| `centella` | Executable entry-point wrapper |
| `commands/centella.md` | Thin plugin skill — reachable as `/centella` from Claude Code |
| `docs/DESIGN.md` | Full design document and rationale |
| `docs/IMPLEMENTATION.md` | Current code-surface spec (functions, caps, schemas) |
| `docs/USAGE.md` | End-to-end walkthrough of one Centella run |
| `CONTRIBUTING.md` | Development setup, task-completion checklist, PR conventions |

## Safety

Acting workers use `--dangerously-skip-permissions`. That is a real risk
surface — it is what makes the run unattended. It is bounded by worktree
isolation (each worker operates in its own isolated checkout, not your main
working tree) but not eliminated. **Run on repositories you trust, ideally in
a container, and review the run branch (`centella/runs/<run-id>`) before relying
on the result.** Push + PR at finalize is the natural review surface; you
can also pass `--no-push` to keep finalize fully local.

The run writes only to `.centella/runs/<run-id>/` (auto-excluded from git
via `.git/info/exclude`) and to `centella/runs/<run-id>` plus
`centella/subtasks/<run-id>/<subtask-id>` branches. Phase 6 (unless
`--no-push`) pushes the run branch to `origin` and opens a PR against
your working branch — your working branch itself is never modified
locally. After a run, the run branch (`centella/runs/<run-id>`) is kept
as an audit trail; per-subtask branches are auto-deleted at finalize,
but each worker's commits remain reachable from the run branch's
`--no-ff` merge graph (`git log centella/runs/<run-id> --graph`). Remove
the run branch (and any leftover subtask branches) with
`scripts/cleanup.sh --run-id <id> --branches` (or `--all-runs --branches`
for an audit cleanup across every past run).

## Troubleshooting

- **`claude: command not found`** — Centella shells out to the Claude Code
  CLI; install it from https://claude.ai/code and confirm with
  `claude --version`. There is no fallback path.

- **Exits with code 10** — not an error. Centella needs clarification
  answers and you are running non-interactively. Read
  `.centella/pending-questions.json`, write the answers to
  `.centella/answers.json`, then `./centella --resume --answers .centella/answers.json`.
  The plugin skill at `commands/centella.md` handles this relay
  automatically when invoked as `/centella`.

- **Run interrupted (Ctrl-C)** — Ctrl-C is treated as the user's explicit
  "throw this away" gesture. The run's worktrees, branches, and state dir
  are all removed (`centella/runs/<run-id>` and
  `centella/subtasks/<run-id>/*` branches included). The run is
  *not* resumable. If you want to abort temporarily and resume later, use
  `kill <pid>` (SIGTERM) instead — see the next entry.

- **Run terminated by signal (SIGTERM, SIGHUP, CI cancel, terminal close, reboot)** —
  worktrees are torn down but state.json + run branch are preserved.
  Resume with `./centella --resume` (auto-picks if exactly one run) or
  `./centella --resume --run-id <id>`. Run `centella --list` to see what's
  in flight.

- **A subtask reports `blocked`** — the implementer hit something it
  cannot resolve and bailed before integration. Read the blocker reason in
  `.centella/state.json` under `blocked[<subtask-id>]`, address the
  upstream cause, then resume. See [`docs/DESIGN.md`](docs/DESIGN.md) §8
  for the evidence-gated loop.

- **Worktree or branch conflicts on a re-run** — `scripts/cleanup.sh --run-id <id> --branches`
  removes that run's worktrees and deletes its branches so a fresh run
  with the same task starts clean. For a global sweep across every past
  run, use `--all-runs --branches`. Then re-invoke as normal.

- **Push or PR failed at finalize** — the run completed locally. Check
  `centella --list` for the run's status (`push-failed` / `pr-failed`)
  and read `.centella/runs/<run-id>/run.json` for the captured stderr.
  The error message at finalize names the exact retry command. Local
  commits are intact on the run branch.

## FAQ

**Do I need an Anthropic API key?**
No. Centella runs entirely on the Claude Code CLI and your existing
subscription. The orchestrator shells out to `claude -p` workers; no API
key is read or sent.

**Can I run multiple Centella instances in the same repository?**
Yes. Each invocation derives a unique `run_id` and namespaces all of its
state under `.centella/runs/<run-id>/` and its branches under
`centella/runs/<run-id>` (run branch) and `centella/subtasks/<run-id>/<sid>`
(subtask branches) — so parallel runs in the same clone never collide.
Use `--list` to see what's in flight and `--resume --run-id <id>` to
resume a specific one.

**Does Centella work outside a git repository?**
No. Per-subtask isolation is provided by `git worktree`; the worktree
mechanism is load-bearing, not optional.

**What if my project has no test runner?**
The validator falls back to a worker-driven correctness check. See
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §4 for `detect_test_runner()`
and what happens when nothing is detected.

**Can I see what each worker did?**
Yes. Every worker commits to its own `centella/subtasks/<run-id>/<subtask-id>`
branch during the run; at finalize, those branches are auto-deleted, but
the integrator merges each one into the run branch with `--no-ff`, so
every worker's commits remain reachable from `centella/runs/<run-id>` as a
named merge bubble. `git log centella/runs/<run-id> --graph` is your
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

v0.2.0 — see [`CHANGELOG.md`](CHANGELOG.md). The orchestrator's phase flow, wave scheduling, cross-domain dependency
resolution, and git worktree mechanics are all tested. First contact with a live
`claude -p` session is the remaining verification step. Limitations and planned
work are in [`docs/DESIGN.md`](docs/DESIGN.md).

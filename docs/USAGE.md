# A worked example: one Pila run, end to end

## What this document is

A walkthrough. It follows a single Pila run from invocation to merge so
you know what to expect on stdout and on disk at each phase. It is *not* a
reference — for the architecture and the reasons it works that way, see
[`docs/DESIGN.md`](DESIGN.md); for the code surface (function names, cap
values, schemas), see [`docs/IMPLEMENTATION.md`](IMPLEMENTATION.md). This
document never restates either.

## Prerequisites recap

You need the `claude` CLI on `PATH` (logged in), `git`, and a git repo
with `user.email` and `user.name` set and a clean working tree. Pila
runs inside a container, so you also need a container runtime: Colima
on macOS (`brew install colima && colima start --runtime containerd
--mount-type virtiofs --cpu N --memory M` where N/M are half your host
CPU/RAM — see [`docs/INSTALL.md`](INSTALL.md) for the bounds the
installer uses automatically; also add the swap-provision YAML block
documented under "Memory pressure: swap configuration"), or
`containerd + nerdctl` natively on Linux.
You do *not* need Python on the host — the image provisions it. For
the full per-OS setup walkthrough see
[`docs/INSTALL.md`](INSTALL.md); for the one-command pila install
(Claude Code marketplace or `curl | bash`) see
[README "Install"](../README.md#install), and
[README "Requirements"](../README.md#requirements) for the full list.

## The example task

We will walk through one run on this concrete task:

> *"Add a `--dry-run` flag to the existing CLI tool that prints the plan
> without executing it, plus a regression test."*

It is a good demonstrator because it touches code (the CLI), touches tests
(a regression), has an obvious dependency (test imports the flag), and fits
in one or two waves. It exercises the orchestrator's classify → plan →
schedule → execute → finalize loop without being so large that the output
becomes noise.

## Step 1 — Invocation

From the root of the target repository:

```bash
export PILA_SOURCE_OF_TRUTH=codebase
pila "Add a --dry-run flag to the CLI that prints the plan without executing it, plus a regression test"

# Equivalent one-off invocation without the env var:
pila --source-of-truth codebase "Add a --dry-run flag …"

# Same idea for the model — judgment workers default to `opus` and the
# acting workers (implementer, conformer) default to `sonnet`; `--model
# <alias>` sets every worker. Per-worker overrides exist
# (e.g. --model-implementer opus).
pila --model opus "Add a --dry-run flag …"
```

Setting `PILA_SOURCE_OF_TRUTH=codebase` up front pins the
source-of-truth preference for this run — useful when the default
(`both`) is not what you want.

Within the first few seconds you will see preflight output on stdout — git
identity check, working-tree-clean check, a live `claude -p` smoke test —
and a fresh `.pila/` directory appears in the repo root. It is
git-excluded automatically (via `.git/info/exclude`, not your tracked
`.gitignore`).

## Step 2 — Classification and clarification

The classifier returns a category set; for our example you should expect
something like `["feat", "test"]` — a feature and a regression test. Along
with the categories the classifier surfaces *intent* questions — things it
genuinely cannot derive from the task or the codebase.

A realistic question for our task: *"Should `--dry-run` exit zero after
printing, or should it also validate the plan and exit non-zero if the
plan would have failed?"* That decision is not in the codebase; the
classifier asks.

In an interactive terminal Pila prompts you; you type answers, the run
continues. In a non-interactive context (CI, a plugin skill) Pila
instead writes `.pila/pending-questions.json` and exits with code 10 —
not an error, a structured "need answers" signal. The plugin skill at
[`commands/pila.md`](../commands/pila.md) shows the questions to
the user, writes their answers to `.pila/answers.json`, and resumes
with `--resume --answers .pila/answers.json`.

## Step 3 — Planning and scheduling

One planner subprocess runs per category, in parallel. Each returns a list
of subtasks with id, domain prefix (`feat-`, `test-`, etc.), description,
and dependencies on other subtasks by id.

For our task expect roughly:

```
feat-add-dry-run-flag       (depends on: none)
test-dry-run-regression     (depends on: feat-add-dry-run-flag)
```

The scheduler merges plans across categories, builds a global dependency
DAG, topologically sorts it into waves, and persists the result. Our two
subtasks become two waves of one subtask each — the test cannot run until
the flag exists. The full rationale for the wave model is in
[`DESIGN.md`](DESIGN.md) §5.

The merged plan lives at `.pila/plan.json`; per-subtask spec files
appear at `.pila/subtasks/<id>.json`.

## Step 4 — Wave execution

For each wave Pila creates a per-subtask git worktree off the run
branch (`pila/runs/<run-id>`), then spawns an implementer worker in
each worktree. Workers run concurrently, capped by `--max-parallel`
(default 2).

On stdout you'll see lines like (with a hypothetical `<run-id>` of
`feat-add-dry-run-flag-a3f7c2`):

```
[wave 1] implementer feat-add-dry-run-flag: start
[wave 1] implementer feat-add-dry-run-flag: ok (3 turns, 12.4s)
[wave 1] integrating feat-add-dry-run-flag into pila/runs/feat-add-dry-run-flag-a3f7c2
[wave 1] validating pila/runs/feat-add-dry-run-flag-a3f7c2
```

And `git worktree list` will show entries like:

```
/your/repo                                                                       abc1234 [main]
/your/repo/.pila/runs/feat-add-dry-run-flag-a3f7c2/worktrees/staging         def5678 [pila/runs/feat-add-dry-run-flag-a3f7c2]
/your/repo/.pila/runs/feat-add-dry-run-flag-a3f7c2/worktrees/feat-add-dry-run-flag  ghi9012 [pila/subtasks/feat-add-dry-run-flag-a3f7c2/feat-add-dry-run-flag]
```

After every implementer commits in its worktree, the integrator merges
its branch into the run branch, and the post-work conformance phase
runs your project's detected build/lint/test commands as advisory
checks against the worktree — surfacing residuals as warnings on the
subtask result, not gating the wave. The wave boundary itself only
runs a deterministic conflict-marker scan; whether the work landed is
the implementer's confidence-gate call (DESIGN §8). Acting workers use
`--dangerously-skip-permissions` by design — bounded by worktree
isolation. See [README "Safety"](../README.md#safety) and
[`DESIGN.md`](DESIGN.md) §6, §9.

## Step 5 — Reviewing the run branch

Before phase 6 opens a PR proposing to merge into your working branch,
**review the run branch yourself**. This is what the
run-branch-as-integration-buffer (DESIGN §6) buys you:

```bash
git log pila/runs/<run-id> --oneline
git diff main..pila/runs/<run-id>
```

You will see one commit per subtask (one per worker), with subtask id in
the subject line. If the diff looks wrong — too broad, missed an edge
case, conflicting with something you wanted preserved — this is where you
intervene. Either re-run Pila with a refined task, hand-edit the run
branch, or abandon and `./scripts/cleanup.sh --run-id <run-id> --branches`.

## Step 6 — Finalization

Phase 6 verifies `pila/runs/<run-id>` is non-empty, pushes it to
`origin`, and opens a PR via `gh pr create --base <working-branch>
--head pila/runs/<run-id>`. Your working branch (the branch you
were on when you invoked Pila, recorded in
`.pila/runs/<run-id>/working-branch`) is **not** modified locally —
review and merge the PR on GitHub when you're satisfied. The run branch
`pila/runs/<run-id>` remains in your repo as the PR head until you
merge the PR. The per-subtask branches `pila/subtasks/<run-id>/*`
are **deleted automatically** at finalize — they were the mechanism for
parallel implementer isolation and carry no information that isn't
already in the run branch's merge graph. Each worker's full commit
history is still reachable from the run branch (the integrator merges
each subtask with `--no-ff`, so every worker's commits appear as a
named merge bubble in `git log pila/runs/<run-id> --graph`).

When you no longer need the run branch either (e.g., after the PR is
merged on GitHub):

```bash
./scripts/cleanup.sh --run-id <run-id> --branches
```

deletes the run branch and any remaining subtask branches. The per-run
state directory `.pila/runs/<run-id>/` is kept as a smaller audit
trail; `rm -rf` it manually when you no longer need that either. For an
audit cleanup across every past run, use `--all-runs --branches`.

## What happens when something goes wrong

**A subtask reports `blocked`.** The implementer hit something it cannot
resolve (an external dependency, an ambiguous spec, a failing test it
cannot fix). The wave aborts *before* integration, the blocker reason
lands in `state['blocked'][<subtask-id>]` and `subtask_status[<id>] =
"blocked"` inside `.pila/runs/<run-id>/state.json`, and Pila
exits non-zero. You read the blocker, fix the upstream issue (often by
editing the task and re-running, sometimes by hand-resolving), then
`./pila --resume`. See [`DESIGN.md`](DESIGN.md) §8 for the
evidence-gated loop logic that produces this signal.

**Integration fails.** The integrator can't merge a subtask branch into
the run branch — usually a conflict it cannot resolve behaviorally.
Pila records the failure in `state['integrator_failure']` (inside
`.pila/runs/<run-id>/state.json`) and exits. Pull up the conflicting
branches yourself, resolve, and resume.

**The run is interrupted.** Ctrl-C, system reboot, budget-cap hit. Run
`./pila --resume` from the same directory. The resume cursor is
`state['completed_waves']`; finished waves are not re-run. The full state
schema is documented in [`IMPLEMENTATION.md`](IMPLEMENTATION.md) §8.

## Tuning for your workflow

- `--source-of-truth codebase|research|both` — one-off CLI override;
  beats env and `pila.toml`. Unset → default `both`.
- `PILA_SOURCE_OF_TRUTH=codebase|research|both` — sticky preference.
- `pila.toml` at the repo root with `source_of_truth = codebase` —
  committed per-repo default; outranked by env and CLI.
- `--model sonnet|opus|haiku` — model for every worker this run.
  Without any override the per-worker defaults apply: judgment workers
  (classifier, planner, reconciler, provision, integrator) run on `opus`; the
  acting workers (implementer, conformer) run on `sonnet`. Per-worker
  `--model-classifier`, `--model-planner`, `--model-reconciler`,
  `--model-provision`, `--model-implementer`, `--model-integrator`,
  `--model-conformer` flags override the global default. Env-var equivalents are
  `PILA_MODEL` (and `PILA_MODEL_<WORKER>` for the per-worker
  overrides); TOML keys are `model` / `model_<worker>` in
  `pila.toml`. Full precedence table in
  [`IMPLEMENTATION.md`](IMPLEMENTATION.md#model-selection). To restore
  the pre-0.3 all-sonnet behavior in one knob, set `--model sonnet` or
  `PILA_MODEL=sonnet`.
- `--max-workers N` — cap total `claude -p` subprocess count over the
  run. Default: `60` (`DEFAULT_CAPS["max_total_workers"]`). Also
  `PILA_MAX_WORKERS` env var or `max_workers` in `pila.toml`
  (same precedence as `--confidence-rounds`: CLI > env > TOML > default).
  Note that the post-work conformance phase (DESIGN §9) spawns up to
  `conformance_rounds` additional workers per *successful* subtask (default
  2), roughly doubling per-subtask worker usage. For large runs you may
  want to raise this proportionally — a cap-hit during the conformance
  phase surfaces as an advisory `conformance_warnings` entry, never as
  a subtask failure, but earlier subtasks would have hit it first and
  aborted the run.
- `--max-parallel N` — cap concurrent implementers per wave. Default:
  `2` (`DEFAULT_CAPS["max_parallel"]`). Lowered from 4 in May 2026
  because subprocess fan-out inside each worker (vitest pools, webpack
  workers, etc.) is unbounded; the only orchestrator-side knob that
  keeps total in-flight toolchain memory in check is the worker count.
  Raise this on machines with more RAM (16 GiB+ recommended for `N=4`).
- `--clarify` — opt into surfacing intent questions to the user
  (default: off). Without it the classifier's filter still runs but
  surviving questions are dropped, and the implementer makes a
  documented best-effort decision. Also `PILA_CLARIFY` env var
  and `clarify = true` in `pila.toml`.
- `--runtime local|fly` — execution backend for per-subtask worker
  containers. Default: `local` (nerdctl on the local container
  runtime). `fly` routes each worker through Fly.io Machines instead
  — requires `flyctl` logged in (`fly auth status`) and a published
  pila image (see `scripts/publish-image.sh`). Also `PILA_RUNTIME`
  env var or `runtime = fly` in `pila.toml` (committed per-repo
  default; outranked by env and CLI). Precedence: `--runtime` →
  `PILA_RUNTIME` → `pila.toml` → default `local`.
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70` — lower auto-compaction threshold
  for worker processes.

The full inventory of CLI flags and environment variables is in the
[README "Install and run" section](../README.md#install-and-run).

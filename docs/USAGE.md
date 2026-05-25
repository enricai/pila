# A worked example: one Centella run, end to end

## What this document is

A walkthrough. It follows a single Centella run from invocation to merge so
you know what to expect on stdout and on disk at each phase. It is *not* a
reference — for the architecture and the reasons it works that way, see
[`docs/DESIGN.md`](DESIGN.md); for the code surface (function names, cap
values, schemas), see [`docs/IMPLEMENTATION.md`](IMPLEMENTATION.md). This
document never restates either.

## Prerequisites recap

You need the `claude` CLI on `PATH` (logged in), Python 3.10+, and a git
repo with `user.email` and `user.name` set and a clean working tree. See
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
export CENTELLA_SOURCE_OF_TRUTH=codebase
/path/to/centella/centella "Add a --dry-run flag to the CLI that prints the plan without executing it, plus a regression test"
```

Setting `CENTELLA_SOURCE_OF_TRUTH=codebase` up front tells the classifier
not to ask whether to trust the codebase or external research — useful for
a tight walkthrough where you don't want to be interrupted.

Within the first few seconds you will see preflight output on stdout — git
identity check, working-tree-clean check, a live `claude -p` smoke test —
and a fresh `.centella/` directory appears in the repo root. It is
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

In an interactive terminal Centella prompts you; you type answers, the run
continues. In a non-interactive context (CI, a plugin skill) Centella
instead writes `.centella/pending-questions.json` and exits with code 10 —
not an error, a structured "need answers" signal. The plugin skill at
[`commands/centella.md`](../commands/centella.md) shows the questions to
the user, writes their answers to `.centella/answers.json`, and resumes
with `--resume --answers .centella/answers.json`.

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

The merged plan lives at `.centella/plan.json`; per-subtask spec files
appear at `.centella/subtasks/<id>.json`.

## Step 4 — Wave execution

For each wave Centella creates a per-subtask git worktree off the
`centella/staging` branch, then spawns an implementer worker in each
worktree. Workers run concurrently, capped by `--max-parallel` (default
4).

On stdout you'll see lines like:

```
[wave 1] implementer feat-add-dry-run-flag: start
[wave 1] implementer feat-add-dry-run-flag: ok (3 turns, 12.4s)
[wave 1] integrating feat-add-dry-run-flag into centella/staging
[wave 1] validating centella/staging
```

And `git worktree list` will show entries like:

```
/your/repo                         abc1234 [main]
/your/repo/.centella/worktrees/staging   def5678 [centella/staging]
/your/repo/.centella/worktrees/feat-add-dry-run-flag  ghi9012 [centella/feat-add-dry-run-flag]
```

After every implementer commits in its worktree, the integrator merges its
branch into `centella/staging`, and the validator runs your project's
detected test runner against staging to confirm nothing regressed. Acting
workers use `--dangerously-skip-permissions` by design — bounded by
worktree isolation. See [README "Safety"](../README.md#safety) and
[`DESIGN.md`](DESIGN.md) §6.

## Step 5 — Reviewing staging

Before phase 6 merges anything into your working branch, **review staging
yourself**. This is what staging-as-integration-buffer (DESIGN §6) buys
you:

```bash
git log centella/staging --oneline
git diff main..centella/staging
```

You will see one commit per subtask (one per worker), with subtask id in
the subject line. If the diff looks wrong — too broad, missed an edge
case, conflicting with something you wanted preserved — this is where you
intervene. Either re-run Centella with a refined task, or hand-edit
staging, or abandon and `scripts/cleanup.sh --branches`.

## Step 6 — Finalization

Phase 6 merges `centella/staging` into your working branch (the branch you
were on when you invoked Centella, recorded in `.centella/working-branch`)
and runs post-merge sanity checks. The `centella/*` worker branches and
the `centella/staging` branch remain in your repo as an audit trail. Each
worker's full commit history is preserved on `centella/<subtask-id>`.

When you no longer need the audit trail:

```bash
scripts/cleanup.sh --branches
```

removes the worktrees and deletes the `centella/*` branches.

## What happens when something goes wrong

**A subtask reports `blocked`.** The implementer hit something it cannot
resolve (an external dependency, an ambiguous spec, a failing test it
cannot fix). The wave aborts *before* integration, the blocker reason
lands in `state['blocked'][<subtask-id>]` and `subtask_status[<id>] =
"blocked"`, and Centella exits non-zero. You read the blocker, fix the
upstream issue (often by editing the task and re-running, sometimes by
hand-resolving), then `./centella --resume`. See
[`DESIGN.md`](DESIGN.md) §8 for the evidence-gated loop logic that
produces this signal.

**Integration fails.** The integrator can't merge a subtask branch into
staging — usually a conflict it cannot resolve behaviorally. Centella
records the failure in `state['integrator_failure']` and exits. Pull up
the conflicting branches yourself, resolve, and resume.

**The run is interrupted.** Ctrl-C, system reboot, budget-cap hit. Run
`./centella --resume` from the same directory. The resume cursor is
`state['completed_waves']`; finished waves are not re-run. The full state
schema is documented in [`IMPLEMENTATION.md`](IMPLEMENTATION.md) §8.

## Tuning for your workflow

- `CENTELLA_SOURCE_OF_TRUTH=codebase|research|both|ask` — global preference,
  skips the source-of-truth clarification question.
- `centella.toml` at the repo root with `source_of_truth = codebase` —
  per-repo override; wins over the env var.
- `--max-workers N` — cap total `claude -p` subprocess count over the run.
- `--max-parallel N` — cap concurrent implementers per wave.
- `--no-clarify` — skip clarification entirely; intent questions are
  dropped and source-of-truth defaults to `codebase`.
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70` — lower auto-compaction threshold
  for worker processes.

The full inventory of CLI flags and environment variables is in the
[README "Install and run" section](../README.md#install-and-run).

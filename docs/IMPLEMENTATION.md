# Centella — Implementation Reference

> **This document describes the current code, not the design.** It is true only
> against the present state of `orchestrator/centella.py`, the worker prompts,
> and the shell scripts. A change to the code that is not reflected here makes
> *this document* wrong — unlike `DESIGN.md`, which describes the architecture
> and stays correct across reimplementation. When this document and the code
> disagree, the code is authoritative. When this document and `DESIGN.md`
> disagree, `DESIGN.md` defines what *should* be true.
>
> Read `DESIGN.md` first for the *why*; this document is the *what* and *where*.

---

## 1. Repository layout

```
centella/
├── .claude-plugin/plugin.json     plugin manifest
├── centella                        executable entry-point wrapper (chmod +x)
├── orchestrator/centella.py        the orchestrator — all control flow (chmod +x)
├── prompts/
│   ├── classifier.md              Phase 1 worker system prompt
│   ├── planner.md                 Phase 2 worker system prompt
│   ├── reconciler.md              Phase 2½ worker — resolve cross-domain
│   │                              capability-tag drift between planners
│   ├── implementer.md             Phase 5 implementer worker system prompt
│   ├── integrator.md              conflict-resolution worker system prompt
│   └── judge.md                  LLM judge worker — 3-dimensional rubric for
│                                  reviewing captured call records
│   (the validator's system prompt is the `VALIDATOR_SYSTEM` constant in
│    `orchestrator/centella.py`, not a file — it is short and has no
│    behavioral tuning that would benefit from out-of-tree editing)
├── scripts/
│   ├── setup-run.sh               create per-run branch + worktree (idempotent)
│   ├── new-worktree.sh            create/reuse a per-subtask worktree (per-run scoped)
│   ├── integrate.sh               merge a subtask branch into the per-run branch
│   ├── finalize.sh                merge run branch into working branch; push; open PR
│   └── cleanup.sh                 remove worktrees / branches (default: scoped to one run)
├── commands/centella.md            thin plugin skill — launches the orchestrator
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
/path/to/centella/centella "Fix the login timeout bug and add a regression test"

# Resume an interrupted run. Auto-picks if exactly one in-flight run exists;
# requires --run-id otherwise (see `centella --list` to enumerate).
/path/to/centella/centella --resume
/path/to/centella/centella --resume --run-id fix-login-timeout-bug-b81e90

# List in-flight and completed runs in this repository:
/path/to/centella/centella --list

# Skip the default push + PR at finalize (run completes with the local merge
# into the working branch only):
/path/to/centella/centella "task" --no-push
export CENTELLA_NO_PUSH=1

# Skip pre-push hooks at finalize (the user's explicit override; defaults off).
# Affects only the final `git push`; worker `git commit` operations inside
# worktrees continue to run all hooks normally.
/path/to/centella/centella "task" --no-verify

# Skip clarification entirely (DESIGN §11). Intent questions from the
# classifier are dropped; the source-of-truth question is satisfied from
# CENTELLA_SOURCE_OF_TRUTH / centella.toml if set, and otherwise defaults
# to `codebase` with a logged warning.
/path/to/centella/centella "task" --no-clarify

# Pre-supply clarification answers:
/path/to/centella/centella "task" --answers answers.json

# Override caps:
/path/to/centella/centella "task" --max-workers 60 --max-parallel 6

# Dial how persistent workers are at building confidence before they exit
# blocked (default: 8 rounds inside each planner / implementer):
/path/to/centella/centella "task" --confidence-rounds 12
export CENTELLA_CONFIDENCE_ROUNDS=12

# Verbosity controls how much per-worker activity surfaces inline.
# Default is `stream`: one-line summary per worker event. -q drops to
# centella's pre-streaming terse output; -qq is fully quiet (errors
# still emit). -vv adds raw payloads. Per-worker .centella/logs/<sid>.log
# files are always written regardless of level.
/path/to/centella/centella "task"        # default: stream
/path/to/centella/centella "task" -q      # normal (pre-streaming)
/path/to/centella/centella "task" -qq     # quiet (errors only)
/path/to/centella/centella "task" -vv     # debug
/path/to/centella/centella "task" --verbosity normal
export CENTELLA_VERBOSITY=stream

# Set the source-of-truth preference globally so centella does not ask
# (or pass --source-of-truth on the command line for a one-off override):
export CENTELLA_SOURCE_OF_TRUTH=codebase    # or: research, both, ask
/path/to/centella/centella "task" --source-of-truth codebase

# Choose the model. Without overrides: judgment workers (classifier,
# planner, reconciler, integrator, validator) default to opus;
# implementer defaults to sonnet. Use the env var for a sticky
# preference, the CLI flag for a one-off, or centella.toml for the
# committed repo default. Per-worker overrides also exist — see §2.
export CENTELLA_MODEL=sonnet                # or: opus, haiku
/path/to/centella/centella "task" --model opus
/path/to/centella/centella "task" --model-implementer opus --model-classifier haiku

# Telemetry: on by default; disable with --no-telemetry or env var:
/path/to/centella/centella "task" --no-telemetry
export CENTELLA_TELEMETRY=0
# Override output subdirectory (default: <run-dir>/events/):
/path/to/centella/centella "task" --telemetry-dir my-events
export CENTELLA_TELEMETRY_DIR=my-events
# Override judge/heal output subdirectories:
/path/to/centella/centella "task" --judge-dir my-judge --heal-dir my-heal
export CENTELLA_JUDGE_DIR=my-judge
export CENTELLA_HEAL_DIR=my-heal

# Judge and heal model overrides (default: sonnet for throughput):
/path/to/centella/centella "task" --judge-model opus --heal-model opus
export CENTELLA_MODEL_JUDGE=sonnet
export CENTELLA_MODEL_HEAL=sonnet

# Heal-loop convergence knobs (defaults shown):
/path/to/centella/centella "task" --heal-max-rounds 10 --heal-success-threshold 0.9
export CENTELLA_HEAL_MAX_ROUNDS=10
export CENTELLA_HEAL_SUCCESS_THRESHOLD=0.9

# Run post-run skill phases against an existing run's captured LLM calls.
# --phase judge: score every call in calls.ndjson with the 3-dim judge rubric
#   and write verdict files to <run-dir>/<judge-dir>/.
# --phase heal: read the judge index for failing call_types and run the
#   self-heal loop for each; if no judge index exists yet, runs judge first.
# Use --run-id to select a run when multiple exist; auto-picks when only one.
/path/to/centella/centella --phase judge --run-id fix-login-timeout-bug-b81e90
/path/to/centella/centella --phase heal  --run-id fix-login-timeout-bug-b81e90
# Combine with heal-loop knobs:
/path/to/centella/centella --phase heal --heal-max-rounds 5 --heal-success-threshold 0.8

# Recommended backstop for worker auto-compaction
# (Claude Code CLI variable — not consumed by centella itself):
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70
```

Requirements: the `claude` CLI on `PATH` and logged in interactively (no API
key — subscription auth); Python 3.10+; a git repository with `user.email` and
`user.name` configured.

Via the plugin skill, from inside Claude Code:

```
claude --plugin-dir /path/to/centella
/centella <task>
```

### Source-of-truth preference

For feature work, centella needs to know whether to draw conventions from the
codebase, from online research, from both (codebase first; research as
fallback), or to ask the user. Resolution order (highest priority first):

1. **`--source-of-truth`** CLI flag, values `codebase` | `research` | `both` |
   `ask`. Argparse rejects anything else before the orchestrator runs.

2. **`CENTELLA_SOURCE_OF_TRUTH`** environment variable, same value set.

3. **`centella.toml` at the repo root** (committed, so the preference travels
   with the repo). Plain `key=value` syntax:

   ```
   source_of_truth = codebase
   ```

4. **Default `ask`.** When unset, centella surfaces the question on a feature
   task and prints a hint that setting the env var or the per-repo file
   will skip it next time.

An invalid value in env or file is rejected at startup via `die()` — bad
config is caught before any worker spawns.

> The CLI/env > file order reflects that the CLI flag and env var are
> session-scoped knobs (a user reaching for them is making a one-off
> override), while `centella.toml` is the committed default for the repo.

### Confidence rounds

Planners and implementers self-gate on confidence (DESIGN §8) and loop their
evidence-gate up to `confidence_rounds` times before they exit `blocked`.
Default 8. Increase if the user wants workers to push harder on hard
diagnoses; decrease for cheaper, faster runs that accept earlier
escalations.

Resolution order (highest priority first):

1. **`--confidence-rounds N`** CLI flag. Argparse rejects non-positive
   integers.
2. **`CENTELLA_CONFIDENCE_ROUNDS`** environment variable, same value set.
3. **`centella.toml` at the repo root**, `confidence_rounds = N`.
4. **Default `8`** (`DEFAULT_CAPS["confidence_rounds"]`).

An invalid value in env or file is rejected at startup via `die()`. The
resolved value is written into `caps["confidence_rounds"]` and passed in
each planner / implementer's user prompt — the cap is prompt-governed (see
§6 "Worker-internal caps" and DESIGN §13), the user-visible knob is real.

### Verbosity

Controls how much of the per-worker activity surfaces to the
orchestrator log. Per-worker `.centella/logs/<sid>.log` files are
always written with the full raw event stream — verbosity governs
only the *inline* summary lines. Four named levels with stackable
`-v`/`-q` shortcuts, following the clig.dev / cargo / kubectl
convention.

| Level    | Flag             | What you see inline |
| -------- | ---------------- | ------------------- |
| `quiet`  | `-qq` / `--verbosity quiet` | Phase boundaries, final result, errors only |
| `normal` | `-q` | Phase boundaries + per-subtask status changes (centella's pre-streaming behavior) |
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
3. **`CENTELLA_VERBOSITY`** environment variable.
4. **`centella.toml`**, `verbosity = "stream"`.
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
reconciler) may read. Forwarded to each `claude -p` invocation as
one `--add-dir` flag per entry. Use this when a task references a
sibling repo outside the current repo cwd — for example, "compare
how beacon and centella handle X, beacon is at `~/src/enric/beacon`":
without `--inspect-dir ~/src/enric/beacon`, the classifier and
planner cannot `Read`/`Grep`/`Glob` that path, and an attempt to
fall back to `ls`/`find` is blocked by the workspace sandbox even
though `INSPECT_TOOLS` allowlists those verbs.

Resolution order (highest priority first):

1. **`--inspect-dir PATH`** CLI flag, repeatable.
2. **`CENTELLA_INSPECT_DIRS`** environment variable, colon-separated.
3. **`centella.toml`**, `inspect_dirs = "/abs/path/a,/abs/path/b"`
   (a comma-separated string, parsed by `_read_toml_key`).
4. **Default** `[]` (no extra directories).

Paths are expanded (`~` → `$HOME`) and resolved to absolute form at
startup. Duplicates are removed. The resolved list lives on
`st.data["inspect_dirs"]` and is re-resolved fresh on every run,
including `--resume`, so the user can add or remove paths without
editing state.

This applies only to inspect-bucket workers. Acting workers
(implementer, integrator) run inside the wave's worktree; the
validator runs inside the integrated wave worktree. Those workers
have `--dangerously-skip-permissions` and operate on the worktree
copy, not the user's wider filesystem — `--add-dir` is unneeded.

### Telemetry

Controls whether centella writes NDJSON telemetry events for LLM calls. Events
land in `<run-dir>/<telemetry_subdir>/` — already under `.centella/` and thus
covered by the existing `.gitignore` exclusion. Telemetry is on by default.

Resolution order (highest priority first):

1. **`--telemetry` / `--no-telemetry`** CLI flags (mutually exclusive).
2. **`CENTELLA_TELEMETRY`** environment variable, boolean spellings
   (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`).
3. **`centella.toml`**, `telemetry = true|false`.
4. **Default `True`** (`TELEMETRY_DEFAULT`).

An invalid boolean in env or file is rejected at startup via `die()`.

### Telemetry directory

The subdirectory name (relative to `<run-dir>`) where telemetry NDJSON event
files are written.

Resolution order (highest priority first):

1. **`--telemetry-dir DIR`** CLI flag.
2. **`CENTELLA_TELEMETRY_DIR`** environment variable.
3. **`centella.toml`**, `telemetry_dir = "events"`.
4. **Default `"events"`** (`TELEMETRY_SUBDIR_DEFAULT`).

### Judge output directory

The subdirectory name (relative to `<run-dir>`) where LLM judge output files
are written.

Resolution order (highest priority first):

1. **`--judge-dir DIR`** CLI flag.
2. **`CENTELLA_JUDGE_DIR`** environment variable.
3. **`centella.toml`**, `judge_dir = "judge-out"`.
4. **Default `"judge-out"`** (`JUDGE_DIR_DEFAULT`).

### Heal output directory

The subdirectory name (relative to `<run-dir>`) where LLM self-heal loop output
files are written.

Resolution order (highest priority first):

1. **`--heal-dir DIR`** CLI flag.
2. **`CENTELLA_HEAL_DIR`** environment variable.
3. **`centella.toml`**, `heal_dir = "heal-out"`.
4. **Default `"heal-out"`** (`HEAL_DIR_DEFAULT`).

### Judge model

The `claude` model alias used when the judge skill spawns a worker to score a
batch of captured calls. The judge does not require broad-context judgment like
the orchestrator's core workers — `sonnet` is the right default for throughput.

Resolution order (highest priority first):

1. **`--judge-model MODEL`** CLI flag.
2. **`CENTELLA_MODEL_JUDGE`** environment variable.
3. **`centella.toml`**, `model_judge = "sonnet"`.
4. **Default `"sonnet"`** (`MODEL_DEFAULT_PER_WORKER["judge"]`).

### Heal model

The `claude` model alias used when the self-heal skill spawns workers for patch
generation and patched-arm replay.

Resolution order (highest priority first):

1. **`--heal-model MODEL`** CLI flag.
2. **`CENTELLA_MODEL_HEAL`** environment variable.
3. **`centella.toml`**, `model_heal = "sonnet"`.
4. **Default `"sonnet"`** (`MODEL_DEFAULT_PER_WORKER["heal"]`).

### Heal-loop convergence parameters

Knobs governing the self-heal loop's iteration limit, pass-rate target, plateau
detection, and budget guard. All default values match Beacon's `DEFAULT_CONFIG`
(prior art at `scripts/heal-loop.ts:154`).

| Knob | CLI flag | Env var | TOML key | Default |
|------|----------|---------|----------|---------|
| Max iterations per call_type | `--heal-max-rounds N` | `CENTELLA_HEAL_MAX_ROUNDS` | `heal_max_rounds = 10` | `10` (`HEAL_MAX_ROUNDS_DEFAULT`) |
| Success pass-rate threshold | `--heal-success-threshold F` | `CENTELLA_HEAL_SUCCESS_THRESHOLD` | `heal_success_threshold = 0.9` | `0.9` (`HEAL_SUCCESS_THRESHOLD_DEFAULT`) |
| Plateau detection window | — | — | — | `3` (`HEAL_PLATEAU_WINDOW_DEFAULT`; not user-tunable) |
| Plateau minimum delta | — | — | — | `0.03` (`HEAL_PLATEAU_DELTA_DEFAULT`; not user-tunable) |
| Per-call_type replay count | — | — | — | `5` (`HEAL_N_REPLAYS_DEFAULT`; not user-tunable) |

The plateau window, plateau delta, and replay count are not currently exposed
as CLI/env/TOML knobs — they are implementation constants. Only the user-facing
knobs (`--heal-max-rounds`, `--heal-success-threshold`) are CLI/env/TOML
resolvable. Resolution for both follows the standard precedence: CLI flag →
env var → `centella.toml` → default.

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
| integrator   | opus    | behavioral conflict resolution; a wrong merge silently corrupts integrated state |
| validator    | opus    | per-criterion judgment in the LLM-fallback path; false-pass/false-fail is expensive |
| implementer  | sonnet  | concrete subtask execution; Sonnet's throughput is the right tradeoff |
| judge        | sonnet  | scoring a batch of captured calls; throughput matters more than broad judgment |
| heal (patch) | sonnet  | patch generation and replay; throughput matters more than broad judgment |

`MODEL_DEFAULT` is the global default (`opus`); `MODEL_DEFAULT_PER_WORKER`
overrides it for specific workers (`implementer`, `judge`, and `heal` all
default to `sonnet`).

Resolution order for each worker type `W` (highest priority first):

1. **`--model-<W>`** CLI flag (e.g. `--model-implementer opus`)
2. **`--model`** CLI flag (sets the global default for this run)
3. **`CENTELLA_MODEL_<W>`** env var (e.g. `CENTELLA_MODEL_IMPLEMENTER=opus`)
4. **`CENTELLA_MODEL`** env var (sets the global default)
5. **`model_<w>`** key in `centella.toml`
6. **`model`** key in `centella.toml`
7. **Per-worker default** from `MODEL_DEFAULT_PER_WORKER`
8. **Global default `MODEL_DEFAULT`** (`opus`)

Eight worker types, each independently overridable:

| Worker       | env var                       | CLI flag                | TOML key            |
|--------------|-------------------------------|-------------------------|---------------------|
| (global)     | `CENTELLA_MODEL`              | `--model`               | `model`             |
| classifier   | `CENTELLA_MODEL_CLASSIFIER`   | `--model-classifier`    | `model_classifier`  |
| planner      | `CENTELLA_MODEL_PLANNER`      | `--model-planner`       | `model_planner`     |
| reconciler   | `CENTELLA_MODEL_RECONCILER`   | `--model-reconciler`    | `model_reconciler`  |
| implementer  | `CENTELLA_MODEL_IMPLEMENTER`  | `--model-implementer`   | `model_implementer` |
| integrator   | `CENTELLA_MODEL_INTEGRATOR`   | `--model-integrator`    | `model_integrator`  |
| validator    | `CENTELLA_MODEL_VALIDATOR`    | `--model-validator`     | `model_validator`   |
| judge        | `CENTELLA_MODEL_JUDGE`        | `--judge-model`         | `model_judge`       |
| heal         | `CENTELLA_MODEL_HEAL`         | `--heal-model`          | `model_heal`        |

Note: `judge` and `heal` use dedicated CLI flags (`--judge-model`, `--heal-model`)
rather than the `--model-<W>` pattern used by orchestrator workers, because they
are post-run skill workers invoked outside the main orchestrate loop and do not
participate in the `--model` global-default resolution path.

An invalid value in env or file is rejected at startup via `die()`. CLI
values are validated by argparse `choices=` and rejected with the standard
argparse error.

**Cost note:** Opus is materially more expensive than Sonnet. A user who
wants the old all-Sonnet behavior sets `CENTELLA_MODEL=sonnet` (or
`--model sonnet`). Per-worker overrides (`--model-planner sonnet`) let
users selectively de-escalate individual workers.

Models are not persisted in `.centella/state.json`. On `--resume`, models are
re-resolved from the current environment, so changing `CENTELLA_MODEL` between
the original run and the resume is intentional and takes effect.

### The `--answers` file

A JSON object keyed by classifier-assigned question `id`, plus
`source_of_truth` set to `"codebase"`, `"research"`, or `"both"` when the
source-of-truth question was asked:

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
| `--output-format stream-json --verbose` | streams one JSON event per stdout line as the worker runs; the final `result` event is the envelope (same shape as `--output-format json`'s single output — `cost`, `usage`, `terminal_reason`, `structured_output`). `_invoke` writes raw events to `.centella/logs/<sid>.log` and emits per-event inline summaries gated by `state.json["verbosity"]` |
| `--json-schema <inline>` | the payload schema; serialized inline as a JSON string — a file path is silently ignored (verified against Claude Code 2.1.143) |
| `--append-system-prompt` | injects the worker's role prompt — read from `prompts/*.md` for classifier/planner/reconciler/implementer/integrator, or the `VALIDATOR_SYSTEM` constant in `centella.py` for the validator |
| `--allowedTools` | tool allowlist; three buckets — **inspect** (`INSPECT_TOOLS`: read set + allowlisted `Bash(ls:*)` / `Bash(find:*)` / `Bash(cat:*)` / … for cross-cwd read-only inspection, **no Write/Edit**) for classifier, planner, and reconciler; **acting** (`ACT_TOOLS`: read set + Bash/Write/Edit) for implementer and integrator; **run-and-read** (`RUN_TOOLS`: read set + Bash) for the validator. The acting and run-and-read buckets keep Bash unrestricted because their workers run with `--dangerously-skip-permissions`; the inspect bucket uses `Bash(<verb>:*)` prefix patterns to pre-approve specific read-only verbs at the CLI level — no Write/Edit so the prompt's "you do not modify code" rule is enforced mechanically per DESIGN §12 |
| `--max-turns` | per-worker turn cap (values in §6) |
| `--model` | model alias for this worker — `sonnet` / `opus` / `haiku`. Value comes from per-worker resolution (see §2 *Model selection*) |
| `--add-dir` | repeated per entry in `state.json["inspect_dirs"]` (forwarded by `claude_p`'s `add_dirs` param). Used only by inspect-bucket workers (classifier, planner, reconciler) so their sandboxed Read/Grep/Glob and allowlisted Bash verbs can reach sibling repos referenced in the task. See §2 *Inspect directories* |
| `--dangerously-skip-permissions` | acting *and* run-and-read workers (implementer, integrator, validator) — suppresses all permission prompts for unattended Bash and file writes. **Not** applied to inspect workers — they run in the real repo cwd (no worktree isolation), so the blast-radius assumption that justifies skip-permissions doesn't hold. The `Bash(<verb>:*)` patterns in `INSPECT_TOOLS` pre-approve listed verbs at the CLI level; anything else (e.g. `rm`, redirect-to-file) falls through and is rejected in non-interactive mode |

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
- **classifier, planner, reconciler, integrator, validator** — not caught
  locally; propagates to `main()`, which aborts with state saved for
  `--resume`.

`claude_p()` logs a non-fatal warning when the envelope `terminal_reason` is not
`"completed"` (e.g. `"max_turns"`).

Maps to `DESIGN.md`: §7 (worker contract), §2 (CLI subprocess form).

---

## 4. Phase walkthrough (`centella.py`)

| Phase | Function(s) | What it does |
|-------|-------------|--------------|
| Preflight | `preflight` | git identity, clean working tree, `claude` CLI version, live `claude -p` smoke test. Run-id collisions are detected later in the flow (filesystem side in `State.rename_to()` post-classify; git side in `setup-run.sh`'s branch-creation step) — they cannot be checked in preflight because the final `run_id` isn't known until phase_classify completes. Smoke test bypassed by `--skip-smoke`; preflight skipped entirely on `--resume` |
| 1 Classify | `phase_classify` | one classifier worker → categories + questions. Returned categories are filtered against the 8-name whitelist in `CATEGORIES` (mirrors DESIGN §4); `die()` if none survive |
| 0 Clarify | `gather_answers` | if questions and interactive: collect; non-interactive: write `pending-questions.json`, exit code 10; `--no-clarify` skips clarification entirely per DESIGN §11 — intent questions dropped, source-of-truth resolved from preference or defaulted to `codebase` with a warning |
| 2 Plan | `phase_plan` | one planner worker per category, awaited concurrently via `gather_or_cancel` (a small wrapper around `asyncio.gather` defined in `centella.py`) under an `asyncio.Semaphore(max_parallel)`; the first worker exception cancels its siblings and propagates to `main()` |
| 2½ Reconcile | `phase_reconcile` | compute set of `requires` capability tags with no matching `provides` across merged planner output. If empty: short-circuit (no worker spawn, plan unchanged). Else: spawn one reconciler worker that emits renames / added_provides / added_subtasks / unresolvable. Orchestrator applies the first three mechanically; if `unresolvable` is non-empty, `die()` with the reconciler's diagnosis (DESIGN §5, §14). |
| 3 Schedule | `schedule`, `validate_plan` | merge plans, build the global DAG, Kahn topological sort into waves; cycle → `die()` |
| 4 Setup | `phase_execute` head → `setup-run.sh` | create the run branch `centella/runs/<run-id>` and its worktree (per-run, isolated from any other run) |
| 5 Execute | `phase_execute`, `settle_subtask`, `integrate_wave`, `validate_wave` | per wave: implementers awaited concurrently via `gather_or_cancel` under a fresh `asyncio.Semaphore(max_parallel)` (separate instance from Phase 2's), then integrate, then re-validate. If any subtask in the wave ends `blocked` or `failed`, `phase_execute` aborts the run *before* `integrate_wave` is called — the blocker is recorded in `state.json` and the run resumes with `--resume` |
| 6 Finalize | `phase_finalize` → `finalize.sh`, `cleanup.sh` | merge run branch into working branch; post-merge sanity checks; push the run branch and open a PR (unless `--no-push`); record push / PR outcome in `run.json` |
| Post-run Judge | `phase_judge`, `judge_capture` | standalone post-run phase (not part of main orchestrate flow): reads `calls.ndjson`, runs one `judge_capture()` per record in parallel under `asyncio.Semaphore(max_parallel)`, writes per-record verdicts to `<judge-dir>/<call_id>.json` and a summary `INDEX.json`; uses `prompts/judge.md` rubric |
| Post-run Heal | `HealState`, `heal_baseline`, `heal_apply_patch`, `heal_replay_patched`, `request_patch`, `phase_heal` | heal-loop phases: `HealState` persists failing_samples / baseline / history / best_so_far at `<heal-dir>/<call_type>/state.json`; `heal_baseline(call_type, failing_records, n, heal_dir, caps, st, models)` runs n unpatched replays per record + judge, writes baseline verdicts + state; `heal_apply_patch(call_type, iter_n, patch_text, anchor_match, heal_dir, failing_records)` materialises patched prompts under `iter-<N>/patched-prompts/`; `heal_replay_patched(call_type, iter_n, n, heal_dir, caps, st, models)` runs n patched replays per record + judge, appends iteration record to state.history; `request_patch(state, iter_n, st, caps, models)` invokes the `patch_generator` worker (schema `SCHEMAS["patch_generator"]`, SID `heal-patch-<call_type>-iter<N>`, prompt from `prompts/patch_generator.md`) and returns `(anchor, replacement)` — raises `ValueError` if the returned anchor is not a literal substring of the resolved prompt body (code-enforced per the prompts-are-advisory principle); `phase_heal(call_type, failing_records, heal_dir, caps, st, models, request_patch_fn=None, n, config)` drives the full baseline→loop→report cycle; `request_patch_fn` defaults to the real `request_patch` when `None`, or accepts a sync/async 2-arg stub for testing |

`phase_classify` runs before `gather_answers` because the question set depends
on the classification.

Between Phase 3 and Phase 4, `write_plan()` persists the merged plan
(`.centella/plan.json`) and per-subtask spec files
(`.centella/subtasks/<id>.json`), and `detect_test_runner()` scans for a
deterministic test harness (pytest, npm, go, cargo, make) — stored in
`state['test_runner']` for `validate_wave`'s fast path.

Maps to `DESIGN.md`: §3.

---

## 5. Deterministic enforcement points

All in `centella.py`, in execution order. This is the concrete catalogue behind
`DESIGN.md` §12 ("prompts advisory, code enforces").

### Preflight (before any LLM work)
| Check | Catches |
|-------|---------|
| `resolve_source_of_truth()` at startup | invalid value in `centella.toml`, `CENTELLA_SOURCE_OF_TRUTH`, or `--source-of-truth` — caught before any worker spawns, not mid-planner |
| `resolve_models()` at startup | invalid model alias in `centella.toml`, any `CENTELLA_MODEL[_*]` env var, or any `--model[-*]` CLI flag — caught before any worker spawns |
| `git user.email` / `user.name` set | commits would fail silently without identity |
| working tree clean | dirty tree → ambiguous diffs, corrupt merge history |
| `claude --version` ≥ `MIN_CLAUDE_CLI` (currently `(2, 1, 22)`) | CLI too old for `--json-schema` (introduced for `claude -p` in v2.1.22) — replaces the cryptic "unknown option" message a stale CLI used to produce |
| `_check_gh_cli(no_push)` — `gh` installed, `gh auth status` ok, `origin` remote present | finalize would fail at push/PR after the full run already ran. Short-circuited when `--no-push` is passed (env / TOML mirrors). |
| live `claude -p` smoke test | auth failure or network problem |

Run-id collisions are detected outside preflight because the final `run_id` is only known after `phase_classify` returns. There are two natural collision points:

| Check | Where | Catches |
|-------|-------|---------|
| `State.rename_to(new_run_id)` refuses if the target dir exists | `orchestrate()` after `phase_classify` | `.centella/runs/<run-id>/` already exists on disk |
| `setup-run.sh` preserves an existing `centella/runs/<run-id>` branch instead of creating it | wave-execute phase | A pre-existing branch with the same name (treated as a resume; the run picks up wherever the branch was left) |

The bootstrap directory `.centella/runs/_bootstrap-<6hex>/` is used until classify completes; the rename is atomic on POSIX same-filesystem.

`--skip-smoke` bypasses only the live smoke test (used by the test harness); the CLI version check and the `gh` check still run because they are local and read-only, and skipping them would defer a confusing failure to mid-run.

### Phase 1 checks — `phase_classify`
| Check | Catches |
|-------|---------|
| classifier-returned categories filtered against the 8-name whitelist `CATEGORIES` (mirrors DESIGN §4) | classifier hallucinating a category outside the eight |
| `die()` if no category survives the filter | a run with no valid domain for any planner |

### Phase 2½ checks — `phase_reconcile`
| Check | Catches |
|-------|---------|
| reconciler's `unresolvable` array non-empty → `die()` with the worker's diagnosis | genuine gaps where no planner produced a needed capability and no plausible connector subtask can be inferred |
| reconciler output validated against `SCHEMAS["reconciler"]` | malformed reconciler response (caught by `claude_p`'s schema gate; structurally invalid output is retried once, then escalated) |
| after applying reconciler output, the unresolved-requires set is recomputed; non-empty → `die()` | the reconciler's renames/added_subtasks/added_provides didn't actually close every gap (e.g., a new subtask itself has unresolved `requires`) — fail-loud rather than progress to `validate_plan` with a still-broken graph |

### Plan validation — `validate_plan` (after scheduling, before persisting the plan)
| Check | Catches |
|-------|---------|
| ids match domain prefix (`bugfix-`, `feat-`, `refactor-`, `perf-`, `test-`, `deps-`, `config-`, `docs-`) | cross-domain collisions, audit ambiguity |
| no `size: large` subtasks | planner violated the sizing constraint |
| no empty `success_criteria_seed` | implementer has no criteria starting point |
| every `depends_on` id exists | dangling edges silently dropped by the scheduler |
| every `requires` tag has a provider | unresolvable cross-domain dependency |

### Per-subtask checks — in `settle_subtask`, every worker result
| Check | Catches | On failure |
|-------|---------|-----------|
| `validate_result()` cross-field invariants | `complete` with empty/failing criteria; `handoff` with no checkpoint file; `blocked` with no blocker; `failed` with no summary; `needs-clarification` with no `clarification_question` or no `checkpoint_path` | **Terminal** |
| `validate_result()` criteria file exists | fabricated `criteria_results`, no real criteria file | **Terminal** |
| `check_branch_has_commits()` | `complete` claim, nothing committed | **Retryable** |
| dirty worktree check | uncommitted changes that vanish on integration | **Retryable** |
| `verify_criteria_lock()` — before every re-invocation | criteria file changed after the hash was stored | raises `WorkerError`, run aborts |
| `lock_criteria()` | stores the sha256 of the criteria file on first settled result | — |

**Proposal-only criteria revision (DESIGN §9, both halves):**

| Check | Catches | On failure |
|-------|---------|-----------|
| `_proposal_structurally_valid()` — when implementer result includes `criteria_revision_proposal` | empty fields; evidence that cites no real path in the worktree | **Rejected**: criteria file unchanged, lock unchanged, rejection logged to `state.json["criteria_revisions"]` |
| `apply_criteria_revision()` + `record_criteria_revision()` — when proposal passes | (n/a: this is the approval path) | **Approved**: new criteria file written, lock re-hashed to match, approval logged; if the implementer's status was `failed`, one retry against the revised criteria is granted (`revision_retries`, hardcoded cap 1) |

The orchestrator approves only on the **structural minimum** — non-empty
proposed text + evidence that references at least one path which actually
exists in the worktree — per DESIGN §12: code judges only what can be
checked mechanically, never semantic merit. A reviewer wanting stronger
judgment reads `state.json["criteria_revisions"]` after the run.

| `check_diff_scope()` | `.centella/` `.git/` `.claude/` in the diff | **Terminal** (protected path); scope-volume warning is non-fatal (triggered when `files_likely_touched` is non-empty *and* touched > max(3× expected, 5), or when touched > 15 regardless of the planner's estimate) |
| `validate_checkpoint()` — on `incomplete-handoff` | required section missing; required section empty/whitespace; required section contains only a placeholder token (`none`/`n/a`/`na`/`tbd`/`nothing`/`unknown`/`todo`/`pending`/`—`/`--`/`-`/`?`, trailing `.`/`!`/`?`/`…` ignored and repeated `?` collapsed); a path listed under `## Files touched` no longer exists in the worktree and is not flagged `[deleted]` | returns `blocked` |
| `_retryable_failure(summary)` — on `status='failed'` returned by the worker itself | worker self-report of failure | routed through the retry policy using the worker's `summary` as the reason; because `summary` is freeform text it almost never matches a retryable marker, so in practice a self-reported `failed` is **terminal** on first occurrence |

### Wave-level checks (after integration, before validation)
| Check | Catches |
|-------|---------|
| `check_criteria_files_exist()` | missing criteria files, before spending validation workers |
| test-runner short-circuit | a passing deterministic runner (pytest/npm/go/cargo/make) skips the LLM validator |
| `scan_conflict_markers()` | unresolved `<<<<<<<` markers in the run-branch worktree after integration |

On a re-validation failure round, the orchestrator re-runs `settle_subtask`
for each failing subtask (which may produce additional fixing commits) and
then `integrate.sh` to re-merge the delta into the run branch, before the
next round of validation. The cap on this loop is `wave_revalidation_rounds`
(5) — exceeding it aborts the run with the failing subtask ids.

### Post-integrator checks (after an integrator handles a conflict)
These verify the integrator honored DESIGN §6's *behavioral* conflict-
resolution contract — the integrator prompt itself
(`prompts/integrator.md`) carries the behavioral spec (read every
involved subtask's intent and frozen criteria, preserve each side's
intent, call irreconcilable cases a `design-conflict`); the
orchestrator only checks the outcome.

| Check | Catches |
|-------|---------|
| `check_merge_committed()` | integrator returned `resolved` but left the worktree mid-merge (`MERGE_HEAD` present) or with staged-uncommitted changes — **terminal**: merge aborted, run stops |
| `check_integrator_commit()` | integrator merge commit touched `.centella/` files — non-fatal warning, recorded to `state.json` |
| integrator status `design-conflict` / `failed` | unresolvable conflict — **terminal**: in-progress merge aborted, the run branch left clean at the last good wave, diagnosis saved, run stops |

### Post-finalize checks
Both are **non-fatal warnings** (logged, not `die()`) — the user is told to
verify manually; the run does not abort.

| Check | Catches | On failure |
|-------|---------|-----------|
| most-recent merge subject contains `'centella:'` (read from `git log --merges -1 --format=%s HEAD`) | finalize merged to the wrong branch | non-fatal warning |
| `git diff --stat centella/runs/<run-id>..HEAD` empty | merge silently dropped changes | non-fatal warning |

### Resume integrity — `validate_resume_state()`
Enforces (one half of) DESIGN §6's "the run branch is the resume contract"
invariant — state.json's `waves`/`completed_waves` say *which* wave to
resume; the never-reset `centella/runs/<run-id>` branch holds *the work*
every prior wave produced. Both must be coherent for resume to be safe.

On `--resume`: asserts `task` is present and non-empty; asserts `waves`,
`completed_waves`, `subtask_status` are well-formed *if present*. `waves` is
intentionally optional — a run interrupted before scheduling has none, and
`main()` handles that case with a clearer message. Rejects corrupt or
hand-edited state without rejecting a legitimately-early interruption.

`orchestrate()` also re-resolves the source-of-truth preference on every
`--resume` and overwrites `state.json`'s `source_of_truth_pref` with the
fresh value, so a change to `centella.toml` or `CENTELLA_SOURCE_OF_TRUTH`
between runs takes effect on resume.

Per-worker models are likewise re-resolved on every `--resume` from the
current CLI flags, env, and `centella.toml`. They are *not* persisted in
`state.json` (they are startup config, not run state), so a change to
`CENTELLA_MODEL`, `--model`, or the per-worker overrides between runs
takes effect immediately on resume.

### Concurrency model
The orchestrator runs on a single `asyncio` event loop. Each `claude -p`
worker is spawned via `asyncio.create_subprocess_exec` (wrapped by the
`run_proc` helper) and awaited; parallel workers within a wave run
concurrently via `gather_or_cancel` — a small `asyncio.gather` wrapper that,
on the first exception, cancels every other in-flight task and awaits its
finalization before re-raising — under an `asyncio.Semaphore` bounded by
`max_parallel`. Because every mutator runs on the single loop, `State` carries
no lock — coroutines only interleave at `await` points, which never fall inside
a `st.data[k] = v; st.save()` pair. `State.save()` still writes to a temp file
then `os.replace()` for atomicity against process crash. On `KeyboardInterrupt`,
`asyncio.run` cancels pending tasks; `run_proc`'s catch-all `BaseException`
handler kills any in-flight child process before re-raising, so no `claude`
or `git` subprocesses are orphaned.

---

## 6. Caps and their values

Defaults in `DEFAULT_CAPS` and the per-worker `claude_p` call sites.

### Code-enforced caps (the orchestrator counts these)
| Loop | Cap | On cap |
|------|-----|--------|
| subtask continuations (re-spawns of an implementer for the same subtask — both context-exhaustion handoffs *and* mid-execution clarifications consume from the same budget) | 3 (`subtask_continuations`) | return `blocked`; fatal at wave boundary |
| corrective retries of a *retryable* failure per subtask (`failed_retries`) | 1 | return `failed` |
| wave re-validation rounds | 5 | abort run, name failing subtasks |
| total worker invocations per run | 40 (`--max-workers`) | abort, state saved for `--resume` |
| concurrent workers within a wave | 4 (`--max-parallel`) | throughput throttle |
| turns per `claude -p` call | per worker (below) | worker stops; implementer → `incomplete-handoff` |
| per-worker wall-clock (`worker_timeout_sec`) | 5400 s (90 min) | worker killed; implementer → `incomplete-handoff` |

`--max-turns` by worker: classifier 20, planner 40, validator 40, integrator
60, implementer 120. For the implementer, 120 turns and 90 minutes both apply —
whichever trips first.

A seventh cap, `revision_retries`, is hardcoded to **1** (not in
`DEFAULT_CAPS`): an implementer that returns `failed` and proposes an
approved criteria revision gets at most one retry against the new
criteria. The literal is intentional — DESIGN §9's lock discipline
exists to prevent a stuck model from weakening its own bar, and a
tunable that allowed repeated revisions would re-open exactly that
loophole. The cap is 1 because DESIGN §9's burden of proof ("hard
evidence") is hard to meet once and harder to meet twice, not because
the architecture forbids >1; promoting it to `DEFAULT_CAPS` would
invite values that defeat the lock.

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
| cross-field invariant violation | Terminal | `validate_result` |
| diff touched a protected path | Terminal | `check_diff_scope` |
| worker-level error (timeout, schema-invalid twice) | Terminal | `WorkerError` path |

`settle_subtask` routes every failure through `_retryable_failure` via the
`fail()` helper. Retryable consumes the retry cap; terminal ends the subtask on
first occurrence.

---

## 7. Git worktree mechanics (`scripts/*.sh`)

Every script takes a `RUN_ID` as its first positional argument (after any flags) so the per-run namespacing is explicit at the shell boundary, not implicit through `cwd`.

| Script | Behavior |
|--------|----------|
| `setup-run.sh <run-id>` | Creates `centella/runs/<run-id>` **only if absent** — never force-resets it (an existing branch carries completed waves; resetting it would destroy resume state). Records the working branch (HEAD-at-run-start) to `.centella/runs/<run-id>/working-branch` on first run only. Adds the run-branch worktree at `.centella/runs/<run-id>/worktrees/staging` if missing. Appends `.centella/` to the repo's `.git/info/exclude` (idempotent). Safe on `--resume`. |
| `new-worktree.sh <id> <run-id>` | Creates `centella/subtasks/<run-id>/<id>` worktree at `.centella/runs/<run-id>/worktrees/<id>` branched off the current `centella/runs/<run-id>` tip; reuses an existing worktree/branch if present (resume after handoff). Prints the absolute worktree path. The run-branch (`centella/runs/…`) and subtask-branch (`centella/subtasks/…`) prefixes are deliberately disjoint so neither is an ancestor ref of the other — git's loose ref store cannot hold a ref AT a path and another ref UNDER that same path simultaneously. |
| `integrate.sh <id> <run-id>` | From repo root, inside the run-branch worktree (`.centella/runs/<run-id>/worktrees/staging`): `git merge --no-ff centella/subtasks/<run-id>/<id>`. Exit 0 clean; exit 1 on conflict, leaving the worktree mid-merge for an integrator; exit 2 on precondition failure (run-branch worktree or subtask branch missing) — `integrate_wave` treats exit 2 as fatal via `die()` and does *not* spawn an integrator, since the worktree-less case would fail in confusing ways. |
| `finalize.sh <run-id>` | The *local-merge half* of finalize. Checks out the working branch (recorded by `setup-run.sh`), merges `centella/runs/<run-id>` into it. On conflict: `git merge --abort`, restore the working branch clean, exit non-zero with manual-merge instructions; run branch left intact. The push and PR step is **not** in this script — it lives in `push_and_open_pr()` in `centella.py` (see below) so it can compose the PR body with `compose_pr_body()`, write `run.json`, and emit Python-style multi-line failure messages. |
| `cleanup.sh [--run-id <id> \| --all-runs \| --bootstrap \| --legacy] [--branches]` | Default (no flag): scans `.centella/runs/*/state.json` for the most-recently-failed run (most recent without `finished_at`), confirms y/N, then removes only that run's worktrees + prunes git metadata. State dir stays as audit. `--run-id <id>` is an explicit single-run cleanup (worktrees only). `--all-runs` runs the same per-run cleanup across every run dir under `.centella/runs/` (excluding `_bootstrap-*`). `--bootstrap` removes orphaned `_bootstrap-*` directories (runs that died before classify completed; not enumerable by `discover_runs`). `--legacy` removes the pre-per-run layout (`.centella/state.json`, `.centella/worktrees/`, `centella/staging` branch). `--branches` (combinable with `--run-id` or `--all-runs`) additionally deletes the matching run branches (`centella/runs/<id>` and `centella/subtasks/<id>/*`); without `--branches`, branches are kept as an audit trail. State dirs are always preserved by `cleanup.sh` — full nuke-the-run is the Ctrl-C path in the orchestrator (`_cleanup_on_abnormal_exit(full_purge=True)`). |

A run branch `centella/runs/<run-id>` is never reset once created — this is the invariant `--resume` depends on. See `DESIGN.md` §6 ("the run branch is the resume contract").

### Push and PR (Python; called from `phase_finalize`)

The push + PR step is implemented in Python rather than in `finalize.sh`. It runs after `finalize.sh` succeeds, unless `--no-push` is in effect.

| Function (centella.py) | Behavior |
|--------|----------|
| `push_and_open_pr(st, no_verify)` | Pushes `centella/runs/<run-id>` to `origin` (with `--no-verify` appended if the CLI flag was set), then opens a PR via `gh pr create --base <working-branch> --head centella/runs/<run-id> --title centella: <run-id> --body-file -` piping `compose_pr_body(st.data, st.run_id)`. Push failure dies non-zero with a multi-line message naming both branches, the captured stderr, and the exact retry command; updates `.centella/runs/<run-id>/run.json` with `push_error`. PR-creation failure is **non-fatal**: logs a warning with the pushed-branch URL and the retry command; updates `run.json` with `pr_error` and returns 0 (the run is complete; only the PR is missing). |
| `_check_gh_cli(no_push)` | Preflight gate. Short-circuits silently when `--no-push` is set. Else verifies `shutil.which("gh")`, `gh auth status` exits 0, and `git remote get-url origin` succeeds. Each failure dies with an actionable message + the `--no-push` escape hatch. |

`--no-push` skips the entire push + PR step (the run completes with the local merge only). CLI flag, `CENTELLA_NO_PUSH` env, `no_push = true` in `centella.toml` — same precedence pattern as `--source-of-truth`. `--no-verify` is CLI-only and only affects the push step (worker `git commit`s inside worktrees still run all hooks).

Maps to `DESIGN.md`: §6 (Finalization — Push and PR).

---

## 8. Coordination directory layout (`.centella/`)

Created in the main repository (not in any worktree — worktrees are disposable).
`setup-run.sh` git-excludes `.centella/` by appending it to the target
repo's `.git/info/exclude` rather than to the user's tracked `.gitignore`
(we deliberately do not modify files the user has committed).

Every run's artifacts live under `.centella/runs/<run-id>/`. The parent
`.centella/` directory is otherwise empty of run data; it only hosts the
`runs/` directory. Two concurrent runs in the same repository share no
coordination state.

```
.centella/
└── runs/
    └── <run-id>/                    (or _bootstrap-<6hex> pre-classify)
        ├── state.json               run state — see field table below
        ├── run.json                 sidecar — see field table below
        ├── working-branch           the branch finalize.sh returns to (HEAD-at-run-start)
        ├── plan.json                merged planner output
        ├── subtasks/<id>.json       per-subtask spec handed to each implementer
        ├── criteria/<id>.md         frozen success criteria, sha256-locked
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

`run.json` fields (a minimal sidecar enabling `centella --list` and resume
discovery without parsing the full `state.json`):

| Field | Shape | Notes |
|-------|-------|-------|
| `run_id` | str | the run identifier (matches the directory name and the branch suffix) |
| `branch` | str | the run branch — always `centella/runs/<run_id>` |
| `working_branch` | str | the branch HEAD-at-run-start; the PR base and the finalize merge target |
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

A corrupt sidecar is flagged but does not block the rest of the system; `centella --list` will render that run with `status=corrupt-sidecar` and the user can inspect or delete the file.

`centella --list` derives a single status per run via `_derive_run_status(run_json, state_json)`. The taxonomy is checked in priority order — earlier rows fire first:

| Status | When it fires | Typical next step |
|--------|---------------|-------------------|
| `corrupt-sidecar` | `run.json` violates one of the three invariants above | inspect the file under `.centella/runs/<id>/run.json` |
| `push-failed` | `push_error` is set | re-run `git push -u origin centella/<id>` after fixing the access issue |
| `pr-failed` | `pr_error` is set (and push succeeded) | re-run `gh pr create` manually using the command logged at finalize |
| `done-pushed-pr` | `pr_url` is set | the happy path: PR open, work merged locally |
| `done-pushed-no-pr` | `pushed_at` set but `pr_url` not | rare: push succeeded, PR wasn't attempted (e.g., gh removed between push and PR) |
| `done-local` | `finished_at` set, no `pushed_at` | the user passed `--no-push`; push manually if desired |
| `in-progress` | none of the above | the run is still active (or died very early); resume with `--resume --run-id <id>` |

`RUN_STATUSES` in `centella.py` declares the seven values; a test coupling check asserts the tuple matches every value `_derive_run_status` can return.

`state.json` fields. This table is canonical: every field the orchestrator
writes to `st.data` must appear here, and every field listed here must be
written somewhere in `orchestrator/centella.py`. The coupling test in
`tests/test_state_fields.py` enforces parity in both directions against the
`STATE_FIELDS` tuple in `centella.py`.

| Field | Shape | Purpose |
|-------|-------|---------|
| `task` | str | the task description passed on the command line |
| `started_at` | ISO-8601 str | wall-clock time at run start |
| `finished_at` | ISO-8601 str | wall-clock time at successful finalize |
| `waves` | list[list[str]] | scheduled subtask ids per wave (from `schedule`) |
| `completed_waves` | int | index of the next wave to run (resume cursor) |
| `subtask_status` | dict[str, str] | per-subtask terminal status |
| `criteria_locks` | dict[str, str] | sha256 per subtask — structural enforcement of DESIGN §9 |
| `criteria_revisions` | list[dict] | append-only audit log of every proposed revision (approved and rejected, DESIGN §9 proposal channel) |
| `blocked` | dict[str, str] | per-subtask blocker reason when a wave aborts |
| `worker_count` | int | running total of `claude -p` invocations against `max_total_workers` |
| `telemetry` | dict | calls, cost_usd, input_tokens, output_tokens — printed at run end |
| `categories` | list[str] | classifier output, post-whitelist filtering |
| `classifier_questions` | list[dict] | intent questions the classifier surfaced |
| `answers` | dict[str, str] | user answers to classifier questions (and source-of-truth) |
| `needs_source_of_truth` | bool | whether classifier asked for source-of-truth disambiguation |
| `source_of_truth_pref` | str | resolved preference (`codebase` / `research` / `both` / `ask`) |
| `no_clarify` | bool | whether `--no-clarify` was passed |
| `verbosity` | str | resolved verbosity level (`quiet` / `normal` / `stream` / `debug`); re-resolved fresh on every run, including `--resume`, so the user can dial up or down without editing state |
| `inspect_dirs` | list[str] | extra absolute paths granted to inspect-bucket workers (classifier, planner, reconciler) via `--add-dir`. Resolved from `--inspect-dir` / `CENTELLA_INSPECT_DIRS` / `inspect_dirs` in `centella.toml`; re-resolved fresh on every run, including `--resume`, so the user can add or remove paths without editing state. Empty list when nothing is configured |
| `test_runner` | list[str] | detected short-circuit test command |
| `integrator_failure` | dict | unresolvable conflict from `integrate_wave` (non-fatal signal log) |
| `integrator_warnings` | dict[str, str] | non-fatal commit warnings from `integrate_wave` (non-fatal signal log) |
| `scope_warnings` | dict[str, dict] | oversized-diff warnings from `check_diff_scope` (non-fatal signal log) |

`pending-questions.json` (written by `gather_answers` on non-TTY exit, read by
the plugin skill in `commands/centella.md`):

| Field | Shape | Notes |
|-------|-------|-------|
| `questions` | array of `{id, question, why_underivable?}` | the classifier-surfaced intent questions not already in `--answers` |
| `source_of_truth` | bool | true if the user still needs to answer the source-of-truth question |
| `source_of_truth_hint` | string \| null | the env-var/`centella.toml` hint to show the user when `source_of_truth` is true |

`answers.json` (written by the plugin skill, passed back via
`--answers .centella/answers.json`):

| Field | Shape | Notes |
|-------|-------|-------|
| `<question id>` | string | one entry per question id from `pending-questions.json.questions[].id` |
| `source_of_truth` | `"codebase"` / `"research"` / `"both"` | required only when `pending-questions.json.source_of_truth` was true |

The checkpoint schema — seven required sections, enforced by
`validate_checkpoint()`: *Frozen success criteria*, *Current status*, *Files
touched*, *Decisions made*, *Evidence gate status*, *Next action*, *Open
unknowns*. The validator enforces three layers: (a) every section header
must be present; (b) every section must carry non-whitespace content; (c)
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
  the orchestrator's preference resolution (see §2) decides whether to
  actually ask.
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
  Phase-1 classifier),
  `criteria_revision_proposal` (DESIGN §9 proposal channel:
  `{proposed_text, evidence}` — both required when the object is present;
  orchestrator decides via the structural-minimum check described in §5).
- **integrator** — required: `incoming_subtask`, `status` (`resolved` /
  `design-conflict` / `failed`). Optional: `resolution_summary`,
  `diagnosis` (read as a fallback for `resolution_summary` when
  diagnosing a non-`resolved` outcome).
- **validator** — required: `results` (array of `{subtask_id,
  all_criteria_met (both required), failing?}`). `failing` is optional in the
  schema; when omitted, the orchestrator treats it as an empty list.
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

Schemas are embedded as Python dicts in `centella.py` and serialized inline.

Maps to `DESIGN.md`: §7, §14.

---

## 10. Telemetry — NDJSON envelope and call_type mapping

Maps to `DESIGN.md`: §14.

### NDJSON envelope schema

Every `claude_p()` invocation appends one JSON object (one line) to
`.centella/runs/<run-id>/calls.ndjson` immediately after the call returns.
The file is opened for append at run start and is never truncated — it is
always a valid NDJSON file through the last complete line even under a hard
kill. It is never read by the orchestrator at runtime; reading is a
post-run operation performed by the judge and heal skills.

| Field | Type | Notes |
|-------|------|-------|
| `call_id` | str (UUID v4) | unique identifier for this invocation; referenced by judge verdicts |
| `run_id` | str | the run identifier — matches the directory name under `.centella/runs/` |
| `call_type` | str | one of `WORKER_TYPES`: `classifier`, `planner`, `reconciler`, `implementer`, `integrator`, `validator` |
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
.centella/runs/<run-id>/calls.ndjson
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
| `validator`    | `VALIDATOR_SYSTEM` constant in `orchestrator/centella.py` | **not a file** — the validator prompt is short (two sentences) and has no behavioral tuning that would benefit from out-of-tree editing; it is the `VALIDATOR_SYSTEM` string constant embedded in `centella.py` |

`VALIDATOR_SYSTEM` is the single exception to the "prompts in `prompts/`"
rule. It is called out explicitly here because the judge skill needs to
know *where to find the system prompt for each call_type* when it builds
the replay context for self-healing — it reads `prompts/<call_type>.md`
for five of the six types, and reads `VALIDATOR_SYSTEM` directly from
`centella.py` for the sixth.

`resolve_prompt(call_type: str) -> tuple[str, str, str]` centralises
this asymmetry: given any member of `WORKER_TYPES`, it returns
`(source_kind, content, location_hint)` where `source_kind` is `"file"`
or `"constant"`, `content` is the prompt body, and `location_hint` is
either the relative path `"prompts/<call_type>.md"` or the literal
`"orchestrator/centella.py:VALIDATOR_SYSTEM"`. The heal loop's
patch-generator worker calls `resolve_prompt` instead of duplicating the
file/constant branching itself. Raises `ValueError` for an unknown
`call_type`.

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
| `test__read_toml_key.py` | `_read_toml_key()` — the shared `centella.toml` line parser used by both resolvers |
| `test_gather_answers_validation.py` | the source-of-truth validation gate in `gather_answers()` |
| `test_retryable_failure.py` | `_retryable_failure()`, **including a coupling test** that the retryable markers actually appear in the strings emitted by `check_branch_has_commits` and the inline dirty-worktree check |
| `test_state_fields.py` | `STATE_FIELDS` tuple parity, in both directions: against the §8 field table, and against every `st.data[...] = …` / `setdefault(...)` write in `centella.py`. This is the mechanism §8's "this table is canonical" claim relies on |
| `test_validate_plan.py` | `validate_plan()` (every rule in §5) |
| `test_validate_result.py` | `validate_result()` (every status-branch invariant) |
| `test_check_merge_committed.py` | `check_merge_committed()` (real-git fixtures) |
| `test_criteria_revision.py` | `_proposal_structurally_valid()`, `apply_criteria_revision()`, `record_criteria_revision()` (DESIGN §9 proposal channel) |
| `test_validator_tools.py` | `RUN_TOOLS` composition and `validate_wave`'s wiring — pins that the validator gets `Bash` but never `Write`/`Edit`, enforcing the DESIGN §12 "you do not modify code" rule mechanically (per §3 of this document) |
| `test_inspect_tools.py` | `INSPECT_TOOLS` composition and the three inspect-callsite wirings (classifier, planner, reconciler) — pins that the inspect bucket grants `Bash(<verb>:*)` patterns but never `Write`/`Edit` or bare `Bash`, the same DESIGN §12 enforcement applied to workers that don't get `--dangerously-skip-permissions` |
| `test_resolve_inspect_dirs.py` | `resolve_inspect_dirs()` precedence (CLI → env → TOML → `[]`), `~` expansion, dedup, and `STATE_FIELDS` membership |
| `test_resolve_prompt.py` | `resolve_prompt()` — every `WORKER_TYPES` member returns a valid triple; parity/coupling test; validator returns `("constant", …, "orchestrator/centella.py:VALIDATOR_SYSTEM")`; unknown call_type raises |
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
